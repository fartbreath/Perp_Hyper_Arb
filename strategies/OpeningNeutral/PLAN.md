# Opening Neutral - Resting Exit Plan

---

## Strategy Overview

At market open, buy **both** YES and NO tokens simultaneously at the current ask (~$0.50).
Once both legs fill, immediately place resting limit SELL orders at $0.35 on both sides.

As the market moves, the losing side drops toward zero and hits the $0.35 resting SELL.
That exit fill recovers $0.35 on the losing leg instead of riding it to $0.00.
The other resting SELL is then cancelled and the winning position transitions to the
**Momentum** strategy - existing SL/TP/monitor logic applies from that point forward.

### P&L math

```
Entry:       BUY YES @ $0.50  +  BUY NO @ $0.50  =  $1.00 combined cost
Loser exit:  SELL losing side @ $0.35             =  $0.35 recovered
Winner:      Transitions to momentum (rides to ~$1.00)
Net:         $1.00 (winner) + $0.35 (loser exit) - $1.00 (entry) = +$0.35 per pair
vs. no exit: $1.00 (winner) + $0.00 (loser at resolution)        =  $0.00 breakeven
```

The resting SELL at $0.35 captures $0.35 per contract from the losing side rather than
waiting for it to expire worthless. The earlier the loser drops, the sooner capital is
freed and the winning leg can be managed actively.

---

## Order Flow

```
1. MARKET OPEN (entry window)
   +-- Qualify market (Up/Down, correct type, within entry window, not already entered)
   +-- Place two BUY orders concurrently at current ask (~$0.50):
         BUY YES @ ask_price
         BUY NO  @ ask_price

2. WAITING FOR BOTH ENTRY FILLS
   +-- Both fill within OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS
   |     +-- Register YES Position (strategy="opening_neutral", neutral_pair_id=pair_id)
   |     +-- Register NO  Position (strategy="opening_neutral", neutral_pair_id=pair_id)
   |     +-- Immediately place resting SELL orders:
   |           SELL YES @ OPENING_NEUTRAL_LOSER_EXIT_PRICE (e.g. $0.35)
   |           SELL NO  @ OPENING_NEUTRAL_LOSER_EXIT_PRICE (e.g. $0.35)
   +-- Only one fills (timeout)
   |     +-- Keep filled leg as strategy="momentum" (one-leg fallback)
   |     +-- Cancel the unfilled BUY
   +-- Neither fills
         +-- Cancel both BUY orders. No position registered.

3. LOSER EXIT (resting SELL hits)
   +-- WS fill event fires for SELL YES (YES was the loser)
   |     +-- Close YES position at $0.35 via risk.close_position()
   |     +-- Cancel resting SELL NO
   |     +-- Transition NO position: strategy="momentum", neutral_pair_id=""
   +-- WS fill event fires for SELL NO (NO was the loser)
         +-- Close NO position at $0.35 via risk.close_position()
         +-- Cancel resting SELL YES
         +-- Transition YES position: strategy="momentum", neutral_pair_id=""

4. MOMENTUM HANDOFF
   +-- Winner position is strategy="momentum" from this point.
       monitor.py, delta SL, TP, GTD hedge - all apply unchanged.
```

---

## Entry Conditions (all must pass)

| Gate | Detail |
|---|---|
| Market type | Must be in `OPENING_NEUTRAL_MARKET_TYPES` |
| Direction | `_is_updown_market()` must return True |
| Entry window | Elapsed time since open <= `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` |
| Combined cost | YES ask + NO ask <= `OPENING_NEUTRAL_COMBINED_COST_MAX` |
| No existing position | `risk.get_open_positions()` has no entry for this market |
| Concurrent cap | Open pairs < `OPENING_NEUTRAL_MAX_CONCURRENT` |
| Not already entered | `_entered_market_ids` does not contain this market |

---

## Configuration

```python
# Entry window: only place BUY orders within the first N seconds of market open.
OPENING_NEUTRAL_ENTRY_WINDOW_SECS: int = 120

# Combined cost gate: skip if YES_ask + NO_ask > this ceiling.
# Near 1.0 = nearly fair pricing at open. >1.01 = too skewed, skip.
OPENING_NEUTRAL_COMBINED_COST_MAX: float = 1.01

# USDC per leg (YES buy = this, NO buy = this).
OPENING_NEUTRAL_SIZE_USD: float = 5.0

# Resting SELL price placed on both sides immediately after entry fills.
# $0.35 recovers $0.35 on the loser instead of $0.00 at resolution.
# Lower = smaller loss on loser but harder to fill; higher = easier fill, less recovery.
OPENING_NEUTRAL_LOSER_EXIT_PRICE: float = 0.35

# Seconds to wait for BOTH BUY legs to fill at entry.
OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS: int = 30

# One-leg fallback when only one BUY fills: "keep_as_momentum" or "exit_immediately".
OPENING_NEUTRAL_ONE_LEG_FALLBACK: str = "keep_as_momentum"

# Market types to watch.
OPENING_NEUTRAL_MARKET_TYPES: list = [
    "bucket_5m", "bucket_15m", "bucket_1h", "bucket_4h", "bucket_daily", "bucket_weekly"
]

# Maximum simultaneous open neutral pairs.
OPENING_NEUTRAL_MAX_CONCURRENT: int = 3

# Order type for the entry BUY legs: "limit" (post-only at current ask) or "market".
# Use "market" when fills at open are hard to obtain (thin book, fast open move).
# The loser-exit SELL is always a resting limit order regardless of this setting.
OPENING_NEUTRAL_ORDER_TYPE: str = "limit"

# When True: log signals and simulate fills but place no real orders.
OPENING_NEUTRAL_DRY_RUN: bool = True
```

**Removed config keys (replaced by resting SELL approach):**

| Key | Reason removed |
|---|---|
| `OPENING_NEUTRAL_LOSER_EXIT_Z` | Replaced by resting SELL order at fixed price |
| `OPENING_NEUTRAL_LOSER_EXIT_MIN_TICKS` | Not needed - CLOB fill is the trigger |

---

## Scanner State

```
_pending_markets     dict[condition_id -> PMMarket]
_token_to_pending    dict[token_id -> condition_id]        YES only (entry path)
_active_pairs        dict[pair_id -> {
                         yes_pos:           Position,
                         no_pos:            Position,
                         yes_exit_order_id: str,    <- resting SELL on YES
                         no_exit_order_id:  str,    <- resting SELL on NO
                         market_id:         str,
                         market_title:      str,
                     }]
_token_to_pair       dict[token_id -> pair_id]             exit fill routing
_entered_market_ids  set[condition_id]                     prevents re-entry
_entering_markets    set[condition_id]                     in-flight guard
```

`_active_pairs` during the resting phase holds both Positions AND the exit order IDs.
When a resting SELL fires: close loser Position, cancel counterpart SELL, mutate winner.

---

## Exit Fill Detection

After both entry BUYs confirm and resting SELLs are placed:

```python
# Both futures registered; scanner listens via _on_price_event / fill callback
pm.register_fill_future(yes_exit_order_id, yes_exit_future)
pm.register_fill_future(no_exit_order_id,  no_exit_future)

# On first fill (whichever side drops to $0.35 first):
#   1. close loser position
#   2. cancel other resting SELL
#   3. winner.strategy = "momentum"; winner.neutral_pair_id = ""
#   4. remove pair from _active_pairs
```

If neither exit SELL fills before market resolution, `monitor.py` handles both
positions via the RESOLVED path (winner pays $1.00, loser pays $0.00).

---

## Momentum Handoff

On loser exit, mutate the winner Position in place:

```python
winner_pos.strategy = "momentum"
winner_pos.neutral_pair_id = ""
```

`monitor.py` picks it up on the next sweep and applies momentum SL/TP/GTD hedge.
No additional registration call needed - the Position is already in `risk.get_open_positions()`.

---

## Logging

Both legs are registered as `strategy="opening_neutral"` at entry.
- Loser close: `record_trade_close(loser_market_id)` - appears as opening_neutral trade
- Winner: mutated to `strategy="momentum"`, closes via normal momentum path

Signal attempts log to `_signals` (via `/opening_neutral/status`):

```json
{
  "ts": "2026-04-29T01:00:00Z",
  "market_id": "0xabc...",
  "market_title": "BTC Up or Down - 9:00PM-9:05PM ET",
  "market_type": "bucket_5m",
  "yes_ask": 0.51,
  "no_ask": 0.49,
  "combined": 1.00,
  "exit_limit_price": 0.35,
  "result": "entry_attempt / too_expensive / too_late / at_cap / dry_run"
}
```

---

## Phase 2 (deferred)

| Item | Reason deferred |
|---|---|
| Per-bucket exit price overrides | Single price sufficient for Phase 1 data collection |
| Dynamic exit price (% below entry) | Needs fill data to calibrate |
| Partial fill handling at entry | REST verification adds complexity |
| Regime filter (low-vol opens only) | Needs fill data to calibrate |
