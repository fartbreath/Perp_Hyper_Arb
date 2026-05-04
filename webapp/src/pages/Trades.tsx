/**
 * Trades — Accounting ledger view.
 *
 * Reads finalized positions from /acct/ledger (acct_ledger.csv).
 * Groups ON-pair legs together by pair_id.
 * Hedges shown inline under their parent.
 *
 * Columns: Market · Type · Underlying · Side · Entry VWAP · Exit VWAP ·
 *          Contracts · Exit Type · Gross · Fees · Rebates · Net P&L · Status · Time
 */
import { useState, useMemo, Fragment } from "react";
import { useAcctLedger } from "../api/client";
import type { AcctLedgerRow } from "../api/client";
import {
  fmtUsd, fmtPrice, fmtContracts,
  netPnl, grossPnl, pnlColor,
  buildGroups,
} from "./tradesUtils";
export type { LedgerGroup } from "./tradesUtils";

const UNDERLYINGS = ["", "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE", "other"];
const STRATEGIES  = ["", "maker", "mispricing", "momentum", "range", "opening_neutral", "momentum_hedge"];
const OUTCOMES    = [
  { value: "",     label: "All" },
  { value: "WIN",  label: "Win" },
  { value: "LOSS", label: "Loss" },
];
const ALL_LIMIT   = 2000;

// ─── helpers (local, private) ────────────────────────────────────────────────

function relTime(iso: string | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1)  return "now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// ─── badges ──────────────────────────────────────────────────────────────────

function TypeBadge({ type }: { type: string }) {
  const colors: Record<string, string> = {
    bucket_5m: "#1e3a5f", bucket_15m: "#1e3a5f", bucket_1h: "#1e4034",
    bucket_4h: "#3b2b00", bucket_daily: "#3b2b00", milestone: "#3b1a6e",
    range: "#2d3748", opening_neutral: "#1a3a2e", momentum_hedge: "#1a1a2e",
  };
  return (
    <span style={{
      background: colors[type] ?? "#374151", border: "1px solid #4b5563",
      borderRadius: 4, padding: "1px 5px", fontSize: 10, fontWeight: 600,
      color: "#cbd5e1", whiteSpace: "nowrap",
    }}>
      {type.replace("bucket_", "").replace("_", " ").toUpperCase() || "—"}
    </span>
  );
}

function StratBadge({ strat }: { strat: string }) {
  const colors: Record<string, string> = {
    maker: "#14532d", mispricing: "#1e3a8a", momentum: "#312e81",
    opening_neutral: "#065f46", momentum_hedge: "#1a1a2e", range: "#374151",
  };
  return (
    <span style={{
      background: colors[strat] ?? "#374151", border: "1px solid #4b5563",
      borderRadius: 4, padding: "1px 5px", fontSize: 10, fontWeight: 600,
      color: "#e2e8f0", whiteSpace: "nowrap",
    }}>{strat || "—"}</span>
  );
}

function SideBadge({ side }: { side: string }) {
  const yes = side === "YES" || side === "UP";
  const no  = side === "NO"  || side === "DOWN";
  return (
    <span style={{
      background: yes ? "#166534" : no ? "#7f1d1d" : "#374151",
      borderRadius: 4, padding: "1px 5px", fontSize: 10, fontWeight: 700,
      color: "#f0fdf4", whiteSpace: "nowrap",
    }}>{side || "—"}</span>
  );
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  if (!outcome) return null;
  const win = outcome.toUpperCase() === "WIN";
  return (
    <span style={{
      background: win ? "#14532d" : "#7f1d1d",
      border: `1px solid ${win ? "#22c55e" : "#ef4444"}`,
      borderRadius: 4, padding: "1px 6px", fontSize: 10, fontWeight: 700,
      color: win ? "#22c55e" : "#ef4444",
    }}>{win ? "WIN" : "LOSS"}</span>
  );
}

function PmBadge({ confirmed, label }: { confirmed: string; label: string }) {
  const ok = confirmed === "True" || confirmed === "true";
  return (
    <span style={{
      background: ok ? "#052e16" : "#1c1917",
      border: `1px solid ${ok ? "#166534" : "#57534e"}`,
      borderRadius: 3, padding: "1px 4px", fontSize: 9, fontWeight: 600,
      color: ok ? "#86efac" : "#78716c",
    }}>{label}: {ok ? "✓" : "?"}</span>
  );
}

// ─── summary bar ─────────────────────────────────────────────────────────────

function SummaryBar({ groups }: { groups: LedgerGroup[] }) {
  const totalNet   = groups.reduce((s, g) => s + g.totalNetPnl,  0);
  const totalFees  = groups.reduce((s, g) => s + g.totalFees,    0);
  const totalRbts  = groups.reduce((s, g) => s + g.totalRebates, 0);
  const wins       = groups.filter(g => g.totalNetPnl > 0).length;
  const losses     = groups.filter(g => g.totalNetPnl < 0).length;
  const total      = wins + losses;
  const winRate    = total > 0 ? ((wins / total) * 100).toFixed(0) : "—";

  const cell = (label: string, value: string, color?: string) => (
    <div style={{ minWidth: 110 }}>
      <div style={{ fontSize: 10, color: "#64748b", marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 600, color: color ?? "#e2e8f0" }}>{value}</div>
    </div>
  );

  return (
    <div style={{
      display: "flex", gap: 24, flexWrap: "wrap",
      background: "#111827", borderRadius: 8, padding: "12px 20px", marginBottom: 16,
      border: "1px solid #1f2937",
    }}>
      {cell("Net P&L",     `$${totalNet.toFixed(2)}`, pnlColor(totalNet))}
      {cell("Win Rate",    `${winRate}%  (${wins}W / ${losses}L)`,
            Number(winRate) >= 50 ? "#22c55e" : "#94a3b8")}
      {cell("Trades",      String(groups.length))}
      {cell("Total Fees",    `$${totalFees.toFixed(2)}`,  "#ef4444")}
      {cell("Total Rebates", `+$${totalRbts.toFixed(2)}`, "#22c55e")}
    </div>
  );
}

// ─── single row ───────────────────────────────────────────────────────────────

function LedgerRow({ row, indent = false }: { row: AcctLedgerRow; indent?: boolean }) {
  const net   = netPnl(row);
  const gross = grossPnl(row);
  const isHedge = row.fill_type === "HEDGE" || row.strategy === "momentum_hedge";

  return (
    <tr style={{ background: indent ? "#0a0f18" : "#111827", borderBottom: "1px solid #1f2937" }}>
      <td style={{ padding: "8px 12px", paddingLeft: indent ? 32 : 12, minWidth: 200 }}>
        <div style={{ fontSize: 12, color: "#e2e8f0", fontWeight: isHedge ? 400 : 500 }}
          title={row.market_id}>
          {isHedge ? "↳ " : ""}{row.market_title || row.market_id?.slice(0, 20) || "—"}
        </div>
        <div style={{ display: "flex", gap: 4, marginTop: 3, flexWrap: "wrap" }}>
          <TypeBadge type={row.market_type} />
          <StratBadge strat={isHedge ? "hedge" : row.strategy} />
        </div>
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#94a3b8", textAlign: "center" }}>{row.underlying || "—"}</td>
      <td style={{ padding: "8px 8px", textAlign: "center" }}>
        <SideBadge side={isHedge ? "hedge" : row.side} />
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#93c5fd", textAlign: "right", fontFamily: "monospace" }}>
        {fmtPrice(row.entry_vwap)}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#fbbf24", textAlign: "right", fontFamily: "monospace" }}>
        {fmtPrice(row.exit_vwap)}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#cbd5e1", textAlign: "right", fontFamily: "monospace" }}>
        {fmtContracts(row.entry_contracts)}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 11, color: "#94a3b8", textAlign: "center" }}>
        {row.exit_type || "—"}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, textAlign: "right", fontFamily: "monospace",
        color: gross > 0 ? "#86efac" : gross < 0 ? "#fca5a5" : "#94a3b8" }}>
        {fmtUsd(row.gross_pnl)}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#f87171", textAlign: "right", fontFamily: "monospace" }}>
        {Number(row.fees_usd) > 0 ? `-${(Number(row.fees_usd) * 100).toFixed(2)}¢` : "—"}
      </td>
      <td style={{ padding: "8px 8px", fontSize: 12, color: "#4ade80", textAlign: "right", fontFamily: "monospace" }}>
        {Number(row.rebates_usd) > 0 ? `+${(Number(row.rebates_usd) * 100).toFixed(2)}¢` : "—"}
      </td>
      <td style={{ padding: "8px 12px", fontWeight: 700, fontSize: 14, textAlign: "right",
        fontFamily: "monospace", color: pnlColor(net) }}>
        {fmtUsd(row.net_pnl)}
      </td>
      <td style={{ padding: "8px 8px" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: "flex-start" }}>
          <OutcomeBadge outcome={row.resolved_outcome} />
          <div style={{ display: "flex", gap: 3, flexWrap: "wrap" }}>
            <PmBadge confirmed={row.pm_entry_confirmed} label="E" />
            <PmBadge confirmed={row.pm_exit_confirmed}  label="X" />
          </div>
        </div>
      </td>
      <td style={{ padding: "8px 8px", fontSize: 11, color: "#64748b", whiteSpace: "nowrap" }}>
        {relTime(row.exit_time || row.entry_time)}
      </td>
    </tr>
  );
}

// ─── group rows (collapsible) ─────────────────────────────────────────────────

function GroupRows({ group }: { group: LedgerGroup }) {
  const [open, setOpen] = useState(false);
  const isPair = group.rows.length > 1 || group.hedges.length > 0;
  const net = group.totalNetPnl;

  if (!isPair) {
    return (
      <>
        <LedgerRow row={group.rows[0]} />
        {group.hedges.map(h => <LedgerRow key={h.pos_id} row={h} indent />)}
      </>
    );
  }

  const repr = group.rows[0];
  return (
    <>
      <tr onClick={() => setOpen(o => !o)}
        style={{ background: "#0d1117", borderBottom: "1px solid #1f2937", cursor: "pointer" }}>
        <td style={{ padding: "8px 12px", minWidth: 200 }}>
          <div style={{ fontSize: 12, color: "#e2e8f0", fontWeight: 600 }}>
            {open ? "▾" : "▸"} {repr.market_title || repr.market_id?.slice(0, 20) || "—"}
          </div>
          <div style={{ display: "flex", gap: 4, marginTop: 3 }}>
            <TypeBadge type={repr.market_type} />
            <StratBadge strat={repr.strategy} />
            <span style={{ background: "#1e3a5f", borderRadius: 3, padding: "1px 5px", fontSize: 9, color: "#93c5fd" }}>
              {group.rows.length}L{group.hedges.length > 0 ? `+${group.hedges.length}H` : ""}
            </span>
          </div>
        </td>
        <td style={{ padding: "8px 8px", fontSize: 12, color: "#94a3b8", textAlign: "center" }}>{repr.underlying || "—"}</td>
        <td style={{ padding: "8px 8px", textAlign: "center" }}>
          <div style={{ display: "flex", gap: 3, justifyContent: "center" }}>
            {group.rows.map(r => <SideBadge key={r.pos_id} side={r.side} />)}
          </div>
        </td>
        <td colSpan={4} style={{ padding: "8px 8px", fontSize: 11, color: "#64748b", textAlign: "center" }}>
          {group.rows.map(r => `${fmtPrice(r.entry_vwap)} → ${fmtPrice(r.exit_vwap)}`).join("  ·  ")}
        </td>
        <td style={{ padding: "8px 8px", fontSize: 12, textAlign: "right", fontFamily: "monospace",
          color: group.totalGross > 0 ? "#86efac" : "#fca5a5" }}>
          {fmtUsd(group.totalGross)}
        </td>
        <td style={{ padding: "8px 8px", fontSize: 12, color: "#f87171", textAlign: "right", fontFamily: "monospace" }}>
          {group.totalFees > 0 ? `-${(group.totalFees * 100).toFixed(2)}¢` : "—"}
        </td>
        <td style={{ padding: "8px 8px", fontSize: 12, color: "#4ade80", textAlign: "right", fontFamily: "monospace" }}>
          {group.totalRebates > 0 ? `+${(group.totalRebates * 100).toFixed(2)}¢` : "—"}
        </td>
        <td style={{ padding: "8px 12px", fontWeight: 700, fontSize: 14, textAlign: "right",
          fontFamily: "monospace", color: pnlColor(net) }}>
          {fmtUsd(net)}
        </td>
        <td style={{ padding: "8px 8px" }}>
          <OutcomeBadge outcome={
            group.rows.length > 1
              ? (net > 0 ? "WIN" : net < 0 ? "LOSS" : "")
              : (group.rows.find(r => r.resolved_outcome)?.resolved_outcome ?? "")
          } />
        </td>
        <td style={{ padding: "8px 8px", fontSize: 11, color: "#64748b", whiteSpace: "nowrap" }}>
          {relTime(group.lastTime)}
        </td>
      </tr>
      {open && (
        <>
          {group.rows.map(r =>   <LedgerRow key={r.pos_id} row={r}  indent />)}
          {group.hedges.map(h => <LedgerRow key={h.pos_id} row={h}  indent />)}
        </>
      )}
    </>
  );
}

// ─── main component ───────────────────────────────────────────────────────────

export default function Trades() {
  const [underlying, setUnderlying] = useState("");
  const [strategy,   setStrategy]   = useState("");
  const [outcome,    setOutcome]     = useState("");
  const [search,     setSearch]      = useState("");

  const { data, loading, error, refresh } = useAcctLedger(ALL_LIMIT, 0, strategy || undefined, underlying || undefined);
  const rows: AcctLedgerRow[] = data?.rows ?? [];

  const filtered = useMemo(() => {
    let r = rows;
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      r = r.filter(x =>
        (x.market_title ?? "").toLowerCase().includes(q) ||
        (x.market_id   ?? "").toLowerCase().includes(q) ||
        (x.underlying  ?? "").toLowerCase().includes(q),
      );
    }
    return r;
  }, [rows, search]);

  const groups = useMemo(() => {
    let g = buildGroups(filtered);
    if (outcome) {
      const want = outcome.toUpperCase();
      g = g.filter(group => {
        const groupOutcome = group.rows.length > 1
          ? (group.totalNetPnl > 0 ? "WIN" : group.totalNetPnl < 0 ? "LOSS" : "")
          : (group.rows.find(r => r.resolved_outcome)?.resolved_outcome ?? "").toUpperCase();
        return groupOutcome === want;
      });
    }
    return g;
  }, [filtered, outcome]);

  const sel = (value: string, onChange: (v: string) => void, options: { value: string; label: string }[]) => (
    <select value={value} onChange={e => onChange(e.target.value)} style={{
      background: "#1f2937", border: "1px solid #374151", borderRadius: 6,
      color: "#e2e8f0", padding: "6px 10px", fontSize: 12, cursor: "pointer",
    }}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );

  return (
    <div style={{ padding: 20, color: "#e2e8f0", fontFamily: "Inter, system-ui, sans-serif" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>Trades</h1>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "#64748b" }}>
            Accounting ledger · {groups.length} records · source: acct_ledger.csv
          </p>
        </div>
        <button onClick={refresh} style={{
          background: "#1f2937", border: "1px solid #374151", borderRadius: 6,
          color: "#94a3b8", padding: "6px 14px", fontSize: 12, cursor: "pointer",
        }}>Refresh</button>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
        {sel(underlying, setUnderlying, [{ value: "", label: "All Underlyings" }, ...UNDERLYINGS.slice(1).map(u => ({ value: u, label: u }))])}
        {sel(strategy,   setStrategy,   [{ value: "", label: "All Strategies"  }, ...STRATEGIES.slice(1).map(s => ({ value: s, label: s }))])}
        {sel(outcome,    setOutcome,    OUTCOMES)}
        <input placeholder="Search market…" value={search} onChange={e => setSearch(e.target.value)}
          style={{ background: "#1f2937", border: "1px solid #374151", borderRadius: 6,
            color: "#e2e8f0", padding: "6px 10px", fontSize: 12, minWidth: 200 }} />
      </div>

      <SummaryBar groups={groups} />

      {loading && <div style={{ color: "#64748b", padding: 20 }}>Loading…</div>}
      {error   && <div style={{ color: "#ef4444", padding: 20 }}>Error: {error}</div>}

      {!loading && groups.length === 0 && (
        <div style={{ color: "#64748b", padding: 40, textAlign: "center" }}>
          No finalized trades yet. Trades appear here once positions are resolved and recorded in acct_ledger.csv.
        </div>
      )}

      {groups.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: "#0a0f18", borderBottom: "2px solid #1f2937" }}>
                {[
                  ["Market",      "left",   200],
                  ["Underlying",  "center", undefined],
                  ["Side",        "center", undefined],
                  ["Entry VWAP",  "right",  undefined],
                  ["Exit VWAP",   "right",  undefined],
                  ["Contracts",   "right",  undefined],
                  ["Exit Type",   "center", undefined],
                  ["Gross",       "right",  undefined],
                  ["Fees",        "right",  undefined],
                  ["Rebates",     "right",  undefined],
                  ["Net P&L",     "right",  undefined],
                  ["Status",      "center", undefined],
                  ["Time",        "center", undefined],
                ].map(([h, align, minW]) => (
                  <th key={h as string} style={{
                    padding: "8px 8px", fontSize: 10, fontWeight: 600, color: "#64748b",
                    textAlign: align as React.CSSProperties["textAlign"],
                    whiteSpace: "nowrap",
                    ...(minW ? { minWidth: minW } : {}),
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {groups.map(g => <GroupRows key={g.pair_id} group={g} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}