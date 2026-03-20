"""
tests/test_mispricing.py — Unit tests for mispricing.py

Run:  pytest tests/test_mispricing.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import math
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import config
config.PAPER_TRADING = True

from strategies.mispricing.math import _norm_cdf, options_implied_probability
from strategies.mispricing.signals import MispricingSignal
from strategies.mispricing.strategy import MispricingScanner
from market_data.deribit import DeribitFetcher
from risk import RiskEngine


# ── options_implied_probability ───────────────────────────────────────────────

# options_implied_probability(spot, strike, time_to_expiry_years, iv, risk_free_rate=0.05)
class TestOptionsImpliedProbability:
    def test_at_the_money_near_half(self):
        """ATM call at tte=1y, iv=0.5 should give probability ~0.5."""
        p = options_implied_probability(
            spot=100.0, strike=100.0, time_to_expiry_years=1.0, iv=0.5
        )
        # Adjusting for risk-free rate the forward is above spot, so p > 0.5
        assert 0.40 < p < 0.70

    def test_deep_itm_near_one(self):
        """spot >> strike → very high probability (option deep in the money)."""
        p = options_implied_probability(
            spot=120.0, strike=80.0, time_to_expiry_years=0.1, iv=0.3
        )
        assert p > 0.90

    def test_deep_otm_near_zero(self):
        """spot << strike → very low probability."""
        p = options_implied_probability(
            spot=60.0, strike=100.0, time_to_expiry_years=0.1, iv=0.3
        )
        assert p < 0.10

    def test_zero_tte_returns_zero(self):
        """tte=0 → early return of 0.0 (guard clause)."""
        result = options_implied_probability(
            spot=100.0, strike=100.0, time_to_expiry_years=0.0, iv=0.5
        )
        assert result == pytest.approx(0.0)

    def test_output_in_unit_interval(self):
        """Result is always a valid probability."""
        for spot in [50, 100, 200]:
            p = options_implied_probability(
                spot=spot, strike=100.0, time_to_expiry_years=0.5, iv=0.4
            )
            assert 0.0 <= p <= 1.0

    def test_higher_iv_widens_distribution(self):
        """Higher IV → probability closer to 0.5 for OTM option."""
        p_low_iv = options_implied_probability(60.0, 100.0, 0.5, iv=0.1)
        p_high_iv = options_implied_probability(60.0, 100.0, 0.5, iv=0.9)
        # High IV means more chance of reaching strike
        assert p_high_iv > p_low_iv


# ── MispricingSignal ──────────────────────────────────────────────────────────

class TestMispricingSignal:
    def _make_signal(self, pm_price, implied_prob, fee_hurdle=0.06):
        deviation = abs(pm_price - implied_prob)
        direction = "BUY_YES" if pm_price < implied_prob else "BUY_NO"
        return MispricingSignal(
            market_id="cond_001",
            market_title="BTC $120k by Dec 31",
            underlying="BTC",
            pm_price=pm_price,
            implied_prob=implied_prob,
            deviation=deviation,
            direction=direction,
            fee_hurdle=fee_hurdle,
            deribit_iv=0.80,
            deribit_instrument="BTC-27DEC24-120000-C",
            spot_price=95000.0,
            strike=120000.0,
            tte_years=0.25,
            fees_enabled=False,
            suggested_size_usd=100.0,
        )

    def test_actionable_when_deviation_exceeds_hurdle(self):
        signal = self._make_signal(pm_price=0.30, implied_prob=0.50)
        # deviation = 0.20 > fee_hurdle=0.06
        assert signal.is_actionable is True

    def test_not_actionable_when_deviation_below_hurdle(self):
        signal = self._make_signal(pm_price=0.47, implied_prob=0.50)
        # deviation = 0.03 < fee_hurdle=0.06
        assert signal.is_actionable is False

    def test_not_actionable_at_exact_hurdle(self):
        signal = self._make_signal(pm_price=0.44, implied_prob=0.50, fee_hurdle=0.06)
        # deviation = 0.06 == fee_hurdle → not strictly greater
        assert signal.is_actionable is False

    def test_summary_contains_key_fields(self):
        signal = self._make_signal(pm_price=0.30, implied_prob=0.50)
        summary = signal.summary()
        assert "0.300" in summary
        assert "0.500" in summary

    def test_direction_when_pm_too_low(self):
        """If pm_price < implied_prob → buy YES (market underpriced)."""
        signal = self._make_signal(pm_price=0.20, implied_prob=0.60)
        assert signal.direction == "BUY_YES"

    def test_direction_when_pm_too_high(self):
        """If pm_price > implied_prob → buy NO (market overpriced)."""
        signal = self._make_signal(pm_price=0.80, implied_prob=0.40)
        assert signal.direction == "BUY_NO"


# ── DeribitFetcher._find_nearest ──────────────────────────────────────────────

# DeribitFetcher._find_nearest(instruments, target_strike, target_date: datetime)
# instruments from Deribit API have keys: option_type, expiration_timestamp (ms), strike
MOCK_INSTRUMENTS = [
    {"option_type": "call", "instrument_name": "BTC-27DEC24-100000-C",
     "strike": 100000, "expiration_timestamp": 1735257600_000},
    {"option_type": "call", "instrument_name": "BTC-27DEC24-110000-C",
     "strike": 110000, "expiration_timestamp": 1735257600_000},
    {"option_type": "call", "instrument_name": "BTC-27DEC24-120000-C",
     "strike": 120000, "expiration_timestamp": 1735257600_000},
    {"option_type": "call", "instrument_name": "BTC-28MAR25-100000-C",
     "strike": 100000, "expiration_timestamp": 1743120000_000},
    {"option_type": "call", "instrument_name": "BTC-28MAR25-120000-C",
     "strike": 120000, "expiration_timestamp": 1743120000_000},
    {"option_type": "put",  "instrument_name": "BTC-27DEC24-100000-P",
     "strike": 100000, "expiration_timestamp": 1735257600_000},  # should be excluded
]
DEC_DT = datetime.fromtimestamp(1735257600, tz=timezone.utc)
MAR_DT = datetime.fromtimestamp(1743120000, tz=timezone.utc)


class TestDeribitFetcherFindNearest:
    def setup_method(self):
        self.fetcher = DeribitFetcher()

    def test_exact_match_preferred(self):
        result = self.fetcher._find_nearest(
            instruments=MOCK_INSTRUMENTS,
            target_strike=120000,
            target_date=DEC_DT,
        )
        assert result["strike"] == 120000
        assert result["expiration_timestamp"] == 1735257600_000

    def test_closest_strike_chosen(self):
        """When expiry matches, should pick nearest strike."""
        result = self.fetcher._find_nearest(
            instruments=MOCK_INSTRUMENTS,
            target_strike=115000,  # between 110k and 120k
            target_date=DEC_DT,
        )
        assert result["strike"] in [110000, 120000]

    def test_returns_none_for_empty_list(self):
        result = self.fetcher._find_nearest(
            instruments=[],
            target_strike=100000,
            target_date=DEC_DT,
        )
        assert result is None

    def test_puts_excluded(self):
        """Only call options should be returned."""
        result = self.fetcher._find_nearest(
            instruments=[{"option_type": "put", "instrument_name": "BTC-27DEC24-100000-P",
                          "strike": 100000, "expiration_timestamp": 1735257600_000}],
            target_strike=100000,
            target_date=DEC_DT,
        )
        assert result is None

    def test_fallback_to_available_expiry(self):
        """If target_date far in future, still returns best available instrument."""
        far_future = datetime.fromtimestamp(9999999999, tz=timezone.utc)
        result = self.fetcher._find_nearest(
            instruments=MOCK_INSTRUMENTS,
            target_strike=100000,
            target_date=far_future,
        )
        assert result is not None
        assert result["option_type"] == "call"


# ── MispricingScanner._extract_strike ────────────────────────────────────────

class TestExtractStrike:
    def setup_method(self):
        pm = MagicMock()
        hl = MagicMock()
        risk = RiskEngine()
        # MispricingScanner(pm, hl, signal_callback, scan_interval=300)
        async def _dummy_callback(signal): pass
        self.scanner = MispricingScanner(pm, hl, _dummy_callback)

    # _extract_strike(title, spot) — spot used for sanity check (>1% of spot)
    SPOT = 95000.0   # simulate BTC spot

    def test_k_suffix(self):
        assert self.scanner._extract_strike("BTC $120k by Dec 31", self.SPOT) == pytest.approx(120000.0)

    def test_m_suffix(self):
        assert self.scanner._extract_strike("BTC $1.2M milestone", self.SPOT) == pytest.approx(1200000.0)

    def test_comma_formatted(self):
        assert self.scanner._extract_strike("BTC $120,000 by year end", self.SPOT) == pytest.approx(120000.0)

    def test_plain_number(self):
        assert self.scanner._extract_strike("BTC reaches $90000", self.SPOT) == pytest.approx(90000.0)

    def test_no_match_returns_none(self):
        assert self.scanner._extract_strike("Will it rain tomorrow?", self.SPOT) is None

    def test_decimal_k(self):
        # $80k → 80000 (within 1% spot check at spot=95000)
        result = self.scanner._extract_strike("BTC $80k target", self.SPOT)
        assert result == pytest.approx(80000.0)
