"""
market_data/chainlink_ws_client.py — Event-driven Chainlink oracle feed.

Subscribes to AnswerUpdated(int256 indexed current, uint256 indexed roundId,
uint256 updatedAt) events on Polygon Mainnet using eth_subscribe/logs over a
persistent WebSocket connection.

Polygon Chainlink uses OCR2 (OffchainAggregatorV2) aggregators. The proxy
contract itself emits NO logs — events are emitted by the underlying OCR2
aggregator. AnswerUpdated IS emitted by the aggregator for backwards
compatibility alongside NewTransmission.

Architecture:
  • On start(): seed current prices via concurrent latestRoundData() HTTP eth_call
    so the position monitor has ground-truth data before the first oracle event.
  • _ws_loop(): persistent WebSocket to POLYGON_WS_URL; single eth_subscribe filter
    covering all 6 OCR2 aggregator addresses.
  • On each AnswerUpdated log from the aggregator: decode price from topics[1],
    update cache, fire callbacks.
  • On reconnect: reseed via latestRoundData() to catch any rounds missed during
    the downtime window.
  • 120 s silence on an established connection → force reconnect (zombie TCP guard).
  • Exponential backoff on disconnect (1 s → 60 s cap).

Requires a WebSocket-capable Polygon JSON-RPC endpoint.
Set POLYGON_WS_URL in .env:
  wss://polygon-mainnet.g.alchemy.com/v2/<KEY>     (Alchemy — recommended)
  wss://polygon-mainnet.infura.io/ws/v3/<KEY>       (Infura)

AnswerUpdated event ABI (emitted by underlying OCR2 aggregator):
  topics[0] = 0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f
  topics[1] = int256 indexed current    ← oracle price (signed, 8 decimals)
  topics[2] = uint256 indexed roundId
  data      = uint256 updatedAt         ← on-chain unix timestamp

Proxy contracts (Polygon Mainnet) — used only for HTTP latestRoundData() seed:
  BTC   0xc907E116054Ad103354f2D350FD2514433D57F6f
  ETH   0xF9680D99D6C9589e2a93a78A04A279e509205945
  SOL   0x10C8264C0935b3B9870013e057f330Ff3e9C56dC
  XRP   0x785ba89291f676b5386652eB12b30cF361020694
  BNB   0x82a6c4AF830caa6c97bb504425f6A66165C2c26e
  DOGE  0xbaf9327b6564454F4a3364C33eFeEf032b4b4444

Underlying OCR2 aggregators — where AnswerUpdated events actually emit:
  BTC   0x014497a2aef847c7021b17bff70a68221d22aa63
  ETH   0x63db7e86391f5d31bab58808bcf75edb272f4f5c
  SOL   0x35b19a67a41282e39c32650b863f714eb95dacf5
  XRP   0x8d5e29ff3b3f55d58abb165ea9ce3886c0a43fc7
  BNB   0x30395df79a543a2308ab0668661fcefc229a19b2
  DOGE  0x2aebe03d31e904ade377f651f9a75519d5d61135

HYPE is absent — no Chainlink AggregatorV3 on Polygon for HYPE.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

import config
from logger import get_bot_logger
from market_data.rtds_client import SpotPrice

log = get_bot_logger(__name__)

# AnswerUpdated(int256,uint256,uint256) — keccak256 of the event signature.
# Emitted by the underlying OCR2 aggregator (NOT the proxy) for backwards compatibility.
_ANSWER_UPDATED_TOPIC = (
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
)

# AggregatorV3Interface.latestRoundData() 4-byte selector — for HTTP seed calls.
_LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"

# All Chainlink USD pairs on Polygon use 8 decimal places.
_DECIMALS = 8

# Per-request HTTP timeout for seed calls.
_HTTP_TIMEOUT_S = 5.0

# Seconds of WS silence before forcing a reconnect (zombie TCP guard).
_WS_SILENCE_TIMEOUT_S = 120.0

# Polygon Mainnet Chainlink proxy addresses — used only for HTTP latestRoundData() seed.
# Must match exactly what Polymarket settlement contracts read at expiry.
_CL_CONTRACTS: dict[str, str] = {
    "BTC":  "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL":  "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "XRP":  "0x785ba89291f676b5386652eB12b30cF361020694",
    "BNB":  "0x82a6c4AF830caa6c97bb504425f6A66165C2c26e",
    "DOGE": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
}

# Underlying OCR2 aggregator addresses — NewTransmission events are emitted here,
# NOT on the proxy.  Resolved via aggregator() (selector 0x245a7bfc) on each proxy.
_CL_AGGREGATORS: dict[str, str] = {
    "BTC":  "0x014497a2aef847c7021b17bff70a68221d22aa63",
    "ETH":  "0x63db7e86391f5d31bab58808bcf75edb272f4f5c",
    "SOL":  "0x35b19a67a41282e39c32650b863f714eb95dacf5",
    "XRP":  "0x8d5e29ff3b3f55d58abb165ea9ce3886c0a43fc7",
    "BNB":  "0x30395df79a543a2308ab0668661fcefc229a19b2",
    "DOGE": "0x2aebe03d31e904ade377f651f9a75519d5d61135",
}

# Reverse map: lowercase aggregator address → coin label, for O(1) lookup in event handler.
_AGG_TO_COIN: dict[str, str] = {
    addr.lower(): coin for coin, addr in _CL_AGGREGATORS.items()
}
# Legacy alias kept for backward compatibility with test imports.
_ADDR_TO_COIN = _AGG_TO_COIN


class ChainlinkWSClient:
    """
    Event-driven Chainlink oracle feed via WebSocket eth_subscribe.

    Connects to a Polygon WebSocket JSON-RPC endpoint (POLYGON_WS_URL) and
    subscribes to AnswerUpdated log events from all tracked OCR2 aggregators
    in a single filter. Events come from the underlying aggregator, not the proxy.
    Callbacks fire within milliseconds of block inclusion —
    no polling interval, no artificial latency.

    On first connect and every reconnect, the current oracle state is seeded via
    concurrent latestRoundData() HTTP eth_call so the price cache is hot before
    the first event arrives (Chainlink heartbeat may be minutes away).

    SpotPrice.timestamp uses local event-receipt time so the scanner's stale_spot
    gate directly measures feed liveness, not oracle-heartbeat age.

    Thread-safety: designed for a single asyncio event loop.
    """

    def __init__(self) -> None:
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        self._running = False
        self._ws: Any = None

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register an async callback(coin, price) fired on every AnswerUpdated event."""
        self._callbacks.append(callback)

    def get_mid(self, coin: str) -> Optional[float]:
        """Latest on-chain oracle price for `coin`; None if not yet seeded."""
        snap = self._prices.get(coin)
        return snap.price if snap is not None else None

    def get_spot(self, coin: str) -> Optional[SpotPrice]:
        """Full SpotPrice snapshot for `coin`; None if not yet seeded."""
        return self._prices.get(coin)

    def get_spot_age(self, coin: str) -> float:
        """Seconds since last AnswerUpdated event receipt for `coin`.

        Uses local event-receipt time, not on-chain updatedAt.  A value within
        a few Chainlink heartbeat periods means the oracle connection is live.
        Returns inf if never received.
        """
        snap = self._prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    def all_mids(self) -> dict[str, float]:
        """Return a copy of the current Chainlink coin → price dict."""
        return {coin: snap.price for coin, snap in self._prices.items()}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Seed prices via HTTP then open the WebSocket subscription."""
        self._running = True
        await self._seed_prices_http()
        asyncio.create_task(self._ws_loop())
        log.info(
            "ChainlinkWSClient started",
            coins=sorted(_CL_CONTRACTS.keys()),
            ws_endpoint=config.POLYGON_WS_URL,
        )

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── HTTP seed ─────────────────────────────────────────────────────────────

    async def _seed_prices_http(self) -> None:
        """Call latestRoundData() for all 6 contracts concurrently via HTTP eth_call.

        Populates the price cache so callers have ground-truth data immediately.
        Called on first start and on every WebSocket reconnect (to catch any
        oracle rounds emitted while the WS was down).
        """
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._http_call_latest_round(session, coin, addr)
                for coin, addr in _CL_CONTRACTS.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        seed_ts = time.time()
        seeded: list[str] = []
        for coin, result in zip(_CL_CONTRACTS.keys(), results):
            if isinstance(result, Exception):
                log.warning(
                    "ChainlinkWSClient: HTTP seed failed",
                    coin=coin, exc=str(result),
                )
                continue
            if result is None:
                log.warning("ChainlinkWSClient: HTTP seed empty", coin=coin)
                continue
            price, _ = result
            self._prices[coin] = SpotPrice(coin=coin, price=price, timestamp=seed_ts)
            seeded.append(f"{coin}={price:.4f}")

        log.info("ChainlinkWSClient: seeded via latestRoundData()", prices=seeded)

    async def _http_call_latest_round(
        self,
        session: aiohttp.ClientSession,
        coin: str,
        address: str,
    ) -> Optional[tuple[float, float]]:
        """eth_call latestRoundData() via HTTP JSON-RPC.  Returns (price, updatedAt)."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": address, "data": _LATEST_ROUND_DATA_SELECTOR},
                "latest",
            ],
        }
        async with session.post(
            config.POLYGON_RPC_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)

        if body.get("error"):
            return None
        hex_result: str = body.get("result", "")
        if not hex_result or hex_result == "0x":
            return None
        return decode_latest_round_data(hex_result)

    # ── WebSocket event loop ──────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Persistent WebSocket subscription to AnswerUpdated events.

        A single eth_subscribe filter covers all 6 OCR2 aggregator addresses.
        (AnswerUpdated is emitted by the aggregator, not the proxy.)
        Reconnects with exponential backoff; reseeds prices via HTTP on each
        reconnect to close any gap in coverage.
        """
        backoff = 1.0
        addresses = [addr.lower() for addr in _CL_AGGREGATORS.values()]
        sub_request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "logs",
                {
                    "address": addresses,
                    "topics": [_ANSWER_UPDATED_TOPIC],
                },
            ],
        })

        while self._running:
            try:
                async with websockets.connect(
                    config.POLYGON_WS_URL,
                    ping_interval=None,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0

                    # Subscribe — single filter for all 6 contracts.
                    await ws.send(sub_request)
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    resp = json.loads(raw)
                    sub_id = resp.get("result")
                    if not sub_id:
                        raise RuntimeError(
                            f"ChainlinkWSClient: eth_subscribe rejected: {resp}"
                        )

                    log.info(
                        "ChainlinkWSClient: subscribed to AnswerUpdated (from OCR2 aggregators)",
                        sub_id=sub_id,
                        aggregators=len(addresses),
                    )

                    # Reseed on reconnect — catches rounds emitted while WS was down.
                    await self._seed_prices_http()

                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=_WS_SILENCE_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            log.warning(
                                "ChainlinkWSClient: silence — forcing reconnect",
                                silence_s=_WS_SILENCE_TIMEOUT_S,
                            )
                            break
                        await self._handle_event(json.loads(raw))

            except ConnectionClosed as exc:
                log.warning("ChainlinkWSClient: WS disconnected", code=exc.code)
            except asyncio.TimeoutError:
                log.warning("ChainlinkWSClient: WS open timeout")
            except Exception as exc:
                log.error("ChainlinkWSClient: WS error", exc=str(exc))
            finally:
                self._ws = None

            if self._running:
                log.info("ChainlinkWSClient: reconnecting", backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_event(self, msg: dict) -> None:
        """Decode one eth_subscription log notification for an AnswerUpdated event.

        Expected shape::

            {
              "jsonrpc": "2.0",
              "method": "eth_subscription",
              "params": {
                "subscription": "0x...",
                "result": {
                  "address": "0x<ocr2_aggregator>",
                  "topics": [
                    "0x0559884f...",   # AnswerUpdated event signature
                    "0x<int256>",      # indexed current (price, signed)
                    "0x<uint256>",     # indexed roundId
                  ],
                  "data": "0x<uint256>",  # updatedAt
                  ...
                }
              }
            }
        """
        params = msg.get("params")
        if not params:
            return
        result = params.get("result")
        if not isinstance(result, dict):
            return

        address = result.get("address", "").lower()
        coin = _AGG_TO_COIN.get(address)
        if coin is None:
            return

        topics = result.get("topics", [])
        if len(topics) < 2:
            return

        # topics[1] = int256 indexed `current` — 32-byte big-endian signed integer.
        try:
            raw_int = int.from_bytes(
                bytes.fromhex(topics[1][2:].zfill(64)),
                byteorder="big",
                signed=True,
            )
            price = raw_int / (10 ** _DECIMALS)
        except (ValueError, IndexError) as exc:
            log.warning(
                "ChainlinkWSClient: event decode error",
                exc=str(exc),
                address=address,
            )
            return

        if price <= 0:
            log.warning("ChainlinkWSClient: non-positive price", coin=coin, price=price)
            return

        snap = SpotPrice(coin=coin, price=price, timestamp=time.time())
        self._prices[coin] = snap

        # ── Phase C: boundary tick logging ────────────────────────────────────
        # Extract on-chain updatedAt from the event's `data` field (raw uint256).
        try:
            _data_hex = result.get("data", "0x")
            _updated_at_onchain: Optional[float] = None
            if _data_hex and len(_data_hex) >= 2:
                _raw = int.from_bytes(
                    bytes.fromhex(_data_hex.removeprefix("0x").zfill(64)),
                    byteorder="big",
                    signed=False,
                )
                if _raw > 0:
                    _updated_at_onchain = float(_raw)
        except (ValueError, AttributeError):
            _updated_at_onchain = None

        # Detect ticks landing within [-15 s, +5 s] of Chainlink round boundaries
        # (bucket_5m=300 s, bucket_15m=900 s, bucket_4h=14400 s).
        _local_ts = snap.timestamp
        for _period_s in (300, 900, 14400):
            _last_boundary = (_local_ts // _period_s) * _period_s
            _secs_after = _local_ts - _last_boundary
            _secs_before_next = _period_s - _secs_after
            if _secs_after <= 5.0 or _secs_before_next <= 15.0:
                log.info(
                    "CL_BOUNDARY_TICK",
                    coin=coin,
                    price=round(price, 6),
                    period_s=_period_s,
                    secs_after_boundary=round(_secs_after, 3),
                    secs_before_next=round(_secs_before_next, 3),
                    local_ts=round(_local_ts, 3),
                    onchain_updated_at=_updated_at_onchain,
                )
                break

        log.debug("ChainlinkWSClient: AnswerUpdated", coin=coin, price=round(price, 6))

        for cb in self._callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error(
                    "ChainlinkWSClient: callback error", coin=coin, exc=str(exc)
                )


# ── Module-level ABI helpers (used by tests and by the HTTP seed path) ────────

def decode_latest_round_data(hex_data: str) -> Optional[tuple[float, float]]:
    """Decode an ABI-encoded latestRoundData() response.

    ABI layout — 5 × 32-byte slots (all left-padded):
      [0]  uint80   roundId
      [1]  int256   answer        ← oracle price, signed two's-complement
      [2]  uint256  startedAt
      [3]  uint256  updatedAt     ← on-chain unix timestamp
      [4]  uint80   answeredInRound

    Returns (price_usd, updated_at_unix) or None on malformed input.
    """
    try:
        raw = bytes.fromhex(hex_data.removeprefix("0x"))
    except ValueError:
        return None

    if len(raw) < 160:   # need at least 5 × 32 bytes
        return None

    # Slot 1 (bytes 32–63): int256 answer — signed big-endian.
    answer = int.from_bytes(raw[32:64], byteorder="big", signed=True)
    # Slot 3 (bytes 96–127): uint256 updatedAt.
    updated_at = int.from_bytes(raw[96:128], byteorder="big", signed=False)

    if answer <= 0:
        return None

    return answer / (10 ** _DECIMALS), float(updated_at)
