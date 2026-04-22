"""
rtds_client.py — Polymarket RTDS exchange-aggregated price feed.

Connects to the Polymarket RTDS WebSocket (wss://ws-live-data.polymarket.com)
and subscribes to two topics on the same connection:

  crypto_prices            — exchange-aggregated spot prices for BTC/ETH/SOL/XRP/BNB/DOGE/HYPE.
  crypto_prices_chainlink  — Chainlink Data Stream push for HYPE/USD (and others).

This feed is used for:
  - 1h / daily / weekly Up/Down market oracle (resolution oracle for these types).
  - Realized-volatility estimation (VolFetcher).
  - Maker strategy spot pricing.
  - HYPE/USD Chainlink oracle for 5m/15m/4h HYPE markets (crypto_prices_chainlink topic).

5m / 15m / 4h non-HYPE markets resolve against on-chain Chainlink AggregatorV3 — see
market_data/chainlink_ws_client.py for that feed.
Oracle routing is handled by market_data/spot_oracle.py (SpotOracle facade).

Public API:
    get_mid(coin)          → Optional[float]
    get_spot(coin)         → Optional[SpotPrice]
    get_spot_age(coin)     → float
    all_mids()             → dict[str, float]
    on_price_update(cb)    → None  (callback fires on every RTDS tick)
    tracked_coins          → set[str]
    start() / stop()
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
    "hypeusdt": "HYPE",
}

# crypto_prices_chainlink topic: slash-delimited symbol → bot coin label.
# Polymarket routes its Chainlink Data Streams push through this topic for all tracked
# underlyings.  HYPE uses Data Streams (no AggregatorV3 on Polygon); the rest mirror
# their on-chain AggregatorV3 values.
_RTDS_CL_SYM_TO_COIN: dict[str, str] = {
    "btc/usd":  "BTC",
    "eth/usd":  "ETH",
    "sol/usd":  "SOL",
    "xrp/usd":  "XRP",
    "bnb/usd":  "BNB",
    "doge/usd": "DOGE",
    "hype/usd": "HYPE",
}


class RTDSClient:
    """
    Streams real-time spot prices from the Polymarket RTDS WebSocket
    (wss://ws-live-data.polymarket.com) via two subscriptions on one connection:

    - ``crypto_prices`` (exchange-aggregated): BTC/ETH/SOL/XRP/BNB/DOGE/HYPE
      Used for 1h/daily/weekly market resolution.
    - ``crypto_prices_chainlink`` (Chainlink Data Stream push): HYPE/USD
      Polymarket's own Chainlink Data Streams relay — sub-second, no API key
      required.  Used for 5m/15m/4h HYPE Up/Down market oracle.

    Oracle routing is handled by SpotOracle — see market_data/spot_oracle.py.
    """

    def __init__(self) -> None:
        # Exchange-aggregated prices (crypto_prices RTDS topic).
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        # Chainlink Data Stream prices relayed via crypto_prices_chainlink RTDS topic.
        self._cl_prices: dict[str, SpotPrice] = {}
        self._cl_callbacks: list[Callable[[str, float], Coroutine]] = []
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
        """Register async callback(coin, price) fired on every crypto_prices_chainlink tick.

        Fires for all coins tracked in _RTDS_CL_SYM_TO_COIN (BTC/ETH/SOL/XRP/BNB/DOGE/HYPE).
        Used by SpotOracle so the position monitor re-evaluates on every Chainlink relay
        event exactly like it does for AggregatorV3 events from ChainlinkWSClient.
        """
        self._cl_callbacks.append(callback)

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

    # ── Chainlink-via-RTDS (crypto_prices_chainlink) accessors ────────────────

    def get_chainlink_mid(self, coin: str) -> Optional[float]:
        """Latest Chainlink price for `coin` as received via the RTDS relay; None if unseen."""
        snap = self._cl_prices.get(coin)
        return snap.price if snap is not None else None

    def get_chainlink_spot(self, coin: str) -> Optional[SpotPrice]:
        """Latest Chainlink SpotPrice snapshot relayed through RTDS; None if unseen."""
        return self._cl_prices.get(coin)

    def get_chainlink_age(self, coin: str) -> float:
        """Seconds since the last crypto_prices_chainlink event for `coin`; inf if never seen."""
        snap = self._cl_prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    @property
    def tracked_coins(self) -> set[str]:
        """Coins this client expects to receive from RTDS."""
        return set(self._tracked_coins)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to RTDS and start streaming.  Returns immediately; the WS
        loop runs as a background asyncio task."""
        self._running = True
        # Track coins that are in TRACKED_UNDERLYINGS and covered by this feed.
        self._tracked_coins = {
            coin for coin in config.TRACKED_UNDERLYINGS
            if coin in set(_RTDS_SYM_TO_COIN.values())
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
        """Every 60 s log RTDS feed health."""
        RTDS_STALE_THRESH = 30.0   # seconds — exchange-aggregated should be very active
        await asyncio.sleep(60)
        while self._running:
            rtds_ages = {
                coin: round(self.get_spot_age(coin), 1)
                for coin in sorted(self._tracked_coins)
            }
            rtds_stale = {c: a for c, a in rtds_ages.items() if a > RTDS_STALE_THRESH}
            if rtds_stale:
                log.warning(
                    "RTDSClient: RTDS exchange prices stale",
                    rtds_stale=rtds_stale,
                    rtds_fresh={c: a for c, a in rtds_ages.items() if c not in rtds_stale},
                )
            else:
                log.info(
                    "RTDSClient: spot prices OK",
                    rtds_ages_s=rtds_ages,
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
        """Subscribe to crypto_prices (Binance-aggregated) and crypto_prices_chainlink (all coins)."""
        msg = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices", "type": "update"},
                {"topic": "crypto_prices_chainlink", "type": "update"},
            ],
        }
        await ws.send(json.dumps(msg))
        log.debug("RTDSClient: subscribed to crypto_prices and crypto_prices_chainlink")

    async def _heartbeat(self, ws) -> None:
        """Send a PING text frame every 5 seconds to maintain the connection."""
        try:
            while True:
                await asyncio.sleep(5)
                await ws.send("PING")
        except Exception:
            pass

    async def _handle_message(self, raw: str) -> None:
        """Parse one RTDS frame and update cached prices."""
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
            await self._handle_crypto_prices(payload)
        elif topic == "crypto_prices_chainlink":
            await self._handle_crypto_prices_chainlink(payload)

    async def _handle_crypto_prices(self, payload: dict) -> None:
        """Handle one crypto_prices (exchange-aggregated) payload."""
        symbol: str = payload.get("symbol", "").lower()
        coin = _RTDS_SYM_TO_COIN.get(symbol)

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
        self._prices[coin] = snap
        log.debug("RTDSClient: price update", coin=coin, price=round(price, 6))
        for cb in self._callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error("RTDSClient: callback error", exc=str(exc), coin=coin)

    async def _handle_crypto_prices_chainlink(self, payload: dict) -> None:
        """Handle one crypto_prices_chainlink (Chainlink Data Stream relay) payload.

        Payload shape::

            {
              "symbol": "hype/usd",
              "timestamp": 174XXXXXX,   # oracle timestamp (unix seconds)
              "value": 12.3456
            }
        """
        symbol: str = payload.get("symbol", "").lower()
        coin = _RTDS_CL_SYM_TO_COIN.get(symbol)
        if coin is None:
            return

        try:
            price = float(payload["value"])
            ts = float(payload.get("timestamp", time.time()))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("RTDSClient: malformed chainlink payload", exc=str(exc), symbol=symbol)
            return

        if price <= 0:
            return

        snap = SpotPrice(coin=coin, price=price, timestamp=ts)
        self._cl_prices[coin] = snap
        log.debug("RTDSClient: chainlink update", coin=coin, price=round(price, 6))
        for cb in self._cl_callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.error("RTDSClient: chainlink callback error", exc=str(exc), coin=coin)

    # ── On-chain Chainlink oracle loop removed ────────────────────────────────
    # Moved to market_data/chainlink_poll_client.py (HTTP polling via eth_call).