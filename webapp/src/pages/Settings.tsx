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

function SelectInput({
  label,
  description,
  value,
  options,
  onSubmit,
}: {
  label: string;
  description: string;
  value: string;
  options: { value: string; label: string }[];
  onSubmit: (v: string) => void;
}) {
  return (
    <div className="settings-row">
      <div className="settings-label">
        <span className="settings-name">{label}</span>
        <span className="settings-desc">{description}</span>
      </div>
      <div className="settings-input-group">
        <select
          className="settings-input"
          value={value}
          onChange={(e) => onSubmit(e.target.value)}
          style={{ cursor: "pointer" }}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
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

        {GAP}

        <Toggle
          label="Opening Neutral (Strategy 5)"
          description="Buy YES + NO at bucket open, exit loser after σ_τ move — converts winner to momentum. Restart required to activate/deactivate."
          value={data.opening_neutral_enabled ?? false}
          onChange={(v) => apply({ opening_neutral_enabled: v })}
        />
      </div>

      {/* ── 2b. Opening Neutral Settings ────────────────────────────── */}
      <div className="card">
        <h3>Opening Neutral</h3>
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Entry gates, exit thresholds, and hold parameters for the Opening Neutral strategy.
        </p>

        <Toggle
          label="Dry Run"
          description="Scanner runs and logs signals but places no real orders. Disable once signals are validated."
          value={data.opening_neutral_dry_run ?? true}
          onChange={(v) => apply({ opening_neutral_dry_run: v })}
        />

        {GAP}

        <Toggle
          label="Take Profit"
          description="After the loser exits, arm a take-profit on the winner at a price that earns the target % on the combined entry cost."
          value={data.opening_neutral_tp_enabled ?? true}
          onChange={(v) => apply({ opening_neutral_tp_enabled: v })}
        />
        {data.opening_neutral_tp_enabled !== false && (
          <>
            {GAP}
            <FloatInput
              label="TP Target %"
              description="Target profit as a % of combined entry cost. Formula: combined_cost × (1 + pct) − loser_exit_price."
              value={+((data.opening_neutral_tp_profit_pct ?? 0.10) * 100).toFixed(1)}
              step={1}
              unit="%"
              onSubmit={(v) => apply({ opening_neutral_tp_profit_pct: v / 100 })}
            />
          </>
        )}

        {GAP}

        <Toggle
          label="Cold Book Spread Gate"
          description="Block entry when either the YES or NO book has a spread (ask − bid) wider than the threshold at open. Wide spreads indicate cold books where the loser exit may stall."
          value={data.opening_neutral_max_individual_spread_enabled ?? true}
          onChange={(v) => apply({ opening_neutral_max_individual_spread_enabled: v })}
        />
        {data.opening_neutral_max_individual_spread_enabled !== false && (
          <>
            {GAP}
            <FloatInput
              label="Max Individual Spread"
              description="Maximum spread (ask − bid) on either leg at open. Entry is blocked when either YES or NO exceeds this threshold."
              value={data.opening_neutral_max_individual_spread ?? 0.15}
              step={0.01}
              unit="$"
              onSubmit={(v) => apply({ opening_neutral_max_individual_spread: v })}
            />
          </>
        )}

        {GAP}

        <FloatInput
          label="Loser Exit Trigger"
          description="Bid-monitor fires when the loser bid drops to ≤ this price. Set slightly above the exit price (e.g. $0.38) to catch fills before the book gaps past $0.35."
          value={data.opening_neutral_loser_exit_trigger ?? 0.38}
          step={0.01}
          unit="$"
          onSubmit={(v) => apply({ opening_neutral_loser_exit_trigger: v })}
        />

        {GAP}

        <FloatInput
          label="Min Hold (seconds)"
          description="Minimum seconds after entry before the bid monitor can declare a loser. Prevents false-loser exits during the first-30s reversal window (T+30s is where YES/NO bids diverge meaningfully)."
          value={data.opening_neutral_min_hold_secs ?? 30}
          step={5}
          unit="s"
          onSubmit={(v) => apply({ opening_neutral_min_hold_secs: v })}
        />

        {GAP}

        <FloatInput
          label="Loser Exit Price"
          description="Resting GTC SELL price placed on both legs immediately after entry. Whichever leg fills first is declared the loser. Net pair P&L = exit_price + $1.00 − 2×entry."
          value={data.opening_neutral_loser_exit_price ?? 0.35}
          step={0.01}
          unit="$"
          onSubmit={(v) => apply({ opening_neutral_loser_exit_price: v })}
        />

        {GAP}

        <FloatInput
          label="Size per Leg"
          description="USDC notional for each leg (YES and NO each get this amount). Combined cost ≈ 2× this value before the spread gate applies."
          value={data.opening_neutral_size_usd ?? 1}
          step={0.5}
          unit="$"
          onSubmit={(v) => apply({ opening_neutral_size_usd: v })}
        />

        {GAP}

        <FloatInput
          label="Max Concurrent Pairs"
          description="Maximum number of simultaneously open YES+NO pairs. New entry signals are skipped when this limit is reached."
          value={data.opening_neutral_max_concurrent ?? 1}
          step={1}
          unit="pairs"
          onSubmit={(v) => apply({ opening_neutral_max_concurrent: Math.round(v) })}
        />

        {GAP}

        <SelectInput
          label="Entry Order Type"
          description="Order type for the entry BUY legs. 'market' crosses immediately (higher fill rate); 'limit' posts at the current ask (lower cost, may miss fast opens)."
          value={data.opening_neutral_order_type ?? "market"}
          options={[
            { value: "market", label: "Market (FAK — cross immediately)" },
            { value: "limit",  label: "Limit (post at ask)" },
          ]}
          onSubmit={(v) => apply({ opening_neutral_order_type: v })}
        />

        {GAP}

        <SelectInput
          label="One-Leg Fallback"
          description="Action when only one leg fills within the entry timeout. 'keep_as_momentum' leaves the filled leg open; 'exit_immediately' taker-exits it at best bid."
          value={data.opening_neutral_one_leg_fallback ?? "keep_as_momentum"}
          options={[
            { value: "keep_as_momentum", label: "Keep as momentum" },
            { value: "exit_immediately",  label: "Exit immediately" },
          ]}
          onSubmit={(v) => apply({ opening_neutral_one_leg_fallback: v })}
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
              label="Min Entry (USD)"
              description="Minimum position size floor. Prevents dust orders when kelly_f is very small near the entry threshold. Set to 1.0 to allow any size; increase to $5–$10 to skip marginal signals."
              value={data.momentum_min_entry_usd ?? 1}
              step={0.5}
              unit="USD"
              onSubmit={(v) => apply({ momentum_min_entry_usd: v })}
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

            {GAP}

            <NumberInput
              label="Delta SL Hysteresis (ticks)"
              description="Number of consecutive below-threshold oracle ticks before the delta stop-loss fires. Prevents a single noisy tick from triggering an exit. Set to 1 to fire on the first bad tick."
              value={data.momentum_delta_sl_min_ticks ?? 3}
              step={1}
              unit="ticks"
              onSubmit={(v) => apply({ momentum_delta_sl_min_ticks: v })}
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
            {GAP}
            <NumberInput
              label="Resolved Force-Close Timeout (s)"
              description="Seconds past end_date before using oracle (spot vs. strike) to force-close a stuck resolved position. PM can take 1–10 min for bucket_5m. Critical in paper mode where auto-redeem is disabled. Default: 300."
              value={data.momentum_resolved_force_close_sec ?? 300}
              step={30}
              unit="s"
              onSubmit={(v) => apply({ momentum_resolved_force_close_sec: v })}
            />

            <SectionHead title="Probability-Based Stop-Loss" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Exit when the CLOB ask price implies win probability has fallen below a threshold.
              Fires independently of the oracle delta SL — catches cases where the market is already
              pricing in a loss before spot has fully moved.
            </p>
            <Toggle
              label="Prob SL Enabled"
              description="When ON, exit if implied win probability (1 − ask_price) drops below the threshold."
              value={data.momentum_prob_sl_enabled ?? true}
              onChange={(v) => apply({ momentum_prob_sl_enabled: v })}
            />
            {(data.momentum_prob_sl_enabled ?? true) && (
              <>
                {GAP}
                <FloatInput
                  label="Win Prob Threshold"
                  description="Exit when implied win probability falls below this value. 0.25 = exit if CLOB prices the position at ≤25% chance of winning."
                  value={data.momentum_prob_sl_pct ?? 0.25}
                  step={0.05}
                  unit=""
                  onSubmit={(v) => apply({ momentum_prob_sl_pct: v })}
                />
                {GAP}
                <NumberInput
                  label="Min TTE to Arm (s)"
                  description="Prob SL only arms when at least this many seconds remain. Prevents misfires near expiry when the CLOB price collapses naturally."
                  value={data.momentum_prob_sl_min_tte_secs ?? 30}
                  step={5}
                  unit="s"
                  onSubmit={(v) => apply({ momentum_prob_sl_min_tte_secs: v })}
                />
                {GAP}
                <FloatInput
                  label="Oracle Staleness Limit (s)"
                  description="Skip the prob SL check when the oracle price is older than this. Stale oracles give unreliable CLOB-vs-oracle comparisons."
                  value={data.momentum_prob_sl_oracle_stale_secs ?? 10.0}
                  step={1}
                  unit="s"
                  onSubmit={(v) => apply({ momentum_prob_sl_oracle_stale_secs: v })}
                />
              </>
            )}
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

            <SectionHead title="Per-Bucket Entry Floor Overrides" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Per-bucket minimum delta required to enter. Combined with the per-coin floor
              by taking the higher of the two. Unlisted buckets fall back to the global floor.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <FloatInput
                label="5-Min Bucket"
                description="5m entry floor override. Tighter floor filters noise near expiry."
                value={data.momentum_min_delta_pct_5m ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_5m: v })}
              />
              <FloatInput
                label="15-Min Bucket"
                description="15m entry floor override."
                value={data.momentum_min_delta_pct_15m ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_15m: v })}
              />
              <FloatInput
                label="1-Hour Bucket"
                description="1h entry floor override."
                value={data.momentum_min_delta_pct_1h ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_1h: v })}
              />
              <FloatInput
                label="4-Hour Bucket"
                description="4h entry floor override."
                value={data.momentum_min_delta_pct_4h ?? data.momentum_min_delta_pct ?? 0}
                step={0.01}
                unit="%"
                onSubmit={(v) => apply({ momentum_min_delta_pct_4h: v })}
              />
            </div>
            {GAP}
            <FloatInput
              label="Daily Bucket"
              description="Daily entry floor override."
              value={data.momentum_min_delta_pct_daily ?? data.momentum_min_delta_pct ?? 0}
              step={0.01}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_delta_pct_daily: v })}
            />

            {GAP}

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

          {/* ── Phase B/C/D/E + Kelly Extensions ─────────────── */}
          <div className="card">
            <h3>Momentum — Advanced Settings</h3>
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Optional enhancements to the momentum scanner. All are disabled by default.
            </p>

            <SectionHead title="Phase B — Resolution Oracle" />
            <Toggle
              label="Use Resolution Oracle Near Expiry"
              description="In the final seconds before expiry, switch to PM's resolution oracle price instead of the RTDS spot feed. Reduces adverse timing noise when the oracle drifts from spot."
              value={data.momentum_use_resolution_oracle_near_expiry ?? false}
              onChange={(v) => apply({ momentum_use_resolution_oracle_near_expiry: v })}
            />

            <SectionHead title="Phase C — Near-Expiry Block (per Market Type)" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Block entries when fewer than N seconds remain. Prevents entering too close to expiry for bucket types where the fill path is too short. 0 = OFF.
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0" }}>
              <NumberInput
                label="5-Minute"
                description="bucket_5m — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_5m ?? 0}
                step={5}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_5m: v })}
              />
              <NumberInput
                label="15-Minute"
                description="bucket_15m — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_15m ?? 0}
                step={5}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_15m: v })}
              />
              <NumberInput
                label="1-Hour"
                description="bucket_1h — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_1h ?? 0}
                step={15}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_1h: v })}
              />
              <NumberInput
                label="4-Hour"
                description="bucket_4h — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_4h ?? 0}
                step={30}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_4h: v })}
              />
              <NumberInput
                label="Daily"
                description="bucket_daily — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_daily ?? 0}
                step={60}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_daily: v })}
              />
              <NumberInput
                label="Weekly"
                description="bucket_weekly — block entries in the last N seconds. 0 = OFF."
                value={data.momentum_phase_c_min_tte_weekly ?? 0}
                step={300}
                unit="s"
                onSubmit={(v) => apply({ momentum_phase_c_min_tte_weekly: v })}
              />
            </div>
            {GAP}
            <NumberInput
              label="Milestone"
              description="milestone — block entries in the last N seconds. 0 = OFF."
              value={data.momentum_phase_c_min_tte_milestone ?? 0}
              step={60}
              unit="s"
              onSubmit={(v) => apply({ momentum_phase_c_min_tte_milestone: v })}
            />

            <SectionHead title="M-10: Funding Rate Entry Gate" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Skip entries when HL funding rate signals market structure opposed to the trade direction.
              High positive funding = market biased DOWN (skip YES); deep negative = biased UP (skip NO).
              Values are HL per-8h rate (e.g. 0.00001 = 0.001% per 8h).
            </p>
            <Toggle
              label="Funding Gate Enabled"
              description="When ON, entries are skipped when funding rate opposes the trade direction."
              value={data.momentum_funding_gate_enabled ?? true}
              onChange={(v) => apply({ momentum_funding_gate_enabled: v })}
            />
            {(data.momentum_funding_gate_enabled ?? true) && (
              <>
                {GAP}
                <FloatInput
                  label="YES Max Funding Rate"
                  description="Skip YES/UP entry when HL 8h funding rate exceeds this. Positive funding = longs paying shorts (bearish signal). e.g. 0.00001 = 0.001% per 8h."
                  value={data.momentum_funding_gate_yes_max ?? 0.00001}
                  step={0.000005}
                  unit="per 8h"
                  onSubmit={(v) => apply({ momentum_funding_gate_yes_max: v })}
                />
                {GAP}
                <FloatInput
                  label="NO Min Funding Rate"
                  description="Skip NO/DOWN entry when HL 8h funding rate is below this. Negative funding = shorts paying longs (bullish signal). e.g. -0.00001 = -0.001% per 8h."
                  value={data.momentum_funding_gate_no_min ?? -0.00001}
                  step={0.000005}
                  unit="per 8h"
                  onSubmit={(v) => apply({ momentum_funding_gate_no_min: v })}
                />
              </>
            )}

            <SectionHead title="M-11: Depth Share Entry Gate" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              yes_depth_share = YES-side bid depth / (YES + NO depth). Low share for YES signals
              the market is not supporting the UP direction (AUC = 0.568, p = 0.002). Fail-open when
              depth share is unavailable.
            </p>
            <Toggle
              label="Depth Share Gate Enabled"
              description="When ON, YES entries require a minimum depth share; NO entries require a maximum."
              value={data.momentum_depth_share_gate_enabled ?? true}
              onChange={(v) => apply({ momentum_depth_share_gate_enabled: v })}
            />
            {(data.momentum_depth_share_gate_enabled ?? true) && (
              <>
                {GAP}
                <FloatInput
                  label="YES Min Depth Share"
                  description="Skip YES/UP entry when yes_depth_share is below this. e.g. 0.40 = YES-side must have at least 40% of total depth."
                  value={data.momentum_depth_share_yes_min ?? 0.40}
                  step={0.05}
                  unit=""
                  onSubmit={(v) => apply({ momentum_depth_share_yes_min: v })}
                />
                {GAP}
                <FloatInput
                  label="NO Max Depth Share"
                  description="Skip NO/DOWN entry when yes_depth_share is above this (inferred by symmetry). e.g. 0.60."
                  value={data.momentum_depth_share_no_max ?? 0.60}
                  step={0.05}
                  unit=""
                  onSubmit={(v) => apply({ momentum_depth_share_no_max: v })}
                />
              </>
            )}

            <SectionHead title="M-14: TWAP Deviation Gate (YES only)" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              In a low-volatility regime, if the oracle is below its recent 10-second TWAP
              (price pulling back), raise the YES/UP entry z-bar. Source: strategy_update.md
              section 1.6 — 37.6% YES win rate in low-vol + negative TWAP deviation.
            </p>
            <Toggle
              label="TWAP Gate Enabled"
              description="When ON, negative TWAP deviation in low-vol raises the YES z-bar by the multiplier below."
              value={data.momentum_twap_gate_enabled ?? true}
              onChange={(v) => apply({ momentum_twap_gate_enabled: v })}
            />
            {(data.momentum_twap_gate_enabled ?? true) && (
              <>
                {GAP}
                <FloatInput
                  label="TWAP Dev Threshold (bps)"
                  description="Trigger when oracle is below its 10s TWAP by this many bps. Negative = oracle below TWAP. e.g. -5.0 = oracle 0.5 bps below TWAP triggers the gate."
                  value={data.momentum_twap_dev_threshold_bps ?? -5.0}
                  step={1}
                  unit="bps"
                  onSubmit={(v) => apply({ momentum_twap_dev_threshold_bps: v })}
                />
                {GAP}
                <FloatInput
                  label="Low-Vol YES Z-Bar Multiplier"
                  description="Multiplies the effective z-bar when TWAP gate fires in low-vol. 1.4 = require 40% more sigma to enter. Does not affect NO/DOWN entries."
                  value={data.momentum_twap_dev_low_vol_yes_multiplier ?? 1.4}
                  step={0.1}
                  unit="x"
                  onSubmit={(v) => apply({ momentum_twap_dev_low_vol_yes_multiplier: v })}
                />
              </>
            )}

            <SectionHead title="M-13: Up-Fraction EWMA Early Exit" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Oracle tick up-fraction EWMA. For YES positions: exit when EWMA stays below threshold
              for N consecutive scan windows. Strongest early-exit signal in the dataset (AUC = 0.703)
              and fires before positions ride to a full loss.
            </p>
            <Toggle
              label="Upfrac Exit Enabled"
              description="When ON, fire an early exit when the oracle tick up-fraction EWMA drops below the threshold for N consecutive windows."
              value={data.momentum_upfrac_exit_enabled ?? true}
              onChange={(v) => apply({ momentum_upfrac_exit_enabled: v })}
            />
            {(data.momentum_upfrac_exit_enabled ?? true) && (
              <>
                {GAP}
                <FloatInput
                  label="Exit Threshold"
                  description="For YES: exit when EWMA is below threshold. For NO: exit when EWMA is above (1 - threshold). 0.40 = exit YES when fewer than 40% of recent ticks moved up."
                  value={data.momentum_upfrac_exit_threshold ?? 0.40}
                  step={0.05}
                  unit=""
                  onSubmit={(v) => apply({ momentum_upfrac_exit_threshold: v })}
                />
                {GAP}
                <NumberInput
                  label="Consecutive Windows"
                  description="Exit only fires after the EWMA has been below-threshold for this many consecutive evaluation windows. Prevents a single bad scan from triggering an exit."
                  value={data.momentum_upfrac_exit_windows ?? 2}
                  step={1}
                  unit="windows"
                  onSubmit={(v) => apply({ momentum_upfrac_exit_windows: v })}
                />
                {GAP}
                <FloatInput
                  label="EWMA Alpha"
                  description="Smoothing factor for the up-fraction EWMA. Higher = more reactive; lower = smoother. 0.3 = moderate smoothing."
                  value={data.momentum_upfrac_ewma_alpha ?? 0.3}
                  step={0.05}
                  unit=""
                  onSubmit={(v) => apply({ momentum_upfrac_ewma_alpha: v })}
                />
              </>
            )}

            <SectionHead title="Analysis Logging" />
            <Toggle
              label="Oracle Tick Log"
              description="Write intra-hold oracle ticks to momentum_ticks.csv. Disable in production to reduce disk I/O. Required for post-trade analysis and strategy calibration."
              value={data.momentum_ticks_log_enabled ?? true}
              onChange={(v) => apply({ momentum_ticks_log_enabled: v })}
            />

            <SectionHead title="Kelly Extensions (COB)" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              The win probability used by Kelly blends two signals: CLOB ask price (reliable when
              liquid) and oracle delta strength (stable near expiry). Weight shifts from CLOB to
              oracle as TTE shrinks, preventing either failure mode from dominating sizing.
            </p>
            <NumberInput
              label="Min Effective TTE"
              description="Kelly-specific TTE floor (seconds). Prevents sigma_tau collapsing at near-expiry (e.g. 3s), which would inflate z very high regardless of edge. Signals with less TTE remaining are sized as if this many seconds remain. Rule of thumb: ~50% of the bucket entry-gate window."
              value={data.momentum_kelly_min_tte_seconds ?? 30}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_kelly_min_tte_seconds: v })}
            />
            {GAP}
            <FloatInput
              label="Edge Premium"
              description="Systematic alpha added to the CLOB ask price when computing win_prob_clob. Calibrate from historical win rate. e.g. 0.07 = assume 7% better win rate than CLOB implies."
              value={data.momentum_kelly_edge_premium ?? 0.07}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ momentum_kelly_edge_premium: v })}
            />
            {GAP}
            <FloatInput
              label="Win Prob Cap"
              description="Hard ceiling on effective win probability. Prevents Kelly sizing runaway when both signals agree near-certainty. e.g. 0.95 = never assume more than 95% chance of winning."
              value={data.momentum_kelly_win_prob_cap ?? 0.95}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ momentum_kelly_win_prob_cap: v })}
            />
            {GAP}
            <NumberInput
              label="CLOB Reliable TTE (s)"
              description="TTE above which the CLOB book is considered fully reliable. Below this, weight shifts toward oracle delta. e.g. 60 = full CLOB weight when more than 60s remain."
              value={data.momentum_kelly_clob_reliable_tte ?? 60}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_kelly_clob_reliable_tte: v })}
            />
            {GAP}
            <FloatInput
              label="Oracle Sensitivity"
              description="Slope: maps signal strength above threshold to win_prob. e.g. 0.15 = each multiple of threshold adds 15pp of win probability."
              value={data.momentum_kelly_oracle_sensitivity ?? 0.15}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ momentum_kelly_oracle_sensitivity: v })}
            />

            <SectionHead title="Kelly Per-Bucket Multipliers" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Applied after the fractional-Kelly fraction to cap sizing on short-duration
              buckets. Even with a TTE floor, sigma_tau is small near expiry, so win_prob
              stays high and Kelly wants full MAX_ENTRY. These multipliers enforce a
              structural cap the vol model cannot compute on its own.
            </p>
            {([
              ["5m",    "bucket_5m",    "momentum_kelly_multiplier_5m",    0.45] as const,
              ["15m",   "bucket_15m",   "momentum_kelly_multiplier_15m",   0.70] as const,
              ["1h",    "bucket_1h",    "momentum_kelly_multiplier_1h",    0.90] as const,
              ["4h",    "bucket_4h",    "momentum_kelly_multiplier_4h",    1.00] as const,
              ["Daily", "bucket_daily", "momentum_kelly_multiplier_daily",  1.00] as const,
              ["Weekly","bucket_weekly","momentum_kelly_multiplier_weekly", 1.00] as const,
            ] as const).map(([label, , field, def]) => (
              <div key={field}>
                {GAP}
                <FloatInput
                  label={`${label} Multiplier`}
                  description={`Kelly size multiplier for ${label} bucket markets. 1.0 = no dampening; 0.45 = size at most 45% of raw Kelly.`}
                  value={(data[field as keyof typeof data] as number | undefined) ?? def}
                  step={0.05}
                  min={0}
                  max={1}
                  unit="×"
                  onSubmit={(v) => apply({ [field]: v })}
                />
              </div>
            ))}

            <SectionHead title="VWAP / RoC Secondary Filter" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Optional secondary confirmation layer. When non-zero, the signal is only
              accepted if price has deviated from its short-term VWAP by at least
              <code>min_vwap_dev_pct</code> AND the rate-of-change over the RoC window
              exceeds <code>min_roc_pct</code>. Set both to 0 to disable.
            </p>
            {GAP}
            <NumberInput
              label="VWAP Window (s)"
              description="Lookback window for VWAP calculation."
              value={data.momentum_vwap_window_sec ?? 30}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_vwap_window_sec: v })}
            />
            {GAP}
            <NumberInput
              label="RoC Window (s)"
              description="Lookback window for rate-of-change calculation."
              value={data.momentum_roc_window_sec ?? 60}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ momentum_roc_window_sec: v })}
            />
            {GAP}
            <FloatInput
              label="Min VWAP Deviation (%)"
              description="Minimum % price deviation from VWAP required to pass. 0 = disabled."
              value={data.momentum_min_vwap_dev_pct ?? 0}
              step={0.1}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_vwap_dev_pct: v })}
            />
            {GAP}
            <FloatInput
              label="Min RoC (%)"
              description="Minimum rate-of-change over the RoC window required to pass. 0 = disabled."
              value={data.momentum_min_roc_pct ?? 0}
              step={0.1}
              unit="%"
              onSubmit={(v) => apply({ momentum_min_roc_pct: v })}
            />

            <SectionHead title="Order Execution" />
            {GAP}
            <Toggle
              label="Take-Profit Resting Order"
              description="Place a resting GTC limit order at the take-profit price immediately after entry fill. When OFF, TP is managed by polling only."
              value={data.momentum_tp_resting_enabled ?? true}
              onChange={(v) => apply({ momentum_tp_resting_enabled: v })}
            />
            {GAP}
            <NumberInput
              label="TP Retry Max"
              description="Maximum number of attempts to place/cancel the TP resting order before giving up."
              value={data.momentum_tp_retry_max ?? 3}
              step={1}
              unit="retries"
              onSubmit={(v) => apply({ momentum_tp_retry_max: v })}
            />
            {GAP}
            <FloatInput
              label="TP Retry Step"
              description="Price improvement per retry tick when TP placement fails (e.g. 0.005 = half-cent)."
              value={data.momentum_tp_retry_step ?? 0.005}
              step={0.001}
              unit=""
              onSubmit={(v) => apply({ momentum_tp_retry_step: v })}
            />
            {GAP}
            <FloatInput
              label="Order Cancel Timeout (s)"
              description="Seconds to wait before cancelling an unfilled entry order and retrying."
              value={data.momentum_order_cancel_sec ?? 8.0}
              step={0.5}
              unit="s"
              onSubmit={(v) => apply({ momentum_order_cancel_sec: v })}
            />
            {GAP}
            <FloatInput
              label="Slippage Cap"
              description="Maximum allowed slippage (in probability points) above the signal price. Entry rejected if best ask exceeds signal_price + slippage_cap."
              value={data.momentum_slippage_cap ?? 0.05}
              step={0.005}
              unit=""
              onSubmit={(v) => apply({ momentum_slippage_cap: v })}
            />
            {GAP}
            <NumberInput
              label="Entry Max Retries"
              description="Maximum entry retry attempts after slippage or cancel timeout."
              value={data.momentum_max_retries ?? 2}
              step={1}
              unit="retries"
              onSubmit={(v) => apply({ momentum_max_retries: v })}
            />
            {GAP}
            <FloatInput
              label="Buy Retry Step"
              description="Price improvement per entry retry tick (e.g. 0.01 = 1 cent higher bid)."
              value={data.momentum_buy_retry_step ?? 0.01}
              step={0.005}
              unit=""
              onSubmit={(v) => apply({ momentum_buy_retry_step: v })}
            />

            <SectionHead title="Chainlink Watchdog" />
            <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
              Emergency circuit-breaker for the Chainlink oracle feed. If no Chainlink
              price update is received within the silence window, new momentum entries are
              blocked until the feed recovers.
            </p>
            {GAP}
            <NumberInput
              label="Chainlink Silence Timeout (s)"
              description="Seconds without a Chainlink update before entries are blocked. 30 = block if feed silent for 30 s."
              value={data.chainlink_silence_watchdog_secs ?? 30}
              step={5}
              unit="s"
              onSubmit={(v) => apply({ chainlink_silence_watchdog_secs: v })}
            />
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
              label="Entry Window (s)"
              description="Only enter in the last N seconds before expiry (same ceiling gate as the Entry Window section above, applied to range markets). e.g. 300 = only enter in the final 5 minutes. Markets with more TTE than this are skipped."
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
