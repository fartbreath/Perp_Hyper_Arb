"""
pm_client.py — Polymarket CLOB + Gamma API wrapper.

Responsibilities:
  - Market discovery via Gamma API
  - WebSocket subscription for real-time orderbook / price updates
  - CLOB heartbeat loop (critical — PM cancels all orders after 15s)
  - Order placement helpers (always post_only, dynamic feeRateBps)
  - Reconnect + repost on WS disconnect

Usage:
    client = PMClient()
    await client.start()
    ...
    await client.place_limit(token_id, "BUY", 0.45, 50.0)
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.exceptions import PolyApiException

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

# ── Order event log (C4) ──────────────────────────────────────────────────────
_ORDERS_CSV = Path(__file__).parent / "data" / "orders.csv"
_ORDERS_HEADER = [
    "timestamp", "order_id", "market_id", "token_id",
    "side", "price", "size", "order_type", "action",
]


def _ensure_orders_csv() -> None:
    """Create orders.csv with header if it doesn't exist."""
    _ORDERS_CSV.parent.mkdir(exist_ok=True)
    if not _ORDERS_CSV.exists():
        with _ORDERS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(_ORDERS_HEADER)
        return
    with _ORDERS_CSV.open("r", newline="") as f:
        try:
            existing = next(csv.reader(f))
        except StopIteration:
            existing = []
    if existing != _ORDERS_HEADER:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = _ORDERS_CSV.with_name(f"orders_{ts}.csv.bak")
        _ORDERS_CSV.rename(backup)
        with _ORDERS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(_ORDERS_HEADER)


def _append_order_event(
    order_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    order_type: str,   # "limit" | "market"
    action: str,       # "placed"
    market_id: str = "",
) -> None:
    """Append one row to the append-only orders.csv log."""
    try:
        _ensure_orders_csv()
        with _ORDERS_CSV.open("a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(),
                order_id, market_id, token_id,
                side, round(price, 6), round(size, 6),
                order_type, action,
            ])
    except Exception as exc:
        log.warning("orders.csv append failed", exc=str(exc))

# Market type labels — assigned by keyword-matching the market slug/title
_MARKET_TYPE_KEYWORDS: dict[str, list[str]] = {
    "bucket_5m":    ["5-minute", "5 minute", "5min"],
    "bucket_15m":   ["15-minute", "15 minute", "15min"],
    "bucket_1h":    ["1-hour", "1 hour", "1h", "hourly"],
    "bucket_4h":    ["4-hour", "4 hour", "4h"],
    "bucket_daily": ["daily", "day", "24-hour", "24 hour"],
    "bucket_weekly":["weekly", "week"],
    "milestone":    [],   # fallback
}

# Gamma API series.recurrence → market_type
# series.recurrence is the authoritative source when available; keyword matching
# is only used as a fallback for non-series events (e.g. one-off FDV launches).
_RECURRENCE_TO_MARKET_TYPE: dict[str, str] = {
    "5m":     "bucket_5m",
    "15m":    "bucket_15m",
    "hourly": "bucket_1h",
    "4h":     "bucket_4h",   # true 4-hour recurring markets (not the UI section label)
    "daily":  "bucket_daily",
    "weekly": "bucket_weekly",
    "monthly": "milestone",   # e.g. "Will BTC hit $X in March?" / "...in 2026?"
}

# Known full lifetimes per market type (seconds).
# Used in two places:
#   1. WS subscription filter in _update_shards: skip pre-created future bucket markets
#      that haven't started yet.  Subscribing thousands of idle future buckets wastes
#      WS capacity and causes permanent "stale" book warnings in the health dashboard.
#   2. Maker strategy: fraction-of-life computations for TTE gates and volume scaling
#      (imported from here to avoid duplicating the dict in strategy.py).
_MARKET_TYPE_DURATION_SECS: dict[str, int] = {
    "bucket_5m":    300,
    "bucket_15m":   900,
    "bucket_1h":    3_600,
    "bucket_4h":    14_400,
    "bucket_daily": 86_400,
    "bucket_weekly": 604_800,
    # milestone has no fixed duration — never filtered from WS subscriptions
}

# Maker rebate fraction by market type (source: docs.polymarket.com/market-makers/maker-rebates).
# Rebates are funded by taker fees in eligible markets and distributed daily in USDC.
# Sports types included for completeness; currently never traded (underlying=UNKNOWN at parse time).
_REBATE_PCT_BY_TYPE: dict[str, float] = {
    "bucket_5m":    0.20,
    "bucket_15m":   0.20,
    "bucket_1h":    0.20,
    "bucket_4h":    0.20,
    "bucket_daily": 0.20,
    "bucket_weekly": 0.20,
    "milestone":    0.20,  # crypto milestone markets with feesEnabled
    "sports_ncaab": 0.25,  # future-proofing
    "sports_serie_a": 0.25,
}


# Word-boundary patterns for underlying detection.
# Short/ambiguous tokens (OP, TON) require full word to avoid false matches
# (e.g. "ton" in "Edmonton", "op" in "OpenAI/option").
_UNDERLYING_PATTERNS: dict[str, list[str]] = {
    "BTC":  [r"\bBTC\b",     r"\bBITCOIN\b"],
    "ETH":  [r"\bETH\b",     r"\bETHEREUM\b"],
    "SOL":  [r"\bSOL\b",     r"\bSOLANA\b"],
    "XRP":  [r"\bXRP\b",     r"\bRIPPLE\b"],
    "BNB":  [r"\bBNB\b"],
    "DOGE": [r"\bDOGE\b",    r"\bDOGECOIN\b"],
    "ADA":  [r"\bADA\b",     r"\bCARDANO\b"],
    "AVAX": [r"\bAVAX\b"],                     # NOT \bAVALANCHE\b — matches Colorado Avalanche
    "LINK": [r"\bLINK\b",    r"\bCHAINLINK\b"],
    "DOT":  [r"\bPOLKADOT\b"],                 # NOT \bDOT\b — matches "dot" in text
    "SUI":  [r"\bSUI\b"],
    "APT":  [r"\bAPTOS\b"],                    # NOT \bAPT\b — matches "apt" in text
    "NEAR": [r"\bNEAR\s*PROTOCOL\b", r"\bNEAR\b"],
    "ARB":  [r"\bARB\b",     r"\bARBITRUM\b"],
    "OP":   [r"\bOPTIMISM\b"],         # NOT \bOP\b — too ambiguous
    "TON":  [r"\bTONCOIN\b"],          # NOT \bTON\b — matches Edmonton/ton
    "HYPE": [r"\bHYPE\b",    r"\bHYPERLIQUID\b"],
}

# Polymarket Gamma API tag slugs for each underlying.
# Polymarket uses coin-specific tag slugs (e.g. "solana", "ripple") rather than
# the generic "crypto" tag for most alt-coin events.  We query all of them so
# that SOL/XRP/DOGE/etc. markets are discovered alongside BTC and ETH.
_UNDERLYING_TAG_SLUGS: dict[str, list[str]] = {
    "BTC":  ["crypto", "bitcoin"],
    "ETH":  ["crypto", "ethereum"],
    "SOL":  ["solana"],
    "XRP":  ["ripple", "xrp"],
    "BNB":  ["bnb", "binance"],
    "DOGE": ["dogecoin"],
    "HYPE": ["hyperliquid", "hype", "up-or-down"],
    # HYPE 5m / 15m / 1h bucket markets are tagged "crypto" + "up-or-down" + "hype"
    # but NOT "hyperliquid" on Polymarket's Gamma API.  "hyperliquid" only returns
    # milestone/monthly events.  "hype" is the direct coin-specific tag and is the
    # primary route to bucket events.  "up-or-down" is kept as a fallback in case
    # Polymarket re-tags events, but "hype" avoids relying on pagination order
    # through the high-volume up-or-down feed.
}
# Flat deduplicated list of all tag slugs to query
_ALL_TAG_SLUGS: list[str] = list(dict.fromkeys(
    slug for slugs in _UNDERLYING_TAG_SLUGS.values() for slug in slugs
))


def _detect_underlying(title: str) -> str:
    """Return the crypto underlying for a market title, or 'UNKNOWN'."""
    t = title.upper()
    for asset, patterns in _UNDERLYING_PATTERNS.items():
        for p in patterns:
            if re.search(p, t):
                return asset
    return "UNKNOWN"


# Matches time-range patterns like "7:20AM-7:25AM" or "11:00PM - 12:00AM"
# used in individual bucket-market titles (e.g. "BNB Up or Down - March 26, 7:20AM-7:25AM ET").
# Groups: (h1)(m1) dash (h2)(m2)
_TIME_RANGE_RE = re.compile(
    r'(\d{1,2}):(\d{2})\s*(?:AM|PM)?\s*[-\u2013]\s*(\d{1,2}):(\d{2})',
    re.IGNORECASE,
)


def _detect_bucket_from_time_range(title: str) -> Optional[str]:
    """Detect bucket market type from a HH:MM–HH:MM time-range in the title.

    Handles titles like "BNB Up or Down - March 26, 7:20AM-7:25AM ET" where
    no keyword (e.g. "5-minute") appears but the window size is encoded as a
    clock-time range.

    Returns the market_type string ("bucket_5m", "bucket_15m", "bucket_1h",
    "bucket_4h") or None if no recognisable range is found.
    """
    m = _TIME_RANGE_RE.search(title)
    if not m:
        return None
    h1, m1, h2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    diff = (h2 * 60 + m2) - (h1 * 60 + m1)
    if diff < 0:
        diff += 12 * 60   # handle 12-hour format boundary (e.g. 12:55PM – 1:00PM)
    if diff == 5:
        return "bucket_5m"
    if diff == 15:
        return "bucket_15m"
    if diff == 60:
        return "bucket_1h"
    if diff == 240:
        return "bucket_4h"
    return None


def _classify_market(title: str) -> str:
    title_l = title.lower()
    # Check longer/more-specific patterns first to avoid substring collisions
    # e.g. "5-minute" matches inside "15-minute" unless we check 15m first.
    priority_order = [
        "bucket_15m",
        "bucket_5m",
        "bucket_4h",
        "bucket_1h",
        "bucket_weekly",
        "bucket_daily",
        "milestone",
    ]
    for market_type in priority_order:
        for kw in _MARKET_TYPE_KEYWORDS[market_type]:
            if kw in title_l:
                return market_type
    # Time-range fallback: detect bucket type from a clock-time window in the title,
    # e.g. "BNB Up or Down - March 26, 7:20AM-7:25AM ET" → "bucket_5m".
    # This handles series whose titles carry no interval keyword (like "BNB Up or Down")
    # but whose individual market questions encode the window as a time range.
    bucket = _detect_bucket_from_time_range(title)
    if bucket is not None:
        return bucket
    return "milestone"


@dataclass
class PMMarket:
    condition_id: str
    token_id_yes: str
    token_id_no: str
    title: str
    market_type: str         # bucket_* | milestone
    underlying: str          # BTC | ETH | SOL | UNKNOWN
    fees_enabled: bool
    end_date: Optional[datetime]
    tick_size: float = 0.01
    max_incentive_spread: float = 0.04
    discovered_at: float = field(default_factory=time.time)
    volume_24hr: float = 0.0
    market_slug: str = ""  # Gamma API event slug → used for https://polymarket.com/event/{slug}
    event_start_time: str = ""  # ISO window-open time e.g. "2026-04-12T14:20:00Z" (for priceToBeat lookup)

    @property
    def is_fee_free(self) -> bool:
        return not self.fees_enabled

    @property
    def rebate_pct(self) -> float:
        """Maker rebate fraction earned on filled quotes in this market.
        Returns 0.0 if the market has no fees (and therefore no rebate pool)."""
        if not self.fees_enabled:
            return 0.0
        return _REBATE_PCT_BY_TYPE.get(self.market_type, 0.0)

    def token_ids(self) -> list[str]:
        return [self.token_id_yes, self.token_id_no]


@dataclass
class OrderBookSnapshot:
    token_id: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size)
    asks: list[tuple[float, float]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        b, a = self.best_bid, self.best_ask
        if b is not None and a is not None:
            return (b + a) / 2
        return b or a


class _WSShard:
    """One PM WebSocket connection owning ≤ config.PM_WS_MAX_MARKETS_PER_WS tokens.

    Each shard runs its own connect/reconnect loop and heartbeat. Message
    payloads (JSON only) are forwarded to PMClient via the *on_message*
    callback. Non-JSON frames such as "INVALID OPERATION" are counted here and
    never forwarded.

    Token assignment is stable: once a token is assigned to a shard it stays
    there until explicitly removed via update_tokens(). Calling update_tokens()
    with a changed token set closes the current WS so _loop() reconnects with
    the fresh set — matching the GroupSocket reconnect-on-update pattern from
    the @ultralumao/poly-websockets library.
    """

    def __init__(
        self,
        shard_id: int,
        on_message: Callable[[str], Coroutine],
    ) -> None:
        self.shard_id = shard_id
        self._on_message = on_message
        self._tokens: set[str] = set()   # tokens assigned to this shard
        self.connected: bool = False
        self._rejected: int = 0
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False

    async def start(self, tokens: set[str]) -> None:
        self._tokens = set(tokens)
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()

    def update_tokens(self, tokens: set[str]) -> None:
        """Replace the token set.  If the WS is open, close it so _loop()
        reconnects with the updated subscription list.
        """
        if tokens == self._tokens:
            return
        self._tokens = set(tokens)
        if self._ws is not None and self.connected:
            asyncio.ensure_future(self._close_ws())

    async def _close_ws(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    config.PM_WS_URL,
                    ping_interval=config.PM_WS_PING_INTERVAL,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1.0
                    log.info("PM WS shard connected",
                             shard_id=self.shard_id, token_count=len(self._tokens))

                    # Snapshot tokens at connect time so mid-session updates
                    # are handled on the *next* reconnect (GroupSocket pattern).
                    tokens_snapshot = list(self._tokens)
                    if tokens_snapshot:
                        # PM market WS API: { "assets_ids": [...], "type": "market" }
                        # No auth field; lowercase type key.  All tokens sent in
                        # a single message — each shard holds ≤ PM_WS_MAX_MARKETS_PER_WS
                        # tokens so this never exceeds the server per-session limit.
                        msg = {"assets_ids": tokens_snapshot, "type": "market"}
                        await ws.send(json.dumps(msg))

                    async for raw in ws:
                        if not raw.startswith("{") and not raw.startswith("["):
                            if "INVALID" in raw.upper():
                                self._rejected += 1
                                log.warning(
                                    "PM WS shard subscription rejected (INVALID OPERATION)",
                                    shard_id=self.shard_id,
                                    shard_rejected=self._rejected,
                                    shard_tokens=len(self._tokens),
                                )
                            else:
                                log.debug("PM WS shard non-JSON",
                                          shard_id=self.shard_id, preview=raw[:80])
                            continue
                        await self._on_message(raw)

            except ConnectionClosed as exc:
                log.warning("PM WS shard disconnected",
                            shard_id=self.shard_id, code=exc.code, reason=exc.reason)
            except Exception as exc:
                log.error("PM WS shard error", shard_id=self.shard_id, exc=str(exc))
            finally:
                self._ws = None
                self.connected = False

            if self._running:
                log.info("PM WS shard reconnecting",
                         shard_id=self.shard_id, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @property
    def subscribed_count(self) -> int:
        return len(self._tokens)

    @property
    def rejected_count(self) -> int:
        return self._rejected


class PMClient:
    """
    Async Polymarket client.

    Call `await client.start()` to begin market discovery + WS subscription.
    Register price-change callbacks via `on_price_change(callback)`.
    """

    def __init__(self, private_key: str = config.POLY_PRIVATE_KEY) -> None:
        self._private_key = private_key
        self._clob: Optional[ClobClient] = None
        self._markets: dict[str, PMMarket] = {}          # condition_id → PMMarket
        self._books: dict[str, OrderBookSnapshot] = {}   # token_id → snapshot
        self._pinned_tokens: set[str] = set()            # tokens that must stay WS-subscribed (open positions)
        self._extra_tokens: set[str] = set()             # tokens registered by non-maker strategies (e.g. momentum)
        self._price_callbacks: list[Callable] = []
        self._order_fill_callbacks: list[Callable] = []
        self._user_ws_reconnect_callbacks: list[Callable] = []  # A1: fired after user WS reconnects
        # One-shot futures resolved by _fire_order_fill so callers can await
        # fill confirmation without polling (replaces asyncio.sleep(1.0)).
        self._pending_fill_futures: dict[str, asyncio.Future] = {}
        # Brief cache for fill events that arrive before register_fill_future()
        # is called (race: fill completes during REST order-placement round-trip).
        # Entries are (msg, timestamp); pruned lazily on each new fill event.
        self._recent_fills: dict[str, tuple[dict, float]] = {}
        self._api_creds: Optional[ApiCreds] = None  # populated after CLOB auth
        self._running = False
        self._paper_mode: bool = config.PAPER_TRADING
        self._last_heartbeat_ts: float = 0.0
        # WS shards — each shard owns ≤ PM_WS_MAX_MARKETS_PER_WS tokens.
        # _token_shard_map gives stable token→shard assignment so bucket
        # positions never shift across market-refresh cycles.
        self._shards: dict[int, _WSShard] = {}    # shard_id → shard
        self._token_shard_map: dict[str, int] = {}  # token_id → shard_id
        self._next_shard_id: int = 0

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def on_price_change(self, callback: Callable[[str, float], Coroutine]) -> None:
        """Register an async callback(token_id, new_mid) called on price updates."""
        self._price_callbacks.append(callback)

    async def _fire_price_change(self, token_id: str, mid: float) -> None:
        # Dispatch each callback as an independent asyncio task so a slow or
        # blocking callback (e.g. maker reprice REST round-trip) does not delay
        # subsequent callbacks (e.g. monitor stop-loss check) for the same tick.
        for cb in self._price_callbacks:
            task = asyncio.create_task(cb(token_id, mid))
            task.add_done_callback(
                lambda t: log.error("price_change callback raised", exc=str(t.exception()))
                if not t.cancelled() and t.exception() is not None else None
            )

    def on_order_fill(self, callback: Callable) -> None:
        """Register an async callback(order_data) called when a PM order is matched."""
        self._order_fill_callbacks.append(callback)

    def register_fill_future(
        self, order_id: str, future: "asyncio.Future[dict]"
    ) -> None:
        """Register a one-shot Future resolved when order_id receives a MATCHED event.

        If the fill already arrived during the REST order-placement round-trip
        (a real race in asyncio: place_market suspends, event loop runs user WS),
        the future is resolved immediately from the recent-fill cache.
        """
        # Prune stale cached fills so this dict doesn't grow if callers never
        # arrive (e.g. order placed then timed out before register_fill_future).
        _now = time.time()
        if self._recent_fills:
            cutoff = _now - 30.0
            self._recent_fills = {
                k: v for k, v in self._recent_fills.items() if v[1] > cutoff
            }
        cached = self._recent_fills.pop(order_id, None)
        if cached is not None and not future.done():
            future.set_result(cached[0])  # cached is (msg, timestamp)
            return
        self._pending_fill_futures[order_id] = future

    def on_user_ws_reconnect(self, callback: Callable) -> None:
        """Register an async callback() called after each PM user WS reconnect.

        Use this to reconcile missed fills: fetch the PM Data API and compare
        against in-memory state — any positions in the wallet but missing from
        the risk engine must have filled during the disconnect window.
        """
        self._user_ws_reconnect_callbacks.append(callback)

    async def _fire_user_ws_reconnect(self) -> None:
        for cb in self._user_ws_reconnect_callbacks:
            try:
                await cb()
            except Exception as exc:
                log.error("user_ws_reconnect callback error", exc=str(exc))

    async def _fire_order_fill(self, order_data: dict) -> None:
        # Prune stale cached fills (older than 30 s) to prevent unbounded growth.
        _now = time.time()
        if self._recent_fills:
            cutoff = _now - 30.0
            self._recent_fills = {
                k: v for k, v in self._recent_fills.items() if v[1] > cutoff
            }

        # Resolve any registered one-shot future for this order.
        order_id = order_data.get("id") or order_data.get("order_id", "")
        if order_id:
            fut = self._pending_fill_futures.pop(order_id, None)
            if fut is not None and not fut.done():
                fut.set_result(order_data)
            elif fut is not None:
                # fut.done() is True — the future already timed out (scanner moved to
                # REST fallback).  The fill event is intentionally dropped here; the
                # REST path already retrieved the fill details independently.
                pass
            else:
                # fut is None — no waiter registered yet.  Park the event so
                # register_fill_future() can resolve it immediately if called
                # shortly after (race: fill arrived during REST round-trip).
                self._recent_fills[order_id] = (order_data, _now)

        for cb in self._order_fill_callbacks:
            try:
                await cb(order_data)
            except Exception as exc:
                log.error("order_fill callback error", exc=str(exc))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise CLOB client, discover markets, start background tasks."""
        if self._paper_mode:
            log.info("PMClient started in paper mode — skipping CLOB auth")
        else:
            self._clob = self._build_clob_client()
        self._running = True
        log.info("PMClient started", paper_mode=self._paper_mode)

        await self._refresh_markets()

        asyncio.create_task(self._market_refresh_loop())
        await self._update_shards()

        if not self._paper_mode and self._clob is not None:
            asyncio.create_task(self._clob_heartbeat_loop())
            asyncio.create_task(self._user_ws_loop())

    async def stop(self) -> None:
        self._running = False
        for shard in self._shards.values():
            await shard.stop()

    def _build_clob_client(self) -> ClobClient:
        """Build an authenticated ClobClient (Level 2)."""
        funder = config.POLY_FUNDER or None
        # signature_type=2: POLY_GNOSIS_SAFE — Polymarket-generated Safe wallet
        # key = EOA private key (signer), funder = Safe address (holds USDC)
        client = ClobClient(
            host=config.POLY_HOST,
            key=self._private_key,
            chain_id=137,  # Polygon mainnet
            signature_type=2,  # POLY_GNOSIS_SAFE
            funder=funder,
        )
        try:
            creds: ApiCreds = client.derive_api_key()
            client.set_api_creds(creds)
            self._api_creds = creds  # stored for user WS authentication
            log.info("CLOB client authenticated", safe=funder, signer=client.get_address())
        except Exception as exc:
            log.warning("CLOB auth failed — running read-only", exc=str(exc))
        return client

    # ── Market discovery ───────────────────────────────────────────────────────

    async def _refresh_markets(self) -> None:
        """Fetch crypto markets from Gamma API.

        Queries every coin-specific tag slug as well as the generic "crypto" tag
        so that alt-coin markets (SOL, XRP, DOGE, etc.) are discovered alongside
        BTC and ETH.  Polymarket uses per-coin slugs ("solana", "ripple", …) rather
        than a shared "crypto" parent tag for most non-BTC/ETH events.
        """
        MAX_PAGES_PER_SLUG = 3   # 3 × 100 = 300 events per tag slug — more than enough
        url = f"{config.GAMMA_HOST}/events"
        new_count = 0
        total_events = 0
        total_markets_seen = 0

        try:
            async with aiohttp.ClientSession() as session:
                for tag_slug in _ALL_TAG_SLUGS:
                    for page in range(MAX_PAGES_PER_SLUG):
                        params: dict = {
                            "active": "true",
                            "closed": "false",
                            "tag_slug": tag_slug,
                            "limit": 100,
                            "offset": page * 100,
                            "order": "volume24hr",
                            "ascending": "false",
                        }

                        async with session.get(
                            url, params=params,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as resp:
                            events_raw = await resp.json()

                        if not isinstance(events_raw, list) or not events_raw:
                            break

                        total_events += len(events_raw)

                        for event in events_raw:
                            event_title = event.get("title", "")
                            # Detect underlying once at event level — more reliable
                            # than per-market question (which may say "Will price be
                            # above $X?" without naming the coin explicitly).
                            underlying = _detect_underlying(event_title)
                            # Extract series info for authoritative market_type.
                            # series can be a dict OR a list depending on the Gamma API endpoint/event.
                            _series_raw = event.get("series")
                            if isinstance(_series_raw, list):
                                _series_raw = _series_raw[0] if _series_raw else None
                            series: dict = _series_raw if isinstance(_series_raw, dict) else {}
                            recurrence = series.get("recurrence")  # e.g. "daily", "weekly", "monthly", "hourly", "5m", "15m"
                            series_title: str = series.get("title", "")  # e.g. "BTC Up or Down 4h", "Solana Up or Down Hourly"

                            event_slug: str = event.get("slug", "")
                            for mkt in event.get("markets", []):
                                total_markets_seen += 1
                                if not mkt.get("active", True):
                                    continue
                                parsed = self._parse_market(mkt, underlying_override=underlying, recurrence_override=recurrence, series_title_override=series_title, event_slug=event_slug)
                                if parsed:
                                    existing = self._markets.get(parsed.condition_id)
                                    if existing is None:
                                        self._markets[parsed.condition_id] = parsed
                                        new_count += 1
                                    else:
                                        # Refresh mutable fields that change over time:
                                        # volume_24hr grows throughout the day (drives volume gate),
                                        # max_incentive_spread and end_date may also be adjusted.
                                        existing.volume_24hr = parsed.volume_24hr
                                        existing.max_incentive_spread = parsed.max_incentive_spread
                                        if parsed.end_date is not None:
                                            existing.end_date = parsed.end_date

                        if len(events_raw) < 100:
                            break   # last page for this slug

        except Exception as exc:
            log.error("Gamma API fetch failed", exc=str(exc))
            return

        log.info("Markets refreshed", total=len(self._markets), new=new_count,
                 events_fetched=total_events, markets_seen=total_markets_seen)

    def _parse_market(self, raw: dict, underlying_override: Optional[str] = None, recurrence_override: Optional[str] = None, series_title_override: str = "", event_slug: str = "") -> Optional[PMMarket]:
        """Parse a Gamma API market dict into a PMMarket, or return None if unusable."""
        # Skip markets that are closed, inactive, or not accepting orders
        if raw.get("closed", False):
            return None
        if not raw.get("active", True):
            return None
        if not raw.get("acceptingOrders", True):
            return None

        if not raw.get("enableOrderBook", True):
            return None

        tokens = raw.get("clobTokenIds", [])
        # clobTokenIds may arrive as a JSON string from the events endpoint
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = []
        if len(tokens) < 2:
            return None

        title = raw.get("question", raw.get("title", ""))
        # Prefer the event-level underlying (passed in), fall back to per-market detection
        underlying = underlying_override if underlying_override and underlying_override != "UNKNOWN" \
            else _detect_underlying(title)

        if underlying == "UNKNOWN":
            return None

        # Classification priority:
        # 1. Series title keyword matching — most reliable for batched markets whose
        #    individual question titles don't name the bucket type.  e.g. "BTC Up or Down
        #    4h" series has recurrence="daily" (batch created daily) but each market is
        #    a 4-hour window.  DOGE/HYPE hourly/5m/15m series also use recurrence="daily".
        # 2. series.recurrence → unambiguous mappings (hourly, 5m, 15m, weekly, monthly).
        # 3. Individual market title keyword / time-range matching — fallback for one-off
        #    events and for series whose title carries no interval keyword (e.g. "BNB Up
        #    or Down" without "5-minute").  Time-range detection handles titles like
        #    "BNB Up or Down - March 26, 7:20AM-7:25AM ET" → bucket_5m.
        #
        # Important: step 3 is also applied when recurrence maps to "bucket_daily"
        # because many short-interval series (5m, 15m, hourly) use recurrence="daily"
        # (batch-created once a day).  Accepting "bucket_daily" blindly for those would
        # give wrong lifecycle fractions and prevent quoting entirely.
        if series_title_override:
            market_type = _classify_market(series_title_override)
            if market_type == "milestone":
                # Series title gave no specific bucket type; try recurrence next.
                if recurrence_override and recurrence_override in _RECURRENCE_TO_MARKET_TYPE:
                    market_type = _RECURRENCE_TO_MARKET_TYPE[recurrence_override]
                # Also try individual title when recurrence maps to "bucket_daily" — short-
                # interval (5m/15m/1h) series often have recurrence="daily" because they
                # are batch-created.  The individual question title (with its time range)
                # is the authoritative source for the actual window size in those cases.
                if market_type in ("milestone", "bucket_daily"):
                    title_type = _classify_market(title)
                    if title_type != "milestone":
                        market_type = title_type
        elif recurrence_override and recurrence_override in _RECURRENCE_TO_MARKET_TYPE:
            market_type = _RECURRENCE_TO_MARKET_TYPE[recurrence_override]
        else:
            market_type = _classify_market(title)

        end_date = None
        if raw.get("endDate"):
            try:
                end_date = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
            except ValueError:
                pass

        return PMMarket(
            condition_id=raw.get("conditionId", raw.get("id", "")),
            token_id_yes=tokens[0],
            token_id_no=tokens[1],
            title=title,
            market_type=market_type,
            underlying=underlying,
            fees_enabled=bool(raw.get("feesEnabled", False)),
            end_date=end_date,
            tick_size=float(raw.get("minimumTickSize", 0.01)),
            max_incentive_spread=float(raw.get("maxIncentiveSpread", 0.04)),
            volume_24hr=float(raw.get("volume24hr", 0.0) or 0.0),
            market_slug=event_slug or raw.get("slug", ""),  # prefer event slug — canonical Polymarket URL
            event_start_time=raw.get("eventStartTime", "") or "",  # window-open ISO timestamp for priceToBeat lookup
        )

    # ── Market pruning ──────────────────────────────────────────────────────────

    def _prune_expired_markets(self) -> int:
        """Remove markets whose end_date has passed.

        Pinned tokens (open/tracked positions) are never removed.
        Stale book snapshots for pruned tokens are also cleared.
        Returns the count of pruned markets.
        """
        now = datetime.now(timezone.utc)
        to_remove = [
            cid for cid, mkt in self._markets.items()
            if (
                mkt.end_date is not None
                and mkt.end_date < now
                and mkt.token_id_yes not in self._pinned_tokens
                and mkt.token_id_no  not in self._pinned_tokens
            )
        ]
        for cid in to_remove:
            mkt = self._markets.pop(cid)
            self._books.pop(mkt.token_id_yes, None)
            self._books.pop(mkt.token_id_no,  None)
        if to_remove:
            log.debug("Pruned expired markets",
                      count=len(to_remove), remaining=len(self._markets))
        return len(to_remove)

    async def _market_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.MARKET_REFRESH_INTERVAL)
            self._prune_expired_markets()
            await self._refresh_markets()
            await self._update_shards()

    # ── WebSocket shards ───────────────────────────────────────────────────────

    def _extract_hb_id_from_exc(self, exc: Exception) -> Optional[str]:
        """Extract the correct heartbeat_id from a Polymarket 400 error response.
        PM returns {"heartbeat_id": "<id>", "error_msg": "Invalid Heartbeat ID"}
        when our ID is wrong — parse that to recover the session."""
        if isinstance(exc, PolyApiException) and isinstance(exc.error_msg, dict):
            return exc.error_msg.get("heartbeat_id") or None
        return None

    async def _clob_heartbeat_loop(self) -> None:
        """Periodically POST /v1/heartbeats to keep open CLOB orders alive.

        Polymarket heartbeat protocol (opt-in, stateful):
          1. First call: heartbeat_id = "" (empty string).
          2. Success response body contains {"heartbeat_id": "<next_id>"} — use
             that value for every subsequent call.
          3. On 400 "Invalid Heartbeat ID" the error body also contains
             {"heartbeat_id": "<correct_id>"} — parse it and resume.
          4. PM cancels all open orders if no heartbeat arrives within 10 s.
        """
        # ── Session start: first heartbeat uses empty string ─────────────────
        current_hb_id = ""
        self._last_heartbeat_ts = time.time()
        try:
            resp = await asyncio.to_thread(self._clob.post_heartbeat, current_hb_id)
            next_id = resp.get("heartbeat_id", "") if isinstance(resp, dict) else ""
            log.info("CLOB heartbeat session started", next_hb_id=next_id)
            current_hb_id = next_id
        except PolyApiException as exc:
            # Server may give us the correct ID even on first-call 400
            recovered = self._extract_hb_id_from_exc(exc)
            if recovered is not None:
                log.info("CLOB heartbeat recovered from first-call error",
                         correct_hb_id=recovered)
                current_hb_id = recovered
            else:
                log.info("CLOB heartbeat not available for this account — skipping",
                         exc=str(exc))
                return
        except Exception as exc:
            log.info("CLOB heartbeat not available for this account — skipping",
                     exc=str(exc))
            return

        # ── Ongoing heartbeat loop ───────────────────────────────────────────
        consecutive_failures = 0
        while self._running:
            await asyncio.sleep(config.PM_HEARTBEAT_INTERVAL)
            self._last_heartbeat_ts = time.time()
            try:
                resp = await asyncio.to_thread(self._clob.post_heartbeat, current_hb_id)
                next_id = resp.get("heartbeat_id", "") if isinstance(resp, dict) else ""
                log.debug("CLOB heartbeat sent", next_hb_id=next_id)
                current_hb_id = next_id
                consecutive_failures = 0
            except PolyApiException as exc:
                recovered = self._extract_hb_id_from_exc(exc)
                if recovered is not None:
                    log.warning("CLOB heartbeat ID corrected from server error",
                                old_hb_id=current_hb_id, new_hb_id=recovered)
                    current_hb_id = recovered
                    consecutive_failures = 0  # successfully recovered
                else:
                    consecutive_failures += 1
                    log.warning("CLOB heartbeat REST failed", exc=str(exc),
                                consecutive=consecutive_failures)
                    if consecutive_failures >= 3:
                        log.info("CLOB heartbeat stopping after 3 consecutive failures")
                        return
            except Exception as exc:
                consecutive_failures += 1
                log.warning("CLOB heartbeat REST failed", exc=str(exc),
                             consecutive=consecutive_failures)
                if consecutive_failures >= 3:
                    log.info("CLOB heartbeat stopping after 3 consecutive failures")
                    return

    async def _update_shards(self) -> None:
        """Stable token-to-shard assignment (GroupRegistry pattern).

        Tokens are assigned to shards once and never moved.  On each call:
          - Expired tokens (no longer in active markets) are removed from their
            shard; the shard reconnects with the pruned token list.
          - New tokens fill existing shards that have spare capacity, or start a
            fresh shard when all existing shards are at the N-token limit.
          - Shards that become empty after removals are stopped and discarded.

        Pinned tokens (open positions) are processed first so they are assigned
        to the earliest-started shards, keeping heartbeats live even during
        higher-shard reconnect cycles.

        N = config.PM_WS_MAX_MARKETS_PER_WS (default 100) — community-confirmed
        per-session server limit for the PM market WS channel.
        """
        # Subscribe only to markets within the quoting horizon that have started
        # and have shown at least some trading activity.
        #
        # Three filters keep WS shard count manageable and eliminate the large
        # "stale" book counts caused by subscribing markets we'd never quote:
        #
        #   1. TTE horizon: skip markets with TTE > (MAKER_MAX_TTE_DAYS + 0.25 days).
        #      The +6 h buffer above the strategy limit ensures price data is ready
        #      the moment a market enters the quoting window.
        #
        #   2. Bucket not yet started: Polymarket pre-creates future 5m/1h/daily
        #      buckets up to days in advance.  Skip until now >= end_date − duration.
        #      (Equivalent to: skip when TTE > known market duration.)
        #
        #   3. Zero volume: markets with no trading activity have no order book and
        #      no mid price, so _evaluate_signal always returns None for them.
        #      Subscribing them wastes a WS slot and generates permanent stale-book
        #      warnings. They are added automatically on the next refresh cycle once
        #      volume_24hr > 0 (typically within 60 s of the first trade).
        #
        # Pinned tokens (open positions) bypass all filters — they must stay
        # subscribed regardless of TTE so position management keeps working.
        _now = time.time()
        _max_tte = (config.MAKER_MAX_TTE_DAYS + 0.25) * 86_400  # +6 h lead-time buffer
        market_tokens: list[str] = []
        for mkt in self._markets.values():
            # Always keep open-position tokens subscribed, regardless of TTE.
            if (mkt.token_id_yes in self._pinned_tokens
                    or mkt.token_id_no in self._pinned_tokens):
                market_tokens.extend(mkt.token_ids())
                continue

            # Filter 3: no trading activity yet — nothing to quote, no mid price
            if mkt.volume_24hr == 0.0:
                continue

            if mkt.end_date is not None:
                _tte = mkt.end_date.timestamp() - _now
                # Filter 0: market already expired — Gamma API may still return it
                # as active=true due to settlement lag, causing a prune-then-re-add
                # cycle every 15 s that forces constant shard reconnects.
                if _tte <= 0:
                    continue
                # Filter 1: beyond quoting horizon
                if _tte > _max_tte:
                    continue
                # Filter 2: bucket hasn't opened yet (TTE still exceeds its full duration)
                _dur = _MARKET_TYPE_DURATION_SECS.get(mkt.market_type)
                if _dur is not None and _tte > _dur:
                    continue
            market_tokens.extend(mkt.token_ids())
        pinned = sorted(self._pinned_tokens)
        others = [t for t in market_tokens if t not in self._pinned_tokens]
        tokens_wanted: set[str] = set(pinned + others) | self._extra_tokens

        N = config.PM_WS_MAX_MARKETS_PER_WS

        # ── Removals ──────────────────────────────────────────────────────────
        expired = set(self._token_shard_map) - tokens_wanted
        if expired:
            by_shard: dict[int, set[str]] = {}
            for tok in expired:
                sid = self._token_shard_map.pop(tok)
                by_shard.setdefault(sid, set()).add(tok)
            for sid, removed in by_shard.items():
                shard = self._shards.get(sid)
                if shard:
                    shard.update_tokens(shard._tokens - removed)
            # Clear stale book snapshots for unsubscribed tokens so the health
            # dashboard shows "no_data" (correct: we're not watching them) rather
            # than an ever-aging "stale" timestamp from the last subscription.
            for tok in expired:
                self._books.pop(tok, None)
            log.debug("PM WS expired tokens removed", count=len(expired))

        # ── Additions ─────────────────────────────────────────────────────────
        # Maintain pinned-first ordering for assignment priority.
        new_tokens = [t for t in (pinned + others) if t not in self._token_shard_map]
        remaining = new_tokens

        while remaining:
            # Find first shard with spare capacity.
            target_sid = next(
                (sid for sid, s in self._shards.items() if len(s._tokens) < N),
                None,
            )
            if target_sid is not None:
                shard = self._shards[target_sid]
                capacity = N - len(shard._tokens)
                to_assign = remaining[:capacity]
                remaining = remaining[capacity:]
                shard.update_tokens(shard._tokens | set(to_assign))
                for t in to_assign:
                    self._token_shard_map[t] = target_sid
                log.debug("PM WS shard expanded",
                          shard_id=target_sid, added=len(to_assign),
                          total=len(shard._tokens))
            else:
                # All shards full — start a new one.
                chunk = remaining[:N]
                remaining = remaining[N:]
                sid = self._next_shard_id
                self._next_shard_id += 1
                shard = _WSShard(sid, self._handle_ws_message)
                await shard.start(set(chunk))
                self._shards[sid] = shard
                for t in chunk:
                    self._token_shard_map[t] = sid
                log.debug("PM WS shard started", shard_id=sid, tokens=len(chunk))

        # ── Cleanup empty shards ───────────────────────────────────────────────
        empty = [sid for sid, s in self._shards.items() if len(s._tokens) == 0]
        for sid in empty:
            await self._shards.pop(sid).stop()
            log.debug("PM WS shard stopped (empty)", shard_id=sid)

        log.debug("PM WS shards updated",
                  shards=len(self._shards), total_tokens=len(self._token_shard_map),
                  max_per_shard=N)

    async def _handle_ws_message(self, raw: str) -> None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("PM WS JSON decode error", preview=raw[:80])
            return

        # PM sends frames as JSON arrays; unwrap to individual messages
        messages = parsed if isinstance(parsed, list) else [parsed]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            await self._process_ws_msg(msg)

    async def _process_ws_msg(self, msg: dict) -> None:
        event_type = msg.get("event_type") or msg.get("type")

        if event_type == "book":
            self._update_book_from_ws(msg)
            token_id = msg.get("asset_id", "")
            snap = self._books.get(token_id)
            if snap and snap.mid is not None:
                await self._fire_price_change(token_id, snap.mid)
        elif event_type == "price_change":
            affected = self._update_price_from_ws(msg)
            for token_id in affected:
                snap = self._books.get(token_id)
                if snap and snap.mid is not None:
                    await self._fire_price_change(token_id, snap.mid)
        elif event_type == "last_trade_price":
            pass  # full book resent on trade via book event
        # other/unknown messages ignored


    def _update_book_from_ws(self, msg: dict) -> None:
        token_id = msg.get("asset_id", "")
        snap = OrderBookSnapshot(token_id=token_id)
        for entry in msg.get("bids", []):
            snap.bids.append((float(entry["price"]), float(entry["size"])))
        for entry in msg.get("asks", []):
            snap.asks.append((float(entry["price"]), float(entry["size"])))
        snap.bids.sort(key=lambda x: -x[0])
        snap.asks.sort(key=lambda x: x[0])
        self._books[token_id] = snap

    def _update_price_from_ws(self, msg: dict) -> set[str]:
        """Apply incremental price-level changes from a price_change event.

        PM format: {"price_changes": [{"asset_id": ..., "price": ..., "size": ...,
        "side": "BUY"|"SELL"}, ...], "event_type": "price_change"}.
        asset_id lives inside each item, NOT at the root of the message.
        Returns the set of token IDs whose books were updated.
        """
        affected: set[str] = set()
        for change in msg.get("price_changes", []):
            token_id = change.get("asset_id", "")
            if not token_id:
                continue
            side = change.get("side", "").upper()
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            snap = self._books.setdefault(token_id, OrderBookSnapshot(token_id=token_id))
            target = snap.bids if side == "BUY" else snap.asks
            target[:] = [(p, s) for p, s in target if p != price]
            if size > 0:
                target.append((price, size))
                target.sort(key=lambda x: -x[0] if side == "BUY" else x[0])
            snap.timestamp = time.time()
            affected.add(token_id)
        return affected

    # ── Order helpers ──────────────────────────────────────────────────────────

    def _round_to_tick(self, price: float, tick_size: float) -> float:
        ticks = round(price / tick_size)
        return round(ticks * tick_size, 10)

    async def place_limit(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        price: float,
        size: float,
        market: Optional[PMMarket] = None,
        post_only: bool = True,
    ) -> Optional[str]:
        """
        Place a GTC limit order.  Returns order_id on success, None on failure.

        post_only=True  (default): maker-only — rejected if it would cross the book.
            The "crosses book" retry backs off one tick so the order rests passively.
        post_only=False: taker limit — crosses the spread immediately for a fast fill.
            No retry logic; if the order is rejected the caller gets None.

        Always fetches feeRateBps dynamically (NEVER uses a hardcoded value).
        """
        if self._paper_mode:
            log.info("[PAPER] place_limit", token_id=token_id, side=side, price=price, size=size)
            return f"paper-{int(time.time())}"

        if self._clob is None:
            log.error("place_limit: CLOB client not initialised")
            return None

        tick = market.tick_size if market else 0.01
        rounded_price = self._round_to_tick(price, tick)

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=rounded_price,
                size=size,
                side=side,
            )
            # NOTE: post_only goes to post_order(), NOT create_order() — passing a dict
            # to create_order(options=) causes "'dict' object has no attribute 'tick_size'"
            # Sign in a thread — create_order() uses requests (blocking I/O) and
            # would stall the event loop, freezing WS book-cache updates during the
            # signing window.
            signed = await asyncio.to_thread(self._clob.create_order, order_args)
            # Taker orders (post_only=False) use FAK so any unfilled remainder is
            # immediately cancelled rather than resting as a passive maker bid.
            order_type_enum = OrderType.GTC if post_only else OrderType.FAK
            # Post in a thread — post_order() is a blocking HTTP POST.
            resp = await asyncio.to_thread(
                self._clob.post_order, signed, order_type_enum, post_only
            )
            order_id = resp.get("orderID")
            log.info("Limit order posted", token_id=token_id, side=side,
                     price=rounded_price, size=size, post_only=post_only,
                     order_type=order_type_enum, order_id=order_id)
            if order_id:
                _append_order_event(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    price=rounded_price,
                    size=size,
                    order_type="limit_taker_fak" if not post_only else "limit",
                    action="placed",
                    market_id=market.condition_id if market else "",
                )
            return order_id
        except Exception as exc:
            exc_str = str(exc)
            # "crosses book" only applies to post_only orders: a taker limit at a
            # crossing price is valid and fills immediately — no retry needed.
            if post_only and "crosses book" in exc_str and tick > 0:
                # Back off one tick (away from the inside) and retry once.
                retry_price = self._round_to_tick(
                    rounded_price - tick if side == "BUY" else rounded_price + tick, tick
                )
                if 0.0 < retry_price < 1.0 and abs(retry_price - rounded_price) >= tick * 0.5:
                    try:
                        # PM requires ≥ $1 notional per GTC order.  If the price
                        # back-off reduces notional below $1, raise size to compensate.
                        retry_size = size
                        retry_notional = retry_size * retry_price
                        if retry_notional < 1.0 and retry_price > 0:
                            retry_size = self._round_to_tick(
                                math.ceil(1.0 / retry_price / tick) * tick, tick
                            )
                            log.debug(
                                "place_limit cross-retry: size raised to meet $1 minimum",
                                token_id=token_id, retry_price=retry_price,
                                original_size=size, retry_size=retry_size,
                            )
                        order_args2 = OrderArgs(
                            token_id=token_id, price=retry_price, size=retry_size, side=side
                        )
                        signed2 = await asyncio.to_thread(self._clob.create_order, order_args2)
                        resp2 = await asyncio.to_thread(
                            self._clob.post_order, signed2, OrderType.GTC, True
                        )
                        order_id2 = resp2.get("orderID")
                        log.warning(
                            "Limit order posted with cross-adjusted price",
                            token_id=token_id, side=side,
                            attempted=rounded_price, actual=retry_price,
                            size=retry_size, order_id=order_id2,
                        )
                        return order_id2
                    except Exception as exc2:
                        log.error("place_limit failed after cross-adjustment",
                                  exc=str(exc2), token_id=token_id)
                        return None
            log.error("place_limit failed", exc=exc_str, token_id=token_id)
            return None

    async def place_market(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        price: float,       # worst-case price floor (slippage protection); 0 = auto from book
        size: float,
        market: Optional[PMMarket] = None,
    ) -> Optional[str]:
        """
        Place a FAK (Fill-And-Kill) market order — sweeps available liquidity
        immediately and cancels any unfilled remainder.

        Docs: https://docs.polymarket.com/trading/orders/create
          BUY  amount = USD to spend; SELL amount = shares to sell.
          price = worst-case limit (slippage floor), not an execution target.
          FAK is preferred over FOK for exits: partial fills are acceptable.

        In paper mode: instant fake fill.
        In live mode: proper create_market_order SDK path, NOT a GTC limit.
        """
        if self._paper_mode:
            log.info("[PAPER] place_market", token_id=token_id, side=side, price=price, size=size)
            return f"paper-mkt-{int(time.time())}"

        if self._clob is None:
            log.error("place_market: CLOB client not initialised")
            return None

        tick = market.tick_size if market else 0.01
        rounded_price = self._round_to_tick(price, tick) if price > 0 else 0.0

        try:
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=size,   # for SELL: number of shares; for BUY: USD amount
                side=side,
                price=rounded_price,
                order_type=OrderType.FAK,
            )
            # Sign in a thread — create_market_order() is blocking (requests).
            # Keeping the event loop alive during signing means WS book-cache
            # updates continue right up until post_order() fires.
            signed = await asyncio.to_thread(self._clob.create_market_order, market_args)
            # Post in a thread — blocking HTTP POST.
            resp = await asyncio.to_thread(self._clob.post_order, signed, OrderType.FAK)
            order_id = resp.get("orderID")
            log.info("Market order posted (FAK)", token_id=token_id, side=side,
                     price=rounded_price, size=size, order_id=order_id)
            if order_id:
                _append_order_event(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    price=rounded_price,
                    size=size,
                    order_type="market_fak",
                    action="placed",
                    market_id=market.condition_id if market else "",
                )
            return order_id
        except Exception as exc:
            log.error("place_market failed", exc=str(exc), token_id=token_id)
            return None

    async def get_order_fill_rest(self, order_id: str) -> Optional[dict]:
        """Fetch fill details from the REST CLOB without the 1-second sleep.

        Used as the fallback when a WS fill future times out or returns a
        MATCHED message without price/size_matched fields.  The caller is
        responsible for ensuring the order is sufficiently settled before
        calling (e.g. after a WS MATCHED event or a >5 s timeout).

        Returns a dict with keys: price, size_matched, size_remaining, status
        or None if the order is not found / has no fills.
        """
        if self._paper_mode or self._clob is None:
            return None
        try:
            order = await asyncio.to_thread(self._clob.get_order, order_id)
            if not order:
                log.warning("get_order_fill_rest: order not found", order_id=order_id[:20])
                return None
            size_matched = float(order.get("size_matched") or 0)
            if size_matched <= 0:
                return None
            # Use the order's own price as the fill price.  The associated-trade
            # endpoint returns data from the counterparty's perspective (e.g. the
            # complementary YES-token seller), which produces wrong price/size values
            # (observed: price=0.97 size=1822 for a 0.035 × 28.57 hedge order).
            # The order price is a safe upper bound — GTC fills at order price or better.
            fill_price = float(order.get("price") or 0)
            if fill_price <= 0:
                return None
            size_total = float(order.get("size") or order.get("original_size") or 0)
            size_remaining = max(0.0, size_total - size_matched)
            status = str(order.get("status") or "MATCHED")
            log.info(
                "Order fill confirmed from CLOB (REST fallback)",
                order_id=order_id[:20],
                fill_price=fill_price,
                fill_size=size_matched,
            )
            return {
                "price": fill_price,
                "size_matched": size_matched,
                "size_remaining": size_remaining,
                "status": status,
            }
        except Exception as exc:
            log.warning("get_order_fill_rest: failed", order_id=order_id[:20], exc=str(exc))
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single resting order by ID. Tolerates 404 (already filled/cancelled)."""
        if self._paper_mode:
            log.debug("[PAPER] cancel_order", order_id=order_id)
            return True
        if self._clob is None:
            return False
        try:
            await asyncio.to_thread(self._clob.cancel, order_id)
            log.debug("Order cancelled", order_id=order_id)
            return True
        except Exception as exc:
            # 404-equivalent (already gone) is not an error — just log and continue
            log.debug("cancel_order: order already gone or failed",
                      order_id=order_id, exc=str(exc))
            return False

    async def cancel_all(self) -> bool:
        if self._paper_mode:
            log.info("[PAPER] cancel_all")
            return True
        if self._clob is None:
            return False
        try:
            await asyncio.to_thread(self._clob.cancel_all)
            log.info("All PM orders cancelled")
            return True
        except Exception as exc:
            log.error("cancel_all failed", exc=str(exc))
            return False

    async def _user_ws_loop(self) -> None:
        """Subscribe to the PM user channel to receive live order fill events.

        Reconnects automatically with exponential backoff on disconnect/error.
        Fires `_fire_order_fill(msg)` for every MATCHED order event received.
        """
        if self._api_creds is None:
            log.warning("User WS: no API credentials — fill events will not arrive via WS; all fills will use REST fallback")
            return
        creds = self._api_creds
        log.info("PM user WS starting", api_key=creds.api_key[:8] + "...")
        sub_msg = json.dumps({
            "auth": {
                "apiKey": creds.api_key,
                "secret": creds.api_secret,
                "passphrase": creds.api_passphrase,
            },
            "markets": [],
            "assets_ids": [],
            "type": "user",
        })
        backoff = 1.0
        first_connect = True
        while self._running:
            try:
                async with websockets.connect(
                    config.PM_USER_WS_URL,
                    ping_interval=config.PM_WS_PING_INTERVAL,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(sub_msg)
                    log.info("PM user WS connected — fill events active")
                    if not first_connect:
                        # Reconnect after a gap: fills may have been missed during
                        # the disconnect window.  Fire reconciliation callbacks.
                        log.info("PM user WS reconnected — triggering fill-gap reconciliation")
                        await self._fire_user_ws_reconnect()
                    first_connect = False
                    backoff = 1.0
                    async for raw in ws:
                        if not isinstance(raw, str):
                            continue
                        if not raw.startswith("{") and not raw.startswith("["):
                            if "INVALID" in raw.upper():
                                log.warning("PM user WS auth rejected — check API creds")
                            continue
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        msgs = parsed if isinstance(parsed, list) else [parsed]
                        for msg in msgs:
                            if not isinstance(msg, dict):
                                continue
                            log.debug("PM user WS msg", status=msg.get("status"), type=msg.get("type"), keys=list(msg.keys())[:8])
                            # PM sends status=="MATCHED" (or "FILLED") on order fill events.
                            # Also handle nested {"event_type": "order", "order": {...}} format.
                            _status = msg.get("status", "")
                            _type   = msg.get("type", "")
                            if _status.upper() in ("MATCHED", "FILLED") or _type.upper() in ("MATCHED", "FILLED"):
                                await self._fire_order_fill(msg)
                            elif msg.get("event_type") == "order":
                                inner = msg.get("order") or {}
                                if isinstance(inner, dict) and inner.get("status", "").upper() in ("MATCHED", "FILLED"):
                                    await self._fire_order_fill(inner)
            except ConnectionClosed as exc:
                log.warning("PM user WS disconnected", code=exc.code)
            except Exception as exc:
                log.error("PM user WS error", exc=str(exc))
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def get_live_orders(self) -> list[dict]:
        """Return currently resting (LIVE) orders from the CLOB API."""
        if self._clob is None:
            return []
        try:
            orders = await asyncio.to_thread(self._clob.get_orders, OpenOrderParams())
            return orders if isinstance(orders, list) else []
        except Exception as exc:
            log.error("get_live_orders failed", exc=str(exc))
            return []

    async def get_token_balance(self, token_id: str) -> Optional[float]:
        """Return actual CTF token balance from the CLOB API.

        This is the authoritative source of truth for how many tokens are in the
        wallet.  Use this as the SELL size for exit orders instead of pos.size,
        which may be slightly off due to taker-fee deductions applied at fill time.

        Returns the balance as a token count (float), or None if unavailable
        (paper mode, CLOB not initialised, or API error).
        """
        if self._paper_mode or self._clob is None:
            return None
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            resp = await asyncio.to_thread(
                self._clob.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id),
            )
            raw = resp.get("balance") if isinstance(resp, dict) else None
            if raw is None:
                return None
            return float(raw) / 1_000_000  # micro-token → token
        except Exception as exc:
            log.warning("get_token_balance failed", token_id=token_id[:20], exc=str(exc))
            return None

    async def get_live_positions(self) -> list[dict]:
        """Return open token positions from the Polymarket Data API.

        Positions are held by the funder (proxy) wallet, NOT the signing key.
        clob.get_address() returns the ECDSA signer key address which is
        different — always use config.POLY_FUNDER for the Data API query.
        """
        address = config.POLY_FUNDER
        if not address:
            log.error("get_live_positions: POLY_FUNDER not configured")
            return []
        url = f"{config.PM_DATA_API_URL}/positions"
        params = {"user": address, "sizeThreshold": "0.01"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning(
                            "get_live_positions: Data API error", status=resp.status
                        )
                        return []
                    data = await resp.json()
                    return data if isinstance(data, list) else []
        except Exception as exc:
            log.error("get_live_positions failed", exc=str(exc))
            return []

    async def fetch_market_resolution(self, condition_id: str) -> Optional[float]:
        """Query the CLOB API for the resolved YES-token price.

        Uses GET https://clob.polymarket.com/markets/{condition_id} which returns
        each outcome token with a ``winner`` flag and the final settlement price.

        Returns 1.0 if YES/Up won, 0.0 if NO/Down won, or None if not yet resolved.
        Callers convert to WIN/LOSS based on position side:
            YES / UP  wins when resolved_yes_price == 1.0
            NO  / DOWN wins when resolved_yes_price == 0.0
        """
        url = f"{config.POLY_HOST}/markets/{condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    if not data.get("closed"):
                        return None
                    tokens = data.get("tokens") or []
                    if not tokens:
                        return None
                    # Find the YES/Up token by its outcome label — more robust than
                    # relying on ordering, which can vary across markets.
                    for tok in tokens:
                        if str(tok.get("outcome", "")).lower() in ("yes", "up"):
                            yes_price = tok.get("price")
                            if yes_price is not None:
                                return float(yes_price)
                    # Outcome label absent — fall back to winner flag or first token.
                    for tok in tokens:
                        if tok.get("winner") is True:
                            # This token won; determine if it's the YES/UP side.
                            is_yes = str(tok.get("outcome", "")).lower() not in ("no", "down")
                            return 1.0 if is_yes else 0.0
                    # Last resort: assume tokens[0] is YES/Up.
                    yes_price = tokens[0].get("price")
                    if yes_price is not None:
                        return float(yes_price)
        except Exception as exc:
            log.debug(
                "fetch_market_resolution failed",
                condition_id=condition_id[:16],
                exc=str(exc),
            )
        return None

    async def fetch_token_side(self, condition_id: str, token_id: str) -> Optional[str]:
        """Determine whether a token is the YES/Up or NO/Down side of a market.

        Calls GET /markets/{condition_id} and matches token_id against the token
        list.  Returns "yes" or "no", or None on API failure / unrecognised token.

        Used as a fallback when the local markets cache has evicted the market
        (e.g. short-lived 5m buckets that expire before the auto-redeem cycle).
        """
        url = f"{config.POLY_HOST}/markets/{condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    tokens = data.get("tokens") or []
                    for tok in tokens:
                        if tok.get("token_id") == token_id:
                            outcome = str(tok.get("outcome", "")).lower()
                            return "yes" if outcome in ("yes", "up") else "no"
        except Exception as exc:
            log.debug("fetch_token_side failed", condition_id=condition_id[:16], exc=str(exc))
        return None

    async def fetch_market_is_closed(self, condition_id: str) -> Optional[bool]:
        """Query the CLOB API and return whether the market has been closed/settled.

        This is a lightweight companion to fetch_market_resolution() that lets
        callers distinguish:
            True  — CLOB says closed=True (PM has processed settlement)
            False — CLOB says closed=False (PM settlement still in-progress; wait)
            None  — network error or unexpected response; state unknown
        """
        url = f"{config.POLY_HOST}/markets/{condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    return bool(data.get("closed"))
        except Exception as exc:
            log.debug(
                "fetch_market_is_closed failed",
                condition_id=condition_id[:16],
                exc=str(exc),
            )
        return None

    async def fetch_price_to_beat(self, event_slug: str) -> Optional[float]:
        """Fetch the canonical opening strike for an Up/Down market from the Gamma API.

        Queries GET /events/slug/{event_slug} and returns
        ``eventMetadata.priceToBeat`` — the exact oracle price at the start of
        the time window, as used by Polymarket's settlement contract.

        Returns None if the slug is blank, the market hasn't opened yet (metadata
        absent), or the network request fails.
        """
        if not event_slug:
            return None
        url = f"{config.GAMMA_HOST}/events/slug/{event_slug}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    meta = data.get("eventMetadata")
                    if not isinstance(meta, dict):
                        return None
                    ptb = meta.get("priceToBeat")
                    if ptb is not None:
                        return float(ptb)
        except Exception as exc:
            log.debug(
                "fetch_price_to_beat failed",
                slug=event_slug,
                exc=str(exc),
            )
        return None

    async def fetch_crypto_price_ptb(
        self,
        symbol: str,
        event_start_time: str,
        end_date: "Optional[datetime]",
    ) -> Optional[float]:
        """Fetch the opening strike for an Up/Down market via the Polymarket
        crypto-price API (``polymarket.com/api/crypto/crypto-price``).

        This endpoint is populated immediately when the window opens and works
        for ALL recurring market types — 5m, 15m, 4h, daily, weekly.

        Parameters
        ----------
        symbol:
            Uppercase coin ticker, e.g. ``"BTC"``, ``"ETH"``, ``"SOL"``.
        event_start_time:
            ISO timestamp of the window open, e.g. ``"2026-04-12T14:20:00Z"``.
            Comes from ``PMMarket.event_start_time``.
        end_date:
            Window close datetime (``PMMarket.end_date``).

        Returns
        -------
        float or None
            ``openPrice`` reported by the API, or ``None`` on any failure.
        """
        if not symbol or not event_start_time or end_date is None:
            return None
        # Polymarket settlement uses the UMA/Chainlink oracle, whose openPrice is
        # exposed via variant="fifteen" for ALL bucket durations (5m, 15m, 1h, 4h,
        # daily, weekly).  Live testing confirms this value exactly matches
        # Gamma's eventMetadata.priceToBeat for every market type.  Dynamic
        # variant mapping ("five", "sixty", etc.) returns values from a different
        # high-frequency feed that diverges from the settlement oracle — do NOT use.
        end_str = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "symbol": symbol.upper(),
            "eventStartTime": event_start_time,
            "variant": "fifteen",
            "endDate": end_str,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://polymarket.com/",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://polymarket.com/api/crypto/crypto-price",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    ptb = data.get("openPrice")
                    if ptb is not None:
                        return float(ptb)
        except Exception as exc:
            log.debug(
                "fetch_crypto_price_ptb failed",
                symbol=symbol,
                event_start_time=event_start_time,
                exc=str(exc),
            )
        return None

    async def fetch_gamma_settle_spot(self, market_slug: str) -> Optional[float]:
        """Fetch the settlement spot price for a resolved market via the Gamma API.

        Calls ``GET /events/slug/{market_slug}`` (the official public Gamma API)
        and extracts ``eventMetadata.closePrice`` — the exact underlying spot price
        published by Polymarket at market resolution.  This is the authoritative
        source that the PM settlement contract used; it is independent of the live
        oracle and valid for all bucket durations (5m, 15m, 1h, 4h, daily…).

        Falls back to ``None`` when the market is not yet resolved (``closePrice``
        absent), the slug is blank, or the request fails.
        """
        if not market_slug:
            return None
        url = f"{config.GAMMA_HOST}/events/slug/{market_slug}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    meta = data.get("eventMetadata")
                    if not isinstance(meta, dict):
                        return None
                    close = meta.get("closePrice")
                    if close is not None:
                        return float(close)
        except Exception as exc:
            log.debug(
                "fetch_gamma_settle_spot failed",
                slug=market_slug,
                exc=str(exc),
            )
        return None

    async def fetch_resolve_spot_price(
        self,
        symbol: str,
        event_start_time: str,
        end_date: "Optional[datetime]",
    ) -> Optional[float]:
        """Fetch the final oracle price at market close from the Polymarket
        crypto-price API (``polymarket.com/api/crypto/crypto-price``).

        The API returns ``closePrice`` — the actual underlying spot price at the
        moment the time window closed, which is the value used by Polymarket's
        settlement contract to determine the binary outcome.  This is distinct
        from ``openPrice`` (the strike / priceToBeat at window open).

        NOTE: ``variant`` is mapped from market duration so the API returns data
        for all bucket types (5m, 15m, 1h, 4h, daily, weekly).

        Returns ``closePrice`` on success, or ``None`` on any failure.
        """
        if not symbol or not event_start_time or end_date is None:
            return None
        # Polymarket settlement uses the UMA/Chainlink oracle.  The closePrice
        # for this oracle is also exposed via variant="fifteen" — the same variant
        # used for openPrice (priceToBeat).  Use "fifteen" for ALL bucket durations.
        end_str = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "symbol": symbol.upper(),
            "eventStartTime": event_start_time,
            "variant": "fifteen",
            "endDate": end_str,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://polymarket.com/",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://polymarket.com/api/crypto/crypto-price",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    if not isinstance(data, dict):
                        return None
                    close = data.get("closePrice")
                    if close is not None:
                        return float(close)
        except Exception as exc:
            log.debug(
                "fetch_resolve_spot_price failed",
                symbol=symbol,
                event_start_time=event_start_time,
                exc=str(exc),
            )
        return None

    # ── Accessors ──────────────────────────────────────────────────────────────

    def get_markets(self) -> dict[str, PMMarket]:
        return dict(self._markets)

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self._books.get(token_id)

    @property
    def _ws_connected(self) -> bool:
        """True if at least one WS shard is currently connected."""
        return any(s.connected for s in self._shards.values())

    @property
    def sub_token_count(self) -> int:
        """Total tokens subscribed across all WS shards."""
        return sum(s.subscribed_count for s in self._shards.values())

    @property
    def sub_rejected_count(self) -> int:
        """Total INVALID OPERATION rejections across all WS shards."""
        return sum(s.rejected_count for s in self._shards.values())

    def get_mid(self, token_id: str) -> Optional[float]:
        snap = self._books.get(token_id)
        return snap.mid if snap else None

    def pin_tokens(self, token_ids: set[str]) -> None:
        """Ensure these token IDs remain WS-subscribed regardless of market refresh.
        Call with the YES token IDs of all open positions."""
        self._pinned_tokens = token_ids

    def register_for_book_updates(self, token_ids: set[str]) -> None:
        """Register additional tokens for WS book subscriptions, bypassing the maker
        TTE/volume filters.  Used by non-maker strategies (e.g. momentum) to subscribe
        to a broader set of bucket markets.  Replaces the previous set on each call;
        triggers an immediate _update_shards pass so new tokens are subscribed within
        one event-loop tick rather than waiting up to 60s for the market-refresh cycle."""
        self._extra_tokens = token_ids
        if self._running:
            asyncio.ensure_future(self._update_shards())

    def fee_free_markets(self) -> list[PMMarket]:
        return [m for m in self._markets.values() if m.is_fee_free]

    def markets_by_type(self, market_type: str) -> list[PMMarket]:
        return [m for m in self._markets.values() if m.market_type == market_type]
