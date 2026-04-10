/**
 * Signals — Capital bar, maker opportunities (undeployed signals),
 * open orders (deployed quotes), and Strategy 2 mispricing signal history.
 */
import { useState } from "react";
import { useSignals, useConfig, useMakerSignals, useCapital, useMomentumDiagnostics, deploySignal } from "../api/client";
import type { MomentumDiagnosticMarket } from "../api/client";
import { usePolymarketEventSlugs } from "../utils/usePolymarketEventSlugs";

// Hide markets that are outside the scanner's interest window or haven't started yet.
// not_started = future bucket markets (TTE > bucket duration) — hiding them keeps the
// table focused on currently LIVE markets only.  no_book / empty_book are kept visible
// so operators can see thin/zero-liquidity markets and monitor WS subscription health.
const SCAN_HIDDEN = new Set(["beyond_horizon", "not_started"]);

function momentumSkipBadge(m: MomentumDiagnosticMarket): { label: string; color: string } {
  const sr = m.skip_reason;
  if (sr === "signal_fired") return { label: "\u2713 Fired", color: "#22c55e" };
  if (sr === "empty_book") return { label: "Empty book", color: "#f97316" };
  if (sr === "no_book") return { label: "No book data", color: "#f97316" };
  if (sr === "not_started") {
    const tte = m.tte_seconds != null ? Math.round(m.tte_seconds / 60) : "\u2014";
    return { label: `Not started \u2014 ${tte}m TTE`, color: "#475569" };
  }
  if (sr === "delta_below_threshold") {
    const delta = m.delta_pct != null ? m.delta_pct.toFixed(1) : "\u2014";
    const thresh = m.threshold_pct != null ? m.threshold_pct.toFixed(1) : "\u2014";
    return { label: `\u0394 ${delta}% vs \u2265${thresh}%`, color: "#f59e0b" };
  }
  if (sr === "tte_too_long") {
    const tte = m.tte_seconds != null ? Math.round(m.tte_seconds / 60) : "\u2014";
    const min = m.min_tte_s != null ? Math.round(m.min_tte_s / 60) : "\u2014";
    return { label: `TTE ${tte}m (max ${min}m)`, color: "#60a5fa" };
  }
  if (sr === "tte_floor") {
    const tte = m.tte_seconds != null ? `${m.tte_seconds}s` : "\u2014";
    return { label: `TTE ${tte} \u2014 too close (no stop-loss window)`, color: "#f97316" };
  }
  if (sr === "out_of_band") {
    const price = m.token_price != null ? Math.round(m.token_price * 100) : "\u2014";
    const lo = m.band_lo != null ? Math.round(m.band_lo * 100) : "\u2014";
    const hi = m.band_hi != null ? Math.round(m.band_hi * 100) : "\u2014";
    return { label: `${price}\u00a2 outside ${lo}\u00a2\u2013${hi}\u00a2`, color: "#64748b" };
  }
  if (sr === "thin_clob") {
    const depth = m.ask_depth_usd != null ? m.ask_depth_usd.toFixed(0) : "\u2014";
    const min = m.min_clob_depth != null ? m.min_clob_depth.toFixed(0) : "\u2014";
    return { label: `Thin book $${depth} vs $${min}`, color: "#f97316" };
  }
  if (sr === "no_ask") return { label: "No asks", color: "#f97316" };
  if (sr === "no_vol") return { label: "No vol data", color: "#ef4444" };
  if (sr === "no_spot" || sr === "stale_spot") return { label: "No spot data", color: "#ef4444" };
  if (sr === "duplicate_position") return { label: "Already held", color: "#a78bfa" };
  if (sr === "concurrent_cap") return { label: "Position cap", color: "#a78bfa" };
  if (sr === "cooldown") {
    const s = m.cooldown_remaining_s != null ? Math.round(m.cooldown_remaining_s) : "\u2014";
    return { label: `Cooldown ${s}s`, color: "#94a3b8" };
  }
  return { label: sr ?? "\u2014", color: "#475569" };
}

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

  const { data: diagData } = useMomentumDiagnostics();
  const slugMap = usePolymarketEventSlugs();
  const [actionPending, setActionPending] = useState<string | null>(null);

  const intervalLabel = cfg
    ? cfg.mispricing_scan_interval >= 60
      ? `${Math.floor(cfg.mispricing_scan_interval / 60)}m${cfg.mispricing_scan_interval % 60 ? ` ${cfg.mispricing_scan_interval % 60}s` : ""}`
      : `${cfg.mispricing_scan_interval}s`
    : "…";

  const makerEnabled = cfg?.strategy_maker ?? false;
  const momentumEnabled = cfg?.strategy_momentum ?? false;
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
      {/* ── Strategy 3: Momentum Scanner ────────────────────────────────────── */}
      <section style={{ marginTop: "2rem" }}>
        <h3 style={{ marginBottom: "0.5rem" }}>
          Strategy 3 — Momentum Scanner
          <span
            className="tag"
            style={{
              marginLeft: "0.75rem",
              background: momentumEnabled ? "#22c55e22" : "#1e293b",
              color: momentumEnabled ? "#22c55e" : "#64748b",
              fontSize: "0.75rem",
            }}
          >
            {momentumEnabled ? "ENABLED" : "DISABLED"}
          </span>
        </h3>
        <p className="muted">Price-confirmation taker — buys into momentum when token price exceeds volatility-adjusted threshold</p>

        {!momentumEnabled && (
          <p className="muted" style={{ marginTop: "0.5rem" }}>Enable in Settings to start scanning.</p>
        )}

        {momentumEnabled && (() => {
          const inWindowMarkets = (diagData?.markets ?? []).filter(
            (m) => !SCAN_HIDDEN.has(m.skip_reason)
          );
          const sortedMarkets = [...inWindowMarkets].sort((a, b) => {
            const key = (m: MomentumDiagnosticMarket) =>
              m.skip_reason === "signal_fired" ? Infinity : (m.gap_pct ?? -Infinity);
            return key(b) - key(a);
          });
          const totalScanned = diagData?.summary?.bucket_markets ?? null;
          const scanAge = diagData?.scan_ts
            ? Math.round(Date.now() / 1000 - diagData.scan_ts)
            : null;

          return (
            <>
              {/* ── Live Market Scan ────────────────────────────────────────── */}
              <div style={{ marginTop: "1.25rem" }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: "0.75rem", marginBottom: "0.5rem" }}>
                  <h4 style={{ margin: 0, color: "#e2e8f0", fontWeight: 600 }}>Live Market Scan</h4>
                  {diagData && (
                    <span style={{ fontSize: "0.75rem", color: "#475569" }}>
                      {inWindowMarkets.length} in-window
                      {totalScanned != null && ` / ${totalScanned} scanned`}
                      {scanAge != null && ` · last scan ${scanAge}s ago`}
                    </span>
                  )}
                </div>

                {!diagData && <p className="muted">Waiting for first scan…</p>}
                {diagData && sortedMarkets.length === 0 && (
                  <p className="muted">No in-window markets in the last scan.</p>
                )}

                {sortedMarkets.length > 0 && (
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" }}>
                      <thead>
                        <tr style={{ borderBottom: "1px solid #1e293b", color: "#64748b", textAlign: "left" }}>
                          <th style={{ padding: "0.4rem 0.6rem" }}>Market</th>
                          <th style={{ padding: "0.4rem 0.6rem" }}>Coin</th>
                          <th style={{ padding: "0.4rem 0.6rem" }}>Bucket</th>
                          <th style={{ padding: "0.4rem 0.6rem" }}>Side</th>
                          <th style={{ padding: "0.4rem 0.6rem" }} title="Token price in YES-space">Price</th>
                          <th style={{ padding: "0.4rem 0.6rem" }} title="Recorded strike — window-open spot for Up/Down, parsed from title otherwise">Strike / Spot</th>
                          <th style={{ padding: "0.4rem 0.6rem" }} title="Spot move vs vol-adjusted threshold">Δ% vs ≥Threshold</th>
                          <th style={{ padding: "0.4rem 0.6rem" }} title="Time to expiry">TTE</th>
                          <th style={{ padding: "0.4rem 0.6rem" }}>Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sortedMarkets.map((m, i) => {
                          const { label: badgeLabel, color: badgeColor } = momentumSkipBadge(m);
                          const sideColor = (m.side === "YES" || m.side === "UP") ? "#22c55e" : "#ef4444";
                          const tteSecs = m.tte_seconds ?? 0;
                          const tteMins = Math.round(tteSecs / 60);
                          const bucketLabel = m.market_type?.replace("bucket_", "") ?? "—";
                          return (
                            <tr key={i} style={{ borderBottom: "1px solid #0f172a", background: i % 2 === 0 ? "transparent" : "#0f172a44" }}>
                              <td style={{ padding: "0.4rem 0.6rem", fontWeight: 600, maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {m.market_title}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem", fontWeight: 600 }}>{m.underlying}</td>
                              <td style={{ padding: "0.4rem 0.6rem", color: "#94a3b8", fontSize: "0.75rem" }}>{bucketLabel}</td>
                              <td style={{ padding: "0.4rem 0.6rem" }}>
                                {m.side ? (
                                  <span style={{ padding: "2px 7px", borderRadius: 4, fontSize: "0.78rem", fontWeight: 600, background: `${sideColor}22`, color: sideColor }}>
                                    {m.side}
                                  </span>
                                ) : <span style={{ color: "#475569" }}>—</span>}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace" }}>
                                {m.token_price != null ? `${(m.token_price * 100).toFixed(1)}\u00a2` : "—"}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace", fontSize: "0.78rem" }}>
                                {m.strike != null ? (
                                  <span>
                                    <span style={{ color: "#e2e8f0" }}>{m.strike.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                                    {m.spot != null && (
                                      <span style={{ color: "#64748b" }}> / {m.spot.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                                    )}
                                  </span>
                                ) : <span style={{ color: "#475569" }}>—</span>}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace" }}>
                                {m.delta_pct != null && m.threshold_pct != null ? (
                                  <span>
                                    <span style={{ color: m.delta_pct >= m.threshold_pct ? "#22c55e" : "#f59e0b", fontWeight: 600 }}>
                                      {m.delta_pct >= 0 ? "+" : ""}{m.delta_pct.toFixed(1)}%
                                    </span>
                                    <span style={{ color: "#475569" }}> vs </span>
                                    <span style={{ color: "#94a3b8" }}>≥{m.threshold_pct.toFixed(1)}%</span>
                                  </span>
                                ) : <span style={{ color: "#475569" }}>—</span>}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace", color: "#94a3b8" }}>
                                {tteSecs > 0
                                  ? tteMins >= 60
                                    ? `${Math.floor(tteMins / 60)}h ${tteMins % 60}m`
                                    : `${tteMins}m`
                                  : "—"}
                              </td>
                              <td style={{ padding: "0.4rem 0.6rem" }}>
                                <span style={{
                                  padding: "2px 8px", borderRadius: 4, fontSize: "0.75rem", fontWeight: 500,
                                  background: `${badgeColor}22`, color: badgeColor, border: `1px solid ${badgeColor}44`,
                                  whiteSpace: "nowrap",
                                }}>
                                  {badgeLabel}
                                </span>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>


            </>
          );
        })()}
      </section>
    </div>
  );
}
