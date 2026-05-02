"""
strategies.Momentum.win_rate — Empirical win-rate lookup table for Phase E gate.

Loads historical momentum fills (data/momentum_fills.csv) and resolved trades
(data/trades.csv) — joins on market_id — and computes an empirical win rate
per (market_type, price_band_5ct, tte_bin_60s) bucket.

Win-rate gate usage
--------------------
    from strategies.Momentum.win_rate import WinRateTable
    tbl = WinRateTable()
    emp_wr = tbl.get("bucket_5m", token_price=0.82, tte_seconds=55.0)
    # Returns float in [0, 1] if bucket has enough samples, else None.

Bucket dimensions
-----------------
  market_type  : bucket_5m | bucket_15m | bucket_1h | bucket_4h | …
  price_band   : floor(token_price * 20) / 20  → bands of 0.05 width
                 e.g. 0.80 covers [0.80, 0.85)
  tte_bin_60s  : floor(tte_seconds / 60) * 60  → 60-second TTE bins
                 e.g. 60 covers [60, 120)

Outcome definition
------------------
  WIN  : trades.csv resolved_outcome == "WIN"
  LOSS : trades.csv resolved_outcome == "LOSS"
  Rows with empty resolved_outcome (early exit, paper mode, unknown) are excluded
  from the win-rate computation to avoid polluting the table with noise.

Minimum sample guard
--------------------
  10 fills are required per bucket before the win rate is considered reliable
  enough to gate on.  Below this threshold, get() returns None (gate is open).
"""
from __future__ import annotations

import csv
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_FILLS_CSV = _DATA_DIR / "momentum_fills.csv"
_TRADES_CSV = _DATA_DIR / "trades.csv"

# Price band width (5 cent buckets covering [0.60, 0.95])
_PRICE_BAND_WIDTH = 0.05
# TTE bin width in seconds (60-second bins)
_TTE_BIN_SECS = 60


def _price_band(price: float) -> float:
    """Return lower bound of the 5-cent price band containing `price`."""
    return math.floor(price / _PRICE_BAND_WIDTH) * _PRICE_BAND_WIDTH


def _tte_bin(tte_seconds: float) -> int:
    """Return lower bound of the 60-second TTE bin containing `tte_seconds`."""
    return int(math.floor(max(tte_seconds, 0) / _TTE_BIN_SECS)) * _TTE_BIN_SECS


class WinRateTable:
    """Empirical win-rate lookup table built from historical fills + trades.

    Thread-safe for read access (GIL).  Reload by constructing a new instance.
    """

    def __init__(self) -> None:
        # Internal: {bucket_key -> [wins, total]}
        # bucket_key = (market_type, price_band, tte_bin)
        self._table: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        self._load()

    def _load(self) -> None:
        """Load fills + trades and populate the win-rate table."""
        if not _FILLS_CSV.exists() or not _TRADES_CSV.exists():
            return

        # Step 1: build market_id → resolved_outcome from trades.csv
        outcomes: dict[str, str] = {}
        try:
            with _TRADES_CSV.open("r", newline="") as f:
                for row in csv.DictReader(f):
                    mkt_id = row.get("market_id", "").strip()
                    outcome = row.get("resolved_outcome", "").strip()
                    if mkt_id and outcome in ("WIN", "LOSS"):
                        # Keep the first resolved outcome per market_id
                        # (trades.csv has one row per close).
                        outcomes.setdefault(mkt_id, outcome)
        except Exception as exc:
            log.debug("WinRateTable: failed to load trades.csv", exc=str(exc))
            return

        if not outcomes:
            return

        # Step 2: read fills and build the win-rate table
        filled_count = 0
        try:
            with _FILLS_CSV.open("r", newline="") as f:
                for row in csv.DictReader(f):
                    # Skip GTD hedge placement rows — they are not signal-quality fills.
                    if row.get("row_type", "entry") == "hedge":
                        continue

                    mkt_id = row.get("market_id", "").strip()
                    outcome = outcomes.get(mkt_id)
                    if outcome is None:
                        # No resolved outcome — skip row.
                        continue

                    market_type = row.get("market_type", "").strip()
                    if not market_type:
                        continue

                    try:
                        fill_price = float(row.get("fill_price") or row.get("signal_price") or 0)
                        tte_secs = float(row.get("tte_seconds") or 0)
                    except (ValueError, TypeError):
                        continue

                    if fill_price <= 0 or tte_secs < 0:
                        continue

                    key = (market_type, _price_band(fill_price), _tte_bin(tte_secs))
                    bucket = self._table[key]
                    bucket[1] += 1  # total
                    if outcome == "WIN":
                        bucket[0] += 1  # wins

                    filled_count += 1
        except Exception as exc:
            log.debug("WinRateTable: failed to load fills", exc=str(exc))
            return

        log.info(
            "WinRateTable: loaded",
            fills_joined=filled_count,
            buckets=len(self._table),
        )

    def get(
        self,
        market_type: str,
        token_price: float,
        tte_seconds: float,
    ) -> Optional[float]:
        """Return empirical win rate for the (market_type, price_band, tte_bin) bucket.

        Returns None if the bucket has fewer than
        config.MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES samples (gate stays open).
        """
        key = (market_type, _price_band(token_price), _tte_bin(tte_seconds))
        bucket = self._table.get(key)
        if bucket is None:
            return None
        wins, total = bucket
        min_samples = getattr(config, "MOMENTUM_WIN_RATE_GATE_MIN_SAMPLES", 10)
        if total < min_samples:
            return None
        return wins / total

    def summary(self) -> dict:
        """Return a summary of non-empty buckets for diagnostics / logging."""
        rows = []
        for (market_type, pb, tb), (wins, total) in sorted(self._table.items()):
            rows.append({
                "market_type": market_type,
                "price_band": round(pb, 2),
                "tte_bin_s": tb,
                "wins": wins,
                "total": total,
                "win_rate": round(wins / total, 4) if total > 0 else None,
            })
        return {"buckets": rows, "total_fills": sum(b[1] for b in self._table.values())}
