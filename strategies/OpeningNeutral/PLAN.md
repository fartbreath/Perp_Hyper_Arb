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

## Entry Timing Architecture (ideas 1, 2, 5)

The original WS-tick-driven entry had ~1s latency from market open:

```
T+0ms    Market opens
T+200ms  First WS book tick arrives (shard latency + event loop)
T+350ms  _evaluate_entry: reads book via cache, checks gates
T+550ms  Two concurrent place_order calls fire
T+900ms  Fill WS events arrive
```

Three optimisations are now implemented to attack this:

### Idea 1 — Scheduled timer entry (~200-400ms saved)

When `_refresh_pending_markets` adds a market that hasn't opened yet
(`elapsed < 0`), it schedules a `_scheduled_entry_task` that sleeps until
`open_ts = end_date - duration` and fires `_evaluate_entry` directly at T+0.
No WS tick needed — entry fires at the known open time regardless of when
the first price event arrives.

### Idea 2 — Pre-qualification (~100-150ms saved)

Static gates (market type, `_is_updown_market` direction, entry-window
membership) are checked **once** at registration time in
`_refresh_pending_markets`.  Only markets that passed all static gates
are added to `_pending_markets` and scheduled for the timer.  At T+0 the
timer path only evaluates dynamic gates (concurrent cap, conflict guard,
combined cost from WS book cache) — no redundant re-checks.

The elapsed-window lower bound is relaxed to `-1.0s` for timer-fired entries
so that calls arriving `~50ms` before open are not rejected by the
`elapsed < 0` guard.

### Idea 5 — TCP connection pre-warm (~50-100ms saved)

The `_scheduled_entry_task` fires a lightweight authenticated GET to the CLOB
API (`get_live_orders`) exactly `OPENING_NEUTRAL_PREWARM_SECS` (200ms) before
`open_ts`.  This establishes a TCP+TLS socket in the underlying `requests`
connection pool so both BUY order POSTs start on an already-open connection.

```
Pre-warm timeline:
  T - 200ms  _prewarm_clob(): GET /orders (warms TCP connection)
  T - 50ms   _evaluate_entry(_timer_fired=True)
               → dynamic gates only (cap, conflict, combined cost)
               → spawn _enter_pair immediately
  T + 0ms    Market open — BUY orders land with warm TCP connection
```

---

## Order Flow

```
0. PRE-OPEN (presub window, -30s to 0s before open)
   +-- _refresh_pending_markets: static gates (type, direction, window, not-entered)
   +-- Schedule _scheduled_entry_task for open_ts

1. MARKET OPEN (entry window, 0s to ENTRY_WINDOW_SECS)
   Timer path (primary):
     T-0.2s  _prewarm_clob() — warm TCP connection
     T-0.05s _evaluate_entry(_timer_fired=True) — dynamic gates only
     → spawn _enter_pair if qualifying
   WS-tick path (fallback, fires if timer missed or market was already open
                 when the scanner started):
     On WS tick → _on_price_event → _evaluate_entry (full gates)
     → spawn _enter_pair if qualifying (debounced to 1 tick/sec)
   +-- Place two BUY orders concurrently:
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
    We can hand off to momentum, or keep the position held with a stop loss configuration.
   +-- Winner position is strategy="momentum" from this point.
       monitor.py, delta SL, TP, GTD hedge - all apply unchanged.
```

---

## Entry Conditions (all must pass)

| Gate | When checked | Detail |
|---|---|---|
| Market type | Pre-qual (registration) | Must be in `OPENING_NEUTRAL_MARKET_TYPES` |
| Direction | Pre-qual (registration) | `_is_updown_market()` must return True |
| Entry window | Pre-qual + dynamic | Elapsed time since open <= `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` |
| Combined cost | Dynamic (T+0) | YES ask + NO ask <= `OPENING_NEUTRAL_COMBINED_COST_MAX` |
| No existing position | Dynamic (T+0) | `risk.get_open_positions()` has no entry for this market |
| Concurrent cap | Dynamic (T+0) | Open pairs < `OPENING_NEUTRAL_MAX_CONCURRENT` |
| Not already entered | Dynamic (T+0) | `_entered_market_ids` does not contain this market |

---

## Configuration

```python
# Entry window: only place BUY orders within the first N seconds of market open.
OPENING_NEUTRAL_ENTRY_WINDOW_SECS: int = 120

# Combined cost gate: skip if YES_ask + NO_ask > this ceiling.
# Near 1.0 = nearly fair pricing at open. >1.01 = too skewed, skip.
OPENING_NEUTRAL_COMBINED_COST_MAX: float = 1.01

# USDC per leg (YES buy = this, NO buy = this).
OPENING_NEUTRAL_SIZE_USD: float = 1

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
OPENING_NEUTRAL_ORDER_TYPE: str = "market"

# Seconds before market open to pre-warm the CLOB HTTP connection pool (idea 5).
OPENING_NEUTRAL_PREWARM_SECS: float = 0.2

# Seconds before market open to fire the scheduled entry timer (idea 1).
# Slightly early to absorb asyncio event-loop scheduling jitter.
OPENING_NEUTRAL_TIMER_ADVANCE_SECS: float = 0.05

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
_pending_markets             dict[condition_id -> PMMarket]
_token_to_pending            dict[token_id -> condition_id]        YES only (entry path)
_scheduled_entry_market_ids  set[condition_id]                     markets with active timer tasks
_active_pairs                dict[pair_id -> {
                                 yes_pos:           Position,
                                 no_pos:            Position,
                                 yes_exit_order_id: str,    <- resting SELL on YES
                                 no_exit_order_id:  str,    <- resting SELL on NO
                                 market_id:         str,
                                 market_title:      str,
                             }]
_entered_market_ids          set[condition_id]                     prevents re-entry
_entering_markets            set[condition_id]                     in-flight guard
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
OPENING_NEUTRAL_SIZE_USD: float = 1

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
OPENING_NEUTRAL_ORDER_TYPE: str = "mARKET"

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
