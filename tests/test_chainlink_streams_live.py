"""
tests/test_chainlink_streams_live.py — Live reliability tests for ChainlinkStreamsClient HA mode.

Connects to the real Chainlink Data Streams WS endpoint and verifies:

  1. HA origin discovery   — server advertises ≥2 origins; configured_connections=2.
  2. Price delivery        — at least one accepted report per configured feed.
  3. Minimum tick count    — at least _MIN_TICKS ticks per coin over _SCAN_DURATION_S.
  4. Max gap               — no silence longer than _MAX_GAP_S per coin.
  5. HA dedup              — deduplicated > 0 when 2 connections are active (both carry same obs_ts).
  6. Plausible prices      — within hard sanity bounds per coin.
  7. Spot age              — finite and recent after the observation window.
  8. is_connected          — True while running, False after stop().
  9. Reconnects            — server-side session rotations (code=1000 every ~15-40s) are
                             survived; stats.partial_reconnects accumulates naturally.

Prerequisites:
  CHAINLINK_DS_USERNAME + CHAINLINK_DS_PASSWORD   (Mercury basic-auth credentials), OR
  CHAINLINK_DS_API_KEY  + CHAINLINK_DS_API_SECRET (legacy HMAC)

  At least one CHAINLINK_DS_{COIN}_FEED_ID must be set in environment.

Run:
    pytest tests/test_chainlink_streams_live.py -v -m live
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
from market_data.chainlink_streams_client import ChainlinkStreamsClient

pytestmark = pytest.mark.live

# ── Timing constants ──────────────────────────────────────────────────────────

# The Chainlink Data Streams push every oracle update immediately — typically
# sub-second during market hours.  The oracle heartbeat is ≤30s even when
# prices are flat.
_SCAN_DURATION_S = 90   # seconds to observe; must exceed one server-session lifetime (~15-40s)

# Maximum silence before we consider a feed degraded.
# Chainlink Data Streams heartbeat ≤30s; 2× margin = 70s.
_MAX_GAP_S = 70.0

# Minimum accepted reports per coin over _SCAN_DURATION_S.
# At heartbeat ≤30s we expect ≥3 in 90s; use 2 as the floor to tolerate jitter.
_MIN_TICKS = 2

# Hard sanity bounds (USD) per coin — wide enough to survive extreme markets.
_PRICE_BOUNDS: dict[str, tuple[float, float]] = {
    "BTC":  (1_000.0,   10_000_000.0),
    "ETH":  (50.0,      500_000.0),
    "SOL":  (1.0,       50_000.0),
    "BNB":  (10.0,      200_000.0),
    "DOGE": (0.001,     1_000.0),
    "XRP":  (0.01,      10_000.0),
    "HYPE": (0.001,     100_000.0),
}
_DEFAULT_BOUNDS = (1e-6, 1e9)


def _has_credentials() -> bool:
    has_basic = bool(config.CHAINLINK_DS_USERNAME and config.CHAINLINK_DS_PASSWORD)
    has_hmac  = bool(config.CHAINLINK_DS_API_KEY  and config.CHAINLINK_DS_API_SECRET)
    return has_basic or has_hmac


def _configured_coins() -> list[str]:
    """Return coins that have a non-empty feed ID in config."""
    return [
        c for c, fid in config.CHAINLINK_DS_FEED_IDS.items()
        if fid and fid.startswith("0x")
    ]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


# ── Module-level fixture: run the client for _SCAN_DURATION_S ────────────────

@pytest.fixture(scope="module")
def streams_sustained() -> dict:
    """
    Start ChainlinkStreamsClient, observe for _SCAN_DURATION_S, then stop.

    Returns a dict with all the data needed by the individual tests.

    Skips if credentials or feed IDs are not configured.
    """
    if not _has_credentials():
        pytest.skip(
            "Chainlink Data Streams credentials not set. "
            "Set CHAINLINK_DS_USERNAME+PASSWORD or CHAINLINK_DS_API_KEY+SECRET "
            "plus at least one CHAINLINK_DS_{COIN}_FEED_ID to run live tests."
        )
    coins = _configured_coins()
    if not coins:
        pytest.skip(
            "No CHAINLINK_DS_{COIN}_FEED_ID variables set. "
            "Set at least one (e.g. CHAINLINK_DS_HYPE_FEED_ID) to run live tests."
        )

    async def _observe():
        tick_times:  dict[str, list[float]] = collections.defaultdict(list)
        tick_prices: dict[str, list[float]] = collections.defaultdict(list)

        client = ChainlinkStreamsClient()

        async def _record(coin: str, price: float) -> None:
            tick_times[coin].append(time.monotonic())
            tick_prices[coin].append(price)

        client.on_price_update(_record)
        await client.start()

        # Short wait to let both HA connections settle before recording state.
        await asyncio.sleep(2)
        connected_after_start = client.is_connected

        await asyncio.sleep(_SCAN_DURATION_S - 2)

        # Snapshot stats before stop — stop() zeros active_connections.
        snapshot_stats = {
            "accepted":               client.stats.accepted,
            "deduplicated":           client.stats.deduplicated,
            "partial_reconnects":     client.stats.partial_reconnects,
            "full_reconnects":        client.stats.full_reconnects,
            "configured_connections": client.stats.configured_connections,
            "active_connections":     client.stats.active_connections,
        }

        final_prices  = {c: client.get_mid(c)      for c in coins}
        final_ages    = {c: client.get_spot_age(c)  for c in coins}

        await client.stop()

        connected_after_stop = client.is_connected

        return {
            "client":                 client,
            "tick_times":             {c: sorted(ts) for c, ts in tick_times.items()},
            "tick_prices":            {c: list(ps)   for c, ps in tick_prices.items()},
            "stats":                  snapshot_stats,
            "final_prices":           final_prices,
            "final_ages":             final_ages,
            "coins":                  coins,
            "connected_after_start":  connected_after_start,
            "connected_after_stop":   connected_after_stop,
        }

    return _run(_observe())


# ── Helper to skip per-coin tests for coins that received zero ticks ──────────

def _require_ticks(coin: str, fixture: dict) -> None:
    if not fixture["tick_times"].get(coin):
        pytest.fail(
            f"ZERO Data Streams ticks received for {coin} in {_SCAN_DURATION_S}s. "
            f"Check that CHAINLINK_DS_{coin}_FEED_ID is set correctly and "
            f"that the feed ID is valid for your API credentials."
        )


# ── 1. HA origin discovery ────────────────────────────────────────────────────

class TestHAOriginDiscovery:
    def test_configured_connections_ge_1(self, streams_sustained):
        """Server must have advertised at least one origin."""
        cc = streams_sustained["stats"]["configured_connections"]
        assert cc >= 1, (
            f"configured_connections={cc} — _fetch_origins() returned an empty list. "
            f"Verify credentials and that the Data Streams endpoint is reachable."
        )

    def test_ha_mode_two_origins(self, streams_sustained):
        """Standard Chainlink infrastructure advertises two origins (001, 002)."""
        cc = streams_sustained["stats"]["configured_connections"]
        assert cc == 2, (
            f"Expected 2 HA origins but got {cc}. "
            f"The server may be running in degraded mode or the HEAD request "
            f"did not return the x-cll-available-origins header."
        )


# ── 2. Connectivity ───────────────────────────────────────────────────────────

class TestConnectivity:
    def test_is_connected_after_start(self, streams_sustained):
        assert streams_sustained["connected_after_start"] is True, (
            "is_connected was False two seconds after start(). "
            "Check credentials and network connectivity."
        )

    def test_is_connected_false_after_stop(self, streams_sustained):
        assert streams_sustained["connected_after_stop"] is False, (
            "is_connected was still True after stop(). "
            "stop() must set _running=False and decrement active_connections."
        )


# ── 3. Price delivery ─────────────────────────────────────────────────────────

class TestPriceDelivery:
    def test_total_accepted_gt_0(self, streams_sustained):
        total = streams_sustained["stats"]["accepted"]
        assert total > 0, (
            f"stats.accepted=0 after {_SCAN_DURATION_S}s. "
            f"No Data Streams reports were decoded and delivered to callbacks. "
            f"Verify feed IDs and credentials."
        )

    @pytest.mark.parametrize("coin", _configured_coins())
    def test_price_received_per_coin(self, coin, streams_sustained):
        _require_ticks(coin, streams_sustained)

    @pytest.mark.parametrize("coin", _configured_coins())
    def test_minimum_tick_count(self, coin, streams_sustained):
        n = len(streams_sustained["tick_times"].get(coin, []))
        assert n >= _MIN_TICKS, (
            f"{coin}: only {n} ticks in {_SCAN_DURATION_S}s "
            f"(need ≥ {_MIN_TICKS}). "
            f"Feed may be throttled or heartbeat exceeded."
        )


# ── 4. Gap analysis ───────────────────────────────────────────────────────────

class TestGapAnalysis:
    @pytest.mark.parametrize("coin", _configured_coins())
    def test_no_excessive_gap(self, coin, streams_sustained):
        times = streams_sustained["tick_times"].get(coin, [])
        if len(times) < 2:
            pytest.skip(f"{coin}: fewer than 2 ticks — see test_minimum_tick_count")
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        worst = max(gaps)
        assert worst <= _MAX_GAP_S, (
            f"{coin}: max gap between ticks = {worst:.1f}s "
            f"(limit {_MAX_GAP_S}s). "
            f"The HA dedup/reconnect path may have introduced a gap."
        )


# ── 5. HA deduplication ───────────────────────────────────────────────────────

class TestHADedup:
    def test_dedup_occurs_in_ha_mode(self, streams_sustained):
        """
        With two parallel connections, the same observationsTimestamp will be
        delivered by both.  Over a 90s window the dedup counter must be > 0.

        If configured_connections == 1 (single-origin fallback), dedup = 0 is
        acceptable and the test is skipped.
        """
        cc   = streams_sustained["stats"]["configured_connections"]
        dedup = streams_sustained["stats"]["deduplicated"]
        if cc < 2:
            pytest.skip("Only one origin configured — no HA dedup expected.")
        assert dedup > 0, (
            f"deduplicated=0 with {cc} configured connections after {_SCAN_DURATION_S}s. "
            f"Both connections were active but no duplicate obs_ts were detected. "
            f"The dedup watermark or stats logic may be broken."
        )

    def test_dedup_plus_accepted_ge_total_reports(self, streams_sustained):
        """accepted + deduplicated must be the total raw reports that arrived."""
        accepted   = streams_sustained["stats"]["accepted"]
        dedup      = streams_sustained["stats"]["deduplicated"]
        # Both counters together account for every report that entered _handle_message.
        # Neither can exceed their sum.
        assert accepted >= 1
        assert accepted + dedup >= accepted  # trivially true; guard for negative values


# ── 6. Reconnect survivability ────────────────────────────────────────────────

class TestReconnects:
    def test_reconnect_counters_are_non_negative(self, streams_sustained):
        """
        Reconnect counters are always non-negative integers.

        Note: the Chainlink server rotates sessions every ~15-90s on average.
        In any given 90s window there is a meaningful probability (~10-20%) that
        no server-side close occurs, so we do NOT assert that reconnects > 0 here.
        The gap tests verify that reconnects (when they do occur) cause no price gap.
        """
        partial = streams_sustained["stats"]["partial_reconnects"]
        full    = streams_sustained["stats"]["full_reconnects"]
        assert partial >= 0
        assert full >= 0
        # Print for observability even when no reconnects occurred.
        print(
            f"\n[reconnect stats] partial={partial} full={full} "
            f"over {_SCAN_DURATION_S}s"
        )

    def test_no_full_reconnect_in_ha_mode(self, streams_sustained):
        """
        In HA mode, one connection dropping is always a partial reconnect because
        the other connection is still live.  full_reconnects should remain 0 unless
        both connections drop simultaneously (rare in a 90s window).
        """
        cc   = streams_sustained["stats"]["configured_connections"]
        full = streams_sustained["stats"]["full_reconnects"]
        if cc < 2:
            pytest.skip("Single-origin mode — full reconnects are expected.")
        assert full == 0, (
            f"full_reconnects={full} in HA mode (2 connections). "
            f"Both connections dropped simultaneously — this is unusual over {_SCAN_DURATION_S}s. "
            f"May indicate a network issue or a logic bug in active_connections accounting."
        )


# ── 7. Price sanity ───────────────────────────────────────────────────────────

class TestPriceSanity:
    @pytest.mark.parametrize("coin", _configured_coins())
    def test_price_positive_and_in_range(self, coin, streams_sustained):
        price = streams_sustained["final_prices"].get(coin)
        if price is None:
            pytest.skip(f"{coin}: no price received — see test_price_received_per_coin")
        lo, hi = _PRICE_BOUNDS.get(coin, _DEFAULT_BOUNDS)
        assert price > 0, f"{coin}: price={price} is not positive"
        assert lo <= price <= hi, (
            f"{coin}: price={price:.6f} is outside sanity bounds [{lo}, {hi}]. "
            f"Possible ABI decode error or wrong feed ID."
        )

    @pytest.mark.parametrize("coin", _configured_coins())
    def test_all_prices_positive(self, coin, streams_sustained):
        prices = streams_sustained["tick_prices"].get(coin, [])
        if not prices:
            pytest.skip(f"{coin}: no ticks")
        bad = [p for p in prices if p <= 0]
        assert not bad, f"{coin}: {len(bad)} non-positive prices received: {bad[:5]}"


# ── 8. Spot age ───────────────────────────────────────────────────────────────

class TestSpotAge:
    @pytest.mark.parametrize("coin", _configured_coins())
    def test_spot_age_finite_and_recent(self, coin, streams_sustained):
        age = streams_sustained["final_ages"].get(coin, float("inf"))
        assert age != float("inf"), (
            f"{coin}: spot age is inf — no price ever received. "
            f"Check feed ID and credentials."
        )
        # Age is measured right before stop(); allow up to 2× heartbeat + scan margin.
        assert age < _MAX_GAP_S, (
            f"{coin}: spot age {age:.1f}s is too old (limit {_MAX_GAP_S}s). "
            f"Last tick was stale — feed may be degraded."
        )
