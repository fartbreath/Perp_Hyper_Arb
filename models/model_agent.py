"""
models/model_agent.py — ML-04 (Phase 2)

ModelAgent runs as a background asyncio task that shadows every rules-bot decision
without affecting any live trade, position, or risk state.

Design:
  - run() is the main async coroutine (create_task in main.py).
    It exits immediately when MODEL_AGENT_ENABLED=False.
  - score_entry(market_id, context) → float: returns Model A probability (0.5 fallback).
  - score_exit(market_id, context) → float: returns Model B probability (0.5 fallback).
  - Background loop polls data/on_fills.csv, data/momentum_fills.csv (new entry rows)
    and data/trades.csv (resolved outcomes) every POLL_INTERVAL_SECS seconds.
  - Writes one row per decision to analysis/shadow_log.csv (never truncated).
  - PENDING rows are resolved to WIN/LOSS when the trade appears in trades.csv.
  - All exceptions are caught and logged at WARNING; never propagates to callers.

Controlled by config.MODEL_AGENT_ENABLED (default False).
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
from pathlib import Path
from typing import Any, Optional

import config
from logger import get_bot_logger
from models.feature_snapshot import (
    MODEL_A_FEATURES,
    MODEL_B_FEATURES,
    build_entry_snapshot,
    build_exit_snapshot,
)

log = get_bot_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_ANALYSIS_DIR = _REPO_ROOT / "analysis"
_SHADOW_LOG_PATH = _ANALYSIS_DIR / "shadow_log.csv"
_ON_FILLS_PATH = _DATA_DIR / "on_fills.csv"
_MOMENTUM_FILLS_PATH = _DATA_DIR / "momentum_fills.csv"
_TRADES_PATH = _DATA_DIR / "trades.csv"

# ── Shadow log schema ─────────────────────────────────────────────────────────

_SHADOW_COLS = [
    "timestamp",
    "market_id",
    "market_type",       # e.g. "bucket_5m" — from fill row, used for filtering
    "decision_type",    # "entry" | "exit"
    "rules_decision",   # "enter" | "exit" | "skip" | "hold"
    "model_a_score",
    "model_b_score",
    "model_decision",   # "agree" | "disagree"
    "agreed",           # "true" | "false"
    "actual_outcome",   # "PENDING" | "WIN" | "LOSS"
    "features_snapshot",  # JSON string
]

POLL_INTERVAL_SECS: float = 10.0
_SENTINEL = -999.0


# ── ModelAgent ────────────────────────────────────────────────────────────────

class ModelAgent:
    """
    Concurrent shadow-mode model agent.

    Parameters mirror the live connector objects already running in main.py.
    None is accepted for any connector; missing connectors cause feature slots
    to fall back to -999 (sentinel) rather than raising.
    """

    def __init__(
        self,
        spot_oracle: Any = None,
        hl_client: Any = None,
        pm_client: Any = None,
        funding_cache: Any = None,
        oracle_tracker: Any = None,
        vol_fetcher: Any = None,
        clob_buffer: Any = None,
    ) -> None:
        self._spot_oracle = spot_oracle
        self._hl_client = hl_client
        self._pm_client = pm_client
        self._funding_cache = funding_cache
        self._oracle_tracker = oracle_tracker
        self._vol_fetcher = vol_fetcher
        self._clob_buffer = clob_buffer

        # Models — loaded lazily on first score call
        self._model_a: Any = None
        self._model_b: Any = None
        self._model_a_loaded: bool = False
        self._model_b_loaded: bool = False

        # Runtime state
        self._status: str = "DISABLED"
        self._last_decision_ts: Optional[float] = None
        self._total_decisions: int = 0
        self._agreement_history: list[bool] = []  # last 20 agreement values
        self._pending: dict[str, float] = {}  # market_id → shadow_log row index

        # Tracking processed CSV rows (by (market_id, timestamp) tuples)
        self._processed_entry_keys: set[tuple[str, str]] = set()
        self._processed_exit_keys: set[tuple[str, str]] = set()

        # In-memory shadow log for API access (capped at 1000 rows)
        self._shadow_rows: list[dict] = []

        # File lock for CSV writes
        self._write_lock = asyncio.Lock()

    # ── Public scoring API ────────────────────────────────────────────────────

    def score_entry(self, market_id: str, context: Optional[dict] = None) -> float:
        """
        Return Model A probability for a Momentum entry decision.

        Returns 0.5 (neutral) on any error or if model is not loaded.
        Never raises.
        """
        if not config.MODEL_AGENT_ENABLED:
            return 0.5
        try:
            model = self._load_model_a()
            if model is None:
                return 0.5
            features = build_entry_snapshot(context or {})
            return self._predict(model, features, MODEL_A_FEATURES)
        except Exception as exc:
            log.warning("ModelAgent.score_entry error", market_id=market_id, exc=str(exc))
            return 0.5

    def score_exit(self, market_id: str, context: Optional[dict] = None) -> float:
        """
        Return Model B probability for an exit quality decision.

        Returns 0.5 (neutral) on any error or if model is not loaded.
        Never raises.
        """
        if not config.MODEL_AGENT_ENABLED:
            return 0.5
        try:
            model = self._load_model_b()
            if model is None:
                return 0.5
            features = build_exit_snapshot(context or {})
            return self._predict(model, features, MODEL_B_FEATURES)
        except Exception as exc:
            log.warning("ModelAgent.score_exit error", market_id=market_id, exc=str(exc))
            return 0.5

    # ── API state accessors ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current runtime status dict for GET /model/status."""
        rate: Optional[float] = None
        if len(self._agreement_history) >= 20:
            rate = round(sum(self._agreement_history[-20:]) / 20, 4)
        return {
            "enabled": config.MODEL_AGENT_ENABLED,
            "status": self._status,
            "last_decision_ts": self._last_decision_ts,
            "agreement_rate_last_20": rate,
            "total_decisions": self._total_decisions,
            "pending_outcomes": len(self._pending),
        }

    def get_shadow_log(self, limit: int = 50, decision_type: str = "all") -> dict:
        """Return last N rows of shadow log, newest first, optionally filtered."""
        rows = self._shadow_rows
        if decision_type != "all":
            rows = [r for r in rows if r.get("decision_type") == decision_type]
        sliced = rows[-limit:][::-1]  # newest first
        return {"rows": sliced, "total": len(rows)}

    # ── Main async loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Background coroutine started by main.py.
        Exits immediately if MODEL_AGENT_ENABLED=False.
        """
        if not config.MODEL_AGENT_ENABLED:
            log.info("ModelAgent disabled — shadow logging inactive")
            return

        log.info("ModelAgent starting shadow loop")
        self._status = "RUNNING"

        # Ensure shadow_log.csv header exists
        await self._ensure_header()

        # Seed already-processed rows so we don't re-log existing fill history
        await self._seed_processed_keys()

        while True:
            try:
                await self._process_new_entries()
                await self._resolve_pending_outcomes()
                await asyncio.sleep(POLL_INTERVAL_SECS)
            except asyncio.CancelledError:
                log.info("ModelAgent cancelled")
                self._status = "DISABLED"
                break
            except Exception as exc:
                log.warning("ModelAgent loop error", exc=str(exc))
                self._status = "ERROR"
                await asyncio.sleep(POLL_INTERVAL_SECS)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_model_a(self) -> Any:
        """Lazy-load Model A pkl. Returns None if file not found."""
        if not self._model_a_loaded:
            self._model_a_loaded = True
            path = Path(config.MODEL_A_PATH)
            if path.exists():
                try:
                    import pickle
                    with open(path, "rb") as fh:
                        self._model_a = pickle.load(fh)
                    log.info("ModelAgent: Model A loaded", path=str(path))
                except Exception as exc:
                    log.warning("ModelAgent: failed to load Model A", exc=str(exc))
        return self._model_a

    def _load_model_b(self) -> Any:
        """Lazy-load Model B pkl. Returns None if file not found."""
        if not self._model_b_loaded:
            self._model_b_loaded = True
            path = Path(config.MODEL_B_PATH)
            if path.exists():
                try:
                    import pickle
                    with open(path, "rb") as fh:
                        self._model_b = pickle.load(fh)
                    log.info("ModelAgent: Model B loaded", path=str(path))
                except Exception as exc:
                    log.warning("ModelAgent: failed to load Model B", exc=str(exc))
        return self._model_b

    def _predict(self, model: Any, features: dict, feature_list: list[str]) -> float:
        """Run inference and return P(class=1). Raises on failure (caller catches)."""
        try:
            import pandas as pd
        except ImportError:
            return 0.5
        row_df = pd.DataFrame([{f: features.get(f, _SENTINEL) for f in feature_list}])
        proba = model.predict_proba(row_df)
        return float(proba[0][1])

    async def _ensure_header(self) -> None:
        """Write CSV header if shadow_log.csv does not yet exist."""
        if not _SHADOW_LOG_PATH.exists():
            _SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            async with self._write_lock:
                with open(_SHADOW_LOG_PATH, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=_SHADOW_COLS)
                    writer.writeheader()

    async def _seed_processed_keys(self) -> None:
        """
        Read existing shadow_log.csv and seed _processed_entry_keys /
        _processed_exit_keys so we don't re-log history on restart.
        """
        if not _SHADOW_LOG_PATH.exists():
            return
        try:
            with open(_SHADOW_LOG_PATH, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    mid = row.get("market_id", "")
                    ts = row.get("timestamp", "")
                    dtype = row.get("decision_type", "")
                    if dtype == "entry":
                        self._processed_entry_keys.add((mid, ts))
                    elif dtype == "exit":
                        self._processed_exit_keys.add((mid, ts))
                    # Populate in-memory log
                    self._shadow_rows.append(row)
                    outcome = row.get("actual_outcome", "PENDING")
                    if outcome == "PENDING" and mid:
                        # Track row index for potential resolution
                        self._pending[mid] = len(self._shadow_rows) - 1
                # Trim to cap
                if len(self._shadow_rows) > 1000:
                    self._shadow_rows = self._shadow_rows[-1000:]
        except Exception as exc:
            log.warning("ModelAgent: could not seed processed keys", exc=str(exc))

    async def _process_new_entries(self) -> None:
        """
        Read on_fills.csv (exit-type, since ON fills capture exit data too) and
        momentum_fills.csv (entry rows) for new rows, score them, and write to shadow_log.
        """
        # ── Momentum entry fills ──────────────────────────────────────────────
        await self._process_csv_for_entries(
            _MOMENTUM_FILLS_PATH,
            decision_type="entry",
            processed_keys=self._processed_entry_keys,
            row_filter=lambda r: r.get("row_type", "entry") == "entry",
        )

        # ── ON fills → treat as exit decisions (exit-check context) ──────────
        await self._process_csv_for_entries(
            _ON_FILLS_PATH,
            decision_type="exit",
            processed_keys=self._processed_exit_keys,
            row_filter=lambda r: True,  # all ON fill rows are exit-related
        )

    async def _process_csv_for_entries(
        self,
        path: Path,
        decision_type: str,
        processed_keys: set[tuple[str, str]],
        row_filter: Any,
    ) -> None:
        """Process a fills CSV for new rows and log shadow decisions."""
        if not path.exists():
            return
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    mid = row.get("market_id", "")
                    ts = row.get("timestamp", "")
                    key = (mid, ts)
                    if key in processed_keys or not mid:
                        continue
                    if not row_filter(row):
                        continue
                    processed_keys.add(key)
                    await self._log_decision_from_row(row, decision_type)
        except Exception as exc:
            log.warning("ModelAgent: error processing CSV", path=str(path), exc=str(exc))

    async def _log_decision_from_row(self, row: dict, decision_type: str) -> None:
        """Score a fill row and write a shadow log entry."""
        try:
            mid = row.get("market_id", "")
            ts_now = str(time.time())

            # Score both models
            if decision_type == "entry":
                features = build_entry_snapshot(row)
                a_score = self.score_entry(mid, row)
                b_score: Optional[float] = None
                rules_decision = "enter"
                # Model agrees if it also would enter (score > 0.5)
                model_decision_enter = a_score >= 0.5
                agreed = model_decision_enter
            else:
                features = build_exit_snapshot(row)
                a_score = None
                b_score = self.score_exit(mid, row)
                rules_decision = "exit"
                # Model agrees if it also would exit (score >= 0.5 means genuine drain)
                model_decision_exit = b_score >= 0.5
                agreed = model_decision_exit

            model_decision = "agree" if agreed else "disagree"

            shadow_row = {
                "timestamp": ts_now,
                "market_id": mid,
                "market_type": row.get("market_type", ""),
                "decision_type": decision_type,
                "rules_decision": rules_decision,
                "model_a_score": f"{a_score:.4f}" if a_score is not None else "",
                "model_b_score": f"{b_score:.4f}" if b_score is not None else "",
                "model_decision": model_decision,
                "agreed": "true" if agreed else "false",
                "actual_outcome": "PENDING",
                "features_snapshot": json.dumps(features),
            }

            await self._write_shadow_row(shadow_row)

            # Track pending outcomes for later resolution
            if mid:
                self._pending[mid] = len(self._shadow_rows) - 1

            # Update runtime counters
            self._last_decision_ts = float(ts_now)
            self._total_decisions += 1
            self._agreement_history.append(agreed)
            if len(self._agreement_history) > 200:
                self._agreement_history = self._agreement_history[-200:]

        except Exception as exc:
            log.warning("ModelAgent: error logging decision", exc=str(exc))

    async def _resolve_pending_outcomes(self) -> None:
        """
        Read trades.csv for rows with resolved_outcome=WIN/LOSS and update any
        PENDING shadow log rows for the same market_id.
        """
        if not self._pending or not _TRADES_PATH.exists():
            return
        try:
            resolved: dict[str, str] = {}  # market_id → "WIN" | "LOSS"
            with open(_TRADES_PATH, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    outcome = row.get("resolved_outcome", "").upper()
                    mid = row.get("market_id", "")
                    if outcome in ("WIN", "LOSS") and mid:
                        resolved[mid] = outcome

            to_remove: list[str] = []
            for mid, _ in list(self._pending.items()):
                if mid in resolved:
                    await self._update_outcome(mid, resolved[mid])
                    to_remove.append(mid)

            for mid in to_remove:
                self._pending.pop(mid, None)

        except Exception as exc:
            log.warning("ModelAgent: error resolving outcomes", exc=str(exc))

    async def _update_outcome(self, market_id: str, outcome: str) -> None:
        """Rewrite shadow_log.csv rows for a market_id from PENDING to WIN/LOSS."""
        if not _SHADOW_LOG_PATH.exists():
            return
        try:
            rows: list[dict] = []
            with open(_SHADOW_LOG_PATH, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            changed = False
            for row in rows:
                if (row.get("market_id") == market_id
                        and row.get("actual_outcome") == "PENDING"):
                    row["actual_outcome"] = outcome
                    changed = True

            if not changed:
                return

            async with self._write_lock:
                with open(_SHADOW_LOG_PATH, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=_SHADOW_COLS)
                    writer.writeheader()
                    writer.writerows(rows)

            # Update in-memory rows too
            for r in self._shadow_rows:
                if (r.get("market_id") == market_id
                        and r.get("actual_outcome") == "PENDING"):
                    r["actual_outcome"] = outcome

        except Exception as exc:
            log.warning("ModelAgent: error updating outcome", market_id=market_id, exc=str(exc))

    async def _write_shadow_row(self, row: dict) -> None:
        """Append one row to shadow_log.csv and update in-memory list."""
        async with self._write_lock:
            with open(_SHADOW_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_SHADOW_COLS)
                writer.writerow(row)

        self._shadow_rows.append(row)
        if len(self._shadow_rows) > 1000:
            self._shadow_rows = self._shadow_rows[-1000:]
