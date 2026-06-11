"""
monitor.py — Position exit monitor.

MOMENTUM STRATEGY — core logic (do not lose sight of this):
============================================================
1. ENTER NEAR EXPIRY (intentional, not a bug)
   Entry gate places us in the last seconds/minutes of the bucket.  At low TTE
   the remaining spot vol (sigma_tau) is very small, making adverse moves
   unlikely.  This IS the edge.  Do not add TTE floors /Kelly nerfs to fight it.

2. EXIT DRIVEN BY ORACLE SPOT, not CLOB alone
   • Delta SL  (MOMENTUM_DELTA_STOP_LOSS_PCT): exit when live oracle spot
     retreats to within SL_PCT% of the strike — fires on every RTDS/Chainlink
     tick, not on a polling loop.
   • Near-expiry delta cross (MOMENTUM_NEAR_EXPIRY): exit when TTE is very
     short AND spot has already crossed the strike (delta < 0).
   • Take-profit (MOMENTUM_TAKE_PROFIT): CLOB mid ≥ 0.999 (token converges to 1).
   • NEVER use CLOB alone for stop-losses: CLOB reprices forward and can drop
     on noise even when spot is stable and the position resolves as a WIN.

3. ORACLE ROUTING
   — bucket_5m, bucket_15m, bucket_4h, non-HYPE  → ChainlinkWSClient (AggregatorV3 eth_subscribe, event-driven)
   — bucket_5m, bucket_15m, bucket_4h, HYPE       → freshest of ChainlinkStreamsClient + RTDS chainlink relay
   — bucket_1h, bucket_daily, bucket_weekly        → RTDS exchange-aggregated ticks
   Oracle routing is handled by SpotOracle facade.  NOT from HL price feed.

4. CLOB vs ORACLE PRICES — never confuse these
   • current_token_price  = PM CLOB mid of the HELD token (YES or NO side)
   • current_spot         = oracle price of the underlying asset
   The delta SL compares current_spot vs pos.strike.
   Take-profit compares current_token_price vs MOMENTUM_TAKE_PROFIT.

5. KELLY IS CORRECT AT LOW TTE
   Near expiry sigma_tau → 0, so z = delta/sigma_tau is large → high win_prob
   → Kelly bets bigger.  This is correct: we are more certain when spot is well
   past strike with seconds left.  Do not floor sigma_tau for Kelly.

Strategy exit rules
-------------------
  * **maker**   — TIME_STOP for non-bucket (milestone/daily) markets within
                  MAKER_EXIT_HOURS of expiry.  Bucket positions hold to
                  RESOLVED.  No per-position profit target or stop-loss.
  * **momentum** — Oracle delta SL + CLOB take-profit (see above).
  * **mispricing** — EXIT_DAYS_BEFORE_RESOLUTION time-stop, PROFIT_TARGET and
                  STOP_LOSS in USD P&L space.
  * **unknown** / any other label — NO triggers at all.  The position is held
                  until the market resolves.

Global action (all strategies)
-------------------------------
  RESOLVED: once ``now >= market.end_date`` the position is recorded as closed
  in the risk engine (price snapped to 0.0 or 1.0).  The ``_auto_redeem_loop``
  background task then polls the PM wallet for ``redeemable=True`` and submits
  the on-chain CTF redemption transaction when the oracle is ready.

Usage:
    monitor = PositionMonitor(pm, risk_engine)
    monitor.record_entry_deviation(market_id, signal.deviation)
    asyncio.create_task(monitor.start())
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import config
from core.types import GateResult, log_gate_suppression, PositionDataHealth, _should_log_suppression as _should_log_count
from logger import get_bot_logger
from risk import RiskEngine, Position
from market_data.pm_client import PMClient, _MARKET_TYPE_DURATION_SECS
from market_data.rtds_client import RTDSClient
from market_data.spot_oracle import SpotOracle
from market_data.oracle_tick_tracker import OracleTickTracker
from ctf_utils import _redeem_ctf_via_safe
from py_clob_client_v2.config import get_contract_config as _get_contract_config
_POLY_CONTRACTS = _get_contract_config(137)  # Polygon mainnet — CTF + collateral addresses
from strategies.Momentum.event_log import emit as _emit_event, write_position_snapshot as _write_position_snapshot, write_exit_snapshot as _write_exit_snapshot

log = get_bot_logger(__name__)


# ── Exit reason constants ─────────────────────────────────────────────────────

class ExitReason:
    PROFIT_TARGET          = "profit_target"
    STOP_LOSS              = "stop_loss"
    TIME_STOP              = "time_stop"
    RESOLVED               = "resolved"
    COIN_LOSS_LIMIT        = "coin_loss_limit"       # maker: aggregate coin P&L exceeded threshold
    MOMENTUM_STOP_LOSS          = "momentum_stop_loss"          # momentum: oracle delta dropped below delta SL threshold
    MOMENTUM_TAKE_PROFIT        = "momentum_take_profit"        # momentum: held token rose above MOMENTUM_TAKE_PROFIT
    MOMENTUM_NEAR_EXPIRY        = "momentum_near_expiry"        # momentum: near expiry and spot has crossed strike
    MOMENTUM_UPFRAC_EXIT        = "upfrac_exit"                 # momentum: oracle tick up-fraction EWMA below threshold for N windows (M-13)
    MOMENTUM_PROB_STOP_LOSS     = "prob_sl"                     # momentum: CLOB token-price dropped below prob-SL threshold
    MOMENTUM_HL_MARK_SL         = "hl_mark_sl"                  # momentum: HL perp mark crossed strike before Chainlink oracle
    MOMENTUM_HL_DEPTH_SL        = "hl_depth_sl"                 # momentum: HL perp book heavily positioned against trade
    MOMENTUM_ORACLE_STALE_SL    = "oracle_stale_sl"              # momentum: oracle stale — exiting blind hold
    LOSER_EXIT                  = "loser_exit"                  # opening_neutral: loser bid-monitor trigger fired



# ── Market outcome persistence ─────────────────────────────────────────────────
# market_outcomes.json:    { condition_id: { resolved_yes_price: float } }
# pending_resolutions.json: [ { condition_id, end_date_iso, checked } ]

_DATA_DIR               = Path(__file__).parent / "data"
_MARKET_OUTCOMES_PATH   = _DATA_DIR / "market_outcomes.json"
_PENDING_RESOLUTIONS_PATH = _DATA_DIR / "pending_resolutions.json"

# ── Momentum tick CSV ─────────────────────────────────────────────────────────
# Dedicated file for every intra-hold price check on open momentum positions.
# Separate from bot.log so it can be analysed in isolation without grepping
# through unrelated log noise.  Used to calibrate combined exit stop thresholds.
#
# Columns:
#   ts            — UTC ISO timestamp of this tick
#   market_id     — first 20 chars of condition_id
#   market_title  — human label (truncated)
#   underlying    — BTC / ETH / SOL / …
#   side          — UP / DOWN / YES / NO
#   tte_s         — seconds until market expiry at this tick
#   entry_tok     — token price at entry
#   token         — current CLOB mid of the held token
#   tok_drop_pct  — (entry_tok - token) / entry_tok * 100  (+ve = token losing value)
#   entry_spot    — oracle spot price at entry (pos.spot_price)
#   spot          — current oracle spot price
#   entry_delta   — (entry_spot - strike) / strike * 100 for UP; reversed for DOWN
#   current_delta — live delta at this tick
#   delta_retreat_pct — (entry_delta - current_delta) / entry_delta * 100
#   exit          — True if this tick triggered an exit
#   reason        — exit reason label or empty
#   hl_mark_price       — HL perp mark price at this tick (Signal A)
#   hl_mark_div_pct     — (hl_mark - strike) / strike * 100; negative = mark below strike for UP
#   hl_depth_imbalance  — position-adjusted HL book imbalance: negative = market against trade (Signal B)

MOMENTUM_TICKS_CSV   = _DATA_DIR / "momentum_ticks.csv"
_MOMENTUM_TICKS_HEADER = [
    "ts", "market_id", "market_title", "underlying", "side",
    "tte_s", "entry_tok", "token", "tok_drop_pct",
    "entry_spot", "spot", "entry_delta", "current_delta", "delta_retreat_pct",
    "exit", "reason",
    "hl_mark_price", "hl_mark_div_pct", "hl_depth_imbalance",
]

def _ensure_momentum_ticks_csv() -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    if not MOMENTUM_TICKS_CSV.exists():
        with MOMENTUM_TICKS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_MOMENTUM_TICKS_HEADER).writeheader()

def _write_momentum_tick(
    pos: "Position",
    tte_seconds: Optional[float],
    current_token_price: Optional[float],
    current_spot: Optional[float],
    current_delta_pct: Optional[float],
    exit_flag: bool,
    reason: str,
    hl_mark_price: Optional[float] = None,
    hl_position_imbalance: Optional[float] = None,
) -> None:
    """Append one row to momentum_ticks.csv."""
    try:
        _ensure_momentum_ticks_csv()
        entry_delta: Optional[float] = None
        if pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                entry_delta = round((pos.spot_price - pos.strike) / pos.strike * 100, 6)
            elif pos.spot_price > pos.strike:
                # Dip-market NO/DOWN: winning direction is spot > strike.
                entry_delta = round((pos.spot_price - pos.strike) / pos.strike * 100, 6)
            else:
                # Reach-market NO/DOWN: winning direction is spot < strike.
                entry_delta = round((pos.strike - pos.spot_price) / pos.strike * 100, 6)
        tok_drop = (
            round((pos.entry_price - current_token_price) / pos.entry_price * 100, 4)
            if current_token_price is not None and pos.entry_price > 0 else None
        )
        delta_retreat = (
            round((entry_delta - current_delta_pct) / entry_delta * 100, 4)
            if entry_delta is not None and entry_delta != 0 and current_delta_pct is not None else None
        )
        # HL mark divergence pct (Signal A): position-direction-adjusted
        hl_mark_div: Optional[float] = None
        if hl_mark_price is not None and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                hl_mark_div = round((hl_mark_price - pos.strike) / pos.strike * 100, 6)
            else:
                hl_mark_div = round((pos.strike - hl_mark_price) / pos.strike * 100, 6)
        row = {
            "ts":               datetime.now(timezone.utc).isoformat(),
            "market_id":        pos.market_id,
            "market_title":     pos.market_title[:60],
            "underlying":       pos.underlying,
            "side":             pos.side,
            "tte_s":            round(tte_seconds, 2) if tte_seconds is not None else "",
            "entry_tok":        pos.entry_price,
            "token":            current_token_price if current_token_price is not None else "",
            "tok_drop_pct":     tok_drop if tok_drop is not None else "",
            "entry_spot":       pos.spot_price,
            "spot":             current_spot if current_spot is not None else "",
            "entry_delta":      entry_delta if entry_delta is not None else "",
            "current_delta":    round(current_delta_pct, 6) if current_delta_pct is not None else "",
            "delta_retreat_pct": delta_retreat if delta_retreat is not None else "",
            "exit":             exit_flag,
            "reason":           reason,
            "hl_mark_price":    round(hl_mark_price, 4) if hl_mark_price is not None else "",
            "hl_mark_div_pct":  round(hl_mark_div, 6) if hl_mark_div is not None else "",
            "hl_depth_imbalance": round(hl_position_imbalance, 6) if hl_position_imbalance is not None else "",
        }
        with MOMENTUM_TICKS_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_MOMENTUM_TICKS_HEADER).writerow(row)
    except Exception as _ex:
        log.debug("_write_momentum_tick failed", exc=str(_ex))  # never let tick logging crash the monitor


def _load_market_outcomes() -> dict:
    """Return { condition_id: { resolved_yes_price: float } }."""
    try:
        if _MARKET_OUTCOMES_PATH.exists():
            return json.loads(_MARKET_OUTCOMES_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_market_outcomes(outcomes: dict) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _MARKET_OUTCOMES_PATH.write_text(
        json.dumps(outcomes, indent=2), encoding="utf-8"
    )


def _record_market_outcome(condition_id: str, resolved_yes_price: float) -> None:
    """Write a resolved market price to market_outcomes.json."""
    outcomes = _load_market_outcomes()
    outcomes[condition_id] = {"resolved_yes_price": resolved_yes_price}
    _save_market_outcomes(outcomes)


def _load_pending_resolutions() -> list:
    """Return list of { condition_id, end_date_iso, checked }."""
    try:
        if _PENDING_RESOLUTIONS_PATH.exists():
            return json.loads(_PENDING_RESOLUTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_pending_resolutions(pending: list) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _PENDING_RESOLUTIONS_PATH.write_text(
        json.dumps(pending, indent=2), encoding="utf-8"
    )


def _add_pending_resolution(
    condition_id: str,
    end_date: Optional[datetime],
    *,
    force: bool = False,
    underlying: str = "",
    market_slug: str = "",
    market_type: str = "",
) -> None:
    """Track a market for retroactive resolution verification.

    force=False (default, taker/stop exits):
        resolved_outcome is not yet set; _check_pending_resolutions fills it in.
    force=True (RESOLVED exits):
        An outcome was already written (possibly from an oracle fallback that may
        have been wrong).  _check_pending_resolutions will re-verify against the
        CLOB API and call patch_trade_outcome(force=True) to correct if needed.
        underlying/market_slug/market_type are stored so _check_pending_resolutions
        can retroactively fetch the Chainlink settlement spot price once the Gamma
        API has it (typically available a few minutes after market close).
    """
    if not condition_id:
        return
    # Coerce metadata args to plain str — callers may pass objects (e.g. MagicMock
    # in tests) due to getattr on arbitrary market objects.
    underlying  = underlying  if isinstance(underlying,   str) else ""
    market_slug = market_slug if isinstance(market_slug,  str) else ""
    market_type = market_type if isinstance(market_type,  str) else ""
    pending = _load_pending_resolutions()
    existing = next((e for e in pending if e["condition_id"] == condition_id), None)
    if existing is not None:
        # Upgrade an existing non-force entry to force if requested.
        if force and not existing.get("force"):
            existing["force"] = True
            existing["checked"] = False
            # Also store market metadata for spot retry if not already present.
            if underlying and not existing.get("underlying"):
                existing["underlying"] = underlying
            if market_slug and not existing.get("market_slug"):
                existing["market_slug"] = market_slug
            if market_type and not existing.get("market_type"):
                existing["market_type"] = market_type
            _save_pending_resolutions(pending)
        return
    end_date_iso = end_date.isoformat() if isinstance(end_date, datetime) else ""
    pending.append({
        "condition_id": condition_id,
        "end_date_iso": end_date_iso,
        "checked": False,
        "force": force,
        "underlying": underlying,
        "market_slug": market_slug,
        "market_type": market_type,
    })
    _save_pending_resolutions(pending)


# ── Pure helpers (easily unit-tested) ────────────────────────────────────────

def compute_unrealised_pnl(pos: Position, current_price: float) -> float:
    """
    Unrealised P&L in USD for an open position.

    current_price is the actual mid price of the HELD token from its own
    CLOB book (YES mid for YES positions, NO mid for NO positions).
    Both sides use the same formula: profit when price rises above entry.
    """
    return (current_price - pos.entry_price) * pos.size


def should_exit(
    pos: Position,
    current_price: float,
    initial_deviation: float,
    market_end_date: Optional[datetime],
    now: Optional[datetime] = None,
    current_token_price: Optional[float] = None,
    tte_seconds: Optional[float] = None,
    current_spot: Optional[float] = None,
    delta_sl_pct: Optional[float] = None,
    oracle_age_seconds: Optional[float] = None,
    suppress_counts: Optional[dict] = None,
    hl_mark_price: Optional[float] = None,
    hl_position_imbalance: Optional[float] = None,
) -> tuple[bool, str, float]:
    """
    Decide whether a position should be exited.

    Strategy dispatch is **explicit**: each recognised strategy has its own
    exit block with an explicit ``return`` so there is no unintended fall-
    through between strategies.  The rules are:

      * **Global (all strategies)**: RESOLVED fires once ``now >= end_date``.
        This is also checked as a fast-path in ``_check_position`` before this
        function is reached, so the check here mainly serves unit tests.
      * **unknown** (position restored without a strategy label): no triggers
        at all — the position is held until the market resolves, then the
        auto-redeem loop handles on-chain redemption.
      * **maker**: TIME_STOP fires for non-bucket (milestone/daily) markets
        within MAKER_EXIT_HOURS of expiry.  Bucket positions are held to
        RESOLVED.  No per-position profit target or stop-loss.
      * **momentum**: token-price exits evaluated against the **held token's
        own CLOB mid** (``current_token_price``), not a price derived from the
        opposite side.  MOMENTUM_STOP_LOSS fires when the token falls below
        threshold; MOMENTUM_TAKE_PROFIT fires near certainty; MOMENTUM_NEAR_EXPIRY
        fires when TTE is very short and the position is in loss territory to
        avoid a binary snap to zero.
      * **mispricing**: EXIT_DAYS_BEFORE_RESOLUTION time-stop, plus per-position
        PROFIT_TARGET and STOP_LOSS in USD P&L space.
      * **any other label**: treated as unknown — no triggers, hold to RESOLVED.

    Args:
        pos:                  Open position to evaluate.
        current_price:        Current mid price of the YES token (YES-space; used
                              for P&L calculation and non-momentum exits).
        initial_deviation:    |PM price - implied prob| at entry (always > 0).
        market_end_date:      When the market resolves (UTC). None = unknown.
        now:                  Override for current time (for testing).
        current_token_price:  Actual mid price of the HELD token from its own
                              CLOB book.  For YES positions this equals
                              ``current_price``; for NO positions it is the
                              NO CLOB mid, not ``1.0 - current_price``.  When
                              None the function falls back to the derived value.
        tte_seconds:          Seconds until market resolution.  Required for the
                              near-expiry stop.  ``None`` disables that check.

    Returns:
        (should_exit, reason, unrealised_pnl_usd)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Minimum hold time: applies to maker and mispricing only.
    # Momentum is event-driven — exits fire immediately on WS ticks; no hold
    # floor is applied so the delta SL can fire the instant spot crosses the strike.
    if pos.strategy != "momentum":
        if (now - pos.opened_at).total_seconds() < config.MIN_HOLD_SECONDS:
            return False, "", 0.0

    unrealised = compute_unrealised_pnl(pos, current_price)

    # ── Global: resolved stop (market past end_date) ──────────────────────────
    if market_end_date is not None and now >= market_end_date:
        # Return 0.0 P&L: the position is fully settled at oracle price;
        # any stale mid-market unrealised figure would be misleading in logs.
        return True, ExitReason.RESOLVED, 0.0

    # ── Unknown strategy: no triggers — hold to RESOLVED only ────────────────
    # Positions restored from wallet without a saved strategy label are tagged
    # "unknown".  We never force-exit them; the auto-redeem loop handles on-chain
    # redemption once the market settles.
    if pos.strategy == "unknown":
        return False, "", unrealised

    # ── Time-to-expiry (computed once; used by maker + mispricing) ────────────
    days_to_expiry = (
        (market_end_date - now).total_seconds() / 86_400
        if market_end_date is not None
        else float("inf")
    )

    # ── Maker exits ───────────────────────────────────────────────────────────
    if pos.strategy == "maker":
        # Bucket markets (5m, 15m, …) have a full lifespan shorter than
        # MAKER_EXIT_HOURS so the hours-based gate would fire immediately for
        # every bucket fill.  Bucket positions are held to RESOLVED via free
        # settlement — never force-exited via taker.
        is_bucket = pos.market_type in _MARKET_TYPE_DURATION_SECS
        if not is_bucket:
            maker_exit_days = config.MAKER_EXIT_HOURS / 24
            if maker_exit_days > 0 and days_to_expiry <= maker_exit_days:
                return True, ExitReason.TIME_STOP, unrealised
        return False, "", unrealised

    # ── Momentum exits — delta-based (live spot vs strike) ─────────────────────
    if pos.strategy == "momentum":
        # ── High-probability suppression ─────────────────────────────────────
        # When the held token is at/above MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE
        # (default 0.90) the position is deeply in-the-money: the crowd has
        # priced a ~90c+ win.  Exiting via taker forfeits expected value.
        # Skip ALL stop-loss exits (delta SL, near-expiry, prob SL, upfrac).
        # Take-profit (→ 0.999) is never suppressed.
        _suppress_above = getattr(config, "MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE", 0.0)
        _token_price_for_suppress = (
            current_token_price if current_token_price is not None else current_price
        )
        _suppress_taker_exits = (
            _suppress_above > 0.0
            and _token_price_for_suppress >= _suppress_above
        )
        if _suppress_taker_exits and suppress_counts is not None:
            _gk = f"{pos.token_id}:suppress_taker_exits"
            _cnt = suppress_counts.get(_gk, 0) + 1
            suppress_counts[_gk] = _cnt
            if _cnt >= config.GATE_LOG_CONSECUTIVE_THRESHOLD and _should_log_count(_cnt, config.GATE_LOG_CONSECUTIVE_THRESHOLD):
                log.warning(
                    "gate_suppressed",
                    strategy="momentum",
                    gate="suppress_taker_exits",
                    entity_id=pos.market_id[:16],
                    consecutive=_cnt,
                    reason=f"token_price={_token_price_for_suppress:.4f} >= suppress_above={_suppress_above}",
                    value=round(_token_price_for_suppress, 4),
                    threshold=_suppress_above,
                )
        elif not _suppress_taker_exits and suppress_counts is not None:
            suppress_counts.pop(f"{pos.token_id}:suppress_taker_exits", None)

        # Delta SL runs FIRST — requires only spot+strike, NOT token_price.
        # This must evaluate even when the NO CLOB book is drained near expiry
        # (book drain sets current_token_price=None for NO positions, which
        # previously blocked this block entirely — causing missed stop-losses).
        _oracle_delta_pct: Optional[float] = None  # captured below for prob-SL gate
        if current_spot is not None and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                # Long YES/UP: profit when spot > strike.  Delta positive when in-the-money.
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            elif pos.spot_price > pos.strike and pos.market_type not in _MARKET_TYPE_DURATION_SECS:
                # Long NO/DOWN on a "dip" milestone market (e.g. "Will ETH dip to $X?"):
                # NO wins when spot STAYS ABOVE the strike.
                # Entry spot was above strike → winning delta = (spot - strike) / strike.
                # pos.spot_price defaults to 0.0 so this branch only activates for live
                # dip-market positions where the scanner stored the actual entry spot.
                # NOTE: bucket markets (5m/15m/4h/1h/daily/weekly) are EXCLUDED here — they
                # are always UP/DOWN format where YES=above-strike and NO=below-strike.
                # In a bucket market, pos.spot_price can be fractionally above the strike
                # (tiny sub-second timing window) while NO still means "ends below strike".
                # Applying the dip-market formula in that case inverts the delta sign and
                # silently disables delta SL for the entire bucket (observed HYPE 9:05AM loss).
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            else:
                # Long NO/DOWN on a reach / bucket UP-DOWN market:
                # NO wins when spot STAYS BELOW the strike.
                # — Reach milestone: "Will ETH reach $X?" — entry spot at or below strike.
                # — All bucket markets: YES=above-strike, NO=below-strike (always this branch).
                current_delta_pct = (pos.strike - current_spot) / pos.strike * 100
            # PROTECTIVE BUFFER: fire when delta drops below +SL_PCT, i.e. while
            # still in-the-money but within SL_PCT% of the strike.  With
            # SL_PCT=0.1 the bot exits when still 0.1% ahead, before the oracle
            # crosses and Polymarket resolves against the position.
            _sl = delta_sl_pct if delta_sl_pct is not None else config.MOMENTUM_DELTA_STOP_LOSS_PCT
            # WINNER-fill-type positions (ON-promoted) use a looser SL threshold to avoid
            # false stops on mid-bucket oscillation.  MOMENTUM_WINNER_DELTA_SL_MULTIPLIER
            # is applied to the per-coin threshold (e.g. 0.5 → BTC 1% → 0.5%).
            _is_winner = getattr(pos, "fill_type", "MAIN") == "WINNER"
            if _is_winner:
                _winner_mult = getattr(config, "MOMENTUM_WINNER_DELTA_SL_MULTIPLIER", 1.0)
                _sl = _sl * _winner_mult
            _oracle_delta_pct = current_delta_pct  # capture for prob-SL gate below
            # Suppress delta SL while the position is in the post-open grace window
            # OR while TTE is still above the entry threshold — whichever ends first.
            # Using purely TTE-based suppression created a blind spot of up to 400s
            # for ON-promoted positions (entry at TTE=520s kept delta SL disabled
            # until TTE=120s).  The grace cap ensures that even a late-bucket
            # promotion is only unprotected for at most MOMENTUM_DELTA_SL_GRACE_SECS
            # seconds, while still avoiding false fires at the moment of promotion
            # when spot is barely across the strike.
            # Example (5m, promotion at TTE=170s, min_tte=120s):
            #   Old: suppressed 50s (until TTE=120s)
            #   New: suppressed until min(60s elapsed, TTE<120s) = 50s — same here.
            # Example (15m, promotion at TTE=520s, min_tte=180s):
            #   Old: suppressed 340s (until TTE=180s) — 340s blind spot.
            #   New: suppressed 60s — blind spot capped at 60s.
            _min_tte = config.MOMENTUM_MIN_TTE_SECONDS.get(
                pos.market_type, config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
            ) if isinstance(config.MOMENTUM_MIN_TTE_SECONDS, dict) else config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
            _delta_sl_grace_secs = (
                getattr(config, "MOMENTUM_WINNER_DELTA_SL_GRACE_SECS",
                        getattr(config, "MOMENTUM_DELTA_SL_GRACE_SECS", 60))
                if _is_winner
                else getattr(config, "MOMENTUM_DELTA_SL_GRACE_SECS", 60)
            )
            _in_grace = (now - pos.opened_at).total_seconds() < _delta_sl_grace_secs
            _above_min_tte = tte_seconds is not None and tte_seconds >= _min_tte
            # WINNER (ON-promoted): require BOTH age < grace AND TTE still above entry
            # threshold — prevents a late-promoted position from holding an infinite
            # blind spot until TTE drops below min_tte (up to 340s for 15m buckets).
            # MAIN: age-only grace — _above_min_tte is always False for bucket_5m MAIN
            # because entry requires TTE ≤ min_tte, so the AND gate made grace a no-op.
            if _is_winner:
                _suppress_delta_sl = _in_grace and _above_min_tte
            else:
                _suppress_delta_sl = _in_grace
            # Token price veto: if the CLOB token mid is still above the veto floor,
            # the oracle retreat is likely a transient noise tick (crowd not repricing).
            # Only applied when current_token_price is available; floor=0.0 disables.
            _token_veto_floor = getattr(config, "MOMENTUM_DELTA_SL_TOKEN_VETO_FLOOR", 0.0)
            if (
                not _suppress_delta_sl
                and _token_veto_floor > 0.0
                and current_token_price is not None
                and current_token_price > _token_veto_floor
            ):
                _suppress_delta_sl = True
            # S1: gate audit logging for delta_sl suppress
            if _suppress_delta_sl and suppress_counts is not None:
                _gk_dsl = f"{pos.token_id}:suppress_delta_sl"
                _cnt_dsl = suppress_counts.get(_gk_dsl, 0) + 1
                suppress_counts[_gk_dsl] = _cnt_dsl
                if _cnt_dsl >= config.GATE_LOG_CONSECUTIVE_THRESHOLD and _should_log_count(_cnt_dsl, config.GATE_LOG_CONSECUTIVE_THRESHOLD):
                    _reason_dsl = (
                        f"token_veto: token_price={current_token_price:.4f} > floor={_token_veto_floor}"
                        if (_token_veto_floor > 0.0 and current_token_price is not None
                            and current_token_price > _token_veto_floor and not (_in_grace and _above_min_tte))
                        else f"grace: age={(now - pos.opened_at).total_seconds():.0f}s < {_delta_sl_grace_secs}s, tte_ok={_above_min_tte}"
                    )
                    log.warning(
                        "gate_suppressed",
                        strategy="momentum",
                        gate="suppress_delta_sl",
                        entity_id=pos.market_id[:16],
                        consecutive=_cnt_dsl,
                        reason=_reason_dsl,
                        delta_pct=round(current_delta_pct, 4) if current_delta_pct is not None else None,
                        sl_threshold=round(_sl, 4),
                    )
            elif not _suppress_delta_sl and suppress_counts is not None:
                suppress_counts.pop(f"{pos.token_id}:suppress_delta_sl", None)
            if not _suppress_taker_exits and not _suppress_delta_sl and current_delta_pct < _sl:
                return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised
            # Near-expiry: only exit if spot has already crossed the strike
            # (delta < 0).  Avoids premature exits from CLOB price collapse.
            # Within MOMENTUM_NEAR_EXPIRY_SUPPRESS_BYPASS_TTE seconds of expiry
            # the suppress gate is overridden — at this range even a high-priced
            # token can collapse terminally before the suppress can lift.
            _near_expiry_bypass_tte = getattr(config, "MOMENTUM_NEAR_EXPIRY_SUPPRESS_BYPASS_TTE", 0)
            _near_expiry_suppress_active = (
                _suppress_taker_exits
                and not (
                    _near_expiry_bypass_tte > 0
                    and tte_seconds is not None
                    and tte_seconds < _near_expiry_bypass_tte
                )
            )
            if (
                not _near_expiry_suppress_active
                and tte_seconds is not None
                and tte_seconds < config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
                and current_delta_pct < 0
            ):
                # Hysteresis: require N consecutive ticks to filter transient oracle
                # noise.  Analysis: false positives had median 2 negative-delta ticks;
                # the genuine save had 112.  Threshold from config (default 3).
                _ne_min_ticks = getattr(config, "MOMENTUM_NEAR_EXPIRY_MIN_CONSECUTIVE_TICKS", 3)
                _ne_key = f"{pos.token_id}:ne_neg_delta"
                if suppress_counts is not None:
                    _ne_cnt = suppress_counts.get(_ne_key, 0) + 1
                    suppress_counts[_ne_key] = _ne_cnt
                else:
                    _ne_cnt = _ne_min_ticks  # no state dict → fire immediately (safe default)
                if _ne_cnt >= _ne_min_ticks:
                    return True, ExitReason.MOMENTUM_NEAR_EXPIRY, unrealised
            else:
                # Reset counter whenever condition is not met (delta recovered or TTE out of window)
                if suppress_counts is not None:
                    suppress_counts.pop(f"{pos.token_id}:ne_neg_delta", None)

            # S2.4 — Near-expiry hard exit on stale oracle: if we are near expiry
            # AND the oracle has been silent for longer than the configured threshold,
            # exit rather than holding blind to resolution.  Covers both a completely
            # dead oracle (spot=None) and a frozen-cache oracle (spot is the stale
            # cached price replayed without a live push).
            _hard_exit_stale_secs = getattr(config, "ORACLE_STALE_NEAR_EXPIRY_HARD_EXIT_SECS", 0)
            if (
                not _suppress_taker_exits
                and _hard_exit_stale_secs > 0
                and tte_seconds is not None
                and tte_seconds < config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
                and oracle_age_seconds is not None
                and oracle_age_seconds >= _hard_exit_stale_secs
            ):
                log.warning(
                    "Monitor: near-expiry hard exit — oracle stale",
                    market_id=pos.market_id,
                    tte_seconds=round(tte_seconds, 1),
                    oracle_stale_secs=round(oracle_age_seconds, 1),
                    threshold=_hard_exit_stale_secs,
                    spot_available=current_spot is not None,
                )
                return True, ExitReason.MOMENTUM_NEAR_EXPIRY, unrealised

            # S2.5 — Mid-hold stale oracle exit: if the oracle has been silent
            # beyond ORACLE_STALE_MID_HOLD_EXIT_SECS (default 120 s) at any TTE,
            # exit rather than holding blind.  Bypasses the winner-suppress gate
            # because a stale oracle cannot confirm the position is still winning.
            _mid_hold_stale_secs = getattr(config, "ORACLE_STALE_MID_HOLD_EXIT_SECS", 0)
            if (
                _mid_hold_stale_secs > 0
                and oracle_age_seconds is not None
                and oracle_age_seconds >= _mid_hold_stale_secs
            ):
                log.warning(
                    "Monitor: mid-hold exit — oracle stale",
                    market_id=pos.market_id,
                    tte_seconds=round(tte_seconds, 1) if tte_seconds is not None else None,
                    oracle_stale_secs=round(oracle_age_seconds, 1),
                    threshold=_mid_hold_stale_secs,
                    spot_available=current_spot is not None,
                )
                return True, ExitReason.MOMENTUM_ORACLE_STALE_SL, unrealised

        # Token-price exits (take-profit + prob-based SL) require the held token's CLOB mid.
        if pos.side in ("YES", "BUY_YES", "UP"):
            # Use actual YES/UP CLOB mid; fall back to current_price.
            token_price = current_token_price if current_token_price is not None else current_price
        else:
            # Use actual NO/DOWN CLOB mid.  Do NOT derive from the YES/UP side —
            # both token CLOBs are independent and 1-p_yes ≠ p_no in general.
            # If the NO/DOWN book is unavailable, skip token-price exits this tick.
            if current_token_price is None:
                return False, "", unrealised
            token_price = current_token_price
        # Recompute unrealised against the actual held-token price.
        unrealised = compute_unrealised_pnl(pos, token_price)

        # Probability-based SL (Item 7): fires when the CLOB token price has
        # dropped more than MOMENTUM_PROB_SL_PCT below the entry price.
        # This is a secondary failsafe complementing the oracle-delta SL.
        # Uses the same MOMENTUM_DELTA_SL_MIN_TICKS hysteresis to avoid single
        # CLOB-noise triggers.
        # Near-expiry guard: in the final MOMENTUM_PROB_SL_MIN_TTE_SECS seconds the
        # CLOB book drains — best_bid collapses to the tick floor while best_ask
        # stays near 1.0, pushing mid to ~0.50 regardless of oracle state.  Firing
        # the prob-SL on this artificial mid collapse exits winning positions at the
        # book floor (0.01).  The oracle-delta SL is the correct protection near
        # expiry; suppress prob-SL below the TTE guard.
        _prob_sl_tte_ok = (
            tte_seconds is None
            or config.MOMENTUM_PROB_SL_MIN_TTE_SECS <= 0
            or tte_seconds >= config.MOMENTUM_PROB_SL_MIN_TTE_SECS
        )
        # Oracle-lag gate: prob-SL's unique value is detecting oracle lag — the
        # CLOB is repricing a move the oracle hasn't reported yet.  If the oracle
        # ticked recently it's current: any CLOB drop is book-drain noise, not a
        # real signal.  Only fire prob-SL when oracle lag is CONFIRMED (last tick
        # was more than MOMENTUM_PROB_SL_ORACLE_STALE_SECS ago).  Unknown timing
        # (None) is treated as fresh — don't fire on missing data.
        _oracle_confirmed_stale = (
            oracle_age_seconds is not None  # must have timing to confirm lag
            and config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS > 0  # guard enabled
            and oracle_age_seconds > config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS
        )
        _prob_sl_oracle_ok = (
            config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS <= 0  # guard disabled → always allow
            or _oracle_confirmed_stale                       # confirmed oracle lag
        )
        # S1: log when prob_sl_oracle_ok is suppressing a would-be prob SL
        _would_fire_prob_sl = (
            not _suppress_taker_exits
            and _prob_sl_tte_ok
            and config.MOMENTUM_PROB_SL_ENABLED
            and pos.prob_sl_threshold > 0.0
            and token_price < pos.prob_sl_threshold
        )
        if _would_fire_prob_sl and not _prob_sl_oracle_ok and suppress_counts is not None:
            _gk_psl = f"{pos.token_id}:suppress_prob_sl_oracle"
            _cnt_psl = suppress_counts.get(_gk_psl, 0) + 1
            suppress_counts[_gk_psl] = _cnt_psl
            if _cnt_psl >= config.GATE_LOG_CONSECUTIVE_THRESHOLD and _should_log_count(_cnt_psl, config.GATE_LOG_CONSECUTIVE_THRESHOLD):
                log.warning(
                    "gate_suppressed",
                    strategy="momentum",
                    gate="suppress_prob_sl_oracle",
                    entity_id=pos.market_id[:16],
                    consecutive=_cnt_psl,
                    reason=f"oracle not confirmed stale: age={oracle_age_seconds}s threshold={config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS}s",
                    oracle_age_secs=oracle_age_seconds,
                    threshold=config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS,
                    token_price=round(token_price, 4),
                    prob_sl_threshold=round(pos.prob_sl_threshold, 4),
                )
        elif not _would_fire_prob_sl and suppress_counts is not None:
            suppress_counts.pop(f"{pos.token_id}:suppress_prob_sl_oracle", None)
        if (
            not _suppress_taker_exits
            and _prob_sl_oracle_ok
            and _prob_sl_tte_ok
            and config.MOMENTUM_PROB_SL_ENABLED
            and pos.prob_sl_threshold > 0.0
            and token_price < pos.prob_sl_threshold
        ):
            return True, ExitReason.MOMENTUM_PROB_STOP_LOSS, unrealised

        # Take-profit: still CLOB-based (converging to 1.0 at resolution).
        # Opening Neutral promoted positions carry a per-position TP price
        # (pos.take_profit_price > 0) that fires before the global 0.999 threshold.
        _tp_threshold = (
            pos.take_profit_price
            if pos.take_profit_price > 0.0
            else config.MOMENTUM_TAKE_PROFIT
        )
        if token_price >= _tp_threshold:
            return True, ExitReason.MOMENTUM_TAKE_PROFIT, unrealised

        # ── Early Warning SL: HL Mark Price Divergence (Signal A) ──────────
        # Fires when HL perp mark has crossed the strike while Chainlink has not.
        # TTE gate 30s (narrower than velocity — perp mark is a faster, cleaner signal).
        if (
            not _suppress_taker_exits
            and getattr(config, "MOMENTUM_HL_MARK_SL_ENABLED", False)
            and hl_mark_price is not None
            and pos.strike > 0
            and tte_seconds is not None
            and tte_seconds < getattr(config, "MOMENTUM_HL_MARK_SL_MAX_TTE", 30)
        ):
            _hl_threshold_pct = getattr(config, "MOMENTUM_HL_MARK_SL_THRESHOLD_PCT", 0.0)
            if pos.side in ("YES", "BUY_YES", "UP"):
                _hl_mark_div = (hl_mark_price - pos.strike) / pos.strike * 100
            else:
                _hl_mark_div = (pos.strike - hl_mark_price) / pos.strike * 100
            # Oracle ITM confirmation gate: if Chainlink oracle confirms the position
            # is solidly ITM (above the floor), trust Chainlink over HL perp mark
            # noise.  HL perpetual mark diverges from Chainlink at volatile moments
            # near expiry — the settlement oracle is Chainlink, not HL.
            _hl_mark_itm_floor = getattr(config, "MOMENTUM_HL_MARK_SL_ORACLE_ITM_FLOOR_PCT", 0.0)
            _hl_mark_oracle_suppressed = (
                _hl_mark_itm_floor > 0.0
                and _oracle_delta_pct is not None
                and _oracle_delta_pct > _hl_mark_itm_floor
            )
            if not _hl_mark_oracle_suppressed and _hl_mark_div < _hl_threshold_pct:
                log.warning(
                    "early_warning_sl",
                    signal="hl_mark",
                    market_id=pos.market_id[:20],
                    hl_mark=round(hl_mark_price, 4),
                    strike=pos.strike,
                    divergence_pct=round(_hl_mark_div, 4),
                    threshold_pct=_hl_threshold_pct,
                    oracle_delta_pct=round(_oracle_delta_pct, 6) if _oracle_delta_pct is not None else None,
                    itm_floor=_hl_mark_itm_floor,
                    suppressed=_hl_mark_oracle_suppressed,
                    tte=round(tte_seconds, 1),
                )
                return True, ExitReason.MOMENTUM_HL_MARK_SL, unrealised

        # ── Early Warning SL: HL Perp Depth Imbalance (Signal B) ───────────
        # Fires when HL perp book is heavily positioned against this trade.
        # TTE gate 30s — requires hl_position_imbalance already adjusted for side.
        if (
            not _suppress_taker_exits
            and getattr(config, "MOMENTUM_HL_DEPTH_SL_ENABLED", False)
            and hl_position_imbalance is not None
            and tte_seconds is not None
            and tte_seconds < getattr(config, "MOMENTUM_HL_DEPTH_SL_MAX_TTE", 30)
        ):
            _depth_threshold = getattr(config, "MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD", 0.40)
            if hl_position_imbalance < -_depth_threshold:
                log.warning(
                    "early_warning_sl",
                    signal="hl_depth",
                    market_id=pos.market_id[:20],
                    position_imbalance=round(hl_position_imbalance, 4),
                    threshold=_depth_threshold,
                    tte=round(tte_seconds, 1),
                )
                return True, ExitReason.MOMENTUM_HL_DEPTH_SL, unrealised

        return False, "", unrealised

    # ── Mispricing exits ──────────────────────────────────────────────────────
    if pos.strategy == "mispricing":
        if days_to_expiry <= config.EXIT_DAYS_BEFORE_RESOLUTION:
            return True, ExitReason.TIME_STOP, unrealised
        profit_target_usd = initial_deviation * config.PROFIT_TARGET_PCT * pos.size
        if unrealised >= profit_target_usd:
            return True, ExitReason.PROFIT_TARGET, unrealised
        if unrealised <= -config.STOP_LOSS_USD:
            return True, ExitReason.STOP_LOSS, unrealised
        return False, "", unrealised

    # ── Range exits — oracle delta SL only ───────────────────────────────────
    # Range positions (strategy="range") use a delta SL based on whether the oracle
    # spot has moved OUTSIDE the range boundaries.  pos.range_lo / pos.range_hi
    # store the actual title-parsed boundaries (set by the scanner); pos.strike holds
    # the midpoint as a fallback.
    # No take-profit or prob-SL: the scanner already confirmed a positive delta at
    # entry, and range markets converge correctly on their own near resolution.
    if pos.strategy == "range":
        if current_spot is not None:
            _sl = delta_sl_pct if delta_sl_pct is not None else config.MOMENTUM_DELTA_STOP_LOSS_PCT
            if pos.range_lo > 0 and pos.range_hi > 0:
                # Compute delta as distance-from-nearest-boundary (positive = in winning zone).
                if pos.side in ("YES", "BUY_YES", "UP"):
                    # YES wins when spot is INSIDE [range_lo, range_hi].
                    if pos.range_lo <= current_spot <= pos.range_hi:
                        mid = pos.strike if pos.strike > 0 else (pos.range_lo + pos.range_hi) / 2
                        current_delta_pct = min(
                            current_spot - pos.range_lo,
                            pos.range_hi - current_spot,
                        ) / mid * 100
                    else:
                        # Spot is outside the range — position losing.
                        current_delta_pct = -1.0  # definitely below _sl threshold
                else:
                    # NO wins when spot is OUTSIDE [range_lo, range_hi].
                    mid = pos.strike if pos.strike > 0 else (pos.range_lo + pos.range_hi) / 2
                    if current_spot > pos.range_hi:
                        current_delta_pct = (current_spot - pos.range_hi) / mid * 100
                    elif current_spot < pos.range_lo:
                        current_delta_pct = (pos.range_lo - current_spot) / mid * 100
                    else:
                        current_delta_pct = -1.0  # spot inside range → NO is losing
            elif pos.strike > 0:
                # Fallback when range bounds not stored: standard (spot-strike)/strike.
                if pos.side in ("YES", "BUY_YES", "UP"):
                    current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
                else:
                    current_delta_pct = (pos.strike - current_spot) / pos.strike * 100
            else:
                current_delta_pct = None
            if current_delta_pct is not None and current_delta_pct < _sl:
                return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised
        return False, "", unrealised

    # ── Any other strategy label: no triggers (hold to RESOLVED) ─────────────
    return False, "", unrealised


# ── Monitor class ─────────────────────────────────────────────────────────────

class PositionMonitor:
    """
    Asyncio task that polls every `interval` seconds.

    For each open position:
      - Fetches current PM mid price from the pm_client order book cache.
      - Evaluates exit conditions via should_exit().
      - If exiting: places a paper sell order, calls risk.close_position(),
        and logs the completed trade.
    """

    def __init__(
        self,
        pm: PMClient,
        risk: RiskEngine,
        interval: int = config.MONITOR_INTERVAL,
        spot_client: Optional[SpotOracle] = None,
        hl_client: Optional[Any] = None,
        on_close_callback: Optional[Callable[[str], None]] = None,
        on_stop_loss_callback: Optional[Callable[[str, float], None]] = None,
        oracle_tracker: Optional[OracleTickTracker] = None,
        on_closed_full_callback: Optional[Callable[[str, str, float, str], None]] = None,
    ) -> None:
        self._pm = pm
        self._risk = risk
        self._spot = spot_client  # SpotOracle facade; routes to correct oracle per market type
        self._hl = hl_client      # HLClient — S3.4 HL mark-price staleness logging
        self._interval = interval
        # Called with market_id whenever a position is successfully closed.
        # Used by MispricingScanner to reset the per-market cooldown clock.
        self._on_close_callback = on_close_callback
        # Called with (market_id, side, exit_price, strategy) whenever a position closes.
        # Used by OpeningNeutralScanner to backfill winner_exit_price in on_fills.csv.
        self._on_closed_full_callback = on_closed_full_callback
        # Called with (market_id, tte_remaining) when a momentum stop-loss fires.
        # Used by MomentumScanner to block re-entry for the remainder of TTE.
        self._on_stop_loss_callback = on_stop_loss_callback
        # Stores the initial |deviation| for each market_id; used for profit target
        self._initial_deviations: dict[str, float] = {}
        self._running = False
        # Token IDs for which an on-chain redemption (or dismissal) has already
        # been processed this session — avoids re-submitting on every poll cycle.
        self._redeemed_tokens: set[str] = set()
        # Hysteresis counter for delta stop-loss: tracks how many consecutive oracle
        # ticks each momentum position has been below MOMENTUM_DELTA_STOP_LOSS_PCT.
        # SL only fires once the count reaches MOMENTUM_DELTA_SL_MIN_TICKS.
        self._delta_sl_ticks: dict[str, int] = {}
        # M-13: consecutive below-threshold upfrac counter per momentum position.
        # Incremented once per MOMENTUM_UPFRAC_WINDOW_SECONDS; reset to 0 on recovery.
        # Exit fires when count reaches config.MOMENTUM_UPFRAC_EXIT_WINDOWS.
        self._upfrac_below_count: dict[str, int] = {}   # pos.market_id → count
        self._upfrac_window_ts: dict[str, float] = {}   # pos.market_id → last window evaluation time
        self._last_upfrac: dict[str, float] = {}         # ML-D1: last computed upfrac per market_id
        self._oracle_tracker = oracle_tracker  # M-13 OracleTickTracker (Phase 3)
        # Tracks positions currently being exited to prevent double-exit races
        # between the poll loop and the event-driven on_price_update path.
        self._exiting_positions: set[str] = set()  # "market_id:side"
        # Tracks positions where all exit attempts failed (EXIT_ORDER_FAILED).
        # Maps "market_id:side" → original exit reason for forced retry on next
        # monitor cycle. Cleared once the retry reaches _exit_position.
        self._pending_exit_positions: dict[str, str] = {}
        # Tracks the monotonic timestamp of the last oracle tick per coin.
        # Used to compute oracle freshness for the prob-SL oracle-lag gate.
        self._last_oracle_tick_ts: dict[str, float] = {}  # coin → time.monotonic()
        # Deduplication for momentum_ticks.csv writes: tracks the last (spot, token_price)
        # state written per market_id.  A tick is skipped when the full market state
        # (oracle spot + CLOB token price) hasn't changed since the last write and
        # exit_flag is False — eliminates the sub-millisecond burst of identical rows
        # caused by multiple oracle sources (chainlink_streams, rtds_chainlink, rtds,
        # PM WS) firing on the same price update.  Using a composite key prevents the
        # edge-case where one oracle source fires just before the cache is updated,
        # producing a slightly stale spot value that bypasses the old spot-only check.
        self._last_tick_state: dict[str, tuple] = {}  # market_id → (spot, token_price)
        # S1 Gate audit: consecutive-suppression counters for should_exit() gate blocks.
        # Key: "{token_id}:{gate_name}",  value: consecutive evaluation count.
        # Passed into should_exit() by reference so the function can update them.
        self._exit_suppress_counts: dict[str, int] = {}
        # Concurrent-check guard: prevents duplicate tick writes caused by multiple
        # oracle callbacks (e.g., RTDS + binance bookTicker) running concurrently
        # for the same position when they interleave at event-loop await boundaries.
        # Key: "{market_id}:{side}" — same format as _exiting_positions.
        self._checking_positions: set[str] = set()

    # ── Public interface ──────────────────────────────────────────────────────

    def record_entry_deviation(self, market_id: str, deviation: float) -> None:
        """
        Record the deviation at the time of entry.
        Call this immediately after opening a mispricing position.
        """
        self._initial_deviations[market_id] = abs(deviation)
        log.debug("Entry deviation recorded", market_id=market_id, deviation=abs(deviation))

    def get_entry_deviation(self, market_id: str, default: float = 0.0) -> float:
        """Return the recorded entry deviation for a market ID."""
        return self._initial_deviations.get(market_id, default)

    def _check_position_data_freshness(self, pos: Position) -> PositionDataHealth:
        """Return a freshness snapshot for a single open position (S2).

        Called at the top of every per-position evaluation before any gate
        runs.  Checks oracle age and PM book age for the held token.
        Statuses: ``"OK"`` | ``"STALE"`` | ``"MISSING"``.

        Side-effects:
        - Logs a WARNING when either feed is ``"STALE"`` or ``"MISSING"``.
        - When oracle is ``"STALE"`` and ORACLE_STALE_POSITION_FALLBACK_SECS > 0,
          schedules a background task to trigger a REST price refresh via
          ``SpotOracle.fetch_rest_spot()``.
        - When book is ``"STALE"`` and POSITION_BOOK_FALLBACK_AGE_SECS > 0,
          asks ``PMClient.register_priority_token()`` so the book refresh loop
          will fetch a fresh REST snapshot.
        """
        now_ts = time.time()

        # ── Oracle freshness ─────────────────────────────────────────────────
        oracle_age: Optional[float] = None
        oracle_source: str = "unknown"
        oracle_status: str = "MISSING"
        if self._spot is not None and pos.underlying:
            spot_mid = self._spot.get_mid(pos.underlying, pos.market_type)
            if spot_mid is not None:
                # Route oracle age through SpotOracle.get_spot_age so it tracks
                # the correct feed per market type: RTDS for 1h/daily/weekly
                # (which settle on Binance OHLC) and Chainlink for 5m/15m/4h.
                # Using _last_oracle_tick_ts (refreshed by both feeds) would mask
                # RTDS staleness for 1h positions when Chainlink is still alive.
                _raw_age = self._spot.get_spot_age(pos.underlying, pos.market_type)
                if not math.isinf(_raw_age):
                    oracle_age = round(_raw_age, 1)
                    stale_thresh = getattr(config, "MOMENTUM_SPOT_MAX_AGE_SECS", 30)
                    oracle_status = "OK" if oracle_age <= stale_thresh else "STALE"
                else:
                    # No snapshot yet (startup / cold cache) — treat as OK.
                    oracle_status = "OK"
            # else: snap missing → stays "MISSING"

        # ── Book freshness ────────────────────────────────────────────────────
        book_age: Optional[float] = None
        book_status: str = "MISSING"
        if pos.token_id:
            book = self._pm.get_book(pos.token_id)
            if book is not None:
                book_age = round(now_ts - book.timestamp, 1)
                stale_thresh_book = getattr(config, "MOMENTUM_BOOK_MAX_AGE_SECS", 30)
                book_status = "OK" if book_age <= stale_thresh_book else "STALE"

        health = PositionDataHealth(
            token_id=pos.token_id,
            coin=pos.underlying or "",
            oracle_age_secs=oracle_age,
            book_age_secs=book_age,
            oracle_status=oracle_status,
            book_status=book_status,
            oracle_source=oracle_source,
        )

        # ── Alerts ───────────────────────────────────────────────────────────
        if oracle_status != "OK":
            log.warning(
                "position_data_stale",
                token_id=pos.token_id[:16] if pos.token_id else "?",
                market_id=pos.market_id[:16],
                coin=pos.underlying,
                feed="oracle",
                status=oracle_status,
                oracle_age_secs=oracle_age,
                oracle_source=oracle_source,
                strategy=pos.strategy,
            )
        if book_status != "OK":
            log.warning(
                "position_data_stale",
                token_id=pos.token_id[:16] if pos.token_id else "?",
                market_id=pos.market_id[:16],
                coin=pos.underlying,
                feed="pm_book",
                status=book_status,
                book_age_secs=book_age,
                strategy=pos.strategy,
            )

        # ── Trigger REST fallbacks ────────────────────────────────────────────
        _oracle_fallback_secs = getattr(config, "ORACLE_STALE_POSITION_FALLBACK_SECS", 0)
        if (
            oracle_status == "STALE"
            and _oracle_fallback_secs > 0
            and oracle_age is not None
            and oracle_age >= _oracle_fallback_secs
            and self._spot is not None
            and hasattr(self._spot, "fetch_rest_spot")
        ):
            asyncio.create_task(
                self._spot.fetch_rest_spot(pos.underlying, pos.market_type),
                name=f"oracle_rest_{pos.underlying}_{pos.market_type}",
            )

        _book_fallback_secs = getattr(config, "POSITION_BOOK_FALLBACK_AGE_SECS", 0)
        if (
            book_status == "STALE"
            and _book_fallback_secs > 0
            and book_age is not None
            and book_age >= _book_fallback_secs
            and pos.token_id
            and hasattr(self._pm, "register_priority_token")
        ):
            self._pm.register_priority_token(pos.token_id)

        return health

    async def start(self) -> None:
        self._running = True
        # Event-driven stop-loss: check open momentum positions on every WS tick.
        # This eliminates the 30 s polling lag for stop-loss and take-profit exits.
        # All open positions are checked on every relevant PM price tick — regardless
        # of strategy.  No polling delay: the only latency is the PM WS round-trip.
        self._pm.on_price_change(self._on_price_update)
        # Event-driven delta SL: also check on every RTDS spot tick so that a
        # rapid underlying move triggers the delta SL immediately, without
        # waiting for the next PM WS tick (which may not come if the PM book
        # goes quiet near expiry — exactly when the SL matters most).
        if self._spot is not None:
            self._spot.on_rtds_update(self._on_spot_update)
            # Also fire on Chainlink ticks so 5m/15m/4h position SLs are
            # evaluated the moment a Chainlink oracle price update arrives.
            self._spot.on_chainlink_update(self._on_spot_update)
        # Immediately pin and register each new position's token in the PM WS
        # the moment risk.open_position() fires — before the main.py state-sync
        # loop (~1 s delay) calls pm.pin_tokens().  This closes the window where
        # a concurrent _update_shards() call could drop the token if the market
        # slips out of _extra_tokens_by_owner["default"].  Also arms REST book
        # fallback proactively so stale-book detection is ready from T+0.
        self._risk.on_position_open(self._on_position_open_sync)
        log.info("PositionMonitor started (event-driven)")
        asyncio.create_task(self._auto_redeem_loop())
        # Rare backstop sweep: catches resolved markets that PM WS never echoes
        # (e.g. resolution during a transient disconnect) and any other edge-case
        # positions that slipped through.  self._interval is acceptable for
        # non-real-time cleanup; all live position checks are driven by PM / RTDS
        # events above.
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                await self._check_all_positions()
            except Exception as exc:
                log.error("PositionMonitor backstop sweep failed", exc=str(exc))
            try:
                await self._check_pending_resolutions()
            except Exception as exc:
                log.error("PositionMonitor pending-resolution check failed", exc=str(exc))

    async def stop(self) -> None:
        self._running = False

    # ── Event-driven price callback ───────────────────────────────────────────

    def _on_position_open_sync(self, pos: "Position") -> None:
        """Synchronous callback fired by risk.open_position() — must be fast.

        Called with the risk lock released (see RiskEngine.open_position) so it
        is safe to call back into the PM client.

        1. Immediately adds the position's token to pm._pinned_tokens so that
           any concurrent _update_shards() call treats it as pinned — no waiting
           for the main.py state-sync loop (~1 s) to call pm.pin_tokens().
        2. Registers the token as a priority token so the _book_timestamp_refresh_loop
           can trigger REST book refreshes if the WS snapshot goes stale — arming
           the fallback from T+0 rather than waiting for the first stale-book
           detection cycle.
        """
        if not pos.token_id:
            return
        if hasattr(self._pm, "pin_token"):
            self._pm.pin_token(pos.token_id)
        if hasattr(self._pm, "register_priority_token"):
            self._pm.register_priority_token(pos.token_id)

    async def _on_price_update(self, token_id: str, mid: float) -> None:
        """Triggered on every PM WS book/price_change tick.

        Checks ALL open positions whose market's YES or NO token just updated.
        This makes every strategy event-driven — no configured delays, only
        WS round-trip latency (≈100–500 ms).  Momentum, maker, and mispricing
        positions are all checked immediately on every relevant price tick.
        """
        for pos in self._risk.get_open_positions():
            market = self._pm._markets.get(pos.market_id)
            if market is None:
                continue
            # React to either the YES or NO token updating for this market.
            if token_id not in (market.token_id_yes, market.token_id_no):
                continue
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                continue  # exit already in progress for this leg
            if key in self._checking_positions:
                continue  # check already in-flight — skip to avoid duplicate tick burst
            self._checking_positions.add(key)
            try:
                await self._check_position(pos, triggering_token_id=token_id, triggering_mid=mid)
            finally:
                self._checking_positions.discard(key)

    async def _on_spot_update(self, coin: str, price: float) -> None:
        """Triggered on every spot price tick (RTDS).

        Checks open momentum positions whose underlying matches `coin`.
        Ensures that a rapid spot move (e.g. BTC flash crash through the
        strike) triggers the delta SL immediately — without waiting for
        the PM WS to echo a price change, which may not happen if the PM
        book is thin or empty near expiry.
        """
        self._last_oracle_tick_ts[coin] = time.monotonic()
        for pos in self._risk.get_open_positions():
            if pos.strategy != "momentum" or pos.underlying != coin:
                continue
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                continue
            if key in self._checking_positions:
                continue  # check already in-flight — skip to avoid duplicate tick burst
            self._checking_positions.add(key)
            try:
                await self._check_position(pos)
            finally:
                self._checking_positions.discard(key)

    # ── Pending market-outcome resolution ─────────────────────────────────────

    async def _check_pending_resolutions(self) -> None:
        """For taker/stop exits, look up what the market eventually resolved to.

        Runs during the backstop sweep.  Checks pending_resolutions.json for
        markets whose end_date has passed, queries the Gamma API, and writes
        the settled YES-price to market_outcomes.json so the webapp can show
        WIN/LOSS for every trade — including early exits.
        """
        pending = _load_pending_resolutions()
        if not pending:
            return
        now = datetime.now(timezone.utc)
        changed = False
        for entry in pending:
            if entry.get("checked"):
                continue
            end_date_iso = entry.get("end_date_iso", "")
            if end_date_iso:
                try:
                    end_dt = datetime.fromisoformat(end_date_iso)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    # Wait at least 5 minutes after end_date before querying.
                    if now < end_dt + timedelta(minutes=5):
                        continue
                except ValueError:
                    pass
            cid = entry.get("condition_id", "")
            if not cid:
                entry["checked"] = True
                changed = True
                continue
            is_force = entry.get("force", False)
            # Non-force (taker/stop): skip if outcome already recorded by a RESOLVED exit.
            # Force (RESOLVED exits): always re-verify against CLOB to catch oracle fallback
            # misfires that wrote the wrong outcome.
            existing = _load_market_outcomes().get(cid)
            if existing is not None and not is_force:
                entry["checked"] = True
                changed = True
                continue
            try:
                resolved_yes_price = await self._pm.fetch_market_resolution(cid)
            except Exception as exc:
                log.debug("_check_pending_resolutions: lookup failed", cid=cid[:16], exc=str(exc))
                continue
            if resolved_yes_price is not None:
                _record_market_outcome(cid, resolved_yes_price)
                entry["checked"] = True
                changed = True
                log.info(
                    "Retroactive resolution recorded",
                    condition_id=cid[:16],
                    resolved_yes_price=resolved_yes_price,
                    force=is_force,
                )
                # Back-fill / correct resolved_outcome in trades.csv:
                #   force=False entries: SL/taker exits with blank outcome — fill in.
                #   force=True entries: RESOLVED exits that may have used oracle fallback —
                #       patch_trade_outcome(force=True) silently no-ops on correct records.
                # patch_trade_outcome is idempotent: patched=0 means already correct.
                try:
                    patched = self._risk.patch_trade_outcome(
                        cid, resolved_yes_price, force=True
                    )
                    if patched:
                        log.info(
                            "patch_trade_outcome: corrected outcome for exit(s)",
                            condition_id=cid[:16], patched=patched,
                        )
                except Exception as exc:
                    log.warning(
                        "patch_trade_outcome failed", cid=cid[:16], exc=str(exc)
                    )
                # For RESOLVED exits that recorded exit_spot_price=0.0 (because the
                # Gamma API didn't have closePrice at the moment of resolution), retry
                # now that the market has had time to settle.
                if is_force:
                    _underlying = entry.get("underlying", "")
                    _mkt_slug   = entry.get("market_slug", "")
                    _mkt_type   = entry.get("market_type", "")
                    _end_date_obj: Optional[datetime] = None
                    if end_date_iso:
                        try:
                            _end_date_obj = datetime.fromisoformat(end_date_iso)
                            if _end_date_obj.tzinfo is None:
                                _end_date_obj = _end_date_obj.replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass
                    _settle_spot: Optional[float] = None
                    # 1. Gamma events endpoint (authoritative)
                    if _mkt_slug:
                        try:
                            _settle_spot = await self._pm.fetch_gamma_settle_spot(_mkt_slug)
                        except Exception:
                            pass
                    # 2. PM crypto-price API fallback
                    if _settle_spot is None and _underlying and _end_date_obj:
                        _dur_s = _MARKET_TYPE_DURATION_SECS.get(_mkt_type, 0)
                        _wstart = (
                            (_end_date_obj - timedelta(seconds=_dur_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if _dur_s else ""
                        )
                        if _wstart:
                            try:
                                _settle_spot = await self._pm.fetch_resolve_spot_price(
                                    _underlying, _wstart, _end_date_obj,
                                )
                            except Exception:
                                pass
                    if _settle_spot and _settle_spot > 0:
                        try:
                            _sp = self._risk.patch_exit_spot_price(cid, _settle_spot)
                            if _sp:
                                log.info(
                                    "patch_exit_spot_price: settlement spot back-filled",
                                    condition_id=cid[:16],
                                    spot=round(_settle_spot, 4),
                                    records=_sp,
                                )
                        except Exception as exc:
                            log.warning(
                                "patch_exit_spot_price failed", cid=cid[:16], exc=str(exc)
                            )
                        # Also back-fill spot_resolve_price on any momentum_hedge row
                        # that was written with 0.0 (hedge token arrived in wallet
                        # before Gamma published closePrice — same race as exit_spot).
                        try:
                            _hp = self._risk.patch_hedge_spot_price(cid, _settle_spot)
                            if _hp:
                                log.info(
                                    "patch_hedge_spot_price: settlement spot back-filled",
                                    condition_id=cid[:16],
                                    spot=round(_settle_spot, 4),
                                    records=_hp,
                                )
                        except Exception as exc:
                            log.warning(
                                "patch_hedge_spot_price failed", cid=cid[:16], exc=str(exc)
                            )
        if changed:
            _save_pending_resolutions(pending)

    # ── Auto-redemption ───────────────────────────────────────────────────────

    async def _auto_redeem_loop(self) -> None:
        """Periodically fetch PM wallet positions and redeem any that are ready.

        Runs every REDEEM_POLL_INTERVAL seconds (default 30 s).  Only active in
        live mode (PAPER_TRADING=False) and when POLY_PRIVATE_KEY + POLY_FUNDER
        are configured.  Each token is redeemed at most once per session; the
        `_redeemed_tokens` set prevents duplicate on-chain submissions.
        """
        # Short initial delay so startup restore completes first
        await asyncio.sleep(30)
        while self._running:
            if not config.PAPER_TRADING and config.POLY_PRIVATE_KEY and config.POLY_FUNDER:
                try:
                    await self._redeem_ready_positions()
                except Exception as exc:
                    log.error("Auto-redeem loop error", exc=str(exc))
            await asyncio.sleep(config.REDEEM_POLL_INTERVAL)

    async def _fetch_hedge_resolve_spot(self, parent_pos: "Position") -> float:
        """Return the settlement spot price for parent_pos's market.

        Resolution order (most-authoritative first):
        1. Gamma API ``/events/slug/{slug}`` → ``eventMetadata.closePrice``:
           the official PM settlement price for the underlying asset, independent
           of bucket duration.  Available once the market is resolved.
        2. Polymarket crypto-price API → ``closePrice`` (bucket-duration-aware):
           same oracle source, duration-variant mapped per market_type.
        3. Live oracle mid: snapshot of the live feed at call time — only a
           best-effort estimate; use only when both API paths fail.
        Returns 0.0 if no price is available.
        """
        mkt = self._pm.get_markets().get(parent_pos.market_id)
        slug = getattr(mkt, "market_slug", "") if mkt is not None else ""
        # 1. Gamma events endpoint (authoritative for resolved markets, all durations)
        if slug:
            try:
                _gamma = await self._pm.fetch_gamma_settle_spot(slug)
                if _gamma is not None:
                    return round(_gamma, 4)
            except Exception:
                pass
        # 2. PM crypto-price API (variant="fifteen" for all bucket types)
        if (
            parent_pos.underlying
            and mkt is not None
            and getattr(mkt, "end_date", None)
        ):
            try:
                # Compute the actual window-open time from end_date minus known
                # duration.  mkt.event_start_time is the Gamma eventStartTime which
                # IS the window start, but use end_date arithmetic as a guard.
                _dur_s = _MARKET_TYPE_DURATION_SECS.get(mkt.market_type, 0)
                _window_start = (
                    (mkt.end_date - timedelta(seconds=_dur_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if _dur_s
                    else (mkt.event_start_time or "")
                )
                _close = await self._pm.fetch_resolve_spot_price(
                    parent_pos.underlying,
                    _window_start,
                    mkt.end_date,
                )
                if _close is not None:
                    return round(_close, 4)
            except Exception:
                pass
        # 3. Do NOT fall back to live oracle mid — the live feed price at detection
        # time will not match the Chainlink oracle settlement price. Return 0.0;
        # _check_pending_resolutions will retry once Gamma has the closePrice
        # (typically a few minutes after market close).
        log.debug(
            "_fetch_hedge_resolve_spot: Gamma API not ready — will retry via pending resolutions",
            market_id=parent_pos.market_id[:20],
            underlying=parent_pos.underlying,
        )
        return 0.0

    async def _redeem_ready_positions(self) -> None:
        """Fetch wallet positions and submit on-chain redemption for each redeemable token.

        Live API is the source of truth:
        - redeemable=False, cur_price mid (0.01–0.99) → oracle still pending, skip
        - redeemable=False, cur_price settled (≤0.01 or ≥0.99) → already redeemed
          externally (e.g. via PM UI); correct trades.csv and mark as done
        - redeemable=True, payout=0  → genuine loser, dismiss
        - redeemable=True, payout>0  → submit on-chain redemption
        """
        raw = await self._pm.get_live_positions()
        if not raw:
            return

        for pos_data in raw:
            token_id: str = pos_data.get("asset") or ""
            if not token_id or token_id in self._redeemed_tokens:
                continue
            if not pos_data.get("redeemable", False):
                # curPrice is stale mid-price — do NOT use it for WIN/LOSS.
                # The CLOB winner flag is the authoritative source of truth.
                _ext_condition_id: str = (
                    pos_data.get("conditionId") or pos_data.get("condition_id") or ""
                )
                if not _ext_condition_id:
                    continue
                _ext_resolved_yes = await self._pm.fetch_market_resolution(_ext_condition_id)
                if _ext_resolved_yes is None:
                    continue  # CLOB not settled yet — nothing to do

                # Settled but redeemable=False → already redeemed externally.
                # Correct trades.csv (force=True is idempotent on correct records)
                # and mark the token so we don't process it again this session.
                _ext_title = pos_data.get("title") or token_id[:20]
                markets_snap = self._pm.get_markets()
                for mkt in markets_snap.values():
                    if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                        is_yes_token = (mkt.token_id_yes == token_id)
                        # pm_resolved_yes is always the YES-token price (1.0=YES won)
                        pm_resolved_yes = _ext_resolved_yes
                        if pm_resolved_yes >= 0.99:
                            # YES won; this token won if it's the YES token
                            _outcome_str = "WIN" if is_yes_token else "LOSS"
                            _exit_p = 1.0 if is_yes_token else 0.0
                        else:
                            # NO won; this token won if it's the NO token
                            _outcome_str = "LOSS" if is_yes_token else "WIN"
                            _exit_p = 0.0 if is_yes_token else 1.0
                        # Close any open (ghost) position
                        for rp in list(self._risk.get_positions().values()):
                            if rp.market_id == mkt.condition_id and not rp.is_closed:
                                self._risk.close_position(
                                    mkt.condition_id, exit_price=_exit_p,
                                    side=rp.side, resolved_outcome=_outcome_str,
                                )
                                if rp.token_id and hasattr(self._pm, "deregister_priority_token"):
                                    self._pm.deregister_priority_token(rp.token_id)
                        # Force-correct any already-closed record with wrong outcome
                        try:
                            patched = self._risk.patch_trade_outcome(
                                mkt.condition_id, pm_resolved_yes, force=True
                            )
                            log.info(
                                "Auto-redeem: externally-redeemed token detected"
                                " — trades.csv corrected",
                                condition_id=mkt.condition_id[:20],
                                title=_ext_title[:60],
                                is_yes_token=is_yes_token,
                                pm_resolved_yes=pm_resolved_yes,
                                outcome=_outcome_str,
                                patched=patched,
                            )
                        except Exception as exc:
                            log.warning(
                                "Auto-redeem: patch_trade_outcome failed"
                                " (external redemption)",
                                exc=str(exc),
                                condition_id=_ext_condition_id[:20],
                            )
                        break
                # Do NOT add to _redeemed_tokens here.  The PM API sometimes
                # returns redeemable=False for a brief window after oracle
                # settlement before flipping to redeemable=True.  If we block the
                # token now it will never be redeemed on-chain.
                # patch_trade_outcome(force=True) is idempotent so repeated calls
                # on consecutive polls are safe.  Once the position disappears from
                # the wallet (post on-chain redemption) it won't appear here again.
                continue

            size = float(pos_data.get("size", 0) or 0)
            cur_price = float(
                pos_data.get("curPrice") or pos_data.get("currentPrice") or pos_data.get("cur_price") or 0
            )
            title = pos_data.get("title") or token_id[:20]
            condition_id: str = pos_data.get("conditionId") or pos_data.get("condition_id") or ""

            # curPrice from the positions API is stale mid-price and must NOT be
            # used for WIN/LOSS determination — it can show ~1.0 for a losing token
            # in the brief window right after settlement (as seen in prod on 2026-04-16).
            # The CLOB winner flag via fetch_market_resolution() is the source of truth.
            if not condition_id:
                log.warning("Auto-redeem: no condition_id — will retry next cycle", token_id=token_id[:20])
                continue
            resolved_yes = await self._pm.fetch_market_resolution(condition_id)
            if resolved_yes is None:
                log.debug(
                    "Auto-redeem: CLOB not settled yet — will retry next cycle",
                    token_id=token_id[:20],
                    cur_price=round(cur_price, 4),
                )
                continue
            # Determine settlement price for this specific token (YES or NO).
            # Primary: check the local markets cache (fast, no API call).
            # Fallback: fetch from CLOB directly — short-lived 5m markets are
            # evicted from the cache immediately after they close, so the loop
            # below can silently fail to find the token, leaving settled_price=0.0
            # and triggering a false payout=0 dismissal.
            markets_snap = self._pm.get_markets()
            settled_price = 0.0  # default: loser until CLOB confirms otherwise
            _token_found_in_cache = False
            for _mkt in markets_snap.values():
                if _mkt.token_id_yes == token_id:
                    settled_price = resolved_yes          # 1.0 if YES won
                    _token_found_in_cache = True
                    break
                if _mkt.token_id_no == token_id:
                    settled_price = 1.0 - resolved_yes    # 1.0 if NO won
                    _token_found_in_cache = True
                    break
            if not _token_found_in_cache:
                # Market evicted from cache (common for 5m buckets).  Ask the CLOB
                # directly which side this token belongs to.
                _side = await self._pm.fetch_token_side(condition_id, token_id)
                if _side == "yes":
                    settled_price = resolved_yes
                elif _side == "no":
                    settled_price = 1.0 - resolved_yes
                else:
                    # Cannot determine side — skip this cycle rather than mis-record.
                    log.warning(
                        "Auto-redeem: token side unknown — will retry next cycle",
                        token_id=token_id[:20],
                        condition_id=condition_id[:20],
                        resolved_yes=resolved_yes,
                    )
                    continue
                log.info(
                    "Auto-redeem: token side resolved via CLOB fallback",
                    token_id=token_id[:20],
                    condition_id=condition_id[:20],
                    side=_side,
                    settled_price=settled_price,
                )
            log.debug(
                "Auto-redeem: CLOB-resolved settlement price",
                token_id=token_id[:20],
                resolved_yes=resolved_yes,
                settled_price=settled_price,
            )

            payout = round(size * settled_price, 4)

            # ── GTD hedge token intercept ─────────────────────────────────────────
            # When a wallet token belongs to a momentum GTD hedge (a resting BUY on
            # the OPPOSITE side), the normal close-position logic must NOT fire:
            # applying the hedge token's settlement price to the main position would
            # record an incorrect WIN/LOSS on the main trade.  Check first.
            _hedge_parent = self._risk.get_position_by_hedge_token(token_id)

            # Fallback: parent Position may have been evicted from memory after a
            # bot restart.  HedgeOrder entities persist across restarts in
            # hedge_orders.json — look up by token_id as a secondary check.
            _hedge_ho: Optional[object] = None
            if _hedge_parent is None:
                _hedge_ho = self._risk.get_hedge_order_by_token_id(token_id)

            # Genuine loser — nothing to redeem on-chain
            if payout == 0:
                self._redeemed_tokens.add(token_id)
                # GTD hedge token that lost: dismiss with a dedicated log line and
                # skip the main-position close entirely.
                if _hedge_parent is not None:
                    # Hedge token was in wallet and resolved to 0 (main WON, hedge LOST).
                    # Guard: if the HedgeOrder was already finalized by the cancel-ok or
                    # reprice path (e.g., status="filled_lost"), skip writing a duplicate
                    # trades.csv row — the cancel path already wrote the correct record.
                    from risk import HedgeStatus as _HS_AR
                    _ho_ar = (
                        self._risk.get_hedge_order(_hedge_parent.hedge_order_id)
                        if _hedge_parent.hedge_order_id else None
                    )
                    if _ho_ar is not None and (
                        _ho_ar.status in _HS_AR.TERMINAL
                        or _ho_ar.status in ("filled_lost", "filled_won")
                    ):
                        log.info(
                            "Auto-redeem: GTD hedge already finalized — skipping duplicate record",
                            token_id=token_id[:20],
                            parent_market_id=_hedge_parent.market_id[:20],
                            existing_status=_ho_ar.status,
                        )
                        continue
                    # Record the outcome so the webapp can show "Expired - LOST".
                    _spot_hl = await self._fetch_hedge_resolve_spot(_hedge_parent)
                    self._risk.record_hedge_fill(
                        parent_market_id=_hedge_parent.market_id,
                        parent_market_title=_hedge_parent.market_title,
                        hedge_token_id=token_id,
                        fill_price=_hedge_parent.hedge_price,
                        fill_size=size,
                        settled_price=0.0,
                        underlying=_hedge_parent.underlying or "",
                        hedge_status="filled_lost",
                        spot_resolve_price=_spot_hl,
                    )
                    log.info(
                        "Auto-redeem: GTD hedge token lost (payout=0) — outcome recorded",
                        token_id=token_id[:20],
                        parent_market_id=_hedge_parent.market_id[:20],
                        spot_resolve_price=round(_spot_hl, 2),
                    )
                    continue
                if _hedge_ho is not None:
                    # Parent position evicted; use HedgeOrder entity for context.
                    from risk import HedgeStatus as _HS
                    if _hedge_ho.status not in _HS.TERMINAL and _hedge_ho.status not in ("filled_won", "filled_lost"):
                        self._risk.finalize_hedge(
                            _hedge_ho.order_id,
                            settled_price=0.0,
                            spot_at_resolution=0.0,
                            hedge_status="filled_lost",
                        )
                        log.info(
                            "Auto-redeem: GTD hedge lost (payout=0, parent evicted) — finalized via HedgeOrder",
                            token_id=token_id[:20],
                            order_id=_hedge_ho.order_id[:20],
                            market_id=_hedge_ho.market_id[:20],
                        )
                    continue
                log.info(
                    "Auto-redeem: lost position dismissed (payout=0)",
                    token_id=token_id[:20],
                    title=title[:60],
                )
                # PM's payout=0 is the definitive source of truth for the outcome.
                # Close any still-open position AND force-correct any already-closed
                # record that was written with the wrong outcome (e.g., the RESOLVED
                # fast-path used a stale CLOB/oracle and recorded WIN).
                if condition_id:
                    markets_snap = self._pm.get_markets()
                    for mkt in markets_snap.values():
                        if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                            # token settled to 0 → that token's side lost
                            # YES token=0  → YES lost  → resolved_yes_price=0.0
                            # NO  token=0  → NO  lost  → resolved_yes_price=1.0
                            is_yes_token = (mkt.token_id_yes == token_id)
                            pm_resolved_yes = 0.0 if is_yes_token else 1.0
                            # Close any open position
                            for rp in list(self._risk.get_positions().values()):
                                if rp.market_id == mkt.condition_id and not rp.is_closed:
                                    self._risk.close_position(
                                        mkt.condition_id, exit_price=0.0, side=rp.side,
                                        resolved_outcome="LOSS",
                                    )
                            # Correct any already-closed record with wrong outcome
                            try:
                                patched = self._risk.patch_trade_outcome(
                                    mkt.condition_id, pm_resolved_yes, force=True
                                )
                                if patched:
                                    log.info(
                                        "Auto-redeem: corrected trades.csv via PM payout=0",
                                        condition_id=mkt.condition_id[:20],
                                        is_yes_token=is_yes_token,
                                        patched=patched,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "Auto-redeem: patch_trade_outcome failed",
                                    exc=str(exc), condition_id=condition_id[:20],
                                )
                            break
                continue

            log.info(
                "Auto-redeem: redeemable position found",
                token_id=token_id[:20],
                title=title[:60],
                size=round(size, 2),
                payout_usd=payout,
                condition_id=condition_id[:20],
            )

            # PM's payout>0 is the definitive source of truth for the outcome.
            # Close any still-open position AND force-correct any already-closed
            # record that was written with the wrong outcome (e.g., RESOLVED fast-path
            # used stale oracle and recorded LOSS when market actually paid out).
            #
            # Exception: GTD hedge tokens share the same condition_id as the main
            # position (they are on the opposite token of the same market).  If we
            # ran the normal close-position loop the hedge token's settlement price
            # (1.0) would be applied to the main trade, recording a phantom WIN.
            # Instead, record the hedge fill's own P&L and let on-chain redemption
            # proceed without touching the main position's accounting.
            if _hedge_parent is not None:
                _spot_hw = await self._fetch_hedge_resolve_spot(_hedge_parent)
                self._risk.record_hedge_fill(
                    parent_market_id=_hedge_parent.market_id,
                    parent_market_title=_hedge_parent.market_title,
                    hedge_token_id=token_id,
                    fill_price=_hedge_parent.hedge_price,
                    fill_size=size,
                    settled_price=settled_price,
                    underlying=_hedge_parent.underlying or "",
                    hedge_status="filled_won",
                    spot_resolve_price=_spot_hw,
                )
                log.info(
                    "Auto-redeem: GTD hedge token won — fill P&L recorded, redeeming on-chain",
                    token_id=token_id[:20],
                    parent_market_id=_hedge_parent.market_id[:20],
                    fill_price=_hedge_parent.hedge_price,
                    fill_size=round(size, 4),
                    payout_usd=payout,
                )
                _emit_event(
                    "HEDGE_FILL",
                    market_id=_hedge_parent.market_id,
                    market_title=_hedge_parent.market_title[:80],
                    hedge_token_id=token_id,
                    fill_price=_hedge_parent.hedge_price,
                    fill_size=round(size, 4),
                    payout_usd=round(payout, 4),
                )
            elif _hedge_ho is not None:
                # Parent Position evicted but HedgeOrder entity is available.
                # Use finalize_hedge so both hedge_orders.json and trades.csv are
                # updated correctly (the critical case: hedge WON after restart).
                from risk import HedgeStatus as _HS
                if _hedge_ho.status not in _HS.TERMINAL and _hedge_ho.status not in ("filled_won", "filled_lost"):
                    self._risk.finalize_hedge(
                        _hedge_ho.order_id,
                        settled_price=settled_price,
                        spot_at_resolution=0.0,
                        hedge_status="filled_won" if settled_price >= 0.5 else "filled_lost",
                    )
                    log.info(
                        "Auto-redeem: GTD hedge won (parent evicted) — finalized via HedgeOrder",
                        token_id=token_id[:20],
                        order_id=_hedge_ho.order_id[:20],
                        market_id=_hedge_ho.market_id[:20],
                        settled_price=settled_price,
                        payout_usd=payout,
                    )
                    _emit_event(
                        "HEDGE_FILL",
                        market_id=_hedge_ho.market_id,
                        market_title=(_hedge_ho.market_title or "")[:80],
                        hedge_token_id=token_id,
                        fill_price=_hedge_ho.avg_fill_price or _hedge_ho.order_price,
                        fill_size=round(_hedge_ho.size_filled, 4),
                        payout_usd=round(payout, 4),
                    )
            else:
                markets_snap = self._pm.get_markets()
                for mkt in markets_snap.values():
                    if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                        # resolved_yes from CLOB is authoritative (see fetch above).
                        # settled_price is 1.0 for this specific token (it won).
                        is_yes_token = (mkt.token_id_yes == token_id)
                        pm_resolved_yes = resolved_yes  # YES-price: 1.0=YES won, 0.0=NO won
                        _win_str = "WIN" if settled_price >= 0.99 else "LOSS"
                        for rp in list(self._risk.get_positions().values()):
                            if rp.market_id == mkt.condition_id and not rp.is_closed:
                                self._risk.close_position(
                                    mkt.condition_id, exit_price=settled_price, side=rp.side,
                                    resolved_outcome=_win_str,
                                )
                                if rp.token_id and hasattr(self._pm, "deregister_priority_token"):
                                    self._pm.deregister_priority_token(rp.token_id)
                        # Correct any already-closed record with wrong outcome
                        try:
                            patched = self._risk.patch_trade_outcome(
                                mkt.condition_id, pm_resolved_yes, force=True
                            )
                            if patched:
                                log.info(
                                    "Auto-redeem: corrected trades.csv via PM payout>0",
                                    condition_id=mkt.condition_id[:20],
                                    payout_usd=payout,
                                    patched=patched,
                                )
                        except Exception as exc:
                            log.warning(
                                "Auto-redeem: patch_trade_outcome failed",
                                exc=str(exc), condition_id=condition_id[:20],
                            )
                        break

            # Mark BEFORE the async call so a crash doesn't cause a double-submit.
            self._redeemed_tokens.add(token_id)
            try:
                ctf_address = _POLY_CONTRACTS.conditional_tokens
                collateral_address = _POLY_CONTRACTS.collateral
                tx_hash = await _redeem_ctf_via_safe(
                    ctf_address=ctf_address,
                    collateral=collateral_address,
                    condition_id=condition_id,
                    index_sets=[1, 2],  # YES=1, NO=2 for binary markets
                    private_key=config.POLY_PRIVATE_KEY,
                    safe_address=config.POLY_FUNDER,
                )
                log.info(
                    "Auto-redeem: on-chain redemption submitted ✓",
                    tx_hash=tx_hash,
                    condition_id=condition_id,
                    payout_usd=payout,
                )
            except Exception as exc:
                self._redeemed_tokens.discard(token_id)
                log.warning(
                    "Auto-redeem: on-chain call failed — will retry next cycle",
                    exc=str(exc),
                    condition_id=condition_id,
                )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _check_all_positions(self) -> None:
        """Iterate all open positions and apply exit logic."""
        open_positions = self._risk.get_open_positions()
        if not open_positions:
            return

        log.debug("PositionMonitor checking positions", count=len(open_positions))

        # ── Maker coin-level loss guard (MAKER_COIN_MAX_LOSS_USD) ─────────────
        # Aggregate unrealised P&L per coin across all open maker positions.
        # If any coin's total drops below -MAKER_COIN_MAX_LOSS_USD, flatten
        # all of that coin's maker positions passively (limit at current mid).
        coin_unrealised: dict[str, float] = {}
        coin_positions: dict[str, list] = {}
        for pos in open_positions:
            if pos.strategy != "maker":
                continue
            market = self._pm._markets.get(pos.market_id)
            if market is None:
                continue
            coin = pos.underlying
            # Always track the position so it is included in the closure sweep.
            # Only contribute to the unrealised sum when book data is available;
            # bookless positions are treated as 0 unrealised (conservative).
            coin_positions.setdefault(coin, []).append(pos)
            book = self._pm._books.get(market.token_id_yes)
            if book is None or book.mid is None:
                log.debug(
                    "Monitor: no book for coin-loss aggregation — treating as 0 unrealised",
                    market_id=pos.market_id, coin=coin,
                )
                continue
            if pos.side in ("YES", "BUY_YES", "UP"):
                cur_price = book.mid
            else:
                book_no = self._pm._books.get(market.token_id_no)
                if book_no is None or book_no.mid is None:
                    log.debug(
                        "Monitor: no NO/DOWN book for coin-loss aggregation — treating as 0 unrealised",
                        market_id=pos.market_id, coin=coin,
                    )
                    continue
                cur_price = book_no.mid
            unrealised = compute_unrealised_pnl(pos, cur_price)
            coin_unrealised[coin] = coin_unrealised.get(coin, 0.0) + unrealised

        # Track which market_ids are being closed by coin-loss so we skip them below
        coin_loss_closed: set[str] = set()
        for coin, total_unr in coin_unrealised.items():
            if total_unr < -config.MAKER_COIN_MAX_LOSS_USD:
                log.warning(
                    "Maker coin loss limit breached — closing all positions for coin",
                    coin=coin,
                    total_unrealised_usd=round(total_unr, 2),
                    limit=config.MAKER_COIN_MAX_LOSS_USD,
                    position_count=len(coin_positions[coin]),
                )
                for pos in coin_positions[coin]:
                    market = self._pm._markets.get(pos.market_id)
                    if market is None:
                        continue
                    book = self._pm._books.get(market.token_id_yes)
                    if book is None or book.mid is None:
                        # No live price — close at entry price (zero P&L, avoids leaving position open)
                        log.warning(
                            "Monitor: closing coin-loss position without book data — using entry price",
                            market_id=pos.market_id,
                        )
                        exit_mid = pos.entry_price
                    else:
                        # Taker exit: YES/UP sells at best_bid; NO/DOWN sells at NO CLOB best_bid.
                        if pos.side in ("YES", "BUY_YES", "UP"):
                            exit_mid = book.best_bid if book.best_bid is not None else book.mid
                        else:
                            book_no_cl = self._pm._books.get(market.token_id_no)
                            if book_no_cl is not None and book_no_cl.best_bid is not None:
                                exit_mid = book_no_cl.best_bid
                            else:
                                log.warning(
                                    "Monitor: NO book unavailable for coin-loss exit — using entry price",
                                    market_id=pos.market_id,
                                )
                                exit_mid = pos.entry_price
                    try:
                        await self._exit_position(
                            pos, market, exit_mid,
                            ExitReason.COIN_LOSS_LIMIT,
                            compute_unrealised_pnl(pos, exit_mid),
                        )
                        coin_loss_closed.add(f"{pos.market_id}:{pos.side}")
                    except Exception as exc:
                        log.error("Error closing coin-loss position",
                                  market_id=pos.market_id, exc=str(exc))

        for pos in open_positions:
            if f"{pos.market_id}:{pos.side}" in coin_loss_closed:
                continue  # already closed above
            try:
                await self._check_position(pos)
            except Exception as exc:
                log.error("Error checking position", market_id=pos.market_id, exc=str(exc))

        # ── GTD hedge order management loop ────────────────────────────────────
        # Runs independently of the position loop so it keeps working even when
        # the parent position's market is not yet in the PM market cache.
        # Two sub-features per open hedge order:
        #   1. Near-expiry cancel: if TTE ≤ MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS and the
        #      held token's CLOB mid is above 0.50 (winning), cancel to prevent adverse fill.
        #   2. Gap-closing reprice: if the CLOB best_ask FELL since the last sweep,
        #      cancel + repost at current_bid + $0.01 to chase liquidity.
        await self._manage_open_hedge_orders()

    async def _manage_open_hedge_orders(self) -> None:
        """Process all open hedge orders: near-expiry cancel + gap-closing reprice."""
        from risk import HedgeStatus
        now_dt = datetime.now(timezone.utc)
        expiry_cancel_secs = getattr(config, "MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS", 0)
        min_retain_usd    = getattr(config, "MOMENTUM_HEDGE_MIN_RETAIN_USD", 0.50)

        # S3.4: Log HL mark-price staleness for coins with open hedges.
        # Dedup by coin so we log at most once per coin per sweep.
        if self._hl is not None and hasattr(self._hl, "get_mark_price_age"):
            _hl_stale_secs = float(getattr(config, "HL_WS_STALE_SECS", 30))
            _logged_hl_coins: set[str] = set()
            for _ho in self._risk.get_open_hedge_orders():
                _coin = getattr(_ho, "underlying", "")
                if not _coin or _coin in _logged_hl_coins:
                    continue
                _age = self._hl.get_mark_price_age(_coin)
                if _age is not None and _age > _hl_stale_secs:
                    log.warning(
                        "[HL_DEGRADED]",
                        coin=_coin,
                        hedge_order=_ho.order_id[:12],
                        mark_price_age_secs=round(_age, 1),
                        threshold=_hl_stale_secs,
                    )
                _logged_hl_coins.add(_coin)

        for ho in self._risk.get_open_hedge_orders():
            try:
                # ── 1. Near-expiry cancel ─────────────────────────────────────
                if expiry_cancel_secs > 0:
                    hedge_market = self._pm._markets.get(ho.market_id)
                    if hedge_market is not None and getattr(hedge_market, "end_date", None) is not None:
                        tte_s = (hedge_market.end_date - now_dt).total_seconds()
                        if tte_s <= expiry_cancel_secs:
                            # Only cancel when we are winning (held-token mid > 0.50).
                            _parent_pos = self._risk.get_position_for_hedge(ho.order_id)
                            _held_mid: Optional[float] = None
                            if _parent_pos is not None and _parent_pos.token_id:
                                _held_book = self._pm._books.get(_parent_pos.token_id)
                                if _held_book is not None:
                                    _held_mid = _held_book.mid
                            if _held_mid is not None and _held_mid > 0.50:
                                ok = await self._pm.cancel_order(ho.order_id)
                                if ok:
                                    self._risk.finalize_hedge(
                                        ho.order_id,
                                        settled_price=0.0,
                                        hedge_status=HedgeStatus.CANCELLED,
                                    )
                                continue  # skip reprice check this sweep

                # ── 2. Gap-closing reprice ────────────────────────────────────
                book = self._pm._books.get(ho.token_id)
                if book is None or getattr(book, "best_ask", None) is None:
                    # No live ask data — clear baseline so next sweep starts fresh.
                    ho.last_clob_ask = None
                    continue

                curr_ask = book.best_ask
                if ho.last_clob_ask is None:
                    # First sweep for this order: store baseline, no action.
                    ho.last_clob_ask = curr_ask
                    continue

                if curr_ask >= ho.last_clob_ask:
                    # Ask flat or rising: no reprice needed.
                    ho.last_clob_ask = curr_ask
                    continue

                # Ask fell → gap-closing reprice opportunity.
                best_bid = getattr(book, "best_bid", None) or 0.0
                new_bid  = round(best_bid + 0.01, 6)

                if ho.price_cap > 0.0 and new_bid > ho.price_cap:
                    # Price cap exceeded: repricing would erode projected P&L.
                    ho.last_clob_ask = curr_ask
                    continue

                ok = await self._pm.cancel_order(ho.order_id)
                if not ok:
                    # Order already filled — skip placement.
                    ho.last_clob_ask = curr_ask
                    continue

                # Compute reprice size.
                remaining = round(ho.order_size - ho.size_filled, 8)
                pm_min_contracts = round(1.0 / new_bid, 6) if new_bid > 0 else remaining
                new_size = round(max(remaining, pm_min_contracts), 6)

                # PnL ceiling: notional ≤ projected_pnl − min_retain.
                if ho.projected_pnl_usd > 0.0:
                    budget   = ho.projected_pnl_usd - min_retain_usd
                    ceiling  = round(budget / new_bid, 6) if new_bid > 0 else new_size
                    new_size = round(min(new_size, ceiling), 6)

                new_order_id = await self._pm.place_limit(
                    token_id=ho.token_id,
                    side="BUY",
                    price=new_bid,
                    size=new_size,
                )
                if new_order_id is None:
                    # Placement failed: mark old order as cancelled.
                    self._risk.finalize_hedge(
                        ho.order_id,
                        settled_price=0.0,
                        hedge_status=HedgeStatus.CANCELLED,
                    )
                else:
                    self._risk.replace_hedge_order(
                        ho.order_id,
                        new_order_id,
                        new_bid,
                        new_order_size=new_size,
                    )

                ho.last_clob_ask = curr_ask

            except Exception as exc:
                log.warning(
                    "Hedge management loop error",
                    order_id=ho.order_id[:20],
                    exc=str(exc),
                )

    async def _check_position(
        self,
        pos: Position,
        *,
        triggering_token_id: Optional[str] = None,
        triggering_mid: Optional[float] = None,
    ) -> None:
        """Evaluate exit conditions for a single position.

        triggering_token_id / triggering_mid: when supplied (event-driven path),
        the fresh WS tick value is used directly as current_token_price for the
        matching held token, bypassing a potentially-stale book re-fetch.
        """
        market = self._pm._markets.get(pos.market_id)
        if market is None:
            log.debug("Monitor: market not in cache", market_id=pos.market_id)
            return

        now = datetime.now(timezone.utc)
        hold_secs = (now - pos.opened_at).total_seconds()

        # ── Resolution fast-path (book not required) ───────────────────────────
        # After PM resolves a market the order book drains to empty, causing
        # book.mid to become None.  The book guard below would then skip this
        # position on every monitor cycle, leaving it stuck indefinitely.
        # Check end_date FIRST so resolved positions are always closed regardless
        # of whether live book data is still available.
        # NOTE: MIN_HOLD_SECONDS is deliberately NOT applied here.  The hold-time
        # guard protects against noise-driven exits on live markets; resolved
        # markets are definitively settled and there is no noise to avoid.
        # Skipping the guard also ensures that positions restored after a bot
        # restart (which receive opened_at=now) are cleaned up immediately on the
        # first monitor cycle rather than after a 60-second delay.
        if (
            market.end_date is not None
            and now >= market.end_date
        ):
            # PM CLOB settlement API is the only source of truth.
            # Never fall back to oracle spot or entry price — an inaccurate
            # result is worse than a delayed one.
            _pm_yes_price = await self._pm.fetch_market_resolution(pos.market_id)
            if _pm_yes_price is None:
                # PM hasn't published the result yet — keep waiting.
                log.debug(
                    "Monitor: RESOLVED market — PM API not settled yet, will retry",
                    market_id=pos.market_id,
                    secs_past_end=round((now - market.end_date).total_seconds(), 0),
                )
                return

            # Convert settled YES-token price to held-token space.
            if pos.side in ("YES", "BUY_YES", "UP"):
                exit_mid = _pm_yes_price
            else:
                exit_mid = 1.0 - _pm_yes_price

            unrealised = compute_unrealised_pnl(pos, exit_mid)
            log.info(
                "Monitor: RESOLVED — closing position",
                market_id=pos.market_id,
                pm_yes_price=_pm_yes_price,
                side=pos.side,
                exit_mid=exit_mid,
            )
            await self._exit_position(pos, market, exit_mid, ExitReason.RESOLVED, unrealised)
            return

        # ── Pending exit retry ─────────────────────────────────────────────────
        # When all exit attempts previously failed (EXIT_ORDER_FAILED), the
        # position is added to _pending_exit_positions for a forced retry on the
        # next monitor cycle — bypassing should_exit() so the retry fires even
        # if the market has quietened and delta no longer meets the SL threshold.
        _pending_key = f"{pos.market_id}:{pos.side}"
        if _pending_key in self._pending_exit_positions:
            _pending_reason = self._pending_exit_positions.pop(_pending_key)
            # Fetch a fresh taker price for the retry.
            _retry_book = self._pm._books.get(market.token_id_yes)
            if pos.side in ("YES", "BUY_YES", "UP"):
                _retry_price: float = (
                    _retry_book.best_bid
                    if _retry_book is not None and _retry_book.best_bid is not None
                    else pos.entry_price
                )
            else:
                _retry_book_no = self._pm._books.get(market.token_id_no)
                _retry_price = (
                    _retry_book_no.best_bid
                    if _retry_book_no is not None and _retry_book_no.best_bid is not None
                    else pos.entry_price
                )
            _retry_unrealised = compute_unrealised_pnl(pos, _retry_price)
            log.warning(
                "Monitor: retrying failed exit",
                market_id=pos.market_id[:20],
                side=pos.side,
                original_reason=_pending_reason,
                retry_price=round(_retry_price, 4),
            )
            self._exiting_positions.add(_pending_key)
            await self._exit_position(
                pos, market, _retry_price, _pending_reason, _retry_unrealised,
                force_taker=True,
            )
            return

        # ── Standard check ─────────────────────────────────────────────────────
        # S2: Check data feed freshness for momentum positions before any exit gate.
        # Logs warnings on stale/missing feeds and triggers REST fallbacks when configured.
        if pos.strategy == "momentum":
            self._check_position_data_freshness(pos)

        # current_price is always the YES-token mid — used for the P&L formula
        # and non-momentum exit conditions (both store entry_price in YES-space).
        # For momentum exits on NO positions we additionally fetch the actual
        # NO CLOB book so stop-loss / take-profit trigger on the correct price.
        book = self._pm._books.get(market.token_id_yes)
        if book is None or book.mid is None:
            if pos.strategy != "momentum":
                # Non-momentum positions need book data for all exit types.
                log.debug("Monitor: no book data yet", market_id=pos.market_id, token_id=market.token_id_yes)
                return
            # Momentum: YES book drained (common near expiry), but the oracle-spot-based
            # delta SL must still fire — it requires only the oracle spot + pos.strike,
            # NOT the CLOB book.  Use entry_price as a stub for the P&L calculation;
            # the actual fill price is determined by place_market in _exit_position.
            log.debug(
                "Monitor: YES book empty for momentum position — delta SL still active",
                market_id=pos.market_id,
            )
            current_price = pos.entry_price
        else:
            current_price = book.mid  # YES-space mid; used for P&L formula
        initial_deviation = self._initial_deviations.get(
            pos.market_id, config.MISPRICING_THRESHOLD
        )

        # For momentum positions evaluate exit triggers against the HELD token's
        # own CLOB mid, not a price derived from the opposite side's book.
        current_token_price: Optional[float] = None
        book_no = None
        if pos.strategy == "momentum":
            if pos.side in ("YES", "BUY_YES", "UP"):
                # YES/UP CLOB mid; None when book is drained (delta SL still active via oracle spot).
                current_token_price = book.mid if book is not None else None
                # ML-C1: also fetch the opposite (NO) book for exit snapshot logging.
                # In-memory lookup only — no network cost.
                book_no = self._pm._books.get(market.token_id_no)
            else:
                book_no = self._pm._books.get(market.token_id_no)
                if book_no is not None and book_no.mid is not None:
                    current_token_price = book_no.mid
                else:
                    # NO/DOWN book unavailable — leave current_token_price as None.
                    # should_exit will skip momentum exit checks this tick rather
                    # than firing on a price derived from the independent first-token book.
                    log.debug(
                        "Monitor: NO/DOWN book unavailable — skipping exit check this tick",
                        market_id=pos.market_id,
                    )

        # E3: if triggered by a live WS tick on the held token, override the
        # book-fetched current_token_price with the fresh value directly.
        # pos.token_id is the CLOB token ID of the held side (YES or NO).
        if (
            pos.strategy == "momentum"
            and triggering_token_id is not None
            and triggering_mid is not None
            and triggering_token_id == pos.token_id
        ):
            current_token_price = triggering_mid

        tte_seconds: Optional[float] = (
            (market.end_date - now).total_seconds()
            if market.end_date is not None else None
        )

        # Fetch live spot price for delta-based stop-loss.
        # get_mid() is a synchronous in-memory cache read — no await needed.
        # Route to the correct oracle: Chainlink for 5m/15m/4h markets (these
        # resolve against the Chainlink oracle), RTDS exchange-aggregated for
        # 1h/daily/weekly markets.
        # NOTE: range positions also need current_spot — their SL (in should_exit)
        # uses spot vs range bounds; without it the SL can never evaluate.
        current_spot: Optional[float] = None
        if pos.strategy in ("momentum", "range") and self._spot is not None and pos.underlying:
            if (
                config.MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY
                and tte_seconds is not None
                and tte_seconds < config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
            ):
                # Phase B: AggregatorV3-only near-expiry — matches Polymarket's
                # settlement contract which calls latestRoundData(), not the relay.
                current_spot = self._spot.get_mid_resolution_oracle(
                    pos.underlying, pos.market_type
                )
            else:
                current_spot = self._spot.get_mid(pos.underlying, pos.market_type)

        coin_sl = config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get(
            pos.underlying, config.MOMENTUM_DELTA_STOP_LOSS_PCT
        ) if pos.underlying else config.MOMENTUM_DELTA_STOP_LOSS_PCT
        # Use SpotOracle.get_spot_age so the age tracks the settlement-correct
        # feed: RTDS for 1h/daily/weekly, Chainlink for 5m/15m/4h.  This ensures
        # S2.4/S2.5 fire when the relevant feed is stale, not masked by the other.
        if self._spot is not None and pos.underlying:
            _raw_age = self._spot.get_spot_age(pos.underlying, pos.market_type)
            oracle_age_secs = None if math.isinf(_raw_age) else _raw_age
        else:
            oracle_age_secs = None

        # ── Early Warning SL signal computation ──────────────────────────────
        # Signal A: HL perp mark price (already arriving via webData2 WS).
        # Always collected for tick logging even when ENABLED=False — should_exit()
        # gates on the ENABLED flag separately.
        _hl_mark_price: Optional[float] = None
        if (
            pos.strategy == "momentum"
            and self._hl is not None
            and pos.underlying
        ):
            _hl_mark_price = self._hl.get_mark_price(pos.underlying) if hasattr(self._hl, "get_mark_price") else None

        # Signal B: HL perp depth imbalance — adjusted for position side.
        # raw imbalance: +1=all bids, -1=all asks.  Position-adjusted: negative means
        # market is positioned against this trade.
        # Always collected for tick logging even when ENABLED=False.
        _hl_position_imbalance: Optional[float] = None
        if (
            pos.strategy == "momentum"
            and self._hl is not None
            and pos.underlying
        ):
            _raw_imbalance = self._hl.get_depth_imbalance(pos.underlying) if hasattr(self._hl, "get_depth_imbalance") else None
            if _raw_imbalance is not None:
                # UP/YES: heavy asks (negative raw) means market offered against position.
                # DOWN/NO: heavy bids (positive raw) means market bid against position.
                if pos.side in ("YES", "BUY_YES", "UP"):
                    _hl_position_imbalance = _raw_imbalance
                else:
                    _hl_position_imbalance = -_raw_imbalance

        exit_flag, reason, unrealised = should_exit(
            pos=pos,
            current_price=current_price,
            initial_deviation=initial_deviation,
            market_end_date=market.end_date,
            now=now,
            current_token_price=current_token_price,
            tte_seconds=tte_seconds,
            current_spot=current_spot,
            delta_sl_pct=coin_sl,
            oracle_age_seconds=oracle_age_secs,
            suppress_counts=self._exit_suppress_counts,
            hl_mark_price=_hl_mark_price,
            hl_position_imbalance=_hl_position_imbalance,
        )

        # Hysteresis: suppress delta SL until it holds for MOMENTUM_DELTA_SL_MIN_TICKS
        # consecutive ticks. Prevents single-tick noise from blipping below threshold.
        if reason == ExitReason.MOMENTUM_STOP_LOSS:
            self._delta_sl_ticks[pos.market_id] = self._delta_sl_ticks.get(pos.market_id, 0) + 1
            if self._delta_sl_ticks[pos.market_id] < config.MOMENTUM_DELTA_SL_MIN_TICKS:
                log.debug(
                    "Monitor: delta SL below threshold — waiting for hysteresis",
                    market_id=pos.market_id[:20],
                    ticks=self._delta_sl_ticks[pos.market_id],
                    required=config.MOMENTUM_DELTA_SL_MIN_TICKS,
                )
                exit_flag, reason = False, ""
        else:
            # Only reset counter when we had a valid spot reading that did NOT
            # trigger the SL — meaning delta genuinely recovered above threshold.
            # If current_spot is None (oracle temporarily unavailable) we leave
            # the counter in place so a brief data gap can't clear a pending SL.
            if current_spot is not None and pos.strike > 0:
                self._delta_sl_ticks.pop(pos.market_id, None)

        # ── M-13: cl_upfrac rolling-window exit ──────────────────────────────
        # AUC 0.703 — strongest predictor in the dataset (strategy_update.md §1.7).
        # Sampled once per MOMENTUM_UPFRAC_WINDOW_SECONDS (not per oracle tick).
        # Uses a rolling count of up-ticks over the last WINDOW_SECONDS rather than
        # per-tick EWMA — this matches the plan's "raw fraction over last N ticks"
        # intent and gives a stable ~8-16 tick sample at oracle tick rates.
        # Counter increments per window-boundary evaluation, not per tick, so
        # WINDOWS=2 means a true 2×WINDOW_SECONDS = 10 seconds minimum dwell.
        #
        # SUPPRESS_UNTIL_ENTRY_WINDOW: when enabled, upfrac is skipped while the
        # position's TTE is still above the entry window for its market type.
        # Prevents stale pre-promotion signal from firing on ON-promoted positions.
        if (
            not exit_flag
            and pos.strategy == "momentum"
            and config.MOMENTUM_UPFRAC_EXIT_ENABLED
            and self._oracle_tracker is not None
        ):
            # High-prob suppression: skip upfrac when token is at/above the 90c
            # threshold — same gate as delta SL / prob SL in should_exit().
            _suppress_above_upfrac = getattr(config, "MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE", 0.0)
            _upfrac_suppress = (
                _suppress_above_upfrac > 0.0
                and current_token_price is not None
                and current_token_price >= _suppress_above_upfrac
            )
            if not _upfrac_suppress and getattr(config, "MOMENTUM_UPFRAC_SUPPRESS_UNTIL_ENTRY_WINDOW", False):
                _upfrac_min_tte = (
                    config.MOMENTUM_MIN_TTE_SECONDS.get(
                        pos.market_type, config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
                    ) if isinstance(config.MOMENTUM_MIN_TTE_SECONDS, dict)
                    else config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
                )
                _upfrac_suppress = tte_seconds is not None and tte_seconds >= _upfrac_min_tte
            if not _upfrac_suppress:
                _window_secs = getattr(config, "MOMENTUM_UPFRAC_WINDOW_SECONDS", 5)
                _now = time.time()
                _last_ts = self._upfrac_window_ts.get(pos.market_id)
                if _last_ts is None:
                    # First visit for this position: seed the clock and skip.
                    # This ensures the first real window starts WINDOW_SECONDS after
                    # entry, not immediately (default=0 would make epoch delta always True).
                    self._upfrac_window_ts[pos.market_id] = _now
                    _due = False
                else:
                    _due = _now - _last_ts >= _window_secs
                if _due:
                    self._upfrac_window_ts[pos.market_id] = _now
                    _upfrac = self._oracle_tracker.get_upfrac_rolling(
                        pos.underlying or "", window_secs=float(_window_secs)
                    )
                    if _upfrac is not None:
                        self._last_upfrac[pos.market_id] = _upfrac  # ML-D1: cache for snapshot
                        _threshold = config.MOMENTUM_UPFRAC_EXIT_THRESHOLD
                        _below = (
                            _upfrac < _threshold
                            if pos.side in ("YES", "UP", "BUY_YES")
                            else _upfrac > (1.0 - _threshold)
                        )
                        if _below:
                            self._upfrac_below_count[pos.market_id] = (
                                self._upfrac_below_count.get(pos.market_id, 0) + 1
                            )
                        else:
                            self._upfrac_below_count[pos.market_id] = 0  # reset on recovery
                        if (
                            self._upfrac_below_count.get(pos.market_id, 0)
                            >= config.MOMENTUM_UPFRAC_EXIT_WINDOWS
                        ):
                            exit_flag = True
                            reason = ExitReason.MOMENTUM_UPFRAC_EXIT
                            log.info(
                                "Monitor: upfrac exit triggered",
                                market_id=pos.market_id[:20],
                                upfrac=round(_upfrac, 4),
                                threshold=_threshold,
                                windows=self._upfrac_below_count[pos.market_id],
                            )

        # Write every intra-hold price check to momentum_ticks.csv for momentum
        # and range positions. Kept out of bot.log to avoid noise; the dedicated
        # CSV is easy to filter and analyse for calibrating exit stop thresholds.
        # Guarded by MOMENTUM_TICKS_LOG_ENABLED so it can be disabled in production.
        if pos.strategy in ("momentum", "range") and getattr(config, "MOMENTUM_TICKS_LOG_ENABLED", True):
            # Dedup: skip the write when the full market state (oracle spot + CLOB
            # token price) hasn't changed since the last tick for this market.
            # Multiple oracle sources (chainlink_streams, rtds_chainlink, rtds, PM WS)
            # can fire within the same millisecond with identical prices, exploding the
            # CSV with hundreds of identical rows per position hold.  Using a composite
            # (spot, token_price) key prevents duplicates even when two oracle sources
            # carry a slightly different cached price for the same underlying oracle round.
            # Always write when exit_flag is True so SL/TP ticks are never suppressed.
            _tick_state = (current_spot, current_token_price)
            if not exit_flag and _tick_state == self._last_tick_state.get(pos.market_id):
                pass  # identical market state — skip this tick
            else:
                _tick_delta: Optional[float] = None
                if current_spot is not None:
                    if pos.strategy == "range" and pos.range_lo > 0 and pos.range_hi > 0:
                        # Range positions: delta = distance from nearest boundary (positive = inside range).
                        _mid = pos.strike if pos.strike > 0 else (pos.range_lo + pos.range_hi) / 2
                        if pos.side in ("YES", "BUY_YES", "UP"):
                            if pos.range_lo <= current_spot <= pos.range_hi:
                                _tick_delta = min(
                                    current_spot - pos.range_lo,
                                    pos.range_hi - current_spot,
                                ) / _mid * 100
                            else:
                                _tick_delta = -1.0
                        else:
                            if current_spot > pos.range_hi:
                                _tick_delta = (current_spot - pos.range_hi) / _mid * 100
                            elif current_spot < pos.range_lo:
                                _tick_delta = (pos.range_lo - current_spot) / _mid * 100
                            else:
                                _tick_delta = -1.0
                    elif pos.strike > 0:
                        if pos.side in ("YES", "BUY_YES", "UP"):
                            _tick_delta = (current_spot - pos.strike) / pos.strike * 100
                        else:
                            _tick_delta = (pos.strike - current_spot) / pos.strike * 100
                _write_momentum_tick(
                    pos=pos,
                    tte_seconds=tte_seconds,
                    current_token_price=current_token_price,
                    current_spot=current_spot,
                    current_delta_pct=_tick_delta,
                    exit_flag=exit_flag,
                    reason=reason,
                    hl_mark_price=_hl_mark_price,
                    hl_position_imbalance=_hl_position_imbalance,
                )
                # ML-D1: write position snapshot for momentum positions (not range).
                # delta_sl_would_fire = raw check ignoring grace/veto suppression.
                # upfrac_below = whether the last sampled upfrac is below threshold.
                if pos.strategy == "momentum":
                    _dsl_would_fire = (
                        _tick_delta is not None and _tick_delta < coin_sl
                    )
                    _last_upfrac_val = self._last_upfrac.get(pos.market_id)
                    _upfrac_thr = config.MOMENTUM_UPFRAC_EXIT_THRESHOLD
                    _upfrac_below_flag = (
                        _last_upfrac_val is not None and (
                            _last_upfrac_val < _upfrac_thr
                            if pos.side in ("YES", "UP", "BUY_YES")
                            else _last_upfrac_val > (1.0 - _upfrac_thr)
                        )
                    )
                    _write_position_snapshot(
                        market_id=pos.market_id,
                        side=pos.side,
                        token_id=pos.token_id,
                        underlying=pos.underlying,
                        tte_seconds=tte_seconds,
                        current_token_price=current_token_price,
                        oracle_delta_pct=_tick_delta,
                        hl_mark_price=_hl_mark_price,
                        hl_depth_imbalance=_hl_position_imbalance,
                        delta_sl_would_fire=_dsl_would_fire,
                        upfrac_below=_upfrac_below_flag,
                        last_upfrac=_last_upfrac_val,
                        coin_sl=coin_sl,
                        exit_flag=exit_flag,
                        reason=reason,
                    )
                # Update dedup tracker so the next identical-state tick is skipped.
                self._last_tick_state[pos.market_id] = _tick_state
        else:
            log.debug(
                "Monitor: position check",
                market_id=pos.market_id,
                entry=round(pos.entry_price, 4),
                mid=round(current_price, 4),
                bid=round(book.best_bid, 4) if book is not None and book.best_bid is not None else None,
                ask=round(book.best_ask, 4) if book is not None and book.best_ask is not None else None,
                unrealised_usd=round(unrealised, 4),
                exit=exit_flag,
                reason=reason or "—",
            )

        if exit_flag:
            # Guard: prevent double-exit when both the poll loop and the
            # event-driven _on_price_update path reach this point concurrently.
            # asyncio is single-threaded but cooperative: a second call can arrive
            # during an await inside _exit_position before pos.is_closed is set.
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                return  # another path is already handling this exit
            self._exiting_positions.add(key)
            # M-13: clean up upfrac state so dicts don't grow indefinitely and
            # a re-entry into the same market starts with a fresh clock.
            self._upfrac_below_count.pop(pos.market_id, None)
            self._upfrac_window_ts.pop(pos.market_id, None)
            self._last_upfrac.pop(pos.market_id, None)

            # ML-C1: log exit-time snapshot for momentum SL exits (not RESOLVED/TP).
            # Written to data/mom_exit_snapshots.jsonl for Model C training data.
            _MOM_SL_EXIT_REASONS = frozenset({
                ExitReason.MOMENTUM_STOP_LOSS,
                ExitReason.MOMENTUM_NEAR_EXPIRY,
                ExitReason.MOMENTUM_PROB_STOP_LOSS,
                ExitReason.MOMENTUM_HL_MARK_SL,
                ExitReason.MOMENTUM_UPFRAC_EXIT,
            })
            if pos.strategy == "momentum" and reason in _MOM_SL_EXIT_REASONS:
                _exit_oracle_delta: Optional[float] = None
                if current_spot is not None and pos.strike > 0:
                    if pos.side in ("YES", "BUY_YES", "UP"):
                        _exit_oracle_delta = round(
                            (current_spot - pos.strike) / pos.strike * 100, 6
                        )
                    elif pos.spot_price > pos.strike:
                        # Dip-market NO/DOWN: winning = spot > strike.
                        _exit_oracle_delta = round(
                            (current_spot - pos.strike) / pos.strike * 100, 6
                        )
                    else:
                        # Reach-market / bucket NO/DOWN: winning = spot < strike.
                        _exit_oracle_delta = round(
                            (pos.strike - current_spot) / pos.strike * 100, 6
                        )
                _exit_bid_delta_pct: Optional[float] = None
                if current_token_price is not None and pos.entry_price > 0:
                    _exit_bid_delta_pct = round(
                        (current_token_price - pos.entry_price) / pos.entry_price * 100, 6
                    )
                _hl_mark_delta_pct_exit: Optional[float] = None
                if _hl_mark_price is not None and pos.strike > 0:
                    if pos.side in ("YES", "BUY_YES", "UP"):
                        _hl_mark_delta_pct_exit = round(
                            (_hl_mark_price - pos.strike) / pos.strike * 100, 6
                        )
                    else:
                        _hl_mark_delta_pct_exit = round(
                            (pos.strike - _hl_mark_price) / pos.strike * 100, 6
                        )
                # Opposite-side CLOB depth at exit: 5-level bid depth of the token
                # we are NOT holding.  Rising opposite depth while held token drains
                # indicates the crowd is actively pricing a reversal (not just settlement
                # liquidity withdrawal).  book_no is now always fetched for momentum
                # positions regardless of side (YES/UP: book_no=NO book).
                _opposite_bid_depth_usd: Optional[float] = None
                if pos.side in ("YES", "BUY_YES", "UP"):
                    _opp_book = book_no  # NO book already fetched above
                else:
                    _opp_book = book    # YES book is the opposite for NO/DOWN positions
                if _opp_book is not None and _opp_book.bids:
                    _opposite_bid_depth_usd = round(
                        sum(p * s for p, s in _opp_book.bids[:5]), 2
                    )
                _write_exit_snapshot(
                    market_id=pos.market_id,
                    side=pos.side,
                    token_id=pos.token_id,
                    underlying=pos.underlying or "",
                    market_type=pos.market_type or "",
                    exit_reason=reason,
                    entry_price=pos.entry_price,
                    entry_spot=pos.spot_price if pos.spot_price else None,
                    tte_remaining_secs=tte_seconds,
                    exit_token_mid=current_token_price,
                    bid_delta_pct=_exit_bid_delta_pct,
                    oracle_delta_pct=_exit_oracle_delta,
                    hl_mark_price=_hl_mark_price,
                    hl_mark_delta_pct=_hl_mark_delta_pct_exit,
                    hl_depth_imbalance=_hl_position_imbalance,
                    opposite_bid_depth_usd=_opposite_bid_depth_usd,
                )

            if reason == ExitReason.RESOLVED:
                # Use mid for both legs on resolution.  best_bid and best_ask straddle
                # 0.50 near expiry: bid rounds down to 0 while ask rounds up to 1,
                # making both legs of the same spread appear as losers simultaneously.
                # Mid gives a single consistent snap for YES and NO (round(mid) → 0 or 1),
                # correctly reflecting which way the market settled.
                # In live trading PM distributes at the true settlement directly;
                # paper trading uses mid as the best available proxy.
                taker_exit_price = current_price
            elif pos.side in ("YES", "BUY_YES", "UP"):
                # Pre-expiry taker exit: YES/UP sell crosses the spread at best_bid.
                # Guard book being None (YES/UP book drained when delta SL fired).
                taker_exit_price = (
                    book.best_bid
                    if book is not None and book.best_bid is not None
                    else current_price
                )
            else:
                # NO close: sell NO tokens at the actual NO CLOB best_bid.
                # YES and NO are independent CLOBs — never derive NO price from YES.
                if book_no is not None and book_no.best_bid is not None:
                    taker_exit_price = book_no.best_bid
                elif reason in (ExitReason.MOMENTUM_STOP_LOSS, ExitReason.MOMENTUM_NEAR_EXPIRY):
                    # Delta-based stop fired but NO CLOB book is also drained.
                    # Attempt a market order targeted at entry_price — place_market
                    # will cross whatever resting bids exist; worst case retries 3×.
                    # Do NOT defer: a drained book near expiry means we may never
                    # get another tick with NO book data before RESOLVED.
                    log.warning(
                        "Monitor: NO book empty on delta stop-loss — attempting market exit at entry_price",
                        market_id=pos.market_id, reason=reason,
                    )
                    taker_exit_price = pos.entry_price
                else:
                    # Price-based exits (take-profit) require a meaningful NO price.
                    # Defer to next tick — NO book will likely repopulate.
                    log.warning(
                        "Monitor: NO book unavailable for pre-expiry exit — deferring to next tick",
                        market_id=pos.market_id, reason=reason,
                    )
                    self._exiting_positions.discard(key)
                    return

            # Stop-loss exits must fill immediately — use a market (taker) order.
            # post_only limit orders on stop exits cause two problems:
            #   1. A SELL at best_bid always crosses the book → rejected → retry
            #      backs off one tick and posts a resting maker order that may
            #      never fill before resolution.
            #   2. The position is recorded as closed even if the CLOB order
            #      never fills, leaving an unhedged live position.
            is_stop = reason in (ExitReason.MOMENTUM_STOP_LOSS, ExitReason.STOP_LOSS,
                                 ExitReason.COIN_LOSS_LIMIT, ExitReason.MOMENTUM_NEAR_EXPIRY,
                                 ExitReason.MOMENTUM_TAKE_PROFIT)
            await self._exit_position(pos, market, taker_exit_price, reason, unrealised,
                                      force_taker=is_stop)

    async def _exit_position(
        self,
        pos: Position,
        market,
        exit_price: float,
        reason: str,
        unrealised_pnl: float,
        *,
        force_taker: bool = False,
    ) -> None:
        """Place a sell order and close the position in the risk engine.

        force_taker=True: uses place_market() (no post_only, crosses the spread).
        This is the correct mode for manual closes — equivalent to a market order.
        Default (False): uses place_limit() with post_only for automatic exits.
        """
        log.info(
            "Monitor: exiting position",
            market_id=pos.market_id,
            market_title=getattr(market, "title", ""),
            reason=reason,
            exit_price=round(exit_price, 4),
            entry_price=round(pos.entry_price, 4),
            expected_pnl=round(unrealised_pnl, 4),
        )

        # For auto-resolved markets PM distributes settlement directly — no
        # trade takes place and the CLOB no longer accepts orders on closed
        # markets.  Skip order placement entirely and go straight to recording
        # the close.  exit_price is snapped to the nearest settlement value.
        if reason == ExitReason.RESOLVED:
            exit_price = float(round(exit_price))  # snap to exact 0.0 or 1.0
        else:
            # Place exit SELL order (opposite side to entry).
            # exit_price is in YES-price space; convert to token price for the order.
            sell_token = (
                market.token_id_yes if pos.side in ("YES", "BUY_YES", "UP")
                else market.token_id_no
            )
            sell_order_price = exit_price  # actual token price for both YES and NO

            # CLOB wallet balance is the source of truth for sell size.
            # pos.size may be slightly wrong (e.g. set from WS size_matched or
            # a USD-budget fallback) because taker fees reduce the received
            # tokens.  Proactively fetch the actual balance so the order never
            # fails with "not enough balance".
            sell_size = pos.size
            if not config.PAPER_TRADING:
                actual_bal = await self._pm.get_token_balance(sell_token)
                if actual_bal is not None and actual_bal > 0:
                    if abs(actual_bal - pos.size) / max(pos.size, 1e-9) > 0.05:
                        log.warning(
                            "Exit: pos.size diverges from CLOB balance — using balance",
                            pos_size=round(pos.size, 6),
                            clob_balance=round(actual_bal, 6),
                            market_id=pos.market_id,
                        )
                    sell_size = actual_bal

            if force_taker:
                # Market order — crosses the spread for immediate fill (stop-loss/manual).
                # Retry up to 5 times (500 ms apart) before falling back to a GTC limit.
                order_id = None
                _gtc_floor_fallback_used: bool = False
                _gtc_floor_price: float = 0.0
                for _attempt in range(5):
                    order_id = await self._pm.place_market(
                        token_id=sell_token,
                        side="SELL",
                        price=sell_order_price,
                        size=sell_size,
                        market=market,
                    )
                    if order_id:
                        break
                    if not config.PAPER_TRADING:
                        log.warning(
                            "Monitor: market exit rejected — retrying",
                            market_id=pos.market_id,
                            attempt=_attempt + 1,
                        )
                        await asyncio.sleep(0.5)
                if order_id is None and not config.PAPER_TRADING:
                    # FAK exhausted — CLOB book has no bids.  Fall back to a GTC limit
                    # at the floor price so the order rests until liquidity returns.
                    # This is better than leaving the position unexited.
                    log.warning(
                        "Monitor: FAK exit exhausted — placing GTC limit at floor price",
                        market_id=pos.market_id,
                        reason=reason,
                    )
                    _floor_price = round(max(sell_order_price * 0.5, 0.01), 4)
                    order_id = await self._pm.place_limit(
                        token_id=sell_token,
                        side="SELL",
                        price=_floor_price,
                        size=sell_size,
                        market=market,
                        post_only=False,
                    )
                    _gtc_floor_fallback_used = order_id is not None
                    _gtc_floor_price = _floor_price
                if order_id is None and not config.PAPER_TRADING:
                    log.error(
                        "EXIT_ORDER_FAILED — manual intervention required",
                        market_id=pos.market_id, side=pos.side, reason=reason,
                    )
                    _emit_event(
                        "SELL_FAILED",
                        market_id=pos.market_id,
                        side=pos.side,
                        reason=reason,
                        exit_price=round(exit_price, 6),
                    )
                    # Register for forced retry on next monitor cycle.
                    # _check_position will bypass should_exit() and call
                    # _exit_position directly with a fresh taker price.
                    self._pending_exit_positions[f"{pos.market_id}:{pos.side}"] = reason
                    self._exiting_positions.discard(f"{pos.market_id}:{pos.side}")
                    # Pre-record the exit reason in accounting so that if the
                    # market resolves before a retry succeeds, handle_resolution()
                    # uses the correct reason instead of 'resolved'.
                    if pos.token_id:
                        try:
                            from accounting import get_ledger as _get_ledger
                            _get_ledger().set_pending_exit_reason(pos.token_id, reason)
                        except Exception:
                            pass
                    return
            else:
                # Limit order with post_only for automatic monitor exits.
                order_id = await self._pm.place_limit(
                    token_id=sell_token,
                    side="SELL",
                    price=sell_order_price,
                    size=sell_size,
                    market=market,
                )

            # In paper mode place_limit always returns a fake ID; in live mode a
            # failed post_only order (e.g. "crosses book" near settlement) is
            # retried once as a market order before giving up.
            if order_id is None and not config.PAPER_TRADING:
                log.warning(
                    "Monitor: post_only exit rejected — retrying as market order",
                    market_id=pos.market_id,
                )
                order_id = await self._pm.place_market(
                    token_id=sell_token,
                    side="SELL",
                    price=sell_order_price,
                    size=sell_size,
                    market=market,
                )
                if order_id is None:
                    log.error(
                        "EXIT_ORDER_FAILED — manual intervention required",
                        market_id=pos.market_id, side=pos.side, reason=reason,
                    )
                    _emit_event(
                        "SELL_FAILED",
                        market_id=pos.market_id,
                        side=pos.side,
                        reason=reason,
                        exit_price=round(exit_price, 6),
                    )
                    # Register for forced retry on next monitor cycle.
                    self._pending_exit_positions[f"{pos.market_id}:{pos.side}"] = reason
                    self._exiting_positions.discard(f"{pos.market_id}:{pos.side}")
                    # Pre-record the exit reason in accounting so that if the
                    # market resolves before a retry succeeds, handle_resolution()
                    # uses the correct reason instead of 'resolved'.
                    if pos.token_id:
                        try:
                            from accounting import get_ledger as _get_ledger
                            _get_ledger().set_pending_exit_reason(pos.token_id, reason)
                        except Exception:
                            pass
                    return

        # ── Taker exit: confirm actual fill price via WS / REST ──────────────
        # The taker SELL often fills at a better price than the book snapshot
        # used as sell_order_price — other resting bids above best_bid also get
        # swept.  Without confirmation we record a stale snapshot that can be
        # wrong by several cents, producing inaccurate P&L (as seen on prod:
        # bot recorded 0.750, actual fill was 0.910).
        if force_taker and order_id and not config.PAPER_TRADING:
            _fill_future: "asyncio.Future[dict]" = asyncio.get_running_loop().create_future()
            self._pm.register_fill_future(order_id, _fill_future)
            _confirmed_exit_price: Optional[float] = None
            try:
                _fill_evt = await asyncio.wait_for(_fill_future, timeout=10.0)
                _ws_price = float(_fill_evt.get("price") or 0)
                _ws_size  = float(_fill_evt.get("size_matched") or 0)
                if _ws_price > 0 and _ws_size > 0:
                    _confirmed_exit_price = _ws_price
                    log.info(
                        "Taker exit fill confirmed via WS",
                        order_id=order_id[:20],
                        book_snapshot=round(exit_price, 4),
                        actual_fill=round(_ws_price, 4),
                        fill_size=round(_ws_size, 4),
                        market_id=pos.market_id,
                    )
                else:
                    # WS event arrived but price/size fields absent — REST provides data.
                    _rest_fill = await self._pm.get_order_fill_rest(order_id)
                    if _rest_fill:
                        _confirmed_exit_price = _rest_fill["price"]
                        log.info(
                            "Taker exit fill confirmed via REST (WS no price)",
                            order_id=order_id[:20],
                            book_snapshot=round(exit_price, 4),
                            actual_fill=round(_rest_fill["price"], 4),
                            market_id=pos.market_id,
                        )
            except asyncio.TimeoutError:
                _rest_fill = await self._pm.get_order_fill_rest(order_id)
                if _rest_fill:
                    _confirmed_exit_price = _rest_fill["price"]
                    log.info(
                        "Taker exit fill confirmed via REST (WS timeout)",
                        order_id=order_id[:20],
                        book_snapshot=round(exit_price, 4),
                        actual_fill=round(_rest_fill["price"], 4),
                        market_id=pos.market_id,
                    )
                else:
                    log.warning(
                        "Taker exit fill unconfirmed — recording book snapshot price",
                        order_id=order_id[:20],
                        book_snapshot=round(exit_price, 4),
                        market_id=pos.market_id,
                    )
            if _confirmed_exit_price is not None and _confirmed_exit_price > 0:
                # GTC floor fallback: REST returns the order's limit price (= floor),
                # not the actual settlement price.  PM may have settled the GTC at a
                # much better price (e.g. resolution at 1.0).  Discard the floor price
                # so the book-snapshot exit_price is kept instead.
                if (
                    _gtc_floor_fallback_used
                    and _confirmed_exit_price <= _gtc_floor_price + 0.005
                ):
                    log.warning(
                        "Monitor: GTC floor fallback — REST returned floor limit price, "
                        "keeping book-snapshot exit price",
                        floor_price=_gtc_floor_price,
                        rest_price=_confirmed_exit_price,
                        keeping_price=round(exit_price, 4),
                        market_id=pos.market_id,
                    )
                else:
                    exit_price = _confirmed_exit_price

        # Fee model depends on exit type:
        #   RESOLVED    — auto-distribution, no trade → zero fees/rebates.
        #   post-only   — we are the maker on exit: earn rebate, pay no taker fee.
        #   force_taker — market order: we are the taker, pay full fee, earn no rebate.
        #   PAPER_TRADING — always zero fees/rebates to keep PnL as pure price delta.
        fee_base = (
            pos.size * exit_price * config.PM_FEE_COEFF * (1.0 - exit_price)
            if market.fees_enabled and not config.PAPER_TRADING else 0.0
        )
        if reason == ExitReason.RESOLVED or not market.fees_enabled or config.PAPER_TRADING:
            exit_fees = 0.0
            total_rebates = 0.0
        elif force_taker:
            # Market order: we are the taker — pay the full taker fee.
            # The maker on the other side earns the rebate, not us.
            exit_fees = fee_base
            total_rebates = 0.0
        else:
            # Post-only limit exit: we are the maker — no taker fee, earn our rebate.
            # Entry rebate was already credited by fill_simulator via record_rebate;
            # this adds only the exit-leg maker rebate to avoid double-counting.
            exit_fees = 0.0
            total_rebates = fee_base * market.rebate_pct if market.rebate_pct > 0.0 else 0.0

        _resolved_outcome = ""
        _market_end_date = getattr(market, "end_date", None)
        if reason == ExitReason.RESOLVED:
            _resolved_outcome = "WIN" if exit_price >= 0.5 else "LOSS"
            # Persist the YES-token settlement price so the webapp can show the
            # true resolution for any position in this market (including those that
            # exited early via taker/stop-loss before resolution).
            # exit_price is in held-token space; convert back to YES-token space:
            #   YES/UP position  → exit_price == YES-token price (no inversion)
            #   NO/DOWN position → exit_price == 1.0 - YES-token price (invert)
            _yes_sides_set = {"YES", "UP", "BUY_YES"}
            _yes_token_price = (
                float(round(exit_price))
                if pos.side in _yes_sides_set
                else float(round(1.0 - exit_price))
            )
            _record_market_outcome(pos.market_id, _yes_token_price)
            # Track RESOLVED exits for retroactive CLOB verification (force=True).
            # Also store market metadata so _check_pending_resolutions can retry
            # the Gamma settlement spot fetch once closePrice is available.
            _mkt_slug = getattr(market, "market_slug", "")
            _add_pending_resolution(
                pos.market_id, _market_end_date, force=True,
                underlying=pos.underlying,
                market_slug=_mkt_slug,
                market_type=pos.market_type,
            )
        else:
            # The market may still resolve after our early exit.  Track it so the
            # background resolution-check task can fill in the outcome later.
            _add_pending_resolution(pos.market_id, _market_end_date)
        # Capture the underlying spot price at exit time.
        # For RESOLVED exits use the authoritative settlement price: Gamma API
        # closePrice first (official, all bucket durations), then PM crypto-price
        # API (duration-aware variant), then live oracle mid as last resort.
        if reason == ExitReason.RESOLVED and pos.underlying:
            _exit_spot = await self._fetch_hedge_resolve_spot(pos)
        elif self._spot is not None and pos.underlying:
            _exit_spot = self._spot.get_mid(pos.underlying, pos.market_type) or 0.0
        else:
            _exit_spot = 0.0
        closed = self._risk.close_position(
            market_id=pos.market_id,
            side=pos.side,
            exit_price=exit_price,
            fees_paid=exit_fees,
            rebates_earned=total_rebates,
            resolved_outcome=_resolved_outcome,
            exit_spot_price=_exit_spot,
            exit_reason=reason,
        )
        # S2.3: remove token from priority refresh set now that position is closed
        if pos.token_id and hasattr(self._pm, "deregister_priority_token"):
            self._pm.deregister_priority_token(pos.token_id)

        # For RESOLVED exits: if the position had a hedge order that was never
        # filled (token never appeared in the wallet), write an "unfilled" record.
        # has_hedge_fill() returns True if a momentum_hedge row already exists
        # (i.e., the auto-redeem loop or a prior RESOLVED exit already wrote one).
        if reason == ExitReason.RESOLVED and closed and closed.hedge_order_id and closed.hedge_token_id:
            if not self._risk.has_hedge_fill(closed.market_id):
                # In paper mode the fill_simulator may have simulated a hedge fill.
                # If so, use that fill data and apply the correct outcome based on
                # which side won: main WON → hedge (opposite) LOST; main LOST → WON.
                _paper_fill = self._risk.get_paper_hedge_fill(closed.hedge_token_id)
                if _paper_fill is not None:
                    _main_won = exit_price >= 0.99
                    _h_status = "filled_lost" if _main_won else "filled_won"
                    _h_settled = 0.0 if _main_won else 1.0
                    self._risk.record_hedge_fill(
                        parent_market_id=closed.market_id,
                        parent_market_title=closed.market_title,
                        hedge_token_id=closed.hedge_token_id,
                        fill_price=_paper_fill["fill_price"],
                        fill_size=_paper_fill["fill_size"],
                        settled_price=_h_settled,
                        underlying=closed.underlying or "",
                        hedge_status=_h_status,
                        spot_resolve_price=_exit_spot,
                    )
                    log.info(
                        "GTD hedge — paper-simulated fill recorded on RESOLVED exit",
                        market_id=closed.market_id[:20],
                        hedge_token_id=closed.hedge_token_id[:20],
                        hedge_status=_h_status,
                        fill_price=_paper_fill["fill_price"],
                        fill_size=round(_paper_fill["fill_size"], 4),
                        spot_resolve_price=round(_exit_spot, 2),
                    )
                else:
                    # Layer WS: WS MATCHED event confirmed this hedge filled during
                    # the trade (live_fill_handler set hedge_fill_detected=True).
                    # However, PM settlement events at market resolution can generate
                    # WS fill notifications for orders that were never actually matched
                    # on the CLOB (PM settlement artifacts — Apr-23 post-mortem Bug #2).
                    # Always verify with CLOB REST before recording to prevent phantom fills.
                    _ws_fill_verified = False
                    if closed.hedge_fill_detected and closed.hedge_fill_size > 0:
                        _ws_rest_verify = await self._pm.get_order_fill_rest(closed.hedge_order_id)
                        if _ws_rest_verify and _ws_rest_verify["size_matched"] > 0:
                            _ws_fill_verified = True
                            _ws_fp = closed.hedge_fill_price if closed.hedge_fill_price > 0 else closed.hedge_price
                            _main_won_ws = exit_price >= 0.99
                            _h_status_ws = "filled_lost" if _main_won_ws else "filled_won"
                            _h_settled_ws = 0.0 if _main_won_ws else 1.0
                            self._risk.record_hedge_fill(
                                parent_market_id=closed.market_id,
                                parent_market_title=closed.market_title,
                                hedge_token_id=closed.hedge_token_id,
                                fill_price=_ws_fp,
                                fill_size=closed.hedge_fill_size,
                                settled_price=_h_settled_ws,
                                underlying=closed.underlying or "",
                                hedge_status=_h_status_ws,
                                spot_resolve_price=_exit_spot,
                            )
                            log.info(
                                "GTD hedge — WS fill recorded on RESOLVED exit (CLOB REST verified)",
                                market_id=closed.market_id[:20],
                                hedge_token_id=closed.hedge_token_id[:20],
                                hedge_status=_h_status_ws,
                                fill_price=_ws_fp,
                                fill_size=round(closed.hedge_fill_size, 4),
                            )
                        else:
                            log.warning(
                                "GTD hedge — WS fill NOT confirmed by CLOB REST on RESOLVED exit "
                                "(PM settlement artifact — falling through to REST/positions check)",
                                market_id=closed.market_id[:20],
                                hedge_order_id=closed.hedge_order_id[:20],
                                ws_fill_size=round(closed.hedge_fill_size, 4),
                            )
                    if not _ws_fill_verified:
                        # Do not write "unfilled" based only on the absence of a WS event.
                        # Query the source of truth NOW, while the CLOB order data is still
                        # fresh (order just expired at resolution — data not yet purged).
                        # Layer A: CLOB REST order lookup — fastest and most reliable at
                        # resolution time because size_matched is still populated.
                        _resolved_rest = await self._pm.get_order_fill_rest(closed.hedge_order_id)
                        if _resolved_rest and _resolved_rest["size_matched"] > 0:
                            _r_fill_price, _r_fill_size = _resolved_rest["price"], _resolved_rest["size_matched"]
                            _main_won_r = exit_price >= 0.99
                            _h_status_r = "filled_lost" if _main_won_r else "filled_won"
                            _h_settled_r = 0.0 if _main_won_r else 1.0
                            self._risk.record_hedge_fill(
                                parent_market_id=closed.market_id,
                                parent_market_title=closed.market_title,
                                hedge_token_id=closed.hedge_token_id,
                                fill_price=_r_fill_price,
                                fill_size=_r_fill_size,
                                settled_price=_h_settled_r,
                                underlying=closed.underlying or "",
                                hedge_status=_h_status_r,
                                spot_resolve_price=_exit_spot,
                            )
                            log.info(
                                "GTD hedge — fill confirmed via CLOB REST on RESOLVED exit",
                                market_id=closed.market_id[:20],
                                hedge_token_id=closed.hedge_token_id[:20],
                                hedge_status=_h_status_r,
                                fill_price=_r_fill_price,
                                fill_size=round(_r_fill_size, 4),
                            )
                        else:
                            # Layer B: PM Data API positions — source of truth for wallet.
                            # Catches fills where the CLOB order was purged or size_matched
                            # was not populated (observed in some settled markets).
                            _live_pos_r = await self._pm.get_live_positions()
                            _pos_data_r = next(
                                (
                                    p for p in _live_pos_r
                                    if (p.get("asset") or p.get("asset_id") or "") == closed.hedge_token_id
                                    and float(p.get("size") or 0) > 0
                                ),
                                None,
                            )
                            if _pos_data_r is not None:
                                _r_fill_size = float(_pos_data_r.get("size") or 0)
                                _r_fill_price = float(
                                    _pos_data_r.get("avgPrice") or _pos_data_r.get("avg_price") or closed.hedge_price
                                )
                                _main_won_r = exit_price >= 0.99
                                _h_status_r = "filled_lost" if _main_won_r else "filled_won"
                                _h_settled_r = 0.0 if _main_won_r else 1.0
                                self._risk.record_hedge_fill(
                                    parent_market_id=closed.market_id,
                                    parent_market_title=closed.market_title,
                                    hedge_token_id=closed.hedge_token_id,
                                    fill_price=_r_fill_price,
                                    fill_size=_r_fill_size,
                                    settled_price=_h_settled_r,
                                    underlying=closed.underlying or "",
                                    hedge_status=_h_status_r,
                                    spot_resolve_price=_exit_spot,
                                )
                                log.info(
                                    "GTD hedge — fill confirmed via PM positions on RESOLVED exit",
                                    market_id=closed.market_id[:20],
                                    hedge_token_id=closed.hedge_token_id[:20],
                                    hedge_status=_h_status_r,
                                    fill_price=_r_fill_price,
                                    fill_size=round(_r_fill_size, 4),
                                )
                            else:
                                # Both API sources confirm no fill.
                                self._risk.record_hedge_fill(
                                    parent_market_id=closed.market_id,
                                    parent_market_title=closed.market_title,
                                    hedge_token_id=closed.hedge_token_id,
                                    fill_price=closed.hedge_price,
                                    fill_size=0.0,
                                    settled_price=0.0,
                                    underlying=closed.underlying or "",
                                    hedge_status="unfilled",
                                    spot_resolve_price=_exit_spot,
                                )
                                log.info(
                                    "GTD hedge — confirmed unfilled on RESOLVED exit (CLOB + positions checked)",
                                    market_id=closed.market_id[:20],
                                    hedge_token_id=closed.hedge_token_id[:20],
                                    spot_resolve_price=round(_exit_spot, 2),
                                )

        # Cancel the resting TP SELL order (Item 1) on any exit that isn't the
        # TP itself.  On a TP exit the order may already be filled; cancel is
        # a no-op if so.  On RESOLVED, PM auto-expires all open orders.
        if closed and closed.tp_order_id and reason != ExitReason.RESOLVED:
            asyncio.create_task(self._pm.cancel_order(closed.tp_order_id))
            log.info(
                "TP resting order cancel submitted on position close",
                tp_order_id=closed.tp_order_id[:20],
                reason=reason,
            )

        # Release the exit guard so the slot can be reused if the same market
        # re-opens (e.g. after a restart or a new bucket round).
        self._exiting_positions.discard(f"{pos.market_id}:{pos.side}")

        if closed:
            log.info(
                "Monitor: position closed ✓",
                market_id=pos.market_id,
                realized_pnl=round(closed.realized_pnl, 4),
                reason=reason,
                hold_seconds=round(
                    (datetime.now(timezone.utc) - pos.opened_at).total_seconds(), 1
                ),
            )
            # Cancel the GTD hedge order on TP exits only.  On SL/near-expiry exits
            # the hedge is the recovery leg and must stay alive.  On RESOLVED exits
            # PM auto-expires orders so we never send an explicit cancel.
            _hedge_cancel_on_win = {ExitReason.PROFIT_TARGET, ExitReason.MOMENTUM_TAKE_PROFIT}
            if closed.hedge_order_id and reason in _hedge_cancel_on_win:
                asyncio.create_task(self._pm.cancel_order(closed.hedge_order_id))
            _emit_event(
                "SELL_CLOSE",
                market_id=pos.market_id,
                market_title=pos.market_title[:80],
                underlying=pos.underlying,
                market_type=pos.market_type,
                side=pos.side,
                entry_price=round(pos.entry_price, 6),
                exit_price=round(exit_price, 6),
                realized_pnl=round(closed.realized_pnl, 4),
                reason=reason,
            )
            self._initial_deviations.pop(pos.market_id, None)
            if self._on_close_callback is not None:
                try:
                    result = self._on_close_callback(pos.market_id)
                    # Support both sync and async callbacks
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as exc:
                    log.warning("on_close_callback raised", exc=str(exc))
            if self._on_closed_full_callback is not None:
                try:
                    self._on_closed_full_callback(
                        pos.market_id, pos.side, exit_price, pos.strategy
                    )
                except Exception as exc:
                    log.warning("on_closed_full_callback raised", exc=str(exc))
            if reason == ExitReason.MOMENTUM_STOP_LOSS and self._on_stop_loss_callback is not None:
                try:
                    _tte_rem = (
                        (market.end_date - datetime.now(timezone.utc)).total_seconds()
                        if market.end_date is not None else 0.0
                    )
                    self._on_stop_loss_callback(pos.market_id, max(0.0, _tte_rem))
                except Exception as exc:
                    log.warning("on_stop_loss_callback raised", exc=str(exc))
