/**
 * Pure helper functions and types for the Trades page.
 * Kept in a separate module so that Trades.tsx only exports React components
 * (required for Vite Fast Refresh compatibility).
 */
import type { AcctLedgerRow } from "../api/client";

export function fmtUsd(v: string | number | undefined): string {
  const n = Number(v ?? 0);
  if (!isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}$${n.toFixed(2)}`;
}

export function fmtPrice(v: string | number | undefined): string {
  const n = Number(v ?? 0);
  if (!isFinite(n) || n === 0) return "—";
  return `${(n * 100).toFixed(1)}¢`;
}

export function fmtContracts(v: string | number | undefined): string {
  const n = Number(v ?? 0);
  if (!isFinite(n) || n === 0) return "—";
  return n.toFixed(2);
}

export function netPnl(row: AcctLedgerRow): number { return Number(row.net_pnl ?? 0); }
export function grossPnl(row: AcctLedgerRow): number { return Number(row.gross_pnl ?? 0); }

export function pnlColor(n: number): string {
  return n > 0 ? "#22c55e" : n < 0 ? "#ef4444" : "#94a3b8";
}

export interface LedgerGroup {
  pair_id:      string;
  rows:         AcctLedgerRow[];
  hedges:       AcctLedgerRow[];
  totalNetPnl:  number;
  totalGross:   number;
  totalFees:    number;
  totalRebates: number;
  lastTime:     string;
}

export function buildGroups(rows: AcctLedgerRow[]): LedgerGroup[] {
  const hedges = rows.filter(r => r.fill_type === "HEDGE" || r.strategy === "momentum_hedge");
  const mains  = rows.filter(r => r.fill_type !== "HEDGE" && r.strategy !== "momentum_hedge");

  const hedgeByParent = new Map<string, AcctLedgerRow[]>();
  for (const h of hedges) {
    const key = h.parent_pos_id || h.market_id;
    const arr = hedgeByParent.get(key) ?? [];
    arr.push(h);
    hedgeByParent.set(key, arr);
  }

  const pairMap = new Map<string, AcctLedgerRow[]>();
  for (const r of mains) {
    const key = r.pair_id || r.pos_id;
    const arr = pairMap.get(key) ?? [];
    arr.push(r);
    pairMap.set(key, arr);
  }

  return Array.from(pairMap.entries()).map(([pairKey, grpRows]) => {
    const grpHedges: AcctLedgerRow[] = [];
    const seenHedgeIds = new Set<string>();
    for (const r of grpRows) {
      for (const h of (hedgeByParent.get(r.pos_id) ?? [])) {
        if (!seenHedgeIds.has(h.pos_id)) { grpHedges.push(h); seenHedgeIds.add(h.pos_id); }
      }
      for (const h of (hedgeByParent.get(r.market_id) ?? [])) {
        if (!seenHedgeIds.has(h.pos_id)) { grpHedges.push(h); seenHedgeIds.add(h.pos_id); }
      }
    }
    const allRows = [...grpRows, ...grpHedges];
    return {
      pair_id:      pairKey,
      rows:         grpRows,
      hedges:       grpHedges,
      totalNetPnl:  allRows.reduce((s, r) => s + Number(r.net_pnl   ?? 0), 0),
      totalGross:   allRows.reduce((s, r) => s + Number(r.gross_pnl  ?? 0), 0),
      totalFees:    allRows.reduce((s, r) => s + Number(r.fees_usd   ?? 0), 0),
      totalRebates: allRows.reduce((s, r) => s + Number(r.rebates_usd ?? 0), 0),
      lastTime:     grpRows.reduce((best, r) => {
        const t = r.exit_time || r.entry_time || "";
        return t > best ? t : best;
      }, ""),
    };
  }).sort((a, b) => b.lastTime.localeCompare(a.lastTime));
}
