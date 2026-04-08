/**
 * Settings — Runtime bot configuration controls.
 *
 * Changes are sent to POST /config and take effect immediately.
 * The page polls GET /config every 5s so out-of-band changes are reflected.
 */
import { useState, type ReactNode } from "react";
import { useConfig, updateConfig } from "../api/client";
import type { ConfigData } from "../api/client";

// ── Primitives ────────────────────────────────────────────────────────────────

function Toggle({
  label,
  description,
  value,
  onChange,
  danger,
}: {
  label: string;
  description: string;
  value: boolean;
  onChange: (v: boolean) => void;
  danger?: boolean;
}) {
  return (
    <div className="settings-row">
      <div className="settings-label">
        <span className="settings-name">{label}</span>
        <span className="settings-desc">{description}</span>
      </div>
      <button
        className={`toggle-btn ${value ? (danger ? "toggle-on-danger" : "toggle-on") : "toggle-off"}`}
        onClick={() => onChange(!value)}
        aria-label={label}
      >
        {value ? "ON" : "OFF"}
      </button>
    </div>
  );
}

function NumberInput({
  label,
  description,
  value,
  step,
  unit,
  onSubmit,
}: {
  label: string;
  description: string;
  value: number;
  min?: number;
  max?: number;
  step: number;
  unit: string;
  onSubmit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  const [prevValue, setPrevValue] = useState(value);
  if (prevValue !== value) { setPrevValue(value); setDraft(String(value)); }
  const commit = () => {
    const n = parseInt(draft, 10);
    if (!Number.isNaN(n)) onSubmit(n);
    else setDraft(String(value));
  };
  return (
    <div className="settings-row">
      <div className="settings-label">
        <span className="settings-name">{label}</span>
        <span className="settings-desc">{description}</span>
      </div>
      <div className="settings-input-group">
        <input
          type="number"
          className="settings-input"
          value={draft}
          step={step}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
        />
        <span className="settings-unit">{unit}</span>
      </div>
    </div>
  );
}

function FloatInput({
  label,
  description,
  value,
  step,
  unit,
  onSubmit,
}: {
  label: string;
  description: string;
  value: number;
  min?: number;
  max?: number;
  step: number;
  unit: string;
  onSubmit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  const [prevValue, setPrevValue] = useState(value);
  if (prevValue !== value) { setPrevValue(value); setDraft(String(value)); }
  const commit = () => {
    const n = parseFloat(draft);
    if (!Number.isNaN(n)) onSubmit(n);
    else setDraft(String(value));
  };
  return (
    <div className="settings-row">
      <div className="settings-label">
        <span className="settings-name">{label}</span>
        <span className="settings-desc">{description}</span>
      </div>
      <div className="settings-input-group">
        <input
          type="number"
          className="settings-input"
          value={draft}
          step={step}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
        />
        <span className="settings-unit">{unit}</span>
      </div>
    </div>
  );
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function SaveBanner({ msg }: { msg: string | null }) {
  if (!msg) return null;
  const isErr = msg.startsWith("Error");
  return (
    <div
      className="banner"
      style={{
        background: isErr ? "#450a0a" : "#052e16",
        border: `1px solid ${isErr ? "#ef4444" : "#22c55e"}`,
        color: isErr ? "#fca5a5" : "#86efac",
        marginBottom: "0.75rem",
      }}
    >
      {msg}
    </div>
  );
}

function SectionHead({ title }: { title: string }) {
  return (
    <h4
      style={{
        color: "#64748b",
        fontSize: "0.75rem",
        textTransform: "uppercase",
        letterSpacing: "0.07em",
        margin: "1.5rem 0 0.6rem",
        paddingBottom: "0.35rem",
        borderBottom: "1px solid #1e293b",
      }}
    >
      {title}
    </h4>
  );
}

/** Collapsible section for advanced / rarely-changed settings. */
function Advanced({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: "1.25rem" }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          background: "none",
          border: "1px solid #1e293b",
          borderRadius: 6,
          color: "#64748b",
          fontSize: "0.8rem",
          cursor: "pointer",
          padding: "0.4rem 0.9rem",
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
        }}
      >
        <span style={{ fontSize: "0.65rem" }}>{open ? "▾" : "▸"}</span>
        {open ? "Hide advanced" : "Show advanced"}
      </button>
      {open && <div style={{ marginTop: "0.75rem" }}>{children}</div>}
    </div>
  );
}

/** Amber warning banner. */
function Warn({ children }: { children: ReactNode }) {
  return (
    <div
      className="banner info"
      style={{
        marginTop: "0.75rem",
        background: "#431407",
        border: "1px solid #f59e0b",
        color: "#fcd34d",
      }}
    >
      {children}
    </div>
  );
}

/** Blue informational note. */
function Note({ children }: { children: ReactNode }) {
  return (
    <div className="banner info" style={{ marginTop: "0.75rem" }}>
      {children}
    </div>
  );
}

/**
 * Collapsible strategy section — wraps all the cards for one strategy.
 * Shows a header bar with the strategy name + enabled badge that toggles
 * the body open/closed.
 */
function StrategySection({
  title,
  enabled,
  defaultOpen = false,
  children,
}: {
  title: string;
  enabled: boolean;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div
      style={{
        border: "1px solid #1e293b",
        borderRadius: 8,
        marginTop: "1rem",
        overflow: "hidden",
      }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          background: open ? "#0f172a" : "#090e1a",
          border: "none",
          borderBottom: open ? "1px solid #1e293b" : "none",
          padding: "0.8rem 1.25rem",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span style={{ color: "#64748b", fontSize: "0.75rem" }}>
          {open ? "▾" : "▸"}
        </span>
        <span style={{ color: "#e2e8f0", fontWeight: 600, fontSize: "1rem", flex: 1 }}>
          {title}
        </span>
        <span
          style={{
            fontSize: "0.72rem",
            fontWeight: 400,
            padding: "2px 8px",
            borderRadius: 3,
            background: enabled ? "#14532d" : "#1e293b",
            color: enabled ? "#86efac" : "#64748b",
          }}
        >
          {enabled ? "enabled" : "disabled"}
        </span>
      </button>
      {open && (
        <div style={{ padding: "0.25rem 0" }}>{children}</div>
      )}
    </div>
  );
}

const GAP = <div style={{ height: "0.75rem" }} />;

// ── Page ──────────────────────────────────────────────────────────────────────

export default function Settings() {
  const { data, error } = useConfig();
  const [saving, setSaving] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const showBanner = (msg: string) => {
    setBanner(msg);
    setTimeout(() => setBanner(null), 3000);
  };

  const apply = async (patch: Partial<ConfigData>) => {
    setSaving(true);
    try {
      await updateConfig(patch);
      showBanner(`✓ Saved: ${Object.keys(patch).join(", ")}`);
    } catch (e: unknown) {
      showBanner(`Error: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setSaving(false);
    }
  };

  if (error)
    return (
      <div className="page">
        <div className="error">API offline — {error}</div>
      </div>
    );
  if (!data)
    return (
      <div className="page">
        <div className="card skeleton" style={{ height: 300 }} />
      </div>
    );

  const makerOn = data.strategy_maker;
  const mispricingOn = data.strategy_mispricing;
  const momentumOn = data.strategy_momentum ?? false;
  const nakedClose = data.maker_naked_close_contracts ?? 15;
  const maxImbalance = data.maker_max_imbalance_contracts ?? 5;
  const bookAge = data.maker_max_book_age_secs ?? 0;

  return (
    <div className="page">
      <h2>Settings</h2>
      <SaveBanner msg={banner} />

      {/* ── 1. Bot Controls ──────────────────────────────────────────── */}
      <div className="card">
        <h3>Bot Controls</h3>

        <Toggle
          label="Paper Trading"
          description="All fills are simulated — no real orders placed."
          value={data.paper_trading}
          onChange={(v) => apply({ paper_trading: v })}
          danger={!data.paper_trading}
        />
        {!data.paper_trading && (
          <Warn>⚠️ LIVE mode — real orders will be placed on Polymarket.</Warn>
        )}

        {GAP}

        <Toggle
          label="Auto-Approve Signals"
          description="Signals are executed immediately without a CLI confirmation prompt."
          value={data.auto_approve}
          onChange={(v) => apply({ auto_approve: v })}
        />

        {GAP}

        <Toggle
          label="Agent Auto Mode"
          description="Agent decisions execute without any human prompt. Requires Auto-Approve ON."
          value={data.agent_auto}
          onChange={(v) => apply({ agent_auto: v })}
        />
        {data.agent_auto && !data.auto_approve && (
          <Note>ℹ️ Agent Auto requires Auto-Approve to also be ON.</Note>
        )}

        {data.paper_trading && (
          <>
            {GAP}
            <FloatInput
              label="Paper Capital"
              description="Virtual budget (USD) for capital-utilisation tracking."
              value={data.paper_capital_usd ?? 10000}
              step={500}
              unit="USD"
              onSubmit={(v) => apply({ paper_capital_usd: v })}
            />
          </>
        )}
      </div>

      {/* ── 2. Active Strategies ─────────────────────────────────────── */}
      <div className="card">
        <h3>Active Strategies</h3>
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Disabling a strategy stops new signals. Open positions continue
          closing normally.
        </p>

        <Toggle
          label="Market Making (Strategy 1)"
          description="Quote PM limit-order book with optional HL delta hedge."
          value={data.strategy_maker}
          onChange={(v) => apply({ strategy_maker: v })}
        />
        {data.strategy_maker && data.paper_trading && (
          <Note>ℹ️ Paper mode — orders are simulated by FillSimulator.</Note>
        )}

        {GAP}

        <Toggle
          label="Mispricing (Strategy 2)"
          description="Deribit N(d₂) / Kalshi implied-probability scan vs PM price."
          value={data.strategy_mispricing}
          onChange={(v) => apply({ strategy_mispricing: v })}
        />

        {GAP}

        <Toggle
          label="Momentum Scanner (Strategy 3)"
          description="Price-confirmation taker — buys into momentum when price exceeds vol-adjusted threshold."
          value={momentumOn}
          onChange={(v) => apply({ strategy_momentum: v })}
        />
      </div>

      {/* ── 3. Market Types ──────────────────────────────────────────── */}
      <div className="card">
        <h3>
          Market Types
          <span
            style={{
              fontSize: "0.75rem",
              color: "#64748b",
              fontWeight: 400,
              marginLeft: "0.5rem",
            }}
          >
            maker
          </span>
        </h3>
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Disable a bucket to stop new quotes of that type. Existing positions
          continue unaffected.
        </p>
        {(["bucket_5m", "bucket_15m", "bucket_1h"] as const).map((bucket) => {
          const excluded = (data.maker_excluded_market_types ?? []).includes(
            bucket
          );
          const desc: Record<string, string> = {
            bucket_5m: "5-minute markets. Highest volume, shortest lifetime.",
            bucket_15m:
              "15-minute markets. Balanced fill frequency and hold time.",
            bucket_1h: "1-hour markets. Lower frequency, longer position life.",
          };
          return (
            <Toggle
              key={bucket}
              label={bucket.replace("bucket_", "") + " bucket"}
              description={desc[bucket]}
              value={!excluded}
              onChange={(enabled) => {
                const cur = data.maker_excluded_market_types ?? [];
                apply({
                  maker_excluded_market_types: enabled
                    ? cur.filter((b) => b !== bucket)
                    : [...cur, bucket],
                });
              }}
            />
          );
        })}
      </div>

      {/* ═════════════ STRATEGY 1 — MARKET MAKING ══════════════════════ */}
      <StrategySection title="Strategy 1 — Market Making" enabled={makerOn} defaultOpen={makerOn}>
        {/* ── 4. Risk Limits ─────────────────────────────────────────── */}
          <div className="card">
            <h3>Risk Limits</h3>

            <NumberInput
              label="Max Maker Positions"
              description="Hard cap on concurrent maker positions."
              value={data.max_concurrent_maker_positions}
              step={1}
              unit=""
              onSubmit={(v) => apply({ max_concurrent_maker_positions: v })}
            />

            {GAP}

            <FloatInput
              label="Max PM Exposure / Market"
              description="Maximum collateral committed to one market (USD)."
              value={data.max_pm_exposure_per_market ?? 500}
              step={50}
              unit="USD"
              onSubmit={(v) => apply({ max_pm_exposure_per_market: v })}
            />

            {GAP}

            <FloatInput
              label="Total PM Exposure"
              description="Maximum total PM collateral across all open positions. New positions are blocked when reached (USD)."
              value={data.max_total_pm_exposure ?? 2000}
              step={100}
              unit="USD"
              onSubmit={(v) => apply({ max_total_pm_exposure: v })}
            />

            {GAP}

            <FloatInput
              label="Max Loss / Coin"
              description="Exits all open maker positions for a coin when aggregate unrealised loss hits this (USD)."
              value={data.maker_coin_max_loss_usd}
              step={5}
              unit="USD"
              onSubmit={(v) => apply({ maker_coin_max_loss_usd: v })}
            />

            {GAP}

            <FloatInput
              label="Hard Stop (Drawdown)"
              description="Emergency halt: bot pauses when cumulative session loss exceeds this amount (USD)."
              value={data.hard_stop_drawdown ?? 500}
              step={50}
              unit="USD"
              onSubmit={(v) => apply({ hard_stop_drawdown: v })}
            />

            <Advanced>
              <NumberInput
                label="Global Position Cap"
                description="Hard maximum concurrent open positions across ALL strategies."
                value={data.max_concurrent_positions}
                step={1}
                unit=""
                onSubmit={(v) => apply({ max_concurrent_positions: v })}
              />
              {GAP}
              <NumberInput
                label="Max Positions / Coin"
                description="Per-underlying open position cap."
                value={data.maker_positions_per_underlying}
                step={1}
                unit=""
                onSubmit={(v) => apply({ maker_positions_per_underlying: v })}
              />
            </Advanced>
          </div>

          {/* ── 5. Imbalance Controls ──────────────────────────────────── */}
          <div className="card">
            <h3>Imbalance Controls</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Three escalating responses when YES/NO fills diverge in a market:
              skew (automatic) → hard stop on new orders → taker force-close.
            </p>

            <NumberInput
              label="Max Fills Per Leg"
              description="Once YES or NO has been filled this many times, stop re-posting that leg. Prevents single-side fill traps. 0 = disabled."
              value={data.maker_max_fills_per_leg ?? 6}
              step={1}
              unit="fills"
              onSubmit={(v) => apply({ maker_max_fills_per_leg: v })}
            />

            {GAP}

            <NumberInput
              label="Max Imbalance — Hard Stop"
              description="Block all new orders on the heavy side when the YES/NO gap exceeds this."
              value={maxImbalance}
              step={1}
              unit="ct"
              onSubmit={(v) => apply({ maker_max_imbalance_contracts: v })}
            />

            {GAP}

            <Toggle
              label="Naked-Leg Auto-Close"
              description="When ON, imbalanced positions are taker-exited automatically once threshold + delay are met. When OFF, detection still runs and logs are written but no order is placed — the AI agent or operator decides."
              value={data.maker_naked_close_enabled ?? true}
              onChange={(v) => apply({ maker_naked_close_enabled: v })}
            />

            {GAP}

            <NumberInput
              label="Naked-Leg Close — Threshold"
              description="Taker-exit the excess when one side leads by this many contracts AND persists for the delay below."
              value={nakedClose}
              step={1}
              unit="ct"
              onSubmit={(v) => apply({ maker_naked_close_contracts: v })}
            />
            {nakedClose < maxImbalance && (
              <Warn>
                ⚠️ Naked-close threshold ({nakedClose} ct) is below max-imbalance
                hard stop ({maxImbalance} ct). The force-close timer starts while
                heavy-side orders are <em>still allowed</em> — the gap can grow
                from {nakedClose} to {maxImbalance} ct before new orders are
                blocked. Set Naked-Leg ≥ Max Imbalance so the hard stop fires
                first.
              </Warn>
            )}

            {GAP}

            <FloatInput
              label="Naked-Leg Close — Delay"
              description="The imbalance must persist for this long before the force-close fires. Prevents false triggers on brief sweeps."
              value={data.maker_naked_close_secs ?? 10}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ maker_naked_close_secs: v })}
            />

            {GAP}

            <FloatInput
              label="Min Spread Profit Margin"
              description="Minimum combined edge when quoting the 2nd leg. Prevents negative-spread entries when mid drifts between the YES and NO fill."
              value={data.maker_min_spread_profit_margin ?? 0.005}
              step={0.001}
              unit=""
              onSubmit={(v) => apply({ maker_min_spread_profit_margin: v })}
            />
          </div>

          {/* ── 6. Market Filter ───────────────────────────────────────── */}
          <div className="card">
            <h3>Market Filter</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Gates that control which markets are eligible for quoting.
            </p>

            <SectionHead title="Signal Score Thresholds" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Score combines edge quality, volume run-rate, price balance, and
              lifecycle TTE (0–100). Per-type overrides take precedence over the
              global floor when non-zero.
            </p>

            {/* 2-column compact grid for score thresholds */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0",
              }}
            >
              <FloatInput
                label="Global Floor"
                description="All types without a specific override."
                value={data.min_signal_score_maker ?? 0}
                step={1}
                unit="/ 100"
                onSubmit={(v) => apply({ min_signal_score_maker: v })}
              />
              <FloatInput
                label="5m Bucket Override"
                description="Override for bucket_5m. 0 = use global."
                value={data.maker_min_signal_score_5m ?? 0}
                step={1}
                unit="/ 100"
                onSubmit={(v) => apply({ maker_min_signal_score_5m: v })}
              />
              <FloatInput
                label="1h Bucket Override"
                description="Override for bucket_1h. 0 = use global."
                value={data.maker_min_signal_score_1h ?? 0}
                step={1}
                unit="/ 100"
                onSubmit={(v) => apply({ maker_min_signal_score_1h: v })}
              />
              <FloatInput
                label="4h Bucket Override"
                description="Override for bucket_4h. 0 = use global."
                value={data.maker_min_signal_score_4h ?? 0}
                step={1}
                unit="/ 100"
                onSubmit={(v) => apply({ maker_min_signal_score_4h: v })}
              />
            </div>

            <SectionHead title="Market Eligibility" />

            <FloatInput
              label="Min Volume (24h)"
              description="Skip markets below this 24h volume. Scaled by fraction-of-life elapsed for bucket markets (USD)."
              value={data.maker_min_volume_24hr ?? 5000}
              step={500}
              unit="USD"
              onSubmit={(v) => apply({ maker_min_volume_24hr: v })}
            />

            {GAP}

            <FloatInput
              label="Min Quote Price"
              description="Skip markets where YES mid is below this or above (1 − this). Avoids deeply one-sided markets."
              value={data.maker_min_quote_price ?? 0.05}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ maker_min_quote_price: v })}
            />

            {GAP}

            <NumberInput
              label="Max TTE Days"
              description="Skip markets resolving more than this many days out."
              value={data.maker_max_tte_days ?? 14}
              step={1}
              unit="days"
              onSubmit={(v) => apply({ maker_max_tte_days: v })}
            />

            {GAP}

            <NumberInput
              label="Min Depth to Quote"
              description="Minimum CLOB depth (contracts) on both bid and ask before posting. 0 = disabled."
              value={data.maker_min_depth_to_quote ?? 0}
              step={5}
              unit="ct"
              onSubmit={(v) => apply({ maker_min_depth_to_quote: v })}
            />

            <Advanced>
              <FloatInput
                label="Min Incentive Spread"
                description="Skip markets where PM max_incentive_spread is below this — insufficient rebate after fees."
                value={data.maker_min_incentive_spread ?? 0.04}
                step={0.005}
                unit=""
                onSubmit={(v) => apply({ maker_min_incentive_spread: v })}
              />
              {GAP}
              <FloatInput
                label="Min Edge"
                description="Minimum fee-adjusted spread edge before quoting a market."
                value={+(data.maker_min_edge_pct * 100).toFixed(3)}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ maker_min_edge_pct: v / 100 })}
              />
              {GAP}
              <NumberInput
                label="Max Contracts / Market"
                description="Hard cap on total YES+NO contracts in a single market. No new quotes once reached until positions close."
                value={data.maker_max_contracts_per_market ?? 500}
                step={50}
                unit="ct"
                onSubmit={(v) => apply({ maker_max_contracts_per_market: v })}
              />
            </Advanced>
          </div>

          {/* ── 7. Quoting Behavior ─────────────────────────────────────── */}
          <div className="card">
            <h3>Quoting Behavior</h3>

            <SectionHead title="Reprice & Quote Guards" />

            <FloatInput
              label="Reprice Trigger"
              description="HL mid must move by at least this % before PM quotes are repriced."
              value={+(data.reprice_trigger_pct * 100).toFixed(3)}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ reprice_trigger_pct: v / 100 })}
            />

            {GAP}

            <NumberInput
              label="Max Quote Age"
              description="Cancel and repost any quote older than this regardless of price movement. Also picks up newly-liquid markets."
              value={data.max_quote_age_seconds}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ max_quote_age_seconds: v })}
            />

            {GAP}

            <FloatInput
              label="Vol Filter"
              description="Pause quoting when HL 5-min realised move exceeds this %. Adverse selection dominates during fast moves."
              value={(data.maker_vol_filter_pct ?? 0.015) * 100}
              step={0.1}
              unit="%"
              onSubmit={(v) => apply({ maker_vol_filter_pct: v / 100 })}
            />

            {GAP}

            <FloatInput
              label="Adverse Drift Reprice"
              description="Force-reprice a partially-filled quote when mid has drifted more than this % from the quote price."
              value={(data.maker_adverse_drift_reprice ?? 0.01) * 100}
              step={0.1}
              unit="%"
              onSubmit={(v) => apply({ maker_adverse_drift_reprice: v / 100 })}
            />

            <SectionHead title="Order Sizing" />

            <FloatInput
              label="Spread Budget % of 24h Volume"
              description="Total spread budget (YES + NO combined) as % of market 24h volume. Contracts per side are derived so both legs share it equally. Clamped to Min–Max."
              value={(data.maker_quote_size_pct ?? 0.04) * 100}
              step={0.1}
              unit="%"
              onSubmit={(v) => apply({ maker_quote_size_pct: v / 100 })}
            />

            {GAP}

            <FloatInput
              label="Min Spread Budget"
              description="Floor: minimum total spread budget (USD). Applies when volume-derived size falls below this."
              value={data.maker_quote_size_min ?? 125}
              step={1}
              unit="USD"
              onSubmit={(v) => apply({ maker_quote_size_min: v })}
            />

            {GAP}

            <FloatInput
              label="Max Spread Budget"
              description="Ceiling: maximum total spread budget (USD), regardless of volume."
              value={data.maker_quote_size_max ?? 575}
              step={10}
              unit="USD"
              onSubmit={(v) => apply({ maker_quote_size_max: v })}
            />

            {GAP}

            <NumberInput
              label="Batch Size"
              description="Maximum contracts per side per order. Limits per-sweep exposure before the next reprice cycle re-checks fill balance."
              value={data.maker_batch_size ?? 50}
              step={5}
              unit="ct"
              onSubmit={(v) => apply({ maker_batch_size: v })}
            />

            <SectionHead title="Lifecycle Gates" />

            <FloatInput
              label="Exit TTE Fraction"
              description="Stop quoting when this fraction of the market's life remains (e.g. 0.20 = block in the last 20%)."
              value={data.maker_exit_tte_frac ?? 0.1}
              step={0.01}
              unit="fraction"
              onSubmit={(v) => apply({ maker_exit_tte_frac: v })}
            />

            {GAP}

            <FloatInput
              label="Exit TTE Fraction — 5m override"
              description="Override for bucket_5m only (e.g. 0.35 = stop with 1m 45s left). 0 = use global fraction."
              value={data.maker_exit_tte_frac_5m ?? 0}
              step={0.05}
              unit="fraction"
              onSubmit={(v) => apply({ maker_exit_tte_frac_5m: v })}
            />

            {GAP}

            <FloatInput
              label="Near-Expiry Exit (milestone markets)"
              description="Close maker positions when this many hours of TTE remain. Not applied to bucket markets — use Exit TTE Fraction instead."
              value={data.maker_exit_hours}
              step={0.5}
              unit="hours"
              onSubmit={(v) => apply({ maker_exit_hours: v })}
            />

            <SectionHead title="Deployment" />

            <div className="settings-row">
              <div className="settings-label">
                <span className="settings-name">Mode</span>
                <span className="settings-desc">
                  <em>auto</em>: place quotes immediately on signal;{" "}
                  <em>manual</em>: wait for explicit deploy from the Signals
                  page.
                </span>
              </div>
              <select
                value={data.deployment_mode ?? "auto"}
                onChange={(e) => apply({ deployment_mode: e.target.value })}
                style={{
                  padding: "0.35rem 0.6rem",
                  background: "#0f172a",
                  color: "#e2e8f0",
                  border: "1px solid #334155",
                  borderRadius: 4,
                  fontSize: "0.9rem",
                }}
              >
                <option value="auto">auto</option>
                <option value="manual">manual</option>
              </select>
            </div>

            <Advanced>
              <SectionHead title="Max Book Age (stale gate)" />
              <NumberInput
                label="Max Book Age"
                description="Skip repricing when PM order book is older than this. 0 = disabled (recommended). WARNING: values > 0 block ALL new quotes during normal 60-second WS reconnects."
                value={bookAge}
                step={5}
                unit="s"
                onSubmit={(v) =>
                  apply({ maker_max_book_age_secs: Math.round(v) })
                }
              />
              {bookAge > 0 && (
                <Warn>
                  ⚠️ Book age gate is active ({bookAge}s). WS reconnects happen
                  every ~60s and briefly leave books stale — this gate will
                  block ALL new quotes until the next WS update arrives. Set to
                  0 to keep quoting through reconnects.
                </Warn>
              )}

              <SectionHead title="Entry Cooldown" />
              <FloatInput
                label="Entry TTE Fraction"
                description="Don't quote until this fraction of the market's life has elapsed. Prevents adverse selection from opening flow. 0 = disabled."
                value={data.maker_entry_tte_frac ?? 0.1}
                step={0.01}
                unit="fraction"
                onSubmit={(v) => apply({ maker_entry_tte_frac: v })}
              />

              <SectionHead title="New Market Logic" />
              <NumberInput
                label="New Market Window"
                description="A market is treated as 'new' for this many seconds after listing. New markets get a wider default spread."
                value={data.new_market_age_limit ?? 3600}
                step={60}
                unit="s"
                onSubmit={(v) => apply({ new_market_age_limit: v })}
              />
              {GAP}
              <FloatInput
                label="Wide Spread (new market)"
                description="Half-spread used when quoting a newly-listed market."
                value={data.new_market_wide_spread ?? 0.08}
                step={0.005}
                unit=""
                onSubmit={(v) => apply({ new_market_wide_spread: v })}
              />
              {GAP}
              <FloatInput
                label="Pull Spread (new market)"
                description="If a competitor quotes inside this spread, tighten to match instead of posting the wide quote."
                value={data.new_market_pull_spread ?? 0.02}
                step={0.001}
                unit=""
                onSubmit={(v) => apply({ new_market_pull_spread: v })}
              />
              {GAP}
              <FloatInput
                label="New Market Size Fallback"
                description="Total spread budget (USD) for newly-listed markets that have no 24h volume yet. Prevents the budget formula returning zero on brand-new markets."
                value={data.maker_quote_size_new_market ?? 100}
                step={10}
                unit="USD"
                onSubmit={(v) => apply({ maker_quote_size_new_market: v })}
              />

              <SectionHead title="CLOB Depth Spread Widening" />
              <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
                Thin books widen the half-spread. Depth = 0 uses the empty-book
                factor; below the threshold uses linear interpolation.
              </p>
              <NumberInput
                label="Thin Book Threshold"
                description="Below this depth (ct) the spread is widened by the thin factor."
                value={data.maker_depth_thin_threshold ?? 50}
                step={10}
                unit="ct"
                onSubmit={(v) => apply({ maker_depth_thin_threshold: v })}
              />
              {GAP}
              <FloatInput
                label="Spread Factor (thin book)"
                description="Half-spread multiplier when depth is below the thin threshold. 1.0 = no widening."
                value={data.maker_depth_spread_factor_thin ?? 1.0}
                step={0.1}
                unit="×"
                onSubmit={(v) => apply({ maker_depth_spread_factor_thin: v })}
              />
              {GAP}
              <FloatInput
                label="Spread Factor (empty book)"
                description="Half-spread multiplier when depth is exactly zero."
                value={data.maker_depth_spread_factor_zero ?? 1.0}
                step={0.1}
                unit="×"
                onSubmit={(v) => apply({ maker_depth_spread_factor_zero: v })}
              />

              <SectionHead title="Inventory Skew (coin level)" />
              <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
                Symmetrically shifts bid and ask based on aggregate coin
                inventory. Net-long position moves the mid lower to attract
                sellers.
              </p>
              <FloatInput
                label="Skew Coefficient"
                description="Price shift per USD of net coin inventory."
                value={data.inventory_skew_coeff ?? 0.0001}
                step={0.00001}
                unit=""
                onSubmit={(v) => apply({ inventory_skew_coeff: v })}
              />
              {GAP}
              <FloatInput
                label="Skew Cap"
                description="Maximum total price shift from coin-level inventory skew."
                value={data.inventory_skew_max ?? 0.01}
                step={0.001}
                unit=""
                onSubmit={(v) => apply({ inventory_skew_max: v })}
              />

              <SectionHead title="Per-Market Imbalance Skew" />
              <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
                Only tightens the lagging leg — attracting fills to close the
                YES/NO gap without sacrificing edge on the overweight side.
              </p>
              <FloatInput
                label="Skew Coefficient"
                description="Price improvement per excess contract on the lagging leg."
                value={data.maker_imbalance_skew_coeff ?? 0.0005}
                step={0.0001}
                unit=""
                onSubmit={(v) => apply({ maker_imbalance_skew_coeff: v })}
              />
              {GAP}
              <FloatInput
                label="Skew Cap"
                description="Maximum price improvement on the lagging leg."
                value={data.maker_imbalance_skew_max ?? 0.03}
                step={0.005}
                unit=""
                onSubmit={(v) => apply({ maker_imbalance_skew_max: v })}
              />
              {GAP}
              <FloatInput
                label="Min Imbalance to Trigger"
                description="Minimum YES/NO contract gap before per-market skew activates. Filters out small natural fluctuations."
                value={data.maker_imbalance_skew_min_ct ?? 10}
                step={5}
                unit="ct"
                onSubmit={(v) => apply({ maker_imbalance_skew_min_ct: v })}
              />
            </Advanced>
          </div>

          {/* ── 8. HL Delta Hedge ──────────────────────────────────────── */}
          <div className="card">
            <h3>HL Delta Hedge</h3>

            <Toggle
              label="Hedge Enabled"
              description="When OFF, PM fills accumulate unhedged. Also controllable from the Dashboard position strips."
              value={data.maker_hedge_enabled ?? true}
              onChange={(v) => apply({ maker_hedge_enabled: v })}
            />
            {!(data.maker_hedge_enabled ?? true) && (
              <Warn>
                ⚠️ Delta hedge is OFF — directional PM exposure will accumulate
                without HL offset.
              </Warn>
            )}

            {GAP}

            <FloatInput
              label="Hedge Threshold"
              description="Minimum net inventory (USD) per coin before a hedge is placed."
              value={data.hedge_threshold_usd}
              step={10}
              unit="USD"
              onSubmit={(v) => apply({ hedge_threshold_usd: v })}
            />

            {GAP}

            <FloatInput
              label="Max HL Notional"
              description="Hard ceiling on any single HL hedge trade (USD notional)."
              value={data.max_hl_notional ?? 3000}
              step={100}
              unit="USD"
              onSubmit={(v) => apply({ max_hl_notional: v })}
            />

            {GAP}

            <FloatInput
              label="Rebalance Threshold"
              description="Only resize an existing hedge when the required change exceeds this % of the current hedge notional. Ignores minor drift."
              value={(data.hedge_rebalance_pct ?? 0.2) * 100}
              step={1}
              unit="%"
              onSubmit={(v) => apply({ hedge_rebalance_pct: v / 100 })}
            />

            {GAP}

            <FloatInput
              label="Min Interval"
              description="Per-coin cooldown (seconds) between two executed HL hedges."
              value={data.hedge_min_interval ?? 8}
              step={1}
              unit="s"
              onSubmit={(v) => apply({ hedge_min_interval: v })}
            />

            {GAP}

            <FloatInput
              label="Debounce"
              description="Batch window (seconds): wait for additional fills before executing a combined hedge. 0 = disabled."
              value={data.hedge_debounce_secs ?? 3}
              step={0.5}
              unit="s"
              onSubmit={(v) => apply({ hedge_debounce_secs: v })}
            />
          </div>

          {/* ── 9. Paper Fill Simulation (paper mode only) ────────────── */}
          {data.paper_trading && (
            <div className="card">
              <h3>Paper Fill Simulation</h3>
              <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
                Controls synthetic fill generation by FillSimulator. Has no
                effect in live mode.
              </p>

              <FloatInput
                label="Fill Probability (normal)"
                description="Base probability a paper quote gets filled each sweep."
                value={data.paper_fill_prob_base}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ paper_fill_prob_base: v })}
              />

              {GAP}

              <FloatInput
                label="Fill Probability (new market)"
                description="Higher probability for newly-discovered markets where spreads tend to be tighter."
                value={data.paper_fill_prob_new_market}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ paper_fill_prob_new_market: v })}
              />

              <Advanced>
                <FloatInput
                  label="Adverse Selection Trigger"
                  description="HL mid must move more than this fraction between sweeps to trigger the adverse-selection fill penalty."
                  value={data.paper_adverse_selection_pct}
                  step={0.001}
                  unit=""
                  onSubmit={(v) => apply({ paper_adverse_selection_pct: v })}
                />
                {GAP}
                <FloatInput
                  label="Adverse Fill Multiplier"
                  description="Multiply fill probability by this when adverse selection is detected. 0.15 = 85% reduction."
                  value={data.paper_adverse_fill_multiplier ?? 0.15}
                  step={0.01}
                  unit=""
                  onSubmit={(v) =>
                    apply({ paper_adverse_fill_multiplier: v })
                  }
                />
              </Advanced>
            </div>
          )}
      </StrategySection>

      {/* ═════════════ STRATEGY 2 — MISPRICING ═════════════════════════ */}
      <StrategySection title="Strategy 2 — Mispricing" enabled={mispricingOn} defaultOpen={mispricingOn}>
        <div className="card" style={{ opacity: mispricingOn ? 1 : 0.55 }}>

        {!mispricingOn && (
          <p className="settings-desc">
            Enable the strategy above to configure it.
          </p>
        )}

        {mispricingOn && (
          <>
            <NumberInput
              label="Scan Interval"
              description="How often the scanner runs a full sweep."
              value={data.mispricing_scan_interval}
              step={10}
              unit="s"
              onSubmit={(v) => apply({ mispricing_scan_interval: v })}
            />
            {data.mispricing_scan_interval < 60 && (
              <Note>ℹ️ Intervals below 60s may hit Deribit rate limits.</Note>
            )}

            {GAP}

            <NumberInput
              label="Max Concurrent Positions"
              description="Maximum concurrent open positions from the mispricing strategy."
              value={data.max_concurrent_mispricing_positions}
              step={1}
              unit=""
              onSubmit={(v) =>
                apply({ max_concurrent_mispricing_positions: v })
              }
            />

            {GAP}

            <FloatInput
              label="Min Signal Score"
              description="Discard signals scoring below this threshold (0–100). 0 = allow all."
              value={data.min_signal_score_mispricing ?? 0}
              step={5}
              unit="/ 100"
              onSubmit={(v) => apply({ min_signal_score_mispricing: v })}
            />

            <SectionHead title="Entry Filters" />

            <FloatInput
              label="Max YES Price for BUY_NO"
              description="Skip BUY_NO signals when YES price is at or above this level. Avoids near-certain YES markets where N(d₂) mismatch is most severe."
              value={data.max_buy_no_yes_price ?? 0.87}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ max_buy_no_yes_price: v })}
            />

            {GAP}

            <FloatInput
              label="Min Strike Distance"
              description="Require strike to be at least this far from current spot (fraction of spot). N(d₂) is most unreliable near-the-money; 0.08 = 8% distance required."
              value={data.min_strike_distance_pct ?? 0.08}
              step={0.01}
              unit="fraction"
              onSubmit={(v) => apply({ min_strike_distance_pct: v })}
            />

            {GAP}

            <NumberInput
              label="Per-Market Cooldown"
              description="After a signal fires for a market, suppress re-entry for this long. Deduplicates adjacent scans; the entry price filter handles persistent false signals."
              value={data.mispricing_market_cooldown_seconds ?? 300}
              step={60}
              unit="s"
              onSubmit={(v) => apply({ mispricing_market_cooldown_seconds: v })}
            />

            <SectionHead title="Kalshi Signal Source" />

            <Toggle
              label="Kalshi Enabled"
              description="Use Kalshi prices as the primary signal source."
              value={data.kalshi_enabled}
              onChange={(v) => apply({ kalshi_enabled: v })}
            />

            {data.kalshi_enabled && (
              <>
                {GAP}
                <Toggle
                  label="Require N(d₂) Confirmation"
                  description="Both Kalshi spread and N(d₂) direction must agree before a signal fires. Recommended."
                  value={data.kalshi_require_nd2_confirmation}
                  onChange={(v) =>
                    apply({ kalshi_require_nd2_confirmation: v })
                  }
                />
                {!data.kalshi_require_nd2_confirmation && (
                  <Warn>⚠️ Kalshi-only mode — N(d₂) confirmation bypassed.</Warn>
                )}
                {GAP}
                <FloatInput
                  label="Min Deviation"
                  description="Minimum |PM − Kalshi| spread required to fire a signal."
                  value={data.kalshi_min_deviation}
                  step={0.01}
                  unit=""
                  onSubmit={(v) => apply({ kalshi_min_deviation: v })}
                />
              </>
            )}

            <SectionHead title="Exit Rules" />

            <FloatInput
              label="Profit Target"
              description="Take profit at this fraction of the initial maximum theoretical gain."
              value={(data.profit_target_pct ?? 0.6) * 100}
              step={5}
              unit="%"
              onSubmit={(v) => apply({ profit_target_pct: v / 100 })}
            />

            {GAP}

            <FloatInput
              label="Stop Loss"
              description="Close immediately if unrealised loss exceeds this amount (USD)."
              value={data.stop_loss_usd ?? 25}
              step={5}
              unit="USD"
              onSubmit={(v) => apply({ stop_loss_usd: v })}
            />

            {GAP}

            <NumberInput
              label="Exit Before Resolution"
              description="Force-close positions this many days before market resolves."
              value={data.exit_days_before_resolution ?? 3}
              step={1}
              unit="days"
              onSubmit={(v) => apply({ exit_days_before_resolution: v })}
            />
          </>
        )}
        </div>
      </StrategySection>

      {saving && (
        <div className="muted" style={{ textAlign: "center", padding: "0.5rem" }}>
          Saving…
        </div>
      )}

      {/* ═════════════ STRATEGY 3 — MOMENTUM SCANNER ══════════════════ */}
      <StrategySection title="Strategy 3 — Momentum" enabled={momentumOn} defaultOpen={momentumOn}>
        {!momentumOn && (
          <div className="card" style={{ opacity: 0.55 }}>
            <p className="settings-desc">Enable the strategy above to configure it.</p>
          </div>
        )}

        {momentumOn && (
          <>
          <div className="card">
            <h3>Momentum — Entry Parameters</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Controls which markets qualify and how entries are sized.
            </p>

            <SectionHead title="Price Band" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Only markets with one side in this range are eligible. Typical: 0.80–0.90
              (the pre-resolution compression zone).
            </p>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="Band Low"
                description="Token price floor of the momentum window."
                value={data.momentum_price_band_low ?? 0.80}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ momentum_price_band_low: v })}
              />
              <FloatInput
                label="Band High"
                description="Token price ceiling of the momentum window."
                value={data.momentum_price_band_high ?? 0.90}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ momentum_price_band_high: v })}
              />
            </div>

            <SectionHead title="Position Sizing — Fractional Kelly" />
            <div className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              <p style={{ margin: 0 }}>
                How much collateral to deploy per signal, scaled by how good the signal actually is.
              </p>
              <p style={{ margin: "0.5rem 0 0" }}>
                <strong>How it works:</strong> Kelly Criterion asks: given our estimated win probability
                and the payout odds, what fraction of the bankroll is mathematically optimal to bet?
                We then apply a safety multiplier (<em>Kelly Fraction</em>) to stay conservative — because
                our win-probability model isn't perfect.
              </p>
              <p style={{ margin: "0.5rem 0 0" }}><strong>Why this is better than a fixed size:</strong></p>
              <ul style={{ margin: "0.25rem 0 0 1.2rem", padding: 0 }}>
                <li>A signal at 3σ gets more collateral than one at 1.65σ — automatically.</li>
                <li>A token priced at 0.82 gets more than one at 0.88 — because the payout odds are better low in the band.</li>
                <li>Marginal signals (barely above threshold) deploy near the $1 floor. Strong signals approach the maximum.</li>
              </ul>
              <p style={{ margin: "0.5rem 0 0" }}><strong>Quick calibration guide:</strong></p>
              <ul style={{ margin: "0.25rem 0 0 1.2rem", padding: 0 }}>
                <li><strong>1.00</strong> (default) — deploy <em>kelly_f × Max Entry</em> directly. e.g. kelly_f=0.75 → $37.50 at a $50 ceiling.</li>
                <li><strong>0.50</strong> — half-Kelly: scales every bet to 50% of the above. More conservative.</li>
                <li><strong>0.25</strong> — quarter-Kelly: sizes every bet at 25% of kelly_f × Max Entry. Most conservative.</li>
              </ul>
            </div>

            <FloatInput
              label="Max Entry (USD)"
              description="Absolute ceiling per position. Kelly never deploys more than this."
              value={data.momentum_max_entry_usd ?? 50}
              step={5}
              unit="USD"
              onSubmit={(v) => apply({ momentum_max_entry_usd: v })}
            />

            {GAP}

            <FloatInput
              label="Kelly Fraction"
              description={
                "Safety multiplier on kelly_f. 1.0 (default) = deploy kelly_f × Max Entry directly. " +
                "0.5 = half-Kelly (deploy 0.5 × kelly_f × Max Entry). " +
                "Lower = more conservative; higher is not meaningful above 1.0. " +
                "Signals near the entry threshold auto-size near the $1 floor; strong signals approach Max Entry."
              }
              value={data.momentum_kelly_fraction ?? 1.0}
              step={0.05}
              unit=""
              onSubmit={(v) => apply({ momentum_kelly_fraction: v })}
            />

            {GAP}

            <FloatInput
              label="Min CLOB Depth"
              description="Minimum total ask-side depth (USD) required before entering. Prevents entering illiquid markets."
              value={data.momentum_min_clob_depth ?? 200}
              step={25}
              unit="USD"
              onSubmit={(v) => apply({ momentum_min_clob_depth: v })}
            />

            {GAP}

            <SectionHead title="Entry Window by Market Type" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Entry window per market type — only enter when the market has <em>at most</em> this many seconds remaining.
              Markets with more time left are skipped until the window opens.
              e.g. set Daily to 900 to restrict entries to the final 15 minutes before settlement.
            </p>

            <NumberInput
              label="5-Minute"
              description="bucket_5m — only enter in the final N seconds before expiry."
              value={data.momentum_min_tte_5m ?? 30}
              step={10}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_5m: v })}
            />

            {GAP}

            <NumberInput
              label="15-Minute"
              description="bucket_15m — only enter in the final N seconds before expiry."
              value={data.momentum_min_tte_15m ?? 60}
              step={15}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_15m: v })}
            />

            {GAP}

            <NumberInput
              label="1-Hour"
              description="bucket_1h — only enter in the final N seconds before expiry."
              value={data.momentum_min_tte_1h ?? 120}
              step={30}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_1h: v })}
            />

            {GAP}

            <NumberInput
              label="4-Hour"
              description="bucket_4h — only enter in the final N seconds before expiry."
              value={data.momentum_min_tte_4h ?? 300}
              step={60}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_4h: v })}
            />

            {GAP}

            <NumberInput
              label="Daily"
              description="bucket_daily — e.g. 900 = only enter in the final 15 minutes before settlement."
              value={data.momentum_min_tte_daily ?? 900}
              step={60}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_daily: v })}
            />

            {GAP}

            <NumberInput
              label="Weekly"
              description="bucket_weekly — e.g. 3600 = only enter in the final hour before settlement."
              value={data.momentum_min_tte_weekly ?? 3600}
              step={300}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_weekly: v })}
            />

            {GAP}

            <NumberInput
              label="Milestone"
              description="milestone — only enter in the final N seconds before expiry."
              value={data.momentum_min_tte_milestone ?? 1800}
              step={300}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_milestone: v })}
            />

            {GAP}

            <NumberInput
              label="Default (fallback)"
              description="Applied to any market type not listed above — only enter in the final N seconds."
              value={data.momentum_min_tte_default ?? 120}
              step={30}
              unit="s"
              onSubmit={(v) => apply({ momentum_min_tte_default: v })}
            />

            <SectionHead title="Scanner" />

            <NumberInput
              label="Scan Interval"
              description="Fallback poll timeout — the scanner is primarily event-driven and wakes on every RTDS price tick and PM book update. This is the maximum time between scans when no events arrive."
              value={data.momentum_scan_interval ?? 10}
              step={1}
              unit="s"
              onSubmit={(v) => apply({ momentum_scan_interval: v })}
            />

            {GAP}

            <NumberInput
              label="Per-Market Cooldown"
              description="After touching a market, suppress re-entry for this long. Prevents duplicate entries across adjacent scans."
              value={data.momentum_market_cooldown_seconds ?? 300}
              step={30}
              unit="s"
              onSubmit={(v) => apply({ momentum_market_cooldown_seconds: v })}
            />

            {GAP}

            <NumberInput
              label="Max Concurrent"
              description="Hard cap on simultaneously open momentum positions."
              value={data.momentum_max_concurrent ?? 3}
              step={1}
              unit=""
              onSubmit={(v) => apply({ momentum_max_concurrent: v })}
            />
          </div>

          <div className="card">
            <h3>Momentum — Exit Thresholds</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Positions are held until the underlying spot crosses one of these thresholds
              (no time-based exit — holds to resolution or exit trigger).
            </p>

            <FloatInput
              label="Delta Stop-Loss"
              description="Exit when the underlying spot has moved this % past the strike against the position (e.g. 0.05 = exit when spot is 0.05% below strike for YES, or 0.05% above strike for NO). Uses live RTDS spot price (Polymarket's oracle feed), not binary CLOB."
              value={data.momentum_delta_stop_loss_pct ?? 0.05}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_delta_stop_loss_pct: v })}
            />

            <SectionHead title="Per-Coin Stop-Loss Overrides" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Higher-IV coins need wider stops — a single DOGE/SOL oracle tick routinely exceeds the global 0.04% stop.
              These values override the global Delta Stop-Loss for the named coin. See PER_COIN_CONFIG.md for calibration rationale.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="BTC"
                description="BTC — lowest IV (~0.50–0.60 σ_ann). Recommended: 0.03."
                value={data.momentum_delta_sl_pct_btc ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_btc: v })}
              />
              <FloatInput
                label="ETH"
                description="ETH — moderate IV (~0.70–0.85 σ_ann). Recommended: 0.04."
                value={data.momentum_delta_sl_pct_eth ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_eth: v })}
              />
              <FloatInput
                label="BNB"
                description="BNB — moderate IV (~0.65–0.80 σ_ann). Recommended: 0.04."
                value={data.momentum_delta_sl_pct_bnb ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_bnb: v })}
              />
              <FloatInput
                label="XRP"
                description="XRP — elevated IV from news spikes (~0.75–1.00 σ_ann). Recommended: 0.05."
                value={data.momentum_delta_sl_pct_xrp ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_xrp: v })}
              />
              <FloatInput
                label="SOL"
                description="SOL — high IV from ecosystem events (~1.00–1.40 σ_ann). Recommended: 0.06."
                value={data.momentum_delta_sl_pct_sol ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_sol: v })}
              />
              <FloatInput
                label="DOGE"
                description="DOGE — very high meme-driven IV (~1.20–1.80 σ_ann). Recommended: 0.08."
                value={data.momentum_delta_sl_pct_doge ?? data.momentum_delta_stop_loss_pct ?? 0.04}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_delta_sl_pct_doge: v })}
              />
            </div>
            {GAP}
            <FloatInput
              label="HYPE"
              description="HYPE — nascent token, widest regime uncertainty (~1.50–2.50 σ_ann). Recommended: 0.10."
              value={data.momentum_delta_sl_pct_hype ?? data.momentum_delta_stop_loss_pct ?? 0.04}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_delta_sl_pct_hype: v })}
            />

            {GAP}

            <FloatInput
              label="Take Profit"
              description="Exit if the held token's price rises above this. Set near 1.0 to hold to near-resolution."
              value={data.momentum_take_profit ?? 0.96}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ momentum_take_profit: v })}
            />

            {GAP}

            <FloatInput
              label="Min Gap Above Threshold (%)"
              description="Spot must exceed the vol-scaled entry threshold by at least this extra margin before a signal is taken. Blocks marginal entries where a single adverse tick can flip the position to a loser before expiry. 0 = disabled."
              value={data.momentum_min_gap_pct ?? 0}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_gap_pct: v })}
            />

            {GAP}

            <NumberInput
              label="Near-Expiry Stop Window (s)"
              description="When fewer than this many seconds remain AND spot has already crossed the strike against the position (delta < 0), exit via taker immediately. Prevents a snap to zero at resolution. Only fires on losing positions — winning positions are unaffected."
              value={data.momentum_near_expiry_time_stop_secs ?? 90}
              step={10}
              unit="s"
              onSubmit={(v) => apply({ momentum_near_expiry_time_stop_secs: v })}
            />
          </div>

          <div className="card">
            <h3>Momentum — Volatility &amp; Staleness</h3>

            <SectionHead title="Vol Signal" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Dynamic threshold = σ_ann × z / √(8760 / TTE_hours). Deribit ATM IV is used
              for BTC/ETH/SOL; HL rolling realized vol is used as fallback.
            </p>

            <FloatInput
              label="Vol Z-Score (global default)"
              description="Multiplier applied to σ_ann to compute the required delta. 1.645 ≈ 95th percentile one-tailed. Per-bucket overrides below take precedence when set."
              value={data.momentum_vol_z_score ?? 1.6449}
              step={0.05}
              unit="σ"
              onSubmit={(v) => apply({ momentum_vol_z_score: v })}
            />

            <FloatInput
              label="Minimum Spot Move Floor"
              description={
                "Absolute minimum spot-to-strike gap required to enter, independent of time bucket or vol regime. " +
                "The z-score gate can still pass a trade when the absolute gap is dangerously small (e.g. low-vol coins " +
                "compress the vol-scaled threshold down near zero). " +
                "This floor asks: even if the signal is technically valid, is the spot far enough from strike that " +
                "a single adverse tick won't flip the position from winning to losing? " +
                "That tick risk is the same whether it's a 5m, 15m, or 1h market — the absolute price distance determines survival. " +
                "0.08 means spot must have moved at least 0.08% in the winning direction. " +
                "Set to 0 to rely solely on the z-score filter."
              }
              value={data.momentum_min_delta_pct ?? 0}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_delta_pct: v })}
            />

            <SectionHead title="Per-Coin Entry Floor Overrides" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Per-coin minimum delta required to enter, overriding the global floor.
              Low priority under normal conditions — the vol-derived threshold dominates.
              Useful as insurance during low-vol periods or oracle lag.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="BTC"
                description="BTC entry floor. Recommended: 0.04."
                value={data.momentum_min_delta_pct_btc ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_btc: v })}
              />
              <FloatInput
                label="ETH"
                description="ETH entry floor. Recommended: 0.05."
                value={data.momentum_min_delta_pct_eth ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_eth: v })}
              />
              <FloatInput
                label="BNB"
                description="BNB entry floor. Recommended: 0.05."
                value={data.momentum_min_delta_pct_bnb ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_bnb: v })}
              />
              <FloatInput
                label="XRP"
                description="XRP entry floor. Recommended: 0.06."
                value={data.momentum_min_delta_pct_xrp ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_xrp: v })}
              />
              <FloatInput
                label="SOL"
                description="SOL entry floor. Recommended: 0.08."
                value={data.momentum_min_delta_pct_sol ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_sol: v })}
              />
              <FloatInput
                label="DOGE"
                description="DOGE entry floor. Recommended: 0.10."
                value={data.momentum_min_delta_pct_doge ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_doge: v })}
              />
            </div>
            {GAP}
            <FloatInput
              label="HYPE"
              description="HYPE entry floor. Recommended: 0.14."
              value={data.momentum_min_delta_pct_hype ?? data.momentum_min_delta_pct ?? 0}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_delta_pct_hype: v })}
            />

            <SectionHead title="Per-Bucket Z-Score Overrides" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Each market type can use a different signal strength threshold.
              Lower = more trades (weaker confirmation); Higher = fewer, higher-conviction trades.
              Set a bucket to match the global default to effectively disable its override.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="5-Min Bucket"
                description="5-min markets. Few signals even at 1.6 — keep strict. Recommended: 1.6."
                value={data.momentum_vol_z_score_5m ?? data.momentum_vol_z_score ?? 1.6449}
                step={0.05}
                unit="σ"
                onSubmit={(v) => apply({ momentum_vol_z_score_5m: v })}
              />
              <FloatInput
                label="15-Min Bucket"
                description="15-min markets. Most near-miss signals here. Recommended: 1.3."
                value={data.momentum_vol_z_score_15m ?? data.momentum_vol_z_score ?? 1.6449}
                step={0.05}
                unit="σ"
                onSubmit={(v) => apply({ momentum_vol_z_score_15m: v })}
              />
              <FloatInput
                label="1-Hour Bucket"
                description="1-hour markets. Good hit rate around 1.3. Recommended: 1.3."
                value={data.momentum_vol_z_score_1h ?? data.momentum_vol_z_score ?? 1.6449}
                step={0.05}
                unit="σ"
                onSubmit={(v) => apply({ momentum_vol_z_score_1h: v })}
              />
              <FloatInput
                label="4-Hour Bucket"
                description="4-hour markets. Illiquid — z rarely blocks here. Recommended: 1.6."
                value={data.momentum_vol_z_score_4h ?? data.momentum_vol_z_score ?? 1.6449}
                step={0.05}
                unit="σ"
                onSubmit={(v) => apply({ momentum_vol_z_score_4h: v })}
              />
            </div>
            {GAP}
            <FloatInput
              label="Daily Bucket"
              description="Daily markets rarely reach 1.6 — max observed z in 12h of data was 1.1. Set to 1.0 to trade them. Recommended: 1.0."
              value={data.momentum_vol_z_score_daily ?? data.momentum_vol_z_score ?? 1.6449}
              step={0.05}
              unit="σ"
              onSubmit={(v) => apply({ momentum_vol_z_score_daily: v })}
            />

            {GAP}

            <FloatInput
              label="Vol Cache TTL"
              description="How long Deribit ATM IV is cached before a fresh fetch. Higher = fewer API calls."
              value={data.momentum_vol_cache_ttl ?? 300}
              step={30}
              unit="s"
              onSubmit={(v) => apply({ momentum_vol_cache_ttl: v })}
            />

            <SectionHead title="Staleness Guards" />

            <FloatInput
              label="Max Spot Age"
              description="Maximum age of the RTDS spot price before the scan skips the market."
              value={data.momentum_spot_max_age_secs ?? 30}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_spot_max_age_secs: v })}
            />

            {GAP}

            <FloatInput
              label="Max Book Age"
              description="Maximum age of the PM CLOB book before the scan skips the market."
              value={data.momentum_book_max_age_secs ?? 60}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_book_max_age_secs: v })}
            />

            <Advanced>
              <Toggle
                label="Use Market Orders"
                description="When ON: fills immediately at best ask. When OFF: limit order placed at ask +0.5¢."
                value={(data.momentum_order_type ?? "limit") === "market"}
                onChange={(v) => apply({ momentum_order_type: v ? "market" : "limit" })}
              />
            </Advanced>
          </div>

          {/* ── Range markets sub-strategy ───────────────────── */}
          <div className="card">
            <h3>Range Markets</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              "Will BTC be between $X and $Y?" markets. YES resolves $1 if spot is inside
              the range at expiry; NO resolves $1 if spot is outside. Treated as a regular
              single-leg momentum trade with a bidirectional delta formula.
            </p>

            <Toggle
              label="Enable Range Markets"
              description="Include 'between $X and $Y' range markets in the momentum scan."
              value={data.momentum_range_enabled ?? false}
              onChange={(v) => apply({ momentum_range_enabled: v })}
            />

            {GAP}

            <SectionHead title="Price Band" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Override the price band used specifically for range market tokens.
              Range YES tokens price differently (bidirectional delta) so a separate
              band lets you tune entries independently of regular momentum.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="Band Low"
                description="Token price floor for range market entries."
                value={data.momentum_range_price_band_low ?? 0.6}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ momentum_range_price_band_low: v })}
              />
              <FloatInput
                label="Band High"
                description="Token price ceiling for range market entries."
                value={data.momentum_range_price_band_high ?? 0.95}
                step={0.01}
                unit=""
                onSubmit={(v) => apply({ momentum_range_price_band_high: v })}
              />
            </div>

            {GAP}

            <FloatInput
              label="Max Entry (USD)"
              description="Position size cap for range market entries. Independent of the standard momentum max entry."
              value={data.momentum_range_max_entry_usd ?? 25}
              step={5}
              unit="USD"
              onSubmit={(v) => apply({ momentum_range_max_entry_usd: v })}
            />

            {GAP}

            <FloatInput
              label="Vol Z-Score"
              description="Signal strength threshold for range markets. Lower = more trades (weaker confirmation). Higher = fewer, higher-conviction trades."
              value={data.momentum_range_vol_z_score ?? 0.8}
              step={0.05}
              unit="σ"
              onSubmit={(v) => apply({ momentum_range_vol_z_score: v })}
            />

            {GAP}

            <NumberInput
              label="Min TTE (s)"
              description="Minimum seconds to expiry required before entering a range market. Range markets often have longer durations than bucket markets — set appropriately."
              value={data.momentum_range_min_tte_seconds ?? 300}
              step={60}
              unit="s"
              onSubmit={(v) => apply({ momentum_range_min_tte_seconds: v })}
            />
          </div>
          </>
        )}
      </StrategySection>

      {/* ── Position Monitor ─────────────────────────────────── */}
      <div className="card" style={{ margin: "1.5rem 0" }}>
        <h3>Position Monitor</h3>
        <p className="settings-desc">
          Fallback poll interval for <strong>maker and mispricing</strong> positions only.
          Momentum exits are event-driven (HL BBO + PM book ticks) and are not affected by this setting.
        </p>

        {GAP}

        <NumberInput
          label="Poll Interval"
          description="How often the background monitor checks open maker and mispricing positions for exit conditions. Lower = faster exit response for those strategies. Has no effect on momentum positions."
          value={data.monitor_interval ?? 30}
          step={5}
          unit="s"
          onSubmit={(v) => apply({ monitor_interval: v })}
        />
      </div>
    </div>
  );
}
