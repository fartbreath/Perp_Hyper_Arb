"""
hl_client.py — Hyperliquid Info + Exchange wrapper.

Responsibilities:
  - WebSocket subscriptions for BBO + allMids (real-time price feed)
  - Dead man's switch (schedule_cancel) with auto-refresh
  - Hedge order placement (market orders with slippage guard)
  - Predicted funding rate polling
  - User state / open position queries

Usage:
    client = HLClient()
    await client.start()
    mid = client.get_mid("BTC")
    await client.place_hedge("BTC", "SHORT", 0.01)
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


@dataclass
class BBO:
    coin: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.bid or self.ask

    @property
    def spread(self) -> Optional[float]:
        if self.bid and self.ask:
            return self.ask - self.bid
        return None


@dataclass
class FundingSnapshot:
    coin: str
    hl_predicted: Optional[float] = None
    binance_predicted: Optional[float] = None
    bybit_predicted: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


class HLClient:
    """
    Async Hyperliquid client.

    Call `await client.start()` to begin WS subscription + dead man's switch.
    """

    def __init__(
        self,
        address: str = config.HL_ADDRESS,
        secret_key: str = config.HL_SECRET_KEY,
    ) -> None:
        self._address = address
        self._secret_key = secret_key
        self._info: Optional[Info] = None
        self._exchange: Optional[Exchange] = None
        self._bbo: dict[str, BBO] = {}          # coin → BBO
        self._mids: dict[str, float] = {}        # coin → mid price
        self._fundings: dict[str, FundingSnapshot] = {}
        self._bbo_callbacks: list[Callable] = []
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._paper_mode = config.PAPER_TRADING
        self._ws_connected: bool = False

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def on_bbo_update(self, callback: Callable[[str, BBO], Coroutine]) -> None:
        """Register an async callback(coin, bbo) called on BBO changes."""
        self._bbo_callbacks.append(callback)

    async def _fire_bbo(self, coin: str, bbo: BBO) -> None:
        for cb in self._bbo_callbacks:
            try:
                await cb(coin, bbo)
            except Exception as exc:
                log.error("bbo callback error", exc=str(exc))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._info = Info(base_url=config.HL_BASE_URL, skip_ws=True)
        if self._secret_key and not self._paper_mode:
            wallet = Account.from_key(self._secret_key)
            self._exchange = Exchange(
                wallet=wallet,
                base_url=config.HL_BASE_URL,
                meta=self._info.meta(),
            )
            self._set_dead_mans_switch()
        self._running = True
        log.info("HLClient started", paper_mode=self._paper_mode)

        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._dead_mans_refresh_loop())
        asyncio.create_task(self._funding_poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    # ── Dead man's switch ──────────────────────────────────────────────────────

    def _set_dead_mans_switch(self) -> None:
        if self._exchange is None or self._paper_mode:
            return
        expiry_ms = int((time.time() + config.HL_DEAD_MAN_INTERVAL) * 1000)
        try:
            self._exchange.schedule_cancel(time=expiry_ms)
            log.info("HL dead man's switch set", expiry_in_s=config.HL_DEAD_MAN_INTERVAL)
        except Exception as exc:
            log.error("Failed to set dead man's switch", exc=str(exc))

    async def _dead_mans_refresh_loop(self) -> None:
        if config.HL_DEAD_MAN_INTERVAL <= 60:
            raise ValueError(
                f"HL_DEAD_MAN_INTERVAL must be > 60s (got {config.HL_DEAD_MAN_INTERVAL}); "
                "refresh_every would be ≤ 0, causing an infinite loop"
            )
        refresh_every = config.HL_DEAD_MAN_INTERVAL - 60  # refresh 1 min before expiry
        while self._running:
            await asyncio.sleep(refresh_every)
            if not self._paper_mode:
                self._set_dead_mans_switch()

    # ── WebSocket ──────────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        # Market data WS (l2Book, allMids) is public  no address required.
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    HL_WS_URL,
                    ping_interval=None,
                ) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    backoff = 1.0
                    log.info("HL WebSocket connected")

                    await self._subscribe_all(ws)

                    async for raw in ws:
                        await self._handle_ws_message(raw)

            except ConnectionClosed as exc:
                log.warning("HL WS disconnected", code=exc.code)
            except Exception as exc:
                log.error("HL WS error", exc=str(exc))
            finally:
                self._ws = None
                self._ws_connected = False

            if self._running:
                log.info("HL WS reconnecting", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _subscribe_all(self, ws) -> None:
        # Subscribe to allMids
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        # Subscribe to l2Book (top of book) per coin
        for coin in config.HL_PERP_COINS:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin},
            }))
        log.debug("HL WS subscribed", coins=config.HL_PERP_COINS)

    async def _handle_ws_message(self, raw: str) -> None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return

        # HL sometimes wraps messages in arrays; unwrap to individual objects
        messages = parsed if isinstance(parsed, list) else [parsed]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            await self._process_ws_msg(msg)

    async def _process_ws_msg(self, msg: dict) -> None:
        channel = msg.get("channel")
        data = msg.get("data", {})

        # Guard: some channels return data as a list (e.g. subscriptionResponse)
        if not isinstance(data, dict):
            return

        if channel == "allMids":
            mids = data.get("mids", {})
            if isinstance(mids, dict):
                for coin, mid_str in mids.items():
                    self._mids[coin] = float(mid_str)

        elif channel == "l2Book":
            coin = data.get("coin", "")
            levels = data.get("levels", [[], []])
            # levels[0] = bids, levels[1] = asks; each entry is {"px": str, "sz": str, ...}
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []
            bid = float(bids[0]["px"]) if bids else None
            ask = float(asks[0]["px"]) if asks else None
            bbo = BBO(coin=coin, bid=bid, ask=ask)
            self._bbo[coin] = bbo
            await self._fire_bbo(coin, bbo)

    # ── Funding rates ──────────────────────────────────────────────────────────

    async def _funding_poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.HL_FUNDING_POLL_INTERVAL)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._fetch_fundings)
            except Exception as exc:
                log.error("Funding poll failed", exc=str(exc))

    def _fetch_fundings(self) -> None:
        if self._info is None:
            return
        try:
            meta, ctxs = self._info.meta_and_asset_ctxs()
            coins = [a["name"] for a in meta.get("universe", [])]
            for idx, ctx in enumerate(ctxs):
                if idx >= len(coins):
                    break
                coin = coins[idx]
                if coin not in config.HL_PERP_COINS:
                    continue
                snap = FundingSnapshot(
                    coin=coin,
                    hl_predicted=float(ctx.get("funding", 0) or 0),
                    binance_predicted=None,
                    bybit_predicted=None,
                )
                self._fundings[coin] = snap
                log.debug("Funding updated", coin=coin, rate=snap.hl_predicted)
        except Exception as exc:
            log.error("_fetch_fundings error", exc=str(exc))

    # ── Order placement ────────────────────────────────────────────────────────

    async def place_hedge(
        self,
        coin: str,
        direction: str,   # "LONG" or "SHORT"
        size: float,      # coin quantity
        slippage: float = config.HL_DEFAULT_SLIPPAGE,
    ) -> Optional[dict]:
        """
        Place a market hedge order on HL.  Returns the response dict or None.
        direction="SHORT" → is_buy=False (short perp to hedge long PM binary)
        """
        if self._paper_mode:
            mid = self._mids.get(coin) or self._bbo.get(coin, BBO(coin=coin)).mid
            log.info("[PAPER] place_hedge", coin=coin, direction=direction,
                     size=size, approx_mid=mid)
            return {"status": "paper", "coin": coin, "direction": direction, "size": size}

        if self._exchange is None:
            log.error("place_hedge: Exchange not initialised (no secret key?)")
            return None

        is_buy = direction.upper() == "LONG"
        try:
            resp = self._exchange.market_open(
                coin=coin,
                is_buy=is_buy,
                sz=size,
                slippage=slippage,
            )
            log.info("HL hedge placed",
                     coin=coin, direction=direction, size=size, resp=resp)
            return resp
        except Exception as exc:
            log.error("place_hedge failed", exc=str(exc), coin=coin)
            return None

    async def close_hedge(
        self,
        coin: str,
        direction: str,   # direction of the existing hedge to close
        size: float,
        slippage: float = config.HL_DEFAULT_SLIPPAGE,
    ) -> Optional[dict]:
        """Close an existing HL hedge position."""
        close_direction = "SHORT" if direction == "LONG" else "LONG"
        return await self.place_hedge(coin, close_direction, size, slippage)

    # ── Accessors ──────────────────────────────────────────────────────────────

    def get_mid(self, coin: str) -> Optional[float]:
        """Best available mid price for coin."""
        bbo = self._bbo.get(coin)
        if bbo and bbo.mid:
            return bbo.mid
        return self._mids.get(coin)

    def get_bbo(self, coin: str) -> Optional[BBO]:
        return self._bbo.get(coin)

    def get_funding(self, coin: str) -> Optional[FundingSnapshot]:
        return self._fundings.get(coin)

    def all_mids(self) -> dict[str, float]:
        return dict(self._mids)

    def get_fundings_snapshot(self) -> dict[str, FundingSnapshot]:
        return dict(self._fundings)

    def get_user_state(self) -> Optional[dict]:
        if self._info is None or not self._address:
            return None
        try:
            return self._info.user_state(self._address)
        except Exception as exc:
            log.error("get_user_state failed", exc=str(exc))
            return None
