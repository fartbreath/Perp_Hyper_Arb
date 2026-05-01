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
        # Bid-monitoring exit state for active pairs.
        # Maps token_id → pair_id for all tokens being watched for bid-threshold exit.
        self._token_to_pair: dict[str, str] = {}
        # Token IDs for which a loser market sell is currently in-flight.
        # Guards against duplicate exit tasks when multiple WS ticks fire before
        # the first _execute_loser_exit task completes.
        self._exiting_legs: set[str] = set()
        # Market IDs where a one-leg fill was promoted to momentum.
        # These occupy a concurrent slot (counted against MAX_CONCURRENT) until the
        # market expires so sequential entries across multiple coins at the same
        # bucket-open time are blocked just like truly-simultaneous ones.
        # Pruned in _refresh_pending_markets when the market_id leaves the live set.
        self._promoted_slots: set[str] = set()
        # ── Scheduled-timer entry state (ideas 1, 2, 5) ─────────────────────
        # condition_ids for which a _scheduled_entry_task is already running.
        # Guards against duplicate timer tasks when _refresh_pending_markets fires
        # multiple times before the market opens.
        self._scheduled_entry_market_ids: set[str] = set()

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info(
            "OpeningNeutralScanner started",
            market_types=config.OPENING_NEUTRAL_MARKET_TYPES,
            window_secs=config.OPENING_NEUTRAL_MARKET_WINDOW_SECS,
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
            # Skip markets deeper in the presub window than the entry timer advance.
            # We show markets from TIMER_ADVANCE_SECS before open so the webapp
            # displays the pre-market entry as it fires.
            if elapsed is not None and elapsed < -(config.OPENING_NEUTRAL_TIMER_ADVANCE_SECS + 1.0):
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
            "entry_window_secs": config.OPENING_NEUTRAL_MARKET_WINDOW_SECS,
        }

    # ── WS subscription refresh loop ─────────────────────────────────────────

    async def _subscription_loop(self) -> None:
        """Periodically re-sync the pending-market map as new buckets are listed.

        Uses a short sleep (5 s) so that a market entering the presub window is
        registered and its timer scheduled within 5 s — well before T-10 s.
        pm.get_markets() is a local cache read so this is cheap.
        """
        while self._running:
            try:
                await self._refresh_pending_markets()
            except Exception as exc:  # pylint: disable=broad-except
                log.error("OpeningNeutralScanner: subscription refresh error", exc=str(exc))
            await asyncio.sleep(5)

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
                # Skip markets that haven't opened yet.  Window must be wide enough
                # that the timer is scheduled with time to sleep until T-10s.
                # Hardcoded -30 was too narrow: on a T-98s restart the 30s loop
                # wouldn't register the market until T-8s, leaving only 8s pre-market
                # and no room for a clean timer sleep.  Use -(ADVANCE+30) = -40s.
                presub_window = -(config.OPENING_NEUTRAL_TIMER_ADVANCE_SECS + 30)
                if elapsed_now < presub_window:
                    continue
                # Skip markets already past their entry window.
                if elapsed_now > config.OPENING_NEUTRAL_MARKET_WINDOW_SECS:
                    continue

            self._pending_markets[cond_id] = market
            # Only register the YES token in the entry-path lookup.
            # Registering both YES and NO caused double-evaluation on every WS
            # batch (one task per token).  When the YES token fires, we re-read
            # both books from the PMClient cache, so the latest NO ask is always
            # captured.  NO-token-only price movements are caught on the next
            # YES event (at most _ENTRY_EVAL_DEBOUNCE_SECS later).
            self._token_to_pending[market.token_id_yes] = cond_id

            # ── Idea 1+2+5: schedule a timer to fire at market open ───────────
            # If the market hasn't opened yet (elapsed_now < 0), schedule a task
            # that sleeps until open_ts and fires _evaluate_entry directly.
            # This removes the dependency on a WS tick arriving after open, cutting
            # ~200-500ms of first-tick latency.  Pre-qualification (static gates)
            # was already done above; only dynamic gates run at timer fire time.
            if duration > 0 and elapsed_now < 0 and cond_id not in self._scheduled_entry_market_ids:
                open_ts = market.end_date.timestamp() - duration
                self._scheduled_entry_market_ids.add(cond_id)
                task = asyncio.create_task(
                    self._scheduled_entry_task(market, open_ts),
                    name=f"on_timer_{cond_id[:20]}",
                )
                task.add_done_callback(
                    lambda t, cid=cond_id: (
                        log.error(
                            "OpeningNeutral: scheduled entry task raised",
                            market_id=cid[:22], exc=str(t.exception()),
                        ) if t.exception() else None
                    )
                )
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
            self._scheduled_entry_market_ids.discard(cid)

        # Prune markets that have drifted past the entry window without being entered.
        # Once elapsed > ENTRY_WINDOW_SECS, _evaluate_entry will always exit early on
        # the window gate — continuing to receive WS events for them wastes CPU.
        past_window_cids = [
            cid for cid, mkt in self._pending_markets.items()
            if cid not in self._entered_market_ids
            and mkt.end_date is not None
            and (lambda d=_MARKET_TYPE_DURATION_SECS.get(mkt.market_type) or 0,
                      t=mkt.end_date.timestamp() - now: d > 0 and (d - t) > config.OPENING_NEUTRAL_MARKET_WINDOW_SECS)()
        ]
        for cid in past_window_cids:
            mkt = self._pending_markets.pop(cid)
            self._token_to_pending.pop(getattr(mkt, "token_id_yes", None), None)
            self._scheduled_entry_market_ids.discard(cid)
            log.debug("OpeningNeutral: delisted past-window market", market_id=cid[:22])

        # Prune expired entries from the persistent entered-market set so that
        # if the same underlying re-lists with a new condition_id, the new bucket
        # is not confused with the old one.  Use the pending-markets expiry as
        # proxy; also prune any condition_id not seen in the current market list.
        live_cids = {getattr(m, "condition_id", "") for m in markets.values()}
        self._entered_market_ids -= (self._entered_market_ids - live_cids)
        # Promoted-slot IDs are freed when the underlying market expires/de-lists.
        self._promoted_slots -= (self._promoted_slots - live_cids)

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

    # ── Scheduled-timer entry (ideas 1, 2, 5) ────────────────────────────────

    async def _scheduled_entry_task(self, market: Any, open_ts: float) -> None:
        """
        Background task that fires _evaluate_entry at the known market-open time.

        Idea 1 — Scheduled timer: replaces the dependency on a WS tick arriving
            after open, cutting 200-500ms of first-tick latency.
        Idea 2 — Pre-qualification: static gates (market type, direction,
            entry window) were already checked in _refresh_pending_markets at
            schedule time.  Only dynamic gates (combined cost, concurrent cap)
            run at the hot moment.
        Idea 5 — TCP pre-warm: fires a lightweight GET 200ms before open to
            ensure the CLOB connection pool has an established TCP socket ready
            for the BUY orders.

        The task always discards cond_id from _scheduled_entry_market_ids on
        exit so _refresh_pending_markets can re-schedule if needed.
        """
        cond_id = getattr(market, "condition_id", "")
        try:
            now = time.time()
            prewarm_at = open_ts - config.OPENING_NEUTRAL_PREWARM_SECS
            entry_at   = open_ts - config.OPENING_NEUTRAL_TIMER_ADVANCE_SECS

            # ── Step A: pre-warm TCP connection ──────────────────────────────
            if prewarm_at > now:
                await asyncio.sleep(prewarm_at - now)
            if not self._running:
                return
            asyncio.create_task(self._prewarm_clob(), name="on_prewarm_clob")

            # ── Step B: wait until just before open ──────────────────────────
            now2 = time.time()
            if entry_at > now2:
                await asyncio.sleep(entry_at - now2)
            if not self._running:
                return

            # Bail early if already entered (WS path beat the timer).
            if cond_id in self._entered_market_ids:
                log.debug(
                    "OpeningNeutral: timer skipped — already entered (WS beat timer)",
                    market_id=cond_id[:22],
                )
                return

            # Re-fetch from pending_markets in case it was pruned while sleeping.
            live_market = self._pending_markets.get(cond_id)
            if live_market is None:
                log.debug(
                    "OpeningNeutral: timer skipped — market no longer pending",
                    market_id=cond_id[:22],
                )
                return

            log.debug(
                "OpeningNeutral: timer firing — evaluating entry",
                market=getattr(live_market, "title", "")[:60],
                market_type=getattr(live_market, "market_type", ""),
                advance_ms=round((time.time() - open_ts) * 1000, 1),
            )
            await self._evaluate_entry(live_market, _timer_fired=True)

        finally:
            self._scheduled_entry_market_ids.discard(cond_id)

    async def _prewarm_clob(self) -> None:
        """
        Idea 5 — TCP pre-warm.

        Sends a lightweight authenticated GET to the CLOB API endpoint 200ms
        before the entry fires.  This establishes a TCP connection in the
        underlying requests session pool so the BUY order POSTs start with an
        already-open socket, saving ~50-100ms of TCP handshake + TLS setup.

        Non-fatal: if it fails for any reason, the entry proceeds normally.
        """
        if getattr(self._pm, "_paper_mode", True) or getattr(self._pm, "_clob", None) is None:
            return
        try:
            await self._pm.get_live_orders()
            log.debug("OpeningNeutral: CLOB connection pre-warmed")
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("OpeningNeutral: pre-warm failed (non-fatal)", exc=str(exc))

    # ── WS price-event handler ────────────────────────────────────────────────

    async def _on_price_event(self, token_id: str, mid: float) -> None:  # noqa: ARG002
        """
        Handle a WS price-change event (fires for every book/price_change update).

        Entry path — token belongs to a pending (not-yet-entered) market:
            Evaluates all entry gates and spawns _enter_pair when qualifying.

        Exit path — token belongs to an active pair:
            Checks the current best bid.  When the bid drops to ≤
            OPENING_NEUTRAL_LOSER_EXIT_PRICE, fires a market sell via
            _execute_loser_exit — guaranteeing an exit at the best available
            price rather than leaving the position to expire worthless.

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

        # ── Exit monitoring path ──────────────────────────────────────────────
        # Fires on every WS book update for tokens in active pairs.  Active-pair
        # tokens are subscribed in _refresh_pending_markets so this path gets
        # fresh bid data on every tick without any polling.
        pair_id = self._token_to_pair.get(token_id)
        if pair_id is not None and token_id not in self._exiting_legs:
            pair = self._active_pairs.get(pair_id)
            if pair is not None:
                yes_pos: Optional[Position] = pair.get("yes_pos")
                no_pos:  Optional[Position] = pair.get("no_pos")
                if yes_pos and not yes_pos.is_closed and getattr(yes_pos, "token_id", "") == token_id:
                    mon_pos: Optional[Position] = yes_pos
                    mon_side = "YES"
                elif no_pos and not no_pos.is_closed and getattr(no_pos, "token_id", "") == token_id:
                    mon_pos = no_pos
                    mon_side = "NO"
                else:
                    mon_pos = None
                    mon_side = None
                if mon_pos is not None and mon_side is not None:
                    book = self._pm.get_book(token_id)
                    best_bid = book.best_bid if book is not None else None
                    if best_bid is not None and best_bid <= config.OPENING_NEUTRAL_LOSER_EXIT_PRICE:
                        self._exiting_legs.add(token_id)
                        asyncio.create_task(
                            self._execute_loser_exit(pair_id, mon_side, token_id, mon_pos, best_bid),
                            name=f"loser_exit_{pair_id[:12]}",
                        )

    async def _evaluate_entry(self, market: Any, _timer_fired: bool = False) -> None:
        """
        Check all entry gates for a pending market; spawn _enter_pair if all pass.

        Called from _on_price_event (WS path) or _scheduled_entry_task (timer path).
        Returns immediately if any gate fails so the event loop is not held.

        _timer_fired=True relaxes the elapsed-window lower bound by 1 second so
        that timer-fired entries (which arrive ~50ms before open) are not rejected
        by the `elapsed < 0` gate.  All other gates are unchanged.
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
        promoted = len(self._promoted_slots)
        if open_pairs + in_flight + promoted >= config.OPENING_NEUTRAL_MAX_CONCURRENT:
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
        # Entry is ONLY allowed in the pre-market window: from TIMER_ADVANCE_SECS
        # before open up to (but not including) market open.  Both the timer path
        # and WS tick path enforce this gate — WS ticks during the window
        # re-evaluate on every book change (debounced at _ENTRY_EVAL_DEBOUNCE_SECS)
        # so a brief improvement in spread is caught immediately rather than
        # relying on a single REST snapshot at T-10s.
        elapsed_min = -(config.OPENING_NEUTRAL_TIMER_ADVANCE_SECS + 1.0)
        elapsed_max = 0.0  # must still be pre-market at execution time
        if elapsed < elapsed_min or elapsed > elapsed_max:
            if _timer_fired:
                log.warning(
                    "OpeningNeutral: entry skipped — outside elapsed window",
                    market=getattr(market, "title", "")[:60],
                    elapsed=round(elapsed, 2),
                    elapsed_min=elapsed_min,
                    elapsed_max=elapsed_max,
                )
            return

        # Conflict guard: skip if another strategy has an open position here.
        open_positions = self._risk.get_open_positions()
        if any(p.market_id == market_id for p in open_positions):
            return

        # Fetch YES and NO books.  On the timer path the WS cache may be stale
        # (subscription only recently established for a pre-market bucket) so we
        # always fetch from the CLOB REST API — the authoritative source of truth.
        # On the WS path we use the cache as usual (already fresh from the event).
        if _timer_fired:
            _yes_book, _no_book = await asyncio.gather(
                self._pm.fetch_book_rest(market.token_id_yes),
                self._pm.fetch_book_rest(market.token_id_no),
            )
        else:
            _yes_book = self._pm.get_book(market.token_id_yes)
            _no_book  = self._pm.get_book(market.token_id_no)
        yes_ask = _yes_book.best_ask if _yes_book is not None else None
        no_ask  = _no_book.best_ask  if _no_book  is not None else None
        if yes_ask is None or no_ask is None:
            log.warning(
                "OpeningNeutral: entry skipped — book not ready (None ask)",
                market=getattr(market, "title", "")[:60],
                yes_ask=yes_ask,
                no_ask=no_ask,
            )
            return

        # FAK depth guard: both sides must have resting ask size ≥ entry size
        # in the book cache.  A non-None best_ask only means someone has ever
        # posted at that price — it doesn't mean that resting order still exists.
        # If the cache shows zero size at the ask, the FAK will be killed instantly.
        size_usd = config.OPENING_NEUTRAL_SIZE_USD
        yes_ask_size = _yes_book.asks[0][1] if (_yes_book and _yes_book.asks) else 0.0
        no_ask_size  = _no_book.asks[0][1]  if (_no_book  and _no_book.asks)  else 0.0
        if yes_ask_size < size_usd or no_ask_size < size_usd:
            log.warning(
                "OpeningNeutral: entry skipped — book too thin",
                market=getattr(market, "title", "")[:60],
                yes_ask=yes_ask, yes_ask_size=round(yes_ask_size, 4),
                no_ask=no_ask, no_ask_size=round(no_ask_size, 4),
                size_usd=size_usd,
            )
            return

        combined = round(yes_ask + no_ask, 6)
        diag: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "market_title": getattr(market, "title", "")[:80],
            "market_type": getattr(market, "market_type", ""),
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "yes_ask_size": round(yes_ask_size, 4),
            "no_ask_size": round(no_ask_size, 4),
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
            log.warning(
                "OpeningNeutral: entry skipped — too expensive",
                market=getattr(market, "title", "")[:60],
                combined=combined,
                threshold=config.OPENING_NEUTRAL_COMBINED_COST_MAX,
            )
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
            log.warning(
                "OpeningNeutral: entry skipped — price band",
                market=getattr(market, "title", "")[:60],
                yes_ask=yes_ask,
                no_ask=no_ask,
                band=f"[{lo}, {hi}]",
            )
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

        DRY_RUN / FAK mode:
            Both legs placed concurrently via _place_leg; results handled as
            before (both filled → register pair, one filled → one-leg fallback,
            neither → no-fill log).

        LIMIT mode (default):
            Both GTC post-only orders placed concurrently.  As soon as the
            FIRST leg fills, the resting other-leg order is cancelled and a
            market (FAK) order is placed for the other side immediately.

            This eliminates the dangerous scenario where a filled leg stops out
            while the opposite resting order remains in the book — previously
            the bot could buy into the losing side for a total loss up to −$0.65.

            Cancel race: if cancel_order returns False (other leg already filled
            before the cancel arrived), a REST check confirms the fill and the
            pair is registered as two maker fills — no market order needed.

            Any market-fill failure falls back to _handle_one_leg_fill (Momentum
            promotion) so a filled leg is never left untracked.
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

            is_fak = config.OPENING_NEUTRAL_ORDER_TYPE == "market"

            # ── DRY_RUN / FAK: concurrent _place_leg path (unchanged) ────────
            # _place_leg handles DRY_RUN simulation and FAK fill-wait internally.
            # FAK orders are fire-and-forget; we just wait for both to settle.
            if config.OPENING_NEUTRAL_DRY_RUN or is_fak:
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

                for t in pending:
                    t.cancel()

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
                return

            # ── LIMIT mode: place both GTC → wait for FIRST fill → cancel + market ──
            yes_place_price = round(min(yes_ask, 0.99), 2)
            no_place_price  = round(min(no_ask,  0.99), 2)
            yes_contracts = round(size_usd / yes_place_price, 6) if yes_place_price > 0 else 0.0
            no_contracts  = round(size_usd / no_place_price,  6) if no_place_price  > 0 else 0.0

            yes_order_id, no_order_id = await asyncio.gather(
                self._pm.place_limit(
                    token_id=yes_token_id, side="BUY",
                    price=yes_place_price, size=yes_contracts,
                    market=market, post_only=True,
                ),
                self._pm.place_limit(
                    token_id=no_token_id, side="BUY",
                    price=no_place_price, size=no_contracts,
                    market=market, post_only=True,
                ),
            )

            if not yes_order_id or not no_order_id:
                # One or both orders were rejected — clean up and abort.
                if yes_order_id:
                    await self._pm.cancel_order(yes_order_id)
                if no_order_id:
                    await self._pm.cancel_order(no_order_id)
                log.warning(
                    "OpeningNeutral: one or both limit orders rejected",
                    yes_ok=bool(yes_order_id),
                    no_ok=bool(no_order_id),
                )
                return

            # Paper mode: both orders fill instantly (no real WS events).
            if self._pm._paper_mode:
                yes_size = round(size_usd / yes_place_price, 6)
                no_size  = round(size_usd / no_place_price,  6)
                await self._register_pair(
                    pair_id, market,
                    {"filled": True, "price": yes_place_price, "size": yes_size, "order_id": yes_order_id},
                    {"filled": True, "price": no_place_price,  "size": no_size,  "order_id": no_order_id},
                    yes_token_id, no_token_id,
                )
                return

            # Register WS fill futures for both resting orders.
            loop = asyncio.get_running_loop()
            yes_future: asyncio.Future = loop.create_future()
            no_future:  asyncio.Future = loop.create_future()
            self._pm.register_fill_future(yes_order_id, yes_future)
            self._pm.register_fill_future(no_order_id,  no_future)

            # Wait for the FIRST leg to fill within the full entry window.
            done, _ = await asyncio.wait(
                {yes_future, no_future},
                timeout=config.OPENING_NEUTRAL_MARKET_WINDOW_SECS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                # Entry window expired — no fills on either leg.
                await self._pm.cancel_order(yes_order_id)
                await self._pm.cancel_order(no_order_id)
                log.info(
                    "OpeningNeutral: neither leg filled within entry window — no position taken",
                    market=market.title[:60],
                )
                _emit_event(
                    "OPENING_NEUTRAL_NO_FILL",
                    market_id=market_id,
                    market_title=market.title[:80],
                    market_type=getattr(market, "market_type", ""),
                    underlying=getattr(market, "underlying", ""),
                )
                return

            # Identify which leg filled first and set other-leg metadata.
            if yes_future in done:
                first_side  = "YES"
                first_token = yes_token_id
                first_order = yes_order_id
                first_price = yes_place_price
                other_side  = "NO"
                other_token = no_token_id
                other_order = no_order_id
                other_price = no_place_price
                first_event = yes_future.result()
            else:
                first_side  = "NO"
                first_token = no_token_id
                first_order = no_order_id
                first_price = no_place_price
                other_side  = "YES"
                other_token = yes_token_id
                other_order = yes_order_id
                other_price = yes_place_price
                first_event = no_future.result()

            # Parse the first fill — prefer WS event fields, fall back to REST.
            ws_price = float(first_event.get("price") or 0)
            ws_size  = float(first_event.get("size_matched") or 0)
            if ws_price > 0 and ws_size > 0:
                first_result: dict = {
                    "filled": True, "price": ws_price,
                    "size": ws_size, "order_id": first_order,
                }
            else:
                rest = await self._pm.get_order_fill_rest(first_order)
                if rest:
                    first_result = {
                        "filled": True,
                        "price": rest["price"],
                        "size": rest["size_matched"],
                        "order_id": first_order,
                    }
                else:
                    # WS event fired but contained no price/size — abort safely.
                    await self._pm.cancel_order(other_order)
                    log.warning(
                        "OpeningNeutral: first fill event empty (no price/size) — aborting",
                        side=first_side,
                    )
                    return

            log.info(
                "OpeningNeutral: first leg filled — cancelling other and market-filling",
                first_side=first_side,
                first_price=first_result["price"],
                other_side=other_side,
            )

            # Cancel the other resting order immediately.
            cancelled_ok = await self._pm.cancel_order(other_order)

            if not cancelled_ok:
                # cancel_order returned False — the other order may have already
                # filled (race condition: both legs became maker fills).
                rest = await self._pm.get_order_fill_rest(other_order)
                if rest and rest.get("size_matched", 0) > 0:
                    # Both filled as makers — best possible outcome.
                    other_result: dict = {
                        "filled": True,
                        "price": rest["price"],
                        "size": rest["size_matched"],
                        "order_id": other_order,
                    }
                    yes_r, no_r = (
                        (first_result, other_result) if first_side == "YES"
                        else (other_result, first_result)
                    )
                    log.info(
                        "OpeningNeutral: both legs filled as makers",
                        market=market.title[:60],
                    )
                    await self._register_pair(
                        pair_id, market, yes_r, no_r, yes_token_id, no_token_id
                    )
                    return
                # Cancel failed and other leg has no fill — one-leg fallback.
                log.warning(
                    "OpeningNeutral: cancel of other leg failed and no fill — one-leg fallback",
                    other_side=other_side,
                )
                await self._handle_one_leg_fill(
                    pair_id, market, first_result, first_side, first_token
                )
                return

            # Cancel succeeded — market-fill the other side immediately.
            mkt_price = round(min(other_price + 0.005, 0.99), 3)
            other_order_id2 = await self._pm.place_market(
                token_id=other_token, side="BUY", price=mkt_price, size=size_usd
            )
            if not other_order_id2:
                log.warning(
                    "OpeningNeutral: market fill of second leg rejected — one-leg fallback",
                    other_side=other_side,
                )
                await self._handle_one_leg_fill(
                    pair_id, market, first_result, first_side, first_token
                )
                return

            # Wait for the market fill.
            other_fill_future: asyncio.Future = loop.create_future()
            self._pm.register_fill_future(other_order_id2, other_fill_future)
            other_result_opt: Optional[dict] = None
            try:
                other_event = await asyncio.wait_for(
                    other_fill_future,
                    timeout=config.OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS,
                )
                ws_p = float(other_event.get("price") or 0)
                ws_s = float(other_event.get("size_matched") or 0)
                if ws_p > 0 and ws_s > 0:
                    other_result_opt = {
                        "filled": True, "price": ws_p,
                        "size": ws_s, "order_id": other_order_id2,
                    }
                else:
                    rest2 = await self._pm.get_order_fill_rest(other_order_id2)
                    if rest2:
                        other_result_opt = {
                            "filled": True,
                            "price": rest2["price"],
                            "size": rest2["size_matched"],
                            "order_id": other_order_id2,
                        }
            except asyncio.TimeoutError:
                rest2 = await self._pm.get_order_fill_rest(other_order_id2)
                if rest2:
                    other_result_opt = {
                        "filled": True,
                        "price": rest2["price"],
                        "size": rest2["size_matched"],
                        "order_id": other_order_id2,
                    }

            if other_result_opt is None:
                log.warning(
                    "OpeningNeutral: market fill of second leg timed out — one-leg fallback",
                    other_side=other_side,
                )
                await self._handle_one_leg_fill(
                    pair_id, market, first_result, first_side, first_token
                )
                return

            # Both legs filled — register the pair.
            yes_r, no_r = (
                (first_result, other_result_opt) if first_side == "YES"
                else (other_result_opt, first_result)
            )
            await self._register_pair(pair_id, market, yes_r, no_r, yes_token_id, no_token_id)

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

        # OrderArgs expects size in contracts (shares), not USD.
        # "limit" — post-only GTC at the current ask; rests in the book.
        # "market" — FAK taker at ask+0.5c; crosses the spread immediately.
        is_fak = config.OPENING_NEUTRAL_ORDER_TYPE == "market"

        if is_fak:
            # FAK: cross spread by +0.5c to guarantee a fill.
            place_price = round(min(ask_price + 0.005, 0.99), 3)
            contracts = round(size_usd / place_price, 6) if place_price > 0 else 0.0
            order_id = await self._pm.place_market(
                token_id=token_id, side="BUY", price=place_price, size=size_usd
            )
        else:
            # GTC post-only: rest at the current ask price (no slippage above ask).
            place_price = round(min(ask_price, 0.99), 2)
            contracts = round(size_usd / place_price, 6) if place_price > 0 else 0.0
            order_id = await self._pm.place_limit(
                token_id=token_id,
                side="BUY",
                price=place_price,
                size=contracts,
                market=market,
                post_only=True,
            )

        if not order_id:
            log.warning("OpeningNeutral: order placement rejected", side=side)
            return {"filled": False, "price": place_price, "size": 0.0, "order_id": ""}

        # Paper mode: instant fill.
        if self._pm._paper_mode:
            entry_size = round(size_usd / place_price, 6)
            return {"filled": True, "price": place_price, "size": entry_size, "order_id": order_id}

        # Wait for WS fill event.
        # FAK: fill-or-kill at the exchange — WS arrives in ~1-2s; use the short
        #      FAK_FILL_TIMEOUT.  No cancel needed if it times out (already dead).
        # GTC: resting post-only order — wait up to ENTRY_TIMEOUT_SECS.  If no
        #      fill arrives in that window, cancel and treat the leg as unfilled.
        fill_timeout = (
            config.OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS
            if is_fak
            else config.OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS
        )
        fill_future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pm.register_fill_future(order_id, fill_future)
        try:
            fill_event = await asyncio.wait_for(fill_future, timeout=fill_timeout)
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
            if is_fak:
                # FAK already dead at the exchange — no cancel needed.
                log.info("OpeningNeutral: FAK leg unfilled (killed at exchange)", side=side)
            else:
                # GTC resting order — cancel it before moving on.
                log.info("OpeningNeutral: GTC leg timed out — cancelling", side=side)
                await self._pm.cancel_order(order_id)
            # Final REST check in case the WS event was delayed.
            rest = await self._pm.get_order_fill_rest(order_id)
            if rest:
                return {
                    "filled": True,
                    "price": rest["price"],
                    "size": rest["size_matched"],
                    "order_id": order_id,
                }

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
            "yes_exit_order_id": "",
            "no_exit_order_id":  "",
        }

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

        # ── Arm bid-monitoring exit on both legs ─────────────────────────────
        # Resting GTC SELLs cannot be placed immediately after entry because the
        # current bid (~$0.44–$0.53) is above the exit threshold, causing the CLOB
        # to reject the post-only order with "crosses book".
        #
        # Instead, register both tokens in _token_to_pair so that _on_price_event
        # checks their bids on every WS tick.  When either bid drops to ≤
        # OPENING_NEUTRAL_LOSER_EXIT_PRICE, _execute_loser_exit fires a taker
        # market sell — guaranteeing an exit at whatever the best bid is rather
        # than holding the position to $0.00 at resolution.
        self._token_to_pair[yes_token_id] = pair_id
        self._token_to_pair[no_token_id]  = pair_id
        log.info(
            "OpeningNeutral: bid-monitoring armed on both legs",
            pair_id=pair_id[:12],
            exit_threshold=config.OPENING_NEUTRAL_LOSER_EXIT_PRICE,
        )

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
            # Occupy the concurrent slot until this market expires so no
            # additional markets enter on the same bucket-opening cycle.
            self._promoted_slots.add(market_id)
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
            spread_id=pair_id,
            neutral_pair_id=pair_id,
            tte_years=getattr(market, "tte_seconds", 0) / (365.25 * 86400),
            spot_price=self._spot.get_mid(getattr(market, "underlying", ""), getattr(market, "market_type", "")) or 0.0,
            strike=getattr(market, "strike", 0.0) or 0.0,
        )

    # ── Loser-exit fill monitoring ────────────────────────────────────────────

    async def _monitor_exit_fills(
        self,
        pair_id: str,
        yes_exit_id: str,
        no_exit_id: str,
    ) -> None:
        """
        Background task: wait for a WS fill on either resting loser-exit SELL.

        Both GTC SELL orders were placed immediately after entry (in _register_pair).
        Whichever fills first is the loser.  The other resting SELL is immediately
        cancelled and the winner transitions to momentum via _on_exit_fill.

        Timeout: after OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS both orders are
        cancelled.  The positions are left to the momentum / resolution handler
        (market has likely expired; winner tracking continues normally).
        """
        loop = asyncio.get_running_loop()
        yes_fut: asyncio.Future = loop.create_future()
        no_fut:  asyncio.Future = loop.create_future()
        self._pm.register_fill_future(yes_exit_id, yes_fut)
        self._pm.register_fill_future(no_exit_id,  no_fut)

        done, _ = await asyncio.wait(
            {yes_fut, no_fut},
            timeout=config.OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            # Market expired without either SELL filling.  Cancel both orders.
            await asyncio.gather(
                self._pm.cancel_order(yes_exit_id),
                self._pm.cancel_order(no_exit_id),
                return_exceptions=True,
            )
            log.warning(
                "OpeningNeutral: exit SELLs timed out — cancelling resting orders",
                pair_id=pair_id[:12],
            )
            return

        if yes_fut in done:
            filled_side = "YES"
            other_order_id = no_exit_id
            fill_event = yes_fut.result()
        else:
            filled_side = "NO"
            other_order_id = yes_exit_id
            fill_event = no_fut.result()

        ws_price = float(fill_event.get("price") or 0)
        # Guard: a GTC limit SELL at the loser threshold should never fill above
        # ~0.65.  A ws_price > 0.65 most likely means a resolution/redemption WS
        # event was mis-routed to this future (order_id collision or PM WS quirk).
        # In that case fall back to the threshold so the loser is recorded at a
        # sane price rather than $1.00, which would silently invert winner/loser.
        _max_loser_price = config.OPENING_NEUTRAL_LOSER_EXIT_PRICE + 0.30
        if ws_price > _max_loser_price:
            log.warning(
                "OpeningNeutral: loser-exit fill price anomalously high — discarding WS price",
                pair_id=pair_id[:12],
                filled_side=filled_side,
                ws_price=ws_price,
                max_expected=_max_loser_price,
            )
            ws_price = 0.0
        exit_price = ws_price if ws_price > 0 else config.OPENING_NEUTRAL_LOSER_EXIT_PRICE

        # Cancel the other resting SELL immediately.
        await self._pm.cancel_order(other_order_id)

        log.info(
            "OpeningNeutral: loser-exit SELL filled",
            pair_id=pair_id[:12],
            filled_side=filled_side,
            exit_price=exit_price,
        )

        await self._on_exit_fill(pair_id, filled_side, exit_price=exit_price)

    async def _execute_loser_exit(
        self,
        pair_id: str,
        side: str,
        token_id: str,
        pos: Position,
        trigger_bid: float,
    ) -> None:
        """
        Market-sell the loser leg when its bid drops to ≤ OPENING_NEUTRAL_LOSER_EXIT_PRICE.

        Uses place_market (taker) so the order fills immediately at whatever the
        best bid is at execution time.  Accepts slippage below $0.35 because the
        alternative — holding to expiry — yields $0.00.

        On failure, removes the token from _exiting_legs so the next WS tick retries.
        """
        log.info(
            "OpeningNeutral: loser bid crossed exit threshold — firing market sell",
            pair_id=pair_id[:12],
            side=side,
            trigger_bid=trigger_bid,
            threshold=config.OPENING_NEUTRAL_LOSER_EXIT_PRICE,
        )
        # By this point Polygon token settlement is long complete (seconds have
        # elapsed since entry).  Fetch the actual credited balance to guarantee
        # we sell exactly what the CLOB holds.
        bal = await self._pm.get_token_balance(token_id)
        sell_size = min(pos.size, bal) if (bal is not None and bal > 0) else pos.size

        order_id = await self._pm.place_market(
            token_id=token_id,
            side="SELL",
            price=0.01,   # floor at $0.01 — accepts any non-zero bid
            size=sell_size,
        )

        if order_id:
            # Confirm actual fill price via WS event (then REST fallback).
            # place_market uses price=0.01 as the floor — get_order_fill_rest's
            # path-3 fallback returns that floor price, not the real fill.
            # Replicate the monitor's pattern: register a fill future, await
            # with a 10 s timeout, fall back to REST, then to trigger_bid.
            _confirmed_price: Optional[float] = None
            if not config.PAPER_TRADING:
                _fill_future: "asyncio.Future[dict]" = (
                    asyncio.get_running_loop().create_future()
                )
                self._pm.register_fill_future(order_id, _fill_future)
                try:
                    _fill_evt = await asyncio.wait_for(_fill_future, timeout=10.0)
                    _ws_price = float(_fill_evt.get("price") or 0)
                    _ws_size  = float(_fill_evt.get("size_matched") or 0)
                    if _ws_price > 0 and _ws_size > 0:
                        _confirmed_price = _ws_price
                        log.info(
                            "OpeningNeutral: loser exit fill confirmed via WS",
                            order_id=order_id[:20],
                            trigger_bid=trigger_bid,
                            actual_fill=round(_ws_price, 4),
                        )
                    else:
                        _rest = await self._pm.get_order_fill_rest(order_id)
                        if _rest and _rest["price"] > 0.01:
                            _confirmed_price = _rest["price"]
                except asyncio.TimeoutError:
                    _rest = await self._pm.get_order_fill_rest(order_id)
                    if _rest and _rest["price"] > 0.01:
                        _confirmed_price = _rest["price"]
                        log.info(
                            "OpeningNeutral: loser exit fill confirmed via REST (WS timeout)",
                            order_id=order_id[:20],
                            trigger_bid=trigger_bid,
                            actual_fill=round(_confirmed_price, 4),
                        )
            exit_price = _confirmed_price if _confirmed_price is not None else trigger_bid
            log.info(
                "OpeningNeutral: loser market sell executed",
                pair_id=pair_id[:12],
                side=side,
                exit_price=exit_price,
                sell_size=sell_size,
                order_id=order_id,
            )
            await self._on_exit_fill(pair_id, side, exit_price=exit_price)
        else:
            # Market sell failed — unblock so the next WS tick can retry.
            self._exiting_legs.discard(token_id)
            log.error(
                "OpeningNeutral: loser market sell failed — will retry on next tick",
                pair_id=pair_id[:12],
                side=side,
                trigger_bid=trigger_bid,
            )

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

        if config.OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM:
            # ── Set strike for delta-SL ───────────────────────────────────────
            # Fetch priceToBeat from Gamma and populate the Momentum scanner's
            # open-spot cache so Momentum already has the strike when it next scans.
            _strike: float | None = None
            if self._momentum is not None:
                _strike = self._momentum._market_open_spot.get(market_id)
            if _strike is None:
                _pm_market = self._pm.get_markets().get(market_id)
                if _pm_market is not None:
                    _strike = await self._pm.fetch_price_to_beat(_pm_market.market_slug)
                if _strike and self._momentum is not None:
                    self._momentum._market_open_spot[market_id] = _strike
            if _strike:
                winner_pos.strike = _strike
                log.info(
                    "OpeningNeutral: set winner strike for delta-SL",
                    pair_id=pair_id[:12],
                    strike=_strike,
                    side=winner_pos.side,
                )
            else:
                log.warning(
                    "OpeningNeutral: could not obtain strike for promoted winner — delta-SL inactive",
                    pair_id=pair_id[:12],
                    market_id=market_id[:16],
                )

            # Promote winner to momentum and arm its prob-SL threshold.
            if config.MOMENTUM_PROB_SL_ENABLED:
                winner_pos.prob_sl_threshold = round(
                    winner_pos.entry_price * (1.0 - config.MOMENTUM_PROB_SL_PCT), 6
                )
            # promote_position_strategy updates: in-memory strategy, _token_strategy
            # file (for correct restart restore), and accounting (on_pair_promoted).
            self._risk.promote_position_strategy(market_id, winner_pos.side, "momentum")
            winner_pos.neutral_pair_id = ""  # clear so it is treated as a plain momentum pos

            log.info(
                "OpeningNeutral: winner promoted to momentum",
                pair_id=pair_id[:12],
                side=winner_pos.side,
                entry=winner_pos.entry_price,
                prob_sl_threshold=winner_pos.prob_sl_threshold,
            )

            # ── Arm take-profit price for the promoted winner ─────────────────
            # Rather than a resting SELL order (which would create orphan-position
            # issues if the TP fills before a subsequent SL is detected), we store
            # the target TP price on the position so the monitor's should_exit()
            # fires a clean taker exit when the price is reached.
            # Formula: combined_cost × (1 + TP_PROFIT_PCT) − loser_exit_price
            # capped at 0.99 (highest meaningful PM price before resolution).
            if config.OPENING_NEUTRAL_TP_ENABLED:
                _combined_cost = round(loser_pos.entry_price + winner_pos.entry_price, 6)
                _raw_tp = _combined_cost * (1.0 + config.OPENING_NEUTRAL_TP_PROFIT_PCT) - exit_price
                _tp_price = round(min(_raw_tp, 0.99), 2)
                if _tp_price >= 0.02:
                    winner_pos.take_profit_price = _tp_price
                    log.info(
                        "OpeningNeutral: winner TP price armed",
                        pair_id=pair_id[:12],
                        side=winner_pos.side,
                        combined_cost=_combined_cost,
                        loser_exit=exit_price,
                        tp_price=_tp_price,
                    )
                else:
                    log.warning(
                        "OpeningNeutral: calculated TP price too low — monitor TP inactive",
                        pair_id=pair_id[:12],
                        tp_price=round(_tp_price, 4),
                        combined_cost=_combined_cost,
                        loser_exit=exit_price,
                    )
        else:
            # No promotion: winner stays as strategy="opening_neutral".
            # The monitor's catch-all ("any other strategy label") holds it with
            # no SL / TP until the market resolves.  Clear neutral_pair_id since
            # the pair structure is dissolved (loser already closed).
            winner_pos.neutral_pair_id = ""
            log.info(
                "OpeningNeutral: winner held to resolution (PROMOTE_TO_MOMENTUM=False)",
                pair_id=pair_id[:12],
                side=winner_pos.side,
                entry=winner_pos.entry_price,
            )

        # ── Stop monitoring this pair ────────────────────────────────────────
        # Remove the pair from opening-neutral state and clean up bid-monitoring
        # lookups for both tokens.  When PROMOTE_TO_MOMENTUM=True the winner is
        # now owned by the momentum monitor; when False it is held by the risk
        # engine until RESOLVED.
        for _exit_pos in (loser_pos, winner_pos):
            _tok = getattr(_exit_pos, "token_id", "")
            if _tok:
                self._token_to_pair.pop(_tok, None)
                self._exiting_legs.discard(_tok)
        self._active_pairs.pop(pair_id, None)

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
