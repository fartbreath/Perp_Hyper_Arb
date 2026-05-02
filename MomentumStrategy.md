# Momentum Strategy

## Core Concept

For any binary crypto price-resolution market:

1. Scan all open bucket markets (5 min / 15 min / 1 h / 4 h).
2. Find any market where **one side is trading between 0.80 and 0.90** (the other side is therefore 10-20c).
3. Identify **which direction spot has moved** relative to the strike.
4. Check: does the spot delta **confirm** the high-probability side?
   - If YES is at 80-90c: spot must be **above** strike by >= y%.
   - If NO is at 80-90c: spot must be **below** strike by >= y%.
5. If both conditions hold: **buy the 80-90c side** (whichever that is).

> **Direction-agnostic by design.** The strategy follows the crowd, not a fixed YES/NO bias.
> If spot is crashing hard into expiry, the NO side may be at 85c - that is the trade.
> If spot is ripping up, the YES side may be at 87c - that is the trade.
> The bot buys whichever token the market has already partially priced in, confirmed by spot momentum.

---

## Why It Works

### Two-Signal Filter

Entry requires **both signals to agree simultaneously**:

| Signal | What it means |
|--------|---------------|
| Token at 80-90c | The crowd believes this outcome is 80-90% likely |
| Spot delta > y% | The raw price data independently confirms the same direction |

Neither signal alone is sufficient. Together they eliminate most coin-flip situations.

### The Edge

Prediction markets reprice more slowly than spot order books. When spot moves hard, the true
fair probability may already be 93-97%, but the token is still only 85c. This strategy captures
that 8-12c lag before the market catches up.

### Why 80-90c and Not 90c+?

Above 90c the remaining edge is small and liquidity thins out. Below 80c the signal is genuinely
ambiguous - you need the price confirmation to already be a large multiple of remaining volatility.
The 80-90c band is the sweet spot: crowd conviction is high, but the prediction market has not
fully closed the gap yet.

### Adverse-Loss Protection

- You **never** buy the cheap 10-20c side - no lottery-ticket blowups.
- You only enter when downside (a hard reversal in the remaining time) is a statistical tail event
  (< 5-8% probability given the required delta).

---

## Core Agent Rules

Read before every task involving this strategy.

1. **Enter near expiry** (last ~60s of the bucket). Low TTE = tiny remaining vol = little room for spot to reverse. This is the source of edge.

2. **Hold YES/Up token** when `spot > strike` at entry. **Hold NO/Down token** when `spot < strike` at entry.

3. **Stop-loss is oracle-driven, not CLOB-driven.** The delta SL (`MOMENTUM_DELTA_STOP_LOSS_PCT`) fires when the Chainlink oracle spot retreats within threshold of the strike. The CLOB token price is used only for take-profit (`token → 0.999`). Do not use CLOB price drops alone as the primary SL signal — CLOB reprices forward and can collapse on book drain while the position is winning.

4. **Read `MOMENTUM_IMPL_PLAN.md` before making any code changes.** The plan is the current source of truth for what is being stripped, what is being added, and what config values are changing. The code in `scanner.py` and `monitor.py` may not yet match the plan — the plan takes precedence over what you find in the code.

5. **Oracle and market data feeds must be event-driven WebSocket streams, not HTTP polling.** Funding rate, mark price, CLOB depth, and oracle prices all come from WebSocket subscriptions. Polling is never acceptable for real-time data used by this strategy.

6. **PM Gamma API is the source of truth for settlement.** Use the CLOB `tokens[].winner` flag for final outcome. Never infer settlement from spot prices, PM UI prices, or `curPrice`.

7. **No GTD hedge on Momentum positions.** The GTD hedge has been deliberately removed from Momentum. All 3 real 5m/15m losses had $0 GTD recovery — the hedge fills only on reversal, not adverse trend (documented in `taker_hedge_design.md`). The `cl_upfrac` EWMA early exit (Phase 3) is the replacement: it fires before positions ride to expiry as a full loss. Do NOT add hedge placement back to Momentum. The preamble GTD hedge accounting rules still apply to Opening Neutral and Open strategy positions.

---

## Configuration

All parameters are set in `config.py` under the `MOMENTUM_*` namespace and can be hot-patched
via `config_overrides.json` without restarting the bot.

| Config Key | Default | Description |
|-----------|---------|-------------|
| `STRATEGY_MOMENTUM_ENABLED` | `False` | Master on/off for the scanner |
| `MOMENTUM_MAX_ENTRY_USD` | `50.0` | Maximum USDC deployed per position |
| `MOMENTUM_MIN_CLOB_DEPTH` | `200.0` | Minimum USDC depth on the ask side within 1c of best ask (thin-book guard) |
| `MOMENTUM_ORDER_TYPE` | `"limit"` | `"limit"` = taker limit at ask+0.5c; `"market"` = market order |
| `MOMENTUM_DELTA_STOP_LOSS_PCT` | `0.05` | Exit when \|(spot − strike) / strike\| reverses past this % against the position. Applied to HL spot vs strike — not the token CLOB price. Fires even when the token book is empty near expiry. |
| `MOMENTUM_DELTA_SL_MIN_TICKS` | `2` | Hysteresis: delta SL only fires after this many **consecutive** below-threshold oracle ticks. Prevents a single noisy tick from triggering an exit. Set to `1` to disable hysteresis. |
| `MOMENTUM_DELTA_SL_PCT_BY_COIN` | `{}` | Per-coin override for `MOMENTUM_DELTA_STOP_LOSS_PCT`. Higher-IV coins need wider stops — a single DOGE or HYPE tick routinely exceeds the global default. Falls back to the global value when the coin is not listed. Example: `{"BTC": 0.03, "SOL": 0.06, "DOGE": 0.08, "HYPE": 0.10}` |
| `MOMENTUM_TAKE_PROFIT` | `0.999` | Exit if held token rises above this |
| `MOMENTUM_MIN_TTE_SECONDS` | see below | Per-bucket-type dict of entry-window ceilings (seconds to expiry); markets with more TTE are outside the entry window and skipped |
| `MOMENTUM_MIN_TTE_SECONDS_DEFAULT` | `120` | Fallback TTE ceiling for any market type not listed in the dict |
| `MOMENTUM_PRICE_BAND_LOW` | `0.80` | Lower bound of the signal price band |
| `MOMENTUM_PRICE_BAND_HIGH` | `0.90` | Upper bound of the signal price band |
| `MOMENTUM_SCAN_INTERVAL` | `10` | Seconds between full scan passes (also max wake latency; event-driven entry can wake sooner) |
| `MOMENTUM_SPOT_MAX_AGE_SECS` | `30` | Discard if Pyth price update is older than this (stale spot guard) |
| `MOMENTUM_BOOK_MAX_AGE_SECS` | `60` | Discard if PM order book is older than this (stale book guard) |
| `MOMENTUM_VOL_CACHE_TTL` | `300` | Seconds to cache Deribit ATM IV before re-fetching |
| `MOMENTUM_VOL_Z_SCORE` | `1.6449` | Global z-score for the probability threshold (1.6449 ≈ 95th percentile) |
| `MOMENTUM_VOL_Z_SCORE_BY_TYPE` | `{}` | Per-bucket-type z-score overrides; unlisted types use the global default. Example: `{"bucket_daily": 1.0, "bucket_15m": 1.3}` |
| `MOMENTUM_MIN_DELTA_PCT` | `0.0` | Absolute minimum spot-to-strike gap (%) required to enter, independent of time bucket or vol regime. See [Absolute Floor Principle](#absolute-floor-principle). |
| `MOMENTUM_MIN_DELTA_PCT_BY_COIN` | `{}` | Per-coin override for `MOMENTUM_MIN_DELTA_PCT`. Low-priority under normal conditions (the vol-derived threshold usually dominates), but provides a tighter safety net for low-IV periods or oracle lag on high-vol coins. Falls back to the global value when the coin is not listed. Example: `{"SOL": 0.08, "DOGE": 0.10, "HYPE": 0.14}` |
| `MOMENTUM_MIN_GAP_PCT` | `0.0` | Minimum *additional* gap required above the effective vol-scaled threshold. Blocks marginal signals where delta barely clears the bar. `0.0` = disabled. Recommended live value: `0.02`. |
| `MOMENTUM_MAX_CONCURRENT` | `3` | Maximum simultaneous momentum positions |
| `MOMENTUM_MARKET_COOLDOWN_SECONDS` | `300` | Seconds to suppress re-entry after any open/close/failed attempt in a market (deduplication guard) |
| `MOMENTUM_MAX_TTE_DAYS` | `7` | Days of bucket markets subscribed for WS book data (independent of maker window) |
| `MOMENTUM_PRESUB_LOOKAHEAD` | `4` | Also subscribe to the next N not-yet-started bucket periods for pre-window data collection |
| `MOMENTUM_RANGE_ENABLED` | `False` | Enable range market sub-strategy ("between $X and $Y" markets) |
| `MOMENTUM_RANGE_PRICE_BAND_LOW` | `0.60` | Token price floor for range market entries |
| `MOMENTUM_RANGE_PRICE_BAND_HIGH` | `0.95` | Token price ceiling for range market entries |
| `MOMENTUM_RANGE_MAX_ENTRY_USD` | `25.0` | Max position size (USD) for range entries |
| `MOMENTUM_RANGE_VOL_Z_SCORE` | `0.80` | Vol z-score threshold for range market signals |
| `MOMENTUM_RANGE_MIN_TTE_SECONDS` | `300` | Min seconds to expiry before entering a range market |

**`MOMENTUM_MIN_TTE_SECONDS` defaults (per bucket type):**

| Bucket type | Default ceiling | Rationale |
|-------------|----------------|-----------|
| `bucket_5m` | 30 s | Last ~10% of a 300 s market |
| `bucket_15m` | 60 s | Last ~7% of a 900 s market |
| `bucket_1h` | 120 s | Last 2 minutes |
| `bucket_4h` | 300 s | Last 5 minutes |
| `bucket_daily` | 900 s | Last 15 minutes |
| `bucket_weekly` | 3 600 s | Last 1 hour |
| `milestone` | 1 800 s | Last 30 minutes (no fixed duration) |

### Quick-tune guide

- **More trades, lower bar**: lower `MOMENTUM_VOL_Z_SCORE` toward 1.28 (90th percentile), lower `MOMENTUM_MIN_CLOB_DEPTH`, raise `MOMENTUM_MAX_ENTRY_USD`.
- **Fewer trades, higher conviction**: raise `MOMENTUM_VOL_Z_SCORE` toward 2.0 (97.7%), raise `MOMENTUM_MIN_CLOB_DEPTH`.
- **Risk control**: lower `MOMENTUM_MAX_ENTRY_USD`, lower `MOMENTUM_MAX_CONCURRENT`.
- **Execution quality**: switch `MOMENTUM_ORDER_TYPE` to `"market"` for guaranteed fill at the cost of wider spread.
- **Per-bucket tuning**: add entries to `MOMENTUM_VOL_Z_SCORE_BY_TYPE` for specific bucket types (e.g. `{"bucket_daily": 1.0}` to lower the bar on daily markets only).
- **Entry window width**: adjust per-type values in `MOMENTUM_MIN_TTE_SECONDS` — smaller values = narrower window = higher certainty required.

---

## Dynamic Volatility

> **The static vol tables have been removed.** All thresholds are computed live from actual market volatility.

### Source Priority

| Priority | Source | Coins | TTL |
|----------|--------|-------|-----|
| 1 | **Deribit ATM IV** (nearest ~7-day call option, ATM strike ± spot) | BTC, ETH, SOL, XRP | `MOMENTUM_VOL_CACHE_TTL` (5 min) |
| 2 | **RTDS rolling realized vol** (log-return std from last 24h of RTDS price feed, min 10 samples) | Any coin in `config.TRACKED_UNDERLYINGS` | 60 s |
| 3 | **Skip signal** | Coins with no data | — |

**Why Deribit ATM IV as primary?**

Options markets price in *expected* future volatility, not just past realized vol. ATM IV on the 
nearest weekly option already reflects the regime (low vol = small threshold, high vol = larger 
threshold). This automatically widens the entry gate during volatile conditions and tightens it 
in quiet markets — exactly the adaptive behavior we need.

**Why RTDS rolling vol as fallback?**

For assets without Deribit options (HYPE, DOGE), the Polymarket RTDS feed (~1 tick/s/coin) provides
tick-level price history free from HL funding-rate basis. Rolling 24h log-return standard deviation
scaled to the relevant time horizon gives a reasonable realized-vol estimate. The `VolFetcher`
maintains a circular buffer (max 2,000 samples) per coin from the RTDS feed and recomputes on demand.

### Threshold Computation

The minimum required spot delta `y` toward the winning side is:

```
sigma_tau = sigma_ann * sqrt(TTE_seconds / 31_536_000)
y = MOMENTUM_VOL_Z_SCORE * sigma_tau * 100   (in percent)
```

A z-score of 1.6449 (default) means the spot must already be more than 1.65 standard deviations
into the winning territory — this corresponds to a fair probability of approximately 95%.

### Example

BTC, 1-hour bucket, Deribit ATM IV = 52% annualized (live):

```
TTE = 45 min = 2700 s
sigma_tau = 0.52 * sqrt(2700 / 31_536_000) = 0.52 * 0.00925 = 0.00481  (0.481%)
y = 1.6449 * 0.00481 * 100 = 0.79%
```

With a static table this would have been fixed at +0.65%. Dynamic vol correctly raises the bar
when the market is volatile, protecting against false entries in noisy conditions.

---

## Absolute Floor Principle

`MOMENTUM_MIN_DELTA_PCT` sets a hard minimum on the spot-to-strike gap, **independent of time bucket, vol regime, or z-score**.

### Why the z-gate alone isn't enough

The vol-scaled threshold `y = z × sigma_tau × 100` shrinks with both TTE and annualized vol:

```
sigma_tau = sigma_ann * sqrt(TTE_s / 31_536_000)
y = z * sigma_tau * 100
```

A low-vol coin (e.g. XRP with sigma_ann ≈ 0.28) at short TTE can produce a threshold as low as 0.065%.
A delta of 0.076% exceeds that threshold — the z-gate passes the trade — yet a single adverse tick
might be 0.02-0.05% on a thin asset. The position is one tick from going underwater at entry.

### The principle

**The absolute gap between spot and strike determines whether the position can survive a single
adverse tick. That tick risk is the same regardless of whether it's a 5m, 15m, or 1h market.**

A 0.05% spot-to-strike gap carries identical snap risk in a 5m bucket and a 15m bucket.
The time bucket changes how likely a reversal is over the full remaining window; it does not
change how close you are to the strike *right now*. The floor is not a TTE safeguard — it is
a minimum viable signal distance.

### Calibration

Set the floor to just above the smallest delta observed on a losing trade:

| Observed (March 31 2026 data) | Detail |
|-------------------------------|--------|
| Smallest losing delta | 0.076% (XRP, 5m bucket, TTE=63s) |
| Smallest winning delta | 0.084% (multiple assets) |
| **Recommended floor** | **0.08%** |

At 0.08%, the XRP loss above is blocked. All observed winners (minimum 0.084%) are preserved.

### Effective threshold

The scanner uses `max(y, MOMENTUM_MIN_DELTA_PCT)` as the effective gate:

```python
_effective_threshold = max(y, config.MOMENTUM_MIN_DELTA_PCT)
```

When vol is high (BTC at 80%+ IV), `y` will far exceed the floor and the floor has no effect.
When vol is low or TTE is short, the floor becomes the binding constraint — which is exactly when
thin-gap entries are most dangerous.

---

## Stale Signal Guards

Eight independent checks must all pass before a signal is acted upon.
**Any single failure silently skips the market** — no error logged unless `DEBUG` is enabled.

| Guard | Check | Threshold | Rationale |
|-------|-------|-----------|-----------|
| **Beyond horizon** | `tte <= 0 or tte > MOMENTUM_MAX_TTE_DAYS * 86400` | 7 days | Markets outside the WS subscription window have no book data |
| **Not yet started** | `tte > market_duration` and beyond `PRESUB_LOOKAHEAD` | per type | Market hasn't started; skip unless within the lookahead window |
| **Cooldown** | `now - last_touch < MOMENTUM_MARKET_COOLDOWN_SECONDS` | 300 s | Suppress re-entry after any recent open/close/failed attempt |
| **Stale PM book** | `time.time() - book.timestamp > MOMENTUM_BOOK_MAX_AGE_SECS` | 60 s | Book may not reflect current market state |
| **Empty book** | `not book.bids and not book.asks` | — | No levels at all — MMs have withdrawn; no meaningful price |
| **Stale spot price** | `time.time() - spot_price.timestamp > MOMENTUM_SPOT_MAX_AGE_SECS` | 30 s | Delta computation relies on fresh spot |
| **Stale vol** | `MOMENTUM_VOL_CACHE_TTL` exceeded and no fresh Deribit response | 300 s | Threshold computation unreliable with old IV |
| **Missing spot** | `spot_price is None or spot_price.mid is None` | — | No spot = no delta computation |
| **Missing book** | `book is None or book.best_ask is None` | — | No ask = cannot determine token price or depth |
| **Price re-check at execution** | Re-fetch book right before placing order; skip if price moved out of band | band + 2c | Market may reprice between signal detection and execution |

---

## Signal Logic (Bi-Directional)

```
For each open bucket market M:

    # ── Horizon pre-filter ────────────────────────────────────────────────
    tte_pre = M.end_date.timestamp() - now_ts
    if tte_pre <= 0 or tte_pre > MOMENTUM_MAX_TTE_DAYS * 86400: continue
    dur = market_duration(M.market_type)
    if dur is not None and tte_pre > dur:
        if not within_presub_lookahead(tte_pre, dur, MOMENTUM_PRESUB_LOOKAHEAD): continue

    # ── Per-market cooldown ───────────────────────────────────────────────
    if now_ts - market_cooldown[M.condition_id] < MOMENTUM_MARKET_COOLDOWN_SECONDS: continue

    # ── PM book for YES token ─────────────────────────────────────────────
    book_yes = pm.get_book(M.token_id_yes)
    if book_yes is None or book_yes.mid is None: continue

    # ── Stale + empty book gates ──────────────────────────────────────────
    if now_ts - book_yes.timestamp > MOMENTUM_BOOK_MAX_AGE_SECS: continue
    if not book_yes.bids and not book_yes.asks: continue  # empty book

    p_yes = book_yes.mid
    book_no = pm.get_book(M.token_id_no)
    if book_no is None or book_no.mid is None: continue
    p_no = book_no.mid

    # ── Find which side is in the target band ─────────────────────────────
    if MOMENTUM_PRICE_BAND_LOW <= p_yes <= MOMENTUM_PRICE_BAND_HIGH:
        high_side  = "YES"
        token_id   = M.token_id_yes
        required_direction = "spot_above_strike"

    elif MOMENTUM_PRICE_BAND_LOW <= p_no <= MOMENTUM_PRICE_BAND_HIGH:
        high_side  = "NO"
        token_id   = M.token_id_no
        required_direction = "spot_below_strike"

    else:
        continue  # no side in band

    # ── Stale spot gate ───────────────────────────────────────────────────
    # Oracle is routed by market type: on-chain Chainlink (primary) / RTDS WS (fallback)
    # for 5m/15m/4h; RTDS exchange-aggregated feed for 1h/daily/weekly.
    if M.market_type in CHAINLINK_MARKET_TYPES:
        spot_price = spot_client.get_mid_chainlink(M.underlying)
    else:
        spot_price = spot_client.get_mid(M.underlying)
    if spot_price is None: continue
    if now_ts - spot_price.timestamp > MOMENTUM_SPOT_MAX_AGE_SECS: continue
    spot = spot_price.mid

    # ── Parse strike from market title ────────────────────────────────────
    # Standard price-level markets: extract "$68,300", "$68k", etc.
    strike = extract_strike(M.title, spot)

    # "Up or Down" directional markets: implicit strike = spot at window open.
    # Recorded on first observation and persisted to data/market_open_spots.json
    # so restarts don't lose the reference price mid-window.
    if strike is None and is_updown_market(M.title):
        if M.condition_id not in market_open_spot:
            market_open_spot[M.condition_id] = spot
            save_open_spots(market_open_spot)
        strike = market_open_spot[M.condition_id]

    if strike is None: continue

    # ── Per-bucket TTE gate (entry window ceiling) ────────────────────────
    tte_seconds = (M.end_date - now).total_seconds()
    min_tte = MOMENTUM_MIN_TTE_SECONDS.get(M.market_type, MOMENTUM_MIN_TTE_SECONDS_DEFAULT)
    _blocked_by_tte = tte_seconds > min_tte
    # Note: we continue past this gate to compute vol/delta for diagnostics.
    # Trading is blocked below after all diag fields are populated.

    # ── Dynamic vol threshold ─────────────────────────────────────────────
    sigma_ann, vol_src = await vol_fetcher.get_sigma_ann(M.underlying)
    if sigma_ann is None: continue  # stale vol guard

    sigma_tau = sigma_ann * sqrt(tte_seconds / 31_536_000)
    # Per-bucket z-score override (falls back to global default)
    z = MOMENTUM_VOL_Z_SCORE_BY_TYPE.get(M.market_type, MOMENTUM_VOL_Z_SCORE)
    y = z * sigma_tau * 100  # entry threshold in percent

    # ── Compute signed delta toward winning direction ──────────────────────
    if required_direction == "spot_above_strike":
        delta_pct = (spot - strike) / strike * 100   # positive = good for YES
    else:
        delta_pct = (strike - spot) / strike * 100   # positive = good for NO

    gap_pct    = delta_pct - y             # +ve = above threshold, -ve = below
    observed_z = delta_pct / (sigma_tau * 100) if sigma_tau > 0 else None

    # Now enforce the TTE gate (diag fields populated above for research)
    if _blocked_by_tte: continue

    # ── Gate: delta must exceed dynamic threshold ─────────────────────────
    _effective_threshold = max(y, MOMENTUM_MIN_DELTA_PCT)
    if delta_pct < _effective_threshold: continue

    # ── Gate: minimum gap above threshold ────────────────────────────────
    # Blocks marginal signals that barely clear the threshold.
    if MOMENTUM_MIN_GAP_PCT > 0 and (delta_pct - _effective_threshold) < MOMENTUM_MIN_GAP_PCT: continue

    # ── Gate: no existing position in this market ─────────────────────────
    if any(p.market_id == M.condition_id for p in risk.get_open_positions()): continue

    # ── Gate: concurrent position cap ────────────────────────────────────
    if open_momentum_count >= MOMENTUM_MAX_CONCURRENT: continue

    # ── Gate: CLOB depth check ────────────────────────────────────────────
    book_target = pm.get_book(token_id)
    if book_target is None or book_target.best_ask is None: continue
    ask_depth_usd = sum(s * p for (p, s) in book_target.asks
                        if p <= book_target.best_ask + 0.01)
    if ask_depth_usd < MOMENTUM_MIN_CLOB_DEPTH: continue

    # ── FIRE: execute immediately ─────────────────────────────────────────
    execute_signal(MomentumSignal(
        market=M, side=high_side, token_id=token_id,
        token_price=book_target.best_ask,
        delta_pct=delta_pct, threshold_pct=y, gap_pct=gap_pct,
        spot=spot, strike=strike, tte_seconds=tte_seconds,
        sigma_ann=sigma_ann, vol_source=vol_src,
    ))
    market_cooldown[M.condition_id] = now_ts
    open_momentum_count += 1
```

---

## Volatility & Threshold — Live Examples

> Unlike the previous version, these numbers change every 5 minutes as Deribit IV updates.
> The table below is illustrative only; actual thresholds are computed at runtime.

| Asset | Deribit ATM IV (live) | TTE | sigma_tau | Threshold y (z=1.645) |
|-------|----------------------|-----|-----------|----------------------|
| BTC | 0.52 | 5 min | 0.104% | **0.17%** |
| BTC | 0.52 | 15 min | 0.180% | **0.30%** |
| BTC | 0.52 | 1 hour | 0.360% | **0.59%** |
| BTC | 0.52 | 4 hours | 0.719% | **1.18%** |
| ETH | 0.68 | 1 hour | 0.471% | **0.78%** |
| SOL | 0.78 | 1 hour | 0.541% | **0.89%** |

*Raise `MOMENTUM_VOL_Z_SCORE` to 2.0 or 2.33 for a stricter gate (97.7%, 99th percentile).*

---

## Entry Parameters

| Parameter | Config Key | Default | Rationale |
|-----------|-----------|---------|-----------|
| Price band low | `MOMENTUM_PRICE_BAND_LOW` | 0.80 | Sweet spot: crowd conviction without full repricing |
| Price band high | `MOMENTUM_PRICE_BAND_HIGH` | 0.90 | Above 90c: thin liquidity, minimal remaining edge |
| Order type | `MOMENTUM_ORDER_TYPE` | `"limit"` | `"limit"` = ask+0.5c (taker, ensures fill); `"market"` = immediate cross |
| Max size | `MOMENTUM_MAX_ENTRY_USD` | 50.0 USDC | Fixed USDC per trade; start at 10-50 USDC |
| Entry window (per type) | `MOMENTUM_MIN_TTE_SECONDS` | dict | Only enter when TTE ≤ this value for the market's type (e.g. 120 s for 1-hour buckets, 900 s for daily) |
| Entry window fallback | `MOMENTUM_MIN_TTE_SECONDS_DEFAULT` | 120 s | Used for any market type not listed in the dict |
| Min depth | `MOMENTUM_MIN_CLOB_DEPTH` | 200 USDC | Thin book guard: ensures our fill doesn't exhaust liquidity |
| Max concurrent | `MOMENTUM_MAX_CONCURRENT` | 3 | Cap correlated momentum exposure |

---

## Exit Rules

Four exit conditions evaluated in priority order. **Delta SL is checked first and does not require the CLOB book to be available** — this is critical because near expiry the NO book often drains completely before the market resolves.

| Priority | Condition | Config Key | Default | Action | Rationale |
|----------|-----------|-----------|---------|--------|-----------|
| 1 | Spot crosses strike against position by > stop-loss % | `MOMENTUM_DELTA_STOP_LOSS_PCT` | `0.05` | Taker-sell held token | Underlying moved against us — genuine adverse signal. Fires on RTDS/Chainlink oracle ticks even if token CLOB book is drained/empty. |
| 2 | Near expiry and spot has crossed strike (delta < 0) | `MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS` | 60 s | Taker-sell held token | Avoid binary snap to zero close to resolution when already losing |
| 3 | Token price >= take-profit | `MOMENTUM_TAKE_PROFIT` | `0.999` | Taker-sell held token | Capture near-certainty gains |
| 4 | Expiry | — | — | Hold to resolution | If no other trigger fires, let the market resolve naturally |

**Why delta SL (not CLOB-price SL)?**

Previous versions used a CLOB-price stop (`MOMENTUM_STOP_LOSS_YES/NO = 0.5`). This was replaced because:
- Near expiry, MMs withdraw from the book. The NO token mid becomes `None`, making a CLOB-price stop impossible to evaluate.
- CLOB price collapses (book thinning) are not the same as genuine adverse spot moves. A thin-book bounce from 0.80 to 0.60 can reverse to 0.85 within a single second.
- Delta SL fires on **underlying price vs strike** — a harder, less-gameable signal that works whether or not the token book is live.

**Token price mapping (monitor):**

- YES position: `token_price` = current YES CLOB mid (falls back to `current_price` if not available)
- NO position: `token_price` = current NO CLOB mid; **delta SL fires even if NO book is empty**

> Exits are handled by `monitor.py` in `should_exit()` under `pos.strategy == "momentum"`.
> The delta SL block runs first in the function, before any token-price fetch, so NO-book drainage
> near expiry no longer silences the stop-loss.

---

## Duplicate Market Guard

Before entering, check all open positions across ALL strategies:

- If any strategy already holds a position in `market.condition_id` → **skip**.
- The scanner uses `risk.get_open_positions()` filtered by `market_id`.
- A per-market cooldown (`MOMENTUM_MARKET_COOLDOWN_SECONDS`, default 300 s) suppresses re-entry after any open, close, or failed attempt. This prevents duplicate orders within the same scan pass and avoids re-entering a cooling market.
- A final duplicate re-check runs inside `_execute_signal` just before order placement to guard against the race condition where two scan passes both detect the same signal.

---

## Failure Mode Map

| Scenario | Guard | Resolution |
|----------|-------|------------|
| Spot reverses immediately after entry | Delta SL (`MOMENTUM_DELTA_STOP_LOSS_PCT`): exit when spot moves > 0.05% past strike against position | Fires on RTDS/Chainlink oracle ticks (~1/s per source); does not require the token CLOB book to be live |
| NO token book drains near expiry (MMs withdraw) | Delta SL evaluated before any NO-book fetch; book drain no longer silences the stop | Delta SL fires regardless of CLOB state |
| Market already at 94c when scanner runs | Upper band check (< 0.90 strictly) | Signal filtered out |
| Signal fires outside entry window | Per-bucket TTE gate (`MOMENTUM_MIN_TTE_SECONDS`) | Signal filtered out |
| Spot price data is stale (> 30s old) | Stale spot price guard | Signal skipped |
| PM book data is stale (> 60s old) | Stale book guard | Signal skipped |
| PM book has no levels (MMs withdrawn) | Empty book guard | Signal skipped |
| Deribit IV fails to fetch | Fallback to RTDS rolling vol; if also absent, skip | Signal skipped; logs at DEBUG |
| Pyth rolling vol has < 10 samples | Vol fetcher returns None | Signal skipped (threshold lowered from 20 to reduce cold-start drops) |
| Marginal signal: delta barely clears threshold | `MOMENTUM_MIN_GAP_PCT` minimum gap above threshold | Blocks signals where a single adverse tick reverses the position |
| Thin liquidity at ask | CLOB depth gate (> 200 USDC) | Signal skipped |
| Price moved out of band before execution | Price re-check at execution time (band + 2c tolerance) | Order not placed |
| Same market already held by any strategy | Duplicate guard + cooldown | Skip entry |
| Bot restarted mid-window for Up/Down market | `data/market_open_spots.json` persists open-spot cache | Correct strike restored from disk |
| RTDS WS feed silently stops delivering | Health-log loop: warning if >30 s without exchange prices | Operator alerted to investigate |
| On-chain Chainlink silent for 120 s | `_onchain_chainlink_loop` zombie-reconnect | Automatic reconnect; RTDS WS fallback serves in the interim |
| Deribit IV cached from high-vol period | Cache TTL 5 min; stale vol widens threshold | Conservative; may miss some trades |
| Vol regime spikes 3x (black swan) | z-score scales threshold with vol | Naturally filters out noisy markets |
| Low-vol coin passes z-gate with tiny gap | `MOMENTUM_MIN_DELTA_PCT` absolute floor (independent of bucket) | Blocks trades where a single tick would cross strike; see [Absolute Floor Principle](#absolute-floor-principle) |
| NO/DOWN position immediately stop-lossed after entry (dip market) | Dip-market delta direction fix: if `entry_spot > strike` swap sign of delta formula in `should_exit()` and `_write_momentum_tick()` | Reach-market NO wins when spot < strike; dip-market NO wins when spot > strike — formula now inferred from `pos.spot_price` recorded at entry |
| Kelly `f* < 0` (negative expected value) | Kelly negative-EV guard: `_compute_kelly_size_usd` returns 0 when `raw_kelly_f < 0` | `MOMENTUM_MIN_ENTRY_USD` floor is not applied; signal skipped; `kelly_f_raw` logged for audit |

---

## Oracle Routing

The bot uses two independent price sources, both delivered by `RTDSClient`:

| Market type | Primary oracle | Fallback |
|-------------|---------------|---------|
| `bucket_5m`, `bucket_15m`, `bucket_4h` | **On-chain Chainlink** (Polygon WSS `AnswerUpdated` events) | RTDS WS `crypto_prices_chainlink` topic |
| `bucket_1h`, `bucket_daily`, `bucket_weekly` | RTDS WS `crypto_prices` topic | — |
| HYPE (all types) | RTDS WS `crypto_prices_chainlink` topic | — (no Chainlink contract on Polygon) |

**Why on-chain Chainlink as primary?**

Polymarket resolves 5m/15m/4h markets by reading `latestAnswer()` from the Chainlink AggregatorV3
contract on Polygon at the exact expiry block. Subscribing to `AnswerUpdated` events directly means
the bot tracks the exact price that will be used for resolution — not a proxy. This is particularly
important for the delta stop-loss: using `get_mid_chainlink()` to evaluate SL correctness means we
exit based on what Polymarket *will* read, not what an exchange-aggregated feed says.

On-chain events fire on ≥0.5% price moves or the ~27-minute heartbeat. Between events, the
`crypto_prices_chainlink` RTDS WS topic provides ~1 tick/s and bridges gaps, but all SL/entry
decisions use the on-chain value as first choice.

---

## Range Markets Sub-Strategy

> Controlled by `MOMENTUM_RANGE_ENABLED` (default: `False`).

**Market format:** "Will BTC be between $X and $Y?" — YES resolves $1 if spot at expiry is inside
the range [lo, hi]; NO resolves $1 if spot is outside.

**Signal logic:** The scanner detects range markets via `_is_range_market()` (regex match on "between
$X and $Y"). Both boundaries are extracted; the delta formula is bidirectional:

```
# For YES token (spot must be inside range at expiry):
delta_to_lower = (spot - lo) / strike_mid * 100   # positive = above lower bound
delta_to_upper = (hi - spot) / strike_mid * 100   # positive = below upper bound
delta_pct = min(delta_to_lower, delta_to_upper)    # worst-case distance to either boundary
```

Treated as a regular single-leg momentum entry after the delta check. Strategy label is `"range"` so
the Positions page groups range trades separately from directional momentum trades.

**Independent config knobs:**

| Config Key | Default | Description |
|-----------|---------|-------------|
| `MOMENTUM_RANGE_ENABLED` | `False` | Master on/off for range market scanning |
| `MOMENTUM_RANGE_PRICE_BAND_LOW` | `0.60` | Token price floor for range entries |
| `MOMENTUM_RANGE_PRICE_BAND_HIGH` | `0.95` | Token price ceiling for range entries |
| `MOMENTUM_RANGE_MAX_ENTRY_USD` | `25.0` | Position size cap for range entries |
| `MOMENTUM_RANGE_VOL_Z_SCORE` | `0.80` | Signal threshold (lower than directional — range YES decays differently) |
| `MOMENTUM_RANGE_MIN_TTE_SECONDS` | `300` | Min TTE before entering (range markets often have longer durations) |
| Concurrent position cap hit | `MOMENTUM_MAX_CONCURRENT` gate | Signal dropped until slot opens |
| Per-bucket vol bar too strict | `MOMENTUM_VOL_Z_SCORE_BY_TYPE` override for that type | Tune per-type without affecting others |

---

## Implementation Architecture

```
strategies/Momentum/
├── __init__.py          # module init
├── signal.py            # MomentumSignal dataclass + edge_pct property
├── vol_fetcher.py       # VolFetcher: Deribit ATM IV + Pyth rolling realized vol + prefetch task
└── scanner.py           # MomentumScanner: event-driven scan + direct execution + diagnostics
```

Persisted state:
```
data/market_open_spots.json   # window-open spot cache for Up/Down markets (survives restart)
```

### Data flow

```
PythClient (~400 ms ticks) ──→ VolFetcher._on_pyth() ──→ rolling mid buffer
         │                                               ↓ (fallback)
         │             DeribitFetcher ──────→ VolFetcher.get_sigma_ann()
         │                                               ↓
         │             VolFetcher.start_prefetch() → background warm-up task
         │
         ├──→ MomentumScanner._on_pyth_spot_update_entry(coin, price)
         │         ↓ (coin matches a tracked underlying? wake immediately)
         │    asyncio.Event.set()
         │
         └──→ PositionMonitor._on_pyth_spot_update(coin, price)
                   ↓ (for each open momentum position with matching underlying)
                   should_exit() — delta SL runs first (no CLOB book required)

PMClient (WS price ticks) ──→ MomentumScanner._on_price_update_entry()
                                            ↓ (band hit? wake immediately)
                                     asyncio.Event.set()
                                            ↓
PMClient (WS price ticks) ──→ PositionMonitor._on_price_update()
                                     ↓ (for open momentum positions)
                                 should_exit() — delta SL + take profit

PMClient (books) ─────────────→ MomentumScanner._scan_once()
                                        ↓ (signal passes all gates)
                                 _execute_signal()
                                        ↓
                                 WS MATCHED event (fill detect, ~0 ms)
                                        ↓ (timeout fallback)
                                 pm.get_order_fill_rest()
                                        ↓
                                 risk.open_position()
                                        ↓
                          PositionMonitor (poll fallback every 5s; event-driven
                          on PM WS ticks + Pyth oracle ticks)
                                        ↓
                          should_exit() — delta SL / near-expiry / take-profit

MomentumScanner._last_scan_diags ──→ GET /momentum/diagnostics
MomentumScanner._on_signal cb ────→ GET /momentum/signals
```

### Key design decisions

1. **No agent loop** — momentum is time-critical; scanner executes directly without Ollama evaluation.
2. **Fully event-driven entry** — both PM CLOB ticks (`_on_price_update_entry`) and Pyth oracle ticks (`_on_pyth_spot_update_entry`) wake the scan loop immediately. The scan loop uses `asyncio.Event.wait()` with a timeout so it fires on either signal type, whichever arrives first.
3. **Fully event-driven exit** — both PM CLOB ticks (`_on_price_update`) and Pyth oracle ticks (`_on_pyth_spot_update`) trigger `_check_position` for open momentum positions. The 5-second poll loop (`MONITOR_INTERVAL`) is a safety-net fallback only. This eliminates the latency gap where a spot move through the strike goes undetected while the PM book is quiet.
4. **Delta SL before CLOB check** — `should_exit()` evaluates the delta stop-loss against Pyth oracle spot vs strike **before** reading the token CLOB price. This ensures the stop fires even when the NO book is fully drained near expiry (MMs withdraw first, markets drain after).
5. **No queue** — direct execution prevents stale signals accumulating.
6. **Re-check at execution** — price is re-validated right before order placement to guard against fast moves between detection and execution.
5. **WS fill detection** — a one-shot `asyncio.Future` is registered before awaiting anything; the MATCHED WS event resolves it (~0 ms, zero REST calls). REST fallback fires only on timeout (5 s).
6. **Token-native entry price** — both YES and NO entries are stored as the held token's actual fill price (no YES-space conversion).
7. **Shared risk engine** — `risk.open_position()` and `risk.get_positions()` are shared with maker/mispricing, ensuring cap enforcement is global.
8. **Shared monitor** — `PositionMonitor` handles momentum exits via `pos.strategy == "momentum"` branch in `should_exit()`. No separate monitor loop needed.
9. **`_blocked_by_tte` pattern** — the TTE gate does not short-circuit the pipeline; vol and delta are computed for all in-band markets regardless, so the diagnostics CSV contains full price-vs-TTE empirical data even for markets outside the entry window.

---

## Current Development Phase

> Full detail in `MOMENTUM_IMPL_PLAN.md`. This section captures the strategic direction so the strategy doc stays current. Source of all data-validated decisions: `strategy_update.md`.

### Phase 0 — Strip (before adding anything)

Removing accreted complexity with no validated positive EV:

| Item | What | Why |
|------|------|-----|
| VWAP/RoC PM token filter | `_price_history` deque + gate in `scanner.py` | PM VWAP is not a validated predictor. Replaced by oracle TWAP deviation (Phase 1). |
| WinRateTable empirical gate | `win_rate.py` gate | Too sparse (<500 fills). Replaced by funding rate gate (Phase 2). |
| Kelly persistence z-boost | `_signal_first_valid` dict | Over-engineering. Funding gate is the correct conviction signal. |
| CalendarSpread Mixin | `CalendarSpreadMixin` in `MomentumScanner` | Different strategy, different risk profile. Extract to `strategies/CalendarSpread/`. |
| GTD hedge | Hedge placement in `scanner.py` Phase D + `MOMENTUM_HEDGE_*` config keys + hedge tracking in `monitor.py` | All 3 real 5m/15m losses had $0 GTD recovery. Hedge fills only on reversal, not adverse trend. `cl_upfrac` EWMA exit (Phase 3) is the replacement. See `taker_hedge_design.md`. |

### Phase 1 — Reusable Data Pipelines (strategy-agnostic, `market_data/`)

| Pipeline | Source | Interface | Consumers |
|----------|--------|-----------|----------|
| `FundingRateCache` | HL WebSocket (`webData2`) | `get(coin) → float \| None` | Momentum (entry gate), Open, Opening Neutral |
| `PMClient.get_depth_share()` | PM CLOB WS (cached) | `get_depth_share(market) → float \| None` | Momentum (entry gate), Open |
| `OracleTickTracker` | CL oracle ticks | `get_upfrac_ewma(coin)`, `get_twap_deviation_bps(coin)`, `get_vol_regime(coin)` | Momentum (exit + TWAP gate), Open |

### Phase 2 — Entry Signal Changes

| Change | Type | Config Key | Status |
|--------|------|-----------|--------|
| Per-coin SL values | Config only | `MOMENTUM_DELTA_SL_PCT_BY_COIN` | Enabled immediately |
| Funding rate gate | Hard block + z-boost | `MOMENTUM_FUNDING_GATE_ENABLED` | Enabled (requires P1) |
| Depth share gate | Hard block | `MOMENTUM_DEPTH_SHARE_GATE_ENABLED` | Enabled (requires P1) |
| TWAP deviation gate | Z-score multiplier | `MOMENTUM_TWAP_GATE_ENABLED` | Enabled (requires P1) |
| Hour-of-day bias | Z-score multiplier | `MOMENTUM_HOUR_BIAS_ENABLED` | **DISABLED — single-day data only** |
| DOGE streak bias | Z-score multiplier | `MOMENTUM_STREAK_BIAS_ENABLED` | **NOT BUILT — multi-day evidence required** |

### Phase 3 — Exit Management

| Change | Signal | Config Key | Status |
|--------|--------|-----------|--------|
| `cl_upfrac` EWMA exit | `upfrac_ewma < 0.40` for 2+ consecutive 5s windows (AUC=0.703) | `MOMENTUM_UPFRAC_EXIT_ENABLED` | Enabled (requires P1) |
| Dynamic SL width | `max(per_coin_floor, 0.5 × prev_bucket_vol_60s)` | `MOMENTUM_DYNAMIC_SL_ENABLED` | **DEFERRED — needs 2+ weeks of vol data** |

### Updated Gate Pipeline (post-implementation)

```
for market in bucket_markets:
    ├── horizon / cooldown / book / TTE        (existing)
    ├── signal band: token 0.80–0.90           (existing)
    ├── effective_z = base_z × funding_conviction_mult
    │                       × twap_dev_mult (low-vol only)
    │                       × streak_mult   (deferred)
    ├── delta >= effective_z threshold         (existing, now uses multiplied z)
    ├── CLOB depth gate                        (existing)
    ├── [REMOVED] VWAP/RoC filter
    ├── [REMOVED] WinRateTable gate
    ├── funding rate direction gate             (NEW — hard block if contradicts direction)
    ├── depth share gate                        (NEW — block if crowd book contradicts)
    └── ENTER POSITION
```

---

## Implementation Checklist

### P0 Strip (before adding anything)

- [ ] Remove GTD hedge placement from `scanner.py` (Phase D block) and `MOMENTUM_HEDGE_*` config keys
- [ ] Remove GTD hedge tracking from `monitor.py` for Momentum positions
- [ ] Remove VWAP/RoC PM token filter (`_price_history` + gate)
- [ ] Remove WinRateTable gate (`win_rate.py` import + `skipped_win_rate` counter)
- [ ] Remove Kelly persistence z-boost (`_signal_first_valid` dict + z-boost logic)

### MVP (required before any live capital)

- [x] `strategies/Momentum/signal.py` — `MomentumSignal` dataclass + `edge_pct` property
- [x] `strategies/Momentum/vol_fetcher.py` — `VolFetcher` with Deribit + HL fallback + `start_prefetch()`
- [x] `strategies/Momentum/scanner.py` — scanner loop + event-driven entry + direct execution + diagnostics
- [x] Config keys: all `MOMENTUM_*` in `config.py` (including per-bucket TTE dict, z-score overrides, cooldown, horizon)
- [x] `monitor.py` — `ExitReason.MOMENTUM_STOP_LOSS/TAKE_PROFIT` + side-specific stops in `should_exit()`
- [x] `main.py` — init `MomentumScanner`, start as asyncio task
- [x] Event-driven entry via `pm.on_price_change()` callback and `pyth.on_price_update()` callback
- [x] Per-bucket `MOMENTUM_MIN_TTE_SECONDS` dict with per-type entry windows
- [x] Per-bucket `MOMENTUM_VOL_Z_SCORE_BY_TYPE` overrides
- [x] "Up or Down" directional market support (implicit strike persisted to disk)
- [x] Pyth outage detection (>50% stale-price warning)
- [x] `/momentum/diagnostics` API endpoint backed by `_last_scan_diags`
- [x] WS subscription refresh every 5 minutes (new bucket markets picked up automatically)
- [x] Unit tests for `vol_fetcher.py` (mock Deribit + HL)
- [x] Unit tests for `scanner.py` stale signal guards and Up/Down market handling
- [x] Unit tests for `monitor.py` momentum exits (side-specific stop-loss)

### Pre-launch (required before going live)

- [ ] **Backtest** against historical PM + HL data in `/Backtest` environment
- [ ] Validate actual win rate vs theoretical 93-97%
- [ ] Validate `MOMENTUM_VOL_Z_SCORE` against real PM microstructure
- [ ] Paper-trade for >= 48h before live capital; monitor stop-loss frequency

### v2 (deferred)

- [x] ~~Kelly sizing based on edge and bankroll~~ → shipped; see [Kelly Sizing](#kelly-sizing) section
- [ ] Regime detection: if Deribit IV > 2x trailing average → raise z-score automatically
- [ ] Telegram alerts on momentum fills
- [x] ~~Per-bucket-type z-score overrides (5m markets may need higher gate)~~ → shipped via `MOMENTUM_VOL_Z_SCORE_BY_TYPE`

---

## Recent Additions

These features were shipped after the initial MVP and are documented here for reference.

### Event-Driven Entry

Instead of relying solely on the periodic `MOMENTUM_SCAN_INTERVAL` poll, the scanner registers a
price-change callback (`pm.on_price_change`) with the PMClient WS layer. When any YES or NO token
price enters the signal band `[MOMENTUM_PRICE_BAND_LOW, MOMENTUM_PRICE_BAND_HIGH]` the callback
fires an `asyncio.Event`, waking the scan loop immediately.

The callback performs only a lightweight pre-filter (band check + cooldown check) on the hot path.
Full gate evaluation still runs in `_scan_once()`. This reduces max entry latency from up to
`MOMENTUM_SCAN_INTERVAL` seconds (10 s) to near-zero on a fresh band-entry tick.

### "Up or Down" Directional Markets

Some Polymarket bucket markets are framed as "Will BTC go up or down in the next 15 minutes?"
rather than referencing a specific strike. For these markets there is no dollar level to parse.

The scanner detects these using `_is_updown_market(title)` and records the HL spot price at
the moment the market window opens as the implicit strike. This reference price is persisted to
`data/market_open_spots.json` so a bot restart mid-window doesn't lose the recorded open.

### Per-Bucket Entry Windows and Z-Score Overrides

`MOMENTUM_MIN_TTE_SECONDS` is now a `dict[str, int]` mapping `market_type` → TTE ceiling. Each
bucket type has its own entry window that reflects the fraction of market life where edge is
meaningful (e.g. last 30 s for 5-minute buckets, last 15 min for daily buckets).

`MOMENTUM_VOL_Z_SCORE_BY_TYPE` allows a different z-score per bucket type without touching the
global default. Longer time horizons may warrant a lower bar (slower reversion relative to TTE);
very short buckets (5m) may warrant a higher bar due to noise.

The `_blocked_by_tte` pattern keeps the data flowing to diagnostics even for markets outside the
entry window — this means the diag CSV captures full price-vs-TTE curves for research purposes.

### Volatility Pre-Warming

`VolFetcher.start_prefetch(underlyings)` launches a background asyncio task that refreshes
Deribit ATM IV for all tracked underlyings at startup and every `MOMENTUM_VOL_CACHE_TTL` seconds.
This ensures the first scan pass never has to wait for a live Deribit round-trip (~200 ms stall).

### WS Subscription Refresh

The scanner refreshes its PMClient WS book subscriptions every 5 minutes (`_SUBSCRIPTION_REFRESH_INTERVAL = 300`). New bucket markets that go live after bot startup are automatically picked up within one refresh cycle without requiring a restart.

### Diagnostics API

`GET /momentum/diagnostics` returns the per-market snapshot from the last completed `_scan_once`
pass. Each entry contains all intermediate values: `p_yes`, `p_no`, `book_age_s`, `spot_age_s`,
`strike`, `tte_seconds`, `sigma_ann`, `sigma_tau`, `threshold_pct`, `delta_pct`, `gap_pct`,
`observed_z`, `vol_source`, `ask_depth_usd`, and `skip_reason`. A `summary` dict with aggregate
skip-reason counts is included for quick scan-health inspection.

The `gap_pct` field (`delta_pct - threshold_pct`) and `observed_z` field are particularly useful
for calibrating `MOMENTUM_VOL_Z_SCORE` and `MOMENTUM_MIN_TTE_SECONDS` from real market data.

### Pyth Outage Detection

If more than 50% of all bucket markets are skipped for stale Pyth price in a single scan pass,
the scanner logs a `WARNING: possible Pyth WS outage`. This distinguishes a genuine feed outage
from isolated stale-price misses and alerts operators to investigate quickly.

### Dip-Market NO Delta Direction Fix

Binary bucket markets come in two flavours for the NO token:

| Flavour | Example title | NO wins when |
|---------|--------------|--------------|
| Reach market | "Will ETH reach $2,500 by 3 PM?" | `spot < strike` at expiry |
| Dip market | "Will ETH dip to $2,200 by 3 PM?" | `spot > strike` at expiry |

`should_exit()` originally used `(strike − spot) / strike × 100` for all NO/DOWN positions
(correct for reach markets). For dip markets this formula is negative whenever the position is
winning (spot above strike), which triggered the stop-loss immediately after entry.

**Fix:** `monitor.py` now inspects `pos.spot_price` (recorded at fill time). If
`pos.spot_price > pos.strike`, the position is treated as a dip-market NO and the delta formula
is flipped: `(spot − strike) / strike × 100`. The fix is applied in both `should_exit()` and
`_write_momentum_tick()` (so `momentum_ticks.csv` also records the correct signed value).

Backward-compatible: `pos.spot_price` defaults to `0.0`; for any saved position loaded with
no spot price, `0.0 ≤ strike` so the reach-market formula is preserved.

### Kelly Sizing

The bot sizes each momentum entry using fractional Kelly:

```
f* = (p × b − (1 − p)) / b      # b = net payout per dollar (1/token_price − 1)
size_usd = (f* × FRACTIONAL_KELLY × bankroll × kelly_multiplier)
           clamped to [MOMENTUM_MIN_ENTRY_USD, MOMENTUM_MAX_ENTRY_USD]
```

Key tuning knobs (all in `config.py`):

| Key | Default | Purpose |
|-----|---------|---------|
| `MOMENTUM_KELLY_FRACTION` | `0.25` | Fractional Kelly (full Kelly is too aggressive for prediction markets) |
| `MOMENTUM_KELLY_MIN_TTE_SECONDS` | `30` | Floor TTE for Kelly math — prevents sigma_tau collapse near expiry inflating `win_prob` to ~1.0 and sizing every entry at MAX |
| `MOMENTUM_KELLY_MULTIPLIER_BY_TYPE` | `{5m:0.45, 15m:0.70, 1h:0.90, 4h+:1.00}` | Per-bucket structural dampener applied after fractional Kelly. Short buckets are noisy; dampening reduces over-sizing near expiry |
| `MOMENTUM_KELLY_PERSISTENCE_ENABLED` | `True` | If True, a signal in-band continuously for ≥ 1 min-TTE window receives a z-score bonus (up to `MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX = 0.5`) — rewards sustained momentum vs. single-tick noise |

**Negative-EV guard:** When `f* < 0` (payout too small to overcome the loss probability),
`_compute_kelly_size_usd` returns `0.0` and `_execute_signal` skips the entry completely.
`MOMENTUM_MIN_ENTRY_USD` does **not** override a negative-EV signal. The raw Kelly fraction
(`kelly_f_raw`) is recorded in both the fills CSV debug dict and structured logs to make
negative-EV skips auditable.

---

## Sanity Checks

### Example 1 - YES trade (spot rising, dynamic vol)

> 1-hour BTC bucket. Strike $68,300. Spot $68,780 (+0.70% above strike).
> YES token: 87c. Deribit ATM IV: 52%. TTE: 45 min = 2700 s.
>
> sigma_tau = 0.52 * sqrt(2700/31536000) = 0.52 * 0.00925 = 0.00481
> y = 1.6449 * 0.00481 * 100 = 0.791%
>
> delta_pct = (68780 - 68300) / 68300 * 100 = +0.70%
>
> 0.70% < 0.791%  →  SKIP. Spot hasn't moved far enough given current vol regime.
>
> (With the old static table y=0.65%, this would have been a trade. Dynamic vol is more conservative.)

### Example 2 - YES trade (spot risen more, same vol)

> 1-hour BTC bucket. Strike $68,300. Spot $68,900 (+0.88% above strike).
> YES token: 87c. Deribit ATM IV: 52%. TTE: 45 min = 2700 s.
> y = 0.791% (same as above).
>
> delta_pct = (68900 - 68300) / 68300 * 100 = +0.879%
> 0.879% > 0.791%  YES
> price band: 0.87 in [0.80, 0.90]  YES
> TTE: 2700s > 120s  YES
> CLOB depth: 450 USDC at ask  YES
>
> **FIRE: Buy YES at 87c.** Fair prob ~95%+. Paying 87c = ~8c of edge.

### Example 3 - NO trade (spot falling)

> 15-min ETH bucket. Strike $3,500. Spot $3,481 (-0.54% below strike).
> NO token: 83c (p_yes = 0.17). Deribit ATM IV (ETH): 68%. TTE: 8 min = 480 s.
>
> sigma_tau = 0.68 * sqrt(480/31536000) = 0.68 * 0.00390 = 0.00265
> y = 1.6449 * 0.00265 * 100 = 0.436%
>
> delta_pct = (3500 - 3481) / 3500 * 100 = +0.543%
> 0.543% > 0.436%  YES
> price band: 0.83 in [0.80, 0.90]  YES
>
> **FIRE: Buy NO at 83c.** Spot is falling, NO wins if price stays below $3,500.

### Example 4 - Rejected (vol regime too high)

> 5-min BTC bucket. Strike $68,300. Spot $68,500 (+0.29% above strike).
> YES token: 84c. Deribit ATM IV: 90% (high-vol day). TTE: 3 min = 180 s.
>
> sigma_tau = 0.90 * sqrt(180/31536000) = 0.90 * 0.00239 = 0.00215
> y = 1.6449 * 0.00215 * 100 = 0.354%
>
> delta_pct = +0.29% < 0.354%  NO
>
> **SKIP.** Even though 84c looks attractive, the high-vol regime correctly raises the bar.
> Dynamic vol protects against entries during chaotic market conditions.

---

## Improvement Set — Items 1, 2, 4, 5, 6, 7

These six improvements were implemented together and are documented here.

---

### Item 1 — Active TP Resting Limit Order

**Motivation:** Previously the monitor polled the CLOB price for take-profit. A resting SELL limit pre-armed at the TP price allows the Polymarket CLOB to fill the exit immediately when the market reaches the target, rather than waiting for the next monitor poll cycle.

**Behaviour:**
1. After entry fill is confirmed, a SELL limit order is placed at `MOMENTUM_TAKE_PROFIT` for the full position size.
2. The order ID is stored in `pos.tp_order_id`.
3. If the position closes for any other reason (SL, near-expiry, RESOLVED), the TP resting order is cancelled via `pm.cancel_order(pos.tp_order_id)`.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `MOMENTUM_TP_RESTING_ENABLED` | `True` | Enable/disable resting TP order |
| `MOMENTUM_TP_RETRY_MAX` | `3` | Max retry attempts if TP order placement fails |
| `MOMENTUM_TP_RETRY_STEP` | `0.005` | Price step down per TP placement retry |

**Event:** `SELL_SUBMIT` emitted when TP order is placed with `order_id`, `tp_price`, and `size`.

---

### Item 2 — Order Cancel-and-Retry Loop

**Motivation:** In live mode, limit entries can remain unfilled for many seconds. Waiting indefinitely wastes time and locks up the `MAX_CONCURRENT` slot. A cancel-and-retry loop re-submits at a higher price (tighter to the ask) up to `MOMENTUM_MAX_RETRIES` times.

**Behaviour (live mode only, paper mode bypasses):**
1. Place BUY limit/market at `current_ask`.
2. Wait `MOMENTUM_ORDER_CANCEL_SEC` seconds for a WS fill event or REST confirmation.
3. On timeout: REST-check the order first (handles race conditions where fill arrived after our check); if genuinely unfilled → cancel and retry at `+MOMENTUM_BUY_RETRY_STEP`.
4. After `MOMENTUM_MAX_RETRIES` exhausted, emit `BUY_FAILED` and return False (position not opened).
5. Hard slippage cap: `token_price × (1 + MOMENTUM_SLIPPAGE_CAP)` — never retry above this.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `MOMENTUM_ORDER_CANCEL_SEC` | `8.0` | Seconds before cancelling an unfilled entry |
| `MOMENTUM_SLIPPAGE_CAP` | `0.05` | Maximum allowed ask-slippage (5%) |
| `MOMENTUM_MAX_RETRIES` | `2` | Retry attempts after first timeout |
| `MOMENTUM_BUY_RETRY_STEP` | `0.01` | Ask price increment per retry |

**Events:** `BUY_SUBMIT` (each attempt), `BUY_CANCEL_TIMEOUT` (timeout), `BUY_FILL` (success), `BUY_FAILED` (all retries exhausted).

---

### Item 4 — VWAP Deviation + Momentum RoC Filter

**Motivation:** The PTB-bot and VWAP-bot both use VWAP deviation and rate-of-change as secondary confirmation — they help avoid entries when price is stale/mean-reverting and confirm that positive momentum is still active.

**Behaviour:**
1. Price ticks are recorded in a per-token `_price_history` deque (up to 600 ticks) whenever `MOMENTUM_MIN_VWAP_DEV_PCT > 0` or `MOMENTUM_MIN_ROC_PCT > 0`.
2. At scan time, `_check_vwap_roc()` computes:
   - **VWAP** = Σ(price × vol_proxy) / Σ(vol_proxy) over `MOMENTUM_VWAP_WINDOW_SEC`
   - **VWAP deviation** = (last_price − VWAP) / VWAP × 100%
   - **RoC** = (last_price − oldest_price) / oldest_price × 100% over `MOMENTUM_ROC_WINDOW_SEC`
3. Signal is skipped if deviation < `MOMENTUM_MIN_VWAP_DEV_PCT` **or** RoC < `MOMENTUM_MIN_ROC_PCT`.
4. Insufficient history (< 3 ticks) passes permissively.

**Volume proxy:** Since Polymarket WS does not expose per-trade volumes, `ask_size` at the best ask is used as a proxy. This is the same approach used in the VWAP-bot reference.

**Default values (0.0 = disabled/permissive):** Both thresholds default to 0 so the filter has no impact until the operator tunes them from live data.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `MOMENTUM_VWAP_WINDOW_SEC` | `30` | Rolling window for VWAP computation (seconds) |
| `MOMENTUM_ROC_WINDOW_SEC` | `60` | Rolling window for RoC computation (seconds) |
| `MOMENTUM_MIN_VWAP_DEV_PCT` | `0.0` | Minimum VWAP deviation required (0 = disabled) |
| `MOMENTUM_MIN_ROC_PCT` | `0.0` | Minimum RoC required (0 = disabled) |

**Diagnostics:** `skipped_vwap` counter added to scan summary; debug dict includes `vwap`, `vwap_dev_pct`, `vwap_samples`, `roc_pct`.

---

### Item 5 — Tightened Chainlink Silence Watchdog

**Motivation:** The previous 120-second silence timeout is too long — a Chainlink feed pause of 30+ seconds is already unsafe for oracle-delta SL accuracy.

**Behaviour:** `_WS_SILENCE_TIMEOUT_S` in `chainlink_ws_client.py` now reads `config.CHAINLINK_SILENCE_WATCHDOG_SECS` (default 30 s) instead of the hardcoded 120 s. If no AnswerUpdated event is received within this window, the WS client triggers reconnection.

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `CHAINLINK_SILENCE_WATCHDOG_SECS` | `30` | Seconds of silence before WS reconnect |

---

### Item 6 — Event-Typed Entry/Exit Journal

**Motivation:** Unstructured log scraping makes trade lifecycle analysis difficult. A structured JSONL event log provides first-class support for PnL accounting, retry analysis, and (in future) streaming dashboards.

**File:** `data/momentum_events.jsonl` — one JSON record per line.

**Schema (schema_version=1):**
```json
{
  "schema_version": 1,
  "ts": "<ISO UTC>",
  "event": "<EVENT_TYPE>",
  "market_id": "...",
  ...event-specific fields...
}
```

**Event types:**
| Event | Emitted by | Description |
|---|---|---|
| `SESSION_START` | `scanner.start()` | Once per bot startup |
| `BUY_SUBMIT` | `_execute_signal` | Each entry order submission |
| `BUY_CANCEL_TIMEOUT` | `_execute_signal` | Unfilled entry cancelled after timeout |
| `BUY_FILL` | `_execute_signal` | Entry fill confirmed (WS or REST) |
| `BUY_FAILED` | `_execute_signal` | All retries exhausted, no position opened |
| `SELL_SUBMIT` | `_execute_signal` | TP resting SELL limit pre-armed |
| `SELL_CLOSE` | `monitor._exit_position` | Position successfully closed |
| `SELL_FAILED` | `monitor._exit_position` | Exit order placement failed |

**API:** `GET /momentum/events?n=200` returns the last N events newest-first.

**Module:** `strategies/Momentum/event_log.py` exports `emit(**kwargs)` and `read_recent(n)`.

---

### Item 7 — Probability-Based Stop-Loss

**Motivation:** The oracle-delta SL only fires when the spot price diverges from the strike. For short-duration markets (5m, 15m), the CLOB token price itself may collapse well before the spot delta crosses the threshold. A secondary SL based on token price captures this.

**Behaviour:**
1. At entry, `pos.prob_sl_threshold` is set to `entry_price × (1 − MOMENTUM_PROB_SL_PCT)`.
2. In `should_exit()`, if `token_price < pos.prob_sl_threshold` and `MOMENTUM_PROB_SL_ENABLED`, exit with `ExitReason.MOMENTUM_STOP_LOSS`.
3. The check fires on the actual held-token CLOB price (same as TP check), so it applies to both YES and NO positions correctly.
4. Setting `prob_sl_threshold = 0.0` disables the check for a position (e.g., when `MOMENTUM_PROB_SL_ENABLED = False` at entry time).

**Config:**
| Parameter | Default | Description |
|---|---|---|
| `MOMENTUM_PROB_SL_ENABLED` | `True` | Enable probability-based CLOB SL |
| `MOMENTUM_PROB_SL_PCT` | `0.15` | Drop from entry price that triggers SL (15%) |

**Interaction with delta SL:** Both the oracle-delta SL and the prob-SL are independent failsafes. Either can fire first. The prob-SL is faster for bucket markets where spot doesn't move much but CLOB prices collapse.

