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
    "exit_vwap",  "exit_contracts",  "exit_time",     "exit_type",
    "spot_entry", "spot_exit", "strike", "tte_seconds",
    "resolve_price", "resolved_outcome",
    "gross_pnl", "fees_usd", "rebates_usd", "net_pnl",
    "pm_entry_confirmed", "pm_exit_confirmed",
    "signal_source", "signal_score",
    "reconciliation_notes",
]

# How many seconds to wait after the first exit fill before declaring ERROR.
_CONFIRM_TIMEOUT_S: int = 600  # 10 minutes


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
    fill_type:       str   # MAIN | LOSER_EXIT | WINNER | HEDGE

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


def _write_ledger_record(pos: AccountingPosition) -> None:
    """Flatten AccountingPosition into a single LedgerRecord and append."""
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
        "reconciliation_notes": "",
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
        fill_type:    str = "MAIN",    # MAIN | LOSER_EXIT | WINNER | HEDGE
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
            if pos.status == PositionStatus.LIVE:
                pos.status       = PositionStatus.CLOSING
                pos.closing_since = now

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

                # For RESOLVED exits where no exit fill was recorded yet
                # (position went straight to redemption), fill in exit at settlement
                if pos.exit_contracts == 0.0:
                    settlement = 1.0 if token_won else 0.0
                    pos.exit_vwap      = settlement
                    pos.exit_contracts = pos.entry_contracts
                    pos.exit_time      = _now_iso()
                    pos.exit_type      = "RESOLVED"

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
            # PM /activity is irrelevant in paper mode; just advance state
            # for positions that have exit fills.
            self._advance_paper_positions()
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
                    PositionStatus.CLOSING, PositionStatus.PENDING_RESOLVE
                ):
                    for row in rows_for_token:
                        row_type = row.get("type", "").upper()
                        row_side = row.get("side", "").upper()
                        if row_type == "TRADE" and row_side == "SELL":
                            pos.pm_exit_confirmed = True
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
