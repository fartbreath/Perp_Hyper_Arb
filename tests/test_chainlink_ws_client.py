"""
tests/test_chainlink_ws_client.py — Unit tests for ChainlinkWSClient.

All tests are fully offline — WebSocket calls are mocked.

Behaviours verified:

  1. decode_latest_round_data  — correct ABI decode for latestRoundData() HTTP seed.
  2. topics[1] decode           — int256 signed price from AnswerUpdated events.
  3. _handle_event              — valid log → price stored + callback fired.
  4. _handle_event              — unknown address, bad topics, non-positive price silently ignored.
  5. Accessors                  — get_mid / get_spot / get_spot_age / all_mids.
  6. Multiple callbacks          — all registered callbacks fire on each event.
  7. SpotPrice.timestamp        — uses local receipt time, not on-chain updatedAt.
  8. start / stop lifecycle     — WS loop starts and terminates cleanly.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.POLYGON_RPC_URL = "http://fake-rpc.example.com"
config.POLYGON_WS_URL = "ws://fake-ws.example.com"

from market_data.chainlink_ws_client import (
    ChainlinkWSClient,
    decode_latest_round_data,
    _CL_CONTRACTS,
    _DECIMALS,
    _ANSWER_UPDATED_TOPIC,
    _ADDR_TO_COIN,
)
from market_data.rtds_client import SpotPrice


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


# ── latestRoundData() ABI decoder ─────────────────────────────────────────────

class TestDecodeLatestRoundData:
    """decode_latest_round_data is a module-level function used for HTTP seed calls."""

    def _encode(self, answer: int, updated_at: int) -> str:
        """Pack 5 × 32-byte ABI slots — slots [1] and [3] carry the interesting data."""
        slot0 = (0).to_bytes(32, "big")
        slot1 = answer.to_bytes(32, byteorder="big", signed=True)
        slot2 = (0).to_bytes(32, "big")
        slot3 = updated_at.to_bytes(32, byteorder="big", signed=False)
        slot4 = (0).to_bytes(32, "big")
        return "0x" + (slot0 + slot1 + slot2 + slot3 + slot4).hex()

    def test_btc_price_decode(self):
        raw_answer = int(65_000.12345678 * 10 ** _DECIMALS)
        result = decode_latest_round_data(self._encode(raw_answer, 1_714_000_000))
        assert result is not None
        price, updated_at = result
        assert abs(price - 65_000.12345678) < 1e-6
        assert updated_at == 1_714_000_000

    def test_eth_price_decode(self):
        raw_answer = int(3_100.0 * 10 ** _DECIMALS)
        result = decode_latest_round_data(self._encode(raw_answer, 1_714_000_100))
        assert result is not None
        price, _ = result
        assert abs(price - 3_100.0) < 1e-6

    def test_doge_small_price(self):
        raw_answer = int(0.15 * 10 ** _DECIMALS)
        result = decode_latest_round_data(self._encode(raw_answer, 1_714_000_200))
        assert result is not None
        price, _ = result
        assert abs(price - 0.15) < 1e-7

    def test_zero_answer_returns_none(self):
        assert decode_latest_round_data(self._encode(0, 1_714_000_000)) is None

    def test_negative_answer_returns_none(self):
        assert decode_latest_round_data(self._encode(-1, 1_714_000_000)) is None

    def test_empty_hex_returns_none(self):
        assert decode_latest_round_data("0x") is None

    def test_short_hex_returns_none(self):
        assert decode_latest_round_data("0xdeadbeef") is None

    def test_invalid_hex_returns_none(self):
        assert decode_latest_round_data("0xZZZZ") is None

    def test_no_0x_prefix_accepted(self):
        raw_answer = int(50_000 * 10 ** _DECIMALS)
        hex_data = self._encode(raw_answer, 1_714_000_000)[2:]   # strip 0x
        result = decode_latest_round_data(hex_data)
        assert result is not None
        price, _ = result
        assert abs(price - 50_000) < 1e-4


# ── AnswerUpdated topics[1] decode ────────────────────────────────────────────

class TestDecodeAnswerUpdatedTopic:
    """topics[1] carries the int256 indexed current (price) in AnswerUpdated events."""

    def _make_topic(self, price_usd: float, decimals: int = _DECIMALS) -> str:
        """Encode price_usd as a 0x-prefixed 32-byte int256 hex string."""
        raw_int = int(round(price_usd * 10 ** decimals))
        return "0x" + raw_int.to_bytes(32, byteorder="big", signed=True).hex()

    def test_positive_price_decode(self):
        topic = self._make_topic(65_000.0)
        raw = int.from_bytes(bytes.fromhex(topic[2:].zfill(64)), byteorder="big", signed=True)
        price = raw / 10 ** _DECIMALS
        assert abs(price - 65_000.0) < 1e-6

    def test_small_price_decode(self):
        topic = self._make_topic(0.15)
        raw = int.from_bytes(bytes.fromhex(topic[2:].zfill(64)), byteorder="big", signed=True)
        price = raw / 10 ** _DECIMALS
        assert abs(price - 0.15) < 1e-7

    def test_negative_signed_value(self):
        """A negative signed int in topics[1] decodes as negative (not a valid price)."""
        neg_bytes = (-1).to_bytes(32, byteorder="big", signed=True)
        raw = int.from_bytes(neg_bytes, byteorder="big", signed=True)
        assert raw < 0

    def test_zero_value(self):
        zero_bytes = (0).to_bytes(32, byteorder="big", signed=True)
        raw = int.from_bytes(zero_bytes, byteorder="big", signed=True)
        assert raw == 0


# ── Accessor tests ────────────────────────────────────────────────────────────

class TestAccessors:

    def _seeded(self, coin: str, price: float) -> ChainlinkWSClient:
        client = ChainlinkWSClient()
        client._prices[coin] = SpotPrice(coin=coin, price=price, timestamp=time.time())
        return client

    def test_get_mid_returns_price(self):
        assert self._seeded("BTC", 65_000.0).get_mid("BTC") == 65_000.0

    def test_get_mid_unknown_coin_returns_none(self):
        assert ChainlinkWSClient().get_mid("HYPE") is None

    def test_get_spot_returns_spotprice(self):
        snap = self._seeded("ETH", 3_100.0).get_spot("ETH")
        assert isinstance(snap, SpotPrice)
        assert snap.price == 3_100.0
        assert snap.coin == "ETH"

    def test_get_spot_unknown_returns_none(self):
        assert ChainlinkWSClient().get_spot("NOTACOIN") is None

    def test_get_spot_age_finite(self):
        age = self._seeded("SOL", 150.0).get_spot_age("SOL")
        assert 0 <= age < 5

    def test_get_spot_age_inf_unseen(self):
        assert ChainlinkWSClient().get_spot_age("SOL") == float("inf")

    def test_all_mids(self):
        client = ChainlinkWSClient()
        client._prices["BTC"] = SpotPrice(coin="BTC", price=65_000.0, timestamp=time.time())
        client._prices["ETH"] = SpotPrice(coin="ETH", price=3_100.0, timestamp=time.time())
        assert client.all_mids() == {"BTC": 65_000.0, "ETH": 3_100.0}


# ── _handle_event tests ───────────────────────────────────────────────────────

def _make_log_msg(address: str, price_usd: float) -> dict:
    """Build a plausible eth_subscription AnswerUpdated log notification."""
    price_raw = int(round(price_usd * 10 ** _DECIMALS))
    topic1 = "0x" + price_raw.to_bytes(32, byteorder="big", signed=True).hex()
    topic2 = "0x" + (1000).to_bytes(32, byteorder="big", signed=False).hex()  # roundId
    updated_at = "0x" + int(time.time()).to_bytes(32, byteorder="big", signed=False).hex()
    return {
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {
            "subscription": "0xsubid",
            "result": {
                "address": address,
                "topics": [_ANSWER_UPDATED_TOPIC, topic1, topic2],
                "data": updated_at,
                "blockNumber": "0x1",
                "transactionHash": "0x" + "00" * 32,
                "logIndex": "0x0",
            },
        },
    }


class TestHandleEvent:

    def test_valid_event_updates_price_cache(self):
        """A valid AnswerUpdated log stores the price with the correct coin."""
        eth_addr = _CL_CONTRACTS["ETH"]
        msg = _make_log_msg(eth_addr, 3_200.0)

        async def _inner():
            client = ChainlinkWSClient()
            await client._handle_event(msg)
            return client

        client = _run(_inner())
        assert client.get_mid("ETH") is not None
        assert abs(client.get_mid("ETH") - 3_200.0) < 1e-4

    def test_all_six_contracts_recognised(self):
        """All six AggregatorV3 contracts map to the correct coin."""
        expected = {"BTC": 65_000.0, "ETH": 3_100.0, "SOL": 150.0,
                    "XRP": 0.5, "BNB": 400.0, "DOGE": 0.15}

        async def _inner():
            client = ChainlinkWSClient()
            for coin, price in expected.items():
                await client._handle_event(_make_log_msg(_CL_CONTRACTS[coin], price))
            return client

        client = _run(_inner())
        for coin, expected_price in expected.items():
            actual = client.get_mid(coin)
            assert actual is not None, f"{coin} not in cache"
            assert abs(actual - expected_price) < 1e-4, f"{coin}: got {actual}"

    def test_callback_fires_on_event(self):
        """on_price_update callback fires with the correct coin + price."""
        received: list[tuple[str, float]] = []

        async def _cb(coin: str, price: float) -> None:
            received.append((coin, price))

        async def _inner():
            client = ChainlinkWSClient()
            client.on_price_update(_cb)
            await client._handle_event(_make_log_msg(_CL_CONTRACTS["BTC"], 65_000.0))

        _run(_inner())
        assert len(received) == 1
        assert received[0][0] == "BTC"
        assert abs(received[0][1] - 65_000.0) < 1e-4

    def test_multiple_callbacks_all_fire(self):
        log_a: list[str] = []
        log_b: list[str] = []

        async def _inner():
            client = ChainlinkWSClient()
            async def _cb_a(c, p): log_a.append(f"a:{c}")
            async def _cb_b(c, p): log_b.append(f"b:{c}")
            client.on_price_update(_cb_a)
            client.on_price_update(_cb_b)
            await client._handle_event(_make_log_msg(_CL_CONTRACTS["ETH"], 3_100.0))

        _run(_inner())
        assert any("a:ETH" in s for s in log_a)
        assert any("b:ETH" in s for s in log_b)

    def test_unknown_address_silently_ignored(self):
        """An AnswerUpdated event from an unknown address does not crash or update cache."""
        unknown_addr = "0x" + "aa" * 20
        msg = _make_log_msg(unknown_addr, 100.0)

        async def _inner():
            client = ChainlinkWSClient()
            await client._handle_event(msg)   # must not raise
            return client

        client = _run(_inner())
        assert client.all_mids() == {}

    def test_non_positive_price_ignored(self):
        """A non-positive value in topics[1] does not enter the cache."""
        eth_addr = _CL_CONTRACTS["ETH"]
        # Craft a zero price
        topic1_zero = "0x" + (0).to_bytes(32, byteorder="big", signed=True).hex()
        msg = {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {
                "subscription": "0xsubid",
                "result": {
                    "address": eth_addr,
                    "topics": [_ANSWER_UPDATED_TOPIC, topic1_zero, "0x00"],
                    "data": "0x00",
                },
            },
        }

        async def _inner():
            client = ChainlinkWSClient()
            await client._handle_event(msg)
            return client

        client = _run(_inner())
        assert client.get_mid("ETH") is None

    def test_missing_params_silently_ignored(self):
        async def _inner():
            client = ChainlinkWSClient()
            await client._handle_event({"jsonrpc": "2.0", "id": 1, "result": "0xsubid"})

        _run(_inner())   # must not raise

    def test_too_few_topics_silently_ignored(self):
        """A log with only one topic (no price data) is silently dropped."""
        msg = {
            "params": {
                "result": {
                    "address": _CL_CONTRACTS["BTC"],
                    "topics": [_ANSWER_UPDATED_TOPIC],  # missing topics[1]
                    "data": "0x00",
                },
            },
        }

        async def _inner():
            client = ChainlinkWSClient()
            await client._handle_event(msg)

        _run(_inner())   # must not raise

    def test_spotprice_timestamp_is_local_receipt_time(self):
        """SpotPrice.timestamp must be local event-receipt time, not on-chain updatedAt."""
        on_chain_ts = 1_000_000_000   # year 2001 — must NOT appear in SpotPrice.timestamp
        eth_addr = _CL_CONTRACTS["ETH"]
        price_raw = int(3_100.0 * 10 ** _DECIMALS)
        topic1 = "0x" + price_raw.to_bytes(32, byteorder="big", signed=True).hex()
        old_data = "0x" + on_chain_ts.to_bytes(32, byteorder="big", signed=False).hex()
        msg = {
            "params": {
                "result": {
                    "address": eth_addr,
                    "topics": [_ANSWER_UPDATED_TOPIC, topic1, "0x00"],
                    "data": old_data,
                },
            },
        }

        async def _inner():
            before = time.time()
            client = ChainlinkWSClient()
            await client._handle_event(msg)
            after = time.time()
            return client, before, after

        client, before, after = _run(_inner())
        snap = client.get_spot("ETH")
        assert snap is not None
        assert before <= snap.timestamp <= after, (
            f"SpotPrice.timestamp {snap.timestamp} is outside event window "
            f"[{before}, {after}] — on-chain updatedAt was used instead"
        )


# ── Lifecycle tests ───────────────────────────────────────────────────────────

class TestLifecycle:

    def test_start_stop_no_crash(self):
        """start() / stop() round-trip does not raise even with unreachable WS."""
        async def _inner():
            with patch("market_data.chainlink_ws_client.aiohttp.ClientSession") as mock_session:
                # Seed HTTP call fails → but start() must succeed anyway
                mock_ctx = AsyncMock()
                mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
                mock_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_ctx.post = MagicMock(side_effect=Exception("network error"))
                mock_session.return_value = mock_ctx
                client = ChainlinkWSClient()
                # start + immediately stop
                await client.start()
                await client.stop()
                assert client._running is False

        _run(_inner())

    def test_stop_before_start_is_safe(self):
        async def _inner():
            client = ChainlinkWSClient()
            await client.stop()   # must not raise

        _run(_inner())

    def test_on_price_update_callback_registered(self):
        received: list = []

        async def _cb(coin, price):
            received.append((coin, price))

        client = ChainlinkWSClient()
        client.on_price_update(_cb)
        assert len(client._callbacks) == 1


# ── _CL_CONTRACTS coverage ────────────────────────────────────────────────────

class TestContractMap:

    def test_six_contracts_defined(self):
        assert len(_CL_CONTRACTS) == 6

    def test_expected_coins_present(self):
        assert set(_CL_CONTRACTS.keys()) == {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"}

    def test_hype_absent(self):
        """HYPE has no AggregatorV3 on Polygon; it uses Chainlink Data Streams instead."""
        assert "HYPE" not in _CL_CONTRACTS

    def test_addr_to_coin_reverse_map(self):
        for coin, addr in _CL_CONTRACTS.items():
            assert _ADDR_TO_COIN[addr.lower()] == coin
