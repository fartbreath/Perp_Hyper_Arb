"""
rtds_client.py — Polymarket RTDS price feed + Chainlink on-chain oracle client.

Two independent price sources, kept strictly separate:

1. RTDS WebSocket (wss://ws-live-data.polymarket.com)
   Subscribes to:
   - ``crypto_prices``           — exchange-aggregated ticks for BTC/ETH/SOL/XRP/BNB/DOGE.
                                   Used for 1h / daily / weekly Up/Down market oracles.
   - ``crypto_prices_chainlink`` — Chainlink oracle price delivered via RTDS WS for all
                                   supported coins.  Stored in ``_chainlink_rtds`` and fires
                                   ``_chainlink_callbacks`` for every coin.  This is the
                                   high-frequency trigger that drives continuous position
                                   monitoring between sparse on-chain AnswerUpdated events.
                                   For HYPE/USD it is also the primary oracle (no on-chain
                                   contract on Polygon).

2. Chainlink On-Chain Oracle (Polygon WSS ``eth_subscribe`` logs)
   Subscribes to ``AnswerUpdated`` events emitted by the Chainlink AggregatorV3
   contracts that Polymarket uses to RESOLVE 5m, 15m, and 4h Up/Down markets.
   THIS IS THE AUTHORITATIVE PRICE — PM reads ``latestAnswer()`` at the exact
   expiry block.  On-chain events fire only on ≥0.5% deviation or ~27 min heartbeat;
   between events the RTDS ``crypto_prices_chainlink`` topic provides resolution-accurate
   Chainlink prices at higher frequency without losing oracle independence.

   Contracts (Polygon Mainnet, 8 decimals):
     BTC/USD  0xc907E116054Ad103354f2D350FD2514433D57F6f
     ETH/USD  0xF9680D99D6C9589e2a93a78A04A279e509205945
     SOL/USD  0x10C8264C0935b3B9870013e057f330Ff3e9C56dC
     XRP/USD  0x785ba89291f676b5386652eB12b30cF361020694
     BNB/USD  0x82a6c4AF830caa6c97bb504425f6A992840839be
     DOGE/USD 0xbaf9327b6564454F4a3364C33eFeEf032b4b4444

Market-type → oracle source mapping (``CHAINLINK_MARKET_TYPES``):
    bucket_5m, bucket_15m, bucket_4h  → on-chain Chainlink (primary) / RTDS CL (fallback)
    bucket_1h, bucket_daily, etc.     → crypto_prices (RTDS exchange-aggregated)

Public API is unchanged — callers use:
    get_mid(coin)            # RTDS exchange-aggregated (non-Chainlink markets)
    get_mid_chainlink(coin)  # on-chain oracle primary, RTDS Chainlink fallback
    on_chainlink_update(cb)  # callback fires on every Chainlink price tick (RTDS or on-chain)
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"


@dataclass
class SpotPrice:
    """Snapshot of the latest RTDS price for one coin."""
    coin: str
    price: float
    timestamp: float = field(default_factory=time.time)   # unix seconds of last update

    @property
    def mid(self) -> float:
        return self.price


# ── Symbol maps ───────────────────────────────────────────────────────────────

# crypto_prices topic: RTDS symbol → bot coin label.
# Symbols are lowercase usdt-pair identifiers as delivered by the RTDS feed.
_RTDS_SYM_TO_COIN: dict[str, str] = {
    "btcusdt":  "BTC",
    "ethusdt":  "ETH",
    "solusdt":  "SOL",
    "xrpusdt":  "XRP",
    "bnbusdt":  "BNB",
    "dogeusdt": "DOGE",
}

# crypto_prices_chainlink topic: Chainlink symbol → bot coin label.
# All tracked coins are available on this topic; symbols use slash/usd format.
# These are the oracle prices Polymarket uses for 5m, 15m, and 4h Up/Down markets.
_CHAINLINK_SYM_TO_COIN: dict[str, str] = {
    "btc/usd":  "BTC",
    "eth/usd":  "ETH",
    "sol/usd":  "SOL",
    "xrp/usd":  "XRP",
    "bnb/usd":  "BNB",
    "doge/usd": "DOGE",
    "hype/usd": "HYPE",
}

# Market types whose resolution oracle is Chainlink on-chain AggregatorV3.
# All other bucket types (bucket_1h, bucket_daily, etc.) use the RTDS
# exchange-aggregated feed (crypto_prices).
CHAINLINK_MARKET_TYPES: frozenset[str] = frozenset({
    "bucket_5m",
    "bucket_15m",
    "bucket_4h",
})

# ── On-chain Chainlink contracts (Polygon Mainnet) ────────────────────────────
# AggregatorV3Interface — each contract emits AnswerUpdated(int256 indexed current,
# uint256 indexed roundId, uint256 updatedAt) whenever a new round is published.
# All pairs use 8 decimal places.
#
# HYPE/USD is intentionally absent — no Chainlink contract on Polygon yet.
# It falls back to the RTDS crypto_prices_chainlink WS topic.
_CHAINLINK_ONCHAIN_CONTRACTS: dict[str, str] = {
    "BTC":  "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL":  "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "XRP":  "0x785ba89291f676b5386652eB12b30cF361020694",
    "BNB":  "0x82a6c4AF830caa6c97bb504425f6A992840839be",
    "DOGE": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
}
# Reverse map: lowercase contract address → coin label (for fast log decode).
_ONCHAIN_ADDR_TO_COIN: dict[str, str] = {
    addr.lower(): coin for coin, addr in _CHAINLINK_ONCHAIN_CONTRACTS.items()
}
_CHAINLINK_DECIMALS: int = 8   # all USD pairs use 8 decimals

# AggregatorV3 AnswerUpdated event topic:
# keccak256("AnswerUpdated(int256,uint256,uint256)")
_ANSWER_UPDATED_TOPIC = (
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
)

class RTDSClient:
    """
    Streams real-time spot prices from two independent sources:

    1. Polymarket RTDS WebSocket — exchange-aggregated ticks (crypto_prices topic)
       for 1h / daily / weekly bucket markets.  Also handles HYPE via the
       crypto_prices_chainlink topic (no on-chain Chainlink contract for HYPE).

    2. Polygon WSS eth_subscribe logs — Chainlink AggregatorV3 AnswerUpdated
       events for BTC/ETH/SOL/XRP/BNB/DOGE.  This is the AUTHORITATIVE on-chain
       price that Polymarket reads at expiry for 5m/15m/4h market resolution.

    Public API is unchanged.  ``get_mid_chainlink()`` returns the on-chain oracle
    price (primary) or the RTDS WS price (HYPE fallback).
    """

    def __init__(self) -> None:
        # Exchange-aggregated prices (crypto_prices RTDS topic).
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        # On-chain Chainlink prices (Polygon AggregatorV3 AnswerUpdated events).
        # PRIMARY source for bucket_5m / bucket_15m / bucket_4h markets.
        self._chainlink_onchain: dict[str, SpotPrice] = {}
        # RTDS WS Chainlink prices (crypto_prices_chainlink topic).
        # FALLBACK — only used for HYPE (no on-chain contract) and as seed
        # before the first on-chain event arrives.
        self._chainlink_rtds: dict[str, SpotPrice] = {}
        self._chainlink_callbacks: list[Callable[[str, float], Coroutine]] = []
        # Coins requested by the bot (from TRACKED_UNDERLYINGS, filtered to RTDS coverage).
        # Populated in start(); messages for other symbols are silently ignored.
        self._tracked_coins: set[str] = set()
        self._ws: Any = None
        self._onchain_ws: Any = None
        self._running = False

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register an async callback(coin, price) fired on every RTDS exchange-aggregated tick."""
        self._callbacks.append(callback)

    def on_chainlink_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register an async callback(coin, price) fired on every Chainlink oracle tick."""
        self._chainlink_callbacks.append(callback)

    # ── Exchange-aggregated (crypto_prices) accessors ─────────────────────────

    def get_mid(self, coin: str) -> Optional[float]:
        """Latest RTDS exchange-aggregated mid for `coin`; None if not received."""
        snap = self._prices.get(coin)
        return snap.price if snap is not None else None

    def get_spot(self, coin: str) -> Optional[SpotPrice]:
        """Latest RTDS SpotPrice snapshot for `coin`; None if not received."""
        return self._prices.get(coin)

    def get_spot_age(self, coin: str) -> float:
        """Seconds since the last RTDS update for `coin`; inf if never seen."""
        snap = self._prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    def all_mids(self) -> dict[str, float]:
        """Return a copy of the current RTDS coin → price dict."""
        return {coin: snap.price for coin, snap in self._prices.items()}

    def all_chainlink_mids(self) -> dict[str, float]:
        """Return a copy of the current Chainlink coin → price dict (on-chain preferred)."""
        result: dict[str, float] = {}
        # Union of both sources; on-chain wins when both are present.
        for coin, snap in self._chainlink_rtds.items():
            result[coin] = snap.price
        for coin, snap in self._chainlink_onchain.items():
            result[coin] = snap.price
        return result

    # ── Chainlink oracle accessors (on-chain primary, RTDS WS fallback) ───────

    def get_mid_chainlink(self, coin: str) -> Optional[float]:
        """Authoritative Chainlink oracle mid: on-chain price when available,
        RTDS WS price as fallback (HYPE).  None if neither has been received."""
        snap = self._chainlink_onchain.get(coin) or self._chainlink_rtds.get(coin)
        return snap.price if snap is not None else None

    def get_spot_chainlink(self, coin: str) -> Optional[SpotPrice]:
        """Full SpotPrice snapshot: on-chain primary, RTDS WS fallback."""
        return self._chainlink_onchain.get(coin) or self._chainlink_rtds.get(coin)

    def get_spot_age_chainlink(self, coin: str) -> float:
        """Seconds since the last on-chain oracle update (primary) or RTDS WS (fallback).
        Note: for BTC/ETH/SOL/XRP/BNB/DOGE the on-chain oracle only updates on
        ≥0.5% price moves or the heartbeat interval — large ages are EXPECTED and
        reflect the true oracle state (i.e. the price PM will use at resolution)."""
        snap = self._chainlink_onchain.get(coin) or self._chainlink_rtds.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    @property
    def tracked_coins(self) -> set[str]:
        """Coins this client expects to receive from RTDS."""
        return set(self._tracked_coins)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to RTDS and start streaming.  Returns immediately; both WS
        loops (RTDS and on-chain Chainlink) run as background asyncio tasks."""
        self._running = True
        # Track any coin that is configured AND covered by either feed.
        all_rtds_coins = set(_RTDS_SYM_TO_COIN.values()) | set(_CHAINLINK_SYM_TO_COIN.values())
        self._tracked_coins = {
            coin for coin in config.TRACKED_UNDERLYINGS if coin in all_rtds_coins
        }
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._onchain_chainlink_loop())
        asyncio.create_task(self._health_log_loop())
        log.info("RTDSClient started", tracked=sorted(self._tracked_coins))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()
        if self._onchain_ws is not None:
            await self._onchain_ws.close()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _health_log_loop(self) -> None:
        """Every 60 s log oracle health across all three price sources.

        For on-chain Chainlink (BTC/ETH/SOL/XRP/BNB/DOGE) large ages are EXPECTED
        (oracle only updates on ≥0.5% deviation) — we log the age separately so
        the operator knows when the last on-chain round was published, which is the
        exact price PM will use at resolution.  We do NOT flag these as stale.
        """
        RTDS_STALE_THRESH = 30.0   # seconds — exchange-aggregated should be very active
        await asyncio.sleep(60)
        while self._running:
            rtds_ages = {
                coin: round(self.get_spot_age(coin), 1)
                for coin in sorted(self._tracked_coins)
            }
            onchain_ages = {
                coin: round(time.time() - snap.timestamp, 1)
                for coin, snap in self._chainlink_onchain.items()
                if coin in self._tracked_coins
            }
            rtds_cl_ages = {
                coin: round(time.time() - snap.timestamp, 1)
                for coin, snap in self._chainlink_rtds.items()
                if coin in self._tracked_coins
            }
            rtds_stale = {c: a for c, a in rtds_ages.items() if a > RTDS_STALE_THRESH}
            if rtds_stale:
                log.warning(
                    "RTDSClient: RTDS exchange prices stale",
                    rtds_stale=rtds_stale,
                    rtds_fresh={c: a for c, a in rtds_ages.items() if c not in rtds_stale},
                    onchain_cl_ages_s=onchain_ages,
                    rtds_cl_fallback_ages_s=rtds_cl_ages,
                )
            else:
                log.info(
                    "RTDSClient: spot prices OK",
                    rtds_ages_s=rtds_ages,
                    onchain_cl_ages_s=onchain_ages,
                    rtds_cl_fallback_ages_s=rtds_cl_ages,
                )
            await asyncio.sleep(60)

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    RTDS_WS_URL,
                    ping_interval=None,   # we send manual PING messages per RTDS spec
                    ping_timeout=None,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("RTDSClient: RTDS WS connected")

                    await self._subscribe(ws)

                    # Start heartbeat — RTDS requires a PING message every 5 s
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        # Use an explicit timeout on each recv() so that a zombie
                        # connection (dead TCP, no close frame) is detected and
                        # causes a reconnect.  We PING every 5 s so we expect at
                        # least a PONG back within 15 s in steady state.
                        while True:
                            try:
                                frame = await asyncio.wait_for(ws.recv(), timeout=15.0)
                            except asyncio.TimeoutError:
                                log.warning(
                                    "RTDSClient: no message received in 15 s — "
                                    "zombie connection, forcing reconnect"
                                )
                                break
                            await self._handle_message(frame)
                    finally:
                        heartbeat_task.cancel()

            except ConnectionClosed as exc:
                log.warning("RTDSClient: RTDS WS disconnected", code=exc.code)
            except Exception as exc:
                log.error("RTDSClient: RTDS WS error", exc=str(exc))
            finally:
                self._ws = None

            if self._running:
                log.info("RTDSClient: reconnecting", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _subscribe(self, ws) -> None:
        """Subscribe to RTDS topics.

        crypto_prices: exchange-aggregated ticks for 1h/daily/weekly oracle markets.
        crypto_prices_chainlink: only needed for HYPE (no on-chain Chainlink contract);
          for all other coins the on-chain oracle loop is authoritative.
        """
        msg = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices",            "type": "update"},
                {"topic": "crypto_prices_chainlink",  "type": "update", "filters": ""},
            ],
        }
        await ws.send(json.dumps(msg))
        log.debug("RTDSClient: subscribed to crypto_prices + crypto_prices_chainlink (HYPE fallback)")

    async def _heartbeat(self, ws) -> None:
        """Send a PING text frame every 5 seconds to maintain the connection."""
        try:
            while True:
                await asyncio.sleep(5)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # ws already closed; the outer loop will reconnect

    async def _handle_message(self, raw: str | bytes | bytearray) -> None:
        # Coerce binary frames to str — RTDS always sends text, but websockets
        # lib typing allows bytes/bytearray/memoryview.
        if not isinstance(raw, str):
            raw = bytes(raw).decode("utf-8")
        if raw == "PONG":
            log.debug("RTDSClient: PONG received")
            return  # heartbeat response; no action needed

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        topic = msg.get("topic")
        msg_type = msg.get("type")

        if msg_type != "update":
            return

        payload = msg.get("payload")
        if not payload:
            return

        if topic == "crypto_prices":
            symbol: str = payload.get("symbol", "").lower()
            coin = _RTDS_SYM_TO_COIN.get(symbol)
        elif topic == "crypto_prices_chainlink":
            symbol = payload.get("symbol", "").lower()
            coin = _CHAINLINK_SYM_TO_COIN.get(symbol)
        else:
            return

        if coin is None or coin not in self._tracked_coins:
            return  # unknown or un-tracked symbol; ignore

        try:
            price = float(payload["value"])
            ts_ms = float(payload.get("timestamp", time.time() * 1000))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("RTDSClient: malformed payload", exc=str(exc), symbol=symbol)
            return

        if price <= 0:
            return

        snap = SpotPrice(coin=coin, price=price, timestamp=ts_ms / 1000.0)

        if topic == "crypto_prices_chainlink":
            # Store in the RTDS fallback dict for all coins.
            # Used by get_mid_chainlink() / get_spot_chainlink() as a fallback when
            # no on-chain event has been received yet, or to bridge between on-chain
            # heartbeats (Chainlink updates on ≥0.5% deviation or ~27 min).
            self._chainlink_rtds[coin] = snap
            log.debug("RTDSClient: chainlink WS update", coin=coin, price=round(price, 6))
            # Fire callbacks for ALL Chainlink coins — this is what keeps the
            # position monitor checking delta SL continuously between on-chain
            # AnswerUpdated events.  The callbacks call _check_position which reads
            # get_mid_chainlink() → on-chain oracle first, RTDS fallback second.
            # The price USED for evaluation is always the correct Chainlink oracle
            # value; RTDS here only provides the trigger frequency.
            for cb in self._chainlink_callbacks:
                try:
                    await cb(coin, price)
                except Exception as exc:
                    log.error("RTDSClient: chainlink WS callback error", exc=str(exc), coin=coin)
        else:
            self._prices[coin] = snap
            log.debug("RTDSClient: price update", coin=coin, price=round(price, 6), source=topic)
            for cb in self._callbacks:
                try:
                    await cb(coin, price)
                except Exception as exc:
                    log.error("RTDSClient: callback error", exc=str(exc), coin=coin)

    # ── On-chain Chainlink oracle (Polygon WSS eth_subscribe logs) ───────────

    async def _onchain_chainlink_loop(self) -> None:
        """Subscribe to Chainlink AggregatorV3 AnswerUpdated events on Polygon via
        a persistent WebSocket (``eth_subscribe`` logs).  Pure event-driven — fires
        ``_chainlink_callbacks`` exactly when PM's resolution oracle posts a new round.

        Reconnects with exponential back-off (1 s → max 60 s).  A 120 s silence on
        an established connection is treated as a stale/zombie socket and triggers
        an immediate reconnect.

        This loop handles BTC/ETH/SOL/XRP/BNB/DOGE.  HYPE (no contract) is handled
        by the RTDS WebSocket loop via ``crypto_prices_chainlink`` topic.
        """
        backoff = 1.0
        addresses = [addr.lower() for addr in _CHAINLINK_ONCHAIN_CONTRACTS.values()]
        # Single subscription request covering all contracts in one filter.
        sub_msg = json.dumps({
            "jsonrpc": "2.0", "id": 1,
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
                    ping_interval=None,   # managed by Polygon node; no manual PING needed
                    open_timeout=15,
                ) as ws:
                    self._onchain_ws = ws
                    backoff = 1.0

                    await ws.send(sub_msg)
                    # First message is the subscription confirmation.
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    resp = json.loads(raw)
                    sub_id = resp.get("result")
                    if not sub_id:
                        # Raise so the except handler logs it and the outer loop
                        # retries with back-off instead of exiting permanently.
                        raise RuntimeError(
                            f"RTDSClient: on-chain Chainlink subscribe failed: {resp}"
                        )

                    log.info(
                        "RTDSClient: on-chain Chainlink subscribed",
                        sub_id=sub_id,
                        coins=sorted(_CHAINLINK_ONCHAIN_CONTRACTS.keys()),
                    )

                    # Event loop: block on recv() with a generous timeout.
                    # We expect a tick every few minutes (heartbeat) at a minimum.
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                        except asyncio.TimeoutError:
                            log.warning(
                                "RTDSClient: on-chain WS silent for 120 s — reconnecting"
                            )
                            break
                        await self._handle_onchain_log(json.loads(raw))

            except ConnectionClosed as exc:
                log.warning("RTDSClient: on-chain WS connection closed", code=exc.code)
            except asyncio.TimeoutError:
                log.warning("RTDSClient: on-chain WS connect timeout")
            except Exception as exc:
                log.error("RTDSClient: on-chain WS error", exc=str(exc))
            finally:
                self._onchain_ws = None

            if self._running:
                log.info("RTDSClient: on-chain WS reconnecting", backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle_onchain_log(self, msg: dict) -> None:
        """Decode a raw ``eth_subscribe`` log notification and update on-chain prices.

        Expected shape::
            {
              "jsonrpc": "2.0",
              "method": "eth_subscription",
              "params": {
                "subscription": "0x…",
                "result": {
                  "address": "0x…",
                  "topics": ["0x0559…", "0x<int256>", "0x<roundId>"],
                  "data": "0x<updatedAt>",
                  …
                }
              }
            }
        """
        params = msg.get("params")
        if not params:
            # Could be a subscription heartbeat or unrelated message; ignore.
            return
        result = params.get("result")
        if not isinstance(result, dict):
            return

        address = result.get("address", "").lower()
        coin = _ONCHAIN_ADDR_TO_COIN.get(address)
        if coin is None or coin not in self._tracked_coins:
            return

        topics = result.get("topics", [])
        if len(topics) < 2:
            return

        # topics[1] = int256 indexed current price (32 bytes, big-endian signed).
        try:
            raw_int = int.from_bytes(
                bytes.fromhex(topics[1][2:].zfill(64)), byteorder="big", signed=True
            )
            price = raw_int / (10 ** _CHAINLINK_DECIMALS)
        except (ValueError, IndexError) as exc:
            log.warning("RTDSClient: on-chain log decode error", exc=str(exc), topics=topics[:2])
            return

        if price <= 0:
            log.warning("RTDSClient: on-chain log non-positive price", coin=coin, price=price)
            return

        snap = SpotPrice(coin=coin, price=price, timestamp=time.time())
        self._chainlink_onchain[coin] = snap
        log.debug("RTDSClient: on-chain Chainlink update", coin=coin, price=round(price, 6))

        # Fire callbacks — same callbacks used by the RTDS WS path for HYPE.
        # monitor.py's _on_spot_update checks stop-loss conditions here.
        for cb in self._chainlink_callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error("RTDSClient: on-chain callback error", exc=str(exc), coin=coin)
