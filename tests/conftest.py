"""
conftest.py — Shared pytest fixtures.

Redirects risk.TRADES_CSV to a temp file for every test so the real
data/trades.csv is never polluted by the test suite.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
import risk


@pytest.fixture(autouse=True)
def _isolate_trades_csv(tmp_path):
    """Patch risk.TRADES_CSV to a per-test temp file."""
    original = risk.TRADES_CSV
    risk.TRADES_CSV = tmp_path / "trades.csv"
    yield
    risk.TRADES_CSV = original


@pytest.fixture(autouse=True)
def _reset_score_thresholds():
    """Reset signal score thresholds to 0 for every test.

    Live config_overrides.json may have non-zero thresholds (e.g. 80) set by
    the operator. Tests that check deployment mechanics, repricing, etc. should
    not be gated by the score filter — they need to be isolated from user config.
    Tests that explicitly test score filtering can set config values directly.
    """
    orig_maker = config.MIN_SIGNAL_SCORE_MAKER
    orig_mispricing = config.MIN_SIGNAL_SCORE_MISPRICING
    config.MIN_SIGNAL_SCORE_MAKER = 0.0
    config.MIN_SIGNAL_SCORE_MISPRICING = 0.0
    yield
    config.MIN_SIGNAL_SCORE_MAKER = orig_maker
    config.MIN_SIGNAL_SCORE_MISPRICING = orig_mispricing
