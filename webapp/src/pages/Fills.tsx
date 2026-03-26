/**
 * Fills — Paper-trade fill audit log.
 *
 * Shows every simulated fill event from data/fills.csv including the full
 * fill context: taker model params, orderbook snapshot at fill time, and
 * whether the fill was adversely selected (HL moved against us).
 *
 * Columns:
 *   Time        — UTC timestamp
 *   Market      — title (truncated)
 *   Underlying  — BTC / ETH / SOL …
 *   Side        — order side (BUY/SELL) + position side (YES/NO)
 *   Fill price  — token price at which we were filled
 *   Contracts   — number of contracts filled this event
 *   USD         — USD cost / proceeds of this fill
 *   Book        — live bid × ask at fill time
 *   Depth       — competing depth at our price level
 *   Taker model — arrival_prob · mean_taker · actual_taker_drawn
 *   HL mid      — HL perp mid at fill time + % move since last sweep
 *   Adverse     — highlighted when HL moved against us (adverse selection)
 */
import { useState } from "react";
import { useFills } from "../api/client";
import type { FillEntry } from "../api/client";

const UNDERLYINGS = ["", "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"];
const PAGE_SIZE   = 100;

function fmtTs(ts: string) {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString("en-US", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    });
  } catch { return ts.slice(0, 19) || "—"; }
}

function num(s: string | undefined, decimals = 4) {
  const n = Number(s);
  return isNaN(n) ? "—" : n.toFixed(decimals);
}

function pct(s: string | undefined) {
  const n = Number(s);
  return isNaN(n) || n === 0 ? "—" : `${(n * 100).toFixed(3)}%`;
}

export default function Fills() {
  const [underlying, setUnderlying] = useState("");
  const [adverseOnly, setAdverseOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const { data, loading, error } = useFills(PAGE_SIZE, offset, underlying || undefined, adverseOnly);

  const fills: FillEntry[] = data?.fills ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="page">
      <h2>
        Fill Log
        {data && (
          <span className="muted" style={{ fontSize: "0.85rem", marginLeft: "0.75rem" }}>
            {total} fills total
          </span>
        )}
      </h2>

      <div className="filters">
        <label>Underlying:
          <select value={underlying} onChange={(e) => { setUnderlying(e.target.value); setOffset(0); }}>
            {UNDERLYINGS.map((u) => <option key={u} value={u}>{u || "All"}</option>)}
          </select>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={adverseOnly}
            onChange={(e) => { setAdverseOnly(e.target.checked); setOffset(0); }}
          />
          Adverse only
        </label>
      </div>

      {error   && <div className="error">Failed to load fills: {error}</div>}
      {loading && !data && <div className="skeleton" style={{ height: 200 }} />}

      {fills.length === 0 && !loading && (
        <p className="muted">No fill events yet. The fill simulator writes to data/fills.csv on each paper fill.</p>
      )}

      {fills.length > 0 && (
        <>
          <div style={{ overflowX: "auto" }}>
            <table className="data-table" style={{ fontSize: "0.79rem" }}>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Market</th>
                  <th>Und.</th>
                  <th>Order / Pos</th>
                  <th title="Token price at which the fill was executed">Fill Price</th>
                  <th title="Number of contracts filled in this event">Contracts</th>
                  <th title="USD cost / proceeds of this fill">USD</th>
                  <th title="Live bid × ask at the moment of fill">Book bid×ask</th>
                  <th title="Competing depth at our price level">Depth</th>
                  <th title="Taker arrival probability used for this sweep">P(arrive)</th>
                  <th title="Mean taker size parameter">Mean tkr</th>
                  <th title="Actual taker size drawn from model">Drawn</th>
                  <th title="HL perp mid at fill time">HL mid</th>
                  <th title="HL price move since previous sweep (positive = rose)">HL Δ</th>
                  <th title="Whether HL moved against our fill direction (adverse selection)">Adv?</th>
                </tr>
              </thead>
              <tbody>
                {fills.map((f, i) => {
                  const isAdverse = f.adverse?.toLowerCase() === "true";
                  const rowBg = isAdverse
                    ? (i % 2 === 0 ? "#7f1d1d22" : "#7f1d1d33")
                    : (i % 2 === 0 ? "transparent" : "#0f172a44");
                  return (
                    <tr key={i} style={{ borderBottom: "1px solid #0f172a", background: rowBg }}>
                      <td className="mono" style={{ color: "#64748b", whiteSpace: "nowrap" }}>
                        {fmtTs(f.timestamp)}
                      </td>
                      <td
                        title={f.market_title}
                        style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {f.market_title || f.market_id}
                      </td>
                      <td style={{ fontWeight: 600 }}>{f.underlying}</td>
                      <td>
                        <span style={{
                          padding: "1px 5px", borderRadius: 3, fontSize: "0.75rem", fontWeight: 600,
                          background: f.order_side === "BUY" ? "#166534" : "#7f1d1d", color: "#fff",
                          marginRight: 3,
                        }}>
                          {f.order_side}
                        </span>
                        <span style={{ color: "#94a3b8", fontSize: "0.75rem" }}>{f.position_side}</span>
                      </td>
                      <td className="mono">{num(f.fill_price, 4)}</td>
                      <td className="mono">{num(f.contracts_filled, 2)}</td>
                      <td className="mono" style={{ color: "#f59e0b" }}>${num(f.fill_cost_usd, 2)}</td>
                      <td className="mono" style={{ fontSize: "0.75rem" }}>
                        <span style={{ color: "#22c55e" }}>{num(f.book_bid, 3)}</span>
                        {" × "}
                        <span style={{ color: "#ef4444" }}>{num(f.book_ask, 3)}</span>
                      </td>
                      <td className="mono">{num(f.depth_at_level, 1)}</td>
                      <td className="mono" style={{ color: "#94a3b8" }}>{pct(f.arrival_prob)}</td>
                      <td className="mono" style={{ color: "#94a3b8" }}>{num(f.mean_taker, 1)}</td>
                      <td className="mono" style={{ color: "#94a3b8" }}>{num(f.taker_size_drawn, 1)}</td>
                      <td className="mono" style={{ color: "#64748b" }}>
                        {f.hl_mid ? `$${Number(f.hl_mid).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—"}
                      </td>
                      <td className="mono" style={{
                        color: Number(f.hl_move_pct) > 0 ? "#22c55e" : Number(f.hl_move_pct) < 0 ? "#ef4444" : "#64748b",
                      }}>
                        {f.hl_move_pct ? pct(f.hl_move_pct) : "—"}
                      </td>
                      <td style={{ textAlign: "center" }}>
                        {isAdverse ? (
                          <span title="Adverse selection — HL moved against fill direction"
                                style={{ color: "#ef4444", fontWeight: 700 }}>⚠</span>
                        ) : (
                          <span style={{ color: "#1e293b" }}>·</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "1rem", alignItems: "center" }}>
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              style={{ padding: "4px 12px", borderRadius: 4, border: "1px solid #334155", background: "transparent", color: "#94a3b8", cursor: offset === 0 ? "default" : "pointer" }}
            >
              ← Prev
            </button>
            <span className="muted">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <button
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              style={{ padding: "4px 12px", borderRadius: 4, border: "1px solid #334155", background: "transparent", color: "#94a3b8", cursor: offset + PAGE_SIZE >= total ? "default" : "pointer" }}
            >
              Next →
            </button>
          </div>
        </>
      )}
    </div>
  );
}
