"""
strategies.Momentum.vol_fetcher — Dynamic volatility for momentum thresholds.

Source priority:
  1. Deribit ATM IV   — nearest ~7-day call option, ATM strike.
                        Supported coins: BTC, ETH, SOL (all have liquid Deribit options).
                        Cached for MOMENTUM_VOL_CACHE_TTL seconds (default 5 min).
  2. HL rolling realized vol — log-return std from a rolling buffer of HL BBO updates.
                              Used for HYPE, DOGE and any coin where Deribit fetch fails.
                              Buffer: up to 2,000 samples per coin, pruned to 24 h.
  3. None             — signal is skipped by the scanner.

Usage:
    fetcher = VolFetcher(hl_client)
    vol_fetcher.register(hl_client)         # start receiving BBO updates
    sigma = await vol_fetcher.get_sigma_ann("BTC")
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import config
from logger import get_bot_logger
from market_data.deribit import DeribitFetcher
from market_data.pyth_client import PythClient

log = get_bot_logger(__name__)

# Coins for which Deribit has liquid options (primary vol source).
# XRP has Deribit options and SOL is well-covered; BTC/ETH are always available.
_DERIBIT_SUPPORTED = frozenset({"BTC", "ETH", "SOL", "XRP"})

# Rolling-vol buffer: max samples per coin and max age in seconds.
_MAX_SAMPLES = 2000
_MAX_AGE_SECS = 86_400          # 24 hours
_MIN_SAMPLES_FOR_VOL = 10       # require at least 10 log-returns


class VolFetcher:
    """
    Provides annualized volatility estimates for momentum threshold computation.

    Must call `register(pyth_client)` before the first `get_sigma_ann()` call so
    that the rolling realized-vol buffer is populated by live Pyth oracle ticks.
    Rolling vol uses Pyth prices (same oracle Polymarket resolves against) rather
    than HL perp to avoid funding-rate basis distorting the vol estimate.
    """

    def __init__(self) -> None:
        self._deribit = DeribitFetcher()

        # Deribit IV cache: coin → (sigma_ann, expires_at, source_tag)
        self._cache: dict[str, tuple[float, float, str]] = {}

        # HL rolling mid buffer: coin → deque[(timestamp, mid)]
        self._mid_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_MAX_SAMPLES)
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def register(self, pyth: PythClient) -> None:
        """
        Register the Pyth price callback so `_mid_history` stays populated.
        Call once during bot startup, before the first scan.
        """
        pyth.on_price_update(self._on_pyth)
        log.info("VolFetcher registered with Pyth oracle feed")

    def start_prefetch(self, underlyings: list[str]) -> "asyncio.Task[None]":
        """Start a background task that keeps the vol cache warm for all
        tracked underlyings.  Call once during bot startup so the first
        scan pass never has to wait for a live Deribit round-trip.

        Returns the Task so the caller can cancel it on shutdown.
        """
        task = asyncio.create_task(self._vol_prefetch_loop(underlyings))
        log.info("VolFetcher prefetch loop started", underlyings=underlyings)
        return task

    async def _vol_prefetch_loop(self, underlyings: list[str]) -> None:
        """Background loop: eagerly refresh vol cache ahead of TTL expiry.

        Deribit TTL is MOMENTUM_VOL_CACHE_TTL (default 300 s); HL realized is
        60 s.  We refresh every 240 s to keep Deribit warm without hammering
        the API, and let HL realized self-refresh on each inner iteration.
        """
        while True:
            for underlying in underlyings:
                try:
                    await self.get_sigma_ann(underlying)
                except Exception as exc:
                    log.warning(
                        "VolFetcher: prefetch error",
                        underlying=underlying,
                        exc=str(exc),
                    )
            await asyncio.sleep(240)

    async def get_sigma_ann(self, underlying: str) -> Optional[tuple[float, str]]:
        """
        Return (annualised_volatility, source_tag) for `underlying`.

        source_tag is "deribit_atm" when Deribit IV was used, or "hl_realized"
        when the HL rolling realized fallback was used.

        Returns None if no data is available (signal should be skipped).
        """
        now = time.time()

        # Try cached value first (avoids Deribit round-trip on every scan pass)
        cached = self._cache.get(underlying)
        if cached is not None and cached[1] > now:
            return cached[0], cached[2]

        # Primary: Deribit ATM IV
        if underlying in _DERIBIT_SUPPORTED:
            iv = await self._fetch_deribit_atm_iv(underlying)
            if iv is not None and iv > 0:
                expires = now + config.MOMENTUM_VOL_CACHE_TTL
                self._cache[underlying] = (iv, expires, "deribit_atm")
                log.debug(
                    "VolFetcher: Deribit IV fetched",
                    underlying=underlying,
                    sigma_ann=round(iv, 4),
                )
                return iv, "deribit_atm"

        # Fallback: Pyth rolling realized vol
        rv = self._compute_rolling_vol(underlying)
        if rv is not None:
            # Shorter TTL for realized vol — less stable than options IV
            self._cache[underlying] = (rv, now + 60.0, "pyth_realized")
            log.debug(
                "VolFetcher: Pyth rolling vol",
                underlying=underlying,
                sigma_ann=round(rv, 4),
            )
            return rv, "pyth_realized"

        log.debug(
            "VolFetcher: no vol available",
            underlying=underlying,
            has_history=len(self._mid_history.get(underlying, [])),
        )
        return None

    # ── Pyth price callback ───────────────────────────────────────────────────

    async def _on_pyth(self, coin: str, price: float) -> None:
        """Append (timestamp, price) to the rolling buffer for `coin`."""
        now_ts = time.time()
        buf = self._mid_history[coin]
        buf.append((now_ts, price))

        # Prune entries older than _MAX_AGE_SECS
        cutoff = now_ts - _MAX_AGE_SECS
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    # ── Deribit ATM IV ────────────────────────────────────────────────────────

    async def _fetch_deribit_atm_iv(self, underlying: str) -> Optional[float]:
        """
        Fetch the ATM mark IV from Deribit for the nearest ~7-day call option.

        Uses the existing DeribitFetcher, passing current spot as the ATM strike
        and a target_date 7 days out (to select the nearest weekly expiry).
        """
        from hl_client import HLClient as _HLC  # avoid circular at module level

        # We need current spot — pull from the mid_history buffer if available
        history = self._mid_history.get(underlying)
        if history:
            spot = history[-1][1]
        else:
            log.debug(
                "VolFetcher: no Pyth price history for Deribit lookup",
                underlying=underlying,
            )
            return None

        target_date = datetime.now(timezone.utc) + timedelta(days=7)
        try:
            iv, instrument = await self._deribit.get_iv_for_target(
                underlying=underlying,
                strike=spot,
                target_date=target_date,
            )
            if iv > 0:
                log.debug(
                    "VolFetcher: Deribit ATM IV",
                    underlying=underlying,
                    instrument=instrument,
                    iv_pct=round(iv * 100, 1),
                    atm_strike=round(spot, 2),
                )
                return iv
        except Exception as exc:
            log.warning(
                "VolFetcher: Deribit fetch error",
                underlying=underlying,
                exc=str(exc),
            )
        return None

    # ── HL rolling realized vol ───────────────────────────────────────────────

    def _compute_rolling_vol(self, underlying: str) -> Optional[float]:
        """
        Compute annualized realized vol from the mid-price buffer.

        Uses log-return standard deviation, annualized via average sample interval.
        Requires at least _MIN_SAMPLES_FOR_VOL data points.
        """
        history = list(self._mid_history.get(underlying, []))
        if len(history) < _MIN_SAMPLES_FOR_VOL + 1:
            return None

        timestamps = [t for (t, _) in history]
        mids = [m for (_, m) in history]

        # Log returns
        log_returns = [
            math.log(mids[i] / mids[i - 1])
            for i in range(1, len(mids))
            if mids[i - 1] > 0 and mids[i] > 0
        ]
        if len(log_returns) < _MIN_SAMPLES_FOR_VOL:
            return None

        try:
            std_ret = statistics.stdev(log_returns)
        except statistics.StatisticsError:
            return None

        # Average interval between samples (seconds)
        if len(timestamps) < 2:
            return None
        avg_interval_s = (timestamps[-1] - timestamps[0]) / (len(timestamps) - 1)
        if avg_interval_s <= 0:
            return None

        # Annualize: sigma_ann = sigma_per_period * sqrt(periods_per_year)
        sigma_ann = std_ret * math.sqrt(31_536_000 / avg_interval_s)
        # Sanity-clamp: realistic vol is between 1% and 1000% annualized
        sigma_ann = max(0.01, min(10.0, sigma_ann))
        return sigma_ann
