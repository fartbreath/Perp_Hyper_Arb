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

## Configuration

All parameters are set in `config.py` under the `MOMENTUM_*` namespace and can be hot-patched
via `config_overrides.json` without restarting the bot.

| Config Key | Default | Description |
|-----------|---------|-------------|
| `STRATEGY_MOMENTUM_ENABLED` | `False` | Master on/off for the scanner |
| `MOMENTUM_MAX_ENTRY_USD` | `50.0` | Maximum USDC deployed per position |
| `MOMENTUM_MIN_CLOB_DEPTH` | `200.0` | Minimum USDC depth on the ask side within 1c of best ask (thin-book guard) |
| `MOMENTUM_ORDER_TYPE` | `"limit"` | `"limit"` = taker limit at ask+0.5c; `"market"` = market order |
| `MOMENTUM_STOP_LOSS_YES` | `0.55` | Exit YES position if p_yes drops below this |
| `MOMENTUM_STOP_LOSS_NO` | `0.55` | Exit NO position if p_no drops below this |
| `MOMENTUM_TAKE_PROFIT` | `0.96` | Exit if held token rises above this |
| `MOMENTUM_MIN_TTE_SECONDS` | see below | Per-bucket-type dict of entry-window ceilings (seconds to expiry); markets with more TTE are outside the entry window and skipped |
| `MOMENTUM_MIN_TTE_SECONDS_DEFAULT` | `120` | Fallback TTE ceiling for any market type not listed in the dict |
| `MOMENTUM_PRICE_BAND_LOW` | `0.80` | Lower bound of the signal price band |
| `MOMENTUM_PRICE_BAND_HIGH` | `0.90` | Upper bound of the signal price band |
| `MOMENTUM_SCAN_INTERVAL` | `10` | Seconds between full scan passes (also max wake latency; event-driven entry can wake sooner) |
| `MOMENTUM_SPOT_MAX_AGE_SECS` | `30` | Discard if HL BBO is older than this (stale spot guard) |
| `MOMENTUM_BOOK_MAX_AGE_SECS` | `60` | Discard if PM order book is older than this (stale book guard) |
| `MOMENTUM_VOL_CACHE_TTL` | `300` | Seconds to cache Deribit ATM IV before re-fetching |
| `MOMENTUM_VOL_Z_SCORE` | `1.6449` | Global z-score for the probability threshold (1.6449 ≈ 95th percentile) |
| `MOMENTUM_VOL_Z_SCORE_BY_TYPE` | `{}` | Per-bucket-type z-score overrides; unlisted types use the global default. Example: `{"bucket_daily": 1.0, "bucket_15m": 1.3}` |
| `MOMENTUM_MAX_CONCURRENT` | `3` | Maximum simultaneous momentum positions |
| `MOMENTUM_MARKET_COOLDOWN_SECONDS` | `300` | Seconds to suppress re-entry after any open/close/failed attempt in a market (deduplication guard) |
| `MOMENTUM_MAX_TTE_DAYS` | `7` | Days of bucket markets subscribed for WS book data (independent of maker window) |
| `MOMENTUM_PRESUB_LOOKAHEAD` | `4` | Also subscribe to the next N not-yet-started bucket periods for pre-window data collection |

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
| 1 | **Deribit ATM IV** (nearest ~7-day call option, ATM strike ± spot) | BTC, ETH, SOL | `MOMENTUM_VOL_CACHE_TTL` (5 min) |
| 2 | **HL rolling realized vol** (log-return std from last 24h of HL BBO feed) | Any coin in `HL_PERP_COINS` | 60 s |
| 3 | **Skip signal** | Coins with no data | — |

**Why Deribit ATM IV as primary?**

Options markets price in *expected* future volatility, not just past realized vol. ATM IV on the 
nearest weekly option already reflects the regime (low vol = small threshold, high vol = larger 
threshold). This automatically widens the entry gate during volatile conditions and tightens it 
in quiet markets — exactly the adaptive behavior we need.

**Why HL rolling vol as fallback?**

For assets without Deribit options (HYPE, DOGE), the HL BBO feed provides tick-level price 
history. Rolling 24h log-return standard deviation scaled to the relevant time horizon gives a 
reasonable realized-vol estimate. The `VolFetcher` maintains a circular buffer (max 2,000 
samples) per coin and recomputes on demand.

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
| **Stale HL spot** | `time.time() - bbo.timestamp > MOMENTUM_SPOT_MAX_AGE_SECS` | 30 s | Delta computation relies on fresh spot |
| **Stale vol** | `MOMENTUM_VOL_CACHE_TTL` exceeded and no fresh Deribit response | 300 s | Threshold computation unreliable with old IV |
| **Missing spot** | `bbo is None or bbo.mid is None` | — | No spot = no delta computation |
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
    p_no  = 1.0 - p_yes

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
    bbo = hl.get_bbo(M.underlying)
    if bbo is None or bbo.mid is None: continue
    if now_ts - bbo.timestamp > MOMENTUM_SPOT_MAX_AGE_SECS: continue
    spot = bbo.mid

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
    if delta_pct < y: continue

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

Three exit conditions, whichever triggers first. Exits are based on the **held token's price**,
not USD P&L (unlike the mispricing strategy):

| Condition | Config Key | Default | Action | Rationale |
|-----------|-----------|---------|--------|-----------|
| YES position: p_yes drops ≤ stop-loss | `MOMENTUM_STOP_LOSS_YES` | 0.55 | Market-sell YES token | Cap loss at ~30c on an 85c entry; tail reversal confirmed |
| NO position: p_no drops ≤ stop-loss | `MOMENTUM_STOP_LOSS_NO` | 0.55 | Market-sell NO token | Symmetric stop on the NO side |
| Token price >= take-profit | `MOMENTUM_TAKE_PROFIT` | 0.96 | Market-sell held token | Lock in 6-16c gain; last 4c has poor risk/reward |
| Expiry | — | — | Hold to resolution | If neither trigger fires, let the market resolve on-chain |

**Token price mapping (monitor):**

- YES position: token_price = current YES mid
- NO position: token_price = 1 - current YES mid

> Exits are handled by `monitor.py` in `should_exit()` under `pos.strategy == "momentum"`.
> No time-stop is applied to momentum positions — the min-TTE gate at entry means all
> positions have at least 2 minutes of life; letting them resolve is always the right default.

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
| Spot reverses immediately after entry | Side-specific stop-loss (`MOMENTUM_STOP_LOSS_YES` / `_NO`) | Max loss ~30c on ~85c entry |
| Market already at 94c when scanner runs | Upper band check (< 0.90 strictly) | Signal filtered out |
| Signal fires outside entry window | Per-bucket TTE gate (`MOMENTUM_MIN_TTE_SECONDS`) | Signal filtered out |
| HL spot data is stale (> 30s old) | Stale spot guard | Signal skipped |
| PM book data is stale (> 60s old) | Stale book guard | Signal skipped |
| PM book has no levels (MMs withdrawn) | Empty book guard | Signal skipped |
| Deribit IV fails to fetch | Fallback to HL rolling vol; if also absent, skip | Signal skipped; logs at DEBUG |
| HL rolling vol has < 20 samples | Vol fetcher returns None | Signal skipped |
| Thin liquidity at ask | CLOB depth gate (> 200 USDC) | Signal skipped |
| Price moved out of band before execution | Price re-check at execution time (band + 2c tolerance) | Order not placed |
| Same market already held by any strategy | Duplicate guard + cooldown | Skip entry |
| Bot restarted mid-window for Up/Down market | `data/market_open_spots.json` persists open-spot cache | Correct strike restored from disk |
| HL WS feed silently stops delivering | HL outage detection: log warning if >50% markets skipped for stale spot | Operator alerted to investigate |
| Deribit IV cached from high-vol period | Cache TTL 5 min; stale vol widens threshold | Conservative; may miss some trades |
| Vol regime spikes 3x (black swan) | z-score scales threshold with vol | Naturally filters out noisy markets |
| Concurrent position cap hit | `MOMENTUM_MAX_CONCURRENT` gate | Signal dropped until slot opens |
| Per-bucket vol bar too strict | `MOMENTUM_VOL_Z_SCORE_BY_TYPE` override for that type | Tune per-type without affecting others |

---

## Implementation Architecture

```
strategies/Momentum/
├── __init__.py          # module init
├── signal.py            # MomentumSignal dataclass + edge_pct property
├── vol_fetcher.py       # VolFetcher: Deribit ATM IV + HL rolling realized vol + prefetch task
└── scanner.py           # MomentumScanner: event-driven scan + direct execution + diagnostics
```

Persisted state:
```
data/market_open_spots.json   # window-open spot cache for Up/Down markets (survives restart)
```

### Data flow

```
HLClient (BBO feed) ──→ VolFetcher._on_bbo() ──→ rolling mid buffer
                                                    ↓ (fallback)
DeribitFetcher ──────────────────────────────→ VolFetcher.get_sigma_ann()
                                                    ↓
VolFetcher.start_prefetch() ──────────────→ background warm-up task

PMClient (WS price ticks) ──→ MomentumScanner._on_price_update_entry()
                                            ↓ (band hit? wake immediately)
                                     asyncio.Event.set()
                                            ↓
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
                                 PositionMonitor (exit checks every 30s)
                                        ↓
                                 should_exit() — MOMENTUM_STOP_LOSS_YES/_NO / TAKE_PROFIT

MomentumScanner._last_scan_diags ──→ GET /momentum/diagnostics
MomentumScanner._on_signal cb ────→ GET /momentum/signals
```

### Key design decisions

1. **No agent loop** — momentum is time-critical; scanner executes directly without Ollama evaluation.
2. **Event-driven entry** — PMClient price-change callbacks wake the scan loop immediately when a market enters the band, instead of waiting the full `MOMENTUM_SCAN_INTERVAL`. The lightweight callback does only a band + cooldown pre-filter; the full gate evaluation runs in the normal scan path.
3. **No queue** — direct execution prevents stale signals accumulating.
4. **Re-check at execution** — price is re-validated right before order placement to guard against fast moves between detection and execution.
5. **WS fill detection** — a one-shot `asyncio.Future` is registered before awaiting anything; the MATCHED WS event resolves it (~0 ms, zero REST calls). REST fallback fires only on timeout (5 s).
6. **YES-space entry price** — NO token fill prices are converted to YES-space (`1.0 - fill_price`) before `risk.open_position()`, keeping P&L arithmetic consistent.
7. **Shared risk engine** — `risk.open_position()` and `risk.get_positions()` are shared with maker/mispricing, ensuring cap enforcement is global.
8. **Shared monitor** — `PositionMonitor` handles momentum exits via `pos.strategy == "momentum"` branch in `should_exit()`. No separate monitor loop needed.
9. **`_blocked_by_tte` pattern** — the TTE gate does not short-circuit the pipeline; vol and delta are computed for all in-band markets regardless, so the diagnostics CSV contains full price-vs-TTE empirical data even for markets outside the entry window.

---

## Implementation Checklist

### MVP (required before any live capital)

- [x] `strategies/Momentum/signal.py` — `MomentumSignal` dataclass + `edge_pct` property
- [x] `strategies/Momentum/vol_fetcher.py` — `VolFetcher` with Deribit + HL fallback + `start_prefetch()`
- [x] `strategies/Momentum/scanner.py` — scanner loop + event-driven entry + direct execution + diagnostics
- [x] Config keys: all `MOMENTUM_*` in `config.py` (including per-bucket TTE dict, z-score overrides, cooldown, horizon)
- [x] `monitor.py` — `ExitReason.MOMENTUM_STOP_LOSS/TAKE_PROFIT` + side-specific stops in `should_exit()`
- [x] `main.py` — init `MomentumScanner`, start as asyncio task
- [x] Event-driven entry via `pm.on_price_change()` callback
- [x] Per-bucket `MOMENTUM_MIN_TTE_SECONDS` dict with per-type entry windows
- [x] Per-bucket `MOMENTUM_VOL_Z_SCORE_BY_TYPE` overrides
- [x] "Up or Down" directional market support (implicit strike persisted to disk)
- [x] HL outage detection (>50% stale-spot warning)
- [x] `/momentum/diagnostics` API endpoint backed by `_last_scan_diags`
- [x] WS subscription refresh every 5 minutes (new bucket markets picked up automatically)
- [ ] Unit tests for `vol_fetcher.py` (mock Deribit + HL)
- [ ] Unit tests for `scanner.py` stale signal guards and Up/Down market handling
- [ ] Unit tests for `monitor.py` momentum exits (side-specific stop-loss)

### Pre-launch (required before going live)

- [ ] **Backtest** against historical PM + HL data in `/Backtest` environment
- [ ] Validate actual win rate vs theoretical 93-97%
- [ ] Validate `MOMENTUM_VOL_Z_SCORE` against real PM microstructure
- [ ] Paper-trade for >= 48h before live capital; monitor stop-loss frequency

### v2 (deferred)

- [ ] Kelly sizing based on edge and bankroll
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

### HL Outage Detection

If more than 50% of all bucket markets are skipped for stale spot in a single scan pass, the
scanner logs a `WARNING: possible HL WS outage`. This distinguishes a genuine feed outage from
isolated stale-spot misses and alerts operators to investigate quickly.

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
