/**
 * Risk — Real-time exposure gauges vs limits.
 */
import { useRisk } from "../api/client";

function GaugeBar({ pct, warn = 80 }: { pct: number; warn?: number }) {
  const clamped = Math.min(100, Math.max(0, pct));
  const color = clamped >= 100 ? "#ef4444" : clamped >= warn ? "#f97316" : "#6366f1";
  return (
    <div className="gauge-track">
      <div className="gauge-fill" style={{ width: `${clamped}%`, background: color }} />
    </div>
  );
}

export default function Risk() {
  const { data, loading, error } = useRisk();

  if (error) return <div className="page"><div className="error">Failed to load risk: {error}</div></div>;
  if (loading || !data) return <div className="page"><div className="skeleton" style={{ height: 300 }} /></div>;

  return (
    <div className="page">
      <h2>Risk Monitor</h2>

      {data.paper_trading && (
        <div className="banner info">📋 Paper trading mode — no real exposure</div>
      )}

      <div className="card">
        <h3>PM Exposure</h3>
        <div className="gauge-row">
          <span>${data.pm_exposure_usd.toFixed(0)}</span>
          <GaugeBar pct={data.pm_exposure_pct} />
          <span className="muted">${data.pm_exposure_limit}</span>
        </div>
        <p className="muted">{data.pm_exposure_pct.toFixed(1)}% of limit used</p>
      </div>

      <div className="card">
        <h3>HL Notional</h3>
        <div className="gauge-row">
          <span>${data.hl_notional_usd.toFixed(0)}</span>
          <GaugeBar pct={data.hl_notional_pct} />
          <span className="muted">${data.hl_notional_limit}</span>
        </div>
        <p className="muted">{data.hl_notional_pct.toFixed(1)}% of limit used</p>
      </div>

      <div className="card">
        <h3>Positions</h3>
        <div className="gauge-row">
          <span>{data.open_positions}</span>
          <GaugeBar pct={data.open_positions / data.max_concurrent_positions * 100} />
          <span className="muted">{data.max_concurrent_positions} max</span>
        </div>
      </div>

      <div className="card">
        <h3>Limits Summary</h3>
        <table className="kv-table">
          <tbody>
            <tr><td>Hard Stop (Max Drawdown)</td><td style={{ color: "#ef4444" }}>−${data.hard_stop_threshold.toFixed(0)}</td></tr>
            <tr><td>Max PM per Market</td><td>${(data.max_pm_per_market ?? 0).toFixed(0)}</td></tr>
            <tr><td>Paper Trading</td><td>{data.paper_trading ? "Yes ✓" : "No — LIVE"}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
