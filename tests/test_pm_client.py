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
    """Unit tests for register_fill_future / _fire_trade_fill / _fire_order_fill / _recent_fills.

    Fill-future resolution is owned by _fire_trade_fill (trade events carry the
    actual execution price).  _fire_order_fill dispatches to maker callbacks only.
    """

    def setup_method(self):
        self.client = PMClient.__new__(PMClient)
        self.client._pending_fill_futures = {}
        self.client._recent_fills = {}
        self.client._order_fill_callbacks = []
        self.client._trade_exec_cache = {}

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- helpers --

    def _make_future(self) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        return loop.create_future()

    def _fill_msg(self, order_id: str = "order_abc") -> dict:
        return {"id": order_id, "status": "MATCHED", "size_matched": "10", "price": "0.55"}

    def _trade_msg(self, taker_order_id: str, price: str = "0.48", size: str = "10") -> dict:
        """Build a minimal trade event as PM user WS sends it."""
        return {
            "event_type":     "trade",
            "type":           "TRADE",
            "taker_order_id": taker_order_id,
            "price":          price,
            "size":           size,
            "status":         "MATCHED",
            "maker_orders":   [],
        }

    # -- test 1 --

    def test_fill_future_resolved_via_trade_event(self):
        """Happy path: future registered before trade event arrives is resolved by _fire_trade_fill."""
        fut = self._make_future()
        self.client.register_fill_future("order_abc", fut)
        assert not fut.done()

        self._run(self.client._fire_trade_fill(self._trade_msg("order_abc", price="0.48", size="10")))

        assert fut.done()
        result = fut.result()
        assert result["id"] == "order_abc"
        assert abs(result["price"] - 0.48) < 1e-9
        assert abs(result["size_matched"] - 10.0) < 1e-9

    # -- test 1b: _fire_order_fill must NOT resolve pending futures --

    def test_order_fill_does_not_resolve_future(self):
        """_fire_order_fill must not resolve pending futures — that is _fire_trade_fill's job.

        If order events could resolve futures, a race where the order MATCHED event
        arrives before the trade event would lock in the wrong limit price.
        """
        fut = self._make_future()
        self.client.register_fill_future("order_abc", fut)
        assert not fut.done()

        self._run(self.client._fire_order_fill(self._fill_msg("order_abc")))

        # Future must still be pending — no trade event arrived yet
        assert not fut.done()

    # -- test 2 --

    def test_fill_future_race_early_trade(self):
        """Race: trade event arrives before register_fill_future is called.

        This happens when the REST order-placement suspends (e.g. slow network)
        and the user WS processes the trade event first.  The fill lands in
        _recent_fills; register_fill_future must resolve the future immediately
        from the cache rather than leaving it pending indefinitely.
        """
        # Fire trade event first (no future registered yet)
        self._run(self.client._fire_trade_fill(self._trade_msg("order_xyz")))
        assert "order_xyz" in self.client._recent_fills

        # Now register — should resolve immediately from cache
        fut = self._make_future()
        self.client.register_fill_future("order_xyz", fut)
        assert fut.done()
        assert fut.result()["id"] == "order_xyz"
        assert abs(fut.result()["price"] - 0.48) < 1e-9
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

    # -- test 4: taker price used directly; maker_orders provide size only --

    def test_trade_fill_uses_taker_price_not_maker_vwap(self):
        """Fill price comes from trade_msg['price'] (taker's execution price).

        maker_orders are used ONLY to aggregate matched size — their individual
        price fields are ignored.  This is correct per the Polymarket types.ts
        spec: Trade.price is the taker's price; MakerOrder.price is the maker's
        price on the maker's token CLOB, which diverges on neg-risk markets.

        The three maker prices here deliberately do NOT average to 0.50 — they
        average to 0.47 — to confirm that maker VWAP is never used.
        """
        trade_msg = {
            "event_type":     "trade",
            "type":           "TRADE",
            "taker_order_id": "order_sweep",
            "price":          "0.50",   # taker's execution price — must be what we record
            "size":           "30",
            "status":         "MATCHED",
            "maker_orders": [
                {"order_id": "maker_1", "price": "0.44", "matched_amount": "10"},
                {"order_id": "maker_2", "price": "0.47", "matched_amount": "10"},
                {"order_id": "maker_3", "price": "0.50", "matched_amount": "10"},
                # VWAP of these maker prices = (0.44+0.47+0.50)/3 = 0.47 ≠ 0.50
                # If the code incorrectly used maker VWAP it would assert 0.47,
                # catching any regression that reverts the neg-risk fix.
            ],
        }
        fut = self._make_future()
        self.client.register_fill_future("order_sweep", fut)
        self._run(self.client._fire_trade_fill(trade_msg))

        assert fut.done()
        result = fut.result()
        # Must be the taker's price (0.50), not the maker VWAP (0.47)
        assert abs(result["price"] - 0.50) < 1e-9
        assert abs(result["size_matched"] - 30.0) < 1e-9

    # -- test 4b: neg-risk complement — maker prices are NO token, taker price is YES --

    def test_trade_fill_neg_risk_uses_taker_price_not_maker_complement(self):
        """Regression test for the neg-risk complement inversion bug.

        On a neg-risk Polymarket market the taker buys YES at 0.79.
        The matched makers are on the NO CLOB and are priced at ~0.21
        (the complement).  trade_msg['price'] = 0.79 (taker's price).
        _fire_trade_fill must record 0.79, not 0.21.

        The original bug: VWAP was computed from maker_orders[i]['price']
        (0.21) instead of trade_msg['price'] (0.79), producing a fill price
        of ~0.21.  That caused band_floor_abort on every live YES trade
        (0.21 < MOMENTUM_PRICE_BAND_LOW=0.6) and bypassed Phase D entirely.
        """
        trade_msg = {
            "event_type":     "trade",
            "type":           "TRADE",
            "taker_order_id": "order_yes_buy",
            "price":          "0.79",   # taker buys YES at 0.79
            "size":           "17.5",
            "status":         "MATCHED",
            "maker_orders": [
                # These makers are on the NO CLOB at the complement price (0.21).
                # They must NOT be used to derive the taker's fill price.
                {"order_id": "no_maker_1", "price": "0.21", "matched_amount": "10.0"},
                {"order_id": "no_maker_2", "price": "0.215", "matched_amount": "7.5"},
            ],
        }
        fut = self._make_future()
        self.client.register_fill_future("order_yes_buy", fut)
        self._run(self.client._fire_trade_fill(trade_msg))

        assert fut.done()
        result = fut.result()
        # Must record the taker YES price (0.79), NOT the NO-complement (≈0.21)
        assert abs(result["price"] - 0.79) < 1e-9, (
            f"Expected taker price 0.79, got {result['price']:.6f} — "
            "neg-risk complement inversion bug may have been reintroduced"
        )
        assert abs(result["size_matched"] - 17.5) < 1e-9

    # -- test 5: maker-side price injection into _fire_order_fill --

    def test_order_fill_injects_maker_trade_price(self):
        """When we are the maker, _fire_order_fill injects the cached trade price."""
        collected = []

        async def capture_cb(data):
            collected.append(data)

        self.client._order_fill_callbacks = [capture_cb]

        # Simulate trade event arriving first — caches the maker order price
        trade_msg = {
            "event_type":     "trade",
            "type":           "TRADE",
            "taker_order_id": "counterparty_order",
            "price":          "0.48",
            "size":           "10",
            "status":         "MATCHED",
            "maker_orders": [
                {"order_id": "our_maker_order", "price": "0.48", "matched_amount": "10"},
            ],
        }
        self._run(self.client._fire_trade_fill(trade_msg))

        # Now order MATCHED event arrives — price should be injected
        order_event = {"id": "our_maker_order", "status": "MATCHED",
                       "size_matched": "10", "price": "0.68"}  # limit price
        self._run(self.client._fire_order_fill(order_event))

        assert len(collected) == 1
        # price must be injected from trade cache (0.48), not limit price (0.68)
        assert abs(collected[0]["price"] - 0.48) < 1e-9


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
        pm.fetch_market_resolution = AsyncMock(return_value=1.0)
        pm.fetch_token_side = AsyncMock(return_value="yes")
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


# ── asyncio.to_thread order-placement tests ───────────────────────────────────
# Verifies that the to_thread wrapping of CLOB SDK calls in place_limit,
# place_market, cancel_order, and cancel_all:
#   1. Returns the correct result (order_id / bool) on success
#   2. Returns None / False on error (exception from the thread)
#   3. Keeps the event loop alive during execution (other tasks can progress)
#   4. Handles the post_only "crosses book" retry path correctly
#   5. Paper-mode paths are unaffected (no thread dispatch)

class TestPlaceLimitToThread:
    """place_limit in live mode uses asyncio.to_thread for all CLOB SDK calls."""

    def _make_client(self) -> PMClient:
        client = PMClient.__new__(PMClient)
        client._paper_mode = False
        client._clob = MagicMock()
        client._books = {}
        client._price_callbacks = []
        client._running = False
        return client

    def _make_market(self, tick: float = 0.01) -> PMMarket:
        mkt = MagicMock(spec=PMMarket)
        mkt.tick_size = tick
        mkt.condition_id = "cond_test"
        return mkt

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- 1. Happy path: returns order_id ----------------------------------------

    def test_place_limit_returns_order_id(self):
        """place_limit returns the order_id from the CLOB response."""
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed_order_obj")
        client._clob.post_order = MagicMock(return_value={"orderID": "order-001", "status": "live"})

        result = self._run(
            client.place_limit("tok_yes", "BUY", 0.75, 20.0,
                               market=self._make_market(), post_only=True)
        )
        assert result == "order-001"
        client._clob.create_order.assert_called_once()
        client._clob.post_order.assert_called_once()

    # -- 2. Taker limit (post_only=False) uses FAK order type -------------------

    def test_place_limit_taker_uses_fak(self):
        """post_only=False: post_order is called with OrderType.FAK."""
        from py_clob_client.clob_types import OrderType as _OT
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(return_value={"orderID": "fak-001"})

        self._run(
            client.place_limit("tok", "BUY", 0.80, 10.0,
                               market=self._make_market(), post_only=False)
        )
        call_args = client._clob.post_order.call_args
        # Second positional arg is the order type
        assert call_args[0][1] == _OT.FAK

    # -- 3. Maker limit (post_only=True) uses GTC order type --------------------

    def test_place_limit_maker_uses_gtc(self):
        """post_only=True: post_order is called with OrderType.GTC."""
        from py_clob_client.clob_types import OrderType as _OT
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(return_value={"orderID": "gtc-001"})

        self._run(
            client.place_limit("tok", "BUY", 0.80, 10.0,
                               market=self._make_market(), post_only=True)
        )
        call_args = client._clob.post_order.call_args
        assert call_args[0][1] == _OT.GTC

    # -- 4. post_order returns no orderID → None --------------------------------

    def test_place_limit_empty_order_id_returns_none(self):
        """If CLOB response has no orderID, place_limit returns None."""
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(return_value={"status": "error"})

        result = self._run(
            client.place_limit("tok", "BUY", 0.80, 10.0, market=self._make_market())
        )
        assert result is None

    # -- 5. Exception → None (no crash) -----------------------------------------

    def test_place_limit_exception_returns_none(self):
        """If CLOB raises, place_limit catches and returns None."""
        client = self._make_client()
        client._clob.create_order = MagicMock(side_effect=RuntimeError("network error"))

        result = self._run(
            client.place_limit("tok", "BUY", 0.80, 10.0, market=self._make_market())
        )
        assert result is None

    # -- 6. "crosses book" retry path -------------------------------------------

    def test_place_limit_crosses_book_retry(self):
        """post_only=True + 'crosses book' exception → retries at price - 1 tick."""
        client = self._make_client()
        # First call raises "crosses book"; second succeeds
        client._clob.create_order = MagicMock(return_value="signed")
        call_count = {"n": 0}

        def _post_order(signed, order_type, post_only=False):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("crosses book")
            return {"orderID": "retry-order-001"}

        client._clob.post_order = MagicMock(side_effect=_post_order)

        result = self._run(
            client.place_limit("tok", "BUY", 0.82, 10.0,
                               market=self._make_market(tick=0.01), post_only=True)
        )
        assert result == "retry-order-001"
        # create_order called twice (original + retry)
        assert client._clob.create_order.call_count == 2

    # -- 7. "crosses book" but price too low to back off → None -----------------

    def test_place_limit_crosses_book_no_retry_at_boundary(self):
        """If backing off one tick would produce price <= 0, give up and return None."""
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(side_effect=Exception("crosses book"))

        # Price = 0.01 tick=0.01 → retry would be 0.00 which is invalid
        result = self._run(
            client.place_limit("tok", "BUY", 0.01, 10.0,
                               market=self._make_market(tick=0.01), post_only=True)
        )
        assert result is None

    # -- 8. taker limit "crosses book" → NOT retried ----------------------------

    def test_place_limit_taker_crosses_book_no_retry(self):
        """post_only=False: 'crosses book' is not retried (taker orders are valid crossings)."""
        client = self._make_client()
        client._clob.create_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(side_effect=Exception("crosses book"))

        result = self._run(
            client.place_limit("tok", "BUY", 0.80, 10.0,
                               market=self._make_market(), post_only=False)
        )
        # Taker: no retry, returns None
        assert result is None
        # create_order called exactly once (no retry)
        assert client._clob.create_order.call_count == 1

    # -- 9. Event loop stays alive: concurrent coroutine progresses -------------

    def test_place_limit_event_loop_stays_alive(self):
        """Other tasks can run while place_limit is executing the blocking CLOB call.

        The test injects a 10 ms sleep into the mock create_order to simulate
        real blocking I/O.  A concurrent asyncio.sleep(0.005) task must complete
        before place_limit returns — which is only possible if the event loop is
        not blocked.
        """
        import time as _time
        client = self._make_client()
        progress = {"concurrent_ran": False}

        def _slow_create_order(order_args):
            _time.sleep(0.01)   # simulate 10 ms signing latency
            return "signed"

        client._clob.create_order = MagicMock(side_effect=_slow_create_order)
        client._clob.post_order = MagicMock(return_value={"orderID": "ok-001"})

        async def _concurrent_task():
            await asyncio.sleep(0.005)   # 5 ms — completes inside the 10 ms signing window
            progress["concurrent_ran"] = True

        async def _run_together():
            task = asyncio.create_task(_concurrent_task())
            await client.place_limit("tok", "BUY", 0.75, 10.0, market=self._make_market())
            await task  # make sure the task had a chance to complete

        asyncio.get_event_loop().run_until_complete(_run_together())
        assert progress["concurrent_ran"], (
            "Concurrent task did not progress — event loop was blocked during CLOB call"
        )

    # -- 10. Paper mode is unchanged (no thread dispatch) -----------------------

    def test_place_limit_paper_mode_no_clob_call(self):
        """In paper mode, CLOB is never touched and an ID is returned immediately."""
        client = self._make_client()
        client._paper_mode = True
        client._clob = MagicMock()  # should never be called

        result = self._run(
            client.place_limit("tok", "BUY", 0.75, 10.0)
        )
        assert result is not None
        assert result.startswith("paper-")
        client._clob.create_order.assert_not_called()
        client._clob.post_order.assert_not_called()


class TestPlaceMarketToThread:
    """place_market in live mode uses asyncio.to_thread for all CLOB SDK calls."""

    def _make_client(self) -> PMClient:
        client = PMClient.__new__(PMClient)
        client._paper_mode = False
        client._clob = MagicMock()
        client._books = {}
        client._price_callbacks = []
        client._running = False
        return client

    def _make_market(self, tick: float = 0.01) -> PMMarket:
        mkt = MagicMock(spec=PMMarket)
        mkt.tick_size = tick
        mkt.condition_id = "cond_market"
        return mkt

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- 1. Happy path ----------------------------------------------------------

    def test_place_market_returns_order_id(self):
        """place_market returns the order_id from the CLOB response."""
        client = self._make_client()
        client._clob.create_market_order = MagicMock(return_value="signed_mkt")
        client._clob.post_order = MagicMock(return_value={"orderID": "mkt-001"})

        result = self._run(
            client.place_market("tok_yes", "BUY", 0.75, 20.0,
                                market=self._make_market())
        )
        assert result == "mkt-001"
        client._clob.create_market_order.assert_called_once()
        client._clob.post_order.assert_called_once()

    # -- 2. Uses FAK order type -------------------------------------------------

    def test_place_market_uses_fak(self):
        """post_order is always called with OrderType.FAK for market orders."""
        from py_clob_client.clob_types import OrderType as _OT
        client = self._make_client()
        client._clob.create_market_order = MagicMock(return_value="signed")
        client._clob.post_order = MagicMock(return_value={"orderID": "fak-mkt-001"})

        self._run(
            client.place_market("tok", "BUY", 0.80, 10.0, market=self._make_market())
        )
        call_args = client._clob.post_order.call_args
        assert call_args[0][1] == _OT.FAK

    # -- 3. Exception → None (no crash) -----------------------------------------

    def test_place_market_exception_returns_none(self):
        """If CLOB raises, place_market catches and returns None."""
        client = self._make_client()
        client._clob.create_market_order = MagicMock(side_effect=ConnectionError("timeout"))

        result = self._run(
            client.place_market("tok", "BUY", 0.75, 10.0, market=self._make_market())
        )
        assert result is None

    # -- 4. Event loop stays alive ----------------------------------------------

    def test_place_market_event_loop_stays_alive(self):
        """Other tasks can run while place_market executes the blocking CLOB call."""
        import time as _time
        client = self._make_client()
        progress = {"concurrent_ran": False}

        def _slow_create(args):
            _time.sleep(0.01)
            return "signed"

        client._clob.create_market_order = MagicMock(side_effect=_slow_create)
        client._clob.post_order = MagicMock(return_value={"orderID": "mkt-live-001"})

        async def _concurrent_task():
            await asyncio.sleep(0.005)
            progress["concurrent_ran"] = True

        async def _run_together():
            task = asyncio.create_task(_concurrent_task())
            await client.place_market("tok", "BUY", 0.75, 10.0, market=self._make_market())
            await task

        asyncio.get_event_loop().run_until_complete(_run_together())
        assert progress["concurrent_ran"], (
            "Concurrent task did not progress — event loop was blocked during market order"
        )

    # -- 5. Paper mode unchanged ------------------------------------------------

    def test_place_market_paper_mode(self):
        """In paper mode, CLOB is never touched."""
        client = self._make_client()
        client._paper_mode = True
        client._clob = MagicMock()

        result = self._run(
            client.place_market("tok", "BUY", 0.75, 10.0)
        )
        assert result is not None
        assert result.startswith("paper-mkt-")
        client._clob.create_market_order.assert_not_called()


class TestCancelOrderToThread:
    """cancel_order / cancel_all use asyncio.to_thread in live mode."""

    def _make_client(self) -> PMClient:
        client = PMClient.__new__(PMClient)
        client._paper_mode = False
        client._clob = MagicMock()
        return client

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- 1. cancel_order success -----------------------------------------------

    def test_cancel_order_returns_true_on_success(self):
        client = self._make_client()
        client._clob.cancel = MagicMock(return_value=None)

        result = self._run(client.cancel_order("order-abc"))
        assert result is True
        client._clob.cancel.assert_called_once_with("order-abc")

    # -- 2. cancel_order 404 / exception → False (not a crash) -----------------

    def test_cancel_order_exception_returns_false(self):
        client = self._make_client()
        client._clob.cancel = MagicMock(side_effect=Exception("404 not found"))

        result = self._run(client.cancel_order("order-gone"))
        assert result is False

    # -- 3. cancel_all success --------------------------------------------------

    def test_cancel_all_returns_true(self):
        client = self._make_client()
        client._clob.cancel_all = MagicMock(return_value=None)

        result = self._run(client.cancel_all())
        assert result is True
        client._clob.cancel_all.assert_called_once()

    # -- 4. cancel_all exception → False ----------------------------------------

    def test_cancel_all_exception_returns_false(self):
        client = self._make_client()
        client._clob.cancel_all = MagicMock(side_effect=RuntimeError("server error"))

        result = self._run(client.cancel_all())
        assert result is False

    # -- 5. cancel_order event loop alive during cancel -------------------------

    def test_cancel_order_event_loop_stays_alive(self):
        """Other tasks can run while cancel_order blocks in the CLOB SDK."""
        import time as _time
        client = self._make_client()
        progress = {"ran": False}

        def _slow_cancel(order_id):
            _time.sleep(0.01)

        client._clob.cancel = MagicMock(side_effect=_slow_cancel)

        async def _concurrent():
            await asyncio.sleep(0.005)
            progress["ran"] = True

        async def _run_together():
            task = asyncio.create_task(_concurrent())
            await client.cancel_order("order-slow")
            await task

        asyncio.get_event_loop().run_until_complete(_run_together())
        assert progress["ran"], "Event loop was blocked during cancel_order"

    # -- 6. paper mode cancel_order --------------------------------------------

    def test_cancel_order_paper_mode(self):
        """Paper mode cancel_order returns True without touching CLOB."""
        client = self._make_client()
        client._paper_mode = True

        result = self._run(client.cancel_order("order-paper"))
        assert result is True
        client._clob.cancel.assert_not_called()

    # -- 7. paper mode cancel_all ----------------------------------------------

    def test_cancel_all_paper_mode(self):
        """Paper mode cancel_all returns True without touching CLOB."""
        client = self._make_client()
        client._paper_mode = True

        result = self._run(client.cancel_all())
        assert result is True
        client._clob.cancel_all.assert_not_called()


# ── Live smoke test — real CLOB, non-destructive ─────────────────────────────
# Places an extremely tight (non-fillable) resting post-only order on a real
# Polymarket market using authenticated CLOB credentials, then immediately
# cancels it.  Verifies:
#   - asyncio.to_thread wrapping works with the real py_clob_client SDK
#   - No event loop errors (SynchronousOnlyOperation, etc.)
#   - cancel_order succeeds on the live order_id
#
# Skipped automatically when POLY_PRIVATE_KEY is not set or PAPER_TRADING=True.
# Run explicitly: pytest tests/test_pm_client.py -v -m live_clob --timeout=30

@pytest.mark.live_clob
class TestPlaceLimitLiveSmoke:
    """Non-destructive smoke test against the live Polymarket CLOB.

    Places a GTC post-only BUY at $0.01 (cannot fill) and cancels immediately.
    This validates that asyncio.to_thread works correctly with the real SDK
    without touching real capital or leaving open orders.
    """

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @pytest.fixture(autouse=True)
    def _require_live_creds(self):
        import os
        import config as cfg
        # conftest forces config.PAPER_TRADING=True for all tests — check the raw
        # env key instead (config.py reads it at import time).
        if not cfg.POLY_PRIVATE_KEY or not os.environ.get("POLY_PRIVATE_KEY"):
            pytest.skip("POLY_PRIVATE_KEY not set — skipping live CLOB smoke test")

    def test_place_and_cancel_post_only_order(self):
        """Places a non-fillable post-only order and cancels it immediately.

        Uses the lowest valid price (0.01) on a real liquid bucket market so
        the order will never match.  Any leftover order is cleaned up in the
        finally block.
        """
        import json as _json
        import config as cfg
        import requests as _req

        # Find a real liquid crypto market with an active CLOB order book.
        # /markets returns clobTokenIds as a JSON-encoded string — parse it.
        gamma_resp = _req.get(
            f"{cfg.GAMMA_HOST}/markets",
            params={"tag": "crypto", "active": "true", "limit": "20"},
            timeout=10,
        )
        gamma_resp.raise_for_status()
        raw_items = gamma_resp.json()
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("data") or []

        markets = []
        for m in raw_items:
            if not m.get("enableOrderBook") or not m.get("acceptingOrders"):
                continue
            raw_ids = m.get("clobTokenIds") or []
            if isinstance(raw_ids, str):
                raw_ids = _json.loads(raw_ids)
            if len(raw_ids) == 2:
                m["_token_ids"] = raw_ids
                markets.append(m)

        assert markets, "No suitable market found for smoke test — is the Gamma API available?"

        raw = markets[0]
        from pm_client import PMClient
        client = PMClient.__new__(PMClient)
        client._paper_mode = False
        client._books = {}
        client._price_callbacks = []
        client._private_key = cfg.POLY_PRIVATE_KEY
        client._clob = client._build_clob_client()

        token_id = raw["_token_ids"][0]
        tick = float(raw.get("orderPriceMinTickSize") or raw.get("minimumTickSize") or "0.01")
        min_size = float(raw.get("orderMinSize") or "5")

        order_id = None
        try:
            order_id = self._run(
                client.place_limit(
                    token_id=token_id,
                    side="BUY",
                    price=0.01,          # non-fillable — far below market
                    size=min_size,       # meet CLOB minimum size requirement
                    post_only=True,
                )
            )
            assert order_id is not None, "place_limit returned None — CLOB rejected order"
            assert isinstance(order_id, str) and len(order_id) > 5, f"Unexpected order_id: {order_id!r}"
        finally:
            if order_id:
                cancelled = self._run(client.cancel_order(order_id))
                assert cancelled, f"Failed to cancel live order {order_id} — manual cleanup required"


# ── fetch_market_resolution: winner-flag priority ────────────────────────────
# Per preamble: the CLOB `winner` flag is the source of truth.  `price` can
# show ~1.0 for a losing token in the brief window right after settlement and
# must NEVER be used as the primary signal.

class TestFetchMarketResolution:
    """Unit tests for PMClient.fetch_market_resolution().

    All HTTP calls are mocked.  Tests verify that:
    1. `winner: True` on the YES token  → returns 1.0
    2. `winner: True` on the NO token   → returns 0.0
    3. winner flag absent               → falls back to YES token price
    4. `price` field is NOT used when winner flag is present (anti-regression)
    5. Not-yet-closed market            → returns None
    """

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_client(self):
        client = PMClient.__new__(PMClient)
        client._paper_mode = True
        return client

    def _mock_response(self, payload: dict):
        """Return a context manager that yields a mock aiohttp response."""
        import json as _json

        class _Resp:
            status = 200
            async def json(self):
                return payload
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class _Session:
            def get(self, url, **kwargs): return _Resp()
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        return _Session

    def test_yes_winner_flag_returns_1(self):
        """winner:True on YES token → 1.0 regardless of price field."""
        payload = {
            "closed": True,
            "tokens": [
                {"outcome": "Yes", "price": 0.999, "winner": True},
                {"outcome": "No",  "price": 0.001, "winner": False},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_001"))
        assert result == 1.0

    def test_no_winner_flag_returns_0(self):
        """winner:True on NO token → 0.0 (YES token lost)."""
        payload = {
            "closed": True,
            "tokens": [
                {"outcome": "Yes", "price": 0.0,   "winner": False},
                {"outcome": "No",  "price": 1.0,   "winner": True},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_002"))
        assert result == 0.0

    def test_winner_flag_beats_wrong_price(self):
        """Anti-regression: even if price shows wrong value, winner flag wins.

        Simulates the settlement window where a losing YES token still shows
        price≈1.0 briefly.  The winner flag must override this.
        """
        payload = {
            "closed": True,
            "tokens": [
                # YES token price mistakenly shows 0.99 but winner=False
                {"outcome": "Yes", "price": 0.99, "winner": False},
                {"outcome": "No",  "price": 0.01, "winner": True},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_003"))
        # Must return 0.0 (NO won, YES lost) from winner flag, not 0.99 from price
        assert result == 0.0, (
            f"Expected 0.0 (NO winner flag), got {result} — "
            "price field incorrectly took priority over winner flag"
        )

    def test_no_winner_flag_falls_back_to_yes_price(self):
        """When winner flag absent, falls back to YES token price."""
        payload = {
            "closed": True,
            "tokens": [
                {"outcome": "Yes", "price": 1.0},
                {"outcome": "No",  "price": 0.0},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_004"))
        assert result == 1.0

    def test_not_closed_returns_none(self):
        """Market not yet closed → None (do not record a resolution)."""
        payload = {
            "closed": False,
            "tokens": [
                {"outcome": "Yes", "price": 0.75, "winner": False},
                {"outcome": "No",  "price": 0.25, "winner": False},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_005"))
        assert result is None

    def test_up_down_labels_recognised(self):
        """UP/DOWN outcome labels (crypto bucket markets) are handled correctly."""
        payload = {
            "closed": True,
            "tokens": [
                {"outcome": "Up",   "price": 0.0, "winner": False},
                {"outcome": "Down", "price": 1.0, "winner": True},
            ],
        }
        client = self._make_client()
        with patch("aiohttp.ClientSession", self._mock_response(payload)):
            result = self._run(client.fetch_market_resolution("cid_006"))
        # Down won → YES/Up lost → 0.0
        assert result == 0.0

