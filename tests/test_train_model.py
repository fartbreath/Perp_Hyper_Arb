"""
tests/test_train_model.py — Unit tests for analysis/train_model.py (ML-03)

Focuses on the logic that doesn't require xgboost installed:
  - Data preparation (_prepare_features, _split_by_column)
  - Leakage guard (_check_leakage)
  - Metrics computation
  - Model A row-count gate
  - Model B AUC gate (exit code)
  - SHAP report generation skipped gracefully when disabled

Tests that require xgboost are marked with @pytest.mark.skipif to avoid
breaking CI when the package is not yet installed.

Run:  pytest tests/test_train_model.py -v
"""
from __future__ import annotations

import pickle
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.train_model import (
    MODEL_A_MIN_ROWS,
    MODEL_B_MIN_AUC,
    LEAKAGE_CORR_THRESHOLD,
    MODEL_A_FEATURES,
    MODEL_B_FEATURES,
    _prepare_features,
    _split_by_column,
    _check_leakage,
)

from analysis.feature_builder import assign_split as assign_split_for_test

_XGBOOST_AVAILABLE = False
try:
    import xgboost  # noqa: F401
    _XGBOOST_AVAILABLE = True
except ImportError:
    pass

_SKLEARN_AVAILABLE = False
try:
    import sklearn  # noqa: F401
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_parquet(
    tmp_dir: Path,
    n: int = 50,
    n_positive: int = 25,
    include_exit_was_wrong: bool = True,
) -> Path:
    """Build a minimal synthetic parquet in tmp_dir and return its path."""
    rng = np.random.default_rng(42)

    rows = []
    base = pd.Timestamp("2026-01-01", tz="UTC")
    for i in range(n):
        win = 1 if i < n_positive else 0
        rows.append({
            "market_id": f"mkt{i}",
            "entry_timestamp": base + pd.Timedelta(hours=i),
            "exit_timestamp": base + pd.Timedelta(hours=i, minutes=5),
            "market_type": "bucket_5m",
            "underlying": "BTC",
            "strategy": "opening_neutral",
            "side": "YES",
            # Feature columns
            "oracle_delta_pct": rng.uniform(-0.05, 0.05),
            "deribit_iv": rng.uniform(0.3, 1.2),
            "implied_prob": rng.uniform(0.3, 0.7),
            "on_yes_depth_share": rng.uniform(0.3, 0.7),
            "on_loser_confidence_score": rng.integers(0, 5),
            "on_loser_fill_price": rng.uniform(0.2, 0.5),
            "on_loser_fill_time_secs": rng.uniform(10, 60),
            "tte_seconds_at_entry": rng.uniform(30, 300),
            "hour_utc": int(i % 24),
            "on_funding_rate": rng.uniform(-0.001, 0.001),
            "on_combined_cost": rng.uniform(0.98, 1.05),
            "clob_yes_best_bid": rng.uniform(0.3, 0.7),
            "clob_yes_bid_depth_5": rng.uniform(50, 500),
            "mom_z_score": rng.uniform(0.5, 2.0),
            "mom_effective_z": rng.uniform(0.5, 2.0),
            "mom_sigma_ann": rng.uniform(0.3, 1.5),
            "mom_tte_seconds": rng.uniform(30, 300),
            "mom_funding_rate": rng.uniform(-0.001, 0.001),
            "mom_yes_depth_share": rng.uniform(0.3, 0.7),
            "mom_kelly_f": rng.uniform(0.01, 0.2),
            "mom_kelly_win_prob": rng.uniform(0.5, 0.9),
            "mom_kelly_multiplier": rng.uniform(0.3, 1.5),
            "mom_kelly_size_usd": rng.uniform(1, 20),
            "mom_twap_dev_bps": rng.uniform(0, 100),
            "mom_vol_regime": "HIGH" if rng.random() > 0.5 else "LOW",
            "mom_signal_delta_pct": rng.uniform(0.03, 0.15),
            "mom_funding_gate_applied": bool(rng.random() > 0.5),
            "day_of_week": int(i % 7),
            # Labels
            "resolved_outcome": win,
            "exit_was_wrong": (1 if win == 1 and rng.random() > 0.7 else 0)
                if include_exit_was_wrong else np.nan,
            "exit_reason": "loser_exit" if win == 1 else "winner_exit",
            # Split
            "split": "train" if i < int(n * 0.7) else ("val" if i < int(n * 0.85) else "test"),
        })

    df = pd.DataFrame(rows)
    path = tmp_dir / "training_data.parquet"
    df.to_parquet(path, index=False)
    return path


# ── _prepare_features ─────────────────────────────────────────────────────────

class TestPrepareFeatures:
    def test_drops_rows_where_label_is_null(self):
        df = pd.DataFrame({
            "feat_a": [1.0, 2.0, 3.0],
            "my_label": [1, None, 0],
        })
        X, y = _prepare_features(df, ["feat_a"], "my_label")
        assert len(X) == 2
        assert len(y) == 2

    def test_fills_nan_with_sentinel(self):
        df = pd.DataFrame({
            "feat_a": [1.0, np.nan, 3.0],
            "my_label": [1, 1, 0],
        })
        X, y = _prepare_features(df, ["feat_a"], "my_label")
        assert X["feat_a"].iloc[1] == pytest.approx(-999.0)

    def test_adds_missing_columns_as_sentinel(self):
        """Feature column not in df → filled with -999."""
        df = pd.DataFrame({
            "feat_a": [1.0, 2.0],
            "my_label": [1, 0],
        })
        X, y = _prepare_features(df, ["feat_a", "feat_b_missing"], "my_label")
        assert "feat_b_missing" in X.columns
        assert (X["feat_b_missing"] == -999.0).all()

    def test_derives_vol_regime_high(self):
        df = pd.DataFrame({
            "mom_vol_regime": ["HIGH", "LOW", "HIGH"],
            "my_label": [1, 0, 1],
        })
        X, y = _prepare_features(df, ["vol_regime_high"], "my_label")
        assert X["vol_regime_high"].tolist() == pytest.approx([1.0, 0.0, 1.0])

    def test_empty_label_column_returns_empty(self):
        df = pd.DataFrame({
            "feat_a": [1.0, 2.0],
            "my_label": [None, None],
        })
        X, y = _prepare_features(df, ["feat_a"], "my_label")
        assert len(X) == 0


# ── _split_by_column ──────────────────────────────────────────────────────────

class TestSplitByColumn:
    def _make_xy(self, n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
        fracs = (0.70, 0.15, 0.15)
        split_vals = (
            ["train"] * int(n * fracs[0]) +
            ["val"] * int(n * fracs[1]) +
            ["test"] * (n - int(n * fracs[0]) - int(n * fracs[1]))
        )
        df = pd.DataFrame({"split": split_vals})
        X = pd.DataFrame({"f": range(n)})
        y = pd.Series([i % 2 for i in range(n)])
        return df, X, y

    def test_produces_three_splits(self):
        df, X, y = self._make_xy(20)
        X_tr, y_tr, X_va, y_va, X_te, y_te = _split_by_column(df, X, y)
        assert len(X_tr) > 0
        assert len(X_va) >= 0
        assert len(X_te) >= 0
        assert len(X_tr) + len(X_va) + len(X_te) == len(y)

    def test_train_indices_precede_val_precede_test(self):
        df, X, y = self._make_xy(20)
        X_tr, y_tr, X_va, y_va, X_te, y_te = _split_by_column(df, X, y)
        if len(X_va) > 0 and len(X_te) > 0:
            assert max(X_tr.index) < min(X_va.index)
            assert max(X_va.index) < min(X_te.index)

    def test_falls_back_when_split_column_all_train(self):
        """If all rows are 'train', fall back to simple chronological split."""
        n = 10
        df = pd.DataFrame({"split": ["train"] * n})
        X = pd.DataFrame({"f": range(n)})
        y = pd.Series([0] * n)
        X_tr, y_tr, X_va, y_va, X_te, y_te = _split_by_column(df, X, y)
        assert len(X_tr) + len(X_va) + len(X_te) == n


# ── _check_leakage ────────────────────────────────────────────────────────────

class TestLeakageGuard:
    def test_no_exit_when_no_high_correlation(self):
        """Low-correlation features must not trigger an exit."""
        rng = np.random.default_rng(99)
        n = 20
        X = pd.DataFrame({"f1": rng.uniform(0, 1, n), "f2": rng.uniform(0, 1, n)})
        y = pd.Series(rng.integers(0, 2, n))
        # Should not raise SystemExit
        _check_leakage(X, y)

    def test_exits_when_feature_perfectly_correlated_with_label(self):
        """Feature = label → Pearson r = 1.0 → SystemExit(1)."""
        n = 20
        y = pd.Series([0, 1] * (n // 2))
        X = pd.DataFrame({"leaky": y.astype(float)})
        with pytest.raises(SystemExit) as exc_info:
            _check_leakage(X, y)
        assert exc_info.value.code == 1

    def test_skipped_when_fewer_than_10_rows(self):
        """Small test set → leakage check is bypassed (not enough signal)."""
        y = pd.Series([0, 1, 0, 1, 0])
        X = pd.DataFrame({"leaky": y.astype(float)})  # would be leaky but too few rows
        _check_leakage(X, y)  # must not raise


# ── Full pipeline smoke tests (xgboost required) ──────────────────────────────

@pytest.mark.skipif(not _XGBOOST_AVAILABLE, reason="xgboost not installed")
class TestTrainSmoke:
    def test_model_b_trains_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            parquet_path = _make_parquet(tmp_dir, n=80, n_positive=40)

            from analysis.train_model import train

            train(
                parquet_path=parquet_path,
                model_b_out=tmp_dir / "model_b_v0.pkl",
                model_a_out=tmp_dir / "model_a_v0.pkl",
                reports_dir=tmp_dir / "reports",
                generate_shap=False,
            )

            assert (tmp_dir / "model_b_v0.pkl").exists()
            with open(tmp_dir / "model_b_v0.pkl", "rb") as f:
                model_b = pickle.load(f)
            assert hasattr(model_b, "predict_proba")

    def test_model_a_skipped_when_below_min_rows(self, capsys):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            # Only 50 rows — below MODEL_A_MIN_ROWS=300
            parquet_path = _make_parquet(tmp_dir, n=50, n_positive=25)

            from analysis.train_model import train

            train(
                parquet_path=parquet_path,
                model_b_out=tmp_dir / "model_b_v0.pkl",
                model_a_out=tmp_dir / "model_a_v0.pkl",
                reports_dir=tmp_dir / "reports",
                generate_shap=False,
            )

            # Model A pkl should NOT exist (skipped)
            assert not (tmp_dir / "model_a_v0.pkl").exists()
            captured = capsys.readouterr()
            assert "skipped" in captured.out.lower()

    @pytest.mark.skipif(not _SKLEARN_AVAILABLE, reason="scikit-learn not installed")
    def test_shap_report_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            parquet_path = _make_parquet(tmp_dir, n=80, n_positive=40)

            from analysis.train_model import train

            train(
                parquet_path=parquet_path,
                model_b_out=tmp_dir / "model_b_v0.pkl",
                model_a_out=tmp_dir / "model_a_v0.pkl",
                reports_dir=tmp_dir / "reports",
                generate_shap=True,
            )

            report = tmp_dir / "reports" / "model_b_v0_shap.html"
            assert report.exists()
            content = report.read_text(encoding="utf-8")
            assert "SHAP" in content

    def test_auc_below_floor_exits_nonzero(self):
        """With random noise data, AUC ≈ 0.5 — train should raise SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            rng = np.random.default_rng(1)
            n = 60
            base = pd.Timestamp("2026-01-01", tz="UTC")
            rows = []
            for i in range(n):
                rows.append({
                    "market_id": f"m{i}",
                    "entry_timestamp": base + pd.Timedelta(hours=i),
                    "split": "train" if i < 42 else ("val" if i < 51 else "test"),
                    "oracle_delta_pct": rng.uniform(-0.5, 0.5),  # pure noise
                    "deribit_iv": rng.uniform(0.1, 2.0),
                    "implied_prob": rng.uniform(0.1, 0.9),
                    "on_yes_depth_share": rng.uniform(0.1, 0.9),
                    "on_loser_confidence_score": rng.integers(0, 10),
                    "on_loser_fill_price": rng.uniform(0.1, 0.9),
                    "on_loser_fill_time_secs": rng.uniform(1, 120),
                    "tte_seconds_at_entry": rng.uniform(10, 600),
                    "hour_utc": int(i % 24),
                    "on_funding_rate": rng.uniform(-0.01, 0.01),
                    "on_combined_cost": rng.uniform(0.9, 1.1),
                    "clob_yes_best_bid": rng.uniform(0.1, 0.9),
                    "clob_yes_bid_depth_5": rng.uniform(10, 1000),
                    # Labels: random → model can't learn anything → AUC ≈ 0.5
                    "resolved_outcome": int(rng.random() > 0.5),
                    "exit_was_wrong": int(rng.random() > 0.5),
                })
            df = pd.DataFrame(rows)
            path = tmp_dir / "training_data.parquet"
            df.to_parquet(path, index=False)

            from analysis.train_model import train

            # AUC on random labels will be near 0.5, below MODEL_B_MIN_AUC=0.60
            # BUT: with only 9 test rows, roc_auc_score might not be exactly 0.5
            # We override the threshold for this test to guarantee failure detection
            with patch("analysis.train_model.MODEL_B_MIN_AUC", 0.99):
                with pytest.raises(SystemExit) as exc_info:
                    train(
                        parquet_path=path,
                        model_b_out=tmp_dir / "model_b.pkl",
                        model_a_out=tmp_dir / "model_a.pkl",
                        reports_dir=tmp_dir / "reports",
                        generate_shap=False,
                    )
                assert exc_info.value.code == 1
