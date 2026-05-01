"""
api_server.py — FastAPI REST server for the monitoring webapp

Runs as an asyncio task alongside the bot:
    asyncio.create_task(run_api_server())

Read-only GET endpoints are open. State-mutating POST endpoints require
a Bearer token when API_SECRET is configured (recommended in production).
Set API_SECRET env var; leave empty to disable auth (development only).
CORS origins are controlled via the API_CORS_ORIGINS env var.

Shared state is held in the module-level `state` object, updated by
the bot components (main.py injects references after startup).
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from logger import get_bot_logger, ring_buffer, warn_ring_buffer

log = get_bot_logger(__name__)

# ── Shared state container ────────────────────────────────────────────────────
# Bot components populate this after startup; API reads it.

@dataclass
class BotState:
    # Health
    started_at: float = field(default_factory=time.time)
    pm_ws_connected: bool = False
    hl_ws_connected: bool = False
    last_heartbeat_ts: float = 0.0
    paper_trading: bool = config.PAPER_TRADING
    bot_active: bool = True  # mirrors config.BOT_ACTIVE; updated by /bot endpoint

    # Positions (filled by risk engine)
    positions: dict = field(default_factory=dict)       # condition_id → Position dict

    # Markets (filled by pm_client)
    markets: dict = field(default_factory=dict)         # condition_id → PMMarket dict

    # Signals (appended by mispricing scanner)
    signals: list = field(default_factory=list)         # list of MispricingSignal dict

    # Funding (filled by hl_client)
    funding: dict = field(default_factory=dict)         # coin → FundingSnapshot dict

    # Active quotes (filled by maker strategy)
    active_quotes: dict = field(default_factory=dict)   # token_id → ActiveQuote dict

    # Maker signals (filled by maker strategy) — token_id → signal dict
    maker_signals: dict = field(default_factory=dict)
    maker_ref: Any = None  # live MakerStrategy instance; set by main.py

    # Momentum signals (appended by momentum scanner) — list of MomentumSignal dicts
    momentum_signals: list = field(default_factory=list)
    momentum_ref: Any = None  # live MomentumScanner instance; set by main.py

    # Opening neutral signals and live instance
    opening_neutral_ref: Any = None  # live OpeningNeutralScanner instance; set by main.py

    # Agent shadow log (filled by agent)
    agent_shadow_log: list = field(default_factory=list)

    # Data quality metrics (filled by state-sync loop in main.py)
    data_quality: dict = field(default_factory=dict)

    # P&L summary (cached, recomputed every 30 s in state_sync_loop)
    pnl_summary: dict = field(default_factory=dict)

    # Live component references — set once at startup by main.py
    monitor_ref: Any = None   # PositionMonitor — used by manual-close endpoint
    pm_ref: Any = None        # PMClient — used by manual-close endpoint
    risk_ref: Any = None      # RiskEngine — used by manual-close endpoint


# Module-level singleton — main.py populates this
state = BotState()

# ── Server-Sent Events (SSE) infrastructure ────────────────────────────────────
# Each connected frontend client gets its own asyncio.Queue.  broadcast_sse()
# pushes a serialised JSON message to all active queues; dead/full queues are
# pruned automatically.  main.py's state_sync_loop calls broadcast_sse() after
# every state update so clients receive live data without polling.

_sse_clients: list[asyncio.Queue] = []

# P&L summary cache — recomputed at most every 30 s (CSV read is O(n) per call)
_pnl_cache: dict = {}
_pnl_cache_ts: float = 0.0


async def broadcast_sse(data: dict) -> None:
    """Push a state-change message to all connected SSE clients."""
    if not _sse_clients:
        return
    payload = f"data: {json.dumps(data)}\n\n"
    dead: list[asyncio.Queue] = []
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def build_live_state() -> dict:
    """Build the SSE state bundle from the current module-level `state`.

    Mirrors the response format of each live REST endpoint so the frontend
    can use SSE data as a drop-in replacement for individual polling calls.
    Excludes heavy/expensive data (trades CSV, live wallet query) — those
    endpoints continue to use REST polling on a slower cadence.
    """
    global _pnl_cache, _pnl_cache_ts
    now_t = time.time()

    # ── Health ────────────────────────────────────────────────────────────────
    uptime_s = now_t - state.started_at
    dq = state.data_quality
    data_issues = (
        dq.get("sub_rejected_count", 0) > 0
        or dq.get("no_book_count", 0) > 5
    )
    try:
        from fill_simulator import get_fill_session_stats
        fill_stats = get_fill_session_stats()
    except Exception:
        fill_stats = {"adverse_triggers_session": 0, "hl_max_move_pct_session": 0.0}
    health = {
        "status": "running",
        "uptime_seconds": round(uptime_s, 1),
        "pm_ws_connected": state.pm_ws_connected,
        "hl_ws_connected": state.hl_ws_connected,
        "last_heartbeat_ts": state.last_heartbeat_ts,
        "last_heartbeat_age_s": round(now_t - state.last_heartbeat_ts, 1)
            if state.last_heartbeat_ts > 0 else None,
        "paper_trading": state.paper_trading,
        "agent_auto": config.AGENT_AUTO,
        "bot_active": config.BOT_ACTIVE,
        "data_quality": dq,
        "data_issues": data_issues,
        "adverse_triggers_session": fill_stats["adverse_triggers_session"],
        "adverse_threshold_pct": config.PAPER_ADVERSE_SELECTION_PCT,
        "hl_max_move_pct_session": fill_stats["hl_max_move_pct_session"],
        "timestamp": now_t,
    }

    # ── Positions ─────────────────────────────────────────────────────────────
    positions_list = list(state.positions.values())
    positions = {"positions": positions_list, "count": len(positions_list), "timestamp": now_t}

    # ── Risk (computed from positions) ────────────────────────────────────────
    total_pm = sum(float(p.get("size_usd", 0)) for p in positions_list if p.get("venue") == "PM")
    total_hl = sum(abs(float(p.get("size_usd", 0))) for p in positions_list if p.get("venue") == "HL")
    risk = {
        "pm_exposure_usd": round(total_pm, 2),
        "pm_exposure_limit": config.MAX_TOTAL_PM_EXPOSURE,
        "pm_exposure_pct": round(total_pm / config.MAX_TOTAL_PM_EXPOSURE * 100, 1),
        "hl_notional_usd": round(total_hl, 2),
        "hl_notional_limit": config.MAX_HL_NOTIONAL,
        "hl_notional_pct": round(total_hl / config.MAX_HL_NOTIONAL * 100, 1),
        "open_positions": len(state.positions),
        "max_concurrent_positions": config.MAX_CONCURRENT_POSITIONS,
        "hard_stop_threshold": config.HARD_STOP_DRAWDOWN,
        "max_pm_per_market": config.MAX_PM_EXPOSURE_PER_MARKET,
        "paper_trading": state.paper_trading,
        "timestamp": now_t,
    }

    # ── Markets (with active-quote enrichment) ────────────────────────────────
    def _valid_prob(v) -> bool:
        return v is not None and 0.0 <= float(v) <= 1.0

    market_list = []
    for cid, m in state.markets.items():
        token_id_yes = m.get("token_id_yes", "")
        q_bid = state.active_quotes.get(token_id_yes)
        q_ask = state.active_quotes.get(f"{token_id_yes}_ask")

        if q_bid is not None and _valid_prob(q_bid.get("price")):
            bid_price, bid_src = q_bid.get("price"), "active_quote"
        else:
            bk = m.get("yes_book_bid")
            bid_price = bk if _valid_prob(bk) else None
            bid_src = "pm_book" if bid_price is not None else None

        if q_ask is not None and _valid_prob(q_ask.get("price")):
            ask_price, ask_src = q_ask.get("price"), "active_quote"
        else:
            bk = m.get("yes_book_ask")
            ask_price = bk if _valid_prob(bk) else None
            ask_src = "pm_book" if ask_price is not None else None

        book_ts = m.get("yes_book_ts")
        book_age_s = round(now_t - book_ts, 1) if book_ts else None
        if book_age_s is None:
            dw: str | None = "no_data"
        elif book_age_s > 120:
            dw = "very_stale"
        elif book_age_s > 30:
            dw = "stale"
        else:
            dw = None

        market_list.append({
            **m,
            "bid_price": bid_price, "ask_price": ask_price,
            "bid_source": bid_src,  "ask_source": ask_src,
            "book_age_s": book_age_s, "data_warning": dw,
            "quoted": q_bid is not None,
        })
    markets = {"markets": market_list, "count": len(market_list), "timestamp": now_t}

    # ── Signals ───────────────────────────────────────────────────────────────
    # Use [:200] to match the in-memory cap so the SSE bundle does not silently
    # truncate below what the Signals page requests (useSignals(100)).
    signals = {
        "signals": sorted(state.signals, key=lambda s: s.get("score", 0.0), reverse=True)[:200],
        "total": len(state.signals),
        "timestamp": now_t,
    }
    momentum_signals = {
        "signals": list(reversed(state.momentum_signals))[:200],
        "total": len(state.momentum_signals),
        "timestamp": now_t,
    }

    # ── Maker quotes ──────────────────────────────────────────────────────────
    quotes_list = sorted(
        state.active_quotes.values(),
        key=lambda x: x.get("posted_at", 0),
        reverse=True,
    )
    for q in quotes_list:
        q["age_seconds"] = round(now_t - float(q.get("posted_at", now_t)), 1)
    maker_quotes = {
        "quotes": quotes_list,
        "count": len(quotes_list),
        "strategy_enabled": config.STRATEGY_MAKER_ENABLED,
        "timestamp": now_t,
    }

    # ── Maker signals ─────────────────────────────────────────────────────────
    deployed_keys = set(state.active_quotes.keys())
    maker_signals_list = []
    for tid, s in state.maker_signals.items():
        mkt = state.markets.get(s.get("market_id", ""), {})
        maker_signals_list.append({
            **s,
            "market_title": mkt.get("title", s.get("market_id", "")),
            "is_deployed": tid in deployed_keys,
            "age_seconds": round(now_t - float(s.get("ts", now_t)), 1),
        })
    maker_signals_list.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    maker_signals = {
        "signals": maker_signals_list,
        "count": len(maker_signals_list),
        "strategy_enabled": config.STRATEGY_MAKER_ENABLED,
        "timestamp": now_t,
    }

    # ── Capital ───────────────────────────────────────────────────────────────
    if state.maker_ref is not None:
        _dep = state.maker_ref.deployed_capital
        _avail = state.maker_ref.available_capital
        _in_pos = state.maker_ref._risk.get_state().get("total_pm_capital_deployed", 0.0)
    else:
        _dep, _avail, _in_pos = 0.0, config.PAPER_CAPITAL_USD, 0.0
    capital = {
        "total_budget": config.PAPER_CAPITAL_USD,
        "deployed": round(_dep, 2),
        "in_positions": round(_in_pos, 2),
        "available": round(_avail, 2),
        "mode": config.MAKER_DEPLOYMENT_MODE,
        "timestamp": now_t,
    }

    # ── Funding ───────────────────────────────────────────────────────────────
    funding_bundle = {"funding": state.funding, "timestamp": now_t}

    # ── P&L (cached — recomputed every 30 s from trades CSV) ─────────────────
    if now_t - _pnl_cache_ts > 30.0:
        try:
            rows = _load_trades_csv()
            _dt_now = datetime.now(timezone.utc)
            today_rows = [r for r in rows if _row_age_days(r, _dt_now) < 1]
            week_rows  = [r for r in rows if _row_age_days(r, _dt_now) < 7]

            def _sp(rs: list[dict]) -> float:
                return sum(float(r.get("pnl", 0)) for r in rs)

            _pnl_cache = {
                "today":             round(_sp(today_rows), 4),
                "week":              round(_sp(week_rows),  4),
                "all_time":          round(_sp(rows),       4),
                "trade_count_today": len(today_rows),
                "trade_count_week":  len(week_rows),
                "trade_count_all":   len(rows),
                "timestamp":         now_t,
            }
            _pnl_cache_ts = now_t
        except Exception:
            pass
    pnl_bundle = _pnl_cache if _pnl_cache else None

    bundle: dict = {
        "health": health,
        "positions": positions,
        "risk": risk,
        "markets": markets,
        "funding": funding_bundle,
        "signals": signals,
        "momentum_signals": momentum_signals,
        "maker_quotes": maker_quotes,
        "maker_signals": maker_signals,
        "capital": capital,
    }
    if pnl_bundle:
        bundle["pnl"] = pnl_bundle
    return bundle


# Path to trades CSV — use absolute path like risk.py to avoid CWD issues
TRADES_CSV = Path(__file__).parent / "data" / "trades.csv"
# Accounting module data files (written by accounting.py)
ACCT_LEDGER_CSV = Path(__file__).parent / "data" / "acct_ledger.csv"
ACCT_POSITIONS_JSON = Path(__file__).parent / "data" / "acct_positions.json"
# Path to paper-fill log CSV written by fill_simulator.py
FILLS_CSV = Path(__file__).parent / "data" / "fills.csv"
# Path to order event log written by pm_client.py
ORDERS_CSV = Path(__file__).parent / "data" / "orders.csv"
# Path to market outcomes file written by monitor.py
MARKET_OUTCOMES_JSON = Path(__file__).parent / "data" / "market_outcomes.json"
# Path to persisted config overrides — survives bot restarts
_OVERRIDES_FILE = Path(__file__).parent / "config_overrides.json"


def _save_overrides(changes: dict) -> None:
    """Merge only the changed key-value pairs into the on-disk overrides file.

    By updating only the keys that just changed (rather than rewriting the
    entire snapshot from in-memory values), manual edits made directly to the
    JSON file while the bot is running are never silently overwritten by a
    subsequent UI save.
    """
    existing: dict = {}
    if _OVERRIDES_FILE.exists():
        try:
            existing = json.loads(_OVERRIDES_FILE.read_text())
        except Exception:
            pass
    existing.update(changes)
    try:
        _OVERRIDES_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        log.warning("Failed to save config overrides", exc=str(exc))

# ── Auth ──────────────────────────────────────────────────────────────────────

def require_auth(authorization: str = Header(default="")) -> None:
    """FastAPI dependency: enforce Bearer token auth when API_SECRET is set."""
    if config.API_SECRET and authorization != f"Bearer {config.API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Perp Hyper Arb API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.API_CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Config ───────────────────────────────────────────────────────────────

_MUTABLE_CONFIG = {
    "paper_trading":              ("PAPER_TRADING",              bool),
    "agent_auto":                 ("AGENT_AUTO",                 bool),
    "auto_approve":               ("AUTO_APPROVE",               bool),
    "mispricing_scan_interval":    ("MISPRICING_SCAN_INTERVAL",    int),
    "strategy_mispricing":        ("STRATEGY_MISPRICING_ENABLED", bool),
    "strategy_maker":             ("STRATEGY_MAKER_ENABLED",      bool),
    "fill_check_interval":        ("FILL_CHECK_INTERVAL",         int),
    "paper_fill_probability":     ("PAPER_FILL_PROBABILITY",      float),
    "max_buy_no_yes_price":       ("MAX_BUY_NO_YES_PRICE",        float),
    "mispricing_market_cooldown_seconds": ("MISPRICING_MARKET_COOLDOWN_SECONDS", int),
    "momentum_market_cooldown_seconds":   ("MOMENTUM_MARKET_COOLDOWN_SECONDS",   int),
    "min_strike_distance_pct":    ("MIN_STRIKE_DISTANCE_PCT",     float),
    "kalshi_enabled":             ("KALSHI_ENABLED",              bool),
    "kalshi_require_nd2_confirmation": ("KALSHI_REQUIRE_ND2_CONFIRMATION", bool),
    "kalshi_min_deviation":       ("KALSHI_MIN_DEVIATION",        float),
    "kalshi_match_max_strike_diff": ("KALSHI_MATCH_MAX_STRIKE_DIFF", float),
    "kalshi_match_max_expiry_days": ("KALSHI_MATCH_MAX_EXPIRY_DAYS", float),
    "max_concurrent_positions":     ("MAX_CONCURRENT_POSITIONS",       int),
    # Market-making config
    "reprice_trigger_pct":         ("REPRICE_TRIGGER_PCT",          float),
    "max_quote_age_seconds":       ("MAX_QUOTE_AGE_SECONDS",        int),
    "maker_min_edge_pct":           ("MAKER_MIN_EDGE_PCT",           float),
    "max_concurrent_maker_positions":     ("MAX_CONCURRENT_MAKER_POSITIONS",     int),
    "max_concurrent_mispricing_positions": ("MAX_CONCURRENT_MISPRICING_POSITIONS", int),
    "paper_fill_prob_base":        ("PAPER_FILL_PROB_BASE",         float),
    "paper_fill_prob_new_market":  ("PAPER_FILL_PROB_NEW_MARKET",   float),
    "paper_adverse_selection_pct": ("PAPER_ADVERSE_SELECTION_PCT",  float),
    "paper_adverse_fill_multiplier": ("PAPER_ADVERSE_FILL_MULTIPLIER", float),
    "maker_coin_max_loss_usd":     ("MAKER_COIN_MAX_LOSS_USD",      float),
    "maker_exit_hours":            ("MAKER_EXIT_HOURS",             float),
    "maker_exit_tte_frac":         ("MAKER_EXIT_TTE_FRAC",          float),
    "maker_entry_tte_frac":         ("MAKER_ENTRY_TTE_FRAC",         float),
    "maker_batch_size":             ("MAKER_BATCH_SIZE",              int),
    "maker_positions_per_underlying": ("MAX_MAKER_POSITIONS_PER_UNDERLYING", int),
    "maker_quote_size_pct":         ("MAKER_SPREAD_SIZE_PCT",         float),
    "maker_quote_size_min":         ("MAKER_SPREAD_SIZE_MIN",         float),
    "maker_quote_size_max":         ("MAKER_SPREAD_SIZE_MAX",         float),
    "maker_quote_size_new_market":  ("MAKER_SPREAD_SIZE_NEW_MARKET",  float),
    "hedge_threshold_usd":         ("HEDGE_THRESHOLD_USD",          float),
    "hedge_rebalance_pct":         ("HEDGE_REBALANCE_PCT",          float),
    "hedge_min_interval":          ("HEDGE_MIN_INTERVAL",           float),
    "hedge_debounce_secs":         ("HEDGE_DEBOUNCE_SECS",          float),
    "deployment_mode":             ("MAKER_DEPLOYMENT_MODE",        str),
    "paper_capital_usd":           ("PAPER_CAPITAL_USD",            float),
    # Quote guards & new-market logic
    "maker_min_quote_price":       ("MAKER_MIN_QUOTE_PRICE",        float),
    "maker_min_volume_24hr":        ("MAKER_MIN_VOLUME_24HR",        float),
    "maker_max_tte_days":          ("MAKER_MAX_TTE_DAYS",           int),
    "new_market_age_limit":        ("NEW_MARKET_AGE_LIMIT",         int),
    "new_market_wide_spread":      ("NEW_MARKET_WIDE_SPREAD",       float),
    "new_market_pull_spread":      ("NEW_MARKET_PULL_SPREAD",       float),
    # Inventory skew
    "inventory_skew_coeff":        ("INVENTORY_SKEW_COEFF",         float),
    "inventory_skew_max":          ("INVENTORY_SKEW_MAX",           float),
    # Hedge sizing
    "max_hl_notional":             ("MAX_HL_NOTIONAL",              float),
    # Position monitor
    "profit_target_pct":           ("PROFIT_TARGET_PCT",            float),
    "stop_loss_usd":               ("STOP_LOSS_USD",                float),
    "exit_days_before_resolution": ("EXIT_DAYS_BEFORE_RESOLUTION",  int),
    "min_hold_seconds":            ("MIN_HOLD_SECONDS",             int),
    # Risk limits
    "max_pm_exposure_per_market":  ("MAX_PM_EXPOSURE_PER_MARKET",   float),
    "max_total_pm_exposure":       ("MAX_TOTAL_PM_EXPOSURE",        float),
    "hard_stop_drawdown":          ("HARD_STOP_DRAWDOWN",           float),
    # Signal scoring
    "min_signal_score_mispricing": ("MIN_SIGNAL_SCORE_MISPRICING",  float),
    "min_signal_score_maker":      ("MIN_SIGNAL_SCORE_MAKER",       float),
    "maker_min_signal_score_5m":   ("MAKER_MIN_SIGNAL_SCORE_5M",    float),
    "maker_min_signal_score_1h":   ("MAKER_MIN_SIGNAL_SCORE_1H",    float),
    "maker_min_signal_score_4h":   ("MAKER_MIN_SIGNAL_SCORE_4H",    float),
    "maker_exit_tte_frac_5m":      ("MAKER_EXIT_TTE_FRAC_5M",       float),
    "score_weight_edge":           ("SCORE_WEIGHT_EDGE",            float),
    "score_weight_source":         ("SCORE_WEIGHT_SOURCE",          float),
    "score_weight_timing":         ("SCORE_WEIGHT_TIMING",          float),
    "score_weight_liquidity":      ("SCORE_WEIGHT_LIQUIDITY",       float),
    # Hedge control
    "maker_hedge_enabled":         ("MAKER_HEDGE_ENABLED",          bool),
    "maker_max_book_age_secs":     ("MAKER_MAX_BOOK_AGE_SECS",      int),
    # Per-market imbalance skew
    "maker_imbalance_skew_coeff":  ("MAKER_IMBALANCE_SKEW_COEFF",  float),
    "maker_imbalance_skew_max":    ("MAKER_IMBALANCE_SKEW_MAX",    float),
    "maker_imbalance_skew_min_ct": ("MAKER_IMBALANCE_SKEW_MIN_CT", float),
    # Incentive spread gate & imbalance hard-stops
    "maker_min_incentive_spread":     ("MAKER_MIN_INCENTIVE_SPREAD",      float),
    "maker_max_imbalance_contracts":  ("MAKER_MAX_IMBALANCE_CONTRACTS",   int),
    "maker_naked_close_enabled":      ("MAKER_NAKED_CLOSE_ENABLED",       bool),
    "maker_naked_close_contracts":    ("MAKER_NAKED_CLOSE_CONTRACTS",     int),
    "maker_naked_close_secs":         ("MAKER_NAKED_CLOSE_SECS",          float),
    "maker_max_fills_per_leg":        ("MAKER_MAX_FILLS_PER_LEG",         int),
    "maker_max_contracts_per_market": ("MAKER_MAX_CONTRACTS_PER_MARKET",  int),
    # CLOB depth gate (UI existed but was non-functional — now wired up)
    "maker_min_depth_to_quote":       ("MAKER_MIN_DEPTH_TO_QUOTE",        int),
    "maker_depth_thin_threshold":     ("MAKER_DEPTH_THIN_THRESHOLD",      int),
    "maker_depth_spread_factor_thin": ("MAKER_DEPTH_SPREAD_FACTOR_THIN",  float),
    "maker_depth_spread_factor_zero": ("MAKER_DEPTH_SPREAD_FACTOR_ZERO",  float),
    # Volatility & drift guards
    "maker_vol_filter_pct":           ("MAKER_VOL_FILTER_PCT",            float),
    "maker_adverse_drift_reprice":    ("MAKER_ADVERSE_DRIFT_REPRICE",     float),
    # Second-leg profit margin
    "maker_min_spread_profit_margin": ("MAKER_MIN_SPREAD_PROFIT_MARGIN",  float),
    # Strategy 3 — Momentum Scanner
    "strategy_momentum":              ("STRATEGY_MOMENTUM_ENABLED",        bool),
    "momentum_price_band_low":        ("MOMENTUM_PRICE_BAND_LOW",          float),
    "momentum_price_band_high":       ("MOMENTUM_PRICE_BAND_HIGH",         float),
    "momentum_max_entry_usd":         ("MOMENTUM_MAX_ENTRY_USD",           float),
    "momentum_kelly_fraction":         ("MOMENTUM_KELLY_FRACTION",          float),
    "momentum_min_clob_depth":        ("MOMENTUM_MIN_CLOB_DEPTH",          float),
    "momentum_order_type":            ("MOMENTUM_ORDER_TYPE",              str),
    "momentum_delta_stop_loss_pct":    ("MOMENTUM_DELTA_STOP_LOSS_PCT",     float),
    "momentum_take_profit":            ("MOMENTUM_TAKE_PROFIT",             float),
    "momentum_min_tte_default":        ("MOMENTUM_MIN_TTE_SECONDS_DEFAULT",         int),
    "momentum_spot_max_age_secs":     ("MOMENTUM_SPOT_MAX_AGE_SECS",       float),
    "momentum_book_max_age_secs":     ("MOMENTUM_BOOK_MAX_AGE_SECS",       float),
    "momentum_vol_cache_ttl":         ("MOMENTUM_VOL_CACHE_TTL",           float),
    "momentum_vol_z_score":           ("MOMENTUM_VOL_Z_SCORE",             float),
    "momentum_min_delta_pct":         ("MOMENTUM_MIN_DELTA_PCT",           float),
    "momentum_scan_interval":         ("MOMENTUM_SCAN_INTERVAL",           int),
    "momentum_max_concurrent":        ("MOMENTUM_MAX_CONCURRENT",          int),
    "momentum_min_gap_pct":           ("MOMENTUM_MIN_GAP_PCT",             float),
    "momentum_near_expiry_time_stop_secs": ("MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS", int),
    "monitor_interval":               ("MONITOR_INTERVAL",                 int),
    # Phase B — resolution oracle near expiry
    "momentum_use_resolution_oracle_near_expiry": ("MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY", bool),
    # Phase D — hedge
    "momentum_hedge_enabled":              ("MOMENTUM_HEDGE_ENABLED",               bool),
    "momentum_hedge_price":                ("MOMENTUM_HEDGE_PRICE",                 float),
    "momentum_hedge_contracts_pct":        ("MOMENTUM_HEDGE_CONTRACTS_PCT",         float),
    "momentum_hedge_cancel_recovery_pct": ("MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT",   float),
    "momentum_hedge_suppresses_delta_sl": ("MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL",   bool),
    # Logging toggles (post-trade analysis files)
    "momentum_hedge_clob_log_enabled":     ("MOMENTUM_HEDGE_CLOB_LOG_ENABLED",      bool),
    "momentum_ticks_log_enabled":          ("MOMENTUM_TICKS_LOG_ENABLED",           bool),
    # Phase E — empirical win-rate gate
    "momentum_win_rate_gate_enabled":  ("MOMENTUM_WIN_RATE_GATE_ENABLED",   bool),
    "momentum_win_rate_gate_min_factor": ("MOMENTUM_WIN_RATE_GATE_MIN_FACTOR", float),
    "momentum_win_rate_gate_min_samples": ("MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES", int),
    # Kelly extensions
    "momentum_kelly_min_tte_seconds":         ("MOMENTUM_KELLY_MIN_TTE_SECONDS",         int),
    "momentum_kelly_persistence_enabled":    ("MOMENTUM_KELLY_PERSISTENCE_ENABLED",    bool),
    "momentum_kelly_persistence_z_boost_max": ("MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX", float),
    # Range markets (sub-strategy of Momentum)
    "momentum_range_enabled":              ("MOMENTUM_RANGE_ENABLED",              bool),
    "momentum_range_price_band_low":       ("MOMENTUM_RANGE_PRICE_BAND_LOW",       float),
    "momentum_range_price_band_high":      ("MOMENTUM_RANGE_PRICE_BAND_HIGH",      float),
    "momentum_range_max_entry_usd":        ("MOMENTUM_RANGE_MAX_ENTRY_USD",        float),
    "momentum_range_vol_z_score":          ("MOMENTUM_RANGE_VOL_Z_SCORE",          float),
    "momentum_range_min_tte_seconds":      ("MOMENTUM_RANGE_MIN_TTE_SECONDS",      int),
    # Item 2: Order cancel-and-retry
    "momentum_order_cancel_sec":           ("MOMENTUM_ORDER_CANCEL_SEC",            float),
    "momentum_slippage_cap":               ("MOMENTUM_SLIPPAGE_CAP",                float),
    "momentum_max_retries":                ("MOMENTUM_MAX_RETRIES",                 int),
    "momentum_buy_retry_step":             ("MOMENTUM_BUY_RETRY_STEP",              float),
    # Item 1: Active TP resting limit order
    "momentum_tp_resting_enabled":         ("MOMENTUM_TP_RESTING_ENABLED",          bool),
    "momentum_tp_retry_max":               ("MOMENTUM_TP_RETRY_MAX",               int),
    "momentum_tp_retry_step":              ("MOMENTUM_TP_RETRY_STEP",              float),
    # Item 4: VWAP/RoC secondary filter
    "momentum_vwap_window_sec":            ("MOMENTUM_VWAP_WINDOW_SEC",             int),
    "momentum_roc_window_sec":             ("MOMENTUM_ROC_WINDOW_SEC",              int),
    "momentum_min_vwap_dev_pct":           ("MOMENTUM_MIN_VWAP_DEV_PCT",            float),
    "momentum_min_roc_pct":                ("MOMENTUM_MIN_ROC_PCT",                 float),
    # Item 7: Probability-based SL
    "momentum_prob_sl_enabled":            ("MOMENTUM_PROB_SL_ENABLED",             bool),
    "momentum_prob_sl_pct":                ("MOMENTUM_PROB_SL_PCT",                 float),
    # Item 5: Chainlink watchdog
    "chainlink_silence_watchdog_secs":     ("CHAINLINK_SILENCE_WATCHDOG_SECS",      int),
    # RESOLVED fast-path fallback timeout
    "momentum_resolved_force_close_sec":   ("MOMENTUM_RESOLVED_FORCE_CLOSE_SEC",    int),
    # Strategy 5 — Opening Neutral
    "opening_neutral_enabled":             ("OPENING_NEUTRAL_ENABLED",             bool),
    "opening_neutral_dry_run":             ("OPENING_NEUTRAL_DRY_RUN",             bool),
    "opening_neutral_tp_enabled":          ("OPENING_NEUTRAL_TP_ENABLED",           bool),
    "opening_neutral_tp_profit_pct":       ("OPENING_NEUTRAL_TP_PROFIT_PCT",        float),
}


class ConfigPatch(BaseModel):
    paper_trading: bool | None = None
    agent_auto: bool | None = None
    auto_approve: bool | None = None
    mispricing_scan_interval: int | None = None
    strategy_mispricing: bool | None = None
    strategy_maker: bool | None = None
    fill_check_interval: int | None = None
    paper_fill_probability: float | None = None
    max_buy_no_yes_price: float | None = None
    mispricing_market_cooldown_seconds: int | None = None
    momentum_market_cooldown_seconds: int | None = None
    min_strike_distance_pct: float | None = None
    kalshi_enabled: bool | None = None
    kalshi_require_nd2_confirmation: bool | None = None
    kalshi_min_deviation: float | None = None
    kalshi_match_max_strike_diff: float | None = None
    kalshi_match_max_expiry_days: float | None = None
    max_concurrent_positions: int | None = None
    # Market-making config
    reprice_trigger_pct: float | None = None
    max_quote_age_seconds: int | None = None
    maker_min_edge_pct: float | None = None
    max_concurrent_maker_positions: int | None = None
    max_concurrent_mispricing_positions: int | None = None
    paper_fill_prob_base: float | None = None
    paper_fill_prob_new_market: float | None = None
    paper_adverse_selection_pct: float | None = None
    paper_adverse_fill_multiplier: float | None = None
    maker_coin_max_loss_usd: float | None = None
    maker_exit_hours: float | None = None
    maker_exit_tte_frac: float | None = None
    maker_entry_tte_frac: float | None = None
    maker_batch_size: int | None = None
    maker_positions_per_underlying: int | None = None
    maker_quote_size_pct: float | None = None
    maker_quote_size_min: float | None = None
    maker_quote_size_max: float | None = None
    maker_quote_size_new_market: float | None = None
    hedge_threshold_usd: float | None = None
    hedge_rebalance_pct: float | None = None
    hedge_min_interval: float | None = None
    hedge_debounce_secs: float | None = None
    deployment_mode: str | None = None
    paper_capital_usd: float | None = None
    # Quote guards & new-market logic
    maker_min_quote_price: float | None = None
    maker_min_volume_24hr: float | None = None
    maker_max_tte_days: int | None = None
    new_market_age_limit: int | None = None
    new_market_wide_spread: float | None = None
    new_market_pull_spread: float | None = None
    # Inventory skew
    inventory_skew_coeff: float | None = None
    inventory_skew_max: float | None = None
    # Hedge sizing
    max_hl_notional: float | None = None
    # Position monitor
    profit_target_pct: float | None = None
    stop_loss_usd: float | None = None
    exit_days_before_resolution: int | None = None
    min_hold_seconds: int | None = None
    # Risk limits
    max_pm_exposure_per_market: float | None = None
    max_total_pm_exposure: float | None = None
    hard_stop_drawdown: float | None = None
    # Signal scoring
    min_signal_score_mispricing: float | None = None
    min_signal_score_maker: float | None = None
    maker_min_signal_score_5m: float | None = None
    maker_min_signal_score_1h: float | None = None
    maker_min_signal_score_4h: float | None = None
    maker_exit_tte_frac_5m: float | None = None
    score_weight_edge: float | None = None
    score_weight_source: float | None = None
    score_weight_timing: float | None = None
    score_weight_liquidity: float | None = None
    # Hedge control
    maker_hedge_enabled: bool | None = None
    maker_max_book_age_secs: int | None = None
    # Per-market imbalance skew
    maker_imbalance_skew_coeff: float | None = None
    maker_imbalance_skew_max: float | None = None
    maker_imbalance_skew_min_ct: float | None = None
    # Incentive spread gate & imbalance hard-stops
    maker_min_incentive_spread: float | None = None
    maker_max_imbalance_contracts: int | None = None
    maker_naked_close_enabled: bool | None = None
    maker_naked_close_contracts: int | None = None
    maker_naked_close_secs: float | None = None
    maker_max_fills_per_leg: int | None = None
    maker_max_contracts_per_market: int | None = None
    # CLOB depth gate
    maker_min_depth_to_quote: int | None = None
    maker_depth_thin_threshold: int | None = None
    maker_depth_spread_factor_thin: float | None = None
    maker_depth_spread_factor_zero: float | None = None
    # Volatility & drift guards
    maker_vol_filter_pct: float | None = None
    maker_adverse_drift_reprice: float | None = None
    # Second-leg profit margin
    maker_min_spread_profit_margin: float | None = None
    # Market type exclusion (list — handled separately in patch_config)
    maker_excluded_market_types: list[str] | None = None
    # Strategy 3 — Momentum Scanner
    strategy_momentum: bool | None = None
    momentum_price_band_low: float | None = None
    momentum_price_band_high: float | None = None
    momentum_max_entry_usd: float | None = None
    momentum_kelly_fraction: float | None = None
    momentum_min_clob_depth: float | None = None
    momentum_order_type: str | None = None
    momentum_delta_stop_loss_pct: float | None = None
    momentum_take_profit: float | None = None
    momentum_min_tte_5m: int | None = None
    momentum_min_tte_15m: int | None = None
    momentum_min_tte_1h: int | None = None
    momentum_min_tte_4h: int | None = None
    momentum_min_tte_daily: int | None = None
    momentum_min_tte_weekly: int | None = None
    momentum_min_tte_milestone: int | None = None
    momentum_min_tte_default: int | None = None
    momentum_spot_max_age_secs: float | None = None
    momentum_book_max_age_secs: float | None = None
    momentum_vol_cache_ttl: float | None = None
    momentum_vol_z_score: float | None = None
    momentum_min_delta_pct: float | None = None
    momentum_vol_z_score_5m: float | None = None
    momentum_vol_z_score_15m: float | None = None
    momentum_vol_z_score_1h: float | None = None
    momentum_vol_z_score_4h: float | None = None
    momentum_vol_z_score_daily: float | None = None
    # Per-coin delta stop-loss overrides
    momentum_delta_sl_pct_btc: float | None = None
    momentum_delta_sl_pct_eth: float | None = None
    momentum_delta_sl_pct_bnb: float | None = None
    momentum_delta_sl_pct_xrp: float | None = None
    momentum_delta_sl_pct_sol: float | None = None
    momentum_delta_sl_pct_doge: float | None = None
    momentum_delta_sl_pct_hype: float | None = None
    # Per-coin minimum delta entry floor overrides
    momentum_min_delta_pct_btc: float | None = None
    momentum_min_delta_pct_eth: float | None = None
    momentum_min_delta_pct_bnb: float | None = None
    momentum_min_delta_pct_xrp: float | None = None
    momentum_min_delta_pct_sol: float | None = None
    momentum_min_delta_pct_doge: float | None = None
    momentum_min_delta_pct_hype: float | None = None
    # Per-bucket-type minimum delta entry floor overrides
    momentum_min_delta_pct_5m: float | None = None
    momentum_min_delta_pct_15m: float | None = None
    momentum_min_delta_pct_1h: float | None = None
    momentum_min_delta_pct_4h: float | None = None
    momentum_min_delta_pct_daily: float | None = None
    momentum_scan_interval: int | None = None
    momentum_max_concurrent: int | None = None
    momentum_min_gap_pct: float | None = None
    momentum_near_expiry_time_stop_secs: int | None = None
    monitor_interval: int | None = None
    # Phase B — resolution oracle near expiry
    momentum_use_resolution_oracle_near_expiry: bool | None = None
    # Phase C — per-type TTE floor (flattened)
    momentum_phase_c_min_tte_5m: int | None = None
    momentum_phase_c_min_tte_15m: int | None = None
    momentum_phase_c_min_tte_1h: int | None = None
    momentum_phase_c_min_tte_4h: int | None = None
    momentum_phase_c_min_tte_daily: int | None = None
    momentum_phase_c_min_tte_weekly: int | None = None
    momentum_phase_c_min_tte_milestone: int | None = None
    # Phase D — hedge
    momentum_hedge_enabled: bool | None = None
    momentum_hedge_price: float | None = None
    momentum_hedge_contracts_pct: float | None = None
    momentum_hedge_cancel_recovery_pct: float | None = None
    momentum_hedge_suppresses_delta_sl: bool | None = None
    momentum_hedge_price_5m: float | None = None
    momentum_hedge_price_15m: float | None = None
    momentum_hedge_price_1h: float | None = None
    momentum_hedge_price_4h: float | None = None
    momentum_hedge_price_daily: float | None = None
    momentum_hedge_price_weekly: float | None = None
    momentum_hedge_price_milestone: float | None = None
    # Per-bucket hedge on/off
    momentum_hedge_enabled_5m: bool | None = None
    momentum_hedge_enabled_15m: bool | None = None
    momentum_hedge_enabled_1h: bool | None = None
    momentum_hedge_enabled_4h: bool | None = None
    momentum_hedge_enabled_daily: bool | None = None
    momentum_hedge_enabled_weekly: bool | None = None
    momentum_hedge_enabled_milestone: bool | None = None
    # Logging toggles (post-trade analysis files)
    momentum_hedge_clob_log_enabled: bool | None = None
    momentum_ticks_log_enabled: bool | None = None
    # Phase E — empirical win-rate gate
    momentum_win_rate_gate_enabled: bool | None = None
    momentum_win_rate_gate_min_factor: float | None = None
    momentum_win_rate_gate_min_samples: int | None = None
    # Kelly extensions
    momentum_kelly_min_tte_seconds: int | None = None
    momentum_kelly_persistence_enabled: bool | None = None
    momentum_kelly_persistence_z_boost_max: float | None = None
    momentum_kelly_multiplier_5m: float | None = None
    momentum_kelly_multiplier_15m: float | None = None
    momentum_kelly_multiplier_1h: float | None = None
    momentum_kelly_multiplier_4h: float | None = None
    momentum_kelly_multiplier_daily: float | None = None
    momentum_kelly_multiplier_weekly: float | None = None
    # Range markets sub-strategy
    momentum_range_enabled: bool | None = None
    momentum_range_price_band_low: float | None = None
    momentum_range_price_band_high: float | None = None
    momentum_range_max_entry_usd: float | None = None
    momentum_range_vol_z_score: float | None = None
    momentum_range_min_tte_seconds: int | None = None
    # Item 2: Order cancel-and-retry
    momentum_order_cancel_sec: float | None = None
    momentum_slippage_cap: float | None = None
    momentum_max_retries: int | None = None
    momentum_buy_retry_step: float | None = None
    # Item 1: Active TP resting limit order
    momentum_tp_resting_enabled: bool | None = None
    momentum_tp_retry_max: int | None = None
    momentum_tp_retry_step: float | None = None
    # Item 4: VWAP/RoC secondary filter
    momentum_vwap_window_sec: int | None = None
    momentum_roc_window_sec: int | None = None
    momentum_min_vwap_dev_pct: float | None = None
    momentum_min_roc_pct: float | None = None
    # Item 7: Probability-based SL
    momentum_prob_sl_enabled: bool | None = None
    momentum_prob_sl_pct: float | None = None
    # Item 5: Chainlink watchdog
    chainlink_silence_watchdog_secs: int | None = None
    # RESOLVED fast-path fallback timeout
    momentum_resolved_force_close_sec: int | None = None
    # Strategy 5 — Opening Neutral
    opening_neutral_enabled: bool | None = None
    opening_neutral_dry_run: bool | None = None
    opening_neutral_tp_enabled: bool | None = None
    opening_neutral_tp_profit_pct: float | None = None


@app.get("/config")
def get_config() -> dict:
    """Return all mutable runtime config values."""
    return {
        "paper_trading":        config.PAPER_TRADING,
        "agent_auto":           config.AGENT_AUTO,
        "auto_approve":         config.AUTO_APPROVE,
        "mispricing_scan_interval": config.MISPRICING_SCAN_INTERVAL,
        "strategy_mispricing":  config.STRATEGY_MISPRICING_ENABLED,
        "strategy_maker":       config.STRATEGY_MAKER_ENABLED,
        "strategy_momentum":    config.STRATEGY_MOMENTUM_ENABLED,
        "fill_check_interval":  config.FILL_CHECK_INTERVAL,
        "paper_fill_probability": config.PAPER_FILL_PROBABILITY,
        "max_buy_no_yes_price": config.MAX_BUY_NO_YES_PRICE,
        "mispricing_market_cooldown_seconds": config.MISPRICING_MARKET_COOLDOWN_SECONDS,
        "momentum_market_cooldown_seconds":   config.MOMENTUM_MARKET_COOLDOWN_SECONDS,
        "min_strike_distance_pct": config.MIN_STRIKE_DISTANCE_PCT,
        "kalshi_enabled": config.KALSHI_ENABLED,
        "kalshi_require_nd2_confirmation": config.KALSHI_REQUIRE_ND2_CONFIRMATION,
        "kalshi_min_deviation": config.KALSHI_MIN_DEVIATION,
        "kalshi_match_max_strike_diff": config.KALSHI_MATCH_MAX_STRIKE_DIFF,
        "kalshi_match_max_expiry_days": config.KALSHI_MATCH_MAX_EXPIRY_DAYS,
        "max_concurrent_positions":   config.MAX_CONCURRENT_POSITIONS,
        # Market-making config
        "reprice_trigger_pct":    config.REPRICE_TRIGGER_PCT,
        "max_quote_age_seconds":  config.MAX_QUOTE_AGE_SECONDS,
        "maker_min_edge_pct":      config.MAKER_MIN_EDGE_PCT,
        "max_concurrent_maker_positions":      config.MAX_CONCURRENT_MAKER_POSITIONS,
        "max_concurrent_mispricing_positions": config.MAX_CONCURRENT_MISPRICING_POSITIONS,
        "paper_fill_prob_base":   config.PAPER_FILL_PROB_BASE,
        "paper_fill_prob_new_market": config.PAPER_FILL_PROB_NEW_MARKET,
        "paper_adverse_selection_pct": config.PAPER_ADVERSE_SELECTION_PCT,
        "paper_adverse_fill_multiplier": config.PAPER_ADVERSE_FILL_MULTIPLIER,
        "maker_coin_max_loss_usd": config.MAKER_COIN_MAX_LOSS_USD,
        "maker_exit_hours":       config.MAKER_EXIT_HOURS,
        "maker_exit_tte_frac":    config.MAKER_EXIT_TTE_FRAC,
        "maker_entry_tte_frac":   config.MAKER_ENTRY_TTE_FRAC,
        "maker_batch_size":       config.MAKER_BATCH_SIZE,
        "maker_positions_per_underlying": config.MAX_MAKER_POSITIONS_PER_UNDERLYING,
        "maker_quote_size_pct":    config.MAKER_SPREAD_SIZE_PCT,
        "maker_quote_size_min":    config.MAKER_SPREAD_SIZE_MIN,
        "maker_quote_size_max":    config.MAKER_SPREAD_SIZE_MAX,
        "maker_quote_size_new_market": config.MAKER_SPREAD_SIZE_NEW_MARKET,
        "hedge_threshold_usd":    config.HEDGE_THRESHOLD_USD,
        "hedge_rebalance_pct":    config.HEDGE_REBALANCE_PCT,
        "hedge_min_interval":     config.HEDGE_MIN_INTERVAL,
        "hedge_debounce_secs":    config.HEDGE_DEBOUNCE_SECS,
        "deployment_mode":        config.MAKER_DEPLOYMENT_MODE,
        "paper_capital_usd":      config.PAPER_CAPITAL_USD,
        # Quote guards & new-market logic
        "maker_min_quote_price":       config.MAKER_MIN_QUOTE_PRICE,
        "maker_min_volume_24hr":        config.MAKER_MIN_VOLUME_24HR,
        "maker_max_tte_days":          config.MAKER_MAX_TTE_DAYS,
        "new_market_age_limit":        config.NEW_MARKET_AGE_LIMIT,
        "new_market_wide_spread":      config.NEW_MARKET_WIDE_SPREAD,
        "new_market_pull_spread":      config.NEW_MARKET_PULL_SPREAD,
        # Inventory skew
        "inventory_skew_coeff":        config.INVENTORY_SKEW_COEFF,
        "inventory_skew_max":          config.INVENTORY_SKEW_MAX,
        # Hedge sizing
        "max_hl_notional":             config.MAX_HL_NOTIONAL,
        # Position monitor
        "profit_target_pct":           config.PROFIT_TARGET_PCT,
        "stop_loss_usd":               config.STOP_LOSS_USD,
        "exit_days_before_resolution": config.EXIT_DAYS_BEFORE_RESOLUTION,
        "min_hold_seconds":            config.MIN_HOLD_SECONDS,
        # Risk limits
        "max_pm_exposure_per_market":  config.MAX_PM_EXPOSURE_PER_MARKET,
        "max_total_pm_exposure":       config.MAX_TOTAL_PM_EXPOSURE,
        "hard_stop_drawdown":          config.HARD_STOP_DRAWDOWN,
        # Signal scoring
        "min_signal_score_mispricing": config.MIN_SIGNAL_SCORE_MISPRICING,
        "min_signal_score_maker":      config.MIN_SIGNAL_SCORE_MAKER,
        "maker_min_signal_score_5m":   config.MAKER_MIN_SIGNAL_SCORE_5M,
        "maker_min_signal_score_1h":   config.MAKER_MIN_SIGNAL_SCORE_1H,
        "maker_min_signal_score_4h":   config.MAKER_MIN_SIGNAL_SCORE_4H,
        "maker_exit_tte_frac_5m":      config.MAKER_EXIT_TTE_FRAC_5M,
        "score_weight_edge":           config.SCORE_WEIGHT_EDGE,
        "score_weight_source":         config.SCORE_WEIGHT_SOURCE,
        "score_weight_timing":         config.SCORE_WEIGHT_TIMING,
        "score_weight_liquidity":      config.SCORE_WEIGHT_LIQUIDITY,
        # Hedge control
        "maker_hedge_enabled":         config.MAKER_HEDGE_ENABLED,
        "maker_max_book_age_secs":     config.MAKER_MAX_BOOK_AGE_SECS,
        # Per-market imbalance skew
        "maker_imbalance_skew_coeff":  config.MAKER_IMBALANCE_SKEW_COEFF,
        "maker_imbalance_skew_max":    config.MAKER_IMBALANCE_SKEW_MAX,
        "maker_imbalance_skew_min_ct": config.MAKER_IMBALANCE_SKEW_MIN_CT,
        # Incentive spread gate & imbalance hard-stops
        "maker_min_incentive_spread":     config.MAKER_MIN_INCENTIVE_SPREAD,
        "maker_max_imbalance_contracts":  config.MAKER_MAX_IMBALANCE_CONTRACTS,
        "maker_naked_close_enabled":      config.MAKER_NAKED_CLOSE_ENABLED,
        "maker_naked_close_contracts":    config.MAKER_NAKED_CLOSE_CONTRACTS,
        "maker_naked_close_secs":         config.MAKER_NAKED_CLOSE_SECS,
        "maker_max_fills_per_leg":        config.MAKER_MAX_FILLS_PER_LEG,
        "maker_max_contracts_per_market": config.MAKER_MAX_CONTRACTS_PER_MARKET,
        # CLOB depth gate
        "maker_min_depth_to_quote":       config.MAKER_MIN_DEPTH_TO_QUOTE,
        "maker_depth_thin_threshold":     config.MAKER_DEPTH_THIN_THRESHOLD,
        "maker_depth_spread_factor_thin": config.MAKER_DEPTH_SPREAD_FACTOR_THIN,
        "maker_depth_spread_factor_zero": config.MAKER_DEPTH_SPREAD_FACTOR_ZERO,
        # Volatility & drift guards
        "maker_vol_filter_pct":           config.MAKER_VOL_FILTER_PCT,
        "maker_adverse_drift_reprice":    config.MAKER_ADVERSE_DRIFT_REPRICE,
        # Second-leg profit margin
        "maker_min_spread_profit_margin": config.MAKER_MIN_SPREAD_PROFIT_MARGIN,
        # Market type exclusion
        "maker_excluded_market_types": list(config.MAKER_EXCLUDED_MARKET_TYPES),
        # Strategy 3 — Momentum Scanner
        "momentum_price_band_low":        config.MOMENTUM_PRICE_BAND_LOW,
        "momentum_price_band_high":       config.MOMENTUM_PRICE_BAND_HIGH,
        "momentum_max_entry_usd":         config.MOMENTUM_MAX_ENTRY_USD,
        "momentum_kelly_fraction":         config.MOMENTUM_KELLY_FRACTION,
        "momentum_min_clob_depth":        config.MOMENTUM_MIN_CLOB_DEPTH,
        "momentum_order_type":            config.MOMENTUM_ORDER_TYPE,
        "momentum_delta_stop_loss_pct":    config.MOMENTUM_DELTA_STOP_LOSS_PCT,
        "momentum_take_profit":            config.MOMENTUM_TAKE_PROFIT,
        "momentum_min_tte_5m":             config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_5m",      config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_15m":            config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_15m",     config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_1h":             config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_1h",      config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_4h":             config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_4h",      config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_daily":          config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_daily",   config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_weekly":         config.MOMENTUM_MIN_TTE_SECONDS.get("bucket_weekly",  config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_milestone":      config.MOMENTUM_MIN_TTE_SECONDS.get("milestone",      config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT),
        "momentum_min_tte_default":        config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT,
        "momentum_spot_max_age_secs":     config.MOMENTUM_SPOT_MAX_AGE_SECS,
        "momentum_book_max_age_secs":     config.MOMENTUM_BOOK_MAX_AGE_SECS,
        "momentum_vol_cache_ttl":         config.MOMENTUM_VOL_CACHE_TTL,
        "momentum_vol_z_score":           config.MOMENTUM_VOL_Z_SCORE,
        "momentum_min_delta_pct":         config.MOMENTUM_MIN_DELTA_PCT,
        "momentum_vol_z_score_5m":         config.MOMENTUM_VOL_Z_SCORE_BY_TYPE.get("bucket_5m",    config.MOMENTUM_VOL_Z_SCORE),
        "momentum_vol_z_score_15m":        config.MOMENTUM_VOL_Z_SCORE_BY_TYPE.get("bucket_15m",   config.MOMENTUM_VOL_Z_SCORE),
        "momentum_vol_z_score_1h":         config.MOMENTUM_VOL_Z_SCORE_BY_TYPE.get("bucket_1h",    config.MOMENTUM_VOL_Z_SCORE),
        "momentum_vol_z_score_4h":         config.MOMENTUM_VOL_Z_SCORE_BY_TYPE.get("bucket_4h",    config.MOMENTUM_VOL_Z_SCORE),
        "momentum_vol_z_score_daily":      config.MOMENTUM_VOL_Z_SCORE_BY_TYPE.get("bucket_daily",  config.MOMENTUM_VOL_Z_SCORE),
        # Per-coin delta stop-loss overrides
        "momentum_delta_sl_pct_btc":   config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("BTC",  config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_eth":   config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("ETH",  config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_bnb":   config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("BNB",  config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_xrp":   config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("XRP",  config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_sol":   config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("SOL",  config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_doge":  config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("DOGE", config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        "momentum_delta_sl_pct_hype":  config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get("HYPE", config.MOMENTUM_DELTA_STOP_LOSS_PCT),
        # Per-coin minimum delta entry floor overrides
        "momentum_min_delta_pct_btc":  config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("BTC",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_eth":  config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("ETH",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_bnb":  config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("BNB",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_xrp":  config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("XRP",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_sol":  config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("SOL",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_doge": config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("DOGE", config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_hype": config.MOMENTUM_MIN_DELTA_PCT_BY_COIN.get("HYPE", config.MOMENTUM_MIN_DELTA_PCT),
        # Per-bucket-type minimum delta entry floor overrides
        "momentum_min_delta_pct_5m":    config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE.get("bucket_5m",    config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_15m":   config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE.get("bucket_15m",   config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_1h":    config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE.get("bucket_1h",    config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_4h":    config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE.get("bucket_4h",    config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_min_delta_pct_daily": config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE.get("bucket_daily",  config.MOMENTUM_MIN_DELTA_PCT),
        "momentum_scan_interval":         config.MOMENTUM_SCAN_INTERVAL,
        "momentum_max_concurrent":        config.MOMENTUM_MAX_CONCURRENT,
        "momentum_min_gap_pct":           config.MOMENTUM_MIN_GAP_PCT,
        "momentum_near_expiry_time_stop_secs": config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS,
        "monitor_interval":               config.MONITOR_INTERVAL,
        # Phase B — resolution oracle near expiry
        "momentum_use_resolution_oracle_near_expiry": config.MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY,
        # Phase C — per-type elapsed-time guard (flattened)
        "momentum_phase_c_min_tte_5m":        config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_5m",    0),
        "momentum_phase_c_min_tte_15m":       config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_15m",   0),
        "momentum_phase_c_min_tte_1h":        config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_1h",    0),
        "momentum_phase_c_min_tte_4h":        config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_4h",    0),
        "momentum_phase_c_min_tte_daily":     config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_daily", 0),
        "momentum_phase_c_min_tte_weekly":    config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("bucket_weekly",0),
        "momentum_phase_c_min_tte_milestone": config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS.get("milestone",    0),
        # Phase D — hedge
        "momentum_hedge_enabled":              config.MOMENTUM_HEDGE_ENABLED,
        "momentum_hedge_price":                config.MOMENTUM_HEDGE_PRICE,
        "momentum_hedge_contracts_pct":        config.MOMENTUM_HEDGE_CONTRACTS_PCT,
        "momentum_hedge_cancel_recovery_pct": config.MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT,
        "momentum_hedge_suppresses_delta_sl": config.MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL,
        # Logging toggles (post-trade analysis files)
        "momentum_hedge_clob_log_enabled":     config.MOMENTUM_HEDGE_CLOB_LOG_ENABLED,
        "momentum_ticks_log_enabled":          config.MOMENTUM_TICKS_LOG_ENABLED,
        "momentum_hedge_price_5m":        config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_5m",    config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_15m":       config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_15m",   config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_1h":        config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_1h",    config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_4h":        config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_4h",    config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_daily":     config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_daily",  config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_weekly":    config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("bucket_weekly", config.MOMENTUM_HEDGE_PRICE),
        "momentum_hedge_price_milestone": config.MOMENTUM_HEDGE_PRICE_BY_TYPE.get("milestone",     config.MOMENTUM_HEDGE_PRICE),
        # Per-bucket hedge on/off
        "momentum_hedge_enabled_5m":        config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_5m",    False),
        "momentum_hedge_enabled_15m":       config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_15m",   False),
        "momentum_hedge_enabled_1h":        config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_1h",    True),
        "momentum_hedge_enabled_4h":        config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_4h",    True),
        "momentum_hedge_enabled_daily":     config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_daily",  True),
        "momentum_hedge_enabled_weekly":    config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("bucket_weekly", True),
        "momentum_hedge_enabled_milestone": config.MOMENTUM_HEDGE_ENABLED_BY_TYPE.get("milestone",     True),
        # Phase E — empirical win-rate gate
        "momentum_win_rate_gate_enabled":     config.MOMENTUM_WIN_RATE_GATE_ENABLED,
        "momentum_win_rate_gate_min_factor":  config.MOMENTUM_WIN_RATE_GATE_MIN_FACTOR,
        "momentum_win_rate_gate_min_samples": config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES,
        # Kelly extensions
        "momentum_kelly_min_tte_seconds":         config.MOMENTUM_KELLY_MIN_TTE_SECONDS,
        "momentum_kelly_persistence_enabled":    config.MOMENTUM_KELLY_PERSISTENCE_ENABLED,
        "momentum_kelly_persistence_z_boost_max": config.MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX,
        "momentum_kelly_multiplier_5m":           config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_5m",    1.0),
        "momentum_kelly_multiplier_15m":          config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_15m",   1.0),
        "momentum_kelly_multiplier_1h":           config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_1h",    1.0),
        "momentum_kelly_multiplier_4h":           config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_4h",    1.0),
        "momentum_kelly_multiplier_daily":        config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_daily", 1.0),
        "momentum_kelly_multiplier_weekly":       config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE.get("bucket_weekly",1.0),
        # Range markets sub-strategy
        "momentum_range_enabled":              config.MOMENTUM_RANGE_ENABLED,
        "momentum_range_price_band_low":       config.MOMENTUM_RANGE_PRICE_BAND_LOW,
        "momentum_range_price_band_high":      config.MOMENTUM_RANGE_PRICE_BAND_HIGH,
        "momentum_range_max_entry_usd":        config.MOMENTUM_RANGE_MAX_ENTRY_USD,
        "momentum_range_vol_z_score":          config.MOMENTUM_RANGE_VOL_Z_SCORE,
        "momentum_range_min_tte_seconds":      config.MOMENTUM_RANGE_MIN_TTE_SECONDS,
        # Item 2: Order cancel-and-retry
        "momentum_order_cancel_sec":           config.MOMENTUM_ORDER_CANCEL_SEC,
        "momentum_slippage_cap":               config.MOMENTUM_SLIPPAGE_CAP,
        "momentum_max_retries":                config.MOMENTUM_MAX_RETRIES,
        "momentum_buy_retry_step":             config.MOMENTUM_BUY_RETRY_STEP,
        # Item 1: Active TP resting limit order
        "momentum_tp_resting_enabled":         config.MOMENTUM_TP_RESTING_ENABLED,
        "momentum_tp_retry_max":               config.MOMENTUM_TP_RETRY_MAX,
        "momentum_tp_retry_step":              config.MOMENTUM_TP_RETRY_STEP,
        # Item 4: VWAP/RoC secondary filter
        "momentum_vwap_window_sec":            config.MOMENTUM_VWAP_WINDOW_SEC,
        "momentum_roc_window_sec":             config.MOMENTUM_ROC_WINDOW_SEC,
        "momentum_min_vwap_dev_pct":           config.MOMENTUM_MIN_VWAP_DEV_PCT,
        "momentum_min_roc_pct":                config.MOMENTUM_MIN_ROC_PCT,
        # Item 7: Probability-based SL
        "momentum_prob_sl_enabled":            config.MOMENTUM_PROB_SL_ENABLED,
        "momentum_prob_sl_pct":                config.MOMENTUM_PROB_SL_PCT,
        # Item 5: Chainlink watchdog
        "chainlink_silence_watchdog_secs":     config.CHAINLINK_SILENCE_WATCHDOG_SECS,
        # RESOLVED fast-path fallback timeout
        "momentum_resolved_force_close_sec":   config.MOMENTUM_RESOLVED_FORCE_CLOSE_SEC,
        # Strategy 5 — Opening Neutral
        "opening_neutral_enabled":             config.OPENING_NEUTRAL_ENABLED,
        "opening_neutral_dry_run":             config.OPENING_NEUTRAL_DRY_RUN,
        "opening_neutral_tp_enabled":          config.OPENING_NEUTRAL_TP_ENABLED,
        "opening_neutral_tp_profit_pct":       config.OPENING_NEUTRAL_TP_PROFIT_PCT,
        "timestamp":            time.time(),
    }


@app.post("/config", dependencies=[Depends(require_auth)])
def patch_config(patch: ConfigPatch) -> dict:
    """
    Update one or more mutable config values at runtime.
    Changes take effect immediately (module-level variables are mutated).
    """
    updated = {}
    for field, (attr, typ) in _MUTABLE_CONFIG.items():
        val = getattr(patch, field)
        if val is not None:
            coerced = typ(val)
            setattr(config, attr, coerced)
            updated[field] = coerced
            log.info("Config updated via API", key=attr, value=coerced)
    # List-type config values handled separately (list[str] incompatible with generic coerce)
    if patch.maker_excluded_market_types is not None:
        config.MAKER_EXCLUDED_MARKET_TYPES = list(patch.maker_excluded_market_types)
        updated["maker_excluded_market_types"] = config.MAKER_EXCLUDED_MARKET_TYPES
        log.info("Config updated via API", key="MAKER_EXCLUDED_MARKET_TYPES",
                 value=config.MAKER_EXCLUDED_MARKET_TYPES)
    # Per-type momentum Min TTE — written directly into the dict
    _tte_map = {
        "momentum_min_tte_5m":        "bucket_5m",
        "momentum_min_tte_15m":       "bucket_15m",
        "momentum_min_tte_1h":        "bucket_1h",
        "momentum_min_tte_4h":        "bucket_4h",
        "momentum_min_tte_daily":     "bucket_daily",
        "momentum_min_tte_weekly":    "bucket_weekly",
        "momentum_min_tte_milestone": "milestone",
    }
    for field, bucket_key in _tte_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_MIN_TTE_SECONDS[bucket_key] = int(v)
            updated[field] = int(v)
            log.info("Config updated via API", key=f"MOMENTUM_MIN_TTE_SECONDS[{bucket_key}]", value=int(v))
    # Per-type momentum vol z-score — written directly into the dict
    _z_score_map = {
        "momentum_vol_z_score_5m":    "bucket_5m",
        "momentum_vol_z_score_15m":   "bucket_15m",
        "momentum_vol_z_score_1h":    "bucket_1h",
        "momentum_vol_z_score_4h":    "bucket_4h",
        "momentum_vol_z_score_daily": "bucket_daily",
    }
    for field, bucket_key in _z_score_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_VOL_Z_SCORE_BY_TYPE[bucket_key] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_VOL_Z_SCORE_BY_TYPE[{bucket_key}]", value=float(v))
    # Phase C — per-type TTE floor — written directly into the dict
    _phase_c_map = {
        "momentum_phase_c_min_tte_5m":        "bucket_5m",
        "momentum_phase_c_min_tte_15m":       "bucket_15m",
        "momentum_phase_c_min_tte_1h":        "bucket_1h",
        "momentum_phase_c_min_tte_4h":        "bucket_4h",
        "momentum_phase_c_min_tte_daily":     "bucket_daily",
        "momentum_phase_c_min_tte_weekly":    "bucket_weekly",
        "momentum_phase_c_min_tte_milestone": "milestone",
    }
    for field, bucket_key in _phase_c_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_PHASE_C_MIN_TTE_SECONDS[bucket_key] = int(v)
            updated[field] = int(v)
            log.info("Config updated via API", key=f"MOMENTUM_PHASE_C_MIN_TTE_SECONDS[{bucket_key}]", value=int(v))
    # Per-bucket Kelly multiplier overrides — written directly into the dict
    _kelly_mult_map = {
        "momentum_kelly_multiplier_5m":      "bucket_5m",
        "momentum_kelly_multiplier_15m":     "bucket_15m",
        "momentum_kelly_multiplier_1h":      "bucket_1h",
        "momentum_kelly_multiplier_4h":      "bucket_4h",
        "momentum_kelly_multiplier_daily":   "bucket_daily",
        "momentum_kelly_multiplier_weekly":  "bucket_weekly",
    }
    for field, bucket_key in _kelly_mult_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_KELLY_MULTIPLIER_BY_TYPE[bucket_key] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_KELLY_MULTIPLIER_BY_TYPE[{bucket_key}]", value=float(v))
    # Phase D — per-bucket hedge price overrides
    _hedge_price_map = {        "momentum_hedge_price_5m":       "bucket_5m",
        "momentum_hedge_price_15m":      "bucket_15m",
        "momentum_hedge_price_1h":       "bucket_1h",
        "momentum_hedge_price_4h":       "bucket_4h",
        "momentum_hedge_price_daily":    "bucket_daily",
        "momentum_hedge_price_weekly":   "bucket_weekly",
        "momentum_hedge_price_milestone":"milestone",
    }
    for field, bucket_key in _hedge_price_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_HEDGE_PRICE_BY_TYPE[bucket_key] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_HEDGE_PRICE_BY_TYPE[{bucket_key}]", value=float(v))
    # Phase D — per-bucket hedge enabled overrides
    _hedge_enabled_map = {
        "momentum_hedge_enabled_5m":        "bucket_5m",
        "momentum_hedge_enabled_15m":       "bucket_15m",
        "momentum_hedge_enabled_1h":        "bucket_1h",
        "momentum_hedge_enabled_4h":        "bucket_4h",
        "momentum_hedge_enabled_daily":     "bucket_daily",
        "momentum_hedge_enabled_weekly":    "bucket_weekly",
        "momentum_hedge_enabled_milestone": "milestone",
    }
    for field, bucket_key in _hedge_enabled_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_HEDGE_ENABLED_BY_TYPE[bucket_key] = bool(v)
            updated[field] = bool(v)
            log.info("Config updated via API", key=f"MOMENTUM_HEDGE_ENABLED_BY_TYPE[{bucket_key}]", value=bool(v))
    # Per-coin delta stop-loss overrides — written directly into the dict
    _delta_sl_coin_map = {
        "momentum_delta_sl_pct_btc":  "BTC",
        "momentum_delta_sl_pct_eth":  "ETH",
        "momentum_delta_sl_pct_bnb":  "BNB",
        "momentum_delta_sl_pct_xrp":  "XRP",
        "momentum_delta_sl_pct_sol":  "SOL",
        "momentum_delta_sl_pct_doge": "DOGE",
        "momentum_delta_sl_pct_hype": "HYPE",
    }
    for field, coin in _delta_sl_coin_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_DELTA_SL_PCT_BY_COIN[coin] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_DELTA_SL_PCT_BY_COIN[{coin}]", value=float(v))
    # Per-coin minimum delta entry floor overrides — written directly into the dict
    _min_delta_coin_map = {
        "momentum_min_delta_pct_btc":  "BTC",
        "momentum_min_delta_pct_eth":  "ETH",
        "momentum_min_delta_pct_bnb":  "BNB",
        "momentum_min_delta_pct_xrp":  "XRP",
        "momentum_min_delta_pct_sol":  "SOL",
        "momentum_min_delta_pct_doge": "DOGE",
        "momentum_min_delta_pct_hype": "HYPE",
    }
    for field, coin in _min_delta_coin_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_MIN_DELTA_PCT_BY_COIN[coin] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_MIN_DELTA_PCT_BY_COIN[{coin}]", value=float(v))
    # Per-bucket-type minimum delta entry floor overrides — written directly into the dict
    _min_delta_type_map = {
        "momentum_min_delta_pct_5m":    "bucket_5m",
        "momentum_min_delta_pct_15m":   "bucket_15m",
        "momentum_min_delta_pct_1h":    "bucket_1h",
        "momentum_min_delta_pct_4h":    "bucket_4h",
        "momentum_min_delta_pct_daily": "bucket_daily",
    }
    for field, bucket_key in _min_delta_type_map.items():
        v = getattr(patch, field)
        if v is not None:
            config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE[bucket_key] = float(v)
            updated[field] = float(v)
            log.info("Config updated via API", key=f"MOMENTUM_MIN_DELTA_PCT_BY_TYPE[{bucket_key}]", value=float(v))
    if updated:
        # Build attr-name → value dict for only the keys that changed so that
        # _save_overrides does a targeted merge (not a full overwrite).
        attr_changes: dict = {
            attr: getattr(config, attr)
            for field, (attr, _) in _MUTABLE_CONFIG.items()
            if field in updated
        }
        if "maker_excluded_market_types" in updated:
            attr_changes["MAKER_EXCLUDED_MARKET_TYPES"] = list(config.MAKER_EXCLUDED_MARKET_TYPES)
        # Persist the whole per-type dict whenever any entry changed
        if any(f in updated for f in _tte_map) or "momentum_min_tte_default" in updated:
            attr_changes["MOMENTUM_MIN_TTE_SECONDS"] = dict(config.MOMENTUM_MIN_TTE_SECONDS)
            attr_changes["MOMENTUM_MIN_TTE_SECONDS_DEFAULT"] = config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
        if any(f in updated for f in _z_score_map):
            attr_changes["MOMENTUM_VOL_Z_SCORE_BY_TYPE"] = dict(config.MOMENTUM_VOL_Z_SCORE_BY_TYPE)
        if any(f in updated for f in _delta_sl_coin_map):
            attr_changes["MOMENTUM_DELTA_SL_PCT_BY_COIN"] = dict(config.MOMENTUM_DELTA_SL_PCT_BY_COIN)
        if any(f in updated for f in _min_delta_coin_map):
            attr_changes["MOMENTUM_MIN_DELTA_PCT_BY_COIN"] = dict(config.MOMENTUM_MIN_DELTA_PCT_BY_COIN)
        if any(f in updated for f in _min_delta_type_map):
            attr_changes["MOMENTUM_MIN_DELTA_PCT_BY_TYPE"] = dict(config.MOMENTUM_MIN_DELTA_PCT_BY_TYPE)
        if any(f in updated for f in _hedge_price_map):
            attr_changes["MOMENTUM_HEDGE_PRICE_BY_TYPE"] = dict(config.MOMENTUM_HEDGE_PRICE_BY_TYPE)
        if any(f in updated for f in _hedge_enabled_map):
            attr_changes["MOMENTUM_HEDGE_ENABLED_BY_TYPE"] = dict(config.MOMENTUM_HEDGE_ENABLED_BY_TYPE)
        _save_overrides(attr_changes)
    return {
        "updated": updated,
        "current": {
            "paper_trading":       config.PAPER_TRADING,
            "agent_auto":          config.AGENT_AUTO,
            "auto_approve":        config.AUTO_APPROVE,
            "mispricing_scan_interval": config.MISPRICING_SCAN_INTERVAL,
            "strategy_mispricing": config.STRATEGY_MISPRICING_ENABLED,
            "strategy_maker":      config.STRATEGY_MAKER_ENABLED,
            "fill_check_interval": config.FILL_CHECK_INTERVAL,
            "paper_fill_probability": config.PAPER_FILL_PROBABILITY,
            "max_buy_no_yes_price": config.MAX_BUY_NO_YES_PRICE,
            "mispricing_market_cooldown_seconds": config.MISPRICING_MARKET_COOLDOWN_SECONDS,
            "momentum_market_cooldown_seconds":   config.MOMENTUM_MARKET_COOLDOWN_SECONDS,
            "min_strike_distance_pct": config.MIN_STRIKE_DISTANCE_PCT,
            "kalshi_enabled": config.KALSHI_ENABLED,
            "kalshi_require_nd2_confirmation": config.KALSHI_REQUIRE_ND2_CONFIRMATION,
            "kalshi_min_deviation": config.KALSHI_MIN_DEVIATION,
            "kalshi_match_max_strike_diff": config.KALSHI_MATCH_MAX_STRIKE_DIFF,
            "kalshi_match_max_expiry_days": config.KALSHI_MATCH_MAX_EXPIRY_DAYS,
            "max_concurrent_positions":   config.MAX_CONCURRENT_POSITIONS,
            # Market-making config
            "reprice_trigger_pct":    config.REPRICE_TRIGGER_PCT,
            "max_quote_age_seconds":  config.MAX_QUOTE_AGE_SECONDS,
            "maker_min_edge_pct":      config.MAKER_MIN_EDGE_PCT,
            "max_concurrent_maker_positions":      config.MAX_CONCURRENT_MAKER_POSITIONS,
            "max_concurrent_mispricing_positions": config.MAX_CONCURRENT_MISPRICING_POSITIONS,
            "paper_fill_prob_base":   config.PAPER_FILL_PROB_BASE,
            "paper_fill_prob_new_market": config.PAPER_FILL_PROB_NEW_MARKET,
            "paper_adverse_selection_pct": config.PAPER_ADVERSE_SELECTION_PCT,
            "paper_adverse_fill_multiplier": config.PAPER_ADVERSE_FILL_MULTIPLIER,
            "maker_coin_max_loss_usd": config.MAKER_COIN_MAX_LOSS_USD,
            "maker_exit_hours":       config.MAKER_EXIT_HOURS,
            "maker_exit_tte_frac":    config.MAKER_EXIT_TTE_FRAC,
            "maker_entry_tte_frac":   config.MAKER_ENTRY_TTE_FRAC,
            "maker_batch_size":       config.MAKER_BATCH_SIZE,
            "maker_positions_per_underlying": config.MAX_MAKER_POSITIONS_PER_UNDERLYING,
            "maker_quote_size_pct":    config.MAKER_SPREAD_SIZE_PCT,
            "maker_quote_size_min":    config.MAKER_SPREAD_SIZE_MIN,
            "maker_quote_size_max":    config.MAKER_SPREAD_SIZE_MAX,
            "maker_quote_size_new_market": config.MAKER_SPREAD_SIZE_NEW_MARKET,
            "hedge_threshold_usd":    config.HEDGE_THRESHOLD_USD,
            "hedge_rebalance_pct":    config.HEDGE_REBALANCE_PCT,
            "hedge_min_interval":     config.HEDGE_MIN_INTERVAL,
            "hedge_debounce_secs":    config.HEDGE_DEBOUNCE_SECS,
            "deployment_mode":        config.MAKER_DEPLOYMENT_MODE,
            "paper_capital_usd":      config.PAPER_CAPITAL_USD,
            # Quote guards & new-market logic
            "maker_min_quote_price":       config.MAKER_MIN_QUOTE_PRICE,
            "maker_min_volume_24hr":        config.MAKER_MIN_VOLUME_24HR,
            "maker_max_tte_days":          config.MAKER_MAX_TTE_DAYS,
            "new_market_age_limit":        config.NEW_MARKET_AGE_LIMIT,
            "new_market_wide_spread":      config.NEW_MARKET_WIDE_SPREAD,
            "new_market_pull_spread":      config.NEW_MARKET_PULL_SPREAD,
            # Inventory skew
            "inventory_skew_coeff":        config.INVENTORY_SKEW_COEFF,
            "inventory_skew_max":          config.INVENTORY_SKEW_MAX,
            # Hedge sizing
            "max_hl_notional":             config.MAX_HL_NOTIONAL,
            # Position monitor
            "profit_target_pct":           config.PROFIT_TARGET_PCT,
            "stop_loss_usd":               config.STOP_LOSS_USD,
            "exit_days_before_resolution": config.EXIT_DAYS_BEFORE_RESOLUTION,
            "min_hold_seconds":            config.MIN_HOLD_SECONDS,
            # Risk limits
            "max_pm_exposure_per_market":  config.MAX_PM_EXPOSURE_PER_MARKET,
            "max_total_pm_exposure":       config.MAX_TOTAL_PM_EXPOSURE,
            "hard_stop_drawdown":          config.HARD_STOP_DRAWDOWN,
            # Signal scoring
            "min_signal_score_mispricing": config.MIN_SIGNAL_SCORE_MISPRICING,
            "min_signal_score_maker":      config.MIN_SIGNAL_SCORE_MAKER,
            "maker_min_signal_score_5m":   config.MAKER_MIN_SIGNAL_SCORE_5M,
            "maker_min_signal_score_1h":   config.MAKER_MIN_SIGNAL_SCORE_1H,
            "maker_min_signal_score_4h":   config.MAKER_MIN_SIGNAL_SCORE_4H,
            "maker_exit_tte_frac_5m":      config.MAKER_EXIT_TTE_FRAC_5M,
            "score_weight_edge":           config.SCORE_WEIGHT_EDGE,
            "score_weight_source":         config.SCORE_WEIGHT_SOURCE,
            "score_weight_timing":         config.SCORE_WEIGHT_TIMING,
            "score_weight_liquidity":      config.SCORE_WEIGHT_LIQUIDITY,
            # Hedge control
            "maker_hedge_enabled":         config.MAKER_HEDGE_ENABLED,
            "maker_max_book_age_secs":     config.MAKER_MAX_BOOK_AGE_SECS,
            # Per-market imbalance skew
            "maker_imbalance_skew_coeff":  config.MAKER_IMBALANCE_SKEW_COEFF,
            "maker_imbalance_skew_max":    config.MAKER_IMBALANCE_SKEW_MAX,
            "maker_imbalance_skew_min_ct": config.MAKER_IMBALANCE_SKEW_MIN_CT,
            # Incentive spread gate & imbalance hard-stops
            "maker_min_incentive_spread":     config.MAKER_MIN_INCENTIVE_SPREAD,
            "maker_max_imbalance_contracts":  config.MAKER_MAX_IMBALANCE_CONTRACTS,
            "maker_naked_close_contracts":    config.MAKER_NAKED_CLOSE_CONTRACTS,
            "maker_naked_close_secs":         config.MAKER_NAKED_CLOSE_SECS,
            "maker_max_fills_per_leg":        config.MAKER_MAX_FILLS_PER_LEG,
            "maker_max_contracts_per_market": config.MAKER_MAX_CONTRACTS_PER_MARKET,
            # CLOB depth gate
            "maker_min_depth_to_quote":       config.MAKER_MIN_DEPTH_TO_QUOTE,
            "maker_depth_thin_threshold":     config.MAKER_DEPTH_THIN_THRESHOLD,
            "maker_depth_spread_factor_thin": config.MAKER_DEPTH_SPREAD_FACTOR_THIN,
            "maker_depth_spread_factor_zero": config.MAKER_DEPTH_SPREAD_FACTOR_ZERO,
            # Volatility & drift guards
            "maker_vol_filter_pct":           config.MAKER_VOL_FILTER_PCT,
            "maker_adverse_drift_reprice":    config.MAKER_ADVERSE_DRIFT_REPRICE,
            # Second-leg profit margin
            "maker_min_spread_profit_margin": config.MAKER_MIN_SPREAD_PROFIT_MARGIN,
            # Market type exclusion
            "maker_excluded_market_types": list(config.MAKER_EXCLUDED_MARKET_TYPES),
            # Range markets sub-strategy
            "momentum_range_enabled":              config.MOMENTUM_RANGE_ENABLED,
            "momentum_range_price_band_low":       config.MOMENTUM_RANGE_PRICE_BAND_LOW,
            "momentum_range_price_band_high":      config.MOMENTUM_RANGE_PRICE_BAND_HIGH,
            "momentum_range_max_entry_usd":        config.MOMENTUM_RANGE_MAX_ENTRY_USD,
            "momentum_range_vol_z_score":          config.MOMENTUM_RANGE_VOL_Z_SCORE,
            "momentum_range_min_tte_seconds":      config.MOMENTUM_RANGE_MIN_TTE_SECONDS,
        },
        "timestamp": time.time(),
    }


@app.get("/config/effective")
def get_effective_config_endpoint() -> dict:
    """Return the fully-merged runtime config (defaults + overrides, post-startup patches).

    This is the single source of truth for what values the bot is actually using.
    Unlike GET /config which only shows mutable UI-facing keys, this returns every
    named constant in config.py with its current in-memory value.
    """
    from config import get_effective_config
    return {"effective_config": get_effective_config(), "timestamp": time.time()}


@app.post("/config/reload", dependencies=[Depends(require_auth)])
def reload_config() -> dict:
    """Re-read config_overrides.json and apply all values to the live config.

    Use this after manually editing the JSON file while the bot is running,
    so the changes take effect without a full restart.
    """
    if not _OVERRIDES_FILE.exists():
        return {"reloaded": 0, "message": "No overrides file found"}
    try:
        on_disk: dict = json.loads(_OVERRIDES_FILE.read_text())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read overrides file: {exc}")

    _module = sys.modules[config.__name__]
    applied: dict = {}
    for k, v in on_disk.items():
        if hasattr(_module, k):
            current = getattr(_module, k)
            try:
                if isinstance(current, dict) and isinstance(v, dict):
                    # Dict config values (e.g. MOMENTUM_MIN_TTE_SECONDS): merge
                    # the saved dict into the in-memory one so any keys present
                    # in config.py defaults but absent from the file are kept.
                    merged = {**current, **v}
                    setattr(_module, k, merged)
                    applied[k] = merged
                elif isinstance(v, list):
                    setattr(_module, k, v)
                    applied[k] = v
                else:
                    coerced = type(current)(v)
                    setattr(_module, k, coerced)
                    applied[k] = coerced
            except Exception:
                pass  # Skip values that can't be coerced

    log.info("Config reloaded from file", n_keys=len(applied), keys=list(applied.keys()))
    return {"reloaded": len(applied), "applied": applied}


# ── Bot start / stop ────────────────────────────────────────────────────

class BotActivePatch(BaseModel):
    active: bool


@app.get("/bot")
def get_bot_status() -> dict:
    """Return current bot active/paused state."""
    return {
        "active": config.BOT_ACTIVE,
        "timestamp": time.time(),
    }


@app.post("/bot", dependencies=[Depends(require_auth)])
def set_bot_status(patch: BotActivePatch) -> dict:
    """
    Start or stop the bot.
    When active=False: scanner and agent loop idle; existing positions still monitored.
    When active=True: normal operation resumes immediately.
    """
    config.BOT_ACTIVE = patch.active
    state.bot_active = patch.active
    log.info("Bot toggled via API", active=patch.active)
    _save_overrides({"BOT_ACTIVE": patch.active})
    return {
        "active": config.BOT_ACTIVE,
        "timestamp": time.time(),
    }


# ── Server-Sent Events stream ─────────────────────────────────────────────────

@app.get("/events")
async def events_stream() -> StreamingResponse:
    """Server-Sent Events stream — pushes live bot state to subscribing frontends.

    Clients connect once; the backend pushes a full state bundle whenever
    positions, markets, signals, or other live data changes (≤1 push/second,
    governed by state_sync_loop).  Keepalives are sent every 30 s to prevent
    proxy or load-balancer timeouts.

    Message format:  ``data: <json>\\n\\n``
    Each JSON object mirrors the merged response of individual REST endpoints,
    keyed by endpoint name (health, positions, risk, markets, …).
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _sse_clients.append(q)

    async def generate():
        try:
            # Immediate keepalive confirms the connection to the client
            yield ": keepalive\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Periodic keepalive prevents proxy/LB timeouts on idle streams
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",          # disable nginx buffering
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
        },
    )


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Bot uptime, WS connection status, last heartbeat timestamp."""
    uptime_s = time.time() - state.started_at
    dq = state.data_quality
    # Derive a top-level warning flag so the UI can highlight issues quickly.
    # Note: stale books are expected for illiquid/inactive subscribed markets and
    # are NOT flagged here — only real data gaps (rejected subs, zero-book markets).
    data_issues = (
        dq.get("sub_rejected_count", 0) > 0
        or dq.get("no_book_count", 0) > 5
    )
    try:
        from fill_simulator import get_fill_session_stats
        fill_stats = get_fill_session_stats()
    except Exception:
        fill_stats = {"adverse_triggers_session": 0, "hl_max_move_pct_session": 0.0}

    return {
        "status": "running",
        "uptime_seconds": round(uptime_s, 1),
        "pm_ws_connected": state.pm_ws_connected,
        "hl_ws_connected": state.hl_ws_connected,
        "last_heartbeat_ts": state.last_heartbeat_ts,
        "last_heartbeat_age_s": round(time.time() - state.last_heartbeat_ts, 1)
        if state.last_heartbeat_ts > 0 else None,
        "paper_trading": state.paper_trading,
        "agent_auto": config.AGENT_AUTO,
        "bot_active": config.BOT_ACTIVE,
        "data_quality": dq,
        "data_issues": data_issues,
        "adverse_triggers_session": fill_stats["adverse_triggers_session"],
        "adverse_threshold_pct": config.PAPER_ADVERSE_SELECTION_PCT,
        "hl_max_move_pct_session": fill_stats["hl_max_move_pct_session"],
        "timestamp": time.time(),
    }


# ── Positions ─────────────────────────────────────────────────────────────────

@app.get("/positions")
def positions() -> dict:
    """All open PM + HL positions with unrealized P&L estimates."""
    return {
        "positions": list(state.positions.values()),
        "count": len(state.positions),
        "timestamp": time.time(),
    }


@app.get("/positions/live")
async def positions_live() -> dict:
    """
    Fetch positions directly from the Polymarket Data API (source of truth).

    Returns the raw PM wallet snapshot and a reconciliation diff:
      - 'wallet_positions': everything PM reports in the funder wallet
      - 'bot_positions': what the bot's risk engine currently tracks (open only)
      - 'discrepancies': tokens present in PM wallet but absent from the bot state,
         or tokens where size differs by > 0.01 contracts
      - 'pending_redemption': resolved tokens still sitting in the wallet
        (curPrice = 0 or 1 but token is still listed — PM hasn't auto-distributed yet,
         or these are winning tokens that need manual CTF redemption)
    """
    pm = state.pm_ref
    if pm is None:
        raise HTTPException(status_code=503, detail="PM client not yet initialised")

    # ALWAYS READ OFFICIAL API SPECS — Polymarket Data API: https://docs.polymarket.com/#data-api
    # Response fields: asset, size, avgPrice, currentPrice, redeemable, outcome, conditionId, title
    raw = await pm.get_live_positions()

    # Enrich each position with market info where possible
    markets_snap = pm.get_markets()
    markets_by_token: dict[str, dict] = {}
    for mkt in markets_snap.values():
        markets_by_token[mkt.token_id_yes] = {"market_id": mkt.condition_id, "title": mkt.title, "side": "YES", "end_date": mkt.end_date.isoformat() if mkt.end_date else None}
        markets_by_token[mkt.token_id_no]  = {"market_id": mkt.condition_id, "title": mkt.title, "side": "NO",  "end_date": mkt.end_date.isoformat() if mkt.end_date else None}

    wallet_positions = []
    pending_redemption = []   # redeemable=True from data API → can claim on-chain NOW
    awaiting_settlement = []  # market ended + price settled, but on-chain CTF resolution still pending
    now_ts = time.time()

    for pos in raw:
        token_id = pos.get("asset") or pos.get("asset_id") or ""
        size = float(pos.get("size", 0) or 0)
        avg_price = float(pos.get("avgPrice") or pos.get("avg_price") or 0)
        cur_price = float(pos.get("currentPrice") or pos.get("curPrice") or pos.get("cur_price") or 0)
        # redeemable is set by the Polymarket data API when the CTF condition has been
        # resolved on-chain and tokens can actually be redeemed via redeemPositions().
        # This is DIFFERENT from the market merely being closed/ended.
        redeemable: bool = bool(pos.get("redeemable", False))
        outcome = pos.get("outcome", "")
        title = pos.get("title") or pos.get("market", "")
        condition_id = pos.get("conditionId") or pos.get("condition_id") or ""
        mkt_info = markets_by_token.get(token_id, {})

        # Derive canonical YES/NO side from PM outcome label.
        # PM Data API returns: 'Yes'/'No' for standard binary markets;
        # 'Up'/'Down' for directional bucket markets.
        # 'Up' = YES side (above strike wins); 'Down' = NO side.
        # This derivation is AUTHORITATIVE — it must never depend on the
        # market snapshot being present, because resolved markets are pruned.
        outcome_lower = (outcome or "").strip().lower()
        side_canonical = "YES" if outcome_lower in ("yes", "up") else "NO"

        enriched = {
            "token_id": token_id,
            "size": round(size, 4),
            "avg_price": round(avg_price, 4),
            "cur_price": round(cur_price, 4),
            "redeemable": redeemable,
            "outcome": outcome,
            "title": title or mkt_info.get("title", ""),
            "condition_id": condition_id or mkt_info.get("market_id", ""),
            # side_guess: always YES/NO — used by webapp's 'Side' column.
            # Derived from the outcome label so it works even when the market
            # is resolved and pruned from the local snapshot cache.
            "side_guess": mkt_info.get("side", "") or side_canonical,
            # side_canonical stored explicitly for cross-reference logic below.
            "side_canonical": side_canonical,
            "end_date": mkt_info.get("end_date"),
            "in_bot_state": False,  # filled in below
            "source": "pm_wallet",  # PM wallet is the authoritative source of truth (C5)
        }
        wallet_positions.append(enriched)

        if size > 0 and (cur_price < 0.01 or cur_price > 0.99):
            payout = round(size * cur_price, 4)
            enriched["payout_usd"] = payout
            entry = {**enriched, "payout_usd": payout, "won": cur_price > 0.99}
            if redeemable:
                # CTF resolved on-chain — tokens can be redeemed right now
                pending_redemption.append(entry)
            else:
                # Only flag as awaiting settlement when the market has actually ended.
                # A token can trade near 0 or 1 on a LIVE market (e.g. a NO token at
                # 99¢ two hours before expiry).  Checking end_date prevents active
                # positions from appearing in the "Oracle pending" settlement banner.
                # If end_date is absent the market has likely been pruned (resolved),
                # so we keep the original behaviour and include it.
                end_date_str = enriched.get("end_date")
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        market_ended = end_dt <= datetime.now(timezone.utc)
                    except Exception:
                        market_ended = False
                else:
                    market_ended = True  # not in cache → assume resolved/pruned
                if market_ended:
                    # Market ended + price settled but UMA/oracle resolution not yet on-chain
                    awaiting_settlement.append(entry)

    # ── SOURCE OF TRUTH cross-reference ──────────────────────────────────────
    # Architecture principle: PM Data API is the authority.  Bot state is a
    # SHADOW of PM reality.  The only legitimate discrepancy is a token that
    # PM says you hold but the risk engine has NO record of.
    #
    # Cross-reference key priority:
    #   1. condition_id + canonical side  (from PM conditionId + outcome label)
    #      → works even when the market is resolved and pruned from the cache.
    #   2. token_id via pos.token_id      (cached at open time; survives prune)
    #   3. token_id via live market snap  (only works while market is in cache)
    # Any successful match → in_bot_state = True, no discrepancy.
    # ─────────────────────────────────────────────────────────────────────────

    # Build lookup structures from bot's open positions (two independent indexes)
    # Index A: (condition_id, side) → bot info  — primary, snapshot-independent
    bot_by_cond_side: dict[tuple[str, str], dict] = {}
    # Index B: token_id → bot info              — fallback when condition_id absent
    bot_by_token: dict[str, dict] = {}

    if state.risk_ref is not None:
        for pos in state.risk_ref.get_positions().values():
            if pos.is_closed:
                continue
            bot_info = {
                "market_id": pos.market_id,
                "side": pos.side,
                "size": pos.size,
                "entry_price": pos.entry_price,
                "market_title": getattr(pos, "market_title", ""),
            }
            # Index A: condition_id + side (always populated; persists after market prune)
            bot_by_cond_side[(pos.market_id, pos.side)] = bot_info
            # Index B-1: token_id cached on Position at open time
            if pos.token_id:
                bot_by_token[pos.token_id] = bot_info
            # Index B-2: token_id from current live market snapshot (best-effort)
            mkt_snap_pos = markets_snap.get(pos.market_id)
            if mkt_snap_pos:
                tid_live = mkt_snap_pos.token_id_yes if pos.side == "YES" else mkt_snap_pos.token_id_no
                bot_by_token[tid_live] = bot_info

    discrepancies = []
    for wp in wallet_positions:
        cond_id = wp["condition_id"]
        side    = wp["side_canonical"]
        tid     = wp["token_id"]

        # Try match in priority order
        bot = (
            bot_by_cond_side.get((cond_id, side))
            if cond_id else None
        ) or bot_by_token.get(tid)

        if bot:
            wp["in_bot_state"] = True
            size_diff = abs(wp["size"] - bot["size"])
            if size_diff > 0.01:
                if size_diff < 0.05 and state.risk_ref is not None:
                    # Small diff (<0.05 ct) — almost certainly a CLOB partial-fill
                    # rounding artifact.  Auto-correct Position.size from PM wallet
                    # (source of truth) silently; no error discrepancy raised.
                    state.risk_ref.reconcile_size(
                        wp["size"],
                        token_id=tid,
                        condition_id=cond_id or "",
                        side=side or "",
                    )
                else:
                    discrepancies.append({
                        "token_id": tid,
                        "type": "size_mismatch",
                        "pm_size": wp["size"],
                        "bot_size": bot["size"],
                        "diff": round(size_diff, 4),
                        "title": wp["title"],
                    })
        else:
            if wp["size"] > 0:
                discrepancies.append({
                    "token_id": tid,
                    "type": "unmanaged_by_bot",
                    "pm_size": wp["size"],
                    "bot_size": 0,
                    "title": wp["title"],
                    "outcome": wp["outcome"],
                })

    # Ghost detection: bot tracks a position that PM wallet no longer holds.
    # Build the set of (condition_id, side) pairs in the PM wallet.
    pm_cond_sides: set[tuple[str, str]] = {
        (wp["condition_id"], wp["side_canonical"])
        for wp in wallet_positions
        if wp["condition_id"]  # only include entries where condition_id was returned
    }
    pm_token_ids: set[str] = {wp["token_id"] for wp in wallet_positions}

    if state.risk_ref is not None:
        for pos in state.risk_ref.get_positions().values():
            if pos.is_closed:
                continue
            matched = (
                (pos.market_id, pos.side) in pm_cond_sides
                or (pos.token_id and pos.token_id in pm_token_ids)
                or (
                    markets_snap.get(pos.market_id) is not None
                    and (
                        markets_snap[pos.market_id].token_id_yes
                        if pos.side == "YES"
                        else markets_snap[pos.market_id].token_id_no
                    ) in pm_token_ids
                )
            )
            if not matched:
                mkt_snap_g = markets_snap.get(pos.market_id)
                fallback_tid = pos.token_id or (
                    (mkt_snap_g.token_id_yes if pos.side == "YES" else mkt_snap_g.token_id_no)
                    if mkt_snap_g else ""
                )
                discrepancies.append({
                    "token_id": fallback_tid,
                    "type": "bot_ghost",
                    "pm_size": 0,
                    "bot_size": pos.size,
                    "bot_side": pos.side,
                    "title": (
                        mkt_snap_g.title if mkt_snap_g
                        else getattr(pos, "market_title", "") or pos.market_id
                    ),
                })

    return {
        "wallet_positions": wallet_positions,
        "pending_redemption": pending_redemption,
        "awaiting_settlement": awaiting_settlement,
        "discrepancies": discrepancies,
        "wallet_count": len(wallet_positions),
        "pending_redemption_count": len(pending_redemption),
        "awaiting_settlement_count": len(awaiting_settlement),
        "discrepancy_count": len(discrepancies),
        "timestamp": now_ts,
    }


@app.post("/positions/ghost/dismiss")
async def dismiss_ghost_position(body: dict) -> dict:
    """
    Force-close a ghost position — one the bot tracks but PM wallet no longer holds
    (market resolved, tokens redeemed/expired, or position closed externally).

    Accepts JSON body: {"token_id": "<token>", "exit_price": <float>}
      - token_id:   the YES or NO token id for the ghost position
      - exit_price: optional (default 0.0 for YES, 1.0 for NO resolved-lose)
                    expressed in YES-probability space (same convention as entry_price)

    Calls risk.close_position() at the supplied exit_price so the trade gets a
    correct P&L entry in trades.csv and the position is removed from live state.
    """
    if state.risk_ref is None or state.pm_ref is None:
        raise HTTPException(status_code=503, detail="Bot not initialised")

    token_id_raw: str = body.get("token_id", "")
    if not token_id_raw:
        raise HTTPException(status_code=400, detail="token_id is required")

    # Look up the position that corresponds to this token_id.
    # We need to find the (market_id, side) pair.
    markets_snap = state.pm_ref.get_markets()
    found_market_id: str | None = None
    found_side: str | None = None

    for mkt in markets_snap.values():
        if mkt.token_id_yes == token_id_raw:
            found_market_id = mkt.condition_id
            found_side = "YES"
            break
        if mkt.token_id_no == token_id_raw:
            found_market_id = mkt.condition_id
            found_side = "NO"
            break

    # If not in current market list (expired/pruned), scan risk positions directly.
    # The risk engine stores market_id but not token_id, so we must match via
    # the markets_snap for in-scope markets. For expired/pruned markets, we fall
    # back to looking at what the bot_open map would have used when the market was live.
    if found_market_id is None:
        # Try to find any open position whose market_id maps to this token via
        # the currently known token → market mapping stored in pm._books key space.
        # As a last resort, expose the raw position lookup for the UI to help.
        raise HTTPException(
            status_code=404,
            detail=(
                f"token_id {token_id_raw[:24]}... not found in current market snapshot. "
                "The market may have been pruned. Restart the bot to reload expired markets "
                "and then retry, or close the position via Polymarket.com."
            ),
        )

    # Determine exit price.
    # Default: 0.0 (YES token worthless = market resolved as NO, or token expired at 0).
    # For NO ghost positions, if caller doesn't supply a price we also default 0.0
    # (YES prob = 0 means NO won → NO exit in YES-space = 0, PnL = -(0-entry)*size = +entry*size).
    raw_exit = body.get("exit_price")
    if raw_exit is not None:
        exit_price = float(raw_exit)
    else:
        exit_price = 0.0

    closed = state.risk_ref.close_position(
        found_market_id,
        exit_price,
        side=found_side,
    )
    if closed is None:
        raise HTTPException(
            status_code=404,
            detail=f"No open {found_side} position found for market {found_market_id}",
        )

    log.info(
        "Ghost position dismissed via API",
        market_id=found_market_id,
        side=found_side,
        exit_price=exit_price,
        pnl=round(closed.realized_pnl, 4),
    )

    return {
        "ok": True,
        "market_id": found_market_id,
        "side": found_side,
        "exit_price": exit_price,
        "size": closed.size,
        "pnl": round(closed.realized_pnl, 4),
    }


@app.post("/positions/{market_id}/close", dependencies=[Depends(require_auth)])
async def close_position_endpoint(market_id: str) -> dict:
    """
    Manually close ALL open positions for a market (both YES and NO legs).

    Performs the same teardown as an automatic monitor exit for each leg:
    - Places a paper taker order at book bid (YES) or book ask (NO).
    - Calls risk.close_position() to mark closed and append to trades.csv.
    - Fires the on_close_callback so the mispricing scanner resets its cooldown.

    Returns combined P&L across all closed legs.
    """
    from monitor import compute_unrealised_pnl

    monitor = state.monitor_ref
    pm = state.pm_ref
    risk_engine = state.risk_ref
    if monitor is None or pm is None or risk_engine is None:
        raise HTTPException(status_code=503, detail="Bot components not yet initialised")

    # Collect ALL open positions for this market (YES + NO sides for maker spreads)
    all_pos = [
        p for p in risk_engine.get_positions().values()
        if p.market_id == market_id and not p.is_closed
    ]
    if not all_pos:
        raise HTTPException(status_code=404, detail="Position not found or already closed")

    total_pnl = 0.0
    exit_prices: dict[str, float] = {}

    market = pm.get_markets().get(market_id)
    if market is None:
        # Market has been pruned from cache (resolved / expired).
        # No CLOB orders are possible; close risk-engine tracking directly.
        for pos in all_pos:
            unrealised = compute_unrealised_pnl(pos, pos.entry_price)
            exit_prices[pos.side] = round(pos.entry_price, 4)
            total_pnl += unrealised
            risk_engine.close_position(market_id, exit_price=pos.entry_price, side=pos.side)
            if monitor._on_close_callback is not None:
                try:
                    monitor._on_close_callback(pos.market_id)
                except Exception:
                    pass
        primary = exit_prices.get("YES") or exit_prices.get("NO") or 0.0
        return {
            "ok": True,
            "market_id": market_id,
            "sides_closed": list(exit_prices.keys()),
            "exit_prices": exit_prices,
            "exit_price": primary,
            "pnl": round(total_pnl, 4),
            "timestamp": time.time(),
        }

    book = pm.get_book(market.token_id_yes)

    for pos in all_pos:
        # Market order: YES exits at best bid (taker sell), NO exits at best ask
        if book is not None:
            if pos.side in ("YES", "BUY_YES"):
                exit_price = book.best_bid if book.best_bid is not None else (book.mid or pos.entry_price)
            else:
                exit_price = book.best_ask if book.best_ask is not None else (book.mid or pos.entry_price)
        else:
            exit_price = pos.entry_price

        unrealised = compute_unrealised_pnl(pos, exit_price)
        total_pnl += unrealised
        exit_prices[pos.side] = round(exit_price, 4)
        await monitor._exit_position(pos, market, exit_price, "manual", unrealised, force_taker=True)

    # Use YES exit price as the primary price for backwards-compat callers
    primary_price = exit_prices.get("YES") or exit_prices.get("NO") or 0.0

    return {
        "ok": True,
        "market_id": market_id,
        "sides_closed": list(exit_prices.keys()),
        "exit_prices": exit_prices,
        "exit_price": primary_price,   # backwards compat
        "pnl": round(total_pnl, 4),
        "timestamp": time.time(),
    }


# ── CTF on-chain redemption ───────────────────────────────────────────────────
# Helpers live in ctf_utils.py so monitor.py can import them without creating
# a circular dependency (api_server → monitor → api_server).
from ctf_utils import _build_redeem_calldata, _redeem_ctf_via_safe  # noqa: E402
from py_clob_client_v2.config import get_contract_config as _get_contract_config  # noqa: E402
_POLY_CONTRACTS = _get_contract_config(137)  # Polygon mainnet — CTF + collateral addresses


class RedeemRequest(BaseModel):
    token_id: str
    condition_id: str
    won: bool
    payout_usd: float = 0.0


@app.post("/positions/redeem", dependencies=[Depends(require_auth)])
async def redeem_position_endpoint(req: RedeemRequest) -> dict:
    """Redeem a settled Polymarket CTF position.

    For winning positions (won=True): submits an on-chain redeemPositions()
    call through the user's Gnosis Safe proxy wallet and closes bot tracking.

    For losing positions (won=False): closes bot tracking at exit_price=0.0.
    No on-chain call is needed — losing tokens pay out 0 USDC.

    Returns {ok, tx_hash (winning only), payout_usd, requires_manual_claim}.
    """
    pm = state.pm_ref
    risk_engine = state.risk_ref
    if pm is None or risk_engine is None:
        raise HTTPException(status_code=503, detail="Bot components not yet initialised")

    # Close any matching open bot position at the settlement price
    settlement_price = 1.0 if req.won else 0.0
    closed_count = 0
    markets_snap = pm.get_markets()
    target_market_id: Optional[str] = None

    # Find which market this token belongs to
    for mkt in markets_snap.values():
        if mkt.token_id_yes == req.token_id:
            target_market_id = mkt.condition_id
            break
        if mkt.token_id_no == req.token_id:
            target_market_id = mkt.condition_id
            break

    if target_market_id:
        for pos in list(risk_engine.get_positions().values()):
            if pos.market_id == target_market_id and not pos.is_closed:
                risk_engine.close_position(target_market_id, exit_price=settlement_price, side=pos.side)
                closed_count += 1

    # For losing positions, nothing further to do on-chain
    if not req.won:
        return {
            "ok": True,
            "won": False,
            "payout_usd": 0.0,
            "bot_positions_closed": closed_count,
            "requires_manual_claim": False,
        }

    # For winning positions, attempt on-chain CTF redemption via Gnosis Safe
    private_key = config.POLY_PRIVATE_KEY
    safe_address = config.POLY_FUNDER

    if not private_key:
        return {
            "ok": False,
            "error": "POLY_PRIVATE_KEY not configured",
            "requires_manual_claim": True,
            "payout_usd": req.payout_usd,
        }
    if not safe_address:
        return {
            "ok": False,
            "error": "POLY_FUNDER (Safe address) not configured",
            "requires_manual_claim": True,
            "payout_usd": req.payout_usd,
        }
    if not req.condition_id:
        return {
            "ok": False,
            "error": "condition_id required for on-chain redemption",
            "requires_manual_claim": True,
            "payout_usd": req.payout_usd,
        }

    try:
        ctf_address = _POLY_CONTRACTS.conditional_tokens
        collateral_address = _POLY_CONTRACTS.collateral

        # Redeem both outcome slots — CTF will pay the winning ones
        tx_hash = await _redeem_ctf_via_safe(
            ctf_address=ctf_address,
            collateral=collateral_address,
            condition_id=req.condition_id,
            index_sets=[1, 2],   # YES=1, NO=2 for binary markets
            private_key=private_key,
            safe_address=safe_address,
        )
        log.info("CTF redemption submitted", tx_hash=tx_hash, condition_id=req.condition_id)
        return {
            "ok": True,
            "won": True,
            "tx_hash": tx_hash,
            "payout_usd": req.payout_usd,
            "bot_positions_closed": closed_count,
            "requires_manual_claim": False,
        }
    except Exception as exc:
        log.warning("CTF on-chain redemption failed", exc=str(exc), condition_id=req.condition_id)
        return {
            "ok": False,
            "won": True,
            "error": str(exc),
            "payout_usd": req.payout_usd,
            "bot_positions_closed": closed_count,
            "requires_manual_claim": True,
        }


# ── Trades ────────────────────────────────────────────────────────────────────

@app.get("/trades")
def trades(
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    strategy: Optional[str] = Query(default=None),
    underlying: Optional[str] = Query(default=None),
) -> dict:
    """Paginated trade history from data/trades.csv."""
    rows = _load_trades_csv()

    # Filter
    if strategy:
        rows = [r for r in rows if r.get("strategy", "").lower() == strategy.lower()]
    if underlying:
        # Main rows: filter by underlying as usual.
        # Hedge rows (strategy="momentum_hedge") are recorded with underlying="" so they
        # would always be dropped by the filter, causing the per-coin P&L to appear wrong
        # (all losses) while "All" shows positive (hedge P&L included).
        # Fix: after identifying the matching main rows, also include any momentum_hedge
        # rows whose market_id is in that set.
        main_rows = [r for r in rows if r.get("underlying", "").upper() == underlying.upper()]
        main_market_ids = {r["market_id"] for r in main_rows}
        hedge_rows = [
            r for r in rows
            if r.get("strategy") == "momentum_hedge"
            and r.get("market_id") in main_market_ids
            and r.get("underlying", "").upper() != underlying.upper()  # avoid duplicates if underlying was set
        ]
        rows = main_rows + hedge_rows

    total = len(rows)
    page = rows[offset: offset + limit]
    return {
        "trades": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "timestamp": time.time(),
    }


# ── Accounting endpoints (accounting.py / acct_ledger.csv / acct_positions.json) ──

@app.get("/acct/ledger")
def acct_ledger(
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    strategy: Optional[str] = Query(default=None),
    underlying: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    fill_type: Optional[str] = Query(default=None),
) -> dict:
    """Paginated finalized ledger records from data/acct_ledger.csv."""
    if not ACCT_LEDGER_CSV.exists():
        return {"rows": [], "total": 0, "limit": limit, "offset": offset, "timestamp": time.time()}
    try:
        with ACCT_LEDGER_CSV.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        log.error("Failed to read acct_ledger.csv", exc=str(exc))
        return {"rows": [], "total": 0, "limit": limit, "offset": offset, "timestamp": time.time()}

    if strategy:
        rows = [r for r in rows if r.get("strategy", "").lower() == strategy.lower()]
    if underlying:
        rows = [r for r in rows if r.get("underlying", "").upper() == underlying.upper()]
    if status:
        rows = [r for r in rows if r.get("status", "").upper() == status.upper()]
    if fill_type:
        rows = [r for r in rows if r.get("fill_type", "").upper() == fill_type.upper()]

    rows = list(reversed(rows))  # most recent first
    total = len(rows)
    return {
        "rows": rows[offset: offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "timestamp": time.time(),
    }


@app.get("/acct/positions")
def acct_positions(
    status: Optional[str] = Query(default=None),
    strategy: Optional[str] = Query(default=None),
    underlying: Optional[str] = Query(default=None),
) -> dict:
    """All AccountingPosition objects from data/acct_positions.json."""
    if not ACCT_POSITIONS_JSON.exists():
        return {"positions": [], "timestamp": time.time()}
    try:
        raw = json.loads(ACCT_POSITIONS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to read acct_positions.json", exc=str(exc))
        return {"positions": [], "timestamp": time.time()}

    positions = list(raw.values()) if isinstance(raw, dict) else raw
    if status:
        statuses = {s.strip().lower() for s in status.split(",")}
        positions = [p for p in positions if p.get("status", "").lower() in statuses]
    if strategy:
        positions = [p for p in positions if p.get("strategy", "").lower() == strategy.lower()]
    if underlying:
        positions = [p for p in positions if p.get("underlying", "").upper() == underlying.upper()]

    return {"positions": positions, "timestamp": time.time()}


@app.get("/acct/pending")
def acct_pending() -> dict:
    """Positions in non-terminal, non-LIVE status (CLOSING, PENDING_RESOLVE, etc.)."""
    _pending_statuses = {"closing", "pending_resolve"}
    if not ACCT_POSITIONS_JSON.exists():
        return {"positions": [], "timestamp": time.time()}
    try:
        raw = json.loads(ACCT_POSITIONS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to read acct_positions.json for pending", exc=str(exc))
        return {"positions": [], "timestamp": time.time()}

    positions = list(raw.values()) if isinstance(raw, dict) else raw
    pending = [p for p in positions if p.get("status", "").lower() in _pending_statuses]
    pending.sort(key=lambda p: p.get("closing_since") or p.get("entry_time") or "", reverse=True)
    return {"positions": pending, "timestamp": time.time()}


@app.get("/market_outcomes")
def market_outcomes_endpoint() -> dict:
    """Return resolved YES-token prices keyed by condition_id.

    Written by monitor.py whenever a position closes — either at settlement
    (RESOLVED exit) or retroactively after a taker/stop-loss exit once the
    market's end_date passes and the Gamma API confirms resolution.

    Response shape:
        { condition_id: { resolved_yes_price: 0.0 | 1.0 } }
    """
    if not MARKET_OUTCOMES_JSON.exists():
        return {}
    try:
        return json.loads(MARKET_OUTCOMES_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to read market_outcomes.json", exc=str(exc))
        return {}


# ── Polymarket source-of-truth trade history ──────────────────────────────────

@app.get("/pm_history")
async def pm_history_endpoint(
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Fetch trade/redeem activity directly from the Polymarket Data API.

    Uses data-api.polymarket.com/activity?user=<funder_address> which is the
    same source Polymarket's own UI uses for the history panel.  Returns raw
    activity rows so the frontend can display the source-of-truth alongside
    our internal trades.csv records.

    Response shape:
        { "rows": [ { proxyWallet, timestamp, type, size, usdcSize, price,
                       side, title, slug, outcome, ... }, ... ] }
    """
    funder = config.POLY_FUNDER
    if not funder:
        raise HTTPException(status_code=503, detail="POLY_FUNDER not configured")
    url = f"https://data-api.polymarket.com/activity?user={funder}&limit={limit}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Data API returns either a list directly or {"value": [...]}
            rows = data if isinstance(data, list) else data.get("value", [])
            return {"rows": rows}
    except httpx.HTTPStatusError as exc:
        log.error("PM Data API error", status=exc.response.status_code, url=url)
        raise HTTPException(status_code=502, detail=f"PM Data API returned {exc.response.status_code}")
    except Exception as exc:
        log.error("PM history fetch failed", exc=str(exc))
        raise HTTPException(status_code=502, detail="Failed to fetch PM history")


@app.post("/reconcile", dependencies=[Depends(require_auth)])
async def reconcile_endpoint() -> dict:
    """Reconcile trades.csv against Polymarket Data API (source of truth).

    Fetches actual fill prices from data-api.polymarket.com/activity and patches
    trades.csv rows where the bot's recorded prices diverge from PM's on-chain data.

    Fixes two known recording bugs:
      1. Entry price stored as order price instead of actual CLOB fill price.
      2. TP-sell exits recorded as WIN at $1.00 when the position was actually
         sold early at a taker price (e.g. 43¢) — often a loss.

    Paper-trading runs are skipped (no PM on-chain activity to reference).
    Returns:
        { "status": "ok"|"skipped", "patched": int, "markets": [...], "errors": [...] }
    """
    if config.PAPER_TRADING:
        return {"status": "skipped", "reason": "paper trading — no PM source of truth", "patched": 0}

    from pm_reconcile import reconcile_trades_csv
    result = await reconcile_trades_csv(config.POLY_FUNDER)
    return {"status": "ok", **result}


# ── Order event log ───────────────────────────────────────────────────────────

@app.get("/orders")
def orders_endpoint(
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    token_id: Optional[str] = Query(default=None),
    order_type: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
) -> dict:
    """Paginated order event log from data/orders.csv (placed live orders only)."""
    if not ORDERS_CSV.exists():
        return {"orders": [], "total": 0, "limit": limit, "offset": offset, "timestamp": time.time()}
    try:
        with ORDERS_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows: list[dict] = list(reader)
    except Exception:
        rows = []

    rows = list(reversed(rows))  # newest first
    if token_id:
        rows = [r for r in rows if r.get("token_id", "") == token_id]
    if order_type:
        rows = [r for r in rows if r.get("order_type", "").lower() == order_type.lower()]
    if action:
        rows = [r for r in rows if r.get("action", "").lower() == action.lower()]

    total = len(rows)
    page = rows[offset: offset + limit]
    return {"orders": page, "total": total, "limit": limit, "offset": offset, "timestamp": time.time()}


# ── P&L summary ───────────────────────────────────────────────────────────────

@app.get("/pnl")
def pnl() -> dict:
    """Daily / weekly / all-time P&L summary."""
    rows = _load_trades_csv()
    now = datetime.now(timezone.utc)

    def _sum_pnl(rows_subset: list[dict]) -> float:
        return sum(float(r.get("pnl", 0)) for r in rows_subset)

    today_rows = [r for r in rows if _row_age_days(r, now) < 1]
    week_rows = [r for r in rows if _row_age_days(r, now) < 7]

    return {
        "today": round(_sum_pnl(today_rows), 4),
        "week": round(_sum_pnl(week_rows), 4),
        "all_time": round(_sum_pnl(rows), 4),
        "trade_count_today": len(today_rows),
        "trade_count_week": len(week_rows),
        "trade_count_all": len(rows),
        "timestamp": time.time(),
    }


# ── Performance analytics ─────────────────────────────────────────────────────

@app.get("/performance")
def performance(period: str = Query(default="all", pattern="^(7d|30d|all)$")) -> dict:
    """
    Full analytics: win rate, Sharpe, equity curve, rebates, breakdowns.
    Recomputed fresh on each request from data/trades.csv.
    """
    rows = _load_trades_csv()
    now = datetime.now(timezone.utc)

    # Filter by period
    if period == "7d":
        rows = [r for r in rows if _row_age_days(r, now) < 7]
    elif period == "30d":
        rows = [r for r in rows if _row_age_days(r, now) < 30]

    if not rows:
        return {"period": period, "no_data": True, "timestamp": time.time()}

    pnl_vals = [float(r.get("pnl", 0)) for r in rows]
    fees_vals = [float(r.get("fees_paid", 0)) for r in rows]
    rebate_vals = [float(r.get("rebates_earned", 0)) for r in rows]

    wins = sum(1 for p in pnl_vals if p > 0)
    total_trades = len(pnl_vals)
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    total_pnl = sum(pnl_vals)
    total_fees = sum(fees_vals)
    total_rebates = sum(rebate_vals)
    avg_pnl = total_pnl / total_trades if total_trades else 0.0

    # Equity curve (cumulative, sorted by timestamp)
    sorted_rows = sorted(rows, key=lambda r: r.get("timestamp", ""))
    equity_curve = []
    cumulative = 0.0
    for r in sorted_rows:
        cumulative += float(r.get("pnl", 0))
        equity_curve.append({
            "t": r.get("timestamp", ""),
            "equity": round(cumulative, 4),
        })

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for pt in equity_curve:
        eq = pt["equity"]
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)

    # Sharpe (7-day rolling if period=all)
    sharpe = _compute_sharpe(sorted_rows)

    # By strategy
    by_strategy: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    for r in rows:
        s = r.get("strategy", "unknown")
        by_strategy[s]["pnl"] += float(r.get("pnl", 0))
        by_strategy[s]["count"] += 1

    # By underlying
    by_underlying: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    for r in rows:
        u = r.get("underlying", "unknown")
        by_underlying[u]["pnl"] += float(r.get("pnl", 0))
        by_underlying[u]["count"] += 1

    # By market type (bucket_5m / bucket_15m / bucket_1h / milestone)
    by_market_type: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0, "wins": 0})
    for r in rows:
        mt = r.get("market_type") or "unknown"
        pnl_v = float(r.get("pnl", 0))
        by_market_type[mt]["pnl"] += pnl_v
        by_market_type[mt]["count"] += 1
        if pnl_v > 0:
            by_market_type[mt]["wins"] += 1
    # Finalise: round pnl, compute win_rate, drop raw wins counter
    by_market_type_out: dict[str, dict] = {}
    for mt, d in by_market_type.items():
        by_market_type_out[mt] = {
            "pnl": round(d["pnl"], 4),
            "count": d["count"],
            "win_rate": round(d["wins"] / d["count"], 4) if d["count"] > 0 else 0.0,
        }

    # P&L histogram (10 buckets)
    histogram = _pnl_histogram(pnl_vals, buckets=10)

    # Best/worst 5 trades
    sorted_by_pnl = sorted(rows, key=lambda r: float(r.get("pnl", 0)), reverse=True)
    best_trades = sorted_by_pnl[:5]
    worst_trades = sorted_by_pnl[-5:][::-1]

    # Time-of-day heatmap (HKT = UTC+8)
    heatmap = _time_of_day_heatmap(sorted_rows)

    return {
        "period": period,
        "summary": {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "total_pnl": round(total_pnl, 4),
            "total_fees": round(total_fees, 4),
            "total_rebates": round(total_rebates, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe_7d": round(sharpe, 4) if sharpe is not None else None,
        },
        "equity_curve": equity_curve,
        "by_strategy": dict(by_strategy),
        "by_underlying": dict(by_underlying),
        "by_market_type": by_market_type_out,
        "pnl_histogram": histogram,
        "best_trades": best_trades,
        "worst_trades": worst_trades,
        "time_of_day_heatmap": heatmap,
        "timestamp": time.time(),
    }


# ── Maker quotes ─────────────────────────────────────────────────────────────

@app.get("/maker/quotes")
def maker_quotes() -> dict:
    """Active market-making quotes currently posted on Polymarket CLOB."""
    age_now = time.time()
    quotes = []
    for tid, q in state.active_quotes.items():
        age_s = round(age_now - float(q.get("posted_at", age_now)), 1)
        quotes.append({
            **q,
            "age_seconds": age_s,
        })
    # Sort newest first
    quotes.sort(key=lambda x: x.get("posted_at", 0), reverse=True)
    return {
        "quotes": quotes,
        "count": len(quotes),
        "strategy_enabled": config.STRATEGY_MAKER_ENABLED,
        "timestamp": time.time(),
    }


@app.get("/maker/signals")
def maker_signals_endpoint() -> dict:
    """Evaluated market opportunities — both deployed (open orders) and undeployed."""
    now = time.time()
    signals = []
    deployed_keys = set(state.active_quotes.keys())
    for tid, s in state.maker_signals.items():
        mkt = state.markets.get(s.get("market_id", ""), {})
        signals.append({
            **s,
            "market_title": mkt.get("title", s.get("market_id", "")),
            "market_slug": mkt.get("market_slug", ""),
            "end_date": mkt.get("end_date"),
            "is_deployed": tid in deployed_keys,
            "age_seconds": round(now - float(s.get("ts", now)), 1),
        })
    signals.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return {
        "signals": signals,
        "count": len(signals),
        "strategy_enabled": config.STRATEGY_MAKER_ENABLED,
        "timestamp": now,
    }


@app.get("/maker/capital")
def maker_capital() -> dict:
    """Capital ledger: total budget, deployed in quotes, in positions, available."""
    if state.maker_ref is None:
        return {
            "total_budget": config.PAPER_CAPITAL_USD,
            "deployed": 0.0,
            "in_positions": 0.0,
            "available": config.PAPER_CAPITAL_USD,
            "mode": config.MAKER_DEPLOYMENT_MODE,
            "timestamp": time.time(),
        }
    deployed = state.maker_ref.deployed_capital
    available = state.maker_ref.available_capital
    risk_st = state.maker_ref._risk.get_state()
    in_positions = risk_st.get("total_pm_capital_deployed", 0.0)
    return {
        "total_budget": config.PAPER_CAPITAL_USD,
        "deployed": round(deployed, 2),
        "in_positions": round(in_positions, 2),
        "available": round(available, 2),
        "mode": config.MAKER_DEPLOYMENT_MODE,
        "timestamp": time.time(),
    }


@app.get("/maker/inventory")
def maker_inventory() -> dict:
    """Net position delta per coin and current HL hedge state."""
    if state.maker_ref is None:
        return {"position_delta": {}, "fill_inventory": {}, "coin_hedges": {}, "threshold_usd": config.HEDGE_THRESHOLD_USD, "timestamp": time.time()}
    pos_delta = {}
    all_coins = set(state.maker_ref._inventory.keys())
    for coin in all_coins:
        pos_delta[coin] = round(state.maker_ref._position_delta_usd(coin), 2)
    fill_inv = {coin: round(usd, 2) for coin, usd in state.maker_ref._inventory.items()}
    hedges = {}
    for coin, h in state.maker_ref._coin_hedges.items():
        hedges[coin] = {
            "direction": h["direction"],
            "size_coins": round(h["size"], 6),
            "entry_price": round(h["price"], 2),
            "notional_usd": round(h["size"] * h["price"], 2),
        }
    return {
        "position_delta": pos_delta,
        "fill_inventory": fill_inv,
        "coin_hedges": hedges,
        "threshold_usd": config.HEDGE_THRESHOLD_USD,
        "timestamp": time.time(),
    }


@app.post("/maker/deploy/{token_id}", dependencies=[Depends(require_auth)])
async def deploy_signal(token_id: str) -> dict:
    """Manually deploy a signal as live quotes (use in manual deployment mode)."""
    if state.maker_ref is None:
        raise HTTPException(status_code=503, detail="Maker strategy not initialized")
    ok = await state.maker_ref.deploy_signal(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Signal not found or market unavailable")
    return {"ok": True, "token_id": token_id, "timestamp": time.time()}


@app.post("/maker/undeploy/{token_id}", dependencies=[Depends(require_auth)])
async def undeploy_quote_endpoint(token_id: str) -> dict:
    """Cancel open orders for a market and revert to signal-only state."""
    if state.maker_ref is None:
        raise HTTPException(status_code=503, detail="Maker strategy not initialized")
    ok = await state.maker_ref.undeploy_quote(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="No open quotes found for this token")
    return {"ok": True, "token_id": token_id, "timestamp": time.time()}


@app.get("/hedge-quality")
def hedge_quality(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """
    Return recent HL hedge execution quality records (newest-first).

    Each record includes:
      - ts            ISO timestamp of the hedge placement
      - coin          Underlying coin
      - direction     LONG or SHORT
      - size_coins    Hedge size in coin units
      - decision_mid  HL mid at decision time
      - exec_price_est  Estimated execution price (bid/ask at decision time)
      - slippage_pct  (exec_price_est − mid) / mid × 100  (always ≥ 0 for taker)
      - bbo_spread    HL spread (ask − bid) at decision time
      - notional_usd  USD notional of the hedge

    Use this data to tune HEDGE_THRESHOLD_USD, HEDGE_MIN_INTERVAL, and
    HEDGE_DEBOUNCE_SECS: high average slippage suggests waiting longer for
    more fills to accumulate before executing.
    """
    if state.maker_ref is None:
        return {"records": [], "total": 0, "timestamp": time.time()}
    records = state.maker_ref.get_hedge_quality(limit=limit)
    return {"records": records, "total": len(records), "timestamp": time.time()}


# ── Signals ───────────────────────────────────────────────────────────────────

@app.get("/signals")
def signals(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Recent Strategy 2 mispricing signals with agent decisions — sorted by score descending."""
    recent = sorted(state.signals, key=lambda s: s.get("score", 0.0), reverse=True)[:limit]
    return {
        "signals": recent,
        "total": len(state.signals),
        "timestamp": time.time(),
    }


@app.get("/momentum/signals")
def momentum_signals(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Recent Strategy 3 momentum signals — most recent first."""
    recent = list(reversed(state.momentum_signals))[:limit]
    return {
        "signals": recent,
        "total": len(state.momentum_signals),
        "timestamp": time.time(),
    }


@app.get("/momentum/diagnostics")
async def momentum_diagnostics() -> dict:
    """Return per-market momentum diagnostics from the last completed scan pass.

    Shape:
      scan_ts  — unix timestamp of the last scan (0 if no scan has run yet)
      markets  — one dict per bucket market with: skip_reason, p_yes, p_no,
                 book_age_s, side, token_price, spot, spot_age_s, strike,
                 tte_seconds, sigma_ann, sigma_tau, threshold_pct, delta_pct,
                 gap_pct (delta-threshold; negative = below threshold),
                 observed_z, vol_source, ask_depth_usd, cooldown_remaining_s,
                 dist_to_band, plus config context fields (configured_z,
                 band_lo/hi, min_tte_s, book/spot_max_age_s, min_clob_depth).
      summary  — skip-count breakdown (bucket_markets, signals_fired,
                 skipped_band, skipped_delta, skipped_vol, etc.)
    """
    if state.momentum_ref is None:
        raise HTTPException(status_code=503, detail="Momentum scanner not initialised")
    result = await state.momentum_ref.diagnostics()
    result["timestamp"] = time.time()
    return result


@app.get("/momentum/scan_summary")
async def momentum_scan_summary() -> dict:
    """Return just the skip-count summary from the last momentum scan pass.

    Lightweight alternative to /momentum/diagnostics when you only need
    aggregate skip counts (e.g., for dashboards or quick health checks).
    """
    if state.momentum_ref is None:
        raise HTTPException(status_code=503, detail="Momentum scanner not initialised")
    d = await state.momentum_ref.diagnostics()
    return {
        "scan_ts": d.get("scan_ts", 0),
        "summary": d.get("summary", {}),
        "timestamp": time.time(),
    }


@app.get("/momentum/events")
async def get_momentum_events(n: int = 200) -> dict:
    """Return the last *n* events from data/momentum_events.jsonl (newest first).

    Events are written by the scanner and monitor whenever key trading
    lifecycle milestones occur (SESSION_START, BUY_SUBMIT, BUY_FILL,
    BUY_CANCEL_TIMEOUT, BUY_FAILED, SELL_SUBMIT, SELL_CLOSE, SELL_FAILED).
    """
    from strategies.Momentum.event_log import read_recent
    events = read_recent(n)
    return {"events": events, "count": len(events)}


# ── Opening Neutral ───────────────────────────────────────────────────────────

@app.get("/opening_neutral/status")
def opening_neutral_status() -> dict:
    """Strategy 5 (Opening Neutral) runtime status.

    Returns:
      enabled      — whether OPENING_NEUTRAL_ENABLED is True in config
      dry_run      — whether OPENING_NEUTRAL_DRY_RUN is True (no real orders)
      active_pairs — count of live YES+NO pairs currently tracked
      pairs        — array of pair detail objects
      recent_signals — last 20 scan-attempt diagnostics
      timestamp    — unix epoch
    """
    if state.opening_neutral_ref is None:
        return {
            "enabled": config.OPENING_NEUTRAL_ENABLED,
            "dry_run": config.OPENING_NEUTRAL_DRY_RUN,
            "active_pairs": 0,
            "pairs": [],
            "recent_signals": [],
            "scanner_running": False,
            "timestamp": time.time(),
        }
    status = state.opening_neutral_ref.get_status()
    status["scanner_running"] = getattr(state.opening_neutral_ref, "_running", False)
    status["timestamp"] = time.time()
    return status


# ── Proxy ────────────────────────────────────────────────────────────────────

_GAMMA_EVENTS_URL = (
    "https://gamma-api.polymarket.com/events"
    "?limit=500&active=true&closed=false&order=volume24hr&ascending=false"
)


@app.get("/proxy/polymarket/events")
async def proxy_polymarket_events() -> list:
    """Proxy Polymarket gamma-API events list to avoid browser CORS restrictions.

    Returns the raw list of active events sorted by 24-h volume, identical to
    what the frontend would fetch directly but routed server-side where there
    are no CORS constraints.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_GAMMA_EVENTS_URL)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("events", [])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Polymarket API error: {exc}") from exc


# ── Risk ──────────────────────────────────────────────────────────────────────

@app.get("/risk")
def risk() -> dict:
    """Current exposure vs configured limits."""
    # Calculate totals from current positions
    total_pm = sum(
        float(p.get("size_usd", 0)) for p in state.positions.values()
        if p.get("venue") == "PM"
    )
    total_hl = sum(
        abs(float(p.get("size_usd", 0))) for p in state.positions.values()
        if p.get("venue") == "HL"
    )
    open_count = len(state.positions)

    return {
        "pm_exposure_usd": round(total_pm, 2),
        "pm_exposure_limit": config.MAX_TOTAL_PM_EXPOSURE,
        "pm_exposure_pct": round(total_pm / config.MAX_TOTAL_PM_EXPOSURE * 100, 1),
        "hl_notional_usd": round(total_hl, 2),
        "hl_notional_limit": config.MAX_HL_NOTIONAL,
        "hl_notional_pct": round(total_hl / config.MAX_HL_NOTIONAL * 100, 1),
        "open_positions": open_count,
        "max_concurrent_positions": config.MAX_CONCURRENT_POSITIONS,
        "hard_stop_threshold": config.HARD_STOP_DRAWDOWN,
        "max_pm_per_market": config.MAX_PM_EXPOSURE_PER_MARKET,
        "paper_trading": state.paper_trading,
        "timestamp": time.time(),
    }


# ── Markets ───────────────────────────────────────────────────────────────────

@app.get("/markets")
def markets() -> dict:
    """Currently tracked PM markets with live bid/ask.

    Price priority per market:
      1. active_quote  — maker's own posted quote (most authoritative)
      2. pm_book       — PM orderbook best bid/ask from live WS snapshot
      3. null          — no data available
    """
    market_list = []
    for cid, m in state.markets.items():
        token_id  = m.get("token_id_yes", "")
        quote_bid = state.active_quotes.get(token_id)
        quote_ask = state.active_quotes.get(f"{token_id}_ask")

        def _valid_prob(v) -> bool:
            """PM prices are probabilities in [0, 1]. Reject anything outside that range."""
            return v is not None and 0.0 <= float(v) <= 1.0

        # Bid — priority 1: active quote, 2: PM orderbook
        if quote_bid is not None and _valid_prob(quote_bid.get("price")):
            bid_price  = quote_bid.get("price")
            bid_source = "active_quote"
        else:
            _book_bid  = m.get("yes_book_bid")
            bid_price  = _book_bid if _valid_prob(_book_bid) else None
            bid_source = "pm_book" if bid_price is not None else None

        # Ask — priority 1: active quote, 2: PM orderbook
        if quote_ask is not None and _valid_prob(quote_ask.get("price")):
            ask_price  = quote_ask.get("price")
            ask_source = "active_quote"
        else:
            _book_ask  = m.get("yes_book_ask")
            ask_price  = _book_ask if _valid_prob(_book_ask) else None
            ask_source = "pm_book" if ask_price is not None else None

        # Book staleness in seconds (None if no book data)
        book_ts    = m.get("yes_book_ts")
        book_age_s = round(time.time() - book_ts, 1) if book_ts else None

        # Data warning level for this market's price feed
        if book_age_s is None:
            data_warning = "no_data"
        elif book_age_s > 120:
            data_warning = "very_stale"
        elif book_age_s > 30:
            data_warning = "stale"
        else:
            data_warning = None

        market_list.append({
            **m,
            "bid_price":    bid_price,
            "ask_price":    ask_price,
            "bid_source":   bid_source,
            "ask_source":   ask_source,
            "book_age_s":   book_age_s,
            "data_warning": data_warning,
            "quoted":       quote_bid is not None,
        })
    return {
        "markets": market_list,
        "count":   len(market_list),
        "timestamp": time.time(),
    }


# ── Funding ───────────────────────────────────────────────────────────────────

@app.get("/funding")
def funding() -> dict:
    """Latest HL predicted funding rates."""
    return {
        "funding": state.funding,
        "timestamp": time.time(),
    }


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
def logs(
    limit: int = Query(default=200, ge=1, le=1000),
    level: str = Query(default="ALL", pattern="^(ALL|DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    module: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> dict:
    """Recent structured log entries from the in-memory ring buffer."""
    entries = ring_buffer.get_recent(limit=limit, level=level, module=module, search=search)
    return {
        "logs": entries,
        "total": len(entries),
        "modules": ring_buffer.all_modules(),
        "timestamp": time.time(),
    }


@app.get("/logs/errors")
def logs_errors(
    limit: int = Query(default=500, ge=1, le=5000),
    module: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
) -> dict:
    """WARNING+ entries from the long-lived error ring buffer (up to 20 000 entries).

    Provides historical warning/error visibility beyond the main ring buffer's
    recency window — useful for tracking intermittent issues over longer periods.
    """
    entries = warn_ring_buffer.get_recent(limit=limit, module=module, search=search)
    return {
        "logs": entries,
        "total": len(entries),
        "modules": warn_ring_buffer.all_modules(),
        "timestamp": time.time(),
    }


# ── Fills (paper-mode fill audit log) ───────────────────────────────────────

@app.get("/fills")
def fills(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    market_id: Optional[str] = Query(default=None),
    underlying: Optional[str] = Query(default=None),
    adverse_only: bool = Query(default=False),
) -> dict:
    """
    Paginated paper-fill audit log from data/fills.csv.

    Each row contains the full fill context recorded at simulation time:
    fill price, contracts filled, USD deployed, live orderbook snapshot
    (bid/ask/depth), taker model parameters (arrival_prob, mean_taker,
    taker_size_drawn), and HL mid + adverse-selection flag.
    """
    rows = _load_fills_csv()
    if market_id:
        rows = [r for r in rows if market_id in r.get("market_id", "")]
    if underlying:
        rows = [r for r in rows if r.get("underlying", "").upper() == underlying.upper()]
    if adverse_only:
        rows = [r for r in rows if r.get("adverse", "").lower() == "true"]
    total = len(rows)
    page = rows[offset: offset + limit]
    return {
        "fills": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "timestamp": time.time(),
    }


# ── Analytics helpers ─────────────────────────────────────────────────────────

def _load_trades_csv() -> list[dict]:
    """Load all rows from data/trades.csv. Returns [] if missing."""
    if not TRADES_CSV.exists():
        return []
    try:
        with TRADES_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        # Guard against headerless CSVs: if the CSV was written without a header,
        # DictReader uses the first data row as field names, producing garbage.
        # Detect this and re-read with explicit fieldnames from risk.py.
        if rows and "timestamp" not in rows[0]:
            log.warning("trades.csv missing header row — re-reading with explicit fieldnames")
            from risk import TRADES_HEADER as _TRADES_HDR
            with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f, fieldnames=_TRADES_HDR))
        return rows
    except Exception as exc:
        log.error("Failed to read trades CSV", exc=str(exc))
        return []


def _load_fills_csv() -> list[dict]:
    """Load all rows from data/fills.csv in reverse-chronological order. Returns [] if missing."""
    if not FILLS_CSV.exists():
        return []
    try:
        with FILLS_CSV.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        rows.reverse()  # most recent first
        return rows
    except Exception as exc:
        log.error("Failed to read fills CSV", exc=str(exc))
        return []


def _row_age_days(row: dict, now: datetime) -> float:
    """Return how many days ago this trade closed."""
    ts_str = row.get("timestamp", "")
    if not ts_str:
        return 9999.0
    try:
        # ISO format timestamp written by risk.py
        from datetime import timezone
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.timestamp()
        return (now.timestamp() - ts) / 86400
    except (ValueError, TypeError):
        return 9999.0


def _compute_sharpe(sorted_rows: list[dict]) -> Optional[float]:
    """Compute 7-day rolling Sharpe: mean_daily_pnl / std_daily_pnl * sqrt(365)."""
    if len(sorted_rows) < 2:
        return None

    # Group by calendar day
    daily: dict = defaultdict(float)
    seven_ago = time.time() - 7 * 86400
    for r in sorted_rows:
        try:
            ts = datetime.fromisoformat(r.get("timestamp", "")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < seven_ago:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] += float(r.get("pnl", 0))

    if len(daily) < 2:
        return None

    daily_pnls = list(daily.values())
    mean = sum(daily_pnls) / len(daily_pnls)
    try:
        std = statistics.stdev(daily_pnls)
    except statistics.StatisticsError:
        return None
    if std == 0:
        return None
    return mean / std * math.sqrt(365)


def _pnl_histogram(pnl_vals: list[float], buckets: int = 10) -> list[dict]:
    """Build a histogram of P&L values for the distribution chart."""
    if not pnl_vals:
        return []
    mn, mx = min(pnl_vals), max(pnl_vals)
    if mn == mx:
        return [{"bucket": f"{mn:.2f}", "count": len(pnl_vals)}]
    width = (mx - mn) / buckets
    bucket_counts: list[int] = [0] * buckets
    for v in pnl_vals:
        idx = min(int((v - mn) / width), buckets - 1)
        bucket_counts[idx] += 1
    return [
        {
            "bucket_start": round(mn + i * width, 4),
            "bucket_end": round(mn + (i + 1) * width, 4),
            "count": bucket_counts[i],
        }
        for i in range(buckets)
    ]


def _time_of_day_heatmap(sorted_rows: list[dict]) -> list[dict]:
    """Average P&L by hour-of-day (HKT = UTC+8)."""
    by_hour: dict[int, list[float]] = defaultdict(list)
    for r in sorted_rows:
        try:
            ts = datetime.fromisoformat(r.get("timestamp", "")).timestamp()
        except (ValueError, TypeError):
            continue
        hkt_hour = (datetime.fromtimestamp(ts, tz=timezone.utc).hour + 8) % 24
        by_hour[hkt_hour].append(float(r.get("pnl", 0)))
    return [
        {
            "hour_hkt": h,
            "avg_pnl": round(sum(v) / len(v), 4),
            "trade_count": len(v),
        }
        for h, v in sorted(by_hour.items())
    ]


# ── Market P&L ────────────────────────────────────────────────────────────────

@app.get("/market_pnl")
def market_pnl_all() -> dict:
    """Return RiskEngine.market_pnl() for every market that has at least one
    tracked position (open or recently closed).

    Response:
        { "markets": { <market_id>: MarketPnlDict, ... }, "timestamp": float }

    Each value is the dict returned by RiskEngine.market_pnl(market_id):
        realized_pnl        — sum of closed position legs
        unrealised_pnl      — 0.0 (server has no live book price here;
                              use the position-level unrealised_pnl_usd field)
        hedge_realized_pnl  — HedgeOrder.net_pnl for the market
        total_pnl           — realized + unrealised + hedge_realized
        positions           — list of {side, size, entry_price, realized_pnl, is_closed}
        hedge               — HedgeOrder summary or null
    """
    if state.risk_ref is None:
        return {"markets": {}, "timestamp": time.time()}

    # Collect unique market IDs from currently-tracked positions (including
    # recently closed ones that are still in state.positions).
    market_ids: set[str] = {
        p["condition_id"]
        for p in state.positions.values()
        if p.get("condition_id") and not p.get("condition_id", "").startswith("hl_hedge_")
    }
    result = {}
    for mid in market_ids:
        try:
            result[mid] = state.risk_ref.market_pnl(mid)
        except Exception:
            pass  # individual market errors must not break the whole response

    return {"markets": result, "timestamp": time.time()}


@app.get("/market_pnl/{market_id}")
def market_pnl_single(market_id: str) -> dict:
    """Return RiskEngine.market_pnl() for a single market.

    Returns 404 if the risk engine is not yet initialised; returns a zero-value
    dict if the market_id is unknown (no positions tracked for it).
    """
    if state.risk_ref is None:
        raise HTTPException(status_code=503, detail="Risk engine not yet initialised")
    return state.risk_ref.market_pnl(market_id)


# ── Server entry point ────────────────────────────────────────────────────────

async def run_api_server(port: int = config.API_PORT) -> None:
    """
    Run the FastAPI server as an asyncio coroutine.
    Called via: asyncio.create_task(run_api_server())
    """
    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    log.info("API server starting", port=port)
    await server.serve()
