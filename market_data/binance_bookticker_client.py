"""
market_data/binance_bookticker_client.py — Binance bookTicker WebSocket feed.

Subscribes to the Binance combined bookTicker stream for all tracked coins
via one persistent WebSocket connection:

    Spot (BTC/ETH/SOL/XRP/BNB/DOGE):
        wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/...

    bookTicker fires on every best-bid or best-ask change — real-time, no
    throttle.  Observed rates: BTC 130/s, ETH 240/s, SOL 30/s, DOGE 40/s.
    Mid price = (best_bid + best_ask) / 2.

HYPE is listed on Binance Futures (HYPEUSDT perp) but not on spot.
For HYPE we use REST polling from fapi.binance.com every 2 s because the
futures WebSocket (fstream.binance.com) is silently filtered on some networks.

Why bookTicker instead of aggTrade?
    aggTrade fires only on executed trades.  On lower-volume spot pairs
    (SOL, XRP, BNB) this produces multi-second gaps — SOL was stale 65% of
    the time in a 10-minute live test.  bookTicker fires on every quote
    change, giving continuous price discovery with far fewer gaps.

Why use this instead of RTDSClient for 1h/daily/weekly?
    RTDS crypto_prices is a throttled relay of Binance aggTrade, lagging the
    live market by 136–744 ms (p50) and up to 5700 ms (p99 on BNB) as
    measured in a live 10-minute test.  Direct bookTicker has no such lag.

Public API (mirrors RTDSClient interface):
    get_spot(coin)          → Optional[SpotPrice]
    get_spot_age(coin)      → float
    get_mid(coin)           → Optional[float]
    on_price_update(cb)     → None   (async callback(coin, price) on each event)
    tracked_coins           → set[str]
    start() / stop()

No API key required.  Binance market-data WebSocket streams are public.
"""
from __future__ import annotations

import asyncio
import functools
import json
import time
import urllib.request
from typing import Any, Callable, Coroutine, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

import config
from logger import get_bot_logger
from market_data.rtds_client import SpotPrice

log = get_bot_logger(__name__)

# Binance spot combined stream base URL.
_BINANCE_SPOT_BASE = "wss://stream.binance.com:9443/stream"

# Binance USD-M futures REST base URL (for HYPE, which has a perp
# but no spot listing on Binance).  We use REST polling rather than
# the futures WebSocket (fstream.binance.com) because the futures WS
# host is silently filtered on some networks — the connection handshake
# succeeds but no stream frames are delivered.  REST via fapi.binance.com
# over port 443 HTTPS is universally accessible and gives ~2 s updates.
_BINANCE_FUTURES_REST = "https://fapi.binance.com/fapi/v1/ticker/price"

# REST poll interval for futures coins (seconds).
_FUTURES_REST_POLL_SECS: float = 2.0

# Proactive WS reconnect before Binance's hard 24-hour connection limit.
# Binance docs: "A single connection is only valid for 24 hours; expect to be
# disconnected at the 24 hour mark."
_MAX_WS_AGE_SECS: float = 23 * 3600

# Binance USDT-pair symbol (lowercase) → bot coin label.
# NOTE: HYPE (HyperLiquid) has no Binance spot pair — it trades on the
# USD-M futures market as HYPEUSDT perp.  Its price is fetched via REST
# (/fapi/v1/ticker/price) and stored the same way as bookTicker mid prices.
_SYM_TO_COIN: dict[str, str] = {
    "btcusdt":  "BTC",
    "ethusdt":  "ETH",
    "solusdt":  "SOL",
    "xrpusdt":  "XRP",
    "bnbusdt":  "BNB",
    "dogeusdt": "DOGE",
    "hypeusdt": "HYPE",   # futures perp — stream from fstream.binance.com
}
_COIN_TO_SYM: dict[str, str] = {v: k for k, v in _SYM_TO_COIN.items()}

# Coins that must be fetched from the futures stream rather than spot.
_FUTURES_COINS: frozenset[str] = frozenset({"HYPE"})


class BinanceBookTickerClient:
    """
    Streams real-time Binance prices for all tracked coins.

    Two data acquisition methods run in parallel:
      - Spot WebSocket  (stream.binance.com:9443): BTC/ETH/SOL/XRP/BNB/DOGE
        bookTicker events fire on every best-bid/ask change — real-time,
        no throttle.  Mid = (best_bid + best_ask) / 2.
      - Futures REST poll (fapi.binance.com, every 2 s): HYPE (perp-only)
        The futures WebSocket (fstream.binance.com) connects but is silently
        filtered on some networks; REST is universally accessible.

    Both methods update the same ``_prices`` dict and fire the same callbacks,
    so callers are unaware of which source a price came from.
    On reconnect/restart, last known prices are preserved — callers see STALE
    status, never DOWN, during any gap window.
    """

    def __init__(self) -> None:
        self._prices: dict[str, SpotPrice] = {}
        self._callbacks: list[Callable[[str, float], Coroutine]] = []
        self._tracked_coins: set[str] = set()
        # Track last-data timestamps separately per stream for health logging.
        self._last_data_ts: dict[str, float] = {}
        self._running = False

    # ── Public interface ──────────────────────────────────────────────────────

    def on_price_update(
        self, callback: Callable[[str, float], Coroutine]
    ) -> None:
        """Register an async callback(coin, price) fired on every bookTicker event."""
        self._callbacks.append(callback)

    def get_spot(self, coin: str) -> Optional[SpotPrice]:
        """Latest Binance SpotPrice for ``coin``; None if not yet received."""
        return self._prices.get(coin)

    def get_spot_age(self, coin: str) -> float:
        """Seconds since the last Binance update for ``coin``; inf if never seen."""
        snap = self._prices.get(coin)
        return time.time() - snap.timestamp if snap is not None else float("inf")

    def get_mid(self, coin: str) -> Optional[float]:
        """Latest Binance price as float; None if not yet received."""
        snap = self._prices.get(coin)
        return snap.price if snap is not None else None

    @property
    def tracked_coins(self) -> set[str]:
        """Coins this client subscribes to (subset of TRACKED_UNDERLYINGS)."""
        return set(self._tracked_coins)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Binance and start streaming.
        Returns immediately; the WS loops run as background tasks."""
        self._running = True
        self._tracked_coins = {
            coin for coin in config.TRACKED_UNDERLYINGS
            if coin in _COIN_TO_SYM
        }
        spot_coins    = self._tracked_coins - _FUTURES_COINS
        futures_coins = self._tracked_coins & _FUTURES_COINS
        if spot_coins:
            asyncio.create_task(
                self._ws_loop(_BINANCE_SPOT_BASE, spot_coins, "spot"),
                name="binance_bookticker_spot",
            )
        if futures_coins:
            # Use REST polling rather than futures WS — fstream.binance.com
            # delivers no data on some networks despite a successful handshake.
            asyncio.create_task(
                self._rest_poll_loop(futures_coins),
                name="binance_bookticker_futures_rest",
            )
        asyncio.create_task(self._health_loop(), name="binance_bookticker_health")
        log.info(
            "BinanceBookTickerClient started",
            spot_ws=sorted(spot_coins),
            futures_rest=sorted(futures_coins),
        )

    async def stop(self) -> None:
        """Gracefully stop both WebSocket loops."""
        self._running = False

    # ── REST polling loop (HYPE / futures-only coins) ───────────────────────

    async def _rest_poll_loop(self, coins: set[str]) -> None:
        """Poll Binance futures REST API for each coin in ``coins``.

        Calls ``GET /fapi/v1/ticker/price?symbol=<SYM>USDT`` every
        ``_FUTURES_REST_POLL_SECS`` seconds in a thread-pool executor so
        the event loop is never blocked.  Updates ``_prices`` and fires
        all registered callbacks exactly like the spot WS path does.
        """
        pairs = [
            (coin, _COIN_TO_SYM[coin].upper())
            for coin in sorted(coins)
            if coin in _COIN_TO_SYM
        ]
        if not pairs:
            return
        log.info(
            "BinanceBookTickerClient: futures REST poller started",
            coins=[c for c, _ in pairs],
            interval_s=_FUTURES_REST_POLL_SECS,
        )

        def _fetch(symbol: str) -> Optional[float]:
            """Blocking REST fetch; runs in executor."""
            try:
                url = f"{_BINANCE_FUTURES_REST}?symbol={symbol}"
                req = urllib.request.Request(
                    url, headers={"User-Agent": "BinanceBookTickerClient/1.0"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                return float(data["price"])
            except Exception as exc:
                log.debug(
                    "BinanceBookTickerClient: REST fetch error",
                    symbol=symbol,
                    exc=str(exc),
                )
                return None

        loop = asyncio.get_event_loop()
        backoff = 1.0

        while self._running:
            any_ok = False
            for coin, symbol in pairs:
                price = await loop.run_in_executor(
                    None, functools.partial(_fetch, symbol)
                )
                if price is not None:
                    now = time.time()
                    self._last_data_ts["rest"] = now
                    self._prices[coin] = SpotPrice(
                        coin=coin, price=price, timestamp=now
                    )
                    any_ok = True
                    for cb in self._callbacks:
                        try:
                            await cb(coin, price)
                        except Exception as exc:
                            log.debug(
                                "BinanceBookTickerClient: REST callback error",
                                exc=str(exc),
                            )

            # Slow down only if everything failed (e.g. network outage).
            if any_ok:
                backoff = 1.0
                await asyncio.sleep(_FUTURES_REST_POLL_SECS)
            else:
                log.warning(
                    "BinanceBookTickerClient: REST poll failed, backing off",
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── WebSocket loop (─────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        """Every 30 s log per-coin age; warn on stale feeds."""
        STALE_THRESH = 30.0
        await asyncio.sleep(30)
        while self._running:
            ages = {
                coin: round(self.get_spot_age(coin), 1)
                for coin in sorted(self._tracked_coins)
            }
            stale = {c: a for c, a in ages.items()
                     if a > STALE_THRESH and a != float("inf")}
            if stale:
                log.warning(
                    "BinanceBookTickerClient: prices stale",
                    stale=stale,
                    fresh={c: a for c, a in ages.items() if c not in stale},
                )
            else:
                log.info("BinanceBookTickerClient: prices OK", ages_s=ages)
            await asyncio.sleep(30)

    async def _ws_loop(
        self, base_url: str, coins: set[str], label: str
    ) -> None:
        """Reconnect loop for one stream endpoint (spot or futures).

        ``label`` is used only for log messages to distinguish the two loops.
        Exponential backoff 1 s → 60 s on errors.
        """
        backoff = 1.0
        while self._running:
            url = self._build_url(base_url, coins)
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    connect_time = time.time()
                    self._last_data_ts[label] = connect_time
                    log.info(
                        "BinanceBookTickerClient: connected",
                        stream=label,
                        coins=sorted(coins),
                    )
                    # Reconnect only on: 23h proactive timer, serverShutdown, or
                    # true connection drop (ConnectionClosed / ping-pong timeout).
                    # Per-coin silence is NOT a reconnect trigger — bookTicker fires
                    # only on spread changes, so low-volume coins (XRP, SOL) naturally
                    # go quiet for multi-second stretches in calm markets.  This is
                    # documented Binance behaviour; Binance's own connector does the
                    # same (23h timer + serverShutdown only).
                    while True:
                        # Proactive reconnect before Binance's hard 24 h limit.
                        if time.time() - connect_time >= _MAX_WS_AGE_SECS:
                            log.info(
                                "BinanceBookTickerClient: 23h proactive reconnect",
                                stream=label,
                            )
                            break
                        try:
                            frame = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # 5 s with no frame at all — even BTC stopped. True zombie.
                            # BTC streams ~130 msg/s so this never fires on a live connection.
                            # Reconnect fast: oracle falls back to RTDS (p99 5700ms lag) after
                            # BINANCE_BOOKTICKER_STALE_SECS=10s — minimise time on that fallback.
                            log.warning(
                                "BinanceBookTickerClient: no message in 5 s — reconnecting",
                                stream=label,
                            )
                            break
                        await self._handle_message(frame, label)

            except ConnectionClosed as exc:
                log.warning(
                    "BinanceBookTickerClient: WS disconnected", stream=label, code=exc.code
                )
            except AttributeError as exc:
                # Transport teardown race: websockets internal AttributeError on
                # resume_reading() during close/reconnect cycle.  Treat as a
                # normal disconnect — reconnect without ERROR log noise.
                log.warning(
                    "BinanceBookTickerClient: WS transport race — reconnecting",
                    stream=label,
                    exc=str(exc),
                )
            except Exception as exc:
                log.error("BinanceBookTickerClient: WS error", stream=label, exc=str(exc))

            if self._running:
                log.info("BinanceBookTickerClient: reconnecting", stream=label, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _build_url(self, base_url: str, coins: set[str]) -> str:
        """Build a Binance combined bookTicker stream URL for the given coins."""
        streams = [
            f"{_COIN_TO_SYM[coin]}@bookTicker"
            for coin in sorted(coins)
            if coin in _COIN_TO_SYM
        ]
        if not streams:
            streams = ["btcusdt@bookTicker"]
        return f"{base_url}?streams={'/'.join(streams)}"

    async def _handle_message(self, raw: Union[str, bytes], label: str = "") -> None:
        """Parse one Binance combined bookTicker frame and update cached prices."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        # Combined stream envelope: {"stream": "btcusdt@bookTicker", "data": {...}}
        # bookTicker payload fields: s=symbol, b=best_bid, B=bid_qty, a=best_ask, A=ask_qty
        data = msg.get("data", msg)
        symbol: str = data.get("s", "").lower()
        coin = _SYM_TO_COIN.get(symbol)
        if coin is None or coin not in self._tracked_coins:
            return

        try:
            # Mid price from best bid/ask — more appropriate than last-trade
            # price for oracle use; avoids one-sided trade price noise.
            price = (float(data["b"]) + float(data["a"])) / 2
        except (KeyError, ValueError, TypeError):
            return

        now = time.time()
        self._last_data_ts[label] = now
        self._prices[coin] = SpotPrice(coin=coin, price=price, timestamp=now)

        for cb in self._callbacks:
            try:
                await cb(coin, price)
            except Exception as exc:
                log.debug("BinanceBookTickerClient: callback error", exc=str(exc))
