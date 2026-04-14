"""
tests/test_pm_client.py — Unit tests for pm_client.py

All external calls (HTTP, WebSocket, CLOB SDK) are mocked.
Run:  pytest tests/test_pm_client.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from market_data.pm_client import PMClient, PMMarket, OrderBookSnapshot, _classify_market


# ── Market classification ─────────────────────────────────────────────────────

class TestClassifyMarket:
    def test_15min(self):
        assert _classify_market("Will BTC be above $90k in the next 15-minute candle?") == "bucket_15m"

    def test_1h(self):
        assert _classify_market("BTC 1-hour price direction") == "bucket_1h"

    def test_daily(self):
        assert _classify_market("Will ETH close higher today? (daily)") == "bucket_daily"

    def test_weekly(self):
        assert _classify_market("BTC weekly close above $100k?") == "bucket_weekly"

    def test_milestone_fallback(self):
        assert _classify_market("Will BTC reach $250k by end of 2026?") == "milestone"

    def test_5min(self):
        assert _classify_market("SOL 5-minute pump?") == "bucket_5m"


# ── _parse_market ─────────────────────────────────────────────────────────────

class TestParseMarket:
    def setup_method(self):
        self.client = PMClient.__new__(PMClient)
        self.client._markets = {}

    def _raw(self, **overrides) -> dict:
        base = {
            "conditionId": "cond_001",
            "question": "Will BTC daily close above $100k on March 15?",
            "enableOrderBook": True,
            "clobTokenIds": ["tok_yes_001", "tok_no_001"],
            "feesEnabled": False,
            "endDate": "2026-03-15T16:00:00Z",
            "minimumTickSize": "0.01",
            "maxIncentiveSpread": "0.04",
        }
        base.update(overrides)
        return base

    def test_valid_market_parsed(self):
        mkt = self.client._parse_market(self._raw())
        assert mkt is not None
        assert mkt.condition_id == "cond_001"
        assert mkt.underlying == "BTC"
        assert mkt.token_id_yes == "tok_yes_001"
        assert mkt.fees_enabled is False
        assert mkt.market_type == "bucket_daily"

    def test_no_order_book_returns_none(self):
        mkt = self.client._parse_market(self._raw(enableOrderBook=False))
        assert mkt is None

    def test_missing_tokens_returns_none(self):
        mkt = self.client._parse_market(self._raw(clobTokenIds=["only_one"]))
        assert mkt is None

    def test_unknown_underlying_is_rejected(self):
        # Markets without a recognised underlying are skipped — no point
        # subscribing to order-book data for non-crypto markets.
        mkt = self.client._parse_market(self._raw(question="Will it rain in Paris on Friday?"))
        assert mkt is None

    def test_fees_enabled_flag(self):
        mkt = self.client._parse_market(self._raw(feesEnabled=True))
        assert mkt.fees_enabled is True
        assert not mkt.is_fee_free

    def test_fee_free_property(self):
        mkt = self.client._parse_market(self._raw(feesEnabled=False))
        assert mkt.is_fee_free is True

    def test_eth_underlying(self):
        mkt = self.client._parse_market(self._raw(question="ETH daily close above $3k?"))
        assert mkt.underlying == "ETH"

    def test_4h_series_with_daily_recurrence(self):
        # "BTC Up or Down 4h" series has recurrence="daily" on Gamma API, but
        # series title keyword matching should classify it as bucket_4h, not bucket_daily.
        raw = self._raw(question="Bitcoin Up or Down - March 19, 4:00AM-8:00AM ET")
        mkt = self.client._parse_market(raw, recurrence_override="daily", series_title_override="BTC Up or Down 4h")
        assert mkt is not None
        assert mkt.market_type == "bucket_4h"

    def test_hourly_series_with_daily_recurrence(self):
        # "DOGE Up or Down Hourly" series also has recurrence="daily"; series title wins.
        raw = self._raw(question="Dogecoin Up or Down - March 19, 4:00AM-5:00AM ET")
        mkt = self.client._parse_market(raw, recurrence_override="daily", series_title_override="DOGE Up or Down Hourly")
        assert mkt is not None
        assert mkt.market_type == "bucket_1h"

    def test_series_title_overrides_recurrence_for_4h(self):
        # Recurrence alone ("daily") would give bucket_daily; series title corrects it.
        raw = self._raw(question="Ethereum Up or Down - March 19, 12:00PM-4:00PM ET")
        mkt_with_series = self.client._parse_market(raw, recurrence_override="daily", series_title_override="Ethereum Up or Down 4H")
        mkt_without_series = self.client._parse_market(raw, recurrence_override="daily")
        assert mkt_with_series.market_type == "bucket_4h"
        assert mkt_without_series.market_type == "bucket_daily"  # old (incorrect) behaviour

    def test_genuinely_daily_series_still_classified_correctly(self):
        # A daily series titled "BTC Daily" should remain bucket_daily.
        raw = self._raw(question="Will BTC close higher today?")
        mkt = self.client._parse_market(raw, recurrence_override="daily", series_title_override="BTC Daily")
        assert mkt is not None
        assert mkt.market_type == "bucket_daily"

    def test_series_title_empty_falls_back_to_recurrence(self):
        # No series title: fall back to recurrence mapping as before.
        raw = self._raw(question="Bitcoin Up or Down - some event")
        mkt = self.client._parse_market(raw, recurrence_override="hourly", series_title_override="")
        assert mkt is not None
        assert mkt.market_type == "bucket_1h"


# ── OrderBookSnapshot ─────────────────────────────────────────────────────────

class TestOrderBookSnapshot:
    def test_best_bid_ask(self):
        snap = OrderBookSnapshot(
            token_id="tok1",
            bids=[(0.48, 100), (0.47, 200)],
            asks=[(0.52, 50), (0.53, 150)],
        )
        assert snap.best_bid == 0.48
        assert snap.best_ask == 0.52

    def test_mid_with_both_sides(self):
        snap = OrderBookSnapshot(
            token_id="tok1",
            bids=[(0.48, 100)],
            asks=[(0.52, 50)],
        )
        assert snap.mid == pytest.approx(0.50)

    def test_mid_bid_only(self):
        snap = OrderBookSnapshot(token_id="tok1", bids=[(0.45, 100)], asks=[])
        assert snap.mid == 0.45

    def test_mid_ask_only(self):
        snap = OrderBookSnapshot(token_id="tok1", bids=[], asks=[(0.55, 100)])
        assert snap.mid == 0.55

    def test_mid_empty_book(self):
        snap = OrderBookSnapshot(token_id="tok1", bids=[], asks=[])
        assert snap.mid is None

    def test_best_bid_empty(self):
        snap = OrderBookSnapshot(token_id="tok1")
        assert snap.best_bid is None
        assert snap.best_ask is None


# ── Tick rounding ─────────────────────────────────────────────────────────────

class TestTickRounding:
    def setup_method(self):
        self.client = PMClient.__new__(PMClient)

    def test_round_to_cent(self):
        assert self.client._round_to_tick(0.4567, 0.01) == pytest.approx(0.46)

    def test_round_to_tenth(self):
        assert self.client._round_to_tick(0.44, 0.1) == pytest.approx(0.40)

    def test_exact_tick_unchanged(self):
        assert self.client._round_to_tick(0.45, 0.01) == pytest.approx(0.45)

    def test_round_to_thousandth(self):
        assert self.client._round_to_tick(0.4567, 0.001) == pytest.approx(0.457)


# ── Paper trading ─────────────────────────────────────────────────────────────

class TestPaperTrading:
    def setup_method(self):
        import config
        config.PAPER_TRADING = True
        self.client = PMClient.__new__(PMClient)
        self.client._paper_mode = True
        self.client._clob = None
        self.client._markets = {}
        self.client._books = {}
        self.client._price_callbacks = []
        self.client._running = False
        self.client._heartbeat_id = 0

    def test_place_limit_paper_returns_id(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.client.place_limit("tok_001", "BUY", 0.50, 100.0)
        )
        assert result is not None
        assert result.startswith("paper-")

    def test_cancel_all_paper_returns_true(self):
        result = asyncio.get_event_loop().run_until_complete(
            self.client.cancel_all()
        )
        assert result is True


# ── WS message handling ───────────────────────────────────────────────────────

class TestWSMessageHandling:
    def setup_method(self):
        import config as cfg
        cfg.PAPER_TRADING = True
        self.client = PMClient.__new__(PMClient)
        self.client._books = {}
        self.client._price_callbacks = []
        self.client._paper_mode = True
        self.client._running = True

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_book_message_updates_snapshot(self):
        msg = {
            "event_type": "book",
            "asset_id": "tok_001",
            "bids": [{"price": "0.48", "size": "100"}],
            "asks": [{"price": "0.52", "size": "50"}],
        }
        import json
        self._run(self.client._handle_ws_message(json.dumps(msg)))
        snap = self.client._books["tok_001"]
        assert snap.best_bid == pytest.approx(0.48)
        assert snap.best_ask == pytest.approx(0.52)

    def test_invalid_json_ignored(self):
        # Should not raise
        self._run(self.client._handle_ws_message("not valid json{{{"))

    def test_bids_sorted_descending(self):
        msg = {
            "event_type": "book",
            "asset_id": "tok_002",
            "bids": [
                {"price": "0.45", "size": "50"},
                {"price": "0.48", "size": "100"},
                {"price": "0.46", "size": "75"},
            ],
            "asks": [],
        }
        import json
        self._run(self.client._handle_ws_message(json.dumps(msg)))
        snap = self.client._books["tok_002"]
        prices = [b[0] for b in snap.bids]
        assert prices == sorted(prices, reverse=True)

    def test_asks_sorted_ascending(self):
        msg = {
            "event_type": "book",
            "asset_id": "tok_003",
            "bids": [],
            "asks": [
                {"price": "0.55", "size": "50"},
                {"price": "0.52", "size": "100"},
                {"price": "0.53", "size": "75"},
            ],
        }
        import json
        self._run(self.client._handle_ws_message(json.dumps(msg)))
        snap = self.client._books["tok_003"]
        prices = [a[0] for a in snap.asks]
        assert prices == sorted(prices)

    def test_price_change_updates_book(self):
        """price_change event uses price_changes list with per-item asset_id."""
        import json
        # Prime with a book snapshot
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "book",
            "asset_id": "tok_004",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })))
        # Incremental update: add a new bid level
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "tok_004", "price": "0.47", "size": "50", "side": "BUY"},
            ],
        })))
        snap = self.client._books["tok_004"]
        bid_prices = [b[0] for b in snap.bids]
        assert 0.47 in bid_prices
        assert snap.best_bid == pytest.approx(0.47)

    def test_price_change_removes_zero_size_level(self):
        """A price_change entry with size=0 removes that level from the book."""
        import json
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "book",
            "asset_id": "tok_005",
            "bids": [{"price": "0.48", "size": "200"}, {"price": "0.46", "size": "100"}],
            "asks": [],
        })))
        # Remove the 0.48 level
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "tok_005", "price": "0.48", "size": "0", "side": "BUY"},
            ],
        })))
        snap = self.client._books["tok_005"]
        bid_prices = [b[0] for b in snap.bids]
        assert 0.48 not in bid_prices
        assert snap.best_bid == pytest.approx(0.46)

    def test_price_change_multi_token(self):
        """A single price_change event can update multiple token books."""
        import json
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "tok_yes", "price": "0.60", "size": "80", "side": "BUY"},
                {"asset_id": "tok_no",  "price": "0.38", "size": "80", "side": "BUY"},
            ],
        })))
        assert self.client._books["tok_yes"].best_bid == pytest.approx(0.60)
        assert self.client._books["tok_no"].best_bid == pytest.approx(0.38)

    def test_book_event_fires_callback(self):
        """book event should trigger price_change callbacks."""
        import json
        fired = []

        async def cb(token_id, mid):
            fired.append((token_id, mid))

        self.client._price_callbacks = [cb]
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "book",
            "asset_id": "tok_006",
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })))
        assert len(fired) == 1
        assert fired[0][0] == "tok_006"
        assert fired[0][1] == pytest.approx(0.50)

    def test_price_change_event_fires_callback(self):
        """price_change event should trigger price_change callbacks for each affected token."""
        import json
        fired = []

        async def cb(token_id, mid):
            fired.append(token_id)

        self.client._price_callbacks = [cb]
        self._run(self.client._handle_ws_message(json.dumps({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "tok_007", "price": "0.50", "size": "100", "side": "BUY"},
                {"asset_id": "tok_007", "price": "0.52", "size": "100", "side": "SELL"},
            ],
        })))
        assert "tok_007" in fired


# ── Fill-future / recent-fills mechanics ─────────────────────────────────────
# Covers M-3 items: WS-path resolution, early-fill race, stale-cache pruning.

class TestFillFuture:
    """Unit tests for register_fill_future / _fire_order_fill / _recent_fills."""

    def setup_method(self):
        self.client = PMClient.__new__(PMClient)
        self.client._pending_fill_futures = {}
        self.client._recent_fills = {}
        self.client._order_fill_callbacks = []

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- helpers --

    def _make_future(self) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        return loop.create_future()

    def _fill_msg(self, order_id: str = "order_abc") -> dict:
        return {"id": order_id, "status": "MATCHED", "size_matched": "10", "price": "0.55"}

    # -- test 1 --

    def test_fill_future_resolved_via_ws(self):
        """Happy path: future registered before fill arrives is resolved by _fire_order_fill."""
        fut = self._make_future()
        self.client.register_fill_future("order_abc", fut)
        assert not fut.done()

        self._run(self.client._fire_order_fill(self._fill_msg("order_abc")))

        assert fut.done()
        assert fut.result()["id"] == "order_abc"

    # -- test 2 --

    def test_fill_future_race_early_fill(self):
        """Race: fill event arrives before register_fill_future is called.

        This happens when the REST order-placement suspends (e.g. slow network)
        and the user WS processes the MATCHED event first.  The fill lands in
        _recent_fills; register_fill_future must resolve the future immediately
        from the cache rather than leaving it pending indefinitely.
        """
        # Fire fill first (no future registered yet)
        self._run(self.client._fire_order_fill(self._fill_msg("order_xyz")))
        assert "order_xyz" in self.client._recent_fills

        # Now register — should resolve immediately from cache
        fut = self._make_future()
        self.client.register_fill_future("order_xyz", fut)
        assert fut.done()
        assert fut.result()["id"] == "order_xyz"
        # Cache entry must be consumed (not left as a dangling entry)
        assert "order_xyz" not in self.client._recent_fills

    # -- test 3 --

    def test_fill_future_stale_cache_pruned(self):
        """Stale entries older than 30s are evicted by register_fill_future."""
        import time as _time

        old_ts = _time.time() - 31          # definitely stale
        self.client._recent_fills["stale_order"] = ({"id": "stale_order"}, old_ts)

        # Calling register_fill_future (for any order) triggers the prune sweep
        fut = self._make_future()
        self.client.register_fill_future("new_order", fut)

        assert "stale_order" not in self.client._recent_fills


# ── Auto-redeem deduplication ─────────────────────────────────────────────────
# Covers M-3: ensure on-chain _redeem_ctf_via_safe is called exactly once per
# token even when _redeem_ready_positions is invoked multiple times.

class TestAutoRedeemDedup:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_monitor(self):
        from monitor import PositionMonitor
        pm = MagicMock()
        pm.get_live_positions = AsyncMock(return_value=[{
            "asset": "tok_winning",
            "redeemable": True,
            "size": "50",
            "currentPrice": "1.0",
            "conditionId": "cond_win",
            "title": "Test market",
        }])
        pm.get_markets = MagicMock(return_value={})
        pm._clob = MagicMock()
        pm._clob.get_conditional_address = MagicMock(return_value="0xCTF")
        pm._clob.get_collateral_address = MagicMock(return_value="0xUSDC")
        risk = MagicMock()
        risk.get_positions = MagicMock(return_value={})
        risk.get_position_by_hedge_token = MagicMock(return_value=None)  # not a hedge token
        mon = PositionMonitor.__new__(PositionMonitor)
        mon._pm = pm
        mon._risk = risk
        mon._redeemed_tokens = set()
        mon._spot = None  # no spot oracle needed for dedup test
        return mon

    def test_auto_redeem_dedup(self):
        """_redeem_ready_positions called twice emits on-chain tx exactly once."""
        import monitor as monitor_mod
        mon = self._make_monitor()

        redeem_mock = AsyncMock(return_value="0xdeadbeef")
        with patch.object(monitor_mod, "_redeem_ctf_via_safe", redeem_mock):
            self._run(mon._redeem_ready_positions())
            self._run(mon._redeem_ready_positions())

        redeem_mock.assert_called_once()
        assert "tok_winning" in mon._redeemed_tokens


# ── Prefetch task cancellation ────────────────────────────────────────────────
# Covers M-3: MomentumScanner.stop() must cancel the vol prefetch task so it
# does not run after the scanner is stopped.

class TestPrefetchTaskCancellation:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_prefetch_task_cancelled_on_stop(self):
        """stop() cancels the vol prefetch background task returned by start_prefetch."""
        from strategies.Momentum.scanner import MomentumScanner

        pm = MagicMock(); pm.on_price_change = MagicMock()
        hl = MagicMock()
        risk = MagicMock()
        vol = MagicMock()

        # start_prefetch returns a real asyncio Task wrapping a never-ending coroutine
        async def _never_end():
            await asyncio.sleep(3600)

        loop = asyncio.get_event_loop()
        fake_task = loop.create_task(_never_end())
        vol.start_prefetch = MagicMock(return_value=fake_task)

        scanner = MomentumScanner.__new__(MomentumScanner)
        scanner._pm = pm
        scanner._hl = hl
        scanner._risk = risk
        scanner._vol = vol
        scanner._running = False
        scanner._market_cooldown = {}
        scanner._open_spot_path = ""
        scanner._market_open_spot = {}
        scanner._last_scan_diags = []
        scanner._last_scan_summary = {}
        scanner._last_scan_ts = 0.0
        scanner._scan_event = asyncio.Event()
        scanner._token_to_market = {}
        scanner._vol_prefetch_task = None

        # Simulate what start() does (just the prefetch assignment, no full start)
        scanner._vol_prefetch_task = vol.start_prefetch([])

        assert not fake_task.done()

        self._run(scanner.stop())

        assert fake_task.cancelled()

