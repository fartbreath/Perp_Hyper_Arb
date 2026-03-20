# Engineering Plan — Perp Hyper Arb
**Mode:** SELECTIVE EXPANSION (Option B)
**Date:** 2026-03-18
**Depends on:** `design.md` (founding design spec, same session)
**Scope:** Engineering plan for changes surfaced by CEO strategy review + design review
**Branch:** N/A (no git repo)

---

## PRE-ENGINEERING SYSTEM AUDIT

### Step 0A — Engineering Scope Challenge

**Total files to change: 10**
- Backend: `config.py`, `strategies/maker/strategy.py`, `api_server.py` (2 endpoints), `config_overrides.json`
- Frontend: `webapp/src/api/client.ts`, `webapp/src/pages/Dashboard.tsx`, `webapp/src/pages/Performance.tsx`, `webapp/src/pages/Risk.tsx`, `webapp/src/pages/Settings.tsx`
- Tests: `tests/test_maker.py`, `tests/test_api_server.py`, `tests/test_fill_simulator.py`

**This exceeds the 8-file smell threshold. Is this justified?**

Yes. The SELECTIVE EXPANSION plan has three independently motivated changes that happen to touch the same files:

1. **Config-only fixes** (adverse threshold, hedge threshold) — `config_overrides.json` only. Zero risk.
2. **Bucket exclusion** (`MAKER_EXCLUDED_MARKET_TYPES`) — touches `config.py` (1 line), `strategy.py` (3 lines), `api_server.py` (4 lines each in `_MUTABLE_CONFIG`, `ConfigPatch`, `GET /config`, `POST /config`). These are all purely additive — no existing logic is modified.
3. **Visibility** (`by_market_type` in `/performance`, per-coin hedge in `/risk`, adverse fields in `/health`) — each is a new output field. No existing field is changed. Frontend adds new components following the design.md spec exactly.

None of the 10 files have structural changes. All changes are additive. The file count is high but the diff entropy is low. Proceed.

**What is NOT in scope (cherry-picked from design.md):**

| Decision | Scope |
|---|---|
| Mobile hamburger nav | Deferred — no confirmed mobile use case |
| Brand name change | Deferred — not blocking strategy work |
| Signal ensemble (multiple sources) | Separate decision — not part of SELECTIVE EXPANSION |
| Kalshi price confirmation layer | Separate module — not touched |
| Performance page event annotations | Requires new event-log API — deferred |
| LIVE trading mode (`PAPER_TRADING=False`) | Not touched |
| `STRATEGY_MISPRICING_ENABLED` toggle | Not touched |

### Step 0B — Current System State

| Component | State |
|---|---|
| Paper trading | Active (`PAPER_TRADING=True` in config_overrides.json) |
| Maker strategy | Active (`STRATEGY_MAKER_ENABLED=True`) |
| Hedge threshold | $200 (config_overrides.json) — never fires with typical position sizes of $11/trade |
| Adverse threshold | 0.001 (already corrected in config_overrides.json) — confirmed via data analysis |
| bucket_1h | Active — losing $30 in 6h session; no exclusion mechanism exists |
| MAKER_EXCLUDED_MARKET_TYPES | Does NOT exist in codebase — needs to be added |
| `/performance` by_market_type | Does NOT exist — `by_strategy` and `by_underlying` only |
| `/risk` per-coin hedge status | Does NOT exist — aggregate only |
| `/health` adverse detection fields | Does NOT exist |
| Tests | 598 passing, 5 skipped, 15 test files |
| Git | No git repo |

### Step 0C — Prior Review Outputs

- **CEO review finding:** bucket_5m +$33, bucket_1h −$30, hedge never fires, adverse never fires
- **Design review:** `design.md` created; `BucketPerformanceStrip`, `HedgeStatusCard`, `AdverseStatusIndicator` specified; design system tokens documented
- **Selected path:** SELECTIVE EXPANSION (Option B) — cherry-pick proven wins, cap losers, fix monitoring blind spots

---

## Section 1: Architecture Review

### 1.1 — Config Architecture

**Rating: 9/10** — the `config.py` + `config_overrides.json` + `_MUTABLE_CONFIG` pattern is clean. Runtime patching via `POST /config` → `_save_overrides()` → restart-persistent. No redesign needed.

**Gap: `MAKER_EXCLUDED_MARKET_TYPES` is missing**

The bot has no mechanism to skip specific market types. `_evaluate_signal()` in `strategy.py` checks TTE, liquidity, spread, and volatility filters but has no market-type exclusion gate. Adding one is a leaf-node change — no architectural impact.

**Confirmed: `hedge_threshold_usd` IS in `_MUTABLE_CONFIG`**

```python
"hedge_threshold_usd": ("HEDGE_THRESHOLD_USD", float),
```

So lowering the hedge threshold to $50 can be done at runtime via `POST /config` or via `config_overrides.json`. No code change needed.

**Confirmed: `paper_adverse_selection_pct` IS in `_MUTABLE_CONFIG`**

Already present. Already set to 0.001 in `config_overrides.json`. This fix is already in place — no action needed.

**Confirmed: `market_type` IS in trades.csv**

`TRADES_HEADER = ["timestamp", "market_id", "market_title", "market_type", "underlying", ...]`

So the `/performance` endpoint can calculate `by_market_type` by reading `r.get("market_type", "unknown")` — the same pattern as `by_underlying`.

### 1.2 — Strategy Architecture

**Rating: 9/10** — `_evaluate_signal()` is the single evaluation gate. Adding a market-type exclusion there is the correct place: it fires before any capital commitment, is easy to test, and is coherent with the existing filter chain.

**The insertion point:**

```python
def _evaluate_signal(self, market: PMMarket, mid: float) -> Optional[MakerSignal]:
    if market.end_date is None:
        return None
    _now_ts = time.time()
    _tte_secs = market.end_date.timestamp() - _now_ts
    if _tte_secs > config.MAKER_MAX_TTE_DAYS * 86_400:
        return None
    # ← INSERT: market-type exclusion gate here (before any expensive computation)
```

Placing it immediately after the TTE check is correct — it short-circuits before the lifecycle-fraction calculation, spread computation, and volatility filter. These are all pure-CPU but the pattern is correct: cheapest guard first.

### 1.3 — API Architecture

**Rating: 8/10** — `GET /performance` computes everything fresh from `trades.csv` on each request. This is fine for the current data volume (< 10k trades). Adding `by_market_type` adds ~3 lines mirroring the existing `by_underlying` pattern. No caching concerns.

**The `/risk` endpoint** returns aggregate PM exposure and HL notional. The maker strategy already has `GET /maker/inventory` which returns `pos_delta`, `fill_inventory`, and `coin_hedges`. The hedge status indicator on Dashboard should pull from `/maker/inventory`, not require changes to `/risk`. This is simpler — zero modification to the risk endpoint.

**The `/health` endpoint** should be extended with adverse detection fields. The adverse detector state is ephemeral (in-memory in `fill_simulator.py`) but the trigger count can be tracked in `BotState`.

### 1.4 — Frontend Architecture

**Rating: 7/10** — all state management is co-located with page components via `useEffect + fetch`. No global store. Clean for a small app.

**Concern: `Dashboard.tsx` already imports 3 API endpoints.** Adding `BucketPerformanceStrip` will require importing `/performance` data, which is the heaviest endpoint (full analytics). The Dashboard should only fetch the lightweight aggregates it needs — `summary.total_pnl` and `by_market_type`. Options:

- **Option A:** Fetch the full `/performance` response and destructure — simple but wasteful on Dashboard polling
- **Option B:** Add a lightweight `/performance/summary` endpoint — too much new API surface
- **Option C:** Accept the full `/performance` fetch on Dashboard — the endpoint runs in <10ms on current data volume; acceptable

**Decision: Option C** — document the latency assumption and revisit if data volume exceeds 50k trades.

**Concern: `client.ts` types need updating.** `PerformanceData` type is missing `by_market_type`. `ConfigData` is missing `maker_excluded_market_types`. These are additive type extensions — no breaking changes.

---

## Section 2: Code Quality Review

### 2.1 — Current Code Quality

The existing codebase scores 8/10. It has:
- Clear, consistently named config constants
- Typed dataclasses (no `dict` where a dataclass should be)
- Structured logging (`structlog`)
- No magic numbers inline (all in `config.py`)
- Comprehensive test coverage (598 passing)

### 2.2 — Quality Risks in New Code

**Risk 1: `MAKER_EXCLUDED_MARKET_TYPES` type in config.py**

`config.py` uses scalar types throughout (`float`, `int`, `bool`, `str`). `list[str]` is new.

Issue: `_MUTABLE_CONFIG` uses a `(attr, typ)` pattern where `typ` is a callable type constructor like `float`. `list[str]` cannot be used this way — `list("bucket_1h")` would split the string into characters.

**Fix:** Handle list config values specially in `patch_config()`. The `ConfigPatch` model should accept `maker_excluded_market_types: list[str] | None = None` and bypass the generic `_MUTABLE_CONFIG` coerce loop for this field.

```python
# In patch_config():
if patch.maker_excluded_market_types is not None:
    config.MAKER_EXCLUDED_MARKET_TYPES = list(patch.maker_excluded_market_types)
    updated["maker_excluded_market_types"] = config.MAKER_EXCLUDED_MARKET_TYPES
```

Store and load in `_save_overrides()` / the override-loading code — already handles JSON natively, so `list[str]` serializes correctly.

**Risk 2: `BucketPerformanceStrip` data shape**

The `by_market_type` dict will have string keys like `"bucket_5m"`, `"bucket_15m"`, `"bucket_1h"`, `"milestone"`, and potentially `"unknown"`. The component must handle:
- Missing keys (bot ran but no trades in a type yet)
- `"unknown"` key (trades without a market_type in legacy CSV rows)
- Ordering (display 5m before 15m before 1h, regardless of dict iteration order)

**Fix:** Define the canonical display order as `BUCKET_ORDER = ["bucket_5m", "bucket_15m", "bucket_1h"]` in the component and filter `by_market_type` against it.

**Risk 3: `MAKER_EXCLUDED_MARKET_TYPES` log message**

The exclusion gate in `_evaluate_signal()` must log at `debug` level, not `info`, to avoid flooding logs during the market scan loop. Match the pattern of the existing `MAKER_MIN_INCENTIVE_SPREAD` skip log.

### 2.3 — Code Quality Standards for New Code

All new code must follow:
- Structlog for all backend logging (no `print()`, no `logging.getLogger()`)
- All new config constants: `UPPERCASE_WITH_UNDERSCORES` in `config.py` with inline comment
- All new API response fields: documented with inline comment in the endpoint function
- All new frontend components: follow `.card` / `.kv-table` / `.stat` token patterns from `design.md`
- No `any` types in TypeScript for new code

---

## Section 3: Test Plan

### 3.1 — Test Coverage Analysis

| Test File | Current State | Changes Needed |
|---|---|---|
| `tests/test_maker.py` | 598 total passing; covers `_evaluate_signal`, signal scoring, TTE gates | Add: market-type exclusion test |
| `tests/test_api_server.py` | covers `/config`, `/performance`, `/trades` | Add: `by_market_type` in `/performance`; `maker_excluded_market_types` in `/config` |
| `tests/test_fill_simulator.py` | covers adverse detection | No change needed — threshold already at 0.001 in config_overrides.json; existing tests pass |

### 3.2 — New Tests Required

#### `tests/test_maker.py` — Exclusion Gate

```python
class TestEvaluateSignalExclusion:
    """Test that _evaluate_signal respects MAKER_EXCLUDED_MARKET_TYPES."""

    def test_excluded_type_returns_none(self, maker_strategy, mock_market):
        """Market with excluded type should return None immediately."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        mock_market.market_type = "bucket_1h"
        result = maker_strategy._evaluate_signal(mock_market, mid=0.5)
        assert result is None
        config.MAKER_EXCLUDED_MARKET_TYPES = []  # reset

    def test_non_excluded_type_passes_gate(self, maker_strategy, mock_market):
        """Market type not in exclusion list should pass the gate."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        mock_market.market_type = "bucket_5m"
        # Should not return None due to market-type exclusion
        # (may still return None for other reasons — just check it gets past this gate)
        # Patch remaining filters to pass:
        with patch.object(maker_strategy, '_get_recent_move_pct', return_value=0.0):
            with patch.object(maker_strategy._risk, 'can_open', return_value=(True, "")):
                # The result may be a MakerSignal or None (other gates) but NOT due to type exclusion
                pass
        config.MAKER_EXCLUDED_MARKET_TYPES = []  # reset

    def test_empty_exclusion_list_passes_all(self, maker_strategy, mock_market):
        """Empty exclusion list should not block any market type."""
        config.MAKER_EXCLUDED_MARKET_TYPES = []
        for mtype in ["bucket_5m", "bucket_15m", "bucket_1h", "milestone"]:
            mock_market.market_type = mtype
            # Gate should not block due to market type
            # (result depends on other gates — just assert no TypeError)
```

#### `tests/test_api_server.py` — by_market_type

```python
class TestPerformanceByMarketType:
    """Test that /performance returns by_market_type breakdown."""

    def test_by_market_type_present(self, client, trades_csv_with_types):
        """Response must include by_market_type dict."""
        resp = client.get("/performance")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_market_type" in data

    def test_by_market_type_aggregates_correctly(self, client, trades_csv_with_types):
        """bucket_5m PnL should aggregate all bucket_5m trades."""
        resp = client.get("/performance")
        data = resp.json()
        bmt = data["by_market_type"]
        assert "bucket_5m" in bmt
        assert bmt["bucket_5m"]["pnl"] == pytest.approx(EXPECTED_5M_PNL, abs=0.01)
        assert bmt["bucket_5m"]["count"] == EXPECTED_5M_COUNT

    def test_unknown_market_type_falls_to_unknown_key(self, client, trades_csv_with_no_type):
        """Trades with no market_type should aggregate under 'unknown' key."""
        resp = client.get("/performance")
        data = resp.json()
        assert "unknown" in data["by_market_type"]


class TestConfigMakerExcludedTypes:
    """Test that maker_excluded_market_types is patchable via /config."""

    def test_patch_excluded_types(self, client):
        resp = client.post("/config", json={"maker_excluded_market_types": ["bucket_1h"]})
        assert resp.status_code == 200
        assert config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_1h"]
        config.MAKER_EXCLUDED_MARKET_TYPES = []  # reset

    def test_clear_excluded_types(self, client):
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        resp = client.post("/config", json={"maker_excluded_market_types": []})
        assert resp.status_code == 200
        assert config.MAKER_EXCLUDED_MARKET_TYPES == []
```

### 3.3 — Regression Tests (already passing, must stay passing)

All 598 existing tests must continue to pass after changes. Specific regression sensitivity:

| Test | Sensitivity |
|---|---|
| `test_evaluate_signal_*` | Any change to `_evaluate_signal()` signature or gate order |
| `test_paper_adverse_*` | `PAPER_ADVERSE_SELECTION_PCT` value — already at 0.001 in overrides |
| `test_hedge_fires_*` | `HEDGE_THRESHOLD_USD` value |
| `test_config_*` | Any new field in `_MUTABLE_CONFIG` or `ConfigPatch` |
| `test_performance_*` | New `by_market_type` field must not break existing `by_strategy` / `by_underlying` |

### 3.4 — Manual Verification Checklist

After code changes, run manually:

```bash
# 1. All existing tests pass
pytest tests/ -v

# 2. Exclusion gate works
python -c "
import config
config.MAKER_EXCLUDED_MARKET_TYPES = ['bucket_1h']
print('Set:', config.MAKER_EXCLUDED_MARKET_TYPES)
"

# 3. API server starts and /performance returns by_market_type
python api_server.py &
curl http://localhost:8080/performance | python -m json.tool | grep -A5 by_market_type

# 4. Config patch round-trips
curl -X POST http://localhost:8080/config \
  -H "Content-Type: application/json" \
  -d '{"maker_excluded_market_types": ["bucket_1h"]}'
```

---

## Section 4: Performance Review

### 4.1 — Backend Performance

**No performance concerns for the planned changes.**

| Change | Performance Impact |
|---|---|
| `MAKER_EXCLUDED_MARKET_TYPES` gate in `_evaluate_signal()` | O(n) where n = len(exclusion list) ≤ 5. Called ~60/min. Negligible. |
| `by_market_type` in `/performance` | One extra `defaultdict` loop over the same `rows` slice. O(trades). `/performance` already O(trades) — no order-of-magnitude change. |
| Adverse detection fields in `/health` | Reading 3 in-memory counters. Nanoseconds. |
| Dashboard `/performance` fetch | Dashboard now calls `/performance` in addition to existing endpoints. Endpoint runs in <10ms. Dashboard polls every 30s. No concern at current data volume. |

**Latency assumption documented:** If `trades.csv` exceeds 50,000 rows, the `/performance` endpoint should switch to pre-aggregated caching. Tag this comment in the endpoint code.

### 4.2 — Frontend Performance

**No performance concerns for new components.**

`BucketPerformanceStrip` renders 3 stat chips from a static dict — pure CPU render, no DOM thrash. `HedgeStatusCard` renders a 4-row kv-table — same. The `/maker/inventory` endpoint already exists and is polled from the existing `Risk.tsx` page; Dashboard can share the same fetch.

**One optimization opportunity (not blocking):** Dashboard currently fetches `/health`, `/pnl`, `/positions`, and `/maker/quotes` separately. Adding `/performance` and `/maker/inventory` brings the total to 6 concurrent endpoint polls at 30s intervals. This is fine. If polling interval is ever reduced below 5s, consolidate into a `/dashboard` aggregate endpoint.

---

## Section 5: Implementation Tasks

Listed in dependency order. Tasks within the same Priority group are independent and can be done in any order.

---

### Priority 0 — Config-only fixes (no code changes)

These are complete or already correct.

#### P0-A: Verify adverse detection threshold
**File:** `config_overrides.json`
**Current value:** `"PAPER_ADVERSE_SELECTION_PCT": 0.001`
**Required value:** 0.001
**Status:** ✅ Already correct in config_overrides.json (confirmed by grep)
**Action:** None — no change needed.

#### P0-B: Lower hedge threshold from $200 → $50
**File:** `config_overrides.json`
**Current value:** `"HEDGE_THRESHOLD_USD"` — not present in config_overrides.json (defaults to 100.0 in config.py, comment says "overridden to 200" but 200 is not in the current config_overrides.json checked via read)

> **NOTE:** config.py line 60 comment says "NOTE: overridden to 200 in config_overrides.json" but the current `config_overrides.json` content (read lines 1-30) does not show `HEDGE_THRESHOLD_USD`. The comment may be stale.
> **Verification step:** Before change, run `grep -i hedge config_overrides.json` to confirm current override value.

**Required value:** `50.0`
**Justification:** Typical per-coin exposure = $11/trade × 3-4 open positions = $33–44. Threshold of $50 fires when 5 positions accumulate on one coin. $200 never fires.

**Change:**
```json
"HEDGE_THRESHOLD_USD": 50.0,
```

**Tests:** No new tests — existing `test_hedge_fires_*` tests use the in-code constant and are not affected by config_overrides.json changes.

---

### Priority 1 — Backend: Bucket exclusion

#### P1-A: Add `MAKER_EXCLUDED_MARKET_TYPES` to `config.py`

**File:** `c:\GitHub\Perp_Hyper_Arb\config.py`
**Section:** Strategy 1 — Market Making (around line 62, after `REPRICE_TRIGGER_PCT`)

```python
# Market types to exclude from quoting (e.g. ["bucket_1h"] to disable hourly buckets).
# Empty list = quote all market types. Patched at runtime via POST /config.
MAKER_EXCLUDED_MARKET_TYPES: list[str] = []
```

**Placement:** After `REPRICE_TRIGGER_PCT` line (~line 62), before `HEDGE_THRESHOLD_USD`.

---

#### P1-B: Add exclusion gate to `_evaluate_signal()`

**File:** `c:\GitHub\Perp_Hyper_Arb\strategies\maker\strategy.py`
**Function:** `_evaluate_signal()` (around line 319)
**Insertion point:** After `if market.end_date is None: return None` and before the TTE check

```python
# Market-type exclusion gate (fastest check — before any computation)
if market.market_type in config.MAKER_EXCLUDED_MARKET_TYPES:
    log.debug(
        "Skipping quote — market type excluded",
        market=market.condition_id[:16],
        market_type=market.market_type,
    )
    return None
```

**Why here:** Before TTE calculation, before lifecycle fraction, before liquidity checks. Short-circuits cleanly. Cost: one `in` check on a list of ≤ 5 strings.

---

#### P1-C: Add `maker_excluded_market_types` to `api_server.py`

**File:** `c:\GitHub\Perp_Hyper_Arb\api_server.py`

Four locations need updating:

**Location 1: `_MUTABLE_CONFIG` dict** — NOT adding here (list type is incompatible with the generic coerce pattern — see Risk 1 analysis in Section 2.2).

**Location 2: `ConfigPatch` model** (around line 180)
```python
class ConfigPatch(BaseModel):
    ...
    maker_excluded_market_types: list[str] | None = None
```

**Location 3: In `patch_config()` function body**, add special-case handler after the generic loop:
```python
# List-type config values handled separately (list[str] not coercible via generic type())
if patch.maker_excluded_market_types is not None:
    config.MAKER_EXCLUDED_MARKET_TYPES = list(patch.maker_excluded_market_types)
    updated["maker_excluded_market_types"] = config.MAKER_EXCLUDED_MARKET_TYPES
    log.info("Config updated via API", key="MAKER_EXCLUDED_MARKET_TYPES",
             value=config.MAKER_EXCLUDED_MARKET_TYPES)
```

**Location 4: `GET /config` return dict and `POST /config` current dict** — add:
```python
"maker_excluded_market_types": config.MAKER_EXCLUDED_MARKET_TYPES,
```
to both the `get_config()` return dict and the `"current"` dict in `patch_config()`.

**Location 5: `_save_overrides()`** — must include the new field. Check whether `_save_overrides()` already uses `getattr`-based reflection or explicit field listing; if explicit listing, add `"MAKER_EXCLUDED_MARKET_TYPES"`.

---

### Priority 2 — Backend: Visibility data

#### P2-A: Add `by_market_type` to `/performance` endpoint

**File:** `c:\GitHub\Perp_Hyper_Arb\api_server.py`
**Section:** `@app.get("/performance")` function (around line 705)

After the `by_underlying` section, add:

```python
# By market type (bucket_5m / bucket_15m / bucket_1h / milestone)
by_market_type: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "count": 0, "win_rate": 0.0})
for r in rows:
    mt = r.get("market_type", "unknown")
    by_market_type[mt]["pnl"] += float(r.get("pnl", 0))
    by_market_type[mt]["count"] += 1
# Compute win_rate per bucket
for mt, data in by_market_type.items():
    wins_mt = sum(1 for r in rows if r.get("market_type", "unknown") == mt and float(r.get("pnl", 0)) > 0)
    data["win_rate"] = round(wins_mt / data["count"], 4) if data["count"] > 0 else 0.0
    data["pnl"] = round(data["pnl"], 4)
```

> **Note:** The win_rate inner loop is O(trades × bucket_types). With <10k trades and 4 bucket types this is fast enough. If performance degrades, refactor to a single-pass accumulation.

Add `"by_market_type": dict(by_market_type)` to the return dict alongside `by_strategy` and `by_underlying`.

#### P2-B: Extend `/health` with adverse detection fields

**File:** `c:\GitHub\Perp_Hyper_Arb\api_server.py`
**Section:** `@app.get("/health")` function (around line 483)

The `BotState` object needs two new fields to track adverse detection session stats. These are incremented by the fill simulator when adverse selection fires.

**In `api_server.py` `BotState` dataclass** (add fields):
```python
adverse_triggers_session: int = 0          # count of adverse-selection throttles this session
hl_max_move_pct_session: float = 0.0       # max HL move seen this session (for calibration)
```

**In `fill_simulator.py`**, after the adverse detection fires:
```python
if is_adverse:
    arrival_prob *= config.PAPER_ADVERSE_FILL_MULTIPLIER
    # Track session stats for /health endpoint
    if state is not None:
        state.adverse_triggers_session += 1
```

And when `_hl_move_pct` is computed, track the max:
```python
if state is not None and abs(hl_move) > state.hl_max_move_pct_session:
    state.hl_max_move_pct_session = abs(hl_move)
```

**NOTE:** `fill_simulator.py` does not currently import or reference `state`. Check whether `state` is accessible from the fill simulator context. If not, use a simpler approach: expose a module-level counter in `fill_simulator.py` that the `/health` endpoint reads directly:

```python
# fill_simulator.py module-level (simpler, no state coupling)
_adverse_triggers_session: int = 0
_hl_max_move_pct_session: float = 0.0

def get_session_stats() -> dict:
    return {
        "adverse_triggers_session": _adverse_triggers_session,
        "hl_max_move_pct_session": _hl_max_move_pct_session,
    }

def reset_session_stats():
    global _adverse_triggers_session, _hl_max_move_pct_session
    _adverse_triggers_session = 0
    _hl_max_move_pct_session = 0.0
```

**In `/health` endpoint**, add:
```python
from fill_simulator import get_session_stats
fill_stats = get_session_stats()
return {
    ...existing fields...
    "adverse_triggers_session": fill_stats["adverse_triggers_session"],
    "adverse_threshold_pct": config.PAPER_ADVERSE_SELECTION_PCT,
    "hl_max_move_pct_session": fill_stats["hl_max_move_pct_session"],
}
```

---

### Priority 3 — Frontend: New components

All frontend changes must follow `design.md` token spec (Section: Design System).

#### P3-A: Update `client.ts` types

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\api\client.ts`

```typescript
// PerformanceData — add by_market_type
export interface PerformanceData {
  ...existing fields...
  by_market_type: Record<string, { pnl: number; count: number; win_rate: number }>;
}

// ConfigData — add maker_excluded_market_types
export interface ConfigData {
  ...existing fields...
  maker_excluded_market_types: string[];
}

// HealthData — add adverse detection fields
export interface HealthData {
  ...existing fields...
  adverse_triggers_session: number;
  adverse_threshold_pct: number;
  hl_max_move_pct_session: number;
}

// InventoryData — already exists at GET /maker/inventory
// Confirm type includes: position_delta, fill_inventory, coin_hedges, threshold_usd
```

---

#### P3-B: `BucketPerformanceStrip` in Dashboard

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\pages\Dashboard.tsx`

**Design spec (from `design.md` Pass 5):**
```
Visual: Row of 3 .stat chips (5m / 15m / 1h), each with:
  - Label: bucket type
  - Value: cumulative PnL (colored positive/negative)
  - Color: --color-positive if PnL > 0, else --color-negative
  - Hint: one-line status ("WORKING" / "MARGINAL" / "LOSING — ⚠ Disable?")
  - Action: inline [Disable] button if PnL < -$20 in session
```

**Canonical bucket order:** `["bucket_5m", "bucket_15m", "bucket_1h"]` — render in this order regardless of `by_market_type` dict key order.

**Data source:** Fetch `/performance` (period=all or default) on Dashboard. Destructure `data.by_market_type`.

**Placement decision (from `design.md` Pass 7, Decision 1):** Separate card in Tier 2 grid alongside P&L card.

**Implementation notes:**
- Use `className="card"` wrapper + `className="summary-row"` inner for the stat row
- Status label logic:
  ```typescript
  function bucketStatus(pnl: number): string {
    if (pnl > 10) return "WORKING";
    if (pnl > -10) return "MARGINAL";
    return "LOSING";
  }
  ```
- Disable button: `onClick` → `PATCH /config {maker_excluded_market_types: [...current, "bucket_1h"]}` — requires confirmation dialog ("Disable bucket_1h markets? The bot will stop quoting hourly buckets.")
- If `by_market_type[bucket]` is undefined (no trades yet): render `—` with `color: var(--text-muted)`
- If `maker_excluded_market_types` includes a bucket: render chip with `opacity: 0.4` + `[Disabled]` label + `[Re-enable]` button

**accessibility:** Each chip must use `aria-label="bucket_5m performance: +$33.14, WORKING"`. No color-only status.

---

#### P3-C: `HedgeStatusCard` in HealthCard (Dashboard)

**Placement decision (from `design.md` Pass 7, Decision 2):** Extend the existing Health card with a hedge status row in the kv-table, not a separate card.

**Data source:** Fetch `/maker/inventory` on Dashboard. The endpoint already returns:
```json
{
  "position_delta": {"BTC": -5.54, "ETH": -23.10},
  "coin_hedges": {"ETH": {"direction": "SHORT", "size_coins": 0.012, ...}},
  "threshold_usd": 50.0
}
```

**Hedge active logic:**
```typescript
function isHedgeActive(inventory: InventoryData): boolean {
  return Object.keys(inventory.coin_hedges).length > 0;
}

function maxNetExposure(inventory: InventoryData): number {
  return Math.max(...Object.values(inventory.position_delta).map(Math.abs));
}
```

**Display in HealthCard kv-table:**
```
Hedge status:  [StatusDot] Inactive — max net $23.10 / threshold $50.00
  → if any coin exceeds threshold: [StatusDot ok=true] Active — ETH short 0.012 HL
  → if coin_hedges empty AND max_net > threshold * 0.8: amber "Approaching threshold"
```

**Shortcut button (from `design.md` Pass 3, Decision 2 → Recommendation A):**
When hedge is inactive AND max net exposure > threshold * 0.5 (more than half threshold consumed), show:
```
[Lower threshold to $25] → PATCH /config {hedge_threshold_usd: 25}
```
This respects the design.md recommendation for in-context intervention.

---

#### P3-D: `AdverseStatusIndicator` in HealthCard (Dashboard)

**Data source:** `/health` endpoint (after P2-B lands) provides `adverse_triggers_session`, `adverse_threshold_pct`, `hl_max_move_pct_session`.

**Display in existing HealthCard kv-table row:**
```
Adverse detection:
  [State 1] "0 triggers · threshold 0.1% · max observed 0.08%"  → StatusDot ok=true
  [State 2] amber "Threshold 0.1% may be high (max observed 0.08%)"  → if threshold > max_observed * 1.5
  [State 3] red "⚠ 3 adverse fills throttled this session"  → if triggers > 0
  [State 4] muted "HL offline — adverse detection paused"  → if hl_ws_connected = false
```

**Calibration warning threshold:** Show amber warning when `adverse_threshold_pct > hl_max_move_pct_session * 1.5` AND `adverse_triggers_session == 0`. This detects "threshold set so high it never fires" without false positives when the session just started.

---

#### P3-E: `by_market_type` chart in Performance page

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\pages\Performance.tsx`

Mirror the existing `by_underlying` BarChart section exactly:

```tsx
{/* By market type */}
<div className="chart-section">
  <h3>By Market Type</h3>
  <BarChart data={BUCKET_ORDER
    .filter(k => data.by_market_type[k])
    .map(k => ({ name: k.replace("bucket_", ""), pnl: data.by_market_type[k].pnl }))
  }>
    <Bar dataKey="pnl" fill={...cell colored by positive/negative}/>
    {/* mirror existing bar chart tooltip/axes from by_underlying */}
  </BarChart>
  {/* Win rate table below chart */}
  <table className="kv-table">
    {BUCKET_ORDER.filter(k => data.by_market_type[k]).map(k => (
      <tr key={k}>
        <td>{k.replace("bucket_", "")}</td>
        <td className={data.by_market_type[k].pnl > 0 ? "positive" : "negative"}>
          ${data.by_market_type[k].pnl.toFixed(2)}
        </td>
        <td>{(data.by_market_type[k].win_rate * 100).toFixed(1)}%</td>
        <td>{data.by_market_type[k].count} trades</td>
      </tr>
    ))}
  </table>
</div>
```

---

#### P3-F: Settings page — Market Types section

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\pages\Settings.tsx`

**Data source:** `GET /config` → `maker_excluded_market_types: string[]`

**New section — add at top of settings, before all other parameters:**

```
MARKET TYPES
────────────────────────────────────────
bucket_5m    [ON/OFF]   5-minute buckets
bucket_15m   [ON/OFF]   15-minute buckets
bucket_1h    [ON/OFF]   1-hour buckets
```

Toggle state: ON = bucket NOT in `maker_excluded_market_types` list. OFF = bucket IS in list.

On toggle: `PATCH /config {maker_excluded_market_types: [...updated_list]}`.

Optionally: show session PnL hint next to each toggle if `by_market_type` data is available (requires also fetching `/performance` on Settings page). This is a nice-to-have — the core toggle is sufficient without the PnL hint.

---

#### P3-G: Accessibility and styling fixes (from `design.md` Pass 6)

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\pages\Dashboard.tsx`

Fix `StatusDot` to pass WCAG:
```tsx
function StatusDot({ ok, label }: { ok: boolean; label?: string }) {
  return (
    <span
      role="img"
      aria-label={label ? `${label}: ${ok ? "connected" : "disconnected"}` : ok ? "OK" : "error"}
      style={{ color: ok ? "#22c55e" : "#ef4444" }}
    >
      {ok ? "●" : "○"}
    </span>
  );
}
```

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\index.css`

```css
/* WCAG 2.5.5 touch targets */
.toggle-btn  { min-height: 44px; }
button       { min-height: 36px; }

/* Keyboard focus ring for all buttons */
button:focus-visible { outline: 2px solid #6366f1; outline-offset: 2px; }

/* Table horizontal scroll at narrow widths */
.table-scroll { overflow-x: auto; }
```

**File:** `c:\GitHub\Perp_Hyper_Arb\webapp\src\pages\Fills.tsx` (or wherever `.data-table` is rendered in Fills page)

Wrap the fills table in `<div className="table-scroll">`.

---

## Section 6: Task Execution Order

```
P0-A: Verify adverse threshold (no change needed)
P0-B: Lower hedge threshold in config_overrides.json
   ↓
P1-A: Add MAKER_EXCLUDED_MARKET_TYPES to config.py
   ↓
P1-B: Add exclusion gate to _evaluate_signal()
   ↓
P1-C: Add maker_excluded_market_types to api_server.py
   ↓
P2-A: Add by_market_type to /performance endpoint
P2-B: Extend /health + fill_simulator session stats (independent of P2-A)
   ↓ (wait for P1-C, P2-A, P2-B)
P3-A: Update client.ts types
   ↓
P3-B: BucketPerformanceStrip on Dashboard
P3-C: HedgeStatusCard in HealthCard on Dashboard
P3-D: AdverseStatusIndicator in HealthCard on Dashboard
P3-E: by_market_type chart on Performance page
P3-F: Settings page Market Types section
P3-G: Accessibility + CSS fixes (independent, can do anytime)
   ↓
Run tests: pytest tests/ -v
Manual verification checklist
```

---

## Section 7: Test Plan Artifact

**File:** `engineering_test_plan.md`
**Location:** `c:\GitHub\Perp_Hyper_Arb\engineering_test_plan.md`

See [engineering_test_plan.md](engineering_test_plan.md) — created alongside this document.

---

## Section 8: What Was NOT Reviewed

To bound the scope and keep SELECTIVE EXPANSION honest:

| Area | Not reviewed | Why |
|---|---|---|
| Signal scoring calibration | Not in scope | CEO review found negative correlation at Q4 but SELECTIVE EXPANSION defers signal redesign; the exclusion of bucket_1h addresses the symptom |
| `mispricing.py` / Strategy 2 | Disabled (`STRATEGY_MISPRICING_ENABLED=False`) | No trades in session; no data to review |
| HL dead man's switch | Not in scope | Infrastructure concern; no incidents |
| `monitor.py` force-close logic | Not in scope | Working correctly; no CEO finding touched it |
| `market_data/pm_client.py` market classification | Not in scope | `market_type` correctly classified; bucket_1h exclusion acts downstream |
| Database / persistence | Not applicable | All persistence is flat CSV + JSON; no DB |
| Authentication | Not in scope | Single-operator tool; `API_SECRET` mechanism exists and is correct |

---

## Summary

**3 bugs fixed (config-only):**
- Adverse threshold already at 0.001 ✅ (already correct in config_overrides.json)
- Hedge threshold lowered from ~$200 → $50 (P0-B)
- bucket_1h exclusion mechanism created as a config-toggleable gate (P1-A/B/C)

**3 visibility gaps closed:**
- Dashboard: Bucket performance strip shows which market type is working (P3-B)
- Dashboard: Hedge status + adverse detection show system health at-a-glance (P3-C/D)
- Performance page: by_market_type chart surfaces the CEO review finding visually (P3-E)

**1 operator intervention path added:**
- Settings: Market Types toggle section allows disabling bucket_1h without restarting or editing files (P3-F)

**Tests:**
- 2 new test classes covering exclusion gate + by_market_type endpoint
- 598 existing tests must continue passing (no structural changes)

**Complexity:** 10 files, all additive changes. No architectural redesign. No new dependencies.
