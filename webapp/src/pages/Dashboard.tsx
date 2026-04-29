/**
 * Dashboard — Live P&L, open positions, system health indicators.
 */
import { useState } from "react";
import {
  useHealth, usePnl, usePositions, useLauncherStatus,
  startBotProcess, stopBotProcess, usePerformance, useInventory, useConfig, updateConfig,
  useMomentumSignals, useMomentumScanSummary, useOpeningNeutralStatus,
} from "../api/client";
import type { Position } from "../api/client";

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
  const { data: cfg } = useConfig();
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
        {/* Strategy enable indicators */}
        {botRunning && cfg && (
          <div style={{ display: "flex", gap: "0.4rem", marginTop: "0.5rem", flexWrap: "wrap" }}>
            {cfg.strategy_momentum && (
              <span style={{ padding: "1px 7px", borderRadius: 999, fontSize: "0.7rem", fontWeight: 700, background: "#2e1065", color: "#a78bfa" }}>Momentum</span>
            )}
            {cfg.strategy_maker && (
              <span style={{ padding: "1px 7px", borderRadius: 999, fontSize: "0.7rem", fontWeight: 700, background: "#1e3a5f", color: "#60a5fa" }}>Maker</span>
            )}
            {cfg.strategy_mispricing && (
              <span style={{ padding: "1px 7px", borderRadius: 999, fontSize: "0.7rem", fontWeight: 700, background: "#064e3b", color: "#34d399" }}>Mispricing</span>
            )}
          </div>
        )}
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
const BUCKET_ORDER = ["bucket_5m", "bucket_15m", "bucket_1h", "bucket_4h"] as const;
const BUCKET_LABELS: Record<string, string> = {
  bucket_5m: "5m",
  bucket_15m: "15m",
  bucket_1h: "1h",
  bucket_4h: "4h",
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
          {dq && (
            <>
              <tr>
                <td>CL Streams WS</td>
                <td>
                  <StatusDot ok={dq.chainlink_streams_connected ?? false} label="CL Streams WS" />
                  {" "}{dq.chainlink_streams_connected ? "Connected" : "Disconnected"}
                  {dq.chainlink_streams_connected === undefined && <span style={{ color: "#64748b" }}> (bot offline)</span>}
                </td>
              </tr>
              <tr>
                <td>CL On-chain WS</td>
                <td>
                  <StatusDot ok={dq.chainlink_ws_connected ?? false} label="CL On-chain WS" />
                  {" "}{dq.chainlink_ws_connected ? "Connected" : "Disconnected"}
                  {dq.chainlink_ws_connected === undefined && <span style={{ color: "#64748b" }}> (bot offline)</span>}
                </td>
              </tr>
            </>
          )}
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
              {dq.chainlink_ages_s && Object.keys(dq.chainlink_ages_s).length > 0 && (
                <tr>
                  <td>CL Oracle</td>
                  <td>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {Object.entries(dq.chainlink_ages_s).sort().map(([coin, age]) => {
                        const price = dq.chainlink_mids?.[coin];
                        const noData = age == null;
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
              {dq.spot_ages_s && Object.keys(dq.spot_ages_s).length > 0 && (
                <tr>
                  <td>RTDS Oracle</td>
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

function timeUntilEnd(iso: string | null | undefined): { label: string; color: string } {
  if (!iso) return { label: "—", color: "#4b5563" };
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return { label: "Resolved", color: "#6b7280" };
  const totalMin = Math.floor(diff / 60_000);
  const h = Math.floor(totalMin / 60);
  const d = Math.floor(h / 24);
  if (d >= 2) return { label: `${d}d`, color: "#94a3b8" };
  if (h >= 4) return { label: `${h}h`, color: "#f59e0b" };
  if (h >= 1) return { label: `${h}h ${totalMin % 60}m`, color: "#ef4444" };
  return { label: `${totalMin}m`, color: "#ef4444" };
}

const STRATEGY_BADGE: Record<string, { label: string; color: string; bg: string }> = {
  momentum:        { label: "MOM",  color: "#a78bfa", bg: "#2e1065" },
  opening_neutral: { label: "ONT",  color: "#38bdf8", bg: "#0c2a3d" },
  maker:           { label: "MKR",  color: "#60a5fa", bg: "#1e3a5f" },
  mispricing:      { label: "MIS",  color: "#34d399", bg: "#064e3b" },
};

function StrategyBadge({ strategy }: { strategy: string }) {
  const b = STRATEGY_BADGE[strategy] ?? { label: strategy.slice(0, 3).toUpperCase(), color: "#94a3b8", bg: "#1e293b" };
  return (
    <span style={{ padding: "1px 6px", borderRadius: 3, fontSize: "0.7rem", fontWeight: 700,
      background: b.bg, color: b.color, fontFamily: "monospace" }}>
      {b.label}
    </span>
  );
}

function sideBg(side: string): string {
  if (side === "YES" || side === "UP")   return "#166534";
  if (side === "NO"  || side === "DOWN") return "#7f1d1d";
  return "#374151";
}

function PositionRow({ p }: { p: Position }) {
  const tokenPrice  = p.token_current_price ?? (p.side === "NO" ? 1 - p.entry_price : p.entry_price);
  const entryToken  = p.side === "NO" ? 1 - p.entry_price : p.entry_price;
  const unrealised  = p.unrealised_pnl_usd;
  const pnlColor    = unrealised == null ? "#94a3b8" : unrealised >= 0 ? "#22c55e" : "#ef4444";
  const tte         = timeUntilEnd(p.end_date);
  const hasHedge    = !!(p.hedge_order_id);
  const hedgeFilled = !!(p.hedge_fill_detected);

  return (
    <tr>
      {/* Market title (truncated) */}
      <td style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          title={p.market_title}>
        {p.market_title ?? p.condition_id.slice(0, 10) + "…"}
      </td>

      {/* Underlying */}
      <td style={{ fontWeight: 600 }}>{p.underlying}</td>

      {/* Side */}
      <td>
        <span style={{ padding: "2px 6px", borderRadius: 3, fontSize: "0.72rem",
          fontWeight: 700, background: sideBg(p.side), color: "#fff" }}>
          {p.side}
        </span>
      </td>

      {/* Strategy */}
      <td><StrategyBadge strategy={p.strategy} /></td>

      {/* Capital deployed */}
      <td style={{ fontFamily: "monospace" }}>${(p.entry_cost_usd ?? 0).toFixed(0)}</td>

      {/* Entry → current token price */}
      <td style={{ fontFamily: "monospace", fontSize: "0.82rem" }}
          title={`Entry: ${(entryToken * 100).toFixed(1)}¢  Current: ${(tokenPrice * 100).toFixed(1)}¢`}>
        {(entryToken * 100).toFixed(0)}→{(tokenPrice * 100).toFixed(0)}<span style={{ color: "#64748b" }}>¢</span>
      </td>

      {/* Unrealised P&L */}
      <td style={{ fontFamily: "monospace", fontWeight: 600, color: pnlColor }}>
        {unrealised == null ? "—" : `${unrealised >= 0 ? "+" : ""}$${unrealised.toFixed(2)}`}
      </td>

      {/* Hedge status (momentum only) */}
      <td>
        {p.strategy === "momentum" && hasHedge ? (
          hedgeFilled
            ? <span style={{ color: "#22c55e", fontSize: "0.75rem" }}>🛡 Filled</span>
            : <span style={{ color: "#a78bfa", fontSize: "0.75rem" }} title={`Price: $${(p.hedge_price ?? 0).toFixed(3)}`}>🛡 Resting</span>
        ) : p.strategy === "momentum" ? (
          <span style={{ color: "#4b5563", fontSize: "0.75rem" }}>—</span>
        ) : null}
      </td>

      {/* TTE */}
      <td style={{ fontFamily: "monospace", fontSize: "0.8rem", color: tte.color }}>
        {tte.label}
      </td>
    </tr>
  );
}

function PositionsCard() {
  const { data, error } = usePositions();
  if (error) return <div className="card error">Positions unavailable</div>;
  if (!data) return <div className="card skeleton" />;

  // Exclude closed grace-period rows and HL hedge rows from the dashboard summary
  const active = data.positions.filter(p => !p.is_closed && p.venue !== "HL");

  return (
    <div className="card">
      <h3>Open Positions ({active.length})</h3>
      {active.length === 0 ? (
        <p className="muted">No open positions</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th><th>Coin</th><th>Side</th><th>Strat</th>
                <th>Capital</th><th>Entry→Now</th><th>Unreal P&amp;L</th><th>Hedge</th><th>TTE</th>
              </tr>
            </thead>
            <tbody>
              {active.map(p => <PositionRow key={p.condition_id + p.side} p={p} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function MomentumCard() {
  const { data: scanData } = useMomentumScanSummary();
  const { data: sigsData } = useMomentumSignals(10);
  const { data: cfgData }  = useConfig();

  const enabled = cfgData?.strategy_momentum ?? false;
  const s = scanData?.summary ?? {};
  const scanTs = scanData?.scan_ts ?? 0;
  const scanAge = scanTs > 0 ? Math.round((Date.now() / 1000) - scanTs) : null;
  const fired = s.signals_fired ?? 0;
  const markets = s.bucket_markets ?? 0;
  const recentSigs = sigsData?.signals ?? [];

  if (!enabled && recentSigs.length === 0 && scanTs === 0) {
    return null; // hide entirely when strategy is off and never ran
  }

  const scanAgeColor = scanAge == null ? "#64748b" : scanAge < 30 ? "#22c55e" : scanAge < 120 ? "#f59e0b" : "#ef4444";

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.6rem" }}>
        <h3 style={{ margin: 0 }}>Momentum Strategy</h3>
        <span style={{
          padding: "2px 8px", borderRadius: 999, fontSize: "0.72rem", fontWeight: 700,
          background: enabled ? "#2e1065" : "#1f2937",
          color: enabled ? "#a78bfa" : "#64748b",
        }}>
          {enabled ? "● Active" : "○ Disabled"}
        </span>
      </div>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
        {/* Scan health */}
        <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 100 }}>
          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Last scan</div>
          <div style={{ fontFamily: "monospace", color: scanAgeColor, fontWeight: 600 }}>
            {scanAge == null ? "Never" : `${scanAge}s ago`}
          </div>
        </div>
        <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 100 }}>
          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Markets</div>
          <div style={{ fontFamily: "monospace", fontWeight: 600 }}>{markets}</div>
        </div>
        <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 100 }}>
          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Signals fired</div>
          <div style={{ fontFamily: "monospace", fontWeight: 600, color: fired > 0 ? "#a78bfa" : "#94a3b8" }}>{fired}</div>
        </div>
        {(s.skipped_delta ?? 0) + (s.skipped_band ?? 0) + (s.skipped_vol ?? 0) + (s.skipped_phase_c ?? 0) > 0 && (
          <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 140 }}>
            <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Skipped</div>
            <div style={{ fontFamily: "monospace", fontSize: "0.78rem", color: "#64748b" }}>
              {s.skipped_delta ?? 0} delta
              {(s.skipped_band ?? 0) > 0 && ` · ${s.skipped_band} band`}
              {(s.skipped_vol ?? 0) > 0 && ` · ${s.skipped_vol} vol`}
              {(s.skipped_phase_c ?? 0) > 0 && ` · ${s.skipped_phase_c} tte`}
            </div>
          </div>
        )}
      </div>

      {/* Recent signals */}
      {recentSigs.length > 0 && (
        <div>
          <div style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "0.4rem" }}>Recent signals</div>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
            {recentSigs.slice(0, 5).map((sig, i) => {
              const age = Math.round((Date.now() / 1000) - sig.timestamp);
              const ageLabel = age < 60 ? `${age}s ago` : `${Math.round(age / 60)}m ago`;
              return (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.5rem",
                  fontSize: "0.8rem", background: "#1e293b", borderRadius: 6, padding: "0.3rem 0.6rem" }}>
                  <span style={{ fontWeight: 700, color: "#a78bfa" }}>{sig.underlying}</span>
                  <span style={{ color: sig.side === "YES" || sig.side === "UP" ? "#22c55e" : "#ef4444",
                    fontWeight: 600 }}>{sig.side}</span>
                  <span style={{ color: "#64748b" }}>{sig.market_type}</span>
                  <span style={{ fontFamily: "monospace" }}>
                    Δ{sig.delta_pct >= 0 ? "+" : ""}{sig.delta_pct.toFixed(1)}%
                  </span>
                  <span style={{ color: "#64748b", marginLeft: "auto" }}>{ageLabel}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function OpeningNeutralCard() {
  const { data } = useOpeningNeutralStatus();
  if (!data) return null;

  const enabled = data.enabled;
  const dryRun = data.dry_run;
  const pairs = data.pairs ?? [];
  const recentSigs = data.recent_signals ?? [];
  const entryAttempts = recentSigs.filter(s => s.result === "entry_attempt").length;
  const tooExpensive = recentSigs.filter(s => s.result === "too_expensive").length;

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.6rem" }}>
        <h3 style={{ margin: 0 }}>Opening Neutral</h3>
        <span style={{
          padding: "2px 8px", borderRadius: 999, fontSize: "0.72rem", fontWeight: 700,
          background: enabled ? "#0c2a3d" : "#1f2937",
          color: enabled ? "#38bdf8" : "#64748b",
        }}>
          {enabled ? "● Active" : "○ Disabled"}
        </span>
        {dryRun && (
          <span style={{ padding: "2px 8px", borderRadius: 999, fontSize: "0.72rem",
            fontWeight: 700, background: "#451a03", color: "#fb923c" }}>
            DRY RUN
          </span>
        )}
      </div>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
        <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 100 }}>
          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Active pairs</div>
          <div style={{ fontFamily: "monospace", fontWeight: 600, color: data.active_pairs > 0 ? "#38bdf8" : "#94a3b8" }}>
            {data.active_pairs}
          </div>
        </div>
        <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 100 }}>
          <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Entry attempts</div>
          <div style={{ fontFamily: "monospace", fontWeight: 600, color: entryAttempts > 0 ? "#38bdf8" : "#94a3b8" }}>
            {entryAttempts}
          </div>
        </div>
        {tooExpensive > 0 && (
          <div style={{ background: "#1e293b", borderRadius: 8, padding: "0.5rem 0.75rem", minWidth: 120 }}>
            <div style={{ fontSize: "0.7rem", color: "#64748b", marginBottom: 2 }}>Too expensive</div>
            <div style={{ fontFamily: "monospace", fontWeight: 600, color: "#f59e0b" }}>{tooExpensive}</div>
          </div>
        )}
      </div>

      {/* Active pairs */}
      {pairs.length > 0 && (
        <div style={{ marginBottom: "0.75rem" }}>
          <div style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "0.4rem" }}>Active pairs</div>
          {pairs.map(pair => (
            <div key={pair.pair_id} style={{
              display: "flex", alignItems: "center", gap: "0.75rem",
              background: "#1e293b", borderRadius: 6, padding: "0.4rem 0.75rem",
              marginBottom: "0.3rem", fontSize: "0.8rem",
            }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
                title={pair.market_title}>
                {pair.market_title}
              </span>
              <span style={{ color: "#22c55e", fontFamily: "monospace" }}>
                YES {pair.yes_entry != null ? pair.yes_entry.toFixed(2) : "—"}
              </span>
              <span style={{ color: "#f87171", fontFamily: "monospace" }}>
                NO {pair.no_entry != null ? pair.no_entry.toFixed(2) : "—"}
              </span>
              {pair.yes_entry != null && pair.no_entry != null && (
                <span style={{ color: "#38bdf8", fontFamily: "monospace" }}>
                  Σ {(pair.yes_entry + pair.no_entry).toFixed(3)}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Recent scan signals */}
      {recentSigs.slice(0, 5).map((sig, i) => (
        <div key={i} style={{
          display: "flex", alignItems: "center", gap: "0.5rem",
          fontSize: "0.78rem", background: "#1e293b", borderRadius: 6,
          padding: "0.3rem 0.6rem", marginBottom: "0.25rem",
        }}>
          <span style={{ color: sig.result === "entry_attempt" ? "#38bdf8" : "#64748b" }}>
            {sig.result === "entry_attempt" ? "▶" : "✗"}
          </span>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
            title={sig.market_title}>{sig.market_title}</span>
          {sig.combined != null && (
            <span style={{ fontFamily: "monospace", color: sig.combined <= sig.threshold ? "#22c55e" : "#ef4444" }}>
              Σ{sig.combined.toFixed(3)}
            </span>
          )}
          <span style={{ color: "#64748b", fontFamily: "monospace" }}>{sig.tte_secs}s TTE</span>
        </div>
      ))}
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
      <MomentumCard />
      <OpeningNeutralCard />
      <PositionsCard />
      <HealthCard />
    </div>
  );
}
