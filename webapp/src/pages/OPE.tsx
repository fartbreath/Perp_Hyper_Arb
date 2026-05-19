/**
 * OPE Reward Surface — ML-D3
 *
 * Off-policy evaluation: expected PnL per trade as a function of config
 * parameter, faceted by vol_regime and underlying.
 *
 * Fetches GET /ope/surface?vol_regime=&underlying= on load.
 * No live polling — batch analysis output only.
 */

import { useEffect, useState } from "react";
import { BASE_URL } from "../api/client";

// ── Types ─────────────────────────────────────────────────────────────────────

interface ZPoint {
  z_threshold: number;
  n: number;
  mean_pnl: number | null;
  win_rate: number | null;
  total_pnl: number | null;
  low_confidence: boolean;
}

interface SLPoint {
  sl_threshold_pct: number;
  n: number;
  tp?: number;
  fp?: number;
  tn?: number;
  fn?: number;
  fp_rate: number | null;
  low_confidence: boolean;
  note?: string;
}

interface KellyPoint {
  kelly_multiplier: number;
  n: number;
  mean_pnl: number | null;
  win_rate: number | null;
  total_pnl: number | null;
  low_confidence: boolean;
}

interface OptimalEntry {
  optimal_value?: number;
  optimal_mean_pnl?: number;
  n_at_optimal?: number;
  live_value?: number;
  delta_from_live?: number | null;
  note?: string;
}

interface OPESurface {
  z_score: ZPoint[];
  delta_sl: SLPoint[];
  kelly: KellyPoint[];
  optimal: { z_score: OptimalEntry; delta_sl: OptimalEntry; kelly: OptimalEntry };
  meta: {
    vol_regime: string;
    underlying: string;
    n_total_trades: number;
    data_source: string;
    has_signal_events: boolean;
    n_signal_events: number;
    signal_events_note: string | null;
    generated_at: string;
  };
}

// ── Mini chart helpers ─────────────────────────────────────────────────────────

const CHART_W = 420;
const CHART_H = 140;
const PAD = { top: 12, right: 16, bottom: 32, left: 52 };
const INNER_W = CHART_W - PAD.left - PAD.right;
const INNER_H = CHART_H - PAD.top - PAD.bottom;

function lerp(v: number, srcMin: number, srcMax: number, dstMin: number, dstMax: number): number {
  if (srcMax === srcMin) return (dstMin + dstMax) / 2;
  return dstMin + ((v - srcMin) / (srcMax - srcMin)) * (dstMax - dstMin);
}

function ticks(min: number, max: number, count = 4): number[] {
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, i) => min + i * step);
}

interface LineChartProps {
  points: { x: number; y: number; lowConf: boolean; n: number }[];
  liveX?: number;
  xLabel: string;
  yLabel: string;
  yFmt?: (v: number) => string;
  xFmt?: (v: number) => string;
}

function LineChart({ points, liveX, xLabel, yLabel, yFmt, xFmt }: LineChartProps) {
  if (points.length === 0) {
    return (
      <div style={{ width: CHART_W, height: CHART_H, display: "flex", alignItems: "center", justifyContent: "center", color: "#64748b", fontSize: 12 }}>
        No data
      </div>
    );
  }
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yRaw = ys.filter((y) => isFinite(y));
  const yMin = yRaw.length ? Math.min(...yRaw) : -0.01;
  const yMax = yRaw.length ? Math.max(...yRaw) : 0.01;
  const yPad = Math.max((yMax - yMin) * 0.1, 0.001);

  const toSvgX = (v: number) => lerp(v, xMin, xMax, PAD.left, PAD.left + INNER_W);
  const toSvgY = (v: number) => lerp(v, yMin - yPad, yMax + yPad, PAD.top + INNER_H, PAD.top);

  const solidPts = points.filter((p) => !p.lowConf && isFinite(p.y));
  const dashPts = points.filter((p) => p.lowConf && isFinite(p.y));

  const toPathD = (pts: typeof points) =>
    pts.map((p, i) => `${i === 0 ? "M" : "L"}${toSvgX(p.x).toFixed(1)},${toSvgY(p.y).toFixed(1)}`).join(" ");

  const zero = toSvgY(0);
  const fmt = yFmt ?? ((v: number) => v.toFixed(4));
  const xfmt = xFmt ?? ((v: number) => v.toFixed(1));

  const yTickVals = ticks(yMin, yMax, 4);
  const xTickVals = ticks(xMin, xMax, 5);

  return (
    <svg width={CHART_W} height={CHART_H} style={{ overflow: "visible" }}>
      {/* zero line */}
      {zero > PAD.top && zero < PAD.top + INNER_H && (
        <line x1={PAD.left} y1={zero} x2={PAD.left + INNER_W} y2={zero} stroke="#475569" strokeWidth={0.5} />
      )}
      {/* y grid + labels */}
      {yTickVals.map((v) => {
        const sy = toSvgY(v);
        return (
          <g key={v}>
            <line x1={PAD.left} y1={sy} x2={PAD.left + INNER_W} y2={sy} stroke="#1e293b" strokeWidth={0.5} />
            <text x={PAD.left - 4} y={sy + 4} textAnchor="end" fill="#94a3b8" fontSize={9}>{fmt(v)}</text>
          </g>
        );
      })}
      {/* x ticks */}
      {xTickVals.map((v) => {
        const sx = toSvgX(v);
        return (
          <g key={v}>
            <line x1={sx} y1={PAD.top + INNER_H} x2={sx} y2={PAD.top + INNER_H + 3} stroke="#475569" strokeWidth={0.5} />
            <text x={sx} y={PAD.top + INNER_H + 12} textAnchor="middle" fill="#94a3b8" fontSize={9}>{xfmt(v)}</text>
          </g>
        );
      })}
      {/* live config vertical line */}
      {liveX !== undefined && (
        <line
          x1={toSvgX(liveX)} y1={PAD.top}
          x2={toSvgX(liveX)} y2={PAD.top + INNER_H}
          stroke="#f59e0b" strokeWidth={1} strokeDasharray="4 2"
        />
      )}
      {/* solid path */}
      {solidPts.length > 1 && (
        <path d={toPathD(solidPts)} fill="none" stroke="#38bdf8" strokeWidth={1.5} />
      )}
      {/* dashed path for low-confidence */}
      {dashPts.length > 1 && (
        <path d={toPathD(dashPts)} fill="none" stroke="#38bdf8" strokeWidth={1} strokeDasharray="4 2" opacity={0.5} />
      )}
      {/* dots */}
      {points.filter((p) => isFinite(p.y)).map((p) => (
        <circle
          key={p.x}
          cx={toSvgX(p.x)} cy={toSvgY(p.y)} r={p.lowConf ? 2 : 3}
          fill={p.lowConf ? "#475569" : "#38bdf8"}
          stroke="#0f172a" strokeWidth={0.5}
        >
          <title>{`${xfmt(p.x)}: ${fmt(p.y)} (n=${p.n})`}</title>
        </circle>
      ))}
      {/* axis labels */}
      <text x={PAD.left + INNER_W / 2} y={CHART_H - 2} textAnchor="middle" fill="#64748b" fontSize={9}>{xLabel}</text>
      <text
        x={10} y={PAD.top + INNER_H / 2}
        textAnchor="middle" fill="#64748b" fontSize={9}
        transform={`rotate(-90, 10, ${PAD.top + INNER_H / 2})`}
      >{yLabel}</text>
    </svg>
  );
}

// ── Optimal table ──────────────────────────────────────────────────────────────

function OptimalTable({ optimal }: { optimal: OPESurface["optimal"] }) {
  const rows: { dim: string; live: number | undefined; opt: number | undefined; delta: number | null | undefined; note?: string }[] = [
    {
      dim: "Z-Score Threshold",
      live: optimal.z_score.live_value,
      opt: optimal.z_score.optimal_value,
      delta: optimal.z_score.delta_from_live,
    },
    {
      dim: "Delta SL %",
      live: optimal.delta_sl.live_value,
      opt: optimal.delta_sl.optimal_value,
      delta: null,
      note: optimal.delta_sl.note,
    },
    {
      dim: "Kelly Fraction",
      live: optimal.kelly.live_value,
      opt: optimal.kelly.optimal_value,
      delta: optimal.kelly.delta_from_live,
    },
  ];

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ color: "#94a3b8", borderBottom: "1px solid #334155" }}>
          <th style={{ textAlign: "left", padding: "4px 8px" }}>Parameter</th>
          <th style={{ textAlign: "right", padding: "4px 8px" }}>Live</th>
          <th style={{ textAlign: "right", padding: "4px 8px" }}>Optimal</th>
          <th style={{ textAlign: "right", padding: "4px 8px" }}>Δ</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const deltaColor = row.delta == null ? "#94a3b8" : row.delta > 0 ? "#4ade80" : row.delta < 0 ? "#f87171" : "#94a3b8";
          return (
            <tr key={row.dim} style={{ borderBottom: "1px solid #1e293b" }}>
              <td style={{ padding: "6px 8px" }}>{row.dim}</td>
              <td style={{ textAlign: "right", padding: "6px 8px", color: "#f59e0b" }}>
                {row.live != null ? row.live : "—"}
              </td>
              <td style={{ textAlign: "right", padding: "6px 8px", color: "#38bdf8" }}>
                {row.opt != null ? row.opt : "—"}
              </td>
              <td style={{ textAlign: "right", padding: "6px 8px", color: deltaColor }}>
                {row.delta != null ? (row.delta >= 0 ? "+" : "") + row.delta.toFixed(3) : row.note ? <span style={{ fontSize: 11, color: "#64748b" }}>{row.note}</span> : "—"}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────────

const VOL_REGIMES = ["ALL", "LOW", "NORMAL", "HIGH"];
const UNDERLYINGS = ["ALL", "BTC", "ETH", "SOL"];

export default function OPEPage() {
  const [volRegime, setVolRegime] = useState<string>("ALL");
  const [underlying, setUnderlying] = useState<string>("ALL");
  const [surface, setSurface] = useState<OPESurface | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function fetchSurface(vr: string, ul: string) {
    setLoading(true);
    setError(null);
    fetch(`${BASE_URL}/ope/surface?vol_regime=${encodeURIComponent(vr)}&underlying=${encodeURIComponent(ul)}`)
      .then((r) => {
        if (!r.ok) return r.json().then((e) => { throw new Error(e.detail ?? r.statusText); });
        return r.json();
      })
      .then((data: OPESurface) => {
        setSurface(data);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }

  useEffect(() => {
    fetchSurface(volRegime, underlying);
  }, [volRegime, underlying]);

  // ── Z-score chart data ────────────────────────────────────────────────────
  const zPoints = surface?.z_score
    .filter((p) => p.mean_pnl != null)
    .map((p) => ({ x: p.z_threshold, y: p.mean_pnl as number, lowConf: p.low_confidence, n: p.n })) ?? [];

  const slPoints = surface?.delta_sl
    .filter((p) => p.fp_rate != null)
    .map((p) => ({ x: p.sl_threshold_pct, y: p.fp_rate as number, lowConf: p.low_confidence, n: p.n })) ?? [];

  const kellyPoints = surface?.kelly
    .filter((p) => p.mean_pnl != null)
    .map((p) => ({ x: p.kelly_multiplier, y: p.mean_pnl as number, lowConf: p.low_confidence, n: p.n })) ?? [];

  const hasSLTickData = surface ? !surface.delta_sl.some((p) => p.note === "tick_data_unavailable") : true;

  return (
    <div style={{ padding: "1.5rem", maxWidth: 1100 }}>
      <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "0.5rem", flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: "1.25rem" }}>OPE Reward Surface</h2>
        <span style={{ fontSize: 12, color: "#64748b", background: "#1e293b", padding: "2px 8px", borderRadius: 4 }}>
          ML-D3 · off-policy evaluation
        </span>
        <button
          onClick={() => fetchSurface(volRegime, underlying)}
          disabled={loading}
          style={{
            marginLeft: "auto", padding: "4px 12px", fontSize: 12,
            background: "#1e3a5f", color: "#93c5fd", border: "1px solid #2563eb",
            borderRadius: 4, cursor: loading ? "wait" : "pointer",
          }}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      <p style={{ color: "#64748b", fontSize: 13, margin: "0 0 1rem 0" }}>
        Expected PnL per trade as a function of config parameter value. Dashed points have &lt;{20} samples.
        Amber vertical line = current live config.
      </p>

      {/* Facet selectors */}
      <div style={{ display: "flex", gap: "1.5rem", marginBottom: "1.5rem", flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <span style={{ fontSize: 13, color: "#94a3b8" }}>Vol Regime:</span>
          {VOL_REGIMES.map((r) => (
            <button
              key={r}
              onClick={() => setVolRegime(r)}
              style={{
                padding: "3px 10px", fontSize: 12, borderRadius: 4, cursor: "pointer",
                background: volRegime === r ? "#1e40af" : "#1e293b",
                color: volRegime === r ? "#93c5fd" : "#94a3b8",
                border: `1px solid ${volRegime === r ? "#3b82f6" : "#334155"}`,
              }}
            >
              {r}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <span style={{ fontSize: 13, color: "#94a3b8" }}>Underlying:</span>
          {UNDERLYINGS.map((u) => (
            <button
              key={u}
              onClick={() => setUnderlying(u)}
              style={{
                padding: "3px 10px", fontSize: 12, borderRadius: 4, cursor: "pointer",
                background: underlying === u ? "#1e40af" : "#1e293b",
                color: underlying === u ? "#93c5fd" : "#94a3b8",
                border: `1px solid ${underlying === u ? "#3b82f6" : "#334155"}`,
              }}
            >
              {u}
            </button>
          ))}
        </div>
      </div>

      {/* Signal events banner */}
      {surface?.meta?.signal_events_note && (
        <div style={{
          marginBottom: "1rem", padding: "8px 12px", borderRadius: 6,
          background: "#1c1917", border: "1px solid #78350f",
          color: "#fbbf24", fontSize: 12,
        }}>
          ⚠ {surface.meta.signal_events_note}
        </div>
      )}

      {error && (
        <div style={{ marginBottom: "1rem", padding: "8px 12px", borderRadius: 6, background: "#1f0a0a", border: "1px solid #7f1d1d", color: "#fca5a5", fontSize: 13 }}>
          Error: {error}
        </div>
      )}

      {loading && !surface && (
        <div style={{ color: "#64748b", fontSize: 13 }}>Computing reward surface…</div>
      )}

      {surface && (
        <>
          {/* Meta strip */}
          <div style={{ display: "flex", gap: "1.5rem", marginBottom: "1.5rem", flexWrap: "wrap", fontSize: 12, color: "#64748b" }}>
            <span>Trades in facet: <strong style={{ color: "#94a3b8" }}>{surface.meta.n_total_trades}</strong></span>
            <span>Data source: <strong style={{ color: "#94a3b8" }}>{surface.meta.data_source}</strong></span>
            <span>Signal events: <strong style={{ color: surface.meta.has_signal_events ? "#4ade80" : "#f87171" }}>{surface.meta.n_signal_events.toLocaleString()}</strong></span>
            <span style={{ marginLeft: "auto" }}>Generated: {new Date(surface.meta.generated_at).toLocaleTimeString()}</span>
          </div>

          {/* Charts grid */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: "1.5rem", marginBottom: "1.5rem" }}>
            {/* Z-Score chart */}
            <div className="card" style={{ padding: "1rem", minWidth: CHART_W + 32 }}>
              <h4 style={{ margin: "0 0 0.75rem 0", fontSize: 13 }}>Z-Score Entry Threshold → Mean PnL/trade</h4>
              <LineChart
                points={zPoints}
                liveX={surface.optimal.z_score.live_value}
                xLabel="z_score_threshold"
                yLabel="mean PnL"
                yFmt={(v) => v.toFixed(4)}
              />
              {zPoints.length === 0 && <p style={{ color: "#64748b", fontSize: 12, margin: "0.5rem 0 0" }}>No z-score column in parquet — add mom_z_score to feature builder.</p>}
            </div>

            {/* Kelly chart */}
            <div className="card" style={{ padding: "1rem", minWidth: CHART_W + 32 }}>
              <h4 style={{ margin: "0 0 0.75rem 0", fontSize: 13 }}>Kelly Fraction → Mean PnL/trade</h4>
              <LineChart
                points={kellyPoints}
                liveX={surface.optimal.kelly.live_value}
                xLabel="kelly_multiplier"
                yLabel="mean PnL"
                yFmt={(v) => v.toFixed(4)}
              />
              {kellyPoints.length === 0 && <p style={{ color: "#64748b", fontSize: 12, margin: "0.5rem 0 0" }}>No kelly_f column in parquet. Add kelly_f to feature builder.</p>}
            </div>

            {/* Delta SL chart */}
            <div className="card" style={{ padding: "1rem", minWidth: CHART_W + 32 }}>
              <h4 style={{ margin: "0 0 0.75rem 0", fontSize: 13 }}>Delta SL % → False-Positive Rate</h4>
              {hasSLTickData ? (
                <LineChart
                  points={slPoints}
                  liveX={surface.optimal.delta_sl.live_value}
                  xLabel="sl_threshold_pct"
                  yLabel="FP rate"
                  yFmt={(v) => (v * 100).toFixed(1) + "%"}
                />
              ) : (
                <div style={{ color: "#64748b", fontSize: 12, height: CHART_H, display: "flex", alignItems: "center" }}>
                  Tick data unavailable — momentum_ticks.csv not found. SL OPE requires tick-level delta data.
                </div>
              )}
            </div>
          </div>

          {/* Optimal config table */}
          <div className="card" style={{ padding: "1rem", marginBottom: "1.5rem" }}>
            <h4 style={{ margin: "0 0 0.75rem 0", fontSize: 13 }}>Optimal Config vs Live</h4>
            <OptimalTable optimal={surface.optimal} />
            <p style={{ margin: "0.5rem 0 0", fontSize: 11, color: "#475569" }}>
              Points with &lt;{20} samples are excluded from optimal selection. ⚠ badge = insufficient data.
              Amber = live value, teal = OPE-optimal.
            </p>
          </div>

          {/* Raw data accordion */}
          <details style={{ marginBottom: "1.5rem" }}>
            <summary style={{ cursor: "pointer", color: "#64748b", fontSize: 13, userSelect: "none" }}>Raw surface data (JSON)</summary>
            <pre style={{
              marginTop: "0.5rem", padding: "0.75rem", background: "#0f172a",
              border: "1px solid #1e293b", borderRadius: 6,
              fontSize: 10, color: "#94a3b8", overflow: "auto", maxHeight: 300,
            }}>
              {JSON.stringify(surface, null, 2)}
            </pre>
          </details>
        </>
      )}
    </div>
  );
}
