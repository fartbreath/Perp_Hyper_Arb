"""
strategies.maker.strategy — Strategy 1: PM Market Making + HL Delta Hedge.

Logic:
  1.  Listen to PM price_change callbacks to know when to reprice
  2.  Listen to HL BBO callbacks to detect moves > REPRICE_TRIGGER_PCT
  3.  For each tracked market: maintain a two-sided quote within
      max_incentive_spread (qualifies for maker rebates)
  4.  When net PM inventory per underlying exceeds HEDGE_THRESHOLD_USD,
      place/adjust an HL perp hedge
  5.  New-market opportunism: on newly discovered fee-free markets,
      post initial wide quotes before the crowd arrives

Market priority:
  - Priority A: feesEnabled=False — zero fees, pure spread capture
  - Priority B: feesEnabled=True, probability in [0.05-0.20] or [0.80-0.95]
  - Avoid:      feesEnabled=True near 0.50 (1.56% taker eats all edge)
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import config
from logger import get_bot_logger
from market_data.pm_client import PMClient, PMMarket, _MARKET_TYPE_DURATION_SECS
from market_data.hl_client import HLClient, BBO
from risk import RiskEngine, Position
import risk as risk_module
from strategies.base import BaseStrategy
from strategies.maker.signals import MakerSignal, ActiveQuote
from strategies.maker.math import (
    implied_sigma,
    bs_digital_coins,
    binary_delta,
    hedge_size_coins,
    parse_strike_from_title,
)
from strategies.scoring import score_maker

log = get_bot_logger(__name__)

# Half a minimum PM tick: used for price-level matching when summing CLOB depth.
# Must match fill_simulator._depth_at_level (0.005 = half of 0.01 tick).
_CLOB_HALF_TICK: float = 0.005


# ── Market priority helper ────────────────────────────────────────────────────

def _is_priority_market(market: PMMarket, mid: float) -> bool:
    """Return True if this market is worth quoting after fee-adjusted edge check."""
    if not market.is_fee_free:
        # Polymarket fee curve: fee ≈ 1.75% × p × (1-p)
        taker_fee_at_mid = config.PM_FEE_COEFF * mid * (1.0 - mid)
        # We EARN a rebate on top of capturing half the spread.
        effective_edge = market.max_incentive_spread / 2 + market.rebate_pct * taker_fee_at_mid
        if effective_edge < config.MIN_EDGE_PCT:
            return False
    return True


# ── Maker Strategy ────────────────────────────────────────────────────────────

class MakerStrategy(BaseStrategy):
    """
    Runs the market-making loop on PM crypto bucket markets.
    Instantiate, then call `await strategy.start()`.
    """

    def __init__(
        self,
        pm: PMClient,
        hl: HLClient,
        risk: RiskEngine,
        quote_size_usd: Optional[float] = None,
    ) -> None:
        self._pm = pm
        self._hl = hl
        self._risk = risk
        self._quote_size_override = quote_size_usd
        self._active_quotes: dict[str, ActiveQuote] = {}  # token_id → quote
        self._inventory: dict[str, float] = {}            # underlying → net USD
        self._last_hl_mids: dict[str, float] = {}         # coin → last mid when quoted
        self._coin_hedges: dict[str, dict] = {}
        self._signals: dict[str, MakerSignal] = {}
        self._hl_mid_history: dict[str, deque] = {}  # coin → rate-limited (ts, mid) history
        # ── Hedge cooldown / debounce ──────────────────────────────────────────
        self._last_hedge_ts: dict[str, float] = {}          # coin → unix ts of last executed hedge
        self._pending_hedge_tasks: dict[str, asyncio.Task] = {}  # coin → in-flight debounce task
        # ── Hedge execution quality ring-buffer (most-recent-first) ───────────
        self._hedge_quality: deque = deque(maxlen=200)
        # ── Naked-leg debounce (Fix B) ─────────────────────────────────────────
        # market_id → unix timestamp when the imbalance was first detected.
        # Reset when balance is restored or a force-close fires.
        self._imbalance_since: dict[str, float] = {}
        # ── Per-leg fill count tracker ─────────────────────────────────────────
        # (market_id, "YES"|"NO") → number of fill events on that leg this session.
        # Used by MAKER_MAX_FILLS_PER_LEG gate in _deploy_quote.
        # Pruned in _quote_age_watchdog for markets with no remaining open positions.
        self._leg_fill_counts: dict[tuple[str, str], int] = {}

    async def start(self) -> None:
        if not config.STRATEGY_MAKER_ENABLED:
            log.info("MakerStrategy disabled — waiting for STRATEGY_MAKER_ENABLED=True")
            while not config.STRATEGY_MAKER_ENABLED:
                await asyncio.sleep(5)
            log.info("MakerStrategy enabled via config — starting now")
        self._pm.on_price_change(self._on_pm_price_change)
        self._hl.on_bbo_update(self._on_hl_bbo_update)
        log.info("MakerStrategy started", quote_size_override=self._quote_size_override)
        asyncio.create_task(self._quote_age_watchdog())
        await self._refresh_all_quotes()

    async def stop(self) -> None:
        log.info("MakerStrategy stopping — cancelling pending hedge tasks",
                 pending_tasks=len(self._pending_hedge_tasks))
        for task in list(self._pending_hedge_tasks.values()):
            if not task.done():
                task.cancel()
        self._pending_hedge_tasks.clear()

    # ── Callbacks ──────────────────────────────────────────────────────────────

    async def _on_pm_price_change(self, token_id: str, new_mid: float) -> None:
        quote = self._active_quotes.get(token_id)
        if quote is None:
            return
        drift = abs(new_mid - quote.price)
        market = self._find_market_for_token(token_id)
        if market and drift > market.max_incentive_spread / 2:
            try:
                await self._reprice_market(market)
            except Exception as exc:
                log.warning("PM price-change reprice failed",
                            market=market.condition_id, exc=str(exc))

    async def _on_hl_bbo_update(self, coin: str, bbo: BBO) -> None:
        if bbo.mid is None:
            return
        # Rate-limited history (≤1 sample/s) for the vol filter in _evaluate_signal.
        hist = self._hl_mid_history.setdefault(coin, deque(maxlen=360))
        _now = time.time()
        if not hist or _now - hist[-1][0] >= 1.0:
            hist.append((_now, bbo.mid))
        last = self._last_hl_mids.get(coin, bbo.mid)
        move_pct = abs(bbo.mid - last) / last if last > 0 else 0.0
        if move_pct >= config.REPRICE_TRIGGER_PCT:
            self._last_hl_mids[coin] = bbo.mid
            try:
                await self._reprice_underlying(coin)
                self._schedule_hedge_rebalance(coin)
            except Exception as exc:
                log.warning("HL BBO reprice/hedge failed", coin=coin, exc=str(exc))

    # ── Quote age watchdog ─────────────────────────────────────────────────────

    async def _quote_age_watchdog(self) -> None:
        """Backstop: force-reprice any quote older than MAX_QUOTE_AGE_SECONDS.
        Also scans all markets every cycle to pick up newly-liquid markets."""
        while True:
            await asyncio.sleep(config.MAX_QUOTE_AGE_SECONDS)
            if not config.STRATEGY_MAKER_ENABLED:
                continue
            now = time.time()
            stale_market_ids: set[str] = set()
            for q in list(self._active_quotes.values()):
                if now - q.posted_at > config.MAX_QUOTE_AGE_SECONDS:
                    stale_market_ids.add(q.market_id)
            if stale_market_ids:
                log.debug("Quote age watchdog firing", stale_markets=len(stale_market_ids))
                for market in self._pm.get_markets().values():
                    if market.condition_id in stale_market_ids:
                        try:
                            await self._reprice_market(market)
                        except Exception as exc:
                            log.warning("Watchdog reprice failed",
                                        market=market.condition_id, exc=str(exc))
            await self._ensure_quoted_all()
            await self._check_naked_legs()

            # Prune fill counts for markets that no longer have any open positions.
            _active_mkt_ids = {p.market_id for p in self._risk.get_open_positions()}
            _stale_keys = [k for k in self._leg_fill_counts if k[0] not in _active_mkt_ids]
            for _k in _stale_keys:
                del self._leg_fill_counts[_k]

            coins_to_check: set[str] = {
                pos.underlying for pos in self._risk.get_open_positions()
            }
            coins_to_check.update(self._coin_hedges.keys())
            for coin in coins_to_check:
                try:
                    self._schedule_hedge_rebalance(coin)
                except Exception as exc:
                    log.warning("Hedge watchdog rebalance failed", coin=coin, exc=str(exc))

    # ── Quoting ────────────────────────────────────────────────────────────────

    async def _check_naked_legs(self) -> None:
        """Force-close legs that have been naked (imbalanced) for too long (Fix B).

        When one side's open contracts exceed the other by MAKER_NAKED_CLOSE_CONTRACTS
        and that imbalance has persisted for at least MAKER_NAKED_CLOSE_SECS seconds,
        cancel the resting quote on the heavy side and place a taker exit for the
        excess contracts to eliminate directional exposure.
        """
        if not config.MAKER_NAKED_CLOSE_ENABLED:
            return

        now = time.time()
        for market in self._pm.get_markets().values():
            yes_pos = next(
                (p for p in self._risk._positions.values()
                 if not p.is_closed and p.market_id == market.condition_id and p.side == "YES"),
                None,
            )
            no_pos = next(
                (p for p in self._risk._positions.values()
                 if not p.is_closed and p.market_id == market.condition_id and p.side == "NO"),
                None,
            )
            yes_ct = yes_pos.size if yes_pos else 0.0
            no_ct  = no_pos.size  if no_pos  else 0.0
            imbalance = abs(yes_ct - no_ct)

            if imbalance < config.MAKER_NAKED_CLOSE_CONTRACTS:
                self._imbalance_since.pop(market.condition_id, None)
                continue

            # Debounce: record when we first saw this level of imbalance.
            # NOTE: the mid/book check is intentionally NOT here — a stale or missing
            # order book must not prevent the timer from ticking.  We only need a price
            # at the moment the forced-close actually fires (below).
            if market.condition_id not in self._imbalance_since:
                self._imbalance_since[market.condition_id] = now
                log.debug(
                    "Naked-leg imbalance detected — starting debounce timer",
                    market=market.condition_id[:16],
                    yes_ct=round(yes_ct, 1), no_ct=round(no_ct, 1),
                    threshold=config.MAKER_NAKED_CLOSE_CONTRACTS,
                )
                continue

            if now - self._imbalance_since[market.condition_id] < config.MAKER_NAKED_CLOSE_SECS:
                continue  # debounce not yet elapsed

            # Threshold exceeded and debounce elapsed — fire taker exit.
            # Only now do we need a mid price (as a fallback for the exit order).
            mid = self._pm.get_mid(market.token_id_yes)
            heavy_side = "YES" if yes_ct > no_ct else "NO"
            excess_ct = int(imbalance)
            book = self._pm.get_book(market.token_id_yes)

            log.warning(
                "Naked-leg force-close firing",
                market=market.condition_id[:16],
                heavy_side=heavy_side,
                yes_ct=round(yes_ct, 1),
                no_ct=round(no_ct, 1),
                excess_ct=excess_ct,
            )

            try:
                if heavy_side == "YES":
                    # Cancel resting BUY YES, then taker-SELL the excess YES contracts.
                    bid_key = market.token_id_yes
                    q = self._active_quotes.pop(bid_key, None)
                    if q and q.order_id:
                        try:
                            await self._pm.cancel_order(q.order_id)
                        except Exception as exc:
                            log.warning("Naked-leg close: cancel BUY failed",
                                        market=market.condition_id[:16], exc=str(exc))
                    exit_price = (
                        book.best_bid if book and book.best_bid is not None else mid
                    )
                    await self._pm.place_market(
                        market.token_id_yes, "SELL", exit_price, excess_ct, market
                    )
                    # Immediately mark the risk position closed so the webapp reflects
                    # the exit without waiting for the async WS fill event.  PM wallet
                    # is the source of truth; we already placed the taker sell above.
                    self._risk.close_position(
                        market.condition_id, exit_price=exit_price, side="YES"
                    )
                    log.info(
                        "Naked-leg close: YES position force-closed in risk engine",
                        market=market.condition_id[:16],
                        exit_price=round(exit_price, 4),
                        excess_ct=excess_ct,
                    )
                else:
                    # Cancel resting SELL YES (ask), then taker-SELL the excess NO contracts.
                    ask_key = f"{market.token_id_yes}_ask"
                    q = self._active_quotes.pop(ask_key, None)
                    if q and q.order_id:
                        try:
                            await self._pm.cancel_order(q.order_id)
                        except Exception as exc:
                            log.warning("Naked-leg close: cancel SELL failed",
                                        market=market.condition_id[:16], exc=str(exc))
                    yes_ask = (
                        book.best_ask if book and book.best_ask is not None else mid
                    )
                    no_sell_price = 1.0 - yes_ask
                    await self._pm.place_market(
                        market.token_id_no, "SELL", no_sell_price, excess_ct, market
                    )
                    # exit_price for NO position must be in YES-probability space
                    # (same convention as entry_price).  Selling NO at no_sell_price
                    # is equivalent to YES at yes_ask.
                    self._risk.close_position(
                        market.condition_id, exit_price=yes_ask, side="NO"
                    )
                    log.info(
                        "Naked-leg close: NO position force-closed in risk engine",
                        market=market.condition_id[:16],
                        yes_ask=round(yes_ask, 4),
                        excess_ct=excess_ct,
                    )
            except Exception as exc:
                log.error(
                    "Naked-leg force-close failed",
                    market=market.condition_id[:16], exc=str(exc),
                )
                continue  # leave _imbalance_since intact so retry fires next cycle

            del self._imbalance_since[market.condition_id]

    async def _refresh_all_quotes(self) -> None:
        for market in self._pm.get_markets().values():
            await self._reprice_market(market)

    async def _ensure_quoted_all(self) -> None:
        already_bid = set(self._active_quotes.keys())
        # Collect all eligible signals, score them, sort best-first so capital/
        # slot limits are consumed by the highest-quality markets.
        candidates: list[tuple[float, PMMarket]] = []
        for market in self._pm.get_markets().values():
            bid_key = market.token_id_yes
            if bid_key not in already_bid:
                mid = self._pm.get_mid(bid_key)
                if mid is None:
                    continue
                signal = self._evaluate_signal(market, mid)
                if signal is not None:
                    candidates.append((signal.score, market))
                else:
                    # Clean up any stale signal entry for markets that no longer qualify.
                    # _reprice_market normally does this but is never called for markets
                    # without active quotes, so entries would otherwise accumulate forever.
                    self._signals.pop(bid_key, None)
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, market in candidates:
            await self._reprice_market(market)

    def _check_gates(
        self,
        market: PMMarket,
        mid: float,
        tte_secs: float,
        lifecycle_frac: Optional[float],
        quote_size: float,
    ) -> Optional[str]:
        """
        Run all quote-eligibility gates.

        Returns None when all gates pass; returns a short failure-reason string
        (for debug logging by the caller) when any gate rejects the market.

        Gates checked (in order):
          1. TTE floor / opening cooldown (lifecycle fraction for bucket markets,
             absolute hours for milestone markets)
          2. Fee-adjusted edge floor (_is_priority_market)
          3. Minimum incentive spread
          4. Volatility filter
          5. Risk check (can_open) or second-leg combined-cost margin
          6. Near-zero/one mid price
          7. Per-market contracts cap
          8. Scaled 24h volume
        """
        # ── 1. TTE / lifecycle fraction gates ─────────────────────────────────
        # lifecycle_frac is non-None iff the market type has a known duration.
        if lifecycle_frac is not None:
            _exit_frac = (
                config.MAKER_EXIT_TTE_FRAC_5M
                if market.market_type == "bucket_5m" and config.MAKER_EXIT_TTE_FRAC_5M > 0.0
                else config.MAKER_EXIT_TTE_FRAC
            )
            if lifecycle_frac < _exit_frac:
                return "expiry cooldown"
            # Only apply opening cooldown when within the active window (lifecycle_frac ≤ 1.0).
            # lifecycle_frac > 1.0 means the market was created in advance of its event window
            # (Polymarket now pre-publishes bucket markets hours ahead) — let it through.
            if (
                config.MAKER_ENTRY_TTE_FRAC > 0.0
                and lifecycle_frac <= 1.0
                and lifecycle_frac > (1.0 - config.MAKER_ENTRY_TTE_FRAC)
            ):
                log.debug(
                    "Skipping quote — opening cooldown active",
                    market=market.condition_id[:16],
                    lifecycle_frac=round(lifecycle_frac, 3),
                    entry_frac=config.MAKER_ENTRY_TTE_FRAC,
                )
                return "opening cooldown"
        else:
            if tte_secs < config.MAKER_EXIT_HOURS * 3600:
                return "exit hours"

        # ── 2. Fee-adjusted edge floor ─────────────────────────────────────────
        if not _is_priority_market(market, mid):
            return "edge too low"

        # ── 3. Minimum incentive spread ────────────────────────────────────────
        if market.max_incentive_spread < config.MAKER_MIN_INCENTIVE_SPREAD:
            log.debug(
                "Skipping quote — spread below minimum",
                market=market.condition_id[:16],
                spread=round(market.max_incentive_spread, 4),
                min_spread=config.MAKER_MIN_INCENTIVE_SPREAD,
            )
            return "spread below minimum"

        # ── 4. Volatility filter ───────────────────────────────────────────────
        # Adaptive window: use market duration / 4, falling back to 86400 for milestones.
        _market_duration = _MARKET_TYPE_DURATION_SECS.get(market.market_type, 86400)
        _vol_window = min(300.0, _market_duration / 4)
        move = self._get_recent_move_pct(market.underlying, window_s=_vol_window)
        if move is not None and move > config.MAKER_VOL_FILTER_PCT:
            log.debug(
                "Skipping quote — HL moving hard",
                underlying=market.underlying,
                move_5m_pct=round(move * 100, 2),
            )
            return "vol filter"

        # ── 5. Risk / second-leg margin ────────────────────────────────────────
        _market_has_open_position = any(
            not p.is_closed and p.market_id == market.condition_id
            for p in self._risk._positions.values()
        )
        if not _market_has_open_position:
            ok, reason = self._risk.can_open(
                market.condition_id, quote_size,
                strategy="maker", underlying=market.underlying,
            )
            if not ok:
                log.debug("Skipping quote — risk check failed",
                          market=market.condition_id, reason=reason)
                return f"risk: {reason}"
        else:
            _half = market.max_incentive_spread / 2
            _threshold = 1.0 - config.MIN_SPREAD_PROFIT_MARGIN
            for _p in self._risk._positions.values():
                if _p.is_closed or _p.market_id != market.condition_id:
                    continue
                if _p.side == "YES":
                    _combined = _p.entry_price + (1.0 - (mid + _half))
                    if _combined >= _threshold:
                        log.debug(
                            "Skipping NO leg — combined cost would be ≥ threshold",
                            market=market.condition_id[:16],
                            yes_entry=round(_p.entry_price, 4),
                            current_ask=round(mid + _half, 4),
                            combined=round(_combined, 4),
                            threshold=round(_threshold, 4),
                        )
                        return "combined cost NO"
                elif _p.side == "NO":
                    _combined = (1.0 - _p.entry_price) + (mid - _half)
                    if _combined >= _threshold:
                        log.debug(
                            "Skipping YES leg — combined cost would be ≥ threshold",
                            market=market.condition_id[:16],
                            no_entry=round(_p.entry_price, 4),
                            current_bid=round(mid - _half, 4),
                            combined=round(_combined, 4),
                            threshold=round(_threshold, 4),
                        )
                        return "combined cost YES"

        # ── 6. Near-zero/one mid price ─────────────────────────────────────────
        if mid < config.MAKER_MIN_QUOTE_PRICE or mid > (1.0 - config.MAKER_MIN_QUOTE_PRICE):
            log.debug("Skipping quote — near-zero/one mid",
                      market=market.condition_id, mid=round(mid, 4))
            return "mid near boundary"

        # ── 7. Per-market contracts cap ────────────────────────────────────────
        _yes_pos_cap = next(
            (p for p in self._risk._positions.values()
             if not p.is_closed and p.market_id == market.condition_id and p.side == "YES"),
            None,
        )
        _no_pos_cap = next(
            (p for p in self._risk._positions.values()
             if not p.is_closed and p.market_id == market.condition_id and p.side == "NO"),
            None,
        )
        _yes_ct_cap = _yes_pos_cap.size if _yes_pos_cap else 0.0
        _no_ct_cap  = _no_pos_cap.size  if _no_pos_cap  else 0.0
        if _yes_ct_cap + _no_ct_cap >= config.MAKER_MAX_CONTRACTS_PER_MARKET:
            log.debug(
                "Skipping quote — per-market contracts cap reached",
                market=market.condition_id[:16],
                yes_ct=round(_yes_ct_cap, 1),
                no_ct=round(_no_ct_cap, 1),
                cap=config.MAKER_MAX_CONTRACTS_PER_MARKET,
            )
            return "contracts cap"

        # ── 8. Scaled 24h volume ───────────────────────────────────────────────
        duration = _MARKET_TYPE_DURATION_SECS.get(market.market_type)
        if duration is not None:
            fraction_elapsed = max(0.0, min(1.0, 1.0 - tte_secs / duration))
            required_volume = config.MAKER_MIN_VOLUME_24HR * fraction_elapsed
        else:
            fraction_elapsed = 1.0
            required_volume = config.MAKER_MIN_VOLUME_24HR

        if market.volume_24hr < required_volume:
            log.debug("Skipping quote — low 24h volume",
                      market=market.condition_id,
                      volume=round(market.volume_24hr, 0),
                      required=round(required_volume, 0),
                      fraction_elapsed=round(fraction_elapsed, 3))
            return "low volume"

        return None  # all gates passed

    @staticmethod
    def _depth_at_level(book, side: str, price: float) -> float:
        """Sum competing contracts at *price* on *side* (within half a tick).

        Mirrors fill_simulator._depth_at_level; used for depth gate and
        depth-aware spread widening.
        """
        if side == "BUY":
            return sum(s for (p, s) in book.bids if abs(p - price) <= _CLOB_HALF_TICK)
        else:
            return sum(s for (p, s) in book.asks if abs(p - price) <= _CLOB_HALF_TICK)

    @staticmethod
    def _depth_spread_factor(depth: float) -> float:
        """Spread widening multiplier based on CLOB depth at our quote level.

        depth >= MAKER_DEPTH_THIN_THRESHOLD : 1.0  (normal book, no widening)
        0 < depth < MAKER_DEPTH_THIN_THRESHOLD : linear interpolation
                                                  FACTOR_THIN … 1.0
        depth == 0                            : MAKER_DEPTH_SPREAD_FACTOR_ZERO
        """
        threshold = config.MAKER_DEPTH_THIN_THRESHOLD
        factor_thin = config.MAKER_DEPTH_SPREAD_FACTOR_THIN
        factor_zero = config.MAKER_DEPTH_SPREAD_FACTOR_ZERO

        if depth >= threshold:
            return 1.0
        if depth <= 0:
            return factor_zero
        # Linear interpolation: depth=threshold→1.0, depth=0→factor_thin
        t = depth / threshold  # 0 at depth=0, 1 at depth=threshold
        return factor_thin + t * (1.0 - factor_thin)

    def _evaluate_signal(self, market: PMMarket, mid: float) -> Optional[MakerSignal]:
        """
        Evaluate whether a market qualifies for quoting.
        Returns a MakerSignal with pre-computed bid/ask prices if valid, None otherwise.
        Capital-free — no orders are posted here.
        """
        if market.end_date is None:
            return None
        # Market-type exclusion gate (cheapest check — before any computation)
        if config.MAKER_EXCLUDED_MARKET_TYPES and market.market_type in config.MAKER_EXCLUDED_MARKET_TYPES:
            log.debug(
                "Skipping quote — market type excluded",
                market=market.condition_id[:16],
                market_type=market.market_type,
            )
            return None
        _now_ts = time.time()
        _tte_secs = market.end_date.timestamp() - _now_ts
        if _tte_secs > config.MAKER_MAX_TTE_DAYS * 86_400:
            return None

        _market_dur = _MARKET_TYPE_DURATION_SECS.get(market.market_type)
        _lifecycle_frac = _tte_secs / _market_dur if _market_dur is not None else None

        quote_size = self._compute_spread_size(market)

        if self._check_gates(market, mid, _tte_secs, _lifecycle_frac, quote_size) is not None:
            return None

        # ── Pricing ────────────────────────────────────────────────────────────
        market_age = _now_ts - market.discovered_at
        book = self._pm.get_book(market.token_id_yes)

        if market_age < config.NEW_MARKET_AGE_LIMIT:
            half_spread = config.NEW_MARKET_WIDE_SPREAD / 2
            if book and book.best_bid is not None and book.best_ask is not None:
                existing_spread = book.best_ask - book.best_bid
                if existing_spread < config.NEW_MARKET_PULL_SPREAD:
                    half_spread = market.max_incentive_spread / 2
        else:
            half_spread = market.max_incentive_spread / 2

        # ── Depth gate + depth-aware spread widening ───────────────────────────
        depth_bid = self._depth_at_level(book, "BUY",  mid - half_spread) if book else 0.0
        depth_ask = self._depth_at_level(book, "SELL", mid + half_spread) if book else 0.0
        depth = min(depth_bid, depth_ask)

        if config.MAKER_MIN_DEPTH_TO_QUOTE > 0 and depth < config.MAKER_MIN_DEPTH_TO_QUOTE:
            log.debug(
                "Skipping quote — CLOB depth below minimum",
                market=market.condition_id[:16],
                depth=depth,
                threshold=config.MAKER_MIN_DEPTH_TO_QUOTE,
            )
            return None

        spread_factor = self._depth_spread_factor(depth)
        if spread_factor > 1.0:
            half_spread = min(half_spread * spread_factor, config.NEW_MARKET_WIDE_SPREAD / 2)
            log.debug(
                "Depth-aware spread widening applied",
                market=market.condition_id[:16],
                depth=depth,
                factor=round(spread_factor, 3),
                half_spread=round(half_spread, 4),
            )

        # ── Inventory skew ─────────────────────────────────────────────────────
        net_inv = self._inventory.get(market.underlying, 0.0)
        raw_skew = net_inv * config.INVENTORY_SKEW_COEFF
        skew = max(-config.INVENTORY_SKEW_MAX, min(config.INVENTORY_SKEW_MAX, raw_skew))
        skewed_mid = mid - skew
        if skew != 0.0:
            log.debug("Inventory skew applied", market=market.condition_id[:16],
                      underlying=market.underlying, net_inv=round(net_inv, 2),
                      skew=round(skew, 5))

        bid_price = max(config.MAKER_MIN_QUOTE_PRICE, skewed_mid - half_spread)
        ask_price = min(1.0 - config.MAKER_MIN_QUOTE_PRICE, skewed_mid + half_spread)

        tick = market.tick_size
        bid_price = self._pm._round_to_tick(bid_price, tick)
        ask_price = self._pm._round_to_tick(ask_price, tick)

        # Per-market asymmetric imbalance skew: tighten the LAGGING leg's price
        # toward mid so takers fill it faster, restoring YES/NO contract balance.
        # Unlike the coin-level symmetric skew above, only the under-filled side
        # is adjusted — the over-filled side stays at fair value.
        #   YES-heavy (yes_ct > no_ct): lower the ask to attract more NO fills
        #   NO-heavy  (no_ct > yes_ct): raise  the bid to attract more YES fills
        if config.MAKER_IMBALANCE_SKEW_COEFF > 0.0:
            _yes_pos = next(
                (p for p in self._risk._positions.values()
                 if not p.is_closed and p.market_id == market.condition_id and p.side == "YES"),
                None,
            )
            _no_pos = next(
                (p for p in self._risk._positions.values()
                 if not p.is_closed and p.market_id == market.condition_id and p.side == "NO"),
                None,
            )
            _yes_ct = _yes_pos.size if _yes_pos else 0.0
            _no_ct  = _no_pos.size  if _no_pos  else 0.0
            _mkt_imbalance = _yes_ct - _no_ct  # positive = YES-heavy

            if abs(_mkt_imbalance) >= config.MAKER_IMBALANCE_SKEW_MIN_CT:
                _raw_imb_adj = abs(_mkt_imbalance) * config.MAKER_IMBALANCE_SKEW_COEFF
                _imb_adj = min(_raw_imb_adj, config.MAKER_IMBALANCE_SKEW_MAX)
                if _mkt_imbalance > 0:
                    # YES-heavy: lower ask to attract buyers (generate more NO fills)
                    ask_price = max(bid_price + tick,
                                    self._pm._round_to_tick(ask_price - _imb_adj, tick))
                else:
                    # NO-heavy: raise bid to attract sellers (generate more YES fills)
                    bid_price = min(ask_price - tick,
                                    self._pm._round_to_tick(bid_price + _imb_adj, tick))
                log.debug(
                    "Per-market imbalance skew applied",
                    market=market.condition_id[:16],
                    yes_ct=round(_yes_ct, 1), no_ct=round(_no_ct, 1),
                    imbalance=round(_mkt_imbalance, 1),
                    adj=round(_imb_adj, 4),
                    bid=round(bid_price, 4), ask=round(ask_price, 4),
                )

        taker_fee_at_mid = config.PM_FEE_COEFF * mid * (1.0 - mid)
        effective_edge = half_spread + market.rebate_pct * taker_fee_at_mid

        signal = MakerSignal(
            market_id=market.condition_id,
            token_id=market.token_id_yes,
            underlying=market.underlying,
            mid=mid,
            bid_price=bid_price,
            ask_price=ask_price,
            half_spread=half_spread,
            effective_edge=effective_edge,
            market_type=market.market_type,
            quote_size=quote_size,
            depth=depth,
        )
        signal.score = score_maker(signal, market.volume_24hr, _tte_secs, market.market_type)
        if signal.score < config.MIN_SIGNAL_SCORE_MAKER:
            log.debug(
                "Skipping quote — signal below score threshold",
                market=market.condition_id,
                score=signal.score,
                min_score=config.MIN_SIGNAL_SCORE_MAKER,
            )
            return None
        # Per-bucket-type score overrides
        if market.market_type == "bucket_5m" and config.MAKER_MIN_SIGNAL_SCORE_5M > 0.0:
            if signal.score < config.MAKER_MIN_SIGNAL_SCORE_5M:
                log.debug(
                    "Skipping quote — bucket_5m score below per-type threshold",
                    market=market.condition_id,
                    score=signal.score,
                    min_score_5m=config.MAKER_MIN_SIGNAL_SCORE_5M,
                )
                return None
        if market.market_type == "bucket_1h" and config.MAKER_MIN_SIGNAL_SCORE_1H > 0.0:
            if signal.score < config.MAKER_MIN_SIGNAL_SCORE_1H:
                log.debug(
                    "Skipping quote — bucket_1h score below per-type threshold",
                    market=market.condition_id,
                    score=signal.score,
                    min_score_1h=config.MAKER_MIN_SIGNAL_SCORE_1H,
                )
                return None
        if market.market_type == "bucket_4h" and config.MAKER_MIN_SIGNAL_SCORE_4H > 0.0:
            if signal.score < config.MAKER_MIN_SIGNAL_SCORE_4H:
                log.debug(
                    "Skipping quote — bucket_4h score below per-type threshold",
                    market=market.condition_id,
                    score=signal.score,
                    min_score_4h=config.MAKER_MIN_SIGNAL_SCORE_4H,
                )
                return None
        return signal

    async def _deploy_quote(self, signal: MakerSignal, market: PMMarket) -> None:
        # Enforce the concurrent-position cap at ORDER PLACEMENT time.
        # This prevents many simultaneous fills from all passing the can_open()
        # check before any position lands in the risk engine.
        if not self._risk.reserve_slot(signal.market_id, "maker", market.underlying):
            log.debug(
                "Deploy skipped — concurrent position cap reached",
                market_id=signal.market_id,
                underlying=market.underlying,
            )
            return

        bid_key = signal.token_id
        ask_key = f"{signal.token_id}_ask"

        # Per-spread contract sizing:
        #   contracts = floor(spread_budget / (bid_price + (1 - ask_price)))
        # Both sides get the SAME contract count so fills are always symmetric.
        # The full budget is utilised up to rounding (at most 1 contract short).
        cost_per_contract = signal.bid_price + (1.0 - signal.ask_price)
        contracts = min(
            config.MAKER_BATCH_SIZE,          # batch cap: limits per-order exposure
            config.MAKER_MAX_CONTRACTS_PER_SIDE,
            max(1, int(signal.quote_size / cost_per_contract)),
        )

        # Imbalance guard: if existing open positions are already tilted heavily
        # toward one side, skip re-posting the overweight leg until fills rebalance.
        yes_pos = next(
            (p for p in self._risk._positions.values()
             if not p.is_closed and p.market_id == signal.market_id and p.side == "YES"),
            None,
        )
        no_pos = next(
            (p for p in self._risk._positions.values()
             if not p.is_closed and p.market_id == signal.market_id and p.side == "NO"),
            None,
        )
        yes_contracts = yes_pos.size if yes_pos else 0.0
        no_contracts  = no_pos.size  if no_pos  else 0.0
        imbalance = yes_contracts - no_contracts  # positive = YES-heavy, negative = NO-heavy

        # Imbalance-aware sizing: reduce the heavy side's new order so that if
        # BOTH legs fill fully, the total open positions are exactly balanced.
        #
        #   yes_size = contracts - max(0, imbalance)   → shrinks when YES is ahead
        #   no_size  = contracts - max(0, -imbalance)  → shrinks when NO is ahead
        #
        # Example: imbalance = +26 (YES has 26 more than NO), contracts = 50:
        #   yes_size = 50 - 26 = 24  →  YES total if filled: 26 + 24 = 50
        #   no_size  = 50 - 0  = 50  →  NO  total if filled:  0 + 50 = 50  ✓ balanced
        #
        # Hard stop: if one side is so far ahead that no_size / yes_size rounds to 0,
        # the MAKER_MAX_IMBALANCE_CONTRACTS guard blocks posting entirely.
        yes_size = max(1, contracts - max(0, int(imbalance)))
        no_size  = max(1, contracts - max(0, int(-imbalance)))

        post_bid = imbalance < config.MAKER_MAX_IMBALANCE_CONTRACTS    # hard stop only
        post_ask = imbalance > -config.MAKER_MAX_IMBALANCE_CONTRACTS   # hard stop only

        if not post_bid:
            log.debug("Imbalance guard: skipping BUY YES (YES already heavy)",
                      market=signal.market_id[:16], yes=yes_contracts, no=no_contracts)
        if not post_ask:
            log.debug("Imbalance guard: skipping SELL YES (NO already heavy)",
                      market=signal.market_id[:16], yes=yes_contracts, no=no_contracts)

        # Fill-count gate: once a leg has been filled MAKER_MAX_FILLS_PER_LEG times,
        # stop re-posting it. Guards against both Factor A (single-side trap where one
        # leg fills repeatedly and the other never fires) and Factor B (high fill count
        # adverse selection on a slowly-moving book).
        if config.MAKER_MAX_FILLS_PER_LEG > 0:
            _yes_fills = self._leg_fill_counts.get((signal.market_id, "YES"), 0)
            _no_fills  = self._leg_fill_counts.get((signal.market_id, "NO"), 0)
            if _yes_fills >= config.MAKER_MAX_FILLS_PER_LEG:
                post_bid = False
                log.debug(
                    "Fill count gate: skipping YES leg",
                    market=signal.market_id[:16],
                    yes_fills=_yes_fills,
                    limit=config.MAKER_MAX_FILLS_PER_LEG,
                )
            if _no_fills >= config.MAKER_MAX_FILLS_PER_LEG:
                post_ask = False
                log.debug(
                    "Fill count gate: skipping NO leg",
                    market=signal.market_id[:16],
                    no_fills=_no_fills,
                    limit=config.MAKER_MAX_FILLS_PER_LEG,
                )
        if post_bid or post_ask:
            log.debug("Imbalance-adjusted sizes",
                      market=signal.market_id[:16],
                      imbalance=round(imbalance, 1),
                      base_contracts=contracts,
                      yes_size=yes_size, no_size=no_size)

        if post_bid:
            bid_id = await self._pm.place_limit(
                signal.token_id, "BUY", signal.bid_price, yes_size, market
            )
            if bid_id:
                self._active_quotes[bid_key] = ActiveQuote(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    side="BUY",
                    price=signal.bid_price,
                    size=yes_size,
                    order_id=bid_id,
                    collateral_usd=round(signal.bid_price * yes_size, 4),
                    original_size=yes_size,
                    score=signal.score,
                )

        if post_ask:
            # BUY NO at (1 - ask_price) is economically equivalent to SELL YES at
            # ask_price but requires only USDC — no YES-token inventory needed.
            #
            # Post-only safety: the NO CLOB is a separate orderbook from the YES CLOB.
            # Its best_ask does NOT always equal (1 - YES_best_bid). If our price
            # reaches or exceeds the actual NO best_ask the CLOB rejects the order
            # with "invalid post-only order: order crosses book", leaving ask_key
            # absent from _active_quotes, which disables the price-idempotency guard
            # and causes an infinite reprice loop driven by book callbacks.
            # Fix: clamp to one tick below the real NO best_ask before placing.
            no_buy_price = 1.0 - signal.ask_price
            no_book = self._pm.get_book(market.token_id_no)
            if no_book is not None and no_book.best_ask is not None:
                tick = market.tick_size
                no_crossing_ceiling = no_book.best_ask - tick
                if no_buy_price >= no_book.best_ask:
                    no_buy_price = self._pm._round_to_tick(no_crossing_ceiling, tick)
                    log.debug(
                        "NO buy price clamped below best_ask to avoid crossing",
                        market=market.condition_id[:16],
                        original=round(1.0 - signal.ask_price, 4),
                        clamped=round(no_buy_price, 4),
                        no_best_ask=round(no_book.best_ask, 4),
                    )
                    if no_buy_price < config.MAKER_MIN_QUOTE_PRICE:
                        log.debug(
                            "NO ask leg skipped — clamped price below min quote price",
                            market=market.condition_id[:16],
                        )
                        post_ask = False
            if post_ask:
                ask_id = await self._pm.place_limit(
                    market.token_id_no, "BUY", no_buy_price, no_size, market
                )
                if ask_id:
                    self._active_quotes[ask_key] = ActiveQuote(
                        market_id=signal.market_id,
                        token_id=market.token_id_no,
                        side="BUY",
                        price=no_buy_price,
                        size=no_size,
                        order_id=ask_id,
                        collateral_usd=round(no_buy_price * no_size, 4),
                        original_size=no_size,
                        score=signal.score,
                    )

        self._last_hl_mids.setdefault(signal.underlying, 0.0)

    async def _reprice_market(self, market: PMMarket) -> None:
        mid = self._pm.get_mid(market.token_id_yes)
        if mid is None:
            return

        # Book age gate: skip repricing when the order book is stale
        if config.MAKER_MAX_BOOK_AGE_SECS > 0:
            book = self._pm.get_book(market.token_id_yes)
            if book is not None:
                age_s = time.time() - book.timestamp
                if age_s > config.MAKER_MAX_BOOK_AGE_SECS:
                    log.debug(
                        "Reprice skipped — book too stale",
                        market=market.condition_id[:16],
                        book_age_s=round(age_s, 1),
                        max_age=config.MAKER_MAX_BOOK_AGE_SECS,
                    )
                    return

        bid_key = market.token_id_yes
        ask_key = f"{market.token_id_yes}_ask"

        # Do NOT cancel/reprice a quote while it is being partially filled, unless:
        # (a) it has gone stale (age > MAX_QUOTE_AGE_SECONDS), OR
        # (b) the market mid has drifted more than MAKER_ADVERSE_DRIFT_REPRICE from
        #     the quote price — this indicates a fast HL move that makes the
        #     remaining resting size adversely selected even if still "fresh".
        for key in [bid_key, ask_key]:
            existing = self._active_quotes.get(key)
            if existing and existing.original_size > 0 and existing.size < existing.original_size:
                age = time.time() - existing.posted_at
                drift = abs(mid - existing.price)
                still_fresh = age < config.MAX_QUOTE_AGE_SECONDS
                low_drift   = drift <= config.MAKER_ADVERSE_DRIFT_REPRICE
                if still_fresh and low_drift:
                    log.debug(
                        "Reprice skipped — quote partially filled and still fresh",
                        key=key[:24],
                        original=existing.original_size,
                        remaining=existing.size,
                        age_s=round(age, 1),
                        drift=round(drift, 4),
                    )
                    return
                reason = "stale" if not still_fresh else "price drift"
                log.debug(
                    "Repricing partial-fill quote",
                    key=key[:24],
                    original=existing.original_size,
                    remaining=existing.size,
                    age_s=round(age, 1),
                    drift=round(drift, 4),
                    reason=reason,
                )
                # Fall through — cancel and re-post at the current market price

        # ── Price-idempotency guard ────────────────────────────────────────────
        # If both legs are resting (unfilled) and the new signal would produce
        # the same prices, skip the cancel/repost entirely.  Without this, our
        # own order entering a thin book can shift the mid enough to exceed the
        # drift threshold in _on_pm_price_change, causing an infinite
        # post → price_change → reprice → post loop at identical prices.
        _bid_q = self._active_quotes.get(bid_key)
        _ask_q = self._active_quotes.get(ask_key)
        if (
            _bid_q is not None
            and _ask_q is not None
            and _bid_q.size >= _bid_q.original_size   # no partial fill
            and _ask_q.size >= _ask_q.original_size
        ):
            _draft = self._evaluate_signal(market, mid)
            if _draft is not None:
                _same_bid = abs(_draft.bid_price - _bid_q.price) < 0.005
                _same_ask = abs((1.0 - _draft.ask_price) - _ask_q.price) < 0.005
                if _same_bid and _same_ask:
                    log.debug(
                        "Reprice skipped — prices unchanged",
                        market=market.condition_id[:16],
                        bid=round(_bid_q.price, 4),
                        ask=round(_ask_q.price, 4),
                    )
                    return

        # Partial-idempotency: bid resting but ask absent (prior placement failed).
        # If the new signal would produce the same bid price, skip the cancel/repost
        # of the bid leg — only the ask leg needs a fresh placement attempt.
        if (
            _bid_q is not None
            and _ask_q is None
            and _bid_q.size >= _bid_q.original_size
        ):
            _draft = self._evaluate_signal(market, mid)
            if _draft is not None and abs(_draft.bid_price - _bid_q.price) < 0.005:
                # Same bid — only need to re-attempt the ask leg.
                signal = _draft
                self._signals[bid_key] = signal
                if config.MAKER_DEPLOYMENT_MODE == "auto":
                    _cost_per = signal.bid_price + (1.0 - signal.ask_price)
                    _c = min(config.MAKER_BATCH_SIZE, max(1, int(signal.quote_size / _cost_per)))
                    needed = round(_cost_per * _c, 4)
                    if self.available_capital >= needed:
                        # Free + re-deploy only the ask; the bid stays in place.
                        self._risk.free_slot(market.condition_id)
                        await self._deploy_quote(signal, market)
                log.debug(
                    "Partial-reprice: bid unchanged, re-attempting ask leg only",
                    market=market.condition_id[:16],
                    bid=round(_bid_q.price, 4),
                )
                return

        for key in [bid_key, ask_key]:
            old = self._active_quotes.get(key)
            if old and old.order_id:
                await self._pm.cancel_order(old.order_id)
        self._active_quotes.pop(bid_key, None)
        self._active_quotes.pop(ask_key, None)
        # Free the slot so (a) reprices are not counted as new deployments and
        # (b) if we decide not to re-deploy the slot doesn't stay zombie-reserved.
        # _deploy_quote will re-reserve it if the signal is still valid.
        self._risk.free_slot(market.condition_id)

        signal = self._evaluate_signal(market, mid)
        if signal is None:
            self._signals.pop(bid_key, None)
            return

        self._signals[bid_key] = signal

        if config.MAKER_DEPLOYMENT_MODE == "auto":
            # Spread budget: contracts = floor(budget / cost_per_contract), both sides.
            _cost_per = signal.bid_price + (1.0 - signal.ask_price)
            _c = min(config.MAKER_BATCH_SIZE, max(1, int(signal.quote_size / _cost_per)))
            needed = round(_cost_per * _c, 4)
            if self.available_capital >= needed:
                await self._deploy_quote(signal, market)
            else:
                log.debug("Auto-deploy skipped — insufficient capital",
                          market=signal.market_id,
                          needed=round(needed, 2),
                          available=round(self.available_capital, 2))

    async def _reprice_underlying(self, coin: str) -> None:
        for market in self._pm.get_markets().values():
            if market.underlying == coin:
                await self._reprice_market(market)

    # ── Delta hedge ────────────────────────────────────────────────────────────

    def _position_delta_usd(self, coin: str) -> float:
        net = 0.0
        for pos in self._risk._positions.values():
            if pos.is_closed or pos.underlying != coin:
                continue
            sign = 1.0 if pos.side == "YES" else -1.0
            net += sign * pos.entry_cost_usd
        return net

    def _position_delta_coins(self, coin: str, hl_mid: float) -> float:
        """Signed HL coins required to delta-hedge all open positions on *coin*.

        Uses the BS digital-call delta (n(d2) / (S σ √T)) whenever a parseable
        market strike + expiry are available.  Falls back to the naive
        *entry_cost_usd / hl_mid* (delta=1 assumption) when they are not.

        When no open positions exist for this coin the method delegates to
        _position_delta_usd / hl_mid, preserving mock semantics in unit tests.
        """
        if hl_mid <= 0:
            return 0.0
        now_ts = time.time()
        net_coins = 0.0
        had_positions = False
        for pos in self._risk._positions.values():
            if pos.is_closed or pos.underlying != coin:
                continue
            had_positions = True
            sign = 1.0 if pos.side == "YES" else -1.0
            market = self._find_market_by_id(pos.market_id)
            if market is not None and market.end_date is not None:
                tte_years = max(0.0, (market.end_date.timestamp() - now_ts) / 31_536_000)
                strike = parse_strike_from_title(market.title)
                if strike is not None and tte_years > 1e-6:
                    sigma = implied_sigma(pos.entry_price, hl_mid, strike, tte_years)
                    if sigma is not None:
                        coins = bs_digital_coins(
                            pos.entry_cost_usd, pos.entry_price, hl_mid, strike, tte_years, sigma
                        )
                        net_coins += sign * coins
                        continue
            # Fallback: naive linear (same as prior formula)
            net_coins += sign * pos.entry_cost_usd / hl_mid
        if not had_positions:
            # No open positions — use USD aggregate so unit-test mocks flow through.
            return self._position_delta_usd(coin) / hl_mid
        return net_coins

    # ── Hedge debounce / cooldown ──────────────────────────────────────────────

    def _schedule_hedge_rebalance(self, coin: str) -> None:
        """Production entry point for hedge rebalancing.

        Coalesces multiple rapid fill/BBO events into a single HL order:
          1. Cancels any in-flight debounce task for this coin.
          2. Waits HEDGE_DEBOUNCE_SECS for additional events to settle.
          3. Enforces HEDGE_MIN_INTERVAL cooldown since the last executed hedge.
          4. Calls _rebalance_hedge() which contains the actual hedge logic.

        Tests should call _rebalance_hedge() directly (no debounce, no cooldown).
        """
        old = self._pending_hedge_tasks.pop(coin, None)
        if old and not old.done():
            try:
                old.cancel()
            except RuntimeError:
                pass  # task's loop may be closed (e.g. per-call _run test helpers)
        self._pending_hedge_tasks[coin] = asyncio.create_task(
            self._debounced_hedge_task(coin)
        )

    async def _debounced_hedge_task(self, coin: str) -> None:
        """Wait for fill burst to settle, respect cooldown, then execute one hedge."""
        try:
            if config.HEDGE_DEBOUNCE_SECS > 0:
                await asyncio.sleep(config.HEDGE_DEBOUNCE_SECS)
            self._pending_hedge_tasks.pop(coin, None)

            now = time.time()
            last = self._last_hedge_ts.get(coin, 0.0)
            remaining_cooldown = config.HEDGE_MIN_INTERVAL - (now - last)
            if remaining_cooldown > 0:
                log.debug(
                    "Hedge cooldown active — waiting",
                    coin=coin,
                    remaining_s=round(remaining_cooldown, 1),
                )
                await asyncio.sleep(remaining_cooldown)

            await self._rebalance_hedge(coin)
        except asyncio.CancelledError:
            pass  # superseded by a newer _schedule_hedge_rebalance call — expected
        except Exception as exc:
            log.warning("_debounced_hedge_task failed", coin=coin, exc=str(exc))

    async def _rebalance_hedge(self, coin: str) -> None:
        if not config.MAKER_HEDGE_ENABLED:
            log.debug("Hedge skipped — MAKER_HEDGE_ENABLED is False", coin=coin)
            return
        net_delta = self._position_delta_usd(coin)
        log.info("Hedge check", coin=coin, net_capital_usd=round(net_delta, 2),
                 threshold=config.HEDGE_THRESHOLD_USD)

        if abs(net_delta) < config.HEDGE_THRESHOLD_USD:
            if coin in self._coin_hedges:
                existing = self._coin_hedges[coin]
                close_mid = self._hl.get_mid(coin) or existing["price"]
                await self._hl.close_hedge(coin, existing["direction"], existing["size"])
                self._risk.record_hl_hedge_trade(
                    coin=coin,
                    direction=existing["direction"],
                    open_price=existing["price"],
                    close_price=close_mid,
                    size_coins=existing["size"],
                )
                del self._coin_hedges[coin]
                self._risk.update_coin_hedge(coin, 0.0)
                log.info("Hedge closed — delta below threshold", coin=coin)
            return

        hl_mid = self._hl.get_mid(coin)
        if hl_mid is None:
            log.warning("No HL mid — cannot hedge", coin=coin)
            return

        direction = "SHORT" if net_delta > 0 else "LONG"
        # Proper digital-call delta when strike/expiry are parseable; naive linear fallback.
        coins_to_hedge = abs(self._position_delta_coins(coin, hl_mid))
        target_notional = coins_to_hedge * hl_mid

        max_coins = config.MAX_HL_NOTIONAL / hl_mid
        if coins_to_hedge > max_coins:
            coins_to_hedge = max_coins
            target_notional = coins_to_hedge * hl_mid
            log.warning("Hedge capped at MAX_HL_NOTIONAL", coin=coin,
                        capped_notional=round(target_notional, 2))

        ok, reason = self._risk.can_hedge(target_notional)
        if not ok:
            log.warning("Hedge blocked by risk limit", reason=reason, coin=coin)
            return

        existing = self._coin_hedges.get(coin)
        if existing is not None:
            current_notional = existing["size"] * hl_mid
            delta_pct = abs(target_notional - current_notional) / current_notional if current_notional > 0 else 1.0
            if existing["direction"] == direction and delta_pct < config.HEDGE_REBALANCE_PCT:
                log.debug("Hedge skip — change below rebalance threshold",
                          coin=coin, delta_pct=round(delta_pct * 100, 1))
                return
            await self._hl.close_hedge(coin, existing["direction"], existing["size"])
            self._risk.record_hl_hedge_trade(
                coin=coin,
                direction=existing["direction"],
                open_price=existing["price"],
                close_price=hl_mid,
                size_coins=existing["size"],
            )

        resp = await self._hl.place_hedge(coin, direction, coins_to_hedge)
        if resp:
            self._coin_hedges[coin] = {
                "size": coins_to_hedge,
                "price": hl_mid,
                "direction": direction,
            }
            self._risk.update_coin_hedge(coin, target_notional)
            # ── Record last-hedge timestamp (feeds cooldown in _debounced_hedge_task) ──
            self._last_hedge_ts[coin] = time.time()
            # ── Execution quality record ───────────────────────────────────────
            bbo = self._hl.get_bbo(coin)
            bbo_spread = round(bbo.spread, 4) if bbo and bbo.spread is not None else None
            # Estimate the worst-case execution price: asks for LONG, bids for SHORT
            exec_est = hl_mid
            if bbo:
                exec_est = (bbo.ask or hl_mid) if direction == "LONG" else (bbo.bid or hl_mid)
            # Slippage = cost of market order vs mid (always positive for taker)
            slippage_pct = round(
                abs(exec_est - hl_mid) / hl_mid * 100, 4
            ) if hl_mid else 0.0
            quality_entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "coin": coin,
                "direction": direction,
                "size_coins": round(coins_to_hedge, 6),
                "decision_mid": round(hl_mid, 4),
                "exec_price_est": round(exec_est, 4),
                "slippage_pct": slippage_pct,
                "bbo_spread": bbo_spread,
                "notional_usd": round(target_notional, 2),
            }
            self._hedge_quality.appendleft(quality_entry)
            # ── Elevated slippage watchdog ────────────────────────────────────
            if len(self._hedge_quality) >= 20:
                _recent = list(self._hedge_quality)[:20]
                _avg_slip = sum(e["slippage_pct"] for e in _recent) / 20
                if _avg_slip > config.HEDGE_SLIPPAGE_ALERT_PCT:
                    log.warning(
                        "Elevated hedge slippage — consider widening spread or reducing hedge frequency",
                        coin=coin,
                        avg_slippage_pct=round(_avg_slip, 4),
                        threshold=config.HEDGE_SLIPPAGE_ALERT_PCT,
                        window=20,
                    )
            log.info(
                "Hedge placed",
                coin=coin, direction=direction,
                size=round(coins_to_hedge, 6), notional=round(target_notional, 2),
                slippage_pct=slippage_pct, bbo_spread=bbo_spread,
            )

    # ── Quote sizing ───────────────────────────────────────────────────────────

    def _compute_spread_size(self, market: PMMarket) -> float:
        """Return the total USD budget for a single spread (both legs combined)."""
        if self._quote_size_override is not None:
            return self._quote_size_override
        try:
            vol = float(market.volume_24hr or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol > 0.0:
            size = vol * config.MAKER_SPREAD_SIZE_PCT
        else:
            size = config.MAKER_SPREAD_SIZE_NEW_MARKET
        # Also cap at per-market exposure limit so risk.can_open() never blocks a fresh position
        return max(config.MAKER_SPREAD_SIZE_MIN, min(
            config.MAKER_SPREAD_SIZE_MAX,
            config.MAX_PM_EXPOSURE_PER_MARKET,
            round(size),
        ))

    # ── Capital accounting ─────────────────────────────────────────────────────

    @property
    def deployed_capital(self) -> float:
        return sum(q.collateral_usd for q in self._active_quotes.values())

    @property
    def available_capital(self) -> float:
        in_positions = self._risk.get_state().get("total_pm_capital_deployed", 0.0)
        realized = self._risk.realized_pnl
        return max(0.0, config.PAPER_CAPITAL_USD + realized - self.deployed_capital - in_positions)

    # ── Signal management ──────────────────────────────────────────────────────

    def get_signals(self) -> dict[str, dict]:
        """Return a snapshot of all evaluated signals (deployed and undeployed)."""
        result = {}
        for k, s in self._signals.items():
            is_deployed = k in self._active_quotes
            bid_q = self._active_quotes.get(k)
            ask_q = self._active_quotes.get(f"{k}_ask")

            if is_deployed:
                collateral = (bid_q.collateral_usd if bid_q else 0.0) + \
                             (ask_q.collateral_usd if ask_q else 0.0)
            else:
                collateral = 0.0

            # ── Partial-fill tracking ──────────────────────────────────────
            # original_size = full order quantity at deployment
            # remaining_size = what is still resting in the order book
            # filled_size    = how much has been filled so far this deployment
            # fill_pct       = 0.0 → 1.0 fill progress
            bid_orig = (bid_q.original_size or s.quote_size) if bid_q else s.quote_size
            bid_rem  = bid_q.size if bid_q else 0.0
            bid_fill = round(bid_orig - bid_rem, 4) if is_deployed else 0.0

            ask_orig = (ask_q.original_size or s.quote_size) if ask_q else s.quote_size
            ask_rem  = ask_q.size if ask_q else 0.0
            ask_fill = round(ask_orig - ask_rem, 4) if is_deployed else 0.0

            # Aggregate across both sides (for the UI summary row)
            total_orig = bid_orig + ask_orig if is_deployed else s.quote_size * 2
            total_rem  = bid_rem + ask_rem
            total_fill = bid_fill + ask_fill
            fill_pct   = round(total_fill / total_orig, 4) if total_orig > 0 else 0.0

            result[k] = {
                "market_id": s.market_id,
                "token_id": s.token_id,
                "underlying": s.underlying,
                "mid": s.mid,
                "bid_price": s.bid_price,
                "ask_price": s.ask_price,
                "half_spread": s.half_spread,
                "effective_edge": s.effective_edge,
                "market_type": s.market_type,
                "quote_size": s.quote_size,
                "ts": s.ts,
                "score": s.score,
                "is_deployed": is_deployed,
                "collateral_usd": round(collateral, 2),
                # Partial-fill state
                "bid_original_size": bid_orig,
                "bid_remaining_size": bid_rem,
                "bid_filled_size":    bid_fill,
                "ask_original_size": ask_orig,
                "ask_remaining_size": ask_rem,
                "ask_filled_size":    ask_fill,
                "total_original_size": total_orig,
                "total_remaining_size": round(total_rem, 4),
                "total_filled_size":   round(total_fill, 4),
                "fill_pct":            fill_pct,
            }
        return result

    async def deploy_signal(self, token_id: str) -> bool:
        signal = self._signals.get(token_id)
        if signal is None:
            return False
        market = self._find_market_for_token(token_id)
        if market is None:
            return False
        await self._deploy_quote(signal, market)
        return True

    async def undeploy_quote(self, token_id: str) -> bool:
        bid_key = token_id
        ask_key = f"{token_id}_ask"
        found = False
        freed_market_id: Optional[str] = None
        for key in [bid_key, ask_key]:
            q = self._active_quotes.pop(key, None)
            if q:
                found = True
                freed_market_id = q.market_id
                if q.order_id:
                    await self._pm.cancel_order(q.order_id)
        if freed_market_id:
            self._risk.free_slot(freed_market_id)
        return found

    def record_fill(self, market_id: str, underlying: str, side: str, size_usd: float) -> None:
        delta = size_usd if "BUY" in side else -size_usd
        if "NO" in side:
            delta = -delta
        self._inventory[underlying] = self._inventory.get(underlying, 0.0) + delta
        log.debug("Inventory updated", underlying=underlying,
                  delta=delta, net=self._inventory[underlying])
        # Increment per-leg fill counter for MAKER_MAX_FILLS_PER_LEG gate.
        _pos_side = "YES" if "BUY" in side else "NO"
        _fill_key: tuple[str, str] = (market_id, _pos_side)
        self._leg_fill_counts[_fill_key] = self._leg_fill_counts.get(_fill_key, 0) + 1

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _find_market_for_token(self, token_id: str) -> Optional[PMMarket]:
        for market in self._pm.get_markets().values():
            if token_id in market.token_ids():
                return market
        return None

    def find_market_for_token(self, token_id: str) -> Optional[PMMarket]:
        """Public lookup: return the PMMarket that contains token_id, or None."""
        return self._find_market_for_token(token_id)

    def _find_market_by_id(self, market_id: str) -> Optional[PMMarket]:
        return self._pm.get_markets().get(market_id)

    def get_active_quotes(self) -> dict[str, ActiveQuote]:
        return dict(self._active_quotes)

    def restore_active_quote(self, key: str, quote: ActiveQuote) -> None:
        """Inject a restored ActiveQuote (from startup) without going through _deploy_quote.

        Also reserves the risk slot so the position cap is respected while the
        order is resting.
        """
        self._active_quotes[key] = quote
        # Reserve the slot so reserve_slot() doesn't double-count when a fill
        # arrives and tries to re-open the position.
        market = self._find_market_by_id(quote.market_id)
        underlying = market.underlying if market else ""
        self._risk.reserve_slot(quote.market_id, "maker", underlying)
        log.debug(
            "Active quote restored",
            key=key[:40],
            market_id=quote.market_id[:16],
            price=round(quote.price, 4),
            remaining=round(quote.size, 2),
        )

    def get_coin_hedges(self) -> dict[str, dict]:
        """Return a snapshot of all active HL delta hedges keyed by coin."""
        return dict(self._coin_hedges)

    def get_hedge_quality(self, limit: int = 100) -> list[dict]:
        """Return the most-recent hedge execution quality records (newest first)."""
        return list(self._hedge_quality)[:limit]

    def get_hl_mid(self, coin: str) -> Optional[float]:
        return self._hl.get_mid(coin)

    def _get_recent_move_pct(self, coin: str, window_s: float = 300.0) -> Optional[float]:
        """Abs % price change over the last *window_s* seconds, or None if no history."""
        hist = self._hl_mid_history.get(coin)
        if not hist or len(hist) < 2:
            return None
        cutoff = time.time() - window_s
        ref_mid = next((mid for ts, mid in hist if ts >= cutoff), None)
        if ref_mid is None or ref_mid <= 0:
            return None
        return abs(hist[-1][1] - ref_mid) / ref_mid

    def consume_fill(self, key: str, fill_size: Optional[float] = None) -> Optional[ActiveQuote]:
        """Remove a fill from a resting quote. Supports partial fills.

        If fill_size is None or >= quote.size the quote is fully consumed (removed).
        Otherwise the stored quote's size and collateral_usd are decremented and a
        new ActiveQuote representing only the filled slice is returned — mirroring
        how a real CLOB keeps the remainder of a partially-filled resting order.
        """
        q = self._active_quotes.get(key)
        if q is None:
            return None
        # Ensure original_size is stamped (handles quotes created before this field existed)
        if q.original_size == 0.0:
            q.original_size = q.size
        if fill_size is None or fill_size >= q.size:
            return self._active_quotes.pop(key)
        # Partial fill: build a slice and update the stored remainder in-place.
        filled = ActiveQuote(
            market_id=q.market_id,
            token_id=q.token_id,
            side=q.side,
            price=q.price,
            size=round(fill_size, 4),
            order_id=q.order_id,
            posted_at=q.posted_at,
            collateral_usd=round(
                (q.price * fill_size) if q.side == "BUY" else ((1.0 - q.price) * fill_size), 4
            ),
            original_size=q.original_size,  # propagate so callers can see total order size
            score=q.score,                  # propagate so fill writer has correct score
        )
        remaining = round(q.size - fill_size, 4)
        q.size = remaining
        q.collateral_usd = round(
            (q.price * remaining) if q.side == "BUY" else ((1.0 - q.price) * remaining), 4
        )
        # original_size is intentionally NOT changed — it always reflects the full order
        return filled

    def get_inventory(self) -> dict[str, float]:
        return dict(self._inventory)

    def schedule_hedge_rebalance(self, coin: str) -> None:
        """Public wrapper around _schedule_hedge_rebalance for use by fill processors."""
        self._schedule_hedge_rebalance(coin)

    def trigger_post_fill_reprice(self, key: str, market) -> None:
        """Schedule an immediate reprice if the quote at *key* was fully consumed."""
        import asyncio
        if key not in self._active_quotes:
            asyncio.create_task(self._reprice_market(market))
