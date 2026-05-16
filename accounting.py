"""
accounting.py — Passive, append-only position accounting ledger.

DESIGN PRINCIPLES
─────────────────
1. Zero coupling to strategy execution.  Strategies never import this module.
   All data flows in via three public hooks called from risk.py:
       on_entry_fill(pos, fill_price, contracts, order_id, source)
       on_exit_fill(pos, fill_price, contracts, order_id, source, exit_type)
       on_resolved(condition_id, resolved_yes_price, spot_exit)

2. Fills are immutable.  Every matched fill event appends one row to
   data/acct_fills.jsonl.  Nothing in this file is ever rewritten.

3. Positions are a state machine.  data/acct_positions.json holds the live
   state of each AccountingPosition, keyed by pos_id.  Mutated in-place as
   fills arrive and the market resolves.

4. Ledger records are written once, never patched.
   data/acct_ledger.csv is the final P&L record.  A row is appended only
   when a position reaches a terminal status AND PM has confirmed the outcome.

5. PM /activity is the second source of truth for every position.
   Reconciliation runs in the background; it matches fills by token_id
   (PM field: "asset") and confirms outcomes by condition_id.

DATA FLOW
─────────
  risk.py close_position()
       │ calls on_exit_fill()
       ▼
  AccountingPosition.status → CLOSING
       │ background reconciler queries PM /activity
       ▼
  status → PENDING_RESOLVE
       │ background reconciler queries CLOB winner flag
       ▼
  status → RESOLVED_WIN | RESOLVED_LOSS | SL | TP
       │ LedgerRecord appended
       ▼
  acct_ledger.csv (immutable row)

STATUS LIFECYCLE
────────────────
  LIVE → CLOSING → PENDING_RESOLVE → RESOLVED_WIN | RESOLVED_LOSS | SL | TP
                                  ↘ ERROR  (exit unconfirmed > CONFIRM_TIMEOUT_S)
"""
from __future__ import annotations

import asyncio
import csv
import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_DATA = Path(__file__).parent / "data"
_DATA.mkdir(exist_ok=True)

FILLS_JSONL       = _DATA / "acct_fills.jsonl"
POSITIONS_JSON    = _DATA / "acct_positions.json"
LEDGER_CSV        = _DATA / "acct_ledger.csv"

LEDGER_HEADER = [
    "record_id", "recorded_at",
    "pos_id", "pair_id", "parent_pos_id",
    "strategy", "fill_type",
    "market_id", "market_title", "market_type", "underlying",
    "side", "token_id",
    "entry_vwap", "entry_contracts", "entry_cost_usd", "entry_time",
    "exit_vwap",  "exit_contracts",  "exit_time",     "exit_type",     "exit_reason",
    "spot_entry", "spot_exit", "strike", "tte_seconds",
    "resolve_price", "resolved_outcome",
    "gross_pnl", "fees_usd", "rebates_usd", "net_pnl",
    "pm_entry_confirmed", "pm_exit_confirmed",
    "signal_source", "signal_score",
    "reconciliation_notes",
]

# How many seconds to wait after the first exit fill before declaring ERROR.
_CONFIRM_TIMEOUT_S: int = 600  # 10 minutes

# ── PM client singleton (set at startup; used by ledger writer to enrich rows) ─
# Wired by reconcile_loop() the first time it runs so we don't have to plumb
# pm_client through every on_*_fill call site.
_PM_CLIENT_REF = None  # type: ignore[var-annotated]


def set_pm_client(pm) -> None:
    """Register the live PMClient so ledger writes can pull authoritative
    fill prices, sizes and fees from PM /data/trades.

    Optional. If never called (e.g. PAPER mode, tests) the ledger writer
    silently degrades to using the in-memory pos values.
    """
    global _PM_CLIENT_REF
    _PM_CLIENT_REF = pm


# Divergence threshold: if pos.entry_contracts - exit_contracts diverges from
# actual on-chain CLOB balance by more than this fraction, the ledger row is
# tagged RECONCILE_REQUIRED and exit_contracts is capped at the on-chain
# balance to prevent over-counting redemption payouts.
_DIVERGENCE_THRESHOLD: float = 0.05  # 5%


# ── Status constants ───────────────────────────────────────────────────────────
class PositionStatus:
    LIVE            = "LIVE"
    CLOSING         = "CLOSING"
    PENDING_RESOLVE = "PENDING_RESOLVE"
    RESOLVED_WIN    = "RESOLVED_WIN"
    RESOLVED_LOSS   = "RESOLVED_LOSS"
    SL              = "SL"
    TP              = "TP"
    ERROR           = "ERROR"

    TERMINAL: frozenset = frozenset({
        "RESOLVED_WIN", "RESOLVED_LOSS", "SL", "TP", "ERROR"
    })


# ── Fill (immutable) ───────────────────────────────────────────────────────────
@dataclass
class Fill:
    fill_id:         str    # uuid
    order_id:        str    # PM CLOB order ID
    token_id:        str    # CLOB token asset ID — PM reconciliation key
    condition_id:    str    # market condition ID
    timestamp_utc:   str    # ISO UTC
    side:            str    # BUY | SELL
    fill_price:      float  # price of this specific fill event
    contracts:       float  # contracts matched in this event (incremental, not cumulative)
    cost_usd:        float  # fill_price × contracts
    source:          str    # ws | rest | paper | pm_activity
    pm_activity_id:  str = ""
    pm_confirmed:    bool = False


# ── AccountingPosition (state machine) ────────────────────────────────────────
@dataclass
class AccountingPosition:
    # Identity
    pos_id:          str
    strategy:        str   # momentum | opening_neutral | momentum_hedge | maker | mispricing
    fill_type:       str   # MAIN | LOSER_EXIT | P_WINNER | WINNER | HEDGE

    # Relationship — both optional
    pair_id:         str = ""   # groups YES+NO legs of same ON trade
    parent_pos_id:   str = ""   # hedge → links to main position pos_id

    # Market
    market_id:       str = ""
    market_title:    str = ""
    market_type:     str = ""
    underlying:      str = ""
    side:            str = ""   # YES | NO | UP | DOWN
    token_id:        str = ""   # CLOB token ID = PM /activity "asset" field

    # Entry (aggregated across all entry fills)
    entry_fill_ids:  list = field(default_factory=list)
    entry_vwap:      float = 0.0
    entry_contracts: float = 0.0
    entry_cost_usd:  float = 0.0
    entry_time:      str = ""
    pm_entry_confirmed: bool = False

    # Context at entry
    spot_entry:      float = 0.0
    strike:          float = 0.0
    tte_seconds:     float = 0.0
    signal_source:   str = ""
    signal_score:    float = 0.0

    # Exit (accumulated as fills arrive; hedge exit may be REDEMPTION)
    exit_fill_ids:   list = field(default_factory=list)
    exit_vwap:       float = 0.0   # updated incrementally
    exit_contracts:  float = 0.0
    exit_time:       str = ""
    exit_type:       str = ""      # RESOLVED | TAKER | SL | TP | LOSER_EXIT | REDEMPTION
    exit_reason:     str = ""      # ExitReason string: momentum_stop_loss | prob_sl | upfrac_exit | loser_exit | …
    closing_since:   str = ""      # timestamp when CLOSING state was first entered
    pm_exit_confirmed: bool = False

    # Resolution
    spot_exit:       float = 0.0
    resolve_price:   float = -1.0  # -1 = unknown; 0.0 or 1.0 once settled (YES-token price)
    resolved_outcome: str = ""     # WIN | LOSS | ""

    # Fees / rebates (accumulated)
    fees_usd:        float = 0.0
    rebates_usd:     float = 0.0

    # Status
    status:          str = PositionStatus.LIVE


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vwap(old_vwap: float, old_qty: float, new_price: float, new_qty: float) -> float:
    """Incremental VWAP update."""
    total = old_qty + new_qty
    if total == 0:
        return 0.0
    return (old_vwap * old_qty + new_price * new_qty) / total


def _gross_pnl(pos: AccountingPosition) -> float:
    """(exit_vwap - entry_vwap) × exit_contracts."""
    return round((pos.exit_vwap - pos.entry_vwap) * pos.exit_contracts, 8)


# ── Ledger (append-only CSV) ───────────────────────────────────────────────────

def _ensure_ledger() -> None:
    if not LEDGER_CSV.exists():
        with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LEDGER_HEADER)


def _append_ledger(row: dict) -> None:
    _ensure_ledger()
    with LEDGER_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_HEADER, extrasaction="ignore")
        w.writerow(row)


def _enrich_position_from_clob(pos: AccountingPosition) -> str:
    """Pull authoritative fill prices/sizes/fees from PM /data/trades.

    Polymarket /data/trades returns the actual taker execution records for an
    order, including a ``fee_rate_bps`` field per trade.  We sum size, value
    and fees per side and overwrite the in-memory pos values so the ledger row
    reflects what really happened on-chain rather than whatever the local WS
    fill event reported (which can be stale or wrong on partial fills).

    For RESOLVED / REDEMPTION exits we keep the settlement-derived exit_vwap
    (1.0 for win, 0.0 for loss, or payout/size for redeem) and only refresh
    fees \u2014 the SELL side never executed for a held-to-resolution position.

    Returns a human-readable note describing any non-trivial deltas, suitable
    for the reconciliation_notes ledger column.  Empty string when no enrichment
    happened or values matched within tolerance.
    """
    pm = _PM_CLIENT_REF
    if pm is None or getattr(pm, "_clob", None) is None:
        return ""
    try:
        from py_clob_client_v2.clob_types import TradeParams
    except Exception:
        return ""

    fills = _load_fills_by_token(pos.token_id)
    entry_oids = sorted({f.order_id for f in fills if f.side == "BUY"  and f.order_id})
    exit_oids  = sorted({f.order_id for f in fills if f.side == "SELL" and f.order_id})

    def _aggregate(order_ids: list[str]) -> tuple[float, float, float, float]:
        total_sz, total_val, total_fees = 0.0, 0.0, 0.0
        for oid in order_ids:
            try:
                trades = pm._clob.get_trades(TradeParams(id=oid))  # blocking REST
            except Exception as exc:
                log.debug("acct enrich: get_trades failed", order_id=oid[:20], exc=str(exc))
                continue
            for t in (trades or []):
                try:
                    sz = float(t.get("size", 0))
                    px = float(t.get("price", 0))
                    fee_bps = float(t.get("fee_rate_bps", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if sz <= 0 or px <= 0:
                    continue
                total_sz   += sz
                total_val  += sz * px
                total_fees += sz * px * (fee_bps / 10_000.0)
        vwap = (total_val / total_sz) if total_sz > 0 else 0.0
        return total_sz, vwap, total_val, total_fees

    notes: list[str] = []
    fees_total = 0.0

    if entry_oids:
        sz, vw, val, fees = _aggregate(entry_oids)
        if sz > 0:
            tol_sz = max(0.01 * pos.entry_contracts, 0.001)
            if abs(sz - pos.entry_contracts) > tol_sz or abs(vw - pos.entry_vwap) > 0.001:
                notes.append(
                    f"entry_clob: contracts {pos.entry_contracts:.4f}->{sz:.4f}, "
                    f"vwap {pos.entry_vwap:.4f}->{vw:.4f}"
                )
            pos.entry_contracts = round(sz, 8)
            pos.entry_vwap      = round(vw, 8)
            pos.entry_cost_usd  = round(val, 8)
            fees_total += fees

    if exit_oids:
        sz, vw, val, fees = _aggregate(exit_oids)
        if sz > 0:
            tol_sz = max(0.01 * pos.exit_contracts, 0.001)
            if pos.exit_type not in ("RESOLVED", "REDEMPTION"):
                if abs(sz - pos.exit_contracts) > tol_sz or abs(vw - pos.exit_vwap) > 0.001:
                    notes.append(
                        f"exit_clob: contracts {pos.exit_contracts:.4f}->{sz:.4f}, "
                        f"vwap {pos.exit_vwap:.4f}->{vw:.4f}"
                    )
                pos.exit_contracts = round(sz, 8)
                pos.exit_vwap      = round(vw, 8)
            else:
                # Settlement-driven exit: keep exit_vwap, but sell-side fees still apply
                if abs(sz - pos.exit_contracts) > tol_sz:
                    notes.append(
                        f"exit_clob_partial: pre_resolve_sells={sz:.4f} (kept settlement vwap)"
                    )
            fees_total += fees

    if fees_total > 0:
        if abs(fees_total - pos.fees_usd) > 1e-6:
            notes.append(f"fees_clob: {pos.fees_usd:.4f}->{fees_total:.4f}")
        pos.fees_usd = round(fees_total, 8)

    return "; ".join(notes)


def _check_clob_balance_divergence(pos: AccountingPosition) -> str:
    """Compare expected on-chain balance vs actual CLOB balance.

    For positions that didn't fully exit on-CLOB before resolution
    (RESOLVED / REDEMPTION), the residual contracts must still be sitting in
    the wallet to be redeemed.  If the actual CLOB balance is materially
    lower than (entry_contracts - exit_contracts), tokens were drained
    elsewhere (manual UI sale, separate bot, etc.) and the redemption payout
    in the ledger row would be overstated.

    Caps pos.exit_contracts at the actual residual + on-chain balance and
    returns a RECONCILE_REQUIRED note when divergence exceeds the threshold.
    Returns empty string when no divergence (or no pm_client wired).
    """
    pm = _PM_CLIENT_REF
    if pm is None or getattr(pm, "_clob", None) is None:
        return ""
    if pos.exit_type not in ("RESOLVED", "REDEMPTION"):
        return ""
    if not pos.token_id:
        return ""
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    except Exception:
        return ""
    try:
        resp = pm._clob.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=pos.token_id),
        )
    except Exception as exc:
        log.debug("acct divergence: balance fetch failed", token=pos.token_id[:16], exc=str(exc))
        return ""
    raw = resp.get("balance") if isinstance(resp, dict) else None
    if raw is None:
        return ""
    try:
        clob_balance = float(raw) / 1_000_000.0
    except (TypeError, ValueError):
        return ""

    # Already-sold contracts (true CLOB SELL fills) reduce the expected residual.
    sold_already = sum(
        f.contracts for f in _load_fills_by_token(pos.token_id) if f.side == "SELL"
    )
    expected_residual = max(0.0, pos.entry_contracts - sold_already)
    if expected_residual <= 1e-6:
        return ""

    diff_frac = abs(expected_residual - clob_balance) / expected_residual
    if diff_frac < _DIVERGENCE_THRESHOLD:
        return ""

    # Cap settlement payout at what is actually redeemable on-chain.
    capped_exit = round(sold_already + clob_balance, 8)
    note = (
        f"RECONCILE_REQUIRED: token_id={pos.token_id[:16]} "
        f"expected_residual={expected_residual:.4f} clob_balance={clob_balance:.4f} "
        f"diff_frac={diff_frac:.3f} exit_contracts {pos.exit_contracts:.4f}->{capped_exit:.4f}"
    )
    log.warning(
        "acct: CLOB balance diverges from expected residual \u2014 capping payout",
        pos_id=pos.pos_id[:12],
        token_id=pos.token_id[:16],
        expected_residual=round(expected_residual, 6),
        clob_balance=round(clob_balance, 6),
        capped_exit=capped_exit,
    )
    pos.exit_contracts = capped_exit
    return note


def _write_ledger_record(pos: AccountingPosition) -> None:
    """Flatten AccountingPosition into a single LedgerRecord and append.

    NOTE: CLOB enrichment (`_enrich_position_from_clob`) and on-chain
    balance divergence checks (`_check_clob_balance_divergence`) are NOT
    called inline here.  They issue blocking REST requests against the PM
    CLOB and `_write_ledger_record` runs from sync `on_resolved`, which is
    called from the async `_reconcile_once` loop — running blocking I/O on
    the event-loop thread starves PM WS shard pings, causing CloseCode 1006
    cascades.

    Enrichment / divergence are now run on-demand via the webapp's
    `/reconcile/run` endpoint, which can call those helpers from a thread
    via `asyncio.to_thread` without blocking the trading loop.
    """
    reconciliation_notes = ""

    gross = _gross_pnl(pos)
    net   = round(gross - pos.fees_usd + pos.rebates_usd, 8)
    row = {
        "record_id":          str(uuid.uuid4()),
        "recorded_at":        _now_iso(),
        "pos_id":             pos.pos_id,
        "pair_id":            pos.pair_id,
        "parent_pos_id":      pos.parent_pos_id,
        "strategy":           pos.strategy,
        "fill_type":          pos.fill_type,
        "market_id":          pos.market_id,
        "market_title":       pos.market_title,
        "market_type":        pos.market_type,
        "underlying":         pos.underlying,
        "side":               pos.side,
        "token_id":           pos.token_id,
        "entry_vwap":         round(pos.entry_vwap, 8),
        "entry_contracts":    round(pos.entry_contracts, 6),
        "entry_cost_usd":     round(pos.entry_cost_usd, 6),
        "entry_time":         pos.entry_time,
        "exit_vwap":          round(pos.exit_vwap, 8),
        "exit_contracts":     round(pos.exit_contracts, 6),
        "exit_time":          pos.exit_time,
        "exit_type":          pos.exit_type,
        "exit_reason":        pos.exit_reason,
        "spot_entry":         pos.spot_entry,
        "spot_exit":          pos.spot_exit,
        "strike":             pos.strike,
        "tte_seconds":        pos.tte_seconds,
        "resolve_price":      pos.resolve_price if pos.resolve_price >= 0 else "",
        "resolved_outcome":   pos.resolved_outcome,
        "gross_pnl":          gross,
        "fees_usd":           round(pos.fees_usd, 8),
        "rebates_usd":        round(pos.rebates_usd, 8),
        "net_pnl":            net,
        "pm_entry_confirmed": pos.pm_entry_confirmed,
        "pm_exit_confirmed":  pos.pm_exit_confirmed,
        "signal_source":      pos.signal_source,
        "signal_score":       pos.signal_score,
        "reconciliation_notes": reconciliation_notes,
    }
    _append_ledger(row)
    log.info(
        "Accounting ledger record written",
        pos_id=pos.pos_id[:12],
        strategy=pos.strategy,
        market=pos.market_title[:50],
        side=pos.side,
        net_pnl=net,
        status=pos.status,
    )


# ── Fills (append-only JSONL) ──────────────────────────────────────────────────

def _append_fill(fill: Fill) -> None:
    """Append one fill to acct_fills.jsonl.  Never rewrites."""
    with FILLS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(fill)) + "\n")


def _load_fills_by_token(token_id: str) -> list[Fill]:
    """Load all fills for a given token_id from acct_fills.jsonl."""
    result: list[Fill] = []
    if not FILLS_JSONL.exists():
        return result
    with FILLS_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("token_id") == token_id:
                    result.append(Fill(**d))
            except Exception:
                pass
    return result


# ── Position store (JSON) ──────────────────────────────────────────────────────

def _load_positions() -> dict[str, AccountingPosition]:
    if not POSITIONS_JSON.exists():
        return {}
    try:
        raw = json.loads(POSITIONS_JSON.read_text(encoding="utf-8"))
        return {k: AccountingPosition(**v) for k, v in raw.items()}
    except Exception as exc:
        log.error("acct: failed to load acct_positions.json", exc=str(exc))
        return {}


def _save_positions(positions: dict[str, AccountingPosition]) -> None:
    tmp = POSITIONS_JSON.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({k: asdict(v) for k, v in positions.items()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(POSITIONS_JSON)


# ── Ledger singleton ───────────────────────────────────────────────────────────

class _Ledger:
    """
    Thread-safe accounting ledger.

    All public methods are safe to call from any thread or from async code.
    Internal state (positions dict) is protected by a threading.Lock.
    Disk writes are done while holding the lock; they are fast (small JSON).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._positions: dict[str, AccountingPosition] = _load_positions()
        # token_id → pos_id for O(1) lookup
        self._token_index: dict[str, str] = {
            p.token_id: pid
            for pid, p in self._positions.items()
            if p.token_id
        }
        _ensure_ledger()

    # ── Public hooks (called from risk.py) ─────────────────────────────────────

    def on_entry_fill(
        self,
        *,
        # Core identifiers
        token_id:     str,
        condition_id: str,
        order_id:     str,
        # Fill data
        fill_price:   float,
        contracts:    float,
        source:       str = "ws",
        # Position context (passed once at first fill; ignored on subsequent fills)
        strategy:     str = "",
        fill_type:    str = "MAIN",    # MAIN | LOSER_EXIT | P_WINNER | WINNER | HEDGE
        pair_id:      str = "",
        parent_pos_id: str = "",
        market_title: str = "",
        market_type:  str = "",
        underlying:   str = "",
        side:         str = "",
        spot_entry:   float = 0.0,
        strike:       float = 0.0,
        tte_seconds:  float = 0.0,
        signal_source: str = "",
        signal_score:  float = 0.0,
        fees_usd:     float = 0.0,
        rebates_usd:  float = 0.0,
    ) -> str:
        """
        Record one entry fill event.  Multiple calls for the same token_id
        accumulate into the same AccountingPosition via VWAP.

        Returns the pos_id of the position that was updated/created.
        """
        # Manual positions (restored from PM wallet without a matching bot entry)
        # have strategy="unknown".  Do not record them in the ledger — only
        # bot-created positions belong in accounting.
        if strategy == "unknown":
            log.debug(
                "acct: skipping entry fill — manual/unknown position",
                token_id=token_id[:16],
            )
            return ""

        now = _now_iso()
        fill = Fill(
            fill_id=str(uuid.uuid4()),
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id,
            timestamp_utc=now,
            side="BUY",
            fill_price=fill_price,
            contracts=contracts,
            cost_usd=round(fill_price * contracts, 8),
            source=source,
        )
        _append_fill(fill)

        with self._lock:
            # Find or create the position for this token
            pos_id = self._token_index.get(token_id)
            if pos_id is None:
                pos_id = str(uuid.uuid4())
                pos = AccountingPosition(
                    pos_id=pos_id,
                    strategy=strategy,
                    fill_type=fill_type,
                    pair_id=pair_id,
                    parent_pos_id=parent_pos_id,
                    market_id=condition_id,
                    market_title=market_title,
                    market_type=market_type,
                    underlying=underlying,
                    side=side,
                    token_id=token_id,
                    entry_time=now,
                    spot_entry=spot_entry,
                    strike=strike,
                    tte_seconds=tte_seconds,
                    signal_source=signal_source,
                    signal_score=signal_score,
                )
                self._positions[pos_id] = pos
                self._token_index[token_id] = pos_id
            else:
                pos = self._positions[pos_id]

            # Accumulate fill into position entry
            pos.entry_vwap     = _vwap(pos.entry_vwap, pos.entry_contracts, fill_price, contracts)
            pos.entry_contracts = round(pos.entry_contracts + contracts, 8)
            pos.entry_cost_usd  = round(pos.entry_cost_usd + fill.cost_usd, 8)
            pos.entry_fill_ids.append(fill.fill_id)
            pos.fees_usd        = round(pos.fees_usd + fees_usd, 8)
            pos.rebates_usd     = round(pos.rebates_usd + rebates_usd, 8)

            _save_positions(self._positions)

        log.debug(
            "acct: entry fill recorded",
            pos_id=pos_id[:12],
            token_id=token_id[:16],
            fill_price=fill_price,
            contracts=contracts,
            vwap=round(pos.entry_vwap, 4),
            total_contracts=round(pos.entry_contracts, 4),
        )
        return pos_id

    def on_exit_fill(
        self,
        *,
        token_id:    str,
        order_id:    str,
        fill_price:  float,
        contracts:   float,
        exit_type:   str,    # RESOLVED | TAKER | SL | TP | LOSER_EXIT | REDEMPTION
        exit_reason: str = "",   # ExitReason string (more granular; stored for display)
        source:      str = "ws",
        spot_exit:   float = 0.0,
        fees_usd:    float = 0.0,
        rebates_usd: float = 0.0,
    ) -> Optional[str]:
        """
        Record one exit fill event.  Multiple calls accumulate via VWAP.
        Position moves to CLOSING after the first exit fill.

        Returns the pos_id, or None if no matching position found.
        """
        now = _now_iso()
        fill = Fill(
            fill_id=str(uuid.uuid4()),
            order_id=order_id,
            token_id=token_id,
            condition_id="",   # filled in below from position
            timestamp_utc=now,
            side="SELL",
            fill_price=fill_price,
            contracts=contracts,
            cost_usd=round(fill_price * contracts, 8),
            source=source,
        )

        with self._lock:
            pos_id = self._token_index.get(token_id)
            if pos_id is None:
                log.warning(
                    "acct: on_exit_fill — no position for token_id",
                    token_id=token_id[:16],
                )
                return None

            pos = self._positions[pos_id]

            # Guard: if the reconciler already finalized this position (moved to
            # terminal), ignore a late exit fill from the risk engine.  The ledger
            # record was already written correctly; a late on_exit_fill would
            # corrupt the exit_vwap and exit_contracts on the terminal entry.
            if pos.status in PositionStatus.TERMINAL:
                log.debug(
                    "acct: on_exit_fill skipped — position already terminal",
                    pos_id=pos_id[:12],
                    status=pos.status,
                    token_id=token_id[:16],
                )
                return pos_id

            fill.condition_id = pos.market_id

            _append_fill(fill)

            # Accumulate exit fill
            pos.exit_vwap      = _vwap(pos.exit_vwap, pos.exit_contracts, fill_price, contracts)
            pos.exit_contracts  = round(pos.exit_contracts + contracts, 8)
            pos.exit_fill_ids.append(fill.fill_id)
            pos.exit_time       = now
            pos.fees_usd        = round(pos.fees_usd + fees_usd, 8)
            pos.rebates_usd     = round(pos.rebates_usd + rebates_usd, 8)

            if pos.spot_exit == 0.0 and spot_exit > 0.0:
                pos.spot_exit = spot_exit

            # First exit fill — record exit_type and move to CLOSING
            if not pos.exit_type:
                pos.exit_type = exit_type
            if not pos.exit_reason and exit_reason:
                pos.exit_reason = exit_reason
            if pos.status == PositionStatus.LIVE:
                pos.status       = PositionStatus.CLOSING
                pos.closing_since = now

            # If the fill came from the PM order API (ws/rest), we already have
            # authoritative confirmation — no need to wait for /activity polling.
            # Paper fills are also immediately confirmed (no real PM transaction).
            if source in ("ws", "rest", "paper"):
                pos.pm_exit_confirmed = True

            _save_positions(self._positions)

        log.debug(
            "acct: exit fill recorded",
            pos_id=pos_id[:12],
            exit_type=exit_type,
            fill_price=fill_price,
            contracts=contracts,
            vwap=round(pos.exit_vwap, 4),
        )
        return pos_id

    def on_resolved(
        self,
        condition_id:      str,
        resolved_yes_price: float,  # 1.0 = YES/UP won; 0.0 = NO/DOWN won
        spot_exit:         float = 0.0,
    ) -> None:
        """
        Record market resolution.  Moves all CLOSING/PENDING_RESOLVE positions
        for this condition_id to a terminal status and writes ledger records.

        resolved_yes_price is always in YES-token space (preamble rule).
        """
        with self._lock:
            changed = False
            for pos in self._positions.values():
                if pos.market_id != condition_id:
                    continue
                if pos.status in PositionStatus.TERMINAL:
                    continue  # already finalised

                # Determine WIN/LOSS for this specific token side
                yes_sides = {"YES", "UP", "BUY_YES"}
                token_won = (
                    resolved_yes_price >= 0.99
                    if pos.side in yes_sides
                    else resolved_yes_price <= 0.01
                )

                pos.resolve_price    = resolved_yes_price
                pos.resolved_outcome = "WIN" if token_won else "LOSS"
                if spot_exit > 0.0:
                    pos.spot_exit = spot_exit

                # Settle any contracts not yet accounted for by pre-resolution fills.
                # Case 1 (pure resolution, no prior exit): exit_contracts == 0
                #   → set all exit fields at settlement price.
                # Case 2 (partial pre-resolution exit): 0 < exit_contracts < entry_contracts
                #   → blend remaining contracts into exit_vwap at settlement price;
                #     advance exit_contracts to entry_contracts.
                # (If exit_contracts already equals entry_contracts, nothing to do.)
                settlement = 1.0 if token_won else 0.0
                remaining  = round(pos.entry_contracts - pos.exit_contracts, 8)
                if remaining > 1e-9:
                    pos.exit_vwap      = _vwap(pos.exit_vwap, pos.exit_contracts,
                                               settlement, remaining)
                    pos.exit_contracts = pos.entry_contracts
                    pos.exit_time      = _now_iso()
                    if not pos.exit_type:
                        pos.exit_type  = "RESOLVED"
                    if not pos.exit_reason:
                        pos.exit_reason = "resolved"

                # Terminal status
                if pos.exit_type in ("SL",):
                    pos.status = PositionStatus.SL
                elif pos.exit_type in ("TP",):
                    pos.status = PositionStatus.TP
                else:
                    pos.status = PositionStatus.RESOLVED_WIN if token_won else PositionStatus.RESOLVED_LOSS

                changed = True

            if changed:
                _save_positions(self._positions)

        # Write ledger records only when positions actually transitioned
        if changed:
            for pos in list(self._positions.values()):
                if (
                    pos.market_id == condition_id
                    and pos.status in PositionStatus.TERMINAL
                    and pos.status != PositionStatus.ERROR
                ):
                    _write_ledger_record(pos)

    def on_pair_promoted(self, token_id: str, new_fill_type: str = "MAIN") -> None:
        """
        Called when an ON winner is promoted to momentum.
        Mutates the position in-place (same pos_id, continuous lifecycle).
        """
        with self._lock:
            pos_id = self._token_index.get(token_id)
            if pos_id is None:
                return
            pos = self._positions[pos_id]
            pos.fill_type = new_fill_type
            pos.strategy  = "momentum"
            _save_positions(self._positions)
        log.info(
            "acct: ON winner promoted to momentum",
            token_id=token_id[:16],
            pos_id=pos_id[:12],
        )

    def on_winner_held(self, token_id: str) -> None:
        """
        Called when an ON winner is held as opening_neutral (PROMOTE_TO_MOMENTUM=False).
        Tags fill_type="P_WINNER" (predicted winner) without changing the strategy.
        P_WINNER != WINNER — it means this leg is expected to resolve at $1 based on
        which side lost.  Actual resolution is confirmed later via on_resolved().
        """
        with self._lock:
            pos_id = self._token_index.get(token_id)
            if pos_id is None:
                return
            pos = self._positions[pos_id]
            pos.fill_type = "P_WINNER"
            _save_positions(self._positions)
        log.info(
            "acct: ON predicted-winner tagged as P_WINNER (held as opening_neutral)",
            token_id=token_id[:16],
            pos_id=pos_id[:12],
        )

    def on_ron_exit(self, winner_token_id: str, loser_token_id: str) -> None:
        """
        Called by ReverseOpenNeutralScanner when the exit trigger fires.

        Retags both legs so acct_ledger records them as reverse_opening_neutral
        (not opening_neutral), keeping them separate in the Performance breakdown:
          - winner leg: strategy="reverse_opening_neutral", fill_type="WINNER"
          - loser leg:  strategy="reverse_opening_neutral", fill_type="MAIN"
        """
        with self._lock:
            changed = False
            for token_id, fill_type in (
                (winner_token_id, "WINNER"),
                (loser_token_id,  "MAIN"),
            ):
                pos_id = self._token_index.get(token_id)
                if pos_id is None:
                    log.warning("acct: on_ron_exit — no position for token_id", token_id=token_id[:16])
                    continue
                pos = self._positions[pos_id]
                pos.strategy  = "reverse_opening_neutral"
                pos.fill_type = fill_type
                changed = True
            if changed:
                _save_positions(self._positions)
        log.info(
            "acct: RON exit — positions retagged as reverse_opening_neutral",
            winner_token_id=winner_token_id[:16],
            loser_token_id=loser_token_id[:16],
        )

    def add_fees(self, token_id: str, fees_usd: float, rebates_usd: float = 0.0) -> None:
        """Accumulate fees/rebates on an existing position."""
        with self._lock:
            pos_id = self._token_index.get(token_id)
            if pos_id is None:
                return
            pos = self._positions[pos_id]
            pos.fees_usd    = round(pos.fees_usd + fees_usd, 8)
            pos.rebates_usd = round(pos.rebates_usd + rebates_usd, 8)
            _save_positions(self._positions)

    # ── Background reconciliation ───────────────────────────────────────────────

    async def reconcile_loop(self, pm_client, interval_s: int = 120) -> None:
        """
        Background task.  Runs every `interval_s` seconds.

        Phases:
          1. PM /activity → confirm entry and exit fills by token_id.
          2. CLOB winner flag → confirm outcomes by condition_id.
          3. Error detection → flag CLOSING positions stuck > CONFIRM_TIMEOUT_S.

        Call this from main.py:
            asyncio.create_task(get_ledger().reconcile_loop(pm_client))
        """
        # Wire the singleton so _write_ledger_record can enrich rows from PM
        # /data/trades and detect on-chain balance divergence.
        set_pm_client(pm_client)
        await asyncio.sleep(30)   # let startup settle
        while True:
            try:
                await self._reconcile_once(pm_client)
            except Exception as exc:
                log.error("acct reconcile_loop error", exc=str(exc))
            await asyncio.sleep(interval_s)

    async def _reconcile_once(self, pm_client) -> None:
        positions_snap = list(self._positions.values())
        if not positions_snap:
            return

        if config.PAPER_TRADING:
            # PM /activity is irrelevant in paper mode; advance CLOSING →
            # PENDING_RESOLVE for any position that already has exit fills.
            # Phase 3 (winner-flag resolution) still runs — fetch_market_resolution
            # queries the PM CLOB winner flag directly and works in paper mode.
            self._advance_paper_positions()
            _pending_cids: set[str] = {
                p.market_id for p in self._positions.values()
                if p.status == PositionStatus.PENDING_RESOLVE and p.market_id
            }
            for _cid in _pending_cids:
                try:
                    _resolved_yes = await pm_client.fetch_market_resolution(_cid)
                    if _resolved_yes is not None:
                        self.on_resolved(_cid, _resolved_yes)
                except Exception as _exc:
                    log.debug("acct: resolution fetch failed", condition_id=_cid[:16], exc=str(_exc))
            return

        # ── Phase 1: PM /activity → confirm fills ─────────────────────────────
        # Collect unique token_ids that still need confirmation
        unconfirmed_tokens: set[str] = set()
        for pos in positions_snap:
            if not pos.pm_entry_confirmed and pos.token_id:
                unconfirmed_tokens.add(pos.token_id)
            if not pos.pm_exit_confirmed and pos.status not in (
                PositionStatus.LIVE, PositionStatus.ERROR
            ) and pos.token_id:
                unconfirmed_tokens.add(pos.token_id)
            # Also watch LIVE positions for manual sells — if the user sells a
            # bot-created position in PM without the bot placing the order, there
            # is no WS fill callback.  The reconcile loop detects the SELL event
            # here and records the actual exit price so the ledger is correct.
            if (pos.status == PositionStatus.LIVE
                    and pos.token_id
                    and not pos.pm_exit_confirmed):
                unconfirmed_tokens.add(pos.token_id)

        pm_activity_by_token: dict[str, list[dict]] = {}
        if unconfirmed_tokens:
            try:
                activity = await _fetch_pm_activity(pm_client)
                for row in activity:
                    asset = row.get("asset") or row.get("token_id") or ""
                    if asset in unconfirmed_tokens:
                        pm_activity_by_token.setdefault(asset, []).append(row)
            except Exception as exc:
                log.warning("acct: PM /activity fetch failed", exc=str(exc))

        # ── Phase 2: match fills and advance status ────────────────────────────
        changed = False
        with self._lock:
            for pos in self._positions.values():
                if pos.status in PositionStatus.TERMINAL:
                    continue

                rows_for_token = pm_activity_by_token.get(pos.token_id, [])

                # Confirm entry
                if not pos.pm_entry_confirmed:
                    for row in rows_for_token:
                        if (row.get("type", "").upper() == "TRADE"
                                and row.get("side", "").upper() == "BUY"):
                            pos.pm_entry_confirmed = True
                            changed = True
                            break

                # Confirm exit (SELL on CLOB or REDEEM)
                if not pos.pm_exit_confirmed and pos.status in (
                    PositionStatus.CLOSING, PositionStatus.PENDING_RESOLVE,
                    PositionStatus.LIVE,
                ):
                    for row in rows_for_token:
                        row_type = row.get("type", "").upper()
                        row_side = row.get("side", "").upper()
                        if row_type == "TRADE" and row_side == "SELL":
                            pos.pm_exit_confirmed = True
                            # Extract actual sell price from PM event.
                            # Covers both bot-placed exits (price already in
                            # exit_vwap via on_exit_fill) and manual sells
                            # (no prior on_exit_fill — we derive it here).
                            _usdc = float(row.get("usdcSize") or 0)
                            _size = float(row.get("size") or 0)
                            if _usdc > 0 and _size > 0 and pos.exit_contracts < 1e-9:
                                _sell_price = round(_usdc / _size, 8)
                                pos.exit_vwap = _vwap(
                                    pos.exit_vwap, pos.exit_contracts,
                                    _sell_price, _size,
                                )
                                pos.exit_contracts = round(pos.exit_contracts + _size, 8)
                                if not pos.exit_time:
                                    pos.exit_time = _now_iso()
                                if not pos.exit_type:
                                    pos.exit_type = "SELL"
                            # Manual sell on a LIVE bot position — transition to
                            # CLOSING so the standard resolution path can complete.
                            if pos.status == PositionStatus.LIVE:
                                pos.status = PositionStatus.CLOSING
                                pos.closing_since = pos.exit_time or _now_iso()
                                log.info(
                                    "acct: manual sell detected on LIVE bot position"
                                    " — advancing to CLOSING",
                                    pos_id=pos.pos_id[:12],
                                    market=pos.market_title[:50],
                                    side=pos.side,
                                    sell_price=pos.exit_vwap,
                                )
                            changed = True
                            break
                        if row_type == "REDEEM":
                            pos.pm_exit_confirmed = True
                            # Redemption: derive exit price from PM payout
                            payout = float(row.get("usdcSize") or 0)
                            size   = float(row.get("size") or pos.exit_contracts or 0)
                            if size > 0 and payout > 0:
                                redeem_price = round(payout / size, 8)
                                pos.exit_vwap = _vwap(
                                    pos.exit_vwap, pos.exit_contracts,
                                    redeem_price, size,
                                )
                                pos.exit_contracts = round(pos.exit_contracts + size, 8)
                                if not pos.exit_type:
                                    pos.exit_type = "REDEMPTION"
                                if not pos.exit_time:
                                    pos.exit_time = _now_iso()
                            changed = True
                            break

                # Advance CLOSING → PENDING_RESOLVE once exit confirmed
                if (pos.status == PositionStatus.CLOSING
                        and pos.pm_exit_confirmed):
                    pos.status = PositionStatus.PENDING_RESOLVE
                    changed = True

                # Error detection: CLOSING stuck for too long
                if pos.status == PositionStatus.CLOSING and pos.closing_since:
                    try:
                        since = datetime.fromisoformat(pos.closing_since)
                        if since.tzinfo is None:
                            since = since.replace(tzinfo=timezone.utc)
                        age_s = (datetime.now(timezone.utc) - since).total_seconds()
                        if age_s > _CONFIRM_TIMEOUT_S:
                            pos.status = PositionStatus.ERROR
                            log.error(
                                "acct: position stuck in CLOSING — marking ERROR",
                                pos_id=pos.pos_id[:12],
                                market=pos.market_title[:50],
                                side=pos.side,
                                age_s=int(age_s),
                            )
                            changed = True
                    except Exception:
                        pass

            if changed:
                _save_positions(self._positions)

        # ── Phase 3: resolve PENDING_RESOLVE positions ─────────────────────────
        pending_cids: set[str] = {
            p.market_id for p in self._positions.values()
            if p.status == PositionStatus.PENDING_RESOLVE and p.market_id
        }
        for cid in pending_cids:
            try:
                resolved_yes = await pm_client.fetch_market_resolution(cid)
                if resolved_yes is not None:
                    self.on_resolved(cid, resolved_yes)
            except Exception as exc:
                log.debug("acct: resolution fetch failed", condition_id=cid[:16], exc=str(exc))

    def _advance_paper_positions(self) -> None:
        """Paper mode: advance CLOSING → PENDING_RESOLVE immediately (no PM API)."""
        changed = False
        with self._lock:
            for pos in self._positions.values():
                if pos.status == PositionStatus.CLOSING and pos.exit_contracts > 0:
                    pos.status = PositionStatus.PENDING_RESOLVE
                    changed = True
            if changed:
                _save_positions(self._positions)

    # ── Query helpers ───────────────────────────────────────────────────────────

    def get_position(self, pos_id: str) -> Optional[AccountingPosition]:
        return self._positions.get(pos_id)

    def get_position_by_token(self, token_id: str) -> Optional[AccountingPosition]:
        pos_id = self._token_index.get(token_id)
        return self._positions.get(pos_id) if pos_id else None

    def get_positions_for_pair(self, pair_id: str) -> list[AccountingPosition]:
        return [p for p in self._positions.values() if p.pair_id == pair_id]

    def get_all_positions(self) -> list[AccountingPosition]:
        return list(self._positions.values())

    def get_open_positions(self) -> list[AccountingPosition]:
        return [p for p in self._positions.values()
                if p.status not in PositionStatus.TERMINAL]


# ── PM /activity fetch helper ─────────────────────────────────────────────────

async def _fetch_pm_activity(pm_client, max_rows: int = 2000) -> list[dict]:
    """
    Page through PM Data API /activity for the configured POLY_FUNDER address.

    Returns all rows up to max_rows.  Each row contains:
        conditionId, type (TRADE/REDEEM), side (BUY/SELL),
        asset (token_id), usdcSize, size, timestamp
    """
    funder = config.POLY_FUNDER
    if not funder:
        return []

    base = config.PM_DATA_API_URL.rstrip("/")
    rows: list[dict] = []
    limit = 500
    offset = 0

    while len(rows) < max_rows:
        url = f"{base}/activity?user={funder}&limit={limit}&offset={offset}"
        try:
            resp = await pm_client.get(url)
            if not resp:
                break
            batch = resp if isinstance(resp, list) else resp.get("data", [])
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        except Exception:
            break

    return rows


# ── Singleton accessor ─────────────────────────────────────────────────────────

_ledger: Optional[_Ledger] = None
_ledger_lock = threading.Lock()


def get_ledger() -> _Ledger:
    """Return the process-wide _Ledger singleton (created on first call)."""
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = _Ledger()
    return _ledger
