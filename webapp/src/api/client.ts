/**
 * api/client.ts — Typed API client for the FastAPI backend.
 *
 * Live data hooks (useHealth, usePositions, useRisk, useMarkets, useFunding,
 * useSignals, useMomentumSignals, useMakerQuotes, useMakerSignals, useCapital)
 * subscribe to the SSE stream at /events and receive push updates from the
 * backend instead of polling.  Each hook still falls back to a REST fetch on
 * initial mount before the SSE connection delivers its first message.
 *
 * Slow-changing or expensive endpoints keep REST polling on longer intervals:
 *   usePnl         — 30 s (CSV file read, not in SSE stream)
 *   useConfig      — 30 s (rarely changes)
 *   useLivePositions — 15 s (live Polymarket API call, expensive)
 *   useTrades      — 30 s (historical, not time-critical)
 *   useLogs        — 2 s  (ring buffer, stays as-is)
 *   useErrorLogs   — 10 s (ring buffer, stays as-is)
 *
 * Set VITE_API_URL in .env (or Vercel environment) to point to the backend.
 */
import { useEffect, useState, useCallback, useRef } from "react";

export const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8080";
const LAUNCHER_URL = import.meta.env.VITE_LAUNCHER_URL ?? "http://localhost:8081";
const POLL_INTERVAL_MS = 5_000;

// ── SSE singleton ─────────────────────────────────────────────────────────────
// A single EventSource connection shared across all hooks.  When the server
// pushes a state bundle, each registered callback receives its slice of the data.

type SSECallback<T> = (data: T) => void;

const _sseCallbacks = new Map<string, Set<SSECallback<unknown>>>();
let _sseSource: EventSource | null = null;

function _ensureSSE() {
  if (_sseSource) return;
  _sseSource = new EventSource(`${BASE_URL}/events`);

  _sseSource.onopen = () => { /* connected */ };

  _sseSource.onmessage = (ev: MessageEvent) => {
    try {
      const bundle = JSON.parse(ev.data) as Record<string, unknown>;
      for (const [key, callbacks] of _sseCallbacks.entries()) {
        if (bundle[key] !== undefined) {
          callbacks.forEach(cb => (cb as SSECallback<unknown>)(bundle[key]));
        }
      }
    } catch {
      // malformed message — ignore
    }
  };

  _sseSource.onerror = () => {
    // Browser reconnects automatically on close; tear down our source to force a fresh connection
    _sseSource?.close();
    _sseSource = null;
    // Re-connect after a brief delay so we don't spam during downtime
    setTimeout(_ensureSSE, 3_000);
  };
}

function _registerSSE<T>(key: string, cb: SSECallback<T>): () => void {
  _ensureSSE();
  if (!_sseCallbacks.has(key)) _sseCallbacks.set(key, new Set());
  (_sseCallbacks.get(key) as Set<SSECallback<unknown>>).add(cb as SSECallback<unknown>);
  return () => {
    _sseCallbacks.get(key)?.delete(cb as SSECallback<unknown>);
  };
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface DataQuality {
  sub_token_count: number;
  sub_rejected_count: number;
  market_count: number;
  fresh_book_count: number;
  stale_book_count: number;
  no_book_count: number;
  spot_mids?: Record<string, number>;    // coin → latest RTDS exchange price
  spot_ages_s?: Record<string, number | null>;  // coin → seconds since last RTDS exchange update; null if never received
  chainlink_ages_s?: Record<string, number | null>; // coin → seconds since last Chainlink oracle update; null if never received
  chainlink_mids?: Record<string, number | null>;   // coin → latest Chainlink oracle price
  chainlink_streams_connected?: boolean;  // ChainlinkStreamsClient WS open
  chainlink_ws_connected?: boolean;       // ChainlinkWSClient (on-chain Polygon) WS open
}

export interface HealthData {
  status: string;
  uptime_seconds: number;
  pm_ws_connected: boolean;
  hl_ws_connected: boolean;
  last_heartbeat_age_s: number | null;
  paper_trading: boolean;
  agent_auto: boolean;
  bot_active: boolean;
  data_quality: DataQuality;
  data_issues: boolean;
  adverse_triggers_session: number;
  adverse_threshold_pct: number;
  hl_max_move_pct_session: number;
  timestamp: number;
}

export interface Position {
  condition_id: string;
  market_title: string;
  market_slug?: string;
  underlying: string;
  side: string;
  size_usd: number;           // legacy field — equals contracts (contract count)
  contracts: number;          // explicit contract count (same value, better name)
  entry_cost_usd: number;     // actual USDC capital deployed at fill
  entry_price: number;        // YES-token price at fill
  token_entry_price: number;  // side-adjusted entry price (NO → 1 − entry_price)
  token_current_price?: number | null;  // side-adjusted current price
  current_mid?: number | null;  // raw YES-token mid from live orderbook
  unrealised_pnl_usd?: number | null;  // server-computed P&L, same formula as monitor
  book_age_s?: number | null;  // age of PM book snapshot for this position (seconds)
  strategy: string;
  venue: string;
  opened_at: string | null;
  hl_size_coins?: number;  // only present for venue="HL" hedge rows
  // Active bid-quote fill state (YES positions; null when no resting BID order)
  active_bid_original_ct?: number | null;
  active_bid_remaining_ct?: number | null;
  active_bid_filled_ct?: number | null;
  // Active ask-quote fill state (NO positions; null when no resting ASK order)
  active_ask_original_ct?: number | null;
  active_ask_remaining_ct?: number | null;
  active_ask_filled_ct?: number | null;
  // Stable CLOB order tracking — persists across reprices
  order_id?: string | null;
  // Market resolution time
  end_date?: string | null;
  // Profit-target progress (mispricing only; null for maker)
  pct_of_target?: number | null;
  profit_target_usd?: number | null;
  stop_loss_usd?: number | null;
  // Signal quality score 0–100 at fill time (stamped on ActiveQuote at deployment)
  signal_score?: number | null;
  // Live order book bid/ask for this market's YES token
  yes_book_bid?: number | null;
  yes_book_ask?: number | null;
  // Entry rebates already credited from maker fills
  pm_rebates_earned?: number | null;
  // Estimated P&L if this leg is closed now at book (incl. rebates, net of taker fees)
  est_close_pnl?: number | null;
  // Recently-closed visibility (present only during grace period after close)
  is_closed?: boolean;
  closed_at?: string | null;
  realized_pnl?: number | null;
  // Legacy field — null for all new positions
  spread_id?: string | null;
  // Opening neutral pair tracking (strategy="opening_neutral" only)
  neutral_pair_id?: string | null;
  strike?: number | null;
  // GTD hedge (momentum strategy only; null for maker/mispricing)
  hedge_order_id?: string | null;
  hedge_token_id?: string | null;
  hedge_price?: number | null;
  hedge_size_usd?: number | null;
  // Set true when a WS MATCHED event confirmed the hedge order filled mid-trade
  hedge_fill_detected?: boolean | null;
  hedge_fill_size?: number | null;
}

export interface Trade {
  market_id: string;
  market_title?: string;     // human-readable question; may be absent on old records
  underlying?: string;       // BTC | ETH | SOL etc. (not present for hl_perp rows)
  market_type: string;
  strategy: string;
  side: string;
  size: string;
  price: string;
  fees_paid: string;
  rebates_earned: string;
  hl_hedge_size: string;
  hl_entry_price: string;
  spot_price: string;       // underlying spot (BTC/ETH/…) at entry
  exit_spot_price?: string; // underlying spot at exit (0 if resolved or not yet recorded)
  pnl: string;
  timestamp: string;
  signal_score?: string;    // 0–100 signal quality score at fill time
  resolved_outcome?: string; // "WIN" | "LOSS" | "" — set on RESOLVED exits; empty for taker/stop
  spread_id?: string;        // legacy field; null for all new positions
  strike?: string;           // recorded strike price (window-open spot for Up/Down, parsed from title otherwise)
  // GTD hedge (momentum strategy only; empty for maker/mispricing)
  hedge_order_id?: string;
  hedge_token_id?: string;
  hedge_price?: string;
  hedge_size_usd?: string;
  // Hedge fill lifecycle — set when the resting bid is matched (HedgeOrder)
  hedge_size_filled?: string;
  hedge_avg_fill_price?: string;
  // Hedge outcome — written when market resolves (PM API is source of truth)
  hedge_status?: string;        // "filled_won" | "filled_lost" | "unfilled" | "cancelled" | ""
  spot_resolve_price?: string;  // oracle spot at market resolution (hedge rows); "0" otherwise
}

export interface PnlData {
  today: number;
  week: number;
  all_time: number;
  trade_count_today: number;
  trade_count_week: number;
  trade_count_all: number;
}

export interface EquityPoint {
  t: string;
  equity: number;
}

export interface PerformanceSummary {
  total_trades: number;
  win_rate: number;
  avg_pnl: number;
  total_pnl: number;
  total_fees: number;
  total_rebates: number;
  max_drawdown: number;
  sharpe_7d: number | null;
}

export interface PerformanceData {
  period: string;
  no_data?: boolean;
  summary: PerformanceSummary;
  equity_curve: EquityPoint[];
  by_strategy: Record<string, { pnl: number; count: number }>;
  by_underlying: Record<string, { pnl: number; count: number }>;
  by_market_type: Record<string, { pnl: number; count: number; win_rate: number }>;
  pnl_histogram: Array<{ bucket_start: number; bucket_end: number; count: number }>;
  best_trades: Trade[];
  worst_trades: Trade[];
  time_of_day_heatmap: Array<{ hour_hkt: number; avg_pnl: number; trade_count: number }>;
}

export interface Signal {
  market_id: string;
  market_title: string;
  pm_price: number;
  implied_prob: number;
  deviation: number;
  direction: string;
  fee_hurdle: number;
  deribit_iv: number;
  deribit_instrument: string;
  is_actionable: boolean;
  score?: number;
  signal_source?: string;
  timestamp: number;
  agent_decision: string | null;
  agent_confidence?: number;
  agent_reason?: string;
}

export interface MakerQuote {
  market_id: string;
  token_id: string;
  side: string;
  price: number;
  size: number;
  order_id: string | null;
  posted_at: number;
  age_seconds: number;
  market_title: string;
  underlying: string;
}

export interface MakerSignal {
  market_id: string;
  market_title: string;
  market_slug: string;
  token_id: string;
  underlying: string;
  mid: number;
  bid_price: number;
  ask_price: number;
  half_spread: number;
  effective_edge: number;
  market_type: string;
  ts: number;
  age_seconds: number;
  is_deployed: boolean;
  collateral_usd: number;
  // Partial-fill tracking (present when is_deployed=true)
  bid_original_size?: number;
  bid_remaining_size?: number;
  bid_filled_size?: number;
  ask_original_size?: number;
  ask_remaining_size?: number;
  ask_filled_size?: number;
  total_original_size?: number;
  total_remaining_size?: number;
  total_filled_size?: number;
  fill_pct?: number;
  // Market resolution time
  end_date?: string | null;
  // Signal quality score 0–100
  score?: number;
}

export interface CapitalData {
  total_budget: number;
  deployed: number;
  in_positions: number;
  available: number;
  mode: string;
  timestamp: number;
}

export interface FillEntry {
  timestamp: string;
  market_id: string;
  market_title: string;
  underlying: string;
  order_side: string;       // BUY | SELL
  position_side: string;    // YES | NO | UP | DOWN
  fill_price: string;
  contracts_filled: string;
  fill_cost_usd: string;
  book_bid: string;
  book_ask: string;
  depth_at_level: string;
  arrival_prob: string;
  mean_taker: string;
  taker_size_drawn: string;
  hl_mid: string;
  hl_move_pct: string;
  adverse: string;          // "True" | "False"
  total_fills_session: string;
}

export interface RiskData {
  pm_exposure_usd: number;
  pm_exposure_limit: number;
  pm_exposure_pct: number;
  hl_notional_usd: number;
  hl_notional_limit: number;
  hl_notional_pct: number;
  open_positions: number;
  max_concurrent_positions: number;
  max_pm_per_market?: number;
  hard_stop_threshold: number;
  paper_trading: boolean;
}

export interface Market {
  condition_id: string;
  title: string;
  market_type: string;
  underlying: string;
  fees_enabled: boolean;
  token_id_yes: string;
  bid_price: number | null;
  ask_price: number | null;
  bid_source: "active_quote" | "pm_book" | null;
  ask_source: "active_quote" | "pm_book" | null;
  book_age_s: number | null;
  data_warning: "stale" | "very_stale" | "no_data" | null;
  quoted: boolean;
}

export interface FundingEntry {
  predicted_rate: number;
  open_interest: number;
  fetched_at: number;
}

export interface MomentumSignal {
  market_id: string;
  market_title: string;
  underlying: string;
  market_type: string;
  side: string;          // "YES" | "NO"  or  "UP" | "DOWN" for Up-or-Down markets
  token_id: string;
  token_price: number;   // 0-1 token price for the high side
  p_yes: number;         // raw YES mid
  delta_pct: number;     // % spot move toward winning direction
  threshold_pct: number; // dynamic vol threshold that was crossed
  spot: number;          // HL spot at signal time
  strike: number;        // parsed strike from market title
  tte_seconds: number;   // seconds to resolution
  sigma_ann: number;     // annualised vol used
  vol_source: string;    // "deribit_atm" | "hl_realized"
  timestamp: number;
}

export interface ConfigData {
  paper_trading: boolean;
  agent_auto: boolean;
  auto_approve: boolean;
  mispricing_scan_interval: number;
  strategy_mispricing: boolean;
  strategy_maker: boolean;
  fill_check_interval: number;
  paper_fill_probability: number;
  max_buy_no_yes_price: number;
  mispricing_market_cooldown_seconds: number;
  momentum_market_cooldown_seconds?: number;
  min_strike_distance_pct: number;
  kalshi_enabled: boolean;
  kalshi_require_nd2_confirmation: boolean;
  kalshi_min_deviation: number;
  kalshi_match_max_strike_diff: number;
  kalshi_match_max_expiry_days: number;
  max_concurrent_positions: number;
  // Market-making config
  reprice_trigger_pct: number;
  max_quote_age_seconds: number;
  maker_min_edge_pct: number;
  max_concurrent_maker_positions: number;
  max_concurrent_mispricing_positions: number;
  paper_fill_prob_base: number;
  paper_fill_prob_new_market: number;
  paper_adverse_selection_pct: number;
  paper_adverse_fill_multiplier: number;
  maker_coin_max_loss_usd: number;
  maker_exit_hours: number;
  maker_exit_tte_frac?: number;
  maker_entry_tte_frac?: number;
  maker_batch_size?: number;
  maker_positions_per_underlying: number;
  maker_quote_size_pct: number;
  maker_quote_size_min: number;
  maker_quote_size_max: number;
  maker_quote_size_new_market: number;
  hedge_threshold_usd: number;
  hedge_rebalance_pct: number;
  hedge_min_interval?: number;
  hedge_debounce_secs?: number;
  deployment_mode: string;
  paper_capital_usd: number;
  // Quote guards & new-market logic
  maker_min_quote_price: number;
  maker_min_volume_24hr: number;
  maker_max_tte_days: number;
  new_market_age_limit: number;
  new_market_wide_spread: number;
  new_market_pull_spread: number;
  // Inventory skew
  inventory_skew_coeff: number;
  inventory_skew_max: number;
  // Hedge sizing
  max_hl_notional: number;
  // Position monitor
  profit_target_pct: number;
  stop_loss_usd: number;
  exit_days_before_resolution: number;
  min_hold_seconds: number;
  // Risk limits
  max_pm_exposure_per_market: number;
  max_total_pm_exposure: number;
  hard_stop_drawdown: number;
  // Signal scoring
  min_signal_score_mispricing?: number;
  min_signal_score_maker?: number;
  maker_min_signal_score_5m?: number;
  maker_min_signal_score_1h?: number;
  maker_min_signal_score_4h?: number;
  maker_exit_tte_frac_5m?: number;
  score_weight_edge?: number;
  score_weight_source?: number;
  score_weight_timing?: number;
  score_weight_liquidity?: number;
  // Hedge control
  maker_hedge_enabled?: boolean;
  maker_max_book_age_secs?: number;
  // Per-market imbalance skew
  maker_imbalance_skew_coeff?: number;
  maker_imbalance_skew_max?: number;
  maker_imbalance_skew_min_ct?: number;
  // Incentive spread gate & imbalance hard-stops
  maker_min_incentive_spread?: number;
  maker_max_imbalance_contracts?: number;
  maker_naked_close_enabled?: boolean;
  maker_naked_close_contracts?: number;
  maker_naked_close_secs?: number;
  maker_max_fills_per_leg?: number;
  maker_max_contracts_per_market?: number;
  // Volatility & drift guards
  maker_vol_filter_pct?: number;
  maker_adverse_drift_reprice?: number;
  // Second-leg profit margin
  maker_min_spread_profit_margin?: number;
  // CLOB depth gate
  maker_min_depth_to_quote?: number;
  maker_depth_thin_threshold?: number;
  maker_depth_spread_factor_thin?: number;
  maker_depth_spread_factor_zero?: number;
  maker_excluded_market_types: string[];
  timestamp: number;
  // Strategy 3 — Momentum Scanner
  strategy_momentum?: boolean;
  momentum_price_band_low?: number;
  momentum_price_band_high?: number;
  momentum_max_entry_usd?: number;
  momentum_kelly_fraction?: number;
  momentum_min_clob_depth?: number;
  momentum_order_type?: string;
  momentum_delta_stop_loss_pct?: number;
  momentum_take_profit?: number;
  momentum_min_tte_5m?: number;
  momentum_min_tte_15m?: number;
  momentum_min_tte_1h?: number;
  momentum_min_tte_4h?: number;
  momentum_min_tte_daily?: number;
  momentum_min_tte_weekly?: number;
  momentum_min_tte_milestone?: number;
  momentum_min_tte_default?: number;
  momentum_spot_max_age_secs?: number;
  momentum_book_max_age_secs?: number;
  momentum_vol_cache_ttl?: number;
  momentum_vol_z_score?: number;
  momentum_vol_z_score_5m?: number;
  momentum_vol_z_score_15m?: number;
  momentum_vol_z_score_1h?: number;
  momentum_vol_z_score_4h?: number;
  momentum_vol_z_score_daily?: number;
  // Per-coin delta stop-loss overrides
  momentum_delta_sl_pct_btc?: number;
  momentum_delta_sl_pct_eth?: number;
  momentum_delta_sl_pct_bnb?: number;
  momentum_delta_sl_pct_xrp?: number;
  momentum_delta_sl_pct_sol?: number;
  momentum_delta_sl_pct_doge?: number;
  momentum_delta_sl_pct_hype?: number;
  // Per-coin minimum delta entry floor overrides
  momentum_min_delta_pct_btc?: number;
  momentum_min_delta_pct_eth?: number;
  momentum_min_delta_pct_bnb?: number;
  momentum_min_delta_pct_xrp?: number;
  momentum_min_delta_pct_sol?: number;
  momentum_min_delta_pct_doge?: number;
  momentum_min_delta_pct_hype?: number;
  momentum_min_delta_pct_5m?: number;
  momentum_min_delta_pct_15m?: number;
  momentum_min_delta_pct_1h?: number;
  momentum_min_delta_pct_4h?: number;
  momentum_min_delta_pct_daily?: number;
  momentum_scan_interval?: number;
  momentum_max_concurrent?: number;
  momentum_min_gap_pct?: number;
  momentum_near_expiry_time_stop_secs?: number;
  momentum_min_delta_pct?: number;
  monitor_interval?: number;
  // Phase B — resolution oracle near expiry
  momentum_use_resolution_oracle_near_expiry?: boolean;
  // Phase C — per-type TTE floor (block last N seconds, 0 = OFF)
  momentum_phase_c_min_tte_5m?: number;
  momentum_phase_c_min_tte_15m?: number;
  momentum_phase_c_min_tte_1h?: number;
  momentum_phase_c_min_tte_4h?: number;
  momentum_phase_c_min_tte_daily?: number;
  momentum_phase_c_min_tte_weekly?: number;
  momentum_phase_c_min_tte_milestone?: number;
  // Phase D — hedge
  momentum_hedge_enabled?: boolean;
  momentum_hedge_enabled_5m?: boolean;
  momentum_hedge_enabled_15m?: boolean;
  momentum_hedge_enabled_1h?: boolean;
  momentum_hedge_enabled_4h?: boolean;
  momentum_hedge_enabled_daily?: boolean;
  momentum_hedge_enabled_weekly?: boolean;
  momentum_hedge_enabled_milestone?: boolean;
  momentum_hedge_price?: number;
  momentum_hedge_contracts_pct?: number;
  momentum_hedge_cancel_recovery_pct?: number;
  momentum_hedge_suppresses_delta_sl?: boolean;
  momentum_hedge_price_5m?: number;
  momentum_hedge_price_15m?: number;
  momentum_hedge_price_1h?: number;
  momentum_hedge_price_4h?: number;
  momentum_hedge_price_daily?: number;
  momentum_hedge_price_weekly?: number;
  momentum_hedge_price_milestone?: number;
  // Phase E — empirical win-rate gate
  momentum_win_rate_gate_enabled?: boolean;
  momentum_win_rate_gate_min_factor?: number;
  momentum_win_rate_gate_min_samples?: number;
  // Kelly extensions
  momentum_kelly_min_tte_seconds?: number;
  momentum_kelly_persistence_enabled?: boolean;
  momentum_kelly_persistence_z_boost_max?: number;
  momentum_kelly_multiplier_5m?: number;
  momentum_kelly_multiplier_15m?: number;
  momentum_kelly_multiplier_1h?: number;
  momentum_kelly_multiplier_4h?: number;
  momentum_kelly_multiplier_daily?: number;
  momentum_kelly_multiplier_weekly?: number;
  // Range markets sub-strategy
  momentum_range_enabled?: boolean;
  momentum_range_price_band_low?: number;
  momentum_range_price_band_high?: number;
  momentum_range_max_entry_usd?: number;
  momentum_range_vol_z_score?: number;
  momentum_range_min_tte_seconds?: number;
  // RESOLVED fallback timeout
  momentum_resolved_force_close_sec?: number;
  // Strategy 5 — Opening Neutral
  opening_neutral_enabled?: boolean;
  opening_neutral_dry_run?: boolean;
}

export interface InventoryData {
  position_delta: Record<string, number>;
  fill_inventory: Record<string, number>;
  coin_hedges: Record<string, { direction: string; size_coins: number; entry_price: number; notional_usd: number }>;
  threshold_usd: number;
  timestamp: number;
}

export interface LogEntry {
  ts: number;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  module: string;
  msg: string;
  extras: Record<string, unknown>;
}

export type ConfigPatch = Partial<Omit<ConfigData, "timestamp">>;

export async function updateConfig(patch: ConfigPatch): Promise<ConfigData> {
  const res = await fetch(`${BASE_URL}/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json = await res.json();
  return json.current as ConfigData;
}

// ── Generic polling hook (kept for slow/expensive endpoints) ──────────────────

function usePolling<T>(
  path: string,
  interval: number = POLL_INTERVAL_MS,
): { data: T | null; error: string | null; loading: boolean; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${BASE_URL}${path}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "fetch error");
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, interval);
    return () => clearInterval(timer);
  }, [fetchData, interval]);

  return { data, error, loading, refresh: fetchData };
}

// ── SSE hook — event-driven live data ─────────────────────────────────────────
// Subscribes to the /events SSE stream for live pushes and does a single REST
// fetch on mount so data is available before the first SSE message arrives.

function useSSE<T>(
  sseKey: string,
  restPath: string,
): { data: T | null; error: string | null; loading: boolean; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Keep a stable ref to setData so the SSE callback closure stays current
  const setDataRef = useRef<(v: T) => void>(setData);
  setDataRef.current = setData;

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${BASE_URL}${restPath}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "fetch error");
    } finally {
      setLoading(false);
    }
  }, [restPath]);

  useEffect(() => {
    fetchData(); // populate immediately before first SSE push

    const unregister = _registerSSE<T>(sseKey, (pushed) => {
      setDataRef.current(pushed);
      setLoading(false);
    });
    return unregister;
  }, [sseKey, fetchData]);

  return { data, error, loading, refresh: fetchData };
}

// ── Typed hooks ───────────────────────────────────────────────────────────────

// Live hooks — event-driven via SSE stream (no polling timer):
export const useHealth = () => useSSE<HealthData>("health", "/health");
export const usePositions = () => useSSE<{ positions: Position[]; count: number }>("positions", "/positions");
export const useRisk = () => useSSE<RiskData>("risk", "/risk");
export const useMarkets = () => useSSE<{ markets: Market[]; count: number }>("markets", "/markets");
export const useFunding = () => useSSE<{ funding: Record<string, FundingEntry> }>("funding", "/funding");
export const useSignals = (limit = 50) => useSSE<{ signals: Signal[]; total: number }>("signals", `/signals?limit=${limit}`);
export const useMakerQuotes = () => useSSE<{ quotes: MakerQuote[]; count: number; strategy_enabled: boolean }>("maker_quotes", "/maker/quotes");
export const useMakerSignals = () => useSSE<{ signals: MakerSignal[]; count: number; strategy_enabled: boolean }>("maker_signals", "/maker/signals");
export const useCapital = () => useSSE<CapitalData>("capital", "/maker/capital");
export const useMomentumSignals = (limit = 50) => useSSE<{ signals: MomentumSignal[]; total: number }>("momentum_signals", `/momentum/signals?limit=${limit}`);

export interface MomentumScanSummary {
  scan_ts: number;
  summary: {
    bucket_markets?: number;
    signals_fired?: number;
    skipped_band?: number;
    skipped_delta?: number;
    skipped_tte?: number;
    skipped_phase_c?: number;
    skipped_vol?: number;
    skipped_cooldown?: number;
    skipped_position_cap?: number;
    skipped_duplicate?: number;
    [key: string]: number | undefined;
  };
  timestamp: number;
}

export const useMomentumScanSummary = () =>
  usePolling<MomentumScanSummary>("/momentum/scan_summary", 15_000);

// Slow/expensive hooks — keep REST polling at relaxed intervals:
export const useConfig = () => usePolling<ConfigData>("/config", 30_000);
export const usePnl = () => useSSE<PnlData>("pnl", "/pnl");   // SSE-updated every 30 s by backend

export interface WalletPosition {
  token_id: string;
  size: number;
  avg_price: number;
  cur_price: number;
  outcome: string;
  title: string;
  condition_id: string;
  side_guess: string;
  end_date: string | null;
  in_bot_state: boolean;
  source: "pm_wallet";  // PM wallet is always the authoritative source of truth
  payout_usd?: number;
  won?: boolean;
}

export interface PositionDiscrepancy {
  token_id: string;
  type: "size_mismatch" | "unmanaged_by_bot" | "bot_ghost";
  pm_size: number;
  bot_size: number;
  bot_side?: string;  // "YES" | "NO", present for bot_ghost entries
  diff?: number;
  title: string;
  outcome?: string;
}

export interface LivePositionsData {
  wallet_positions: WalletPosition[];
  pending_redemption: WalletPosition[];
  discrepancies: PositionDiscrepancy[];
  wallet_count: number;
  pending_redemption_count: number;
  discrepancy_count: number;
  awaiting_settlement_count?: number;
  awaiting_settlement?: { won: boolean; size: number; avg_price: number; title: string; token_id: string; outcome: string; payout_usd?: number }[];
  timestamp: number;
}

export const useLivePositions = () => usePolling<LivePositionsData>("/positions/live", 15_000);

export interface MomentumDiagnosticMarket {
  market_id: string;
  market_title: string;
  underlying: string;
  market_type: string;
  skip_reason: string;
  // Prices (may be null if market hasn't reached price gate)
  p_yes: number | null;
  p_no: number | null;
  token_price: number | null;
  side: string | null;
  // Movement
  delta_pct: number | null;
  threshold_pct: number | null;
  gap_pct: number | null;        // delta - threshold (negative = below)
  observed_z: number | null;
  // Timing
  tte_seconds: number | null;
  // Vol
  sigma_ann: number | null;
  vol_source: string | null;
  // Liquidity
  ask_depth_usd: number | null;
  // Config snapshot for the row
  configured_z: number | null;
  band_lo: number | null;
  band_hi: number | null;
  min_tte_s: number | null;
  min_clob_depth: number | null;
  // Optional extras
  cooldown_remaining_s?: number | null;
  dist_to_band?: number | null;
  strike?: number | null;           // recorded window-open spot (Up/Down) or title-parsed strike
  spot?: number | null;             // live oracle spot at scan time
}

export interface MomentumDiagnosticsResponse {
  scan_ts: number;
  markets: MomentumDiagnosticMarket[];
  summary: Record<string, number>;
  timestamp: number;
}

export const useMomentumDiagnostics = () =>
  usePolling<MomentumDiagnosticsResponse>("/momentum/diagnostics", 15_000);

export interface MomentumEvent {
  schema_version: number;
  ts: string;
  event: string;
  market_id?: string;
  market_title?: string;
  underlying?: string;
  market_type?: string;
  side?: string;
  order_price?: number;
  fill_price?: number;
  fill_size?: number;
  fill_from_ws?: boolean;
  size_usd?: number;
  retry?: number;
  order_id?: string;
  timeout_s?: number;
  reason?: string;
  retries?: number;
  tp_price?: number;
  exit_reason?: string;
  bot_version?: string;
  paper?: boolean;
  [key: string]: unknown;
}

export const useMomentumEvents = (n = 200) =>
  usePolling<{ events: MomentumEvent[]; count: number }>(`/momentum/events?n=${n}`, 10_000);
export const useFills = (
  limit = 100,
  offset = 0,
  underlying?: string,
  adverseOnly = false,
) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (underlying) params.set("underlying", underlying);
  if (adverseOnly) params.set("adverse_only", "true");
  return usePolling<{ fills: FillEntry[]; total: number }>(`/fills?${params}`);
};
export const useTrades = (limit = 100, offset = 0, strategy?: string, underlying?: string) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (strategy) params.set("strategy", strategy);
  if (underlying) params.set("underlying", underlying);
  return usePolling<{ trades: Trade[]; total: number }>(`/trades?${params}`, 30_000);
};

/** Polymarket source-of-truth activity rows from data-api.polymarket.com.
 *  Each row: { proxyWallet, timestamp, type, size, usdcSize, price, side, title, slug, outcome }
 *  'type' is "TRADE" | "REDEEM". Refreshed every 30 s. */
export interface PmActivityRow {
  proxyWallet: string;
  timestamp: number;
  conditionId: string;
  type: "TRADE" | "REDEEM" | string;
  size: number;
  usdcSize: number;
  price: number;
  asset: string;
  side: "BUY" | "SELL" | string;
  outcomeIndex: number;
  title: string;
  slug: string;
  eventSlug: string;
  outcome: string;
}

export const usePmHistory = (limit = 50) =>
  usePolling<{ rows: PmActivityRow[] }>(`/pm_history?limit=${limit}`, 30_000);

/** Resolved YES-token prices keyed by condition_id.
 *  { [condition_id: string]: { resolved_yes_price: number } }
 *  Populated by monitor.py from RESOLVED exits and retroactively for taker/stop exits. */
export const useMarketOutcomes = () =>
  usePolling<Record<string, { resolved_yes_price: number }>>("/market_outcomes", 60_000);

export const usePerformance = (period: "7d" | "30d" | "all" = "all") =>
  usePolling<PerformanceData>(`/performance?period=${period}`, 30_000);
export const useInventory = () => usePolling<InventoryData>("/maker/inventory");

// ── Market P&L ────────────────────────────────────────────────────────────────

/** Per-position summary as returned inside MarketPnlRow.positions. */
export interface MarketPnlPosition {
  side: string;
  size: number;
  entry_price: number;
  realized_pnl: number;
  is_closed: boolean;
  strategy: string;
}

/** HedgeOrder summary embedded in MarketPnlRow. */
export interface MarketPnlHedge {
  order_id: string;
  status: string;
  order_price: number;
  size_filled: number;
  avg_fill_price: number;
  net_pnl: number;
  parent_side: string;
}

/** Aggregate P&L snapshot for a single market (from /market_pnl or /market_pnl/{id}). */
export interface MarketPnlRow {
  market_id: string;
  realized_pnl: number;
  /** Always 0.0 from the server — use position-level unrealised_pnl_usd for live value. */
  unrealised_pnl: number;
  hedge_realized_pnl: number;
  total_pnl: number;
  positions: MarketPnlPosition[];
  hedge: MarketPnlHedge | null;
}

/** Response from GET /market_pnl (all active markets). */
export interface MarketPnlResponse {
  markets: Record<string, MarketPnlRow>;
  timestamp: number;
}

/** Polls /market_pnl every 10 s — covers all markets that have open positions. */
export const useMarketPnl = () =>
  usePolling<MarketPnlResponse>("/market_pnl", 10_000);

export async function toggleBot(active: boolean): Promise<{ active: boolean }> {
  const res = await fetch(`${BASE_URL}/bot`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active }),
  });
  if (!res.ok) throw new Error(`Bot toggle failed: ${res.status}`);
  return res.json();
}

export async function deploySignal(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE_URL}/maker/deploy/${encodeURIComponent(tokenId)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Deploy failed: ${res.status}`);
  return res.json();
}

export async function undeployQuote(tokenId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE_URL}/maker/undeploy/${encodeURIComponent(tokenId)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`Undeploy failed: ${res.status}`);
  return res.json();
}

export async function closePosition(marketId: string): Promise<{ ok: boolean; exit_price: number; pnl: number; sides_closed: string[]; exit_prices: Record<string, number> }> {
  const res = await fetch(`${BASE_URL}/positions/${encodeURIComponent(marketId)}/close`, {
    method: "POST",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail ?? `Close failed: ${res.status}`);
  }
  return res.json();
}

export interface RedeemResult {
  ok: boolean;
  won?: boolean;
  tx_hash?: string;
  payout_usd?: number;
  bot_positions_closed?: number;
  requires_manual_claim?: boolean;
  error?: string;
}

export async function redeemPosition(
  tokenId: string,
  conditionId: string,
  won: boolean,
  payoutUsd: number,
): Promise<RedeemResult> {
  const res = await fetch(`${BASE_URL}/positions/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_id: tokenId, condition_id: conditionId, won, payout_usd: payoutUsd }),
  });
  // Always return body — backend returns meaningful data even on partial failure
  return res.json();
}

export async function dismissGhostPosition(tokenId: string, exitPrice: number = 0.0): Promise<{ ok: boolean; market_id: string; side: string; exit_price: number; size: number; pnl: number }> {
  const res = await fetch(`${BASE_URL}/positions/ghost/dismiss`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_id: tokenId, exit_price: exitPrice }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail ?? `Dismiss failed: ${res.status}`);
  }
  return res.json();
}

// ── Launcher (port 8081) — process-level start/stop ───────────────────────────

export interface LauncherStatus {
  running: boolean;
  pid: number | null;
  exit_code: number | null;
  timestamp: number;
}

export function useLauncherStatus(): { data: LauncherStatus | null; error: string | null; loading: boolean } {
  const [data, setData] = useState<LauncherStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(`${LAUNCHER_URL}/status`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "fetch error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchData]);

  return { data, error, loading };
}

export async function startBotProcess(): Promise<{ ok: boolean; pid?: number; reason?: string }> {
  const res = await fetch(`${LAUNCHER_URL}/start`, { method: "POST" });
  if (!res.ok) throw new Error(`Launcher start failed: ${res.status}`);
  return res.json();
}

export async function stopBotProcess(): Promise<{ ok: boolean; exit_code?: number; reason?: string }> {
  const res = await fetch(`${LAUNCHER_URL}/stop`, { method: "POST" });
  if (!res.ok) throw new Error(`Launcher stop failed: ${res.status}`);
  return res.json();
}

export const useLogs = (
  limit = 200,
  level = "ALL",
  module?: string,
  search?: string,
) => {
  const params = new URLSearchParams({ limit: String(limit), level });
  if (module) params.set("module", module);
  if (search) params.set("search", search);
  return usePolling<{ logs: LogEntry[]; total: number; modules: string[] }>(
    `/logs?${params}`,
    2_000,   // poll every 2s for near-realtime feel
  );
};

export const useErrorLogs = (
  limit = 500,
  module?: string,
  search?: string,
) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (module) params.set("module", module);
  if (search) params.set("search", search);
  return usePolling<{ logs: LogEntry[]; total: number; modules: string[] }>(
    `/logs/errors?${params}`,
    10_000,  // poll every 10s — warnings/errors are low-frequency
  );
};

// ── Opening Neutral (Strategy 5) ──────────────────────────────────────────────

export interface OpeningNeutralPair {
  pair_id: string;
  market_id: string;
  market_title: string;
  yes_entry: number | null;
  no_entry: number | null;
  yes_closed: boolean | null;
  no_closed: boolean | null;
}

export interface OpeningNeutralClosedPair {
  pair_id: string;
  market_id: string;
  market_title: string;
  yes_entry: number | null;
  no_entry: number | null;
  closed_at: string;
}

export interface OpeningNeutralSignal {
  ts: string;
  market_id: string;
  market_title: string;
  market_type: string;
  yes_ask: number | null;
  no_ask: number | null;
  combined: number | null;
  threshold: number;
  tte_secs: number;
  elapsed_secs: number;
  result: "entry_attempt" | "too_expensive" | string;
}

/** Live snapshot of a market being tracked for entry. Updated on every get_status() call. */
export interface TrackedMarket {
  market_id: string;
  market_title: string;
  market_type: string;
  yes_ask: number | null;
  no_ask: number | null;
  combined: number | null;
  tte_secs: number | null;
  elapsed_secs: number | null;
  /** True only when a confirmed fill/pair exists for this market (source of truth). */
  entered: boolean;
  /** True while an entry order is in-flight (placed but not yet confirmed). */
  entering: boolean;
}

export interface OpeningNeutralStatus {
  enabled: boolean;
  dry_run: boolean;
  active_pairs: number;
  pairs: OpeningNeutralPair[];
  closed_pairs: OpeningNeutralClosedPair[];
  recent_signals: OpeningNeutralSignal[];
  /** Live per-market tracking state: current prices, TTE, entry status. Updated every poll. */
  tracked_markets: TrackedMarket[];
  /** Config value: entry window in seconds (used to render window progress). */
  entry_window_secs?: number;
  scanner_running: boolean;
  timestamp: number;
}

export const useOpeningNeutralStatus = () =>
  usePolling<OpeningNeutralStatus>("/opening_neutral/status", 1_000);
