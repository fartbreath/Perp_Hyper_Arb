"""
strategies.OpeningNeutral.scanner — Opening Neutral scanner (Strategy 5).

Scans bucket markets near opening, simultaneously buys YES and NO when
combined cost ≤ OPENING_NEUTRAL_COMBINED_COST_MAX.  Guaranteed profitable
at resolution if both legs fill; one-leg fallback promotes to momentum.

See strategies/OpeningNeutral/PLAN.md for full specification.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import config
from logger import get_bot_logger
from market_data.pm_client import _MARKET_TYPE_DURATION_SECS
from risk import Position
from strategies.base import BaseStrategy
from strategies.Momentum.event_log import emit as _emit_event
from strategies.Momentum.market_utils import _is_updown_market

log = get_bot_logger(__name__)

# Minimum seconds between entry evaluations for the same market on the hot WS path.
# Both YES and NO tokens map to the same condition_id; without this debounce, every
# WS batch fires _evaluate_entry twice (once per token), and "too_expensive" markets
# generate a signal on every tick because they never add to _entering_markets.
_ENTRY_EVAL_DEBOUNCE_SECS: float = 1.0


class OpeningNeutralScanner(BaseStrategy):
    """
    Scans bucket markets for simultaneous YES+NO opening entries.

    Parameters
    ----------
    pm        : polymarket client (pm_client.PolymarketClient)
    risk      : RiskEngine instance
    spot_client : spot oracle (SpotOracle)
    vol_fetcher : VolFetcher instance (unused; retained for constructor compatibility)
    momentum_scanner : MomentumScanner reference (for conflict guard)
    on_close_callback : called with market_id when a loser is exited
    on_open_callback  : zero-arg callable fired after a pair is registered;
                        used to wake state_sync_loop immediately so positions
                        appear in the webapp without waiting up to 1 s.
    """

    def __init__(
        self,
        pm,
        risk,
        spot_client,
        vol_fetcher,
        momentum_scanner=None,
        on_close_callback=None,
        on_open_callback=None,
    ) -> None:
        self._pm = pm
        self._risk = risk
        self._spot = spot_client
        self._vol = vol_fetcher
        self._momentum = momentum_scanner
        self._on_close_callback = on_close_callback
        self._on_open_callback = on_open_callback

        self._running: bool = False
        # Markets currently being entered (guards against double-entry).
        self._entering_markets: set[str] = set()
        # Active neutral pairs: pair_id -> {
        #   "market_id": str, "market_title": str,
        #   "yes_pos": Position, "no_pos": Position,
        #   "yes_exit_order_id": str,  # resting SELL order on YES leg
        #   "no_exit_order_id": str,   # resting SELL order on NO leg
        # }
        # Populated after both legs fill.
        self._active_pairs: dict[str, dict[str, Any]] = {}
        # Signal history for get_signals() / webapp display.
        self._signals: list[dict] = []

        # ── Event-driven subscription state ─────────────────────────────────
        # Markets qualified for entry but not yet entered.
        # Populated by _refresh_pending_markets; cleared when market is entered or expires.
        self._pending_markets: dict[str, Any] = {}          # condition_id → PMMarket
        # O(1) reverse lookup: token_id → condition_id for pending markets.
        # Both YES and NO tokens map to the same condition_id.
        self._token_to_pending: dict[str, str] = {}         # token_id → condition_id
        # O(1) reverse lookup: token_id → pair_id for active pairs.
        self._token_to_pair: dict[str, str] = {}            # token_id → pair_id
        # Per-market entry debounce: rate-limits _evaluate_entry to at most
        # _ENTRY_EVAL_DEBOUNCE_SECS for "too_expensive" markets that would
        # otherwise spam a signal on every WS tick.
        # NOTE: only the YES token is registered in _token_to_pending (structural
        # fix for the double-fire from YES+NO both triggering evaluation in the
        # same WS batch).  The NO-token WS event is ignored on the entry path;
        # when evaluation fires, both books are read from cache so the latest
        # NO ask is always used.
        self._market_last_eval: dict[str, float] = {}       # condition_id → last eval ts
        # Persistent set of market IDs for which entry has been attempted in this
        # session.  Added the moment _evaluate_entry decides to enter (before the
        # order is placed).  Prevents re-evaluation even if _register_pair fails
        # silently (task exception swallowed) or _token_to_pending cleanup races.
        # Pruned in _refresh_pending_markets when the market expires.
        self._entered_market_ids: set[str] = set()
        # Ring buffer of recently closed/resolved pairs (kept for webapp display).
        self._closed_pairs: list[dict] = []
        # Per-pair debounce for loser-exit: "pair_id_SIDE" keys prevent the
        # same leg from firing _execute_loser_exit twice if price oscillates
        # around the threshold.  Cleaned up in _refresh_pending_markets when
        # the pair is pruned.
        self._pending_exits: set[str] = set()

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info(
            "OpeningNeutralScanner started",
            market_types=config.OPENING_NEUTRAL_MARKET_TYPES,
            window_secs=config.OPENING_NEUTRAL_ENTRY_WINDOW_SECS,
            combined_cost_max=config.OPENING_NEUTRAL_COMBINED_COST_MAX,
            dry_run=config.OPENING_NEUTRAL_DRY_RUN,
        )
        # Register event-driven WS price callback.
        # Fires on every book/price_change update for any subscribed token.
        self._pm.on_price_change(self._on_price_event)
        # Pre-populate the pending-market map so we catch markets that are already
        # in their opening window when the scanner starts.
        await self._refresh_pending_markets()
        # Background loop: re-syncs pending markets as new buckets are listed.
        asyncio.create_task(self._subscription_loop(), name="opening_neutral_presub")

    async def stop(self) -> None:
        self._running = False

    def get_signals(self) -> list[dict]:
        """Return snapshot of recent opening-neutral signal attempts."""
        return list(self._signals[-50:])

    # ── Status helper (used by api_server endpoint) ───────────────────────────

    def get_status(self) -> dict:
        """Return a status snapshot for the /opening_neutral/status endpoint."""
        pairs_out = []
        for pair_id, pair in self._active_pairs.items():
            yes_p = pair.get("yes_pos")
            no_p = pair.get("no_pos")
            pairs_out.append({
                "pair_id": pair_id,
                "market_id": pair.get("market_id", ""),
                "market_title": pair.get("market_title", ""),
                "yes_entry": round(yes_p.entry_price, 4) if yes_p else None,
                "no_entry": round(no_p.entry_price, 4) if no_p else None,
                "yes_closed": yes_p.is_closed if yes_p else None,
                "no_closed": no_p.is_closed if no_p else None,
                "yes_exit_order_id": pair.get("yes_exit_order_id", ""),
                "no_exit_order_id": pair.get("no_exit_order_id", ""),
            })

        # Build live tracking snapshot — fresh book prices for every pending market.
        # Computed on each get_status() call so the webapp always shows current prices.
        now = time.time()
        tracked: list[dict] = []
        for cond_id, market in self._pending_markets.items():
            yes_book = self._pm.get_book(market.token_id_yes)
            no_book  = self._pm.get_book(market.token_id_no)
            yes_ask = yes_book.best_ask if yes_book is not None else None
            no_ask  = no_book.best_ask  if no_book  is not None else None
            tte = (market.end_date.timestamp() - now) if market.end_date else None
            duration = _MARKET_TYPE_DURATION_SECS.get(market.market_type) or 0
            elapsed  = (duration - tte) if (duration and tte is not None) else None
            combined = round(yes_ask + no_ask, 4) if (yes_ask is not None and no_ask is not None) else None
            # Skip markets that haven't opened yet (presub window; elapsed < 0)
            if elapsed is not None and elapsed < 0:
                continue
            # Source-of-truth for confirmed entry: an active pair exists for this market.
            # _entered_market_ids is set at intent time (before orders) and is used only
            # to block duplicate attempts — it is NOT a confirmed-fill indicator.
            confirmed_entered = any(
                p.get("market_id") == cond_id for p in self._active_pairs.values()
            )
            # In-flight: order placed but fill not yet confirmed.
            currently_entering = cond_id in self._entering_markets
            tracked.append({
                "market_id":    cond_id,
                "market_title": getattr(market, "title", "")[:80],
                "market_type":  getattr(market, "market_type", ""),
                "yes_ask":      yes_ask,
                "no_ask":       no_ask,
                "combined":     combined,
                "tte_secs":     round(tte)     if tte     is not None else None,
                "elapsed_secs": round(elapsed) if elapsed is not None else None,
                "entered":      confirmed_entered,
                "entering":     currently_entering,
            })
        # Sort: unactioned first, then entering, then entered; within each group by TTE asc.
        tracked.sort(key=lambda m: (1 if m["entered"] else (0 if not m["entering"] else 0), m["tte_secs"] or 9999))

        return {
            "enabled":         config.OPENING_NEUTRAL_ENABLED,
            "dry_run":         config.OPENING_NEUTRAL_DRY_RUN,
            "active_pairs":    len(self._active_pairs),
            "pairs":           pairs_out,
            "closed_pairs":    list(self._closed_pairs[-10:]),
            "recent_signals":  list(self._signals[-20:]),
            "tracked_markets": tracked,
            "entry_window_secs": config.OPENING_NEUTRAL_ENTRY_WINDOW_SECS,
        }

    # ── WS subscription refresh loop ─────────────────────────────────────────

    async def _subscription_loop(self) -> None:
        """Periodically re-sync the pending-market map as new buckets are listed."""
        while self._running:
            try:
                await self._refresh_pending_markets()
            except Exception as exc:  # pylint: disable=broad-except
                log.error("OpeningNeutralScanner: subscription refresh error", exc=str(exc))
            await asyncio.sleep(30)

    async def _refresh_pending_markets(self) -> None:
        """
        Sync _pending_markets with pm.get_markets().

        Any qualifying market (correct type, Up/Down direction, not yet entered,
        not in active_pairs) is registered in _pending_markets and _token_to_pending
        so _on_price_event can perform O(1) lookup when WS events arrive.

        Expired pending markets are pruned on each call.
        """
        if not config.OPENING_NEUTRAL_ENABLED:
            return

        now = time.time()
        markets = self._pm.get_markets()

        for market in markets.values():
            market_type = getattr(market, "market_type", None)
            if market_type not in config.OPENING_NEUTRAL_MARKET_TYPES:
                continue
            if not _is_updown_market(getattr(market, "title", "")):
                continue
            cond_id = getattr(market, "condition_id", None)
            if not cond_id:
                continue
            if cond_id in self._pending_markets:
                continue
            if cond_id in self._entering_markets:
                continue
            if any(p.get("market_id") == cond_id for p in self._active_pairs.values()):
                continue
            if market.end_date is None:
                continue
            tte_now = market.end_date.timestamp() - now
            if tte_now <= 0:
                continue  # already expired

            # Only subscribe markets that are inside (or about to enter) the entry
            # window.  Without this gate, pm.get_markets() returns hundreds of future
            # buckets and past-window markets, causing the "1791 markets" problem.
            # The prune loop below would remove them, but the add loop re-added them
            # on the very next refresh cycle.  Gate at add time instead.
            duration = _MARKET_TYPE_DURATION_SECS.get(market_type) or 0
            if duration > 0:
                elapsed_now = duration - tte_now
                # Skip markets that haven't opened yet (allow 30 s presub window so
                # book data is ready the moment the entry window opens).
                if elapsed_now < -30:
                    continue
                # Skip markets already past their entry window.
                if elapsed_now > config.OPENING_NEUTRAL_ENTRY_WINDOW_SECS:
                    continue

            self._pending_markets[cond_id] = market
            # Only register the YES token in the entry-path lookup.
            # Registering both YES and NO caused double-evaluation on every WS
            # batch (one task per token).  When the YES token fires, we re-read
            # both books from the PMClient cache, so the latest NO ask is always
            # captured.  NO-token-only price movements are caught on the next
            # YES event (at most _ENTRY_EVAL_DEBOUNCE_SECS later).
            self._token_to_pending[market.token_id_yes] = cond_id
            log.debug(
                "OpeningNeutral: registered pending market",
                market=getattr(market, "title", "")[:60],
                market_type=market_type,
                tte_secs=round(market.end_date.timestamp() - now),
            )

        # Prune expired pending markets (TTE ≤ 0).
        expired_cids = [
            cid for cid, mkt in self._pending_markets.items()
            if mkt.end_date is None or mkt.end_date.timestamp() <= now
        ]
        for cid in expired_cids:
            mkt = self._pending_markets.pop(cid)
            # Only YES token was registered in _token_to_pending (see above).
            self._token_to_pending.pop(getattr(mkt, "token_id_yes", None), None)

        # Prune markets that have drifted past the entry window without being entered.
        # Once elapsed > ENTRY_WINDOW_SECS, _evaluate_entry will always exit early on
        # the window gate — continuing to receive WS events for them wastes CPU.
        past_window_cids = [
            cid for cid, mkt in self._pending_markets.items()
            if cid not in self._entered_market_ids
            and mkt.end_date is not None
            and (lambda d=_MARKET_TYPE_DURATION_SECS.get(mkt.market_type) or 0,
                      t=mkt.end_date.timestamp() - now: d > 0 and (d - t) > config.OPENING_NEUTRAL_ENTRY_WINDOW_SECS)()
        ]
        for cid in past_window_cids:
            mkt = self._pending_markets.pop(cid)
            self._token_to_pending.pop(getattr(mkt, "token_id_yes", None), None)
            log.debug("OpeningNeutral: delisted past-window market", market_id=cid[:22])

        # Prune expired entries from the persistent entered-market set so that
        # if the same underlying re-lists with a new condition_id, the new bucket
        # is not confused with the old one.  Use the pending-markets expiry as
        # proxy; also prune any condition_id not seen in the current market list.
        live_cids = {getattr(m, "condition_id", "") for m in markets.values()}
        self._entered_market_ids -= (self._entered_market_ids - live_cids)

        # Prune active pairs where both legs are now closed (resolved by monitor.py or
        # manually exited).  _check_one_pair normally handles this, but it only runs
        # when a WS price event fires — which stops after market resolution as books
        # drain.  This periodic sweep is the backstop so resolved pairs don't stay
        # stuck in _active_pairs indefinitely.
        resolved_pair_ids = [
            pid for pid, pair in self._active_pairs.items()
            if self._pair_is_resolved(pair)
        ]
        for pid in resolved_pair_ids:
            pair = self._active_pairs.pop(pid, {})
            yes_p = pair.get("yes_pos")
            no_p  = pair.get("no_pos")
            if yes_p:
                self._token_to_pair.pop(getattr(yes_p, "token_id", ""), None)
            if no_p:
                self._token_to_pair.pop(getattr(no_p, "token_id", ""), None)
            # Clean up loser-exit debounce keys for this pair.
            self._pending_exits.discard(f"{pid}_YES")
            self._pending_exits.discard(f"{pid}_NO")
            self._closed_pairs.append({
                "pair_id":      pid,
                "market_id":    pair.get("market_id", ""),
                "market_title": pair.get("market_title", ""),
                "yes_entry":    round(yes_p.entry_price, 4) if yes_p else None,
                "no_entry":     round(no_p.entry_price,  4) if no_p  else None,
                "closed_at":    datetime.now(timezone.utc).isoformat(),
            })
            if len(self._closed_pairs) > 20:
                self._closed_pairs = self._closed_pairs[-20:]
            log.info(
                "OpeningNeutral: pair pruned (both legs closed)",
                pair_id=pid[:12],
                market_id=pair.get("market_id", "")[:22],
            )

        # Subscribe all pending markets (both YES and NO tokens) to the PM WS so
        # that pm.get_book() returns live prices.  Uses a named owner so the
        # momentum scanner's own registrations are not overwritten.
        # Also include tokens from active (not-yet-resolved) pairs so WS book
        # events keep firing after entry — required for loser-exit evaluation.
        extra = {
            t
            for mkt in self._pending_markets.values()
            for t in (mkt.token_id_yes, mkt.token_id_no)
            if t
        }
        for pair in self._active_pairs.values():
            if self._pair_is_resolved(pair):
                continue
            yes_p = pair.get("yes_pos")
            no_p  = pair.get("no_pos")
            if yes_p and not yes_p.is_closed:
                t = getattr(yes_p, "token_id", "")
                if t:
                    extra.add(t)
            if no_p and not no_p.is_closed:
                t = getattr(no_p, "token_id", "")
                if t:
                    extra.add(t)
        self._pm.register_for_book_updates(extra, owner="opening_neutral")

    # ── WS price-event handler ────────────────────────────────────────────────

    async def _on_price_event(self, token_id: str, mid: float) -> None:  # noqa: ARG002
        """
        Handle a WS price-change event (fires for every book/price_change update).

        Entry path  — token belongs to a pending (not-yet-entered) market:
            Evaluates all entry gates and spawns _enter_pair when qualifying.

        Loser-exit path — token belongs to an active neutral pair:
            When the token's mid price falls to ≤ OPENING_NEUTRAL_LOSER_EXIT_PRICE,
            a taker SELL is executed to recover partial value.

        The book snapshot in pm.get_book() is already updated before this
        callback fires, so yes_ask / best_bid reads are always fresh.
        """
        if not config.OPENING_NEUTRAL_ENABLED:
            return

        # ── Entry path ────────────────────────────────────────────────────────
        cond_id = self._token_to_pending.get(token_id)
        if cond_id is not None:
            now_e = time.time()
            if (
                cond_id not in self._entering_markets
                and now_e - self._market_last_eval.get(cond_id, 0.0) >= _ENTRY_EVAL_DEBOUNCE_SECS
            ):
                self._market_last_eval[cond_id] = now_e
                market = self._pending_markets.get(cond_id)
                if market is not None:
                    await self._evaluate_entry(market)
            return

        # ── Loser-exit monitoring path ────────────────────────────────────────
        # A token registered in _token_to_pair belongs to an active neutral pair.
        # When its mid price drops to the loser-exit threshold we trigger a sell.
        pair_id = self._token_to_pair.get(token_id)
        if pair_id is not None:
            pair = self._active_pairs.get(pair_id)
            if pair is not None and not self._pair_is_resolved(pair):
                await self._maybe_exit_loser(pair_id, pair, token_id, mid)

    async def _evaluate_entry(self, market: Any) -> None:
        """
        Check all entry gates for a pending market; spawn _enter_pair if all pass.

        Called from _on_price_event — on the hot WS path.  Returns immediately
        if any gate fails so the event loop is not held.
        """
        if not config.OPENING_NEUTRAL_ENABLED:
            return

        market_id = getattr(market, "condition_id", None)
        if not market_id:
            return

        # Persistent per-session entry guard — prevents re-evaluation for any market
        # that has already been entered in this session, even if cleanup races or
        # _register_pair raised silently.
        if market_id in self._entered_market_ids:
            return

        # Concurrent-pair cap: count both active pairs and in-flight entries.
        open_pairs = sum(
            1 for p in self._active_pairs.values()
            if not self._pair_is_resolved(p)
        )
        in_flight = len(self._entering_markets)
        if open_pairs + in_flight >= config.OPENING_NEUTRAL_MAX_CONCURRENT:
            return

        # TTE / entry-window gate.
        if market.end_date is None:
            return
        now = time.time()
        tte_secs = market.end_date.timestamp() - now
        if tte_secs <= 0:
            return
        duration = _MARKET_TYPE_DURATION_SECS.get(market.market_type) or tte_secs
        elapsed = duration - tte_secs
        if elapsed < 0 or elapsed > config.OPENING_NEUTRAL_ENTRY_WINDOW_SECS:
            return

        # Conflict guard: skip if another strategy has an open position here.
        open_positions = self._risk.get_open_positions()
        if any(p.market_id == market_id for p in open_positions):
            return

        # Fetch YES and NO best asks from the freshly-updated book cache.
        _yes_book = self._pm.get_book(market.token_id_yes)
        _no_book  = self._pm.get_book(market.token_id_no)
        yes_ask = _yes_book.best_ask if _yes_book is not None else None
        no_ask  = _no_book.best_ask  if _no_book  is not None else None
        if yes_ask is None or no_ask is None:
            return

        combined = round(yes_ask + no_ask, 6)
        diag: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "market_title": getattr(market, "title", "")[:80],
            "market_type": getattr(market, "market_type", ""),
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "combined": combined,
            "threshold": config.OPENING_NEUTRAL_COMBINED_COST_MAX,
            "tte_secs": round(tte_secs),
            "elapsed_secs": round(elapsed),
        }

        if combined > config.OPENING_NEUTRAL_COMBINED_COST_MAX:
            diag["result"] = "too_expensive"
            self._signals.append(diag)
            if len(self._signals) > 200:
                self._signals = self._signals[-100:]
            return

        # Per-side price band: both legs must be near 50/50.
        # Prevents entries into highly-skewed markets (e.g. YES=0.12 / NO=0.89)
        # where the exit-at-0.35 logic breaks down (entry already below exit price).
        lo = config.OPENING_NEUTRAL_MIN_SIDE_PRICE
        hi = config.OPENING_NEUTRAL_MAX_SIDE_PRICE
        if not (lo <= yes_ask <= hi and lo <= no_ask <= hi):
            diag["result"] = "skewed"
            self._signals.append(diag)
            if len(self._signals) > 200:
                self._signals = self._signals[-100:]
            return

        # Entry qualifies — spawn entry task (non-blocking).
        diag["result"] = "entry_attempt"
        self._signals.append(diag)
        if len(self._signals) > 200:
            self._signals = self._signals[-100:]

        # Mark as entered NOW (before the order is in-flight) so re-evaluation
        # is blocked for the rest of this market's window regardless of whether
        # _register_pair completes successfully.
        self._entered_market_ids.add(market_id)
        self._entering_markets.add(market_id)

        def _log_entry_exc(task: asyncio.Task) -> None:
            exc = task.exception()
            if exc is not None:
                log.error(
                    "OpeningNeutral: _enter_pair raised",
                    market_id=market_id[:22],
                    exc=str(exc),
                )

        task = asyncio.create_task(
            self._enter_pair(market, yes_ask, no_ask),
            name=f"on_entry_{market_id[:20]}",
        )
        task.add_done_callback(_log_entry_exc)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def _enter_pair(self, market: Any, yes_ask: float, no_ask: float) -> None:
        """
        Attempt to simultaneously fill YES and NO legs.

        Both orders are placed concurrently.  After ENTRY_TIMEOUT_SECS the
        unfilled leg is abandoned and the fallback strategy applies.
        """
        market_id: str = market.condition_id
        try:
            pair_id = uuid.uuid4().hex
            size_usd = config.OPENING_NEUTRAL_SIZE_USD

            log.info(
                "OpeningNeutral: entering pair",
                market=market.title[:60],
                yes_ask=yes_ask,
                no_ask=no_ask,
                combined=round(yes_ask + no_ask, 6),
                pair_id=pair_id[:12],
                dry_run=config.OPENING_NEUTRAL_DRY_RUN,
            )

            # Resolve token IDs for YES and NO sides.
            yes_token_id = market.token_id_yes
            no_token_id = market.token_id_no
            if not yes_token_id or not no_token_id:
                log.warning("OpeningNeutral: cannot resolve token IDs", market_id=market_id[:22])
                return

            # Place both orders concurrently.
            yes_order_task = asyncio.create_task(
                self._place_leg(yes_token_id, "YES", yes_ask, size_usd, market)
            )
            no_order_task = asyncio.create_task(
                self._place_leg(no_token_id, "NO", no_ask, size_usd, market)
            )

            done, pending = await asyncio.wait(
                [yes_order_task, no_order_task],
                timeout=config.OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS,
                return_when=asyncio.ALL_COMPLETED,
            )

            # Cancel any still-pending tasks.
            for t in pending:
                t.cancel()

            # Guard: task.result() raises if _place_leg raised an exception.
            # Treat a failed leg as unfilled so the fallback path handles it.
            try:
                yes_result = yes_order_task.result() if yes_order_task in done else None
            except Exception as _exc:  # pylint: disable=broad-except
                log.warning("OpeningNeutral: YES leg task raised", exc=str(_exc))
                yes_result = None
            try:
                no_result = no_order_task.result() if no_order_task in done else None
            except Exception as _exc:  # pylint: disable=broad-except
                log.warning("OpeningNeutral: NO leg task raised", exc=str(_exc))
                no_result = None

            yes_filled = yes_result and yes_result.get("filled")
            no_filled = no_result and no_result.get("filled")

            if yes_filled and no_filled:
                await self._register_pair(
                    pair_id, market, yes_result, no_result,
                    yes_token_id, no_token_id,
                )
            elif yes_filled and not no_filled:
                await self._handle_one_leg_fill(
                    pair_id, market, yes_result, "YES", yes_token_id
                )
            elif no_filled and not yes_filled:
                await self._handle_one_leg_fill(
                    pair_id, market, no_result, "NO", no_token_id
                )
            else:
                log.info(
                    "OpeningNeutral: neither leg filled — no position taken",
                    market=market.title[:60],
                )
                _emit_event(
                    "OPENING_NEUTRAL_NO_FILL",
                    market_id=market_id,
                    market_title=market.title[:80],
                    market_type=getattr(market, "market_type", ""),
                    underlying=getattr(market, "underlying", ""),
                )
        finally:
            self._entering_markets.discard(market_id)

    async def _place_leg(
        self,
        token_id: str,
        side: str,
        ask_price: float,
        size_usd: float,
        market: Any,
    ) -> dict:
        """
        Place one leg (YES or NO) and wait for fill.

        Returns a dict: {"filled": bool, "price": float, "size": float, "order_id": str}.
        In DRY_RUN mode, always returns filled=False so no positions are registered.
        """
        if config.OPENING_NEUTRAL_DRY_RUN:
            # Simulate a fill at the observed ask so the pair is registered in
            # _active_pairs.  Without this the market is re-scanned every tick
            # because no pair is ever recorded, producing infinite duplicate signals.
            # _exit_loser has its own DRY_RUN guard so no real orders are placed
            # during monitoring either.
            sim_size = round(size_usd / ask_price, 6) if ask_price > 0 else 0.0
            log.debug(
                "OpeningNeutral DRY_RUN: simulating fill",
                side=side, price=ask_price, size=sim_size,
            )
            return {"filled": True, "price": ask_price, "size": sim_size, "order_id": f"dry_{uuid.uuid4().hex[:8]}"}

        # Taker limit: cross spread by +0.5c.
        place_price = round(min(ask_price + 0.005, 0.99), 3)
        # OrderArgs expects size in contracts (shares), not USD.
        contracts = round(size_usd / place_price, 6) if place_price > 0 else 0.0

        if config.OPENING_NEUTRAL_ORDER_TYPE == "market":
            order_id = await self._pm.place_market(
                token_id=token_id, side="BUY", price=place_price, size=size_usd
            )
        else:
            order_id = await self._pm.place_limit(
                token_id=token_id,
                side="BUY",
                price=place_price,
                size=contracts,
                market=market,
                post_only=False,
            )

        if not order_id:
            log.warning("OpeningNeutral: order placement rejected", side=side)
            return {"filled": False, "price": place_price, "size": 0.0, "order_id": ""}

        # Paper mode: instant fill.
        if self._pm._paper_mode:
            entry_size = round(size_usd / place_price, 6)
            return {"filled": True, "price": place_price, "size": entry_size, "order_id": order_id}

        # Wait for WS fill event.
        # FAK orders are fill-or-kill: if the exchange matched them the trade WS
        # event arrives within ~1-2s.  Use the short FAK timeout so a killed leg
        # is detected in seconds rather than the full 30s entry timeout.
        fill_future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pm.register_fill_future(order_id, fill_future)
        try:
            fill_event = await asyncio.wait_for(
                fill_future, timeout=config.OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS
            )
            ws_price = float(fill_event.get("price") or 0)
            ws_size = float(fill_event.get("size_matched") or 0)
            if ws_price > 0 and ws_size > 0:
                return {"filled": True, "price": ws_price, "size": ws_size, "order_id": order_id}
            # WS fired but empty — REST fallback.
            rest = await self._pm.get_order_fill_rest(order_id)
            if rest:
                return {
                    "filled": True,
                    "price": rest["price"],
                    "size": rest["size_matched"],
                    "order_id": order_id,
                }
        except asyncio.TimeoutError:
            # FAK is already dead at the exchange (fill-or-kill) — no cancel needed.
            # Do a final REST check in case the WS event was delayed.
            rest = await self._pm.get_order_fill_rest(order_id)
            if rest:
                return {
                    "filled": True,
                    "price": rest["price"],
                    "size": rest["size_matched"],
                    "order_id": order_id,
                }
            log.info("OpeningNeutral: FAK leg unfilled (killed at exchange)", side=side)

        return {"filled": False, "price": place_price, "size": 0.0, "order_id": order_id}

    # ── Post-fill registration ────────────────────────────────────────────────

    async def _register_pair(
        self,
        pair_id: str,
        market: Any,
        yes_result: dict,
        no_result: dict,
        yes_token_id: str,
        no_token_id: str,
    ) -> None:
        """Register both legs with the RiskEngine and track the active pair."""
        market_id = market.condition_id
        market_title = getattr(market, "title", "")

        yes_pos = self._build_position(
            market, "YES", yes_result, yes_token_id, pair_id
        )
        no_pos = self._build_position(
            market, "NO", no_result, no_token_id, pair_id
        )

        self._risk.open_position(yes_pos)
        self._risk.open_position(no_pos)

        self._active_pairs[pair_id] = {
            "market_id": market_id,
            "market_title": market_title[:80],
            "yes_pos": yes_pos,
            "no_pos": no_pos,
        }

        # Register token → pair reverse map so _on_price_event routes monitor events.
        self._token_to_pair[yes_token_id] = pair_id
        self._token_to_pair[no_token_id]  = pair_id

        # Remove from pending now that entry is registered.
        # Only the YES token was in _token_to_pending (NO was never registered
        # on the entry path — see _refresh_pending_markets).
        self._pending_markets.pop(market_id, None)
        self._token_to_pending.pop(yes_token_id, None)

        combined_cost = round(yes_pos.entry_price + no_pos.entry_price, 6)
        log.info(
            "OpeningNeutral: both legs filled — pair registered",
            market=market_title[:60],
            pair_id=pair_id[:12],
            yes_entry=yes_pos.entry_price,
            no_entry=no_pos.entry_price,
            combined_cost=combined_cost,
            guaranteed_pnl=round(1.0 - combined_cost, 6),
        )
        _emit_event(
            "OPENING_NEUTRAL_PAIR_REGISTERED",
            market_id=market_id,
            market_title=market_title[:80],
            market_type=getattr(market, "market_type", ""),
            underlying=getattr(market, "underlying", ""),
            pair_id=pair_id,
            yes_entry=yes_pos.entry_price,
            yes_size=yes_pos.size,
            no_entry=no_pos.entry_price,
            no_size=no_pos.size,
            combined_cost=combined_cost,
            guaranteed_pnl=round(1.0 - combined_cost, 6),
        )
        # Wake state_sync_loop immediately so the new positions appear in the
        # webapp without waiting the 1-second backstop interval.
        if self._on_open_callback is not None:
            self._on_open_callback()
        # Loser-exit monitoring starts automatically: _on_price_event watches
        # both token_ids via _token_to_pair and calls _maybe_exit_loser when
        # either token's mid price drops to ≤ OPENING_NEUTRAL_LOSER_EXIT_PRICE.

    async def _handle_one_leg_fill(
        self,
        pair_id: str,
        market: Any,
        result: dict,
        side: str,
        token_id: str,
    ) -> None:
        """Handle partial fill (only one leg filled)."""
        # Clean up pending state — one leg filled means entry is complete
        # (win or fail), so this market must not be re-entered.
        market_id = getattr(market, "condition_id", "")
        self._pending_markets.pop(market_id, None)
        # Only YES token was registered (NO was never added to _token_to_pending).
        self._token_to_pending.pop(getattr(market, "token_id_yes", ""), None)

        fallback = config.OPENING_NEUTRAL_ONE_LEG_FALLBACK
        market_title = getattr(market, "title", "")

        if fallback == "keep_as_momentum":
            # Promote to a standard momentum position (no neutral_pair_id set).
            pos = self._build_position(market, side, result, token_id, pair_id="")
            pos.strategy = "momentum"
            pos.neutral_pair_id = ""
            if config.MOMENTUM_PROB_SL_ENABLED:
                pos.prob_sl_threshold = round(
                    pos.entry_price * (1.0 - config.MOMENTUM_PROB_SL_PCT), 6
                )
            self._risk.open_position(pos)
            log.info(
                "OpeningNeutral: one-leg fill — promoting to momentum",
                market=market_title[:60],
                side=side,
                entry=result["price"],
            )
            _emit_event(
                "OPENING_NEUTRAL_ONE_LEG_PROMOTED",
                market_id=market_id,
                market_title=market_title[:80],
                market_type=getattr(market, "market_type", ""),
                underlying=getattr(market, "underlying", ""),
                pair_id=pair_id,
                side=side,
                entry_price=result["price"],
                entry_size=result["size"],
                order_id=result.get("order_id", ""),
            )
        else:
            # exit_immediately: taker-exit at best bid.
            log.info(
                "OpeningNeutral: one-leg fill — exiting immediately",
                market=market_title[:60],
                side=side,
            )
            _emit_event(
                "OPENING_NEUTRAL_ONE_LEG_EXITED",
                market_id=market_id,
                market_title=market_title[:80],
                market_type=getattr(market, "market_type", ""),
                underlying=getattr(market, "underlying", ""),
                pair_id=pair_id,
                side=side,
                entry_price=result["price"],
                entry_size=result["size"],
                order_id=result.get("order_id", ""),
            )
            if not config.OPENING_NEUTRAL_DRY_RUN:
                await self._pm.place_market(
                    token_id=token_id, side="SELL",
                    price=0.01, size=result["size"]
                )

    def _build_position(
        self,
        market: Any,
        side: str,
        result: dict,
        token_id: str,
        pair_id: str,
    ) -> Position:
        """Construct a Position dataclass from fill result."""
        entry_price = result["price"]
        entry_size = result["size"]
        return Position(
            market_id=market.condition_id,
            market_type=getattr(market, "market_type", ""),
            underlying=getattr(market, "underlying", ""),
            side=side,
            size=entry_size,
            entry_price=entry_price,
            entry_cost_usd=round(entry_price * entry_size, 6),
            strategy="opening_neutral",
            token_id=token_id,
            market_title=getattr(market, "title", ""),
            order_id=result.get("order_id", ""),
            neutral_pair_id=pair_id,
            tte_years=getattr(market, "tte_seconds", 0) / (365.25 * 86400),
            spot_price=self._spot.get_mid(getattr(market, "underlying", ""), getattr(market, "market_type", "")) or 0.0,
            strike=getattr(market, "strike", 0.0) or 0.0,
        )

    # ── Loser-exit monitoring ─────────────────────────────────────────────────

    async def _maybe_exit_loser(
        self,
        pair_id: str,
        pair: dict,
        token_id: str,
        mid: float,
    ) -> None:
        """
        Called from _on_price_event for every price tick on an active-pair token.

        Triggers a taker-exit SELL when the token's mid price drops to
        ≤ OPENING_NEUTRAL_LOSER_EXIT_PRICE.  A per-pair debounce flag
        (_pending_exits) prevents the same leg from firing twice if the
        price oscillates around the threshold.
        """
        yes_pos: Position = pair["yes_pos"]
        no_pos:  Position = pair["no_pos"]

        if token_id == yes_pos.token_id:
            loser_pos  = yes_pos
            filled_side = "YES"
        else:
            loser_pos  = no_pos
            filled_side = "NO"

        if loser_pos.is_closed:
            return
        if mid > config.OPENING_NEUTRAL_LOSER_EXIT_PRICE:
            return

        # Wide-spread false-positive guard.
        # When a PM WS shard reconnects it delivers a fresh book snapshot that
        # can have a very low bid (market-maker repositioning) while the ask
        # stays near the entry price.  This creates an artificially low mid
        # (≤ threshold) even though the token hasn't genuinely declined.
        # A token at genuine fair-value 0.35 will have ask ≈ 0.36–0.38.
        # Any ask still within 95 % of entry price means the ask hasn't moved
        # and the mid is a wide-spread artefact — not a real price drop.
        _book = self._pm.get_book(loser_pos.token_id)
        _ask = _book.best_ask if _book else None
        if _ask is None or _ask > loser_pos.entry_price * 0.95:
            return  # no ask visible or ask near entry — reconnect artefact, not genuine decline

        exit_key = f"{pair_id}_{filled_side}"
        if exit_key in self._pending_exits:
            return  # already in flight

        self._pending_exits.add(exit_key)

        def _on_done(task: asyncio.Task) -> None:
            exc = task.exception()
            if exc is not None:
                log.error(
                    "OpeningNeutral: _execute_loser_exit raised",
                    pair_id=pair_id[:12],
                    side=filled_side,
                    exc=str(exc),
                )

        task = asyncio.create_task(
            self._execute_loser_exit(pair_id, filled_side, loser_pos, mid),
            name=f"loser_exit_{pair_id[:12]}_{filled_side}",
        )
        task.add_done_callback(_on_done)

    async def _execute_loser_exit(
        self,
        pair_id: str,
        filled_side: str,
        loser_pos: "Position",
        trigger_mid: float,
    ) -> None:
        """
        Execute the loser-exit taker SELL and record the fill.

        DRY_RUN: simulates the fill at trigger_mid and calls _on_exit_fill.
        Paper/Live: places a FAK SELL, waits for the WS fill event, then
        calls _on_exit_fill with the actual fill price.

        On FAK rejection (empty book near expiry) falls back to a post_only
        resting SELL at best bid per preamble exit-order-handling rules.
        """
        actual_price = trigger_mid  # fallback if fill event has no price

        if config.OPENING_NEUTRAL_DRY_RUN:
            log.debug(
                "OpeningNeutral DRY_RUN: simulating loser exit",
                pair_id=pair_id[:12],
                side=filled_side,
                price=trigger_mid,
            )
            # Simulate at the configured exit price (0.35), not trigger_mid.
            # In live mode a FAK limit-sell is placed AT 0.35; if trigger_mid
            # has already dipped below 0.35 the order would be rejected anyway.
            await self._on_exit_fill(pair_id, filled_side,
                                     exit_price=config.OPENING_NEUTRAL_LOSER_EXIT_PRICE)
            return

        order_id = await self._pm.place_limit(
            token_id=loser_pos.token_id,
            side="SELL",
            price=config.OPENING_NEUTRAL_LOSER_EXIT_PRICE,
            size=loser_pos.size,
            market=None,
            post_only=False,
        )

        if not order_id:
            log.error(
                "OpeningNeutral: loser exit FAK rejected — position left open, manual action required",
                pair_id=pair_id[:12],
                side=filled_side,
            )
            return

        if self._pm._paper_mode:
            await self._on_exit_fill(pair_id, filled_side, exit_price=trigger_mid)
            return

        fill_future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pm.register_fill_future(order_id, fill_future)
        try:
            fill_event = await asyncio.wait_for(
                fill_future,
                timeout=config.OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS,
            )
            ws_price = float(fill_event.get("price") or 0)
            if ws_price > 0:
                actual_price = ws_price
        except asyncio.TimeoutError:
            rest = await self._pm.get_order_fill_rest(order_id)
            if rest:
                actual_price = rest["price"]
            else:
                await self._pm.cancel_order(order_id)
                log.error(
                    "OpeningNeutral: loser exit fill timeout — position left open, manual action required",
                    pair_id=pair_id[:12],
                    side=filled_side,
                )
                return

        await self._on_exit_fill(pair_id, filled_side, exit_price=actual_price)

    async def _on_exit_fill(
        self,
        pair_id: str,
        filled_side: str,
        exit_price: Optional[float] = None,
    ) -> None:
        """
        Handle a loser-exit fill on one side of a neutral pair.

        1. Close the loser in the risk engine at the actual fill price.
        2. Promote the winner to momentum and arm its prob-SL.
        3. Fire on_close_callback.

        Idempotent: if the loser position is already closed, returns immediately.
        """
        if exit_price is None:
            exit_price = config.OPENING_NEUTRAL_LOSER_EXIT_PRICE

        pair = self._active_pairs.get(pair_id)
        if pair is None:
            return  # idempotent guard — pair already pruned

        if filled_side == "YES":
            loser_pos: Position  = pair["yes_pos"]
            winner_pos: Position = pair["no_pos"]
        else:
            loser_pos  = pair["no_pos"]
            winner_pos = pair["yes_pos"]

        if loser_pos.is_closed:
            return  # idempotent guard — already closed

        market_id = loser_pos.market_id

        log.info(
            "OpeningNeutral: loser exit filled — closing loser, promoting winner",
            pair_id=pair_id[:12],
            loser_side=filled_side,
            market=loser_pos.market_title[:60],
            exit_price=exit_price,
        )

        # Close loser in risk engine — writes trade record to trades.csv.
        self._risk.close_position(
            market_id,
            exit_price,
            side=filled_side,
        )

        # Promote winner to momentum and arm its prob-SL threshold.
        if config.MOMENTUM_PROB_SL_ENABLED:
            winner_pos.prob_sl_threshold = round(
                winner_pos.entry_price * (1.0 - config.MOMENTUM_PROB_SL_PCT), 6
            )
        winner_pos.strategy = "momentum"
        winner_pos.neutral_pair_id = ""  # clear so it is treated as a plain momentum pos

        log.info(
            "OpeningNeutral: winner promoted to momentum",
            pair_id=pair_id[:12],
            side=winner_pos.side,
            entry=winner_pos.entry_price,
            prob_sl_threshold=winner_pos.prob_sl_threshold,
        )

        # ── Stop monitoring this pair ────────────────────────────────────────
        # The winner is now owned by the momentum monitor.  Remove the pair
        # and both token IDs from opening-neutral state so that subsequent
        # price ticks for the winner token do NOT re-trigger _maybe_exit_loser
        # (which would close the winner prematurely as a "second loser").
        self._active_pairs.pop(pair_id, None)
        self._token_to_pair.pop(loser_pos.token_id, None)
        self._token_to_pair.pop(winner_pos.token_id, None)
        self._pending_exits.discard(f"{pair_id}_YES")
        self._pending_exits.discard(f"{pair_id}_NO")

        # Fire close callback so main.py can update cooldowns and emit
        # notify_state_changed().
        if self._on_close_callback is not None:
            self._on_close_callback(market_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pair_is_resolved(self, pair: dict) -> bool:
        """True if both legs of a pair are closed (market resolved or manual exit)."""
        yes_p: Optional[Position] = pair.get("yes_pos")
        no_p: Optional[Position] = pair.get("no_pos")
        return (
            (yes_p is None or yes_p.is_closed)
            and (no_p is None or no_p.is_closed)
        )
