/**
 * ModelC.tsx — ML-C2 Model C Calibration Simulator
 *
 * Shows:
 *  - Calibration curve (predicted P(WIN) vs actual win rate)
 *  - Score distribution histogram
 *  - Summary stats card
 */

import { useModelCCalibration, type ModelCCalibrationBucket } from "../api/client";
import { useConfig } from "../api/client";

// ─── helpers ────────────────────────────────────────────────────────────────

const PCT = (v: number | null | undefined) =>
  v == null ? "—" : `${(v * 100).toFixed(1)}%`;

const LOW_N_THRESHOLD = 10;

// ─── Calibration Chart (SVG) ─────────────────────────────────────────────────

interface CalibrationChartProps {
  buckets: ModelCCalibrationBucket[];
}

function CalibrationChart({ buckets }: CalibrationChartProps) {
  const W = 420;
  const H = 220;
  const PAD = { top: 18, right: 18, bottom: 40, left: 46 };

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;

  const toX = (v: number) => PAD.left + v * innerW;
  const toY = (v: number) => PAD.top + (1 - v) * innerH;

  // Only buckets with actual data
  const withData = buckets.filter((b) => b.actual_win_rate != null);

  // Reference diagonal line (perfect calibration)
  const diagPath = `M ${toX(0)} ${toY(0)} L ${toX(1)} ${toY(1)}`;

  // Data polyline
  const dataPath =
    withData.length > 1
      ? withData
          .map((b, i) => `${i === 0 ? "M" : "L"} ${toX(b.predicted_midpoint)} ${toY(b.actual_win_rate!)}`)
          .join(" ")
      : null;

  const yTicks = [0, 0.25, 0.5, 0.75, 1.0];
  const xTicks = [0, 0.25, 0.5, 0.75, 1.0];

  return (
    <svg width={W} height={H} className="block mx-auto">
      {/* Grid */}
      {yTicks.map((t) => (
        <line
          key={`gy-${t}`}
          x1={PAD.left}
          y1={toY(t)}
          x2={PAD.left + innerW}
          y2={toY(t)}
          stroke="#334155"
          strokeWidth={0.5}
        />
      ))}
      {xTicks.map((t) => (
        <line
          key={`gx-${t}`}
          x1={toX(t)}
          y1={PAD.top}
          x2={toX(t)}
          y2={PAD.top + innerH}
          stroke="#334155"
          strokeWidth={0.5}
        />
      ))}

      {/* Axes */}
      <line x1={PAD.left} y1={PAD.top} x2={PAD.left} y2={PAD.top + innerH} stroke="#475569" />
      <line
        x1={PAD.left}
        y1={PAD.top + innerH}
        x2={PAD.left + innerW}
        y2={PAD.top + innerH}
        stroke="#475569"
      />

      {/* Y tick labels */}
      {yTicks.map((t) => (
        <text key={`yl-${t}`} x={PAD.left - 6} y={toY(t) + 4} textAnchor="end" fontSize={9} fill="#64748b">
          {(t * 100).toFixed(0)}%
        </text>
      ))}

      {/* X tick labels */}
      {xTicks.map((t) => (
        <text
          key={`xl-${t}`}
          x={toX(t)}
          y={PAD.top + innerH + 14}
          textAnchor="middle"
          fontSize={9}
          fill="#64748b"
        >
          {(t * 100).toFixed(0)}%
        </text>
      ))}

      {/* Axis labels */}
      <text
        x={PAD.left + innerW / 2}
        y={H - 4}
        textAnchor="middle"
        fontSize={9}
        fill="#64748b"
      >
        Predicted P(WIN)
      </text>
      <text
        transform={`rotate(-90,${PAD.left - 36},${PAD.top + innerH / 2})`}
        x={PAD.left - 36}
        y={PAD.top + innerH / 2 + 4}
        textAnchor="middle"
        fontSize={9}
        fill="#64748b"
      >
        Actual Win Rate
      </text>

      {/* Perfect calibration reference (diagonal) */}
      <path d={diagPath} stroke="#2dd4bf" strokeWidth={1} strokeDasharray="4 3" opacity={0.5} fill="none" />

      {/* Data polyline */}
      {dataPath && (
        <path d={dataPath} stroke="#f59e0b" strokeWidth={1.5} fill="none" />
      )}

      {/* Data points */}
      {withData.map((b) => {
        const lowN = b.n < LOW_N_THRESHOLD;
        return (
          <circle
            key={`pt-${b.predicted_midpoint}`}
            cx={toX(b.predicted_midpoint)}
            cy={toY(b.actual_win_rate!)}
            r={lowN ? 3 : 4.5}
            fill={lowN ? "#475569" : "#f59e0b"}
            stroke="#0f172a"
            strokeWidth={1}
          >
            <title>
              Predicted: {PCT(b.predicted_midpoint)} → Actual: {PCT(b.actual_win_rate)} (n={b.n})
            </title>
          </circle>
        );
      })}
    </svg>
  );
}

// ─── Score Histogram ─────────────────────────────────────────────────────────

interface HistogramProps {
  histogram: { lo: number; hi: number; count: number }[];
}

function ScoreHistogram({ histogram }: HistogramProps) {
  const maxCount = Math.max(...histogram.map((b) => b.count), 1);
  const W = 420;
  const H = 80;
  const barW = W / histogram.length - 1;

  return (
    <svg width={W} height={H} className="block mx-auto">
      {histogram.map((b, i) => {
        const barH = (b.count / maxCount) * (H - 16);
        return (
          <rect
            key={i}
            x={i * (barW + 1)}
            y={H - 16 - barH}
            width={barW}
            height={barH}
            fill="#f59e0b"
            opacity={0.7}
          >
            <title>
              [{(b.lo * 100).toFixed(0)}%–{(b.hi * 100).toFixed(0)}%]: {b.count} rows
            </title>
          </rect>
        );
      })}
      <text x={0} y={H - 2} fontSize={8} fill="#64748b">0%</text>
      <text x={W / 2} y={H - 2} textAnchor="middle" fontSize={8} fill="#64748b">50%</text>
      <text x={W} y={H - 2} textAnchor="end" fontSize={8} fill="#64748b">100%</text>
    </svg>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export default function ModelCPage() {
  const { data, error, loading, refresh } = useModelCCalibration();
  const { data: cfg } = useConfig();

  const modelEnabled = cfg?.model_c_enabled ?? false;

  return (
    <div className="p-4 space-y-5" style={{ color: "#cbd5e1", background: "#0f172a", minHeight: "100vh" }}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: "#f1f5f9" }}>
            Model C — Exit Calibration Simulator
          </h1>
          <p className="text-sm mt-0.5" style={{ color: "#64748b" }}>
            ML-C2 · calibrated P(WIN) on ON exits · shadow mode only
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span
            className="text-xs px-2 py-1 rounded font-medium"
            style={{
              background: modelEnabled ? "#14532d" : "#1c1917",
              color: modelEnabled ? "#4ade80" : "#78716c",
              border: `1px solid ${modelEnabled ? "#16a34a" : "#44403c"}`,
            }}
          >
            {modelEnabled ? "Live (shadow)" : "Disabled"}
          </span>
          <button
            onClick={refresh}
            className="text-xs px-3 py-1.5 rounded font-medium"
            style={{ background: "#1e293b", color: "#94a3b8", border: "1px solid #334155" }}
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Loading / Error */}
      {loading && !data && (
        <div className="text-sm" style={{ color: "#64748b" }}>Loading calibration data…</div>
      )}
      {error && (
        <div className="text-sm p-3 rounded" style={{ background: "#450a0a", color: "#fca5a5", border: "1px solid #991b1b" }}>
          Error: {error}
        </div>
      )}

      {/* Model not trained yet */}
      {data && !data.exists && (
        <div className="p-5 rounded-lg" style={{ background: "#1e293b", border: "1px solid #334155" }}>
          <div className="flex items-center gap-2 mb-2">
            <span style={{ color: "#f59e0b" }}>⚠</span>
            <span className="font-medium" style={{ color: "#fbbf24" }}>Model C not trained yet</span>
          </div>
          <p className="text-sm" style={{ color: "#94a3b8" }}>
            {data.reason ?? "Run a retrain to generate model_c_v0.pkl"}
          </p>
          <p className="text-sm mt-1" style={{ color: "#64748b" }}>
            Model C requires ON fills with resolved outcomes in training_data.parquet.
          </p>
        </div>
      )}

      {/* Calibration chart */}
      {data?.exists && data.buckets && data.buckets.length > 0 && (
        <div className="rounded-lg p-4 space-y-3" style={{ background: "#1e293b", border: "1px solid #334155" }}>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium" style={{ color: "#e2e8f0" }}>Calibration Curve</h2>
            <span className="text-xs" style={{ color: "#64748b" }}>
              Amber = data · Teal dashed = perfect calibration · Gray dots = n &lt; {LOW_N_THRESHOLD}
            </span>
          </div>
          <CalibrationChart buckets={data.buckets} />
        </div>
      )}

      {/* Score distribution histogram */}
      {data?.exists && data.score_histogram && data.score_histogram.length > 0 && (
        <div className="rounded-lg p-4 space-y-2" style={{ background: "#1e293b", border: "1px solid #334155" }}>
          <h2 className="text-sm font-medium" style={{ color: "#e2e8f0" }}>Score Distribution</h2>
          <ScoreHistogram histogram={data.score_histogram} />
        </div>
      )}

      {/* Summary stats */}
      {data?.exists && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            { label: "Rows Scored", value: data.n_scored?.toLocaleString() ?? "—" },
            { label: "Avg Model Score", value: PCT(data.mean_score) },
            { label: "Actual Win Rate", value: PCT(data.actual_win_rate) },
            { label: "Model Status", value: modelEnabled ? "Shadow live" : "Disabled" },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="rounded-lg p-3"
              style={{ background: "#1e293b", border: "1px solid #334155" }}
            >
              <div className="text-xs" style={{ color: "#64748b" }}>{label}</div>
              <div className="text-base font-semibold mt-1" style={{ color: "#f1f5f9" }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Bucket table */}
      {data?.exists && data.buckets && data.buckets.length > 0 && (
        <div className="rounded-lg overflow-hidden" style={{ border: "1px solid #334155" }}>
          <table className="w-full text-xs">
            <thead style={{ background: "#0f172a", color: "#64748b" }}>
              <tr>
                <th className="text-left p-2">Bucket</th>
                <th className="text-right p-2">N Rows</th>
                <th className="text-right p-2">Actual Win Rate</th>
                <th className="text-right p-2">Gap (Actual − Predicted)</th>
              </tr>
            </thead>
            <tbody>
              {data.buckets.map((b, i) => {
                const gap =
                  b.actual_win_rate != null
                    ? b.actual_win_rate - b.predicted_midpoint
                    : null;
                const lowN = b.n < LOW_N_THRESHOLD;
                return (
                  <tr
                    key={i}
                    style={{
                      background: i % 2 === 0 ? "#1e293b" : "#0f172a",
                      color: lowN ? "#475569" : "#cbd5e1",
                    }}
                  >
                    <td className="p-2">
                      {PCT(b.bucket_lo)}–{PCT(b.bucket_hi)}
                    </td>
                    <td className="text-right p-2">{b.n}</td>
                    <td className="text-right p-2">{PCT(b.actual_win_rate)}</td>
                    <td
                      className="text-right p-2 font-mono"
                      style={{
                        color:
                          gap == null
                            ? "#475569"
                            : gap > 0.05
                            ? "#4ade80"
                            : gap < -0.05
                            ? "#f87171"
                            : "#94a3b8",
                      }}
                    >
                      {gap == null ? "—" : `${gap > 0 ? "+" : ""}${(gap * 100).toFixed(1)}pp`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
