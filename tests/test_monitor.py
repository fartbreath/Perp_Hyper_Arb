"""
tests/test_monitor.py — Unit tests for monitor.py

Run: pytest tests/test_monitor.py -v
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
from risk import RiskEngine, Position
from monitor import (
    compute_unrealised_pnl,
    should_exit,
    PositionMonitor,
    ExitReason,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_position(
    side="YES",
    entry_price=0.40,
    size=100.0,
    strategy="mispricing",
    market_id="mkt_001",
    seconds_ago=120,
    now: Optional[datetime] = None,
) -> Position:
    ref = now if now is not None else datetime.now(timezone.utc)
    opened = ref - timedelta(seconds=seconds_ago)
    return Position(
        market_id=market_id,
        market_type="milestone",
        underlying="BTC",
        side=side,
        size=size,
        entry_price=entry_price,
        strategy=strategy,
        opened_at=opened,
    )


# ── compute_unrealised_pnl ────────────────────────────────────────────────────

class TestComputeUnrealisedPnl:
    def test_yes_profit(self):
        pos = _make_position(side="YES", entry_price=0.40, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.55)
        assert pnl == pytest.approx(15.0)

    def test_yes_loss(self):
        pos = _make_position(side="YES", entry_price=0.50, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.35)
        assert pnl == pytest.approx(-15.0)

    def test_no_profit(self):
        # Bought NO at 0.60 (YES was 0.40); profit when YES price falls (NO rises)
        pos = _make_position(side="NO", entry_price=0.60, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.40)
        assert pnl == pytest.approx(20.0)

    def test_no_loss(self):
        pos = _make_position(side="NO", entry_price=0.60, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.70)
        assert pnl == pytest.approx(-10.0)

    def test_buy_yes_alias(self):
        pos = _make_position(side="BUY_YES", entry_price=0.40, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.50)
        assert pnl == pytest.approx(10.0)

    def test_breakeven(self):
        pos = _make_position(side="YES", entry_price=0.50, size=200.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.50)
        assert pnl == pytest.approx(0.0)


# ── should_exit ───────────────────────────────────────────────────────────────

class TestShouldExit:
    NOW = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _pos(self, **kwargs):
        return _make_position(**kwargs)

    def test_min_hold_blocks_exit(self):
        # Position only 10s old — should not exit even if profit target hit.
        # Use actual current time so opened_at (10s ago) is consistent.
        config.MIN_HOLD_SECONDS = 60
        now = datetime.now(timezone.utc)
        opened_at = now - timedelta(seconds=10)
        pos = Position(
            market_id="mkt_hold",
            market_type="milestone",
            underlying="BTC",
            side="YES",
            size=100.0,
            entry_price=0.40,
            strategy="mispricing",
            opened_at=opened_at,
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.60,
            initial_deviation=0.20,
            market_end_date=None,
            now=now,
        )
        assert not exit_flag

    def test_profit_target_hit(self):
        # deviation=0.20, PROFIT_TARGET_PCT=0.60, size=100 → target=$12
        # current pnl = (0.52-0.40)*100 = $12
        config.PROFIT_TARGET_PCT = 0.60
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(entry_price=0.40, size=100.0, seconds_ago=120)
        exit_flag, reason, pnl = should_exit(
            pos=pos,
            current_price=0.52,
            initial_deviation=0.20,
            market_end_date=None,
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.PROFIT_TARGET
        assert pnl == pytest.approx(12.0)

    def test_profit_target_not_yet_hit(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(entry_price=0.40, size=100.0, seconds_ago=120)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.45,          # only $5 pnl — target is $12
            initial_deviation=0.20,
            market_end_date=None,
            now=self.NOW,
        )
        assert not exit_flag

    def test_stop_loss_triggered(self):
        config.STOP_LOSS_USD = 25.0
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, reason, pnl = should_exit(
            pos=pos,
            current_price=0.24,          # pnl = (0.24-0.50)*100 = -$26
            initial_deviation=0.10,
            market_end_date=None,
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.STOP_LOSS
        assert pnl < -25.0

    def test_stop_loss_not_triggered(self):
        config.STOP_LOSS_USD = 25.0
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.40,          # pnl = -$10 — within stop
            initial_deviation=0.10,
            market_end_date=None,
            now=self.NOW,
        )
        assert not exit_flag

    def test_time_stop_triggered(self):
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60
        end_date = self.NOW + timedelta(days=2)   # 2 days away → ≤ 3 days threshold
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,
            initial_deviation=0.10,
            market_end_date=end_date,
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.TIME_STOP

    def test_time_stop_not_triggered(self):
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60
        end_date = self.NOW + timedelta(days=10)  # 10 days — plenty of time
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.50,
            initial_deviation=0.10,
            market_end_date=end_date,
            now=self.NOW,
        )
        assert not exit_flag

    def test_resolved_stop(self):
        config.MIN_HOLD_SECONDS = 60
        end_date = self.NOW - timedelta(hours=1)  # already past
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,
            initial_deviation=0.10,
            market_end_date=end_date,
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.RESOLVED

    def test_no_exit_condition(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 25.0
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60
        end_date = self.NOW + timedelta(days=30)
        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.52,          # small unrealised gain, not at target yet
            initial_deviation=0.10,
            market_end_date=end_date,
            now=self.NOW,
        )
        assert not exit_flag

    def test_momentum_stop_loss_triggered(self):
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MOMENTUM_STOP_LOSS_NO = 0.55
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,          # p_yes = 0.50 ≤ MOMENTUM_STOP_LOSS_YES=0.55
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_momentum_stop_loss_triggered_no_side(self):
        # NO position entered at p_yes=0.15 → p_no=0.85; stop fires if p_no ≤ 0.55
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MOMENTUM_STOP_LOSS_NO = 0.55
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,          # p_no = 1 - 0.50 = 0.50 ≤ MOMENTUM_STOP_LOSS_NO=0.55
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_momentum_stop_loss_no_not_triggered_when_price_favourable(self):
        # NO position: p_yes falls (NO token rises) — should NOT stop out
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MOMENTUM_STOP_LOSS_NO = 0.55
        config.MOMENTUM_TAKE_PROFIT = 0.96
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.10,          # p_no = 1 - 0.10 = 0.90 → well above stop
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_momentum_take_profit_triggered(self):
        config.MOMENTUM_TAKE_PROFIT = 0.96
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.97,          # p_yes = 0.97 ≥ MOMENTUM_TAKE_PROFIT=0.96
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_TAKE_PROFIT

    def test_momentum_no_time_stop_for_bucket_market(self):
        # Bucket market: TTE is 10 minutes (< EXIT_DAYS_BEFORE_RESOLUTION=3 days).
        # Prior to the fix, the else-branch would TIME_STOP any non-maker position.
        # The fix guards with `pos.strategy != "momentum"` so this must NOT exit.
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MOMENTUM_STOP_LOSS_NO = 0.55
        config.MOMENTUM_TAKE_PROFIT = 0.96
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.87,          # within stop/TP band — no exit
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag
        assert reason != ExitReason.TIME_STOP


# ── PositionMonitor ───────────────────────────────────────────────────────────

def _make_monitor():
    pm = MagicMock()
    pm._markets = {}
    pm._books = {}
    pm.place_limit = AsyncMock(return_value="paper_order_001")
    pm.place_market = AsyncMock(return_value="paper_mkt_001")
    pm.on_price_change = MagicMock()  # called by PositionMonitor.start()
    risk = RiskEngine()
    monitor = PositionMonitor(pm=pm, risk=risk, interval=30)
    return monitor, pm, risk


class TestRecordEntryDeviation:
    def test_records_absolute_value(self):
        monitor, _, _ = _make_monitor()
        monitor.record_entry_deviation("mkt_001", -0.15)
        assert monitor._initial_deviations["mkt_001"] == pytest.approx(0.15)

    def test_overwrites_existing(self):
        monitor, _, _ = _make_monitor()
        monitor.record_entry_deviation("mkt_001", 0.10)
        monitor.record_entry_deviation("mkt_001", 0.20)
        assert monitor._initial_deviations["mkt_001"] == pytest.approx(0.20)


class TestCheckPosition:
    def _make_market(self, market_id="mkt_001", end_date=None):
        mkt = MagicMock()
        mkt.condition_id = market_id
        mkt.token_id_yes = "tok_yes"
        mkt.token_id_no = "tok_no"
        mkt.fees_enabled = False
        mkt.end_date = end_date
        mkt.title = "Will BTC exceed $100k?"
        return mkt

    def _make_book(self, mid, bid=None, ask=None):
        book = MagicMock()
        book.mid = mid
        # Provide realistic best_bid/best_ask so bid/ask exit-price logic works.
        # Default: bid = ask = mid (simplifies unit tests; preserves P&L assertions).
        book.best_bid = bid if bid is not None else mid
        book.best_ask = ask if ask is not None else mid
        return book

    def test_no_exit_when_no_market_in_cache(self):
        monitor, pm, risk = _make_monitor()
        pm._markets = {}  # market not found
        pos = _make_position(seconds_ago=120)
        risk.open_position(pos)
        _run(monitor._check_position(pos))
        # Position should still be open
        assert not risk._positions["mkt_001:YES"].is_closed

    def test_no_exit_when_no_book_data(self):
        monitor, pm, risk = _make_monitor()
        pm._markets["mkt_001"] = self._make_market()
        pm._books = {}  # no book data
        pos = _make_position(seconds_ago=120)
        risk.open_position(pos)
        _run(monitor._check_position(pos))
        assert not risk._positions["mkt_001:YES"].is_closed

    def test_exits_on_profit_target(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 25.0
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.55)  # entry=0.40, pnl=$15

        pos = _make_position(entry_price=0.40, size=100.0, seconds_ago=120)
        risk.open_position(pos)
        monitor.record_entry_deviation("mkt_001", 0.20)  # target = 0.20*0.60*100=$12

        _run(monitor._check_position(pos))

        assert risk._positions["mkt_001:YES"].is_closed
        assert risk._positions["mkt_001:YES"].realized_pnl == pytest.approx(15.0, abs=0.01)

    def test_exits_on_stop_loss(self):
        config.STOP_LOSS_USD = 20.0
        config.PROFIT_TARGET_PCT = 0.60
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.25)  # entry=0.50, pnl=-$25

        pos = _make_position(entry_price=0.50, size=100.0, seconds_ago=120)
        risk.open_position(pos)
        monitor.record_entry_deviation("mkt_001", 0.10)

        _run(monitor._check_position(pos))
        assert risk._positions["mkt_001:YES"].is_closed

    def test_no_exit_before_min_hold(self):
        config.MIN_HOLD_SECONDS = 300

        monitor, pm, risk = _make_monitor()
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.90)  # huge profit

        pos = _make_position(entry_price=0.40, size=100.0, seconds_ago=10)
        risk.open_position(pos)
        monitor.record_entry_deviation("mkt_001", 0.20)

        _run(monitor._check_position(pos))
        assert not risk._positions["mkt_001:YES"].is_closed

    def test_clears_deviation_on_close(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 25.0
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.55)

        pos = _make_position(entry_price=0.40, size=100.0, seconds_ago=120)
        risk.open_position(pos)
        monitor.record_entry_deviation("mkt_001", 0.20)

        _run(monitor._check_position(pos))
        assert "mkt_001" not in monitor._initial_deviations

    def test_resolved_spread_both_legs_snap_consistently(self):
        """
        Regression: when a spread (YES + NO) resolves near mid=0.50, the old code
        used best_bid for the YES exit and best_ask for the NO exit.  With a typical
        1–2 cent spread (bid=0.49, ask=0.51) both would round to opposite values
        (0 and 1 respectively), making both legs appear as losers simultaneously.

        After the fix both legs use mid, so round(mid) gives the same settlement
        value and the combined spread P&L is always positive (capturing the spread).
        """
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 999.0  # disable stop-loss
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()

        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        mkt = self._make_market(end_date=past)  # already resolved
        pm._markets["mkt_001"] = mkt

        # Book centered near 0.50 with a 2-cent spread: bid rounds down, ask rounds up.
        pm._books["tok_yes"] = self._make_book(mid=0.50, bid=0.49, ask=0.51)

        # YES leg: entry 0.490
        yes_pos = _make_position(side="YES", entry_price=0.490, size=20.0,
                                 strategy="maker", seconds_ago=120)
        # NO leg: entry 0.520 (YES space)
        no_pos = _make_position(side="NO", entry_price=0.520, size=20.0,
                                strategy="maker", market_id="mkt_001", seconds_ago=120)
        no_pos.market_id = "mkt_001"  # same market, separate risk key
        yes_pos.market_id = "mkt_001"

        risk.open_position(yes_pos)
        risk.open_position(no_pos)

        _run(monitor._check_position(yes_pos))
        _run(monitor._check_position(no_pos))

        yes_closed = risk._positions["mkt_001:YES"]
        no_closed  = risk._positions["mkt_001:NO"]

        assert yes_closed.is_closed, "YES leg should be closed on resolution"
        assert no_closed.is_closed,  "NO leg should be closed on resolution"

        # Combined P&L must be positive: the spread was (0.490 + (1−0.520)) = 0.970,
        # leaving 0.030/ct × 20 = $0.60 spread capture regardless of direction.
        combined_pnl = yes_closed.realized_pnl + no_closed.realized_pnl
        assert combined_pnl > 0.0, (
            f"Spread should capture positive P&L on resolution, "
            f"got YES={yes_closed.realized_pnl:.4f} NO={no_closed.realized_pnl:.4f}"
        )


class TestCheckAllPositions:
    def test_skips_closed_positions(self):
        monitor, pm, risk = _make_monitor()
        pos = _make_position(seconds_ago=120)
        risk.open_position(pos)
        risk._positions["mkt_001:YES"].is_closed = True

        # Should not error and should not try to check closed position
        _run(monitor._check_all_positions())

    def test_no_positions_is_noop(self):
        monitor, _, _ = _make_monitor()
        _run(monitor._check_all_positions())   # should not raise

    def _make_market(self, market_id="mkt_001", token_id_yes="tok_yes"):
        mkt = MagicMock()
        mkt.condition_id = market_id
        mkt.token_id_yes = token_id_yes
        mkt.token_id_no = token_id_yes + "_no"
        mkt.fees_enabled = False
        mkt.end_date = None
        mkt.title = "Will BTC exceed $100k?"
        return mkt

    def _make_book(self, mid, bid=None, ask=None):
        book = MagicMock()
        book.mid = mid
        book.best_bid = bid if bid is not None else mid
        book.best_ask = ask if ask is not None else mid
        return book

    def test_coin_loss_limit_closes_all_maker_positions_for_coin(self):
        """
        When aggregate unrealised P&L for a coin drops below
        -MAKER_COIN_MAX_LOSS_USD, all maker positions for that coin
        must be passively closed (coin_loss_limit exit reason).
        """
        config.MAKER_COIN_MAX_LOSS_USD = 50.0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pm.place_limit = AsyncMock(return_value="paper_exit_001")

        # Two maker BTC positions, each down $30 → combined -$60 < -$50 limit
        pos1 = _make_position(
            market_id="mkt_btc_1", entry_price=0.50, size=100.0,
            strategy="maker", seconds_ago=300,
        )
        pos2 = _make_position(
            market_id="mkt_btc_2", entry_price=0.50, size=100.0,
            strategy="maker", seconds_ago=300,
        )
        risk.open_position(pos1)
        risk.open_position(pos2)

        # Market + book mocks — current mid = 0.20, so each position unrealised = -$30
        mkt1 = self._make_market("mkt_btc_1", "tok_btc_1")
        mkt2 = self._make_market("mkt_btc_2", "tok_btc_2")
        pm._markets = {"mkt_btc_1": mkt1, "mkt_btc_2": mkt2}
        pm._books = {
            "tok_btc_1": self._make_book(mid=0.20),
            "tok_btc_2": self._make_book(mid=0.20),
        }

        _run(monitor._check_all_positions())

        assert risk._positions["mkt_btc_1:YES"].is_closed, "pos1 should be closed by coin-loss limit"
        assert risk._positions["mkt_btc_2:YES"].is_closed, "pos2 should be closed by coin-loss limit"

    def test_coin_loss_limit_not_triggered_below_threshold(self):
        """Positions within the coin loss limit are left open."""
        config.MAKER_COIN_MAX_LOSS_USD = 100.0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pm.place_limit = AsyncMock(return_value="paper_exit_001")

        # One maker BTC position down $30 — below $100 limit, should stay open
        pos = _make_position(
            market_id="mkt_btc_1", entry_price=0.50, size=100.0,
            strategy="maker", seconds_ago=300,
        )
        risk.open_position(pos)

        mkt = self._make_market("mkt_btc_1", "tok_btc_1")
        pm._markets = {"mkt_btc_1": mkt}
        pm._books = {"tok_btc_1": self._make_book(mid=0.20)}

        _run(monitor._check_all_positions())

        assert not risk._positions["mkt_btc_1:YES"].is_closed, "pos should stay open below limit"


# ── Event-driven stop-loss (_on_price_update) ─────────────────────────────────

class TestEventDrivenStopLoss:
    """Tests for PositionMonitor._on_price_update (event-driven exit path)."""

    def _make_market(self, market_id="mkt_001", token_yes="tok_yes", token_no="tok_no",
                     end_date=None):
        mkt = MagicMock()
        mkt.condition_id = market_id
        mkt.token_id_yes = token_yes
        mkt.token_id_no = token_no
        mkt.fees_enabled = False
        mkt.end_date = end_date
        mkt.title = "Will BTC exceed $100k?"
        return mkt

    def _make_book(self, mid, bid=None, ask=None):
        book = MagicMock()
        book.mid = mid
        book.best_bid = bid if bid is not None else mid
        book.best_ask = ask if ask is not None else mid
        return book

    def test_stop_loss_triggered_via_price_event(self):
        """YES momentum position exits when price drops below stop via WS tick."""
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}  # below stop

        # Simulate a WS tick on the YES token
        _run(monitor._on_price_update("tok_yes", 0.50))

        assert risk._positions["mkt_001:YES"].is_closed, "Position should be closed by event-driven stop-loss"
        pm.place_market.assert_called_once()  # exit used market (force_taker) order

    def test_no_exit_for_non_momentum_strategy(self):
        """Event-driven path ignores maker/mispricing positions."""
        monitor, pm, risk = _make_monitor()
        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.10)}

        # Even with price at 0.10 (far below any stop), mispricing is not checked here
        _run(monitor._on_price_update("tok_yes", 0.10))

        assert not risk._positions["mkt_001:YES"].is_closed, "Mispricing position should NOT be closed by event-driven path"

    def test_no_exit_for_unrelated_token(self):
        """Tick on an unrelated token_id does not affect open positions."""
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80, seconds_ago=120)
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

        # Tick for a completely different token
        _run(monitor._on_price_update("tok_unrelated", 0.50))

        assert not risk._positions["mkt_001:YES"].is_closed

    def test_double_exit_prevented_by_exiting_guard(self):
        """Second _on_price_update call while first exit is in-flight is silently skipped."""
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80, seconds_ago=120)
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

        # Pre-populate the exiting set to simulate an in-flight exit
        monitor._exiting_positions.add("mkt_001:YES")

        _run(monitor._on_price_update("tok_yes", 0.50))

        # No second order should have been placed
        pm.place_market.assert_not_called()

    def test_no_trigger_fires_on_no_token_too(self):
        """A WS tick on the NO token also triggers the check for that market's position."""
        config.MOMENTUM_STOP_LOSS_YES = 0.55
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80, seconds_ago=120)
        risk.open_position(pos)

        mkt = self._make_market(token_yes="tok_yes", token_no="tok_no")
        pm._markets = {"mkt_001": mkt}
        # YES book shows mid=0.50 — below stop even though we fired via NO token
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

        _run(monitor._on_price_update("tok_no", 0.50))

        assert risk._positions["mkt_001:YES"].is_closed, "NO-token tick should trigger check and close YES position"
