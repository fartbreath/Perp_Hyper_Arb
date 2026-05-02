"""
conftest.py — Shared pytest fixtures.

Redirects all mutable data-file paths to per-test temp files so the real
data/ directory is never polluted by the test suite.

Files isolated (module-level constants → tmp_path):
  risk.TRADES_CSV                              → trades.csv
  risk.OPEN_POSITIONS_JSON                     → open_positions.json
  risk.HEDGE_ORDERS_JSON                       → hedge_orders.json
  risk.PAPER_HEDGE_FILLS_JSON                  → paper_hedge_fills.json
  strategies.Momentum.scanner.MOMENTUM_FILLS_CSV → momentum_fills.csv
  strategies.Momentum.event_log.MOMENTUM_EVENTS_PATH → momentum_events.jsonl
  fill_simulator.FILLS_CSV                     → fills.csv
  monitor._PENDING_RESOLUTIONS_PATH            → pending_resolutions.json
  monitor.HEDGE_CLOB_TICKS_CSV                 → hedge_clob_ticks.csv
  logger file handlers                         → bot.log / errors.log
"""
import logging
import logging.handlers as _log_handlers
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
import fill_simulator as _fill_sim_mod
import monitor as _monitor_mod
import risk
import strategies.Momentum.event_log as _event_log_mod
import strategies.Momentum.scanner as _scanner_mod


@pytest.fixture(autouse=True)
def _isolate_momentum_fills_csv(tmp_path):
    """Patch scanner.MOMENTUM_FILLS_CSV to a per-test temp file."""
    original = _scanner_mod.MOMENTUM_FILLS_CSV
    _scanner_mod.MOMENTUM_FILLS_CSV = tmp_path / "momentum_fills.csv"
    yield
    _scanner_mod.MOMENTUM_FILLS_CSV = original


@pytest.fixture(autouse=True)
def _isolate_trades_csv(tmp_path):
    """Patch risk.TRADES_CSV to a per-test temp file."""
    original = risk.TRADES_CSV
    risk.TRADES_CSV = tmp_path / "trades.csv"
    yield
    risk.TRADES_CSV = original


@pytest.fixture(autouse=True)
def _isolate_open_positions(tmp_path):
    """Patch risk.OPEN_POSITIONS_JSON to a per-test temp file.

    Prevents test token IDs (tid_yes_001, tok_yes, etc.) from being written
    into the live data/open_positions.json during pytest runs.
    """
    original = risk.OPEN_POSITIONS_JSON
    risk.OPEN_POSITIONS_JSON = tmp_path / "open_positions.json"
    yield
    risk.OPEN_POSITIONS_JSON = original


@pytest.fixture(autouse=True)
def _isolate_hedge_orders(tmp_path):
    """Patch risk.HEDGE_ORDERS_JSON to a per-test temp file."""
    original = risk.HEDGE_ORDERS_JSON
    risk.HEDGE_ORDERS_JSON = tmp_path / "hedge_orders.json"
    yield
    risk.HEDGE_ORDERS_JSON = original


@pytest.fixture(autouse=True)
def _isolate_paper_hedge_fills(tmp_path):
    """Patch risk.PAPER_HEDGE_FILLS_JSON to a per-test temp file."""
    original = risk.PAPER_HEDGE_FILLS_JSON
    risk.PAPER_HEDGE_FILLS_JSON = tmp_path / "paper_hedge_fills.json"
    yield
    risk.PAPER_HEDGE_FILLS_JSON = original


@pytest.fixture(autouse=True)
def _isolate_momentum_events(tmp_path):
    """Patch event_log.MOMENTUM_EVENTS_PATH to a per-test temp file."""
    original = _event_log_mod.MOMENTUM_EVENTS_PATH
    _event_log_mod.MOMENTUM_EVENTS_PATH = tmp_path / "momentum_events.jsonl"
    yield
    _event_log_mod.MOMENTUM_EVENTS_PATH = original


@pytest.fixture(autouse=True)
def _isolate_fills_csv(tmp_path):
    """Patch fill_simulator.FILLS_CSV to a per-test temp file."""
    original = _fill_sim_mod.FILLS_CSV
    _fill_sim_mod.FILLS_CSV = tmp_path / "fills.csv"
    yield
    _fill_sim_mod.FILLS_CSV = original


@pytest.fixture(autouse=True)
def _isolate_pending_resolutions(tmp_path):
    """Patch monitor._PENDING_RESOLUTIONS_PATH to a per-test temp file."""
    original = _monitor_mod._PENDING_RESOLUTIONS_PATH
    _monitor_mod._PENDING_RESOLUTIONS_PATH = tmp_path / "pending_resolutions.json"
    yield
    _monitor_mod._PENDING_RESOLUTIONS_PATH = original


@pytest.fixture(autouse=True)
def _isolate_log_files(tmp_path):
    """Redirect rotating file handlers (bot.log, errors.log) to tmp_path.

    _setup_root_logger() runs at import time and attaches RotatingFileHandlers
    to the root logger.  Simply patching module-level LOG_FILE / ERRORS_FILE
    constants has no effect on already-attached handlers, so we must reach into
    the handler objects and swap out the underlying file path.
    """
    root = logging.getLogger()
    redirected: dict = {}
    for h in root.handlers:
        if not isinstance(h, _log_handlers.RotatingFileHandler):
            continue
        fname = Path(h.baseFilename).name
        if fname not in ("bot.log", "errors.log"):
            continue
        h.close()
        redirected[h] = h.baseFilename
        h.baseFilename = str(tmp_path / fname)
        h.stream = h._open()
    yield
    for h, orig_path in redirected.items():
        h.close()
        h.baseFilename = orig_path
        h.stream = h._open()


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
