/**
 * Trades — Per-market aggregated view.
 *
 * Each row represents one market. Legs (YES + NO) are aggregated together:
 *
 *   Market      — title (hover for market_id)
 *   Type        — bucket_5m / bucket_15m / milestone / etc.
 *   Underlying  — BTC / ETH / SOL …
 *   Sides       — e.g. "YES 20ct@0.54 · NO 19ct@0.52"  (one badge per leg)
 *   Close Type  — RESOLVED→0 | RESOLVED→1 | TAKER EXIT | PRE-EXPIRY
 *   Size        — total USDC deployed (sum both legs)
 *   Signal      — avg signal score across legs
 *   Gross       — price-move before fees (sum)
 *   Fees        — total fees paid (sum)
 *   Rebate      — total rebates earned (sum)
 *   Net P&L     — realised PnL (sum) with colour
 */
import { useState, useMemo, Fragment } from "react";
import { useTrades, useMarketOutcomes } from "../api/client";
import type { Trade } from "../api/client";

const UNDERLYINGS = ["", "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"];
const TYPES       = ["", "bucket_5m", "bucket_15m", "bucket_1h", "bucket_4h", "milestone", "hl_perp"];
const ALL_LIMIT   = 2000; // fetch enough to aggregate client-side (API cap is 5000)

// ── Aggregation helpers ────────────────────────────────────────────────────────

function isHl(t: Trade) { return t.market_type === "hl_perp"; }

/** Gross = price-move before fees: pnl + fees − rebates */
function gross(t: Trade): number {
  return Number(t.pnl) + Number(t.fees_paid) - Number(t.rebates_earned);
}

/**
 * Reverse-engineer the exit price from the recorded P&L.
 * P&L = (exit - entry) * size for both YES and NO tokens.
 * entry_price is the actual token fill price in both cases.
 *   exit = entry + gross / size
 */
function impliedExitToken(t: Trade): number | null {
  const size = Number(t.size);
  if (size === 0) return null;
  const entry = Number(t.price);
  const g = gross(t);
  return entry + g / size;
}

/**
 * Classify close type from the implied exit token price.
 * Near 0 or 1 → resolution snap. Otherwise taker or pre-expiry.
 * exit token price is the same space as entry for both YES and NO.
 */
function closeType(legs: Trade[]): string {
  // Check each leg — all legs of a spread share the same close type
  for (const t of legs) {
    if (isHl(t)) return "HL CLOSE";
    const exitToken = impliedExitToken(t);
    if (exitToken === null) continue;
    const dist0 = Math.abs(exitToken - 0);
    const dist1 = Math.abs(exitToken - 1);
    const snapDist = Math.min(dist0, dist1);
    if (snapDist < 0.015) {
      const settled = dist1 < dist0 ? 1 : 0;
      return `RESOLVED → ${settled}`;
    }
    // exit near 0 but not truly 0 → pre-expiry taker
    if (exitToken < 0.05 || exitToken > 0.95) return "PRE-EXPIRY";
    return `TAKER @ ${exitToken.toFixed(3)}`;
  }
  return "—";
}

interface MarketGroup {
  market_id: string;
  market_title: string;
  market_type: string;
  underlying: string;
  strategy: string;
  legs: Trade[];              // individual rows (strategy !== "momentum_hedge")
  hedgeTrades: Trade[];       // GTD hedge fill rows (strategy === "momentum_hedge")
  totalSize: number;          // USDC deployed across main legs only
  totalGross: number;
  totalFees: number;
  totalRebates: number;
  totalPnl: number;
  avgScore: number | null;
  closeTypeLabel: string;
  lastTimestamp: string;
  isWin: boolean;             // net positive
}

function aggregateToMarkets(trades: Trade[], typeFilter: string): MarketGroup[] {
  const map = new Map<string, Trade[]>();
  for (const t of trades) {
    const bucket = map.get(t.market_id) ?? [];
    bucket.push(t);
    map.set(t.market_id, bucket);
  }

  const groups: MarketGroup[] = [];
  for (const [mid, allRows] of map) {
    // Separate GTD hedge fill rows from main position legs
    const legs        = allRows.filter(t => t.strategy !== "momentum_hedge");
    const hedgeTrades = allRows.filter(t => t.strategy === "momentum_hedge");
    // Use first main leg for market metadata; fall back to first row if only hedges exist
    const first = legs[0] ?? allRows[0];
    const mtype = first.market_type ?? "";
    if (typeFilter && mtype !== typeFilter) continue;

    // USDC cost basis: main position legs only (entry_price is the actual token price)
    const totalSize = legs.reduce((acc, t) => {
      return acc + (isHl(t) ? Number(t.size) * Number(t.price) : Number(t.price) * Number(t.size));
    }, 0);

    const totalGross   = legs.reduce((a, t) => a + gross(t), 0);
    // Fees and rebates across all rows including hedge fills
    const totalFees    = [...legs, ...hedgeTrades].reduce((a, t) => a + Number(t.fees_paid), 0);
    const totalRebates = [...legs, ...hedgeTrades].reduce((a, t) => a + Number(t.rebates_earned), 0);
    // Include hedge PnL so market-level total correctly reflects realized outcome
    let totalPnl       = [...legs, ...hedgeTrades].reduce((a, t) => a + Number(t.pnl), 0);

    // Hedge cost accounting: only deduct hedge_size_usd when there is credible evidence
    // the hedge order was filled AND lost (i.e., main WON → hedge was on the losing token
    // which may have briefly touched the 2¢ resting bid near expiry via book drain).
    // When main LOST, the hedge was on the WINNING token (going to $1, not $0.02) —
    // a GTC limit BUY at 2¢ on a token trading 37¢→100¢ is structurally impossible to
    // fill, so cost = $0.  GTC limit orders debit USDC only on fill, not on placement;
    // unfilled orders are returned at market close.
    const hedgeLeg = legs.find(t => t.hedge_order_id);
    if (hedgeLeg && hedgeTrades.length === 0) {
      const ctLabel = closeType(legs);
      // TP exit: hedge was actively cancelled, no cost lost.
      let exitHedgeTp: number | null = null;
      for (const t of legs) { const ex = impliedExitToken(t); if (ex !== null) { exitHedgeTp = ex; break; } }
      const isTpExit = ctLabel === "PRE-EXPIRY" && exitHedgeTp !== null && exitHedgeTp > 0.95;
      if (!isTpExit) {
        // Deduct only when main WON (hedge was on losing token — can briefly touch 2¢
        // via book drain).  Main WON = exit token > 0.95 OR resolved_outcome="WIN".
        const mainWon = legs.some(t => {
          const ex = impliedExitToken(t);
          const roWin = (t.resolved_outcome ?? "").toUpperCase() === "WIN";
          return roWin || (ex !== null && ex > 0.95);
        });
        if (mainWon) {
          totalPnl -= Number(hedgeLeg.hedge_size_usd ?? 0);
        }
      }
    }

    const scores = legs.map((t) => Number(t.signal_score)).filter((s) => !isNaN(s) && s > 0);
    const avgScore = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;

    const sorted = [...legs].sort((a, b) => (a.timestamp > b.timestamp ? -1 : 1));

    groups.push({
      market_id: mid,
      market_title: first.market_title ?? mid.slice(0, 12) + "…",
      market_type: mtype,
      underlying: first.underlying ?? "—",
      strategy: first.strategy ?? "—",
      legs,
      hedgeTrades,
      totalSize,
      totalGross,
      totalFees,
      totalRebates,
      totalPnl,
      avgScore,
      closeTypeLabel: closeType(legs),
      lastTimestamp: sorted[0]?.timestamp ?? "",
      isWin: totalPnl >= 0,
    });
  }

  return groups.sort((a, b) => (a.lastTimestamp > b.lastTimestamp ? -1 : 1));
}

// ── Formatting ─────────────────────────────────────────────────────────────────

function pnlColor(v: number) { return v >= 0 ? "#22c55e" : "#ef4444"; }

function fmtSigned(v: number, dec = 2) {
  return `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(dec)}`;
}

function fmtTs(ts: string) {
  try {
    const d = new Date(ts);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
      + " " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return ts.slice(0, 16) || "—"; }
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function TypeBadge({ t }: { t: string }) {
  const colour: Record<string, string> = {
    bucket_5m:  "#6366f1", bucket_15m: "#8b5cf6", bucket_1h: "#a855f7",
    bucket_4h:  "#c026d3", milestone:  "#0ea5e9", hl_perp:  "#f97316",
    range:      "#0d9488",
  };
  return (
    <span style={{
      background: colour[t] ?? "#475569",
      color: "#fff", fontSize: "0.72em", fontWeight: 700,
      padding: "2px 6px", borderRadius: 4,
    }}>{t}</span>
  );
}

function CloseBadge({ label }: { label: string }) {
  let bg = "#475569";
  if (label.startsWith("RESOLVED \u2192 WIN") || label.startsWith("RESOLVED \u2192 1")) bg = "#15803d";
  else if (label.startsWith("RESOLVED \u2192 LOSS") || label.startsWith("RESOLVED \u2192 0")) bg = "#b91c1c";
  else if (label.includes("\u2192 WIN")) bg = "#166534";  // taker/pre-expiry + known WIN
  else if (label.includes("\u2192 LOSS")) bg = "#7f1d1d"; // taker/pre-expiry + known LOSS
  else if (label.startsWith("TAKER")) bg = "#ca8a04";
  else if (label.startsWith("PRE-EXPIRY")) bg = "#0284c7";
  return (
    <span style={{
      background: bg, color: "#fff", fontSize: "0.72em", fontWeight: 700,
      padding: "2px 7px", borderRadius: 4, whiteSpace: "nowrap",
    }}>{label}</span>
  );
}

/** Derive WIN/LOSS for a leg, checking CSV field first then market_outcomes lookup. */
function effectiveOutcome(
  t: Trade,
  outcomes: Record<string, { resolved_yes_price: number }> | null,
): "WIN" | "LOSS" | "" {
  if (t.resolved_outcome === "WIN" || t.resolved_outcome === "LOSS") return t.resolved_outcome;
  const entry = outcomes?.[t.market_id];
  if (!entry) return "";
  const rp = entry.resolved_yes_price;
  if (rp !== 0 && rp !== 1) return "";
  const isYesSide = t.side === "YES" || t.side === "UP";
  return rp === 1 ? (isYesSide ? "WIN" : "LOSS") : (isYesSide ? "LOSS" : "WIN");
}

/** One badge per leg: "YES 19.0ct@0.54" */
function LegsBadges({ legs, outcomes }: { legs: Trade[]; outcomes: Record<string, { resolved_yes_price: number }> | null }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {legs.map((t, i) => {
        const ct   = Number(t.size).toFixed(1);
        // entry_price is the actual token fill price for both YES and NO.
        const px   = Number(t.price).toFixed(3);
        const exitTok = impliedExitToken(t);
        const exitStr = exitTok !== null ? ` → ${exitTok.toFixed(3)}` : "";
        const bg   = (t.side === "YES" || t.side === "UP") ? "#0c4a6e" : "#4a1d0c";
        const col  = (t.side === "YES" || t.side === "UP") ? "#38bdf8" : "#fb923c";
        const score = t.signal_score ? ` ·${Number(t.signal_score).toFixed(0)}` : "";
        // Append WIN/LOSS badge when the market resolved
        const eff = effectiveOutcome(t, outcomes);
        const resolvedStr = eff === "WIN" ? " \u2714WIN" : eff === "LOSS" ? " \u2718LOSS" : "";
        return (
          <span key={i} style={{
            background: bg, color: col, fontSize: "0.75em",
            fontWeight: 600, padding: "2px 6px", borderRadius: 4,
            whiteSpace: "nowrap",
          }}>
            {t.side} {ct}ct @ {px}{exitStr}{score}{resolvedStr}
          </span>
        );
      })}
    </div>
  );
}

// ── Summary bar ───────────────────────────────────────────────────────────────

function SummaryBar({ groups }: { groups: MarketGroup[] }) {
  if (groups.length === 0) return null;
  const totalPnl  = groups.reduce((a, g) => a + g.totalPnl, 0);
  const wins      = groups.filter((g) => g.isWin).length;
  const losses    = groups.length - wins;
  const totalFees = groups.reduce((a, g) => a + g.totalFees, 0);
  const totalReb  = groups.reduce((a, g) => a + g.totalRebates, 0);
  return (
    <div style={{
      display: "flex", gap: 24, flexWrap: "wrap",
      padding: "10px 16px", marginBottom: 12,
      background: "rgba(255,255,255,0.04)", borderRadius: 8,
      fontSize: "0.88em",
    }}>
      <span>Markets: <strong>{groups.length}</strong></span>
      <span>Wins: <strong style={{ color: "#22c55e" }}>{wins}</strong></span>
      <span>Losses: <strong style={{ color: "#ef4444" }}>{losses}</strong></span>
      <span>Win rate: <strong>{groups.length ? ((wins / groups.length) * 100).toFixed(0) : 0}%</strong></span>
      <span>Fees: <strong style={{ color: "#ef4444" }}>−${totalFees.toFixed(2)}</strong></span>
      <span>Rebates: <strong style={{ color: "#22c55e" }}>+${totalReb.toFixed(2)}</strong></span>
      <span style={{ marginLeft: "auto" }}>
        Net P&L: <strong style={{ color: pnlColor(totalPnl), fontSize: "1.05em" }}>
          {fmtSigned(totalPnl)}
        </strong>
      </span>
    </div>
  );
}

// ── Hedge sub-section (shown inside expanded LegDetail) ────────────────────────

function HedgeSection({
  legs, hedgeTrades, closeTypeLabel,
}: {
  legs: Trade[];
  hedgeTrades: Trade[];
  closeTypeLabel: string;
}) {
  const hedgeLeg = legs.find(t => t.hedge_order_id);
  if (!hedgeLeg) return null;

  const entryPriceCents = Number(hedgeLeg.hedge_price ?? 0) * 100;
  const sizeUsd         = Number(hedgeLeg.hedge_size_usd ?? 0);

  // momentum_hedge row — written when the hedge outcome is recorded by the backend.
  // The hedge_status field is the canonical source of truth (set by PM API callbacks).
  // Fall back to legacy frontend heuristics for records predating hedge_status.
  const fillRow     = hedgeTrades[0];
  const hedgeStatus = fillRow?.hedge_status ?? "";  // from trades.csv (backend source of truth)

  // Fill ratio fields from HedgeOrder lifecycle (new columns)
  const sizeFilled    = Number(fillRow?.hedge_size_filled ?? 0);
  const avgFillPrice  = Number(fillRow?.hedge_avg_fill_price ?? 0);
  const orderSize     = Number(hedgeLeg.hedge_size_usd ?? 0) / Math.max(Number(hedgeLeg.hedge_price ?? 1), 0.001);
  const fillRatioPct  = orderSize > 0 ? (sizeFilled / orderSize) * 100 : 0;

  let status: string;
  let statusBg: string;
  let statusFg: string;
  let exitStr: string;
  let pnlDisplay: string;
  let pnlFg: string;
  let spotResolveDisplay: string = "—";
  let sizeDisplay: string = `$${sizeUsd.toFixed(2)}`;

  // ── Backend-authoritative path (hedge_status populated by PM API callbacks) ───
  if (hedgeStatus === "filled_won") {
    // Hedge order filled AND hedge token won (main LOST) → payout redeemed
    status    = "Expired - WON";
    statusBg  = "#14532d";
    statusFg  = "#4ade80";
    exitStr   = "1.000";
    const p   = Number(fillRow.pnl);
    pnlDisplay = fmtSigned(p, 2);
    pnlFg      = pnlColor(p);
    const srp = Number(fillRow?.spot_resolve_price ?? 0);
    spotResolveDisplay = srp > 0 ? `$${srp.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—";

  } else if (hedgeStatus === "filled_lost") {
    // Hedge order filled but hedge token lost (main WON) → cost = fill_price × size
    status    = "Expired - LOST";
    statusBg  = "#450a0a";
    statusFg  = "#f87171";
    exitStr   = "0.000";
    const loss = -sizeUsd;
    pnlDisplay = fmtSigned(loss, 2);
    pnlFg      = pnlColor(loss);
    const srp = Number(fillRow?.spot_resolve_price ?? 0);
    spotResolveDisplay = srp > 0 ? `$${srp.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—";

  } else if (hedgeStatus === "unfilled") {
    // Order was never filled (token never in wallet) → cost = $0
    status    = "Expired - Unfilled";
    statusBg  = "#1c1917";
    statusFg  = "#6b7280";
    exitStr   = "—";
    pnlDisplay = "$0.00";
    pnlFg      = "#6b7280";
    const srp = Number(fillRow?.spot_resolve_price ?? 0);
    spotResolveDisplay = srp > 0 ? `$${srp.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—";

  } else if (hedgeStatus === "cancelled") {
    // Order was cancelled by the bot before it could fill → cost = $0
    status    = "Cancelled";
    statusBg  = "#431407";
    statusFg  = "#fb923c";
    exitStr   = "—";
    pnlDisplay = "$0.00";
    pnlFg      = "#6b7280";

  } else if (hedgeStatus === "filled_exited") {
    // Hedge order filled during deferred-cancel window, then market-sold for recovery
    status    = "Filled – Exited";
    statusBg  = "#1e3a5f";
    statusFg  = "#60a5fa";
    exitStr   = "SOLD";
    const p   = Number(fillRow?.pnl ?? 0);
    pnlDisplay = fmtSigned(p, 2);
    pnlFg      = pnlColor(p);
    const srp = Number(fillRow?.spot_resolve_price ?? 0);
    spotResolveDisplay = srp > 0 ? `$${srp.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—";
    if (sizeFilled > 0 && avgFillPrice > 0) {
      sizeDisplay = `${sizeFilled.toFixed(2)} ct @ ${(avgFillPrice * 100).toFixed(1)}¢`;
    }

  } else if (hedgeStatus === "cancelled_partial") {
    // Cancelled after accumulating partial fills
    status    = "Cancelled (partial)";
    statusBg  = "#431407";
    statusFg  = "#fb923c";
    exitStr   = "—";
    const p   = Number(fillRow?.pnl ?? 0);
    pnlDisplay = fmtSigned(p, 2);
    pnlFg      = pnlColor(p);
    if (sizeFilled > 0 && orderSize > 0) {
      sizeDisplay = `${sizeFilled.toFixed(2)}/${orderSize.toFixed(2)} ct (${fillRatioPct.toFixed(0)}%)`;
    }

  } else if (hedgeStatus === "expired_partial") {
    // GTD order expired with partial fill
    status    = "Expired – Partial";
    statusBg  = "#451a03";
    statusFg  = "#fbbf24";
    exitStr   = "PARTIAL";
    const p   = Number(fillRow?.pnl ?? 0);
    pnlDisplay = fmtSigned(p, 2);
    pnlFg      = pnlColor(p);
    const srp = Number(fillRow?.spot_resolve_price ?? 0);
    spotResolveDisplay = srp > 0 ? `$${srp.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—";
    if (sizeFilled > 0 && orderSize > 0) {
      sizeDisplay = `${sizeFilled.toFixed(2)}/${orderSize.toFixed(2)} ct (${fillRatioPct.toFixed(0)}%)`;
    }

  } else if (fillRow) {
    // Legacy: momentum_hedge row exists but no hedge_status field (old records)
    // Treat as "Filled" — hedge was redeemed with payout > 0
    status    = "Filled";
    statusBg  = "#14532d";
    statusFg  = "#4ade80";
    exitStr   = "1.000";
    const p   = Number(fillRow.pnl);
    pnlDisplay = fmtSigned(p, 2);
    pnlFg      = pnlColor(p);

  } else {
    // ── Legacy heuristic path (no hedge_status, no fill row) ────────────────
    let exitTokenPrice: number | null = null;
    for (const t of legs) {
      const ex = impliedExitToken(t);
      if (ex !== null) { exitTokenPrice = ex; break; }
    }
    const isTpExit = closeTypeLabel === "PRE-EXPIRY" && exitTokenPrice !== null && exitTokenPrice > 0.95;
    const isLossEarlyExit = closeTypeLabel.startsWith("TAKER") ||
                            (closeTypeLabel === "PRE-EXPIRY" && exitTokenPrice !== null && exitTokenPrice < 0.05);

    if (isTpExit) {
      status    = "Cancelled";
      statusBg  = "#431407";
      statusFg  = "#fb923c";
      exitStr   = "—";
      pnlDisplay = "—";
      pnlFg      = "#94a3b8";
    } else if (isLossEarlyExit) {
      // Taker exits keep the hedge as an independent recovery flow. Without an
      // explicit momentum_hedge row from the backend, do not infer final hedge
      // state from the main leg's outcome.
      status    = "Recovery";
      statusBg  = "#1e3a5f";
      statusFg  = "#60a5fa";
      exitStr   = "—";
      pnlDisplay = "—";
      pnlFg      = "#94a3b8";
    } else {
      const anyResolved = legs.some(t => {
        const ex = impliedExitToken(t);
        return ex !== null && (ex < 0.05 || ex > 0.95);
      });
      if (anyResolved) {
        const mainWonResolved = legs.some(t => {
          const ex = impliedExitToken(t);
          const roWin = (t.resolved_outcome ?? "").toUpperCase() === "WIN";
          return roWin || (ex !== null && ex > 0.95);
        });
        if (mainWonResolved) {
          status    = "Expired - LOST";
          statusBg  = "#450a0a";
          statusFg  = "#f87171";
          exitStr   = "0.000";
          const loss = -sizeUsd;
          pnlDisplay = fmtSigned(loss, 2);
          pnlFg      = pnlColor(loss);
        } else {
          status    = "Expired - Unfilled";
          statusBg  = "#1c1917";
          statusFg  = "#6b7280";
          exitStr   = "—";
          pnlDisplay = "$0.00";
          pnlFg      = "#6b7280";
        }
      } else {
        status    = "Placed";
        statusBg  = "#2e1065";
        statusFg  = "#c4b5fd";
        exitStr   = "—";
        pnlDisplay = "—";
        pnlFg      = "#94a3b8";
      }
    }
  }

  return (
    <div style={{ marginTop: 10, borderTop: "1px dashed #334155", paddingTop: 8 }}>
      <div style={{
        color: "#a78bfa", fontSize: "0.72em", fontWeight: 700,
        textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 5,
      }}>
        GTD Hedge
      </div>
      <table style={{ width: "100%", fontSize: "0.8em", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ color: "#94a3b8", borderBottom: "1px solid #1e293b" }}>
            <th style={{ textAlign: "left",  padding: "2px 8px" }}>Time</th>
            <th style={{ textAlign: "left",  padding: "2px 8px" }}>Status</th>
            <th style={{ textAlign: "right", padding: "2px 8px" }}>Entry (¢)</th>
            <th style={{ textAlign: "right", padding: "2px 8px" }}>Size ($)</th>
            <th style={{ textAlign: "right", padding: "2px 8px" }}>Exit</th>
            <th style={{ textAlign: "right", padding: "2px 8px" }}>Spot Resolve</th>
            <th style={{ textAlign: "right", padding: "2px 8px" }}>Net P&L</th>
            <th style={{ textAlign: "right", padding: "2px 8px", color: "#374151" }}>Order</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td style={{ padding: "3px 8px", color: "#64748b" }}>{fmtTs(hedgeLeg.timestamp)}</td>
            <td style={{ padding: "3px 8px" }}>
              <span style={{
                background: statusBg, color: statusFg, fontWeight: 700,
                fontSize: "0.85em", padding: "2px 7px", borderRadius: 4,
              }}>
                {status}
              </span>
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#a78bfa" }}>
              {entryPriceCents.toFixed(1)}¢
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace" }}>
              {sizeDisplay}
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#94a3b8" }}>
              {exitStr}
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#94a3b8" }}>
              {spotResolveDisplay}
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", fontWeight: 700, color: pnlFg }}>
              {pnlDisplay}
            </td>
            <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#374151", fontSize: "0.75em" }}
                title={`Token: ${hedgeLeg.hedge_token_id ?? "—"}`}>
              {hedgeLeg.hedge_order_id?.slice(0, 10)}…
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// ── Expanded leg detail ───────────────────────────────────────────────────────

function LegDetail({
  legs, hedgeTrades, closeTypeLabel, outcomes,
}: {
  legs: Trade[];
  hedgeTrades: Trade[];
  closeTypeLabel: string;
  outcomes: Record<string, { resolved_yes_price: number }> | null;
}) {
  return (
    <tr>
      <td colSpan={14} style={{ padding: "6px 16px 10px 32px", background: "rgba(0,0,0,0.25)" }}>
        <table style={{ width: "100%", fontSize: "0.8em", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: "#94a3b8", borderBottom: "1px solid #334155" }}>
              <th style={{ textAlign: "left",  padding: "3px 8px" }}>Time</th>
              <th style={{ textAlign: "left",  padding: "3px 8px" }}>Side</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Contracts</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Entry</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Exit (calc)</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Resolved</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Strike</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Spot Entry</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Spot Exit</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Gross</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Fee</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Rebate</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Net P&L</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Score</th>
            </tr>
          </thead>
          <tbody>
            {legs.map((t, i) => {
              const g      = gross(t);
              const pnl    = Number(t.pnl);
              const fee    = Number(t.fees_paid);
              const reb    = Number(t.rebates_earned);
              // entry_price is the actual token fill price for both YES and NO.
              const exitToken  = impliedExitToken(t);
              const entryToken = Number(t.price);
              const entrySpot  = Number(t.spot_price);
              const exitSpot   = t.exit_spot_price ? Number(t.exit_spot_price) : null;
              // Resolved trades: exit_spot_price IS the resolved spot (same oracle).
              const eff = effectiveOutcome(t, outcomes);
              // Stop-loss/taker trades: exit_spot_price = spot at time of SL fire.
              const isResolved = (eff === "WIN" || eff === "LOSS")
                || ((exitToken !== null) && (Math.abs(exitToken) < 0.015 || Math.abs(exitToken - 1) < 0.015));
              const exitSpotLabel = isResolved ? "Resolved" : "Exit";
              return (
                <tr key={i} style={{ borderBottom: "1px solid #1e293b" }}>
                  <td style={{ padding: "3px 8px", color: "#64748b" }}>{fmtTs(t.timestamp)}</td>
                  <td style={{ padding: "3px 8px" }}>
                    <span style={{
                      color: (t.side === "YES" || t.side === "UP") ? "#38bdf8" : "#fb923c",
                      fontWeight: 600,
                    }}>{t.side}</span>
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace" }}>
                    {Number(t.size).toFixed(2)}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace" }}>
                    {entryToken.toFixed(4)}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#94a3b8" }}>
                    {exitToken !== null ? exitToken.toFixed(4) : "—"}
                  </td>
                  {/* Resolved outcome — WIN/LOSS regardless of whether bot held to resolution */}
                  <td style={{ padding: "3px 8px", textAlign: "right" }}>
                    {eff === "WIN"
                      ? <span style={{ color: "#22c55e", fontWeight: 700 }}>✔ WIN</span>
                      : eff === "LOSS"
                        ? <span style={{ color: "#ef4444", fontWeight: 700 }}>✘ LOSS</span>
                        : <span style={{ color: "#475569" }}>—</span>
                    }
                  </td>
                  {/* Strike price */}
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#a78bfa" }}>
                    {t.strike && Number(t.strike) > 0
                      ? `$${Number(t.strike).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`
                      : "—"}
                  </td>
                  {/* Underlying spot at entry */}
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", color: "#cbd5e1" }}>
                    {entrySpot > 0 ? `$${entrySpot.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}` : "—"}
                  </td>
                  {/* Underlying spot at exit (stop-loss) or resolution */}
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace" }}
                      title={exitSpotLabel}>
                    {exitSpot && exitSpot > 0
                      ? <span style={{ color: isResolved ? "#94a3b8" : "#fbbf24" }}>
                          ${exitSpot.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
                        </span>
                      : <span style={{ color: "#475569" }}>—</span>
                    }
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", color: pnlColor(g), fontFamily: "monospace" }}>
                    {fmtSigned(g, 4)}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", color: fee ? "#ef4444" : "#475569", fontFamily: "monospace" }}>
                    {fee ? `−$${fee.toFixed(4)}` : "—"}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", color: reb ? "#22c55e" : "#475569", fontFamily: "monospace" }}>
                    {reb ? `+$${reb.toFixed(4)}` : "—"}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", fontFamily: "monospace", fontWeight: 700, color: pnlColor(pnl) }}>
                    {fmtSigned(pnl, 4)}
                  </td>
                  <td style={{ padding: "3px 8px", textAlign: "right", color: "#94a3b8" }}>
                    {t.signal_score ? Number(t.signal_score).toFixed(1) : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <HedgeSection legs={legs} hedgeTrades={hedgeTrades} closeTypeLabel={closeTypeLabel} />
      </td>
    </tr>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Trades() {
  const [underlying, setUnderlying] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [expanded,   setExpanded]   = useState<Set<string>>(new Set());

  const { data, loading, error } = useTrades(ALL_LIMIT, 0, undefined, underlying || undefined);
  const { data: outcomes } = useMarketOutcomes();

  const groups = useMemo(() => {
    if (!data?.trades) return [];
    return aggregateToMarkets(data.trades, typeFilter);
  }, [data, typeFilter]);

  function toggleExpand(mid: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(mid)) next.delete(mid); else next.add(mid);
      return next;
    });
  }

  return (
    <div className="page">
      <h2>Trades <span className="muted" style={{ fontSize: "0.6em", fontWeight: 400 }}>per market</span></h2>

      <div className="filters">
        <label>Underlying:
          <select value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
            {UNDERLYINGS.map((u) => <option key={u} value={u}>{u || "All"}</option>)}
          </select>
        </label>
        <label>Type:
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
            {TYPES.map((tp) => <option key={tp} value={tp}>{tp || "All types"}</option>)}
          </select>
        </label>
        <span className="muted">{groups.length} markets</span>
      </div>

      {error   && <div className="error">Failed to load trades: {error}</div>}
      {loading && <div className="skeleton" style={{ height: 200 }} />}

      {!loading && data && (
        <>
          <SummaryBar groups={groups} />

          <table className="data-table" style={{ tableLayout: "auto" }}>
            <thead>
              <tr>
                <th style={{ width: 36 }}></th>
                <th>Market</th>
                <th>Type</th>
                <th>Underlying</th>
                <th>Side(s)</th>
                <th>Close Type</th>
                <th style={{ textAlign: "right" }}>Size</th>
                <th style={{ textAlign: "right" }}>Signal</th>
                <th style={{ textAlign: "right" }}>Gross</th>
                <th style={{ textAlign: "right" }}>Fees</th>
                <th style={{ textAlign: "right" }}>Rebate</th>
                <th style={{ textAlign: "right" }}>Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => {
                const isOpen = expanded.has(g.market_id);
                return (
                  <Fragment key={g.market_id}>
                    <tr
                      style={{ cursor: "pointer", background: isOpen ? "rgba(255,255,255,0.04)" : undefined }}
                      onClick={() => toggleExpand(g.market_id)}
                    >
                      {/* expand toggle */}
                      <td style={{ textAlign: "center", color: "#64748b", userSelect: "none", paddingRight: 0 }}>
                        {isOpen ? "▼" : "▶"}
                      </td>

                      {/* market title */}
                      <td title={g.market_id} style={{ maxWidth: 260 }}>
                        <span style={{ fontSize: "0.87em" }}>{g.market_title}</span>
                      </td>

                      {/* type badge */}
                      <td>
                        <TypeBadge t={g.market_type} />
                        {g.strategy === "range" && (
                          <span style={{ marginLeft: 4, background: "#0d9488", color: "#fff", fontSize: "0.72em", fontWeight: 700, padding: "2px 6px", borderRadius: 4 }}>RANGE</span>
                        )}
                      </td>

                      {/* underlying */}
                      <td style={{ fontWeight: 600 }}>{g.underlying}</td>

                      {/* legs */}
                      <td><LegsBadges legs={g.legs} outcomes={outcomes} /></td>

                      {/* close type — augment taker labels with resolved outcome when known */}
                      <td><CloseBadge label={(() => {
                        const lbl = g.closeTypeLabel;
                        if (!lbl.startsWith("TAKER") && !lbl.startsWith("PRE-EXPIRY")) return lbl;
                        const eff = g.legs
                          .map(t => effectiveOutcome(t, outcomes))
                          .find(o => o === "WIN" || o === "LOSS");
                        return eff ? `${lbl} → ${eff}` : lbl;
                      })()} /></td>

                      {/* size */}
                      <td style={{ textAlign: "right", fontFamily: "monospace" }}>
                        ${g.totalSize.toFixed(2)}
                      </td>

                      {/* signal score */}
                      <td style={{ textAlign: "right", color: "#94a3b8" }}>
                        {g.avgScore !== null ? g.avgScore.toFixed(1) : "—"}
                      </td>

                      {/* gross */}
                      <td style={{ textAlign: "right", fontFamily: "monospace", color: pnlColor(g.totalGross) }}>
                        {fmtSigned(g.totalGross)}
                      </td>

                      {/* fees */}
                      <td style={{ textAlign: "right", fontFamily: "monospace", color: g.totalFees > 0 ? "#ef4444" : "#475569" }}>
                        {g.totalFees > 0 ? `−$${g.totalFees.toFixed(4)}` : "—"}
                      </td>

                      {/* rebate */}
                      <td style={{ textAlign: "right", fontFamily: "monospace", color: g.totalRebates > 0 ? "#22c55e" : "#475569" }}>
                        {g.totalRebates > 0 ? `+$${g.totalRebates.toFixed(4)}` : "—"}
                      </td>

                      {/* net P&L */}
                      <td style={{ textAlign: "right" }}>
                        <span style={{
                          color: pnlColor(g.totalPnl), fontWeight: 700,
                          fontFamily: "monospace", fontSize: "1.0em",
                        }}>
                          {fmtSigned(g.totalPnl)}
                        </span>
                      </td>
                    </tr>

                    {/* expandable leg detail */}
                    {isOpen && <LegDetail legs={g.legs} hedgeTrades={g.hedgeTrades} closeTypeLabel={g.closeTypeLabel} outcomes={outcomes} />}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
