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
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
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
_WS_SILENCE_TIMEOUT_S = 30.0

# HA reconnect parameters — mirrors Chainlink's Go SDK stream.go constants.
_HA_RECONNECT_MIN_S    = 1.0
_HA_RECONNECT_MAX_S    = 10.0
_HA_MAX_RECONNECT_ATTEMPTS = 5

# Phase-offset stagger applied to origin_index > 0 connections so the two
# HA origins do not expire and reconnect at the same moment.  Must be much
# shorter than the observed server session TTL (~30-60 s) to prevent both
# origins being simultaneously down during the wait.  5 s is enough separation
# while keeping the reconnect gap small.
_HA_RECONNECT_STAGGER_S = 5.0

# Partial-reconnect wait: how long a single dropped origin waits before
# reconnecting when the other origin is still alive.  Keeping this short (3 s)
# ensures the down origin rejoins before the live origin's session also expires,
# preventing the cascade into a full outage.  Do NOT use _WS_SILENCE_TIMEOUT_S
# / 2 (15 s) here — that was the original bug: 15 s > half the server TTL,
# so one partial drop reliably cascaded into a full outage.
_HA_PARTIAL_RECONNECT_WAIT_S = 3.0

# Library built-in WebSocket PING interval and PONG timeout (seconds).
# The websockets library handles PING/PONG at the protocol level, integrated
# with the frame-receive loop, so PONG resolution is not subject to asyncio
# task-scheduling delay.  A 30-second PONG timeout gives plenty of margin for
# WAN latency spikes while still detecting genuinely dead connections before
# the 30-second silence timeout fires.
# NOTE: our earlier manual keepalive loop (RFC 6455 ws.ping() + asyncio
# wait_for with a 10-second pong_waiter timeout) was the root cause of
# frequent disconnections: asyncio task-scheduling jitter on Windows
# ProactorEventLoop caused the 10-second wait_for to fire spuriously even
# when the server did respond in time, producing session lengths as short as
# 15-20 seconds.  The library built-in avoids this race entirely.
_WS_PING_INTERVAL_S = 5.0
_WS_PING_TIMEOUT_S  = 30.0

# Header names used by the Chainlink data-streams infrastructure.
_CLL_ORIGIN_HEADER          = "CLL-ORIGIN"
_CLL_AVAILABLE_ORIGINS_HDR  = "x-cll-available-origins"


@dataclass
class StreamStats:
    """
    Mirrors Chainlink Go SDK Stats struct (stream.go).

    accepted              — reports accepted and delivered to callbacks
    deduplicated          — reports discarded because the other HA connection
                            already delivered the same observationsTimestamp
    partial_reconnects    — reconnects while ≥1 other connection was still live
    full_reconnects       — reconnects with zero active connections (full gap)
    configured_connections — number of origins the server advertised
    active_connections    — connections currently open
    """
    accepted:               int = 0
    deduplicated:           int = 0
    partial_reconnects:     int = 0
    full_reconnects:        int = 0
    configured_connections: int = 0
    active_connections:     int = 0

    def __str__(self) -> str:
        return (
            f"accepted={self.accepted} dedup={self.deduplicated} "
            f"partial_reconnects={self.partial_reconnects} "
            f"full_reconnects={self.full_reconnects} "
            f"configured={self.configured_connections} "
            f"active={self.active_connections}"
        )


class ChainlinkStreamsClient:
    """
    Direct Chainlink Data Streams WebSocket client — HA mode.

    Maintains one persistent WebSocket connection per server origin
    (typically 2: origin 001 and 002).  Reports arriving on both
    connections are deduplicated by ``observationsTimestamp`` so each
    oracle update is delivered to callbacks exactly once — the first
    copy wins.  If one origin drops, the other continues without
    interruption; the latency edge is never lost.

    This is the same architecture as Chainlink's official Go SDK
    (data-streams-sdk/go/stream.go): HA connections + dedup watermark.

    Authentication modes (first match wins):
      1. CHAINLINK_DS_USERNAME + CHAINLINK_DS_PASSWORD  (Mercury HMAC credentials).
      2. CHAINLINK_DS_API_KEY  + CHAINLINK_DS_API_SECRET (alias for the same thing).

    If neither credential set is configured, start() logs guidance and returns
    immediately.  SpotOracle degrades gracefully to the RTDS relay.
    """

    def __init__(self) -> None:
        self._prices:    dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        self._running  = False
        self._enabled  = False
        # Active feeds for this client instance (set in start())
        self._active_feeds:    dict[str, str] = {}
        # Reverse lookup: lowercase feed ID -> coin name (rebuilt in start())
        self._feedid_to_coin:  dict[str, str] = {}
        # HA: list of active ws handles so stop() can close them all
        self._ws_handles: list = []
        # HA dedup: feedID.lower() → latest observationsTimestamp delivered
        # No asyncio.Lock needed — asyncio is cooperative; the check+set
        # sequence in _handle_message has no await between them.
        self._watermark: dict[str, int] = {}
        # Stats
        self.stats = StreamStats()
        # S3.3 — Rolling reconnect rate tracking (full reconnects only).
        # _reconnect_timestamps holds wall-clock timestamps of every full_reconnect
        # event.  Entries older than 3600s are pruned each time a new event fires.
        self._reconnect_timestamps: list[float] = []
        self._last_reconnect_at: float = 0.0

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register async callback(coin, price) fired on every new Data Streams report."""
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

    @property
    def is_connected(self) -> bool:
        """True if at least one HA WebSocket connection is currently open."""
        return self._running and self.stats.active_connections > 0

    @property
    def reconnects_1h(self) -> int:
        """Number of full reconnects in the rolling 60-minute window (S3.3)."""
        cutoff = time.time() - 3600.0
        return sum(1 for t in self._reconnect_timestamps if t >= cutoff)

    @property
    def last_reconnect_at(self) -> float:
        """Epoch timestamp of the most recent full reconnect. 0.0 = never (S3.3)."""
        return self._last_reconnect_at

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        has_basic = bool(config.CHAINLINK_DS_USERNAME and config.CHAINLINK_DS_PASSWORD)
        has_hmac  = bool(config.CHAINLINK_DS_API_KEY  and config.CHAINLINK_DS_API_SECRET)
        return has_basic or has_hmac

    def _api_key_secret(self) -> tuple[str, str]:
        if config.CHAINLINK_DS_USERNAME:
            return config.CHAINLINK_DS_USERNAME, config.CHAINLINK_DS_PASSWORD
        return config.CHAINLINK_DS_API_KEY, config.CHAINLINK_DS_API_SECRET

    async def start(self, coin: Optional[str] = None) -> None:
        """Begin streaming.

        Args:
            coin: If provided, subscribe only to this coin's feed (e.g. "HYPE",
                  "BTC").  If omitted, all configured feeds are subscribed.
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

        self._feedid_to_coin = {
            fid.lower(): c for c, fid in self._active_feeds.items()
        }

        # Fetch available server origins for HA mode.
        origins = await self._fetch_origins()
        self.stats.configured_connections = len(origins) if origins else 1

        self._running = True
        self._enabled = True

        if len(origins) > 1:
            log.info(
                "ChainlinkStreamsClient: HA mode — launching parallel connections",
                origins=origins,
                coins=list(self._active_feeds.keys()),
            )
            for i, origin in enumerate(origins):
                asyncio.create_task(
                    self._conn_loop(origin, initial_delay=i * _HA_RECONNECT_STAGGER_S, origin_index=i)
                )
        else:
            origin = origins[0] if origins else ""
            log.info(
                "ChainlinkStreamsClient: single-connection mode",
                origin=origin or "default",
                coins=list(self._active_feeds.keys()),
            )
            asyncio.create_task(self._conn_loop(origin))

    async def stop(self) -> None:
        self._running = False
        for ws in list(self._ws_handles):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_handles.clear()

    # ── Origin discovery ──────────────────────────────────────────────────────

    async def _fetch_origins(self) -> list[str]:
        """Fetch CLL-AVAILABLE-ORIGINS from the server via a HEAD request.

        The server responds with a header like::

            x-cll-available-origins: {001,002}

        which we parse into ["001", "002"].  Falls back to an empty list
        (single-connection mode) if the request fails.
        """
        host = config.CHAINLINK_DS_HOST
        host = host.replace("wss://", "https://").replace("ws://", "http://")
        api_key, api_secret = self._api_key_secret()
        ts         = int(time.time() * 1000)
        body_hash  = hashlib.sha256(b"").hexdigest()
        sts        = f"GET / {body_hash} {api_key} {ts}"
        sig        = hmac.new(api_secret.encode(), sts.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            host + "/",
            method="HEAD",
            headers={
                "Authorization":                    api_key,
                "X-Authorization-Timestamp":        str(ts),
                "X-Authorization-Signature-SHA256": sig,
            },
        )
        origins_hdr = ""
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                origins_hdr = r.headers.get(_CLL_AVAILABLE_ORIGINS_HDR, "")
        except urllib.error.HTTPError as e:
            origins_hdr = e.headers.get(_CLL_AVAILABLE_ORIGINS_HDR, "")
        except Exception as exc:
            log.warning("ChainlinkStreamsClient: could not fetch origins", exc=str(exc))
            return []

        if origins_hdr:
            origins = re.findall(r"\w+", origins_hdr)
            if origins:
                return origins
        return []

    # ── Authentication ────────────────────────────────────────────────────────

    def _build_auth_headers(self, origin: str = "") -> dict[str, str]:
        """Build HMAC authentication headers.

        Chainlink Data Streams HMAC auth (per official spec):
          Authorization:                    <api_key>
          X-Authorization-Timestamp:        <unix_ms>
          X-Authorization-Signature-SHA256: hex(HMAC-SHA256(secret,
              "GET {path} {sha256_empty_body} {api_key} {unix_ms}"))

        The optional ``origin`` value is added as ``CLL-ORIGIN`` so the server
        routes this connection to the requested instance (HA mode).
        """
        api_key, api_secret = self._api_key_secret()
        feed_ids_str   = ",".join(fid for fid in self._active_feeds.values() if fid)
        path           = f"{_DS_WS_PATH_PREFIX}?feedIDs={feed_ids_str}"
        timestamp_ms   = int(time.time() * 1000)
        body_hash      = hashlib.sha256(b"").hexdigest()
        string_to_sign = f"GET {path} {body_hash} {api_key} {timestamp_ms}"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Authorization":                    api_key,
            "X-Authorization-Timestamp":        str(timestamp_ms),
            "X-Authorization-Signature-SHA256": signature,
        }
        if origin:
            headers[_CLL_ORIGIN_HEADER] = origin
        return headers

    # ── Per-origin connection loop ────────────────────────────────────────────

    async def _conn_loop(
        self,
        origin: str,
        initial_delay: float = 0.0,
        origin_index: int = 0,
    ) -> None:
        """Persistent reconnect loop for one server origin.

        Mirrors the newWSconnWithRetry / monitorConn pattern from the Go SDK.
        Gives up only when attempts exceed _HA_MAX_RECONNECT_ATTEMPTS AND
        no other connection is currently active (same condition as the SDK).

        initial_delay:  seconds to wait before the very first connection attempt,
                       used to stagger HA origins so their reconnect cycles are
                       always offset — guaranteeing one connection survives.
        origin_index:  0-based index of this origin; used to restore the phase
                       offset after a full reconnect (both connections down).
        """
        if initial_delay > 0:
            log.debug(
                "ChainlinkStreamsClient: staggering startup",
                origin=origin or "default",
                delay_s=initial_delay,
            )
            await asyncio.sleep(initial_delay)

        feed_ids_str = ",".join(fid for fid in self._active_feeds.values() if fid)
        host = config.CHAINLINK_DS_HOST
        if host.startswith("http://"):
            host = "ws://" + host[len("http://"):]
        elif host.startswith("https://"):
            host = "wss://" + host[len("https://"):]
        url         = f"{host}{_DS_WS_PATH_PREFIX}?feedIDs={feed_ids_str}"
        origin_label = origin or "default"
        backoff      = _HA_RECONNECT_MIN_S
        attempts     = 0

        while self._running:
            # Give up only if exceeded attempts AND no other live connection.
            if (attempts >= _HA_MAX_RECONNECT_ATTEMPTS
                    and self.stats.active_connections == 0):
                log.error(
                    "ChainlinkStreamsClient: max reconnect attempts exhausted "
                    "with no active connections — giving up",
                    origin=origin_label,
                    attempts=attempts,
                )
                break

            try:
                auth_headers = self._build_auth_headers(origin)
                async with websockets.connect(
                    url,
                    additional_headers=auth_headers,
                    ping_interval=_WS_PING_INTERVAL_S,
                    ping_timeout=_WS_PING_TIMEOUT_S,
                    open_timeout=15,
                ) as ws:
                    self._ws_handles.append(ws)
                    self.stats.active_connections += 1
                    backoff  = _HA_RECONNECT_MIN_S
                    attempts = 0
                    conn_started_at = time.time()
                    log.info(
                        "ChainlinkStreamsClient: connected",
                        origin=origin_label,
                        active_connections=self.stats.active_connections,
                    )

                    try:
                        # Use `async for` instead of `asyncio.wait_for(ws.recv(), ...)`
                        # to avoid the Windows ProactorEventLoop cancellation race.
                        # `asyncio.wait_for` creates an inner Task and cancels it on
                        # timeout; that cancellation triggers the transport-cleanup
                        # race ('NoneType' has no attribute 'resume_reading') every
                        # 10-25 s when combined with the library's 5-s ping interval.
                        # `async for raw in ws:` calls recv() directly without a
                        # Task/wait_for wrapper, eliminating the cancellation path.
                        # Silence detection is handled by ping_interval + ping_timeout
                        # (5 s ping, 30 s pong timeout) set on the connection above.
                        try:
                            async for raw in ws:
                                await self._handle_message(json.loads(raw))
                        except (AttributeError, OSError) as exc:
                            # Windows ProactorEventLoop transport-cleanup race — same
                            # root cause as pm_client WS shard 'resume_reading' error.
                            log.info(
                                "ChainlinkStreamsClient: connection reset",
                                origin=origin_label,
                                exc=type(exc).__name__,
                                session_s=round(time.time() - conn_started_at, 1),
                            )
                    finally:
                        try:
                            self._ws_handles.remove(ws)
                        except ValueError:
                            pass
                        session_s = round(time.time() - conn_started_at, 1)
                        self.stats.active_connections -= 1
                        log.info(
                            "ChainlinkStreamsClient: disconnected",
                            origin=origin_label,
                            session_s=session_s,
                            active_connections=self.stats.active_connections,
                        )

            except ConnectionClosed as exc:
                code = getattr(exc, "code", exc)
                log.warning("ChainlinkStreamsClient: WS closed", origin=origin_label, code=code)
            except asyncio.TimeoutError:
                log.warning("ChainlinkStreamsClient: WS open timeout", origin=origin_label)
            except Exception as exc:
                log.error("ChainlinkStreamsClient: WS error", origin=origin_label, exc=str(exc))

            if not self._running:
                break

            attempts += 1
            # Mirror SDK stats: partial if another connection is still alive.
            if self.stats.active_connections > 0:
                self.stats.partial_reconnects += 1
                # Another connection is live — reconnect quickly (3 s) to restore
                # HA coverage before the live connection's session also expires.
                # The previous value (_WS_SILENCE_TIMEOUT_S / 2 = 15 s) was the
                # root of the cascade: 15 s > half the ~30 s server session TTL,
                # so a partial drop reliably caused both origins to be down
                # simultaneously on the very next server-close event.
                wait = _HA_PARTIAL_RECONNECT_WAIT_S
                wait += random.uniform(0.0, 1.0)
                log.info(
                    "ChainlinkStreamsClient: partial reconnect",
                    origin=origin_label,
                    partial_reconnects=self.stats.partial_reconnects,
                    wait_s=round(wait, 1),
                    active_connections=self.stats.active_connections,
                )
            else:
                # All connections simultaneously down — log at WARNING so it's visible.
                # Reconnect quickly to minimise data gap.
                self.stats.full_reconnects += 1
                # S3.3 — Track rolling reconnect rate and alert when excessive.
                _now = time.time()
                self._last_reconnect_at = _now
                self._reconnect_timestamps.append(_now)
                # Prune entries older than 1 hour.
                self._reconnect_timestamps = [
                    t for t in self._reconnect_timestamps if t >= _now - 3600.0
                ]
                _reconnects_1h = len(self._reconnect_timestamps)
                if _reconnects_1h > config.CHAINLINK_MAX_RECONNECTS_1H:
                    log.warning(
                        "[CHAINLINK_DEGRADED]",
                        reconnects_1h=_reconnects_1h,
                        threshold=config.CHAINLINK_MAX_RECONNECTS_1H,
                        oracle_quality="impacted",
                    )
                log.warning(
                    "ChainlinkStreamsClient: all connections down — reconnecting",
                    origin=origin_label,
                    full_reconnects=self.stats.full_reconnects,
                    reconnects_1h=_reconnects_1h,
                    stats=str(self.stats),
                )
                # Restore the phase offset so this origin reconnects at a
                # different time from origin 0, preventing both connections
                # from cycling in phase after a simultaneous full drop.
                # Use _HA_RECONNECT_STAGGER_S (5 s) not _WS_SILENCE_TIMEOUT_S/2
                # (15 s) — 5 s is enough offset while keeping the reconnect gap
                # well below the server's ~30-60 s session TTL.
                jitter  = random.uniform(0.0, backoff * 0.3)
                stagger = origin_index * _HA_RECONNECT_STAGGER_S
                wait    = backoff + jitter + stagger

            log.debug(
                "ChainlinkStreamsClient: reconnecting",
                origin=origin_label,
                backoff_s=round(wait, 2),
                stats=str(self.stats),
            )
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, _HA_RECONNECT_MAX_S)

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """Decode one Data Streams V3 report with HA deduplication.

        Chainlink Data Streams WebSocket pushes JSON with this shape::

            {
                "report": {
                    "feedID": "0x...",
                    "validFromTimestamp": 1234567890,
                    "observationsTimestamp": 1234567890,
                    "fullReport": "0x..."
                }
            }

        HA dedup: ``observationsTimestamp`` in the JSON header is the oracle
        consensus timestamp.  When two connections carry the same report the
        first one wins; the duplicate is counted and silently discarded.
        No lock needed — asyncio is cooperative and the check+set has no
        ``await`` between them.
        """
        report = msg.get("report")
        if not isinstance(report, dict):
            return

        full_report_hex = report.get("fullReport", "")
        if not full_report_hex:
            return

        raw_feed_id = report.get("feedID", "").lower()
        coin = self._feedid_to_coin.get(raw_feed_id)
        if coin is None:
            log.debug("ChainlinkStreamsClient: unknown feedID", feed_id=raw_feed_id[:20])
            return

        # ── HA dedup ──────────────────────────────────────────────────────────
        obs_ts = report.get("observationsTimestamp", 0)
        if obs_ts <= self._watermark.get(raw_feed_id, 0):
            self.stats.deduplicated += 1
            log.debug("ChainlinkStreamsClient: dedup", coin=coin, obs_ts=obs_ts)
            return
        self._watermark[raw_feed_id] = obs_ts
        # ─────────────────────────────────────────────────────────────────────

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
        self.stats.accepted += 1
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
