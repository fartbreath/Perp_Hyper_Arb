/**
 * ModelPaperTrades — ML-08 independent entry scan results.
 *
 * Shows:
 *   - Summary cards: total, open, closed, model-only win rate vs rules-eligible win rate
 *   - Filter toggle: All trades / Independent-only (would_rules_have_entered=false)
 *   - Table: timestamp, market, score, entry_price, status, pnl, would_rules_entered
 */
import { useState } from "react";
import { useModelPaperTrades } from "../api/client";
import type { PaperTradeRow, PaperTradesData } from "../api/client";

// ── Helpers ───────────────────────────────────────────────────────────────────

function relativeTime(ts: string | number | null): string {
  if (!ts) return "—";
  const secs = Math.floor(Date.now() / 1000) - Number(ts);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtPrice(s: string): string {
  const n = parseFloat(s);
  return isNaN(n) ? "—" : n.toFixed(3);
}

function fmtPnl(s: string): { text: string; color: string } {
  if (!s) return { text: "—", color: "inherit" };
  const n = parseFloat(s);
  if (isNaN(n)) return { text: "—", color: "inherit" };
  return {
    text: n >= 0 ? `+${n.toFixed(4)}` : n.toFixed(4),
    color: n > 0 ? "#4ade80" : n < 0 ? "#f87171" : "inherit",
  };
}

function tteDuration(secs: string): string {
  const n = parseInt(secs, 10);
  if (isNaN(n)) return "—";
  if (n < 60) return `${n}s`;
  if (n < 3600) return `${Math.round(n / 60)}m`;
  if (n < 86400) return `${Math.round(n / 3600)}h`;
  return `${Math.round(n / 86400)}d`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SummaryCard({
  label,
  value,
  sub,
  colour,
}: {
  label: string;
  value: string;
  sub?: string;
  colour?: string;
}) {
  return (
    <div
      style={{
        background: "#1e293b",
        border: "1px solid #334155",
        borderRadius: 8,
        padding: "14px 18px",
        minWidth: 140,
      }}
    >
      <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: colour ?? "#f1f5f9" }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function WouldRulesBadge({ val }: { val: string }) {
  const yes = val === "true";
  return (
    <span
      style={{
        background: yes ? "#1e3a5f" : "#3b2a1a",
        color: yes ? "#93c5fd" : "#fdba74",
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 11,
        fontFamily: "monospace",
      }}
    >
      {yes ? "YES" : "NO"}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colours: Record<string, { bg: string; fg: string }> = {
    proposed: { bg: "#1e3a5f", fg: "#93c5fd" },
    closed:   { bg: "#1e3b2a", fg: "#86efac" },
  };
  const c = colours[status] ?? { bg: "#374151", fg: "#d1d5db" };
  return (
    <span
      style={{
        background: c.bg,
        color: c.fg,
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 11,
        fontFamily: "monospace",
        fontWeight: 600,
      }}
    >
      {status}
    </span>
  );
}

function PaperTradesTable({ rows }: { rows: PaperTradeRow[] }) {
  if (rows.length === 0) {
    return (
      <div style={{ color: "#64748b", padding: "32px 0", textAlign: "center" }}>
        No paper trades yet. Enable MODEL_A_INDEPENDENT_ENABLED in config to start.
      </div>
    );
  }

  const thStyle: React.CSSProperties = {
    textAlign: "left",
    padding: "8px 12px",
    color: "#94a3b8",
    fontSize: 11,
    borderBottom: "1px solid #334155",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };
  const tdStyle: React.CSSProperties = {
    padding: "7px 12px",
    borderBottom: "1px solid #1e293b",
    fontSize: 12,
    whiteSpace: "nowrap",
    verticalAlign: "middle",
  };

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th style={thStyle}>When</th>
            <th style={thStyle}>Market</th>
            <th style={thStyle}>Asset</th>
            <th style={thStyle}>Type</th>
            <th style={thStyle}>TTE</th>
            <th style={thStyle}>Score</th>
            <th style={thStyle}>Entry</th>
            <th style={thStyle}>Exit</th>
            <th style={thStyle}>PnL</th>
            <th style={thStyle}>Status</th>
            <th style={thStyle}>Rules would?</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const pnl = fmtPnl(r.pnl);
            return (
              <tr key={i} style={{ background: i % 2 === 0 ? "#0f172a" : "#111827" }}>
                <td style={{ ...tdStyle, color: "#64748b" }}>{relativeTime(r.timestamp)}</td>
                <td style={{ ...tdStyle, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {r.market_title || r.market_id.slice(0, 16)}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace", color: "#94a3b8" }}>
                  {r.underlying}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace", color: "#64748b", fontSize: 11 }}>
                  {r.market_type}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace" }}>
                  {tteDuration(r.tte_seconds_at_entry)}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace", color: "#fbbf24", fontWeight: 600 }}>
                  {parseFloat(r.model_a_score).toFixed(3)}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace" }}>{fmtPrice(r.entry_price)}</td>
                <td style={{ ...tdStyle, fontFamily: "monospace" }}>
                  {r.exit_price ? fmtPrice(r.exit_price) : "—"}
                </td>
                <td style={{ ...tdStyle, fontFamily: "monospace", color: pnl.color, fontWeight: 600 }}>
                  {pnl.text}
                </td>
                <td style={tdStyle}>
                  <StatusBadge status={r.status} />
                </td>
                <td style={tdStyle}>
                  <WouldRulesBadge val={r.would_rules_have_entered} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function ModelPaperTradesPage() {
  const [independentOnly, setIndependentOnly] = useState(false);
  const { data, loading, error } = useModelPaperTrades(independentOnly);

  const d: PaperTradesData = data ?? {
    rows: [], total: 0, open: 0, closed: 0,
    model_only_win_rate: null, rules_eligible_win_rate: null,
  };

  return (
    <div style={{ padding: "24px 28px", color: "#f1f5f9" }}>
      <h2 style={{ margin: "0 0 6px", fontSize: 20, fontWeight: 700 }}>
        Model Paper Trades
        <span style={{ fontSize: 12, color: "#64748b", marginLeft: 12 }}>ML-08</span>
      </h2>
      <p style={{ margin: "0 0 20px", color: "#64748b", fontSize: 13 }}>
        Entries proposed by the independent entry scan — no rules pre-filters applied.
        <strong style={{ color: "#94a3b8" }}> "Rules would?"=NO</strong> rows are the genuine additive-alpha signal.
      </p>

      {/* Summary cards */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        <SummaryCard label="Total Proposed" value={String(d.total)} />
        <SummaryCard label="Open" value={String(d.open)} colour="#93c5fd" />
        <SummaryCard label="Closed" value={String(d.closed)} />
        <SummaryCard
          label="Model-Only Win Rate"
          value={fmtPct(d.model_only_win_rate)}
          sub="would_rules=NO"
          colour={
            d.model_only_win_rate == null
              ? "#94a3b8"
              : d.model_only_win_rate >= 0.55
              ? "#4ade80"
              : d.model_only_win_rate >= 0.45
              ? "#fbbf24"
              : "#f87171"
          }
        />
        <SummaryCard
          label="Rules-Eligible Win Rate"
          value={fmtPct(d.rules_eligible_win_rate)}
          sub="would_rules=YES"
          colour="#94a3b8"
        />
      </div>

      {/* Filter toggle */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16, alignItems: "center" }}>
        <span style={{ fontSize: 13, color: "#94a3b8" }}>Show:</span>
        {(["all", "independent"] as const).map((v) => {
          const active = independentOnly === (v === "independent");
          return (
            <button
              key={v}
              onClick={() => setIndependentOnly(v === "independent")}
              style={{
                padding: "5px 14px",
                borderRadius: 5,
                border: active ? "1px solid #3b82f6" : "1px solid #334155",
                background: active ? "#1e3a5f" : "#1e293b",
                color: active ? "#93c5fd" : "#94a3b8",
                cursor: "pointer",
                fontSize: 13,
              }}
            >
              {v === "all" ? "All trades" : "Independent only (rules=NO)"}
            </button>
          );
        })}
        {loading && (
          <span style={{ fontSize: 12, color: "#64748b", marginLeft: 8 }}>Refreshing…</span>
        )}
      </div>

      {error && (
        <div style={{ color: "#f87171", marginBottom: 16, fontSize: 13 }}>
          Error loading paper trades: {error}
        </div>
      )}

      {/* Table */}
      <div
        style={{
          background: "#0f172a",
          border: "1px solid #1e293b",
          borderRadius: 8,
          overflow: "hidden",
        }}
      >
        <PaperTradesTable rows={d.rows} />
      </div>

      <div style={{ marginTop: 10, fontSize: 11, color: "#475569" }}>
        Refreshes every 15 s · showing up to 100 rows newest-first ·
        enable MODEL_A_INDEPENDENT_ENABLED to start accumulating data
      </div>
    </div>
  );
}
