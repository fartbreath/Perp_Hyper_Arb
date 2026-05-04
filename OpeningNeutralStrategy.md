# Opening Neutral Strategy

## Glossary

The following terms are used throughout this document.

- **Polymarket** — the prediction market platform on which this strategy operates.
- **Hyperliquid** — the perpetual futures exchange used as a proxy for real-money directional sentiment in crypto markets. Used by Phase 1 signal-informed pricing.
- **Bucket market** — a binary prediction market with a fixed expiry time horizon. Polymarket offers buckets of 5 minutes, 15 minutes, 1 hour, 4 hours, daily, and weekly duration.
- **Up/Down market** — a market of the form "Will BTC be above $X?" or "Will BTC be below $X?". Opening Neutral only enters Up/Down markets. Specific-strike markets are excluded because their YES and NO tokens are not symmetrically priced at open.
- **Delta-neutral** — zero directional bias at entry. The strategy holds equal-sized positions on both YES and NO and does not predict which side wins.
- **Pair** — one Opening Neutral trade: two simultaneous positions (YES + NO) in the same market, managed together until the loser exits.
- **Loser leg** — whichever side resolves to $0.00. Its price descends from ~$0.50 toward zero during the bucket. The resting SELL catches it at $0.35 on the way down.
- **Winner leg** — whichever side resolves to $1.00. After the loser exits, the winner is handed to the Momentum strategy for exit management toward $1.00.
- **Resting SELL** — a limit order placed at $0.35 immediately after entry fills, waiting passively for the loser to drop through that price.
- **Pair completion** — a pair is complete when the resting SELL on the loser leg fills. Incomplete pairs — where the loser never falls to $0.35 before resolution — produce a small loss.
- **Momentum handoff** — after the loser exits, the winner position is passed to the Momentum strategy. It is then managed by Momentum's oracle delta stop-loss and take-profit, not by Opening Neutral.
- **Combined cost** — the total capital deployed per pair: YES ask price + NO ask price at entry. The maximum qualifying combined cost is $1.01.
- **Cold book** — a market where the bid-ask spread is wide, indicating thin liquidity. In cold-book windows, the resting SELL at $0.35 is unreliable — the loser may stall and never reach the exit price before resolution.
- **Central Limit Order Book (CLOB)** — the order book where YES and NO prediction market tokens are traded on Polymarket.
- **WebSocket** — a persistent, real-time data connection. Fill events and price updates arrive via WebSocket; nothing is polled.

---

## Overview

The Opening Neutral strategy enters both sides of a binary prediction market simultaneously at open, then recovers value from whichever side loses. It is a loser-recovery strategy: by placing a resting limit order on both legs at entry, it captures $0.35 from the losing leg on its descent to zero — converting a break-even structure into a consistent profit per completed pair.

The strategy is **direction-agnostic**: it does not predict whether YES or NO wins. It enters both sides at equal size and equal price (~$0.50 each). The losing leg passes through $0.35 on its way to $0.00 — the resting SELL is simply in place to catch it. The winning leg is handed to the Momentum strategy, which manages it from ~$0.50 to near $1.00.

> **The edge is not prediction. It is fill reliability.** A correctly-entered pair always produces +$0.35 from the loser, provided the resting SELL fills before resolution. The entire focus of gate design is filtering windows where that fill is unreliable.

---

## The Edge: Loser Recovery

Binary markets always resolve to $1.00 / $0.00. The loser token starts at ~$0.50 at open and ends at $0.00 at resolution. During price discovery, it typically descends toward zero — the resting SELL is in place to catch it at $0.35 on that descent. The resting SELL must fire before resolution: at settlement, the loser snaps directly to $0.00 without trading through $0.35.

```
Entry:       BUY YES @ $0.50  +  BUY NO @ $0.50  =  $1.00 combined cost
Loser exit:  SELL losing side @ $0.35             =  $0.35 recovered
Winner:      Handed to Momentum → exits near $1.00 at resolution
Net:         ~$1.00 (winner) + $0.35 (loser exit) − $1.00 (entry) = +$0.35 per pair

Without resting SELL:
             $1.00 (winner) + $0.00 (loser at resolution) − $1.00 (entry) ≈ $0.00
```

The resting SELL is what separates a break-even structure from a profitable one. The earlier the loser falls, the sooner capital is freed and the winner transitions to Momentum.

**Why the Momentum handoff matters:** Holding the winner passively at a flat $0.35 exit would leave most of its upside uncaptured. The Momentum monitor manages the winner from ~$0.50 to near $1.00 — that journey is larger than the entire loser recovery and cannot be abandoned.

**Structural failure mode:** If the loser stalls at $0.40–$0.45 through the entire bucket, the resting SELL never fills. Both legs resolve at expiry — winner pays $1.00, loser pays $0.00. Net: `$1.00 − combined_cost`. At the median combined cost of $1.02, this is a −$0.02 loss per pair. This is why cold-book windows must be filtered before capital is deployed.

### Combined Cost Distribution (from dataset)

| Metric | Value |
|--------|-------|
| Mean combined cost at open | $1.0567 |
| Median combined cost | $1.02 |
| Qualifying windows (≤ $1.01) | ~5th percentile |

The strategy is deliberately selective. The tight $1.01 gate is a feature — only the cheapest ~5% of opens qualify, and those are the windows where the combined cost leaves sufficient margin even in the failure-mode scenario.

### Cold-Book Spread Distribution (from dataset)

| Metric | Value |
|--------|-------|
| Mean individual leg spread at open | 11.3% of token price |
| Shape | Long right tail — many windows have spreads of 20–40% |
| Windows with spread > 0.15 on either leg | Unreliable resting SELL fill timing |

A 15-cent spread on a ~$0.50 token means market makers are pricing in 30% uncertainty around mid. The resting SELL at $0.35 requires the loser to fall ~15 cents from mid — a spread that wide means the CLOB has already absorbed most of that move as uncertainty, making fill timing unpredictable.

---

## How It Works

For each Up/Down bucket market reaching its scheduled open timestamp:

1. **Pre-screen at registration.** Before open, markets are evaluated once for structural eligibility: correct market type, Up/Down structure, not already entered. Markets that fail are dropped and never scheduled.
2. **Warm the connection.** 200 milliseconds before open, a lightweight request warms the underlying network connection so the entry orders start on an already-established socket.
3. **Evaluate live conditions at open.** In the final 50ms before the market opens, live book state is checked against three gates: combined cost, price balance between legs, and cold-book spread. If any gate fails, the window is skipped.
4. **Enter both sides simultaneously.** YES and NO buy orders fire concurrently at the current ask price. Both orders are placed at the same instant — entry is as close to delta-neutral as the order book allows.
5. **Place resting SELLs immediately.** As soon as both fills confirm, resting limit SELL orders are placed at $0.35 on both legs. The loser leg will descend and fill one of them; the winner leg's SELL is cancelled once the loser fills.
6. **Wait for the loser to fall.** The resting SELL requires no monitoring — a live fill notification fires the moment the loser drops through $0.35.
7. **Loser exits, winner is handed off.** When the resting SELL fires on one leg: that position is closed at $0.35, the other leg's resting SELL is cancelled, and the surviving position is passed to the Momentum strategy.
8. **Momentum takes over the winner.** The winning position is now managed by Momentum's oracle stop-loss and take-profit. It exits near $1.00 at resolution or earlier if the oracle reverses.

---

## The Gate Stack

All gates must pass. Pre-qualification gates are evaluated once at registration; dynamic gates are evaluated with live book data at the moment of open.

| Layer | Gate | What It Blocks |
|-------|------|----------------|
| **Market type** | Up/Down bucket markets only | Specific-strike markets (asymmetric entry costs at open) |
| **Dedup** | Not already entered in this market | Re-entry on the same market within the same window |
| **Combined cost** | YES ask + NO ask ≤ $1.01 | ~95% of markets — only the cheapest 5th percentile qualifies |
| **Price balance** | Both legs trading near 50/50 | Skewed markets where one leg is already priced below the $0.35 exit target |
| **Cold-book spread** | Individual leg spread ≤ $0.15 | Cold-book windows where the resting SELL fill is unreliable |
| **Concurrent cap** | Open pairs < maximum | Correlated simultaneous exposure across too many pairs |

---

## Entry Gates

### Combined Cost Gate

The combined cost at open is the sum of YES ask + NO ask. This is the total capital deployed per pair, and its maximum qualifying value is **$1.01**.

The tight gate reflects the data: median combined cost is $1.02, meaning more than half of all opens fail immediately. The strategy only enters the cheapest windows — those where the market maker spread has not yet pushed the cost above the threshold where the failure-mode scenario (loser never fills) produces an unacceptable loss.

At $1.01 combined cost, a no-fill resolution produces −$0.01. At $1.03, it produces −$0.03. The gate is calibrated to keep the no-fill loss small relative to the +$0.35 gain from a completed pair. Do not loosen this gate — the selectivity is the point.

### Price Balance Gate

Both individual legs must be trading near 50/50 at entry. This blocks highly skewed markets — for example, YES = $0.12, NO = $0.89 — where the loser token is already priced *below* the $0.35 resting SELL before the pair is even entered. Entering such a market would require the loser to rise to $0.35 before it could fall through — inverting the entire exit mechanic.

The gate ensures both legs have room to fall from their entry price to the $0.35 target.

### Cold-Book Spread Gate

The spread on each individual leg is the difference between the best ask and the best bid. When either leg's spread exceeds **$0.15**, the window is skipped as a cold-book market. When a leg has no visible bid at all, the window is skipped for missing liquidity.

**Why $0.15:** A spread of 15 cents on a ~$0.50 token means market makers are pricing in approximately 30% uncertainty around the mid price. The resting SELL at $0.35 requires the loser to fall ~15 cents from the ~$0.50 mid to trigger. A 15-cent spread means the CLOB already has 15 cents of uncertainty baked in — the loser could stall anywhere in that range without a clean $0.35 fill. Windows with spreads above $0.15 have historically shown unreliable pair-completion rates.

The threshold starts conservative at $0.15. It may be widened to $0.17 or $0.20 only if paper-trade data shows it is blocking windows that genuinely complete.

---

## Entry Timing

Opening Neutral uses timer-driven entry. Market open timestamps are known in advance and scheduled precisely — entry does not depend on waiting for the first price update after open.

```
T − 200ms   Warm the CLOB connection.
            A lightweight request ensures the network path is live before orders fire.

T − 50ms    Evaluate live gates.
            Read current book state: combined cost → price balance → cold-book spread.
            If any gate fails → skip, log the skip reason.

T + 0ms     Fire both BUY orders simultaneously.
            YES and NO orders sent concurrently — neither waits for the other.

T + 0 to 30s
            Await fills on both legs.
            Both fill   → place resting SELLs at $0.35 on both legs.
            One fills   → keep as standalone Momentum position; cancel the other.
            Neither fills → cancel both; no position taken.
```

If the market is already open when the bot starts (a restart scenario), entry is evaluated on each incoming price update instead of a timer, using the same gate stack. Entry is blocked entirely after 120 seconds past the market open — too much time has elapsed for the open-price dynamics to still be valid.

---

## Exit Logic

### Two Pair Outcomes, Two Paths

Opening Neutral exits unfold in one of two ways. The design is built around the expected path (pair completes) but the failure path (pair does not complete) is handled cleanly.

- **Pair completes** — the loser leg falls through $0.35 during the bucket. The resting SELL fires, the loser position closes at $0.35, the winner is handed to Momentum. This is the profitable outcome.
- **Pair does not complete** — the loser stalls above $0.35 through the entire bucket. Both legs reach resolution: winner pays $1.00, loser pays $0.00. Net is `$1.00 − combined_cost` — a small loss at the $1.01 entry threshold.

### Exit Priority Table

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | Resting SELL on YES fires at $0.35 | Close YES (loser). Cancel NO resting SELL. Pass NO to Momentum. |
| 2 | Resting SELL on NO fires at $0.35 | Close NO (loser). Cancel YES resting SELL. Pass YES to Momentum. |
| 3 | Only one BUY fills within 30 seconds of entry | Keep filled leg as standalone Momentum position. Cancel unfilled BUY. |
| 4 | Market resolves with no resting SELL fired | Winner resolves at $1.00, loser resolves at $0.00. |

### Resting SELL

After both entry fills confirm, resting limit SELL orders are placed at $0.35 on both legs immediately. The resting SELL is always a limit order — never a market sell. Using a market sell would execute below $0.35 and reduce or eliminate the recovery edge.

Fill detection uses the live WebSocket stream. When the loser drops through $0.35, the fill notification arrives via the data feed — there is no periodic polling or monitoring loop.

### Momentum Handoff

When the resting SELL fires on one leg:

1. The loser position is closed at $0.35.
2. The winner's resting SELL is cancelled — the winner should not be sold at $0.35.
3. The winner position is passed to Momentum. From this point, Momentum's oracle stop-loss and take-profit manage the exit.

The winner enters Momentum at ~$0.50–$0.65 and is managed toward $1.00. Momentum's standard exit paths apply: oracle delta stop-loss, take-profit at near-certainty, and oracle tick direction signal if Phase 3 is live.

### One-Leg Fallback

If only one BUY fills within the 30-second entry timeout, the filled leg is kept as a standalone Momentum position. The unfilled BUY is cancelled. No resting SELL is placed — the Momentum strategy manages the single leg from its entry price using its own stop-loss and take-profit.

The filled leg entered at ~$0.50 is a valid Momentum entry. The risk is asymmetric: there is no neutral hedge, but Momentum's stop-loss limits the downside.

### Resolution Path

If the resting SELL never fires, both legs reach market resolution. The winner resolves at $1.00, the loser resolves at $0.00. The net result is `$1.00 − combined_cost` — at the $1.01 qualifying threshold, this is −$0.01 per pair. The resolution path is the failure mode. Every gate is designed to reduce the frequency of this outcome.

---

## Phase 1 Signal-Informed Pricing

Two enhancements to resting SELL pricing are built into the strategy but disabled by default. They require two weeks of production paper-trade data to validate before enabling and are dependent on Hyperliquid funding rate and Polymarket depth share data being available from the Momentum strategy's Phase 1 pipelines.

Both features adjust only the *price level* of the resting SELLs — both legs always receive a resting SELL regardless of direction. The strategy remains delta-neutral at entry.

### Asymmetric Resting Sell Prices

When the Hyperliquid perpetual funding rate has a strong directional lean, one leg is more likely to be the loser. In those windows, the predicted winner's resting SELL is raised slightly above the standard $0.35 — giving it headroom so it is not accidentally filled by intraday noise before it has moved toward $1.00. The predicted loser keeps the standard $0.35 sell. This creates a price gap between the two resting SELLs ($0.35 vs ~$0.38), which reflects the relative likelihood of each leg being the one to fall.

When funding is flat or unavailable, both legs use the standard $0.35 symmetric price.

**Enablement criteria:** The average intraday minimum price of the predicted winner leg must be consistently above the tightened threshold (i.e. the winner does not dip through the higher price and get accidentally exited). Validated over at least two weeks of paper fills before enabling.

### Loser Confidence Scoring

Combines the Hyperliquid funding rate signal with Polymarket's order book depth share into a two-signal confidence score. When both signals independently agree on which leg is likely to be the loser, an additional tighten is applied to that leg's resting SELL on top of any asymmetric adjustment.

The tighten is only applied when both signals agree (a score of ±2). When they partially agree or disagree, no tighten is applied. This ensures the feature only acts on high-conviction windows where the evidence is clear from two independent sources.

---

## Worked Examples

### Pair completes — loser exits at $0.35

BTC, 5-minute bucket. At open, YES is trading at $0.502 and NO at $0.503. Combined cost = $1.005 — qualifies. YES spread = $0.08, NO spread = $0.11 — both below $0.15. Both legs near $0.50 — price balance passes.

Both BUY orders fill within 2 seconds. Resting SELLs placed at $0.35 on YES and NO.

Market resolves YES (BTC closes above strike). NO token price descends: $0.42 → $0.38 → $0.35. Resting SELL on NO fires. NO closed at $0.35. YES resting SELL cancelled. YES handed to Momentum at $0.65.

Momentum exits YES near $1.00 at resolution.

**Result: $0.35 (NO exit) + ~$1.00 (YES via Momentum) − $1.00 (entry) = +$0.35 per pair.**

---

### Entry skipped — cold book

ETH, 15-minute bucket. At open, YES is at $0.51, NO at $0.495. Combined cost = $1.005 — qualifies. NO spread = $0.09 — within threshold. YES spread = $0.22 — exceeds $0.15.

**SKIP: YES spread 0.22 > 0.15. Cold-book window. Pair not entered.**

---

### Entry skipped — combined cost

SOL, 1-hour bucket. At open, YES is at $0.52, NO at $0.51. Combined cost = $1.03 — exceeds $1.01 threshold.

**SKIP: Combined cost $1.03 > $1.01. In the failure-mode scenario (loser never fills), net would be −$0.03 per pair — too expensive.**

---

### Entry skipped — skewed price

BTC, 5-minute bucket. At open, YES is at $0.17, NO at $0.84. Combined cost = $1.01 — technically qualifies on cost. However, YES is priced at $0.17 — far below the $0.35 resting SELL target.

**SKIP: Price balance gate. YES ask of $0.17 is below the minimum qualifying price. Entering would require YES to rise from $0.17 to $0.35 before it could fall through — the resting SELL mechanic does not work on a token already below its exit price.**

---

### One-leg fallback — entry timeout

BTC, 5-minute bucket. Entry qualifies on all gates. Both BUY orders placed. YES fills at $0.495 within 1 second. NO has no asks — sits unfilled for 30 seconds.

Timeout fires. NO BUY cancelled. YES position kept and passed directly to Momentum.

**Result: Standalone Momentum position in YES at $0.495 entry. No loser recovery. Managed by Momentum to delta stop-loss or take-profit.**

---

## Failure Mode Reference

| Scenario | Guard | How It Fires |
|----------|-------|-------------|
| Loser stalls above $0.35 through entire bucket | Cold-book spread gate filters thin books before entry | Pre-entry; if passed and book unexpectedly thins, loser resolves $0.00 via resolution path |
| Wide spread at open signals cold book | Individual spread gate — both legs must be ≤ $0.15 | Fires at entry evaluation using live book state |
| No visible bid on either leg | Missing bid check — skip with "no_spread" | Fires before spread calculation |
| Combined cost too high for acceptable loss | Combined cost gate — YES ask + NO ask ≤ $1.01 | Fires at entry evaluation immediately after book read |
| Skewed market — one leg already below $0.35 | Price balance gate — both legs must be near 50/50 | Fires after combined cost gate |
| Only one BUY fills within entry timeout | One-leg fallback — keep as Momentum | Fires 30 seconds after entry if second leg has not filled |
| Neither BUY fills | Both orders cancelled; no position taken | Fires at entry timeout |
| Resting SELL never fires before resolution | Resolution path — winner $1.00, loser $0.00 | At market resolution via standard settlement |
| Re-entry attempted on same market | Dedup guard blocks re-evaluation | Fires at pre-qualification and again at entry spawn |
| Too many pairs open simultaneously | Concurrent pair cap | Evaluated at entry with live pair count |
| Winner accidentally sold by its own resting SELL | Winner's resting SELL cancelled when loser exits | Fires on loser fill — winner SELL cancelled before Momentum takes over |
| Phase 1 asymmetric sell set too tight on winner leg | Winner SELL buffer — extra headroom above base price | Only relevant when Phase 1 is enabled; buffer prevents winner from being exited early |
| Entry window already closed | 120-second post-open cutoff | Entry blocked; market dropped |
| Both legs enter but market was already moving | Timer fires at open_ts precisely; pre-warm reduces latency | TCP pre-warm 200ms before open reduces fill delay |
| Market open when bot starts (restart) | WS-tick fallback path — evaluates all gates on each price update | Covers markets already live; no timer available for past opens |
