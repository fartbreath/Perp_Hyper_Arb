/**
 * Logs — Live structured bot log viewer.
 * Polls /logs every 2s. Filter by level, module, or free-text search.
 * "Error History" tab polls /logs/errors for the long-lived WARNING+ buffer.
 */
import { useState, useEffect, useRef } from "react";
import { useLogs, useErrorLogs } from "../api/client";
import type { LogEntry } from "../api/client";

// ── Level styling ─────────────────────────────────────────────────────────────

const LEVEL_STYLE: Record<string, { bg: string; color: string }> = {
  DEBUG:    { bg: "#1e293b",  color: "#64748b" },
  INFO:     { bg: "#1e3a5f",  color: "#93c5fd" },
  WARNING:  { bg: "#422006",  color: "#fcd34d" },
  ERROR:    { bg: "#450a0a",  color: "#fca5a5" },
  CRITICAL: { bg: "#4c0519",  color: "#f9a8d4" },
};

const LEVELS = ["ALL", "INFO", "WARNING", "ERROR"] as const;
type ViewMode = "live" | "errors";

// ── Single log row ────────────────────────────────────────────────────────────

function LogRow({ entry }: { entry: LogEntry }) {
  const [expanded, setExpanded] = useState(false);
  const style = LEVEL_STYLE[entry.level] ?? LEVEL_STYLE.INFO;
  const hasExtras = Object.keys(entry.extras).length > 0;
  const time = new Date(entry.ts * 1000).toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

  return (
    <div
      className="log-row"
      onClick={() => hasExtras && setExpanded((e) => !e)}
      style={{ cursor: hasExtras ? "pointer" : "default" }}
    >
      <span className="log-time">{time}</span>
      <span
        className="log-level"
        style={{ background: style.bg, color: style.color }}
      >
        {entry.level}
      </span>
      <span className="log-module">{entry.module}</span>
      <span className="log-msg">{entry.msg}</span>
      {hasExtras && !expanded && (
        <span className="log-extras-peek">
          {Object.entries(entry.extras)
            .slice(0, 4)
            .map(([k, v]) => (
              <span key={k} className="log-kv">
                <span className="log-k">{k}</span>
                <span className="log-v">{String(v)}</span>
              </span>
            ))}
          {Object.keys(entry.extras).length > 4 && (
            <span className="log-kv-more">+{Object.keys(entry.extras).length - 4} more</span>
          )}
        </span>
      )}
      {expanded && (
        <div className="log-extras-full">
          {Object.entries(entry.extras).map(([k, v]) => (
            <div key={k} className="log-kv-full">
              <span className="log-k">{k}</span>
              <span className="log-v">{String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Logs() {
  const [viewMode, setViewMode] = useState<ViewMode>("live");
  const [level, setLevel] = useState<string>("INFO");
  const [module, setModule] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [debouncedSearch, setDebouncedSearch] = useState<string>("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);

  // Reset module filter when switching view modes
  useEffect(() => { setModule(""); }, [viewMode]);

  // Debounce search input
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  const liveData = useLogs(
    300,
    paused ? "ALL" : level,
    module || undefined,
    debouncedSearch || undefined,
  );
  const errorData = useErrorLogs(
    500,
    module || undefined,
    debouncedSearch || undefined,
  );

  const data    = viewMode === "live" ? liveData.data    : errorData.data;
  const error   = viewMode === "live" ? liveData.error   : errorData.error;

  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to top (newest-first)
  useEffect(() => {
    if (autoScroll && !paused && containerRef.current) {
      containerRef.current.scrollTop = 0;
    }
  }, [data, autoScroll, paused]);

  const displayedLogs = data?.logs ?? [];

  return (
    <div className="page">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.75rem" }}>
        <h2>Logs</h2>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <span className="muted" style={{ fontSize: 12 }}>
            {data ? `${data.total} entries` : "–"}
          </span>
          {viewMode === "live" && (
            <button
              className={`toggle-btn ${paused ? "toggle-on-danger" : "toggle-off"}`}
              style={{ fontSize: 12, padding: "0.25rem 0.75rem" }}
              onClick={() => setPaused((p) => !p)}
            >
              {paused ? "▶ Resume" : "⏸ Pause"}
            </button>
          )}
        </div>
      </div>

      {/* View mode tabs */}
      <div className="period-tabs" style={{ marginBottom: "0.5rem" }}>
        <button
          className={viewMode === "live" ? "active" : ""}
          onClick={() => setViewMode("live")}
        >
          Live
        </button>
        <button
          className={viewMode === "errors" ? "active" : ""}
          style={viewMode === "errors" ? { background: LEVEL_STYLE.WARNING.bg, borderColor: LEVEL_STYLE.WARNING.color, color: LEVEL_STYLE.WARNING.color } : undefined}
          onClick={() => setViewMode("errors")}
        >
          Error History
        </button>
      </div>

      {/* Filters */}
      <div className="log-filters">
        {/* Level tabs — only shown in live mode */}
        {viewMode === "live" && (
          <div className="period-tabs">
            {LEVELS.map((l) => (
              <button
                key={l}
                className={level === l ? "active" : ""}
                onClick={() => setLevel(l)}
                style={
                  l !== "ALL" && level === l
                    ? { background: LEVEL_STYLE[l]?.bg, borderColor: LEVEL_STYLE[l]?.color, color: LEVEL_STYLE[l]?.color }
                    : undefined
                }
              >
                {l}
              </button>
            ))}
          </div>
        )}

        {/* Module filter */}
        <select
          className="log-select"
          value={module}
          onChange={(e) => setModule(e.target.value)}
        >
          <option value="">All modules</option>
          {(data?.modules ?? []).map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>

        {/* Search */}
        <input
          className="log-search"
          type="text"
          placeholder="Search…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />

        {/* Auto-scroll toggle */}
        {viewMode === "live" && (
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: 12, color: "#94a3b8", whiteSpace: "nowrap" }}>
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              style={{ accentColor: "#6366f1" }}
            />
            Auto-scroll
          </label>
        )}
      </div>

      {viewMode === "errors" && (
        <div className="muted" style={{ fontSize: 12, marginBottom: "0.5rem" }}>
          Showing WARNING+ entries from the long-lived error buffer (up to 20 000 entries, refreshed every 10 s).
        </div>
      )}

      {error && <div className="error">Log API error: {error}</div>}

      {/* Log list */}
      <div className="log-container" ref={containerRef}>
        {displayedLogs.length === 0 && !error && (
          <div className="muted" style={{ padding: "1rem", textAlign: "center" }}>
            No log entries match the current filters.
          </div>
        )}
        {displayedLogs.map((entry, i) => (
          <LogRow key={`${entry.ts}-${i}`} entry={entry} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
