# Momentum Strategy

## Glossary

The following terms are used throughout this document.

- **Polymarket** — the prediction market platform on which this strategy operates.
- **Hyperliquid** — the perpetual futures exchange used as a proxy for real-money directional sentiment in crypto markets.
- **Bucket market** — a binary prediction market with a fixed expiry time horizon. Polymarket offers buckets of 5 minutes, 15 minutes, 1 hour, 4 hours, daily, weekly, and milestone duration.
- **Strike** — the price level at which a market resolves. For a "Will BTC be above $68,000?" market, the strike is $68,000.
- **Oracle** — an authoritative external price feed used for settlement and stop-loss decisions. This strategy uses Chainlink on-chain oracle prices for short-duration markets and Polymarket's real-time data feed for longer ones.
- **Delta** — the percentage gap between the current spot price and the strike, measured in the direction of the winning outcome. A YES trade has positive delta when spot is above strike.
- **Stop-loss (SL)** — an automatic exit that closes the position before the loss grows further.
- **Take-profit (TP)** — an automatic exit that locks in gains when the token reaches near-certainty.
- **Expected value (EV)** — the probability-weighted average outcome. Negative expected value means the trade is expected to lose money before fees.
- **Implied volatility (IV)** — the market's expectation of future price volatility, as inferred from options prices.
- **At-the-money (ATM)** — the options term for the strike closest to the current spot price. ATM implied volatility is the standard measure of expected move.
- **Time to expiry (TTE)** — how many seconds remain until the market closes and resolves.
- **Time-Weighted Average Price (TWAP)** — a price average where each reading is weighted by the time it was held. Used here as a short-term reference price to detect whether the oracle is trending up or down immediately before entry.
- **Exponentially Weighted Moving Average (EWMA)** — a smoothed average where recent values carry more weight than older ones.
- **Area Under the Curve (AUC)** — a measure of a binary classifier's predictive power. An AUC of 0.5 is no better than random; 1.0 is perfect. AUC 0.703 means the signal correctly ranks a winning outcome above a losing one 70.3% of the time.
- **Basis points (bps)** — hundredths of a percent. 100 bps = 1%.
- **Central Limit Order Book (CLOB)** — the order book where YES and NO prediction market tokens are traded.
- **Real-Time Data Service (RTDS)** — Polymarket's streaming market data feed.
- **WebSocket** — a persistent, real-time data connection. All price and order book inputs arrive via WebSocket; nothing is polled.
- **Chainlink** — a decentralized oracle network that publishes verified asset prices on-chain. The primary price source for 5-minute, 15-minute, and 4-hour Polymarket markets.

---

## Overview

The Momentum strategy buys a prediction market token in the final seconds before expiry, when the outcome is already heavily favoured by both the spot price and the crowd. It is a near-expiry spread-capture strategy: it profits from the gap between the token's current market price (typically around 85 cents) and its resolution value of $1.00, in windows where regime signals confirm that the directional trend is real.

The strategy is **direction-agnostic**: it enters either the YES or NO side, whichever the market has partially priced in, as long as the oracle price independently confirms that direction. If spot is falling hard into expiry, the NO token may be at 85 cents — that is the trade. If spot is rising, the YES token may be at 87 cents — that is the trade.

---

## The Edge: Selection Over Prediction

Analysis of 681 labeled 5-minute binary market windows across 7 coins shows the key structural finding:

> **Aggregate YES win rate: 49.8%. Filtered regimes: 62–76%.**

The edge does not come from predicting direction better than the market. It comes from knowing which windows to skip entirely. In flat-funding regimes — where the Hyperliquid perpetual futures market has no strong directional lean — the win rate is approximately 49%, which is negative expected value after fees. In directionally aligned regimes the win rate rises to 62–76%. The regime gates are what turn selection into edge.

| Hyperliquid Funding Regime | Direction | Samples | Win Rate |
|---------------------------|-----------|---------|----------|
| Strongly negative (below −0.00001) | YES | 21 | **76.2%** |
| Strongly positive (above +0.00001) | NO | 138 | **62.3%** |
| Flat (between the two thresholds) | either | ~450 | ~49% — **skip** |

---

## How It Works

For each open bucket market (5-minute, 15-minute, 1-hour, 4-hour):

1. **Find a candidate.** One side of the market is trading in the 80–90 cent band — the crowd believes this outcome is 80–90% likely.
2. **Check the oracle.** The underlying spot price must confirm the same direction at or above a volatility-scaled threshold. Spot must be above strike for YES entries; below strike for NO entries.
3. **Apply regime gates.** Three independent signal layers filter out regimes with low or negative expected value: Hyperliquid funding rate direction, Polymarket order book depth positioning, and oracle price trend in low-volatility conditions.
4. **Enter.** If all gates pass, buy the in-band token as a taker. Hold until resolution, take-profit, or a stop fires.
5. **Monitor in real time.** Two independent exit paths cover both sharp reversals (delta stop-loss) and gradual deterioration (oracle tick direction signal). Either can fire first.

---

## The Signal Stack

All layers must agree. Outer gates eliminate unprofitable regimes; inner signals confirm the specific trade.

| Layer | Signal | Filters | Win Rate Impact |
|-------|--------|---------|-----------------|
| **Regime gate** | HL funding rate direction | Flat-funding windows have ~49% win rate | +12–27pp vs unfiltered |
| **Book gate** | PM depth share (YES bid %) | Crowd book positioning predicts direction | AUC 0.5683 |
| **Momentum gate** | Oracle TWAP deviation × vol regime | Low-vol sell-off into entry = 37.6% YES win rate | Blocks noise entries |
| **Entry signal** | Token at 80–90c | Crowd conviction already high before entry | Filters coin-flip situations |
| **Confirmation** | Spot delta ≥ vol-scaled threshold | Raw price independently confirms crowd direction | Filters false crowd moves |
| **Hold signal** | Oracle tick direction (upfrac EWMA) | Real-time oracle tick direction during the hold | AUC 0.703 — primary exit signal |

---

## Regime Gates

### Funding Rate Direction Gate

The Hyperliquid perpetual funding rate is the highest-priority entry filter. When perpetual longs are paying shorts, the dominant market position is short — directional pressure is downward. A strongly positive funding rate is therefore bearish signal and works against YES entries. When shorts are paying longs, the dominant position is long — a strongly negative funding rate supports YES entries.

Analysis shows that entering against the funding direction in a strongly aligned regime produces approximately 38% win rate for YES — deeply negative expected value.

**Rules:**
- Skip YES entries when the Hyperliquid funding rate is strongly positive — the perpetual market is broadly long-biased, opposing a YES trade.
- Skip NO entries when the funding rate is strongly negative — the perpetual market is broadly short-biased, opposing a NO trade.
- When funding is flat (between the two thresholds), skip the market entirely — these regimes produce ~49% win rate with negative expected value after fees.

**Conviction boost:** When the funding direction aligns with the intended entry — for example, strongly negative funding for a YES trade — the minimum required spot delta is reduced slightly. This allows entries from a marginally wider range without loosening the absolute floor.

**Size multiplier:** A position sizing multiplier can be activated for high-conviction funding-aligned regimes, but only after enough forward-validated live samples confirm the expected win rate holds out-of-sample.



### Crowd Book Depth Share Gate

The depth share measures what fraction of total bid-side liquidity in the Polymarket CLOB sits on the YES side. When YES depth share is in the bottom quartile (below 25%), the YES win rate drops to 41.5%. When it is in the top quartile (above 75%), the YES win rate rises to 60%. This signal is independent of the funding rate and compounds with it when they agree.

**Rules:**
- Skip YES entries when the YES depth share is below the minimum threshold — the order book does not reflect crowd conviction in the YES direction.
- Skip NO entries when the YES depth share is above the maximum threshold — the crowd book is positioned against a NO entry.

Depth share is derived from the live Polymarket order book data. No additional data calls are required.



### Oracle TWAP Deviation Gate (Low-Volatility Conditions)

In normal or high-volatility regimes, whether the spot price has been trending up or down in the moments before entry carries no predictive information. However, in low-volatility regimes — when the 60-second realized volatility is below its rolling median — a downward price drift into the entry window is a meaningful warning sign.

When the Chainlink oracle price is below its own 10-second TWAP at the time of entry (a downward deviation measured in basis points), and volatility is low, the YES win rate drops to 37.6%. This pattern suggests the underlying is softening without the conviction that higher-volatility directional moves carry.

**Rule:** In low-volatility conditions, apply a higher multiplier to the minimum required spot delta for YES entries when the oracle is drifting below its recent average. This raises the effective entry bar, blocking borderline entries during low-volatility sell-offs.



---

## Dynamic Volatility Threshold

The minimum required spot delta is computed live from actual market volatility — not a static table. This automatically widens the entry gate in volatile conditions and tightens it in quiet markets.

### Volatility Source

| Priority | Source | Assets | Refresh Rate |
|----------|--------|--------|--------------|
| 1 | **Deribit at-the-money implied volatility** (nearest ~7-day call option, ATM strike) | BTC, ETH, SOL, XRP | Every 5 minutes |
| 2 | **Rolling realized volatility** (log-return standard deviation from recent price history) | All tracked assets | Every 60 seconds |
| 3 | **Skip signal** | Assets with no data | — |

Deribit ATM implied volatility reflects *expected* future volatility, not just past realized moves. Using the nearest weekly option means the threshold already accounts for the current regime — upcoming events, macro conditions, sentiment — not just recent price history.

For assets without listed options (HYPE, DOGE), rolling realized volatility is computed from streaming price tick history provided by Polymarket's RTDS feed. If no volatility estimate is available for an asset, that market is skipped.

### How the Threshold is Computed

The entry threshold scales the annualized volatility down to the specific time horizon of the trade: the annualized volatility is reduced proportionally to the fraction of a year represented by the remaining time to expiry, then multiplied by a configured z-score. The strategy targets the 95th percentile of a normal distribution — the spot price must already be further into winning territory than 95% of random starting positions.

This threshold is expressed as a percentage: the spot must be at least this many percent above (YES) or below (NO) the strike.

### Example Thresholds (BTC at 52% annualized IV)

| Time to Expiry | Example threshold (BTC at 52% annual IV) |
|---------------|--------------------------------------|
| 5 minutes | 0.17% |
| 15 minutes | 0.30% |
| 1 hour | 0.59% |
| 4 hours | 1.18% |

For ETH at 68% implied volatility with 1 hour to expiry, the threshold is 0.78%. For SOL at 78%, it is 0.89%. The threshold moves with volatility automatically.



---

## Absolute Minimum Distance

An absolute minimum floor on the spot-to-strike gap is maintained **independently of the volatility-scaled threshold**. It exists because the volatility-scaled threshold can shrink to very small values at short time to expiry and low volatility, passing entries where the spot is only one tick from the strike — meaning a single adverse price move would put the position underwater.

**The principle:** The absolute gap between spot and strike determines whether the position can survive a single adverse price tick. That tick risk is the same regardless of how much time remains.

**Calibration from data:**

| Observation | Detail |
|-------------|--------|
| Smallest losing delta in dataset | 0.076% (XRP, 5-minute bucket, 63 seconds to expiry) |
| Smallest winning delta in dataset | 0.084% |
| Recommended floor | **0.08%** — blocks the observed losing gap; preserves all observed winners |

The effective entry threshold is always the higher of the two: the volatility-scaled threshold or the absolute floor. When volatility is high, the vol-scaled threshold dominates and the floor has no effect. When volatility is low or time is short, the floor becomes binding — exactly when thin-gap entries are most dangerous.

---

## Entry Window

The strategy only enters in the final moments before expiry, where so little time remains that spot has almost no room to reverse. Entries outside these windows are ignored regardless of signal strength.

| Market Duration | Entry Window |
|----------------:|:------------|
| 5 minutes | Last 30 seconds |
| 15 minutes | Last 60 seconds |
| 1 hour | Last 2 minutes |
| 4 hours | Last 5 minutes |
| Daily | Last 15 minutes |
| Weekly | Last 1 hour |
| Milestone events | Last 30 minutes |

---

## Exit Logic

### Two Reversal Shapes, Two Paths

Exit logic covers two non-overlapping failure modes. Using a single stop-loss mechanism for both would result in late exits in gradual reversals and spurious exits on sharp-but-temporary bounces. The two paths are designed independently.

- **Sharp reversal** — the oracle spot price moves quickly against the position. The delta stop-loss fires within 1–2 oracle ticks, well before slower signals can accumulate.
- **Gradual reversal** — the oracle drifts against the position while the moment-to-moment tick direction has already shifted. The oracle tick direction signal fires first, typically 10–30 seconds before the delta stop-loss threshold would be reached.

The delta stop-loss and near-expiry time stop are evaluated first on every oracle update. If neither fires, the oracle tick direction signal is checked. This ordering ensures the hard oracle backstop always has priority, while the tick direction signal provides an earlier, smoother exit in gradual deterioration.

### Exit Priority Table

| Priority | Condition | Fires When |
|----------|-----------|------------|
| 1 | Oracle spot retreats within the stop-loss buffer of the strike | Sharp reversal. Requires the condition to hold for several consecutive oracle ticks before firing, preventing single noisy ticks from triggering. Fires regardless of whether the token order book is live. |
| 2 | Near expiry with spot already past the strike on the losing side | Final seconds with a losing position u2014 avoids a binary snap to zero. |
| 3 | Token price drops significantly below entry price, with oracle confirmed silent | Oracle is lagging; the CLOB has already repriced a move the oracle hasn't reported. Suppressed near expiry to avoid false fires from thin-book noise. |
| 4 | Token price reaches near-certainty | Capture near-certainty gains before expiry. Pre-armed resting order fills this at near-zero latency. |
| 5 | Oracle tick direction (upfrac EWMA) below threshold for several consecutive windows | Gradual reversal u2014 oracle momentum has shifted against the position before the delta stop-loss threshold is crossed. AUC 0.703. Only evaluated if conditions 1u20134 did not fire. |
| 6 | Expiry | No trigger fired u2014 hold to resolution. |

### Oracle Delta Stop-Loss

The delta stop-loss fires when the Chainlink oracle spot price retreats within the configured buffer of the strike. This is a protective early exit — it exits the position while technically still in-the-money, before the oracle crosses the strike and Polymarket resolves against the position.

**Why oracle price, not token price?** Near expiry, market makers withdraw from the token order book. The CLOB token price can collapse from 80 cents to 60 cents and recover to 85 cents within a single second — purely from book thinning, not from a real adverse move. The delta stop-loss fires on the underlying oracle price, which cannot be distorted by book drain. This is particularly important for NO tokens, whose CLOB order book frequently goes empty near expiry.

**Hysteresis:** The stop requires its condition to hold for a minimum number of consecutive oracle ticks before firing. This prevents a single noisy oracle price print from triggering an exit.

**Per-asset calibration:** Higher-volatility assets require wider buffers because their oracle ticks are larger in absolute terms. Assets like DOGE and HYPE, which have larger average tick sizes, warrant wider buffers than lower-volatility assets.

**Dip-market note:** Some NO markets are “dip” markets — for example, “Will ETH dip to $3,500?” where NO wins when spot stays *above* the strike. The monitor automatically detects this from whether the spot price was above or below the strike at entry time, and applies the stop-loss formula in the correct direction.



### Oracle Tick Direction Signal (Upfrac EWMA)

The fraction of Chainlink oracle ticks that are up-ticks in a rolling window is the strongest single predictive signal in the dataset, with an AUC of 0.703. It cannot be used as an entry signal (the market window hasn’t started when entry decisions are made), but it is the primary real-time exit signal during the hold.

When this fraction — smoothed by an EWMA — drops below the configured threshold for a YES position across several consecutive measurement windows, oracle momentum has shifted downward against the position. For NO positions, the trigger is symmetric: an elevated up-tick fraction signals upward momentum running counter to the NO position.

The EWMA smoothing ensures brief tick clusters do not trigger an exit. A counter resets to zero whenever the condition does not hold, requiring the signal to be sustained for the full configured number of windows.



### Probability-Based Stop-Loss

A secondary CLOB-based stop fires when the held token's price drops significantly below its entry price. This covers the oracle-lag scenario: the CLOB has repriced a move that the Chainlink oracle has not yet reported.

Two guards prevent false fires from order book thinning near expiry:
- **Oracle-lag confirmation:** the stop only fires when the oracle has been confirmed silent for a minimum period. If the oracle is current, any CLOB price drop is treated as book noise, not a genuine adverse move.
- **Near-expiry suppression:** the stop is disabled in the final moments before expiry, when market maker withdrawal makes CLOB prices unreliable.

---

## Position Sizing (Kelly Criterion)

Position size is determined by the fractional Kelly criterion — a sizing method that accounts for both the estimated win probability and the payout ratio.

The strategy estimates the probability of winning based on how far the spot price has moved relative to its expected volatility range (the observed z-score of the delta). A higher delta relative to the vol-scaled threshold implies a higher win probability. The Kelly fraction is then scaled down by a safety factor — full Kelly is too aggressive for prediction markets, where the true win probability cannot be estimated with precision.

If the Kelly calculation produces a negative fraction — meaning the trade has negative expected value after accounting for probabilities and payouts — the entry is skipped entirely.

A minimum time-to-expiry floor is enforced on the Kelly computation to prevent extreme probability estimates very close to expiry.



---

## Oracle Routing

Settlement oracle selection is critical: using the wrong price source creates a mismatch between what the strategy monitors and what Polymarket actually uses when it resolves the market.

| Market Type | Primary Price Source | Fallback |
|-------------|---------------------|----------|
| 5-minute, 15-minute, 4-hour markets | On-chain Chainlink oracle (Polygon blockchain, live events) | Polymarket RTDS Chainlink feed |
| 1-hour, daily, weekly markets | Polymarket RTDS exchange-aggregated feed | — |
| HYPE markets (all durations) | Polymarket RTDS Chainlink feed | — |

Polymarket resolves 5-minute, 15-minute, and 4-hour markets by reading the Chainlink oracle's latest answer directly from its smart contract on the Polygon blockchain at the exact expiry block. The strategy subscribes to on-chain oracle update events, meaning it tracks the exact same price that Polymarket will read at resolution — not a proxy. Oracle updates fire when the price moves at least 0.5% or after a 27-minute heartbeat.

In the final seconds before expiry, the strategy switches to reading the oracle contract directly — ensuring the delta stop-loss and position monitoring are computed from the precise settlement price.

---

## Range Markets Sub-Strategy

The range markets sub-strategy extends the core logic to markets of the form “Will BTC be between $X and $Y?” These markets resolve YES if spot at expiry is inside the price range, and NO if outside.

The primary difference is in how the entry delta is computed. For range markets, the relevant distance is to the **nearest boundary**, not a single strike. The effective delta is the smaller of the distance from the lower bound and the distance from the upper bound — the worst-case proximity to an adverse resolution. Everything else — regime gates, volatility threshold, stop-loss, sizing — applies identically.

Range market positions are tracked separately from directional trades in the interface.



---

## Worked Examples

### YES trade — passes

BTC, 1-hour bucket. The strike is $68,300 and spot is at $68,900 — 0.88% above strike. The YES token is trading at 87 cents. Annualised implied volatility from Deribit is 52%. Time to expiry is 45 minutes.

Scaling that volatility down to the 45-minute horizon gives a threshold of approximately 0.791%. The spot delta of 0.88% exceeds this. Hyperliquid funding rate is strongly negative (a positive signal for YES trades). Crowd book depth share is 62%, above the 40% minimum threshold. All regime gates pass.

**FIRE: Buy YES at 87c.**

---

### YES trade — rejected by funding gate

Same market. Same delta. Hyperliquid funding rate is strongly positive — the perpetual market is strongly long-biased. This opposes the YES trade.

**SKIP: Funding gate blocks YES entry in a strongly long-biased perpetual regime.**

---

### YES trade — rejected by dynamic volatility threshold

BTC, 5-minute bucket. The strike is $68,300 and spot is at $68,500 — 0.29% above strike. YES token at 84 cents. However, annualised implied volatility is 90% (a high-volatility day). Time to expiry is 3 minutes.

Scaling 90% annual volatility down to the 3-minute horizon raises the required threshold to 0.354%. The spot delta of 0.29% falls short.

**SKIP: High implied volatility correctly raises the bar. The delta is insufficient for this vol regime.**

---

### NO trade — passes

ETH, 15-minute bucket. The strike is $3,500 and spot is at $3,481 — 0.54% below strike. The NO token is trading at 83 cents. Annualised implied volatility is 68%. Time to expiry is 8 minutes.

Scaling 68% annual volatility to the 8-minute horizon gives a required threshold of approximately 0.436%. The NO-side delta — the distance from the current spot down toward (and past) the strike — is 0.543%, which exceeds this threshold. Regime gates pass.

**FIRE: Buy NO at 83c.**

---

## Failure Mode Reference

| Scenario | Guard | How It Fires |
|----------|-------|-------------|
| Sharp oracle reversal after entry | Delta stop-loss (global and per-asset buffers) | Oracle ticks approximately once per second; hysteresis requires the condition to hold for 3 consecutive ticks |
| Gradual oracle deterioration (tick direction shifts) | Oracle tick direction signal (upfrac EWMA) | Fires 10–30 seconds before the delta stop-loss threshold in a typical gradual reversal |
| NO token order book drains near expiry | Delta stop-loss is evaluated against the oracle price, not the CLOB | Fires regardless of order book state |
| Oracle lag — CLOB reprices faster than oracle | Probability-based stop-loss (oracle-silence confirmation required) | Only fires when oracle confirmed silent for the configured minimum duration |
| Flat-funding, coin-flip market | Funding rate gate | Skips markets where win rate is approximately 49% |
| Crowd book positioned against direction | Crowd book depth share gate | Skips markets where the order book contradicts the entry side |
| Low-vol sell-off into entry window | TWAP deviation gate | Raises the volatility z-score requirement for YES entries in a low-vol sell-off |
| Marginal delta (one adverse tick crosses strike) | Absolute floor on minimum delta and additional gap requirement | Blocks entries where spot is very close to the boundary |
| High-vol regime (signal noise elevated) | Dynamic vol z-score gate auto-widens threshold | Fewer trades in high-volatility conditions |
| Market priced above 90 cents when scanner runs | Upper price band check | Signal filtered out |
| Thin liquidity at best ask | Minimum order book depth check | Signal skipped |
| Stale oracle or order book | Staleness guards on both oracle price and order book age | Signal skipped |
| Price moved out of band between detection and fill | Pre-execution price re-verification | Order not placed if price has moved |
| Same market held by any strategy | Duplicate guard and cooldown | Entry skipped |
| Bot restarted mid-window for directional market | Window-open spot price persisted to disk | Correct strike restored on restart |
| On-chain oracle feed goes silent | Chainlink silence watchdog triggers automatic reconnect | Fallback price feed serves during reconnect |
| Unfilled entry order times out | Cancel-and-retry loop | Resubmits at slightly higher price; abandons cleanly after maximum retries |
| Kelly computation gives negative expected value | Kelly negative-EV check | Signal skipped and logged |
| Dip-market NO stop-lossed immediately on entry | Delta direction inferred from whether spot was above or below strike at entry | Dip markets use the correct (flipped) delta formula |
| Concurrent position cap reached | Maximum concurrent position check | Signal dropped until a position slot opens |
sdzaxnhjbgybcsdxxxxxxx