/**
 * Events — Momentum strategy trading lifecycle event log.
 *
 * Displays the last N events from data/momentum_events.jsonl via the
 * GET /momentum/events?n=... endpoint.  Events are newest-first.
 * Each row shows: timestamp, event type badge, market title, side, and
 * event-specific details (price, size, reason, order ID, etc.).
 */
import { useState } from "react";
import { useMomentumEvents } from "../api/client";
import type { MomentumEvent } from "../api/client";

// ── Colour-coded event badges ─────────────────────────────────────────────────

const EVENT_COLOURS: Record<string, { bg: string; fg: string }> = {
  SESSION_START:        { bg: "#1e40af", fg: "#dbeafe" },
  BUY_SUBMIT:          { bg: "#92400e", fg: "#fef3c7" },
  BUY_FILL:            { bg: "#166534", fg: "#dcfce7" },
  BUY_CANCEL_TIMEOUT:  { bg: "#7c3aed", fg: "#ede9fe" },
  BUY_FAILED:          { bg: "#991b1b", fg: "#fee2e2" },
  SELL_SUBMIT:         { bg: "#0e7490", fg: "#cffafe" },
  SELL_CLOSE:          { bg: "#065f46", fg: "#d1fae5" },
  SELL_FAILED:         { bg: "#9f1239", fg: "#ffe4e6" },
};

function EventBadge({ event }: { event: string }) {
  const c = EVENT_COLOURS[event] ?? { bg: "#374151", fg: "#f9fafb" };
  return (
    <span
      style={{
        background: c.bg,
        color: c.fg,
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 12,
        fontFamily: "monospace",
        whiteSpace: "nowrap",
      }}
    >
      {event}
    </span>
  );
}

// ── Detail cell — compact key=value pairs for event-specific fields ───────────

const SKIP_KEYS = new Set([
  "schema_version", "ts", "event", "market_id", "market_title",
  "underlying", "market_type", "side",
]);

function EventDetails({ row }: { row: MomentumEvent }) {
  const entries = Object.entries(row)
    .filter(([k]) => !SKIP_KEYS.has(k))
    .map(([k, v]) => {
      if (typeof v === "number") {
        const disp = Number.isInteger(v) ? String(v) : v.toFixed(4);
        return `${k}=${disp}`;
      }
      if (typeof v === "boolean") return `${k}=${v}`;
      if (v === null || v === undefined) return null;
      return `${k}=${String(v).slice(0, 40)}`;
    })
    .filter(Boolean);
  return (
    <span style={{ fontSize: 12, color: "#94a3b8", fontFamily: "monospace" }}>
      {entries.join("  ")}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Events() {
  const [n, setN] = useState(200);
  const { data, loading, error, refresh } = useMomentumEvents(n);

  const events = data?.events ?? [];

  return (
    <div style={{ padding: "1rem 1.5rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Momentum Events</h2>
        <select
          value={n}
          onChange={e => setN(Number(e.target.value))}
          style={{ padding: "4px 8px", borderRadius: 4, background: "#1e293b", color: "#f1f5f9", border: "1px solid #334155" }}
        >
          {[50, 100, 200, 500].map(v => (
            <option key={v} value={v}>Last {v}</option>
          ))}
        </select>
        <button
          onClick={refresh}
          style={{ padding: "4px 12px", borderRadius: 4, cursor: "pointer", background: "#334155", color: "#f1f5f9", border: "none" }}
        >
          Refresh
        </button>
        {data && (
          <span style={{ color: "#64748b", fontSize: 13 }}>
            {data.count} event{data.count !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {loading && <p style={{ color: "#64748b" }}>Loading…</p>}
      {error && <p style={{ color: "#f87171" }}>Error: {error}</p>}

      {events.length === 0 && !loading && (
        <p style={{ color: "#64748b" }}>
          No events recorded yet. Events appear here when the Momentum scanner is active.
        </p>
      )}

      {events.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid #334155", textAlign: "left" }}>
                <th style={{ padding: "6px 10px", color: "#94a3b8", fontWeight: 500 }}>Time (UTC)</th>
                <th style={{ padding: "6px 10px", color: "#94a3b8", fontWeight: 500 }}>Event</th>
                <th style={{ padding: "6px 10px", color: "#94a3b8", fontWeight: 500 }}>Market</th>
                <th style={{ padding: "6px 10px", color: "#94a3b8", fontWeight: 500 }}>Side</th>
                <th style={{ padding: "6px 10px", color: "#94a3b8", fontWeight: 500 }}>Details</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => {
                const ts = ev.ts ? new Date(ev.ts).toLocaleTimeString("en-GB", {
                  hour: "2-digit", minute: "2-digit", second: "2-digit",
                  timeZone: "UTC",
                }) : "–";
                const date = ev.ts ? new Date(ev.ts).toLocaleDateString("en-GB", { timeZone: "UTC" }) : "";
                return (
                  <tr
                    key={i}
                    style={{
                      borderBottom: "1px solid #1e293b",
                      background: i % 2 === 0 ? "transparent" : "#0f172a22",
                    }}
                  >
                    <td style={{ padding: "5px 10px", color: "#64748b", whiteSpace: "nowrap" }}>
                      <span title={ev.ts ?? ""}>
                        {date} {ts}
                      </span>
                    </td>
                    <td style={{ padding: "5px 10px" }}>
                      <EventBadge event={ev.event} />
                    </td>
                    <td style={{ padding: "5px 10px", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      <span title={ev.market_title ?? ev.market_id ?? ""} style={{ color: "#e2e8f0" }}>
                        {ev.market_title ?? ev.market_id ?? "–"}
                      </span>
                    </td>
                    <td style={{ padding: "5px 10px", fontFamily: "monospace", color: "#38bdf8" }}>
                      {ev.side ?? "–"}
                    </td>
                    <td style={{ padding: "5px 10px" }}>
                      <EventDetails row={ev} />
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
