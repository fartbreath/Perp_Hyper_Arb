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
        if pos.spot_price > 0 and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                entry_delta = round((pos.spot_price - pos.strike) / pos.strike * 100, 6)
            else:
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


def _add_pending_resolution(condition_id: str, end_date: Optional[datetime]) -> None:
    """Track a market whose resolution we haven't seen yet (taker/stop exit)."""
    if not condition_id:
        return
    pending = _load_pending_resolutions()
    if any(e["condition_id"] == condition_id for e in pending):
        return  # already tracked
    end_date_iso = end_date.isoformat() if end_date else ""
    pending.append({"condition_id": condition_id, "end_date_iso": end_date_iso, "checked": False})
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
        if current_spot is not None and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES", "UP"):
                # Long YES/UP: profit when spot > strike.  Delta positive when in-the-money.
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            else:
                # Long NO/DOWN: profit when spot < strike.  Delta positive when in-the-money.
                current_delta_pct = (pos.strike - current_spot) / pos.strike * 100
            # PROTECTIVE BUFFER: fire when delta drops below +SL_PCT, i.e. while
            # still in-the-money but within SL_PCT% of the strike.  With
            # SL_PCT=0.1 the bot exits when still 0.1% ahead, before the oracle
            # crosses and Polymarket resolves against the position.
            _sl = delta_sl_pct if delta_sl_pct is not None else config.MOMENTUM_DELTA_STOP_LOSS_PCT
            if current_delta_pct < _sl:
                return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised
            # Near-expiry: only exit if spot has already crossed the strike
            # (delta < 0).  Avoids premature exits from CLOB price collapse.
            if (
                tte_seconds is not None
                and tte_seconds < config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
                and current_delta_pct < 0
            ):
                return True, ExitReason.MOMENTUM_NEAR_EXPIRY, unrealised

        # Token-price exits (take-profit) require the held token's CLOB mid.
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
        for pos in self._risk.get_open_positions():
            if pos.strategy != "momentum" or pos.underlying != coin:
                continue
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                continue
            await self._check_position(pos)

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
            # Skip if already recorded from a RESOLVED exit.
            existing = _load_market_outcomes().get(cid)
            if existing is not None:
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
                )
                # Back-fill resolved_outcome in trades.csv for SL/taker exits
                # that never received an outcome (resolved_outcome is blank).
                # force=False so we don't override outcomes already set by
                # _redeem_ready_positions() or the RESOLVED fast-path.
                try:
                    patched = self._risk.patch_trade_outcome(
                        cid, resolved_yes_price, force=False
                    )
                    if patched:
                        log.info(
                            "patch_trade_outcome: filled outcome for SL exit(s)",
                            condition_id=cid[:16], patched=patched,
                        )
                except Exception as exc:
                    log.warning(
                        "patch_trade_outcome failed", cid=cid[:16], exc=str(exc)
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

    async def _redeem_ready_positions(self) -> None:
        """Fetch wallet positions and submit on-chain redemption for each redeemable token.

        Live API is the source of truth:
        - redeemable=False  → oracle still pending, skip
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
                continue  # Oracle still pending — nothing to do yet

            size = float(pos_data.get("size", 0) or 0)
            cur_price = float(
                pos_data.get("curPrice") or pos_data.get("currentPrice") or pos_data.get("cur_price") or 0
            )
            payout = round(size * cur_price, 4)
            title = pos_data.get("title") or token_id[:20]
            condition_id: str = pos_data.get("conditionId") or pos_data.get("condition_id") or ""

            # Genuine loser — nothing to redeem on-chain
            if payout == 0:
                self._redeemed_tokens.add(token_id)
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

            if not condition_id:
                log.warning("Auto-redeem: no condition_id — will retry next cycle", token_id=token_id[:20])
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
            markets_snap = self._pm.get_markets()
            for mkt in markets_snap.values():
                if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                    # cur_price=1.0 for a settled winner
                    # YES token=1  → YES won  → resolved_yes_price=1.0
                    # NO  token=1  → NO  won  → resolved_yes_price=0.0
                    is_yes_token = (mkt.token_id_yes == token_id)
                    pm_resolved_yes = 1.0 if is_yes_token else 0.0
                    for rp in list(self._risk.get_positions().values()):
                        if rp.market_id == mkt.condition_id and not rp.is_closed:
                            self._risk.close_position(
                                mkt.condition_id, exit_price=cur_price, side=rp.side,
                                resolved_outcome="WIN",
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
                ctf_address = self._pm._clob.get_conditional_address()
                collateral_address = self._pm._clob.get_collateral_address()
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
            # ── Determine exit_mid — three levels, most authoritative first ───
            #
            # L1: PM CLOB settlement API.
            #     GET /markets/{condition_id} returns closed=True and each
            #     token's settled price (0.0 or 1.0) once PM has processed the
            #     oracle resolution.  This is PM's own statement of the outcome
            #     and is more authoritative than either the order book or our
            #     local oracle snapshot.
            #
            # L2: Oracle spot vs. strike (momentum only).
            #     The Chainlink / RTDS oracle can reflect the settlement price
            #     before the CLOB market object is updated to closed=True.  This
            #     catches the window where L1 is still None.
            #
            # L3: CLOB book mid (last resort).
            #     At the exact settlement second the book may show ~$1.00 for a
            #     token that will settle to $0.  Used only when L1 and L2 both
            #     fail (e.g. no oracle data and PM API unavailable).
            #     _redeem_ready_positions() acts as a final safety-net in live
            #     mode — it re-closes with the correct payout from the Data API.

            exit_mid: Optional[float] = None

            # L1 — PM CLOB market resolution
            _pm_yes_price = await self._pm.fetch_market_resolution(pos.market_id)
            if _pm_yes_price is not None:
                # _pm_yes_price is the settled YES/UP token price (0.0 or 1.0).
                # Convert to price-of-held-token space so the existing snap and
                # WIN/LOSS logic in _exit_position works correctly.
                if pos.side in ("YES", "BUY_YES", "UP"):
                    exit_mid = _pm_yes_price
                else:
                    exit_mid = 1.0 - _pm_yes_price
                log.info(
                    "Monitor: RESOLVED — PM CLOB settlement confirms outcome",
                    market_id=pos.market_id,
                    pm_yes_price=_pm_yes_price,
                    side=pos.side,
                    exit_mid=exit_mid,
                )

            # L2 — oracle spot vs. strike (momentum positions; fills the gap
            # before the CLOB market object is marked closed=True)
            if (
                exit_mid is None
                and pos.strategy == "momentum"
                and pos.strike > 0
                and self._spot is not None
                and pos.underlying
            ):
                _res_spot = self._spot.get_mid(pos.underlying, pos.market_type)
                if _res_spot is not None and _res_spot > 0:
                    if pos.side in ("YES", "BUY_YES", "UP"):
                        exit_mid = 1.0 if _res_spot > pos.strike else 0.0
                    else:
                        exit_mid = 1.0 if _res_spot < pos.strike else 0.0
                    log.info(
                        "Monitor: RESOLVED momentum — oracle vs. strike (PM API not yet closed)",
                        market_id=pos.market_id,
                        spot=round(_res_spot, 4),
                        strike=round(pos.strike, 4),
                        side=pos.side,
                        oracle_exit_mid=exit_mid,
                    )

            # L3 — CLOB book mid
            book = self._pm._books.get(market.token_id_yes)
            if exit_mid is None:
                if pos.side in ("YES", "BUY_YES", "UP"):
                    exit_mid = (
                        book.mid
                        if book is not None and book.mid is not None
                        else pos.entry_price
                    )
                else:
                    book_no_res = self._pm._books.get(market.token_id_no)
                    if book_no_res is not None and book_no_res.mid is not None:
                        exit_mid = book_no_res.mid
                    else:
                        log.warning(
                            "Monitor: NO/DOWN book gone at resolution — using entry_price (zero P&L)",
                            market_id=pos.market_id,
                        )
                        exit_mid = pos.entry_price

            unrealised = compute_unrealised_pnl(pos, exit_mid)
            log.info(
                "Monitor: resolved market — closing position",
                market_id=pos.market_id,
                exit_mid=round(exit_mid, 4),
                book_available=book is not None and book.mid is not None,
            )
            await self._exit_position(pos, market, exit_mid, ExitReason.RESOLVED, unrealised)
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
        current_spot: Optional[float] = None
        if pos.strategy == "momentum" and self._spot is not None and pos.underlying:
            current_spot = self._spot.get_mid(pos.underlying, pos.market_type)

        coin_sl = config.MOMENTUM_DELTA_SL_PCT_BY_COIN.get(
            pos.underlying, config.MOMENTUM_DELTA_STOP_LOSS_PCT
        ) if pos.underlying else config.MOMENTUM_DELTA_STOP_LOSS_PCT
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
        # positions. Kept out of bot.log to avoid noise; the dedicated CSV is
        # easy to filter and analyse for calibrating exit stop thresholds.
        if pos.strategy == "momentum":
            _tick_delta: Optional[float] = None
            if current_spot is not None and pos.strike > 0:
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
                # Retry up to 3 times (200 ms apart) before giving up.
                order_id = None
                for _attempt in range(3):
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
                        await asyncio.sleep(0.2)
                if order_id is None and not config.PAPER_TRADING:
                    log.error(
                        "EXIT_ORDER_FAILED — manual intervention required",
                        market_id=pos.market_id, side=pos.side, reason=reason,
                    )
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
                    self._exiting_positions.discard(f"{pos.market_id}:{pos.side}")
                    return

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
        if reason == ExitReason.RESOLVED:
            _resolved_outcome = "WIN" if exit_price >= 0.5 else "LOSS"
            # Persist the exact YES-price settlement so the webapp can show the
            # true resolution for any position in this market (including those that
            # exited early via taker/stop-loss before resolution).
            _record_market_outcome(pos.market_id, float(round(exit_price)))
        else:
            # The market may still resolve after our early exit.  Track it so the
            # background resolution-check task can fill in the outcome later.
            _market_end_date = getattr(market, "end_date", None)
            _add_pending_resolution(pos.market_id, _market_end_date)
        # Capture the underlying spot price at exit time.
        # Use the same oracle that the market resolves against: Chainlink for
        # 5m/15m/4h markets, RTDS exchange-aggregated for 1h/daily/weekly.
        if self._spot is not None and pos.underlying:
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
