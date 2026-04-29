"""
risk.py — Position sizing, exposure tracking, P&L, and hard stop logic.

All state is in-memory (fast) and mirrored to data/trades.csv on every update.
"""
from __future__ import annotations

import asyncio
import csv
import dataclasses
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

# ── Data directory ─────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
TRADES_CSV = DATA_DIR / "trades.csv"
OPEN_POSITIONS_JSON = DATA_DIR / "open_positions.json"
PAPER_HEDGE_FILLS_JSON = DATA_DIR / "paper_hedge_fills.json"
HEDGE_ORDERS_JSON = DATA_DIR / "hedge_orders.json"
TRADES_HEADER = [
    "timestamp", "entry_timestamp", "market_id", "market_title", "market_type", "underlying", "side", "size", "price",
    "fees_paid", "rebates_earned", "hl_hedge_size", "hl_entry_price",
    "strategy", "spread_id", "pnl",
    # Signal context — populated for mispricing strategy; 0.0 for maker
    "entry_deviation",  # |pm_price - N(d2)| at signal time
    "implied_prob",     # Deribit N(d2) value
    "deribit_iv",       # annualised IV used in model
    "tte_years",        # time-to-expiry at entry (years)
    "spot_price",       # underlying spot price at entry (Pyth oracle)
    "exit_spot_price",  # underlying spot price at exit (Pyth oracle; 0.0 if unrecorded)
    "strike",           # parsed target price from market title
    "kalshi_price",     # matched Kalshi YES price at signal time (0.0 = no match)
    "signal_source",    # "kalshi_confirmed" | "kalshi_only" | "nd2_only"
    "signal_score",     # quality score 0–100 at signal time
    "resolved_outcome", # WIN | LOSS | "" (empty = early exit / paper / unknown)
    # GTD hedge fields (momentum strategy only; empty for maker/mispricing)
    "hedge_order_id",       # PM CLOB order ID of the resting opposite-token bid
    "hedge_token_id",       # CLOB token_id of the opposite (hedged) token
    "hedge_price",          # limit price the hedge bid was placed at
    "hedge_size_usd",       # USD size of the hedge order
    "hedge_status",         # "filled_won" | "filled_lost" | "unfilled" | "cancelled" | "filled_exited" | "rejected_price" | "" (main rows)
    "spot_resolve_price",   # oracle spot at market resolution (hedge rows only; 0.0 otherwise)
    "hedge_size_filled",    # contracts actually filled (empty for non-hedge rows)
    "hedge_avg_fill_price", # VWAP of all fills (empty for non-hedge rows)
]


# ── Fee model ─────────────────────────────────────────────────────────────────

def min_edge_after_fees(p: float) -> float:
    """
    Minimum required edge (as a fraction) to be profitable at probability p.

    Accounts for:
     - PM taker fee (only applicable on fee-enabled markets; worst-case assumption)
     - HL taker fee (fixed tier-0 rate)
     - Basis-risk / slippage buffer

    Args:
        p: Binary probability (0–1) of the PM market.

    Returns:
        Minimum fraction of contract notional that the edge must exceed.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be strictly between 0 and 1, got {p}")
    pm_taker_fee = config.PM_FEE_COEFF * p * (1.0 - p)
    return pm_taker_fee + config.HL_TAKER_FEE + config.EDGE_BUFFER


# ── HedgeOrder lifecycle ──────────────────────────────────────────────────────

class HedgeStatus:
    """FIX-protocol style order status constants for GTD hedge orders."""
    OPEN               = "open"
    PARTIALLY_FILLED   = "partially_filled"
    FILLED             = "filled"
    CANCELLED          = "cancelled"
    CANCELLED_PARTIAL  = "cancelled_partial"   # cancel confirmed after some fills
    EXPIRED_UNFILLED   = "expired_unfilled"    # GTD/market expiry with zero fill
    EXPIRED_PARTIAL    = "expired_partial"     # GTD/market expiry with partial fill
    FILLED_EXITED      = "filled_exited"       # main position exited; hedge settled

    # Terminal states — no further fill updates expected.
    TERMINAL: frozenset = frozenset({
        "filled", "cancelled", "cancelled_partial",
        "expired_unfilled", "expired_partial", "filled_exited",
    })


@dataclasses.dataclass
class HedgeFill:
    """A single matched fill event for a GTD hedge order."""
    fill_id:   str    # unique id (uuid or WS sequence)
    price:     float  # fill price
    size:      float  # contracts filled in this event
    timestamp: str    # ISO UTC
    source:    str    # "ws" | "clob_rest" | "reconciliation" | "paper"


@dataclasses.dataclass
class HedgeOrder:
    """First-class entity tracking the full lifecycle of a GTD hedge order."""
    # ── Identity ──────────────────────────────────────────────────────────────
    order_id:       str
    market_id:      str
    token_id:       str
    underlying:     str
    market_type:    str
    market_title:   str
    placed_at:      str   # ISO UTC

    # ── Order params ─────────────────────────────────────────────────────────
    order_price:    float
    order_size:     float
    order_size_usd: float

    # ── Live state ────────────────────────────────────────────────────────────
    status:         str   = dataclasses.field(default_factory=lambda: HedgeStatus.OPEN)
    size_filled:    float = 0.0   # cumulative contracts filled (REPLACE, not ADD)
    size_remaining: float = 0.0   # set by REST reconciliation
    avg_fill_price: float = 0.0   # VWAP across all fills
    fills:          list  = dataclasses.field(default_factory=list)  # list[HedgeFill] (as dicts)

    # ── Parent position reference ──────────────────────────────────────────────
    # Stored so RiskEngine can go from order_id → Position in O(1).
    # Empty string for orders loaded from old hedge_orders.json (pre-migration).
    parent_side:    str   = ""   # side of the parent Position (YES | NO | UP | DOWN)

    # ── Parent position reference ──────────────────────────────────────────────
    # Stored so RiskEngine can go from order_id → Position in O(1).
    # Empty string for orders loaded from old hedge_orders.json (pre-migration).
    parent_side:    str   = ""   # side of the parent Position (YES | NO | UP | DOWN)

    # ── Deferred cancel state ─────────────────────────────────────────────────
    pending_cancel_threshold:  float = 0.0   # HL price that triggers cancel
    pending_cancel_side:       str   = ""    # "long" | "short"
    pending_cancel_strike:     float = 0.0   # market strike
    pending_cancel_entry_spot: float = 0.0   # spot at position entry

    # ── Gap-closing reprice state ─────────────────────────────────────────────
    # price_cap: max bid price the hedge may be repriced to without eroding profit
    # below MOMENTUM_HEDGE_MIN_RETAIN_USD.  Set at placement; copied on each reprice.
    price_cap:          float          = 0.0
    # projected_pnl_usd: expected win PnL of the main position at entry
    # (entry_size × (1 − entry_price)).  Hard ceiling: reprice notional must
    # never exceed this value.  Set at registration; copied on each reprice.
    projected_pnl_usd:  float          = 0.0
    # initial_notional_usd: the USD budget committed at first placement
    # (order_price × order_size at registration).  Never mutated by replace_hedge_order.
    # Used by the reprice loop to cap notional at each step: contracts ≤ initial_notional / new_bid.
    # This scales correctly with position size — larger positions get proportionally
    # more insurance budget; smaller positions are capped at their initial spend.
    initial_notional_usd: float        = 0.0
    # natural_contracts: position-matched contract count (entry_size × HEDGE_CONTRACTS_PCT)
    # BEFORE any $1 PM-minimum floor is applied.  The floor may inflate order_size to
    # 1.0/hedge_price (e.g. 50 at 2¢) when the natural size costs < $1.  The monitor
    # reprice loop must use this field — not order_size — to recalculate sizing at each
    # step:  max(natural_contracts, 1.0/new_price).  This keeps spend at exactly $1
    # when natural coverage is cheap, but never wastes money on inflated carry-forward
    # counts from the original placement price.  0.0 = unknown (pre-migration hedge).
    natural_contracts:  float          = 0.0
    # last_clob_ask: best_ask from the previous monitor sweep, used to detect
    # whether the seller is coming toward us (ask falling → step up bid by $0.01).
    # Persisted so bot restarts don't lose the reference point.
    last_clob_ask:      Optional[float] = None

    # ── Resolution ────────────────────────────────────────────────────────────
    settled_price:      float = 0.0  # 1.0 WIN / 0.0 LOSS
    resolved_at:        str   = ""
    spot_at_resolution: float = 0.0
    net_pnl:            float = 0.0


# ── Position ──────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Position:
    market_id: str
    market_type: str          # bucket_15m | bucket_1h | bucket_daily | milestone
    underlying: str           # BTC | ETH | SOL
    side: str                 # YES | NO  (standard markets)  or  UP | DOWN  (Up-or-Down bucket markets)
    size: float               # USDC notional
    entry_price: float        # PM limit price
    strategy: str             # maker | mispricing
    token_id: str = ""        # CLOB token_id (asset) for this side; cached so wallet cross-reference works even after market is pruned from snapshot
    opened_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc))

    # HL hedge fields (populated when hedge is placed)
    hl_hedge_size: float = 0.0
    hl_entry_price: float = 0.0
    hl_side: str = ""         # LONG | SHORT

    # Fee tracking
    pm_fees_paid: float = 0.0
    pm_rebates_earned: float = 0.0

    # Capital deployed to open this position (actual USDC cost, not face value).
    # Both YES and NO: entry_price × size.
    entry_cost_usd: float = 0.0

    # P&L
    realized_pnl: float = 0.0
    is_closed: bool = False
    closed_at: Optional[datetime] = None

    # Signal context (mispricing strategy — 0.0/"nd2_only" for maker)
    entry_deviation: float = 0.0  # |pm_price - N(d2)| at signal time
    implied_prob: float = 0.0     # Deribit N(d2) value
    deribit_iv: float = 0.0       # annualised IV
    tte_years: float = 0.0        # time-to-expiry at entry
    spot_price: float = 0.0       # underlying spot at entry
    strike: float = 0.0           # parsed target price
    kalshi_price: float = 0.0     # matched Kalshi YES price (0.0 = no Kalshi match)
    signal_source: str = "nd2_only"  # "kalshi_confirmed" | "kalshi_only" | "nd2_only"
    signal_score: float = 0.0        # quality score 0–100 at signal time
    market_title: str = ""           # human-readable question label for display

    # CLOB order tracking — links filled contracts back to the originating limit order.
    # order_id: the PM order ID of the order that produced this position's fills.
    # Updated when a reprice fires a new order so logs can follow order lifecycle.
    order_id: str = ""

    # GTD hedge fields (populated when momentum hedge is placed)
    hedge_order_id: str = ""
    hedge_token_id: str = ""
    hedge_price: float = 0.0
    hedge_size_usd: float = 0.0
    # Hedge failure fields (populated when hedge placement was attempted but rejected)
    hedge_fail_reason: str = ""     # e.g. "all_attempts_exhausted"
    hedge_opp_best_ask: float = 0.0 # best ask of opposite token at time of failure

    # WS fill detection — set by live_fill_handler when a GTD hedge fills mid-trade
    hedge_fill_detected: bool = False
    hedge_fill_size: float = 0.0
    hedge_fill_price: float = 0.0

    # Active TP resting limit order (Item 1): PM CLOB order ID of a pre-armed SELL
    # at the take-profit price.  Cancelled automatically on any non-TP exit.
    tp_order_id: str = ""

    # Probability-based SL threshold (Item 7): CLOB token price below which the
    # prob-based SL fires.  Set to entry_price * (1 - MOMENTUM_PROB_SL_PCT) when
    # the position is opened.  0.0 = disabled (non-momentum positions).
    prob_sl_threshold: float = 0.0

    # Range market bounds (populated for strategy="range" positions only).
    # range_lo / range_hi are the actual lower and upper price boundaries from the
    # market title (e.g. "between $72,000 and $74,000").  Both default to 0.0 for
    # non-range positions.  The SL in should_exit() uses these to determine whether
    # the spot has moved outside the range rather than relying on pos.strike alone
    # (pos.strike stores the range midpoint, so spot between [lo, strike) would
    # falsely look like a negative delta without the full range bounds).
    range_lo: float = 0.0
    range_hi: float = 0.0

    # Spread pair tracking: when this position is one leg of a calendar spread,
    # both legs share the same spread_id (uuid4().hex).  None for single-leg positions.
    spread_id: Optional[str] = None

    # Opening neutral pair tracking: both YES and NO legs share this ID.
    # Cleared on the winner when it converts to strategy='momentum'.
    neutral_pair_id: str = ""

    @property
    def pm_delta_notional(self) -> float:
        """Signed notional exposure: positive = net long YES/UP (first token)."""
        return self.size if self.side in ("YES", "UP") else -self.size


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO UTC timestamp string to a timezone-aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── Risk Engine ───────────────────────────────────────────────────────────────

class RiskEngine:
    """
    Thread-safe risk engine.  All public methods acquire a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._positions: dict[str, Position] = {}
        self._realized_pnl: float = 0.0
        self._daily_start_pnl: float = 0.0   # Reset at midnight UTC
        self._hard_stop_triggered: bool = False
        # Tracks coin-level hedge notional placed by MakerStrategy._rebalance_hedge.
        # Separate from per-position hl_hedge_size because maker hedges span many markets.
        self._coin_hedge_notionals: dict[str, float] = {}
        # Tracks deployed-but-not-yet-filled orders so the concurrent position cap
        # is enforced at ORDER PLACEMENT time, not at fill time.
        # key: market_id  value: (strategy, underlying)
        self._deployed_slots: dict[str, tuple[str, str]] = {}
        # Persisted token_id → strategy mapping so position restores after restart
        # assign the correct strategy regardless of which strategies are enabled.
        self._token_strategy: dict[str, str] = self._load_token_strategy()
        # Lock protecting CSV file writes (used by _write_csv_row in thread pool).
        self._csv_write_lock = threading.Lock()
        # Paper-mode GTD hedge fills simulated by FillSimulator._sweep_hedges().
        # keyed by hedge_token_id; value = {"fill_price": float, "fill_size": float, "ts": str}
        # Persisted to PAPER_HEDGE_FILLS_JSON so fills survive bot restarts.
        self._paper_hedge_fills: dict[str, dict] = self._load_paper_hedge_fills()
        # First-class HedgeOrder entities keyed by order_id.
        # Persisted to HEDGE_ORDERS_JSON on every state change.
        self._hedge_orders: dict[str, HedgeOrder] = self._load_hedge_orders()
        self._ensure_csv()

    # ── CSV ────────────────────────────────────────────────────────────────────

    def _ensure_csv(self) -> None:
        if not TRADES_CSV.exists():
            with TRADES_CSV.open("w", newline="") as f:
                csv.writer(f).writerow(TRADES_HEADER)
            return
        # If the file exists but the header is stale (schema changed), either:
        # 1. Migrate in-place (additive change: old header is a prefix of new header).
        # 2. Back up and start fresh (column removed/reordered — incompatible change).
        with TRADES_CSV.open("r", newline="") as f:
            reader = csv.reader(f)
            try:
                existing_header = next(reader)
            except StopIteration:
                existing_header = []
        if existing_header == TRADES_HEADER:
            return  # nothing to do
        n = len(existing_header)
        if existing_header == TRADES_HEADER[:n]:
            # Additive schema change: append new empty columns to every existing row.
            new_cols = TRADES_HEADER[n:]
            lines = TRADES_CSV.read_bytes().splitlines(keepends=True)
            out = []
            for i, line in enumerate(lines):
                stripped = line.rstrip(b"\r\n")
                suffix = b"," + b",".join(b"" for _ in new_cols)
                if i == 0:
                    # Update header
                    suffix = b"," + b",".join(c.encode() for c in new_cols)
                out.append(stripped + suffix + b"\n")
            TRADES_CSV.write_bytes(b"".join(out))
            log.info(
                "trades.csv schema migrated (additive)",
                new_columns=new_cols,
            )
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = TRADES_CSV.with_name(f"trades_{ts}.csv.bak")
            TRADES_CSV.rename(backup)
            log.info("trades.csv schema changed — backed up old file", backup=str(backup))
            with TRADES_CSV.open("w", newline="") as f:
                csv.writer(f).writerow(TRADES_HEADER)

    def _append_csv(self, row: dict) -> None:
        """Schedule a CSV row append.  Off-loads to a thread-pool worker when the
        asyncio event loop is running so the hot path (event loop thread) is not
        blocked by file I/O.  Falls back to a synchronous write otherwise (startup,
        unit tests).
        """
        try:
            loop = asyncio.get_running_loop()
            # Fire-and-forget: the row is a plain dict, no shared mutable state.
            loop.run_in_executor(None, self._write_csv_row, row)
        except RuntimeError:
            # No running event loop.
            self._write_csv_row(row)

    def _write_csv_row(self, row: dict) -> None:
        """Write a single row to trades.csv.  Thread-safe via _csv_write_lock."""
        with self._csv_write_lock:
            with TRADES_CSV.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=TRADES_HEADER).writerow(row)

    def patch_trade_outcome(
        self,
        market_id: str,
        resolved_yes_price: float,
        *,
        force: bool = False,
    ) -> int:
        """Correct resolved_outcome (and optionally pnl) for trades.csv records.

        For each record with a matching market_id, derives the correct outcome from
        resolved_yes_price + the record's side:
            YES / UP   → WIN if resolved_yes_price == 1.0
            NO  / DOWN → WIN if resolved_yes_price == 0.0

        force=False (default, SL/taker exits):
            Only patches records where resolved_outcome is blank / "nan" /  "None".
            pnl is NOT changed — it already reflects the actual CLOB fill price.
            resolved_outcome is set for informational purposes (did the market
            ultimately resolve in the direction we bet on?).

        force=True (RESOLVED exits with wrong outcome):
            Also corrects records where the stored outcome contradicts
            resolved_yes_price (e.g., bot recorded WIN but PM settled as LOSS).
            exit_price is reset to the settlement value (1.0 or 0.0) and pnl is
            recomputed as (exit_price − entry_price) × size − fees + rebates.

        Returns the number of records changed.
        """
        _blank = {"", "nan", "None", "none"}
        _yes_sides = {"YES", "UP", "BUY_YES"}

        with self._csv_write_lock:
            if not TRADES_CSV.exists():
                return 0
            try:
                with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                # Guard against headerless CSVs: if the first row's keys don't
                # include "timestamp" the CSV was written without a header row
                # (DictReader misused the first data row as fieldnames).
                # Re-read with explicit fieldnames from TRADES_HEADER.
                if rows and "timestamp" not in rows[0]:
                    log.warning("patch_trade_outcome: headerless trades.csv — re-reading with explicit fieldnames")
                    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f, fieldnames=TRADES_HEADER))
            except Exception as exc:
                log.error("patch_trade_outcome: read failed", exc=str(exc))
                return 0

            patched = 0
            for row in rows:
                if row.get("market_id") != market_id:
                    continue
                # momentum_hedge rows have their own P&L semantics (settled_price of
                # the hedge token, not the main position token).  patch_trade_outcome
                # only knows about main-position YES/NO direction and would compute
                # the wrong settlement value for the hedge side.  Always skip them.
                if row.get("strategy") == "momentum_hedge":
                    continue
                side = row.get("side", "")
                is_yes_side = side in _yes_sides
                # Settlement value of the token held by this record (0.0 or 1.0)
                settlement = resolved_yes_price if is_yes_side else (1.0 - resolved_yes_price)
                correct_outcome = "WIN" if settlement >= 0.5 else "LOSS"
                stored = (row.get("resolved_outcome") or "").strip()

                if stored in _blank:
                    # SL/taker exit missing an outcome: fill in resolved_outcome.
                    # Also correct PnL if we recorded a loss on a WIN outcome — this
                    # indicates the GTC floor-fallback path reported the order's limit
                    # price (e.g. 0.50) rather than the actual PM settlement price.
                    # PM API is the source of truth: settlement = 1.0 (WIN) / 0.0 (LOSS).
                    row["resolved_outcome"] = correct_outcome
                    patched += 1
                    _pnl_corrected = False
                    if correct_outcome == "WIN":
                        try:
                            entry = float(row.get("price", 0) or 0)
                            size  = float(row.get("size",  0) or 0)
                            fees  = float(row.get("fees_paid",      0) or 0)
                            reb   = float(row.get("rebates_earned", 0) or 0)
                            old_pnl = float(row.get("pnl", 0) or 0)
                            if size > 0 and old_pnl < 0:
                                new_pnl = (settlement - entry) * size - fees + reb
                                row["pnl"] = str(round(new_pnl, 10))
                                _pnl_corrected = True
                                log.info(
                                    "patch_trade_outcome: pnl corrected (GTC floor WIN mismatch)",
                                    market_id=market_id[:20],
                                    side=side,
                                    new_outcome=correct_outcome,
                                    old_pnl=round(old_pnl, 4),
                                    new_pnl=round(new_pnl, 4),
                                    settlement=settlement,
                                )
                        except (ValueError, TypeError) as exc:
                            log.warning("patch_trade_outcome: pnl correction parse error", exc=str(exc))
                    if not _pnl_corrected:
                        log.info(
                            "patch_trade_outcome: resolved_outcome filled (SL exit)",
                            market_id=market_id[:20],
                            side=side,
                            new_outcome=correct_outcome,
                        )
                elif force and stored != correct_outcome:
                    # RESOLVED exit with wrong outcome: update outcome AND recompute pnl.
                    try:
                        entry   = float(row.get("price",          0) or 0)
                        size    = float(row.get("size",           0) or 0)
                        fees    = float(row.get("fees_paid",      0) or 0)
                        rebates = float(row.get("rebates_earned", 0) or 0)
                        new_pnl = (settlement - entry) * size - fees + rebates
                        row["resolved_outcome"] = correct_outcome
                        row["pnl"]              = str(round(new_pnl, 10))
                        patched += 1
                        log.info(
                            "patch_trade_outcome: outcome+pnl corrected (RESOLVED wrong)",
                            market_id=market_id[:20],
                            side=side,
                            old_outcome=stored,
                            new_outcome=correct_outcome,
                            new_pnl=round(new_pnl, 4),
                        )
                    except (ValueError, TypeError) as exc:
                        log.warning("patch_trade_outcome: numeric parse error", exc=str(exc))
                elif force and correct_outcome == "WIN":
                    # force=True: outcome already correct but PnL may be wrong due to
                    # GTC floor-fallback recording the limit price instead of settlement.
                    # Correct if PnL is negative on a WIN outcome.
                    try:
                        entry   = float(row.get("price",          0) or 0)
                        size    = float(row.get("size",           0) or 0)
                        fees    = float(row.get("fees_paid",      0) or 0)
                        rebates = float(row.get("rebates_earned", 0) or 0)
                        old_pnl = float(row.get("pnl",            0) or 0)
                        if size > 0 and old_pnl < 0:
                            new_pnl = (settlement - entry) * size - fees + rebates
                            row["pnl"] = str(round(new_pnl, 10))
                            patched += 1
                            log.info(
                                "patch_trade_outcome: pnl corrected (GTC floor WIN mismatch, force)",
                                market_id=market_id[:20],
                                side=side,
                                old_pnl=round(old_pnl, 4),
                                new_pnl=round(new_pnl, 4),
                                settlement=settlement,
                            )
                    except (ValueError, TypeError) as exc:
                        log.warning("patch_trade_outcome: pnl correction parse error", exc=str(exc))

            if patched == 0:
                return 0

            # Atomically rewrite the whole file via a .tmp sibling
            try:
                tmp = TRADES_CSV.with_suffix(".tmp")
                with tmp.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=TRADES_HEADER)
                    writer.writeheader()
                    writer.writerows(rows)
                tmp.replace(TRADES_CSV)
            except Exception as exc:
                log.error("patch_trade_outcome: rewrite failed", exc=str(exc))
                return 0

            return patched

    def patch_exit_spot_price(self, market_id: str, spot_price: float) -> int:
        """Update exit_spot_price for trades.csv records where it is 0.0.

        Called by _check_pending_resolutions once the Gamma API has the settlement
        close price available (typically a few minutes after market resolution).
        Only patches rows where exit_spot_price == 0.0 — never overwrites a valid
        price that was recorded at exit time.
        Returns the number of records changed.
        """
        if spot_price <= 0:
            return 0
        with self._csv_write_lock:
            if not TRADES_CSV.exists():
                return 0
            try:
                with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                if rows and "timestamp" not in rows[0]:
                    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f, fieldnames=TRADES_HEADER))
            except Exception as exc:
                log.error("patch_exit_spot_price: read failed", exc=str(exc))
                return 0
            patched = 0
            for row in rows:
                if row.get("market_id") != market_id:
                    continue
                try:
                    current = float(row.get("exit_spot_price", 0) or 0)
                except (ValueError, TypeError):
                    current = 0.0
                if current == 0.0:
                    row["exit_spot_price"] = str(round(spot_price, 4))
                    patched += 1
            if patched == 0:
                return 0
            try:
                tmp = TRADES_CSV.with_suffix(".tmp")
                with tmp.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=TRADES_HEADER)
                    writer.writeheader()
                    writer.writerows(rows)
                tmp.replace(TRADES_CSV)
            except Exception as exc:
                log.error("patch_exit_spot_price: rewrite failed", exc=str(exc))
                return 0
            log.info(
                "patch_exit_spot_price: settlement spot filled",
                market_id=market_id[:20],
                spot_price=round(spot_price, 4),
                records=patched,
            )
            return patched

    def patch_hedge_spot_price(self, market_id: str, spot_price: float) -> int:
        """Update spot_resolve_price for momentum_hedge rows in trades.csv where it is 0.0.

        Called by _check_pending_resolutions once the Gamma API has the settlement
        close price available (typically a few minutes after market resolution).
        Mirrors patch_exit_spot_price but targets the spot_resolve_price column on
        rows where strategy == 'momentum_hedge'.  Never overwrites a valid price
        that was recorded at the time the hedge was settled.
        Returns the number of records changed.
        """
        if spot_price <= 0:
            return 0
        with self._csv_write_lock:
            if not TRADES_CSV.exists():
                return 0
            try:
                with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                if rows and "timestamp" not in rows[0]:
                    with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f, fieldnames=TRADES_HEADER))
            except Exception as exc:
                log.error("patch_hedge_spot_price: read failed", exc=str(exc))
                return 0
            patched = 0
            for row in rows:
                if row.get("market_id") != market_id:
                    continue
                if row.get("strategy") != "momentum_hedge":
                    continue
                try:
                    current = float(row.get("spot_resolve_price", 0) or 0)
                except (ValueError, TypeError):
                    current = 0.0
                if current == 0.0:
                    row["spot_resolve_price"] = str(round(spot_price, 4))
                    patched += 1
            if patched == 0:
                return 0
            try:
                tmp = TRADES_CSV.with_suffix(".tmp")
                with tmp.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=TRADES_HEADER)
                    writer.writeheader()
                    writer.writerows(rows)
                tmp.replace(TRADES_CSV)
            except Exception as exc:
                log.error("patch_hedge_spot_price: rewrite failed", exc=str(exc))
                return 0
            log.info(
                "patch_hedge_spot_price: settlement spot filled for hedge row",
                market_id=market_id[:20],
                spot_price=round(spot_price, 4),
                records=patched,
            )
            return patched

    def _load_token_strategy(self) -> dict[str, str]:
        """Load persisted token_id → strategy map from disk."""
        if OPEN_POSITIONS_JSON.exists():
            try:
                return json.loads(OPEN_POSITIONS_JSON.read_text())
            except Exception:
                pass
        return {}

    def _save_token_strategy(self) -> None:
        """Persist token_id → strategy map. Must be called under self._lock."""
        OPEN_POSITIONS_JSON.write_text(json.dumps(self._token_strategy))

    def get_token_strategy(self, token_id: str) -> str | None:
        """Return the strategy that opened the position for this token_id, or None."""
        return self._token_strategy.get(token_id)

    # ── Checks ─────────────────────────────────────────────────────────────────

    def can_open(
        self,
        market_id: str,
        size_usd: float,
        strategy: str = "maker",
        underlying: str = "",
    ) -> tuple[bool, str]:
        """
        Returns (True, "") if a new position of this size can be opened,
        or (False, reason) if a risk limit would be breached.
        """
        with self._lock:
            if self._hard_stop_triggered:
                return False, "hard stop active"

            open_positions = [p for p in self._positions.values() if not p.is_closed]

            # CLOB semantics: placing an order reserves the position slot.  Subsequent
            # fill slices on the same market are NOT new positions — they merge into the
            # existing one.  Count-based caps must only fire for genuinely new markets.
            market_already_open = (
                any(p.market_id == market_id for p in open_positions)
                or market_id in self._deployed_slots
            )

            if not market_already_open:
                # ── Per-strategy concurrent position cap (Flaw §5) ────────────
                if strategy == "maker":
                    strategy_cap = config.MAX_CONCURRENT_MAKER_POSITIONS
                else:
                    strategy_cap = config.MAX_CONCURRENT_MISPRICING_POSITIONS
                strategy_open = [p for p in open_positions if p.strategy == strategy]
                if len(strategy_open) >= strategy_cap:
                    return False, (
                        f"max concurrent {strategy} positions ({strategy_cap}) reached"
                    )

                # ── Per-underlying maker cap (Area D) ─────────────────────────
                # Prevents correlated blow-ups when many markets for the same coin
                # fill at once during a vol spike.
                if strategy == "maker" and underlying:
                    underlying_open = [
                        p for p in strategy_open if p.underlying == underlying
                    ]
                    per_coin_cap = config.MAX_MAKER_POSITIONS_PER_UNDERLYING
                    if len(underlying_open) >= per_coin_cap:
                        return False, (
                            f"per-underlying maker cap ({per_coin_cap}) reached for {underlying}"
                        )

                # ── Global backstop cap ───────────────────────────────────────
                if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
                    return False, (
                        f"global concurrent cap ({config.MAX_CONCURRENT_POSITIONS}) reached"
                    )

            # Exposure is measured in USD (entry_cost_usd = price × contracts).
            # Using p.size (contracts) here would compare different units against the
            # USD limits — that is the class of bug that created sub-$10 positions.
            per_market_exposure = sum(
                p.entry_cost_usd for p in open_positions if p.market_id == market_id
            )
            if per_market_exposure + size_usd > config.MAX_PM_EXPOSURE_PER_MARKET:
                return False, (
                    f"per-market limit ${config.MAX_PM_EXPOSURE_PER_MARKET:.0f} "
                    f"would be breached (current ${per_market_exposure:.2f})"
                )

            total_exposure = sum(p.entry_cost_usd for p in open_positions)
            if total_exposure + size_usd > config.MAX_TOTAL_PM_EXPOSURE:
                return False, (
                    f"total PM exposure limit ${config.MAX_TOTAL_PM_EXPOSURE:.0f} "
                    f"would be breached (current ${total_exposure:.2f})"
                )

            return True, ""

    def can_hedge(self, hedge_notional_usd: float) -> tuple[bool, str]:
        """Check if a new HL hedge of this notional stays within limits."""
        with self._lock:
            # Existing per-position hedges
            position_hedge_notional = sum(
                abs(p.hl_hedge_size * p.hl_entry_price)
                for p in self._positions.values()
                if not p.is_closed and p.hl_hedge_size != 0.0
            )
            # Coin-level hedges placed by MakerStrategy (tracked separately)
            coin_hedge_notional = sum(self._coin_hedge_notionals.values())
            current_hl = position_hedge_notional + coin_hedge_notional
            if current_hl + hedge_notional_usd > config.MAX_HL_NOTIONAL:
                return False, (
                    f"HL notional limit ${config.MAX_HL_NOTIONAL:.0f} "
                    f"would be breached (current ${current_hl:.2f})"
                )
            return True, ""

    def reserve_slot(
        self,
        market_id: str,
        strategy: str,
        underlying: str = "",
    ) -> bool:
        """
        Reserve a position slot when a maker order is placed (before any fill).

        Enforces all concurrent-position caps (strategy cap, per-underlying cap,
        global backstop) at ORDER PLACEMENT time so the fill simulator cannot
        blow past the limits by filling many simultaneous resting orders.

        Returns True if the slot was accepted (deploy should proceed).
        Returns False if a cap would be exceeded (deploy should be skipped).
        Idempotent: if this market_id already has a reservation or an open
        position, the slot entry is refreshed and True is returned (reprices).
        """
        with self._lock:
            open_positions = [p for p in self._positions.values() if not p.is_closed]
            market_already_open = any(p.market_id == market_id for p in open_positions)
            market_already_reserved = market_id in self._deployed_slots

            if not market_already_open and not market_already_reserved:
                # ── Per-strategy cap ──────────────────────────────────────────
                strategy_cap = (
                    config.MAX_CONCURRENT_MAKER_POSITIONS if strategy == "maker"
                    else config.MAX_CONCURRENT_MISPRICING_POSITIONS
                )
                strategy_deployed = sum(
                    1 for v in self._deployed_slots.values() if v[0] == strategy
                )
                strategy_open = sum(1 for p in open_positions if p.strategy == strategy)
                if strategy_open + strategy_deployed >= strategy_cap:
                    log.debug(
                        "reserve_slot: strategy cap reached",
                        strategy=strategy, cap=strategy_cap,
                        open=strategy_open, deployed=strategy_deployed,
                    )
                    return False

                # ── Per-underlying cap (maker only) ───────────────────────────
                if strategy == "maker" and underlying:
                    per_coin_cap = config.MAX_MAKER_POSITIONS_PER_UNDERLYING
                    coin_deployed = sum(
                        1 for v in self._deployed_slots.values()
                        if v[0] == strategy and v[1] == underlying
                    )
                    coin_open = sum(
                        1 for p in open_positions
                        if p.strategy == strategy and p.underlying == underlying
                    )
                    if coin_open + coin_deployed >= per_coin_cap:
                        log.debug(
                            "reserve_slot: per-underlying cap reached",
                            underlying=underlying, cap=per_coin_cap,
                            open=coin_open, deployed=coin_deployed,
                        )
                        return False

                # ── Global backstop cap ───────────────────────────────────────
                if len(open_positions) + len(self._deployed_slots) >= config.MAX_CONCURRENT_POSITIONS:
                    log.debug(
                        "reserve_slot: global cap reached",
                        cap=config.MAX_CONCURRENT_POSITIONS,
                    )
                    return False

            self._deployed_slots[market_id] = (strategy, underlying)
            return True

    def free_slot(self, market_id: str) -> None:
        """Release a deployment reservation (order filled or cancelled)."""
        with self._lock:
            self._deployed_slots.pop(market_id, None)

    # ── Position lifecycle ──────────────────────────────────────────────────────

    @staticmethod
    def _pos_key(market_id: str, side: str) -> str:
        """Composite position key — one slot per (market, side) pair."""
        return f"{market_id}:{side}"

    def open_position(self, position: Position) -> None:
        with self._lock:
            key = self._pos_key(position.market_id, position.side)
            existing = self._positions.get(key)
            if existing and not existing.is_closed:
                # Merge fills into the same (market, side) position.
                existing.size += position.size
                existing.entry_cost_usd += position.entry_cost_usd
                # Update weighted-average entry price so close_position P&L uses the
                # blended cost across all fills (not just the first batch's price).
                # Both YES and NO: avg_price = total_cost / total_size
                if existing.size > 0:
                    existing.entry_price = existing.entry_cost_usd / existing.size
                # Update order_id when a reprice fires a new order.
                if position.order_id and position.order_id != existing.order_id:
                    existing.order_id = position.order_id
                log.info(
                    "Position merged",
                    market_id=position.market_id,
                    side=position.side,
                    new_size=existing.size,
                    order_id=position.order_id or "(none)",
                    strategy=position.strategy,
                )
            else:
                self._positions[key] = position
                log.info(
                    "Position opened",
                    market_id=position.market_id,
                    side=position.side,
                    size=position.size,
                    order_id=position.order_id or "(none)",
                    strategy=position.strategy,
                )
            # Keep token_id → strategy map current so restores after restart
            # assign the correct strategy even when running mixed strategies.
            if position.token_id:
                self._token_strategy[position.token_id] = position.strategy
                self._save_token_strategy()

    def update_gtd_hedge(
        self,
        market_id: str,
        side: str,
        hedge_order_id: str,
        hedge_token_id: str,
        hedge_price: float,
        hedge_size_usd: float,
    ) -> None:
        """Store GTD hedge details on the position after momentum hedge is placed."""
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos is None:
                log.warning("update_gtd_hedge: unknown position", market_id=market_id, side=side)
                return
            pos.hedge_order_id = hedge_order_id
            pos.hedge_token_id = hedge_token_id
            pos.hedge_price = hedge_price
            pos.hedge_size_usd = hedge_size_usd
            # Register hedge token in the token→strategy map so that if the hedge
            # fills and the bot restarts, the filled token is restored with the
            # correct "momentum_hedge" strategy (not "unknown").
            if hedge_token_id:
                self._token_strategy[hedge_token_id] = "momentum_hedge"
                self._save_token_strategy()
            # Mirror parent_side onto the HedgeOrder entity so the reverse lookup
            # (order_id → Position) can use the stored key directly (O(1)).
            ho = self._hedge_orders.get(hedge_order_id)
            if ho is not None and not ho.parent_side:
                ho.parent_side = side
                self._save_hedge_orders()

    def set_hedge_failed(
        self,
        market_id: str,
        side: str,
        reason: str,
        opp_best_ask: float,
        max_price: float = 0.0,
    ) -> None:
        """Record that a hedge placement was attempted but rejected (e.g. priced out).

        This causes close_position() to write hedge_status='rejected_price' in
        trades.csv so the webapp can surface the failure.
        - hedge_price is repurposed to store max_price (what the bot was willing to pay).
        - hedge_opp_best_ask stores the opposite-token best ask at failure time.
        """
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos is None:
                return
            pos.hedge_fail_reason = reason
            pos.hedge_opp_best_ask = opp_best_ask
            if max_price > 0:
                pos.hedge_price = max_price  # reuse field: max price the bot offered

    def get_position_by_hedge_token(self, hedge_token_id: str) -> Optional[Position]:
        """Return the position whose GTD hedge_token_id matches, or None."""
        with self._lock:
            for pos in self._positions.values():
                if pos.hedge_token_id == hedge_token_id:
                    return pos
        return None

    def get_position_for_hedge(self, hedge_order_id: str) -> Optional[Position]:
        """Return the open position whose GTD hedge_order_id matches.

        O(1) when the HedgeOrder entity carries a parent_side (set by
        update_gtd_hedge).  Falls back to an O(n) scan for legacy hedge orders
        that pre-date the parent_side field (loaded from old hedge_orders.json).
        """
        with self._lock:
            ho = self._hedge_orders.get(hedge_order_id)
            if ho is not None and ho.parent_side:
                key = self._pos_key(ho.market_id, ho.parent_side)
                pos = self._positions.get(key)
                if pos is not None and not pos.is_closed:
                    return pos
                # Hedge may have been placed, position may have been re-keyed — fall
                # through to scan so we never silently lose a fill.
            # Legacy fallback: full scan
            for pos in self._positions.values():
                if pos.hedge_order_id == hedge_order_id and not pos.is_closed:
                    return pos
        return None

    def get_position_by_hedge_order_id(self, hedge_order_id: str) -> Optional[Position]:
        """Deprecated alias for get_position_for_hedge(). Use that instead."""
        return self.get_position_for_hedge(hedge_order_id)

    # ── Market-aggregate P&L ──────────────────────────────────────────────────

    def market_pnl(self, market_id: str) -> dict:
        """Return a combined P&L snapshot for all positions and hedges in a market.

        The result is a plain dict (JSON-serializable) so callers — the webapp,
        api_server, monitor diagnostics — can consume it without extra imports.

        Keys:
            realized_pnl        float  — sum of closed position P&L for this market
            unrealised_pnl      float  — sum of open position P&L at entry price
                                         (caller should pass current prices if available)
            hedge_realized_pnl  float  — net_pnl from any finalized HedgeOrder
            total_pnl           float  — realized + unrealised + hedge_realized
            positions           list   — one dict per (market_id, side) position
            hedge               dict | None  — most recent non-terminal HedgeOrder summary

        Note: unrealised_pnl is computed at entry_price (cost basis), not current
        market price.  Use compute_unrealised_pnl(pos, current_price) from monitor.py
        for a live mark-to-market figure when a current price is available.
        """
        with self._lock:
            pos_list = [
                p for p in self._positions.values() if p.market_id == market_id
            ]

        realized = sum(p.realized_pnl for p in pos_list if p.is_closed)
        # Unrealised at cost basis (price has not moved since entry).
        unrealised = sum(
            (p.entry_price - p.entry_price) * p.size
            for p in pos_list if not p.is_closed
        )  # always 0 — kept explicit so callers know to override with live price

        # Hedge P&L: look for the most recent non-terminal HedgeOrder, then
        # fall back to the most recent terminal one (so closed markets still show).
        ho = self.get_hedge_order_by_market(market_id)
        if ho is None:
            # Market may be resolved — try any HedgeOrder for it (including terminal)
            with self._lock:
                candidates = [
                    h for h in self._hedge_orders.values() if h.market_id == market_id
                ]
            ho = max(candidates, key=lambda h: h.placed_at) if candidates else None

        hedge_realized = ho.net_pnl if ho is not None else 0.0
        hedge_summary: Optional[dict] = None
        if ho is not None:
            hedge_summary = {
                "order_id":      ho.order_id,
                "status":        ho.status,
                "order_price":   ho.order_price,
                "size_filled":   ho.size_filled,
                "avg_fill_price": ho.avg_fill_price,
                "net_pnl":       ho.net_pnl,
                "parent_side":   ho.parent_side,
            }

        pos_summaries = [
            {
                "side":         p.side,
                "size":         p.size,
                "entry_price":  p.entry_price,
                "realized_pnl": p.realized_pnl,
                "is_closed":    p.is_closed,
                "strategy":     p.strategy,
            }
            for p in pos_list
        ]

        total = round(realized + unrealised + hedge_realized, 6)
        return {
            "market_id":          market_id,
            "realized_pnl":       round(realized, 6),
            "unrealised_pnl":     round(unrealised, 6),
            "hedge_realized_pnl": round(hedge_realized, 6),
            "total_pnl":          total,
            "positions":          pos_summaries,
            "hedge":              hedge_summary,
        }

    def record_paper_hedge_fill_sim(
        self, hedge_token_id: str, fill_price: float, fill_size: float
    ) -> None:
        """Record a paper-mode GTD hedge fill simulated by FillSimulator._sweep_hedges().

        Called when the live CLOB shows the opposite token touching the hedge bid.
        The actual settled outcome (filled_won/filled_lost) is determined later at
        market resolution in monitor._exit_position(), where the main position's
        exit_price tells us which side won.
        """
        with self._lock:
            self._paper_hedge_fills[hedge_token_id] = {
                "fill_price": fill_price,
                "fill_size": fill_size,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self._save_paper_hedge_fills()

    def get_paper_hedge_fill(self, hedge_token_id: str) -> Optional[dict]:
        """Return simulated fill info for hedge_token_id, or None if not yet filled."""
        with self._lock:
            return self._paper_hedge_fills.get(hedge_token_id)

    def _load_paper_hedge_fills(self) -> dict[str, dict]:
        """Load persisted paper-mode hedge fills from disk."""
        if PAPER_HEDGE_FILLS_JSON.exists():
            try:
                return json.loads(PAPER_HEDGE_FILLS_JSON.read_text())
            except Exception:
                pass
        return {}

    def _save_paper_hedge_fills(self) -> None:
        """Write paper-mode hedge fills to disk. Must be called under self._lock."""
        try:
            PAPER_HEDGE_FILLS_JSON.write_text(json.dumps(self._paper_hedge_fills))
        except Exception as exc:
            log.warning("Failed to persist paper hedge fills", exc=str(exc))

    # ── HedgeOrder persistence ────────────────────────────────────────────────

    def _load_hedge_orders(self) -> dict[str, HedgeOrder]:
        """Load persisted HedgeOrder entities from disk."""
        if not HEDGE_ORDERS_JSON.exists():
            return {}
        try:
            raw = json.loads(HEDGE_ORDERS_JSON.read_text())
            result: dict[str, HedgeOrder] = {}
            for order_id, d in raw.items():
                fills = d.pop("fills", [])
                ho = HedgeOrder(**d)
                ho.fills = fills
                result[order_id] = ho
            self._prune_old_hedge_orders(result)
            return result
        except Exception as exc:
            log.warning("Failed to load hedge_orders.json", exc=str(exc))
            return {}

    def _save_hedge_orders(self) -> None:
        """Write hedge orders to disk. Must be called under self._lock."""
        try:
            HEDGE_ORDERS_JSON.write_text(
                json.dumps({k: dataclasses.asdict(v) for k, v in self._hedge_orders.items()})
            )
        except Exception as exc:
            log.warning("Failed to persist hedge_orders.json", exc=str(exc))

    def _prune_old_hedge_orders(self, orders: Optional[dict] = None) -> None:
        """Remove terminal HedgeOrders older than 7 days to keep file size bounded."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        target = orders if orders is not None else self._hedge_orders
        to_del = [
            oid for oid, ho in target.items()
            if ho.status in HedgeStatus.TERMINAL and ho.placed_at
            and _parse_iso(ho.placed_at) < cutoff
        ]
        for k in to_del:
            del target[k]

    # ── HedgeOrder CRUD ───────────────────────────────────────────────────────

    def register_hedge_order(
        self,
        order_id: str,
        market_id: str,
        token_id: str,
        underlying: str,
        market_type: str,
        market_title: str,
        order_price: float,
        order_size: float,
        order_size_usd: float,
        *,
        parent_side: str = "",
        price_cap: float = 0.0,
        projected_pnl_usd: float = 0.0,
        natural_contracts: float = 0.0,
    ) -> HedgeOrder:
        """Create and persist a new HedgeOrder in OPEN status."""
        with self._lock:
            ho = HedgeOrder(
                order_id=order_id,
                market_id=market_id,
                token_id=token_id,
                underlying=underlying,
                market_type=market_type,
                market_title=market_title,
                placed_at=datetime.now(timezone.utc).isoformat(),
                order_price=order_price,
                order_size=order_size,
                order_size_usd=order_size_usd,
                size_remaining=order_size,
                parent_side=parent_side,
                price_cap=price_cap,
                projected_pnl_usd=projected_pnl_usd,
                initial_notional_usd=order_size_usd,
                natural_contracts=natural_contracts,
            )
            self._hedge_orders[order_id] = ho
            self._save_hedge_orders()
            return ho

    def replace_hedge_order(
        self,
        old_order_id: str,
        new_order_id: str,
        new_price: float,
        new_order_size: float = 0.0,
    ) -> Optional["HedgeOrder"]:
        """Cancel old hedge order and register a replacement at new_price.

        Atomically:
          1. Marks old HedgeOrder as CANCELLED.
          2. Creates a new HedgeOrder copying all metadata (including price_cap).
          3. Updates the parent Position's hedge_order_id to the new order.
          4. Persists.

        new_order_size: the actual contracts placed in the reprice order.
        If 0.0, falls back to old_ho.order_size (backward compat with callers
        that don't compute the reprice size themselves).

        Returns the new HedgeOrder, or None if old_order_id is unknown.
        """
        with self._lock:
            old_ho = self._hedge_orders.get(old_order_id)
            if old_ho is None:
                log.warning("replace_hedge_order: unknown old order", order_id=old_order_id[:20])
                return None

            _actual_size = new_order_size if new_order_size > 0.0 else old_ho.order_size
            new_ho = HedgeOrder(
                order_id=new_order_id,
                market_id=old_ho.market_id,
                token_id=old_ho.token_id,
                underlying=old_ho.underlying,
                market_type=old_ho.market_type,
                market_title=old_ho.market_title,
                placed_at=datetime.now(timezone.utc).isoformat(),
                order_price=new_price,
                order_size=_actual_size,
                order_size_usd=round(_actual_size * new_price, 6),
                size_remaining=max(0.0, _actual_size - old_ho.size_filled),
                parent_side=old_ho.parent_side,
                price_cap=old_ho.price_cap,
                projected_pnl_usd=old_ho.projected_pnl_usd,
                initial_notional_usd=old_ho.initial_notional_usd,
                natural_contracts=old_ho.natural_contracts,
                pending_cancel_threshold=old_ho.pending_cancel_threshold,
                pending_cancel_side=old_ho.pending_cancel_side,
                pending_cancel_strike=old_ho.pending_cancel_strike,
                pending_cancel_entry_spot=old_ho.pending_cancel_entry_spot,
            )

            old_ho.status = HedgeStatus.CANCELLED

            self._hedge_orders[new_order_id] = new_ho

            # Update the parent Position so future monitor sweeps reference the new order.
            for pos in self._positions.values():
                if pos.hedge_order_id == old_order_id:
                    pos.hedge_order_id = new_order_id
                    pos.hedge_price = new_price
                    break

            self._save_hedge_orders()
            return new_ho

    def update_hedge_fill(
        self,
        order_id: str,
        fill_price: float,
        cumulative_size: float,
        source: str = "ws",
    ) -> Optional[HedgeOrder]:
        """Update a HedgeOrder with a new cumulative fill snapshot (REPLACE semantics).

        PM WS sends cumulative size_matched totals, not per-fill deltas.
        VWAP is computed incrementally: only the incremental delta is new.

        Returns the updated HedgeOrder or None if order_id is unknown.
        """
        with self._lock:
            ho = self._hedge_orders.get(order_id)
            if ho is None:
                log.debug("update_hedge_fill: unknown order_id", order_id=order_id[:20])
                return None
            if ho.status in HedgeStatus.TERMINAL:
                log.debug(
                    "update_hedge_fill: order already terminal",
                    order_id=order_id[:20], status=ho.status,
                )
                return ho

            old_size = ho.size_filled
            new_size = round(cumulative_size, 6)
            delta = round(new_size - old_size, 6)
            if delta <= 0:
                return ho  # no new fill — idempotent

            # VWAP: (old_vwap × old_size + delta × fill_price) / new_size
            if new_size > 0:
                ho.avg_fill_price = round(
                    (ho.avg_fill_price * old_size + delta * fill_price) / new_size, 6
                )
            ho.size_filled = new_size

            fill = HedgeFill(
                fill_id=f"{order_id[:8]}_{len(ho.fills)}",
                price=fill_price,
                size=delta,
                timestamp=datetime.now(timezone.utc).isoformat(),
                source=source,
            )
            ho.fills.append(dataclasses.asdict(fill))
            ho.status = (
                HedgeStatus.FILLED
                if new_size >= ho.order_size - 1e-6
                else HedgeStatus.PARTIALLY_FILLED
            )
            self._save_hedge_orders()
            log.info(
                "HedgeOrder fill updated",
                order_id=order_id[:20],
                delta=delta,
                cumulative=new_size,
                avg_fill=ho.avg_fill_price,
                status=ho.status,
            )
            return ho

    def finalize_hedge(
        self,
        order_id: str,
        *,
        settled_price: float,
        spot_at_resolution: float = 0.0,
        hedge_status: str = HedgeStatus.FILLED_EXITED,
    ) -> Optional[HedgeOrder]:
        """Mark a HedgeOrder as settled and write a momentum_hedge row to trades.csv.

        This is the preferred replacement for record_hedge_fill() in the new flow.

        Args:
            order_id:             PM CLOB order ID of the hedge.
            settled_price:        1.0 if hedge token won, 0.0 if lost.
            spot_at_resolution:   oracle spot at market resolution time.
            hedge_status:         terminal status constant (HedgeStatus.*).

        Returns the finalized HedgeOrder or None if unknown.
        """
        _csv_row: Optional[dict] = None
        with self._lock:
            ho = self._hedge_orders.get(order_id)
            if ho is None:
                log.warning("finalize_hedge: unknown order_id", order_id=order_id[:20])
                return None

            fill_price = ho.avg_fill_price if ho.avg_fill_price else ho.order_price
            fill_size = ho.size_filled
            pnl = round((settled_price - fill_price) * fill_size, 6)

            ho.status = hedge_status
            ho.settled_price = settled_price
            ho.resolved_at = datetime.now(timezone.utc).isoformat()
            ho.spot_at_resolution = spot_at_resolution
            ho.net_pnl = pnl
            self._realized_pnl += pnl
            self._save_hedge_orders()

            if ho.token_id in self._paper_hedge_fills:
                del self._paper_hedge_fills[ho.token_id]
                self._save_paper_hedge_fills()

            _csv_row = {
                "timestamp":            datetime.now(timezone.utc).isoformat(),
                "entry_timestamp":      "",
                "market_id":            ho.market_id,
                "market_title":         (ho.market_title or "")[:60],
                "market_type":          "momentum_hedge",
                "underlying":           ho.underlying,
                "side":                 "hedge",
                "size":                 round(fill_size, 6),
                "price":                round(fill_price, 4),
                "fees_paid":            0.0,
                "rebates_earned":       0.0,
                "hl_hedge_size":        0.0,
                "hl_entry_price":       0.0,
                "strategy":             "momentum_hedge",
                "spread_id":            "",
                "pnl":                  pnl,
                "entry_deviation":      0.0,
                "implied_prob":         0.0,
                "deribit_iv":           0.0,
                "tte_years":            0.0,
                "spot_price":           0.0,
                "exit_spot_price":      settled_price,
                "strike":               0.0,
                "kalshi_price":         0.0,
                "signal_source":        "gtd_hedge",
                "signal_score":         0.0,
                "resolved_outcome":     "WIN" if settled_price >= 0.5 else "LOSS",
                "hedge_order_id":       order_id,
                "hedge_token_id":       ho.token_id,
                "hedge_price":          round(ho.order_price, 4),
                "hedge_size_usd":       round(ho.order_size_usd, 6),
                "hedge_status":         hedge_status,
                "spot_resolve_price":   round(spot_at_resolution, 4),
                "hedge_size_filled":    round(fill_size, 6),
                "hedge_avg_fill_price": round(fill_price, 6),
            }
            log.info(
                "HedgeOrder finalized",
                order_id=order_id[:20],
                market_id=ho.market_id[:20],
                fill_size=fill_size,
                fill_price=fill_price,
                settled_price=settled_price,
                pnl=pnl,
                status=hedge_status,
            )
        self._append_csv(_csv_row)
        return ho

    def get_hedge_order(self, order_id: str) -> Optional[HedgeOrder]:
        """Return the HedgeOrder for order_id, or None."""
        with self._lock:
            return self._hedge_orders.get(order_id)

    def get_hedge_order_by_market(self, market_id: str) -> Optional[HedgeOrder]:
        """Return the most recent non-terminal HedgeOrder for market_id, or None."""
        with self._lock:
            match: Optional[HedgeOrder] = None
            for ho in self._hedge_orders.values():
                if ho.market_id == market_id and ho.status not in HedgeStatus.TERMINAL:
                    if match is None or ho.placed_at > match.placed_at:
                        match = ho
            return match

    def get_hedge_order_by_token_id(self, token_id: str) -> Optional[HedgeOrder]:
        """Return the HedgeOrder whose token_id matches, or None.

        Used as a fallback in the auto-redeem loop when the parent Position has
        been evicted from memory (e.g. after a bot restart).  The HedgeOrder
        entity persists in hedge_orders.json across restarts.
        """
        with self._lock:
            for ho in self._hedge_orders.values():
                if ho.token_id == token_id:
                    return ho
        return None

    def get_hedge_orders_with_pending_cancel(self) -> list:
        """Return all non-terminal HedgeOrders that have a pending cancel registered."""
        with self._lock:
            return [
                ho for ho in self._hedge_orders.values()
                if ho.pending_cancel_side and ho.status not in HedgeStatus.TERMINAL
            ]

    def get_open_hedge_orders(self) -> list:
        """Return all non-terminal HedgeOrders (open / partially_filled)."""
        with self._lock:
            return [
                ho for ho in self._hedge_orders.values()
                if ho.status not in HedgeStatus.TERMINAL
            ]

    def set_pending_cancel(
        self,
        order_id: str,
        threshold: float,
        side: str,
        strike: float,
        entry_spot: float,
    ) -> None:
        """Register a deferred cancel trigger on a HedgeOrder."""
        with self._lock:
            ho = self._hedge_orders.get(order_id)
            if ho is None:
                log.warning("set_pending_cancel: unknown order_id", order_id=order_id[:20])
                return
            ho.pending_cancel_threshold  = threshold
            ho.pending_cancel_side       = side
            ho.pending_cancel_strike     = strike
            ho.pending_cancel_entry_spot = entry_spot
            self._save_hedge_orders()

    def clear_pending_cancel(self, order_id: str) -> None:
        """Clear the deferred cancel trigger from a HedgeOrder."""
        with self._lock:
            ho = self._hedge_orders.get(order_id)
            if ho is None:
                return
            ho.pending_cancel_threshold  = 0.0
            ho.pending_cancel_side       = ""
            ho.pending_cancel_strike     = 0.0
            ho.pending_cancel_entry_spot = 0.0
            self._save_hedge_orders()

    def has_hedge_fill(self, market_id: str) -> bool:
        """Return True if a momentum_hedge record has already been FINALIZED for market_id.

        Guards against double-writes: returns True only when finalize_hedge() or
        record_hedge_fill() has committed the outcome (terminal status or
        filled_won/filled_lost).  A non-terminal order with size_filled>0 means
        "we have WS fill data but haven't written the outcome yet" — returning
        True there would prevent _record_pending_resolution_hedge from ever
        committing the result (the original bug that caused XRP hedge wins to
        go unrecorded).

        Deprecated: new code should use get_hedge_order_by_market() directly.
        """
        # Statuses that mean finalize_hedge() already committed the outcome row.
        _FINALIZED = frozenset({"filled_won", "filled_lost"}) | HedgeStatus.TERMINAL
        with self._lock:
            for ho in self._hedge_orders.values():
                if ho.market_id == market_id and ho.status in _FINALIZED:
                    return True
        # Fallback: scan trades.csv for legacy rows written before the HedgeOrder model
        try:
            if not TRADES_CSV.exists():
                return False
            with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, fieldnames=TRADES_HEADER)
                next(reader, None)  # skip header row
                for row in reader:
                    if (row.get("market_id") == market_id
                            and row.get("strategy") == "momentum_hedge"):
                        return True
        except Exception:
            pass
        return False

    def record_hedge_fill(
        self,
        parent_market_id: str,
        parent_market_title: str,
        hedge_token_id: str,
        fill_price: float,
        fill_size: float,
        settled_price: float,
        *,
        underlying: str = "",
        hedge_status: str = "filled_won",
        spot_resolve_price: float = 0.0,
    ) -> None:
        """Record a GTD hedge outcome in trades.csv.

        Called by _redeem_ready_positions() when the auto-redeem loop detects
        that a token_id belongs to a known momentum GTD hedge, or when a RESOLVED
        exit confirms the hedge was never filled.

        Args:
            parent_market_id:    condition_id of the main momentum trade.
            parent_market_title: human label for the market.
            hedge_token_id:      PM CLOB token_id of the hedged (opposite) token.
            fill_price:          price at which the hedge BUY filled (pos.hedge_price).
            fill_size:           number of contracts (0.0 for unfilled hedge).
            settled_price:       1.0 if hedge token won, 0.0 if lost (ignored for unfilled).
            hedge_status:        "filled_won" | "filled_lost" | "unfilled".
            spot_resolve_price:  oracle spot at market resolution time.
        """
        pnl = round((settled_price - fill_price) * fill_size, 6)
        _csv_row: dict = {}
        with self._lock:
            self._realized_pnl += pnl
            _csv_row = {
                "timestamp":        datetime.now(timezone.utc).isoformat(),
                "entry_timestamp":  "",
                "market_id":        parent_market_id,
                "market_title":     (parent_market_title or "")[:60],
                "market_type":      "momentum_hedge",
                "underlying":       underlying,
                "side":             "hedge",
                "size":             round(fill_size, 6),
                "price":            round(fill_price, 4),
                "fees_paid":        0.0,
                "rebates_earned":   0.0,
                "hl_hedge_size":    0.0,
                "hl_entry_price":   0.0,
                "strategy":         "momentum_hedge",
                "spread_id":        "",
                "pnl":              pnl,
                "entry_deviation":  0.0,
                "implied_prob":     0.0,
                "deribit_iv":       0.0,
                "tte_years":        0.0,
                "spot_price":       0.0,
                "exit_spot_price":  settled_price,
                "strike":           0.0,
                "kalshi_price":     0.0,
                "signal_source":      "gtd_hedge",
                "signal_score":       0.0,
                "resolved_outcome":   "WIN" if settled_price >= 0.5 else "LOSS",
                "hedge_order_id":     "",
                "hedge_token_id":     hedge_token_id,
                "hedge_price":        round(fill_price, 4),
                "hedge_size_usd":     round(fill_size * fill_price, 6),
                "hedge_status":       hedge_status,
                "spot_resolve_price": round(spot_resolve_price, 4),
                "hedge_size_filled":    round(fill_size, 6),
                "hedge_avg_fill_price": round(fill_price, 6),
            }
            log.info(
                "GTD hedge fill recorded",
                parent_market_id=parent_market_id[:20],
                hedge_token_id=hedge_token_id[:20],
                fill_price=fill_price,
                fill_size=round(fill_size, 4),
                settled_price=settled_price,
                pnl=pnl,
            )
            # Clean up persisted paper-mode fill entry now that the outcome is
            # written to trades.csv — prevents stale entries after restart.
            if hedge_token_id in self._paper_hedge_fills:
                del self._paper_hedge_fills[hedge_token_id]
                self._save_paper_hedge_fills()
        self._append_csv(_csv_row)

    def update_hedge(
        self,
        market_id: str,
        hl_hedge_size: float,
        hl_entry_price: float,
        hl_side: str,
        side: str = "YES",
    ) -> None:
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos is None:
                log.warning("update_hedge: unknown market_id", market_id=market_id, side=side)
                return
            pos.hl_hedge_size = hl_hedge_size
            pos.hl_entry_price = hl_entry_price
            pos.hl_side = hl_side

    def update_coin_hedge(
        self,
        coin: str,
        notional: float,
    ) -> None:
        """Track a coin-level hedge placed by MakerStrategy (spans multiple positions)."""
        with self._lock:
            if notional <= 0:
                self._coin_hedge_notionals.pop(coin, None)
            else:
                self._coin_hedge_notionals[coin] = notional

    def reconcile_size(self, pm_size: float, *, token_id: str = "", condition_id: str = "", side: str = "") -> bool:
        """Auto-correct Position.size from PM wallet for small fill-rounding diffs.

        Called from /positions/live reconciliation when |pm_size - bot_size| < 0.05.
        Returns True if a position was found and updated, False otherwise.
        """
        with self._lock:
            pos: Optional[Position] = None
            if condition_id and side:
                pos = self._positions.get(self._pos_key(condition_id, side))
            if pos is None and token_id:
                for p in self._positions.values():
                    if not p.is_closed and p.token_id == token_id:
                        pos = p
                        break
            if pos is None or pos.is_closed:
                return False
            old_size = pos.size
            pos.size = pm_size
            log.info(
                "Position size reconciled from PM wallet",
                market_id=pos.market_id,
                side=pos.side,
                old_size=round(old_size, 4),
                new_size=round(pm_size, 4),
            )
            return True

    def record_rebate(self, market_id: str, rebate_usd: float, side: str = "YES") -> None:
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos:
                pos.pm_rebates_earned += rebate_usd

    def close_position(
        self,
        market_id: str,
        exit_price: float,
        *,
        side: str = "YES",
        fees_paid: float = 0.0,
        rebates_earned: float = 0.0,
        resolved_outcome: str = "",
        exit_spot_price: float = 0.0,   # underlying spot (BTC/ETH/…) at exit time
    ) -> Optional[Position]:
        _csv_row: Optional[dict] = None
        _closed_pos: Optional[Position] = None
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos is None or pos.is_closed:
                return None

            pnl = (exit_price - pos.entry_price) * pos.size
            pnl -= fees_paid
            # Total rebates = entry rebate (already on pos) + exit rebate passed in
            total_rebates_earned = pos.pm_rebates_earned + rebates_earned
            pnl += total_rebates_earned

            pos.realized_pnl = pnl
            pos.pm_fees_paid += fees_paid
            pos.pm_rebates_earned = total_rebates_earned
            pos.is_closed = True
            pos.closed_at = datetime.now(timezone.utc)

            # Remove from token→strategy map so a fresh position on the same
            # token after restart isn't mis-labelled from a prior trade.
            if pos.token_id and pos.token_id in self._token_strategy:
                del self._token_strategy[pos.token_id]
                self._save_token_strategy()

            self._realized_pnl += pnl

            # Check hard stop
            if self._realized_pnl < -config.HARD_STOP_DRAWDOWN:
                self._hard_stop_triggered = True
                log.critical(
                    "HARD STOP triggered",
                    realized_pnl=self._realized_pnl,
                    threshold=-config.HARD_STOP_DRAWDOWN,
                )

            _csv_row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "entry_timestamp": pos.opened_at.isoformat() if pos.opened_at else "",
                "market_id": market_id,
                "market_title": pos.market_title,
                "market_type": pos.market_type,
                "underlying": pos.underlying,
                "side": pos.side,
                "size": pos.size,
                "price": pos.entry_price,  # actual token fill price for both YES and NO
                "fees_paid": fees_paid,
                "rebates_earned": total_rebates_earned,
                "hl_hedge_size": pos.hl_hedge_size,
                "hl_entry_price": pos.hl_entry_price,
                "strategy": pos.strategy,
                "spread_id": pos.spread_id or "",
                "pnl": pnl,
                "entry_deviation": pos.entry_deviation,
                "implied_prob": pos.implied_prob,
                "deribit_iv": pos.deribit_iv,
                "tte_years": round(pos.tte_years, 6),
                "spot_price": pos.spot_price,
                "exit_spot_price": exit_spot_price,
                "strike": pos.strike,
                "kalshi_price": pos.kalshi_price,
                "signal_source": pos.signal_source,
                "signal_score": pos.signal_score,
                "resolved_outcome": resolved_outcome,
                "hedge_order_id": pos.hedge_order_id,
                "hedge_token_id": pos.hedge_token_id,
                "hedge_price": pos.hedge_price,
                "hedge_size_usd": pos.hedge_size_usd,
                "hedge_status": "rejected_price" if pos.hedge_fail_reason else "",
                # For rejected_price rows: repurpose spot_resolve_price to store opp_best_ask
                # so the UI can show "Max: Xc · Ask: Yc" tooltip.
                "spot_resolve_price": pos.hedge_opp_best_ask if pos.hedge_fail_reason else 0.0,
                "hedge_size_filled": "",
                "hedge_avg_fill_price": "",
            }
            _closed_pos = pos
            log.info(
                "Position closed",
                market_id=market_id,
                pnl=round(pnl, 4),
                total_realized=round(self._realized_pnl, 4),
            )
        # Write CSV outside the lock so file I/O does not extend lock-hold time.
        # _append_csv schedules this on the thread pool when an event loop is running.
        if _csv_row is not None:
            self._append_csv(_csv_row)
        return _closed_pos

    # ── HL hedge accounting ──────────────────────────────────────────────────

    def record_hl_hedge_trade(
        self,
        coin: str,
        direction: str,    # LONG or SHORT (position direction, not the closing order)
        open_price: float,
        close_price: float,
        size_coins: float,
    ) -> None:
        """Record a completed HL hedge round-trip in trades.csv.

        P&L = price move (direction-adjusted) − open fee − close fee.
        Both fees use HL_TAKER_FEE because market orders are always taker.
        """
        open_fee = size_coins * open_price * config.HL_TAKER_FEE
        close_fee = size_coins * close_price * config.HL_TAKER_FEE
        total_fees = round(open_fee + close_fee, 6)

        if direction == "LONG":
            price_pnl = (close_price - open_price) * size_coins
        else:  # SHORT
            price_pnl = (open_price - close_price) * size_coins

        pnl = round(price_pnl - total_fees, 6)

        with self._lock:
            self._realized_pnl += pnl
            _csv_row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "entry_timestamp": "",
                "market_id": f"hl_{coin}",
                "market_title": f"HL {coin} perp hedge",
                "market_type": "hl_perp",
                "underlying": coin,
                "side": direction,
                "size": round(size_coins, 6),
                "price": round(open_price, 4),
                "fees_paid": total_fees,
                "rebates_earned": 0.0,
                "hl_hedge_size": round(size_coins, 6),
                "hl_entry_price": round(open_price, 4),
                "strategy": "maker_hedge",
                "pnl": pnl,
                "entry_deviation": 0.0,
                "implied_prob": 0.0,
                "deribit_iv": 0.0,
                "tte_years": 0.0,
                "spot_price": round(close_price, 4),
                "strike": 0.0,
                "kalshi_price": 0.0,
                "signal_source": "hl_hedge",
                "signal_score": 0.0,
                "resolved_outcome": "",
            }
            log.info(
                "HL hedge trade recorded",
                coin=coin, direction=direction,
                open_price=open_price, close_price=close_price,
                size_coins=size_coins, fees=total_fees, pnl=pnl,
            )
        # Write CSV outside the lock — off-loaded to thread pool by _append_csv.
        self._append_csv(_csv_row)

    # ── Snapshots ──────────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        """Return a serialisable snapshot for the API server."""
        with self._lock:
            open_pos = [p for p in self._positions.values() if not p.is_closed]
            return {
                "hard_stop_triggered": self._hard_stop_triggered,
                "realized_pnl": round(self._realized_pnl, 4),
                "open_positions_count": len(open_pos),
                "total_pm_exposure": round(sum(p.entry_cost_usd for p in open_pos), 2),
                "total_pm_capital_deployed": round(sum(p.entry_cost_usd for p in open_pos), 2),
                "total_hl_notional": round(
                    sum(
                        abs(p.hl_hedge_size * p.hl_entry_price)
                        for p in open_pos
                        if p.hl_hedge_size
                    ), 2
                ),
                "positions": [dataclasses.asdict(p) for p in open_pos],
                "limits": {
                    "max_pm_per_market": config.MAX_PM_EXPOSURE_PER_MARKET,
                    "max_total_pm": config.MAX_TOTAL_PM_EXPOSURE,
                    "max_hl_notional": config.MAX_HL_NOTIONAL,
                    "hard_stop_threshold": config.HARD_STOP_DRAWDOWN,
                },
            }

    @property
    def hard_stop_triggered(self) -> bool:
        return self._hard_stop_triggered

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def get_open_positions(self) -> list:
        """Return a list of open (not closed) Position objects."""
        with self._lock:
            return [p for p in self._positions.values() if not p.is_closed]

    def get_positions(self) -> dict:
        """Return a snapshot dict of all positions (open and closed)."""
        with self._lock:
            return dict(self._positions)
