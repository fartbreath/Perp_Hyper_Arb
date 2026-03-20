"""
tests/test_rebate_csv.py — QA regression tests for rebate_usd and fills-CSV schema.

Gaps closed  (session /qa — depth-gate + rebate_usd changes):
  R1.  FillResult.rebate_usd == 0 when fees_enabled=False
  R2.  FillResult.rebate_usd == 0 when rebate_pct == 0
  R3.  FillResult.rebate_usd > 0 when fees_enabled=True and rebate_pct > 0
  R3b. FillResult is None (not partially computed) when risk gate blocks the fill
  R4a. FILLS_HEADER ends with "rebate_usd"
  R4b. Paper fill CSV row contains rebate_usd == 0 when fees disabled
  R4c. Paper fill CSV row contains rebate_usd > 0 when fees enabled
  R5.  Live fill CSV row contains rebate_usd column
  R6.  _ensure_fills_csv creates new file with current FILLS_HEADER
  R7.  _ensure_fills_csv backs up stale-schema file and writes current header

Run:  pytest tests/test_rebate_csv.py -v
"""
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock, AsyncMock

import config

config.PAPER_TRADING = True
config.STRATEGY_MAKER_ENABLED = True

from fill_simulator import FILLS_CSV, FILLS_HEADER, FillSimulator
from live_fill_handler import LiveFillHandler
from risk import RiskEngine
from strategies.maker.fill_logic import FillResult, open_position_from_fill
from strategies.maker.signals import ActiveQuote


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _make_quote(side: str = "BUY", price: float = 0.40, size: float = 50.0) -> ActiveQuote:
    return ActiveQuote(
        market_id="mkt_rebate",
        token_id="tok_yes_rebate",
        side=side,
        price=price,
        size=size,
        order_id="reb-ord-001",
    )


def _make_market(fees_enabled: bool = False, rebate_pct: float = 0.0):
    m = MagicMock()
    m.condition_id = "mkt_rebate"
    m.token_id_yes = "tok_yes_rebate"
    m.underlying = "ETH"
    m.title = "ETH above 3k"
    m.market_type = "bucket_1h"
    m.is_fee_free = not fees_enabled
    m.fees_enabled = fees_enabled
    m.rebate_pct = rebate_pct
    m.max_incentive_spread = 0.04
    m.tick_size = 0.01
    return m


def _make_sim_factory(market, quote):
    """Return a FillSimulator whose dependencies are fully mocked."""
    pm = MagicMock()
    pm._markets = {"mkt_rebate": market}
    pm._books = {}
    pm.place_limit = AsyncMock(return_value="paper-exit-001")

    maker = MagicMock()
    maker.consume_fill = MagicMock(return_value=quote)
    maker.record_fill = MagicMock()
    maker.get_hl_mid = MagicMock(return_value=None)
    maker.schedule_hedge_rebalance = MagicMock()
    maker._reprice_market = AsyncMock()
    maker.get_active_quotes = MagicMock(return_value={})
    maker.trigger_post_fill_reprice = MagicMock()
    maker._active_quotes = {}

    risk = RiskEngine()
    monitor = MagicMock()
    monitor.record_entry_deviation = MagicMock()

    return FillSimulator(pm, maker, risk, monitor), pm, maker, risk, monitor


# ── R1-R3b: FillResult.rebate_usd ─────────────────────────────────────────────

class TestFillResultRebateUsd:
    """Regression: rebate_usd must be computed correctly in fill_logic.open_position_from_fill."""

    def _run(self, fees_enabled: bool, rebate_pct: float,
             side: str = "BUY", price: float = 0.40, size: float = 50.0):
        quote = _make_quote(side=side, price=price, size=size)
        market = _make_market(fees_enabled=fees_enabled, rebate_pct=rebate_pct)
        maker = MagicMock()
        maker.consume_fill = MagicMock(return_value=quote)
        maker.record_fill = MagicMock()
        risk = RiskEngine()
        monitor = MagicMock()
        monitor.record_entry_deviation = MagicMock()
        result = open_position_from_fill(
            maker=maker,
            risk=risk,
            monitor=monitor,
            key="tok_yes_rebate",
            fill_price=price,
            filled_size=size,
            market=market,
        )
        return result, risk

    def test_rebate_usd_zero_when_fees_disabled(self):
        """R1: fees_enabled=False → rebate_usd == 0 regardless of rebate_pct."""
        result, _ = self._run(fees_enabled=False, rebate_pct=0.50)
        assert result is not None
        assert result.rebate_usd == pytest.approx(0.0)

    def test_rebate_usd_zero_when_rebate_pct_is_zero(self):
        """R2: fees_enabled=True but rebate_pct=0 → rebate_usd == 0."""
        result, _ = self._run(fees_enabled=True, rebate_pct=0.0)
        assert result is not None
        assert result.rebate_usd == pytest.approx(0.0)

    def test_rebate_usd_positive_when_fees_enabled(self):
        """R3: fees_enabled=True + rebate_pct=0.5, price=0.40, size=50 → rebate_usd > 0.

        Expected:  50 * 0.0175 * 0.40 * 0.60 * 0.50 * 0.25 = 0.02625
        """
        result, _ = self._run(fees_enabled=True, rebate_pct=0.50, price=0.40, size=50.0)
        assert result is not None
        assert result.rebate_usd == pytest.approx(0.02625, rel=1e-4)

    def test_rebate_usd_is_zero_for_no_side(self):
        """R3 (NO side): SELL quote → position_side=NO, token_price=1-fill_price.

        Expected:  50 * 0.0175 * 0.60 * 0.40 * 0.50 * 0.25 = 0.02625  (symmetric)
        """
        result, _ = self._run(
            fees_enabled=True, rebate_pct=0.50, side="SELL", price=0.40, size=50.0
        )
        assert result is not None
        # token_price for NO = 1 - fill_price = 0.60; still > 0
        assert result.rebate_usd > 0.0

    def test_fillresult_is_none_when_risk_gate_blocks(self):
        """R3b: If risk gate rejects the fill, FillResult is None (no partial rebate side-effects)."""
        quote = _make_quote()
        market = _make_market(fees_enabled=True, rebate_pct=0.50)
        maker = MagicMock()
        maker.consume_fill = MagicMock(return_value=quote)
        maker.record_fill = MagicMock()
        monitor = MagicMock()
        monitor.record_entry_deviation = MagicMock()

        orig_limit = config.MAX_TOTAL_PM_EXPOSURE
        config.MAX_TOTAL_PM_EXPOSURE = 0.01  # any fill will exceed this
        try:
            risk = RiskEngine()
            result = open_position_from_fill(
                maker=maker,
                risk=risk,
                monitor=monitor,
                key="tok_yes_rebate",
                fill_price=0.40,
                filled_size=50.0,
                market=market,
            )
            assert result is None
        finally:
            config.MAX_TOTAL_PM_EXPOSURE = orig_limit


# ── R4a: FILLS_HEADER schema ───────────────────────────────────────────────────

class TestFillsHeaderSchema:
    """R4a: FILLS_HEADER must include 'rebate_usd' as its final column."""

    def test_fills_header_includes_rebate_usd(self):
        assert "rebate_usd" in FILLS_HEADER

    def test_fills_header_rebate_usd_is_last(self):
        assert FILLS_HEADER[-1] == "rebate_usd"


# ── R4b/c: Paper fills CSV ─────────────────────────────────────────────────────

class TestPaperFillsCsvRebateUsd:
    """R4b/c: FillSimulator._on_fill must write rebate_usd to fills.csv."""

    def test_paper_fill_writes_rebate_usd_zero(self, tmp_path):
        """R4b: fees_enabled=False → rebate_usd column present and == 0 in CSV row."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(FILLS_HEADER)

        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv
        try:
            quote = _make_quote(side="BUY", price=0.40, size=50.0)
            market = _make_market(fees_enabled=False, rebate_pct=0.0)
            sim, *_ = _make_sim_factory(market, quote)

            asyncio.get_event_loop().run_until_complete(
                sim._on_fill("tok_yes_rebate", quote, market, 50.0)
            )

            with fake_csv.open() as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert "rebate_usd" in rows[0]
            assert float(rows[0]["rebate_usd"]) == pytest.approx(0.0)
        finally:
            fsim_module.FILLS_CSV = orig_csv

    def test_paper_fill_writes_nonzero_rebate_when_fees_enabled(self, tmp_path):
        """R4c: fees_enabled=True + rebate_pct=0.5 → rebate_usd == 0.02625 in CSV row."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(FILLS_HEADER)

        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv
        try:
            quote = _make_quote(side="BUY", price=0.40, size=50.0)
            market = _make_market(fees_enabled=True, rebate_pct=0.50)
            sim, *_ = _make_sim_factory(market, quote)

            asyncio.get_event_loop().run_until_complete(
                sim._on_fill("tok_yes_rebate", quote, market, 50.0)
            )

            with fake_csv.open() as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert float(rows[0]["rebate_usd"]) == pytest.approx(0.02625, rel=1e-4)
        finally:
            fsim_module.FILLS_CSV = orig_csv


# ── R5: Live fills CSV ─────────────────────────────────────────────────────────

class TestLiveFillsCsvRebateUsd:
    """R5: LiveFillHandler._process_fill_slice must write rebate_usd to fills.csv."""

    def test_live_fill_writes_rebate_usd_column(self, tmp_path):
        """R5: fees_enabled=False → rebate_usd column written and == 0 in live CSV row."""
        import live_fill_handler as lfh_module

        fake_csv = tmp_path / "fills.csv"
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(FILLS_HEADER)

        orig_csv = lfh_module.FILLS_CSV
        orig_paper = config.PAPER_TRADING
        config.PAPER_TRADING = False
        lfh_module.FILLS_CSV = fake_csv

        try:
            quote = _make_quote(side="BUY", price=0.45, size=50.0)
            market = _make_market(fees_enabled=False, rebate_pct=0.0)

            pm = MagicMock()
            pm._markets = {"mkt_rebate": market}
            maker = MagicMock()
            maker.consume_fill = MagicMock(return_value=quote)
            maker.record_fill = MagicMock()
            maker._active_quotes = {}
            maker._reprice_market = AsyncMock()
            risk = RiskEngine()
            monitor = MagicMock()
            monitor.record_entry_deviation = MagicMock()

            handler = LiveFillHandler(pm=pm, maker=maker, risk=risk, monitor=monitor)
            asyncio.get_event_loop().run_until_complete(
                handler._process_fill_slice(
                    "tok_yes_rebate", "reb-ord-001", 0.45, "BUY", 50.0, market
                )
            )

            with fake_csv.open() as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 1
            assert "rebate_usd" in rows[0]
            assert float(rows[0]["rebate_usd"]) == pytest.approx(0.0)
        finally:
            lfh_module.FILLS_CSV = orig_csv
            config.PAPER_TRADING = orig_paper


# ── R6-R7: _ensure_fills_csv schema migration ─────────────────────────────────

class TestEnsureFillsCsvSchema:
    """R6/R7: _ensure_fills_csv must create or migrate fills.csv when the schema changes."""

    def _make_blank_sim(self, tmp_path):
        """Return a fresh FillSimulator pointing at tmp_path/fills.csv."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv

        pm = MagicMock()
        pm.place_limit = AsyncMock()
        maker = MagicMock()
        maker.get_active_quotes = MagicMock(return_value={})
        maker.trigger_post_fill_reprice = MagicMock()
        risk = RiskEngine()
        monitor = MagicMock()

        try:
            FillSimulator(pm, maker, risk, monitor)
        finally:
            fsim_module.FILLS_CSV = orig_csv

        return fake_csv

    def test_creates_new_file_with_current_header(self, tmp_path):
        """R6: When fills.csv does not exist, FillSimulator.__init__ creates it with FILLS_HEADER."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        assert not fake_csv.exists()

        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv
        try:
            pm = MagicMock()
            pm.place_limit = AsyncMock()
            maker = MagicMock()
            maker.get_active_quotes = MagicMock(return_value={})
            maker.trigger_post_fill_reprice = MagicMock()
            FillSimulator(pm, maker, RiskEngine(), MagicMock())
        finally:
            fsim_module.FILLS_CSV = orig_csv

        assert fake_csv.exists()
        with fake_csv.open() as f:
            header = next(csv.reader(f))
        assert header == FILLS_HEADER

    def test_migrates_stale_schema_by_backing_up(self, tmp_path):
        """R7: When fills.csv has old schema (missing rebate_usd), it is backed up
        and a fresh file with the current header is created."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        old_header = [c for c in FILLS_HEADER if c != "rebate_usd"]
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(old_header)

        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv
        try:
            pm = MagicMock()
            pm.place_limit = AsyncMock()
            maker = MagicMock()
            maker.get_active_quotes = MagicMock(return_value={})
            maker.trigger_post_fill_reprice = MagicMock()
            FillSimulator(pm, maker, RiskEngine(), MagicMock())
        finally:
            fsim_module.FILLS_CSV = orig_csv

        # New file must have current header
        with fake_csv.open() as f:
            header = next(csv.reader(f))
        assert header == FILLS_HEADER

        # Backup file must exist with old header
        backups = list(tmp_path.glob("fills_*.csv.bak"))
        assert len(backups) == 1, "Stale-schema file must be backed up"
        with backups[0].open() as f:
            backed_header = next(csv.reader(f))
        assert backed_header == old_header

    def test_no_backup_when_schema_matches(self, tmp_path):
        """R7b: When fills.csv already has the correct header, no backup is created."""
        import fill_simulator as fsim_module

        fake_csv = tmp_path / "fills.csv"
        with fake_csv.open("w", newline="") as f:
            csv.writer(f).writerow(FILLS_HEADER)

        orig_csv = fsim_module.FILLS_CSV
        fsim_module.FILLS_CSV = fake_csv
        try:
            pm = MagicMock()
            pm.place_limit = AsyncMock()
            maker = MagicMock()
            maker.get_active_quotes = MagicMock(return_value={})
            maker.trigger_post_fill_reprice = MagicMock()
            FillSimulator(pm, maker, RiskEngine(), MagicMock())
        finally:
            fsim_module.FILLS_CSV = orig_csv

        backups = list(tmp_path.glob("fills_*.csv.bak"))
        assert len(backups) == 0, "No backup should be created when schema is current"
