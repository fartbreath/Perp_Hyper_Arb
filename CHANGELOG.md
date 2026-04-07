# Changelog

All notable changes to this repository are documented in this file.

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
