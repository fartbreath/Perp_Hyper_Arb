# Changelog

All notable changes to this repository are documented in this file.

## [2026-04-16] - Bug fixes: range spot propagation, range tick delta, prob-SL oracle gate, WS fill detection, FAK fallback, hedge cancel regression

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
