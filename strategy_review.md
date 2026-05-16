# Strategy Review — May 2026 (v4)

**Last updated:** May 11, 2026
**Note:** v4 is a ground-up rewrite based on reading the actual config and source. Previous versions
recommended things already live or experimentally invalidated. This version starts from
what is actually built.

---

## 0 — What Is Actually Built (Not Recommendations — Facts)

### Opening Neutral exit gate pipeline (S1, in evaluation order):

| Gate | Config key | State | What it does |
|---|---|---|---|
| Min-hold | `OPENING_NEUTRAL_MIN_HOLD_SECS=45` | Live | Blocks exits within 45s of entry |
| Winner confirm (ON-07) | `OPENING_NEUTRAL_WINNER_CONFIRM_FLOOR=0.65` | Live | Exit suppressed if winner_bid < $0.65 |
| Oracle delta (ON-06) | `OPENING_NEUTRAL_ORACLE_DELTA_GATE_ENABLED=True` | Live | Exit suppressed if oracle says the exiting leg is actually winning |
| Model B (ML-06) | `MODEL_B_ENABLED=false` | Disabled | Probabilistic multi-signal extension of ON-06 |
| Loser confidence tighten (ON-05) | `OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED=true`, tighten=0.02 | Live | Adjusts per-leg exit trigger based on confidence score |

### Momentum exit signal stack:

| Signal | Config | State | Role |
|---|---|---|---|
| Oracle delta SL | `MOMENTUM_DELTA_STOP_LOSS_PCT`, per-coin (BTC/ETH=1%, SOL=1%, DOGE/HYPE=1.5%) | Live | Hard floor — fires on sharp oracle reversal |
| Upfrac EWMA exit (M-13) | threshold=0.38, 4 windows × 5s = 20s sustained reversal | Live | Early exit on gradual oracle deterioration (AUC 0.703) |
| Winner delta SL multiplier | `MOMENTUM_WINNER_DELTA_SL_MULTIPLIER=0.2`, grace=150s | Live | Promoted winners get 5× tighter stop after 150s grace |
| Near-expiry time stop | `MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS=40` | Live | Exits if TTE < 40s and position is losing |
| Delta SL grace period | `MOMENTUM_DELTA_SL_GRACE_SECS=90` | Live | 90s window before delta SL can fire |

### Momentum entry gate stack:

| Gate | Config | State |
|---|---|---|
| Dynamic vol-scaled delta threshold | `MOMENTUM_VOL_Z_SCORE_BY_TYPE` + Deribit/realized vol | Live |
| Absolute minimum delta floor | `MOMENTUM_MIN_DELTA_PCT_BY_COIN`, `MOMENTUM_MIN_DELTA_PCT_BY_TYPE` | Live |
| Funding rate gate | `MOMENTUM_FUNDING_GATE_ENABLED`, thresholds per side | Live |
| Depth share gate | `MOMENTUM_DEPTH_SHARE_YES_MIN=0.25`, `MOMENTUM_DEPTH_SHARE_NO_MAX=0.75` | Live |
| TWAP deviation gate | `MOMENTUM_TWAP_DEV_LOW_VOL_YES_MULTIPLIER=1.6` in low-vol regimes | Live |
| Price band | `MOMENTUM_PRICE_BAND_LOW=0.65`, `MOMENTUM_PRICE_BAND_HIGH=0.92` | Live |
| TTE entry window | `MOMENTUM_MIN_TTE_SECONDS` by bucket type (5m=120s, 15m=180s, 1h=600s) | Live |
| Minimum CLOB depth | `MOMENTUM_MIN_CLOB_DEPTH=125` | Live |
| Kelly negative-EV check | computed at entry | Live |
| Concurrent cap | `MOMENTUM_MAX_CONCURRENT=20` | Live |

This is a comprehensive gate stack. The bot is not thin on signals.

---

## 1 — EV Baselines (Unchanged From v3 — Still Correct)

### Opening Neutral at 75% accuracy

```
EV per pair = 0.2827 + 0.75 - 1.0113 = +$0.021/pair
At $0.35 fill (design target): +$0.089/pair
```

Winner hold is correct at 75%+ accuracy. Concurrent exit is negative EV (-$0.035).
Both these facts are unchanged. The question is: why are fills at $0.28 when the gate
stack (including winner confirm) is live and designed to prevent bad exits?

### Momentum at 85% win rate

```
EV per trade = 0.85 × $0.485 - 0.15 × $1.00 = +$0.262/trade
```

---

## 2 — Opening Neutral: What the Gates Actually Solve (and What They Don't)

### 2.1 Direction Problem vs Fill Quality Problem

The existing gate stack addresses TWO different problems:

**Problem A — Wrong leg exit (exiting the actual winner):**
This is what ON-06 (oracle delta), ON-07 (winner confirm floor), and ON-05 (loser confidence)
collectively solve. If the oracle says spot is above strike, the YES leg should NOT be
exited even if YES_bid has dipped to $0.38. This problem is addressed.

**Problem B — Fill quality on confirmed loser exits:**
Even when all gates pass (oracle confirms loser, winner confirms), the market order to
exit the confirmed loser still fills at a discount. The average trigger is $0.38 and the
average fill is $0.28. This gap is structural — it is not solved by direction-confirmation
gates.

Two mechanisms drive the fill quality gap:
1. **Execution latency** (~100-200ms between WS event and REST call arriving at matcher)
   — During continuous descent, the bid falls another 8-10 cents in this window
2. **Cliff collapse** — loser bid gaps from $0.45 to $0.07 in a single WS update.
   The WS event fires because bid=$0.07 is below $0.38. Winner simultaneously jumps
   to $0.80+ so the bilateral gate passes. Market order fills at $0.07.

Both of these are structural. The bilateral confirmation (ON-07) correctly fires in
both cases — it just cannot recover the fill price.

### 2.2 Reducing Slippage: The One Lever

**Winner-primary trigger does not work.** If winner_bid = $0.78, the loser_bid is already
~$0.22 (YES+NO bids track inversely; combined tracks ~$1.00). A winner-primary trigger
observes the same moment, just from the other side — it does not fire earlier. This
was confirmed structurally: at any winner threshold high enough to constitute a strong
signal, the loser is already below the current trigger level.

**The only lever is `OPENING_NEUTRAL_LOSER_EXIT_TRIGGER`.**

Currently `$0.38`. The market sell fires when bid ≤ $0.38, then 100-200ms of execution
latency causes another $0.08-0.10 drop before the REST call arrives. Fill = ~$0.28.

Raising the trigger fires earlier in continuous descent (80% of exits):

| Trigger | Loser at fire | Expected fill (after latency) | Improvement |
|---|---|---|---|
| $0.38 (current) | $0.38 | ~$0.28 | baseline |
| $0.42 | $0.42 | ~$0.32–0.34 | +$0.04–0.06 |
| $0.45 | $0.45 | ~$0.35–0.37 | +$0.07–0.09 |

**Cliff collapse (20% of exits) is irreducible regardless of trigger level.** The loser
bid gaps from $0.45 to $0.28 (or lower) in a single WS update. The first event seen has
bid already below any reasonable trigger. Resting limit orders don't help — they sit
unfilled when the bid gaps below the limit price without touching it. Market orders
guarantee a fill but at the post-cliff price.

**Tradeoff for raising the trigger:** fires earlier when the loser is less committed —
higher mean-reversion risk. The `OPENING_NEUTRAL_WINNER_CONFIRM_FLOOR` gate is the
guard. At loser=$0.42, winner must be ≥ floor for the exit to proceed. As long as the
floor is set conservatively (≥ 0.65), the pair is sufficiently committed at trigger
time.

**Recommendation:** raise `OPENING_NEUTRAL_LOSER_EXIT_TRIGGER` from $0.38 to $0.42 once
n ≥ 30 exits are available to measure mean-reversion rate at the new threshold. Do not
change without that baseline.

### 2.3 ON-06 Utilization Is Unknown

`OPENING_NEUTRAL_ORACLE_DELTA_GATE_ENABLED=True` with `fallback="allow_exit"`.
In practice: when the oracle is unavailable at exit decision time, the gate falls back
to allow_exit. We do not know what fraction of exits use the oracle vs fall back.
If the oracle is frequently unavailable during the WS bid-monitor path (which runs on
the async WS event loop, not the main scan cycle), ON-06 may be falling back more than
expected and providing less protection than designed.

**Check:** grep bot.log for `"ON-06_oracle_delta"` entries to see the pass/fallback/suppress
split. No code change required — this is a log analysis.

---

## 3 — Momentum: Accept the Structural Constraint

### 3.1 Whipsaws Are Irreducible at Current Infrastructure

The user states this directly: impossible to forecast whipsaws with enough accuracy
without sub-second data feeds and ms reactions based on CEX data.

This is correct. The mechanics of a momentum whipsaw in a 5m binary market:
1. Spot is at $68,900 vs strike $68,300 → YES at $0.87 → entry
2. Binance order book flips (large bid wall pulled, ask wall appears) — this happens in
   sub-100ms and is not visible from Chainlink spot or any on-chain feed
3. Chainlink oracle lags by 1-3 seconds. First update after the flip shows spot at
   $68,250 (below strike) → delta SL fires → loss
4. The position held 60-90 seconds with no adversarial signal, then the flip happened

No gate in the current stack can see step 2. The funding rate does not update at
this frequency. Depth share is computed at entry, not continuously. TWAP deviation
is a slow signal. The upfrac EWMA (M-13) requires 20s of sustained adversarial ticks
to fire — by which point the delta SL may already have fired.

**The upfrac EWMA (M-13) at 4×5s=20s is intentionally slow.** Shortening the window
would increase false exits on noise bounces. This was already tested in TTE-linked
stop tightening experiments (decreases wins). The 20s window is a calibrated choice,
not a gap.

### 3.2 The Correct Response to Whipsaws

At 85% win rate:
- Accept the payoff structure. 2.06 wins to recover each loss is fixed math, not a
  policy choice.
- Do not add new exit mechanisms without CEX CLOB data to validate them against.
  New mechanisms on current infrastructure will either: add false exits (decreasing
  wins, as seen in TTE stop tightening) or be redundant with the existing stack.
- The only lever that improves the loss experience without requiring CEX data is
  ENTRY QUALITY — entering with cleaner directional momentum reduces whipsaw
  occurrence. But the user also notes this cannot be improved without sub-second CEX
  CLOB data. Correct — the entry gates are already consuming everything available from
  current data sources.

**Conclusion: Momentum whipsaws are not solvable at current infrastructure. Do not spend
engineering effort chasing this. Hold the current exit stack and accept the 15% loss rate.**

### 3.3 What CAN Be Validated (Post-Outcome-Reconciliation)

Once the shadow log bug is fixed (Section 4), the following questions become answerable:

1. **Which gate state combinations correlate with whipsaw losses?**
   - Are whipsaw losses clustered in specific funding regimes or depth share states?
   - If yes, tighten the relevant entry gate threshold for that regime
   - If distributed uniformly, the gates are already at their calibration limit

2. **Is the upfrac EWMA threshold (0.38) still optimal in 2026?**
   - Compare upfrac value at whipsaw exit time vs at resolution on winning positions
   - If upfrac is consistently at 0.35 on whipsaws and 0.45 on winners, the threshold
     can be raised (more exits caught early)
   - If upfrac is identical between whipsaws and winners, the signal is not discriminating

3. **Is the winner delta SL multiplier (0.2×) correct for ON-promoted winners?**
   - Promoted winners have different entry context than direct Momentum entries
   - Validate whether 0.2× produces more or fewer false exits on promoted winners vs
     direct entries using the fills CSV pair_id linkage

These are data-driven calibration tasks that require no new infrastructure — only
outcome reconciliation.

---

## 4 — Model A and the Shadow Log Bug

### 4.1 Model A Is Training-Only (No Bug on Fills CSVs)

Model A is not in the execution path. `on_fills.csv.model_a_score` being NaN is correct
and expected — these fills happened without Model A involvement. Nothing to fix here.

### 4.2 The Real Bug: Outcome Reconciliation

All 3,140 shadow_log entries show `actual_outcome = PENDING`. Zero resolved.

This is the single highest-impact fix in the codebase. Without it:
- Model A has no training signal (features logged, no labels → learning nothing)
- Cannot validate gate AUC in 2026 live data (Section 3.3 tasks cannot be done)
- Cannot determine cliff-collapse vs continuous-descent split (Section 2.2 analysis blocked)
- Cannot validate whether ON-06 is actually suppressing wrong exits or falling back

**Fix:** When a position closes (monitor callback, ledger write, or settlement event),
match by `market_id` + `decision_type=entry` in shadow_log and update `actual_outcome`
to `WIN` or `LOSS`. Use the `CLOB API winner flag` as the source of truth per preamble.

This is the only bug fix in this review. Everything else is either working or analytically
blocked by this bug.

---

## 5 — Model B: Reframe as What It Is

Model B exists, is built, and is disabled (`MODEL_B_ENABLED=false`). The model proposal
(analysis/model_proposal.md) describes it as a learned calibration of ON-06 — extending
the binary oracle delta gate to a probabilistic multi-signal gate.

Model B consistently shows scores < 0.5 on exit events, meaning it disagrees with most
loser exits. Two possible explanations:

1. **Model B is correctly detecting wrong-leg exits (25% of exits at 75% accuracy)**
   → Model B would be valuable when enabled. Fix outcome reconciliation first, validate AUC.

2. **Model B is miscalibrated because it trained on PENDING outcomes**
   → Model B has no valid training signal because actual_outcome is always PENDING.
   Every score it produces is based on feature patterns with zero outcome feedback.

Explanation 2 is more likely given the shadow log state. All of Model B's training data
is unresolved. The consistent sub-0.5 scores are not informative — they reflect training
on unlabeled data, not a signal.

**Keep MODEL_B_ENABLED=False. Fix outcome reconciliation first, then retrain, then assess.**

---

## 6 — Priority Ranking (Three Items)

### Priority 1 — FIX: Shadow log outcome reconciliation (2-3 days)

Wire position close path to update `actual_outcome` in shadow_log.csv. Use CLOB API
winner flag as source of truth. This is the only genuine bug in the codebase, and it
blocks everything else in this review.

Expected impact: Model A gains a training signal. Gate validation becomes possible.
Cliff-collapse analysis becomes possible. Model B retraining becomes possible.

### Priority 2 — ANALYSE: Cliff-collapse rate in on_monitor_ticks.csv (1-2 days, no code)

`on_monitor_ticks.csv` is already being written on every WS tick for active pairs.
Compute per-pair `max_tick_to_tick_bid_drop`. If any pair has a drop > $0.10 in a single
tick, classify as cliff-collapse. Compute the fraction of loser exits that were cliff
collapses.

If cliff-collapse rate > 20%: design and implement winner-primary trigger (Section 2.2).
If cliff-collapse rate < 10%: the continuous-descent fill gap is the main cost driver,
and it is mostly irreducible (execution latency ~100-200ms on a descending bid).

This analysis exists in already-collected data. No new collection needed.

### Priority 3 — VALIDATE: Gate AUC re-calibration (3-4 weeks post-reconciliation)

Once outcome reconciliation is running and >= 200 outcomes are resolved:
- Per-gate AUC analysis: does funding_gate, depth_share_gate, TWAP_deviation_gate
  still deliver the expected win-rate improvements in 2026 live data?
- Upfrac EWMA threshold validation: is 0.38 still the correct threshold given 2026 noise?
- ON-06 utilization: what fraction of exits are suppressed by oracle delta vs
  falling back to allow_exit?

Tighten or loosen gate thresholds based on 2026 data, not the backtested calibration.

---

## 7 — What to Hold

Everything in the current gate stack is correct for current infrastructure. Do not change:
- Winner confirmation floor (0.65) — correct and live
- Oracle delta gate (ON-06) — correct and live
- Upfrac EWMA (threshold=0.38, 4 windows) — experimentally validated
- Kelly sizing at 85% win rate — correct
- Winner hold for ON at 75%+ accuracy — correct
- Market order exit for ON loser — correct (resting limits failed, market orders guarantee fill)
- `MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY=false` — correct given near-resolution noise

---

## 8 — Summary

```
WHAT IS TRUE (May 2026):

ON:
  EV at 75% accuracy: +$0.021/pair  (positive — hold winner)
  Fill gap: $0.38 trigger → $0.28 fill  (-$0.10 structural slippage)
  Gate stack: complete (ON-05, ON-06, ON-07 all live)
  Direction errors: addressed by ON-06 + ON-07
  Fill quality: cliff collapse (20%) irreducible; continuous descent (80%) improvable
    by raising LOSER_EXIT_TRIGGER ($0.38→$0.42 = ~+$0.05/continuous exit)
    winner-primary trigger does NOT help — at winner=$0.78, loser is already $0.22
  Unknown: mean-reversion rate at $0.42 trigger (need n≥30 exits), ON-06 suppression rate

MOMENTUM:
  EV at 85% win rate: +$0.262/trade  (positive)
  Exit stack: complete (delta SL + upfrac EWMA + near-expiry stop + winner multiplier)
  Whipsaw fix: not available without sub-second CEX CLOB data — accept structural constraint
  Unknown: gate AUC in 2026 live data (need outcome reconciliation)

MODEL A:
  Status: training-only, varied scores (0.51-0.77)
  Only bug: actual_outcome never reconciled (all PENDING) — Priority 1
  Fills CSVs: NaN is correct and expected (not in execution path)

MODEL B:
  Status: disabled
  Likely state: miscalibrated due to PENDING training data
  Action: retrain after outcome reconciliation

THE ONE FIX: Shadow log outcome reconciliation (Priority 1)
All other improvements are analytical tasks that require this fix first.
```

---

*Last revised: May 11, 2026*
