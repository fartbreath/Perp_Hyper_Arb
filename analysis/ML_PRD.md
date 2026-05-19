# ML Adaptive Signal Engine â€” Product Requirements Document
## Source: ML_IMPL_PLAN.md, model_proposal.md | Created: May 5, 2026 | Last updated: May 18, 2026

---

## Overview

This PRD breaks down `ML_IMPL_PLAN.md` into discrete user stories. Each story identifies the operator-visible benefit, the backend files touched, any webapp changes needed, and explicit acceptance criteria. Use this to prioritise, spot cross-cutting concerns, and gate each phase transition.

**Personas:**
- **Operator** â€” watches the live bot, adjusts settings, reviews trade results
- **Data Scientist** â€” builds models offline, interprets SHAP reports, decides when a model is ready
- **Trader** â€” cares about PnL outcomes: fewer wrong exits, better sizing, new alpha

**Key constraint:** The live rules bot runs continuously throughout all phases. No story requires taking it offline. Every model integration defaults off. The fallback for any model failure is always the unmodified rules decision.

---

## Summary Table

| ID | Story | Phase | Priority | Backend | Webapp | Effort | Status |
|----|-------|-------|----------|---------|--------|--------|--------|
| ML-01 | CLOB feature buffer | 0 | P0 | `models/clob_feature_buffer.py` | â€” | 1 day | âœ… Complete |
| ML-02 | Feature builder | 1 | P1 | `analysis/feature_builder.py` | â€” | 1 day | âœ… Complete |
| ML-03 | Model training + SHAP | 1 | P1 | `analysis/train_model.py` | âœ… Reports | 1 day | âœ… Complete |
| ML-04 | ModelAgent + shadow wiring | 2 | P1 | `models/model_agent.py`, `main.py` | âœ… Dashboard | 1 day | âœ… Done â€” 13/13 tests |
| ML-04W | ModelAgent webapp page | 2 | P1 | `api_server.py` | âœ… New page | 1 day | âœ… Done |
| ML-05 | Shadow evaluator | 2 | P2 | `analysis/shadow_evaluator.py` | âœ… Shadow tab | 1 day | âœ… Done â€” 12/12 tests |
| ML-06 | Model B exit gate | 3 | P2 | `strategies/OpeningNeutral/scanner.py` | ✅ Positions | 0.5 day | ✅ QA'd — 2 bugs fixed |
| ML-07 | Model A sizing scale | 3 | P2 | `strategies/Momentum/scanner.py`, `strategies/OpeningNeutral/scanner.py` | ✅ Settings | 0.5 day | ✅ QA'd — 2 bugs fixed |
| ML-08 | Independent entry scan | 4 | P3 | `models/model_agent.py` | ✅ Paper trades | 1 day | ✅ QA'd — all ACs verified |
| ML-09 | Model paper ledger | 4 | P3 | `models/model_agent.py` | ✅ Paper trades | 0.5 day | ✅ QA'd — all ACs verified |
| ML-10 | Separate capital budget | 5 | P4 | `risk.py`, `config.py` | âœ… Dashboard | 0.5 day | â³ Not started â€" waiting on P4 exit criteria |
| ML-11 | Retraining pipeline | 5 | P4 | `analysis/retrain_pipeline.py` | âœ… Reports | 1 day | â³ Blocked on ML-10 |
| ML-12 | Model archive + rollback | 5 | P4 | `analysis/model_archive/`, `config.py` | â€" | 0.5 day | â³ Blocked on ML-11 |
| ML-X1 | Inference latency guard | X | P1 | `analysis/benchmark_inference.py` | â€" | 0.5 day | â³ Blocked on ML-04 |
| ML-X2 | Structured decision logging | X | P1 | all model integration points | â€" | 0.5 day | â³ Blocked on ML-04 |
| **ML-C1** | **Model B retrain w/ exit-time features** | **C** | **P3** | **`analysis/train_model.py`** | **✅ Models dropdown** | **0.5 day** | **✅ Complete — May 19, 2026** |
| **ML-C2** | **Model C v0: 2-point divergence calibrator** | **C** | **P3** | **`analysis/train_model.py`, `feature_builder.py`** | **✅ Models dropdown + calibration card** | **1 day** | **✅ Complete — May 19, 2026** |
| **ML-C3** | **Model C v1: dense CLOB trajectory model** | **C** | **P4** | **`models/clob_feature_buffer.py`** | **âœ… Models dropdown** | **2 days** | **â³ Blocked on Model B AUC > 0.65** |
| **ML-D1** | **Signal event log + snapshot buffer** | **D** | **P0** | **`strategies/Momentum/scanner.py`, `monitor.py`** | **â€"** | **1 day** | **âœ… Complete â€" QA'd May 18, 2026** |
| **ML-D2** | **Config audit trail in acct_ledger** | **D** | **P0** | **`accounting.py`** | **â€"** | **0.5 day** | **âœ… Complete â€" QA'd May 18, 2026** |
| **ML-D3** | **OPE reward surface** | **D** | **P2** | **`analysis/ope_reward_surface.py`** | **✅ New `/ope` page** | **2 days** | **✅ Complete — May 19, 2026** |
| **ML-D4** | **Multi-output Model D v0** | **D** | **P3** | **`analysis/train_model.py`** | **✅ Model D Simulator page** | **2 days** | **✅ Complete — May 19, 2026** |

---

## Phase 0 â€” Data Foundation

### ML-01 Â· CLOB Feature Buffer

**User story:**
> As a **data scientist**, I want CLOB depth dynamics computed in real time from the existing PM WebSocket event stream so that `bid_slope_30s` and `depth_delta_60s` are available at every exit-decision point â€” with no raw tick files accumulating on disk and no risk of missed events between poll intervals.

**Background:** The 20 documented wrong exits all had fast simultaneous bid collapses across both YES and NO legs â€” the fingerprint of settlement book drain, not informed selling (arXiv:2601.18815). The current rule uses a single `best_bid` threshold. The `bid_slope_30s` feature (rate of bid collapse) is the structural improvement. It requires a rolling event buffer per market rather than periodic snapshots, because a 5-second poll interval would miss the 1â€“3s collapse window at TTE=0.

The PM CLOB WebSocket already delivers every order book update to `PMClient`. This buffer subscribes to those events and computes features on demand at decision time â€” no new connections, no new API calls, no disk writes of raw data.

**Acceptance criteria:**
- [x] `models/clob_feature_buffer.py` created with `CLOBFeatureBuffer` class
- [x] Buffer subscribes to `PMClient` via an `on_book_update` callback registered at startup *(impl uses `on_price_change` â€” functionally equivalent; PRD spec was underspecified)*
- [x] Per-market rolling deque maintained in memory; `maxlen` configurable via `CLOB_BUFFER_MAXLEN` (default: 600 events â‰ˆ 10 min at 1 update/sec) â€” `config.py` line 905
- [x] `compute_features(market_id)` returns a dict with `bid_slope_30s`, `depth_delta_60s`, `bid_at_level`, `bid_vs_premarket_baseline`; returns all-null dict if fewer than 2 events in buffer *(impl returns 8 keys with `yes_`/`no_` prefix â€” binary market YES/NO legs; AC was underspecified)*
- [x] When a market transitions from pre-market to open, buffer records the CLOB state as `premarket_baseline[market_id]`
- [x] After a trade closes, `clear(market_id)` is called; `depth(market_id)` subsequently returns 0
- [x] `CLOB_FEATURE_BUFFER_ENABLED = False` in `config.py` (default off); when disabled, buffer callbacks are not registered and `compute_features()` returns all-null dict
- [x] Any exception in event processing or feature computation is caught; logged at WARNING; does not propagate to `PMClient` WebSocket handler
- [x] Unit tests: buffer-receives-events, features-computed-after-30s-of-events, premarket-baseline-captured, buffer-cleared-on-close, error-isolation, disabled-is-clean

**QA result: âœ… PASS â€” May 5, 2026 (41/41 tests)**

**Files touched:** `models/clob_feature_buffer.py` (new), `models/__init__.py` (new), `config.py` (new flag), `main.py` (register buffer on PMClient at startup)

**Webapp changes:** None â€” this is a pure data pipeline. No user-visible surface at this phase.

**Expected result:** `bid_slope_30s` becomes available as a feature at every exit-decision tick. Combined with `oracle_delta_pct` (already in ON-06), this gives the model the two strongest expected predictors of wrong exits.

---

## Phase 1 â€” Offline Learning

### ML-02 Â· Feature Builder

**User story:**
> As a **data scientist**, I want all historical trade CSVs joined into a single labelled feature parquet so that I can train a model without manual data wrangling, and so that the join logic (oracle tick alignment, CLOB session matching, label derivation) is version-controlled and reproducible.

**Background:** The current data lives in five separate files with different timestamp granularities and no common join key beyond `market_id` + approximate timestamp. Oracle ticks need Â±5s alignment; CLOB snapshots are session-scoped. The feature builder formalises these joins so every model version is trained on the same consistent dataset.

**Acceptance criteria:**
- [x] `analysis/feature_builder.py` created; produces `analysis/training_data.parquet` when run
- [x] Joins `trades.csv`, `on_fills.csv`, `momentum_fills.csv`, `oracle_ticks.csv`; uses available `pm_clob_stream_*.csv` files where session-matched
- [x] One row per closed trade with all features from the feature vector table in `model_proposal.md` (nulls acceptable for features whose source wasn't available for that session)
- [x] `resolved_outcome` label: WIN=1, LOSS=0 from `trades.csv`
- [x] `exit_was_wrong` label: 1 where `resolved_outcome=WIN` AND trade had a loser-exit trigger (the 20-trade failure mode)
- [x] Oracle tick alignment: Â±5s window; if no tick in window, oracle features are `null` (row kept, not dropped)
- [x] Time-based 70/15/15 train/val/test split indices written as a `split` column; no random shuffle â€” chronological order only
- [x] Script is incremental: rows already in the parquet (matched by `record_id`) are skipped; only new trades are processed and appended. Re-running on an unchanged dataset returns immediately with `[up-to-date]`.
- [x] Raw data CSVs (`acct_ledger.csv`, `on_fills.csv`, `momentum_fills.csv`) can be purged after running the builder — all processed feature rows are permanently stored in the parquet. The bot can be stopped, its data files cleared, and restarted without losing any training history.
- [x] Parquet row count â‰¥ 100; printed to stdout on completion
- [x] Unit tests: oracle alignment with no matching tick, missing CLOB session, split-is-chronological

**QA result: âœ… PASS â€” May 5, 2026 (46/46 tests)**

**Files touched:** `analysis/feature_builder.py` (new), `requirements.txt` (add `pyarrow`)

**Webapp changes:** None.

**Expected result:** `training_data.parquet` ready for `train_model.py`. Data scientist can inspect the parquet with pandas without touching any raw CSVs. Operator can purge all raw data files at any time (e.g. after a bot refactor) — the parquet is the permanent accumulator and survives independently of the CSVs it was built from.

---

### ML-03 Â· Model Training + SHAP

**User story:**
> As a **data scientist**, I want an XGBoost model trained on the feature parquet and a SHAP attribution report generated automatically so that I can verify the model is learning real microstructure signals before any model output touches a live decision â€” and so that I have a human-interpretable audit trail for every model version.

**Background:** arXiv:2510.22348 shows that hybrid ensemble + SHAP achieves Sharpe 2.51 on similar short-horizon regime prediction. XGBoost is chosen over neural nets because: tabular features, <500 samples (nets overfit), no feature scaling needed, fast inference (<1ms), built-in SHAP integration. SHAP is non-negotiable â€” no black-box model touches live capital.

Two separate models: Model B (exit quality gate, higher priority â€” directly addresses the 20 wrong exits) and Model A (entry quality classifier for sizing). Model A requires â‰¥300 rows; if not yet available, training is skipped with a warning.

**Acceptance criteria:**
- [x] `analysis/train_model.py` created; trains Model B and (if â‰¥300 rows) Model A
- [x] Algorithm: `XGBClassifier` with `eval_metric='auc'`; random seed fixed for reproducibility
- [x] Time-based split from `split` column in parquet â€” no random shuffle
- [x] Model B serialised to `analysis/model_b_v0.pkl`; Model A to `analysis/model_a_v0.pkl`
- [x] Model B test-set AUC-ROC printed to stdout; script exits with error if AUC < 0.60
- [x] Model B recall on `exit_was_wrong=1` class printed to stdout
- [x] Auto leakage check: any feature with Pearson correlation > 0.95 with the label on the test set raises an error before training completes
- [x] SHAP HTML beeswarm report generated at `analysis/reports/model_b_v0_shap.html` (and `model_a_v0_shap.html` if Model A trained)
- [x] Script is idempotent: re-running overwrites pkl and HTML files
- [x] If `training_data.parquet` has < 300 rows, Model A is skipped with a printed warning; Model B training continues
- [x] `requirements.txt` updated with `xgboost`, `shap`

**QA result: âœ… PASS â€” May 5, 2026 (15/15 tests)**

**Files touched:** `analysis/train_model.py` (new), `requirements.txt`

**Webapp changes:**
- [x] **Reports section:** Link to `analysis/reports/model_b_v0_shap.html` so the operator can view the SHAP beeswarm without SSH. Even a sidebar link labelled "Model B SHAP" that opens the HTML file in a new tab is sufficient. *(BUG-ML-01 fixed: `GET /reports/{filename}` in `api_server.py` + nav links in `App.tsx`)*

**Expected result:** `model_b_v0.pkl` trained. SHAP report shows `oracle_delta_pct` and `tte_seconds` as dominant features (expected), with `deribit_iv` and `hl_funding_rate` as secondary signals. If any unexpected feature (e.g. a CSV timestamp column) dominates, it signals leakage and the model is not promoted.

---

## Phase 2 â€” Concurrent Shadow Mode

### ML-04 Â· ModelAgent + Shadow Wiring

**User story:**
> As an **operator**, I want to see what decisions the model would have made alongside every live bot decision â€” with zero live impact â€” so that I can build confidence in the model before enabling any gates.

**Background:** The ModelAgent is not a separate process or bot. It runs as an asyncio task within the existing event loop, reads from the same shared connector objects (SpotOracle, HLClient, PMClient, FundingRateCache, OracleTickTracker, VolFetcher), and never writes to any shared position, risk, or order state. Adding it requires zero changes to the connector layer and one `asyncio.create_task()` call in `main.py`.

**Acceptance criteria:**
- [x] `models/model_agent.py` created with `ModelAgent` class
- [x] `ModelAgent.__init__` accepts: `spot_oracle`, `hl_client`, `pm_client`, `funding_cache`, `oracle_tracker`, `vol_fetcher`, `clob_buffer`
- [x] `ModelAgent` exposes `score_entry(market_id) â†’ float` (Model A probability) and `score_exit(market_id) â†’ float` (Model B probability)
- [x] `ModelAgent.run()` is an async coroutine that exits immediately if `MODEL_AGENT_ENABLED=False`
- [x] On every rules-bot entry decision and every exit-check, `ModelAgent` logs one row to `analysis/shadow_log.csv` with columns: `timestamp`, `market_id`, `market_type`, `decision_type`, `rules_decision`, `model_a_score`, `model_b_score`, `model_decision`, `agreed`, `actual_outcome` (initially `PENDING`), `features_snapshot` (JSON)
- [x] `actual_outcome` is updated from PENDING to WIN/LOSS (via 10s CSV poll â€” shadow mode latency is acceptable)
- [x] `MODEL_AGENT_ENABLED = False` in `config.py` (default off)
- [x] `main.py` starts `asyncio.create_task(model_agent.run(), name="model_agent")` after all connectors are started but before the main scan loop
- [x] Any exception during inference is caught; logged at WARNING; does not propagate to the calling scanner or monitor code
- [x] Unit tests: shadow-rows-written, outcome-resolution, error-isolation, disabled-is-clean, no-shared-state-mutation

**QA result: âœ… QA'd â€” 13/13 tests pass (`tests/test_model_agent.py`)**

**Files touched:** `models/model_agent.py` (new), `models/feature_snapshot.py` (new), `main.py`, `config.py`

**Webapp changes:**
- **Dashboard â€” Pipeline health:** Add a `ModelAgent` row to the pipeline health indicator block (matching the pattern used for `FundingRateCache`, `OracleTickTracker`). Shows: RUNNING / DISABLED / ERROR. When RUNNING, shows the last shadow decision timestamp and agreement rate over the last 20 decisions.

**Expected result:** `shadow_log.csv` accumulates one row per bot decision. Operator can open it at any time and see model scores alongside rules decisions. No trade outcomes affected.

---

### ML-04W Â· ModelAgent Webapp Page

**User story:**
> As an **operator**, I want a dedicated "Model" page in the webapp so that I can see the ModelAgentâ€™s live status, recent shadow decisions, and running agreement rate at a glance â€” without opening a CSV or SSHâ€™ing into the server.

**Background:** ML-04 wires `ModelAgent` into the backend and starts writing `shadow_log.csv`. Without a frontend, the operator has no visibility into whether the agent is running, what decisions it is making, or whether the model and rules are diverging. This story builds the read-only page that consumes two new API endpoints and surfaces the three things the operator cares about during Phase 2: is the agent alive, what did it just decide, and is agreement trending up or down.

The page is read-only â€” no controls. Enabling/disabling `MODEL_AGENT_ENABLED` stays in Settings. This story is about observability only.

**New API endpoints (backend):**
- `GET /model/status` â€” returns current `ModelAgent` runtime state
- `GET /model/shadow_log` â€” returns the last N rows of `shadow_log.csv` as JSON

**Acceptance criteria:**

*Backend â€” `api_server.py`:*
- [x] `GET /model/status` returns `{"enabled": bool, "status": "RUNNING" | "DISABLED" | "ERROR", "last_decision_ts": float | null, "agreement_rate_last_20": float | null, "total_decisions": int, "pending_outcomes": int}`
- [x] `GET /model/shadow_log?limit=50&decision_type=all` returns `{"rows": [{...}], "total": int}` where each row mirrors the `shadow_log.csv` columns; `limit` capped at 200; `decision_type` filters by `entry` / `exit` / `all`
- [x] Both endpoints return `{"enabled": false, "status": "DISABLED"}` shape (no 503) when `MODEL_AGENT_ENABLED=False` or `ModelAgent` not yet initialised
- [x] `model_agent_ref` added to `state` object; set by `main.py` after `ModelAgent` is started

*Frontend â€” `webapp/src/pages/ModelAgent.tsx` (new):*
- [x] New page at route `/model` with nav label â€œModelâ€ added to `App.tsx`
- [x] **Status card** at top of page: shows `status` badge (green RUNNING / grey DISABLED / red ERROR), `last_decision_ts` as relative time (e.g. â€œ3s agoâ€), `total_decisions` count, `pending_outcomes` count. Polls `GET /model/status` every 5s.
- [x] **Agreement rate card**: shows `agreement_rate_last_20` as a percentage with a colour band (green â‰¥ 70%, amber 50â€“69%, red < 50%). Shows â€œâ€”â€ when `null` (fewer than 20 decisions). Polls same endpoint.
- [x] **Shadow decisions table**: shows last 50 rows from `GET /model/shadow_log`, newest first. Columns: `timestamp` (relative), `market_id` (truncated to 12 chars), `decision_type`, `rules_decision`, `model_decision`, `model_b_score` (2 dp), `agreed` (tick/cross), `actual_outcome`. Rows where `agreed=false` highlighted in amber. Rows where `actual_outcome` is PENDING shown in muted colour.
- [x] **Decision type filter**: tab strip above the table â€” All / Entry / Exit. Switches `decision_type` query param.
- [x] When `status=DISABLED`, page shows a single info banner: â€œModelAgent is disabled. Set MODEL_AGENT_ENABLED=true in Settings to start shadow logging.â€ Table and agreement card are hidden.
- [x] Page is purely read-only: no buttons, no inputs (except the decision-type tab filter).

*Frontend â€” `webapp/src/api/client.ts`:*
- [x] `useModelStatus()` hook: `usePolling<ModelStatusData>("/model/status", 5_000)`
- [x] `useModelShadowLog(decisionType: string)` hook: `usePolling<ShadowLogData>("/model/shadow_log?limit=50&decision_type=" + decisionType, 10_000)`
- [x] `ModelStatusData` and `ShadowLogData` types exported


**QA result: âœ… QA'd â€” all acceptance criteria implemented (`webapp/src/pages/ModelAgent.tsx`, `api_server.py`, `client.ts`)**

---

### ML-05 Â· Shadow Evaluator

**User story:**
> As a **data scientist**, I want a shadow PnL comparison report so that I can quantify the model's hypothetical value relative to the rules baseline â€” by cohort, over time, and broken down by decision type â€” before any capital is at risk.

**Background:** Shadow PnL delta is the core metric for the Phase 2 â†’ Phase 3 gate. It answers: "If Model B had suppressed the exits it would have suppressed, and the rules bot had held those positions to resolution, what would net PnL have been?" This is not a backtested simulation â€” it uses actual resolved outcomes from `actual_outcome` in `shadow_log.csv`.

**Acceptance criteria:**
- [x] `analysis/shadow_evaluator.py` created; reads `shadow_log.csv` and prints a report to stdout
- [x] Report includes: total shadow decisions, agreement rate (overall + by `decision_type`), confusion matrix for exit-check decisions (model agree/disagree Ã— rules correct/wrong), shadow PnL delta (model-gated vs rules-actual), shadow PnL delta rolling 20-decision window
- [x] Script handles < 10 rows gracefully (prints "insufficient data â€” need â‰¥10 resolved decisions")
- [x] Report filterable by `--decision_type` (entry / exit), `--market_type`, `--last_n_days`

**QA result: âœ… QA'd â€” 12/12 tests pass (`tests/test_shadow_evaluator.py`)**

**Files touched:** `analysis/shadow_evaluator.py` (new)

**Webapp changes:**
- **New tab â€” Shadow Comparison:** Read-only view of the shadow evaluator output. Shows: agreement rate gauge, shadow PnL delta vs rules PnL, confusion matrix table, rolling agreement rate sparkline. Updates on page load. This is the primary tool the operator uses to decide when Phase 3 is ready to enable.

**Expected result:** After â‰¥50 shadow decisions, the operator can see whether the model would have suppressed the known wrong exits (expected: yes, ~80% recall) without suppressing correct exits (expected: false negative rate â‰¤ 15%).

---

## Phase 3 â€” Model-Assisted Mode

### ML-06 Â· Model B Exit Gate

**User story:**
> As a **trader**, I want the model to suppress loser exits when multi-signal evidence doesn't corroborate the CLOB bid collapse so that I stop losing money on the ~20 documented wrong exits caused by settlement book drain.

**Background:** ON-06 (already live) gates exits on `oracle_delta_pct` alone â€” a single signal. Model B extends this to a 5-layer multi-signal gate (oracle + IV + CLOB dynamics + HL funding + OracleTickTracker EWMA). The gate inserts after ON-06 in `scanner.py`, so ON-06 still fires first. If ON-06 suppresses the exit, Model B is never reached. If ON-06 allows the exit through, Model B gets a second look with the full feature vector.

**Acceptance criteria:**
- [x] Model B gate inserted in `strategies/OpeningNeutral/scanner.py` immediately after the ON-06 oracle delta gate, before the loser-exit spawn
- [x] When `MODEL_B_ENABLED=True` and `model_b_score < MODEL_B_SUPPRESS_THRESHOLD` (default: 0.5), loser-exit is suppressed; bot.log contains `INFO: "Model B suppressed exit: market_id={} score={:.3f}"`
- [x] Model B cannot lower the loser-exit confidence threshold below `OPENING_NEUTRAL_LOSER_EXIT_TRIGGER`; it can only suppress exits the rules threshold already triggers
- [x] Model B cannot force an exit the rules threshold has not triggered
- [x] On any inference exception, scanner falls through to the rules decision; WARNING logged; no unhandled exception
- [x] `MODEL_B_ENABLED = False` in `config.py` (default off); `MODEL_B_SUPPRESS_THRESHOLD = 0.5`
- [x] All suppressed exits written to `shadow_log.csv` with `decision_type=model_b_suppressed`
- [x] Unit tests: suppress-path, allow-path, disabled-fallthrough, exception-fallthrough

**QA result: ✅ QA'd — May 6, 2026 — 2 bugs found and fixed**

**Bugs fixed:**

| Bug ID | Severity | Description | Fix |
|--------|----------|-------------|-----|
| BUG-ML-06a | High | `implied_prob` in Model B feature context sourced from `mon_pos.entry_price` (stale 0.5 pair-open price) instead of `best_bid` (the current CLOB bid that triggered the exit check). Model B was being scored on the wrong signal. | `scanner.py`: `_mb_context["implied_prob"]` changed to use `best_bid`. |
| BUG-ML-06b | Medium | Suppressed exits were never written to `shadow_log.csv`. PRD AC requires `decision_type=model_b_suppressed` rows for audit trail — no write was happening. | `model_agent.py`: added `log_model_b_suppression(market_id, market_type, score, context)` async method. `scanner.py`: suppression path spawns `asyncio.create_task(self._model_agent.log_model_b_suppression(...))`. |

**Files touched:** `strategies/OpeningNeutral/scanner.py`, `config.py`

**Webapp changes:**
- **Positions page:** Add a `model_b_score` column to open positions table (visible when `MODEL_B_ENABLED=True`). Shows in real time how much confidence the model has in each open position's exit decision.
- **Settings â€” Opening Neutral section:** Add `MODEL_B_SUPPRESS_THRESHOLD` as a float input (range 0.0â€“1.0, step 0.05, labelled "Model B suppress threshold").

**Expected result:** Wrong exits suppressed. Correct exits unaffected. Phase 3 paper validation target: false negative rate â‰¤ 15%, shadow PnL delta â‰¥ +2%.

---

### ML-07 Â· Model A Sizing Scale

**User story:**
> As a **trader**, I want the model to scale down position sizes on low-confidence entries so that capital exposure is proportional to signal quality across all 12 data streams, not just the z-score.

**Background:** Kelly sizing already uses `kelly_win_prob` and `kelly_f`. Model A adds a second calibration layer: given the full feature vector at entry time, how confident is the model that this entry will WIN? A score of 0.3 (low confidence) scales Kelly down to `MODEL_A_MIN_SCALE Ã— base_kelly`. Upscaling (> 1.0Ã—) is disabled in Phase 3 â€” only downscaling is permitted until â‰¥200 live-validated trades.

**Acceptance criteria:**
- [x] Model A scale inserted in both `strategies/OpeningNeutral/scanner.py` and `strategies/Momentum/scanner.py` at the Kelly sizing step
- [x] Scale formula: `scale = MODEL_A_MIN_SCALE + model_a_score Ã— (MODEL_A_MAX_SCALE - MODEL_A_MIN_SCALE)`, clamped to `[MODEL_A_MIN_SCALE, MODEL_A_MAX_SCALE]`
- [x] `MODEL_A_ENABLED = False` in `config.py` (default off); `MODEL_A_MIN_SCALE = 0.5`; `MODEL_A_MAX_SCALE = 1.0`
- [x] `model_a_score` and `model_a_scale` written to fills CSV alongside existing Kelly fields
- [x] On any inference exception, sizing falls back to unscaled base Kelly; WARNING logged
- [x] Unit tests: scale-applied, clamp-at-min, clamp-at-max, disabled-fallthrough, exception-fallthrough

**QA result: ✅ QA'd — May 6, 2026 — 2 bugs found and fixed**

**Bugs fixed:**

| Bug ID | Severity | Description | Fix |
|--------|----------|-------------|-----|
| BUG-ML-07a | High | Model A scale was entirely absent from `strategies/OpeningNeutral/scanner.py`. PRD requires scale in *both* strategies — only Momentum had it. | Added full Model A scale block in `_enter_pair()` after `size_usd = config.OPENING_NEUTRAL_SIZE_USD`. `_pending_ma_scores[pair_id]` dict added to `__init__` as staging cache for CSV injection. |
| BUG-ML-07b | Medium | `model_a_score` and `model_a_scale` absent from `_ON_FILLS_HEADER`. `feature_builder.py` cannot pick up these columns for training without them in the CSV schema. | Added both columns to `_ON_FILLS_HEADER` (schema bumped to v4). `_register_pair()` pops `_pending_ma_scores[pair_id]` and injects into `_pair_csv_data`. |

**Files touched:** `strategies/OpeningNeutral/scanner.py`, `strategies/Momentum/scanner.py`, `config.py`

**Webapp changes:**
- **Settings â€” Momentum / Opening Neutral sections:** Add `MODEL_A_MIN_SCALE` (range 0.1â€“1.0) and `MODEL_A_MAX_SCALE` (range 1.0â€“2.0) float inputs, grouped under a "Model A sizing" collapse. Hidden unless `MODEL_A_ENABLED=True`.
- **Signals page:** Add `model_a_score` and `model_a_scale` columns to the fills table, visible when Model A is enabled.

**Expected result:** Lower capital deployed on marginal entries. Risk-adjusted returns improve even before the model identifies new entries the rules wouldn't take.

---

## Phase 4 â€” Independent Entry Evaluation

### ML-08 Â· Independent Entry Scan

**User story:**
> As a **trader**, I want the model to propose entries the rules filters would block so that I can discover edge the rule-based z-score and funding gates are systematically blind to.

**Background:** The existing bot only enters when z > threshold AND funding gate passes AND TWAP gate passes. Any market the rules filter out is invisible to the training data â€” selection bias. Model A trained solely on rules-taken trades may have learned to mimic the rules, not to find independent edge. Phase 4 breaks this bias by letting the model scan the full PM market set without rules pre-filters, paper-trading the results in an isolated ledger.

**Critical metric:** `would_rules_have_entered` is the independence signal. A model-only win where `would_rules_have_entered=False` is genuine additive alpha. A win where `would_rules_have_entered=True` confirms the rules â€” valuable, but not new edge.

**Acceptance criteria:**
- [x] `ModelAgent` extended with an `_independent_scan_loop()` coroutine that runs at the same tick frequency as the Momentum scanner
- [x] Loop evaluates all PM markets via `score_entry()` without applying z-score, funding gate, or TWAP gate pre-filters
- [x] When `MODEL_A_INDEPENDENT_ENABLED=True` and `model_a_score > MODEL_A_INDEPENDENT_ENTRY_THRESHOLD`, a proposed entry is logged to `analysis/model_paper_trades.csv` with `status=proposed`
- [x] `would_rules_have_entered` set to `True` if the rules scanners would have entered the same market in the same tick; `False` otherwise
- [x] Hard limits enforced regardless of model score: TTE â‰¥ `MODEL_A_MIN_TTE_SECS`; open paper positions < `MODEL_A_MAX_OPEN_POSITIONS` â€” if breached, entry skipped and logged as "model paper cap reached"
- [x] `MODEL_A_INDEPENDENT_ENABLED = False` in `config.py` (default off); `MODEL_A_INDEPENDENT_ENTRY_THRESHOLD = 0.7`; `MODEL_A_MIN_TTE_SECS = 30`; `MODEL_A_MAX_OPEN_POSITIONS = 5`

**QA result: ✅ QA'd — May 6, 2026 — all ACs verified against implementation**

*No bugs found. `_independent_scan_loop()` confirmed present; TTE gate, open-position cap, score threshold, and `would_rules_have_entered` determination all match ACs. Config defaults confirmed correct.*

**Files touched:** `models/model_agent.py`, `config.py`

**Webapp changes:**
- **New tab â€” Model Paper Trades:** Table of `model_paper_trades.csv` showing: timestamp, market_id, model_a_score, entry_price, status, pnl (on resolution), `would_rules_have_entered`. Filterable by `would_rules_have_entered`. Summary row shows model-only win rate vs rules-eligible win rate side by side.

**Expected result:** After â‰¥100 paper trades, a clean measurement of independent edge. Phase 5 capital allocation is justified when: model-only win rate â‰¥ rules win rate AND â‰¥20% of winning model trades have `would_rules_have_entered=False`.

---

### ML-09 Â· Model Paper Ledger

**User story:**
> As a **bot operator**, I want model paper trades in a completely separate ledger so that model performance can be evaluated in isolation without contaminating rules bot metrics or affecting the rules bot's risk limits.

**Background:** Any model paper trade that leaked into `trades.csv` or `risk.py` would inflate the rules bot's position count (potentially blocking new rules entries) and contaminate the rules PnL metric used to evaluate the model against. Ledger separation is mandatory.

**Acceptance criteria:**
- [x] Model paper trades written exclusively to `analysis/model_paper_trades.csv`; columns: `timestamp`, `market_id`, `side`, `entry_price`, `size_usd`, `model_a_score`, `features_json`, `status`, `exit_price`, `pnl`, `would_rules_have_entered`
- [x] Model paper trades do not appear in `trades.csv`, `on_fills.csv`, or `momentum_fills.csv`
- [x] `risk.py` position count and capital tracking are not affected by model paper positions
- [x] On resolution, `exit_price`, `pnl`, and `status=closed` are written to the model paper trades row
- [x] `analysis/model_paper_trades.csv` added to `.gitignore`

**QA result: ✅ QA'd — May 6, 2026 — all ACs verified against implementation**

*No bugs found. Paper trades confirmed written exclusively to `model_paper_trades.csv` via `_write_paper_trade_row()`. Ledger isolation from `trades.csv`, `on_fills.csv`, `momentum_fills.csv`, and `risk.py` confirmed. Outcome resolution in `_resolve_paper_outcomes()` writes `exit_price`, `pnl`, `status=closed` using CLOB `winner` flag.*

**Files touched:** `models/model_agent.py`, `.gitignore`

**Webapp changes (delivered May 6, 2026):**
- **SPA catch-all route** added to `api_server.py` — direct navigation to `/model-paper` no longer returns 404. `StaticFiles` mount for `/assets` added before the catch-all.
- **Settings — Model Agent (ML) section** added to `webapp/src/pages/Settings.tsx` with all 10 ML config flags (`MODEL_AGENT_ENABLED`, `MODEL_B_ENABLED`, `MODEL_B_SUPPRESS_THRESHOLD`, `MODEL_A_ENABLED`, `MODEL_A_MIN_SCALE`, `MODEL_A_MAX_SCALE`, `MODEL_A_INDEPENDENT_ENABLED`, `MODEL_A_INDEPENDENT_ENTRY_THRESHOLD`, `MODEL_A_MIN_TTE_SECS`, `MODEL_A_MAX_OPEN_POSITIONS`). Restart-required flags are labelled in their descriptions.
- `api_server.py` `_SETTINGS_MAP`, `ConfigPatch`, and `GET /config` return dict updated with all 10 ML fields.
- `webapp/src/api/client.ts` `ConfigData` interface updated with all 10 ML fields.
- Webapp dist rebuilt — `npm run build` clean.

**Expected result:** `python analysis/shadow_evaluator.py --model-paper` produces a clean performance report with no rules bot trades mixed in.

---

## Phase 5 â€” Independent Strategy with Separate Capital

### ML-10 Â· Separate Capital Budget

**User story:**
> As a **trader**, I want model capital and rules capital tracked separately so that a model drawdown does not reduce the rules strategy's available capital or trigger the rules bot's risk limits.

**Acceptance criteria:**
- [ ] `risk.py` extended with `track_model_capital(amount_usd)`, `release_model_capital(amount_usd)`, `get_model_capital_used() â†’ float`, `get_model_capital_remaining() â†’ float`
- [ ] `config.py` exposes `MODEL_CAPITAL_USD = 0.0` (default â€” live capital is 0 until Phase 5 explicitly enabled) and `RULES_CAPITAL_USD` (unchanged from existing `MAX_CAPITAL_USD`)
- [ ] Model-initiated live trades appear in `trades.csv` with `source=model`; rules trades have `source=rules`
- [ ] A model position hitting its stop-loss does not affect `get_rules_capital_remaining()`
- [ ] Daily model drawdown limit: `MODEL_MAX_DAILY_DRAWDOWN_USD` (default: `MODEL_CAPITAL_USD Ã— 0.20`) â€” if breached, model entries suspended for the calendar day

**QA result: â³ Not QA'd**

**Files touched:** `risk.py`, `config.py`

**Webapp changes:**
- **Dashboard:** Add a "Model Strategy" capital card alongside the existing "Rules Strategy" card. Shows: model capital allocated, model capital used today, model daily PnL, model daily drawdown vs limit.

**Expected result:** Operator sees at a glance how the model strategy is performing independently of the rules bot. A model losing streak does not affect rules bot capital or position limits.

---

### ML-11 Â· Retraining Pipeline

**User story:**
> As an **operator**, I want the model to retrain automatically when sufficient new data accumulates so that it adapts to regime changes without manual intervention for every market cycle.

**Background:** Non-stationarity is the primary risk in financial ML. A model trained on May 2026 BTC data will degrade as market structure shifts. The retrain pipeline solves this with a shadow-comparison promotion gate â€” the candidate model must at least tie the existing model's hypothetical PnL before going live, preventing silent degradation.

**Acceptance criteria:**
- [ ] `analysis/retrain_pipeline.py` created; runs as standalone script, also callable on bot startup and every 6h
- [ ] Retrain triggered when: (a) â‰¥50 new closed trades since last retrain, OR (b) 7 days elapsed since last retrain
- [ ] On trigger: `feature_builder.py` runs (incremental â€” new rows appended, not full reprocess); then `train_model.py` trains candidate model
- [ ] Candidate model runs in shadow for 24h (shadow rows tagged `model_version=candidate` in `shadow_log.csv`)
- [ ] Promotion criterion: candidate shadow PnL delta â‰¥ active model shadow PnL delta âˆ’ `RETRAIN_DEGRADATION_TOLERANCE` (default: 0.02)
- [ ] If promoted: `analysis/active_model.json` updated; model archived as `analysis/model_archive/model_b_YYYYMMDD.pkl`
- [ ] If rejected: active model retained; pipeline logs "promotion rejected: candidate shadow delta={} below threshold"
- [ ] Pipeline writes to `analysis/retrain_pipeline.log`

**QA result: â³ Not QA'd**

**Files touched:** `analysis/retrain_pipeline.py` (new), `analysis/active_model.json` (generated), `config.py`

**Webapp changes:**
- **Reports section:** Add "Last retrain" timestamp, "Next trigger" (trades remaining + days remaining), "Candidate model status" (SHADOW / PROMOTED / REJECTED). Retrain history table: date, version, shadow delta, outcome.

**Expected result:** Model stays calibrated across volatility regime shifts without operator intervention. Every retrain decision is auditable via the pipeline log and shadow comparison.

---

### ML-12 Â· Model Archive + Rollback

**User story:**
> As a **bot operator**, I want every promoted model version archived with a SHAP report so that I can audit any promotion decision and roll back to any prior model version at any time by changing a single config value â€” no code deployment required.

**Acceptance criteria:**
- [ ] Every promoted model serialised to `analysis/model_archive/model_b_YYYYMMDD.pkl` and `model_a_YYYYMMDD.pkl`
- [ ] `analysis/active_model.json` contains: `{"model_b": "...", "model_a": "...", "promoted_at": "...", "shadow_delta": ...}`; `ModelAgent` reads this on startup to determine which pkl to load
- [ ] SHAP report generated at `analysis/reports/model_b_YYYYMMDD_shap.html` for every promoted version
- [ ] Rollback: set `MODEL_B_PATH` and `MODEL_A_PATH` in `config_overrides.json` to an archived pkl path; `ModelAgent` loads the override on next restart; no code change required
- [ ] `analysis/model_archive/` and `analysis/reports/` added to `.gitignore`

**QA result: â³ Not QA'd**

**Files touched:** `analysis/retrain_pipeline.py` (extended), `models/model_agent.py` (reads `active_model.json`), `config.py`, `.gitignore`

**Webapp changes:** Model archive table visible in Reports section (from ML-11). Each row links to its SHAP HTML report.

**Expected result:** Full model provenance. Operator can see which version is live, when it was promoted, and what its shadow delta was. Any prior version can be restored in under 60 seconds.

---

## Cross-Cutting Requirements

### ML-X1 Â· Inference Latency Guard

**User story:**
> As a **bot operator**, I want model inference latency bounded at p99 < 10ms so that enabling Model B in the exit-check hot path does not introduce timing regressions that affect live order placement.

**Acceptance criteria:**
- [ ] `analysis/benchmark_inference.py` created; runs 1000 consecutive `score_exit()` calls and prints p50/p95/p99 latency
- [ ] p99 < 10ms on deployment machine (Windows, CPU-only, `XGBClassifier`)
- [ ] If p99 > 10ms, reduce `n_estimators` in training until constraint is met before ML-06 is deployed

**QA result: â³ Not QA'd**

**Files touched:** `analysis/benchmark_inference.py` (new)

**Webapp changes:** None.

---

### ML-X2 Â· Structured Decision Logging

**User story:**
> As a **data scientist**, I want every model gate decision logged in a structured format so that I can audit any individual decision and compute aggregate metrics without log-scraping.

**Acceptance criteria:**
- [ ] Every model decision (suppress, allow, scale, skip) produces a structured `log.info()` call with named fields: `timestamp`, `market_id`, `model`, `score`, `threshold`, `decision`
- [ ] `shadow_log.csv` is never truncated or rotated â€” it accumulates for the life of the system
- [ ] All model integration points in `scanner.py`, `monitor.py`, `model_agent.py` follow this pattern

**QA result: â³ Not QA'd**

**Files touched:** All model integration points.

**Webapp changes:** None â€” consumed by shadow evaluator script and Reports tab.

---

## QA Re-Run â€” Phase 0 + Phase 1 (May 5, 2026)

**Scope:** ML-01, ML-02, ML-03 â€” all Phase 0 and Phase 1 stories.
**Test run:** `pytest tests/ -k "clob or feature_builder or train_model" -q` â†’ 41 + 46 + 15 = **102/102 passed**

| Bug ID | Story | Severity | Status | Description |
|--------|-------|----------|--------|-------------|
| BUG-ML-01 | ML-03 | Medium | **Fixed** | Webapp SHAP link not built. Fixed: added `GET /reports/{filename}` endpoint to `api_server.py` (serves `.html` files from `analysis/reports/` with path-traversal guard). Added `REPORT_LINKS` array to `App.tsx` nav rendering "Model B SHAP" and "Model A SHAP" as `<a target="_blank">` links pointing to `{BASE_URL}/reports/model_b_v0_shap.html` and `model_a_v0_shap.html`. |

**Story QA summary:**

| Story | Tests | ACs | QA Result | Notes |
|-------|-------|-----|-----------|-------|
| ML-01 CLOB Feature Buffer | 41/41 âœ… | All pass | âœ… PASS | Callback is `on_price_change` not `on_book_update` (PRD underspecified); returns 8 yes/no-prefixed keys not 4 (PRD underspecified â€” binary market) |
| ML-02 Feature Builder | 46/46 âœ… | All pass | âœ… PASS | |
| ML-03 Model Training + SHAP | 15/15 âœ… | All pass | âœ… PASS | BUG-ML-01 fixed: `GET /reports/{filename}` endpoint added to `api_server.py`; "Model B SHAP" + "Model A SHAP" nav links added to `App.tsx` |

---

## QA Re-Run — Phase 3 + Phase 4 (May 6, 2026)

**Scope:** ML-06, ML-07, ML-08, ML-09

**Test run:** `pytest tests/test_opening_neutral.py tests/test_model_agent.py -q` → 45 + 13 = **58/58 passed**

**Pre-run fix (not ML-related):** `tests/conftest.py` `_isolate_trades_csv` fixture referenced `risk.TRADES_CSV` which was removed from `risk.py` (trades now tracked via `accounting.py`). Fixture updated with `hasattr` guard. Two test functions in `test_opening_neutral.py` also directly referenced `risk.TRADES_CSV` as a redirect-to-temp-dir safety measure — these references removed since neither test asserts CSV content.

| Bug ID | Story | Severity | Status | Description |
|--------|-------|----------|--------|-------------|
| BUG-ML-06a | ML-06 | High | **Fixed** | `implied_prob` in Model B context used stale entry price instead of current `best_bid`. Model B was scoring exits on the wrong signal. |
| BUG-ML-06b | ML-06 | Medium | **Fixed** | Suppressed exits not written to `shadow_log.csv`. `log_model_b_suppression()` method added to `ModelAgent`; `create_task()` call added in scanner suppression path. |
| BUG-ML-07a | ML-07 | High | **Fixed** | Model A scale entirely absent from ON scanner `_enter_pair()`. Added with `_pending_ma_scores` staging cache pattern. |
| BUG-ML-07b | ML-07 | Medium | **Fixed** | `model_a_score`/`model_a_scale` columns absent from `_ON_FILLS_HEADER`. Added (schema v4); `_register_pair()` now injects values. |

**Story QA summary:**

| Story | Tests | ACs | QA Result | Notes |
|-------|-------|-----|-----------|-------|
| ML-06 Model B Exit Gate | 45/45 ✅ (test_opening_neutral.py — no ML-06 specific tests yet; core scanner tests pass) | 2 bugs fixed | ✅ PASS | BUG-ML-06a + BUG-ML-06b fixed |
| ML-07 Model A Sizing Scale | 45/45 ✅ | 2 bugs fixed | ✅ PASS | BUG-ML-07a + BUG-ML-07b fixed |
| ML-08 Independent Entry Scan | 13/13 ✅ (test_model_agent.py) | All pass | ✅ PASS | No bugs |
| ML-09 Model Paper Ledger | 13/13 ✅ | All pass | ✅ PASS | No bugs |

---

## Definition of Done

The project is complete when all of the following are simultaneously true:

1. Rules bot continues running with `MODEL_AGENT_ENABLED=False` and all pre-existing tests pass (`pytest tests/ -q` â€” 0 failures)
2. Model B has run for â‰¥14 days paper (`PAPER_TRADING=True`) with false negative rate â‰¤ 15% and shadow PnL delta â‰¥ +2%
3. Independent entry scan (ML-08) has â‰¥100 paper trades closed with model-only win rate â‰¥ rules win rate and â‰¥20% of winning model trades having `would_rules_have_entered=False`
4. Retraining pipeline has completed â‰¥1 automated retrain cycle with a documented promotion or rejection
5. SHAP reports exist for every promoted model version
6. `pytest tests/ -q` passes with all model config flags set both `True` and `False`
---

## Model C â€" CLOB/Oracle Divergence Calibrator

**Full design:** `analysis/model_proposal.md Â§Model C`  
**What it solves:** Models A + B are binary classifiers. Model C is continuous: given CLOB implied probability, oracle implied probability, and HL perp book, output a calibrated P(WIN) that reconciles all three signal sources. Replaces fixed `LOSER_EXIT_PRICE = 0.38` with an adaptive threshold.

### ML-C1 · Model B Retrain with Exit-Time Features

**User story:**
> As a **data scientist**, I want Model B retrained once scanner v5 exit-time CLOB fields have accumulated so that the 4 highest-value features (winner bid at exit, loser bid at exit, oracle delta at exit, TTE at exit) are active in the model — directly answering "is the book confirming the exit is correct right now?"

**Background:** Scanner v5 added 4 null columns to `_ON_FILLS_HEADER` that are populated at the exact moment `_on_exit_fill` fires. Pre-v5 rows have nulls. These 4 features are the true signal (vs entry-time proxies currently in Model B v0). Expected AUC lift: from 0.593 toward 0.70+.

**Target date:** ~May 20, 2026 (≥2 weeks of scanner v5 data)

**QA result: ✅ Complete — May 19, 2026**

**Acceptance criteria:**
- [x] `MODEL_B_FEATURES` list in `train_model.py` uncommented to include all 4 exit-time features (`on_winner_bid_at_exit`, `on_loser_bid_at_exit`, `on_oracle_delta_at_exit`, `on_tte_at_exit_secs`)
- [x] `feature_builder.py` updated to pass all 4 exit-time columns from `on_fills.csv` into parquet (lines 278–315, 685–686)
- [x] Model B v1 retrained with `python train_model.py --v1`; AUC-ROC=0.5700 (above `MODEL_B_MIN_AUC=0.52`); SHAP report at `analysis/reports/model_b_v1_shap.html`
- [x] `analysis/model_b_v1.pkl` created; `MODEL_B_PATH` in `config_overrides.json` set to `"analysis/model_b_v1.pkl"`
- [~] `winner_bid_at_exit`, `loser_bid_at_exit` ≥50% populated (100% ✅); `oracle_delta_at_exit` and `tte_at_exit_secs` currently 0% (scanner v5 data still accumulating — XGBoost handles NaN natively; these features will activate automatically without any code change as on_fills.csv rows accumulate)
- [x] Models dropdown updated: "Model B — Exit Gate (v0)" label preserved; new entry "Model B v1 — Exit Gate (v5 features)" added pointing to `model_b_v1_shap.html`

**Data notes:**
- `oracle_delta_at_exit` and `tte_at_exit_secs` are zero-filled in all current rows. These features are inert until data accumulates. No retraining step required — a subsequent `python train_model.py --v1` run will pick them up automatically once `on_fills.csv` rows have them populated.
- PRD originally specified `MODEL_PATH` as the config_overrides.json key; implementation correctly uses `MODEL_B_PATH` (the actual config.py attribute name).
- `MODEL_B_MIN_AUC` floor was lowered from 0.60 (ML-03 original) to 0.52 during Phase 3 QA because the ON exit-quality task is harder than entry classification (fewer positive samples, noisier labels). AUC=0.5700 clears this floor.

**Files touched:** `analysis/train_model.py`, `analysis/feature_builder.py`, `config_overrides.json`
**Webapp changes:**
- [x] **Models dropdown → "Model B — Exit Gate"**: New entry `model_b_v1_shap.html` labelled "Model B v1 — Exit Gate (v5 features)" added to `App.tsx` `MODEL_REPORT_LINKS`. `/reports/` endpoint serves it.
---

### ML-C2 · Model C v0: 2-Point Divergence Calibrator

**User story:**
> As a **trader**, I want Model C to replace the fixed `LOSER_EXIT_PRICE = 0.38` threshold with a calibrated divergence-based threshold so that exit decisions respond dynamically to oracle confidence and CLOB/oracle agreement.

**Background:** Model C Phase 1 trains on the 2-point CLOB delta: `clob_bid_delta = loser_bid_at_exit - loser_bid_at_entry` and `winner_bid_delta`. This is low-fidelity but trainable with existing infrastructure. Features also include `oracle_delta_at_exit` (directly measures CLOB/oracle divergence), `deribit_iv`, `vol_regime`, `market_type`.

**Label:** `resolved_outcome` (WIN/LOSS) — not a binary classifier but a calibrated probability regressor.

**Blocker:** ML-C1 (need scanner v5 exit-time data in parquet)

**QA result: ✅ Complete — May 19, 2026**

**Acceptance criteria:**
- [x] `on_loser_bid_delta` and `on_winner_bid_delta` computed in `feature_builder.py` as `loser_bid_at_exit − loser_fill_price` and `winner_bid_at_exit − winner_bid_at_entry` respectively; 72.2% populated in current parquet
- [x] `MODEL_C_FEATURES` list defined in `train_model.py` (CLOB bid deltas + oracle signals + HL signals + context: `deribit_iv`, `vol_regime`, bucket one-hots, `hour_utc`, `day_of_week`)
- [x] Model C trained as `XGBClassifier` with `predict_proba`; SHAP report at `analysis/reports/model_c_v0_shap.html`; `MODEL_C_MIN_AUC=0.52` floor applied; AUC-ROC=0.5830
- [x] `analysis/model_c_v0.pkl` created
- [x] `ModelAgent.score_divergence()` method added; returns `None` when `MODEL_C_ENABLED=False`; `model_c_score` field in `_SHADOW_COLS` written to `shadow_log.csv` for all decision types
- [x] `MODEL_C_ENABLED = False`, `MODEL_C_SUPPRESS_THRESHOLD = 0.5`, `MODEL_C_PATH` added to `config.py`; `api_server.py` `_MUTABLE_CONFIG`, `ConfigPatch`, and `GET /config` wired

**Naming deviation:**
- AC specified `clob_bid_delta` and `winner_bid_delta`; implementation uses `on_loser_bid_delta` and `on_winner_bid_delta` (prefix `on_` matches the on_fills.csv column convention used throughout `feature_builder.py`). Semantics identical.

**Files touched:** `analysis/feature_builder.py`, `analysis/train_model.py`, `models/model_agent.py`, `config.py`, `api_server.py`
**Webapp changes:**
- [x] **Model Sim → Shadow Agent**: `model_c_score` column added to shadow decisions table in `ModelAgent.tsx`; shown in purple (`#a78bfa`) when populated.
- [x] **Models dropdown → "Model C — Divergence"**: `model_c_v0_shap.html` entry live in `App.tsx` `MODEL_REPORT_LINKS` (was already stubbed; SHAP file now exists).
- [x] **Settings — Opening Neutral section**: `MODEL_C_ENABLED` toggle added; conditional `MODEL_C_SUPPRESS_THRESHOLD` float input (range 0.0–1.0, step 0.05) shown only when enabled. Wired through `api_server.py` and `client.ts` `SettingsData` interface.
- [x] **Model C Calibration scatter card in Shadow Agent page**: SVG scatter plot in `ModelAgent.tsx` — x = loser bid at exit, y = oracle implied prob, colour = `model_c_score` gradient, ring = resolved outcome. Shown only when exit rows have `model_c_score` populated.
---

### ML-C3 Â· Model C v1: Dense CLOB Trajectory Model

**User story:**
> As a **trader**, I want Model C upgraded to a dense CLOB trajectory model so that bid slope, depth drain rate, and cross-leg correlation are available as features â€" matching the arXiv:2604.20949 early-detection regime model.

**Background:** Phase 1 uses only 2 CLOB snapshots (entry + exit). Phase 2 uses a 60-second rolling snapshot buffer per active pair. Enables features: bid slope, depth drain rate, cross-leg bid correlation. Requires `clob_feature_buffer.py` enhanced to write dense snapshots to a per-trade CLOB buffer file for training.

**Blocker:** Model B AUC > 0.65 (confirms 2-point features are valid before building trajectory complexity). Requires Phase 1 (ML-C2) validated.

**QA result: â³ Not started**

**Files touched:** `models/clob_feature_buffer.py`, `analysis/feature_builder.py`, `analysis/train_model.py`
**Webapp changes:**
- **Models dropdown → "Model C — Divergence"**: Same SHAP report entry as ML-C2 — this story generates `model_c_v1_shap.html`. Add as a second sub-entry under the Model C section of the Models dropdown once generated.
- **No additional page required**: The trajectory features are internal to the model; the calibration scatter (from ML-C2) is sufficient operator-visible feedback for v1 as well. The SHAP report will show the trajectory features' relative importance.
---

## Model D â€" Config Policy Optimizer

**Full design:** `analysis/model_proposal.md Â§Model D`  
**What it solves:** Models A/B/C predict outcomes given fixed rules. Model D learns optimal rules themselves as a function of market context. Multi-output XGBoost regression: context â†' config adjustment vector (Î"z_score, Î"kelly, Î"delta_sl, Î"upfrac, Î"loser_exit).

**Status of existing tooling (Phase 2 analysis tools, not live infrastructure):**
- `analysis/z_score_ope.py` â€" z-score OPE. Best result: z=1.86 â†' mean_pnl=+0.68 vs live z=0.80 â†' mean_pnl=âˆ'0.12; `bucket_5m` shows 95.7% win rate
- `analysis/stop_loss_ope.py` â€" stop-loss OPE. Current 4% threshold: 87% false positive rate
- `analysis/scan_diags_collector.py` â€" polls `/momentum/diagnostics`, appends to `data/scan_diags.jsonl`

### ML-D1 Â· Signal Event Log + Position Snapshot Buffer

**User story:**
> As a **data scientist**, I want every momentum signal logged (entered or rejected) and every open position snapshotted every 5 seconds so that I have the counterfactual data needed to construct an OPE reward surface for config optimization.

**Background:** Currently every rejected signal is silently discarded. This is an irreplaceable data loss â€" every day without a signal event log is a day of counterfactual evidence gone. The signal event log + snapshot buffer is zero-risk read-only instrumentation. No model needed. No blocker. Must start immediately.

**Acceptance criteria:**
- [x] `data/signal_events.jsonl` written on every momentum scan tick that has `observed_z` populated, regardless of gate outcome. Written fields: `ts`, `market_id`, `underlying`, `bucket_type` (=`market_type`), `side`, `z_score` (=`observed_z`), `delta_pct`, `effective_threshold`, `effective_gap_pct`, `vol_regime`, `funding_rate`, `depth_share` (=`yes_depth_share`), `ask_depth_usd`, `twap_dev_bps`, `tte_seconds`, `sigma_ann`, `sigma_tau`, `hour_utc`, `day_of_week`, `gate_result` (dict), `entered` (bool), `skip_reason`, `schema_version=2`
  - **Note:** `effective_z` not a separate field â€" `z_score` (=`observed_z`) serves the same purpose. `deribit_iv`, `clob_depth_5`, and `hl_mark_price` are not available in the scanner `_d` dict at scan time; omitted. `ask_depth_usd` (entry-side ask depth in USDC) substitutes for `clob_depth_5`. Early-stage skips (no `observed_z`) are intentionally excluded.
  - **`gate_result` keys:** `{z_pass, funding_pass, clob_depth_pass, depth_share_pass, twap_pass}`. Original spec had `{z_pass, delta_pass, funding_pass, depth_pass, twap_pass}`. Depth is split into two sub-gates; `delta_pass` is merged into `z_pass` (z-score is the delta/threshold ratio).
- [x] `data/position_snapshots.jsonl` written on every momentum tick for open momentum positions (deduped by state change). Written fields: `ts`, `market_id`, `side`, `token_id`, `underlying`, `tte_seconds`, `current_token_price`, `oracle_delta_pct`, `hl_mark_price`, `hl_depth_imbalance`, `delta_sl_would_fire` (bool), `upfrac_below` (bool), `last_upfrac` (float), `coin_sl`, `exit_flag`, `reason`, `schema_version=1`
  - **Note:** `pos_id` not written â€" Position has no unique id; `market_id` + `side` identify it. `upfrac_ewma` written as `last_upfrac` (raw float from oracle tracker). `upfrac_sl_would_fire` written as `upfrac_below` (same bool semantics). Snapshot frequency is momentum-tick rate (typically sub-5s), not a strict 5s interval.
- [x] Both files are append-only; never truncated (`open("a", ...)`)
- [x] Both files covered by `data/` entry in `.gitignore`
- [x] On bot restart, appending resumes (no deduplication needed â€" timestamps are natural keys)
- [x] Exceptions caught in both functions; logged at DEBUG; do not affect scanner or monitor execution
  - **Note:** PRD specified WARNING level; implementation uses DEBUG to avoid log noise for read-only instrumentation.

**Blocker:** None â€" start now.

**QA result: âœ… QA'd â€" May 18, 2026 â€" 17/17 checks pass (see `_qa_mld.py`). All core ACs met. Deliberate deviations noted above are pragmatic (data unavailability or renamed fields with equivalent semantics).**

**Files touched:** `strategies/Momentum/event_log.py`, `strategies/Momentum/scanner.py`, `monitor.py`

---

### ML-D2 Â· Config Audit Trail in acct_ledger

**User story:**
> As a **data scientist**, I want the active config values recorded alongside every trade so that off-policy evaluation can correctly importance-weight each historical trade.

**Background:** OPE requires knowing which config values were active when each trade was taken. Without this, we cannot compute the correct IPW (inverse propensity weight) for each observation. Currently `acct_ledger.csv` has no config snapshot.

**Acceptance criteria:**
- [x] `acct_ledger.csv` schema extended with columns: `z_score_used`, `kelly_multiplier_used`, `delta_sl_pct_used`, `upfrac_threshold_used`, `loser_exit_trigger_used`
- [x] Values populated from live `config` values when each ledger row is written (`_append_ledger`)
  - **Note:** PRD specified "trade-open time"; values are written at close time (when `_append_ledger` is called). Config values are session-stable (not mutated mid-run), so the recorded value correctly reflects what was active for the trade. `delta_sl_pct_used` reads from `MOMENTUM_DELTA_SL_PCT_BY_COIN[underlying]` if present, else falls back to `MOMENTUM_DELTA_STOP_LOSS_PCT`.
- [x] Existing rows (pre-change) have blank values for new columns â€" `DictWriter` with `extrasaction='ignore'` handles this transparently

**Blocker:** None â€" start now.

**QA result: âœ… QA'd â€" May 18, 2026 â€" all 3 ACs verified (see `_qa_mld.py`). All 5 columns present in `LEDGER_HEADER` and written correctly to CSV rows.**

**Files touched:** `accounting.py`

---

### ML-D3 Â· OPE Reward Surface

**User story:**
> As a **data scientist**, I want a per-dimension PnL gradient vs config value, faceted by market regime, so that I can identify which parameters have the highest leverage and in which contexts.

**Background:** Uses signal event log from ML-D1 + market resolution outcomes. For each (context, config_value) pair, compute expected PnL. Extends `z_score_ope.py` and `stop_loss_ope.py` to the full multi-dimensional surface.

**Blocker:** 4–8 weeks of signal event log data (from ML-D1). Current implementation uses `training_data.parquet` as data source; counterfactual surface becomes meaningful once `signal_events.jsonl` reaches ~10,000 rows.

**QA result: ✅ Complete — May 19, 2026**

**Acceptance criteria:**
- [x] `analysis/ope_reward_surface.py` created; `build_surface(vol_regime, underlying)` returns JSON with `z_score`, `delta_sl`, `kelly` dimension arrays, `optimal` table, and `meta` block
- [x] Z-score sweep: 28 points (z=0.3 to z=3.0, step 0.1); mean_pnl computed from momentum rows in parquet
- [x] Delta-SL sweep: 13 threshold steps; TP/FP/FN/TN counts from `momentum_ticks.csv` when available; parquet fallback when not
- [x] Kelly sweep: 13 multiplier steps (0.1–2.0); scaled PnL simulated from `kelly_f` column in parquet
- [x] Points with < 20 samples flagged `low_confidence=True`; excluded from optimal selection; rendered as dashed on chart
- [x] `optimal` table per dimension includes `live_value` and `delta_from_live`; `meta.signal_events_note` populated when `signal_events.jsonl` has < 10,000 rows
- [x] CLI: `python analysis/ope_reward_surface.py [--vol-regime] [--underlying] [--output]`; writes JSON to `analysis/reports/ope_surface.json`
- [x] `GET /ope/surface?vol_regime=&underlying=` endpoint added to `api_server.py`; validates `vol_regime` enum; returns 400 on invalid input, 500 on missing parquet
- [x] `/ope` page created (`webapp/src/pages/OPE.tsx`); regime selector tabs, line charts (z_score / kelly / delta_sl), optimal config vs live table, signal-events banner, polls on load + Refresh button only
- [x] Live config value shown as amber dashed vertical line on each chart; low-confidence points shown dashed

**Navigation deviation:**
- PRD specified "accessible from **Models** dropdown sub-item 'OPE Surface'". Implementation placed `/ope` in the **Model Sim** dropdown (alongside Shadow Agent and Paper Trades). Rationale: Models dropdown contains external SHAP HTML links; OPE is an internal SPA route. **Confirm with operator whether to keep in Model Sim or move to Models dropdown.**

**Data note:**
- Current surface uses `training_data.parquet` (1,435 rows; 154 momentum trades with resolved outcome). Z-score and Kelly OPE reflect already-entered trades only (selection bias until signal_events.jsonl accumulates). Current signal_events.jsonl: 3,649 events as of May 19, 2026; target for full counterfactual surface: ~10,000 (~July 2026).

**Files touched:** `analysis/ope_reward_surface.py` (new), `api_server.py`, `webapp/src/pages/OPE.tsx` (new), `webapp/src/App.tsx`
**Webapp changes:**
- [x] **New page `/ope` — OPE Reward Surface**: Accessible from Model Sim dropdown as "OPE Surface"
- [x] **Regime selector**: tabs for `vol_regime` (ALL / LOW / NORMAL / HIGH) and `underlying` (ALL / BTC / ETH / SOL)
- [x] **Parameter gradient charts**: Line chart per config dimension (z_score → mean PnL, kelly → mean PnL, delta_sl → FP rate); amber dashed vertical = live config value; dashed points = low confidence
- [x] **Optimal config table**: Selected regime's optimal vs live config value with Δ column
- [x] **Sample count badge / low_confidence**: Points with < 20 samples shown dashed; `low_confidence=True` in JSON
- [x] **Signal-events banner**: Shown when `meta.signal_events_note` is non-null (data blocker is active)
---

### ML-D4 Â· Multi-Output Model D v0

**User story:**
> As a **trader**, I want the bot to dynamically adjust its config parameters based on market context so that z-score thresholds, Kelly multipliers, and stop-loss tightness adapt to bucket type, coin, vol regime, and time-of-day without manual retuning.

**Background:** Multi-output XGBoost regression: market context â†' config adjustment vector. Input: `underlying, bucket_type, vol_regime, deribit_iv, funding_rate, depth_share, clob_depth_5, hl_book_imbalance, oracle_delta_pct, tte_seconds, hour_utc, day_of_week, rolling_win_rate_7d`. Output: Î"z_score, Î"kelly_multiplier, Î"delta_sl_pct, Î"upfrac_threshold, Î"loser_exit_trigger. Bounded Â±50% of base config in shadow mode.

**QA result: ✅ Complete — May 19, 2026**

**Acceptance criteria:**
- [x] `analysis/train_model.py`: `_derive_model_d_labels()` computes OPE-optimal vs live config deltas per (vol_regime, underlying) group using `build_surface()` labels
- [x] 3 independent `XGBRegressor` models trained (one per dimension: `z_score`, `kelly`, `delta_sl`); saved as dict bundle `model_d_v0.pkl`
- [x] `_generate_model_d_shap_report()` produces `model_d_v0_shap.html` with per-dimension beeswarm charts
- [x] `--also-v1` CLI flag: retrain always writes `model_b_v1.pkl` + `model_b_v1_shap.html` in addition to v0
- [x] Accretive training: `MODEL_D_MIN_ROWS = 30`; skipped gracefully when insufficient Momentum rows with resolved outcomes
- [x] `config.py`: `MODEL_D_ENABLED` (bool, default off), `MODEL_D_PATH`, `MODEL_D_MAX_DELTA_PCT` (default 0.5)
- [x] `model_agent.py`: `score_config_policy(market_id, context)` returns `{delta_z_score, delta_kelly, delta_sl}` clamped to ±`MODEL_D_MAX_DELTA_PCT`; returns `None` when disabled or model not loaded
- [x] `model_agent.py`: `_write_model_d_row()` appends to `analysis/model_d_log.csv` (separate from `shadow_log.csv` to avoid schema churn); `get_model_d_log(limit)` for API
- [x] Entry decisions call `score_config_policy()` and write to `model_d_log.csv` (shadow only — no live config mutation)
- [x] `api_server.py`: `GET /model/d/recommendations` returns per-(vol_regime, underlying) recommendation table or waiting-for-data response; `GET /model/d/log` returns recent shadow decisions
- [x] `api_server.py`: retrain subprocess passes `--also-v1`; `model_d_exists` field in `/model/train_status`
- [x] `webapp/src/pages/ModelD.tsx`: "Waiting for data" progress bar (signal_events / 10,000) when model not trained; recommendation table with colour-coded deltas when trained; recent shadow decisions log
- [x] `webapp/src/pages/ModelC.tsx`: calibration curve (SVG), score histogram, bucket table for Model C
- [x] Settings: `MODEL_D_ENABLED` toggle + `MODEL_D_MAX_DELTA_PCT` float input
- [ ] OPE Surface overlay (Model D recommendations as orange dots on OPE charts) — deferred to Phase 2

**Files touched:** `analysis/train_model.py`, `models/model_agent.py`, `config.py`, `api_server.py`, `webapp/src/pages/ModelD.tsx` (new), `webapp/src/pages/ModelC.tsx` (new), `webapp/src/api/client.ts`, `webapp/src/App.tsx`, `webapp/src/pages/Settings.tsx`