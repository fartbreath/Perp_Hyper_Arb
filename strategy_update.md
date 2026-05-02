# Strategy Update — Data-Driven Recommendations
## Source: REPORT.md analysis of dataset `20260501T112909`
## Applies to: Opening Neutral Strategy + Momentum Strategy + Open Market Entry (new)

---

## CEO Framing

The analysis dataset (681 labeled 5-minute binary market windows, 7 coins, 8 hours) reveals a structural finding that changes the strategic posture of this bot:

**The aggregate YES win rate is 49.8% — but filtered regimes reach 62–76%.** The edge does not come from being smarter about price direction. It comes from **market selection** — knowing which windows to trade at all. The Momentum strategy currently enters based on two signals (token price 80–90c + spot delta). The analysis shows five additional pre-entry signals that are independent of token price and consistently improve selection. These should be bolted on as gates, not as replacements for the existing logic.

The second finding is structural: the data was collected at **market open** (T=0 of the 5-minute bucket), not at expiry. This is a completely different entry regime than Momentum's near-expiry window. The pre-open signal quality is high enough to warrant a dedicated strategy: **Open**, a full-bucket taker entry at market start, using pre-open oracle and perp signals to predict direction over the full 5-minute window.

These two strategies are complementary, not competing:

```
OPEN STRATEGY                          MOMENTUM STRATEGY
Entry: T=0 (bucket opens)             Entry: T-30s to T-120s (near expiry)
Token price: ~0.40–0.60               Token price: 0.80–0.90c
Edge source: pre-open signal array    Edge source: time-decay + spot delta
Hold: full 5 min (300s)               Hold: last ~60s
Risk: 5 min oracle reversal           Risk: last-second reversal
EV driver: selection quality          EV driver: mean spread capture
```

---

## Part 0 — Opening Neutral Strategy: Specific Changes

The Opening Neutral strategy buys both YES and NO at open (~$0.50 each), places resting sell orders at $0.35 on both legs, and transitions the surviving leg to Momentum after the loser exits. The dataset provides specific, actionable guidance on which windows to enter and how to optimise the loser exit.

### 0.1 Context: What the Data Actually Shows About Opening Neutral

The P&L math in the PLAN assumes entry at `combined_cost ≈ $1.00`. The data shows:

| Metric | Value |
|--------|-------|
| Mean combined cost (YES_ask + NO_ask) | **$1.0567** |
| Median combined cost | $1.02 |
| Fraction with combined cost > $1.00 | **100%** |
| Percentiles (5th / 25th / 50th / 75th / 95th) | $1.01 / $1.01 / $1.02 / $1.10 / $1.17 |

The PLAN's current `OPENING_NEUTRAL_COMBINED_COST_MAX = 1.01` is already tight. Only the bottom ~5th percentile of windows qualifies. That is not a bug — it is correct selectivity. The data says *entering at median combined cost of $1.02 is acceptable, but entering above $1.10 is expensive*. **The existing gate is calibrated correctly and should not be loosened.**

### 0.2 Add Spread Gate — Cold Book Guard (HIGH PRIORITY)

**Data:** Mean spread is 11.3%, but the distribution has a long right tail. Windows with wide spreads at open (cold books) have higher entry costs and slower loser-leg price discovery. A cold book means the $0.35 resting sell may sit for a long time before a counterparty arrives.

**Recommended change:** Add `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD` gate. If either the YES spread or the NO spread at entry is above this threshold, skip.

```json
"OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD": 0.15
```

**Rationale:** A 15c spread on a ~50c token means the market maker is extracting 30% of the token price. The resting sell at $0.35 requires the token to fall 15c from mid just to fill — in a cold book, the loser leg may not drop cleanly. 0.15 is a softer gate than the other AI's suggested 0.20; use 0.15 initially and widen if it blocks too many otherwise-good windows.

**Implementation:** Already computed in the WS book cache at T=0 (`best_ask − best_bid` for each token). Zero new data sources.

### 0.3 Use Funding Rate to Estimate Loser Leg Exit Speed (MEDIUM PRIORITY)

**Data:** The funding rate predicts which direction the oracle will likely move:
- Funding > +0.00001 → 62.3% probability of NO win (YES will be the loser leg)
- Funding < −0.00001 → 76.2% probability of YES win (NO will be the loser leg)

For Opening Neutral, knowing which leg is likely the loser is directly actionable: it lets you **set a tighter resting sell on the predicted loser** to increase fill probability, while keeping the standard price on the predicted winner.

**Recommended change:** Add directional asymmetric resting sell pricing:

```python
# In _place_resting_sells():
if funding_rate > OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD:
    # NO leg is likely loser — place tighter sell to get the fill faster
    no_sell_price = OPENING_NEUTRAL_LOSER_EXIT_PRICE        # default 0.35
    yes_sell_price = OPENING_NEUTRAL_LOSER_EXIT_PRICE + 0.03  # give winner more room
elif funding_rate < -OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD:
    yes_sell_price = OPENING_NEUTRAL_LOSER_EXIT_PRICE
    no_sell_price = OPENING_NEUTRAL_LOSER_EXIT_PRICE + 0.03
else:
    yes_sell_price = no_sell_price = OPENING_NEUTRAL_LOSER_EXIT_PRICE
```

```json
"OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD": 0.00001,
"OPENING_NEUTRAL_WINNER_SELL_BUFFER": 0.03
```

**Rationale:** We are not trying to predict direction — Opening Neutral is always neutral. But knowing which leg is *more likely* to be the loser lets us set the loser's resting sell price more aggressively (tighter = easier to fill = capital freed sooner), and give the winner's resting sell price more headroom so it does not accidentally fill early on a transient dip.

**Important caveat:** This is a soft optimization, not a hard gate. The strategy must remain genuinely neutral — both legs always get a resting sell. Only the *price level* is asymmetric.

**⚠️ Deploy disabled initially.** The 0.03 winner buffer is not derived from empirical tick-level fill data — it is an estimate. Without data on actual resting order fill times, there is a genuine risk the predicted winner's resting sell (placed at $0.38 instead of $0.35) allows the winner to dip briefly to $0.36 on intraday noise and get sold early — locking in a suboptimal exit vs. holding to $1.00. Add `OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED: false` (default). After 2 weeks of production runs with symmetric sells ($0.35 on both legs), measure: (a) average intraday minimum price of the predicted winner leg, and (b) average time for the predicted loser to first reach $0.35. If the winner minimum is always > $0.38 and the loser fills at least 30% faster when its sell is tighter, enable the asymmetry.

### 0.4 Use YES Depth Share to Confirm Predicted Loser and Set Exit Levels (MEDIUM PRIORITY)

**Data:** YES depth share (YES_bid_depth / total PM depth) predicts direction with AUC 0.5683 (p=0.002):

| YES Depth Share | YES Win Rate | Predicted Loser |
|----------------|-------------|-----------------|
| Q1 < 25% | 41.5% | YES leg more likely to lose |
| Q4 > 75% | 60.0% | NO leg more likely to lose |

When funding rate and depth share agree on the predicted loser, the signal is compounded.

**Recommended change:** Combine funding + depth share into a loser confidence score:

```python
loser_confidence = 0  # -1 = YES likely loser, +1 = NO likely loser, 0 = neutral

if funding_rate > threshold:   loser_confidence += 1  # NO wins → YES is loser
if funding_rate < -threshold:  loser_confidence -= 1  # YES wins → NO is loser
if yes_depth_share > 0.75:     loser_confidence += 1  # crowd favors YES → NO is loser
if yes_depth_share < 0.25:     loser_confidence -= 1  # crowd favors NO → YES is loser
```

When `abs(loser_confidence) >= 2` (both signals agree), tighten the predicted loser's resting sell by an additional 0.02 (e.g., $0.35 → $0.33) for faster exit. When `loser_confidence == 0` (conflicting signals), keep both sells at the standard $0.35.

This is a **fill-probability optimisation** — getting the loser sell filled faster frees capital and triggers the Momentum handoff sooner.

### 0.5 Evaluation of Other AI's Opening Neutral Suggestions

| Suggestion | Our Assessment | Action |
|-----------|---------------|--------|
| Combined cost ≤ 1.015–1.02 | **Reject — already tighter.** The PLAN has `1.01`. Loosening to 1.02 would allow the median window (which has no edge) to qualify. Keep at `1.01`. | No change |
| Skip if spread ≥ 0.20 at T+0 | **Accept with tighter threshold.** A 0.15 spread gate is more conservative and appropriate for a $1 position. | Added as §0.2 |
| Funding + velocity lean aids loser leg exit | **Accept in modified form.** We use funding (not velocity) to set asymmetric resting sell prices. Velocity is already available but adding it as a second gate would over-filter a strategy that is already very selective. | Added as §0.3 |
| Depth share for loser prediction | **Accept.** Valid use of depth share for fill-probability optimisation, not direction betting. | Added as §0.4 |
| Position sizing $3–$8 per leg | **Already handled.** Current config is `$1/leg` for paper trading. Scaling to $3–$8 in live is a sizing decision, not a strategy change. | No code change needed |
| Hybrid market/limit exit (60-70% market sell + resting limit) | **Reject.** Adds execution complexity with no validated benefit. The current resting limit at $0.35 already captures $0.35 vs $0.00 at resolution. A market sell at the wrong moment captures less than $0.35. The split-order logic would require a new order management path, new edge cases, and a new failure mode (partial fill reconciliation). Not worth it. | No change |
| Pre-strike entry subset testing | **Defer.** Too vague to implement. No data in the dataset to validate pre-strike entry timing for Opening Neutral. | Deferred |

### 0.6 Opening Neutral — Consolidated Config Keys and Implementation Summary

The three recommended changes (§0.2, §0.3, §0.4) add the following new config keys:

```json
// §0.2 — Cold book guard
"OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD": 0.15,

// §0.3 — Asymmetric resting sell prices based on funding direction
"OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD": 0.00001,
"OPENING_NEUTRAL_WINNER_SELL_BUFFER": 0.03,

// §0.4 — Loser confidence score: tighten predicted loser exit by additional 0.02
//         when both funding and depth share agree (|loser_confidence| >= 2)
"OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN": 0.02
```

**Implementation sequence:**

1. **§0.2 first** (P0, 15 min): Gate check at entry. If `YES_spread > 0.15 OR NO_spread > 0.15`, skip. This is one line in `_qualify_entry()`. Immediately reduces cold-book exposures.

2. **§0.3 next** (P1, 45 min, **default disabled**): Build the asymmetric pricing logic in `_place_resting_sells()` but ship with `OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED = false`. Enable only after 2 weeks of fill-time measurement data confirms the winner leg does not dip below $0.38 intraday. See §0.3 for the specific enablement criteria.

3. **§0.4 last** (P1, 30 min): Compute `loser_confidence` from funding + depth share at entry time (both available from cached data at T=0). When `|loser_confidence| >= 2`, tighten predicted loser's sell by `OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN = 0.02`.

**Config keys that do NOT change:** `OPENING_NEUTRAL_COMBINED_COST_MAX = 1.01` (keep tight), `OPENING_NEUTRAL_LOSER_EXIT_PRICE = 0.35` (keep at $0.35, not $0.30–0.32), `OPENING_NEUTRAL_SIZE_USD = 1` (keep at $1 for paper trading).

### 0.7 Opening Neutral — Go/No-Go Criteria

The Opening Neutral strategy has thin edge after combined cost. Its P&L depends entirely on the loser leg reaching $0.35 within the 5-minute bucket. If the loser stalls at $0.42–$0.45 for the full bucket, both legs settle: the loser goes to $0.00 (not $0.35) and the net result is `$1.00 (winner) + $0.00 (stalled loser) − $1.02 (entry) = −$0.02`. This is a structural failure mode that the optimisations in §0.3/§0.4 reduce but cannot eliminate. A valid critique is that the ON strategy's complexity is only justified if its pair-completion mechanics actually work in live PM CLOB conditions.

**Before investing additional development time in Opening Neutral, the first 20 paper trades must clear all three thresholds:**

| Threshold | Measurement | Pass Criterion | Fail Action |
|-----------|-------------|----------------|-------------|
| Pair-completion rate | % of trades where loser fills ≤$0.35 within the bucket | > 50% | Raise `OPENING_NEUTRAL_LOSER_EXIT_PRICE` to 0.38, or deprioritize ON |
| Winner conversion rate | % of completed pairs where winner reaches ≥$0.90 | > 60% | Investigate if Momentum handoff timing is broken |
| Realized P&L per pair | Mean net gain per completed pair (net of entry cost) | > $0.25 | Review combined_cost gate — tighten from 1.01 to 1.005 if most good windows are blocked |

If the pair-completion rate falls below 50%, the $0.35 resting sell is too tight for PM CLOB dynamics in these markets — the loser token maintains buyer support at $0.40+ through the full bucket. In this scenario, **deprioritize ON development and redirect effort to the Open directional strategy (Part 2)**, which has cleaner EV mechanics and no dependency on intra-bucket fill timing. The Open strategy is the better fallback.

---

## Part 1 — Momentum Strategy: Specific Changes

### 1.1 Add HL Funding Rate as a Direction Gate (HIGH PRIORITY)

**Data:** The funding rate is the single most predictive pre-entry filter in the dataset.

| Funding | Direction | n | Win Rate | EV/Trade |
|---------|-----------|---|----------|----------|
| < −0.00001 | YES | 21 | 76.2% | +36.4% |
| > +0.00001 | NO | 138 | 62.3% | +9.5% |
| −0.00001 to +0.00001 | either | ~450 | ~49% | negative |

The current Momentum strategy enters YES when spot is above strike — it has no funding gate. This means it enters YES entries in strongly positive-funding regimes where the NO win rate is 62.3%. That is trading against the signal.

**Recommended change:** Add `MOMENTUM_FUNDING_GATE_ENABLED` (default: `True`). When enabled:
- **YES entries**: skip if `funding_rate > MOMENTUM_FUNDING_GATE_YES_MAX` (recommended: `+0.00001`)
- **NO entries**: skip if `funding_rate < MOMENTUM_FUNDING_GATE_NO_MIN` (recommended: `−0.00001`)
- When `|funding_rate| > 0.00001` and direction agrees: treat as a +1 conviction boost (allow smaller `MOMENTUM_PRICE_BAND_LOW`, e.g. accept 0.75c entries) **and** apply a size multiplier of `MOMENTUM_FUNDING_SIZE_MULTIPLIER` (recommended: `1.5`) — but only for the NO-in-positive-funding regime (n=138). The YES-in-negative-funding regime (n=21) does **not** get a size increase — see small-sample rule below.

**New config keys:**
```json
"MOMENTUM_FUNDING_GATE_ENABLED": true,
"MOMENTUM_FUNDING_GATE_YES_MAX": 0.00001,
"MOMENTUM_FUNDING_GATE_NO_MIN": -0.00001,
"MOMENTUM_FUNDING_CONVICTION_BOOST": true,
"MOMENTUM_FUNDING_SIZE_MULTIPLIER": 1.5,
"MOMENTUM_FUNDING_SIZE_MULTIPLIER_MIN_N": 50
```

The `MOMENTUM_FUNDING_SIZE_MULTIPLIER` only applies when the regime has at least `MOMENTUM_FUNDING_SIZE_MULTIPLIER_MIN_N = 50` validated forward samples. On first deployment, this starts at 1.0× for both regimes. After 50 forward-validated NO-in-positive-funding fills confirm ≥60% win rate, enable 1.5× for that regime only.

**Implementation note:** Funding rate is already available from `hl_client` — this is a one-line gate check. The HL `funding_rate` field is in the imbalance/mark data already fetched for delta computation. No new API calls required.

**⚠️ Small-sample sizing rule (n < 50):** The extreme negative-funding YES case (n=21, 76.2%) is the single highest-confidence signal in the dataset — but n=21 is too small to size at Kelly or half-Kelly. Until at least 50 forward-validated windows confirm the signal, use it only as a conviction boost: apply the `MOMENTUM_FUNDING_CONVICTION_BOOST` (smaller price band, lower z-score bar) but do **not** increase position size above the normal `MOMENTUM_MAX_POSITION_USD`. Re-evaluate after 30 days of forward data. The same rule applies to any regime with n < 50 in this dataset.

---

### 1.2 Add Hour-of-Day Bias Filter (MEDIUM PRIORITY)

**Data:** Strong, consistent time-of-day effects across all 7 coins (n=84 per hour):

| UTC Hour | YES Win Rate | Action |
|----------|-------------|--------|
| 11–13 | 62–65% | Normal YES entries |
| 14 | 39.0% | Require higher z-score for YES; lean NO |
| 15 | 63.6% | Normal YES entries |
| 16 | **35.0%** | Block YES entries OR require 2× z-score |
| 17 | 49.7% | Flat — skip marginal signals |
| 18 | **35.2%** | Block YES entries OR require 2× z-score |
| 19 | 46.9% | Mild NO lean |

The 16:00 and 18:00 UTC windows are not slightly weak — they are consistently 15 percentage points below break-even for YES. Entering YES at 85c in these hours produces negative EV regardless of the token-price and delta signals.

**Recommended change:** Add `MOMENTUM_HOUR_BIAS_MULTIPLIER` — a dict mapping UTC hour to a z-score multiplier for each direction:

```json
"MOMENTUM_HOUR_BIAS_ENABLED": false,
"MOMENTUM_HOUR_Z_MULTIPLIER": {
  "14": {"YES": 1.4, "NO": 0.85},
  "16": {"YES": 1.8, "NO": 0.75},
  "17": {"YES": 1.2, "NO": 1.0},
  "18": {"YES": 1.8, "NO": 0.75},
  "19": {"YES": 1.1, "NO": 0.95}
}
```

The effective z-score becomes `MOMENTUM_VOL_Z_SCORE × multiplier`. At a multiplier of 1.8 on the 16:00 YES side, a z-score of 1.6449 becomes an effective requirement of ~2.96 (99.85th percentile) — effectively blocking all but extreme YES moves at that hour.

**Implementation note:** `datetime.utcnow().hour` is the only input needed. No new data sources.

**⚠️ Single-day data caveat — default disabled.** `MOMENTUM_HOUR_BIAS_ENABLED` defaults to `false`. These multipliers are derived from a single 8-hour session (May 1, 2026). The 16:00 and 18:00 UTC effects (35% YES) may reflect that specific day's bearish afternoon, not a structural pattern. Enable only after 30 forward trading days confirm the same hours consistently fall below 45% YES win rate. If the effect does not replicate over 30 days, discard this section and rely solely on §1.6 (TWAP × volatility) as the regime filter. §1.6 is the durable generalisation; this section is a provisional single-day shortcut.

---

### 1.3 Add YES Depth Share as Confirmation (MEDIUM PRIORITY)

**Data:** PM book depth share predicts direction with AUC 0.5683 (p=0.002):

| YES Depth Share | YES Win Rate |
|----------------|-------------|
| Q1 < 25% | 41.5% |
| Q4 > 75% | 60.0% |

When the Momentum strategy is about to enter YES, the PM book is already showing the direction. If YES depth share is in Q1 (<25%), the PM book is positioned against the YES entry.

**Recommended change:** Add `MOMENTUM_DEPTH_SHARE_GATE_ENABLED`. When enabled:
- YES entries: skip if `yes_depth_share < MOMENTUM_DEPTH_SHARE_YES_MIN` (recommended: `0.40`)
- NO entries: skip if `yes_depth_share > MOMENTUM_DEPTH_SHARE_NO_MAX` (recommended: `0.60`)

The depth share is computable from the PM CLOB snapshot already fetched during the scan. YES depth share = `YES_bid_depth / (YES_bid_depth + NO_bid_depth)`.

**New config keys:**
```json
"MOMENTUM_DEPTH_SHARE_GATE_ENABLED": true,
"MOMENTUM_DEPTH_SHARE_YES_MIN": 0.40,
"MOMENTUM_DEPTH_SHARE_NO_MAX": 0.60
```

---

### 1.4 Update Per-Coin Stop-Loss Levels (HIGH PRIORITY — quick win)

**Data:** Empirical CL oracle adverse move distribution for NO outcomes:

| CL Decline from Strike | % of NO outcomes |
|-----------------------|-----------------|
| >0.01% | 89.2% |
| >0.02% | 79.2% |
| >0.05% | 59.4% |
| >0.10% | 30.4% |
| >0.20% | 9.4% |

Combined with the per-coin vol characteristics implied by the dataset, the recommended `MOMENTUM_DELTA_SL_PCT_BY_COIN` values are:

```json
"MOMENTUM_DELTA_SL_PCT_BY_COIN": {
  "BTC": 0.03,
  "ETH": 0.04,
  "SOL": 0.05,
  "XRP": 0.06,
  "BNB": 0.04,
  "DOGE": 0.08,
  "HYPE": 0.10
}
```

**Rationale:** The global default `MOMENTUM_DELTA_STOP_LOSS_PCT = 0.05` is appropriate for ETH/SOL but too wide for BTC (which rarely moves 0.05% in 60s) and too tight for DOGE/HYPE (where a single tick routinely exceeds 0.05%). Tightening BTC/ETH reduces hold time on losing positions. Widening DOGE/HYPE avoids noise-triggered exits.

**The 0.05% threshold captures 59.4% of all losing trades** — meaning ~40% of losses involve only a small adverse move that triggers SL without the trade being "clearly wrong." The per-coin calibration reduces false exits on high-IV coins.

---

### 1.5 Add DOGE Serial Dependence (LOW EFFORT, HIGH SPECIFICITY)

**Data:** DOGE shows the strongest autocorrelation pattern in the dataset:
- After two consecutive NO outcomes: **75.0% YES** on next window
- After two consecutive YES outcomes: **65.2% NO** on next window
- Lag-1 AC = −0.123, Lag-2 AC = −0.217 (strongest mean-reversion coin)

This is actionable because the Momentum strategy can track outcome history per coin per bucket type. When DOGE has had two consecutive NOs, the prior is strongly bullish — this should lower the z-score threshold for the next DOGE YES entry.

**Recommended change:** Add `MOMENTUM_STREAK_BIAS_ENABLED` and a `streak_tracker` dict (maintained in memory by the strategy, not persisted between restarts). When the last two DOGE outcomes are both NO, apply `DOGE_NO2_STREAK_MULTIPLIER = 0.80` to the effective z-score for the next YES entry (lowering the bar). Clear streak on any new entry.

**New config keys:**
```json
"MOMENTUM_STREAK_BIAS_ENABLED": true,
"MOMENTUM_STREAK_BIAS_MAP": {
  "DOGE": {"NO_NO": {"YES": 0.80}, "YES_YES": {"NO": 0.80}},
  "XRP":  {"YES_YES": {"YES": 0.85}}
}
```

**Caveat:** This is a small sample (75% based on n=~12 events). Size very conservatively until n > 30. Use the multiplier to allow entries at the existing price band — do not lower the band below 0.75c.

---

### 1.6 CL 10s TWAP Deviation as Low-Vol Gate (MEDIUM PRIORITY)

**Data:** The TWAP × volatility interaction is one of the most actionable findings:

|  | Low Vol | High Vol |
|--|---------|---------|
| TWAP dev > 0 (price above avg) | 52.9% YES | 54.1% YES |
| TWAP dev < 0 (price below avg) | **37.6% YES** | 54.6% YES (random) |

When 60s realized vol is below its median AND the CL oracle price arrived at open below its 10-second TWAP (i.e., price sold off in the last 10s), the YES win rate is only 37.6%.

**Recommended change:** For YES entries in low-vol regimes, add a soft gate:
- Compute `cl_twap_10_dev_bps = (CL_current − CL_TWAP_10s) / CL_TWAP_10s × 10000`
- If `cl_vol_60s < vol_median` (below-median volatility) AND `cl_twap_10_dev_bps < -5` (price is below its 10s avg):
  - Apply z-score multiplier of 1.4 for YES entries (raise the bar)
  - The signal is a momentum sell-off into the entry window → continuation risk

**New config keys:**
```json
"MOMENTUM_TWAP_GATE_ENABLED": true,
"MOMENTUM_TWAP_DEV_LOW_VOL_YES_MULTIPLIER": 1.4,
"MOMENTUM_TWAP_DEV_THRESHOLD_BPS": -5
```

**Note:** The 60s vol median must be tracked as a rolling statistic per coin. A simple rolling percentile buffer (already partially implemented in `VolFetcher`) is sufficient.

---

### 1.7 Hold Optimisation — cl_upfrac_during (MEDIUM PRIORITY)

**Data:** `cl_upfrac_during` (fraction of CL up-ticks during the 5-min bucket) has AUC **0.7031** — the strongest predictive signal in the entire dataset. It cannot be used at entry but is the primary real-time exit signal.

The current Momentum strategy uses:
- `MOMENTUM_TAKE_PROFIT = 0.999` (token approaches 1.0)
- `MOMENTUM_DELTA_STOP_LOSS_PCT` (oracle retreats toward strike)

What's missing: an adaptive hold signal. When `cl_upfrac_during` drops below 0.45 for 2+ consecutive 5-second measurement windows, the oracle momentum has shifted against the position. This is a stronger exit trigger than the delta SL for gradual reversals.

**Recommended change:** Add a soft momentum-exit to the position monitor:

```
if cl_upfrac_last_10s < MOMENTUM_UPFRAC_EXIT_THRESHOLD for MOMENTUM_UPFRAC_EXIT_WINDOWS consecutive:
    exit position (do not wait for delta SL)
```

**New config keys:**
```json
"MOMENTUM_UPFRAC_EXIT_ENABLED": true,
"MOMENTUM_UPFRAC_EXIT_THRESHOLD": 0.40,
"MOMENTUM_UPFRAC_EXIT_WINDOWS": 2,
"MOMENTUM_UPFRAC_WINDOW_SECONDS": 5
```

**Implementation:** Use an **exponentially weighted moving fraction (EWMA)** over the last 10–15 CL ticks rather than a raw rolling count. Raw fraction is noisy in the first 60 seconds of a bucket — a single cluster of 3 up-ticks can push the fraction from 0.35 to 0.65 and back down within 5 seconds. EWMA with α=0.3 smooths this without significant lag.

Only act on the EWMA if it has been continuously below `MOMENTUM_UPFRAC_EXIT_THRESHOLD` (or above for buys) for at least `MOMENTUM_UPFRAC_EXIT_WINDOWS × MOMENTUM_UPFRAC_WINDOW_SECONDS` consecutive seconds — this avoids whipsaw exits from a single tick cluster.

The CL WebSocket stream is already consumed by the bot. The `VolFetcher` already maintains a rolling buffer. `cl_upfrac` is the ratio of `(current_price > prev_price)` ticks in the last N ticks. This is a two-line addition to the position monitor loop.

**The missing mirror: pyramid on confirmed direction.** The EWMA exit fires when oracle momentum turns *against* the position. The symmetric case is equally actionable: if `cl_upfrac_ewma` at T+60s is above a high threshold (e.g., 0.65+), the oracle has shown sustained directional momentum in the winning direction through the first full minute. Adding to the existing position at this point is equivalent to an Open-strategy entry at T+60s — with the additional confirmation of 60 seconds of directional oracle data already in hand. The added size benefits from both the remaining directional momentum AND the remaining time-decay compression (4 more minutes). See §7.1 for the mid-bucket entry pipeline entry. **Prerequisite before enabling:** Confirm that the average winning position's token price at T+60s is still below 0.80c — if the market has already moved the token to 0.85c by T+60s, there is little upside remaining and the add-on would chase an almost-settled position. Config key when ready: `MOMENTUM_UPFRAC_PYRAMID_ENABLED: false`, `MOMENTUM_UPFRAC_PYRAMID_THRESHOLD: 0.65`, `MOMENTUM_UPFRAC_PYRAMID_ADD_USD: 5.0`.

---

## Part 2 — Open Market Entry Strategy (New)

### 2.1 The Core Thesis

The Momentum strategy's edge comes from **near-expiry time-decay compression** — buying a token already priced at 85c with seconds remaining. The analysis dataset reveals a completely different and complementary edge: **pre-open oracle and perp signals** that predict direction at the **start** of a 5-minute bucket, when the token is priced near 50c.

This is the "Open" strategy: enter at T=0 when the bucket opens, hold for the full 5 minutes, exit adaptively using cl_upfrac_during and PM CLOB trajectory signals.

**Why this works separately from Momentum:**
- Momentum requires the token to be 80–90c already (high crowd conviction)
- Open requires no crowd conviction — it enters when price is near 50/50 and uses independent oracle/perp signals
- The two strategies almost never conflict: when Momentum is active (last 60–120s), Open has already been in the position for 3–4 minutes

### 2.2 Signal Architecture

Entry uses a **composite gate score** computed from pre-open data available at T=0 (or T−1s):

```
SCORE = 0

# Primary gate (highest weight)
if funding_rate < -0.00001 and direction = YES:   SCORE += 3
if funding_rate > +0.00001 and direction = NO:    SCORE += 2
if abs(funding_rate) < 0.00001:                   SCORE -= 1  # penalise neutral

# Hour-of-day
if utc_hour in {11,12,13,15} and direction = YES: SCORE += 1
if utc_hour in {14,16,18} and direction = NO:     SCORE += 1

# CL velocity + funding agreement
if cl_vel_bps_60s agrees with direction:          SCORE += 1

# PM book
if yes_depth_share > 0.75 and direction = YES:    SCORE += 1
if yes_depth_share < 0.25 and direction = NO:     SCORE += 1

# TWAP (low-vol only)
if cl_vol_60s < vol_median and cl_twap_10_dev > 0 and YES:   SCORE += 1
if cl_vol_60s < vol_median and cl_twap_10_dev < 0 and NO:    SCORE += 1

# Streak (DOGE only)
if coin == DOGE and last_2_outcomes == [NO, NO] and YES:     SCORE += 2

ENTER if SCORE >= OPEN_ENTRY_MIN_SCORE (recommended: 2)
```

**Validated filter performance (from REPORT):**

| Score condition | Direction | n | Win Rate | EV/Trade |
|----------------|-----------|---|----------|----------|
| Score ≥ 3 (funding extreme) | YES | 21 | 76.2% | +36.4% |
| Score ≥ 2 (funding + hour) | NO | 138 | 62.3% | +9.5% |
| Score ≥ 2 (combined) | YES | 166 | 60.2% | +2.9% |

Break-even win rate at median spread (6.1%): **52.7%**. All three regimes exceed this.

**⚠️ In-sample inflation warning.** The composite score uses seven components all fitted on a single day. The backtested win rates (60–76%) are likely inflated. Before enabling `OPEN_STRATEGY_ENABLED`, run a time-series split on the existing dataset: train on windows 1–340 (first 4 hours), test on windows 341–681 (last 4 hours). If out-of-sample win rate drops below 54% in the test split, raise `OPEN_ENTRY_MIN_SCORE` from 2 to 3. Do not deploy to live capital with in-sample validation only.

**⚠️ Small-sample sizing rule for Score = 3 entries.** The +3 score weight for extreme negative funding (n=21, 76.2% YES win rate) reflects the **magnitude** of EV/trade (+36.4%), not a 3× confidence advantage over the +2 regime. A SCORE ≥ 3 entry driven entirely by the extreme funding signal is subject to the same small-sample sizing rule from §1.1: position size is capped at `OPEN_MAX_POSITION_USD_BY_COIN × 1.0` (no multiplier) until 50 forward samples confirm ≥70% out-of-sample win rate for this regime. High composite score does not mean high n — do not conflate conviction with sample size.

### 2.3 Entry Mechanics

| Parameter | Recommended Value | Rationale |
|-----------|------------------|-----------|
| Entry price band | 0.40 – 0.65 | Near-50/50 at open; excludes already-biased markets |
| Entry timing | T=0 to T+10s | Within 10s of bucket open |
| Token side | Determined by score direction | YES if score favors YES, NO if NO |
| Max position size | `OPEN_MAX_POSITION_USD_BY_COIN` (see §2.5) | Per-coin cap accounts for book depth |
| Depth-adjusted size | `min(coin_cap, 0.5% of total YES+NO bid depth at T=0)` | Prevents self-impact on DOGE/HYPE thin books |
| Order type | Limit at ask+0.5c | Consistent with Momentum fill logic |
| Max concurrent | 2 (separate from Momentum count) | Different hold regime, different risk |

### 2.4 Exit Mechanics

**The PM CLOB trajectory tells you within 5–30 seconds whether you're losing:**

| Time | Signal | Action |
|------|--------|--------|
| T+1s | No information (YES bid = 0.4529 winning vs 0.4528 losing) | Hold |
| T+5s | YES bid divergence opens (77bp gap) | Monitor |
| T+10s | Clear divergence (164bp); if YES bid < 0.460 → likely NO win | First exit decision |
| T+30s | Maximum divergence (526bp); YES bid < 0.440 → exit immediately | Hard exit gate |

**Adaptive hold algorithm:**

```
HOLD while:
  1. cl_upfrac_during > 0.50 (oracle still trending in winning direction)
  2. pm_yes_bid at T+10s > 0.455 (for YES entries)
  3. CL oracle has not declined >0.05% from strike

EXIT when any of:
  1. cl_upfrac_during < 0.45 for 2+ consecutive 5s windows  [EWMA, α=0.3]
  2. pm_yes_bid at T+10s < 0.450 (for YES entries)
  3. CL oracle declines >0.05% from strike (59.4% of losing trades captured)
  4. Token bid approaches 0.92+ (near-settlement profit lock)
  5. T+270s (3s before settlement window) — mandatory exit if still open
```

**Probabilistic SL at T+10s (combined trigger):** At T+10s, compute `implied_prob = YES_mid / (YES_mid + NO_mid)` where mid = (bid+ask)/2. If `implied_prob` is below the exit threshold **and** CL has simultaneously declined by `OPEN_DELTA_STOP_LOSS_PCT × 0.60` or more, treat this as a double-trigger early exit — market consensus and oracle are both pointing against the position. This adapts the SL to PM CLOB sentiment without relying on PM CLOB as the sole trigger (per preamble rules, CLOB alone is not authoritative).

**⚠️ The 0.38 threshold is not data-derived.** The dataset shows YES bid at T+10s = 0.4799 for winning windows and 0.4273 for losing windows. The corresponding mid-price `implied_prob` for a typical losing window is approximately `0.4273 / (0.4273 + 0.5727) ≈ 0.43`. A threshold of 0.38 is materially below the losing-window median and would only trigger on the worst-performing subset of losses. **Before enabling `OPEN_IMPLIED_PROB_SL_ENABLED`:** compute from the dataset (1) the 10th percentile of `implied_prob` at T+10s for YES-winning windows — the threshold must sit below this to avoid exiting winners; (2) the 90th percentile for losing windows — set the threshold at or above this value to catch typical losses. Replace 0.38 with the empirically-derived value.

```json
"OPEN_IMPLIED_PROB_SL_ENABLED": true,
"OPEN_IMPLIED_PROB_SL_THRESHOLD_YES": 0.38,
"OPEN_IMPLIED_PROB_SL_THRESHOLD_NO": 0.38,
"OPEN_IMPLIED_PROB_SL_DELTA_PCT_TRIGGER": 0.03
```

### 2.5 Config Keys

All keys prefixed `OPEN_` to avoid collision with `MOMENTUM_` namespace:

```json
"OPEN_STRATEGY_ENABLED": false,
"OPEN_ENTRY_MIN_SCORE": 2,
"OPEN_MAX_ENTRY_USD": 30.0,
"OPEN_PRICE_BAND_LOW": 0.40,
"OPEN_PRICE_BAND_HIGH": 0.65,
"OPEN_ENTRY_WINDOW_SECONDS": 10,
"OPEN_MAX_CONCURRENT": 2,
"OPEN_MARKET_COOLDOWN_SECONDS": 300,
"OPEN_FUNDING_GATE_YES_MAX": 0.00001,
"OPEN_FUNDING_GATE_NO_MIN": -0.00001,
"OPEN_DEPTH_SHARE_YES_MIN": 0.40,
"OPEN_DEPTH_SHARE_NO_MAX": 0.60,
"OPEN_DELTA_STOP_LOSS_PCT": 0.05,
"OPEN_DELTA_SL_PCT_BY_COIN": {
  "BTC": 0.04, "ETH": 0.05, "SOL": 0.06,
  "XRP": 0.07, "BNB": 0.05, "DOGE": 0.10, "HYPE": 0.12
},
"OPEN_UPFRAC_EXIT_THRESHOLD": 0.45,
"OPEN_UPFRAC_EXIT_WINDOWS": 2,
"OPEN_PM_BID_EXIT_THRESHOLD_YES": 0.450,
"OPEN_PM_BID_EXIT_THRESHOLD_NO": 0.450,
"OPEN_PROFIT_TARGET": 0.88,
"OPEN_HOUR_SCORE_MAP": {
  "11": {"YES": 1}, "12": {"YES": 1}, "13": {"YES": 1},
  "14": {"YES": -1, "NO": 1},
  "15": {"YES": 1},
  "16": {"YES": -2, "NO": 2},
  "17": {},
  "18": {"YES": -2, "NO": 2},
  "19": {"NO": 1}
},
"OPEN_MAX_POSITION_USD_BY_COIN": {
  "BTC": 30.0, "ETH": 30.0, "SOL": 20.0,
  "XRP": 20.0, "BNB": 20.0, "DOGE": 10.0, "HYPE": 15.0
},
"OPEN_DEPTH_ADJUSTED_SIZING_ENABLED": true,
"OPEN_MAX_DEPTH_FRACTION": 0.005,
"MAX_PER_COIN_NOTIONAL_USD": 50.0
```

**Cooldown design note:** `OPEN_MARKET_COOLDOWN_SECONDS = 300` prevents re-entry in the same coin for 5 minutes (one full bucket). If an Open position exits early via stop-loss at T+30s, the cooldown should expire at bucket end, not 300 wall-clock seconds from exit. **Implementation:** Track cooldown as `(coin, bucket_start_ts)` keyed per-coin-per-bucket rather than a rolling wall-clock timer. This allows re-entry in the *next* bucket (at T+300s from bucket open) while still preventing double-entry within the same bucket.

### 2.6 Risk Controls

**Combined exposure:** The Open strategy should share the existing `risk.py` exposure limits. A position open via Open and a Momentum position in the same coin at different bucket stages is acceptable (they operate in different TTE regimes) but the combined notional must stay within per-coin exposure limits.

**Per-coin notional cap:** `MAX_PER_COIN_NOTIONAL_USD = 50` is shared between Open and Momentum for the same coin. If an Open position of $30 is active in BTC, a Momentum entry in BTC is capped at $20, not its normal `MOMENTUM_MAX_POSITION_USD`. The risk module must sum Open + Momentum notionals per coin at entry time — not just count positions.

**NO entries are the primary regime:** 62.3% win rate (n=138) vs 60.2% for YES (n=166). The NO regime has higher sample count and higher EV. Budget more of the Open strategy's coin cap to NO entries in high-funding windows.

**SL philosophy is identical to Momentum:** Oracle-driven, not CLOB-driven. The CLOB YES bid will naturally collapse for a losing position — do not use that collapse as the primary SL trigger. Use the CL oracle delta SL as the hard floor, and cl_upfrac + PM bid as the early warning system.

**GTD hedge cost for 5-minute holds:** The GTD hedge applies per preamble rules and must not be removed. However, its premium (typically 2–3% of notional) directly erodes Open strategy edge. At the 60.2% YES win rate case, EV/trade is approximately +2.9% before costs — a 2.5% hedge cost reduces this to +0.4%, near slippage break-even. **Action:** Measure the actual GTD hedge cost on the first 10 paper trades. If cost exceeds 1.5% of notional, raise `OPEN_ENTRY_MIN_SCORE` to 3 (not 2) to ensure only higher-conviction entries pass. The hedge stays on — but the score gate must account for its cost.

**Correlated direction cap:** BTC, ETH, SOL, BNB, and HYPE are co-driven by macro events (equity risk-off, Fed announcements, crypto-wide sentiment shifts). Two simultaneous YES positions in BTC and ETH are not two independent coin-flip bets — they are one macro bet expressed in two vehicles. On a macro reversal, both lose simultaneously. The existing `OPEN_MAX_CONCURRENT: 2` limit does not fully protect against this: the two concurrent positions could both be from this correlated group in the same direction. Add a **correlated group cap**: at most one YES position (and separately, at most one NO position) may be active from the group {BTC, ETH, SOL, BNB, HYPE} at any time. DOGE and XRP have lower correlation with this group and may run independently.

```json
"OPEN_MAX_CORRELATED_DIRECTION_POSITIONS": 1,
"OPEN_CORRELATED_COIN_GROUPS": [["BTC", "ETH", "SOL", "BNB", "HYPE"]]
```

**Risk module implementation:** At entry evaluation, count active Open positions with the same `direction` (YES/NO) among coins in the same correlated group. If count ≥ `OPEN_MAX_CORRELATED_DIRECTION_POSITIONS`, skip the entry — even if the entry score passes and `OPEN_MAX_CONCURRENT` is not yet reached.

---

## Part 3 — What NOT to Change

These are things the analysis might tempt you to change — don't:

### 3.1 Do NOT Use HL Mark vs CL Basis as a Hard Gate

`hl_mark_vs_cl_bps` (HL perpetual premium to CL oracle) has AUC 0.5191 and p=0.384 — **not statistically significant**. The feature appears in the RF importance table because the RF found spurious correlations in this single-day dataset. Do not add a gate on this signal until validated on 30+ days.

**On the independence argument:** A valid counter-point is that HL mark vs CL basis is **independent** of PM CLOB signals (different venue, different data source), so even a modest AUC could add information in a composite score. However, independence does not overcome statistical significance. A p-value of 0.384 means a 38% probability the observed AUC is sampling noise — adding a randomly-noisy independent signal to a composite score contributes as many false positives as true positives, netting near zero. The CL velocity component already in §2.2 captures the same directional information from CL with higher signal-to-noise. Revisit HL vs CL basis only if a 30-day dataset produces p < 0.05 for this feature.

### 3.2 Do NOT Change the 80–90c Momentum Price Band

The analysis data is about T=0 entries (market open at ~50c). It does not directly validate changes to the near-expiry Momentum band. The 80–90c band is empirically validated by months of Momentum strategy live trading. The improvements in Part 1 improve the quality of 80–90c entries — they do not change what tokens are targeted.

### 3.3 Do NOT Remove the GTD Hedge From Open Entries

It is tempting to skip the GTD hedge for Open positions given the 5-minute hold window is long enough to see the oracle direction clearly. However, the preamble rule is explicit: the GTD hedge must stay on all positions. The hedge accounting logic in `risk.py` assumes it is always present. A loss scenario that takes the full 5 minutes to materialize (gradual oracle decline from T=0 to T=250s) is exactly when the hedge pays off. Keep it.

### 3.4 Do NOT Use pm_divergence_preopen or NO Bid Drift as Entry Signals

These have AUC 0.521 and 0.500 respectively — effectively random. The pre-open PM CLOB dynamics carry no directional information. The **static** PM CLOB structure at T=0 (depth share, mid-price lean) does carry signal. Do not conflate the two.

### 3.5 Do NOT Try to Trade the Arb Gap

Every single window in the dataset had combined cost > $1.00. There is no arbitrage opportunity in buying both YES and NO. The combined cost ranges from $1.01 to $1.17. Any arb-scanning logic would find zero trades. This is not a viable strategy on these 5-minute binary markets.
### 3.6 Do NOT Loosen the Opening Neutral Combined Cost Gate

The current `OPENING_NEUTRAL_COMBINED_COST_MAX = 1.01` sits at approximately the 5th percentile of the dataset. That is the correct level — it admits only windows with nearly-fair combined pricing. Loosening to 1.015 or 1.02 (the median) would admit windows where the combined entry cost has already consumed most of the $0.35 loser-recovery edge. At combined cost of $1.02, the net P&L on the pair is `$1.00 (winner) + $0.35 (loser) − $1.02 (entry) = +$0.33`, versus `+$0.35` at $1.00 entry. Not a disaster, but the gate exists precisely to avoid crowding out the edge. Keep it tight.

### 3.7 Do NOT Treat the n=681 Dataset as 681 Independent Observations

Windows for BTC, ETH, SOL, BNB and HYPE at the same UTC timestamp are co-driven by the same macro event. A single equity risk-off move at, say, 15:30 UTC hits all 5 simultaneously. The effective independent sample size is much smaller than 681. Two practical consequences:
- Do not run 7 concurrent Open strategy positions at once — the correlation is not zero and simultaneous losses are possible.
- The per-coin win rates (BTC 62%, ETH 48%) may primarily reflect that day's coin-specific news, not structural edges.

### 3.8 Do NOT Assume Zero Slippage on PM Entries

All EV calculations in REPORT.md use mid-price or best-ask as the fill price. In thin PM order books (e.g. DOGE, HYPE), a $30 market entry can move the ask by 2–5 cents. At a mid-price of 0.50 with a 3c adverse fill, the effective cost is $0.53 — raising the break-even from 52.7% to ~54.5%. The 60.2% and 62.3% win rates cited in §2.2 still survive this, but the 52.9% YES win rate in "low vol + positive TWAP" (§1.6) does not. Before live deployment:
- Measure actual fill prices vs best-ask in paper trading for each coin.
- Add a per-coin `OPEN_SLIPPAGE_COST_BPS` estimate to the entry score minimum calculation.
- Do not enter DOGE or HYPE with Open strategy until slippage is measured.

---

## Part 4 — Implementation Priority Order

| Priority | Change | Effort | EV Impact | Risk |
|----------|--------|--------|-----------|------|
| **P0** | Update `MOMENTUM_DELTA_SL_PCT_BY_COIN` per-coin values | 10 min | Immediate loss reduction on DOGE/HYPE | None |
| **P0** | Add `MOMENTUM_FUNDING_GATE_ENABLED` and implementation | 30 min | Blocks negative-EV entries in wrong funding regime | Low |
| **P0** | Add `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD` gate (0.15) | 15 min | Blocks cold-book entries; faster loser discovery | None |
| **P1** | Add `MOMENTUM_HOUR_Z_MULTIPLIER` | 20 min | Blocks worst hours for YES at 16:00/18:00 UTC | None |
| **P1** | Add `MOMENTUM_DEPTH_SHARE_GATE_ENABLED` | 45 min | Confirms crowd direction agreement | Low |
| **P1** | Add Opening Neutral asymmetric resting sells (§0.3) | 45 min | Faster loser exit; earlier Momentum handoff | Low |
| **P1** | Add Opening Neutral depth share loser confidence (§0.4) | 30 min | Further fill-probability optimisation | Low |
| **P2** | Add `MOMENTUM_UPFRAC_EXIT_ENABLED` with EWMA smoothing | 1 hour | Better exit timing; avoids whipsaw | Low |
| **P2** | Add `MOMENTUM_TWAP_GATE_ENABLED` | 1 hour | Blocks low-vol sell-off entries | Low |
| **P3** | Add `MOMENTUM_STREAK_BIAS_ENABLED` for DOGE | 1 hour | Small positive EV on DOGE | Low |
| **P4** | Scaffold `Open` strategy class | 4 hours | New revenue source (separate from Momentum) | Medium |
| **P5** | Integrate `Open` strategy with signal computation | 3 hours | Full Open strategy operational | Medium |
| **P5** | Paper trade `Open` strategy for 5 days | — | Validate before live capital | — |

**Total P0–P1 effort: ~90 minutes.** All P0/P1 changes are config-gated, instantly reversible, and require no changes to the core execution path — only new filter checks before `_enter_position()` is called.

---

## Part 5 — Validation Plan

Before enabling any of these gates on live capital:

1. **Backtest P0–P1 gates against existing `data/trades.csv`**: Run the funding gate and hour multiplier against all historical Momentum fills. Any fill that would have been blocked — check its actual outcome. Target: blocked fills should have <50% win rate (i.e., they were correctly excluded).

2. **Paper trade Open strategy for 5 days** before first live entry. Confirm:
   - Score ≥ 2 filter fires on ~15–25% of windows (expected based on n distribution)
   - Actual win rate in paper ≥ 55% (above median-spread break-even of 52.7%)
   - No execution edge cases (fill latency, book depth at T=0, market cooldown collisions)

3. **Single-day data caveat:** All findings come from May 1, 2026. The hour-of-day effects could reflect that specific day's bearish afternoon rather than a structural pattern. Run the hour multiplier for 2 weeks, log would-have-been entries, then review before hardening the multipliers.

---

*Data source: REPORT.md | Dataset: 20260501T112909 | Analysis: scripts/analyze_deep_signals.py, scripts/analyze_dual_clob.py*

---

## Part 6 — Evaluation of Proposed New Strategies

A second AI proposed five additional strategies derivable from this dataset. This section evaluates each and records the decision.

### 6.1 YES/NO Pairs Trade (Ratio Mean Reversion) — REJECT

**Proposal:** At open, compute `YES_bid / NO_bid`. If it deviates >1.5× rolling IQR, short the overvalued token and long the undervalued one. Hold 30–90 seconds for ratio convergence.

**Why rejected:**
- Polymarket does not allow naked short sales. Selling YES requires holding YES tokens to close. Selling NO requires holding NO. You cannot establish a net short on either leg without first buying it.
- YES + NO converge to 1.00 **at settlement**, not intraday. A ratio imbalance at T+30s can persist until T+300s or widen further as one side trends to winning.
- "Rolling IQR from prior windows" requires historical ratio data we don’t have from a single day.
- The fundamental binary constraint does not create intraday mean reversion — it creates terminal binary resolution.

### 6.2 Pre-Open HL Oracle vs CL Basis + Delta Hedge — PARTIALLY REJECTED

**Proposal:** At T−30s to T−10s, if HL oracle > CL by >0.05%, buy YES at T=0 and simultaneously short HL perp to isolate binary convexity.

**Why partially rejected:**
- The signal (`hl_oracle_vs_cl_bps`, AUC 0.565) is already incorporated in the Open strategy entry score (§2.2). No additional implementation needed.
- The simultaneous HL perp short creates a cross-venue delta hedge that is architecturally complex: the perp delta changes continuously as the binary option's implied delta shifts, requiring dynamic hedging. This is a separate strategy requiring its own position management, order routing, and cross-margin accounting. Out of scope.
- The premise “HL oracle leads CL in low-latency regimes” is unvalidated at the required precision (sub-10s lead time). The 0.565 AUC at T=0 does not establish a directional lead-lag relationship.

### 6.3 Per-Coin Streak Carry (DOGE + XRP Standalone) — REJECT

**Proposal:** Trade DOGE mean-reversion and XRP continuation after 2-streak outcomes, sized at 1–2× base, full 5-min hold.

**Why rejected:**
- Already covered as a **modifier** to existing strategies in §1.5 (Momentum) and §2.2 (Open score). Adding it as a standalone strategy duplicates infrastructure for marginal gain.
- n for specific streak patterns within a single 8-hour session is 10–20 events per pattern. The Sharpe claim (ρ≈0.03 between DOGE and XRP, higher combined Sharpe) cannot be validated from n<20 each.
- Defer standalone consideration until 30+ days of streak data is available.

### 6.4 Real-Time Up-Frac Scalper (Enter Neutral, Scale in on First-60s Signal) — REJECT

**Proposal:** Enter at T=0 with no directional view, then scale into YES or NO based on `upfrac_5s` over the first 60 seconds.

**Why rejected:**
- The justification cites `cl_upfrac_during` AUC of 0.70 — but this is the **full 5-minute** cl_upfrac, computed with the complete outcome window. It is forward-looking relative to a T+5s entry decision. The partial `upfrac_5s` AUC after only 5–60 seconds of a 5-minute window is unknown and almost certainly much lower (closer to 0.55–0.60 at best).
- The proposal amounts to "enter blind, then decide direction" — which is worse than the Open strategy's entry score (§2.2), which already has directional conviction at T=0 before entering.
- Implementing a "neutral entry then scale" requires a separate order management path, an initial position that is immediately at risk, and a flip mechanism. More complex than necessary.

### 6.5 Liquidity Provision on Thin CLOB Side — REJECT

**Proposal:** When YES depth share > 75%, place limit buy orders on NO at 0.30 or lower, wait for NO to rebound to 0.45+.

**Why rejected (logic error):** The data shows YES depth share > 75% → **YES wins 60% of the time** → NO goes to **$0.00** at settlement (not $0.45). Buying NO at 0.30 and expecting a "liquidity rebound" to 0.45 would lose approximately 60% of trades at final settlement. The mean reversion the proposal expects does not exist for binary markets that resolve to 0 or 1. This is not a thin-book liquidity rebound — it is the crowd correctly pricing a losing token down to zero.

### 6.6 Evaluation Summary

| Proposal | Decision | Reason |
|---------|----------|--------|
| YES/NO pairs trade | **Reject** | No short mechanism; convergence at settlement only |
| HL oracle + delta hedge | **Signal already covered; hedge rejected** | AUC 0.565 in Open score; perp hedge out of scope |
| DOGE/XRP streak standalone | **Reject — use §1.5 modifier instead** | n<20 per pattern; duplicate infrastructure |
| Real-time upfrac scalper | **Reject** | AUC 0.70 is forward-looking; partial upfrac unknown |
| Liquidity provision on thin side | **Reject — logic error** | High depth share → that side wins; thin side goes to 0 |

---

## Part 7 — New Potential Strategies (Pipeline)

These are strategies that are **not ready to implement today** but are grounded in signals already present in the dataset and worth developing as forward data accumulates. Each entry specifies what additional data or validation is needed before they become buildable.

---

### 7.1 Mid-Bucket Entry (T+60s Regime)

**Concept:** The current strategy space has two entry regimes — T=0 (Open) and T−30s to T−120s (Momentum). There is an unexploited gap: entering at T+45s to T+90s, after the oracle direction has established itself but well before the near-expiry spread compression. The token would be priced at approximately 0.55–0.70c for the winning side.

**Why this might work:** `cl_upfrac_during` has AUC 0.703 — but this is computed over the full 5 minutes. After 60 seconds of a 5-minute window, approximately 20% of all CL ticks have occurred. If the partial `cl_upfrac_0_to_60s` AUC is even 0.62–0.65, a T+60s entry at 0.60c has both directional confirmation (from the first minute's oracle stream) AND remaining time-decay compression (4 more minutes).

**What's needed:**
- Compute `cl_upfrac_first_60s` from the existing stream data and measure its AUC against outcome.
- Measure the token bid price distribution at T+60s (currently only measured at T=0, T+1s, T+5s, T+10s, T+30s in the dual CLOB analysis).
- If partial-upfrac AUC > 0.60 and median token price at T+60s is 0.55–0.65c, this becomes a viable third entry regime.

**Risk:** Token at T+60s is already partially priced-in. If it's 0.65c, the remaining EV from 0.65c to 1.00c is $0.35 — still meaningful, but the PM crowd has already priced some of the signal away.

---

### 7.2 Cross-Coin Correlated Entry (Basket Signal)

**Concept:** BTC, ETH, SOL, BNB, and HYPE are co-driven by macro. When **three or more** of these coins simultaneously show the same funding-direction signal, the macro regime is confirmed independently, and the probability that all signals are noise simultaneously drops sharply.

**Why this might work:** The §3.7 cross-coin correlation problem (681 overstates independence) cuts both ways. If correlated positions are risky when they all go wrong, they are also more informative as a confirmation cluster when they all agree. A basket entry — `COMPOSITE_MACRO_SIGNAL = count of coins where funding agrees with Open entry direction` — could gate the Open strategy's score: require `COMPOSITE_MACRO_SIGNAL >= 3` for higher-conviction entries.

**Implementation sketch:**
```python
macro_yes_count = sum(1 for coin in TRACKED_COINS if funding[coin] < -THRESHOLD)
macro_no_count  = sum(1 for coin in TRACKED_COINS if funding[coin] > +THRESHOLD)

# Add to Open strategy entry score:
if direction == "YES" and macro_yes_count >= 3:  SCORE += 2
if direction == "NO"  and macro_no_count  >= 3:  SCORE += 2
```

**What's needed:**
- Per-coin funding rates at each T=0 in the dataset (already in `analysis_features_deep_20260501T112909.csv`).
- Cross-tabulate: when 3+ coins agree on funding direction, does the target coin's win rate rise above the single-coin rate?
- This analysis takes ~30 minutes to run against the existing CSV.

**Risk:** On a single-day dataset, 3-coin funding agreement may reflect the entire day's regime rather than individual window edges. Specifically: if all 7 coins are in positive-funding regime all day, then "3+ coins agree on NO" is always true and adds no independent information — it just amplifies the existing single-coin funding signal. **Before using this:** Cross-tabulate from the existing CSV: compare the target coin's win rate when (a) only that coin's funding signal qualifies vs (b) 3+ coins agree on the same direction. If the win rate difference is < 3 percentage points, the basket signal adds no independent value over the single-coin gate and should be dropped entirely.

---

### 7.3 Opening Neutral — Survivor Leg Size-Up on Transition to Momentum

**Concept:** When the Opening Neutral loser leg fills at $0.35, the surviving winner leg transitions to Momentum. Currently this leg is sized at `OPENING_NEUTRAL_SIZE_USD = $1`. But at that moment, the bot has strong directional information: the loser has confirmed which direction is losing, and the winner is priced at ~0.55–0.70c (not yet near 1.00c). The Momentum strategy itself would accept this as an entry.

**Why this might work:** The Momentum handoff from Opening Neutral is currently a passive transition — just continue holding. Aggressively sizing up on the winner at the transition point would be equivalent to a T+30s to T+90s Open-style entry with the additional confirmation of having seen the loser leg fill. The directional signal is stronger than at T=0 (we have actual trade flow as a signal, not just funding).

**Implementation sketch:**
```python
# In _on_loser_fill():
if self.config.OPENING_NEUTRAL_SIZE_UP_ON_TRANSITION:
    additional_size = self.config.OPENING_NEUTRAL_TRANSITION_ADD_USD
    # Market-buy additional winner tokens at current ask
    self._enter_momentum_extension(winner_token, additional_size)
```

**What's needed:**
- Measure the winner token price distribution at the time of loser fill (i.e., when does the loser first hit $0.35 after open?). The current analysis has T+30s prices — loser fill likely occurs within 15–45s of open in most windows.
- Validate that winner token price at T+30s is still below 0.75c (Momentum's upper price band) — if so, a size-up qualifies as a normal Momentum entry.
- Backtest: do windows where YES_bid > 0.55 at T+30s (i.e., winner is not yet near settlement) have a higher final win rate? The answer is almost certainly yes, since by T+30s the loser has confirmed.

**Config keys (when ready):**
```json
"OPENING_NEUTRAL_SIZE_UP_ON_TRANSITION": false,
"OPENING_NEUTRAL_TRANSITION_ADD_USD": 5.0
```

**Risk:** Size-up adds directional exposure at a point when the bot has already committed $1 per leg. Requires its own risk limit separate from `OPENING_NEUTRAL_MAX_CONCURRENT`. Keep disabled until validated.

---

### 7.4 Intra-Session Volatility Regime Switching

**Concept:** The dataset shows vol regime (high/low relative to rolling median) changes the meaning of every other signal. Currently the TWAP gate (§1.6) hard-codes a single vol-median threshold computed from the analysis dataset. In production, the vol median should be computed rolling from the current session's data, and the strategy should explicitly track whether it is in a high-vol or low-vol regime — switching its signal weights accordingly.

**Why this matters:** On a low-vol day (all windows below the single-session median from May 1), the static vol threshold classifies everything as "low vol" and misapplies the high-vol weights. A self-calibrating regime classifier avoids this.

**Implementation sketch:**
```python
class VolRegimeTracker:
    def __init__(self, window=20):
        self.buffer = deque(maxlen=window)  # last N realised-vol readings
    
    def update(self, vol_60s):
        self.buffer.append(vol_60s)
    
    @property
    def regime(self):  # 'HIGH' | 'LOW' | 'UNKNOWN'
        if len(self.buffer) < 5: return 'UNKNOWN'
        median = sorted(self.buffer)[len(self.buffer)//2]
        current = self.buffer[-1]
        return 'HIGH' if current > median else 'LOW'
```

When `regime == 'UNKNOWN'` (session just started, < 5 data points), apply no vol-dependent gates — only use funding and depth share gates which don't require a vol baseline.

**What's needed:** No new data — this is a code change only. The `VolFetcher` class already maintains a rolling vol buffer; adding a `regime` property is trivial. Can be implemented as part of the P2 `MOMENTUM_TWAP_GATE_ENABLED` work.

**Cross-session calibration (future extension):** The design above calibrates the regime threshold within a session (rolling last 20 buckets). On the first 5 buckets of a new session, `regime = 'UNKNOWN'`. A better long-term design: persist the previous session’s median vol to the state file and use it as the initial baseline for the new session. This eliminates the UNKNOWN gap entirely and handles days that are uniformly high-vol or low-vol (where intra-session calibration classifies everything as ‘MEDIUM’ because the within-session distribution is narrow). Requires persisting a single `prev_session_vol_median` float across restarts — trivial to add to the existing state file.

---

### 7.5 Latency Exploitation: PM Repricing Lag After CL Update

**Concept:** `hl_oracle_vs_cl_bps` has AUC 0.565 — modest, but the mechanism suggests a higher-frequency edge: when CL publishes a new round (every ~1–4 seconds on Polygon), the Polymarket CLOB reprices with a lag. During this lag window (estimated 0.5–3 seconds), the YES/NO token prices reflect the *old* CL price. An entry into the now-correctly-priced direction captures the crowd's repricing move.

**Why this is different from the Open strategy's use of CL/HL basis:** The Open strategy uses the pre-open CL/HL basis as a 300-second directional signal. This strategy uses the *intra-bucket* CL update as a 1–10 second repricing signal. Much shorter timeframe, much higher frequency.

**What's needed:**
- Latency measurement: time-stamp every CL tick and every PM CLOB tick and measure the empirical lag distribution. The stream data from `data/market_data/` may already contain enough timestamps to compute this.
- If the median PM repricing lag > 0.5s, the edge is exploitable. If < 0.3s, the network latency to execute an order likely exceeds the window.
- This strategy requires **sub-second order placement** — the PM REST API latency must be measured. If PM orders take > 500ms round-trip, this strategy is not executable regardless of signal quality.

**Risk:** Latency arbitrage is an arms race. Any advantage here erodes as PM's price-feed latency improves or other bots start exploiting the same lag. Do not build significant infrastructure around this until latency advantage is confirmed and stable.

---

### 7.7 Tail Hedge — Cheap NO Limit as Insurance on YES Entries

**Concept:** Instead of the rejected YES/NO pairs trade (§6.1, which requires short-selling), use a one-sided limit buy: when entering YES via the Open strategy, simultaneously place a GTC limit buy order for NO at a very low price (e.g., $0.10). If YES wins as expected, the NO order simply sits unfilled (negligible cost). If YES unexpectedly loses, the NO order fills at $0.10 and pays out $1.00 at settlement — a 10× return that offsets most of the YES loss.

**Why this is different from the pairs trade:** It does not require shorting. It does not require the YES+NO ratio to mean-revert intraday. It is a pure tail hedge that exploits the binary settlement structure.

**P&L math:**
```
YES position: $20
Tail hedge: GTC limit buy NO at $0.10 for $2 (10% of YES position)

Scenario A — YES wins (60% probability):
  YES pays $20 / 0.60 * 1.00 = full payout
  NO order sits unfilled, GTC cancelled at exit
  Net: normal YES profit

Scenario B — YES loses (40% probability):
  YES: -$20 loss
  NO fills at $0.10, pays $1.00 at settlement: +$18 profit
  Net: -$20 + $18 = -$2 (vs. -$20 without the hedge)
```

**The hedge costs ~$0.06 in expected value** (40% chance of NOT needing it × $0.10 order cost + order fees) but reduces maximum loss from $20 to $2. At 60% win rate, this converts a -40% drawdown tail to a -10% drawdown, at the cost of approximately 0.3% of position notional per trade.

**Config keys:**
```json
"OPEN_TAIL_HEDGE_ENABLED": false,
"OPEN_TAIL_HEDGE_PRICE": 0.10,
"OPEN_TAIL_HEDGE_SIZE_FRACTION": 0.10
```

**What's needed:** Measure whether GTC limit orders at $0.10 are regularly filled by PM market structure (i.e., does PM accept resting orders at deep-out-of-the-money prices?). The fill price of $0.10 may be below PM's minimum tick or may not attract any counterparty in the first minutes of a 5-min bucket. Validate this on paper first.

---

### 7.8 Pre-Open Drift Divergence as Position Size Signal (Not Direction Signal)

**Concept:** The document correctly found that pre-open PM drift has no directional AUC (0.521). But the *magnitude* of divergence between YES and NO bid drifts could signal the oracle will move significantly in *some* direction — useful for scaling position size, not for picking direction.

**Rationale:** A large pre-open divergence (e.g., YES bid +2c, NO bid −2c pre-open) suggests informed trading is occurring before open. The oracle move at T=0 is likely to be larger than average, which means the directional signal (from funding/depth share) generates more EV than on a quiet open.

**Implementation sketch:**
```python
abs_divergence = abs(yes_preopen_drift_bps - no_preopen_drift_bps)

# Use as a size multiplier, not a direction signal:
if abs_divergence > OPEN_DIVERGENCE_SIZE_THRESHOLD:
    position_size *= OPEN_DIVERGENCE_SIZE_MULTIPLIER  # e.g. 1.25
```

**What's needed:** Compute `abs(yes_preopen_drift - no_preopen_drift)` from the existing dataset and correlate with `abs(cl_close - cl_open)` (magnitude of oracle move). If Spearman correlation > 0.3, the feature has sizing utility. If < 0.15, drop the idea. This is a 30-minute analysis against the existing CSV.

**Config keys (when ready):**
```json
"OPEN_DIVERGENCE_SIZE_ENABLED": false,
"OPEN_DIVERGENCE_SIZE_THRESHOLD_BPS": 50,
"OPEN_DIVERGENCE_SIZE_MULTIPLIER": 1.25
```

---

### 7.9 Order Book Imbalance Velocity as Early Exit Signal

**Concept:** The document uses YES depth share as a static snapshot at T=0. But the **rate of change** of depth share in the first 10 seconds may be more informative: if YES depth share was 60% at T=0 and drops to 45% by T+10s, informed sellers are exiting the YES side — a strong bearish signal that is available within the trade itself.

**Why this is different from the static depth share gate (§1.3/2.2):** The static gate filters entry using pre-open data. This uses post-entry data (the first 10s of live trading) as a dynamic early exit trigger. The two are complementary.

**Implementation sketch:**
```python
# Sample depth share every 2s for first 10s post-entry
depth_samples = []  # [(t=0, ds=0.60), (t=2, ds=0.57), ...]
if len(depth_samples) >= 4:
    slope = linregress([t for t,_ in depth_samples],
                       [ds for _,ds in depth_samples]).slope  # ds per second
    # For a YES position:
    if slope < -OPEN_DEPTH_VELOCITY_EXIT_THRESHOLD:  # e.g. -0.02 per second
        # YES depth share collapsing — exit early
        self._exit_position(reason='depth_velocity_exit')
```

**What's needed:** The existing tick data captures depth share at T=0 and T+30s (not every 2s). This signal requires live data collection in the first 10s of each bucket to validate. Add 2-second depth-share sampling to the WS event handler and collect 2 weeks of data before evaluating.

**Config keys (when ready):**
```json
"OPEN_DEPTH_VELOCITY_EXIT_ENABLED": false,
"OPEN_DEPTH_VELOCITY_SAMPLE_INTERVAL_S": 2,
"OPEN_DEPTH_VELOCITY_EXIT_THRESHOLD": 0.02
```

---

### 7.10 Per-Bucket Dynamic Stop-Loss Width

**Concept:** The current per-coin SL percentages (BTC 0.03%, DOGE 0.08%) are static calibrations from the May 1 dataset. In practice, oracle volatility varies significantly across days. On a calm macro day, DOGE’s 60-second realised vol might be 0.04%, making a 0.08% SL overly generous. On a volatile day, BTC’s vol might be 0.08%, making a 0.03% SL a near-certain exit on any non-trivial tick.

**Recommended approach:** Compute the 60-second realised vol of CL in the **previous bucket** for that coin, and set SL as:

```python
prev_bucket_vol_pct = compute_realised_vol_60s(prev_bucket_cl_ticks)
static_floor = MOMENTUM_DELTA_SL_PCT_BY_COIN[coin]  # existing per-coin floor
dynamic_sl = max(static_floor, 0.5 * prev_bucket_vol_pct)
```

This adapts to changing market conditions: quiet periods use tighter SL (capped at the static floor), volatile periods widen automatically.

**Example:** If BTC had 0.10% realised vol in the previous bucket (very volatile), `dynamic_sl = max(0.03, 0.05) = 0.05%`. Normal BTC vol of 0.04% gives `max(0.03, 0.02) = 0.03%` (static floor holds).

**Implementation:** The `VolFetcher` already maintains a per-coin rolling vol buffer by bucket. This is a 3-line addition to the SL computation at position entry.

**Config keys:**
```json
"MOMENTUM_DYNAMIC_SL_ENABLED": false,
"MOMENTUM_DYNAMIC_SL_PREV_VOL_FRACTION": 0.5,
"OPEN_DYNAMIC_SL_ENABLED": false,
"OPEN_DYNAMIC_SL_PREV_VOL_FRACTION": 0.5
```

---

### 7.12 Pre-Strike Entry Window — Pre-Open Accumulator for Opening Neutral

**Concept:** Before each 5-minute bucket opens, there is a brief window (estimated 30–90 seconds) during which PM tokens are tradeable but the strike price has not yet been fixed. If PM CLOB dynamics in this window price tokens at a lower combined cost than at T=0 (wider spreads, softer books, fewer informed participants), an earlier entry would pay less for the same pair — capturing more of the $0.35 loser-leg recovery edge before the market crowds in.

**Why this could be the highest-EV Opening Neutral improvement:** If combined cost at T−30s is $1.00 vs $1.02 at T=0 on the same window, pre-strike entry captures $0.02 more per pair. At $1/leg that is a 2% improvement — roughly equivalent to one standard deviation of the current edge. The resting sell at $0.35 remains the same; only the entry cost is lower.

**Why it might not work:** Pre-open books may be *thinner* than at T=0 (fewer market makers), leading to wider spreads and higher combined costs. The crowd may price the strike uncertainty as additional premium. Pre-open fill latency is also harder to control precisely when the bucket open timestamp is the synchronisation anchor.

**What's needed:**
- Modify the PM CLOB WS handler to start recording book state from T−60s before each bucket open (currently starts at T=0)
- Collect 1 week of pre-open CLOB data (combined cost at T−60s, T−30s, T−10s, T=0 for the same windows)
- If median combined cost at T−30s is < $1.01 (below the existing T=0 gate threshold), pre-strike entry is viable
- If median combined cost at T−30s is ≥ $1.02 (same as or worse than T=0), the idea does not improve entry cost — abandon

**Config keys (when ready):**
```json
"OPENING_NEUTRAL_PRE_OPEN_ENTRY_ENABLED": false,
"OPENING_NEUTRAL_PRE_OPEN_ENTRY_SECONDS": 30
```

**Risk:** Pre-open entries commit capital before the strike is fixed. If CL is at 100.00 pre-open and the strike is fixed at 100.05 at T=0, a YES token entered at $0.50 is immediately slightly out-of-the-money. Size at normal ON levels ($1/leg) — do not scale up until the combined cost advantage is empirically confirmed.

---

### 7.13 Dynamic Kelly Sizing — Composite Score to Position Size Mapping

**Concept:** The Open strategy entry score predicts win rate, but all entries above `OPEN_ENTRY_MIN_SCORE` currently use the same flat per-coin size cap. A fractional Kelly mapping would allocate more capital to high-conviction entries (Score ≥ 4) and less to marginal passes (Score = 2), improving capital efficiency.

**Kelly formula for binary markets:**
```
Kelly_fraction = (p × b − q) / b
where: p = estimated win probability
       q = 1 − p
       b = net payoff per dollar staked (≈ 0.95 at $1.05 combined cost)
```

**Score-to-size mapping (in-sample estimates — must be replaced with forward-validated rates):**

| Score | In-Sample Win Rate | Half-Kelly | Recommended (1/4 Kelly) |
|-------|--------------------|-----------|--------------------------|
| ≥ 4   | ~76%               | ~26%      | ~13% of coin bankroll    |
| 3     | ~65%               | ~14%      | ~7% of coin bankroll     |
| 2     | ~60%               | ~9%       | ~4.5% of coin bankroll   |

In practice, apply as dollar amounts keyed to `OPEN_MAX_POSITION_USD_BY_COIN`:
```json
"OPEN_KELLY_SIZING_ENABLED": false,
"OPEN_KELLY_SIZE_BY_SCORE": {
  "2": 10.0,
  "3": 17.5,
  "4": 25.0
}
```

**Critical prerequisite:** These dollar amounts are derived from in-sample win rates that are likely inflated (see §2.2 in-sample inflation warning). **Do not enable until 30 forward trading days produce per-score-bucket win rate estimates.** After 30 days of paper trading with score logging, compute actual forward win rates per bucket. Apply the Kelly formula to forward rates, multiply by 1/4 (conservative), and update the dollar amounts. If the Score ≥ 4 regime shows only 60% forward win rate (not 76%), the $25 sizing should drop to ~$13.

**What's needed:** Log `(score, direction, coin, outcome)` for every paper trade. 30-day minimum. Then compute per-bucket forward win rates and update the `OPEN_KELLY_SIZE_BY_SCORE` mapping.

---

### 7.14 Dynamic Loser-Leg Exit Price Optimization (Opening Neutral)

**Concept:** The Opening Neutral resting sell on the predicted loser is currently fixed at $0.35 (with minor adjustments from §0.3 and §0.4). A higher-fidelity approach: monitor the real-time speed of the loser's price decline in the first 30–60 seconds, then adjust the resting sell price dynamically. Fast-declining losers can support a higher exit target (more value captured from the collapse). Stalling losers need a lower target to ensure the fill occurs before bucket end.

**Why this matters:** The go/no-go criteria in §0.7 flag a stalling loser as the primary failure mode. A dynamic exit system converts some stalled-loser scenarios into partial recoveries ($0.30 exit instead of $0.00 at settlement).

**Implementation sketch:**
```python
# At T+30s, measure loser price decline rate:
loser_price_t0 = entry_loser_ask        # ~$0.50
loser_price_t30 = current_loser_bid    # current bid at T+30s
decline_rate = (loser_price_t0 - loser_price_t30) / 30.0  # $/second

if decline_rate > OPENING_NEUTRAL_FAST_DECLINE_RATE:   # e.g. >$0.005/s = >$0.15/min
    # Declining fast — raise exit target while loser is still falling
    new_exit = min(loser_price_t30 - 0.03, OPENING_NEUTRAL_LOSER_EXIT_PRICE + 0.05)
elif decline_rate < OPENING_NEUTRAL_SLOW_DECLINE_RATE:  # e.g. <$0.001/s = <$0.03/min
    # Stalling — lower target to maximise fill probability before bucket end
    new_exit = max(OPENING_NEUTRAL_EXIT_FLOOR, OPENING_NEUTRAL_LOSER_EXIT_PRICE - 0.05)
else:
    new_exit = OPENING_NEUTRAL_LOSER_EXIT_PRICE  # no change
# Cancel existing resting sell and replace at new_exit if changed
```

**Config keys (when ready):**
```json
"OPENING_NEUTRAL_DYNAMIC_EXIT_ENABLED": false,
"OPENING_NEUTRAL_FAST_DECLINE_RATE": 0.005,
"OPENING_NEUTRAL_SLOW_DECLINE_RATE": 0.001,
"OPENING_NEUTRAL_FAST_EXIT_PREMIUM": 0.05,
"OPENING_NEUTRAL_SLOW_EXIT_DISCOUNT": 0.05,
"OPENING_NEUTRAL_EXIT_FLOOR": 0.28
```

**What's needed:**
- Measure actual loser token bid prices at T+10s and T+30s during paper trading (PM CLOB WS already provides this)
- Implement cancel-and-replace order logic in the PM order manager (also needed for §0.3 asymmetric sells — bundle these)
- Validate: does raising the exit target on fast-declining losers increase realized recovery vs holding at $0.35?

**Implementation dependency:** Requires the cancel-and-replace order infrastructure. Build this once and share between §0.3 (asymmetric sells), §0.4 (loser confidence tighten), and this feature.

---

### 7.15 Summary — Pipeline Status

| Strategy | Data Requirement | Effort to Validate | Effort to Build | EV Potential |
|---------|-----------------|-------------------|-----------------|-------------|
| Mid-bucket T+60s entry | Compute partial cl_upfrac AUC from existing CSV | 1 hour | 3 hours | Medium |
| Cross-coin basket signal | Cross-tabulate per-coin funding from existing CSV | 30 min | 2 hours | Medium |
| Opening Neutral size-up on transition | Measure winner price at loser-fill time | 1 hour | 2 hours | Low-Medium |
| Intra-session vol regime switching | No new data needed | — | 1 hour | Low (quality improvement) |
| PM repricing lag exploitation | Latency measurement + timestamp analysis | 4 hours | 8 hours | High if viable |
| Tail hedge (YES entry + NO limit) | Validate PM accepts resting $0.10 orders | 30 min | 1 hour | Low-Medium (risk reduction) |
| Pre-open drift divergence as size signal | 30-min analysis on existing CSV | 30 min | 1 hour | Low |
| Depth share imbalance velocity | 2-week live data collection (2s sampling) | 2 weeks | 2 hours | Medium |
| Per-bucket dynamic SL | No new data needed | — | 1 hour | Low (quality improvement) |
| Pre-strike entry window (ON) | 1-week pre-open CLOB data collection | 1 week | 2 hours | High if combined cost < $1.01 |
| Dynamic Kelly sizing | 30-day forward paper trade log per score bucket | 30 days | 2 hours | Medium (capital efficiency) |
| Dynamic loser-leg exit (ON) | Loser price trajectory from paper trading + cancel-and-replace infra | 2 weeks | 3 hours | Low-Medium (failure mode mitigation) |

**Recommended sequencing:** (1) Compute cross-coin basket and drift divergence from existing CSV — 30 minutes each. (2) Validate tail hedge on paper (accepts resting $0.10?). (3) Bundle dynamic SL, vol regime switching, and pyramid-on-upfrac with the P2 TWAP gate work — low effort, co-located. (4) After ON paper trading clears §0.7 go/no-go, collect loser price trajectory data and build cancel-and-replace infrastructure to enable §7.14, §0.3, and §0.4. (5) Start 30-day score-logging for §7.13 Kelly sizing from day one of paper trading. (6) Pre-strike entry (§7.12) and latency arb (§7.5) require the most infrastructure and should be last. (7) Mid-bucket entry (§7.1) becomes viable once cl_upfrac_first_60s AUC is computed.
