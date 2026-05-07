# Reverse Opening Neutral (RON) — Strategy Plan

---

## Purpose

RON is a **paper-only experiment** to test whether the reverse exit logic produces better
EV than ON. RON never places real orders. It simulates what would have happened if, instead
of selling the loser and promoting the winner to momentum, we had sold the winner for TP and
held the loser to resolution.

We also add an optional 'doubledown' config where we set the USD amount that buys the loser position when we TP the winner. $0 means it is off.

Data from `on_fills.csv` (ON) and `ron_fills.csv` (RON) can be joined on `on_pair_id` to
compare outcomes for exactly the same entry.

---

## Scope Constraints

- **RON is permanently paper/dry-run.** No real orders, ever.
- **Minimal changes to ON.** ON is production code. RON is an experiment layered on top.
  Only one small hook is added to ON — a callback fired after a pair is successfully
  registered. Everything else in ON is untouched.
- **RON is only active if `REVERSE_OPENING_NEUTRAL_ENABLED: true`** in config_overrides.

---

## Architecture

RON does not scan markets independently. It piggybacks on ON's entry.

```
ON scanner ──► _register_pair() completes
                    │
                    └──► fires _on_pair_registered_callbacks  [1 new line in ON]
                                    │
                                    ▼
                         RON._on_on_entry_received(market, on_pair_id, yes_result, no_result)
                                    │
                                    ├── creates paper positions at the same entry prices
                                    ├── registers pair in RON's own _active_pairs
                                    ├── arms bid-monitoring on YES + NO tokens
                                    └── writes on_pair_id into _pair_csv_data for the CSV join
```

When the loser bid drops to `OPENING_NEUTRAL_LOSER_EXIT_TRIGGER` on RON's monitored tokens:
- RON simulates selling the WINNER at current best bid (no real order)
- RON holds the LOSER paper position open until resolution
- Optionally simulates a double-down buy on the LOSER
- Writes `ron_fills.csv` row with winner simulated TP price

The loser paper position is settled by `PositionMonitor` at resolution, which records
the outcome in `acct_ledger.csv` as `strategy="reverse_opening_neutral"`.

---

## Changes Required

### 1. `strategies/OpeningNeutral/scanner.py` — minimal

**One addition to `__init__`:**
```python
self._on_pair_registered_callbacks: list = []
```

**One new public method:**
```python
def register_pair_callback(self, cb) -> None:
    self._on_pair_registered_callbacks.append(cb)
```

**One new call at the end of `_register_pair`** (after the bid-monitoring arm block):
```python
for _cb in self._on_pair_registered_callbacks:
    asyncio.create_task(_cb(market, pair_id, yes_pos, no_pos))
```

The callback signature: `(market, on_pair_id: str, yes_pos: Position, no_pos: Position)`
— passes the already-built Position objects so RON doesn't need to re-fetch entry prices.

That is the complete change to ON. Three lines total, zero logic change.

### 2. `strategies/ReverseOpenNeutral/scanner.py`

**Override `_refresh_pending_markets` and `_evaluate_entry` as no-ops:**

RON never scans. All entry comes through the ON callback.

```python
async def _refresh_pending_markets(self) -> None:
    pass  # RON does not scan independently

async def _evaluate_entry(self, market: Any, _timer_fired: bool = False) -> None:
    pass  # RON does not self-evaluate
```

**Override `start()` to register the ON callback:**

```python
async def start(self) -> None:
    if not getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
        return
    if self._on_scanner is not None:
        self._on_scanner.register_pair_callback(self._on_on_entry_received)
    self._running = True
    log.info("ReverseOpenNeutral: started (paper-only, coupled to ON)")
```

**Add `_on_scanner` constructor argument:**

```python
def __init__(self, *args, on_scanner=None, **kwargs):
    super().__init__(*args, **kwargs)
    self._on_scanner = on_scanner
    _ensure_ron_fills_csv()
```

**Add `_on_on_entry_received` callback:**

```python
async def _on_on_entry_received(
    self,
    market: Any,
    on_pair_id: str,
    yes_pos: "Position",
    no_pos: "Position",
) -> None:
    """
    Called by ON scanner after a pair is registered.
    RON creates paper positions at the same entry prices and begins monitoring.
    """
    if not getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
        return

    pair_id = f"ron_{uuid.uuid4().hex[:12]}"
    market_id = getattr(market, "condition_id", "")
    yes_token_id = getattr(yes_pos, "token_id", "")
    no_token_id  = getattr(no_pos,  "token_id", "")

    # Build paper Position copies — same entry prices, tagged as RON.
    ron_yes = self._build_position(
        market, "YES",
        {"price": yes_pos.entry_price, "size": yes_pos.size, "order_id": f"ron_{uuid.uuid4().hex[:8]}"},
        yes_token_id, pair_id,
    )
    ron_no = self._build_position(
        market, "NO",
        {"price": no_pos.entry_price, "size": no_pos.size, "order_id": f"ron_{uuid.uuid4().hex[:8]}"},
        no_token_id, pair_id,
    )
    ron_yes.strategy = "reverse_opening_neutral"
    ron_no.strategy  = "reverse_opening_neutral"

    self._risk.open_position(ron_yes)
    self._risk.open_position(ron_no)

    self._active_pairs[pair_id] = {
        "market_id":    market_id,
        "market_title": getattr(market, "title", "")[:80],
        "yes_pos":      ron_yes,
        "no_pos":       ron_no,
        "yes_exit_order_id": "",
        "no_exit_order_id":  "",
        "entry_ts":     time.time(),
        "yes_trigger":  config.OPENING_NEUTRAL_LOSER_EXIT_TRIGGER,
        "no_trigger":   config.OPENING_NEUTRAL_LOSER_EXIT_TRIGGER,
    }
    # Store on_pair_id for CSV cross-reference join.
    self._pair_csv_data[pair_id] = {"on_pair_id": on_pair_id, "_entry_ts": time.time()}

    # Arm bid-monitoring on both legs.
    self._token_to_pair[yes_token_id] = pair_id
    self._token_to_pair[no_token_id]  = pair_id

    log.info(
        "ReverseOpenNeutral: paper pair created (mirroring ON entry)",
        on_pair_id=on_pair_id[:12],
        ron_pair_id=pair_id[:12],
        market=getattr(market, "title", "")[:60],
        yes_entry=ron_yes.entry_price,
        no_entry=ron_no.entry_price,
    )
```

**Override `_execute_loser_exit` to simulate only (no real orders):**

The existing `_execute_loser_exit` already contains the winner-sell logic. Override it to
skip the real `place_market` call and use the current best bid as the simulated fill price:

```python
# Instead of: order_id = await self._pm.place_market(...)
# Use:        winner_exit_price = winner_book.best_bid (simulated)
#             order_id = f"ron_sim_{uuid.uuid4().hex[:8]}"
```

The double-down (if enabled) is also simulated: no real order is placed; the loser position
size is increased by `RON_DOUBLE_DOWN_USD / current_ask`.

### 3. `launcher.py` — wire ON instance to RON

```python
on_scanner  = OpeningNeutralScanner(...)
ron_scanner = ReverseOpenNeutralScanner(..., on_scanner=on_scanner)
```

### 4. `config.py` — add RON keys

```python
# ── Strategy 5b — Reverse Opening Neutral (RON) ──────────────────────────────
REVERSE_OPENING_NEUTRAL_ENABLED: bool = False   # master gate
# Additional USDC to simulate buying more of the LOSER at winner TP time.
# 0.0 = disabled. Paper-only — no real order is placed.
RON_DOUBLE_DOWN_USD: float = 0.0
```

`REVERSE_OPENING_NEUTRAL_DRY_RUN` is removed — RON is always paper. There is no live mode.

---

## ron_fills.csv Schema Update

Add to the existing header:

| Column | Type | Description |
|---|---|---|
| `on_pair_id` | str | ON's pair_id — join key to `on_fills.csv` |
| `double_down_size` | float | Additional simulated contracts on loser (0.0 if disabled) |
| `double_down_price` | float | Simulated fill price for double-down (0.0 if disabled) |

---

## Data / Analysis Flow

```
on_fills.csv  ──┐
                ├── join on on_pair_id ──► compare ON vs RON for same entry
ron_fills.csv ──┘

acct_ledger.csv: ON rows → strategy="opening_neutral"
                 RON rows → strategy="reverse_opening_neutral"
```

---

## Configuration Summary

| Key | Default | Overridable |
|---|---|---|
| `REVERSE_OPENING_NEUTRAL_ENABLED` | `False` | Yes |
| `RON_DOUBLE_DOWN_USD` | `0.0` | Yes |
| All `OPENING_NEUTRAL_*` keys | inherited | Already overridable |

---

## What Does NOT Change

- ON scanner scanning logic, entry gates, bid-monitoring, exit logic: **untouched**
- Loser trigger price, market types, entry window, size: **same config, shared**
- `acct_ledger.csv` RON tagging (`reverse_opening_neutral`): **unchanged**
- All existing tests: **pass without modification**
- RON never places a real order under any configuration
