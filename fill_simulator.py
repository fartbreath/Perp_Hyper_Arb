"""
fill_simulator.py — Paper-trade fill simulator for Strategy 1 (Market Making).

How it works
------------
The maker strategy posts paper limit orders via pm_client.place_limit(), which in
paper mode immediately returns a fake order ID without touching the real CLOB.  No fill
event ever arrives, so without this simulator the maker runs silently with no P&L.

The simulator polls every FILL_CHECK_INTERVAL seconds and checks each resting paper
quote against the live order-book data that the real PM WebSocket is streaming.

Fill detection — CLOB taker-size model
---------------------------------------
Real CLOBs fill resting orders when an incoming taker order is large enough to
consume the competing liquidity at our price level and reach our position in the queue.

Each sweep per resting quote:
  1. Touch check: is our quote at or better than the best bid/ask?
     (Same as before — no fill possible if we're behind the touch.)
  2. Taker arrival: draw from a Bernoulli with probability PAPER_FILL_PROB_BASE
     (or PAPER_FILL_PROB_NEW_MARKET for new markets).  Models whether a marketable
     taker order arrives at our price level this tick.
     Adverse-selection: when HL has moved against our fill direction since the last
     sweep (informed flow), arrival probability is scaled DOWN by multiplying by
     PAPER_ADVERSE_FILL_MULTIPLIER — informed takers are faster bots that jump the
     queue; we are less likely to receive a fill at a stale price.
     Quote-age decay: arrival_prob is further reduced for older resting quotes
     (time priority erodes as newer makers post tighter or at the same level).
  3. Taker size: sample from Exponential(mean = max(quote.size×0.75, depth×0.5)),
     lower-bounded at 1 contract.  Adverse moves do not inflate taker size.
  4. Filled amount: the taker first absorbs a random fraction of competing depth
     (modelling our random queue position at the price level), then any residual
     capacity reaches us:
         competing = depth × uniform(0, 1)   # random queue position
         filled    = min(quote.size, max(0, taker_size − competing))
     If filled > 0, consume_fill(key, filled) keeps the remainder alive.

Position semantics
------------------
  BUY YES at bid  → Position(side="YES", entry_price=bid)   profit if price ↑
  SELL YES at ask → Position(side="NO",  entry_price=ask)   profit if price ↓

The existing PositionMonitor handles all exits (profit target / stop-loss / time-stop).
The market-making P&L naturally appears in trades.csv tagged strategy="maker".

Configuration
-------------
  FILL_CHECK_INTERVAL              seconds between fill sweeps  (default 5)
  PAPER_FILL_PROB_BASE             taker arrival probability for normal markets
  PAPER_FILL_PROB_NEW_MARKET       taker arrival probability for new markets
  PAPER_ADVERSE_SELECTION_PCT      HL move fraction that triggers adverse scaling
  PAPER_ADVERSE_FILL_MULTIPLIER    arrival_prob multiplier when adverse (< 1 → reduces fills)
  PAPER_REBATE_CAPTURE_RATE        realistic fraction of theoretical per-fill rebate captured
"""
from __future__ import annotations

import asyncio
import csv
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from logger import get_bot_logger
from strategies.maker.strategy import MakerStrategy
from strategies.maker.fill_logic import open_position_from_fill
from monitor import PositionMonitor
from market_data.pm_client import PMClient
from risk import RiskEngine

# ── Fills CSV ─────────────────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
FILLS_CSV = _DATA_DIR / "fills.csv"
FILLS_HEADER = [
    "timestamp", "market_id", "market_title", "underlying",
    "order_side", "position_side",
    "fill_price", "contracts_filled", "fill_cost_usd",
    "book_bid", "book_ask", "depth_at_level",
    "arrival_prob", "mean_taker", "taker_size_drawn",
    "hl_mid", "hl_move_pct", "adverse",
    "total_fills_session",
    "signal_score",
    "rebate_usd",
]

log = get_bot_logger(__name__)

# ── Session stats — readable by /health endpoint ─────────────────────────────
_adverse_triggers_session: int = 0
_hl_max_move_pct_session: float = 0.0


def get_fill_session_stats() -> dict:
    """Return adverse-detection session counters for the /health endpoint."""
    return {
        "adverse_triggers_session": _adverse_triggers_session,
        "hl_max_move_pct_session": round(_hl_max_move_pct_session, 5),
    }


def reset_fill_session_stats() -> None:
    """Reset session counters (call at bot start)."""
    global _adverse_triggers_session, _hl_max_move_pct_session
    _adverse_triggers_session = 0
    _hl_max_move_pct_session = 0.0


class FillSimulator:
    """
    Async task that simulates market-maker fills in paper-trading mode.

    Instantiate, then call ``await simulator.start()``.
    """

    def __init__(
        self,
        pm: PMClient,
        maker: MakerStrategy,
        risk: RiskEngine,
        monitor: PositionMonitor,
    ) -> None:
        self._pm = pm
        self._maker = maker
        self._risk = risk
        self._monitor = monitor
        self._running = False
        self._fills_total: int = 0
        self._started_at: float = time.time()
        # Tracks HL mid at the start of each sweep per coin.
        # Used to detect adverse price moves that indicate we're being picked off.
        self._prev_hl_mids: dict[str, float] = {}
        self._ensure_fills_csv()

    def _ensure_fills_csv(self) -> None:
        """Create fills.csv with header if it doesn't exist; migrate if schema changes."""
        if not FILLS_CSV.exists():
            with FILLS_CSV.open("w", newline="") as f:
                csv.writer(f).writerow(FILLS_HEADER)
            return
        with FILLS_CSV.open("r", newline="") as f:
            reader = csv.reader(f)
            try:
                existing = next(reader)
            except StopIteration:
                existing = []
        if existing != FILLS_HEADER:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup = FILLS_CSV.with_name(f"fills_{ts}.csv.bak")
            FILLS_CSV.rename(backup)
            log.info("fills.csv schema changed — backed up old file", backup=str(backup))
            with FILLS_CSV.open("w", newline="") as f:
                csv.writer(f).writerow(FILLS_HEADER)

    async def start(self) -> None:
        if not config.PAPER_TRADING:
            log.info("FillSimulator disabled — not in paper trading mode")
            return
        reset_fill_session_stats()  # clear carry-over stats from any prior session
        self._running = True
        log.info(
            "FillSimulator started",
            check_interval=config.FILL_CHECK_INTERVAL,
            fill_prob_base=config.PAPER_FILL_PROB_BASE,
            fill_prob_new_market=config.PAPER_FILL_PROB_NEW_MARKET,
        )
        asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.FILL_CHECK_INTERVAL)
            if not config.STRATEGY_MAKER_ENABLED:
                continue
            try:
                await self._sweep()
            except Exception as exc:
                log.error("FillSimulator sweep error", exc=str(exc))

    async def _sweep(self) -> None:
        """
        CLOB-faithful sweep: for each resting quote, model a taker arrival and
        determine how much of the resting order gets filled.
        """
        quotes = self._maker.get_active_quotes()  # snapshot copy (key → ActiveQuote)

        for key, quote in quotes.items():
            market = self._pm._markets.get(quote.market_id)
            if market is None:
                continue

            book = self._pm._books.get(market.token_id_yes)
            if book is None:
                continue

            # BUY NO orders (ask key) are checked against the YES book using the
            # YES-equivalent price (1 - NO_price) on the SELL side — the YES and NO
            # books are mirrors so this gives the same depth/cross signals.
            is_no_buy = key.endswith("_ask")
            check_side  = "SELL" if is_no_buy else quote.side
            check_price = (1.0 - quote.price) if is_no_buy else quote.price

            # ── Touch check: are we competitive at the current best price? ────
            if not self._is_crossed(check_side, check_price, book):
                continue

            # ── Step 1: taker arrival probability ────────────────────────────
            market_age = time.time() - market.discovered_at
            arrival_prob = (
                config.PAPER_FILL_PROB_NEW_MARKET
                if market_age < config.NEW_MARKET_AGE_LIMIT
                else config.PAPER_FILL_PROB_BASE
            )

            # Adverse-selection: informed takers arrive more often and in larger
            # sizes when the HL mid moves against our fill direction.
            hl_move = self._hl_move_pct(market.underlying)

            # Per-bucket adversity threshold (A7): short-duration markets nearing
            # resolution are nearly fully-informational — any HL move indicates
            # adverse selection.  Longer markets tolerate larger moves before
            # classifying a fill as adversely selected.
            _ADVERSITY_THRESHOLDS: dict[str, float] = {
                "bucket_5m":     0.0001,   # any detectable move is signal near expiry
                "bucket_15m":    0.0003,
                "bucket_1h":     0.001,
                "bucket_4h":     0.002,
                "bucket_daily":  config.PAPER_ADVERSE_SELECTION_PCT,
                "bucket_weekly": config.PAPER_ADVERSE_SELECTION_PCT,
                "milestone":     config.PAPER_ADVERSE_SELECTION_PCT,
            }
            adverse_pct = _ADVERSITY_THRESHOLDS.get(
                market.market_type, config.PAPER_ADVERSE_SELECTION_PCT
            )

            is_adverse = (
                (check_side == "BUY" and hl_move is not None
                 and hl_move < -adverse_pct)
                or
                (check_side == "SELL" and hl_move is not None
                 and hl_move > adverse_pct)
            )

            # Track session max HL move for /health calibration indicator
            if hl_move is not None:
                global _hl_max_move_pct_session
                _hl_max_move_pct_session = max(_hl_max_move_pct_session, abs(hl_move))

            # Quote-age decay: newer quotes have better time-priority in the queue.
            age_s = time.time() - quote.posted_at
            arrival_prob *= max(0.2, 1.0 - age_s / config.MAX_QUOTE_AGE_SECONDS)

            if is_adverse:
                # Adverse move → reduce fill prob.  Informed takers are faster bots
                # that jump the queue; our stale quote gets fewer actual executions.
                arrival_prob *= config.PAPER_ADVERSE_FILL_MULTIPLIER
                global _adverse_triggers_session
                _adverse_triggers_session += 1

            if random.random() > arrival_prob:
                continue  # no taker arrived this sweep

            # ── Step 2: taker size ────────────────────────────────────────────
            # Mean taker is at least 75% of our quote size (so partial fills are
            # common) and grows with depth at our level (deeper books attract
            # bigger institutional takers that can sweep through them).
            depth = self._depth_at_level(book, check_side, check_price)
            mean_taker = max(quote.size * 0.75, depth * 0.5) if depth > 0 else quote.size * 0.75
            # No size inflation for adverse moves — lower arrival_prob already accounts
            # for adverse selection; inflating taker size compounds the bias incorrectly.
            taker_size = max(1.0, random.expovariate(1.0 / mean_taker))

            # ── Step 3: filled amount ─────────────────────────────────────────
            # Randomise queue position: we are not always last at our price level.
            # Draw a uniform fraction of depth that sits ahead of us in the queue.
            queue_fraction = random.uniform(0.0, 1.0)
            competing_depth = depth * queue_fraction
            filled_amt = min(quote.size, max(0.0, taker_size - competing_depth))
            if filled_amt <= 0.0:
                continue  # taker was fully absorbed by depth ahead of us

            fill_ctx = {
                "book_bid": book.best_bid,
                "book_ask": book.best_ask,
                "depth": depth,
                "queue_fraction": round(queue_fraction, 3),
                "arrival_prob": round(arrival_prob, 4),
                "mean_taker": round(mean_taker, 4),
                "taker_size": round(taker_size, 4),
            }
            await self._on_fill(key, quote, market, filled_amt, fill_ctx)

        # Snapshot HL mids at end of sweep for next sweep's adverse-selection check
        for quote in quotes.values():
            mk = self._pm._markets.get(quote.market_id)
            if mk:
                mid = self._maker.get_hl_mid(mk.underlying)
                if mid is not None:
                    self._prev_hl_mids[mk.underlying] = mid

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _hl_move_pct(self, underlying: str) -> Optional[float]:
        """Return HL mid % change since last sweep (+ve = price rose), or None."""
        prev = self._prev_hl_mids.get(underlying)
        if prev is None or prev <= 0:
            return None
        cur = self._maker.get_hl_mid(underlying)
        if cur is None:
            return None
        return (cur - prev) / prev

    def _depth_at_level(self, book, side: str, price: float) -> float:
        """Sum of competing contracts at the same price level as our quote."""
        # 0.005 = half a minimum PM tick — same value as strategy._CLOB_HALF_TICK
        if side == "BUY":
            return sum(s for (p, s) in book.bids if abs(p - price) <= 0.005)
        else:
            return sum(s for (p, s) in book.asks if abs(p - price) <= 0.005)

    # Maximum distance (in probability units) below the inside best bid/ask
    # at which a paper resting order is still considered reachable by incoming
    # takers.  Polymarket tick = 0.01; one tick (0.01) lets us model being one
    # position behind the inside without allowing stale quotes to keep collecting
    # fills long after the book has moved away from our price.
    _FILL_QUEUE_TOLERANCE: float = 0.01

    def _is_crossed(self, side: str, price: float, book) -> bool:
        """
        Return True when our resting paper order is reachable by an arriving taker.

        Real semantics:
          BUY  side: a SELL taker sweeps bids from best_bid downward.  We get hit
                     if our price is within _FILL_QUEUE_TOLERANCE below best_bid
                     (we are 0–3 ticks behind the inside bid) or above it (crossed).
          SELL side: a BUY  taker sweeps asks from best_ask upward.  We get hit
                     if our price is within _FILL_QUEUE_TOLERANCE above best_ask
                     (we are 0–3 ticks above the inside ask) or below it (crossed).

        Using strict `price >= best_bid` wrongly excluded all maker bids that sit
        one or two ticks below the real inside (which is the normal state for a
        paper quote).
        """
        if side == "BUY":
            bid = book.best_bid
            if bid is None:
                return False
            # Filled if we're within TOLERANCE ticks of the inside bid, or crossed
            return price >= bid - self._FILL_QUEUE_TOLERANCE
        else:  # SELL
            ask = book.best_ask
            if ask is None:
                return False
            return price <= ask + self._FILL_QUEUE_TOLERANCE

    # ── Fill handler ───────────────────────────────────────────────────────────

    async def _on_fill(
        self, key: str, quote, market, filled_size: float,
        fill_ctx: Optional[dict] = None,
    ) -> None:
        """
        Process a (possibly partial) simulated fill.

          1. Consume filled_size from the maker's resting quote (partial or full).
          2. Open a Position in the risk engine for the filled portion only.
          3. Update maker inventory (triggers hedge rebalance).
          4. If the remaining resting quote is below MAKER_QUOTE_SIZE_MIN, consume
             it immediately to avoid accumulating micro-positions (paper mode only;
             live CLOB fills arrive via user WS events and are handled separately).
        """
        await self._process_fill_slice(key, quote, market, filled_size, fill_ctx)

        # Real CLOB behaviour: a partially-filled remainder stays in the order book.
        # We do NOT auto-consume sub-minimum remainders — the reprice machinery will
        # cancel and re-quote the order on the next sweep if the size is too small.
        # Auto-consuming created extra micro-fills that do not happen in live trading.

    async def _process_fill_slice(
        self, key: str, quote, market, filled_size: float,
        fill_ctx: Optional[dict] = None,
    ) -> None:
        """Open a position for one filled slice (shared by primary fill + remainder flush)."""
        result = open_position_from_fill(
            maker=self._maker,
            risk=self._risk,
            monitor=self._monitor,
            key=key,
            fill_price=quote.price,
            filled_size=filled_size,
            market=market,
        )
        if result is None:
            return

        consumed = result.consumed
        fill_price = result.fill_price
        actual_filled = result.actual_filled
        position_side = result.position_side
        fill_cost_usd = result.fill_cost_usd

        # Paper-mode only: schedule HL hedge rebalance after fill
        self._maker.schedule_hedge_rebalance(market.underlying)

        hl_mid_now = self._maker.get_hl_mid(market.underlying)
        hl_mid_prev = self._prev_hl_mids.get(market.underlying)
        adverse = False
        hl_move_pct: Optional[float] = None
        if hl_mid_now is not None and hl_mid_prev is not None and hl_mid_prev > 0:
            hl_move_pct = round((hl_mid_now - hl_mid_prev) / hl_mid_prev, 6)
# Use position_side for adverse direction: YES position is adverse when HL
        # falls (YES probability drops); NO position is adverse when HL rises.
        adverse = (
            (result.position_side == "YES" and hl_move_pct is not None
             and hl_move_pct < -config.PAPER_ADVERSE_SELECTION_PCT)
            or (result.position_side == "NO" and hl_move_pct is not None
             and hl_move_pct > config.PAPER_ADVERSE_SELECTION_PCT)
            )

        self._fills_total += 1
        ctx = fill_ctx or {}
        log.info(
            "[PAPER FILL] Maker order filled",
            market=market.title[:60],
            market_id=consumed.market_id,
            underlying=market.underlying,
            order_side=consumed.side,
            position_side=position_side,
            fill_price=round(fill_price, 4),
            contracts_filled=round(actual_filled, 4),
            fill_cost_usd=round(fill_cost_usd, 4),
            book_bid=ctx.get("book_bid"),
            book_ask=ctx.get("book_ask"),
            depth_at_level=ctx.get("depth"),
            arrival_prob=ctx.get("arrival_prob"),
            mean_taker=ctx.get("mean_taker"),
            taker_size_drawn=ctx.get("taker_size"),
            hl_mid=hl_mid_now,
            hl_move_pct=hl_move_pct,
            adverse=adverse,
            total_fills=self._fills_total,
        )
        # Persist to fills.csv for audit / replay (survives server restarts)
        try:
            with FILLS_CSV.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=FILLS_HEADER).writerow({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "market_id": consumed.market_id,
                    "market_title": market.title[:80],
                    "underlying": market.underlying,
                    "order_side": consumed.side,
                    "position_side": position_side,
                    "fill_price": round(fill_price, 4),
                    "contracts_filled": round(actual_filled, 4),
                    "fill_cost_usd": round(fill_cost_usd, 4),
                    "book_bid": ctx.get("book_bid", ""),
                    "book_ask": ctx.get("book_ask", ""),
                    "depth_at_level": ctx.get("depth", ""),
                    "arrival_prob": ctx.get("arrival_prob", ""),
                    "mean_taker": ctx.get("mean_taker", ""),
                    "taker_size_drawn": ctx.get("taker_size", ""),
                    "hl_mid": hl_mid_now if hl_mid_now is not None else "",
                    "hl_move_pct": hl_move_pct if hl_move_pct is not None else "",
                    "adverse": adverse,
                    "total_fills_session": self._fills_total,
                    "signal_score": round(consumed.score, 2),
                    "rebate_usd": result.rebate_usd,
                })
        except Exception as csv_exc:
            log.warning("Failed to write fill to fills.csv", exc=str(csv_exc))

        # Trigger immediate reprice if quote was fully consumed
        self._maker.trigger_post_fill_reprice(key, market)

    # ── Stats ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "fills_total": self._fills_total,
            "running": self._running,
            "uptime_s": round(time.time() - self._started_at),
        }

