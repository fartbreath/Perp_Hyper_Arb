# Perp_Hyper_Arb Bot — Preamble (read before every prompt)

## API Sources of Truth

- **PM Gamma API** is the source of truth for ALL finalized market data — resolved outcomes,
  price-to-beat, market metadata. Never use any other source for these. Accounting errors and
  real financial losses result from using anything else.
- **CLOB API `winner` flag** (`GET /markets/{condition_id}`, field `tokens[].winner`) is the
  source of truth for whether a specific token won or lost. Never infer WIN/LOSS from
  `curPrice`, `price`, or any wallet positions API field — these are stale mid-prices that can
  show ~1.0 for a losing token in the brief window right after settlement.
- **`curPrice` from the PM wallet positions API is NOT authoritative for outcome.** It is a
  stale CLOB mid-price.
- **`redeemable=True` does not mean the token won.** It means the position can be finalised
  on-chain. A losing token is also redeemable — it just returns $0. Always determine
  WIN/LOSS from the CLOB winner flag, not from redeemability.
- Always read the Polymarket API specs. Do not make assumptions about how endpoints behave.
  Ask for clarification if unsure.

## Market Structure

- Up/Down markets use **Chainlink** as the settlement oracle, not Hyperliquid spot or Pyth.
  The "final price" shown in the PM UI may differ from the Chainlink oracle price used for
  settlement. The CLOB winner flag is always correct; UI display values are not authoritative.
- For range markets, positions have `range_lo` and `range_hi` set by the scanner.
  `strike` is the midpoint. Delta SL and tick delta must use the range bounds, not the strike.
- YES and NO tokens are independent and have separate order books. Do not assume they are
  perfectly inversely correlated.

## Stop-Loss Logic

- **Prob-SL** (CLOB price below `prob_sl_threshold`) fires when the CLOB token price collapses.
  Near expiry, book drain causes the CLOB to collapse even for winning positions — this is NOT
  a real move. The oracle delta gate suppresses prob-SL when oracle delta > 1% ITM (position
  solidly in-the-money). Never fire prob-SL solely on CLOB price when the oracle confirms ITM.
- **Delta SL** for range positions must compute delta against `range_lo`/`range_hi` bounds, not
  the strike midpoint.
- Range positions must receive `current_spot` for delta SL to evaluate. The spot fetch guard
  must include `"range"` strategy, not just `"momentum"`.

## Exit Order Handling

- FAK (Fill-or-Kill / market sell) orders are frequently rejected near expiry when the book is
  empty. Always implement retries (≥5) with meaningful sleep (≥0.5s). After exhausting FAK
  retries, fall back to a GTC limit order at a floor price rather than leaving the position
  open indefinitely.
- An `EXIT_ORDER_FAILED` log means the position was NOT exited. It requires manual
  intervention. Never silently swallow this case.

## GTD Hedge Accounting

- GTD hedge cancel must only fire on WIN exits (`PROFIT_TARGET`, `MOMENTUM_TAKE_PROFIT`).
  On loss exits (`STOP_LOSS`, `NEAR_EXPIRY`, etc.) the hedge must be kept alive — it may
  recover value and partially offset the loss.
- When a hedge token settles, `record_hedge_fill` must be called with the correct
  `settled_price` from the CLOB winner flag, not from `curPrice`.

## WebSocket Fill Detection

- The PM user WebSocket emits both `"MATCHED"` and `"FILLED"` status values (case may vary).
  Also handles a nested `{"event_type": "order", "order": {...}}` format. Status checks must be
  case-insensitive and handle both formats.

## YES / NO / UP / DOWN Token Logic (Read This Every Time)

This is the most common source of circular bugs. Commit this to memory.

### Token sides and what they mean

| Position side | Token held | Wins when |
|---|---|---|
| `YES` / `UP` / `BUY_YES` | YES/Up token (`token_id_yes`) | Oracle price ≥ strike at expiry |
| `NO` / `DOWN` | NO/Down token (`token_id_no`) | Oracle price < strike at expiry |

- **`token_id_yes` and `token_id_no` are different tokens with independent order books.**
  Never derive one from the other. A NO position buys and holds the NO token (`token_id_no`),
  and sells the NO token on exit — not the YES token.
- **Sides in code:** `pos.side in ("YES", "BUY_YES", "UP")` is the YES/winning-up group.
  Everything else (`"NO"`, `"DOWN"`) is the NO/winning-down group.

### `resolved_yes_price` — what it means

The CLOB API always expresses resolution as the YES-token price:
- `resolved_yes_price = 1.0` → YES/Up token won (spot ended ≥ strike)
- `resolved_yes_price = 0.0` → NO/Down token won (spot ended < strike)

**Converting to WIN/LOSS for the position we hold:**
```
YES / UP  position → WIN if resolved_yes_price == 1.0
NO  / DOWN position → WIN if resolved_yes_price == 0.0   (i.e. YES lost)
```
In code: `settlement = resolved_yes_price if is_yes_side else (1.0 - resolved_yes_price)`
then `WIN if settlement >= 0.5`.

**Do NOT** flip this. Do NOT assume that because the NO token settled to 1.0 
the `resolved_yes_price` is 1.0 — it is 0.0. `resolved_yes_price` is always the 
YES-token price, regardless of which token we held.

### Delta direction (oracle SL + tick delta)

"Delta" = how far spot has moved in the WINNING direction from the strike.

| Position side | Market type | Winning delta formula |
|---|---|---|
| YES / UP | reach ("will it reach X?") | `(spot − strike) / strike × 100` (positive = winning) |
| NO / DOWN | reach market | `(strike − spot) / strike × 100` (positive = winning) |
| NO / DOWN | dip market (`pos.spot_price > strike` at entry) | `(spot − strike) / strike × 100` (positive = winning) |

Delta SL fires when `current_delta < MOMENTUM_DELTA_STOP_LOSS_PCT`.
A **positive** delta means on-side (winning). A **negative** delta means off-side (losing).
The SL fires when delta retreats below threshold — i.e., spot has moved toward the strike.

**For range positions** delta is distance to nearest bound, not to the strike midpoint.
- YES range: `min(spot − range_lo, range_hi − spot) / mid × 100` (positive = inside range)
- NO range: distance above `range_hi` or below `range_lo` (positive = outside = winning)

### GTD hedge token side

The hedge is placed on the **opposite** side of the main position:
- Main = YES/UP → hedge buys NO/Down token (`token_id_no`)
- Main = NO/DOWN → hedge buys YES/Up token (`token_id_yes`)

The hedge wins when the main position loses. The hedge's `resolved_yes_price` to
WIN/LOSS mapping is therefore inverted relative to the main position.
`record_hedge_fill` computes P&L from the hedge token's own `settled_price` (0 or 1),
not from the main position's outcome.

### `pm_resolved_yes` in patch_trade_outcome

`patch_trade_outcome(condition_id, pm_resolved_yes)` always expects the **YES-token price**.
Callers must pass the value returned by `fetch_market_resolution()` directly — do NOT
invert it based on the side of the position we held. The function internally converts
per-row based on each record's `side` column.

## Momentum Strategy — Core Rules

1. **Enter near expiry** (last ~60s of the bucket). Low TTE = tiny remaining vol =
   little room for spot to reverse. This is the source of edge.

2. **Hold YES/Up token** when `spot > strike` at entry.
   **Hold NO/Down token** when `spot < strike` at entry.

3. **Stop-loss is oracle-driven, not CLOB-driven.** The delta SL
   (`MOMENTUM_DELTA_STOP_LOSS_PCT`) fires when the Chainlink oracle spot retreats
   within threshold of the strike. The CLOB token price is used only for take-profit
   (`token → 0.999`). Do not use CLOB price drops alone as the primary SL signal —
   CLOB reprices forward and can collapse on book drain while the position is winning.

4. **Oracle feeds must be event-driven WebSocket streams, not HTTP polling.**
   Polling is not institutional grade — it introduces latency jitter, misses intra-candle
   moves, and wastes rate-limit budget. All spot price feeds must push ticks via WS.
   If a data source does not offer a WS feed, use the lowest-latency streaming alternative
   available. Never introduce polling as a "simpler" solution — the edge at near-expiry
   depends on reacting to oracle moves within milliseconds, not seconds.
   Current routing:
   - `bucket_5m`, `bucket_15m`, `bucket_4h` → Chainlink WS tick stream
   - `bucket_1h`, `bucket_daily`, `bucket_weekly` → RTDS WS exchange-aggregated

5. **Settlement oracle is Chainlink**, not Hyperliquid spot, not Pyth, not PM UI price.
   Always use `fetch_market_resolution()` for final outcome, not any spot feed value.