"""
tests/test_integration.py — Integration tests for Perp_Hyper_Arb.

Covers cross-component boundaries:
  - Fill → entry_cost_usd (capital vs face value)
  - _position_delta_usd (sign and magnitude)
  - _rebalance_hedge → direction, size, guard on no HL mid
  - trades.csv schema (underlying column)
  - CSV migration (stale header → backup)
  - get_state total_pm_capital_deployed

Run:
    python -m pytest tests/test_integration.py -v
    python -m pytest tests/test_integration.py -v -m p0   # critical only
"""

import asyncio
import csv
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call


def _run(coro):
    """Run a coroutine in a fresh, isolated event loop without polluting the thread's loop.

    asyncio.run() closes the running loop after completion, which causes
    subsequent tests using asyncio.get_event_loop() to fail (Python 3.10+).
    Using a fresh loop keeps event loop state isolated per call.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

# Force paper mode — prevents any real network calls
config.PAPER_TRADING = True

import risk
from risk import RiskEngine, Position, TRADES_HEADER
from strategies.maker.strategy import MakerStrategy
from market_data.hl_client import BBO


# ── Helpers ───────────────────────────────────────────────────────────────────

def _yes_position(
    market_id: str = "mkt_btc_001",
    underlying: str = "BTC",
    size: float = 600.0,
    entry_price: float = 0.10,
) -> Position:
    """YES position: entry_cost_usd = entry_price * size."""
    cost = round(entry_price * size, 6)
    return Position(
        market_id=market_id,
        market_type="bucket_daily",
        underlying=underlying,
        side="YES",
        size=size,
        entry_price=entry_price,
        strategy="maker",
        entry_cost_usd=cost,
    )


def _no_position(
    market_id: str = "mkt_btc_002",
    underlying: str = "BTC",
    size: float = 600.0,
    entry_price: float = 0.90,  # 90¢ YES = 10¢ NO token
) -> Position:
    """NO position: entry_cost_usd = (1 - entry_price) * size."""
    cost = round((1.0 - entry_price) * size, 6)
    return Position(
        market_id=market_id,
        market_type="bucket_daily",
        underlying=underlying,
        side="NO",
        size=size,
        entry_price=entry_price,
        strategy="maker",
        entry_cost_usd=cost,
    )


def _make_maker(hl_mock) -> MakerStrategy:
    """Build a MakerStrategy wired to a mock HL and PM client."""
    pm_mock = MagicMock()
    pm_mock.get_markets.return_value = {}
    pm_mock.get_mid.return_value = None
    pm_mock.on_price_change = MagicMock()
    pm_mock.on_bbo_update = MagicMock()

    engine = RiskEngine()
    strategy = MakerStrategy(pm=pm_mock, hl=hl_mock, risk=engine)
    return strategy


def _mock_hl(hl_mid: float = 2000.0) -> MagicMock:
    """Mock HLClient that tracks place_hedge/close_hedge calls."""
    hl = MagicMock()
    hl.get_mid = MagicMock(return_value=hl_mid)
    hl.place_hedge = AsyncMock(return_value=True)
    hl.close_hedge = AsyncMock(return_value=True)
    hl.on_bbo_update = MagicMock()
    return hl


# ── IT-01: YES entry_cost_usd = price × size ──────────────────────────────────

@pytest.mark.p0
class TestCapitalVsFaceValue:
    def test_yes_entry_cost_is_price_times_size(self):
        """IT-01: 600 YES at 10¢ → $60 cost, not $600."""
        pos = _yes_position(size=600.0, entry_price=0.10)
        assert pos.entry_cost_usd == pytest.approx(60.0), (
            f"Expected entry_cost_usd=60, got {pos.entry_cost_usd}"
        )

    def test_yes_entry_cost_at_90_cents(self):
        """IT-01: 600 YES at 90¢ → $540 cost."""
        pos = _yes_position(size=600.0, entry_price=0.90)
        assert pos.entry_cost_usd == pytest.approx(540.0)

    def test_no_entry_cost_is_one_minus_price_times_size(self):
        """IT-02: 600 NO at 90¢ YES price → 10¢ NO token → $60 cost."""
        pos = _no_position(size=600.0, entry_price=0.90)
        assert pos.entry_cost_usd == pytest.approx(60.0), (
            f"Expected entry_cost_usd=60, got {pos.entry_cost_usd}"
        )

    def test_no_entry_cost_at_10_cents_yes(self):
        """IT-02: 600 NO at 10¢ YES price → 90¢ NO token → $540 cost."""
        pos = _no_position(size=600.0, entry_price=0.10)
        assert pos.entry_cost_usd == pytest.approx(540.0)

    def test_face_value_vs_capital_differ_at_extremes(self):
        """Face value (size) is always 600; capital varies with price."""
        pos_cheap = _yes_position(size=600.0, entry_price=0.10)
        pos_pricy = _yes_position(size=600.0, entry_price=0.90)
        assert pos_cheap.size == pos_pricy.size == 600.0
        assert pos_cheap.entry_cost_usd != pos_pricy.entry_cost_usd


# ── IT-03: _position_delta_usd uses capital ────────────────────────────────────

@pytest.mark.p0
class TestPositionDeltaUsd:
    def setup_method(self):
        self.hl = _mock_hl()
        self.strategy = _make_maker(self.hl)

    def _open(self, pos: Position) -> None:
        self.strategy._risk.open_position(pos)

    def test_yes_delta_is_positive(self):
        """IT-03: YES position contributes +entry_cost_usd."""
        self._open(_yes_position(size=600.0, entry_price=0.10))  # cost=$60
        delta = self.strategy._position_delta_usd("BTC")
        assert delta == pytest.approx(60.0)

    def test_no_delta_is_negative(self):
        """IT-03: NO position contributes -entry_cost_usd."""
        self._open(_no_position(size=600.0, entry_price=0.90))  # cost=$60
        delta = self.strategy._position_delta_usd("BTC")
        assert delta == pytest.approx(-60.0)

    def test_delta_sums_multiple_yes_positions(self):
        """IT-03: Two YES positions sum their capital."""
        self._open(_yes_position("m1", size=600.0, entry_price=0.10))  # $60
        self._open(_yes_position("m2", size=600.0, entry_price=0.40))  # $240
        delta = self.strategy._position_delta_usd("BTC")
        assert delta == pytest.approx(300.0)

    def test_delta_uses_capital_not_face_value(self):
        """IT-03: 600 contracts at 10¢ → delta=$60, not $600."""
        self._open(_yes_position(size=600.0, entry_price=0.10))
        delta = self.strategy._position_delta_usd("BTC")
        assert delta != pytest.approx(600.0)
        assert delta == pytest.approx(60.0)

    def test_opposite_positions_cancel(self):
        """IT-06: $300 YES + $300 NO → net=0."""
        # YES: 600 contracts at 50¢ = $300
        self._open(_yes_position("m1", size=600.0, entry_price=0.50))
        # NO: 600 contracts at 50¢ YES = 50¢ NO = $300 cost
        self._open(_no_position("m2", size=600.0, entry_price=0.50))
        delta = self.strategy._position_delta_usd("BTC")
        assert abs(delta) < 1.0  # effectively zero

    def test_closed_positions_excluded(self):
        """IT-03: Closed positions should not contribute to delta."""
        pos = _yes_position("m1", size=600.0, entry_price=0.50)  # $300
        self.strategy._risk.open_position(pos)
        self.strategy._risk.close_position("m1", exit_price=0.55)
        delta = self.strategy._position_delta_usd("BTC")
        assert delta == pytest.approx(0.0)

    def test_delta_isolated_by_coin(self):
        """IT-03: ETH positions should not affect BTC delta."""
        self._open(_yes_position("m1", underlying="BTC", size=600.0, entry_price=0.50))
        self._open(_yes_position("m2", underlying="ETH", size=600.0, entry_price=0.50))
        assert self.strategy._position_delta_usd("BTC") == pytest.approx(300.0)
        assert self.strategy._position_delta_usd("ETH") == pytest.approx(300.0)


# ── IT-04 / IT-05 / IT-15 / IT-16: Hedge placement ───────────────────────────

@pytest.mark.p0
class TestHedgePlacement:
    def setup_method(self):
        self._orig_hedge_enabled = config.MAKER_HEDGE_ENABLED
        config.MAKER_HEDGE_ENABLED = True
        self.hl_mid = 2000.0
        self.hl = _mock_hl(hl_mid=self.hl_mid)
        self.strategy = _make_maker(self.hl)

    def teardown_method(self):
        config.MAKER_HEDGE_ENABLED = self._orig_hedge_enabled

    def _threshold_yes(self) -> Position:
        """Open a YES position with capital > HEDGE_THRESHOLD_USD."""
        threshold = config.HEDGE_THRESHOLD_USD  # e.g. $200
        size = (threshold + 100) / 0.50  # enough capital above threshold
        return _yes_position("m1", size=round(size, 2), entry_price=0.50)

    def test_yes_above_threshold_places_short_hedge(self):
        """IT-04 / IT-16: net>0 (YES) → HL SHORT."""
        self.strategy._risk.open_position(self._threshold_yes())
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.place_hedge.assert_called_once()
        _, kwargs = self.hl.place_hedge.call_args
        assert kwargs.get("direction", self.hl.place_hedge.call_args[0][1]) == "SHORT"

    def test_no_above_threshold_places_long_hedge(self):
        """IT-05 / IT-16: net<0 (NO) → HL LONG."""
        threshold = config.HEDGE_THRESHOLD_USD
        size = (threshold + 100) / 0.50
        pos = _no_position("m1", size=round(size, 2), entry_price=0.50)
        self.strategy._risk.open_position(pos)
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.place_hedge.assert_called_once()
        args = self.hl.place_hedge.call_args[0]
        assert args[1] == "LONG"

    def test_hedge_size_equals_net_capital_over_hl_mid(self):
        """IT-15: coins_to_hedge = |net_capital_usd| / hl_mid."""
        # YES: 1000 contracts at 50¢ = $500 net capital
        pos = _yes_position("m1", size=1000.0, entry_price=0.50)
        self.strategy._risk.open_position(pos)
        _run(self.strategy._rebalance_hedge("BTC"))

        net_capital = pos.entry_cost_usd  # $500
        expected_coins = net_capital / self.hl_mid  # 0.25
        placed_coins = self.hl.place_hedge.call_args[0][2]
        assert placed_coins == pytest.approx(expected_coins, rel=1e-4)

    def test_opposite_fills_no_hedge(self):
        """IT-06: equal YES+NO capital → net≈0 → no hedge placed."""
        size = 600.0
        self.strategy._risk.open_position(_yes_position("m1", size=size, entry_price=0.50))
        self.strategy._risk.open_position(_no_position("m2", size=size, entry_price=0.50))
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.place_hedge.assert_not_called()

    def test_no_hl_mid_returns_early(self):
        """IT-08: hl.get_mid returns None → no exception, no place_hedge."""
        self.hl.get_mid.return_value = None
        pos = _yes_position("m1", size=1000.0, entry_price=0.50)
        self.strategy._risk.open_position(pos)
        _run(self.strategy._rebalance_hedge("BTC"))  # must not raise
        self.hl.place_hedge.assert_not_called()

    def test_below_threshold_does_not_place_hedge(self):
        """No hedge when net capital < HEDGE_THRESHOLD_USD (threshold now $50)."""
        # $20 capital ($40 face / 2) — well below $50 threshold
        pos = _yes_position("m1", size=40.0, entry_price=0.50)  # $20
        self.strategy._risk.open_position(pos)
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.place_hedge.assert_not_called()


# ── IT-07: Hedge removed when position closes ─────────────────────────────────

@pytest.mark.p0
class TestHedgeRemoval:
    def setup_method(self):
        self._orig_hedge_enabled = config.MAKER_HEDGE_ENABLED
        config.MAKER_HEDGE_ENABLED = True
        self.hl = _mock_hl(hl_mid=2000.0)
        self.strategy = _make_maker(self.hl)

    def teardown_method(self):
        config.MAKER_HEDGE_ENABLED = self._orig_hedge_enabled

    def test_close_position_drops_delta_below_threshold(self):
        """IT-07: After closing the position, delta = 0, hedge not placed."""
        # Open with capital above threshold
        size = (config.HEDGE_THRESHOLD_USD + 100) / 0.50
        pos = _yes_position("m1", size=round(size, 2), entry_price=0.50)
        self.strategy._risk.open_position(pos)

        # Simulate that a hedge is already in place
        self.strategy._coin_hedges["BTC"] = {
            "size": 0.15,
            "price": 2000.0,
            "direction": "SHORT",
        }
        self.strategy._risk.update_coin_hedge("BTC", 300.0)

        # Close the position
        self.strategy._risk.close_position("m1", exit_price=0.55)

        # Now rebalance — delta is 0, hedge should be removed
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.close_hedge.assert_called_once()
        assert "BTC" not in self.strategy._coin_hedges


# ── IT-09 / IT-10: CSV `underlying` column ────────────────────────────────────

@pytest.mark.p0
class TestCSVUnderlying:
    def test_close_position_writes_underlying(self, tmp_path):
        """IT-09: close_position() CSV row has correct `underlying`."""
        risk.TRADES_CSV = tmp_path / "trades.csv"
        engine = RiskEngine()
        pos = _yes_position("m1", underlying="ETH", size=100.0, entry_price=0.50)
        engine.open_position(pos)
        engine.close_position("m1", exit_price=0.55)

        rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
        assert len(rows) == 1
        assert rows[0]["underlying"] == "ETH"

    def test_hl_hedge_trade_writes_underlying(self, tmp_path):
        """IT-10: record_hl_hedge_trade() CSV row has correct `underlying`."""
        risk.TRADES_CSV = tmp_path / "trades.csv"
        engine = RiskEngine()
        engine.record_hl_hedge_trade(
            coin="SOL",
            direction="SHORT",
            open_price=120.0,
            close_price=115.0,
            size_coins=1.0,
        )

        rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
        assert len(rows) == 1
        assert rows[0]["underlying"] == "SOL"

    def test_underlying_column_in_header(self, tmp_path):
        """TRADES_HEADER must include `underlying`."""
        assert "underlying" in TRADES_HEADER


# ── IT-11: CSV schema migration ───────────────────────────────────────────────

@pytest.mark.p1
class TestCSVMigration:
    def test_stale_header_triggers_backup_and_new_header(self, tmp_path):
        """IT-11: Old header without `underlying` → backup created, new header written."""
        old_trades = tmp_path / "trades.csv"
        old_header = [c for c in TRADES_HEADER if c != "underlying"]  # missing one col

        with old_trades.open("w", newline="") as f:
            csv.writer(f).writerow(old_header)

        old_original = risk.TRADES_CSV
        risk.TRADES_CSV = old_trades
        try:
            RiskEngine()  # _ensure_csv runs on init
        finally:
            risk.TRADES_CSV = old_original

        # New header must match TRADES_HEADER
        with old_trades.open("r", newline="") as f:
            new_header = next(csv.reader(f))
        assert new_header == TRADES_HEADER

        # A backup file must exist
        backups = list(tmp_path.glob("trades_*.csv.bak"))
        assert len(backups) == 1, "Expected exactly one backup file"


# ── IT-18: get_state total_pm_capital_deployed ────────────────────────────────

@pytest.mark.p0
class TestGetStateCapitalDeployed:
    def test_total_pm_capital_deployed_uses_entry_cost_usd(self):
        """IT-18: get_state sums entry_cost_usd, not size (face value)."""
        engine = RiskEngine()
        # 600 contracts at 10¢ = $60 per position
        engine.open_position(_yes_position("m1", size=600.0, entry_price=0.10))
        engine.open_position(_yes_position("m2", size=600.0, entry_price=0.10))

        state = engine.get_state()
        # total_pm_capital_deployed should be $120 (not $1200)
        assert state["total_pm_capital_deployed"] == pytest.approx(120.0), (
            f"Expected 120, got {state['total_pm_capital_deployed']} — "
            "likely using face value instead of entry_cost_usd"
        )

    def test_total_pm_capital_deployed_excludes_closed_positions(self):
        """IT-18: Closed positions should not contribute to capital deployed."""
        engine = RiskEngine()
        engine.open_position(_yes_position("m1", size=600.0, entry_price=0.10))  # $60
        engine.open_position(_yes_position("m2", size=600.0, entry_price=0.50))  # $300
        engine.close_position("m1", exit_price=0.12)

        state = engine.get_state()
        # Only m2 ($300) remains open
        assert state["total_pm_capital_deployed"] == pytest.approx(300.0)


# ── IT-17: Hedge capped at MAX_HL_NOTIONAL ────────────────────────────────────

@pytest.mark.p1
class TestHedgeCap:
    def test_hedge_capped_at_max_hl_notional(self):
        """IT-17: Hedge size is capped at MAX_HL_NOTIONAL / hl_mid."""
        hl_mid = 2000.0
        hl = _mock_hl(hl_mid=hl_mid)
        strategy = _make_maker(hl)

        # Open position with huge capital (10× the HL notional limit)
        huge_capital = config.MAX_HL_NOTIONAL * 10
        size = huge_capital / 0.50
        pos = _yes_position("m1", size=round(size, 2), entry_price=0.50)
        strategy._risk.open_position(pos)

        _run(strategy._rebalance_hedge("BTC"))

        if hl.place_hedge.called:
            placed_coins = hl.place_hedge.call_args[0][2]
            max_coins = config.MAX_HL_NOTIONAL / hl_mid
            assert placed_coins <= max_coins * 1.001, (
                f"Hedge {placed_coins:.6f} coins exceeds cap {max_coins:.6f}"
            )


# ── IT-19: /markets bid/ask fallback priority ─────────────────────────────────

import time as _time

@pytest.mark.p0
class TestMarketsBidAskFallback:
    """IT-19: Verify active_quote > pm_book > null priority in /markets."""

    def _base_market(self, token_id: str = "tok_yes_1") -> dict:
        return {
            "condition_id": "cid1",
            "title": "BTC up?",
            "market_type": "bucket_daily",
            "underlying": "BTC",
            "fees_enabled": False,
            "token_id_yes": token_id,
            "market_slug": "btc-up",
            "yes_book_bid": 0.42,
            "yes_book_ask": 0.45,
            "yes_book_ts":  _time.time(),
        }

    def test_active_quote_wins_over_pm_book(self):
        """active_quote bid/ask overrides PM book values."""
        import api_server
        api_server.state.markets = {"cid1": self._base_market("tok1")}
        api_server.state.active_quotes = {
            "tok1":     {"price": 0.40, "side": "BUY"},
            "tok1_ask": {"price": 0.48, "side": "SELL"},
        }
        result = api_server.markets()
        m = result["markets"][0]
        assert m["bid_price"]  == pytest.approx(0.40)
        assert m["ask_price"]  == pytest.approx(0.48)
        assert m["bid_source"] == "active_quote"
        assert m["ask_source"] == "active_quote"
        assert m["quoted"] is True

    def test_pm_book_used_when_no_active_quote(self):
        """PM orderbook best_bid/ask surfaces when no active_quote exists."""
        import api_server
        api_server.state.markets = {"cid1": self._base_market("tok2")}
        api_server.state.active_quotes = {}
        result = api_server.markets()
        m = result["markets"][0]
        assert m["bid_price"]  == pytest.approx(0.42)
        assert m["ask_price"]  == pytest.approx(0.45)
        assert m["bid_source"] == "pm_book"
        assert m["ask_source"] == "pm_book"
        assert m["quoted"] is False

    def test_null_when_no_data_at_all(self):
        """Both bid and ask are null when no active_quote and no book snapshot."""
        import api_server
        mkt = self._base_market("tok3")
        mkt["yes_book_bid"] = None
        mkt["yes_book_ask"] = None
        mkt["yes_book_ts"]  = None
        api_server.state.markets = {"cid1": mkt}
        api_server.state.active_quotes = {}
        result = api_server.markets()
        m = result["markets"][0]
        assert m["bid_price"]  is None
        assert m["ask_price"]  is None
        assert m["bid_source"] is None
        assert m["ask_source"] is None
        assert m["book_age_s"] is None

    def test_hl_mid_leakage_rejected(self):
        """HL asset mid (~$70k for BTC) must never appear as a PM probability price.

        PM prices are probabilities in [0, 1].  If hl_mid (the raw USD asset
        price) somehow ends up in yes_book_bid/ask it must be silently dropped
        so the UI never shows '7065050.00¢'.
        """
        import api_server
        mkt = self._base_market("tok6")
        # Simulate the leakage bug: HL mid ($70,650) used as a book price
        mkt["yes_book_bid"] = 70_650.0
        mkt["yes_book_ask"] = 70_651.0
        mkt["yes_book_ts"]  = _time.time()
        api_server.state.markets = {"cid1": mkt}
        api_server.state.active_quotes = {}
        result = api_server.markets()
        m = result["markets"][0]
        # Out-of-range prices must be suppressed
        assert m["bid_price"] is None, "USD asset price must not bleed into PM probability bid"
        assert m["ask_price"] is None, "USD asset price must not bleed into PM probability ask"
        assert m["bid_source"] is None
        assert m["ask_source"] is None

    def test_book_age_s_is_recent(self):
        """book_age_s reflects freshness of PM book snapshot."""
        import api_server
        mkt = self._base_market("tok4")
        mkt["yes_book_ts"] = _time.time() - 5.0  # 5 seconds old
        api_server.state.markets = {"cid1": mkt}
        api_server.state.active_quotes = {}
        result = api_server.markets()
        m = result["markets"][0]
        assert m["book_age_s"] is not None
        assert 4.0 <= m["book_age_s"] <= 10.0  # allow for test execution time

    def test_mixed_quote_only_bid(self):
        """active_quote present for bid but not ask → ask falls back to pm_book."""
        import api_server
        api_server.state.markets = {"cid1": self._base_market("tok5")}
        api_server.state.active_quotes = {
            "tok5": {"price": 0.39, "side": "BUY"},
            # no tok5_ask entry
        }
        result = api_server.markets()
        m = result["markets"][0]
        assert m["bid_price"]  == pytest.approx(0.39)
        assert m["bid_source"] == "active_quote"
        assert m["ask_price"]  == pytest.approx(0.45)  # from pm_book
        assert m["ask_source"] == "pm_book"
