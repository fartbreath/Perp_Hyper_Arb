"""
logger.py — Structured logging to console + rotating file, plus Telegram alerts.

Usage:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("Market discovered", market_id="abc123", fees_enabled=False)
"""
import logging
import logging.handlers
import os
import shutil
import sys
import asyncio
import threading
from pathlib import Path
from typing import Any, Optional

import requests

import config

# ── Paths ─────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "data"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

# ── Windows-safe rotating file handler ───────────────────────────────────────

class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Windows-compatible RotatingFileHandler.

    On Windows, os.rename() fails with WinError 32 when any other process (or
    the uvicorn reloader's sibling worker) still holds the log file open.
    We override rotate() to use shutil.copy2 + truncate (the 'copytruncate'
    pattern) which works even when the file is held open elsewhere.
    """

    def rotate(self, source: str, dest: str) -> None:
        if sys.platform != "win32":
            super().rotate(source, dest)
            return
        try:
            shutil.copy2(source, dest)
            # Truncate the source in-place so existing file handles remain valid
            with open(source, "w", encoding="utf-8"):
                pass
        except (PermissionError, FileNotFoundError):
            pass  # Best-effort: log rotation failed but logging continues


# ── Formatter ─────────────────────────────────────────────────────────────────

class _ContextFormatter(logging.Formatter):
    """Appends structured kwargs to the log line: key=value ..."""

    FMT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

    def __init__(self):
        super().__init__(fmt=self.FMT, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in logging.LogRecord.__dict__ and not k.startswith("_")
            and k
            not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "asctime",
            )
        }
        if extras:
            pairs = " ".join(f"{k}={v!r}" for k, v in extras.items())
            return f"{base}  {pairs}"
        return base


_STANDARD_LOGRECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class RingBufferHandler(logging.Handler):
    """In-memory circular buffer of the last MAX log records, queryable by level/module/search."""

    MAX = 5000

    # Modules that emit high-frequency DEBUG noise (WebSocket frame messages).
    # These are still written to console/file but suppressed from the ring buffer
    # so they don't drown out business-logic log entries from other modules.
    _NOISY_DEBUG_MODULES = frozenset({"client", "pm_client", "hl_client"})

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            module = record.name.split(".")[-1]
            # Suppress DEBUG-level noise from WS client modules — they flood the
            # ring buffer and make it impossible to read business-logic logs.
            if record.levelno == logging.DEBUG and module in self._NOISY_DEBUG_MODULES:
                return

            _safe = (str, int, float, bool, type(None))
            extras = {
                k: v if isinstance(v, _safe) else str(v)
                for k, v in record.__dict__.items()
                if k not in _STANDARD_LOGRECORD_ATTRS and not k.startswith("_")
            }
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "module": module,
                "msg": record.getMessage(),
                "extras": extras,
            }
            with self._lock:
                self._records.append(entry)
                if len(self._records) > self.MAX:
                    del self._records[: len(self._records) - self.MAX]
        except Exception:
            self.handleError(record)

    def get_recent(
        self,
        limit: int = 200,
        level: str = "ALL",
        module: Optional[str] = None,
        search: Optional[str] = None,
    ) -> list[dict]:
        with self._lock:
            records = list(reversed(self._records))   # newest first
        if level and level != "ALL":
            min_lvl = getattr(logging, level.upper(), 0)
            records = [r for r in records if getattr(logging, r["level"], 0) >= min_lvl]
        if module:
            records = [r for r in records if r["module"] == module]
        if search:
            sl = search.lower()
            records = [
                r for r in records
                if sl in r["msg"].lower()
                or any(sl in str(v).lower() for v in r["extras"].values())
            ]
        return records[:limit]

    def all_modules(self) -> list[str]:
        with self._lock:
            return sorted({r["module"] for r in self._records})


# Module-level singleton — imported by api_server
ring_buffer = RingBufferHandler()


# ── Root logger setup (call once at import) ───────────────────────────────────

def _setup_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(logging.DEBUG)

    # In-memory ring buffer — always first so nothing is missed
    root.addHandler(ring_buffer)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(_ContextFormatter())
    root.addHandler(console)

    # Rotating file handler — 5 MB per file, keep 5 backups
    file_h = _SafeRotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(_ContextFormatter())
    root.addHandler(file_h)


_setup_root_logger()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ── Structured log helper ─────────────────────────────────────────────────────

class BotLogger:
    """Thin wrapper that attaches structured kwargs to every log call."""

    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, msg: str, **kwargs: Any) -> None:
        self._log.log(level, msg, extra=kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.CRITICAL, msg, **kwargs)


def get_bot_logger(name: str) -> BotLogger:
    return BotLogger(name)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> None:
    """Fire-and-forget Telegram message. Silently skips if unconfigured."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception:  # noqa: BLE001
        pass  # Never let alerting crash the bot


async def alert(text: str) -> None:
    """Async Telegram alert — runs sync HTTP in a thread to avoid blocking."""
    await asyncio.get_event_loop().run_in_executor(None, _send_telegram, text)


def alert_sync(text: str) -> None:
    """Synchronous Telegram alert for use outside async contexts."""
    _send_telegram(text)
