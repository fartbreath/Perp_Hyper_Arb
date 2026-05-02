"""
market_data/funding_rate_cache.py — WS-push-fed HL funding rate cache.

Receives funding rate updates from HLClient's webData2 handler via on_ws_update().
No polling. Staleness is measured from last push timestamp, not a TTL timer.
History buffer (last 10 readings per coin) is for future trend analysis only —
NOT validated as a gate signal. Do not add trend-based logic here.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional

import config


class FundingRateCache:
    """Push-fed funding rate cache. Updated by HLClient on every webData2 message."""

    def __init__(self, stale_threshold_s: float | None = None) -> None:
        self._stale_s: float = (
            stale_threshold_s
            if stale_threshold_s is not None
            else float(getattr(config, "FUNDING_STALE_THRESHOLD_S", 120))
        )
        self._rates: dict[str, float] = {}
        self._timestamps: dict[str, float] = {}
        self._history: dict[str, deque] = {}  # coin → deque[(ts, rate), maxlen=10]

    # ── Write path ─────────────────────────────────────────────────────────────

    def on_ws_update(self, coin: str, funding_rate: float, ts: float) -> None:
        """Called by HLClient on every webData2 push. Single-threaded asyncio — no locks."""
        self._rates[coin] = funding_rate
        self._timestamps[coin] = ts
        if coin not in self._history:
            self._history[coin] = deque(maxlen=10)
        self._history[coin].append((ts, funding_rate))

    # ── Read path ──────────────────────────────────────────────────────────────

    def get(self, coin: str) -> Optional[float]:
        """Current rate; None if stale or never received."""
        if self.is_stale(coin):
            return None
        return self._rates.get(coin)

    def get_direction(self, coin: str) -> Optional[str]:
        """'POSITIVE' | 'NEGATIVE' | 'NEUTRAL' | None (if stale)."""
        rate = self.get(coin)
        if rate is None:
            return None
        if rate > 0:
            return "POSITIVE"
        if rate < 0:
            return "NEGATIVE"
        return "NEUTRAL"

    def get_history(self, coin: str) -> list[tuple[float, float]]:
        """Last ≤10 (ts, rate) tuples for this coin, oldest first."""
        return list(self._history.get(coin, []))

    def is_stale(self, coin: str) -> bool:
        """True if no push received within stale_threshold_s, or never received."""
        ts = self._timestamps.get(coin)
        if ts is None:
            return True
        return (time.time() - ts) > self._stale_s

    # ── Health helpers ─────────────────────────────────────────────────────────

    def fresh_count(self, coins: list[str]) -> int:
        """Number of coins in *coins* with non-stale data."""
        return sum(1 for c in coins if not self.is_stale(c))

    def last_update_ts(self) -> Optional[float]:
        """Most recent push timestamp across all coins. None if no data received yet."""
        if not self._timestamps:
            return None
        return max(self._timestamps.values())
