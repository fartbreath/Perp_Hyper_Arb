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
_MODEL_D_LOG_PATH = _ANALYSIS_DIR / "model_d_log.csv"  # ML-D4: separate log (avoids shadow_log schema churn)
_ON_FILLS_PATH = _DATA_DIR / "on_fills.csv"
_MOMENTUM_FILLS_PATH = _DATA_DIR / "momentum_fills.csv"
_ACCT_LEDGER_PATH = _DATA_DIR / "acct_ledger.csv"
_PAPER_TRADES_PATH = _ANALYSIS_DIR / "model_paper_trades.csv"  # ML-08

# ── Shadow log schema ─────────────────────────────────────────────────────────

_SHADOW_COLS = [
    "timestamp",
    "market_id",
    "market_type",       # e.g. "bucket_5m" — from fill row, used for filtering
    "decision_type",    # "entry" | "exit"
    "rules_decision",   # "enter" | "exit" | "skip" | "hold"
    "model_a_score",
    "model_b_score",
    "model_c_score",    # ML-C2: CLOB/oracle divergence calibrator score (None when disabled)
    "model_decision",   # "agree" | "disagree"
    "agreed",           # "true" | "false"
    "actual_outcome",   # "PENDING" | "WIN" | "LOSS"
    "features_snapshot",  # JSON string
]

# ML-D4: separate log schema (separate file avoids shadow_log CSV schema churn)
_MODEL_D_COLS = [
    "timestamp",
    "market_id",
    "market_type",
    "decision_type",     # "entry" (config policy applies at entry)
    "delta_z_score",     # recommended z_score adjustment
    "delta_kelly",       # recommended kelly multiplier adjustment
    "delta_sl",          # recommended delta_sl_pct adjustment
    "context_snapshot",  # JSON of input context features
]

POLL_INTERVAL_SECS: float = 10.0
_SENTINEL = -999.0

# ── ML-08: paper trades schema (model_paper_trades.csv) ──────────────────────
# Written exclusively by _independent_scan_loop; never touches rules-bot files.
_PAPER_COLS = [
    "timestamp",
    "market_id",
    "market_title",
    "underlying",
    "market_type",
    "side",
    "entry_price",
    "size_usd",
    "model_a_score",
    "features_json",
    "status",                   # proposed | closed
    "exit_price",               # resolved YES price: 1.0 (WIN) | 0.0 (LOSS)
    "pnl",                      # exit_price - entry_price per contract
    "would_rules_have_entered", # true | false
    "tte_seconds_at_entry",
]


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
        self._model_c: Any = None
        self._model_d: Any = None  # ML-D4: dict bundle {"models": {dim: regressor}, ...}
        self._model_a_loaded: bool = False
        self._model_b_loaded: bool = False
        self._model_c_loaded: bool = False
        self._model_d_loaded: bool = False
        self._model_d_log_lock: Optional[asyncio.Lock] = None  # lazy init (needs event loop)

        # Runtime state
        self._status: str = "DISABLED"
        self._last_decision_ts: Optional[float] = None
        self._total_decisions: int = 0
        self._agreement_history: list[bool] = []  # last 20 agreement values
        self._pending: dict[str, str] = {}  # market_id → decision_type

        # Tracking processed CSV rows (by (market_id, timestamp) tuples)
        self._processed_entry_keys: set[tuple[str, str]] = set()
        self._processed_exit_keys: set[tuple[str, str]] = set()

        # In-memory shadow log for API access (capped at 1000 rows)
        self._shadow_rows: list[dict] = []

        # File lock for CSV writes
        self._write_lock = asyncio.Lock()

        # ML-08: independent entry scan state
        self._momentum_scanner: Any = None          # injected by main.py after scanner creation
        self._paper_positions: dict[str, dict] = {} # market_id → open proposed row
        self._paper_rows: list[dict] = []           # in-memory ring buffer (last 200 rows)
        self._paper_write_lock = asyncio.Lock()

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

    def score_divergence(self, market_id: str, context: Optional[dict] = None) -> Optional[float]:
        """
        ML-C2: Return Model C calibrated P(WIN) for an exit decision.

        Uses CLOB bid deltas and oracle context to output a continuous 0–1 probability.
        Returns None when MODEL_C_ENABLED=False or model not loaded.
        Never raises.
        """
        if not getattr(config, "MODEL_C_ENABLED", False):
            return None
        if not config.MODEL_AGENT_ENABLED:
            return None
        try:
            from analysis.train_model import MODEL_C_FEATURES
            model = self._load_model_c()
            if model is None:
                return None
            features = build_exit_snapshot(context or {})
            return self._predict(model, features, MODEL_C_FEATURES)
        except Exception as exc:
            log.warning("ModelAgent.score_divergence error", market_id=market_id, exc=str(exc))
            return None

    def score_config_policy(self, market_id: str, context: Optional[dict] = None) -> Optional[dict]:
        """
        ML-D4: Return Model D recommended config deltas for an entry decision.

        Returns dict with keys: delta_z_score, delta_kelly, delta_sl (each float).
        Returns None when MODEL_D_ENABLED=False, model not loaded, or on error.
        Deltas are clamped to ±MODEL_D_MAX_DELTA_PCT of 1.0 (relative).
        Never raises.
        """
        if not getattr(config, "MODEL_D_ENABLED", False):
            return None
        if not config.MODEL_AGENT_ENABLED:
            return None
        try:
            from analysis.train_model import MODEL_D_FEATURES
            bundle = self._load_model_d()
            if bundle is None:
                return None
            models = bundle.get("models", {})
            if not models:
                return None
            features = build_entry_snapshot(context or {})
            max_delta = float(getattr(config, "MODEL_D_MAX_DELTA_PCT", 0.5))
            result: dict = {}
            import pandas as pd
            for dim in ("z_score", "kelly", "delta_sl"):
                model_dim = models.get(dim)
                if model_dim is None:
                    result[f"delta_{dim}"] = 0.0
                    continue
                row_df = pd.DataFrame([{f: features.get(f, -999.0) for f in MODEL_D_FEATURES}])
                raw_delta = float(model_dim.predict(row_df)[0])
                clamped = max(-max_delta, min(max_delta, raw_delta))
                result[f"delta_{dim}"] = round(clamped, 4)
            return result
        except Exception as exc:
            log.warning("ModelAgent.score_config_policy error", market_id=market_id, exc=str(exc))
            return None

    # ── API state accessors ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current runtime status dict for GET /model/status."""
        rate: Optional[float] = None
        if len(self._agreement_history) >= 20:
            rate = round(sum(self._agreement_history[-20:]) / 20, 4)

        # Accuracy: among resolved rows, how often did the model predict correctly?
        # Model A (entry): predicts WIN if score >= 0.5 → correct when outcome=WIN
        # Model B (exit):  predicts "bad exit" if score < 0.5 → correct when outcome=LOSS
        accuracy_a: Optional[float] = None
        accuracy_b: Optional[float] = None
        resolved_a = [
            r for r in self._shadow_rows
            if r.get("decision_type") == "entry"
            and r.get("actual_outcome") in ("WIN", "LOSS")
            and r.get("model_a_score") not in (None, "")
        ]
        resolved_b = [
            r for r in self._shadow_rows
            if r.get("decision_type") in ("exit", "model_b_suppressed")
            and r.get("actual_outcome") in ("WIN", "LOSS")
            and r.get("model_b_score") not in (None, "")
        ]
        if resolved_a:
            correct_a = sum(
                1 for r in resolved_a
                if (float(r["model_a_score"]) >= 0.5) == (r["actual_outcome"] == "WIN")
            )
            accuracy_a = round(correct_a / len(resolved_a), 4)
        if resolved_b:
            correct_b = sum(
                1 for r in resolved_b
                if (float(r["model_b_score"]) < 0.5) == (r["actual_outcome"] == "LOSS")
            )
            accuracy_b = round(correct_b / len(resolved_b), 4)

        return {
            "enabled": config.MODEL_AGENT_ENABLED,
            "status": self._status,
            "last_decision_ts": self._last_decision_ts,
            "agreement_rate_last_20": rate,
            "total_decisions": self._total_decisions,
            "pending_outcomes": len(self._pending),
            "model_a_accuracy": accuracy_a,
            "model_b_accuracy": accuracy_b,
            "model_a_resolved": len(resolved_a),
            "model_b_resolved": len(resolved_b),
        }

    def get_shadow_log(self, limit: int = 50, decision_type: str = "all") -> dict:
        """Return last N rows of shadow log, newest first, optionally filtered."""
        rows = self._shadow_rows
        if decision_type != "all":
            rows = [r for r in rows if r.get("decision_type") == decision_type]
        sliced = rows[-limit:][::-1]  # newest first
        return {"rows": sliced, "total": len(rows)}

    async def log_model_b_suppression(
        self,
        market_id: str,
        market_type: str,
        score: float,
        context: Optional[dict],
    ) -> None:
        """
        ML-06: Write a shadow_log row for a Model-B-suppressed exit.

        Called from ON scanner when MODEL_B_ENABLED=True and score < threshold.
        decision_type='model_b_suppressed', rules_decision='exit', agreed='false'.
        Never raises — all exceptions logged at WARNING.
        """
        try:
            import json as _json
            features: dict = {}
            try:
                from models.feature_snapshot import build_exit_snapshot
                features = build_exit_snapshot(context or {})
            except Exception:
                pass
            shadow_row = {
                "timestamp": str(time.time()),
                "market_id": market_id,
                "market_type": market_type,
                "decision_type": "model_b_suppressed",
                "rules_decision": "exit",
                "model_a_score": "",
                "model_b_score": f"{score:.4f}",
                "model_c_score": "",
                "model_decision": "suppress",
                "agreed": "false",
                "actual_outcome": "PENDING",
                "features_snapshot": _json.dumps(features),
            }
            await self._write_shadow_row(shadow_row)
            if market_id:
                self._pending[market_id] = "model_b_suppressed"
        except Exception as exc:
            log.warning("ModelAgent.log_model_b_suppression error", market_id=market_id, exc=str(exc))

    def get_paper_trades(self, limit: int = 100, independent_only: bool = False) -> dict:
        """
        Return ML-08 paper trades and win-rate summary for GET /model/paper_trades.

        independent_only=True filters to rows where would_rules_have_entered=false,
        i.e. the genuine additive-alpha set.
        """
        rows = self._paper_rows
        if independent_only:
            rows = [r for r in rows if r.get("would_rules_have_entered") == "false"]
        sliced = list(reversed(rows[-limit:]))  # newest first

        def _win_rate(subset: list[dict]) -> Optional[float]:
            if not subset:
                return None
            wins = sum(1 for r in subset if _safe_float_str(r.get("pnl")) > 0)
            return round(wins / len(subset), 4)

        def _safe_float_str(v: Any) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return -999.0

        closed = [r for r in self._paper_rows if r.get("status") == "closed"]
        model_only_closed = [r for r in closed if r.get("would_rules_have_entered") == "false"]
        rules_eligible_closed = [r for r in closed if r.get("would_rules_have_entered") == "true"]

        return {
            "rows": sliced,
            "total": len(self._paper_rows),
            "open": len(self._paper_positions),
            "closed": len(closed),
            "model_only_win_rate": _win_rate(model_only_closed),
            "rules_eligible_win_rate": _win_rate(rules_eligible_closed),
        }

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

        # ML-08: start independent scan as a sibling task (runs while shadow loop runs)
        asyncio.create_task(self._independent_scan_loop(), name="model_independent_scan")

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

    def _load_model_c(self) -> Any:
        """Lazy-load Model C pkl (ML-C2). Returns None if disabled or file not found."""
        if not self._model_c_loaded:
            self._model_c_loaded = True
            path = Path(getattr(config, "MODEL_C_PATH", ""))
            if path and path.exists():
                try:
                    import pickle
                    with open(path, "rb") as fh:
                        self._model_c = pickle.load(fh)
                    log.info("ModelAgent: Model C loaded", path=str(path))
                except Exception as exc:
                    log.warning("ModelAgent: failed to load Model C", exc=str(exc))
        return self._model_c

    def _load_model_d(self) -> Any:
        """Lazy-load Model D bundle pkl (ML-D4). Returns None if disabled or file not found."""
        if not self._model_d_loaded:
            self._model_d_loaded = True
            path = Path(getattr(config, "MODEL_D_PATH", ""))
            if path and path.exists():
                try:
                    import pickle
                    with open(path, "rb") as fh:
                        self._model_d = pickle.load(fh)
                    dims = list((self._model_d or {}).get("models", {}).keys())
                    log.info("ModelAgent: Model D loaded", path=str(path), dims=dims)
                except Exception as exc:
                    log.warning("ModelAgent: failed to load Model D", exc=str(exc))
        return self._model_d

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
                        # Track decision_type for outcome resolution
                        self._pending[mid] = dtype
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

            # Score all models
            if decision_type == "entry":
                features = build_entry_snapshot(row)
                a_score = self.score_entry(mid, row)
                b_score: Optional[float] = None
                c_score: Optional[float] = None
                d_policy: Optional[dict] = self.score_config_policy(mid, row)  # ML-D4
                rules_decision = "enter"
                # Model agrees if it also would enter (score > 0.5)
                model_decision_enter = a_score >= 0.5
                agreed = model_decision_enter
            else:
                features = build_exit_snapshot(row)
                a_score = None
                b_score = self.score_exit(mid, row)
                c_score = self.score_divergence(mid, row)  # ML-C2: may be None
                d_policy = None  # config policy only applies at entry
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
                "model_c_score": f"{c_score:.4f}" if c_score is not None else "",
                "model_decision": model_decision,
                "agreed": "true" if agreed else "false",
                "actual_outcome": "PENDING",
                "features_snapshot": json.dumps(features),
            }

            await self._write_shadow_row(shadow_row)

            # ML-D4: write Model D policy recommendation to separate log (entry only)
            if d_policy is not None and decision_type == "entry":
                await self._write_model_d_row({
                    "timestamp": ts_now,
                    "market_id": mid,
                    "market_type": row.get("market_type", ""),
                    "decision_type": decision_type,
                    "delta_z_score": f"{d_policy.get('delta_z_score', 0.0):.4f}",
                    "delta_kelly": f"{d_policy.get('delta_kelly', 0.0):.4f}",
                    "delta_sl": f"{d_policy.get('delta_sl', 0.0):.4f}",
                    "context_snapshot": json.dumps({f: features.get(f) for f in list(features.keys())[:20]}),
                })

            # Track pending outcomes for later resolution
            if mid:
                self._pending[mid] = decision_type

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
        Read acct_ledger.csv for rows with resolved_outcome=WIN/LOSS and update any
        PENDING shadow log rows for the same market_id.

        Outcome assignment:
        - entry (Momentum): resolved_outcome from the single acct_ledger row for
          that market_id is assigned directly (WIN = trade won, LOSS = trade lost).
        - exit / model_b_suppressed (OpeningNeutral): the loser_leg is looked up in
          on_fills.csv, then the loser token's resolved_outcome is read from
          acct_ledger. If the loser resolved to LOSS (correct identification) the
          model outcome is WIN; if the loser resolved to WIN (wrong leg exited)
          the model outcome is LOSS.
        """
        if not self._pending or not _ACCT_LEDGER_PATH.exists():
            return
        try:
            # Build per-market, per-side outcome map from acct_ledger
            # al_outcomes[market_id][SIDE] = "WIN" | "LOSS"
            al_outcomes: dict[str, dict[str, str]] = {}
            with open(_ACCT_LEDGER_PATH, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    mid = row.get("market_id", "")
                    side = row.get("side", "").upper()
                    outcome = row.get("resolved_outcome", "").upper()
                    if mid and side and outcome in ("WIN", "LOSS"):
                        al_outcomes.setdefault(mid, {})[side] = outcome

            # Build loser_leg map from on_fills.csv for ON exit labelling
            loser_leg_by_mid: dict[str, str] = {}
            if _ON_FILLS_PATH.exists():
                with open(_ON_FILLS_PATH, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        mid = row.get("market_id", "")
                        loser_leg = row.get("loser_leg", "").upper()
                        if mid and loser_leg:
                            loser_leg_by_mid[mid] = loser_leg

            to_remove: list[str] = []
            for mid, decision_type in list(self._pending.items()):
                if mid not in al_outcomes:
                    continue

                model_outcome: Optional[str] = None
                sides = al_outcomes[mid]

                if decision_type == "entry":
                    # Momentum: one leg per market — use the single resolved outcome
                    if len(sides) == 1:
                        model_outcome = next(iter(sides.values()))
                    else:
                        # Multiple legs (ON pair matched against a momentum shadow row)
                        # Prefer the first non-empty; this is an unusual edge case
                        model_outcome = next(iter(sides.values()))

                else:
                    # exit / model_b_suppressed: ON loser exit labelling
                    loser_leg = loser_leg_by_mid.get(mid, "")
                    if not loser_leg:
                        # loser_leg not recorded yet — wait for on_fills to be written
                        continue
                    loser_resolved = sides.get(loser_leg, "")
                    if not loser_resolved:
                        continue
                    # Correct exit (loser resolved to 0 = LOSS) → model made good call
                    model_outcome = "WIN" if loser_resolved == "LOSS" else "LOSS"

                if model_outcome:
                    await self._update_outcome(mid, model_outcome)
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

    async def _write_model_d_row(self, row: dict) -> None:
        """ML-D4: append one row to model_d_log.csv (separate file for schema hygiene)."""
        if self._model_d_log_lock is None:
            self._model_d_log_lock = asyncio.Lock()
        async with self._model_d_log_lock:
            write_header = not _MODEL_D_LOG_PATH.exists()
            _MODEL_D_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_MODEL_D_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_MODEL_D_COLS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

    def get_model_d_log(self, limit: int = 100) -> dict:
        """Return last N rows of model_d_log.csv, newest first."""
        if not _MODEL_D_LOG_PATH.exists():
            return {"rows": [], "total": 0}
        try:
            with open(_MODEL_D_LOG_PATH, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            sliced = rows[-limit:][::-1]
            return {"rows": sliced, "total": len(rows)}
        except Exception as exc:
            log.warning("ModelAgent: error reading model_d_log", exc=str(exc))
            return {"rows": [], "total": 0, "error": str(exc)}

    # ── ML-08: independent entry scan (paper trades) ──────────────────────────

    async def _independent_scan_loop(self) -> None:
        """
        ML-08 independent entry scan loop.

        Evaluates ALL PM markets via score_entry() without the rules pre-filters
        (no z-score gate, no funding gate, no TWAP gate).  When
        MODEL_A_INDEPENDENT_ENABLED=True and score > MODEL_A_INDEPENDENT_ENTRY_THRESHOLD,
        logs a paper trade to analysis/model_paper_trades.csv.

        Runs at the same tick frequency as the Momentum scanner.
        Does nothing when MODEL_AGENT_ENABLED=False.
        """
        if self._pm_client is None:
            log.info("ML-08: pm_client not wired — independent scan loop exiting")
            return

        await self._ensure_paper_trades_header()
        await self._seed_paper_positions()
        log.info("ML-08: independent scan loop started")

        while True:
            try:
                if getattr(config, "MODEL_A_INDEPENDENT_ENABLED", False):
                    await self._run_independent_scan_tick()
                    await self._resolve_paper_outcomes()
            except asyncio.CancelledError:
                log.info("ML-08: independent scan cancelled")
                raise
            except Exception as exc:
                log.warning("ML-08: scan tick error", exc=str(exc))

            scan_interval = float(getattr(config, "MOMENTUM_SCAN_INTERVAL", 10))
            await asyncio.sleep(scan_interval)

    async def _run_independent_scan_tick(self) -> None:
        """Score all PM markets and propose paper entries above the threshold."""
        import datetime as _dt

        now_ts = time.time()
        now_dt = _dt.datetime.utcnow()
        max_open = getattr(config, "MODEL_A_MAX_OPEN_POSITIONS", 5)
        threshold = getattr(config, "MODEL_A_INDEPENDENT_ENTRY_THRESHOLD", 0.7)
        min_tte = getattr(config, "MODEL_A_MIN_TTE_SECS", 30)

        if len(self._paper_positions) >= max_open:
            log.debug("ML-08: paper cap reached — skipping tick", open=len(self._paper_positions))
            return

        # Snapshot Momentum scanner diags for would_rules_have_entered determination.
        # Markets with effective_gap_pct >= 0 passed the rules delta gate this tick.
        _diag_by_market: dict[str, dict] = {}
        ms = self._momentum_scanner
        if ms is not None:
            for d in getattr(ms, "_last_scan_diags", []):
                mid = d.get("market_id")
                if mid:
                    _diag_by_market[mid] = d

        proposed_this_tick = 0
        _state_skips = frozenset({
            "concurrent_cap", "duplicate_position", "cooldown",
            "stop_loss_block", "opening_neutral_active",
        })

        for _scan_i, market in enumerate(list(self._pm_client.get_markets().values())):
            # Yield every 50 markets so websocket keepalive pings (Chainlink
            # ping_interval=2s, ping_timeout=20s) can be processed.  Without
            # this yield, iterating ~4000 markets × ~2-5ms sklearn predict_proba
            # blocks the asyncio loop for 8-20s, starving pong handlers and
            # triggering reconnect cycles.
            if _scan_i % 50 == 0:
                await asyncio.sleep(0)
            if len(self._paper_positions) + proposed_this_tick >= max_open:
                break

            if market.condition_id in self._paper_positions:
                continue

            # TTE gate — hard minimum, no model override
            if market.end_date is None:
                continue
            tte_secs = market.end_date.timestamp() - now_ts
            if tte_secs < min_tte:
                continue

            # Build context from diag data + live enrichment
            diag = _diag_by_market.get(market.condition_id, {})
            _funding_rate = None
            _twap_dev_bps = diag.get("twap_dev_bps")
            _vol_regime = diag.get("vol_regime")

            if self._funding_cache is not None and market.underlying:
                try:
                    _funding_rate = self._funding_cache.get(market.underlying)
                except Exception:
                    pass

            if self._oracle_tracker is not None and market.underlying:
                try:
                    if _twap_dev_bps is None:
                        _twap_dev_bps = self._oracle_tracker.get_twap_deviation_bps(market.underlying)
                    if not _vol_regime or _vol_regime == "UNKNOWN":
                        _vol_regime = self._oracle_tracker.get_vol_regime(market.underlying)
                except Exception:
                    pass

            context: dict = {
                "signal_obs_z":     diag.get("observed_z"),
                "signal_sigma_ann": diag.get("sigma_ann"),
                "oracle_delta_pct": diag.get("delta_pct"),
                "signal_delta_pct": diag.get("delta_pct"),
                "tte_seconds":      round(tte_secs),
                "tte_seconds_at_entry": round(tte_secs),
                "timestamp":        now_ts,
                "funding_rate":     _funding_rate,
                "twap_dev_bps":     _twap_dev_bps,
                "vol_regime":       _vol_regime,
                "hour_utc":         now_dt.hour,
                "day_of_week":      now_dt.weekday(),
            }

            score = self.score_entry(market.condition_id, context)
            if score <= threshold:
                continue

            # would_rules_have_entered: True iff the market passed the delta gate
            # in the rules scanner's last tick (state-based skips do not count against it).
            _passed_delta = (
                "effective_gap_pct" in diag
                and float(diag.get("effective_gap_pct", -999)) >= 0
            )
            _skip = diag.get("skip_reason")
            would_rules = _passed_delta and (_skip is None or _skip == "" or _skip in _state_skips)

            # Current YES ask as paper entry price
            entry_price: float = 0.0
            try:
                book = self._pm_client.get_book(market.token_id_yes)
                if book is not None:
                    p = book.best_ask or book.mid
                    if p is not None:
                        entry_price = round(float(p), 4)
            except Exception:
                pass

            features_json = "{}"
            try:
                from models.feature_snapshot import build_entry_snapshot
                features_json = json.dumps(build_entry_snapshot(context))
            except Exception:
                pass

            paper_row: dict = {
                "timestamp":               str(now_ts),
                "market_id":               market.condition_id,
                "market_title":            market.title[:80],
                "underlying":              market.underlying,
                "market_type":             market.market_type,
                "side":                    "YES",
                "entry_price":             str(entry_price),
                "size_usd":                "0.0",
                "model_a_score":           str(round(score, 4)),
                "features_json":           features_json,
                "status":                  "proposed",
                "exit_price":              "",
                "pnl":                     "",
                "would_rules_have_entered": "true" if would_rules else "false",
                "tte_seconds_at_entry":    str(round(tte_secs)),
            }
            await self._write_paper_trade_row(paper_row)
            self._paper_positions[market.condition_id] = paper_row
            proposed_this_tick += 1

            log.info(
                "ML-08: paper trade proposed",
                market=market.title[:50],
                market_id=market.condition_id[:16],
                score=round(score, 3),
                threshold=threshold,
                would_rules=would_rules,
                tte_secs=round(tte_secs),
                entry_price=entry_price,
            )

        if proposed_this_tick:
            log.info(
                "ML-08: scan tick",
                proposed=proposed_this_tick,
                total_open=len(self._paper_positions),
            )

    async def _resolve_paper_outcomes(self) -> None:
        """
        For each open paper position whose expected resolution time has elapsed,
        call fetch_market_resolution() (CLOB winner flag) to get the outcome.
        Updates exit_price, pnl, and status=closed in both CSV and in-memory rows.
        """
        if not self._paper_positions or self._pm_client is None:
            return

        now_ts = time.time()
        to_resolve: list[str] = []

        for mid, row in self._paper_positions.items():
            try:
                entry_ts = float(row.get("timestamp", 0))
                tte_at_entry = float(row.get("tte_seconds_at_entry", 0))
                if now_ts > entry_ts + tte_at_entry:
                    to_resolve.append(mid)
            except (TypeError, ValueError):
                pass

        for mid in to_resolve:
            try:
                resolved_yes = await self._pm_client.fetch_market_resolution(mid)
                if resolved_yes is None:
                    continue  # not settled yet — retry on next tick

                row = self._paper_positions[mid]
                entry_price_f = float(row.get("entry_price") or 0)
                exit_price_f = resolved_yes       # 1.0 = YES won, 0.0 = NO won
                pnl_f = round(exit_price_f - entry_price_f, 4)

                await self._update_paper_trade_closed(mid, exit_price_f, pnl_f)
                del self._paper_positions[mid]

                log.info(
                    "ML-08: paper trade closed",
                    market_id=mid[:16],
                    exit_price=exit_price_f,
                    pnl=pnl_f,
                    would_rules=row.get("would_rules_have_entered"),
                )
            except Exception as exc:
                log.warning("ML-08: error resolving paper trade", market_id=mid[:16], exc=str(exc))

    async def _ensure_paper_trades_header(self) -> None:
        """Create analysis/model_paper_trades.csv with header if it does not exist."""
        if not _PAPER_TRADES_PATH.exists():
            _PAPER_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
            async with self._paper_write_lock:
                with open(_PAPER_TRADES_PATH, "w", newline="", encoding="utf-8") as fh:
                    csv.DictWriter(fh, fieldnames=_PAPER_COLS).writeheader()

    async def _seed_paper_positions(self) -> None:
        """Load existing proposed positions from model_paper_trades.csv on restart."""
        if not _PAPER_TRADES_PATH.exists():
            return
        try:
            with open(_PAPER_TRADES_PATH, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    self._paper_rows.append(row)
                    if row.get("status") == "proposed" and row.get("market_id"):
                        self._paper_positions[row["market_id"]] = row
            if len(self._paper_rows) > 200:
                self._paper_rows = self._paper_rows[-200:]
            log.info(
                "ML-08: seeded paper positions",
                open=len(self._paper_positions),
                total_rows=len(self._paper_rows),
            )
        except Exception as exc:
            log.warning("ML-08: error seeding paper positions", exc=str(exc))

    async def _write_paper_trade_row(self, row: dict) -> None:
        """Append one row to model_paper_trades.csv and update in-memory list."""
        async with self._paper_write_lock:
            with open(_PAPER_TRADES_PATH, "a", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=_PAPER_COLS, extrasaction="ignore").writerow(row)
        self._paper_rows.append(row)
        if len(self._paper_rows) > 200:
            self._paper_rows = self._paper_rows[-200:]

    async def _update_paper_trade_closed(
        self, market_id: str, exit_price: float, pnl: float
    ) -> None:
        """Rewrite model_paper_trades.csv updating the closed row for market_id."""
        if not _PAPER_TRADES_PATH.exists():
            return
        try:
            with open(_PAPER_TRADES_PATH, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            changed = False
            for row in rows:
                if row.get("market_id") == market_id and row.get("status") == "proposed":
                    row["exit_price"] = str(round(exit_price, 4))
                    row["pnl"] = str(pnl)
                    row["status"] = "closed"
                    changed = True

            if not changed:
                return

            async with self._paper_write_lock:
                with open(_PAPER_TRADES_PATH, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=_PAPER_COLS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(rows)

            # Mirror update into in-memory list
            for r in self._paper_rows:
                if r.get("market_id") == market_id and r.get("status") == "proposed":
                    r["exit_price"] = str(round(exit_price, 4))
                    r["pnl"] = str(pnl)
                    r["status"] = "closed"
        except Exception as exc:
            log.warning(
                "ML-08: error updating paper trade", market_id=market_id[:16], exc=str(exc)
            )
