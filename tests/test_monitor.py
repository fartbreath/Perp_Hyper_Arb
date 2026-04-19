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
        """NO position: delta SL does NOT fire when spot is still well in-the-money.

        With the protective-buffer semantics, SL fires when delta < +SL_PCT.
        A spot of 99_900 gives delta_no = (100000−99900)/100000×100 = +0.1% > +0.05%
        → position is still 0.1% in-the-money → no fire.
        """
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        pos = _make_position(
            entry_price=0.15, size=50.0, seconds_ago=120, strategy="momentum", side="NO",
            strike=100_000.0,
        )
        # spot 0.1% BELOW strike → NO is in-the-money, delta_no = +0.1% > +0.05% → no fire
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.55,
            current_token_price=0.65,
            current_spot=99_900.0,   # (100000−99900)/100000×100 = +0.1% > +0.05%
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_per_coin_delta_sl_pct_kwarg_overrides_global(self):
        """delta_sl_pct kwarg takes precedence over global MOMENTUM_DELTA_STOP_LOSS_PCT.

        Global SL is set to 0.03% — which would NOT fire on a +0.05% delta.
        Per-coin SL is set to 0.07% — which DOES fire on a +0.05% delta (fires sooner).
        The higher per-coin value provides a wider ITM buffer for high-IV coins.
        """
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.03
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        # delta_yes = (100050−100000)/100000×100 = +0.05% → global 0.03 would NOT fire (0.05 >= 0.03)
        exit_flag_global, _, _ = should_exit(
            pos=pos, current_price=0.50, current_spot=100_050.0,
            initial_deviation=0.0, market_end_date=self.NOW + timedelta(minutes=10), now=self.NOW,
        )
        assert not exit_flag_global  # confirm global doesn't fire here

        # with per-coin SL = 0.07%: 0.05 < 0.07 → DOES fire (wider protective buffer)
        exit_flag_coin, reason, _ = should_exit(
            pos=pos, current_price=0.50, current_spot=100_050.0,
            initial_deviation=0.0, market_end_date=self.NOW + timedelta(minutes=10), now=self.NOW,
            delta_sl_pct=0.07,
        )
        assert exit_flag_coin
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_per_coin_delta_sl_pct_kwarg_none_falls_back_to_global(self):
        """delta_sl_pct=None (default) falls back to global MOMENTUM_DELTA_STOP_LOSS_PCT.

        Ensures backwards-compatibility: callers that don't pass delta_sl_pct
        get the same behaviour as before the parameter was added.
        """
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.07
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        # delta_yes = +0.05% → 0.05 < global 0.07 → fires via global fallback
        exit_flag, reason, _ = should_exit(
            pos=pos, current_price=0.50, current_spot=100_050.0,
            initial_deviation=0.0, market_end_date=self.NOW + timedelta(minutes=10), now=self.NOW,
            # delta_sl_pct not passed — defaults to None → falls back to global 0.07
        )
        assert exit_flag
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    # ── Near-expiry stop ──────────────────────────────────────────────────

    def test_momentum_near_expiry_stop_triggers(self):
        """Near-expiry stop fires when TTE < threshold AND spot has crossed the strike.

        Uses a large negative SL_PCT to disable the primary delta SL so that the
        near-expiry check fires as the independent last-resort safety net.
        """
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # negative → primary SL disabled
        config.MOMENTUM_TAKE_PROFIT = 0.999
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
        """Near-expiry stop does NOT fire when spot is above the strike (delta > 0).

        Uses large negative SL_PCT to isolate near-expiry logic from the primary delta SL.
        """
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # negative → primary SL disabled
        config.MOMENTUM_TAKE_PROFIT = 0.999
        pos = _make_position(
            entry_price=0.85, size=50.0, seconds_ago=120, strategy="momentum", side="YES",
            strike=100_000.0,
        )
        # spot above strike → delta > 0 → no NE exit; and delta > -999 → no delta SL
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
        """Near-expiry stop does NOT fire when TTE is still above the time threshold.

        Uses large negative SL_PCT to isolate near-expiry logic from the primary delta SL.
        """
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # negative → primary SL disabled
        config.MOMENTUM_TAKE_PROFIT = 0.999
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
        """Near-expiry stop is silently disabled when tte_seconds=None.

        Uses large negative SL_PCT to isolate near-expiry logic from the primary delta SL.
        """
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # negative → primary SL disabled
        config.MOMENTUM_TAKE_PROFIT = 0.999
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

def _make_mock_spot(spot: float, coin: str = "BTC") -> MagicMock:
    """Return a mock SpotOracle whose get_mid(coin, market_type) returns the given spot price."""
    mock_spot = MagicMock()
    mock_spot.get_mid = MagicMock(side_effect=lambda c, mt=None: spot if c == coin else None)
    return mock_spot


def _make_monitor(spot_client=None):
    pm = MagicMock()
    pm._markets = {}
    pm._books = {}
    pm.place_limit = AsyncMock(return_value="paper_order_001")
    pm.place_market = AsyncMock(return_value="paper_mkt_001")
    pm.on_price_change = MagicMock()  # called by PositionMonitor.start()
    # get_token_balance is awaited in _exit_position when PAPER_TRADING=False.
    # Return None so the code falls back to pos.size (safe in tests).
    pm.get_token_balance = AsyncMock(return_value=None)
    pm.fetch_market_resolution = AsyncMock(return_value=None)
    risk = RiskEngine()
    # Tests call _check_position once and expect SL to fire immediately.
    config.MOMENTUM_DELTA_SL_MIN_TICKS = 1
    monitor = PositionMonitor(pm=pm, risk=risk, interval=30, spot_client=spot_client)
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
        Regression: at resolution the PM CLOB settlement API is the sole source
        of truth.  Both legs exit using fetch_market_resolution() — no CLOB book
        or oracle fallback.

        A spread bought at YES=0.49 + NO=0.48 (total cost 0.97/ct) should yield
        positive combined P&L when PM settles YES=0 (NO wins), capturing the
        spread ≈ 0.03/ct × 20 = ~$0.60.
        """
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 999.0  # disable stop-loss
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        # PM API: YES settled at 0.0 (YES loses, NO wins)
        pm.fetch_market_resolution = AsyncMock(return_value=0.0)

        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        mkt = self._make_market(end_date=past)  # already resolved
        pm._markets["mkt_001"] = mkt

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

        # YES: pm_yes=0.0 → exit_mid=0.0 → pnl=(0-0.49)*20=-9.8
        # NO:  pm_yes=0.0 → exit_mid=1-0.0=1.0 → pnl=(1.0-0.48)*20=+10.4
        # Combined ≈ +0.6 (spread capture)
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
    spot_client=None,
):
    """Return (monitor, pm, risk, mkt) with a single market in the PM books cache.

    If no_mid is None the NO book is absent from the cache (simulating an
    unavailable NO CLOB).  If yes_mid is None the YES book is also absent
    (simulating a fully drained CLOB near expiry — the key precondition for
    the delta-SL-when-book-empty tests).
    """
    monitor, pm, risk = _make_monitor(spot_client=spot_client)

    mkt = MagicMock()
    mkt.condition_id = "mkt_001"
    mkt.token_id_yes = "tok_yes"
    mkt.token_id_no  = "tok_no"
    mkt.fees_enabled = False
    mkt.end_date     = end_date
    mkt.title        = "Will BTC exceed $100k?"
    pm._markets      = {"mkt_001": mkt}

    books: dict = {}

    if yes_mid is not None:
        yes_book = MagicMock()
        yes_book.mid      = yes_mid
        yes_book.best_bid = yes_bid
        yes_book.best_ask = yes_ask
        books["tok_yes"] = yes_book

    if no_mid is not None:
        no_book = MagicMock()
        no_book.mid      = no_mid
        no_book.best_bid = no_bid if no_bid is not None else no_mid
        no_book.best_ask = no_ask if no_ask is not None else no_mid
        books["tok_no"] = no_book

    pm._books = books
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

    def test_resolution_exit_no_uses_pm_api(self):
        """At resolution, NO position exits using the PM CLOB settlement API only.
        pm_yes_price=0.0 means YES lost → NO won → exit_mid=1.0."""
        now = datetime.now(timezone.utc)
        end_date = now - timedelta(seconds=5)  # already past expiry

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=0.01,  # book data present but ignored in RESOLVED path
            no_mid=0.99,
            end_date=end_date,
        )
        # PM API confirms YES settled at 0 → NO wins
        pm.fetch_market_resolution = AsyncMock(return_value=0.0)

        no_pos = _make_position(side="NO", entry_price=0.82, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        closed = risk._positions["mkt_001:NO"]
        assert closed.is_closed
        # exit_mid = 1.0 - 0.0 = 1.0 → pnl = (1.0 - 0.82) * 100 = +18
        assert closed.realized_pnl > 0.0, (
            f"P&L should be positive (NO won: exit_mid=1.0), got {closed.realized_pnl}"
        )

    def test_resolution_pm_api_loss_no_book_missing(self):
        """At resolution, PM API is sole source of truth regardless of whether CLOB books
        are present.  When NO book is absent the position still closes correctly.

        pm_yes_price=1.0 → YES won → NO lost → exit_mid=0.0 → pnl=(0-0.20)*100=-20.
        Old CLOB-book fallback would have derived exit_mid=entry_price or 1-YES_mid=0.99,
        both of which gave wrong P&L.
        """
        now = datetime.now(timezone.utc)
        end_date = now - timedelta(seconds=5)

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=0.01,   # book data present but irrelevant — PM API is authoritative
            no_mid=None,    # NO book absent — must not affect outcome
            end_date=end_date,
        )
        # YES won → NO lost
        pm.fetch_market_resolution = AsyncMock(return_value=1.0)

        no_pos = _make_position(side="NO", entry_price=0.20, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        closed = risk._positions["mkt_001:NO"]
        assert closed.is_closed, "Resolved position must always be closed when PM API returns a result"
        # exit_mid = 1.0 - 1.0 = 0.0 → pnl = (0.0 - 0.20) * 100 = -20
        assert closed.realized_pnl == pytest.approx(-20.0, abs=0.5), (
            f"exit must use PM settlement (exit_mid=0.0, pnl≈-20), got {closed.realized_pnl:.4f}"
        )

    def test_resolution_pm_api_not_settled_yet_skips(self):
        """When fetch_market_resolution returns None (PM API hasn't propagated yet),
        the position must NOT be closed — wait for the next poll cycle."""
        now = datetime.now(timezone.utc)
        end_date = now - timedelta(seconds=5)

        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=0.99,  # looks like a winner but PM API not settled
            end_date=end_date,
        )
        # PM API not ready yet
        pm.fetch_market_resolution = AsyncMock(return_value=None)

        pos = _make_position(side="YES", entry_price=0.80, size=100.0, strategy="momentum",
                             seconds_ago=10, now=now)
        risk.open_position(pos)

        _run(monitor._check_position(pos))

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "Position must not be closed while PM API has not yet confirmed settlement"
        )

    # ── pre-expiry taker exit ─────────────────────────────────────────────────

    def test_pre_expiry_exit_no_uses_no_book_best_bid(self):
        """Near-expiry taker exit for NO uses the actual NO CLOB best_bid."""
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=30)   # 30s TTE → within near-expiry window

        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0  # prevent SL from firing first
        config.MOMENTUM_TAKE_PROFIT = 0.999

        # For NO: NE fires when spot > strike (delta_no < 0).
        # strike=100, spot=101 → delta_no = (100-101)/100×100 = −1% < 0
        spot = _make_mock_spot(spot=101.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=0.60, no_bid=0.58, no_ask=0.62,  # independent of YES
            end_date=future,
            spot_client=spot,
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

    def test_pre_expiry_exit_no_book_missing_exits_at_entry_price(self):
        """When the near-expiry delta stop fires but NO book is drained, the monitor
        must still close the position — using entry_price as the order target rather
        than deferring indefinitely.  This prevents wipeouts where the NO book stays
        empty all the way to RESOLVED.

        Key invariant: the exit price must NOT be derived from the YES book (1 - yes_mid).
        spot=101, strike=100 → delta_no = −1% (YES side would give 1 − 0.30 = 0.70 ≠ entry_price).
        """
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=30)

        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 60
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 100.0  # prevent delta SL; NE fires
        config.MOMENTUM_TAKE_PROFIT = 0.999

        spot = _make_mock_spot(spot=101.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=None,   # NO book absent
            end_date=future,
            spot_client=spot,
        )
        no_pos = _make_position(side="NO", entry_price=0.85, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now, strike=100.0)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        assert risk._positions["mkt_001:NO"].is_closed, (
            "Near-expiry delta stop must still exit even when NO book is drained"
        )
        # Exit must use entry_price (zero P&L), NOT a YES-derived price
        assert risk._positions["mkt_001:NO"].realized_pnl == pytest.approx(0.0, abs=0.01), (
            "P&L must be ~0 (entry_price fallback), not a YES-derived value"
        )
        pm.place_market.assert_called_once()

    def test_take_profit_defers_when_no_book_missing(self):
        """Take-profit (CLOB-price-based) still defers when NO book is unavailable — it
        requires a valid NO mid to fire.  Only delta-based stops bypass the NO book guard."""
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=120)  # TTE > NE threshold → NE won't fire

        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 30  # below 120s → NE not active
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.01   # extremely low — would trigger if price checked

        # NO position in-the-money (spot < strike) with delta well above SL threshold
        # spot=99_900 → delta_no = (100000−99900)/100000×100 = +0.1% > +0.05% → no delta SL
        spot = _make_mock_spot(spot=99_900.0)  # 0.1% BELOW strike → NO in-the-money
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=self.YES_MID, yes_bid=0.29, yes_ask=0.31,
            no_mid=None,   # NO book absent
            end_date=future,
            spot_client=spot,
        )
        no_pos = _make_position(side="NO", entry_price=0.85, size=100.0, strategy="momentum",
                                seconds_ago=120, now=now, strike=100_000.0)
        risk.open_position(no_pos)

        _run(monitor._check_position(no_pos))

        assert not risk._positions["mkt_001:NO"].is_closed, (
            "Take-profit must not fire when NO book is absent (no valid token price)"
        )
        pm.place_market.assert_not_called()

    # ── Delta SL fires even when YES CLOB book is drained ─────────────────────

    def test_delta_sl_fires_when_yes_book_empty(self):
        """Root-cause regression: YES CLOB book drains near expiry but delta SL
        must still fire via the HL spot path.

        Before the fix: book guard returned early → should_exit never called → SL missed.
        After the fix: momentum bypasses the book guard → delta SL fires correctly.
        """
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=120)  # 2 min TTE

        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 30  # below 120s TTE — NE won't fire

        # spot 0.1% below strike (100_000) → delta = −0.1% < −0.05% → SL fires
        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=None,  # YES CLOB book drained — only RTDS spot is available
            end_date=future,
            spot_client=spot,
        )
        yes_pos = _make_position(
            side="YES", entry_price=0.85, size=100.0,
            strategy="momentum", seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(yes_pos)

        _run(monitor._check_position(yes_pos))

        assert risk._positions["mkt_001:YES"].is_closed, (
            "Delta SL must fire even when YES CLOB book is empty"
        )

    def test_delta_sl_does_not_fire_when_spot_within_threshold(self):
        """Confirm delta SL is NOT over-eager when YES book is empty and position is in-the-money.

        With protective-buffer semantics, SL fires when delta < +SL_PCT.
        spot=100_100 gives delta_yes = (100100−100000)/100000×100 = +0.1% > +0.05%
        → position is 0.1% in-the-money → delta SL does NOT fire.
        """
        now = datetime.now(timezone.utc)
        future = now + timedelta(seconds=120)

        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 30

        # spot 0.1% ABOVE strike → YES is clearly in-the-money, delta = +0.1% > +0.05% → no SL
        spot = _make_mock_spot(spot=100_100.0)
        monitor, pm, risk, _ = _make_monitor_with_market(
            yes_mid=None,  # YES CLOB book drained
            end_date=future,
            spot_client=spot,
        )
        yes_pos = _make_position(
            side="YES", entry_price=0.85, size=100.0,
            strategy="momentum", seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(yes_pos)

        _run(monitor._check_position(yes_pos))

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "Delta SL must NOT fire when position is still clearly in-the-money"
        )

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

        spot = _make_mock_spot(spot=99_899.0)  # 0.101% below strike → exceeds 0.05% threshold
        monitor, pm, risk = _make_monitor(spot_client=spot)
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

    def test_non_momentum_strategy_exits_on_pm_tick_when_stop_triggers(self):
        """PM price ticks now check ALL strategies — mispricing exits on stop-loss.

        After removing the momentum-only filter from _on_price_update, all open
        positions are evaluated on every relevant PM WS tick. This test verifies
        that a mispricing position with a qualifying stop-loss DOES exit.
        """
        config.STOP_LOSS_USD = 50.0
        config.PROFIT_TARGET_PCT = 999.0  # disable profit target
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.10)}

        # unrealised = (0.10 - 0.80) * 100 = -$70 ≤ -$50 → stop-loss fires
        _run(monitor._on_price_update("tok_yes", 0.10))

        assert risk._positions["mkt_001:YES"].is_closed, (
            "Mispricing position must exit on PM tick when stop-loss is triggered"
        )

    def test_no_exit_for_non_momentum_with_no_stop_trigger(self):
        """When a mispricing position's price is still above the stop level,
        it should NOT be exited even though it is now checked on every PM tick.
        All strategies are event-driven; this verifies the exit logic correctly
        applies per-strategy conditions (not that mispricing is skipped)."""
        config.STOP_LOSS_USD = 9999.0   # disable stop-loss for this test
        config.PROFIT_TARGET_PCT = 0.999  # disable profit target
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0

        monitor, pm, risk = _make_monitor()
        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.80,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)

        mkt = self._make_market()
        pm._markets = {"mkt_001": mkt}
        pm._books = {"tok_yes": self._make_book(mid=0.10)}

        # Price is well below entry but stop-loss is disabled — no exit should fire
        _run(monitor._on_price_update("tok_yes", 0.10))

        assert not risk._positions["mkt_001:YES"].is_closed, "No exit: stop-loss disabled for this test"

    def test_no_exit_for_unrelated_token(self):
        """Tick on an unrelated token_id does not affect open positions."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MIN_HOLD_SECONDS = 0

        spot = _make_mock_spot(spot=99_899.0)  # would trigger SL if correct token fires
        monitor, pm, risk = _make_monitor(spot_client=spot)
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

        spot = _make_mock_spot(spot=99_899.0)  # would trigger SL but guard fires first
        monitor, pm, risk = _make_monitor(spot_client=spot)
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

        spot = _make_mock_spot(spot=99_899.0)  # 0.101% below strike → delta SL fires
        monitor, pm, risk = _make_monitor(spot_client=spot)
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


# ── Hold-floor removal: momentum exits immediately, non-momentum still guarded ─

class TestMomentumHoldFloorRemoval:
    """Verify that MOMENTUM_MIN_HOLD_SECONDS removal has the intended effect:
    - Momentum positions can SL/TP the instant they are opened (0 s hold).
    - Non-momentum positions still respect MIN_HOLD_SECONDS.
    """

    NOW = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_momentum_exits_at_time_zero(self):
        """Delta SL fires for a momentum position held for 0 seconds.
        This is the key regression test for the hold-floor removal — previously
        MOMENTUM_MIN_HOLD_SECONDS=10 meant a freshly opened position was
        immune to the SL for 10 seconds, which is a long time near expiry."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999

        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=0,   # opened RIGHT NOW
            strike=100_000.0,
        )
        # spot 0.1% below strike → delta = −0.1% < −0.05% → SL should fire instantly
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.70,
            current_spot=99_900.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=5),
            now=self.NOW,
        )
        assert exit_flag, "Delta SL must fire even at seconds_held=0 (no hold floor for momentum)"
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_non_momentum_hold_floor_still_applies(self):
        """MIN_HOLD_SECONDS still blocks exits for mispricing positions held < floor.
        Ensures the floor removal was scoped to momentum only."""
        config.MIN_HOLD_SECONDS = 60
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 5.0  # hair-trigger stop

        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.50,
            size=100.0, seconds_ago=5, now=self.NOW,  # only 5s old — within the 60s floor
        )
        # P&L = (0.20 - 0.50) × 100 = −$30, well past the $5 stop — but hold floor blocks it
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.20,
            initial_deviation=0.10,
            market_end_date=self.NOW + timedelta(days=30),
            now=self.NOW,
        )
        assert not exit_flag, "Mispricing position must NOT exit before MIN_HOLD_SECONDS"

    def test_non_momentum_exits_after_hold_floor(self):
        """Same mispricing position exits once hold floor has passed."""
        config.MIN_HOLD_SECONDS = 60
        config.STOP_LOSS_USD = 5.0

        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.50,
            size=100.0, seconds_ago=120, now=self.NOW,  # 120s > 60s floor → allowed
        )
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.20,
            initial_deviation=0.10,
            market_end_date=self.NOW + timedelta(days=30),
            now=self.NOW,
        )
        assert exit_flag
        assert reason == ExitReason.STOP_LOSS


# ── RTDS spot event path (_on_spot_update) ─────────────────────────────

class TestSpotOracleEventPath:
    """Tests for PositionMonitor._on_spot_update — the RTDS spot event path.

    This is the critical path that must fire the delta SL when the PM CLOB book
    drains near expiry (the root-cause scenario for the missed stop-losses).
    Unlike _on_price_update (PM book ticks), this fires purely on RTDS spot price
    updates and does NOT require the PM book to have a valid mid.
    """

    def _make_market_and_books(self, pm, yes_mid=None, no_mid=None,
                                end_date=None, market_id="mkt_001"):
        mkt = MagicMock()
        mkt.condition_id = market_id
        mkt.token_id_yes = "tok_yes"
        mkt.token_id_no  = "tok_no"
        mkt.fees_enabled = False
        mkt.end_date     = end_date
        mkt.title        = "Will BTC exceed $100k?"
        pm._markets = {market_id: mkt}

        books = {}
        if yes_mid is not None:
            b = MagicMock(); b.mid = yes_mid; b.best_bid = yes_mid; b.best_ask = yes_mid
            books["tok_yes"] = b
        if no_mid is not None:
            b = MagicMock(); b.mid = no_mid; b.best_bid = no_mid; b.best_ask = no_mid
            books["tok_no"] = b
        pm._books = books
        return mkt

    # ── Root-cause scenario ───────────────────────────────────────────────────

    def test_spot_tick_fires_delta_sl_when_yes_book_empty(self):
        """THE ROOT-CAUSE SCENARIO.

        Spot moves past the strike while the YES CLOB book is completely drained
        (near expiry).  The PM tick path (_on_price_update) is silent because
        pm_client only fires _fire_price_change when book.mid is not None.
        The RTDS spot path must still trigger the delta SL.

        Before the fix: YES book guard returned early → SL missed → RESOLVED wipeout.
        After the fix:  momentum bypasses the guard → delta SL fires.
        """
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 10  # only fire NE if <10s TTE

        # spot 0.1% below strike → delta = −0.1% < −0.05% → SL fires
        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        # YES CLOB book completely empty (simulates market near expiry)
        self._make_market_and_books(pm, yes_mid=None,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(pos)

        # Fire the RTDS spot path — as if Pyth WS received a price tick for BTC
        _run(monitor._on_spot_update("BTC", 99_900.0))

        assert risk._positions["mkt_001:YES"].is_closed, (
            "Delta SL must fire via RTDS spot path even when YES CLOB book is empty"
        )
        pm.place_market.assert_called_once()  # force_taker=True for stop-loss

    def test_spot_tick_no_fire_when_spot_within_threshold(self):
        """Delta SL does NOT fire when position is still clearly in-the-money.

        With protective-buffer semantics, SL fires when delta < +SL_PCT.
        spot=100_100 gives delta_yes = +0.1% > +0.05% → no fire.
        """
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 10

        # spot 0.1% ABOVE strike → YES is in-the-money, delta = +0.1% > +0.05% → no fire
        spot = _make_mock_spot(spot=100_100.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        self._make_market_and_books(pm, yes_mid=None,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(pos)

        _run(monitor._on_spot_update("BTC", 100_100.0))

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "Delta SL must NOT fire when position is still clearly in-the-money"
        )

    def test_spot_tick_ignores_wrong_coin(self):
        """ETH tick does not affect a BTC position."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05

        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        self._make_market_and_books(pm, yes_mid=None,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(pos)

        # ETH tick — underlying is BTC, so this should be filtered out
        _run(monitor._on_spot_update("ETH", 2_905.0))

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "ETH tick must not trigger a check on a BTC momentum position"
        )

    def test_spot_tick_skips_non_momentum_positions(self):
        """RTDS spot path only checks momentum positions, not maker/mispricing."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05

        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        self._make_market_and_books(pm, yes_mid=0.50,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.50,
            size=100.0, seconds_ago=120, now=now,
        )
        risk.open_position(pos)

        _run(monitor._on_spot_update("BTC", 99_900.0))

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "RTDS spot path must skip non-momentum positions"
        )

    def test_spot_tick_double_exit_guard(self):
        """Exit already in-flight is not re-triggered by a second RTDS spot tick."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05

        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        self._make_market_and_books(pm, yes_mid=None,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(pos)

        # Pre-mark as exiting (simulates first exit in-flight)
        monitor._exiting_positions.add("mkt_001:YES")

        _run(monitor._on_spot_update("BTC", 99_900.0))

        pm.place_market.assert_not_called()

    def test_spot_no_client_does_not_crash(self):
        """Monitor without spot_client wired does not register price callback — no crash."""
        monitor, pm, risk = _make_monitor(spot_client=None)
        # _on_spot_update is never called when spot_client is None (not registered),
        # but if somehow called manually it should be a no-op (no positions match).
        now = datetime.now(timezone.utc)
        self._make_market_and_books(pm, yes_mid=0.50,
                                     end_date=now + timedelta(seconds=120))
        pos = _make_position(strategy="momentum", side="YES", seconds_ago=60, now=now)
        risk.open_position(pos)

        # Should not raise even without spot_client
        _run(monitor._on_spot_update("BTC", 99_050.0))

    def test_spot_tick_no_position_does_not_crash(self):
        """RTDS spot tick with no open positions is a no-op."""
        spot = _make_mock_spot(spot=99_900.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)
        self._make_market_and_books(pm)
        # No positions registered
        _run(monitor._on_spot_update("BTC", 99_900.0))

    def test_pyth_tick_fires_delta_sl_no_position_both_books_empty(self):
        """NO position: spot crosses above strike, both YES and NO books are empty.
        Delta SL fires and exits at entry_price (worst-case fallback)."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS = 10

        # spot 0.1% above strike → delta_no = −0.1% < −0.05% → SL fires for NO position
        spot = _make_mock_spot(spot=100_100.0)
        monitor, pm, risk = _make_monitor(spot_client=spot)

        now = datetime.now(timezone.utc)
        # Both books empty
        self._make_market_and_books(pm, yes_mid=None, no_mid=None,
                                     end_date=now + timedelta(seconds=120))

        pos = _make_position(
            strategy="momentum", side="NO", entry_price=0.15,
            size=100.0, seconds_ago=60, now=now, strike=100_000.0,
        )
        risk.open_position(pos)

        _run(monitor._on_spot_update("BTC", 100_100.0))

        assert risk._positions["mkt_001:NO"].is_closed, (
            "Delta SL must fire for NO position even when both YES and NO books are empty"
        )
        pm.place_market.assert_called_once()


# ── GTD Hedge: cancel on close + redeem token intercept ──────────────────────

class TestGtdHedgeCancelOnClose:
    """GTD hedge cancel_order behaviour on position close.

    New rule: cancel hedge ONLY on take-profit (we won — insurance unneeded).
    On stop-loss/near-expiry exits keep the hedge alive — it is the recovery leg.
    On RESOLVED exits PM auto-expires orders; never send explicit cancel.

    Covers the cancel logic in _exit_position:
        _hedge_cancel_on_win = {ExitReason.PROFIT_TARGET, ExitReason.MOMENTUM_TAKE_PROFIT}
        if closed and closed.hedge_order_id and reason in _hedge_cancel_on_win:
            asyncio.create_task(self._pm.cancel_order(closed.hedge_order_id))
    """

    def _make_market(self, market_id="mkt_001", end_date=None):
        mkt = MagicMock()
        mkt.condition_id = market_id
        mkt.token_id_yes = "tok_yes"
        mkt.token_id_no  = "tok_no"
        mkt.fees_enabled = False
        mkt.end_date     = end_date
        mkt.title        = "Will BTC exceed $100k?"
        return mkt

    def _make_book(self, mid):
        book = MagicMock()
        book.mid = mid
        book.best_bid = mid
        book.best_ask = mid
        return book

    def _open_with_hedge(self, risk):
        """Open a YES mispricing position and attach a GTD hedge order."""
        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.50,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)
        risk.update_gtd_hedge(
            market_id="mkt_001",
            side="YES",
            hedge_order_id="gtd_order_hedge_001",
            hedge_token_id="tok_no_hedge",
            hedge_price=0.04,
            hedge_size_usd=5.0,
        )
        return pos

    def test_no_cancel_on_stop_loss(self):
        """SL exit → hedge kept alive (recovery leg); cancel_order must NOT be called."""
        config.STOP_LOSS_USD = 20.0
        config.PROFIT_TARGET_PCT = 0.99  # effectively disabled
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm.cancel_order = AsyncMock(return_value=True)
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.25)  # pnl=(0.25-0.50)*100=-$25 → SL

        pos = self._open_with_hedge(risk)
        monitor.record_entry_deviation("mkt_001", 0.10)
        _run(monitor._check_position(pos))

        assert risk._positions["mkt_001:YES"].is_closed, "Position should be closed by SL"
        pm.cancel_order.assert_not_called()  # hedge stays alive as recovery leg

    def test_cancel_on_profit_target(self):
        """Profit target exit → we won; cancel_order called to tear down hedge."""
        config.PROFIT_TARGET_PCT = 0.60
        config.STOP_LOSS_USD = 999.0  # disabled
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm.cancel_order = AsyncMock(return_value=True)
        pm._markets["mkt_001"] = self._make_market()
        # entry=0.40, mid=0.55 → pnl=$15; deviation=0.20, target=$12 → TP fires
        pm._books["tok_yes"] = self._make_book(mid=0.55)

        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.40,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)
        risk.update_gtd_hedge(
            market_id="mkt_001", side="YES",
            hedge_order_id="gtd_order_hedge_001",
            hedge_token_id="tok_no_hedge",
            hedge_price=0.04, hedge_size_usd=5.0,
        )
        monitor.record_entry_deviation("mkt_001", 0.20)  # target = 0.20*0.60*100 = $12
        _run(monitor._check_position(pos))

        assert risk._positions["mkt_001:YES"].is_closed, "Position should be closed by TP"
        pm.cancel_order.assert_called_once_with("gtd_order_hedge_001")

    def test_no_cancel_on_resolved_exit(self):
        """RESOLVED exit: cancel_order must NOT be called (PM auto-expires orders)."""
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 0
        config.STOP_LOSS_USD = 999.0
        config.PROFIT_TARGET_PCT = 0.99

        monitor, pm, risk = _make_monitor()
        pm.cancel_order = AsyncMock(return_value=True)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        pm._markets["mkt_001"] = self._make_market(end_date=past)
        pm._books["tok_yes"] = self._make_book(mid=0.50)
        pm.fetch_market_resolution = AsyncMock(return_value=0.0)  # YES settles to 0

        pos = self._open_with_hedge(risk)
        _run(monitor._check_position(pos))

        assert risk._positions["mkt_001:YES"].is_closed, "Position should close via RESOLVED"
        pm.cancel_order.assert_not_called()

    def test_no_cancel_if_no_hedge_order_id(self):
        """Position without hedge_order_id: cancel_order is never called."""
        config.STOP_LOSS_USD = 20.0
        config.PROFIT_TARGET_PCT = 0.99
        config.EXIT_DAYS_BEFORE_RESOLUTION = 0
        config.MIN_HOLD_SECONDS = 60

        monitor, pm, risk = _make_monitor()
        pm.cancel_order = AsyncMock(return_value=True)
        pm._markets["mkt_001"] = self._make_market()
        pm._books["tok_yes"] = self._make_book(mid=0.25)

        # Open WITHOUT attaching a hedge
        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.50,
            size=100.0, seconds_ago=120,
        )
        risk.open_position(pos)
        monitor.record_entry_deviation("mkt_001", 0.10)
        _run(monitor._check_position(pos))

        assert risk._positions["mkt_001:YES"].is_closed
        pm.cancel_order.assert_not_called()


class TestGtdHedgeRedeemIntercept:
    """_redeem_ready_positions routes hedge tokens without corrupting the main position.

    Covers:
    - Hedge token winner (payout>0): record_hedge_fill called; main position stays open
    - Hedge token loser (payout=0): dismissed silently; main position stays open
    - Normal (non-hedge) token winner: existing close_position flow unchanged
    """

    def setup_method(self):
        import risk as risk_module
        from pathlib import Path
        import tempfile
        self._risk_module = risk_module
        self._orig_csv = risk_module.TRADES_CSV
        self._tmp = tempfile.mkdtemp()
        risk_module.TRADES_CSV = Path(self._tmp) / "trades.csv"

    def teardown_method(self):
        self._risk_module.TRADES_CSV = self._orig_csv

    def _make_pm(self, token_id, redeemable, cur_price, condition_id="mkt_001",
                 title="BTC > 100k", size=10.0):
        pm = MagicMock()
        pm._markets = {}
        pm._books = {}
        pm.place_limit  = AsyncMock(return_value="paper_order_001")
        pm.place_market = AsyncMock(return_value="paper_mkt_001")
        pm.on_price_change = MagicMock()
        pm.get_token_balance = AsyncMock(return_value=None)
        pm.fetch_market_resolution = AsyncMock(return_value=None)
        pm.get_live_positions = AsyncMock(return_value=[{
            "asset": token_id,
            "redeemable": redeemable,
            "size": size,
            "curPrice": cur_price,
            "conditionId": condition_id,
            "title": title,
        }])
        pm.get_markets = MagicMock(return_value={})
        return pm

    def _open_main_pos_with_hedge(self, risk, hedge_token_id, market_id="mkt_001"):
        """Open a momentum YES position and attach a GTD hedge pointing at hedge_token_id."""
        pos = _make_position(
            strategy="momentum", side="YES", entry_price=0.85,
            size=100.0, seconds_ago=120, market_id=market_id,
        )
        risk.open_position(pos)
        risk.update_gtd_hedge(
            market_id=market_id,
            side="YES",
            hedge_order_id="gtd_order_999",
            hedge_token_id=hedge_token_id,
            hedge_price=0.04,
            hedge_size_usd=5.0,
        )
        return pos

    def test_winner_calls_record_hedge_fill(self):
        """Hedge token settles at 1.0 → record_hedge_fill called; main position stays open."""
        hedge_token_id = "tok_hedge_no_001"
        pm = self._make_pm(token_id=hedge_token_id, redeemable=True, cur_price=1.0)
        # CLOB: YES lost (resolved_yes=0.0) → NO/hedge token won (settled_price=1.0)
        pm.fetch_market_resolution = AsyncMock(return_value=0.0)
        mkt_h = MagicMock()
        mkt_h.token_id_yes = "tok_yes_opposite_001"
        mkt_h.token_id_no = hedge_token_id
        pm.get_markets = MagicMock(return_value={"mkt_001": mkt_h})
        risk = RiskEngine()
        config.MOMENTUM_DELTA_SL_MIN_TICKS = 1
        monitor = PositionMonitor(pm=pm, risk=risk, interval=30)
        self._open_main_pos_with_hedge(risk, hedge_token_id)
        before_pnl = risk.realized_pnl

        with patch("monitor._redeem_ctf_via_safe", AsyncMock(return_value="0xhash")):
            _run(monitor._redeem_ready_positions())

        # Main position must still be open — hedge token's settlement must not close it
        assert not risk._positions["mkt_001:YES"].is_closed, (
            "Main position must stay open after hedge winner is processed"
        )
        # realized_pnl must have increased: (1.0 - 0.04) * 10 = 9.6
        assert risk.realized_pnl > before_pnl, "Hedge fill should add to realized P&L"

    def test_loser_dismissed_without_closing_main_position(self):
        """Hedge token settles at 0.0 → outcome recorded as filled_lost; main position stays open."""
        hedge_token_id = "tok_hedge_no_002"
        pm = self._make_pm(token_id=hedge_token_id, redeemable=True, cur_price=0.0)
        # CLOB: YES won (resolved_yes=1.0) → NO/hedge token lost (settled_price=0.0)
        pm.fetch_market_resolution = AsyncMock(return_value=1.0)
        mkt_h = MagicMock()
        mkt_h.token_id_yes = "tok_yes_opposite_002"
        mkt_h.token_id_no = hedge_token_id
        pm.get_markets = MagicMock(return_value={"mkt_001": mkt_h})
        risk = RiskEngine()
        config.MOMENTUM_DELTA_SL_MIN_TICKS = 1
        monitor = PositionMonitor(pm=pm, risk=risk, interval=30)
        self._open_main_pos_with_hedge(risk, hedge_token_id)
        hedge_pos = risk._positions["mkt_001:YES"]
        expected_pnl_delta = -(hedge_pos.hedge_price * 10.0)  # fill_size=10 (default size param)

        _run(monitor._redeem_ready_positions())

        assert not risk._positions["mkt_001:YES"].is_closed, (
            "Main position must not be closed by a losing hedge token"
        )
        # New behaviour: filled_lost records (settled_price - fill_price) * fill_size = cost of hedge
        assert abs(risk.realized_pnl - expected_pnl_delta) < 1e-6, (
            f"filled_lost should record hedge cost; expected {expected_pnl_delta}, got {risk.realized_pnl}"
        )

    def test_non_hedge_token_normal_close_flow(self):
        """Regular (non-hedge) token settles at 1.0 → normal close_position path runs."""
        regular_token_id = "tok_yes_regular"
        pm = self._make_pm(
            token_id=regular_token_id, redeemable=True, cur_price=1.0,
            condition_id="mkt_002", title="ETH > 3k",
        )
        # Provide a market so the normal winner path can find the token and close positions
        mkt = MagicMock()
        mkt.condition_id = "mkt_002"
        mkt.token_id_yes = regular_token_id
        mkt.token_id_no  = "tok_no_regular"
        pm.get_markets = MagicMock(return_value={"mkt_002": mkt})
        # CLOB: YES won (resolved_yes=1.0) → YES token settled_price=1.0
        pm.fetch_market_resolution = AsyncMock(return_value=1.0)

        risk = RiskEngine()
        config.MOMENTUM_DELTA_SL_MIN_TICKS = 1
        monitor = PositionMonitor(pm=pm, risk=risk, interval=30)

        pos = _make_position(
            strategy="mispricing", side="YES", entry_price=0.40,
            size=10.0, seconds_ago=120, market_id="mkt_002",
        )
        risk.open_position(pos)

        with patch("monitor._redeem_ctf_via_safe", AsyncMock(return_value="0xhash")):
            _run(monitor._redeem_ready_positions())

        assert risk._positions["mkt_002:YES"].is_closed, (
            "Normal (non-hedge) winner token must trigger close_position"
        )


class TestPendingResolutionHedgeBackfill:
    """_record_pending_resolution_hedge writes independent hedge outcomes."""

    def setup_method(self):
        import risk as risk_module
        import tempfile

        self._risk_module = risk_module
        self._orig_csv = risk_module.TRADES_CSV
        self._tmp = tempfile.mkdtemp()
        risk_module.TRADES_CSV = Path(self._tmp) / "trades.csv"

    def teardown_method(self):
        self._risk_module.TRADES_CSV = self._orig_csv

    def _read_hedge_rows(self):
        import csv

        rows = []
        with self._risk_module.TRADES_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("strategy") == "momentum_hedge":
                    rows.append(row)
        return rows

    def _open_and_close_parent(self, risk: RiskEngine, *, side: str = "YES") -> None:
        pos = _make_position(
            strategy="momentum",
            side=side,
            entry_price=0.85,
            size=100.0,
            seconds_ago=120,
            market_id="mkt_001",
        )
        risk.open_position(pos)
        risk.update_gtd_hedge(
            market_id="mkt_001",
            side=side,
            hedge_order_id="paper-hedge-001",
            hedge_token_id="tok_no_hedge",
            hedge_price=0.04,
            hedge_size_usd=5.0,
        )
        risk.close_position(
            market_id="mkt_001",
            side=side,
            exit_price=0.60,
            resolved_outcome="",
        )

    def test_records_filled_won_from_paper_fill(self):
        monitor, pm, risk = _make_monitor()
        pm.get_markets = MagicMock(return_value={})
        pm.fetch_resolve_spot_price = AsyncMock(return_value=None)

        self._open_and_close_parent(risk, side="YES")
        risk.record_paper_hedge_fill_sim(
            hedge_token_id="tok_no_hedge",
            fill_price=0.04,
            fill_size=10.0,
        )

        # YES resolved to 0.0 -> main lost -> opposite hedge token won.
        _run(monitor._record_pending_resolution_hedge("mkt_001", 0.0))

        hedge_rows = self._read_hedge_rows()
        assert len(hedge_rows) == 1
        assert hedge_rows[0]["hedge_status"] == "filled_won"
        assert float(hedge_rows[0]["size"]) == pytest.approx(10.0)
        assert risk.get_paper_hedge_fill("tok_no_hedge") is None

    def test_records_unfilled_without_paper_fill(self):
        monitor, pm, risk = _make_monitor()
        pm.get_markets = MagicMock(return_value={})
        pm.fetch_resolve_spot_price = AsyncMock(return_value=None)

        self._open_and_close_parent(risk, side="YES")
        _run(monitor._record_pending_resolution_hedge("mkt_001", 1.0))

        hedge_rows = self._read_hedge_rows()
        assert len(hedge_rows) == 1
        assert hedge_rows[0]["hedge_status"] == "unfilled"
        assert float(hedge_rows[0]["size"]) == pytest.approx(0.0)

# ── Item 7: Probability-based SL in should_exit ───────────────────────────────


class TestProbSlShouldExit:

    NOW = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _momentum_pos(self, entry_price=0.85, prob_sl_threshold=0.0):
        pos = _make_position(
            entry_price=entry_price, size=50.0, seconds_ago=120,
            strategy="momentum", side="YES", strike=100_000.0,
        )
        pos.prob_sl_threshold = prob_sl_threshold
        return pos

    def test_prob_sl_fires_when_price_below_threshold(self):
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS = 10.0
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # disable oracle SL
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 60

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.75,
            current_token_price=0.75,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
            oracle_age_seconds=25.0,  # stale — confirmed oracle lag
        )
        assert exit_flag is True
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_prob_sl_does_not_fire_at_threshold(self):
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 60

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        # Exactly at threshold -- strict < means no trigger
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.80,
            current_token_price=0.80,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_prob_sl_does_not_fire_when_disabled(self):
        config.MOMENTUM_PROB_SL_ENABLED = False
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 60

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.60,  # well below threshold
            current_token_price=0.60,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_prob_sl_does_not_fire_when_threshold_zero(self):
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 60

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.0)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.10,  # extremely low but threshold is 0 (not set)
            current_token_price=0.10,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_prob_sl_above_threshold_no_exit(self):
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 60

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.70)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.88,
            current_token_price=0.88,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(minutes=10),
            now=self.NOW,
        )
        assert not exit_flag

    def test_prob_sl_suppressed_near_expiry(self):
        """Bug fix: prob-SL must not fire when TTE < MOMENTUM_PROB_SL_MIN_TTE_SECS.
        Near expiry the CLOB book drains, pushing mid to ~0.50 even on winning
        positions — the oracle-delta SL is the correct protection in this window."""
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_MIN_TTE_SECS = 300
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0  # disable oracle SL for isolation
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        pos = self._momentum_pos(entry_price=0.96, prob_sl_threshold=0.816)
        # Token price collapsed to 0.49 (drained book: bid=0.01, ask=0.97, mid=0.49)
        # but we are only 250 s from expiry — guard must suppress the prob-SL.
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.49,
            current_token_price=0.49,
            current_spot=100_100.0,   # spot still above strike → winning position
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=250),
            tte_seconds=250.0,        # below the 300-second guard
            now=self.NOW,
        )
        assert exit_flag is False, "prob-SL must be suppressed when TTE < guard"

    def test_prob_sl_fires_when_tte_above_guard(self):
        """Prob-SL must still fire normally when TTE >= MOMENTUM_PROB_SL_MIN_TTE_SECS."""
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_MIN_TTE_SECS = 300
        config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS = 10.0
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = -999.0
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.70,
            current_token_price=0.70,
            current_spot=100_000.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=600),
            tte_seconds=600.0,        # above the 300-second guard
            now=self.NOW,
            oracle_age_seconds=25.0,  # stale — confirmed oracle lag
        )
        assert exit_flag is True
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_prob_sl_blocked_when_oracle_fresh(self):
        """Oracle-lag gate: prob-SL must NOT fire when oracle ticked recently.
        A fresh oracle confirms spot hasn't moved — CLOB drop is book-drain noise."""
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_MIN_TTE_SECS = 300
        config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS = 10.0
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.02   # real delta SL (not disabled)
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        # Oracle is fresh (2 s ago): delta is comfortably ITM (0.5%), CLOB drops
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.70,
            current_token_price=0.70,
            current_spot=100_500.0,  # 0.5% above strike — comfortably ITM
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=600),
            tte_seconds=600.0,
            now=self.NOW,
            oracle_age_seconds=2.0,  # fresh — within 10s window
        )
        assert exit_flag is False, "prob-SL must be blocked when oracle is fresh"

    def test_prob_sl_fires_when_oracle_stale(self):
        """Oracle-lag gate: prob-SL MUST fire when oracle is stale.
        Stale oracle = CLOB drop may be a real move the oracle hasn't reported."""
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_MIN_TTE_SECS = 300
        config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS = 10.0
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.02
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.70,
            current_token_price=0.70,
            current_spot=100_500.0,  # oracle says 0.5% ITM — but oracle is stale
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=600),
            tte_seconds=600.0,
            now=self.NOW,
            oracle_age_seconds=25.0,  # stale — beyond 10s window
        )
        assert exit_flag is True
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_prob_sl_blocked_when_oracle_age_unknown(self):
        """Oracle-lag gate: prob-SL must be BLOCKED when oracle_age_seconds is None.
        Unknown timing = treat as fresh (conservative) — do not fire on missing data."""
        config.MOMENTUM_PROB_SL_ENABLED = True
        config.MOMENTUM_PROB_SL_MIN_TTE_SECS = 300
        config.MOMENTUM_PROB_SL_ORACLE_STALE_SECS = 10.0
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.02
        config.MOMENTUM_TAKE_PROFIT = 0.999
        config.MIN_HOLD_SECONDS = 0

        pos = self._momentum_pos(entry_price=0.85, prob_sl_threshold=0.80)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.70,
            current_token_price=0.70,
            current_spot=100_500.0,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(seconds=600),
            tte_seconds=600.0,
            now=self.NOW,
            oracle_age_seconds=None,  # no timing info — must be blocked
        )
        assert exit_flag is False, "prob-SL must be blocked when oracle timing is unknown"


class TestRangeStrategyExits:
    """Range positions (strategy='range') must use delta-based SL.
    Previously they hit the 'unknown' catch-all and were held to resolved."""

    NOW = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _range_pos(self, entry_price=0.84, spot_at_entry=72400.0, strike=73000.0,
                   range_lo=72000.0, range_hi=74000.0):
        pos = _make_position(
            entry_price=entry_price, size=20.0, seconds_ago=120,
            strategy="range", side="YES", strike=strike,
        )
        pos.spot_price = spot_at_entry
        pos.range_lo = range_lo
        pos.range_hi = range_hi
        return pos

    def test_range_delta_sl_fires_when_spot_below_strike(self):
        """Range YES: when spot drops below the range midpoint-strike, SL fires."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.04
        config.MIN_HOLD_SECONDS = 0

        pos = self._range_pos(entry_price=0.84, spot_at_entry=72400.0, strike=73000.0)
        # Spot drops far below range midpoint → delta strongly negative
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.50,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(hours=2),
            now=self.NOW,
            current_spot=71000.0,   # 72K-74K range; spot at 71K → outside and below
        )
        assert exit_flag is True
        assert reason == ExitReason.MOMENTUM_STOP_LOSS

    def test_range_no_exit_when_spot_inside_range(self):
        """Range YES: spot inside the range → delta positive → no SL."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.04
        config.MIN_HOLD_SECONDS = 0

        pos = self._range_pos(entry_price=0.84, spot_at_entry=72400.0, strike=73000.0)
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.84,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(hours=2),
            now=self.NOW,
            current_spot=72800.0,   # still inside 72K-74K range
        )
        assert exit_flag is False

    def test_range_no_exit_without_spot_data(self):
        """Range: if oracle spot unavailable, no SL (never fire on stale/missing data)."""
        config.MOMENTUM_DELTA_STOP_LOSS_PCT = 0.04
        config.MIN_HOLD_SECONDS = 0

        pos = self._range_pos()
        exit_flag, _, _ = should_exit(
            pos=pos,
            current_price=0.50,
            initial_deviation=0.0,
            market_end_date=self.NOW + timedelta(hours=2),
            now=self.NOW,
            current_spot=None,  # oracle unavailable
        )
        assert exit_flag is False

    def test_range_global_resolved_still_fires(self):
        """RESOLVED is a global exit that must fire for range positions too."""
        config.MIN_HOLD_SECONDS = 0
        pos = self._range_pos()
        past_end = self.NOW - timedelta(seconds=10)
        exit_flag, reason, _ = should_exit(
            pos=pos,
            current_price=0.84,
            initial_deviation=0.0,
            market_end_date=past_end,
            now=self.NOW,
        )
        assert exit_flag is True
        assert reason == ExitReason.RESOLVED
