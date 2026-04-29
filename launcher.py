"""
launcher.py — Thin process manager for the trading bot.

Run this instead of main.py:
    python launcher.py

What it does:
  • Spawns main.py as a subprocess (auto-starts on launcher startup)
  • Hosts a tiny REST API on port 8081 so the dashboard can start/stop the bot
    even when the bot itself (port 8080) is offline
  • Kills any stale process on port 8080 before spawning, so port-in-use crashes
    don't prevent restarts
  • CORS is open so the Vite dev server (port 5173) can reach it

Endpoints:
  GET  /status  → { running: bool, pid: int|null, exit_code: int|null }
  POST /start   → spawn main.py if not already running
  POST /stop    → terminate the bot process gracefully
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

LAUNCHER_PORT = 8081
BOT_PORT = 8080
BOT_SCRIPT = Path(__file__).parent / "main.py"

# Resolve the venv Python — prefer the project venv over sys.executable so
# the bot always runs with the correct interpreter regardless of how the
# launcher itself was started (e.g. system Python, VS Code terminal, etc.).
def _find_venv_python() -> str:
    here = BOT_SCRIPT.parent
    candidates = [
        here.parent / ".venv" / "Scripts" / "python.exe",  # Windows
        here.parent / ".venv" / "bin" / "python",           # Unix/macOS
        here / ".venv" / "Scripts" / "python.exe",
        here / ".venv" / "bin" / "python",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable  # fallback: hope the current interpreter works

BOT_PYTHON = _find_venv_python()

app = FastAPI(title="Bot Launcher")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_proc: subprocess.Popen | None = None


def _process_running() -> bool:
    return _proc is not None and _proc.poll() is None


def _port_free(port: int, timeout: float = 5.0) -> bool:
    """Return True once the port stops being bound (or timeout expires)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.25)
    return False


def _kill_port(port: int) -> None:
    """Best-effort: terminate any process currently listening on port."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                try:
                    psutil.Process(conn.pid).terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    except Exception:
        pass  # best-effort only


def _spawn() -> subprocess.Popen:
    _kill_port(BOT_PORT)
    _port_free(BOT_PORT, timeout=5.0)
    return subprocess.Popen(
        [BOT_PYTHON, str(BOT_SCRIPT)],
        cwd=str(BOT_SCRIPT.parent),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    running = _process_running()
    return {
        "running": running,
        "pid": _proc.pid if running else None,
        "exit_code": _proc.poll() if _proc else None,
        "timestamp": time.time(),
    }


@app.post("/start")
def start():
    global _proc
    if _process_running():
        return {"ok": False, "reason": "already running", "pid": _proc.pid}
    _proc = _spawn()
    return {"ok": True, "pid": _proc.pid}


@app.post("/stop")
def stop():
    global _proc
    if not _process_running():
        return {"ok": False, "reason": "not running"}
    _proc.terminate()
    try:
        _proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _proc.kill()
    exit_code = _proc.returncode
    _proc = None
    _port_free(BOT_PORT, timeout=5.0)  # wait for port to fully release
    return {"ok": True, "exit_code": exit_code}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[launcher] Starting bot: {BOT_SCRIPT}")
    _proc = _spawn()
    print(f"[launcher] Bot PID {_proc.pid}. Launcher API on port {LAUNCHER_PORT}.")
    uvicorn.run(app, host="0.0.0.0", port=LAUNCHER_PORT, log_level="warning")
