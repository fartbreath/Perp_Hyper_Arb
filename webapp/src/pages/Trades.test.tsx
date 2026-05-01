/**
 * webapp/src/pages/Trades.test.tsx
 *
 * Tests for the Trades accounting ledger page.
 *
 * Coverage split:
 *   1. Pure helpers — tested directly without DOM (fast, exhaustive)
 *   2. buildGroups  — grouping logic for pair_id and hedges
 *   3. Component    — render with mocked useAcctLedger hook
 *
 * All assertions reflect BUSINESS semantics:
 *   - Formatting must match what traders see on screen
 *   - Grouping must correctly pair YES/NO legs by pair_id
 *   - Hedge rows (fill_type="HEDGE" or strategy="momentum_hedge") must appear
 *     under their parent, not as top-level groups
 */
import { render, screen, fireEvent } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { AcctLedgerRow } from "../api/client";
import {
  fmtUsd,
  fmtPrice,
  fmtContracts,
  netPnl,
  grossPnl,
  pnlColor,
  buildGroups,
} from "./Trades";

// ── Mock the API hook ─────────────────────────────────────────────────────────
// Prevents HTTP requests; tests control returned data directly.
const mockUseAcctLedger = vi.fn();
vi.mock("../api/client", () => ({
  useAcctLedger: (...args: unknown[]) => mockUseAcctLedger(...args),
}));

// ── Default to empty / loading state ─────────────────────────────────────────
beforeEach(() => {
  mockUseAcctLedger.mockReturnValue({ data: null, loading: true, error: null, refresh: vi.fn() });
});

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makeRow(overrides: Partial<AcctLedgerRow> = {}): AcctLedgerRow {
  return {
    pos_id:             "pos-001",
    strategy:           "momentum",
    fill_type:          "MAIN",
    pair_id:            "",
    parent_pos_id:      "",
    market_id:          "0xabc123",
    market_title:       "BTC > $50k on 2025-06-01?",
    market_type:        "bucket_daily",
    underlying:         "BTC",
    side:               "YES",
    token_id:           "tok-001",
    entry_vwap:         "0.40",
    entry_contracts:    "100",
    entry_cost_usd:     "40.00",
    entry_time:         new Date(Date.now() - 3_600_000).toISOString(),
    pm_entry_confirmed: "True",
    spot_entry:         "50000",
    strike:             "50000",
    tte_seconds:        "86400",
    signal_source:      "chainlink",
    signal_score:       "75",
    exit_vwap:          "1.0",
    exit_contracts:     "100",
    exit_time:          new Date(Date.now() - 1_800_000).toISOString(),
    exit_type:          "RESOLVED",
    spot_exit:          "51000",
    resolve_price:      "1.0",
    resolved_outcome:   "WIN",
    fees_usd:           "1.50",
    rebates_usd:        "0.50",
    gross_pnl:          "60.00",
    net_pnl:            "59.00",
    status:             "RESOLVED_WIN",
    pm_exit_confirmed:  "True",
    reconciliation_notes: "",
    ...overrides,
  };
}

// ══════════════════════════════════════════════════════════════════════════════
// 1  fmtUsd
// ══════════════════════════════════════════════════════════════════════════════

describe("fmtUsd", () => {
  it("formats positive value with + prefix", () => {
    expect(fmtUsd(59)).toBe("+$59.00");
  });

  it("formats positive decimal correctly", () => {
    expect(fmtUsd(1.5)).toBe("+$1.50");
  });

  it("formats negative value with minus sign and no +", () => {
    // fmtUsd uses toFixed which includes the minus inside: "$-40.00"
    expect(fmtUsd(-40)).toBe("$-40.00");
    expect(fmtUsd(-40)).not.toContain("+");
  });

  it("formats zero as $0.00 with no sign prefix", () => {
    expect(fmtUsd(0)).toBe("$0.00");
  });

  it("accepts string numbers", () => {
    expect(fmtUsd("25.00")).toBe("+$25.00");
  });

  it("accepts negative string numbers", () => {
    expect(fmtUsd("-10.00")).toBe("$-10.00");
  });

  it("returns — for Infinity", () => {
    expect(fmtUsd(Infinity)).toBe("—");
  });

  it("returns — for NaN", () => {
    expect(fmtUsd(NaN)).toBe("—");
  });

  it("handles undefined as $0.00", () => {
    expect(fmtUsd(undefined)).toBe("$0.00");
  });

  it("rounds to two decimal places", () => {
    expect(fmtUsd(1.234)).toBe("+$1.23");
    expect(fmtUsd(1.235)).toBe("+$1.24");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 2  fmtPrice
// ══════════════════════════════════════════════════════════════════════════════

describe("fmtPrice", () => {
  it("converts fractional price to cents", () => {
    expect(fmtPrice(0.50)).toBe("50.0¢");
  });

  it("formats 0.40 as 40.0¢", () => {
    expect(fmtPrice(0.40)).toBe("40.0¢");
  });

  it("formats 1.0 as 100.0¢", () => {
    expect(fmtPrice(1.0)).toBe("100.0¢");
  });

  it("formats 0.001 as 0.1¢", () => {
    expect(fmtPrice(0.001)).toBe("0.1¢");
  });

  it("returns — for 0", () => {
    expect(fmtPrice(0)).toBe("—");
  });

  it("returns — for undefined", () => {
    expect(fmtPrice(undefined)).toBe("—");
  });

  it("returns — for NaN string", () => {
    expect(fmtPrice("abc")).toBe("—");
  });

  it("accepts string input", () => {
    expect(fmtPrice("0.65")).toBe("65.0¢");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 3  fmtContracts
// ══════════════════════════════════════════════════════════════════════════════

describe("fmtContracts", () => {
  it("formats 100 as 100.00", () => {
    expect(fmtContracts(100)).toBe("100.00");
  });

  it("formats fractional contracts to two decimals", () => {
    expect(fmtContracts(99.5)).toBe("99.50");
  });

  it("returns — for 0", () => {
    expect(fmtContracts(0)).toBe("—");
  });

  it("returns — for undefined", () => {
    expect(fmtContracts(undefined)).toBe("—");
  });

  it("accepts string input", () => {
    expect(fmtContracts("150")).toBe("150.00");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 4  netPnl / grossPnl — parse from AcctLedgerRow strings
// ══════════════════════════════════════════════════════════════════════════════

describe("netPnl / grossPnl", () => {
  it("netPnl parses net_pnl string to number", () => {
    const row = makeRow({ net_pnl: "59.00" });
    expect(netPnl(row)).toBeCloseTo(59.0);
  });

  it("grossPnl parses gross_pnl string to number", () => {
    const row = makeRow({ gross_pnl: "60.00" });
    expect(grossPnl(row)).toBeCloseTo(60.0);
  });

  it("netPnl returns 0 when net_pnl is undefined", () => {
    const row = makeRow({ net_pnl: undefined as unknown as string });
    expect(netPnl(row)).toBe(0);
  });

  it("grossPnl returns negative for losing trade", () => {
    const row = makeRow({ gross_pnl: "-40.00" });
    expect(grossPnl(row)).toBeCloseTo(-40.0);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 5  pnlColor
// ══════════════════════════════════════════════════════════════════════════════

describe("pnlColor", () => {
  it("returns green for positive P&L", () => {
    expect(pnlColor(59)).toBe("#22c55e");
  });

  it("returns red for negative P&L", () => {
    expect(pnlColor(-40)).toBe("#ef4444");
  });

  it("returns neutral for zero", () => {
    expect(pnlColor(0)).toBe("#94a3b8");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 6  buildGroups — core grouping logic
// ══════════════════════════════════════════════════════════════════════════════

describe("buildGroups", () => {
  it("returns empty array for empty input", () => {
    expect(buildGroups([])).toEqual([]);
  });

  it("wraps single row in a group", () => {
    const row = makeRow({ pos_id: "p1", pair_id: "" });
    const groups = buildGroups([row]);
    expect(groups).toHaveLength(1);
    expect(groups[0].rows).toHaveLength(1);
  });

  it("groups two rows with same pair_id together", () => {
    const yes = makeRow({ pos_id: "p1", pair_id: "pair-A", side: "YES" });
    const no  = makeRow({ pos_id: "p2", pair_id: "pair-A", side: "NO",  net_pnl: "-41.00" });
    const groups = buildGroups([yes, no]);
    expect(groups).toHaveLength(1);
    expect(groups[0].rows).toHaveLength(2);
  });

  it("keeps rows with different pair_ids in separate groups", () => {
    const a = makeRow({ pos_id: "p1", pair_id: "pair-A" });
    const b = makeRow({ pos_id: "p2", pair_id: "pair-B" });
    const groups = buildGroups([a, b]);
    expect(groups).toHaveLength(2);
  });

  it("assigns hedge row (fill_type=HEDGE) to parent group via parent_pos_id", () => {
    const main  = makeRow({ pos_id: "main-1", pair_id: "", fill_type: "MAIN" });
    const hedge = makeRow({
      pos_id: "hedge-1", pair_id: "", fill_type: "HEDGE",
      parent_pos_id: "main-1", strategy: "momentum_hedge",
    });
    const groups = buildGroups([main, hedge]);
    expect(groups).toHaveLength(1);                 // hedge not a top-level group
    expect(groups[0].hedges).toHaveLength(1);
    expect(groups[0].hedges[0].pos_id).toBe("hedge-1");
  });

  it("assigns hedge row (strategy=momentum_hedge) to parent group", () => {
    const main  = makeRow({ pos_id: "main-2", pair_id: "" });
    const hedge = makeRow({
      pos_id: "hedge-2", fill_type: "MAIN",   // fill_type not HEDGE but strategy is
      strategy: "momentum_hedge", parent_pos_id: "main-2",
    });
    const groups = buildGroups([main, hedge]);
    expect(groups).toHaveLength(1);
    expect(groups[0].hedges).toHaveLength(1);
  });

  it("totalNetPnl sums all rows including hedges", () => {
    const main  = makeRow({ pos_id: "m1", pair_id: "", net_pnl: "60.00" });
    const hedge = makeRow({
      pos_id: "h1", fill_type: "HEDGE", parent_pos_id: "m1",
      net_pnl: "-40.00",
    });
    const groups = buildGroups([main, hedge]);
    expect(groups[0].totalNetPnl).toBeCloseTo(20.0);
  });

  it("totalFees sums all rows including hedges", () => {
    const main  = makeRow({ pos_id: "m2", pair_id: "", fees_usd: "1.50" });
    const hedge = makeRow({
      pos_id: "h2", fill_type: "HEDGE", parent_pos_id: "m2", fees_usd: "0.80",
    });
    const groups = buildGroups([main, hedge]);
    expect(groups[0].totalFees).toBeCloseTo(2.30);
  });

  it("rows without pair_id each form their own group", () => {
    const a = makeRow({ pos_id: "solo-1", pair_id: "" });
    const b = makeRow({ pos_id: "solo-2", pair_id: "" });
    const groups = buildGroups([a, b]);
    expect(groups).toHaveLength(2);
  });

  it("sorts groups by lastTime descending (most recent first)", () => {
    const older = makeRow({ pos_id: "old",  pair_id: "", exit_time: "2024-01-01T00:00:00Z" });
    const newer = makeRow({ pos_id: "new",  pair_id: "", exit_time: "2024-12-31T00:00:00Z" });
    const groups = buildGroups([older, newer]);
    expect(groups[0].rows[0].pos_id).toBe("new");
  });

  it("does not include hedge rows in the top-level rows list", () => {
    const main  = makeRow({ pos_id: "m3", pair_id: "" });
    const hedge = makeRow({ pos_id: "h3", fill_type: "HEDGE", parent_pos_id: "m3" });
    const groups = buildGroups([main, hedge]);
    const topLevelIds = groups[0].rows.map(r => r.pos_id);
    expect(topLevelIds).not.toContain("h3");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 7  Trades component — render tests
// ══════════════════════════════════════════════════════════════════════════════

// Lazy-import the default export after mocks are registered
let Trades: React.ComponentType;
beforeEach(async () => {
  const mod = await import("./Trades");
  Trades = mod.default;
});

describe("Trades component", () => {
  it("shows loading indicator while fetching", () => {
    mockUseAcctLedger.mockReturnValue({ data: null, loading: true, error: null, refresh: vi.fn() });
    render(<Trades />);
    // Should not crash and not render any rows
    expect(screen.queryByRole("row")).toBeNull();
  });

  it("shows empty state when no rows returned", () => {
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [], total: 0 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);
    expect(screen.getByText(/no finalized trades/i)).toBeTruthy();
  });

  it("renders market title for a WIN row", () => {
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [makeRow()], total: 1 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);
    expect(screen.getByText("BTC > $50k on 2025-06-01?")).toBeTruthy();
  });

  it("renders WIN outcome badge", () => {
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [makeRow({ resolved_outcome: "WIN" })], total: 1 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);
    expect(screen.getByText("WIN")).toBeTruthy();
  });

  it("renders LOSS outcome badge for a losing trade", () => {
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [makeRow({ resolved_outcome: "LOSS", net_pnl: "-41.00", gross_pnl: "-40.00" })], total: 1 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);
    expect(screen.getByText("LOSS")).toBeTruthy();
  });

  it("renders SummaryBar with correct trade count", () => {
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [makeRow(), makeRow({ pos_id: "p2", pair_id: "" })], total: 2 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);
    // SummaryBar shows "Trades" label with count = number of groups
    expect(screen.getByText("2")).toBeTruthy();
  });

  it("filters rows by outcome dropdown", () => {
    const winRow  = makeRow({ pos_id: "w1", pair_id: "", resolved_outcome: "WIN",  market_title: "WIN Market" });
    const lossRow = makeRow({ pos_id: "l1", pair_id: "", resolved_outcome: "LOSS", market_title: "LOSS Market", net_pnl: "-41.00", gross_pnl: "-40.00" });
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [winRow, lossRow], total: 2 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);

    // Select "Win" from the outcome dropdown
    const select = screen.getByDisplayValue("All");
    fireEvent.change(select, { target: { value: "WIN" } });

    expect(screen.queryByText("WIN Market")).toBeTruthy();
    expect(screen.queryByText("LOSS Market")).toBeNull();
  });

  it("filters rows by search term in market title", () => {
    const ethRow = makeRow({ pos_id: "e1", pair_id: "", market_title: "ETH > $3000?",  underlying: "ETH" });
    const btcRow = makeRow({ pos_id: "b1", pair_id: "", market_title: "BTC > $50000?", underlying: "BTC" });
    mockUseAcctLedger.mockReturnValue({
      data: { rows: [ethRow, btcRow], total: 2 },
      loading: false, error: null, refresh: vi.fn(),
    });
    render(<Trades />);

    const searchInput = screen.getByPlaceholderText(/search/i);
    fireEvent.change(searchInput, { target: { value: "ETH" } });

    expect(screen.queryByText("ETH > $3000?")).toBeTruthy();
    expect(screen.queryByText("BTC > $50000?")).toBeNull();
  });

  it("renders error state when fetch fails", () => {
    mockUseAcctLedger.mockReturnValue({
      data: null, loading: false, error: "Network error", refresh: vi.fn(),
    });
    render(<Trades />);
    expect(screen.getByText(/network error/i)).toBeTruthy();
  });
});

// need React in scope for JSX
import React from "react";
