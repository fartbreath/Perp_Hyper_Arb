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
  stale CLOB mid-price. It caused a real loss on 2026-04-16: SOL DOWN curPrice showed ~1.0
  after settlement but the token paid $0. Always call `fetch_market_resolution(condition_id)`
  for WIN/LOSS; never use `curPrice >= 0.99` as a signal.
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

## Testing Discipline

- Before diagnosing a test failure as a bug in your fix, always verify it was passing at HEAD
  (`git stash`, run test, `git stash pop`). Pre-existing regressions in other working-tree
  files are common and must not be confused with regressions introduced by the current fix.
- Oracle gate thresholds in tests often set `MOMENTUM_DELTA_STOP_LOSS_PCT = -999` to disable
  the oracle SL. A gate that uses `MOMENTUM_DELTA_STOP_LOSS_PCT * N` as its threshold will
  be broken by this pattern. Use a fixed absolute threshold (e.g., `1.0%`) instead.
- Tests for the auto-redeem paths must mock `fetch_market_resolution` with a real return value
  (`AsyncMock(return_value=1.0)` or `0.0`). A default of `return_value=None` causes the new
  CLOB-first flow to skip the cycle and the test to silently pass without exercising the path.