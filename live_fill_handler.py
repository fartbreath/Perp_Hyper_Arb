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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE OF TRUTH: THE PM DATA API IS ALWAYS RIGHT.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If the Polymarket Data API says a position exists, it exists — regardless of
what the bot's internal state says, and regardless of whether fields like
avgPrice are zero or missing.  DO NOT filter out positions based on missing
cost-basis data.  The only valid reason to skip a position record is:

  1. size <= 0  (PM says we hold nothing)
  2. token_id is absent/empty (malformed record)

Everything else — avgPrice, curPrice, redeemable, outcome — is supplementary
data for display or automation.  Never use it as a gate on whether to restore
a position.  Keep this logic simple.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import asyncio
import csv
import time
from datetime import datetime, timezone
from typing import Optional

import config
from fill_simulator import FILLS_CSV, FILLS_HEADER
from logger import get_bot_logger
from risk import Position, RiskEngine
from strategies.maker.fill_logic import open_position_from_fill
from strategies.maker.signals import ActiveQuote

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
        # HL mid snapshot per underlying coin at last fill — used to detect
        # adverse price moves between consecutive fills on the same coin.
        self._prev_hl_mids: dict[str, float] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register fill callback with PMClient.  No-op in paper mode."""
        if config.PAPER_TRADING:
            return
        self._pm.on_order_fill(self._on_order_fill)
        # A1: Register reconnect callback so fills missed during a WS gap are
        # reconciled by re-fetching the PM Data API position snapshot.
        self._pm.on_user_ws_reconnect(self._reconcile_after_reconnect)
        log.info("LiveFillHandler started — listening for order fill events")

    async def startup_restore(self) -> None:
        """Restore live state from a previous session.

        Flow:
          1. Fetch current resting (LIVE) orders from the CLOB API.
          2. For orders belonging to known markets: re-register them as
             ActiveQuote entries so the bot continues to manage them.
          3. Cancel any orders for unknown/expired markets (can't manage them).
          4. Restore open token positions into the risk engine.

        This avoids the blast-cancel-and-repost cycle that causes unnecessary
        API churn and potentially misses fills in flight at restart.
        """
        if config.PAPER_TRADING:
            return

        log.info("Live startup: restoring open orders from previous session…")
        unknown_order_ids = await self._restore_open_orders()

        if unknown_order_ids:
            log.info(
                "Live startup: cancelling orders for unknown/expired markets",
                count=len(unknown_order_ids),
            )
            for oid in unknown_order_ids:
                try:
                    await self._pm.cancel_order(oid)
                except Exception as exc:
                    log.warning("startup_restore: cancel failed", order_id=oid[:20], exc=str(exc))

        log.info("Live startup: loading existing positions from PM Data API…")
        await self._restore_positions()
    # ── Open-order restore ───────────────────────────────────────────────

    async def _restore_open_orders(self) -> list[str]:
        """Re-register resting CLOB orders as ActiveQuote entries.

        Returns a list of order_ids that could NOT be matched to a known market
        and should be cancelled by the caller.
        """
        live_orders = await self._pm.get_live_orders()
        if not live_orders:
            log.info("No resting orders found on CLOB — starting with clean quote state")
            return []

        # Build token_id → PMMarket lookup
        markets_by_token: dict[str, object] = {}
        for mkt in self._pm.get_markets().values():
            markets_by_token[mkt.token_id_yes] = mkt
            markets_by_token[mkt.token_id_no] = mkt

        unknown: list[str] = []
        restored = 0

        for order in live_orders:
            order_id: str = order.get("id") or order.get("order_id") or ""
            token_id: str = order.get("asset_id") or ""
            if not order_id or not token_id:
                continue

            try:
                price = float(order.get("price", 0))
                orig_size = float(order.get("original_size", 0) or order.get("size", 0))
                size_matched = float(order.get("size_matched", 0))
            except (TypeError, ValueError):
                continue

            remaining = round(orig_size - size_matched, 6)
            if price <= 0 or remaining <= 0:
                continue

            market = markets_by_token.get(token_id)
            if market is None:
                log.warning(
                    "startup_restore: order for unknown market — will cancel",
                    order_id=order_id[:20],
                    token_id=token_id[:24],
                )
                unknown.append(order_id)
                continue

            # Determine which key to use in _active_quotes:
            #   YES token → bid_key  = token_id_yes
            #   NO  token → ask_key  = token_id_yes + "_ask"
            is_yes = token_id == market.token_id_yes
            key = market.token_id_yes if is_yes else f"{market.token_id_yes}_ask"

            collateral = price * remaining

            aq = ActiveQuote(
                market_id=market.condition_id,
                token_id=token_id,
                side="BUY",
                price=price,
                size=remaining,
                order_id=order_id,
                posted_at=time.time(),   # age from now; watchdog will reprice if stale
                collateral_usd=round(collateral, 4),
                original_size=orig_size,
                score=0.0,
            )
            self._maker.restore_active_quote(key, aq)
            # Keep WS subscription alive for this token
            self._pm._pinned_tokens.add(token_id)
            # Seed the cumulative-fill tracker so incremental diffs are correct
            if size_matched > 0:
                self._matched_so_far[order_id] = size_matched

            restored += 1
            log.info(
                "Order restored to active quotes",
                market=market.title[:60],
                side="YES" if is_yes else "NO",
                price=round(price, 4),
                remaining=round(remaining, 2),
                order_id=order_id[:20],
            )

        log.info(
            "Open-order restore complete",
            restored=restored,
            unknown=len(unknown),
            scanned=len(live_orders),
        )
        return unknown
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
            try:
                size = float(pos_data.get("size", 0))
                avg_price = float(
                    pos_data.get("avgPrice") or pos_data.get("avg_price") or 0
                )
            except (TypeError, ValueError):
                continue

            # SOURCE OF TRUTH: PM says we hold it → we hold it.
            # Only skip truly empty/malformed records. Never filter on avgPrice.
            if size <= 0 or not token_id:
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

            # Determine side: prefer explicit outcome field; fall back to token_id match.
            # PM outcome field is "Yes"/"No" for binary markets, but "Up"/"Down" (or other
            # named outcomes) for UP/DOWN bucket markets.  Only "yes" maps to is_yes=True;
            # all other named outcomes fall through to the token_id comparison which is
            # always an exact, reliable match.
            outcome_raw = (pos_data.get("outcome") or "").strip().lower()
            if outcome_raw == "yes":
                is_yes = True
            elif outcome_raw == "no":
                is_yes = False
            else:
                # Named outcome ("Up", "Down", "Will X", etc.) or absent field —
                # match by token_id against the known YES token for this market.
                is_yes = token_id == market.token_id_yes
            side = "YES" if is_yes else "NO"

            # entry_price is in YES-probability space. avg_price=0 means the PM
            # Data API didn't populate cost-basis (common for older/external fills).
            # We still restore the position — size is what matters for risk tracking.
            if avg_price > 0:
                entry_price = avg_price if is_yes else (1.0 - avg_price)
            else:
                entry_price = 0.0
            entry_cost = entry_price * size

            # Look up strategy from the persisted token→strategy map written by
            # the risk engine when the position was originally opened.  Fall back
            # to "unknown" so the webapp shows a clear signal rather than wrong data.
            saved_strategy = self._risk.get_token_strategy(token_id) or "unknown"

            pos = Position(
                market_id=market.condition_id,
                market_title=market.title,
                market_type=market.market_type,
                underlying=market.underlying,
                side=side,
                size=size,
                entry_price=entry_price,
                strategy=saved_strategy,
                opened_at=datetime.now(timezone.utc),
                entry_cost_usd=round(entry_cost, 4),
                token_id=token_id,
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

    # ── Reconnect reconciliation (A1) ─────────────────────────────────────────

    async def _reconcile_after_reconnect(self) -> None:
        """Reconcile risk engine state against PM wallet after a user WS reconnect.

        Fills received while the WS was disconnected were never delivered by the
        user channel.  Fetching the PM Data API gives us the authoritative wallet
        state.  Any position present in the wallet but absent from the risk engine
        was filled during the gap and must be imported now to keep the bot's
        inventory model accurate.

        Ghost positions (bot tracks but PM wallet doesn't) are logged for human
        review; they can be dismissed via POST /positions/ghost/dismiss.
        """
        log.info("Reconciliation: fetching PM wallet positions after reconnect…")
        raw_positions = await self._pm.get_live_positions()
        if not raw_positions:
            log.info("Reconciliation: no positions in PM wallet")
            return

        markets_by_token: dict[str, object] = {}
        for mkt in self._pm.get_markets().values():
            markets_by_token[mkt.token_id_yes] = mkt
            markets_by_token[mkt.token_id_no] = mkt

        # Build current risk-engine state keyed by token_id
        bot_by_token: dict[str, object] = {}
        for pos in self._risk.get_positions().values():
            if pos.is_closed:
                continue
            mkt = self._pm.get_markets().get(pos.market_id)
            if mkt:
                tid = mkt.token_id_yes if pos.side == "YES" else mkt.token_id_no
                bot_by_token[tid] = pos

        imported = 0
        for pos_data in raw_positions:
            token_id: str = pos_data.get("asset") or pos_data.get("asset_id") or ""
            try:
                size = float(pos_data.get("size", 0))
                avg_price = float(
                    pos_data.get("avgPrice") or pos_data.get("avg_price") or 0
                )
            except (TypeError, ValueError):
                continue

            # SOURCE OF TRUTH: PM says we hold it → we hold it.
            # Only skip truly empty/malformed records. Never filter on avgPrice.
            if size <= 0 or not token_id:
                continue
            if token_id in bot_by_token:
                continue  # already tracked

            market = markets_by_token.get(token_id)
            if market is None:
                self._pm._pinned_tokens.add(token_id)
                log.warning(
                    "Reconciliation: found wallet position for unknown market",
                    token_id=token_id[:24],
                )
                continue

            outcome_raw = (pos_data.get("outcome") or "").strip().lower()
            if outcome_raw == "yes":
                is_yes = True
            elif outcome_raw == "no":
                is_yes = False
            else:
                is_yes = token_id == market.token_id_yes
            side = "YES" if is_yes else "NO"
            if avg_price > 0:
                entry_price = avg_price if is_yes else (1.0 - avg_price)
            else:
                entry_price = 0.0
            entry_cost = entry_price * size

            from risk import Position
            saved_strategy = self._risk.get_token_strategy(token_id) or "unknown"
            pos = Position(
                market_id=market.condition_id,
                market_title=market.title,
                market_type=market.market_type,
                underlying=market.underlying,
                side=side,
                size=size,
                entry_price=entry_price,
                strategy=saved_strategy,
                opened_at=datetime.now(timezone.utc),
                entry_cost_usd=round(entry_cost, 4),
                token_id=token_id,
            )
            self._risk.open_position(pos)
            self._pm._pinned_tokens.add(token_id)
            imported += 1
            log.info(
                "Reconciliation: imported wallet position missing from bot",
                market=market.title[:60],
                side=side,
                size=round(size, 2),
                entry_price=round(entry_price, 4),
            )

        # Auto-dismiss ghost positions — bot tracks them but PM wallet doesn't.
        # PM wallet is the source of truth; close them immediately so the webapp
        # reflects the correct state rather than showing a stale ghost position.
        # This catches fills missed while the WS was disconnected (e.g. naked-leg
        # close taker orders that resolved before the WS reconnected).
        pm_token_ids = {
            pos_data.get("asset") or pos_data.get("asset_id") or ""
            for pos_data in raw_positions
        }
        ghost_count = 0
        for tid, pos in bot_by_token.items():
            if tid not in pm_token_ids:
                ghost_count += 1
                closed = self._risk.close_position(
                    pos.market_id, exit_price=0.0, side=pos.side
                )
                if closed is not None:
                    log.warning(
                        "Reconciliation: auto-dismissed ghost position (absent from PM wallet)",
                        token_id=tid[:24],
                        market_id=pos.market_id[:24],
                        side=pos.side,
                    )
                else:
                    log.warning(
                        "Reconciliation: ghost position already closed",
                        token_id=tid[:24],
                    )

        log.info(
            "Reconciliation complete",
            imported=imported,
            ghosts_detected=ghost_count,
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

        # Compute HL adverse-selection signal: compare current HL mid against the
        # last recorded mid for this coin.  YES position is adverse when HL falls
        # (probability of up-move drops); NO position is adverse when HL rises.
        _hl_raw = self._maker.get_hl_mid(market.underlying)
        hl_mid_now: Optional[float] = _hl_raw if isinstance(_hl_raw, (int, float)) else None
        hl_mid_prev: Optional[float] = self._prev_hl_mids.get(market.underlying)
        hl_move_pct: Optional[float] = None
        if hl_mid_now is not None and hl_mid_prev is not None and hl_mid_prev > 0:
            hl_move_pct = round((hl_mid_now - hl_mid_prev) / hl_mid_prev, 6)
        adverse = (
            (position_side == "YES" and hl_move_pct is not None
             and hl_move_pct < -config.PAPER_ADVERSE_SELECTION_PCT)
            or (position_side == "NO" and hl_move_pct is not None
             and hl_move_pct > config.PAPER_ADVERSE_SELECTION_PCT)
        )
        if hl_mid_now is not None:
            self._prev_hl_mids[market.underlying] = hl_mid_now

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
            hl_mid=hl_mid_now,
            hl_move_pct=hl_move_pct,
            adverse=adverse,
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
                    "hl_mid": hl_mid_now if hl_mid_now is not None else "",
                    "hl_move_pct": hl_move_pct if hl_move_pct is not None else "",
                    "adverse": adverse,
                    "total_fills_session": self._fills_total,
                    "signal_score": round(consumed.score, 2),
                    "rebate_usd": result.rebate_usd,
                })
        except Exception as exc:
            log.warning("Failed to write live fill to fills.csv", exc=str(exc))
        if key not in self._maker._active_quotes:
            asyncio.create_task(self._maker._reprice_market(market))
