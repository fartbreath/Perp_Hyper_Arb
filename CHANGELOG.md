# Changelog

All notable changes to this repository are documented in this file.

## [2026-06-11] — Binance bookTicker oracle; Chainlink stale-secs fix; near-expiry hysteresis; veto-floor reduction; HL mark oracle ITM gate; duplicate-tick guard; ghost-dismiss accounting fix; Model D simulate; signal_events 180-day retention; PM WS subscription optimisation

### Feature — Binance bookTicker as primary spot oracle for 1h/daily/weekly (`market_data/spot_oracle.py`, `market_data/binance_bookticker_client.py`, `config.py`, `main.py`)

`BinanceBookTickerClient` is now wired as the primary spot price source for 1h/daily/weekly markets (which settle against Binance candle close prices).  `SpotOracle._get_spot_1h_daily_weekly()` tries `BinanceBookTickerClient` first (freshness gate: `BINANCE_BOOKTICKER_STALE_SECS`, default 10 s), falling through to RTDS when stale.  `get_spot_age()` for these market types now returns `min(binance_age, rtds_age)` so a live bookTicker snapshot suppresses false `oracle_stale_sl` exits even during brief RTDS outages.  `on_rtds_update()` fires on both RTDS ticks **and** bookTicker ticks.

`oracle_tick_log.py` gains a `SOURCE_BINANCE` constant.

**New config var:** `BINANCE_BOOKTICKER_STALE_SECS: float = 10.0`

---

### Fix — Chainlink Streams stale threshold 3 s → 30 s (`config.py`)

`CHAINLINK_STREAMS_STALE_SECS` was 3.0 s — tighter than the average Data Streams inter-event interval (~1.6 s, with gaps to ~10 s during quiet prices).  This caused frequent false fallthrough to the RTDS relay mid-hold, which triggered monitor callbacks at the relay's 1 Hz heartbeat even when no new Chainlink round had arrived — the root cause of dual-source false stop-losses.

Raised to **30.0 s** (conservative ceiling aligned with the AggregatorV3 deviation heartbeat).  Comment in config updated with measured cadence data (153 k events / 72 h).

---

### Fix — Near-expiry stop: threshold 39 → 20 s + 3-tick hysteresis (`monitor.py`, `config.py`, `config_overrides.json`)

Analysis of 94 near-expiry stop firings revealed a 95.7% false-positive rate: the median TTE at trigger was 28.3 s (above the 39 s threshold), and false-positive positions had only 1–2 consecutive negative-delta ticks (transient oracle noise) vs 112 for the one genuine save.

Two fixes:
- **Threshold**: `MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS` lowered from 39 → **20 s** via `config_overrides.json` (below the 28.3 s median false-positive TTE).
- **Hysteresis**: new `MOMENTUM_NEAR_EXPIRY_MIN_CONSECUTIVE_TICKS` (default 3) requires that many consecutive ticks with `delta < 0` AND `TTE < threshold` before firing.  Counter is stored in the existing `suppress_counts` dict keyed `"{token_id}:ne_neg_delta"` and reset the moment the condition is not met.  When `suppress_counts is None` the counter defaults to threshold (fires immediately — safe fallback).

Post-fix validation (Jun 8–11, 93 trades): 8/8 near-expiry exits are WIN, zero false positives, +$3.28 net (vs −$8.62 wasted on false exits pre-fix).

**New config var:** `MOMENTUM_NEAR_EXPIRY_MIN_CONSECUTIVE_TICKS: int = 3`

---

### Fix — Delta SL veto floor 0.65 → 0.50 (`config_overrides.json`)

`MOMENTUM_DELTA_SL_TOKEN_VETO_FLOOR` was 0.65.  Momentum entries occur at token prices 0.69–0.75, meaning the veto floor engaged immediately after entry and suppressed the delta stop-loss for the full hold.  Lowered to **0.50**: the veto still blocks exits when the crowd price clearly favours the trade (>50% WIN) but no longer fires automatically at typical entry prices.

---

### Feature — HL mark SL oracle ITM confirmation gate (`monitor.py`, `config.py`, `webapp/src/pages/Settings.tsx`)

New suppression gate for the HL perp mark stop-loss: when the Chainlink settlement oracle confirms the position is solidly ITM by more than `MOMENTUM_HL_MARK_SL_ORACLE_ITM_FLOOR_PCT`, the HL mark stop is suppressed.  Rationale: near expiry the HL perp mark diverges from Chainlink due to funding-rate noise — Chainlink is the settlement source, so a solid Chainlink ITM margin overrides a bearish perp mark.  `suppressed` and `itm_floor` fields added to the `hl_mark_sl` warning log.  Settings UI exposes the new knob ("Oracle ITM Floor %").

**New config var:** `MOMENTUM_HL_MARK_SL_ORACLE_ITM_FLOOR_PCT: float = 0.0`

---

### Fix — Duplicate tick guard in PositionMonitor (`monitor.py`)

Multiple oracle callbacks (RTDS + Binance bookTicker, or two chainlink_streams events) could interleave at asyncio `await` boundaries, causing two concurrent `_check_position` coroutines for the same position.  This produced duplicate momentum_ticks.csv rows and could trigger duplicate taker-exit orders.

New `_checking_positions: set[str]` tracks in-flight checks by `"{market_id}:{side}"`.  Both the PM price-change handler and the spot-update handler skip a position check if one is already in flight.

---

### Fix — Ghost-dismiss WIN correction in accounting (`accounting.py`)

When a PM WS gap causes the reconciler to mark a position as a ghost at $0.00 (`ghost_dismissed`), and the market subsequently resolves WIN, the ledger was recording a $0 exit VWAP — booking a full loss on a winning trade.

The resolver now detects this case (`remaining ≤ 0, exit_reason == "ghost_dismissed", token_won, exit_vwap ≈ 0`) and corrects `pos.exit_vwap` to the settlement price (1.0) before writing the ledger row.

---

### Fix — Ghost reconciler: don't dismiss unresolved positions (`live_fill_handler.py`)

The ghost reconciler was closing positions at $0 when the PM wallet API returned no matching position, even for markets that had not yet resolved.  This booked false losses when the market subsequently settled WIN.

The reconciler now only dismisses when the market is confirmed resolved.  Unresolved-market positions are left in memory (a comment chain in the code explains the three cases A/B/C).

---

### Feature — Model D simulate flag; startup O(1) seed (`models/model_agent.py`, `config.py`, `api_server.py`)

- **`MODEL_D_SIMULATE: bool = True`** — when True, Model D recommendations are logged only and never applied to live config.  Exposed in `api_server.py` `_MUTABLE_CONFIG` and `ConfigPatch` so it can be toggled at runtime.
- **Fast cold start**: new `_read_csv_tail` reads only the last 2,000 rows of `shadow_log` via a binary seek from EOF, avoiding a full sequential scan on every startup.  `_seed_sync()` runs dedup-key seeding in an executor thread.

---

### Feature — signal_events 180-day rolling retention (`strategies/Momentum/event_log.py`)

`_KEEP_DAYS` raised from 7 → **180** (trigger: file > 200 MB).  OPE reward surface computation for Model D requires 4–8 weeks of accumulated signal events; 7-day rotation was discarding the data before it could be used for training.  Fast-prefix timestamp check avoids full JSON parse during pruning.

---

### Fix — PM WS subscription optimisation: per-type presub lookahead (`strategies/Momentum/scanner.py`, `config.py`)

`MOMENTUM_PRESUB_LOOKAHEAD = 4` was applied uniformly to all market types, causing weekly markets to pre-subscribe `4 × 7 days = 28 days` of future markets — the dominant driver of 1,808 WS-subscribed tokens (≈ 19 shards at 100-token capacity).

New `MOMENTUM_PRESUB_LOOKAHEAD_BY_TYPE` dict sets per-type lookahead: **1** for 5m/15m/1h/4h (one period ahead), **0** for daily/weekly (subscribe only when active).  The subscription refresh already fires every 30 s **and** on every PM market-discovery event (`on_pm_markets_refreshed`), so a 30-second subscription latency for newly-active daily/weekly markets is negligible.

Expected reduction: **1,808 → ~400–600 tokens** (6–10 fewer WS shards, faster post-PM-restart reconnect).

Investigation finding (confirmed via Polymarket status page): the recurring ABNORMAL_CLOSURE 1006 mass-disconnects on Jun 10–11 are **PM-side CLOB restarts and RTDS infrastructure incidents**, not a CPU/data throughput bottleneck on the bot.

**New config var:** `MOMENTUM_PRESUB_LOOKAHEAD_BY_TYPE: dict[str, int]`

---

### Feature — Training page: failure details + log expand (`webapp/src/pages/Dashboard.tsx`)

When a training run fails, the Dashboard now shows the highest-priority error line extracted from the log tail (regex: `error|auc|minimum|leakage|not installed`) inline below the status badge.  A "Show log / Hide log" toggle reveals the full scrollable log tail (max 260 px).

---

## [2026-05-26] — M-15 HL entry gate; S2.4/S2.5 stale oracle exits; ML-C1 exit snapshot log; Model A feature refresh; delta SL grace fix; accounting pending-exit reason

### Feature — M-15: HL Perp Depth Imbalance Entry Gate (`strategies/Momentum/scanner.py`, `strategies/Momentum/signal.py`, `config.py`, `api_server.py`)

Blocks entry when the HL perp book is heavily positioned against the trade. Position-adjusted imbalance (negative = market against trade) is fetched from `HLClient.get_depth_imbalance()` at scan time. Analysis (77 trades, 2026-05-19): imbalance < −0.30 → 50% WR vs 70.7% for the rest (+9.7pp). XRP excluded — imbalance signal is inverted for that coin. Fail-open when HL WS is not connected (returns None).

**New config vars:**
- `MOMENTUM_HL_ENTRY_GATE_ENABLED: bool = False`
- `MOMENTUM_HL_ENTRY_IMBALANCE_MIN: float = -0.30`
- `MOMENTUM_HL_ENTRY_GATE_EXCLUDE_COINS: list = ["XRP"]`

`hl_entry_imbalance` column added to `momentum_fills.csv` and logged unconditionally (gate off or on). `MomentumSignal.entry_hl_depth_imbalance` field added. Gate and `skipped_hl_entry` counter exposed in scan diagnostics. API patch/GET endpoints updated.

---

### Feature — S2.5: Mid-hold stale oracle exit (`monitor.py`, `config.py`)

New S2.5 exit block fires when the settlement-correct oracle (RTDS for 1h/daily/weekly; Chainlink for 5m/15m/4h) has been silent for `ORACLE_STALE_MID_HOLD_EXIT_SECS` seconds at **any** TTE. Bypasses the winner-suppress gate — a stale oracle cannot confirm the position is winning.

**New config var:** `ORACLE_STALE_MID_HOLD_EXIT_SECS: int = 120`

**New exit reason:** `ExitReason.MOMENTUM_ORACLE_STALE_SL = "oracle_stale_sl"`

---

### Fix — S2.4: Near-expiry stale oracle exit now catches frozen-cache oracles (`monitor.py`, `config.py`)

S2.4 previously required `current_spot is None` to fire, meaning it only triggered when the oracle was completely offline. A stale RTDS feed that replays a cached price (e.g. RTDS disconnected but internal cache still holds the last-known value) would never satisfy `current_spot is None`, leaving the near-expiry exit blind.

Removed the `current_spot is None` condition — S2.4 now fires based on `oracle_age_seconds >= threshold` alone. `spot_available=True/False` added to the warning log for diagnostics.

`ORACLE_STALE_NEAR_EXPIRY_HARD_EXIT_SECS` lowered from 60 → **10** seconds.

---

### Fix — Oracle age tracks settlement-correct feed per market type (`monitor.py`)

`oracle_age_seconds` (used by S2.4, S2.5, and position health reporting) was computed from `_last_oracle_tick_ts`, which is refreshed by **both** RTDS and Chainlink callbacks. This meant a 1h/daily/weekly position with stale RTDS showed `oracle_age` of only 2–3 s if Chainlink was alive, preventing S2.4/S2.5 from firing.

Both call sites now use `self._spot.get_spot_age(pos.underlying, pos.market_type)`, which routes to RTDS age for 1h/daily/weekly and Chainlink age for 5m/15m/4h. Chainlink staying alive can no longer mask a stale RTDS for 1h positions.

---

### Fix — Delta SL grace applies age-only for MAIN positions (`monitor.py`)

The existing grace gate was `_in_grace AND _above_min_tte`. For `bucket_5m` MAIN entries, `tte_seconds < MOMENTUM_MIN_TTE_SECONDS` is a required entry condition, so `_above_min_tte` was always `False` — making the grace period a no-op for all MAIN 5m positions.

Now split by position type:
- **MAIN**: grace = `_in_grace` only (age-based)
- **WINNER (ON-promoted)**: grace = `_in_grace AND _above_min_tte` (both age and TTE)

---

### Feature — ML-C1: Momentum exit snapshot log (`strategies/Momentum/event_log.py`, `monitor.py`)

`write_exit_snapshot()` appends one record to `data/mom_exit_snapshots.jsonl` for every momentum SL exit (`momentum_stop_loss`, `hl_mark_sl`, `prob_sl`, `momentum_near_expiry`, `upfrac_exit`). Not written on RESOLVED or take-profit exits.

**Fields logged:** `bid_delta_pct`, `oracle_delta_pct`, `hl_mark_delta_pct`, `hl_depth_imbalance`, `opposite_bid_depth_usd`, `tte_remaining_secs`, `exit_token_mid`, `entry_price`, `entry_spot`, `exit_reason`.

For YES/UP positions, the NO book is now also fetched in `_check_position` (in-memory lookup, no network cost) to populate `opposite_bid_depth_usd`.

---

### Feature — ML-07: Model A feature set refresh (`models/feature_snapshot.py`, `strategies/Momentum/scanner.py`)

- `deribit_iv` removed from `MODEL_A_FEATURES` — 87.5% null for momentum rows, dropped in model_a_v0 retrain
- `clob_yes_bid_depth_5` added — top-5 YES bid depth in USDC at entry
- `mom_hl_depth_imbalance` added — position-adjusted HL book imbalance at entry
- `is_bucket_5m`, `is_bucket_1h`, `is_bucket_15m`, `is_bucket_4h` added — one-hot flags derived from `market_type`
- `build_entry_snapshot()` updated to populate all new fields; `_ma_context` in scanner updated to pass `clob_yes_bid_depth_5`, `hl_entry_imbalance`, and `market_type`
- Model B v5 exit-time features (`on_winner_bid_at_exit`, `on_loser_bid_at_exit`, `on_oracle_delta_at_exit`, `on_tte_at_exit_secs`) un-commented and activated in `MODEL_B_FEATURES` — XGBoost handles NaN natively; inert until populated by scanner v5 data

---

### Fix — Accounting: pending exit reason survives market resolution (`accounting.py`)

`AccountingPosition.pending_exit_reason` field added. `_Ledger.set_pending_exit_reason(token_id, reason)` pre-records the intended exit reason when a taker exit is attempted. `handle_resolution()` now uses `pending_exit_reason` as fallback instead of `"resolved"` — prevents a near-expiry SL attempt from being recorded as `exit_reason="resolved"` when the market settles before the retry fill arrives.

`set_pending_exit_reason()` is called from both taker-exit retry paths in `_exit_position()` (`monitor.py`).

---

### Fix — Risk: `skip_accounting=True` on startup position restore (`risk.py`, `live_fill_handler.py`)

`RiskEngine.open_position()` gains a `skip_accounting: bool = False` kwarg. The startup restore path in `live_fill_handler.py` now passes `skip_accounting=True` to prevent `on_entry_fill()` from double-counting contracts for positions already recorded in `acct_positions.json`.

---

### Fix — API: Model C calibration handles mixed resolved_outcome types (`api_server.py`)

`model_c_calibration()` now coerces all feature columns to float (`pd.to_numeric(errors="coerce").fillna(-999)`) before calling `predict_proba()`, and handles `resolved_outcome` stored as either int (1/0) or string ("WIN"/"LOSS").

---

### Fix — Scanner: eagerly creates `momentum_fills.csv` on start (`strategies/Momentum/scanner.py`)

`_ensure_momentum_fills_csv()` is now called in `MomentumScanner.start()` so `feature_builder.py` can always open the file even if no fills have occurred in the current session.

---

## [2026-05-19] — Early Warning SL; ML-D4 Model D simulator; Model C simulator; Chainlink zombie-feed fix; near-expiry suppress bypass; config audit trail; OPE page

### Feature — Early Warning SL: HL Mark Price Divergence (Signal A) (`monitor.py`, `hl_client.py`, `config.py`)

Independent stop-loss signal that fires when the HL perp mark price crosses the strike while the Chainlink oracle has not yet updated. The perp mark leads the oracle by 2–5 s on genuine directional moves, giving a narrow but clean early exit window.

**New config vars (all off by default):**
- `MOMENTUM_HL_MARK_SL_ENABLED: bool = False`
- `MOMENTUM_HL_MARK_SL_THRESHOLD_PCT: float = 0.0` — fires when mark divergence < this (0.0 = mark crossed strike; negative = allow slack)
- `MOMENTUM_HL_MARK_SL_MAX_TTE: int = 30` — only active within this many seconds of expiry

**New exit reason:** `ExitReason.MOMENTUM_HL_MARK_SL = "hl_mark_sl"`

**HLClient changes:** `FundingSnapshot.mark_px` field added; `get_mark_price(coin)` method returns current webData2 mark price. New momentum_ticks.csv column: `hl_mark_price`, `hl_mark_div_pct`.

---

### Feature — Early Warning SL: HL Perp Depth Imbalance (Signal B) (`monitor.py`, `hl_client.py`, `config.py`)

Independent stop-loss signal that fires when the HL perp order book is heavily positioned against the trade (asks >> bids for UP trades, bids >> asks for DOWN). Computed from the top N levels of the l2Book WS stream.

**New config vars (all off by default):**
- `MOMENTUM_HL_DEPTH_SL_ENABLED: bool = False`
- `MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD: float = 0.40` — fires when position-adjusted imbalance < −threshold
- `MOMENTUM_HL_DEPTH_SL_MAX_TTE: int = 30`
- `MOMENTUM_HL_DEPTH_SL_LEVELS: int = 5` — order book levels to sum

**New exit reason:** `ExitReason.MOMENTUM_HL_DEPTH_SL = "hl_depth_sl"`

**HLClient changes:** `_depth_imbalance` dict updated on every l2Book WS message; `get_depth_imbalance(coin)` returns position-adjusted imbalance in [−1, +1]. New momentum_ticks.csv column: `hl_depth_imbalance`.

---

### Feature — Near-expiry suppress bypass TTE gate (`monitor.py`, `config.py`)

`MOMENTUM_NEAR_EXPIRY_SUPPRESS_BYPASS_TTE: int = 30` — within this many seconds of expiry the `suppress_taker_exits` gate is bypassed for the near-expiry check. Prevents a 0.92-token from escaping stop-loss in the final 30 s where terminal collapse is observed empirically.

Set to 0 to disable (previous behaviour).

---

### Fix — Chainlink zombie-feed staleness (`market_data/spot_oracle.py`, `config.py`)

`SpotOracle.get_spot()` now treats a Chainlink Streams snapshot as stale if its age exceeds `CHAINLINK_STREAMS_STALE_SECS` (default 3.0 s). A connected-but-frozen feed (upstream repeating the same price) falls through to the RTDS relay rather than blocking it. SpotOracle switches back to Chainlink Streams automatically when a fresh snapshot arrives — no sticky state.

Previously, a zombie Streams feed would suppress the RTDS relay indefinitely, causing oracle data to appear stuck even when RTDS was healthy.

---

### Feature — ML-D1: Signal event log + position snapshots (`strategies/Momentum/event_log.py`, `strategies/Momentum/scanner.py`, `monitor.py`)

Scan diagnostic rows that reach the vol/delta computation stage are appended to `data/signal_events.jsonl` via `emit_signal_events_batch()`. Gate results (z_pass, funding_pass, depth_share_pass, twap_pass, entered) are computed from `skip_reason` and stored alongside all ML features.

`write_position_snapshot()` appends open-position state snapshots to `data/position_snapshots.jsonl` at each monitor tick for ML-D4 context attribution.

PositionMonitor tracks `_last_upfrac` per market for ML-D1 upfrac feature.

---

### Feature — ML-D2: Config audit trail in acct_ledger (`accounting.py`)

Five new columns added to `LEDGER_HEADER` and `_write_ledger_record`:
`z_score_used`, `kelly_multiplier_used`, `delta_sl_pct_used`, `upfrac_threshold_used`, `loser_exit_trigger_used`.

Captures live config values at close time (session-granularity attribution — stable within a session; config_overrides.json reloads apply to all trades opened after the reload).

---

### Feature — ML-D4: Multi-output Model D v0 — Config Policy Simulator (`analysis/train_model.py`, `models/model_agent.py`, `config.py`, `api_server.py`, webapp)

Model D is a set of 3 independent `XGBRegressor` models (one per dimension: `z_score`, `kelly`, `delta_sl`) that recommend config deltas per market context. Trained accretively from `training_data.parquet` using OPE reward surface labels (optimal − live) as targets. Shadow mode only — deltas are never applied to live config.

**`analysis/train_model.py`:**
- `_derive_model_d_labels(df)` — calls `build_surface()` per (vol_regime, underlying) group, assigns `target_delta_z_score/kelly/sl` labels
- `_generate_model_d_shap_report()` — per-dimension SHAP beeswarm HTML report
- `MODEL_D_MIN_ROWS = 30` — skips training gracefully when insufficient data
- `--also-v1` flag — retrain always writes `model_b_v1.pkl` + `model_b_v1_shap.html` (previously only written when explicitly targeting v1)

**`models/model_agent.py`:**
- `score_config_policy(market_id, context)` — returns `{delta_z_score, delta_kelly, delta_sl}` clamped to ±`MODEL_D_MAX_DELTA_PCT`; `None` when disabled
- `_write_model_d_row()` — async append to `analysis/model_d_log.csv` (separate file; no shadow_log schema churn)
- `get_model_d_log(limit)` — public method for API

**`config.py`:** `MODEL_D_ENABLED`, `MODEL_D_PATH`, `MODEL_D_MAX_DELTA_PCT` (±50% default), plus `MODEL_C_ENABLED`, `MODEL_C_SUPPRESS_THRESHOLD`, `MODEL_C_PATH`.

**`api_server.py`:**
- `GET /model/d/recommendations` — per-(vol_regime, underlying) recommendation table or waiting-for-data (n_signal_events / 10,000 target)
- `GET /model/d/log` — recent shadow decisions from `model_d_log.csv`
- `GET /model/c/calibration` — Model C calibration curve (predicted P(WIN) vs actual win rate in decile buckets) + score histogram
- `model_d_exists`, `model_c_exists` in `/model/train_status`
- `MODEL_D_ENABLED` / `MODEL_D_MAX_DELTA_PCT` in `_MUTABLE_CONFIG`, `ConfigPatch`, `GET /config`
- Retrain subprocess now passes `--also-v1`

**Webapp:**
- `webapp/src/pages/ModelD.tsx` (new) — "Waiting for data" progress bar (% toward 10k signal events, ~July 2026) when model not trained; recommendation table with colour-coded Δ values (green=loosen, red=tighten) when trained; recent shadow decisions log
- `webapp/src/pages/ModelC.tsx` (new) — SVG calibration curve, score distribution histogram, bucket table; "Model C not trained yet" state when pkl absent
- `webapp/src/App.tsx` — `/model-c` and `/model-d` routes added to Model Sim nav dropdown
- `webapp/src/pages/Settings.tsx` — Model D toggle + `MODEL_D_MAX_DELTA_PCT` conditional float input
- `webapp/src/api/client.ts` — `ModelCCalibrationResponse`, `ModelDRecommendationsResponse`, `ModelDLogResponse` interfaces + poll hooks; `model_c_exists` / `model_d_exists` in `ModelTrainStatus`

---

### Feature — ML-D3: OPE Reward Surface webapp page (`webapp/src/pages/OPE.tsx`, `api_server.py`)

New `/ope` route in the Model Sim dropdown. Fetches `GET /ope/surface` with vol_regime / underlying filters and renders reward surface heatmaps + optimal config table.

---

### Feature — Kelly at-cap size limit + LOW-vol TWAP gate (`config.py`, `strategies/Momentum/scanner.py`)

- `MOMENTUM_KELLY_AT_CAP_MAX_USD: float = 5` — hard cap on position size when Kelly win_prob reaches the 0.95 cap. At-cap entries have the same win rate as sub-cap entries but produce larger losses when they fail (avg −$0.88 vs −$0.03).
- `MOMENTUM_TWAP_REQUIRE_DATA_LOW_VOL: bool = True` — blocks entry in LOW vol regime when `twap_dev_bps` is unavailable. LOW vol + NaN TWAP observed 50% win rate vs 91% when TWAP is present; fail-closed prevents the TWAP multiplier from silently doing nothing.

---

## [2026-05-16] — Accounting CLOB enrichment + reconcile sweep; Model B feature fix; RTDS stability; ON FAK slippage; asyncio watchdog; analysis OPE suite

### Fix — Model B `score_exit` feature mismatch (`models/feature_snapshot.py`)

**Root cause:** `MODEL_B_FEATURES` in `feature_snapshot.py` had drifted to 15 features while
`model_b.pkl` was trained on 10.  XGBoost raised `feature_names mismatch` on every `score_exit`
call, causing the model to return 0.5 (neutral) for every open ON position — Model B was
effectively disabled since the last retrain.

**Fix:** Synced `MODEL_B_FEATURES` back to the 10-feature list in `analysis/train_model.py`.
The 5 extra features (`oracle_delta_pct`, `implied_prob`, `on_yes_depth_share`, `on_funding_rate`,
`on_combined_cost`) were added to `feature_snapshot.py` during an earlier refactor but were never
included in the training run.  Comment block added to both files warning against future drift.

**Requires bot restart** to clear the stale import.

---

### Fix — RTDS stale-reconnect churn (`market_data/rtds_client.py`)

**Root cause:** `RTDS_STALE_RECONNECT_S = 45` was too aggressive.  The `crypto_prices_chainlink`
RTDS topic is event-driven — during stable markets it legitimately goes 40-60 s between pushes.
The reconnect fired prematurely, cleared `_prices` / `_cl_prices`, briefly made `get_spot()`
return `None`, and caused FeedHealth to report DOWN (HTTP 503) for open positions.

**Changes:**
- `RTDS_STALE_RECONNECT_S` raised from 45 s to 120 s.  Reconnect now only fires after 2 minutes
  of genuine exchange-price silence.
- `_prices.clear()` / `_cl_prices.clear()` on reconnect removed.  Prices stay STALE (not DOWN)
  during the reconnect window — STALE never triggers 503.

---

### Fix — Ghost position close uses settlement price (`live_fill_handler.py`)

**Root cause:** Ghost positions (absent from PM wallet during reconcile) were unconditionally
closed at `exit_price=0.0`.  If the market had resolved while the WS was down, this recorded a
$0 loss instead of the real WIN/LOSS settlement price.

**Changes:**
- Before closing a ghost, `fetch_market_resolution(market_id)` is called.  If resolved, the
  position is closed at the correct settlement price (1.0 WIN or 0.0 LOSS).
- `exit_reason="ghost_dismissed"` and `resolved_outcome` passed to `close_position`.
- `OPENING_NEUTRAL_DRY_RUN` positions explicitly skipped — they are never in the PM wallet by
  design, so attempting to ghost-dismiss them corrupted P_WINNER ledger rows.

---

### Fix — PM WS shard max markets per shard (`config.py`, `pm_client.py`)

**Root cause:** `PM_WS_MAX_MARKETS_PER_WS=100` was too high; shards were rejected with
"INVALID OPERATION" at ~94+ tokens.  `custom_feature_enabled` (best_bid_ask events) was
unconditionally included, generating events for all subscribed tokens and saturating the event
loop.

**Changes:**
- `PM_WS_MAX_MARKETS_PER_WS` lowered from 100 to 50.
- `PM_WS_BEST_BID_ASK: bool = True` flag added.  `custom_feature_enabled` is now gated on this
  flag so it can be disabled when shard 1006 cascades occur at scale.

---

### Feature — Accounting CLOB enrichment from PM `/data/trades` (`accounting.py`)

Ledger rows are now enriched with authoritative fill prices, sizes, and fees from PM's
`/data/trades` endpoint at close time.

**New functions:**
- `set_pm_client(pm)` — registers the live `PMClient` singleton so ledger writes can call CLOB.
  No-op in PAPER mode or tests.
- `_enrich_position_from_clob(pos)` — pulls actual taker execution records per order ID; overwrites
  `entry_contracts`, `entry_vwap`, `exit_contracts`, `exit_vwap`, `fees_usd` when PM differs from
  in-memory WS-fill values.  Settlement exits keep their vwap but refresh fees.  Returns a
  `reconciliation_notes` string describing any non-trivial deltas.
- `_check_clob_balance_divergence(pos)` — compares expected residual contracts against on-chain
  CLOB balance for RESOLVED/REDEMPTION exits.  If divergence > 5% (`_DIVERGENCE_THRESHOLD = 0.05`),
  caps `exit_contracts` at the actual redeemable balance and appends `RECONCILE_REQUIRED` to notes.

---

### Feature — Manual ledger vs PM `/activity` reconcile sweep (`api_server.py`)

New authenticated endpoint `POST /reconcile/run?days=N` (default 14 days, max 90).

Computes:
- `ledger_pnl` — sum of `net_pnl` in `acct_ledger.csv` over the window
- `pm_realized` — `sum(SELL + REDEEM - BUY)` from PM data-api `/activity`
- `drift_usd` — ledger vs PM difference
- `per_market` — same breakdown grouped by market title, sorted worst-first
- `reconciliation_flagged` — ledger rows tagged `RECONCILE_REQUIRED` by the balance divergence guard

Read-only.  Never mutates the ledger.  Triggered from the Performance page.

---

### Feature — ON entry FAK slippage cap and depth margin (`config.py`, `strategies/OpeningNeutral/scanner.py`)

**Root cause:** One-leg-fill failures occurred when the top-of-book ask was swept in the
millisecond between book-cache snapshot and FAK matcher arrival.  The YES leg was killed with
"no orders found to match" while the NO leg filled simultaneously.

**New config keys:**
- `OPENING_NEUTRAL_FAK_SLIPPAGE_CAP: float = 0.01` — price cap added above observed best ask.
  Lets the FAK sweep to the next price level rather than dying at the swept level.
- `OPENING_NEUTRAL_DEPTH_MARGIN_MULT: float = 2.0` — both legs must show resting ask size >= 2x
  required contracts in the book cache before the FAK is sent.

---

### Feature — asyncio slow-callback watchdog (`main.py`)

`loop.slow_callback_duration` set to `ASYNCIO_SLOW_CALLBACK_SECS` (default 0.5 s) at startup.
When any coroutine blocks the loop beyond this threshold, asyncio logs a WARNING naming the exact
callable and source location.  Root diagnostic for PM WS shard 1006 cascades — shards die because
their 20 s `ping_timeout` fires when the loop cannot process pongs in time.

---

### Feature — Model accuracy tracking (`models/model_agent.py`)

`get_status()` now returns `model_a_accuracy`, `model_b_accuracy`, `model_a_resolved`,
`model_b_resolved` computed across resolved shadow log rows since startup.

- Model A (entry): correct when `score >= 0.5` and `outcome == WIN`.
- Model B (exit): correct when `score < 0.5` and `outcome == LOSS`.

Also: `_pending` dict corrected from `dict[str, float]` (row index) to `dict[str, str]`
(decision_type).  `_resolve_pending_outcomes` now reads `acct_ledger.csv` instead of the
removed `trades.csv`.

---

### Fix — ON market scope narrowed to `bucket_5m` (`config.py`)

`OPENING_NEUTRAL_MARKET_TYPES` restricted to `["bucket_5m"]`.  The 15m bucket had a 60% win
rate and negative mean PnL per pair while 5m was at 95.7%+.

---

### Fix — Loser confidence gate disabled (`config.py`)

`OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED` set to `False`.  With ~390 pairs the feature was
tightening the wrong leg trigger based on sparse funding/depth signals.

---

### Fix — Monitor gate suppression log throttling (`monitor.py`)

The `gate_suppressed` warning for `suppress_prob_sl_oracle` was missing the `_should_log_count`
power-of-2 throttle guard, causing a log storm on every consecutive suppression.  Added to match
the existing `suppress_delta_sl` pattern.

---

### Fix — Priority token deregistration after resolution (`monitor.py`)

After `close_position()` on a resolved market, `pm.deregister_priority_token(token_id)` is now
called.  Previously, resolved tokens stayed in the priority set indefinitely, consuming polling
budget for tokens with no open position.

---

### Analysis — OPE suite and Model D infrastructure (`analysis/`)

Five new scripts for offline policy evaluation and Model D signal collection:

- **`scan_diags_collector.py`** — polls `/momentum/diagnostics` every 2 s; deduplicates by
  `scan_ts`; filters pre-signal structural skips; appends to `data/scan_diags.jsonl`.
- **`stop_loss_ope.py`** — sweeps `MOMENTUM_DELTA_STOP_LOSS_PCT` thresholds 0.5-10% against
  `momentum_ticks.csv` + `market_outcomes.json`; computes TP/FP/precision/recall/FP-rate per
  threshold; faceted by coin and bucket.  Current 4% threshold: 87% FP rate.
- **`z_score_ope.py`** — sweeps z-threshold on `training_data.parquet` momentum rows; computes
  mean_pnl/win_rate/kelly_weighted_pnl per threshold; faceted by bucket/coin.  Best z=1.86
  (mean_pnl=+0.68) vs live z=0.80 (mean_pnl=-0.12); `bucket_5m` shows 95.7% win rate.
- **`config_snapshot_logger.py`** — polls `config_overrides.json` every 3600 s; SHA-256
  deduplication; appends `{ts, config_hash, config}` to `data/config_snapshots.jsonl`.
- **`feature_builder.py`** (updated) — v5 exit-time fields added to schema, float cast list, and
  derivation block: `on_winner_bid_at_exit`, `on_loser_bid_at_exit`, `on_oracle_delta_at_exit`,
  `on_tte_at_exit_secs`, `on_loser_bid_delta`, `on_winner_bid_delta`.

---

## [2026-05-09] — PM WS ping race fix; RON lock-step sync; ON loser-exit callbacks; institutional-grade gate audit logging; feed health monitoring; position data safety guards; analysis tooling

### Fix — PM WS shard ping race causing stale books (`pm_client.py`)

**Root cause:** The manual `_ping_loop` task called `ws.send("PING")` concurrently with the
websockets library's own transport teardown on Windows ProactorEventLoop.  This triggered an
`AttributeError: 'NoneType'.resume_reading` crash in the shard's receive loop, disconnecting
the shard silently and freezing all book state for its tokens.  The ON strategy's winner-confirm
gate (ON-07) then saw a stale winner bid of ~0.57 instead of the real ~0.65+, permanently
blocking loser exits.

**Changes:**
- `_ping_loop` method and its `create_task` / `finally: ping_task.cancel()` scaffolding removed.
- `ping_interval=config.PM_WS_PING_INTERVAL, ping_timeout=20` restored on `websockets.connect()`.
  The library now owns the RFC-6455 binary ping lifecycle, synchronized with its own transport.
- `AttributeError` workaround except-block removed (root cause is fixed; workaround was masking it).
- `_connected_at`, `_last_message_at`, `_message_count` tracking fields added for feed health.

**Result:** No shard crashes observed in 75-minute live session post-fix.  ON-07 winner bid reads
correctly; loser exits fire as expected.

---

### Fix — PM WS shard incremental subscription (`pm_client.py`)

**Root cause:** `update_tokens()` only updated the in-memory token set.  Tokens registered
mid-session (new momentum markets, presub lookahead) never received a PM WS book snapshot until
the shard's next natural reconnect.

**Change:** When new tokens are added while a shard is connected, `_subscribe_incremental()` sends
an additive `{"operation": "subscribe", "assets_ids": [...]}` message on the live connection.
Neither path requires a reconnect.

---

### Fix — RON fires in lock-step with ON loser exit (`strategies/ReverseOpenNeutral/scanner.py`, `strategies/OpeningNeutral/scanner.py`)

**Root cause:** RON independently monitored PM bid events and fired when *any* leg crossed the bid
threshold.  For the DOGE pair, NO hit 0.29 first; RON "sold YES (winner) at 0.50 and held NO" —
then YES collapsed to 0.27 (the actual loser), so RON had bet on the wrong side ~90 s early.

**Changes:**
- `strategies/OpeningNeutral/scanner.py`: added `_on_loser_exit_callbacks` list and
  `register_loser_exit_callback(cb)`.  In `_on_exit_fill`, immediately after the idempotency
  guard, all registered callbacks are fired as named asyncio tasks
  (`on_loser_exit_cb_{pair_id[:12]}`).
- `strategies/ReverseOpenNeutral/scanner.py`: removed `self._pm.on_price_change(...)`.  Replaced
  `_on_price_event` with `_notify_loser_exit(on_pair_id, loser_side, exit_price)` which fires in
  the exact same event-loop tick as ON's `_on_exit_fill`.  `yes_trigger`/`no_trigger` keys removed
  from pair dict (no longer needed).

**Result:** RON always exits the correct leg at the correct moment, fully coupled to ON.

---

### Feature — Institutional-grade gate audit logging (S1) (`monitor.py`, `config.py`, `core/types.py`)

Gates that suppress exits now emit `WARNING`-level `gate_suppressed` log events after
`GATE_LOG_CONSECUTIVE_THRESHOLD` consecutive suppressions on the same entity, and again at
power-of-2 multiples thereafter (threshold, 2×, 4×, …).

**Gates covered:** `suppress_taker_exits`, `suppress_delta_sl`.

**New config keys:**
- `GATE_LOG_CONSECUTIVE_THRESHOLD: int = 10`

---

### Feature — Position data safety guards (S2) (`monitor.py`, `config.py`, `market_data/spot_oracle.py`)

Three runtime guards prevent the bot from holding a position blindly when data feeds degrade:

- **S2.2 — Oracle REST fallback:** when the oracle is stale for
  `ORACLE_STALE_POSITION_FALLBACK_SECS` (default 60 s) and a position is open for that coin, a
  one-shot REST price fetch tops up the oracle cache.
- **S2.3 — PM book REST refresh:** when a priority token's book age exceeds
  `POSITION_BOOK_FALLBACK_AGE_SECS` (default 45 s), a REST book refresh is triggered via
  `PMClient.fetch_book_rest()`.
- **S2.4 — Near-expiry hard exit on stale oracle:** if TTE < near-expiry threshold AND oracle
  stale ≥ `ORACLE_STALE_NEAR_EXPIRY_HARD_EXIT_SECS` (default 60 s) AND token is not clearly
  winning, fire a taker exit rather than hold blind to resolution.

**New config keys:** `ORACLE_STALE_POSITION_FALLBACK_SECS`, `POSITION_BOOK_FALLBACK_AGE_SECS`,
`ORACLE_STALE_NEAR_EXPIRY_HARD_EXIT_SECS`.

---

### Feature — Feed health monitoring (S3) (`monitor.py`, `config.py`)

- **S3.3 — Chainlink reconnect rate alerting:** warn when rolling-1h reconnect count for any
  Chainlink origin exceeds `CHAINLINK_MAX_RECONNECTS_1H` (default 10).
- **S3.4 — HL WS mark price staleness:** warn when mark price age for a coin with open hedges
  exceeds `HL_WS_STALE_SECS` (default 30 s).

---

### Config — `MOMENTUM_BOOK_MAX_AGE_SECS` relaxed (`config.py`)

Raised from 30 s → 120 s.  The 30 s threshold was too aggressive: a single PM WS message
arriving 31 s after the previous one (normal during low-volume periods) caused the bot to skip
the entire market for that tick, suppressing valid entries.

---

### Analysis — ON vs RON comparison tooling (`analysis/on_vs_ron.py`, `analysis/backfill_ron_settlement.py`, `analysis/check_acct.py`, `analysis/ron_winners.py`)

Post-session analysis scripts added:

- `on_vs_ron.py`: full ON vs RON comparison table.  ON PnL sourced from `acct_ledger.csv`
  (correct per-pair contract sizes); RON PnL uses `winner_sold_price + loser_settlement - cost`.
- `backfill_ron_settlement.py`: reads `acct_ledger.csv` to determine whether each RON pair's
  held leg settled WIN or LOSS; writes `loser_settlement` column to `ron_fills.csv`.
- `check_acct.py` / `ron_winners.py`: ad-hoc audit helpers.

**Session result (29 logical pairs):** ON +$20.64, RON −$14.35 (loser=0 assumption).  RON is
structurally negative EV — winner is capped at ~0.65–0.80 while holding loser to settlement.

---

## [2026-05-08] — WebSocket stability: PM shard 1006 fix; RTDS staleness reconnect; Chainlink Streams HA phase-shift; event-loop yield

### Fix — PM WS shard mass-disconnect (code 1006) (`pm_client.py`)

**Root cause:** `update_tokens()` was closing every shard's WebSocket on every market-refresh cycle
(~30 s). With 16+ shards reconnecting simultaneously, the PM server dropped connections with code
1006. Additionally, `create_task` per WS message at startup flooded the event loop with ~10 800
concurrent tasks, starving server-pong handlers.

**Changes:**
- `update_tokens()` no longer forces a WS reconnect — the `_close_ws()` path and
  `asyncio.ensure_future` call removed entirely. New tokens are picked up on the shard's next
  natural reconnect.
- Receive loop: `await self._on_message(raw)` + `await asyncio.sleep(0)` replaces
  `asyncio.create_task(...)` per message. Keeps ready-task count ~18 + callbacks instead of 10k+.
- `ping_interval=None, ping_timeout=None` — PM server does not respond to client pings; server
  owns keepalive.
- Startup stagger: shard `i` waits `i × 1.0 s` before first connect so 16 shards do not flood
  the server simultaneously.
- Reconnect stagger: `backoff + (shard_id % 8) × 0.5 s` prevents a mass-disconnect wave
  collapsing back into simultaneous reconnects.

**Result:** Zero server-initiated 1006 disconnects observed after fix.

---

### Fix — RTDS stale-price reconnect (`market_data/rtds_client.py`)

**Root cause:** The RTDS server occasionally kept the TCP connection alive (responding to pings)
while silently stopping price pushes. The 15 s recv timeout never fired because pings/pongs
kept the socket alive. Spot prices went stale indefinitely.

**Changes:**
- `_reconnect_requested: bool` flag added (set by health loop, cleared by recv loop).
- Health loop now detects when ALL tracked coins are stale ≥ 120 s and sets
  `_reconnect_requested = True` instead of calling `ws.close()` (which could race with the recv
  loop on Windows).
- Recv loop checks the flag at the top of every cycle and breaks cleanly to trigger reconnect.
- `recv()` timeout tightened from 15 s → 10 s (pings fire every 5 s so 10 s is still generous).

**Result:** Bot reconnects within one health-loop cycle (~60 s) when RTDS stops delivering prices.

---

### Fix — Chainlink Streams HA phase-shift (`market_data/chainlink_streams_client.py`)

**Root cause:** The Chainlink Mercury infrastructure enforces a ~30 s session TTL per WebSocket,
RST-ing both HA origins within ~2 s of each other. Because the two origins share the same
connect/reconnect timing, they converge back into phase after every dual-RST cycle, causing
brief windows where `active_connections=0`.

**Changes:**
- **Partial reconnect delay:** when one origin drops while the other is still alive, the
  reconnect loop waits `_WS_SILENCE_TIMEOUT_S / 2 + random.uniform(0, 2)` ≈ 15-17 s before
  reconnecting. This keeps the two origins permanently ~15 s out of phase.
- **Full reconnect (both down):** reconnects immediately with original backoff + jitter (fast
  recovery path). Emits a `WARNING` log.
- **Startup stagger:** origin `'002'` starts `_ORIGIN_STAGGER_S = 15 s` after `'001'` via
  `initial_delay` in `_conn_loop()`.
- `ping_interval=None, ping_timeout=None` — server owns keepalive; client pings were not
  needed and had no effect on session TTL.
- Routine connect/disconnect log lines demoted to `DEBUG`. Only `full_reconnects` emit `WARNING`.
- `ConnectionClosed` and `(AttributeError, OSError)` catch blocks added with descriptive
  `DEBUG` log messages instead of silent `break`.

**Result:** Every reconnect now shows `active_connections=2`. Zero "all connections down" warnings
observed in live run after fix.

---

### Fix — ModelAgent independent scan event-loop yield (`models/model_agent.py`)

**Root cause:** Iterating ~4 000 PM markets with `sklearn.predict_proba` per market blocked the
asyncio event loop for 8-20 s. This starved WS keepalive pong handlers and was the root cause
of WS disconnects observed during the investigation.

**Change:** `await asyncio.sleep(0)` inserted every 50 markets in `_independent_scan_loop()` so
the event loop can process I/O (WebSocket pongs, RTDS data, etc.) between batches.

---

### Cleanup — Remove asyncio debug mode from `main.py`

Temporary `asyncio.run(main(), debug=True)`, `loop.slow_callback_duration = 0.5`, and
`logging.getLogger("asyncio").setLevel(DEBUG)` removed now that the blocking operation was
identified (ModelAgent scan loop) and fixed.

---

## [2026-05-06] — ML Phase 3+4 (model-assisted exits + paper trading); WINNER-fill delta SL; risk.py CSV removal; HL mark price; SPA catch-all

### Feature — ML Phase 3: Model B exit gate (ML-06) (`strategies/OpeningNeutral/scanner.py`, `models/model_agent.py`, `config.py`)

Implements the Model B exit suppression gate for Opening Neutral.  After ON-06 passes, Model B
scores the CLOB context; if `model_b_score < MODEL_B_SUPPRESS_THRESHOLD` the loser exit is
suppressed and the rules bot continues holding.

**Config keys added:** `MODEL_B_ENABLED = False`, `MODEL_B_SUPPRESS_THRESHOLD = 0.5`

**Bugs fixed:**
- **BUG-ML-06a (High):** `implied_prob` in the Model B feature context was sourced from
  `mon_pos.entry_price` (stale 0.5 pair-open price) instead of `best_bid` (the current CLOB
  bid that triggered the exit check).  Model B was being scored on the wrong signal.
- **BUG-ML-06b (Medium):** Suppressed exits were never written to `shadow_log.csv`.
  `log_model_b_suppression()` async method added to `ModelAgent`; scanner suppression path
  now spawns `asyncio.create_task(self._model_agent.log_model_b_suppression(...))`.

---

### Feature — ML Phase 3: Model A sizing scale (ML-07) (`strategies/OpeningNeutral/scanner.py`, `strategies/Momentum/scanner.py`, `config.py`)

Model A scale applied at the Kelly sizing step in both Momentum and Opening Neutral scanners.
Formula: `scale = MODEL_A_MIN_SCALE + score × (MODEL_A_MAX_SCALE − MODEL_A_MIN_SCALE)`, clamped
to `[MODEL_A_MIN_SCALE, MODEL_A_MAX_SCALE]`.  Phase 3: upscaling disabled (`MODEL_A_MAX_SCALE = 1.0`).
On inference exception sizing falls back to unscaled base Kelly.

**Config keys added:** `MODEL_A_ENABLED = False`, `MODEL_A_MIN_SCALE = 0.5`, `MODEL_A_MAX_SCALE = 1.0`

**Bugs fixed:**
- **BUG-ML-07a (High):** Model A scale was entirely absent from
  `strategies/OpeningNeutral/scanner.py`.  PRD requires scale in both strategies — only Momentum
  had it.  Added full Model A scale block in `_enter_pair()`.  `_pending_ma_scores[pair_id]`
  dict added as staging cache for CSV injection.
- **BUG-ML-07b (Medium):** `model_a_score` and `model_a_scale` absent from `_ON_FILLS_HEADER`.
  `feature_builder.py` cannot pick up these columns for training.  Added both columns to
  `_ON_FILLS_HEADER` (schema bumped to v4).  `_register_pair()` injects values from
  `_pending_ma_scores`.

---

### Feature — ML Phase 4: Independent entry scan + paper ledger (ML-08, ML-09) (`models/model_agent.py`, `config.py`)

**ML-08:** `ModelAgent` extended with `_independent_scan_loop()` that evaluates all PM markets
via `score_entry()` without applying z-score, funding gate, or TWAP gate pre-filters.  When
`MODEL_A_INDEPENDENT_ENABLED=True` and `model_a_score > MODEL_A_INDEPENDENT_ENTRY_THRESHOLD`, a
proposed entry is logged to `analysis/model_paper_trades.csv` with `status=proposed`.
`would_rules_have_entered` determined via `_momentum_scanner._last_scan_diags`.  Hard limits:
TTE ≥ `MODEL_A_MIN_TTE_SECS`, open paper positions < `MODEL_A_MAX_OPEN_POSITIONS`.

**ML-09:** Paper trades written exclusively to `analysis/model_paper_trades.csv` via
`_write_paper_trade_row()`.  On resolution, `_resolve_paper_outcomes()` writes `exit_price`,
`pnl`, `status=closed` using CLOB `winner` flag.  No interaction with `trades.csv`,
`on_fills.csv`, `momentum_fills.csv`, or `risk.py` position tracking.
`analysis/model_paper_trades.csv` added to `.gitignore`.

**Config keys added:** `MODEL_A_INDEPENDENT_ENABLED = False`,
`MODEL_A_INDEPENDENT_ENTRY_THRESHOLD = 0.7`, `MODEL_A_MIN_TTE_SECS = 30`,
`MODEL_A_MAX_OPEN_POSITIONS = 5`

---

### Feature — ML scanner wiring (`main.py`)

`model_agent` injected into both scanners post-construction
(`momentum_scanner._model_agent`, `opening_neutral_scanner._model_agent`) for gate/scale
decisions.  `momentum_scanner` back-wired into `model_agent._momentum_scanner` for
`would_rules_have_entered` determination.  All assignments happen after connector startup but
before asyncio tasks start (safe single-threaded window).

---

### Feature — Webapp SPA catch-all + ML Settings section (`api_server.py`, `webapp/src/pages/Settings.tsx`, `webapp/src/api/client.ts`)

**SPA catch-all:** `@app.get("/{full_path:path}")` added as the last route in `api_server.py`,
serving `webapp/dist/index.html` for all non-API paths.  `StaticFiles` mount for `/assets`
added before the catch-all.  Fixes 404 on direct navigation to `/model-paper`.

**Model Paper Trades page** (`webapp/src/pages/ModelPaperTrades.tsx`): new page at route
`/model-paper` showing the paper trade ledger table (timestamp, market_id, model score,
entry/exit price, PnL, `would_rules_have_entered`).  Filterable by `would_rules_have_entered`.
Added to `App.tsx` nav.

**Settings ML section:** "Model Agent (ML)" card added to Settings page with all 10 ML
config controls: `MODEL_AGENT_ENABLED` (restart-required), `MODEL_B_ENABLED`,
`MODEL_B_SUPPRESS_THRESHOLD`, `MODEL_A_ENABLED`, `MODEL_A_MIN_SCALE`, `MODEL_A_MAX_SCALE`,
`MODEL_A_INDEPENDENT_ENABLED` (restart-required), `MODEL_A_INDEPENDENT_ENTRY_THRESHOLD`,
`MODEL_A_MIN_TTE_SECS`, `MODEL_A_MAX_OPEN_POSITIONS`.

`api_server.py` `_SETTINGS_MAP`, `ConfigPatch`, and `GET /config` updated with all 10 ML
fields.  `webapp/src/api/client.ts` `ConfigData` interface updated.  Webapp dist rebuilt.

---

### Feature — WINNER-fill delta SL (`risk.py`, `monitor.py`, `config.py`)

`Position.fill_type: str = "MAIN"` field added; Opening Neutral scanner sets
`fill_type = "WINNER"` on ON-promoted positions.  `should_exit()` reads `fill_type`:
- WINNER positions multiply `_sl` by `MOMENTUM_WINNER_DELTA_SL_MULTIPLIER` (default 0.5 →
  half the per-coin threshold).
- WINNER positions use `MOMENTUM_WINNER_DELTA_SL_GRACE_SECS` (default 150 s) instead of
  the standard `MOMENTUM_DELTA_SL_GRACE_SECS` (90 s).

Prevents ON-promoted winners mid-bucket from being stopped out on transient oscillation.

**Config keys added:** `MOMENTUM_WINNER_DELTA_SL_MULTIPLIER = 0.5`,
`MOMENTUM_WINNER_DELTA_SL_GRACE_SECS = 150`

---

### Feature — HL mark price propagation (`hl_client.py`, `market_data/funding_rate_cache.py`)

`HLClient._fire_funding()` now passes `mark_px` extracted from the `webData2` `markPx` field.
`FundingRateCache.on_ws_update()` accepts and caches `mark_px`; new `get_mark(coin) → Optional[float]`
accessor added.  Backward-compatible: old callbacks without `mark_px` handled via `TypeError` catch.

---

### Refactor — `risk.py` CSV removal (`risk.py`, `monitor.py`)

`TRADES_CSV`, `TRADES_HEADER`, `_ensure_csv()`, `_append_csv()`, `_write_csv_row()`, and
`_csv_write_lock` removed from `risk.py`.  Trade recording fully managed by `accounting.py`
(`acct_ledger.csv`).  540-line net reduction.

`_pm_reconcile_loop()` removed from `monitor.py` — it was the background loop that patched
`trades.csv` from the PM Data API; no longer needed now that trades are in the accounting ledger.

---

### Fix — Test infrastructure (`tests/conftest.py`, `tests/test_opening_neutral.py`)

`_isolate_trades_csv` autouse fixture in `conftest.py` referenced `risk.TRADES_CSV` which had
been removed from `risk.py`; fixture updated with `hasattr` guard.  Two test bodies in
`test_opening_neutral.py` that directly referenced `risk.TRADES_CSV` as a temp-dir redirect
had those references removed (neither test asserts CSV content).  Restores **58/58 tests**
passing in `test_opening_neutral.py` + `test_model_agent.py`.

---

## [2026-05-05] - ON-07 winner confirmation gate; ML training pipeline; ON→momentum fill logging; feature_builder vol_regime_high fix; PMClobWS dashboard fix

### Feature — ON-07 winner confirmation gate (`strategies/OpeningNeutral/scanner.py`, `config.py`, `config_overrides.json`, `api_server.py`, `webapp/src/pages/Settings.tsx`, `webapp/src/api/client.ts`)

Addresses the root cause of 32% wrong-loser identification in Opening Neutral (19/59 pairs,
−$37.12 total loss): both YES and NO bids oscillate near $0.50 for the first 60–90 seconds
after entry and a transient dip on one leg fires a premature loser exit before the market
has picked a direction.

**Gate logic** (inserted between min-hold gate and oracle-delta gate in `_on_price_event`):
Before declaring a loser, the *other* leg's live WS best bid must be ≥
`OPENING_NEUTRAL_WINNER_CONFIRM_FLOOR`.  If the winner hasn't diverged yet, the exit is
suppressed and re-evaluated on the next tick.

**New config key:**
```
OPENING_NEUTRAL_WINNER_CONFIRM_FLOOR: float = 0.0   # disabled by default
# config_overrides.json live value: 0.60
```

Wired end-to-end: `config.py` default → `config_overrides.json` live override (0.60) →
`api_server.py` `_SETTINGS_MAP` + `ConfigPatch` → webapp `client.ts` interface field →
Settings page `FloatInput` "Winner Confirm Floor" control (ON section, after Min Hold).

---

### Feature — ML model training pipeline (`api_server.py`, `webapp/src/pages/Dashboard.tsx`, `webapp/src/api/client.ts`)

**Auto-train on startup** (`api_server.py`)
New `@app.on_event("startup")` hook checks whether `model_a_v0.pkl` / `model_b_v0.pkl`
exist on disk.  If either is missing, automatically triggers the training pipeline
(`feature_builder.py` → `train_model.py`) as a background asyncio task so models are
rebuilt without manual intervention after a clean deploy.

**`/model/train` endpoints** (`api_server.py`)
- `POST /model/train` — triggers feature build + model train subprocess; 409 if already running.
- `GET /model/train/status` — returns `{running, last_started_ts, last_finished_ts, last_exit_code, last_log_lines, model_b_exists, model_a_exists}`.

**`ModelTrainingCard` webapp widget** (`Dashboard.tsx`)
New card on the Dashboard showing:
- Model A and Model B presence chips (green ✅ / grey ⭕), linked to their SHAP HTML reports.
- Last run timestamp and exit code.
- "Train" button (POST `/model/train`); disabled while training is running with spinner label.
- PMClobWS pipeline health detail fixed: no longer shows "heartbeat_age=None" when no
  maker orders are active — now shows "WS connected · no maker orders (heartbeat idle — normal)".

---

### Fix — ON→momentum handover logs to `momentum_fills.csv` (`strategies/OpeningNeutral/scanner.py`)

ON-promoted winner positions were never logged to `momentum_fills.csv`, leaving all `mom_*`
ML features as −999 sentinel for the 75 promoted trades in the training set (89% null
coverage).  Model A would train on garbage even when 300 rows are reached.

**Fix:** `_on_exit_fill()` now appends a `row_type="on_promoted"` row to `momentum_fills.csv`
immediately after promotion, capturing: `market_id`, `side`, `entry_price`, `entry_cost_usd`,
`tte_seconds`, `funding_rate`, `yes_depth_share` (from the `_csv_row` captured at ON entry),
and a live CLOB snapshot of the winner book at handover time.  Fields unavailable at handover
time (z-score, Kelly, vol_regime) are written as `None` — handled by the −999 sentinel fill
in `_prepare_features`.

Imports `MOMENTUM_FILLS_CSV`, `MOMENTUM_FILLS_HEADER`, `_ensure_momentum_fills_csv` from
`strategies.Momentum.scanner` (no circular import — ON scanner already imports Momentum
utilities).

---

### Fix — `momentum_fills.csv` CLOB + Deribit IV columns (`strategies/Momentum/scanner.py`)

Added five columns to `MOMENTUM_FILLS_HEADER` and the fill-row writer:
`clob_yes_best_bid`, `clob_yes_best_ask`, `clob_yes_spread`, `clob_yes_bid_depth_5`,
`deribit_iv`.  Captures the live WS order-book snapshot at the moment of fill confirmation.
`deribit_iv` is only populated when `signal.vol_source == "deribit_atm"`.

---

### Fix — `vol_regime_high` missing from training parquet (`analysis/feature_builder.py`)

`vol_regime_high` was listed as a Model A feature in `train_model.py` but was never written
to `training_data.parquet` — `_prepare_features` fell back to all-NaN silently.

**Fix:** Added `vol_regime_high` to `_make_empty_schema()` column list.  Added a post-merge
derivation step (step 5, applied to the **full df** including existing rows so old parquet
rows are backfilled): `HIGH → 1.0`, `LOW/UNKNOWN → 0.0`, absent/None string → NaN.
Uses `np.where` on the `.str.upper()` of `mom_vol_regime`.

---



### Feature — ML Adaptive Signal Engine, Phase 0–2 (`models/`, `main.py`, `api_server.py`, `config.py`, `requirements.txt`)

Implements the first three phases of the ML Adaptive Signal Engine.  All ML flags default
`False`; the bot behaves identically to the prior release unless they are explicitly enabled.

**Phase 0 — CLOB Feature Buffer (ML-01)** (`models/clob_feature_buffer.py`)  
Registers an `on_price_change` callback on `PMClient` after the WS shards start.  Maintains
a per-token in-memory rolling deque of CLOB book snapshots (`CLOB_BUFFER_MAXLEN = 600` ticks
≈ 10 min at 1 tick/s).  No disk writes.  Enabled via `CLOB_FEATURE_BUFFER_ENABLED = True`.

**Feature snapshot builder** (`models/feature_snapshot.py`)  
Assembles the real-time feature vector at signal time from CLOB buffer, oracle, funding,
and vol inputs.  Used by both the shadow logger and future live model gate.

**Phase 2 — ModelAgent shadow logger (ML-04)** (`models/model_agent.py`, `main.py`)  
Loads pre-trained Model A (entry) and Model B (exit) from pickles; intercepts every entry
and exit decision; logs rule/model agreement to an in-memory deque without affecting any
live trade logic.  `model_agent.run()` coroutine is started as a named asyncio task.

**New config keys:**
```
CLOB_FEATURE_BUFFER_ENABLED: bool = False   # ML-01
CLOB_BUFFER_MAXLEN: int = 600
MODEL_AGENT_ENABLED: bool = False           # ML-04
MODEL_A_PATH: str = ".../analysis/model_a_v0.pkl"
MODEL_B_PATH: str = ".../analysis/model_b_v0.pkl"
MODEL_A_SCORE_THRESHOLD: float = 0.5
MODEL_B_SCORE_THRESHOLD: float = 0.5
```

**New API endpoints** (`api_server.py`):
- `GET /model/status` — ModelAgent runtime status: enabled flag, last decision ts, agreement rate.
- `GET /model/shadow_log?limit=50&decision_type=all` — last N shadow log rows.
- `GET /reports/{filename}.html` — serves static HTML reports from `analysis/reports/` (path-traversal guarded).

`BotState.model_agent_ref` field wired in `api_server.py`; set at startup by `main.py`.

**New dependencies** (`requirements.txt`): `pyarrow`, `xgboost`, `shap`, `scikit-learn`, `matplotlib`.

**New tests** (5 files):
- `test_clob_feature_buffer.py` — buffer subscribe/tick/evict/unsubscribe.
- `test_feature_builder.py` — feature snapshot field coverage and types.
- `test_model_agent.py` — shadow log accumulation, agreement rate, get_status().
- `test_shadow_evaluator.py` — outcome backfill and agreement scoring.
- `test_train_model.py` — smoke tests: model A/B train, SHAP report generation.

**New webapp page** (`webapp/src/pages/ModelAgent.tsx`, `webapp/src/App.tsx`):  
`/model` route added to nav. Displays ModelAgent status card and scrollable shadow log table.
Report links (Model A/B SHAP HTML) added to nav sidebar.

---

### Feature — Delta SL grace window + token-price veto (`monitor.py`, `config.py`, `webapp/src/pages/Settings.tsx`)

Two new stop-loss quality guards that reduce false-positive delta SL exits.

**Grace window** — `MOMENTUM_DELTA_SL_GRACE_SECS: int = 60`  
The delta SL is suppressed while _both_ the position age is below this threshold _and_
TTE is still above the bucket minimum entry window.  Whichever condition clears first
re-arms the SL.  Fixes a blind-spot regression for ON-promoted winners (15m bucket,
promotion at TTE=520s) where the previous TTE-only gate left the SL disabled for up
to 340 s after promotion.  The grace cap limits the blind spot to 60 s regardless of
TTE.  Also added to the Settings UI as "Delta SL Grace Window (s)".

**Token-price veto** — `MOMENTUM_DELTA_SL_TOKEN_VETO_FLOOR: float = 0.0` (disabled by default)  
If the held token's CLOB mid is still above this floor when the oracle delta retreats below
the SL threshold, the SL is suppressed.  Rationale: when the CLOB crowd has not repriced
the token the oracle move is likely transient noise.  Set via override (recommended: 0.55).
Also added to the Settings UI as "Delta SL Token Price Veto Floor".

---

### Feature — GTD hedge management loop (`monitor.py`, `config.py`)

`PositionMonitor._manage_open_hedge_orders()` runs every `_check_all_positions` cycle,
independently of the position loop.  Two sub-features:

1. **Near-expiry cancel** — if `TTE ≤ MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS` (default 5 s) and
   the held token's CLOB mid is above 0.50 (winning), the hedge is cancelled to prevent an
   adverse fill at settlement.  Calls `risk.finalize_hedge(..., HedgeStatus.CANCELLED)`.

2. **Gap-closing reprice** — tracks `HedgeOrder.last_clob_ask` sweep-over-sweep.  When the
   opposite token's ask falls, cancels the existing order and reposts at `best_bid + $0.01`.
   Respects `price_cap` (set at placement) and a `MOMENTUM_HEDGE_MIN_RETAIN_USD = 0.50`
   PnL floor.  Calls `risk.replace_hedge_order(old_id, new_id, new_price, new_size)`.

**Cancel on take-profit** — `_exit_position` now fires `asyncio.create_task(pm.cancel_order(hedge_order_id))`
when the exit reason is `PROFIT_TARGET` or `MOMENTUM_TAKE_PROFIT`.  SL/near-expiry exits
deliberately keep the hedge alive as the recovery leg.

**New config keys:**
```
MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: int = 5
MOMENTUM_HEDGE_MIN_RETAIN_USD: float = 0.50
MOMENTUM_HEDGE_CLOB_LOG_ENABLED: bool = True
```

**Coverage:** 17 new tests across `TestHedgeGapClosingReprice`, `TestHedgeNearExpiryCancel`,
`TestHedgeRepriceSizing`, and `TestGtdHedgeCancelOnClose` in `test_monitor.py`.

---

### Feature — ON oracle delta gate (ON-06) (`strategies/OpeningNeutral/scanner.py`, `config.py`)

When `OPENING_NEUTRAL_ORACLE_DELTA_GATE_ENABLED = True`, the bid-monitor loser exit is
only allowed to fire when the oracle spot confirms the leg is losing (`delta ≤ 0`).
Suppresses false-positive exits caused by CLOB book-drain at settlement (market makers
withdraw bids on both legs simultaneously in the final seconds, collapsing `best_bid` to
0.29–0.38 regardless of which side will win).

Fallback policy when oracle is unavailable:
- `allow_exit` (default): fire the loser exit as before — safe against perpetually-stale oracle.
- `suppress`: hold — risky if oracle is never available.

**New config keys:**
```
OPENING_NEUTRAL_ORACLE_DELTA_GATE_ENABLED: bool = True
OPENING_NEUTRAL_ORACLE_DELTA_GATE_FALLBACK: str = "allow_exit"
```

---

### Feature — Pipeline health dashboard card (`webapp/src/pages/Dashboard.tsx`, `webapp/src/api/client.ts`)

New `PipelineStatusCard` component on the Dashboard page polls `/health/pipelines` every
10 s and renders a status table with colour-coded LIVE/STALE/ERROR/NOT_STARTED indicators
and age labels for each data pipeline (RTDS, Chainlink, HL WS, PM WS, ModelAgent, etc.).

New `usePipelineHealth()` hook and `PipelineStatus` / `PipelineHealthData` types added to
`client.ts`.

---

### Feature — Kelly size signal-strength scaling (`strategies/Momentum/scanner.py`)

`_compute_kelly_size_usd` now scales the edge premium by signal strength:
`strength = delta_pct / threshold_pct`.  At `strength = 1.0` (barely at gate) the alpha
premium is zero; at `strength = 2.0` (twice the threshold) the full premium is claimed.
Corrects flat-premium overconfidence observed in the 0.90–0.95 kelly_win_prob band
(47.6% actual win rate vs 92.5% expected).

---

### Feature — PositionMonitor `on_closed_full_callback` (`monitor.py`, `main.py`)

`PositionMonitor` accepts a new `on_closed_full_callback(market_id, side, exit_price, strategy)`
parameter.  `main.py` passes `_on_closed_full` which calls
`opening_neutral_scanner.notify_winner_closed(...)` to backfill `winner_exit_price` in
`on_fills.csv` when a promoted momentum winner closes.

---

### Fix — Test suite tech-debt resolved (`tests/conftest.py`, `tests/test_monitor.py`, `tests/test_opening_neutral.py`, `tests/test_risk.py`, `tests/test_integration.py`)

24 pre-existing test failures fixed; suite now passes 1,298 tests with 0 failures.

- **Event loop poisoning** (`conftest.py`): `_reset_event_loop` autouse fixture installs a
  fresh `asyncio` event loop before each test.  `asyncio.run()` in `test_model_agent.py`
  closed the loop; Python 3.10 `get_event_loop()` then raised `RuntimeError` for 193+ tests.

- **Stale CSV tests** (`test_risk.py`, `test_integration.py`): `record_hedge_fill` and
  `close_position` now route through the accounting ledger, not `TRADES_CSV`.  Three tests
  rewritten to assert on `engine.realized_pnl` and `get_ledger()` state; one dead test removed.

- **SL config override leaking into tests** (`conftest.py`): `_reset_production_limits` now
  resets `MOMENTUM_DELTA_SL_GRACE_SECS`, `MOMENTUM_DELTA_SL_TOKEN_VETO_FLOOR`,
  `MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS`, `MOMENTUM_HEDGE_MIN_RETAIN_USD`, and
  `MOMENTUM_HEDGE_CLOB_LOG_ENABLED` to code defaults so `config_overrides.json` production
  values don't break test assertions.

- **`state.data_quality` pollution** (`conftest.py`): `api_server.state.data_quality = {}`
  added to the autouse reset.  `test_main_wiring.py` stores `MagicMock` values in
  `data_quality` which caused `health()` to raise `TypeError` in `test_startup_smoke.py`.

- **Removed `TestPendingResolutionHedgeBackfill`** (`test_monitor.py`): called
  `monitor._record_pending_resolution_hedge()` which no longer exists.

- **OpeningNeutral mock completeness** (`test_opening_neutral.py`): added
  `pm.get_depth_share = MagicMock(return_value=None)` and
  `pm.fetch_price_to_beat = AsyncMock(return_value=None)` to `_make_scanner()` and the
  local pm in `test_on_exit_fill_idempotent`.

---



### Fix — Delta SL dip/reach misclassification for bucket-market NO positions (`monitor.py`, `tests/test_monitor.py`)

`should_exit()` used `pos.spot_price > pos.strike` to select the dip-market delta formula
(where NO wins by spot staying above strike).  For bucket markets the ON scanner sets
`pos.spot_price` from the live oracle at registration time; a tiny timing window at the
bucket open can place spot fractionally above the strike, silently activating the dip branch
for a UP/DOWN position where NO always means "ends below strike".

Fix: the dip branch now requires `pos.market_type not in _MARKET_TYPE_DURATION_SECS`
(i.e. only fires for non-bucket milestone markets).  Bucket markets always use the reach
formula regardless of `pos.spot_price`.

Two regression tests added:
- `test_bucket_no_uses_reach_formula_even_when_spot_price_above_strike` — reproduces the
  HYPE 9:05AM loss where `pos.spot_price=41.09368 > pos.strike=41.08321` inverted delta SL.
- `test_milestone_no_dip_branch_preserved_when_spot_price_above_strike` — confirms milestone
  dip markets are unaffected.

### Fix — Upfrac rolling-window premature exit (`monitor.py`, `tests/test_monitor.py`)

`_upfrac_window_ts.get(pos.market_id, 0)` returned epoch zero for any new position,
making `now - 0 >= WINDOW_SECONDS` always True on the very first monitor scan after entry.
With `WINDOWS=2` this halved the effective minimum dwell: window 1 fired at entry, window 2
fired `WINDOW_SECONDS` later — instead of the intended `2 × WINDOW_SECONDS`.

Observed impact: SOL and XRP UP positions exited at `upfrac=0.29` and `0.375` after 5.5 s
and 15.3 s respectively, while both markets subsequently resolved UP.

Fix: on the first visit for a position, seed the clock and skip evaluation.  The first real
window evaluation occurs `WINDOW_SECONDS` after entry, giving a true `WINDOWS × WINDOW_SECONDS`
minimum dwell.  Also clears `_upfrac_window_ts` on position close so re-entries into the
same market start with a fresh clock.

Four existing `TestUpfracEwmaExit` tests updated to reflect the new seed-and-skip behavior.

### Refactor — M-13: EWMA → rolling-window upfrac (`market_data/oracle_tick_tracker.py`, `monitor.py`, `config.py`)

Replaced per-tick EWMA up-fraction with a true rolling-window fraction (`get_upfrac_rolling()`),
matching the plan's "raw fraction over last N ticks" intent.  The new method counts up-ticks
in the last `window_secs` of oracle history and returns `up_count / total_ticks`.  This gives
a stable, interpretable signal compared with the EWMA which was sensitive to tick rate and
lookback length.  `get_upfrac_ewma()` retained on `OracleTickTracker` but no longer called
by the monitor.

New config keys:
- `MOMENTUM_UPFRAC_WINDOW_SECONDS` (5) — duration of each measurement window
- `MOMENTUM_UPFRAC_SUPPRESS_UNTIL_ENTRY_WINDOW` (True) — suppress upfrac while TTE is above
  the entry-window threshold (prevents stale pre-entry signal from firing on ON-promoted positions)
- `MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE` (0.90) — suppress all taker exits (delta SL, prob SL,
  upfrac) when CLOB mid ≥ 0.90; position held to settlement at high-confidence levels

### Feature — ON sell-trigger improvements: asymmetric triggers and loser-confidence tighten (`strategies/OpeningNeutral/scanner.py`, `config.py`)

Two new ON-04/ON-05 refinements to per-pair exit trigger computation:

**ON-04 — Asymmetric triggers** (`OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED`, `OPENING_NEUTRAL_WINNER_SELL_BUFFER=0.03`):
The predicted winner's loser-exit trigger is lowered by `WINNER_SELL_BUFFER` so it is
harder to accidentally sell the winning leg, while the predicted loser's trigger is kept
at the global threshold.  Prediction is based on funding direction (positive funding → YES
favored; negative → NO favored) when the gate is enabled.

**ON-05 — Loser confidence tighten** (`OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED`, `OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN=0.02`):
When the loser-confidence score exceeds a threshold, the loser leg's exit trigger is raised
by `LOSER_CONFIDENCE_TIGHTEN`, making it more sensitive and reducing exit latency on
high-confidence loser signals.  Confidence score now back-filled into `on_fills.csv`.

New ON funding gate (`OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD=0.00001`): entry blocked when
|funding| exceeds threshold and direction disagrees with intended position.

**`OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM`** (True) — controls whether the ON winner is
promoted to a full momentum position (with delta-SL, prob-SL, upfrac gates) after the loser
exits.  Previously wired but not config-gated.

### Feature — `accounting.py`: `exit_reason` column; paper-mode reconcile loop (`accounting.py`, `risk.py`, `main.py`)

`exit_reason` (e.g. `momentum_stop_loss`, `prob_sl`, `upfrac_exit`, `loser_exit`) added as
a dedicated column in the accounting ledger CSV alongside the coarser `exit_type`
(`TAKER` / `SL` / `RESOLVED`).  Propagated from `ExitReason` through `RiskEngine.close_position()`
→ `_Ledger.record_fill()` → `AccountingPosition.exit_reason`.

Paper-mode reconcile loop (`get_ledger().reconcile_loop(pm)`) launched as a named asyncio
task in `main.py`.  Ensures `PENDING_RESOLVE` paper positions advance through PM CLOB
winner-flag resolution even when no on-chain events arrive.  Also wires `funding_cache`
into `OpeningNeutralScanner` constructor.

### Fix — `hl_client.py`: funding update filtered to `HL_PERP_COINS`

`HLClient` `webData2` handler was updating the funding cache for all coins in the universe
snapshot, including coins not tracked by the bot.  Now skips any coin not in
`config.HL_PERP_COINS`, preventing stale or irrelevant funding data from polluting the cache.

---

## [2026-05-04] - Opening Neutral wiring; Momentum diagnostics; webapp Settings; HMR fix

### Feature — Opening Neutral ON-02: fills CSV (`strategies/OpeningNeutral/scanner.py`)

`on_fills.csv` is now written on every completed pair.  Schema (v1) captures entry
context (yes/no entry prices, combined cost, spreads, funding rate, YES depth share,
loser confidence score), sell prices placed, and outcome fields (loser leg, loser fill
price, loser fill time, winner exit price).  Auto-creates file with header on first run;
backs up and migrates on schema change.  `_update_on_fills_winner_exit()` backfills
winner exit price when the winner resolves.

### Feature — Opening Neutral new config keys (`config.py`, `api_server.py`, `config_overrides.json`)

Nine new ON config fields wired end-to-end (config defaults → overrides → runtime PATCH):
- `OPENING_NEUTRAL_LOSER_EXIT_TRIGGER` (0.38) — bid-monitor trigger with buffer above exit price
- `OPENING_NEUTRAL_MIN_HOLD_SECS` (30.0) — minimum hold before loser bid monitor can fire
- `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED` / `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD` (0.15) — cold-book spread gate
- `OPENING_NEUTRAL_LOSER_EXIT_PRICE` (0.35) — market-sell target price for loser leg
- `OPENING_NEUTRAL_SIZE_USD` (5.0) — dollars per leg
- `OPENING_NEUTRAL_ORDER_TYPE` ("market") — FAK entry order type
- `OPENING_NEUTRAL_ONE_LEG_FALLBACK` ("keep_as_momentum") — one-leg fill disposition
- `OPENING_NEUTRAL_MAX_CONCURRENT` (5) — concurrent pair ceiling

All nine keys added to `_MUTABLE_CONFIG`, `ConfigPatch` model, and `GET /config` response in `api_server.py`.

### Feature — Webapp Settings: Opening Neutral card (`webapp/src/pages/Settings.tsx`, `webapp/src/api/client.ts`)

New dedicated Opening Neutral settings card with 11 controls:
- Dry Run toggle
- Take Profit toggle + conditional TP Target % float input
- Cold Book Spread Gate toggle + conditional Max Individual Spread float input
- Loser Exit Trigger ($), Min Hold (s), Loser Exit Price ($)
- Size per Leg ($), Max Concurrent Pairs (int)
- Entry Order Type select (market / limit)
- One-Leg Fallback select (keep_as_momentum / exit_immediately)

New `SelectInput` primitive added alongside existing `FloatInput` for dropdown fields.
`ConfigData` TypeScript interface extended with all nine new ON keys.

### Fix — Vite Fast Refresh HMR warnings (`webapp/src/pages/`)

`Pending.tsx` and `Trades.tsx` were exporting non-component helpers alongside the default
component export, breaking Vite Fast Refresh.  Fixed by extracting helpers into dedicated
util modules:
- `pendingUtils.ts` — `timeSince()`, `unrealizedPnl()`
- `tradesUtils.ts` — `fmtUsd()`, `fmtPrice()`, `fmtContracts()`, `netPnl()`, `grossPnl()`,
  `pnlColor()`, `buildGroups()`, `LedgerGroup` interface

Both page files now import from utils; page files export only React components.
Test files (`Pending.test.tsx`, `Trades.test.tsx`) updated to import from utils.

### Fix — Momentum scanner out-of-band diagnostics (`strategies/Momentum/scanner.py`)

`out_of_band` diag entries now expose `token_price` and `side` for whichever token is
closest to the entry band, plus a best-effort `spot` field from cache.  Previously the
diag row showed `"—"` for price and side, making the webapp diagnostics table uninformative
for the majority of skipped markets.

### Config — `MOMENTUM_BOOK_MAX_AGE_SECS` default corrected (`config.py`, `config_overrides.json`)

Default lowered from 60 s to 30 s in `config.py` to match the override already in
`config_overrides.json`.  The previous 60 s default was inherited from pre-M-series code;
10 s was briefly set as an override with no data justification and eliminated 54% of
candidates as stale.  30 s balances freshness with the WS update cadence of illiquid markets.

---

## [2026-05-03] - M-series momentum upgrades; Opening Neutral promote flag; startup smoke tests; stale code removal

### Feature — FundingRateCache (`market_data/funding_rate_cache.py`, `hl_client.py`)

New push-fed funding rate module updated by `HLClient`'s `webData2` WebSocket handler.
No polling — staleness is measured from last push timestamp.  History buffer (last 10
readings per coin) retained for future trend analysis; not used as a gate signal.
`FUNDING_STALE_THRESHOLD_S` (default 120 s) guards against stale reads.
`FundingRateCache.get(coin)` returns `None` when data is absent or stale.

### Feature — OracleTickTracker (`market_data/oracle_tick_tracker.py`, `monitor.py`)

New per-coin oracle analytics module registered against `SpotOracle` callbacks.
Tracks three signals per coin:
- **EWMA up-fraction** — smoothed fraction of oracle ticks that were up-moves; used for
  `MOMENTUM_UPFRAC_EXIT` (exit when EWMA drops below threshold for N consecutive windows).
- **TWAP deviation** — oracle TWAP vs current price in bps; used for M-14 vol-regime gating.
- **Volatility regime** — classifies each coin as `HIGH`, `LOW`, or `UNKNOWN` based on
  rolling tick magnitudes.

### Feature — Momentum M-series signal improvements (`strategies/Momentum/scanner.py`, `config.py`)

`MomentumScanner` now accepts optional `funding_cache` and `oracle_tracker` constructor
args wired through `main.py`.  New fill CSV columns capture entry context for post-trade
analysis:
- `funding_rate` — HL funding rate at entry
- `yes_depth_share` — YES bid depth fraction (relative depth gauge)
- `hour_utc` — UTC hour of entry (for intraday regime splits)
- `effective_z` — z-score after all multipliers (position sizing trace)
- `funding_gate_applied` — whether the funding gate was active at entry
- `twap_dev_bps` — oracle TWAP deviation in bps at scan time (M-14)
- `vol_regime` — volatility regime at scan time: `HIGH` / `LOW` / `UNKNOWN` (M-14)

New `ExitReason.MOMENTUM_UPFRAC_EXIT` — fires when EWMA up-fraction drops below the
threshold for N consecutive evaluation windows (configurable via `MOMENTUM_UPFRAC_*` keys).

Removed stale code:
- `_signal_first_valid` persistence dict and disk I/O (Kelly persistence z-boost removed)
- `MOMENTUM_HEDGE_ENABLED` branch in `fill_simulator._loop()` and entire `_sweep_hedges()` method
- All `MOMENTUM_HEDGE_*`, `MOMENTUM_WIN_RATE_GATE_*`, `MOMENTUM_KELLY_PERSISTENCE_*` references
  from `api_server.py` mutable config map, `ConfigPatch` model, and `GET /config` response

### Feature — New strategy docs (`OpeningNeutralStrategy.md`, `OpenStrategy.md`, `strategy_update.md`)

- **`OpeningNeutralStrategy.md`** — concise authoritative spec for the Opening Neutral strategy,
  including the updated loser-exit (bid-monitoring FAK) and winner-promotion flow.
- **`OpenStrategy.md`** — spec for a new Open Market Entry strategy: single directional
  position at T=0 when a 5-minute bucket opens, using pre-open oracle + perp signals.
- **`strategy_update.md`** — data-driven recommendations from a 681-window analysis
  (`strategy_update.md` Part 0–2) covering Opening Neutral, Momentum, and Open strategies.

### Feature — Opening Neutral: `OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM` flag (`scanner.py`, `config.py`)

New config key `OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM` (default `False`).  When `True` the
existing winner-promotion path fires: the winner leg is re-labelled as a momentum position
and handed to `PositionMonitor` for delta-SL / TP management.  When `False` the winner
simply holds to resolution under the `opening_neutral` strategy label — no delta-SL is
armed, the position resolves for full payout.

Scanner now sets `winner_pos.strategy = "momentum"` directly on the in-memory `Position`
object immediately before calling `_risk.promote_position_strategy(...)`, ensuring the
in-memory object is updated even when the risk engine is mocked in tests.

### Fix — `main.py`: `logger` → `log` NameError in Phase 1 pipeline startup

Four `logger.info/error` calls in the Phase 1 pipeline startup block used the wrong name.
The module-level logger is `log = get_bot_logger(__name__)` throughout the codebase.
Fixed: all four occurrences changed to `log.info` / `log.error`.

### Fix — API `/pnl` and `/performance` migrated to `_load_acct_ledger_trades()` (`api_server.py`)

Both endpoints previously called `_load_trades_csv()` (the old `RiskEngine` CSV path).
Migrated to `_load_acct_ledger_trades()` (the new `accounting.py` ledger) to reflect
current accounting data.

### Tests — startup smoke suite (`tests/test_startup_smoke.py`, 16 tests)

New module-level smoke test suite covering:
- **`TestConfigAudit`** — scans all production `.py` files for `config.UPPERCASE_ATTR`
  references, asserts each attribute exists in `config.py`.  Catches stale config refs
  at CI time without running the full bot.
- **`TestApiServerSmoke`** — `GET /health`, `GET /config`, `GET /pnl` via FastAPI `TestClient`.
- **`TestFillSimulatorSmoke`** — `_sweep()` and `_loop()` with mocked dependencies.
- **`TestMomentumScannerSmoke`** — `_scan_once()`, instantiation, `start()`.
- **`TestMispricingScannerSmoke`** — instantiation, `start()`.
- **`TestOpeningNeutralScannerSmoke`** — `_refresh_pending_markets()`, instantiation, `start()`.

All 16 tests pass.

### Tests — `test_opening_neutral.py` regression fixes (12 failures → all 27 passing)

Six root-cause fixes:
1. **Config rename** — 6 `patch.object` calls updated from `OPENING_NEUTRAL_ENTRY_WINDOW_SECS`
   to `OPENING_NEUTRAL_MARKET_WINDOW_SECS`.
2. **Promote-to-momentum gate** — 4 tests that exercise the `_on_exit_fill` promotion path
   now patch `OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM=True` to unlock that code path.
3. **Exit mechanism rewrite** — `test_register_pair_places_exit_sells_immediately` rewritten:
   no `place_limit` expected (bid-monitoring replaces resting SELLs); asserts
   `_token_to_pair[yes_token_id]` and `_token_to_pair[no_token_id]` are populated, and
   `pair["yes_exit_order_id"] == ""`.
4. **Accounting migration** — `test_on_exit_fill_idempotent` no longer checks `TRADES_CSV`
   row count (accounting moved to `acct_ledger.csv`); replaced with `is_closed is False` guard.
5. **E2E test** — `pm.get_markets.return_value = {}` prevents a `TypeError` from `await`-ing
   a plain `MagicMock` in `fetch_price_to_beat` when promotion is enabled.
6. **Scan entry timing** — 3 scan tests updated from `tte_seconds=3500` to `tte_seconds=3605`
   (5 s pre-open) matching the new `elapsed ∈ [-11, 0]` gate in `_evaluate_entry`.

### Tests — stale hedge tests deleted (`tests/test_hedge_sizing.py`, `tests/test_hedge_sweep.py`)

Both files targeted the defunct `MOMENTUM_HEDGE_*` code path (hedge GTD orders, sweep
logic, `fill_simulator._sweep_hedges()`).  Deleted as the code they covered no longer exists.

---

## [2026-05-01] - Accounting ledger; OpeningNeutral TP + fill-price confirmation + delta-SL fix; test overhaul

### Feature — Standalone accounting ledger (`accounting.py`, `risk.py`, `api_server.py`)

New module `accounting.py` replaces the old `RiskEngine`/`trades.csv` pipeline for tracking
position outcomes.  It maintains a `_Ledger` singleton backed by three files in `data/`:
`acct_fills.jsonl` (raw fill journal), `acct_positions.json` (live position state), and
`acct_ledger.csv` (finalized P&L records).

**API:**
- `on_entry_fill(...)` — opens a new position row, VWAP-accumulates on add-ons.
- `on_exit_fill(...)` — records partial/full exit, advances to `CLOSING` or `PENDING_RESOLVE`.
- `on_resolved(condition_id, resolved_yes_price)` — settles all positions for the market.
  YES/UP wins on `resolved_yes_price ≥ 0.99`; NO/DOWN wins on `resolved_yes_price ≤ 0.01`
  (preamble-compliant; `resolved_yes_price` always expresses the YES token price).
- `on_pair_promoted(token_id, new_fill_type)` — relabels loser/winner on OpeningNeutral promotion.
- `add_fees(...)` — accumulates fees/rebates without an exit fill.
- `get_ledger()` — singleton accessor for use across modules.

**Hooks in `risk.py`:** `open_position()` now calls `get_ledger().on_entry_fill(...)` as a
fire-and-forget hook.  Old `RiskEngine.mark_resolved()`, `mark_spot_exit()`, and
`mark_hedge_spot_exit()` now return 0 immediately (stub) — accounting is owned by
`accounting.py`.  `Position` gains `take_profit_price: float = 0.0` for per-position TP.
Hedge `spread_id` now inherits from the parent momentum position.

**Bug fix:** `on_resolved()` was writing duplicate CSV rows on repeated calls.  The
`_write_ledger_record` loop is now gated on `if changed:` so it only fires when
positions actually transition.

**REST endpoints in `api_server.py`:**
- `GET /acct/ledger` — paginated finalized ledger rows from `acct_ledger.csv`.
  Filterable by `strategy`, `underlying`, `status`, `fill_type`.
- `GET /acct/positions` — all positions from `acct_positions.json` (optional `status` filter).
- `GET /acct/pending` — live `CLOSING` and `PENDING_RESOLVE` positions.

### Feature — OpeningNeutral: take-profit for promoted winner (`scanner.py`, `monitor.py`, `config.py`, `api_server.py`)

After the loser leg exits, the scanner arms a per-position TP price on the promoted winner:

```
tp_price = combined_cost × (1 + OPENING_NEUTRAL_TP_PROFIT_PCT) − loser_exit_price
```

Capped at 0.99.  Stored in `pos.take_profit_price`; `monitor.should_exit()` uses this
threshold in place of the global `MOMENTUM_TAKE_PROFIT` when > 0.

**Config keys added:** `OPENING_NEUTRAL_TP_ENABLED = True`, `OPENING_NEUTRAL_TP_PROFIT_PCT = 0.10`.
Both are hot-patachable via `/config` PATCH.

### Fix — OpeningNeutral: loser exit fill price confirmation (`scanner.py`)

`_execute_loser_exit` previously called `get_order_fill_rest()` to confirm the fill price —
but that method's path-3 fallback returns the floor price `0.01` (the FAK placeholder),
not the real fill.  Now replicates the monitor's WS pattern: registers a fill future,
awaits with a 10 s timeout, falls back to REST only when the WS event is missing or
returns `price=0`.  REST fallback also rejects `price ≤ 0.01`.

Also adds a sanity guard: a fill price > `OPENING_NEUTRAL_LOSER_EXIT_PRICE + 0.30` is
treated as a mis-routed WS event (resolution/redemption event with order_id collision)
and discarded rather than used as the exit price.

### Fix — OpeningNeutral: WS entry path restored for pre-market window (`scanner.py`)

The WS entry path was unconditionally blocked (`if not _timer_fired: return`).  It is
now restored for the pre-market window: both the timer path and WS tick path evaluate
entry when `elapsed ∈ [elapsed_min, 0.0]`.  WS ticks re-evaluate on every book change
(debounced at `_ENTRY_EVAL_DEBOUNCE_SECS`) so a brief spread improvement is caught
immediately rather than relying on a single REST snapshot at T-10 s.  Warning log is
now only emitted on the timer path to avoid spam from out-of-window WS ticks.

### Fix — OpeningNeutral: spread_id propagated to position at entry (`scanner.py`)

`open_position()` call now passes `spread_id=pair_id` so accounting and monitor can
trace the position back to its pair without relying solely on `neutral_pair_id`.

### Fix — OpeningNeutral: strike set on promoted winner for delta-SL (`scanner.py`)

After promotion, the scanner now fetches `priceToBeat` from the Gamma API and writes it
to `winner_pos.strike`.  Also pre-populates `momentum._market_open_spot[market_id]`.
If the fetch fails a warning is logged and delta-SL remains inactive (safe default).

### Fix — monitor: delta-SL suppressed outside momentum entry window (`monitor.py`)

OpeningNeutral-promoted positions are handed over at ~T+15 s of a fresh bucket — spot
barely above/below strike → delta is tiny → delta-SL fired immediately on every
promotion.  `should_exit()` now gates delta-SL: if `tte_seconds > MOMENTUM_MIN_TTE_SECONDS`
(i.e., outside the entry window) the delta check is skipped.  Prob-SL remains active
throughout.

### Config change — `MOMENTUM_PROB_SL_MIN_TTE_SECS` reduced to 30 s (`config.py`)

Was 300 s (5 minutes).  Reduced to 30 s — prob-SL is now suppressed only in the final
30 seconds of a market rather than the final 5 minutes.

### Webapp — Accounting UI (`webapp/src/`)

New pages and hooks to display accounting data:

- **`Trades.tsx`** (rewrite) — finalized ledger grouped by pair; per-group collapsible rows
  showing gross P&L, net P&L, fees, fill type (MAIN/HEDGE/MOMENTUM), WIN/LOSS badges.
  Outcome filter, text search, and pagination.
- **`Pending.tsx`** (new) — live `CLOSING` and `PENDING_RESOLVE` positions with urgency
  colouring, unrealized P&L, market type badge, and PM confirmation link.
- **`Positions.tsx`** — aggregate summary bar: open count, today's net P&L, total net P&L.
- **`App.tsx`** — Pending route and nav item added.
- **`client.ts`** — `AcctLedgerRow`, `AcctPosition` types; `useAcctLedger`,
  `useAcctPositions`, `useAcctPending` hooks wired to the new `/acct/*` endpoints.
- **`Settings.tsx`** — `opening_neutral_tp_enabled` and `opening_neutral_tp_profit_pct`
  controls added.

### Tests — full overhaul (`tests/test_accounting_e2e.py`, `webapp/src/pages/*.test.tsx`)

Old `test_accounting_e2e.py` (1164 lines targeting obsolete `RiskEngine`/`trades.csv`)
replaced with 93 tests targeting `accounting.py`'s `_Ledger` API directly.  Sections:
A entry-fill mechanics, B exit-fill mechanics, C gross-P&L arithmetic, D net-P&L,
E YES/NO/UP/DOWN resolution (preamble rules), F resolution state machine,
G ledger-CSV integrity, H two-sided maker pair tracking, I pair/hedge relationships,
J position query helpers, K VWAP helper, L token-space independence.

Vitest 4.1.5 + `@testing-library/react` + jsdom installed in `webapp/`.
New UI test files: `Trades.test.tsx` (50 tests), `Pending.test.tsx` (26 tests).
Pure helpers exported from `Trades.tsx` and `Pending.tsx` for direct unit testing.

---

## [2026-04-30] - OpeningNeutral pre-market entry fixes; WS bid-monitoring exit; REST book source of truth

### Fix — OpeningNeutral: pre-market entry failures (`strategies/OpeningNeutral/scanner.py`, `config.py`)

Three root-cause fixes that together enable reliable pre-market entries:

**Subscription loop speed (5 s):** `_subscription_loop` sleep reduced from 30 s to 5 s so that a market entering the presub window is registered within 5 s — well before T-10 s.  With the 30 s loop a late restart could miss the window entirely.

**Presub window widened to -(TIMER_ADVANCE+30) s:** `_refresh_pending_markets` previously skipped markets with `elapsed < -30`.  With `TIMER_ADVANCE_SECS = 10`, this was too narrow — the earliest a market could be registered was T-30 s, leaving no room for the timer to sleep to T-10 s.  Now `presub_window = -(OPENING_NEUTRAL_TIMER_ADVANCE_SECS + 30) = -40 s`.

**REST API as source of truth on timer path:** `_evaluate_entry` on the timer path now fetches both YES and NO books via `pm_client.fetch_book_rest()` (CLOB REST API) instead of the WS book cache.  The WS cache was stale for pre-market tokens whose subscription had only been established seconds earlier (observed: BTC 6:35 AM bucket showed 0.46/0.55 stale data while REST showed 0.50/0.51).  Root cause confirmed from live logs.

**Entry restricted to pre-market timer path only:** WS path in `_evaluate_entry` now returns immediately (`_timer_fired` must be `True`).  Post-open asks are skewed by market movement and are no longer chased.  `elapsed_max = 0.0` enforces that the timer must still fire pre-market.

**Config changes:** `OPENING_NEUTRAL_TIMER_ADVANCE_SECS = 10.0` (was 0.05); `OPENING_NEUTRAL_PREWARM_SECS = 10.2` (was 0.2); `OPENING_NEUTRAL_MIN_SIDE_PRICE = 0.48` (was 0.44); `OPENING_NEUTRAL_MAX_SIDE_PRICE = 0.52` (was 0.56); `OPENING_NEUTRAL_COMBINED_COST_MAX = 1.01` (was 1.02); `OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS = 10` (was 5); renamed `OPENING_NEUTRAL_ENTRY_WINDOW_SECS → OPENING_NEUTRAL_MARKET_WINDOW_SECS = 60`.

**Silent rejection logging:** All `_evaluate_entry` rejection branches now emit `log.warning` with market name and reason (outside elapsed window, book not ready, book too thin, too expensive, price band).

### Feature — OpeningNeutral: WS bid-monitoring exit (`strategies/OpeningNeutral/scanner.py`)

Replaces resting GTC SELL exit orders with a WS-driven market sell:

**Root cause of prior approach failure:** Resting post-only GTC SELLs placed immediately after entry at `$0.35` were rejected by the CLOB with "crosses book" — because the current bid (~$0.50) is above the sell price, making them immediate takers rather than makers.

**New mechanism:** After both legs fill, `_register_pair` arms bid monitoring by writing both token IDs into `_token_to_pair: dict[str, str]`.  On every WS book update, `_on_price_event` checks the bid for monitored tokens.  When `best_bid ≤ OPENING_NEUTRAL_LOSER_EXIT_PRICE (0.35)`, it adds the token to `_exiting_legs` (duplicate-exit guard) and spawns `_execute_loser_exit` as an asyncio task.

**`_execute_loser_exit`:** Fetches actual CLOB balance (`get_token_balance`), then calls `place_market(side="SELL", price=0.01, size=sell_size)` — a FAK taker that fills at the best available bid.  Confirmed working in live trading: NO leg sold at $0.30 (trigger bid 0.34).  Winner promoted to momentum via existing `_on_exit_fill` path.

### Feature — `pm_client.fetch_book_rest()` (`pm_client.py`)

New async method `fetch_book_rest(token_id: str) → Optional[OrderBookSnapshot]` that fetches a fresh book from `GET https://clob.polymarket.com/book?token_id=<token_id>`.  Normalises bids/asks to sorted `(price, size)` tuples matching `OrderBookSnapshot` format, updates `_books` cache so subsequent `get_book()` calls are fresh, and returns `None` on any error (with debug log).  Used by `_evaluate_entry` timer path as the authoritative source of truth when WS cache may be stale.

---

## [2026-04-30] - OpeningNeutral entry latency; Chainlink HA mode; monitor & reconcile fixes

### Feature — OpeningNeutral entry latency reduction (`strategies/OpeningNeutral/scanner.py`, `config.py`)

Three optimisations that together reduce entry latency from ~1 s post-open to ~50 ms:

**Idea 1 — Scheduled timer entry (~200-400 ms saved):**  When `_refresh_pending_markets` registers a pre-open market, it spawns a `_scheduled_entry_task` that sleeps until `open_ts - TIMER_ADVANCE_SECS` (default 50 ms before open) and fires `_evaluate_entry` directly.  Entry no longer depends on a WS tick arriving after market open.  `_entered_market_ids` prevents the WS path from entering twice if both paths race.

**Idea 2 — Pre-qualification (~100-150 ms saved):**  Static gates (market type, `_is_updown_market` direction, entry-window membership) are checked once in `_refresh_pending_markets` at registration time rather than on every WS tick.  At timer-fire time only dynamic gates (combined cost, concurrent cap, conflict guard) run.  The `elapsed_min` lower bound is relaxed to `-1.0 s` when `_timer_fired=True` so timer-fired calls arriving 50 ms before open are not rejected.

**Idea 5 — TCP connection pre-warm (~50-100 ms saved):**  `_scheduled_entry_task` fires `_prewarm_clob()` at `open_ts - PREWARM_SECS` (default 200 ms before open), sending a lightweight authenticated GET (`get_live_orders`) to establish a TCP+TLS socket in the `requests` connection pool before the BUY order POSTs fire.  Non-fatal — entry proceeds normally if pre-warm fails.

**New config params (2):** `OPENING_NEUTRAL_PREWARM_SECS = 0.2`, `OPENING_NEUTRAL_TIMER_ADVANCE_SECS = 0.05`.

**New scanner state:** `_scheduled_entry_market_ids: set[str]` guards against duplicate timer scheduling across repeated `_refresh_pending_markets` sweeps; discarded in `finally` block of task.

**`_evaluate_entry` signature change:** `async def _evaluate_entry(self, market, _timer_fired=False)` — new `_timer_fired` parameter; elapsed lower bound becomes `elapsed_min = -1.0 if _timer_fired else 0.0`.

**Docs:** `strategies/OpeningNeutral/PLAN.md` updated with Entry Timing Architecture section (ideas 1, 2, 5), revised Order Flow (step 0 pre-open), Entry Conditions table with pre-qual column, Configuration section with new keys, Scanner State section.

### Feature — Chainlink Data Streams HA mode (`market_data/chainlink_streams_client.py`)

Migrates the Chainlink WS client from a single persistent connection to a multi-origin High-Availability architecture, mirroring the Chainlink Go SDK (`data-streams-sdk/go/stream.go`):

- One persistent WebSocket per server origin (typically 2: origin 001 and 002) discovered from the `x-cll-available-origins` header on the initial HTTP GET.
- Reports from all origins are deduplicated by `observationsTimestamp` watermark — the first copy wins, all duplicates are discarded.  No asyncio.Lock needed (check+set has no await between them).
- Reconnect with exponential backoff (`_HA_RECONNECT_MIN_S = 1.0`, `_HA_RECONNECT_MAX_S = 10.0`, max 5 attempts) classified as partial (≥1 other connection alive) vs full (all down).
- `StreamStats` dataclass (`accepted`, `deduplicated`, `partial_reconnects`, `full_reconnects`, `configured_connections`, `active_connections`) mirrors Go SDK Stats struct; accessible as `client.stats`.
- `is_connected` now returns `True` when `stats.active_connections > 0` (was: `self._ws is not None`).

### Fix — `should_exit` hedge-active parameter (`monitor.py`)

`should_exit` is a free function; it previously accessed `self._risk` directly to determine hedge fill status. `hedge_active: bool` is now a parameter computed by `PositionMonitor._check_and_exit_position` before the call, keeping `should_exit` pure. Logic unchanged.

### Fix — Momentum ticks dedup by composite key (`monitor.py`)

`_last_tick_state: dict[str, tuple]` tracks the last `(spot, token_price)` written per `market_id`. A tick is skipped when both values are unchanged and `exit_flag=False`, eliminating sub-millisecond burst rows caused by multiple oracle sources (Chainlink Streams, RTDS Chainlink, RTDS, PM WS) firing on the same price update.

### Fix — FAK order integer-k amount (`pm_client.py`)

The CLOB API requires `takerAmount` to be a multiple of 10 000 (≤ 2 dp in USDC).  `round(contracts × price, 2)` produces 4 dp for most tick-aligned prices.  Fix: `fak_amount = k * price` where `k = max(1, round(contracts))`.  IEEE 754 guarantees `(k × x)` is exact for integer `k`, so `takerAmount = k × 10^6` is always an exact multiple.  Market order BUY `mkt_amount` clamped to `max(size, 1.0)` to satisfy the PM $1 minimum.

### Fix — Reconcile: skip PnL patch when winner REDEEM not yet indexed (`pm_reconcile.py`)

`correct_outcome = None` is now returned when only `$0`-value REDEEM entries exist — this is ambiguous between "loser redeemed for $0" and "winner REDEEM not indexed by PM yet".  `None` means "leave recorded value unchanged".  PnL correction is also gated on `_has_real_exit_proceeds` to prevent over-writing a valid PnL with `−buy_usdc` when exit proceeds are missing.

### Fix — `finalize_hedge` double-write guard (`risk.py`)

`finalize_hedge` now returns early with the existing record if the hedge order's status is already in `HedgeStatus.TERMINAL`, preventing duplicate CSV rows when the function is called more than once for the same order.

---

## [2026-04-29] - Strategy 5 (Opening Neutral); CLOB v2 migration; hedge SL fix; venv auto-detection

### Feature — Strategy 5: Opening Neutral (`strategies/OpeningNeutral/scanner.py`, `strategies/OpeningNeutral/__init__.py`, `config.py`, `main.py`, `api_server.py`, `risk.py`)

Simultaneously buys the YES and NO token of the same Up/Down bucket market within `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` of market open. When both FAK legs fill at a combined cost ≤ $1.00 the pair is guaranteed-profitable at resolution. When only one leg fills the surviving leg is either promoted to a standard momentum position (`keep_as_momentum`) or immediately taker-exited (`exit_immediately`).

**New config params (14):** `OPENING_NEUTRAL_ENABLED`, `OPENING_NEUTRAL_DRY_RUN`, `OPENING_NEUTRAL_MARKET_TYPES`, `OPENING_NEUTRAL_ENTRY_WINDOW_SECS`, `OPENING_NEUTRAL_COMBINED_COST_MAX`, `OPENING_NEUTRAL_SIZE_USD`, `OPENING_NEUTRAL_ORDER_TYPE`, `OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS`, `OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS` (5 s — short window so exchange-killed FAKs fail fast), `OPENING_NEUTRAL_ONE_LEG_FALLBACK`, `OPENING_NEUTRAL_LOSER_EXIT_PRICE`, `OPENING_NEUTRAL_MIN_SIDE_PRICE`, `OPENING_NEUTRAL_MAX_SIDE_PRICE`, `OPENING_NEUTRAL_MAX_CONCURRENT`.

**Other changes:**
- `risk.py`: `neutral_pair_id: str = ""` field on `Position` links both YES and NO legs; cleared on the winner when promoted to momentum.
- `api_server.py`: `opening_neutral_ref` in `BotState`; `opening_neutral_enabled` / `opening_neutral_dry_run` exposed via `GET /config` and `PATCH /config`; `GET /opening_neutral/status` endpoint returns enabled state, dry_run, active pair count, pair details, and recent scan diagnostics.
- `main.py`: `OpeningNeutralScanner` instantiated when `OPENING_NEUTRAL_ENABLED=True`; wired into the asyncio task graph; `neutral_pair_id` propagated in `state_sync_loop`.
- `strategies/Momentum/scanner.py`: Opening-neutral conflict guard — skips any market where `opening_neutral` already has an open position, with `skip_reason="opening_neutral_active"` in diagnostics.
- Event logging (4 types → `data/momentum_events.jsonl`): `OPENING_NEUTRAL_PAIR_REGISTERED`, `OPENING_NEUTRAL_ONE_LEG_PROMOTED`, `OPENING_NEUTRAL_ONE_LEG_EXITED`, `OPENING_NEUTRAL_NO_FILL`.
- Concurrent entry guard counts both active pairs **and** in-flight entry attempts against `OPENING_NEUTRAL_MAX_CONCURRENT`.

### Fix — CLOB v2 migration (`pm_client.py`, `monitor.py`, `api_server.py`, `tests/test_pm_client.py`)

Fully migrated all production CLOB calls from `py_clob_client` → `py_clob_client_v2`.

**`pm_client.py` changes:**
- FAK (taker) limit path now uses `MarketOrderArgs` / `create_market_order` instead of `OrderArgs` / `create_order`. `MarketOrderArgs` uses the market-order signing path which rounds USDC amount to 2dp automatically, satisfying the API's maker_amount constraint.
- `cancel` → `cancel_order(OrderPayload(orderID=order_id))`.
- `get_orders` → `get_open_orders`.
- `BalanceAllowanceParams`, `AssetType` imports updated to `py_clob_client_v2`.
- Order `size` rounded to 2dp before all CLOB calls (API constraint: maker amount max 2dp).

**`monitor.py`, `api_server.py` redeem fix:**
- `pm._clob.get_conditional_address()` / `get_collateral_address()` don't exist in v2.
- Replaced with `_POLY_CONTRACTS = _get_contract_config(137)` (pure/hardcoded — no network call); use `.conditional_tokens` and `.collateral` fields.

**Tests (`tests/test_pm_client.py`):**
- All `py_clob_client` imports → `py_clob_client_v2`.
- FAK tests updated to mock `create_market_order` and assert `create_order.assert_not_called()`.
- `cancel` tests updated to `cancel_order(OrderPayload(...))`.
- Stale `get_conditional_address` / `get_collateral_address` mocks removed from `TestAutoRedeemDedup`.
- `test_no_winner_flag_falls_back_to_yes_price` renamed to `test_no_winner_flag_returns_none_for_retry` (preamble rule: never infer WIN/LOSS from `price`).

### Fix — Hedge SL suppression now requires actual fill (`monitor.py`)

Previously, any position with a non-null `hedge_order_id` suppressed all stop-losses (oracle delta SL, near-expiry stop, prob-SL). If the hedge order was cancelled or expired unfilled, the position was left naked with no protection but stop-losses still disabled.

**Fix:** `_hedge_active` now calls `self._risk.get_hedge_order(pos.hedge_order_id)` and checks `size_filled > 0`. An unfilled or cancelled hedge provides zero insurance — stop-losses run normally in that state.

### Fix — Launcher and main.py venv auto-detection (`launcher.py`, `main.py`)

**`launcher.py`:** Added `_find_venv_python()` which scans sibling `.venv` directories for the correct Python interpreter. `BOT_PYTHON` replaces `sys.executable` in `subprocess.Popen` — the bot always starts with the project venv regardless of how the launcher was invoked.

**`main.py`:** Venv guard at the top of the file: if `py_clob_client_v2` is not importable, the script calls `os.execv()` to relaunch itself with the `.venv` Python. Falls back to a clear error message with activation instructions if no venv is found.

### Fix — Gamma API per-slug error handling (`pm_client.py`)

Previously, any exception during the Gamma slug fetch (most commonly `asyncio.TimeoutError` with an empty `str()`) aborted the entire market refresh, leaving `_markets` stale. Now exceptions are caught per-slug; the failed slug is logged as a `WARNING` and the loop continues to the next slug.

### Feat — Multi-strategy WS token registration (`pm_client.py`)

`register_for_book_updates(token_ids, owner="default")` now accepts an `owner` key. Registrations are stored in `_extra_tokens_by_owner: dict[str, set[str]]` and unioned in `_update_shards`. This prevents one strategy from overwriting another's WS subscriptions (e.g. momentum and opening_neutral can both register independently). Extra tokens are also ordered and appended to `new_tokens` in `_update_shards` so they actually get subscribed to shards.

### Fix — `fetch_market_resolution` no price fallback (`pm_client.py`)

When `closed=True` but no `winner` flags are present in the CLOB response, the method previously fell back to the YES token's `price` field. Per the preamble rule, `price` can briefly show ~1.0 for a losing token right after settlement. The fallback is removed: the method now returns `None` so the monitor retries later when winner flags are set.

## [2026-04-27] - GTD hedge reprice sizing fix; `natural_contracts` field; parametric sweep tests

### Bug fix — Hedge reprice used inflated contract count instead of position-matched count (`monitor.py`, `risk.py`, `strategies/Momentum/scanner.py`)

When the natural coverage cost was below the PM $1 minimum at placement, `hedge_contracts`
was inflated to `1 / hedge_price` (e.g. 50ct for a 7.75ct position at 2¢). The monitor
reprice loop carried that inflated size forward on each tick, so repricing to 3¢ cost
`50 × 0.03 = $1.50` instead of the correct `ceil(1/0.03) × 0.03 = $1.00`. This eroded
`MIN_RETAIN_USD` and in some cases made the reprice unprofitable.

**Root cause:** `_pnl_cap` used the inflated `hedge_contracts` as divisor (6× too narrow),
and `monitor.py` reused `ho.order_size` (the inflated placement size) rather than the
natural position-matched count.

**Fix:**

- `scanner.py`: Capture `_natural_hedge_contracts = hedge_contracts` *before* the `$1` floor
  overwrites it. Use `_natural_hedge_contracts` for `price_cap`, ladder, and taker
  computations. Pass it to `register_hedge_order(natural_contracts=...)`.

- `risk.py` — `HedgeOrder` dataclass: Added `natural_contracts: float = 0.0` field (the
  pre-floor count, stored at placement). `register_hedge_order` accepts and stores it.
  `replace_hedge_order` accepts `new_order_size: float = 0.0`; if provided, the replacement
  `HedgeOrder` uses the new size rather than propagating the old inflated count.
  `natural_contracts` is always propagated across reprices.

- `monitor.py` — reprice sizing block: Replaced `max(remaining, 1/new_bid)` with
  a two-branch formula mirroring scanner placement:
  - If `ho.natural_contracts × new_bid >= $1` → use `natural_contracts` (full coverage).
  - Else → use `1 / new_bid` (PM $1 minimum, correct floor size for new price).
  Falls back to `ho.order_size` for legacy `HedgeOrder` objects without `natural_contracts`.
  PnL ceiling (projected_pnl − MIN_RETAIN) applied afterwards.

- `config.py`: `MOMENTUM_HEDGE_MIN_RETAIN_USD` changed from `0.15` → `0.25`.

### Feature — COB (CLOB-Oracle Blend) Kelly win-probability (`config.py`)

Added four new config parameters for the planned CLOB-Oracle Blend sizing model:
- `MOMENTUM_KELLY_EDGE_PREMIUM = 0.07` — systematic alpha above CLOB ask.
- `MOMENTUM_KELLY_WIN_PROB_CAP = 0.95` — hard cap on blended win_prob.
- `MOMENTUM_KELLY_CLOB_RELIABLE_TTE = 60` — seconds above which CLOB is fully weighted.
- `MOMENTUM_KELLY_ORACLE_SENSITIVITY = 0.15` — signal-strength → win_prob slope.

### Tests — Hedge sizing mathematical model and parametric sweep (`tests/test_hedge_sizing.py`, `tests/test_hedge_sweep.py`)

- `test_hedge_sizing.py`: 83-test pure-math suite verifying the scanner→monitor sizing
  pipeline: floor/natural branch crossover, `price_cap` profitability proof, `HedgeOrder`
  `natural_contracts` round-trip through `register`/`replace`, 7 scenario reprice ladders,
  and `HedgeOrder` risk-engine unit tests.

- `test_hedge_sweep.py`: 200-case parametric sweep (10 entry prices 0.65→0.83 × 20 buy
  notionals $1→$20) each running 94 hedge price steps (1¢–94¢). Confirms across 4,434
  non-blocked steps: PM $1 minimum never violated, over-hedge margin $0.00, MIN_RETAIN
  floor always maintained. Runs in < 1 second.

### Fix — Webapp trades page (`webapp/src/pages/Trades.tsx`)

Minor display improvements to the Trades dashboard page.

## [2026-04-25] - Chainlink Data Streams direct feed for all coins; hedge reprice + SL suppression; Phase C TTE gate; per-type delta floor

### Feature — Chainlink Data Streams direct feed extended to all 7 coins (`market_data/chainlink_streams_client.py`, `market_data/spot_oracle.py`, `config.py`)

Previously `ChainlinkStreamsClient` only fed HYPE/USD; all other coins used the RTDS
`crypto_prices_chainlink` relay as primary. After benchmarking all seven supported coins
(HYPE, BTC, ETH, SOL, BNB, DOGE, XRP), the direct Data Streams WebSocket consistently
arrives ~190ms ahead of the relay with 0.000bps price delta (100% direct wins over 58
matched rounds per coin in 60s tests).

**Changes:**

- `config.py`: Added `CHAINLINK_DS_FEED_IDS` dict — maps all 7 coins to their feed IDs,
  read from per-coin env vars `CHAINLINK_DS_{COIN}_FEED_ID`. Old single
  `CHAINLINK_DS_HYPE_FEED_ID` var retained for backwards compatibility.

- `chainlink_streams_client.py`: Multi-feed support — connects to a single WebSocket with
  all configured feed IDs and dispatches messages by `report.feedID`. `start()` now accepts
  an optional `coin=` parameter to subscribe to a single feed (used by the comparison
  script to avoid per-connection rate limiting during testing). `_active_feeds` dict replaces
  direct `config.CHAINLINK_DS_FEED_IDS` references in `_build_auth_headers` and `_ws_loop`.

- `spot_oracle.py`: `_get_chainlink_spot()` now prioritises ChainlinkStreamsClient for all
  coins in Chainlink bucket types (5m/15m/4h), falling back to RTDS relay then ChainlinkWSClient.
  Previous logic used freshest-timestamp arbitration and only used direct streams for HYPE.

**New env vars (all optional — bot degrades gracefully to RTDS-only without them):**
```
CHAINLINK_DS_BTC_FEED_ID=0x00039d9e4539...
CHAINLINK_DS_ETH_FEED_ID=0x000362205e10...
CHAINLINK_DS_SOL_FEED_ID=0x0003b778d3f6...
CHAINLINK_DS_BNB_FEED_ID=0x000335fd3f3f...
CHAINLINK_DS_DOGE_FEED_ID=0x000356ca64d3...
CHAINLINK_DS_XRP_FEED_ID=0x0003c16c6aed...
```

**New comparison script:** `scripts/compare_oracle_feeds.py --coin <COIN> --duration <S>`
runs a live side-by-side benchmark of direct vs relay for any coin.

---

### Feature — Hedge gap-closing reprice (`risk.py`, `monitor.py`, `config.py`)

When a GTD hedge order is resting in the CLOB, the monitor now tracks whether the
opposite-token's `best_ask` is falling (seller moving toward our bid). If the ask drops
since the last sweep, the hedge is cancelled and reposted at `current_bid + $0.01` —
closing the spread without chasing a rising ask.

Repricing is bounded by `price_cap` (set at placement: max price that keeps projected PnL
above `MOMENTUM_HEDGE_MIN_RETAIN_USD`). Reprices that would exceed the cap are skipped.

**New `HedgeOrder` fields:** `price_cap: float`, `last_clob_ask: Optional[float]` — both
persisted in `hedge_orders.json` so restarts don't lose the reference ask.

**New `RiskEngine` method:** `replace_hedge_order(old_id, new_id, new_price)` — atomically
marks the old order CANCELLED, creates a replacement with all metadata copied, updates
the parent Position's `hedge_order_id`, and persists.

**New config key:** `MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: int = 5`  
Near-expiry cancel: if TTE ≤ this threshold and the held token's CLOB mid is above 0.50
(winning), the hedge is cancelled — insurance no longer needed and adverse fill prevented.
Set to `0` to disable.

---

### Feature — Hedge SL suppression (`monitor.py`, `config.py`)

When `MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL = True` and a position has a resting GTD hedge,
all stop-losses (oracle delta SL, near-expiry time stop, CLOB prob-SL) are suppressed.
The hedge bounds the downside; any SL exit would lock in a loss before the hedge pays off.
Take-profit remains active. Defaults to `False` (conservative — all SLs fire regardless).

**New config key:** `MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL: bool = False`

---

### Feature — Phase C per-type TTE floor (`strategies/Momentum/scanner.py`, `config.py`)

A new per-bucket-type TTE ceiling blocks entries when time-to-expiry falls below a
configured threshold. Complements Phase B (global TTE ceiling) with type-specific tuning.

**New config key:** `MOMENTUM_PHASE_C_MIN_TTE_SECONDS: dict[str, int] = {}`  
Example: `{"bucket_5m": 30, "bucket_15m": 45}` — block entries in the last 30s of 5m
markets and last 45s of 15m markets. `0` or absent = disabled for that type.

Skipped markets are counted in scan diagnostics as `skipped_phase_c`.

---

### Feature — Per-bucket-type delta floor (`strategies/Momentum/scanner.py`, `config.py`)

`MOMENTUM_MIN_DELTA_PCT` can now be overridden per bucket type. The effective floor is
`max(coin_floor, type_floor)` — never lower than either individual setting.

**New config key:** `MOMENTUM_MIN_DELTA_PCT_BY_TYPE: dict[str, float] = {}`  
Example: `{"bucket_5m": 0.10, "bucket_15m": 0.08}`. Absent = falls back to coin floor.

`min_delta_floor` in scan diagnostics now reflects the combined (type + coin) floor.

---

### Fix — GTD hedge finalization after bot restart (`monitor.py`, `risk.py`)

Auto-redeem loop now handles the case where the parent Position was evicted from memory
after a restart but the HedgeOrder entity persisted in `hedge_orders.json`.

Previously: hedge payout silently lost (no `finalize_hedge` call, no trades.csv entry).  
Now: `get_hedge_order_by_token_id()` is called as a secondary lookup. If the HedgeOrder
is found, `finalize_hedge()` is called directly with the correct `filled_won`/`filled_lost`
status, updating both `hedge_orders.json` and `trades.csv`.

**New `RiskEngine` method:** `get_hedge_order_by_token_id(token_id)` — O(n) scan of
`_hedge_orders` by token_id; used only in the auto-redeem path (low frequency).

---

### Fix — Pending exit retry on EXIT_ORDER_FAILED (`monitor.py`)

Positions where all CLOB exit attempts fail now register in `_pending_exit_positions`
(maps `"market_id:side"` → original exit reason). On the next monitor sweep, the exit
is retried automatically. Cleared once the retry reaches `_exit_position`.

---

## [2026-04-23] - Concurrent TP+hedge; band_floor_abort hedge fix

### Bug fix — Sequential TP blocking hedge placement (`strategies/Momentum/scanner.py`)

**Root cause:** After a fill, the scanner placed the take-profit SELL limit (Phase C) and
only then placed the GTD hedge (Phase D). A TP retry loop of up to N attempts (~2 s wall
time) ran before the hedge started. On fast-moving markets (e.g. BTC 9:05AM April 22) the
opposite-token book drained during that delay, leaving nothing to bid on when the hedge
eventually ran.

**Fix:** Both coroutines (`_do_tp` and `_do_hedge`) are now defined as local `async def`
functions and launched together with `asyncio.gather(_do_tp(), _do_hedge(),
return_exceptions=True)`. They share the same event-loop and interleave at every `await`
point, meaning the hedge bid hits the CLOB at essentially the same instant as the TP order.

No new config keys.

---

### Bug fix — `band_floor_abort` path skipped GTD hedge entirely (`strategies/Momentum/scanner.py`)

**Root cause:** When a taker fill landed below `MOMENTUM_PRICE_BAND_LOW` (swept-book fill,
e.g. signal 0.88 → fill 0.34), the scanner registered the position via `_pos_bfa` and
immediately `return False`-ed. Phase D (the GTD hedge coroutine) was never reached.

This is precisely the scenario where the hedge matters most: a fill deep in the band means
the position is already underwater. A $0.05 resting BUY on the opposite token costs ~$1
and pays up to ~$19 if the position ultimately resolves against the main side.

**Measured miss (April 22 ETH 9:05AM):** fill 0.34 (signal 0.88), no hedge placed → loss
-$2.38. A DOWN hedge at $0.05 would have recovered ~$18 if ETH settled DOWN.

**Fix:** Replaced the early `return False` with a `_band_floor_aborted` flag. Execution now
falls through to the normal Phase C+D `asyncio.gather` block. `_do_tp` returns immediately
when the flag is set (no TP management for swept-book entries). `_do_hedge` runs normally
and is subject to all existing hedge rules:

- Projected win PnL must exceed $1 (`entry_size × (1 − entry_price) > 1.0`)
- Profit-safe price cap: `max_hedge_price = (projected_pnl − MOMENTUM_HEDGE_MIN_RETAIN_USD) / hedge_contracts`
- PnL cap ≤ 0 → hedge skipped
- TTE < 5 s guard still applies
- Book depth / ladder exhaustion still applies

After the CSV write and position-opened log, `return False` is restored so the monitor's
active SL/TP loop is skipped — exactly as before for band_floor positions.

No new config keys.

---

## [2026-04-22] - Hedge optimization (cap + ladder + taker + TTE); fill price fix; winner-flag resolution; test hardening

### Feature — Hedge optimization: profit-safe price cap (`strategies/Momentum/scanner.py`, `config.py`)

Before placing any hedge, the bot now computes the maximum price per contract it may pay
while still retaining at least `MOMENTUM_HEDGE_MIN_RETAIN_USD` of projected win PnL.

```
max_hedge_price = (projected_win_pnl − MOMENTUM_HEDGE_MIN_RETAIN_USD) / hedge_contracts
```

If the cap comes out ≤ 0 (the trade isn't profitable enough to afford any hedge), Phase D
is skipped entirely. The cap is always respected by both the taker and maker-ladder branches.

**New config key:** `MOMENTUM_HEDGE_MIN_RETAIN_USD: float = 0.50`  
Set to `0.0` to disable (old behaviour — no floor on retained profit).

---

### Feature — Hedge optimization: N-tick concession maker ladder (`strategies/Momentum/scanner.py`, `config.py`)

Instead of a single maker-only attempt at the configured price, the bot now retries up to N
times, raising the bid price by one tick (`$0.01`) per attempt. The ladder stops as soon as
a placement succeeds or the next price would exceed the profit-safe cap.

**New config key:** `MOMENTUM_HEDGE_MAX_TICKS_CONCESSION: int = 3`  
Set to `1` for a single attempt (closest to old behaviour).

---

### Feature — Hedge optimization: book-aware taker fallback (`strategies/Momentum/scanner.py`)

Before starting the maker ladder, the bot fetches the live order book for the opposite
token. If the current best ask is at or below the profit-safe cap, it switches to a taker
(FAK, `post_only=False`) order to grab the fill immediately rather than resting.

No new config key — fires automatically when `best_ask ≤ max_hedge_price`.

---

### Feature — Hedge optimization: TTE aggression mode (`strategies/Momentum/scanner.py`, `config.py`)

When time-to-expiry (`tte_seconds`) falls below a threshold, the bot forces taker mode even
if the book wouldn't have triggered it. This handles near-expiry thin-book scenarios where a
resting maker order has no realistic chance of being matched.

**New config keys:**
- `MOMENTUM_HEDGE_AGGRESSIVE_TTE_S: int = 0` — 0 = disabled; set e.g. 30 to activate
- `MOMENTUM_HEDGE_AGGRESSIVE_TAKER: bool = False` — True = always use taker (paper-mode testing)

---

### Feature — Hedge optimization: per-attempt $1 minimum size (taker branch) (`strategies/Momentum/scanner.py`)

The taker branch now recomputes contract count at the actual taker price (which may be
lower than the config price) and raises it to meet Polymarket's $1 minimum notional floor.
If meeting the $1 minimum would exceed the profit-safe cap budget, the hedge is skipped
rather than placing an oversized order.

No new config key — uses existing `MOMENTUM_HEDGE_MIN_RETAIN_USD`.

---

### Feature — Hedge CLOB tick log (`monitor.py`, `config.py`, `webapp/src/pages/Settings.tsx`)

New `hedge_clob_ticks.csv` sampled once per `_check_all_positions()` sweep while a GTD
hedge order is open and unfilled. Records CLOB mid, best bid, best ask, and TTE alongside
the hedge bid price — used post-trade to diagnose why a hedge didn't fill.

Columns: `ts`, `market_id`, `market_title`, `underlying`, `parent_side`, `hedge_order_id`,
`hedge_token_id`, `hedge_bid_price`, `clob_mid`, `clob_best_bid`, `clob_best_ask`, `tte_s`, `status`.

**New config keys:**
- `MOMENTUM_HEDGE_CLOB_LOG_ENABLED: bool = True` — toggle the hedge_clob_ticks.csv log
- `MOMENTUM_TICKS_LOG_ENABLED: bool = True` — toggle the existing momentum_ticks.csv log

Both are exposed as toggles in the webapp Settings page under **Analysis Logging**.

---

### Feature — Positions page: GTD Hedge Fills section (`webapp/src/pages/Positions.tsx`)

Open positions where `strategy="momentum_hedge"` are now surfaced in a dedicated
**GTD Hedge Fills** table on the Positions page, separate from main momentum positions.
Shows entry price, current CLOB price, deployed capital, and unrealised P&L for each
filled hedge.

---

### Bug fix — Fill price complement inversion (`pm_client.py`)

**Root cause:** `_fire_trade_fill` was computing taker execution price as a VWAP over
`maker_orders[i].price`. On neg-risk Polymarket markets, YES takers are matched against
NO-side makers whose price is the complement (e.g. 0.21 when the taker bought YES at
0.79). Using maker prices produced fill_price ≈ 0.21 for every YES entry.

**Cascading consequence:** Every live YES trade had fill_price < MOMENTUM_PRICE_BAND_LOW
(0.6), triggering `band_floor_abort` on every position. Phase D (GTD hedge placement)
was never reached for any live trade.

**Fix:** `exec_price` now comes exclusively from `trade_msg["price"]` — the taker's
execution price per the Polymarket CLOB API `types.ts` spec. `maker_orders` are used
only to aggregate matched size.

### Bug fix — `fetch_market_resolution` winner-flag priority (`pm_client.py`)

**Root cause:** `fetch_market_resolution` checked `tok.get("price")` first, then the
`winner` flag as a fallback. The preamble explicitly states that `price` can show ~1.0
for a losing token during the settlement window and must never be used as the primary
signal. This could cause WIN/LOSS outcomes in `trades.csv` to be recorded incorrectly
when the monitor ran in the brief window after market close.

**Fix:** `winner: True` flag is now checked first. `price` is only used as a fallback
when the `winner` field is absent from the CLOB API response entirely.

### Tests — Fill and resolution pipeline coverage

**`tests/test_pm_client.py`**
- Replaced `test_trade_fill_vwap_multi_level` with
  `test_trade_fill_uses_taker_price_not_maker_vwap`: maker prices now average to 0.47
  (not 0.50) so the old maker-VWAP path would fail — accidental symmetry removed.
- Added `test_trade_fill_neg_risk_uses_taker_price_not_maker_complement`: exact
  reproduction of the live bug (YES buy at 0.79, NO makers at 0.21). Asserts
  fill price = 0.79.
- Added `TestFetchMarketResolution` (6 tests): winner-flag priority, NO-win path,
  `test_winner_flag_beats_wrong_price` (anti-regression for settlement window race),
  price fallback when flag absent, closed=False → None, UP/DOWN label handling.

**`tests/test_momentum_scanner.py` — `TestGTDHedge`**
- Added `test_live_fill_valid_price_does_not_trigger_band_floor_abort`: live-mode
  scanner test (`_paper_mode=False`) that injects a correct WS fill at 0.79, then
  asserts position opens without `band_floor_abort` and Phase D hedge fires on the
  NO token. Directly catches any regression that reverts the complement price fix.

Total non-live-network tests: 1114 passed, 1 skipped.

---

## [2026-04-21] - HedgeOrder lifecycle entity; async CLOB I/O; Signals sort; Trades fill-ratio display; market_pnl API

### Feature — First-class `HedgeOrder` entity (`risk.py`)

**Problem:** GTD hedge state was scattered across `Position` fields, a transient
`_pending_hedge_cancels` dict in `PositionMonitor`, and ad-hoc logic in several files.
Fills were stored only as WS-detected booleans; there was no structured per-fill history,
no VWAP tracking, and no FIX-style lifecycle (open → partially_filled → filled).

**New `HedgeOrder` dataclass** tracks the full lifecycle of every GTD hedge order:
- Identity fields: `order_id`, `market_id`, `token_id`, `underlying`, `market_type`,
  `market_title`, `placed_at`
- Order params: `order_price`, `order_size`, `order_size_usd`
- Live state: `status` (FIX-style via `HedgeStatus` constants), `size_filled`,
  `size_remaining`, `avg_fill_price` (VWAP), `fills: list[HedgeFill]`
- Deferred-cancel state: `pending_cancel_threshold`, `pending_cancel_side`,
  `pending_cancel_strike`, `pending_cancel_entry_spot` — replaces the transient
  `PositionMonitor._pending_hedge_cancels` dict; survives bot restarts
- Resolution: `settled_price`, `resolved_at`, `spot_at_resolution`, `net_pnl`
- Parent reference: `parent_side` for O(1) `order_id → Position` lookup

**New `HedgeFill` dataclass:** `fill_id`, `price`, `size`, `timestamp`, `source`
(`"ws"` | `"clob_rest"` | `"reconciliation"` | `"paper"`)

**New `HedgeStatus` class:** `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`,
`CANCELLED_PARTIAL`, `EXPIRED_UNFILLED`, `EXPIRED_PARTIAL`, `FILLED_EXITED`, and a
`TERMINAL` frozenset.

**Persistence:** `HedgeOrder` entities are persisted to `data/hedge_orders.json` on
every state change and reloaded on startup.

**New `RiskEngine` methods:**
- `register_hedge_order(...)` — creates and persists a new `HedgeOrder`
- `update_hedge_fill(order_id, price, size, source)` — records a fill event,
  updates VWAP and `size_filled`, transitions status to `PARTIALLY_FILLED`/`FILLED`
- `update_gtd_hedge(...)` — mirrors `parent_side` onto the HedgeOrder entity
- `finalize_hedge(order_id, settled_price, spot_at_resolution, hedge_status)` — writes
  the terminal status and `net_pnl`; called at market resolution
- `get_position_for_hedge(order_id)` — O(1) lookup via `parent_side` key; falls back
  to O(n) scan for legacy orders missing `parent_side`
- `get_position_by_hedge_order_id(order_id)` — deprecated alias for the above
- `get_hedge_order_by_market(market_id)` — returns the most recent non-terminal (or
  any terminal) `HedgeOrder` for a market
- `get_hedge_orders_with_pending_cancel()` — returns all HedgeOrders with a live
  deferred-cancel threshold; replaces the in-memory `_pending_hedge_cancels` dict
- `market_pnl(market_id) → dict` — combined realized + unrealised + hedge P&L snapshot
  for a market; returns JSON-serializable dict consumed by the webapp and api_server

### Feature — Additive `trades.csv` schema migration (`risk.py`)

Two new columns added to `TRADES_HEADER`:
- `hedge_size_filled` — contracts actually matched (hedge rows only)
- `hedge_avg_fill_price` — VWAP across all fill events (hedge rows only)

`_ensure_csv()` now distinguishes between additive schema changes (old header is a
prefix of the new one) and incompatible ones. For additive changes it migrates the
existing file in-place (appends empty columns to every row) rather than backing up and
discarding the history.

### Fix — Async CLOB I/O (`pm_client.py`)

`create_order()`, `post_order()`, `create_market_order()`, `cancel()`, and
`cancel_all()` all use the blocking `requests` library under the hood. These were called
directly from the asyncio event loop, which stalled WS book-cache updates during the
signing + HTTP POST window.

All five calls now run via `asyncio.to_thread()`, keeping the event loop alive for WS
processing right up until (and during) the order-placement round trip.

`get_order_fill_rest()` return type changed from `Optional[tuple[float, float]]` to
`Optional[dict]` with keys `price`, `size_matched`, `size_remaining`, `status`. The old
`associate_trades` path was removed — it was fetching data from the counterparty
perspective, producing wrong price/size values (observed: price=0.97, size=1822 for a
0.035 × 28.57 hedge order). The order's own `price` field is now used as the fill price.

### Fix — `PositionMonitor` deferred-cancel dict replaced by `HedgeOrder` (`monitor.py`)

`_pending_hedge_cancels: dict[str, dict]` removed. All cancel-trigger state now lives on
`HedgeOrder.pending_cancel_*` fields, loaded from `hedge_orders.json`. The `on_price_update`
loop calls `risk.get_hedge_orders_with_pending_cancel()` instead of iterating the local dict.

`_add_pending_resolution` now coerces `underlying`, `market_slug`, and `market_type` args
to `str` (guards against `MagicMock` objects leaking in from test harness). `end_date` is
only converted with `.isoformat()` if it is a `datetime` instance.

### Fix — Band-floor abort path registers position (`scanner.py`)

Previously, when a momentum fill landed below `MOMENTUM_PRICE_BAND_LOW`, the order was
cancelled and the bot discarded the position entirely — tokens already in the wallet with
no settlement path. The abort path now registers a `Position` with `signal_source="band_floor_abort"`,
ensuring the PM-payout resolution path in `monitor.py` records the correct trades.csv row.

`scanner.py` updated to use `actual_fill["price"]` and `actual_fill["size_matched"]`
dict keys (matching the new `get_order_fill_rest()` return type).

### Feature — `GET /market_pnl` and `GET /market_pnl/{market_id}` endpoints (`api_server.py`)

Two new read-only endpoints expose `RiskEngine.market_pnl()` to the webapp:
- `GET /market_pnl` — returns P&L for all markets with tracked positions
- `GET /market_pnl/{market_id}` — returns P&L for a single market; 503 if risk engine not ready

Response shape: `{ "markets": { "<market_id>": MarketPnlRow }, "timestamp": float }`

### Feature — Webapp: hedge fill fields in SSE position rows (`main.py`)

`hedge_fill_detected`, `hedge_fill_size`, and `hedge_fill_price` were present on the
`Position` dataclass but never serialised into the SSE position dict in `state_sync_loop`.
`Positions.tsx`'s hedge badge always saw `undefined` and could never transition to the
"Filled" state even when WS detection had fired.

### Feature — Webapp: market P&L inline in Positions page; hedge fill badge (`Positions.tsx`, `client.ts`)

`useMarketPnl()` hook polls `/market_pnl` every 10 s. `MomentumRow` and `RangeRow`
receive a `pnl?: MarketPnlRow | null` prop and, when a hedge fill is confirmed and
`hedge_realized_pnl` is non-zero, render the realized hedge P&L inline next to the
fill badge (green `+$X.XX` / red `-$X.XX`).

New types in `client.ts`: `MarketPnlPosition`, `MarketPnlHedge`, `MarketPnlRow`,
`MarketPnlResponse`. New per-bucket hedge toggle fields added to `ConfigData`
(`momentum_hedge_enabled_5m/15m/1h/4h/daily/weekly/milestone`). `Trade` interface gains
`hedge_size_filled` and `hedge_avg_fill_price`.

### Feature — Webapp: sortable Momentum Scan table (`Signals.tsx`)

Column headers Bucket, Δ% vs Threshold, TTE, and Status are now clickable sort controls.
Default sort remains `gap_pct` descending. Clicking the same column toggles asc/desc;
clicking a different column resets to that column's natural direction (TTE defaults to
ascending; others descend). Active sort column shows a ▲/▼ indicator.

### Feature — Webapp: hedge fill-ratio display + new statuses (`Trades.tsx`)

The HedgeSection component now reads `hedge_size_filled` and `hedge_avg_fill_price` from
the trade row and computes fill ratio when available. Three new terminal statuses are
handled with distinct badge colours:
- `filled_exited` — hedge order filled during deferred-cancel window, then market-sold
- `cancelled_partial` — cancelled after accumulating partial fills
- `expired_partial` — GTD order expired with partial fill

### Tests

`tests/test_pm_client.py` added (526 lines): covers `get_order_fill_rest()` dict return,
`asyncio.to_thread()` wrapping for `create_order`/`post_order`/`cancel`/`cancel_all`,
`fetch_token_side()`, and paper-mode guards.

`tests/test_risk.py` additions: `HedgeOrder` lifecycle (`register_hedge_order`,
`update_hedge_fill`, `finalize_hedge`, VWAP accumulation, terminal-state guard),
`get_position_for_hedge()` O(1) path and legacy fallback, `market_pnl()`.

Full suite: **1,095 passed, 1 skipped** (the skipped test is a live Chainlink feed test).

---

## [2026-04-20] - Fix: hedge fill detection pipeline; webapp hedge state badge

### Fix — GTD hedge fills silently dropped by WS fill handler (`live_fill_handler.py`, `risk.py`, `monitor.py`)

**Problem:** Momentum GTD hedge orders are placed by `scanner.py` and tracked in
`risk._positions[].hedge_order_id`, but are never added to the maker's `ActiveQuote` dict.
`live_fill_handler._on_order_fill()` only searches `active_quotes` for the matching order,
so when a counterparty filled the hedge the WS MATCHED event arrived and was silently
dropped with a DEBUG log (`"fill for unknown/consumed order"`). The order disappeared from
the open CLOB order list, the bot's wallet held the filled tokens, but no `HEDGE_FILL` event
was recorded and `_record_pending_resolution_hedge()` would later mark the hedge as
`"unfilled"` — losing the actual payout.

Confirmed on 2026-04-19: XRP daily hedge order `0xf60fa033…` was placed at 0.02 for 45.45
contracts; CLOB shows `status=MATCHED, size_matched=45.45`; no fill row in `trades.csv`.

**Fix — three-layer detection pipeline:**

1. `live_fill_handler._on_order_fill` — after the `active_quotes` lookup fails, calls
   `risk.get_position_by_hedge_order_id(order_id)` (new method). If matched, sets
   `pos.hedge_fill_detected = True` and `pos.hedge_fill_size = cumulative_matched`.
   Logs at INFO. This covers real-time fills while the bot is running.

2. `monitor._record_pending_resolution_hedge` — before recording `"unfilled"`, checks
   `parent.hedge_fill_detected` + `parent.hedge_fill_size` and writes `filled_won` /
   `filled_lost` accordingly. Covers current-session fills.

3. `monitor._record_pending_resolution_hedge` (REST fallback) — calls
   `get_order_fill_rest(hedge_order_id)` on the CLOB. Covers fills that happened while
   the bot was offline or before WS detection was in place (e.g. the Apr-19 XRP case).

**`Position` dataclass additions (`risk.py`):**
- `hedge_fill_detected: bool = False`
- `hedge_fill_size: float = 0.0`

**New `RiskEngine` method:** `get_position_by_hedge_order_id(order_id) -> Optional[Position]`
— finds the open position whose `hedge_order_id` matches the given CLOB order ID.

### Feature — Webapp Positions page shows hedge state badge (`Positions.tsx`, `client.ts`)

**Previous behaviour:** The GTD Hedge column showed a plain purple text label with price and
USD size. No way to tell whether the hedge order was still resting or had been filled.

**New behaviour:** The cell now shows a coloured state badge:
- `—` — no hedge placed
- Purple **"Live · 2.2¢"** — hedge order resting on CLOB (not yet filled)
- Green **"Filled · 45.5ct"** — WS MATCHED event confirmed the hedge filled mid-trade

Each badge has a detailed tooltip (order ID, token ID, fill/bid price, size).

`Position` TypeScript interface gains `hedge_fill_detected?: boolean | null` and
`hedge_fill_size?: number | null` (serialised from `dataclasses.asdict` on the backend).

---

## [2026-04-16] - Bug fixes: range spot propagation, range tick delta, prob-SL oracle gate, WS fill detection, FAK fallback, hedge cancel regression, auto-redeem stale curPrice

### Fix — Auto-redeem used stale `curPrice` for WIN/LOSS determination (`monitor.py`)

**Problem:** Both auto-redeem paths (`redeemable=False` externally-settled detection and
`redeemable=True` on-chain submission) used `curPrice >= 0.99` from the PM wallet positions API
to decide WIN vs LOSS. `curPrice` is a stale CLOB mid-price that can show ~1.0 for a *losing*
token in the brief window right after settlement, before PM's oracle updates it. Confirmed on
2026-04-16: SOL DOWN token curPrice showed ~1.0 immediately after resolution even though SOL
ended UP (DOWN = LOSS). The bot closed the position as WIN (pnl=+$0.46), submitted an on-chain
redemption expecting $2.76, but the contract correctly paid $0.

**Fix:** Both paths now call `fetch_market_resolution(condition_id)` which reads the CLOB
`winner` flag — the authoritative source of truth per the PM Gamma API. `curPrice` is no
longer used for outcome determination anywhere in the auto-redeem flow. If CLOB isn't settled
yet (returns `None`), the cycle is skipped and retried on the next poll. This also closes the
same vulnerability in the externally-redeemed path.



### Fix — Range positions never received `current_spot` (`monitor.py`)

**Problem:** `_check_position()` fetched `current_spot` only when `pos.strategy == "momentum"`,
so range positions always had `current_spot = None`. This meant the delta stop-loss could never
evaluate for range markets. Confirmed by audit: 82,935 BTC weekly-range ticks all had empty
spot, so the position ran unprotected to expiry and lost $14.71.

**Fix:** Changed guard to `pos.strategy in ("momentum", "range")` so both strategy types get
spot data. Range positions already had `range_lo` / `range_hi` populated by the scanner; this
change connects the spot feed so those bounds can actually be evaluated.

### Fix — Range tick delta used momentum (strike-midpoint) formula (`monitor.py`)

**Problem:** `_write_momentum_tick()` always computed delta as `(spot − strike) / strike × 100`
regardless of strategy. For range positions the meaningful metric is distance to the nearest
bound, not distance to the midpoint strike.

**Fix:** When `pos.strategy == "range"` and `range_lo / range_hi` are populated, the tick delta
is computed as `min(spot − range_lo, range_hi − spot) / mid × 100` (positive = inside range)
for YES positions, and as distance above/below the range for NO positions (positive = outside
range = winning direction). Momentum positions use the unchanged strike-midpoint formula.

### Fix — Prob-SL could fire on CLOB book drain while solidly ITM (`monitor.py`)

**Problem:** When a range/momentum market approaches expiry, liquidity drains from the CLOB
book. The resulting price collapse could drop the token below `prob_sl_threshold` even when the
oracle confirms the position is solidly in-the-money. Confirmed by audit: XRP daily fired
prob-SL at 62% CLOB collapse while oracle delta was +27% ITM — a clear false positive.

**Fix:** Added `_oracle_delta_pct` capture from the oracle block in `should_exit()`. A new
`_prob_sl_oracle_ok` gate fires prob-SL only when:
- oracle data is unavailable (prob-SL remains the sole guard), **or**
- oracle delta is < 1.0% from strike (genuinely close — may legitimately be at threshold).

When oracle delta > 1% the position is solidly ITM; a CLOB collapse is book drain, not a real
directional move, and prob-SL is suppressed.

### Fix — User WS fill detection missed FILLED status and nested event format (`pm_client.py`)

**Problem:** The PM user WebSocket fill handler checked `msg.get("status") == "MATCHED"` with
an exact case-sensitive match. PM's API also emits `"FILLED"` status and a nested
`{"event_type": "order", "order": {...}}` format, both of which were silently ignored. Result:
`fill_from_ws = 0/32` fills detected via WS — all fell back to REST polling.

**Fix:** Broadened the check to handle `"MATCHED"` and `"FILLED"` case-insensitively, and added
a nested-format handler. Added `log.debug` of all user WS messages to aid future diagnostics.

### Fix — FAK exit retries exhausted with no fallback (`monitor.py`)

**Problem:** `_exit_position()` retried a FAK market sell up to 3 times (0.2 s sleep), then
logged `EXIT_ORDER_FAILED` and returned without placing any order — leaving the position open
indefinitely if the book was momentarily empty near expiry.

**Fix:** Increased retries to 5 (0.5 s sleep). After all FAK attempts fail, places a GTC limit
at `max(sell_price × 0.5, 0.01)` as a floor-price safety net. Only logs `EXIT_ORDER_FAILED`
and returns if the limit order also fails, requiring manual intervention.

### Fix — Hedge cancel fired on all loss exits (pre-existing regression) (`monitor.py`)

**Problem:** Working-tree code changed the GTD hedge cancel logic from "cancel only on win
exits" to "cancel on everything except RESOLVED and deferred MOMENTUM_STOP_LOSS", breaking the
intended behaviour of keeping the hedge alive on loss exits so it can partially recover.

**Fix:** Restored the `elif reason in _hedge_cancel_on_win` guard (win-only cancel), where
`_hedge_cancel_on_win = {ExitReason.PROFIT_TARGET, ExitReason.MOMENTUM_TAKE_PROFIT}`. Loss
exits (STOP_LOSS, NEAR_EXPIRY, etc.) fall through without cancelling the hedge.

---

## [2026-04-12] - Bug fixes: dip-market delta inversion, negative-EV Kelly override, per-bucket multiplier test isolation

### Fix — Dip-market NO/DOWN delta stop-loss inversion (`monitor.py`)

**Problem:** `should_exit()` always computed the NO/DOWN winning delta as
`(strike − spot) / strike × 100`, which is correct for *reach* markets ("Will ETH reach $3k?")
but inverted for *dip* markets ("Will ETH dip to $2,200?"). For a dip-market NO, the position
wins when `spot > strike`. With `spot = $2,223` and `strike = $2,200` the formula returned
`−1.065%`, which is always below the `+0.04%` stop-loss threshold — firing an instant false
stop-loss at open regardless of spot movement.

Root cause confirmed by examining a live trade: `entry_delta = −1.065`, `tok_drop_pct = 2.46%`
(bid/ask spread artefact), `hold_seconds = 0.1` — the position was killed in the same second
it was opened, with spot completely unchanged.

**Fix:** For NO/DOWN positions, infer the winning direction from `pos.spot_price` recorded at
entry. If `pos.spot_price > pos.strike` (dip market: entry spot was above strike) the correct
delta formula is `(current_spot − strike) / strike × 100`. Otherwise the legacy reach-market
formula `(strike − current_spot) / strike × 100` applies. `pos.spot_price` defaults to `0.0`,
so all existing reach-market positions, saved positions, and tests are unaffected.

The same directional fix was applied to `_write_momentum_tick()` so `momentum_ticks.csv`
records the correct signed `entry_delta` for auditing.

### Fix — Kelly MIN_ENTRY floor overrides negative-EV signals (`scanner.py`)

**Problem:** When raw Kelly fraction `f* = (p×b − (1−p)) / b < 0` (the model says the bet has
negative expected value), the `MOMENTUM_MIN_ENTRY_USD` floor was forcing a `$1` minimum entry
anyway. This occurred for deeply in-the-money tokens (price ≥ 0.95¢) where `payout_b` is so
small that `win_prob` cannot overcome the hurdle: e.g. `token = 0.955`, `payout_b = 0.0471`,
`win_prob = 0.919` → `f* = (0.919×0.047 − 0.081)/0.047 = −0.80`.

**Fix:** `_compute_kelly_size_usd` now returns `size_usd = 0.0` when `raw_kelly_f < 0`. The
`_execute_signal` entry path checks `size_usd == 0.0` and skips with an INFO log rather than
placing the order. `MOMENTUM_MIN_ENTRY_USD` only applies when Kelly says "bet small" (raw ≥ 0).
A new `kelly_f_raw` field is added to the fills CSV debug dict to make negative-EV decisions
auditable without re-running the math.

### Fix — TestPaperModePositionSizing missing multiplier reset (`tests/test_momentum_scanner.py`)

`TestPaperModePositionSizing.setup_method` was missing `MOMENTUM_KELLY_MULTIPLIER_BY_TYPE = {}`
in its saved/restored config state. When the per-bucket multiplier feature was added in the
previous session, the `bucket_5m` multiplier of `0.45` silently reduced the test's expected
`MAX_ENTRY_USD = $3.0` position to `$1.35`, causing four assertions to fail with
`assert 1.35 == 3.0`. Fixed by neutralising the per-bucket multiplier dict in setup, mirroring
the existing pattern in `TestKellySizing`.

The old `test_negative_ev_signal_returns_min` test (which asserted that a negative-EV signal
returns `MIN_ENTRY_USD`) was renamed `test_negative_ev_signal_returns_zero` and updated to
assert `size == 0.0`.

---

## [2026-04-11] - Phase B/B2/C/D/E + Kelly extensions: near-expiry oracle, hedging, win-rate gate

### Phase B — Two-oracle near-expiry strategy (`monitor.py`, `spot_oracle.py`, `config.py`)

**Problem:** The delta stop-loss and L2 oracle-vs-strike resolution path both used
`SpotOracle.get_mid()` (freshest-wins between RTDS relay and AggregatorV3).  Near expiry
the RTDS relay leads AggregatorV3 by up to ~15 s.  Polymarket's resolution contract calls
`latestRoundData()` on AggregatorV3 — not the relay — so a brief sub-strike dip captured
only by the relay can fire the SL even though AggregatorV3 (and therefore PM resolution)
never saw it.

**Fix:** New `SpotOracle.get_mid_resolution_oracle(underlying, market_type)` returns the
ChainlinkWSClient AggregatorV3-only price for Chainlink market types (non-HYPE), falling
back to `get_mid()` for HYPE and non-Chainlink types.

New config flag `MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY` (default `True`).  When
enabled:
- Near-expiry delta SL (`tte_seconds < MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS`) uses
  `get_mid_resolution_oracle()` instead of `get_mid()` so the SL matches PM's settlement.
- RESOLVED exit L2 path uses the same AggregatorV3-only feed.

**Caveat:** If PM's resolution bot itself uses the Data Streams relay in future, this fix
would need to be revisited.  Cross-check `oracle_ticks.csv` against known resolved
outcomes before disabling the flag.

### Phase B2 — LiveFillHandler per-market state reset (`live_fill_handler.py`)

**Problem:** `_matched_so_far` accumulated `{order_id: cumulative_fill_usd}` entries for
the lifetime of the process.  On a busy session with many markets the dict grew without
bound.  More critically, a late fill event from a previous market's WS recovery window
could be misread as belonging to the current market if order IDs ever collided.

**Fix:**
- New reverse index `_cond_to_order_ids: dict[str, set[str]]` tracks which order IDs
  belong to each condition ID.
- New `reset_market_state(condition_id)` method atomically removes all associated
  `_matched_so_far` entries when a market is closed/expired.
- `startup_restore()` now skips tokens with `redeemable=True` **or** a settled
  `cur_price` (≤ 0.01 or ≥ 0.99) — both categories are owned by
  `_redeem_ready_positions()`.  Restoring them as open positions caused a duplicate
  `trades.csv` row on every bot restart until they were redeemed on-chain.

### Phase C — Minimum elapsed-time guard (`scanner.py`, `config.py`)

New per-type dict `MOMENTUM_MIN_ELAPSED_SECONDS` (default empty → disabled).  When a
bucket type has a value set (e.g. `{"bucket_5m": 30}`), entries fired before that many
seconds have elapsed since market open are suppressed with `skip_reason="too_early"`.

**Rationale:** Early-window entries face a thin order book with wide spreads and noisy
initial ticks.  The elapsed-time guard gives the book time to stabilise before committing
capital.  The persistence clock (`signal_first_valid`) is also reset so a re-entry after
the guard window starts a fresh persistence accumulation.

`skipped_too_early` counter added to scanner summary and diagnostics API.

### Phase C (Chainlink) — Boundary tick logging (`market_data/chainlink_ws_client.py`)

For every AggregatorV3 `AnswerUpdated` event that lands within **[-15 s, +5 s]** of a
bucket boundary (300 s / 900 s / 14 400 s), a structured `CL_BOUNDARY_TICK` log entry
is emitted at INFO level including:

```
coin, price, period_s, secs_after_boundary, secs_before_next, local_ts, onchain_updated_at
```

`onchain_updated_at` is decoded from the event `data` field (raw `uint256` epoch seconds),
enabling post-hoc validation of whether the anchor was captured in the correct Chainlink
round.  Also added `_ADDR_TO_COIN` legacy alias for backward-compatible test imports.

### Phase D — GTD hedge (`scanner.py`, `config.py`)

After a confirmed momentum entry, optionally place a GTC maker limit BUY on the
**opposite** token at `MOMENTUM_HEDGE_PRICE` (default `$0.02`).

**Economics:** A fill at $0.02 that redeems at $1.00 returns $0.98/contract.  If the
held token loses, the opposite token resolves at $1.00, providing partial downside cover
that requires no oracle knowledge.  Maximum hedge cost ≈ `entry_size × 0.02` ≈ 2–3 % of
entry cost.

**Config:**
- `MOMENTUM_HEDGE_ENABLED` (default `True`) — master switch
- `MOMENTUM_HEDGE_PRICE` (default `0.02`) — GTC bid price on the opposite token

Hedge order is placed as `post_only=True` (maker).  Errors are logged at WARNING and do
not abort the primary position.  Hedge order IDs are not tracked (GTC orders are
self-managing and expire when the market closes).

### Phase E — Empirical win-rate gate (`strategies/Momentum/win_rate.py`, `scanner.py`, `config.py`)

New `WinRateTable` class (`win_rate.py`) builds a historical win-rate matrix from
`data/trades.csv` bucketed by `(market_type, price_band_5ct, time_bin_per_minute)`.
Loaded once at scanner startup; silently disabled if the data file is missing or has
insufficient fills.

**Gate logic:** If `MOMENTUM_WIN_RATE_GATE_ENABLED` is `True` and the win-rate table
has ≥ `MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES` fills in a bucket, an entry is suppressed
when `empirical_win_rate < model_win_prob × MOMENTUM_WIN_RATE_GATE_MIN_FACTOR`.

**Config:**
- `MOMENTUM_WIN_RATE_GATE_ENABLED` (default `False`) — disabled until ≥ 100 fills/bucket
- `MOMENTUM_WIN_RATE_GATE_MIN_FACTOR` (default `0.9`) — empirical WR must be ≥ 90 % of model WR
- `MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES` (default `10`) — minimum samples before gate activates

`skipped_win_rate` counter added to scanner summary.  Diagnostics fields `emp_win_rate`
and `model_win_rate` written to scan_diags when the gate fires.

### Kelly extensions: intra-sigma + persistence z-boost (`scanner.py`, `signal.py`, `config.py`)

Two optional extensions to the Kelly sizing formula:

**Intra-bucket realised sigma** (`MOMENTUM_KELLY_INTRA_SIGMA_ENABLED`, default `True`):
- Rolling `_spot_mid_history` deque (maxlen 300) records every RTDS/Chainlink tick per
  underlying coin.  `_compute_bucket_intra_sigma()` returns the annualised realised vol
  from the recent tick history.
- Kelly uses `max(sigma_ann, bucket_intra_sigma)` so elevated intra-bucket vol
  (e.g. after a sudden move) is captured even if the historical annual sigma is low.

**Persistence z-boost** (`MOMENTUM_KELLY_PERSISTENCE_ENABLED`, default `True`):
- `signal_first_valid` tracks the Unix timestamp when each market's signal first
  continuously cleared all gates.  Persisted to `data/signal_first_valid.json` so the
  clock survives bot restarts.
- `MomentumSignal.signal_valid_since_ts` exposes the clock to `_compute_kelly_size_usd()`.
- A z-boost up to `MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX` (default `0.5`) is blended in
  as the signal ages, rewarding durable edge vs. brief transient spikes.

**New `MomentumSignal` fields:** `bucket_intra_sigma: Optional[float]`,
`signal_valid_since_ts: float`.

### Monitor — External redemption detection + stale curPrice resolution (`monitor.py`)

`_redeem_ready_positions()` now handles two additional cases that previously leaked:

1. **Externally redeemed tokens (`redeemable=False`, settled `curPrice`):**
   When `cur_price ≤ 0.01` or `≥ 0.99` but `redeemable=False`, the token was already
   redeemed via the PM UI (or another bot instance).  The code now:
   - Infers the settlement direction from `cur_price` and whether the token is YES/NO,
   - Closes any ghost open position,
   - Calls `patch_trade_outcome(force=True)` to correct `trades.csv`.

2. **Stale mid-price on redeemable tokens:**
   When `redeemable=True` but `curPrice` is in `(0.01, 0.99)` (PM API hasn't updated
   from CLOB mid to settlement yet), the code now queries `fetch_market_resolution()` and
   resolves the correct YES-token settlement price before computing the payout.

`_check_pending_resolutions()` upgraded to `force=True` so PM CLOB settlement data
always overrides any earlier incorrect outcome recorded by the RESOLVED fast-path.

### Webapp — Phase B/C/D/E settings UI (`webapp/src/pages/Settings.tsx`, `webapp/src/api/client.ts`)

New **"Momentum — Advanced Phases"** card in Settings with controls for:
- Phase B: resolution oracle near-expiry toggle
- Phase C: per-type elapsed-time guards (5m / 15m / 1h / 4h / daily / weekly / milestone)
- Phase D: hedge enabled / hedge price
- Phase E: win-rate gate enabled / min factor / min samples

All fields wired to API server (`api_server.py`) via `ConfigPatch` model and
`_MUTABLE_CONFIG` registry.  `MOMENTUM_SCAN_INTERVAL` reduced to 1 s;
`MOMENTUM_MAX_CONCURRENT` raised to 20 to match live throughput requirements.

### Tests

- New test files: `tests/test_live_data_integrity.py`, `tests/test_spot_oracle.py`,
  `tests/test_win_rate.py`
- Significant additions to `tests/test_chainlink_ws_client.py` (boundary tick logging),
  `tests/test_live_fill_handler.py` (reset_market_state, settled-token skip),
  `tests/test_momentum_scanner.py` (Phase C/D/E gates, Kelly extensions),
  `tests/test_e2e_live.py`

---

## [2026-04-10b] - RESOLVED exit: 3-level outcome hierarchy (PM API → oracle → CLOB mid)

### Monitor — RESOLVED exit now uses PM settlement data as primary source

**Bug:** At the exact settlement second, the CLOB order book shows a stale mid price
(e.g. DOWN token ≈ $1.00) before it has absorbed the Chainlink oracle update.  The
RESOLVED fast-path was using `book_no_res.mid` as `exit_mid`, which snapped to `1.0`
→ `resolved_outcome="WIN"` → `pnl=+$0.15`.  No sell order is placed for RESOLVED exits
(PM distributes settlement directly), so this paper gain was **never received**.

**Example trade:** Bitcoin Up or Down 2:35–2:40 AM ET on 2026-04-10.
- Entered DOWN at $0.90, size 1.511
- CLOB book mid for DOWN at 06:40:01: ~$1.00 → bot recorded WIN +$0.15
- Chainlink settled BTC = $72,015.76 > strike $71,983.31 → DOWN lost
- Actual payout from Polymarket: $0 (real P&L: −$1.36; accounting error: +$1.51)

**Fix:** The RESOLVED fast-path now uses a three-level hierarchy to determine
`exit_mid`, from most to least authoritative:

1. **PM CLOB settlement API** (`fetch_market_resolution(condition_id)`):
   Queries `GET /markets/{condition_id}`, returns the settled YES-token price
   (0.0 or 1.0) once `closed=True`.  This is PM's own statement of the outcome,
   independent of order book state.  For NO/DOWN positions, `exit_mid = 1 − yes_price`.

2. **Oracle spot vs. strike** (momentum positions only, when L1 is still `None`):
   Compares the RTDS/Chainlink oracle spot price against `pos.strike` to infer the
   settlement direction.  Catches the window before the CLOB market object is marked
   closed on PM's side.

3. **CLOB book mid** (fallback, original behaviour):
   Used only when L1 and L2 both fail.  `_redeem_ready_positions()` acts as a final
   safety net in live mode — it re-closes with the correct payout from the Data API.

---

## [2026-04-10] - Momentum bug fixes: hysteresis, auto-redeem LOSS, slippage guard, strike diagnostics

### Monitor — Hysteresis reset guard (P2)

Previously `_delta_sl_ticks` (the 2-tick consecutive-below-threshold counter) was reset
on **any** non-STOP event, including oracle data gaps where `current_spot is None`.
A brief WebSocket interruption before the second tick would silently clear the counter,
effectively disabling the SL until the position crossed the threshold again from scratch.

Fix: the counter is now only reset when `current_spot is not None and pos.strike > 0`
— i.e., only when we have a valid oracle reading that genuinely showed delta is above
threshold.  Data gaps no longer reset the in-progress hysteresis accumulation.

### Monitor — Auto-redeem records LOSS outcome (P3)

When the PM wallet returned `redeemable=True, payout=0` (position resolved against),
`close_position()` was never called.  The position stayed open in the risk engine
indefinitely, `resolved_outcome="LOSS"` was never written to `trades.csv`, and the
risk engine's USD exposure remained inflated.

Fix: `_redeem_ready_positions()` now calls `close_position(exit_price=0.0, resolved_outcome="LOSS")`
for every zero-payout resolution, using the same market/token lookup as the WIN path.
Also fixed the `curPrice` field resolution to handle all PM API field name variants
(`curPrice`, `currentPrice`, `cur_price`).

### Scanner — Post-fill slippage guard

Added a post-fill check that aborts and cancels the order if the confirmed fill price
is below `MOMENTUM_PRICE_BAND_LOW`.  A fill significantly below the band means the ask
stack was swept during order transit (e.g. 0.925 → 0.12 with 87% slippage); the token
is no longer in a valid signal state and holding it is uneconomical.

### Scanner — Strike surfaced in diagnostics early (P1 partial)

The window-open spot recording for Up/Down markets was moved to a **pre-band** block
that runs before the signal band filter.  This ensures the strike is locked at the
moment the window opens, even for markets that start out-of-band.  Previously the
strike could be recorded minutes late after the price had already moved.

The recorded strike is now written to `_d["strike"]` at every scan-loop state —
including markets that are skipped by band/cooldown/delta filters — so the Signals page
can display it for all in-window markets.  Explicit-strike markets (e.g. "BTC above
$72,000") also surface their title-parsed strike.

### Webapp — Strike / Spot column on Signals page

The momentum diagnostics table now has a **Strike / Spot** column showing the recorded
strike price (white) alongside the current live oracle spot (grey) for every in-window
market.  This allows visual validation that the recorded strike aligns with Polymarket's
actual settlement oracle before deciding on P1 (strike recording alignment fix).

### pm_client — Taker order support

`place_limit_order()` accepts a `post_only=False` flag that switches the order type to
FAK (Fill-And-Kill) for immediate taker execution.  The "crosses book" retry is skipped
for taker orders since a crossing price is intentional.

### live_fill_handler — Skip duplicate reconciliation of closed markets

The PM wallet retains won tokens until they are manually redeemed on-chain.  Without
this guard, the reconciler would re-import already-closed positions, triggering a
duplicate `close_position()` call and a second row in `trades.csv`.

Fix: `closed_market_ids` is now built from the risk engine before the wallet loop, and
any wallet position whose `condition_id` is already closed is skipped.

### market_data — ChainlinkWSClient targets OCR2 aggregator addresses

Corrected the `eth_subscribe` filter to target the underlying OCR2 aggregator addresses
(not the proxy contracts) so `AnswerUpdated` events are actually received.  Proxy
contracts emit no logs directly — events come from the aggregator.
`SpotOracle` updated to include `ChainlinkWSClient` as a first-class source in the
freshest-wins race for non-HYPE Chainlink coins, with documentation clarifying that
live WS events require a paid Polygon RPC endpoint (Alchemy/Infura).

---

## [2026-04-07] - RTDS Chainlink routing + Webapp QA

### Summary

- Backend: RTDS Chainlink routing updated so short-bucket markets (5m/15m/4h) use the RTDS `crypto_prices_chainlink` relay as primary; ChainlinkWSClient (public eth_subscribe) is not relied on in production.
- RTDS: expanded `crypto_prices_chainlink` symbol map to include BTC/ETH/SOL/XRP/BNB/DOGE in addition to HYPE.
- Chainlink: corrected BNB AggregatorV3 Polygon address to `0x82a6c4AF830caa6c97bb504425f6A66165C2c26e`.
- `SpotOracle`: simplified routing to prefer RTDS chainlink snapshots; HYPE still races with Chainlink Streams when configured.
- New: `data/_compare_chainlink_sources.py` — script to compare RTDS chainlink relay vs direct AggregatorV3 HTTP polling (used for audit & latency analysis).

### Tests

- Unit tests: 918 passed, 6 skipped (live RTDS/Chainlink WS integration tests related to public eth_subscribe remain known-failing and are excluded from CI until a paid/working WS provider is available).
- `tests/test_main_wiring.py` updated to assert RTDS chainlink routing for 5m/15m/4h markets.

### Webapp

- Fixed multiple UI bugs (Performance, Logs, Markets, Settings, Trades) and resolved ESLint/TypeScript issues.
- Webapp dev server: verified running on port 5174 in dev environment.

### Rationale

These changes ensure production uses a low-latency, non-polling Chainlink relay (RTDS) for short-bucket oracle resolution while keeping on-chain AggregatorV3 addresses correct for reference and recovery modes. The QA pass tightened front-end correctness and linting, improving developer ergonomics and observability.

---

For detailed notes, see `market_data/rtds_client.py`, `market_data/spot_oracle.py`, and `webapp/src/pages/*`.
# Changelog

## 2026-04-06 — On-chain Chainlink oracle, range markets, UP/DOWN side fix, spot client rename

### On-chain Chainlink oracle (`market_data/rtds_client.py`)
- Added a second persistent WebSocket to `RTDSClient`: Polygon WSS `eth_subscribe` logs for
  Chainlink AggregatorV3 `AnswerUpdated` events on BTC/ETH/SOL/XRP/BNB/DOGE contracts.
- This is the **authoritative** price Polymarket reads at expiry to resolve 5m/15m/4h markets —
  subscribing to it on-chain means the bot uses the exact same oracle, not a proxy.
- Internal state split into `_chainlink_onchain` (primary) and `_chainlink_rtds` (fallback for HYPE
  and as a bridge between on-chain heartbeats).  Public API unchanged: `get_mid_chainlink()` /
  `get_spot_chainlink()` return on-chain price first, RTDS WS second.
- Added `all_chainlink_mids()` helper to expose both sources merged.
- Health-log loop updated: RTDS exchange prices still warn on >30 s staleness; on-chain ages are
  logged informatively (large ages are expected — oracle only updates on ≥0.5% deviation).
- LINK removed from `_RTDS_SYM_TO_COIN` (not a traded underlying; was causing untracked-coin log spam).
- Reconnect with exponential back-off (1 s → 60 s); 120 s silence triggers zombie-reconnect.

### Range markets sub-strategy (`config.py`, `scanner.py`, `api_server.py`, webapp)
- Added `MOMENTUM_RANGE_ENABLED` flag (off by default) to opt-in to scanning "Will BTC be between
  $X and $Y?" range markets.
- New independent config knobs: `MOMENTUM_RANGE_PRICE_BAND_LOW/HIGH`, `MOMENTUM_RANGE_MAX_ENTRY_USD`,
  `MOMENTUM_RANGE_VOL_Z_SCORE`, `MOMENTUM_RANGE_MIN_TTE_SECONDS` — all hot-patchable at runtime.
- Scanner detects range markets via `_RANGE_MARKET_RE` (in new `market_utils.py`) and strips the YES
  token from the band check if `MOMENTUM_RANGE_ENABLED` is false.
- Webapp Settings page has a new "Range Markets" card with all five knobs.
- Webapp Positions page shows a separate "Range Positions" table for `strategy == "range"` trades.
- All range config fields wired through `api_server.py` `/config` GET/PATCH endpoints.

### UP/DOWN market side-label fix (`live_fill_handler.py`)
- Introduced `_side_for_token()`: for Up/Down markets the side label is `"UP"` or `"DOWN"`, not
  `"YES"` or `"NO"`.  `risk.py` keys positions as `{market_id}:{side}`, so a mismatch created
  a duplicate ghost position instead of merging fills correctly.
- `import_positions()` also fixed: `bot_by_token` lookup now treats `"UP"` as `token_id_yes` and
  `"DOWN"` as `token_id_no`, preventing duplicate imports on restart.

### `spread_id` field (`risk.py`)
- Added `spread_id: Optional[str]` to the `Position` dataclass (populated when a position is one
  leg of a calendar spread; `None` for all existing single-leg positions).
- Added `spread_id` column to `data/trades.csv` (empty string for legacy / single-leg trades).

### Shared market-classification utilities (`strategies/Momentum/market_utils.py`) — NEW FILE
- Extracted `_STRIKE_PATTERNS`, `_UPDOWN_RE`, `_INVERTED_DIRECTION_RE`, `_RANGE_MARKET_RE`,
  `_extract_strike`, `_extract_range_bounds`, `_is_updown_market`, `_is_range_market`,
  `_is_inverted_direction_market` into a standalone module.
- Breaks the circular import between `scanner.py` and `spread.py`, both of which need these helpers.
- `live_fill_handler.py` reuses `_is_updown_market` for the side-label fix above.

### `pyth` → `spot_client` rename (all files)
- Every internal `pyth` / `self._pyth` / `pyth=` reference renamed to `spot_client` / `self._spot` /
  `spot_client=` across `monitor.py`, `scanner.py`, `vol_fetcher.py`, `maker/strategy.py`,
  `mispricing/strategy.py`, `main.py`.  RTDSClient is the actual underlying technology; "Pyth" was
  a historical misnomer.

### Tests
- `tests/test_rtds_live.py`: added three new live-feed test classes (887 total unit tests):
  - `TestRTDSSustainedFeed` — 30 s observation, tick-count, max-gap, half-window silence (23 tests).
  - `TestRTDSSourcesSeparated` — RTDS and Chainlink tracked in separate dicts over 30 s (28 tests).
  - `TestRTDSRawThroughput` — raw WS frame counter vs processed ticks diagnostic (1 test).
  Confirmed: ~1 tick/s/coin per source is the RTDS feed ceiling (not a client limitation); zero
  frames are dropped.
- `tests/test_api_server.py`: added coverage for range market config endpoints.
- `tests/test_momentum_scanner.py`: extended coverage for range market detection and inverted-direction logic.

## 2026-04-03 — Docs: SSE & polling updates

- Documented backend SSE endpoint and frontend SSE hook migration (reduced polling).
- Noted changes to polling intervals and cache behaviour (P&L 30s cache).
- Mentioned client-side `useSSE` hooks and server `/events` stream for live updates.
- QA fixes: mispricing scanner event ordering, signals list cap, small docstring fixes.

See commit history for code changes and tests (757 passed, 6 skipped).
