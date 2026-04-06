# Perp Hyper Arb

A semi-automated crypto trading bot that runs three complementary strategies: market making on Polymarket, mispricing detection against Kalshi, and momentum-based taker entries on high-probability buckets. Net delta exposure is hedged via Hyperliquid perpetuals.

---

## What It Does

**Strategy 1 — Market Making:** Posts two-sided YES/NO quotes on Polymarket crypto bucket markets, earning the spread plus maker rebates. Every significant inventory build is automatically hedged via a Hyperliquid perp trade in the opposite direction, keeping the book close to market-neutral. All pricing decisions (reprice triggers, hedge sizing, vol estimation) use **Polymarket RTDS** spot prices — Hyperliquid is used only for order execution.

**Strategy 2 — Mispricing Scanner:** Scans Polymarket milestone markets against matching Kalshi markets. When both venues list the same crypto event (e.g. "Will BTC close above $90k on March 31?"), any price divergence above the fee hurdle is a candidate trade. Deribit N(d₂) can optionally be used as a second-layer confirmation signal. Spot price for Black-Scholes is sourced from **Polymarket RTDS**.

**Strategy 3 — Momentum Scanner:** Runs a price-confirmation taker strategy that enters high-probability contracts when Polymarket token prices and spot movement jointly confirm momentum. It supports volatility-aware thresholds, per-market cooldowns, stop-loss / take-profit exits, a near-expiry protective exit, and an optional range-market sub-strategy for "Will BTC be between $X and $Y?" markets.

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
│   └── Momentum/             # Strategy 3: momentum scanner + taker execution
│       ├── market_utils.py   #   shared market-classification + strike-extraction helpers
│       └── ...
│
├── tests/                    # Pytest suite (887 unit tests)
├── data/                     # CSV trade logs, paper trade records
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
| `HEDGE_THRESHOLD_USD` | `100` | Net inventory before a perp hedge fires (USD) |
| `MOMENTUM_SCAN_INTERVAL` | `10` | Seconds between momentum scan passes |
| `MAX_QUOTE_AGE_SECONDS` | `30` | Backstop reprice interval |
| `MAKER_EXIT_HOURS` | `0.0` | Non-bucket time-stop (hours before expiry); `0.0` disables this gate |
| `MAX_PM_EXPOSURE_PER_MARKET` | `500` | Max USD deployed per market |
| `MAX_TOTAL_PM_EXPOSURE` | `5000` | Total PM exposure cap (USD) |
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

**887 tests collected** (unit tests; live RTDS/on-chain tests excluded from default run) as of the current release.

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
