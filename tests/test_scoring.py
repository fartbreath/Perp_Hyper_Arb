"""
tests/test_scoring.py — Unit + integration tests for strategies.scoring and the
lifecycle / volume improvements in MakerStrategy._evaluate_signal.

Covers:
  • _tte_pts_maker  — bucket lifecycle zones, cross-type symmetry, absolute fallback
  • _volume_pts     — type-aware log-scale caps, dust floor, monotonicity
  • score_maker     — component weights, capital-velocity bonus, bounds
  • MakerStrategy._evaluate_signal
      – lifecycle TTE gate  (MAKER_EXIT_TTE_FRAC)
      – lifecycle-scaled volume gate

Run:  pytest tests/test_scoring.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import math
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

import config
config.PAPER_TRADING = True

from strategies.scoring import (
    _tte_pts_maker,
    _volume_pts,
    score_maker,
    _MARKET_TYPE_DURATION_SECS,
    _VOLUME_TYPE_CAP,
    _VOLUME_DUST_FLOOR,
)
from strategies.maker.signals import MakerSignal
from strategies.maker.strategy import MakerStrategy
from risk import RiskEngine


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_signal(
    *,
    mid: float = 0.50,
    effective_edge: float = 0.02,
    market_type: str = "bucket_1h",
) -> MakerSignal:
    """Minimal MakerSignal for scoring-only tests."""
    return MakerSignal(
        market_id="test-market-001",
        token_id="tok_yes_001",
        underlying="BTC",
        mid=mid,
        bid_price=round(mid - 0.02, 4),
        ask_price=round(mid + 0.02, 4),
        half_spread=0.02,
        effective_edge=effective_edge,
        market_type=market_type,
    )


def _make_strategy() -> MakerStrategy:
    """Lightweight MakerStrategy backed by MagicMock PM and a real RiskEngine."""
    pm = MagicMock()
    pm.on_price_change = MagicMock()
    pm.get_markets = MagicMock(return_value={})
    pm.cancel_order = AsyncMock(return_value=True)
    pm.place_limit = AsyncMock(return_value="order-001")
    pm.get_mid = MagicMock(return_value=0.50)
    pm.get_book = MagicMock(return_value=None)
    pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))
    hl = MagicMock()
    hl.on_bbo_update = MagicMock()
    return MakerStrategy(pm, hl, RiskEngine())


def _make_market(
    *,
    mid: float = 0.50,
    volume_24hr: float = 5_000.0,
    market_type: str = "bucket_1h",
    tte_offset_secs: float = 1800.0,   # default mid-lifecycle of bucket_1h
    max_incentive_spread: float = 0.04,
    is_fee_free: bool = False,
    rebate_pct: float = 0.20,
):
    """Synthetic PMMarket-like MagicMock for _evaluate_signal integration tests."""
    m = MagicMock()
    m.condition_id = "cond_scoring_001"
    m.token_id_yes = "tok_yes_001"
    m.underlying = "BTC"
    m.is_fee_free = is_fee_free
    m.fees_enabled = not is_fee_free
    m.rebate_pct = rebate_pct
    m.volume_24hr = volume_24hr
    m.max_incentive_spread = max_incentive_spread
    m.tick_size = 0.01
    m.market_type = market_type
    m.discovered_at = time.time() - 9000  # 2.5 h old → normal spread, not new-market wide
    m.end_date = datetime.fromtimestamp(time.time() + tte_offset_secs, tz=timezone.utc)
    return m


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ═════════════════════════════════════════════════════════════════════════════
# _tte_pts_maker — Bucket lifecycle scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestTtePtsMakerBucketLifecycle:
    """Lifecycle-relative scoring for bucket_* market types."""

    def test_imminent_expiry_scores_zero(self):
        """frac < 0.10 → 0 pts (gamma spike, adverse selection)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.05, "bucket_1h") == pytest.approx(0.0, abs=0.01)

    def test_frac_at_10pct_boundary_scores_zero(self):
        """Exactly 10% of life remaining → bottom of linear ramp → 0 pts."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.10, "bucket_1h") == pytest.approx(0.0, abs=0.01)

    def test_final_approach_midpoint_scores_6(self):
        """frac = 0.175 (midpoint of [0.10, 0.25] ramp) → 6 pts.
        Linear: (0.175-0.10)/(0.25-0.10) * 12 = 0.5 * 12 = 6."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        pts = _tte_pts_maker(dur * 0.175, "bucket_1h")
        assert pts == pytest.approx(6.0, abs=0.2)

    def test_frac_25pct_scores_12(self):
        """frac = 0.25 → 12 pts (top of final-approach ramp)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.25, "bucket_1h") == pytest.approx(12.0, abs=0.2)

    def test_frac_50pct_scores_25(self):
        """frac = 0.50 → 25 pts (entering sweet-spot zone)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.50, "bucket_1h") == pytest.approx(25.0, abs=0.01)

    def test_frac_65pct_scores_25(self):
        """frac = 0.65 (mid of sweet-spot [0.50, 0.85]) → 25 pts."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.65, "bucket_1h") == pytest.approx(25.0, abs=0.01)

    def test_frac_85pct_scores_25(self):
        """frac = 0.85 → 25 pts (top edge of sweet-spot)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 0.85, "bucket_1h") == pytest.approx(25.0, abs=0.01)

    def test_brand_new_frac_100pct_scores_22(self):
        """frac = 1.0 (just opened) → 22 pts (thin-book discount)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 1.0, "bucket_1h") == pytest.approx(22.0, abs=0.1)

    def test_brand_new_zone_between_22_and_25(self):
        """frac = 0.90 (in brand-new zone [0.85, 1.0]) → strictly between 22 and 25."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        pts = _tte_pts_maker(dur * 0.90, "bucket_1h")
        assert 22.0 < pts < 25.0, f"Expected 22 < pts < 25, got {pts}"

    def test_over_full_duration_clamps_to_22(self):
        """TTE > canonical duration → frac clamped to 1.0 → 22 pts (no crash)."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        assert _tte_pts_maker(dur * 1.5, "bucket_1h") == pytest.approx(22.0, abs=0.1)

    def test_zero_tte_scores_zero(self):
        """TTE = 0 → frac = 0 < 0.10 → 0 pts."""
        assert _tte_pts_maker(0.0, "bucket_1h") == pytest.approx(0.0)

    def test_negative_tte_clamps_to_zero(self):
        """Negative TTE (expired market) → 0 pts without error."""
        assert _tte_pts_maker(-100.0, "bucket_1h") == pytest.approx(0.0)

    def test_bucket_daily_lifecycle_zones(self):
        """bucket_daily (86400s duration) zones are identical by fraction."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        assert _tte_pts_maker(dur * 0.05, "bucket_daily") == pytest.approx(0.0, abs=0.01)
        assert _tte_pts_maker(dur * 0.65, "bucket_daily") == pytest.approx(25.0, abs=0.01)
        assert _tte_pts_maker(dur * 1.00, "bucket_daily") == pytest.approx(22.0, abs=0.1)

    def test_ramp_lower_half_12_to_25(self):
        """frac inside [0.25, 0.50] ramps from 12 → 25 pts."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        pts_25  = _tte_pts_maker(dur * 0.250, "bucket_1h")  # 12.0 pts
        pts_375 = _tte_pts_maker(dur * 0.375, "bucket_1h")  # ~18.5 pts (midpoint)
        pts_50  = _tte_pts_maker(dur * 0.500, "bucket_1h")  # 25.0 pts
        assert pts_25  == pytest.approx(12.0, abs=0.2)
        assert pts_375 == pytest.approx(18.5, abs=0.5)
        assert pts_50  == pytest.approx(25.0, abs=0.01)


# ═════════════════════════════════════════════════════════════════════════════
# _tte_pts_maker — Cross-type lifecycle symmetry
# ═════════════════════════════════════════════════════════════════════════════

class TestTtePtsMakerSymmetry:
    """Equivalent lifecycle fractions score identically across bucket types."""

    def test_fresh_1h_equals_fresh_daily(self):
        """bucket_1h at frac=1.0 must score identically to bucket_daily at frac=1.0."""
        dur_1h    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        dur_daily = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        pts_1h    = _tte_pts_maker(dur_1h    * 1.0, "bucket_1h")
        pts_daily = _tte_pts_maker(dur_daily * 1.0, "bucket_daily")
        assert pts_1h == pytest.approx(pts_daily, abs=0.01)

    def test_sweet_spot_5m_equals_sweet_spot_daily(self):
        """frac=0.65 → 25 pts in both bucket_5m and bucket_daily."""
        dur_5m    = _MARKET_TYPE_DURATION_SECS["bucket_5m"]
        dur_daily = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        assert _tte_pts_maker(dur_5m    * 0.65, "bucket_5m")    == pytest.approx(25.0, abs=0.01)
        assert _tte_pts_maker(dur_daily * 0.65, "bucket_daily") == pytest.approx(25.0, abs=0.01)

    def test_absolute_tte_illusion_corrected(self):
        """
        Daily in its FINAL HOUR (TTE=3600s) scores 0 — expiry risk.
        bucket_1h at TTE=3600s (identical absolute time) scores 22 — brand-new.
        The lifecycle model correctly distinguishes these two cases that the old
        absolute-time logic would have treated identically (both at ~4% of daily
        life → 0 pts).
        """
        pts_daily_final_hour = _tte_pts_maker(3600.0, "bucket_daily")  # frac ≈ 0.042 < 0.10 → 0
        pts_1h_fresh         = _tte_pts_maker(3600.0, "bucket_1h")      # frac = 1.0 → 22
        assert pts_daily_final_hour == pytest.approx(0.0, abs=0.01)
        assert pts_1h_fresh         == pytest.approx(22.0, abs=0.2)
        assert pts_1h_fresh > pts_daily_final_hour


# ═════════════════════════════════════════════════════════════════════════════
# _tte_pts_maker — Absolute fallback (milestone / unknown market types)
# ═════════════════════════════════════════════════════════════════════════════

class TestTtePtsMakerAbsoluteFallback:
    """Milestone/unknown market types use original absolute TTE logic."""

    def test_under_6h_scores_zero(self):
        assert _tte_pts_maker(3 * 3600.0, "")          == pytest.approx(0.0)
        assert _tte_pts_maker(3 * 3600.0, "milestone") == pytest.approx(0.0)

    def test_1day_scores_25(self):
        assert _tte_pts_maker(86_400.0, "") == pytest.approx(25.0, abs=0.2)

    def test_3day_sweet_spot_scores_25(self):
        assert _tte_pts_maker(3 * 86_400.0, "") == pytest.approx(25.0, abs=0.01)

    def test_5day_still_scores_25(self):
        """5 days is the top of the sweet-spot; decay starts after."""
        assert _tte_pts_maker(5 * 86_400.0, "") == pytest.approx(25.0, abs=0.01)

    def test_decay_starts_after_5_days(self):
        """7 days < 25 pts; 10 days < 7 days (monotone decay)."""
        pts_7d  = _tte_pts_maker(7  * 86_400.0, "")
        pts_10d = _tte_pts_maker(10 * 86_400.0, "")
        assert pts_7d  < 25.0
        assert pts_10d < pts_7d

    def test_over_14day_scores_2(self):
        assert _tte_pts_maker(20 * 86_400.0, "") == pytest.approx(2.0, abs=0.01)

    def test_zero_tte_fallback_zero(self):
        assert _tte_pts_maker(0.0, "")  == pytest.approx(0.0)
        assert _tte_pts_maker(0.0, "milestone") == pytest.approx(0.0)


# ═════════════════════════════════════════════════════════════════════════════
# _volume_pts — Type-aware log-scale volume scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestVolumePts:
    """Type-aware log-scale volume component."""

    MAX_PTS = 35.0

    def test_zero_volume_scores_zero(self):
        assert _volume_pts(0.0, self.MAX_PTS) == pytest.approx(0.0)

    def test_below_dust_floor_scores_zero(self):
        assert _volume_pts(_VOLUME_DUST_FLOOR - 1, self.MAX_PTS) == pytest.approx(0.0)

    def test_at_type_cap_scores_max(self):
        """Volume exactly at the per-type cap → log_frac = 1.0 → max_pts."""
        cap_5m = _VOLUME_TYPE_CAP["bucket_5m"]
        assert _volume_pts(cap_5m, self.MAX_PTS, "bucket_5m") == pytest.approx(self.MAX_PTS, abs=0.01)

    def test_above_type_cap_clamps_to_max(self):
        """Volume 10× the cap is still clamped to max_pts."""
        cap_daily = _VOLUME_TYPE_CAP["bucket_daily"]
        assert _volume_pts(cap_daily * 10, self.MAX_PTS, "bucket_daily") == pytest.approx(self.MAX_PTS, abs=0.01)

    def test_bucket_5m_saturates_at_15k(self):
        """bucket_5m cap is $15k; at that volume it must score full marks."""
        assert _volume_pts(15_000.0, self.MAX_PTS, "bucket_5m") == pytest.approx(self.MAX_PTS, abs=0.01)

    def test_bucket_daily_saturates_at_250k(self):
        """bucket_daily cap is $250k."""
        assert _volume_pts(250_000.0, self.MAX_PTS, "bucket_daily") == pytest.approx(self.MAX_PTS, abs=0.01)

    def test_same_volume_scores_higher_in_small_bucket(self):
        """$15k raw volume: saturates bucket_5m (35 pts) but not bucket_daily (~27 pts)."""
        pts_5m    = _volume_pts(15_000.0, self.MAX_PTS, "bucket_5m")
        pts_daily = _volume_pts(15_000.0, self.MAX_PTS, "bucket_daily")
        assert pts_5m    == pytest.approx(self.MAX_PTS, abs=0.01)
        assert pts_daily < self.MAX_PTS * 0.80

    def test_unknown_type_uses_100k_default_cap(self):
        """Unknown market_type falls back to $100k cap."""
        from strategies.scoring import _VOLUME_DEFAULT_CAP
        assert _volume_pts(_VOLUME_DEFAULT_CAP, self.MAX_PTS, "unknown_xtype") == pytest.approx(self.MAX_PTS, abs=0.01)

    def test_monotonically_increasing_with_volume(self):
        """Increasing volume always produces non-decreasing score (up to cap)."""
        volumes = [500, 1_000, 5_000, 20_000, 60_000]
        prev = -1.0
        for vol in volumes:
            pts = _volume_pts(vol, self.MAX_PTS, "bucket_1h")
            assert pts > prev, f"Score not strictly increasing: {vol} → {pts} <= {prev}"
            prev = pts

    def test_log_scale_nonlinear_growth(self):
        """Doubling volume produces less than double score (log scale)."""
        pts_1k = _volume_pts(1_000.0, self.MAX_PTS, "bucket_1h")
        pts_2k = _volume_pts(2_000.0, self.MAX_PTS, "bucket_1h")
        pts_4k = _volume_pts(4_000.0, self.MAX_PTS, "bucket_1h")
        # increment 1k→2k should be larger than 2k→4k (diminishing returns)
        assert (pts_2k - pts_1k) > 0
        assert (pts_4k - pts_2k) < (pts_2k - pts_1k)


# ═════════════════════════════════════════════════════════════════════════════
# score_maker — Integration: all four components + capital velocity bonus
# ═════════════════════════════════════════════════════════════════════════════

class TestScoreMakerIntegration:
    """score_maker combines edge + volume + TTE + balance + cap_vel, normalised to 100."""

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _perfect_params():
        """Return (sig, volume, tte, market_type) that should produce score=100."""
        dur  = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        cap  = _VOLUME_TYPE_CAP["bucket_1h"]
        sig  = _make_signal(mid=0.50, effective_edge=0.02, market_type="bucket_1h")
        return sig, cap, dur * 0.65, "bucket_1h"  # sweet-spot TTE

    # ── Perfect signal ────────────────────────────────────────────────────────
    def test_perfect_signal_scores_100(self):
        """Max edge + cap volume + sweet-spot TTE + balanced mid → 100."""
        sig, vol, tte, mtype = self._perfect_params()
        assert score_maker(sig, vol, tte, mtype) == pytest.approx(100.0, abs=1.0)

    # ── Score bounds ──────────────────────────────────────────────────────────
    def test_score_never_exceeds_100(self):
        """Pathological inputs (huge edge, infinite volume, huge TTE) → still ≤ 100."""
        sig = _make_signal(mid=0.50, effective_edge=1.0)   # 100× max edge
        assert score_maker(sig, 1e12, 1e9, "bucket_daily") <= 100.0

    def test_score_never_below_zero(self):
        """Worst-case inputs → score ≥ 0."""
        sig = _make_signal(mid=0.50, effective_edge=0.0)
        assert score_maker(sig, 0.0, 0.0, "") >= 0.0

    # ── Volume component ──────────────────────────────────────────────────────
    def test_zero_volume_reduces_score_materially(self):
        """Dust volume zeroes the fill-probability component (35 out of 105 raw pts)."""
        sig, cap, tte, mtype = self._perfect_params()
        full_score = score_maker(sig, cap,  tte, mtype)
        dust_score = score_maker(sig, 0.0,  tte, mtype)
        assert dust_score < full_score * 0.70, (
            f"Zero-volume score {dust_score} should be < 70% of perfect {full_score}"
        )

    # ── TTE component ─────────────────────────────────────────────────────────
    def test_expired_tte_reduces_score(self):
        """TTE=0 → tte_pts=0, cap_vel=0 → well below sweet-spot score."""
        sig, cap, tte, mtype = self._perfect_params()
        sweet_score   = score_maker(sig, cap, tte,  mtype)
        expired_score = score_maker(sig, cap, 0.0,  mtype)
        assert expired_score < sweet_score * 0.90

    def test_imminent_bucket_expiry_reduces_score(self):
        """frac=0.05 → tte_pts=0 → lower than sweet-spot."""
        dur  = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        cap  = _VOLUME_TYPE_CAP["bucket_1h"]
        sig  = _make_signal(mid=0.50, effective_edge=0.02, market_type="bucket_1h")
        sweet_score   = score_maker(sig, cap, dur * 0.65, "bucket_1h")
        expiry_score  = score_maker(sig, cap, dur * 0.05, "bucket_1h")
        assert expiry_score < sweet_score

    # ── Price balance component ───────────────────────────────────────────────
    def test_unbalanced_mid_reduces_score(self):
        """mid=0.10 (deep OTM) → lower balance component than mid=0.50."""
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        cap = _VOLUME_TYPE_CAP["bucket_1h"]
        tte = dur * 0.65
        balanced   = score_maker(_make_signal(mid=0.50), cap, tte, "bucket_1h")
        unbalanced = score_maker(_make_signal(mid=0.10), cap, tte, "bucket_1h")
        assert unbalanced < balanced

    # ── Edge component ────────────────────────────────────────────────────────
    def test_tiny_edge_reduces_score(self):
        """effective_edge=0.001 is only 5% of max → low total score."""
        cap = _VOLUME_TYPE_CAP["bucket_1h"]
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        tte = dur * 0.65
        tiny_score    = score_maker(_make_signal(mid=0.50, effective_edge=0.001), cap, tte, "bucket_1h")
        perfect_score = score_maker(_make_signal(mid=0.50, effective_edge=0.02),  cap, tte, "bucket_1h")
        assert tiny_score < perfect_score * 0.80

    # ── Capital velocity bonus ────────────────────────────────────────────────
    def test_capital_velocity_fires_in_sweet_zone(self):
        """High volume + sweet-spot TTE → cap_vel = sqrt(1×1)*5 = 5 extra pts.
        Compare sweet-spot vs imminent-expiry (where cap_vel is ~0)."""
        sig = _make_signal(mid=0.50, effective_edge=0.02, market_type="bucket_daily")
        cap = _VOLUME_TYPE_CAP["bucket_daily"]
        dur = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        sweet_score  = score_maker(sig, cap, dur * 0.65, "bucket_daily")
        expiry_score = score_maker(sig, cap, dur * 0.05, "bucket_daily")
        assert sweet_score > expiry_score

    def test_capital_velocity_diminished_with_zero_volume(self):
        """cap_vel = sqrt(vol_frac × tte_vel_frac)*5; zero volume → vol_frac floored at dust.
        Without fill volume the bonus drops significantly."""
        sig = _make_signal(mid=0.50, effective_edge=0.02, market_type="bucket_1h")
        dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        tte = dur * 0.65
        no_vol_score   = score_maker(sig, 0.0,                       tte, "bucket_1h")
        full_vol_score = score_maker(sig, _VOLUME_TYPE_CAP["bucket_1h"], tte, "bucket_1h")
        assert no_vol_score < full_vol_score

    def test_milestone_capital_velocity_1_to_5_day_sweet_spot(self):
        """Milestone market: cap_vel sweet zone is 1–5 days absolute TTE."""
        sig = _make_signal(mid=0.50, effective_edge=0.02, market_type="")
        from strategies.scoring import _VOLUME_DEFAULT_CAP
        cap = _VOLUME_DEFAULT_CAP
        sweet_score  = score_maker(sig, cap, 3 * 86_400.0, "")   # 3d — sweet
        cold_score   = score_maker(sig, cap, 30 * 86_400.0, "")  # 30d — outside
        assert sweet_score > cold_score


# ═════════════════════════════════════════════════════════════════════════════
# _evaluate_signal — Lifecycle TTE gate (MAKER_EXIT_TTE_FRAC)
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalLifecycleGate:
    """_evaluate_signal returns None when lifecycle fraction < MAKER_EXIT_TTE_FRAC."""

    def test_bucket_below_exit_frac_returns_none(self):
        """frac = 0.05 < MAKER_EXIT_TTE_FRAC = 0.10 → lifecycle gate fires → None."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 0.05,   # 5% of life remaining
            volume_24hr=50_000.0,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None, (
            f"Expected None for frac=0.05 < MAKER_EXIT_TTE_FRAC={config.MAKER_EXIT_TTE_FRAC}"
        )

    def test_bucket_at_9pct_returns_none(self):
        """frac = 0.09 — just under threshold → still gated."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 0.09,
            volume_24hr=50_000.0,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None

    def test_bucket_well_above_exit_frac_not_gated_by_tte(self):
        """frac = 0.65 (sweet-spot) — TTE gate must NOT fire.
        Result may still be None from other checks, but no exception raised."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 0.65,
            volume_24hr=50_000.0,
            max_incentive_spread=0.04,
        )
        strategy._pm.get_mid.return_value = 0.50
        # Just verifying no crash; TTE is not the reason for any None return here.
        strategy._evaluate_signal(market, 0.50)  # must not raise

    def test_different_bucket_types_gate_correctly(self):
        """bucket_daily at 5% of 86400s = 4320s → also returns None."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        market = _make_market(
            market_type="bucket_daily",
            tte_offset_secs=dur * 0.05,
            volume_24hr=200_000.0,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None

    def test_milestone_market_not_subject_to_lifecycle_gate(self):
        """Milestone/unknown market → uses MAKER_EXIT_HOURS absolute gate (0.0 from override).
        With MAKER_EXIT_HOURS=0.0 and a valid TTE, the TTE gate should not fire."""
        strategy = _make_strategy()
        market = _make_market(
            market_type="",              # milestone path
            tte_offset_secs=86_400.0,   # 1 day
            volume_24hr=50_000.0,
            max_incentive_spread=0.04,
        )
        strategy._pm.get_mid.return_value = 0.50
        # With MAKER_EXIT_HOURS=0.0 (override), TTE gate is: tte_secs >= 0 → never fires
        strategy._evaluate_signal(market, 0.50)  # must not raise


# ═════════════════════════════════════════════════════════════════════════════
# _evaluate_signal — Entry TTE cooldown (MAKER_ENTRY_TTE_FRAC)
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalEntryTteCooldown:
    """
    MAKER_ENTRY_TTE_FRAC blocks quoting for brand-new buckets (frac > 1 − entry_frac).
    The BTC 8PM-8:05PM adverse fill happened at T+2s (frac=0.993) — this gate
    would have blocked it.
    """

    def test_brand_new_bucket_blocked(self):
        """frac = 0.99 > 1 − 0.10 = 0.90 → opening cooldown fires → None."""
        orig = config.MAKER_ENTRY_TTE_FRAC
        config.MAKER_ENTRY_TTE_FRAC = 0.10
        try:
            strategy = _make_strategy()
            dur = _MARKET_TYPE_DURATION_SECS["bucket_5m"]
            market = _make_market(
                market_type="bucket_5m",
                tte_offset_secs=dur * 0.99,   # 99% of life remaining (just opened)
                volume_24hr=5_000.0,
                max_incentive_spread=0.04,
            )
            strategy._pm.get_mid.return_value = 0.50
            result = strategy._evaluate_signal(market, 0.50)
            assert result is None, (
                "Brand-new bucket (frac=0.99) should be blocked by entry TTE cooldown"
            )
        finally:
            config.MAKER_ENTRY_TTE_FRAC = orig

    def test_at_cooldown_boundary_blocked(self):
        """frac = 0.91 > 1 − 0.10 = 0.90 → still in cooldown → None."""
        orig = config.MAKER_ENTRY_TTE_FRAC
        config.MAKER_ENTRY_TTE_FRAC = 0.10
        try:
            strategy = _make_strategy()
            dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
            market = _make_market(
                market_type="bucket_1h",
                tte_offset_secs=dur * 0.91,
                volume_24hr=50_000.0,
                max_incentive_spread=0.04,
            )
            strategy._pm.get_mid.return_value = 0.50
            result = strategy._evaluate_signal(market, 0.50)
            assert result is None, "frac=0.91 still inside cooldown zone"
        finally:
            config.MAKER_ENTRY_TTE_FRAC = orig

    def test_past_cooldown_not_blocked(self):
        """frac = 0.85 < 1 − 0.10 = 0.90 → cooldown elapsed → gate does not fire."""
        orig = config.MAKER_ENTRY_TTE_FRAC
        config.MAKER_ENTRY_TTE_FRAC = 0.10
        try:
            strategy = _make_strategy()
            dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
            market = _make_market(
                market_type="bucket_1h",
                tte_offset_secs=dur * 0.85,
                volume_24hr=50_000.0,
                max_incentive_spread=0.04,
            )
            strategy._pm.get_mid.return_value = 0.50
            # frac=0.85 is exactly at sweet-spot; entry cooldown must not fire.
            # Result may be None for other reasons; just verify no exception.
            try:
                strategy._evaluate_signal(market, 0.50)
            except Exception as exc:
                pytest.fail(f"Entry gate raised unexpectedly at frac=0.85: {exc}")
        finally:
            config.MAKER_ENTRY_TTE_FRAC = orig

    def test_disabled_when_zero(self):
        """MAKER_ENTRY_TTE_FRAC=0 → opening cooldown disabled; brand-new bucket not blocked."""
        orig = config.MAKER_ENTRY_TTE_FRAC
        config.MAKER_ENTRY_TTE_FRAC = 0.0
        try:
            strategy = _make_strategy()
            dur = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
            market = _make_market(
                market_type="bucket_1h",
                tte_offset_secs=dur * 0.99,   # brand-new
                volume_24hr=50_000.0,
                max_incentive_spread=0.04,
            )
            strategy._pm.get_mid.return_value = 0.50
            # With disabled gate, no entry-cooldown None should be returned for this reason.
            try:
                strategy._evaluate_signal(market, 0.50)
            except Exception as exc:
                pytest.fail(f"Unexpected exception with disabled cooldown: {exc}")
        finally:
            config.MAKER_ENTRY_TTE_FRAC = orig

    def test_milestone_not_subject_to_entry_cooldown(self):
        """Milestone market uses absolute TTE path — entry cooldown does not apply."""
        orig = config.MAKER_ENTRY_TTE_FRAC
        config.MAKER_ENTRY_TTE_FRAC = 0.10
        try:
            strategy = _make_strategy()
            market = _make_market(
                market_type="",         # milestone path
                tte_offset_secs=86_400.0,
                volume_24hr=50_000.0,
                max_incentive_spread=0.04,
            )
            strategy._pm.get_mid.return_value = 0.50
            try:
                strategy._evaluate_signal(market, 0.50)
            except Exception as exc:
                pytest.fail(f"Unexpected exception for milestone market: {exc}")
        finally:
            config.MAKER_ENTRY_TTE_FRAC = orig


# ═════════════════════════════════════════════════════════════════════════════
# _evaluate_signal — Lifecycle-scaled volume gate
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluateSignalVolumeGate:
    """Volume gate threshold scales with fraction-of-life elapsed for bucket markets."""

    def test_fresh_bucket_passes_with_zero_volume(self):
        """Just-opened bucket_1h: fraction_elapsed = 0 → required_volume = 0 → passes."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 1.0,   # just opened (frac_remaining=1.0, elapsed=0)
            volume_24hr=0.0,
            max_incentive_spread=0.04,
        )
        strategy._pm.get_mid.return_value = 0.50
        # Volume gate: required_volume = MAKER_MIN_VOLUME_24HR * 0.0 = 0.0
        # 0.0 >= 0.0 → passes.  Other checks may still filter — just must not raise.
        try:
            strategy._evaluate_signal(market, 0.50)
        except Exception as e:
            pytest.fail(f"Volume gate raised unexpectedly for fresh bucket: {e}")

    def test_half_elapsed_with_zero_volume_returns_none(self):
        """50% elapsed → required = MAKER_MIN_VOLUME_24HR × 0.50; vol=0 → None."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 0.50,   # 50% life remaining → 50% elapsed
            volume_24hr=0.0,
            max_incentive_spread=0.04,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None, (
            "Expected None: volume=0 with 50% elapsed should fail volume gate"
        )

    def test_volume_proportional_pass_at_half_elapsed(self):
        """50% elapsed: provide exactly MIN_VOLUME × 0.50 USD volume → volume gate passes."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_1h"]
        # required = config.MAKER_MIN_VOLUME_24HR * 0.50
        # set volume just above that threshold
        required = config.MAKER_MIN_VOLUME_24HR * 0.50
        market = _make_market(
            market_type="bucket_1h",
            tte_offset_secs=dur * 0.50,
            volume_24hr=required + 1.0,   # just above threshold
            max_incentive_spread=0.04,
        )
        strategy._pm.get_mid.return_value = 0.50
        # Should not be gated by volume. Other filters may still return None.
        try:
            strategy._evaluate_signal(market, 0.50)
        except Exception as e:
            pytest.fail(f"Raised unexpectedly when volume passes gate: {e}")

    def test_milestone_always_needs_full_volume(self):
        """Milestone market: no lifecycle fraction → required = MAKER_MIN_VOLUME_24HR."""
        strategy = _make_strategy()
        market = _make_market(
            market_type="",           # milestone → no duration lookup
            tte_offset_secs=86_400.0,
            volume_24hr=0.0,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None, "Milestone with zero volume must be gated out"

    def test_volume_gate_with_daily_bucket(self):
        """bucket_daily at 75% elapsed (6h remaining): required = MIN_VOLUME × 0.75."""
        strategy = _make_strategy()
        dur    = _MARKET_TYPE_DURATION_SECS["bucket_daily"]
        market = _make_market(
            market_type="bucket_daily",
            tte_offset_secs=dur * 0.25,   # 25% remaining → 75% elapsed
            volume_24hr=0.0,
        )
        strategy._pm.get_mid.return_value = 0.50
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None, (
            "bucket_daily with 75% elapsed and zero volume must be gated out"
        )
