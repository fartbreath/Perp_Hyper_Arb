/**
 * ModelAgent — ML-04 shadow-mode decision log.
 *
 * Shows:
 *   - Status card: RUNNING / DISABLED / ERROR, total decisions, pending outcomes
 *   - Agreement rate card: % agreement (last 20), coloured green/amber/red
 *   - Decision type tabs: All / Entry / Exit
 *   - Shadow decisions table: last 50 rows, newest first
 */
import { useState } from "react";
import { useModelStatus, useModelShadowLog } from "../api/client";
import type { ModelStatusData, ShadowLogRow } from "../api/client";

// ── Helpers ───────────────────────────────────────────────────────────────────

function relativeTime(ts: string | number | null): string {
  if (ts === null || ts === undefined) return "—";
  const secs = Math.floor(Date.now() / 1000) - Number(ts);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function pct(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

// ── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: ModelStatusData["status"] | string }) {
  const colours: Record<string, { bg: string; fg: string }> = {
    RUNNING:  { bg: "#166534", fg: "#dcfce7" },
    DISABLED: { bg: "#374151", fg: "#d1d5db" },
    ERROR:    { bg: "#991b1b", fg: "#fee2e2" },
  };
  const c = colours[status] ?? colours.DISABLED;
  return (
    <span
      style={{
        background: c.bg,
        color: c.fg,
        padding: "3px 10px",
        borderRadius: 4,
        fontSize: 13,
        fontFamily: "monospace",
        fontWeight: 600,
      }}
    >
      {status}
    </span>
  );
}

// ── Agreement rate card ───────────────────────────────────────────────────────

function AgreementCard({ rate }: { rate: number | null }) {
  if (rate === null) {
    return (
      <div className="card" style={{ minWidth: 180 }}>
        <div className="card-label">Agreement Rate (last 20)</div>
        <div className="card-value" style={{ color: "#9ca3af" }}>—</div>
        <div style={{ fontSize: 12, color: "#6b7280" }}>need 20+ decisions</div>
      </div>
    );
  }
  const colour = rate >= 0.7 ? "#22c55e" : rate >= 0.5 ? "#f59e0b" : "#ef4444";
  return (
    <div className="card" style={{ minWidth: 180 }}>
      <div className="card-label">Agreement Rate (last 20)</div>
      <div className="card-value" style={{ color: colour, fontSize: 32, fontWeight: 700 }}>
        {pct(rate)}
      </div>
      <div style={{ fontSize: 12, color: "#6b7280" }}>
        {rate >= 0.7 ? "✓ Model aligns with rules" : rate >= 0.5 ? "⚠ Moderate divergence" : "✗ High divergence"}
      </div>
    </div>
  );
}

// ── Decision type tabs ────────────────────────────────────────────────────────

const TABS = ["all", "entry", "exit"] as const;
type Tab = (typeof TABS)[number];

function TabBar({ active, onChange }: { active: Tab; onChange: (t: Tab) => void }) {
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
      {TABS.map((t) => (
        <button
          key={t}
          onClick={() => onChange(t)}
          style={{
            padding: "4px 14px",
            borderRadius: 4,
            border: "1px solid #374151",
            background: active === t ? "#1d4ed8" : "#1f2937",
            color: active === t ? "#fff" : "#9ca3af",
            cursor: "pointer",
            fontSize: 13,
            fontWeight: active === t ? 600 : 400,
          }}
        >
          {t.charAt(0).toUpperCase() + t.slice(1)}
        </button>
      ))}
    </div>
  );
}

// ── Shadow log table ──────────────────────────────────────────────────────────

function outcomeColour(outcome: string): string {
  if (outcome === "WIN") return "#22c55e";
  if (outcome === "LOSS") return "#ef4444";
  return "#6b7280"; // PENDING
}

function agreedIcon(agreed: string): string {
  return agreed.toLowerCase() === "true" ? "✓" : "✗";
}

function agreedColour(agreed: string): string {
  return agreed.toLowerCase() === "true" ? "#22c55e" : "#f59e0b";
}

function ShadowTable({ rows }: { rows: ShadowLogRow[] }) {
  if (rows.length === 0) {
    return (
      <div style={{ color: "#6b7280", padding: "24px 0", textAlign: "center" }}>
        No shadow decisions yet.
      </div>
    );
  }

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Market</th>
            <th>Type</th>
            <th>Rules</th>
            <th>Model A</th>
            <th>Model B</th>
            <th>Decision</th>
            <th>Agreed</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const isDisagreement = r.agreed.toLowerCase() === "false";
            const isPending = r.actual_outcome === "PENDING";
            const rowStyle: React.CSSProperties = {
              opacity: isPending ? 0.6 : 1,
              background: isDisagreement ? "rgba(245,158,11,0.08)" : undefined,
            };
            return (
              <tr key={i} style={rowStyle}>
                <td style={{ color: "#9ca3af", whiteSpace: "nowrap" }}>
                  {relativeTime(r.timestamp)}
                </td>
                <td
                  style={{
                    fontFamily: "monospace",
                    fontSize: 12,
                    maxWidth: 120,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={r.market_id}
                >
                  {r.market_id.slice(0, 12)}…
                </td>
                <td>
                  <span
                    style={{
                      background: r.decision_type === "entry" ? "#1e3a5f" : "#2d1b47",
                      color: r.decision_type === "entry" ? "#93c5fd" : "#c4b5fd",
                      padding: "1px 6px",
                      borderRadius: 3,
                      fontSize: 11,
                      fontFamily: "monospace",
                    }}
                  >
                    {r.decision_type}
                  </span>
                </td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{r.rules_decision}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12, color: "#d1d5db" }}>
                  {r.model_a_score || "—"}
                </td>
                <td style={{ fontFamily: "monospace", fontSize: 12, color: "#d1d5db" }}>
                  {r.model_b_score ? Number(r.model_b_score).toFixed(2) : "—"}
                </td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{r.model_decision}</td>
                <td style={{ color: agreedColour(r.agreed), fontWeight: 700 }}>
                  {agreedIcon(r.agreed)}
                </td>
                <td style={{ color: outcomeColour(r.actual_outcome), fontFamily: "monospace", fontSize: 12 }}>
                  {r.actual_outcome}
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

export default function ModelAgentPage() {
  const [tab, setTab] = useState<Tab>("all");
  const { data: statusData } = useModelStatus();
  const { data: shadowData } = useModelShadowLog(tab);

  const status = statusData ?? {
    enabled: false,
    status: "DISABLED" as const,
    last_decision_ts: null,
    agreement_rate_last_20: null,
    total_decisions: 0,
    pending_outcomes: 0,
  };

  return (
    <div className="page">
      <h1 className="page-title">Model Agent</h1>
      <p style={{ color: "#9ca3af", marginBottom: 20, fontSize: 14 }}>
        ML-04 — shadow-mode decision log. The model observes every bot decision
        without affecting live trades or positions.
      </p>

      {/* Disabled banner */}
      {!status.enabled && (
        <div
          style={{
            background: "#1f2937",
            border: "1px solid #374151",
            borderRadius: 6,
            padding: "12px 16px",
            marginBottom: 20,
            color: "#9ca3af",
            fontSize: 14,
          }}
        >
          ModelAgent is disabled. Set <code>MODEL_AGENT_ENABLED = True</code> in{" "}
          <code>config.py</code> to enable shadow logging.
        </div>
      )}

      {/* Status cards — always show Status badge; Agreement card and table only when enabled */}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 24 }}>
        <div className="card" style={{ minWidth: 220 }}>
          <div className="card-label">Status</div>
          <div style={{ marginTop: 6 }}>
            <StatusBadge status={status.status} />
          </div>
          <div style={{ fontSize: 12, color: "#6b7280", marginTop: 6 }}>
            Last decision: {relativeTime(status.last_decision_ts)}
          </div>
        </div>

        {status.enabled && <AgreementCard rate={status.agreement_rate_last_20} />}

        {status.enabled && (
          <div className="card" style={{ minWidth: 160 }}>
            <div className="card-label">Total Decisions</div>
            <div className="card-value">{status.total_decisions.toLocaleString()}</div>
          </div>
        )}

        {status.enabled && (
          <div className="card" style={{ minWidth: 160 }}>
            <div className="card-label">Pending Outcomes</div>
            <div className="card-value" style={{ color: status.pending_outcomes > 0 ? "#f59e0b" : "#9ca3af" }}>
              {status.pending_outcomes}
            </div>
          </div>
        )}
      </div>

      {/* Shadow log table (only shown when enabled) */}
      {status.enabled && (
        <>
          <TabBar active={tab} onChange={setTab} />
          <div style={{ color: "#6b7280", fontSize: 12, marginBottom: 8 }}>
            {shadowData ? `${shadowData.total} total decisions` : "Loading…"}
          </div>
          <ShadowTable rows={shadowData?.rows ?? []} />
        </>
      )}
    </div>
  );
}
