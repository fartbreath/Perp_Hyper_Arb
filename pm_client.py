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
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

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
    "ADA":  ["cardano"],
    "AVAX": ["avalanche"],
    "LINK": ["chainlink"],
    "DOT":  ["polkadot"],
    "SUI":  ["sui"],
    "APT":  ["aptos"],
    "NEAR": ["near-protocol"],
    "ARB":  ["arbitrum"],
    "OP":   ["optimism"],
    "TON":  ["toncoin"],
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
        self._price_callbacks: list[Callable] = []
        self._order_fill_callbacks: list[Callable] = []
        self._api_creds: Optional[ApiCreds] = None  # populated after CLOB auth
        self._running = False
        self._heartbeat_id: int = 0
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
        for cb in self._price_callbacks:
            try:
                await cb(token_id, mid)
            except Exception as exc:
                log.error("price_change callback error", exc=str(exc))

    def on_order_fill(self, callback: Callable) -> None:
        """Register an async callback(order_data) called when a PM order is matched."""
        self._order_fill_callbacks.append(callback)

    async def _fire_order_fill(self, order_data: dict) -> None:
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
        client = ClobClient(
            host=config.POLY_HOST,
            key=self._private_key,
            chain_id=137,  # Polygon mainnet
        )
        try:
            creds: ApiCreds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            self._api_creds = creds  # stored for user WS authentication
            log.info("CLOB client authenticated")
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
        # 3. Individual market title keyword matching — fallback for one-off events.
        if series_title_override:
            market_type = _classify_market(series_title_override)
            if market_type == "milestone":
                # Series title gave no specific bucket type; try recurrence next.
                if recurrence_override and recurrence_override in _RECURRENCE_TO_MARKET_TYPE:
                    market_type = _RECURRENCE_TO_MARKET_TYPE[recurrence_override]
                if market_type == "milestone":
                    market_type = _classify_market(title)
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
            log.info("Pruned expired markets",
                     count=len(to_remove), remaining=len(self._markets))
        return len(to_remove)

    async def _market_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.MARKET_REFRESH_INTERVAL)
            self._prune_expired_markets()
            await self._refresh_markets()
            await self._update_shards()

    # ── WebSocket shards ───────────────────────────────────────────────────────

    def _next_hb_id(self) -> int:
        """Return next heartbeat ID and record the timestamp for monitoring."""
        self._heartbeat_id += 1
        self._last_heartbeat_ts = time.time()
        return self._heartbeat_id

    async def _clob_heartbeat_loop(self) -> None:
        """Periodically POST /heartbeat to keep open CLOB orders alive.

        PM cancels all open orders if a heartbeat is not received within 10s
        of the last one (opt-in: once you start sending heartbeats, you must
        continue).  This is a REST call — the market WS channel does NOT accept
        JSON heartbeat messages (returns INVALID OPERATION).  Only runs in live
        (non-paper) mode with an authenticated ClobClient.
        """
        while self._running:
            try:
                hb_id = str(self._next_hb_id())
                await asyncio.to_thread(self._clob.post_heartbeat, hb_id)
                log.debug("CLOB heartbeat sent", hb_id=hb_id)
            except Exception as exc:
                log.warning("CLOB heartbeat REST failed", exc=str(exc))
            await asyncio.sleep(config.PM_HEARTBEAT_INTERVAL)

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
        tokens_wanted: set[str] = set(pinned + others)

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
                log.info("PM WS shard started", shard_id=sid, tokens=len(chunk))

        # ── Cleanup empty shards ───────────────────────────────────────────────
        empty = [sid for sid, s in self._shards.items() if len(s._tokens) == 0]
        for sid in empty:
            await self._shards.pop(sid).stop()
            log.info("PM WS shard stopped (empty)", shard_id=sid)

        log.info("PM WS shards updated",
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
    ) -> Optional[str]:
        """
        Place a post-only limit order.  Returns order_id on success, None on failure.

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
            # post_only ensures we're always a maker (order rejected if it would cross)
            signed = self._clob.create_order(order_args, options={"post_only": True})
            resp = self._clob.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID")
            log.info("Limit order posted", token_id=token_id, side=side,
                     price=rounded_price, size=size, order_id=order_id)
            return order_id
        except Exception as exc:
            log.error("place_limit failed", exc=str(exc), token_id=token_id)
            return None

    async def place_market(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        price: float,       # aggressive crossing price — guarantees fill
        size: float,
        market: Optional[PMMarket] = None,
    ) -> Optional[str]:
        """
        Place an immediate (taker) order.  Crosses the spread — fills at the
        best available price.  No post_only constraint.

        In paper mode: behaves identically to place_limit (instant fake fill).
        In live mode:  GTC limit without post_only — sweeps the book at `price`.
        Use for manual closes and any exit that must fill immediately.
        """
        if self._paper_mode:
            log.info("[PAPER] place_market", token_id=token_id, side=side, price=price, size=size)
            return f"paper-mkt-{int(time.time())}"

        if self._clob is None:
            log.error("place_market: CLOB client not initialised")
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
            # GTC without post_only — will cross the spread and fill immediately
            signed = self._clob.create_order(order_args)
            resp = self._clob.post_order(signed, OrderType.GTC)
            order_id = resp.get("orderID")
            log.info("Market order posted", token_id=token_id, side=side,
                     price=rounded_price, size=size, order_id=order_id)
            return order_id
        except Exception as exc:
            log.error("place_market failed", exc=str(exc), token_id=token_id)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single resting order by ID. Tolerates 404 (already filled/cancelled)."""
        if self._paper_mode:
            log.debug("[PAPER] cancel_order", order_id=order_id)
            return True
        if self._clob is None:
            return False
        try:
            self._clob.cancel(order_id)
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
            self._clob.cancel_all()
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
            log.warning("User WS: no API credentials — fill events will not arrive")
            return
        creds = self._api_creds
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
        while self._running:
            try:
                async with websockets.connect(
                    config.PM_USER_WS_URL,
                    ping_interval=config.PM_WS_PING_INTERVAL,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(sub_msg)
                    log.info("PM user WS connected — fill events active")
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
                            # PM sends status=="MATCHED" on order fill events
                            if msg.get("status") == "MATCHED":
                                await self._fire_order_fill(msg)
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
            orders = await asyncio.to_thread(self._clob.get_orders, {"status": "LIVE"})
            return orders if isinstance(orders, list) else []
        except Exception as exc:
            log.error("get_live_orders failed", exc=str(exc))
            return []

    async def get_live_positions(self) -> list[dict]:
        """Return open token positions from the Polymarket Data API."""
        if self._clob is None:
            return []
        try:
            address: str = await asyncio.to_thread(self._clob.get_address)
        except Exception as exc:
            log.error("get_live_positions: could not get wallet address", exc=str(exc))
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

    def fee_free_markets(self) -> list[PMMarket]:
        return [m for m in self._markets.values() if m.is_fee_free]

    def markets_by_type(self, market_type: str) -> list[PMMarket]:
        return [m for m in self._markets.values() if m.market_type == market_type]
