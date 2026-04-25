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

_DS_WS_PATH_PREFIX = "/api/v1/ws"

# Data Streams uses uint192 benchmarkPrice with 18 decimal places.
_DS_DECIMALS = 18

# Seconds of WS silence before forcing a reconnect.
_WS_SILENCE_TIMEOUT_S = 30.0   # Data Streams heartbeats are frequent; 30 s is conservative


class ChainlinkStreamsClient:
    """
    Direct Chainlink Data Streams WebSocket client for HYPE/USD.

    Supports two authentication modes (first match wins):
      1. Mercury Basic auth — set CHAINLINK_DS_USERNAME + CHAINLINK_DS_PASSWORD.
         Host is taken from CHAINLINK_DS_HOST (default wss://ws.dataengine.chain.link).
         Used for direct Mercury pipeline access (e.g. pipeline management endpoints).
      2. Legacy HMAC — set CHAINLINK_DS_API_KEY + CHAINLINK_DS_API_SECRET.
         Connects to the standard Data Streams consumer WS with HMAC-signed headers.

    If neither credential set is configured, start() logs guidance and returns
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
        # Active feeds for this client instance (set in start())
        self._active_feeds: dict[str, str] = {}
        # Reverse lookup: lowercase feed ID -> coin name (rebuilt in start())
        self._feedid_to_coin: dict[str, str] = {}

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

    def _has_credentials(self) -> bool:
        """Return True if at least one auth mode is fully configured."""
        has_basic = bool(config.CHAINLINK_DS_USERNAME and config.CHAINLINK_DS_PASSWORD)
        has_hmac  = bool(config.CHAINLINK_DS_API_KEY and config.CHAINLINK_DS_API_SECRET)
        return has_basic or has_hmac

    async def start(self, coin: Optional[str] = None) -> None:
        """Begin streaming.

        Args:
            coin: If provided, subscribe only to this coin's feed (e.g. "HYPE",
                  "BTC").  Useful when the API account has a per-connection feed
                  limit and you want one client per coin.  If omitted, all
                  configured feeds are subscribed in a single connection.
        """
        all_feeds = {c: fid for c, fid in config.CHAINLINK_DS_FEED_IDS.items() if fid}
        if coin is not None:
            fid = all_feeds.get(coin.upper())
            self._active_feeds = {coin.upper(): fid} if fid else {}
        else:
            self._active_feeds = all_feeds

        if not self._active_feeds or not self._has_credentials():
            log.info(
                "ChainlinkStreamsClient: disabled — credentials or feed IDs not set. "
                "Set CHAINLINK_DS_USERNAME+PASSWORD (Mercury) or "
                "CHAINLINK_DS_API_KEY+SECRET (HMAC) plus per-coin CHAINLINK_DS_{COIN}_FEED_ID vars. "
                "Prices will be sourced from the RTDS crypto_prices_chainlink relay instead."
            )
            return

        # Build reverse lookup from the active subset.
        self._feedid_to_coin = {
            fid.lower(): c for c, fid in self._active_feeds.items()
        }

        self._running = True
        self._enabled = True
        asyncio.create_task(self._ws_loop())
        log.info(
            "ChainlinkStreamsClient: started",
            coins=list(self._active_feeds.keys()),
            num_feeds=len(self._active_feeds),
            host=config.CHAINLINK_DS_HOST,
            auth_mode="hmac",
        )

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    def _build_auth_headers(self) -> dict[str, str]:
        """Build HMAC authentication headers for the WebSocket upgrade request.

        Chainlink Data Streams HMAC auth (per official spec):
          Authorization:                    <api_key>
          X-Authorization-Timestamp:        <unix_ms>
          X-Authorization-Signature-SHA256: hex(HMAC-SHA256(secret,
              "GET {path} {sha256_empty_body} {api_key} {unix_ms}"))

        Credentials: CHAINLINK_DS_USERNAME + CHAINLINK_DS_PASSWORD are treated
        as api_key + api_secret (they are HMAC credentials, not Basic auth).
        CHAINLINK_DS_API_KEY + CHAINLINK_DS_API_SECRET are an alternative name
        for the same values.
        """
        if config.CHAINLINK_DS_USERNAME:
            api_key    = config.CHAINLINK_DS_USERNAME
            api_secret = config.CHAINLINK_DS_PASSWORD
        else:
            api_key    = config.CHAINLINK_DS_API_KEY
            api_secret = config.CHAINLINK_DS_API_SECRET

        feed_ids_str = ",".join(fid for fid in self._active_feeds.values() if fid)
        path         = f"{_DS_WS_PATH_PREFIX}?feedIDs={feed_ids_str}"
        timestamp_ms = int(time.time() * 1000)
        body_hash    = hashlib.sha256(b"").hexdigest()
        string_to_sign = f"GET {path} {body_hash} {api_key} {timestamp_ms}"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": api_key,
            "X-Authorization-Timestamp": str(timestamp_ms),
            "X-Authorization-Signature-SHA256": signature,
        }

    async def _ws_loop(self) -> None:
        """Persistent WebSocket loop with exponential backoff reconnect."""
        backoff = 1.0
        feed_ids_str = ",".join(fid for fid in self._active_feeds.values() if fid)
        # Strip a trailing http(s):// scheme from the host if the user set an https:// URL.
        host = config.CHAINLINK_DS_HOST
        if host.startswith("http://"):
            host = "ws://" + host[len("http://"):]
        elif host.startswith("https://"):
            host = "wss://" + host[len("https://"):]
        url = f"{host}{_DS_WS_PATH_PREFIX}?feedIDs={feed_ids_str}"

        while self._running:
            try:
                auth_headers = self._build_auth_headers()
                async with websockets.connect(
                    url,
                    extra_headers=auth_headers,
                    ping_interval=None,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info(
                        "ChainlinkStreamsClient: connected to Data Streams",
                        feeds=feed_ids_str[:80],
                    )

                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=_WS_SILENCE_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            log.warning(
                                "ChainlinkStreamsClient: silence — forcing reconnect",
                                silence_s=_WS_SILENCE_TIMEOUT_S,
                            )
                            break
                        await self._handle_message(json.loads(raw))

            except ConnectionClosed as exc:
                log.warning("ChainlinkStreamsClient: WS disconnected", code=exc.code if hasattr(exc, 'code') else exc)
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

        # Determine which coin this report is for via the feedID field.
        raw_feed_id = report.get("feedID", "").lower()
        coin = self._feedid_to_coin.get(raw_feed_id)
        if coin is None:
            log.debug("ChainlinkStreamsClient: unknown feedID", feed_id=raw_feed_id[:20])
            return

        result = decode_streams_report(full_report_hex)
        if result is None:
            log.warning(
                "ChainlinkStreamsClient: could not decode fullReport",
                coin=coin,
                raw_prefix=full_report_hex[:66],
            )
            return

        price, _ = result
        snap = SpotPrice(coin=coin, price=price, timestamp=time.time())
        self._prices[coin] = snap
        log.debug("ChainlinkStreamsClient: report", coin=coin, price=round(price, 6))

        for cb in self._callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error("ChainlinkStreamsClient: callback error", exc=str(exc))


# ── Module-level decode helper ────────────────────────────────────────────────

def decode_streams_report(full_report_hex: str) -> Optional[tuple[float, int]]:
    """Decode a Chainlink Data Streams V3 fullReport.

    Returns (price_usd, observations_timestamp_unix) or None on malformed input.

    V3 fullReport ABI layout (Mercury pipeline, 992 bytes typical):
      The fullReport is ABI-encoded as:
        (bytes32[3] reportContext, bytes reportBlob, bytes32[] rawRs, bytes32[] rawSs, bytes rawVs)

      Fixed-header (7 × 32 bytes):
        [0:32]    reportContext[0]
        [32:64]   reportContext[1]
        [64:96]   reportContext[2]
        [96:128]  ABI offset for reportBlob  = 224 (0xE0)
        [128:160] ABI offset for rawRs
        [160:192] ABI offset for rawSs
        [192:224] ABI offset for rawVs
        [224:256] reportBlob length = 288 (0x120)

      reportBlob (9 × 32 bytes, starting at byte 256):
        [256:288]  bytes32  feedId
        [288:320]  uint32   validFromTimestamp
        [320:352]  uint32   observationsTimestamp
        [352:384]  uint192  nativeFee
        [384:416]  uint192  linkFee
        [416:448]  uint32   expiresAt
        [448:480]  int192   benchmarkPrice  ← 18 decimal places
        [480:512]  int192   bid
        [512:544]  int192   ask

    Data Streams prices use 18 decimal places (NOT 8 like AggregatorV3):
      price_usd = benchmarkPrice / 10**18
    """
    try:
        raw = bytes.fromhex(full_report_hex.removeprefix("0x"))
    except ValueError:
        return None

    # Minimum: 7 header slots (224 bytes) + length slot (32) + struct through benchmarkPrice (7 slots = 224).
    if len(raw) < 480:
        return None

    # observationsTimestamp: reportBlob slot 2 → fullReport byte 320
    obs_ts = int.from_bytes(raw[320:352], byteorder="big", signed=False)

    # benchmarkPrice: reportBlob slot 6 → fullReport byte 448; int192 in two's complement
    benchmark = int.from_bytes(raw[448:480], byteorder="big", signed=True)

    if benchmark <= 0:
        return None

    return benchmark / (10 ** _DS_DECIMALS), obs_ts
