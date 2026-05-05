"""
models/clob_feature_buffer.py — ML-01: Event-driven CLOB feature buffer.

Subscribes to PMClient's on_price_change callback and maintains a per-token
rolling deque of (timestamp, best_bid, total_bid_size) tuples.  No raw events
are written to disk; all state lives in memory.  Feature computation happens
on demand at decision time (exit-check hot path).

Controlled by config.CLOB_FEATURE_BUFFER_ENABLED (default False).  When
disabled, all callbacks are a no-op and compute_features() returns an all-null
dict.  Any exception inside event processing is caught, logged at WARNING, and
never propagated to the PMClient WebSocket handler.

Usage in main.py (after pm.start()):
    from models.clob_feature_buffer import CLOBFeatureBuffer
    clob_buffer = CLOBFeatureBuffer()
    clob_buffer.register(pm)   # registers on_price_change callback

Usage at exit-decision time:
    features = clob_buffer.compute_features(market_id)
    # features["yes_bid_slope_30s"], features["no_bid_slope_30s"], ...
"""
from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Dict, Optional

import config
from logger import get_bot_logger

if TYPE_CHECKING:
    from pm_client import PMClient

log = get_bot_logger(__name__)

# Type alias for a single buffered CLOB tick: (timestamp, best_bid, total_bid_size)
_Tick = tuple[float, float, float]

# Sentinel dict returned when buffer is disabled or data is insufficient.
_NULL_FEATURES: Dict[str, Optional[float]] = {
    "yes_bid_slope_30s": None,
    "no_bid_slope_30s": None,
    "yes_depth_delta_60s": None,
    "no_depth_delta_60s": None,
    "yes_bid_at_level": None,
    "no_bid_at_level": None,
    "yes_bid_vs_premarket_baseline": None,
    "no_bid_vs_premarket_baseline": None,
}


class CLOBFeatureBuffer:
    """In-memory rolling CLOB depth buffer.

    One instance lives for the lifetime of the bot.  Call register(pm_client)
    once after pm.start() to wire the callbacks.  All public methods are safe
    to call whether or not the buffer is enabled — they return None-filled dicts
    when disabled or when there is insufficient data.
    """

    def __init__(self) -> None:
        # token_id → deque of _Tick
        self._buffers: Dict[str, deque[_Tick]] = {}
        # condition_id → {"yes": (ts, best_bid, total_depth), "no": ...}
        self._premarket_baseline: Dict[str, Dict[str, Optional[_Tick]]] = {}
        self._pm_client: Optional["PMClient"] = None
        self._enabled: bool = config.CLOB_FEATURE_BUFFER_ENABLED
        self._maxlen: int = config.CLOB_BUFFER_MAXLEN

    # ── Registration ───────────────────────────────────────────────────────

    def register(self, pm_client: "PMClient") -> None:
        """Wire this buffer into *pm_client*.

        Must be called after pm_client.start() so that the WS shards are
        already connected and _markets is populated.  Safe to call when
        CLOB_FEATURE_BUFFER_ENABLED=False — does nothing in that case.
        """
        if not self._enabled:
            log.info("CLOBFeatureBuffer disabled — skipping registration")
            return
        self._pm_client = pm_client
        pm_client.on_price_change(self._on_price_update)
        log.info("CLOBFeatureBuffer registered on PMClient", maxlen=self._maxlen)

    # ── Internal callback ──────────────────────────────────────────────────

    async def _on_price_update(self, token_id: str, mid: float) -> None:
        """Called by PMClient._fire_price_change on every book update.

        Runs inside an asyncio.Task (create_task in PMClient).  All exceptions
        are caught here so they never propagate back to the PMClient handler.
        """
        if not self._enabled or self._pm_client is None:
            return
        try:
            snap = self._pm_client.get_book(token_id)
            if snap is None:
                return
            best_bid = snap.best_bid
            if best_bid is None:
                return
            total_bid_size = sum(s for _, s in snap.bids)
            tick: _Tick = (time.time(), best_bid, total_bid_size)
            buf = self._buffers.get(token_id)
            if buf is None:
                buf = deque(maxlen=self._maxlen)
                self._buffers[token_id] = buf
            buf.append(tick)
        except Exception as exc:
            log.warning(
                "CLOBFeatureBuffer event processing error",
                token_id=token_id[:12],
                exc=str(exc),
            )

    # ── Public API ─────────────────────────────────────────────────────────

    def set_premarket_baseline(self, market_id: str) -> None:
        """Record the current CLOB state as the pre-market open baseline.

        Call this when a market transitions from pre-market to open (e.g. in
        the OpeningNeutral scanner's _prewarm_clob step or just before the
        first entry attempt).  If the buffer is disabled or books are absent,
        the baseline is stored as None and bid_vs_premarket_baseline returns
        None at compute time.
        """
        if not self._enabled or self._pm_client is None:
            return
        try:
            market = self._pm_client.get_markets().get(market_id)
            if market is None:
                return
            yes_tick = self._latest_tick(market.token_id_yes)
            no_tick = self._latest_tick(market.token_id_no)
            self._premarket_baseline[market_id] = {
                "yes": yes_tick,
                "no": no_tick,
                "timestamp": time.time(),
            }
            log.debug(
                "CLOBFeatureBuffer premarket baseline recorded",
                market_id=market_id[:16],
                yes_bid=yes_tick[1] if yes_tick else None,
                no_bid=no_tick[1] if no_tick else None,
            )
        except Exception as exc:
            log.warning(
                "CLOBFeatureBuffer set_premarket_baseline error",
                market_id=market_id[:16],
                exc=str(exc),
            )

    def clear(self, market_id: str) -> None:
        """Remove buffered data for both token sides of *market_id*.

        Call this after a trade closes so stale ticks do not pollute the next
        trade in the same market.
        """
        if not self._enabled or self._pm_client is None:
            return
        try:
            market = self._pm_client.get_markets().get(market_id)
            if market is None:
                return
            self._buffers.pop(market.token_id_yes, None)
            self._buffers.pop(market.token_id_no, None)
            self._premarket_baseline.pop(market_id, None)
        except Exception as exc:
            log.warning(
                "CLOBFeatureBuffer clear error",
                market_id=market_id[:16],
                exc=str(exc),
            )

    def depth(self, market_id: str) -> int:
        """Number of buffered ticks for *market_id* (max of YES / NO counts).

        Returns 0 when disabled or when no ticks have been received yet.
        """
        if not self._enabled or self._pm_client is None:
            return 0
        try:
            market = self._pm_client.get_markets().get(market_id)
            if market is None:
                return 0
            yes_n = len(self._buffers.get(market.token_id_yes, []))
            no_n = len(self._buffers.get(market.token_id_no, []))
            return max(yes_n, no_n)
        except Exception:
            return 0

    def compute_features(self, market_id: str) -> Dict[str, Optional[float]]:
        """Compute CLOB depth features for *market_id* on demand.

        Returns an 8-key dict with YES and NO variants of each feature.
        All values are None when:
          • the buffer is disabled
          • fewer than 2 ticks are available for a side
          • an exception occurs during computation

        Feature definitions:
          bid_slope_30s        Δbid / Δtime ($/s) over the last 30s of ticks.
                               Negative → bid is collapsing; strongly negative
                               + simultaneous on both legs = settlement drain.
          depth_delta_60s      Change in total bid-side depth ($) over 60s.
                               Negative → liquidity being withdrawn.
          bid_at_level         Current best bid (most recent tick).
          bid_vs_premarket_baseline  current_bid / baseline_bid at market open.
                               < 1 → bid has eroded since open.
        """
        if not self._enabled:
            return dict(_NULL_FEATURES)
        try:
            if self._pm_client is None:
                return dict(_NULL_FEATURES)
            market = self._pm_client.get_markets().get(market_id)
            if market is None:
                return dict(_NULL_FEATURES)
            yes_buf = self._buffers.get(market.token_id_yes)
            no_buf = self._buffers.get(market.token_id_no)
            baseline = self._premarket_baseline.get(market_id, {})
            yes_baseline_tick: Optional[_Tick] = baseline.get("yes")
            no_baseline_tick: Optional[_Tick] = baseline.get("no")

            return {
                "yes_bid_slope_30s": self._bid_slope(yes_buf, window_secs=30.0),
                "no_bid_slope_30s": self._bid_slope(no_buf, window_secs=30.0),
                "yes_depth_delta_60s": self._depth_delta(yes_buf, window_secs=60.0),
                "no_depth_delta_60s": self._depth_delta(no_buf, window_secs=60.0),
                "yes_bid_at_level": self._latest_bid(yes_buf),
                "no_bid_at_level": self._latest_bid(no_buf),
                "yes_bid_vs_premarket_baseline": self._bid_vs_baseline(
                    yes_buf, yes_baseline_tick
                ),
                "no_bid_vs_premarket_baseline": self._bid_vs_baseline(
                    no_buf, no_baseline_tick
                ),
            }
        except Exception as exc:
            log.warning(
                "CLOBFeatureBuffer compute_features error",
                market_id=market_id[:16],
                exc=str(exc),
            )
            return dict(_NULL_FEATURES)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _latest_tick(self, token_id: str) -> Optional[_Tick]:
        """Return the most recent tick for *token_id*, or None."""
        buf = self._buffers.get(token_id)
        if not buf:
            return None
        return buf[-1]

    def _latest_bid(self, buf: Optional[deque]) -> Optional[float]:
        if not buf:
            return None
        return buf[-1][1]

    def _bid_slope(
        self, buf: Optional[deque], window_secs: float = 30.0
    ) -> Optional[float]:
        """Δbest_bid / Δtime ($/s) over the last *window_secs* of ticks.

        Uses the oldest tick within the window and the most recent tick so
        the slope reflects the full measured interval, not just adjacent ticks.
        Returns None when fewer than 2 ticks fall within the window.
        """
        if not buf:
            return None
        now = time.time()
        cutoff = now - window_secs
        # Collect ticks within the window (buf is ordered oldest→newest)
        window_ticks = [(ts, bid) for ts, bid, _ in buf if ts >= cutoff]
        if len(window_ticks) < 2:
            return None
        ts_first, bid_first = window_ticks[0]
        ts_last, bid_last = window_ticks[-1]
        dt = ts_last - ts_first
        if dt < 0.1:  # < 100ms — not enough elapsed time to compute a slope
            return None
        return (bid_last - bid_first) / dt

    def _depth_delta(
        self, buf: Optional[deque], window_secs: float = 60.0
    ) -> Optional[float]:
        """Change in total bid-side depth ($) over the last *window_secs*.

        Finds the tick closest to *window_secs* ago and computes:
            current_depth - depth_at_that_time

        Returns None when there is no tick older than the window start.
        """
        if not buf:
            return None
        now = time.time()
        cutoff = now - window_secs
        # Find the most recent tick that is older than the cutoff
        past_tick: Optional[_Tick] = None
        for tick in buf:
            if tick[0] <= cutoff:
                past_tick = tick
            else:
                break  # deque is ordered oldest→newest; stop once we pass the cutoff
        if past_tick is None:
            return None
        current_depth = buf[-1][2]
        return current_depth - past_tick[2]

    def _bid_vs_baseline(
        self, buf: Optional[deque], baseline_tick: Optional[_Tick]
    ) -> Optional[float]:
        """current_best_bid / baseline_best_bid.

        Returns None when either the current book or the baseline is absent,
        or when the baseline bid is zero (prevents division-by-zero).
        """
        if not buf or baseline_tick is None:
            return None
        current_bid = buf[-1][1]
        baseline_bid = baseline_tick[1]
        if baseline_bid == 0.0:
            return None
        return current_bid / baseline_bid
