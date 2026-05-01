# Strategy Analysis — MM Pricing Intelligence & Adaptation

## What We Now Know About Market Structure

Institutional MMs on Polymarket 5m bucket markets are **CEX CLOB-anchored price makers**, not
reactive participants. Their workflow:

1. Stream the **CEX order book** (Binance/Bybit perp) in real-time — not just last price, but
   full depth: bid/ask levels, resting order sizes, iceberg orders, order flow imbalance
2. Compute a fair-value probability for the upcoming 5m candle outcome (UP or DOWN) from that
   CLOB structure — a thin ask side / large bid wall = price likely to go UP
3. Pre-load the Polymarket CLOB with bids/asks reflecting that estimate — **before the
   resolution window starts** (visible at T-1s and earlier)
4. At the exact candle open (T+0), briefly widen quotes (SOL: spread 0.05 → 0.12) to protect
   against informed flow during the most uncertain moment
5. Re-anchor within ~1s as the CEX CLOB evolves

**Critical clarification on what "CEX" means here:**

Chainlink data stream is already the CEX spot price — it's on-chain, public, and available to
everyone including us. There is no lag to exploit in the *spot price* feed. The MM's
informational edge is specifically the **CEX CLOB** — the live order book depth and flow on
Binance/Bybit — which is NOT captured by Chainlink and is NOT available through any oracle.

| Signal | What it is | Who has it | Lag exploitable? |
|--------|-----------|-----------|-----------------|
| Chainlink spot | CEX last price / mid | Everyone | No — public on-chain |
| CEX CLOB depth | Bid/ask levels & sizes | Anyone with exchange API | Yes — not on-chain |
| CEX order flow | Large orders hitting book | Exchange feed subscribers | Yes — not on-chain |

The T-1s prices are **already informed** by the CEX CLOB. The MM set YES=0.56 at T-1s not
because spot was above strike, but because the Binance order book showed a strong bid-side
imbalance — enough to predict UP with high confidence before the candle even started.

**Key empirical observation from the 05:00 UTC capture:**

| Coin | T-1s spread | T+0 spread | T+1s spread | Interpretation |
|------|------------|-----------|------------|----------------|
| BTC  | 0.01       | 0.01      | 0.01       | Liquid, confident MM |
| ETH  | 0.01       | 0.01      | 0.02       | Liquid, minor uncertainty |
| SOL  | 0.05       | 0.12      | 0.02       | **Opening gap — MM pulled quotes** |
| XRP  | 0.01       | 0.01      | 0.01       | Liquid, confident MM |

---

## Why Latency Arbitrage on Spot Is Off the Table

The MM re-anchors in ~1s. To exploit a stale CLOB quote based on spot price you would need
sub-500ms end-to-end latency. This requires co-location and dedicated infrastructure. But more
importantly — **Chainlink spot is not the signal to chase**. It is already public. Everyone
already has it.

The real question is: **can we read the CEX CLOB ourselves?**

Yes — Binance and Bybit provide public WebSocket feeds for order book depth (no auth needed).
This is the same feed the MM is consuming. We cannot beat the MM's reaction time to it, but
we do not need to. The edge is not speed — it is **using the same leading indicator the MM
uses to price the Polymarket CLOB, before that pricing is reflected in the tokens we buy**.

---

## What We CAN Exploit

Chainlink spot is already in our hands — it's the settlement oracle, we use it for delta SL.
The incremental edge is the **CEX CLOB** signals that precede spot movement:

### 1. CEX order book imbalance predicts the next 5m candle direction

When the Binance BTC/USDT perp book has 3× more bid depth than ask depth in the top 10
levels, price is statistically more likely to go UP. This is the *same signal the MM used*
to price YES=0.56 at T-1s. We can read it directly via the Binance WebSocket depth stream.

**Edge**: subscribe to Binance/Bybit `<symbol>@depth` WS for all active coins.
At `T-30s` before each 5m candle open, compute bid/ask imbalance ratio. Use as a directional
signal for both strategies.

### 2. Large resting orders (bid walls / ask walls) set support/resistance for the candle

A 200 BTC bid wall at strike-0.1% effectively anchors the price above the strike for the
duration of a 5m candle. The MM sees this and prices YES at 0.65+. If we see the same wall
before the MM updates the CLOB, we can enter the YES token at the current (stale-ish) price.

### 3. The opening spread gap is real capital at risk for the MM

When SOL's spread blows to 0.12 at T+0, the MM is explicitly saying "we are uncertain about
the CEX CLOB right now." That uncertainty window (0–1s) is where a CEX CLOB imbalance signal
is most valuable — you have an independent read on direction that the MM is currently not
using to price the market.

### 4. The MM prices the *average* candle, not the current CEX momentum

The MM sets T-1s prices based on the current CLOB state. If the CEX CLOB imbalance is
*accelerating* (bid side growing rapidly in the 30s before open), the MM's T-1s price may
already be stale — not because of latency, but because their update cycle is slower than
the order book is moving.

---

## OpeningNeutral Strategy — CEX CLOB Integration Plan

### Proposal (from user)

Use pre-open CEX CLOB data (T-60s to T-1s) + Polymarket combined cost as a quality filter:
- At T-60s to T-10s: subscribe to Binance depth WS, compute imbalance ratio averaged over 30–60s
- At T-5s to T+0: fetch YES ask + NO ask, compute combined cost
- Gate: only enter if imbalance is low (neutral-ish) AND combined cost is below threshold

### CEO Challenge #1 — This proposal turns a neutral strategy into a directional one

The core premise of OpeningNeutral is **direction-agnostic**: you buy both legs and profit regardless of outcome via the resting SELL exit. The moment you use CEX CLOB imbalance to decide whether to enter, you are implicitly predicting direction — you are skipping entries when the book is strongly imbalanced, which is the same as saying "I think the MM is right and the loser leg will move too fast."

**The empirical data contradicts this fear:**
From the 05:05 window, imbalance was strongly YES across all 4 coins. All 4 went UP 20+c by T+30s. The NO legs went to 0.28 (BTC), 0.27 (ETH), 0.28 (SOL), 0.25 (XRP). A resting SELL at 0.35 on NO would have filled cleanly on every coin. The directional move **helped** the neutral trade — the loser dropped decisively and the resting SELL was easy to fill.

**Implication**: High CEX CLOB imbalance does NOT degrade an OpeningNeutral trade. It accelerates the loser exit. The strategy does not need a directional filter on entry — it needs a **directional adapter on exit pricing**.

### Counter-Proposal A (RECOMMENDED): Use imbalance to set resting SELL price, not entry gate

Instead of blocking entries when imbalance is high, adjust the resting SELL price on the weaker leg:

```
if cex_imbalance > 2.0:           # strongly directional
    loser_resting_sell = 0.40     # set higher — loser will drop fast, 0.35 leaves $ on table
elif cex_imbalance > 1.5:
    loser_resting_sell = 0.37
else:                              # neutral / uncertain
    loser_resting_sell = 0.35     # current default
```

When the book is strongly imbalanced, the loser will collapse faster and further. A resting SELL at 0.35 may fill immediately (good) or the book may gap through it before it executes (bad — you're leaving a better fill on the table). Setting the resting SELL at 0.40 for strongly directional windows captures more of the loser leg's value.

This preserves direction-agnosticism at entry while using the imbalance signal where it actually matters: **exit pricing**.

### Counter-Proposal B: Gate only on cold-book state, not directional imbalance

The only entry gate that the empirical data supports is the cold-book gate already identified:

```
Skip if: combined ≥ 1.05 AND T+0 spread ≥ 0.20
```

SOL and XRP at 05:10 and 05:15 showed spread=0.26 at T+0ms — the MM is absent. That is the real quality signal. The CEX CLOB imbalance doesn't add to this gate; it would add a directional overlay that the neutral strategy doesn't need.

### What to build (Hybrid: A + B)

```
Pre-open (T-60s to T-10s):
  Subscribe to Binance/Bybit depth WS → compute rolling imbalance ratio per coin

At T-5s:
  Fetch Polymarket YES ask + NO ask → compute combined cost

Entry gate (direction-agnostic):
  SKIP if combined ≥ 1.05 AND T+0 spread ≥ 0.20  (cold book)
  SKIP if combined > 1.06 (book too wide regardless)
  OTHERWISE: ENTER

Exit pricing (uses directional signal):
  imbalance > 2.0  → resting SELL on weaker leg at 0.40
  imbalance > 1.5  → resting SELL on weaker leg at 0.37
  else             → resting SELL on weaker leg at 0.35
```

### CEO Challenge #2 — Pre-subscribing to Binance WS at T-60s adds real operational complexity

Adding a live Binance WebSocket feed to the bot introduces: connection management, reconnect logic, heartbeat monitoring, staleness detection, error handling. This is non-trivial and untested. The current bot already runs Hyperliquid's oracle feed — we could derive order book imbalance from HL perp CLOB instead, using the existing feed.

**Counter-proposal**: Before building the live feed, extend `capture_market_open_depth.py` to also capture Binance book imbalance at T-60s/T-30s/T-10s/T-1s for 10+ windows. Correlate imbalance ratio with actual Polymarket outcome. Calibrate the thresholds from real data. Then implement.

---

## Momentum Strategy — CEX CLOB Imbalance & Z-Score Relationship

### The Proposal

Add CEX order book imbalance as part of the signal strength algorithm — either as a secondary filter or combined with the z-score.

### What z-score measures vs what imbalance measures

These are fundamentally different signals and should NOT be collapsed into a single combined threshold:

| Signal | What it measures | Direction | Time horizon |
|--------|-----------------|-----------|--------------|
| **Vol z-score** | How far spot has **already moved** relative to expected vol. "Is the move statistically complete enough that reversal is unlikely?" | Backward-looking | Measures past move against future reversal probability |
| **CEX CLOB imbalance** | How the current order book is **set up to push price**. "Is there resting bid pressure that will continue the move?" | Forward-looking | Measures current book structure against near-term direction |

The z-score answers: *"has price moved far enough from the strike that I'm likely to win at expiry?"*
The imbalance answers: *"is there order book fuel to prevent a reversal in the remaining TTE?"*

**They are complementary, not redundant.** A high z-score with low imbalance means: price moved far but the book could snap back. A high imbalance with low z-score means: strong directional pressure but price hasn't confirmed enough yet. The strongest signal is **both high simultaneously**.

### Architecture: Two independent gates, not a combined score

```
ENTRY =
  spot_delta > vol_z_threshold(coin, TTE, sigma_ann)   ← z-score gate (existing)
  AND
  cex_imbalance_30s_avg > MOMENTUM_CEX_IMBALANCE_MIN   ← imbalance gate (new)
```

**Do NOT** combine them as `z_score * imbalance > composite_threshold`. That creates:
- False positives: moderate z + moderate imbalance clears a composite bar that neither alone would
- False negatives: a very high z-score gets blocked by low imbalance even when TTE is short and reversal is physically unlikely
- Uncalibrated noise: you have no data to set a composite threshold

Keep them as two independent AND-gates. Each has a clear semantic meaning and can be tuned independently.

### CEO Challenge #1 — You are not the only one reading the Binance CLOB

If the CEX CLOB imbalance is 4:1 in favor of bids, the MM has already seen it and repriced the Polymarket token. The Polymarket token at 0.82 with a 4:1 imbalance is not "stale" — it's exactly where the MM put it based on that imbalance. You are entering at the MM's informed price, not at a discount.

**This is the core tension**: the imbalance signal is strongest precisely when the MM has already acted on it (token is already repriced). When would imbalance be a leading signal vs a confirming one?

**Answer**: Only when the imbalance is **growing** (accelerating) and the PM token price is **lagging** (stationary). A static 4:1 imbalance = MM priced it in. A rapidly growing imbalance (1:1 → 4:1 in the last 60s) while token stayed at 0.75 = MM's update cycle is slower than the book movement.

**Refined signal:**
```
cex_imbalance_delta = imbalance_now - imbalance_60s_ago
pm_token_delta      = token_ask_now - token_ask_60s_ago

edge_signal = cex_imbalance_delta > IMBALANCE_ACCELERATION_THRESHOLD
              AND pm_token_delta < PM_PRICE_STALE_THRESHOLD  (token hasn't repriced)
```

This is the actual "MM is behind" detector. Static high imbalance with a repriced token = no edge. Rising imbalance with a stationary token = edge.

### CEO Challenge #2 — We have no calibrated data for imbalance thresholds

We captured 4 windows of Polymarket CLOB data. We have **zero** windows of Binance CLOB imbalance data correlated against Polymarket outcomes. Any threshold proposed now (`MOMENTUM_CEX_IMBALANCE_MIN = 2.0`) is a guess.

**Counter-proposal (phased approach):**

**Phase 1 — Research (do first):** Extend `capture_market_open_depth.py` to also capture:
- Binance/Bybit perp depth snapshot at T-60s, T-30s, T-10s, T-1s for each coin
- Imbalance ratio at each snapshot
- Track whether imbalance was rising or falling into the open

Run for 20+ windows. Then compute: correlation between T-1s imbalance and T+30s outcome. Set thresholds from actual data.

**Phase 2 — Implementation (only after Phase 1):** Add `CexClob` subscriber class to the bot. Feed imbalance into Momentum `_evaluate_entry` as a secondary AND gate (not composite score). Use Phase 1 data to calibrate `MOMENTUM_CEX_IMBALANCE_MIN` and `IMBALANCE_ACCELERATION_THRESHOLD`.

**Phase 3 — OpeningNeutral exit pricing**: Wire imbalance into resting SELL price selection per Counter-Proposal A above.

### CEO Challenge #3 — Hyperliquid perp CLOB vs Binance perp CLOB

We already subscribe to Hyperliquid oracle/price feeds. HL perp has significant liquidity on BTC/ETH/SOL/XRP. We could derive order book imbalance from the existing HL feed rather than adding a Binance WS dependency.

However: the Polymarket settlement oracle for 5m markets is **Chainlink** (which tracks a basket of CEX prices, dominated by Binance/Coinbase). HL perp book structure may diverge from Binance spot book structure at key moments — especially during HL-specific liquidation cascades. For settlement purposes, Binance depth is more directly relevant than HL depth.

**Recommended**: Use Binance `@depth20@100ms` as the primary CLOB feed. Use HL as a sanity cross-check if available. Do not use HL as primary.

---

## Revised Summary Table — What Changes

| Parameter / Behaviour | Current | Phase 1 (Research) | Phase 2 (Live) |
|----------------------|---------|-------------------|----------------|
| OpeningNeutral entry gate | Combined cost + cold-book skip | Same (no change) | Same |
| OpeningNeutral resting SELL price | Fixed 0.35 | Test 0.37/0.40 in simulation | Dynamic based on live imbalance |
| Momentum entry gate | `delta > vol_z_threshold` | Same | Add `AND imbalance_delta > threshold` |
| Momentum signal strength | Binary (in/out) | Research imbalance correlation | Imbalance multiplier on position size |
| CEX CLOB feed | None | Extend capture script | New `CexClob` WS subscriber in bot |
| Imbalance thresholds | N/A | Derived from 20+ windows of data | Calibrated values from Phase 1 |
| Z-score role | Entry gate | Unchanged | Unchanged — remains independent of imbalance |
| Combined z+imbalance score | N/A | Not recommended | Not recommended — keep gates independent |

---

---

## Empirical Findings — 3-Window Capture (2026-04-30 05:05 / 05:10 / 05:15 UTC)

### Raw Data Summary

| Window | Coin | T-1s YES | T-1s NO | Combined | MM Lean | T+0 Spread | T+30 YES | T+30 NO | Actual | MM Correct? |
|--------|------|----------|---------|----------|---------|-----------|----------|---------|--------|-------------|
| 05:05  | BTC  | 0.56 | 0.47 | 1.03 | YES +0.09 | 0.01 | 0.72 | 0.29 | UP   | YES |
| 05:05  | ETH  | 0.55 | 0.48 | 1.03 | YES +0.07 | 0.03 | 0.74 | 0.27 | UP   | YES |
| 05:05  | SOL  | 0.55 | 0.49 | 1.04 | YES +0.06 | 0.10 | 0.74 | 0.28 | UP   | YES |
| 05:05  | XRP  | 0.54 | 0.49 | 1.03 | YES +0.05 | 0.12 | 0.77 | 0.25 | UP   | YES |
| 05:10  | BTC  | 0.53 | 0.48 | 1.01 | YES +0.05 | 0.01 | 0.41 | 0.60 | DOWN | MISS |
| 05:10  | ETH  | 0.46 | 0.55 | 1.01 | NO  -0.09 | 0.02 | 0.50 | 0.51 | FLAT | MISS |
| 05:10  | SOL  | 0.54 | 0.52 | 1.06 | NEUTRAL   | 0.26 | 0.50 | 0.53 | FLAT | — |
| 05:10  | XRP  | 0.54 | 0.52 | 1.06 | NEUTRAL   | 0.26 | 0.39 | 0.63 | DOWN | — |
| 05:15  | BTC  | 0.53 | 0.48 | 1.01 | YES +0.05 | 0.01 | 0.51 | 0.50 | FLAT | MISS |
| 05:15  | ETH  | 0.43 | 0.59 | 1.02 | NO  -0.16 | 0.02 | 0.50 | 0.51 | FLAT | MISS |
| 05:15  | SOL  | 0.54 | 0.50 | 1.04 | YES +0.04 | 0.03 | 0.61 | 0.40 | UP   | MISS |
| 05:15  | XRP  | 0.52 | 0.54 | 1.06 | NEUTRAL   | 0.26 | 0.59 | 0.42 | UP   | — |

Also captured: validation window 05:00 (from prior single-window run, all 4 coins UP strongly).

### Key Finding 1: The T-1s Lean Is a Strong TREND Signal, Not a Per-Candle Signal

The 05:05 window was a **continuation of a strong bull run** — all 4 coins went UP 20+c.
The MM correctly pre-loaded YES > NO at T-1s across all 4 coins. But this is not a
per-candle edge — the MM was reading the same momentum that had already played out in the
05:00 window (also UP). The signal is not "MM is prescient about the next 5m candle",
it's "MM reflects the current CEX trend, which persists across candles."

**Implication**: T-1s YES > NO lean is a trend-following signal, not a mean-reversion
trigger. When YES is consistently > NO across multiple consecutive candles, the
underlying coin is trending. This aligns with the Momentum strategy's core premise.

### Key Finding 2: MM Is Wrong at Trend Reversals

The 05:10 window followed a strong UP trend. The MM still priced BTC YES > NO (+0.05)
even as the market reversed DOWN to YES=0.41 by T+30. The 05:15 window: ETH NO lean
was extreme (-0.16) but the market ended FLAT.

**MMs calibrate to current spot level, not momentum continuation.** At a trend top
(right after a strong UP candle), the MM is leaning YES because spot is still above
the strike — but the momentum has stalled or reversed. This is the precise moment
where OpeningNeutral has its best profile: the combined cost is ~1.01 (tight market),
the MM lean is ambiguous, and both legs have roughly equal probability of winning.

### Key Finding 3: Cold-Book Opening for SOL and XRP

SOL and XRP consistently showed spread=0.26 at T+0ms (ask=0.63, bid=0.37, the
identical "cold book" sentinel across multiple windows and tokens). This confirms:
- The CLOB book for SOL/XRP is **not published at the exact market open millisecond**
- The MM goes live for these coins ~1s after open
- BTC and ETH are consistently tight (spread 0.01-0.03) at T+0

**For OpeningNeutral**: the 50ms pre-timer entry (Idea 1) will hit a cold book for
SOL and XRP. The actual tradeable price does not appear until T+500ms to T+1s.
This means the "scheduled entry" fires into an empty book for these coins.
**Action**: treat T+0 cold-book state as a "wait" condition — retry at T+500ms.

**For Momentum**: SOL and XRP's cold-book opening is not relevant (Momentum entries
occur well inside the candle, not at T+0).

### Key Finding 4: Bid Depth Asymmetry Is NOT a Reliable Directional Signal

BTC at 05:10 had bid depth asymmetry of **8.57x** (YES bids 8.57× greater than NO
bids) yet the coin went DOWN (YES=0.41 at T+30). XRP at 05:05 had 0.98x (balanced)
yet went strongly UP (+0.25). The depth asymmetry reflects order-book structure, not
directional conviction.

**Retract the earlier recommendation** to use bid depth asymmetry as a directional
signal or to vary the resting SELL price based on it. It adds noise, not signal.

### Key Finding 5: Combined Cost 1.06 = Uncertain / Thin Market

SOL and XRP at 05:10 had combined cost = 1.06 AND the cold-book T+0 spread of 0.26.
These are the same coins/windows where the MM is effectively absent at open. The
1.06 combined cost is caused by the MM quoting wide (0.54 ask on both YES and NO)
precisely *because* they cannot confidently anchor to CEX at that moment.

**OpeningNeutral action**: skip entry when combined ≥ 1.05 AND T+0 spread ≥ 0.20
for ANY coin in the pair. The MM's wide quote is a "I don't know" signal, not an
opportunity — you are not taking the other side of a confident MM, you are taking
the other side of an uncertain one with a wide spread locked in against you.

### Updated Combined Cost Gate

| Combined at T-1s | T+0 Spread | Interpretation | OpeningNeutral Action |
|-----------------|-----------|----------------|----------------------|
| 1.00 – 1.02     | ≤ 0.02    | MM neutral, tight | Best entry — both legs fair |
| 1.03 – 1.04     | ≤ 0.05    | MM has mild lean, liquid | Enter — one leg may move faster |
| 1.05 – 1.06     | ≤ 0.05    | MM has strong lean | Enter only if spread < 0.05 |
| Any             | ≥ 0.20    | Cold book / absent MM | **Skip** — book not live yet |
| > 1.06          | Any       | Book too wide | **Skip** |
