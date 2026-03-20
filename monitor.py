"""
monitor.py — Position exit monitor.

Runs as an asyncio task, checking all open positions every MONITOR_INTERVAL
seconds and closing them when any exit condition is met:

  1. Profit target  — unrealised P&L >= PROFIT_TARGET_PCT * initial_deviation * size
  2. Stop-loss      — unrealised P&L <= -STOP_LOSS_USD
  3. Time stop      — market end_date within EXIT_DAYS_BEFORE_RESOLUTION days
  4. Resolved stop  — market end_date has passed (prevent holding into resolution)

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

log = get_bot_logger(__name__)


# ── Exit reason constants ─────────────────────────────────────────────────────

class ExitReason:
    PROFIT_TARGET    = "profit_target"
    STOP_LOSS        = "stop_loss"
    TIME_STOP        = "time_stop"
    RESOLVED         = "resolved"
    COIN_LOSS_LIMIT  = "coin_loss_limit"  # maker: aggregate coin P&L exceeded threshold


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

    Args:
        pos:                Open position to evaluate.
        current_price:      Current mid price of the held token.
        initial_deviation:  |PM price - implied prob| at entry (always > 0).
        market_end_date:    When the market resolves (UTC). None = unknown.
        now:                Override for current time (for testing).

    Returns:
        (should_exit, reason, unrealised_pnl_usd)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Respect minimum hold time — avoids noise-triggered exits
    hold_seconds = (now - pos.opened_at).total_seconds()
    if hold_seconds < config.MIN_HOLD_SECONDS:
        return False, "", 0.0

    unrealised = compute_unrealised_pnl(pos, current_price)

    # ── Resolved stop (market already past end_date) ──────────────────────────
    if market_end_date is not None and now >= market_end_date:
        return True, ExitReason.RESOLVED, unrealised

    # ── Time stop ─────────────────────────────────────────────────────────────
    if market_end_date is not None:
        days_to_expiry = (market_end_date - now).total_seconds() / 86_400
        if pos.strategy == "maker":
            # Bucket markets (5m, 15m, …) have a full lifespan shorter than
            # MAKER_EXIT_HOURS so the hours-based gate would fire immediately
            # for every bucket fill.  Bucket positions should be held to free
            # settlement (RESOLVED), not force-exited via taker.  Only apply
            # MAKER_EXIT_HOURS to markets with no fixed duration (milestone/daily).
            is_bucket = pos.market_type in _MARKET_TYPE_DURATION_SECS
            if not is_bucket:
                maker_exit_days = config.MAKER_EXIT_HOURS / 24
                if maker_exit_days > 0 and days_to_expiry <= maker_exit_days:
                    return True, ExitReason.TIME_STOP, unrealised
        else:
            if days_to_expiry <= config.EXIT_DAYS_BEFORE_RESOLUTION:
                return True, ExitReason.TIME_STOP, unrealised

    # ── Profit target — mispricing strategy only (Flaw §6) ───────────────────
    # Maker P&L is aggregate rebate flow, not per-position reversion.
    # Exiting via profit target forces unnecessary taker fees on maker fills.
    if pos.strategy != "maker":
        profit_target_usd = initial_deviation * config.PROFIT_TARGET_PCT * pos.size
        if unrealised >= profit_target_usd:
            return True, ExitReason.PROFIT_TARGET, unrealised

    # ── Stop-loss — mispricing strategy only (Flaw §6) ───────────────────────
    # A hard stop on maker positions guarantees realised losses — the opposite of
    # what a maker should do. Inventory should be flattened passively, not panic-exited
    # via taker. Portfolio-level loss is tracked per coin via MAKER_COIN_MAX_LOSS_USD.
    if pos.strategy != "maker":
        if unrealised <= -config.STOP_LOSS_USD:
            return True, ExitReason.STOP_LOSS, unrealised

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
        log.info("PositionMonitor started", interval=self._interval)
        while self._running:
            await asyncio.sleep(self._interval)
            try:
                await self._check_all_positions()
            except Exception as exc:
                log.error("PositionMonitor iteration failed", exc=str(exc))

    async def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _check_all_positions(self) -> None:
        """Iterate all open positions and apply exit logic."""
        open_positions = [
            p for p in self._risk._positions.values()
            if not p.is_closed
        ]
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
            await self._exit_position(pos, market, taker_exit_price, reason, unrealised)

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

        # Place exit SELL order (opposite side to entry).
        # exit_price is in YES-price space; convert to token price for the order.
        sell_token = (
            market.token_id_yes if pos.side in ("YES", "BUY_YES")
            else market.token_id_no
        )
        sell_order_price = exit_price if pos.side in ("YES", "BUY_YES") else (1.0 - exit_price)
        if force_taker:
            # Market order — crosses the spread for immediate fill (manual close).
            order_id = await self._pm.place_market(
                token_id=sell_token,
                side="SELL",
                price=sell_order_price,
                size=pos.size,
                market=market,
            )
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
        # failed order means we should NOT record a close (risk stays open).
        if order_id is None and not config.PAPER_TRADING:
            log.error(
                "Monitor: exit order rejected — position stays open",
                market_id=pos.market_id,
            )
            return

        # For auto-resolved markets PM distributes directly — no trade takes place,
        # so exit_price is the true settlement (0 or 1), fees = 0, rebates = 0.
        if reason == ExitReason.RESOLVED:
            exit_price = float(round(exit_price))  # snap to exact 0.0 or 1.0

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
                    self._on_close_callback(pos.market_id)
                except Exception as exc:
                    log.warning("on_close_callback raised", exc=str(exc))