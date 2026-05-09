"""
strategies.ReverseOpenNeutral.scanner — Reverse Opening Neutral (Strategy 5b).

PAPER-ONLY EXPERIMENT: Mirrors every OpeningNeutral entry and simulates the
reverse exit (sell winner for TP, hold loser to resolution).  No real orders
are ever placed.  Results are recorded to data/ron_fills.csv for comparison
with on_fills.csv; join on the on_pair_id column.

Architecture:
  - RON does NOT scan markets independently.
  - It registers a callback with the live ON scanner instance via
    register_pair_callback().
  - When ON successfully registers a pair, RON creates paper Position objects
    at the same entry prices and begins bid-monitoring the same tokens.
  - When the loser bid drops to OPENING_NEUTRAL_LOSER_EXIT_TRIGGER, RON records
    the simulated winner TP price (best bid at trigger time) in ron_fills.csv.
  - No orders are placed; no risk engine writes are made.

Gate: config.REVERSE_OPENING_NEUTRAL_ENABLED must be True.
All ON config values (trigger, size, market types, min-hold) are shared.
"""
from __future__ import annotations

import asyncio
import csv
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
from logger import get_bot_logger
from risk import Position
from strategies.OpeningNeutral.scanner import OpeningNeutralScanner

log = get_bot_logger(__name__)

# ── RON fills CSV ─────────────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_RON_FILLS_CSV = _DATA_DIR / "ron_fills.csv"

# Schema version: 2.
# on_pair_id:          ON's pair_id — join key to on_fills.csv.
# winner_sold_price:   simulated fill price (= winner best_bid at trigger time).
# double_down_size:    additional simulated contracts on the loser (0 if disabled).
# double_down_price:   simulated ask price used for double-down (0 if disabled).
_RON_FILLS_HEADER = [
    "timestamp",
    "pair_id",
    "on_pair_id",            # ON's pair_id — join key to on_fills.csv
    "market_id",
    "market_title",
    "underlying",
    "market_type",
    "yes_entry",
    "no_entry",
    "combined_cost",
    "loser_leg",             # side that triggered (held to resolution)
    "loser_trigger_bid",     # bid price that fired the exit
    "winner_side",           # side that was sold (simulated)
    "winner_sold_price",     # simulated fill price (= winner best_bid at trigger)
    "winner_sold_time_secs", # seconds from entry to winner TP
    "double_down_size",      # additional simulated contracts on loser (0 if disabled)
    "double_down_price",     # simulated ask price for double-down (0 if disabled)
]


def _ensure_ron_fills_csv() -> None:
    """Create ron_fills.csv with header if it doesn't exist; back up on schema change."""
    _DATA_DIR.mkdir(exist_ok=True)
    if not _RON_FILLS_CSV.exists():
        with _RON_FILLS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_RON_FILLS_HEADER)
        return
    with _RON_FILLS_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            existing_header = next(reader)
        except StopIteration:
            existing_header = []
    if existing_header != _RON_FILLS_HEADER:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = _RON_FILLS_CSV.with_name(f"ron_fills_{ts}.csv.bak")
        _RON_FILLS_CSV.rename(backup)
        with _RON_FILLS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_RON_FILLS_HEADER)


def _write_ron_fills_row(row: dict) -> None:
    """Append one completed pair row to ron_fills.csv."""
    try:
        with _RON_FILLS_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(
                f, fieldnames=_RON_FILLS_HEADER, extrasaction="ignore"
            ).writerow(row)
    except Exception as exc:  # pylint: disable=broad-except
        import logging
        logging.getLogger(__name__).error("ron_fills.csv write failed", exc_info=exc)


# ── Scanner ───────────────────────────────────────────────────────────────────

class ReverseOpenNeutralScanner(OpeningNeutralScanner):
    """
    Paper-only experiment that mirrors every OpeningNeutral entry and simulates
    the reverse exit: sell winner for TP, hold loser to resolution.

    Coupled to ON via register_pair_callback().  Does not scan independently.
    No real orders are placed under any configuration.

    Gate: REVERSE_OPENING_NEUTRAL_ENABLED must be True.
    """

    def __init__(
        self,
        *args: Any,
        on_scanner: Optional[OpeningNeutralScanner] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_scanner: Optional[OpeningNeutralScanner] = on_scanner
        _ensure_ron_fills_csv()

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        status = super().get_status()
        status["enabled"] = getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False)
        return status

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
            return
        self._running = True
        _ensure_ron_fills_csv()
        # Hook into ON's entry so every ON pair is mirrored by RON.
        if self._on_scanner is not None:
            self._on_scanner.register_pair_callback(self._on_on_entry_received)
            # Fire RON's exit in lock-step with ON's loser exit — not independently.
            self._on_scanner.register_loser_exit_callback(self._notify_loser_exit)
        log.info("ReverseOpenNeutralScanner started (paper-only, coupled to ON)")

    async def stop(self) -> None:
        self._running = False

    # ── Scanning disabled (callback-driven entry only) ────────────────────────

    async def _refresh_pending_markets(self) -> None:
        pass  # RON does not scan independently

    async def _evaluate_entry(self, market: Any, _timer_fired: bool = False) -> None:
        pass  # RON does not self-evaluate

    # ── ON entry callback ─────────────────────────────────────────────────────

    async def _on_on_entry_received(
        self,
        market: Any,
        on_pair_id: str,
        yes_pos: "Position",
        no_pos: "Position",
    ) -> None:
        """
        Called by OpeningNeutralScanner after it successfully registers a pair.
        Creates paper Position objects at the same entry prices and arms
        bid-monitoring on both tokens.
        No orders are placed and no risk engine writes are made.
        """
        if not getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
            return

        pair_id      = f"ron_{uuid.uuid4().hex[:12]}"
        market_id    = getattr(market, "condition_id", "")
        yes_token_id = getattr(yes_pos, "token_id", "")
        no_token_id  = getattr(no_pos,  "token_id", "")

        # Paper positions — same prices as ON, tagged as reverse_opening_neutral.
        # Not registered in the risk engine (paper-only; avoids key collision with
        # ON's live positions on the same market_id:side slot).
        ron_yes = Position(
            market_id=market_id,
            market_type=getattr(yes_pos, "market_type", ""),
            underlying=getattr(yes_pos, "underlying", ""),
            side="YES",
            size=yes_pos.size,
            entry_price=yes_pos.entry_price,
            entry_cost_usd=yes_pos.entry_cost_usd,
            strategy="reverse_opening_neutral",
            token_id=yes_token_id,
            market_title=getattr(market, "title", ""),
            order_id=f"ron_{uuid.uuid4().hex[:8]}",
            spread_id=pair_id,
            neutral_pair_id=pair_id,
            tte_years=getattr(yes_pos, "tte_years", 0.0),
            spot_price=getattr(yes_pos, "spot_price", 0.0),
            strike=getattr(yes_pos, "strike", 0.0),
        )
        ron_no = Position(
            market_id=market_id,
            market_type=getattr(no_pos, "market_type", ""),
            underlying=getattr(no_pos, "underlying", ""),
            side="NO",
            size=no_pos.size,
            entry_price=no_pos.entry_price,
            entry_cost_usd=no_pos.entry_cost_usd,
            strategy="reverse_opening_neutral",
            token_id=no_token_id,
            market_title=getattr(market, "title", ""),
            order_id=f"ron_{uuid.uuid4().hex[:8]}",
            spread_id=pair_id,
            neutral_pair_id=pair_id,
            tte_years=getattr(no_pos, "tte_years", 0.0),
            spot_price=getattr(no_pos, "spot_price", 0.0),
            strike=getattr(no_pos, "strike", 0.0),
        )

        self._active_pairs[pair_id] = {
            "market_id":         market_id,
            "market_title":      getattr(market, "title", "")[:80],
            "yes_pos":           ron_yes,
            "no_pos":            ron_no,
            "yes_exit_order_id": "",
            "no_exit_order_id": "",
            "entry_ts":          time.time(),
        }
        self._pair_csv_data[pair_id] = {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "pair_id":      pair_id,
            "on_pair_id":   on_pair_id,
            "market_id":    market_id,
            "market_title": getattr(market, "title", "")[:80],
            "underlying":   getattr(yes_pos, "underlying", ""),
            "market_type":  getattr(yes_pos, "market_type", ""),
            "yes_entry":    yes_pos.entry_price,
            "no_entry":     no_pos.entry_price,
            "combined_cost": round(yes_pos.entry_price + no_pos.entry_price, 6),
            "_entry_ts":    time.time(),
        }

        # Arm bid-monitoring: same token_ids as ON's pair.
        self._token_to_pair[yes_token_id] = pair_id
        self._token_to_pair[no_token_id]  = pair_id

        log.info(
            "ReverseOpenNeutral: paper pair created (mirroring ON entry)",
            on_pair_id=on_pair_id[:12],
            ron_pair_id=pair_id[:12],
            market=getattr(market, "title", "")[:60],
            yes_entry=ron_yes.entry_price,
            no_entry=ron_no.entry_price,
        )

    # ── ON loser-exit notification (fires in lock-step with ON) ──────────────

    async def _notify_loser_exit(
        self,
        on_pair_id: str,
        loser_side: str,
        exit_price: float,
    ) -> None:
        """
        Called by ON's _on_exit_fill via register_loser_exit_callback().
        Looks up the matching RON pair by on_pair_id and fires the paper exit
        at exactly the same moment ON's real exit fills.
        """
        if not getattr(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
            return

        # Find the RON pair that mirrors this ON pair.
        ron_pair_id = next(
            (pid for pid, csv_row in self._pair_csv_data.items()
             if csv_row.get("on_pair_id") == on_pair_id),
            None,
        )
        if ron_pair_id is None:
            return  # pair already exited or never registered

        pair = self._active_pairs.get(ron_pair_id)
        if pair is None:
            return

        loser_pos: Optional[Position] = pair.get(f"{loser_side.lower()}_pos")
        if loser_pos is None:
            return

        loser_token_id = getattr(loser_pos, "token_id", "")
        if loser_token_id in self._exiting_legs:
            return  # already firing

        self._exiting_legs.add(loser_token_id)
        asyncio.create_task(
            self._execute_loser_exit(ron_pair_id, loser_side, loser_token_id, loser_pos, exit_price),
            name=f"ron_exit_{ron_pair_id[:12]}",
        )

    # ── Exit logic (paper simulation) ─────────────────────────────────────────

    async def _execute_loser_exit(
        self,
        pair_id: str,
        side: str,         # loser side (whose bid dropped)
        token_id: str,     # loser token_id (for _exiting_legs guard)
        pos: "Position",   # loser paper Position (not in risk engine)
        trigger_bid: float,
    ) -> None:
        """
        Paper simulation of the reverse exit:
          - Simulated winner TP price = winner's best bid at trigger time.
          - Optional double-down: simulate additional buy on loser.
          - Write ron_fills.csv row.
          - No real orders placed; no risk engine writes.
        """
        pair = self._active_pairs.get(pair_id)
        if pair is None:
            self._exiting_legs.discard(token_id)
            return

        winner_side:     str      = "NO" if side == "YES" else "YES"
        winner_pos:      Position = pair["no_pos"] if side == "YES" else pair["yes_pos"]
        winner_token_id: str      = getattr(winner_pos, "token_id", "")

        if not winner_token_id:
            log.error(
                "ReverseOpenNeutral: winner token_id missing — aborting paper exit",
                pair_id=pair_id[:12],
                loser_side=side,
            )
            self._exiting_legs.discard(token_id)
            return

        # Simulated winner exit price = best bid on winner at trigger time.
        winner_book = self._pm.get_book(winner_token_id)
        winner_exit_price: float = (
            winner_book.best_bid
            if winner_book is not None and winner_book.best_bid is not None
            else round(1.0 - trigger_bid, 4)
        )

        log.info(
            "ReverseOpenNeutral: paper exit — recording simulated winner TP",
            pair_id=pair_id[:12],
            loser_side=side,
            winner_side=winner_side,
            simulated_winner_price=round(winner_exit_price, 4),
            loser_trigger_bid=round(trigger_bid, 4),
        )

        # ── Double-down simulation ─────────────────────────────────────────────
        dd_usd: float   = getattr(config, "RON_DOUBLE_DOWN_USD", 0.0)
        dd_size: float  = 0.0
        dd_price: float = 0.0
        if dd_usd > 0:
            loser_token_id = getattr(pos, "token_id", "")
            loser_book     = self._pm.get_book(loser_token_id) if loser_token_id else None
            dd_price       = (
                loser_book.best_ask
                if loser_book is not None and loser_book.best_ask is not None
                else round(trigger_bid + 0.01, 4)
            )
            dd_size = round(dd_usd / dd_price, 6) if dd_price > 0 else 0.0
            log.info(
                "ReverseOpenNeutral: paper double-down simulated",
                loser_side=side,
                dd_usd=dd_usd,
                dd_size=dd_size,
                dd_price=round(dd_price, 4),
            )

        # ── Write ron_fills.csv row ────────────────────────────────────────────
        _csv_row = self._pair_csv_data.pop(pair_id, {})
        _entry_ts: float = _csv_row.pop("_entry_ts", time.time())
        _csv_row.update({
            "loser_leg":             side,
            "loser_trigger_bid":     round(trigger_bid, 4),
            "winner_side":           winner_side,
            "winner_sold_price":     round(winner_exit_price, 4),
            "winner_sold_time_secs": round(time.time() - _entry_ts, 1),
            "double_down_size":      dd_size,
            "double_down_price":     round(dd_price, 4),
        })
        _write_ron_fills_row(_csv_row)

        # ── Pair cleanup ───────────────────────────────────────────────────────
        yes_pos: Optional[Position] = pair.get("yes_pos")
        no_pos:  Optional[Position] = pair.get("no_pos")
        for _p in (yes_pos, no_pos):
            if _p is not None:
                _tid = getattr(_p, "token_id", "")
                if _tid:
                    self._token_to_pair.pop(_tid, None)
                    self._exiting_legs.discard(_tid)

        self._active_pairs.pop(pair_id, None)

        if self._on_close_callback is not None:
            self._on_close_callback(pair.get("market_id", ""))

    # ── Winner-closed notification: no-op ─────────────────────────────────────

    def notify_winner_closed(
        self, market_id: str, side: str, exit_price: float
    ) -> None:
        """No-op — reverse strategy records winner exit at TP time."""
