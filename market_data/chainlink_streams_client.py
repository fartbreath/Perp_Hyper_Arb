"""
market_data/chainlink_streams_client.py — Direct Chainlink Data Streams WebSocket.

Connects to the Chainlink Data Streams engine WebSocket and streams HYPE/USD
reports directly from the oracle network — zero intermediary, zero polling.

This is what high-frequency bots and oracle-lag-sniper strategies use.
It delivers each HYPE price report within milliseconds of the oracle network
reaching consensus, before any intermediary (including Polymarket itself) has
processed the update.

Architecture:
  • One persistent WebSocket per feed (this client manages HYPE/USD only).
  • HMAC-SHA256 authentication using a free Chainlink-sponsored API key
    (obtain in ~30 seconds at https://pm-ds-request.streams.chain.link/).
  • Reconnects with exponential backoff (1s → 60s); reseeds nothing on
    reconnect since the very next oracle report provides fresh ground truth.
  • Gracefully disabled if CHAINLINK_DS_API_KEY is not configured — bot runs
    on RTDS crypto_prices_chainlink topic (Polymarket's own relay) instead.

Required environment variables:
  CHAINLINK_DS_API_KEY        — Chainlink sponsored API key
  CHAINLINK_DS_API_SECRET     — Corresponding HMAC signing secret
  CHAINLINK_DS_HYPE_FEED_ID   — Feed ID for HYPE/USD (provided with key)

WebSocket endpoint:
  wss://ws.dataengine.chain.link/api/v1/ws?feedIDs=<feed_id>

Authentication (HTTP upgrade headers per Chainlink spec):
  CHAINLINK-DS-APIKEY:    <api_key>
  CHAINLINK-DS-TIMESTAMP: <unix_ms>
  CHAINLINK-DS-SIGNATURE: hex(HMAC-SHA256(api_secret, "GET\\n{path}\\n{timestamp_ms}"))
  where path = /api/v1/ws?feedIDs=<feed_id>

Data Streams V3 Report schema (all ABI-encoded, 32 bytes per slot):
  Prefix 32 bytes: report context (schema version + chain identifiers)
  [0]  bytes32  feedId
  [1]  uint32   validFromTimestamp
  [2]  uint32   observationsTimestamp  (unix seconds — used as oracle timestamp)
  [3]  uint192  nativeFee
  [4]  uint192  linkFee
  [5]  uint32   expiresAt
  [6]  int192   benchmarkPrice         (18 decimal places — NOT 8 like AggregatorV3)
  [7]  int192   bid
  [8]  int192   ask

IMPORTANT: Data Streams prices use 18 decimals, not 8.
  price_usd = benchmarkPrice / 10**18

SpotPrice.timestamp uses local event-receipt time (not on-chain observationsTimestamp)
so the scanner's stale_spot gate measures live feed health correctly.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Callable, Coroutine, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import config
from logger import get_bot_logger
from market_data.rtds_client import SpotPrice

log = get_bot_logger(__name__)

_DS_WS_HOST = "wss://ws.dataengine.chain.link"
_DS_WS_PATH_PREFIX = "/api/v1/ws"

# Data Streams uses uint192 benchmarkPrice with 18 decimal places.
_DS_DECIMALS = 18

# Seconds of WS silence before forcing a reconnect.
_WS_SILENCE_TIMEOUT_S = 30.0   # Data Streams heartbeats are frequent; 30 s is conservative


class ChainlinkStreamsClient:
    """
    Direct Chainlink Data Streams WebSocket client for HYPE/USD.

    When CHAINLINK_DS_API_KEY is configured, connects directly to the Chainlink
    Data Streams engine WebSocket and receives HYPE/USD oracle reports with
    minimum latency — no Polymarket intermediary, no polling.

    If CHAINLINK_DS_API_KEY is not set, start() logs guidance and returns
    immediately.  SpotOracle degrades gracefully to the RTDS
    crypto_prices_chainlink relay (which Polymarket pushes sub-second from the
    same source).

    Dual-feed operation (when both this client and RTDS chainlink relay are
    running): SpotOracle picks the snapshot with the fresher timestamp, giving
    natural failover and latency racing.
    """

    def __init__(self) -> None:
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        self._running = False
        self._ws = None
        self._enabled = False

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register async callback(coin, price) fired on every Data Streams report."""
        self._callbacks.append(callback)

    def get_mid(self, coin: str) -> Optional[float]:
        snap = self._prices.get(coin)
        return snap.price if snap is not None else None

    def get_spot(self, coin: str) -> Optional[SpotPrice]:
        return self._prices.get(coin)

    def get_spot_age(self, coin: str) -> float:
        snap = self._prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    @property
    def enabled(self) -> bool:
        """True if the client has valid API credentials and is actively streaming."""
        return self._enabled

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin streaming.  No-op (with warning) if API key is not configured."""
        if not config.CHAINLINK_DS_API_KEY or not config.CHAINLINK_DS_HYPE_FEED_ID:
            log.info(
                "ChainlinkStreamsClient: disabled — CHAINLINK_DS_API_KEY or "
                "CHAINLINK_DS_HYPE_FEED_ID not set. "
                "Obtain a free sponsored key at https://pm-ds-request.streams.chain.link/ "
                "for direct oracle access. HYPE prices will be sourced from the "
                "RTDS crypto_prices_chainlink relay instead."
            )
            return
        self._running = True
        self._enabled = True
        asyncio.create_task(self._ws_loop())
        log.info(
            "ChainlinkStreamsClient: started",
            feed_id=config.CHAINLINK_DS_HYPE_FEED_ID,
        )

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    def _build_auth_headers(self) -> dict[str, str]:
        """Build the three HMAC authentication headers required by the Data Streams API.

        Signature spec (from Chainlink Data Streams documentation):
          HMAC-SHA256(api_secret, "GET\\n{path_with_query}\\n{timestamp_ms}")
        where path_with_query = /api/v1/ws?feedIDs=<feed_id>
        """
        feed_id = config.CHAINLINK_DS_HYPE_FEED_ID
        path = f"{_DS_WS_PATH_PREFIX}?feedIDs={feed_id}"
        timestamp_ms = str(int(time.time() * 1000))
        message = f"GET\n{path}\n{timestamp_ms}"
        signature = hmac.new(
            config.CHAINLINK_DS_API_SECRET.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "CHAINLINK-DS-APIKEY": config.CHAINLINK_DS_API_KEY,
            "CHAINLINK-DS-TIMESTAMP": timestamp_ms,
            "CHAINLINK-DS-SIGNATURE": signature,
        }

    async def _ws_loop(self) -> None:
        """Persistent WebSocket loop with exponential backoff reconnect."""
        backoff = 1.0
        feed_id = config.CHAINLINK_DS_HYPE_FEED_ID
        url = f"{_DS_WS_HOST}{_DS_WS_PATH_PREFIX}?feedIDs={feed_id}"

        while self._running:
            try:
                auth_headers = self._build_auth_headers()
                async with websockets.connect(
                    url,
                    additional_headers=auth_headers,
                    ping_interval=None,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info(
                        "ChainlinkStreamsClient: connected to Data Streams",
                        feed_id=feed_id,
                    )

                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=_WS_SILENCE_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            log.warning(
                                "ChainlinkStreamsClient: %ds silence — forcing reconnect",
                                _WS_SILENCE_TIMEOUT_S,
                            )
                            break
                        await self._handle_message(json.loads(raw))

            except ConnectionClosed as exc:
                log.warning("ChainlinkStreamsClient: WS disconnected", code=exc.code)
            except asyncio.TimeoutError:
                log.warning("ChainlinkStreamsClient: WS open timeout")
            except Exception as exc:
                log.error("ChainlinkStreamsClient: WS error", exc=str(exc))
            finally:
                self._ws = None

            if self._running:
                log.info("ChainlinkStreamsClient: reconnecting", backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_message(self, msg: dict) -> None:
        """Decode one Data Streams V3 report message.

        Chainlink Data Streams WebSocket pushes JSON with this shape::

            {
                "report": {
                    "feedID": "0x...",
                    "validFromTimestamp": 1234567890,
                    "observationsTimestamp": 1234567890,
                    "fullReport": "0x..."
                }
            }

        The ``fullReport`` is an ABI-encoded V3 report containing the
        benchmarkPrice at slot [6] (bytes 192–223 after the 32-byte context
        prefix) with 18 decimal places.
        """
        report = msg.get("report")
        if not isinstance(report, dict):
            return

        full_report_hex = report.get("fullReport", "")
        if not full_report_hex:
            return

        result = decode_streams_report(full_report_hex)
        if result is None:
            log.warning(
                "ChainlinkStreamsClient: could not decode fullReport",
                raw_prefix=full_report_hex[:66],
            )
            return

        price, _ = result
        snap = SpotPrice(coin="HYPE", price=price, timestamp=time.time())
        self._prices["HYPE"] = snap
        log.debug("ChainlinkStreamsClient: HYPE report", price=round(price, 6))

        for cb in self._callbacks:
            try:
                await cb("HYPE", price)
            except Exception as exc:
                log.error("ChainlinkStreamsClient: callback error", exc=str(exc))


# ── Module-level decode helper ────────────────────────────────────────────────

def decode_streams_report(full_report_hex: str) -> Optional[tuple[float, int]]:
    """Decode a Chainlink Data Streams V3 fullReport.

    Returns (price_usd, observations_timestamp_unix) or None on malformed input.

    V3 report ABI layout (all fields padded to 32 bytes):
      The fullReport contains a 32-byte context prefix, then:
      [0]  bytes32  feedId
      [1]  uint32   validFromTimestamp
      [2]  uint32   observationsTimestamp
      [3]  uint192  nativeFee
      [4]  uint192  linkFee
      [5]  uint32   expiresAt
      [6]  int192   benchmarkPrice         ← 18 decimal places
      [7]  int192   bid
      [8]  int192   ask

    Total: 32 (context) + 9 * 32 (struct) = 320 bytes minimum.

    Data Streams prices use 18 decimal places (NOT 8 like AggregatorV3):
      price_usd = benchmarkPrice / 10**18

    NOTE: If this decoder returns None or gives implausible prices against
    your first live response, log the raw full_report_hex and compare the
    byte layout against the actual API response.  Chainlink may update the
    context prefix size in future schema versions.
    """
    try:
        raw = bytes.fromhex(full_report_hex.removeprefix("0x"))
    except ValueError:
        return None

    # Minimum size: 32-byte context prefix + 9 ABI slots × 32 bytes = 320 bytes.
    if len(raw) < 320:
        return None

    # 32-byte context prefix is skipped; struct begins at byte 32.
    # Slot [2] (relative) = observationsTimestamp: bytes 96–127 after context → raw[128:160]
    obs_ts = int.from_bytes(raw[128:160], byteorder="big", signed=False)

    # Slot [6] (relative) = benchmarkPrice: bytes 224–255 after context → raw[256:288]
    # int192 sign-extended to 256 bits (big-endian two's complement).
    benchmark = int.from_bytes(raw[256:288], byteorder="big", signed=True)

    if benchmark <= 0:
        return None

    return benchmark / (10 ** _DS_DECIMALS), obs_ts
