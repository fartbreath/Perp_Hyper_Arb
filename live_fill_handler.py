"""
live_fill_handler.py — Live fill delivery and startup state restoration.

In paper-trading mode this module is inert; FillSimulator handles everything.
In live mode it:
  * On startup: cancels all open orders (clean slate), then loads open token
    positions from the Polymarket Data API and restores them to the risk engine.
  * Registers an order-fill callback on PMClient so that every MATCHED event
    from the PM user WebSocket is immediately processed (open position, credit
    rebate, write to fills.csv, schedule reprice).

Note: HL hedge calls are intentionally omitted here.  Initial live tests will
use small contract sizes that do not cross HL_HEDGE_THRESHOLD.  Hedge support
can be added to _process_fill_slice() once the live fill path is validated.
"""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from typing import Optional

import config
from fill_simulator import FILLS_CSV, FILLS_HEADER
from logger import get_bot_logger
from risk import Position, RiskEngine
from strategies.maker.fill_logic import open_position_from_fill

log = get_bot_logger(__name__)


class LiveFillHandler:
    """Processes real PM fill events; restores live state on startup.

    Usage::

        handler = LiveFillHandler(pm, maker, risk_engine, monitor)
        # before maker starts quoting:
        if not config.PAPER_TRADING:
            await handler.startup_restore()
        await handler.start()
    """

    def __init__(self, pm, maker, risk: RiskEngine, monitor) -> None:
        self._pm = pm
        self._maker = maker
        self._risk = risk
        self._monitor = monitor

        # Track cumulative size_matched per order_id.
        # PM user WS sends the *cumulative* filled amount, not the incremental
        # amount of the most-recent trade.  We diff against our running total to
        # get the incremental slice that this particular event added.
        self._matched_so_far: dict[str, float] = {}
        self._fills_total: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register fill callback with PMClient.  No-op in paper mode."""
        if config.PAPER_TRADING:
            return
        self._pm.on_order_fill(self._on_order_fill)
        log.info("LiveFillHandler started — listening for order fill events")

    async def startup_restore(self) -> None:
        """Cancel all open orders and restore live positions.

        Called once before the maker strategy begins quoting so the risk engine
        reflects any positions already held from a previous session.
        """
        if config.PAPER_TRADING:
            return

        log.info("Live startup: cancelling all open orders…")
        await self._pm.cancel_all()

        log.info("Live startup: loading existing positions from PM Data API…")
        await self._restore_positions()

    # ── Position restore ───────────────────────────────────────────────────────

    async def _restore_positions(self) -> None:
        """Load open positions from PM Data API → inject into risk engine."""
        raw_positions = await self._pm.get_live_positions()
        if not raw_positions:
            log.info("No open positions found on PM — starting with clean risk state")
            return

        # Build token_id → PMMarket lookup from all discovered markets
        markets_by_token: dict[str, object] = {}
        for mkt in self._pm.get_markets().values():
            markets_by_token[mkt.token_id_yes] = mkt
            markets_by_token[mkt.token_id_no] = mkt

        restored = 0
        for pos_data in raw_positions:
            token_id: str = pos_data.get("asset") or pos_data.get("asset_id") or ""
            outcome: str = (pos_data.get("outcome") or "yes").lower()
            try:
                size = float(pos_data.get("size", 0))
                avg_price = float(
                    pos_data.get("avgPrice") or pos_data.get("avg_price") or 0
                )
            except (TypeError, ValueError):
                continue

            if size <= 0 or avg_price <= 0 or not token_id:
                continue

            market = markets_by_token.get(token_id)
            if market is None:
                log.warning(
                    "startup_restore: no market found for token — pinning anyway",
                    token_id=token_id[:24],
                )
                # Keep the token WS-subscribed so we receive price updates.
                self._pm._pinned_tokens.add(token_id)
                continue

            # Position convention mirrors fill_simulator / risk.py:
            #   YES token bought  → side="YES", entry_price = avg_price (PM limit px)
            #   NO  token bought  → side="NO",  entry_price = avg_price  (PM limit px)
            # entry_cost_usd = actual USDC paid:
            #   YES: avg_price × size
            #   NO:  (1 − avg_price) × size   (we paid 1−p per NO contract)
            is_yes = (outcome == "yes" or token_id == market.token_id_yes)
            side = "YES" if is_yes else "NO"
            entry_price = avg_price
            entry_cost = avg_price * size if is_yes else (1.0 - avg_price) * size

            pos = Position(
                market_id=market.condition_id,
                market_title=market.title,
                market_type=market.market_type,
                underlying=market.underlying,
                side=side,
                size=size,
                entry_price=entry_price,
                strategy="maker",
                opened_at=datetime.now(timezone.utc),
                entry_cost_usd=round(entry_cost, 4),
            )
            self._risk.open_position(pos)
            # Pin so WS subscription survives the next market-refresh sweep.
            self._pm._pinned_tokens.add(token_id)
            restored += 1
            log.info(
                "Position restored from PM",
                market=market.title[:60],
                side=side,
                size=round(size, 2),
                entry_price=round(entry_price, 4),
            )

        log.info(
            "Position restore complete",
            restored=restored,
            scanned=len(raw_positions),
        )

    # ── Fill event handler ─────────────────────────────────────────────────────

    async def _on_order_fill(self, order_data: dict) -> None:
        """Process a MATCHED event from the PM user WebSocket.

        Computes the *incremental* fill for this event by diffing against our
        running cumulative total, finds the matching ActiveQuote by order_id,
        and hands off to _process_fill_slice().
        """
        order_id: str = order_data.get("id") or order_data.get("order_id") or ""
        token_id: str = order_data.get("asset_id") or ""
        side_raw: str = (order_data.get("side") or "").upper()  # "BUY" | "SELL"

        try:
            fill_price = float(order_data.get("price", 0))
            cumulative_matched = float(order_data.get("size_matched", 0))
        except (TypeError, ValueError):
            log.warning("LiveFillHandler: malformed fill event", data=str(order_data)[:200])
            return

        if not order_id or fill_price <= 0 or cumulative_matched <= 0:
            return

        # Compute incremental fill (PM sends cumulative totals per order)
        prev = self._matched_so_far.get(order_id, 0.0)
        incremental = round(cumulative_matched - prev, 6)
        if incremental <= 0:
            return

        self._matched_so_far[order_id] = cumulative_matched

        # Locate the ActiveQuote that owns this order_id
        key: Optional[str] = None
        for k, q in list(self._maker.get_active_quotes().items()):
            if q.order_id == order_id:
                key = k
                break

        if key is None:
            # Fill arrived after the quote was already fully consumed — normal race.
            log.debug(
                "LiveFillHandler: fill for unknown/consumed order",
                order_id=order_id[:20],
                token_id=token_id[:24],
            )
            return

        # Resolve the market (strip _ask suffix to get the base token key)
        base_token = key.replace("_ask", "")
        market = self._maker.find_market_for_token(base_token)
        if market is None:
            log.warning(
                "LiveFillHandler: market not found for filled order",
                order_id=order_id[:20],
                key=key[:40],
            )
            return

        await self._process_fill_slice(
            key, order_id, fill_price, side_raw, incremental, market
        )

        # Clean up matched-size tracker for fully-consumed quotes
        active_quotes_now = self._maker.get_active_quotes()
        if key not in active_quotes_now:
            self._matched_so_far.pop(order_id, None)

        # Prune stale entries for cancelled/replaced orders not in any active quote
        known_order_ids = {q.order_id for q in active_quotes_now.values() if q.order_id}
        for oid in [o for o in list(self._matched_so_far) if o not in known_order_ids]:
            self._matched_so_far.pop(oid, None)

    async def _process_fill_slice(
        self,
        key: str,
        order_id: str,
        fill_price: float,
        side: str,        # "BUY" | "SELL"
        filled_size: float,
        market,
    ) -> None:
        """Open a live position for one fill slice."""
        result = open_position_from_fill(
            maker=self._maker,
            risk=self._risk,
            monitor=self._monitor,
            key=key,
            fill_price=fill_price,
            filled_size=filled_size,
            market=market,
        )
        if result is None:
            return

        consumed = result.consumed
        actual_filled = result.actual_filled
        position_side = result.position_side
        fill_cost_usd = result.fill_cost_usd

        # HL hedge intentionally omitted for initial live tests.

        self._fills_total += 1
        log.info(
            "[LIVE FILL] Maker order filled",
            market=market.title[:60],
            market_id=consumed.market_id,
            order_side=consumed.side,
            position_side=position_side,
            fill_price=round(fill_price, 4),
            contracts_filled=round(actual_filled, 4),
            fill_cost_usd=round(fill_cost_usd, 4),
            total_fills_session=self._fills_total,
            signal_score=round(consumed.score, 2),
        )

        try:
            with FILLS_CSV.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=FILLS_HEADER).writerow({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market_id": consumed.market_id,
                    "market_title": market.title[:80],
                    "underlying": market.underlying,
                    "order_side": consumed.side,
                    "position_side": position_side,
                    "fill_price": round(fill_price, 4),
                    "contracts_filled": round(actual_filled, 4),
                    "fill_cost_usd": round(fill_cost_usd, 4),
                    "book_bid": "",
                    "book_ask": "",
                    "depth_at_level": "",
                    "arrival_prob": "",
                    "mean_taker": "",
                    "taker_size_drawn": "",
                    "hl_mid": "",
                    "hl_move_pct": "",
                    "adverse": False,
                    "total_fills_session": self._fills_total,
                    "signal_score": round(consumed.score, 2),
                    "rebate_usd": result.rebate_usd,
                })
        except Exception as exc:
            log.warning("Failed to write live fill to fills.csv", exc=str(exc))
        if key not in self._maker._active_quotes:
            asyncio.create_task(self._maker._reprice_market(market))
