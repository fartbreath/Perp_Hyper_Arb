"""
tests/test_hl_client.py — Unit tests for hl_client.py

All HL SDK / network calls are mocked.
Run:  pytest tests/test_hl_client.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

import config
from market_data.hl_client import HLClient, BBO, FundingSnapshot


# ── BBO dataclass ─────────────────────────────────────────────────────────────

class TestBBO:
    def test_mid_both_sides(self):
        bbo = BBO(coin="BTC", bid=83000.0, ask=83100.0)
        assert bbo.mid == pytest.approx(83050.0)

    def test_mid_bid_only(self):
        bbo = BBO(coin="BTC", bid=83000.0, ask=None)
        assert bbo.mid == 83000.0

    def test_mid_ask_only(self):
        bbo = BBO(coin="BTC", bid=None, ask=83100.0)
        assert bbo.mid == 83100.0

    def test_mid_neither(self):
        bbo = BBO(coin="BTC")
        assert bbo.mid is None

    def test_spread(self):
        bbo = BBO(coin="BTC", bid=83000.0, ask=83100.0)
        assert bbo.spread == pytest.approx(100.0)

    def test_spread_none_when_missing_side(self):
        bbo = BBO(coin="BTC", bid=83000.0, ask=None)
        assert bbo.spread is None


# ── get_mid ───────────────────────────────────────────────────────────────────

class TestGetMid:
    def setup_method(self):
        config.PAPER_TRADING = True
        self.client = HLClient.__new__(HLClient)
        self.client._bbo = {}
        self.client._mids = {}
        self.client._fundings = {}
        self.client._bbo_callbacks = []
        self.client._paper_mode = True
        self.client._running = False
        self.client._info = None
        self.client._exchange = None

    def test_get_mid_from_bbo(self):
        self.client._bbo["BTC"] = BBO(coin="BTC", bid=83000.0, ask=83200.0)
        assert self.client.get_mid("BTC") == pytest.approx(83100.0)

    def test_get_mid_falls_back_to_allmids(self):
        self.client._mids["ETH"] = 2500.0
        assert self.client.get_mid("ETH") == 2500.0

    def test_get_mid_unknown_coin(self):
        assert self.client.get_mid("DOGE") is None

    def test_bbo_takes_priority_over_mids(self):
        self.client._bbo["BTC"] = BBO(coin="BTC", bid=83000.0, ask=83200.0)
        self.client._mids["BTC"] = 80000.0  # stale
        # BBO mid should win
        assert self.client.get_mid("BTC") == pytest.approx(83100.0)


# ── Paper trading — place_hedge ───────────────────────────────────────────────

class TestPlaceHedgePaper:
    def setup_method(self):
        config.PAPER_TRADING = True
        self.client = HLClient.__new__(HLClient)
        self.client._paper_mode = True
        self.client._exchange = None
        self.client._info = None
        self.client._bbo = {}
        self.client._mids = {"BTC": 83000.0}
        self.client._fundings = {}
        self.client._bbo_callbacks = []
        self.client._running = False

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_paper_short_returns_status(self):
        result = self._run(self.client.place_hedge("BTC", "SHORT", 0.01))
        assert result is not None
        assert result["status"] == "paper"
        assert result["direction"] == "SHORT"

    def test_paper_long_returns_status(self):
        result = self._run(self.client.place_hedge("ETH", "LONG", 0.1))
        assert result is not None
        assert result["direction"] == "LONG"
        assert result["coin"] == "ETH"

    def test_no_exchange_live_returns_none(self):
        self.client._paper_mode = False
        self.client._exchange = None
        result = self._run(self.client.place_hedge("BTC", "SHORT", 0.01))
        assert result is None


# ── WS message handling ───────────────────────────────────────────────────────

class TestWSMessageHandling:
    def setup_method(self):
        config.PAPER_TRADING = True
        self.client = HLClient.__new__(HLClient)
        self.client._bbo = {}
        self.client._mids = {}
        self.client._bbo_callbacks = []
        self.client._paper_mode = True
        self.client._running = True
        self.client._info = None
        self.client._exchange = None
        self.client._fundings = {}

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_allmids_updates_mids(self):
        msg = json.dumps({
            "channel": "allMids",
            "data": {"mids": {"BTC": "83000.5", "ETH": "2500.25"}},
        })
        self._run(self.client._handle_ws_message(msg))
        assert self.client._mids["BTC"] == pytest.approx(83000.5)
        assert self.client._mids["ETH"] == pytest.approx(2500.25)

    def test_bbo_updates_bbo(self):
        msg = json.dumps({
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "levels": [
                    [{"px": "83000.0", "sz": "0.5", "n": 1}],
                    [{"px": "83100.0", "sz": "0.3", "n": 1}],
                ],
            },
        })
        self._run(self.client._handle_ws_message(msg))
        bbo = self.client._bbo.get("BTC")
        assert bbo is not None
        assert bbo.bid == pytest.approx(83000.0)
        assert bbo.ask == pytest.approx(83100.0)

    def test_bbo_fires_callback(self):
        fired = []

        async def cb(coin, bbo):
            fired.append((coin, bbo))

        self.client._bbo_callbacks = [cb]
        msg = json.dumps({
            "channel": "l2Book",
            "data": {
                "coin": "ETH",
                "levels": [
                    [{"px": "2500.0", "sz": "1.0", "n": 1}],
                    [{"px": "2502.0", "sz": "0.5", "n": 1}],
                ],
            },
        })
        self._run(self.client._handle_ws_message(msg))
        assert len(fired) == 1
        assert fired[0][0] == "ETH"

    def test_invalid_json_ignored(self):
        # Should not raise
        self._run(self.client._handle_ws_message("{{not json}}"))

    def test_unknown_channel_ignored(self):
        msg = json.dumps({"channel": "unknownChannel", "data": {}})
        # Should not raise
        self._run(self.client._handle_ws_message(msg))


# ── Funding snapshot ──────────────────────────────────────────────────────────

class TestFundingSnapshot:
    def setup_method(self):
        config.PAPER_TRADING = True
        self.client = HLClient.__new__(HLClient)
        self.client._fundings = {}
        self.client._bbo = {}
        self.client._mids = {}
        self.client._bbo_callbacks = []
        self.client._paper_mode = True
        self.client._running = False

        self.mock_info = MagicMock()
        self.client._info = self.mock_info

    def test_fetch_fundings_updates_snapshot(self):
        config.HL_PERP_COINS = ["BTC", "ETH"]
        self.mock_info.meta_and_asset_ctxs.return_value = (
            {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]},
            [
                {"funding": "0.0001", "openInterest": "1234.5"},
                {"funding": "0.00005", "openInterest": "5678.0"},
                {"funding": "0.00002", "openInterest": "999.0"},
            ],
        )
        self.client._fetch_fundings()
        snap = self.client._fundings.get("BTC")
        assert snap is not None
        assert snap.hl_predicted == pytest.approx(0.0001)

    def test_get_funding_returns_snapshot(self):
        self.client._fundings["BTC"] = FundingSnapshot(coin="BTC", hl_predicted=0.0001)
        snap = self.client.get_funding("BTC")
        assert snap.hl_predicted == pytest.approx(0.0001)

    def test_get_funding_unknown_coin(self):
        assert self.client.get_funding("DOGE") is None
