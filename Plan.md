# Polymarket × Hyperliquid Multi-Strategy Bot

**TL;DR:** Build a semi-automated bot with three strategies: (1) market making on Polymarket crypto buckets with HL perp hedging, (2) PM-vs-Kalshi mispricing detection for milestone markets, and (3) a momentum scanner that takes high-probability entries when token-price and spot-direction signals align. The bot starts paper-trading, graduates to live with $50–200 size, and is designed to hand back to human approval on large positions.

> Historical note: this document started as the original market-making + mispricing launch plan. Momentum strategy details now live in `strategies/Momentum/MomentumStrategy.md`.

---

## Context

- **Budget:** $10,000 starting capital
- **Location:** Hong Kong (Polygon + Hyperliquid accessible; low latency to Asia nodes possible)
- **Date:** March 2026
- **Automation:** Semi-auto first, graduate to fully automated
- **Timeline:** 1–2 weeks to first live trades

### Why the original latency-arb approach doesn't work

Polymarket's taker fee at 50% probability is **~1.56%** per trade. That means a round-trip (PM taker + HL taker) costs **~1.6%** at mid-price, requiring >1.6% edge just to break even. 15-min bucket markets have been live since Jan 2026, and sophisticated bots dominate. PM CLOB matching + Polygon settlement is inherently slower than HL WebSocket BBO — the "lag" you'd want to exploit doesn't exist by the time your order is confirmed.

**The real opportunity:** Be a PM market **maker** (earn rebates instead of paying fees) and hedge delta on HL. Flip the fee equation entirely.

---

## Fee Reality Check

| PM Probability | PM Taker Fee | Round-Trip w/ HL Taker |
|---|---|---|
| $0.50 | ~1.56% | ~1.60% |
| $0.30 / $0.70 | ~0.92% | ~0.97% |
| $0.10 / $0.90 | ~0.20% | ~0.25% |
| $0.05 / $0.95 | ~0.06% | ~0.11% |

Fee formula: `fee = C × p × 0.0175 × (p × (1-p))`  
Only applies to markets with `feesEnabled: true` deployed after activation dates. Pre-existing markets: **zero fees**.

**Maker rebate:** 20% of taker fees collected on your resting orders, paid daily in USDC.

---

## Architecture & Folder Structure

```
Perp_Hyper_Arb/
├── main.py                   # Entry point + asyncio event loop
├── config.py                 # Constants, thresholds, flags + get_effective_config()
├── config_overrides.json     # Runtime overrides (persisted across restarts)
├── api_server.py             # FastAPI REST server on port 8080
├── risk.py                   # Position sizing, exposure limits, P&L tracking
├── monitor.py                # Position monitor (closes on expiry, loss, profit target)
├── fill_simulator.py         # Paper trading fill simulator
├── live_fill_handler.py      # Live fill handler + WS reconciliation
├── agent.py                  # AI agent: signal evaluation + execution decision
├── logger.py                 # File + console logging + Telegram alerts
├── launcher.py               # Helper launcher script
├── .env                      # Secrets (never committed)
├── .env.example              # .env template
├── requirements.txt
│
├── market_data/              # Exchange client wrappers
│   ├── pm_client.py          # Polymarket CLOB REST + WS + Gamma API
│   ├── hl_client.py          # Hyperliquid REST + WS
│   ├── kalshi_client.py      # Kalshi REST (read-only, no auth)
│   └── deribit.py            # Deribit IV / N(d₂) data
│
├── strategies/
│   ├── maker/                # Strategy 1: quoting, repricing, inventory skew, hedge
│   ├── mispricing/           # Strategy 2: Kalshi + N(d₂) signal filters
│   └── Momentum/             # Strategy 3: momentum scanner + taker execution
│
├── tests/                    # Pytest suite (672 passed, 7 skipped)
├── data/                     # CSV trade logs, paper trade records
└── webapp/                   # Vite + React monitoring dashboard
    ├── src/pages/            # Dashboard, Trades, Positions, Performance,
    │                         #   Signals, Risk, Markets, Fills, Logs, Settings
    ├── package.json
    ├── vite.config.ts
    └── .env.local            # VITE_API_URL=http://localhost:8080
```

---

## Environment Setup

**Python 3.10** (required exactly — `hyperliquid-python-sdk` has issues on 3.11+)

```bash
python3.10 -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

pip install -r requirements.txt
```

**.env file:**
```
# Polymarket
POLY_PRIVATE_KEY=0x...          # Polygon EOA private key (funded with USDC.e)
POLY_FUNDER=0x...               # Optional: separate funder wallet

# Hyperliquid
HL_ADDRESS=0x...                # Hyperliquid main wallet address
HL_SECRET_KEY=0x...             # Hyperliquid API wallet private key (not master key)

# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# API server
API_PORT=8080                   # FastAPI server port
API_SECRET=                     # Bearer token for mutating endpoints (leave blank to disable auth)
```

**Ollama setup (local, one-time):**
```bash
# Install Ollama from https://ollama.com, then pull a model:
ollama pull qwen2.5:7b          # Recommended: strong reasoning, fast on most hardware
# Alternative: ollama pull deepseek-r1:7b
```

**Webapp setup (one-time):**
```bash
cd webapp
npm install
# copy .env.local.example → .env.local and set VITE_API_URL
npm run dev   # local dev
npm run build # production build → deploy dist/ to Vercel
```

---

## Step 1: PM Client (`pm_client.py`)

Wrapper around `py-clob-client` v0.34.6 and the Gamma API.

**Market discovery:**
- `GET https://gamma-api.polymarket.com/events?active=true&closed=false` — poll every 60s
- Filter for crypto tag, `enableOrderBook: true`, label each as `bucket_15m`, `bucket_1h`, `bucket_daily`, or `milestone`
- Flag `feesEnabled` on each market object — prefer `feesEnabled: false` (pre-existing) markets for highest edge

**WebSocket subscription:**
- Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe to `book` and `price_change` events on all tracked `token_id`s
- Send `PING` every 10s for WS keep-alive

**Heartbeat loop (critical):**
- Send CLOB heartbeat every **5 seconds**
- PM cancels ALL open orders if no valid heartbeat for **>15 seconds**
- Reconnect and repost all limit orders immediately on WS disconnect

**Order helpers:**
- `place_limit(token_id, side, price, size)` — always use `post_only` modifier to guarantee maker status; fetches `feeRateBps` dynamically (NEVER hardcode)
- `batch_orders(orders)` — up to 15 orders per `postOrders()` call
- Respect tick size per market (fetch via `get_tick_size(token_id)`) — non-conforming orders are silently rejected

---

## Step 2: HL Client (`hl_client.py`)

Wrapper around `hyperliquid-python-sdk` v0.22.0.

**Price feed (WebSocket):**
- Subscribe to `bbo` channel for BTC, ETH, SOL — pushed on each block (~0.5s) when BBO changes
- Subscribe to `allMids` for reference pricing

**On startup:**
- Call `exchange.schedule_cancel(now + 300)` — dead man's switch, cancels all HL orders if bot dies for >5 min
- Refresh dead man's switch every 3 minutes

**Helpers:**
- `place_hedge(coin, side, size, slippage=0.003)` — market order via `exchange.market_open()`
- `get_predicted_fundings()` — poll `info.predicted_fundings()` every 5 min for BTC/ETH/SOL across Binance, Bybit, HL
- `get_user_state()` — track open positions and margin

**HL fee tier:** Default taker 0.045%, maker 0.015%. At $10K starting volume, you're at Tier 0 for a while.

---

## Step 3: Strategy 1 — PM Market Making + HL Delta Hedge (`maker.py`)

**Primary strategy.** Core loop runs every ~1s triggered by WS events.

**Target markets:**
1. **Priority A:** Pre-existing `feesEnabled: false` crypto markets (1H, daily, weekly buckets) — zero fees, pure spread capture
2. **Priority B:** `feesEnabled: true` markets with probabilities in the $0.05–$0.20 / $0.80–$0.95 range — fee hurdle is <0.3%, spreads are usually wider, less competition than at 50%
3. **Avoid initially:** 15-min bucket markets at 50% — competitive, 1.56% fee eats all edge

**Quoting logic:**
- Fetch `max_incentive_spread` per market (qualifies for maker rebate program)
- Post limit bids and asks within this spread, centered on midpoint
- Use `post_only` on all orders — if order would cross, skip and reprice
- **Repricing trigger:** When HL BBO moves >0.5%, cancel and repost at updated prices

**Delta hedging:**
- Track net PM inventory per underlying (BTC, ETH, SOL) across all open limit orders
- When net delta exceeds **$500 notional**, hedge via HL perp:
  - Delta approximation: `Δ ≈ N(d2)` from Black-Scholes using HL IV implied from current HL prices (or fallback: use PM price directly as Δ proxy for simplicity early on)
  - If long PM Yes (expecting up), hedge short HL perp; if long PM No, hedge long HL perp
- Rebalance HL hedge when PM inventory changes by >$100

**New market opportunism:**
- Poll for markets < 1 hour old with thin books
- Post wide initial quotes (5–10 cents) on new fee-free markets to be first maker
- Auto-cancel when a competing quote narrower than 2 cents appears

---

## Step 4: Strategy 2 — Milestone Market Mispricing Scanner (`mispricing.py`)

**Secondary strategy.** Runs in a separate async coroutine, fires alerts not auto-trades (initially).

**Signal: PM binary price vs Deribit options-implied probability**

1. Fetch PM milestone markets (e.g., "BTC > $120k by end of Q2?") — filter from Gamma API with `endDate > 30 days away`
2. Map each market to the corresponding Deribit instrument (nearest strike and expiry to the PM resolution condition)
3. Compute options-implied probability: `P = N(d2)` using Black-Scholes log-normal with Deribit IV
4. Compare: `deviation = |PM_price - options_implied_prob|`

**Trade trigger:**
- `deviation > 5%` (fee hurdle ~0.25% for a taker entry + basis risk buffer)
- PM market has `feesEnabled: false` OR probability is near extreme (< $0.15 or > $0.85)

**In semi-auto mode:**
- Print detailed alert: market name, PM price, Deribit-implied prob, deviation, suggested direction, estimated trade size
- Wait for `y/n` input before executing
- Log decision either way

**Execution (when approved):**
- Take PM taker order (accept taker fee — this is a mispricing play, not a maker play)
- Place HL perp hedge for delta management (approximate binary delta using N(d2))
- Record as pair position in risk.py

**Fully automated later:** Replaced by the agent decision layer (see Step 7) after 10+ validated signals.

---

## Step 5: Risk Engine (`risk.py`)

| Parameter | Value | Rationale |
|---|---|---|
| Max PM exposure per market | $500 | 5% of $10K |
| Max total PM exposure | $2,000 | 20% of $10K |
| Max HL perp notional | $3,000 | Hedge only, no speculation |
| Min edge after fees | Dynamic | `fee_formula(p) + 0.2%` buffer |
| Max concurrent positions | 5 | Preserves capital liquidity |
| Hard stop (drawdown) | $500 (5%) | Halt all new orders + alert |

**Position tracking:**
- Per-market: `entry_price`, `size`, `side`, `hl_hedge_size`, `hl_entry_price`, `pm_fees_paid`, `pm_rebates_earned`
- Aggregate: `total_pm_delta`, `total_hl_notional`, `unrealized_pnl`, `realized_pnl`

**`min_edge_after_fees(p)` function:**
```python
def min_edge_after_fees(p: float) -> float:
    pm_taker_fee = p * 0.0175 * (p * (1 - p))  # per $1 contract
    hl_taker_fee = 0.00045  # 0.045%
    buffer = 0.002  # basis risk / slippage
    return pm_taker_fee + hl_taker_fee + buffer
```

---

## Step 6: Logging & Alerts (`logger.py`)

- Structured file logging + console output with timestamps
- Telegram alerts on:
  - Any order fill > $100
  - Hard stop triggered
  - Daily P&L summary (08:00 HKT)
  - Strategy 2 mispricing signal detected
  - WS disconnect / reconnect events
- CSV trade log (`data/trades.csv`): `timestamp, market_id, market_type, side, size, price, fees_paid, rebates_earned, hl_hedge_size, hl_entry_price, strategy, pnl`

---

## Step 7: AI Agent Decision Layer (`agent.py`)

Replaces the manual `y/n` prompt for Strategy 2 signals once enough validated signals have been collected. Uses a locally-running Ollama model — zero latency, no API cost, no external dependency.

**Model:** `qwen2.5:7b` (default) or `deepseek-r1:7b`, configured via `config.py: AGENT_MODEL`.

**On every Strategy 2 signal, the agent:**
1. Receives a structured prompt containing:
   - Market name, PM price, Deribit-implied probability, deviation %
   - Current portfolio: total PM exposure, total HL notional, drawdown so far today
   - Recent trade context: last 5 trades (outcome, P&L)
   - Macro context: 24h BTC/ETH price change, current HL predicted funding rates
2. Calls a small set of local tools (Python functions, not remote APIs):
   - `get_risk_state()` → current exposure vs limits
   - `get_recent_trades(n=5)` → last N trades from `data/trades.csv`
   - `get_hl_funding()` → predicted funding for BTC/ETH/SOL
   - `get_position_summary()` → open positions and unrealized P&L
3. Returns structured JSON:
   ```json
   {
     "decision": "execute" | "skip",
     "confidence": 0.0–1.0,
     "reasoning": "One paragraph explanation"
   }
   ```

**Hard override rules (always enforced, agent cannot bypass):**
- Risk limits would be breached → always `skip`
- `confidence < 0.6` → always `skip`
- Hard stop already triggered → always `skip`

**Logging:** Agent decision, confidence score, and full reasoning are logged per signal to `data/agent_decisions.csv` and surfaced in the webapp.

**Rollout:** Agent starts in **shadow mode** — logs decisions but still shows `y/n` prompt. After 10+ signals where agent decision matches human decision, switch to **agent-auto mode** (`config.py: AGENT_AUTO = True`).

---

## Step 8: REST API + Monitoring Webapp (`api_server.py` + `webapp/`)

### API Server (`api_server.py`)

FastAPI server that runs alongside the bot in a separate asyncio task (or subprocess). Exposes read-only endpoints — no trading actions exposed.

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Bot uptime, WS connection status, last heartbeat ts |
| GET | `/positions` | All open PM + HL positions with unrealized P&L |
| GET | `/trades` | Paginated trade history from `data/trades.csv` |
| GET | `/pnl` | Daily/weekly/all-time P&L summary |
| GET | `/performance` | Full analytics: win rate, Sharpe, equity curve, rebates, breakdown by strategy/asset |
| GET | `/signals` | Recent Strategy 2 signals with agent decisions |
| GET | `/risk` | Current exposure vs limits |
| GET | `/markets` | Currently tracked PM markets and quoted spreads |
| GET | `/funding` | Latest HL predicted funding rates |

All responses are JSON. CORS enabled for `*` (lock down to Vercel domain in production).

Start alongside bot: `asyncio.create_task(run_api_server(port=API_PORT))`

### Webapp (`webapp/`)

**Stack:** Vite + React + TypeScript. Deploys as a static site to Vercel — no server-side rendering needed.

**Pages / sections:**

1. **Dashboard (home):** Live P&L chart (daily + cumulative), open positions table, system health indicators (WS connected, heartbeat status, bot running)
2. **Trades:** Filterable trade log table — strategy, market, side, size, fees, P&L per trade
3. **Performance:** Deep analytics on completed trades (see breakdown below)
4. **Signals:** Strategy 2 signal history — each card shows PM price, Deribit-implied prob, deviation, agent decision + reasoning + confidence
5. **Risk:** Real-time exposure gauges vs limits (PM exposure, HL notional, drawdown)
6. **Markets:** Currently tracked PM markets — quoted spread, fill rate, rebates earned

**Performance page — metrics & charts:**
- **Summary row:** Total trades, win rate %, avg P&L per trade, total fees paid, total maker rebates earned, net P&L
- **Equity curve:** Cumulative P&L over time (line chart), with drawdown shaded below the curve
- **P&L breakdown:** Bar chart split by strategy (Strategy 1 maker vs Strategy 2 mispricing) and by underlying (BTC, ETH, SOL)
- **Trade outcome distribution:** Histogram of per-trade P&L — visualises the distribution of wins/losses
- **Rolling metrics (7-day window):** Win rate, avg trade P&L, Sharpe ratio estimate (`mean_daily_pnl / std_daily_pnl × √365`)
- **Best / worst trades table:** Top 5 and bottom 5 trades by P&L with full detail
- **Rebates tracker:** Daily bar chart of maker rebates earned vs taker fees paid — key signal for maker strategy health
- **Agent accuracy (once agent is live):** Agent `execute` decisions that were profitable vs unprofitable; shadow mode agreement rate with human decisions
- **Time-of-day heatmap:** Average P&L by hour of day (HKT) — identifies best/worst trading windows

**API endpoint to add:** `GET /performance?period=7d|30d|all` — returns all metrics above pre-computed from `data/trades.csv`; recomputed on each request (fast enough at $10K scale)

**Data flow:**
- Webapp polls API every 5s for live data (no WS needed — polling is fine for monitoring)
- `VITE_API_URL` env var sets bot API URL: `http://localhost:8080` locally, `http://<VPS_IP>:8080` in production
- Vercel deployment: `vercel --prod` from `webapp/` — zero config needed with `vercel.json`

**Vercel config (`webapp/vercel.json`):**
```json
{
  "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }]
}
```

---

## Steps 9–10: Paper Trading → Live

**Paper mode (`config.py: PAPER_TRADING = True`):**
- Intercept all order calls, simulate fills at mid-price (conservative)
- Track simulated P&L in `data/paper_trades.csv`
- Run for **minimum 3 days** (ideally 5), targeting 20+ fill events
- Validate: fill rate, average spread captured, delta hedge effectiveness, heartbeat recovery

**Go-live sequence:**
1. Set max sizes to 10% of strategy limits ($50/trade)
2. Enable Strategy 1 (maker) on fee-free markets only
3. Monitor via webapp dashboard 24h continuously
4. After 24h stable: increase to full Strategy 1 limits
5. Enable Strategy 2 signal alerts — agent runs in shadow mode (logs vs human)
6. After 10+ signals with agent/human agreement: set `AGENT_AUTO = True`

---

## Verification Checklist

- [ ] Unit test `min_edge_after_fees()` at p = 0.05, 0.10, 0.30, 0.50, 0.90
- [ ] Assert `feeRateBps` is always fetched via API, never hardcoded — add assertion in `pm_client.place_limit()`
- [ ] Test PM heartbeat recovery: kill WS artificially, confirm orders cancelled < 15s and bot resubscribes correctly
- [ ] Test HL dead man's switch: set 30s expiry in test mode, confirm it fires
- [ ] Test repricing loop: simulate HL BBO move of 1%, confirm PM quotes are cancelled and reposted at updated prices
- [ ] Backtest: replay 30 days of HK BTC price data through quoting logic, estimate spread capture and delta hedge P&L
- [ ] Paper trade 3–5 days before any live capital
- [ ] Test agent shadow mode: fire a synthetic signal, confirm agent logs decision without executing
- [ ] Test agent hard overrides: set exposure to 99% of limit, confirm agent always returns `skip` regardless of signal quality
- [ ] Verify all webapp `/api/*` endpoints return correct data during paper trading
- [ ] Deploy webapp to Vercel, confirm it reads from bot API URL correctly
- [ ] Test webapp with bot offline: confirm graceful degradation (shows "disconnected" state, not crash)

---

## Key Technical Gotchas

1. **PM heartbeat kill switch:** >15s without heartbeat = all orders cancelled. Design heartbeat as a high-priority coroutine with its own exception handling.
2. **Tick size:** Always fetch `get_tick_size(token_id)` before posting. Non-conforming prices cause silent order rejection.
3. **`feeRateBps` in signed payload:** Must be dynamically fetched. If wrong → order rejected.
4. **GTD orders:** Add 60s buffer — `expiration = now + 60 + desired_lifetime_seconds`.
5. **Sports market books:** Cancelled at game start. Don't accidentally quote sports markets.
6. **HL expiresAfter stale cancellations:** Stale expiresAfter cancellations consume **5× normal rate limit**. Set dead man's switch time carefully.
7. **HL Python 3.10 only:** `hyperliquid-python-sdk` has dependency issues on 3.11+.
8. **HK location advantage:** Polygon's primary servers are `eu-west-2`. HL latency from HK is reportedly good. For <150ms to HL, local machine is likely fine. If PM latency becomes an issue, Hetzner Singapore (~$5/mo) is the VPS upgrade path.

