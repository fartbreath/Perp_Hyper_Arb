/**
 * Performance — Deep analytics: equity curve, win rate, Sharpe, rebates, heatmap.
 */
import { useState } from "react";
import { usePerformance, runReconcile, type ReconcileResult } from "../api/client";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Cell, ReferenceLine,
} from "recharts";

type Period = "7d" | "30d" | "all";

function SummaryRow({ s }: { s: ReturnType<typeof usePerformance>["data"] }) {
  if (!s || s.no_data) return null;
  const { summary } = s;
  const pnlColor = summary.total_pnl >= 0 ? "#22c55e" : "#ef4444";
  return (
    <div className="summary-row">
      <div className="stat"><div className="stat-label">Trades</div><div className="stat-val">{summary.total_trades}</div></div>
      <div className="stat"><div className="stat-label">Win Rate</div><div className="stat-val">{(summary.win_rate * 100).toFixed(1)}%</div></div>
      <div className="stat"><div className="stat-label">Avg P&L</div><div className="stat-val" style={{ color: summary.avg_pnl >= 0 ? "#22c55e" : "#ef4444" }}>${summary.avg_pnl.toFixed(4)}</div></div>
      <div className="stat"><div className="stat-label">Net P&L</div><div className="stat-val" style={{ color: pnlColor }}>${summary.total_pnl.toFixed(2)}</div></div>
      <div className="stat"><div className="stat-label">Fees Paid</div><div className="stat-val" style={{ color: "#f97316" }}>${summary.total_fees.toFixed(4)}</div></div>
      <div className="stat"><div className="stat-label">Rebates</div><div className="stat-val" style={{ color: "#22c55e" }}>+${summary.total_rebates.toFixed(4)}</div></div>
      <div className="stat"><div className="stat-label">Max DD</div><div className="stat-val" style={{ color: "#ef4444" }}>-${summary.max_drawdown.toFixed(2)}</div></div>
      <div className="stat"><div className="stat-label">Sharpe 7D</div><div className="stat-val">{summary.sharpe_7d !== null ? summary.sharpe_7d.toFixed(2) : "—"}</div></div>
    </div>
  );
}

export default function Performance() {
  const [period, setPeriod] = useState<Period>("all");
  const { data, loading, error } = usePerformance(period);

  const [reconcileDays, setReconcileDays] = useState<number>(14);
  const [reconcileLoading, setReconcileLoading] = useState<boolean>(false);
  const [reconcileError, setReconcileError] = useState<string | null>(null);
  const [reconcileResult, setReconcileResult] = useState<ReconcileResult | null>(null);

  async function handleReconcile() {
    setReconcileLoading(true);
    setReconcileError(null);
    try {
      const r = await runReconcile(reconcileDays);
      setReconcileResult(r);
    } catch (e) {
      setReconcileError(e instanceof Error ? e.message : String(e));
    } finally {
      setReconcileLoading(false);
    }
  }

  return (
    <div className="page">
      <h2>Performance</h2>

      <div className="period-tabs">
        {(["7d", "30d", "all"] as Period[]).map((p) => (
          <button key={p} className={period === p ? "active" : ""} onClick={() => setPeriod(p)}>
            {p === "all" ? "All time" : p}
          </button>
        ))}
      </div>

      {/* Manual ledger ↔ PM /activity reconciliation sweep */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Ledger Reconcile (manual)</h3>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ fontSize: 13 }}>
            Window (days):{" "}
            <input
              type="number"
              min={1}
              max={90}
              value={reconcileDays}
              onChange={(e) => setReconcileDays(Math.max(1, Math.min(90, Number(e.target.value) || 14)))}
              style={{ width: 64 }}
            />
          </label>
          <button onClick={handleReconcile} disabled={reconcileLoading}>
            {reconcileLoading ? "Running…" : "Run reconcile"}
          </button>
          {reconcileError && <span style={{ color: "#ef4444" }}>Error: {reconcileError}</span>}
        </div>

        {reconcileResult && (
          <div style={{ marginTop: 12, fontSize: 13 }}>
            <div className="summary-row" style={{ marginBottom: 8 }}>
              <div className="stat"><div className="stat-label">Ledger Net P&L</div><div className="stat-val">${reconcileResult.ledger.net_pnl.toFixed(2)}</div></div>
              <div className="stat"><div className="stat-label">PM Realized</div><div className="stat-val">${reconcileResult.pm.net_realized.toFixed(2)}</div></div>
              <div className="stat">
                <div className="stat-label">Drift</div>
                <div className="stat-val" style={{ color: Math.abs(reconcileResult.drift_usd) < 0.5 ? "#22c55e" : "#ef4444" }}>
                  ${reconcileResult.drift_usd.toFixed(2)}
                </div>
              </div>
              <div className="stat"><div className="stat-label">PM Buy / Sell / Redeem</div><div className="stat-val">${reconcileResult.pm.buy_usd.toFixed(0)} / ${reconcileResult.pm.sell_usd.toFixed(0)} / ${reconcileResult.pm.redeem_usd.toFixed(0)}</div></div>
              <div className="stat"><div className="stat-label">Ledger Rows</div><div className="stat-val">{reconcileResult.ledger.rows}</div></div>
            </div>

            {reconcileResult.reconciliation_flagged.length > 0 && (
              <details open style={{ marginTop: 8 }}>
                <summary style={{ color: "#f97316", cursor: "pointer" }}>
                  ⚠ {reconcileResult.reconciliation_flagged.length} ledger row(s) flagged RECONCILE_REQUIRED
                </summary>
                <table style={{ width: "100%", marginTop: 6, fontSize: 12 }}>
                  <thead>
                    <tr><th align="left">When</th><th align="left">Market</th><th align="left">Side</th><th align="right">Net P&L</th><th align="left">Notes</th></tr>
                  </thead>
                  <tbody>
                    {reconcileResult.reconciliation_flagged.map((r, i) => (
                      <tr key={i}>
                        <td>{r.recorded_at.slice(0, 19).replace("T", " ")}</td>
                        <td>{r.market_title.slice(0, 60)}</td>
                        <td>{r.side}</td>
                        <td align="right" style={{ color: r.net_pnl >= 0 ? "#22c55e" : "#ef4444" }}>${r.net_pnl.toFixed(4)}</td>
                        <td style={{ fontSize: 11, color: "#888" }}>{r.notes}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            )}

            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer" }}>Per-market breakdown ({reconcileResult.per_market.length})</summary>
              <table style={{ width: "100%", marginTop: 6, fontSize: 12 }}>
                <thead>
                  <tr><th align="left">Market</th><th align="right">Buy</th><th align="right">Sell</th><th align="right">Redeem</th><th align="right">PM Net</th></tr>
                </thead>
                <tbody>
                  {reconcileResult.per_market.map((m, i) => (
                    <tr key={i}>
                      <td>{m.market_title.slice(0, 70)}</td>
                      <td align="right">${m.buy_usd.toFixed(2)}</td>
                      <td align="right">${m.sell_usd.toFixed(2)}</td>
                      <td align="right">${m.redeem_usd.toFixed(2)}</td>
                      <td align="right" style={{ color: m.pm_net_usd >= 0 ? "#22c55e" : "#ef4444" }}>${m.pm_net_usd.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </div>
        )}
      </div>

      {error && <div className="error">Failed to load: {error}</div>}
      {loading && <div className="skeleton" style={{ height: 400 }} />}
      {data?.no_data && <p className="muted">No trade data yet.</p>}

      {data && !data.no_data && (
        <>
          <SummaryRow s={data} />

          {/* Equity Curve */}
          <div className="card">
            <h3>Equity Curve</h3>
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={data.equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                <XAxis dataKey="t" hide />
                <YAxis tickFormatter={(v) => `$${v}`} />
                <Tooltip formatter={(v: unknown) => [`$${Number(v).toFixed(2)}`, "Equity"]} />
                <ReferenceLine y={0} stroke="#555" />
                <Line type="monotone" dataKey="equity" stroke="#6366f1" dot={false} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* P&L by Strategy + Underlying */}
          <div className="grid-2">
            <div className="card">
              <h3>By Strategy</h3>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={Object.entries(data.by_strategy).map(([k, v]) => ({ name: k, pnl: v.pnl }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                  <XAxis dataKey="name" />
                  <YAxis tickFormatter={(v) => `$${v}`} />
                  <Tooltip formatter={(v: unknown) => [`$${Number(v).toFixed(4)}`, "P&L"]} />
                  <Bar dataKey="pnl">
                    {Object.entries(data.by_strategy).map(([k, v]) => (
                      <Cell key={k} fill={v.pnl >= 0 ? "#22c55e" : "#ef4444"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="card">
              <h3>By Underlying</h3>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={Object.entries(data.by_underlying).map(([k, v]) => ({ name: k, pnl: v.pnl }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                  <XAxis dataKey="name" />
                  <YAxis tickFormatter={(v) => `$${v}`} />
                  <Tooltip formatter={(v: unknown) => [`$${Number(v).toFixed(4)}`, "P&L"]} />
                  <Bar dataKey="pnl">
                    {Object.entries(data.by_underlying).map(([k, v]) => (
                      <Cell key={k} fill={v.pnl >= 0 ? "#22c55e" : "#ef4444"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* P3-E: P&L by Market Type */}
          {data.by_market_type && Object.keys(data.by_market_type).length > 0 && (() => {
            const BUCKET_ORDER = ["bucket_5m", "bucket_15m", "bucket_1h", "milestone"];
            const ordered = BUCKET_ORDER
              .filter((k) => k in data.by_market_type)
              .concat(Object.keys(data.by_market_type).filter((k) => !BUCKET_ORDER.includes(k)));
            const chartData = ordered.map((k) => ({
              name: k.replace("bucket_", ""),
              pnl: data.by_market_type[k].pnl,
              win_rate: data.by_market_type[k].win_rate,
              count: data.by_market_type[k].count,
            }));
            return (
              <div className="card">
                <h3>By Market Type</h3>
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                    <XAxis dataKey="name" />
                    <YAxis tickFormatter={(v) => `$${v}`} />
                    <Tooltip formatter={(v: unknown) => [`$${Number(v).toFixed(4)}`, "P&L"]} />
                    <Bar dataKey="pnl">
                      {chartData.map((d) => (
                        <Cell key={d.name} fill={d.pnl >= 0 ? "#22c55e" : "#ef4444"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                <div className="table-scroll" style={{ marginTop: "0.75rem" }}>
                  <table className="data-table">
                    <thead>
                      <tr><th>Bucket</th><th>Trades</th><th>Win Rate</th><th>Net P&L</th></tr>
                    </thead>
                    <tbody>
                      {chartData.map((d) => (
                        <tr key={d.name}>
                          <td>{d.name}</td>
                          <td>{d.count}</td>
                          <td>{(d.win_rate * 100).toFixed(1)}%</td>
                          <td style={{ color: d.pnl >= 0 ? "#22c55e" : "#ef4444" }}>
                            {d.pnl >= 0 ? "+" : ""}${d.pnl.toFixed(4)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })()}

          {/* P&L Histogram */}
          <div className="card">
            <h3>P&L Distribution</h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={data.pnl_histogram.map((b) => ({
                name: `${(b.bucket_start ?? 0).toFixed(3)}`,
                count: b.count,
                positive: (b.bucket_start ?? 0) >= 0,
              }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                <YAxis allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count">
                  {data.pnl_histogram.map((b, i) => (
                    <Cell key={i} fill={(b.bucket_start ?? 0) >= 0 ? "#22c55e" : "#ef4444"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Time-of-Day Heatmap */}
          <div className="card">
            <h3>Avg P&L by Hour (HKT)</h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={data.time_of_day_heatmap.map((h) => ({
                hour: `${h.hour_hkt}:00`,
                avg_pnl: h.avg_pnl,
                count: h.trade_count,
              }))}>
                <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                <XAxis dataKey="hour" tick={{ fontSize: 10 }} />
                <YAxis tickFormatter={(v) => `$${v.toFixed(2)}`} />
                <Tooltip formatter={(v: unknown) => [`$${Number(v).toFixed(4)}`, "Avg P&L"]} />
                <ReferenceLine y={0} stroke="#555" />
                <Bar dataKey="avg_pnl">
                  {data.time_of_day_heatmap.map((h, i) => (
                    <Cell key={i} fill={h.avg_pnl >= 0 ? "#6366f1" : "#ef4444"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Best / Worst trades */}
          <div className="grid-2">
            <div className="card">
              <h3>Best 5 Trades</h3>
              <table className="data-table">
                <thead><tr><th>Market</th><th>P&L</th></tr></thead>
                <tbody>
                  {data.best_trades.map((t, i) => (
                    <tr key={i}>
                      <td className="mono">{(t.market_id ?? "").slice(0, 10)}…</td>
                      <td style={{ color: "#22c55e" }}>+${Number(t.pnl).toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card">
              <h3>Worst 5 Trades</h3>
              <table className="data-table">
                <thead><tr><th>Market</th><th>P&L</th></tr></thead>
                <tbody>
                  {data.worst_trades.map((t, i) => {
                    const pnl = Number(t.pnl);
                    return (
                      <tr key={i}>
                        <td className="mono">{(t.market_id ?? "").slice(0, 10)}…</td>
                        <td style={{ color: pnl >= 0 ? "#22c55e" : "#ef4444" }}>
                          {pnl >= 0 ? `+$${pnl.toFixed(4)}` : `-$${Math.abs(pnl).toFixed(4)}`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
