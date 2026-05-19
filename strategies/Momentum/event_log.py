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
  HEDGE_FAIL          — GTD hedge placement rejected by CLOB (crosses book or exception)
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
from typing import Any, Optional

from logger import get_bot_logger

log = get_bot_logger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
MOMENTUM_EVENTS_PATH    = _DATA_DIR / "momentum_events.jsonl"
SIGNAL_EVENTS_PATH      = _DATA_DIR / "signal_events.jsonl"
POSITION_SNAPSHOTS_PATH = _DATA_DIR / "position_snapshots.jsonl"


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


# ── ML-D1: Signal event log ───────────────────────────────────────────────────
# Appends one JSON line per evaluated scan-diag entry (all markets that reached
# the vol/delta computation stage) to data/signal_events.jsonl.
# The log is the primary training-data source for Model D (signal quality).
#
# Only entries with an observed_z value are written — earlier-stage skips
# (stale_spot, no_vol, beyond_horizon) lack the ML features needed for training.
#
# gate_result encodes which gates passed, derived from skip_reason:
#   z_pass           — delta_pct >= effective_threshold (effective_gap_pct >= 0)
#   funding_pass     — did not exit at funding_stale or funding_block gate
#   depth_share_pass — did not exit at depth_share_yes or depth_share_no gate
#   twap_pass        — did not exit at twap_yes gate
#   entered          — signal fired AND execution was attempted (skip_reason == signal_fired)

_SKIP_REASONS_FUNDING_FAIL    = frozenset({"funding_stale", "funding_block"})
_SKIP_REASONS_DEPTH_SHARE_FAIL = frozenset({"depth_share_yes", "depth_share_no"})
_SKIP_REASONS_CLOB_FAIL       = frozenset({"no_ask", "thin_clob"})
_SKIP_REASONS_TWAP_FAIL       = frozenset({"twap_yes"})


def emit_signal_events_batch(scan_diags: list[dict]) -> None:
    """Write all ML-eligible scan diag entries from one scan pass.

    Called once per scan at the end of the scan loop.  Eligible = has
    ``observed_z`` populated (reached the vol/delta computation stage).

    Fire-and-forget: never raises; errors are logged at DEBUG level only.
    """
    eligible = [d for d in scan_diags if d.get("observed_z") is not None]
    if not eligible:
        return
    now = datetime.now(timezone.utc)
    hour_utc    = now.hour
    day_of_week = now.weekday()  # 0=Mon … 6=Sun
    lines: list[str] = []
    for d in eligible:
        skip = d.get("skip_reason", "")
        entered = skip == "signal_fired" and bool(d.get("executed", True))
        gate_result = {
            "z_pass":           (d.get("effective_gap_pct") or 0.0) >= 0.0,
            "funding_pass":     skip not in _SKIP_REASONS_FUNDING_FAIL,
            "clob_depth_pass":  skip not in _SKIP_REASONS_CLOB_FAIL,
            "depth_share_pass": skip not in _SKIP_REASONS_DEPTH_SHARE_FAIL,
            "twap_pass":        skip not in _SKIP_REASONS_TWAP_FAIL,
        }
        row: dict[str, Any] = {
            "schema_version":    2,
            "ts":                now.isoformat(),
            "market_id":         d.get("market_id", ""),
            "underlying":        d.get("underlying", ""),
            "bucket_type":       d.get("market_type", ""),
            "side":              d.get("side", ""),
            "z_score":           d.get("observed_z"),
            "delta_pct":         d.get("delta_pct"),
            "effective_threshold": d.get("effective_threshold"),
            "effective_gap_pct": d.get("effective_gap_pct"),
            "vol_regime":        d.get("vol_regime"),
            "funding_rate":      d.get("funding_rate"),
            "depth_share":       d.get("yes_depth_share"),
            "ask_depth_usd":     d.get("ask_depth_usd"),
            "twap_dev_bps":      d.get("twap_dev_bps"),
            "tte_seconds":       d.get("tte_seconds"),
            "sigma_ann":         d.get("sigma_ann"),
            "sigma_tau":         d.get("sigma_tau"),
            "hour_utc":          hour_utc,
            "day_of_week":       day_of_week,
            "gate_result":       gate_result,
            "entered":           entered,
            "skip_reason":       skip,
        }
        lines.append(json.dumps(row))
    try:
        with SIGNAL_EVENTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        log.debug("signal_events.jsonl write error", exc=str(exc))


# ── ML-D1: Position snapshot buffer ──────────────────────────────────────────
# Appends one JSON line per intra-hold position tick to data/position_snapshots.jsonl.
# Contains delta_sl_would_fire and upfrac_below flags needed for Model D training.

def write_position_snapshot(
    *,
    market_id: str,
    side: str,
    token_id: str,
    underlying: str,
    tte_seconds: Optional[float],
    current_token_price: Optional[float],
    oracle_delta_pct: Optional[float],
    hl_mark_price: Optional[float],
    hl_depth_imbalance: Optional[float],
    delta_sl_would_fire: bool,
    upfrac_below: bool,
    last_upfrac: Optional[float],
    coin_sl: float,
    exit_flag: bool,
    reason: str,
) -> None:
    """Append one position-snapshot row to position_snapshots.jsonl.

    Fire-and-forget: never raises; errors are logged at DEBUG level only.
    """
    row: dict[str, Any] = {
        "schema_version":    1,
        "ts":                datetime.now(timezone.utc).isoformat(),
        "market_id":         market_id,
        "side":              side,
        "token_id":          token_id,
        "underlying":        underlying,
        "tte_seconds":       round(tte_seconds, 2) if tte_seconds is not None else None,
        "current_token_price": round(current_token_price, 6) if current_token_price is not None else None,
        "oracle_delta_pct":  round(oracle_delta_pct, 6) if oracle_delta_pct is not None else None,
        "hl_mark_price":     round(hl_mark_price, 4) if hl_mark_price is not None else None,
        "hl_depth_imbalance": round(hl_depth_imbalance, 6) if hl_depth_imbalance is not None else None,
        "delta_sl_would_fire": delta_sl_would_fire,
        "upfrac_below":      upfrac_below,
        "last_upfrac":       round(last_upfrac, 6) if last_upfrac is not None else None,
        "coin_sl":           round(coin_sl, 6),
        "exit_flag":         exit_flag,
        "reason":            reason,
    }
    try:
        with POSITION_SNAPSHOTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:
        log.debug("position_snapshots.jsonl write error", exc=str(exc))
