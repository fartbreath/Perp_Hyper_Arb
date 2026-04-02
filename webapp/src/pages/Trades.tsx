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
import { useTrades } from "../api/client";
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
  legs: Trade[];              // individual rows
  totalSize: number;          // USDC deployed across all legs
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
  for (const [mid, legs] of map) {
    const first = legs[0];
    const mtype = first.market_type ?? "";
    if (typeFilter && mtype !== typeFilter) continue;

    // USDC cost basis per leg: price × size (entry_price is the actual token price for both YES and NO)
    const totalSize = legs.reduce((acc, t) => {
      return acc + (isHl(t) ? Number(t.size) * Number(t.price) : Number(t.price) * Number(t.size));
    }, 0);

    const totalGross    = legs.reduce((a, t) => a + gross(t), 0);
    const totalFees     = legs.reduce((a, t) => a + Number(t.fees_paid), 0);
    const totalRebates  = legs.reduce((a, t) => a + Number(t.rebates_earned), 0);
    const totalPnl      = legs.reduce((a, t) => a + Number(t.pnl), 0);

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
  if (label.startsWith("RESOLVED → 1")) bg = "#15803d";
  else if (label.startsWith("RESOLVED → 0")) bg = "#b91c1c";
  else if (label.startsWith("TAKER")) bg = "#ca8a04";
  else if (label.startsWith("PRE-EXPIRY")) bg = "#0284c7";
  return (
    <span style={{
      background: bg, color: "#fff", fontSize: "0.72em", fontWeight: 700,
      padding: "2px 7px", borderRadius: 4, whiteSpace: "nowrap",
    }}>{label}</span>
  );
}

/** One badge per leg: "YES 19.0ct@0.54" */
function LegsBadges({ legs }: { legs: Trade[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {legs.map((t, i) => {
        const ct   = Number(t.size).toFixed(1);
        // entry_price is the actual token fill price for both YES and NO.
        const px   = Number(t.price).toFixed(3);
        const bg   = t.side === "YES" ? "#0c4a6e" : "#4a1d0c";
        const col  = t.side === "YES" ? "#38bdf8" : "#fb923c";
        const score = t.signal_score ? ` ·${Number(t.signal_score).toFixed(0)}` : "";
        return (
          <span key={i} style={{
            background: bg, color: col, fontSize: "0.75em",
            fontWeight: 600, padding: "2px 6px", borderRadius: 4,
            whiteSpace: "nowrap",
          }}>
            {t.side} {ct}ct @ {px}{score}
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

// ── Expanded leg detail ───────────────────────────────────────────────────────

function LegDetail({ legs }: { legs: Trade[] }) {
  return (
    <tr>
      <td colSpan={11} style={{ padding: "6px 16px 10px 32px", background: "rgba(0,0,0,0.25)" }}>
        <table style={{ width: "100%", fontSize: "0.8em", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: "#94a3b8", borderBottom: "1px solid #334155" }}>
              <th style={{ textAlign: "left",  padding: "3px 8px" }}>Time</th>
              <th style={{ textAlign: "left",  padding: "3px 8px" }}>Side</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Contracts</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Entry</th>
              <th style={{ textAlign: "right", padding: "3px 8px" }}>Exit (calc)</th>
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
              return (
                <tr key={i} style={{ borderBottom: "1px solid #1e293b" }}>
                  <td style={{ padding: "3px 8px", color: "#64748b" }}>{fmtTs(t.timestamp)}</td>
                  <td style={{ padding: "3px 8px" }}>
                    <span style={{
                      color: t.side === "YES" ? "#38bdf8" : "#fb923c",
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

  const groups = useMemo(() => {
    if (!data?.trades) return [];
    return aggregateToMarkets(data.trades, typeFilter);
  }, [data, typeFilter]);

  function toggleExpand(mid: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(mid) ? next.delete(mid) : next.add(mid);
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
                      <td><TypeBadge t={g.market_type} /></td>

                      {/* underlying */}
                      <td style={{ fontWeight: 600 }}>{g.underlying}</td>

                      {/* legs */}
                      <td><LegsBadges legs={g.legs} /></td>

                      {/* close type */}
                      <td><CloseBadge label={g.closeTypeLabel} /></td>

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
                    {isOpen && <LegDetail legs={g.legs} />}
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
