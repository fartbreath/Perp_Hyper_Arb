"""
tests/test_rtds_live.py — Live end-to-end tests for RTDSClient.

Connects to the real Polymarket RTDS WebSocket and verifies that:
  1. The crypto_prices topic delivers prices for all RTDS coins
     (BTC, ETH, SOL, XRP, BNB, DOGE).
  2. SpotPrice dataclass is populated correctly (coin, price > 0, timestamp, .mid).
  3. on_price_update callbacks fire for RTDS coins.
  4. get_mid / get_spot / get_spot_age all return sensible values.
  5. RTDSClient shuts down cleanly via stop().

Run:
    pytest tests/test_rtds_live.py -v -m live
"""
from __future__ import annotations

import asyncio
import collections
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]

from market_data.rtds_client import RTDSClient, SpotPrice

pytestmark = pytest.mark.live

# How long to wait for the first price before declaring a test failed.
_TIMEOUT_S = 20

# How long to observe the feed for sustained-delivery tests.
_SCAN_DURATION_S = 30

# Minimum ticks expected per coin over _SCAN_DURATION_S seconds.
# RTDS WS typically updates every 1–5 s per coin; ≥5 ticks in 30 s is conservative.
_MIN_TICKS_PER_COIN = 5

# Maximum acceptable gap (seconds) between consecutive ticks for a coin.
# If zero ticks arrive for ≥20 s the SL monitor would be flying blind.
_MAX_GAP_S = 20


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

    All coins come from the ``crypto_prices`` topic → stored in client._prices.
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
        return dict(client._prices)

    return _run(_collect())


# ─────────────────────────────────────────────────────────────────────────────
# Basic connectivity
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSConnectivity:

    def test_receives_at_least_one_price(self, live_rtds_prices):
        """RTDSClient connects and gets at least one price update."""
        assert len(live_rtds_prices) > 0, "RTDSClient received no prices in time"


# ─────────────────────────────────────────────────────────────────────────────
# crypto_prices topic — RTDS coins
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSCoins:
    """Verify all crypto_prices (RTDS) topic coins are populated."""

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

    def test_callback_fires_for_rtds_coins(self):
        """on_price_update fires for RTDS coins (BTC/ETH/SOL/XRP/BNB/DOGE)."""
        received: dict[str, float] = {}

        async def _collect() -> None:
            client = RTDSClient()

            async def _cb(coin: str, price: float) -> None:
                received[coin] = price

            client.on_price_update(_cb)
            await client.start()

            deadline = time.monotonic() + _TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                has_rtds = any(c in received for c in ["BTC", "ETH", "SOL"])
                if has_rtds:
                    break

            await client.stop()

        _run(_collect())

        rtds_coins = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}
        received_rtds = rtds_coins & set(received.keys())
        assert received_rtds, (
            "No RTDS crypto_prices coins received via callback — "
            f"crypto_prices subscription may be broken. Received: {list(received.keys())}"
        )
        for coin, price in received.items():
            assert price > 0, f"Callback received non-positive price for {coin}: {price}"


# ─────────────────────────────────────────────────────────────────────────────
# Sustained feed: 30-second continuous observation per coin
#
# This proves the feed isn't just a one-shot grab — it keeps delivering ticks
# continuously, as required for the stop-loss monitor to run without blind spots.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live_rtds_sustained() -> dict[str, list[float]]:
    """
    Run RTDSClient for exactly _SCAN_DURATION_S seconds, recording the
    timestamp of every tick received per coin via both callback paths:
      - on_price_update  → RTDS exchange-aggregated (BTC/ETH/SOL/XRP/BNB/DOGE)
      - on_chainlink_update → Chainlink oracle (HYPE + Chainlink coins)

    Returns a dict mapping coin → sorted list of tick timestamps.
    """
    async def _observe() -> dict[str, list[float]]:
        tick_times: dict[str, list[float]] = collections.defaultdict(list)
        client = RTDSClient()

        async def _record(coin: str, price: float) -> None:
            tick_times[coin].append(time.monotonic())

        client.on_price_update(_record)
        await client.start()
        await asyncio.sleep(_SCAN_DURATION_S)
        await client.stop()
        return {coin: sorted(ts) for coin, ts in tick_times.items()}

    return _run(_observe())


class TestRTDSSustainedFeed:
    """
    Verify the spot price feed delivers continuous ticks for every coin over
    a 30-second window — the minimum bar for reliable stop-loss monitoring.
    """

    RTDS_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]

    @pytest.mark.parametrize("coin", RTDS_COINS)
    def test_coin_receives_minimum_ticks(self, coin, live_rtds_sustained):
        """Each coin must deliver at least _MIN_TICKS_PER_COIN ticks in 30 s."""
        ticks = live_rtds_sustained.get(coin, [])
        assert len(ticks) >= _MIN_TICKS_PER_COIN, (
            f"{coin}: only {len(ticks)} ticks in {_SCAN_DURATION_S}s "
            f"— expected ≥{_MIN_TICKS_PER_COIN}. "
            "Feed may be stalling or subscription failed."
        )

    @pytest.mark.parametrize("coin", RTDS_COINS)
    def test_coin_max_gap_under_threshold(self, coin, live_rtds_sustained):
        """No gap between consecutive ticks should exceed _MAX_GAP_S seconds."""
        ticks = live_rtds_sustained.get(coin, [])
        if len(ticks) < 2:
            pytest.skip(f"{coin}: not enough ticks to measure gaps ({len(ticks)} received)")
        gaps = [ticks[i+1] - ticks[i] for i in range(len(ticks) - 1)]
        worst_gap = max(gaps)
        assert worst_gap < _MAX_GAP_S, (
            f"{coin}: longest gap between ticks is {worst_gap:.1f}s "
            f"(threshold: {_MAX_GAP_S}s). "
            "The stop-loss monitor would be blind for this duration."
        )

    @pytest.mark.parametrize("coin", RTDS_COINS)
    def test_rtds_coin_callback_path(self, coin, live_rtds_sustained):
        """RTDS (exchange-aggregated) coins fire on_price_update continuously."""
        ticks = live_rtds_sustained.get(coin, [])
        assert len(ticks) > 0, (
            f"{coin}: zero ticks received via on_price_update in {_SCAN_DURATION_S}s"
        )

    def test_no_coin_goes_silent_midway(self, live_rtds_sustained):
        """
        Split the 30 s window into two 15 s halves and verify every coin got
        at least one tick in EACH half.  Catches feeds that update once then die.
        """
        half = _SCAN_DURATION_S / 2.0
        all_ticks_flat = [t for ts in live_rtds_sustained.values() for t in ts]
        if not all_ticks_flat:
            pytest.skip("No ticks received at all")
        origin = min(all_ticks_flat)

        for coin in self.RTDS_COINS:
            ticks = live_rtds_sustained.get(coin, [])
            rel = [t - origin for t in ticks]
            first_half  = [t for t in rel if t < half]
            second_half = [t for t in rel if t >= half]
            assert first_half,  (
                f"{coin}: no ticks in first 15 s half of the 30 s window"
            )
            assert second_half, (
                f"{coin}: no ticks in second 15 s half — feed went silent after {max(rel, default=0):.1f}s"
            )

    def test_tick_rate_summary(self, live_rtds_sustained):
        """Print a per-coin tick-rate summary (informational — always passes)."""
        lines = [f"\n{'Coin':<8} {'Ticks':>6} {'Rate/min':>10} {'MaxGap(s)':>11}"]
        lines.append("-" * 40)
        for coin in sorted(live_rtds_sustained):
            ticks = live_rtds_sustained[coin]
            rate  = len(ticks) / _SCAN_DURATION_S * 60
            gaps  = [ticks[i+1] - ticks[i] for i in range(len(ticks) - 1)]
            worst = max(gaps) if gaps else float("inf")
            lines.append(f"{coin:<8} {len(ticks):>6}  {rate:>9.1f}  {worst:>10.1f}s")
        print("\n".join(lines))
        assert True  # always passes — output visible with pytest -s


# ─────────────────────────────────────────────────────────────────────────────
# Raw WS throughput: count frames BEFORE any per-coin filtering
#
# Answers: is 1 tick/s the feed ceiling, or is the client discarding frames?
# ─────────────────────────────────────────────────────────────────────────────

class TestRTDSRawThroughput:
    """
    Measure the raw frame rate on the RTDS WebSocket vs the per-coin processed
    tick rate.  If raw >> processed, there is client-side filtering/deduplication
    to investigate.  If raw ≈ processed × coins, the feed itself is the ceiling.
    """

    def test_raw_frame_rate_vs_processed_ticks(self):
        """
        Monkey-patch _handle_message to count raw frames before processing,
        while also counting per-coin RTDS ticks normally.
        Run for _SCAN_DURATION_S seconds and print the comparison.
        """
        raw_frames: list[float] = []
        rtds_ticks: dict[str, list[float]] = collections.defaultdict(list)

        async def _run_probe() -> dict:
            client = RTDSClient()

            # Wrap _handle_message to count every raw WS frame
            original_handle = client._handle_message
            async def _counting_handle(raw):
                raw_frames.append(time.monotonic())
                await original_handle(raw)
            client._handle_message = _counting_handle

            async def _rtds_cb(coin: str, price: float) -> None:
                rtds_ticks[coin].append(time.monotonic())

            client.on_price_update(_rtds_cb)
            await client.start()
            await asyncio.sleep(_SCAN_DURATION_S)
            await client.stop()

            total_raw  = len(raw_frames)
            total_rtds = sum(len(v) for v in rtds_ticks.values())
            raw_rate   = total_raw  / _SCAN_DURATION_S
            rtds_rate  = total_rtds / _SCAN_DURATION_S

            return {
                "total_raw": total_raw,
                "total_rtds_processed": total_rtds,
                "raw_fps": raw_rate,
                "rtds_fps": rtds_rate,
                "rtds_coins": {c: len(v) for c, v in rtds_ticks.items()},
            }

        stats = _run(_run_probe())

        lines = [
            f"\n{'Metric':<35} {'Value':>10}",
            "-" * 47,
            f"{'Raw WS frames in 30s':<35} {stats['total_raw']:>10}",
            f"{'Raw frame rate (fps)':<35} {stats['raw_fps']:>10.1f}",
            f"{'RTDS processed ticks (all coins)':<35} {stats['total_rtds_processed']:>10}",
            f"{'RTDS tick rate (all coins, fps)':<35} {stats['rtds_fps']:>10.1f}",
            "",
            f"{'Unaccounted frames':<35} {stats['total_raw'] - stats['total_rtds_processed']:>10}",
            "  (PONG frames, sub confirmations, unknown symbols)",
            "",
            "Per-coin RTDS ticks:",
        ]
        for coin in sorted(stats["rtds_coins"]):
            lines.append(f"  {coin:<8} {stats['rtds_coins'][coin]:>4} ticks  "
                         f"({stats['rtds_coins'][coin]/_SCAN_DURATION_S*60:.0f}/min)")
        print("\n".join(lines))

        overhead = stats["total_raw"] - stats["total_rtds_processed"]
        assert overhead >= 0, "More processed ticks than raw frames — impossible"
        assert overhead < 150, (
            f"Unexpectedly high unaccounted frames ({overhead}) — "
            "client may be dropping frames silently"
        )
        assert stats["rtds_fps"] >= 1.0, (
            f"RTDS tick rate {stats['rtds_fps']:.2f} fps < 1 fps — feed appears stalled"
        )

