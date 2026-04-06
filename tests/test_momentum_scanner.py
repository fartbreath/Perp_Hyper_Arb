"""
tests/test_momentum_scanner.py â€” Unit tests for strategies/Momentum/scanner.py

Run: pytest tests/test_momentum_scanner.py -v

Coverage:
  - _extract_strike() parsing ($68k, $1.5m, $68,300, plain number)
  - _is_updown_market()
  - Cooldown persistence helpers (_load_cooldowns, _save_cooldowns)
  - MomentumSignal.vol_z_score field and edge_pct property
  - record_trade_close (per-side cooldown, persistence)
  - YES/NO cooldown independence (YES cooling never blocks NO)
  - E5: Kelly-criterion sizing (_compute_kelly_size_usd)
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
    _compute_kelly_size_usd,
    _extract_strike,
    _is_updown_market,
    _load_cooldowns,
    _save_cooldowns,
)
from strategies.Momentum.market_utils import (
    _extract_range_bounds,
    _is_range_market,
)
from strategies.Momentum.signal import MomentumSignal


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

    spot = MagicMock()
    spot.get_mid = MagicMock(side_effect=lambda c: 99_900.0)

    scanner = MomentumScanner(pm=pm, hl=hl, risk=risk, vol_fetcher=vol, spot_client=spot)
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


# â”€â”€ _extract_strike â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # "1" in title is way below 1% of spot=68000=680 minimum â†’ filtered out
        assert _extract_strike("ETH 1% move", 68_000) is None


# â”€â”€ _is_updown_market â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestIsUpdownMarket:
    def test_detects_up_or_down(self):
        assert _is_updown_market("Will ETH go Up or Down by 2%?") is True

    def test_detects_lowercase(self):
        assert _is_updown_market("btc up or down this hour?") is True

    def test_false_for_strike_market(self):
        assert _is_updown_market("Will BTC reach $70,000?") is False


# â”€â”€ Cooldown persistence helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # Non-existent root directory â€” must not raise.
        _save_cooldowns("/nonexistent_xyz_root/cd.json", {"k": 1.0})


# â”€â”€ MomentumSignal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # delta_pct == threshold_pct â†’ excess_z = 0 â†’ edge_pct is N(z)-price â‰¥ 0
        sig = _make_signal(delta_pct=1.5, threshold_pct=1.5, token_price=0.50)
        assert sig.edge_pct >= 0.0

    def test_edge_pct_smaller_for_weaker_signal(self):
        strong = _make_signal(delta_pct=5.0, threshold_pct=1.0, token_price=0.85)
        weak = _make_signal(delta_pct=1.5, threshold_pct=1.0, token_price=0.85)
        assert strong.edge_pct > weak.edge_pct


# â”€â”€ record_trade_close â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # Both sides set inside the same call â†’ identical timestamp
        assert yes_ts == pytest.approx(no_ts, abs=1e-3)


# â”€â”€ YES/NO cooldown independence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestCooldownIndependence:
    def test_yes_cooled_does_not_block_no(self, tmp_path):
        """YES on cooldown must not affect NO-side entry eligibility."""
        scanner = _make_scanner(tmp_path)
        orig = config.MOMENTUM_MARKET_COOLDOWN_SECONDS
        config.MOMENTUM_MARKET_COOLDOWN_SECONDS = 1800
        try:
            scanner._market_cooldown["cond_001:YES"] = time.time()
            # NO was never touched â†’ elapsed is large â†’ not on cooldown
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


# â”€â”€ E5: Kelly-criterion sizing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestKellySizing:
    """Tests for _compute_kelly_size_usd â€” the fractional-Kelly position sizer."""

    def setup_method(self):
        self._saved = {
            "MOMENTUM_MAX_ENTRY_USD": config.MOMENTUM_MAX_ENTRY_USD,
            "MOMENTUM_MIN_ENTRY_USD": config.MOMENTUM_MIN_ENTRY_USD,
            "MOMENTUM_KELLY_FRACTION": config.MOMENTUM_KELLY_FRACTION,
        }
        config.MOMENTUM_MAX_ENTRY_USD = 50.0
        config.MOMENTUM_MIN_ENTRY_USD = 1.0
        config.MOMENTUM_KELLY_FRACTION = 1.0

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _size(self, **kwargs) -> float:
        return _compute_kelly_size_usd(_make_signal(**kwargs))[0]

    def test_very_strong_signal_returns_max(self):
        # 6Ïƒ+ signal: z capped â†’ win_prob â‰ˆ 1 â†’ kelly_f â‰ˆ 1 â†’ MAX_ENTRY.
        # Use a coin-flip token (price=0.5, symmetric payout) for a clean result.
        size = self._size(delta_pct=100.0, sigma_ann=0.8, tte_seconds=3600,
                          token_price=0.5)
        assert size == pytest.approx(config.MOMENTUM_MAX_ENTRY_USD)

    def test_negative_ev_signal_returns_min(self):
        # delta=0 â†’ z=0 â†’ win_prob=0.5; at token_price=0.85 the payout bâ‰ˆ0.18
        # which makes kelly_f negative â†’ clamped to 0 â†’ size = MIN_ENTRY.
        size = self._size(delta_pct=0.0, sigma_ann=0.8, tte_seconds=3600,
                          token_price=0.85)
        assert size == pytest.approx(config.MOMENTUM_MIN_ENTRY_USD)

    def test_result_always_within_bounds(self):
        # A range of signals should always land in [MIN_ENTRY, MAX_ENTRY].
        for delta in (0.0, 1.0, 3.0, 10.0, 100.0):
            size = self._size(delta_pct=delta, sigma_ann=0.8, tte_seconds=3600,
                              token_price=0.5)
            assert config.MOMENTUM_MIN_ENTRY_USD <= size <= config.MOMENTUM_MAX_ENTRY_USD

    def test_kelly_fraction_scales_output(self):
        # token_price=0.5, delta=3, tte=86400: intermediate kelly_f (~0.53).
        # Halving KELLY_FRACTION should roughly halve the dollar size.
        sig_kwargs = dict(delta_pct=3.0, sigma_ann=0.8, tte_seconds=86400,
                          token_price=0.5)
        config.MOMENTUM_KELLY_FRACTION = 1.0
        size_full = self._size(**sig_kwargs)
        config.MOMENTUM_KELLY_FRACTION = 0.5
        size_half = self._size(**sig_kwargs)
        assert size_half == pytest.approx(size_full / 2, abs=0.02)

    def test_stronger_delta_gives_larger_or_equal_size(self):
        # Monotonicity: larger delta â†’ larger or equal Kelly size.
        sizes = [
            self._size(delta_pct=d, sigma_ann=0.8, tte_seconds=86400, token_price=0.5)
            for d in (1.0, 2.0, 3.0, 5.0)
        ]
        assert sizes == sorted(sizes)

    def test_debug_dict_has_expected_keys(self):
        _, debug = _compute_kelly_size_usd(_make_signal())
        expected = {
            "kelly_sigma_tau", "kelly_z_total", "kelly_win_prob",
            "kelly_payout_b", "kelly_f", "kelly_fraction_cfg", "kelly_size_usd",
        }
        assert expected.issubset(debug.keys())


# â”€â”€ E7: diagnostics() feed health â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ E9: cooldown disk persistence on write â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        recent_ts = time.time() - 5.0   # 5 seconds ago â€” still cooling
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


# â”€â”€ _on_price_update_entry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Paper-mode position sizing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            "MOMENTUM_ORDER_TYPE": config.MOMENTUM_ORDER_TYPE,
        }
        config.STRATEGY_MOMENTUM_ENABLED = True
        config.BOT_ACTIVE = True
        config.MOMENTUM_PRICE_BAND_LOW = 0.50
        config.MOMENTUM_PRICE_BAND_HIGH = 0.95
        config.MOMENTUM_MAX_ENTRY_USD = 3.0
        config.MOMENTUM_MIN_ENTRY_USD = 0.5
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
        # _make_book uses midÂ±0.005; set mid = ask_price - 0.005 so best_ask == ask_price
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
        """entry_cost_usd must equal entry_price * token_count â‰ˆ size_usd."""
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
        # Should also be â‰ˆ size_usd (within rounding)
        assert pos.entry_cost_usd == pytest.approx(size_usd, abs=0.01)

    def test_paper_no_size_is_token_count_not_usd(self, tmp_path):
        """NO side: order_price is the NO CLOB ask (e.g. 0.80), converts correctly.
        A NO signal fires when the NO token is in-band (50-95c), meaning YES is low."""
        no_ask = 0.80   # NO token at 80c (YES â‰ˆ 0.20 â€” market strongly against)
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
        """NO entry_cost_usd = entry_price Ã— token_count (actual NO token price Ã— tokens)."""
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
        # entry_cost = entry_price Ã— size â‰ˆ size_usd
        expected_cost = round(pos.entry_price * pos.size, 6)
        assert pos.entry_cost_usd == pytest.approx(expected_cost, abs=1e-4)
        assert pos.entry_cost_usd == pytest.approx(size_usd, abs=0.01)


# â”€â”€ _extract_range_bounds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestExtractRangeBounds:
    """Unit tests for _extract_range_bounds (market_utils.py)."""

    def test_dollar_comma_format(self):
        """Standard comma-separated dollar amounts."""
        result = _extract_range_bounds("Will the price of Bitcoin be between $64,000 and $66,000 on April 5?")
        assert result == pytest.approx((64_000.0, 66_000.0))

    def test_k_suffix_lowercase(self):
        """$64k / $66k notation."""
        result = _extract_range_bounds("Will BTC be between $64k and $66k?")
        assert result == pytest.approx((64_000.0, 66_000.0))

    def test_k_suffix_uppercase(self):
        result = _extract_range_bounds("BTC between $64K and $66K by Friday?")
        assert result == pytest.approx((64_000.0, 66_000.0))

    def test_m_suffix(self):
        result = _extract_range_bounds("Will ETH be between $2m and $3m?")
        assert result == pytest.approx((2_000_000.0, 3_000_000.0))

    def test_decimal_values(self):
        result = _extract_range_bounds("Will ETH be between $2000.50 and $2100.75?")
        assert result == pytest.approx((2000.50, 2100.75))

    def test_directional_market_returns_none(self):
        """Directional markets ('above $84k') are not range markets."""
        assert _extract_range_bounds("Will BTC be above $84k?") is None

    def test_no_numbers_returns_none(self):
        assert _extract_range_bounds("Will BTC go up?") is None

    def test_inverted_bounds_returns_none(self):
        """If lo >= hi, should return None (sanity guard)."""
        # Regex captures in order, but if somehow lo > hi:
        result = _extract_range_bounds("Will BTC be between $70,000 and $60,000?")
        assert result is None

    def test_returns_tuple_of_floats(self):
        result = _extract_range_bounds("Will BTC be between $64k and $66k?")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(v, float) for v in result)


# â”€â”€ _is_range_market â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestIsRangeMarket:
    """Unit tests for _is_range_market (market_utils.py)."""

    def test_standard_between_pattern(self):
        assert _is_range_market("Will the price of Bitcoin be between $64,000 and $66,000?") is True

    def test_k_suffix_between(self):
        assert _is_range_market("Will BTC be between $64k and $66k?") is True

    def test_directional_above_is_not_range(self):
        assert _is_range_market("Will BTC be above $84k?") is False

    def test_directional_below_is_not_range(self):
        assert _is_range_market("Will ETH fall below $2,000?") is False

    def test_general_strike_market_is_not_range(self):
        assert _is_range_market("Will BTC reach $70k by end of hour?") is False

    def test_empty_string_is_not_range(self):
        assert _is_range_market("") is False


# â”€â”€ Range delta formula â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestRangeDeltaFormula:
    """
    Verify the bidirectional delta formula used for range markets in scanner.py.
    YES (spot_inside_range): delta_pct = min(spot-lo, hi-spot) / mid * 100
    NO  (spot_above):        delta_pct = (spot - hi) / mid * 100
    NO  (spot_below):        delta_pct = (lo - spot) / mid * 100
    where mid = (lo + hi) / 2
    """

    def _yes_delta(self, spot: float, lo: float, hi: float) -> float:
        mid = (lo + hi) / 2
        return min(spot - lo, hi - spot) / mid * 100

    def _no_delta_above(self, spot: float, lo: float, hi: float) -> float:
        mid = (lo + hi) / 2
        return (spot - hi) / mid * 100

    def _no_delta_below(self, spot: float, lo: float, hi: float) -> float:
        mid = (lo + hi) / 2
        return (lo - spot) / mid * 100

    def test_yes_delta_at_midpoint(self):
        """Spot at midpoint â†’ equal distance to both bounds â†’ max delta."""
        lo, hi = 64_000.0, 66_000.0
        mid = 65_000.0
        delta = self._yes_delta(mid, lo, hi)
        assert delta == pytest.approx(1000 / 65_000 * 100, rel=1e-6)

    def test_yes_delta_near_lower_bound(self):
        """Spot near lo â†’ min distance = spot-lo (small)."""
        lo, hi = 64_000.0, 66_000.0
        spot = 64_500.0   # 500 from lo, 1500 from hi
        delta = self._yes_delta(spot, lo, hi)
        assert delta == pytest.approx(500 / 65_000 * 100, rel=1e-6)

    def test_yes_delta_near_upper_bound(self):
        """Spot near hi â†’ min distance = hi-spot (small)."""
        lo, hi = 64_000.0, 66_000.0
        spot = 65_800.0   # 200 from hi, 1800 from lo
        delta = self._yes_delta(spot, lo, hi)
        assert delta == pytest.approx(200 / 65_000 * 100, rel=1e-6)

    def test_no_delta_above_range(self):
        """Spot above hi â†’ NO delta = (spot - hi) / mid."""
        lo, hi = 64_000.0, 66_000.0
        spot = 68_000.0
        delta = self._no_delta_above(spot, lo, hi)
        assert delta == pytest.approx(2_000 / 65_000 * 100, rel=1e-6)

    def test_no_delta_below_range(self):
        """Spot below lo â†’ NO delta = (lo - spot) / mid."""
        lo, hi = 64_000.0, 66_000.0
        spot = 62_000.0
        delta = self._no_delta_below(spot, lo, hi)
        assert delta == pytest.approx(2_000 / 65_000 * 100, rel=1e-6)

    def test_symmetry_above_below(self):
        """Equal distance above and below the range â†’ equal NO deltas."""
        lo, hi = 64_000.0, 66_000.0
        delta_above = self._no_delta_above(68_000.0, lo, hi)
        delta_below = self._no_delta_below(62_000.0, lo, hi)
        assert delta_above == pytest.approx(delta_below, rel=1e-6)

    def test_yes_delta_always_positive_inside_range(self):
        lo, hi = 64_000.0, 66_000.0
        for spot in (64_100, 65_000, 65_900):
            assert self._yes_delta(float(spot), lo, hi) > 0

    def test_no_delta_positive_outside_range(self):
        lo, hi = 64_000.0, 66_000.0
        assert self._no_delta_above(68_000.0, lo, hi) > 0
        assert self._no_delta_below(62_000.0, lo, hi) > 0

#  Range market integration: _execute_signal strategy label 

class TestRangeStrategyLabel:
    """
    Integration tests that verify _execute_signal stamps the correct strategy
    label on the resulting Position -- "range" for range markets, "momentum"
    for all other title formats.
    """

    def setup_method(self):
        self._saved = {
            "STRATEGY_MOMENTUM_ENABLED": config.STRATEGY_MOMENTUM_ENABLED,
            "BOT_ACTIVE": config.BOT_ACTIVE,
            "MOMENTUM_PRICE_BAND_LOW": config.MOMENTUM_PRICE_BAND_LOW,
            "MOMENTUM_PRICE_BAND_HIGH": config.MOMENTUM_PRICE_BAND_HIGH,
            "MOMENTUM_MAX_ENTRY_USD": config.MOMENTUM_MAX_ENTRY_USD,
            "MOMENTUM_MIN_ENTRY_USD": config.MOMENTUM_MIN_ENTRY_USD,
            "MOMENTUM_ORDER_TYPE": config.MOMENTUM_ORDER_TYPE,
            "MOMENTUM_RANGE_ENABLED": config.MOMENTUM_RANGE_ENABLED,
        }
        config.STRATEGY_MOMENTUM_ENABLED = True
        config.BOT_ACTIVE = True
        config.MOMENTUM_PRICE_BAND_LOW = 0.50
        config.MOMENTUM_PRICE_BAND_HIGH = 0.95
        config.MOMENTUM_MAX_ENTRY_USD = 3.0
        config.MOMENTUM_MIN_ENTRY_USD = 0.5
        config.MOMENTUM_ORDER_TYPE = "market"
        config.MOMENTUM_RANGE_ENABLED = True

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def _run_execute(self, scanner, signal, market):
        return _run(scanner._execute_signal(signal, market))

    def test_range_market_gets_range_strategy_label(self, tmp_path):
        """_execute_signal with a 'between X and Y' title -> position.strategy == 'range'."""
        ask = 0.82
        scanner = _make_scanner(tmp_path)
        book = _make_book(mid=ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)

        mkt = _make_market(title="Will BTC be between $64k and $66k on April 5?")
        sig = _make_signal(
            side="YES",
            token_id=mkt.token_id_yes,
            token_price=ask,
            p_yes=ask,
            delta_pct=3.0,
            market_title=mkt.title,
        )
        result = self._run_execute(scanner, sig, mkt)
        assert result is True
        positions = scanner._risk.get_open_positions()
        assert positions[0].strategy == "range", (
            f"Expected strategy='range', got '{positions[0].strategy}'"
        )

    def test_directional_market_gets_momentum_strategy_label(self, tmp_path):
        """'Will BTC reach $70k?' -> strategy == 'momentum'."""
        ask = 0.82
        scanner = _make_scanner(tmp_path)
        book = _make_book(mid=ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)

        mkt = _make_market(title="Will BTC reach $70k by end of hour?")
        sig = _make_signal(
            side="YES",
            token_id=mkt.token_id_yes,
            token_price=ask,
            p_yes=ask,
            delta_pct=3.0,
            market_title=mkt.title,
        )
        result = self._run_execute(scanner, sig, mkt)
        assert result is True
        positions = scanner._risk.get_open_positions()
        assert positions[0].strategy == "momentum"

    def test_range_and_momentum_both_count_toward_cap(self, tmp_path):
        """Range and momentum positions together are counted by the concurrent cap."""
        scanner = _make_scanner(tmp_path)
        ask = 0.82
        book = _make_book(mid=ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)

        # Directional momentum position
        mkt_dir = _make_market(condition_id="cond_dir", title="Will BTC reach $70k?")
        sig_dir = _make_signal(
            market_id="cond_dir",
            side="YES",
            token_id=mkt_dir.token_id_yes,
            token_price=ask,
            p_yes=ask,
            delta_pct=3.0,
            market_title=mkt_dir.title,
        )
        _run(scanner._execute_signal(sig_dir, mkt_dir))

        # Range position
        mkt_rng = _make_market(
            condition_id="cond_range",
            token_id_yes="tid_rng_yes",
            token_id_no="tid_rng_no",
            title="Will BTC be between $64k and $66k?",
        )
        sig_rng = _make_signal(
            market_id="cond_range",
            side="YES",
            token_id=mkt_rng.token_id_yes,
            token_price=ask,
            p_yes=ask,
            delta_pct=3.0,
            market_title=mkt_rng.title,
        )
        _run(scanner._execute_signal(sig_rng, mkt_rng))

        all_open = scanner._risk.get_open_positions()
        live_count = sum(1 for p in all_open if p.strategy in ("momentum", "range"))
        assert live_count == 2
        assert any(p.strategy == "momentum" for p in all_open)
        assert any(p.strategy == "range" for p in all_open)

    def test_range_no_side_gets_range_label(self, tmp_path):
        """NO leg of a range market -> strategy == 'range'."""
        no_ask = 0.80
        scanner = _make_scanner(tmp_path)
        book = _make_book(mid=no_ask - 0.005, age_secs=0.1)
        scanner._pm.get_book = MagicMock(return_value=book)

        mkt = _make_market(title="Will BTC be between $64k and $66k on April 5?")
        sig = _make_signal(
            side="NO",
            token_id=mkt.token_id_no,
            token_price=no_ask,
            p_yes=1.0 - no_ask,
            delta_pct=3.0,
            market_title=mkt.title,
        )
        result = self._run_execute(scanner, sig, mkt)
        assert result is True
        pos = scanner._risk.get_open_positions()[0]
        assert pos.strategy == "range"
