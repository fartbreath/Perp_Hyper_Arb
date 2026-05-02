"""
tests/test_api_server.py — Unit tests for api_server.py

Run:  pytest tests/test_api_server.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import csv
import json
import math
import time
import tempfile
import os
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import patch, MagicMock

import config
config.PAPER_TRADING = True
config.AGENT_AUTO = False

from fastapi.testclient import TestClient
import api_server
from api_server import app, state, _pnl_histogram, _time_of_day_heatmap, _compute_sharpe

client = TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_state():
    """Reset shared state to defaults before each test."""
    state.pm_ws_connected = False
    state.hl_ws_connected = False
    state.last_heartbeat_ts = 0.0
    state.paper_trading = True
    state.positions = {}
    state.markets = {}
    state.signals = []
    state.funding = {}
    state.active_quotes = {}
    state.agent_shadow_log = []


def _make_trade_rows(n=5, base_pnl=10.0) -> list[dict]:
    now = time.time()
    return [
        {
            "market_id": f"cond_{i:03d}",
            "underlying": "BTC",
            "strategy": "maker",
            "side": "YES_BUY",
            "size_usd": "100.0",
            "entry_price": "0.45",
            "exit_price": "0.55",
            "pnl": str(base_pnl + i),
            "fee": "1.5",
            "rebate": "0.8",
            "timestamp": datetime.fromtimestamp(now - i * 3600, tz=timezone.utc).isoformat(),
        }
        for i in range(n)
    ]


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    def setup_method(self):
        _reset_state()

    def test_status_running(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "running"

    def test_paper_trading_flag(self):
        r = client.get("/health")
        assert r.json()["paper_trading"] is True

    def test_ws_connected_false_by_default(self):
        r = client.get("/health")
        data = r.json()
        assert data["pm_ws_connected"] is False
        assert data["hl_ws_connected"] is False

    def test_ws_connected_true_when_set(self):
        state.pm_ws_connected = True
        state.hl_ws_connected = True
        r = client.get("/health")
        data = r.json()
        assert data["pm_ws_connected"] is True
        assert data["hl_ws_connected"] is True

    def test_uptime_positive(self):
        r = client.get("/health")
        assert r.json()["uptime_seconds"] >= 0

    def test_heartbeat_age_none_when_never_seen(self):
        r = client.get("/health")
        assert r.json()["last_heartbeat_age_s"] is None


# ── /positions ────────────────────────────────────────────────────────────────

class TestPositions:
    def setup_method(self):
        _reset_state()

    def test_empty_positions(self):
        r = client.get("/positions")
        assert r.status_code == 200
        assert r.json()["count"] == 0
        assert r.json()["positions"] == []

    def test_positions_listed(self):
        state.positions = {
            "cond_001": {"condition_id": "cond_001", "venue": "PM", "size_usd": 100.0},
        }
        r = client.get("/positions")
        assert r.json()["count"] == 1

    def test_multiple_positions(self):
        state.positions = {
            "cond_001": {"condition_id": "cond_001"},
            "cond_002": {"condition_id": "cond_002"},
        }
        r = client.get("/positions")
        assert r.json()["count"] == 2


# ── /trades ───────────────────────────────────────────────────────────────────

class TestTrades:
    def setup_method(self):
        _reset_state()

    def _patch_trades(self, rows):
        return patch("api_server._load_trades_csv", return_value=rows)

    def test_empty_trades(self):
        with self._patch_trades([]):
            r = client.get("/trades")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_trades_returned(self):
        rows = _make_trade_rows(n=3)
        with self._patch_trades(rows):
            r = client.get("/trades")
        assert r.json()["total"] == 3

    def test_pagination_limit(self):
        rows = _make_trade_rows(n=20)
        with self._patch_trades(rows):
            r = client.get("/trades?limit=5")
        assert len(r.json()["trades"]) == 5

    def test_pagination_offset(self):
        rows = _make_trade_rows(n=10)
        with self._patch_trades(rows):
            r = client.get("/trades?limit=5&offset=5")
        assert r.json()["offset"] == 5

    def test_filter_by_strategy(self):
        rows = _make_trade_rows(n=5)
        rows[0]["strategy"] = "mispricing"
        with self._patch_trades(rows):
            r = client.get("/trades?strategy=mispricing")
        assert r.json()["total"] == 1

    def test_filter_by_underlying(self):
        rows = _make_trade_rows(n=5)
        rows[2]["underlying"] = "ETH"
        with self._patch_trades(rows):
            r = client.get("/trades?underlying=ETH")
        assert r.json()["total"] == 1


# ── /pnl ─────────────────────────────────────────────────────────────────────

class TestPnl:
    def setup_method(self):
        _reset_state()

    def test_empty_returns_zeros(self):
        with patch("api_server._load_acct_ledger_trades", return_value=[]):
            r = client.get("/pnl")
        data = r.json()
        assert data["all_time"] == 0.0
        assert data["trade_count_all"] == 0

    def test_all_time_sum(self):
        rows = _make_trade_rows(n=3, base_pnl=10.0)
        # pnl values: 10, 11, 12 → sum=33
        with patch("api_server._load_acct_ledger_trades", return_value=rows):
            r = client.get("/pnl")
        assert r.json()["all_time"] == pytest.approx(33.0)

    def test_response_has_all_keys(self):
        with patch("api_server._load_acct_ledger_trades", return_value=[]):
            r = client.get("/pnl")
        keys = r.json().keys()
        for k in ("today", "week", "all_time", "trade_count_all"):
            assert k in keys


# ── /performance ──────────────────────────────────────────────────────────────

class TestPerformance:
    def setup_method(self):
        _reset_state()

    def test_no_data_when_empty(self):
        with patch("api_server._load_acct_ledger_trades", return_value=[]):
            r = client.get("/performance")
        assert r.json()["no_data"] is True

    def test_win_rate_computed(self):
        rows = _make_trade_rows(n=4, base_pnl=5.0)  # all pnl > 0
        with patch("api_server._load_acct_ledger_trades", return_value=rows):
            r = client.get("/performance")
        assert r.json()["summary"]["win_rate"] == pytest.approx(1.0)

    def test_equity_curve_ascending(self):
        rows = _make_trade_rows(n=5, base_pnl=10.0)
        with patch("api_server._load_acct_ledger_trades", return_value=rows):
            r = client.get("/performance")
        curve = r.json()["equity_curve"]
        assert len(curve) == 5
        # All positive pnl → equity should increase
        equities = [pt["equity"] for pt in curve]
        assert equities[-1] > equities[0]

    def test_period_7d_filter(self):
        old_row = _make_trade_rows(n=1, base_pnl=100.0)[0]
        old_row["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        new_row = _make_trade_rows(n=1, base_pnl=5.0)[0]
        new_row["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch("api_server._load_acct_ledger_trades", return_value=[old_row, new_row]):
            r = client.get("/performance?period=7d")
        assert r.json()["summary"]["total_trades"] == 1

    def test_by_strategy_breakdown(self):
        rows = _make_trade_rows(n=4, base_pnl=10.0)
        rows[0]["strategy"] = "mispricing"
        with patch("api_server._load_acct_ledger_trades", return_value=rows):
            r = client.get("/performance")
        by_s = r.json()["by_strategy"]
        assert "maker" in by_s
        assert "mispricing" in by_s

    def test_histogram_present(self):
        rows = _make_trade_rows(n=10, base_pnl=5.0)
        with patch("api_server._load_acct_ledger_trades", return_value=rows):
            r = client.get("/performance")
        assert len(r.json()["pnl_histogram"]) > 0

    def test_invalid_period_rejected(self):
        r = client.get("/performance?period=invalid")
        assert r.status_code == 422


# ── /signals ─────────────────────────────────────────────────────────────────

class TestSignals:
    def setup_method(self):
        _reset_state()

    def test_empty_signals(self):
        r = client.get("/signals")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_signals_returned(self):
        state.signals = [{"id": 1}, {"id": 2}, {"id": 3}]
        r = client.get("/signals")
        assert r.json()["total"] == 3

    def test_limit_applied(self):
        state.signals = [{"id": i} for i in range(100)]
        r = client.get("/signals?limit=10")
        assert len(r.json()["signals"]) == 10


# ── /risk ─────────────────────────────────────────────────────────────────────

class TestRisk:
    def setup_method(self):
        _reset_state()

    def test_empty_exposure(self):
        r = client.get("/risk")
        data = r.json()
        assert data["pm_exposure_usd"] == 0.0
        assert data["hl_notional_usd"] == 0.0

    def test_pm_exposure_calculated(self):
        state.positions = {
            "cond_001": {"venue": "PM", "size_usd": 200.0},
            "cond_002": {"venue": "PM", "size_usd": 150.0},
        }
        r = client.get("/risk")
        assert r.json()["pm_exposure_usd"] == pytest.approx(350.0)

    def test_hl_notional_uses_abs(self):
        state.positions = {
            "hl_btc": {"venue": "HL", "size_usd": -500.0},
        }
        r = client.get("/risk")
        assert r.json()["hl_notional_usd"] == pytest.approx(500.0)

    def test_limits_present(self):
        r = client.get("/risk")
        data = r.json()
        assert data["pm_exposure_limit"] == config.MAX_TOTAL_PM_EXPOSURE
        assert data["hl_notional_limit"] == config.MAX_HL_NOTIONAL


# ── /markets ─────────────────────────────────────────────────────────────────

class TestMarkets:
    def setup_method(self):
        _reset_state()

    def test_empty_markets(self):
        r = client.get("/markets")
        assert r.json()["count"] == 0

    def test_market_listed(self):
        state.markets = {
            "cond_001": {
                "condition_id": "cond_001",
                "title": "BTC $120k",
                "token_id_yes": "tok_001",
            }
        }
        r = client.get("/markets")
        assert r.json()["count"] == 1

    def test_market_includes_quote_info(self):
        state.markets = {
            "cond_001": {
                "condition_id": "cond_001",
                "title": "BTC $120k",
                "token_id_yes": "tok_001",
            }
        }
        state.active_quotes = {
            "tok_001": {"price": 0.45, "side": "BUY", "size": 50.0},
        }
        r = client.get("/markets")
        mkt = r.json()["markets"][0]
        assert mkt["quoted"] is True
        assert mkt["bid_price"] == pytest.approx(0.45)


# ── /funding ─────────────────────────────────────────────────────────────────

class TestFunding:
    def setup_method(self):
        _reset_state()

    def test_empty_funding(self):
        r = client.get("/funding")
        assert r.json()["funding"] == {}

    def test_funding_data(self):
        state.funding = {
            "BTC": {"predicted_rate": 0.0001, "annual_rate": 0.0365}
        }
        r = client.get("/funding")
        assert "BTC" in r.json()["funding"]


# ── Analytics unit functions ──────────────────────────────────────────────────

class TestPnlHistogram:
    def test_empty_returns_empty(self):
        assert _pnl_histogram([]) == []

    def test_buckets_count(self):
        vals = [float(i) for i in range(100)]
        result = _pnl_histogram(vals, buckets=10)
        assert len(result) == 10

    def test_all_values_accounted(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _pnl_histogram(vals, buckets=5)
        assert sum(b["count"] for b in result) == 5

    def test_single_value(self):
        result = _pnl_histogram([5.0])
        assert len(result) == 1
        assert result[0]["count"] == 1


class TestComputeSharpe:
    def test_too_few_rows_returns_none(self):
        assert _compute_sharpe([]) is None

    def test_returns_float_with_valid_data(self):
        now = time.time()
        rows = [
            {"timestamp": datetime.fromtimestamp(now - i * 86400, tz=timezone.utc).isoformat(), "pnl": str(5.0 + i)}
            for i in range(10)
        ]
        result = _compute_sharpe(rows)
        # Should be a number (or None if daily variation is 0)
        assert result is None or isinstance(result, float)


class TestTimeOfDayHeatmap:
    def test_empty_rows_returns_empty(self):
        assert _time_of_day_heatmap([]) == []

    def test_hour_range(self):
        now = time.time()
        rows = [{"timestamp": datetime.fromtimestamp(now - i * 3600, tz=timezone.utc).isoformat(), "pnl": "10.0"} for i in range(24)]
        result = _time_of_day_heatmap(rows)
        hours = [entry["hour_hkt"] for entry in result]
        for h in hours:
            assert 0 <= h <= 23


# ── Regression: /health adverse stats fields (BUG-3 related) ─────────────────

class TestHealthAdverseStats:
    """
    Verify /health always returns adversive-detection fields, even when
    fill_simulator module counters are at zero (default state).
    """

    def setup_method(self):
        _reset_state()

    def test_adverse_fields_present(self):
        r = client.get("/health")
        data = r.json()
        assert "adverse_triggers_session" in data
        assert "adverse_threshold_pct" in data
        assert "hl_max_move_pct_session" in data

    def test_adverse_triggers_zero_by_default(self):
        import fill_simulator as _fsm
        _fsm.reset_fill_session_stats()
        r = client.get("/health")
        assert r.json()["adverse_triggers_session"] == 0

    def test_adverse_threshold_matches_config(self):
        r = client.get("/health")
        assert r.json()["adverse_threshold_pct"] == pytest.approx(config.PAPER_ADVERSE_SELECTION_PCT)

    def test_hl_max_move_reflected(self):
        """After manually setting _hl_max_move_pct_session, /health should reflect it."""
        import fill_simulator as _fsm
        _fsm._hl_max_move_pct_session = 0.03
        r = client.get("/health")
        assert r.json()["hl_max_move_pct_session"] == pytest.approx(0.03, abs=1e-4)
        _fsm.reset_fill_session_stats()  # clean up


# ── Regression: /performance by_market_type breakdown ────────────────────────

def _make_perf_rows_with_market_type():
    now = time.time()
    return [
        {"underlying": "BTC", "strategy": "maker", "pnl": "10.0", "market_type": "bucket_5m",
         "fees_paid": "0", "rebates_earned": "0",
         "timestamp": datetime.fromtimestamp(now - 100, tz=timezone.utc).isoformat()},
        {"underlying": "BTC", "strategy": "maker", "pnl": "-5.0", "market_type": "bucket_5m",
         "fees_paid": "0", "rebates_earned": "0",
         "timestamp": datetime.fromtimestamp(now - 200, tz=timezone.utc).isoformat()},
        {"underlying": "BTC", "strategy": "maker", "pnl": "8.0", "market_type": "bucket_1h",
         "fees_paid": "0", "rebates_earned": "0",
         "timestamp": datetime.fromtimestamp(now - 300, tz=timezone.utc).isoformat()},
    ]


class TestPerformanceByMarketType:
    """Regression tests for /performance by_market_type breakdown."""

    def setup_method(self):
        _reset_state()

    def test_by_market_type_key_present(self):
        with patch("api_server._load_acct_ledger_trades", return_value=_make_perf_rows_with_market_type()):
            r = client.get("/performance")
        assert "by_market_type" in r.json()

    def test_bucket_5m_count(self):
        with patch("api_server._load_acct_ledger_trades", return_value=_make_perf_rows_with_market_type()):
            r = client.get("/performance")
        bmt = r.json()["by_market_type"]
        assert "bucket_5m" in bmt
        assert bmt["bucket_5m"]["count"] == 2

    def test_bucket_5m_pnl(self):
        with patch("api_server._load_acct_ledger_trades", return_value=_make_perf_rows_with_market_type()):
            r = client.get("/performance")
        bmt = r.json()["by_market_type"]
        assert bmt["bucket_5m"]["pnl"] == pytest.approx(5.0)

    def test_bucket_5m_win_rate(self):
        # bucket_5m: 1 win (pnl=10) out of 2 = 50 %
        with patch("api_server._load_acct_ledger_trades", return_value=_make_perf_rows_with_market_type()):
            r = client.get("/performance")
        bmt = r.json()["by_market_type"]
        assert bmt["bucket_5m"]["win_rate"] == pytest.approx(0.5)

    def test_bucket_1h_win_rate_100pct(self):
        # bucket_1h: 1 win (pnl=8) out of 1 = 100 %
        with patch("api_server._load_acct_ledger_trades", return_value=_make_perf_rows_with_market_type()):
            r = client.get("/performance")
        bmt = r.json()["by_market_type"]
        assert bmt["bucket_1h"]["win_rate"] == pytest.approx(1.0)


# ── Regression: /config + POST /config maker_excluded_market_types ────────────

class TestConfigMakerExcludedTypes:
    """
    Regression tests for BUG-1 / BUG-2: the toggleBucket fix depends on
    GET /config correctly returning maker_excluded_market_types and
    POST /config correctly updating it so re-enabling a bucket works.
    """

    def setup_method(self):
        _reset_state()
        config.MAKER_EXCLUDED_MARKET_TYPES = []

    def teardown_method(self):
        config.MAKER_EXCLUDED_MARKET_TYPES = []

    def test_get_config_returns_empty_exclusion_list_by_default(self):
        r = client.get("/config")
        assert r.status_code == 200
        assert r.json()["maker_excluded_market_types"] == []

    def test_patch_adds_bucket_type(self):
        r = client.post("/config", json={"maker_excluded_market_types": ["bucket_1h"]})
        assert r.status_code == 200
        assert "bucket_1h" in r.json()["current"]["maker_excluded_market_types"]
        assert config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_1h"]

    def test_patch_replaces_entire_list(self):
        """Sending a new list replaces the old one — not append-only."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_5m", "bucket_1h"]
        r = client.post("/config", json={"maker_excluded_market_types": ["bucket_15m"]})
        assert r.status_code == 200
        assert config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_15m"]

    def test_patch_empty_list_re_enables_all(self):
        """Sending [] after a disable clears exclusions — this is how re-enable works (BUG-1 regression)."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h", "bucket_5m"]
        r = client.post("/config", json={"maker_excluded_market_types": []})
        assert r.status_code == 200
        assert r.json()["current"]["maker_excluded_market_types"] == []
        assert config.MAKER_EXCLUDED_MARKET_TYPES == []

    def test_toggle_add_then_remove_round_trip(self):
        """Full disable→enable cycle as performed by corrected toggleBucket (BUG-1 regression)."""
        # Step 1: disable bucket_5m
        client.post("/config", json={"maker_excluded_market_types": ["bucket_5m"]})
        assert config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_5m"]

        # Step 2: fetch config (as toggleBucket does), derive excluded state
        r_get = client.get("/config")
        current = r_get.json()["maker_excluded_market_types"]
        assert "bucket_5m" in current      # confirms it was stored

        # Step 3: re-enable by sending list without bucket_5m
        updated = [b for b in current if b != "bucket_5m"]
        client.post("/config", json={"maker_excluded_market_types": updated})
        assert config.MAKER_EXCLUDED_MARKET_TYPES == []

    def test_null_field_leaves_list_unchanged(self):
        """Not sending maker_excluded_market_types (null) must not alter the current list."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        # Patch a different field — should not touch exclusions
        client.post("/config", json={"paper_fill_probability": 0.9})
        assert config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_1h"]


# ── Range market config endpoint ──────────────────────────────────────────────

class TestConfigRangeMarket:
    """
    Tests for the momentum_range_enabled config key added by the range-market
    refactor (replacing the 17-key calendar-spread block).
    """

    def setup_method(self):
        _reset_state()
        config.MOMENTUM_RANGE_ENABLED = False

    def teardown_method(self):
        config.MOMENTUM_RANGE_ENABLED = False

    def test_get_config_returns_range_enabled_key(self):
        """GET /config must include momentum_range_enabled in the response."""
        r = client.get("/config")
        assert r.status_code == 200
        assert "momentum_range_enabled" in r.json()

    def test_get_config_range_enabled_default_false(self):
        r = client.get("/config")
        assert r.json()["momentum_range_enabled"] is False

    def test_patch_enables_range_markets(self):
        r = client.post("/config", json={"momentum_range_enabled": True})
        assert r.status_code == 200
        assert config.MOMENTUM_RANGE_ENABLED is True
        assert r.json()["current"]["momentum_range_enabled"] is True

    def test_patch_disables_range_markets(self):
        config.MOMENTUM_RANGE_ENABLED = True
        r = client.post("/config", json={"momentum_range_enabled": False})
        assert r.status_code == 200
        assert config.MOMENTUM_RANGE_ENABLED is False
        assert r.json()["current"]["momentum_range_enabled"] is False

    def test_null_leaves_range_enabled_unchanged(self):
        """Patching an unrelated field must not change momentum_range_enabled."""
        config.MOMENTUM_RANGE_ENABLED = True
        client.post("/config", json={"paper_fill_probability": 0.75})
        assert config.MOMENTUM_RANGE_ENABLED is True

    def test_get_config_no_stale_calendar_spread_key(self):
        """None of the old calendar-spread keys should appear in GET /config."""
        r = client.get("/config")
        body = r.json()
        stale_keys = [k for k in body if "calendar_spread" in k or
                      (k.startswith("momentum_spread") and k != "momentum_range_enabled")]
        assert stale_keys == [], f"Stale calendar-spread keys still in /config: {stale_keys}"


# ── Regression: BUG-B — _row_age_days tz-aware timestamp handling ─────────────

class TestRowAgeDays:
    """
    BUG-B regression: _row_age_days used .replace(tzinfo=utc) unconditionally,
    which silently overwrites tzinfo on already-tz-aware datetimes instead of
    converting.  Fix: only call .replace() when tzinfo is None.
    """

    def setup_method(self):
        from api_server import _row_age_days as fn
        self._fn = fn

    def test_tz_aware_utc_suffix_is_accepted(self):
        """Timestamp with +00:00 suffix (as written by datetime.now(UTC).isoformat()) must not raise."""
        now = datetime.now(timezone.utc)
        row = {"timestamp": "2020-01-01T00:00:00+00:00"}
        age = self._fn(row, now)
        assert isinstance(age, float)
        assert age > 0

    def test_tz_aware_age_is_correct(self):
        """A timestamp exactly 7 days old (tz-aware) should report ~7 days."""
        now = datetime.now(timezone.utc)
        ts_7d_ago = (now - timedelta(days=7)).isoformat()  # includes +00:00 suffix
        row = {"timestamp": ts_7d_ago}
        age = self._fn(row, now)
        assert abs(age - 7.0) < 0.01

    def test_naive_timestamp_treated_as_utc(self):
        """Naive timestamps (no tzinfo) must be treated as UTC (legacy format)."""
        now = datetime.now(timezone.utc)
        # Simulate a naive timestamp written without tz info
        naive_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        row = {"timestamp": naive_ts}
        age = self._fn(row, now)
        assert isinstance(age, float)
        assert age >= 0.0

    def test_naive_age_close_to_zero(self):
        """A naive timestamp for 'just now' should report near-zero age."""
        now = datetime.now(timezone.utc)
        just_now = now.strftime("%Y-%m-%dT%H:%M:%S")
        row = {"timestamp": just_now}
        age = self._fn(row, now)
        assert age < 0.01  # less than ~15 minutes

    def test_missing_timestamp_returns_sentinel(self):
        """Missing timestamp key returns sentinel 9999.0 (no exception)."""
        now = datetime.now(timezone.utc)
        age = self._fn({}, now)
        assert age == 9999.0

    def test_invalid_timestamp_returns_sentinel(self):
        """Unparseable timestamp returns sentinel 9999.0 (no exception)."""
        now = datetime.now(timezone.utc)
        age = self._fn({"timestamp": "not-a-date"}, now)
        assert age == 9999.0
