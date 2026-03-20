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

import csv
import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from logger import get_bot_logger, ring_buffer

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

    # Agent shadow log (filled by agent)
    agent_shadow_log: list = field(default_factory=list)

    # Data quality metrics (filled by state-sync loop in main.py)
    data_quality: dict = field(default_factory=dict)

    # Live component references — set once at startup by main.py
    monitor_ref: Any = None   # PositionMonitor — used by manual-close endpoint
    pm_ref: Any = None        # PMClient — used by manual-close endpoint
    risk_ref: Any = None      # RiskEngine — used by manual-close endpoint


# Module-level singleton — main.py populates this
state = BotState()

# Path to trades CSV — use absolute path like risk.py to avoid CWD issues
TRADES_CSV = Path(__file__).parent / "data" / "trades.csv"
# Path to paper-fill log CSV written by fill_simulator.py
FILLS_CSV = Path(__file__).parent / "data" / "fills.csv"
# Path to persisted config overrides — survives bot restarts
_OVERRIDES_FILE = Path(__file__).parent / "config_overrides.json"


def _save_overrides() -> None:
    """Write all mutable config values to disk so they survive a restart."""
    snapshot: dict = {attr: getattr(config, attr) for _, (attr, _) in _MUTABLE_CONFIG.items()}
    # Also persist BOT_ACTIVE (toggled separately via /bot)
    snapshot["BOT_ACTIVE"] = config.BOT_ACTIVE
    # Persist list-type config values separately (not in _MUTABLE_CONFIG)
    snapshot["MAKER_EXCLUDED_MARKET_TYPES"] = list(config.MAKER_EXCLUDED_MARKET_TYPES)
    try:
        _OVERRIDES_FILE.write_text(json.dumps(snapshot, indent=2))
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
    "scan_interval":              ("SCAN_INTERVAL",              int),
    "strategy_mispricing":        ("STRATEGY_MISPRICING_ENABLED", bool),
    "strategy_maker":             ("STRATEGY_MAKER_ENABLED",      bool),
    "fill_check_interval":        ("FILL_CHECK_INTERVAL",         int),
    "paper_fill_probability":     ("PAPER_FILL_PROBABILITY",      float),
    "max_buy_no_yes_price":       ("MAX_BUY_NO_YES_PRICE",        float),
    "market_cooldown_seconds":    ("MARKET_COOLDOWN_SECONDS",     int),
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
    "min_edge_pct":                ("MIN_EDGE_PCT",                 float),
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
}


class ConfigPatch(BaseModel):
    paper_trading: bool | None = None
    agent_auto: bool | None = None
    auto_approve: bool | None = None
    scan_interval: int | None = None
    strategy_mispricing: bool | None = None
    strategy_maker: bool | None = None
    fill_check_interval: int | None = None
    paper_fill_probability: float | None = None
    max_buy_no_yes_price: float | None = None
    market_cooldown_seconds: int | None = None
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
    min_edge_pct: float | None = None
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
    # Market type exclusion (list — handled separately in patch_config)
    maker_excluded_market_types: list[str] | None = None


@app.get("/config")
def get_config() -> dict:
    """Return all mutable runtime config values."""
    return {
        "paper_trading":        config.PAPER_TRADING,
        "agent_auto":           config.AGENT_AUTO,
        "auto_approve":         config.AUTO_APPROVE,
        "scan_interval":        config.SCAN_INTERVAL,
        "strategy_mispricing":  config.STRATEGY_MISPRICING_ENABLED,
        "strategy_maker":       config.STRATEGY_MAKER_ENABLED,
        "fill_check_interval":  config.FILL_CHECK_INTERVAL,
        "paper_fill_probability": config.PAPER_FILL_PROBABILITY,
        "max_buy_no_yes_price": config.MAX_BUY_NO_YES_PRICE,
        "market_cooldown_seconds": config.MARKET_COOLDOWN_SECONDS,
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
        "min_edge_pct":           config.MIN_EDGE_PCT,
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
        # Market type exclusion
        "maker_excluded_market_types": list(config.MAKER_EXCLUDED_MARKET_TYPES),
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
    if updated:
        _save_overrides()
    return {
        "updated": updated,
        "current": {
            "paper_trading":       config.PAPER_TRADING,
            "agent_auto":          config.AGENT_AUTO,
            "auto_approve":        config.AUTO_APPROVE,
            "scan_interval":       config.SCAN_INTERVAL,
            "strategy_mispricing": config.STRATEGY_MISPRICING_ENABLED,
            "strategy_maker":      config.STRATEGY_MAKER_ENABLED,
            "fill_check_interval": config.FILL_CHECK_INTERVAL,
            "paper_fill_probability": config.PAPER_FILL_PROBABILITY,
            "max_buy_no_yes_price": config.MAX_BUY_NO_YES_PRICE,
            "market_cooldown_seconds": config.MARKET_COOLDOWN_SECONDS,
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
            "min_edge_pct":           config.MIN_EDGE_PCT,
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
            # Market type exclusion
            "maker_excluded_market_types": list(config.MAKER_EXCLUDED_MARKET_TYPES),
        },
        "timestamp": time.time(),
    }


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
    _save_overrides()
    return {
        "active": config.BOT_ACTIVE,
        "timestamp": time.time(),
    }


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


@app.post("/positions/{market_id}/close", dependencies=[Depends(require_auth)])
async def close_position_manually(market_id: str) -> dict:
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

    market = pm.get_markets().get(market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found in cache")

    book = pm.get_book(market.token_id_yes)
    total_pnl = 0.0
    exit_prices: dict[str, float] = {}

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
        rows = [r for r in rows if r.get("underlying", "").upper() == underlying.upper()]

    total = len(rows)
    page = rows[offset: offset + limit]
    return {
        "trades": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "timestamp": time.time(),
    }


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
        if book_ts is None:
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
            return list(reader)
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
