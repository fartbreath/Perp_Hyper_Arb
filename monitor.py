"""
monitor.py — Position exit monitor.

Runs as an asyncio task, checking all open positions every MONITOR_INTERVAL
seconds.  Exit logic is **strategy-isolated**: each strategy has its own
explicit exit conditions and no position can be exited by a rule belonging to
a different strategy.

Strategy exit rules
-------------------
  * **maker**   — TIME_STOP for non-bucket (milestone/daily) markets within
                  MAKER_EXIT_HOURS of expiry.  Bucket positions hold to
                  RESOLVED.  No per-position profit target or stop-loss.
  * **momentum** — token-price exits (MOMENTUM_STOP_LOSS / MOMENTUM_TAKE_PROFIT).
                  No time-stop; the min-TTE entry gate already places entries
                  close to expiry.
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
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

import config
from logger import get_bot_logger
from risk import RiskEngine, Position
from market_data.pm_client import PMClient, _MARKET_TYPE_DURATION_SECS
from market_data.pyth_client import PythClient
from ctf_utils import _redeem_ctf_via_safe

log = get_bot_logger(__name__)


# ── Exit reason constants ─────────────────────────────────────────────────────

class ExitReason:
    PROFIT_TARGET          = "profit_target"
    STOP_LOSS              = "stop_loss"
    TIME_STOP              = "time_stop"
    RESOLVED               = "resolved"
    COIN_LOSS_LIMIT        = "coin_loss_limit"       # maker: aggregate coin P&L exceeded threshold
    MOMENTUM_STOP_LOSS     = "momentum_stop_loss"    # momentum: held token fell below MOMENTUM_STOP_LOSS
    MOMENTUM_TAKE_PROFIT   = "momentum_take_profit"  # momentum: held token rose above MOMENTUM_TAKE_PROFIT
    MOMENTUM_NEAR_EXPIRY   = "momentum_near_expiry"  # momentum: near expiry and in loss territory


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

    # ── Momentum exits — delta-based (live HL spot vs strike) ─────────────────
    if pos.strategy == "momentum":
        # Delta SL runs FIRST — requires only spot+strike, NOT token_price.
        # This must evaluate even when the NO CLOB book is drained near expiry
        # (book drain sets current_token_price=None for NO positions, which
        # previously blocked this block entirely — causing missed stop-losses).
        if current_spot is not None and pos.strike > 0:
            if pos.side in ("YES", "BUY_YES"):
                # Long YES: profit when spot > strike.  Delta negative when spot < strike.
                current_delta_pct = (current_spot - pos.strike) / pos.strike * 100
            else:
                # Long NO: profit when spot < strike.  Delta negative when spot > strike.
                current_delta_pct = (pos.strike - current_spot) / pos.strike * 100
            if current_delta_pct < -config.MOMENTUM_DELTA_STOP_LOSS_PCT:
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
        if pos.side in ("YES", "BUY_YES"):
            # Use actual YES CLOB mid; fall back to YES-space current_price.
            token_price = current_token_price if current_token_price is not None else current_price
        else:
            # Use actual NO CLOB mid.  Do NOT derive from the YES side —
            # YES and NO are independent CLOBs and 1-p_yes ≠ p_no in general.
            # If the NO book is unavailable, skip token-price exits this tick.
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
        pyth_client: Optional[PythClient] = None,
        on_close_callback: Optional[Callable[[str], None]] = None,
        on_stop_loss_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self._pm = pm
        self._risk = risk
        self._pyth = pyth_client  # PythClient; authoritative spot price for momentum SL
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
        # Token IDs for which an on-chain redemption has already been submitted
        # this session (avoids re-submitting on every poll cycle).
        self._redeemed_tokens: set[str] = set()
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
        # Event-driven delta SL: also check on every Pyth price tick so that a
        # rapid underlying move triggers the delta SL immediately, without
        # waiting for the next PM WS tick (which may not come if the PM book
        # goes quiet near expiry — exactly when the SL matters most).
        if self._pyth is not None:
            self._pyth.on_price_update(self._on_pyth_spot_update)
        log.info("PositionMonitor started (event-driven)")
        asyncio.create_task(self._auto_redeem_loop())
        # Rare backstop sweep: catches resolved markets that PM WS never echoes
        # (e.g. resolution during a transient disconnect) and any other edge-case
        # positions that slipped through.  300 s is acceptable for non-real-time
        # cleanup; all live position checks are driven by PM / Pyth events above.
        while self._running:
            await asyncio.sleep(300)
            try:
                await self._check_all_positions()
            except Exception as exc:
                log.error("PositionMonitor backstop sweep failed", exc=str(exc))

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

    async def _on_pyth_spot_update(self, coin: str, price: float) -> None:
        """Triggered on every Pyth price tick.

        Checks open momentum positions whose underlying matches `coin`.
        Ensures that a rapid spot move (e.g. BTC flash crash through the
        strike) triggers the delta SL immediately — without waiting for
        the PM WS to echo a price change, which may not happen if the PM
        book is thin or empty near expiry.

        Uses the Pyth oracle price, which is the same data source that
        Polymarket's resolution bots read at market end_date, eliminating
        the HL perp funding-rate basis that caused missed stop-losses.
        """
        for pos in self._risk.get_open_positions():
            if pos.strategy != "momentum" or pos.underlying != coin:
                continue
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                continue
            await self._check_position(pos)

    # ── Auto-redemption ───────────────────────────────────────────────────────

    async def _auto_redeem_loop(self) -> None:
        """Periodically fetch PM wallet positions and redeem any that are ready.

        Runs every REDEEM_POLL_INTERVAL seconds (default 60 s).  Only active in
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
        """Fetch wallet positions and submit on-chain redemption for each redeemable token."""
        raw = await self._pm.get_live_positions()
        if not raw:
            return

        for pos_data in raw:
            token_id: str = pos_data.get("asset") or ""
            if not token_id or token_id in self._redeemed_tokens:
                continue
            if not pos_data.get("redeemable", False):
                continue

            size = float(pos_data.get("size", 0) or 0)
            cur_price = float(pos_data.get("currentPrice") or pos_data.get("curPrice") or pos_data.get("cur_price") or 0)
            # The `outcome` field labels WHICH TOKEN the user holds ("Yes"/"No"/"Up"/"Down"),
            # NOT whether that token won the market.  The correct signal is currentPrice:
            # winning tokens settle at 1.0, losing tokens at 0.0.
            won = cur_price > 0.5
            condition_id: str = pos_data.get("conditionId") or pos_data.get("condition_id") or ""
            payout = round(size * cur_price, 4)
            title = pos_data.get("title") or token_id[:20]

            # For losing positions (payout=0): just close bot tracking, skip on-chain call.
            # Mark in _redeemed_tokens immediately so we don't re-log every poll cycle.
            settlement_price = 1.0 if won else 0.0
            markets_snap = self._pm.get_markets()
            target_market_id: Optional[str] = None
            for mkt in markets_snap.values():
                if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                    target_market_id = mkt.condition_id
                    break

            # Fallback: API sometimes omits conditionId; use the market cache entry.
            condition_id = condition_id or target_market_id or ""

            log.info(
                "Auto-redeem: redeemable position found",
                token_id=token_id[:20],
                title=title[:60],
                won=won,
                size=round(size, 2),
                payout_usd=payout,
                condition_id=condition_id[:20] if condition_id else "(missing)",
            )

            if target_market_id:
                for rp in list(self._risk.get_positions().values()):
                    if rp.market_id == target_market_id and not rp.is_closed:
                        self._risk.close_position(
                            target_market_id, exit_price=settlement_price, side=rp.side,
                            resolved_outcome="WIN" if won else "LOSS",
                        )

            if not won:
                # Nothing to redeem on-chain for a losing position — suppress future re-logging.
                self._redeemed_tokens.add(token_id)
                log.info("Auto-redeem: lost position dismissed (payout=0)", token_id=token_id[:20])
                continue

            if not condition_id:
                # Do NOT mark as redeemed — allow retry next cycle when condition_id arrives.
                log.warning("Auto-redeem: no condition_id — will retry next cycle", token_id=token_id[:20])
                continue

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
                # Remove from redeemed set so it can be retried next cycle
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
            if pos.side in ("YES", "BUY_YES"):
                cur_price = book.mid
            else:
                book_no = self._pm._books.get(market.token_id_no)
                if book_no is None or book_no.mid is None:
                    log.debug(
                        "Monitor: no NO book for coin-loss aggregation — treating as 0 unrealised",
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
                        # Taker exit: YES sells at best_bid; NO sells at NO CLOB best_bid.
                        if pos.side in ("YES", "BUY_YES"):
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
            book = self._pm._books.get(market.token_id_yes)
            # Use last known mid for the held token; fall back to entry_price.
            if pos.side in ("YES", "BUY_YES"):
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
                    # NO book gone at resolution — cannot derive the outcome from
                    # the YES book (YES and NO are independent CLOBs).  Fall back
                    # to entry_price (zero P&L); the risk engine still closes the
                    # position and records the trade.
                    log.warning(
                        "Monitor: NO book gone at resolution — using entry_price (zero P&L)",
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
            # Momentum: YES book drained (common near expiry), but the HL-spot-based
            # delta SL must still fire — it requires only HL spot + pos.strike, NOT
            # the CLOB book.  Use entry_price as a stub for the P&L calculation;
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
            if pos.side in ("YES", "BUY_YES"):
                # YES CLOB mid; None when book is drained (delta SL still active via HL spot).
                current_token_price = book.mid if book is not None else None
            else:
                book_no = self._pm._books.get(market.token_id_no)
                if book_no is not None and book_no.mid is not None:
                    current_token_price = book_no.mid
                else:
                    # NO book unavailable — leave current_token_price as None.
                    # should_exit will skip momentum exit checks this tick rather
                    # than firing on a price derived from the independent YES side.
                    log.debug(
                        "Monitor: NO book unavailable — skipping exit check this tick",
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

        # Fetch live Pyth oracle spot for delta-based stop-loss.
        # get_mid() is a synchronous in-memory cache read — no await needed.
        # Pyth reflects the same price Polymarket resolves on; HL perp would
        # carry funding-rate basis that can mask a genuine oracle crossing.
        current_spot: Optional[float] = None
        if pos.strategy == "momentum" and self._pyth is not None and pos.underlying:
            current_spot = self._pyth.get_mid(pos.underlying)

        exit_flag, reason, unrealised = should_exit(
            pos=pos,
            current_price=current_price,
            initial_deviation=initial_deviation,
            market_end_date=market.end_date,
            now=now,
            current_token_price=current_token_price,
            tte_seconds=tte_seconds,
            current_spot=current_spot,
        )

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
            elif pos.side in ("YES", "BUY_YES"):
                # Pre-expiry taker exit: YES sell crosses the spread at best_bid.
                # Guard book being None (YES book drained when delta SL fired).
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
                market.token_id_yes if pos.side in ("YES", "BUY_YES")
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
        # Capture the underlying spot price at exit time (Pyth oracle).
        _exit_spot = (
            self._pyth.get_mid(pos.underlying)
            if self._pyth is not None and pos.underlying
            else 0.0
        ) or 0.0
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
