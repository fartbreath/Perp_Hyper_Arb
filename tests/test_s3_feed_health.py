"""
tests/test_s3_feed_health.py — Unit tests for S3 Feed Health & Self-Healing.

Covers all four sub-tasks:

  S3.1  SpotOracle.get_feed_health(coin, market_type)
          - Correct source resolution priority (Streams → RTDS relay → CL WS → RTDS)
          - HEALTHY / STALE / DOWN status assignment
          - Chainlink reconnect metadata passed through
          - Status-transition logging fires exactly once per direction

  S3.2  _WSShard.health property + PMClient.get_shard_health()
          - DISCONNECTED when not running
          - CONNECTING when running but TCP not yet established
          - CONNECTED when running, connected, recent message
          - DEGRADED when running, connected, but silent > HL_WS_STALE_SECS
          - CONNECTED when running, connected, no message ever received
          - get_shard_health() assembles ShardHealth dataclasses with correct fields
          - last_message_age_secs is None before first message

  S3.3  ChainlinkStreamsClient reconnect rate tracking
          - reconnects_1h counts only timestamps within the last 3600 s
          - last_reconnect_at returns 0.0 before any reconnect
          - last_reconnect_at returns the most-recent reconnect epoch

  S3.4  HLClient.is_connected() + get_mark_price_age(coin)
          - is_connected() reflects _ws_connected flag
          - get_mark_price_age() returns None for unknown coin
          - get_mark_price_age() returns elapsed seconds based on FundingSnapshot.timestamp
          - PositionMonitor._manage_open_hedge_orders logs [HL_DEGRADED] when stale
          - No log when age is within threshold
          - Dedup: same coin with two hedge orders logs [HL_DEGRADED] only once per sweep

Run: pytest tests/test_s3_feed_health.py -v
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
from core.types import FeedHealth, ShardHealth

# ── module-level config patches so imports succeed without real credentials ──
config.CHAINLINK_DS_USERNAME   = "test-api-key"
config.CHAINLINK_DS_PASSWORD   = "test-api-secret"
config.CHAINLINK_DS_API_KEY    = ""
config.CHAINLINK_DS_API_SECRET = ""
config.CHAINLINK_DS_HOST       = "wss://ws.dataengine.chain.link"
config.CHAINLINK_DS_FEED_IDS   = {"BTC": "0xdeadbeef" + "0" * 56}

from market_data.rtds_client import SpotPrice
from market_data.spot_oracle import SpotOracle, CHAINLINK_MARKET_TYPES
from market_data.chainlink_streams_client import ChainlinkStreamsClient
from hl_client import HLClient, FundingSnapshot
from pm_client import _WSShard


# ── shared helpers ────────────────────────────────────────────────────────────

def _snap(coin: str, price: float, age_secs: float = 0.5) -> SpotPrice:
    """Build a SpotPrice snapshot with timestamp `age_secs` seconds in the past."""
    return SpotPrice(coin=coin, price=price, timestamp=time.time() - age_secs)


def _make_oracle(
    streams_snap: Optional[SpotPrice] = None,
    rtds_chainlink_snap: Optional[SpotPrice] = None,
    cl_ws_snap: Optional[SpotPrice] = None,
    rtds_snap: Optional[SpotPrice] = None,
    streams_reconnects_1h: int = 0,
    streams_last_reconnect_at: float = 0.0,
) -> SpotOracle:
    """Construct a SpotOracle backed entirely by mocks."""
    rtds = MagicMock()
    rtds.get_chainlink_spot = MagicMock(return_value=rtds_chainlink_snap)
    rtds.get_spot = MagicMock(return_value=rtds_snap)
    rtds.get_mid = MagicMock(return_value=rtds_snap.price if rtds_snap else None)
    rtds.get_spot_age = MagicMock(return_value=0.5)
    rtds.on_chainlink_update = MagicMock()
    rtds.on_price_update = MagicMock()
    rtds.all_mids = MagicMock(return_value={})

    cl = MagicMock()
    cl.get_spot = MagicMock(return_value=cl_ws_snap)
    cl.on_price_update = MagicMock()

    streams = MagicMock()
    streams.get_spot = MagicMock(return_value=streams_snap)
    streams.on_price_update = MagicMock()
    streams.reconnects_1h = streams_reconnects_1h
    streams.last_reconnect_at = streams_last_reconnect_at

    oracle = SpotOracle(rtds=rtds, chainlink=cl, streams=streams)
    return oracle


# ═════════════════════════════════════════════════════════════════════════════
# S3.1 — SpotOracle.get_feed_health
# ═════════════════════════════════════════════════════════════════════════════

class TestGetFeedHealthSourceResolution:
    """Source priority and field mapping for Chainlink vs non-Chainlink markets."""

    def test_chainlink_market_uses_streams_first(self):
        streams_snap = _snap("BTC", 90_000.0, age_secs=0.2)
        oracle = _make_oracle(
            streams_snap=streams_snap,
            rtds_chainlink_snap=_snap("BTC", 89_000.0, age_secs=0.5),
            cl_ws_snap=_snap("BTC", 88_000.0, age_secs=1.0),
        )
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.primary_source == "chainlink_streams"
        assert fh.price == pytest.approx(90_000.0)

    def test_chainlink_market_falls_to_rtds_relay_when_streams_empty(self):
        rtds_relay = _snap("BTC", 89_000.0, age_secs=0.5)
        oracle = _make_oracle(
            streams_snap=None,
            rtds_chainlink_snap=rtds_relay,
            cl_ws_snap=_snap("BTC", 88_000.0, age_secs=1.0),
        )
        fh = oracle.get_feed_health("BTC", "bucket_15m")
        assert fh.primary_source == "rtds_chainlink"
        assert fh.price == pytest.approx(89_000.0)

    def test_chainlink_market_falls_to_cl_ws_last_resort(self):
        oracle = _make_oracle(
            streams_snap=None,
            rtds_chainlink_snap=None,
            cl_ws_snap=_snap("BTC", 88_000.0, age_secs=1.0),
        )
        fh = oracle.get_feed_health("BTC", "bucket_4h")
        assert fh.primary_source == "chainlink_ws"
        assert fh.price == pytest.approx(88_000.0)

    def test_non_chainlink_market_uses_rtds_exchange(self):
        rtds = _snap("BTC", 87_000.0, age_secs=0.3)
        oracle = _make_oracle(rtds_snap=rtds)
        fh = oracle.get_feed_health("BTC", "bucket_1h")
        assert fh.primary_source == "rtds"
        assert fh.price == pytest.approx(87_000.0)

    def test_non_chainlink_does_not_consult_streams_or_cl_ws(self):
        rtds = _snap("ETH", 3_500.0, age_secs=0.3)
        oracle = _make_oracle(rtds_snap=rtds)
        fh = oracle.get_feed_health("ETH", "bucket_daily")
        # Streams and CL WS must not be queried for non-Chainlink markets
        oracle._streams.get_spot.assert_not_called()
        oracle._cl.get_spot.assert_not_called()

    def test_all_chainlink_market_types_resolved_via_streams(self):
        for mtype in CHAINLINK_MARKET_TYPES:
            streams_snap = _snap("XRP", 0.55, age_secs=0.1)
            oracle = _make_oracle(streams_snap=streams_snap)
            fh = oracle.get_feed_health("XRP", mtype)
            assert fh.primary_source == "chainlink_streams", f"failed for {mtype}"
            assert fh.coin == "XRP"


class TestGetFeedHealthStatus:
    """HEALTHY / STALE / DOWN classification."""

    def setup_method(self):
        config.MOMENTUM_SPOT_MAX_AGE_SECS = 30

    def test_healthy_when_age_below_threshold(self):
        oracle = _make_oracle(streams_snap=_snap("BTC", 90_000.0, age_secs=5.0))
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.status == "HEALTHY"
        assert fh.age_secs is not None
        assert fh.age_secs < config.MOMENTUM_SPOT_MAX_AGE_SECS

    def test_stale_when_age_at_or_above_threshold(self):
        oracle = _make_oracle(streams_snap=_snap("BTC", 90_000.0, age_secs=35.0))
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.status == "STALE"
        assert fh.age_secs >= config.MOMENTUM_SPOT_MAX_AGE_SECS

    def test_down_when_no_price_available(self):
        oracle = _make_oracle(
            streams_snap=None,
            rtds_chainlink_snap=None,
            cl_ws_snap=None,
        )
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.status == "DOWN"
        assert fh.price is None
        assert fh.age_secs is None

    def test_down_for_rtds_market_with_no_data(self):
        oracle = _make_oracle(rtds_snap=None)
        fh = oracle.get_feed_health("ETH", "bucket_1h")
        assert fh.status == "DOWN"

    def test_age_secs_rounded_to_two_decimal_places(self):
        oracle = _make_oracle(streams_snap=_snap("BTC", 90_000.0, age_secs=10.0))
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.age_secs is not None
        # round(..., 2) applied — verify it has at most 2 decimal places
        assert round(fh.age_secs, 2) == fh.age_secs


class TestGetFeedHealthReconnectMetadata:
    """Chainlink reconnect count and timestamp pass-through."""

    def test_chainlink_market_carries_reconnect_count(self):
        oracle = _make_oracle(
            streams_snap=_snap("BTC", 90_000.0),
            streams_reconnects_1h=7,
            streams_last_reconnect_at=1_000_000.0,
        )
        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.reconnect_count_1h == 7
        assert fh.last_reconnect_at == pytest.approx(1_000_000.0)

    def test_non_chainlink_market_reconnect_fields_are_zero(self):
        oracle = _make_oracle(
            rtds_snap=_snap("BTC", 90_000.0),
            streams_reconnects_1h=99,  # should be ignored for non-CL market
            streams_last_reconnect_at=1_000_000.0,
        )
        fh = oracle.get_feed_health("BTC", "bucket_daily")
        assert fh.reconnect_count_1h == 0
        assert fh.last_reconnect_at == 0.0

    def test_no_streams_client_reconnect_fields_are_zero(self):
        # Build oracle with streams=None
        rtds = MagicMock()
        rtds.get_chainlink_spot = MagicMock(return_value=_snap("BTC", 90_000.0))
        rtds.get_spot = MagicMock(return_value=None)
        rtds.get_spot_age = MagicMock(return_value=0.5)
        rtds.on_chainlink_update = MagicMock()
        rtds.on_price_update = MagicMock()
        rtds.all_mids = MagicMock(return_value={})
        cl = MagicMock()
        cl.get_spot = MagicMock(return_value=None)
        cl.on_price_update = MagicMock()
        oracle = SpotOracle(rtds=rtds, chainlink=cl, streams=None)

        fh = oracle.get_feed_health("BTC", "bucket_5m")
        assert fh.reconnect_count_1h == 0
        assert fh.last_reconnect_at == 0.0


class TestGetFeedHealthReturnShape:
    """FeedHealth dataclass field completeness."""

    def test_all_fields_present_healthy(self):
        oracle = _make_oracle(streams_snap=_snap("ETH", 3_500.0, age_secs=1.0))
        fh = oracle.get_feed_health("ETH", "bucket_15m")
        assert isinstance(fh, FeedHealth)
        assert fh.coin == "ETH"
        assert fh.price == pytest.approx(3_500.0, rel=1e-4)
        assert fh.status == "HEALTHY"
        assert fh.primary_source is not None
        assert fh.checked_at > 0  # auto-set by dataclass default_factory

    def test_down_feed_has_none_price_and_age(self):
        oracle = _make_oracle(streams_snap=None, rtds_chainlink_snap=None, cl_ws_snap=None)
        fh = oracle.get_feed_health("SOL", "bucket_5m")
        assert fh.price is None
        assert fh.age_secs is None


class TestGetFeedHealthTransitionLogging:
    """Status-transition log events fire exactly once per direction."""

    def _make_oracle_with_snap(self, age_secs: float) -> SpotOracle:
        return _make_oracle(streams_snap=_snap("BTC", 90_000.0, age_secs=age_secs))

    def test_stale_transition_logs_warning_once(self):
        config.MOMENTUM_SPOT_MAX_AGE_SECS = 30
        oracle = self._make_oracle_with_snap(age_secs=0.5)
        # First call: HEALTHY — no warning
        oracle.get_feed_health("BTC", "bucket_5m")

        # Simulate stale snapshot
        oracle._streams.get_spot = MagicMock(
            return_value=_snap("BTC", 90_000.0, age_secs=40.0)
        )
        with patch("market_data.spot_oracle.SpotOracle.get_feed_health") as _patched:
            # Don't patch the real method — just verify the cache logic directly
            pass

        # Use a real logger mock to count warning calls
        with patch("logger.get_bot_logger") as _mock_log_factory:
            _mock_log = MagicMock()
            _mock_log_factory.return_value = _mock_log
            # Re-create oracle so the patched logger is used
            oracle2 = self._make_oracle_with_snap(age_secs=0.5)
            oracle2.get_feed_health("BTC", "bucket_5m")   # → HEALTHY, no warning
            oracle2._streams.get_spot = MagicMock(
                return_value=_snap("BTC", 90_000.0, age_secs=40.0)
            )
            oracle2.get_feed_health("BTC", "bucket_5m")   # → STALE, warning fires
            oracle2.get_feed_health("BTC", "bucket_5m")   # → STALE again, no new warning
            warning_calls = [c for c in _mock_log.warning.call_args_list
                             if c.args and "oracle_feed_degraded" in str(c.args[0])]
            assert len(warning_calls) == 1, (
                f"Expected exactly 1 oracle_feed_degraded warning, got {len(warning_calls)}"
            )

    def test_recovery_logs_info_once(self):
        config.MOMENTUM_SPOT_MAX_AGE_SECS = 30
        with patch("logger.get_bot_logger") as _mock_log_factory:
            _mock_log = MagicMock()
            _mock_log_factory.return_value = _mock_log
            oracle = self._make_oracle_with_snap(age_secs=40.0)  # starts STALE
            oracle.get_feed_health("BTC", "bucket_5m")            # STALE (first time → warning)
            oracle._streams.get_spot = MagicMock(
                return_value=_snap("BTC", 90_000.0, age_secs=0.5)
            )
            oracle.get_feed_health("BTC", "bucket_5m")  # → HEALTHY (recovery → info)
            oracle.get_feed_health("BTC", "bucket_5m")  # → HEALTHY (no new info)
            recovery_calls = [c for c in _mock_log.info.call_args_list
                              if c.args and "oracle_feed_recovered" in str(c.args[0])]
            assert len(recovery_calls) == 1, (
                f"Expected exactly 1 oracle_feed_recovered info, got {len(recovery_calls)}"
            )

    def test_first_call_healthy_does_not_log(self):
        config.MOMENTUM_SPOT_MAX_AGE_SECS = 30
        with patch("logger.get_bot_logger") as _mock_log_factory:
            _mock_log = MagicMock()
            _mock_log_factory.return_value = _mock_log
            oracle = self._make_oracle_with_snap(age_secs=0.5)
            oracle.get_feed_health("BTC", "bucket_5m")
            _mock_log.warning.assert_not_called()
            _mock_log.info.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# S3.2 — _WSShard.health + PMClient.get_shard_health
# ═════════════════════════════════════════════════════════════════════════════

class TestWSShardHealth:
    """_WSShard.health property state machine."""

    def _make_shard(self) -> _WSShard:
        shard = _WSShard(shard_id=0, on_message=AsyncMock())
        return shard

    def test_disconnected_when_not_running(self):
        shard = self._make_shard()
        assert shard._running is False
        assert shard.health == "DISCONNECTED"

    def test_connecting_when_running_but_not_connected(self):
        shard = self._make_shard()
        shard._running = True
        shard.connected = False
        assert shard.health == "CONNECTING"

    def test_connected_when_running_connected_with_recent_message(self):
        config.HL_WS_STALE_SECS = 30
        shard = self._make_shard()
        shard._running = True
        shard.connected = True
        shard._last_message_at = time.time() - 5.0   # 5 s ago — well within threshold
        assert shard.health == "CONNECTED"

    def test_degraded_when_running_connected_but_silent_too_long(self):
        config.HL_WS_STALE_SECS = 30
        shard = self._make_shard()
        shard._running = True
        shard.connected = True
        shard._last_message_at = time.time() - 60.0  # 60 s — exceeds 30s threshold
        assert shard.health == "DEGRADED"

    def test_connected_when_running_connected_no_message_yet(self):
        """No message ever received (_last_message_at == 0.0) → CONNECTED not DEGRADED."""
        config.HL_WS_STALE_SECS = 30
        shard = self._make_shard()
        shard._running = True
        shard.connected = True
        shard._last_message_at = 0.0
        assert shard.health == "CONNECTED"

    def test_degraded_uses_hl_ws_stale_secs_threshold(self):
        """Threshold is read from config.HL_WS_STALE_SECS, not hardcoded."""
        config.HL_WS_STALE_SECS = 10
        shard = self._make_shard()
        shard._running = True
        shard.connected = True
        shard._last_message_at = time.time() - 15.0  # > 10s custom threshold
        assert shard.health == "DEGRADED"

    def test_message_count_and_last_message_at_initialise_to_defaults(self):
        shard = self._make_shard()
        assert shard._message_count == 0
        assert shard._last_message_at == 0.0


class TestPMClientGetShardHealth:
    """PMClient.get_shard_health() assembles ShardHealth objects correctly."""

    def _make_pm_client(self) -> object:
        """Create a bare PMClient instance without triggering network code."""
        from pm_client import PMClient
        client = PMClient.__new__(PMClient)
        client._shards = {}
        return client

    def test_empty_when_no_shards(self):
        client = self._make_pm_client()
        result = client.get_shard_health()
        assert result == []

    def test_returns_one_entry_per_shard(self):
        client = self._make_pm_client()
        for sid in (0, 1, 2):
            s = _WSShard(shard_id=sid, on_message=AsyncMock())
            s._running = True
            s.connected = True
            s._last_message_at = time.time() - 1.0
            s._message_count = 42
            client._shards[sid] = s
        result = client.get_shard_health()
        assert len(result) == 3
        ids = {sh.shard_id for sh in result}
        assert ids == {0, 1, 2}

    def test_each_entry_is_shard_health_dataclass(self):
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = time.time() - 2.0
        s._message_count = 10
        client._shards[0] = s
        result = client.get_shard_health()
        sh = result[0]
        assert isinstance(sh, ShardHealth)

    def test_last_message_age_secs_none_before_first_message(self):
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = 0.0   # never received a message
        s._message_count = 0
        client._shards[0] = s
        result = client.get_shard_health()
        assert result[0].last_message_age_secs is None

    def test_last_message_age_secs_is_positive_after_message(self):
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = time.time() - 5.0
        s._message_count = 1
        client._shards[0] = s
        result = client.get_shard_health()
        age = result[0].last_message_age_secs
        assert age is not None
        assert age >= 4.9  # at least ~5 s ago (allows for tiny CI timing skew)

    def test_message_count_matches_shard_counter(self):
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = time.time() - 1.0
        s._message_count = 77
        client._shards[0] = s
        result = client.get_shard_health()
        assert result[0].message_count == 77

    def test_token_count_matches_subscribed_count(self):
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = time.time() - 1.0
        s._message_count = 0
        # Directly populate _tokens to control subscribed_count
        s._tokens = {"tok_a", "tok_b", "tok_c"}
        client._shards[0] = s
        result = client.get_shard_health()
        assert result[0].token_count == 3

    def test_health_field_matches_shard_health_property(self):
        config.HL_WS_STALE_SECS = 30
        client = self._make_pm_client()
        s = _WSShard(shard_id=0, on_message=AsyncMock())
        s._running = True
        s.connected = True
        s._last_message_at = time.time() - 60.0  # DEGRADED
        s._message_count = 5
        client._shards[0] = s
        result = client.get_shard_health()
        assert result[0].health == "DEGRADED"


# ═════════════════════════════════════════════════════════════════════════════
# S3.3 — ChainlinkStreamsClient reconnect rate tracking
# ═════════════════════════════════════════════════════════════════════════════

class TestChainlinkStreamsClientReconnectTracking:
    """Rolling reconnect count and last_reconnect_at timestamp."""

    def _make_client(self) -> ChainlinkStreamsClient:
        client = ChainlinkStreamsClient.__new__(ChainlinkStreamsClient)
        client._reconnect_timestamps = []
        client._last_reconnect_at = 0.0
        # Minimum required attributes to avoid AttributeError in properties
        return client

    def test_reconnects_1h_zero_when_no_history(self):
        client = self._make_client()
        assert client.reconnects_1h == 0

    def test_reconnects_1h_counts_recent_timestamps(self):
        client = self._make_client()
        now = time.time()
        client._reconnect_timestamps = [now - 100, now - 200, now - 300]
        assert client.reconnects_1h == 3

    def test_reconnects_1h_excludes_timestamps_older_than_1h(self):
        client = self._make_client()
        now = time.time()
        old = now - 3700  # older than 3600 s
        recent = now - 50
        client._reconnect_timestamps = [old, old, recent]
        # Only the 1 recent entry should count
        assert client.reconnects_1h == 1

    def test_reconnects_1h_empty_after_all_expire(self):
        client = self._make_client()
        now = time.time()
        client._reconnect_timestamps = [now - 4000, now - 5000]
        assert client.reconnects_1h == 0

    def test_last_reconnect_at_zero_initially(self):
        client = self._make_client()
        assert client.last_reconnect_at == 0.0

    def test_last_reconnect_at_reflects_stored_value(self):
        client = self._make_client()
        expected = time.time() - 120.0
        client._last_reconnect_at = expected
        assert client.last_reconnect_at == pytest.approx(expected)

    def test_reconnects_1h_boundary_exactly_3600s_is_excluded(self):
        """A timestamp exactly at cutoff (>= cutoff only) must be handled correctly."""
        client = self._make_client()
        now = time.time()
        # Exactly 3600 s ago — the property filters `t >= cutoff` (cutoff = now - 3600)
        boundary = now - 3600.0
        client._reconnect_timestamps = [boundary - 1, boundary + 1]
        # boundary-1 is just outside (older), boundary+1 is just inside
        assert client.reconnects_1h == 1


# ═════════════════════════════════════════════════════════════════════════════
# S3.4 — HLClient.is_connected / get_mark_price_age
# ═════════════════════════════════════════════════════════════════════════════

class TestHLClientHealthAccessors:
    """is_connected() and get_mark_price_age() reflect internal state."""

    def _make_client(self) -> HLClient:
        client = HLClient.__new__(HLClient)
        client._ws_connected = False
        client._fundings = {}
        client._bbo = {}
        client._mids = {}
        client._bbo_callbacks = []
        return client

    def test_is_connected_false_by_default(self):
        client = self._make_client()
        assert client.is_connected() is False

    def test_is_connected_true_when_flag_set(self):
        client = self._make_client()
        client._ws_connected = True
        assert client.is_connected() is True

    def test_get_mark_price_age_none_for_unknown_coin(self):
        client = self._make_client()
        assert client.get_mark_price_age("BTC") is None

    def test_get_mark_price_age_returns_elapsed_seconds(self):
        client = self._make_client()
        ts = time.time() - 10.0
        client._fundings["BTC"] = FundingSnapshot(coin="BTC", timestamp=ts)
        age = client.get_mark_price_age("BTC")
        assert age is not None
        assert 9.0 <= age <= 11.0   # ~10 s with some test-clock tolerance

    def test_get_mark_price_age_fresh_data_near_zero(self):
        client = self._make_client()
        client._fundings["ETH"] = FundingSnapshot(coin="ETH", timestamp=time.time())
        age = client.get_mark_price_age("ETH")
        assert age is not None
        assert age < 1.0


# ═════════════════════════════════════════════════════════════════════════════
# S3.4 — PositionMonitor HL stale logging
# ═════════════════════════════════════════════════════════════════════════════

class TestMonitorHLStaleness:
    """_manage_open_hedge_orders logs [HL_DEGRADED] correctly."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_hedge_order(self, order_id: str, coin: str) -> MagicMock:
        ho = MagicMock()
        ho.order_id = order_id
        ho.underlying = coin
        ho.market_id = "mkt_001"
        ho.token_id = "tok_yes_001"
        ho.last_clob_ask = None
        return ho

    def _make_monitor(self, hl_client=None):
        from monitor import PositionMonitor
        pm = MagicMock()
        pm._markets = {}
        pm._books = {}
        risk = MagicMock()
        risk.get_open_hedge_orders.return_value = []
        monitor = PositionMonitor.__new__(PositionMonitor)
        monitor._pm = pm
        monitor._risk = risk
        monitor._hl = hl_client
        return monitor

    def test_no_crash_when_hl_client_none(self):
        monitor = self._make_monitor(hl_client=None)
        monitor._risk.get_open_hedge_orders.return_value = []
        self._run(monitor._manage_open_hedge_orders())

    def test_hl_degraded_logged_when_age_exceeds_threshold(self):
        config.HL_WS_STALE_SECS = 30
        hl = MagicMock()
        hl.get_mark_price_age.return_value = 45.0   # stale
        monitor = self._make_monitor(hl_client=hl)
        monitor._risk.get_open_hedge_orders.return_value = [
            self._make_hedge_order("ord_abc123", "BTC")
        ]

        with patch("monitor.log") as mock_log:
            self._run(monitor._manage_open_hedge_orders())
            warning_calls = [c for c in mock_log.warning.call_args_list
                             if c.args and "[HL_DEGRADED]" in str(c.args[0])]
            assert len(warning_calls) == 1
            call_kwargs = warning_calls[0].kwargs
            assert call_kwargs["coin"] == "BTC"
            assert call_kwargs["mark_price_age_secs"] == pytest.approx(45.0, rel=1e-2)

    def test_hl_degraded_not_logged_when_age_within_threshold(self):
        config.HL_WS_STALE_SECS = 30
        hl = MagicMock()
        hl.get_mark_price_age.return_value = 5.0   # fresh
        monitor = self._make_monitor(hl_client=hl)
        monitor._risk.get_open_hedge_orders.return_value = [
            self._make_hedge_order("ord_xyz789", "ETH")
        ]

        with patch("monitor.log") as mock_log:
            self._run(monitor._manage_open_hedge_orders())
            warning_calls = [c for c in mock_log.warning.call_args_list
                             if c.args and "[HL_DEGRADED]" in str(c.args[0])]
            assert len(warning_calls) == 0

    def test_hl_degraded_deduped_per_coin(self):
        """Two hedge orders on same coin must fire only one [HL_DEGRADED] log per sweep."""
        config.HL_WS_STALE_SECS = 30
        hl = MagicMock()
        hl.get_mark_price_age.return_value = 60.0   # stale
        monitor = self._make_monitor(hl_client=hl)
        monitor._risk.get_open_hedge_orders.return_value = [
            self._make_hedge_order("ord_1", "BTC"),
            self._make_hedge_order("ord_2", "BTC"),  # same coin, second order
        ]

        with patch("monitor.log") as mock_log:
            self._run(monitor._manage_open_hedge_orders())
            warning_calls = [c for c in mock_log.warning.call_args_list
                             if c.args and "[HL_DEGRADED]" in str(c.args[0])]
            assert len(warning_calls) == 1, (
                "Same coin with two hedge orders must not double-log [HL_DEGRADED]"
            )

    def test_hl_degraded_logged_separately_for_different_coins(self):
        config.HL_WS_STALE_SECS = 30
        hl = MagicMock()
        hl.get_mark_price_age.return_value = 60.0   # stale for all coins
        monitor = self._make_monitor(hl_client=hl)
        monitor._risk.get_open_hedge_orders.return_value = [
            self._make_hedge_order("ord_1", "BTC"),
            self._make_hedge_order("ord_2", "ETH"),  # different coin
        ]

        with patch("monitor.log") as mock_log:
            self._run(monitor._manage_open_hedge_orders())
            warning_calls = [c for c in mock_log.warning.call_args_list
                             if c.args and "[HL_DEGRADED]" in str(c.args[0])]
            coins_logged = {c.kwargs["coin"] for c in warning_calls}
            assert coins_logged == {"BTC", "ETH"}

    def test_hl_degraded_not_logged_when_age_is_none(self):
        """get_mark_price_age returns None (coin never seen) — no [HL_DEGRADED] log."""
        config.HL_WS_STALE_SECS = 30
        hl = MagicMock()
        hl.get_mark_price_age.return_value = None
        monitor = self._make_monitor(hl_client=hl)
        monitor._risk.get_open_hedge_orders.return_value = [
            self._make_hedge_order("ord_1", "SOL")
        ]

        with patch("monitor.log") as mock_log:
            self._run(monitor._manage_open_hedge_orders())
            warning_calls = [c for c in mock_log.warning.call_args_list
                             if c.args and "[HL_DEGRADED]" in str(c.args[0])]
            assert len(warning_calls) == 0


# ═════════════════════════════════════════════════════════════════════════════
# S3 Dataclasses — ShardHealth & FeedHealth field completeness
# ═════════════════════════════════════════════════════════════════════════════

class TestS3Dataclasses:
    """Verify the core.types dataclasses have the spec-required fields."""

    def test_feed_health_fields(self):
        fh = FeedHealth(
            coin="BTC",
            primary_source="chainlink_streams",
            price=90_000.0,
            age_secs=0.3,
            status="HEALTHY",
            last_reconnect_at=0.0,
            reconnect_count_1h=0,
        )
        assert fh.coin == "BTC"
        assert fh.primary_source == "chainlink_streams"
        assert fh.price == pytest.approx(90_000.0)
        assert fh.age_secs == pytest.approx(0.3)
        assert fh.status == "HEALTHY"
        assert fh.last_reconnect_at == 0.0
        assert fh.reconnect_count_1h == 0
        assert fh.checked_at > 0  # auto-populated

    def test_shard_health_fields(self):
        sh = ShardHealth(
            shard_id=3,
            health="CONNECTED",
            token_count=50,
            last_message_age_secs=1.5,
            message_count=10_000,
        )
        assert sh.shard_id == 3
        assert sh.health == "CONNECTED"
        assert sh.token_count == 50
        assert sh.last_message_age_secs == pytest.approx(1.5)
        assert sh.message_count == 10_000
        assert sh.checked_at > 0

    def test_shard_health_last_message_age_secs_can_be_none(self):
        sh = ShardHealth(
            shard_id=0,
            health="CONNECTING",
            token_count=0,
            last_message_age_secs=None,
            message_count=0,
        )
        assert sh.last_message_age_secs is None
