# Perp Hyper Arb

A semi-automated crypto trading bot that runs three complementary strategies: market making on Polymarket, mispricing detection against Kalshi, and momentum-based taker entries on high-probability buckets. Net delta exposure is hedged via Hyperliquid perpetuals.

---

## What It Does

**Strategy 1 — Market Making:** Posts two-sided YES/NO quotes on Polymarket crypto bucket markets, earning the spread plus maker rebates. Every significant inventory build is automatically hedged via a Hyperliquid perp trade in the opposite direction, keeping the book close to market-neutral. All pricing decisions (reprice triggers, hedge sizing, vol estimation) use **Polymarket RTDS** spot prices — Hyperliquid is used only for order execution.

**Strategy 2 — Mispricing Scanner:** Scans Polymarket milestone markets against matching Kalshi markets. When both venues list the same crypto event (e.g. "Will BTC close above $90k on March 31?"), any price divergence above the fee hurdle is a candidate trade. Deribit N(d₂) can optionally be used as a second-layer confirmation signal. Spot price for Black-Scholes is sourced from **Polymarket RTDS**.

**Strategy 3 — Momentum Scanner:** Runs a price-confirmation taker strategy that enters high-probability contracts when Polymarket token prices and spot movement jointly confirm momentum. It supports volatility-aware thresholds, per-market cooldowns, fractional Kelly position sizing (with per-bucket dampeners and a negative-EV guard), stop-loss / take-profit exits with correct directional delta for both reach-market and dip-market NO positions, a near-expiry protective exit, and an optional range-market sub-strategy for "Will BTC be between $X and $Y?" markets.

**Strategy 5 — Opening Neutral:** Simultaneously buys the YES and NO token of the same Up/Down bucket market within a short window of market open (default 120 s). When both FAK legs fill at a combined cost ≤ $1.00 the pair is guaranteed-profitable at resolution regardless of direction — the winning token pays out $1.00. When only one leg fills (the exchange killed the other FAK), the surviving leg is either promoted to a standard momentum position or immediately taker-exited. A price skew filter (`[0.44, 0.56]`) ensures both sides are priced near 50¢, keeping the strategy truly neutral. Full spec: [strategies/OpeningNeutral/PLAN.md](strategies/OpeningNeutral/PLAN.md).

All strategies start in **paper trading mode** (no real funds). Switching to live is a single config change.

---

## Architecture

```
Perp_Hyper_Arb/
├── main.py                   # Entry point — asyncio event loop, wires all tasks
├── config.py                 # All constants + get_effective_config()
├── config_overrides.json     # Runtime overrides (persists across restarts)
│
├── api_server.py             # FastAPI REST server on port 8080
├── risk.py                   # Position sizing, exposure limits, P&L tracking
├── monitor.py                # Position monitor (closes on expiry, loss, profit target)
├── fill_simulator.py         # Paper trading fill simulator
├── live_fill_handler.py      # Live fill handler + WS reconciliation
├── agent.py                  # AI signal evaluation + auto-execute / shadow-mode
├── logger.py                 # Structured logging + Telegram alerts
├── launcher.py               # Helper launcher script
│
├── market_data/
│   ├── pm_client.py          # Polymarket CLOB REST + WS + Gamma API
│   ├── hl_client.py          # Hyperliquid REST + WS (execution only)
│   ├── rtds_client.py        # RTDS WS + on-chain Chainlink oracle (Polygon WSS)
│   ├── kalshi_client.py      # Kalshi REST (read-only, no auth required)
│   └── deribit.py            # Deribit implied vol / N(d₂) data
│
├── strategies/
│   ├── maker/                # Strategy 1: quoting, repricing, inventory skew, hedge
│   ├── mispricing/           # Strategy 2: signal generation, Kalshi + N(d₂) filters
│   ├── Momentum/             # Strategy 3: momentum scanner + taker execution
│   │   ├── market_utils.py   #   shared market-classification + strike-extraction helpers
│   │   └── ...
│   └── OpeningNeutral/       # Strategy 5: simultaneous YES+NO FAK entry at market open
│       └── scanner.py
│
├── tests/                    # Pytest suite
├── data/                     # CSV trade logs, paper trade records, analysis scripts (data/_*.py)
│
└── webapp/                   # Vite + React monitoring dashboard (port 5173)
    └── src/pages/            # Dashboard, Trades, Positions, Performance,
                              #   Signals, Risk, Markets, Fills, Logs, Settings
```

**Runtime task graph (asyncio):**

```
pm_client.run()           ← Polymarket WS + heartbeat
hl_client.run()           ← Hyperliquid WS + dead-man's switch (execution only)
rtds_client.start()       ← Polymarket RTDS WS (crypto_prices + crypto_prices_chainlink topics)
                            + Polygon WSS on-chain Chainlink oracle (AggregatorV3 AnswerUpdated)
maker_strategy.start()    ← quoting sweep + hedge debounce (reprice trigger = RTDS tick)
momentum_scanner.start()  ← scan every 5-10 s + event-driven entry (PM ticks + RTDS/Chainlink ticks)
opening_neutral_scanner.start() ← polls for market opens; simultaneous YES+NO FAK entry (if OPENING_NEUTRAL_ENABLED)
mispricing_scanner.start()← scan every 60 s (spot price from RTDS)
agent_loop()              ← consumes signal queue from scanner
api_server                ← FastAPI REST for webapp
state_sync_loop()         ← pushes bot state to API layer (spot_mid routed by market type)
```

The momentum scanner wakes immediately on **either** a PM CLOB price tick or a spot tick from RTDS **or** Chainlink (whichever feed the market resolves against).
The position monitor evaluates exit conditions for **all open positions** on every PM WS tick
and for momentum positions on every RTDS/Chainlink tick, so a spot move through the stop-loss strike is
detected within one WebSocket round-trip (~100–500 ms) rather than any configured poll delay.

**Spot price sources:**

| Source | Coins | Format | Used for |
|--------|-------|--------|----------|
| RTDS WS `crypto_prices` | BTC, ETH, SOL, XRP, BNB, DOGE | RTDS usdt-pair format (`btcusdt`, …) | 1h, daily, weekly markets (`get_mid` / `get_spot`) |
| RTDS WS `crypto_prices_chainlink` | HYPE (+ fallback for others) | Chainlink slash-format (`btc/usd`, …) | HYPE oracle; bridge between on-chain heartbeats |
| **On-chain Chainlink** (Polygon WSS) | BTC, ETH, SOL, XRP, BNB, DOGE | AggregatorV3 `AnswerUpdated` events | **Primary oracle** for 5m/15m/4h market resolution (`get_mid_chainlink`) |

**Price source split:**

| Component | Polymarket RTDS | Hyperliquid |
|-----------|----------------|-------------|
| Reprice trigger (maker) | ✓ spot ticks | ✗ |
| Hedge sizing (maker) | ✓ `get_mid()` | ✗ |
| Rolling realized vol (vol_fetcher) | ✓ price feed | ✗ |
| Black-Scholes spot S (mispricing) | ✓ `get_mid()` | ✗ |
| Momentum delta SL / scanner entry | ✓ `get_mid()` (RTDS) or `get_mid_chainlink()` (on-chain primary / RTDS WS fallback) | ✗ |
| Exit spot recorded in trades.csv | ✓ oracle-routed at exit time | ✗ |
| Hedge order placement | ✗ | ✓ |
| `get_bbo()` for slippage measurement | ✗ | ✓ |

---

## Quick Start

### Prerequisites

- Python 3.10 (exact version — `hyperliquid-python-sdk` has issues on 3.11+)
- Node.js 18+ (for the webapp)

### Backend

```bash
# 1. Create and activate virtualenv
python3.10 -m venv venv
source venv/bin/activate        # Linux/macOS
venv\Scripts\activate           # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
# Edit .env with your keys (see Environment Variables below)

# 4. Run (paper trading by default)
python main.py

# Live trading (real funds — be careful)
# Set PAPER_TRADING=false in config_overrides.json via the Settings page,
# or set the env var:
PAPER_TRADING=false python main.py
```

### Webapp

```bash
cd webapp
npm install
npm run dev        # dev server on http://localhost:5173
```

The webapp connects to the API at `http://localhost:8080` by default. Override with:
```
VITE_API_URL=http://your-server:8080
```
in `webapp/.env.local`.

---

## Environment Variables

Create a `.env` file at the project root (see `.env.example`):

```env
# Polymarket
POLY_PRIVATE_KEY=0x...       # Polygon EOA private key (funded with USDC.e on Polygon)
POLY_FUNDER=0x...            # Optional: separate funder wallet address

# Hyperliquid
HL_ADDRESS=0x...             # Hyperliquid main wallet address
HL_SECRET_KEY=0x...          # Hyperliquid API wallet private key (not master key)

# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# API server
API_PORT=8080                # Default: 8080
API_SECRET=...               # Bearer token for mutating endpoints; leave empty to disable auth
API_CORS_ORIGINS=http://localhost:5173  # Allowed CORS origins
```

---

## Configuration

All parameters live in `config.py` as module-level constants. Any parameter can be overridden at runtime via two mechanisms:

1. **Settings page (webapp):** Sends `POST /config` and saves to `config_overrides.json`
2. **Direct edit:** Edit `config_overrides.json` (changes take effect on next restart or `POST /config/reload`)

To see the live effective configuration (defaults merged with overrides):

```
GET /config/effective
```

Key parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PAPER_TRADING` | `True` | Paper mode — no real orders placed |
| `STRATEGY_MAKER_ENABLED` | `False` | Enable the market making strategy |
| `STRATEGY_MISPRICING_ENABLED` | `False` | Enable the mispricing scanner |
| `STRATEGY_MOMENTUM_ENABLED` | `False` | Enable the momentum scanner |
| `MOMENTUM_RANGE_ENABLED` | `False` | Include range ("between $X and $Y") markets in the momentum scan |
| `OPENING_NEUTRAL_ENABLED` | `False` | Enable the opening neutral strategy (Strategy 5) |
| `OPENING_NEUTRAL_DRY_RUN` | `True` | Signals and pair tracking run normally; no real orders placed |
| `OPENING_NEUTRAL_SIZE_USD` | `1.0` | USDC notional per leg (YES and NO each get this amount) |
| `OPENING_NEUTRAL_ONE_LEG_FALLBACK` | `"keep_as_momentum"` | Action when only one FAK leg fills: `"keep_as_momentum"` or `"exit_immediately"` |
| `OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM` | `False` | When `True`, winner leg is promoted to a momentum position for delta-SL/TP management; when `False` holds to resolution as `opening_neutral` |
| `OPENING_NEUTRAL_MARKET_WINDOW_SECS` | `60` | Window around market open within which entry is attempted (renamed from `OPENING_NEUTRAL_ENTRY_WINDOW_SECS`) |
| `HEDGE_THRESHOLD_USD` | `100` | Net inventory before a perp hedge fires (USD) |
| `MOMENTUM_SCAN_INTERVAL` | `10` | Seconds between momentum scan passes |
| `MAX_QUOTE_AGE_SECONDS` | `30` | Backstop reprice interval |
| `MAKER_EXIT_HOURS` | `0.0` | Non-bucket time-stop (hours before expiry); `0.0` disables this gate |
| `MAX_PM_EXPOSURE_PER_MARKET` | `500` | Max USD deployed per market |
| `MAX_TOTAL_PM_EXPOSURE` | `5000` | Total PM exposure cap (USD) |

---

## Recent Changes

### 2026-05-03 — M-series momentum upgrades; Opening Neutral promote flag; startup smoke tests

- **`FundingRateCache`** (`market_data/funding_rate_cache.py`): push-fed HL funding rate module updated by `HLClient` WebSocket `webData2` handler. No polling; staleness is measured from last push timestamp.
- **`OracleTickTracker`** (`market_data/oracle_tick_tracker.py`): per-coin oracle analytics tracking EWMA up-fraction, TWAP deviation, and volatility regime (`HIGH`/`LOW`/`UNKNOWN`). Integrated into `monitor.py` and `MomentumScanner`.
- **Momentum scanner improvements**: new fill CSV columns (`funding_rate`, `yes_depth_share`, `hour_utc`, `effective_z`, `funding_gate_applied`, `twap_dev_bps`, `vol_regime`) capture entry context for post-trade analysis. New `MOMENTUM_UPFRAC_EXIT` exit reason fires when EWMA up-fraction drops below threshold for N consecutive windows.
- **Opening Neutral `OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM`**: new flag (default `False`) controls whether the winner leg is handed to `PositionMonitor` as a momentum position or simply held to resolution. Default-off means the winner collects full payout without delta-SL risk.
- **Config rename**: `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` → `OPENING_NEUTRAL_MARKET_WINDOW_SECS`.
- **New strategy docs**: `OpeningNeutralStrategy.md` (concise spec), `OpenStrategy.md` (new Open Market Entry strategy), `strategy_update.md` (data-driven recommendations from 681-window analysis).
- **Stale code removed**: `MOMENTUM_HEDGE_*` branch and `_sweep_hedges()` from `fill_simulator.py`; all `MOMENTUM_HEDGE_*` / `MOMENTUM_WIN_RATE_GATE_*` / `MOMENTUM_KELLY_PERSISTENCE_*` entries removed from `api_server.py` mutable config.
- **Bug fix** (`main.py`): `logger.info/error` → `log.info/error` in Phase 1 pipeline startup (NameError at startup when Phase 1 modules failed to import).
- **API `/pnl` and `/performance`**: migrated from `_load_trades_csv()` to `_load_acct_ledger_trades()` to reflect current accounting ledger.
- **Tests**: 16-test startup smoke suite added (`tests/test_startup_smoke.py`) including a `TestConfigAudit` that catches stale `config.ATTR` references at CI time. Stale `test_hedge_sizing.py` and `test_hedge_sweep.py` deleted. All 27 `test_opening_neutral.py` tests passing.

### 2026-04-29 — Strategy 5 (Opening Neutral); CLOB v2 migration; hedge SL fix; venv auto-detection

- **Strategy 5 — Opening Neutral** (`strategies/OpeningNeutral/scanner.py`, `config.py`, `main.py`, `api_server.py`): Simultaneous YES+NO FAK entry at bucket market open. Both legs combined cost ≤ $1.00 guarantees profit at resolution regardless of direction. One-leg fallback: promote to momentum or taker-exit. 14 new `OPENING_NEUTRAL_*` config params. `neutral_pair_id` on `Position`. `GET /opening_neutral/status` endpoint. Opening-neutral conflict guard added to Momentum scanner.
- **CLOB v2 migration** (`pm_client.py`, `monitor.py`, `api_server.py`): Fully migrated to `py_clob_client_v2`. FAK path uses `MarketOrderArgs` / `create_market_order`. `cancel` → `cancel_order(OrderPayload(...))`. `get_orders` → `get_open_orders`. Redeem endpoints now use `_get_contract_config(137)` (removed broken `get_conditional_address()` / `get_collateral_address()` calls).
- **Hedge SL suppression fix** (`monitor.py`): `_hedge_active` now checks `size_filled > 0`; an unfilled or cancelled hedge no longer suppresses stop-losses on a naked position.
- **Launcher venv auto-detection** (`launcher.py`, `main.py`): `launcher.py` resolves the project `.venv` Python automatically; `main.py` venv guard auto-relaunches with the correct interpreter when `py_clob_client_v2` is missing.
- **`pm_client.py` stability**: Gamma slug-fetch exceptions now skip the failed slug rather than aborting the full refresh. Multi-strategy token registration via `register_for_book_updates(owner=...)` prevents strategies from overwriting each other's WS subscriptions. Order size rounded to 2dp for CLOB API compliance. `fetch_market_resolution` returns `None` instead of falling back to the `price` field when winner flags are absent.
- **Tests**: 1,569 collected; updated throughout for v2 (`py_clob_client_v2` imports, `create_market_order`, `cancel_order(OrderPayload(...))`, stale mocks removed).

### 2026-04-21 — HedgeOrder lifecycle entity; async CLOB I/O; market_pnl API; sortable Signals table

- **`HedgeOrder` entity** (`risk.py`): full FIX-style lifecycle tracking for GTD hedge orders — `OPEN → PARTIALLY_FILLED → FILLED/CANCELLED/EXPIRED` with per-fill history, VWAP accumulation, and deferred-cancel state. Persisted to `data/hedge_orders.json`. Replaces scattered `Position` fields and the transient `_pending_hedge_cancels` dict in `PositionMonitor`.
- **`market_pnl(market_id)`** (`risk.py`): new `RiskEngine` method returning a JSON-serializable P&L snapshot (realized + unrealised + hedge) for all positions/hedges in a market. Exposed via `GET /market_pnl` and `GET /market_pnl/{market_id}`.
- **Async CLOB I/O** (`pm_client.py`): `create_order()`, `post_order()`, `create_market_order()`, `cancel()`, and `cancel_all()` now run in `asyncio.to_thread()`, eliminating event-loop stalls during order placement. `get_order_fill_rest()` returns a `dict` instead of a tuple; the `associate_trades` fetch path removed (was returning counterparty-perspective prices).
- **Band-floor abort fix** (`scanner.py`): fill prices below `MOMENTUM_PRICE_BAND_LOW` now register the position for PM-payout settlement instead of silently discarding it.
- **Trades.csv additive migration** (`risk.py`): new `hedge_size_filled` and `hedge_avg_fill_price` columns; `_ensure_csv()` migrates existing files in-place when the change is purely additive.
- **Webapp — Positions**: `hedge_fill_detected/size/price` now serialised in SSE rows; hedge badge correctly transitions to "Filled"; market P&L rendered inline via `useMarketPnl()` hook.
- **Webapp — Signals**: Momentum Scan table columns (Bucket, Δ% vs Threshold, TTE, Status) are now clickable sort controls.
- **Webapp — Trades**: `HedgeSection` shows fill ratio and handles `filled_exited`, `cancelled_partial`, `expired_partial` statuses with distinct badge colours.
- **Tests**: 1,095 passing; `test_pm_client.py` added (526 lines), new risk/HedgeOrder lifecycle tests.

### 2026-04-12 — Dip-market delta fix, Kelly negative-EV guard, per-bucket multiplier

- **Dip-market NO delta direction fix** (`monitor.py`): `should_exit()` and `_write_momentum_tick()` now infer the winning direction for NO/DOWN positions from `pos.spot_price` recorded at entry. If `entry_spot > strike` (the "Will ETH dip to $X?" flavour), the delta formula is `(spot − strike) / strike × 100` rather than the inverted reach-market version. This fixes instant false stop-losses that fired in the same second as entry for any dip-market NO.
- **Kelly negative-EV guard** (`scanner.py`): `_compute_kelly_size_usd` now returns `0.0` when `raw_kelly_f < 0`. The `_execute_signal` entry path skips the order entirely. `MOMENTUM_MIN_ENTRY_USD` does not override negative-EV signals. A `kelly_f_raw` field added to debug CSV/logs for auditability.
- **Per-bucket Kelly multiplier** (`MOMENTUM_KELLY_MULTIPLIER_BY_TYPE`): structural dampener applied after fractional Kelly — `{5m: 0.45, 15m: 0.70, 1h: 0.90, 4h+: 1.00}`. Reduces over-sizing in noisy short-bucket markets.
- **Kelly TTE floor** (`MOMENTUM_KELLY_MIN_TTE_SECONDS = 30`): Kelly uses `max(tte, 30)` as effective TTE, preventing sigma_tau collapse near expiry from inflating `win_prob` to ~1.0 on every near-expiry signal.

### 2026-05 — Per-coin config + delta SL hysteresis

- **Per-coin stop-loss overrides** (`MOMENTUM_DELTA_SL_PCT_BY_COIN`): each coin can now have a tailored delta stop-loss threshold instead of the global default. Higher-IV assets (DOGE, HYPE) use wider stops; BTC/ETH use tighter ones. Falls back to `MOMENTUM_DELTA_STOP_LOSS_PCT` when the coin is not listed.
- **Per-coin entry floor** (`MOMENTUM_MIN_DELTA_PCT_BY_COIN`): same pattern for the absolute minimum entry gap. Provides per-coin safety insurance during low-vol or oracle-lag conditions.
- **Delta SL hysteresis** (`MOMENTUM_DELTA_SL_MIN_TICKS = 2`): the stop-loss now only fires after 2 consecutive below-threshold oracle ticks, preventing single-tick noise from triggering premature exits.
- Both per-coin dicts are exposed via `GET /config` and `PATCH /config` (webapp Settings page includes dedicated per-coin grid sections for both stop-loss and entry floor).
- Default calibrated values are pre-populated in `config_overrides.json`.
- Analysis scripts moved to `data/` (prefixed `_*.py`).

### 2026-04-07 — RTDS Chainlink routing
- RTDS Chainlink routing: 5m / 15m / 4h markets now route to the RTDS `crypto_prices_chainlink` relay by default (production constraint: ChainlinkWSClient eth_subscribe on public RPCs is not reliable). This ensures sub-second Chainlink-relayed prices are used for short-bucket oracle resolution.
- RTDS coverage expanded: `crypto_prices_chainlink` mapping was extended from HYPE-only to include BTC/ETH/SOL/XRP/BNB/DOGE (Polymarket relay). The `RTDSClient` now exposes `get_chainlink_spot(coin)` for all supported coins.
- Chainlink on-chain address fix: the Polygon AggregatorV3 BNB contract address was corrected to the official `0x82a6c4AF830caa6c97bb504425f6A66165C2c26e`.
- `SpotOracle` routing simplified: short-bucket markets (5m/15m/4h) use RTDS chainlink as the primary source; HYPE still races with Chainlink Streams when configured.
- New comparison script: `data/_compare_chainlink_sources.py` compares RTDS `crypto_prices_chainlink` vs direct AggregatorV3 HTTP polling (helpful for audit and latency checks).

These changes were made to eliminate silent HTTP-polling fallbacks and ensure the production path uses the RTDS Chainlink relay (institutional-grade, no HTTP polling requirement). See `market_data/rtds_client.py` and `market_data/spot_oracle.py` for the routing logic.
| `KALSHI_ENABLED` | `True` | Require Kalshi confirmation for mispricing signals |

Full parameter reference: [MAKER_STRATEGY.md — Appendix A](MAKER_STRATEGY.md#appendix-a--configuration-reference)

---

## API Reference

The API server runs on port 8080. Read-only endpoints are open; mutating endpoints require `Authorization: Bearer <API_SECRET>` when `API_SECRET` is set.

### Read-only

| Endpoint | Description |
|----------|-------------|
| `GET /health` | System health (WS status, heartbeat, adversity flags) |
| `GET /config` | All config parameters and their current values |
| `GET /config/effective` | Live effective config (defaults + overrides merged) |
| `GET /bot` | Bot status (active / paused) |
| `GET /positions` | Open positions (bot-tracked) |
| `GET /positions/live` | Live PM wallet + HL positions, reconciled |
| `GET /trades` | Trade history from `data/trades.csv` |
| `GET /orders` | Live PM wallet open orders |
| `GET /pnl` | Realized P&L summary |
| `GET /performance` | Detailed analytics by strategy / underlying / market type |
| `GET /risk` | Exposure and risk metrics |
| `GET /markets` | Monitored Polymarket markets |
| `GET /signals` | Mispricing signal history |
| `GET /momentum/signals` | Momentum signal history |
| `GET /momentum/diagnostics` | Momentum scanner diagnostics and skip reasons |
| `GET /opening_neutral/status` | Opening Neutral strategy status: enabled, dry_run, active pairs, recent scan signals |
| `GET /maker/quotes` | Active resting quotes |
| `GET /maker/signals` | Maker signal evaluation history |
| `GET /maker/capital` | Capital allocation per market |
| `GET /maker/inventory` | Per-coin inventory and hedge status |
| `GET /hedge-quality` | Hedge execution quality (rolling slippage) |
| `GET /funding` | Hyperliquid funding rates |
| `GET /logs` | Recent log entries |
| `GET /logs/errors` | Long-lived WARNING/ERROR log buffer |
| `GET /proxy/polymarket/events` | Backend proxy for Polymarket event slugs |
| `GET /fills` | Paper fill history |
| `GET /market_pnl` | Combined realized + hedge P&L for all tracked markets |
| `GET /market_pnl/{market_id}` | P&L snapshot for a single market |

### Mutating (require auth)

| Endpoint | Description |
|----------|-------------|
| `POST /config` | Patch config at runtime and persist to `config_overrides.json` |
| `POST /config/reload` | Reload `config_overrides.json` from disk |
| `POST /bot` | Pause / resume the bot |
| `POST /positions/{market_id}/close` | Manually close a position |
| `POST /positions/redeem` | Redeem a resolved CTF position on Polygon |
| `POST /positions/ghost/dismiss` | Manual fallback to dismiss a wallet/bot discrepancy |
| `POST /maker/deploy/{token_id}` | Manually deploy quotes to a specific market |
| `POST /maker/undeploy/{token_id}` | Manually remove quotes from a market |

---

## Webapp

The React dashboard at `http://localhost:5173` gives real-time visibility into every aspect of the bot:

| Page | What you see |
|------|-------------|
| **Dashboard** | Bot status, P&L summary, open positions, system health |
| **Trades** | Full trade history with search / filter; each leg shows entry & exit spot prices (RTDS) |
| **Positions** | Open positions, momentum positions, range positions, recently closed spreads, and settlement/redemption state |
| **Performance** | Analytics breakdowns by market type, underlying, and strategy leg |
| **Signals** | Strategy 1/2/3 panel: maker opportunities, mispricing queue, and live momentum scan diagnostics |
| **Risk** | Exposure utilization, per-coin inventory, hedge status |
| **Markets** | All monitored markets with quoting status and signal scores |
| **Fills** | Paper fill events with adversity highlighting |
| **Logs** | Live stream plus Error History (long-lived WARNING/ERROR buffer) |
| **Settings** | Runtime config editor, including full momentum strategy controls |

---

## Testing

```bash
# Run all tests
pytest

# Run a specific test file
pytest tests/test_maker.py -v

# Run with coverage
pytest --cov=. --cov-report=term-missing
```

**1,569 tests collected** (unit tests; live RTDS/on-chain tests excluded from default run) as of the current release.

Test files:
- `tests/test_maker.py` — strategy quoting, repricing, inventory skew, edge filters
- `tests/test_fill_simulator.py` — paper fill logic, adversity thresholds
- `tests/test_live_fill_handler.py` — live fill parsing, WS reconciliation
- `tests/test_risk.py` — position sizing, exposure caps, P&L tracking
- `tests/test_rtds_live.py` — live RTDS WebSocket tests (both topics, all 7 coins)
- `tests/test_api_server.py` — all API endpoints, auth, serialization
- Plus additional integration/support modules

---

## Key Design Decisions

**Why market making, not latency arbitrage?** Polymarket taker fees at 50% probability are ~1.56% per trade, making round-trip latency arb unviable. As a maker, you earn a rebate instead of paying that fee — flipping the fee equation entirely. Full analysis in [Plan.md](Plan.md).

**Why Hyperliquid for the hedge?** Zero-fee maker quotes on HL perps, sub-second execution, and reliable REST + WebSocket APIs. The hedge fires once per fill burst (debounced), reducing HL order frequency.

**Why Kalshi for mispricing confirmation?** N(d₂) from Deribit gives a terminal price probability, but Polymarket markets resolve on a barrier-hit (one-touch) condition — a structural mismatch that can be 20–40 percentage points. Kalshi lists the same events on the same terms, making PM↔Kalshi comparison a true apples-to-apples mispricing signal. Full analysis in [MISPRICING_STRATEGY.md](MISPRICING_STRATEGY.md).

**Why Polymarket RTDS drives momentum exits (not a poll timer)?** Near expiry, Polymarket MMs withdraw from the NO/YES CLOB before the market resolves — the book drains to empty. A poll-based monitor with a 30 s interval may only fire once during a 57-second hold, and a drained CLOB book silences token-price stops. Wiring `rtds_client.on_price_update` to both the scanner and monitor means every RTDS tick evaluates the delta stop-loss independently of PM book state. RTDS is used for all spot decisions (entry gate, delta SL, Black-Scholes S, hedge sizing); Hyperliquid is execution-only.

**Why delta SL (not CLOB-price SL)?** The previous `MOMENTUM_STOP_LOSS_YES/NO` config stopped out positions when the *token price* dropped below a threshold. Near expiry, token prices collapse as MMs leave — a 0.85 token can momentarily show a mid of 0.50 from a single thin-book trade, triggering an unnecessary exit. Delta SL compares the **RTDS spot** to the strike: if spot has genuinely moved past the strike against us by >0.05%, we exit. RTDS spot is harder to game, doesn’t depend on PM order book liquidity, and is free from the HL funding-rate basis that would otherwise distort the comparison.

**Config architecture:** `config.py` holds all defaults as module-level constants. `config_overrides.json` holds any runtime changes. At startup, overrides are applied via `setattr`. The `GET /config/effective` endpoint returns the merged live view, making the running state fully inspectable without reading files.

---

## Documentation

| File | Contents |
|------|----------|
| [MAKER_STRATEGY.md](MAKER_STRATEGY.md) | Full market making strategy spec: quoting, repricing, hedging, fills, paper mode, config reference |
| [MISPRICING_STRATEGY.md](MISPRICING_STRATEGY.md) | Mispricing strategy: Kalshi + N(d₂) signal layers, known flaws, config reference |
| [strategies/Momentum/MomentumStrategy.md](strategies/Momentum/MomentumStrategy.md) | Momentum strategy spec: entry gates, volatility model, cooldowns, exits, diagnostics |
| [strategies/OpeningNeutral/PLAN.md](strategies/OpeningNeutral/PLAN.md) | Opening Neutral strategy spec: entry logic, FAK fill handling, one-leg fallback, pair accounting |
| [Plan.md](Plan.md) | Original project plan: fee analysis, architecture rationale, environment setup |
| [webapp/README.md](webapp/README.md) | Webapp routes, API hooks, and local development notes |
| [design.md](design.md) | UI/UX design spec for the webapp (founding document, March 2026) |
| [engineering.md](engineering.md) | Engineering plan for SELECTIVE EXPANSION features |
| [engineering_test_plan.md](engineering_test_plan.md) | Test plan for SELECTIVE EXPANSION changes |
| [INTEGRATION_TEST_PLAN.md](INTEGRATION_TEST_PLAN.md) | Integration test matrix for cross-component scenarios |
| [CEO_report.md](CEO_report.md) | First live run performance analysis (March 2026) |

---

## Security Notes

- **Never commit `.env`** — it's in `.gitignore`. Use `.env.example` as a template.
- **Use an API wallet key for HL**, not the master key. The API key can be revoked without moving funds.
- **Set `API_SECRET`** in production. Without it, any client on the network can pause/resume the bot or close positions.
- **`PAPER_TRADING=True` by default** — the bot will not place real orders until you explicitly disable it.
