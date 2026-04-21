"""
strategies.Momentum.event_log — Event-typed entry/exit journal (Item 6).

Appends one JSON line per momentum trading event to data/momentum_events.jsonl.
This file is the single source of truth for structured trade lifecycle events.

Event types (adapted from PTB-bot _emit_trading_analysis schema):
  SESSION_START       — emitted once when the MomentumScanner starts
  BUY_SUBMIT          — limit/market buy order submitted to PM CLOB
  BUY_CANCEL_TIMEOUT  — unfilled entry cancelled after MOMENTUM_ORDER_CANCEL_SEC
  BUY_FILL            — entry order confirmed filled (WS or REST)
  BUY_FAILED          — all placement/retry attempts failed; no position opened
  SELL_SUBMIT         — resting TP SELL limit pre-armed after entry fill
  SELL_CLOSE          — position closed (TP fill, SL, near-expiry, or RESOLVED)
  SELL_FAILED         — exit order placement failed (manual intervention needed)
  HEDGE_SUBMIT        — GTD hedge BUY limit order placed on opposite token
  HEDGE_FILL          — GTD hedge outcome recorded at market resolution (filled_won / filled_lost)
  HEDGE_EXPIRED       — GTD hedge order never filled; all detection layers exhausted
  HEDGE_CANCEL        — GTD hedge order cancelled (delta recovered or take-profit win)

Schema (schema_version=1):
  {
    "schema_version": 1,
    "ts":             "<ISO UTC>",
    "event":          "<EVENT_TYPE>",
    "market_id":      "<condition_id>",
    "market_title":   "<str>",
    "underlying":     "<BTC|ETH|…>",
    "market_type":    "<bucket_5m|…>",
    "side":           "<YES|NO|UP|DOWN>",
    ...event-specific fields...
  }

Usage:
    from strategies.Momentum.event_log import emit
    emit("BUY_SUBMIT", market_id="0x…", side="YES", order_price=0.845, size_usd=12.5)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger import get_bot_logger

log = get_bot_logger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
MOMENTUM_EVENTS_PATH = _DATA_DIR / "momentum_events.jsonl"


def emit(event: str, **kwargs: Any) -> None:
    """Append one structured event record to momentum_events.jsonl.

    Fire-and-forget: never raises; errors are logged at DEBUG level only.
    All keyword arguments are merged into the record alongside the fixed
    schema_version, ts, and event fields.
    """
    row: dict[str, Any] = {
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    row.update(kwargs)
    try:
        with MOMENTUM_EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:
        log.debug("momentum_events.jsonl write error", exc=str(exc))


def read_recent(n: int = 200) -> list[dict]:
    """Return the last *n* event records from momentum_events.jsonl.

    Returns an empty list if the file does not exist or cannot be read.
    Records are returned newest-first.
    """
    try:
        lines = MOMENTUM_EVENTS_PATH.read_text(encoding="utf-8").splitlines()
        parsed: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(parsed) >= n:
                break
        return parsed
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.debug("momentum_events.jsonl read error", exc=str(exc))
        return []
