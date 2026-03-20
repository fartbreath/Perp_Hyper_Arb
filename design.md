# Design Review — Perp Hyper Arb Webapp
**Mode:** SELECTIVE EXPANSION (Option B)
**Date:** 2026-03-18
**Scope:** Webapp UI review + design spec for changes surfaced by CEO strategy review
**Branch:** N/A (no git repo)

---

## PRE-REVIEW SYSTEM AUDIT

### System State
- **Webapp:** React 18 + TypeScript, Vite, Recharts, React Router. 10 pages.
- **No DESIGN.md exists** — this document will serve as the founding spec.
- **No components/ directory** — all logic is inlined in page files. One shared CSS file (`index.css`).
- **Design system in practice:** Inter font · 14px base · `#0f172a` bg · `#1e293b` card · `#6366f1` accent · semantic green/red/orange.
- **Charts:** Recharts with custom dark styling.
- **No prior design review commits.**

### UI Scope of the SELECTIVE EXPANSION Plan
The following UI surfaces are affected by the strategy review findings:

| Finding | UI Surface Affected |
|---|---|
| bucket_5m profitable, bucket_1h bleeding | Dashboard (add bucket strip); Settings (bucket type toggles) |
| Hedge never fires ($200 threshold too high) | Dashboard (hedge status card); Risk page |
| Adverse detection never fires (threshold too high) | Fills page (adverse highlight); Dashboard health card |
| Signal score negatively correlated at Q4 | Performance page (score-vs-PnL chart) |
| YES fills systemically losing in bear conditions | Performance page (YES/NO side breakdown) |

### Existing Patterns to Preserve
- `StatusDot` / `GaugeBar` — reuse for hedge and adverse indicators
- `.card` + `.kv-table` — the standard info surface
- `.summary-row` + `.stat` — use for new bucket breakdown strip
- `.banner.info` — existing pattern for contextual alerts
- Period tabs (`7d / 30d / all`) — use for all time-sliced views
- `.filters` + `.period-tabs` — filter bar pattern for all list pages

### Retrospective (prior review cycles)
No prior review cycles. This is the first formal design review.

---

## Step 0: Design Scope Assessment

### 0A. Initial Design Rating: **5/10**

The webapp is clean and functional. It is **not AI slop** — it's an operator-grade dark dashboard with consistent patterns and real data. But it scores 5/10 because:

1. The most critical operational insight from the strategy review — *bucket_5m is making money, bucket_1h is destroying it* — is invisible on the Dashboard. It's buried three clicks away in Performance.
2. The hedge has never fired in 134 trades, but there's zero indication of this on any page. The system looks "healthy" even when a critical risk limb is inactive.
3. The Dashboard asks the operator to read 4 separate cards to answer one question: "Is the strategy working right now?"
4. Navigation has 10 flat items — no hierarchy between "I check this every 30 seconds" (Dashboard, Risk) and "I check this when investigating" (Logs, Fills).

**What a 10/10 looks like for this plan:**
The Dashboard answers within 5 seconds: bot running ✅, net PnL positive ✅, 5m bucket working ✅, hedge active ✅, no adverse fills ✅. Operator interventions (disable 1h, lower hedge threshold) are reachable from the Dashboard in one click, not buried in Settings.

### 0B. DESIGN.md Status
No `DESIGN.md` found. This document IS the founding DESIGN.md.
All new UI work from the SELECTIVE EXPANSION plan should calibrate against the tokens and patterns defined in **Section: Design System** below.

### 0C. Existing Design Leverage
The existing CSS has every primitive needed. No new component library required. The SELECTIVE EXPANSION UI additions can be built entirely from:
- `.stat` chips for bucket PnL display
- `.card` + `.kv-table` for hedge status
- `.banner.info` for first-time / actionable alerts
- `GaugeBar` for hedge inventory level
- `StatusDot` for adverse detection status (after fixing accessibility)

---

## Pass 1: Information Architecture

**Rating: 5/10**

### What's broken
The Dashboard has four cards stacked vertically with equal visual weight:
1. Bot control (start/stop)
2. System health (connections, heartbeat)
3. P&L summary (today/7d/all-time)
4. Open positions (table)

This hierarchy treats "PM WebSocket connected" with the same weight as "Am I making money?" A trader opening this page at 3 AM has one question: **is the strategy working and should I intervene?**

The bucket breakdown — the most strategically urgent output of the last 6 hours of live data — is on the Performance page, three clicks away, and has no surface area on Dashboard.

### Fix

**Revised Dashboard hierarchy:**
```
┌─────────────────────────────────────────────────────────────┐
│ TIER 1: STATUS STRIP (always visible, top of page)          │
│  Bot: ▶ Running  │  Mode: 📋 Paper  │  Uptime: 2h 14m       │
│  PM WS: ● │  HL WS: ●  │  Heartbeat: 3s ago                 │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ TIER 2: FINANCIAL SUMMARY (primary, 2-col grid)             │
│                                                             │
│  ┌─────────────────────┐  ┌─────────────────────────────┐  │
│  │ P&L Summary          │  │ Bucket Performance          │  │
│  │ Today:  +$33.14  ✅  │  │ 5m:  +$33.14  ▓▓▓▓▓   ✅  │  │
│  │ 7-Day:  −$8.37   ❌  │  │ 15m: −$7.75   ░░░     ⚠   │  │
│  │ All:    −$8.37   ❌  │  │ 1h:  −$34.38  ░░░░░░  ❌  │  │
│  │ Win Rate: 48%        │  │         [Disable 1h]        │  │
│  └─────────────────────┘  └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ TIER 3: RISK & HEDGE STATUS (secondary, 2-col grid)         │
│                                                             │
│  ┌─────────────────────┐  ┌─────────────────────────────┐  │
│  │ Hedge Status         │  │ Open Positions (3)          │  │
│  │ BTC net: −$5.54      │  │ [table]                     │  │
│  │ ETH net: −$23.10 ⚠  │  │                             │  │
│  │ Threshold: $200      │  │                             │  │
│  │ HL hedge: Inactive   │  │                             │  │
│  └─────────────────────┘  └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ TIER 4: ADVERSE DETECTION (contextual, show if active)      │
│   🟡 0 adverse fills this session. Threshold: 0.3% HL move  │
│   (Note: max HL move observed was 0.1% — threshold may be   │
│    set too high to ever trigger. Check Settings.)            │
└─────────────────────────────────────────────────────────────┘
```

**Nav hierarchy fix:**
Demote secondary pages visually. The nav currently treats all 10 items equally.

```
Primary (always visible, full opacity):
  Dashboard · Trades · Positions · Risk · Settings

Secondary (visible but muted, grouped with divider):
  Performance · Signals · Markets · Fills · Logs
```

Implementation: add a `nav-divider` class between groups. No hamburger needed at desktop.

**Rating after fix: 8/10**

---

## Pass 2: Interaction State Coverage

**Rating: 6/10**

### Existing states
| Feature | Loading | Empty | Error | Success | Partial |
|---|---|---|---|---|---|
| Bot status | Implied (launcher.data loading) | "Launcher offline — run: python launcher.py" | ✅ localError shown | ✅ Running/stopped | ✅ "Starting... API not yet ready" |
| Health card | ✅ skeleton | N/A | ✅ "API offline" | ✅ table | N/A |
| P&L summary | ✅ skeleton | Implicit (zeroes) | ✅ "P&L unavailable" | ✅ colored values | N/A |
| Open positions | ✅ skeleton | "No open positions" | ✅ "Positions unavailable" | ✅ table | N/A |
| Performance | ✅ skeleton | "No trade data yet." | ✅ "Failed to load" | ✅ charts | N/A |
| Fills | N/A (table) | No fill data message | ✅ error class | ✅ rows | ✅ pagination |

### Gaps

**Empty state: "No open positions"**
Currently: `<p className="muted">No open positions</p>`
This fires in two very different contexts: (1) bot just started, no fills yet, and (2) bot has been running but everything closed. These need different copy and different warmth.

```
Context: bot running, first session (0 sessions total)
  "No positions yet — the bot is scanning markets and will fill when it
   finds a competitive quote. Typical time to first fill: 2–5 minutes."
  [View Fills Log]

Context: bot running, positions closed normally
  "All positions closed. Last close: [timestamp]."
  [View Trades]
```

**Empty state: Bucket Performance card (new card)**
If all bucket types have zero trades (bot never ran): show "No data yet" with a link to check if the bot is running.
If one bucket type has zero trades: show "—" with muted color, not 0.

**Error state: "API offline"**
Currently: `<div className="card error">API offline — {error}</div>`
Too bare. Add a recovery action:
```
API offline — the backend server is unreachable.
Run: python api_server.py   or   python main.py
[Retry]
```

**Adverse detection state (new)**
Three states needed for the adverse detection indicator on Dashboard:
- **Never triggered:** muted amber — "0 adverse fills. HL move threshold may be too high (current: 0.3%). Observed max: 0.1%."
- **Active trigger:** red banner — "⚠ Adverse selection detected! HL moved 0.4% against position. Fill probability throttled."
- **No HL data:** gray — "HL connection offline — adverse detection paused."

**Hedge status state (new)**
Three states for the Hedge Status card on Dashboard:
- **Inactive (below threshold):** muted indicator — "No hedge needed. Max per-coin net: $23.10 (threshold: $200)."
- **Active hedge:** green — "Hedge active. ETH short −0.012 on HL. Delta-neutral."
- **Hedge error:** red — "Hedge failed. HL order rejected. Check Logs."

**Rating after fix: 9/10**

---

## Pass 3: User Journey & Emotional Arc

**Rating: 4/10**

### Storyboard: Primary user loop ("Is it working?")

```
STEP | USER DOES                     | USER FEELS         | PLAN SPECIFIES?
-----|-------------------------------|--------------------|-----------------
1    | Opens webapp tab              | Anxious / curious  | ❌ 4 cards, equal weight
2    | Wants to know: is it working? | Scanning quickly   | ❌ No primary signal
3    | Reads P&L card (+$1.41)       | Relieved but meh   | ✅ Shows PnL
4    | Wonders: which bucket?        | Still uncertain    | ❌ Not on Dashboard
5    | Clicks Performance            | Slight friction    | ❌ Should be instant
6    | Sees 5m +$33, 1h −$30         | Alarmed by 1h loss | ❌ No action affordance
7    | Wants to disable 1h markets   | Frustrated         | ❌ Must navigate to Settings
8    | Finds MAKER_EXIT_HOURS toggle | Confused (wrong)   | ❌ No bucket type toggle
```

The journey breaks at step 4. The operator has to visit Performance to understand what's happening, and then navigate to Settings to act on it. This is three screens for what should be one.

### Fix: "The 5-second answer"
The Dashboard redesign from Pass 1 addresses steps 1–5. But two additional journey fixes:

**Step 6+: "One-click intervention from Dashboard"**
The Bucket Performance card should include a `[Disable 1h]` action that PATCH-es the config inline. Confirm dialog: "Disable bucket_1h markets? The bot will stop quoting hourly buckets and close any open 1h positions within 6 hours."

**Settings page: Bucket type section**
Add a dedicated "Market Types" section at the top of Settings (above all other parameters):
```
Market Types
─────────────
bucket_5m    [ON]   5-minute BTC/ETH/SOL/XRP buckets — WORKING
bucket_15m   [ON]   15-minute buckets — marginal
bucket_1h    [ON]   1-hour buckets — ⚠ unprofitable this session
```
Include a one-line PnL hint next to each toggle (pulled from the same performance data already fetched).

**5-year reflective design:**
The operator who uses this system every day needs to trust it. Trust erodes when the system looks healthy but is silently losing on one market type. The bucket performance strip earns that trust by making the strategy's behavior legible at a glance — even when it's bad news.

**Rating after fix: 8/10**

---

## Pass 4: AI Slop Risk

**Rating: 8/10 — minimal risk**

This dashboard is NOT slop. It is functional, operator-grade, and intentionally austere. It does not have:
- Hero sections
- Marketing copy
- Three-column feature grids
- Gradient blobs
- "AI-generated looking" card layouts

**What does qualify as slop risk (minor):**

1. **Chart tooltips use `$${Number(v).toFixed(2)}`** — the raw format string is visible if the API returns non-numeric data. Add a `safeNum(v)` guard.

2. **`Perp Hyper Arb` brand name** is a placeholder. Not a sloppy design, but it's a code name. The navbar brand (`nav-brand`) is styled with the indigo accent — good foundation for a real name later.

3. **The equity curve chart (Performance page) shows no annotation** when the strategy starts losing. Compare: a simple `<ReferenceLine y={0}>` already exists — but there's no annotation for when bucket_1h was enabled vs disabled, or when market type was changed. Without this annotation, the chart is a line that goes down and the operator can't reason about it.

**Fix for #3 only (the others are deferred):**
Add event annotations to the equity curve as vertical reference lines. Events: "1h disabled", "hedge threshold changed", "strategy restarted". Sourced from the same API (can be a simple array of `{t, label}` from the `/performance` endpoint).

**Rating after fix: 8/10 — no further action needed**

---

## Pass 5: Design System Alignment

**Rating: 4/10** (no DESIGN.md → by definition unaligned)

### The real problem: drift risk, not current inconsistency
The current webapp is internally consistent. The CSS is clean and all classes align to a coherent visual language. The risk is that new UI additions (bucket performance strip, hedge status card, adverse detection indicator) will each be built ad-hoc without a spec, leading to subtle inconsistencies in spacing, color use, and type scale.

### Token specification (founding DESIGN.md content)

The following tokens are extracted from the existing `index.css` and are the canonical design system:

#### Color tokens
```
Background layers:
  --bg-base:     #0f172a   (body, log viewer background)
  --bg-surface:  #1e293b   (cards, nav, inputs)
  --bg-overlay:  #334155   (borders, hover states, dividers)

Text:
  --text-primary:   #e2e8f0
  --text-secondary: #94a3b8
  --text-muted:     #64748b

Accent:
  --accent:       #6366f1   (primary action, active nav, focus outline)
  --accent-hover: #818cf8   (nav brand, hover states)

Semantic:
  --color-positive: #22c55e   (profit, connected, success)
  --color-negative: #ef4444   (loss, error, danger states)
  --color-warning:  #f97316   (fees, stale data, caution)
  --color-alert:    #f59e0b   (amber — degraded state, not yet failed)

Info:
  --info-bg:     #1e3a5f
  --info-border: #3b82f6
  --info-text:   #93c5fd
```

#### Spacing scale
```
4px   (micro gap — within stat labels)
8px   (small gap — within cards)
12px  (medium — card padding tight)
16px  (base — between sibling cards in grid)
20px  (large — page padding sides)
24px  (page section gap)
```

#### Type scale (from rendered CSS)
```
11px  stat-label, signal-time, tag
12px  small data, muted, log entries
13px  tables, filters, buttons
14px  body (base)
15px  card h3 (0.95rem)
20px  page h2 (1.4rem)
```

#### Component inventory (documented for reuse)
```
.card              — primary content surface
.kv-table          — label:value pairs
.data-table        — sortable data grids
.summary-row       — stat chip strip
.stat              — individual stat chip
.gauge-row/.gauge-track/.gauge-fill  — progress bar
.banner.info       — contextual info strip
.filters           — filter bar (selects + labels)
.period-tabs       — time period selector
.pagination        — prev/next rows
.skeleton          — loading placeholder (pulse animation)
.error             — inline error state
.muted             — secondary text
.mono              — monospace (log data)
.tag               — small label pill
.toggle-btn        — ON/OFF toggle (settings)
.settings-row      — settings form row
```

### New components needed for SELECTIVE EXPANSION

These don't exist yet and must be specified:

**`BucketPerformanceStrip`**
```
Visual: Row of 3 .stat chips (5m / 15m / 1h), each with:
  - Label: bucket type
  - Value: cumulative PnL ($+33.14 / −$7.75 / −$34.38)
  - Color: --color-positive if PnL > 0, else --color-negative
  - Hint: one-line status ("WORKING" / "MARGINAL" / "LOSING — ⚠ Disable?")
  - Action: inline [Disable] button if PnL < −$20 in session
Placement: Inside PnL card on Dashboard, below the existing 3×2 PnL grid
or as a standalone card in the Tier 2 grid.
```

**`HedgeStatusCard`**
```
Visual: .card with .kv-table showing per-coin net + threshold
  - Rows: BTC / ETH / SOL / XRP net inventory
  - Threshold row: "Hedge fires at > $200 net" (editable shortcut)
  - Status indicator: StatusDot(ok=false) "Inactive — all below threshold"
  - If hedge active: green fill + "Hedged · ETH short 0.012 HL"
  - If threshold issue: amber warning "Max net $23.10 — threshold $200".
    Inline [Lower to $25] shortcut button → PATCH /config HEDGE_THRESHOLD_USD=25
Edge cases:
  - HL offline: "HL not connected — hedge disabled"
  - Zero net positions: "—" (no coins with open positions)
```

**`AdverseStatusIndicator`**
```
Visual: Single row in HealthCard (not a new card — extends existing)
  Label: "Adverse detection"
  Value on normal: StatusDot(ok=true) "0 triggers · threshold 0.3%"
  Value on anomaly: Amber dot "Threshold 0.3% > max observed 0.1% — may never fire"
  Value on trigger: Red StatusDot "⚠ Triggered · 2 adverse fills this session"
Data source: Add to /health API response:
  adverse_triggers_session: int
  adverse_threshold_pct: float
  hl_max_move_pct_session: float
```

**Rating after fix: 8/10**

---

## Pass 6: Responsive & Accessibility

**Rating: 3/10**

### Responsive: what breaks

**Nav overflow (mobile — critical)**
```css
/* Current */
.nav-links { display: flex; gap: 0.25rem; list-style: none; }
```
10 horizontal nav items at 14px with padding will overflow any 375px viewport. The nav has no fallback.

**Fix:**
```css
@media (max-width: 900px) {
  .nav-links { display: none; }          /* hide all nav items */
  .nav-hamburger { display: block; }     /* show hamburger icon */
}
/* Mobile nav: full-screen overlay with the same 10 items in 2 columns */
```

For a trading bot webapp, mobile usage is likely limited to monitoring. The minimal fix is hiding the nav on mobile and showing a hamburger that opens an overlay. This is not a zero-effort change but it prevents the UI from being broken on any screen below 900px wide.

**Tables at mobile (moderate)**
`.data-table` has no mobile breakpoint. 10+ column tables (Fills page) will require horizontal scroll. The fix is to add `overflow-x: auto` to a wrapping `.table-scroll` div — a 2-line CSS change.

**`pnl-grid` at mobile (minor)**
```css
.pnl-grid { display: grid; grid-template-columns: repeat(3, 1fr); }
```
3 columns at 375px will squeeze to ~120px per cell. Fix: Add `@media (max-width: 768px) { .pnl-grid { grid-template-columns: repeat(2, 1fr); } }`

### Accessibility: what breaks

**StatusDot — screen reader failure (critical)**
```tsx
// Current — both states render identical text
<span style={{ color: ok ? "#22c55e" : "#ef4444" }}>●</span>
```
A screen reader reads "bullet bullet bullet bullet" for the health card. No semantic difference communicated.

**Fix:**
```tsx
function StatusDot({ ok, label }: { ok: boolean; label?: string }) {
  return (
    <span
      role="img"
      aria-label={label ? `${label}: ${ok ? "OK" : "error"}` : ok ? "OK" : "error"}
      style={{ color: ok ? "#22c55e" : "#ef4444" }}
    >
      {ok ? "●" : "○"}   {/* distinct glyphs — filled vs hollow */}
    </span>
  );
}
```

**Color-only status communication (moderate)**
The `BotControlCard` uses green/red text color for status but the text itself ("Running", "Bot stopped") is readable without color. OK — but the new `BucketPerformanceStrip` must NOT rely on color alone. Add a text indicator alongside the color:

```
5m:  +$33.14  ✅ Working
1h:  −$34.38  ❌ Losing
```

**Keyboard navigation: buttons (minor)**
All `<button>` elements use inline `style` but CSS focus outline is overridden by the `*` reset (`margin: 0; padding: 0`). The `.settings-input:focus` rule adds a focus ring, but no equivalent exists for `.toggle-btn` or the Start/Stop button.

**Fix** (add to `index.css`):
```css
button:focus-visible { outline: 2px solid #6366f1; outline-offset: 2px; }
```

**Touch target sizes (moderate)**
The Start/Stop button has `padding: 8px 20px` — approximately 36px tall at 14px font, below the 44px minimum. The toggle buttons in Settings have `padding: 0.35rem 1.1rem` — ~22px tall. These need `min-height: 44px` to meet WCAG 2.5.5.

**Fix** (add to `index.css`):
```css
.toggle-btn  { min-height: 44px; }
button       { min-height: 36px; } /* soft minimum — relax for nav items */
```

**ARIA nav landmark (minor)**
The `<nav>` element exists (correct), but the main content `<main className="main-content">` doesn't have `role="main"` or an `aria-label`. React Router skips focus on navigation. A skip link would be ideal:
```html
<a href="#main-content" className="skip-link">Skip to main content</a>
```

**Rating after fix: 7/10** (mobile nav is a medium effort — the rest are all small)

---

## Pass 7: Unresolved Design Decisions

These are the genuine choices that need answers before implementing the SELECTIVE EXPANSION plan. Each will spawn a code change — the decision shape matters.

### Decision 1: Where does bucket performance live on Dashboard?

Two options:

**Option A — Inline inside the P&L card** (lower friction)
Adds a row of chips below the existing `pnl-grid`. Risk: makes the P&L card tall. Good if bucket performance is always relevant.
*(effort: S — 1h human / 5min CC)*

**Option B — Separate card in Tier 2 grid** (cleaner hierarchy)
PnL card and Bucket Performance card sit side by side in `grid-2`. Risk: adds visual density at mobile. Better if bucket performance is the *second question* not the *first question*.
*(effort: S — 1h human / 5min CC)*

**Recommendation: B** — keeping P&L summary focused (today/week/alltime) and bucket performance focused (which type is working) is a cleaner separation of concerns. The `grid-2` layout already handles the responsive collapse.

---

### Decision 2: Where does the hedge status live?

Three options:

**Option A — As a new Tier 3 card on Dashboard**
Most visible. Risk: Dashboard gets long on mobile.
*(effort: S)*

**Option B — Add a "Hedge" row to the existing Risk page gauges**
Less prominent but operationally logical — Risk page is where you go for exposure data. Risk: operators may not check Risk page unless alerted.
*(effort: XS)*

**Option C — Add to System Health card on Dashboard (new rows in kv-table)**
Doesn't add a whole new card — just extends the existing Health card with: "Hedge: Inactive (max net $23/threshold $200)". Lowest footprint.
*(effort: XS)*

**Recommendation: C** — The hedge status is a health indicator, not a financial one. It belongs in the Health card alongside WS connections and heartbeat. Reserve a full card for when the hedge IS active and needs monitoring attention (a conditional full-card expansion).

---

### Decision 3: Should the Dashboard include a "Disable 1h markets" shortcut?

The bucket performance strip surfacing a `[Disable]` button creates an in-context action that can:
- Patch `MAKER_BUCKET_TYPES` config (if such a parameter exists or is added)
- Or patch a per-type `STRATEGY_MAKER_BUCKET_1H_ENABLED` flag

**The config-side question:** does a `MAKER_BUCKET_TYPES` parameter exist?

Looking at `MAKER_STRATEGY.md` Appendix A, there is no `MAKER_BUCKET_TYPES` config key. The bot currently selects markets by querying Polymarket's API for all open bucket markets and quotes them all. To disable 1h, the config would need a new parameter: `MAKER_EXCLUDED_MARKET_TYPES` (a list).

This means the UI decision depends on a backend change. The design decision is:

**Option A — Add the UI shortcut AND the backend config at the same time**
The shortcut button is visible and wired. Clicking it creates a PATCH to `/config` with `MAKER_EXCLUDED_MARKET_TYPES=["bucket_1h"]`.
*(effort: M — 3h human / 15min CC for backend + frontend)*

**Option B — Add the UI shortcut as a direct Settings page nav shortcut**
The Bucket Performance card shows the numbers + a "Manage in Settings →" link that scrolls to the new Market Types section in Settings. No inline action — just surfacing the gap.
*(effort: XS — 30min human / 2min CC)*

**Recommendation: A** — The SELECTIVE EXPANSION plan already recommends adding the bucket type toggle. Doing both the config and UI at the same time is a lake, not an ocean.

---

### Decision 4: Adverse detection — show a warning even when it never fires?

The data shows `PAPER_ADVERSE_SELECTION_PCT = 0.003 (0.3%)` but the max observed HL move is `0.001 (0.1%)`. The adverse detector will never fire under these conditions.

Options:

**Option A — Show a calibration warning in the Health card**
"⚠ Adverse threshold (0.3%) exceeds observed HL moves (max 0.1%). May never fire. Consider lowering to 0.05%."

**Option B — Add a red dot in Settings next to the PAPER_ADVERSE_SELECTION_PCT field**
Contextual warning only visible in Settings. Quieter but less likely to be noticed.

**Option C — No UI change — fix the config value**
The threshold IS a bug, not a design concern. Just lower `PAPER_ADVERSE_SELECTION_PCT` to `0.0005` and the UI doesn't need to change.

**Recommendation: C** — This is a config bug, not a UI design decision. The threshold should be lowered as part of the SELECTIVE EXPANSION fixes. A UI warning for a config bug is adding complexity to hide a fixable problem.

---

## "NOT in scope" — Deferred Design Decisions

| Decision | Why deferred |
|---|---|
| Mobile hamburger nav | No mobile use case documented — defer until confirmed need |
| Brand name / logo | Placeholder; not blocking any strategy work |
| Performance page event annotations on equity curve | Backend support needed (event log API) |
| DESIGN.md branding section (typography specimens, palette swatches) | This doc IS the DESIGN.md for now; branding is not a blocker |
| `Signals` page redesign | Signals page is informational; no strategy-critical changes needed |
| `Markets` page improvement | Not identified as a pain point in strategy review |
| Dark mode toggle (it's always dark) | Only one mode; toggle would be feature bloat |
| RTL language support | Not applicable for a personal trading tool |

---

## "What already exists" — Patterns to reuse unchanged

| Pattern | File | Use for |
|---|---|---|
| `StatusDot` | `Dashboard.tsx` | Hedge status, adverse status — after a11y fix |
| `.summary-row` + `.stat` | `index.css` | Bucket performance strip |
| `GaugeBar` | `Risk.tsx` | Hedge inventory bar vs threshold |
| `.banner.info` | `index.css` | Per-session calibration warnings |
| `.kv-table` | `index.css` | Hedge status rows in Health card |
| Period tabs | `Performance.tsx` | Any time-sliced bucket performance view |
| Same error/loading pattern | All pages | New cards use identical guard pattern |

---

## TODOS (items surfaced by this review)

### TODO 1 — Add bucket performance strip to Dashboard
**What:** Show bucket_5m / bucket_15m / bucket_1h cumulative PnL in a `.stat` chip row on the Dashboard, inside or adjacent to the existing PnL card.
**Why:** The most critical operational insight (1h is losing, 5m is winning) is invisible on the primary screen.
**Pros:** Immediate visibility; uses existing `.summary-row` / `.stat` primitives; no new API endpoint needed (performance endpoint already returns `by_type` breakdown).
**Cons:** Adds a row to the already-itemized P&L card. Manage by putting it in a separate card.
**Context:** Sourced from `/performance` endpoint's `by_strategy` or a new `by_market_type` key.
**Depends on:** Performance API must return per-bucket-type PnL.

---

### TODO 2 — Add hedge status to System Health card (Dashboard)
**What:** Add 2 rows to the HealthCard kv-table: "Hedge status" (Inactive / Active) and "Max net / threshold" (e.g. $23 / $200).
**Why:** The hedge has not fired once in the session. Operators have no visibility into WHY without checking Risk page.
**Pros:** Zero-footprint addition. Uses existing `.kv-table` row pattern.
**Cons:** Makes the Health card slightly longer on first render.
**Context:** Data comes from the risk API (`/risk` endpoint `hl_notional_usd`, plus a new field: `hedge_active: bool`, `max_per_coin_net_usd: float`).
**Depends on:** Risk API needs two new fields.

---

### TODO 3 — Add per-coin net to Risk page with hedge threshold bar
**What:** Add a per-coin gauge section to the Risk page showing BTC/ETH/SOL/XRP net inventory vs the $200 (or configured) hedge threshold.
**Why:** The current Risk page shows aggregate HL notional but not the per-coin net that drives hedge decisions.
**Pros:** Completes the risk picture; uses existing `GaugeBar` primitive.
**Cons:** Adds 4 rows (one per coin) to the Risk page.
**Context:** The hedge threshold is `HEDGE_THRESHOLD_USD` in config. Per-coin net is computed inside `maker.py` inventory tracking.
**Depends on:** Risk API must expose per-coin net inventory.

---

### TODO 4 — Add Market Types section to Settings page
**What:** New section at top of Settings: "Market Types" with ON/OFF toggles for `bucket_5m`, `bucket_15m`, `bucket_1h`.
**Why:** No current way to disable a losing bucket type without editing config files. Decision 3 above resolves to doing both UI + backend.
**Pros:** Operators can respond to strategy signals in one click. Shows PnL hint next to each toggle.
**Cons:** Requires new backend config parameter `MAKER_EXCLUDED_MARKET_TYPES` (list) — backend change needed first.
**Context:** Current Settings page uses `Toggle` and `NumberInput` primitives — identical pattern for these new rows.
**Depends on:** Backend config needs `MAKER_EXCLUDED_MARKET_TYPES: list[str]`.

---

### TODO 5 — Fix StatusDot accessibility
**What:** Add `role="img"` and `aria-label` to `StatusDot`. Use distinct glyphs (● vs ○) in addition to color.
**Why:** Screen readers announce the same character for both states. This is a WCAG 1.4.1 (use of color) violation.
**Pros:** Takes ~5 minutes. Fixes all downstream uses (HealthCard, hedge status, adverse status).
**Cons:** None.
**Context:** `StatusDot` is defined in `Dashboard.tsx`. Any component that calls it gets the fix automatically.
**Depends on:** Nothing.

---

### TODO 6 — Add `button:focus-visible` and `min-height: 44px` to toggle buttons
**What:** Add focus ring CSS for all interactive elements, and ensure touch targets meet 44px minimum.
**Why:** Currently `.toggle-btn` has no focus ring (overridden by the `*` reset). Keyboard navigation is broken for settings controls.
**Pros:** 3 CSS lines. Fixes keyboard and mobile touch simultaneously.
**Cons:** Slightly taller buttons on settings page.
**Context:** Add to `index.css` near the `.toggle-btn` rules.
**Depends on:** Nothing.

---

### TODO 7 — Add `overflow-x: auto` wrapper to Fills and Trades tables
**What:** Wrap `.data-table` in a scrollable container at mobile breakpoint.
**Why:** Fills page has 12+ columns. At 375px it renders broken (columns overlap or hidden).
**Pros:** 2 CSS lines. Standard fix.
**Cons:** Horizontal scroll is not ideal UX but it's correct and expected for dense data tables.
**Context:** Add `.table-scroll { overflow-x: auto }` to `index.css`. Wrap `<table>` in `<div className="table-scroll">` in `Fills.tsx` and `Trades.tsx`.
**Depends on:** Nothing.

---

## Completion Summary

```
+========================================================================+
|              DESIGN REVIEW — COMPLETION SUMMARY                        |
+========================================================================+
| System Audit       | No DESIGN.md (founded here). 10 pages. No git.   |
| Initial Rating     | 5/10 — clean UI, wrong hierarchy for strategy    |
+========================================================================+
| Pass 1: IA         | 5→8/10  | Add bucket strip + nav hierarchy       |
| Pass 2: States     | 6→9/10  | Warm empty states, hedge/adverse states |
| Pass 3: Journey    | 4→8/10  | 5-second answer; one-click intervention|
| Pass 4: AI Slop    | 8/10    | No action — functional, not sloppy     |
| Pass 5: Design Sys | 4→8/10  | Token spec written; 3 new components   |
| Pass 6: A11y       | 3→7/10  | StatusDot fix; focus rings; tables     |
| Pass 7: Decisions  | —       | 4 decisions made; 3 deferred           |
+========================================================================+
| TODOs surfaced     | 7 actionable items                               |
| NOT in scope       | Mobile nav, branding, event annotations, RTL    |
+========================================================================+
| Key insight        | The UI is not the problem. The hierarchy is.     |
|                    | Bucket performance must surface on Dashboard.    |
|                    | The hedge gap must be visible in Health card.    |
+========================================================================+
```

### Priority order for implementation

1. **TODO 5** — StatusDot a11y fix (10 minutes, unblocks all status indicators)
2. **TODO 1** — Bucket performance strip on Dashboard (30 minutes, highest strategic value)
3. **TODO 2** — Hedge status in Health card (20 minutes, closes the most alarming blind spot)
4. **TODO 4** — Market Types in Settings (requires backend config PR first)
5. **TODO 3** — Per-coin net on Risk page (completes the hedge picture)
6. **TODO 6** — Focus ring + touch target CSS (10 minutes, free a11y win)
7. **TODO 7** — Table scroll wrapper (10 minutes, fixes mobile)

All 7 TODOs combined: **human team ~1 day / CC+gstack ~45 minutes**
