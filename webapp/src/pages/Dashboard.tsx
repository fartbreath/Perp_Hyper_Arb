/**
 * Dashboard — Live P&L, open positions, system health indicators.
 */
import { useState } from "react";
import {
  useHealth, usePnl, usePositions, useLauncherStatus,
  startBotProcess, stopBotProcess, usePerformance, useInventory, useConfig, updateConfig,
} from "../api/client";

// P3-G: WCAG-compliant status indicator (role="img" + text-based symbol for screen reader)
function StatusDot({ ok, label }: { ok: boolean; label?: string }) {
  return (
    <span
      role="img"
      aria-label={label ? `${label}: ${ok ? "connected" : "disconnected"}` : ok ? "OK" : "error"}
      style={{ color: ok ? "#22c55e" : "#ef4444", fontWeight: "bold" }}
    >
      {ok ? "●" : "○"}
    </span>
  );
}

function BotControlCard() {
  const launcher = useLauncherStatus();
  const health = useHealth();
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const launcherOffline = !!launcher.error;
  const botRunning = launcher.data?.running ?? false;

  async function handleStart() {
    setBusy(true);
    setLocalError(null);
    try {
      const r = await startBotProcess();
      if (!r.ok) setLocalError(r.reason ?? "Failed to start");
    } catch (e: unknown) {
      setLocalError(e instanceof Error ? e.message : "Start failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    setLocalError(null);
    try {
      const r = await stopBotProcess();
      if (!r.ok) setLocalError(r.reason ?? "Failed to stop");
    } catch (e: unknown) {
      setLocalError(e instanceof Error ? e.message : "Stop failed");
    } finally {
      setBusy(false);
    }
  }

  // Derive display state
  let statusText: string;
  let statusColor: string;
  if (launcherOffline) {
    statusText = "Launcher offline — run: python launcher.py";
    statusColor = "#f59e0b";
  } else if (!botRunning) {
    statusText = "Bot stopped";
    statusColor = "#ef4444";
  } else if (health.error) {
    statusText = "Starting… (API not yet ready)";
    statusColor = "#f59e0b";
  } else {
    statusText = "Running";
    statusColor = "#22c55e";
  }

  return (
    <div className="card" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem" }}>
      <div>
        <h3 style={{ margin: 0 }}>Bot</h3>
        <p className="muted" style={{ margin: "4px 0 0", color: statusColor }}>
          {statusText}
          {launcher.data?.pid && botRunning ? ` (PID ${launcher.data.pid})` : ""}
        </p>
        {localError && <p style={{ margin: "4px 0 0", color: "#ef4444", fontSize: "0.8rem" }}>{localError}</p>}
      </div>
      {!launcherOffline && (
        <button
          onClick={botRunning ? handleStop : handleStart}
          disabled={busy}
          style={{
            padding: "8px 20px",
            borderRadius: "6px",
            border: "none",
            cursor: busy ? "wait" : "pointer",
            fontWeight: 600,
            fontSize: "0.9rem",
            background: botRunning ? "#ef4444" : "#22c55e",
            color: "#fff",
            minWidth: "100px",
            flexShrink: 0,
          }}
        >
          {busy ? "…" : botRunning ? "⏹ Stop" : "▶ Start"}
        </button>
      )}
    </div>
  );
}

// P3-B: Bucket performance strip — shows per-bucket P&L + disable toggle
const BUCKET_ORDER = ["bucket_5m", "bucket_15m", "bucket_1h"] as const;
const BUCKET_LABELS: Record<string, string> = {
  bucket_5m: "5m",
  bucket_15m: "15m",
  bucket_1h: "1h",
};

function BucketPerformanceStrip() {
  const { data: perfData } = usePerformance("all");
  const { data: cfgData } = useConfig();
  const [busy, setBusy] = useState<string | null>(null);
  const [busyHedge, setBusyHedge] = useState(false);
  // Local state updated immediately after toggle — avoids 5s poll lag
  const [localExcluded, setLocalExcluded] = useState<string[] | null>(null);
  const [localHedgeEnabled, setLocalHedgeEnabled] = useState<boolean | null>(null);

  const byMt = perfData?.by_market_type ?? {};
  // Use local state (immediately reactive) over polled cfgData
  const excludedTypes: string[] = localExcluded ?? cfgData?.maker_excluded_market_types ?? [];
  const hedgeEnabled: boolean = localHedgeEnabled ?? cfgData?.maker_hedge_enabled ?? true;

  async function toggleBucket(bucket: string) {
    setBusy(bucket);
    try {
      // Fetch fresh config to avoid acting on stale polled state
      const res = await fetch(
        (import.meta.env.VITE_API_URL ?? "http://localhost:8080") + "/config"
      );
      const cfg = res.ok ? await res.json() : { maker_excluded_market_types: [] };
      const current: string[] = cfg.maker_excluded_market_types ?? [];
      const isExcluded = current.includes(bucket);
      const updated = isExcluded
        ? current.filter((b: string) => b !== bucket)  // re-enable
        : [...current, bucket];                         // disable
      const result = await updateConfig({ maker_excluded_market_types: updated });
      setLocalExcluded(result.maker_excluded_market_types);
    } finally {
      setBusy(null);
    }
  }

  async function toggleHedge() {
    setBusyHedge(true);
    try {
      const res = await fetch(
        (import.meta.env.VITE_API_URL ?? "http://localhost:8080") + "/config"
      );
      const cfg = res.ok ? await res.json() : { maker_hedge_enabled: true };
      const isEnabled: boolean = cfg.maker_hedge_enabled ?? true;
      const result = await updateConfig({ maker_hedge_enabled: !isEnabled });
      setLocalHedgeEnabled(result.maker_hedge_enabled ?? true);
    } finally {
      setBusyHedge(false);
    }
  }

  const hlD = byMt["hl_perp"];
  const hlPnl = hlD?.pnl ?? 0;
  const hlWR = hlD?.win_rate ?? null;
  const hlCount = hlD?.count ?? 0;
  const hlPnlColor = hlPnl >= 0 ? "#22c55e" : "#ef4444";
  const hedgeDisabled = !hedgeEnabled;

  return (
    <div className="card">
      <h3>Market Buckets</h3>
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
        {BUCKET_ORDER.map((bucket) => {
          const d = byMt[bucket];
          const pnl = d?.pnl ?? 0;
          const winRate = d?.win_rate ?? null;
          const count = d?.count ?? 0;
          const pnlColor = pnl >= 0 ? "#22c55e" : "#ef4444";
          const isExcluded = excludedTypes.includes(bucket);
          return (
            <div
              key={bucket}
              style={{
                flex: 1,
                minWidth: 90,
                background: "#1e293b",
                borderRadius: 8,
                padding: "0.6rem 0.75rem",
                textAlign: "center",
                opacity: isExcluded ? 0.5 : 1,
              }}
            >
              <div style={{ fontSize: "1.1rem", fontWeight: 700 }}>{BUCKET_LABELS[bucket]}</div>
              <div style={{ fontSize: "1.15rem", fontWeight: 700, color: pnlColor }}>
                {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
              </div>
              {count > 0 && winRate !== null && (
                <div style={{ fontSize: "0.72rem", color: "#64748b" }}>
                  {count} trades · {(winRate * 100).toFixed(0)}% WR
                </div>
              )}
              {count === 0 && (
                <div style={{ fontSize: "0.72rem", color: "#64748b" }}>no data</div>
              )}
              <button
                style={{
                  marginTop: "0.4rem",
                  fontSize: "0.7rem",
                  padding: "2px 10px",
                  borderRadius: 999,
                  border: "none",
                  cursor: busy === bucket ? "wait" : "pointer",
                  background: isExcluded ? "#14532d" : (pnl < -20 ? "#450a0a" : "#334155"),
                  color: isExcluded ? "#86efac" : (pnl < -20 ? "#fca5a5" : "#94a3b8"),
                  fontWeight: 600,
                }}
                disabled={busy === bucket}
                onClick={() => toggleBucket(bucket)}
                title={isExcluded ? "Enable bucket" : (pnl < -20 ? "P&L below -$20 — consider disabling" : "Disable bucket")}
              >
                {busy === bucket ? "…" : (isExcluded ? "✓ Enable" : "Disable")}
              </button>
            </div>
          );
        })}

        {/* HL Perp hedge toggle */}
        <div
          style={{
            flex: 1,
            minWidth: 90,
            background: "#1e293b",
            borderRadius: 8,
            padding: "0.6rem 0.75rem",
            textAlign: "center",
            opacity: hedgeDisabled ? 0.5 : 1,
            borderLeft: "2px solid #334155",
          }}
        >
          <div style={{ fontSize: "1.1rem", fontWeight: 700 }}>HL Hedge</div>
          <div style={{ fontSize: "1.15rem", fontWeight: 700, color: hlPnlColor }}>
            {hlCount > 0 ? `${hlPnl >= 0 ? "+" : ""}$${hlPnl.toFixed(2)}` : "—"}
          </div>
          {hlCount > 0 && hlWR !== null && (
            <div style={{ fontSize: "0.72rem", color: "#64748b" }}>
              {hlCount} trades · {(hlWR * 100).toFixed(0)}% WR
            </div>
          )}
          {hlCount === 0 && (
            <div style={{ fontSize: "0.72rem", color: "#64748b" }}>no data</div>
          )}
          <button
            style={{
              marginTop: "0.4rem",
              fontSize: "0.7rem",
              padding: "2px 10px",
              borderRadius: 999,
              border: "none",
              cursor: busyHedge ? "wait" : "pointer",
              background: hedgeDisabled ? "#14532d" : (hlPnl < -10 ? "#450a0a" : "#334155"),
              color: hedgeDisabled ? "#86efac" : (hlPnl < -10 ? "#fca5a5" : "#94a3b8"),
              fontWeight: 600,
            }}
            disabled={busyHedge}
            onClick={toggleHedge}
            title={hedgeDisabled ? "Enable HL delta hedge" : "Disable HL delta hedge"}
          >
            {busyHedge ? "…" : (hedgeDisabled ? "✓ Enable" : "Disable")}
          </button>
        </div>
      </div>
    </div>
  );
}


function HealthCard() {
  const { data, error } = useHealth();
  const { data: inv } = useInventory();
  if (error) return <div className="card error">API offline — {error}</div>;
  if (!data) return <div className="card skeleton" />;

  const hbAge = data.last_heartbeat_age_s;
  const hbOk = data.paper_trading ? true : (hbAge !== null && hbAge < 30);
  const dq = data.data_quality;
  const noBookPct = dq && dq.market_count > 0
    ? Math.round(dq.no_book_count / dq.market_count * 100) : 0;
  const stalePct = dq && dq.market_count > 0
    ? Math.round(dq.stale_book_count / dq.market_count * 100) : 0;
  const dataOk = !data.data_issues;

  // P3-D: Adverse detection indicator
  const adverseTriggers = data.adverse_triggers_session ?? 0;
  const adverseThreshold = data.adverse_threshold_pct ?? 0;
  const hlMaxMove = data.hl_max_move_pct_session ?? 0;
  const adverseCalibrationWarn = hlMaxMove > 0 && hlMaxMove < adverseThreshold / 2;

  // P3-C: Hedge status rows from inventory
  const coinHedges = inv?.coin_hedges ?? {};
  const hedgeThreshold = inv?.threshold_usd ?? 0;
  const totalHedgeNotional = Object.values(coinHedges)
    .reduce((s, h) => s + Math.abs(h.notional_usd), 0);
  const positionDeltas = inv?.position_delta ?? {};
  const maxDelta = Object.values(positionDeltas).reduce(
    (m, v) => Math.max(m, Math.abs(v)), 0
  );

  return (
    <div className="card">
      <h3>System Health</h3>
      <table className="kv-table">
        <tbody>
          <tr><td>PM WebSocket</td><td><StatusDot ok={data.pm_ws_connected} label="PM WebSocket" /> {data.pm_ws_connected ? "Connected" : "Disconnected"}</td></tr>
          <tr><td>HL WebSocket</td><td><StatusDot ok={data.hl_ws_connected} label="HL WebSocket" /> {data.hl_ws_connected ? "Connected" : "Disconnected"}</td></tr>
          <tr><td>Heartbeat</td><td><StatusDot ok={hbOk} label="Heartbeat" /> {hbAge !== null ? `${hbAge.toFixed(0)}s ago` : data.paper_trading ? "N/A (paper)" : "Never"}</td></tr>
          <tr><td>Uptime</td><td>{Math.floor(data.uptime_seconds / 60)}m {Math.floor(data.uptime_seconds % 60)}s</td></tr>
          <tr><td>Mode</td><td>{data.paper_trading ? "📋 Paper" : "🔴 LIVE"}</td></tr>
          <tr><td>Agent</td><td>{data.agent_auto ? "🤖 Auto" : "👤 Shadow"}</td></tr>
          {dq && (
            <>
              <tr>
                <td>Market Data</td>
                <td>
                  <StatusDot ok={dataOk} label="Market data" />
                  {" "}
                  {dq.fresh_book_count} fresh
                  {dq.stale_book_count > 0 && (
                    <span style={{ color: "#f97316", marginLeft: 6 }}>
                      · {dq.stale_book_count} stale ({stalePct}%)
                    </span>
                  )}
                  {dq.no_book_count > 0 && (
                    <span style={{ color: "#ef4444", marginLeft: 6 }}>
                      · {dq.no_book_count} no data ({noBookPct}%)
                    </span>
                  )}
                </td>
              </tr>
              <tr>
                <td>WS Subscriptions</td>
                <td>
                  {dq.sub_token_count} tokens
                  {dq.sub_rejected_count > 0 && (
                    <span style={{ color: "#ef4444", marginLeft: 6 }}>
                      · ⚠ {dq.sub_rejected_count} rejected
                    </span>
                  )}
                </td>
              </tr>
              {dq.spot_ages_s && Object.keys(dq.spot_ages_s).length > 0 && (
                <tr>
                  <td>Spot Oracle</td>
                  <td>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {Object.entries(dq.spot_ages_s).sort().map(([coin, age]) => {
                        const price = dq.spot_mids?.[coin];
                        const noData = age == null || (age as number) >= 1e6;
                        const stale = !noData && (age as number) > 30;
                        const color = noData ? "#ef4444" : stale ? "#f97316" : "#22c55e";
                        const ageLabel = noData
                          ? " (no data)"
                          : stale
                          ? ` ⚠${(age as number).toFixed(0)}s`
                          : price
                          ? ` $${price < 10 ? price.toFixed(4) : price.toFixed(2)}`
                          : " ✓";
                        return (
                          <span
                            key={coin}
                            title={noData ? `${coin}: never received` : `${coin}: ${(age as number).toFixed(1)}s ago`}
                            style={{ color, fontFamily: "monospace", fontSize: "0.85em" }}
                          >
                            {coin}{ageLabel}
                          </span>
                        );
                      })}
                    </div>
                  </td>
                </tr>
              )}
            </>
          )}
          {/* P3-C: Hedge status */}
          <tr>
            <td>Hedge (HL)</td>
            <td>
              {totalHedgeNotional > 0
                ? <span style={{ color: "#6366f1" }}>
                    {Object.entries(coinHedges).map(([coin, h]) =>
                      `${coin} ${h.direction} $${h.notional_usd.toFixed(0)}`
                    ).join(" · ")}
                  </span>
                : <span style={{ color: "#64748b" }}>
                    None · max Δ ${maxDelta.toFixed(0)} / threshold ${hedgeThreshold}
                  </span>
              }
            </td>
          </tr>
          {/* P3-D: Adverse detection */}
          <tr>
            <td>Adverse detect</td>
            <td>
              {adverseTriggers > 0
                ? <span style={{ color: "#f97316" }}>
                    {adverseTriggers} triggers · max HL move {(hlMaxMove * 100).toFixed(3)}%
                  </span>
                : <span style={{ color: adverseCalibrationWarn ? "#f59e0b" : "#64748b" }}>
                    0 triggers · max move {(hlMaxMove * 100).toFixed(3)}% vs threshold {(adverseThreshold * 100).toFixed(2)}%
                    {adverseCalibrationWarn && " ⚠ low volatility"}
                  </span>
              }
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function PnlCard() {
  const { data, error } = usePnl();
  if (error) return <div className="card error">P&L unavailable</div>;
  if (!data) return <div className="card skeleton" />;

  const fmt = (n: number) => `$${n >= 0 ? "+" : ""}${n.toFixed(2)}`;
  const color = (n: number) => ({ color: n >= 0 ? "#22c55e" : "#ef4444" });

  return (
    <div className="card">
      <h3>P&amp;L Summary</h3>
      <div className="pnl-grid">
        <div><span className="label">Today</span><span className="value" style={color(data.today)}>{fmt(data.today)}</span></div>
        <div><span className="label">7-Day</span><span className="value" style={color(data.week)}>{fmt(data.week)}</span></div>
        <div><span className="label">All-Time</span><span className="value" style={color(data.all_time)}>{fmt(data.all_time)}</span></div>
        <div><span className="label">Trades Today</span><span className="value">{data.trade_count_today}</span></div>
        <div><span className="label">Trades 7D</span><span className="value">{data.trade_count_week}</span></div>
        <div><span className="label">Total Trades</span><span className="value">{data.trade_count_all}</span></div>
      </div>
    </div>
  );
}

function PositionsCard() {
  const { data, error } = usePositions();
  if (error) return <div className="card error">Positions unavailable</div>;
  if (!data) return <div className="card skeleton" />;

  return (
    <div className="card">
      <h3>Open Positions ({data.count})</h3>
      {data.count === 0 ? (
        <p className="muted">No open positions</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th><th>Underlying</th><th>Side</th><th>Capital</th><th>Entry (token)</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p) => {
                const tokenPrice = p.side === "NO" ? 1 - Number(p.entry_price) : Number(p.entry_price);
                return (
                  <tr key={p.condition_id}>
                    <td className="mono">{p.condition_id.slice(0, 10)}…</td>
                    <td>{p.underlying}</td>
                    <td>{p.side}</td>
                    <td>${Number(p.entry_cost_usd ?? 0).toFixed(2)}</td>
                    <td>{(tokenPrice * 100).toFixed(2)}¢</td>
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

export default function Dashboard() {
  return (
    <div className="page">
      <h2>Dashboard</h2>
      <BotControlCard />
      <div className="grid-2">
        <PnlCard />
        <BucketPerformanceStrip />
      </div>
      <div className="grid-2">
        <HealthCard />
        <PositionsCard />
      </div>
    </div>
  );
}
