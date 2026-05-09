"""
tests/test_s4_health_api.py — Unit tests for S4 Health API & Structured Observability.

Covers:
  S4.1  GET /health/feeds
          - Returns HEALTHY when all feeds OK
          - Returns DEGRADED when oracle STALE
          - Returns DOWN when oracle DOWN and open position exists
          - HTTP 503 when DOWN
          - HTTP 200 when HEALTHY
          - HL WS disconnected + open position → DOWN
          - PM shard counts populated correctly
          - oracle per-coin entries populated
          - oldest_mark_price_age_secs = max across coins
          - uptime_secs present and numeric

  S4.2  GET /health/positions
          - Empty list when no open positions
          - Per-position entry contains required fields
          - oracle_age_secs / oracle_status come from spot_oracle
          - tte_secs is None when market object absent
          - gates_suppressed: empty when count <= threshold
          - gates_suppressed: populated when count > threshold
          - gates_suppressed: only gates for the matching token_id

  S4.4  _feed_summary_loop
          - Does NOT log when all feeds HEALTHY
          - Logs [FEED_SUMMARY] when any oracle is STALE
          - Logs [FEED_SUMMARY] when HL disconnected
          - pm_shards segment present in log message

Run: pytest tests/test_s4_health_api.py -v
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config

# ── module-level config patches so imports succeed without real credentials ──
config.CHAINLINK_DS_USERNAME   = "test-api-key"
config.CHAINLINK_DS_PASSWORD   = "test-api-secret"
config.CHAINLINK_DS_API_KEY    = ""
config.CHAINLINK_DS_API_SECRET = ""
config.CHAINLINK_DS_HOST       = "wss://ws.dataengine.chain.link"
config.CHAINLINK_DS_FEED_IDS   = {"BTC": "0xdeadbeef" + "0" * 56}

from core.types import FeedHealth, ShardHealth
from api_server import app, state as api_state
from fastapi.testclient import TestClient

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_feed_health(
    coin: str = "BTC",
    status: str = "HEALTHY",
    age_secs: float = 0.3,
    primary_source: str = "chainlink_streams",
    reconnect_count_1h: int = 0,
) -> FeedHealth:
    return FeedHealth(
        coin=coin,
        primary_source=primary_source,
        price=60_000.0,
        age_secs=age_secs,
        status=status,
        last_reconnect_at=0.0,
        reconnect_count_1h=reconnect_count_1h,
        checked_at=time.time(),
    )


def _make_shard_health(shard_id: int = 0, health: str = "CONNECTED") -> ShardHealth:
    return ShardHealth(
        shard_id=shard_id,
        health=health,
        token_count=1000,
        last_message_age_secs=0.5,
        message_count=100,
        checked_at=time.time(),
    )


def _make_mock_oracle(coin_statuses: dict[str, str]) -> MagicMock:
    """Return a mock SpotOracle whose get_feed_health returns the given status per coin."""
    oracle = MagicMock()
    def _get_feed_health(coin, market_type):
        status = coin_statuses.get(coin, "HEALTHY")
        return _make_feed_health(coin=coin, status=status)
    oracle.get_feed_health.side_effect = _get_feed_health
    return oracle


def _make_mock_pm(
    shards: list[ShardHealth] | None = None,
) -> MagicMock:
    pm = MagicMock()
    pm.get_shard_health.return_value = shards or [_make_shard_health()]
    return pm


def _make_mock_hl(connected: bool = True, mark_ages: dict[str, float] | None = None) -> MagicMock:
    hl = MagicMock()
    hl.is_connected.return_value = connected
    ages = mark_ages or {}
    hl.get_mark_price_age.side_effect = lambda coin: ages.get(coin)
    return hl


def _make_mock_risk(positions=None) -> MagicMock:
    risk = MagicMock()
    risk.get_open_positions.return_value = positions or []
    return risk


def _make_position(token_id: str = "abc123", underlying: str = "BTC", market_type: str = "bucket_5m", strategy: str = "maker") -> MagicMock:
    pos = MagicMock()
    pos.token_id = token_id
    pos.underlying = underlying
    pos.market_type = market_type
    pos.strategy = strategy
    pos.is_closed = False
    pos.market_id = "market_001"
    return pos


def _reset_state():
    """Reset api_state refs to None to avoid cross-test bleed."""
    api_state.spot_oracle_ref = None
    api_state.hl_ref = None
    api_state.pm_ref = None
    api_state.risk_ref = None
    api_state.monitor_ref = None


client = TestClient(app)


# ── S4.1 tests ────────────────────────────────────────────────────────────────

class TestHealthFeedsEndpoint:
    """GET /health/feeds — S4.1"""

    def setup_method(self):
        _reset_state()

    def test_healthy_all_feeds_ok(self):
        """All feeds healthy → status=HEALTHY, HTTP 200."""
        api_state.spot_oracle_ref = _make_mock_oracle({c: "HEALTHY" for c in ["BTC", "ETH", "SOL"]})
        api_state.pm_ref = _make_mock_pm([_make_shard_health(0, "CONNECTED"), _make_shard_health(1, "CONNECTED")])
        api_state.hl_ref = _make_mock_hl(connected=True, mark_ages={"BTC": 0.5})
        api_state.risk_ref = _make_mock_risk([])

        r = client.get("/health/feeds")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "HEALTHY"
        assert "feeds" in body
        assert "oracle" in body["feeds"]
        assert "pm_ws" in body["feeds"]
        assert "hl_ws" in body["feeds"]
        assert isinstance(body["uptime_secs"], (int, float))

    def test_degraded_when_oracle_stale(self):
        """Oracle STALE → DEGRADED."""
        oracle = _make_mock_oracle({"BTC": "STALE", "ETH": "HEALTHY"})
        api_state.spot_oracle_ref = oracle
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        r = client.get("/health/feeds")
        body = r.json()
        assert body["status"] == "DEGRADED"
        assert r.status_code == 200  # degraded = 200, not 503

    def test_down_when_oracle_down_and_open_position(self):
        """Oracle DOWN + open position on that coin → DOWN + 503."""
        pos = _make_position(underlying="BTC", market_type="bucket_5m")
        oracle = _make_mock_oracle({"BTC": "DOWN", "ETH": "HEALTHY"})
        api_state.spot_oracle_ref = oracle
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([pos])

        r = client.get("/health/feeds")
        body = r.json()
        assert body["status"] == "DOWN"
        assert r.status_code == 503

    def test_http_200_when_not_down(self):
        """HEALTHY → HTTP 200 (suitable for uptime monitor)."""
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        r = client.get("/health/feeds")
        assert r.status_code == 200

    def test_hl_disconnected_with_open_position_is_down(self):
        """HL WS disconnected + open position → DOWN."""
        pos = _make_position(underlying="BTC")
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=False)
        api_state.risk_ref = _make_mock_risk([pos])

        r = client.get("/health/feeds")
        assert r.json()["status"] == "DOWN"
        assert r.status_code == 503

    def test_hl_connected_field_populated(self):
        """hl_ws.connected reflects is_connected() return value."""
        api_state.spot_oracle_ref = _make_mock_oracle({})
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=False)
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        assert body["feeds"]["hl_ws"]["connected"] is False

    def test_oldest_mark_price_age_secs_is_max(self):
        """oldest_mark_price_age_secs = max across all coins queried."""
        api_state.spot_oracle_ref = _make_mock_oracle({})
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True, mark_ages={"BTC": 1.0, "ETH": 5.0, "SOL": 2.0})
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        assert body["feeds"]["hl_ws"]["oldest_mark_price_age_secs"] == pytest.approx(5.0, abs=0.2)

    def test_pm_shard_counts_populated(self):
        """pm_ws fields reflect shard health list."""
        shards = [
            _make_shard_health(0, "CONNECTED"),
            _make_shard_health(1, "CONNECTED"),
            _make_shard_health(2, "DEGRADED"),
            _make_shard_health(3, "DISCONNECTED"),
        ]
        api_state.spot_oracle_ref = _make_mock_oracle({})
        api_state.pm_ref = _make_mock_pm(shards)
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        pm = body["feeds"]["pm_ws"]
        assert pm["shards_total"] == 4
        assert pm["shards_connected"] == 2
        assert pm["shards_degraded"] == 1
        assert pm["shards_disconnected"] == 1

    def test_oracle_per_coin_entry_present(self):
        """Oracle section contains an entry for each coin in HL_PERP_COINS."""
        hl_coins = getattr(config, "HL_PERP_COINS", ["BTC", "ETH"])
        oracle = _make_mock_oracle({c: "HEALTHY" for c in hl_coins})
        api_state.spot_oracle_ref = oracle
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        for coin in hl_coins:
            assert coin in body["feeds"]["oracle"]
            entry = body["feeds"]["oracle"][coin]
            assert "status" in entry
            assert "age_secs" in entry

    def test_degraded_when_pm_shard_disconnected(self):
        """PM shard DISCONNECTED → DEGRADED overall."""
        shards = [_make_shard_health(0, "CONNECTED"), _make_shard_health(1, "DISCONNECTED")]
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.pm_ref = _make_mock_pm(shards)
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        assert body["status"] == "DEGRADED"

    def test_no_refs_returns_healthy_unknown(self):
        """No refs set → endpoint still returns valid JSON without error."""
        _reset_state()
        r = client.get("/health/feeds")
        assert r.status_code in (200, 503)
        body = r.json()
        assert "status" in body
        assert "feeds" in body

    def test_oracle_fallback_entry_has_reconnect_count_when_no_oracle_ref(self):
        """reconnect_count_1h present even when no oracle ref set (bug #1 fallback path)."""
        _reset_state()  # spot_oracle_ref = None
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([])

        body = client.get("/health/feeds").json()
        for coin, entry in body["feeds"]["oracle"].items():
            assert "reconnect_count_1h" in entry, (
                f"Missing reconnect_count_1h for {coin} in no-oracle-ref fallback path"
            )
            assert isinstance(entry["reconnect_count_1h"], int), (
                f"reconnect_count_1h should be int for {coin}, got {type(entry['reconnect_count_1h'])}"
            )

    def test_positions_at_risk_is_zero_when_all_feeds_healthy(self):
        """positions_at_risk = 0 when all open position oracle feeds are HEALTHY (bug #2)."""
        pos = _make_position(underlying="BTC")
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([pos])

        body = client.get("/health/feeds").json()
        assert body["positions_at_risk"] == 0, (
            f"Expected 0 positions_at_risk for all-HEALTHY feeds, got {body['positions_at_risk']}"
        )

    def test_positions_at_risk_counts_only_stale_or_down(self):
        """positions_at_risk counts only positions with non-HEALTHY feeds (bug #2)."""
        pos_btc = _make_position(token_id="tok_btc", underlying="BTC")
        pos_eth = _make_position(token_id="tok_eth", underlying="ETH")
        oracle = _make_mock_oracle({"BTC": "STALE", "ETH": "HEALTHY"})
        api_state.spot_oracle_ref = oracle
        api_state.pm_ref = _make_mock_pm()
        api_state.hl_ref = _make_mock_hl(connected=True)
        api_state.risk_ref = _make_mock_risk([pos_btc, pos_eth])

        body = client.get("/health/feeds").json()
        assert body["positions_at_risk"] == 1, (
            f"Expected 1 (BTC=STALE, ETH=HEALTHY), got {body['positions_at_risk']}"
        )


# ── S4.2 tests ────────────────────────────────────────────────────────────────

class TestHealthPositionsEndpoint:
    """GET /health/positions — S4.2"""

    def setup_method(self):
        _reset_state()

    def test_empty_when_no_open_positions(self):
        """No open positions → empty list."""
        api_state.risk_ref = _make_mock_risk([])
        r = client.get("/health/positions")
        assert r.status_code == 200
        assert r.json() == []

    def test_no_risk_ref_returns_empty(self):
        """No risk ref → empty list."""
        _reset_state()
        r = client.get("/health/positions")
        assert r.json() == []

    def test_per_position_required_fields(self):
        """Each entry has expected shape."""
        pos = _make_position(token_id="tok001", underlying="BTC")
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.pm_ref = MagicMock()
        api_state.pm_ref.get_book.return_value = None  # no book available

        r = client.get("/health/positions")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        entry = items[0]

        for field in ("token_id", "strategy", "coin", "oracle_age_secs",
                      "book_age_secs", "oracle_status", "book_status",
                      "oracle_source", "tte_secs", "gates_suppressed"):
            assert field in entry, f"Missing field: {field}"

    def test_oracle_status_from_spot_oracle(self):
        """oracle_status = STALE when feed is STALE."""
        pos = _make_position(underlying="ETH", market_type="bucket_5m")
        oracle = _make_mock_oracle({"ETH": "STALE"})
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = oracle
        api_state.pm_ref = MagicMock()
        api_state.pm_ref.get_book.return_value = None

        items = client.get("/health/positions").json()
        assert items[0]["oracle_status"] == "STALE"

    def test_tte_secs_none_when_market_not_found(self):
        """tte_secs = None when market object not in pm._markets."""
        pos = _make_position()
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        pm = MagicMock()
        pm._markets = {}
        pm.get_book.return_value = None
        api_state.pm_ref = pm

        items = client.get("/health/positions").json()
        assert items[0]["tte_secs"] is None

    def test_gates_suppressed_empty_below_threshold(self):
        """gates_suppressed = [] when counts <= threshold."""
        pos = _make_position(token_id="tok1")
        monitor = MagicMock()
        monitor._exit_suppress_counts = {"tok1:STALE_ORACLE": 2, "tok1:LOW_PROB": 1}
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.monitor_ref = monitor
        api_state.pm_ref = MagicMock()
        api_state.pm_ref.get_book.return_value = None

        items = client.get("/health/positions").json()
        # default threshold = 3; counts of 2 and 1 are below threshold
        assert items[0]["gates_suppressed"] == []

    def test_gates_suppressed_populated_above_threshold(self):
        """gates_suppressed populated when count >= threshold."""
        pos = _make_position(token_id="tok2")
        threshold = int(getattr(config, "GATE_LOG_CONSECUTIVE_THRESHOLD", 3))
        monitor = MagicMock()
        monitor._exit_suppress_counts = {
            f"tok2:STALE_ORACLE": threshold + 1,
            f"tok2:LOW_PROB": threshold - 1,
        }
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.monitor_ref = monitor
        api_state.pm_ref = MagicMock()
        api_state.pm_ref.get_book.return_value = None

        items = client.get("/health/positions").json()
        suppressed = items[0]["gates_suppressed"]
        assert "STALE_ORACLE" in suppressed
        assert "LOW_PROB" not in suppressed

    def test_gates_suppressed_only_for_matching_token(self):
        """gates_suppressed only includes gates for that token, not other tokens."""
        pos = _make_position(token_id="mine")
        monitor = MagicMock()
        monitor._exit_suppress_counts = {
            "mine:GATE_A": 10,
            "other:GATE_B": 10,
        }
        api_state.risk_ref = _make_mock_risk([pos])
        api_state.spot_oracle_ref = _make_mock_oracle({"BTC": "HEALTHY"})
        api_state.monitor_ref = monitor
        api_state.pm_ref = MagicMock()
        api_state.pm_ref.get_book.return_value = None

        items = client.get("/health/positions").json()
        suppressed = items[0]["gates_suppressed"]
        assert "GATE_A" in suppressed
        assert "GATE_B" not in suppressed


# ── S4.4 tests ────────────────────────────────────────────────────────────────

class TestFeedSummaryLoop:
    """_feed_summary_loop — S4.4"""

    def _run_one_iteration(self, spot_oracle, hl_client, risk_engine, sleep_time=0.0):
        """
        Drive one loop iteration by patching asyncio.sleep with a quick side_effect
        then cancelling after the first iteration.
        """
        import main as main_module
        call_count = 0

        async def _driver():
            nonlocal call_count
            async def fast_sleep(_):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError()
            with patch("asyncio.sleep", side_effect=fast_sleep):
                try:
                    await main_module._feed_summary_loop(
                        spot_oracle, hl_client, risk_engine, interval_secs=0.001
                    )
                except asyncio.CancelledError:
                    pass

        asyncio.run(_driver())

    def test_no_log_when_all_healthy(self, caplog):
        """No [FEED_SUMMARY] emitted when all feeds healthy."""
        import logging
        oracle = _make_mock_oracle({c: "HEALTHY" for c in ["BTC", "ETH", "SOL"]})
        hl = _make_mock_hl(connected=True)
        risk = _make_mock_risk([])

        with patch("api_server.state") as mock_state:
            mock_state.pm_ref = _make_mock_pm()
            with caplog.at_level(logging.WARNING):
                self._run_one_iteration(oracle, hl, risk)

        assert "[FEED_SUMMARY]" not in caplog.text

    def test_logs_when_oracle_stale(self, caplog):
        """[FEED_SUMMARY] emitted when oracle has STALE coin."""
        import logging
        oracle = _make_mock_oracle({"BTC": "STALE", "ETH": "HEALTHY"})
        hl = _make_mock_hl(connected=True)
        risk = _make_mock_risk([])

        with patch("api_server.state") as mock_state:
            mock_state.pm_ref = _make_mock_pm()
            with caplog.at_level(logging.WARNING):
                self._run_one_iteration(oracle, hl, risk)

        assert "[FEED_SUMMARY]" in caplog.text
        assert "STALE" in caplog.text

    def test_logs_when_hl_disconnected(self, caplog):
        """[FEED_SUMMARY] emitted when HL WS is disconnected."""
        import logging
        oracle = _make_mock_oracle({"BTC": "HEALTHY"})
        hl = _make_mock_hl(connected=False)
        risk = _make_mock_risk([])

        with patch("api_server.state") as mock_state:
            mock_state.pm_ref = _make_mock_pm()
            with caplog.at_level(logging.WARNING):
                self._run_one_iteration(oracle, hl, risk)

        assert "[FEED_SUMMARY]" in caplog.text
        assert "DISCONNECTED" in caplog.text

    def test_pm_shards_segment_present(self, caplog):
        """Log line contains pm_shards= segment."""
        import logging
        oracle = _make_mock_oracle({"BTC": "STALE"})  # trigger log
        hl = _make_mock_hl(connected=True)
        risk = _make_mock_risk([])

        with patch("api_server.state") as mock_state:
            mock_state.pm_ref = _make_mock_pm([
                _make_shard_health(0, "CONNECTED"),
                _make_shard_health(1, "CONNECTED"),
            ])
            with caplog.at_level(logging.WARNING):
                self._run_one_iteration(oracle, hl, risk)

        assert "pm_shards=" in caplog.text
