# Early Warning Stop-Loss Signals — Implementation Plan

## Problem Statement

Four of nine momentum losses involved the delta SL firing correctly but too late
(tte=5–9s) to fill before resolution. The Chainlink oracle publishes a cross only
after a ≥0.27% deviation confirms the move. HL perp mark price and CLOB depth
lead the oracle by 2–5 seconds on real directional moves; PM token price velocity
is a crowd-consensus leading indicator. These three signals are added as
**independent, early-trigger SL signals** alongside the existing delta SL.

Design contract:
- Each signal is **independent** — any one firing at sub-TTE-threshold is
  sufficient to exit. No AND-gate. No weighted score.
- All data comes from **existing WS streams**. Zero new subscriptions, zero polling.
- All signals are **disabled by default** via config so can be enabled/tuned
  safely in production without a code deploy.
- Each signal is **logged in momentum_ticks.csv** from day 1 so ML models can
  learn from the data once enough trades accumulate.

---

## Signal Definitions

### Signal A — HL Mark Price Divergence (`MOMENTUM_HL_MARK_SL_*`)

**Premise**: HL perp mark price is computed continuously from the perpetual CLOB
(every taker fill). It leads the Chainlink oracle (which fires only on ≥0.27%
deviation) by 2–5 seconds on real directional moves. When HL mark crosses the
strike while Chainlink oracle is still above it, the perpetual market has already
priced the cross.

**Data source**: `HLClient._fundings[coin]` — `markPx` already arrives in every
`webData2` push. The value is parsed but not stored or exposed today.

**Trigger logic**:
```
hl_mark_divergence_pct = (hl_mark - strike) / strike * 100  # UP position
                       = (strike - hl_mark) / strike * 100  # DOWN position
if (
    hl_mark_divergence_pct < MOMENTUM_HL_MARK_SL_THRESHOLD_PCT   # e.g. 0.0 = mark crossed strike
    AND tte_seconds < MOMENTUM_HL_MARK_SL_MAX_TTE                 # e.g. 30s
    AND NOT _suppress_taker_exits
):
    return True, ExitReason.MOMENTUM_HL_MARK_SL, unrealised
```

**Config keys to add** (with recommended defaults):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `MOMENTUM_HL_MARK_SL_ENABLED` | bool | `False` | Master enable. Off until tuned. |
| `MOMENTUM_HL_MARK_SL_THRESHOLD_PCT` | float | `0.0` | Divergence % at which SL fires. 0.0 = fire exactly when mark crosses strike. Negative = allow slack. |
| `MOMENTUM_HL_MARK_SL_MAX_TTE` | int | `30` | Only active when tte_seconds < this (seconds). |

---

### Signal B — HL Perp Order Book Depth Imbalance (`MOMENTUM_HL_DEPTH_SL_*`)

**Premise**: When the HL perp CLOB is heavily offer-sided (asks >> bids) at the
moment a position is approaching the strike, professional market participants are
selling the direction. This is a strong conviction signal at low TTE.

**Data source**: `HLClient` already subscribes to `l2Book` per coin via WS and
parses BBO (top-of-book only). The raw `bids` / `asks` arrays from the same
message contain multiple levels — the parser currently discards everything below
the best. We need to store depth totals for N levels.

**New computation**:
```
sum_bid_size = sum(float(level["sz"]) for level in bids[:N])
sum_ask_size = sum(float(level["sz"]) for level in asks[:N])
depth_imbalance = (sum_bid_size - sum_ask_size) / (sum_bid_size + sum_ask_size)
# +1.0 = all bids, -1.0 = all asks, 0.0 = balanced
```

**Position-adjusted imbalance** (in `_check_position`):
```
# Imbalance from the position's perspective:
# UP position suffers when asks are heavy (perp offered into the position)
# DOWN position suffers when bids are heavy (perp bid against the position)
hl_position_imbalance = depth_imbalance if side in (UP, YES) else -depth_imbalance
# Negative = market is positioned against this trade
```

**Trigger logic**:
```
if (
    hl_position_imbalance < -MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD  # e.g. -0.40
    AND tte_seconds < MOMENTUM_HL_DEPTH_SL_MAX_TTE                       # e.g. 30s
    AND NOT _suppress_taker_exits
):
    return True, ExitReason.MOMENTUM_HL_DEPTH_SL, unrealised
```

**Config keys to add**:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `MOMENTUM_HL_DEPTH_SL_ENABLED` | bool | `False` | Master enable. |
| `MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD` | float | `0.40` | Fire when imbalance against position exceeds this. 0.40 = 70% asks vs 30% bids. |
| `MOMENTUM_HL_DEPTH_SL_MAX_TTE` | int | `30` | Only active when tte_seconds < this (seconds). |
| `MOMENTUM_HL_DEPTH_SL_LEVELS` | int | `5` | Order book levels to sum for depth calculation. |

---

### Signal C — PM Token Price Velocity (`MOMENTUM_TOKEN_VELOCITY_SL_*`)

**Premise**: The PM CLOB token mid is already tracked per tick. The *rate of
change* over the last N seconds is a leading indicator that the crowd is repricing
the outcome. A token dropping at −3¢/s at tte=20s means the market expects to lose
before the oracle confirms it.

**Data source**: `current_token_price` already arrives in `_check_position` on
every oracle tick. Need a per-position ring buffer tracking `(ts, price)` pairs
accumulated across ticks.

**Storage**: Add `_token_price_history: dict[str, deque]` to `PositionMonitor`,
keyed by `pos.token_id`. At each `_check_position`, append
`(time.monotonic(), current_token_price)` before calling `should_exit()`.

**Velocity computation** (in `_check_position`):
```python
from collections import deque
history = self._token_price_history.setdefault(pos.token_id, deque(maxlen=50))
if current_token_price is not None:
    history.append((time.monotonic(), current_token_price))

token_velocity: Optional[float] = None
window = config.MOMENTUM_TOKEN_VELOCITY_WINDOW_SECS  # e.g. 10.0s
if len(history) >= config.MOMENTUM_TOKEN_VELOCITY_MIN_TICKS:
    # Find oldest point within the window
    now_mono = time.monotonic()
    cutoff = now_mono - window
    in_window = [(ts, px) for ts, px in history if ts >= cutoff]
    if len(in_window) >= config.MOMENTUM_TOKEN_VELOCITY_MIN_TICKS:
        dt = in_window[-1][0] - in_window[0][0]
        if dt > 0:
            token_velocity = (in_window[-1][1] - in_window[0][1]) / dt
```

**Trigger logic** (inside `should_exit()`):
```
if (
    token_velocity is not None
    and token_velocity < MOMENTUM_TOKEN_VELOCITY_SL_THRESHOLD   # e.g. -0.03 $/s
    and tte_seconds < MOMENTUM_TOKEN_VELOCITY_SL_MAX_TTE        # e.g. 45s
    AND NOT _suppress_taker_exits
):
    return True, ExitReason.MOMENTUM_TOKEN_VELOCITY_SL, unrealised
```

**Config keys to add**:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `MOMENTUM_TOKEN_VELOCITY_SL_ENABLED` | bool | `False` | Master enable. |
| `MOMENTUM_TOKEN_VELOCITY_SL_THRESHOLD` | float | `-0.03` | $/s threshold. Token dropping faster than this fires SL. |
| `MOMENTUM_TOKEN_VELOCITY_SL_MAX_TTE` | int | `45` | Only active when tte_seconds < this. Slightly wider than A/B because token leads the oracle. |
| `MOMENTUM_TOKEN_VELOCITY_WINDOW_SECS` | float | `10.0` | Ring buffer look-back window in seconds. |
| `MOMENTUM_TOKEN_VELOCITY_MIN_TICKS` | int | `3` | Minimum ticks in window before velocity is trusted. |

---

## Files to Change

### 1. `hl_client.py` — Expose mark price + depth imbalance

**A. `FundingSnapshot` dataclass** — add `mark_px` field:
```python
@dataclass
class FundingSnapshot:
    coin: str
    hl_predicted: Optional[float] = None
    binance_predicted: Optional[float] = None
    bybit_predicted: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    mark_px: Optional[float] = None   # ← NEW
```

**B. `webData2` handler** — already parses `mark_px` into local var. Store it:
```python
self._fundings[coin] = FundingSnapshot(
    coin=coin,
    hl_predicted=rate,
    timestamp=ts,
    mark_px=mark_px,   # ← add this
)
```

**C. `l2Book` handler** — currently discards depth below BBO. After extracting BBO,
also compute and cache depth imbalance:
```python
# New field: self._depth_imbalance: dict[str, float] = {}
levels = config.MOMENTUM_HL_DEPTH_SL_LEVELS  # or a fixed constant
sum_bid = sum(float(b["sz"]) for b in bids[:levels]) if bids else 0.0
sum_ask = sum(float(a["sz"]) for a in asks[:levels]) if asks else 0.0
total = sum_bid + sum_ask
if total > 0:
    self._depth_imbalance[coin] = (sum_bid - sum_ask) / total
```

**D. New accessors**:
```python
def get_mark_price(self, coin: str) -> Optional[float]:
    snap = self._fundings.get(coin)
    return snap.mark_px if snap is not None else None

def get_depth_imbalance(self, coin: str) -> Optional[float]:
    return self._depth_imbalance.get(coin)
```

---

### 2. `monitor.py` — Three changes

**A. `PositionMonitor.__init__`** — add ring buffer dict:
```python
self._token_price_history: dict[str, deque] = {}
```

**B. `_check_position()`** — after computing `current_token_price`, before calling
`should_exit()`:
1. Update token price ring buffer → compute `token_velocity`
2. Fetch `hl_mark_price = self._hl.get_mark_price(pos.underlying)` if `self._hl` is set
3. Fetch raw imbalance `= self._hl.get_depth_imbalance(pos.underlying)` and adjust for
   side → `hl_position_imbalance`
4. Pass all three to `should_exit()`

Clean up ring buffer on position close (in `_exit_position` or equivalent):
```python
self._token_price_history.pop(pos.token_id, None)
```

**C. `should_exit()` signature** — add three new optional params:
```python
def should_exit(
    pos: Position,
    ...existing params...,
    hl_mark_price: Optional[float] = None,
    hl_position_imbalance: Optional[float] = None,
    token_velocity_per_sec: Optional[float] = None,
) -> tuple[bool, str, float]:
```

Inside the momentum block, after the existing delta SL block and before
near-expiry stop, add three independent trigger blocks (one per signal), each
gated by its own `*_ENABLED` flag. **Order matters**: fire in ascending TTE
threshold order so the widest signal (token velocity, 45s) is checked first,
narrowest last (HL signals, 30s). This ensures each has a chance to fire before
the later, narrower ones.

**D. `_write_momentum_tick()` and `MOMENTUM_TICKS_CSV`** — add four new columns:
```
hl_mark_price, hl_mark_divergence_pct, hl_depth_imbalance, token_velocity_per_sec
```
These are logged on every tick so the ML model can correlate signal values at the
moment of exit with outcomes. `_check_position()` must pass them down to the
writer.

**E. Add new `ExitReason` values**:
```python
MOMENTUM_HL_MARK_SL        = "hl_mark_sl"
MOMENTUM_HL_DEPTH_SL       = "hl_depth_sl"
MOMENTUM_TOKEN_VELOCITY_SL = "token_velocity_sl"
```

---

### 3. `config.py` — Add 13 new constants

Add to the `# ── Momentum ──` section:
```python
# ── Early Warning SL: HL Mark Divergence ────────────────────────────────────
MOMENTUM_HL_MARK_SL_ENABLED:       bool  = False
MOMENTUM_HL_MARK_SL_THRESHOLD_PCT: float = 0.0
MOMENTUM_HL_MARK_SL_MAX_TTE:       int   = 30

# ── Early Warning SL: HL Perp Depth Imbalance ────────────────────────────────
MOMENTUM_HL_DEPTH_SL_ENABLED:             bool  = False
MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD: float = 0.40
MOMENTUM_HL_DEPTH_SL_MAX_TTE:             int   = 30
MOMENTUM_HL_DEPTH_SL_LEVELS:              int   = 5

# ── Early Warning SL: PM Token Price Velocity ────────────────────────────────
MOMENTUM_TOKEN_VELOCITY_SL_ENABLED:    bool  = False
MOMENTUM_TOKEN_VELOCITY_SL_THRESHOLD:  float = -0.03
MOMENTUM_TOKEN_VELOCITY_SL_MAX_TTE:    int   = 45
MOMENTUM_TOKEN_VELOCITY_WINDOW_SECS:   float = 10.0
MOMENTUM_TOKEN_VELOCITY_MIN_TICKS:     int   = 3
```

All off by default. Turn on in `config_overrides.json` via webapp settings.

---

### 4. `webapp/src/pages/Settings.tsx` — New settings card

Location: After the existing "Momentum — Winner SL Tuning" card (the `div.card`
ending around line 790), insert a new card **"Momentum — Early Warning SL Signals"**.

Structure (three sub-sections, one per signal, each with a master Toggle and
one or two tuning fields):

```
Card: "Momentum — Early Warning SL Signals"
  Description paragraph explaining the signals are independent triggers that
  fire before the Chainlink oracle confirms a cross.

  ── Signal A: HL Mark Price Divergence ──
  Toggle:  MOMENTUM_HL_MARK_SL_ENABLED
  Slider:  MOMENTUM_HL_MARK_SL_THRESHOLD_PCT  (step 0.1, unit "%")
  Number:  MOMENTUM_HL_MARK_SL_MAX_TTE        (step 5, unit "s")

  GAP

  ── Signal B: HL Perp Depth Imbalance ──
  Toggle:  MOMENTUM_HL_DEPTH_SL_ENABLED
  Slider:  MOMENTUM_HL_DEPTH_SL_IMBALANCE_THRESHOLD  (step 0.05, unit "ratio")
  Number:  MOMENTUM_HL_DEPTH_SL_MAX_TTE              (step 5, unit "s")
  Number:  MOMENTUM_HL_DEPTH_SL_LEVELS               (step 1, unit "levels", Advanced)

  GAP

  ── Signal C: Token Price Velocity ──
  Toggle:  MOMENTUM_TOKEN_VELOCITY_SL_ENABLED
  Slider:  MOMENTUM_TOKEN_VELOCITY_SL_THRESHOLD  (step 0.005, unit "$/s")
  Number:  MOMENTUM_TOKEN_VELOCITY_SL_MAX_TTE    (step 5, unit "s")
  Number:  MOMENTUM_TOKEN_VELOCITY_WINDOW_SECS   (step 1, unit "s", Advanced)
  Number:  MOMENTUM_TOKEN_VELOCITY_MIN_TICKS     (step 1, unit "ticks", Advanced)
```

The toggle key names follow existing conventions:
- `MOMENTUM_HL_MARK_SL_ENABLED` → `data.momentum_hl_mark_sl_enabled`
- etc.

---

### 5. `models/feature_snapshot.py` — Future ML columns (collect now, train later)

**Phase 1 (this build)**: The new tick columns are logged but NOT added to
`MODEL_B_FEATURES`. Follow the existing commented-out pattern:
```python
# v6: early-warning SL signals — ADD once 200+ exits have been logged with
# signal values (currently null in all historical rows).
# "exit_hl_mark_divergence_pct",
# "exit_hl_depth_imbalance",
# "exit_token_velocity_per_sec",
# "exit_tte_secs",
```

**Phase 2 (future, once data accumulates)**:
- Add the four features to `MODEL_B_FEATURES`
- Update `build_exit_snapshot()` to read them from the ticks row joined on
  `market_id + exit_tick_ts`
- Retrain model_b with the new feature set
- New features answer: "given the crowd and perp signals at the moment the SL
  fired, was this a correct or false-positive exit?"

The `on_hl_mark_price` feature already in MODEL_B is an **entry-time** mark price.
These new features are **exit-time** signal values — distinct and complementary.

---

## Execution Order

| Step | File | Description |
|------|------|-------------|
| 1 | `hl_client.py` | Add `mark_px` to `FundingSnapshot`; store in webData2 handler; add depth cache + accessor in l2Book handler; add `get_mark_price()` and `get_depth_imbalance()` |
| 2 | `config.py` | Add 13 new constants |
| 3 | `monitor.py` | Add `ExitReason` values; extend `should_exit()` signature + 3 trigger blocks; update `_check_position()` to compute and pass signals; extend tick CSV columns + `_write_momentum_tick()` |
| 4 | `webapp/src/pages/Settings.tsx` | Add new card with 3 signal sub-sections |
| 5 | `models/feature_snapshot.py` | Add commented-out v6 feature placeholders |
| 6 | `tests/` | Unit tests for each new trigger block in `should_exit()` with signal values above/below threshold; test that `hl_mark_price=None` skips the block |

---

## Testing Strategy

Each signal block is independently unit-testable by calling `should_exit()` with
a `momentum` position and injecting only the new signal parameter, with everything
else set to values that would NOT trigger the existing delta SL:

```python
# Signal A test: HL mark has crossed strike, TTE=20s, Chainlink still above
result = should_exit(
    pos=pos_btc_up, ...,
    current_spot=78_050,   # oracle still above strike 78,000 — no delta SL
    hl_mark_price=77_800,  # perp already below strike
    tte_seconds=20.0,
)
assert result[0] is True
assert result[1] == "hl_mark_sl"

# Signal A disabled test: same inputs but ENABLED=False → no exit
with override(MOMENTUM_HL_MARK_SL_ENABLED=False):
    result = should_exit(...)
    assert result[0] is False
```

Same pattern for Signals B and C.

---

## Calibration Notes (post-deployment)

Once the signals are logging in momentum_ticks.csv but disabled:
1. Run analysis on tick rows where `exit=True AND reason=momentum_stop_loss` —
   check what `hl_mark_divergence_pct` and `token_velocity_per_sec` were at those
   ticks. This tells you whether the signals would have fired earlier.
2. Run analysis on tick rows where `exit=False` near the market end — check
   `false_positive_rate`: how many times would each signal have fired on positions
   that resolved WIN.
3. Set threshold and TTE gate based on that data before enabling.
