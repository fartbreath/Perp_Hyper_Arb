"""
tests/test_rtds_chainlink_live.py — Live oracle reliability tests for ALL supported coins.

This is the primary oracle gap-detection test suite.  It was created specifically
to catch the silent-zero-events bug that caused onchain_cl_ages_s={} in earlier
versions (free public RPC silently dropped all eth_subscribe events; the WS
appeared connected but never delivered a single AnswerUpdated log).

Two oracle paths are tested independently:

  A. ChainlinkWSClient  — eth_subscribe AnswerUpdated for BTC/ETH/SOL/XRP/BNB/DOGE
     Source: Polygon Mainnet AggregatorV3 contracts via POLYGON_WS_URL.
     Heartbeat: ≤ 30 s per contract.  Section skipped if POLYGON_WS_URL is not
     a real paid endpoint (Alchemy/Infura/QuickNode).

  B. RTDS crypto_prices_chainlink  — HYPE/USD Chainlink Data Streams relay
     Source: Polymarket RTDS WebSocket, crypto_prices_chainlink topic.
     No API keys required.  Heartbeat: ≤ 30 s (Chainlink deviation-triggered
     updates can arrive faster during price moves).

For each path the tests verify:
  1. HTTP seed (ChainlinkWSClient only): all coins populated within a few seconds.
  2. WS events actually arrive — callback counter > 0 for every coin.
     This directly catches the silent-zero-events bug.
  3. Minimum tick count over the observation window.
  4. Maximum gap between consecutive ticks per coin ≤ MAX_GAP_S.
  5. All spot ages are finite after the observation window (not inf = never received).
  6. Prices are positive and within plausible ranges.

Run (requires live network access and, for section A, a paid POLYGON_WS_URL):
    pytest tests/test_rtds_chainlink_live.py -v -m live

IMPORTANT — AggregatorV3 heartbeat reference (Polygon Mainnet):
    BTC/USD   30 s,  0.5 % deviation
    ETH/USD   30 s,  0.5 % deviation
    SOL/USD   30 s,  1.0 % deviation
    XRP/USD   30 s,  1.0 % deviation
    BNB/USD   30 s,  0.5 % deviation
    DOGE/USD  30 s,  1.0 % deviation
    HYPE/USD  (Data Streams, not AggregatorV3) — Chainlink DevEx sets heartbeat.
"""

import asyncio
import collections
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"]

from market_data.chainlink_ws_client import ChainlinkWSClient, _CL_CONTRACTS
from market_data.rtds_client import RTDSClient, SpotPrice

pytestmark = pytest.mark.live

# ── Timing constants ──────────────────────────────────────────────────────────

# Maximum seconds to wait for the very first event per coin.
# AggregatorV3 heartbeat ≤ 30 s; HYPE heartbeat ≤ 30 s.  70 s gives 2× margin.
_FIRST_EVENT_TIMEOUT_S = 70

# Sustained observation window used for tick-count and gap checks.
_SCAN_DURATION_S = 90

# AggregatorV3 heartbeat ≤ 30 s; allow 2× + jitter = 70 s before flagging a gap.
_CL_MAX_GAP_S = 70.0

# HYPE (Chainlink Data Streams relay): same 30 s heartbeat, same margin.
_HYPE_MAX_GAP_S = 70.0

# Minimum WS events per AggregatorV3 coin over _SCAN_DURATION_S.
# At ≥ 1 event every 30 s we expect ≥ 3 in 90 s.  Use 2 as the floor to allow
# for jitter near the window boundaries.
_CL_MIN_TICKS = 2

# Minimum events for HYPE relayed via RTDS.
_HYPE_MIN_TICKS = 2

# Demo/fallback Alchemy URL that won't actually deliver events.
_DEMO_WS_URL_FRAGMENT = "/demo"


def _is_real_ws_url() -> bool:
    """Return True only if POLYGON_WS_URL looks like a real paid endpoint.

    The default/demo Alchemy URL will connect but the demo key has very limited
    rate limits and does not deliver eth_subscribe logs reliably.
    Always set POLYGON_WS_URL to your own key before running section A.
    """
    url = getattr(config, "POLYGON_WS_URL", "")
    return bool(url) and _DEMO_WS_URL_FRAGMENT not in url


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


# ═════════════════════════════════════════════════════════════════════════════
# Section A — ChainlinkWSClient (AggregatorV3 eth_subscribe, all 6 coins)
# ═════════════════════════════════════════════════════════════════════════════

_CL_COINS = sorted(_CL_CONTRACTS.keys())   # BTC, ETH, SOL, XRP, BNB, DOGE


@pytest.fixture(scope="module")
def chainlink_ws_sustained() -> dict:
    """
    Run ChainlinkWSClient for _SCAN_DURATION_S seconds.

    Records every WS event (via on_price_update callback) per coin.
    The HTTP seed runs first (within start()), populating _prices regardless
    of WS liveness — we track WS events separately via callbacks.

    Returns::
        {
            "client":       stopped ChainlinkWSClient,
            "tick_times":   dict[coin, sorted list of monotonic event times],
            "tick_prices":  dict[coin, list of prices in arrival order],
            "seed_ages":    dict[coin, float] — ages immediately after start(),
                            measured before the WS could deliver any event.
        }

    Skips if POLYGON_WS_URL is not a real paid endpoint.
    """
    if not _is_real_ws_url():
        pytest.skip(
            "POLYGON_WS_URL is the demo URL or not set — no real eth_subscribe events "
            "will arrive.  Set POLYGON_WS_URL to a paid Alchemy/Infura/QuickNode WSS "
            "endpoint to run ChainlinkWSClient reliability tests."
        )

    async def _observe():
        tick_times: dict[str, list[float]] = collections.defaultdict(list)
        tick_prices: dict[str, list[float]] = collections.defaultdict(list)
        client = ChainlinkWSClient()

        async def _record(coin: str, price: float) -> None:
            tick_times[coin].append(time.monotonic())
            tick_prices[coin].append(price)

        client.on_price_update(_record)
        await client.start()

        # Capture ages immediately after seed — before WS events can arrive.
        # If HTTP seed worked, all ages should be finite here.
        seed_ages = {coin: client.get_spot_age(coin) for coin in _CL_COINS}

        await asyncio.sleep(_SCAN_DURATION_S)
        await client.stop()

        return {
            "client":      client,
            "tick_times":  {c: sorted(ts) for c, ts in tick_times.items()},
            "tick_prices": {c: list(ps) for c, ps in tick_prices.items()},
            "seed_ages":   seed_ages,
        }

    return _run(_observe())


class TestChainlinkWSHTTPSeed:
    """HTTP seed runs synchronously inside start() — all 6 coins must be
    populated before the WS can deliver any event."""

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_seed_populated_coin(self, coin, chainlink_ws_sustained):
        client: ChainlinkWSClient = chainlink_ws_sustained["client"]
        mid = client.get_mid(coin)
        # Cache is preserved after stop().
        assert mid is not None, (
            f"ChainlinkWSClient.get_mid('{coin}') is None after run. "
            f"HTTP seed (latestRoundData) may have failed for this coin."
        )
        assert mid > 0

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_seed_age_finite_at_start(self, coin, chainlink_ws_sustained):
        """Age must be finite immediately after start() — HTTP seed should have fired."""
        age = chainlink_ws_sustained["seed_ages"][coin]
        assert age != float("inf"), (
            f"Age for '{coin}' was inf immediately after start() — "
            f"HTTP latestRoundData() seed completely failed."
        )
        # Seed happens synchronously in start(); age should be near zero.
        assert age < 30, f"'{coin}' seed age {age:.1f}s is unexpectedly old"


class TestChainlinkWSEventDelivery:
    """
    WS event delivery tests — directly catches the silent-zero-events bug.

    The old bug: WS connected to a free public RPC that silently accepted the
    eth_subscribe request but never delivered a single AnswerUpdated event.
    All coin ages remained at their HTTP seed value (never updated by WS).
    These tests assert that WS events actually flow from the subscription.
    """

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_ws_events_received_for_coin(self, coin, chainlink_ws_sustained):
        """At least one WS AnswerUpdated event must be received per coin over the window.

        ZERO WS events = silent zero-events bug.  The HTTP seed can mask this in
        production because prices appear populated, but oracle liveness is broken.
        """
        n = len(chainlink_ws_sustained["tick_times"].get(coin, []))
        assert n >= 1, (
            f"ZERO WS AnswerUpdated events received for {coin} in {_SCAN_DURATION_S}s. "
            f"Silent-zero-events bug detected: the WS endpoint accepted the "
            f"eth_subscribe request but delivered no events. "
            f"Verify POLYGON_WS_URL is a paid endpoint that supports eth_subscribe logs."
        )

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_minimum_tick_count(self, coin, chainlink_ws_sustained):
        n = len(chainlink_ws_sustained["tick_times"].get(coin, []))
        assert n >= _CL_MIN_TICKS, (
            f"{coin}: only {n} WS events in {_SCAN_DURATION_S}s "
            f"(need ≥ {_CL_MIN_TICKS}, AggregatorV3 heartbeat ≤ 30 s). "
            f"Feed may be degraded or endpoint has rate-limiting."
        )

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_no_excessive_gap_between_events(self, coin, chainlink_ws_sustained):
        """Max gap between consecutive AnswerUpdated events must be < _CL_MAX_GAP_S.

        A gap > 2× heartbeat while the WS appears connected indicates the oracle
        endpoint is rate-limiting or silently throttling event delivery.
        """
        times = chainlink_ws_sustained["tick_times"].get(coin, [])
        if len(times) < 2:
            pytest.skip(f"{coin}: fewer than 2 events — see test_minimum_tick_count")
        gaps = [(times[i + 1] - times[i]) for i in range(len(times) - 1)]
        worst = max(gaps)
        assert worst <= _CL_MAX_GAP_S, (
            f"{coin}: gap of {worst:.1f}s between consecutive WS events "
            f"(limit {_CL_MAX_GAP_S}s, heartbeat 30 s). "
            f"Events are dropping — check endpoint rate limits or connection stability."
        )

    @pytest.mark.parametrize("coin", _CL_COINS)
    def test_event_prices_positive(self, coin, chainlink_ws_sustained):
        for price in chainlink_ws_sustained["tick_prices"].get(coin, []):
            assert price > 0, f"{coin}: WS event delivered non-positive price {price}"

    def test_all_coins_received_ws_events(self, chainlink_ws_sustained):
        """After the full observation window, all coins must have received WS events.

        Zero-events for any coin = silent-zero-events bug.  The HTTP seed can mask
        this in production: prices appear populated but the event path is broken.
        """
        zero_coins = [
            coin for coin in _CL_COINS
            if len(chainlink_ws_sustained["tick_times"].get(coin, [])) == 0
        ]
        assert not zero_coins, (
            f"Zero WS events received for: {zero_coins}. "
            f"These coins had prices only from the HTTP seed — oracle event path is broken."
        )


# ═════════════════════════════════════════════════════════════════════════════
# Section B — RTDS crypto_prices_chainlink relay (HYPE/USD) + co-existence
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def rtds_chainlink_sustained() -> dict:
    """
    Run RTDSClient for _SCAN_DURATION_S seconds, observing the
    crypto_prices_chainlink topic (HYPE/USD) via on_chainlink_update.

    Also records crypto_prices (exchange-aggregated) ticks for BTC/ETH/SOL/XRP/
    BNB/DOGE to verify the two subscriptions on the same WS don't interfere.

    Returns::
        {
            "client":           stopped RTDSClient,
            "hype_tick_times":  sorted list of HYPE tick arrival times,
            "hype_tick_prices": list of HYPE prices in arrival order,
            "rtds_tick_counts": dict[coin, int] — tick count per RTDS coin,
        }
    """
    async def _observe():
        hype_tick_times: list[float] = []
        hype_tick_prices: list[float] = []
        rtds_tick_counts: dict[str, int] = collections.defaultdict(int)
        client = RTDSClient()

        async def _hype_record(coin: str, price: float) -> None:
            if coin == "HYPE":
                hype_tick_times.append(time.monotonic())
                hype_tick_prices.append(price)

        async def _rtds_record(coin: str, price: float) -> None:
            rtds_tick_counts[coin] += 1

        client.on_chainlink_update(_hype_record)
        client.on_price_update(_rtds_record)
        await client.start()
        await asyncio.sleep(_SCAN_DURATION_S)
        await client.stop()

        return {
            "client":           client,
            "hype_tick_times":  sorted(hype_tick_times),
            "hype_tick_prices": hype_tick_prices,
            "rtds_tick_counts": dict(rtds_tick_counts),
        }

    return _run(_observe())


class TestHYPEChainlinkRelayDelivery:
    """
    Gap-detection tests for HYPE via Polymarket's crypto_prices_chainlink relay.

    Same structure as Section A: zero-events check, min tick count, max gap.
    """

    def test_hype_events_received(self, rtds_chainlink_sustained):
        """At least one HYPE event must arrive via crypto_prices_chainlink.

        Zero events = feed is either down or the subscription filter is broken.
        """
        n = len(rtds_chainlink_sustained["hype_tick_times"])
        assert n >= 1, (
            f"ZERO crypto_prices_chainlink events for HYPE in {_SCAN_DURATION_S}s. "
            f"This feed is the primary oracle for HYPE 5m/15m markets. "
            f"Either Polymarket has stopped pushing this topic, or the subscription "
            f'filter ("hype/usd") is mismatched with the live feed.'
        )

    def test_hype_minimum_tick_count(self, rtds_chainlink_sustained):
        n = len(rtds_chainlink_sustained["hype_tick_times"])
        assert n >= _HYPE_MIN_TICKS, (
            f"HYPE: only {n} ticks in {_SCAN_DURATION_S}s (need ≥ {_HYPE_MIN_TICKS}). "
            f"Chainlink heartbeat ≤ 30 s — feed may be degraded."
        )

    def test_hype_no_excessive_gap(self, rtds_chainlink_sustained):
        times = rtds_chainlink_sustained["hype_tick_times"]
        if len(times) < 2:
            pytest.skip("Too few HYPE ticks for gap analysis — see test_hype_minimum_tick_count")
        gaps = [(times[i + 1] - times[i]) for i in range(len(times) - 1)]
        worst = max(gaps)
        assert worst <= _HYPE_MAX_GAP_S, (
            f"HYPE: gap of {worst:.1f}s between consecutive oracle ticks "
            f"(limit {_HYPE_MAX_GAP_S}s). "
            f"Long gaps mean the SL monitor is flying blind between heartbeats."
        )

    def test_hype_prices_positive(self, rtds_chainlink_sustained):
        for price in rtds_chainlink_sustained["hype_tick_prices"]:
            assert price > 0, f"HYPE relay delivered non-positive price: {price}"

    def test_hype_prices_plausible(self, rtds_chainlink_sustained):
        for price in rtds_chainlink_sustained["hype_tick_prices"]:
            assert 1.0 <= price <= 10_000.0, f"HYPE price {price} outside [$1, $10k] range"

    def test_hype_accessor_mid(self, rtds_chainlink_sustained):
        client: RTDSClient = rtds_chainlink_sustained["client"]
        mid = client.get_chainlink_mid("HYPE")
        assert mid is not None, "get_chainlink_mid('HYPE') is None after sustained run"
        assert mid > 0

    def test_hype_accessor_spot(self, rtds_chainlink_sustained):
        client: RTDSClient = rtds_chainlink_sustained["client"]
        snap = client.get_chainlink_spot("HYPE")
        assert isinstance(snap, SpotPrice)
        assert snap.coin == "HYPE"
        assert snap.price > 0

    def test_hype_accessor_age_finite(self, rtds_chainlink_sustained):
        client: RTDSClient = rtds_chainlink_sustained["client"]
        age = client.get_chainlink_age("HYPE")
        assert age != float("inf"), "HYPE get_chainlink_age returned inf — never received"
        assert age < _SCAN_DURATION_S + 30

    def test_get_chainlink_mid_unknown_coin_is_none(self, rtds_chainlink_sustained):
        client: RTDSClient = rtds_chainlink_sustained["client"]
        assert client.get_chainlink_mid("NOTACOIN") is None

    def test_get_chainlink_age_unknown_coin_is_inf(self, rtds_chainlink_sustained):
        client: RTDSClient = rtds_chainlink_sustained["client"]
        assert client.get_chainlink_age("NOTACOIN") == float("inf")


class TestDualSubscriptionNonInterference:
    """
    Adding crypto_prices_chainlink on the same WS connection as crypto_prices
    must not disrupt exchange-aggregated tick delivery for any coin.

    This is the co-existence test: both subscriptions must deliver independently.
    """

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_rtds_coin_still_receives_ticks(self, coin, rtds_chainlink_sustained):
        n = rtds_chainlink_sustained["rtds_tick_counts"].get(coin, 0)
        assert n > 0, (
            f"{coin}: zero crypto_prices ticks received during {_SCAN_DURATION_S}s "
            f"while crypto_prices_chainlink was active on the same WS connection. "
            f"The dual subscription may be breaking the primary exchange-aggregated feed."
        )

    def test_hype_and_btc_both_received(self, rtds_chainlink_sustained):
        """Sanity: HYPE (chainlink relay) and BTC (exchange-aggregated) both arrive."""
        hype_n = len(rtds_chainlink_sustained["hype_tick_times"])
        btc_n = rtds_chainlink_sustained["rtds_tick_counts"].get("BTC", 0)
        assert hype_n > 0, "HYPE chainlink relay: zero ticks"
        assert btc_n > 0, "BTC exchange-aggregated: zero ticks (dual-sub may have broken feed)"
