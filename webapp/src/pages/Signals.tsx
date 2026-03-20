/**
 * Signals — Capital bar, maker opportunities (undeployed signals),
 * open orders (deployed quotes), and Strategy 2 mispricing signal history.
 */
import { useState } from "react";
import { useSignals, useConfig, useMakerSignals, useCapital, deploySignal } from "../api/client";
import { usePolymarketEventSlugs } from "../utils/usePolymarketEventSlugs";

const DECISION_COLOR: Record<string, string> = {
  EXECUTE: "#22c55e",
  SKIP: "#94a3b8",
  HALT: "#ef4444",
};

function scoreBadgeColor(score: number): string {
  if (score >= 80) return "#22c55e";
  if (score >= 60) return "#86efac";
  if (score >= 40) return "#fbbf24";
  return "#ef4444";
}

function ScoreBadge({ score, tooltip }: { score?: number; tooltip?: string }) {
  if (score === undefined || score === null) return <span style={{ color: "#475569" }}>—</span>;
  const c = scoreBadgeColor(score);
  return (
    <span
      title={tooltip}
      style={{
        display: "inline-block",
        background: `${c}22`,
        color: c,
        border: `1px solid ${c}44`,
        borderRadius: 4,
        padding: "0.1rem 0.45rem",
        fontFamily: "monospace",
        fontSize: "0.78rem",
        cursor: tooltip ? "help" : undefined,
      }}
    >
      {score.toFixed(1)}
    </span>
  );
}

export default function Signals() {
  const { data, loading, error } = useSignals(100);
  const { data: cfg } = useConfig();
  const { data: signalsData, loading: signalsLoading } = useMakerSignals();
  const { data: capital } = useCapital();
  const slugMap = usePolymarketEventSlugs();
  const [actionPending, setActionPending] = useState<string | null>(null);

  const intervalLabel = cfg
    ? cfg.scan_interval >= 60
      ? `${Math.floor(cfg.scan_interval / 60)}m${cfg.scan_interval % 60 ? ` ${cfg.scan_interval % 60}s` : ""}`
      : `${cfg.scan_interval}s`
    : "…";

  const makerEnabled = cfg?.strategy_maker ?? false;
  const allSignals = signalsData?.signals ?? [];
  const opportunities = allSignals.filter((s) => !s.is_deployed);

  const deploymentMode = capital?.mode ?? cfg?.deployment_mode ?? "auto";
  const budget = capital?.total_budget ?? cfg?.paper_capital_usd ?? 0;
  const deployed = capital?.deployed ?? 0;
  const inPositions = capital?.in_positions ?? 0;
  const available = capital?.available ?? 0;
  const usedPct = budget > 0 ? Math.min(100, ((deployed + inPositions) / budget) * 100) : 0;

  async function handleDeploy(tokenId: string) {
    setActionPending(tokenId);
    try { await deploySignal(tokenId); } catch { /* error handled by server */ }
    finally { setActionPending(null); }
  }

  return (
    <div className="page">
      <h2>Signals</h2>

      {/* ── Strategy 1: Capital Bar ─────────────────────────────────────── */}
      <section style={{ marginBottom: "2rem" }}>
        <h3 style={{ marginBottom: "0.5rem" }}>
          Strategy 1 — Market Making
          <span
            className="tag"
            style={{
              marginLeft: "0.75rem",
              background: makerEnabled ? "#22c55e22" : "#1e293b",
              color: makerEnabled ? "#22c55e" : "#64748b",
              fontSize: "0.75rem",
            }}
          >
            {makerEnabled ? "ENABLED" : "DISABLED"}
          </span>
          <span
            className="tag"
            style={{
              marginLeft: "0.5rem",
              background: "#1e293b",
              color: "#94a3b8",
              fontSize: "0.75rem",
              textTransform: "uppercase",
            }}
          >
            {deploymentMode} deploy
          </span>
        </h3>

        {/* Capital utilisation bar */}
        <div style={{ marginBottom: "1.25rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", color: "#94a3b8", marginBottom: "0.3rem" }}>
            <span>Capital</span>
            <span>
              <span style={{ color: "#f59e0b" }}>${deployed.toFixed(0)} deployed</span>
              {" + "}
              <span style={{ color: "#3b82f6" }}>${inPositions.toFixed(0)} in positions</span>
              {" / "}
              <span style={{ color: "#e2e8f0" }}>${budget.toFixed(0)} budget</span>
              {" — "}
              <span style={{ color: "#22c55e" }}>${available.toFixed(0)} free</span>
            </span>
          </div>
          <div style={{ height: 8, background: "#1e293b", borderRadius: 4, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${usedPct}%`, background: usedPct > 85 ? "#ef4444" : usedPct > 60 ? "#f59e0b" : "#22c55e", transition: "width 0.4s" }} />
          </div>
        </div>

        {/* Opportunities — undeployed signals */}
        <div style={{ marginBottom: "1.5rem" }}>
          <h4 style={{ marginBottom: "0.4rem", color: "#94a3b8", fontWeight: 500 }}>
            Opportunities ({opportunities.length})
            <span style={{ marginLeft: "0.5rem", fontSize: "0.75rem", color: "#475569" }}>
              — qualifying markets, no capital deployed
            </span>
          </h4>

          {signalsLoading && <div className="skeleton" style={{ height: 60 }} />}

          {!signalsLoading && !makerEnabled && (
            <p className="muted">Maker strategy is disabled. Enable it in Settings to start quoting.</p>
          )}

          {!signalsLoading && makerEnabled && opportunities.length === 0 && (
            <p className="muted">No undeployed opportunities at this time.</p>
          )}

          {opportunities.length > 0 && (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid #1e293b", color: "#64748b", textAlign: "left" }}>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Market</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Underlying</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Mid</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Bid / Ask</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Spread</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Edge</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Score</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Type</th>
                    <th style={{ padding: "0.4rem 0.6rem" }}>Age</th>
                    {deploymentMode === "manual" && <th style={{ padding: "0.4rem 0.6rem" }} />}
                  </tr>
                </thead>
                <tbody>
                  {opportunities.map((s, i) => {
                    const marketUrl = s.market_slug
                      ? `https://polymarket.com/event/${s.market_slug}`
                      : slugMap[s.market_id]
                      ? `https://polymarket.com/event/${slugMap[s.market_id]}`
                      : null;
                    return (
                      <tr key={i} style={{ borderBottom: "1px solid #0f172a", background: i % 2 === 0 ? "transparent" : "#0f172a44" }}>
                        <td style={{ padding: "0.4rem 0.6rem", fontWeight: 600, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {marketUrl ? (
                            <a href={marketUrl} target="_blank" rel="noopener noreferrer">{s.market_title || s.market_id}</a>
                          ) : (
                            s.market_title
                          )}
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontWeight: 600 }}>{s.underlying}</td>
                        <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace" }}>{(s.mid * 100).toFixed(1)}¢</td>
                        <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace" }}>
                          <span style={{ color: "#22c55e" }}>{(s.bid_price * 100).toFixed(1)}</span>
                          {" / "}
                          <span style={{ color: "#ef4444" }}>{(s.ask_price * 100).toFixed(1)}</span>
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace" }}>{(s.half_spread * 200).toFixed(1)}¢</td>
                        <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace", color: "#22c55e" }}>
                          {(s.effective_edge * 100).toFixed(2)}%
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem" }}>
                          <ScoreBadge score={s.score} />
                        </td>
                        <td style={{ padding: "0.4rem 0.6rem", color: "#94a3b8", fontSize: "0.75rem" }}>{s.market_type}</td>
                        <td style={{ padding: "0.4rem 0.6rem", color: "#64748b" }}>{s.age_seconds.toFixed(0)}s</td>
                        {deploymentMode === "manual" && (
                          <td style={{ padding: "0.4rem 0.6rem" }}>
                            <button
                              onClick={() => handleDeploy(s.token_id)}
                              disabled={actionPending === s.token_id}
                              style={{ padding: "0.2rem 0.6rem", fontSize: "0.75rem", background: "#22c55e22", color: "#22c55e", border: "1px solid #22c55e44", borderRadius: 4, cursor: "pointer" }}
                            >
                              {actionPending === s.token_id ? "…" : "Deploy"}
                            </button>
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* ── Strategy 2: Mispricing Signals ──────────────────────────────── */}
      <section>
        <h3 style={{ marginBottom: "0.5rem" }}>Strategy 2 — Mispricing Scanner</h3>
        <p className="muted">Deribit options-implied probability vs Polymarket price</p>

        {error && <div className="error">Failed to load signals: {error}</div>}
        {loading && <div className="skeleton" style={{ height: 300 }} />}

        {data && data.signals.length === 0 && (
          <p className="muted">No signals yet. Scanner runs every {intervalLabel}.</p>
        )}

        {data && data.signals.map((sig, i) => (
          <div key={i} className="signal-card">
            <div className="signal-header">
              <span className="signal-title">{sig.market_title}</span>
              <span style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                {sig.score !== undefined && <ScoreBadge score={sig.score} />}
                <span className="signal-time">{new Date(sig.timestamp * 1000).toLocaleString()}</span>
              </span>
            </div>

            <div className="signal-body">
              <div className="signal-prices">
                <div>
                  <span className="label">PM Price</span>
                  <span className="value">{(sig.pm_price * 100).toFixed(1)}%</span>
                </div>
                <div>
                  <span className="label">Implied Prob</span>
                  <span className="value">{(sig.implied_prob * 100).toFixed(1)}%</span>
                </div>
                <div>
                  <span className="label">Deviation</span>
                  <span className="value" style={{ color: sig.deviation > sig.fee_hurdle ? "#22c55e" : "#94a3b8" }}>
                    {(sig.deviation * 100).toFixed(1)}%
                  </span>
                </div>
                <div>
                  <span className="label">Fee Hurdle</span>
                  <span className="value muted">{(sig.fee_hurdle * 100).toFixed(2)}%</span>
                </div>
                <div>
                  <span className="label">Direction</span>
                  <span className="value">{sig.direction}</span>
                </div>
                <div>
                  <span className="label">Deribit IV</span>
                  <span className="value">{(sig.deribit_iv * 100).toFixed(0)}%</span>
                </div>
              </div>

              <div className="agent-box">
                <span className="label">Agent</span>
                {sig.agent_decision ? (
                  <>
                    <span
                      className="agent-decision"
                      style={{ color: DECISION_COLOR[sig.agent_decision] ?? "#fff" }}
                    >
                      {sig.agent_decision}
                    </span>
                    {sig.agent_confidence !== undefined && (
                      <span className="muted"> ({(sig.agent_confidence * 100).toFixed(0)}%)</span>
                    )}
                    {sig.agent_reason && <div className="agent-reason">{sig.agent_reason}</div>}
                  </>
                ) : (
                  <span className="muted">Pending</span>
                )}
              </div>
            </div>

            <div className="signal-footer">
              <span className="mono muted">{sig.deribit_instrument}</span>
              <span
                className="tag"
                style={{ background: sig.is_actionable ? "#22c55e22" : "#1e293b", color: sig.is_actionable ? "#22c55e" : "#64748b" }}
              >
                {sig.is_actionable ? "Actionable" : "Below hurdle"}
              </span>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}
