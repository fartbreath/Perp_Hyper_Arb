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


@pytest.fixture(autouse=True)
def _reset_production_limits():
    """Reset position-size / exposure / capital config to test-safe defaults.

    config_overrides.json carries tight production limits (e.g. $40 total PM
    exposure, $30 paper capital, 30-contract spread cap) that are correct for
    live operation but break any test that exercises the risk engine or maker
    strategy size logic with realistic, larger numbers.

    This fixture saves and restores the affected values so tests run against
    the code's *documented* defaults and individual tests can still override
    specific values as needed.
    """
    _keys = (
        "MAX_PM_EXPOSURE_PER_MARKET",
        "MAX_TOTAL_PM_EXPOSURE",
        "MAX_CONCURRENT_POSITIONS",
        "MAX_CONCURRENT_MAKER_POSITIONS",
        "MAX_CONCURRENT_MISPRICING_POSITIONS",
        "MAX_MAKER_POSITIONS_PER_UNDERLYING",
        "PAPER_CAPITAL_USD",
        "MAKER_SPREAD_SIZE_MAX",
        "MAKER_SPREAD_SIZE_MIN",
        "MAKER_SPREAD_SIZE_NEW_MARKET",
        # Prevents test-order config leaks: config_overrides.json sets this to
        # False for live trading, which causes tests relying on paper mode to
        # break when run in isolation (get_token_balance is awaited in live mode).
        "PAPER_TRADING",
    )
    saved = {k: getattr(config, k) for k in _keys}

    # Test-safe defaults — generous enough for unit / integration tests while
    # still being finite so limit-enforcement tests can set lower values.
    config.MAX_PM_EXPOSURE_PER_MARKET     = 500.0
    config.MAX_TOTAL_PM_EXPOSURE          = 2000.0
    config.MAX_CONCURRENT_POSITIONS       = 12
    config.MAX_CONCURRENT_MAKER_POSITIONS = 8
    config.MAX_CONCURRENT_MISPRICING_POSITIONS = 3
    config.MAX_MAKER_POSITIONS_PER_UNDERLYING  = 3
    config.PAPER_CAPITAL_USD              = 10_000.0
    config.MAKER_SPREAD_SIZE_MAX          = 500.0   # must equal MAX_PM_EXPOSURE_PER_MARKET
    config.MAKER_SPREAD_SIZE_MIN          = 125.0
    config.MAKER_SPREAD_SIZE_NEW_MARKET   = 100.0
    config.PAPER_TRADING                  = True  # tests operate in paper mode by default

    yield

    for k, v in saved.items():
        setattr(config, k, v)
