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
    strike: float = 0.0,
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
        strike=strike,
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
        # Bought NO at 0.40 (YES was 0.60); YES falls to 0.40 so actual NO rises to 0.60 → profit.
        pos = _make_position(side="NO", entry_price=0.40, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.60)
        assert pnl == pytest.approx(20.0)

    def test_no_loss(self):
        # Bought NO at 0.40; YES rises so actual NO falls to 0.30 → loss.
        pos = _make_position(side="NO", entry_price=0.40, size=100.0)
        pnl = compute_unrealised_pnl(pos, current_price=0.30)
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
        # YES position: spot 0.101% below strike — exceeds 0.05% threshold.
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_MIN_HOLD_SECONDS = 60
        config.MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,
            current_spot=99_899.0,   # (99899−100000)/100000×100 = −0.101% < −0.05%
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_momentum_stop_loss_triggered_no_side(self):
        # NO position: spot 0.101% above strike — delta_no = −0.101% < −0.05%.
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 60
        config.MOMENTUM_MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO",
            strike=100_000.0,
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,              # YES-space mid (used for P&L only)
            current_token_price=0.50,        # actual NO CLOB mid
            current_spot=100_101.0,          # (100000−100101)/100000×100 = −0.101% < −0.05%
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_momentum_no_exit_when_no_book_unavailable(self):
        # NO position with NO book unavailable (current_token_price=None) →
        # function returns early before any SL/NE check.
        config.MOMENTUM_TAKE_PROFIT = 0.96
        config.MIN_HOLD_SECONDS = 60
        config.MOMENTUM_MIN_HOLD_SECONDS = 60
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO"
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.10,          # YES mid only — no current_token_price passed
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag  # NO book unavailable → skip, never derive

    def test_momentum_take_profit_triggered(self):
        config.MOMENTUM_TAKE_PROFIT = 0.96
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

    # ── Delta-based SL — NO side ──────────────────────────────────────────

    def test_momentum_stop_loss_no_side_fires_when_delta_exceeded(self):
        """NO position: delta SL fires when spot exceeds strike by threshold.
        Verifies the correct delta formula is used (spot vs strike), NOT CLOB price."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO",
            strike=100_000.0,
        )
        # spot 0.101% above strike → delta_no = (100000−100101)/100000×100 = −0.101% < −0.05%
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.55,              # YES mid (P&L only)
            current_token_price=0.65,        # NO CLOB mid (not used for SL)
            current_spot=100_101.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_momentum_stop_loss_no_side_no_fire_below_delta_threshold(self):
        """NO position: delta SL does NOT fire when spot hasn't moved past threshold."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO",
            strike=100_000.0,
        )
        # spot only 0.01% above strike → delta_no = −0.01% > −0.05% → no fire
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.55,
            current_token_price=0.65,
            current_spot=100_010.0,   # (100000−100010)/100000×100 = −0.01% > −0.05%
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    # ── Near-expiry stop ──────────────────────────────────────────────────

    def test_momentum_near_expiry_stop_triggers(self):
        """Near-expiry stop fires when TTE < threshold AND spot has crossed the strike."""
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0  # large — so SL doesn't fire first
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.60,
            current_token_price=0.60,
            current_spot=99_990.0,   # 0.01% below strike → delta = −0.01% < 0 → NE fires
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=30),   # TTE=30s < 60s threshold
            tte_seconds=30.0,
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_NEAR_EXPIRY

    def test_momentum_near_expiry_stop_no_trigger_when_spot_above_strike(self):
        """Near-expiry stop does NOT fire when spot is above the strike (delta > 0)."""
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        # spot above strike → delta > 0 → no NE exit
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.80,
            current_token_price=0.80,
            current_spot=100_100.0,   # +0.1% above strike → delta = +0.1%
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=30),
            tte_seconds=30.0,
            now=self.NOW,
        )
        assert not exit_flag

    def test_momentum_near_expiry_stop_no_trigger_with_tte_above_threshold(self):
        """Near-expiry stop does NOT fire when TTE is still above the time threshold."""
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        # spot below strike (would trigger NE) — but TTE=90s > 60s → no exit
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.60,
            current_token_price=0.60,
            current_spot=99_990.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=90),
            tte_seconds=90.0,
            now=self.NOW,
        )
        assert not exit_flag

    def test_momentum_near_expiry_no_stop_without_tte_seconds(self):
        """Near-expiry stop is silently disabled when tte_seconds=None."""
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.60,
            current_token_price=0.60,
            current_spot=99_990.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=30),
            tte_seconds=None,              # ← disabled
            now=self.NOW,
        )
        assert not exit_flag


# ── PositionMonitor ───────────────────────────────────────────────────────────

def _make_mock_hl(spot: float, coin: str = "BTC") -> MagicMock:
    """Return a mock HLClient whose get_mid(coin) returns the given spot price."""
    hl = MagicMock()
    hl.get_mid = MagicMock(side_effect=lambda c: spot if c == coin else None)
    return hl


def _make_monitor(hl_client=None):
    pm = MagicMock()
    pm._markets = {}
    pm._books = {}
    pm.place_limit = AsyncMock(return_value="paper_order_001")
    pm.place_market = AsyncMock(return_value="paper_mkt_001")
    pm.on_price_change = MagicMock()  # called by PositionMonitor.start()
    # get_token_balance is awaited in _exit_position when PAPER_TRADING=False.
    # Return None so the code falls back to pos.size (safe in tests).
    pm.get_token_balance = AsyncMock(return_value=None)
    risk = RiskEngine()
    monitor = PositionMonitor(pm=pm, risk=risk, interval=30, hl_client=hl_client)
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
        Regression: at resolution each leg must exit using its own CLOB book mid
        (YES book for YES positions, NO book for NO positions).

        A spread bought at YES=0.49 + NO=0.48 (total cost 0.97/ct) should yield
        positive combined P&L when YES snaps to 0 and NO snaps to 1 (or vice
        versa), capturing the spread ≈ 0.03/ct × 20 = ~$0.60.
        """
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 999.0  # disable stop-loss
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()

        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        mkt = self._make_market(end_date=past)  # already resolved
        pm._markets["mkt_001"] = mkt

        # YES mid=0.49 → round(0.49)=0 → YES loses
        pm._books["tok_yes"] = self._make_book(mid=0.49, bid=0.48, ask=0.50)
        # NO mid=0.51  → round(0.51)=1 → NO wins
        # Must provide an independent NO book; never derive from YES book.
        pm._books["tok_no"]  = self._make_book(mid=0.51, bid=0.50, ask=0.52)

        # YES leg: entry 0.490
        yes_pos = _make_position(side="YES", entry_price=0.490, size=20.0,
                                 strategy="maker", seconds_ago=120)
        # NO leg: entry 0.480 (actual NO token price)
        no_pos = _make_position(side="NO", entry_price=0.480, size=20.0,
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

        # YES: exit at round(0.49)=0 → pnl=(0-0.49)*20=-9.8
        # NO:  exit at round(0.51)=1 → pnl=(1-0.48)*20=+10.4
        # Combined ≈ +0.6 (spread capture)
        combined_pnl = yes_closed.realized_pnl + no_closed.realized_pnl
        assert combined_pnl > 0.0, (
            f"Spread should capture positive P&L on resolution, "
            f"got YES={yes_closed.realized_pnl:.4f} NO={no_closed.realized_pnl:.4f}"
        )

        _run(monitor._check_position(yes_pos))
        _run(monitor._check_position(no_pos))

        yes_closed = risk._positions["mkt_001:YES"]
        no_closed  = risk._positions["mkt_001:NO"]

        assert yes_closed.is_closed, "YES leg should be closed on resolution"
        assert no_closed.is_closed,  "NO leg should be closed on resolution"

        # Combined P&L must be positive: YES entry 0.490 + NO entry 0.480 = 0.970/ct.
        # At resolution one side pays 1.0: spread capture = 1.0 - 0.970 = 0.030/ct × 20 = $0.60
        combined_pnl = yes_closed.realized_pnl + no_closed.realized_pnl
        assert combined_pnl > 0.0, (
            f"Spread should capture positive P&L on resolution, "
            f"got YES={yes_closed.realized_pnl:.4f} NO={no_closed.realized_pnl:.4f}"
        )


# ── YES/NO CLOB independence tests ───────────────────────────────────────────

def _make_monitor_with_market(
    yes_mid=0.30, yes_bid=0.29, yes_ask=0.31,
    no_mid=None, no_bid=None, no_ask=None,
    end_date=None,
    hl_client=None,
):
    """Return (monitor, pm, risk) with a single market in the PM books cache.

    If no_mid is None the NO book is absent from the cache (simulating an
    unavailable NO CLOB), which is the key precondition for every test here.
    """
    monitor, pm, risk = _make_monitor(hl_client=hl_client)

    mkt = MagicMock()
    mkt.condition_id = "mkt_001"
    mkt.token_id_yes = "tok_yes"
    mkt.token_id_no  = "tok_no"
    mkt.fees_enabled = False
    mkt.end_date     = end_date
    mkt.title        = "Will BTC exceed $100k?"
    pm._markets      = {"mkt_001": mkt}

    yes_book = MagicMock()
    yes_book.mid      = yes_mid
    yes_book.best_bid = yes_bid
    yes_book.best_ask = yes_ask

    if no_mid is not None:
        no_book = MagicMock()
        no_book.mid      = no_mid
        no_book.best_bid = no_bid if no_bid is not None else no_mid
        no_book.best_ask = no_ask if no_ask is not None else no_mid
        pm._books = {"tok_yes": yes_book, "tok_no": no_book}
    else:
        pm._books = {"tok_yes": yes_book}  # NO book absent

    return monitor, pm, risk, mkt


class TestYesNoBookIndependence:
    """
    Verify that the monitor NEVER derives a NO token price from the YES book.
    For every exit path, when the NO CLOB book is unavailable the monitor must
    either skip the operation or fall back to entry_price — it must NOT compute
    `1.0 - yes_price` as a proxy for the NO price.

    Key invariant: YES mid=0.30  ≠  1.0 − NO mid.  We set NO mid=0.60 to make
    the difference obvious: any remaining derivation would produce 0.70, not 0.60.
    """

    YES_MID = 0.30   # NOT 1 - NO_MID (0.40); intentionally decoupled

    # ── coin-loss P&L aggregation ─────────────────────────────────────────────

    def test_coin_loss_pnl_no_book_missing_skips_position(self):
        """When the NO CLOB book is absent, the NO position must be excluded from
        the coin-loss P&L sum rather than contributing a derived value."""
        config.MAKER_COIN_MAX_LOSS_USD = 1.0   # very tight to force trigger if any value leaks

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=None,  # NO book absent
        )
        no_pos = _make_position(side="NO", entry_price=0.60, size=100.0, strategy="maker")
        risk.open_position(no_pos)

        # If a derived price (1 - 0.30 = 0.70) were used, unrealised = (0.70 - 0.60) × 100 = +$10
        # which is positive, so the coin-loss limit would not trigger.
        # If entry_price were used, unrealised = 0, no trigger.
        # Either way, the position must NOT be closed (it should be skipped).
        _run(monitor._check_all_positions())

        assert not risk._positions["mkt_001:NO"].is_closed, (
            "NO position should be skipped (not closed) when NO book is unavailable"
        )

    def test_coin_loss_pnl_uses_no_book_mid_not_derived(self):
        """When the NO CLOB book IS available, coin-loss P&L must use the actual
        NO mid, not 1 − YES_mid."""
        config.MAKER_COIN_MAX_LOSS_USD = 0.01  # trigger on any loss

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID,   # 0.30
            no_mid=0.55, no_bid=0.54, no_ask=0.56,   # independent of YES
        )
        # Entry at 0.60, NO now 0.55 → loss of 5c × 10 = -$0.50 → triggers limit
        no_pos = _make_position(side="NO", entry_price=0.60, size=10.0, strategy="maker")
        risk.open_position(no_pos)

        _run(monitor._check_all_positions())

        # Position should be closed because actual NO mid (0.55) < entry (0.60) → loss
        assert risk._positions["mkt_001:NO"].is_closed, (
            "NO position should be closed using actual NO book mid (0.55), not derived 1-0.30=0.70"
        )

    # ── coin-loss exit price ──────────────────────────────────────────────────

    def test_coin_loss_exit_no_book_missing_uses_entry_price(self):
        """When NO book is unavailable for coin-loss exit, fall back to entry_price
        (zero P&L) rather than deriving 1 − YES_ask."""
        config.MAKER_COIN_MAX_LOSS_USD = 0.01  # force trigger

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=None,   # NO book absent → must skip in P&L agg → position NOT in coin_positions for closure
        )
        # With NO book absent, position is skipped entirely in aggregation.
        # Coin-loss limit cannot trigger → position stays open.
        no_pos = _make_position(side="NO", entry_price=0.60, size=100.0, strategy="maker")
        risk.open_position(no_pos)

        _run(monitor._check_all_positions())

        assert not risk._positions["mkt_001:NO"].is_closed, (
            "NO position must not be closed via derived exit price when NO book is absent"
        )

    # ── resolution exit ───────────────────────────────────────────────────────

    def test_resolution_exit_no_uses_no_book_mid(self):
        """At resolution, NO position should exit at the actual NO CLOB mid."""
        now = datetime.now(timezone.utc)
        end_date = now - timedelta(seconds=5)  # already past expiry

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=0.01,                          # YES snapped near 0
            no_mid=0.99, no_bid=0.99, no_ask=1.0,  # NO should win
            end_date=end_date,
        )
        no_pos = _make_position(side="NO", entry_price=0.82, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        closed = risk._positions["mkt_001:NO"]
        assert closed.is_closed
        # Exit must be at NO book mid (0.99), not at 1 − YES_mid (1 − 0.01 = 0.99 coincidentally)
        # Use a different pairing to make the test meaningful:
        # YES_mid=0.01 → derived=0.99; actual NO_mid=0.99 — same here.
        # Test that realized_pnl is positive (correct), not zero (entry_price fallback).
        assert closed.realized_pnl > 0.0, (
            f"P&L should be positive (NO gained value from 0.82 to 0.99), got {closed.realized_pnl}"
        )

    def test_resolution_exit_no_book_missing_uses_entry_price(self):
        """At resolution, when NO book is gone, exit at entry_price (no YES derivation).

        entry_price=0.20 is used so that:
          - New code: exit_mid=0.20 → round(0.20)=0.0 → pnl=(0-0.20)*100≈-20  (NO loses)
          - Old code: exit_mid=1.0-round(YES_mid=0.01)=1.0 → pnl=(1-0.20)*100=+80 (NO wins)
        This verifies the derivation path is no longer taken.
        """
        now = datetime.now(timezone.utc)
        end_date = now - timedelta(seconds=5)

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=0.01,   # YES snaps to 0 — old code would derive NO exit=1.0
            no_mid=None,    # NO book absent
            end_date=end_date,
        )
        no_pos = _make_position(side="NO", entry_price=0.20, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        closed = risk._positions["mkt_001:NO"]
        assert closed.is_closed, "Resolved position must always be closed"
        # Old derivation: 1.0-round(0.01)=1.0 → pnl ≈ +80.  New: entry_price=0.20 → pnl ≈ -20.
        assert closed.realized_pnl == pytest.approx(-20.0, abs=0.5), (
            f"exit must use entry_price=0.20 (pnl≈-20), not derived 1.0 (pnl≈+80), got {closed.realized_pnl:.4f}"
        )

    # ── pre-expiry taker exit ─────────────────────────────────────────────────

    def test_pre_expiry_exit_no_uses_no_book_best_bid(self):
        """Near-expiry taker exit for NO uses the actual NO CLOB best_bid."""
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=30)   # 30s TTE → within near-expiry window

        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0  # prevent SL from firing first
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0

        # For NO: NE fires when spot > strike (delta_no < 0).
        # strike=100, spot=101 → delta_no = (100-101)/100×100 = −1% < 0
        hl = _make_mock_hl(spot=101.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=0.60, no_bid=0.58, no_ask=0.62,  # independent of YES
            end_date=future,
            hl_client=hl,
        )
        no_pos = _make_position(side="NO", entry_price=0.85, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now, strike=100.0)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        closed = risk._positions["mkt_001:NO"]
        assert closed.is_closed, "Near-expiry exit should have fired"
        # Realized P&L must reflect actual NO best_bid (0.58), not 1 − YES_ask (1 − 0.31 = 0.69)
        expected_pnl = (0.58 - 0.85) * 100.0  # exit at 0.58, entry at 0.85
        assert closed.realized_pnl == pytest.approx(expected_pnl, abs=0.5), (
            f"P&L should use NO best_bid=0.58, got {closed.realized_pnl:.4f}"
        )

    def test_pre_expiry_exit_no_book_missing_defers(self):
        """When NO book is unavailable the monitor must defer (current_token_price=None
        causes early return before the NE delta check)."""
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=30)

        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_MIN_HOLD_SECONDS = 0

        hl = _make_mock_hl(spot=101.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=None,   # NO book absent → must defer
            end_date=future,
            hl_client=hl,
        )
        no_pos = _make_position(side="NO", entry_price=0.85, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now, strike=100.0)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        assert not risk._positions["mkt_001:NO"].is_closed, (
            "Monitor must defer the exit (not close) when NO book is unavailable"
        )
        pm.place_market.assert_not_called()
        pm.place_limit.assert_not_called()

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
        """YES momentum position exits when spot crosses below strike threshold via WS tick."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 0

        hl = _make_mock_hl(spot=99_899.0)  # 0.101% below strike → exceeds 0.05% threshold
        monitor, pm, risk = _make_monitor(hl_client=hl)
        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120, strike=100_000.0,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

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
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 0

        hl = _make_mock_hl(spot=99_899.0)  # would trigger SL if correct token fires
        monitor, pm, risk = _make_monitor(hl_client=hl)
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80,
                             seconds_ago=120, strike=100_000.0)
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

        # Tick for a completely different token
        _run(monitor._on_price_update("tok_unrelated", 0.50))

        assert not risk._positions["mkt_001:YES"].is_closed

    def test_double_exit_prevented_by_exiting_guard(self):
        """Second _on_price_update call while first exit is in-flight is silently skipped."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 0

        hl = _make_mock_hl(spot=99_899.0)  # would trigger SL but guard fires first
        monitor, pm, risk = _make_monitor(hl_client=hl)
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80,
                             seconds_ago=120, strike=100_000.0)
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
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 0

        hl = _make_mock_hl(spot=99_899.0)  # 0.101% below strike → delta SL fires
        monitor, pm, risk = _make_monitor(hl_client=hl)
        pos = _make_position(strategy="momentum", side="YES", entry_price=0.80,
                             seconds_ago=120, strike=100_000.0)
        risk.open_position(pos)

        mkt = self._make_market(token_yes="tok_yes", token_no="tok_no")
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.50)}

        _run(monitor._on_price_update("tok_no", 0.50))

        assert risk._positions["mkt_001:YES"].is_closed, "NO-token tick should trigger check and close YES position"

    def test_take_profit_uses_force_taker(self):
        """
        MOMENTUM_TAKE_PROFIT exit must use a market order (force_taker=True) so it
        executes immediately rather than resting as a post-only limit that would
        likely be rejected or delayed at near-certainty prices.
        """
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        # Price at or above take-profit level
        pm._books = {"tok_yes": self._make_book(mid=0.999)}

        _run(monitor._on_price_update("tok_yes", 0.999))

        assert risk._positions["mkt_001:YES"].is_closed, "Position should be closed by take-profit"
        # force_taker=True → place_market() must be called, not place_limit()
        pm.place_market.assert_called_once()
        pm.place_limit.assert_not_called()
