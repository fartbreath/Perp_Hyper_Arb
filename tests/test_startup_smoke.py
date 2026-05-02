"""
tests/test_startup_smoke.py — Startup smoke tests.

Verifies the bot can start in paper mode without AttributeError or ImportError,
and that each strategy scanner can run a full scan pass without crashing.

Test categories
───────────────
  A. Config attribute audit  (static)
       Scan every production .py file for  config.UPPERCASE_ATTR  references
       and assert each one exists in config.py.  This test would have caught
       the regression that caused:
           AttributeError: module 'config' has no attribute 'MOMENTUM_HEDGE_ENABLED'

  B. API server smoke  (offline, TestClient)
       GET /health, /config, /pnl must all return HTTP 200 without raising
       AttributeError.  This directly reproduces the production crash at
       api_server.py::get_config line 1001.

  C. FillSimulator sweep smoke  (offline, mocked deps)
       Instantiate FillSimulator in paper mode and call _sweep() with no
       active quotes.  Confirms the sweep loop doesn't crash (previously
       failed every 5 s with config.MOMENTUM_HEDGE_ENABLED).

  D. MomentumScanner scan smoke  (offline, mocked deps)
       Instantiate MomentumScanner and run _scan_once() over an empty market
       list.  Confirms the scan loop initialises and executes one full pass.

  E. MispricingScanner scan smoke  (offline, mocked deps)
       Instantiate MispricingScanner and run _scan_once() over an empty
       market list.

  F. OpeningNeutralScanner scan smoke  (offline, mocked deps)
       Instantiate OpeningNeutralScanner and run _refresh_pending_markets()
       with an empty market list.

Run:
    pytest tests/test_startup_smoke.py -v
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config

# Force paper mode so no real orders can ever be placed
config.PAPER_TRADING = True
config.STRATEGY_MOMENTUM_ENABLED = True
config.STRATEGY_MAKER_ENABLED = True
config.OPENING_NEUTRAL_ENABLED = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent

# Production Python files to audit (excludes tests, config.py itself, scripts)
_AUDIT_DIRS = [
    _REPO_ROOT,                          # root-level modules
    _REPO_ROOT / "strategies",
    _REPO_ROOT / "market_data",
]
_AUDIT_EXCLUDES = {
    "config.py",    # source of truth — definitions, not usages
    "conftest.py",  # test infrastructure
}
_AUDIT_EXCLUDE_DIRS = {"tests", "__pycache__", ".preamble"}

# Pattern: config.UPPERCASE_ATTR  (conventional constant names only)
_CONFIG_REF_RE = re.compile(r"\bconfig\.([A-Z][A-Z0-9_]+)\b")

# Lines that are safe even if the attribute is absent
_SAFE_LINE_RE = re.compile(
    r"""
    getattr\s*\(\s*config   # getattr(config, ...) has a fallback default
    | \bconfig\.[A-Z][A-Z0-9_]+\s*=  # assignment in test setup
    | #.*config\.          # inside a comment
    """,
    re.VERBOSE,
)


def _collect_config_refs() -> dict[str, list[tuple[str, int]]]:
    """
    Scan production code for  config.ATTR  references.

    Returns dict: attr_name -> [(rel_path, lineno), ...]
    """
    refs: dict[str, list[tuple[str, int]]] = {}

    for base_dir in _AUDIT_DIRS:
        for py_file in base_dir.rglob("*.py"):
            # Skip excluded directories
            if any(part in _AUDIT_EXCLUDE_DIRS for part in py_file.parts):
                continue
            # Skip excluded filenames
            if py_file.name in _AUDIT_EXCLUDES:
                continue
            # Skip test files inside any sub-dir
            if py_file.name.startswith("test_"):
                continue

            try:
                lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, start=1):
                # Skip lines that are safe (getattr with default, assignment, comment)
                if _SAFE_LINE_RE.search(line):
                    continue
                for m in _CONFIG_REF_RE.finditer(line):
                    attr = m.group(1)
                    rel = str(py_file.relative_to(_REPO_ROOT))
                    refs.setdefault(attr, []).append((rel, lineno))

    return refs


def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _noop_create_task(coro, *args, **kwargs):
    """Patch target for asyncio.create_task that closes the coroutine immediately."""
    coro.close()
    return MagicMock()


# ─────────────────────────────────────────────────────────────────────────────
# A. Config attribute audit
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigAudit:
    """
    Every config.ATTR reference in production code must exist in config.py.

    If any attribute is missing this test fails with a clear list of:
        MISSING_ATTR  (file:lineno)
    so the developer sees exactly which files to fix.
    """

    def test_all_referenced_config_attrs_exist(self):
        refs = _collect_config_refs()
        missing: dict[str, list[tuple[str, int]]] = {}

        for attr, locations in refs.items():
            if not hasattr(config, attr):
                missing[attr] = locations

        if missing:
            lines = ["Config attribute audit FAILED — attributes referenced in code but absent from config.py:"]
            for attr, locs in sorted(missing.items()):
                loc_str = ", ".join(f"{f}:{ln}" for f, ln in locs[:5])
                lines.append(f"  config.{attr}  ← referenced at {loc_str}")
            pytest.fail("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# B. API server smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestApiServerSmoke:
    """
    Each critical GET endpoint must return HTTP 200 without raising
    AttributeError.  These tests directly reproduce the production crash:

        File "api_server.py", line 1001, in get_config
            "momentum_hedge_enabled": config.MOMENTUM_HEDGE_ENABLED,
        AttributeError: module 'config' has no attribute 'MOMENTUM_HEDGE_ENABLED'
    """

    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        import api_server
        self.client = TestClient(api_server.app, raise_server_exceptions=True)

    def test_health_returns_200(self):
        r = self.client.get("/health")
        assert r.status_code == 200, f"/health returned {r.status_code}: {r.text}"

    def test_config_returns_200(self):
        """GET /config must not raise AttributeError for any config field."""
        r = self.client.get("/config")
        assert r.status_code == 200, f"/config returned {r.status_code}: {r.text}"

    def test_pnl_returns_200(self):
        """GET /pnl must not crash even when trades CSV is empty."""
        with patch("api_server._load_acct_ledger_trades", return_value=[]):
            r = self.client.get("/pnl")
        assert r.status_code == 200, f"/pnl returned {r.status_code}: {r.text}"

    def test_config_response_is_dict(self):
        r = self.client.get("/config")
        data = r.json()
        assert isinstance(data, dict)
        assert "paper_trading" in data


# ─────────────────────────────────────────────────────────────────────────────
# C. FillSimulator sweep smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestFillSimulatorSmoke:
    """
    FillSimulator._sweep() must complete without error in paper mode with no
    active quotes.  This previously crashed every 5 s with:
        FillSimulator sweep error  exc="module 'config' has no attribute 'MOMENTUM_HEDGE_ENABLED'"
    """

    def _make_simulator(self):
        from fill_simulator import FillSimulator
        from risk import RiskEngine

        pm = MagicMock()
        pm._markets = {}
        pm._books = {}
        pm.place_limit = AsyncMock(return_value="paper-sell-001")

        maker = MagicMock()
        maker.get_active_quotes = MagicMock(return_value={})
        maker.get_hl_mid = MagicMock(return_value=None)
        maker._rebalance_hedge = AsyncMock()
        maker._reprice_market = AsyncMock()

        risk = RiskEngine()
        monitor = MagicMock()

        return FillSimulator(pm, maker, risk, monitor)

    def test_sweep_does_not_crash(self):
        """_sweep() with no active quotes must complete without exception."""
        config.STRATEGY_MAKER_ENABLED = True
        sim = self._make_simulator()
        _run(sim._sweep())  # must not raise

    def test_loop_iteration_does_not_crash(self):
        """A single _loop iteration must not crash regardless of maker state."""
        config.STRATEGY_MAKER_ENABLED = False  # skips _sweep entirely
        sim = self._make_simulator()
        sim._running = False  # prevent infinite loop
        _run(sim._loop())  # exits immediately since _running is False


# ─────────────────────────────────────────────────────────────────────────────
# D. MomentumScanner scan smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestMomentumScannerSmoke:
    """
    MomentumScanner._scan_once() must complete without error given an empty
    market list.  Proves the scan pipeline is wired correctly end-to-end.
    """

    def _make_scanner(self):
        from strategies.Momentum.scanner import MomentumScanner
        from risk import RiskEngine

        pm = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.get_mid = MagicMock(return_value=None)
        pm.get_book = MagicMock(return_value=None)

        hl = MagicMock()
        hl.get_mid = MagicMock(return_value=None)

        risk = RiskEngine()
        vol = MagicMock()
        spot = MagicMock()
        spot.get_mid = MagicMock(return_value=None)
        spot.get_spot = MagicMock(return_value=None)

        scanner = MomentumScanner(
            pm=pm,
            hl=hl,
            risk=risk,
            vol_fetcher=vol,
            spot_client=spot,
            on_signal=None,
            funding_cache=None,
            oracle_tracker=None,
        )
        # Prevent writing to the real cooldown file
        scanner._cooldown_path = ""
        scanner._open_spot_path = ""
        return scanner

    def test_scan_once_empty_markets_no_crash(self):
        """_scan_once() with no bucket markets must return without error."""
        scanner = self._make_scanner()
        _run(scanner._scan_once())  # must not raise

    def test_scanner_instantiation(self):
        """MomentumScanner can be instantiated with mocked dependencies."""
        scanner = self._make_scanner()
        assert scanner is not None

    def test_start_completes_without_error(self):
        """start() must register subscriptions and return without crashing."""
        scanner = self._make_scanner()
        scanner._pm.on_price_change = MagicMock()
        scanner._pm.get_markets = MagicMock(return_value={})

        with patch("asyncio.create_task", side_effect=_noop_create_task):
            _run(scanner.start())


# ─────────────────────────────────────────────────────────────────────────────
# E. MispricingScanner scan smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestMispricingScannerSmoke:
    """
    MispricingScanner._scan_once() must complete without error given an empty
    market list.
    """

    def _make_scanner(self):
        from strategies.mispricing.strategy import MispricingScanner
        from risk import RiskEngine

        pm = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.get_mid = MagicMock(return_value=None)
        pm.get_book = MagicMock(return_value=None)

        hl = MagicMock()
        hl.get_mid = MagicMock(return_value=None)

        async def _dummy_signal(_sig):
            pass

        return MispricingScanner(
            pm=pm,
            hl=hl,
            signal_callback=_dummy_signal,
            scan_interval=300,
            spot_client=None,
        )

    def test_scan_once_empty_markets_no_crash(self):
        """_scan_once() over an empty market list must not raise."""
        scanner = self._make_scanner()
        _run(scanner._scan_once())  # must not raise

    def test_scanner_instantiation(self):
        scanner = self._make_scanner()
        assert scanner is not None

    def test_start_completes_without_error(self):
        """start() must register callbacks and return without crashing."""
        scanner = self._make_scanner()
        scanner._pm.on_price_change = MagicMock()
        scanner._kalshi.refresh_markets = AsyncMock()

        with patch("asyncio.create_task", side_effect=_noop_create_task):
            _run(scanner.start())


# ─────────────────────────────────────────────────────────────────────────────
# F. OpeningNeutralScanner scan smoke
# ─────────────────────────────────────────────────────────────────────────────

class TestOpeningNeutralScannerSmoke:
    """
    OpeningNeutralScanner._refresh_pending_markets() must complete without error
    given an empty market list.
    """

    def _make_scanner(self):
        from strategies.OpeningNeutral.scanner import OpeningNeutralScanner
        from risk import RiskEngine

        pm = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.on_price_change = MagicMock()

        risk = RiskEngine()
        spot = MagicMock()
        spot.get_mid = MagicMock(return_value=None)
        vol = MagicMock()

        return OpeningNeutralScanner(
            pm=pm,
            risk=risk,
            spot_client=spot,
            vol_fetcher=vol,
            momentum_scanner=None,
            on_close_callback=None,
            on_open_callback=None,
        )

    def test_refresh_pending_markets_empty_no_crash(self):
        """_refresh_pending_markets() with no markets must not raise."""
        scanner = self._make_scanner()
        _run(scanner._refresh_pending_markets())  # must not raise

    def test_scanner_instantiation(self):
        scanner = self._make_scanner()
        assert scanner is not None

    def test_start_completes_without_error(self):
        """start() must register callbacks and return without crashing."""
        scanner = self._make_scanner()
        with patch("asyncio.create_task", side_effect=_noop_create_task):
            _run(scanner.start())
