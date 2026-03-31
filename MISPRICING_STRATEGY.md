# Mispricing Strategy

## Overview

The mispricing strategy compares **Polymarket (PM) binary outcome market prices** against
an external fair-value reference to identify mispricings worth trading.

The strategy operates a **two-layer signal filter**:

| Layer | Source | Signal | Status |
|-------|--------|--------|---------|
| **Layer 1 (primary)** | Kalshi market price | `\|PM − Kalshi\|` | Active when `KALSHI_ENABLED=True` |
| **Layer 2 (confirmation)** | Deribit N(d₂) | `\|PM − N(d₂)\|` | Used to confirm Layer 1 direction |

When Kalshi is enabled, a signal only fires when:
1. A matching Kalshi market exists for the same underlying/strike/expiry
2. The |PM − Kalshi| spread exceeds `KALSHI_MIN_DEVIATION`
3. (If `KALSHI_REQUIRE_ND2_CONFIRMATION=True`) The N(d₂) direction agrees

When Kalshi is disabled, the strategy falls back to N(d₂)-only mode (see limitations).

> ⚠️ See **Known Flaws** below for a full breakdown of the N(d₂)-only limitations and
> why Kalshi confirmation materially improves signal quality.

## Core Idea

A Polymarket binary market on "Will BTC reach $120k by end of Q2?" resolves YES if the
high of any Binance 1-minute candle during the period **ever touches or exceeds** the
strike. This is a **barrier-hit (one-touch) probability**, not a terminal price
probability.

Deribit's Black-Scholes N(d₂) gives the **terminal probability** P(S_T ≥ K) — the
chance the price is *above* the strike at *exactly expiry*. For a volatile asset like
BTC, the one-touch probability is structurally higher, often by 20–40 percentage points
for OTM upside strikes.

The strategy attempts to use this Deribit signal as a fair-value benchmark for PM prices
anyway, on the premise that PM participants may misprice even this structurally-different
quantity by enough to exceed fees. This is a bet on **relative sentiment** between the
two markets, not a riskless arbitrage.

> ⚠️ See **Known Flaws** below for a full breakdown of why the signal is systematically
> biased and what that means in practice.

---

## Kalshi Confirmation Layer

Kalshi and Polymarket both list markets on identical crypto price events  
(e.g. "Will BTC close above $90k on March 31?"). Kalshi's public REST API  
(`/trade-api/v2/markets`) requires no authentication for read-only price data.

### Why Kalshi prices are a better anchor than N(d₂)

| Aspect | N(d₂) only | Kalshi-confirmed |
|--------|-----------|------------------|
| **Formula mismatch** | N(d₂) = terminal probability; PM = barrier/touch → structural bias of 20–40% | Both prices are for same event type → apples-to-apples comparison |
| **Model risk** | IV, vol-of-vol, smile, jumps — all introduce error | No model; just two market prices |
| **Anchor quality** | Deribit professionals price options, not PM-style binaries | Kalshi is a regulated prediction market with sophisticated participants |
| **Signal type** | Relative sentiment proxy | Closer to true mispricing between two liquid venues |

### Matching logic

For each PM market the scanner looks for a Kalshi counterpart with:
- Same underlying (BTC/ETH/etc.)
- Strike within `KALSHI_MATCH_MAX_STRIKE_DIFF` (2% by default) of the PM strike
- Expiry within `KALSHI_MATCH_MAX_EXPIRY_DAYS` (±2 days by default)

If no match is found the market is skipped (no N(d₂) fallback when Kalshi is enabled).

### Signal resolution

| Kalshi found | Spread ≥ min | N(d₂) agrees | `REQUIRE_ND2_CONFIRMATION` | Action | `signal_source` |
|---|---|---|---|---|---|
| ✓ | ✓ | ✓ | either | Trade | `kalshi_confirmed` |
| ✓ | ✓ | ✗ | `False` | Trade | `kalshi_only` |
| ✓ | ✓ | ✗ | `True` | Skip | — |
| ✓ | ✗ | — | — | Skip | — |
| ✗ | — | — | — | Skip | — |
| — | — | — | `KALSHI_ENABLED=False` | N(d₂) path | `nd2_only` |

All three `signal_source` values are stored in `trades.csv` for retrospective analysis.

---

## Signal Generation

### 0. Kalshi Match (Layer 1)

For each PM milestone market the `KalshiClient` fetches the best-matching Kalshi market
by scoring `strike_diff + expiry_diff`. If `KALSHI_ENABLED=True` and no match is found,
the market is skipped. If found:

```
kalshi_deviation = |pm_price - kalshi_yes_mid|
kalshi_direction = "BUY_YES" if pm_price < kalshi_price else "BUY_NO"
```

Kalshi markets are cached for 5 minutes (`CACHE_TTL = 300 s`).

### 1. Market Universe

Each scan cycle (`MISPRICING_SCAN_INTERVAL = 60 s`) the scanner:

- Loads all PM markets with `market_type = "milestone"` (i.e. non-bucketed, named
  strike/price markets)
- Filters to markets resolving at least `MILESTONE_MIN_DAYS = 1` day in the future
- Skips markets with no live order-book mid price or a PM price outside `(0.01, 0.99)` —
  extremely one-sided markets are illiquid

### 2. Strike Extraction

The strike price is parsed from the market title using a regex that handles common
formats: `$120k`, `$120,000`, `$1.2M`.  
If no parseable strike is found the market is skipped.

### 3. Deribit IV Lookup

For each viable market the bot queries Deribit for the **nearest call option** to the
target (strike, expiry) pair by minimising a composite score:

$$
\text{score} = \frac{|\text{expiry} - \text{target\_date}|}{30 \text{ days}} + \frac{|\text{strike}_\text{Deribit} - \text{strike}_\text{PM}|}{\text{strike}_\text{PM}}
$$

The `mark_iv` (in %) of the winning instrument is fetched from the Deribit order book.

### 4. Black-Scholes Implied Probability

The implied probability of the event is **N(d₂)** from Black-Scholes:

$$
d_1 = \frac{\ln(S / K) + (r + \tfrac{1}{2}\sigma^2) \cdot T}{\sigma \sqrt{T}}
$$

$$
d_2 = d_1 - \sigma \sqrt{T}
$$

$$
P_\text{implied} = N(d_2)
$$

Where:
| Symbol | Meaning |
|--------|---------|
| $S$ | Current spot price (from Hyperliquid mid) |
| $K$ | Strike parsed from PM market title |
| $r$ | Risk-free rate (5%) |
| $\sigma$ | Deribit mark IV (annualised) |
| $T$ | Time to PM resolution in years |

### 5. Deviation & Fee Hurdle

```
deviation = |pm_price - implied_prob|
fee_hurdle = min_edge_after_fees(pm_price) + 0.03
```

`min_edge_after_fees(p)` accounts for:
- **PM taker fee**: `p × PM_FEE_COEFF × p(1−p)` (where `PM_FEE_COEFF = 0.0175`)
- **HL taker fee**: fixed tier-0 rate
- **Edge buffer**: slippage / basis-risk allowance

An additional **3% basis buffer** is added on top to avoid trading marginal signals.

A `MispricingSignal` is emitted only when `deviation > fee_hurdle`.

### 6. Direction

| Condition | Trade |
|-----------|-------|
| `pm_price < implied_prob` | **BUY_YES** — PM underprices the event |
| `pm_price > implied_prob` | **BUY_NO** — PM overprices the event |

---

## Position Sizing

```python
suggested_size_usd = min(
    MAX_PM_EXPOSURE_PER_MARKET × 0.5,   # hard cap = $250
    deviation × 1000,                    # rough Kelly fraction
)
```

`MAX_PM_EXPOSURE_PER_MARKET = $500`, so the cap is **$250 per position**.  
The Kelly-inspired term scales size linearly with edge: a 10% deviation → $100, a 25%
deviation → $250 (capped).

---

## Exit Conditions

Once a position is open, `monitor.py` checks every 30 seconds (with a minimum hold of
`MIN_HOLD_SECONDS = 60 s`):

| Exit | Condition | Rationale |
|------|-----------|-----------|
| **Profit target** | `unrealised_pnl ≥ entry_deviation × PROFIT_TARGET_PCT × size` | Capture 60% of the initial edge; avoid mean-reversion risk |
| **Stop-loss** | `unrealised_pnl ≤ −STOP_LOSS_USD` | Hard stop at **−$25** regardless of size |
| **Time stop** | `days_to_expiry ≤ EXIT_DAYS_BEFORE_RESOLUTION = 3` | Avoid binary resolution risk and low-liquidity close to expiry |
| **Resolved stop** | `now ≥ market_end_date` | Market has already resolved; exit immediately |

In the current implementation, mispricing unrealised P&L is evaluated by:

```
unrealised_pnl = (current_reference_price - entry_price) * size
```

`current_reference_price` comes from the monitor's price feed for that position.

---

## Data Flow

```
KalshiClient            DeribitFetcher          PMClient                 HLClient
    │ YES mid price         │ mark_iv               │ YES mid price          │ spot price
    │                       └──────────┬────────────┘                        │
    │                                  ▼                                     │
    │                      options_implied_probability(S,K,T,σ) ◄───────────┘
    │                                  │
    │           ┌──────────────────────┘
    ▼           ▼
 Layer 1: |PM − Kalshi| ≥ KALSHI_MIN_DEVIATION?
    │ No → skip (no Kalshi match or spread too small)
    │ Yes
    ▼
 Layer 2 (if REQUIRE_ND2_CONFIRMATION):
    Kalshi direction == N(d₂) direction?
    │ No → skip (conflicting signals)
    │ Yes
    ▼
 MispricingSignal {signal_source: "kalshi_confirmed"}
    │
    ▼
 AgentDecisionLayer / AUTO_APPROVE
    │
    ▼
 risk.open_position()
    │
    ▼
 PositionMonitor (every 30 s)
    │
  exit condition met?
    │
    ▼
 risk.close_position() → trades.csv
```

*When `KALSHI_ENABLED=False` the Kalshi steps are bypassed and the flow goes directly
from `options_implied_probability` → `deviation > fee_hurdle` → signal.*

---

## Key Configuration

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MISPRICING_SCAN_INTERVAL` | 60 s | Time between full scan passes |
| `MILESTONE_MIN_DAYS` | 1 | Skip markets resolving within 1 day |
| `MAX_PM_EXPOSURE_PER_MARKET` | $500 | Hard cap on position notional |
| `PM_FEE_COEFF` | 0.0175 | PM fee coefficient for hurdle calc |
| `PROFIT_TARGET_PCT` | 0.60 | Capture 60% of initial deviation |
| `STOP_LOSS_USD` | $25 | Maximum loss per trade |
| `EXIT_DAYS_BEFORE_RESOLUTION` | 3 | Exit this many days before expiry |
| `MIN_HOLD_SECONDS` | 60 | Minimum position hold time |
| `KALSHI_ENABLED` | True | Use Kalshi as primary signal layer |
| `KALSHI_REQUIRE_ND2_CONFIRMATION` | True | N(d₂) must agree with Kalshi direction |
| `KALSHI_MIN_DEVIATION` | 0.03 | Min \|PM − Kalshi\| to fire signal |
| `KALSHI_MATCH_MAX_STRIKE_DIFF` | 0.02 | Max 2% fractional strike mismatch |
| `KALSHI_MATCH_MAX_EXPIRY_DAYS` | 2.0 | Max ±2 day expiry tolerance |

---

## Known Flaws & Critical Limitations

### 1. Fatal Conceptual Mismatch: Barrier Probability vs. Terminal Probability

**Severity reduced from Fatal → High when `KALSHI_ENABLED=True`.**

The strategy's N(d₂) signal — that a PM "Will BTC reach $X?" binary equals a Deribit
digital call with fair price N(d₂) — is **fundamentally incorrect** in isolation.

- **PM milestone markets** resolve YES if the high of *any* Binance 1-minute candle
  during the period ever touches or exceeds the strike. This is a **one-touch /
  barrier-hit** probability: $P(\max_{0 \le t \le T} S_t \ge K)$.
- **Deribit N(d₂)** is the risk-neutral **terminal** probability: $P(S_T \ge K)$.

For a volatile asset like BTC (50–80% IV), the probability of *ever* touching a level
before $T$ is dramatically higher than the probability of being *above it at expiry* —
often 20–40% higher for OTM upside strikes. As a result:

> **PM YES prices will systematically trade above Deribit N(d₂).** The scanner will
> repeatedly flag "PM overpriced → BUY_NO" signals that are not mispricings — they are
> correct prices for two structurally different random variables.

The strategy is fighting a permanent structural bias, not noise. This is the primary
reason the edge is illusory.

---

### 2. Inconsistent Volatility Horizon

Even setting aside the barrier/terminal mismatch:

- Deribit expiries are fixed (daily/weekly/monthly/quarterly); PM resolution dates rarely
  align exactly.
- The code fetches `mark_iv` from the *nearest* Deribit option and plugs it into
  Black-Scholes with `T = PM resolution time` — a different horizon.
- Using a volatility number calibrated to one maturity inside a formula for a different
  maturity is mathematically inconsistent. Correct practice requires interpolating the
  vol term structure and forward volatility, which this strategy does not do.

Basis risk from this mismatch is large and persistent.

---

### 3. Not Arbitrage — Pure Directional Bet

This is a single-leg trade with no hedge:

- There is no offsetting Deribit position to lock in a spread.
- If the two markets never converge (the likely outcome given flaws 1 & 2), the position
  sits until the 3-day time stop and takes a partial or full loss.
- P&L is 100% exposed to the actual BTC path and PM liquidity.

The "mispricing" signal is therefore a **view on relative sentiment** between retail
prediction-market participants and professional options traders — not a riskless spread.

---

### 4. Fee Hurdle Miscalibrated

As of early 2026, all crypto markets on Polymarket have taker fees enabled, following a
curve that peaks around **1.56% at 50% probability**. The current formula:

```python
pm_taker_fee = p * PM_FEE_COEFF * (p * (1 - p))   # PM_FEE_COEFF = 0.0175
```

uses a coefficient and functional form from an older/sports regime. The actual crypto
taker cost is materially higher near mid-book. The `fee_hurdle` is therefore too low,
meaning the bot will trade marginal or negative-EV signals. The `+3%` basis buffer is an
arbitrary patch that does not fix the root miscalibration.

---

### 5. Model Risk (More Severe Than Stated)

- Crypto returns exhibit **jumps, vol-of-vol, and strong skew/smile**. A single IV point
  from the nearest Deribit call badly misprices tail probabilities.
- For barrier-hit events the error is compounded — closed-form barrier formulas already
  diverge from European formulas under Black-Scholes; real-world jumps make the
  divergence larger still.
- The constant `r = 5%` risk-free rate and spot price sourced from a perp exchange
  (Hyperliquid) add small but unnecessary additional noise.

---

### 6. Execution & Operational Risks

- **Strike parsing** via regex on market titles is fragile. Formats like "BTC to 120k",
  abbreviations, and typos can silently produce a wrong strike and a garbage signal.
- **Thin books** — milestone markets are often illiquid. The hurdle formula assumes taker
  fills at mid; real slippage on a $100–$250 position can consume the entire theoretical
  edge.
- **Correlated positions** — the $250-per-market cap is weakly protective when multiple
  open positions are all long/short BTC milestone markets. The portfolio has concentrated
  directional BTC exposure regardless of position count.
- No handling of limit vs. market order choice, queue position, or API latency across
  PM, Deribit, and Hyperliquid.

---

### 7. Risk-Management Gaps

- **Sizing** — `deviation × 1000` is a linear heuristic, not a proper Kelly fraction for
  a binary outcome with path-dependent exits.
- **Stop-loss** — the hard $25 stop is good discipline but will be triggered frequently
  if the bias in flaw #1 generates systematically "wrong" directional trades.
- **Profit target at 60% of initial edge** means the strategy requires multiple wins to
  offset each stop-loss; the asymmetry worsens when initial signals are systematically
  biased.
- **3-day time stop** forces closure near expiry when liquidity is worst, often locking
  in a loss if convergence has not occurred.

---

### Minor but Cumulative Issues

- Fixed 5% risk-free rate regardless of prevailing rates or term structure.
- `mark_iv` is a mark price, not always a tradable bid/ask level.
- No handling of early resolution, oracle disputes, or Polymarket resolution delays.
- No back-test accounting for survivorship bias, delisted markets, or resolution disputes.

---

### Summary

| Category | Severity | Description |
|----------|----------|-------------|
| Barrier vs. terminal probability | **Fatal** | PM resolves on path max, not terminal price; N(d₂) is the wrong formula |
| Inconsistent vol horizon | **Severe** | Deribit IV applied at wrong maturity |
| Single-leg, no hedge | **Severe** | No spread locked; pure directional exposure |
| Fee hurdle miscalibration | **High** | Underestimates actual PM taker cost |
| Heavy tails / smile | **High** | Single-point IV badly misprices tail events |
| Strike parsing fragility | **Medium** | Regex errors → wrong strike → garbage signal |
| Thin-book slippage | **Medium** | Model assumes mid fills; reality differs |
| Correlated positions | **Medium** | Multi-position sizing ignores portfolio BTC delta |
| Sizing / Kelly approximation | **Low** | Linear heuristic, not optimal Kelly |
