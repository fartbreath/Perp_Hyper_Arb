/**
 * Pending Results — positions in limbo (CLOSING or PENDING_RESOLVE).
 *
 * Positions that have exited the bot's active state but are awaiting:
 *   CLOSING          — exit fill recorded, waiting for PM /activity confirmation
 *   PENDING_RESOLVE  — exit fill confirmed, waiting for market resolution on-chain
 *
 * Auto-refreshes every 15 s. Shows how long each position has been waiting.
 */
import { useAcctPending } from "../api/client";
import type { AcctPosition } from "../api/client";
import { timeSince, unrealizedPnl } from "./pendingUtils";

// ─── helpers ─────────────────────────────────────────────────────────────────

function relTime(iso: string | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-US", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch { return iso.slice(0, 16); }
}

function fmtPrice(n: number): string {
  if (!n || !isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}¢`;
}

function fmtUsd(n: number): string {
  if (!isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}

// ─── badges ──────────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const cfg: Record<string, { bg: string; fg: string; label: string }> = {
    closing:          { bg: "#431407", fg: "#fbbf24", label: "CLOSING" },
    pending_resolve:  { bg: "#1e3a5f", fg: "#60a5fa", label: "PENDING RESOLVE" },
  };
  const key = status.toLowerCase();
  const { bg, fg, label } = cfg[key] ?? { bg: "#374151", fg: "#94a3b8", label: status.toUpperCase() };
  return (
    <span style={{
      background: bg, border: `1px solid ${fg}`,
      borderRadius: 4, padding: "2px 8px",
      fontSize: 11, fontWeight: 700, color: fg,
    }}>{label}</span>
  );
}

function SideBadge({ side }: { side: string }) {
  const yes = side === "YES" || side === "UP";
  const no  = side === "NO"  || side === "DOWN";
  return (
    <span style={{
      background: yes ? "#166534" : no ? "#7f1d1d" : "#374151",
      borderRadius: 4, padding: "1px 6px",
      fontSize: 10, fontWeight: 700, color: "#f0fdf4",
    }}>{side || "—"}</span>
  );
}

function PmBadge({ confirmed, label }: { confirmed: boolean; label: string }) {
  return (
    <span style={{
      background: confirmed ? "#052e16" : "#1c1917",
      border: `1px solid ${confirmed ? "#166534" : "#57534e"}`,
      borderRadius: 3, padding: "1px 5px",
      fontSize: 9, fontWeight: 600,
      color: confirmed ? "#86efac" : "#78716c",
    }}>{label}: {confirmed ? "✓" : "?"}</span>
  );
}

function WaitingUrgency({ iso }: { iso: string | undefined }) {
  if (!iso) return <span style={{ color: "#64748b" }}>—</span>;
  const mins = (Date.now() - new Date(iso).getTime()) / 60_000;
  const color = mins > 60 ? "#ef4444" : mins > 30 ? "#f59e0b" : "#22c55e";
  return <span style={{ color, fontWeight: 600 }}>{timeSince(iso)}</span>;
}

// ─── main component ───────────────────────────────────────────────────────────

export default function Pending() {
  const { data, loading, error, refresh } = useAcctPending();
  const positions: AcctPosition[] = data?.positions ?? [];

  const closing        = positions.filter(p => p.status.toLowerCase() === "closing");
  const pendingResolve = positions.filter(p => p.status.toLowerCase() === "pending_resolve");

  return (
    <div style={{ padding: 20, color: "#e2e8f0", fontFamily: "Inter, system-ui, sans-serif" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>Pending Results</h1>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "#64748b" }}>
            Positions awaiting PM confirmation or market resolution · auto-refreshes every 15s
          </p>
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <span style={{
            background: "#431407", borderRadius: 6, padding: "6px 12px",
            fontSize: 12, color: "#fbbf24", fontWeight: 600,
          }}>
            Closing: {closing.length}
          </span>
          <span style={{
            background: "#1e3a5f", borderRadius: 6, padding: "6px 12px",
            fontSize: 12, color: "#60a5fa", fontWeight: 600,
          }}>
            Pending Resolve: {pendingResolve.length}
          </span>
          <button onClick={refresh} style={{
            background: "#1f2937", border: "1px solid #374151", borderRadius: 6,
            color: "#94a3b8", padding: "6px 14px", fontSize: 12, cursor: "pointer",
          }}>Refresh</button>
        </div>
      </div>

      {loading && <div style={{ color: "#64748b", padding: 20 }}>Loading…</div>}
      {error   && <div style={{ color: "#ef4444", padding: 20 }}>Error: {error}</div>}

      {!loading && positions.length === 0 && (
        <div style={{
          color: "#64748b", padding: 40, textAlign: "center",
          background: "#111827", borderRadius: 8, border: "1px solid #1f2937",
        }}>
          No pending positions. All exits have been confirmed.
        </div>
      )}

      {positions.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: "#0a0f18", borderBottom: "2px solid #1f2937" }}>
                {[
                  ["Market",       "left"  ],
                  ["Status",       "center"],
                  ["Strategy",     "center"],
                  ["Underlying",   "center"],
                  ["Side",         "center"],
                  ["Entry VWAP",   "right" ],
                  ["Exit VWAP",    "right" ],
                  ["Contracts",    "right" ],
                  ["Exit Type",    "center"],
                  ["Est. P&L",     "right" ],
                  ["PM Confirmed", "center"],
                  ["Opened",       "center"],
                  ["Waiting",      "center"],
                ].map(([h, align]) => (
                  <th key={h} style={{
                    padding: "8px 10px", fontSize: 10, fontWeight: 600, color: "#64748b",
                    textAlign: align as React.CSSProperties["textAlign"],
                    whiteSpace: "nowrap",
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map(pos => {
                const pnl = unrealizedPnl(pos);
                return (
                  <tr key={pos.pos_id}
                    style={{ borderBottom: "1px solid #1f2937", background: "#111827" }}>

                    {/* Market */}
                    <td style={{ padding: "10px 12px", minWidth: 200 }}>
                      <div style={{ fontSize: 12, fontWeight: 500, color: "#e2e8f0" }}
                        title={pos.market_id}>
                        {pos.market_title || pos.market_id?.slice(0, 20) || "—"}
                      </div>
                      <div style={{ fontSize: 10, color: "#64748b", marginTop: 2 }}>
                        {pos.market_type}
                      </div>
                    </td>

                    {/* Status */}
                    <td style={{ padding: "10px 8px", textAlign: "center" }}>
                      <StatusBadge status={pos.status} />
                    </td>

                    {/* Strategy */}
                    <td style={{ padding: "10px 8px", fontSize: 11, color: "#94a3b8", textAlign: "center" }}>
                      {pos.strategy || "—"}
                    </td>

                    {/* Underlying */}
                    <td style={{ padding: "10px 8px", fontSize: 12, fontWeight: 600, color: "#e2e8f0", textAlign: "center" }}>
                      {pos.underlying || "—"}
                    </td>

                    {/* Side */}
                    <td style={{ padding: "10px 8px", textAlign: "center" }}>
                      <SideBadge side={pos.side} />
                    </td>

                    {/* Entry VWAP */}
                    <td style={{ padding: "10px 8px", fontFamily: "monospace", color: "#93c5fd", textAlign: "right" }}>
                      {fmtPrice(pos.entry_vwap)}
                    </td>

                    {/* Exit VWAP */}
                    <td style={{ padding: "10px 8px", fontFamily: "monospace", color: "#fbbf24", textAlign: "right" }}>
                      {fmtPrice(pos.exit_vwap)}
                    </td>

                    {/* Contracts */}
                    <td style={{ padding: "10px 8px", fontFamily: "monospace", color: "#cbd5e1", textAlign: "right" }}>
                      {pos.entry_contracts > 0 ? pos.entry_contracts.toFixed(2) : "—"}
                    </td>

                    {/* Exit Type */}
                    <td style={{ padding: "10px 8px", fontSize: 11, color: "#94a3b8", textAlign: "center" }}>
                      {pos.exit_type || "—"}
                    </td>

                    {/* Est. P&L */}
                    <td style={{
                      padding: "10px 8px", fontFamily: "monospace", textAlign: "right",
                      fontWeight: 600, fontSize: 13,
                      color: pnl == null ? "#64748b" : pnl > 0 ? "#22c55e" : pnl < 0 ? "#ef4444" : "#94a3b8",
                    }}>
                      {pnl == null ? "—" : fmtUsd(pnl)}
                    </td>

                    {/* PM Confirmed */}
                    <td style={{ padding: "10px 8px" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "center" }}>
                        <PmBadge confirmed={pos.pm_entry_confirmed} label="Entry" />
                        <PmBadge confirmed={pos.pm_exit_confirmed}  label="Exit" />
                      </div>
                    </td>

                    {/* Opened */}
                    <td style={{ padding: "10px 8px", fontSize: 11, color: "#64748b", textAlign: "center", whiteSpace: "nowrap" }}>
                      {relTime(pos.entry_time)}
                    </td>

                    {/* Waiting since closing */}
                    <td style={{ padding: "10px 8px", textAlign: "center" }}>
                      <WaitingUrgency iso={pos.closing_since || pos.exit_time} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Info footer */}
      {positions.length > 0 && (
        <div style={{ marginTop: 16, fontSize: 11, color: "#374151", lineHeight: 1.6 }}>
          <strong style={{ color: "#fbbf24" }}>CLOSING</strong> — exit fill recorded by bot; waiting for PM /activity API to confirm the fill. Usually resolves in 1–5 min.
          <br />
          <strong style={{ color: "#60a5fa" }}>PENDING_RESOLVE</strong> — PM confirmed exit; waiting for market resolution on-chain. Can take minutes to days depending on market end date.
        </div>
      )}
    </div>
  );
}
