"""
market_data.deribit — Deribit options data fetcher.

Pure market-data retrieval: fetches instruments and mark IV from the public
Deribit API.  No strategy logic, no signal types, no PM/HL references.

Extracted from mispricing.py so any future strategy can consume Deribit data
without pulling in the full mispricing strategy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import aiohttp

from logger import get_bot_logger

log = get_bot_logger(__name__)

DERIBIT_API = "https://www.deribit.com/api/v2"


class DeribitFetcher:
    """Fetches IV from Deribit for a given underlying and target strike/expiry."""

    async def get_iv_for_target(
        self,
        underlying: str,    # "BTC" or "ETH"
        strike: float,
        target_date: datetime,
    ) -> tuple[float, str]:
        """
        Find the nearest Deribit call option to (strike, target_date) and return
        its mark_iv and instrument name.

        Returns: (iv, instrument_name) or (0.0, "") on failure.
        """
        currency = underlying.upper()
        try:
            instruments = await self._fetch_instruments(currency)
            if not instruments:
                return 0.0, ""

            best = self._find_nearest(instruments, strike, target_date)
            if not best:
                return 0.0, ""

            iv = await self._fetch_mark_iv(best["instrument_name"])
            return iv, best["instrument_name"]
        except Exception as exc:
            log.error("Deribit fetch failed", exc=str(exc), underlying=underlying)
            return 0.0, ""

    async def _fetch_instruments(self, currency: str) -> list[dict]:
        url = f"{DERIBIT_API}/public/get_instruments"
        params = {"currency": currency, "kind": "option", "expired": "false"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    return data.get("result", [])
        except Exception as exc:
            log.error("Deribit instruments fetch failed", exc=str(exc))
            return []

    def _find_nearest(
        self,
        instruments: list[dict],
        target_strike: float,
        target_date: datetime,
    ) -> Optional[dict]:
        """Find the call option with closest (expiry, strike) to the target."""
        calls = [i for i in instruments if i.get("option_type") == "call"]
        if not calls:
            return None

        target_ts = target_date.timestamp()

        def score(inst: dict) -> float:
            expiry_ts = inst.get("expiration_timestamp", 0) / 1000
            strike = inst.get("strike", 0)
            # Normalise by typical scales: 30 days in seconds, 10% strike diff
            time_diff = abs(expiry_ts - target_ts) / (30 * 86400)
            strike_diff = abs(strike - target_strike) / target_strike if target_strike > 0 else 1.0
            return time_diff + strike_diff

        return min(calls, key=score)

    async def _fetch_mark_iv(self, instrument_name: str) -> float:
        url = f"{DERIBIT_API}/public/get_order_book"
        params = {"instrument_name": instrument_name, "depth": 1}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    result = data.get("result", {})
                    iv = result.get("mark_iv", 0.0)
                    return float(iv) / 100.0  # Deribit returns IV as percentage
        except Exception as exc:
            log.error("Deribit mark_iv fetch failed", exc=str(exc), instrument=instrument_name)
            return 0.0
