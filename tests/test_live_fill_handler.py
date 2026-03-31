"""
tests/test_live_fill_handler.py — Unit tests for LiveFillHandler

Run:  pytest tests/test_live_fill_handler.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import csv
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
config.PAPER_TRADING = True  # overridden per-test where needed

from live_fill_handler import LiveFillHandler
from fill_simulator import FILLS_CSV, FILLS_HEADER
from risk import RiskEngine
from strategies.maker.signals import ActiveQuote


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_market(
    condition_id="mkt_001",
    underlying="BTC",
    fees_enabled=False,
    rebate_pct=0.0,
    token_id_yes="tok_yes",
    token_id_no="tok_no",
):
    m = MagicMock()
    m.condition_id = condition_id
    m.token_id_yes = token_id_yes
    m.token_id_no = token_id_no
    m.underlying = underlying
    m.title = "Will BTC exceed $80k?"
    m.market_type = "bucket_1h"
    m.max_incentive_spread = 0.04
    m.fees_enabled = fees_enabled
    m.rebate_pct = rebate_pct
    return m


def _make_quote(
    market_id="mkt_001",
    token_id="tok_yes",
    side="BUY",
    price=0.45,
    size=100.0,
    order_id="ord_001",
    original_size=100.0,
    score=50.0,
) -> ActiveQuote:
    return ActiveQuote(
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_id=order_id,
        posted_at=time.time(),
        collateral_usd=price * size,
        original_size=original_size,
        score=score,
    )


def _make_handler(paper=True, fills_csv_path=None):
    """Build a LiveFillHandler with all dependencies mocked."""
    config.PAPER_TRADING = paper

    pm = MagicMock()
    pm._pinned_tokens = set()
    pm.cancel_all = AsyncMock(return_value=True)
    pm.get_live_positions = AsyncMock(return_value=[])
    pm.get_live_orders = AsyncMock(return_value=[])
    pm.get_markets = MagicMock(return_value={})
    pm.on_order_fill = MagicMock()

    maker = MagicMock()
    maker.get_active_quotes = MagicMock(return_value={})
    maker._active_quotes = {}
    maker.consume_fill = MagicMock(return_value=None)
    maker.record_fill = MagicMock()
    maker.find_market_for_token = MagicMock(return_value=None)
    maker._reprice_market = AsyncMock()

    risk = RiskEngine()

    monitor = MagicMock()
    monitor.record_entry_deviation = MagicMock()

    handler = LiveFillHandler(pm, maker, risk, monitor)

    # Redirect fills.csv to temp path for tests
    if fills_csv_path is not None:
        import fill_simulator
        handler._fills_csv_path = fills_csv_path  # informational only
        # Patch the module-level FILLS_CSV used inside _process_fill_slice
        # by overriding it via the patch mechanism in tests that need CSV

    return handler, pm, maker, risk, monitor


# ── Paper-mode no-ops ──────────────────────────────────────────────────────────

class TestPaperModeNoOps:
    def test_startup_restore_noop_in_paper_mode(self):
        """startup_restore() must be a no-op when PAPER_TRADING=True."""
        handler, pm, *_ = _make_handler(paper=True)
        asyncio.get_event_loop().run_until_complete(handler.startup_restore())
        pm.cancel_all.assert_not_called()
        pm.get_live_positions.assert_not_called()

    def test_start_noop_in_paper_mode(self):
        """start() must not register any fill callback when PAPER_TRADING=True."""
        handler, pm, *_ = _make_handler(paper=True)
        asyncio.get_event_loop().run_until_complete(handler.start())
        pm.on_order_fill.assert_not_called()


# ── Live mode startup ──────────────────────────────────────────────────────────

class TestLiveModeStartup:
    def setup_method(self):
        config.PAPER_TRADING = False

    def teardown_method(self):
        config.PAPER_TRADING = True

    def test_startup_restore_calls_get_live_orders(self):
        """startup_restore() must fetch resting orders to restore them."""
        handler, pm, *_ = _make_handler(paper=False)
        asyncio.get_event_loop().run_until_complete(handler.startup_restore())
        pm.get_live_orders.assert_called_once()

    def test_startup_restore_calls_get_live_positions(self):
        """startup_restore() must fetch live positions after cancelling orders."""
        handler, pm, *_ = _make_handler(paper=False)
        asyncio.get_event_loop().run_until_complete(handler.startup_restore())
        pm.get_live_positions.assert_called_once()

    def test_start_registers_fill_callback(self):
        """start() must register _on_order_fill with PMClient in live mode."""
        handler, pm, *_ = _make_handler(paper=False)
        asyncio.get_event_loop().run_until_complete(handler.start())
        pm.on_order_fill.assert_called_once_with(handler._on_order_fill)

    def test_get_live_orders_before_position_restore(self):
        """get_live_orders must be called before get_live_positions (order matters)."""
        call_order = []

        async def _orders():
            call_order.append("get_live_orders")
            return []

        async def _positions():
            call_order.append("get_live_positions")
            return []

        handler, pm, *_ = _make_handler(paper=False)
        pm.get_live_orders = _orders
        pm.get_live_positions = _positions

        asyncio.get_event_loop().run_until_complete(handler.startup_restore())
        assert call_order == ["get_live_orders", "get_live_positions"]


# ── Position restore ───────────────────────────────────────────────────────────

class TestPositionRestore:
    def setup_method(self):
        config.PAPER_TRADING = False

    def teardown_method(self):
        config.PAPER_TRADING = True

    def _run_restore(self, positions_data, markets=None):
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        pm.get_live_positions = AsyncMock(return_value=positions_data)
        if markets is not None:
            pm.get_markets = MagicMock(return_value=markets)
        asyncio.get_event_loop().run_until_complete(handler._restore_positions())
        return handler, pm, maker, risk

    def test_empty_positions_no_risk_state(self):
        """No positions returned → risk engine stays empty."""
        handler, pm, maker, risk = self._run_restore([])
        assert risk._positions == {}

    def test_yes_position_restored_to_risk(self):
        """YES token position is correctly restored as side=YES."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {
                "asset": "tok_yes",
                "outcome": "Yes",
                "size": 50.0,
                "avgPrice": 0.45,
                "closed": False,
            }
        ]
        handler, pm, maker, risk = self._run_restore(pos_data, markets)

        assert len(risk._positions) == 1
        pos = next(iter(risk._positions.values()))
        assert pos.side == "YES"
        assert pos.size == 50.0
        assert abs(pos.entry_price - 0.45) < 1e-6
        assert abs(pos.entry_cost_usd - 0.45 * 50.0) < 1e-4

    def test_no_position_restored_to_risk(self):
        """NO token position is correctly restored as side=NO."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {
                "asset": "tok_no",
                "outcome": "No",
                "size": 30.0,
                "avgPrice": 0.60,
                "closed": False,
            }
        ]
        handler, pm, maker, risk = self._run_restore(pos_data, markets)

        assert len(risk._positions) == 1
        pos = next(iter(risk._positions.values()))
        assert pos.side == "NO"
        assert pos.size == 30.0
        # avgPrice is stored as-is (no derivation): entry_price=0.60, entry_cost=0.60*30=18.0
        assert abs(pos.entry_price - 0.60) < 1e-6
        assert abs(pos.entry_cost_usd - 0.60 * 30.0) < 1e-4

    def test_token_pinned_for_restored_position(self):
        """Token is added to pm._pinned_tokens on restore."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {"asset": "tok_yes", "outcome": "Yes", "size": 10.0, "avgPrice": 0.3}
        ]
        handler, pm, *_ = self._run_restore(pos_data, markets)
        assert "tok_yes" in pm._pinned_tokens

    def test_unknown_token_pinned_without_crash(self):
        """Unknown token (no market found) is pinned but does not raise."""
        pos_data = [
            {"asset": "tok_unknown", "outcome": "Yes", "size": 5.0, "avgPrice": 0.4}
        ]
        # Empty markets dict → no match
        handler, pm, maker, risk = self._run_restore(pos_data, markets={})
        assert "tok_unknown" in pm._pinned_tokens
        # Risk engine stays empty (no market to attach position to)
        assert risk._positions == {}

    def test_zero_size_skipped(self):
        """Positions with size=0 are skipped."""
        pos_data = [
            {"asset": "tok_yes", "outcome": "Yes", "size": 0.0, "avgPrice": 0.4}
        ]
        handler, pm, maker, risk = self._run_restore(pos_data, markets={})
        assert risk._positions == {}

    def test_zero_price_skipped(self):
        """Positions with avgPrice=0 are skipped."""
        pos_data = [
            {"asset": "tok_yes", "outcome": "Yes", "size": 5.0, "avgPrice": 0.0}
        ]
        handler, pm, maker, risk = self._run_restore(pos_data, markets={})
        assert risk._positions == {}


# ── Fill event processing ──────────────────────────────────────────────────────

class TestOnOrderFill:
    def setup_method(self):
        config.PAPER_TRADING = False

    def teardown_method(self):
        config.PAPER_TRADING = True

    def _make_event(self, order_id="ord_001", token_id="tok_yes",
                    side="BUY", price="0.45", size_matched="50"):
        return {
            "id": order_id,
            "asset_id": token_id,
            "side": side,
            "price": price,
            "size_matched": size_matched,
            "status": "MATCHED",
        }

    def test_unknown_order_id_is_safe_noop(self):
        """Fill for an order not in _active_quotes completes without error."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        maker.get_active_quotes = MagicMock(return_value={})
        evt = self._make_event(order_id="unknown_order")

        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt))
        maker.consume_fill.assert_not_called()

    def test_valid_fill_triggers_consume(self):
        """Fill for a known order_id calls consume_fill on the maker."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        quote = _make_quote(order_id="ord_001", size=50.0)
        maker.get_active_quotes = MagicMock(return_value={"tok_yes": quote})
        maker._active_quotes = {"tok_yes": quote}
        maker.find_market_for_token = MagicMock(return_value=mkt)

        # consume_fill returns a consumed slice to trigger position open
        consumed = MagicMock()
        consumed.size = 50.0
        consumed.side = "BUY"
        consumed.market_id = "mkt_001"
        consumed.order_id = "ord_001"
        consumed.score = 50.0
        consumed.price = 0.45
        maker.consume_fill = MagicMock(return_value=consumed)

        evt = self._make_event(order_id="ord_001", size_matched="50")
        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt))

        maker.consume_fill.assert_called_once_with("tok_yes", 50.0)

    def test_cumulative_tracking_incremental_only(self):
        """Second event for same order_id is processed as incremental fill only."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        quote = _make_quote(order_id="ord_001", size=100.0, original_size=100.0)
        maker._active_quotes = {"tok_yes": quote}
        maker.get_active_quotes = MagicMock(return_value={"tok_yes": quote})
        maker.find_market_for_token = MagicMock(return_value=mkt)

        consumed = MagicMock()
        consumed.size = 30.0
        consumed.side = "BUY"
        consumed.market_id = "mkt_001"
        consumed.order_id = "ord_001"
        consumed.score = 50.0
        consumed.price = 0.45
        maker.consume_fill = MagicMock(return_value=consumed)

        # First event: cumulative = 30
        evt1 = self._make_event(order_id="ord_001", size_matched="30")
        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt1))
        first_call_size = maker.consume_fill.call_args_list[0][0][1]
        assert abs(first_call_size - 30.0) < 1e-6

        # Second event: cumulative = 50 → incremental = 20
        evt2 = self._make_event(order_id="ord_001", size_matched="50")
        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt2))
        second_call_size = maker.consume_fill.call_args_list[1][0][1]
        assert abs(second_call_size - 20.0) < 1e-6

    def test_duplicate_event_zero_incremental_ignored(self):
        """Repeated identical cumulative total produces no second consume call."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        quote = _make_quote(order_id="ord_001")
        maker._active_quotes = {"tok_yes": quote}
        maker.get_active_quotes = MagicMock(return_value={"tok_yes": quote})
        maker.find_market_for_token = MagicMock(return_value=mkt)

        consumed = MagicMock()
        consumed.size = 50.0
        consumed.side = "BUY"
        consumed.market_id = "mkt_001"
        consumed.order_id = "ord_001"
        consumed.score = 50.0
        consumed.price = 0.45
        maker.consume_fill = MagicMock(return_value=consumed)

        evt = self._make_event(order_id="ord_001", size_matched="50")

        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt))
        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt))

        # Only one consume call (second event has zero incremental)
        assert maker.consume_fill.call_count == 1

    def test_malformed_event_does_not_raise(self):
        """Malformed fill event (bad price) is handled gracefully."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        evt = {"id": "ord_001", "price": "not_a_float", "size_matched": "50"}
        asyncio.get_event_loop().run_until_complete(handler._on_order_fill(evt))
        maker.consume_fill.assert_not_called()


# ── _process_fill_slice ────────────────────────────────────────────────────────

class TestProcessFillSlice:
    def setup_method(self):
        config.PAPER_TRADING = False

    def teardown_method(self):
        config.PAPER_TRADING = True

    def _make_consumed(self, side="BUY", size=50.0, price=0.45):
        consumed = MagicMock()
        consumed.size = size
        consumed.side = side
        consumed.market_id = "mkt_001"
        consumed.order_id = "ord_001"
        consumed.score = 60.0
        consumed.price = price
        return consumed

    def test_yes_position_opened_for_buy(self):
        """BUY fill opens a YES position in the risk engine."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        consumed = self._make_consumed(side="BUY", size=50.0, price=0.45)
        maker.consume_fill = MagicMock(return_value=consumed)
        maker._active_quotes = {}  # quote gone → full fill

        asyncio.get_event_loop().run_until_complete(
            handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 50.0, mkt)
        )

        assert len(risk._positions) == 1
        pos = next(iter(risk._positions.values()))
        assert pos.side == "YES"
        assert pos.size == 50.0
        assert abs(pos.entry_price - 0.45) < 1e-6

    def test_no_position_opened_for_sell(self):
        """SELL fill (ask side) opens a NO position in the risk engine."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        consumed = self._make_consumed(side="SELL", size=40.0, price=0.55)
        maker.consume_fill = MagicMock(return_value=consumed)
        maker._active_quotes = {}

        asyncio.get_event_loop().run_until_complete(
            handler._process_fill_slice(
                "tok_yes_ask", "ord_002", 0.55, "SELL", 40.0, mkt
            )
        )

        assert len(risk._positions) == 1
        pos = next(iter(risk._positions.values()))
        assert pos.side == "NO"
        assert pos.size == 40.0

    def test_risk_blocked_does_not_open_position(self):
        """If can_open() returns False, no position is opened."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        consumed = self._make_consumed(size=50.0, price=0.45)
        maker.consume_fill = MagicMock(return_value=consumed)
        maker._active_quotes = {}

        with patch.object(risk, "can_open", return_value=(False, "test_cap")):
            asyncio.get_event_loop().run_until_complete(
                handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 50.0, mkt)
            )
            assert risk._positions == {}

    def test_reprice_scheduled_after_full_fill(self):
        """When active_quotes no longer has the key, _reprice_market is scheduled."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        consumed = self._make_consumed(size=50.0)
        maker.consume_fill = MagicMock(return_value=consumed)
        # Simulate key gone (full fill consumed): _active_quotes excludes "tok_yes"
        maker._active_quotes = {}

        asyncio.get_event_loop().run_until_complete(
            handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 50.0, mkt)
        )
        maker._reprice_market.assert_called_once_with(mkt)

    def test_no_reprice_on_partial_fill(self):
        """When active_quotes still has the key (partial), no reprice is scheduled."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        quote = _make_quote(size=50.0)  # still open (partial fill)
        consumed = self._make_consumed(size=30.0)
        maker.consume_fill = MagicMock(return_value=consumed)
        # Key still present → partial fill, no reprice yet
        maker._active_quotes = {"tok_yes": quote}

        asyncio.get_event_loop().run_until_complete(
            handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 30.0, mkt)
        )
        maker._reprice_market.assert_not_called()

    def test_consume_returns_none_is_safe(self):
        """If consume_fill returns None (already consumed), no position is opened."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        maker.consume_fill = MagicMock(return_value=None)

        asyncio.get_event_loop().run_until_complete(
            handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 50.0, mkt)
        )
        assert risk._positions == {}

    def test_fills_csv_written(self, tmp_path):
        """A fill record is appended to fills.csv."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        consumed = self._make_consumed(size=50.0, price=0.45)
        maker.consume_fill = MagicMock(return_value=consumed)
        maker._active_quotes = {}

        fake_csv = tmp_path / "fills.csv"
        # Pre-create the CSV with headers so DictReader can parse appended rows
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(FILLS_HEADER)

        import live_fill_handler as lfh_module
        lfh_original = lfh_module.FILLS_CSV
        lfh_module.FILLS_CSV = fake_csv

        try:
            asyncio.get_event_loop().run_until_complete(
                handler._process_fill_slice("tok_yes", "ord_001", 0.45, "BUY", 50.0, mkt)
            )
            assert fake_csv.exists()
            with fake_csv.open() as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert rows[0]["order_side"] == "BUY"
            assert rows[0]["position_side"] == "YES"
            assert float(rows[0]["fill_price"]) == pytest.approx(0.45)
        finally:
            lfh_module.FILLS_CSV = lfh_original
            config.PAPER_TRADING = True

    def test_fill_total_increments(self):
        """_fills_total increments with each processed fill."""
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        mkt = _make_market()
        maker._active_quotes = {}

        for i in range(3):
            consumed = self._make_consumed(size=10.0)
            maker.consume_fill = MagicMock(return_value=consumed)
            asyncio.get_event_loop().run_until_complete(
                handler._process_fill_slice(
                    "tok_yes", f"ord_{i}", 0.45, "BUY", 10.0, mkt
                )
            )

        assert handler._fills_total == 3


# ── YES/NO entry price independence: restore paths ──────────────────────────

class TestYesNoEntryPriceRestore:
    """
    startup_restore and _reconcile_after_reconnect must store avgPrice as-is
    for both YES and NO positions — never derive `1.0 - avg_price` for NO.

    YES and NO are independent CLOBs.  PM avgPrice semantics for NO tokens
    are not confirmed to be in YES-space.  Storing raw avgPrice is always
    safer than deriving a potentially-wrong entry_price.
    """

    def setup_method(self):
        config.PAPER_TRADING = False

    def teardown_method(self):
        config.PAPER_TRADING = True

    def _run_restore(self, positions_data, markets=None):
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        pm.get_live_positions = AsyncMock(return_value=positions_data)
        if markets is not None:
            pm.get_markets = MagicMock(return_value=markets)
        asyncio.get_event_loop().run_until_complete(handler._restore_positions())
        return handler, pm, risk

    def _run_reconcile(self, positions_data, markets=None):
        from live_fill_handler import LiveFillHandler
        handler, pm, maker, risk, monitor = _make_handler(paper=False)
        pm.get_live_positions = AsyncMock(return_value=positions_data)
        if markets is not None:
            pm.get_markets = MagicMock(return_value=markets)
        asyncio.get_event_loop().run_until_complete(handler._reconcile_after_reconnect())
        return handler, pm, risk

    def test_startup_restore_no_entry_price_not_derived(self):
        """avg_price=0.18 for a NO position must be stored as entry_price=0.18,
        not derived as 1.0 - 0.18 = 0.82."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {
                "asset": "tok_no",
                "outcome": "No",
                "size": 20.0,
                "avgPrice": 0.18,   # raw PM avgPrice for NO token
                "closed": False,
            }
        ]
        handler, pm, risk = self._run_restore(pos_data, markets)

        pos = next(iter(risk._positions.values()))
        assert pos.side == "NO"
        assert abs(pos.entry_price - 0.18) < 1e-6, (
            f"entry_price must be avg_price=0.18 (not 1-0.18=0.82), got {pos.entry_price}"
        )

    def test_startup_restore_yes_entry_price_unchanged(self):
        """YES positions must still use avg_price directly (unchanged behavior)."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {
                "asset": "tok_yes",
                "outcome": "Yes",
                "size": 20.0,
                "avgPrice": 0.82,
                "closed": False,
            }
        ]
        handler, pm, risk = self._run_restore(pos_data, markets)

        pos = next(iter(risk._positions.values()))
        assert pos.side == "YES"
        assert abs(pos.entry_price - 0.82) < 1e-6, (
            f"YES entry_price must be avg_price=0.82, got {pos.entry_price}"
        )

    def test_reconcile_no_entry_price_not_derived(self):
        """reconcile_after_reconnect: avg_price=0.18 for NO must be stored as 0.18."""
        mkt = _make_market()
        markets = {mkt.condition_id: mkt}
        pos_data = [
            {
                "asset": "tok_no",
                "outcome": "No",
                "size": 15.0,
                "avgPrice": 0.18,
                "closed": False,
            }
        ]
        handler, pm, risk = self._run_reconcile(pos_data, markets)

        assert len(risk._positions) > 0, "Position should have been imported by reconcile"
        pos = next(iter(risk._positions.values()))
        assert pos.side == "NO"
        assert abs(pos.entry_price - 0.18) < 1e-6, (
            f"entry_price must be avg_price=0.18 (not derived 0.82), got {pos.entry_price}"
        )
