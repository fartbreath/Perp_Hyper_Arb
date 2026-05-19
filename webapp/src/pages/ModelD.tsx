/**
 * ModelD.tsx — ML-D4 Config Policy Simulator
 *
 * Shows:
 *  - "Waiting for data" progress state when model not yet trained
 *  - Recommendation table per (vol_regime, underlying) when trained
 *  - Recent shadow decisions log from /model/d/log
 */

import { useModelDRecommendations, useModelDLog } from "../api/client";

// ─── helpers ────────────────────────────────────────────────────────────────

function fmtDelta(v: number | null | undefined): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(4)}`;
}

function deltaColor(v: number | null | undefined): string {
  if (v == null) return "#64748b";
  if (v > 0.01) return "#4ade80";  // loosen → green
  if (v < -0.01) return "#f87171"; // tighten → red
  return "#94a3b8";                // ~0 → gray
}

function fmtTs(ts: string): string {
  const n = parseFloat(ts);
  if (isNaN(n)) return ts;
  return new Date(n * 1000).toLocaleTimeString();
}

// ─── Progress bar ─────────────────────────────────────────────────────────────

interface ProgressBarProps {
  current: number;
  target: number;
}

function ProgressBar({ current, target }: ProgressBarProps) {
  const pct = Math.min((current / target) * 100, 100);
  return (
    <div>
      <div className="flex justify-between text-xs mb-1" style={{ color: "#64748b" }}>
        <span>{current.toLocaleString()} signal events</span>
        <span>Target: {target.toLocaleString()}</span>
      </div>
      <div className="rounded-full h-2 overflow-hidden" style={{ background: "#334155" }}>
        <div
          className="h-2 rounded-full transition-all"
          style={{ width: `${pct}%`, background: "#f59e0b" }}
        />
      </div>
      <div className="text-xs mt-1" style={{ color: "#64748b" }}>
        {pct.toFixed(1)}% — estimated ~July 2026 for sufficient coverage
      </div>
    </div>
  );
}

// ─── Recommendation table ────────────────────────────────────────────────────

function RecommendationTable({
  rows,
}: {
  rows: { vol_regime: string; underlying: string; n: number; delta_z_score: number | null; delta_kelly: number | null; delta_delta_sl: number | null }[];
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm" style={{ color: "#64748b" }}>
        No recommendations generated yet (model trained but no context groups found).
      </p>
    );
  }

  return (
    <div className="rounded-lg overflow-hidden" style={{ border: "1px solid #334155" }}>
      <table className="w-full text-xs">
        <thead style={{ background: "#0f172a", color: "#64748b" }}>
          <tr>
            <th className="text-left p-2">Vol Regime</th>
            <th className="text-left p-2">Underlying</th>
            <th className="text-right p-2">N rows</th>
            <th className="text-right p-2">Δ Z-Score</th>
            <th className="text-right p-2">Δ Kelly</th>
            <th className="text-right p-2">Δ Stop-Loss</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={i}
              style={{ background: i % 2 === 0 ? "#1e293b" : "#0f172a", color: "#cbd5e1" }}
            >
              <td className="p-2">{r.vol_regime}</td>
              <td className="p-2">{r.underlying}</td>
              <td className="text-right p-2" style={{ color: "#64748b" }}>{r.n}</td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(r.delta_z_score) }}>
                {fmtDelta(r.delta_z_score)}
              </td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(r.delta_kelly) }}>
                {fmtDelta(r.delta_kelly)}
              </td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(r.delta_delta_sl) }}>
                {fmtDelta(r.delta_delta_sl)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Log table ───────────────────────────────────────────────────────────────

function DLogTable({
  rows,
}: {
  rows: { timestamp: string; market_id: string; market_type: string; delta_z_score: string; delta_kelly: string; delta_sl: string }[];
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm" style={{ color: "#64748b" }}>No shadow decisions logged yet.</p>
    );
  }

  return (
    <div className="rounded-lg overflow-hidden" style={{ border: "1px solid #334155" }}>
      <table className="w-full text-xs">
        <thead style={{ background: "#0f172a", color: "#64748b" }}>
          <tr>
            <th className="text-left p-2">Time</th>
            <th className="text-left p-2">Market</th>
            <th className="text-left p-2">Type</th>
            <th className="text-right p-2">Δ Z</th>
            <th className="text-right p-2">Δ Kelly</th>
            <th className="text-right p-2">Δ SL</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={i}
              style={{ background: i % 2 === 0 ? "#1e293b" : "#0f172a", color: "#cbd5e1" }}
            >
              <td className="p-2 tabular-nums" style={{ color: "#64748b" }}>{fmtTs(r.timestamp)}</td>
              <td className="p-2 font-mono text-xs">{r.market_id.slice(0, 20)}</td>
              <td className="p-2" style={{ color: "#94a3b8" }}>{r.market_type}</td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(parseFloat(r.delta_z_score)) }}>
                {r.delta_z_score}
              </td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(parseFloat(r.delta_kelly)) }}>
                {r.delta_kelly}
              </td>
              <td className="text-right p-2 font-mono" style={{ color: deltaColor(parseFloat(r.delta_sl)) }}>
                {r.delta_sl}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export default function ModelDPage() {
  const { data: recs, error: recsErr, loading: recsLoading, refresh: recsRefresh } = useModelDRecommendations();
  const { data: log } = useModelDLog(20);

  return (
    <div className="p-4 space-y-5" style={{ color: "#cbd5e1", background: "#0f172a", minHeight: "100vh" }}>
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: "#f1f5f9" }}>
            Model D — Config Policy Simulator
          </h1>
          <p className="text-sm mt-0.5" style={{ color: "#64748b" }}>
            ML-D4 · recommended z_score / kelly / stop-loss deltas per regime · simulator mode only
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span
            className="text-xs px-2 py-1 rounded font-medium"
            style={{ background: "#451a03", color: "#fbbf24", border: "1px solid #92400e" }}
          >
            Simulator mode only
          </span>
          <button
            onClick={recsRefresh}
            className="text-xs px-3 py-1.5 rounded font-medium"
            style={{ background: "#1e293b", color: "#94a3b8", border: "1px solid #334155" }}
          >
            Refresh
          </button>
        </div>
      </div>

      {recsLoading && !recs && (
        <div className="text-sm" style={{ color: "#64748b" }}>Loading…</div>
      )}
      {recsErr && (
        <div className="text-sm p-3 rounded" style={{ background: "#450a0a", color: "#fca5a5", border: "1px solid #991b1b" }}>
          Error: {recsErr}
        </div>
      )}

      {/* Not yet trained — waiting for data */}
      {recs && !recs.exists && (
        <div className="rounded-lg p-5 space-y-4" style={{ background: "#1e293b", border: "1px solid #334155" }}>
          <div className="flex items-center gap-2">
            <span style={{ color: "#f59e0b", fontSize: 20 }}>⏳</span>
            <span className="text-base font-medium" style={{ color: "#fbbf24" }}>
              Waiting for training data
            </span>
          </div>
          <p className="text-sm" style={{ color: "#94a3b8" }}>
            Model D needs ~10,000 momentum signal events with resolved outcomes to produce
            meaningful config policy recommendations. Data accumulates automatically as the bot
            trades.
          </p>
          <ProgressBar
            current={recs.n_signal_events ?? 0}
            target={recs.target_signal_events ?? 10000}
          />
        </div>
      )}

      {/* Model exists — show recommendations */}
      {recs?.exists && (
        <>
          {/* Metadata card */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { label: "Rows Trained", value: recs.n_rows_trained?.toLocaleString() ?? "—" },
              {
                label: "Trained At",
                value: recs.trained_at
                  ? new Date(recs.trained_at * 1000).toLocaleDateString()
                  : "—",
              },
              { label: "Dimensions", value: recs.dimensions?.join(", ") ?? "—" },
              { label: "Regime Groups", value: recs.recommendations?.length.toString() ?? "—" },
            ].map(({ label, value }) => (
              <div
                key={label}
                className="rounded-lg p-3"
                style={{ background: "#1e293b", border: "1px solid #334155" }}
              >
                <div className="text-xs" style={{ color: "#64748b" }}>{label}</div>
                <div className="text-sm font-semibold mt-1" style={{ color: "#f1f5f9" }}>{value}</div>
              </div>
            ))}
          </div>

          {/* Recommendation table */}
          <div className="space-y-2">
            <h2 className="text-sm font-medium" style={{ color: "#e2e8f0" }}>
              Config Policy Recommendations
            </h2>
            <p className="text-xs" style={{ color: "#64748b" }}>
              Green = loosen entry/exit threshold · Red = tighten · Near-zero = no change recommended.
              These are simulator-only suggestions — the bot does not act on them.
            </p>
            <RecommendationTable rows={recs.recommendations ?? []} />
          </div>
        </>
      )}

      {/* Shadow decisions log */}
      <div className="space-y-2">
        <h2 className="text-sm font-medium" style={{ color: "#e2e8f0" }}>
          Recent Shadow Decisions
          {log && (
            <span className="ml-2 font-normal text-xs" style={{ color: "#64748b" }}>
              (last 20 of {log.total})
            </span>
          )}
        </h2>
        <DLogTable rows={log?.rows ?? []} />
      </div>
    </div>
  );
}
