"""
tests/test_fill_simulator.py — Unit tests for fill_simulator.py

Run:  pytest tests/test_fill_simulator.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

import config
config.PAPER_TRADING = True
config.STRATEGY_MAKER_ENABLED = True

from fill_simulator import FillSimulator
from strategies.maker.strategy import MakerStrategy
from strategies.maker.signals import ActiveQuote
from monitor import PositionMonitor
from risk import RiskEngine
from market_data.pm_client import OrderBookSnapshot


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_book(best_bid=None, best_ask=None) -> OrderBookSnapshot:
    book = OrderBookSnapshot(token_id="tok_yes")
    if best_bid is not None:
        book.bids = [(best_bid, 100.0)]
    if best_ask is not None:
        book.asks = [(best_ask, 100.0)]
    return book


def _make_simulator(fill_prob_base=0.99):
    """Create a FillSimulator with all dependencies mocked."""
    pm = MagicMock()
    pm._markets = {}
    pm._books = {}
    pm.place_limit = AsyncMock(return_value="paper-sell-001")

    maker = MagicMock()
    maker.get_active_quotes = MagicMock(return_value={})
    maker.consume_fill = MagicMock(return_value=None)
    maker.record_fill = MagicMock()
    maker.get_hl_mid = MagicMock(return_value=None)
    maker._rebalance_hedge = AsyncMock()  # hedge rebalance triggered on every fill
    maker._reprice_market = AsyncMock()   # immediate reprice scheduled after full fill

    risk = RiskEngine()
    monitor = MagicMock()
    monitor.record_entry_deviation = MagicMock()

    sim = FillSimulator(pm, maker, risk, monitor)
    # Override base probability so tests are deterministic
    config.PAPER_FILL_PROB_BASE = fill_prob_base
    config.PAPER_FILL_PROB_NEW_MARKET = fill_prob_base
    return sim, pm, maker, risk, monitor


def _make_market(market_id="mkt_001", underlying="BTC", age_seconds=9000,
                 fees_enabled=False):
    """Create a mock PMMarket."""
    m = MagicMock()
    m.condition_id = market_id
    m.token_id_yes = "tok_yes"
    m.token_id_no = "tok_no"
    m.underlying = underlying
    m.title = "Will BTC be above $85k?"
    m.market_type = "bucket_1h"
    m.is_fee_free = not fees_enabled
    m.fees_enabled = fees_enabled
    m.max_incentive_spread = 0.04
    m.tick_size = 0.01
    m.discovered_at = time.time() - age_seconds
    m.end_date = None
    return m


# ── _is_crossed ────────────────────────────────────────────────────────────────
# Logic: our quote is "live" when it is competitive at the best touch.
#   BUY  at P: competitive when P >= best_bid  (we are the best / tied-best bid)
#   SELL at P: competitive when P <= best_ask  (we are the best / tied-best ask)

class TestIsCrossed:
    def setup_method(self):
        self.sim, *_ = _make_simulator()

    # BUY cases ────────────────────────────────────────────────────────────────
    def test_buy_competitive_when_bid_equals_best_bid(self):
        """Our bid == book best_bid → we are tied-best, should fill."""
        book = _make_book(best_bid=0.15)
        assert self.sim._is_crossed("BUY", 0.15, book) is True

    def test_buy_competitive_when_bid_above_best_bid(self):
        """Our bid > book best_bid → we are the best bid, should fill."""
        book = _make_book(best_bid=0.13)
        assert self.sim._is_crossed("BUY", 0.15, book) is True

    def test_buy_not_competitive_when_bid_below_best_bid(self):
        """Our bid is more than queue-tolerance below best_bid → should not fill."""
        book = _make_book(best_bid=0.20)   # 0.20 - 0.15 = 0.05 > 0.03 tolerance
        assert self.sim._is_crossed("BUY", 0.15, book) is False

    def test_buy_not_competitive_when_no_bid(self):
        """No bids in book → cannot be competitive."""
        book = _make_book()
        assert self.sim._is_crossed("BUY", 0.15, book) is False

    # SELL cases ───────────────────────────────────────────────────────────────
    def test_sell_competitive_when_ask_equals_best_ask(self):
        """Our ask == book best_ask → we are tied-best, should fill."""
        book = _make_book(best_ask=0.17)
        assert self.sim._is_crossed("SELL", 0.17, book) is True

    def test_sell_competitive_when_ask_below_best_ask(self):
        """Our ask < book best_ask → we are the best ask, should fill."""
        book = _make_book(best_ask=0.19)
        assert self.sim._is_crossed("SELL", 0.17, book) is True

    def test_sell_not_competitive_when_ask_above_best_ask(self):
        """Our ask is more than queue-tolerance above best_ask → should not fill."""
        book = _make_book(best_ask=0.12)   # 0.17 - 0.12 = 0.05 > 0.03 tolerance
        assert self.sim._is_crossed("SELL", 0.17, book) is False

    def test_sell_not_competitive_when_no_ask(self):
        """No asks in book → cannot be competitive."""
        book = _make_book()
        assert self.sim._is_crossed("SELL", 0.17, book) is False


# ── _hl_move_pct ──────────────────────────────────────────────────────────────
# The CLOB taker model replaced the old flat _fill_probability with an
# inline taker-arrival model.  _hl_move_pct() is the shared helper that
# drives adverse-selection detection in _sweep.

class TestHlMovePct:
    def setup_method(self):
        self.sim, _, maker, *_ = _make_simulator()
        self.maker = maker

    def test_returns_none_when_no_prev_mid(self):
        self.sim._prev_hl_mids.clear()
        assert self.sim._hl_move_pct("BTC") is None

    def test_returns_none_when_current_mid_unavailable(self):
        self.sim._prev_hl_mids["BTC"] = 80000.0
        self.maker.get_hl_mid = MagicMock(return_value=None)
        assert self.sim._hl_move_pct("BTC") is None

    def test_positive_move(self):
        self.sim._prev_hl_mids["BTC"] = 80000.0
        self.maker.get_hl_mid = MagicMock(return_value=80400.0)
        pct = self.sim._hl_move_pct("BTC")
        assert pct == pytest.approx(0.005)  # +0.5%

    def test_negative_move(self):
        self.sim._prev_hl_mids["BTC"] = 80000.0
        self.maker.get_hl_mid = MagicMock(return_value=79600.0)
        pct = self.sim._hl_move_pct("BTC")
        assert pct == pytest.approx(-0.005)  # -0.5%

    def test_zero_prev_mid_returns_none(self):
        self.sim._prev_hl_mids["BTC"] = 0.0
        self.maker.get_hl_mid = MagicMock(return_value=80000.0)
        assert self.sim._hl_move_pct("BTC") is None


# ── _on_fill state machine ─────────────────────────────────────────────────────

class TestOnFillStateMachine:
    """
    _on_fill must:
    1. consume_fill the quote (returns None if already taken)
    2. open a Position in the risk engine
    3. call record_entry_deviation on the monitor
    4. call record_fill on the maker (inventory updated)
    """

    def setup_method(self):
        from datetime import datetime, timezone
        self.market = _make_market()

        self.pm = MagicMock()
        self.pm._markets = {"mkt_001": self.market}
        self.pm._books = {}
        self.pm.place_limit = AsyncMock(return_value="paper-exit-001")

        self.quote = ActiveQuote(
            market_id="mkt_001",
            token_id="tok_yes",
            side="BUY",
            price=0.13,
            size=50.0,
            order_id="paper-001",
        )

        self.maker = MagicMock()
        self.maker.consume_fill = MagicMock(return_value=self.quote)
        self.maker.record_fill = MagicMock()
        self.maker.get_hl_mid = MagicMock(return_value=None)
        self.maker.schedule_hedge_rebalance = MagicMock()  # hedge rebalance on fill
        self.maker._reprice_market = AsyncMock()            # immediate reprice after full fill
        # No remainder after fill by default (prevents tiny-remainder path)
        self.maker.get_active_quotes = MagicMock(return_value={})

        self.risk = RiskEngine()
        self.monitor = MagicMock()
        self.monitor.record_entry_deviation = MagicMock()

        self.sim = FillSimulator(self.pm, self.maker, self.risk, self.monitor)

    def test_consume_fill_called(self):
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        self.maker.consume_fill.assert_called_once_with("tok_yes", 50.0)

    def test_position_opened_in_risk(self):
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        positions = self.risk.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.strategy == "maker"
        assert pos.side == "YES"
        assert pos.entry_price == pytest.approx(0.13)
        assert pos.size == pytest.approx(50.0)

    def test_monitor_deviation_recorded(self):
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        self.monitor.record_entry_deviation.assert_called_once_with(
            "mkt_001",
            pytest.approx(0.02),  # max_incentive_spread/2 = 0.04/2
        )

    def test_inventory_updated(self):
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        # Inventory tracks USD entry cost (entry_cost_usd), NOT contract count.
        # BUY YES: fill_cost_usd = price × size = 0.13 × 50 = 6.5
        # Using USD ensures the inventory-skew coefficient (1c per $100 USD) is correct.
        expected_usd = pytest.approx(0.13 * 50.0, abs=0.001)   # 6.5 USD
        self.maker.record_fill.assert_called_once_with(
            "mkt_001", "BTC", "YES_BUY", expected_usd
        )

    def test_hedge_rebalanced_after_fill(self):
        """After inventory is updated, _schedule_hedge_rebalance must be called."""
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        self.maker.schedule_hedge_rebalance.assert_called_once_with("BTC")

    def test_already_consumed_is_noop(self):
        """If consume_fill returns None (another sweep took it), do nothing."""
        self.maker.consume_fill = MagicMock(return_value=None)
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        # No position should have been opened
        assert len(self.risk.get_open_positions()) == 0
        self.monitor.record_entry_deviation.assert_not_called()

    def test_no_buy_creates_no_position(self):
        """BUY NO fill (ask key with _ask suffix) → position side = NO."""
        self.quote.side = "BUY"   # ask leg is now BUY NO, not SELL YES
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes_ask", self.quote, self.market, 50.0)
        )
        positions = self.risk.get_open_positions()
        assert positions[0].side == "NO"

    def test_fills_total_increments(self):
        assert self.sim._fills_total == 0
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 50.0)
        )
        assert self.sim._fills_total == 1

    def test_sub_min_remainder_stays_alive(self):
        """CLOB-accurate: a sub-minimum remainder must NOT be auto-consumed.

        In a real CLOB, a partially-filled order sits in the book until it is
        explicitly cancelled (by a reprice cycle) or filled by another taker.
        The fill simulator must NOT auto-flush sub-minimum remainders.
        """
        remainder_quote = ActiveQuote(
            market_id="mkt_001", token_id="tok_yes", side="BUY",
            price=0.13, size=15.0, order_id="paper-001",
            collateral_usd=round(0.13 * 15, 4),  # 1.95 USD < MAKER_QUOTE_SIZE_MIN=50
        )
        # consume_fill returns a 35-unit slice
        partial_quote = ActiveQuote(
            market_id="mkt_001", token_id="tok_yes", side="BUY",
            price=0.13, size=35.0, order_id="paper-001",
        )
        self.maker.consume_fill = MagicMock(return_value=partial_quote)
        # Remainder is still present after the primary fill
        self.maker.get_active_quotes = MagicMock(return_value={"tok_yes": remainder_quote})

        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", self.quote, self.market, 35.0)
        )

        # consume_fill must only be called ONCE — no auto-flush of the remainder.
        assert self.maker.consume_fill.call_count == 1, (
            "Sub-minimum remainder must not be auto-consumed; "
            "only the primary fill slice should call consume_fill"
        )
        # Only the primary fill (35 contracts) was processed
        positions = self.risk.get_open_positions()
        total_size = sum(p.size for p in positions)
        assert total_size == pytest.approx(35.0)
        assert self.sim._fills_total == 1


# ── Capital bounds enforcement ─────────────────────────────────────────────────

class TestCapitalBoundsEnforcement:
    """
    Accumulated partial fills must never push per-market entry_cost_usd above
    MAX_PM_EXPOSURE_PER_MARKET (which equals MAKER_QUOTE_SIZE_MAX = $250).

    The fix: MAX_PM_EXPOSURE_PER_MARKET == MAKER_QUOTE_SIZE_MAX so can_open()
    blocks any fill that would push the accumulated total past $250.
    """

    def setup_method(self):
        self.sim, self.pm, self.maker, self.risk, self.monitor = _make_simulator()
        self.market = _make_market(market_id="mkt_bounds")
        self.pm._markets = {self.market.condition_id: self.market}
        # Ensure config limits are at their intended values
        self._orig_per_market = config.MAX_PM_EXPOSURE_PER_MARKET
        self._orig_max = config.MAKER_SPREAD_SIZE_MAX
        # Pin BOTH to 250 for test isolation: the relationship-integrity test
        # (test_per_market_limit_matches_quote_size_max) still passes because
        # they're equal; the fill-blocking tests need a known cap to assert against
        # regardless of what config_overrides.json sets MAKER_SPREAD_SIZE_MAX to.
        config.MAKER_SPREAD_SIZE_MAX = 250.0
        config.MAX_PM_EXPOSURE_PER_MARKET = 250.0
        self._orig_total = config.MAX_TOTAL_PM_EXPOSURE
        config.MAX_TOTAL_PM_EXPOSURE = 2500.0  # well above test amounts

    def teardown_method(self):
        config.MAX_PM_EXPOSURE_PER_MARKET = self._orig_per_market
        config.MAKER_SPREAD_SIZE_MAX = self._orig_max
        config.MAX_TOTAL_PM_EXPOSURE = self._orig_total

    def _fill_slice(self, price: float, size: float, side: str = "BUY") -> None:
        """Simulate one fill slice by calling _process_fill_slice directly."""
        filled_quote = ActiveQuote(
            market_id="mkt_bounds", token_id="tok_yes",
            side=side, price=price, size=size,
            collateral_usd=round(price * size, 4),
            original_size=size,
        )
        self.maker.consume_fill = MagicMock(return_value=filled_quote)
        self.maker.get_active_quotes = MagicMock(return_value={})
        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", filled_quote, self.market, size)
        )

    def test_accumulated_capital_never_exceeds_max(self):
        """
        Simulate five partial fills of ~$50 each; can_open should block the
        fill that would push total entry_cost_usd past MAX_PM_EXPOSURE_PER_MARKET.
        The final accumulated position must not exceed the cap.
        """
        price = 0.50
        fill_size = 100  # 100 contracts × $0.50 = $50 per slice

        # First fill: $50 → total $50 (≤ $250 cap)
        self._fill_slice(price, fill_size)
        # Second fill: $50 → total $100
        self._fill_slice(price, fill_size)
        # Third fill: $50 → total $150
        self._fill_slice(price, fill_size)
        # Fourth fill: $50 → total $200
        self._fill_slice(price, fill_size)
        # Fifth fill: $50 → total $250 — right at the edge, should still be allowed
        self._fill_slice(price, fill_size)

        positions = self.risk.get_open_positions()
        assert len(positions) == 1, "All fills for the same market should merge into one position"
        pos = positions[0]
        assert pos.entry_cost_usd <= config.MAX_PM_EXPOSURE_PER_MARKET, (
            f"entry_cost_usd={pos.entry_cost_usd:.2f} exceeds "
            f"MAX_PM_EXPOSURE_PER_MARKET={config.MAX_PM_EXPOSURE_PER_MARKET}"
        )

        # A sixth fill that would push past $250 must be blocked
        ok, reason = self.risk.can_open(
            "mkt_bounds", 1.0, strategy="maker", underlying="BTC"
        )
        assert not ok, "Sixth fill should be blocked once per-market limit is saturated"

    def test_single_fill_does_not_create_above_max_position(self):
        """
        Even a single oversized fill slice cannot push entry_cost_usd above MAX.
        (can_open receives fill_cost_usd = price × contracts and must reject it.)
        """
        # Attempt a fill worth $300 on an empty market (no existing exposure)
        oversized_quote = ActiveQuote(
            market_id="mkt_bounds", token_id="tok_yes",
            side="BUY", price=0.50, size=600,  # 600 × $0.50 = $300 > $250
            collateral_usd=300.0, original_size=600,
        )
        self.maker.consume_fill = MagicMock(return_value=oversized_quote)
        self.maker.get_active_quotes = MagicMock(return_value={})

        asyncio.get_event_loop().run_until_complete(
            self.sim._on_fill("tok_yes", oversized_quote, self.market, 600)
        )

        # can_open should have rejected fill_cost_usd=$300 > MAX=$250
        positions = self.risk.get_open_positions()
        assert len(positions) == 0, (
            "Oversized fill ($300 > $250 max) should have been blocked by can_open"
        )

    def test_per_market_limit_matches_quote_size_max(self):
        """
        config.MAX_PM_EXPOSURE_PER_MARKET must equal config.MAKER_QUOTE_SIZE_MAX
        so per-market accumulated capital is bounded by the single-quote ceiling.
        """
        assert config.MAX_PM_EXPOSURE_PER_MARKET == config.MAKER_SPREAD_SIZE_MAX, (
            f"MAX_PM_EXPOSURE_PER_MARKET={config.MAX_PM_EXPOSURE_PER_MARKET} "
            f"!= MAKER_SPREAD_SIZE_MAX={config.MAKER_SPREAD_SIZE_MAX}. "
            "Set MAX_PM_EXPOSURE_PER_MARKET = MAKER_SPREAD_SIZE_MAX in config.py."
        )


# ── Regression: BUG-3 — session stats reset on start ──────────────────────────

import fill_simulator as _fill_sim_module


class TestFillSessionStatsReset:
    """
    BUG-3 regression: FillSimulator.start() must call reset_fill_session_stats()
    so that stop→start cycles within the same process produce a clean slate.
    """

    def setup_method(self):
        _fill_sim_module.reset_fill_session_stats()

    def test_initial_state_is_zeros(self):
        s = _fill_sim_module.get_fill_session_stats()
        assert s["adverse_triggers_session"] == 0
        assert s["hl_max_move_pct_session"] == 0.0

    def test_reset_clears_mutated_counters(self):
        _fill_sim_module._adverse_triggers_session = 7
        _fill_sim_module._hl_max_move_pct_session = 0.05
        _fill_sim_module.reset_fill_session_stats()
        s = _fill_sim_module.get_fill_session_stats()
        assert s["adverse_triggers_session"] == 0
        assert s["hl_max_move_pct_session"] == 0.0

    def test_start_resets_session_counters(self):
        """start() must reset counters so stop→start gives a clean slate (BUG-3)."""
        # Simulate counters left over from a previous session
        _fill_sim_module._adverse_triggers_session = 42
        _fill_sim_module._hl_max_move_pct_session = 0.99

        sim, _pm, _maker, _risk, _monitor = _make_simulator()
        asyncio.get_event_loop().run_until_complete(sim.start())

        s = _fill_sim_module.get_fill_session_stats()
        assert s["adverse_triggers_session"] == 0, (
            "start() must call reset_fill_session_stats() to clear prior session data"
        )
        assert s["hl_max_move_pct_session"] == 0.0


# ── YES/NO CLOB independence: fill_simulator._sweep ──────────────────────────

class TestNoOrderBookIndependence:
    """
    _sweep must fetch the NO CLOB book for BUY NO orders, not mirror the YES
    book via a `1.0 - price` derivation.

    Key invariant: YES_mid=0.30 and NO_mid=0.82 are independent.
    A BUY NO quote at 0.82 should cross the NO book (best_ask=0.82), not
    the YES book (which would require a derived price of 0.18 to cross).
    """

    def _make_no_buy_quote(
        self,
        market_id="mkt_001",
        price=0.82,
        size=50.0,
    ) -> ActiveQuote:
        """Return a BUY NO quote.  The key used in get_active_quotes must end
        with '_ask' so that is_no_buy=True in _sweep."""
        return ActiveQuote(
            market_id=market_id,
            token_id="tok_no",
            side="BUY",
            price=price,
            size=size,
            order_id="paper-no-001",
        )

    def test_no_buy_order_uses_no_book_for_touch_check(self):
        """BUY NO order must cross against the NO CLOB book, not the YES book."""
        sim, pm, maker, risk, monitor = _make_simulator(fill_prob_base=0.99)
        market = _make_market()
        pm._markets = {"mkt_001": market}

        # YES book: best_ask = 0.31 (irrelevant for NO order)
        yes_book = _make_book(best_bid=0.29, best_ask=0.31)
        # NO book: best_ask = 0.82 — the BUY NO quote at 0.82 should cross this
        no_book = _make_book(best_bid=0.80, best_ask=0.82)
        pm._books = {"tok_yes": yes_book, "tok_no": no_book}

        quote = self._make_no_buy_quote(price=0.82)
        # Key ends with '_ask' → interpreted as BUY NO by _sweep
        maker.get_active_quotes = MagicMock(return_value={"tok_no_ask": quote})
        maker.consume_fill = MagicMock(return_value=quote)
        maker.record_fill = MagicMock()
        maker.schedule_hedge_rebalance = MagicMock()
        maker.get_hl_mid = MagicMock(return_value=None)
        maker._reprice_market = AsyncMock()

        asyncio.get_event_loop().run_until_complete(sim._sweep())

        # Fill should have been processed using the real NO book
        maker.consume_fill.assert_called_once(), "NO order must be filled via NO CLOB touch-check"

    def test_no_buy_order_skips_when_no_book_missing(self):
        """When the NO CLOB book is absent, BUY NO order must be skipped entirely
        (no fill, no derivation)."""
        sim, pm, maker, risk, monitor = _make_simulator(fill_prob_base=0.99)
        market = _make_market()
        pm._markets = {"mkt_001": market}

        # Only YES book present; NO book absent
        yes_book = _make_book(best_bid=0.29, best_ask=0.31)
        pm._books = {"tok_yes": yes_book}   # tok_no intentionally absent

        quote = self._make_no_buy_quote(price=0.82)
        maker.get_active_quotes = MagicMock(return_value={"tok_no_ask": quote})
        maker.consume_fill = MagicMock(return_value=quote)
        maker.record_fill = MagicMock()

        asyncio.get_event_loop().run_until_complete(sim._sweep())

        # No fill should occur because the NO book is absent
        maker.consume_fill.assert_not_called(), "BUY NO order must be skipped when NO book is absent"
        assert len(risk.get_open_positions()) == 0

    def test_no_buy_does_not_use_derived_yes_price(self):
        """Regression: price 1 - 0.82 = 0.18 must NEVER be used against the YES book.
        If YES best_ask is 0.31 and a derived price of 0.18 were used on the SELL side,
        it would NOT cross (0.18 < 0.31) and the order would be skipped even when the
        actual NO book WOULD cross.  Test that the order fills correctly via the real
        NO book even when the derived price would have failed the touch check."""
        sim, pm, maker, risk, monitor = _make_simulator(fill_prob_base=0.99)
        market = _make_market()
        pm._markets = {"mkt_001": market}

        # YES book: best_ask=0.31 — derived price 0.18 would fail the SELL check here
        yes_book = _make_book(best_bid=0.29, best_ask=0.31)
        # NO book: best_ask=0.82 — actual NO quote at 0.82 crosses this
        no_book = _make_book(best_bid=0.80, best_ask=0.82)
        pm._books = {"tok_yes": yes_book, "tok_no": no_book}

        quote = self._make_no_buy_quote(price=0.82)
        maker.get_active_quotes = MagicMock(return_value={"tok_no_ask": quote})
        maker.consume_fill = MagicMock(return_value=quote)
        maker.record_fill = MagicMock()
        maker.schedule_hedge_rebalance = MagicMock()
        maker.get_hl_mid = MagicMock(return_value=None)
        maker._reprice_market = AsyncMock()

        asyncio.get_event_loop().run_until_complete(sim._sweep())

        # If derivation were used, the order would be skipped (derived 0.18 < YES best_ask 0.31).
        # With the fix, the NO book is checked directly and the order fills.
        maker.consume_fill.assert_called_once(), (
            "BUY NO at 0.82 must fill via NO book — not be skipped due to derived YES price check"
        )
