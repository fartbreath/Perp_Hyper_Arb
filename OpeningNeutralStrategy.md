# Opening Neutral Strategy

## Core Concept

At market open, enter **both** YES and NO tokens simultaneously at the current ask (~$0.50 each). Immediately place resting SELL limit orders at $0.35 on both legs. As the market moves, one side drops toward zero and hits the $0.35 resting SELL — recovering $0.35 instead of waiting for expiry at $0.00. The other leg becomes a clean directional winner and is handed off to the Momentum strategy.

> **Delta-neutral by design.** The strategy has zero directional bias at entry. It does not predict which side wins — it simply extracts value from the loser regardless of direction.

---

## P&L Math

```
Entry:        BUY YES @ $0.50  +  BUY NO @ $0.50  =  $1.00 combined cost
Loser exit:   SELL losing side @ $0.35             =  $0.35 recovered
Winner:       Transitions to Momentum (rides to ~$1.00 via delta SL / TP)
Net:          $1.00 (winner) + $0.35 (loser exit) − $1.00 (entry) = +$0.35 per pair
vs. no exit:  $1.00 (winner) + $0.00 (loser at resolution)         = $0.00 breakeven
```

The resting SELL captures $0.35 from the loser instead of $0.00. The earlier the loser drops, the sooner capital is freed and the winning leg transitions to Momentum.

**Structural failure mode:** If the loser stalls at $0.40–$0.45 through the entire bucket, the resting SELL never fills. Both legs resolve at expiry: winner pays $1.00, loser pays $0.00. Net: `$1.00 − combined_cost`. At `combined_cost = 1.02`, this is a −$0.02 loss. This is why cold-book windows (wide spreads, slow price discovery) must be filtered.

---

## Why It Works

### The Loser Recovery Edge

Binary markets always resolve to $1.00 / $0.00. The loser token starts at ~$0.50 and ends at $0.00. In every market, the loser leg will pass through $0.35 at some point on its way to zero — the resting SELL simply needs to be in place to catch it.

The edge is not about predicting direction. It is about capturing the intra-bucket price movement of the losing leg rather than holding it to worthless expiry.

### Why Not Just Hold Both to Resolution?

Without the resting SELL:
- Winner: $1.00 (profit on the winning leg)
- Loser: $0.00 (full loss on the losing leg)
- Net: $1.00 − combined_cost ≈ $0.00 (break-even or small loss at median $1.02 combined cost)

The resting SELL converts a break-even structure into a consistent +$0.35 per completed pair by harvesting the loser's descent.

### Why 80–90c Momentum Is Better Than Resting Both

After the loser exits, holding the winner passively (resting SELL at $0.35 as the only exit) would capture $0.35 less per winning pair than letting Momentum manage it to $0.999. The Momentum handoff is what makes the +$0.35 math work — without it, the winner would also need a $0.35 floor and net P&L drops to $0.70 − combined_cost.

### Entry Price Reality (from dataset, strategy_update.md §0.1)

| Metric | Value |
|--------|-------|
| Mean combined cost at open | $1.0567 |
| Median combined cost | $1.02 |
| Qualifying windows (≤ $1.01) | ~5th percentile |

The strategy is correctly selective at `combined_cost ≤ $1.01`. The tight gate is a feature, not a bug — only the cheapest opens qualify.

---

## Core Agent Rules

Read before every task involving this strategy.

1. **Read `OPENING_NEUTRAL_IMPL_PLAN.md` before making any code changes.** The plan is the source of truth for what gates are changing, what is disabled by default, and what requires validation data before enabling.

2. **The strategy is always delta-neutral at entry.** Both legs ALWAYS get a resting SELL. Never add directional logic that prevents one leg from receiving a resting SELL. The asymmetric pricing in Phase 1 only adjusts the *price level* — both legs always have an exit order placed.

3. **The resting SELL on the loser is a limit order, always.** The entry BUY can be market or limit (config). The loser exit is always a resting limit — never a market order on the loser. Using a market sell prematurely would get a worse price than $0.35.

4. **Phase 1 features (§0.3, §0.4) are DISABLED by default.** Do not enable `OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED` or `OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED` until the 2-week enablement criteria in OPENING_NEUTRAL_IMPL_PLAN.md §1.1 are met. Check before enabling.

5. **Requires `FundingRateCache` and `PMClient.get_depth_share()`** from Momentum Phase 1 for Phase 1 features. Do not build Phase 1 features until those pipelines are live.

6. **The 20-trade paper validation (§0.7) must pass before further development is prioritised.** If pair-completion rate < 50%, redirect effort to the Open directional strategy. Do not continue investing in Opening Neutral until the pair-completion mechanics are validated in live CLOB conditions.

7. **Momentum handoff is a field mutation, not a re-registration.** When the loser exits, set `winner_pos.strategy = "momentum"`. Do not call `risk.open_position()` again — the position is already in the risk engine.

---

## Configuration

All parameters are in `config.py` under the `OPENING_NEUTRAL_*` namespace and can be overridden via `config_overrides.json`.

| Config Key | Default | Description |
|-----------|---------|-------------|
| `STRATEGY_OPENING_NEUTRAL_ENABLED` | `False` | Master on/off |
| `OPENING_NEUTRAL_DRY_RUN` | `True` | Simulate fills, place no real orders |
| `OPENING_NEUTRAL_SIZE_USD` | `1.0` | USDC per leg (YES buy = this, NO buy = this) |
| `OPENING_NEUTRAL_COMBINED_COST_MAX` | `1.01` | Skip if YES_ask + NO_ask > this. Do not loosen — only the 5th percentile qualifies and that is correct selectivity |
| `OPENING_NEUTRAL_LOSER_EXIT_PRICE` | `0.35` | Resting SELL price placed on both legs immediately after entry |
| `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD` | `0.15` | Skip if either YES or NO spread (ask−bid) > this. Cold book guard |
| `OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED` | `true` | Enable/disable the spread gate |
| `OPENING_NEUTRAL_ENTRY_WINDOW_SECS` | `120` | Only place BUY orders within this many seconds of market open |
| `OPENING_NEUTRAL_ENTRY_TIMEOUT_SECS` | `30` | Wait this long for both BUY legs to fill; one-leg fallback fires on timeout |
| `OPENING_NEUTRAL_ONE_LEG_FALLBACK` | `"keep_as_momentum"` | When only one BUY fills: `"keep_as_momentum"` or `"exit_immediately"` |
| `OPENING_NEUTRAL_ORDER_TYPE` | `"market"` | Entry BUY type: `"market"` for guaranteed fill; `"limit"` for post-only at ask |
| `OPENING_NEUTRAL_MAX_CONCURRENT` | `3` | Maximum simultaneous open neutral pairs |
| `OPENING_NEUTRAL_MARKET_TYPES` | all bucket types | Market types to watch |
| `OPENING_NEUTRAL_PREWARM_SECS` | `0.2` | Seconds before open to fire CLOB TCP pre-warm (idea 5) |
| `OPENING_NEUTRAL_TIMER_ADVANCE_SECS` | `0.05` | Seconds before open to fire scheduled entry evaluation |
| `OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED` | `false` | Enable funding-informed asymmetric sell pricing. **DISABLED until 2-week data validates** |
| `OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD` | `0.00001` | Funding rate threshold for asymmetric sell pricing |
| `OPENING_NEUTRAL_WINNER_SELL_BUFFER` | `0.03` | Extra headroom added to predicted winner's resting SELL to prevent early accidental fill |
| `OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED` | `false` | Enable loser confidence scoring (funding + depth share). **DISABLED until 2-week data validates** |
| `OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN` | `0.02` | Additional tighten on predicted loser's SELL when both funding and depth share agree |

---

## Order Flow

```
0. PRE-OPEN (presub window, 30s before open)
   ├── _refresh_pending_markets: static pre-qualification
   │     ├── Market type in OPENING_NEUTRAL_MARKET_TYPES?
   │     ├── Is an Up/Down market (_is_updown_market)?
   │     └── Not already entered?
   └── Schedule _scheduled_entry_task for open_ts

1. MARKET OPEN (0s to OPENING_NEUTRAL_ENTRY_WINDOW_SECS)
   Timer path (primary):
     T−200ms  _prewarm_clob(): warm TCP connection to CLOB
     T−50ms   _evaluate_entry(timer_fired=True): dynamic gates only
               ├── Combined cost: YES_ask + NO_ask ≤ COMBINED_COST_MAX
               ├── Spread gate:   YES spread ≤ 0.15 AND NO spread ≤ 0.15
               ├── No existing position in this market
               └── Open pairs < OPENING_NEUTRAL_MAX_CONCURRENT
     T+0ms    → spawn _enter_pair
   WS-tick path (fallback):
     On PM WS price tick → _on_price_event → _evaluate_entry (all gates)
     → spawn _enter_pair if qualifying (debounced 1/sec)

2. ENTRY: Place BUY orders concurrently
   asyncio.gather(
       pm.place_order(YES_token, BUY, ask_price, size),
       pm.place_order(NO_token,  BUY, ask_price, size),
   )

3. WAITING FOR FILLS
   ├── Both fill within ENTRY_TIMEOUT_SECS:
   │     ├── Register YES Position (strategy="opening_neutral", pair_id=X)
   │     ├── Register NO  Position (strategy="opening_neutral", pair_id=X)
   │     └── Place resting SELLs immediately:
   │           SELL YES @ yes_sell_price  (default $0.35; asymmetric if Phase 1 enabled)
   │           SELL NO  @ no_sell_price   (default $0.35; asymmetric if Phase 1 enabled)
   ├── Only one fills (timeout):
   │     ├── Keep filled leg as strategy="momentum" (one-leg fallback)
   │     └── Cancel the unfilled BUY
   └── Neither fills: cancel both BUY orders, no position registered

4. LOSER EXIT: Resting SELL fires on one leg
   ├── WS fill event → _on_fill(loser_order_id):
   │     ├── risk.close_position(loser_pos, price=$0.35)
   │     ├── pm.cancel_order(counterpart_sell_order_id)
   │     └── winner_pos.strategy = "momentum"; winner_pos.neutral_pair_id = ""
   └── Remove pair from _active_pairs

5. MOMENTUM HANDOFF
   Winner position is now strategy="momentum".
   monitor.py picks it up on the next sweep:
   delta SL, take-profit, and (if applicable) Momentum Phase 3 upfrac EWMA exit apply.

6. RESOLUTION (if resting SELLs never fill)
   monitor.py handles via RESOLVED path:
   winner pays $1.00, loser pays $0.00.
   Net: $1.00 − combined_cost (small loss at $1.02 combined cost).
```

---

## Entry Conditions

All must pass at T=0 for a pair to be entered.

| Gate | Type | Threshold | Rationale |
|------|------|-----------|-----------|
| Market type | Pre-qual | In `OPENING_NEUTRAL_MARKET_TYPES` | Only bucket markets with known open timestamps |
| Direction market | Pre-qual | `_is_updown_market()` = True | Only Up/Down markets have the neutral entry structure — specific-strike markets have asymmetric entry costs |
| Not already entered | Pre-qual | Not in `_entered_market_ids` | Prevent re-entry on same market |
| Combined cost | Dynamic | YES_ask + NO_ask ≤ `1.01` | Only the cheapest ~5th percentile qualifies |
| Spread gate | Dynamic | YES spread ≤ `0.15` AND NO spread ≤ `0.15` | Cold book guard — wide spread = unreliable $0.35 fill |
| No existing position | Dynamic | `risk.get_open_positions()` has no entry | Prevent duplicate position in same market |
| Concurrent cap | Dynamic | Open pairs < `OPENING_NEUTRAL_MAX_CONCURRENT` | Limit correlated simultaneous exposure |

---

## Exit Conditions

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | Resting SELL on YES fills at $0.35 | Close YES (loser), cancel NO SELL, mutate NO to Momentum |
| 2 | Resting SELL on NO fills at $0.35 | Close NO (loser), cancel YES SELL, mutate YES to Momentum |
| 3 | Only one entry BUY fills (timeout) | Keep that leg as standalone Momentum position |
| 4 | Market resolves without resting fill | winner = $1.00, loser = $0.00 via RESOLVED path |

After **step 1 or 2**, the winning leg is fully managed by Momentum (oracle delta SL, take-profit at $0.999, upfrac EWMA exit once Phase 3 is live).

---

## Implementation Architecture

```
strategies/OpeningNeutral/
├── __init__.py
├── scanner.py      # OpeningNeutralScanner: timer entry, pair management, resting SELL, loser exit
└── PLAN.md         # Original design document (architecture reference)
```

**Persisted state:**
```
data/market_open_spots.json   # shared with Momentum for Up/Down market reference prices
```

**Internal scanner state:**
```
_pending_markets             dict[condition_id → PMMarket]       markets awaiting open
_token_to_pending            dict[token_id → condition_id]       YES entry path
_scheduled_entry_market_ids  set[condition_id]                   markets with active timer tasks
_active_pairs                dict[pair_id → {
                                 yes_pos, no_pos,
                                 yes_exit_order_id,
                                 no_exit_order_id,
                                 market_id, market_title
                             }]
_entered_market_ids          set[condition_id]                   prevents re-entry
_entering_markets            set[condition_id]                   in-flight guard
```

### Key design decisions

1. **Timer-driven entry, not WS-tick-driven.** Market open timestamps are known in advance. The scanner schedules entry exactly at `open_ts` — no dependency on the first WS tick arriving. WS-tick path is fallback for markets already open when the bot starts.

2. **TCP pre-warm.** 200ms before open, a lightweight CLOB GET warms the underlying TCP+TLS connection so both BUY POSTs start on an already-open socket.

3. **Static pre-qualification.** Direction check and market type check happen once at registration. T+0 evaluation is dynamic gates only (combined cost, spread, cap, dedup).

4. **WS fill detection for loser exit.** `pm.register_fill_future(order_id, future)` — the resting SELL fill resolves the future via the WS stream. Zero polling.

5. **Momentum handoff is a field mutation.** `winner_pos.strategy = "momentum"` — no re-registration needed. The position is already in `risk.get_open_positions()`.

---

## Current Development Phase

> Full detail in `OPENING_NEUTRAL_IMPL_PLAN.md`. Source of all data-validated decisions: `strategy_update.md` Part 0.

| Phase | Status | Description |
|-------|--------|-------------|
| Base implementation | ✅ Built | Timer entry, TCP prewarm, resting SELL, WS fill detection, Momentum handoff |
| P0: Spread gate (§0.2) | 🔲 To build | 1-line cold-book guard in `_qualify_entry()`. 15 min. No dependencies. |
| P0: Logging columns | 🔲 To build | `yes_spread`, `no_spread`, `funding_rate`, `loser_leg`, `loser_fill_time_secs` |
| P1: Asymmetric sells (§0.3) | 🔲 Disabled | Requires `FundingRateCache` (Momentum Phase 1). Ships disabled; enable after 2-week data. |
| P1: Loser confidence (§0.4) | 🔲 Disabled | Requires `FundingRateCache` + `PMClient.get_depth_share()`. Ships disabled. |
| P2: Paper validation | ⏳ Pending | 20 paper trades must pass 3 thresholds before further development |

**Gate pipeline after P0:**
```
for market at open:
    ├── static pre-qual: market type, Up/Down direction, not entered  (pre-open)
    ├── combined cost: YES_ask + NO_ask ≤ 1.01                        (T+0)
    ├── spread gate:   YES_spread ≤ 0.15 AND NO_spread ≤ 0.15        (T+0) NEW
    ├── no existing position + concurrent cap                         (T+0)
    └── ENTER PAIR
         └── place resting SELLs (asymmetric pricing if Phase 1 enabled)
```

---

## Implementation Checklist

### Implemented (before paper trading)

- [x] `strategies/OpeningNeutral/scanner.py` — `OpeningNeutralScanner` with timer entry
- [x] Dual concurrent BUY at open via `asyncio.gather`
- [x] Resting SELL placed on both legs immediately after entry fills
- [x] WS fill detection for loser-leg exit (`register_fill_future`)
- [x] Momentum handoff: `winner_pos.strategy = "momentum"` on loser exit
- [x] One-leg fallback on entry timeout
- [x] TCP pre-warm (`_prewarm_clob`) 200ms before open
- [x] Static pre-qualification at registration time
- [x] `_entered_market_ids` deduplication guard
- [x] `/opening_neutral/status` API endpoint

### To build (P0 — before first paper trades)

- [ ] Spread gate: `YES_spread ≤ 0.15 AND NO_spread ≤ 0.15` in `_qualify_entry()`
- [ ] Fills logging: `yes_spread`, `no_spread`, `loser_leg`, `loser_fill_time_secs`, `winner_exit_price`

### To build (P1 — after Momentum Phase 1 pipelines are live, default disabled)

- [ ] `OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED` — asymmetric sell pricing from funding
- [ ] `OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED` — loser confidence score (funding + depth share)
- [ ] Fills logging: `funding_rate`, `yes_depth_share`, `loser_confidence_score`, `yes_sell_price_placed`, `no_sell_price_placed`

### Validation gate (P2 — before further investment)

- [ ] 20 paper trades: pair-completion > 50%, winner conversion > 60%, P&L/pair > $0.25
