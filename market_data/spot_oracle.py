"""
market_data/spot_oracle.py — Unified spot oracle facade.

Routes get_mid / get_spot / get_spot_age calls to the correct price source
based on market type and underlying, matching Polymarket's actual settlement logic.

    bucket_5m / bucket_15m / bucket_4h
      all coins →  ChainlinkStreamsClient direct feed  (primary, ~190ms ahead of relay)
                   RTDSClient crypto_prices_chainlink   (fallback if direct unavailable)
                   ChainlinkWSClient on-chain Polygon   (last-resort fallback)
    all other bucket types  →  RTDSClient exchange-aggregated

Oracle sources by coin × market type:
  - All coins on 5m/15m/4h: priority order:
      1. ChainlinkStreamsClient — direct Chainlink Data Streams WebSocket, ~190ms ahead
         of the RTDS relay for all seven coins.  Requires CHAINLINK_DS_* env vars.
         Gracefully disabled (falls through to RTDS) if not configured.
      2. RTDSClient crypto_prices_chainlink relay — Polymarket's own Data Streams
         push, ~1 tick/sec.  No API key required.  Used when direct is unavailable.
      3. ChainlinkWSClient — direct on-chain Polygon AnswerUpdated events.  HTTP-seeded
         on start; WS events require a paid Polygon RPC (POLYGON_WS_URL).  Last resort.
  - All coins on 1h/daily/weekly: RTDSClient exchange-aggregated prices.

Callers register callbacks via:
  on_chainlink_update(cb)   — fires on every event from ChainlinkWSClient (non-HYPE
                              Chainlink coins) AND RTDSClient chainlink relay (all
                              coins) AND ChainlinkStreamsClient (HYPE direct stream).
  on_rtds_update(cb)        — fires on every RTDS exchange-aggregated tick.

CHAINLINK_MARKET_TYPES is the single canonical definition of which market types
use the Chainlink oracle.  Import it here; do not import from rtds_client.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Coroutine, Optional

from market_data.chainlink_streams_client import ChainlinkStreamsClient
from market_data.chainlink_ws_client import ChainlinkWSClient
from market_data.oracle_tick_log import (
    make_logging_callback,
    SOURCE_CHAINLINK_WS,
    SOURCE_RTDS_CHAINLINK,
    SOURCE_CHAINLINK_STREAMS,
    SOURCE_RTDS,
)
from market_data.rtds_client import RTDSClient, SpotPrice

# Market types whose resolution oracle is Chainlink (on-chain AggregatorV3 for non-HYPE;
# Data Streams for HYPE).  All other bucket types use RTDS exchange-aggregated feed.
CHAINLINK_MARKET_TYPES: frozenset[str] = frozenset({
    "bucket_5m",
    "bucket_15m",
    "bucket_4h",
})


class SpotOracle:
    """
    Unified routing facade over RTDSClient, ChainlinkWSClient, and ChainlinkStreamsClient.

    Instantiate once in main() and pass to PositionMonitor / MomentumScanner.
    MakerStrategy, VolFetcher, and state_sync_loop continue to use RTDSClient
    directly for their RTDS-only needs.
    """

    def __init__(
        self,
        rtds: RTDSClient,
        chainlink: ChainlinkWSClient,
        streams: Optional[ChainlinkStreamsClient] = None,
    ) -> None:
        self._rtds = rtds
        self._cl = chainlink
        self._streams = streams

    # ── Oracle-routing accessors ──────────────────────────────────────────────

    def get_mid(self, underlying: str, market_type: str) -> Optional[float]:
        """Return the oracle-correct mid price for `underlying` given `market_type`.

        Returns None if the relevant feed has not yet received a price.
        No silent fallback between oracles — silence is the correct signal of
        data unavailability for short-duration markets.
        """
        if market_type in CHAINLINK_MARKET_TYPES:
            snap = self._get_chainlink_spot(underlying)
            return snap.price if snap is not None else None
        return self._rtds.get_mid(underlying)

    def get_spot(self, underlying: str, market_type: str) -> Optional[SpotPrice]:
        """Return the oracle-correct SpotPrice snapshot for `underlying`."""
        if market_type in CHAINLINK_MARKET_TYPES:
            return self._get_chainlink_spot(underlying)
        return self._rtds.get_spot(underlying)

    def get_spot_age(self, underlying: str, market_type: str) -> float:
        """Seconds since the last oracle update for `underlying`; inf if never received."""
        if market_type in CHAINLINK_MARKET_TYPES:
            snap = self._get_chainlink_spot(underlying)
            return time.time() - snap.timestamp if snap is not None else float("inf")
        return self._rtds.get_spot_age(underlying)

    def get_mid_resolution_oracle(
        self, underlying: str, market_type: str
    ) -> Optional[float]:
        """AggregatorV3-only price for near-expiry resolution matching.

        Polymarket's resolution contract calls latestRoundData() on the on-chain
        Polygon AggregatorV3 at expiry.  Near-expiry, the on-chain AggregatorV3
        price is a better predictor of the settlement outcome than the RTDS relay
        (which leads AggregatorV3 by up to ~15 s and can differ at a round boundary).

        Returns the ChainlinkWSClient price only (no RTDS relay, no Streams relay)
        for Chainlink market types on non-HYPE coins.  Falls back to standard
        get_mid() for HYPE (no Polygon AggregatorV3) and non-Chainlink types.

        NOTE: 1h / daily / weekly markets resolve against Binance OHLC candle data
        (close vs open), not Chainlink.  For these market types this method falls
        through to get_mid() which returns the RTDS exchange-aggregated price — the
        closest available proxy for Binance spot.
        """
        if market_type in CHAINLINK_MARKET_TYPES and underlying != "HYPE":
            snap = self._cl.get_spot(underlying)
            return snap.price if snap is not None else None
        return self.get_mid(underlying, market_type)

    # ── Chainlink dual-feed arbiter (all coins) ───────────────────────────────

    def _get_chainlink_spot(self, coin: str) -> Optional[SpotPrice]:
        """Chainlink price for `coin` — direct feed primary, RTDS relay fallback.

        Priority order:
          1. ChainlinkStreamsClient — direct Data Streams WS, ~190ms ahead of relay
             for all seven coins.  Used whenever a snapshot is available.
          2. RTDSClient relay     — Polymarket's crypto_prices_chainlink push.
             Used when direct feed has not yet delivered a price.
          3. ChainlinkWSClient   — on-chain Polygon AggregatorV3 (non-HYPE).
             Last-resort fallback; seed timestamp ages out quickly without a
             paid Polygon WS endpoint.
        """
        # 1. Direct Data Streams — primary for all coins (API key required).
        if self._streams is not None:
            streams_snap = self._streams.get_spot(coin)
            if streams_snap is not None:
                return streams_snap

        # 2. RTDS relay — fallback when direct is unavailable.
        rtds_snap = self._rtds.get_chainlink_spot(coin)
        if rtds_snap is not None:
            return rtds_snap

        # 3. On-chain Polygon AggregatorV3 — last resort (non-HYPE only).
        if coin != "HYPE":
            return self._cl.get_spot(coin)
        return None

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_chainlink_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register callback(coin, price) fired on every Chainlink oracle event.

        Fires on:
          - AnswerUpdated events for BTC/ETH/SOL/XRP/BNB/DOGE (ChainlinkWSClient)
          - crypto_prices_chainlink RTDS relay for all coins including HYPE (RTDSClient)
          - Direct Data Streams reports for all coins (ChainlinkStreamsClient, if enabled)
        """
        self._cl.on_price_update(callback)
        self._rtds.on_chainlink_update(callback)
        if self._streams is not None:
            self._streams.on_price_update(callback)

    def on_rtds_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register callback(coin, price) fired on every RTDS exchange-aggregated tick."""
        self._rtds.on_price_update(callback)

    # ── Oracle tick logging ─────────────────────────────────────────────

    def enable_oracle_tick_log(self) -> None:
        """Register append-only CSV logging on all four oracle channels.

        Writes every oracle price update to data/oracle_ticks.csv with a
        per-source label.  Use for:
          • Live feed-liveness monitoring (oracle connectivity without open positions).
          • Post-trade analysis: join on (ts, coin) against momentum_ticks.csv to
            see the exact oracle path from position entry through to resolution,
            confirm which feed delivered the strike price, and measure inter-feed
            latency (rtds_chainlink vs chainlink_streams).

        Call once after instantiation in main():
            spot_oracle.enable_oracle_tick_log()
        """
        self._cl.on_price_update(make_logging_callback(SOURCE_CHAINLINK_WS))
        self._rtds.on_chainlink_update(make_logging_callback(SOURCE_RTDS_CHAINLINK))
        if self._streams is not None:
            self._streams.on_price_update(make_logging_callback(SOURCE_CHAINLINK_STREAMS))
        self._rtds.on_price_update(make_logging_callback(SOURCE_RTDS))

    # ── RTDS dashboard conveniences (used by state_sync_loop and data quality) ─

    def all_mids(self) -> dict[str, float]:
        """RTDS exchange-aggregated coin → price.  Used for dashboard display."""
        return self._rtds.all_mids()

    def get_spot_age_rtds(self, coin: str) -> float:
        """RTDS spot age for `coin`.  Used for dashboard data-quality display."""
        return self._rtds.get_spot_age(coin)

    @property
    def tracked_coins(self) -> set[str]:
        """Coins tracked by the RTDS feed."""
        return self._rtds.tracked_coins

    # ── Chainlink health conveniences (used by state_sync_loop) ──────────────

    @property
    def chainlink_streams_connected(self) -> bool:
        """True if the ChainlinkStreamsClient WebSocket is currently open."""
        return self._streams.is_connected if self._streams is not None else False

    @property
    def chainlink_ws_connected(self) -> bool:
        """True if the ChainlinkWSClient WebSocket is currently open."""
        return self._cl.is_connected

    def get_chainlink_ages_s(self, coins: list[str]) -> dict[str, float | None]:
        """Per-coin age (seconds) of the best available Chainlink oracle price.

        Uses the same priority order as _get_chainlink_spot(): Streams → RTDS
        relay → on-chain WS.  Returns None instead of inf for coins with no data
        so the result is safely JSON-serialisable.
        """
        result: dict[str, float | None] = {}
        for coin in coins:
            age = self.get_spot_age(coin, "bucket_5m")
            result[coin] = None if math.isinf(age) else round(age, 1)
        return result

    def get_chainlink_mids(self, coins: list[str]) -> dict[str, float | None]:
        """Per-coin mid price from the best available Chainlink oracle source."""
        return {coin: self.get_mid(coin, "bucket_5m") for coin in coins}


