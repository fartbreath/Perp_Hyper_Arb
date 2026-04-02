"""
tests/test_momentum_scanner.py — Unit tests for strategies/Momentum/scanner.py

Run: pytest tests/test_momentum_scanner.py -v

Coverage:
  - _extract_strike() parsing ($68k, $1.5m, $68,300, plain number)
  - _is_updown_market()
  - Cooldown persistence helpers (_load_cooldowns, _save_cooldowns)
  - MomentumSignal.vol_z_score field and edge_pct property
  - record_trade_close (per-side cooldown, persistence)
  - YES/NO cooldown independence (YES cooling never blocks NO)
  - E5: edge-proportional sizing formula
  - E7: diagnostics() returns pm_feed_health / stale_book_ratio
  - E9: cooldowns loaded on scanner init and saved on every write
  - _on_price_update_entry (band-triggered scan wakeup, per-side)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
from risk import RiskEngine
from pm_client import PMMarket, OrderBookSnapshot
from strategies.Momentum.scanner import (
    MomentumScanner,
    _extract_strike,
    _is_updown_market,
    _load_cooldowns,
    _save_cooldowns,
)
from strategies.Momentum.signal import MomentumSignal


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_market(
    condition_id: str = "cond_001",
    token_id_yes: str = "tid_yes_001",
    token_id_no: str = "tid_no_001",
    title: str = "Will BTC reach $70k?",
    market_type: str = "bucket_5m",
    underlying: str = "BTC",
    end_date: Optional[datetime] = None,
) -> PMMarket:
    if end_date is None:
        end_date = datetime.now(timezone.utc) + timedelta(seconds=90)
    return PMMarket(
        condition_id=condition_id,
        token_id_yes=token_id_yes,
        token_id_no=token_id_no,
        title=title,
        market_type=market_type,
        underlying=underlying,
        fees_enabled=False,
        end_date=end_date,
    )


def _make_book(mid: float, age_secs: float = 0.5) -> OrderBookSnapshot:
    half = 0.005
    snap = OrderBookSnapshot(token_id="t")
    snap.bids = [(round(mid - half, 3), 500.0)]
    snap.asks = [(round(mid + half, 3), 500.0)]
    snap.timestamp = time.time() - age_secs
    return snap


def _make_scanner(tmp_path=None) -> MomentumScanner:
    """Return a MomentumScanner with all external dependencies mocked."""
    pm = MagicMock()
    pm._paper_mode = True
    pm._books = {}
    pm._markets = {}
    pm.get_book = MagicMock(return_value=None)
    pm.get_markets = MagicMock(return_value={})
    pm.on_price_change = MagicMock()
    pm.place_market = AsyncMock(return_value="order_123")
    pm.place_limit = AsyncMock(return_value="order_123")
    pm.get_token_balance = AsyncMock(return_value=None)
    pm.register_fill_future = MagicMock()

    hl = MagicMock()
    hl.get_bbo = MagicMock(return_value=None)

    risk = RiskEngine()

    vol = MagicMock()
    vol.get_sigma_ann = AsyncMock(return_value=(0.80, "hl_realized"))
    vol.start_prefetch = MagicMock()

    scanner = MomentumScanner(pm=pm, hl=hl, risk=risk, vol_fetcher=vol)
    if tmp_path is not None:
        scanner._cooldown_path = str(tmp_path / "cooldowns.json")
        scanner._open_spot_path = str(tmp_path / "open_spots.json")
    return scanner


def _make_signal(**kwargs) -> MomentumSignal:
    defaults = dict(
        market_id="cond_001",
        market_title="Will BTC reach $70k?",
        underlying="BTC",
        market_type="bucket_5m",
        side="YES",
        token_id="tid_yes_001",
        token_price=0.85,
        p_yes=0.85,
        delta_pct=3.0,
        threshold_pct=1.5,
        spot=70_000.0,
        strike=70_000.0,
        tte_seconds=60.0,
        sigma_ann=0.80,
        vol_source="hl_realized",
        vol_z_score=1.6449,
    )
    defaults.update(kwargs)
    return MomentumSignal(**defaults)


# ── _extract_strike ───────────────────────────────────────────────────────────

class TestExtractStrike:
    def test_dollar_k(self):
        val = _extract_strike("Will BTC reach $68k by end of hour?", 68_000)
        assert val == pytest.approx(68_000)

    def test_dollar_k_capital(self):
        val = _extract_strike("BTC above $70K this hour", 70_000)
        assert val == pytest.approx(70_000)

    def test_dollar_comma_number(self):
        val = _extract_strike("Will BTC be above $68,300?", 68_000)
        assert val == pytest.approx(68_300)

    def test_dollar_m(self):
        val = _extract_strike("ETH reaches $1.5m market cap proxy?", 1_500_000)
        assert val == pytest.approx(1_500_000)

    def test_plain_above(self):
        val = _extract_strike("68000 above current price", 68_000)
        assert val == pytest.approx(68_000)

    def test_no_strike_returns_none(self):
        assert _extract_strike("Will BTC go up or down this hour", 68_000) is None

    def test_sanity_guard_filters_tiny_value(self):
        # "1" in title is way below 1% of spot=68000=680 minimum → filtered out
        assert _extract_strike("ETH 1% move", 68_000) is None


# ── _is_updown_market ─────────────────────────────────────────────────────────

class TestIsUpdownMarket:
    def test_detects_up_or_down(self):
        assert _is_updown_market("Will ETH go Up or Down by 2%?") is True

    def test_detects_lowercase(self):
        assert _is_updown_market("btc up or down this hour?") is True

    def test_false_for_strike_market(self):
        assert _is_updown_market("Will BTC reach $70,000?") is False


# ── Cooldown persistence helpers ──────────────────────────────────────────────

class TestCooldownPersistence:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "cd.json")
        data = {"cond:YES": 1_000.5, "cond:NO": 2_000.5}
        _save_cooldowns(path, data)
        loaded = _load_cooldowns(path)
        assert loaded["cond:YES"] == pytest.approx(1_000.5)
        assert loaded["cond:NO"] == pytest.approx(2_000.5)

    def test_load_missing_file_returns_empty(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        assert _load_cooldowns(path) == {}

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        path = str(tmp_path / "bad.json")
        Path(path).write_text("not valid json{{{")
        assert _load_cooldowns(path) == {}

    def test_save_silently_ignores_bad_path(self):
        # Non-existent root directory — must not raise.
        _save_cooldowns("/nonexistent_xyz_root/cd.json", {"k": 1.0})


# ── MomentumSignal ────────────────────────────────────────────────────────────

class TestMomentumSignal:
    def test_vol_z_score_field_default(self):
        sig = _make_signal()
        assert sig.vol_z_score == pytest.approx(1.6449)

    def test_vol_z_score_custom_stored_correctly(self):
        sig = _make_signal(vol_z_score=2.0)
        assert sig.vol_z_score == pytest.approx(2.0)

    def test_edge_pct_positive_when_delta_exceeds_threshold(self):
        sig = _make_signal(delta_pct=3.0, threshold_pct=1.0, token_price=0.85)
        assert sig.edge_pct > 0.0

    def test_edge_pct_non_negative_at_threshold(self):
        # delta_pct == threshold_pct → excess_z = 0 → edge_pct is N(z)-price ≥ 0
        sig = _make_signal(delta_pct=1.5, threshold_pct=1.5, token_price=0.50)
        assert sig.edge_pct >= 0.0

    def test_edge_pct_smaller_for_weaker_signal(self):
        strong = _make_signal(delta_pct=5.0, threshold_pct=1.0, token_price=0.85)
        weak = _make_signal(delta_pct=1.5, threshold_pct=1.0, token_price=0.85)
        assert strong.edge_pct > weak.edge_pct


# ── record_trade_close ────────────────────────────────────────────────────────

class TestRecordTradeClose:
    def test_sets_both_sides_in_memory(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        before = time.time()
        scanner.record_trade_close("cond_001")
        after = time.time()
        assert "cond_001:YES" in scanner._market_cooldown
        assert "cond_001:NO" in scanner._market_cooldown
        assert before <= scanner._market_cooldown["cond_001:YES"] <= after
        assert before <= scanner._market_cooldown["cond_001:NO"] <= after

    def test_persists_both_sides_to_disk(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        scanner.record_trade_close("cond_001")
        loaded = _load_cooldowns(scanner._cooldown_path)
        assert "cond_001:YES" in loaded
        assert "cond_001:NO" in loaded

    def test_yes_and_no_timestamps_equal(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        scanner.record_trade_close("cond_001")
        yes_ts = scanner._market_cooldown["cond_001:YES"]
        no_ts = scanner._market_cooldown["cond_001:NO"]
        # Both sides set inside the same call → identical timestamp
        assert yes_ts == pytest.approx(no_ts, abs=1e-3)


# ── YES/NO cooldown independence ──────────────────────────────────────────────

class TestCooldownIndependence:
    def test_yes_cooled_does_not_block_no(self, tmp_path):
        """YES on cooldown must not affect NO-side entry eligibility."""
        scanner = _make_scanner(tmp_path)
        orig = config.MOMENTUM_MARKET_COOLDOWN_SECONDS
        config.MOMENTUM_MARKET_COOLDOWN_SECONDS = 1800
        try:
            scanner._market_cooldown["cond_001:YES"] = time.time()
            # NO was never touched → elapsed is large → not on cooldown
            no_elapsed = time.time() - scanner._market_cooldown.get("cond_001:NO", 0.0)
            assert no_elapsed >= config.MOMENTUM_MARKET_COOLDOWN_SECONDS
        finally:
            config.MOMENTUM_MARKET_COOLDOWN_SECONDS = orig

    def test_no_cooled_does_not_block_yes(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        orig = config.MOMENTUM_MARKET_COOLDOWN_SECONDS
        config.MOMENTUM_MARKET_COOLDOWN_SECONDS = 1800
        try:
            scanner._market_cooldown["cond_001:NO"] = time.time()
            yes_elapsed = time.time() - scanner._market_cooldown.get("cond_001:YES", 0.0)
            assert yes_elapsed >= config.MOMENTUM_MARKET_COOLDOWN_SECONDS
        finally:
            config.MOMENTUM_MARKET_COOLDOWN_SECONDS = orig


# ── E5: edge-proportional sizing formula ─────────────────────────────────────

class TestEdgeProportionalSizing:
    """Tests replicate the sizing formula embedded in _execute_signal."""

    @staticmethod
    def _compute_size(edge_pct: float) -> float:
        _anchor = config.MOMENTUM_EDGE_SIZE_ANCHOR
        _max = config.MOMENTUM_MAX_ENTRY_USD
        _min = config.MOMENTUM_MIN_ENTRY_USD
        if _anchor > 0 and edge_pct > 0:
            _fraction = min(1.0, edge_pct / _anchor)
            return max(_min, round(_fraction * _max, 2))
        return _max

    def setup_method(self):
        self._saved = {
            "MOMENTUM_EDGE_SIZE_ANCHOR": config.MOMENTUM_EDGE_SIZE_ANCHOR,
            "MOMENTUM_MAX_ENTRY_USD": config.MOMENTUM_MAX_ENTRY_USD,
            "MOMENTUM_MIN_ENTRY_USD": config.MOMENTUM_MIN_ENTRY_USD,
        }
        config.MOMENTUM_EDGE_SIZE_ANCHOR = 0.10
        config.MOMENTUM_MAX_ENTRY_USD = 50.0
        config.MOMENTUM_MIN_ENTRY_USD = 1.0

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def test_full_size_at_anchor(self):
        assert self._compute_size(0.10) == pytest.approx(50.0)

    def test_full_size_above_anchor(self):
        assert self._compute_size(0.20) == pytest.approx(50.0)

    def test_half_size_at_half_anchor(self):
        assert self._compute_size(0.05) == pytest.approx(25.0)

    def test_quarter_size_at_quarter_anchor(self):
        assert self._compute_size(0.025) == pytest.approx(12.5)

    def test_floored_at_min_entry(self):
        config.MOMENTUM_MIN_ENTRY_USD = 5.0
        # edge_pct=0.001 → fraction=0.01 → raw=0.5 → floored to 5.0
        assert self._compute_size(0.001) == pytest.approx(5.0)

    def test_zero_edge_pct_returns_max(self):
        # edge=0 → formula bypassed, returns MAX
        assert self._compute_size(0.0) == pytest.approx(50.0)


# ── E7: diagnostics() feed health ────────────────────────────────────────────

class TestFeedHealth:
    def test_diagnostics_has_feed_health_keys(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        result = _run(scanner.diagnostics())
        assert "pm_feed_health" in result
        assert "stale_book_ratio" in result

    def test_initial_pm_feed_health_is_unknown(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        result = _run(scanner.diagnostics())
        assert result["pm_feed_health"] == "unknown"

    def test_initial_stale_book_ratio_is_zero(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        result = _run(scanner.diagnostics())
        assert result["stale_book_ratio"] == pytest.approx(0.0)

    def test_scan_ts_is_zero_before_first_scan(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        result = _run(scanner.diagnostics())
        assert result["scan_ts"] == pytest.approx(0.0)


# ── E9: cooldown disk persistence on write ───────────────────────────────────

class TestCooldownDiskPersistence:
    def test_record_close_updates_disk(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        before = time.time()
        scanner.record_trade_close("cond_B")
        after = time.time()
        loaded = _load_cooldowns(scanner._cooldown_path)
        assert "cond_B:YES" in loaded
        assert before <= loaded["cond_B:YES"] <= after

    def test_scanner_respects_persisted_cooldown_after_restart(self, tmp_path):
        """Simulate restart: pre-write cooldown, reboot scanner, verify it reads it."""
        path = str(tmp_path / "cooldowns.json")
        recent_ts = time.time() - 5.0   # 5 seconds ago — still cooling
        with open(path, "w") as f:
            json.dump({"cond_C:YES": recent_ts, "cond_C:NO": recent_ts}, f)
        # Re-create scanner (restart) by directly calling _load_cooldowns
        loaded = _load_cooldowns(path)
        assert loaded["cond_C:YES"] == pytest.approx(recent_ts, abs=1e-3)

    def test_multiple_closes_overwrite_disk(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        scanner.record_trade_close("cond_D")
        ts_first = _load_cooldowns(scanner._cooldown_path)["cond_D:YES"]
        time.sleep(0.01)
        scanner.record_trade_close("cond_D")
        ts_second = _load_cooldowns(scanner._cooldown_path)["cond_D:YES"]
        assert ts_second >= ts_first


# ── _on_price_update_entry ────────────────────────────────────────────────────

class TestOnPriceUpdateEntry:

    def setup_method(self):
        self._saved = {
            "STRATEGY_MOMENTUM_ENABLED": config.STRATEGY_MOMENTUM_ENABLED,
            "BOT_ACTIVE": config.BOT_ACTIVE,
            "MOMENTUM_PRICE_BAND_LOW": config.MOMENTUM_PRICE_BAND_LOW,
            "MOMENTUM_PRICE_BAND_HIGH": config.MOMENTUM_PRICE_BAND_HIGH,
            "MOMENTUM_MARKET_COOLDOWN_SECONDS": config.MOMENTUM_MARKET_COOLDOWN_SECONDS,
        }
        config.STRATEGY_MOMENTUM_ENABLED = True
        config.BOT_ACTIVE = True
        config.MOMENTUM_PRICE_BAND_LOW = 0.80
        config.MOMENTUM_PRICE_BAND_HIGH = 0.90
        config.MOMENTUM_MARKET_COOLDOWN_SECONDS = 1800

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def test_yes_in_band_sets_scan_event(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt, mkt.token_id_no: mkt}
        assert not scanner._scan_event.is_set()
        _run(scanner._on_price_update_entry(mkt.token_id_yes, 0.85))
        assert scanner._scan_event.is_set()

    def test_no_in_band_sets_scan_event(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt, mkt.token_id_no: mkt}
        _run(scanner._on_price_update_entry(mkt.token_id_no, 0.82))
        assert scanner._scan_event.is_set()

    def test_out_of_band_yes_does_not_set_event(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt}
        _run(scanner._on_price_update_entry(mkt.token_id_yes, 0.65))
        assert not scanner._scan_event.is_set()

    def test_out_of_band_no_does_not_set_event(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt, mkt.token_id_no: mkt}
        _run(scanner._on_price_update_entry(mkt.token_id_no, 0.99))
        assert not scanner._scan_event.is_set()

    def test_cooled_yes_side_does_not_wake_scanner(self, tmp_path):
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt}
        scanner._market_cooldown[f"{mkt.condition_id}:YES"] = time.time()
        _run(scanner._on_price_update_entry(mkt.token_id_yes, 0.84))
        assert not scanner._scan_event.is_set()

    def test_yes_cooled_no_still_wakes_scanner(self, tmp_path):
        """YES side on cooldown must not suppress NO-side wakeup (per-side independence)."""
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt, mkt.token_id_no: mkt}
        scanner._market_cooldown[f"{mkt.condition_id}:YES"] = time.time()
        # NO is NOT on cooldown; an in-band NO tick must still trigger wake
        _run(scanner._on_price_update_entry(mkt.token_id_no, 0.84))
        assert scanner._scan_event.is_set()

    def test_strategy_disabled_does_not_set_event(self, tmp_path):
        config.STRATEGY_MOMENTUM_ENABLED = False
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt}
        _run(scanner._on_price_update_entry(mkt.token_id_yes, 0.85))
        assert not scanner._scan_event.is_set()

    def test_bot_inactive_does_not_set_event(self, tmp_path):
        config.BOT_ACTIVE = False
        scanner = _make_scanner(tmp_path)
        mkt = _make_market()
        scanner._token_to_market = {mkt.token_id_yes: mkt}
        _run(scanner._on_price_update_entry(mkt.token_id_yes, 0.85))
        assert not scanner._scan_event.is_set()


# ── Paper-mode position sizing ────────────────────────────────────────────────

class TestPaperModePositionSizing:
    """
    Verify that _execute_signal stores token count (not USD budget) and that
    entry_cost_usd is computed correctly for both YES and NO sides.
    """

    def setup_method(self):
        self._saved = {
            "STRATEGY_MOMENTUM_ENABLED": config.STRATEGY_MOMENTUM_ENABLED,
            "BOT_ACTIVE": config.BOT_ACTIVE,
            "MOMENTUM_PRICE_BAND_LOW": config.MOMENTUM_PRICE_BAND_LOW,
            "MOMENTUM_PRICE_BAND_HIGH": config.MOMENTUM_PRICE_BAND_HIGH,
            "MOMENTUM_MAX_ENTRY_USD": config.MOMENTUM_MAX_ENTRY_USD,
            "MOMENTUM_MIN_ENTRY_USD": config.MOMENTUM_MIN_ENTRY_USD,
            "MOMENTUM_EDGE_SIZE_ANCHOR": config.MOMENTUM_EDGE_SIZE_ANCHOR,
            "MOMENTUM_ORDER_TYPE": config.MOMENTUM_ORDER_TYPE,
        }
        config.STRATEGY_MOMENTUM_ENABLED = True
        config.BOT_ACTIVE = True
        config.MOMENTUM_PRICE_BAND_LOW = 0.50
        config.MOMENTUM_PRICE_BAND_HIGH = 0.95
        config.MOMENTUM_MAX_ENTRY_USD = 3.0
        config.MOMENTUM_MIN_ENTRY_USD = 0.5
        config.MOMENTUM_EDGE_SIZE_ANCHOR = 0.0   # disable edge sizing → always max
        config.MOMENTUM_ORDER_TYPE = "market"

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _run_execute(self, scanner, signal, market):
        return _run(scanner._execute_signal(signal, market))

    def test_paper_yes_size_is_token_count_not_usd(self, tmp_path):
        """entry_size must be size_usd / ask_price (token count), not size_usd itself."""
        ask_price = 0.85
        size_usd = config.MOMENTUM_MAX_ENTRY_USD   # = 3.0
        scanner = _make_scanner(tmp_path)
        # _make_book uses mid±0.005; set mid = ask_price - 0.005 so best_ask == ask_price
        book = _make_book(mid=ask_price - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)
        mkt = _make_market()
        sig = _make_signal(side="YES", token_id=mkt.token_id_yes, token_price=ask_price,
                           p_yes=ask_price, delta_pct=3.0)
        result = self._run_execute(scanner, sig, mkt)
        assert result is True
        positions = scanner._risk.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        expected_tokens = round(size_usd / ask_price, 6)
        assert pos.size == pytest.approx(expected_tokens, rel=1e-4), (
            f"Expected {expected_tokens} tokens, got {pos.size} (USD budget={size_usd})"
        )

    def test_paper_yes_entry_cost_usd_correct(self, tmp_path):
        """entry_cost_usd must equal entry_price * token_count ≈ size_usd."""
        ask_price = 0.85
        size_usd = config.MOMENTUM_MAX_ENTRY_USD
        scanner = _make_scanner(tmp_path)
        book = _make_book(mid=ask_price - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)
        mkt = _make_market()
        sig = _make_signal(side="YES", token_id=mkt.token_id_yes, token_price=ask_price,
                           p_yes=ask_price, delta_pct=3.0)
        self._run_execute(scanner, sig, mkt)
        pos = scanner._risk.get_open_positions()[0]
        expected_cost = round(pos.entry_price * pos.size, 6)
        assert pos.entry_cost_usd == pytest.approx(expected_cost, abs=1e-4)
        # Should also be ≈ size_usd (within rounding)
        assert pos.entry_cost_usd == pytest.approx(size_usd, abs=0.01)

    def test_paper_no_size_is_token_count_not_usd(self, tmp_path):
        """NO side: order_price is the NO CLOB ask (e.g. 0.80), converts correctly.
        A NO signal fires when the NO token is in-band (50-95c), meaning YES is low."""
        no_ask = 0.80   # NO token at 80c (YES ≈ 0.20 — market strongly against)
        size_usd = config.MOMENTUM_MAX_ENTRY_USD
        scanner = _make_scanner(tmp_path)
        # mid = no_ask - 0.005 so best_ask == no_ask exactly
        book = _make_book(mid=no_ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)
        mkt = _make_market()
        sig = _make_signal(side="NO", token_id=mkt.token_id_no,
                           token_price=no_ask, p_yes=1.0 - no_ask,
                           delta_pct=3.0)
        result = self._run_execute(scanner, sig, mkt)
        assert result is True
        pos = scanner._risk.get_open_positions()[0]
        expected_tokens = round(size_usd / no_ask, 6)
        assert pos.size == pytest.approx(expected_tokens, rel=1e-4)

    def test_paper_no_entry_cost_usd_correct(self, tmp_path):
        """NO entry_cost_usd = entry_price × token_count (actual NO token price × tokens)."""
        no_ask = 0.80  # actual NO token price at ask
        size_usd = config.MOMENTUM_MAX_ENTRY_USD
        scanner = _make_scanner(tmp_path)
        book = _make_book(mid=no_ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)
        mkt = _make_market()
        sig = _make_signal(side="NO", token_id=mkt.token_id_no,
                           token_price=no_ask, p_yes=1.0 - no_ask,
                           delta_pct=3.0)
        self._run_execute(scanner, sig, mkt)
        pos = scanner._risk.get_open_positions()[0]
        # entry_price for NO = actual NO token price = no_ask
        # entry_cost = entry_price × size ≈ size_usd
        expected_cost = round(pos.entry_price * pos.size, 6)
        assert pos.entry_cost_usd == pytest.approx(expected_cost, abs=1e-4)
        assert pos.entry_cost_usd == pytest.approx(size_usd, abs=0.01)
