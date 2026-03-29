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
from ctf_utils import _redeem_ctf_via_safe

log = get_bot_logger(__name__)


# ── Exit reason constants ─────────────────────────────────────────────────────

class ExitReason:
    PROFIT_TARGET       = "profit_target"
    STOP_LOSS           = "stop_loss"
    TIME_STOP           = "time_stop"
    RESOLVED            = "resolved"
    COIN_LOSS_LIMIT     = "coin_loss_limit"      # maker: aggregate coin P&L exceeded threshold
    MOMENTUM_STOP_LOSS  = "momentum_stop_loss"   # momentum: held token fell below MOMENTUM_STOP_LOSS
    MOMENTUM_TAKE_PROFIT= "momentum_take_profit" # momentum: held token rose above MOMENTUM_TAKE_PROFIT


# ── Pure helpers (easily unit-tested) ────────────────────────────────────────

def compute_unrealised_pnl(pos: Position, current_price: float) -> float:
    """
    Unrealised P&L in USD for an open position.

    YES side: profit when price rises (bought low, priced higher now).
    NO  side: profit when price falls (bought NO at entry_price means
              we expect YES to fail, so we gain as YES price falls).
    """
    if pos.side in ("YES", "BUY_YES"):
        return (current_price - pos.entry_price) * pos.size
    else:
        return (pos.entry_price - current_price) * pos.size


def should_exit(
    pos: Position,
    current_price: float,
    initial_deviation: float,
    market_end_date: Optional[datetime],
    now: Optional[datetime] = None,
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
      * **momentum**: token-price exits (MOMENTUM_STOP_LOSS / MOMENTUM_TAKE_PROFIT)
        in YES-price space.  No time-stop — the min-TTE entry gate already
        places entries close to expiry, so holding to RESOLVED is correct.
      * **mispricing**: EXIT_DAYS_BEFORE_RESOLUTION time-stop, plus per-position
        PROFIT_TARGET and STOP_LOSS in USD P&L space.
      * **any other label**: treated as unknown — no triggers, hold to RESOLVED.

    Args:
        pos:                Open position to evaluate.
        current_price:      Current mid price of the YES token.
        initial_deviation:  |PM price - implied prob| at entry (always > 0).
        market_end_date:    When the market resolves (UTC). None = unknown.
        now:                Override for current time (for testing).

    Returns:
        (should_exit, reason, unrealised_pnl_usd)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Respect minimum hold time — avoids noise-triggered exits.
    # Momentum uses a much shorter hold floor (positions are near expiry;
    # MIN_HOLD_SECONDS=60 is calibrated for mispricing positions that last days).
    _min_hold = (
        config.MOMENTUM_MIN_HOLD_SECONDS
        if pos.strategy == "momentum"
        else config.MIN_HOLD_SECONDS
    )
    hold_seconds = (now - pos.opened_at).total_seconds()
    if hold_seconds < _min_hold:
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

    # ── Momentum exits — token-price based (not USD P&L) ─────────────────────
    if pos.strategy == "momentum":
        if pos.side in ("YES", "BUY_YES"):
            token_price = current_price
            stop_loss = config.MOMENTUM_STOP_LOSS_YES
        else:
            token_price = 1.0 - current_price
            stop_loss = config.MOMENTUM_STOP_LOSS_NO
        if token_price <= stop_loss:
            return True, ExitReason.MOMENTUM_STOP_LOSS, unrealised
        if token_price >= config.MOMENTUM_TAKE_PROFIT:
            return True, ExitReason.MOMENTUM_TAKE_PROFIT, unrealised
        # No time-stop — min-TTE entry gate means position is near expiry already.
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
        on_close_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._pm = pm
        self._risk = risk
        self._interval = interval
        # Called with market_id whenever a position is successfully closed.
        # Used by MispricingScanner to reset the per-market cooldown clock.
        self._on_close_callback = on_close_callback
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
        # Non-momentum strategies (maker / mispricing) have hold windows of hours
        # or days so the slower poll loop is adequate for them.
        self._pm.on_price_change(self._on_price_update)
        log.info("PositionMonitor started", interval=self._interval)
        asyncio.create_task(self._auto_redeem_loop())
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                await self._check_all_positions()
            except Exception as exc:
                log.error("PositionMonitor iteration failed", exc=str(exc))

    async def stop(self) -> None:
        self._running = False

    # ── Event-driven price callback ───────────────────────────────────────────

    async def _on_price_update(self, token_id: str, mid: float) -> None:
        """Triggered on every WS book/price_change tick.

        Checks open momentum positions whose market's YES *or* NO token just
        updated.  Fires ``_check_position`` immediately so stop-loss and take-
        profit exits are detected within one WS round-trip (~100-500 ms) rather
        than waiting for the 30-second poll cycle.

        Only momentum positions are evaluated here because:
          * maker / mispricing positions have hold windows of hours/days, where
            30 s polling lag is negligible.
          * checking every strategy on every tick would add unnecessary latency
            to the WS message-processing path.
        """
        for pos in self._risk.get_open_positions():
            if pos.strategy != "momentum":
                continue
            market = self._pm._markets.get(pos.market_id)
            if market is None:
                continue
            # React to either the YES or NO token updating for this market.
            if token_id not in (market.token_id_yes, market.token_id_no):
                continue
            key = f"{pos.market_id}:{pos.side}"
            if key in self._exiting_positions:
                continue  # exit already in progress for this leg
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
            cur_price = float(pos_data.get("curPrice") or pos_data.get("currentPrice") or pos_data.get("cur_price") or 0)
            won = cur_price > 0.99
            condition_id: str = pos_data.get("conditionId") or pos_data.get("condition_id") or ""
            payout = round(size * cur_price, 4)
            title = pos_data.get("title") or token_id[:20]

            log.info(
                "Auto-redeem: redeemable position found",
                token_id=token_id[:20],
                title=title[:60],
                won=won,
                size=round(size, 2),
                payout_usd=payout,
            )

            # Mark immediately so a crash mid-call doesn't cause a double-submit
            self._redeemed_tokens.add(token_id)

            # For losing positions (payout=0): just close bot tracking, skip on-chain call
            settlement_price = 1.0 if won else 0.0
            markets_snap = self._pm.get_markets()
            target_market_id: Optional[str] = None
            for mkt in markets_snap.values():
                if mkt.token_id_yes == token_id or mkt.token_id_no == token_id:
                    target_market_id = mkt.condition_id
                    break
            if target_market_id:
                for rp in list(self._risk.get_positions().values()):
                    if rp.market_id == target_market_id and not rp.is_closed:
                        self._risk.close_position(
                            target_market_id, exit_price=settlement_price, side=rp.side
                        )

            if not won:
                log.info("Auto-redeem: lost position dismissed (payout=0)", token_id=token_id[:20])
                continue

            if not condition_id:
                log.warning("Auto-redeem: no condition_id — skipping on-chain call", token_id=token_id[:20])
                continue

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
            unrealised = compute_unrealised_pnl(pos, book.mid)
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
                        # Taker exit: YES position sells at best_bid; NO position buys YES at best_ask.
                        # Using mid would overstate P&L by half the spread on every close.
                        if pos.side in ("YES", "BUY_YES"):
                            exit_mid = book.best_bid if book.best_bid is not None else book.mid
                        else:  # NO — close by selling NO = buying YES at best_ask
                            exit_mid = book.best_ask if book.best_ask is not None else book.mid
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

    async def _check_position(self, pos: Position) -> None:
        """Evaluate exit conditions for a single position."""
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
            # Use last known mid if the book still has data; otherwise fall back
            # to entry_price so round() snaps to the nearest settlement value.
            exit_mid = (
                book.mid
                if book is not None and book.mid is not None
                else pos.entry_price
            )
            unrealised = compute_unrealised_pnl(pos, exit_mid)
            log.info(
                "Monitor: resolved market — closing position",
                market_id=pos.market_id,
                exit_mid=round(exit_mid, 4),
                book_available=book is not None and book.mid is not None,
            )
            await self._exit_position(pos, market, exit_mid, ExitReason.RESOLVED, unrealised)
            return

        # ── Standard check (live book required for all other exits) ───────────
        # Always read the YES token price — all P&L formulas are in YES-price space.
        # entry_price is stored as the YES price (pm_price at signal time) for both
        # YES and NO positions, so current_price must also be the YES token price.
        book = self._pm._books.get(market.token_id_yes)
        if book is None or book.mid is None:
            log.debug("Monitor: no book data yet", market_id=pos.market_id, token_id=market.token_id_yes)
            return

        current_price = book.mid  # mid used for trigger-condition evaluation only
        initial_deviation = self._initial_deviations.get(
            pos.market_id, config.MISPRICING_THRESHOLD
        )

        exit_flag, reason, unrealised = should_exit(
            pos=pos,
            current_price=current_price,
            initial_deviation=initial_deviation,
            market_end_date=market.end_date,
            now=now,
        )

        log.debug(
            "Monitor: position check",
            market_id=pos.market_id,
            entry=round(pos.entry_price, 4),
            mid=round(current_price, 4),
            bid=round(book.best_bid, 4) if book.best_bid is not None else None,
            ask=round(book.best_ask, 4) if book.best_ask is not None else None,
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
                taker_exit_price = book.best_bid if book.best_bid is not None else current_price
            else:
                # NO close: sell NO at NO_bid = 1 − YES_ask (P&L formula in YES space).
                taker_exit_price = book.best_ask if book.best_ask is not None else current_price

            # Stop-loss exits must fill immediately — use a market (taker) order.
            # post_only limit orders on stop exits cause two problems:
            #   1. A SELL at best_bid always crosses the book → rejected → retry
            #      backs off one tick and posts a resting maker order that may
            #      never fill before resolution.
            #   2. The position is recorded as closed even if the CLOB order
            #      never fills, leaving an unhedged live position.
            is_stop = reason in (ExitReason.MOMENTUM_STOP_LOSS, ExitReason.STOP_LOSS,
                                 ExitReason.COIN_LOSS_LIMIT)
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
            sell_order_price = exit_price if pos.side in ("YES", "BUY_YES") else (1.0 - exit_price)
            if force_taker:
                # Market order — crosses the spread for immediate fill (stop-loss/manual).
                # Retry up to 3 times (200 ms apart) before giving up.
                order_id = None
                for _attempt in range(3):
                    order_id = await self._pm.place_market(
                        token_id=sell_token,
                        side="SELL",
                        price=sell_order_price,
                        size=pos.size,
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
                    size=pos.size,
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
                    size=pos.size,
                    market=market,
                )
                if order_id is None:
                    log.error(
                        "EXIT_ORDER_FAILED — manual intervention required",
                        market_id=pos.market_id, side=pos.side, reason=reason,
                    )
                    self._exiting_positions.discard(f"{pos.market_id}:{pos.side}")
                    return

        # Token price for the exit side (NO positions close in YES-price space).
        token_exit_price = exit_price if pos.side in ("YES", "BUY_YES") else (1.0 - exit_price)

        # Fee model depends on exit type:
        #   RESOLVED    — auto-distribution, no trade → zero fees/rebates.
        #   post-only   — we are the maker on exit: earn rebate, pay no taker fee.
        #   force_taker — market order: we are the taker, pay full fee, earn no rebate.
        fee_base = (
            pos.size * token_exit_price * config.PM_FEE_COEFF * (1.0 - token_exit_price)
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

        closed = self._risk.close_position(
            market_id=pos.market_id,
            side=pos.side,
            exit_price=exit_price,
            fees_paid=exit_fees,
            rebates_earned=total_rebates,
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
