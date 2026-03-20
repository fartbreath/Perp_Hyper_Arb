/**
 * Markets — Currently tracked PM markets with quoted spreads and funding.
 */
import { useState, useEffect, useRef } from "react";
import { useMarkets, useFunding, useHealth } from "../api/client";

function sourceColor(src: string | null): string {
  if (src === "active_quote") return "#22c55e";  // green  — maker's own quote
  if (src === "pm_book")      return "#facc15";  // yellow — PM orderbook
  return "#6b7280";                               // gray   — no data
}

function sourceTip(src: string | null): string {
  if (src === "active_quote") return "Maker's own active quote";
  if (src === "pm_book")      return "PM orderbook best price";
  return "No price available";
}

function DataWarningBadge({ warning, ageS }: { warning: string | null; ageS: number | null }) {
  if (warning === "no_data") {
    return (
      <span style={{ color: "#ef4444", fontSize: "0.75rem", marginLeft: 4 }}
            title="No book snapshot received — market may be unsubscribed">
        ✕ no data
      </span>
    );
  }
  if (warning === "very_stale") {
    return (
      <span style={{ color: "#ef4444", fontSize: "0.75rem", marginLeft: 4 }}
            title={`Book snapshot is ${ageS}s old — likely not receiving WS updates`}>
        ⚠ {ageS}s stale
      </span>
    );
  }
  if (warning === "stale") {
    return (
      <span style={{ color: "#f97316", fontSize: "0.75rem", marginLeft: 4 }}
            title={`Book snapshot is ${ageS}s old`}>
        ⚠ {ageS}s
      </span>
    );
  }
  return null;
}

export default function Markets() {
  const { data: mkData, loading: mkLoading, error: mkError } = useMarkets();
  const { data: fnData } = useFunding();
  const { data: healthData } = useHealth();
  const [secondsAgo, setSecondsAgo] = useState<number | null>(null);
  const refreshedAt = useRef<number | null>(null);

  // Record timestamp whenever fresh data arrives
  useEffect(() => {
    if (mkData) {
      refreshedAt.current = Date.now();
      setSecondsAgo(0);
    }
  }, [mkData]);

  // Tick every second to keep the counter live
  useEffect(() => {
    const id = setInterval(() => {
      if (refreshedAt.current !== null) {
        setSecondsAgo(Math.floor((Date.now() - refreshedAt.current) / 1000));
      }
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const refreshLabel =
    secondsAgo === null ? "" :
    secondsAgo < 5 ? "✓ Just refreshed" :
    `Last refreshed ${secondsAgo}s ago`;

  return (
    <div className="page">
      <div style={{ display: "flex", alignItems: "baseline", gap: "0.75rem" }}>
        <h2 style={{ margin: 0 }}>Markets</h2>
        {refreshLabel && (
          <span style={{ fontSize: "0.8rem", color: "#888" }}>{refreshLabel}</span>
        )}
      </div>

      {mkError && <div className="error">Failed to load markets: {mkError}</div>}
      {mkLoading && <div className="skeleton" style={{ height: 200 }} />}

      {mkData && (() => {
        const noDataCount  = mkData.markets.filter(m => m.data_warning === "no_data").length;
        const staleCount   = mkData.markets.filter(m => m.data_warning === "stale" || m.data_warning === "very_stale").length;
        const rejectedCount = healthData?.data_quality?.sub_rejected_count ?? 0;
        const showBanner = noDataCount + staleCount + rejectedCount > 0;
        return (
        <div className="card">
          <h3>Tracked PM Markets ({mkData.count})</h3>
          {showBanner && (
            <div style={{ color: "#f97316", marginBottom: "0.5rem", fontSize: "0.85rem", padding: "0.4rem 0.6rem", background: "rgba(249,115,22,0.08)", borderRadius: 4 }}>
              ⚠ {staleCount > 0 && `${staleCount} stale`}{staleCount > 0 && noDataCount > 0 && " · "}{noDataCount > 0 && <span style={{ color: "#ef4444" }}>{noDataCount} no-data</span>}{rejectedCount > 0 && <span style={{ color: "#ef4444" }}> · {rejectedCount} WS subscriptions rejected</span>} — market pricing may be unreliable
            </div>
          )}
          {mkData.count === 0 ? (
            <p className="muted">No markets loaded yet.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Type</th>
                  <th>Underlying</th>
                  <th>Fees</th>
                  <th>Bid</th>
                  <th>Ask</th>
                  <th>Spread</th>
                  <th>Quoted</th>
                </tr>
              </thead>
              <tbody>
                {mkData.markets.map((m) => {
                  const bid    = m.bid_price !== null ? `${(m.bid_price * 100).toFixed(2)}¢` : "—";
                  const ask    = m.ask_price !== null ? `${(m.ask_price * 100).toFixed(2)}¢` : "—";
                  const spread = m.bid_price !== null && m.ask_price !== null
                    ? `${((m.ask_price - m.bid_price) * 100).toFixed(2)}¢`
                    : "—";
                  return (
                    <tr key={m.condition_id}>
                      <td>{m.title}</td>
                      <td><span className="tag">{m.market_type}</span></td>
                      <td>{m.underlying}</td>
                      <td>{m.fees_enabled ? "Yes" : <span style={{ color: "#22c55e" }}>Free</span>}</td>
                      <td title={sourceTip(m.bid_source)}>
                        <span style={{ color: sourceColor(m.bid_source) }}>●</span> {bid}
                      </td>
                      <td title={sourceTip(m.ask_source)}>
                        <span style={{ color: sourceColor(m.ask_source) }}>●</span> {ask}
                      </td>
                      <td>{spread}</td>
                      <td>
                        {m.quoted
                          ? <span style={{ color: "#22c55e" }}>●</span>
                          : <span style={{ color: "#ef4444" }}>●</span>}
                        <DataWarningBadge warning={m.data_warning} ageS={m.book_age_s ?? null} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
        );
      })()}

      {fnData && Object.keys(fnData.funding).length > 0 && (
        <div className="card">
          <h3>HL Funding Rates</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>Asset</th>
                <th>Predicted Rate</th>
                <th>Annualised</th>
                <th>Open Interest</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(fnData.funding).map(([coin, f]) => {
                const annual = f.predicted_rate * 3 * 365;
                return (
                  <tr key={coin}>
                    <td>{coin}</td>
                    <td style={{ color: f.predicted_rate >= 0 ? "#22c55e" : "#ef4444" }}>
                      {(f.predicted_rate * 100).toFixed(4)}%
                    </td>
                    <td style={{ color: annual >= 0 ? "#22c55e" : "#ef4444" }}>
                      {(annual * 100).toFixed(1)}% pa
                    </td>
                    <td>${(f.open_interest / 1e6).toFixed(2)}M</td>
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
