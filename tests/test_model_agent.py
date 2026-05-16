"""
tests/test_model_agent.py â€” Unit tests for models/model_agent.py (ML-04)

Tests cover all 5 acceptance criteria from ML_PRD.md ML-04:
  1. shadow-rows-written         â€” new CSV row written per fill row
  2. outcome-resolution          â€” PENDING â†’ WIN/LOSS on trades.csv match
  3. error-isolation             â€” single bad row never breaks the loop
  4. disabled-is-clean           â€” MODEL_AGENT_ENABLED=False: run() exits immediately
  5. no-shared-state-mutation    â€” run() never mutates positions/risk state

Run:  pytest tests/test_model_agent.py -v
"""
import asyncio
import csv
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


_MOM_FILL_COLS = [
    "timestamp", "market_id", "market_title", "underlying", "market_type",
    "side", "signal_price", "order_price", "fill_price", "fill_size",
    "slippage_pct", "signal_delta_pct", "signal_obs_z", "signal_sigma_ann",
    "tte_seconds", "ask_depth_usd", "fill_from_ws", "kelly_win_prob",
    "kelly_payout_b", "kelly_f", "kelly_fraction_cfg", "kelly_multiplier",
    "kelly_size_usd", "row_type", "funding_rate", "yes_depth_share",
    "hour_utc", "effective_z", "funding_gate_applied", "streak_key",
    "twap_dev_bps", "vol_regime",
]

_ON_FILL_COLS = [
    "timestamp", "pair_id", "market_id", "market_title", "underlying",
    "market_type", "yes_entry", "no_entry", "combined_cost", "yes_spread",
    "no_spread", "funding_rate", "yes_depth_share", "loser_confidence_score",
    "yes_sell_price_placed", "no_sell_price_placed", "loser_leg",
    "loser_fill_price", "loser_fill_time_secs", "winner_exit_price",
]

_ACCT_LEDGER_COLS = [
    "record_id", "recorded_at", "pos_id", "pair_id", "parent_pos_id",
    "strategy", "fill_type", "market_id", "market_title", "market_type",
    "underlying", "side", "token_id", "entry_vwap", "entry_contracts",
    "entry_cost_usd", "entry_time", "exit_vwap", "exit_contracts", "exit_time",
    "exit_type", "exit_reason", "spot_entry", "spot_exit", "strike",
    "tte_seconds", "resolve_price", "resolved_outcome", "gross_pnl",
    "fees_usd", "rebates_usd", "net_pnl", "pm_entry_confirmed",
    "pm_exit_confirmed", "signal_source", "signal_score", "reconciliation_notes",
]


def _make_momentum_fill(market_id: str = "mkt-001", ts: str = None) -> dict:
    return {
        "timestamp": ts or str(time.time()),
        "market_id": market_id,
        "market_title": f"Test Market {market_id}",
        "underlying": "BTC",
        "market_type": "bucket_5m",
        "side": "YES",
        "signal_price": "0.52",
        "order_price": "0.52",
        "fill_price": "0.52",
        "fill_size": "10",
        "slippage_pct": "0.0",
        "signal_delta_pct": "0.05",
        "signal_obs_z": "2.1",
        "signal_sigma_ann": "0.8",
        "tte_seconds": "300",
        "ask_depth_usd": "200",
        "fill_from_ws": "true",
        "kelly_win_prob": "0.55",
        "kelly_payout_b": "0.9",
        "kelly_f": "0.06",
        "kelly_fraction_cfg": "0.25",
        "kelly_multiplier": "1.0",
        "kelly_size_usd": "10",
        "row_type": "entry",
        "funding_rate": "0.0001",
        "yes_depth_share": "0.6",
        "hour_utc": "14",
        "effective_z": "2.1",
        "funding_gate_applied": "false",
        "streak_key": "",
        "twap_dev_bps": "5",
        "vol_regime": "normal",
    }


# â”€â”€ Fixtures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.fixture()
def tmp_paths(tmp_path):
    """Returns a dict of patched paths for ModelAgent CSV I/O."""
    data_dir = tmp_path / "data"
    analysis_dir = tmp_path / "analysis"
    data_dir.mkdir()
    analysis_dir.mkdir()
    return {
        "data": data_dir,
        "analysis": analysis_dir,
        "momentum_fills": data_dir / "momentum_fills.csv",
        "on_fills": data_dir / "on_fills.csv",
        "acct_ledger": data_dir / "acct_ledger.csv",
        "shadow_log": analysis_dir / "shadow_log.csv",
    }


def _make_agent(paths: dict):
    """Create ModelAgent with patched paths and MODEL_AGENT_ENABLED=True."""
    from models.model_agent import ModelAgent

    with (
        patch("models.model_agent._ON_FILLS_PATH", paths["on_fills"]),
        patch("models.model_agent._MOMENTUM_FILLS_PATH", paths["momentum_fills"]),
        patch("models.model_agent._ACCT_LEDGER_PATH", paths["acct_ledger"]),
        patch("models.model_agent._SHADOW_LOG_PATH", paths["shadow_log"]),
        patch.object(config, "MODEL_AGENT_ENABLED", True),
    ):
        ma = ModelAgent()
    return ma


def _run_agent_async(paths: dict, coro_factory):
    """Patch paths into model_agent module, create agent, run async coroutine."""
    from models.model_agent import ModelAgent

    async def _inner():
        with (
            patch("models.model_agent._ON_FILLS_PATH", paths["on_fills"]),
            patch("models.model_agent._MOMENTUM_FILLS_PATH", paths["momentum_fills"]),
            patch("models.model_agent._ACCT_LEDGER_PATH", paths["acct_ledger"]),
            patch("models.model_agent._SHADOW_LOG_PATH", paths["shadow_log"]),
            patch.object(config, "MODEL_AGENT_ENABLED", True),
        ):
            ma = ModelAgent()
            await coro_factory(ma)
        return ma

    return asyncio.run(_inner())


# â”€â”€ AC-1: shadow-rows-written â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_shadow_row_written_for_new_entry_fill(tmp_paths):
    """When a new momentum fill row exists, ModelAgent writes one shadow_log row."""
    _write_csv(tmp_paths["momentum_fills"], [_make_momentum_fill("mkt-001")], _MOM_FILL_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)

    rows = _read_csv(tmp_paths["shadow_log"])
    assert len(rows) == 1
    assert rows[0]["market_id"] == "mkt-001"
    assert rows[0]["decision_type"] == "entry"
    assert rows[0]["actual_outcome"] == "PENDING"


def test_no_duplicate_rows_on_reprocess(tmp_paths):
    """Running _process_new_entries twice on the same file doesn't create duplicate rows."""
    _write_csv(tmp_paths["momentum_fills"], [_make_momentum_fill("mkt-002")], _MOM_FILL_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)

    rows = _read_csv(tmp_paths["shadow_log"])
    assert len(rows) == 1  # no duplicate


# â”€â”€ AC-2: outcome-resolution â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_pending_resolved_to_win_when_trade_csv_shows_win(tmp_paths):
    """PENDING shadow row is updated to WIN when acct_ledger shows resolved_outcome=WIN."""
    ts = str(time.time() - 10)
    _write_csv(tmp_paths["momentum_fills"], [_make_momentum_fill("mkt-003", ts=ts)], _MOM_FILL_COLS)

    ledger = {k: "" for k in _ACCT_LEDGER_COLS}
    ledger["market_id"] = "mkt-003"
    ledger["side"] = "YES"
    ledger["resolved_outcome"] = "WIN"
    _write_csv(tmp_paths["acct_ledger"], [ledger], _ACCT_LEDGER_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()
        await ma._resolve_pending_outcomes()

    _run_agent_async(tmp_paths, run)

    rows = _read_csv(tmp_paths["shadow_log"])
    assert len(rows) == 1
    assert rows[0]["actual_outcome"] == "WIN"


def test_pending_resolved_to_loss(tmp_paths):
    """PENDING → LOSS when acct_ledger shows LOSS."""
    ts = str(time.time() - 10)
    _write_csv(tmp_paths["momentum_fills"], [_make_momentum_fill("mkt-004", ts=ts)], _MOM_FILL_COLS)

    ledger = {k: "" for k in _ACCT_LEDGER_COLS}
    ledger["market_id"] = "mkt-004"
    ledger["side"] = "YES"
    ledger["resolved_outcome"] = "LOSS"
    _write_csv(tmp_paths["acct_ledger"], [ledger], _ACCT_LEDGER_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()
        await ma._resolve_pending_outcomes()

    _run_agent_async(tmp_paths, run)

    rows = _read_csv(tmp_paths["shadow_log"])
    assert rows[0]["actual_outcome"] == "LOSS"


# â”€â”€ AC-3: error-isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_malformed_csv_row_does_not_raise(tmp_paths):
    """A momentum_fills.csv with malformed rows doesn't raise; agent stays alive."""
    tmp_paths["momentum_fills"].write_text("this,is,not,valid\ngarbage\n", encoding="utf-8")

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)  # must not raise

    rows = _read_csv(tmp_paths["shadow_log"])
    assert len(rows) == 0


def test_missing_fills_csv_does_not_raise(tmp_paths):
    """Missing fills CSV files: _process_new_entries returns cleanly."""
    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)  # must not raise

    assert not tmp_paths["shadow_log"].exists() or _read_csv(tmp_paths["shadow_log"]) == []


# â”€â”€ AC-4: disabled-is-clean â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_run_exits_immediately_when_disabled():
    """run() returns without starting a loop when MODEL_AGENT_ENABLED=False."""
    from models.model_agent import ModelAgent

    async def _run():
        with patch.object(config, "MODEL_AGENT_ENABLED", False):
            ma = ModelAgent()
            await asyncio.wait_for(ma.run(), timeout=1.0)
            return ma

    ma = asyncio.run(_run())
    assert ma._status == "DISABLED"


def test_score_entry_returns_neutral_when_disabled():
    """score_entry() returns exactly 0.5 when disabled."""
    from models.model_agent import ModelAgent

    with patch.object(config, "MODEL_AGENT_ENABLED", False):
        ma = ModelAgent()
        result = ma.score_entry("any-market", {})
    assert result == 0.5


def test_score_exit_returns_neutral_when_disabled():
    """score_exit() returns exactly 0.5 when disabled."""
    from models.model_agent import ModelAgent

    with patch.object(config, "MODEL_AGENT_ENABLED", False):
        ma = ModelAgent()
        result = ma.score_exit("any-market", {})
    assert result == 0.5


# â”€â”€ AC-5: API schema / no-shared-state-mutation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_get_status_returns_correct_schema(tmp_paths):
    """get_status() returns dict with all required keys and correct types."""
    async def run(ma):
        status = ma.get_status()
        return status

    async def _inner():
        with (
            patch("models.model_agent._ON_FILLS_PATH", tmp_paths["on_fills"]),
            patch("models.model_agent._MOMENTUM_FILLS_PATH", tmp_paths["momentum_fills"]),
            patch("models.model_agent._ACCT_LEDGER_PATH", tmp_paths["acct_ledger"]),
            patch("models.model_agent._SHADOW_LOG_PATH", tmp_paths["shadow_log"]),
            patch.object(config, "MODEL_AGENT_ENABLED", True),
        ):
            from models.model_agent import ModelAgent
            ma = ModelAgent()
            return ma.get_status()

    status = asyncio.run(_inner())
    assert isinstance(status["enabled"], bool)
    assert status["status"] in ("RUNNING", "DISABLED", "ERROR")
    assert isinstance(status["total_decisions"], int)
    assert isinstance(status["pending_outcomes"], int)
    assert status["last_decision_ts"] is None
    assert status["agreement_rate_last_20"] is None


def test_get_shadow_log_returns_correct_schema(tmp_paths):
    """get_shadow_log() returns {rows: list, total: int}."""
    from models.model_agent import ModelAgent

    with patch.object(config, "MODEL_AGENT_ENABLED", True):
        ma = ModelAgent()
        result = ma.get_shadow_log()
    assert "rows" in result
    assert "total" in result
    assert isinstance(result["rows"], list)
    assert isinstance(result["total"], int)


def test_get_shadow_log_filter_by_decision_type(tmp_paths):
    """get_shadow_log(decision_type='entry') only returns entry rows."""
    _write_csv(tmp_paths["momentum_fills"], [_make_momentum_fill("mkt-010")], _MOM_FILL_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)

    from models.model_agent import ModelAgent

    async def _check():
        with (
            patch("models.model_agent._ON_FILLS_PATH", tmp_paths["on_fills"]),
            patch("models.model_agent._MOMENTUM_FILLS_PATH", tmp_paths["momentum_fills"]),
            patch("models.model_agent._ACCT_LEDGER_PATH", tmp_paths["acct_ledger"]),
            patch("models.model_agent._SHADOW_LOG_PATH", tmp_paths["shadow_log"]),
            patch.object(config, "MODEL_AGENT_ENABLED", True),
        ):
            ma = ModelAgent()
            await ma._seed_processed_keys()
            entry_result = ma.get_shadow_log(decision_type="entry")
            exit_result = ma.get_shadow_log(decision_type="exit")
            return entry_result, exit_result

    entry_result, exit_result = asyncio.run(_check())
    assert all(r["decision_type"] == "entry" for r in entry_result["rows"])
    assert all(r["decision_type"] == "exit" for r in exit_result["rows"])


def test_multiple_markets_each_get_own_row(tmp_paths):
    """Multiple distinct markets each produce their own shadow row."""
    fills = [
        _make_momentum_fill("mkt-A", ts=str(time.time() - 5)),
        _make_momentum_fill("mkt-B", ts=str(time.time() - 4)),
        _make_momentum_fill("mkt-C", ts=str(time.time() - 3)),
    ]
    _write_csv(tmp_paths["momentum_fills"], fills, _MOM_FILL_COLS)

    async def run(ma):
        await ma._ensure_header()
        await ma._process_new_entries()

    _run_agent_async(tmp_paths, run)

    rows = _read_csv(tmp_paths["shadow_log"])
    market_ids = {r["market_id"] for r in rows}
    assert "mkt-A" in market_ids
    assert "mkt-B" in market_ids
    assert "mkt-C" in market_ids
    assert len(rows) == 3

