"""
market_data/oracle_tick_tracker.py — Per-coin CL oracle tick analytics.

Tracks Chainlink and RTDS oracle price ticks per coin and exposes:
  - EWMA up-fraction (directional momentum proxy)
  - TWAP deviation from current price (mean-reversion gauge)
  - Volatility regime classification (HIGH / LOW / UNKNOWN)

Registration example:
    tracker = OracleTickTracker()
    tracker.register(spot_oracle)

All callbacks are synchronous fire-and-forget wrappers so they integrate
with SpotOracle's async callback pattern (the on_chainlink_update signature
accepts async callbacks, but wraps them via asyncio.ensure_future internally;
we use a sync-compatible shim so no event loop dependency at init time).
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from typing import Any, Optional

from logger import get_bot_logger

log = get_bot_logger(__name__)

_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "oracle_tracker_state.json")

# EWMA smoothing factor (α) for up-fraction
_EWMA_ALPHA: float = 0.3

# Minimum ticks before get_upfrac_ewma() returns a value
_MIN_TICKS: int = 5

# Seconds of price history to include in TWAP calculation
_TWAP_WINDOW_DEFAULT: float = 10.0

# Seconds of history used for 60-second realised vol
_VOL_WINDOW_S: float = 60.0

# Rolling median window for vol-regime classification
_VOL_BUFFER_MAXLEN: int = 20


class _CoinState:
    __slots__ = (
        "price_buffer",   # deque[(ts, price), maxlen=600]
        "upfrac_ewma",    # float — EWMA of up-fraction
        "tick_count",     # int — total ticks received
        "vol_buffer",     # deque[float, maxlen=20] — rolling realized-vol readings
    )

    def __init__(self) -> None:
        self.price_buffer: deque = deque(maxlen=600)
        self.upfrac_ewma: float = 0.5   # neutral prior
        self.tick_count: int = 0
        self.vol_buffer: deque = deque(maxlen=_VOL_BUFFER_MAXLEN)


class OracleTickTracker:
    """Accumulates oracle ticks and derives directional + volatility metrics."""

    def __init__(self) -> None:
        self._coins: dict[str, _CoinState] = {}
        self._load_state()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, spot_oracle: Any) -> None:
        """Attach to a SpotOracle, subscribing to both Chainlink and RTDS feeds.

        SpotOracle.on_chainlink_update / on_rtds_update accept async callbacks.
        We supply a sync wrapper that schedules the update immediately without
        blocking the caller.
        """
        spot_oracle.on_chainlink_update(self._make_async_shim())
        spot_oracle.on_rtds_update(self._make_async_shim())

    def _make_async_shim(self):
        """Return an async callback(coin, price) that calls _on_tick synchronously."""
        async def _cb(coin: str, price: float) -> None:
            self._on_tick(coin, price)
        return _cb

    # ── Internal tick handler ─────────────────────────────────────────────────

    def _on_tick(self, coin: str, price: float) -> None:
        if price <= 0:
            return
        state = self._coins.get(coin)
        if state is None:
            state = _CoinState()
            self._coins[coin] = state

        ts = time.time()
        prev_price = state.price_buffer[-1][1] if state.price_buffer else None

        state.price_buffer.append((ts, price))
        state.tick_count += 1

        # EWMA up-fraction update
        if prev_price is not None:
            direction = 1.0 if price > prev_price else 0.0
            state.upfrac_ewma = _EWMA_ALPHA * direction + (1.0 - _EWMA_ALPHA) * state.upfrac_ewma

        # Update vol buffer every ~60 ticks (amortise cost)
        if state.tick_count % 60 == 0:
            rv = self._realised_vol_60s(state)
            if rv is not None:
                state.vol_buffer.append(rv)
                self._save_state_async(coin, state)

    # ── Read API ──────────────────────────────────────────────────────────────

    def get_upfrac_ewma(self, coin: str) -> Optional[float]:
        """EWMA up-fraction [0,1]. None if fewer than _MIN_TICKS received."""
        state = self._coins.get(coin)
        if state is None or state.tick_count < _MIN_TICKS:
            return None
        return state.upfrac_ewma

    def get_twap_deviation_bps(
        self, coin: str, window_secs: float = _TWAP_WINDOW_DEFAULT
    ) -> Optional[float]:
        """(current_price - TWAP) / TWAP * 10 000 bps.

        Returns None if fewer than 2 ticks in the window or no ticks at all.
        """
        state = self._coins.get(coin)
        if state is None or not state.price_buffer:
            return None
        now = time.time()
        cutoff = now - window_secs
        window_prices = [p for ts, p in state.price_buffer if ts >= cutoff]
        if len(window_prices) < 2:
            return None
        twap = sum(window_prices) / len(window_prices)
        if twap <= 0:
            return None
        current = state.price_buffer[-1][1]
        return (current - twap) / twap * 10_000

    def get_vol_regime(self, coin: str) -> str:
        """'HIGH' | 'LOW' | 'UNKNOWN'."""
        state = self._coins.get(coin)
        if state is None or len(state.vol_buffer) < 3:
            return "UNKNOWN"
        current_rv = self._realised_vol_60s(state)
        if current_rv is None:
            return "UNKNOWN"
        median_rv = _median(list(state.vol_buffer))
        if median_rv <= 0:
            return "UNKNOWN"
        return "HIGH" if current_rv > median_rv else "LOW"

    def reset_coin(self, coin: str) -> None:
        """Reset per-coin state (e.g. after a data gap)."""
        if coin in self._coins:
            self._coins[coin] = _CoinState()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _realised_vol_60s(self, state: _CoinState) -> Optional[float]:
        """Annualised realised vol from log-returns over the last 60 seconds."""
        now = time.time()
        cutoff = now - _VOL_WINDOW_S
        window = [(ts, p) for ts, p in state.price_buffer if ts >= cutoff]
        if len(window) < 2:
            return None
        log_returns = [
            math.log(window[i][1] / window[i - 1][1])
            for i in range(1, len(window))
            if window[i - 1][1] > 0 and window[i][1] > 0
        ]
        if len(log_returns) < 2:
            return None
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std = math.sqrt(variance)
        # Annualise: ticks per second × 3600 × 24 × 365
        elapsed = window[-1][0] - window[0][0]
        if elapsed <= 0:
            return None
        ticks_per_sec = len(log_returns) / elapsed
        return std * math.sqrt(ticks_per_sec * 86400 * 365)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_state_async(self, _coin: str, _state: _CoinState) -> None:
        """Persist vol_buffers to disk (best-effort, non-blocking)."""
        try:
            payload = {
                coin: list(s.vol_buffer)
                for coin, s in self._coins.items()
                if s.vol_buffer
            }
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            with open(_STATE_PATH, "w") as f:
                json.dump(payload, f)
        except Exception as exc:
            log.debug("oracle_tracker state save failed", exc=str(exc))

    def _load_state(self) -> None:
        """Restore vol_buffers from disk on startup (cold start)."""
        if not os.path.exists(_STATE_PATH):
            return
        try:
            with open(_STATE_PATH) as f:
                payload = json.load(f)
            for coin, vol_list in payload.items():
                if not isinstance(vol_list, list):
                    continue
                state = self._coins.setdefault(coin, _CoinState())
                for v in vol_list:
                    if isinstance(v, (int, float)):
                        state.vol_buffer.append(float(v))
        except Exception as exc:
            log.debug("oracle_tracker state load failed", exc=str(exc))


# ── Utilities ──────────────────────────────────────────────────────────────────

def _median(values: list[float]) -> float:
    """Return the median of a non-empty list."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]
