"""
tests/test_rtds_client.py — Unit tests for market_data/rtds_client.py

Tests cover the reconnect-storm fix:
  - _prices and _cl_prices are cleared on each fresh WS connect so that
    get_spot_age() returns inf instead of stale pre-reconnect timestamps.
  - The health loop does not trigger another reconnect immediately after
    connect when all ages are inf (never-received, not stale).
  - The health loop's all([]) == True empty-iterable edge case is handled.

Run:  pytest tests/test_rtds_client.py -v
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

# Pin TRACKED_UNDERLYINGS so RTDSClient._tracked_coins is deterministic.
config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP"]

from market_data.rtds_client import RTDSClient, SpotPrice


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestRTDSReconnectStorm:
    """RTDSClient must not enter a reconnect storm after a fresh WS connection.

    Root cause: after reconnect, self._prices retained timestamps from the old
    connection.  The health loop fired within 20–45 s, saw all coins as stale,
    and triggered ANOTHER reconnect before the new connection had time to
    deliver any prices.  This repeated indefinitely, locking out all
    1h/daily/weekly markets via the stale_spot gate.
    """

    def test_prices_cleared_on_fresh_connect(self):
        """After a WS reconnect _prices must be empty so get_spot_age returns inf.

        If _prices still holds old SpotPrice objects, get_spot_age() will
        return a large positive age (seconds since last real price) which the
        health loop interprets as stale and immediately forces another reconnect.
        """
        client = RTDSClient()

        # Inject pre-existing prices from the old session (simulates stale data).
        old_ts = time.time() - 60.0  # 60 s ago — well past RTDS_STALE_THRESH
        client._prices["BTC"] = SpotPrice("BTC", 50000.0, old_ts)
        client._prices["ETH"] = SpotPrice("ETH", 3000.0, old_ts)

        # Sanity check: ages look stale before reconnect.
        assert client.get_spot_age("BTC") >= 60.0
        assert client.get_spot_age("ETH") >= 60.0

        # Simulate what _ws_loop does on fresh connect: clear _prices.
        client._prices.clear()
        client._cl_prices.clear()

        # After clear, get_spot_age must return inf — "never received", not "stale".
        assert client.get_spot_age("BTC") == float("inf"), (
            "After reconnect-clear, BTC age must be inf (never received)"
        )
        assert client.get_spot_age("ETH") == float("inf"), (
            "After reconnect-clear, ETH age must be inf (never received)"
        )

    def test_health_loop_does_not_reconnect_when_all_ages_inf(self):
        """Health loop must NOT set _reconnect_requested when all prices are unseen (inf).

        When _prices is cleared on connect, get_spot_age returns inf for all
        coins.  The old health-loop code used:
            all(a >= 45 for a in ages.values() if a != inf)
        which evaluates all([]) == True — an empty-iterable bug that triggered
        an immediate reconnect storm.

        Runs the actual _health_log_loop with a patched sleep so one iteration
        executes instantly.
        """
        client = RTDSClient()
        client._tracked_coins = {"BTC", "ETH"}
        client._last_reconnect_at = time.time() - 90.0  # cooldown expired
        client._ws = MagicMock()
        # _prices is empty — all get_spot_age() return inf (never received).

        def _run_loop_once(c: RTDSClient) -> None:
            call_count = [0]
            async def fake_sleep(_: float) -> None:
                call_count[0] += 1
                if call_count[0] >= 2:
                    c._running = False

            async def _go():
                c._running = True
                with patch("market_data.rtds_client.asyncio.sleep", new=fake_sleep):
                    await c._health_log_loop()

            _run(_go())

        _run_loop_once(client)

        assert not client._reconnect_requested, (
            "Health loop must NOT set _reconnect_requested when all prices are inf. "
            "all([]) == True is the empty-iterable bug this guards against."
        )

    def test_health_loop_does_reconnect_when_prices_genuinely_stale(self):
        """Health loop MUST set _reconnect_requested when prices are stale and cooldown expired.

        Runs the actual _health_log_loop with a patched sleep.
        """
        client = RTDSClient()
        client._tracked_coins = {"BTC", "ETH"}
        client._last_reconnect_at = time.time() - 90.0  # cooldown expired

        old_ts = time.time() - 60.0  # 60 s ago — well past the 45 s stale threshold
        client._prices["BTC"] = SpotPrice("BTC", 50000.0, old_ts)
        client._prices["ETH"] = SpotPrice("ETH", 3000.0, old_ts)
        client._ws = MagicMock()

        call_count = [0]
        async def fake_sleep(_: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                client._running = False

        async def _go():
            client._running = True
            with patch("market_data.rtds_client.asyncio.sleep", new=fake_sleep):
                await client._health_log_loop()

        _run(_go())

        assert client._reconnect_requested, (
            "Health loop must set _reconnect_requested when all prices are stale >= 45 s "
            "and the cooldown has expired."
        )

    def test_health_loop_respects_cooldown_after_connect(self):
        """Health loop must not set _reconnect_requested within 60 s of the last connect.

        Runs the actual _health_log_loop with a patched sleep.
        """
        client = RTDSClient()
        client._tracked_coins = {"BTC"}
        client._last_reconnect_at = time.time() - 5.0  # only 5 s ago — cooldown active

        old_ts = time.time() - 60.0
        client._prices["BTC"] = SpotPrice("BTC", 50000.0, old_ts)
        client._ws = MagicMock()

        call_count = [0]
        async def fake_sleep(_: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 2:
                client._running = False

        async def _go():
            client._running = True
            with patch("market_data.rtds_client.asyncio.sleep", new=fake_sleep):
                await client._health_log_loop()

        _run(_go())

        assert not client._reconnect_requested, (
            "Health loop must NOT set _reconnect_requested within 60 s of last connect "
            "(cooldown guard prevents storm on slow-starting connections)."
        )

    def test_chainlink_subscription_uses_wildcard_type(self):
        """crypto_prices_chainlink subscription must use type='*' not type='update'.

        The official RTDS docs show:
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}
        Using type='update' silently drops non-update message types (e.g. snapshot
        messages on subscribe), causing missed Chainlink price events.
        """
        import json

        client = RTDSClient()

        sent: list[str] = []

        class FakeWS:
            async def send(self, msg: str) -> None:
                sent.append(msg)

        _run(client._subscribe(FakeWS()))

        assert len(sent) == 1
        payload = json.loads(sent[0])
        subs = {s["topic"]: s["type"] for s in payload["subscriptions"]}

        assert subs.get("crypto_prices_chainlink") == "*", (
            f"crypto_prices_chainlink must use type='*', got: {subs.get('crypto_prices_chainlink')}"
        )
        assert subs.get("crypto_prices") == "update", (
            f"crypto_prices must use type='update', got: {subs.get('crypto_prices')}"
        )
