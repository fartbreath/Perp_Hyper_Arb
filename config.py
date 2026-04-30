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
# Community-derived server-side limit: ~100 tokens per WS session before PM
# returns "INVALID OPERATION" and stops delivering book events for that chunk.
PM_WS_MAX_MARKETS_PER_WS: int = 100

# ── Hyperliquid ─────────────────────────────────────────────────────────────
HL_ADDRESS: str = os.getenv("HL_ADDRESS", "")
HL_SECRET_KEY: str = os.getenv("HL_SECRET_KEY", "")
HL_BASE_URL: str = "https://api.hyperliquid.xyz"

HL_PERP_COINS: list[str] = ["BTC", "ETH", "SOL", "BNB", "DOGE", "HYPE", "XRP"]
HL_DEFAULT_SLIPPAGE: float = 0.003   # 0.3% max slippage for hedge market orders

HL_DEAD_MAN_INTERVAL: int = 300      # seconds — refresh dead man's switch every 5 min
HL_FUNDING_POLL_INTERVAL: int = 120  # seconds

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

# ── Strategy 5 — Opening Neutral ──────────────────────────────────────────
# Market types the scanner watches for simultaneous YES+NO entry opportunities.
# All bucket types are included — the _is_updown_market() filter ensures only
# Up/Down direction markets are entered regardless of bucket size.
OPENING_NEUTRAL_MARKET_TYPES: list = [
    "bucket_5m", "bucket_15m", "bucket_1h", "bucket_4h"
]
# Entry window: only consider markets whose TTE is within this many seconds of opening.
OPENING_NEUTRAL_ENTRY_WINDOW_SECS: int = 120
# Maximum combined cost for YES + NO (≤ 1.0 = guaranteed profit at resolution;
# ≤ 1.01 allows 1-tick slip / fee headroom).
OPENING_NEUTRAL_COMBINED_COST_MAX: float = 1.02
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
OPENING_NEUTRAL_FAK_FILL_TIMEOUT_SECS: int = 5
# What to do when only one leg fills within the timeout.
# "keep_as_momentum" — leave the filled leg running as a momentum position.
# "exit_immediately" — taker-exit the filled leg at best bid.
OPENING_NEUTRAL_ONE_LEG_FALLBACK: str = "keep_as_momentum"
# Exit price for the losing leg: resting GTC SELL placed on both sides immediately
# after entry at this price.  Whichever fills first is the loser.
# Net pair P&L = exit_price + $1.00 − 2×entry.
OPENING_NEUTRAL_LOSER_EXIT_PRICE: float = 0.35
# Seconds to wait for either resting exit SELL to fill before cancelling both orders.
# Should cover the full market duration (5m market = 300 s).
OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS: int = 300
# Seconds before market open to pre-warm the CLOB HTTP connection pool (idea 5).
# A lightweight GET is sent this many seconds before open so the TCP connection
# is established before the BUY orders fire.
OPENING_NEUTRAL_PREWARM_SECS: float = 0.2
# Seconds before market open to fire the scheduled entry timer (idea 1).
# Slightly early to absorb asyncio event-loop scheduling jitter (~1-5ms).
OPENING_NEUTRAL_TIMER_ADVANCE_SECS: float = 0.05
# Per-side price band: both YES ask and NO ask must be within [MIN, MAX] for
# an entry to qualify.  Keeps the strategy truly neutral (near 50/50) and
# prevents entries into highly-skewed markets (e.g. YES=0.12 / NO=0.89)
# where the exit logic breaks down and one leg is almost certain to lose.
OPENING_NEUTRAL_MIN_SIDE_PRICE: float = 0.44
OPENING_NEUTRAL_MAX_SIDE_PRICE: float = 0.56
# Maximum simultaneous opening-neutral pairs.
OPENING_NEUTRAL_MAX_CONCURRENT: int = 1
# DRY_RUN: when True all order placements are skipped (no real orders sent).
# Signals, pair tracking, and all logic run normally — only the pm_client calls
# are suppressed.  Safe to deploy inactive; set False after validation.
OPENING_NEUTRAL_DRY_RUN: bool = True

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
MOMENTUM_KELLY_CLOB_RELIABLE_TTE: int = 60       # seconds; above this CLOB fully weighted
MOMENTUM_KELLY_ORACLE_SENSITIVITY: float = 0.15  # slope: delta multiples → win_prob

# Kelly Phase-A extension — persistence z-boost.

# PERSISTENCE: rewards signals that have remained continuously valid (above
# threshold) for a sustained period.  A z-boost ramps linearly from 0 up to
# PERSISTENCE_Z_BOOST_MAX as the signal ages, so a signal that has been strong
# for the full min-TTE window gets slightly more sizing than a brand-new trigger.
MOMENTUM_KELLY_PERSISTENCE_ENABLED: bool = True
MOMENTUM_KELLY_PERSISTENCE_Z_BOOST_MAX: float = 0.5  # max additional z at full persistence window

# Minimum USDC depth on the ask side within 1c of best ask (thin-book guard).
# Prevents entering markets where our order would exhaust available liquidity.
MOMENTUM_MIN_CLOB_DEPTH: float = 200.0

# Order type: "limit" = taker limit at ask+0.5c (ensures fill); "market" = immediate cross.
MOMENTUM_ORDER_TYPE: str = "limit"

# Exit thresholds — based on the underlying spot (not CLOB binary price).
# Delta-based stop-loss: exit when live HL spot has moved this % past the
# strike against the position (e.g. 0.05 → exit when spot is 0.05% below
# strike for YES, or 0.05% above strike for NO).
MOMENTUM_DELTA_STOP_LOSS_PCT: float = 0.01  # protective buffer: exit when delta (in-the-money %) drops BELOW this threshold (fires before strike is crossed)
MOMENTUM_DELTA_SL_MIN_TICKS: int = 3        # hysteresis: delta SL only fires after this many consecutive below-threshold ticks (prevents single-tick noise from triggering exit)
MOMENTUM_TAKE_PROFIT: float = 0.999         # Exit if held token rises above this
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

# Phase D — GTD (GTC resting) hedge: after a confirmed entry, optionally place
# a low-price GTC limit BUY on the opposite token as oracle-free downside cover.
# If the trade loses (held token → $0), the opposite token may briefly trade
# at HEDGE_PRICE; the GTC order catches that dip and redeems at $1.00.
MOMENTUM_HEDGE_ENABLED: bool = True
MOMENTUM_HEDGE_PRICE: float = 0.02    # GTC bid price fallback (when no per-bucket override)
# Per-bucket hedge bid prices — shorter windows carry higher mismatch risk, so
# the resting bid needs to be a bit higher to get filled during a panic dip.
# These are intentionally deep OTM bids: the opposite token must fall from
# its ~10-30% implied prob all the way to these prices before filling.
MOMENTUM_HEDGE_PRICE_BY_TYPE: dict[str, float] = {
    "bucket_5m":  0.02,
    "bucket_15m": 0.02,
    "bucket_1h":  0.015,
    "bucket_4h":  0.01,
    "bucket_daily":   0.01,
    "bucket_weekly":  0.01,
    "milestone":      0.01,
}
# Per-bucket hedge on/off flags.
# 5m and 15m are OFF by default: at ≤120 s TTE there is no time for the whipsaw
# path (favorable excursion + reversal) that gives the hedge insurance value.
# The hedge is also cancelled on any non-RESOLVED exit to prevent double-jeopardy.
# MOMENTUM_HEDGE_ENABLED is the global master switch; per-bucket flags are only
# consulted when the master switch is True.
MOMENTUM_HEDGE_ENABLED_BY_TYPE: dict[str, bool] = {
    "bucket_5m":    False,
    "bucket_15m":   False,
    "bucket_1h":    True,
    "bucket_4h":    True,
    "bucket_daily": True,
    "bucket_weekly":True,
    "milestone":    True,
}
# Hedge size as fraction of main entry contracts (1.0 = same contract count as main position).
# Actual USDC cost = hedge_contracts × hedge_price (e.g. 25ct × $0.02 = $0.50).
MOMENTUM_HEDGE_CONTRACTS_PCT: float = 1.0

# Profit-safe hedge price cap: the bot will not pay more per hedge contract than
# (projected_pnl - MOMENTUM_HEDGE_MIN_RETAIN_USD) / hedge_contracts.
# Set to 0.0 to disable the cap (no floor on retained profit — old behaviour).
MOMENTUM_HEDGE_MIN_RETAIN_USD: float = 0.25

# N-tick concession ladder: how many times to retry with price raised by 1 tick ($0.01).
# 1 = single attempt at the configured price (closest to old behaviour).
MOMENTUM_HEDGE_MAX_TICKS_CONCESSION: int = 15

# Post-placement gap-closing reprice.
# On every monitor sweep, if the hedge token's best_ask has FALLEN since the prior
# sweep (seller moving toward us), cancel + repost the hedge at current_bid + $0.01.
# If the ask is flat or rising (seller moved away), hold — we don't chase upward.
# Repricing is capped by price_cap (computed at placement from projected PnL minus
# MOMENTUM_HEDGE_MIN_RETAIN_USD), so the hedge is always PnL-positive.
#
# Near-expiry cancel: if TTE ≤ MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS and the held
# token's CLOB mid is above 0.50 (we are winning), the hedge is cancelled — the
# insurance is no longer needed and we prevent an adverse fill at resolution.
# Set to 0 to disable the near-expiry cancel.
MOMENTUM_HEDGE_EXPIRY_CANCEL_SECS: int = 5

# TTE aggression threshold (seconds).  When time-to-expiry is below this value the
# bot forces a taker (FAK) order regardless of the book state.
# 0 = disabled.  Recommended starting value if enabling: 30.

MOMENTUM_HEDGE_AGGRESSIVE_TTE_S: int = 0
# Global taker override.  True = always use taker for hedge regardless of TTE or book.
# Useful for paper-mode testing.  Leave False in production.
MOMENTUM_HEDGE_AGGRESSIVE_TAKER: bool = False

# ── Logging toggles (post-trade analysis files) ───────────────────────────────
# These CSV files are only needed for post-trade analysis and calibration.
# Disable them when running in production to save disk space and improve I/O
# performance.  Both default to True so analysis data is collected by default.
MOMENTUM_HEDGE_CLOB_LOG_ENABLED: bool = True   # hedge_clob_ticks.csv — CLOB prices for open hedge bids
MOMENTUM_TICKS_LOG_ENABLED: bool = True         # momentum_ticks.csv   — intra-hold price ticks

# SL hedge cancel: deferred cancellation factor.
# When the main position exits via delta SL, the GTD hedge is NOT cancelled
# immediately.  Instead, the cancel is held until delta recovers to
#   cancel_threshold = coin_sl * (1 + MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT)
# This keeps the hedge alive during transient spikes (false-positive SLs), so
# the hedge can still catch a whipsaw fill if the opposite token dips.
# When delta does recover back above the threshold, the hedge is cancelled.
# If delta never recovers and the market resolves, the hedge rides to resolution.
# Set to 0.0 to restore immediate-cancel behaviour (old default).
MOMENTUM_HEDGE_CANCEL_RECOVERY_PCT: float = 0.5

# When True, suppress ALL stop-losses for any position that already has an
# active GTD hedge order placed.  This includes the oracle delta SL, the
# near-expiry time stop, and the CLOB prob-SL.  The hedge is the insurance
# leg — once it is resting in the CLOB the downside is bounded, so any SL
# exit would only lock in a loss before the hedge can pay off at resolution.
# Take-profit remains active (winning path; hedge is irrelevant there).
# Defaults to False (conservative) — all SLs fire regardless of hedge status.
MOMENTUM_HEDGE_SUPPRESSES_DELTA_SL: bool = False

# Paper-mode GTD hedge fill simulation.
# When PAPER_TRADING=True the fill_simulator also checks resting hedge bids against
# the live CLOB on every sweep.  If the opposite token's best_ask ≤ hedge_price,
# the hedge is considered "at touch" and fills with this probability.
# Mirrors the maker's taker-arrival model but without queue-position randomness
# (a resting BUY at the touch fills quickly in real CLOBs).
PAPER_HEDGE_FILL_PROB: float = 0.60   # probability of fill per sweep when ask <= bid

# Phase E — Empirical win-rate gate: load data/win_rate.csv at startup and gate
# entries where historical win rate < WIN_RATE_GATE_MIN_FACTOR × model win_prob.
# Disabled by default until ≥100 fills per bucket are available.
MOMENTUM_WIN_RATE_GATE_ENABLED: bool = False
MOMENTUM_WIN_RATE_GATE_MIN_FACTOR: float = 0.9  # empirical WR must be ≥ 90% of model WR
MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES: int = 10    # minimum fills per bucket before gate activates

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
MOMENTUM_PROB_SL_MIN_TTE_SECS: int = 300  # suppress prob-SL in final 5 minutes
MOMENTUM_PROB_SL_ORACLE_STALE_SECS: float = 10.0  # suppress prob-SL when oracle ticked within this many seconds (0 = disabled)

# ── Chainlink watchdog (Item 5) ──────────────────────────────────────────────
# Seconds of silence on an established WebSocket connection before forcing a
# reconnect.  Reduced from 120 s to 30 s so a zombie TCP connection is detected
# within one 5m-bucket expiry window rather than two.
CHAINLINK_SILENCE_WATCHDOG_SECS: int = 30

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
