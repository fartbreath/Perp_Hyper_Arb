# Market Making Strategy (Strategy 1)

## What This Strategy Does

The market making strategy acts like a currency exchange booth at an airport. It
simultaneously offers to **buy** and **sell** the same contract, pocketing the small
gap between the buy price and the sell price (the "spread") when both sides trade.

We post these two-sided offers on **Polymarket**, a prediction market where people
trade on Yes/No questions like "Will Bitcoin be above $80,000 by Friday?". Our orders
sit passively in the book waiting for someone to trade against them.

When both sides fill, we earn the spread plus a small rebate that Polymarket pays
back to liquidity providers. We are not taking a view on the outcome  we are being
paid to provide liquidity.

---

## How Money Is Made

**Best case  both sides fill:**
We buy YES at 18 and sell YES at 26 on the same market. Once both trades happen
we have no net position and have collected 8 on a $50 stake  approximately $4 gross.

**Acceptable case  spread capture over many trades:**
Even if only one side fills at a time, across hundreds of short-dated markets the law
of large numbers means fills should average out to near-equal on both sides. Rebates
accumulate on every fill regardless of direction.

**Risk  adverse selection:**
Faster or better-informed traders pick off our stale quotes just before the market
moves. We get filled on the wrong side right as the price moves against us. This is the
primary cost this strategy must manage.

---

## Which Markets We Quote

We only post quotes in markets where we have a reasonable chance of earning more than
we risk:

- **Markets that are neither near-certain nor near-impossible.** If a market is
  trading at 2 or 98, the book is thin and informed traders dominate. We skip them.

- **Markets where the fee-adjusted profit is positive.** Polymarket charges taker fees
  that eat into the spread. We only quote when we expect the spread we'll capture,
  plus the rebate Polymarket pays us, to exceed that cost.
- **Markets with sufficient volume run-rate.** Illiquid markets tie up capital and
  revolve slowly. The threshold scales with how much of the market’s lifetime has
  elapsed — a brand-new daily bucket starts at $0 required and ramps up to the full
  `MAKER_MIN_VOLUME_24HR` threshold as it approaches expiry. This avoids
  accidentally filtering out active new markets solely because they are young.

- **Markets with a score above `MIN_SIGNAL_SCORE_MAKER`.** Every qualifying signal is
  scored using `score_maker` which combines edge, volume run-rate, and TTE
  attractiveness. When capital is constrained, the highest-scoring markets receive
  quotes first.
- **Newly-discovered markets.** When a fresh market appears we post a wider-than-normal
  spread briefly to capture first-mover advantage before competitors arrive.

As of March 2026, all crypto bucket markets carry taker fees, so the fee calculation
matters on every trade.

---

## Keeping Quotes Fresh

Stale quotes get picked off. Four mechanisms reprice continuously:

1. **Polymarket price move.** The moment the market mid drifts far enough from our
   posted quotes, we cancel and repost at the new price. This is nearly instant.

2. **Bitcoin or Ethereum price move.** If the underlying crypto price on the Pyth oracle moves
   by more than 0.2%, we reprice all related Polymarket quotes and re-check the hedge.

3. **30-second backstop.** Every 30 seconds, any quote older than 30 seconds is
   force-repriced regardless of what else is happening. This also picks up newly-liquid
   markets discovered since startup.

4. **Adverse drift guard on partial fills.** When a quote has been partially filled
   (one leg partially traded), we would normally leave the remainder resting. But if
   the market mid moves more than 1.5% away from our quoted price, the remainder is
   immediately cancelled and reposted at the new price — even if the quote is still
   within its 30-second lifetime. This prevents the remaining contracts sitting at an
   adversely-selected price after a fast HL move.

---

## Managing Inventory

Every time one side fills, we accumulate a directional position (inventory). The
strategy tracks this coin by coin:

- If we've sold more YES than we've bought, we're short YES  we profit if the outcome
  is No. We shift our quotes slightly to make buying YES from us cheaper, nudging the
  inventory back toward neutral.

- If we've bought more YES than we've sold, we're long YES  we profit if the outcome
  is Yes. We shift our quotes the other way.

This automatic price nudging (inventory skew) is the first line of defence. It costs
nothing because it's just adjusting within the same spread window.

## Units & Invariants
- **Canonical unit for inventory and hedge sizing:** USD of capital deployed (`entry_cost_usd`), not contract count or face value.
- **YES/NO price integrity:** YES and NO are independent CLOB books. Maker logic reads prices from the held token's own book; it does not derive NO as `1 - YES`.
- **Inventory accounting:** Inventory updates are recorded in USD (see Appendix D "Inventory accounting").
- **Hedging and thresholds:** All hedging and risk thresholds operate on USD capital units (e.g. `HEDGE_THRESHOLD_USD`, `HEDGE_REBALANCE_USD`).

We also cap how many open positions we'll hold per coin at any one time (3 maximum).
Once that cap is hit, no new fills are opened for that coin until an existing one closes.

---

## The Hedge

Once our net position on a coin exceeds the configured threshold (`HEDGE_THRESHOLD_USD`,
default **$100**, often overridden to **$200** in `config_overrides.json`), we place a live perp trade on
**Hyperliquid** in the opposite direction:

- We’re net long YES on BTC markets → we short BTC perp on Hyperliquid
- We’re net short YES on BTC markets → we long BTC perp on Hyperliquid

This means if Bitcoin suddenly drops 10%, our Hyperliquid short gains roughly the same
as our Polymarket YES positions lose. The strategy becomes close to market-neutral.

The hedge size is calculated using a standard options pricing formula (Black-Scholes
digital delta) tuned to the strike price and time to expiry of each contract. If the
formula can’t run (e.g. no strike parseable from the market title), it falls back to
a simpler estimate based purely on the probability price.

**Debounce and cooldown.** Multiple fills arriving within seconds would otherwise each
trigger an HL order. Instead, every hedge event restarts a short debounce timer;
only one HL order fires after the burst settles. A minimum interval between executions
(`HEDGE_MIN_INTERVAL`) prevents HL order spam on rapid oscillating fills.

**Slippage alerting.** Every hedge execution is recorded in a quality ring-buffer.
If the rolling 20-trade average slippage exceeds 0.30%, a warning is logged suggesting
spread widening or reduced hedge frequency.

The hedge is re-evaluated on every Pyth oracle price tick and every new fill. If inventory
drops back below the configured threshold, the hedge is removed.

---

## When Positions Close

Every 30 seconds the monitor checks all open positions. For maker positions, exits are:

| Reason | Detail |
|--------|--------|
| **Near expiry (non-bucket only)** | `days_to_expiry <= MAKER_EXIT_HOURS/24` when `MAKER_EXIT_HOURS > 0` |
| **Market resolved** | The contract has settled |
| **Coin-level loss** | All positions on one coin are collectively down more than $75 |
| **Per-position stop/profit** | Not used for maker positions |

By default (`MAKER_EXIT_HOURS = 0.0`) the non-bucket near-expiry gate is disabled.
Bucket maker positions hold to resolution unless closed by coin-level risk limits.

---

## Paper Trading Mode

In paper trading mode, no real money moves. Orders are recorded internally and a
simulator checks every 5 seconds whether a real trader would have hit our price. When
a simulated fill is triggered, a realistic probability gate filters it further  not
every competitive quote fills, just as in the real world where we're at the back of
the queue.

Paper mode uses the live Polymarket order book data for fill detection **and for
exit pricing**. The design goal is that switching `PAPER_TRADING` from `True` to
`False` should produce near-identical P&L once fill frequency is accounted for.

CLOB-faithful elements:
- **Fill price** — always the maker's quoted price (not the mid). This is what the
  live CLOB guarantees: a maker order fills at its limit price, not at a worse price.
- **Exit price** — when the position monitor closes a position it uses the live book
  `best_bid` on the held token's own book for taker exits (YES sell at YES bid,
  NO sell at NO bid). Using mid would incorrectly award the full
  half-spread to every close, inflating P&L relative to live trading.
- **Inventory units** — tracked in USD (`entry_cost_usd`), matching the canonical unit
  for hedge sizing and inventory-skew. Tracking contracts would overshoot the skew
  coefficient by a factor of 1/fill_price.
- **Partial-fill remainders** — stay in the order book (as a real CLOB resting order
  would). Sub-minimum remainders are **not** auto-consumed; the reprice cycle cancels
  them on the next quote sweep.
- **Maker rebates** — credited at fill time per leg, then exit-leg rebate added on
  close. This mirrors the timing of PM's daily fee-rebate accounting.
- **HL hedge** — uses live HL mid prices with taker fees applied on both open and
  close legs. No manufactured hedge prices.

## Key Risks

1. **Adverse selection**  faster bots take our quotes at exactly the wrong moment.
   Managed by tight reprice triggers and a fill probability penalty during volatile
   periods.

2. **Gamma blowup near expiry**  short-dated binary markets can swing from 30 to
  90 in minutes as the candle close approaches. Managed primarily by coin-level
  loss caps and inventory/hedge controls; optional non-bucket time-stop is
  controlled by `MAKER_EXIT_HOURS`.

3. **Correlated one-sided fills**  during a BTC flash crash, we might get filled on
   Long YES across 5 BTC markets simultaneously before any reprice fires.
   Managed by the $75 coin-level aggregate loss limit and the 3-position per-coin cap.

4. **Fee drag**  at mid-range probabilities (around 50) the taker fee on filled
   quotes is at its highest. The edge filter ensures we only quote where the spread
   after fees is positive, but it is tight.

---

## Current Status (March 2026)

- All strategies are off by default. Enable what you need from the Settings page.
- Paper trading mode active — no real funds at risk.
- All critical bugs fixed (see Appendix F).
- Capital velocity scoring active — markets ranked by edge × volume run-rate × TTE attractiveness.
- Orphaned one-sided positions fixed — the second leg is now always re-posted after a fill.
- One-sided order-book fill handling fixed — crossed quotes now still fill when one side of the book is temporarily empty.
- Hedge logging active — check the Logs tab for `"Hedge placed"` entries. Slippage alert at 0.30% rolling average.
- `MAKER_MAX_BOOK_AGE_SECS` set to 30 — stale order book data triggers a skip rather than quoting on bad data.
- Adversity thresholds now per-bucket type — `bucket_5m` uses tighter thresholds than `bucket_15m` and `bucket_1h`.
- WS reconnect reconciliation active — positions are re-synced with the PM wallet on every Polymarket WS reconnect.
- Ghost positions are auto-dismissed during reconciliation when PM wallet state proves the leg is gone.
- `GET /config/effective` endpoint available for inspection of the live merged config.

---
---

# Appendix A  Configuration Reference

All parameters are runtime-adjustable via `PATCH /config` on the API and survive
restarts via `config_overrides.json`.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STRATEGY_MAKER_ENABLED` | `False` | Enable/disable the strategy |
| `MAKER_MIN_QUOTE_PRICE` | `0.05` | Skip markets where mid < 5c or > 95c |
| `MAKER_MIN_EDGE_PCT` | `0.001` | Minimum fee-adjusted edge to post (0.1%); overridden to 0.005 |
| `MAKER_MIN_SPREAD_PROFIT_MARGIN` | `0.010` | Minimum locked-in spread edge for one-sided close logic |
| `PM_FEE_COEFF` | `0.0175` | Polymarket taker fee coefficient (~1.75% peak) |
| `REPRICE_TRIGGER_PCT` | `0.002` | Pyth oracle move that triggers full reprice (0.2%) |
| `MAX_QUOTE_AGE_SECONDS` | `30` | Backstop reprice interval (seconds) |
| `MAKER_ADVERSE_DRIFT_REPRICE` | `0.015` | Force-reprice partial fill if mid drifts > 1.5% from quote price |
| `MAKER_VOL_FILTER_PCT` | `0.015` | Pause quoting when 5-min HL move exceeds 1.5% |
| `MAKER_MIN_VOLUME_24HR` | `5000` | Min lifetime-scaled 24h volume to quote (USD); 0 to disable |
| `MAKER_MAX_TTE_DAYS` | `14` | Skip markets resolving more than 14 days out |
| `MAKER_EXIT_HOURS` | `6.0` | Hours before expiry to close all positions |
| `MAKER_MAX_BOOK_AGE_SECS` | `30` | Skip quoting if PM order book is older than this (seconds) |
| `HEDGE_THRESHOLD_USD` | `200` | Minimum net inventory before hedging (USD) |
| `HEDGE_REBALANCE_PCT` | `0.10` | Min fractional change to adjust existing hedge (10%) |
| `HEDGE_DEBOUNCE_SECS` | — | Wait this many seconds for fill burst to settle before hedging |
| `HEDGE_MIN_INTERVAL` | — | Cooldown between successive hedge executions (seconds) |
| `HEDGE_SLIPPAGE_ALERT_PCT` | `0.30` | Warn when rolling 20-hedge avg slippage exceeds this (%) |
| `INVENTORY_SKEW_COEFF` | `0.0001` | Skew per unit of inventory (1c per $100) |
| `INVENTORY_SKEW_MAX` | `0.03` | Hard cap on inventory skew (±3c) |
| `MAKER_MAX_IMBALANCE_CONTRACTS` | `50` | Skip reposting a leg when open contracts on that side exceed this |
| `MAX_MAKER_POSITIONS_PER_UNDERLYING` | `3` | Per-coin open position cap (overridden to 16) |
| `MAX_CONCURRENT_MAKER_POSITIONS` | `8` | Total maker position cap (overridden to 20) |
| `MAX_PM_EXPOSURE_PER_MARKET` | `500` | Max USD deployed per market (overridden to 600) |
| `MAKER_COIN_MAX_LOSS_USD` | `75` | Coin-level aggregate loss limit (USD) |
| `MIN_SIGNAL_SCORE_MAKER` | `0.0` | Capital velocity score gate (0 = disabled) |
| `NEW_MARKET_AGE_LIMIT` | `3600` | Wide-spread window for newly discovered markets (seconds) |
| `NEW_MARKET_WIDE_SPREAD` | `0.08` | Initial wide spread for new markets (8c total) |
| `NEW_MARKET_PULL_SPREAD` | `0.02` | Pull back to normal if competition inside 2c |
| `FILL_CHECK_INTERVAL` | `5` | Fill simulator sweep frequency (seconds) |
| `PAPER_FILL_PROB_BASE` | `0.04` | Baseline paper fill probability |
| `PAPER_FILL_PROB_NEW_MARKET` | `0.12` | New-market paper fill probability |
| `PAPER_ADVERSE_SELECTION_PCT` | `0.003` | HL move fraction that triggers adverse-selection boost |
| `PAPER_ADVERSE_FILL_MULTIPLIER` | `0.15` | Fill probability multiplier during adverse HL move (fills ×6.7×) |
| `PAPER_REBATE_CAPTURE_RATE` | `0.25` | Fraction of theoretical per-fill rebate credited (realistic share) |

---

# Appendix B  Quoting and Edge Formulas

### Signal evaluation pipeline

For every market, `_evaluate_signal` runs the following checks in order. The first
failing check returns `None` (no quote posted):

1. Market has a valid `end_date`
2. TTE within `[MAKER_EXIT_HOURS, MAKER_MAX_TTE_DAYS × 24h]`
3. Fee-adjusted edge passes `MAKER_MIN_EDGE_PCT` (see formula below)
4. Underlying Pyth oracle move over the **adaptive vol window** ≤ `MAKER_VOL_FILTER_PCT` (1.5%)
5. Risk capacity check — `can_open()` (skipped for markets with an already-open one-sided position to avoid false-blocking the missing leg)
6. Mid within `[MAKER_MIN_QUOTE_PRICE, 1 − MAKER_MIN_QUOTE_PRICE]`
7. Lifetime-scaled volume gate (see below)
8. Capital velocity score ≥ `MIN_SIGNAL_SCORE_MAKER`

### Edge calculation

```python
taker_fee_at_mid = PM_FEE_COEFF * mid * (1 - mid)   # peaks at ~0.44% when p=0.5
effective_edge   = half_spread + market.rebate_pct * taker_fee_at_mid
# Rebate is earned, not paid — it is additive to the spread
if effective_edge < MAKER_MIN_EDGE_PCT:
    skip market
```

`rebate_pct` varies by market type (e.g. `bucket_daily` ≈ 20%); zero for fee-free markets.

### Lifetime-scaled volume gate

For `bucket_*` markets with a known fixed duration the required volume scales with
how much of the market's life has actually elapsed, so brand-new markets are not
falsy rejected:

```python
# duration = total market lifetime in seconds (e.g. 86400 for bucket_daily)
fraction_elapsed = max(0.0, min(1.0, 1.0 - tte_secs / duration))
required_volume  = MAKER_MIN_VOLUME_24HR * fraction_elapsed
# bucket_daily, 8 h elapsed (16 h remaining) → required = $1,667 (not $5,000)
# bucket_daily, just launched               → required = $0     (always passes)
# milestone / unknown duration              → required = $5,000 (full threshold)
```

### Adaptive volatility filter window

Short-duration buckets get a proportionally tighter look-back to remain responsive
to fast moves within their lifetime:

```python
_market_duration = _MARKET_TYPE_DURATION_SECS.get(market.market_type, 86400)
_vol_window = min(300.0, _market_duration / 4)
# bucket_5m  → 75 s   bucket_15m → 225 s   anything ≥ 20 min → 300 s
```

### Inventory-skewed quote placement

```python
half_spread = market.max_incentive_spread / 2

net_inv    = self._inventory.get(market.underlying, 0.0)
raw_skew   = net_inv * INVENTORY_SKEW_COEFF
skew       = max(-INVENTORY_SKEW_MAX, min(INVENTORY_SKEW_MAX, raw_skew))
skewed_mid = mid - skew   # positive inventory skews mid down (encourages SELL to reduce)

bid_price  = max(MAKER_MIN_QUOTE_PRICE,     skewed_mid - half_spread)
ask_price  = min(1 - MAKER_MIN_QUOTE_PRICE, skewed_mid + half_spread)
# Both prices are then rounded to market.tick_size
```

### Imbalance guard (deploy time)

When open positions already tilt one side heavily, re-posting the overweight leg is
skipped to avoid compounding a directional exposure:

```python
imbalance  = yes_contracts - no_contracts
post_bid   = imbalance < MAKER_MAX_IMBALANCE_CONTRACTS   # skip BUY YES if too YES-heavy
post_ask   = imbalance > -MAKER_MAX_IMBALANCE_CONTRACTS  # skip SELL YES if too NO-heavy
```

### Reprice triggers

```python
# PM price callback
if abs(new_mid - quote.price) > market.max_incentive_spread / 2:
    await self._reprice_market(market)

# Pyth oracle callback
move_pct = abs(pyth_price.mid - last_mid) / last_mid
if move_pct >= REPRICE_TRIGGER_PCT:
    await _reprice_underlying(coin)
    _schedule_hedge_rebalance(coin)   # debounced; see hedge section
```

### Partial-fill reprice guard

A partially-filled quote is not cancelled immediately (that would waste the partial
fill progress), BUT it is force-repriced if either:

- Age exceeds `MAX_QUOTE_AGE_SECONDS` (stale), **or**
- `|mid − quote.price| > MAKER_ADVERSE_DRIFT_REPRICE` (1.5%) — market moved away
  fast enough that the remaining resting size is adversely selected regardless of age

```python
still_fresh = age < MAX_QUOTE_AGE_SECONDS
low_drift   = abs(mid - existing.price) <= MAKER_ADVERSE_DRIFT_REPRICE
if still_fresh and low_drift:
    return   # safe to leave resting
# otherwise: cancel + re-post at current market price
```

### Capital velocity scoring (`score_maker`)

Signals are ranked by a composite score before capital is allocated in
`_ensure_quoted_all`. Markets with identical edge are differentiated by volume
run-rate and time-to-expiry attractiveness. The score gate `MIN_SIGNAL_SCORE_MAKER`
filters out low-quality markets before any slot is reserved.

---

# Appendix C  Hedge Math

### Net inventory per coin

```python
# Sum over all open positions on that coin:
net_inventory_usd = sum(
    (+1 if pos.side == "YES" else -1) * pos.entry_cost_usd
    for pos in open_positions if pos.underlying == coin
)
```

### Black-Scholes digital delta (primary path)

When the market title contains a parseable strike price and expiry is in the future,
the hedge size uses the proper digital-call delta:

```
sigma          = implied_sigma(p_PM, S, K, T)   # closed-form, solved from p = N(d2)
d2             = N_inv(p)
u              = -d2 + sqrt(d2^2 + 2*ln(S/K))
sigma          = u / sqrt(T)

delta          = n(d2) / (S * sigma * sqrt(T))   # n = standard normal PDF
coins_to_hedge = |entry_cost_usd| * delta

where S = Pyth oracle spot, K = strike, T = TTE in years
```

If `sigma` is invalid (negative, zero, non-finite — common near expiry or deep
ITM/OTM), the result is discarded and the fallback is used.

### Fallback (naive linear delta)

```python
coins_to_hedge = abs(net_inventory_usd) / pyth_mid
```

### Hedge direction and sizing

```python
direction       = "SHORT" if net_inventory_usd > 0 else "LONG"  # short perp when long PM
coins_to_hedge  = abs(_position_delta_coins(coin, pyth_mid))
target_notional = coins_to_hedge * pyth_mid

# Hard cap: never hedge more than MAX_HL_NOTIONAL USD notional
coins_to_hedge  = min(coins_to_hedge, MAX_HL_NOTIONAL / pyth_mid)
```

### Hedge fire/adjust conditions

| Condition | Action |
|-----------|--------|
| `abs(net_inventory_usd) < HEDGE_THRESHOLD_USD` | Close existing hedge if open |
| Same direction, notional change < `HEDGE_REBALANCE_PCT` of current | Skip rebalance |
| Direction flipped or change ≥ threshold | Close old hedge, open new one |

### Hedge debounce and cooldown

Fill bursts (multiple PM fills within seconds) would trigger multiple HL orders
without throttling. The strategy solves this with two layers:

1. **Debounce** (`HEDGE_DEBOUNCE_SECS`): each new fill/BBO event cancels and restarts
   a debounce timer. Only one HL order fires after the burst settles.
2. **Cooldown** (`HEDGE_MIN_INTERVAL`): even after the burst, a minimum interval
   before the next hedge execution prevents HL order spam on rapid oscillating fills.

### Slippage alerting

Every hedge execution appends an entry to a ring buffer (last 200). When the rolling
20-hedge average `slippage_pct` exceeds `HEDGE_SLIPPAGE_ALERT_PCT` (0.30%), a
`log.warning` fires suggesting spread widening or reduced hedge frequency.

### Hedge debug logging

```powershell
Invoke-RestMethod "http://localhost:8080/logs?limit=100&search=Hedge+placed" |
  ConvertTo-Json -Depth 4
```

Key fields: `coin`, `direction`, `size_coins`, `decision_mid`, `exec_price_est`,
`slippage_pct`, `bbo_spread`, `notional_usd`.

### Trades CSV schema

`trades.csv` includes an `underlying` column immediately after `market_type`. Use it
to correlate PM fills with HL hedge rows.

---

# Appendix D  System Architecture & Data Flow

### Component map

```
PM WebSocket              HL WebSocket
    | price_change             | bbo_update
    v                          v
_on_pm_price_change()     _on_hl_bbo_update()
    |  [try/except]            |  [try/except]
    v                          v
_reprice_market()         _reprice_underlying(coin)
    |                     _rebalance_hedge(coin)
    v
pm_client.place_limit()  [post-only; paper returns "paper-{ts}"]
    |
    +-- [live]  PM CLOB fill --> record_fill() --> _rebalance_hedge()
    |
    +-- [paper] FillSimulator._sweep() every FILL_CHECK_INTERVAL seconds
                    |
                    |  competitive-at-touch check:
                    |    BUY:  quote.price >= book.best_bid
                    |    SELL: quote.price <= book.best_ask
                    |
                    |  three-tier probability gate
                    v
              _on_fill() --> risk.open_position()
                                 |
                           PositionMonitor (every 30s)
                                 |
                     per-position & coin-aggregate exit checks
                                 |
                        risk.close_position()
                                 |
                           trades.csv row written
```

### Paper fill probability tiers

| Tier | Condition | Probability |
|------|-----------|-------------|
| New-market | Market age < `NEW_MARKET_AGE_LIMIT` | `PAPER_FILL_PROB_NEW_MARKET = 0.12` |
| Base | All other markets | `PAPER_FILL_PROB_BASE = 0.04` |
| Adverse-selection boost | HL moved >= `PAPER_ADVERSE_SELECTION_PCT` since last sweep | ÷ `PAPER_ADVERSE_FILL_MULTIPLIER (0.15)` ≈ ×6.7× |

Note: when the adverse-selection flag fires, fill arrival probability is **boosted**
(not penalised) because informed takers aggressively pick off stale quotes. The taker
size is also inflated. This correctly models being picked off during HL vol events.

### Inventory accounting (USD)

Inventory is stored in USD (`entry_cost_usd`) for all inventory deltas.

| Fill event | Inventory delta for underlying coin |
|------------|-------------------------------------|
| BUY YES filled | `+fill_cost_usd` |
| SELL YES filled | `-fill_cost_usd` |
| BUY NO filled | `-fill_cost_usd` |
| SELL NO filled | `+fill_cost_usd` |

Where `fill_cost_usd = fill_price × contracts` (BUY YES) or `(1 − fill_price) × contracts` (SELL YES).

---

# Appendix E  Known Limitations & Open Issues

### 1. Fee drag at mid-range probabilities
At `p = 0.50` the taker fee peaks at ~0.44%. The maker rebate recovers only ~0.088% of
that. The edge filter at 0.1% is deliberately loose  monitor realised P&L to confirm
it is creating positive-expectation markets.

### 2. Gamma approximation breaks down near expiry
The BS digital delta assumes continuous diffusion. In practice, short-dated bucket
markets (15min, 1H, daily near candle close) have discontinuous payoffs and extreme
gamma in the final minutes. The 6-hour exit stop is the primary mitigation. True
sub-second gamma re-hedging is not implemented.

### 3. Adverse selection latency gap
PM WebSocket  asyncio event loop  post-only round-trip  100500 ms. Systematic
arb bots operate sub-100 ms. During news events, stale quotes are picked off before
the callback fires. The adverse-selection probability halving is a partial model, not
a prevention.

### 4. Per-position stop is wrong for spread-capture
A hard -$25 stop on a single adversely-filled leg turns a temporary inventory
imbalance into a guaranteed loss. Correct behaviour is to widen the opposite side
quote and flatten passively. Inventory skew quoting partially addresses this but
does not fully replace the stop.

### 5. Paper fill rate overstates real fill frequency
`PAPER_FILL_PROB_BASE = 0.04` (was 0.10) is calibrated for a back-of-queue post-only
order. True single-account fill rate at the touch is likely 1–5% per 5-second tick.
Paper P&L should still be discounted by ~30–50% when estimating live performance
because the fill simulator cannot model queue position precisely.

---

# Appendix F  Bug Fixes Applied (March 2026)

| Issue | Root Cause | Fix Applied |
|-------|-----------|-------------|
| Zero fills in paper mode | Fill detector checked for inverted book (never occurs) | Competitive-at-touch model |
| Edge formula wrong sign | Subtracted taker fee instead of adding rebate | `effective_edge = half_spread + rebate` |
| Crude `delta = p_PM` hedge | No BS model; ignored strike, vol, time | Full BS digital delta + `implied_sigma` |
| Large unhedged window | Threshold $500; rebalance floor $100 | Threshold $200; rebalance floor $50 |
| Correlated coin blow-up | Only per-position stop; no coin-level limit | Coin aggregate loss limit $75 + per-coin cap 3 |
| Inventory skew absent | Mid always centred; inventory not reflected in quotes | Skewed mid (1c per $100, hard cap ±3c) |
| Unhandled WS callback exceptions | Bare `await` in callbacks kills asyncio task | `try/except` wrapping both callbacks |
| Strategies on by default | Defaults `True`; first scan opens positions before UI seen | Both strategies default `False` |
| Exit price used mid not bid/ask | Monitor closed positions at `book.mid` (gave free half-spread) | YES exits at YES `best_bid`; NO exits at NO `best_bid` |
| Inventory tracked in contracts | `record_fill()` received contract count not USD; skew overshooting | Passes `fill_cost_usd` (price × contracts) |
| Auto-consume of sub-min remainders | Non-CLOB: simulator flushed remainders below MIN immediately | Removed; reprice cycle cancels small remainders |
| Entry rebate double-counted | Monitor credited entry+exit rebates; fill_simulator now credits entry | Monitor credits exit-leg rebate only |
| Config comment wrong for adverse multiplier | Said "85% less likely" but code divides (boosts) probability | Comment updated to match actual behaviour |
| Orphaned one-sided positions | `can_open()` added full `quote_size` on top of existing leg → per-market cap falsely blocked re-quoting missing side | Skip `can_open` when market already has an open position; downstream `reserve_slot` + capital guards handle it correctly |
| Volume gate rejected new 24h markets | Used `discovered_at` (bot restart timestamp) to project activity — explodes at small elapsed values | Replaced with TTE-based proportional threshold; new markets always pass at launch |
| Partial fill adversely selected after HL move | Only stale-age check; fresh partial fill stayed at old price through fast moves | Added drift check: force reprice if `\|mid − quote.price\| > MAKER_ADVERSE_DRIFT_REPRICE` (1.5%) |
| Hedge spam during fill bursts | Every fill triggered immediate HL order | Added debounce + cooldown (`HEDGE_DEBOUNCE_SECS`, `HEDGE_MIN_INTERVAL`) |
| Hedge over-firing on small inventory changes | Rebalanced on any notional change | Guard: skip if change < `HEDGE_REBALANCE_PCT` (10%) of current notional |
| Vol filter window too wide for short-duration buckets | Fixed 300 s window for all market types | Adaptive: `min(300, market_duration / 4)` — bucket_5m uses 75 s |
| `stop()` left pending hedge tasks running | `stop()` only logged; debounce tasks continued | `stop()` now cancels all in-flight `_pending_hedge_tasks` |
| One-sided-book crossed quotes did not fill | Fill simulator returned `False` when one side of the PM book was missing | Added opposite-side fallback: BUY crosses `best_ask`; SELL crosses `best_bid` when only one side exists |
| Ghost positions persisted until manual cleanup | Reconciliation only logged wallet/bot mismatch and required manual dismiss | Reconciliation now auto-dismisses ghosts by closing stale risk positions immediately |
