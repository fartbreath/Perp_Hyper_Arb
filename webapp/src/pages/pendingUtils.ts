/**
 * Pure helper functions for the Pending page.
 * Kept in a separate module so that Pending.tsx only exports React components
 * (required for Vite Fast Refresh compatibility).
 */
import type { AcctPosition } from "../api/client";

export function timeSince(iso: string | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1)  return "< 1m";
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function unrealizedPnl(pos: AcctPosition): number | null {
  // For CLOSING — we have exit_vwap and entry_vwap
  if (pos.exit_vwap > 0 && pos.entry_vwap > 0 && pos.entry_contracts > 0) {
    const gross = (pos.exit_vwap - pos.entry_vwap) * pos.entry_contracts;
    return gross - pos.fees_usd + pos.rebates_usd;
  }
  return null;
}
