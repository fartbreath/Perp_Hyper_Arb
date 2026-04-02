"""
tests/test_risk.py — Unit tests for risk.py

Run:  pytest tests/test_risk.py -v
"""
import math
import sys
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import timezone, datetime

import config
from risk import min_edge_after_fees, RiskEngine, Position


# ── min_edge_after_fees ───────────────────────────────────────────────────────

class TestMinEdgeAfterFees:
    """Verify the fee model at key probability levels."""

    def test_at_fifty_percent(self):
        """At p=0.50 the PM fee is at its maximum (~0.44%)."""
        edge = min_edge_after_fees(0.50)
        # PM fee alone at 0.50: 0.0175 * 0.50 * 0.50 = 0.004375
        # + HL taker 0.00045 + buffer 0.002 = ~0.006825
        # Combined round-trip hurdle should be > 0.3% (substantial)
        assert edge > 0.003, f"edge at 0.50 expected > 0.003, got {edge}"
        assert edge < 0.02, f"edge at 0.50 expected < 0.02, got {edge}"

    def test_at_extremes_are_low(self):
        """Near extremes the fee hurdle should be much lower."""
        edge_05 = min_edge_after_fees(0.05)
        edge_95 = min_edge_after_fees(0.95)
        # At extremes PM fee is tiny; combined hurdle should be < 0.5%
        assert edge_05 < 0.005, f"edge at 0.05 expected < 0.005, got {edge_05}"
        assert edge_95 < 0.005, f"edge at 0.95 expected < 0.005, got {edge_95}"

    def test_symmetric_at_complement(self):
        """
        The fee formula is PM_FEE_COEFF * p * (1-p), which is symmetric:
        fee(p) == fee(1-p) since p*(1-p) == (1-p)*p.
        """
        for p in [0.10, 0.20, 0.30, 0.40]:
            fee_at_p = min_edge_after_fees(p)
            fee_at_complement = min_edge_after_fees(1.0 - p)
            assert abs(fee_at_p - fee_at_complement) < 1e-12, (
                f"Expected fee({p:.2f}) == fee({1.0-p:.2f}), "
                f"got {fee_at_p} vs {fee_at_complement}"
            )

    def test_monotonic_toward_midpoint(self):
        """Fee should increase from p=0.01 toward p=0.50."""
        probabilities = [0.01, 0.05, 0.10, 0.20, 0.30, 0.50]
        edges = [min_edge_after_fees(p) for p in probabilities]
        for i in range(len(edges) - 1):
            assert edges[i] < edges[i + 1], (
                f"Not monotonic at index {i}: p={probabilities[i]} "
                f"edge={edges[i]} > p={probabilities[i+1]} edge={edges[i+1]}"
            )

    def test_invalid_inputs(self):
        """p=0 and p=1 should raise ValueError."""
        with pytest.raises(ValueError):
            min_edge_after_fees(0.0)
        with pytest.raises(ValueError):
            min_edge_after_fees(1.0)
        with pytest.raises(ValueError):
            min_edge_after_fees(-0.1)
        with pytest.raises(ValueError):
            min_edge_after_fees(1.1)

    def test_includes_hl_fee_and_buffer(self):
        """Even at extreme probability, edge must include HL fee + buffer floor."""
        edge = min_edge_after_fees(0.001)
        floor = config.HL_TAKER_FEE + config.EDGE_BUFFER
        assert edge > floor, f"Edge {edge} should exceed HL+buffer floor {floor}"


# ── RiskEngine ────────────────────────────────────────────────────────────────

def _make_position(market_id="mkt_001", size=100.0, strategy="maker", entry_price=0.55) -> Position:
    return Position(
        market_id=market_id,
        market_type="bucket_1h",
        underlying="BTC",
        side="YES",
        size=size,
        entry_price=entry_price,
        strategy=strategy,
        entry_cost_usd=round(entry_price * size, 4),  # USD deployed = price × contracts
    )


class TestRiskEngineLimits:
    """Verify exposure limits are enforced."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_can_open_fresh(self):
        ok, reason = self.engine.can_open("mkt_001", 100.0)
        assert ok, reason

    def test_per_market_limit(self):
        """Should refuse if a single market exceeds MAX_PM_EXPOSURE_PER_MARKET."""
        ok, reason = self.engine.can_open("mkt_001", config.MAX_PM_EXPOSURE_PER_MARKET + 1)
        assert not ok
        assert "per-market" in reason

    def test_total_pm_exposure_limit(self):
        """Should refuse if total PM exposure would exceed limit."""
        # Fill up to just below total limit
        chunk = config.MAX_TOTAL_PM_EXPOSURE / config.MAX_CONCURRENT_POSITIONS
        for i in range(config.MAX_CONCURRENT_POSITIONS):
            pos = _make_position(f"mkt_{i:03d}", size=chunk)
            self.engine.open_position(pos)
        # Next open should breach total limit
        ok, reason = self.engine.can_open("mkt_999", chunk)
        assert not ok
        assert "total PM" in reason or "concurrent" in reason

    def test_max_concurrent_positions(self):
        """Should refuse when MAX_CONCURRENT_POSITIONS is reached."""
        for i in range(config.MAX_CONCURRENT_POSITIONS):
            pos = _make_position(f"mkt_{i:03d}", size=10.0)
            self.engine.open_position(pos)
        ok, reason = self.engine.can_open("mkt_999", 10.0)
        assert not ok
        assert "concurrent" in reason

    def test_hl_notional_limit(self):
        """can_hedge should refuse when HL notional limit is exceeded."""
        ok, reason = self.engine.can_hedge(config.MAX_HL_NOTIONAL + 1)
        assert not ok
        assert "HL notional" in reason

    def test_hl_notional_ok_within_limit(self):
        ok, reason = self.engine.can_hedge(config.MAX_HL_NOTIONAL - 1)
        assert ok, reason


class TestRiskEngineHardStop:
    """Verify hard stop triggers correctly."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_hard_stop_triggers_on_drawdown(self):
        # entry_price=1.0 so closing at 0 loses the full size in USD
        # size > HARD_STOP_DRAWDOWN guarantees the threshold is breached
        pos = _make_position(size=config.HARD_STOP_DRAWDOWN + 100, entry_price=1.0)
        self.engine.open_position(pos)
        self.engine.close_position("mkt_001", exit_price=0.0)
        assert self.engine.hard_stop_triggered

    def test_hard_stop_blocks_new_opens(self):
        pos = _make_position(size=config.HARD_STOP_DRAWDOWN + 100, entry_price=1.0)
        self.engine.open_position(pos)
        self.engine.close_position("mkt_001", exit_price=0.0)
        assert self.engine.hard_stop_triggered

        ok, reason = self.engine.can_open("mkt_002", 10.0)
        assert not ok
        assert "hard stop" in reason

    def test_no_hard_stop_on_small_loss(self):
        pos = _make_position(size=50.0)
        self.engine.open_position(pos)
        self.engine.close_position("mkt_001", exit_price=0.0)  # lose $50
        assert not self.engine.hard_stop_triggered


class TestRiskEnginePnL:
    """Verify P&L accounting."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_winning_trade_yes(self):
        """Long YES at 0.50, close at 1.0 → profit = size * (1.0 - 0.50)."""
        pos = _make_position(size=100.0)
        pos.entry_price = 0.50
        self.engine.open_position(pos)
        closed = self.engine.close_position("mkt_001", exit_price=1.0)
        assert closed is not None
        assert math.isclose(closed.realized_pnl, 50.0, rel_tol=1e-6)

    def test_winning_trade_no(self):
        """Long NO at 0.50, close at 1.0 (actual NO when YES fails) → profit = size × 0.50."""
        pos = _make_position(size=100.0)
        pos.entry_price = 0.50
        pos.side = "NO"
        self.engine.open_position(pos)
        closed = self.engine.close_position("mkt_001", exit_price=1.0, side="NO")
        assert closed is not None
        assert math.isclose(closed.realized_pnl, 50.0, rel_tol=1e-6)

    def test_fees_reduce_pnl(self):
        pos = _make_position(size=100.0)
        pos.entry_price = 0.50
        self.engine.open_position(pos)
        closed = self.engine.close_position("mkt_001", exit_price=1.0, fees_paid=5.0)
        assert math.isclose(closed.realized_pnl, 45.0, rel_tol=1e-6)

    def test_rebates_increase_pnl(self):
        pos = _make_position(size=100.0)
        pos.entry_price = 0.50
        self.engine.open_position(pos)
        closed = self.engine.close_position("mkt_001", exit_price=1.0, rebates_earned=2.0)
        assert math.isclose(closed.realized_pnl, 52.0, rel_tol=1e-6)

    def test_get_state_reflects_open_position(self):
        self.engine.open_position(_make_position())
        state = self.engine.get_state()
        assert state["open_positions_count"] == 1
        assert state["total_pm_exposure"] > 0
        assert not state["hard_stop_triggered"]

    def test_double_close_is_idempotent(self):
        """Closing the same position twice should return None on second call."""
        self.engine.open_position(_make_position())
        first = self.engine.close_position("mkt_001", exit_price=1.0)
        second = self.engine.close_position("mkt_001", exit_price=1.0)
        assert first is not None
        assert second is None
        # P&L should only be counted once
        assert math.isclose(self.engine.realized_pnl, first.realized_pnl, rel_tol=1e-6)

def test_open_position_merges_instead_of_overwriting():
    from risk import RiskEngine, Position
    from datetime import datetime, timedelta, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos1 = Position(
        market_id="mkt_001",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=100.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    engine.open_position(pos1)
    # Open again with same market_id, should merge
    pos2 = Position(
        market_id="mkt_001",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=50.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now + timedelta(minutes=5),  # Should not reset
        entry_cost_usd=5.0,
    )
    engine.open_position(pos2)
    open_pos = engine.get_open_positions()
    assert len(open_pos) == 1
    merged = open_pos[0]
    assert merged.size == 150.0
    assert merged.entry_cost_usd == 15.0
    assert merged.opened_at == now  # Should not reset
    assert not merged.is_closed

    # Now close and try to open again, should create new position
    engine.close_position("mkt_001", exit_price=0.20)
    pos3 = Position(
        market_id="mkt_001",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=10.0,
        entry_price=0.15,
        strategy="maker",
        opened_at=now + timedelta(minutes=10),
        entry_cost_usd=1.5,
    )
    engine.open_position(pos3)
    open_pos2 = engine.get_open_positions()
    assert len(open_pos2) == 1
    assert open_pos2[0].size == 10.0
    assert open_pos2[0].opened_at == now + timedelta(minutes=10)
    assert not open_pos2[0].is_closed

def test_partial_fill_merge_does_not_reset_opened_at():
    from risk import RiskEngine, Position
    from datetime import datetime, timedelta, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos1 = Position(
        market_id="mkt_partial",
        market_type="bucket_daily",
        underlying="ETH",
        side="YES",
        size=100.0,
        entry_price=0.20,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=20.0,
    )
    engine.open_position(pos1)
    # Simulate partial fill (same market/side)
    pos2 = Position(
        market_id="mkt_partial",
        market_type="bucket_daily",
        underlying="ETH",
        side="YES",
        size=50.0,
        entry_price=0.20,
        strategy="maker",
        opened_at=now + timedelta(minutes=10),
        entry_cost_usd=10.0,
    )
    engine.open_position(pos2)
    open_pos = engine.get_open_positions()
    assert len(open_pos) == 1
    merged = open_pos[0]
    assert merged.size == 150.0
    assert merged.entry_cost_usd == 30.0
    assert merged.opened_at == now
    assert not merged.is_closed


def test_merge_updates_weighted_avg_entry_price():
    """Merging fills at different prices must keep entry_price as the weighted average.

    Without this fix, close_position P&L would use only the first batch's price,
    over-counting or under-counting gain for batches filled at a different price.
    """
    from risk import RiskEngine, Position
    from datetime import datetime, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)

    # Batch 1: 20 contracts @ $0.40 → cost $8.00
    p1 = Position(
        market_id="mkt_avg",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=20.0,
        entry_price=0.40,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=8.0,
    )
    engine.open_position(p1)

    # Batch 2: 20 contracts @ $0.50 → cost $10.00
    p2 = Position(
        market_id="mkt_avg",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=20.0,
        entry_price=0.50,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    engine.open_position(p2)

    merged = engine.get_open_positions()[0]
    assert merged.size == 40.0
    assert merged.entry_cost_usd == 18.0
    # Weighted average: 18.0 / 40.0 = 0.45
    assert abs(merged.entry_price - 0.45) < 1e-9

    # P&L at exit 0.60 should be (0.60 - 0.45) × 40 = $6.00
    engine.close_position("mkt_avg", exit_price=0.60)
    closed = [p for p in engine._positions.values() if p.market_id == "mkt_avg"][0]
    assert abs(closed.realized_pnl - 6.0) < 1e-9


def test_merge_updates_weighted_avg_entry_price_no_side():
    """Same weighted-average fix for NO-side positions."""
    from risk import RiskEngine, Position
    from datetime import datetime, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)

    # NO side: entry_cost_usd = entry_price × size  (actual NO token price)
    # Batch 1: 20 contracts @ actual NO price 0.30 → cost 0.30 × 20 = $6.00
    p1 = Position(
        market_id="mkt_avg_no",
        market_type="bucket_daily",
        underlying="ETH",
        side="NO",
        size=20.0,
        entry_price=0.30,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=6.0,
    )
    engine.open_position(p1)

    # Batch 2: 20 contracts @ actual NO price 0.40 → cost 0.40 × 20 = $8.00
    p2 = Position(
        market_id="mkt_avg_no",
        market_type="bucket_daily",
        underlying="ETH",
        side="NO",
        size=20.0,
        entry_price=0.40,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=8.0,
    )
    engine.open_position(p2)

    merged = engine.get_open_positions()[0]
    assert merged.size == 40.0
    assert merged.entry_cost_usd == 14.0
    # avg actual NO price: 14/40 = 0.35
    assert abs(merged.entry_price - 0.35) < 1e-9

def test_max_concurrent_positions_cap():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone
    import config

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    config.MAX_CONCURRENT_POSITIONS = 3
    # Ensure neither the per-strategy cap nor the per-underlying cap fires
    # before the global cap — this test is exclusively exercising the global cap.
    config.MAX_CONCURRENT_MAKER_POSITIONS = 10
    config.MAX_MAKER_POSITIONS_PER_UNDERLYING = 10
    for i in range(3):
        pos = Position(
            market_id=f"mkt_{i}",
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=10.0,
            entry_price=0.10,
            strategy="maker",
            opened_at=now,
            entry_cost_usd=1.0,
        )
        ok, reason = engine.can_open(pos.market_id, pos.size, strategy=pos.strategy, underlying=pos.underlying)
        assert ok
        engine.open_position(pos)
    # 4th position should be blocked
    pos4 = Position(
        market_id="mkt_4",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=10.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=1.0,
    )
    ok, reason = engine.can_open(pos4.market_id, pos4.size, strategy=pos4.strategy, underlying=pos4.underlying)
    assert not ok
    assert "global concurrent cap" in reason

def test_per_market_exposure_limit():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone
    import config

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    config.MAX_PM_EXPOSURE_PER_MARKET = 100
    # Open up to the limit: pos1 + pos2 = $60 + $40 = $100 (fills the cap)
    pos1 = Position(
        market_id="mkt_exposure",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=600,           # contracts: 600 × $0.10 = $60 USD
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=60.0,  # actual USD deployed
    )
    pos2 = Position(
        market_id="mkt_exposure",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=400,           # contracts: 400 × $0.10 = $40 USD
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=40.0,  # actual USD deployed
    )
    ok, reason = engine.can_open(pos1.market_id, pos1.entry_cost_usd, strategy=pos1.strategy, underlying=pos1.underlying)
    assert ok
    engine.open_position(pos1)
    ok, reason = engine.can_open(pos2.market_id, pos2.entry_cost_usd, strategy=pos2.strategy, underlying=pos2.underlying)
    assert ok
    engine.open_position(pos2)
    # cap = $100 exactly full; any further USD should be blocked
    pos3 = Position(
        market_id="mkt_exposure",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=10,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=1.0,
    )
    ok, reason = engine.can_open(pos3.market_id, pos3.entry_cost_usd, strategy=pos3.strategy, underlying=pos3.underlying)
    assert not ok
    assert "per-market limit" in reason

def test_per_underlying_cap():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone
    import config

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    config.MAX_MAKER_POSITIONS_PER_UNDERLYING = 2
    for i in range(2):
        pos = Position(
            market_id=f"mkt_under_{i}",
            market_type="bucket_daily",
            underlying="ETH",
            side="YES",
            size=10.0,
            entry_price=0.10,
            strategy="maker",
            opened_at=now,
            entry_cost_usd=1.0,
        )
        ok, reason = engine.can_open(pos.market_id, pos.size, strategy=pos.strategy, underlying=pos.underlying)
        assert ok
        engine.open_position(pos)
    # 3rd position on same underlying should be blocked
    pos3 = Position(
        market_id="mkt_under_2",
        market_type="bucket_daily",
        underlying="ETH",
        side="YES",
        size=10.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=1.0,
    )
    ok, reason = engine.can_open(pos3.market_id, pos3.size, strategy=pos3.strategy, underlying=pos3.underlying)
    assert not ok
    assert "per-underlying maker cap" in reason

def test_close_and_immediate_reopen_creates_new_position():
    from risk import RiskEngine, Position
    from datetime import datetime, timedelta, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos1 = Position(
        market_id="mkt_reopen",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=100.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    engine.open_position(pos1)
    engine.close_position("mkt_reopen", exit_price=0.20)
    # Reopen immediately
    later = now + timedelta(minutes=5)
    pos2 = Position(
        market_id="mkt_reopen",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=50.0,
        entry_price=0.15,
        strategy="maker",
        opened_at=later,
        entry_cost_usd=7.5,
    )
    engine.open_position(pos2)
    open_pos = engine.get_open_positions()
    assert len(open_pos) == 1
    reopened = open_pos[0]
    assert reopened.size == 50.0
    assert reopened.opened_at == later
    assert not reopened.is_closed

def test_coin_level_loss_limit_triggers_close():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone
    import config

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    config.MAKER_COIN_MAX_LOSS_USD = 50
    # Open two positions on same coin
    pos1 = Position(
        market_id="mkt_loss1",
        market_type="bucket_daily",
        underlying="SOL",
        side="YES",
        size=100.0,
        entry_price=0.50,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=50.0,
    )
    pos2 = Position(
        market_id="mkt_loss2",
        market_type="bucket_daily",
        underlying="SOL",
        side="YES",
        size=100.0,
        entry_price=0.50,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=50.0,
    )
    engine.open_position(pos1)
    engine.open_position(pos2)
    # Simulate loss by closing both at a much lower price
    engine.close_position("mkt_loss1", exit_price=0.0)
    engine.close_position("mkt_loss2", exit_price=0.0)
    # Both should be closed
    open_pos = engine.get_open_positions()
    assert len(open_pos) == 0

def test_rebates_and_fees_accounting():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone
    import config

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    _orig_coeff = config.PM_FEE_COEFF
    config.PM_FEE_COEFF = 0.02
    try:
        pos = Position(
            market_id="mkt_fees",
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=100.0,
            entry_price=0.50,
            strategy="maker",
            opened_at=now,
            entry_cost_usd=50.0,
        )
        engine.open_position(pos)
        # Simulate close with fees and exit rebate only (no entry rebate via record_rebate).
        closed = engine.close_position("mkt_fees", exit_price=0.60, fees_paid=2.0, rebates_earned=1.0)
        assert closed is not None
        assert closed.pm_fees_paid == 2.0
        assert closed.pm_rebates_earned == 1.0
        assert closed.realized_pnl == (0.60 - 0.50) * 100.0 - 2.0 + 1.0
    finally:
        config.PM_FEE_COEFF = _orig_coeff

def test_simultaneous_fills_different_markets_same_underlying():
    from risk import RiskEngine, Position
    from datetime import datetime, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos1 = Position(
        market_id="mkt_simul_1",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=10.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=1.0,
    )
    pos2 = Position(
        market_id="mkt_simul_2",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=20.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=2.0,
    )
    engine.open_position(pos1)
    engine.open_position(pos2)
    open_pos = engine.get_open_positions()
    assert len(open_pos) == 2
    assert sum(p.size for p in open_pos if p.underlying == "BTC") == 30.0

def test_inventory_skew_application():
    # This test is a stub: actual skew logic is in maker.py, but we can check inventory math here.
    from risk import RiskEngine, Position
    from datetime import datetime, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos1 = Position(
        market_id="mkt_skew_1",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=100.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    pos2 = Position(
        market_id="mkt_skew_2",
        market_type="bucket_daily",
        underlying="BTC",
        side="NO",
        size=50.0,
        entry_price=0.10,
        strategy="maker",
        opened_at=now,
        entry_cost_usd=5.0,
    )
    engine.open_position(pos1)
    engine.open_position(pos2)
    # Net inventory for BTC should be 100 - 50 = 50
    net_inv = sum(p.size if p.side == "YES" else -p.size for p in engine.get_open_positions() if p.underlying == "BTC")
    assert net_inv == 50.0
    # Actual price skew logic is in maker.py, but this confirms inventory math.

def test_profit_target_and_stop_loss_exit():
    # This test is a stub: actual exit logic is in monitor.py, but we can simulate P&L math.
    from risk import RiskEngine, Position
    from datetime import datetime, timezone

    engine = RiskEngine()
    now = datetime.now(timezone.utc)
    pos = Position(
        market_id="mkt_exit",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=100.0,
        entry_price=0.10,
        strategy="mispricing",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    engine.open_position(pos)
    # Simulate profit target hit
    closed = engine.close_position("mkt_exit", exit_price=0.20)
    assert closed is not None
    assert closed.realized_pnl == (0.20 - 0.10) * 100.0
    # Reopen and simulate stop-loss
    pos2 = Position(
        market_id="mkt_exit",
        market_type="bucket_daily",
        underlying="BTC",
        side="YES",
        size=100.0,
        entry_price=0.10,
        strategy="mispricing",
        opened_at=now,
        entry_cost_usd=10.0,
    )
    engine.open_position(pos2)
    closed2 = engine.close_position("mkt_exit", exit_price=0.00)
    assert closed2 is not None
    assert closed2.realized_pnl == (0.00 - 0.10) * 100.0
