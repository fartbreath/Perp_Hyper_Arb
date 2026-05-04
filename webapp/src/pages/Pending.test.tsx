/**
 * webapp/src/pages/Pending.test.tsx
 *
 * Tests for the Pending Results page.
 *
 * Coverage split:
 *   1. Pure helpers — timeSince, unrealizedPnl (tested directly)
 *   2. Component    — render with mocked useAcctPending hook
 *
 * StatusBadge, WaitingUrgency, SideBadge are tested through the component
 * render because they're internal helpers.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { AcctPosition } from "../api/client";
import { timeSince, unrealizedPnl } from "./pendingUtils";

// ── Mock the API hook ─────────────────────────────────────────────────────────
const mockUseAcctPending = vi.fn();
vi.mock("../api/client", () => ({
  useAcctPending: () => mockUseAcctPending(),
}));

beforeEach(() => {
  mockUseAcctPending.mockReturnValue({ data: null, loading: true, error: null, refresh: vi.fn() });
});

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makePosition(overrides: Partial<AcctPosition> = {}): AcctPosition {
  return {
    pos_id:             "pos-001",
    strategy:           "momentum",
    fill_type:          "MAIN",
    pair_id:            "",
    parent_pos_id:      "",
    market_id:          "0xabc",
    market_title:       "BTC > $50k on 2025-06-01?",
    market_type:        "bucket_daily",
    underlying:         "BTC",
    side:               "YES",
    token_id:           "tok-001",
    entry_vwap:         0.40,
    entry_contracts:    100.0,
    entry_cost_usd:     40.0,
    entry_time:         new Date(Date.now() - 3_600_000).toISOString(),
    pm_entry_confirmed: true,
    spot_entry:         50_000,
    strike:             50_000,
    tte_seconds:        86_400,
    signal_source:      "chainlink",
    signal_score:       75,
    exit_vwap:          0.65,
    exit_contracts:     100.0,
    exit_time:          new Date(Date.now() - 1_200_000).toISOString(),
    exit_type:          "TAKER",
    closing_since:      new Date(Date.now() - 1_200_000).toISOString(),
    spot_exit:          51_000,
    resolve_price:      0,
    resolved_outcome:   "",
    fees_usd:           1.50,
    rebates_usd:        0.50,
    status:             "CLOSING",
    pm_exit_confirmed:  false,
    ...overrides,
  };
}

// ══════════════════════════════════════════════════════════════════════════════
// 1  timeSince
// ══════════════════════════════════════════════════════════════════════════════

describe("timeSince", () => {
  it("returns < 1m for very recent timestamps", () => {
    const iso = new Date(Date.now() - 30_000).toISOString();  // 30 seconds ago
    expect(timeSince(iso)).toBe("< 1m");
  });

  it("returns Xm for times under 1 hour", () => {
    const iso = new Date(Date.now() - 45 * 60_000).toISOString();  // 45 min ago
    expect(timeSince(iso)).toBe("45m");
  });

  it("returns 1m for exactly 1 minute", () => {
    const iso = new Date(Date.now() - 60_000).toISOString();
    expect(timeSince(iso)).toBe("1m");
  });

  it("returns XhYm for times over 1 hour", () => {
    const iso = new Date(Date.now() - 90 * 60_000).toISOString();  // 1h 30m ago
    expect(timeSince(iso)).toBe("1h 30m");
  });

  it("returns XhYm for exactly 2 hours", () => {
    const iso = new Date(Date.now() - 120 * 60_000).toISOString();
    expect(timeSince(iso)).toBe("2h 0m");
  });

  it("returns Xd Yh for times over 24 hours", () => {
    const iso = new Date(Date.now() - 25 * 3_600_000).toISOString();  // 25h ago
    expect(timeSince(iso)).toBe("1d 1h");
  });

  it("returns — for undefined", () => {
    expect(timeSince(undefined)).toBe("—");
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 2  unrealizedPnl
// ══════════════════════════════════════════════════════════════════════════════

describe("unrealizedPnl", () => {
  it("computes gross - fees + rebates when exit_vwap is set", () => {
    // gross = (0.65 - 0.40) × 100 = $25; fees=$1.50; rebates=$0.50 → $24
    const pos = makePosition({
      entry_vwap: 0.40, exit_vwap: 0.65, entry_contracts: 100.0,
      fees_usd: 1.50, rebates_usd: 0.50,
    });
    expect(unrealizedPnl(pos)).toBeCloseTo(24.0);
  });

  it("returns negative value for a losing position", () => {
    // gross = (0.20 - 0.40) × 100 = −$20; fees=$2.00; rebates=$0 → −$22
    const pos = makePosition({
      entry_vwap: 0.40, exit_vwap: 0.20, entry_contracts: 100.0,
      fees_usd: 2.00, rebates_usd: 0.00,
    });
    expect(unrealizedPnl(pos)).toBeCloseTo(-22.0);
  });

  it("returns null when exit_vwap is 0 (no exit fill yet)", () => {
    const pos = makePosition({ exit_vwap: 0 });
    expect(unrealizedPnl(pos)).toBeNull();
  });

  it("returns null when entry_vwap is 0", () => {
    const pos = makePosition({ entry_vwap: 0 });
    expect(unrealizedPnl(pos)).toBeNull();
  });

  it("returns null when entry_contracts is 0", () => {
    const pos = makePosition({ entry_contracts: 0 });
    expect(unrealizedPnl(pos)).toBeNull();
  });

  it("includes rebates in the P&L calculation", () => {
    // gross = (0.65 - 0.40) × 100 = $25; fees=0; rebates=$5 → $30
    const pos = makePosition({
      entry_vwap: 0.40, exit_vwap: 0.65, entry_contracts: 100.0,
      fees_usd: 0, rebates_usd: 5.0,
    });
    expect(unrealizedPnl(pos)).toBeCloseTo(30.0);
  });

  it("scales with contract size", () => {
    // gross = (0.65 - 0.40) × 200 = $50; no fees or rebates → $50
    const pos = makePosition({
      entry_vwap: 0.40, exit_vwap: 0.65, entry_contracts: 200.0,
      fees_usd: 0, rebates_usd: 0,
    });
    expect(unrealizedPnl(pos)).toBeCloseTo(50.0);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// 3  Pending component — render tests
// ══════════════════════════════════════════════════════════════════════════════

let Pending: React.ComponentType;
beforeEach(async () => {
  const mod = await import("./Pending");
  Pending = mod.default;
});

describe("Pending component", () => {
  it("renders without crashing when loading", () => {
    mockUseAcctPending.mockReturnValue({ data: null, loading: true, error: null, refresh: vi.fn() });
    render(<Pending />);
    // No crash. Should show the page skeleton.
    expect(screen.getByText(/pending results/i)).toBeTruthy();
  });

  it("shows 'No pending positions' when positions list is empty", () => {
    mockUseAcctPending.mockReturnValue({
      data: { positions: [] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText(/no pending positions/i)).toBeTruthy();
  });

  it("shows Closing count badge with correct number", () => {
    const closingPos = makePosition({ status: "CLOSING" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [closingPos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    // Badge label is "Closing: 1"
    expect(screen.getByText("Closing: 1")).toBeTruthy();
  });

  it("shows Pending Resolve count badge with correct number", () => {
    const prPos = makePosition({ status: "PENDING_RESOLVE" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [prPos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText("Pending Resolve: 1")).toBeTruthy();
  });

  it("renders CLOSING status badge for a CLOSING position", () => {
    const pos = makePosition({ status: "CLOSING" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    // "CLOSING" may appear in multiple places (header section label + status badge)
    const closingElements = screen.getAllByText("CLOSING");
    expect(closingElements.length).toBeGreaterThan(0);
  });

  it("renders PENDING RESOLVE status badge for a PENDING_RESOLVE position", () => {
    const pos = makePosition({ status: "PENDING_RESOLVE" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText("PENDING RESOLVE")).toBeTruthy();
  });

  it("renders market title in the positions table", () => {
    const pos = makePosition({ market_title: "ETH > $3k on 2025-07-01?" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText("ETH > $3k on 2025-07-01?")).toBeTruthy();
  });

  it("renders YES side badge for YES position", () => {
    const pos = makePosition({ side: "YES" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText("YES")).toBeTruthy();
  });

  it("renders PM entry confirmed badge as ✓ when confirmed", () => {
    const pos = makePosition({ pm_entry_confirmed: true });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    // Badge text is "Entry: ✓" or similar
    expect(screen.getByText(/entry.*✓/i)).toBeTruthy();
  });

  it("renders PM entry badge as ? when not confirmed", () => {
    const pos = makePosition({ pm_entry_confirmed: false });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [pos] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText(/entry.*\?/i)).toBeTruthy();
  });

  it("both CLOSING and PENDING_RESOLVE positions visible at once", () => {
    const closing = makePosition({ pos_id: "c1", status: "CLOSING", market_title: "Market C" });
    const pending = makePosition({ pos_id: "pr1", status: "PENDING_RESOLVE", market_title: "Market P" });
    mockUseAcctPending.mockReturnValue({
      data: { positions: [closing, pending] }, loading: false, error: null, refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText("Market C")).toBeTruthy();
    expect(screen.getByText("Market P")).toBeTruthy();
    expect(screen.getByText("Closing: 1")).toBeTruthy();
    expect(screen.getByText("Pending Resolve: 1")).toBeTruthy();
  });

  it("shows error message when fetch fails", () => {
    mockUseAcctPending.mockReturnValue({
      data: null, loading: false, error: "fetch failed", refresh: vi.fn(),
    });
    render(<Pending />);
    expect(screen.getByText(/fetch failed/i)).toBeTruthy();
  });
});
