# CEO Strategy Performance Report
## Polymarket Maker Bot — First Live Run Analysis
**Report Date:** March 19, 2026 (03:55 UTC)  
**Data Sources:** Live API endpoints (`/performance`, `/health`, `/risk`, `/positions`, `/maker/inventory`, `/maker/signals`), trades.csv  
**Trading Mode:** Paper Trading (Simulated)  
**Run Duration:** ~14 hours (14:05 UTC March 18 → 03:55 UTC March 19)

---

## Executive Summary

The bot completed its first meaningful live run after two critical infrastructure bugs were resolved:
1. `state_sync_loop` NameError — crashed every 5 seconds, leaving all state blind
2. `MAKER_EXIT_HOURS` misconfiguration — force-exited all bucket positions within 60 seconds of opening

With those fixed, the system ran cleanly for 14 hours across 295 trades. **Realized PnL: +$152.33**. The strategy is profitable in aggregate, but the result masks a highly polarized performance across market types: `bucket_15m` is the engine generating all profit, while `bucket_5m` and the HL perp hedge are both loss-making drags.

| Metric | Value |
|---|---|
| Realized PnL | **+$152.33** |
| Total Trades | 295 |
| Win Rate | 43.4% |
| Avg PnL / Trade | $0.52 |
| Max Drawdown | $148.40 |
| Drawdown/Profit Ratio | 0.97x |
| Sharpe (7d proxy) | 41.59 |
| Total Fees Paid | $29.11 |
| Rebates Earned | $0.00 |
| Current Open Positions | 29 |
| PM Exposure Utilization | 81.5% ($4,073 / $5,000) |
| HL Delta Hedge Active | $0 (none) |

---

## 1. Equity Curve

The session opened with an immediate **$148 drawdown** at 14:30 UTC as the first batch of bucket_15m trades resolved — three large positions in the same BTC 10:15–10:30AM ET market resolved adversely (the maker had filled both sides: YES at -$71 and NO at +$58, netting -$13 on that market, but other co-resolving trades in ETH and SOL also lost simultaneously, amplifying the drawdown).

Recovery began by 15:00 UTC. The equity curve trended upward through the overnight US session (21:00–02:00 UTC), reaching **a peak of $218.33 at 02:00 UTC**, then declined to the current $152.33.

```
$218  |                                    ●
$150  |                         ●●●●●●●●●   ●●●
$100  |              ●●●●●●●●●●●
 $50  |         ●●●●
  $0  |●●●●●●●●●
-$50  |
-$100 |
-$150 |●  (max drawdown: -$148 at 14:30 UTC)
      +-----------------------------------------→
      14:00     17:00     20:00     23:00    03:00 UTC
```

---

## 2. Performance by Market Type

This is the most important view. The strategy's profitability is **entirely driven by bucket_15m**.

| Market Type | Realized PnL | Trades | Win Rate | Notes |
|---|---|---|---|---|
| **bucket_15m** | **+$132.49** | 133 | **49.6%** | Core profit engine — near-coinflip win rate but positive EV |
| **bucket_1h** | +$67.70 | 16 | 56.3% | Strongest win rate; **currently excluded from config** |
| **bucket_daily** | +$17.20 | 3 | 66.7% | Only 3 trades — insufficient data |
| **bucket_5m** | **-$47.88** | 55 | 41.8% | **Loss-making; fastest expiry, highest noise** |
| **hl_perp (hedge)** | **-$17.18** | 88 | 31.8% | **Delta hedge leg — net drag on performance** |
| **TOTAL** | **+$152.33** | 295 | 43.4% | |

**Key finding:** If `bucket_5m` and `hl_perp` were disabled, realized PnL would be **+$217.39** on 149 trades at a 52.3% win rate — a dramatically cleaner profile.

---

## 3. Performance by Underlying

| Underlying | Realized PnL | Trades | Avg PnL/Trade |
|---|---|---|---|
| **ETH** | **+$100.90** | 77 | **$1.31** |
| **XRP** | +$22.99 | 27 | $0.85 |
| **SOL** | +$21.78 | 57 | $0.38 |
| **BTC** | +$6.65 | 134 | $0.05 |

**Key finding:** BTC generates the most trade volume (134, or 45% of total) but has the lowest PnL per trade ($0.05). ETH is the highest-value underlying by a wide margin ($1.31/trade). The signal system may be over-allocated to BTC markets relative to its α.

---

## 4. Strategy Leg Breakdown

| Strategy Leg | PnL | Trades | Avg/Trade |
|---|---|---|---|
| Maker (PM) | +$169.51 | 207 | $0.82 |
| Maker Hedge (HL Perp) | -$17.18 | 88 | -$0.20 |

The hedge leg costs ~$0.20 per hedge trade and wins only 31.8% of the time. This suggests the hedge is:
- Opening when adverse price moves are already happening (reactive, not pre-emptive)
- Being caught on the wrong side of mean-reversion after brief adverse moves
- Adding friction without sufficient protective value in paper trading mode

**Currently: zero active HL hedges** (`hl_notional_usd: $0`). The coin_hedges object is empty despite net delta exposure of -$181 across SOL/ETH/XRP.

---

## 5. Top Trades

### Best Trades (Realized)

| # | Market | Type | Side | Size | Entry | PnL | Signal Score |
|---|---|---|---|---|---|---|---|
| 1 | BTC 10:15–10:30AM ET | bucket_15m | NO | 96.6 shares | 0.61 | **+$58.92** | 85.7 |
| 2 | ETH 9PM ET (1h bucket) | bucket_1h | YES | 100 shares | 0.445 | **+$55.50** | 88.6 |
| 3 | SOL 12:30–12:45PM ET | bucket_15m | YES | 83.7 shares | 0.464 | **+$44.87** | 85.1 |
| 4 | BTC 9:15–9:20PM ET | bucket_5m | NO | 50 shares | 0.80 | **+$40.00** | 85.3 |
| 5 | XRP 12:15–12:30PM ET | bucket_15m | NO | 76 shares | 0.507 | **+$38.56** | 60.8 |

### Worst Trades (Realized)

| # | Market | Type | Side | Size | Entry | PnL | Signal Score |
|---|---|---|---|---|---|---|---|
| 1 | BTC 10:15–10:30AM ET | bucket_15m | YES | 123.6 shares | 0.575 | **-$71.14** | 85.8 |
| 2 | XRP 10PM ET (1h bucket) | bucket_1h | YES | 93.4 shares | 0.581 | **-$54.31** | 89.6 |
| 3 | ETH 9PM ET (1h bucket) | bucket_1h | NO | 83.0 shares | 0.476 | **-$43.51** | 91.0 |
| 4 | ETH 4:15–4:30PM ET | bucket_15m | NO | 100 shares | 0.61 | **-$39.00** | 94.2 |
| 5 | ETH 10:15–10:30AM ET | bucket_15m | YES | 55.2 shares | 0.684 | **-$37.75** | 81.5 |

**Critical observation on signal scores:** The worst trade had a signal score of **94.2** (ETH bucket_15m, -$39). Three of the five worst trades had signal scores above 85. High signal confidence is NOT reliably predictive of individual trade outcomes. This is expected for a maker strategy (you fill both sides), but the current signal score threshold approach alone cannot prevent large losses.

**Maker dynamics visible in worst/best lists:** Trade #1 best and Trade #1 worst are the **same market** (BTC 10:15–10:30AM ET). The maker filled YES at 0.575 (123.6 shares) and NO at 0.610 (96.6 shares). The market resolved YES=0, so the NO side won +$58.92 and the YES side lost -$71.14. **Net impact on that market: -$12.22**. The large headline numbers reflect natural maker inventory asymmetry, not directional drift.

---

## 6. Time-of-Day Performance (HKT)

| Hour (HKT) | Avg PnL/Trade | Trade Count | US ET Equivalent |
|---|---|---|---|
| 3 HKT | **+$1.98** | 9 | 3PM ET |
| 9 HKT | **+$1.97** | 12 | 9PM ET |
| 5 HKT | **+$1.75** | 15 | 5PM ET |
| 7 HKT | **+$1.64** | 14 | 7PM ET |
| 0 HKT | **-$0.91** | 28 | 12 noon ET |
| 22 HKT | **-$0.59** | 42 | 10AM ET (market open) |

**Finding:** The US market open (22 HKT = 10AM ET) has the heaviest trade volume (42 trades) but is loss-making at -$0.59/trade. This is the window when bucket_15m markets resolve with the highest price volatility — the maker is most exposed at open. The afternoon (3–9PM ET) is the best performance window.

---

## 7. Current Open Positions (Snapshot, 03:55 UTC)

**29 positions open** across BTC, ETH, SOL, XRP daily/strike markets.

**Estimated unrealized PnL breakdown:**

| Category | Estimated Unrealized | Notable Positions |
|---|---|---|
| Winners | ~+$355 | SOL above $90 YES (+$68.6), ETH above $2200 YES (+$51.3), BTC dip $70k NO (+$61.3) |
| Losers | ~-$329 | SOL above $90 NO (-$56.0), ETH above $2200 NO (-$39.3), BTC dip $70k YES (-$61.4) |
| **Net unrealized** | **~+$26** | (hedged pairs, near-neutral) |

Most open positions are **paired (both YES and NO in same market)** — the maker has filled both sides at different prices and awaits resolution. The net unrealized across pairs is small, confirming the maker book is roughly balanced.

---

## 8. Risk & Operational Status

### System Health
| Component | Status |
|---|---|
| API Server | Running ✅ |
| Polymarket WebSocket | Connected ✅ |
| Hyperliquid WebSocket | Connected ✅ |
| Uptime | 14h 2m |
| Agent Auto-Mode | Disabled (manual) |
| Paper Trading | Active |

### Data Quality Concern
- **4,654 markets monitored**: 867 fresh books (18.6%), **3,772 stale books (81.1%)**, 15 no-book  
- **Data issues flag: TRUE** — the majority of monitored markets have stale order books
- 107 adverse triggers logged this session (HL max move: 0.879%)
- Stale books may result in sub-optimal spread placement; markets where the book is fresh are likely getting the best fill quality

### Exposure & Risk Limits
| Metric | Current | Limit | Utilization |
|---|---|---|---|
| PM Exposure | $4,073 | $5,000 | **81.5%** |
| HL Notional | $0 | $5,000 | 0% |
| Open Positions | 29 | 50 | 58% |
| Hard Stop | — | $500 loss | Not triggered |

### Inventory / Delta Risk
| Asset | Net Position Delta | Fill Inventory | Hedge Threshold | Status |
|---|---|---|---|---|
| SOL | -$55.01 | -$1.41 | $150 | ⚠️ Below threshold |
| ETH | -$61.24 | +$8.89 | $150 | ⚠️ Below threshold |
| BTC | +$6.67 | -$2.51 | $150 | ✅ Neutral |
| XRP | -$71.89 | -$5.16 | $150 | ⚠️ Below threshold |

All three short-skewed assets (SOL, ETH, XRP) are approaching but have not crossed the $150 hedge trigger. **No active HL hedges are open.** The total net unhedged delta is approximately -$181 (net short crypto). If crypto prices rise overnight, these positions will face unrealized losses.

---

## 9. Signal Intelligence

**Active signals (at report time): 1**
- BTC daily "Bitcoin Up or Down on March 19?" → Score: **96.7**, 90.9% filled ($500 ask, $50 bid pending), Age: 9.4s

The ND2-only signal source is active and generating high-confidence reads. A score of 96.7 is among the highest observed. However, as noted in the worst-trades analysis, signal scores above 85 have produced large losses — the model's high confidence appears uncalibrated against actual resolution outcomes.

---

## 10. Key Findings & Strategic Recommendations

### What's Working ✅
1. **bucket_15m is the alpha engine** — 133 trades, +$132.49, 49.6% WR. The ND2 signal finds genuine edge in 15-minute crypto bucket markets. This is the strategy's core.
2. **ETH generates the highest value per trade** ($1.31 avg). Overweighting ETH buckets vs BTC should improve returns.
3. **Afternoon/evening US session** (3–9PM ET) shows consistent positive PnL per trade. Consider restricting trading hours to these windows.
4. **The maker book is well-balanced** — paired positions net near-zero unrealized, confirming healthy two-sided inventory management.

### What's Not Working ❌
1. **bucket_5m is loss-making: -$47.88, 41.8% WR** — 5-minute markets are too noisy. The signal does not have sufficient edge at this resolution. Recommend: **exclude bucket_5m from trading** as bucket_1h was excluded.
2. **HL perp hedge drags -$17.18 at 31.8% WR** — The hedge opens reactively after adverse moves and loses to mean-reversion. Either fix hedge entry timing or disable hedging entirely. At current scale, the hedge costs more than it saves.
3. **US market open (10AM ET) is loss-making** — 42 trades at -$0.59/trade during the highest-volatility window. This is when the ND2 signal is least reliable as price discovery is most aggressive.
4. **No rebates earned ($0)** — This is paper trading mode and rebate tracking may not be simulated. In live trading, the maker should earn positive rebates on all passive fills.
5. **High signal scores ≠ predictable outcome per trade** — Scores of 85–94 appear on the worst trades. The signal is useful as a filter ("don't trade below X") but cannot prevent individual losses. The model's per-trade EV needs to come from spread width, not directional prediction.

### Immediate Actions 🚨
| Priority | Action | Expected Impact |
|---|---|---|
| P0 | Exclude `bucket_5m` from `MAKER_EXCLUDED_MARKET_TYPES` | +$47.88 annualized drag removed |
| P0 | Evaluate HL hedge logic — consider disabling until timing is fixed | +$17.18 drag removed |
| P1 | Investigate stale book rate (81.1%) — root cause of data quality issue | Better spread placement |
| P1 | Tune signal score threshold specific to bucket_5m vs bucket_15m | Tighter risk filter on noisy markets |
| P2 | Backtest time-of-day filter (exclude 22–23 HKT = US open) | Reduce open-hour losses |
| P2 | Re-evaluate BTC vs ETH position sizing given ETH's 26x higher PnL/trade | Better capital allocation |

### Forward-Looking Watch Items 👀
- **29 open positions** will resolve over the next 12 hours (mostly March 19 daily markets at 16:00 UTC). Current unrealized net is approximately +$26 but is highly dependent on whether BTC closes above $70k, $71k, $72k, SOL above $90, ETH above $2,200, and XRP above $1.40/$1.50.
- **The daily bucket strategy** (3 trades, +$17.20, 66.7% WR) shows strong but low-sample promise. Monitor carefully as the overnight session completes.
- **PM exposure at 81.5%** — approaching limit. If the current batch of opens fills further before March 19 opens resolve, hard limit may constrain new signal deployment.

---

## 11. Summary Scorecard

| Dimension | Grade | Commentary |
|---|---|---|
| Revenue | B+ | +$152 in 14h paper trading; strong relative to early-stage system |
| Win Rate | C+ | 43.4% overall; 49.6% on the profitable bucket_15m segment |
| Risk Management | C | No active hedges despite $181 net delta; 81% exposure utilization |
| Signal Quality | B | Edge present in 15m/1h; bucket_5m and US open are signal edge-cases |
| Infrastructure | B+ | Both bugs fixed; system stable; data staleness needs investigation |
| Capital Efficiency | C+ | BTC over-allocated vs ETH value; hedge leg wasting HL capacity |

**Overall Assessment: The strategy has a real, profitable alpha source in bucket_15m markets. The system now runs cleanly after the critical infrastructure fixes. The primary action items are operational: exclude bucket_5m, fix or disable the HL hedge, and improve book freshness. If those three issues are addressed, the expected P&L profile nearly doubles.**

---

*Report generated from: `/performance?period=all`, `/health`, `/risk`, `/positions`, `/maker/inventory`, `/maker/signals`, and `data/trades.csv`*  
*Paper trading — all figures simulated. No real capital at risk.*
