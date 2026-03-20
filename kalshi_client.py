"""
kalshi_client.py — Read-only Kalshi market data client (no auth required).

Used as a signal confirmation layer in the mispricing strategy. When a PM
milestone market has a matching Kalshi counterpart, |PM_price - Kalshi_price|
is used as the primary deviation signal instead of |PM_price - N(d₂)|.

This eliminates the barrier/terminal probability mismatch: Kalshi crypto markets
also resolve on "close above X at expiry" (terminal), giving an apples-to-apples
comparison against PM's barrier-resolved milestone markets at the price level.

API: https://trading-api.kalshi.com/trade-api/v2 — no auth needed for reads.
Markets are cached for CACHE_TTL seconds to avoid hammering the public endpoint.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

KALSHI_API = "https://trading-api.kalshi.com/trade-api/v2"
CACHE_TTL = 300  # 5 minutes — refresh market list on first use then every 5 min

_TRACKED_UNDERLYINGS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX",
    "LINK", "DOT", "TON", "SUI", "APT", "NEAR", "OP", "ARB",
}


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    close_time: datetime
    underlying: str          # "BTC" | "ETH" | etc.
    strike: Optional[float]  # parsed from title; None if unparseable

    @property
    def yes_mid(self) -> float:
        """Best available mid price; falls back to whichever side is non-zero."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_ask if self.yes_ask > 0 else self.yes_bid


class KalshiClient:
    """
    Lightweight async client for Kalshi's public market data.

    Thread-safety: uses an asyncio.Lock so concurrent refresh_markets() calls
    are serialised (only one in-flight request at a time).
    """

    def __init__(self) -> None:
        self._markets: list[KalshiMarket] = []
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def refresh_markets(self) -> int:
        """
        Fetch all open crypto markets from Kalshi and update the cache.
        Returns the number of markets cached.
        Thread-safe: concurrent calls await the lock instead of double-fetching.
        """
        async with self._lock:
            return await self._do_refresh()

    async def maybe_refresh(self) -> None:
        """Refresh only if the cache is older than CACHE_TTL seconds."""
        if time.time() - self._last_refresh > CACHE_TTL:
            await self.refresh_markets()

    async def _do_refresh(self) -> int:
        url = f"{KALSHI_API}/markets"
        params = {"limit": 1000, "status": "open"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning(
                            "Kalshi refresh failed: non-200 response",
                            status=resp.status,
                        )
                        return 0
                    data = await resp.json()
        except Exception as exc:
            log.error("Kalshi market refresh failed", exc=str(exc))
            return 0

        markets: list[KalshiMarket] = []
        for m in data.get("markets", []):
            close_str = m.get("close_time") or m.get("expiration_time")
            if not close_str:
                continue
            try:
                close_time = datetime.fromisoformat(
                    close_str.replace("Z", "+00:00")
                )
            except ValueError:
                continue

            yes_bid = float(m.get("yes_bid") or 0)
            yes_ask = float(m.get("yes_ask") or 0)
            if yes_bid <= 0 and yes_ask <= 0:
                continue  # no live price

            title = m.get("title", "")
            ticker = m.get("ticker", "")
            underlying = _infer_underlying(title, ticker)
            if not underlying:
                continue  # not a recognised crypto market

            strike = _extract_strike(title)
            markets.append(
                KalshiMarket(
                    ticker=ticker,
                    title=title,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    close_time=close_time,
                    underlying=underlying,
                    strike=strike,
                )
            )

        self._markets = markets
        self._last_refresh = time.time()
        log.info(
            "Kalshi market cache refreshed",
            total=len(markets),
        )
        return len(markets)

    async def get_price(
        self,
        underlying: str,
        strike: float,
        resolution_date: datetime,
    ) -> tuple[float | None, str | None]:
        """
        Find the best-matching Kalshi market for (underlying, strike, resolution_date)
        and return (yes_mid_price, ticker).

        Match tolerances are controlled by:
          KALSHI_MATCH_MAX_STRIKE_DIFF  — max fractional strike difference
          KALSHI_MATCH_MAX_EXPIRY_DAYS  — max calendar-day expiry difference

        Returns (None, None) if no suitable match is found.
        """
        await self.maybe_refresh()

        candidates = [
            m for m in self._markets
            if m.underlying == underlying.upper() and m.strike is not None
        ]
        if not candidates:
            return None, None

        target_ts = resolution_date.timestamp()
        best: Optional[KalshiMarket] = None
        best_score = math.inf

        for m in candidates:
            strike_diff = abs(m.strike - strike) / max(strike, 1.0)
            if strike_diff > config.KALSHI_MATCH_MAX_STRIKE_DIFF:
                continue

            expiry_diff_days = abs(m.close_time.timestamp() - target_ts) / 86400.0
            if expiry_diff_days > config.KALSHI_MATCH_MAX_EXPIRY_DAYS:
                continue

            # Composite score: equal weight on fractional strike diff and
            # normalised expiry diff (both range 0–1 within tolerances).
            score = strike_diff + expiry_diff_days / max(
                config.KALSHI_MATCH_MAX_EXPIRY_DAYS, 1e-9
            )
            if score < best_score:
                best_score = score
                best = m

        if best is None or best.yes_mid <= 0:
            return None, None

        return best.yes_mid, best.ticker


# ── Module-level helpers ──────────────────────────────────────────────────────

def _infer_underlying(title: str, ticker: str) -> str | None:
    """Return the uppercase asset symbol if found in the title or ticker."""
    combined = f"{title} {ticker}".upper()
    for asset in _TRACKED_UNDERLYINGS:
        if asset in combined:
            return asset
    return None


def _extract_strike(title: str) -> float | None:
    """
    Parse a USD strike price from a Kalshi market title.
    Handles: $90,000  $120k  $1.2M  $90000
    """
    match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)([kKmM]?)", title.replace(",", ""))
    if not match:
        return None
    try:
        value = float(match.group(1))
        suffix = match.group(2).lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        return value if value > 0 else None
    except ValueError:
        return None
