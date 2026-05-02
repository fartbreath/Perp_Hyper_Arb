# Open Market Entry Strategy

## Core Concept

Enter a **single directional position** at T=0 when a 5-minute bucket opens, using pre-open oracle and perp signals that predict direction when the token is priced near $0.50. Hold for the full 5 minutes, exit adaptively using oracle delta SL and PM CLOB trajectory signals.

> **Complements Momentum, not competes.** Momentum requires tokens at 80–90c with near-expiry time decay. Open enters at ~50c using independent signal architecture. When both strategies are in the same market at the same time, they are in different hold regimes — Momentum is in the last 60–120 seconds; Open has already been running for 3–4 minutes.

---

## P&L Math

```
Entry:          BUY YES (or NO) @ ~$0.50
Target exit:    Token approaches $0.92+  →  +$0.42 per $1 notional
Stop-loss:      Oracle delta SL (per-coin) or cl_upfrac deterioration
Net per win:    ~+$0.42  (at 60.2% win rate: EV ≈ +$0.25 − costs)
Net per loss:   ~−$0.30  (oracle SL fires before full $0.50 loss)
Break-even:     52.7% win rate (at median 6.1% spread)
```

**Validated win rates by regime (from strategy_update.md §2.2, single-day dataset May 1 2026):**

| Signal Regime | Direction | n | Win Rate | EV/Trade |
|---|---|---|---|---|
| Extreme negative funding | YES | 21 | 76.2% | +36.4% |
| Positive funding (confirmed) | NO | 138 | 62.3% | +9.5% |
| Composite score ≥ 2 | YES | 166 | 60.2% | +2.9% |

> ⚠️ **In-sample inflation warning.** These rates come from a single day (681 windows). Do a time-series split (train on first 340, test on last 341) before deploying live capital. If out-of-sample win rate < 54%, raise `OPEN_ENTRY_MIN_SCORE` from 2 to 3.

---

## Why It Works

### Pre-Open Signals Are Genuinely Predictive

Three independent signals each beat the 52.7% break-even threshold:

1. **Funding rate** (strongest): HL perpetual funding reflects where leveraged money is positioned. When funding is strongly negative (longs getting paid), the spot oracle is more likely to move UP — binary YES wins. Win rate 76.2% (n=21) for extreme negative funding. 62.3% (n=138) for positive funding → NO side.

2. **Hour-of-day bias**: Specific UTC hours have directional tendencies in the dataset. Hours 11, 12, 13, 15 favour YES; hours 14, 16, 18 favour NO. Likely reflects US session open dynamics and afternoon selling pressure.

3. **PM CLOB depth share**: When YES bid depth > 75% of total, crowd is positioned long → NO more likely to win (crowd is wrong at extremes). AUC 0.5683, p=0.002.

No single signal is used alone. The composite score gates entries to windows where 2+ signals agree, filtering out noise entries where signals conflict.

### Why Composite Score ≥ 2 Is the Right Gate

- Score ≥ 1 (any single signal): ~52–55% win rate — barely above break-even, erased by hedge costs.
- Score ≥ 2 (two agreeing signals): 60–62% win rate — above break-even after costs.
- Score ≥ 3 (extreme funding): 76.2% — but n=21, subject to small-sample sizing rule (no multiplier until 50 forward samples).

The score architecture ensures the strategy only enters when multiple independent sources agree.

### Why It Is Distinct From Momentum

| Dimension | Momentum | Open |
|---|---|---|
| Entry token price | 80–90c | 45–65c |
| Time to expiry at entry | 30–120s | 270–300s |
| Primary signal | Oracle price vs strike | Pre-open composite score |
| Hold regime | Near-expiry time-decay | Full 5-minute bucket |
| Exit signal | Delta SL + cl_upfrac EWMA | Delta SL + cl_upfrac + PM bid trajectory |
| Number of coins active simultaneously | Up to 7 | Max 2 (correlated group cap) |

---

## Core Agent Rules

Read before every task involving this strategy.

1. **Read `OPEN_IMPL_PLAN.md` before making any code changes.** The plan is the source of truth for what phases are built, what is disabled by default, and what requires validation data.

2. **SL is oracle-driven, not CLOB-driven.** The PM YES bid will naturally collapse for a losing position — do not use CLOB collapse as the primary SL trigger. Oracle delta SL is the hard floor; `cl_upfrac` EWMA and PM bid are the early warning system. This is identical to the Momentum SL architecture.

3. **The GTD hedge stays on all Open positions** (per `strategy_update.md §3.3`). The full 5-minute hold is exactly when the hedge earns its premium on gradual oracle losses. Do not remove it. Measure its actual cost in the first 10 paper trades and raise `OPEN_ENTRY_MIN_SCORE` to 3 if cost exceeds 1.5% of notional.

4. **Score ≥ 3 entries (extreme negative funding, n=21) use base size only.** High composite score ≠ high n. Do not apply `OPEN_FUNDING_SIZE_MULTIPLIER` until 50 forward samples confirm ≥ 70% out-of-sample win rate for this regime. Until then: standard `OPEN_MAX_POSITION_USD_BY_COIN` cap, no multiplier.

5. **Correlated group cap: at most 1 YES and 1 NO simultaneously from {BTC, ETH, SOL, BNB, HYPE}.** These 5 coins move together on macro events. Two simultaneous YES positions in BTC + ETH are a single macro bet, not two independent trades. DOGE and XRP may run independently.

6. **Cooldown is per (coin, bucket_start_ts), not wall-clock.** If an entry stops out at T+30s, the cooldown expires at bucket end — not 300 wall-clock seconds later. This prevents double-entry within the same bucket while allowing re-entry in the next.

7. **Do not enable on live capital before paper trading passes.** The in-sample win rates (60–76%) are from a single day and are inflated. `OPEN_STRATEGY_ENABLED` defaults to `false`. Enable paper trades first; validate out-of-sample win rate ≥ 55% before any live capital.

8. **Do not enter DOGE or HYPE until slippage is measured.** Thin books mean a $30 market entry can move the ask by 2–5 cents. At a 3c adverse fill, the break-even rises from 52.7% to ~54.5%. Paper-trade DOGE/HYPE first to measure actual vs quoted fill prices before enabling live for those coins.

9. **`OPEN_ENTRY_MIN_SCORE` is the primary safety dial.** If out-of-sample win rate drops below 54% in validation, raise the min score from 2 to 3 before any further changes. Do not adjust the score components themselves — adjust the threshold.

---

## Composite Entry Score

Computed from pre-open data available at T=0 (or T−1s). All inputs come from cached WebSocket state — zero REST calls at entry time.

```python
SCORE = 0

# Funding rate (primary gate, highest weight)
if funding_rate < -OPEN_FUNDING_GATE_NO_MIN and direction == "YES":  SCORE += 3
if funding_rate > +OPEN_FUNDING_GATE_YES_MAX and direction == "NO":  SCORE += 2
if abs(funding_rate) < OPEN_FUNDING_GATE_YES_MAX:                    SCORE -= 1  # penalise neutral

# Hour-of-day bias (from OPEN_HOUR_SCORE_MAP)
SCORE += OPEN_HOUR_SCORE_MAP.get(str(utc_hour), {}).get(direction, 0)

# CL velocity — oracle trending in entry direction in prior 60s
if cl_vel_bps_60s agrees with direction:    SCORE += 1

# PM CLOB depth share
if yes_depth_share > 0.75 and direction == "NO":   SCORE += 1
if yes_depth_share < 0.25 and direction == "YES":  SCORE += 1

# TWAP (low-vol regime only)
if cl_vol_60s < vol_median:
    if cl_twap_10_dev > 0 and direction == "YES":  SCORE += 1
    if cl_twap_10_dev < 0 and direction == "NO":   SCORE += 1

# Streak bias (DOGE only)
if coin == "DOGE" and last_2_outcomes == ["NO", "NO"] and direction == "YES":  SCORE += 2

ENTER if SCORE >= OPEN_ENTRY_MIN_SCORE  (default: 2)
```

---

## Entry Conditions

All must pass for an entry to be placed.

| Gate | Type | Threshold | Rationale |
|---|---|---|---|
| Market type | Static | 5m/15m buckets only | Only these have known oracle-based resolution via Chainlink |
| Strategy enabled | Static | `OPEN_STRATEGY_ENABLED = true` | Master on/off |
| Entry window | Dynamic | T=0 to T+`OPEN_ENTRY_WINDOW_SECONDS` (10s) | Near-50/50 pricing expires quickly |
| Price band | Dynamic | Token ask between `0.40` and `0.65` | Excludes already-biased markets |
| Composite score | Dynamic | ≥ `OPEN_ENTRY_MIN_SCORE` (2) | Multi-signal agreement required |
| Correlated group cap | Dynamic | ≤ 1 active same-direction from {BTC,ETH,SOL,BNB,HYPE} | Prevents macro-correlated position clustering |
| Concurrent cap | Dynamic | Active Open positions < `OPEN_MAX_CONCURRENT` (2) | Separate from Momentum count |
| Market cooldown | Dynamic | No entry in same (coin, bucket) already attempted | Per-bucket dedup guard |
| Depth-adjusted size | Dynamic | Capped at 0.5% of total YES+NO bid depth | Prevents self-impact on thin DOGE/HYPE books |

---

## Exit Conditions

Priority-ordered. First trigger that fires wins.

| Priority | Condition | Action |
|---|---|---|
| 1 | Token bid ≥ `OPEN_PROFIT_TARGET` (0.88) | Market sell — near-settlement profit lock |
| 2 | Oracle delta SL: CL spot declined > `OPEN_DELTA_SL_PCT_BY_COIN[coin]` from strike | Market sell — hard floor |
| 3 | `cl_upfrac_during` < 0.45 for ≥ 2 consecutive 5s windows (EWMA α=0.3) | Resting limit sell — oracle momentum has stalled |
| 4 | PM YES bid < `OPEN_PM_BID_EXIT_THRESHOLD_YES` (0.450) at T+10s AND oracle also declining | Double-trigger early exit — crowd + oracle both against position |
| 5 | T+270s | Mandatory exit if still open (3s before settlement window) |

**PM bid exit caveat:** The `OPEN_IMPLIED_PROB_SL_ENABLED` threshold of 0.38 is **not data-derived**. Compute from the dataset: (1) 10th percentile of implied_prob at T+10s for winning windows (threshold must be below this), (2) 90th percentile for losing windows (threshold must be above this). Replace 0.38 with empirically-derived values before enabling.

---

## Configuration

All parameters in `config.py` under the `OPEN_*` namespace.

| Config Key | Default | Description |
|---|---|---|
| `OPEN_STRATEGY_ENABLED` | `false` | Master on/off. Keep false until paper validation passes |
| `OPEN_ENTRY_MIN_SCORE` | `2` | Minimum composite score required to enter. Primary safety dial |
| `OPEN_MAX_ENTRY_USD` | `30.0` | Maximum position size in USDC |
| `OPEN_PRICE_BAND_LOW` | `0.40` | Minimum token ask price at entry |
| `OPEN_PRICE_BAND_HIGH` | `0.65` | Maximum token ask price at entry |
| `OPEN_ENTRY_WINDOW_SECONDS` | `10` | Max seconds after bucket open to place entry |
| `OPEN_MAX_CONCURRENT` | `2` | Max simultaneous Open positions (separate from Momentum count) |
| `OPEN_MARKET_COOLDOWN_SECONDS` | `300` | Cooldown per (coin, bucket_start_ts) after any entry attempt |
| `OPEN_FUNDING_GATE_YES_MAX` | `0.00001` | Skip YES if funding > this (crowd short → oracle goes down) |
| `OPEN_FUNDING_GATE_NO_MIN` | `-0.00001` | Skip NO if funding < this (crowd long → oracle goes up) |
| `OPEN_DEPTH_SHARE_YES_MIN` | `0.40` | Minimum YES bid depth share to enter YES |
| `OPEN_DEPTH_SHARE_NO_MAX` | `0.60` | Maximum YES bid depth share to enter NO |
| `OPEN_DELTA_STOP_LOSS_PCT` | `0.05` | Default oracle delta SL (fallback if not in per-coin map) |
| `OPEN_DELTA_SL_PCT_BY_COIN` | See below | Per-coin oracle delta SL thresholds |
| `OPEN_UPFRAC_EXIT_THRESHOLD` | `0.45` | cl_upfrac threshold below which exit is triggered |
| `OPEN_UPFRAC_EXIT_WINDOWS` | `2` | Consecutive windows below threshold before exit fires |
| `OPEN_PM_BID_EXIT_THRESHOLD_YES` | `0.450` | YES bid floor at T+10s; combined with oracle for early exit |
| `OPEN_PM_BID_EXIT_THRESHOLD_NO` | `0.450` | NO bid floor at T+10s |
| `OPEN_PROFIT_TARGET` | `0.88` | Token bid level at which to take profit |
| `OPEN_FUNDING_SIZE_MULTIPLIER` | `1.0` | Size multiplier when funding is extreme. Keep at 1.0 until n≥50 confirms ≥70% win rate |
| `OPEN_DEPTH_ADJUSTED_SIZING_ENABLED` | `true` | Cap position size to 0.5% of book depth |
| `OPEN_MAX_DEPTH_FRACTION` | `0.005` | Fraction of total depth for depth-adjusted cap |
| `OPEN_MAX_CORRELATED_DIRECTION_POSITIONS` | `1` | Max same-direction positions from correlated group simultaneously |
| `OPEN_CORRELATED_COIN_GROUPS` | `[["BTC","ETH","SOL","BNB","HYPE"]]` | Correlated coin groups for directional cap |
| `OPEN_IMPLIED_PROB_SL_ENABLED` | `false` | Enable PM implied-prob stop. Default off until threshold calibrated |
| `OPEN_IMPLIED_PROB_SL_THRESHOLD_YES` | `0.38` | NOT data-derived. Must be computed from dataset before enabling |
| `OPEN_IMPLIED_PROB_SL_THRESHOLD_NO` | `0.38` | NOT data-derived. Must be computed from dataset before enabling |

**Per-coin delta SL values:**
```json
"OPEN_DELTA_SL_PCT_BY_COIN": {
    "BTC": 0.04, "ETH": 0.05, "SOL": 0.06,
    "XRP": 0.07, "BNB": 0.05, "DOGE": 0.10, "HYPE": 0.12
}
```

**Hour score map (UTC):**
```json
"OPEN_HOUR_SCORE_MAP": {
    "11": {"YES": 1}, "12": {"YES": 1}, "13": {"YES": 1},
    "14": {"YES": -1, "NO": 1},
    "15": {"YES": 1},
    "16": {"YES": -2, "NO": 2},
    "17": {},
    "18": {"YES": -2, "NO": 2},
    "19": {"NO": 1}
}
```

**Per-coin max position:**
```json
"OPEN_MAX_POSITION_USD_BY_COIN": {
    "BTC": 30.0, "ETH": 30.0, "SOL": 20.0,
    "XRP": 20.0, "BNB": 20.0, "DOGE": 10.0, "HYPE": 15.0
}
```

---

## Risk Architecture

### Shared Exposure with Momentum

Open positions share the `risk.py` exposure limits. A $30 Open position in BTC + a $20 Momentum position in BTC = $50 combined notional. `MAX_PER_COIN_NOTIONAL_USD = 50` is the combined cap. The risk module must sum Open + Momentum notionals per coin at entry time — not just count positions.

### GTD Hedge Accounting

The GTD hedge applies identically to Open positions as to Momentum. The hedge is placed on the opposite token. On WIN exits, cancel the hedge. On LOSS exits, keep the hedge alive — it may partially recover. See preamble `## GTD Hedge Accounting` for full rules.

### Correlated Group Risk

{BTC, ETH, SOL, BNB, HYPE} move together on macro events. The correlated group cap prevents accumulating more than 1 YES and 1 NO position from this group simultaneously. DOGE and XRP are independent and may run alongside the group.

---

## Implementation Architecture

```
strategies/Open/
├── __init__.py
├── scanner.py      # OpenScanner: composite score, T=0 entry, exit management
├── signal.py       # compute_entry_score(): funding + hour + velocity + depth + TWAP + streak
└── PLAN.md         # (future: original design doc)
```

**Data sources (all WebSocket — zero polling):**
- `HLClient.get_funding_rate(coin)` — funding rate from HL WS `webData2` subscription
- `PMClient.get_depth_share(market_id)` — YES bid depth share from PM CLOB WS cache
- `OracleTickTracker.get_velocity_bps(coin, window=60)` — CL oracle velocity from Chainlink WS
- `OracleTickTracker.get_twap_deviation(coin, window=10)` — TWAP deviation from oracle tick stream
- `OracleTickTracker.get_vol_ratio(coin)` — rolling vol vs session median

**Requires Momentum Phase 1 pipelines before building:**
- `FundingRateCache` (§1.1 of `MOMENTUM_IMPL_PLAN.md`) — funding rate access
- `PMClient.get_depth_share()` (§1.2) — depth share
- `OracleTickTracker` (§1.3) — CL velocity, TWAP, vol

---

## Current Development Phase

> Full detail in `OPEN_IMPL_PLAN.md`. Source of all validated decisions: `strategy_update.md` Part 2.

| Phase | Status | Description |
|---|---|---|
| Scaffold | 🔲 Not built | `strategies/Open/` directory, `OpenScanner`, `signal.py` |
| P0: Signal compute | 🔲 Not built | `compute_entry_score()` with all components; requires Phase 1 pipelines |
| P0: Entry gating | 🔲 Not built | Price band, score gate, concurrent cap, correlated group cap, cooldown |
| P0: Exit management | 🔲 Not built | Delta SL, cl_upfrac EWMA, profit target, T+270s mandatory exit |
| P0: Paper trades | ⏳ Pending | 5-day paper trade with `OPEN_STRATEGY_ENABLED=true, DRY_RUN=true` |
| P1: Out-of-sample validation | ⏳ Pending | Time-series split on existing dataset; raise min_score if < 54% |
| P1: Slippage measurement | ⏳ Pending | Measure DOGE/HYPE fill prices before enabling those coins live |
| P2: Live capital | ⏳ Pending | Enable only after paper validation passes; start with BTC/ETH only |

---

## Implementation Checklist

### Pre-build requirements

- [ ] Momentum Phase 1 pipelines live (`FundingRateCache`, `get_depth_share()`, `OracleTickTracker`)
- [ ] Dataset time-series split run (train/test split) to calibrate `OPEN_ENTRY_MIN_SCORE`

### To build (P0 — first paper trades)

- [ ] `strategies/Open/__init__.py`
- [ ] `strategies/Open/signal.py` — `compute_entry_score()` with all 6 signal components
- [ ] `strategies/Open/scanner.py` — `OpenScanner` with T=0 entry logic
- [ ] Composite score gate at entry evaluation
- [ ] Entry window guard (T=0 to T+10s)
- [ ] Price band gate (0.40–0.65)
- [ ] Correlated group cap check at entry
- [ ] Per-bucket cooldown tracker (keyed by coin + bucket_start_ts)
- [ ] Delta SL (oracle-driven, per-coin thresholds)
- [ ] cl_upfrac EWMA exit (α=0.3, threshold 0.45, 2 consecutive windows)
- [ ] Profit target exit (bid ≥ 0.88)
- [ ] T+270s mandatory exit
- [ ] GTD hedge placement and accounting (identical to Momentum)
- [ ] Fills logging (entry_score, funding_rate, yes_depth_share, cl_vel_bps_60s, utc_hour)
- [ ] `/open/status` API endpoint

### To build (P1 — after paper validation)

- [ ] PM implied-prob SL (calibrate threshold from dataset before enabling)
- [ ] `OPEN_FUNDING_SIZE_MULTIPLIER` — enable only after 50+ forward samples confirm win rate
- [ ] DOGE/HYPE: measure slippage, then enable if below threshold
- [ ] Out-of-sample dataset split validation

### Live capital gate (P2)

- [ ] 5-day paper trade: win rate ≥ 55%, score distribution matches expected ~15–25% of windows
- [ ] GTD hedge cost measured < 1.5% of notional per trade
- [ ] Correlated group cap verified working (no simultaneous BTC+ETH YES)
- [ ] Start BTC/ETH only; expand to other coins after 10 live trades
