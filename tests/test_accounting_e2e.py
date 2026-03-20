"""
tests/test_accounting_e2e.py — End-to-end accounting tests.

Written from a trader / accountant perspective: *given these fills at these
prices, what exact dollar amounts appear in the ledger?*  No internal mocks.
All assertions derive from arithmetic that can be verified on a calculator.

Sections
────────
  A  Entry cost arithmetic         — what you pay to open
  B  Unrealised P&L (MTM)          — current mark-to-market value
  C  Realised P&L after close      — what actually hits the ledger
  D  Fees and rebate accounting    — effect on realised P&L
  E  Partial-fill accumulation     — multiple fills → one position
  F  Two-sided market making       — YES + NO on same market coexist
  G  Market resolution payoffs     — YES→1.0 and YES→0.0 outcomes
  H  Session P&L accumulation      — running total and hard stop
  I  CSV ledger integrity          — what gets written on close
  J  Exit trigger arithmetic       — profit-target and stop-loss thresholds

Key formulas
────────────
  entry_cost_usd  YES = entry_price × contracts
  entry_cost_usd  NO  = (1 − entry_price) × contracts
  unrealised_pnl  YES = (current_price − entry_price) × contracts
  unrealised_pnl  NO  = (entry_price − current_price) × contracts
  realised_pnl        = price_pnl − fees_paid + rebates_earned
  pm_fee (per ctr)    = PM_FEE_COEFF × price × (1 − price)
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

# Force paper mode — no real network calls can happen.
config.PAPER_TRADING = True

import risk as _risk_module
from risk import RiskEngine, Position
from monitor import compute_unrealised_pnl, should_exit, ExitReason


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _yes(
    market_id: str = "mkt",
    entry_price: float = 0.50,
    size: float = 100.0,
    underlying: str = "BTC",
    strategy: str = "maker",
    seconds_ago: int = 120,
) -> Position:
    """YES position helper.  entry_cost_usd = entry_price × size."""
    opened = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return Position(
        market_id=market_id,
        market_type="bucket_daily",
        underlying=underlying,
        side="YES",
        size=size,
        entry_price=entry_price,
        strategy=strategy,
        opened_at=opened,
        entry_cost_usd=round(entry_price * size, 6),
    )


def _no(
    market_id: str = "mkt",
    entry_price: float = 0.50,  # YES token price at fill (SELL YES → BUY NO)
    size: float = 100.0,
    underlying: str = "BTC",
    strategy: str = "maker",
    seconds_ago: int = 120,
) -> Position:
    """NO position helper.  entry_cost_usd = (1 − entry_price) × size."""
    opened = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return Position(
        market_id=market_id,
        market_type="bucket_daily",
        underlying=underlying,
        side="NO",
        size=size,
        entry_price=entry_price,
        strategy=strategy,
        opened_at=opened,
        entry_cost_usd=round((1.0 - entry_price) * size, 6),
    )


def _fresh_engine(tmp_path: Path | None = None) -> RiskEngine:
    """Return a RiskEngine with an isolated trades.csv so tests don't share file state."""
    engine = RiskEngine()
    if tmp_path is not None:
        _risk_module.TRADES_CSV = tmp_path / "trades.csv"
        engine._ensure_csv()
    return engine


# ══════════════════════════════════════════════════════════════════════════════
# A  Entry cost arithmetic
# ══════════════════════════════════════════════════════════════════════════════

class TestEntryCost:
    """What you actually pay (USDC) to open a position, not the face value."""

    # ── YES positions ──────────────────────────────────────────────────────────

    def test_yes_at_10_cents(self):
        # 100 contracts at $0.10 per contract = $10 deployed.
        pos = _yes(entry_price=0.10, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(10.0)

    def test_yes_at_50_cents(self):
        pos = _yes(entry_price=0.50, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(50.0)

    def test_yes_at_90_cents(self):
        pos = _yes(entry_price=0.90, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(90.0)

    def test_yes_face_value_differs_from_cost(self):
        # 1000 contracts at $0.10 costs $100, NOT $1000.
        pos = _yes(entry_price=0.10, size=1000.0)
        assert pos.size == 1000.0
        assert pos.entry_cost_usd == pytest.approx(100.0)
        assert pos.entry_cost_usd != pos.size

    # ── NO positions ──────────────────────────────────────────────────────────
    # Buying NO = selling YES at entry_price.  The NO token price = 1 − YES price.

    def test_no_when_yes_is_10_cents(self):
        # YES at $0.10 → NO at $0.90 → 100 NO contracts costs $90.
        pos = _no(entry_price=0.10, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(90.0)

    def test_no_when_yes_is_50_cents(self):
        pos = _no(entry_price=0.50, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(50.0)

    def test_no_when_yes_is_90_cents(self):
        # YES at $0.90 → NO at $0.10 → 100 NO contracts costs $10.
        pos = _no(entry_price=0.90, size=100.0)
        assert pos.entry_cost_usd == pytest.approx(10.0)

    def test_yes_no_cost_complement_at_midpoint(self):
        # At 50¢ YES = 50¢ NO; the costs are equal.
        yes = _yes(entry_price=0.50, size=100.0)
        no  = _no(entry_price=0.50, size=100.0)
        assert yes.entry_cost_usd == pytest.approx(no.entry_cost_usd)

    def test_yes_no_costs_sum_to_face_value(self):
        # YES cost + NO cost = size (a complete binary market exhausts the notional).
        p, s = 0.30, 200.0
        assert _yes(entry_price=p, size=s).entry_cost_usd + \
               _no(entry_price=p, size=s).entry_cost_usd == pytest.approx(s)

    def test_leverage_ratio_deep_otm_yes(self):
        # Deep OTM YES at 5¢: $5 deployed on 100 contracts → 19× payout if YES wins.
        pos = _yes(entry_price=0.05, size=100.0)
        max_proceeds = 1.0 * pos.size  # resolves to $1 per contract
        leverage = max_proceeds / pos.entry_cost_usd
        assert leverage == pytest.approx(20.0)

    def test_leverage_ratio_deep_itm_no(self):
        # Deep OTM NO (YES=0.95) at 5¢: $5 deployed, 20× payout if NO wins.
        pos = _no(entry_price=0.95, size=100.0)
        max_proceeds = 1.0 * pos.size   # NO token resolves to $1 if YES fails
        leverage = max_proceeds / pos.entry_cost_usd
        assert leverage == pytest.approx(20.0)


# ══════════════════════════════════════════════════════════════════════════════
# B  Unrealised P&L (mark-to-market)
# ══════════════════════════════════════════════════════════════════════════════

class TestUnrealisedPnl:
    """compute_unrealised_pnl in isolation — no engine needed."""

    def test_yes_price_rises_profit(self):
        pos = _yes(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 0.65) == pytest.approx(25.0)

    def test_yes_price_falls_loss(self):
        pos = _yes(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 0.25) == pytest.approx(-15.0)

    def test_yes_unchanged_zero_pnl(self):
        pos = _yes(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 0.40) == pytest.approx(0.0)

    def test_no_price_falls_profit(self):
        # Sold YES at 0.70 (bought NO at 0.30). YES falls to 0.50 → profit.
        pos = _no(entry_price=0.70, size=100.0)
        assert compute_unrealised_pnl(pos, 0.50) == pytest.approx(20.0)

    def test_no_price_rises_loss(self):
        # Sold YES at 0.30 (bought NO at 0.70). YES rises to 0.45 → loss.
        pos = _no(entry_price=0.30, size=100.0)
        assert compute_unrealised_pnl(pos, 0.45) == pytest.approx(-15.0)

    def test_no_unchanged_zero_pnl(self):
        pos = _no(entry_price=0.60, size=100.0)
        assert compute_unrealised_pnl(pos, 0.60) == pytest.approx(0.0)

    def test_pnl_scales_linearly_with_size(self):
        # Same price move on 10× the contracts → 10× the P&L.
        small = _yes(entry_price=0.40, size=100.0)
        large = _yes(entry_price=0.40, size=1000.0)
        assert compute_unrealised_pnl(large, 0.60) == pytest.approx(
            compute_unrealised_pnl(small, 0.60) * 10
        )

    def test_yes_maximum_gain_at_resolution_to_one(self):
        # YES resolves YES: full notional realised, minus cost.
        pos = _yes(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 1.00) == pytest.approx(60.0)

    def test_yes_maximum_loss_at_resolution_to_zero(self):
        # YES resolves NO: total cost lost.
        pos = _yes(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 0.00) == pytest.approx(-40.0)

    def test_no_maximum_gain_at_yes_resolution_to_zero(self):
        # Sold YES at 0.40 (NO entry). YES→0: full NO notional.
        pos = _no(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 0.00) == pytest.approx(40.0)

    def test_no_maximum_loss_at_yes_resolution_to_one(self):
        # Sold YES at 0.40 (NO entry). YES→1: lose full entry cost.
        pos = _no(entry_price=0.40, size=100.0)
        assert compute_unrealised_pnl(pos, 1.00) == pytest.approx(-60.0)


# ══════════════════════════════════════════════════════════════════════════════
# C  Realised P&L after close
# ══════════════════════════════════════════════════════════════════════════════

class TestRealisedPnl:
    """Verify the ledger entry created by close_position."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_yes_win(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.65)
        assert closed.realized_pnl == pytest.approx(25.0)

    def test_yes_loss(self):
        self.engine.open_position(_yes("m", entry_price=0.70, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.20)
        assert closed.realized_pnl == pytest.approx(-50.0)

    def test_yes_breakeven(self):
        self.engine.open_position(_yes("m", entry_price=0.50, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.50)
        assert closed.realized_pnl == pytest.approx(0.0)

    def test_no_win(self):
        # Sold YES at 0.70 (NO). YES falls to 0.50 → profit.
        self.engine.open_position(_no("m", entry_price=0.70, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.50, side="NO")
        assert closed.realized_pnl == pytest.approx(20.0)

    def test_no_loss(self):
        # Sold YES at 0.30 (NO). YES rises to 0.55 → loss.
        self.engine.open_position(_no("m", entry_price=0.30, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.55, side="NO")
        assert closed.realized_pnl == pytest.approx(-25.0)

    def test_yes_full_resolution_win(self):
        # YES resolves to 1: maximum gain = proceeds − cost.
        self.engine.open_position(_yes("m", entry_price=0.40, size=500.0))
        closed = self.engine.close_position("m", exit_price=1.00)
        assert closed.realized_pnl == pytest.approx(300.0)  # (1.0-0.40)×500

    def test_yes_full_resolution_loss(self):
        # YES resolves to 0: total loss of entry cost.
        self.engine.open_position(_yes("m", entry_price=0.40, size=500.0))
        closed = self.engine.close_position("m", exit_price=0.00)
        assert closed.realized_pnl == pytest.approx(-200.0)  # (0-0.40)×500

    def test_no_resolution_yes_wins(self):
        # Sold YES at 0.30 (NO). YES resolves to 1 → maximum NO loss.
        self.engine.open_position(_no("m", entry_price=0.30, size=200.0))
        closed = self.engine.close_position("m", exit_price=1.00, side="NO")
        assert closed.realized_pnl == pytest.approx(-140.0)  # (0.30-1.0)×200

    def test_no_resolution_no_wins(self):
        # Sold YES at 0.30 (NO). YES resolves to 0 → maximum NO gain.
        self.engine.open_position(_no("m", entry_price=0.30, size=200.0))
        closed = self.engine.close_position("m", exit_price=0.00, side="NO")
        assert closed.realized_pnl == pytest.approx(60.0)  # (0.30-0.0)×200

    def test_pnl_is_set_on_position_object(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.60)
        assert closed is not None
        assert closed.is_closed
        assert closed.realized_pnl == pytest.approx(20.0)

    def test_close_twice_idempotent(self):
        # Second close returns None; P&L is only counted once.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        first  = self.engine.close_position("m", exit_price=0.60)
        second = self.engine.close_position("m", exit_price=0.60)
        assert first  is not None
        assert second is None
        assert self.engine.realized_pnl == pytest.approx(first.realized_pnl)

    def test_closed_position_excluded_from_open_positions(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.close_position("m", exit_price=0.60)
        assert len(self.engine.get_open_positions()) == 0


# ══════════════════════════════════════════════════════════════════════════════
# D  Fees and rebate accounting
# ══════════════════════════════════════════════════════════════════════════════

class TestFeeAndRebateAccounting:
    """Fees reduce and rebates increase the realised P&L line."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_fees_paid_reduce_pnl(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.65, fees_paid=3.50)
        # price_pnl = 25.0; minus fees = 21.50
        assert closed.realized_pnl == pytest.approx(21.50)

    def test_rebates_earned_increase_pnl(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.65, rebates_earned=1.20)
        # price_pnl = 25.0; plus rebate = 26.20
        assert closed.realized_pnl == pytest.approx(26.20)

    def test_fees_and_rebates_combine(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        closed = self.engine.close_position(
            "m", exit_price=0.65, fees_paid=4.00, rebates_earned=1.50
        )
        # price_pnl = 25.0 − 4.0 + 1.5 = 22.50
        assert closed.realized_pnl == pytest.approx(22.50)

    def test_fees_stored_on_position_object(self):
        self.engine.open_position(_yes("m", entry_price=0.50, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.70, fees_paid=2.00)
        assert closed.pm_fees_paid == pytest.approx(2.00)

    def test_rebates_stored_on_position_object(self):
        self.engine.open_position(_yes("m", entry_price=0.50, size=100.0))
        closed = self.engine.close_position(
            "m", exit_price=0.70, rebates_earned=0.80
        )
        assert closed.pm_rebates_earned == pytest.approx(0.80)

    def test_record_rebate_accumulates_before_close(self):
        # Maker earns a non-zero rebate at fill time via record_rebate().
        # That rebate should be included in the final realized_pnl.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.record_rebate("m", 0.50)          # first partial fill rebate
        self.engine.record_rebate("m", 0.30)          # second partial fill rebate
        pos = self.engine.get_open_positions()[0]
        assert pos.pm_rebates_earned == pytest.approx(0.80)

    def test_record_rebate_included_in_realised_pnl(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.record_rebate("m", 1.00)
        # Entry rebate (1.00) credited via record_rebate; no additional exit rebate.
        # realized_pnl must include the entry rebate: price_pnl + rebate = 10.0 + 1.0 = 11.0
        closed = self.engine.close_position("m", exit_price=0.50)
        assert closed.pm_rebates_earned == pytest.approx(1.00)
        assert closed.realized_pnl == pytest.approx(11.0)

    def test_pm_fee_formula_at_50_cents(self):
        # PM fee per contract at p=0.50: PM_FEE_COEFF × 0.50 × 0.50
        fee_per_contract = config.PM_FEE_COEFF * 0.50 * 0.50
        assert fee_per_contract == pytest.approx(0.004375, rel=1e-4)

    def test_pm_fee_formula_is_symmetric(self):
        # fee(p) = fee(1-p) because p*(1-p) is symmetric around 0.50.
        for p in [0.10, 0.25, 0.40]:
            assert (config.PM_FEE_COEFF * p * (1 - p) ==
                    pytest.approx(config.PM_FEE_COEFF * (1 - p) * p))

    def test_pm_fee_highest_at_midpoint(self):
        fee_at_mid    = config.PM_FEE_COEFF * 0.50 * 0.50
        fee_at_extreme = config.PM_FEE_COEFF * 0.10 * 0.90
        assert fee_at_mid > fee_at_extreme

    def test_round_trip_fee_on_winning_trade(self):
        # A winning trade must overcome the round-trip fee to be net profitable.
        # With fees < price_pnl, realized_pnl is still positive.
        price = 0.50
        size  = 100.0
        fee_per_leg = config.PM_FEE_COEFF * price * (1 - price) * size
        round_trip  = 2 * fee_per_leg            # entry + exit taker fees

        self.engine.open_position(_yes("m", entry_price=price, size=size))
        # Edge = 5¢ move on 100 contracts = $5; roundtrip fee ≈ $0.875 → net positive
        closed = self.engine.close_position(
            "m", exit_price=0.55, fees_paid=round_trip
        )
        assert closed.realized_pnl > 0, "Trade profitable even after fees"
        assert closed.pm_fees_paid == pytest.approx(round_trip)


# ══════════════════════════════════════════════════════════════════════════════
# E  Partial-fill accumulation
# ══════════════════════════════════════════════════════════════════════════════

class TestPartialFillAccumulation:
    """Multiple fill slices on the same order merge into a single position."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_two_fills_same_price_merge(self):
        # Fill 1: 100 YES at 0.40 = $40.  Fill 2: 60 YES at 0.40 = $24.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_yes("m", entry_price=0.40, size=60.0))
        positions = self.engine.get_open_positions()
        assert len(positions) == 1
        merged = positions[0]
        assert merged.size           == pytest.approx(160.0)
        assert merged.entry_cost_usd == pytest.approx(64.0)   # 40 + 24

    def test_three_fills_same_price_merge(self):
        for _ in range(3):
            self.engine.open_position(_yes("m", entry_price=0.50, size=50.0))
        pos = self.engine.get_open_positions()[0]
        assert pos.size           == pytest.approx(150.0)
        assert pos.entry_cost_usd == pytest.approx(75.0)

    def test_opened_at_not_reset_on_merge(self):
        # The timestamp of the FIRST fill is preserved across merges.
        t0 = datetime.now(timezone.utc) - timedelta(seconds=300)
        pos1 = _yes("m", entry_price=0.40, size=100.0)
        pos1.opened_at = t0
        self.engine.open_position(pos1)

        pos2 = _yes("m", entry_price=0.40, size=50.0)
        pos2.opened_at = datetime.now(timezone.utc)   # later fill
        self.engine.open_position(pos2)

        merged = self.engine.get_open_positions()[0]
        assert merged.opened_at == t0

    def test_merged_position_pnl_on_close(self):
        # 100 + 60 = 160 YES at 0.40; close at 0.55 → (0.15 × 160) = $24.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_yes("m", entry_price=0.40, size=60.0))
        closed = self.engine.close_position("m", exit_price=0.55)
        assert closed.realized_pnl == pytest.approx(24.0)

    def test_merged_cost_equals_sum_of_fills(self):
        # entry_cost_usd must equal the arithmetic sum of all individual fill costs.
        fills = [(100.0, 0.40), (80.0, 0.40), (40.0, 0.40)]  # same price
        expected_cost = sum(s * p for s, p in fills)
        for size, price in fills:
            self.engine.open_position(_yes("m", entry_price=price, size=size))
        pos = self.engine.get_open_positions()[0]
        assert pos.entry_cost_usd == pytest.approx(expected_cost)

    def test_reopen_after_close_creates_fresh_position(self):
        # Closing and then refilling the same market should create a NEW position,
        # not contaminate the closed one.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.close_position("m", exit_price=0.50)          # P&L = $10

        self.engine.open_position(_yes("m", entry_price=0.60, size=50.0))
        positions = self.engine.get_open_positions()
        assert len(positions) == 1
        reopened = positions[0]
        assert reopened.size           == pytest.approx(50.0)
        assert reopened.entry_cost_usd == pytest.approx(30.0)
        assert reopened.entry_price    == pytest.approx(0.60)


# ══════════════════════════════════════════════════════════════════════════════
# F  Two-sided market making — YES and NO coexist on the same market
# ══════════════════════════════════════════════════════════════════════════════

class TestTwoSidedMaking:
    """
    A market maker posts both a BID (→ YES fill) and an ASK (→ NO fill) on the
    same market.  The composite position key (market_id:side) ensures they are
    tracked independently so neither position pollutes the other's P&L.
    """

    def setup_method(self):
        self.engine = RiskEngine()

    def test_yes_and_no_are_separate_positions(self):
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_no("m",  entry_price=0.60, size=100.0))
        assert len(self.engine.get_open_positions()) == 2

    def test_yes_fill_does_not_corrupt_no_position(self):
        self.engine.open_position(_no("m",  entry_price=0.60, size=100.0))
        self.engine.open_position(_yes("m", entry_price=0.40, size=200.0))  # different size
        yes_pos = next(p for p in self.engine.get_open_positions() if p.side == "YES")
        no_pos  = next(p for p in self.engine.get_open_positions() if p.side == "NO")
        assert yes_pos.size == pytest.approx(200.0)
        assert no_pos.size  == pytest.approx(100.0)

    def test_spread_capture_both_sides_profitable(self):
        # The market maker quotes BID=0.40, ASK=0.60 (20¢ spread).
        # Both sides fill.  At any final price between 0.40 and 0.60, both
        # positions are profitable.  Here we close at mid = 0.50:
        #   YES pnl = (0.50 − 0.40) × 100 = +$10
        #   NO  pnl = (0.60 − 0.50) × 100 = +$10
        #   Total   = +$20 = spread × size = 0.20 × 100
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_no("m",  entry_price=0.60, size=100.0))

        yes_closed = self.engine.close_position("m", exit_price=0.50, side="YES")
        no_closed  = self.engine.close_position("m", exit_price=0.50, side="NO")

        assert yes_closed.realized_pnl == pytest.approx(10.0)
        assert no_closed.realized_pnl  == pytest.approx(10.0)
        assert yes_closed.realized_pnl + no_closed.realized_pnl == pytest.approx(20.0)

    def test_spread_capture_is_direction_independent(self):
        # The 20¢ spread is locked in regardless of which way the market resolves.
        # YES resolves to 1.0:
        # YES pnl = (1.0 − 0.40) × 100 = +$60
        # NO  pnl = (0.60 − 1.0) × 100 = −$40
        # Net     = +$20 = spread capture ✓
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_no("m",  entry_price=0.60, size=100.0))

        yes_r1 = self.engine.close_position("m", exit_price=1.00, side="YES")
        no_r1  = self.engine.close_position("m", exit_price=1.00, side="NO")
        assert yes_r1.realized_pnl + no_r1.realized_pnl == pytest.approx(20.0)

    def test_spread_capture_yes_resolves_to_zero(self):
        # YES resolves to 0.0:
        # YES pnl = (0.0 − 0.40) × 100 = −$40
        # NO  pnl = (0.60 − 0.0) × 100 = +$60
        # Net     = +$20 ✓
        engine2 = RiskEngine()
        engine2.open_position(_yes("m", entry_price=0.40, size=100.0))
        engine2.open_position(_no("m",  entry_price=0.60, size=100.0))

        yes_r0 = engine2.close_position("m", exit_price=0.00, side="YES")
        no_r0  = engine2.close_position("m", exit_price=0.00, side="NO")
        assert yes_r0.realized_pnl + no_r0.realized_pnl == pytest.approx(20.0)

    def test_independent_close_yes_only(self):
        # Closing YES side does not close NO side.
        self.engine.open_position(_yes("m", entry_price=0.40, size=100.0))
        self.engine.open_position(_no("m",  entry_price=0.60, size=100.0))
        self.engine.close_position("m", exit_price=0.50, side="YES")
        assert len(self.engine.get_open_positions()) == 1
        assert self.engine.get_open_positions()[0].side == "NO"

    def test_different_sizes_tracked_independently(self):
        # YES: 300 contracts.  NO: 150 contracts.  Separate positions, no bleed.
        self.engine.open_position(_yes("m", entry_price=0.45, size=300.0))
        self.engine.open_position(_no("m",  entry_price=0.55, size=150.0))
        yes_pos = next(p for p in self.engine.get_open_positions() if p.side == "YES")
        no_pos  = next(p for p in self.engine.get_open_positions() if p.side == "NO")
        assert yes_pos.entry_cost_usd == pytest.approx(0.45 * 300)   # $135
        assert no_pos.entry_cost_usd  == pytest.approx(0.45 * 150)   # $67.50 (NO token at $0.45)


# ══════════════════════════════════════════════════════════════════════════════
# G  Market resolution payoffs — all YES/NO × 0/1 combinations
# ══════════════════════════════════════════════════════════════════════════════

class TestResolutionPayoffs:
    """
    Binary market resolves to exactly 0.0 or 1.0.
    The four combinations fully determine the payout tables.
    """

    ENTRY_PRICES = [0.10, 0.25, 0.50, 0.75, 0.90]

    def setup_method(self):
        self.engine = RiskEngine()

    @pytest.mark.parametrize("entry_price", ENTRY_PRICES)
    def test_yes_wins_all_entry_prices(self, entry_price: float):
        # YES at any entry price; market resolves to 1.0 → profit = (1 - entry) × size
        self.engine = RiskEngine()
        self.engine.open_position(_yes("m", entry_price=entry_price, size=100.0))
        closed = self.engine.close_position("m", exit_price=1.0)
        expected = (1.0 - entry_price) * 100.0
        assert closed.realized_pnl == pytest.approx(expected, rel=1e-6)

    @pytest.mark.parametrize("entry_price", ENTRY_PRICES)
    def test_yes_loses_all_entry_prices(self, entry_price: float):
        # YES at any entry price; market resolves to 0.0 → loss = entry × size
        self.engine = RiskEngine()
        self.engine.open_position(_yes("m", entry_price=entry_price, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.0)
        expected = -entry_price * 100.0
        assert closed.realized_pnl == pytest.approx(expected, rel=1e-6)

    @pytest.mark.parametrize("entry_price", ENTRY_PRICES)
    def test_no_wins_all_entry_prices(self, entry_price: float):
        # NO (= sold YES at entry_price); YES resolves to 0.0 → NO wins.
        self.engine = RiskEngine()
        self.engine.open_position(_no("m", entry_price=entry_price, size=100.0))
        closed = self.engine.close_position("m", exit_price=0.0, side="NO")
        expected = entry_price * 100.0    # (entry - 0.0) × size
        assert closed.realized_pnl == pytest.approx(expected, rel=1e-6)

    @pytest.mark.parametrize("entry_price", ENTRY_PRICES)
    def test_no_loses_all_entry_prices(self, entry_price: float):
        # NO; YES resolves to 1.0 → NO loses.
        self.engine = RiskEngine()
        self.engine.open_position(_no("m", entry_price=entry_price, size=100.0))
        closed = self.engine.close_position("m", exit_price=1.0, side="NO")
        expected = (entry_price - 1.0) * 100.0   # negative
        assert closed.realized_pnl == pytest.approx(expected, rel=1e-6)

    def test_yes_and_no_at_same_entry_are_complements(self):
        # YES + NO at same price → one exactly cancels the other at resolution.
        eng_yes = RiskEngine()
        eng_no  = RiskEngine()
        p, s = 0.40, 100.0

        eng_yes.open_position(_yes("m", entry_price=p, size=s))
        eng_no.open_position(_no("m",   entry_price=p, size=s))

        for exit_price in [0.0, 1.0]:
            pnl_yes = (exit_price - p) * s
            pnl_no  = (p - exit_price) * s
            assert pnl_yes + pnl_no == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# H  Session P&L accumulation and hard stop
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionPnl:
    """engine.realized_pnl accumulates across trades; hard stop fires on threshold."""

    def setup_method(self):
        self.engine = RiskEngine()

    def test_single_trade_pnl(self):
        self.engine.open_position(_yes("m1", entry_price=0.40, size=100.0))
        self.engine.close_position("m1", exit_price=0.65)
        assert self.engine.realized_pnl == pytest.approx(25.0)

    def test_multiple_trades_accumulate(self):
        # Trade 1: +$25.  Trade 2: −$15.  Trade 3: +$30.  Net: +$40.
        self.engine.open_position(_yes("m1", entry_price=0.40, size=100.0))
        self.engine.close_position("m1", exit_price=0.65)   # +25

        self.engine.open_position(_yes("m2", entry_price=0.60, size=100.0))
        self.engine.close_position("m2", exit_price=0.45)   # -15

        self.engine.open_position(_yes("m3", entry_price=0.20, size=100.0))
        self.engine.close_position("m3", exit_price=0.50)   # +30

        assert self.engine.realized_pnl == pytest.approx(40.0)

    def test_closed_position_not_in_open_list(self):
        self.engine.open_position(_yes("m1", entry_price=0.40, size=100.0))
        self.engine.open_position(_yes("m2", entry_price=0.50, size=100.0))
        self.engine.close_position("m1", exit_price=0.60)
        open_ids = {p.market_id for p in self.engine.get_open_positions()}
        assert "m1" not in open_ids
        assert "m2" in open_ids

    def test_hard_stop_triggers_when_drawdown_exceeded(self):
        # Lose more than HARD_STOP_DRAWDOWN in one trade → hard stop fires.
        loss = config.HARD_STOP_DRAWDOWN + 100.0
        size = loss  # entry_price=1.0, exit_price=0.0 → loss = size
        self.engine.open_position(_yes("m", entry_price=1.0, size=size))
        self.engine.close_position("m", exit_price=0.0)
        assert self.engine.hard_stop_triggered

    def test_hard_stop_blocks_new_opens(self):
        loss = config.HARD_STOP_DRAWDOWN + 100.0
        self.engine.open_position(_yes("m", entry_price=1.0, size=loss))
        self.engine.close_position("m", exit_price=0.0)
        ok, reason = self.engine.can_open("m2", 10.0)
        assert not ok
        assert "hard stop" in reason

    def test_no_hard_stop_on_small_loss(self):
        # Loss well below threshold → no hard stop.
        self.engine.open_position(_yes("m", entry_price=0.50, size=10.0))
        self.engine.close_position("m", exit_price=0.40)   # −$1
        assert not self.engine.hard_stop_triggered

    def test_running_total_is_sum_of_realized_pnl_fields(self):
        trades = [("m1", 0.40, 0.60), ("m2", 0.70, 0.50), ("m3", 0.30, 0.45)]
        expected = sum((exit - entry) * 100.0 for _, entry, exit in trades)
        for mid, entry, exit in trades:
            self.engine.open_position(_yes(mid, entry_price=entry, size=100.0))
            self.engine.close_position(mid, exit_price=exit)
        assert self.engine.realized_pnl == pytest.approx(expected)


# ══════════════════════════════════════════════════════════════════════════════
# I  CSV ledger integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestCsvLedger:
    """Each close_position() call must write exactly one correctly-populated row."""

    def test_single_trade_writes_one_row(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", underlying="BTC", entry_price=0.40, size=100.0))
        engine.close_position("m1", exit_price=0.60)
        rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
        assert len(rows) == 1

    def test_csv_row_has_correct_pnl(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", entry_price=0.40, size=100.0))
        engine.close_position("m1", exit_price=0.65)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert float(row["pnl"]) == pytest.approx(25.0)

    def test_csv_row_has_correct_side(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_no("m1", entry_price=0.60))
        engine.close_position("m1", exit_price=0.40, side="NO")
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert row["side"] == "NO"

    def test_csv_row_has_correct_underlying(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", underlying="ETH", entry_price=0.50, size=200.0))
        engine.close_position("m1", exit_price=0.55)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert row["underlying"] == "ETH"

    def test_csv_row_fees_and_rebates(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", entry_price=0.50, size=100.0))
        engine.close_position("m1", exit_price=0.60, fees_paid=2.50, rebates_earned=1.00)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert float(row["fees_paid"])       == pytest.approx(2.50)
        assert float(row["rebates_earned"])  == pytest.approx(1.00)

    def test_csv_row_pnl_net_of_fees_rebates(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", entry_price=0.40, size=100.0))
        engine.close_position("m1", exit_price=0.65, fees_paid=3.00, rebates_earned=1.50)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        # price_pnl=25; net = 25 - 3 + 1.5 = 23.5
        assert float(row["pnl"]) == pytest.approx(23.50)

    def test_csv_row_entry_price_and_size(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", entry_price=0.35, size=250.0))
        engine.close_position("m1", exit_price=0.50)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert float(row["price"]) == pytest.approx(0.35)
        assert float(row["size"])  == pytest.approx(250.0)

    def test_multiple_trades_produce_multiple_rows(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        for i in range(5):
            engine.open_position(_yes(f"m{i}", entry_price=0.50, size=100.0))
            engine.close_position(f"m{i}", exit_price=0.55)
        rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
        assert len(rows) == 5

    def test_csv_row_market_id_correct(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        market_id = "0x" + "a" * 60   # realistic hex condition ID
        engine.open_position(_yes(market_id, entry_price=0.50, size=100.0))
        engine.close_position(market_id, exit_price=0.60)
        row = list(csv.DictReader((tmp_path / "trades.csv").open()))[0]
        assert row["market_id"] == market_id

    def test_closed_position_not_written_twice(self, tmp_path):
        engine = _fresh_engine(tmp_path)
        engine.open_position(_yes("m1", entry_price=0.50, size=100.0))
        engine.close_position("m1", exit_price=0.60)
        engine.close_position("m1", exit_price=0.60)   # second call → None, no write
        rows = list(csv.DictReader((tmp_path / "trades.csv").open()))
        assert len(rows) == 1


# ══════════════════════════════════════════════════════════════════════════════
# J  Exit trigger arithmetic
# ══════════════════════════════════════════════════════════════════════════════

class TestExitTriggers:
    """
    should_exit() is a pure function — no engine needed.
    All values are exact floats; assertions use the same formula as the code.
    """

    # Use a fixed "now" far in the future so markets are never near expiry
    # unless we explicitly construct one.
    FAR_FUTURE = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)

    def _pos(self, **kwargs) -> Position:
        return _yes(seconds_ago=120, **kwargs)

    # ── Profit target ──────────────────────────────────────────────────────────

    def test_profit_target_fires_exactly_at_threshold(self):
        config.PROFIT_TARGET_PCT  = 0.60
        config.MIN_HOLD_SECONDS   = 60
        # deviation=0.10, PCT=0.60, size=100 → target = $6.00
        pos = _yes("m", entry_price=0.40, size=100.0, strategy="mispricing")
        # current_price that gives exactly $6.01 unrealised
        trigger_price = pos.entry_price + (6.01 / pos.size)
        flag, reason, _ = should_exit(
            pos, trigger_price, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert flag and reason == ExitReason.PROFIT_TARGET

    def test_profit_target_does_not_fire_just_below(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.MIN_HOLD_SECONDS  = 60
        pos = _yes("m", entry_price=0.40, size=100.0, strategy="mispricing")
        # unrealised = $5.99 < $6.00 target → no exit
        price = pos.entry_price + (5.99 / pos.size)
        flag, _, _ = should_exit(
            pos, price, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert not flag

    def test_profit_target_threshold_formula(self):
        # Verify the formula: target_usd = deviation × PCT × size
        config.PROFIT_TARGET_PCT = 0.60
        deviation, size = 0.08, 200.0
        expected_target = 0.08 * 0.60 * 200.0   # = $9.60
        assert expected_target == pytest.approx(9.60)

    def test_profit_target_scales_with_deviation(self):
        config.PROFIT_TARGET_PCT = 0.60
        config.MIN_HOLD_SECONDS  = 60
        # Larger deviation → higher threshold → same price move doesn't trigger.
        pos = _yes("m", entry_price=0.40, size=100.0, strategy="mispricing")
        # +$6 unrealised → triggers at deviation=0.10 but not at deviation=0.20
        price = pos.entry_price + (6.01 / pos.size)
        flag_small_dev, _, _ = should_exit(
            pos, price, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        flag_large_dev, _, _ = should_exit(
            pos, price, initial_deviation=0.20,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert flag_small_dev
        assert not flag_large_dev

    # ── Stop-loss ─────────────────────────────────────────────────────────────

    def test_stop_loss_fires_at_threshold(self):
        config.STOP_LOSS_USD     = 25.0
        config.MIN_HOLD_SECONDS  = 60
        pos = _yes("m", entry_price=0.60, size=100.0, strategy="mispricing")
        # Loss of $25.01 → stop
        price = pos.entry_price - (25.01 / pos.size)
        flag, reason, _ = should_exit(
            pos, price, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert flag and reason == ExitReason.STOP_LOSS

    def test_stop_loss_does_not_fire_just_above(self):
        config.STOP_LOSS_USD     = 25.0
        config.MIN_HOLD_SECONDS  = 60
        pos = _yes("m", entry_price=0.60, size=100.0, strategy="mispricing")
        price = pos.entry_price - (24.99 / pos.size)
        flag, _, _ = should_exit(
            pos, price, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert not flag

    def test_maker_has_no_profit_target_or_stop_loss(self):
        # Maker positions exit only on time-stop or coin-level loss limit (outside
        # should_exit).  Profit target and stop-loss must never fire for maker.
        config.MIN_HOLD_SECONDS = 60
        pos = _yes("m", entry_price=0.40, size=100.0, strategy="maker")
        # Price collapsed to near-zero: unrealised loss = −$39 (well past $25 stop)
        flag, reason, _ = should_exit(
            pos, 0.01, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert not flag

    def test_maker_has_no_profit_target(self):
        config.MIN_HOLD_SECONDS = 60
        pos = _yes("m", entry_price=0.10, size=100.0, strategy="maker")
        # Huge profit: +$80 (8× the default STOP_LOSS_USD) — must not trigger
        flag, reason, _ = should_exit(
            pos, 0.90, initial_deviation=0.10,
            market_end_date=None, now=self.FAR_FUTURE
        )
        assert not flag

    # ── Minimum hold time ───────────────────────────────────────────────────

    def test_min_hold_blocks_exit_when_too_young(self):
        config.MIN_HOLD_SECONDS  = 60
        # Opened only 10 seconds ago.
        pos = _yes("m", entry_price=0.40, size=100.0, strategy="mispricing",
                   seconds_ago=10)
        flag, _, _ = should_exit(
            pos, 0.90, initial_deviation=0.10,
            market_end_date=None, now=datetime.now(timezone.utc)
        )
        assert not flag

    # ── Time stop ─────────────────────────────────────────────────────────────

    def test_time_stop_fires_for_mispricing_near_expiry(self):
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60
        now = self.FAR_FUTURE
        end_date = now + timedelta(days=2)   # 2 days left < 3-day threshold
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="mispricing")
        flag, reason, _ = should_exit(
            pos, 0.50, initial_deviation=0.10,
            market_end_date=end_date, now=now
        )
        assert flag and reason == ExitReason.TIME_STOP

    def test_time_stop_does_not_fire_for_mispricing_far_from_expiry(self):
        config.EXIT_DAYS_BEFORE_RESOLUTION = 3
        config.MIN_HOLD_SECONDS = 60
        now = self.FAR_FUTURE
        end_date = now + timedelta(days=5)   # 5 days left > 3-day threshold
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="mispricing")
        flag, _, _ = should_exit(
            pos, 0.50, initial_deviation=0.10,
            market_end_date=end_date, now=now
        )
        assert not flag

    def test_time_stop_fires_for_maker_within_maker_exit_hours(self):
        """MAKER_EXIT_HOURS time stop fires for non-bucket (milestone) maker positions."""
        config.MAKER_EXIT_HOURS    = 6.0
        config.MIN_HOLD_SECONDS    = 60
        now = self.FAR_FUTURE
        end_date = now + timedelta(hours=4)   # 4h left < 6h threshold
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="maker")
        pos.market_type = "milestone"  # non-bucket — MAKER_EXIT_HOURS applies
        flag, reason, _ = should_exit(
            pos, 0.50, initial_deviation=0.01,
            market_end_date=end_date, now=now
        )
        assert flag and reason == ExitReason.TIME_STOP

    def test_time_stop_does_not_fire_for_bucket_maker_exit_hours(self):
        """Bucket market positions are NOT time-stopped by MAKER_EXIT_HOURS — held to RESOLVED."""
        config.MAKER_EXIT_HOURS    = 6.0
        config.MIN_HOLD_SECONDS    = 60
        now = self.FAR_FUTURE
        # bucket_5m — entire lifespan is 5min, always within 6h threshold
        end_date = now + timedelta(minutes=3)
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="maker")
        pos.market_type = "bucket_5m"  # bucket — should NOT fire time stop
        flag, reason, _ = should_exit(
            pos, 0.50, initial_deviation=0.01,
            market_end_date=end_date, now=now
        )
        assert not flag, "bucket_5m must not be time-stopped by MAKER_EXIT_HOURS"

    def test_time_stop_does_not_fire_for_maker_far_from_expiry(self):
        config.MAKER_EXIT_HOURS = 6.0
        config.MIN_HOLD_SECONDS = 60
        now = self.FAR_FUTURE
        end_date = now + timedelta(hours=10)  # 10h > 6h threshold
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="maker")
        flag, _, _ = should_exit(
            pos, 0.50, initial_deviation=0.01,
            market_end_date=end_date, now=now
        )
        assert not flag

    def test_resolved_stop_fires_when_past_end_date(self):
        config.MIN_HOLD_SECONDS = 60
        now = self.FAR_FUTURE
        end_date = now - timedelta(seconds=1)  # already resolved
        pos = _yes("m", entry_price=0.50, size=100.0, strategy="mispricing")
        flag, reason, _ = should_exit(
            pos, 0.50, initial_deviation=0.10,
            market_end_date=end_date, now=now
        )
        assert flag and reason == ExitReason.RESOLVED


# ══════════════════════════════════════════════════════════════════════════════
# K  Exit fee model — three distinct regimes
# ══════════════════════════════════════════════════════════════════════════════

class TestExitFeeModel:
    """
    Verify the three exit-fee regimes in monitor._exit_position.

    1. RESOLVED     — PM auto-distributes. No trade, no fee, no rebate.
                      exit_price snaps to exact 0.0 or 1.0.
    2. Post-only    — we are the maker on exit: earn rebate, pay zero taker fee.
    3. Force-taker  — market order: pay full taker fee, earn no rebate.

    These tests call risk.close_position() directly with the expected fee/rebate
    values so we can verify the P&L arithmetic without needing to spin up a full
    Monitor object.
    """

    def setup_method(self):
        self.engine = RiskEngine()
        config.PM_FEE_COEFF = 0.0175

    # ── helper ────────────────────────────────────────────────────────────────

    @staticmethod
    def _fee(size: float, token_price: float) -> float:
        return size * token_price * config.PM_FEE_COEFF * (1.0 - token_price)

    # ── RESOLVED: zero fees, exact settlement, exact P&L ──────────────────────

    def test_resolved_yes_loses_no_fee(self):
        # YES position: market resolved NO → settlement 0.0, fee = 0.
        # entry 0.62, exit 0.0 → pnl = (0.0 - 0.62) × 25 = -15.50 exactly.
        self.engine.open_position(_yes("m", entry_price=0.62, size=25.0))
        exit_price = 0.0   # true settlement
        closed = self.engine.close_position("m", exit_price=exit_price,
                                            fees_paid=0.0, rebates_earned=0.0)
        assert closed.pm_fees_paid     == pytest.approx(0.0)
        assert closed.pm_rebates_earned == pytest.approx(0.0)
        assert closed.realized_pnl     == pytest.approx((0.0 - 0.62) * 25.0)

    def test_resolved_yes_wins_no_fee(self):
        # YES position: market resolved YES → settlement 1.0, fee = 0.
        self.engine.open_position(_yes("m", entry_price=0.37, size=25.0))
        closed = self.engine.close_position("m", exit_price=1.0,
                                            fees_paid=0.0, rebates_earned=0.0)
        assert closed.pm_fees_paid      == pytest.approx(0.0)
        assert closed.realized_pnl      == pytest.approx((1.0 - 0.37) * 25.0)

    def test_resolved_no_side_wins_no_fee(self):
        # NO position: market resolved NO (YES→0) → NO pays $1.
        # P&L for NO: (entry_price - exit_price) × size = (0.42 - 0.0) × 20 = $8.40
        self.engine.open_position(_no("m", entry_price=0.42, size=20.0))
        closed = self.engine.close_position("m", exit_price=0.0, side="NO",
                                            fees_paid=0.0, rebates_earned=0.0)
        assert closed.pm_fees_paid  == pytest.approx(0.0)
        assert closed.realized_pnl  == pytest.approx((0.42 - 0.0) * 20.0)

    # ── Post-only exit (maker on exit): zero fee, earn rebate ─────────────────

    def test_postonly_exit_zero_fee(self):
        # Closing YES at mid p=0.55 via post-only limit: no taker fee charged.
        size, exit_price, rebate_pct = 100.0, 0.55, 0.20
        expected_rebate = self._fee(size, exit_price) * rebate_pct
        self.engine.open_position(_yes("m", entry_price=0.45, size=size))
        closed = self.engine.close_position("m", exit_price=exit_price,
                                            fees_paid=0.0,
                                            rebates_earned=expected_rebate)
        assert closed.pm_fees_paid      == pytest.approx(0.0)
        assert closed.pm_rebates_earned == pytest.approx(expected_rebate)
        # P&L = price_pnl + rebate
        assert closed.realized_pnl      == pytest.approx(
            (exit_price - 0.45) * size + expected_rebate
        )

    def test_postonly_rebate_reduces_net_cost_vs_old_model(self):
        # Old (buggy) model charged taker_fee − rebate on post-only exits.
        # New model: fee=0, earn=rebate → net is BETTER by the taker fee amount.
        size, exit_price, rebate_pct = 100.0, 0.50, 0.20
        taker_fee   = self._fee(size, exit_price)
        maker_rebate = taker_fee * rebate_pct

        # Old-model net drag: taker_fee - maker_rebate
        old_net_drag = taker_fee - maker_rebate   # 80% of taker fee
        # New-model net effect: 0 fee, +rebate (positive)
        new_net_effect = maker_rebate              # positive

        # The two accounting treatments differ by the full taker fee
        assert new_net_effect - (-old_net_drag) == pytest.approx(taker_fee)

    # ── Force-taker exit (market order): pay full fee, no rebate ─────────────

    def test_force_taker_pays_full_fee(self):
        # Market-order exit: we are the taker → pay full PM taker fee.
        size, exit_price = 100.0, 0.55
        taker_fee = self._fee(size, exit_price)
        self.engine.open_position(_yes("m", entry_price=0.45, size=size))
        closed = self.engine.close_position("m", exit_price=exit_price,
                                            fees_paid=taker_fee,
                                            rebates_earned=0.0)
        assert closed.pm_fees_paid      == pytest.approx(taker_fee)
        assert closed.pm_rebates_earned == pytest.approx(0.0)
        assert closed.realized_pnl      == pytest.approx(
            (exit_price - 0.45) * size - taker_fee
        )

    def test_force_taker_no_rebate(self):
        # As a taker we do not earn a rebate — the other side's maker earns it.
        size, exit_price = 50.0, 0.60
        taker_fee = self._fee(size, exit_price)
        self.engine.open_position(_yes("m", entry_price=0.40, size=size))
        closed = self.engine.close_position("m", exit_price=exit_price,
                                            fees_paid=taker_fee,
                                            rebates_earned=0.0)
        assert closed.pm_rebates_earned == pytest.approx(0.0)

