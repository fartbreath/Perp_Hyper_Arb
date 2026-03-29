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
TRADES_HEADER = [
    "timestamp", "market_id", "market_title", "market_type", "underlying", "side", "size", "price",
    "fees_paid", "rebates_earned", "hl_hedge_size", "hl_entry_price",
    "strategy", "pnl",
    # Signal context — populated for mispricing strategy; 0.0 for maker
    "entry_deviation",  # |pm_price - N(d2)| at signal time
    "implied_prob",     # Deribit N(d2) value
    "deribit_iv",       # annualised IV used in model
    "tte_years",        # time-to-expiry at entry (years)
    "spot_price",       # underlying spot price at entry
    "strike",           # parsed target price from market title
    "kalshi_price",     # matched Kalshi YES price at signal time (0.0 = no match)
    "signal_source",    # "kalshi_confirmed" | "kalshi_only" | "nd2_only"
    "signal_score",     # quality score 0–100 at signal time
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


# ── Position ──────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Position:
    market_id: str
    market_type: str          # bucket_15m | bucket_1h | bucket_daily | milestone
    underlying: str           # BTC | ETH | SOL
    side: str                 # YES | NO
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
    # BUY YES: entry_price × size.  SELL YES (NO side): (1 − entry_price) × size.
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

    @property
    def pm_delta_notional(self) -> float:
        """Signed notional exposure: positive = net long YES."""
        return self.size if self.side == "YES" else -self.size


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
        self._ensure_csv()

    # ── CSV ────────────────────────────────────────────────────────────────────

    def _ensure_csv(self) -> None:
        if not TRADES_CSV.exists():
            with TRADES_CSV.open("w", newline="") as f:
                csv.writer(f).writerow(TRADES_HEADER)
            return
        # If the file exists but the header is stale (schema changed), migrate it:
        # read the current header and, if columns differ, back up the old file
        # and start a fresh one with the current schema.
        with TRADES_CSV.open("r", newline="") as f:
            reader = csv.reader(f)
            try:
                existing_header = next(reader)
            except StopIteration:
                existing_header = []
        if existing_header != TRADES_HEADER:
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
                # YES:  avg_price = total_cost / total_size
                # NO:   entry_cost = (1 − price) × size  →  avg_price = 1 − cost/size
                if existing.size > 0:
                    if existing.side == "YES":
                        existing.entry_price = existing.entry_cost_usd / existing.size
                    else:
                        existing.entry_price = 1.0 - existing.entry_cost_usd / existing.size
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
    ) -> Optional[Position]:
        _csv_row: Optional[dict] = None
        _closed_pos: Optional[Position] = None
        with self._lock:
            pos = self._positions.get(self._pos_key(market_id, side))
            if pos is None or pos.is_closed:
                return None

            pnl = (exit_price - pos.entry_price) * pos.size
            if pos.side == "NO":
                pnl = -pnl
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
                "market_id": market_id,
                "market_title": pos.market_title,
                "market_type": pos.market_type,
                "underlying": pos.underlying,
                "side": pos.side,
                "size": pos.size,
                "price": pos.entry_price,
                "fees_paid": fees_paid,
                "rebates_earned": total_rebates_earned,
                "hl_hedge_size": pos.hl_hedge_size,
                "hl_entry_price": pos.hl_entry_price,
                "strategy": pos.strategy,
                "pnl": pnl,
                "entry_deviation": pos.entry_deviation,
                "implied_prob": pos.implied_prob,
                "deribit_iv": pos.deribit_iv,
                "tte_years": round(pos.tte_years, 6),
                "spot_price": pos.spot_price,
                "strike": pos.strike,
                "kalshi_price": pos.kalshi_price,
                "signal_source": pos.signal_source,
                "signal_score": pos.signal_score,
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
