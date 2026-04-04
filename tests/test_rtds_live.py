"""
tests/test_rtds_live.py — Live end-to-end tests for RTDSClient.

Connects to the real Polymarket RTDS WebSocket and verifies that:
  1. The crypto_prices topic delivers prices for all 6 Binance coins
     (BTC, ETH, SOL, XRP, BNB, DOGE).
  2. The crypto_prices_chainlink topic delivers prices for HYPE.
  3. The RTDSClient correctly merges both topics into a single price cache.
  4. SpotPrice dataclass is populated correctly (coin, price > 0, timestamp, .mid).
  5. on_price_update callbacks fire for coins from both topics.
  6. get_mid / get_spot / get_spot_age all return sensible values.
  7. RTDSClient shuts down cleanly via stop().

Run:
    pytest tests/test_rtds_live.py -v -m live
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"]

from market_data.rtds_client import RTDSClient, SpotPrice

pytestmark = pytest.mark.live

# How long to wait for prices before declaring a test failed.
_TIMEOUT_S = 20


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Cancel any pending background tasks (e.g. RTDSClient._ws_loop) so the
        # loop closes cleanly without "Task was destroyed but it is pending!" noise.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture: a running RTDSClient that has received at least one tick
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live_rtds_prices() -> dict[str, SpotPrice]:
    """
    Start an RTDSClient, wait until prices arrive for every configured coin
    (up to _TIMEOUT_S seconds), then stop the client and return the snapshot.
    """
    async def _collect() -> dict[str, SpotPrice]:
        client = RTDSClient()
        await client.start()

        deadline = time.monotonic() + _TIMEOUT_S
        expected = set(config.TRACKED_UNDERLYINGS)

        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            have = set(client.all_mids().keys())
            if expected.issubset(have):
                break

        await client.stop()
        return client._prices.copy()

    return _run(_collect())


# ─────────────────────────────────────────────────────────────────────────────
# Basic connectivity
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSConnectivity:

    def test_receives_at_least_one_price(self, live_rtds_prices):
        """RTDSClient connects and gets at least one price update."""
        assert len(live_rtds_prices) > 0, "RTDSClient received no prices in time"


# ─────────────────────────────────────────────────────────────────────────────
# crypto_prices topic — Binance coins
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSBinanceCoins:
    """Verify all six crypto_prices coins are populated."""

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_coin_price_received(self, coin, live_rtds_prices):
        snap = live_rtds_prices.get(coin)
        assert snap is not None, f"No price received for {coin} from crypto_prices topic"

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_coin_price_positive(self, coin, live_rtds_prices):
        snap = live_rtds_prices.get(coin)
        if snap is None:
            pytest.skip(f"No price for {coin}")
        assert snap.price > 0, f"{coin}: price {snap.price} is not positive"

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_coin_mid_matches_price(self, coin, live_rtds_prices):
        snap = live_rtds_prices.get(coin)
        if snap is None:
            pytest.skip(f"No price for {coin}")
        assert snap.mid == snap.price, f"{coin}: .mid ({snap.mid}) != .price ({snap.price})"

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_coin_timestamp_recent(self, coin, live_rtds_prices):
        snap = live_rtds_prices.get(coin)
        if snap is None:
            pytest.skip(f"No price for {coin}")
        age = time.time() - snap.timestamp
        assert age < 120, f"{coin}: price timestamp is {age:.0f}s old — stale"

    def test_btc_price_plausible(self, live_rtds_prices):
        """BTC price sanity check: should be between $1k and $10M."""
        snap = live_rtds_prices.get("BTC")
        if snap is None:
            pytest.skip("No BTC price")
        assert 1_000 < snap.price < 10_000_000, f"BTC price out of plausible range: {snap.price}"

    def test_eth_price_plausible(self, live_rtds_prices):
        """ETH price sanity check: should be between $10 and $100k."""
        snap = live_rtds_prices.get("ETH")
        if snap is None:
            pytest.skip("No ETH price")
        assert 10 < snap.price < 100_000, f"ETH price out of plausible range: {snap.price}"


# ─────────────────────────────────────────────────────────────────────────────
# crypto_prices_chainlink topic — HYPE
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSChainlinkCoins:
    """Verify HYPE is populated from the crypto_prices_chainlink topic."""

    def test_hype_price_received(self, live_rtds_prices):
        snap = live_rtds_prices.get("HYPE")
        assert snap is not None, (
            "No price received for HYPE from crypto_prices_chainlink topic. "
            "Check that Chainlink subscription is working."
        )

    def test_hype_price_positive(self, live_rtds_prices):
        snap = live_rtds_prices.get("HYPE")
        if snap is None:
            pytest.skip("No HYPE price")
        assert snap.price > 0, f"HYPE price {snap.price} is not positive"

    def test_hype_mid_matches_price(self, live_rtds_prices):
        snap = live_rtds_prices.get("HYPE")
        if snap is None:
            pytest.skip("No HYPE price")
        assert snap.mid == snap.price

    def test_hype_price_plausible(self, live_rtds_prices):
        """HYPE price sanity check: should be between $0.01 and $10k."""
        snap = live_rtds_prices.get("HYPE")
        if snap is None:
            pytest.skip("No HYPE price")
        assert 0.01 < snap.price < 10_000, f"HYPE price out of plausible range: {snap.price}"

    def test_hype_coin_field(self, live_rtds_prices):
        snap = live_rtds_prices.get("HYPE")
        if snap is None:
            pytest.skip("No HYPE price")
        assert snap.coin == "HYPE", f"SpotPrice.coin field is '{snap.coin}', expected 'HYPE'"


# ─────────────────────────────────────────────────────────────────────────────
# Public API surface: get_mid, get_spot, get_spot_age, all_mids, callback
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSPublicAPI:
    """Verify the public interface returns correct types and values."""

    def test_get_mid_returns_float(self, live_rtds_prices):
        async def _inner():
            client = RTDSClient()
            # Seed prices directly so we don't need another live connection.
            client._prices = live_rtds_prices.copy()
            for coin, snap in live_rtds_prices.items():
                mid = client.get_mid(coin)
                assert isinstance(mid, float), f"{coin}: get_mid returned {type(mid)}"
                assert mid == snap.price
        _run(_inner())

    def test_get_mid_unknown_coin_returns_none(self, live_rtds_prices):
        async def _inner():
            client = RTDSClient()
            client._prices = live_rtds_prices.copy()
            assert client.get_mid("NOTACOIN") is None
        _run(_inner())

    def test_get_spot_returns_spotprice(self, live_rtds_prices):
        async def _inner():
            client = RTDSClient()
            client._prices = live_rtds_prices.copy()
            for coin in live_rtds_prices:
                snap = client.get_spot(coin)
                assert isinstance(snap, SpotPrice), f"{coin}: get_spot returned {type(snap)}"
        _run(_inner())

    def test_get_spot_age_finite_for_seen_coin(self, live_rtds_prices):
        async def _inner():
            client = RTDSClient()
            client._prices = live_rtds_prices.copy()
            for coin in live_rtds_prices:
                age = client.get_spot_age(coin)
                assert age != float("inf"), f"{coin}: get_spot_age returned inf for a seen coin"
                assert age >= 0
        _run(_inner())

    def test_get_spot_age_infinite_for_unseen_coin(self):
        async def _inner():
            client = RTDSClient()
            assert client.get_spot_age("NOTACOIN") == float("inf")
        _run(_inner())

    def test_all_mids_returns_dict(self, live_rtds_prices):
        async def _inner():
            client = RTDSClient()
            client._prices = live_rtds_prices.copy()
            mids = client.all_mids()
            assert isinstance(mids, dict)
            assert set(mids.keys()) == set(live_rtds_prices.keys())
            for coin, price in mids.items():
                assert isinstance(price, float)
                assert price > 0
        _run(_inner())


# ─────────────────────────────────────────────────────────────────────────────
# Callback fires for coins from both topics
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSCallback:

    def test_callback_fires_for_binance_and_chainlink(self):
        """on_price_update callback receives coins from both RTDS topics."""
        received: dict[str, float] = {}

        async def _collect() -> None:
            client = RTDSClient()

            async def _cb(coin: str, price: float) -> None:
                received[coin] = price

            client.on_price_update(_cb)
            await client.start()

            deadline = time.monotonic() + _TIMEOUT_S
            # Wait until we have at least one Binance coin AND HYPE (Chainlink)
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                has_binance = any(c in received for c in ["BTC", "ETH", "SOL"])
                has_hype = "HYPE" in received
                if has_binance and has_hype:
                    break

            await client.stop()

        _run(_collect())

        binance_coins = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}
        received_binance = binance_coins & set(received.keys())
        assert received_binance, (
            "No Binance-topic coins received via callback — "
            f"crypto_prices subscription may be broken. Received: {list(received.keys())}"
        )
        assert "HYPE" in received, (
            "HYPE not received via callback — "
            "crypto_prices_chainlink subscription may be broken. "
            f"Received coins: {list(received.keys())}"
        )
        # All prices should be positive
        for coin, price in received.items():
            assert price > 0, f"Callback received non-positive price for {coin}: {price}"
