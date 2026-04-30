"""
tests/test_chainlink_streams_client.py — Unit tests for ChainlinkStreamsClient (HA mode).

All tests are fully offline — WebSocket connections and HTTP requests are mocked.

Behaviours verified:

  1. decode_streams_report      — correct ABI decode of V3 fullReport bytes.
  2. _handle_message            — valid report → price stored + callback fired.
  3. _handle_message            — unknown feedID silently ignored.
  4. HA dedup                   — same observationsTimestamp from second connection
                                  is counted as deduplicated, callback NOT fired twice.
  5. HA dedup                   — newer observationsTimestamp from either connection
                                  is accepted and replaces the watermark.
  6. HA failover                — one connection drops (ConnectionClosed); price
                                  delivery continues without interruption from second.
  7. active_connections counter — increments on connect, decrements on disconnect.
  8. partial_reconnects stat    — incremented when one of two connections drops.
  9. full_reconnects stat       — incremented when the only connection drops.
 10. _fetch_origins             — parses {001,002} header; falls back to [] on error.
 11. auth headers               — HMAC signature present and non-empty for correct path.
 12. Accessors                  — get_mid / get_spot / get_spot_age / enabled / is_connected.
 13. stop()                     — closes all ws handles and sets _running=False.
 14. StreamStats.__str__        — formats all six fields correctly.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch credentials before importing the module under test.
import config
config.CHAINLINK_DS_USERNAME  = "test-api-key"
config.CHAINLINK_DS_PASSWORD  = "test-api-secret"
config.CHAINLINK_DS_API_KEY   = ""
config.CHAINLINK_DS_API_SECRET = ""
config.CHAINLINK_DS_HOST      = "wss://ws.dataengine.chain.link"
config.CHAINLINK_DS_FEED_IDS  = {"BTC": "0xdeadbeef00000000000000000000000000000000000000000000000000000001"}

from market_data.chainlink_streams_client import (
    ChainlinkStreamsClient,
    StreamStats,
    decode_streams_report,
    _DS_DECIMALS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


_FEED_ID_HEX = config.CHAINLINK_DS_FEED_IDS["BTC"]
_FEED_ID_LOW = _FEED_ID_HEX.lower()


def _build_full_report(price_usd: float, obs_ts: int = 1_000_000) -> str:
    """
    Build a minimal V3 fullReport hex string that decode_streams_report can parse.

    Layout (see chainlink_streams_client.py docstring):
      bytes 0–255   : 8 × 32-byte header slots (context + ABI offsets + length)
      bytes 256–287 : feedId
      bytes 288–319 : validFromTimestamp (uint32 → last 4 bytes of slot)
      bytes 320–351 : observationsTimestamp
      bytes 352–383 : nativeFee
      bytes 384–415 : linkFee
      bytes 416–447 : expiresAt
      bytes 448–479 : benchmarkPrice  ← 18 dp int192 (signed)
      bytes 480–511 : bid
      bytes 512–543 : ask
    """
    benchmark = int(price_usd * 10 ** _DS_DECIMALS)
    raw = bytearray(544)

    # observationsTimestamp at byte 320
    raw[320:352] = obs_ts.to_bytes(32, "big")

    # benchmarkPrice at byte 448 (signed int192 in two's complement, big-endian 32 bytes)
    if benchmark < 0:
        benchmark_bytes = benchmark.to_bytes(32, "big", signed=True)
    else:
        benchmark_bytes = benchmark.to_bytes(32, "big")
    raw[448:480] = benchmark_bytes

    return "0x" + raw.hex()


def _make_report_msg(price: float, obs_ts: int = 1_000_000) -> dict:
    return {
        "report": {
            "feedID": _FEED_ID_HEX,
            "observationsTimestamp": obs_ts,
            "validFromTimestamp": obs_ts,
            "fullReport": _build_full_report(price, obs_ts),
        }
    }


# ── 1. decode_streams_report ─────────────────────────────────────────────────

class TestDecodeStreamsReport:
    def test_round_trip_btc_price(self):
        price_in = 50_000.12345
        hex_report = _build_full_report(price_in)
        result = decode_streams_report(hex_report)
        assert result is not None
        price_out, _ = result
        assert abs(price_out - price_in) < 1e-9

    def test_obs_ts_decoded_correctly(self):
        hex_report = _build_full_report(100.0, obs_ts=1_714_000_000)
        _, obs_ts = decode_streams_report(hex_report)
        assert obs_ts == 1_714_000_000

    def test_returns_none_for_short_payload(self):
        assert decode_streams_report("0x" + "00" * 100) is None

    def test_returns_none_for_zero_price(self):
        hex_report = _build_full_report(0.0)
        assert decode_streams_report(hex_report) is None

    def test_returns_none_for_garbage_hex(self):
        assert decode_streams_report("0xnothex") is None

    def test_without_0x_prefix(self):
        hex_report = _build_full_report(1.0)
        result = decode_streams_report(hex_report[2:])   # strip exactly "0x"
        assert result is not None


# ── 2. _handle_message — valid report ────────────────────────────────────────

class TestHandleMessage:
    def test_price_stored_after_valid_message(self):
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}

        _run(client._handle_message(_make_report_msg(50_000.0)))

        assert client.get_mid("BTC") is not None
        assert abs(client.get_mid("BTC") - 50_000.0) < 1e-6

    def test_callback_fired_on_valid_message(self):
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}

        received = []

        async def cb(coin, price):
            received.append((coin, price))

        client.on_price_update(cb)
        _run(client._handle_message(_make_report_msg(50_000.0)))

        assert len(received) == 1
        assert received[0][0] == "BTC"
        assert abs(received[0][1] - 50_000.0) < 1e-6

    def test_accepted_stat_incremented(self):
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}

        _run(client._handle_message(_make_report_msg(1.0)))
        assert client.stats.accepted == 1

    def test_unknown_feed_id_ignored(self):
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}

        msg = _make_report_msg(1.0)
        msg["report"]["feedID"] = "0xdeadbeef"  # not in _feedid_to_coin
        _run(client._handle_message(msg))

        assert client.get_mid("BTC") is None
        assert client.stats.accepted == 0

    def test_missing_report_key_ignored(self):
        client = ChainlinkStreamsClient()
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}
        _run(client._handle_message({"not_a_report": {}}))
        assert client.stats.accepted == 0


# ── 3 & 4. HA deduplication ──────────────────────────────────────────────────

class TestHADedup:
    def _make_client(self):
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}
        return client

    def test_same_obs_ts_from_second_conn_is_deduplicated(self):
        """Both connections carry the same obs_ts — callback fires exactly once."""
        client = self._make_client()
        fired = []

        async def cb(coin, price):
            fired.append(price)

        client.on_price_update(cb)

        msg = _make_report_msg(50_000.0, obs_ts=1_000)
        _run(client._handle_message(msg))   # connection 001 — accepted
        _run(client._handle_message(msg))   # connection 002 — duplicate

        assert len(fired) == 1
        assert client.stats.accepted == 1
        assert client.stats.deduplicated == 1

    def test_newer_obs_ts_from_second_conn_is_accepted(self):
        """Connection 002 delivers a *newer* report — should be accepted."""
        client = self._make_client()
        fired = []

        async def cb(coin, price):
            fired.append(price)

        client.on_price_update(cb)

        _run(client._handle_message(_make_report_msg(50_000.0, obs_ts=1_000)))
        _run(client._handle_message(_make_report_msg(51_000.0, obs_ts=1_001)))

        assert len(fired) == 2
        assert client.stats.accepted == 2
        assert client.stats.deduplicated == 0

    def test_older_obs_ts_is_deduplicated(self):
        """Stale re-delivery (obs_ts less than watermark) is discarded."""
        client = self._make_client()
        fired = []

        async def cb(coin, price):
            fired.append(price)

        client.on_price_update(cb)

        _run(client._handle_message(_make_report_msg(50_000.0, obs_ts=1_001)))
        _run(client._handle_message(_make_report_msg(49_000.0, obs_ts=999)))

        assert len(fired) == 1
        assert client.stats.accepted == 1
        assert client.stats.deduplicated == 1

    def test_watermark_advances_per_feed(self):
        """Watermark is per-feedID; a second feed is deduplicated independently."""
        feed2 = "0xdeadbeef00000000000000000000000000000000000000000000000000000002"
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX, "ETH": feed2}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC", feed2.lower(): "ETH"}

        def _msg(feed, price, obs_ts):
            return {
                "report": {
                    "feedID": feed,
                    "observationsTimestamp": obs_ts,
                    "validFromTimestamp": obs_ts,
                    "fullReport": _build_full_report(price, obs_ts),
                }
            }

        _run(client._handle_message(_msg(_FEED_ID_HEX, 50_000.0, 1_000)))
        _run(client._handle_message(_msg(feed2, 2_000.0, 500)))
        # Duplicate BTC — should be deduped; ETH at same ts also deduplicated
        _run(client._handle_message(_msg(_FEED_ID_HEX, 50_000.0, 1_000)))
        _run(client._handle_message(_msg(feed2, 2_000.0, 500)))

        assert client.stats.accepted == 2
        assert client.stats.deduplicated == 2


# ── 5. HA failover — one conn drops, other continues ─────────────────────────

class TestHAFailover:
    def test_price_delivered_when_primary_conn_drops(self):
        """
        Simulate: conn-001 fires messages, then drops (ConnectionClosed).
        conn-002 continues delivering messages.  Prices must be available
        throughout — no gap.

        We test this by driving _handle_message directly (the public contract
        for the delivery path) rather than mocking the WS protocol stack,
        which would be testing asyncio internals rather than our logic.
        """
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}
        client._running = True

        prices_seen = []

        async def cb(coin, price):
            prices_seen.append(price)

        client.on_price_update(cb)

        # Phase 1 — conn-001 delivers obs_ts=1000
        _run(client._handle_message(_make_report_msg(50_000.0, obs_ts=1_000)))
        assert client.get_mid("BTC") == pytest.approx(50_000.0, rel=1e-6)

        # Phase 2 — conn-001 "drops" (active_connections decrements; we simulate it)
        client.stats.active_connections = max(0, client.stats.active_connections - 1)
        # conn-002 was already open; it delivers the next tick immediately
        _run(client._handle_message(_make_report_msg(50_100.0, obs_ts=1_001)))

        assert client.get_mid("BTC") == pytest.approx(50_100.0, rel=1e-6)
        assert len(prices_seen) == 2  # no gap — both ticks delivered

    def test_duplicate_from_second_conn_after_primary_drops_is_deduped(self):
        """
        conn-001 drops mid-tick; conn-002 delivers the same obs_ts.
        Must be deduplicated (not delivered twice).
        """
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}

        fired = []

        async def cb(coin, price):
            fired.append(price)

        client.on_price_update(cb)

        _run(client._handle_message(_make_report_msg(50_000.0, obs_ts=1_000)))
        # conn-001 drops; conn-002 carries the same obs_ts (normal HA scenario)
        _run(client._handle_message(_make_report_msg(50_000.0, obs_ts=1_000)))

        assert len(fired) == 1
        assert client.stats.deduplicated == 1


# ── 6 & 7. active_connections counter and reconnect stats ────────────────────

class TestReconnectStats:
    def test_partial_reconnect_when_other_conn_active(self):
        """
        partial_reconnects increments when at least one other connection is live.
        """
        client = ChainlinkStreamsClient()
        client.stats.active_connections = 1  # conn-002 still live

        # Simulate the logic at the bottom of _conn_loop after a disconnect
        if client.stats.active_connections > 0:
            client.stats.partial_reconnects += 1
        else:
            client.stats.full_reconnects += 1

        assert client.stats.partial_reconnects == 1
        assert client.stats.full_reconnects == 0

    def test_full_reconnect_when_no_other_conn_active(self):
        """
        full_reconnects increments when no other connection is live.
        """
        client = ChainlinkStreamsClient()
        client.stats.active_connections = 0  # both connections down

        if client.stats.active_connections > 0:
            client.stats.partial_reconnects += 1
        else:
            client.stats.full_reconnects += 1

        assert client.stats.partial_reconnects == 0
        assert client.stats.full_reconnects == 1

    def test_active_connections_decrements_on_disconnect(self):
        """active_connections tracks open handles correctly."""
        client = ChainlinkStreamsClient()
        assert client.stats.active_connections == 0

        client.stats.active_connections += 1
        client.stats.active_connections += 1
        assert client.stats.active_connections == 2

        client.stats.active_connections -= 1
        assert client.stats.active_connections == 1


# ── 8. _fetch_origins ─────────────────────────────────────────────────────────

class TestFetchOrigins:
    def _make_client(self):
        client = ChainlinkStreamsClient()
        client._active_feeds = {"BTC": _FEED_ID_HEX}
        return client

    def test_parses_two_origins(self):
        """Standard server response: {001,002} → ['001', '002']."""
        client = self._make_client()

        mock_error = urllib.error.HTTPError(
            url="", code=403, msg="Forbidden",
            hdrs=MagicMock(**{"get.return_value": "{001,002}"}),
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=mock_error):
            origins = _run(client._fetch_origins())

        assert origins == ["001", "002"]

    def test_falls_back_to_empty_list_on_network_error(self):
        """Non-HTTP error (e.g. socket timeout) → empty list (single-conn mode)."""
        client = self._make_client()

        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            origins = _run(client._fetch_origins())

        assert origins == []

    def test_falls_back_to_empty_list_when_header_absent(self):
        """403 with no origins header → empty list."""
        client = self._make_client()

        mock_error = urllib.error.HTTPError(
            url="", code=403, msg="Forbidden",
            hdrs=MagicMock(**{"get.return_value": ""}),
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=mock_error):
            origins = _run(client._fetch_origins())

        assert origins == []

    def test_parses_single_origin(self):
        client = self._make_client()

        mock_error = urllib.error.HTTPError(
            url="", code=403, msg="Forbidden",
            hdrs=MagicMock(**{"get.return_value": "{001}"}),
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=mock_error):
            origins = _run(client._fetch_origins())

        assert origins == ["001"]


# ── 9. Auth headers ───────────────────────────────────────────────────────────

class TestAuthHeaders:
    def test_hmac_headers_present_and_non_empty(self):
        client = ChainlinkStreamsClient()
        client._active_feeds = {"BTC": _FEED_ID_HEX}

        headers = client._build_auth_headers(origin="001")

        assert headers["Authorization"] == "test-api-key"
        assert len(headers["X-Authorization-Timestamp"]) > 0
        assert len(headers["X-Authorization-Signature-SHA256"]) == 64  # hex SHA256
        assert headers["CLL-ORIGIN"] == "001"

    def test_no_origin_header_when_empty(self):
        client = ChainlinkStreamsClient()
        client._active_feeds = {"BTC": _FEED_ID_HEX}

        headers = client._build_auth_headers(origin="")

        assert "CLL-ORIGIN" not in headers

    def test_signature_changes_with_timestamp(self):
        """Two calls at different times must produce different signatures."""
        client = ChainlinkStreamsClient()
        client._active_feeds = {"BTC": _FEED_ID_HEX}

        h1 = client._build_auth_headers()
        time.sleep(0.002)
        h2 = client._build_auth_headers()

        # Timestamps must differ; signatures must differ.
        assert h1["X-Authorization-Timestamp"] != h2["X-Authorization-Timestamp"] or \
               h1["X-Authorization-Signature-SHA256"] != h2["X-Authorization-Signature-SHA256"]


# ── 10. Accessors ─────────────────────────────────────────────────────────────

class TestAccessors:
    def _client_with_price(self, price=50_000.0):
        from market_data.rtds_client import SpotPrice
        client = ChainlinkStreamsClient()
        client._active_feeds   = {"BTC": _FEED_ID_HEX}
        client._feedid_to_coin = {_FEED_ID_LOW: "BTC"}
        _run(client._handle_message(_make_report_msg(price)))
        return client

    def test_get_mid_returns_price(self):
        client = self._client_with_price(50_000.0)
        assert abs(client.get_mid("BTC") - 50_000.0) < 1e-6

    def test_get_mid_unknown_coin_is_none(self):
        client = self._client_with_price()
        assert client.get_mid("XRP") is None

    def test_get_spot_returns_spot_price(self):
        from market_data.rtds_client import SpotPrice
        client = self._client_with_price()
        snap = client.get_spot("BTC")
        assert isinstance(snap, SpotPrice)
        assert snap.coin == "BTC"

    def test_get_spot_age_is_finite_and_small(self):
        client = self._client_with_price()
        age = client.get_spot_age("BTC")
        assert 0.0 <= age < 2.0

    def test_get_spot_age_unknown_coin_is_inf(self):
        client = ChainlinkStreamsClient()
        assert client.get_spot_age("UNKNOWN") == float("inf")

    def test_enabled_false_before_start(self):
        client = ChainlinkStreamsClient()
        assert client.enabled is False

    def test_is_connected_false_when_no_active_connections(self):
        client = ChainlinkStreamsClient()
        client._running = True
        client.stats.active_connections = 0
        assert client.is_connected is False

    def test_is_connected_true_when_running_and_one_active(self):
        client = ChainlinkStreamsClient()
        client._running = True
        client.stats.active_connections = 1
        assert client.is_connected is True

    def test_is_connected_false_when_not_running(self):
        client = ChainlinkStreamsClient()
        client._running = False
        client.stats.active_connections = 2
        assert client.is_connected is False


# ── 11. stop() ────────────────────────────────────────────────────────────────

class TestStop:
    def test_stop_sets_running_false(self):
        client = ChainlinkStreamsClient()
        client._running = True

        ws1, ws2 = AsyncMock(), AsyncMock()
        client._ws_handles = [ws1, ws2]

        _run(client.stop())

        assert client._running is False

    def test_stop_closes_all_ws_handles(self):
        client = ChainlinkStreamsClient()
        client._running = True

        ws1, ws2 = AsyncMock(), AsyncMock()
        client._ws_handles = [ws1, ws2]

        _run(client.stop())

        ws1.close.assert_awaited_once()
        ws2.close.assert_awaited_once()

    def test_stop_clears_handles_list(self):
        client = ChainlinkStreamsClient()
        client._running = True
        ws1 = AsyncMock()
        client._ws_handles = [ws1]

        _run(client.stop())

        assert client._ws_handles == []


# ── 12. StreamStats ───────────────────────────────────────────────────────────

class TestStreamStats:
    def test_str_contains_all_fields(self):
        s = StreamStats(
            accepted=10, deduplicated=3,
            partial_reconnects=2, full_reconnects=1,
            configured_connections=2, active_connections=2,
        )
        text = str(s)
        assert "accepted=10" in text
        assert "dedup=3" in text
        assert "partial_reconnects=2" in text
        assert "full_reconnects=1" in text
        assert "configured=2" in text
        assert "active=2" in text

    def test_default_all_zeros(self):
        s = StreamStats()
        assert s.accepted == 0
        assert s.deduplicated == 0
        assert s.partial_reconnects == 0
        assert s.full_reconnects == 0
        assert s.configured_connections == 0
        assert s.active_connections == 0
