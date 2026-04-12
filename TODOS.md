# TODOS

## P1 — Oracle alignment: near-expiry SL must use AggregatorV3, not Data Streams relay
**What:** Add `SpotOracle.get_mid_resolution_oracle(underlying, market_type)` that for Chainlink market types returns **only** the on-chain `ChainlinkWSClient` price (AggregatorV3 `latestRoundData`), not the freshest-wins result that typically picks the RTDS Data Streams relay. In `_check_position`, when `tte_seconds < MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS`, use this stricter oracle for the spot used in delta SL and the L2 oracle-vs-strike resolution path.
**Why:** `_get_chainlink_spot()` uses freshest-wins between AggregatorV3 (~1/hr heartbeat or 0.5% deviation) and RTDS Data Streams relay (~1/sec). The relay almost always "wins" due to higher frequency. But Polymarket's resolution contract calls `latestRoundData()` on AggregatorV3 at expiry — giving the last confirmed on-chain round, which can be minutes stale. Near expiry this divergence is material: a flash move below strike that the relay captures but AggregatorV3 hasn't confirmed will fire the SL, but Polymarket resolves on the older AggregatorV3 price and the position would have won.
**Concrete failure mode:**
```
AggregatorV3 at expiry:  73,100 (last round 2 min ago, above strike 73,000)
RTDS relay at T-2s:      72,985 (brief dip, sub-strike, unconfirmed on-chain)
→ SL fires on relay → bot exits
→ Polymarket resolves on AggregatorV3 → YES wins → unnecessary loss
```
**Fix:** Two-oracle strategy:
- Live SL (all TTE): current freshest-wins behavior — fast reaction to confirmed moves
- Near-expiry final decision (TTE < NEAR_EXPIRY_TIME_STOP_SECS): prefer `ChainlinkWSClient` only (AggregatorV3) so the SL matches what the resolution contract will read
**Caveat:** If Polymarket's resolution bot itself uses the Data Streams relay (not the on-chain AggregatorV3 round), this fix would be backwards. Validate by cross-checking oracle_ticks.csv against known resolved outcomes before deploying.
**Effort:** S/M (human: ~2h / CC: ~15 min)
**Files:** `market_data/spot_oracle.py`, `monitor.py` (`_check_position`), tests

---

## P1 — Verify `clear_token_fills()` / fill-state reset per market in `live_fill_handler.py`
**What:** Confirm that `live_fill_handler.py` fully resets its fill-tracking state (pending order IDs, partial fill accumulators) when a new market condition ID is detected — equivalent to Poly-Tutor's `user_ws.clear_token_fills()` call in `_setup_market()`.
**Why:** Without a per-market reset, a fill event delivered late from the previous market's WebSocket recovery window could be misread as a fill for the current market, creating a phantom position or a mis-sized entry. This is a silent correctness issue that only surfaces under bad network conditions.
**Concrete failure mode:**
```
Market A times out → WS recovery starts → market ends before fill confirmed
→ Market B loads → delayed fill for Market A arrives → handler records position for Market B
→ bot holds phantom position, misses real entry
```
**Fix:** At market-start event (when new `condition_id` is seen), call `live_fill_handler.reset_market_state(condition_id)` or equivalent. If state is already per-condition-id scoped, verify and document it.
**Effort:** XS (human: ~30 min / CC: ~5 min) — mostly a read + test.
**Files:** `live_fill_handler.py`, `monitor.py` (market transition site)

---

## P2 — Add Chainlink boundary calibration tick logging
**What:** In `ChainlinkWSClient._handle_message()` (or equivalent in `market_data/spot_oracle.py`), log every Chainlink tick that falls within [-15s, +5s] of a market-window boundary, recording both the Chainlink internal timestamp and the local `time.time()` at receipt.
**Why:** Without this, it's impossible to validate post-hoc whether the oracle anchor was captured at the correct moment or was off by a tick. Poly-Tutor implements this exact pattern and uses it to calibrate anchor accuracy. This directly supports the P1 oracle alignment investigation — cross-reference these logs against `oracle_ticks.csv` and known resolution outcomes.
**Log format (from Poly-Tutor):**
```
BTC_TICK 17:00:00.000 (local 17:00:01.578) $69,483.32 [+0.000s after 17:00:00]
BTC_TICK 16:59:59.000 (local 17:00:00.653) $69,481.26 [-1.000s before 17:00:00]
```
**Effort:** XS (human: ~20 min / CC: ~5 min)
**Files:** `market_data/spot_oracle.py` or `market_data/chainlink_ws_client.py`

---

## P2 — Verify anchor capture uses Chainlink payload timestamp, not local clock
**What:** Check whether `btc_anchor_price` capture uses the timestamp from the Chainlink RTDS payload (`payload.timestamp` in ms) or `time.time()` for boundary-crossing detection.
**Why:** If local `time.time()` is used and the server clock drifts even 1–2 seconds vs Chainlink's internal clock, the anchor could be captured on the wrong side of a market boundary. Poly-Tutor explicitly uses `chainlink_ts_ms / 1000.0` from the payload. With a dedicated Chainlink API key on the way, fixing this now ensures the anchor is correct before the oracle P1 fix applies.
**Fix:** `price_ts = payload.get("timestamp", 0) / 1000.0 or time.time()` — use payload ts for window detection, local ts only as fallback.
**Effort:** XS (human: ~20 min / CC: ~5 min)
**Files:** `market_data/spot_oracle.py` or `market_data/chainlink_ws_client.py`

---

## P2 — $0.02 GTD hedge on opposite token as oracle-free downside protection
**What:** After a confirmed entry, place a Good-Till-Date limit buy on the opposite token at `$0.02` (configurable). Enable via `MOMENTUM_HEDGE_ENABLED` flag (default: False).
**Why:** Poly-Tutor uses this as a structural hedge that requires zero oracle knowledge. If the trade loses (our token → $0), the opposite token may briefly trade at $0.02 during the move. The GTD fills there, redeems at $1.00, recovering ~$0.98/contract. This partially offsets losses — especially valuable while the P1 oracle alignment fix is pending, since it doesn't rely on accurate BTC spot price to trigger.
**PnL math:**
```
Unhedged loss: -C × P_entry
Hedged loss (if GTD fills): -C × P_entry + C_hedge × 0.98
```
**Config:** `MOMENTUM_HEDGE_ENABLED: bool = False`, `MOMENTUM_HEDGE_PRICE: float = 0.02`, `MOMENTUM_HEDGE_ORDER_TYPE: str = "GTD"`
**Effort:** M (human: ~3h / CC: ~30 min)
**Files:** `monitor.py`, `hl_client.py`, `config.py`

---

## P2 — Build empirical win-rate matrix from historical trades
**What:** Build a `data/win_rate.csv` (or in-memory equivalent) — a matrix of historical win rates bucketed by: (market_type, price_band_5ct, time_bin_per_minute). Use it as a soft filter: if empirical win rate for the current (price, time_bin) is below `signal.win_prob × 0.9`, suppress entry.
**Why:** Current entry sizing uses a Gaussian model (Kelly with `sigma_ann`) for win probability. Historical empirical win rates capture regime effects that the Gaussian misses — e.g., bucket_5m markets in the last 60s at $0.82–$0.86 may historically resolve at 88%, while the Gaussian says 79%. Poly-Tutor uses this pattern and it's their primary signal quality filter. With 100+ fills this becomes actionable.
**Implementation:**
```python
# Offline: build from trades.csv + oracle_ticks.csv
# Online: load at startup, gate entries with:
emp_wr = win_rate_table.get(market_type, price_band, time_bin)
if emp_wr is not None and emp_wr < signal.win_prob * 0.9:
    return  # suppress entry
```
**Effort:** M (human: ~2h / CC: ~30 min) — requires >100 trades per bucket to be reliable.
**Depends on:** Kelly TTE floor PR live long enough to accumulate fills.
**Files:** `strategies/Momentum/scanner.py`, `data/win_rate.csv` (new), `audit_trades.py`

---

## P2 — Verify scanner has a minimum elapsed-time gate (too-early entry guard)
**What:** Confirm `scanner.py` has a gate that prevents firing signals in the first N seconds of a market (e.g., first 30–60s of a 5m bucket). If not, add `MOMENTUM_MIN_ELAPSED_SECONDS` config per market type.
**Why:** Early in a market window the order book is thin, spreads are wide, and price moves are noisy. Poly-Tutor explicitly enforces `min_elapsed_sec` as a separate gate from `min_tte`. Entries in the first 30s of a 5m market are almost always on thin volume and create wide slippage. A too-early entry could be the source of some unexpected losses.
**Effort:** XS (human: ~30 min / CC: ~5 min)
**Files:** `strategies/Momentum/scanner.py`, `config.py`

---

## P2 — Check momentum threshold floor (require >X%, not just >0)
**What:** Confirm what `MOMENTUM_MIN_DELTA_PCT` is set to in `config_overrides.json`. If it's ≤0.5%, raise to ~3–5% (equivalent to Poly-Tutor's `mom > 5%` gate).
**Why:** A near-zero momentum threshold allows entries on trivial ticks that don't reflect real directional flow. Poly-Tutor explicitly requires `momentum > 5%` (price change over 60s lookback) to filter noise entries. A higher floor means fewer entries but materially better signal quality.
**Note:** This is a tuning change, not a structural change. Measure impact on entry frequency before deploying live.
**Effort:** XS (human: ~10 min / CC: ~2 min)
**Files:** `config_overrides.json`, `config.py`

---

## P2 — Regression analysis: validate Kelly debug constants empirically
**What:** After 50+ fills with new Kelly debug fields (`kelly_persistence_pct`, `kelly_z_boost`, `kelly_tte_eff_s`), run a regression analysis.
**Why:** The persistence z-boost max (0.5σ) is an empirical constant. Need to validate that higher `persistence_pct` correlates with wins before relying on it for sizing.
**Effort:** M (human: ~1 day / CC: ~1 hour) — requires ~50 trades of data first.
**Depends on:** Kelly TTE floor + path history PR being live for long enough to accumulate fills.

---

## P3 — Per-coin Kelly TTE floor
**What:** Add `MOMENTUM_KELLY_TTE_FLOOR_SECONDS_BY_COIN` config — let BTC have a different TTE floor than DOGE in Kelly's sigma_tau computation.
**Why:** BTC jump risk (dollar magnitudes) differs from DOGE (percentage magnitudes at same IV). Currently floor = same for all coins within a bucket type. True fix is the Kelly TTE floor across all coins; per-coin extension is a future refinement.
**Note (user priority):** Low value. The real issue is that Kelly explodes at low TTE across all coins — a per-bucket-type TTE floor addresses this completely. Per-coin split is over-engineering until you have calibration data.
**Depends on:** Kelly TTE floor PR (must ship first).
**Effort:** S (human: ~1h / CC: ~5 min)

---

## P3 — Persist `_signal_first_valid` to disk on shutdown
**What:** Write `_signal_first_valid` to a JSON file on shutdown (like `_market_cooldown`), reload on startup.
**Why:** Currently the signal persistence state resets on every restart. Signals appear "fresh" post-restart, so the persistence z-boost is never applied in the first entry window after a restart.
**Note (user priority):** Nice-to-have. Only useful once signal persistence boost is implemented and you have evidence it's contributing.
**Effort:** S (human: ~30 min / CC: ~5 min)
