"""
strategies.Momentum.scanner — Strategy 3: Momentum / price-confirmation taker.

Signal detection + direct execution (no agent loop):

  1. Every MOMENTUM_SCAN_INTERVAL seconds, scan all open bucket markets.
  2. Find markets where one side is in the 0.80-0.90 price band.
  3. Compute signed spot delta toward the winning direction.
  4. Apply dynamic vol threshold (Deribit ATM IV or HL rolling realized vol).
  5. Apply all staleness + depth + duplicate guards.
  6. Execute immediately: pm.place_limit() / pm.place_market() + risk.open_position().

Exit conditions are handled by PositionMonitor (should_exit momentum branch).

See MomentumStrategy.md for full specification.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config
from logger import get_bot_logger
from market_data.pm_client import PMClient, PMMarket, _MARKET_TYPE_DURATION_SECS
from market_data.hl_client import HLClient
from market_data.rtds_client import RTDSClient, SpotPrice, CHAINLINK_MARKET_TYPES
from risk import RiskEngine, Position
from strategies.base import BaseStrategy
from strategies.Momentum.signal import MomentumSignal
from strategies.Momentum.vol_fetcher import VolFetcher

log = get_bot_logger(__name__)

# ── Momentum fill log ─────────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
MOMENTUM_FILLS_CSV = _DATA_DIR / "momentum_fills.csv"
MOMENTUM_FILLS_HEADER = [
    "timestamp",          # ISO UTC when fill was confirmed
    "market_id",
    "market_title",
    "underlying",
    "market_type",
    "side",               # YES | NO
    "signal_price",       # mid price when signal was detected (token_price from signal)
    "order_price",        # price sent to CLOB
    "fill_price",         # actual fill price (from WS/REST confirmation)
    "fill_size",          # contracts filled
    "slippage_pct",       # (fill_price - signal_price) / signal_price * 100
    "signal_delta_pct",   # delta_pct at signal time
    "signal_obs_z",       # observed z at signal time
    "signal_sigma_ann",   # annualised vol used
    "tte_seconds",        # TTE at entry
    "ask_depth_usd",      # CLOB depth at signal time
    "fill_from_ws",       # True = confirmed via WS event, False = REST fallback
    # ── Kelly sizing debug (added with fractional-Kelly implementation) ──
    "kelly_win_prob",     # N(observed_z_total) — model's estimated win probability
    "kelly_payout_b",     # (1-token_price)/token_price — dollars won per dollar risked
    "kelly_f",            # raw full-Kelly fraction before scaling
    "kelly_fraction_cfg", # MOMENTUM_KELLY_FRACTION config value used
    "kelly_size_usd",     # intended position size in USD (=size_usd before fill)
]


def _ensure_momentum_fills_csv() -> None:
    """Create momentum_fills.csv with header; back up on schema change."""
    if not MOMENTUM_FILLS_CSV.exists():
        with MOMENTUM_FILLS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(MOMENTUM_FILLS_HEADER)
        return
    with MOMENTUM_FILLS_CSV.open("r", newline="") as f:
        reader = csv.reader(f)
        try:
            existing = next(reader)
        except StopIteration:
            existing = []
    if existing != MOMENTUM_FILLS_HEADER:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = MOMENTUM_FILLS_CSV.with_name(f"momentum_fills_{ts}.csv.bak")
        MOMENTUM_FILLS_CSV.rename(backup)
        log.info("momentum_fills.csv schema changed — backed up", backup=str(backup))
        with MOMENTUM_FILLS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(MOMENTUM_FILLS_HEADER)


# Bucket market types that the scanner targets.
_TARGET_MARKET_TYPES = frozenset(_MARKET_TYPE_DURATION_SECS.keys())


def _get_oracle_spot(market_type: str, underlying: str, pyth: RTDSClient) -> Optional[SpotPrice]:
    """Return the correct oracle SpotPrice for `underlying` given `market_type`.

    5m / 15m / 4h Up/Down markets resolve against Chainlink; all other bucket
    types (1h, daily, weekly) use the RTDS exchange-aggregated feed.
    """
    if market_type in CHAINLINK_MARKET_TYPES:
        return pyth.get_spot_chainlink(underlying)
    return pyth.get_spot(underlying)


class MomentumScanner(BaseStrategy):
    """
    Async scanner loop for the momentum / price-confirmation strategy.

    Instantiate in main.py and call await scanner.start().
    """

    def __init__(
        self,
        pm: PMClient,
        hl: HLClient,
        risk: RiskEngine,
        vol_fetcher: VolFetcher,
        pyth: RTDSClient,
        on_signal: Any = None,
    ) -> None:
        self._pm = pm
        self._hl = hl
        self._pyth = pyth
        self._risk = risk
        self._vol = vol_fetcher
        self._running = False
        self._on_signal: Any = on_signal  # optional callback(signal_dict) for API state
        # Per-market cooldown after any open/close/failed entry
        self._market_cooldown: dict[str, float] = {}   # market_id → unix timestamp of last touch
        # Persist cooldowns to disk so restarts honour the full cooldown window.
        self._cooldown_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "momentum_cooldowns.json"
        )
        self._market_cooldown: dict[str, float] = _load_cooldowns(self._cooldown_path)
        # Stop-loss block: after a stop-loss exit, re-entry into the same market is
        # blocked until the market's remaining TTE expires to prevent compounding losses.
        # key: condition_id  value: unix timestamp at which the block lifts
        self._stop_loss_blocked: dict[str, float] = {}
        # Open-spot cache for "Up or Down" directional markets.
        # key: condition_id  value: RTDS spot price at the moment the window opened.
        # Persisted to disk so restarts don't lose recorded opens mid-window.
        self._open_spot_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "market_open_spots.json"
        )
        self._market_open_spot: dict[str, float] = _load_open_spots(self._open_spot_path)
        # Diagnostics: per-market snapshot from the last completed _scan_once pass.
        # Read by /momentum/diagnostics — no lock needed (GIL + single asyncio writer).
        self._last_scan_diags: list[dict] = []
        self._last_scan_summary: dict = {}
        self._last_scan_ts: float = 0.0
        self._last_pm_feed_health: str = "unknown"   # "ok" | "degraded" | "unknown"
        self._last_stale_book_ratio: float = 0.0     # fraction of markets with stale books
        # Event-driven entry: set when a price tick enters the signal band.
        # Allows the scan loop to wake immediately rather than waiting the full
        # MOMENTUM_SCAN_INTERVAL (default 10 s) before acting on a fresh signal.
        self._scan_event: asyncio.Event = asyncio.Event()
        # Reverse map: token_id (YES or NO) → PMMarket, rebuilt on each subscription
        # refresh.  Used by _on_price_update_entry for O(1) market lookup per WS tick.
        self._token_to_market: dict[str, PMMarket] = {}
        self._vol_prefetch_task: Optional[asyncio.Task] = None
        # Throttle: last unix timestamp we emitted a log.info for started markets.
        self._last_live_log_ts: float = 0.0

    # ── BaseStrategy interface ────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        log.info(
            "MomentumScanner started",
            interval=config.MOMENTUM_SCAN_INTERVAL,
            band=(config.MOMENTUM_PRICE_BAND_LOW, config.MOMENTUM_PRICE_BAND_HIGH),
            z=config.MOMENTUM_VOL_Z_SCORE,
        )
        # Subscribe bucket markets for book updates immediately so the first scan
        # has book data.  The loop also refreshes every 5 minutes.
        await self._refresh_subscriptions()
        # Pre-warm the vol cache so the first scan pass never waits for a
        # live Deribit round-trip (avoids ~200 ms stall on first signal eval).
        self._vol_prefetch_task = self._vol.start_prefetch(config.TRACKED_UNDERLYINGS)
        # Event-driven entry: wake the scan loop immediately when a YES/NO token
        # price enters the signal band rather than waiting up to SCAN_INTERVAL.
        self._pm.on_price_change(self._on_price_update_entry)
        # Also wake on spot ticks — spot moves can cross the strike and create or
        # dissolve a signal between PM CLOB ticks.  Register for BOTH feed sources:
        # RTDS (1h / daily / weekly markets) and Chainlink (5m / 15m / 4h markets).
        self._pyth.on_price_update(self._on_pyth_spot_update_entry)
        self._pyth.on_chainlink_update(self._on_pyth_spot_update_entry)
        asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._running = False
        if self._vol_prefetch_task and not self._vol_prefetch_task.done():
            self._vol_prefetch_task.cancel()

    def record_trade_close(self, market_id: str) -> None:
        """Refresh per-market cooldown when any momentum position closes.

        Resets BOTH the YES and NO side cooldown clocks for the market.  When
        one side closes (stop-loss or take-profit), the market is considered
        recently active and neither side should be re-entered until the full
        MOMENTUM_MARKET_COOLDOWN_SECONDS window expires.
        """
        now = time.time()
        # Clear all four possible side-key formats so both Yes/No markets (using
        # "YES"/"NO" keys) and Up/Down markets (using "UP"/"DOWN" keys) are covered.
        for _s in ("YES", "NO", "UP", "DOWN"):
            self._market_cooldown[f"{market_id}:{_s}"] = now
        _save_cooldowns(self._cooldown_path, self._market_cooldown)
        log.info("Momentum cooldown reset on close", market_id=market_id[:22])

    def record_stop_loss_close(self, market_id: str, tte_remaining: float) -> None:
        """Block re-entry into this market until its TTE expires after a stop-loss.

        After a stop-loss exit the same market often re-signals within seconds
        (price still in band, delta still above threshold).  Re-entering into a
        market that just stopped us out compounds the loss.  This method records
        an absolute block-until timestamp so no new position is opened in this
        market for the remainder of its window.
        """
        block_until = time.time() + max(0.0, tte_remaining)
        self._stop_loss_blocked[market_id] = block_until
        self.record_trade_close(market_id)
        log.info(
            "Momentum stop-loss block set",
            market_id=market_id[:22],
            block_secs=round(tte_remaining),
        )

    def get_signals(self) -> list[dict]:
        """Return an empty list — momentum signals execute immediately (no queue)."""
        return []

    # ── Subscription refresh ──────────────────────────────────────────────────

    async def _refresh_subscriptions(self) -> None:
        """Register all started bucket markets within MOMENTUM_MAX_TTE_DAYS for PM WS
        book subscriptions, independently of the maker's TTE/volume filters.

        Also subscribes to upcoming (not-yet-started) bucket markets within
        MOMENTUM_PRESUB_LOOKAHEAD additional periods, giving book data for the
        pre-start window (useful for price-vs-TTE data collection).

        Called on startup and every 5 minutes from the scan loop.  This populates
        self._pm._books for the full momentum candidate set so get_book() returns
        live data rather than None for non-maker markets.
        """
        _now = time.time()
        _max_tte = config.MOMENTUM_MAX_TTE_DAYS * 86_400
        _lookahead = config.MOMENTUM_PRESUB_LOOKAHEAD
        tokens: set[str] = set()
        for mkt in self._pm.get_markets().values():
            if mkt.market_type not in _TARGET_MARKET_TYPES:
                continue
            if mkt.end_date is None:
                continue
            tte = mkt.end_date.timestamp() - _now
            if tte <= 0 or tte > _max_tte:
                continue
            _dur = _MARKET_TYPE_DURATION_SECS.get(mkt.market_type)
            if _dur is not None and tte > _dur:
                # Market hasn't started yet.  Subscribe if within the lookahead
                # window (next MOMENTUM_PRESUB_LOOKAHEAD periods) so we capture
                # price data before the entry window opens.
                if _lookahead <= 0 or tte > _dur * (1 + _lookahead):
                    continue
            # Subscribe broadly — include all started + near-start markets
            tokens.add(mkt.token_id_yes)
            tokens.add(mkt.token_id_no)
        self._pm.register_for_book_updates(tokens)
        # Rebuild token → market reverse map (both YES and NO tokens).
        # Used by _on_price_update_entry for O(1) lookup on every WS tick.
        new_map: dict[str, PMMarket] = {}
        for mkt in self._pm.get_markets().values():
            if mkt.market_type in _TARGET_MARKET_TYPES:
                new_map[mkt.token_id_yes] = mkt
                new_map[mkt.token_id_no] = mkt
        self._token_to_market = new_map
        log.info(
            "MomentumScanner: WS subscriptions refreshed",
            tokens=len(tokens),
            max_tte_days=config.MOMENTUM_MAX_TTE_DAYS,
            presub_lookahead=_lookahead,
        )

    # ── Scan loop ─────────────────────────────────────────────────────────────

    _SUBSCRIPTION_REFRESH_INTERVAL = 30   # seconds between _refresh_subscriptions calls (short: 5m buckets need fast pickup)

    async def _scan_loop(self) -> None:
        # start() already called _refresh_subscriptions() — wait the full interval
        # before the next one.  The old formula (time.time() - (INTERVAL - 90))
        # was written for INTERVAL=300; with INTERVAL=60 it produces a negative
        # subtraction and pushes _last_sub_refresh 30 s into the future.
        _last_sub_refresh = time.time()
        while self._running:
            if not config.STRATEGY_MOMENTUM_ENABLED or not config.BOT_ACTIVE:
                self._scan_event.clear()
                await asyncio.sleep(10)
                continue
            # Periodically refresh WS subscriptions so newly-created bucket markets
            # enter the subscription set within a few minutes of going live.
            if time.time() - _last_sub_refresh >= self._SUBSCRIPTION_REFRESH_INTERVAL:
                try:
                    await self._refresh_subscriptions()
                except Exception as exc:
                    log.warning("MomentumScanner: subscription refresh failed", exc=str(exc))
                _last_sub_refresh = time.time()
            # Clear the event BEFORE scanning so any tick that fires DURING _scan_once
            # is captured and triggers an immediate follow-up scan.
            self._scan_event.clear()
            try:
                await self._scan_once()
            except Exception as exc:
                log.error("MomentumScanner: scan loop error", exc=str(exc))
            # Wait for the regular poll interval, but wake immediately if a price-change
            # event signals a market has entered the signal band.
            try:
                await asyncio.wait_for(
                    self._scan_event.wait(), timeout=config.MOMENTUM_SCAN_INTERVAL
                )
                log.debug("MomentumScanner: early wakeup from price event")
            except asyncio.TimeoutError:
                pass  # normal poll interval elapsed

    async def _scan_once(self) -> None:
        """Run one full scan pass over all open bucket markets."""
        now = datetime.now(timezone.utc)
        now_ts = time.time()

        all_markets = list(self._pm.get_markets().values())
        bucket_markets = [m for m in all_markets if m.market_type in _TARGET_MARKET_TYPES]

        if not bucket_markets:
            return

        # Count open momentum positions for the concurrent cap
        open_momentum = sum(
            1 for p in self._risk.get_open_positions()
            if p.strategy == "momentum"
        )

        signals_fired = 0
        skipped_band = 0
        skipped_stale_book = 0
        skipped_stale_spot = 0
        skipped_no_strike = 0
        skipped_delta = 0
        skipped_tte = 0
        skipped_duplicate = 0
        skipped_depth = 0
        skipped_vol = 0
        skipped_cooldown = 0
        skipped_cap = 0
        skipped_beyond_horizon = 0
        skipped_not_started = 0

        band_lo = config.MOMENTUM_PRICE_BAND_LOW
        band_hi = config.MOMENTUM_PRICE_BAND_HIGH

        # Config snapshot embedded in every diag entry — makes the CSV self-contained.
        # min_tte_s is the entry window ceiling, resolved per-market below.
        _min_tte_by_type: dict[str, int] = config.MOMENTUM_MIN_TTE_SECONDS
        _min_tte_default: int = config.MOMENTUM_MIN_TTE_SECONDS_DEFAULT
        _z_by_type: dict[str, float] = config.MOMENTUM_VOL_Z_SCORE_BY_TYPE
        _diag_cfg = {
            "configured_z":    config.MOMENTUM_VOL_Z_SCORE,
            "band_lo":         band_lo,
            "band_hi":         band_hi,
            "book_max_age_s":  config.MOMENTUM_BOOK_MAX_AGE_SECS,
            "spot_max_age_s":  config.MOMENTUM_SPOT_MAX_AGE_SECS,
            "min_clob_depth":  config.MOMENTUM_MIN_CLOB_DEPTH,
        }
        scan_diags: list[dict] = []  # one entry per market; consumed by diagnostics()

        _momentum_max_tte = config.MOMENTUM_MAX_TTE_DAYS * 86_400

        for market in bucket_markets:
            # Build a diag dict incrementally; fields added as each gate passes.
            _d: dict = {
                "market_id":    market.condition_id,
                "market_title": market.title[:80],
                "underlying":   market.underlying,
                "market_type":  market.market_type,
                **_diag_cfg,
            }

            # Pre-compute TTE once for this market.  Stored in _tte_pre so it can
            # be added to the diag entry at every pipeline gate — including early
            # exits — enabling full price-vs-TTE analysis in the CSV output.
            _tte_pre: Optional[float] = (
                market.end_date.timestamp() - now_ts
                if market.end_date is not None else None
            )

            # ── Horizon pre-filter ───────────────────────────────────────────
            # Quickly skip markets that are outside the momentum WS subscription
            # window (no book data exists for them).  This avoids 1000+ get_book()
            # calls per cycle that all return None.
            if _tte_pre is not None:
                if _tte_pre <= 0 or _tte_pre > _momentum_max_tte:
                    skipped_beyond_horizon += 1
                    _d["tte_seconds"] = round(_tte_pre)
                    _d["skip_reason"] = "beyond_horizon"
                    scan_diags.append(_d)
                    continue
                _dur_pre = _MARKET_TYPE_DURATION_SECS.get(market.market_type)
                if _dur_pre is not None and _tte_pre > _dur_pre:
                    skipped_not_started += 1
                    _d["tte_seconds"] = round(_tte_pre)
                    _d["skip_reason"] = "not_started"
                    scan_diags.append(_d)
                    continue

            # ── Cooldown pre-filter ──────────────────────────────────────────
            # Each side has an independent cooldown clock keyed by condition_id:side.
            # Up/Down markets use "UP"/"DOWN" keys; standard markets use "YES"/"NO".
            # Check both conventions so the pre-filter works regardless of which
            # label was written (e.g. markets scanned before this rename keep their
            # old ":YES"/":NO" cooldown entries and are still respected here).
            # max() picks the most-recently-set entry (largest timestamp → smallest
            # elapsed time) so a cooldown in EITHER format is honoured.
            _cd_yes = now_ts - max(
                self._market_cooldown.get(f"{market.condition_id}:YES", 0.0),
                self._market_cooldown.get(f"{market.condition_id}:UP",  0.0),
            )
            _cd_no  = now_ts - max(
                self._market_cooldown.get(f"{market.condition_id}:NO",   0.0),
                self._market_cooldown.get(f"{market.condition_id}:DOWN", 0.0),
            )
            if _cd_yes < config.MOMENTUM_MARKET_COOLDOWN_SECONDS and \
               _cd_no  < config.MOMENTUM_MARKET_COOLDOWN_SECONDS:
                skipped_cooldown += 1
                _d["skip_reason"] = "cooldown"
                # Report how long until the sooner cooldown expires.
                _d["cooldown_remaining_s"] = round(
                    min(config.MOMENTUM_MARKET_COOLDOWN_SECONDS - _cd_yes,
                        config.MOMENTUM_MARKET_COOLDOWN_SECONDS - _cd_no), 1)
                scan_diags.append(_d)
                continue

            # ── PM book for YES token ────────────────────────────────────────
            book_yes = self._pm.get_book(market.token_id_yes)
            if book_yes is None or book_yes.mid is None:
                if _tte_pre is not None:
                    _d["tte_seconds"] = round(_tte_pre)
                _d["skip_reason"] = "no_book"
                scan_diags.append(_d)
                continue

            _book_age = round(now_ts - book_yes.timestamp, 1)
            _d["book_age_s"] = _book_age
            _d["p_yes"] = round(book_yes.mid, 4)

            # ── Stale book gate ──────────────────────────────────────────────
            # If the WS shard for this token has silently stopped delivering
            # updates, the book will still be populated but its mid price will
            # be frozen at the last known value.  Gate out books older than
            # MOMENTUM_BOOK_MAX_AGE_SECS to avoid trading on stale PM data.
            if _book_age > config.MOMENTUM_BOOK_MAX_AGE_SECS:
                skipped_stale_book += 1
                _d["skip_reason"] = "stale_book"
                scan_diags.append(_d)
                continue

            # ── Empty book guard ─────────────────────────────────────────────
            # Check for a book that has no levels at all (distinct from stale:
            # an empty book means no MMs have posted; the no_ask / thin_clob
            # guards downstream will catch a missing best ask on a live book).
            if not book_yes.bids and not book_yes.asks:
                skipped_stale_book += 1
                _d["skip_reason"] = "empty_book"
                scan_diags.append(_d)
                continue

            p_yes = book_yes.mid
            # Fetch actual NO CLOB book mid — evaluate the NO band against the
            # NO token's own orderbook, not a value derived from the YES side.
            # Fall back to derivation only if the NO book is unavailable (rare:
            # both tokens are subscribed; this typically only occurs during WS
            # reconnect within the first few seconds of startup).
            book_no = self._pm.get_book(market.token_id_no)
            if book_no is not None and book_no.mid is not None:
                p_no = book_no.mid
            else:
                # NO book unavailable (WS reconnect / startup race).
                # Do NOT derive from the YES side — YES and NO are independent
                # CLOBs and 1-p_yes ≠ p_no in general.  Skip this market for
                # this scan cycle; it will be re-evaluated on the next tick.
                log.debug(
                    "MomentumScanner: NO book unavailable — skipping market this scan",
                    market_id=market.condition_id[:22],
                    p_yes=round(p_yes, 4),
                )
                skipped_stale_book += 1
                _d["skip_reason"] = "no_book_missing"
                scan_diags.append(_d)
                continue
            _d["p_no"] = round(p_no, 4)

            # ── Find which side is in the signal band ────────────────────────
            # For "Up or Down" bucket markets the outcomes are named "Up" / "Down"
            # (not "Yes" / "No").  Use descriptive labels so signals and positions
            # clearly reflect the correct direction; monitor.py and risk.py accept
            # both "YES"/"UP" (long the first token) and "NO"/"DOWN" (long the second).
            _is_updown = _is_updown_market(market.title)

            # For "dip / drop / fall below $X" markets YES resolves on a DOWNWARD
            # spot move, which is the opposite of a normal YES market.  Invert the
            # direction assignment so delta_pct is computed correctly:
            #   normal YES:   required_direction = "spot_above_strike"
            #   inverted YES: required_direction = "spot_below_strike"  (dip confirmed)
            # With the correct sign, a spurious signal like the Apr-5 XRP dip trades
            # (spot $1.317 above the $1.20/$1.25 dip level) produces a NEGATIVE
            # delta_pct and is rejected by the delta gate — as it should be.
            # A genuinely confirmed dip (spot already below the strike) produces a
            # positive delta_pct and is entered correctly.
            _is_inverted = _is_inverted_direction_market(market.title)

            if band_lo <= p_yes <= band_hi:
                high_side = "UP" if _is_updown else "YES"
                token_id = market.token_id_yes
                token_price = p_yes
                required_direction = "spot_below_strike" if _is_inverted else "spot_above_strike"
            elif band_lo <= p_no <= band_hi:
                high_side = "DOWN" if _is_updown else "NO"
                token_id = market.token_id_no
                token_price = p_no
                required_direction = "spot_above_strike" if _is_inverted else "spot_below_strike"
            else:
                skipped_band += 1
                # Distance to nearest band edge shows tuning headroom.
                _d["skip_reason"] = "out_of_band"
                _d["dist_to_band"] = round(
                    min(abs(p_yes - band_lo), abs(p_yes - band_hi),
                        abs(p_no - band_lo), abs(p_no - band_hi)), 4)
                if _tte_pre is not None:
                    _d["tte_seconds"] = round(_tte_pre)
                scan_diags.append(_d)
                continue

            _d["side"] = high_side
            _d["token_price"] = round(token_price, 4)

            # ── Stop-loss block ──────────────────────────────────────────────
            # If this market had a stop-loss exit, re-entry is blocked until the
            # market's TTE expires to avoid compounding losses into the same window.
            _sl_block_until = self._stop_loss_blocked.get(market.condition_id, 0.0)
            if now_ts < _sl_block_until:
                skipped_cooldown += 1
                _d["skip_reason"] = "stop_loss_block"
                _d["cooldown_remaining_s"] = round(_sl_block_until - now_ts, 1)
                scan_diags.append(_d)
                continue

            # ── Per-side cooldown check ──────────────────────────────────────
            # The early pre-filter above skips only when BOTH sides are cooling.
            # Now that we know which side is in-band, gate on that side's clock.
            _side_key = f"{market.condition_id}:{high_side}"
            if now_ts - self._market_cooldown.get(_side_key, 0.0) < config.MOMENTUM_MARKET_COOLDOWN_SECONDS:
                skipped_cooldown += 1
                _d["skip_reason"] = "cooldown"
                _d["cooldown_remaining_s"] = round(
                    config.MOMENTUM_MARKET_COOLDOWN_SECONDS - (now_ts - self._market_cooldown.get(_side_key, 0.0)), 1)
                scan_diags.append(_d)
                continue

            # ── Oracle spot (source depends on market type) ───────────────────
            # 5m / 15m / 4h → Chainlink (matches Polymarket's resolution oracle).
            # 1h / daily / weekly → RTDS exchange-aggregated.
            snap = _get_oracle_spot(market.market_type, market.underlying, self._pyth)
            if snap is None or snap.mid is None:
                skipped_stale_spot += 1
                _d["skip_reason"] = "no_spot"
                scan_diags.append(_d)
                continue

            _spot_age = round(now_ts - snap.timestamp, 1)
            _d["spot_age_s"] = _spot_age

            # ── Stale spot guard ─────────────────────────────────────────────
            if _spot_age > config.MOMENTUM_SPOT_MAX_AGE_SECS:
                skipped_stale_spot += 1
                _d["skip_reason"] = "stale_spot"
                scan_diags.append(_d)
                continue

            spot = snap.mid
            _d["spot"] = round(spot, 4)

            # ── Parse strike from market title ───────────────────────────────
            strike = _extract_strike(market.title, spot)

            if strike is None and _is_updown_market(market.title):
                # Directional "Up or Down" market: the implicit strike is the spot
                # at window-open time.  Record on first observation using the
                # oracle-correct price source (Chainlink for 5m/15m/4h markets).
                # The not_started pre-filter above ensures we only reach here when
                # tte_seconds <= bucket_duration, so the window has actually opened.
                mid_id = market.condition_id
                if mid_id not in self._market_open_spot:
                    _oracle_src = "chainlink" if market.market_type in CHAINLINK_MARKET_TYPES else "rtds"
                    self._market_open_spot[mid_id] = spot
                    _save_open_spots(self._open_spot_path, self._market_open_spot)
                    log.debug(
                        "MomentumScanner: recorded window-open spot for Up/Down market",
                        market=market.title[:60],
                        market_id=mid_id[:16],
                        open_spot=round(spot, 4),
                        oracle=_oracle_src,
                    )
                strike = self._market_open_spot[mid_id]

            if strike is None:
                skipped_no_strike += 1
                _d["skip_reason"] = "no_strike"
                scan_diags.append(_d)
                continue
            _d["strike"] = round(strike, 4)

            # ── TTE gate ─────────────────────────────────────────────────────
            if market.end_date is None:
                _d["skip_reason"] = "no_end_date"
                scan_diags.append(_d)
                continue
            tte_seconds = (market.end_date - now).total_seconds()
            _d["tte_seconds"] = round(tte_seconds)
            _min_tte = _min_tte_by_type.get(market.market_type, _min_tte_default)
            _d["min_tte_s"] = _min_tte

            # Entry window ceiling: flag markets with too much time left.
            # We do NOT continue here — vol/delta are computed regardless so that
            # the diagnostic CSV contains full empirical data for every in-band
            # market (price, sigma, observed_z, gap_pct) across all TTE values.
            # Trading is blocked later by the _blocked_by_tte flag.
            _blocked_by_tte = tte_seconds > _min_tte
            if _blocked_by_tte:
                skipped_tte += 1

            # ── Dynamic vol threshold ────────────────────────────────────────
            vol_result = await self._vol.get_sigma_ann(market.underlying)
            if vol_result is None:
                skipped_vol += 1
                _d["skip_reason"] = "tte_too_long" if _blocked_by_tte else "no_vol"
                scan_diags.append(_d)
                continue
            sigma_ann, vol_src = vol_result

            sigma_tau = sigma_ann * math.sqrt(tte_seconds / 31_536_000)
            _vol_z = _z_by_type.get(market.market_type, config.MOMENTUM_VOL_Z_SCORE)
            y = _vol_z * sigma_tau * 100  # percent

            _d["sigma_ann"]     = round(sigma_ann, 6)
            _d["sigma_tau"]     = round(sigma_tau, 6)
            _d["configured_z"]  = _vol_z
            _d["threshold_pct"] = round(y, 6)
            _d["vol_source"]    = vol_src

            # ── Signed delta toward winning direction ────────────────────────
            if required_direction == "spot_above_strike":
                delta_pct = (spot - strike) / strike * 100
            else:
                delta_pct = (strike - spot) / strike * 100

            _d["delta_pct"]  = round(delta_pct, 6)
            _d["gap_pct"]    = round(delta_pct - y, 6)   # +ve = above vol-scaled threshold, -ve = below
            _d["observed_z"] = round(delta_pct / (sigma_tau * 100), 4) if sigma_tau > 0 else None

            # Effective threshold: vol-scaled y or the configured absolute floor,
            # whichever is larger.  Recorded so diagnostics reflect the actual gate.
            _effective_threshold = max(y, config.MOMENTUM_MIN_DELTA_PCT)
            _d["min_delta_floor"]      = round(config.MOMENTUM_MIN_DELTA_PCT, 6)
            _d["effective_threshold"]  = round(_effective_threshold, 6)
            _d["effective_gap_pct"]    = round(delta_pct - _effective_threshold, 6)  # +ve = passed gate

            # Now gate on TTE — all diag fields are populated above for research.
            if _blocked_by_tte:
                _d["skip_reason"] = "tte_too_long"
                scan_diags.append(_d)
                continue

            # ── Gate: delta must exceed threshold ────────────────────────────
            # max() enforces an absolute floor independent of time bucket.
            # The floor guards against tick risk: if the spot-to-strike gap is
            # too small, a single adverse tick can flip the position from winning
            # to losing before expiry — regardless of how strong the vol-scaled
            # z-signal looks.  This risk is the same whether it's a 5m, 15m, or
            # 1h market; the absolute price distance determines survival, not TTE.
            if delta_pct < _effective_threshold:
                skipped_delta += 1
                _d["skip_reason"] = "delta_below_threshold"
                scan_diags.append(_d)
                continue

            # ── Gate: minimum gap above threshold ────────────────────────────
            # Prevents marginal signals where a single adverse tick can flip
            # the position from winning to losing before expiry.  0.0 = off.
            _min_gap = config.MOMENTUM_MIN_GAP_PCT
            if _min_gap > 0 and (delta_pct - _effective_threshold) < _min_gap:
                skipped_delta += 1
                _d["skip_reason"] = "gap_below_minimum"
                scan_diags.append(_d)
                continue

            # ── Duplicate guard ──────────────────────────────────────────────
            if any(p.market_id == market.condition_id
                   for p in self._risk.get_open_positions()):
                skipped_duplicate += 1
                _d["skip_reason"] = "duplicate_position"
                scan_diags.append(_d)
                continue

            # ── Concurrent position cap ──────────────────────────────────────
            # Use a live count from the risk engine rather than the snapshot taken
            # at the start of the scan pass — event-driven entries (via
            # _on_price_update_entry waking _scan_loop early) may have added
            # positions during the awaits above, making the snapshot stale.
            _live_momentum = sum(
                1 for p in self._risk.get_open_positions() if p.strategy == "momentum"
            )
            if _live_momentum >= config.MOMENTUM_MAX_CONCURRENT:
                skipped_cap += 1
                _d["skip_reason"] = "concurrent_cap"
                _d["open_momentum"] = _live_momentum
                scan_diags.append(_d)
                continue

            # ── CLOB depth guard ─────────────────────────────────────────────
            book_target = self._pm.get_book(token_id)
            if book_target is None or book_target.best_ask is None:
                skipped_depth += 1
                _d["skip_reason"] = "no_ask"
                scan_diags.append(_d)
                continue

            best_ask = book_target.best_ask
            # Sum USDC depth at asks within 1c of best ask. Be defensive:
            # - treat missing/empty ask lists as zero depth
            # - treat zero or missing depth as insufficient
            try:
                ask_depth_usd = sum(
                    s * p
                    for (p, s) in getattr(book_target, "asks", [])
                    if p <= (best_ask or 0) + 0.01
                )
            except Exception:
                ask_depth_usd = 0.0

            # Normalize and record in diagnostics
            ask_depth_usd = 0.0 if ask_depth_usd is None else float(ask_depth_usd)
            _d["ask_depth_usd"] = round(ask_depth_usd, 2)

            # Enforce: missing/zero depth or depth <= required is thin
            if not ask_depth_usd or ask_depth_usd <= config.MOMENTUM_MIN_CLOB_DEPTH:
                skipped_depth += 1
                log.debug(
                    "Momentum: thin CLOB depth",
                    market=market.title[:60],
                    side=high_side,
                    ask_depth_usd=round(ask_depth_usd, 1),
                    required=config.MOMENTUM_MIN_CLOB_DEPTH,
                )
                _d["skip_reason"] = "thin_clob"
                scan_diags.append(_d)
                continue

            # ── SIGNAL: emit immediately ─────────────────────────────────────
            # vol_src already set above from vol_result (reflects actual source used)
            signal = MomentumSignal(
                market_id=market.condition_id,
                market_title=market.title,
                underlying=market.underlying,
                market_type=market.market_type,
                side=high_side,
                token_id=token_id,
                token_price=token_price,
                p_yes=p_yes,
                delta_pct=delta_pct,
                threshold_pct=_effective_threshold,  # max(vol_threshold, MIN_DELTA_PCT floor)
                spot=spot,
                strike=strike,
                tte_seconds=tte_seconds,
                sigma_ann=sigma_ann,
                vol_source=vol_src,
                vol_z_score=_vol_z,
            )
            # ── Kelly sizing preview for diagnostics ─────────────────────
            # Compute Kelly fields now so the webapp diagnostics row for this
            # signal shows the full sizing breakdown, not just whether it fired.
            _kelly_size_preview, _kelly_preview_debug = _compute_kelly_size_usd(signal)
            _d.update(_kelly_preview_debug)
            _d["skip_reason"] = "signal_fired"
            scan_diags.append(_d)
            log.info(
                "Momentum signal detected",
                **_signal_log_dict(signal),
            )
            # Push to API state for webapp display
            if self._on_signal is not None:
                self._on_signal(_signal_log_dict(signal) | {"timestamp": time.time()})
            # Mark cooldown BEFORE awaiting execution so that concurrent event-driven
            # scan-once calls (triggered by the same price tick while _execute_signal
            # is in-flight) see the cooldown and skip this market immediately.
            # Without this, the event-driven path can fire the same signal 30+ times
            # per second while the async order-placement coroutine is awaited.
            self._market_cooldown[f"{market.condition_id}:{high_side}"] = now_ts
            _save_cooldowns(self._cooldown_path, self._market_cooldown)
            executed = await self._execute_signal(signal, market)
            if executed:
                signals_fired += 1
                # NB: open_momentum counter is NOT incremented here.
                # The cap check above re-queries the risk engine on every
                # iteration (live count), so no manual bookkeeping is needed.

        # ── HL outage detection ───────────────────────────────────────────────
        # If more than half of all scanned markets are being skipped for stale
        # spot, it is far more likely that the HL WS feed has stopped delivering
        # data than that every market genuinely has stale underlying prices.
        # Log a warning so the operator can investigate quickly.
        if len(bucket_markets) > 0 and skipped_stale_spot / len(bucket_markets) > 0.5:
            log.warning(
                "MomentumScanner: possible RTDS outage — >50% markets skipped for stale spot",
                skipped_stale_spot=skipped_stale_spot,
                total_bucket_markets=len(bucket_markets),
            )

        log.debug(
            "Momentum scan complete",
            bucket_markets=len(bucket_markets),
            signals_fired=signals_fired,
            skipped_beyond_horizon=skipped_beyond_horizon,
            skipped_not_started=skipped_not_started,
            skipped_band=skipped_band,
            skipped_stale_book=skipped_stale_book,
            skipped_stale_spot=skipped_stale_spot,
            skipped_no_strike=skipped_no_strike,
            skipped_delta=skipped_delta,
            skipped_tte=skipped_tte,
            skipped_duplicate=skipped_duplicate,
            skipped_depth=skipped_depth,
            skipped_vol=skipped_vol,
            skipped_cooldown=skipped_cooldown,
            skipped_cap=skipped_cap,
        )
        # Persist diags for /momentum/diagnostics — no vol re-calls needed.
        self._last_scan_diags = scan_diags
        self._last_scan_summary = {
            "bucket_markets":          len(bucket_markets),
            "signals_fired":           signals_fired,
            "skipped_beyond_horizon":  skipped_beyond_horizon,
            "skipped_not_started":     skipped_not_started,
            "skipped_band":            skipped_band,
            "skipped_stale_book":      skipped_stale_book,
            "skipped_stale_spot":      skipped_stale_spot,
            "skipped_no_strike":       skipped_no_strike,
            "skipped_delta":           skipped_delta,
            "skipped_tte":             skipped_tte,
            "skipped_duplicate":       skipped_duplicate,
            "skipped_depth":           skipped_depth,
            "skipped_vol":             skipped_vol,
            "skipped_cooldown":        skipped_cooldown,
            "skipped_cap":             skipped_cap,
        }
        self._last_scan_ts = now_ts

        # ── Scanner log: started markets (throttled to once per minute) ──────
        # "Started" = passed the horizon + not_started gates (i.e. market is
        # currently live).  Emit at INFO so entries appear in the webapp log
        # ring-buffer (debug is suppressed for the scanner module).
        _started_diags = [
            d for d in scan_diags
            if d.get("skip_reason") not in ("beyond_horizon", "not_started")
        ]
        if _started_diags and now_ts - self._last_live_log_ts >= 60:
            self._last_live_log_ts = now_ts
            from collections import Counter as _Counter
            _status_counts = dict(_Counter(
                d.get("skip_reason", "?") for d in _started_diags
            ))
            log.info(
                "Momentum: live markets",
                started=len(_started_diags),
                **_status_counts,
            )

        # ── PM feed health ────────────────────────────────────────────────────
        # If >50% of subscribed (non-horizon-filtered) markets have a stale PM
        # book, it is more likely a WS shard outage than normal market activity.
        _total = len(bucket_markets)
        _stale = sum(
            1 for d in scan_diags
            if d.get("skip_reason") in ("stale_book", "empty_book")
        )
        self._last_stale_book_ratio = round(_stale / _total, 3) if _total > 0 else 0.0
        self._last_pm_feed_health = (
            "degraded" if _total > 0 and self._last_stale_book_ratio > 0.5 else "ok"
        )

    # ── Event-driven entry ────────────────────────────────────────────────────

    async def _on_price_update_entry(self, token_id: str, mid: float) -> None:
        """Wake the scan loop immediately when a market enters the signal band.

        Called on every WS book/price_change tick via pm_client's price-change
        callback chain.  Performs only a lightweight band + cooldown pre-filter
        on the hot path; the full multi-gate evaluation is left to _scan_once()
        which runs when the loop wakes up.

        Both YES/UP and NO/DOWN token IDs are in _token_to_market so either token firing
        triggers an early wakeup — important when only the DOWN/NO side is in-band.
        """
        if not config.STRATEGY_MOMENTUM_ENABLED or not config.BOT_ACTIVE:
            return
        market = self._token_to_market.get(token_id)
        if market is None:
            return
        # Each side (YES/UP or NO/DOWN) is evaluated against its own CLOB book mid.
        # The token_id that fired tells us which side updated; use that book's
        # mid directly — do not derive the opposite side's price from 1.0 - mid.
        band_lo = config.MOMENTUM_PRICE_BAND_LOW
        band_hi = config.MOMENTUM_PRICE_BAND_HIGH
        now = time.time()
        # Use "UP"/"DOWN" keys for Up-or-Down markets, "YES"/"NO" for others —
        # consistent with the labels written by _scan_once and record_trade_close.
        _updown = _is_updown_market(market.title)
        if token_id == market.token_id_yes:
            # First token (Up / Yes) fired.
            if not (band_lo <= mid <= band_hi):
                return  # not in band
            side_key = f"{market.condition_id}:{'UP' if _updown else 'YES'}"
        else:
            # Second token (Down / No) fired.
            if not (band_lo <= mid <= band_hi):
                return  # not in band
            side_key = f"{market.condition_id}:{'DOWN' if _updown else 'NO'}"
        # Skip if this side is still in cooldown.
        if now - self._market_cooldown.get(side_key, 0.0) < config.MOMENTUM_MARKET_COOLDOWN_SECONDS:
            return
        # Signal the scan loop to wake up and run a full evaluation now.
        self._scan_event.set()

    async def _on_pyth_spot_update_entry(self, coin: str, price: float) -> None:
        """Wake the scan loop when the RTDS spot price for `coin` changes.

        Mirrors _on_price_update_entry but fires on RTDS ticks rather
        than PM CLOB ticks.  A spot move through the strike changes the delta
        signal even if no PM order-book update has arrived yet.

        Uses the RTDS spot price — the same source Polymarket resolves on —
        so that signal detection and stop-loss are consistent with settlement.
        """
        if not config.STRATEGY_MOMENTUM_ENABLED or not config.BOT_ACTIVE:
            return
        for mkt in self._token_to_market.values():
            if mkt.underlying == coin:
                self._scan_event.set()
                return

    async def diagnostics(self) -> dict:
        """Return per-market diagnostics from the last completed _scan_once pass.

        No vol re-fetching — this is the exact same data the real scanner used.
        Response shape:
          scan_ts   — unix timestamp of the last scan
          markets   — list of per-market dicts, one per bucket market, with:
                       skip_reason, p_yes, p_no, book_age_s, side, token_price,
                       spot, spot_age_s, strike, tte_seconds, sigma_ann, sigma_tau,
                       threshold_pct, delta_pct, gap_pct, observed_z, vol_source,
                       ask_depth_usd, cooldown_remaining_s, dist_to_band,
                       configured_z, band_lo/hi, min_tte_s, book/spot_max_age_s,
                       min_clob_depth.
          summary   — skip-count breakdown matching the debug log line.
        """
        return {
            "scan_ts": self._last_scan_ts,
            "markets": self._last_scan_diags,
            "summary": self._last_scan_summary,
            "pm_feed_health": self._last_pm_feed_health,
            "stale_book_ratio": self._last_stale_book_ratio,
        }

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute_signal(self, signal: MomentumSignal, market: PMMarket) -> bool:
        """
        Place an order for the momentum signal.

        Returns True if an order was placed and the position was opened.
        Performs a final pre-execution price re-check to guard against fast moves
        between signal detection and order placement.
        """
        # ── Final duplicate re-check (race condition guard) ──────────────────
        if any(p.market_id == market.condition_id
               for p in self._risk.get_open_positions()):
            log.debug(
                "Momentum: duplicate guard at execution",
                market_id=signal.market_id,
            )
            return False

        # ── Re-fetch book for pre-execution price validation ─────────────────
        book = self._pm.get_book(signal.token_id)
        if book is None or book.best_ask is None:
            log.warning(
                "Momentum: book gone before execution",
                token_id=signal.token_id[:16],
            )
            return False

        current_ask = book.best_ask
        band_lo = config.MOMENTUM_PRICE_BAND_LOW
        band_hi = config.MOMENTUM_PRICE_BAND_HIGH

        # Allow 0.5c tolerance above band top (tight guard against slippage).
        if not (band_lo <= current_ask <= band_hi + 0.005):
            log.info(
                "Momentum: price moved out of band before execution — skipping",
                token_id=signal.token_id[:16],
                ask=current_ask,
                band=(band_lo, band_hi),
            )
            return False

        # ── Fractional Kelly sizing ────────────────────────────────────────────
        size_usd, _kelly_debug = _compute_kelly_size_usd(signal)

        # ── Place order ───────────────────────────────────────────────────────
        order_id: Optional[str] = None
        order_price = current_ask  # intent price sent to CLOB (not the actual fill)

        if config.MOMENTUM_ORDER_TYPE == "market":
            order_id = await self._pm.place_market(
                token_id=signal.token_id,
                side="BUY",
                price=order_price,
                size=size_usd,
            )
        else:
            # Taker limit: ask + 0.5c to cross the spread and ensure fill
            order_price = round(min(current_ask + 0.005, 0.99), 3)
            order_id = await self._pm.place_limit(
                token_id=signal.token_id,
                side="BUY",
                price=order_price,
                size=size_usd,
                market=market,
                post_only=False,
            )

        if not order_id:
            log.warning(
                "Momentum: order placement failed",
                market=signal.market_title[:60],
                side=signal.side,
                size_usd=size_usd,
            )
            return False

        # ── Record execution from PM source of truth ──────────────────────────
        # YES and NO are independent CLOBs.  entry_price is the actual token fill
        # price for both sides — no YES-space conversion.
        #   YES token: entry_price = fill_price (e.g. 0.83 if buying YES at 0.83)
        #   NO  token: entry_price = fill_price (e.g. 0.83 if buying NO  at 0.83)
        # P&L = (exit_price - entry_price) × size  — same formula for both sides.
        #
        # Fill detection strategy (fastest first):
        #   1. Register a one-shot Future before awaiting anything else.  If the
        #      MATCHED WS event arrives (typical path), extract price/size_matched
        #      directly — zero REST round-trips, ~0 ms latency.
        #   2. If the WS event has no usable price/size fields, fall back to a
        #      single REST call WITHOUT the 1-second sleep (we know it filled).
        #   3. Timeout (5 s) → single REST fallback, then give up gracefully.
        actual_fill: Optional[tuple[float, float]] = None
        if not self._pm._paper_mode:
            _fill_future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pm.register_fill_future(order_id, _fill_future)
            try:
                fill_event = await asyncio.wait_for(_fill_future, timeout=5.0)
                ws_price = float(fill_event.get("price") or 0)
                ws_size  = float(fill_event.get("size_matched") or 0)
                if ws_price > 0 and ws_size > 0:
                    actual_fill = (ws_price, ws_size)
                    log.debug(
                        "Momentum: fill confirmed via WS",
                        order_id=order_id[:20],
                        ws_price=ws_price,
                        ws_size=ws_size,
                    )
                else:
                    actual_fill = await self._pm.get_order_fill_rest(order_id)
            except asyncio.TimeoutError:
                log.debug("Momentum: fill WS timeout — REST fallback", order_id=order_id[:20])
                actual_fill = await self._pm.get_order_fill_rest(order_id)

        if actual_fill is not None:
            raw_fill_price, actual_size = actual_fill
            entry_price = raw_fill_price  # actual token fill price for both YES and NO
            entry_size = actual_size
        else:
            # Paper mode or fill data unavailable.
            entry_price = order_price  # actual token price for both sides
            if not self._pm._paper_mode:
                # Live mode: fill data was not recoverable from WS or REST.
                # Use the CLOB token balance as the source of truth — it reflects
                # exactly how many tokens landed in the wallet after fee deductions.
                _clob_bal = await self._pm.get_token_balance(signal.token_id)
                if _clob_bal and _clob_bal > 0:
                    log.warning(
                        "Momentum: fill data unavailable — using CLOB balance as entry size",
                        token_id=signal.token_id[:20],
                        clob_balance=round(_clob_bal, 6),
                    )
                    entry_size = _clob_bal
                else:
                    # Could not determine size from any source — abort rather than
                    # record a position with a meaningless USD-budget size.
                    log.error(
                        "Momentum: cannot determine entry size (fill + CLOB both failed) — aborting position",
                        token_id=signal.token_id[:20],
                    )
                    return False
            else:
                # Paper mode: convert USD budget → token count so that
                # P&L calculations (exit_price - entry_price) * size are correct.
                entry_size = round(size_usd / order_price, 6)

        # ── Register position with risk engine ────────────────────────────────
        _entry_cost = round(entry_price * entry_size, 6)
        # excess_z: how many annualized-vol standard deviations above the threshold
        # the signal is.  Uses sigma_ann (not sigma_tau) so the value is stable
        # across TTE — near-expiry signals won't artificially inflate it.
        _excess_z = (signal.delta_pct - signal.threshold_pct) / (signal.sigma_ann * 100 + 1e-9)
        pos = Position(
            market_id=signal.market_id,
            market_type=market.market_type,
            underlying=signal.underlying,
            side=signal.side,
            size=entry_size,
            entry_price=entry_price,
            entry_cost_usd=_entry_cost,
            strategy="momentum",
            token_id=signal.token_id,
            market_title=market.title,
            order_id=order_id,
            # Signal metadata — stored so trades.csv supports TTE/z-score analysis
            # without requiring a join to the heavy scanner_samples diagnostic CSV.
            tte_years=signal.tte_seconds / (365.25 * 86400),
            spot_price=signal.spot,
            strike=signal.strike,
            # signal_score = excess_z (sigma_ann-normalised), not the tau-normalised
            # observed_z that inflates near expiry.  Positive means delta exceeded
            # the z-score threshold; higher = stronger signal relative to annual vol.
            signal_score=round(_excess_z, 4),
        )
        self._risk.open_position(pos)

        # ── Write momentum fills CSV for execution-quality analysis ──────────
        try:
            _ensure_momentum_fills_csv()
            _obs_z = signal.delta_pct / (
                signal.sigma_ann * math.sqrt(max(signal.tte_seconds, 1.0) / 31_536_000) * 100
                + 1e-9
            )
            _slippage_pct = (
                round((entry_price - signal.token_price) / signal.token_price * 100, 4)
                if signal.token_price > 0 else 0.0
            )
            # ask depth in USD at execution-time book (within price band)
            _ask_depth_usd = round(
                sum(p * s for p, s in book.asks if p <= band_hi + 0.02), 2
            )
            _fill_row: dict = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_id": signal.market_id,
                "market_title": signal.market_title[:80],
                "underlying": signal.underlying,
                "market_type": signal.market_type,
                "side": signal.side,
                "signal_price": signal.token_price,
                "order_price": order_price,
                "fill_price": round(entry_price, 6),
                "fill_size": round(entry_size, 6),
                "slippage_pct": _slippage_pct,
                "signal_delta_pct": round(signal.delta_pct, 6),
                "signal_obs_z": round(_obs_z, 4),
                "signal_sigma_ann": round(signal.sigma_ann, 6),
                "tte_seconds": round(signal.tte_seconds, 1),
                "ask_depth_usd": _ask_depth_usd,
                "fill_from_ws": actual_fill is not None and not self._pm._paper_mode,
                # Kelly debug fields
                "kelly_win_prob":     _kelly_debug["kelly_win_prob"],
                "kelly_payout_b":     _kelly_debug["kelly_payout_b"],
                "kelly_f":            _kelly_debug["kelly_f"],
                "kelly_fraction_cfg": _kelly_debug["kelly_fraction_cfg"],
                "kelly_size_usd":     _kelly_debug["kelly_size_usd"],
            }
            with MOMENTUM_FILLS_CSV.open("a", newline="") as _f:
                csv.DictWriter(_f, fieldnames=MOMENTUM_FILLS_HEADER).writerow(_fill_row)
        except Exception as _ex:
            log.debug("momentum_fills.csv write error", exc=str(_ex))

        log.info(
            "Momentum position opened ✓",
            market=signal.market_title[:60],
            side=signal.side,
            token_price=current_ask,
            order_price=order_price,
            entry_price=round(entry_price, 4),
            entry_size=entry_size,
            fill_from_clob=actual_fill is not None,
            delta_pct=round(signal.delta_pct, 3),
            threshold_pct=round(signal.threshold_pct, 3),
            sigma_ann=round(signal.sigma_ann, 3),
            vol_source=signal.vol_source,
            order_id=order_id,
            # Kelly sizing breakdown for observability
            kelly_win_prob=_kelly_debug["kelly_win_prob"],
            kelly_payout_b=_kelly_debug["kelly_payout_b"],
            kelly_f=_kelly_debug["kelly_f"],
            kelly_fraction_cfg=_kelly_debug["kelly_fraction_cfg"],
            kelly_size_usd=_kelly_debug["kelly_size_usd"],
        )
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

_STRIKE_PATTERNS = [
    r"\$([0-9,]+(?:\.[0-9]+)?)([kKmM]?)",                      # "$68,300" / "$68k" / "$1.5m"
    r"([0-9,]+(?:\.[0-9]+)?)([kKmM]?)\s*(?:above|below|at)",  # "68000 above"
    r"(?:above|below|at)\s+([0-9,]+(?:\.[0-9]+)?)([kKmM]?)",  # "above 64,200"
]

_UPDOWN_RE = re.compile(r'\bup\s+or\s+down\b', re.IGNORECASE)

# Markets where YES resolves on a DOWNWARD price move.  The scanner's direction
# mapping assumes YES ≡ spot_above_strike; for these markets that assumption is
# inverted, which produces a backwards z-score signal (large positive when spot
# is far ABOVE the dip level) and full Kelly sizing on a doomed trade.
# Safer to skip until a touch-market model is implemented.
_INVERTED_DIRECTION_RE = re.compile(
    r'\b(?:dip|drop|fall|decline|crash)s?\s+(?:to|below|under|beneath)\b'
    r'|\bdip\s+to\b'
    r'|\bbelow\s+\$'
    r'|\bfall\s+to\b'
    r'|\bdrop\s+to\b',
    re.IGNORECASE,
)


def _is_updown_market(title: str) -> bool:
    """Return True if the market is a directional 'Up or Down' window market."""
    return bool(_UPDOWN_RE.search(title))


def _is_inverted_direction_market(title: str) -> bool:
    """Return True if YES resolves on a downward price move (dip/drop/fall markets).

    The scanner maps YES-in-band → spot_above_strike.  These markets invert that
    assumption (YES wins if spot goes DOWN), so they must be excluded to prevent
    the delta_pct sign being backwards and the z-score blowing up.
    """
    return bool(_INVERTED_DIRECTION_RE.search(title))


def _load_open_spots(path: str) -> dict[str, float]:
    """Load persisted open-spot cache from disk, or return empty dict on error."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save_open_spots(path: str, cache: dict[str, float]) -> None:
    """Persist open-spot cache to disk (best-effort, never raises)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass  # non-critical — we just lose the cache on restart for this market


def _load_cooldowns(path: str) -> dict[str, float]:
    """Load persisted cooldown timestamps from disk, or return empty dict on error."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _save_cooldowns(path: str, cache: dict[str, float]) -> None:
    """Persist cooldown timestamps to disk (best-effort, never raises)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass  # non-critical — cooldown continues in-memory; only loses persistence on restart


def _extract_strike(title: str, spot: float) -> Optional[float]:
    """
    Extract a numeric strike from a market title string.

    Handles '$68,300', '$68k', '$1.5m' and the like.
    Returns None if no plausible value is found.
    """
    for pattern in _STRIKE_PATTERNS:
        match = re.search(pattern, title.replace(",", ""))
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                suffix = match.group(2).lower()
                if suffix == "k":
                    value *= 1_000
                elif suffix == "m":
                    value *= 1_000_000
                # Sanity: must be at least 1% of current spot (catches unitless noise)
                if value > spot * 0.01:
                    return value
            except (ValueError, IndexError):
                continue
    return None


def _compute_kelly_size_usd(signal: "MomentumSignal") -> tuple[float, dict]:
    """Compute position size in USD using fractional Kelly Criterion.

    Kelly Criterion for a binary bet:
        f* = max(0, (p×b − (1-p)) / b)
    where:
        p = win probability  estimated as N(observed_z_total)
        b = payout per dollar risked = (1 − token_price) / token_price

    The raw Kelly fraction is multiplied by MOMENTUM_KELLY_FRACTION (default 1.0)
    as a safety dampener.  At 1.0 (no dampening) size = kelly_f × MAX_ENTRY.
    Lower values scale every bet down proportionally while preserving rank.

    Natural behaviour (all three sizing intuitions without explicit knobs):
      • Stronger signal (higher delta) → higher win_prob → bigger size
      • Larger gap above threshold     → higher win_prob → bigger size
      • Higher token price in band     → smaller payout_b → smaller size

    The sigma_tau denominator is floored at 1 second to prevent the z
    computation blowing up when TTE approaches zero near expiry.

    Returns (size_usd, debug_dict).  The debug_dict is written to both the
    fills CSV and the scanner diagnostics so every sizing decision is auditable.

    Debug fields:
        kelly_sigma_tau     — σ scaled to remaining TTE window
        kelly_z_total       — total z-score above zero (vol_z + excess)
        kelly_win_prob      — N(kelly_z_total); model win probability
        kelly_payout_b      — (1 - token_price) / token_price
        kelly_f             — raw full-Kelly fraction
        kelly_fraction_cfg  — MOMENTUM_KELLY_FRACTION config value
        kelly_size_usd      — resulting USD size (= what will be placed)

    ASCII diagram — how each signal dimension flows to size:

        delta_pct ──┐
                    ├──▶ delta / sigma_tau = z ──▶ N(z) = win_prob ──┐
        sigma_ann ──▶ sigma_tau ──────────────────────────────────────┘ ──▶ kelly_f ──▶ × fraction ──▶ size_usd
        token_price ────────────────────────────────────────────────────────────▶ payout_b ──┘

    NOTE: VOL_Z_SCORE, MIN_DELTA_PCT, MIN_GAP_PCT affect the *entry gate* but
    not the Kelly probability estimate once the signal has fired.  The gate
    config lives on the signal's threshold_pct and vol_z_score fields and is
    used for signal_score analytics (position meta) — not for sizing.
    """
    # σ_τ = σ_ann × √(TTE_seconds / one_year_seconds)
    # Floor at 1 s so TTE → 0 near expiry doesn't collapse sigma_tau to 0
    # and send observed_z_total to +∞ (which would always return MAX_ENTRY).
    sigma_tau = signal.sigma_ann * math.sqrt(max(signal.tte_seconds, 1.0) / 31_536_000)

    # Win probability: P(underlying finishes in-the-money at expiry).
    # z = delta_pct / (sigma_tau * 100) — how many remaining-window σ units
    # the spot is currently above (YES) or below (NO) the strike.
    # Using the raw delta/sigma_tau directly is robust to any threshold config:
    # VOL_Z_SCORE, MIN_DELTA_PCT, MIN_GAP_PCT all affect the gate but not the
    # actual probability of the trade being in-the-money at settlement.
    observed_z_total = signal.delta_pct / (sigma_tau * 100 + 1e-9)
    # Cap at 6σ to prevent erf() overflow on very strong signals
    observed_z_total = min(observed_z_total, 6.0)

    # Model win probability: P(underlying finishes on the winning side)
    win_prob = 0.5 * (1.0 + math.erf(observed_z_total / math.sqrt(2.0)))

    # Payout multiple: for a binary token at price p, you risk p dollars to
    # win (1 - p) dollars, so b = (1 - p) / p.
    # Safety-clamp token_price so we never divide by 0 or get negative b.
    token_p = max(0.01, min(0.99, signal.token_price))
    payout_b = (1.0 - token_p) / token_p

    # Full Kelly fraction: the fraction of bankroll a perfect model would bet.
    lose_prob = 1.0 - win_prob
    kelly_f = max(0.0, (win_prob * payout_b - lose_prob) / payout_b)

    # Fractional Kelly: multiply by KELLY_FRACTION safety factor.
    # KELLY_FRACTION=1.0 → deploy kelly_f × MAX_ENTRY (full-Kelly relative to max).
    # KELLY_FRACTION=0.5 → deploy 0.5 × kelly_f × MAX_ENTRY (half-Kelly).
    # KELLY_FRACTION=0.25 → quarter-Kelly.
    _fraction_cfg = max(0.0, min(1.0, config.MOMENTUM_KELLY_FRACTION))
    fraction_of_max = min(1.0, kelly_f * _fraction_cfg)
    size_usd = max(
        config.MOMENTUM_MIN_ENTRY_USD,
        round(fraction_of_max * config.MOMENTUM_MAX_ENTRY_USD, 2),
    )

    debug: dict = {
        "kelly_sigma_tau":    round(sigma_tau, 6),
        "kelly_z_total":      round(observed_z_total, 4),
        "kelly_win_prob":     round(win_prob, 4),
        "kelly_payout_b":     round(payout_b, 4),
        "kelly_f":            round(kelly_f, 4),
        "kelly_fraction_cfg": _fraction_cfg,
        "kelly_size_usd":     size_usd,
    }
    return size_usd, debug


def _signal_log_dict(s: MomentumSignal) -> dict:
    """Return a dict suitable for both structured logging and the /momentum/signals API.

    Keys match the TypeScript MomentumSignal interface in client.ts.
    The abbreviated "market"/"tte_s" aliases are kept alongside for log readability.
    """
    return {
        # Full-field names used by the webapp API
        "market_id":     s.market_id,
        "market_title":  s.market_title[:60],
        "underlying":    s.underlying,
        "market_type":   s.market_type,
        "side":          s.side,
        "token_id":      s.token_id,
        "token_price":   round(s.token_price, 3),
        "p_yes":         round(s.p_yes, 3),
        "delta_pct":     round(s.delta_pct, 3),
        "threshold_pct": round(s.threshold_pct, 3),
        "spot":          round(s.spot, 2),
        "strike":        round(s.strike, 2),
        "tte_seconds":   round(s.tte_seconds),
        "sigma_ann":     round(s.sigma_ann, 3),
        "vol_source":    s.vol_source,
    }
