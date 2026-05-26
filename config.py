"""
config.py — All constants, thresholds, and runtime flags.
Import this everywhere instead of hardcoding values.
"""
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Polymarket ──────────────────────────────────────────────────────────────
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER: str = os.getenv("POLY_FUNDER", "") or os.getenv("POLY_ADDRESS", "")
POLY_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
# WebSocket RPC for Polygon — eth_subscribe logs for Chainlink AnswerUpdated events.
# Default: PublicNode free endpoint — reliable eth_subscribe, no API key required.
# For higher reliability / lower latency, set POLYGON_WS_URL to a paid endpoint:
#   wss://polygon-mainnet.g.alchemy.com/v2/<KEY>     (Alchemy — recommended)
#   wss://polygon-mainnet.infura.io/ws/v3/<KEY>       (Infura)
#   wss://polygon.getblock.io/<KEY>/mainnet/           (GetBlock)
POLYGON_WS_URL: str = os.getenv("POLYGON_WS_URL", "wss://polygon-bor-rpc.publicnode.com")

# Chainlink Data Streams / Mercury Pipeline — direct WebSocket feed for HYPE/USD.
# Two auth modes are supported (first match wins):
#   1. Mercury Basic auth — set CHAINLINK_DS_USERNAME + CHAINLINK_DS_PASSWORD.
#      WS host should point to your Mercury pipeline endpoint (wss://...).
#   2. Legacy HMAC — set CHAINLINK_DS_API_KEY + CHAINLINK_DS_API_SECRET.
#      Connects to wss://ws.dataengine.chain.link with HMAC-signed headers.
# Leave all empty to run in RTDS-only mode (HYPE price still delivered sub-second
# via the crypto_prices_chainlink RTDS topic, without direct oracle access).
#
# Mercury pipeline endpoint (wss:// scheme required for WebSocket).
CHAINLINK_DS_HOST: str = os.getenv("CHAINLINK_DS_HOST", "wss://ws.dataengine.chain.link")
# Mercury Basic-auth credentials (provided by Chainlink DevEx for pipeline access).
CHAINLINK_DS_USERNAME: str = os.getenv("CHAINLINK_DS_USERNAME", "")
CHAINLINK_DS_PASSWORD: str = os.getenv("CHAINLINK_DS_PASSWORD", "")
# Candlestick REST API key — for historical OHLCV queries (separate from streaming).
CHAINLINK_DS_CANDLESTICK_KEY: str = os.getenv("CHAINLINK_DS_CANDLESTICK_KEY", "")
# Legacy HMAC auth (standard Data Streams consumer API).
CHAINLINK_DS_API_KEY: str = os.getenv("CHAINLINK_DS_API_KEY", "")
CHAINLINK_DS_API_SECRET: str = os.getenv("CHAINLINK_DS_API_SECRET", "")
# Feed ID for HYPE/USD on Chainlink Data Streams — provided with your API key.
CHAINLINK_DS_HYPE_FEED_ID: str = os.getenv("CHAINLINK_DS_HYPE_FEED_ID", "")
# Per-coin feed IDs for all tracked underlyings.
CHAINLINK_DS_FEED_IDS: dict[str, str] = {
    coin: os.getenv(f"CHAINLINK_DS_{coin}_FEED_ID", "")
    for coin in ["BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP"]
}

RELAYER_API_KEY: str = os.getenv("RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS: str = os.getenv("RELAYER_API_KEY_ADDRESS", "")

# Token IDs of underlying assets tracked. Used to label markets.
TRACKED_UNDERLYINGS: list[str] = [
    "BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP",
]

# How often (seconds) to refresh the market list from Gamma API
MARKET_REFRESH_INTERVAL: int = 15

# PM WebSocket
PM_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PM_USER_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
PM_DATA_API_URL: str = "https://data-api.polymarket.com"
PM_HEARTBEAT_INTERVAL: float = 5.0   # seconds — must be < 15s hard limit
PM_WS_PING_INTERVAL: float = 10.0    # seconds
# Empirically confirmed server-side limit: 100 tokens per WS session triggers
# INVALID OPERATION ("shard_tokens=100 shard_rejected=6" observed in prod).
# Set to 50 to stay well within the limit and allow headroom for incremental
# subscriptions added mid-session without crossing the rejection threshold.
PM_WS_MAX_MARKETS_PER_WS: int = 50
# Enable best_bid_ask WS events (requires custom_feature_enabled on subscription).
# These events give authoritative best-bid pruning on thin near-expiry books but
# generate events for every subscribed token — can saturate the event loop at
# scale (35 shards × 50 tokens). Disable if shards are 1006-cascading.
PM_WS_BEST_BID_ASK: bool = True

# ── Hyperliquid ─────────────────────────────────────────────────────────────
HL_ADDRESS: str = os.getenv("HL_ADDRESS", "")
HL_SECRET_KEY: str = os.getenv("HL_SECRET_KEY", "")
HL_BASE_URL: str = "https://api.hyperliquid.xyz"

HL_PERP_COINS: list[str] = ["BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP"]
HL_DEFAULT_SLIPPAGE: float = 0.003   # 0.3% max slippage for hedge market orders

HL_DEAD_MAN_INTERVAL: int = 300      # seconds — refresh dead man's switch every 5 min
HL_FUNDING_POLL_INTERVAL: int = 120  # seconds — unused (funding now via webData2 WS)
FUNDING_STALE_THRESHOLD_S: int = 120  # seconds — FundingRateCache staleness window

# ── Strategy 1 — Market Making ───────────────────────────────────────────────
# Repricing: cancel + repost when HL BBO moves by more than this fraction
# Tightened from 0.5% → 0.2% (Flaw §4: gamma can swing delta >0.3→0.8 in <0.5%)
REPRICE_TRIGGER_PCT: float = 0.002   # 0.2%; NOTE: overridden to 0.01 in config_overrides.json

# Market types to exclude from quoting entirely (e.g. ["bucket_1h"] to disable hourly buckets).
# Empty list = quote all market types. Patched at runtime via POST /config.
MAKER_EXCLUDED_MARKET_TYPES: list = []

# Enable/disable the HL delta-hedge leg entirely. When False, _rebalance_hedge is a no-op.
# Patched at runtime via POST /config.
MAKER_HEDGE_ENABLED: bool = True

# Maximum age in seconds of a PM order book before skipping a reprice on that market.
# 0 = disabled (no age check). Set to e.g. 30 to skip markets with stale books.
MAKER_MAX_BOOK_AGE_SECS: int = 0

# Inventory threshold before hedging on HL (notional USD).
# Must be reachable: MAX_MAKER_POSITIONS_PER_UNDERLYING × quote_size must exceed this.
# Default: 3 positions × $50 = $150 max per coin → threshold set to $100 (2 fills triggers hedge).
HEDGE_THRESHOLD_USD: float = 100.0  # NOTE: overridden to 200 in config_overrides.json
# Rebalance HL hedge only when the required size change exceeds this fraction of the current hedge notional.
# e.g. 0.20 = only resize if the hedge needs to change by >20%. Scales naturally across coins.
HEDGE_REBALANCE_PCT: float = 0.20
# Per-coin cooldown: minimum seconds between two executed HL hedges for the same coin.
# Prevents churning when many PM fills arrive in quick succession.
HEDGE_MIN_INTERVAL: float = 8.0   # NOTE: overridden in config_overrides.json
# Debounce window: when a fill/BBO event triggers a hedge, wait this many seconds for
# additional fill events to accumulate before executing a single combined hedge order.
# Set to 0 to disable (immediate execution — useful for direct test calls).
HEDGE_DEBOUNCE_SECS: float = 3.0  # NOTE: overridden in config_overrides.json

# Volume-proportional spread sizing (per SPREAD, not per side).
# Contracts per side = floor(spread_budget / (bid_price + (1 - ask_price)))
# so both legs always have equal contract counts and together consume the full budget.
# MAKER_SPREAD_SIZE_PCT × market 24hr volume, clamped to [MIN, MAX].
# New markets with no recorded volume use MAKER_SPREAD_SIZE_NEW_MARKET as a conservative fallback.
MAKER_SPREAD_SIZE_PCT: float = 0.04       # 4% of 24hr volume per spread
MAKER_SPREAD_SIZE_MIN: float = 125.0      # floor: $100 minimum per spread
MAKER_SPREAD_SIZE_MAX: float = 575.0      # ceiling: $500 maximum per spread
MAKER_SPREAD_SIZE_NEW_MARKET: float = 100.0 # fallback for markets with no volume data
# Safety backstop: absolute ceiling on contracts per side per order.
# Under normal operation MAKER_BATCH_SIZE (below) is the operative per-order cap;
# this only fires if MAKER_BATCH_SIZE is set unreasonably high.
MAKER_MAX_CONTRACTS_PER_SIDE: int = 999
# Hard cap on total (YES + NO combined) open contracts per market.
# Once this threshold is reached, no new quotes are posted until the market's positions
# are closed or expire.  Prevents runaway accumulation when fills are lopsided.
MAKER_MAX_CONTRACTS_PER_MARKET: int = 500
# Imbalance hard-stop: if one side's open position exceeds the other by more than this
# many contracts, block ALL new orders on the heavy side entirely.
# Under normal operation the imbalance-aware sizing in _deploy_quote converges positions
# to balance before this threshold is reached; this is only a last-resort circuit breaker.
MAKER_MAX_IMBALANCE_CONTRACTS: int = 5
# Naked-leg force-close: if one side exceeds the other by this many contracts AND the
# imbalance has persisted for at least MAKER_NAKED_CLOSE_SECS seconds, the strategy
# will taker-exit the excess quantity to eliminate directional exposure.
# NOTE: must be >= MAKER_MAX_IMBALANCE_CONTRACTS so the hard stop fires first.
# When MAKER_NAKED_CLOSE_ENABLED=False the detection still runs but no taker exit is
# placed — the AI agent (or operator) can take over the close decision.
MAKER_NAKED_CLOSE_ENABLED: bool = True
MAKER_NAKED_CLOSE_CONTRACTS: int = 10
MAKER_NAKED_CLOSE_SECS: float = 10.0
# Per-leg fill cap: once a single leg (YES or NO) of a market has been filled this many
# times, stop re-posting that leg. Prevents runaway accumulation from repeated adverse
# selection on one side (Factor A: single-side trap; Factor B: high fill count bleed).
# 0 = disabled.
MAKER_MAX_FILLS_PER_LEG: int = 6
# Minimum max_incentive_spread required to post a quote.  Markets with a spread reward
# below this floor provide insufficient edge after fees and rebates; skip them entirely.
MAKER_MIN_INCENTIVE_SPREAD: float = 0.04

# New market: post initial wide quote if market < this many seconds old
NEW_MARKET_AGE_LIMIT: int = 3600     # 1 hour
NEW_MARKET_WIDE_SPREAD: float = 0.08 # 8-cent spread (4c on each side)
NEW_MARKET_PULL_SPREAD: float = 0.02 # Pull if competing quote is inside 2c

# Backstop max age for any resting quote before forced reprice (Flaw §4)
MAX_QUOTE_AGE_SECONDS: int = 30

# Minimum YES-price to quote on either side (adverse-selection guard).
# Markets where mid < MAKER_MIN_QUOTE_PRICE or mid > (1 − MAKER_MIN_QUOTE_PRICE)
# are deeply OTM/ITM binary options: the book is thin, informed flow dominates,
# and paper fills at 0.01 are almost always adversely selected.
MAKER_MIN_QUOTE_PRICE: float = 0.05

# Minimum 24h USD volume required to quote. Markets below this threshold are
# illiquid: fills are slow, capital revolves inefficiently, adverse selection risk
# is higher. Defaults to $5K; raise to $10–$50K for tighter capital velocity focus.
MAKER_MIN_VOLUME_24HR: float = 5000.0

# Minimum fee-adjusted edge per side before posting (Flaw §2).
# Correct formula: effective_edge = half_spread + market.rebate_pct * taker_fee
# (we earn the rebate, not pay the taker fee; rate varies by market type).
# Lowered from 0.005 → 0.001 because rebate contribution is additive.
MAKER_MIN_EDGE_PCT: float = 0.001  # NOTE: overridden to 0.005 in config_overrides.json

# Minimum profit margin (per contract) required when quoting the second leg of a
# spread.  When one side is already filled, the second-leg quote is only posted if:
#   YES_entry + (1 - current_ask)  <=  1.0 - MAKER_MIN_SPREAD_PROFIT_MARGIN  (NO needed)
#   (1 - NO_entry) + current_bid   <=  1.0 - MAKER_MIN_SPREAD_PROFIT_MARGIN  (YES needed)
# This prevents negative-spread entries caused by mid drifting between fills.
# 0.005 = require at least 0.5¢/contract combined edge before posting the missing leg.
MAKER_MIN_SPREAD_PROFIT_MARGIN: float = 0.010

# Per-coin inventory loss limit for maker strategy (Flaw §5)
# If total unrealised P&L across all open positions for a coin drops below
# -MAKER_COIN_MAX_LOSS_USD, the monitor will force-close that coin's positions.
MAKER_COIN_MAX_LOSS_USD: float = 75.0

# Maker near-expiry passive-flatten threshold (Flaw §6)
# MAKER_EXIT_HOURS  — close existing positions when this many hours remain until
#                     market resolution (TTE-based, NOT position age).
#                     Also used as the quoting gate for milestone/unknown markets.
#                     Overridden to 0.0 in config_overrides.json (rely on
#                     MAKER_EXIT_TTE_FRAC for bucket markets instead).
MAKER_EXIT_HOURS: float = 0.0
# MAKER_EXIT_TTE_FRAC — quoting gate for bucket_* markets expressed as a fraction
#                       of the canonical market lifetime (0.10 = last 10% of life).
#                       Replaces the absolute MAKER_EXIT_HOURS gate for short-duration
#                       buckets so that e.g. a fresh bucket_1h (TTE=1h, frac=1.0) is
#                       treated as healthy rather than inside the 6h exit window.
#                       Set to 0.0 to disable (not recommended).
MAKER_EXIT_TTE_FRAC: float = 0.10
# MAKER_EXIT_TTE_FRAC_5M — per-type exit TTE override for bucket_5m markets only.
#                          5m buckets frequently yield one-sided fills because the
#                          market expires before the opposing leg can trade.  A larger
#                          exit fraction (e.g. 0.35 = stop quoting when 35% = 1m 45s
#                          remains) gives offsetting flow more time to arrive while
#                          still capturing the core of the market's duration.
#                          Set to 0.0 to fall back to MAKER_EXIT_TTE_FRAC.
MAKER_EXIT_TTE_FRAC_5M: float = 0.35
# MAKER_ENTRY_TTE_FRAC — symmetric opening cooldown: don't start quoting until this
#                        fraction of the market's life has ELAPSED (i.e. tte_secs/duration
#                        must be < 1 - MAKER_ENTRY_TTE_FRAC).
#                        E.g. 0.10 = skip the first 10% of the bucket's life.
#                        For bucket_5m that is a 30-second opening blackout — long enough
#                        for the HL vol filter to build history and informed opening flow
#                        to clear before the maker participates.
#                        Set to 0.0 to disable.
MAKER_ENTRY_TTE_FRAC: float = 0.10
# MAKER_BATCH_SIZE — maximum contracts per side per individual quote order.
#                    Together with imbalance-aware sizing, this creates natural fill
#                    batching: each order is at most this large, so one adversarial
#                    sweep can only take MAKER_BATCH_SIZE contracts before the next
#                    reprice cycle re-evaluates balance and decides whether to re-post.
#                    Set to a large value (e.g. 999) to disable (same as current
#                    MAKER_MAX_CONTRACTS_PER_SIDE behaviour).
MAKER_BATCH_SIZE: int = 50

# Maximum time-to-expiry for a market to be quoted.
# Markets resolving beyond this many days are too long-dated for market making
# (open directional risk, low rebate frequency). Excludes truly open-ended markets
# (end_date=None). Should be > 1 day to include daily-resolution bucket markets.
MAKER_MAX_TTE_DAYS: int = 14

# Inventory skew quoting (Area C): shift mid by this fraction of net inventory
# to passively encourage fills that reduce open inventory.
# 0.0001 = 1 cent of skew per $100 of net inventory, capped at ±INVENTORY_SKEW_MAX.
INVENTORY_SKEW_COEFF: float = 0.0001
INVENTORY_SKEW_MAX: float = 0.03   # hard cap: ±3 cents (≈ 1 half-spread unit; meaningful vs 2–4c spread)

# Per-market asymmetric imbalance skew: when YES fills > NO fills (or vice versa)
# within a single market, tighten the LAGGING side's price toward mid to attract
# fills faster, sacrificing a few ticks of edge to avoid accumulating directional
# exposure.  Only the under-filled leg is adjusted; the over-filled leg is left at
# fair-value pricing (unlike the symmetric coin-level skew above which moves both).
#
# MAKER_IMBALANCE_SKEW_COEFF: price improvement per excess contract
#   e.g. 0.0005 → 50 excess contracts → 2.5 cent tightening (capped at MAX)
# MAKER_IMBALANCE_SKEW_MAX:   hard cap on the per-market price improvement
# MAKER_IMBALANCE_SKEW_MIN_CT: minimum imbalance (contracts) before adjustment fires
MAKER_IMBALANCE_SKEW_COEFF: float = 0.0012
MAKER_IMBALANCE_SKEW_MAX:   float = 0.05    # ±3 cents, ≈ 1 half-spread unit
MAKER_IMBALANCE_SKEW_MIN_CT: float = 5.0   # ignore tiny natural fluctuations

# ── CLOB Depth-aware quoting ──────────────────────────────────────────────────
# Gate: minimum competing contracts at our quote level before posting.
# 0 = disabled.  When book depth < this at our price, every fill is from
# informed flow (highest adverse-selection scenario).
MAKER_MIN_DEPTH_TO_QUOTE: int = 0

# Below this depth threshold the book is considered "thin".
MAKER_DEPTH_THIN_THRESHOLD: int = 50

# Spread widening multipliers by depth zone:
#   depth >= THIN_THRESHOLD   → factor 1.0 (competitive book, no widening)
#   0 < depth < THIN_THRESHOLD → FACTOR_THIN (linear interpolation toward this)
#   depth == 0                → FACTOR_ZERO (we ARE the market, max widening)
# Applied as: half_spread = min(half_spread * factor, NEW_MARKET_WIDE_SPREAD / 2)
MAKER_DEPTH_SPREAD_FACTOR_THIN: float = 1.0   # 1.0 = disabled; try 1.5 to widen when thin
MAKER_DEPTH_SPREAD_FACTOR_ZERO: float = 1.0   # 1.0 = disabled; try 2.0 when sole maker

MAKER_DEPLOYMENT_MODE: str = "auto"  # "auto": deploy all qualifying signals immediately; "manual": wait for explicit deploy
# Pause quoting when HL 5-minute realised move exceeds this fraction.
# 1.5% → adverse selection dominates; wait for the dust to settle.
MAKER_VOL_FILTER_PCT: float = 0.015

# Force-reprice a partially-filled quote when market mid has drifted more than
# this fraction from the quote price, even if the quote is not yet stale.
# Prevents the remaining resting size from sitting at an adversely-selected
# price after a fast HL move. 1.5% ≈ half a typical max_incentive_spread.
MAKER_ADVERSE_DRIFT_REPRICE: float = 0.010

# ── Bot-level kill switch ─────────────────────────────────────────────────────
# When False, the agent loop and mispricing scanner idle without opening new
# positions. Existing open positions continue to be monitored for exit.
# Toggled at runtime via POST /bot in the API.
BOT_ACTIVE: bool = True

# ── Strategy Toggles ────────────────────────────────────────────────────────
STRATEGY_MISPRICING_ENABLED: bool = False   # Strategy 2: Deribit implied-prob mispricing
STRATEGY_MAKER_ENABLED: bool = False        # Strategy 1: PM market making + HL delta hedge
STRATEGY_MOMENTUM_ENABLED: bool = False     # Strategy 3: Momentum / price-confirmation taker
STRATEGY_SPREAD_ENABLED: bool = False       # Strategy 4: Calendar spread / relative-value
OPENING_NEUTRAL_ENABLED: bool = False       # Strategy 5: Opening neutral (simultaneous YES+NO entry)
REVERSE_OPENING_NEUTRAL_ENABLED: bool = False  # Strategy 5b: Reverse ON — paper experiment; mirrors ON entry, sells winner for TP, holds loser
RON_DOUBLE_DOWN_USD: float = 1.0  # Strategy 5b: additional USDC to simulate buying more of the LOSER at winner TP time; 0=disabled

# ── Strategy 5 — Opening Neutral ──────────────────────────────────────────
# Market types the scanner watches for simultaneous YES+NO entry opportunities.
# All bucket types are included — the _is_updown_market() filter ensures only
# Up/Down direction markets are entered regardless of bucket size.
OPENING_NEUTRAL_MARKET_TYPES: list = [
    "bucket_5m"#, "bucket_15m" , "bucket_1h", "bucket_4h"
]
# How long after open to keep a market in pending state / LIMIT-mode fill timeout.
# Not an entry gate — entries are pre-market only (timer path). This controls
# how long stale markets stay in _pending_markets before being pruned.
OPENING_NEUTRAL_MARKET_WINDOW_SECS: int = 60
# Maximum combined cost for YES + NO (≤ 1.0 = guaranteed profit at resolution;
# ≤ 1.01 allows 1-tick slip / fee headroom).
OPENING_NEUTRAL_COMBINED_COST_MAX: float = 1.01
# USDC notional per leg (YES position = this; NO position = this).
OPENING_NEUTRAL_SIZE_USD: float = 1
# Order type for the entry BUY legs: "limit" (post-only at current ask — preferred)
# or "market" (cross immediately; use when fills are hard to get at open).
# The loser-exit SELL is always a resting limit order regardless of this setting.
OPENING_NEUTRAL_ORDER_TYPE: str = "market"
# Seconds to wait for an entry fill before abandoning the attempt.
OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS: int = 30
# Seconds to wait for a WS fill confirmation on each FAK leg.
# FAK orders are fill-or-kill at the exchange: if a fill WS event hasn't arrived
# within this window the order was killed (no match) and the leg is treated as
# unfilled.  Kept short so the one-leg decision is made in seconds, not 30s.
OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS: int = 10
# FAK slippage cap (in price units, i.e. dollars-per-share) added to the observed
# best ask when sending the FAK BUY.  A wider cap lets the order sweep through to
# the next price level when the top-of-book ask gets swept in the millisecond
# window between book-cache snapshot and matcher arrival — the dominant cause of
# one-leg-fill (see bot.log 11:09:58: YES leg killed with "no orders found to
# match" while NO leg filled at the same instant).  Pays a few extra ticks on
# entry but completes the pair.
OPENING_NEUTRAL_FAK_SLIPPAGE_CAP: float = 0.01
# Per-leg book-depth safety multiplier for the thin-book entry gate.  Both YES
# and NO must show resting ask size >= MULT * required_contracts in the WS book
# cache before the FAK is sent.  >1.0 protects against a partial sweep between
# snapshot and matcher arrival killing one leg.
OPENING_NEUTRAL_DEPTH_MARGIN_MULT: float = 2.0
# What to do when only one leg fills within the timeout.
# "keep_as_momentum" — leave the filled leg running as a momentum position.
# "exit_immediately" — taker-exit the filled leg at best bid.
OPENING_NEUTRAL_ONE_LEG_FALLBACK: str = "keep_as_momentum"
# Exit price for the losing leg: resting GTC SELL placed on both sides immediately
# after entry at this price.  Whichever fills first is the loser.
# Net pair P&L = exit_price + $1.00 − 2×entry.
OPENING_NEUTRAL_LOSER_EXIT_PRICE: float = 0.35
# Bid-monitor trigger price: the exit task fires when best_bid <= this value.
# Setting this slightly above LOSER_EXIT_PRICE (e.g. 0.38) acts as a buffer
# against CLOB discreteness — the loser bid can jump from $0.40 to $0.22
# with no tick at $0.35, so triggering at $0.38 captures value before the gap.
# Must be >= LOSER_EXIT_PRICE.  Set equal to LOSER_EXIT_PRICE to disable.
OPENING_NEUTRAL_LOSER_EXIT_TRIGGER: float = 0.38
# Minimum seconds to hold both legs before the bid-monitor loser exit can fire.
# Prevents early "false loser" exits: at T+1s YES/NO bids are statistically
# indistinguishable (REPORT.md §7.6).  A 30s hold aligns with the T+30s
# divergence window where a 526bp gap separates true losers from reversals.
OPENING_NEUTRAL_MIN_HOLD_SECS: float = 30.0
# Seconds to wait for either resting exit SELL to fill before cancelling both orders.
# Should cover the full market duration (5m market = 300 s).
OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS: int = 300
# Seconds before market open to pre-warm the CLOB HTTP connection pool (idea 5).
# A lightweight GET is sent this many seconds before open so the TCP connection
# is established before the BUY orders fire.
OPENING_NEUTRAL_PREWARM_SECS: float = 10.2
# Seconds before market open to fire the scheduled entry timer (idea 1).
# Pre-market books are live and fillable, so firing the FAK 10 s before open
# catches the resting ask while it is undisturbed — eliminating the T=0 race
# where other bots drain the ask in the same millisecond window.
OPENING_NEUTRAL_TIMER_ADVANCE_SECS: float = 10.0
# Per-side price band: both YES ask and NO ask must be within [MIN, MAX] for
# an entry to qualify.  Keeps the strategy truly neutral (near 50/50) and
# prevents entries into highly-skewed markets (e.g. YES=0.12 / NO=0.89)
# where the exit logic breaks down and one leg is almost certain to lose.
OPENING_NEUTRAL_MIN_SIDE_PRICE: float = 0.49
OPENING_NEUTRAL_MAX_SIDE_PRICE: float = 0.51
# Cold-book spread gate (ON-01): skip entry when either leg's spread (ask − bid)
# exceeds this threshold.  A wide spread at open indicates thin liquidity where
# the loser bid-monitoring exit may never trigger before resolution.
# Set ENABLED=False during early calibration to log spreads without rejecting entries.
OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED: bool = True
OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD: float = 0.15
# Maximum simultaneous opening-neutral pairs.
OPENING_NEUTRAL_MAX_CONCURRENT: int = 1

# ── Phase 1 — Asymmetric Sell Triggers (ON-04) ───────────────────────────────
# Ship disabled. Enable only after ≥ 2 weeks of symmetric paper-fill data
# confirms (a) the winner bid never dips below (LOSER_EXIT_TRIGGER - buffer)
# intraday, and (b) the loser reaches the trigger at least 30% faster.
# strategy_update.md §0.3 and PRD ON-04 are the source of truth for enablement.
#
# Bid-monitor semantics:
#   Predicted loser: monitored at LOSER_EXIT_TRIGGER (standard).
#   Predicted winner: monitored at LOSER_EXIT_TRIGGER - WINNER_SELL_BUFFER so
#     its bid must fall further before a loser-exit fires — protecting against
#     accidental early exits on intraday noise.
#
# Funding semantics (from strategy_update.md §0.3 data):
#   funding_rate > threshold  →  YES is likely loser (NO wins 62.3%)
#   funding_rate < -threshold →  NO is likely loser (YES wins 76.2%)
OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED: bool = True
OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD: float = 0.00001
OPENING_NEUTRAL_WINNER_SELL_BUFFER: float = 0.03

# ── Phase 1 — Loser Confidence Scoring (ON-05) ───────────────────────────────
# Ship disabled. Additive on top of ON-04; either feature can be enabled alone.
# When |score| >= 2 (funding and depth share agree on the loser), the predicted
# loser's bid-monitor trigger is raised by TIGHTEN so the market sell fires
# sooner, freeing capital faster.
# Score convention (strategy_update.md §0.4 data):
#   +1 per signal that predicts YES as loser:
#     funding > threshold       → YES loser (NO wins 62.3%)
#     depth_share < 0.25        → YES loser (YES wins only 41.5%)
#   -1 per signal that predicts NO as loser:
#     funding < -threshold      → NO loser (YES wins 76.2%)
#     depth_share > 0.75        → NO loser (YES wins 60.0%)
#   |score| >= 2 → both signals agree → apply TIGHTEN to predicted loser trigger.
OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED: bool = False
OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN: float = 0.02

# ── Oracle delta gate (ON-06) ─────────────────────────────────────────────────
# When enabled, the bid-monitor loser exit is only allowed to fire when the
# oracle spot confirms the position is losing (delta ≤ 0).  Suppresses
# false-positive exits caused by CLOB book-drain at settlement: market makers
# pull bids in the final seconds, collapsing best_bid to 0.29–0.38 on both
# legs simultaneously regardless of which side will win.
#
# Oracle delta for bucket UP/DOWN markets:
#   YES/UP: delta = (spot - strike) / strike * 100  — positive = YES winning
#   NO/DOWN: delta = (strike - spot) / strike * 100 — positive = NO winning
#
# Fallback policy when oracle is unavailable (spot=None) or strike=0:
#   allow_exit — fire loser exit as before (safe: avoids holding true losers
#                to $0 if oracle is stale at settlement).
#   suppress   — hold; risky if oracle is perpetually stale.
# Default: allow_exit (conservative, matches pre-gate behaviour on oracle failure).
OPENING_NEUTRAL_ORACLE_DELTA_GATE_ENABLED: bool = True
OPENING_NEUTRAL_ORACLE_DELTA_GATE_FALLBACK: str = "allow_exit"  # "allow_exit" | "suppress"

# ── Winner confirmation gate (ON-07) ──────────────────────────────────────────
# Before firing a loser exit, the OTHER leg's best bid must be at or above
# this floor to confirm it is the genuine winner.  When both tokens are still
# near $0.50 (market hasn't decided direction) a trigger-below-floor dip is
# noise — wait for the next tick.
# Set to 0.0 to disable (matches pre-gate behaviour).
# Recommended live value: 0.60 (requires the winner to have diverged by at
# least 10 cents from neutral before the loser exit fires).
OPENING_NEUTRAL_WINNER_CONFIRM_FLOOR: float = 0.0

# DRY_RUN: when True all order placements are skipped (no real orders sent).
# Signals, pair tracking, and all logic run normally — only the pm_client calls
# are suppressed.  Safe to deploy inactive; set False after validation.``
OPENING_NEUTRAL_DRY_RUN: bool = True
# Take-profit for the promoted winner leg.
# When enabled, after the loser exits the bot places a resting SELL limit on
# the winner at a price calculated to earn OPENING_NEUTRAL_TP_PROFIT_PCT on
# the total combined cost of both legs (net of the loser proceeds already
# received).  Formula:
#   tp_price = combined_cost * (1 + TP_PROFIT_PCT) - loser_exit_price
# If the calculated price exceeds the tick-adjusted max (0.99), it is capped
# there and the position will settle at resolution instead.
# Prob-SL and delta-SL remain active and fire before the TP if spot reverses.
OPENING_NEUTRAL_TP_ENABLED: bool = True
OPENING_NEUTRAL_TP_PROFIT_PCT: float = 0.30  # 30% profit on combined cost
# When True (default), the winner leg is promoted to the Momentum strategy after
# the loser exits; momentum SL / TP / delta-SL / prob-SL all apply from that
# point.  When False the winner stays as strategy="opening_neutral" and is held
# until the market resolves — no stop-loss or take-profit is applied.
OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM: bool = True

# ── Strategy 3 — Momentum Scanner ─────────────────────────────────────────
# Price band: scanner fires when held-side is in [LOW, HIGH].
MOMENTUM_PRICE_BAND_LOW: float = 0.80
MOMENTUM_PRICE_BAND_HIGH: float = 0.90

# Maximum USDC deployed per momentum position.
MOMENTUM_MAX_ENTRY_USD: float = 50.0
# ── Fractional Kelly position sizing ──────────────────────────────────────
# Replaces the old edge_pct anchor approach.  Kelly Criterion sizes each bet
# in proportion to the mathematical edge: how much better is our win probability
# than what the odds imply?
#
# How it works:
#   1. win_prob = N(observed_z_total) — the probability the underlying
#      finishes on the winning side, estimated from our vol model.
#   2. payout_b = (1 - token_price) / token_price — how many dollars you
#      win for every dollar you risk (e.g. buying at 0.85 → b = 0.18).
#   3. Full Kelly fraction = max(0, (win_prob × b − lose_prob) / b)
#      This is what a perfect model would bet as a fraction of the bankroll.
#   4. We scale by MOMENTUM_KELLY_FRACTION (a safety multiplier 0–1):
#        size = MAX_ENTRY × min(1, kelly_f × KELLY_FRACTION)
#      At KELLY_FRACTION=1.0, size = kelly_f × MAX_ENTRY directly.
#      Lowering it shrinks every bet proportionally without changing signal rank.
#
# Natural behaviour (all three rules without explicit knobs):
#   • Stronger signal   → higher win_prob → larger kelly_f → bigger size
#   • Larger gap        → higher win_prob (more σ above zero) → bigger size
#   • Higher band price → smaller payout_b → smaller kelly_f → smaller size
#
# MOMENTUM_KELLY_FRACTION calibration (safety multiplier applied to kelly_f):
#   1.00 — full Kelly relative to MAX_ENTRY (default).
#           size = kelly_f × MAX_ENTRY.  e.g. kelly_f=0.85 → $42.50 at $50 max.
#   0.50 — half-Kelly: size = 0.5 × kelly_f × MAX_ENTRY. Lower variance.
#   0.25 — quarter-Kelly: most conservative; max effective bet = 25% of MAX.
MOMENTUM_KELLY_FRACTION: float = 1.0     # safety multiplier on kelly_f (0 < x ≤ 1.0)
MOMENTUM_MIN_ENTRY_USD: float = 1.0      # floor to avoid dust orders

# Kelly-specific minimum effective TTE.  Prevents sigma_tau from collapsing at
# very small TTEs (e.g. 3s, 5s), which inflates z → 6σ hard cap → win_prob ≈ 1.0
# → MAX_ENTRY on every signal regardless of actual edge.
#
# This is NOT the entry-gate ceiling (MOMENTUM_MIN_TTE_SECONDS): we still enter
# markets at any TTE within the gate.  This floor only affects how confident Kelly
# is — a signal fired with 3s left is sized as if MOMENTUM_KELLY_MIN_TTE_SECONDS
# seconds remain, preventing overbetting while keeping the correct direction.
#
# Rule of thumb: ~50% of the entry-gate window (bucket_5m gate=60s → floor=30s).
MOMENTUM_KELLY_MIN_TTE_SECONDS: int = 30

# Per-bucket Kelly multiplier — applied after the fractional-Kelly fraction to
# further dampen position sizes on short-duration buckets where the TTE floor
# alone does not fully prevent over-sizing.
#
# Why this is needed:
#   Even with MOMENTUM_KELLY_MIN_TTE_SECONDS=30, most 5m signals fire in the
#   last 10–25 seconds.  sigma_tau at 30s effective TTE is still very small,
#   so observed_z is large → high win_prob → Kelly still wants full MAX_ENTRY.
#   The multiplier applies a structural cap that the vol model cannot compute
#   on its own because it has no knowledge of the bucket's typical noise regime.
#
# Calibration:
#   bucket_5m  0.45 — strong dampening; 5m markets are noisy & near-expiry
#   bucket_15m 0.70 — moderate dampening
#   bucket_1h+ 1.00 — no dampening; longer TTEs have reliable vol estimates
#   Unlisted bucket types default to 1.0 (no dampening).
MOMENTUM_KELLY_MULTIPLIER_BY_TYPE: dict[str, float] = {
    "bucket_5m":    0.45,
    "bucket_15m":   0.70,
    "bucket_1h":    0.90,
    "bucket_4h":    1.00,
    "bucket_daily": 1.00,
    "bucket_weekly": 1.00,
}

# ── CLOB-Oracle Blend (COB) win-probability for Kelly sizing ──────────────
# The vol-model win_prob = N(z) collapses near expiry (sigma_tau → 0 → z → ∞
# → win_prob ≈ 1.0 on every signal).  The CLOB ask price is reliable when the
# book is liquid but drains near expiry (bid → 0.01, mid unreliable).
# COB blends two independent estimates, shifting weight from CLOB to oracle-
# delta as TTE shrinks, so neither failure mode dominates sizing:
#
#   win_prob_clob   = min(ask_price + EDGE_PREMIUM, WIN_PROB_CAP)
#                     market consensus; reliable when book has depth.
#
#   win_prob_oracle = 0.50 + min(0.45, (strength − 1) × ORACLE_SENSITIVITY)
#                     where strength = delta_pct / threshold_pct.
#                     Purely physical distance from strike; stable near expiry.
#
#   clob_weight     = min(1.0, tte_seconds / CLOB_RELIABLE_TTE)
#   win_prob_eff    = clob_weight × win_prob_clob + (1−clob_weight) × win_prob_oracle
#
# EDGE_PREMIUM: our systematic alpha over the CLOB ask (calibrate from win rate data).
# WIN_PROB_CAP: hard ceiling on win_prob_eff to prevent Kelly runaway.
# CLOB_RELIABLE_TTE: TTE above which CLOB book is considered fully reliable.
# ORACLE_SENSITIVITY: maps signal-strength multiples above threshold → win_prob slope.
MOMENTUM_KELLY_EDGE_PREMIUM: float = 0.07        # alpha above CLOB ask price
MOMENTUM_KELLY_WIN_PROB_CAP: float = 0.95        # hard cap on win_prob_eff
# When win_prob reaches the cap (0.95), position size is additionally capped at
# this USD value.  0.0 = disabled.  At-cap entries have the same win rate as
# below-cap entries but produce larger losses when they fail (avg -$0.88 vs -$0.03).
# Applies after all other kelly math; MIN_ENTRY_USD floor is still respected.
MOMENTUM_KELLY_AT_CAP_MAX_USD: float = 5
MOMENTUM_KELLY_CLOB_RELIABLE_TTE: int = 60       # seconds; above this CLOB fully weighted
MOMENTUM_KELLY_ORACLE_SENSITIVITY: float = 0.15  # slope: delta multiples → win_prob
# Per-bucket floor on _edge_scale.  0.0 = no change (default for all types).
# Set e.g. {"bucket_5m": 0.3} to ensure 5m threshold signals claim 30% of the
# edge premium instead of 0% — preventing kelly_f from collapsing to zero on
# signals that just clear the entry gate.
MOMENTUM_KELLY_EDGE_SCALE_BASE_BY_TYPE: dict[str, float] = {}

# Z-score piecewise multiplier on kelly_f.  Empty list = no scaling (default).
# Each element is [z_min, multiplier].  The multiplier for a signal is taken
# from the entry with the highest z_min that is still <= signal_obs_z.
# Example: [[0.0, 0.5], [1.5, 1.0], [2.0, 1.25]]
#   z < 1.5        → 0.5×  (reduce sizing on weak signals)
#   1.5 ≤ z < 2.0  → 1.0×  (no change)
#   z ≥ 2.0        → 1.25× (boost sizing on strong signals)
# The final kelly_f is still clamped to [0, 1] and subject to
# MOMENTUM_MAX_ENTRY_USD and MOMENTUM_MIN_ENTRY_USD floors.
MOMENTUM_KELLY_Z_SCORE_MULTIPLIER: list[list] = []

# Kelly Phase-A extension — persistence z-boost.

# Minimum USDC depth on the ask side within 1c of best ask (thin-book guard).
# Prevents entering markets where our order would exhaust available liquidity.
MOMENTUM_MIN_CLOB_DEPTH: float = 200.0

# ── M-10: Funding Rate Entry Gate ────────────────────────────────────────────
# Block entry when HL perpetual funding rate signals market structure opposed to
# the prediction-market side.  High positive funding = longs paying shorts (price
# biased DOWN); high negative funding = shorts paying longs (price biased UP).
#
# MOMENTUM_FUNDING_GATE_YES_MAX: skip YES/UP entry when funding > this value
#   (market is paying longs → expensive to hold long, bearish signal)
# MOMENTUM_FUNDING_GATE_NO_MIN: skip NO/DOWN entry when funding < this value
#   (market is paying shorts → expensive to hold short, bullish signal)
# Values are expressed as HL's per-8h rate (e.g. 0.00001 = 0.001% per 8h).
MOMENTUM_FUNDING_GATE_ENABLED: bool = True
MOMENTUM_FUNDING_GATE_YES_MAX: float = 0.00001   # ~0.001%/8h; block YES when funding > this
MOMENTUM_FUNDING_GATE_NO_MIN: float = -0.00001   # ~-0.001%/8h; block NO when funding < this

# ── M-11: Depth Share Entry Gate ─────────────────────────────────────────────
# yes_depth_share = YES-side depth / (YES-side + NO-side depth).
# Low share for YES signals the market is not supporting the UP direction.
# Validated for YES/UP (AUC=0.5683, p=0.002, Q1<25% → 41.5% win rate).
# NO/DOWN gate is inferred by symmetry — not independently validated.
# Fail-open when get_depth_share() returns None.
MOMENTUM_DEPTH_SHARE_GATE_ENABLED: bool = True
MOMENTUM_DEPTH_SHARE_YES_MIN: float = 0.40   # skip YES entry when yes_depth_share < 0.40
MOMENTUM_DEPTH_SHARE_NO_MAX: float = 0.60    # skip NO entry when yes_depth_share > 0.60

# ── M-15: HL Perp Depth Imbalance Entry Gate ─────────────────────────────────
# Block entry when the HL perp book is heavily positioned against the trade.
# Raw imbalance: +1 = all bids, -1 = all asks. Position-adjusted so that
# negative values always mean "market positioned against this trade".
# Analysis (77 trades, 2026-05-19): entry imbalance < -0.30 → 50% WR vs 70.7%
# for the rest (+9.7pp). XRP excluded — imbalance is inverted for that coin.
# Source: analysis/hl_gate_overlap.py (2026-05-19).
# Fail-open when HL WS not connected (returns None).
MOMENTUM_HL_ENTRY_GATE_ENABLED: bool = False
MOMENTUM_HL_ENTRY_IMBALANCE_MIN: float = -0.30   # block entry when position-adj imbalance < this
MOMENTUM_HL_ENTRY_GATE_EXCLUDE_COINS: list = ["XRP"]  # coins exempt from this gate (inverted signal)

# ── M-14: TWAP Deviation Entry Gate ──────────────────────────────────────────
# YES/UP entries only (NO/DOWN not validated — see PRD M-14).
# In LOW volatility regime: if oracle is below its 10s TWAP by this many bps,
# raise the z-bar by the multiplier. Soft gate — strong delta can still pass.
# Source: strategy_update.md §1.6. 37.6% YES win rate in low-vol + TWAP dev < 0.
MOMENTUM_TWAP_GATE_ENABLED: bool = True
MOMENTUM_TWAP_DEV_THRESHOLD_BPS: float = -5.0        # dev below this (oracle below TWAP) triggers multiplier
MOMENTUM_TWAP_DEV_LOW_VOL_YES_MULTIPLIER: float = 1.4  # raises YES/UP z-bar in low-vol + neg TWAP dev
# Hard gate: block entry in LOW vol regime when twap_dev_bps is unavailable.
# LOW vol + NaN TWAP observed 50% win rate vs 91% when TWAP is present.
# The TWAP multiplier protection (M-14) cannot fire without data — fail-closed.
MOMENTUM_TWAP_REQUIRE_DATA_LOW_VOL: bool = True

# ── M-13: cl_upfrac rolling-window exit ──────────────────────────────────────
# Rolling up-tick fraction over UPFRAC_WINDOW_SECONDS. Sampled once per window.
# For YES positions: exit when rolling fraction stays below threshold for
# UPFRAC_EXIT_WINDOWS consecutive windows (= WINDOWS × WINDOW_SECONDS minimum).
# Source: strategy_update.md §1.7. AUC = 0.703 (strongest signal in dataset).
MOMENTUM_UPFRAC_EXIT_ENABLED: bool = True
MOMENTUM_UPFRAC_EXIT_THRESHOLD: float = 0.40    # exit YES when frac < this; exit NO when frac > (1 - this)
MOMENTUM_UPFRAC_EXIT_WINDOWS: int = 2            # consecutive below-threshold windows before exit fires
MOMENTUM_UPFRAC_WINDOW_SECONDS: int = 5          # duration of each measurement window in seconds
MOMENTUM_UPFRAC_EWMA_ALPHA: float = 0.3          # smoothing factor for up-fraction EWMA (higher = more reactive)
MOMENTUM_UPFRAC_SUPPRESS_UNTIL_ENTRY_WINDOW: bool = True  # when True: upfrac exit is suppressed while TTE > MOMENTUM_MIN_TTE_SECONDS[market_type] (guards against stale pre-promotion EWMA on ON-promoted positions)

# Delta SL post-open grace window (seconds).  Delta SL is suppressed while BOTH
# the position age is below this threshold AND TTE is above the entry window.
# Whichever condition clears first ends the suppression.  Caps the blind spot for
# ON-promoted positions at 60s instead of the previous TTE-based 300-400s gap.
MOMENTUM_DELTA_SL_GRACE_SECS: int = 60

# WINNER-fill-type (ON-promoted) positions use a wider grace window and a looser
# delta SL threshold.  WINNER positions are promoted mid-bucket (avg t+47s) and
# have full remaining TTE — the standard 1% per-coin SL fires on normal mid-bucket
# oscillation rather than genuine reversals.
# MOMENTUM_WINNER_DELTA_SL_GRACE_SECS: grace window (seconds) for WINNER positions.
# MOMENTUM_WINNER_DELTA_SL_MULTIPLIER: fraction applied to the per-coin SL threshold.
#   e.g. 0.5 → BTC WINNER SL = 1% × 0.5 = 0.5% (vs 1% for MAIN positions).
MOMENTUM_WINNER_DELTA_SL_GRACE_SECS: int = 60  # set to 150 in config_overrides.json
MOMENTUM_WINNER_DELTA_SL_MULTIPLIER: float = 1.0  # set to 0.5 in config_overrides.json

# Order type: "limit" = taker limit at ask+0.5c (ensures fill); "market" = immediate cross.
MOMENTUM_ORDER_TYPE: str = "limit"

# Exit thresholds — based on the underlying spot (not CLOB binary price).
# Delta-based stop-loss: exit when live HL spot has moved this % past the
# strike against the position (e.g. 0.05 → exit when spot is 0.05% below
# strike for YES, or 0.05% above strike for NO).
MOMENTUM_DELTA_STOP_LOSS_PCT: float = 0.01  # protective buffer: exit when delta (in-the-money %) drops BELOW this threshold (fires before strike is crossed)
MOMENTUM_DELTA_SL_MIN_TICKS: int = 3        # hysteresis: delta SL only fires after this many consecutive below-threshold ticks (prevents single-tick noise from triggering exit)
# Token price veto for delta SL: if the held token's CLOB mid is still above this
# price when the oracle delta retreats below the threshold, the delta SL is suppressed.
# Rationale: when the CLOB crowd has not moved (token still ~0.55+) but the oracle
# shows a transient sub-threshold tick, the move is likely noise.  This is a veto
# (suppression), not a trigger — the SL still fires if the token also reprices down.
# Set to 0.0 to disable.  Only applied when current_token_price is available.
MOMENTUM_DELTA_SL_TOKEN_VETO_FLOOR: float = 0.0  # disabled by default; set via override

# ── Early Warning SL: HL Mark Price Divergence ───────────────────────────────
# Independent SL signal: fires when the HL perp mark price crosses the strike
# while the Chainlink oracle is still above it.  The perp CLOB leads the
# oracle by 2-5 seconds on real directional moves.  All-off by default.
MOMENTUM_HL_MARK_SL_ENABLED:       bool  = False
MOMENTUM_HL_MARK_SL_THRESHOLD_PCT: float = 0.0   # fire when mark divergence < this (0.0 = mark crossed strike; negative = allow slack)
MOMENTUM_HL_MARK_SL_MAX_TTE:       int   = 30    # only active when tte_seconds < this

# ── Early Warning SL: HL Perp Depth Imbalance ────────────────────────────────
# Independent SL signal: fires when the HL perp book is heavily positioned
# against this trade (asks >> bids for UP, bids >> asks for DOWN).
MOMENTUM_HL_DEPTH_SL_ENABLED:             bool  = False
MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD: float = 0.40  # fire when position-adjusted imbalance < -threshold
MOMENTUM_HL_DEPTH_SL_MAX_TTE:             int   = 30    # only active when tte_seconds < this
MOMENTUM_HL_DEPTH_SL_LEVELS:              int   = 5     # order book levels to include in depth sum

MOMENTUM_TAKE_PROFIT: float = 0.999         # Exit if held token rises above this
# High-probability taker-exit suppression: when the held token's CLOB mid is AT OR
# ABOVE this price all stop-loss taker exits (delta SL, near-expiry time stop, prob
# SL, upfrac) are suppressed and the position is held to settlement.  The take-profit
# (MOMENTUM_TAKE_PROFIT ≈ 0.999) is never suppressed.
# Rationale: at 90c+ the crowd has priced a near-certain win; any taker exit forfeits
# expected value.  Set to 0.0 to disable.
MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE: float = 0.92
# How long to wait for PM API to confirm settlement before falling back to the
# resolution oracle.  PM can take 1–10 min to flip closed=True on the CLOB API
# for short-duration (5m/15m) bucket markets.  After this many seconds past
# end_date, _check_position_exit() closes the position using the oracle spot vs
# strike comparison — which is authoritative for bucket markets.  In live mode
# _auto_redeem_loop() remains the final safety-net and will correct any
# oracle-estimation error once PM confirms the real payout.
# In paper-trading mode _auto_redeem_loop() is disabled, so this timeout IS the
# final fallback.
MOMENTUM_RESOLVED_FORCE_CLOSE_SEC: int = 300  # seconds past end_date before oracle fallback

# Near-expiry time-stop: when TTE is very short and spot has already crossed
# the strike (delta < 0), exit via taker to avoid a binary snap to zero.
MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS: int = 90        # TTE threshold (seconds)
# Within this many seconds of expiry the suppress_taker_exits gate is bypassed
# for the near-expiry check regardless of token price.  At TTE < 30s even a
# 0.92 token is at risk of a terminal collapse (as seen in the ETH DOWN case).
# Set to 0 to disable (suppress always applies up to the time-stop threshold).
MOMENTUM_NEAR_EXPIRY_SUPPRESS_BYPASS_TTE: int = 30

# Phase B — Two-oracle strategy: near-expiry delta SL uses only the on-chain
# AggregatorV3 feed (ChainlinkWSClient) instead of freshest-wins (which normally
# picks the RTDS relay).  This matches Polymarket's resolution contract, which
# calls latestRoundData() at expiry — not the Data Streams relay feed.
# Default False: validate by cross-checking oracle_ticks.csv against resolved
# outcomes before enabling in production.
MOMENTUM_USE_RESOLUTION_ORACLE_NEAR_EXPIRY: bool = True

# Entry window per bucket market type — only enter when TTE ≤ this value.
# Markets with MORE time remaining than this are outside the entry window and
# are skipped.  Subscribe to them early so book data is ready when the window
# opens; the scan itself enforces the ceiling gate.
# Example: bucket_daily = 900  →  only enter daily markets in the LAST 15 min.
MOMENTUM_MIN_TTE_SECONDS: dict[str, int] = {
    "bucket_5m":      30,    # last 30 s  (~last 10% of a 300 s market)
    "bucket_15m":     60,    # last 60 s
    "bucket_1h":     120,    # last 2 min
    "bucket_4h":     300,    # last 5 min
    "bucket_daily":  900,    # last 15 min
    "bucket_weekly": 3_600,  # last 1 hour
    "milestone":     1_800,  # last 30 min (no fixed duration)
}
# Fallback for any market type not listed above.
MOMENTUM_MIN_TTE_SECONDS_DEFAULT: int = 120

# Staleness guards: discard signals if price data is older than these thresholds.
MOMENTUM_SPOT_MAX_AGE_SECS: float = 30.0   # HL BBO age (seconds)
MOMENTUM_BOOK_MAX_AGE_SECS: float = 120.0  # PM book age gate: skip market if book is older than this (WS shard outage).

# Volatility source / threshold config.
# MOMENTUM_VOL_CACHE_TTL: Deribit ATM IV is cached this many seconds before re-fetch.
# MOMENTUM_VOL_Z_SCORE: z statistic for the required delta (1.6449 ≈ 95th percentile).
#   Raise toward 2.0 for fewer, higher-conviction entries.
#   Lower toward 1.28 for more trades in low-vol regimes.
MOMENTUM_VOL_CACHE_TTL: float = 300.0
MOMENTUM_VOL_Z_SCORE: float = 1.6449
# Per-bucket z-score overrides. Any bucket type listed here overrides
# MOMENTUM_VOL_Z_SCORE for that bucket; unlisted buckets use the global default.
# Example: {"bucket_daily": 1.0, "bucket_15m": 1.3}
MOMENTUM_VOL_Z_SCORE_BY_TYPE: dict[str, float] = {}

# Minimum absolute spot displacement required regardless of how small the
# vol-based threshold has collapsed near expiry.  0.0 = disabled (default).
# Example: 0.05 means the spot price must have moved at least 0.05% above (YES)
# or below (NO) the strike; signals smaller than this are ignored even if they
# technically exceed the vol-scaled z-threshold.
MOMENTUM_MIN_DELTA_PCT: float = 0.0
# Per-coin overrides for MOMENTUM_MIN_DELTA_PCT.  If a coin is listed here its
# value replaces the global floor; unlisted coins use MOMENTUM_MIN_DELTA_PCT.
MOMENTUM_MIN_DELTA_PCT_BY_COIN: dict[str, float] = {}
# Per-bucket-type overrides for MOMENTUM_MIN_DELTA_PCT.  If a bucket type is
# listed here its value is compared with the coin floor and the HIGHER of the
# two is used as the effective floor.  Unlisted bucket types fall back to the
# coin floor (or global default if that is also absent).
# Example: {"bucket_5m": 0.10, "bucket_15m": 0.08}
MOMENTUM_MIN_DELTA_PCT_BY_TYPE: dict[str, float] = {}

# Per-coin overrides for MOMENTUM_DELTA_STOP_LOSS_PCT.  Higher-IV coins (DOGE,
# SOL, HYPE) need a wider stop because a single oracle tick routinely exceeds
# the global 0.04% threshold.  Falls back to MOMENTUM_DELTA_STOP_LOSS_PCT when
# the coin is not listed.  See PER_COIN_CONFIG.md for calibration rationale.
MOMENTUM_DELTA_SL_PCT_BY_COIN: dict[str, float] = {}

# Minimum additional gap required above the vol-scaled threshold (percentage
# points of |(spot - strike) / strike|).  Prevents marginal signals where the
# spot-to-strike gap barely clears the threshold and a single adverse tick is
# enough to flip the position from winning to losing before expiry.
# 0.0 = disabled (default).  Recommended live value: 0.02 (see config_overrides).
MOMENTUM_MIN_GAP_PCT: float = 0.0

# Phase C — Minimum elapsed-time guard: suppress entries fired in the first N
# seconds of a market window (thin book, wide spreads, noisy ticks).
# Empty dict = disabled for all types.  Per-type example: {"bucket_5m": 30}.
MOMENTUM_PHASE_C_MIN_TTE_SECONDS: dict[str, int] = {}  # 0 = OFF; N = block entries in last N s

# ── Logging toggles (post-trade analysis files) ───────────────────────────────
# These CSV files are only needed for post-trade analysis and calibration.
# Disable them when running in production to save disk space and improve I/O
# performance.  Both default to True so analysis data is collected by default.
MOMENTUM_TICKS_LOG_ENABLED: bool = True         # momentum_ticks.csv   — intra-hold price ticks

# How often to run a full scan pass over all bucket markets (seconds).
MOMENTUM_SCAN_INTERVAL: int = 1

# Maximum simultaneous open momentum positions.
MOMENTUM_MAX_CONCURRENT: int = 20

# Pre-subscription lookahead: also subscribe to bucket markets that haven't
# started yet but are within this many additional durations of their start.
# 0 = only started markets (safe minimum).
# 4 = also subscribe to the next 4 bucket slots ahead (e.g. next 20 min of 5-min
#     buckets, next 1h of 15-min buckets, etc.).  Bounded — adds ~40 extra tokens
#     per scan cycle, not thousands.  Useful for data collection to capture
#     price-vs-TTE curves before the entry window opens.
MOMENTUM_PRESUB_LOOKAHEAD: int = 4

# How many days of bucket markets the momentum scanner subscribes to via PM WS,
# independently of the maker's MAKER_MAX_TTE_DAYS window.  Wider than the maker
# window so we can watch markets that are "near certain" but not yet in the
# maker's quoting horizon.  Increase if 76% no_book persists after market refresh.
MOMENTUM_MAX_TTE_DAYS: int = 7

# GTD hedge order management (gap-closing reprice + near-expiry cancel).
# MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: if TTE ≤ this when the monitor sweeps,
#   and the held token's CLOB mid is above 0.50 (winning), cancel the open
#   hedge order to prevent an adverse fill at expiry.  0 = disabled.
MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: int = 5
# MOMENTUM_HEDGE_MIN_RETAIN_USD: when repricing a hedge, ensure the notional
#   cost of the new order does not exceed (projected_pnl - min_retain_usd).
#   Prevents paying more for the hedge than the expected net profit.
MOMENTUM_HEDGE_MIN_RETAIN_USD: float = 0.50
# MOMENTUM_HEDGE_CLOB_LOG_ENABLED: verbose per-sweep CLOB ask logging for
#   hedge orders.  Disable in tests and low-noise environments.
MOMENTUM_HEDGE_CLOB_LOG_ENABLED: bool = True

# ── Range markets (sub-strategy of Momentum) ─────────────────────────────────
# "Will BTC be between $X and $Y?" — YES resolves $1 if spot inside [lo, hi],
# NO resolves $1 if spot outside [lo, hi].  Treated as a regular single-leg
# momentum trade; the bidirectional delta formula uses both boundaries.
MOMENTUM_RANGE_ENABLED: bool = False  # Include range markets in momentum scans
# Per-range overrides (fall back to standard momentum values if not set separately):
MOMENTUM_RANGE_PRICE_BAND_LOW: float = 0.6    # Token price floor for range market entries
MOMENTUM_RANGE_PRICE_BAND_HIGH: float = 0.95  # Token price ceiling for range market entries
MOMENTUM_RANGE_MAX_ENTRY_USD: float = 25.0    # Max position size (USD) for range entries
MOMENTUM_RANGE_VOL_Z_SCORE: float = 0.8       # Vol z-score threshold for range market signals
MOMENTUM_RANGE_MIN_TTE_SECONDS: int = 300     # Minimum seconds to expiry for range entries

# ── Strategy 2 — Mispricing Scanner ─────────────────────────────────────────
MILESTONE_MIN_DAYS: int = 1           # Only scan markets resolving > 1 day away
MISPRICING_THRESHOLD: float = 0.05   # Flag if |PM - options_implied| > 5%
MISPRICING_EXTREME_THRESHOLD: float = 0.15  # Apply looser threshold at extremes
MISPRICING_SCAN_INTERVAL: int = 60   # seconds between full mispricing scans

# Entry filters (derived from overnight data analysis, 2026-03-12):
#   YES ≥ 0.90 → 0% win rate across all 78 trades; NO entries when YES is
#   near-certain are noise from the N(d₂) / barrier-pricing mismatch.
MAX_BUY_NO_YES_PRICE: float = 0.87   # Skip BUY_NO signals where YES ≥ this
MIN_BUY_YES_YES_PRICE: float = 0.13  # Skip BUY_YES signals where YES ≤ this (symmetric guard)

# Minimum distance between spot and strike as a fraction of spot.
# N(d₂) diverges most severely from the true barrier probability when spot is
# close to strike (near-the-money, short TTE). Data (2026-03-13, 3 trades):
#   $72k strike, spot $70,949 (+1.5%) → YES 70.5%, N(d2) 35.6% → -$31.87
#   $72k strike, spot $71,578 (+0.6%) → YES 86.5%, N(d2) 44%   → -$26.38
#   $74k strike, spot $70,949 (+4.3%) → YES 29%,   N(d2) 14.9% → -$27.30
# Setting to 8%: requires strike to be ≥ 8% above (or below) current spot.
MIN_STRIKE_DISTANCE_PCT: float = 0.08  # fraction of spot, e.g. 0.08 = 8%

# Per-market cooldown: once a signal fires for a market, suppress re-entry for
# this many seconds. Prevents duplicate orders within adjacent scans but is NOT
# the primary protection against persistent false signals - that is handled by
# MAX_BUY_NO_YES_PRICE. Retro analysis showed 30 min was over-filtering:
# price filter alone blocks the bad signals; short cooldown just deduplicates.
MISPRICING_MARKET_COOLDOWN_SECONDS: int = 300   # 5 minutes — mispricing strategy
MOMENTUM_MARKET_COOLDOWN_SECONDS: int = 60  # 30 minutes — momentum strategy

# ── Momentum: order cancel-and-retry (Item 2) ─────────────────────────────────
# After MOMENTUM_ORDER_CANCEL_SEC seconds without a fill, the unfilled limit
# order is cancelled and re-submitted at a higher price (chasing the market).
# Price steps up by MOMENTUM_BUY_RETRY_STEP each attempt, capped at
# entry_price * (1 + MOMENTUM_SLIPPAGE_CAP) or 0.995, whichever is lower.
MOMENTUM_ORDER_CANCEL_SEC: float = 8.0    # seconds to wait before cancelling unfilled entry
MOMENTUM_SLIPPAGE_CAP: float = 0.05       # max allowable slippage from initial signal price (5%)
MOMENTUM_MAX_RETRIES: int = 2             # max cancel-and-retry attempts per entry signal
MOMENTUM_BUY_RETRY_STEP: float = 0.01    # price step per retry (chase the market)

# ── Momentum: active TP resting limit order (Item 1) ─────────────────────────
# When MOMENTUM_TP_RESTING_ENABLED is True, a SELL limit order is pre-armed at
# MOMENTUM_TAKE_PROFIT immediately after the entry fill.  The order sits in the
# CLOB and fills automatically when the token converges to certainty, removing
# the monitor-latency round-trip for take-profit exits.
# Up to MOMENTUM_TP_RETRY_MAX placement attempts; price steps up by
# MOMENTUM_TP_RETRY_STEP each attempt (in case the initial price is crossed).
MOMENTUM_TP_RESTING_ENABLED: bool = True  # pre-arm SELL at TP level after entry fill
MOMENTUM_TP_RETRY_MAX: int = 3            # max placement attempts for the resting TP SELL
MOMENTUM_TP_RETRY_STEP: float = 0.005     # price step per TP placement retry

# ── Momentum: VWAP deviation + momentum RoC secondary filter (Item 4) ────────
# VWAP is computed over a MOMENTUM_VWAP_WINDOW_SEC rolling window from PM CLOB
# mid-price ticks, weighted by ask size as a volume proxy.
# Momentum RoC is the percentage price change over MOMENTUM_ROC_WINDOW_SEC.
# Both thresholds default to 0.0 (permissive / filter disabled) so they can be
# tuned from live data without blocking signals on first deployment.
MOMENTUM_VWAP_WINDOW_SEC: int = 30        # rolling window for VWAP computation (seconds)
MOMENTUM_ROC_WINDOW_SEC: int = 60         # rolling window for momentum RoC (seconds)
MOMENTUM_MIN_VWAP_DEV_PCT: float = 0.0   # min VWAP deviation % required (0 = disabled)
MOMENTUM_MIN_ROC_PCT: float = 0.0         # min momentum RoC % required  (0 = disabled)

# ── Momentum: probability-based stop-loss (Item 7) ────────────────────────────
# Complement to the oracle-delta SL.  Fires when the held token's CLOB price
# drops more than MOMENTUM_PROB_SL_PCT below the entry price, indicating the
# market has significantly repriced against the position regardless of the
# current oracle delta.  Both SL conditions are independent — the position
# exits on whichever fires first.
# Note: the canonical SL is the oracle-delta SL; CLOB-price SL is a secondary
# failsafe.  Keep MOMENTUM_PROB_SL_PCT large enough (≥0.10) to avoid false
# triggers from normal CLOB bid-ask noise.
MOMENTUM_PROB_SL_ENABLED: bool = True     # enable CLOB-price-based SL as complement
MOMENTUM_PROB_SL_PCT: float = 0.25        # SL fires when token drops 25% below entry
# Near-expiry guard: disable prob-based SL when TTE is below this value (seconds).
# In the final minutes before resolution, CLOB books drain — the best_bid collapses
# to the tick floor (0.01) while the best_ask remains near 1.0, making the mid an
# unreliable price signal.  The oracle-delta SL remains active and is the correct
# primary stop near expiry.  Set to 0 to disable the guard.
MOMENTUM_PROB_SL_MIN_TTE_SECS: int = 30  # suppress prob-SL in final 30 seconds
MOMENTUM_PROB_SL_ORACLE_STALE_SECS: float = 10.0  # suppress prob-SL when oracle ticked within this many seconds (0 = disabled)

# ── Chainlink watchdog (Item 5) ──────────────────────────────────────────────
# Seconds of silence on an established WebSocket connection before forcing a
# reconnect.  Reduced from 120 s to 30 s so a zombie TCP connection is detected
# within one 5m-bucket expiry window rather than two.
CHAINLINK_SILENCE_WATCHDOG_SECS: int = 30

# Maximum age of a ChainlinkStreams snapshot before SpotOracle falls through
# to the RTDS relay.  ChainlinkStreams pushes ~2–3 updates/sec per coin, so
# anything older than 3 s indicates a zombie feed (connected but no new data).
# SpotOracle switches back to ChainlinkStreams automatically the moment a fresh
# snapshot arrives — no sticky state.
CHAINLINK_STREAMS_STALE_SECS: float = 3.0

# ── Kalshi signal confirmation layer ──────────────────────────────────────
# When KALSHI_ENABLED=True, the scanner fetches matching Kalshi market prices
# and uses |PM − Kalshi| as the primary deviation signal. Markets with no Kalshi
# counterpart are skipped. When False, falls back to pure N(d₂) path.
# NOTE: Disabled because Kalshi API returns non-200 (credentials not configured),
# which blocks ALL mispricing signals.  Re-enable once API credentials are set.
KALSHI_ENABLED: bool = False

# When True, the Kalshi direction and N(d₂) direction must agree. Recommended
# while building track record; disable only once Kalshi-only mode is validated.
KALSHI_REQUIRE_ND2_CONFIRMATION: bool = True

# Minimum |PM − Kalshi| spread to generate a signal (filters noise / stale data).
KALSHI_MIN_DEVIATION: float = 0.03   # 3 cents

# Matching tolerances: how close the Kalshi market must be to the PM market.
KALSHI_MATCH_MAX_STRIKE_DIFF: float = 0.02   # 2% fractional strike distance
KALSHI_MATCH_MAX_EXPIRY_DAYS: float = 2.0    # ±2 calendar days expiry tolerance

# ── Risk ─────────────────────────────────────────────────────────────────────
MAX_PM_EXPOSURE_PER_MARKET: float = 500.0   # USD — matches MAKER_SPREAD_SIZE_MAX; one spread per market
MAX_TOTAL_PM_EXPOSURE: float = 2000.0       # USD
MAX_HL_NOTIONAL: float = 3000.0             # USD (hedges only)
MAX_CONCURRENT_POSITIONS: int = 12          # total backstop across all strategies; NOTE: overridden to 40 in config_overrides.json
# Strategy-specific concurrent position limits (Flaw §5: shared cap removed)
MAX_CONCURRENT_MAKER_POSITIONS: int = 8     # maker fills are short-lived, higher cap OK; NOTE: overridden to 20 in config_overrides.json
MAX_MAKER_POSITIONS_PER_UNDERLYING: int = 3  # cap correlated exposure per coin; NOTE: overridden to 16 in config_overrides.json
MAX_CONCURRENT_MISPRICING_POSITIONS: int = 3  # mispricing holds longer, stay conservative
HARD_STOP_DRAWDOWN: float = 500.0           # USD — halt all new orders if breached

# Fee model constants (do NOT use these as actual fee figures — use dynamic API fetch)
# These are only used for the min-edge calculation.
PM_FEE_COEFF: float = 0.0175
HL_TAKER_FEE: float = 0.00045       # 0.045%
EDGE_BUFFER: float = 0.002          # 0.2% risk/slippage buffer

# ── Signal Scoring ──────────────────────────────────────────────────────────
# Minimum score (0–100) to be considered for entry.  0.0 = no filter.
MIN_SIGNAL_SCORE_MISPRICING: float = 0.0
MIN_SIGNAL_SCORE_MAKER: float = 60.0
# Per-bucket-type score overrides. When > 0, applied in addition to MIN_SIGNAL_SCORE_MAKER.
# e.g. set MAKER_MIN_SIGNAL_SCORE_5M = 70.0 to only quote 5m markets with strong signals.
# 5m buckets default to 92.0: session data shows -$4.19/trade avg at lower scores due
# to structural one-sided exposure in the short quote window.
MAKER_MIN_SIGNAL_SCORE_5M: float = 92.0
# Per-type override for bucket_1h markets. 1h buckets resolve directionally and carry
# ~25% YES/NO imbalance risk when skew doesn't fully balance the book. Raising this
# to 88.0 filters the weakest signals responsible for the -$1.89/trade average loss.
MAKER_MIN_SIGNAL_SCORE_1H: float = 88.0
# Per-type override for bucket_4h markets. Longer duration gives the imbalance skew
# more time to balance YES/NO, so a slightly looser threshold (85.0) is appropriate.
# Set to 0.0 to use the global Maker threshold.
MAKER_MIN_SIGNAL_SCORE_4H: float = 85.0
# Per-factor weight multipliers (1.0 = natural weight; tune after more history).
SCORE_WEIGHT_EDGE: float = 1.0
SCORE_WEIGHT_SOURCE: float = 1.0
SCORE_WEIGHT_TIMING: float = 1.0
SCORE_WEIGHT_LIQUIDITY: float = 1.0

# ── ML Adaptive Signal Engine ────────────────────────────────────────────────
# Phase 0 — CLOB Feature Buffer (ML-01)
# Subscribes to PMClient on_price_change; maintains a per-token in-memory
# rolling deque of CLOB book snapshots.  No raw ticks are written to disk.
# All model flags default False — set True only after each phase is validated.
CLOB_FEATURE_BUFFER_ENABLED: bool = False   # ML-01: enable real-time CLOB buffer
CLOB_BUFFER_MAXLEN: int = 600               # ticks per token (~10 min at 1 tick/s)

# Phase 2 — ModelAgent shadow logging (ML-04)
MODEL_AGENT_ENABLED: bool = False           # ML-04: enable shadow logging
MODEL_A_PATH: str = str(Path(__file__).parent / "analysis" / "model_a_v0.pkl")
MODEL_B_PATH: str = str(Path(__file__).parent / "analysis" / "model_b_v0.pkl")
MODEL_A_SCORE_THRESHOLD: float = 0.5       # Phase 3: entry gate threshold
MODEL_B_SCORE_THRESHOLD: float = 0.5       # Phase 3: exit suppress threshold

# Phase 3 — Model-assisted mode (ML-06, ML-07)
# All default False — enable only after Phase 2 shadow validation criteria are met.
MODEL_B_ENABLED: bool = False              # ML-06: Model B exit suppression gate
MODEL_B_SUPPRESS_THRESHOLD: float = 0.5   # ML-06: suppress loser exit if score < this
MODEL_A_ENABLED: bool = False              # ML-07: Model A entry sizing scale
MODEL_A_MIN_SCALE: float = 0.5            # ML-07: minimum Kelly scale (50% of base)
MODEL_A_MAX_SCALE: float = 1.0            # ML-07: maximum Kelly scale (no upscale in Phase 3)

# Model C — CLOB/Oracle divergence calibrator (ML-C2)
MODEL_C_ENABLED: bool = False             # ML-C2: enable Model C shadow scoring (default off)
MODEL_C_SUPPRESS_THRESHOLD: float = 0.5  # ML-C2: adaptive exit threshold (0.0–1.0)
MODEL_C_PATH: str = str(Path(__file__).parent / "analysis" / "model_c_v0.pkl")

# Model D — Config Policy Optimizer (ML-D4)
MODEL_D_ENABLED: bool = False            # ML-D4: enable Model D shadow scoring (default off)
MODEL_D_PATH: str = str(Path(__file__).parent / "analysis" / "model_d_v0.pkl")
MODEL_D_MAX_DELTA_PCT: float = 0.5       # ML-D4: max config adjustment as fraction of live value (±50%)

# Phase 4 — Independent Entry Evaluation (ML-08, ML-09)
# All default False / conservative until Phase 3 acceptance criteria are met.
MODEL_A_INDEPENDENT_ENABLED: bool = False         # ML-08: run model-only entry scan loop
MODEL_A_INDEPENDENT_ENTRY_THRESHOLD: float = 0.7 # ML-08: paper trade when score exceeds this
MODEL_A_MIN_TTE_SECS: int = 30                   # ML-08: skip markets with TTE < this
MODEL_A_MAX_OPEN_POSITIONS: int = 5              # ML-08: max concurrent paper positions

# ── Agent ─────────────────────────────────────────────────────────────────────
AGENT_MODEL: str = "qwen2.5:7b"     # Ollama model name
AGENT_MIN_CONFIDENCE: float = 0.6   # Skip if agent confidence below this
AGENT_AUTO: bool = False            # Set True after 10+ shadow-mode validations
AGENT_MIN_TRUST_SCORE: int = 10     # Number of validated predictions before auto mode

# ── API Server ────────────────────────────────────────────────────────────────
API_PORT: int = int(os.getenv("API_PORT", "8080"))
API_SECRET: str = os.getenv("API_SECRET", "")   # Bearer token for POST endpoints; empty = no auth (dev only)
API_CORS_ORIGINS: list[str] = os.getenv("API_CORS_ORIGINS", "*").split(",")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_FILL_THRESHOLD: float = 100.0  # Only alert on fills > $100

# ── Paper Trading ─────────────────────────────────────────────────────────────
PAPER_TRADING: bool = True   # ← Switch to False for live trading
PAPER_CAPITAL_USD: float = 10000.0  # virtual budget for capital tracking in paper mode
# Fill simulator (Strategy 1 paper trading only)
FILL_CHECK_INTERVAL: int = 5          # seconds between fill-simulation sweeps
PAPER_FILL_PROBABILITY: float = 0.35  # kept for backward compat; new keys below used
# Three-tier fill probability model (Flaw §7):
#   Base: back-of-queue post-only in a thin book → low fill rate
#   New-market: first-mover advantage on recently discovered markets
#   Adverse selection penalty: halve probability when HL moved recently (being picked off)
PAPER_FILL_PROB_BASE: float = 0.04         # was 0.10 — realistic back-of-queue post-only rate
PAPER_FILL_PROB_NEW_MARKET: float = 0.12   # was 0.35 — first-mover advantage on new markets
PAPER_ADVERSE_SELECTION_PCT: float = 0.003  # HL move fraction that triggers the penalty
# During an adverse HL move, arrival_prob is MULTIPLIED by this factor.
# 0.15 → fills drop to 15% of normal rate.  Informed flow means faster bots
# are ahead in queue; our stale quote is skipped or only tiny remnants reach us.
# Keep this < 1 to reduce fill probability on adverse ticks.
PAPER_ADVERSE_FILL_MULTIPLIER: float = 0.15
# Fraction of the theoretical per-fill rebate that a non-top maker realistically
# captures from the daily pool (top makers ~100%; small/mid makers ~10–30%).
PAPER_REBATE_CAPTURE_RATE: float = 0.25

# Alert when the rolling 20-hedge average slippage exceeds this threshold (%).
# Suggests HL spread is wide or the book is thin; hedging is costing more than
# expected and the strategy may need to widen its own spread to compensate.
HEDGE_SLIPPAGE_ALERT_PCT: float = 0.30

# Auto-approve signals without human y/n prompt (safe for paper testing)
AUTO_APPROVE: bool = True
# Bypass Ollama agent entirely — execute all actionable signals directly.
# Use during paper testing to validate mispricing signal quality without
# needing Ollama running. Set False when agent is ready for live trading.
BYPASS_AGENT: bool = True

# ── Position Monitor ──────────────────────────────────────────────────────────
MONITOR_INTERVAL: int = 30           # seconds between position checks
# How often to poll PM wallet for redeemable settled positions (live mode only)
REDEEM_POLL_INTERVAL: int = 30       # seconds
# Profit target: exit when unrealised >= this fraction of (deviation * size)
PROFIT_TARGET_PCT: float = 0.60      # capture 60% of the initial mispricing
# Stop-loss: exit when unrealised loss exceeds this in USD
STOP_LOSS_USD: float = 25.0
# Time stop: close positions this many days before market resolution (mispricing only)
EXIT_DAYS_BEFORE_RESOLUTION: int = 3
# Minimum hold time before any exit is considered (prevents flip on noise).
# Applies to maker and mispricing positions only.  Momentum is event-driven — exits
# fire immediately on WS ticks; no hold floor is applied.
MIN_HOLD_SECONDS: int = 60

# ── Institutional Grade — Gate Audit Logging (S1) ────────────────────────────
# Number of consecutive suppressions on the same gate+entity before the log
# level escalates to WARNING.  At threshold and power-of-2 multiples thereafter
# (threshold, 2×, 4×, …).  Set to 0 to always log at WARNING.
GATE_LOG_CONSECUTIVE_THRESHOLD: int = 10

# ── Institutional Grade — Position Data Safety (S2) ──────────────────────────
# S2.2 — Oracle REST fallback: when oracle is stale for this many seconds AND
# a position is open for that coin, trigger a one-shot REST price fetch to
# top-up the oracle cache.  0 = disabled.
ORACLE_STALE_POSITION_FALLBACK_SECS: int = 60

# S2.3 — PM book REST refresh: when a priority token's book is older than this,
# trigger a REST book refresh from PMClient.fetch_book_rest().  0 = disabled.
# Priority tokens are registered automatically when a position is opened.
POSITION_BOOK_FALLBACK_AGE_SECS: int = 45

# S2.4 — Near-expiry hard exit on stale oracle: if TTE < MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS
# AND oracle has been stale for longer than this threshold AND the token is not
# clearly winning (below MOMENTUM_TAKER_EXIT_SUPPRESS_ABOVE), fire a taker exit
# rather than holding blind to resolution.  0 = disabled.
ORACLE_STALE_NEAR_EXPIRY_HARD_EXIT_SECS: int = 10

# S2.5 — Mid-hold stale oracle exit: if the oracle has been silent for this
# many seconds while a momentum position is open (at any TTE), exit rather
# than holding blind.  Bypasses the winner-suppress gate — a stale oracle
# cannot confirm the position is winning.  0 = disabled.
ORACLE_STALE_MID_HOLD_EXIT_SECS: int = 120

# ── Institutional Grade — Feed Health (S3) ────────────────────────────────────
# S3.3 — Chainlink reconnect rate alerting: warn when the rolling-1h reconnect
# count for any Chainlink origin exceeds this threshold.
CHAINLINK_MAX_RECONNECTS_1H: int = 10

# S3.4 — HL WebSocket mark price staleness threshold: warn when the mark price
# age for a coin with open hedges exceeds this many seconds.
HL_WS_STALE_SECS: int = 30

# ── Runtime overrides (persisted across restarts) ───────────────────────────
# When api_server patches a config value it writes config_overrides.json.
# On the next startup that file is loaded here, layered on top of the defaults
# above, so settings survive a bot restart.
_OVERRIDES_FILE = Path(__file__).parent / "config_overrides.json"
if _OVERRIDES_FILE.exists():
    try:
        _overrides: dict = json.loads(_OVERRIDES_FILE.read_text())
        import sys as _sys
        _module = _sys.modules[__name__]
        for _k, _v in _overrides.items():
            if hasattr(_module, _k):
                setattr(_module, _k, _v)
    except Exception as _e:
        import warnings as _w
        _w.warn(f"[config] Failed to load {_OVERRIDES_FILE}: {_e}")


def get_effective_config() -> dict:
    """Return a snapshot of all current runtime config values.

    Reads directly from the module so post-startup patches (via POST /config)
    are reflected.  Excludes private helpers (leading underscore), imported
    modules, classes, callables, and env-derived secrets.
    """
    import sys as _sys
    import types as _types
    _skip = {"POLY_PRIVATE_KEY", "HL_SECRET_KEY", "API_SECRET",
              "TELEGRAM_BOT_TOKEN"}
    mod = _sys.modules[__name__]
    result: dict = {}
    for name in dir(mod):
        if name.startswith("_"):
            continue
        if name in _skip:
            continue
        val = getattr(mod, name)
        # Only include JSON-serialisable scalars and plain collections.
        # Exclude: functions, classes, module objects, and anything else complex.
        if callable(val):
            continue
        if isinstance(val, _types.ModuleType):
            continue
        if not isinstance(val, (bool, int, float, str, list, dict, type(None))):
            continue
        result[name] = val
    return result
