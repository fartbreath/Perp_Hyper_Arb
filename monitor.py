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
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import config
from logger import get_bot_logger
from risk import RiskEngine, Position
from market_data.pm_client import PMClient, _MARKET_TYPE_DURATION_SECS
from market_data.rtds_client import RTDSClient
from market_data.spot_oracle import SpotOracle
from ctf_utils import _redeem_ctf_via_safe
from py_clob_client_v2.config import get_contract_config as _get_contract_config
_POLY_CONTRACTS = _get_contract_config(137)  # Polygon mainnet — CTF + collateral addresses
from strategies.Momentum.event_log import emit as _emit_event

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

MOMENTUM_TICKS_CSV   = _DATA_DIR / "momentum_ticks.csv"
_MOMENTUM_TICKS_HEADER = [
    "ts", "market_id", "market_title", "underlying", "side",
    "tte_s", "entry_tok", "token", "tok_drop_pct",
    "entry_spot", "spot", "entry_delta", "current_delta", "delta_retreat_pct",
    "exit", "reason",
]

# ── Hedge CLOB tick CSV ───────────────────────────────────────────────────────
# Dedicated file for CLOB price sampling of open GTD hedge limit orders.
# Sampled once per _check_all_positions() sweep while the hedge is unfilled.
# Used post-trade to understand why a hedge didn't fill (was CLOB far from bid?).
#
# Columns:
#   ts              — UTC ISO timestamp
#   market_id       — first 20 chars of condition_id
#   market_title    — human label (truncated to 60)
#   underlying      — BTC / ETH / SOL / …
#   parent_side     — side of the parent momentum position (YES/NO/UP/DOWN)
#   hedge_order_id  — PM order ID of the resting GTD hedge bid
#   hedge_token_id  — token_id of the hedge token (opposite of parent)
#   hedge_bid_price — the price at which the hedge limit bid was placed
#   clob_mid        — CLOB mid of the hedge token at sample time
#   clob_best_bid   — CLOB best bid of the hedge token at sample time
#   clob_best_ask   — CLOB best ask of the hedge token at sample time
#   tte_s           — seconds to market expiry at sample time
#   status          — HedgeOrder status (open / partially_filled / …)

HEDGE_CLOB_TICKS_CSV    = _DATA_DIR / "hedge_clob_ticks.csv"
_HEDGE_CLOB_TICKS_HEADER = [
    "ts", "market_id", "market_title", "underlying", "parent_side",
    "hedge_order_id", "hedge_token_id", "hedge_bid_price",
    "clob_mid", "clob_best_bid", "clob_best_ask", "tte_s", "status",
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
        }
        with MOMENTUM_TICKS_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_MOMENTUM_TICKS_HEADER).writerow(row)
    except Exception as _ex:
        log.debug("_write_momentum_tick failed", exc=str(_ex))  # never let tick logging crash the monitor


def _ensure_hedge_clob_ticks_csv() -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    if not HEDGE_CLOB_TICKS_CSV.exists():
        with HEDGE_CLOB_TICKS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_HEDGE_CLOB_TICKS_HEADER).writeheader()


def _write_hedge_clob_tick(
    market_id: str,
    market_title: str,
    underlying: str,
    parent_side: str,
    hedge_order_id: str,
    hedge_token_id: str,
    hedge_bid_price: float,
    clob_mid: Optional[float],
    clob_best_bid: Optional[float],
    clob_best_ask: Optional[float],
    tte_s: Optional[float],
    status: str,
) -> None:
    """Append one CLOB price sample for an open GTD hedge order to hedge_clob_ticks.csv."""
    try:
        _ensure_hedge_clob_ticks_csv()
        row = {
            "ts":               datetime.now(timezone.utc).isoformat(),
            "market_id":        market_id[:20],
            "market_title":     market_title[:60],
            "underlying":       underlying,
            "parent_side":      parent_side,
            "hedge_order_id":   hedge_order_id[:20],
            "hedge_token_id":   hedge_token_id[:20] if hedge_token_id else "",
            "hedge_bid_price":  round(hedge_bid_price, 4),
            "clob_mid":         round(clob_mid, 4) if clob_mid is not None else "",
            "clob_best_bid":    round(clob_best_bid, 4) if clob_best_bid is not None else "",
            "clob_best_ask":    round(clob_best_ask, 4) if clob_best_ask is not None else "",
            "tte_s":            round(tte_s, 1) if tte_s is not None else "",
            "status":           status,
        }
        with HEDGE_CLOB_TICKS_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_HEDGE_CLOB_TICKS_HEADER).writerow(row)
    except Exception as _ex:
        log.debug("_write_hedge_clob_tick failed", exc=str(_ex))


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
    hedge_active: bool = False,
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
        # Delta SL runs FIRST — requires only spot+strike, NOT token_price.
        # This must evaluate even when the NO CLOB book is drained near expiry
        # (book drain sets current_token_price=None for NO positions, which
        # previously blocked this block entirely — causing missed stop-losses).
        _oracle_delta_pct: Optional[float] = None  # captured below for prob-SL gate
        # Hedge suppression: when MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL is True and
        # the position has a GTD hedge order that has at least partially filled,
        # suppress ALL stop-losses (oracle delta SL, near-expiry stop, and prob-SL).
        # The hedge is the insurance leg — it bounds the downside, so any SL exit
        # would lock in a loss before the hedge can pay off at resolution.
        # Take-profit remains active (winning path, hedge irrelevant).
        # IMPORTANT: suppress ONLY if the hedge has actually filled (size_filled > 0).
        # An unfilled/cancelled hedge provides zero insurance — suppressing SLs
        # in that state leaves the position naked with no protection.
        _suppress_all_sl = (
            config.MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL and hedge_active
        )
        if current_spot is not None and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                # Long YES/UP: profit when spot > strike.  Delta positive when in-the-money.
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            elif pos.spot_price > pos.strike:
                # Long NO/DOWN on a "dip" market (e.g. "Will ETH dip to $X?"):
                # NO wins when spot STAYS ABOVE the strike.
                # Entry spot was above strike → winning delta = (spot - strike) / strike.
                # pos.spot_price defaults to 0.0 so this branch only activates for live
                # dip-market positions where the scanner stored the actual entry spot.
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            else:
                # Long NO/DOWN on a "reach" market (e.g. "Will ETH reach $X?"):
                # NO wins when spot STAYS BELOW the strike.
                # Entry spot was at or below strike → winning delta = (strike - spot) / strike.
                current_delta_pct = (pos.strike - current_spot) / pos.strike * 100
            # PROTECTIVE BUFFER: fire when delta drops below +SL_PCT, i.e. while
            # still in-the-money but within SL_PCT% of the strike.  With
            # SL_PCT=0.1 the bot exits when still 0.1% ahead, before the oracle
            # crosses and Polymarket resolves against the position.
            _sl = delta_sl_pct if delta_sl_pct is not None else config.MOMENTUM_DELTA_STOP_LOSS_PCT
            _oracle_delta_pct = current_delta_pct  # capture for prob-SL gate below
            if not _suppress_all_sl and current_delta_pct < _sl:
                return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised
            # Near-expiry: only exit if spot has already crossed the strike
            # (delta < 0).  Avoids premature exits from CLOB price collapse.
            if (
                not _suppress_all_sl
                and tte_seconds is not None
                and tte_seconds < config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
                and current_delta_pct < 0
            ):
                return True, ExitReason.MOMENTUM_NEAR_EXPIRY, unrealised

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
        if (
            not _suppress_all_sl
            and _prob_sl_oracle_ok
            and _prob_sl_tte_ok
            and config.MOMENTUM_PROB_SL_ENABLED
            and pos.prob_sl_threshold > 0.0
            and token_price < pos.prob_sl_threshold
        ):
            return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised

        # Take-profit: still CLOB-based (converging to 1.0 at resolution).
        if token_price >= config.MOMENTUM_TAKE_PROFIT:
            return True, ExitReason.MOMENTUM_TAKE_PROFIT, unrealised
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
        on_close_callback: Optional[Callable[[str], None]] = None,
        on_stop_loss_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self._pm = pm
        self._risk = risk
        self._spot = spot_client  # SpotOracle facade; routes to correct oracle per market type
        self._interval = interval
        # Called with market_id whenever a position is successfully closed.
        # Used by MispricingScanner to reset the per-market cooldown clock.
        self._on_close_callback = on_close_callback
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
        log.info("PositionMonitor started (event-driven)")
        asyncio.create_task(self._auto_redeem_loop())
        if not config.PAPER_TRADING:
            asyncio.create_task(self._pm_reconcile_loop())
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
            await self._check_position(pos, triggering_token_id=token_id, triggering_mid=mid)

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
            await self._check_position(pos)

        # Check deferred hedge cancels: cancel the resting GTD hedge once delta
        # has recovered above the threshold (confirming the SL was a false positive).
        for ho in list(self._risk.get_hedge_orders_with_pending_cancel()):
            if ho.pending_cancel_side == "" or ho.pending_cancel_strike <= 0:
                continue
            # Rebuild market_id-level state from the HedgeOrder entity
            market_id = ho.market_id
            underlying = ho.underlying
            if underlying != coin:
                continue
            strike = ho.pending_cancel_strike
            if ho.pending_cancel_side in ("YES", "BUY_YES", "UP"):
                current_delta = (price - strike) / strike * 100
            elif ho.pending_cancel_entry_spot > strike:
                current_delta = (price - strike) / strike * 100
            else:
                current_delta = (strike - price) / strike * 100
            if current_delta >= ho.pending_cancel_threshold:
                log.info(
                    "GTD hedge cancel: delta recovered above threshold",
                    market_id=market_id[:20],
                    underlying=coin,
                    current_delta=round(current_delta, 4),
                    cancel_threshold=round(ho.pending_cancel_threshold, 4),
                    hedge_order_id=ho.order_id[:20],
                )
                cancel_ok = await self._pm.cancel_order(ho.order_id)

                if not cancel_ok and ho.token_id:
                    # cancel_order returns False when the order is already gone (404).
                    # Check WS-tracked fills first (most reliable source).
                    if ho.size_filled > 0:
                        # WS/update_hedge_fill already confirmed a fill.
                        log.warning(
                            "GTD hedge already filled (WS-confirmed) — market-exiting",
                            market_id=market_id[:20],
                            hedge_token_id=ho.token_id[:20],
                            fill_price=ho.avg_fill_price,
                            fill_size=round(ho.size_filled, 4),
                        )
                        exit_order_id = await self._pm.place_market(
                            token_id=ho.token_id,
                            side="SELL",
                            price=0.0,
                            size=ho.size_filled,
                        )
                        if exit_order_id:
                            log.info(
                                "GTD hedge market-exit order placed",
                                exit_order_id=str(exit_order_id)[:20],
                                hedge_token_id=ho.token_id[:20],
                            )
                        if ho.status not in ("filled_exited", "cancelled", "filled_won", "filled_lost"):
                            self._risk.finalize_hedge(
                                ho.order_id,
                                settled_price=0.0,
                                spot_at_resolution=price,
                                hedge_status="filled_exited",
                            )
                    else:
                        # WS didn't see a fill — query CLOB REST to confirm.
                        fill_data = await self._pm.get_order_fill_rest(ho.order_id)
                        if fill_data:
                            live_fill_price = fill_data["price"]
                            live_fill_size = fill_data["size_matched"]
                            log.warning(
                                "GTD hedge was already filled — market-exiting",
                                market_id=market_id[:20],
                                hedge_token_id=ho.token_id[:20],
                                fill_price=live_fill_price,
                                fill_size=round(live_fill_size, 4),
                            )
                            exit_order_id = await self._pm.place_market(
                                token_id=ho.token_id,
                                side="SELL",
                                price=0.0,
                                size=live_fill_size,
                            )
                            if exit_order_id:
                                log.info(
                                    "GTD hedge market-exit order placed",
                                    exit_order_id=str(exit_order_id)[:20],
                                    hedge_token_id=ho.token_id[:20],
                                )
                            # Sync fill state into HedgeOrder before finalizing
                            self._risk.update_hedge_fill(
                                ho.order_id, live_fill_price, live_fill_size, "clob_rest"
                            )
                            self._risk.finalize_hedge(
                                ho.order_id,
                                settled_price=0.0,
                                spot_at_resolution=price,
                                hedge_status="filled_exited",
                            )
                        else:
                            # Cancel returned False but no fill found — treat as cancelled.
                            log.info(
                                "GTD hedge cancel returned False but no fill found — treating as cancelled",
                                market_id=market_id[:20],
                            )
                            if ho.token_id:
                                self._risk.finalize_hedge(
                                    ho.order_id,
                                    settled_price=0.0,
                                    spot_at_resolution=price,
                                    hedge_status="cancelled",
                                )
                else:
                    # Cancel succeeded (or paper mode) — record as cancelled.
                    # But if WS fills were already tracked, honour them with the correct
                    # status so the frontend shows the true P&L rather than "$0.00".
                    from risk import HedgeStatus
                    if ho.status not in HedgeStatus.TERMINAL:
                        if ho.size_filled > 0:
                            # Cancel confirmed after partial or full fill.
                            # Delta-recovery fires when main is winning → hedge token (opposite) lost.
                            _fill_ratio = ho.size_filled / ho.order_size if ho.order_size > 0 else 1.0
                            _fill_status = "filled_lost" if _fill_ratio >= 0.99 else "cancelled_partial"
                            self._risk.finalize_hedge(
                                ho.order_id,
                                settled_price=0.0,
                                spot_at_resolution=price,
                                hedge_status=_fill_status,
                            )
                            log.info(
                                "GTD hedge cancel-ok (delta-recovery) but WS fills detected — recorded fill outcome",
                                hedge_order_id=ho.order_id[:20],
                                size_filled=round(ho.size_filled, 4),
                                order_size=round(ho.order_size, 4),
                                status=_fill_status,
                            )
                        else:
                            self._risk.finalize_hedge(
                                ho.order_id,
                                settled_price=0.0,
                                spot_at_resolution=price,
                                hedge_status="cancelled",
                            )

                # Resolve market_title from position or HedgeOrder
                _market_title = ho.market_title or ""
                _emit_event(
                    "HEDGE_CANCEL",
                    market_id=market_id,
                    market_title=_market_title[:80],
                    hedge_order_id=ho.order_id,
                    reason="delta_recovered",
                )
                self._risk.clear_pending_cancel(ho.order_id)

    async def _record_pending_resolution_hedge(
        self, condition_id: str, resolved_yes_price: float
    ) -> None:
        """Write momentum_hedge outcome for a taker-closed position if missing.

        Pending-resolution checks run after early exits (taker/stop/near-expiry).
        If the position had a GTD hedge order and no momentum_hedge row exists
        yet, resolve it now using paper fill simulation data when available.
        """
        if self._risk.has_hedge_fill(condition_id):
            return

        candidates = [
            p for p in self._risk.get_positions().values()
            if (
                p.market_id == condition_id
                and p.is_closed
                and p.hedge_order_id
                and p.hedge_token_id
            )
        ]
        if not candidates:
            return

        parent = max(candidates, key=lambda p: p.closed_at or p.opened_at)
        main_settled = (
            resolved_yes_price if parent.side in {"YES", "UP"}
            else (1.0 - resolved_yes_price)
        )
        main_won = main_settled >= 0.5

        spot_resolve = await self._fetch_hedge_resolve_spot(parent)
        paper_fill = self._risk.get_paper_hedge_fill(parent.hedge_token_id)
        if paper_fill is not None:
            hedge_status = "filled_lost" if main_won else "filled_won"
            hedge_settled = 0.0 if main_won else 1.0
            fill_price = float(paper_fill.get("fill_price") or parent.hedge_price)
            fill_size = float(paper_fill.get("fill_size") or 0.0)
            if fill_size > 0.0:
                ho = self._risk.get_hedge_order(parent.hedge_order_id)
                if ho is not None:
                    self._risk.update_hedge_fill(
                        parent.hedge_order_id, fill_price, fill_size, "paper"
                    )
                    self._risk.finalize_hedge(
                        parent.hedge_order_id,
                        settled_price=hedge_settled,
                        spot_at_resolution=spot_resolve,
                        hedge_status=hedge_status,
                    )
                else:
                    # Legacy: no HedgeOrder entity — fall back to direct CSV write
                    self._risk.record_hedge_fill(
                        parent_market_id=parent.market_id,
                        parent_market_title=parent.market_title,
                        hedge_token_id=parent.hedge_token_id,
                        fill_price=fill_price,
                        fill_size=fill_size,
                        settled_price=hedge_settled,
                        underlying=parent.underlying or "",
                        hedge_status=hedge_status,
                        spot_resolve_price=spot_resolve,
                    )
                log.info(
                    "Pending resolution: GTD hedge fill recorded",
                    condition_id=condition_id[:16],
                    hedge_token_id=parent.hedge_token_id[:16],
                    hedge_status=hedge_status,
                    fill_price=fill_price,
                    fill_size=round(fill_size, 4),
                )
                _emit_event(
                    "HEDGE_FILL",
                    market_id=parent.market_id,
                    market_title=parent.market_title[:80],
                    hedge_token_id=parent.hedge_token_id,
                    hedge_order_id=parent.hedge_order_id,
                    hedge_status=hedge_status,
                    fill_price=fill_price,
                    fill_size=round(fill_size, 4),
                    source="paper",
                )
                return

        # Fallback: HedgeOrder entity tracks WS fills via update_hedge_fill()
        ho = self._risk.get_hedge_order(parent.hedge_order_id)
        if ho is not None and ho.size_filled > 0.0:
            hedge_status = "filled_lost" if main_won else "filled_won"
            hedge_settled = 0.0 if main_won else 1.0
            self._risk.finalize_hedge(
                parent.hedge_order_id,
                settled_price=hedge_settled,
                spot_at_resolution=spot_resolve,
                hedge_status=hedge_status,
            )
            log.info(
                "Pending resolution: GTD hedge fill recorded (HedgeOrder entity)",
                condition_id=condition_id[:16],
                hedge_token_id=parent.hedge_token_id[:16],
                hedge_status=hedge_status,
                fill_price=ho.avg_fill_price,
                fill_size=round(ho.size_filled, 4),
            )
            _emit_event(
                "HEDGE_FILL",
                market_id=parent.market_id,
                market_title=parent.market_title[:80],
                hedge_token_id=parent.hedge_token_id,
                hedge_order_id=parent.hedge_order_id,
                hedge_status=hedge_status,
                fill_price=ho.avg_fill_price,
                fill_size=round(ho.size_filled, 4),
                source="ws",
            )
            return

        # Layer 3: query CLOB REST API to check if the hedge order was
        # filled while the bot was offline or before WS detection was added.
        if parent.hedge_order_id:
            rest_fill = await self._pm.get_order_fill_rest(parent.hedge_order_id)
            if rest_fill:
                rest_fill_price = rest_fill["price"]
                rest_fill_size = rest_fill["size_matched"]
                if rest_fill_size > 0.0:
                    hedge_status = "filled_lost" if main_won else "filled_won"
                    hedge_settled = 0.0 if main_won else 1.0
                    if ho is not None:
                        self._risk.update_hedge_fill(
                            parent.hedge_order_id, rest_fill_price, rest_fill_size, "clob_rest"
                        )
                        self._risk.finalize_hedge(
                            parent.hedge_order_id,
                            settled_price=hedge_settled,
                            spot_at_resolution=spot_resolve,
                            hedge_status=hedge_status,
                        )
                    else:
                        self._risk.record_hedge_fill(
                            parent_market_id=parent.market_id,
                            parent_market_title=parent.market_title,
                            hedge_token_id=parent.hedge_token_id,
                            fill_price=rest_fill_price,
                            fill_size=rest_fill_size,
                            settled_price=hedge_settled,
                            underlying=parent.underlying or "",
                            hedge_status=hedge_status,
                            spot_resolve_price=spot_resolve,
                        )
                    log.info(
                        "Pending resolution: GTD hedge fill recorded (REST fallback)",
                        condition_id=condition_id[:16],
                        hedge_token_id=parent.hedge_token_id[:16],
                        hedge_status=hedge_status,
                        fill_price=rest_fill_price,
                        fill_size=round(rest_fill_size, 4),
                    )
                    _emit_event(
                        "HEDGE_FILL",
                        market_id=parent.market_id,
                        market_title=parent.market_title[:80],
                        hedge_token_id=parent.hedge_token_id,
                        hedge_order_id=parent.hedge_order_id,
                        hedge_status=hedge_status,
                        fill_price=rest_fill_price,
                        fill_size=round(rest_fill_size, 4),
                        source="clob_rest",
                    )
                    return

        # Layer 4: PM Data API positions — source of truth for what the wallet
        # actually holds. Catches fills missed by WS and CLOB REST (e.g. when
        # the event loop was blocked during fill dispatch, or CLOB order lookup
        # returned size_matched=0 despite the fill having settled on-chain).
        live_positions = await self._pm.get_live_positions()
        pos_data = next(
            (
                p for p in live_positions
                if (p.get("asset") or p.get("asset_id") or "") == parent.hedge_token_id
                and float(p.get("size") or 0) > 0
            ),
            None,
        )
        if pos_data is not None:
            fill_size = float(pos_data.get("size") or 0)
            fill_price = float(
                pos_data.get("avgPrice") or pos_data.get("avg_price") or parent.hedge_price
            )
            hedge_status = "filled_lost" if main_won else "filled_won"
            hedge_settled = 0.0 if main_won else 1.0
            if ho is not None:
                self._risk.update_hedge_fill(
                    parent.hedge_order_id, fill_price, fill_size, "reconciliation"
                )
                self._risk.finalize_hedge(
                    parent.hedge_order_id,
                    settled_price=hedge_settled,
                    spot_at_resolution=spot_resolve,
                    hedge_status=hedge_status,
                )
            else:
                self._risk.record_hedge_fill(
                    parent_market_id=parent.market_id,
                    parent_market_title=parent.market_title,
                    hedge_token_id=parent.hedge_token_id,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    settled_price=hedge_settled,
                    underlying=parent.underlying or "",
                    hedge_status=hedge_status,
                    spot_resolve_price=spot_resolve,
                )
            log.info(
                "Pending resolution: GTD hedge fill recorded (PM positions — source of truth)",
                condition_id=condition_id[:16],
                hedge_token_id=parent.hedge_token_id[:16],
                hedge_status=hedge_status,
                fill_price=fill_price,
                fill_size=round(fill_size, 4),
            )
            _emit_event(
                "HEDGE_FILL",
                market_id=parent.market_id,
                market_title=parent.market_title[:80],
                hedge_token_id=parent.hedge_token_id,
                hedge_order_id=parent.hedge_order_id,
                hedge_status=hedge_status,
                fill_price=fill_price,
                fill_size=round(fill_size, 4),
                source="reconciliation",
            )
            return

        # All layers exhausted — mark as unfilled
        if ho is not None:
            self._risk.finalize_hedge(
                parent.hedge_order_id,
                settled_price=0.0,
                spot_at_resolution=spot_resolve,
                hedge_status="unfilled",
            )
        else:
            self._risk.record_hedge_fill(
                parent_market_id=parent.market_id,
                parent_market_title=parent.market_title,
                hedge_token_id=parent.hedge_token_id,
                fill_price=parent.hedge_price,
                fill_size=0.0,
                settled_price=0.0,
                underlying=parent.underlying or "",
                hedge_status="unfilled",
                spot_resolve_price=spot_resolve,
            )
        log.info(
            "Pending resolution: GTD hedge marked unfilled",
            condition_id=condition_id[:16],
            hedge_token_id=parent.hedge_token_id[:16],
        )
        _emit_event(
            "HEDGE_EXPIRED",
            market_id=parent.market_id,
            market_title=parent.market_title[:80],
            hedge_token_id=parent.hedge_token_id,
            hedge_order_id=parent.hedge_order_id,
            hedge_price=parent.hedge_price,
        )

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
                try:
                    await self._record_pending_resolution_hedge(cid, resolved_yes_price)
                except Exception as exc:
                    log.warning(
                        "record_pending_resolution_hedge failed",
                        cid=cid[:16],
                        exc=str(exc),
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

    # ── PM reconciliation ─────────────────────────────────────────────────────

    async def _pm_reconcile_loop(self) -> None:
        """Periodically reconcile trades.csv against Polymarket Data API.

        Runs every 5 minutes in live mode.  Patches trades.csv with actual fill
        prices and PnL from data-api.polymarket.com/activity — correcting the two
        known recording bugs:
          1. Entry price stored as order price instead of actual CLOB fill price.
          2. TP-sell exits recorded as WIN at $1 when the position was sold early.
        """
        # Initial delay: let startup restore and first trades settle
        await asyncio.sleep(90)
        while self._running:
            try:
                from pm_reconcile import reconcile_trades_csv
                result = await reconcile_trades_csv(config.POLY_FUNDER)
                if result.get("patched", 0) > 0:
                    log.info(
                        "PM reconciliation: trades.csv patched",
                        patched=result["patched"],
                        markets=[m["market_title"][:40] for m in result.get("markets", [])],
                    )
                if result.get("errors"):
                    log.warning("PM reconciliation errors", errors=result["errors"])
            except Exception as exc:
                log.error("PM reconcile loop error", exc=str(exc))
            await asyncio.sleep(300)  # every 5 minutes

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

        # ── Hedge CLOB tick logging + gap-closing reprice ──────────────────────
        # For every open GTD hedge order:
        #   1. Log CLOB state (if enabled).
        #   2. Near-expiry cancel: if TTE ≤ MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS and
        #      the HELD token mid > 0.50 (we are winning), cancel the hedge — the
        #      insurance is no longer needed.
        #   3. Gap-closing reprice: if best_ask FELL since last sweep (seller coming
        #      toward us), cancel + repost hedge at current_bid + $0.01.  If ask is
        #      flat or rising (seller moved away), hold.  Capped by ho.price_cap.
        from risk import HedgeStatus as _HedgeStatus
        _tick = 0.01
        _expiry_cancel_secs = getattr(config, "MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS", 5)
        for ho in self._risk.get_open_hedge_orders():
            if not ho.token_id:
                continue
            try:
                market = self._pm._markets.get(ho.market_id)
                book   = self._pm._books.get(ho.token_id)
                clob_mid: Optional[float] = None
                clob_bid: Optional[float] = None
                clob_ask: Optional[float] = None
                if book is not None:
                    clob_bid = book.best_bid
                    clob_ask = book.best_ask
                    if clob_bid is not None and clob_ask is not None:
                        clob_mid = (clob_bid + clob_ask) / 2
                    elif clob_bid is not None:
                        clob_mid = clob_bid
                    elif clob_ask is not None:
                        clob_mid = clob_ask
                tte_s: Optional[float] = None
                if market is not None and market.end_date is not None:
                    tte_s = (market.end_date - datetime.now(timezone.utc)).total_seconds()

                if getattr(config, "MOMENTUM_HEDGE_CLOB_LOG_ENABLED", True):
                    _write_hedge_clob_tick(
                        market_id=ho.market_id,
                        market_title=ho.market_title or "",
                        underlying=ho.underlying or "",
                        parent_side=ho.parent_side or "",
                        hedge_order_id=ho.order_id,
                        hedge_token_id=ho.token_id,
                        hedge_bid_price=ho.order_price or 0.0,
                        clob_mid=clob_mid,
                        clob_best_bid=clob_bid,
                        clob_best_ask=clob_ask,
                        tte_s=tte_s,
                        status=ho.status,
                    )

                # ── Near-expiry cancel ────────────────────────────────────────
                if (
                    _expiry_cancel_secs > 0
                    and tte_s is not None
                    and 0 < tte_s <= _expiry_cancel_secs
                ):
                    # Check held-token mid to confirm we are winning.
                    _parent_pos = self._risk.get_position_for_hedge(ho.order_id)
                    _held_mid: Optional[float] = None
                    if _parent_pos is not None and _parent_pos.token_id:
                        _held_book = self._pm._books.get(_parent_pos.token_id)
                        if _held_book is not None:
                            if _held_book.best_bid is not None and _held_book.best_ask is not None:
                                _held_mid = (_held_book.best_bid + _held_book.best_ask) / 2
                            elif _held_book.best_bid is not None:
                                _held_mid = _held_book.best_bid
                    if _held_mid is not None and _held_mid > 0.50:
                        log.info(
                            "Hedge near-expiry cancel: position winning, cancelling insurance",
                            market_id=ho.market_id[:20],
                            tte_s=round(tte_s, 1),
                            held_mid=round(_held_mid, 3),
                            hedge_order_id=ho.order_id[:20],
                        )
                        _cancel_ok = await self._pm.cancel_order(ho.order_id)
                        if _cancel_ok:
                            self._risk.finalize_hedge(
                                ho.order_id,
                                settled_price=0.0,
                                hedge_status=_HedgeStatus.CANCELLED,
                            )
                        else:
                            # Already filled / gone — reconcile on next sweep
                            log.debug(
                                "Hedge near-expiry cancel: cancel_order returned False",
                                hedge_order_id=ho.order_id[:20],
                            )
                        continue  # no reprice after cancel

                # ── Gap-closing reprice ───────────────────────────────────────
                if clob_ask is None:
                    # No ask data — update last_ask and skip.
                    with self._risk._lock:
                        ho.last_clob_ask = None
                    continue

                _prev_ask = ho.last_clob_ask

                # Always update the stored ask for next sweep.
                with self._risk._lock:
                    ho.last_clob_ask = clob_ask

                if _prev_ask is None:
                    # First sweep for this order — record baseline, nothing to compare.
                    continue

                # Seller moved toward us (ask fell)?
                if clob_ask >= _prev_ask:
                    continue  # flat or rising — hold

                _current_bid = ho.order_price or 0.0
                _new_bid     = round(_current_bid + _tick, 4)
                _price_cap   = ho.price_cap

                if _price_cap > 0 and _new_bid > _price_cap:
                    log.debug(
                        "Hedge reprice: price cap reached, holding",
                        market_id=ho.market_id[:20],
                        current_bid=_current_bid,
                        new_bid=_new_bid,
                        price_cap=_price_cap,
                    )
                    continue

                # Cancel existing order and repost one tick higher.
                log.info(
                    "Hedge reprice: ask fell — stepping bid up",
                    market_id=ho.market_id[:20],
                    prev_ask=round(_prev_ask, 4),
                    current_ask=round(clob_ask, 4),
                    old_bid=_current_bid,
                    new_bid=_new_bid,
                    price_cap=_price_cap,
                    hedge_order_id=ho.order_id[:20],
                )
                _cancel_ok = await self._pm.cancel_order(ho.order_id)
                if not _cancel_ok:
                    # Order already filled or gone — do not replace.
                    log.debug(
                        "Hedge reprice: cancel returned False (likely already filled)",
                        hedge_order_id=ho.order_id[:20],
                    )
                    continue

                # Reprice sizing rules:
                #   1. PM minimum order = $1, so we always spend at least $1.
                #   2. Goal is to match the NATURAL (position-matched) contract count.
                #   3. If natural coverage costs < $1 at the new price: use $1 worth
                #      of contracts (PM floor — fewer contracts than the inflated
                #      original placement, spending exactly $1).
                #   4. If natural coverage costs >= $1 at the new price: use natural
                #      contracts (full hedge coverage, possibly > $1).
                #   5. Hard ceiling: total spend never exceeds projected win PnL minus
                #      MIN_RETAIN_USD — maintained by the PnL cap block below.
                #
                # ho.natural_contracts is the position-matched count stored at placement
                # (entry_size × HEDGE_CONTRACTS_PCT, before any $1 floor inflation).
                # Fallback for old hedges without this field: use ho.order_size which
                # was the inflated count — preserves pre-fix behaviour on legacy objects.
                _nat = ho.natural_contracts if ho.natural_contracts > 0.0 else max(0.0, ho.order_size - ho.size_filled)
                _natural_cost = _nat * _new_bid
                if _natural_cost >= 1.0:
                    _reprice_size = _nat              # full position coverage
                else:
                    _reprice_size = 1.0 / _new_bid    # PM $1 minimum at new price
                if ho.projected_pnl_usd > 0.0:
                    # Use the same retained-profit floor as placement: ceiling is
                    # projected_pnl minus MIN_RETAIN_USD, not the full projected_pnl.
                    # This keeps the hedge cost constraint consistent end-to-end.
                    _pnl_budget  = max(0.0, ho.projected_pnl_usd - config.MOMENTUM_HEDGE_MIN_RETAIN_USD)
                    _pnl_ceiling = _pnl_budget / _new_bid if _pnl_budget > 0.0 else 0.0
                    if _reprice_size > _pnl_ceiling:
                        log.debug(
                            "Hedge reprice: capping size to projected PnL ceiling",
                            uncapped_size=round(_reprice_size, 4),
                            capped_size=round(_pnl_ceiling, 4),
                            notional_usd=round(_pnl_ceiling * _new_bid, 4),
                            projected_pnl=round(ho.projected_pnl_usd, 4),
                            min_retain=config.MOMENTUM_HEDGE_MIN_RETAIN_USD,
                            pnl_budget=round(_pnl_budget, 4),
                            market_id=ho.market_id[:20],
                        )
                        _reprice_size = _pnl_ceiling
                _reprice_size = round(_reprice_size, 6)

                _new_order_id = await self._pm.place_limit(
                    token_id=ho.token_id,
                    side="BUY",
                    price=_new_bid,
                    size=_reprice_size,
                    market=market,
                    post_only=True,
                )
                if _new_order_id:
                    self._risk.replace_hedge_order(ho.order_id, _new_order_id, _new_bid, new_order_size=_reprice_size)
                    log.info(
                        "Hedge reprice: new order placed",
                        new_order_id=_new_order_id[:20],
                        new_bid=_new_bid,
                        reprice_size=_reprice_size,
                        notional_usd=round(_reprice_size * _new_bid, 4),
                        market_id=ho.market_id[:20],
                    )
                else:
                    # Placement failed — old order cancelled, no hedge resting.
                    # replace_hedge_order was not called, so ho still references the
                    # cancelled order.  Finalize it as cancelled so the next sweep
                    # does not retry a dead order_id.
                    self._risk.finalize_hedge(
                        ho.order_id,
                        settled_price=0.0,
                        hedge_status=_HedgeStatus.CANCELLED,
                    )
                    log.warning(
                        "Hedge reprice: placement failed after cancel — hedge lost",
                        market_id=ho.market_id[:20],
                        new_bid=_new_bid,
                    )

            except Exception as _hex:
                log.debug("Hedge CLOB tick/reprice failed", order_id=ho.order_id[:20], exc=str(_hex))

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
        _last_tick = self._last_oracle_tick_ts.get(pos.underlying or "")
        oracle_age_secs = (
            time.monotonic() - _last_tick if _last_tick is not None else None
        )
        # Determine hedge-active flag here (in the method, where self._risk is available)
        # so the free function should_exit does not need to access self._risk directly.
        _hedge_active = False
        if pos.hedge_order_id:
            _ho = self._risk.get_hedge_order(pos.hedge_order_id)
            _hedge_active = _ho is not None and _ho.size_filled > 0

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
            hedge_active=_hedge_active,
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
        fee_base = (
            pos.size * exit_price * config.PM_FEE_COEFF * (1.0 - exit_price)
            if market.fees_enabled else 0.0
        )
        if reason == ExitReason.RESOLVED or not market.fees_enabled:
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
        )

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

        # Cancel the resting GTD hedge order on WIN exits only.
        # Rule: cancel hedge ONLY when the main position won (take-profit) — the
        # insurance leg is no longer needed.  On loss/stop/near-expiry exits, keep
        # the hedge alive: it acts as the recovery leg if spot reverses, and catches
        # a genuine fill if the market resolves against the main position.
        # Special case (MOMENTUM_STOP_LOSS): if MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT > 0,
        # set up a deferred cancel — fire once delta recovers to confirm a false-positive SL.
        # RESOLVED exits are excluded: PM auto-expires all open orders at resolution.
        _hedge_cancel_on_win = {ExitReason.PROFIT_TARGET, ExitReason.MOMENTUM_TAKE_PROFIT}
        if closed and closed.hedge_order_id and reason != ExitReason.RESOLVED:
            _recovery_pct = config.MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT
            _defer_cancel = (
                reason == ExitReason.MOMENTUM_STOP_LOSS
                and _recovery_pct > 0.0
                and closed.hedge_token_id
                and not self._risk.has_hedge_fill(closed.market_id)
                and closed.strike > 0
                and closed.underlying
            )
            if _defer_cancel:
                # Compute the cancel threshold: fire cancel once delta recovers to
                # SL_threshold × (1 + recovery_pct).  Uses the same coin SL that
                # triggered this exit so the threshold is symmetric.
                _coin_sl = (
                    config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get(
                        closed.underlying, config.MOMENTUM_DELTA_STOP_LOSS_PCT
                    )
                    if closed.underlying else config.MOMENTUM_DELTA_STOP_LOSS_PCT
                )
                _cancel_threshold = _coin_sl * (1.0 + _recovery_pct)
                # Register the deferred cancel on the HedgeOrder entity (persisted).
                _ho = self._risk.get_hedge_order(closed.hedge_order_id)
                if _ho is not None:
                    self._risk.set_pending_cancel(
                        closed.hedge_order_id,
                        threshold=_cancel_threshold,
                        side=closed.side,
                        strike=closed.strike,
                        entry_spot=closed.spot_price,
                    )
                else:
                    # Legacy: no HedgeOrder entity — the deferred cancel will not
                    # fire via get_hedge_orders_with_pending_cancel().  Log a warning.
                    log.warning(
                        "GTD hedge deferred cancel: no HedgeOrder entity found — cancel may be missed on restart",
                        hedge_order_id=closed.hedge_order_id[:20],
                        market_id=closed.market_id[:20],
                    )
                log.info(
                    "GTD hedge cancel deferred — awaiting delta recovery",
                    hedge_order_id=closed.hedge_order_id[:20],
                    coin_sl_pct=round(_coin_sl, 4),
                    cancel_threshold=round(_cancel_threshold, 4),
                    recovery_pct=_recovery_pct,
                )
            elif reason in _hedge_cancel_on_win:
                # Await the cancel so we can act on the response.
                # Do NOT write "cancelled" before we know what the API says.
                _cancel_ok = await self._pm.cancel_order(closed.hedge_order_id)
                log.info(
                    "GTD hedge order cancel submitted",
                    hedge_order_id=closed.hedge_order_id[:20],
                    cancel_ok=_cancel_ok,
                    reason=reason,
                )
                if not _cancel_ok:
                    # Cancel returned False — order is already gone (expired or filled).
                    # Query CLOB REST immediately to determine which case it is.
                    _win_rest = await self._pm.get_order_fill_rest(closed.hedge_order_id)
                    if _win_rest and _win_rest["size_matched"] > 0:
                        _wf_price = _win_rest["price"]
                        _wf_size  = _win_rest["size_matched"]
                        # Main won (WIN exit) → hedge token (opposite side) lost.
                        _ho = self._risk.get_hedge_order(closed.hedge_order_id)
                        if _ho is not None and _ho.status not in ("filled_lost", "filled_exited", "filled_exited"):
                            self._risk.update_hedge_fill(
                                closed.hedge_order_id, _wf_price, _wf_size, "clob_rest"
                            )
                            self._risk.finalize_hedge(
                                closed.hedge_order_id,
                                settled_price=0.0,
                                spot_at_resolution=_exit_spot,
                                hedge_status="filled_lost",
                            )
                        elif closed.hedge_token_id and not self._risk.has_hedge_fill(closed.market_id):
                            self._risk.record_hedge_fill(
                                parent_market_id=closed.market_id,
                                parent_market_title=closed.market_title,
                                hedge_token_id=closed.hedge_token_id,
                                fill_price=_wf_price,
                                fill_size=_wf_size,
                                settled_price=0.0,
                                underlying=closed.underlying or "",
                                hedge_status="filled_lost",
                                spot_resolve_price=_exit_spot,
                            )
                        log.info(
                            "GTD hedge filled before cancel arrived — recorded filled_lost",
                            hedge_order_id=closed.hedge_order_id[:20],
                            fill_price=_wf_price,
                            fill_size=round(_wf_size, 4),
                        )
                    else:
                        # CLOB confirms no fill — order expired/cancelled by PM.
                        _ho = self._risk.get_hedge_order(closed.hedge_order_id)
                        if _ho is not None and _ho.status not in ("cancelled", "filled_won", "filled_lost", "filled_exited"):
                            self._risk.finalize_hedge(
                                closed.hedge_order_id,
                                settled_price=0.0,
                                spot_at_resolution=_exit_spot,
                                hedge_status="cancelled",
                            )
                        elif closed.hedge_token_id and not self._risk.has_hedge_fill(closed.market_id):
                            self._risk.record_hedge_fill(
                                parent_market_id=closed.market_id,
                                parent_market_title=closed.market_title,
                                hedge_token_id=closed.hedge_token_id,
                                fill_price=closed.hedge_price,
                                fill_size=0.0,
                                settled_price=0.0,
                                underlying=closed.underlying or "",
                                hedge_status="cancelled",
                                spot_resolve_price=_exit_spot,
                            )
                        log.info(
                            "GTD hedge cancel failed but CLOB confirms no fill — recorded cancelled",
                            hedge_order_id=closed.hedge_order_id[:20],
                        )
                else:
                    # Cancel succeeded — but PM cancel-ok can fire on a fully-consumed
                    # order (nothing remaining to cancel).  If WS fill detection already
                    # recorded fills, honour them rather than overwriting with "cancelled".
                    _ho = self._risk.get_hedge_order(closed.hedge_order_id)
                    if _ho is not None and _ho.status not in ("cancelled", "filled_won", "filled_lost", "filled_exited", "cancelled_partial"):
                        if _ho.size_filled > 0:
                            # WS fills exist: order was consumed before cancel arrived.
                            # Main position WON → hedge token (opposite side) LOST.
                            _fill_ratio = _ho.size_filled / _ho.order_size if _ho.order_size > 0 else 1.0
                            _fill_status = "filled_lost" if _fill_ratio >= 0.99 else "cancelled_partial"
                            self._risk.finalize_hedge(
                                closed.hedge_order_id,
                                settled_price=0.0,
                                spot_at_resolution=_exit_spot,
                                hedge_status=_fill_status,
                            )
                            log.info(
                                "GTD hedge cancel-ok but WS fills detected — recorded fill outcome",
                                hedge_order_id=closed.hedge_order_id[:20],
                                size_filled=round(_ho.size_filled, 4),
                                order_size=round(_ho.order_size, 4),
                                status=_fill_status,
                            )
                        else:
                            self._risk.finalize_hedge(
                                closed.hedge_order_id,
                                settled_price=0.0,
                                spot_at_resolution=_exit_spot,
                                hedge_status="cancelled",
                            )
                    elif closed.hedge_token_id and not self._risk.has_hedge_fill(closed.market_id):
                        self._risk.record_hedge_fill(
                            parent_market_id=closed.market_id,
                            parent_market_title=closed.market_title,
                            hedge_token_id=closed.hedge_token_id,
                            fill_price=closed.hedge_price,
                            fill_size=0.0,
                            settled_price=0.0,
                            underlying=closed.underlying or "",
                            hedge_status="cancelled",
                            spot_resolve_price=_exit_spot,
                        )
                _emit_event(
                    "HEDGE_CANCEL",
                    market_id=pos.market_id,
                    market_title=pos.market_title[:80],
                    hedge_order_id=closed.hedge_order_id,
                    reason=str(reason),
                )

        # Clean up any pending deferred hedge cancel when the market resolves —
        # PM auto-expires the resting order at resolution; no explicit cancel needed.
        if reason == ExitReason.RESOLVED and pos.hedge_order_id:
            self._risk.clear_pending_cancel(pos.hedge_order_id)

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
            if reason == ExitReason.MOMENTUM_STOP_LOSS and self._on_stop_loss_callback is not None:
                try:
                    _tte_rem = (
                        (market.end_date - datetime.now(timezone.utc)).total_seconds()
                        if market.end_date is not None else 0.0
                    )
                    self._on_stop_loss_callback(pos.market_id, max(0.0, _tte_rem))
                except Exception as exc:
                    log.warning("on_stop_loss_callback raised", exc=str(exc))
