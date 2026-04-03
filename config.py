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
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
RELAYER_API_KEY: str = os.getenv("RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS: str = os.getenv("RELAYER_API_KEY_ADDRESS", "")

# Token IDs of underlying assets tracked. Used to label markets.
TRACKED_UNDERLYINGS: list[str] = [
    "BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP", "LINK",
]

# How often (seconds) to refresh the market list from Gamma API
MARKET_REFRESH_INTERVAL: int = 60

# PM WebSocket
PM_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PM_USER_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
PM_DATA_API_URL: str = "https://data-api.polymarket.com"
PM_HEARTBEAT_INTERVAL: float = 5.0   # seconds — must be < 15s hard limit
PM_WS_PING_INTERVAL: float = 10.0    # seconds
# Community-derived server-side limit: ~100 tokens per WS session before PM
# returns "INVALID OPERATION" and stops delivering book events for that chunk.
PM_WS_MAX_MARKETS_PER_WS: int = 100

# ── Hyperliquid ─────────────────────────────────────────────────────────────
HL_ADDRESS: str = os.getenv("HL_ADDRESS", "")
HL_SECRET_KEY: str = os.getenv("HL_SECRET_KEY", "")
HL_BASE_URL: str = "https://api.hyperliquid.xyz"

HL_PERP_COINS: list[str] = ["BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP", "LINK"]
HL_DEFAULT_SLIPPAGE: float = 0.003   # 0.3% max slippage for hedge market orders

# ── Pyth Oracle — spot price feed (used for momentum delta SL / entry delta) ─
# Polymarket "Up or Down" bucket markets resolve against the Pyth spot price at
# their end_date (directly or via Chainlink/Binance feeds which track the same
# underlying).  Using Pyth instead of HL perp eliminates funding-rate basis and
# ensures the bot's stop-loss triggers on the same price that Polymarket settles.
# Feed IDs from https://pyth.network/developers/price-feed-ids (no "0x" prefix).
PYTH_PRICE_FEED_IDS: dict[str, str] = {
    "BTC":  "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH":  "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL":  "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP":  "ec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8",
    "BNB":  "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "DOGE": "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "HYPE": "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b",
}
HL_DEAD_MAN_INTERVAL: int = 300      # seconds — refresh dead man's switch every 5 min
HL_FUNDING_POLL_INTERVAL: int = 300  # seconds

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

# ── Strategy 3 — Momentum Scanner ─────────────────────────────────────────
# Price band: scanner fires when held-side is in [LOW, HIGH].
MOMENTUM_PRICE_BAND_LOW: float = 0.80
MOMENTUM_PRICE_BAND_HIGH: float = 0.90

# Maximum USDC deployed per momentum position.
MOMENTUM_MAX_ENTRY_USD: float = 50.0
# Edge-proportional sizing: scale entry size based on signal edge strength.
# When edge_pct >= MOMENTUM_EDGE_SIZE_ANCHOR the full MAX_ENTRY is deployed.
# Below the anchor: size = (edge_pct / anchor) * MAX_ENTRY, floored at MIN_ENTRY_USD.
MOMENTUM_EDGE_SIZE_ANCHOR: float = 0.10  # edge_pct at which full MAX_ENTRY is used
MOMENTUM_MIN_ENTRY_USD: float = 1.0      # floor to avoid dust orders

# Minimum USDC depth on the ask side within 1c of best ask (thin-book guard).
# Prevents entering markets where our order would exhaust available liquidity.
MOMENTUM_MIN_CLOB_DEPTH: float = 200.0

# Order type: "limit" = taker limit at ask+0.5c (ensures fill); "market" = immediate cross.
MOMENTUM_ORDER_TYPE: str = "limit"

# Exit thresholds — based on the underlying spot (not CLOB binary price).
# Delta-based stop-loss: exit when live HL spot has moved this % past the
# strike against the position (e.g. 0.05 → exit when spot is 0.05% below
# strike for YES, or 0.05% above strike for NO).
MOMENTUM_DELTA_STOP_LOSS_PCT: float = 0.01  # % beyond strike before stop fires
MOMENTUM_TAKE_PROFIT: float = 0.999         # Exit if held token rises above this

# Near-expiry time-stop: when TTE is very short and spot has already crossed
# the strike (delta < 0), exit via taker to avoid a binary snap to zero.
MOMENTUM_NEAR_EXPIRY_TIME_STOP_SECS: int = 90        # TTE threshold (seconds)

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
MOMENTUM_BOOK_MAX_AGE_SECS: float = 60.0   # PM book age gate: skip market if book is older than this (WS shard outage).

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

# Minimum additional gap required above the vol-scaled threshold (percentage
# points of |(spot - strike) / strike|).  Prevents marginal signals where the
# spot-to-strike gap barely clears the threshold and a single adverse tick is
# enough to flip the position from winning to losing before expiry.
# 0.0 = disabled (default).  Recommended live value: 0.02 (see config_overrides).
MOMENTUM_MIN_GAP_PCT: float = 0.0

# How often to run a full scan pass over all bucket markets (seconds).
MOMENTUM_SCAN_INTERVAL: int = 10

# Maximum simultaneous open momentum positions.
MOMENTUM_MAX_CONCURRENT: int = 3

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
REDEEM_POLL_INTERVAL: int = 60       # seconds
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
