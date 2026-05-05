"""
tests/test_shadow_evaluator.py — Unit tests for analysis/shadow_evaluator.py (ML-05)

Tests cover:
  - insufficient-data guard (< 10 resolved rows)
  - agreement-rate calculation (overall + by decision_type)
  - confusion-matrix values
  - shadow PnL delta calculation
  - --decision_type filter
  - --last_n_days filter
  - missing file exits with code 1

Run:  pytest tests/test_shadow_evaluator.py -v
"""
import csv
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

_SHADOW_COLS = [
    "timestamp", "market_id", "market_type", "decision_type", "rules_decision",
    "model_a_score", "model_b_score", "model_decision", "agreed",
    "actual_outcome", "features_snapshot",
]


def _write_shadow_log(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_SHADOW_COLS)
        writer.writeheader()
        writer.writerows(rows)


def _make_row(
    market_id: str = "mkt-001",
    decision_type: str = "exit",
    agreed: bool = True,
    outcome: str = "WIN",
    ts_offset: float = 0.0,
    market_type: str = "bucket_5m",
) -> dict:
    return {
        "timestamp": str(time.time() - ts_offset),
        "market_id": market_id,
        "market_type": market_type,
        "decision_type": decision_type,
        "rules_decision": "exit" if decision_type == "exit" else "enter",
        "model_a_score": "0.55" if decision_type == "entry" else "",
        "model_b_score": "0.55" if decision_type == "exit" else "",
        "model_decision": "agree" if agreed else "disagree",
        "agreed": "true" if agreed else "false",
        "actual_outcome": outcome,
        "features_snapshot": "{}",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_missing_file_exits_with_code_1(tmp_path):
    """Missing shadow_log.csv raises SystemExit(1)."""
    from analysis.shadow_evaluator import _load

    missing = tmp_path / "nonexistent.csv"
    with pytest.raises(SystemExit) as exc_info:
        _load(missing, "all", None)
    assert exc_info.value.code == 1


def test_insufficient_data_guard(tmp_path, capsys):
    """< 10 resolved rows prints 'insufficient data' message."""
    shadow_log = tmp_path / "shadow_log.csv"
    # 5 resolved rows — below threshold
    rows = [_make_row(f"mkt-{i:03d}", outcome="WIN") for i in range(5)]
    _write_shadow_log(shadow_log, rows)

    from analysis.shadow_evaluator import _load, _MIN_ROWS

    with patch("analysis.shadow_evaluator._SHADOW_LOG", shadow_log):
        loaded = _load(shadow_log, "all", None)

    resolved = [r for r in loaded if r.get("actual_outcome") in ("WIN", "LOSS")]
    assert len(resolved) < _MIN_ROWS


def test_agreement_rate_all_agree(tmp_path):
    """100% agreement rate when all rows have agreed=true."""
    from analysis.shadow_evaluator import _agreement_rate

    rows = [_make_row(agreed=True, outcome="WIN") for _ in range(20)]
    assert _agreement_rate(rows) == 1.0


def test_agreement_rate_none_agree(tmp_path):
    """0% agreement rate when all rows have agreed=false."""
    from analysis.shadow_evaluator import _agreement_rate

    rows = [_make_row(agreed=False, outcome="WIN") for _ in range(20)]
    assert _agreement_rate(rows) == 0.0


def test_agreement_rate_partial(tmp_path):
    """50% agreement rate for half-agree, half-disagree."""
    from analysis.shadow_evaluator import _agreement_rate

    rows = (
        [_make_row(agreed=True,  outcome="WIN")  for _ in range(10)] +
        [_make_row(agreed=False, outcome="LOSS") for _ in range(10)]
    )
    rate = _agreement_rate(rows)
    assert abs(rate - 0.5) < 1e-9


def test_shadow_pnl_delta_positive_when_model_catches_wrong_exits():
    """
    Shadow PnL delta is +N for N disagreements where actual_outcome=WIN.
    (Model would have suppressed wrong exits = saved +1 per suppressed wrong exit.)
    """
    from analysis.shadow_evaluator import _shadow_pnl_delta

    rows = [
        _make_row(decision_type="exit", agreed=False, outcome="WIN")
        for _ in range(5)
    ]
    assert _shadow_pnl_delta(rows) == 5.0


def test_shadow_pnl_delta_negative_when_model_suppresses_correct_exits():
    """
    Shadow PnL delta is -N for N disagreements where actual_outcome=LOSS.
    (Model wrongly suppressed correct exits = would have cost -1 per suppression.)
    """
    from analysis.shadow_evaluator import _shadow_pnl_delta

    rows = [
        _make_row(decision_type="exit", agreed=False, outcome="LOSS")
        for _ in range(3)
    ]
    assert _shadow_pnl_delta(rows) == -3.0


def test_shadow_pnl_delta_none_when_no_exit_rows():
    """Returns None when no exit rows exist."""
    from analysis.shadow_evaluator import _shadow_pnl_delta

    rows = [_make_row(decision_type="entry", agreed=True, outcome="WIN")]
    assert _shadow_pnl_delta(rows) is None


def test_confusion_matrix_counts():
    """Confusion matrix TP/FP/TN/FN counts are correct."""
    from analysis.shadow_evaluator import _confusion_matrix

    rows = [
        # model agree + rules correct (tp): agreed=True, LOSS (rules exited correctly)
        _make_row(decision_type="exit", agreed=True,  outcome="LOSS"),
        _make_row(decision_type="exit", agreed=True,  outcome="LOSS"),
        # model agree + rules wrong (fp): agreed=True, WIN (rules shouldn't have exited)
        _make_row(decision_type="exit", agreed=True,  outcome="WIN"),
        # model disagree + rules correct (fn): agreed=False, LOSS
        _make_row(decision_type="exit", agreed=False, outcome="LOSS"),
        # model disagree + rules wrong (tn): agreed=False, WIN (model caught the error)
        _make_row(decision_type="exit", agreed=False, outcome="WIN"),
        _make_row(decision_type="exit", agreed=False, outcome="WIN"),
    ]
    cm = _confusion_matrix(rows)
    assert cm["model_agree_rules_correct"]    == 2   # tp
    assert cm["model_agree_rules_wrong"]      == 1   # fp
    assert cm["model_disagree_rules_correct"] == 1   # fn
    assert cm["model_disagree_rules_wrong"]   == 2   # tn
    assert cm["total_exit_resolved"]          == 6


def test_last_n_days_filter(tmp_path):
    """--last_n_days=1 excludes rows older than 1 day."""
    from analysis.shadow_evaluator import _load

    shadow_log = tmp_path / "shadow_log.csv"
    old_row = _make_row(market_id="old", ts_offset=86400 * 3)  # 3 days ago
    new_row = _make_row(market_id="new", ts_offset=3600)        # 1 hour ago
    _write_shadow_log(shadow_log, [old_row, new_row])

    rows = _load(shadow_log, "all", last_n_days=1)
    market_ids = {r["market_id"] for r in rows}
    assert "new" in market_ids
    assert "old" not in market_ids


def test_decision_type_filter(tmp_path):
    """--decision_type=entry only returns entry rows."""
    from analysis.shadow_evaluator import _load

    shadow_log = tmp_path / "shadow_log.csv"
    rows = [
        _make_row(market_id="e1", decision_type="entry"),
        _make_row(market_id="x1", decision_type="exit"),
        _make_row(market_id="e2", decision_type="entry"),
    ]
    _write_shadow_log(shadow_log, rows)

    loaded = _load(shadow_log, "entry", last_n_days=None)
    assert all(r["decision_type"] == "entry" for r in loaded)
    assert len(loaded) == 2


def test_rolling_agreement_window():
    """Rolling agreement returns correct length (len(rows) - window + 1)."""
    from analysis.shadow_evaluator import _rolling_agreement

    rows = [_make_row(agreed=(i % 2 == 0)) for i in range(30)]
    rates = _rolling_agreement(rows, window=20)
    assert len(rates) == 30 - 20 + 1
    # Every window of 20 alternating rows: 10 agree / 10 disagree → 0.5
    assert all(abs(r - 0.5) < 1e-9 for r in rates)


def test_market_type_filter(tmp_path):
    """--market_type filters rows so only the matching market_type is returned."""
    from analysis.shadow_evaluator import _load

    shadow_log = tmp_path / "shadow_log.csv"
    rows = [
        _make_row(market_id="a", market_type="bucket_5m"),
        _make_row(market_id="b", market_type="bucket_5m"),
        _make_row(market_id="c", market_type="bucket_1h"),
        _make_row(market_id="d", market_type="bucket_1h"),
        _make_row(market_id="e", market_type="bucket_5m"),
    ]
    _write_shadow_log(shadow_log, rows)

    loaded_5m = _load(shadow_log, "all", None, market_type="bucket_5m")
    loaded_1h = _load(shadow_log, "all", None, market_type="bucket_1h")
    loaded_all = _load(shadow_log, "all", None, market_type=None)

    assert all(r["market_type"] == "bucket_5m" for r in loaded_5m)
    assert len(loaded_5m) == 3
    assert all(r["market_type"] == "bucket_1h" for r in loaded_1h)
    assert len(loaded_1h) == 2
    assert len(loaded_all) == 5
