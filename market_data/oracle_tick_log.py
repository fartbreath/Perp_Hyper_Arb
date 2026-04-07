"""
market_data/oracle_tick_log.py — Lightweight append-only oracle tick recorder.

Writes every oracle price update from every source to data/oracle_ticks.csv.
Runs unconditionally — not just when positions are open — so you get:

  1. A continuous feed-liveness record for connectivity debugging.
  2. A per-source price time series for post-trade analysis:
     Given any open position (from momentum_ticks.csv or bot.log), you can
     join on (ts, coin) to see the full oracle path from entry to resolution,
     including which feed was fastest and by how many milliseconds.

CSV columns:
  ts            — UTC ISO-8601 timestamp (local event-receipt time, not on-chain)
  coin          — BTC / ETH / SOL / … / HYPE
  source        — "chainlink_ws"     : ChainlinkWSClient AnswerUpdated event (Polygon AggregatorV3)
                  "rtds_chainlink"   : RTDS crypto_prices_chainlink relay (Polymarket → CL Data Streams)
                  "chainlink_streams": ChainlinkStreamsClient direct Data Streams WebSocket
                  "rtds"             : RTDSClient crypto_prices (exchange-aggregated)
  price         — oracle price in USD

Usage — call enable_oracle_tick_log() on SpotOracle after instantiation in main():

    spot_oracle = SpotOracle(spot_client, chainlink_ws, chainlink_streams)
    spot_oracle.enable_oracle_tick_log()     # <-- enables the recorder

The CSV is append-only.  Rotate / archive it externally if needed.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Coroutine

from logger import get_bot_logger

log = get_bot_logger(__name__)

ORACLE_TICKS_CSV = Path(__file__).parent.parent / "data" / "oracle_ticks.csv"

_HEADER = ["ts", "coin", "source", "price"]

_SOURCE_CHAINLINK_WS      = "chainlink_ws"
_SOURCE_RTDS_CHAINLINK    = "rtds_chainlink"
_SOURCE_CHAINLINK_STREAMS = "chainlink_streams"
_SOURCE_RTDS              = "rtds"


def _ensure_csv() -> None:
    ORACLE_TICKS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not ORACLE_TICKS_CSV.exists():
        with ORACLE_TICKS_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_HEADER).writeheader()


def _write_tick(coin: str, source: str, price: float) -> None:
    try:
        _ensure_csv()
        row = {
            "ts":     datetime.now(timezone.utc).isoformat(),
            "coin":   coin,
            "source": source,
            "price":  price,
        }
        with ORACLE_TICKS_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_HEADER).writerow(row)
    except Exception as exc:
        # Never let tick logging crash the bot.
        log.debug("oracle_tick_log: write failed", exc=str(exc))


def make_logging_callback(source: str) -> Callable[[str, float], Coroutine]:
    """Return an async callback(coin, price) that appends one row to oracle_ticks.csv.

    ``source`` is one of the _SOURCE_* constants above.
    """
    async def _cb(coin: str, price: float) -> None:
        _write_tick(coin, source, price)
    return _cb


# Public source labels — importable by SpotOracle and tests.
SOURCE_CHAINLINK_WS      = _SOURCE_CHAINLINK_WS
SOURCE_RTDS_CHAINLINK    = _SOURCE_RTDS_CHAINLINK
SOURCE_CHAINLINK_STREAMS = _SOURCE_CHAINLINK_STREAMS
SOURCE_RTDS              = _SOURCE_RTDS
