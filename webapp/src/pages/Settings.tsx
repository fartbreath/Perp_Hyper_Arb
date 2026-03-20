/**
 * Settings — Runtime bot configuration controls.
 *
 * Changes are sent to POST /config and take effect immediately
 * (no bot restart needed). The page polls GET /config every 5s
 * so any out-of-band changes are reflected automatically.
 */
import { useState, useEffect } from "react";
import { useConfig, updateConfig } from "../api/client";
import type { ConfigData } from "../api/client";

// ── Toggle row ────────────────────────────────────────────────────────────────

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

// ── Number input row ──────────────────────────────────────────────────────────

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
  useEffect(() => setDraft(String(value)), [value]);

  const handleSubmit = () => {
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
          onBlur={handleSubmit}
          onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
        />
        <span className="settings-unit">{unit}</span>
      </div>
    </div>
  );
}

// ── Float input row ──────────────────────────────────────────────────

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
  useEffect(() => setDraft(String(value)), [value]);

  const handleSubmit = () => {
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
          onBlur={handleSubmit}
          onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
        />
        <span className="settings-unit">{unit}</span>
      </div>
    </div>
  );
}

// ── Status banner ─────────────────────────────────────────────────────────────

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

// ── Shared sub-section heading ────────────────────────────────────────────────

function SectionHead({ title }: { title: string }) {
  return (
    <h4 style={{
      color: "#64748b",
      fontSize: "0.75rem",
      textTransform: "uppercase",
      letterSpacing: "0.07em",
      margin: "1.5rem 0 0.6rem",
      paddingBottom: "0.35rem",
      borderBottom: "1px solid #1e293b",
    }}>
      {title}
    </h4>
  );
}

const GAP = <div style={{ height: "0.75rem" }} />;

// ── Main page ─────────────────────────────────────────────────────────────────

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
      const keys = Object.keys(patch).join(", ");
      showBanner(`✓ Saved: ${keys}`);
    } catch (e: unknown) {
      showBanner(`Error: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setSaving(false);
    }
  };

  if (error) return <div className="page"><div className="error">API offline — {error}</div></div>;
  if (!data) return <div className="page"><div className="card skeleton" style={{ height: 300 }} /></div>;

  return (
    <div className="page">
      <h2>Settings</h2>
      <SaveBanner msg={banner} />

      {/* ── Market Types ────────────────────────────────────────────────
          Toggle individual bucket types on/off. Excluded types are skipped
          in _evaluate_signal() — no new quotes placed, existing ones kept.   */}
      <div className="card">
        <h3>Market Types <span style={{ fontSize: "0.75rem", color: "#64748b", fontWeight: 400, marginLeft: "0.5rem" }}>maker</span></h3>
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Disable a bucket to stop new quotes for that market type. Existing positions continue to run.
        </p>
        {(["bucket_5m", "bucket_15m", "bucket_1h"] as const).map((bucket) => {
          const excluded = (data.maker_excluded_market_types ?? []).includes(bucket);
          const label = bucket.replace("bucket_", "") + " bucket";
          const bucketDesc: Record<string, string> = {
            bucket_5m: "5-minute markets. Highest volume, shortest lifetime (~5 min). Fastest capital recycling but most sensitive to timing and adverse selection.",
            bucket_15m: "15-minute markets. Balanced between fill frequency and position hold time.",
            bucket_1h: "1-hour markets. Lower frequency, longer TTE, slower capital recycling.",
          };
          return (
            <Toggle
              key={bucket}
              label={label}
              description={bucketDesc[bucket] ?? `Quote ${bucket.replace("bucket_", "")} markets.`}
              value={!excluded}
              onChange={(enabled) => {
                const current = data.maker_excluded_market_types ?? [];
                const updated = enabled
                  ? current.filter((b) => b !== bucket)
                  : [...current, bucket];
                apply({ maker_excluded_market_types: updated });
              }}
            />
          );
        })}
      </div>

      {/* ╔══════════════════════════════════════════════════════════════╗
          ║  GLOBAL — applies to the whole bot                          ║
          ╚══════════════════════════════════════════════════════════════╝ */}

      {/* ── Bot Controls ─────────────────────────────────────────────── */}
      <div className="card">
        <h3>Bot Controls <span style={{ fontSize: "0.75rem", color: "#64748b", fontWeight: 400, marginLeft: "0.5rem" }}>global</span></h3>

        <Toggle
          label="Paper Trading"
          description="When ON, no real orders are placed. All fills are simulated."
          value={data.paper_trading}
          onChange={(v) => apply({ paper_trading: v })}
          danger={!data.paper_trading}
        />
        {!data.paper_trading && (
          <div className="banner info" style={{ marginTop: "0.75rem" }}>
            ⚠️ LIVE mode is active — real orders will be placed on Polymarket.
          </div>
        )}

        {GAP}

        <Toggle
          label="Auto-Approve Signals"
          description="Bypass the CLI y/n prompt. Signals approved by the agent are executed immediately."
          value={data.auto_approve}
          onChange={(v) => apply({ auto_approve: v })}
        />

        {GAP}

        <Toggle
          label="Agent Auto Mode"
          description="Agent decisions execute directly without any human prompt. Requires Auto-Approve ON."
          value={data.agent_auto}
          onChange={(v) => apply({ agent_auto: v })}
        />
        {data.agent_auto && !data.auto_approve && (
          <div className="banner info" style={{ marginTop: "0.75rem" }}>
            ℹ️ Agent Auto requires Auto-Approve to also be ON to bypass the prompt.
          </div>
        )}

        {GAP}

        <FloatInput
          label="Paper Capital"
          description="Virtual budget (USD) used for capital-utilisation tracking in paper mode."
          value={data.paper_capital_usd ?? 10000}
          min={100}
          max={1000000}
          step={500}
          unit="USD"
          onSubmit={(v) => apply({ paper_capital_usd: v })}
        />
      </div>

      {/* ── Active Strategies ─────────────────────────────────────────── */}
      <div className="card">
        <h3>Active Strategies <span style={{ fontSize: "0.75rem", color: "#64748b", fontWeight: 400, marginLeft: "0.5rem" }}>global</span></h3>
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Disabling a strategy stops new signals. Open positions continue to be monitored and closed normally.
        </p>

        <Toggle
          label="Mispricing (Strategy 2)"
          description="Deribit implied-probability scan vs PM price. The active paper-trade strategy."
          value={data.strategy_mispricing}
          onChange={(v) => apply({ strategy_mispricing: v })}
        />

        {GAP}

        <Toggle
          label="Market Making (Strategy 1)"
          description="Quote PM limit-order book with HL delta hedge. Requires live credentials."
          value={data.strategy_maker}
          onChange={(v) => apply({ strategy_maker: v })}
        />
        {data.strategy_maker && data.paper_trading && (
          <div className="banner info" style={{ marginTop: "0.75rem" }}>
            ℹ️ Running in paper mode — orders are simulated by FillSimulator. No real funds are at risk.
          </div>
        )}
      </div>

      {/* ── Global Risk Limits ────────────────────────────────────────── */}
      <div className="card">
        <h3>Global Risk Limits <span style={{ fontSize: "0.75rem", color: "#64748b", fontWeight: 400, marginLeft: "0.5rem" }}>global</span></h3>

        <NumberInput
          label="Global Position Cap"
          description="Hard maximum concurrent open positions across both strategies."
          value={data.max_concurrent_positions}
          min={1}
          max={50}
          step={1}
          unit=""
          onSubmit={(v) => apply({ max_concurrent_positions: v })}
        />

        {GAP}

        <FloatInput
          label="PM Exposure / Market"
          description="Maximum Polymarket collateral committed to any single market (USD)."
          value={data.max_pm_exposure_per_market ?? 500}
          min={50}
          max={10000}
          step={50}
          unit="USD"
          onSubmit={(v) => apply({ max_pm_exposure_per_market: v })}
        />

        {GAP}

        <FloatInput
          label="Total PM Exposure"
          description="Maximum total Polymarket collateral across all open positions (USD). New positions are blocked when this is reached."
          value={data.max_total_pm_exposure ?? 2000}
          min={100}
          max={100000}
          step={100}
          unit="USD"
          onSubmit={(v) => apply({ max_total_pm_exposure: v })}
        />

        {GAP}

        <FloatInput
          label="Hard Stop (Drawdown)"
          description="Total cumulative loss (USD) that triggers an emergency halt. Bot pauses automatically until manually restarted."
          value={data.hard_stop_drawdown ?? 500}
          min={50}
          max={50000}
          step={50}
          unit="USD"
          onSubmit={(v) => apply({ hard_stop_drawdown: v })}
        />
      </div>

      {/* ╔══════════════════════════════════════════════════════════════╗
          ║  STRATEGY 2 — MISPRICING                                    ║
          ╚══════════════════════════════════════════════════════════════╝ */}

      <div className="card">
        <h3>
          Strategy 2 — Mispricing
          <span style={{
            fontSize: "0.72rem", fontWeight: 400, marginLeft: "0.6rem",
            padding: "2px 8px", borderRadius: 3,
            background: data.strategy_mispricing ? "#14532d" : "#1e293b",
            color: data.strategy_mispricing ? "#86efac" : "#64748b",
          }}>
            {data.strategy_mispricing ? "enabled" : "disabled"}
          </span>
        </h3>
        <p className="settings-desc" style={{ marginBottom: "0.5rem" }}>
          Scans for N(d₂)/Kalshi mispricings vs Polymarket prices. Enters directional positions; exits via profit target or stop-loss.
        </p>

        <SectionHead title="Scanner" />

        <NumberInput
          label="Scan Interval"
          description="How often the mispricing scanner runs a full sweep of all tracked markets."
          value={data.scan_interval}
          min={10}
          max={3600}
          step={10}
          unit="seconds"
          onSubmit={(v) => apply({ scan_interval: v })}
        />
        {data.scan_interval < 60 && (
          <div className="banner info" style={{ marginTop: "0.75rem" }}>
            ℹ️ Scan intervals below 60s may hit Deribit rate limits.
          </div>
        )}

        {GAP}

        <NumberInput
          label="Max Concurrent Positions"
          description="Maximum concurrent open positions from the mispricing strategy."
          value={data.max_concurrent_mispricing_positions}
          min={1}
          max={20}
          step={1}
          unit=""
          onSubmit={(v) => apply({ max_concurrent_mispricing_positions: v })}
        />

        {GAP}

        <FloatInput
          label="Min Signal Score (Mispricing)"
          description="Discard mispricing signals scoring below this threshold (0–100). Set to 0 to allow all signals. Higher values keep only high-quality opportunities."
          value={data.min_signal_score_mispricing ?? 0}
          min={0}
          max={100}
          step={5}
          unit="/ 100"
          onSubmit={(v) => apply({ min_signal_score_mispricing: v })}
        />

        <SectionHead title="Signal Source — Kalshi" />

        <Toggle
          label="Kalshi Enabled"
          description="Use Kalshi prices as the primary signal. When OFF, falls back to N(d₂)-only mode."
          value={data.kalshi_enabled}
          onChange={(v) => apply({ kalshi_enabled: v })}
        />

        {data.kalshi_enabled && (
          <>
            {GAP}
            <Toggle
              label="Require N(d₂) Confirmation"
              description="Both Kalshi spread and N(d₂) direction must agree before a signal fires. Recommended while building track record."
              value={data.kalshi_require_nd2_confirmation}
              onChange={(v) => apply({ kalshi_require_nd2_confirmation: v })}
            />
            {!data.kalshi_require_nd2_confirmation && (
              <div className="banner info" style={{ marginTop: "0.75rem", background: "#431407", border: "1px solid #f59e0b", color: "#fcd34d" }}>
                ⚠️ Kalshi-only mode — N(d₂) check bypassed. Direction is determined entirely by the Kalshi spread.
              </div>
            )}
            {GAP}
            <FloatInput
              label="Min Deviation"
              description="Minimum |PM − Kalshi| spread required to fire a signal."
              value={data.kalshi_min_deviation}
              min={0.01}
              max={0.5}
              step={0.01}
              unit=""
              onSubmit={(v) => apply({ kalshi_min_deviation: v })}
            />
            {GAP}
            <FloatInput
              label="Max Strike Diff"
              description="Maximum fractional strike difference for a Kalshi market to match (e.g. 0.02 = 2%)."
              value={data.kalshi_match_max_strike_diff}
              min={0.001}
              max={0.2}
              step={0.005}
              unit=""
              onSubmit={(v) => apply({ kalshi_match_max_strike_diff: v })}
            />
            {GAP}
            <FloatInput
              label="Max Expiry Days"
              description="Maximum calendar-day difference in resolution date for a Kalshi market to match."
              value={data.kalshi_match_max_expiry_days}
              min={0.5}
              max={14}
              step={0.5}
              unit="days"
              onSubmit={(v) => apply({ kalshi_match_max_expiry_days: v })}
            />
          </>
        )}

        <SectionHead title="Exit Rules — Mispricing positions only" />
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Profit target and stop-loss apply only to mispricing positions.
          Maker positions use coin-level loss limits and a time-based exit (configured separately below).
        </p>

        <FloatInput
          label="Profit Target %"
          description="Close a position when unrealised P&L reaches this fraction of the initial edge (e.g. 0.60 = take profit at 60% of max theoretical gain)."
          value={(data.profit_target_pct ?? 0.6) * 100}
          min={10}
          max={100}
          step={5}
          unit="%"
          onSubmit={(v) => apply({ profit_target_pct: v / 100 })}
        />

        {GAP}

        <FloatInput
          label="Stop Loss"
          description="Hard per-position stop: close immediately if unrealised loss exceeds this amount."
          value={data.stop_loss_usd ?? 25}
          min={1}
          max={500}
          step={5}
          unit="USD"
          onSubmit={(v) => apply({ stop_loss_usd: v })}
        />

        {GAP}

        <NumberInput
          label="Exit Before Resolution"
          description="Force-close mispricing positions this many days before the market resolves. Avoids holding through the final binary outcome."
          value={data.exit_days_before_resolution ?? 3}
          min={0}
          max={14}
          step={1}
          unit="days"
          onSubmit={(v) => apply({ exit_days_before_resolution: v })}
        />

        {GAP}

        <NumberInput
          label="Min Hold Time"
          description="Minimum seconds a position must be held before any exit condition can trigger. Prevents immediate exits due to stale prices at fill time. Applies to all positions."
          value={data.min_hold_seconds ?? 60}
          min={0}
          max={3600}
          step={30}
          unit="seconds"
          onSubmit={(v) => apply({ min_hold_seconds: v })}
        />
      </div>

      {/* ╔══════════════════════════════════════════════════════════════╗
          ║  STRATEGY 1 — MARKET MAKING                                 ║
          ╚══════════════════════════════════════════════════════════════╝ */}

      <div className="card">
        <h3>
          Strategy 1 — Market Making
          <span style={{
            fontSize: "0.72rem", fontWeight: 400, marginLeft: "0.6rem",
            padding: "2px 8px", borderRadius: 3,
            background: data.strategy_maker ? "#14532d" : "#1e293b",
            color: data.strategy_maker ? "#86efac" : "#64748b",
          }}>
            {data.strategy_maker ? "enabled" : "disabled"}
          </span>
        </h3>
        <p className="settings-desc" style={{ marginBottom: "0.5rem" }}>
          Quotes the PM limit-order book with an HL perp delta hedge.
          Changes take effect on the next repricing cycle (within Max Quote Age).
        </p>

        <SectionHead title="Position Limits & Exit" />

        <NumberInput
          label="Max Concurrent Positions"
          description="Maximum concurrent open positions from the maker strategy."
          value={data.max_concurrent_maker_positions}
          min={1}
          max={20}
          step={1}
          unit=""
          onSubmit={(v) => apply({ max_concurrent_maker_positions: v })}
        />

        {GAP}

        <NumberInput
          label="Max Positions / Coin"
          description="Per-underlying open position cap. Must satisfy (cap × quote_size) ≥ Hedge Threshold to allow hedging."
          value={data.maker_positions_per_underlying}
          min={1}
          max={20}
          step={1}
          unit=""
          onSubmit={(v) => apply({ maker_positions_per_underlying: v })}
        />

        {GAP}

        <FloatInput
          label="Max Loss / Coin"
          description="Maximum aggregate unrealised loss (USD) across all open maker positions for one underlying before the bot exits all of them."
          value={data.maker_coin_max_loss_usd}
          min={10}
          max={500}
          step={5}
          unit="USD"
          onSubmit={(v) => apply({ maker_coin_max_loss_usd: v })}
        />

        {GAP}

        <FloatInput
          label="Near-Expiry Exit (hours before resolution)"
          description="Close maker positions when this many hours of TTE remain. This is a TTE threshold, NOT a position age limit. For bucket markets (5m–weekly) the quoting gate is controlled by 'Exit TTE Fraction' below, not this value — so setting this to 0 is fine when trading buckets."
          value={data.maker_exit_hours}
          min={0}
          max={48}
          step={0.5}
          unit="hours remaining"
          onSubmit={(v) => apply({ maker_exit_hours: v })}
        />

        {GAP}

        <FloatInput
          label="Exit TTE Fraction (bucket markets)"
          description="Stop opening new quotes when this fraction of the market's total lifecycle remains. E.g. 0.10 = block quoting in the last 10% of the market's life. A bucket_1h at TTE=6min (last 10%) is blocked; same bucket at TTE=30min (last 50%) scores full marks. Also shapes the TTE quality score — markets in the final 10–25% of life are penalised. Does NOT affect position closing (that is controlled by Near-Expiry Exit)."
          value={data.maker_exit_tte_frac ?? 0.10}
          min={0}
          max={0.5}
          step={0.01}
          unit="fraction"
          onSubmit={(v) => apply({ maker_exit_tte_frac: v })}
        />

        {GAP}

        <FloatInput
          label="Entry TTE Fraction (opening cooldown)"
          description="Skip quoting until this fraction of the market's life has elapsed. E.g. 0.10 = don't quote in the first 10% of the bucket's life (30s for a bucket_5m, 6min for bucket_1h). Prevents adverse-selection from informed opening flow and gives the HL vol filter time to build history. Set to 0 to disable."
          value={data.maker_entry_tte_frac ?? 0.10}
          min={0}
          max={0.5}
          step={0.01}
          unit="fraction"
          onSubmit={(v) => apply({ maker_entry_tte_frac: v })}
        />

        {GAP}

        <NumberInput
          label="Batch Size (contracts per order)"
          description="Maximum contracts per side per individual quote order. Limits exposure from a single adversarial sweep: a sweep can take at most this many contracts before the next reprice cycle re-checks fill balance. Works with the imbalance-aware sizing — after a YES-only sweep the next cycle will not re-post YES until NO catches up. Lower = safer but slower capital deployment. Set to 999 to disable."
          value={data.maker_batch_size ?? 50}
          min={1}
          max={999}
          step={10}
          unit="contracts"
          onSubmit={(v) => apply({ maker_batch_size: v })}
        />

        {GAP}

        <div className="settings-row">
          <div className="settings-label">
            <span className="settings-name">Deployment Mode</span>
            <span className="settings-desc">
              <em>auto</em>: deploy all qualifying quotes immediately; <em>manual</em>: wait for explicit deploy from Signals page.
            </span>
          </div>
          <div className="settings-input-group">
            <select
              value={data.deployment_mode ?? "auto"}
              onChange={(e) => apply({ deployment_mode: e.target.value })}
              style={{ padding: "0.35rem 0.6rem", background: "#0f172a", color: "#e2e8f0", border: "1px solid #334155", borderRadius: 4, fontSize: "0.9rem" }}
            >
              <option value="auto">auto</option>
              <option value="manual">manual</option>
            </select>
          </div>
        </div>

        <SectionHead title="Quoting" />

        <FloatInput
          label="Reprice Trigger"
          description="HL best bid/offer must move by this percentage before PM quotes are repriced. Partial fills also have a separate adverse-drift guard that triggers independently of HL."
          value={+(data.reprice_trigger_pct * 100).toFixed(3)}
          min={0.01}
          max={5}
          step={0.01}
          unit="%"
          onSubmit={(v) => apply({ reprice_trigger_pct: v / 100 })}
        />

        {GAP}

        <NumberInput
          label="Max Quote Age (backstop)"
          description="Cancel and repost any quote older than this regardless of price movement. Also picks up newly-liquid markets discovered since last reprice cycle."
          value={data.max_quote_age_seconds}
          min={5}
          max={300}
          step={5}
          unit="seconds"
          onSubmit={(v) => apply({ max_quote_age_seconds: v })}
        />

        {GAP}

        <FloatInput
          label="Min Edge"
          description="Minimum spread edge required (after fees) before quoting a market. Markets with insufficient edge are skipped entirely."
          value={+(data.min_edge_pct * 100).toFixed(3)}
          min={0.01}
          max={10}
          step={0.01}
          unit="%"
          onSubmit={(v) => apply({ min_edge_pct: v / 100 })}
        />

        {GAP}

        <FloatInput
          label="Max Book Age"
          description="Skip repricing a market if its PM order book is older than this many seconds. 0 = disabled (always reprice). Raise to 30–60s to avoid quoting against stale books on thin markets."
          value={data.maker_max_book_age_secs ?? 0}
          min={0}
          max={300}
          step={5}
          unit="s"
          onSubmit={(v) => apply({ maker_max_book_age_secs: Math.round(v) })}
        />

        <SectionHead title="Quote Sizing" />

        <FloatInput
          label="Size % of 24h Volume"
          description="Quote size per side as a % of the market's 24h volume. Clamped between Min and Max below."
          value={(data.maker_quote_size_pct ?? 0.01) * 100}
          min={0.1}
          max={10}
          step={0.1}
          unit="%"
          onSubmit={(v) => apply({ maker_quote_size_pct: v / 100 })}
        />

        {GAP}

        <FloatInput
          label="New Market Fallback"
          description="Quote size for markets with no recorded volume (newly listed). Auto-adjusts once volume data is available."
          value={data.maker_quote_size_new_market ?? 25}
          min={5}
          max={200}
          step={5}
          unit="USD"
          onSubmit={(v) => apply({ maker_quote_size_new_market: v })}
        />

        {GAP}

        <FloatInput
          label="Min Size"
          description="Floor: never quote less than this per side."
          value={data.maker_quote_size_min ?? 10}
          min={1}
          max={100}
          step={1}
          unit="USD"
          onSubmit={(v) => apply({ maker_quote_size_min: v })}
        />

        {GAP}

        <FloatInput
          label="Max Size"
          description="Ceiling: never quote more than this per side, regardless of volume."
          value={data.maker_quote_size_max ?? 200}
          min={10}
          max={1000}
          step={10}
          unit="USD"
          onSubmit={(v) => apply({ maker_quote_size_max: v })}
        />

        <SectionHead title="Quote Guards" />

        <FloatInput
          label="Min Signal Score (Maker)"
          description="Skip quoting markets with a signal score below this threshold (0–100). Score combines edge, volume run-rate, price balance, and lifecycle-relative TTE quality — bucket markets (5m–weekly) are scored by fraction-of-life-remaining so a fresh 1h bucket scores equally to a fresh daily bucket. Set to 0 to quote all qualifying markets."
          value={data.min_signal_score_maker ?? 0}
          min={0}
          max={100}
          step={5}
          unit="/ 100"
          onSubmit={(v) => apply({ min_signal_score_maker: v })}
        />

        {GAP}

        <FloatInput
          label="Min Signal Score (5m bucket)"
          description="Per-type override: skip bucket_5m quotes scoring below this threshold. 5m markets default to 92 — they have a short quote window that leaves one-sided fills when the opposing leg doesn't arrive before expiry. Set to 0 to use the global Maker threshold above."
          value={data.maker_min_signal_score_5m ?? 0}
          min={0}
          max={100}
          step={1}
          unit="/ 100"
          onSubmit={(v) => apply({ maker_min_signal_score_5m: v })}
        />

        {GAP}

        <FloatInput
          label="Min Signal Score (1h bucket)"
          description="Per-type override: skip bucket_1h quotes scoring below this threshold. 1h buckets carry directional risk when YES/NO can't be balanced before expiry — raising this to 88 filters the weakest entries. Set to 0 to use the global Maker threshold."
          value={data.maker_min_signal_score_1h ?? 0}
          min={0}
          max={100}
          step={1}
          unit="/ 100"
          onSubmit={(v) => apply({ maker_min_signal_score_1h: v })}
        />

        {GAP}

        <FloatInput
          label="Min Signal Score (4h bucket)"
          description="Per-type override: skip bucket_4h quotes scoring below this threshold. 4h markets give the skew more time to balance YES/NO, so a slightly looser threshold is appropriate. Set to 0 to use the global Maker threshold."
          value={data.maker_min_signal_score_4h ?? 0}
          min={0}
          max={100}
          step={1}
          unit="/ 100"
          onSubmit={(v) => apply({ maker_min_signal_score_4h: v })}
        />

        {GAP}

        <FloatInput
          label="Exit TTE Fraction (5m bucket)"
          description="Stop quoting bucket_5m markets when this fraction of their life remains. Default 0.35 = stop with 1m 45s left on a 5-minute market, giving offsetting flow time to arrive before expiry. Set to 0 to fall back to the global Exit TTE Fraction."
          value={data.maker_exit_tte_frac_5m ?? 0}
          min={0}
          max={0.9}
          step={0.05}
          unit=""
          onSubmit={(v) => apply({ maker_exit_tte_frac_5m: v })}
        />

        {GAP}

        <FloatInput
          label="Min Quote Price"
          description="Reject markets where YES price is below this or above (1 − this). Prevents quoting deeply-OTM markets."
          value={data.maker_min_quote_price ?? 0.05}
          min={0.01}
          max={0.49}
          step={0.01}
          unit=""
          onSubmit={(v) => apply({ maker_min_quote_price: v })}
        />

        {GAP}

        <FloatInput
          label="Min Volume (24h)"
          description="Skip markets with less than this 24h volume. For bucket markets the threshold is scaled by fraction-of-life-elapsed — a brand-new bucket always passes regardless of this value; the full threshold only applies near expiry. Volume is also compared against a type-specific cap (5m=$15K, 1h=$60K, daily=$250K) in the scorer."
          value={data.maker_min_volume_24hr ?? 5000}
          min={0}
          step={100}
          unit="USD"
          onSubmit={(v) => apply({ maker_min_volume_24hr: v })}
        />

        {GAP}

        <NumberInput
          label="Max TTE Days"
          description="Skip markets resolving more than this many days out. For bucket markets the lower bound is 'Exit TTE Fraction' (last N% of market life); for milestone/unknown markets the lower bound is 'Near-Expiry Exit' hours."
          value={data.maker_max_tte_days ?? 14}
          min={1}
          max={90}
          step={1}
          unit="days"
          onSubmit={(v) => apply({ maker_max_tte_days: v })}
        />

        <SectionHead title="New Market Logic" />

        <NumberInput
          label="New Market Window"
          description="A market is treated as 'new' for this many seconds after listing. New markets get a wider spread and higher simulated fill probability."
          value={data.new_market_age_limit ?? 3600}
          min={60}
          max={86400}
          step={60}
          unit="seconds"
          onSubmit={(v) => apply({ new_market_age_limit: v })}
        />

        {GAP}

        <FloatInput
          label="Wide Spread (new market)"
          description="Half-spread used when quoting a newly-listed market. Wider spread compensates for higher uncertainty."
          value={data.new_market_wide_spread ?? 0.08}
          min={0.01}
          max={0.25}
          step={0.005}
          unit=""
          onSubmit={(v) => apply({ new_market_wide_spread: v })}
        />

        {GAP}

        <FloatInput
          label="Pull Spread (new market)"
          description="If a competitor is already quoting inside this spread, tighten to match rather than post the wide quote."
          value={data.new_market_pull_spread ?? 0.02}
          min={0.001}
          max={0.1}
          step={0.001}
          unit=""
          onSubmit={(v) => apply({ new_market_pull_spread: v })}
        />

        <SectionHead title="Coin-Level Inventory Skew" />
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Shifts both bid and ask based on aggregate coin inventory across all markets for that coin. A net-long position moves the mid lower to attract more sellers. Unlike per-market skew, both legs move together.
        </p>

        <FloatInput
          label="Skew Coefficient"
          description="Price shift per dollar of net coin inventory. E.g. 0.0001 with $300 net inventory moves the mid by 3¢."
          value={data.inventory_skew_coeff ?? 0.0001}
          min={0}
          max={0.01}
          step={0.00001}
          unit=""
          onSubmit={(v) => apply({ inventory_skew_coeff: v })}
        />

        {GAP}

        <FloatInput
          label="Skew Cap"
          description="Maximum price shift from coin-level skew. Prevents large inventory from pushing quotes too far from fair value."
          value={data.inventory_skew_max ?? 0.01}
          min={0.001}
          max={0.1}
          step={0.001}
          unit=""
          onSubmit={(v) => apply({ inventory_skew_max: v })}
        />

        <SectionHead title="Per-Market Imbalance Skew" />
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          When YES and NO contract counts diverge within a single market, only the lagging leg's price is tightened — attracting fills to close the gap. The over-filled leg stays at fair value. Targets one-sided spread accumulation.
        </p>

        <FloatInput
          label="Imbalance Skew Coefficient"
          description="Price improvement per excess contract on the lagging leg. Example: 0.0005 with a 50-contract imbalance → 2.5¢ tighter quote on the lagging side."
          value={data.maker_imbalance_skew_coeff ?? 0.0005}
          min={0}
          max={0.01}
          step={0.0001}
          unit=""
          onSubmit={(v) => apply({ maker_imbalance_skew_coeff: v })}
        />

        {GAP}

        <FloatInput
          label="Imbalance Skew Cap"
          description="Maximum price improvement on the lagging leg (0.03 = 3¢ cap). Prevents sacrificing too much spread edge to fix the imbalance."
          value={data.maker_imbalance_skew_max ?? 0.03}
          min={0}
          max={0.1}
          step={0.005}
          unit=""
          onSubmit={(v) => apply({ maker_imbalance_skew_max: v })}
        />

        {GAP}

        <FloatInput
          label="Min Imbalance to Trigger"
          description="Minimum YES/NO contract gap before per-market skew activates. Filters out small natural fluctuations — only kick in when the imbalance is meaningful."
          value={data.maker_imbalance_skew_min_ct ?? 10}
          min={0}
          max={100}
          step={5}
          unit="ct"
          onSubmit={(v) => apply({ maker_imbalance_skew_min_ct: v })}
        />

        <SectionHead title="CLOB Depth Gate" />
        <p className="settings-desc" style={{ color: "#9ca3af", fontSize: "0.82rem", margin: "0.25rem 0 0.75rem" }}>
          Controls how thin order books affect quoting. The hard gate suppresses signals entirely when depth is below the minimum;
          spread factors widen half-spread on thin or empty books, which boosts the edge score (Factor 1) but also reduces fill probability.
        </p>

        {GAP}

        <NumberInput
          label="Min Depth to Quote"
          description="Minimum order-book depth (contracts) on both bid and ask before quoting. Set to 0 to disable the gate."
          value={data.maker_min_depth_to_quote ?? 0}
          step={10}
          unit="ct"
          onSubmit={(v) => apply({ maker_min_depth_to_quote: v })}
        />

        {GAP}

        <NumberInput
          label="Thin Book Threshold"
          description="Below this depth (contracts) the spread is widened by the thin-book factor. A depth of 0 uses the empty-book factor instead."
          value={data.maker_depth_thin_threshold ?? 50}
          step={10}
          unit="ct"
          onSubmit={(v) => apply({ maker_depth_thin_threshold: v })}
        />

        {GAP}

        <FloatInput
          label="Spread Factor (thin book)"
          description="Multiplier applied to half-spread when depth is below the thin threshold. 1.0 = no widening."
          value={data.maker_depth_spread_factor_thin ?? 1.0}
          min={1.0}
          max={5.0}
          step={0.1}
          unit="×"
          onSubmit={(v) => apply({ maker_depth_spread_factor_thin: v })}
        />

        {GAP}

        <FloatInput
          label="Spread Factor (empty book)"
          description="Multiplier applied to half-spread when book depth is exactly zero. 1.0 = no widening."
          value={data.maker_depth_spread_factor_zero ?? 1.0}
          min={1.0}
          max={5.0}
          step={0.1}
          unit="×"
          onSubmit={(v) => apply({ maker_depth_spread_factor_zero: v })}
        />

        <SectionHead title="HL Delta Hedge" />

        <Toggle
          label="HL Hedge Enabled"
          description="Enable or disable the HyperLiquid perp delta-hedge leg. When OFF, PM fills accumulate unhedged. Also controllable from the Dashboard strips."
          value={data.maker_hedge_enabled ?? true}
          onChange={(v) => apply({ maker_hedge_enabled: v })}
        />
        {!(data.maker_hedge_enabled ?? true) && (
          <div className="banner info" style={{ marginTop: "0.75rem", background: "#431407", border: "1px solid #f59e0b", color: "#fcd34d" }}>
            ⚠️ Delta hedge is OFF — directional PM exposure will accumulate without HL offset.
          </div>
        )}

        {GAP}

        <FloatInput
          label="Hedge Threshold"
          description="Minimum net inventory (USD) per coin before an HL delta hedge is placed."
          value={data.hedge_threshold_usd}
          min={10}
          max={1000}
          step={10}
          unit="USD"
          onSubmit={(v) => apply({ hedge_threshold_usd: v })}
        />

        {GAP}

        <FloatInput
          label="Hedge Rebalance %"
          description="Only resize an existing HL hedge when the required change exceeds this % of the current hedge notional. Ignores small adjustments."
          value={(data.hedge_rebalance_pct ?? 0.20) * 100}
          min={1}
          max={100}
          step={1}
          unit="%"
          onSubmit={(v) => apply({ hedge_rebalance_pct: v / 100 })}
        />

        {GAP}

        <FloatInput
          label="Hedge Min Interval"
          description="Per-coin cooldown (seconds) between two executed HL hedges. Prevents churning when many PM fills arrive simultaneously."
          value={data.hedge_min_interval ?? 8}
          min={0}
          max={120}
          step={1}
          unit="s"
          onSubmit={(v) => apply({ hedge_min_interval: v })}
        />

        {GAP}

        <FloatInput
          label="Hedge Debounce"
          description="Batch window (seconds): wait this long for additional fills to accumulate before executing a single combined hedge order. Set to 0 to disable."
          value={data.hedge_debounce_secs ?? 3}
          min={0}
          max={30}
          step={0.5}
          unit="s"
          onSubmit={(v) => apply({ hedge_debounce_secs: v })}
        />

        {GAP}

        <FloatInput
          label="Max HL Notional"
          description="Hard ceiling on any single HL delta-hedge trade (USD notional). Prevents oversized hedges on illiquid perps."
          value={data.max_hl_notional ?? 3000}
          min={100}
          max={50000}
          step={100}
          unit="USD"
          onSubmit={(v) => apply({ max_hl_notional: v })}
        />



        <SectionHead title="Paper Fill Simulation" />
        <p className="settings-desc" style={{ marginBottom: "0.75rem" }}>
          Controls how the FillSimulator generates synthetic fills in paper mode. Has no effect in live mode.
        </p>

        <FloatInput
          label="Fill Probability (normal)"
          description="Base probability a paper quote gets filled each sweep."
          value={data.paper_fill_prob_base}
          min={0.01}
          max={1.0}
          step={0.01}
          unit=""
          onSubmit={(v) => apply({ paper_fill_prob_base: v })}
        />

        {GAP}

        <FloatInput
          label="Fill Probability (new market)"
          description="Higher fill probability for newly-discovered markets (tighter spreads at listing)."
          value={data.paper_fill_prob_new_market}
          min={0.01}
          max={1.0}
          step={0.01}
          unit=""
          onSubmit={(v) => apply({ paper_fill_prob_new_market: v })}
        />

        {GAP}

        <FloatInput
          label="Adverse Selection Trigger %"
          description="HL mid must move more than this fraction between sweeps to trigger the adverse-selection fill penalty."
          value={data.paper_adverse_selection_pct}
          min={0.001}
          max={0.05}
          step={0.001}
          unit=""
          onSubmit={(v) => apply({ paper_adverse_selection_pct: v })}
        />

        {GAP}

        <FloatInput
          label="Adverse Fill Multiplier"
          description="Multiply fill probability by this when adverse selection is detected (HL moved against our fill side). 0.15 = 85% reduction."
          value={data.paper_adverse_fill_multiplier ?? 0.15}
          min={0.01}
          max={1.0}
          step={0.01}
          unit=""
          onSubmit={(v) => apply({ paper_adverse_fill_multiplier: v })}
        />
      </div>

      {/* ── Current Runtime Config (read-only) ───────────────────────── */}
      <div className="card">
        <h3>Current Runtime Config</h3>
        <table className="kv-table">
          <tbody>
            <tr><td colSpan={2} style={{ color: "#64748b", fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.06em", paddingTop: "0.5rem" }}>Global</td></tr>
            <tr><td>PAPER_TRADING</td><td className="mono">{String(data.paper_trading)}</td></tr>
            <tr><td>AGENT_AUTO</td><td className="mono">{String(data.agent_auto)}</td></tr>
            <tr><td>AUTO_APPROVE</td><td className="mono">{String(data.auto_approve)}</td></tr>
            <tr><td>PAPER_CAPITAL_USD</td><td className="mono">${(data.paper_capital_usd ?? 10000).toFixed(0)}</td></tr>
            <tr><td>STRATEGY_MISPRICING_ENABLED</td><td className="mono">{String(data.strategy_mispricing)}</td></tr>
            <tr><td>STRATEGY_MAKER_ENABLED</td><td className="mono">{String(data.strategy_maker)}</td></tr>
            <tr><td>MAX_CONCURRENT_POSITIONS</td><td className="mono">{data.max_concurrent_positions}</td></tr>
            <tr><td>MAX_PM_EXPOSURE_PER_MARKET</td><td className="mono">${(data.max_pm_exposure_per_market ?? 500).toFixed(0)}</td></tr>
            <tr><td>MAX_TOTAL_PM_EXPOSURE</td><td className="mono">${(data.max_total_pm_exposure ?? 2000).toFixed(0)}</td></tr>
            <tr><td>HARD_STOP_DRAWDOWN</td><td className="mono">${(data.hard_stop_drawdown ?? 500).toFixed(0)}</td></tr>

            <tr><td colSpan={2} style={{ color: "#64748b", fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.06em", paddingTop: "1rem" }}>Strategy 2 — Mispricing</td></tr>
            <tr><td>SCAN_INTERVAL</td><td className="mono">{data.scan_interval}s</td></tr>
            <tr><td>MAX_CONCURRENT_MISPRICING_POSITIONS</td><td className="mono">{data.max_concurrent_mispricing_positions}</td></tr>
            <tr><td>KALSHI_ENABLED</td><td className="mono">{String(data.kalshi_enabled)}</td></tr>
            <tr><td>KALSHI_REQUIRE_ND2_CONFIRMATION</td><td className="mono">{String(data.kalshi_require_nd2_confirmation)}</td></tr>
            <tr><td>KALSHI_MIN_DEVIATION</td><td className="mono">{(data.kalshi_min_deviation ?? 0).toFixed(2)}</td></tr>
            <tr><td>KALSHI_MATCH_MAX_STRIKE_DIFF</td><td className="mono">{(data.kalshi_match_max_strike_diff * 100).toFixed(1)}%</td></tr>
            <tr><td>KALSHI_MATCH_MAX_EXPIRY_DAYS</td><td className="mono">{data.kalshi_match_max_expiry_days}d</td></tr>
            <tr><td>PROFIT_TARGET_PCT</td><td className="mono">{((data.profit_target_pct ?? 0.6) * 100).toFixed(0)}%</td></tr>
            <tr><td>STOP_LOSS_USD</td><td className="mono">${(data.stop_loss_usd ?? 25).toFixed(0)}</td></tr>
            <tr><td>EXIT_DAYS_BEFORE_RESOLUTION</td><td className="mono">{data.exit_days_before_resolution ?? 3}d</td></tr>
            <tr><td>MIN_HOLD_SECONDS</td><td className="mono">{data.min_hold_seconds ?? 60}s</td></tr>
            <tr><td>FILL_CHECK_INTERVAL</td><td className="mono">{data.fill_check_interval}s</td></tr>
            <tr><td>PAPER_FILL_PROBABILITY</td><td className="mono">{(data.paper_fill_probability * 100).toFixed(0)}%</td></tr>
            <tr><td>MAX_BUY_NO_YES_PRICE</td><td className="mono">{(data.max_buy_no_yes_price ?? 0).toFixed(2)}</td></tr>
            <tr><td>MARKET_COOLDOWN_SECONDS</td><td className="mono">{data.market_cooldown_seconds}s ({(data.market_cooldown_seconds / 60).toFixed(0)} min)</td></tr>
            <tr><td>MIN_STRIKE_DISTANCE_PCT</td><td className="mono">{(data.min_strike_distance_pct * 100).toFixed(0)}%</td></tr>

            <tr><td colSpan={2} style={{ color: "#64748b", fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.06em", paddingTop: "1rem" }}>Strategy 1 — Market Making</td></tr>
            <tr><td>MAX_CONCURRENT_MAKER_POSITIONS</td><td className="mono">{data.max_concurrent_maker_positions}</td></tr>
            <tr><td>MAX_MAKER_POSITIONS_PER_UNDERLYING</td><td className="mono">{data.maker_positions_per_underlying}</td></tr>
            <tr><td>MAKER_COIN_MAX_LOSS_USD</td><td className="mono">${(data.maker_coin_max_loss_usd ?? 0).toFixed(0)}</td></tr>
            <tr><td>MAKER_EXIT_HOURS</td><td className="mono">{data.maker_exit_hours}h</td></tr>
            <tr><td>MAKER_EXIT_TTE_FRAC</td><td className="mono">{((data.maker_exit_tte_frac ?? 0.10) * 100).toFixed(0)}%</td></tr>
            <tr><td>MAKER_ENTRY_TTE_FRAC</td><td className="mono">{((data.maker_entry_tte_frac ?? 0.10) * 100).toFixed(0)}%</td></tr>
            <tr><td>MAKER_BATCH_SIZE</td><td className="mono">{data.maker_batch_size ?? 50} ct</td></tr>
            <tr><td>MAKER_DEPLOYMENT_MODE</td><td className="mono">{data.deployment_mode ?? "auto"}</td></tr>
            <tr><td>REPRICE_TRIGGER_PCT</td><td className="mono">{(data.reprice_trigger_pct * 100).toFixed(2)}%</td></tr>
            <tr><td>MAX_QUOTE_AGE_SECONDS</td><td className="mono">{data.max_quote_age_seconds}s</td></tr>
            <tr><td>MIN_EDGE_PCT</td><td className="mono">{(data.min_edge_pct * 100).toFixed(2)}%</td></tr>
            <tr><td>MAKER_QUOTE_SIZE_PCT</td><td className="mono">{((data.maker_quote_size_pct ?? 0.01) * 100).toFixed(1)}%</td></tr>
            <tr><td>MAKER_QUOTE_SIZE_NEW_MARKET</td><td className="mono">${(data.maker_quote_size_new_market ?? 25).toFixed(0)}</td></tr>
            <tr><td>MAKER_QUOTE_SIZE_MIN</td><td className="mono">${(data.maker_quote_size_min ?? 10).toFixed(0)}</td></tr>
            <tr><td>MAKER_QUOTE_SIZE_MAX</td><td className="mono">${(data.maker_quote_size_max ?? 200).toFixed(0)}</td></tr>
            <tr><td>MAKER_MIN_QUOTE_PRICE</td><td className="mono">{(data.maker_min_quote_price ?? 0.05).toFixed(2)}</td></tr>
            <tr><td>MAKER_MIN_VOLUME_24HR</td><td className="mono">${(data.maker_min_volume_24hr ?? 5000).toFixed(0)}</td></tr>
            <tr><td>MAKER_MAX_TTE_DAYS</td><td className="mono">{data.maker_max_tte_days ?? 14}d</td></tr>
            <tr><td>NEW_MARKET_AGE_LIMIT</td><td className="mono">{data.new_market_age_limit ?? 3600}s</td></tr>
            <tr><td>NEW_MARKET_WIDE_SPREAD</td><td className="mono">{(data.new_market_wide_spread ?? 0.08).toFixed(3)}</td></tr>
            <tr><td>NEW_MARKET_PULL_SPREAD</td><td className="mono">{(data.new_market_pull_spread ?? 0.02).toFixed(3)}</td></tr>
            <tr><td>INVENTORY_SKEW_COEFF</td><td className="mono">{(data.inventory_skew_coeff ?? 0.0001).toFixed(5)}</td></tr>
            <tr><td>INVENTORY_SKEW_MAX</td><td className="mono">{(data.inventory_skew_max ?? 0.01).toFixed(3)}</td></tr>
            <tr><td>HEDGE_THRESHOLD_USD</td><td className="mono">${(data.hedge_threshold_usd ?? 0).toFixed(0)}</td></tr>
            <tr><td>HEDGE_REBALANCE_PCT</td><td className="mono">{((data.hedge_rebalance_pct ?? 0.20) * 100).toFixed(0)}%</td></tr>
            <tr><td>HEDGE_MIN_INTERVAL</td><td className="mono">{data.hedge_min_interval ?? 8}s</td></tr>
            <tr><td>HEDGE_DEBOUNCE_SECS</td><td className="mono">{data.hedge_debounce_secs ?? 3}s</td></tr>
            <tr><td>MAX_HL_NOTIONAL</td><td className="mono">${(data.max_hl_notional ?? 3000).toFixed(0)}</td></tr>
            <tr><td>MAKER_HEDGE_ENABLED</td><td className="mono">{(data.maker_hedge_enabled ?? true) ? 'true' : 'false'}</td></tr>
            <tr><td>MAKER_MAX_BOOK_AGE_SECS</td><td className="mono">{data.maker_max_book_age_secs ?? 0}s</td></tr>
            <tr><td>MAKER_IMBALANCE_SKEW_COEFF</td><td className="mono">{(data.maker_imbalance_skew_coeff ?? 0.0005).toFixed(4)}</td></tr>
            <tr><td>MAKER_IMBALANCE_SKEW_MAX</td><td className="mono">{(data.maker_imbalance_skew_max ?? 0.03).toFixed(3)}</td></tr>
            <tr><td>MAKER_IMBALANCE_SKEW_MIN_CT</td><td className="mono">{data.maker_imbalance_skew_min_ct ?? 10} ct</td></tr>
            <tr><td>MAKER_MIN_DEPTH_TO_QUOTE</td><td className="mono">{data.maker_min_depth_to_quote ?? 0} ct</td></tr>
            <tr><td>MAKER_DEPTH_THIN_THRESHOLD</td><td className="mono">{data.maker_depth_thin_threshold ?? 50} ct</td></tr>
            <tr><td>MAKER_DEPTH_SPREAD_FACTOR_THIN</td><td className="mono">{(data.maker_depth_spread_factor_thin ?? 1.0).toFixed(2)}×</td></tr>
            <tr><td>MAKER_DEPTH_SPREAD_FACTOR_ZERO</td><td className="mono">{(data.maker_depth_spread_factor_zero ?? 1.0).toFixed(2)}×</td></tr>
            <tr><td>MAKER_MIN_SIGNAL_SCORE_5M</td><td className="mono">{data.maker_min_signal_score_5m ?? 0}</td></tr>
            <tr><td>MAKER_MIN_SIGNAL_SCORE_1H</td><td className="mono">{data.maker_min_signal_score_1h ?? 0}</td></tr>
            <tr><td>MAKER_MIN_SIGNAL_SCORE_4H</td><td className="mono">{data.maker_min_signal_score_4h ?? 0}</td></tr>
            <tr><td>MAKER_EXIT_TTE_FRAC_5M</td><td className="mono">{(data.maker_exit_tte_frac_5m ?? 0).toFixed(2)}</td></tr>
            <tr><td>MAKER_EXCLUDED_MARKET_TYPES</td><td className="mono">{(data.maker_excluded_market_types ?? []).join(', ') || 'none'}</td></tr>
            <tr><td>PAPER_FILL_PROB_BASE</td><td className="mono">{(data.paper_fill_prob_base * 100).toFixed(0)}%</td></tr>
            <tr><td>PAPER_FILL_PROB_NEW_MARKET</td><td className="mono">{(data.paper_fill_prob_new_market * 100).toFixed(0)}%</td></tr>
            <tr><td>PAPER_ADVERSE_SELECTION_PCT</td><td className="mono">{(data.paper_adverse_selection_pct * 100).toFixed(2)}%</td></tr>
            <tr><td>PAPER_ADVERSE_FILL_MULTIPLIER</td><td className="mono">{(data.paper_adverse_fill_multiplier ?? 0.15).toFixed(2)}</td></tr>
          </tbody>
        </table>
      </div>

      {saving && <div className="muted" style={{ textAlign: "center" }}>Saving…</div>}
    </div>
  );
}
