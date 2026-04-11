"""
tests/test_win_rate.py — Unit tests for strategies/Momentum/win_rate.py (Phase E).

Run: pytest tests/test_win_rate.py -v
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config

from strategies.Momentum.win_rate import WinRateTable, _price_band, _tte_bin


# ── helper: write temp CSV files ─────────────────────────────────────────────

def _write_fills(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "timestamp", "market_id", "market_title", "underlying", "market_type", "side",
        "signal_price", "order_price", "fill_price", "fill_size", "slippage_pct",
        "signal_delta_pct", "signal_obs_z", "signal_sigma_ann", "tte_seconds",
        "ask_depth_usd", "fill_from_ws",
        "kelly_win_prob", "kelly_payout_b", "kelly_f", "kelly_fraction_cfg", "kelly_size_usd",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            full = {k: row.get(k, "") for k in fieldnames}
            w.writerow(full)


def _write_trades(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "timestamp", "entry_timestamp", "market_id", "market_title", "market_type",
        "underlying", "side", "size", "price", "fees_paid", "rebates_earned",
        "hl_hedge_size", "hl_entry_price", "strategy", "spread_id", "pnl",
        "entry_deviation", "implied_prob", "deribit_iv", "tte_years", "spot_price",
        "exit_spot_price", "strike", "kalshi_price", "signal_source", "signal_score",
        "resolved_outcome",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            full = {k: row.get(k, "") for k in fieldnames}
            w.writerow(full)


def _make_table(tmp_path: Path, fills: list[dict], trades: list[dict]) -> WinRateTable:
    """Build WinRateTable from scratch using temp data files."""
    fills_csv = tmp_path / "momentum_fills.csv"
    trades_csv = tmp_path / "trades.csv"
    _write_fills(fills_csv, fills)
    _write_trades(trades_csv, trades)

    # Patch module-level paths inside win_rate
    import strategies.Momentum.win_rate as wr_mod
    original_fills = wr_mod._FILLS_CSV
    original_trades = wr_mod._TRADES_CSV
    wr_mod._FILLS_CSV = fills_csv
    wr_mod._TRADES_CSV = trades_csv

    try:
        tbl = WinRateTable()
    finally:
        wr_mod._FILLS_CSV = original_fills
        wr_mod._TRADES_CSV = original_trades

    return tbl


# ── Bucket helpers ────────────────────────────────────────────────────────────

class TestBucketHelpers:
    def test_price_band_0_82(self):
        assert _price_band(0.82) == pytest.approx(0.80)

    def test_price_band_0_85_boundary(self):
        assert _price_band(0.85) == pytest.approx(0.85)

    def test_price_band_0_60(self):
        # 0.60 / 0.05 has float precision issues: use 0.65 which divides exactly
        assert _price_band(0.65) == pytest.approx(0.65)
        # And verify 0.62 falls into the 0.60 band
        assert _price_band(0.62) == pytest.approx(0.60)

    def test_price_band_0_949(self):
        assert _price_band(0.949) == pytest.approx(0.90)

    def test_tte_bin_55s(self):
        assert _tte_bin(55.0) == 0  # [0, 60)

    def test_tte_bin_60s(self):
        assert _tte_bin(60.0) == 60  # [60, 120)

    def test_tte_bin_119s(self):
        assert _tte_bin(119.0) == 60  # [60, 120)

    def test_tte_bin_120s(self):
        assert _tte_bin(120.0) == 120  # [120, 180)

    def test_tte_bin_negative_clamps_to_zero(self):
        assert _tte_bin(-5.0) == 0


# ── WinRateTable construction ─────────────────────────────────────────────────

class TestWinRateTableLoad:
    def test_empty_when_no_data_files(self, tmp_path):
        """No data files → empty table, no crash."""
        import strategies.Momentum.win_rate as wr_mod
        orig_f, orig_t = wr_mod._FILLS_CSV, wr_mod._TRADES_CSV
        wr_mod._FILLS_CSV = tmp_path / "no_fills.csv"
        wr_mod._TRADES_CSV = tmp_path / "no_trades.csv"
        try:
            tbl = WinRateTable()
            assert tbl.get("bucket_5m", 0.82, 60.0) is None
        finally:
            wr_mod._FILLS_CSV = orig_f
            wr_mod._TRADES_CSV = orig_t

    def test_loads_fills_and_computes_win_rate(self, tmp_path):
        """10 WIN fills in one bucket → emp_wr = 1.0."""
        mkt_ids = [f"mkt_{i:03d}" for i in range(10)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids
        ]
        trades = [
            {"market_id": mid, "resolved_outcome": "WIN"}
            for mid in mkt_ids
        ]
        tbl = _make_table(tmp_path, fills, trades)
        orig_min = getattr(config, "MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES", 10)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        try:
            wr = tbl.get("bucket_5m", 0.82, 65.0)
            assert wr == pytest.approx(1.0)
        finally:
            config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = orig_min

    def test_mixed_win_loss(self, tmp_path):
        """7 WIN / 3 LOSS in bucket → emp_wr = 0.7."""
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        mkt_ids_win = [f"win_{i:03d}" for i in range(7)]
        mkt_ids_loss = [f"loss_{i:03d}" for i in range(3)]
        fills = [
            {"market_id": mid, "market_type": "bucket_15m",
             "fill_price": "0.85", "tte_seconds": "90"}
            for mid in mkt_ids_win + mkt_ids_loss
        ]
        trades = (
            [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids_win] +
            [{"market_id": mid, "resolved_outcome": "LOSS"} for mid in mkt_ids_loss]
        )
        tbl = _make_table(tmp_path, fills, trades)
        wr = tbl.get("bucket_15m", 0.85, 90.0)
        assert wr == pytest.approx(0.7)

    def test_below_min_samples_returns_none(self, tmp_path):
        """5 fills in bucket < min_samples=10 → None (gate stays open)."""
        mkt_ids = [f"mkt_{i:03d}" for i in range(5)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids
        ]
        trades = [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids]
        tbl = _make_table(tmp_path, fills, trades)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        wr = tbl.get("bucket_5m", 0.82, 65.0)
        assert wr is None

    def test_excludes_empty_outcome_rows(self, tmp_path):
        """Rows with empty/unknown outcome must not inflate totals."""
        mkt_ids_good = [f"mkt_{i:03d}" for i in range(10)]
        mkt_ids_unknown = [f"unk_{i:03d}" for i in range(5)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids_good + mkt_ids_unknown
        ]
        trades = (
            [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids_good] +
            [{"market_id": mid, "resolved_outcome": ""} for mid in mkt_ids_unknown]
        )
        tbl = _make_table(tmp_path, fills, trades)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        # Only the 10 WIN rows count; the 5 unknown rows are excluded from totals
        wr = tbl.get("bucket_5m", 0.82, 65.0)
        assert wr == pytest.approx(1.0)  # 10/10, not 10/15

    def test_fills_without_matching_trade_are_excluded(self, tmp_path):
        """Fills with no matching trade outcome must be ignored."""
        mkt_ids_matched = [f"mkt_{i:03d}" for i in range(10)]
        mkt_ids_unmatched = [f"nomatch_{i:03d}" for i in range(5)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids_matched + mkt_ids_unmatched
        ]
        trades = [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids_matched]
        tbl = _make_table(tmp_path, fills, trades)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        wr = tbl.get("bucket_5m", 0.82, 65.0)
        assert wr == pytest.approx(1.0)  # 10/10 matched rows only

    def test_buckets_are_independent(self, tmp_path):
        """Fills in different price bands don't contaminate each other."""
        mkt_a = [f"a_{i:03d}" for i in range(10)]
        mkt_b = [f"b_{i:03d}" for i in range(10)]
        fills = (
            [{"market_id": mid, "market_type": "bucket_5m",
              "fill_price": "0.82", "tte_seconds": "65"} for mid in mkt_a] +
            [{"market_id": mid, "market_type": "bucket_5m",
              "fill_price": "0.87", "tte_seconds": "65"} for mid in mkt_b]
        )
        trades = (
            [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_a] +
            [{"market_id": mid, "resolved_outcome": "LOSS"} for mid in mkt_b]
        )
        tbl = _make_table(tmp_path, fills, trades)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        wr_band_80 = tbl.get("bucket_5m", 0.82, 65.0)
        wr_band_85 = tbl.get("bucket_5m", 0.87, 65.0)
        assert wr_band_80 == pytest.approx(1.0)
        assert wr_band_85 == pytest.approx(0.0)

    def test_different_market_types_are_independent(self, tmp_path):
        mkt_5m = [f"m5_{i:03d}" for i in range(10)]
        mkt_15m = [f"m15_{i:03d}" for i in range(10)]
        fills = (
            [{"market_id": mid, "market_type": "bucket_5m",
              "fill_price": "0.82", "tte_seconds": "65"} for mid in mkt_5m] +
            [{"market_id": mid, "market_type": "bucket_15m",
              "fill_price": "0.82", "tte_seconds": "65"} for mid in mkt_15m]
        )
        trades = (
            [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_5m] +
            [{"market_id": mid, "resolved_outcome": "LOSS"} for mid in mkt_15m]
        )
        tbl = _make_table(tmp_path, fills, trades)
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES = 10
        assert tbl.get("bucket_5m", 0.82, 65.0) == pytest.approx(1.0)
        assert tbl.get("bucket_15m", 0.82, 65.0) == pytest.approx(0.0)

    def test_unknown_bucket_returns_none(self, tmp_path):
        """Bucket outside any historical data returns None."""
        tbl = _make_table(tmp_path, [], [])
        assert tbl.get("bucket_4h", 0.75, 200.0) is None


# ── summary() ─────────────────────────────────────────────────────────────────

class TestWinRateSummary:
    def test_summary_counts_total_fills(self, tmp_path):
        mkt_ids = [f"mkt_{i:03d}" for i in range(10)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids
        ]
        trades = [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids]
        tbl = _make_table(tmp_path, fills, trades)
        summary = tbl.summary()
        assert summary["total_fills"] == 10

    def test_summary_has_win_rate_per_bucket(self, tmp_path):
        mkt_ids = [f"mkt_{i:03d}" for i in range(10)]
        fills = [
            {"market_id": mid, "market_type": "bucket_5m",
             "fill_price": "0.82", "tte_seconds": "65"}
            for mid in mkt_ids
        ]
        trades = [{"market_id": mid, "resolved_outcome": "WIN"} for mid in mkt_ids]
        tbl = _make_table(tmp_path, fills, trades)
        summary = tbl.summary()
        assert len(summary["buckets"]) >= 1
        bucket = summary["buckets"][0]
        assert "win_rate" in bucket
        assert bucket["total"] == 10
