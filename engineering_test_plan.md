# Test Plan — SELECTIVE EXPANSION Engineering Changes
**Project:** Perp Hyper Arb
**Date:** 2026-03-18
**Scope:** All test assertions for changes defined in engineering.md

---

## T1 — Unit Tests (pytest)

### T1.1 `tests/test_maker.py` — Bucket exclusion gate

| ID | Test | Expected |
|---|---|---|
| T1.1.1 | `_evaluate_signal(market_type="bucket_1h")` when `MAKER_EXCLUDED_MARKET_TYPES=["bucket_1h"]` | Returns `None` |
| T1.1.2 | `_evaluate_signal(market_type="bucket_5m")` when `MAKER_EXCLUDED_MARKET_TYPES=["bucket_1h"]` | Does NOT return `None` due to market-type gate (may return None for other reasons) |
| T1.1.3 | `_evaluate_signal(market_type="bucket_1h")` when `MAKER_EXCLUDED_MARKET_TYPES=[]` | Not blocked by market-type gate |
| T1.1.4 | All 4 market types (5m, 15m, 1h, milestone) pass when `MAKER_EXCLUDED_MARKET_TYPES=[]` | No TypeError raised |
| T1.1.5 | Exclusion list with multiple types: `["bucket_1h", "bucket_15m"]` | Both types return `None` |

**Reset pattern:** Always reset `config.MAKER_EXCLUDED_MARKET_TYPES = []` in teardown.

---

### T1.2 `tests/test_api_server.py` — `/performance` by_market_type

| ID | Test | Expected |
|---|---|---|
| T1.2.1 | `GET /performance` response contains `"by_market_type"` key | `True` |
| T1.2.2 | `by_market_type["bucket_5m"]["pnl"]` aggregates all bucket_5m trades correctly | Sum matches expected value |
| T1.2.3 | `by_market_type["bucket_5m"]["count"]` equals number of bucket_5m rows | Integer match |
| T1.2.4 | `by_market_type["bucket_5m"]["win_rate"]` in [0.0, 1.0] | Float in valid range |
| T1.2.5 | Trades with `market_type=""` or missing key aggregate under `"unknown"` | `"unknown"` key present |
| T1.2.6 | `GET /performance?period=7d` — by_market_type respects period filter | Filtered rows only |
| T1.2.7 | Existing keys `by_strategy`, `by_underlying` still present after change | `True` |

---

### T1.3 `tests/test_api_server.py` — `/config` maker_excluded_market_types

| ID | Test | Expected |
|---|---|---|
| T1.3.1 | `POST /config {"maker_excluded_market_types": ["bucket_1h"]}` | `config.MAKER_EXCLUDED_MARKET_TYPES == ["bucket_1h"]` |
| T1.3.2 | `POST /config {"maker_excluded_market_types": []}` | `config.MAKER_EXCLUDED_MARKET_TYPES == []` |
| T1.3.3 | `GET /config` response contains `"maker_excluded_market_types"` key | `True` |
| T1.3.4 | `GET /config` after patch returns updated value | Round-trip consistent |
| T1.3.5 | `POST /config {"maker_excluded_market_types": ["unknown_type"]}` | Accepted (list allows arbitrary strings) |

---

### T1.4 `tests/test_api_server.py` — `/health` adverse detection fields

| ID | Test | Expected |
|---|---|---|
| T1.4.1 | `GET /health` contains `"adverse_triggers_session"` | `True` |
| T1.4.2 | `GET /health` contains `"adverse_threshold_pct"` | `True` |
| T1.4.3 | `GET /health` contains `"hl_max_move_pct_session"` | `True` |
| T1.4.4 | `adverse_threshold_pct` equals `config.PAPER_ADVERSE_SELECTION_PCT` | `True` |
| T1.4.5 | `adverse_triggers_session` is `int >= 0` | `True` |

---

### T1.5 Regression tests — must not break

| ID | Test file | Test | Must still |
|---|---|---|---|
| T1.5.1 | `test_maker.py` | All existing `_evaluate_signal` tests | Pass |
| T1.5.2 | `test_api_server.py` | `test_by_strategy_breakdown` | Pass |
| T1.5.3 | `test_api_server.py` | `test_filter_by_strategy` | Pass |
| T1.5.4 | `test_api_server.py` | `test_filter_by_underlying` | Pass |
| T1.5.5 | `test_fill_simulator.py` | All adverse detection tests at threshold 0.001 | Pass |
| T1.5.6 | All test files | Full suite: 598 passing, 5 skipped | Pass |

---

## T2 — Integration Tests (manual)

### T2.1 Config round-trip

```bash
# Start API server
python api_server.py &

# Verify initial state
curl -s http://localhost:8080/config | python -m json.tool | grep excluded
# Expected: "maker_excluded_market_types": []

# Patch to exclude bucket_1h
curl -s -X POST http://localhost:8080/config \
  -H "Content-Type: application/json" \
  -d '{"maker_excluded_market_types": ["bucket_1h"]}' \
  | python -m json.tool | grep -A3 excluded
# Expected: "maker_excluded_market_types": ["bucket_1h"] in both "updated" and "current"

# Verify it persisted in config_overrides.json
cat config_overrides.json | python -m json.tool | grep excluded
# Expected: "MAKER_EXCLUDED_MARKET_TYPES": ["bucket_1h"]

# Clear it
curl -s -X POST http://localhost:8080/config \
  -H "Content-Type: application/json" \
  -d '{"maker_excluded_market_types": []}' \
  | python -m json.tool | grep excluded
# Expected: "maker_excluded_market_types": []
```

---

### T2.2 Performance endpoint by_market_type

```bash
curl -s http://localhost:8080/performance | python -m json.tool | python -c "
import json, sys
data = json.load(sys.stdin)
bmt = data.get('by_market_type', {})
print('Keys:', sorted(bmt.keys()))
for k, v in sorted(bmt.items()):
    print(f'  {k}: pnl={v[\"pnl\"]:.2f}, count={v[\"count\"]}, win_rate={v[\"win_rate\"]:.2%}')
"
# Expected output includes bucket_5m, bucket_15m, bucket_1h entries
# Expected: bucket_5m pnl ≈ +33.14 (from session data)
```

---

### T2.3 Hedge threshold verification

```bash
# Verify current hedge threshold
curl -s http://localhost:8080/config | python -m json.tool | grep hedge_threshold
# Expected: "hedge_threshold_usd": 50.0  (after P0-B change)

# Verify inventory endpoint
curl -s http://localhost:8080/maker/inventory | python -m json.tool
# Expected: "threshold_usd": 50.0
# Expected: "position_delta" shows per-coin net values
```

---

### T2.4 Health endpoint adverse detection

```bash
curl -s http://localhost:8080/health | python -m json.tool | python -c "
import json, sys
data = json.load(sys.stdin)
print('adverse_triggers_session:', data.get('adverse_triggers_session'))
print('adverse_threshold_pct:', data.get('adverse_threshold_pct'))
print('hl_max_move_pct_session:', data.get('hl_max_move_pct_session'))
"
# Expected: all three fields present, adverse_threshold_pct = 0.001
```

---

## T3 — Frontend Tests (manual in browser)

### T3.1 BucketPerformanceStrip

| ID | Check | Pass condition |
|---|---|---|
| T3.1.1 | Dashboard loads | BucketPerformanceStrip renders without console error |
| T3.1.2 | All 3 bucket chips visible | bucket_5m, bucket_15m, bucket_1h chips present |
| T3.1.3 | Positive PnL chip is green | `color: var(--color-positive)` applied |
| T3.1.4 | Negative PnL chip is red | `color: var(--color-negative)` applied |
| T3.1.5 | No trades yet | `—` shown in muted color, no crash |
| T3.1.6 | Disable button appears for bucket_1h (PnL < -$20) | Button rendered, labeled "Disable" |
| T3.1.7 | Click Disable → confirmation dialog | Dialog text includes "bucket_1h" and consequence |
| T3.1.8 | Confirm disable → chip shows [Disabled] | Chip opacity reduced, Re-enable button appears |
| T3.1.9 | Screen reader: chip aria-label present | `aria-label="bucket_5m performance: +$33.14, WORKING"` (inspect DOM) |

---

### T3.2 HedgeStatusCard / HealthCard hedge row

| ID | Check | Pass condition |
|---|---|---|
| T3.2.1 | Hedge status row visible in HealthCard | Row with label "Hedge status" present |
| T3.2.2 | Inactive state: StatusDot hollow | `○` glyph (unfilled) shown |
| T3.2.3 | Per-coin max exposure shown | "max net $23.10" text visible |
| T3.2.4 | Threshold shown | "threshold $50.00" text visible |
| T3.2.5 | "Lower threshold" shortcut appears when max_net > threshold × 0.5 | Button visible when applicable |
| T3.2.6 | Click threshold button → PATCH fires | Network tab shows `POST /config {"hedge_threshold_usd": 25}` |

---

### T3.3 AdverseStatusIndicator / HealthCard adverse row

| ID | Check | Pass condition |
|---|---|---|
| T3.3.1 | Adverse detection row visible in HealthCard | Row with label "Adverse detection" present |
| T3.3.2 | Normal state: green StatusDot | `●` in green, "0 triggers" text |
| T3.3.3 | Calibration warning: amber shown when `threshold > max_observed × 1.5` | Amber indicator, warning text |
| T3.3.4 | HL offline: muted state | "HL offline" text when `hl_ws_connected=false` |

---

### T3.4 Performance page by_market_type

| ID | Check | Pass condition |
|---|---|---|
| T3.4.1 | "By Market Type" section visible on Performance page | Heading + chart rendered |
| T3.4.2 | Bars render for each known bucket type | 3 bars (5m, 15m, 1h) visible |
| T3.4.3 | Bar color: positive green, negative red | Color coded |
| T3.4.4 | Win rate table below chart | Table with pnl, win_rate, count per type |
| T3.4.5 | Unknown/legacy trades table row | "unknown" row renders without crash |
| T3.4.6 | Period tabs filter applies | Switching period updates chart |

---

### T3.5 Settings page Market Types section

| ID | Check | Pass condition |
|---|---|---|
| T3.5.1 | Market Types section at top of Settings | Section visible above other settings |
| T3.5.2 | 3 toggles: bucket_5m, bucket_15m, bucket_1h | All 3 rendered |
| T3.5.3 | Initial state matches `/config` | Toggles reflect `maker_excluded_market_types` |
| T3.5.4 | Toggle off bucket_1h → PATCH fires | Network: `POST /config {"maker_excluded_market_types": ["bucket_1h"]}` |
| T3.5.5 | Toggle on bucket_1h → PATCH fires | Network: `POST /config {"maker_excluded_market_types": []}` |
| T3.5.6 | Toggle buttons meet 44px touch target | `height >= 44px` (computed style in DevTools) |

---

### T3.6 Accessibility

| ID | Check | Pass condition |
|---|---|---|
| T3.6.1 | StatusDot has role="img" and aria-label | Inspect DOM on HealthCard |
| T3.6.2 | Focus ring visible on all buttons | Tab through page — indigo outline appears |
| T3.6.3 | Fills table wrapped in table-scroll div | `overflow-x: auto` visible in DevTools |

---

## T4 — Acceptance Criteria

The SELECTIVE EXPANSION engineering changes are complete when:

1. `pytest tests/ -v` → **598+ passing, 0 failures**
2. `GET /performance` → response includes `"by_market_type"` with `bucket_5m`, `bucket_15m`, `bucket_1h` keys
3. `POST /config {"maker_excluded_market_types": ["bucket_1h"]}` → `_evaluate_signal()` returns `None` for bucket_1h markets on next scan cycle
4. `GET /config` → `"hedge_threshold_usd": 50.0`
5. `GET /health` → includes `adverse_triggers_session`, `adverse_threshold_pct`, `hl_max_move_pct_session`
6. Dashboard loads → BucketPerformanceStrip renders 3 chips with correct PnL colors
7. Dashboard → HealthCard includes hedge status and adverse detection rows
8. Performance page → "By Market Type" section with bar chart
9. Settings page → Market Types section at top with 3 toggles
10. All manual T3.x checks pass
