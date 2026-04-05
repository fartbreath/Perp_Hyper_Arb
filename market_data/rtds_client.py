"""
rtds_client.py — Polymarket Real-Time Data Socket (RTDS) price feed client.

Connects to the Polymarket RTDS WebSocket (wss://ws-live-data.polymarket.com)
and subscribes to two topics in a single connection:
  - ``crypto_prices``           — RTDS default topic (BTC, ETH, SOL, XRP, BNB, DOGE).
                                  Exchange-aggregated prices.  Used for 1h / daily /
                                  weekly Up/Down markets.
  - ``crypto_prices_chainlink`` — Chainlink oracle prices for ALL tracked coins.
                                  Polymarket resolves 5m, 15m, and 4h Up/Down markets
                                  against this feed.  Use ``get_mid_chainlink()`` /
                                  ``get_spot_chainlink()`` for delta-SL and strike
                                  calculations on those market types.

Market-type → oracle source mapping (``CHAINLINK_MARKET_TYPES``):
    bucket_5m, bucket_15m, bucket_4h  → crypto_prices_chainlink (Chainlink)
    bucket_1h, bucket_daily, etc.     → crypto_prices (RTDS exchange-aggregated)

Usage:
    client = RTDSClient()
    await client.start()
    price = client.get_mid("ETH")             # RTDS exchange-aggregated
    price = client.get_mid_chainlink("BNB")   # Chainlink oracle
    age   = client.get_spot_age("ETH")        # seconds since last RTDS update
    client.on_price_update(async_callback)    # callback(coin, price) — RTDS ticks
    client.on_chainlink_update(async_callback)# callback(coin, price) — Chainlink ticks
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
    "linkusdt": "LINK",
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

# Market types whose resolution oracle is Chainlink (crypto_prices_chainlink).
# All other bucket types (bucket_1h, bucket_daily, etc.) use the RTDS
# exchange-aggregated feed (crypto_prices).
CHAINLINK_MARKET_TYPES: frozenset[str] = frozenset({
    "bucket_5m",
    "bucket_15m",
    "bucket_4h",
})

class RTDSClient:
    """
    Streams real-time spot prices from Polymarket's own RTDS WebSocket.

    Subscribes to two topics:
    - ``crypto_prices`` — exchange-aggregated RTDS prices for BTC/ETH/SOL/XRP/BNB/DOGE/LINK.
      Used as the oracle for 1h, daily, and weekly bucket markets.
    - ``crypto_prices_chainlink`` — Chainlink oracle prices for BTC/ETH/SOL/XRP/BNB/DOGE/HYPE.
      Used as the oracle for 5m, 15m, and 4h bucket markets (``CHAINLINK_MARKET_TYPES``).

    Each topic is cached separately.  Callers must use the appropriate method:
    ``get_mid`` / ``get_spot`` for RTDS markets, and
    ``get_mid_chainlink`` / ``get_spot_chainlink`` for Chainlink markets.

    Reconnection is automatic with exponential back-off (max 30 s).
    A PING frame is sent every 5 seconds to keep the connection alive.
    """

    def __init__(self) -> None:
        # Exchange-aggregated prices (crypto_prices topic).
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        # Chainlink oracle prices (crypto_prices_chainlink topic).
        self._chainlink_prices: dict[str, SpotPrice] = {}
        self._chainlink_callbacks: list[Callable[[str, float], Coroutine]] = []
        # Coins requested by the bot (from TRACKED_UNDERLYINGS, filtered to RTDS coverage).
        # Populated in start(); messages for other symbols are silently ignored.
        self._tracked_coins: set[str] = set()
        self._ws: Any = None
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

    # ── Chainlink oracle (crypto_prices_chainlink) accessors ──────────────────

    def get_mid_chainlink(self, coin: str) -> Optional[float]:
        """Latest Chainlink oracle mid for `coin`; None if not received yet."""
        snap = self._chainlink_prices.get(coin)
        return snap.price if snap is not None else None

    def get_spot_chainlink(self, coin: str) -> Optional[SpotPrice]:
        """Latest Chainlink SpotPrice snapshot for `coin`; None if not received."""
        return self._chainlink_prices.get(coin)

    def get_spot_age_chainlink(self, coin: str) -> float:
        """Seconds since the last Chainlink tick for `coin`; inf if never seen."""
        snap = self._chainlink_prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    @property
    def tracked_coins(self) -> set[str]:
        """Coins this client expects to receive from RTDS."""
        return set(self._tracked_coins)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to RTDS and start streaming.  Returns immediately; the WS
        loop and heartbeat run as background asyncio tasks."""
        self._running = True
        # Track any coin that is configured AND covered by either feed.
        all_rtds_coins = set(_RTDS_SYM_TO_COIN.values()) | set(_CHAINLINK_SYM_TO_COIN.values())
        self._tracked_coins = {
            coin for coin in config.TRACKED_UNDERLYINGS if coin in all_rtds_coins
        }
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._health_log_loop())
        log.info("RTDSClient started", tracked=sorted(self._tracked_coins))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _health_log_loop(self) -> None:
        """Every 60 s log the age of each tracked coin's spot price (both feeds).

        Logs INFO when all coins are fresh (age < 30 s).
        Logs WARNING when any coin is stale, naming which coins and their ages.
        This makes oracle outages visible in the log without needing DEBUG level.
        """
        STALE_THRESH = 30.0   # seconds — matches MOMENTUM_SPOT_MAX_AGE_SECS
        await asyncio.sleep(60)   # first check after 60 s so startup noise settles
        while self._running:
            rtds_ages = {
                coin: round(self.get_spot_age(coin), 1)
                for coin in sorted(self._tracked_coins)
            }
            cl_ages = {
                coin: round(self.get_spot_age_chainlink(coin), 1)
                for coin in sorted(self._tracked_coins)
                if coin in _CHAINLINK_SYM_TO_COIN.values()
            }
            rtds_stale = {c: a for c, a in rtds_ages.items() if a > STALE_THRESH}
            cl_stale   = {c: a for c, a in cl_ages.items()   if a > STALE_THRESH}
            if rtds_stale or cl_stale:
                log.warning(
                    "RTDSClient: stale spot prices",
                    rtds_stale=rtds_stale or None,
                    chainlink_stale=cl_stale or None,
                    rtds_fresh={c: a for c, a in rtds_ages.items() if c not in rtds_stale},
                    chainlink_fresh={c: a for c, a in cl_ages.items() if c not in cl_stale},
                )
            else:
                log.info("RTDSClient: spot prices OK", rtds_ages_s=rtds_ages, cl_ages_s=cl_ages)
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
                                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                            except asyncio.TimeoutError:
                                log.warning(
                                    "RTDSClient: no message received in 15 s — "
                                    "zombie connection, forcing reconnect"
                                )
                                break
                            await self._handle_message(raw)
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
        """Subscribe to both RTDS topics in a single message.

        crypto_prices (RTDS): no filter needed — receive all symbols and
        filter client-side.  The per-symbol filter format caused 400 errors.

        crypto_prices_chainlink (Chainlink): empty string filter delivers all symbols;
        per-symbol JSON filter only returns a historical backfill snapshot,
        then stops sending live ticks — so we subscribe to all and filter
        client-side for HYPE.
        """
        msg = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices",            "type": "update"},
                {"topic": "crypto_prices_chainlink",  "type": "update", "filters": ""},
            ],
        }
        await ws.send(json.dumps(msg))
        log.debug("RTDSClient: subscribed to crypto_prices + crypto_prices_chainlink")

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

    async def _handle_message(self, raw: str) -> None:
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
            self._chainlink_prices[coin] = snap
            log.debug("RTDSClient: chainlink update", coin=coin, price=round(price, 6))
            for cb in self._chainlink_callbacks:
                try:
                    await cb(coin, price)
                except Exception as exc:
                    log.error("RTDSClient: chainlink callback error", exc=str(exc), coin=coin)
        else:
            self._prices[coin] = snap
            log.debug("RTDSClient: price update", coin=coin, price=round(price, 6), source=topic)
            for cb in self._callbacks:
                try:
                    await cb(coin, price)
                except Exception as exc:
                    log.error("RTDSClient: callback error", exc=str(exc), coin=coin)
