"""
pyth_client.py — Pyth Hermes WebSocket price feed client.

Connects to the Pyth Hermes WebSocket endpoint and subscribes to the
configured price feed IDs.  Maintains a real-time per-coin price cache
that is the authoritative spot price source for momentum stop-loss and
entry-delta calculations.

Polymarket "Up or Down" bucket markets resolve against the actual spot
price at their end_date.  Using the Pyth oracle — the same data source
that underpins Binance spot, Chainlink Data Streams, and other oracles
Polymarket references — guarantees that the bot's delta calculations
are on-chain-consistent rather than tracking HL perp, which carries
funding-rate basis and can diverge from the settlement price at expiry.

Usage:
    client = PythClient()
    await client.start()
    price = client.get_mid("ETH")   # Optional[float], None if no data yet
    age   = client.get_spot_age("ETH")  # seconds since last Pyth update
    client.on_price_update(async_callback)  # callback(coin: str, price: float)
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

HERMES_WS_URL = "wss://hermes.pyth.network/ws"


@dataclass
class SpotPrice:
    """Snapshot of the latest Pyth price for one coin."""
    coin: str
    price: float
    timestamp: float = field(default_factory=time.time)   # unix time of last Pyth update

    @property
    def mid(self) -> float:
        return self.price


class PythClient:
    """
    Streams real-time spot prices from the Pyth Hermes WebSocket.

    Maintains an in-memory `SpotPrice` cache keyed by coin symbol.  The
    monitor and scanner read prices synchronously via `get_mid()`; they
    register async callbacks via `on_price_update()` for event-driven
    position checks triggered by each incoming Pyth tick.

    Reconnection is automatic with exponential back-off (max 30 s).
    """

    def __init__(self) -> None:
        # coin → SpotPrice; populated on first Pyth tick, updated every ~400 ms
        self._prices: dict[str, SpotPrice] = {}
        # feed_id (without 0x prefix, lower-case) → coin symbol
        self._feed_to_coin: dict[str, str] = {
            fid.lower().lstrip("0x"): coin
            for coin, fid in config.PYTH_PRICE_FEED_IDS.items()
        }
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        self._ws: Any = None
        self._running = False

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register an async callback(coin, price) fired on every Pyth tick."""
        self._callbacks.append(callback)

    def get_mid(self, coin: str) -> Optional[float]:
        """Latest Pyth mid price for `coin`; None if no data received yet."""
        snap = self._prices.get(coin)
        return snap.price if snap is not None else None

    def get_spot(self, coin: str) -> Optional[SpotPrice]:
        """Latest SpotPrice snapshot for `coin`; None if no data received yet."""
        return self._prices.get(coin)

    def get_spot_age(self, coin: str) -> float:
        """Seconds since the last Pyth update for `coin`; inf if never seen."""
        snap = self._prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    def all_mids(self) -> dict[str, float]:
        """Return a copy of the current coin → price dict."""
        return {coin: snap.price for coin, snap in self._prices.items()}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Hermes WS and start streaming.  Returns immediately;
        the WS loop runs as a background asyncio task."""
        self._running = True
        asyncio.create_task(self._ws_loop())
        log.info("PythClient started", feeds=list(config.PYTH_PRICE_FEED_IDS.keys()))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    HERMES_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("PythClient: Hermes WS connected")

                    await self._subscribe(ws)

                    async for raw in ws:
                        await self._handle_message(raw)

            except ConnectionClosed as exc:
                log.warning("PythClient: Hermes WS disconnected", code=exc.code)
            except Exception as exc:
                log.error("PythClient: Hermes WS error", exc=str(exc))
            finally:
                self._ws = None

            if self._running:
                log.info("PythClient: reconnecting", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _subscribe(self, ws) -> None:
        """Send the subscribe message for all configured feed IDs."""
        ids = [
            f"0x{fid.lower().lstrip('0x')}"
            for fid in config.PYTH_PRICE_FEED_IDS.values()
        ]
        msg = {"ids": ids, "type": "subscribe"}
        await ws.send(json.dumps(msg))
        log.debug("PythClient: subscribed to feeds", count=len(ids))

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type == "response":
            # Subscription acknowledgement — log and ignore
            status = msg.get("status", "")
            if status != "success":
                log.warning("PythClient: unexpected subscribe response", msg=msg)
            return

        if msg_type != "price_update":
            return

        feed_data = msg.get("price_feed")
        if not feed_data:
            return

        feed_id = feed_data.get("id", "").lower().lstrip("0x")
        coin = self._feed_to_coin.get(feed_id)
        if coin is None:
            return  # unrecognised feed

        price_info = feed_data.get("price", {})
        try:
            raw_price = int(price_info["price"])
            expo = int(price_info["expo"])
            price = raw_price * (10.0 ** expo)
            publish_time = float(price_info.get("publish_time", time.time()))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("PythClient: malformed price_update", exc=str(exc), coin=coin)
            return

        if price <= 0:
            return  # Pyth publishes 0 for unavailable feeds; skip

        snap = SpotPrice(coin=coin, price=price, timestamp=publish_time)
        self._prices[coin] = snap

        log.debug("PythClient: price update", coin=coin, price=round(price, 6))

        for cb in self._callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error("PythClient: callback error", exc=str(exc), coin=coin)
