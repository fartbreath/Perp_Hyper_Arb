# Changelog

All notable changes to this repository are documented in this file.

## [2026-04-30] - OpeningNeutral entry latency; Chainlink HA mode; monitor & reconcile fixes

### Feature — OpeningNeutral entry latency reduction (`strategies/OpeningNeutral/scanner.py`, `config.py`)

Three optimisations that together reduce entry latency from ~1 s post-open to ~50 ms:

**Idea 1 — Scheduled timer entry (~200-400 ms saved):**  When `_refresh_pending_markets` registers a pre-open market, it spawns a `_scheduled_entry_task` that sleeps until `open_ts - TIMER_ADVANCE_SECS` (default 50 ms before open) and fires `_evaluate_entry` directly.  Entry no longer depends on a WS tick arriving after market open.  `_entered_market_ids` prevents the WS path from entering twice if both paths race.

**Idea 2 — Pre-qualification (~100-150 ms saved):**  Static gates (market type, `_is_updown_market` direction, entry-window membership) are checked once in `_refresh_pending_markets` at registration time rather than on every WS tick.  At timer-fire time only dynamic gates (combined cost, concurrent cap, conflict guard) run.  The `elapsed_min` lower bound is relaxed to `-1.0 s` when `_timer_fired=True` so timer-fired calls arriving 50 ms before open are not rejected.

**Idea 5 — TCP connection pre-warm (~50-100 ms saved):**  `_scheduled_entry_task` fires `_prewarm_clob()` at `open_ts - PREWARM_SECS` (default 200 ms before open), sending a lightweight authenticated GET (`get_live_orders`) to establish a TCP+TLS socket in the `requests` connection pool before the BUY order POSTs fire.  Non-fatal — entry proceeds normally if pre-warm fails.

**New config params (2):** `OPENING_NEUTRAL_PREWARM_SECS = 0.2`, `OPENING_NEUTRAL_TIMER_ADVANCE_SECS = 0.05`.

**New scanner state:** `_scheduled_entry_market_ids: set[str]` guards against duplicate timer scheduling across repeated `_refresh_pending_markets` sweeps; discarded in `finally` block of task.

**`_evaluate_entry` signature change:** `async def _evaluate_entry(self, market, _timer_fired=False)` — new `_timer_fired` parameter; elapsed lower bound becomes `elapsed_min = -1.0 if _timer_fired else 0.0`.

**Docs:** `strategies/OpeningNeutral/PLAN.md` updated with Entry Timing Architecture section (ideas 1, 2, 5), revised Order Flow (step 0 pre-open), Entry Conditions table with pre-qual column, Configuration section with new keys, Scanner State section.

### Feature — Chainlink Data Streams HA mode (`market_data/chainlink_streams_client.py`)

Migrates the Chainlink WS client from a single persistent connection to a multi-origin High-Availability architecture, mirroring the Chainlink Go SDK (`data-streams-sdk/go/stream.go`):

- One persistent WebSocket per server origin (typically 2: origin 001 and 002) discovered from the `x-cll-available-origins` header on the initial HTTP GET.
- Reports from all origins are deduplicated by `observationsTimestamp` watermark — the first copy wins, all duplicates are discarded.  No asyncio.Lock needed (check+set has no await between them).
- Reconnect with exponential backoff (`_HA_RECONNECT_MIN_S = 1.0`, `_HA_RECONNECT_MAX_S = 10.0`, max 5 attempts) classified as partial (≥1 other connection alive) vs full (all down).
- `StreamStats` dataclass (`accepted`, `deduplicated`, `partial_reconnects`, `full_reconnects`, `configured_connections`, `active_connections`) mirrors Go SDK Stats struct; accessible as `client.stats`.
- `is_connected` now returns `True` when `stats.active_connections > 0` (was: `self._ws is not None`).

### Fix — `should_exit` hedge-active parameter (`monitor.py`)

`should_exit` is a free function; it previously accessed `self._risk` directly to determine hedge fill status. `hedge_active: bool` is now a parameter computed by `PositionMonitor._check_and_exit_position` before the call, keeping `should_exit` pure. Logic unchanged.

### Fix — Momentum ticks dedup by composite key (`monitor.py`)

`_last_tick_state: dict[str, tuple]` tracks the last `(spot, token_price)` written per `market_id`. A tick is skipped when both values are unchanged and `exit_flag=False`, eliminating sub-millisecond burst rows caused by multiple oracle sources (Chainlink Streams, RTDS Chainlink, RTDS, PM WS) firing on the same price update.

### Fix — FAK order integer-k amount (`pm_client.py`)

The CLOB API requires `takerAmount` to be a multiple of 10 000 (≤ 2 dp in USDC).  `round(contracts × price, 2)` produces 4 dp for most tick-aligned prices.  Fix: `fak_amount = k * price` where `k = max(1, round(contracts))`.  IEEE 754 guarantees `(k × x)` is exact for integer `k`, so `takerAmount = k × 10^6` is always an exact multiple.  Market order BUY `mkt_amount` clamped to `max(size, 1.0)` to satisfy the PM $1 minimum.

### Fix — Reconcile: skip PnL patch when winner REDEEM not yet indexed (`pm_reconcile.py`)

`correct_outcome = None` is now returned when only `$0`-value REDEEM entries exist — this is ambiguous between "loser redeemed for $0" and "winner REDEEM not indexed by PM yet".  `None` means "leave recorded value unchanged".  PnL correction is also gated on `_has_real_exit_proceeds` to prevent over-writing a valid PnL with `−buy_usdc` when exit proceeds are missing.

### Fix — `finalize_hedge` double-write guard (`risk.py`)

`finalize_hedge` now returns early with the existing record if the hedge order's status is already in `HedgeStatus.TERMINAL`, preventing duplicate CSV rows when the function is called more than once for the same order.

---

## [2026-04-29] - Strategy 5 (Opening Neutral); CLOB v2 migration; hedge SL fix; venv auto-detection

### Feature — Strategy 5: Opening Neutral (`strategies/OpeningNeutral/scanner.py`, `strategies/OpeningNeutral/__init__.py`, `config.py`, `main.py`, `api_server.py`, `risk.py`)

Simultaneously buys the YES and NO token of the same Up/Down bucket market within `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` of market open. When both FAK legs fill at a combined cost ≤ $1.00 the pair is guaranteed-profitable at resolution. When only one leg fills the surviving leg is either promoted to a standard momentum position (`keep_as_momentum`) or immediately taker-exited (`exit_immediately`).

**New config params (14):** `OPENING_NEUTRAL_ENABLED`, `OPENING_NEUTRAL_DRY_RUN`, `OPENING_NEUTRAL_MARKET_TYPES`, `OPENING_NEUTRAL_ENTRY_WINDOW_SECS`, `OPENING_NEUTRAL_COMBINED_COST_MAX`, `OPENING_NEUTRAL_SIZE_USD`, `OPENING_NEUTRAL_ORDER_TYPE`, `OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS`, `OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS` (5 s — short window so exchange-killed FAKs fail fast), `OPENING_NEUTRAL_ONE_LEG_FALLBACK`, `OPENING_NEUTRAL_LOSER_EXIT_PRICE`, `OPENING_NEUTRAL_MIN_SIDE_PRICE`, `OPENING_NEUTRAL_MAX_SIDE_PRICE`, `OPENING_NEUTRAL_MAX_CONCURRENT`.

**Other changes:**
- `risk.py`: `neutral_pair_id: str = ""` field on `Position` links both YES and NO legs; cleared on the winner when promoted to momentum.
- `api_server.py`: `opening_neutral_ref` in `BotState`; `opening_neutral_enabled` / `opening_neutral_dry_run` exposed via `GET /config` and `PATCH /config`; `GET /opening_neutral/status` endpoint returns enabled state, dry_run, active pair count, pair details, and recent scan diagnostics.
- `main.py`: `OpeningNeutralScanner` instantiated when `OPENING_NEUTRAL_ENABLED=True`; wired into the asyncio task graph; `neutral_pair_id` propagated in `state_sync_loop`.
- `strategies/Momentum/scanner.py`: Opening-neutral conflict guard — skips any market where `opening_neutral` already has an open position, with `skip_reason="opening_neutral_active"` in diagnostics.
- Event logging (4 types → `data/momentum_events.jsonl`): `OPENING_NEUTRAL_PAIR_REGISTERED`, `OPENING_NEUTRAL_ONE_LEG_PROMOTED`, `OPENING_NEUTRAL_ONE_LEG_EXITED`, `OPENING_NEUTRAL_NO_FILL`.
- Concurrent entry guard counts both active pairs **and** in-flight entry attempts against `OPENING_NEUTRAL_MAX_CONCURRENT`.

### Fix — CLOB v2 migration (`pm_client.py`, `monitor.py`, `api_server.py`, `tests/test_pm_client.py`)

Fully migrated all production CLOB calls from `py_clob_client` → `py_clob_client_v2`.

**`pm_client.py` changes:**
- FAK (taker) limit path now uses `MarketOrderArgs` / `create_market_order` instead of `OrderArgs` / `create_order`. `MarketOrderArgs` uses the market-order signing path which rounds USDC amount to 2dp automatically, satisfying the API's maker_amount constraint.
- `cancel` → `cancel_order(OrderPayload(orderID=order_id))`.
- `get_orders` → `get_open_orders`.
- `BalanceAllowanceParams`, `AssetType` imports updated to `py_clob_client_v2`.
- Order `size` rounded to 2dp before all CLOB calls (API constraint: maker amount max 2dp).

**`monitor.py`, `api_server.py` redeem fix:**
- `pm._clob.get_conditional_address()` / `get_collateral_address()` don't exist in v2.
- Replaced with `_POLY_CONTRACTS = _get_contract_config(137)` (pure/hardcoded — no network call); use `.conditional_tokens` and `.collateral` fields.

**Tests (`tests/test_pm_client.py`):**
- All `py_clob_client` imports → `py_clob_client_v2`.
- FAK tests updated to mock `create_market_order` and assert `create_order.assert_not_called()`.
- `cancel` tests updated to `cancel_order(OrderPayload(...))`.
- Stale `get_conditional_address` / `get_collateral_address` mocks removed from `TestAutoRedeemDedup`.
- `test_no_winner_flag_falls_back_to_yes_price` renamed to `test_no_winner_flag_returns_none_for_retry` (preamble rule: never infer WIN/LOSS from `price`).

### Fix — Hedge SL suppression now requires actual fill (`monitor.py`)

Previously, any position with a non-null `hedge_order_id` suppressed all stop-losses (oracle delta SL, near-expiry stop, prob-SL). If the hedge order was cancelled or expired unfilled, the position was left naked with no protection but stop-losses still disabled.

**Fix:** `_hedge_active` now calls `self._risk.get_hedge_order(pos.hedge_order_id)` and checks `size_filled > 0`. An unfilled or cancelled hedge provides zero insurance — stop-losses run normally in that state.

### Fix — Launcher and main.py venv auto-detection (`launcher.py`, `main.py`)

**`launcher.py`:** Added `_find_venv_python()` which scans sibling `.venv` directories for the correct Python interpreter. `BOT_PYTHON` replaces `sys.executable` in `subprocess.Popen` — the bot always starts with the project venv regardless of how the launcher was invoked.

**`main.py`:** Venv guard at the top of the file: if `py_clob_client_v2` is not importable, the script calls `os.execv()` to relaunch itself with the `.venv` Python. Falls back to a clear error message with activation instructions if no venv is found.

### Fix — Gamma API per-slug error handling (`pm_client.py`)

Previously, any exception during the Gamma slug fetch (most commonly `asyncio.TimeoutError` with an empty `str()`) aborted the entire market refresh, leaving `_markets` stale. Now exceptions are caught per-slug; the failed slug is logged as a `WARNING` and the loop continues to the next slug.

### Feat — Multi-strategy WS token registration (`pm_client.py`)

`register_for_book_updates(token_ids, owner="default")` now accepts an `owner` key. Registrations are stored in `_extra_tokens_by_owner: dict[str, set[str]]` and unioned in `_update_shards`. This prevents one strategy from overwriting another's WS subscriptions (e.g. momentum and opening_neutral can both register independently). Extra tokens are also ordered and appended to `new_tokens` in `_update_shards` so they actually get subscribed to shards.

### Fix — `fetch_market_resolution` no price fallback (`pm_client.py`)

When `closed=True` but no `winner` flags are present in the CLOB response, the method previously fell back to the YES token's `price` field. Per the preamble rule, `price` can briefly show ~1.0 for a losing token right after settlement. The fallback is removed: the method now returns `None` so the monitor retries later when winner flags are set.

## [2026-04-27] - GTD hedge reprice sizing fix; `natural_contracts` field; parametric sweep tests

### Bug fix — Hedge reprice used inflated contract count instead of position-matched count (`monitor.py`, `risk.py`, `strategies/Momentum/scanner.py`)

When the natural coverage cost was below the PM $1 minimum at placement, `hedge_contracts`
was inflated to `1 / hedge_price` (e.g. 50ct for a 7.75ct position at 2¢). The monitor
reprice loop carried that inflated size forward on each tick, so repricing to 3¢ cost
`50 × 0.03 = $1.50` instead of the correct `ceil(1/0.03) × 0.03 = $1.00`. This eroded
`MIN_RETAIN_USD` and in some cases made the reprice unprofitable.

**Root cause:** `_pnl_cap` used the inflated `hedge_contracts` as divisor (6× too narrow),
and `monitor.py` reused `ho.order_size` (the inflated placement size) rather than the
natural position-matched count.

**Fix:**

- `scanner.py`: Capture `_natural_hedge_contracts = hedge_contracts` *before* the `$1` floor
  overwrites it. Use `_natural_hedge_contracts` for `price_cap`, ladder, and taker
  computations. Pass it to `register_hedge_order(natural_contracts=...)`.

- `risk.py` — `HedgeOrder` dataclass: Added `natural_contracts: float = 0.0` field (the
  pre-floor count, stored at placement). `register_hedge_order` accepts and stores it.
  `replace_hedge_order` accepts `new_order_size: float = 0.0`; if provided, the replacement
  `HedgeOrder` uses the new size rather than propagating the old inflated count.
  `natural_contracts` is always propagated across reprices.

- `monitor.py` — reprice sizing block: Replaced `max(remaining, 1/new_bid)` with
  a two-branch formula mirroring scanner placement:
  - If `ho.natural_contracts × new_bid >= $1` → use `natural_contracts` (full coverage).
  - Else → use `1 / new_bid` (PM $1 minimum, correct floor size for new price).
  Falls back to `ho.order_size` for legacy `HedgeOrder` objects without `natural_contracts`.
  PnL ceiling (projected_pnl − MIN_RETAIN) applied afterwards.

- `config.py`: `MOMENTUM_HEDGE_MIN_RETAIN_USD` changed from `0.15` → `0.25`.

### Feature — COB (CLOB-Oracle Blend) Kelly win-probability (`config.py`)

Added four new config parameters for the planned CLOB-Oracle Blend sizing model:
- `MOMENTUM_KELLY_EDGE_PREMIUM = 0.07` — systematic alpha above CLOB ask.
- `MOMENTUM_KELLY_WIN_PROB_CAP = 0.95` — hard cap on blended win_prob.
- `MOMENTUM_KELLY_CLOB_RELIABLE_TTE = 60` — seconds above which CLOB is fully weighted.
- `MOMENTUM_KELLY_ORACLE_SENSITIVITY = 0.15` — signal-strength → win_prob slope.

### Tests — Hedge sizing mathematical model and parametric sweep (`tests/test_hedge_sizing.py`, `tests/test_hedge_sweep.py`)

- `test_hedge_sizing.py`: 83-test pure-math suite verifying the scanner→monitor sizing
  pipeline: floor/natural branch crossover, `price_cap` profitability proof, `HedgeOrder`
  `natural_contracts` round-trip through `register`/`replace`, 7 scenario reprice ladders,
  and `HedgeOrder` risk-engine unit tests.

- `test_hedge_sweep.py`: 200-case parametric sweep (10 entry prices 0.65→0.83 × 20 buy
  notionals $1→$20) each running 94 hedge price steps (1¢–94¢). Confirms across 4,434
  non-blocked steps: PM $1 minimum never violated, over-hedge margin $0.00, MIN_RETAIN
  floor always maintained. Runs in < 1 second.

### Fix — Webapp trades page (`webapp/src/pages/Trades.tsx`)

Minor display improvements to the Trades dashboard page.

## [2026-04-25] - Chainlink Data Streams direct feed for all coins; hedge reprice + SL suppression; Phase C TTE gate; per-type delta floor

### Feature — Chainlink Data Streams direct feed extended to all 7 coins (`market_data/chainlink_streams_client.py`, `market_data/spot_oracle.py`, `config.py`)

Previously `ChainlinkStreamsClient` only fed HYPE/USD; all other coins used the RTDS
`crypto_prices_chainlink` relay as primary. After benchmarking all seven supported coins
(HYPE, BTC, ETH, SOL, BNB, DOGE, XRP), the direct Data Streams WebSocket consistently
arrives ~190ms ahead of the relay with 0.000bps price delta (100% direct wins over 58
matched rounds per coin in 60s tests).

**Changes:**

- `config.py`: Added `CHAINLINK_DS_FEED_IDS` dict — maps all 7 coins to their feed IDs,
  read from per-coin env vars `CHAINLINK_DS_{COIN}_FEED_ID`. Old single
  `CHAINLINK_DS_HYPE_FEED_ID` var retained for backwards compatibility.

- `chainlink_streams_client.py`: Multi-feed support — connects to a single WebSocket with
  all configured feed IDs and dispatches messages by `report.feedID`. `start()` now accepts
  an optional `coin=` parameter to subscribe to a single feed (used by the comparison
  script to avoid per-connection rate limiting during testing). `_active_feeds` dict replaces
  direct `config.CHAINLINK_DS_FEED_IDS` references in `_build_auth_headers` and `_ws_loop`.

- `spot_oracle.py`: `_get_chainlink_spot()` now prioritises ChainlinkStreamsClient for all
  coins in Chainlink bucket types (5m/15m/4h), falling back to RTDS relay then ChainlinkWSClient.
  Previous logic used freshest-timestamp arbitration and only used direct streams for HYPE.

**New env vars (all optional — bot degrades gracefully to RTDS-only without them):**
```
CHAINLINK_DS_BTC_FEED_ID=0x00039d9e4539...
CHAINLINK_DS_ETH_FEED_ID=0x000362205e10...
CHAINLINK_DS_SOL_FEED_ID=0x0003b778d3f6...
CHAINLINK_DS_BNB_FEED_ID=0x000335fd3f3f...
CHAINLINK_DS_DOGE_FEED_ID=0x000356ca64d3...
CHAINLINK_DS_XRP_FEED_ID=0x0003c16c6aed...
```

**New comparison script:** `scripts/compare_oracle_feeds.py --coin <COIN> --duration <S>`
runs a live side-by-side benchmark of direct vs relay for any coin.

---

### Feature — Hedge gap-closing reprice (`risk.py`, `monitor.py`, `config.py`)

When a GTD hedge order is resting in the CLOB, the monitor now tracks whether the
opposite-token's `best_ask` is falling (seller moving toward our bid). If the ask drops
since the last sweep, the hedge is cancelled and reposted at `current_bid + $0.01` —
closing the spread without chasing a rising ask.

Repricing is bounded by `price_cap` (set at placement: max price that keeps projected PnL
above `MOMENTUM_HEDGE_MIN_RETAIN_USD`). Reprices that would exceed the cap are skipped.

**New `HedgeOrder` fields:** `price_cap: float`, `last_clob_ask: Optional[float]` — both
persisted in `hedge_orders.json` so restarts don't lose the reference ask.

**New `RiskEngine` method:** `replace_hedge_order(old_id, new_id, new_price)` — atomically
marks the old order CANCELLED, creates a replacement with all metadata copied, updates
the parent Position's `hedge_order_id`, and persists.

**New config key:** `MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: int = 5`  
Near-expiry cancel: if TTE ≤ this threshold and the held token's CLOB mid is above 0.50
(winning), the hedge is cancelled — insurance no longer needed and adverse fill prevented.
Set to `0` to disable.

---

### Feature — Hedge SL suppression (`monitor.py`, `config.py`)

When `MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL = True` and a position has a resting GTD hedge,
all stop-losses (oracle delta SL, near-expiry time stop, CLOB prob-SL) are suppressed.
The hedge bounds the downside; any SL exit would lock in a loss before the hedge pays off.
Take-profit remains active. Defaults to `False` (conservative — all SLs fire regardless).

**New config key:** `MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL: bool = False`

---

### Feature — Phase C per-type TTE floor (`strategies/Momentum/scanner.py`, `config.py`)

A new per-bucket-type TTE ceiling blocks entries when time-to-expiry falls below a
configured threshold. Complements Phase B (global TTE ceiling) with type-specific tuning.

**New config key:** `MOMENTUM_PHASE_C_MIN_TTE_SECONDS: dict[str, int] = {}`  
Example: `{"bucket_5m": 30, "bucket_15m": 45}` — block entries in the last 30s of 5m
markets and last 45s of 15m markets. `0` or absent = disabled for that type.

Skipped markets are counted in scan diagnostics as `skipped_phase_c`.

---

### Feature — Per-bucket-type delta floor (`strategies/Momentum/scanner.py`, `config.py`)

`MOMENTUM_MIN_DELTA_PCT` can now be overridden per bucket type. The effective floor is
`max(coin_floor, type_floor)` — never lower than either individual setting.

**New config key:** `MOMENTUM_MIN_DELTA_PCT_BY_TYPE: dict[str, float] = {}`  
Example: `{"bucket_5m": 0.10, "bucket_15m": 0.08}`. Absent = falls back to coin floor.

`min_delta_floor` in scan diagnostics now reflects the combined (type + coin) floor.

---

### Fix — GTD hedge finalization after bot restart (`monitor.py`, `risk.py`)

Auto-redeem loop now handles the case where the parent Position was evicted from memory
after a restart but the HedgeOrder entity persisted in `hedge_orders.json`.

Previously: hedge payout silently lost (no `finalize_hedge` call, no trades.csv entry).  
Now: `get_hedge_order_by_token_id()` is called as a secondary lookup. If the HedgeOrder
is found, `finalize_hedge()` is called directly with the correct `filled_won`/`filled_lost`
status, updating both `hedge_orders.json` and `trades.csv`.

**New `RiskEngine` method:** `get_hedge_order_by_token_id(token_id)` — O(n) scan of
`_hedge_orders` by token_id; used only in the auto-redeem path (low frequency).

---

### Fix — Pending exit retry on EXIT_ORDER_FAILED (`monitor.py`)

Positions where all CLOB exit attempts fail now register in `_pending_exit_positions`
(maps `"market_id:side"` → original exit reason). On the next monitor sweep, the exit
is retried automatically. Cleared once the retry reaches `_exit_position`.

---

## [2026-04-23] - Concurrent TP+hedge; band_floor_abort hedge fix

### Bug fix — Sequential TP blocking hedge placement (`strategies/Momentum/scanner.py`)

**Root cause:** After a fill, the scanner placed the take-profit SELL limit (Phase C) and
only then placed the GTD hedge (Phase D). A TP retry loop of up to N attempts (~2 s wall
time) ran before the hedge started. On fast-moving markets (e.g. BTC 9:05AM April 22) the
opposite-token book drained during that delay, leaving nothing to bid on when the hedge
eventually ran.

**Fix:** Both coroutines (`_do_tp` and `_do_hedge`) are now defined as local `async def`
functions and launched together with `asyncio.gather(_do_tp(), _do_hedge(),
return_exceptions=True)`. They share the same event-loop and interleave at every `await`
point, meaning the hedge bid hits the CLOB at essentially the same instant as the TP order.

No new config keys.

---

### Bug fix — `band_floor_abort` path skipped GTD hedge entirely (`strategies/Momentum/scanner.py`)

**Root cause:** When a taker fill landed below `MOMENTUM_PRICE_BAND_LOW` (swept-book fill,
e.g. signal 0.88 → fill 0.34), the scanner registered the position via `_pos_bfa` and
immediately `return False`-ed. Phase D (the GTD hedge coroutine) was never reached.

This is precisely the scenario where the hedge matters most: a fill deep in the band means
the position is already underwater. A $0.05 resting BUY on the opposite token costs ~$1
and pays up to ~$19 if the position ultimately resolves against the main side.

**Measured miss (April 22 ETH 9:05AM):** fill 0.34 (signal 0.88), no hedge placed → loss
-$2.38. A DOWN hedge at $0.05 would have recovered ~$18 if ETH settled DOWN.

**Fix:** Replaced the early `return False` with a `_band_floor_aborted` flag. Execution now
falls through to the normal Phase C+D `asyncio.gather` block. `_do_tp` returns immediately
when the flag is set (no TP management for swept-book entries). `_do_hedge` runs normally
and is subject to all existing hedge rules:

- Projected win PnL must exceed $1 (`entry_size × (1 − entry_price) > 1.0`)
- Profit-safe price cap: `max_hedge_price = (projected_pnl − MOMENTUM_HEDGE_MIN_RETAIN_USD) / hedge_contracts`
- PnL cap ≤ 0 → hedge skipped
- TTE < 5 s guard still applies
- Book depth / ladder exhaustion still applies

After the CSV write and position-opened log, `return False` is restored so the monitor's
active SL/TP loop is skipped — exactly as before for band_floor positions.

No new config keys.

---

## [2026-04-22] - Hedge optimization (cap + ladder + taker + TTE); fill price fix; winner-flag resolution; test hardening

### Feature — Hedge optimization: profit-safe price cap (`strategies/Momentum/scanner.py`, `config.py`)

Before placing any hedge, the bot now computes the maximum price per contract it may pay
while still retaining at least `MOMENTUM_HEDGE_MIN_RETAIN_USD` of projected win PnL.

```
max_hedge_price = (projected_win_pnl − MOMENTUM_HEDGE_MIN_RETAIN_USD) / hedge_contracts
```

If the cap comes out ≤ 0 (the trade isn't profitable enough to afford any hedge), Phase D
is skipped entirely. The cap is always respected by both the taker and maker-ladder branches.

**New config key:** `MOMENTUM_HEDGE_MIN_RETAIN_USD: float = 0.50`  
Set to `0.0` to disable (old behaviour — no floor on retained profit).

---

### Feature — Hedge optimization: N-tick concession maker ladder (`strategies/Momentum/scanner.py`, `config.py`)

Instead of a single maker-only attempt at the configured price, the bot now retries up to N
times, raising the bid price by one tick (`$0.01`) per attempt. The ladder stops as soon as
a placement succeeds or the next price would exceed the profit-safe cap.

**New config key:** `MOMENTUM_HEDGE_MAX_TICKS_CONCESSION: int = 3`  
Set to `1` for a single attempt (closest to old behaviour).

---

### Feature — Hedge optimization: book-aware taker fallback (`strategies/Momentum/scanner.py`)

Before starting the maker ladder, the bot fetches the live order book for the opposite
token. If the current best ask is at or below the profit-safe cap, it switches to a taker
(FAK, `post_only=False`) order to grab the fill immediately rather than resting.

No new config key — fires automatically when `best_ask ≤ max_hedge_price`.

---

### Feature — Hedge optimization: TTE aggression mode (`strategies/Momentum/scanner.py`, `config.py`)

When time-to-expiry (`tte_seconds`) falls below a threshold, the bot forces taker mode even
if the book wouldn't have triggered it. This handles near-expiry thin-book scenarios where a
resting maker order has no realistic chance of being matched.

**New config keys:**
- `MOMENTUM_HEDGE_AGGRESSIVE_TTE_S: int = 0` — 0 = disabled; set e.g. 30 to activate
- `MOMENTUM_HEDGE_AGGRESSIVE_TAKER: bool = False` — True = always use taker (paper-mode testing)

---

### Feature — Hedge optimization: per-attempt $1 minimum size (taker branch) (`strategies/Momentum/scanner.py`)

The taker branch now recomputes contract count at the actual taker price (which may be
lower than the config price) and raises it to meet Polymarket's $1 minimum notional floor.
If meeting the $1 minimum would exceed the profit-safe cap budget, the hedge is skipped
rather than placing an oversized order.

No new config key — uses existing `MOMENTUM_HEDGE_MIN_RETAIN_USD`.

---

### Feature — Hedge CLOB tick log (`monitor.py`, `config.py`, `webapp/src/pages/Settings.tsx`)

New `hedge_clob_ticks.csv` sampled once per `_check_all_positions()` sweep while a GTD
hedge order is open and unfilled. Records CLOB mid, best bid, best ask, and TTE alongside
the hedge bid price — used post-trade to diagnose why a hedge didn't fill.

Columns: `ts`, `market_id`, `market_title`, `underlying`, `parent_side`, `hedge_order_id`,
`hedge_token_id`, `hedge_bid_price`, `clob_mid`, `clob_best_bid`, `clob_best_ask`, `tte_s`, `status`.

**New config keys:**
- `MOMENTUM_HEDGE_CLOB_LOG_ENABLED: bool = True` — toggle the hedge_clob_ticks.csv log
- `MOMENTUM_TICKS_LOG_ENABLED: bool = True` — toggle the existing momentum_ticks.csv log

Both are exposed as toggles in the webapp Settings page under **Analysis Logging**.

---

### Feature — Positions page: GTD Hedge Fills section (`webapp/src/pages/Positions.tsx`)

Open positions where `strategy="momentum_hedge"` are now surfaced in a dedicated
**GTD Hedge Fills** table on the Positions page, separate from main momentum positions.
Shows entry price, current CLOB price, deployed capital, and unrealised P&L for each
filled hedge.

---

### Bug fix — Fill price complement inversion (`pm_client.py`)

**Root cause:** `_fire_trade_fill` was computing taker execution price as a VWAP over
`maker_orders[i].price`. On neg-risk Polymarket markets, YES takers are matched against
NO-side makers whose price is the complement (e.g. 0.21 when the taker bought YES at
0.79). Using maker prices produced fill_price ≈ 0.21 for every YES entry.

**Cascading consequence:** Every live YES trade had fill_price < MOMENTUM_PRICE_BAND_LOW
(0.6), triggering `band_floor_abort` on every position. Phase D (GTD hedge placement)
was never reached for any live trade.

**Fix:** `exec_price` now comes exclusively from `trade_msg["price"]` — the taker's
execution price per the Polymarket CLOB API `types.ts` spec. `maker_orders` are used
only to aggregate matched size.

### Bug fix — `fetch_market_resolution` winner-flag priority (`pm_client.py`)

**Root cause:** `fetch_market_resolution` checked `tok.get("price")` first, then the
`winner` flag as a fallback. The preamble explicitly states that `price` can show ~1.0
for a losing token during the settlement window and must never be used as the primary
signal. This could cause WIN/LOSS outcomes in `trades.csv` to be recorded incorrectly
when the monitor ran in the brief window after market close.

**Fix:** `winner: True` flag is now checked first. `price` is only used as a fallback
when the `winner` field is absent from the CLOB API response entirely.

### Tests — Fill and resolution pipeline coverage

**`tests/test_pm_client.py`**
- Replaced `test_trade_fill_vwap_multi_level` with
  `test_trade_fill_uses_taker_price_not_maker_vwap`: maker prices now average to 0.47
  (not 0.50) so the old maker-VWAP path would fail — accidental symmetry removed.
- Added `test_trade_fill_neg_risk_uses_taker_price_not_maker_complement`: exact
  reproduction of the live bug (YES buy at 0.79, NO makers at 0.21). Asserts
  fill price = 0.79.
- Added `TestFetchMarketResolution` (6 tests): winner-flag priority, NO-win path,
  `test_winner_flag_beats_wrong_price` (anti-regression for settlement window race),
  price fallback when flag absent, closed=False → None, UP/DOWN label handling.

**`tests/test_momentum_scanner.py` — `TestGTDHedge`**
- Added `test_live_fill_valid_price_does_not_trigger_band_floor_abort`: live-mode
  scanner test (`_paper_mode=False`) that injects a correct WS fill at 0.79, then
  asserts position opens without `band_floor_abort` and Phase D hedge fires on the
  NO token. Directly catches any regression that reverts the complement price fix.

Total non-live-network tests: 1114 passed, 1 skipped.

---

## [2026-04-21] - HedgeOrder lifecycle entity; async CLOB I/O; Signals sort; Trades fill-ratio display; market_pnl API

### Feature — First-class `HedgeOrder` entity (`risk.py`)

**Problem:** GTD hedge state was scattered across `Position` fields, a transient
`_pending_hedge_cancels` dict in `PositionMonitor`, and ad-hoc logic in several files.
Fills were stored only as WS-detected booleans; there was no structured per-fill history,
no VWAP tracking, and no FIX-style lifecycle (open → partially_filled → filled).

**New `HedgeOrder` dataclass** tracks the full lifecycle of every GTD hedge order:
- Identity fields: `order_id`, `market_id`, `token_id`, `underlying`, `market_type`,
  `market_title`, `placed_at`
- Order params: `order_price`, `order_size`, `order_size_usd`
- Live state: `status` (FIX-style via `HedgeStatus` constants), `size_filled`,
  `size_remaining`, `avg_fill_price` (VWAP), `fills: list[HedgeFill]`
- Deferred-cancel state: `pending_cancel_threshold`, `pending_cancel_side`,
  `pending_cancel_strike`, `pending_cancel_entry_spot` — replaces the transient
  `PositionMonitor._pending_hedge_cancels` dict; survives bot restarts
- Resolution: `settled_price`, `resolved_at`, `spot_at_resolution`, `net_pnl`
- Parent reference: `parent_side` for O(1) `order_id → Position` lookup

**New `HedgeFill` dataclass:** `fill_id`, `price`, `size`, `timestamp`, `source`
(`"ws"` | `"clob_rest"` | `"reconciliation"` | `"paper"`)

**New `HedgeStatus` class:** `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`,
`CANCELLED_PARTIAL`, `EXPIRED_UNFILLED`, `EXPIRED_PARTIAL`, `FILLED_EXITED`, and a
`TERMINAL` frozenset.

**Persistence:** `HedgeOrder` entities are persisted to `data/hedge_orders.json` on
every state change and reloaded on startup.

**New `RiskEngine` methods:**
- `register_hedge_order(...)` — creates and persists a new `HedgeOrder`
- `update_hedge_fill(order_id, price, size, source)` — records a fill event,
  updates VWAP and `size_filled`, transitions status to `PARTIALLY_FILLED`/`FILLED`
- `update_gtd_hedge(...)` — mirrors `parent_side` onto the HedgeOrder entity
- `finalize_hedge(order_id, settled_price, spot_at_resolution, hedge_status)` — writes
  the terminal status and `net_pnl`; called at market resolution
- `get_position_for_hedge(order_id)` — O(1) lookup via `parent_side` key; falls back
  to O(n) scan for legacy orders missing `parent_side`
- `get_position_by_hedge_order_id(order_id)` — deprecated alias for the above
- `get_hedge_order_by_market(market_id)` — returns the most recent non-terminal (or
  any terminal) `HedgeOrder` for a market
- `get_hedge_orders_with_pending_cancel()` — returns all HedgeOrders with a live
  deferred-cancel threshold; replaces the in-memory `_pending_hedge_cancels` dict
- `market_pnl(market_id) → dict` — combined realized + unrealised + hedge P&L snapshot
  for a market; returns JSON-serializable dict consumed by the webapp and api_server

### Feature — Additive `trades.csv` schema migration (`risk.py`)

Two new columns added to `TRADES_HEADER`:
- `hedge_size_filled` — contracts actually matched (hedge rows only)
- `hedge_avg_fill_price` — VWAP across all fill events (hedge rows only)

`_ensure_csv()` now distinguishes between additive schema changes (old header is a
prefix of the new one) and incompatible ones. For additive changes it migrates the
existing file in-place (appends empty columns to every row) rather than backing up and
discarding the history.

### Fix — Async CLOB I/O (`pm_client.py`)

`create_order()`, `post_order()`, `create_market_order()`, `cancel()`, and
`cancel_all()` all use the blocking `requests` library under the hood. These were called
directly from the asyncio event loop, which stalled WS book-cache updates during the
signing + HTTP POST window.

All five calls now run via `asyncio.to_thread()`, keeping the event loop alive for WS
processing right up until (and during) the order-placement round trip.

`get_order_fill_rest()` return type changed from `Optional[tuple[float, float]]` to
`Optional[dict]` with keys `price`, `size_matched`, `size_remaining`, `status`. The old
`associate_trades` path was removed — it was fetching data from the counterparty
perspective, producing wrong price/size values (observed: price=0.97, size=1822 for a
0.035 × 28.57 hedge order). The order's own `price` field is now used as the fill price.

### Fix — `PositionMonitor` deferred-cancel dict replaced by `HedgeOrder` (`monitor.py`)

`_pending_hedge_cancels: dict[str, dict]` removed. All cancel-trigger state now lives on
`HedgeOrder.pending_cancel_*` fields, loaded from `hedge_orders.json`. The `on_price_update`
loop calls `risk.get_hedge_orders_with_pending_cancel()` instead of iterating the local dict.

`_add_pending_resolution` now coerces `underlying`, `market_slug`, and `market_type` args
to `str` (guards against `MagicMock` objects leaking in from test harness). `end_date` is
only converted with `.isoformat()` if it is a `datetime` instance.

### Fix — Band-floor abort path registers position (`scanner.py`)

Previously, when a momentum fill landed below `MOMENTUM_PRICE_BAND_LOW`, the order was
cancelled and the bot discarded the position entirely — tokens already in the wallet with
no settlement path. The abort path now registers a `Position` with `signal_source="band_floor_abort"`,
ensuring the PM-payout resolution path in `monitor.py` records the correct trades.csv row.

`scanner.py` updated to use `actual_fill["price"]` and `actual_fill["size_matched"]`
dict keys (matching the new `get_order_fill_rest()` return type).

### Feature — `GET /market_pnl` and `GET /market_pnl/{market_id}` endpoints (`api_server.py`)

Two new read-only endpoints expose `RiskEngine.market_pnl()` to the webapp:
- `GET /market_pnl` — returns P&L for all markets with tracked positions
- `GET /market_pnl/{market_id}` — returns P&L for a single market; 503 if risk engine not ready

Response shape: `{ "markets": { "<market_id>": MarketPnlRow }, "timestamp": float }`

### Feature — Webapp: hedge fill fields in SSE position rows (`main.py`)

`hedge_fill_detected`, `hedge_fill_size`, and `hedge_fill_price` were present on the
`Position` dataclass but never serialised into the SSE position dict in `state_sync_loop`.
`Positions.tsx`'s hedge badge always saw `undefined` and could never transition to the
"Filled" state even when WS detection had fired.

### Feature — Webapp: market P&L inline in Positions page; hedge fill badge (`Positions.tsx`, `client.ts`)

`useMarketPnl()` hook polls `/market_pnl` every 10 s. `MomentumRow` and `RangeRow`
receive a `pnl?: MarketPnlRow | null` prop and, when a hedge fill is confirmed and
`hedge_realized_pnl` is non-zero, render the realized hedge P&L inline next to the
fill badge (green `+$X.XX` / red `-$X.XX`).

New types in `client.ts`: `MarketPnlPosition`, `MarketPnlHedge`, `MarketPnlRow`,
`MarketPnlResponse`. New per-bucket hedge toggle fields added to `ConfigData`
(`momentum_hedge_enabled_5m/15m/1h/4h/daily/weekly/milestone`). `Trade` interface gains
`hedge_size_filled` and `hedge_avg_fill_price`.

### Feature — Webapp: sortable Momentum Scan table (`Signals.tsx`)

Column headers Bucket, Δ% vs Threshold, TTE, and Status are now clickable sort controls.
Default sort remains `gap_pct` descending. Clicking the same column toggles asc/desc;
clicking a different column resets to that column's natural direction (TTE defaults to
ascending; others descend). Active sort column shows a ▲/▼ indicator.

### Feature — Webapp: hedge fill-ratio display + new statuses (`Trades.tsx`)

The HedgeSection component now reads `hedge_size_filled` and `hedge_avg_fill_price` from
the trade row and computes fill ratio when available. Three new terminal statuses are
handled with distinct badge colours:
- `filled_exited` — hedge order filled during deferred-cancel window, then market-sold
- `cancelled_partial` — cancelled after accumulating partial fills
- `expired_partial` — GTD order expired with partial fill

### Tests

`tests/test_pm_client.py` added (526 lines): covers `get_order_fill_rest()` dict return,
`asyncio.to_thread()` wrapping for `create_order`/`post_order`/`cancel`/`cancel_all`,
`fetch_token_side()`, and paper-mode guards.

`tests/test_risk.py` additions: `HedgeOrder` lifecycle (`register_hedge_order`,
`update_hedge_fill`, `finalize_hedge`, VWAP accumulation, terminal-state guard),
`get_position_for_hedge()` O(1) path and legacy fallback, `market_pnl()`.

Full suite: **1,095 passed, 1 skipped** (the skipped test is a live Chainlink feed test).

---

## [2026-04-20] - Fix: hedge fill detection pipeline; webapp hedge state badge

### Fix — GTD hedge fills silently dropped by WS fill handler (`live_fill_handler.py`, `risk.py`, `monitor.py`)

**Problem:** Momentum GTD hedge orders are placed by `scanner.py` and tracked in
`risk._positions[].hedge_order_id`, but are never added to the maker's `ActiveQuote` dict.
`live_fill_handler._on_order_fill()` only searches `active_quotes` for the matching order,
so when a counterparty filled the hedge the WS MATCHED event arrived and was silently
dropped with a DEBUG log (`"fill for unknown/consumed order"`). The order disappeared from
the open CLOB order list, the bot's wallet held the filled tokens, but no `HEDGE_FILL` event
was recorded and `_record_pending_resolution_hedge()` would later mark the hedge as
`"unfilled"` — losing the actual payout.

Confirmed on 2026-04-19: XRP daily hedge order `0xf60fa033…` was placed at 0.02 for 45.45
contracts; CLOB shows `status=MATCHED, size_matched=45.45`; no fill row in `trades.csv`.

**Fix — three-layer detection pipeline:**

1. `live_fill_handler._on_order_fill` — after the `active_quotes` lookup fails, calls
   `risk.get_position_by_hedge_order_id(order_id)` (new method). If matched, sets
   `pos.hedge_fill_detected = True` and `pos.hedge_fill_size = cumulative_matched`.
   Logs at INFO. This covers real-time fills while the bot is running.

2. `monitor._record_pending_resolution_hedge` — before recording `"unfilled"`, checks
   `parent.hedge_fill_detected` + `parent.hedge_fill_size` and writes `filled_won` /
   `filled_lost` accordingly. Covers current-session fills.

3. `monitor._record_pending_resolution_hedge` (REST fallback) — calls
   `get_order_fill_rest(hedge_order_id)` on the CLOB. Covers fills that happened while
   the bot was offline or before WS detection was in place (e.g. the Apr-19 XRP case).

**`Position` dataclass additions (`risk.py`):**
- `hedge_fill_detected: bool = False`
- `hedge_fill_size: float = 0.0`

**New `RiskEngine` method:** `get_position_by_hedge_order_id(order_id) -> Optional[Position]`
— finds the open position whose `hedge_order_id` matches the given CLOB order ID.

### Feature — Webapp Positions page shows hedge state badge (`Positions.tsx`, `client.ts`)

**Previous behaviour:** The GTD Hedge column showed a plain purple text label with price and
USD size. No way to tell whether the hedge order was still resting or had been filled.

**New behaviour:** The cell now shows a coloured state badge:
- `—` — no hedge placed
- Purple **"Live · 2.2¢"** — hedge order resting on CLOB (not yet filled)
- Green **"Filled · 45.5ct"** — WS MATCHED event confirmed the hedge filled mid-trade

Each badge has a detailed tooltip (order ID, token ID, fill/bid price, size).

`Position` TypeScript interface gains `hedge_fill_detected?: boolean | null` and
`hedge_fill_size?: number | null` (serialised from `dataclasses.asdict` on the backend).

---

## [2026-04-16] - Bug fixes: range spot propagation, range tick delta, prob-SL oracle gate, WS fill detection, FAK fallback, hedge cancel regression, auto-redeem stale curPrice

### Fix — Auto-redeem used stale `curPrice` for WIN/LOSS determination (`monitor.py`)

**Problem:** Both auto-redeem paths (`redeemable=False` externally-settled detection and
`redeemable=True` on-chain submission) used `curPrice >= 0.99` from the PM wallet positions API
to decide WIN vs LOSS. `curPrice` is a stale CLOB mid-price that can show ~1.0 for a *losing*
token in the brief window right after settlement, before PM's oracle updates it. Confirmed on
2026-04-16: SOL DOWN token curPrice showed ~1.0 immediately after resolution even though SOL
ended UP (DOWN = LOSS). The bot closed the position as WIN (pnl=+$0.46), submitted an on-chain
redemption expecting $2.76, but the contract correctly paid $0.

**Fix:** Both paths now call `fetch_market_resolution(condition_id)` which reads the CLOB
`winner` flag — the authoritative source of truth per the PM Gamma API. `curPrice` is no
longer used for outcome determination anywhere in the auto-redeem flow. If CLOB isn't settled
yet (returns `None`), the cycle is skipped and retried on the next poll. This also closes the
same vulnerability in the externally-redeemed path.



### Fix — Range positions never received `current_spot` (`monitor.py`)

**Problem:** `_check_position()` fetched `current_spot` only when `pos.strategy == "momentum"`,
so range positions always had `current_spot = None`. This meant the delta stop-loss could never
evaluate for range markets. Confirmed by audit: 82,935 BTC weekly-range ticks all had empty
spot, so the position ran unprotected to expiry and lost $14.71.

**Fix:** Changed guard to `pos.strategy in ("momentum", "range")` so both strategy types get
spot data. Range positions already had `range_lo` / `range_hi` populated by the scanner; this
change connects the spot feed so those bounds can actually be evaluated.

### Fix — Range tick delta used momentum (strike-midpoint) formula (`monitor.py`)

**Problem:** `_write_momentum_tick()` always computed delta as `(spot − strike) / strike × 100`
regardless of strategy. For range positions the meaningful metric is distance to the nearest
bound, not distance to the midpoint strike.

**Fix:** When `pos.strategy == "range"` and `range_lo / range_hi` are populated, the tick delta
is computed as `min(spot − range_lo, range_hi − spot) / mid × 100` (positive = inside range)
for YES positions, and as distance above/below the range for NO positions (positive = outside
range = winning direction). Momentum positions use the unchanged strike-midpoint formula.

### Fix — Prob-SL could fire on CLOB book drain while solidly ITM (`monitor.py`)

**Problem:** When a range/momentum market approaches expiry, liquidity drains from the CLOB
book. The resulting price collapse could drop the token below `prob_sl_threshold` even when the
oracle confirms the position is solidly in-the-money. Confirmed by audit: XRP daily fired
prob-SL at 62% CLOB collapse while oracle delta was +27% ITM — a clear false positive.

**Fix:** Added `_oracle_delta_pct` capture from the oracle block in `should_exit()`. A new
`_prob_sl_oracle_ok` gate fires prob-SL only when:
- oracle data is unavailable (prob-SL remains the sole guard), **or**
- oracle delta is < 1.0% from strike (genuinely close — may legitimately be at threshold).

When oracle delta > 1% the position is solidly ITM; a CLOB collapse is book drain, not a real
directional move, and prob-SL is suppressed.

### Fix — User WS fill detection missed FILLED status and nested event format (`pm_client.py`)

**Problem:** The PM user WebSocket fill handler checked `msg.get("status") == "MATCHED"` with
an exact case-sensitive match. PM's API also emits `"FILLED"` status and a nested
`{"event_type": "order", "order": {...}}` format, both of which were silently ignored. Result:
`fill_from_ws = 0/32` fills detected via WS — all fell back to REST polling.

**Fix:** Broadened the check to handle `"MATCHED"` and `"FILLED"` case-insensitively, and added
a nested-format handler. Added `log.debug` of all user WS messages to aid future diagnostics.

### Fix — FAK exit retries exhausted with no fallback (`monitor.py`)

**Problem:** `_exit_position()` retried a FAK market sell up to 3 times (0.2 s sleep), then
logged `EXIT_ORDER_FAILED` and returned without placing any order — leaving the position open
indefinitely if the book was momentarily empty near expiry.

**Fix:** Increased retries to 5 (0.5 s sleep). After all FAK attempts fail, places a GTC limit
at `max(sell_price × 0.5, 0.01)` as a floor-price safety net. Only logs `EXIT_ORDER_FAILED`
and returns if the limit order also fails, requiring manual intervention.

### Fix — Hedge cancel fired on all loss exits (pre-existing regression) (`monitor.py`)

**Problem:** Working-tree code changed the GTD hedge cancel logic from "cancel only on win
exits" to "cancel on everything except RESOLVED and deferred MOMENTUM_STOP_LOSS", breaking the
intended behaviour of keeping the hedge alive on loss exits so it can partially recover.

**Fix:** Restored the `elif reason in _hedge_cancel_on_win` guard (win-only cancel), where
`_hedge_cancel_on_win = {ExitReason.PROFIT_TARGET, ExitReason.MOMENTUM_TAKE_PROFIT}`. Loss
exits (STOP_LOSS, NEAR_EXPIRY, etc.) fall through without cancelling the hedge.

---

## [2026-04-12] - Bug fixes: dip-market delta inversion, negative-EV Kelly override, per-bucket multiplier test isolation

### Fix — Dip-market NO/DOWN delta stop-loss inversion (`monitor.py`)

**Problem:** `should_exit()` always computed the NO/DOWN winning delta as
`(strike − spot) / strike × 100`, which is correct for *reach* markets ("Will ETH reach $3k?")
but inverted for *dip* markets ("Will ETH dip to $2,200?"). For a dip-market NO, the position
wins when `spot > strike`. With `spot = $2,223` and `strike = $2,200` the formula returned
`−1.065%`, which is always below the `+0.04%` stop-loss threshold — firing an instant false
stop-loss at open regardless of spot movement.

Root cause confirmed by examining a live trade: `entry_delta = −1.065`, `tok_drop_pct = 2.46%`
(bid/ask spread artefact), `hold_seconds = 0.1` — the position was killed in the same second
it was opened, with spot completely unchanged.

**Fix:** For NO/DOWN positions, infer the winning direction from `pos.spot_price` recorded at
entry. If `pos.spot_price > pos.strike` (dip market: entry spot was above strike) the correct
delta formula is `(current_spot − strike) / strike × 100`. Otherwise the legacy reach-market
formula `(strike − current_spot) / strike × 100` applies. `pos.spot_price` defaults to `0.0`,
so all existing reach-market positions, saved positions, and tests are unaffected.

The same directional fix was applied to `_write_momentum_tick()` so `momentum_ticks.csv`
records the correct signed `entry_delta` for auditing.

### Fix — Kelly MIN_ENTRY floor overrides negative-EV signals (`scanner.py`)

**Problem:** When raw Kelly fraction `f* = (p×b − (1−p)) / b < 0` (the model says the bet has
negative expected value), the `MOMENTUM_MIN_ENTRY_USD` floor was forcing a `$1` minimum entry
anyway. This occurred for deeply in-the-money tokens (price ≥ 0.95¢) where `payout_b` is so
small that `win_prob` cannot overcome the hurdle: e.g. `token = 0.955`, `payout_b = 0.0471`,
`win_prob = 0.919` → `f* = (0.919×0.047 − 0.081)/0.047 = −0.80`.

**Fix:** `_compute_kelly_size_usd` now returns `size_usd = 0.0` when `raw_kelly_f < 0`. The
`_execute_signal` entry path checks `size_usd == 0.0` and skips with an INFO log rather than
placing the order. `MOMENTUM_MIN_ENTRY_USD` only applies when Kelly says "bet small" (raw ≥ 0).
A new `kelly_f_raw` field is added to the fills CSV debug dict to make negative-EV decisions
auditable without re-running the math.

### Fix — TestPaperModePositionSizing missing multiplier reset (`tests/test_momentum_scanner.py`)

`TestPaperModePositionSizing.setup_method` was missing `MOMENTUM_KELLY_MULTIPLIER_BY_TYPE = {}`
in its saved/restored config state. When the per-bucket multiplier feature was added in the
previous session, the `bucket_5m` multiplier of `0.45` silently reduced the test's expected
`MAX_ENTRY_USD = $3.0` position to `$1.35`, causing four assertions to fail with
`assert 1.35 == 3.0`. Fixed by neutralising the per-bucket multiplier dict in setup, mirroring
the existing pattern in `TestKellySizing`.

The old `test_negative_ev_signal_returns_min` test (which asserted that a negative-EV signal
returns `MIN_ENTRY_USD`) was renamed `test_negative_ev_signal_returns_zero` and updated to
assert `size == 0.0`.

---

## [2026-04-11] - Phase B/B2/C/D/E + Kelly extensions: near-expiry oracle, hedging, win-rate gate

### Phase B — Two-oracle near-expiry strategy (`monitor.py`, `spot_oracle.py`, `config.py`)

**Problem:** The delta stop-loss and L2 oracle-vs-strike resolution path both used
`SpotOracle.get_mid()` (freshest-wins between RTDS relay and AggregatorV3).  Near expiry
the RTDS relay leads AggregatorV3 by up to ~15 s.  Polymarket's resolution contract calls
`latestRoundData()` on AggregatorV3 — not the relay — so a brief sub-strike dip captured
only by the relay can fire the SL even though AggregatorV3 (and therefore PM resolution)
never saw it.

**Fix:** New `SpotOracle.get_mid_resolution_oracle(underlying, market_type)` returns the
ChainlinkWSClient AggregatorV3-only price for Chainlink market types (non-HYPE), falling
back to `get_mid()` for HYPE and non-Chainlink types.

New config flag `MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY` (default `True`).  When
enabled:
- Near-expiry delta SL (`tte_seconds < MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS`) uses
  `get_mid_resolution_oracle()` instead of `get_mid()` so the SL matches PM's settlement.
- RESOLVED exit L2 path uses the same AggregatorV3-only feed.

**Caveat:** If PM's resolution bot itself uses the Data Streams relay in future, this fix
would need to be revisited.  Cross-check `oracle_ticks.csv` against known resolved
outcomes before disabling the flag.

### Phase B2 — LiveFillHandler per-market state reset (`live_fill_handler.py`)

**Problem:** `_matched_so_far` accumulated `{order_id: cumulative_fill_usd}` entries for
the lifetime of the process.  On a busy session with many markets the dict grew without
bound.  More critically, a late fill event from a previous market's WS recovery window
could be misread as belonging to the current market if order IDs ever collided.

**Fix:**
- New reverse index `_cond_to_order_ids: dict[str, set[str]]` tracks which order IDs
  belong to each condition ID.
- New `reset_market_state(condition_id)` method atomically removes all associated
  `_matched_so_far` entries when a market is closed/expired.
- `startup_restore()` now skips tokens with `redeemable=True` **or** a settled
  `cur_price` (≤ 0.01 or ≥ 0.99) — both categories are owned by
  `_redeem_ready_positions()`.  Restoring them as open positions caused a duplicate
  `trades.csv` row on every bot restart until they were redeemed on-chain.

### Phase C — Minimum elapsed-time guard (`scanner.py`, `config.py`)

New per-type dict `MOMENTUM_MIN_ELAPSED_SECONDS` (default empty → disabled).  When a
bucket type has a value set (e.g. `{"bucket_5m": 30}`), entries fired before that many
seconds have elapsed since market open are suppressed with `skip_reason="too_early"`.

**Rationale:** Early-window entries face a thin order book with wide spreads and noisy
initial ticks.  The elapsed-time guard gives the book time to stabilise before committing
capital.  The persistence clock (`signal_first_valid`) is also reset so a re-entry after
the guard window starts a fresh persistence accumulation.

`skipped_too_early` counter added to scanner summary and diagnostics API.

### Phase C (Chainlink) — Boundary tick logging (`market_data/chainlink_ws_client.py`)

For every AggregatorV3 `AnswerUpdated` event that lands within **[-15 s, +5 s]** of a
bucket boundary (300 s / 900 s / 14 400 s), a structured `CL_BOUNDARY_TICK` log entry
is emitted at INFO level including:

```
coin, price, period_s, secs_after_boundary, secs_before_next, local_ts, onchain_updated_at
```

`onchain_updated_at` is decoded from the event `data` field (raw `uint256` epoch seconds),
enabling post-hoc validation of whether the anchor was captured in the correct Chainlink
round.  Also added `_ADDR_TO_COIN` legacy alias for backward-compatible test imports.

### Phase D — GTD hedge (`scanner.py`, `config.py`)

After a confirmed momentum entry, optionally place a GTC maker limit BUY on the
**opposite** token at `MOMENTUM_HEDGE_PRICE` (default `$0.02`).

**Economics:** A fill at $0.02 that redeems at $1.00 returns $0.98/contract.  If the
held token loses, the opposite token resolves at $1.00, providing partial downside cover
that requires no oracle knowledge.  Maximum hedge cost ≈ `entry_size × 0.02` ≈ 2–3 % of
entry cost.

**Config:**
- `MOMENTUM_HEDGE_ENABLED` (default `True`) — master switch
- `MOMENTUM_HEDGE_PRICE` (default `0.02`) — GTC bid price on the opposite token

Hedge order is placed as `post_only=True` (maker).  Errors are logged at WARNING and do
not abort the primary position.  Hedge order IDs are not tracked (GTC orders are
self-managing and expire when the market closes).

### Phase E — Empirical win-rate gate (`strategies/Momentum/win_rate.py`, `scanner.py`, `config.py`)

New `WinRateTable` class (`win_rate.py`) builds a historical win-rate matrix from
`data/trades.csv` bucketed by `(market_type, price_band_5ct, time_bin_per_minute)`.
Loaded once at scanner startup; silently disabled if the data file is missing or has
insufficient fills.

**Gate logic:** If `MOMENTUM_WIN_RATE_GATE_ENABLED` is `True` and the win-rate table
has ≥ `MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES` fills in a bucket, an entry is suppressed
when `empirical_win_rate < model_win_prob × MOMENTUM_WIN_RATE_GATE_MIN_FACTOR`.

**Config:**
- `MOMENTUM_WIN_RATE_GATE_ENABLED` (default `False`) — disabled until ≥ 100 fills/bucket
- `MOMENTUM_WIN_RATE_GATE_MIN_FACTOR` (default `0.9`) — empirical WR must be ≥ 90 % of model WR
- `MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES` (default `10`) — minimum samples before gate activates

`skipped_win_rate` counter added to scanner summary.  Diagnostics fields `emp_win_rate`
and `model_win_rate` written to scan_diags when the gate fires.

### Kelly extensions: intra-sigma + persistence z-boost (`scanner.py`, `signal.py`, `config.py`)

Two optional extensions to the Kelly sizing formula:

**Intra-bucket realised sigma** (`MOMENTUM_KELLY_INTRA_SIGMA_ENABLED`, default `True`):
- Rolling `_spot_mid_history` deque (maxlen 300) records every RTDS/Chainlink tick per
  underlying coin.  `_compute_bucket_intra_sigma()` returns the annualised realised vol
  from the recent tick history.
- Kelly uses `max(sigma_ann, bucket_intra_sigma)` so elevated intra-bucket vol
  (e.g. after a sudden move) is captured even if the historical annual sigma is low.

**Persistence z-boost** (`MOMENTUM_KELLY_PERSISTENCE_ENABLED`, default `True`):
- `signal_first_valid` tracks the Unix timestamp when each market's signal first
  continuously cleared all gates.  Persisted to `data/signal_first_valid.json` so the
  clock survives bot restarts.
- `MomentumSignal.signal_valid_since_ts` exposes the clock to `_compute_kelly_size_usd()`.
- A z-boost up to `MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX` (default `0.5`) is blended in
  as the signal ages, rewarding durable edge vs. brief transient spikes.

**New `MomentumSignal` fields:** `bucket_intra_sigma: Optional[float]`,
`signal_valid_since_ts: float`.

### Monitor — External redemption detection + stale curPrice resolution (`monitor.py`)

`_redeem_ready_positions()` now handles two additional cases that previously leaked:

1. **Externally redeemed tokens (`redeemable=False`, settled `curPrice`):**
   When `cur_price ≤ 0.01` or `≥ 0.99` but `redeemable=False`, the token was already
   redeemed via the PM UI (or another bot instance).  The code now:
   - Infers the settlement direction from `cur_price` and whether the token is YES/NO,
   - Closes any ghost open position,
   - Calls `patch_trade_outcome(force=True)` to correct `trades.csv`.

2. **Stale mid-price on redeemable tokens:**
   When `redeemable=True` but `curPrice` is in `(0.01, 0.99)` (PM API hasn't updated
   from CLOB mid to settlement yet), the code now queries `fetch_market_resolution()` and
   resolves the correct YES-token settlement price before computing the payout.

`_check_pending_resolutions()` upgraded to `force=True` so PM CLOB settlement data
always overrides any earlier incorrect outcome recorded by the RESOLVED fast-path.

### Webapp — Phase B/C/D/E settings UI (`webapp/src/pages/Settings.tsx`, `webapp/src/api/client.ts`)

New **"Momentum — Advanced Phases"** card in Settings with controls for:
- Phase B: resolution oracle near-expiry toggle
- Phase C: per-type elapsed-time guards (5m / 15m / 1h / 4h / daily / weekly / milestone)
- Phase D: hedge enabled / hedge price
- Phase E: win-rate gate enabled / min factor / min samples

All fields wired to API server (`api_server.py`) via `ConfigPatch` model and
`_MUTABLE_CONFIG` registry.  `MOMENTUM_SCAN_INTERVAL` reduced to 1 s;
`MOMENTUM_MAX_CONCURRENT` raised to 20 to match live throughput requirements.

### Tests

- New test files: `tests/test_live_data_integrity.py`, `tests/test_spot_oracle.py`,
  `tests/test_win_rate.py`
- Significant additions to `tests/test_chainlink_ws_client.py` (boundary tick logging),
  `tests/test_live_fill_handler.py` (reset_market_state, settled-token skip),
  `tests/test_momentum_scanner.py` (Phase C/D/E gates, Kelly extensions),
  `tests/test_e2e_live.py`

---

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
