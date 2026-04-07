"""
market_data/spot_oracle.py — Unified spot oracle facade.

Routes get_mid / get_spot / get_spot_age calls to the correct price source
based on market type and underlying, matching Polymarket's actual settlement logic.

    bucket_5m / bucket_15m / bucket_4h, all coins   →  freshest of RTDSClient chainlink relay
                                                        + ChainlinkWSClient (HTTP-seeded fallback)
    all other bucket types                           →  RTDSClient (exchange-aggregated)

Oracle sources by coin × market type:
  - ALL coins on 5m/15m/4h: RTDSClient crypto_prices_chainlink relay is the primary
    live feed (Polymarket's own Chainlink Data Streams push, ~1 tick/sec per coin).
    ChainlinkWSClient provides an HTTP-seeded price baseline (latestRoundData() on
    startup) that fills in until RTDS chainlink delivers its first tick, and
    thereafter acts as a warm backup.  The fresher timestamp always wins.
  - HYPE on 5m/15m/4h additionally races against ChainlinkStreamsClient when keys
    are configured (direct Data Streams WebSocket, sub-millisecond latency).
  - All coins on 1h/daily/weekly: RTDSClient exchange-aggregated prices.

Note: ChainlinkWSClient's eth_subscribe path is unreliable on public RPC endpoints
(publicnode delivers zero log events).  RTDS chainlink relay is the confirmed working
primary for all Chainlink-oracle coins.

Callers register callbacks via:
  on_chainlink_update(cb)   — fires on every event from ChainlinkWSClient (non-HYPE
                              Chainlink coins) AND RTDSClient chainlink relay (HYPE)
                              AND ChainlinkStreamsClient (HYPE direct stream).
  on_rtds_update(cb)        — fires on every RTDS exchange-aggregated tick.

CHAINLINK_MARKET_TYPES is the single canonical definition of which market types
use the Chainlink oracle.  Import it here; do not import from rtds_client.
"""
from __future__ import annotations

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

    # ── Chainlink dual-feed arbiter (all coins) ───────────────────────────────

    def _get_chainlink_spot(self, coin: str) -> Optional[SpotPrice]:
        """Chainlink price for `coin` from live WebSocket feeds only.

        Sources (freshest timestamp wins):
          - RTDSClient crypto_prices_chainlink relay — primary for all coins.
          - ChainlinkStreamsClient — HYPE only, races with RTDS relay.
        """
        rtds_snap = self._rtds.get_chainlink_spot(coin)
        streams_snap = (
            self._streams.get_spot(coin) if self._streams and coin == "HYPE" else None
        )
        if rtds_snap is not None and streams_snap is not None:
            return rtds_snap if rtds_snap.timestamp >= streams_snap.timestamp else streams_snap
        return rtds_snap if rtds_snap is not None else streams_snap

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_chainlink_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register callback(coin, price) fired on every Chainlink oracle event.

        Fires on:
          - AnswerUpdated events for BTC/ETH/SOL/XRP/BNB/DOGE (ChainlinkWSClient)
          - crypto_prices_chainlink RTDS relay for HYPE (RTDSClient)
          - Direct Data Streams reports for HYPE (ChainlinkStreamsClient, if enabled)
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

