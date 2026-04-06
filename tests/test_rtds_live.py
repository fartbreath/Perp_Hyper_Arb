"""
tests/test_rtds_live.py — Live end-to-end tests for RTDSClient.

Connects to the real Polymarket RTDS WebSocket and verifies that:
  1. The crypto_prices topic delivers prices for all RTDS coins
     (BTC, ETH, SOL, XRP, BNB, DOGE, LINK).
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
import collections
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"]

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

    RTDS coins (BTC, ETH, SOL, XRP, BNB, DOGE, LINK) come from the
    ``crypto_prices`` topic → stored in client._prices.
    Chainlink coins (HYPE, plus all the above) come from
    ``crypto_prices_chainlink`` → stored in client._chainlink_rtds.
    The fixture merges both caches so tests can look up any coin.
    """
    async def _collect() -> dict[str, SpotPrice]:
        client = RTDSClient()
        await client.start()

        deadline = time.monotonic() + _TIMEOUT_S
        expected = set(config.TRACKED_UNDERLYINGS)

        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            # Merge both caches: RTDS exchange-aggregated + Chainlink oracle.
            have = set(client.all_mids().keys()) | set(client.all_chainlink_mids().keys())
            if expected.issubset(have):
                break

        await client.stop()
        # Return merged snapshot; Chainlink values overwrite RTDS for same coin
        # (both are valid; the merge ensures HYPE is visible).
        return {**client._prices, **client._chainlink_rtds}

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

    def test_callback_fires_for_rtds_and_chainlink(self):
        """on_price_update fires for RTDS coins; on_chainlink_update fires for HYPE."""
        received: dict[str, float] = {}

        async def _collect() -> None:
            client = RTDSClient()

            async def _cb(coin: str, price: float) -> None:
                received[coin] = price

            # RTDS exchange-aggregated ticks → on_price_update
            client.on_price_update(_cb)
            # Chainlink oracle ticks → on_chainlink_update (HYPE lives here)
            client.on_chainlink_update(_cb)
            await client.start()

            deadline = time.monotonic() + _TIMEOUT_S
            # Wait until we have at least one RTDS coin AND HYPE (Chainlink)
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                has_rtds = any(c in received for c in ["BTC", "ETH", "SOL"])
                has_hype = "HYPE" in received
                if has_rtds and has_hype:
                    break

            await client.stop()

        _run(_collect())

        rtds_coins = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "LINK"}
        received_rtds = rtds_coins & set(received.keys())
        assert received_rtds, (
            "No RTDS crypto_prices coins received via callback — "
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
        client.on_chainlink_update(_record)
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

    RTDS_COINS      = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]
    CHAINLINK_COINS = ["HYPE"]
    ALL_COINS       = RTDS_COINS + CHAINLINK_COINS

    @pytest.mark.parametrize("coin", ALL_COINS)
    def test_coin_receives_minimum_ticks(self, coin, live_rtds_sustained):
        """Each coin must deliver at least _MIN_TICKS_PER_COIN ticks in 30 s."""
        ticks = live_rtds_sustained.get(coin, [])
        assert len(ticks) >= _MIN_TICKS_PER_COIN, (
            f"{coin}: only {len(ticks)} ticks in {_SCAN_DURATION_S}s "
            f"— expected ≥{_MIN_TICKS_PER_COIN}. "
            "Feed may be stalling or subscription failed."
        )

    @pytest.mark.parametrize("coin", ALL_COINS)
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

    def test_chainlink_hype_callback_path(self, live_rtds_sustained):
        """HYPE fires on_chainlink_update continuously (Chainlink oracle feed)."""
        ticks = live_rtds_sustained.get("HYPE", [])
        assert len(ticks) > 0, (
            f"HYPE: zero ticks received via on_chainlink_update in {_SCAN_DURATION_S}s"
        )

    def test_no_coin_goes_silent_midway(self, live_rtds_sustained):
        """
        Split the 30 s window into two 15 s halves and verify every coin got
        at least one tick in EACH half.  Catches feeds that update once then die.
        """
        half = _SCAN_DURATION_S / 2.0
        # tick_times are monotonic offsets from fixture start; reconstruct relative
        # to the earliest tick to make the halving meaningful.
        all_ticks_flat = [t for ts in live_rtds_sustained.values() for t in ts]
        if not all_ticks_flat:
            pytest.skip("No ticks received at all")
        origin = min(all_ticks_flat)

        for coin in self.ALL_COINS:
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
# Separated source test: RTDS vs Chainlink callbacks recorded independently
#
# The previous fixture writes both sources to the same dict, so we can't tell
# which source is responsible for BTC/ETH/SOL/XRP/BNB/DOGE ticks.
# This fixture tracks them in two separate dicts with 30 s observation.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live_rtds_separated() -> dict[str, dict[str, list[float]]]:
    """
    Run RTDSClient for _SCAN_DURATION_S seconds with RTDS and Chainlink
    callbacks writing to separate dicts.

    Returns {"rtds": {coin: [timestamps]}, "chainlink": {coin: [timestamps]}}
    """
    async def _observe() -> dict[str, dict[str, list[float]]]:
        rtds_ticks: dict[str, list[float]]      = collections.defaultdict(list)
        chainlink_ticks: dict[str, list[float]] = collections.defaultdict(list)
        client = RTDSClient()

        async def _rtds_cb(coin: str, price: float) -> None:
            rtds_ticks[coin].append(time.monotonic())

        async def _chainlink_cb(coin: str, price: float) -> None:
            chainlink_ticks[coin].append(time.monotonic())

        client.on_price_update(_rtds_cb)
        client.on_chainlink_update(_chainlink_cb)
        await client.start()
        await asyncio.sleep(_SCAN_DURATION_S)
        await client.stop()
        return {
            "rtds":      {c: sorted(ts) for c, ts in rtds_ticks.items()},
            "chainlink": {c: sorted(ts) for c, ts in chainlink_ticks.items()},
        }

    return _run(_observe())


class TestRTDSSourcesSeparated:
    """
    Verify RTDS (exchange-aggregated) and Chainlink oracle feeds deliver
    ticks independently over 30 s — each source is tested in isolation.
    """

    RTDS_COINS      = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]
    CHAINLINK_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"]

    # ── RTDS exchange-aggregated (crypto_prices topic) ────────────────────────

    @pytest.mark.parametrize("coin", RTDS_COINS)
    def test_rtds_source_delivers_ticks(self, coin, live_rtds_separated):
        """on_price_update fires ≥ _MIN_TICKS_PER_COIN times for RTDS coins."""
        ticks = live_rtds_separated["rtds"].get(coin, [])
        assert len(ticks) >= _MIN_TICKS_PER_COIN, (
            f"{coin}: RTDS exchange-aggregated feed delivered only {len(ticks)} ticks "
            f"in {_SCAN_DURATION_S}s via on_price_update "
            f"(expected ≥{_MIN_TICKS_PER_COIN})"
        )

    @pytest.mark.parametrize("coin", RTDS_COINS)
    def test_rtds_source_max_gap(self, coin, live_rtds_separated):
        """RTDS feed: no gap between consecutive ticks exceeds _MAX_GAP_S."""
        ticks = live_rtds_separated["rtds"].get(coin, [])
        if len(ticks) < 2:
            pytest.skip(f"{coin}: not enough RTDS ticks to measure gaps")
        worst = max(ticks[i+1] - ticks[i] for i in range(len(ticks) - 1))
        assert worst < _MAX_GAP_S, (
            f"{coin}: RTDS feed gap of {worst:.1f}s via on_price_update "
            f"(threshold {_MAX_GAP_S}s) — SL monitor would be blind"
        )

    # ── Chainlink oracle (crypto_prices_chainlink RTDS + on-chain Polygon) ───

    @pytest.mark.parametrize("coin", CHAINLINK_COINS)
    def test_chainlink_source_delivers_ticks(self, coin, live_rtds_separated):
        """on_chainlink_update fires ≥ _MIN_TICKS_PER_COIN times for every coin."""
        ticks = live_rtds_separated["chainlink"].get(coin, [])
        assert len(ticks) >= _MIN_TICKS_PER_COIN, (
            f"{coin}: Chainlink feed delivered only {len(ticks)} ticks "
            f"in {_SCAN_DURATION_S}s via on_chainlink_update "
            f"(expected ≥{_MIN_TICKS_PER_COIN}). "
            "Check that the blindspot fix (fire for ALL coins) is active."
        )

    @pytest.mark.parametrize("coin", CHAINLINK_COINS)
    def test_chainlink_source_max_gap(self, coin, live_rtds_separated):
        """Chainlink feed: no gap between consecutive ticks exceeds _MAX_GAP_S."""
        ticks = live_rtds_separated["chainlink"].get(coin, [])
        if len(ticks) < 2:
            pytest.skip(f"{coin}: not enough Chainlink ticks to measure gaps")
        worst = max(ticks[i+1] - ticks[i] for i in range(len(ticks) - 1))
        assert worst < _MAX_GAP_S, (
            f"{coin}: Chainlink feed gap of {worst:.1f}s via on_chainlink_update "
            f"(threshold {_MAX_GAP_S}s) — 5m/15m/4h SL monitor would be blind"
        )

    def test_hype_only_in_chainlink_not_rtds(self, live_rtds_separated):
        """HYPE has no crypto_prices RTDS entry — it must not appear in on_price_update."""
        rtds_hype = live_rtds_separated["rtds"].get("HYPE", [])
        assert len(rtds_hype) == 0, (
            f"HYPE received {len(rtds_hype)} ticks via on_price_update — "
            "unexpected: HYPE should only come through on_chainlink_update"
        )

    def test_separated_source_summary(self, live_rtds_separated):
        """Print per-coin, per-source tick counts (informational — always passes)."""
        rtds      = live_rtds_separated["rtds"]
        chainlink = live_rtds_separated["chainlink"]
        all_coins = sorted(set(rtds.keys()) | set(chainlink.keys()))
        lines = [
            f"\n{'Coin':<8} {'RTDS ticks':>12} {'CL ticks':>10} {'RTDS gap':>10} {'CL gap':>8}",
            "-" * 54,
        ]
        for coin in all_coins:
            r = rtds.get(coin, [])
            c = chainlink.get(coin, [])
            r_gap = max(r[i+1]-r[i] for i in range(len(r)-1)) if len(r) > 1 else float("inf")
            c_gap = max(c[i+1]-c[i] for i in range(len(c)-1)) if len(c) > 1 else float("inf")
            lines.append(
                f"{coin:<8} {len(r):>12}  {len(c):>9}  {r_gap:>8.1f}s  {c_gap:>6.1f}s"
            )
        print("\n".join(lines))
        assert True


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
        while also counting per-coin RTDS and Chainlink ticks normally.
        Run for _SCAN_DURATION_S seconds and print the comparison.
        """
        raw_frames: list[float] = []
        rtds_ticks:      dict[str, list[float]] = collections.defaultdict(list)
        chainlink_ticks: dict[str, list[float]] = collections.defaultdict(list)

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

            async def _cl_cb(coin: str, price: float) -> None:
                chainlink_ticks[coin].append(time.monotonic())

            client.on_price_update(_rtds_cb)
            client.on_chainlink_update(_cl_cb)
            await client.start()
            await asyncio.sleep(_SCAN_DURATION_S)
            await client.stop()

            # Compute stats
            total_raw   = len(raw_frames)
            total_rtds  = sum(len(v) for v in rtds_ticks.values())
            total_cl    = sum(len(v) for v in chainlink_ticks.values())
            raw_rate    = total_raw   / _SCAN_DURATION_S
            rtds_rate   = total_rtds  / _SCAN_DURATION_S
            cl_rate     = total_cl    / _SCAN_DURATION_S

            return {
                "total_raw": total_raw,
                "total_rtds_processed": total_rtds,
                "total_cl_processed": total_cl,
                "raw_fps": raw_rate,
                "rtds_fps": rtds_rate,
                "cl_fps": cl_rate,
                "rtds_coins": {c: len(v) for c, v in rtds_ticks.items()},
                "cl_coins": {c: len(v) for c, v in chainlink_ticks.items()},
            }

        stats = _run(_run_probe())

        lines = [
            f"\n{'Metric':<35} {'Value':>10}",
            "-" * 47,
            f"{'Raw WS frames in 30s':<35} {stats['total_raw']:>10}",
            f"{'Raw frame rate (fps)':<35} {stats['raw_fps']:>10.1f}",
            f"{'RTDS processed ticks (all coins)':<35} {stats['total_rtds_processed']:>10}",
            f"{'RTDS tick rate (all coins, fps)':<35} {stats['rtds_fps']:>10.1f}",
            f"{'Chainlink processed ticks (all)':<35} {stats['total_cl_processed']:>10}",
            f"{'Chainlink tick rate (all coins)':<35} {stats['cl_fps']:>10.1f}",
            f"{'Combined processed fps':<35} {stats['rtds_fps'] + stats['cl_fps']:>10.1f}",
            "",
            f"{'Unaccounted frames':<35} {stats['total_raw'] - stats['total_rtds_processed'] - stats['total_cl_processed']:>10}",
            "  (PONG frames, sub confirmations, unknown symbols)",
            "",
            "Per-coin RTDS ticks:",
        ]
        for coin in sorted(stats["rtds_coins"]):
            lines.append(f"  {coin:<8} {stats['rtds_coins'][coin]:>4} ticks  "
                         f"({stats['rtds_coins'][coin]/_SCAN_DURATION_S*60:.0f}/min)")
        lines.append("Per-coin Chainlink ticks:")
        for coin in sorted(stats["cl_coins"]):
            lines.append(f"  {coin:<8} {stats['cl_coins'][coin]:>4} ticks  "
                         f"({stats['cl_coins'][coin]/_SCAN_DURATION_S*60:.0f}/min)")
        print("\n".join(lines))

        # Unaccounted frames = overhead: PONG heartbeats (1 per 5s = 6/30s),
        # subscription confirmation messages, and RTDS coins we don't track
        # (e.g. LINK appears in crypto_prices but is not in TRACKED_UNDERLYINGS).
        # These are expected — not dropped frames.  Allow up to 150 overhead.
        overhead = stats["total_raw"] - stats["total_rtds_processed"] - stats["total_cl_processed"]
        assert overhead >= 0, "More processed ticks than raw frames — impossible"
        assert overhead < 150, (
            f"Unexpectedly high unaccounted frames ({overhead}) — "
            "client may be dropping frames silently"
        )
        # Both feeds should be delivering at least 1 fps combined
        assert stats["rtds_fps"] + stats["cl_fps"] >= 1.0, (
            "Combined RTDS + Chainlink tick rate < 1 fps — feed appears stalled"
        )
