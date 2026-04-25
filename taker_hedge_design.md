# Taker Hedge Design — 5m/15m Buckets
**Date:** 2026-04-23 | **Status:** Pre-implementation design spec

---

## Summary

Replace the GTD resting hedge with a simultaneous taker for 5m and 15m bucket positions.
All 3 loss trades in the April 23 session had $0 hedge recovery from the GTD. A same-size taker
hedge at entry would have capped those losses to ~$0.10–$0.38 (spread cost only).
Keep GTD for 1h, 4h, daily, weekly buckets — adequate TTE for reversal to develop.

---

## 1. Problem (from actual data)

| Trade | Market | Main PnL | GTD hedge result | Root cause |
|-------|--------|----------|-----------------|------------|
| SOL UP 01:34 (band_floor_abort) | bucket_5m | -$10.80 | $0 — TTE < 5s guard fired, hedge skipped | Hedge guard blocks near-expiry entries |
| SOL DOWN 01:53 | bucket_5m | -$10.75 | $0 — GTD bid $0.05, UP tokens rising to $1.00, never filled | GTD only fills on reversal, not adverse trend |
| ETH DOWN 01:53 | bucket_5m | -$10.22 | $0 — same: ETH went UP, DOWN settled $0, UP rising | Same structural failure |

**Win trades:** GTD placed, cancelled on exit → $0 cost. But 3 losses: $0 recovery.

---

## 2. The Binary Market Math

In a binary PM market, YES and NO are complementary. At settlement:

$$\text{YES settles } \$1.00 + \text{NO settles } \$0.00 \quad\text{or}\quad \text{YES settles } \$0.00 + \text{NO settles } \$1.00$$

If you buy both legs simultaneously with the same number of contracts:

$$\text{PnL} = (\text{size} \times \$1.00) - (\text{size} \times \text{main\_ask}) - (\text{size} \times \text{opp\_ask})$$

$$= \text{size} \times (1.00 - \text{main\_ask} - \text{opp\_ask})$$

**This PnL is the same regardless of which side wins.** You've guaranteed the outcome.

- If `main_ask + opp_ask < 1.00`: guaranteed profit (arb)
- If `main_ask + opp_ask = 1.00`: break even both ways
- If `main_ask + opp_ask > 1.00`: small guaranteed loss (the spread cost — the price of certainty)

In practice, PM markets have bid-ask spreads, so `main_ask + opp_ask` is typically $1.01–$1.04.
The spread cost is the cost of eliminating variance.

---

## 3. Per-Trade Math on the 3 Losses

Actual entry prices from `trades.csv`. Opp ask estimated from implied mid + typical spread.

### Loss 1: SOL UP band_floor_abort

| | Value |
|--|--|
| Main entry price | $0.5275 (UP token) |
| Main size | 21.22 contracts |
| Main notional | $11.20 |
| Opp (DOWN) implied mid | $0.4725 |
| Opp ask estimate | ~$0.49 |
| Sum of asks | $0.5275 + $0.49 = **$1.0175** |
| Spread cost per pair | $0.0175 |
| **Hedged PnL** | 21.22 × (−$0.0175) = **−$0.37** |
| **Unhedged PnL** | **−$10.80** |
| **Saving** | **$10.43** |

*Special case: TTE < 5s guard would have blocked a post-fill hedge. Simultaneous taker fires at order time (same `asyncio.gather` as main), before TTE deteriorates.*

### Loss 2: SOL DOWN (high entry price — cheapest hedge)

| | Value |
|--|--|
| Main entry price | $0.8593 (DOWN token) |
| Main size | 12.65 contracts |
| Main notional | $10.87 |
| Opp (UP) implied mid | $0.1407 |
| Opp ask estimate | ~$0.15 |
| Sum of asks | $0.8593 + $0.15 = **$1.009** |
| Spread cost per pair | $0.009 |
| **Hedged PnL** | 12.65 × (−$0.009) = **−$0.11** |
| **Unhedged PnL** | **−$10.75** |
| **Saving** | **$10.64** |

### Loss 3: ETH DOWN (lower entry price — more expensive hedge)

| | Value |
|--|--|
| Main entry price | $0.6766 (DOWN token) |
| Main size | 15.48 contracts |
| Main notional | $10.48 |
| Opp (UP) implied mid | $0.3234 |
| Opp ask estimate | ~$0.35 |
| Sum of asks | $0.6766 + $0.35 = **$1.027** |
| Spread cost per pair | $0.027 |
| **Hedged PnL** | 15.48 × (−$0.027) = **−$0.42** |
| **Unhedged PnL** | **−$10.22** |
| **Saving** | **$9.80** |

---

## 4. The Effect on Wins

The hedge costs the same spread on win trades too. Illustrated with two real wins:

### BTC DOWN 01:35 (win at same price as SOL DOWN loss)

- Entry: $0.8593, size 19.65 contracts, unhedged PnL: **+$2.73**
- Opp ask ~$0.15, sum $1.009, spread cost = 19.65 × $0.009 = **$0.18**
- Hedged PnL: **−$0.18** (win of $2.73 becomes $0.18 loss)

### BTC UP 02:14 (win at lower price)

- Entry: $0.6766, size 16.11 contracts, unhedged PnL: **+$5.08**
- Opp ask ~$0.35, sum $1.027, spread cost = 16.11 × $0.027 = **$0.43**
- Hedged PnL: **−$0.43** (win of $5.08 becomes $0.43 loss)

### Session comparison

| Trades | Unhedged | Fully hedged (estimated) |
|--------|----------|--------------------------|
| 10 wins | +$31.44 | ~−$2.00 (all become small spread losses) |
| 3 losses | −$31.77 | ~−$0.90 (all become small spread losses) |
| **Net** | **−$0.33** | **~−$2.90** |

**Full hedge is worse in expected value.** You're paying spreads on 13 trades to eliminate variance.
The strategy is not "hedge everything" — it's **selective hedging based on entry price**.

---

## 5. Entry Price Gates Hedge Cost

The spread cost depends entirely on how expensive the opp token is:

| Main entry price | Opp implied mid | Opp ask estimate | Spread cost per pair | Spread % of $1 notional |
|-----------------|----------------|-----------------|----------------------|--------------------------|
| $0.92 | $0.08 | ~$0.09 | $0.010 | 1.0% |
| $0.86 | $0.14 | ~$0.15 | $0.010 | 1.0% |
| $0.82 | $0.18 | ~$0.20 | $0.020 | 2.0% |
| $0.78 | $0.22 | ~$0.24 | $0.020 | 2.0% |
| $0.70 | $0.30 | ~$0.33 | $0.030 | 3.0% |
| $0.65 | $0.35 | ~$0.38 | $0.030 | 3.0% |

At high entry price ($0.85+), the hedge costs ~1% per dollar notional. At lower entry prices ($0.65-$0.70), it costs 3%+. **Gate the hedge on the actual live opp ask price** — only place the taker if opp ask ≤ threshold.

The entry price is also a proxy for signal confidence: higher price = market has moved further = stronger signal. Gating hedge on max opp ask automatically selects the highest-confidence entries.

---

## 6. Recommended Configuration

```python
# ── Taker hedge (5m / 15m buckets only) ──────────────────────────────────────
MOMENTUM_HEDGE_TAKER_ENABLED: bool = False  # set True to activate
MOMENTUM_HEDGE_TAKER_PCT: float = 1.0       # hedge size as fraction of main position
MOMENTUM_HEDGE_TAKER_MAX_PRICE: float = 0.20  # skip taker if live opp ask > this
                                               # prevents expensive hedges at low-confidence entries
                                               # $0.20 opp ask → main ≥ ~$0.80 implied
```

`TAKER_MAX_PRICE = 0.20` means:
- At main $0.86: opp implied $0.14, ask ~$0.15 → hedge fires ✅ (spread ~$0.09/contract)
- At main $0.78: opp implied $0.22, ask ~$0.24 → hedge fires ✅ (spread ~$0.02/contract)
- At main $0.68: opp implied $0.32, ask ~$0.34 → hedge **skipped** ❌ (ask > $0.20)

Applying this gate to the April 23 losses:
- Loss 2 (SOL DOWN $0.859): opp ask ~$0.15 ≤ $0.20 → **hedged → saves $10.64**
- Loss 1 (SOL UP band_floor_abort $0.528): opp ask ~$0.49 > $0.20 → **skipped** (opp too expensive)
- Loss 3 (ETH DOWN $0.677): opp ask ~$0.35 > $0.20 → **skipped** (opp too expensive)

The $0.20 cap saves the full SOL DOWN loss and avoids paying spread on low-confidence entries.
At the cost: some wins at price $0.78–$0.82 have their hedge fire and pay ~$0.20–$0.30 drag.

The cap is a tunable parameter. Start at $0.20 and adjust based on observed opp ask distribution.

---

## 7. Arb Detection (free addition)

Check opp ask before placing main order. If `main_price + opp_ask < 1.0`, this is a partial arb:
both sides are underpriced, guaranteed profit regardless of outcome. Size to `MAX_ENTRY_USD`.

```python
# In execute_signal(), before main order, using cached opp book:
if opp_ask and (signal.token_price + opp_ask) < 1.0:
    size_usd = config.MOMENTUM_MAX_ENTRY_USD  # arb: size to max
```

The band_floor_abort scenario often pushes the main token below fair value → sum < $1.00.

---

## 8. What Needs to Be Validated First

Before setting `MOMENTUM_HEDGE_TAKER_ENABLED = True`, confirm:

1. **Actual opp ask at entry time is logged.** The estimates in Section 3 are based on implied mid + spread assumptions. Run a session with taker hedge in paper mode and log `_hedge_taker_ask` alongside each fill. If actual opp ask at Loss 2 was $0.18 instead of $0.15, the spread cost was $0.43 instead of $0.11 — still far better than -$10.75 but the number should be real.

2. **Opp ask distribution by entry price bracket.** From `momentum_ticks.csv`, for all ticks where a signal was fired, what was the opp book ask? Bin by main entry price to validate the table in Section 5.

3. **Taker fill rate near expiry.** Does a FAK order on opp tokens fill at TTE < 30s? The main entry fires as a taker and fills, so there is liquidity. But opp book depth may be thinner. Log `_hedge_taker_fill_rate` per bucket.

---

## 9. Bucket Routing

| Bucket | Hedge type | Reason |
|--------|-----------|--------|
| **5m** | **Taker (new)** | GTD unfill rate on losses = 100% (confirmed). TTE too short for reversal to develop. |
| **15m** | **Taker (new)** | Same structural issue. GTD unfill rate expected high based on session data. |
| 1h | GTD (keep) | Adequate TTE; reversal can develop; taker spread on long hold is expensive. |
| 4h | GTD (keep) | Same. |
| Daily, Weekly | GTD (keep) | Long-duration markets regularly reverse; resting bid has time to find value. |

Taker and GTD can run simultaneously for 5m/15m if desired — taker provides baseline floor
protection, GTD provides larger payout if the opp token genuinely reverses to near zero.

---

## 10. What This Does NOT Change

- `MOMENTUM_HEDGE_AGGRESSIVE_TTE_S` remains at 0 (taker hedge fires from `asyncio.gather` at order placement time, before TTE deteriorates — the TTE guard is irrelevant for taker)
- Entry logic unchanged — near-expiry entries remain the edge source
- Kelly sizing unchanged — arb detection override is a separate gate
- GTD hedge for 1h+ unchanged — fully operational
