"""
tests/test_feature_builder.py — Unit tests for analysis/feature_builder.py (ML-02)

Covers the three ACs from ML_PRD.md:
  1. oracle-alignment-with-no-matching-tick  → null features, row kept
  2. missing-clob-session                    → null CLOB features, row kept
  3. split-is-chronological                  → train < val < test by timestamp

Run:  pytest tests/test_feature_builder.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Make repo root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.feature_builder import (
    ORACLE_ALIGN_WINDOW_S,
    SPLIT_FRACS,
    align_oracle_tick,
    assign_split,
    build,
    _load_trades,
    _load_on_fills,
    _load_momentum_fills,
    _load_oracle_ticks,
    _derive_resolved_outcome,
    _derive_exit_was_wrong,
    _match_clob_session,
    _safe_float,
    _safe_bool,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts(offset_s: float = 0.0) -> pd.Timestamp:
    """Return a fixed UTC timestamp displaced by offset_s seconds."""
    base = pd.Timestamp("2026-05-01T10:00:00", tz="UTC")
    return base + pd.Timedelta(seconds=offset_s)


def _make_oracle_df(ticks: list[tuple[float, str, float]]) -> pd.DataFrame:
    """Build a minimal oracle_ticks DataFrame.  ticks = [(offset_s, coin, price)]"""
    rows = [
        {"_ts": _ts(offset_s), "coin": coin, "price": price}
        for offset_s, coin, price in ticks
    ]
    return pd.DataFrame(rows)


def _make_clob_session(coin: str, capture_offset_s: float = 0.0) -> pd.DataFrame:
    """Build a minimal pm_clob_stream DataFrame for one coin, YES side."""
    return pd.DataFrame([
        {
            "token_id": "abc123",
            "coin": coin,
            "token_side": "YES",
            "_ts": _ts(capture_offset_s),
            "best_bid": 0.72,
            "best_ask": 0.75,
            "spread": 0.03,
            "bid_depth_5_usdc": 250.0,
        }
    ])


def _make_trades_csv(tmp_dir: Path, rows: list[dict]) -> None:
    """Write a trades.csv to tmp_dir."""
    df = pd.DataFrame(rows)
    df.to_csv(tmp_dir / "trades.csv", index=False)


def _make_empty_csvs(tmp_dir: Path) -> None:
    """Write empty but schema-correct CSVs for sources other than trades.csv."""
    pd.DataFrame(columns=[
        "timestamp", "pair_id", "market_id", "market_title", "underlying", "market_type",
        "yes_entry", "no_entry", "combined_cost", "yes_spread", "no_spread",
        "funding_rate", "yes_depth_share", "loser_confidence_score",
        "yes_sell_price_placed", "no_sell_price_placed", "loser_leg",
        "loser_fill_price", "loser_fill_time_secs", "winner_exit_price",
    ]).to_csv(tmp_dir / "on_fills.csv", index=False)

    pd.DataFrame(columns=[
        "timestamp", "market_id", "market_title", "underlying", "market_type",
        "side", "signal_price", "order_price", "fill_price", "fill_size",
        "slippage_pct", "signal_delta_pct", "signal_obs_z", "signal_sigma_ann",
        "tte_seconds", "ask_depth_usd", "fill_from_ws", "kelly_win_prob",
        "kelly_payout_b", "kelly_f", "kelly_fraction_cfg", "kelly_multiplier",
        "kelly_size_usd", "row_type", "funding_rate", "yes_depth_share",
        "hour_utc", "effective_z", "funding_gate_applied", "streak_key",
        "twap_dev_bps", "vol_regime",
    ]).to_csv(tmp_dir / "momentum_fills.csv", index=False)

    pd.DataFrame(columns=["ts", "coin", "source", "price"]).to_csv(
        tmp_dir / "oracle_ticks.csv", index=False
    )


# ── AC1: oracle-alignment-with-no-matching-tick ──────────────────────────────

class TestOracleAlignment:
    def test_returns_price_when_within_window(self):
        oracle_df = _make_oracle_df([(0.0, "BTC", 80_000.0)])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price == pytest.approx(80_000.0)

    def test_returns_price_at_edge_of_window(self):
        """Exactly at ORACLE_ALIGN_WINDOW_S should still match."""
        oracle_df = _make_oracle_df([(ORACLE_ALIGN_WINDOW_S, "BTC", 80_001.0)])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price == pytest.approx(80_001.0)

    def test_returns_none_when_no_tick_in_window(self):
        """Row is kept but oracle feature is None — the key AC."""
        oracle_df = _make_oracle_df([(ORACLE_ALIGN_WINDOW_S + 1.0, "BTC", 80_000.0)])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price is None

    def test_returns_none_when_coin_not_in_df(self):
        oracle_df = _make_oracle_df([(0.0, "ETH", 2500.0)])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price is None

    def test_returns_none_for_empty_oracle_df(self):
        empty = pd.DataFrame(columns=["_ts", "coin", "price"])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=empty)
        assert price is None

    def test_returns_none_for_nat_timestamp(self):
        oracle_df = _make_oracle_df([(0.0, "BTC", 80_000.0)])
        price = align_oracle_tick(ts=pd.NaT, coin="BTC", oracle_df=oracle_df)
        assert price is None

    def test_picks_closest_tick_when_multiple_in_window(self):
        oracle_df = _make_oracle_df([(1.0, "BTC", 80_001.0), (4.0, "BTC", 80_004.0)])
        # ts = _ts(0.0), so tick at offset 1.0s is closer
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price == pytest.approx(80_001.0)

    def test_case_insensitive_coin_lookup(self):
        oracle_df = _make_oracle_df([(0.0, "btc", 80_000.0)])
        price = align_oracle_tick(ts=_ts(0.0), coin="BTC", oracle_df=oracle_df)
        assert price == pytest.approx(80_000.0)

    def test_build_with_no_oracle_ticks_keeps_row(self):
        """Full build: no oracle ticks → oracle_delta_pct is None, row still present."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            _make_empty_csvs(tmp_dir)
            _make_trades_csv(tmp_dir, [{
                "timestamp": "2026-05-01T10:05:00+00:00",
                "entry_timestamp": "2026-05-01T10:00:00+00:00",
                "market_id": "mkt1",
                "market_title": "BTC 5m",
                "market_type": "bucket_5m",
                "underlying": "BTC",
                "strategy": "opening_neutral",
                "side": "YES",
                "resolved_outcome": "WIN",
                "exit_reason": "winner_exit",
                "strike": 80_000.0,
                "spot_price": 80_100.0,
                "tte_years": 0.0001,
                "deribit_iv": None,
                "implied_prob": 0.52,
            }])

            df = build(
                output_path=tmp_dir / "out.parquet",
                data_dir=tmp_dir,
                analysis_dir=tmp_dir,
            )
            assert len(df) == 1, "Row must be kept even when oracle tick is absent"
            assert pd.isna(df["oracle_tick_price"].iloc[0])
            assert df["oracle_tick_aligned"].iloc[0] is False or df["oracle_tick_aligned"].iloc[0] == False


# ── AC2: missing-clob-session ──────────────────────────────────────────────────

class TestMissingClobSession:
    def test_null_clob_features_when_no_sessions(self):
        result = _match_clob_session(ts=_ts(0.0), coin="BTC", clob_sessions=[])
        assert result["clob_yes_best_bid"] is None
        assert result["clob_yes_best_ask"] is None
        assert result["clob_yes_spread"] is None
        assert result["clob_yes_bid_depth_5"] is None

    def test_null_clob_features_when_session_too_old(self):
        """Session is 2h away — beyond the 1h tolerance."""
        sess = _make_clob_session("BTC", capture_offset_s=0.0)
        ts_far = _ts(7200.0)  # 2h later
        result = _match_clob_session(ts=ts_far, coin="BTC", clob_sessions=[sess])
        assert result["clob_yes_best_bid"] is None

    def test_returns_clob_features_when_session_matches(self):
        sess = _make_clob_session("BTC", capture_offset_s=0.0)
        result = _match_clob_session(ts=_ts(0.0), coin="BTC", clob_sessions=[sess])
        assert result["clob_yes_best_bid"] == pytest.approx(0.72)
        assert result["clob_yes_bid_depth_5"] == pytest.approx(250.0)

    def test_null_when_coin_not_in_session(self):
        sess = _make_clob_session("ETH", capture_offset_s=0.0)
        result = _match_clob_session(ts=_ts(0.0), coin="BTC", clob_sessions=[sess])
        assert result["clob_yes_best_bid"] is None

    def test_build_with_no_clob_sessions_keeps_row(self):
        """Full build: no CLOB sessions → CLOB features null, row still present."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            _make_empty_csvs(tmp_dir)
            _make_trades_csv(tmp_dir, [{
                "timestamp": "2026-05-01T10:05:00+00:00",
                "entry_timestamp": "2026-05-01T10:00:00+00:00",
                "market_id": "mkt1",
                "market_title": "BTC 5m",
                "market_type": "bucket_5m",
                "underlying": "BTC",
                "strategy": "opening_neutral",
                "side": "YES",
                "resolved_outcome": "WIN",
                "exit_reason": "winner_exit",
                "strike": 80_000.0,
                "spot_price": 80_100.0,
                "tte_years": 0.0001,
            }])
            df = build(
                output_path=tmp_dir / "out.parquet",
                data_dir=tmp_dir,
                analysis_dir=tmp_dir,  # no pm_clob_stream_*.csv here
            )
            assert len(df) == 1
            assert pd.isna(df["clob_yes_best_bid"].iloc[0])


# ── AC3: split-is-chronological ───────────────────────────────────────────────

class TestSplitIsChronological:
    def _make_df(self, n: int) -> pd.DataFrame:
        """DataFrame with n rows ordered chronologically."""
        timestamps = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
        return pd.DataFrame({"entry_timestamp": timestamps})

    def test_split_counts_match_fracs(self):
        n = 100
        df = self._make_df(n)
        split = assign_split(df)
        n_train = (split == "train").sum()
        n_val = (split == "val").sum()
        n_test = (split == "test").sum()
        assert n_train + n_val + n_test == n
        assert n_train == pytest.approx(70, abs=2)
        assert n_val == pytest.approx(15, abs=2)
        assert n_test == pytest.approx(15, abs=2)

    def test_train_comes_before_val_comes_before_test(self):
        """No test row should appear before any train row in the index."""
        n = 100
        df = self._make_df(n)
        split = assign_split(df)

        train_idx = split[split == "train"].index
        val_idx = split[split == "val"].index
        test_idx = split[split == "test"].index

        assert max(train_idx) < min(val_idx), "Train must end before val starts"
        assert max(val_idx) < min(test_idx), "Val must end before test starts"

    def test_no_random_shuffle(self):
        """Two identical DataFrames must produce identical splits."""
        n = 50
        df = self._make_df(n)
        split1 = assign_split(df)
        split2 = assign_split(df.copy())
        pd.testing.assert_series_equal(split1, split2)

    def test_single_row_is_train(self):
        df = self._make_df(1)
        split = assign_split(df)
        assert split.iloc[0] == "train"

    def test_empty_df_returns_empty_series(self):
        df = pd.DataFrame({"entry_timestamp": []})
        split = assign_split(df)
        assert len(split) == 0

    def test_build_split_column_present(self):
        """Full build: 'split' column is in output parquet."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            _make_empty_csvs(tmp_dir)
            # Three trades at different times
            _make_trades_csv(tmp_dir, [
                {
                    "timestamp": "2026-05-01T10:05:00+00:00",
                    "entry_timestamp": f"2026-05-01T{10 + i:02d}:00:00+00:00",
                    "market_id": f"mkt{i}",
                    "market_title": f"Trade {i}",
                    "market_type": "bucket_5m",
                    "underlying": "BTC",
                    "strategy": "momentum",
                    "side": "YES",
                    "resolved_outcome": "WIN",
                    "exit_reason": "winner_exit",
                    "strike": 80000.0,
                    "spot_price": 80100.0,
                    "tte_years": 0.0001,
                }
                for i in range(3)
            ])
            df = build(
                output_path=tmp_dir / "out.parquet",
                data_dir=tmp_dir,
                analysis_dir=tmp_dir,
            )
            assert "split" in df.columns
            assert set(df["split"].unique()).issubset({"train", "val", "test"})


# ── Label derivation ───────────────────────────────────────────────────────────

class TestLabelDerivation:
    @pytest.mark.parametrize("raw,expected", [
        ("WIN", 1), ("win", 1), ("1", 1), ("TRUE", 1),
        ("LOSS", 0), ("LOSE", 0), ("loss", 0), ("0", 0), ("FALSE", 0),
        ("PENDING", None), ("UNKNOWN", None), (None, None), ("", None),
    ])
    def test_resolved_outcome(self, raw, expected):
        assert _derive_resolved_outcome(raw) == expected

    def test_exit_was_wrong_when_win_and_loser_exit(self):
        assert _derive_exit_was_wrong(1, "loser_threshold_exit") == 1

    def test_exit_was_wrong_zero_when_win_and_non_loser_exit(self):
        assert _derive_exit_was_wrong(1, "winner_exit") == 0

    def test_exit_was_wrong_zero_when_loss(self):
        assert _derive_exit_was_wrong(0, "loser_threshold_exit") == 0

    def test_exit_was_wrong_none_when_pending(self):
        assert _derive_exit_was_wrong(None, "loser_threshold_exit") is None

    def test_exit_was_wrong_detects_loser_keyword(self):
        """Any exit_reason containing 'loser' counts as the failure mode."""
        for reason in ["loser", "LOSER_EXIT", "exit_via_loser_threshold"]:
            assert _derive_exit_was_wrong(1, reason) == 1, f"failed for reason={reason!r}"


# ── Utility helpers ────────────────────────────────────────────────────────────

class TestSafeConversions:
    def test_safe_float_normal(self):
        assert _safe_float(1.5) == pytest.approx(1.5)

    def test_safe_float_none(self):
        assert _safe_float(None) is None

    def test_safe_float_nan(self):
        assert _safe_float(float("nan")) is None

    def test_safe_float_string(self):
        assert _safe_float("3.14") == pytest.approx(3.14)

    def test_safe_float_invalid_string(self):
        assert _safe_float("abc") is None

    def test_safe_bool_true(self):
        assert _safe_bool("true") is True
        assert _safe_bool("1") is True

    def test_safe_bool_false(self):
        assert _safe_bool("false") is False
        assert _safe_bool("0") is False

    def test_safe_bool_none(self):
        assert _safe_bool(None) is None
