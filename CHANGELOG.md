# Changelog

All notable changes to this repository are documented in this file.

## [2026-04-10b] - RESOLVED exit: 3-level outcome hierarchy (PM API → oracle → CLOB mid)

### Monitor — RESOLVED exit now uses PM settlement data as primary source

**Bug:** At the exact settlement second, the CLOB order book shows a stale mid price
(e.g. DOWN token ≈ $1.00) before it has absorbed the Chainlink oracle update.  The
RESOLVED fast-path was using `book_no_res.mid` as `exit_mid`, which snapped to `1.0`
→ `resolved_outcome="WIN"` → `pnl=+$0.15`.  No sell order is placed for RESOLVED exits
(PM distributes settlement directly), so this paper gain was **never received**.

**Example trade:** Bitcoin Up or Down 2:35–2:40 AM ET on 2026-04-10.
- Entered DOWN at $0.90, size 1.511
- CLOB book mid for DOWN at 06:40:01: ~$1.00 → bot recorded WIN +$0.15
- Chainlink settled BTC = $72,015.76 > strike $71,983.31 → DOWN lost
- Actual payout from Polymarket: $0 (real P&L: −$1.36; accounting error: +$1.51)

**Fix:** The RESOLVED fast-path now uses a three-level hierarchy to determine
`exit_mid`, from most to least authoritative:

1. **PM CLOB settlement API** (`fetch_market_resolution(condition_id)`):
   Queries `GET /markets/{condition_id}`, returns the settled YES-token price
   (0.0 or 1.0) once `closed=True`.  This is PM's own statement of the outcome,
   independent of order book state.  For NO/DOWN positions, `exit_mid = 1 − yes_price`.

2. **Oracle spot vs. strike** (momentum positions only, when L1 is still `None`):
   Compares the RTDS/Chainlink oracle spot price against `pos.strike` to infer the
   settlement direction.  Catches the window before the CLOB market object is marked
   closed on PM's side.

3. **CLOB book mid** (fallback, original behaviour):
   Used only when L1 and L2 both fail.  `_redeem_ready_positions()` acts as a final
   safety net in live mode — it re-closes with the correct payout from the Data API.

---

## [2026-04-10] - Momentum bug fixes: hysteresis, auto-redeem LOSS, slippage guard, strike diagnostics

### Monitor — Hysteresis reset guard (P2)

Previously `_delta_sl_ticks` (the 2-tick consecutive-below-threshold counter) was reset
on **any** non-STOP event, including oracle data gaps where `current_spot is None`.
A brief WebSocket interruption before the second tick would silently clear the counter,
effectively disabling the SL until the position crossed the threshold again from scratch.

Fix: the counter is now only reset when `current_spot is not None and pos.strike > 0`
— i.e., only when we have a valid oracle reading that genuinely showed delta is above
threshold.  Data gaps no longer reset the in-progress hysteresis accumulation.

### Monitor — Auto-redeem records LOSS outcome (P3)

When the PM wallet returned `redeemable=True, payout=0` (position resolved against),
`close_position()` was never called.  The position stayed open in the risk engine
indefinitely, `resolved_outcome="LOSS"` was never written to `trades.csv`, and the
risk engine's USD exposure remained inflated.

Fix: `_redeem_ready_positions()` now calls `close_position(exit_price=0.0, resolved_outcome="LOSS")`
for every zero-payout resolution, using the same market/token lookup as the WIN path.
Also fixed the `curPrice` field resolution to handle all PM API field name variants
(`curPrice`, `currentPrice`, `cur_price`).

### Scanner — Post-fill slippage guard

Added a post-fill check that aborts and cancels the order if the confirmed fill price
is below `MOMENTUM_PRICE_BAND_LOW`.  A fill significantly below the band means the ask
stack was swept during order transit (e.g. 0.925 → 0.12 with 87% slippage); the token
is no longer in a valid signal state and holding it is uneconomical.

### Scanner — Strike surfaced in diagnostics early (P1 partial)

The window-open spot recording for Up/Down markets was moved to a **pre-band** block
that runs before the signal band filter.  This ensures the strike is locked at the
moment the window opens, even for markets that start out-of-band.  Previously the
strike could be recorded minutes late after the price had already moved.

The recorded strike is now written to `_d["strike"]` at every scan-loop state —
including markets that are skipped by band/cooldown/delta filters — so the Signals page
can display it for all in-window markets.  Explicit-strike markets (e.g. "BTC above
$72,000") also surface their title-parsed strike.

### Webapp — Strike / Spot column on Signals page

The momentum diagnostics table now has a **Strike / Spot** column showing the recorded
strike price (white) alongside the current live oracle spot (grey) for every in-window
market.  This allows visual validation that the recorded strike aligns with Polymarket's
actual settlement oracle before deciding on P1 (strike recording alignment fix).

### pm_client — Taker order support

`place_limit_order()` accepts a `post_only=False` flag that switches the order type to
FAK (Fill-And-Kill) for immediate taker execution.  The "crosses book" retry is skipped
for taker orders since a crossing price is intentional.

### live_fill_handler — Skip duplicate reconciliation of closed markets

The PM wallet retains won tokens until they are manually redeemed on-chain.  Without
this guard, the reconciler would re-import already-closed positions, triggering a
duplicate `close_position()` call and a second row in `trades.csv`.

Fix: `closed_market_ids` is now built from the risk engine before the wallet loop, and
any wallet position whose `condition_id` is already closed is skipped.

### market_data — ChainlinkWSClient targets OCR2 aggregator addresses

Corrected the `eth_subscribe` filter to target the underlying OCR2 aggregator addresses
(not the proxy contracts) so `AnswerUpdated` events are actually received.  Proxy
contracts emit no logs directly — events come from the aggregator.
`SpotOracle` updated to include `ChainlinkWSClient` as a first-class source in the
freshest-wins race for non-HYPE Chainlink coins, with documentation clarifying that
live WS events require a paid Polygon RPC endpoint (Alchemy/Infura).

---

## [2026-04-07] - RTDS Chainlink routing + Webapp QA

### Summary

- Backend: RTDS Chainlink routing updated so short-bucket markets (5m/15m/4h) use the RTDS `crypto_prices_chainlink` relay as primary; ChainlinkWSClient (public eth_subscribe) is not relied on in production.
- RTDS: expanded `crypto_prices_chainlink` symbol map to include BTC/ETH/SOL/XRP/BNB/DOGE in addition to HYPE.
- Chainlink: corrected BNB AggregatorV3 Polygon address to `0x82a6c4AF830caa6c97bb504425f6A66165C2c26e`.
- `SpotOracle`: simplified routing to prefer RTDS chainlink snapshots; HYPE still races with Chainlink Streams when configured.
- New: `data/_compare_chainlink_sources.py` — script to compare RTDS chainlink relay vs direct AggregatorV3 HTTP polling (used for audit & latency analysis).

### Tests

- Unit tests: 918 passed, 6 skipped (live RTDS/Chainlink WS integration tests related to public eth_subscribe remain known-failing and are excluded from CI until a paid/working WS provider is available).
- `tests/test_main_wiring.py` updated to assert RTDS chainlink routing for 5m/15m/4h markets.

### Webapp

- Fixed multiple UI bugs (Performance, Logs, Markets, Settings, Trades) and resolved ESLint/TypeScript issues.
- Webapp dev server: verified running on port 5174 in dev environment.

### Rationale

These changes ensure production uses a low-latency, non-polling Chainlink relay (RTDS) for short-bucket oracle resolution while keeping on-chain AggregatorV3 addresses correct for reference and recovery modes. The QA pass tightened front-end correctness and linting, improving developer ergonomics and observability.

---

For detailed notes, see `market_data/rtds_client.py`, `market_data/spot_oracle.py`, and `webapp/src/pages/*`.
# Changelog

## 2026-04-06 — On-chain Chainlink oracle, range markets, UP/DOWN side fix, spot client rename

### On-chain Chainlink oracle (`market_data/rtds_client.py`)
- Added a second persistent WebSocket to `RTDSClient`: Polygon WSS `eth_subscribe` logs for
  Chainlink AggregatorV3 `AnswerUpdated` events on BTC/ETH/SOL/XRP/BNB/DOGE contracts.
- This is the **authoritative** price Polymarket reads at expiry to resolve 5m/15m/4h markets —
  subscribing to it on-chain means the bot uses the exact same oracle, not a proxy.
- Internal state split into `_chainlink_onchain` (primary) and `_chainlink_rtds` (fallback for HYPE
  and as a bridge between on-chain heartbeats).  Public API unchanged: `get_mid_chainlink()` /
  `get_spot_chainlink()` return on-chain price first, RTDS WS second.
- Added `all_chainlink_mids()` helper to expose both sources merged.
- Health-log loop updated: RTDS exchange prices still warn on >30 s staleness; on-chain ages are
  logged informatively (large ages are expected — oracle only updates on ≥0.5% deviation).
- LINK removed from `_RTDS_SYM_TO_COIN` (not a traded underlying; was causing untracked-coin log spam).
- Reconnect with exponential back-off (1 s → 60 s); 120 s silence triggers zombie-reconnect.

### Range markets sub-strategy (`config.py`, `scanner.py`, `api_server.py`, webapp)
- Added `MOMENTUM_RANGE_ENABLED` flag (off by default) to opt-in to scanning "Will BTC be between
  $X and $Y?" range markets.
- New independent config knobs: `MOMENTUM_RANGE_PRICE_BAND_LOW/HIGH`, `MOMENTUM_RANGE_MAX_ENTRY_USD`,
  `MOMENTUM_RANGE_VOL_Z_SCORE`, `MOMENTUM_RANGE_MIN_TTE_SECONDS` — all hot-patchable at runtime.
- Scanner detects range markets via `_RANGE_MARKET_RE` (in new `market_utils.py`) and strips the YES
  token from the band check if `MOMENTUM_RANGE_ENABLED` is false.
- Webapp Settings page has a new "Range Markets" card with all five knobs.
- Webapp Positions page shows a separate "Range Positions" table for `strategy == "range"` trades.
- All range config fields wired through `api_server.py` `/config` GET/PATCH endpoints.

### UP/DOWN market side-label fix (`live_fill_handler.py`)
- Introduced `_side_for_token()`: for Up/Down markets the side label is `"UP"` or `"DOWN"`, not
  `"YES"` or `"NO"`.  `risk.py` keys positions as `{market_id}:{side}`, so a mismatch created
  a duplicate ghost position instead of merging fills correctly.
- `import_positions()` also fixed: `bot_by_token` lookup now treats `"UP"` as `token_id_yes` and
  `"DOWN"` as `token_id_no`, preventing duplicate imports on restart.

### `spread_id` field (`risk.py`)
- Added `spread_id: Optional[str]` to the `Position` dataclass (populated when a position is one
  leg of a calendar spread; `None` for all existing single-leg positions).
- Added `spread_id` column to `data/trades.csv` (empty string for legacy / single-leg trades).

### Shared market-classification utilities (`strategies/Momentum/market_utils.py`) — NEW FILE
- Extracted `_STRIKE_PATTERNS`, `_UPDOWN_RE`, `_INVERTED_DIRECTION_RE`, `_RANGE_MARKET_RE`,
  `_extract_strike`, `_extract_range_bounds`, `_is_updown_market`, `_is_range_market`,
  `_is_inverted_direction_market` into a standalone module.
- Breaks the circular import between `scanner.py` and `spread.py`, both of which need these helpers.
- `live_fill_handler.py` reuses `_is_updown_market` for the side-label fix above.

### `pyth` → `spot_client` rename (all files)
- Every internal `pyth` / `self._pyth` / `pyth=` reference renamed to `spot_client` / `self._spot` /
  `spot_client=` across `monitor.py`, `scanner.py`, `vol_fetcher.py`, `maker/strategy.py`,
  `mispricing/strategy.py`, `main.py`.  RTDSClient is the actual underlying technology; "Pyth" was
  a historical misnomer.

### Tests
- `tests/test_rtds_live.py`: added three new live-feed test classes (887 total unit tests):
  - `TestRTDSSustainedFeed` — 30 s observation, tick-count, max-gap, half-window silence (23 tests).
  - `TestRTDSSourcesSeparated` — RTDS and Chainlink tracked in separate dicts over 30 s (28 tests).
  - `TestRTDSRawThroughput` — raw WS frame counter vs processed ticks diagnostic (1 test).
  Confirmed: ~1 tick/s/coin per source is the RTDS feed ceiling (not a client limitation); zero
  frames are dropped.
- `tests/test_api_server.py`: added coverage for range market config endpoints.
- `tests/test_momentum_scanner.py`: extended coverage for range market detection and inverted-direction logic.

## 2026-04-03 — Docs: SSE & polling updates

- Documented backend SSE endpoint and frontend SSE hook migration (reduced polling).
- Noted changes to polling intervals and cache behaviour (P&L 30s cache).
- Mentioned client-side `useSSE` hooks and server `/events` stream for live updates.
- QA fixes: mispricing scanner event ordering, signals list cap, small docstring fixes.

See commit history for code changes and tests (757 passed, 6 skipped).
