"""
tests/test_accounting_e2e.py — End-to-end tests for accounting.py.

Written from a trader / accountant perspective:
  *Given these fills at these prices, what exact state and dollar amounts
  appear in the ledger?*

Every assertion is independently verifiable by hand calculation.
No implementation details are mirrored — tests assert BUSINESS OUTCOMES.

Sections
────────
  A  Entry fill mechanics            — single and multi-fill VWAP accumulation
  B  Exit fill mechanics             — VWAP, status transitions, exit_type
  C  Gross P&L arithmetic            — (exit_vwap - entry_vwap) × exit_contracts
  D  Net P&L = gross − fees + rebates
  E  YES / NO / UP / DOWN resolution — preamble rules for resolved_yes_price
  F  Market resolution state machine — on_resolved() outcome mapping
  G  Ledger CSV integrity            — on_resolved() writes a correct CSV row
  H  Two-sided (maker) pair tracking — YES + NO on same market, independent
  I  Pair / hedge relationships       — pair_id, parent_pos_id, on_pair_promoted
  J  Position query helpers          — get_position_by_token, get_positions_for_pair
  K  add_fees accumulation           — post-fill fee/rebate adjustments
  L  Token-space independence        — YES and NO have separate books (preamble)

Key formulas
────────────
  entry_cost_usd  = sum(fill_price × contracts) over all entry fills
  entry_vwap      = VWAP of all entry fills
  exit_vwap       = VWAP of all exit fills
  gross_pnl       = (exit_vwap − entry_vwap) × exit_contracts
  net_pnl         = gross_pnl − fees_usd + rebates_usd

Preamble rules enforced
───────────────────────
  resolved_yes_price = 1.0  → YES/UP token WON  (NO/DOWN token LOST)
  resolved_yes_price = 0.0  → NO/DOWN token WON  (YES/UP token LOST)
  YES and NO have SEPARATE order books — never derive one from the other.
"""
from __future__ import annotations

import csv
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
config.PAPER_TRADING = True   # no real network calls

import accounting as _acct_mod
from accounting import (
    AccountingPosition,
    PositionStatus,
    _Ledger,
    _vwap,
    _gross_pnl,
    LEDGER_HEADER,
)


# ── Isolation fixture ──────────────────────────────────────────────────────────
# Each test gets a fresh _Ledger whose files live in a private tmp directory.
# The module-level singleton is patched so any lazy get_ledger() calls within
# accounting.py itself also see the isolated instance.

@pytest.fixture
def ledger(tmp_path: Path) -> _Ledger:
    """Fresh _Ledger backed by per-test tmp files."""
    fills_path    = tmp_path / "acct_fills.jsonl"
    positions_path = tmp_path / "acct_positions.json"
    ledger_path   = tmp_path / "acct_ledger.csv"

    orig_fills    = _acct_mod.FILLS_JSONL
    orig_pos      = _acct_mod.POSITIONS_JSON
    orig_ledger   = _acct_mod.LEDGER_CSV
    orig_singleton = _acct_mod._ledger

    _acct_mod.FILLS_JSONL    = fills_path
    _acct_mod.POSITIONS_JSON = positions_path
    _acct_mod.LEDGER_CSV     = ledger_path

    inst = _Ledger()
    _acct_mod._ledger = inst

    yield inst

    _acct_mod.FILLS_JSONL    = orig_fills
    _acct_mod.POSITIONS_JSON = orig_pos
    _acct_mod.LEDGER_CSV     = orig_ledger
    _acct_mod._ledger        = orig_singleton


# ── Shared builders ─────────────────────────────────────────────────────────────

def _new_token() -> str:
    return "tok_" + uuid.uuid4().hex[:12]


def _new_condition() -> str:
    return "0x" + uuid.uuid4().hex[:40]


def _entry(
    ledger: _Ledger,
    token_id: str,
    condition_id: str,
    *,
    fill_price: float,
    contracts: float,
    side: str = "YES",
    strategy: str = "momentum",
    fill_type: str = "MAIN",
    pair_id: str = "",
    parent_pos_id: str = "",
    market_title: str = "BTC > $50000 on 2025-06-01?",
    market_type: str = "bucket_daily",
    underlying: str = "BTC",
    spot_entry: float = 50_000.0,
    strike: float = 50_000.0,
    tte_seconds: float = 86_400.0,
    signal_source: str = "chainlink",
    signal_score: float = 75.0,
    fees_usd: float = 0.0,
    rebates_usd: float = 0.0,
) -> str:
    return ledger.on_entry_fill(
        token_id=token_id,
        condition_id=condition_id,
        order_id="ord_" + uuid.uuid4().hex[:8],
        fill_price=fill_price,
        contracts=contracts,
        source="ws",
        strategy=strategy,
        fill_type=fill_type,
        pair_id=pair_id,
        parent_pos_id=parent_pos_id,
        market_title=market_title,
        market_type=market_type,
        underlying=underlying,
        side=side,
        spot_entry=spot_entry,
        strike=strike,
        tte_seconds=tte_seconds,
        signal_source=signal_source,
        signal_score=signal_score,
        fees_usd=fees_usd,
        rebates_usd=rebates_usd,
    )


def _exit(
    ledger: _Ledger,
    token_id: str,
    *,
    fill_price: float,
    contracts: float,
    exit_type: str = "TAKER",
    spot_exit: float = 0.0,
    fees_usd: float = 0.0,
    rebates_usd: float = 0.0,
) -> str | None:
    return ledger.on_exit_fill(
        token_id=token_id,
        order_id="ord_" + uuid.uuid4().hex[:8],
        fill_price=fill_price,
        contracts=contracts,
        exit_type=exit_type,
        source="ws",
        spot_exit=spot_exit,
        fees_usd=fees_usd,
        rebates_usd=rebates_usd,
    )


# ══════════════════════════════════════════════════════════════════════════════
# A  Entry fill mechanics
# ══════════════════════════════════════════════════════════════════════════════

class TestEntryFillMechanics:
    """on_entry_fill() creates and accumulates positions correctly."""

    def test_single_fill_creates_position(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pos_id = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        pos = ledger.get_position(pos_id)
        assert pos is not None
        assert pos.entry_contracts == pytest.approx(100.0)
        assert pos.entry_vwap      == pytest.approx(0.40)
        assert pos.entry_cost_usd  == pytest.approx(40.0)   # 0.40 × 100

    def test_entry_status_is_live(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pos_id = _entry(ledger, tok, cid, fill_price=0.50, contracts=50.0)
        assert ledger.get_position(pos_id).status == PositionStatus.LIVE

    def test_returns_stable_pos_id_on_second_fill(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        id1 = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        id2 = _entry(ledger, tok, cid, fill_price=0.40, contracts=50.0)
        assert id1 == id2  # same position for same token_id

    def test_two_fills_same_price_accumulate_contracts(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=60.0)
        pos = ledger.get_position(pid)
        assert pos.entry_contracts == pytest.approx(160.0)
        assert pos.entry_cost_usd  == pytest.approx(64.0)   # 40 + 24

    def test_two_fills_different_prices_vwap(self, ledger):
        # 100 × 0.40 + 100 × 0.60 = $100 total, VWAP = 0.50
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        pid = _entry(ledger, tok, cid, fill_price=0.60, contracts=100.0)
        pos = ledger.get_position(pid)
        assert pos.entry_vwap      == pytest.approx(0.50)
        assert pos.entry_cost_usd  == pytest.approx(100.0)

    def test_three_fills_weighted_vwap(self, ledger):
        # 200 × 0.30 + 100 × 0.60 = 60 + 60 = $120, 300 contracts, VWAP = 0.40
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.30, contracts=200.0)
        _entry(ledger, tok, cid, fill_price=0.60, contracts=100.0)
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=0.0)
        pos = ledger.get_position(pid)
        assert pos.entry_vwap      == pytest.approx(0.40)
        assert pos.entry_contracts == pytest.approx(300.0)

    def test_different_tokens_create_separate_positions(self, ledger):
        tok_yes = _new_token()
        tok_no  = _new_token()
        cid = _new_condition()
        id_yes = _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=100.0, side="YES")
        id_no  = _entry(ledger, tok_no,  cid, fill_price=0.60, contracts=100.0, side="NO")
        assert id_yes != id_no
        assert ledger.get_position(id_yes).side == "YES"
        assert ledger.get_position(id_no).side  == "NO"

    def test_metadata_stored_on_first_fill(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(
            ledger, tok, cid,
            fill_price=0.50, contracts=100.0,
            side="YES", strategy="momentum", fill_type="MAIN",
            market_title="BTC > $60k?", market_type="bucket_5m",
            underlying="BTC", strike=60_000.0, tte_seconds=300.0,
            signal_source="chainlink", signal_score=88.5,
        )
        pos = ledger.get_position(pid)
        assert pos.side          == "YES"
        assert pos.strategy      == "momentum"
        assert pos.fill_type     == "MAIN"
        assert pos.market_title  == "BTC > $60k?"
        assert pos.market_type   == "bucket_5m"
        assert pos.underlying    == "BTC"
        assert pos.strike        == pytest.approx(60_000.0)
        assert pos.tte_seconds   == pytest.approx(300.0)
        assert pos.signal_source == "chainlink"
        assert pos.signal_score  == pytest.approx(88.5)

    def test_entry_time_is_iso_utc(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.50, contracts=10.0)
        pos = ledger.get_position(pid)
        dt = datetime.fromisoformat(pos.entry_time)
        assert dt.tzinfo is not None

    def test_entry_fees_and_rebates_accumulated(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.50, contracts=100.0, fees_usd=1.20, rebates_usd=0.40)
        pid = _entry(ledger, tok, cid, fill_price=0.50, contracts=50.0, fees_usd=0.60, rebates_usd=0.20)
        pos = ledger.get_position(pid)
        assert pos.fees_usd    == pytest.approx(1.80)
        assert pos.rebates_usd == pytest.approx(0.60)

    def test_fills_appended_to_jsonl(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _entry(ledger, tok, cid, fill_price=0.60, contracts=50.0)
        lines = [json.loads(l) for l in _acct_mod.FILLS_JSONL.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert all(l["token_id"] == tok for l in lines)
        assert lines[0]["side"] == "BUY"


# ══════════════════════════════════════════════════════════════════════════════
# B  Exit fill mechanics
# ══════════════════════════════════════════════════════════════════════════════

class TestExitFillMechanics:
    """on_exit_fill() updates exit VWAP, status, and returns the pos_id."""

    def test_exit_fill_returns_pos_id(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        returned = _exit(ledger, tok, fill_price=0.60, contracts=100.0)
        assert returned == pid

    def test_exit_fill_sets_exit_vwap(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.65, contracts=100.0)
        pos = ledger.get_position(pid)
        assert pos.exit_vwap      == pytest.approx(0.65)
        assert pos.exit_contracts == pytest.approx(100.0)

    def test_exit_fill_moves_status_to_closing(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0)
        assert ledger.get_position(pid).status == PositionStatus.CLOSING

    def test_exit_fill_records_exit_type(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0, exit_type="SL")
        assert ledger.get_position(pid).exit_type == "SL"

    def test_exit_fill_records_closing_since(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0)
        pos = ledger.get_position(pid)
        assert pos.closing_since != ""
        dt = datetime.fromisoformat(pos.closing_since)
        assert dt.tzinfo is not None

    def test_exit_fill_unknown_token_returns_none(self, ledger):
        result = _exit(ledger, "nonexistent_token", fill_price=0.50, contracts=10.0)
        assert result is None

    def test_multiple_exit_fills_accumulate_via_vwap(self, ledger):
        # Exit 1: 60 @ 0.65.  Exit 2: 40 @ 0.75.
        # VWAP = (60×0.65 + 40×0.75) / 100 = (39 + 30) / 100 = 0.69
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.65, contracts=60.0)
        _exit(ledger, tok, fill_price=0.75, contracts=40.0)
        pos = ledger.get_position(pid)
        assert pos.exit_vwap      == pytest.approx(0.69)
        assert pos.exit_contracts == pytest.approx(100.0)

    def test_exit_spot_stored(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0, spot_exit=62_000.0)
        assert ledger.get_position(pid).spot_exit == pytest.approx(62_000.0)

    def test_exit_fees_add_to_entry_fees(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, fees_usd=0.50)
        pid = _exit(ledger, tok, fill_price=0.60, contracts=100.0, fees_usd=1.50)
        pos = ledger.get_position(pid)
        assert pos.fees_usd == pytest.approx(2.00)   # 0.50 entry + 1.50 exit

    def test_exit_appends_sell_fill_to_jsonl(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0)
        lines = [json.loads(l) for l in _acct_mod.FILLS_JSONL.read_text().splitlines() if l.strip()]
        sells = [l for l in lines if l["side"] == "SELL"]
        assert len(sells) == 1
        assert sells[0]["fill_price"] == pytest.approx(0.60)

    def test_exit_fill_on_terminal_position_is_noop(self, ledger):
        """
        If the accounting reconciler already finalized a position (moved it to
        RESOLVED_WIN) before the risk engine calls on_exit_fill, the late fill
        must be silently ignored.  It must not overwrite exit_vwap / exit_contracts
        on the terminal record that was already written to the ledger.

        Scenario: reconciler wins the race, sets exit_vwap=1.0 (settlement).
        Risk engine then calls on_exit_fill with exit_vwap=0.82 (taker fill).
        The terminal record must preserve 1.0, not 0.82.
        """
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.52, contracts=100.0, side="YES")
        # Simulate reconciler finalizing the position directly (market resolved)
        ledger.on_resolved(cid, resolved_yes_price=1.0)
        pos = ledger.get_position(pid)
        assert pos.status == PositionStatus.RESOLVED_WIN
        assert pos.exit_vwap == pytest.approx(1.0)

        # Late on_exit_fill from risk engine must be a no-op
        result = _exit(ledger, tok, fill_price=0.82, contracts=100.0)
        pos_after = ledger.get_position(pid)
        assert result == pid                           # returns pos_id (not None)
        assert pos_after.status == PositionStatus.RESOLVED_WIN   # status unchanged
        assert pos_after.exit_vwap == pytest.approx(1.0)          # not overwritten with 0.82


# ══════════════════════════════════════════════════════════════════════════════
# C  Gross P&L arithmetic
# ══════════════════════════════════════════════════════════════════════════════

class TestGrossPnlArithmetic:
    """
    gross_pnl = (exit_vwap - entry_vwap) × exit_contracts

    This formula is token-agnostic — YES, NO, UP, DOWN all use actual prices.
    """

    def _pos(
        self,
        entry_vwap: float,
        entry_contracts: float,
        exit_vwap: float,
        exit_contracts: float,
    ) -> AccountingPosition:
        p = AccountingPosition(pos_id=str(uuid.uuid4()), strategy="m", fill_type="MAIN")
        p.entry_vwap      = entry_vwap
        p.entry_contracts = entry_contracts
        p.exit_vwap       = exit_vwap
        p.exit_contracts  = exit_contracts
        return p

    def test_profitable_long(self):
        # Bought 100 @ 0.40, sold 100 @ 0.65 → gross = (0.65-0.40)×100 = $25
        pos = self._pos(0.40, 100.0, 0.65, 100.0)
        assert _gross_pnl(pos) == pytest.approx(25.0)

    def test_losing_long(self):
        # Bought 100 @ 0.70, sold 100 @ 0.30 → gross = −$40
        pos = self._pos(0.70, 100.0, 0.30, 100.0)
        assert _gross_pnl(pos) == pytest.approx(-40.0)

    def test_breakeven(self):
        pos = self._pos(0.50, 100.0, 0.50, 100.0)
        assert _gross_pnl(pos) == pytest.approx(0.0)

    def test_scales_linearly_with_contracts(self):
        pos_small = self._pos(0.40, 100.0, 0.60, 100.0)
        pos_large = self._pos(0.40, 1000.0, 0.60, 1000.0)
        assert _gross_pnl(pos_large) == pytest.approx(_gross_pnl(pos_small) * 10)

    def test_yes_resolves_win(self):
        # YES token bought at 0.40, settles at 1.0 → gross = $60
        pos = self._pos(0.40, 100.0, 1.00, 100.0)
        assert _gross_pnl(pos) == pytest.approx(60.0)

    def test_yes_resolves_loss(self):
        # YES token bought at 0.40, settles at 0.0 → gross = −$40
        pos = self._pos(0.40, 100.0, 0.00, 100.0)
        assert _gross_pnl(pos) == pytest.approx(-40.0)

    def test_no_token_win(self):
        # NO token bought at 0.60 (independently priced), settles at 1.0 → gross = $40
        pos = self._pos(0.60, 100.0, 1.00, 100.0)
        assert _gross_pnl(pos) == pytest.approx(40.0)

    def test_no_token_loss(self):
        # NO token bought at 0.60, settles at 0.0 → gross = −$60
        pos = self._pos(0.60, 100.0, 0.00, 100.0)
        assert _gross_pnl(pos) == pytest.approx(-60.0)

    def test_partial_exit_only_on_exited_portion(self):
        # Entered 100 contracts, only 60 exited so far.
        # gross = (0.65 − 0.40) × 60 = $15
        pos = self._pos(0.40, 100.0, 0.65, 60.0)
        assert _gross_pnl(pos) == pytest.approx(15.0)

    def test_zero_exit_contracts_is_zero(self):
        pos = self._pos(0.40, 100.0, 0.0, 0.0)
        assert _gross_pnl(pos) == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# D  Net P&L = gross − fees + rebates
# ══════════════════════════════════════════════════════════════════════════════

class TestNetPnlCalculation:
    """Ledger net_pnl verified via on_resolved() writing the CSV row."""

    def _setup_and_resolve(
        self,
        ledger,
        side: str,
        resolved_yes_price: float,
        entry_price: float = 0.40,
        contracts: float = 100.0,
        entry_fees: float = 0.0,
        entry_rebates: float = 0.0,
        exit_fees: float = 0.0,
        exit_rebates: float = 0.0,
    ) -> dict:
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=entry_price, contracts=contracts,
               side=side, fees_usd=entry_fees, rebates_usd=entry_rebates)
        if side in ("YES", "UP"):
            exit_price = 1.0 if resolved_yes_price >= 0.99 else 0.0
        else:
            exit_price = 1.0 if resolved_yes_price <= 0.01 else 0.0
        _exit(ledger, tok, fill_price=exit_price, contracts=contracts,
              exit_type="RESOLVED", fees_usd=exit_fees, rebates_usd=exit_rebates)
        ledger.on_resolved(cid, resolved_yes_price)
        rows = list(csv.DictReader(_acct_mod.LEDGER_CSV.open(encoding="utf-8")))
        return rows[-1]

    def test_net_pnl_win_with_fees_and_rebates(self, ledger):
        # gross = $60; fees=1.50; rebates=0.50 → net = 60 - 1.50 + 0.50 = $59
        row = self._setup_and_resolve(
            ledger, "YES", 1.0, entry_fees=0.50, entry_rebates=0.20,
            exit_fees=1.00, exit_rebates=0.30,
        )
        assert float(row["gross_pnl"])    == pytest.approx(60.0)
        assert float(row["fees_usd"])     == pytest.approx(1.50)
        assert float(row["rebates_usd"])  == pytest.approx(0.50)
        assert float(row["net_pnl"])      == pytest.approx(59.0)

    def test_net_pnl_loss_with_fees_and_rebates(self, ledger):
        # NO loses (YES wins): gross = (0.0 - 0.40)×100 = −$40; fees=1.50; rebates=0.50 → net=−$41
        row = self._setup_and_resolve(
            ledger, "NO", 1.0, entry_fees=0.50, entry_rebates=0.20,
            exit_fees=1.00, exit_rebates=0.30,
        )
        assert float(row["gross_pnl"]) == pytest.approx(-40.0)
        assert float(row["net_pnl"])   == pytest.approx(-41.0)

    def test_fees_reduce_net_pnl(self, ledger):
        row = self._setup_and_resolve(ledger, "YES", 1.0, exit_fees=3.00)
        assert float(row["net_pnl"]) == pytest.approx(57.0)   # 60 - 3 + 0

    def test_rebates_increase_net_pnl(self, ledger):
        row = self._setup_and_resolve(ledger, "YES", 1.0, exit_rebates=2.00)
        assert float(row["net_pnl"]) == pytest.approx(62.0)   # 60 + 0 + 2

    def test_net_pnl_no_fees_no_rebates(self, ledger):
        row = self._setup_and_resolve(ledger, "YES", 1.0)
        assert float(row["net_pnl"]) == pytest.approx(float(row["gross_pnl"]))


# ══════════════════════════════════════════════════════════════════════════════
# E  YES / NO / UP / DOWN resolution — preamble token-side rules
# ══════════════════════════════════════════════════════════════════════════════

class TestResolutionTokenRules:
    """
    PREAMBLE RULES (must not be inverted):
      resolved_yes_price = 1.0  → YES / UP token won
      resolved_yes_price = 0.0  → NO / DOWN token won

    Any regression here indicates a catastrophic accounting inversion.
    """

    def _resolve_position(
        self, ledger: _Ledger, side: str, resolved_yes_price: float
    ) -> AccountingPosition:
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side=side)
        _exit(ledger, tok, fill_price=0.60, contracts=100.0, exit_type="TAKER")
        ledger.on_resolved(cid, resolved_yes_price)
        return ledger.get_position_by_token(tok)

    # ── YES token ──────────────────────────────────────────────────────────────

    def test_yes_side_price_one_is_win(self, ledger):
        pos = self._resolve_position(ledger, "YES", 1.0)
        assert pos.resolved_outcome == "WIN"
        assert pos.status == PositionStatus.RESOLVED_WIN

    def test_yes_side_price_zero_is_loss(self, ledger):
        pos = self._resolve_position(ledger, "YES", 0.0)
        assert pos.resolved_outcome == "LOSS"
        assert pos.status == PositionStatus.RESOLVED_LOSS

    # ── NO token — MUST be INVERTED relative to resolved_yes_price ─────────────

    def test_no_side_price_zero_is_win(self, ledger):
        """
        resolved_yes_price=0.0 means YES failed → NO token WINS.
        This is the critical preamble rule.
        """
        pos = self._resolve_position(ledger, "NO", 0.0)
        assert pos.resolved_outcome == "WIN", (
            "NO side MUST win when resolved_yes_price=0.0"
        )
        assert pos.status == PositionStatus.RESOLVED_WIN

    def test_no_side_price_one_is_loss(self, ledger):
        """
        resolved_yes_price=1.0 means YES succeeded → NO token LOSES.
        """
        pos = self._resolve_position(ledger, "NO", 1.0)
        assert pos.resolved_outcome == "LOSS", (
            "NO side MUST lose when resolved_yes_price=1.0"
        )
        assert pos.status == PositionStatus.RESOLVED_LOSS

    # ── UP / DOWN directional buckets ─────────────────────────────────────────

    def test_up_side_price_one_is_win(self, ledger):
        pos = self._resolve_position(ledger, "UP", 1.0)
        assert pos.resolved_outcome == "WIN"

    def test_up_side_price_zero_is_loss(self, ledger):
        pos = self._resolve_position(ledger, "UP", 0.0)
        assert pos.resolved_outcome == "LOSS"

    def test_down_side_price_zero_is_win(self, ledger):
        pos = self._resolve_position(ledger, "DOWN", 0.0)
        assert pos.resolved_outcome == "WIN"

    def test_down_side_price_one_is_loss(self, ledger):
        pos = self._resolve_position(ledger, "DOWN", 1.0)
        assert pos.resolved_outcome == "LOSS"

    def test_resolve_price_stored_on_position(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        _exit(ledger, tok, fill_price=1.0, contracts=100.0, exit_type="RESOLVED")
        ledger.on_resolved(cid, 1.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.resolve_price == pytest.approx(1.0)

    def test_resolve_price_zero_stored(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        _exit(ledger, tok, fill_price=0.0, contracts=100.0, exit_type="RESOLVED")
        ledger.on_resolved(cid, 0.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.resolve_price == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# F  Market resolution state machine
# ══════════════════════════════════════════════════════════════════════════════

class TestResolutionStateMachine:
    """on_resolved() transitions, multi-position markets, idempotency."""

    def test_position_is_terminal_after_resolve(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        _exit(ledger, tok, fill_price=1.0, contracts=100.0, exit_type="RESOLVED")
        ledger.on_resolved(cid, 1.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.status in PositionStatus.TERMINAL

    def test_resolve_without_prior_exit_sets_exit_vwap_win(self, ledger):
        """No explicit exit fill — resolution auto-sets exit_vwap to 1.0 (win)."""
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        ledger.on_resolved(cid, 1.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.exit_vwap      == pytest.approx(1.0)
        assert pos.exit_contracts == pytest.approx(100.0)
        assert pos.exit_type      == "RESOLVED"

    def test_resolve_without_prior_exit_sets_exit_vwap_zero_for_loss(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        ledger.on_resolved(cid, 0.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.exit_vwap == pytest.approx(0.0)

    def test_two_positions_same_condition_both_resolved(self, ledger):
        """YES and NO legs on same condition_id are both resolved in one call."""
        cid     = _new_condition()
        tok_yes = _new_token()
        tok_no  = _new_token()
        _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=100.0, side="YES")
        _entry(ledger, tok_no,  cid, fill_price=0.60, contracts=100.0, side="NO")
        ledger.on_resolved(cid, 1.0)   # YES wins, NO loses
        yes_pos = ledger.get_position_by_token(tok_yes)
        no_pos  = ledger.get_position_by_token(tok_no)
        assert yes_pos.resolved_outcome == "WIN"
        assert no_pos.resolved_outcome  == "LOSS"

    def test_terminal_position_not_re_resolved(self, ledger):
        """Second on_resolved() call must not change the outcome."""
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        ledger.on_resolved(cid, 1.0)
        ledger.on_resolved(cid, 0.0)   # attempt to flip — must be ignored
        pos = ledger.get_position_by_token(tok)
        assert pos.resolved_outcome == "WIN"

    def test_sl_exit_type_maps_to_sl_status(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        _exit(ledger, tok, fill_price=0.20, contracts=100.0, exit_type="SL")
        ledger.on_resolved(cid, 0.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.status == PositionStatus.SL

    def test_tp_exit_type_maps_to_tp_status(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0, side="YES")
        _exit(ledger, tok, fill_price=0.90, contracts=100.0, exit_type="TP")
        ledger.on_resolved(cid, 1.0)
        pos = ledger.get_position_by_token(tok)
        assert pos.status == PositionStatus.TP


# ══════════════════════════════════════════════════════════════════════════════
# G  Ledger CSV integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerCsvIntegrity:
    """on_resolved() must write exactly one CSV row with all fields populated."""

    def _setup_and_resolve(
        self,
        ledger: _Ledger,
        *,
        side: str = "YES",
        entry_price: float = 0.40,
        contracts: float = 100.0,
        resolved_yes_price: float = 1.0,
        fees: float = 0.0,
        rebates: float = 0.0,
        underlying: str = "BTC",
        strategy: str = "momentum",
        market_title: str = "BTC > $50k?",
    ) -> dict:
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=entry_price, contracts=contracts,
               side=side, underlying=underlying, strategy=strategy,
               market_title=market_title)
        if side in ("YES", "UP"):
            exit_price = 1.0 if resolved_yes_price >= 0.99 else 0.0
        else:
            exit_price = 1.0 if resolved_yes_price <= 0.01 else 0.0
        _exit(ledger, tok, fill_price=exit_price, contracts=contracts,
              exit_type="RESOLVED", fees_usd=fees, rebates_usd=rebates)
        ledger.on_resolved(cid, resolved_yes_price)
        rows = list(csv.DictReader(_acct_mod.LEDGER_CSV.open(encoding="utf-8")))
        assert len(rows) >= 1
        return rows[-1]

    def test_all_header_columns_present(self, ledger):
        row = self._setup_and_resolve(ledger)
        for col in LEDGER_HEADER:
            assert col in row, f"Missing column: {col}"

    def test_pos_id_is_uuid(self, ledger):
        row = self._setup_and_resolve(ledger)
        uuid.UUID(row["pos_id"])

    def test_correct_gross_pnl_win(self, ledger):
        row = self._setup_and_resolve(ledger, side="YES", entry_price=0.40, contracts=100.0, resolved_yes_price=1.0)
        assert float(row["gross_pnl"]) == pytest.approx(60.0)

    def test_correct_gross_pnl_loss(self, ledger):
        row = self._setup_and_resolve(ledger, side="YES", entry_price=0.40, contracts=100.0, resolved_yes_price=0.0)
        assert float(row["gross_pnl"]) == pytest.approx(-40.0)

    def test_net_pnl_with_fees_and_rebates(self, ledger):
        # gross = $60; fees=$3; rebates=$1 → net=$58
        row = self._setup_and_resolve(ledger, fees=3.00, rebates=1.00)
        assert float(row["net_pnl"]) == pytest.approx(58.0)

    def test_resolved_outcome_win_in_csv(self, ledger):
        row = self._setup_and_resolve(ledger, side="YES", resolved_yes_price=1.0)
        assert row["resolved_outcome"] == "WIN"

    def test_resolved_outcome_loss_in_csv(self, ledger):
        row = self._setup_and_resolve(ledger, side="YES", resolved_yes_price=0.0)
        assert row["resolved_outcome"] == "LOSS"

    def test_side_stored_correctly(self, ledger):
        row = self._setup_and_resolve(ledger, side="NO", resolved_yes_price=0.0)
        assert row["side"] == "NO"

    def test_underlying_stored_correctly(self, ledger):
        row = self._setup_and_resolve(ledger, underlying="ETH")
        assert row["underlying"] == "ETH"

    def test_strategy_stored_correctly(self, ledger):
        row = self._setup_and_resolve(ledger, strategy="maker")
        assert row["strategy"] == "maker"

    def test_entry_vwap_in_csv(self, ledger):
        row = self._setup_and_resolve(ledger, entry_price=0.35)
        assert float(row["entry_vwap"]) == pytest.approx(0.35)

    def test_entry_contracts_in_csv(self, ledger):
        row = self._setup_and_resolve(ledger, contracts=250.0)
        assert float(row["entry_contracts"]) == pytest.approx(250.0)

    def test_four_resolutions_produce_four_rows(self, ledger):
        for _ in range(4):
            tok = _new_token()
            cid = _new_condition()
            _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
            _exit(ledger, tok, fill_price=1.0, contracts=100.0, exit_type="RESOLVED")
            ledger.on_resolved(cid, 1.0)
        rows = list(csv.DictReader(_acct_mod.LEDGER_CSV.open(encoding="utf-8")))
        assert len(rows) == 4

    def test_duplicate_resolve_call_does_not_add_row(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        _exit(ledger, tok, fill_price=1.0, contracts=100.0, exit_type="RESOLVED")
        ledger.on_resolved(cid, 1.0)
        ledger.on_resolved(cid, 1.0)   # duplicate
        rows = list(csv.DictReader(_acct_mod.LEDGER_CSV.open(encoding="utf-8")))
        assert len(rows) == 1


# ══════════════════════════════════════════════════════════════════════════════
# H  Two-sided market making — YES and NO are independent
# ══════════════════════════════════════════════════════════════════════════════

class TestTwoSidedMakerPairTracking:
    """
    PREAMBLE: YES and NO tokens have separate order books.
    They must never share a pos_id or contaminate each other's VWAP.
    """

    def test_yes_and_no_legs_have_different_pos_ids(self, ledger):
        cid     = _new_condition()
        tok_yes = _new_token()
        tok_no  = _new_token()
        id_yes = _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=100.0, side="YES")
        id_no  = _entry(ledger, tok_no,  cid, fill_price=0.60, contracts=100.0, side="NO")
        assert id_yes != id_no

    def test_yes_fill_does_not_contaminate_no_vwap(self, ledger):
        cid     = _new_condition()
        tok_yes = _new_token()
        tok_no  = _new_token()
        _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=200.0, side="YES")
        id_no  = _entry(ledger, tok_no,  cid, fill_price=0.60, contracts=100.0, side="NO")
        no_pos = ledger.get_position(id_no)
        assert no_pos.entry_vwap      == pytest.approx(0.60)
        assert no_pos.entry_contracts == pytest.approx(100.0)

    def test_maker_spread_captured_regardless_of_resolution_direction(self):
        """
        Both YES and NO entered at 0.40. On resolution, one side pays 1.0,
        the other pays 0.0. Net P&L for both legs combined = $20 (= spread × size).
        """
        def _make_pos(entry: float, exit_p: float, contracts: float) -> AccountingPosition:
            p = AccountingPosition(pos_id=str(uuid.uuid4()), strategy="maker", fill_type="MAIN")
            p.entry_vwap      = entry
            p.entry_contracts = contracts
            p.exit_vwap       = exit_p
            p.exit_contracts  = contracts
            return p

        # YES wins → YES exits at 1.0, NO exits at 0.0
        yes_win  = _make_pos(0.40, 1.00, 100.0)
        no_loss  = _make_pos(0.40, 0.00, 100.0)
        assert _gross_pnl(yes_win) + _gross_pnl(no_loss) == pytest.approx(20.0)

        # NO wins → YES exits at 0.0, NO exits at 1.0
        yes_loss = _make_pos(0.40, 0.00, 100.0)
        no_win   = _make_pos(0.40, 1.00, 100.0)
        assert _gross_pnl(yes_loss) + _gross_pnl(no_win) == pytest.approx(20.0)

    def test_different_sizes_tracked_independently(self, ledger):
        cid     = _new_condition()
        tok_yes = _new_token()
        tok_no  = _new_token()
        id_yes = _entry(ledger, tok_yes, cid, fill_price=0.45, contracts=300.0, side="YES")
        id_no  = _entry(ledger, tok_no,  cid, fill_price=0.55, contracts=150.0, side="NO")
        yes_pos = ledger.get_position(id_yes)
        no_pos  = ledger.get_position(id_no)
        assert yes_pos.entry_contracts == pytest.approx(300.0)
        assert no_pos.entry_contracts  == pytest.approx(150.0)
        assert yes_pos.entry_cost_usd  == pytest.approx(0.45 * 300)
        assert no_pos.entry_cost_usd   == pytest.approx(0.55 * 150)


# ══════════════════════════════════════════════════════════════════════════════
# I  Pair / hedge relationships and on_pair_promoted
# ══════════════════════════════════════════════════════════════════════════════

class TestPairAndHedgeRelationships:
    """pair_id groups ON legs; parent_pos_id links hedge to main."""

    def test_pair_id_groups_two_legs(self, ledger):
        cid     = _new_condition()
        tok_yes = _new_token()
        tok_no  = _new_token()
        pair    = "pair_" + uuid.uuid4().hex[:8]
        id_yes = _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=100.0,
                        side="YES", pair_id=pair)
        id_no  = _entry(ledger, tok_no,  cid, fill_price=0.60, contracts=100.0,
                        side="NO",  pair_id=pair)
        positions = ledger.get_positions_for_pair(pair)
        assert len(positions) == 2
        ids = {p.pos_id for p in positions}
        assert id_yes in ids
        assert id_no  in ids

    def test_parent_pos_id_links_hedge_to_main(self, ledger):
        cid_main  = _new_condition()
        tok_main  = _new_token()
        tok_hedge = _new_token()
        main_pid = _entry(ledger, tok_main, cid_main, fill_price=0.40, contracts=100.0,
                          side="YES", strategy="momentum", fill_type="MAIN")
        _entry(ledger, tok_hedge, cid_main, fill_price=0.60, contracts=100.0,
               side="NO", strategy="momentum_hedge", fill_type="HEDGE",
               parent_pos_id=main_pid)
        hedge_pos = ledger.get_position_by_token(tok_hedge)
        assert hedge_pos.parent_pos_id == main_pid
        assert hedge_pos.fill_type     == "HEDGE"

    def test_on_pair_promoted_changes_fill_type_to_main(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0,
                     side="YES", strategy="opening_neutral", fill_type="WINNER")
        ledger.on_pair_promoted(tok, new_fill_type="MAIN")
        pos = ledger.get_position(pid)
        assert pos.fill_type == "MAIN"
        assert pos.strategy  == "momentum"

    def test_on_pair_promoted_unknown_token_does_not_raise(self, ledger):
        ledger.on_pair_promoted("nonexistent_token", "MAIN")  # must not raise

    def test_add_fees_accumulates_on_existing_position(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.40, contracts=100.0)
        ledger.add_fees(tok, fees_usd=1.00, rebates_usd=0.30)
        ledger.add_fees(tok, fees_usd=0.50, rebates_usd=0.10)
        pos = ledger.get_position(pid)
        assert pos.fees_usd    == pytest.approx(1.50)
        assert pos.rebates_usd == pytest.approx(0.40)

    def test_add_fees_unknown_token_does_not_raise(self, ledger):
        ledger.add_fees("nonexistent_token", fees_usd=5.00)


# ══════════════════════════════════════════════════════════════════════════════
# J  Position query helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionQueryHelpers:
    def test_get_position_returns_none_for_unknown(self, ledger):
        assert ledger.get_position("nonexistent_id") is None

    def test_get_position_by_token_returns_none_for_unknown(self, ledger):
        assert ledger.get_position_by_token("nonexistent_token") is None

    def test_get_position_by_token_finds_existing(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.50, contracts=100.0)
        pos = ledger.get_position_by_token(tok)
        assert pos is not None
        assert pos.pos_id == pid

    def test_get_all_positions_includes_live_and_terminal(self, ledger):
        tok1 = _new_token()
        tok2 = _new_token()
        cid1 = _new_condition()
        cid2 = _new_condition()
        _entry(ledger, tok1, cid1, fill_price=0.40, contracts=100.0)
        _entry(ledger, tok2, cid2, fill_price=0.40, contracts=100.0)
        ledger.on_resolved(cid2, 1.0)
        assert len(ledger.get_all_positions()) == 2

    def test_get_open_positions_excludes_terminal(self, ledger):
        tok1 = _new_token()
        tok2 = _new_token()
        cid1 = _new_condition()
        cid2 = _new_condition()
        _entry(ledger, tok1, cid1, fill_price=0.40, contracts=100.0)
        _entry(ledger, tok2, cid2, fill_price=0.40, contracts=100.0)
        ledger.on_resolved(cid2, 1.0)
        open_pos = ledger.get_open_positions()
        open_ids = {p.pos_id for p in open_pos}
        tok1_pos = ledger.get_position_by_token(tok1)
        tok2_pos = ledger.get_position_by_token(tok2)
        assert tok1_pos.pos_id     in open_ids
        assert tok2_pos.pos_id not in open_ids

    def test_get_positions_for_pair_empty_when_missing(self, ledger):
        assert ledger.get_positions_for_pair("nonexistent_pair") == []


# ══════════════════════════════════════════════════════════════════════════════
# K  _vwap helper
# ══════════════════════════════════════════════════════════════════════════════

class TestVwapHelper:
    """_vwap() is the core accumulation primitive — tested independently."""

    def test_first_fill(self):
        assert _vwap(0.0, 0.0, 0.50, 100.0) == pytest.approx(0.50)

    def test_two_equal_fills(self):
        v = _vwap(0.0,  0.0,   0.40, 100.0)
        v = _vwap(v,    100.0, 0.40, 100.0)
        assert v == pytest.approx(0.40)

    def test_two_different_fills(self):
        v = _vwap(0.0,  0.0,   0.40, 100.0)
        v = _vwap(v,    100.0, 0.60, 100.0)
        assert v == pytest.approx(0.50)

    def test_size_weighted_not_simple_average(self):
        # 200 @ 0.30 and 100 @ 0.60 → VWAP = 0.40, NOT (0.30+0.60)/2 = 0.45
        v = _vwap(0.0,  0.0,   0.30, 200.0)
        v = _vwap(v,    200.0, 0.60, 100.0)
        assert v == pytest.approx(0.40)
        assert v != pytest.approx(0.45)

    def test_zero_total_qty_returns_zero(self):
        assert _vwap(0.50, 0.0, 0.60, 0.0) == pytest.approx(0.0)

    def test_commutative_order_of_fills(self):
        """Final VWAP must be the same regardless of fill order."""
        v_ab = _vwap(_vwap(0.0, 0.0, 0.30, 100.0), 100.0, 0.70, 300.0)
        v_ba = _vwap(_vwap(0.0, 0.0, 0.70, 300.0), 300.0, 0.30, 100.0)
        assert v_ab == pytest.approx(v_ba)


# ══════════════════════════════════════════════════════════════════════════════
# L  Token-space independence (preamble rule: separate order books)
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenSpaceIndependence:
    """YES and NO have their own prices — no cross-contamination."""

    def test_yes_fill_does_not_create_position_for_untraded_no_token(self, ledger):
        cid     = _new_condition()
        tok_yes = _new_token()
        _entry(ledger, tok_yes, cid, fill_price=0.40, contracts=100.0, side="YES")
        tok_no_untraded = _new_token()
        assert ledger.get_position_by_token(tok_no_untraded) is None

    def test_resolving_one_market_does_not_affect_other_markets(self, ledger):
        cid_a = _new_condition()
        cid_b = _new_condition()
        tok_a = _new_token()
        tok_b = _new_token()
        _entry(ledger, tok_a, cid_a, fill_price=0.40, contracts=100.0, side="YES")
        _entry(ledger, tok_b, cid_b, fill_price=0.40, contracts=100.0, side="YES")
        ledger.on_resolved(cid_a, 1.0)
        pos_b = ledger.get_position_by_token(tok_b)
        assert pos_b.status == PositionStatus.LIVE

    def test_no_entry_cost_uses_no_token_price_not_yes_complement(self, ledger):
        """
        NO token cost = NO fill_price × contracts.
        It is NOT derived from 1 - YES price.
        We record the actual market price paid.
        """
        tok_no = _new_token()
        cid    = _new_condition()
        pid = _entry(ledger, tok_no, cid, fill_price=0.55, contracts=100.0, side="NO")
        pos = ledger.get_position(pid)
        assert pos.entry_cost_usd == pytest.approx(55.0)   # 0.55 × 100, NOT (1-0.55)×100

    def test_yes_entry_cost_is_yes_price_times_contracts(self, ledger):
        tok = _new_token()
        cid = _new_condition()
        pid = _entry(ledger, tok, cid, fill_price=0.70, contracts=100.0, side="YES")
        pos = ledger.get_position(pid)
        assert pos.entry_cost_usd == pytest.approx(70.0)
