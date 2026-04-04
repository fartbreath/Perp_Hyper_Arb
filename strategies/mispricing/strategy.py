"""
strategies.mispricing.strategy — Strategy 2: Milestone Market Mispricing Scanner.

Compares Polymarket binary prices against Deribit options-implied probabilities.
When the deviation exceeds the fee hurdle + buffer, emits a MispricingSignal for
the agent (or human) to evaluate.

Signal flow:
  1.  Fetch PM milestone markets (endDate > MILESTONE_MIN_DAYS away)
  2.  Fetch Deribit IV for nearest matching instrument  (via market_data.deribit)
  3.  Compute N(d2) from Black-Scholes log-normal model
  4.  Optional: confirm via Kalshi price              (via market_data.kalshi_client)
  5.  If deviation > threshold → emit MispricingSignal
  6.  Signal goes to AgentDecisionLayer or (in semi-auto) to y/n prompt
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import config
from logger import get_bot_logger
from market_data.pm_client import PMClient, PMMarket
from market_data.hl_client import HLClient
from market_data.rtds_client import RTDSClient
from market_data.deribit import DeribitFetcher
from market_data.kalshi_client import KalshiClient
from risk import min_edge_after_fees
from strategies.base import BaseStrategy
from strategies.mispricing.signals import MispricingSignal
from strategies.mispricing.math import options_implied_probability
from strategies.scoring import score_mispricing

log = get_bot_logger(__name__)


class MispricingScanner(BaseStrategy):
    """
    Scans PM milestone markets periodically and emits MispricingSignal when
    a significant deviation vs Deribit-implied probability is found.
    """

    def __init__(
        self,
        pm: PMClient,
        hl: HLClient,
        signal_callback,   # async callable(signal: MispricingSignal)
        scan_interval: int = 300,
        pyth: Optional[RTDSClient] = None,
    ) -> None:
        self._pm = pm
        self._hl = hl
        self._pyth = pyth  # RTDSClient — authoritative spot, same as Polymarket resolution source
        self._on_signal = signal_callback
        self._default_scan_interval = scan_interval
        self._deribit = DeribitFetcher()
        self._kalshi = KalshiClient()
        self._running = False
        self._market_last_traded: dict[str, float] = {}
        # Set when PM price data changes — wakes the scan loop early
        self._scan_event: asyncio.Event = asyncio.Event()

    async def _on_price_update_mispricing(self, token_id: str, mid: float) -> None:
        """PM price-change callback: wake the scan loop immediately."""
        self._scan_event.set()

    async def start(self) -> None:
        self._running = True
        log.info("MispricingScanner started", interval=config.MISPRICING_SCAN_INTERVAL)
        # Wake scan immediately on every PM price tick so we catch deviations
        # without waiting the full MISPRICING_SCAN_INTERVAL.
        self._pm.on_price_change(self._on_price_update_mispricing)
        await self._kalshi.refresh_markets()
        asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._running = False

    def get_signals(self) -> list:
        """BaseStrategy compliance — scanner signals are delivered via callback, not polled."""
        return []

    def record_trade_close(self, market_id: str) -> None:
        """Reset cooldown clock when a mispricing position closes."""
        self._market_last_traded[market_id] = time.time()
        log.debug("Cooldown reset after close", market_id=market_id[:22])

    # ── Scan loop ──────────────────────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        while self._running:
            if not config.STRATEGY_MISPRICING_ENABLED or not config.BOT_ACTIVE:
                await asyncio.sleep(10)
                continue
            # Clear the event BEFORE scanning so any tick that fires DURING
            # _scan_once is captured and triggers an immediate follow-up pass.
            self._scan_event.clear()
            try:
                await self._scan_once()
            except Exception as exc:
                log.error("Scan loop error", exc=str(exc))
            interval = config.MISPRICING_SCAN_INTERVAL
            log.debug("Scan sleeping", seconds=interval)
            # Wait for a PM price-change event (wakes early) OR the full backstop timeout.
            try:
                await asyncio.wait_for(self._scan_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _scan_once(self) -> None:
        """Run one full scan pass over all milestone markets."""
        now = datetime.now(timezone.utc)
        min_end = now + timedelta(days=config.MILESTONE_MIN_DAYS)
        all_markets = list(self._pm.get_markets().values())
        milestone_markets = [m for m in all_markets if m.market_type == "milestone"]
        signals_found = 0
        checked = 0
        skipped_no_end = 0
        skipped_too_soon = 0
        skipped_no_price = 0
        skipped_no_spot = 0
        skipped_no_strike = 0
        skipped_too_close = 0
        skipped_no_iv = 0
        skipped_no_kalshi = 0
        skipped_kalshi_spread = 0
        skipped_kalshi_conflict = 0
        stale_books_skipped = 0     # skipped_no_price due to stale/missing book
        _STALE_THRESHOLD_S = 120.0  # seconds before a book is considered stale
        book_ages_checked: list[float] = []  # book ages (s) for markets that reach IV check

        log.info(
            "Scan started",
            total_markets=len(all_markets),
            milestone_markets=len(milestone_markets),
        )

        for market in milestone_markets:
            if market.end_date is None or market.end_date < min_end:
                log.debug("Scan skip: end date", market=market.title[:60],
                          end_date=str(market.end_date)[:10] if market.end_date else None)
                skipped_no_end += 1 if market.end_date is None else 0
                skipped_too_soon += 1 if market.end_date is not None else 0
                continue

            pm_price = self._pm.get_mid(market.token_id_yes)
            if pm_price is None or not (0.01 < pm_price < 0.99):
                _snap = self._pm.get_book(market.token_id_yes)
                _book_age_s = round(time.time() - _snap.timestamp, 1) if _snap is not None else None
                log.debug(
                    "Scan skip: no PM price",
                    market=market.title[:60],
                    pm_price=pm_price,
                    has_book=_snap is not None,
                    book_age_s=_book_age_s,
                )
                if _snap is None or (_book_age_s is not None and _book_age_s > _STALE_THRESHOLD_S):
                    stale_books_skipped += 1
                skipped_no_price += 1
                continue

            # Use RTDS spot if wired (authoritative resolution price); fall back to HL perp.
            spot = (
                self._pyth.get_mid(market.underlying)
                if self._pyth is not None
                else self._hl.get_mid(market.underlying)
            )
            if spot is None:
                log.debug("Scan skip: no oracle spot", market=market.title[:60],
                          underlying=market.underlying)
                skipped_no_spot += 1
                continue

            strike = self._extract_strike(market.title, spot)
            if strike is None:
                log.debug("Scan skip: no strike parsed", market=market.title[:60])
                skipped_no_strike += 1
                continue

            checked += 1
            _snap = self._pm.get_book(market.token_id_yes)
            if _snap is not None:
                book_ages_checked.append(time.time() - _snap.timestamp)
            log.debug(
                "Scan checking",
                market=market.title[:60],
                underlying=market.underlying,
                pm_price=round(pm_price, 4),
                spot=round(spot, 0),
                strike=round(strike, 0),
                book_age_s=round(time.time() - _snap.timestamp, 1) if _snap is not None else None,
            )

            tte_years = (market.end_date - now).total_seconds() / (365.25 * 86400)

            iv, instrument = await self._deribit.get_iv_for_target(
                market.underlying, strike, market.end_date
            )
            if iv <= 0:
                log.debug("Scan skip: no Deribit IV", market=market.title[:60],
                          instrument=instrument)
                skipped_no_iv += 1
                continue

            implied = options_implied_probability(spot, strike, tte_years, iv)
            deviation = abs(pm_price - implied)
            fee_hurdle = min_edge_after_fees(pm_price) + 0.03

            log.info(
                "Scan result",
                market=market.title[:60],
                pm=round(pm_price, 4),
                implied=round(implied, 4),
                deviation=round(deviation, 4),
                hurdle=round(fee_hurdle, 4),
                iv_pct=round(iv * 100, 1),
                actionable=deviation > fee_hurdle,
            )

            if deviation > fee_hurdle:
                direction = "BUY_YES" if pm_price < implied else "BUY_NO"

                # Entry filter: skip when strike is too close to spot
                if strike is not None and spot > 0:
                    dist_pct = abs(strike - spot) / spot
                    if dist_pct < config.MIN_STRIKE_DISTANCE_PCT:
                        log.info(
                            "Scan skip: strike too close to spot",
                            market=market.title[:60],
                            dist_pct=round(dist_pct * 100, 2),
                            min_pct=round(config.MIN_STRIKE_DISTANCE_PCT * 100, 1),
                        )
                        skipped_too_close += 1
                        continue

                # Entry filter: skip near-certain prices
                if direction == "BUY_NO" and pm_price >= config.MAX_BUY_NO_YES_PRICE:
                    log.info("Scan skip: BUY_NO YES price too high",
                             market=market.title[:60], pm_yes=round(pm_price, 4),
                             max_allowed=config.MAX_BUY_NO_YES_PRICE)
                    continue
                if direction == "BUY_YES" and pm_price <= config.MIN_BUY_YES_YES_PRICE:
                    log.info("Scan skip: BUY_YES YES price too low",
                             market=market.title[:60], pm_yes=round(pm_price, 4),
                             min_allowed=config.MIN_BUY_YES_YES_PRICE)
                    continue

                # Per-market cooldown
                last_ts = self._market_last_traded.get(market.condition_id, 0.0)
                secs_since = time.time() - last_ts
                if secs_since < config.MISPRICING_MARKET_COOLDOWN_SECONDS:
                    log.info("Scan skip: market cooldown active",
                             market=market.title[:60],
                             cooldown_remaining=round(config.MISPRICING_MARKET_COOLDOWN_SECONDS - secs_since))
                    continue

                # Kalshi confirmation layer
                kalshi_price: Optional[float] = None
                kalshi_ticker: Optional[str] = None
                kalshi_deviation: Optional[float] = None
                signal_source = "nd2_only"

                if config.KALSHI_ENABLED:
                    kalshi_price, kalshi_ticker = await self._kalshi.get_price(
                        market.underlying, strike, market.end_date
                    )
                    if kalshi_price is None:
                        log.info("Scan skip: no Kalshi match", market=market.title[:60],
                                 underlying=market.underlying, strike=round(strike, 0))
                        skipped_no_kalshi += 1
                        continue

                    kalshi_deviation = abs(pm_price - kalshi_price)
                    kalshi_direction = "BUY_YES" if pm_price < kalshi_price else "BUY_NO"

                    if kalshi_deviation < config.KALSHI_MIN_DEVIATION:
                        log.info("Scan skip: Kalshi spread too small",
                                 market=market.title[:60],
                                 pm_price=round(pm_price, 4),
                                 kalshi_price=round(kalshi_price, 4),
                                 spread=round(kalshi_deviation, 4),
                                 min_spread=config.KALSHI_MIN_DEVIATION)
                        skipped_kalshi_spread += 1
                        continue

                    nd2_agrees = (kalshi_direction == direction)
                    if config.KALSHI_REQUIRE_ND2_CONFIRMATION and not nd2_agrees:
                        log.info("Scan skip: Kalshi and N(d₂) direction conflict",
                                 market=market.title[:60],
                                 kalshi_dir=kalshi_direction,
                                 nd2_dir=direction,
                                 kalshi_price=round(kalshi_price, 4),
                                 nd2_implied=round(implied, 4))
                        skipped_kalshi_conflict += 1
                        continue

                    signal_source = "kalshi_confirmed" if nd2_agrees else "kalshi_only"
                    direction = kalshi_direction
                    deviation = kalshi_deviation
                    log.info("Kalshi signal confirmed", market=market.title[:60],
                             pm_price=round(pm_price, 4),
                             kalshi_price=round(kalshi_price, 4),
                             kalshi_deviation=round(kalshi_deviation, 4),
                             signal_source=signal_source, direction=direction)

                signal = MispricingSignal(
                    market_id=market.condition_id,
                    market_title=market.title,
                    underlying=market.underlying,
                    pm_price=pm_price,
                    implied_prob=implied,
                    deviation=deviation,
                    direction=direction,
                    fee_hurdle=fee_hurdle,
                    deribit_iv=iv,
                    deribit_instrument=instrument,
                    spot_price=spot,
                    strike=strike,
                    tte_years=tte_years,
                    fees_enabled=market.fees_enabled,
                    suggested_size_usd=min(
                        config.MAX_PM_EXPOSURE_PER_MARKET * 0.5,
                        deviation * 1000,
                    ),
                    kalshi_price=kalshi_price,
                    kalshi_ticker=kalshi_ticker,
                    kalshi_deviation=kalshi_deviation,
                    signal_source=signal_source,
                )
                signal.volume_24hr = market.volume_24hr
                signal.score = score_mispricing(signal, market.volume_24hr, market.market_type)
                if signal.score < config.MIN_SIGNAL_SCORE_MISPRICING:
                    log.info(
                        "Mispricing signal below score threshold — skipped",
                        market=market.title,
                        score=signal.score,
                        min_score=config.MIN_SIGNAL_SCORE_MISPRICING,
                    )
                    continue
                log.info("Mispricing signal", market=market.title,
                         deviation=round(deviation, 4), direction=direction,
                         score=signal.score)
                signals_found += 1
                self._market_last_traded[market.condition_id] = time.time()
                try:
                    await self._on_signal(signal)
                except Exception as exc:
                    log.error("Signal callback error", exc=str(exc))

        log.info(
            "Scan complete",
            checked=checked,
            signals=signals_found,
            skipped_no_end=skipped_no_end,
            skipped_too_soon=skipped_too_soon,
            skipped_no_price=skipped_no_price,
            skipped_no_spot=skipped_no_spot,
            skipped_no_strike=skipped_no_strike,
            skipped_too_close=skipped_too_close,
            skipped_no_iv=skipped_no_iv,
            skipped_no_kalshi=skipped_no_kalshi,
            skipped_kalshi_spread=skipped_kalshi_spread,
            skipped_kalshi_conflict=skipped_kalshi_conflict,
            stale_books_skipped=stale_books_skipped,
            book_oldest_s=round(max(book_ages_checked), 1) if book_ages_checked else None,
            book_freshest_s=round(min(book_ages_checked), 1) if book_ages_checked else None,
            book_stale_checked=sum(1 for a in book_ages_checked if a > _STALE_THRESHOLD_S),
        )

    def _extract_strike(self, title: str, spot: float) -> Optional[float]:
        """
        Parse a target price from a market title.
        e.g. "Will BTC reach $120k by end of Q2?" → 120000.0

        Falls back to None if not parseable or implausibly small vs spot.
        """
        patterns = [r"\$([0-9,]+(?:\.[0-9]+)?)([kKmM]?)"]
        for pattern in patterns:
            match = re.search(pattern, title.replace(",", ""))
            if match:
                num_str = match.group(1).replace(",", "")
                suffix = match.group(2).lower()
                try:
                    value = float(num_str)
                    if suffix == "k":
                        value *= 1_000
                    elif suffix == "m":
                        value *= 1_000_000
                    if value > spot * 0.01:  # sanity: > 1% of spot
                        return value
                except ValueError:
                    continue
        return None
