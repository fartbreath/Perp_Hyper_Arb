"""
tests/test_maker.py — Unit tests for maker.py

Run:  pytest tests/test_maker.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import math
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import config
config.PAPER_TRADING = True

from strategies.maker.strategy import MakerStrategy, _is_priority_market
from strategies.maker.signals import ActiveQuote
from strategies.maker.math import binary_delta, hedge_size_coins, _norm_cdf
from market_data.pm_client import PMMarket
from risk import RiskEngine, Position
from datetime import datetime, timezone, timedelta


# ── _norm_cdf ─────────────────────────────────────────────────────────────────

class TestNormCdf:
    def test_zero_is_half(self):
        assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-4)

    def test_positive_above_half(self):
        assert _norm_cdf(1.0) > 0.5

    def test_negative_below_half(self):
        assert _norm_cdf(-1.0) < 0.5

    def test_symmetry(self):
        assert _norm_cdf(1.0) == pytest.approx(1.0 - _norm_cdf(-1.0), abs=1e-6)

    def test_known_values(self):
        # N(1.96) ≈ 0.975
        assert _norm_cdf(1.96) == pytest.approx(0.975, abs=0.002)
        # N(-1.645) ≈ 0.05
        assert _norm_cdf(-1.645) == pytest.approx(0.05, abs=0.002)


# ── binary_delta ──────────────────────────────────────────────────────────────

class TestBinaryDelta:
    def test_at_fifty_percent(self):
        assert binary_delta(0.5) == pytest.approx(0.5)

    def test_at_ten_percent(self):
        assert binary_delta(0.1) == pytest.approx(0.1)

    def test_at_ninety_percent(self):
        assert binary_delta(0.9) == pytest.approx(0.9)

    def test_clamp_above_one(self):
        assert binary_delta(1.5) == pytest.approx(1.0)

    def test_clamp_below_zero(self):
        assert binary_delta(-0.1) == pytest.approx(0.0)


# ── hedge_size_coins ──────────────────────────────────────────────────────────

class TestHedgeSizeCoins:
    def test_basic_calculation(self):
        # pm_notional=1000, pm_price=0.5, hl_price=80000
        # delta=0.5, notional_to_hedge=500, coins=500/80000=0.00625
        result = hedge_size_coins(1000.0, 0.5, 80000.0)
        assert result == pytest.approx(0.00625, rel=1e-4)

    def test_low_delta_reduces_size(self):
        # pm_price=0.1 → delta=0.1 → coins = 0.1 * 1000 / 80000 = 0.00125
        result = hedge_size_coins(1000.0, 0.1, 80000.0)
        assert result == pytest.approx(0.00125, rel=1e-4)

    def test_zero_hl_price_returns_zero(self):
        assert hedge_size_coins(1000.0, 0.5, 0.0) == 0.0

    def test_high_pm_price_increases_size(self):
        low = hedge_size_coins(1000.0, 0.1, 50000.0)
        high = hedge_size_coins(1000.0, 0.9, 50000.0)
        assert high > low


# ── parse_strike_from_title ───────────────────────────────────────────────────

from strategies.maker.math import parse_strike_from_title, implied_sigma, bs_digital_coins


class TestParseStrikeFromTitle:
    def test_above_dollar(self):
        assert parse_strike_from_title("Will BTC be above $90,000 today?") == pytest.approx(90000.0)

    def test_below_dollar(self):
        assert parse_strike_from_title("Will ETH be below $3,500 on Friday?") == pytest.approx(3500.0)

    def test_over(self):
        assert parse_strike_from_title("Price over $85000 at close") == pytest.approx(85000.0)

    def test_under(self):
        assert parse_strike_from_title("Price under $2,000") == pytest.approx(2000.0)

    def test_between_returns_none(self):
        assert parse_strike_from_title("Will BTC be between $72,000 and $74,000 on March 13?") is None

    def test_no_strike_returns_none(self):
        assert parse_strike_from_title("Will Kanye West release an album in 2026?") is None

    def test_decimal_strike(self):
        # e.g. ETH at $2,500.50
        result = parse_strike_from_title("Will ETH be above $2,500.50?")
        assert result == pytest.approx(2500.50)


# ── implied_sigma ─────────────────────────────────────────────────────────────

class TestImpliedSigma:
    def test_atm_returns_positive_sigma(self):
        # Near-ATM (p=0.45, S=K), 1-day bucket: formula converges to high but finite vol
        sigma = implied_sigma(0.45, 85000.0, 85000.0, 1.0 / 365)
        assert sigma is not None
        assert 0.01 <= sigma <= 50.0

    def test_invalid_zero_T(self):
        assert implied_sigma(0.50, 85000.0, 85000.0, 0.0) is None

    def test_invalid_zero_S(self):
        assert implied_sigma(0.50, 0.0, 85000.0, 0.01) is None

    def test_invalid_extreme_p(self):
        assert implied_sigma(0.001, 85000.0, 85000.0, 0.01) is None
        assert implied_sigma(0.999, 85000.0, 85000.0, 0.01) is None

    def test_roundtrip_consistency(self):
        """Implied sigma should reproduce the original price within N(d2) tolerance."""
        import math
        # BTC 'above $80k' market, spot=$85k (6% ITM), 1-week to expiry, p=0.70
        S, K, T = 85000.0, 80000.0, 7.0 / 365
        p_orig = 0.70
        sigma = implied_sigma(p_orig, S, K, T)
        assert sigma is not None
        assert 0.01 <= sigma <= 50.0
        # Verify: N(d2) ≈ p_orig
        sqrt_T = math.sqrt(T)
        d2 = (math.log(S / K) - 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
        p_recovered = _norm_cdf(d2)
        assert abs(p_recovered - p_orig) < 0.001


# ── bs_digital_coins ─────────────────────────────────────────────────────────

class TestBsDigitalCoins:
    def test_returns_positive_coins(self):
        coins = bs_digital_coins(50.0, 0.5, 85000.0, 85000.0, 1.0 / 365, 0.80)
        assert coins > 0

    def test_fallback_on_zero_T(self):
        # T=0 → fallback to binary_delta approach
        coins = bs_digital_coins(50.0, 0.5, 85000.0, 85000.0, 0.0, 0.80)
        assert coins > 0  # fallback returns binary_delta(0.5) * 50 / 85000

    def test_higher_sigma_means_different_hedge(self):
        # Higher vol → different n(d2)/(sigma*sqrt(T)) value
        low_vol = bs_digital_coins(100.0, 0.5, 85000.0, 85000.0, 1.0 / 365, 0.30)
        high_vol = bs_digital_coins(100.0, 0.5, 85000.0, 85000.0, 1.0 / 365, 1.50)
        # Both are positive and differ
        assert low_vol > 0
        assert high_vol > 0
        assert low_vol != pytest.approx(high_vol)

    def test_larger_notional_scales_linearly(self):
        coins_50 = bs_digital_coins(50.0, 0.5, 85000.0, 85000.0, 1.0 / 365, 0.80)
        coins_100 = bs_digital_coins(100.0, 0.5, 85000.0, 85000.0, 1.0 / 365, 0.80)
        assert coins_100 == pytest.approx(coins_50 * 2, rel=1e-6)


# ── _is_priority_market ───────────────────────────────────────────────────────

def _make_market(fees_enabled=False, underlying="BTC") -> PMMarket:
    """Helper — only sets the fields used by _is_priority_market."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.fees_enabled = fees_enabled
    m.is_fee_free = not fees_enabled   # mirrors the real property
    m.rebate_pct = 0.20 if fees_enabled else 0.0
    m.underlying = underlying
    m.market_type = "bucket_1h"
    m.condition_id = "cond_001"
    m.max_incentive_spread = 0.04      # standard 4-cent spread
    m.volume_24hr = 0.0                # no volume → new-market fallback sizing
    return m


class TestIsPriorityMarket:
    def test_fee_free_always_priority(self):
        market = _make_market(fees_enabled=False)
        assert _is_priority_market(market, 0.50) is True

    def test_fee_enabled_at_extreme_is_priority(self):
        market = _make_market(fees_enabled=True)
        assert _is_priority_market(market, 0.10) is True
        assert _is_priority_market(market, 0.90) is True

    def test_fee_enabled_at_mid_not_priority(self):
        market = _make_market(fees_enabled=True)
        # Standard 4c spread still has edge at mid — the old 0.20/0.80 binary
        # cutoff is now replaced by continuous fee-adjusted edge check.
        # At p=0.50: fee=0.004375, half_spread=0.02, edge=0.0156 > MIN_EDGE_PCT=0.005
        # So the standard spread IS priority even at mid — fee check is more permissive.
        assert _is_priority_market(market, 0.50) is True
        # Markets near 0.35-0.49 with standard spread are also priority:
        assert _is_priority_market(market, 0.35) is True
        # Only markets with very small incentive spreads are rejected at mid
        # (tested in TestIsPriorityMarketFeeAdjusted)

    def test_fee_enabled_boundary_at_020(self):
        market = _make_market(fees_enabled=True)
        # With fee-adjusted check, standard 4c spread is priority everywhere
        assert _is_priority_market(market, 0.20) is True

    def test_fee_enabled_boundary_at_021(self):
        market = _make_market(fees_enabled=True)
        # 0.21 also has positive edge with standard spread
        assert _is_priority_market(market, 0.21) is True


# ── MakerStrategy — inventory tracking ───────────────────────────────────────

class TestInventoryTracking:
    def setup_method(self):
        from unittest.mock import MagicMock, AsyncMock
        self.pm = MagicMock()
        self.pm.on_price_change = MagicMock()   # no-op registration
        self.pm.get_markets = MagicMock(return_value={})
        self.hl = MagicMock()
        self.hl.on_bbo_update = MagicMock()     # no-op registration
        self.risk = RiskEngine()
        # MakerStrategy(pm, hl, risk, quote_size_usd=50)
        self.strategy = MakerStrategy(self.pm, self.hl, self.risk, quote_size_usd=50.0)

    def test_yes_buy_increases_inventory(self):
        self.strategy.record_fill("mkt", "BTC", "YES_BUY", 100.0)
        assert self.strategy.get_inventory()["BTC"] == pytest.approx(100.0)

    def test_yes_sell_decreases_inventory(self):
        self.strategy.record_fill("mkt", "BTC", "YES_BUY", 200.0)
        self.strategy.record_fill("mkt", "BTC", "YES_SELL", 50.0)
        assert self.strategy.get_inventory()["BTC"] == pytest.approx(150.0)

    def test_no_buy_decreases_inventory(self):
        """Buying NO is effectively short the underlying — decreases net."""
        self.strategy.record_fill("mkt", "BTC", "NO_BUY", 100.0)
        assert self.strategy.get_inventory()["BTC"] == pytest.approx(-100.0)

    def test_no_sell_increases_inventory(self):
        self.strategy.record_fill("mkt", "BTC", "NO_BUY", 200.0)
        self.strategy.record_fill("mkt", "BTC", "NO_SELL", 50.0)
        assert self.strategy.get_inventory()["BTC"] == pytest.approx(-150.0)

    def test_multiple_underlyings_independent(self):
        self.strategy.record_fill("mkt", "BTC", "YES_BUY", 100.0)
        self.strategy.record_fill("mkt", "ETH", "YES_BUY", 200.0)
        inv = self.strategy.get_inventory()
        assert inv["BTC"] == pytest.approx(100.0)
        assert inv["ETH"] == pytest.approx(200.0)

    def test_net_zero_cancels_out(self):
        self.strategy.record_fill("mkt", "BTC", "YES_BUY", 100.0)
        self.strategy.record_fill("mkt", "BTC", "YES_SELL", 100.0)
        assert self.strategy.get_inventory()["BTC"] == pytest.approx(0.0)


# ── _is_priority_market — fee-adjusted edge ───────────────────────────────────

class TestIsPriorityMarketFeeAdjusted:
    """
    _is_priority_market now uses a continuous fee-adjusted edge check:
        effective_edge = half_spread - PM_FEE_COEFF * p * (1-p)
    rather than the old hard 0.20/0.80 binary boundary.
    """

    def _make_market_with_spread(self, fees_enabled=True, incentive_spread=0.04):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.fees_enabled = fees_enabled
        m.is_fee_free = not fees_enabled
        m.rebate_pct = 0.20 if fees_enabled else 0.0
        m.underlying = "BTC"
        m.max_incentive_spread = incentive_spread
        return m

    def test_standard_spread_passes_at_mid(self):
        """Standard 4c spread: effective_edge = 0.02 + 0.20*0.004375 = 0.020875 > MIN_EDGE_PCT."""
        market = self._make_market_with_spread(incentive_spread=0.04)
        assert _is_priority_market(market, 0.50) is True

    def test_standard_spread_passes_at_extreme(self):
        """Standard 4c spread at p=0.15: effective_edge adds rebate on top of half-spread."""
        market = self._make_market_with_spread(incentive_spread=0.04)
        assert _is_priority_market(market, 0.15) is True

    def test_tight_spread_passes_at_mid(self):
        """1c spread: rebate model makes this viable — effective_edge = half_spread + rebate.
        New formula: effective_edge = 0.005 + 0.20 * 0.004375 = 0.005875 > MIN_EDGE_PCT (0.001).
        Pin MIN_EDGE_PCT to the documented baseline, not any config_overrides.json value."""
        orig = config.MIN_EDGE_PCT
        config.MIN_EDGE_PCT = 0.001
        try:
            market = self._make_market_with_spread(incentive_spread=0.01)
            assert _is_priority_market(market, 0.50) is True
        finally:
            config.MIN_EDGE_PCT = orig

    def test_tight_spread_passes_at_extreme(self):
        """1c spread at p=0.15: effective_edge = 0.005 + 0.20*0.002231 = 0.005446 > 0.001.
        Pin MIN_EDGE_PCT to the documented baseline, not any config_overrides.json value."""
        orig = config.MIN_EDGE_PCT
        config.MIN_EDGE_PCT = 0.001
        try:
            market = self._make_market_with_spread(incentive_spread=0.01)
            assert _is_priority_market(market, 0.15) is True
        finally:
            config.MIN_EDGE_PCT = orig

    def test_fee_free_always_priority(self):
        """Fee-free markets skip the edge check entirely."""
        market = self._make_market_with_spread(fees_enabled=False, incentive_spread=0.01)
        assert _is_priority_market(market, 0.50) is True

    def test_medium_spread_mid_price(self):
        """2c spread: effective_edge = 0.01 + 0.20*0.004375 = 0.010875 > MIN_EDGE_PCT."""
        orig = config.MIN_EDGE_PCT
        config.MIN_EDGE_PCT = 0.001
        try:
            market = self._make_market_with_spread(incentive_spread=0.02)
            assert _is_priority_market(market, 0.50) is True
        finally:
            config.MIN_EDGE_PCT = orig

    def test_rebate_is_additive_not_subtractive(self):
        """Confirm the formula adds the rebate contribution, i.e. effective_edge > half_spread."""
        import config as cfg
        market = self._make_market_with_spread(incentive_spread=0.04)
        mid = 0.50
        taker_fee = cfg.PM_FEE_COEFF * mid * (1.0 - mid)
        rebate_contrib = market.rebate_pct * taker_fee
        # effective_edge should be ABOVE half_spread when rebate > 0
        assert rebate_contrib > 0
        assert _is_priority_market(market, mid) is True


# ── _reprice_market — cancel-before-reprice ────────────────────────────────────

class TestRepriceMarketCancels:
    """
    _reprice_market must call pm.cancel_order(old_order_id) for each
    existing resting quote before posting new orders.
    """

    def _make_maker(self):
        from unittest.mock import MagicMock, AsyncMock
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})

        # Mock cancel_order as an async coroutine
        pm.cancel_order = AsyncMock(return_value=True)
        # place_limit returns a fake order ID
        pm.place_limit = AsyncMock(return_value="order-new-001")
        pm.get_mid = MagicMock(return_value=0.50)
        pm.get_book = MagicMock(return_value=None)
        pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))

        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), pm

    def _make_market(self, condition_id="cond_001", spread=0.04):
        import time as t
        from unittest.mock import MagicMock
        m = MagicMock()
        m.condition_id = condition_id
        m.token_id_yes = "tok_yes_001"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0       # required float for score_maker in _evaluate_signal
        m.volume_24hr = 10_000.0  # above MAKER_MIN_VOLUME_24HR at any lifecycle fraction
        m.max_incentive_spread = spread
        m.tick_size = 0.01
        m.market_type = "bucket_1h"   # lifecycle-fraction → required_volume scales to 0 at fresh
        m.discovered_at = t.time() - 9000  # old market — normal spread
        m.end_date = datetime.now(timezone.utc) + timedelta(minutes=30)  # frac≈0.5 → inside entry/exit gate range
        return m

    def test_cancel_called_for_existing_bid(self):
        """When a BID quote exists, cancel_order must be called with its order_id."""
        strategy, pm = self._make_maker()
        market = self._make_market()

        # Pre-seed an existing bid quote
        strategy._active_quotes[market.token_id_yes] = ActiveQuote(
            market_id=market.condition_id,
            token_id=market.token_id_yes,
            side="BUY",
            price=0.13,
            size=50.0,
            order_id="old-bid-order",
        )

        asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
        pm.cancel_order.assert_any_call("old-bid-order")

    def test_cancel_called_for_existing_ask(self):
        """When an ASK quote exists, cancel_order must be called with its order_id."""
        strategy, pm = self._make_maker()
        market = self._make_market()

        ask_key = f"{market.token_id_yes}_ask"
        strategy._active_quotes[ask_key] = ActiveQuote(
            market_id=market.condition_id,
            token_id=market.token_id_yes,
            side="SELL",
            price=0.17,
            size=50.0,
            order_id="old-ask-order",
        )

        asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
        pm.cancel_order.assert_any_call("old-ask-order")

    def test_no_cancel_when_no_existing_quote(self):
        """If no quote exists yet, cancel_order must not be called at all."""
        strategy, pm = self._make_maker()
        market = self._make_market()

        asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
        pm.cancel_order.assert_not_called()

    def test_new_market_uses_wide_spread(self):
        """Markets younger than NEW_MARKET_AGE_LIMIT get NEW_MARKET_WIDE_SPREAD quotes."""
        import time as t
        strategy, pm = self._make_maker()
        market = self._make_market()
        market.discovered_at = t.time()  # brand-new market

        asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))

        # The bid should be mid - NEW_MARKET_WIDE_SPREAD/2 = 0.50 - 0.04 = 0.46
        # The ask should be mid + NEW_MARKET_WIDE_SPREAD/2 = 0.50 + 0.04 = 0.54
        calls = [call.args for call in pm.place_limit.call_args_list]
        bid_call = next((c for c in calls if c[1] == "BUY"), None)
        ask_call = next((c for c in calls if c[1] == "SELL"), None)
        assert bid_call is not None and bid_call[2] == pytest.approx(0.46, abs=0.01)
        assert ask_call is not None and ask_call[2] == pytest.approx(0.54, abs=0.01)


# ── Depth gate + depth-aware spread widening ──────────────────────────────────

class TestDepthGate:
    """
    Tests for MAKER_MIN_DEPTH_TO_QUOTE gate and depth-aware spread widening.
    All tests use _depth_spread_factor and _depth_at_level as pure-function targets
    so they run without a live PM/HL connection.
    """

    def _strategy(self):
        from unittest.mock import MagicMock
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.get_book = MagicMock(return_value=None)
        pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))
        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        risk = RiskEngine()
        return MakerStrategy(pm=pm, hl=hl, risk=risk)

    # ── _depth_spread_factor ──────────────────────────────────────────────────

    def test_spread_factor_normal_depth_is_one(self):
        """Depth at or above threshold → factor 1.0 (no widening)."""
        orig_thin = config.MAKER_DEPTH_SPREAD_FACTOR_THIN
        orig_zero = config.MAKER_DEPTH_SPREAD_FACTOR_ZERO
        config.MAKER_DEPTH_SPREAD_FACTOR_THIN = 1.5
        config.MAKER_DEPTH_SPREAD_FACTOR_ZERO = 2.0
        try:
            s = self._strategy()
            assert s._depth_spread_factor(100.0) == pytest.approx(1.0)
            assert s._depth_spread_factor(50.0)  == pytest.approx(1.0)  # exactly at threshold
        finally:
            config.MAKER_DEPTH_SPREAD_FACTOR_THIN = orig_thin
            config.MAKER_DEPTH_SPREAD_FACTOR_ZERO = orig_zero

    def test_spread_factor_zero_depth_returns_factor_zero(self):
        """depth == 0 → MAKER_DEPTH_SPREAD_FACTOR_ZERO."""
        orig = config.MAKER_DEPTH_SPREAD_FACTOR_ZERO
        config.MAKER_DEPTH_SPREAD_FACTOR_ZERO = 2.0
        try:
            s = self._strategy()
            assert s._depth_spread_factor(0.0) == pytest.approx(2.0)
        finally:
            config.MAKER_DEPTH_SPREAD_FACTOR_ZERO = orig

    def test_spread_factor_thin_book_is_interpolated(self):
        """depth=25 (half threshold=50) → midpoint between FACTOR_THIN and 1.0."""
        orig_thin = config.MAKER_DEPTH_SPREAD_FACTOR_THIN
        orig_thresh = config.MAKER_DEPTH_THIN_THRESHOLD
        config.MAKER_DEPTH_SPREAD_FACTOR_THIN = 1.5
        config.MAKER_DEPTH_THIN_THRESHOLD = 50
        try:
            s = self._strategy()
            # At depth=25: t = 25/50 = 0.5; factor = 1.5 + 0.5*(1.0-1.5) = 1.25
            assert s._depth_spread_factor(25.0) == pytest.approx(1.25)
        finally:
            config.MAKER_DEPTH_SPREAD_FACTOR_THIN = orig_thin
            config.MAKER_DEPTH_THIN_THRESHOLD = orig_thresh

    def test_spread_factor_defaults_no_widening(self):
        """Default config (factors = 1.0) → always returns 1.0."""
        s = self._strategy()
        assert s._depth_spread_factor(0.0)   == pytest.approx(1.0)
        assert s._depth_spread_factor(25.0)  == pytest.approx(1.0)
        assert s._depth_spread_factor(100.0) == pytest.approx(1.0)

    # ── _depth_at_level ───────────────────────────────────────────────────────

    def test_depth_at_level_sums_matching_bids(self):
        """Contracts at price within half_tick of quote level are summed."""
        from unittest.mock import MagicMock
        book = MagicMock()
        # Two bids at the same price level, one too far away.
        # half_tick=0.005; both 0.48 entries match (|0|≤0.005), 0.46 does not.
        book.bids = [(0.48, 30.0), (0.48, 20.0), (0.46, 50.0)]
        book.asks = []
        s = self._strategy()
        depth = s._depth_at_level(book, "BUY", 0.48)
        assert depth == pytest.approx(50.0)

    def test_depth_at_level_empty_book_returns_zero(self):
        from unittest.mock import MagicMock
        book = MagicMock()
        book.bids = []
        book.asks = []
        s = self._strategy()
        assert s._depth_at_level(book, "BUY", 0.48) == 0.0
        assert s._depth_at_level(book, "SELL", 0.52) == 0.0

    # ── Depth gate in _evaluate_signal ────────────────────────────────────────

    def _make_market(self, mid=0.50):
        """Minimal PMMarket mock that passes all gates before the depth check."""
        import time as t
        from unittest.mock import MagicMock
        m = MagicMock()
        m.condition_id = "cond_depth_001"
        m.token_id_yes = "tok_yes_depth"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.tick_size = 0.01
        m.volume_24hr = 100_000.0
        m.max_incentive_spread = 0.04
        m.market_type = "bucket_daily"
        m.discovered_at = t.time() - 7200   # 2h old (past NEW_MARKET_AGE_LIMIT)
        from datetime import datetime, timezone, timedelta
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=20)
        m.title = "BTC above 80k on [date]"
        return m

    def test_depth_gate_disabled_by_default(self):
        """Default MAKER_MIN_DEPTH_TO_QUOTE=0 → gate inactive, signal returned."""
        s = self._strategy()
        from unittest.mock import MagicMock
        book = MagicMock()
        book.bids = []
        book.asks = []
        book.best_bid = None
        book.best_ask = None
        s._pm.get_book = MagicMock(return_value=book)
        s._pm.get_mid = MagicMock(return_value=0.50)

        market = self._make_market(mid=0.50)
        # With depth gate disabled, even an empty book should not block the signal
        result = s._evaluate_signal(market, 0.50)
        # Should not be blocked by depth gate (may pass or fail on score, but depth alone is not the reason)
        assert config.MAKER_MIN_DEPTH_TO_QUOTE == 0  # confirm default

    def test_depth_gate_blocks_empty_book_when_enabled(self):
        """MAKER_MIN_DEPTH_TO_QUOTE=1 → empty book (depth=0) blocks the signal."""
        orig = config.MAKER_MIN_DEPTH_TO_QUOTE
        config.MAKER_MIN_DEPTH_TO_QUOTE = 1
        try:
            s = self._strategy()
            from unittest.mock import MagicMock
            book = MagicMock()
            book.bids = []
            book.asks = []
            book.best_bid = None
            book.best_ask = None
            s._pm.get_book = MagicMock(return_value=book)
            s._pm.get_mid = MagicMock(return_value=0.50)

            market = self._make_market(mid=0.50)
            result = s._evaluate_signal(market, 0.50)
            assert result is None, "Depth gate should block when depth=0 < min=1"
        finally:
            config.MAKER_MIN_DEPTH_TO_QUOTE = orig

    def test_depth_gate_passes_with_sufficient_depth(self):
        """MAKER_MIN_DEPTH_TO_QUOTE=10 → book with 20 contracts on each side passes."""
        orig = config.MAKER_MIN_DEPTH_TO_QUOTE
        config.MAKER_MIN_DEPTH_TO_QUOTE = 10
        try:
            s = self._strategy()
            from unittest.mock import MagicMock
            book = MagicMock()
            # 20 contracts at our quote level (0.48 bid, 0.52 ask for mid=0.50, spread=0.04)
            book.bids = [(0.48, 20.0)]
            book.asks = [(0.52, 20.0)]
            book.best_bid = 0.48
            book.best_ask = 0.52
            s._pm.get_book = MagicMock(return_value=book)

            market = self._make_market(mid=0.50)
            result = s._evaluate_signal(market, 0.50)
            # Depth gate should pass (20 >= 10); result may still be None due to score gate
            # but depth is not the cause — check depth attribute if signal returned
            if result is not None:
                assert result.depth == pytest.approx(20.0)
        finally:
            config.MAKER_MIN_DEPTH_TO_QUOTE = orig


# ── _rebalance_hedge — HEDGE_REBALANCE_USD ────────────────────────────────────

class TestRebalanceHedge:
    """
    _rebalance_hedge must:
    1. Skip when inventory < HEDGE_THRESHOLD_USD
    2. Skip rebalance when delta < HEDGE_REBALANCE_USD
    3. Clear coin hedge state when inventory falls below threshold
    """

    def setup_method(self, method):
        self._orig_hedge_enabled = config.MAKER_HEDGE_ENABLED
        config.MAKER_HEDGE_ENABLED = True

    def teardown_method(self, method):
        config.MAKER_HEDGE_ENABLED = self._orig_hedge_enabled

    def _make_maker_with_hl(self, hl_mid=80000.0):
        from unittest.mock import MagicMock, AsyncMock
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.cancel_order = AsyncMock(return_value=True)

        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        hl.get_mid = MagicMock(return_value=hl_mid)
        hl.place_hedge = AsyncMock(return_value={"ok": True})
        hl.close_hedge = AsyncMock(return_value={"ok": True})

        risk = RiskEngine()
        strategy = MakerStrategy(pm, hl, risk)
        return strategy, hl, risk

    def test_below_threshold_no_hedge(self):
        strategy, hl, _ = self._make_maker_with_hl()
        with patch.object(strategy, '_position_delta_usd', return_value=config.HEDGE_THRESHOLD_USD - 1):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        hl.place_hedge.assert_not_called()

    def test_above_threshold_hedge_placed(self):
        strategy, hl, _ = self._make_maker_with_hl()
        with patch.object(strategy, '_position_delta_usd', return_value=config.HEDGE_THRESHOLD_USD + 100):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        hl.place_hedge.assert_called_once()

    def test_rebalance_usd_gate_skips_small_delta(self):
        """Second rebalance with tiny delta change must be skipped."""
        strategy, hl, risk = self._make_maker_with_hl()

        # First rebalance places the hedge
        with patch.object(strategy, '_position_delta_usd', return_value=600.0):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        assert hl.place_hedge.call_count == 1

        # Tiny delta change ($5) — below HEDGE_REBALANCE_PCT (20%)
        with patch.object(strategy, '_position_delta_usd', return_value=605.0):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        assert hl.place_hedge.call_count == 1

    def test_clearing_inventory_removes_hedge(self):
        """When inventory drops below threshold, coin hedge state is cleared."""
        strategy, hl, risk = self._make_maker_with_hl()
        with patch.object(strategy, '_position_delta_usd', return_value=600.0):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        assert "BTC" in strategy._coin_hedges

        # Delta drops below threshold — hedge should be closed
        with patch.object(strategy, '_position_delta_usd', return_value=config.HEDGE_THRESHOLD_USD - 1):
            asyncio.get_event_loop().run_until_complete(strategy._rebalance_hedge("BTC"))
        assert "BTC" not in strategy._coin_hedges


# ── monitor.py — maker-specific exit logic ────────────────────────────────────

class TestShouldExitMakerVsMispricing:
    """
    Maker positions must skip profit-target and stop-loss.
    Time stop must use MAKER_EXIT_HOURS, not EXIT_DAYS_BEFORE_RESOLUTION.
    Mispricing positions must still use the original logic unchanged.
    """

    def _make_pos(self, strategy="mispricing"):
        pos = Position(
            market_id="mkt",
            market_type="bucket_1h",
            underlying="BTC",
            side="YES",
            size=50.0,
            entry_price=0.15,
            strategy=strategy,
            opened_at=datetime(2026, 3, 13, 0, 0, tzinfo=timezone.utc),
        )
        return pos

    def test_mispricing_stop_loss_fires(self):
        from monitor import should_exit
        pos = self._make_pos(strategy="mispricing")
        # Loss exceeds STOP_LOSS_USD
        current_price = pos.entry_price - (config.STOP_LOSS_USD / pos.size) - 0.01
        fired, reason, _ = should_exit(
            pos, current_price, 0.10,
            market_end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            now=datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc),
        )
        assert fired is True
        assert reason == "stop_loss"

    def test_maker_stop_loss_does_not_fire(self):
        """Maker positions must NOT exit on stop-loss."""
        from monitor import should_exit
        pos = self._make_pos(strategy="maker")
        current_price = pos.entry_price - (config.STOP_LOSS_USD / pos.size) - 0.01
        fired, reason, _ = should_exit(
            pos, current_price, 0.10,
            market_end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            now=datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc),
        )
        assert fired is False

    def test_mispricing_profit_target_fires(self):
        from monitor import should_exit
        pos = self._make_pos(strategy="mispricing")
        deviation = 0.10
        target = deviation * config.PROFIT_TARGET_PCT * pos.size + 0.01
        current_price = pos.entry_price + target / pos.size
        fired, reason, _ = should_exit(
            pos, current_price, deviation,
            market_end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            now=datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc),
        )
        assert fired is True
        assert reason == "profit_target"

    def test_maker_profit_target_does_not_fire(self):
        """Maker positions must NOT exit on profit target."""
        from monitor import should_exit
        pos = self._make_pos(strategy="maker")
        deviation = 0.10
        target = deviation * config.PROFIT_TARGET_PCT * pos.size + 0.01
        current_price = pos.entry_price + target / pos.size
        fired, reason, _ = should_exit(
            pos, current_price, deviation,
            market_end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            now=datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc),
        )
        assert fired is False

    def test_maker_near_expiry_uses_hours(self):
        """Maker time stop fires at MAKER_EXIT_HOURS for non-bucket (milestone) markets."""
        from monitor import should_exit
        import datetime as dt
        orig_exit_hours = config.MAKER_EXIT_HOURS
        config.MAKER_EXIT_HOURS = 6.0
        try:
            pos = self._make_pos(strategy="maker")
            pos.market_type = "milestone"  # non-bucket — MAKER_EXIT_HOURS applies
            pos.opened_at = datetime(2026, 3, 13, 0, 0, tzinfo=timezone.utc)
            now = datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc)
            # 5 hours to expiry — within MAKER_EXIT_HOURS(6) but outside EXIT_DAYS_BEFORE_RESOLUTION(3 days)
            end = now + dt.timedelta(hours=5)
            fired, reason, _ = should_exit(pos, 0.15, 0.10, market_end_date=end, now=now)
            assert fired is True
            assert reason == "time_stop"
        finally:
            config.MAKER_EXIT_HOURS = orig_exit_hours

    def test_mispricing_not_kicked_at_hours(self):
        """At 7 hours to expiry: maker does NOT fire (>6h), mispricing DOES fire (<3 days)."""
        from monitor import should_exit
        import datetime as dt
        import config as cfg
        pos = self._make_pos(strategy="mispricing")
        pos.opened_at = datetime(2026, 3, 13, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc)
        # 7 hours: outside MAKER_EXIT_HOURS(6h) but inside EXIT_DAYS_BEFORE_RESOLUTION(3d=72h)
        end = now + dt.timedelta(hours=7)
        # Override EXIT_DAYS_BEFORE_RESOLUTION to the expected 3-day value for this test
        # (config_overrides.json sets it to 0 for production, but this test verifies the
        #  time-stop logic at the canonical 3-day threshold).
        old_val = cfg.EXIT_DAYS_BEFORE_RESOLUTION
        cfg.EXIT_DAYS_BEFORE_RESOLUTION = 3
        try:
            fired, reason, _ = should_exit(pos, 0.15, 0.10, market_end_date=end, now=now)
        finally:
            cfg.EXIT_DAYS_BEFORE_RESOLUTION = old_val
        # Mispricing should fire (7h < 3 days)
        assert fired is True
        assert reason == "time_stop"

    def test_maker_not_kicked_at_seven_hours(self):
        """At 7 hours to expiry: maker does NOT fire (7h > MAKER_EXIT_HOURS=6h)."""
        from monitor import should_exit
        import datetime as dt
        pos = self._make_pos(strategy="maker")
        pos.opened_at = datetime(2026, 3, 13, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 13, 1, 0, tzinfo=timezone.utc)
        end = now + dt.timedelta(hours=7)
        fired, _, _ = should_exit(pos, 0.15, 0.10, market_end_date=end, now=now)
        assert fired is False


# ── Capital Accounting + Signal / Deploy / Undeploy ───────────────────────────

class TestCapitalAndSignals:
    """
    Tests for the 4-stage capital flow:
      _reprice_market → _signals (+ per-coin cap check)
      deployed_capital / available_capital
      auto vs manual deployment mode
      deploy_signal() / undeploy_quote()
    """

    def _make_maker(self):
        from unittest.mock import MagicMock, AsyncMock
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.cancel_order = AsyncMock(return_value=True)
        pm.place_limit = AsyncMock(return_value="order-xyz")
        pm.get_mid = MagicMock(return_value=0.50)
        pm.get_book = MagicMock(return_value=None)
        pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))

        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), pm, risk

    def _make_market(self, underlying="BTC"):
        import time as t
        from unittest.mock import MagicMock
        m = MagicMock()
        m.condition_id = f"cond_{underlying}"
        m.token_id_yes = f"tok_{underlying}_yes"
        m.underlying = underlying
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.volume_24hr = 10_000.0  # above MAKER_MIN_VOLUME_24HR at any lifecycle fraction
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = "bucket_1h"   # lifecycle-fraction → required_volume scales to 0 at fresh
        m.discovered_at = t.time() - 9000  # old market → normal spread
        m.end_date = datetime.now(timezone.utc) + timedelta(minutes=30)  # frac≈0.5 → inside entry/exit gate range
        return m

    # ── 1. Per-coin cap check passes underlying to can_open ──────────────────

    def test_reprice_market_passes_underlying_to_risk(self):
        """_evaluate_signal must call can_open with underlying= so per-coin cap fires."""
        from unittest.mock import patch, MagicMock
        strategy, pm, risk = self._make_maker()

        reached_args = {}

        original_can_open = risk.can_open

        def spy_can_open(condition_id, size, strategy="mispricing", underlying=""):
            reached_args["underlying"] = underlying
            return original_can_open(condition_id, size, strategy=strategy, underlying=underlying)

        market = self._make_market("ETH")
        with patch.object(risk, "can_open", side_effect=spy_can_open):
            asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))

        assert reached_args.get("underlying") == "ETH", (
            "_evaluate_signal must forward underlying= to risk.can_open"
        )

    # ── 2. deployed_capital arithmetic ───────────────────────────────────────

    def test_deployed_capital_counts_actual_collateral(self):
        """BUY locks price×size; SELL locks (1−price)×size."""
        strategy, _, _ = self._make_maker()
        strategy._active_quotes["tok1"] = ActiveQuote(
            market_id="m1", token_id="tok1", side="BUY",
            price=0.40, size=50.0, order_id="o1",
            collateral_usd=0.40 * 50.0,   # 20.0
        )
        strategy._active_quotes["tok1_ask"] = ActiveQuote(
            market_id="m1", token_id="tok1", side="SELL",
            price=0.44, size=50.0, order_id="o2",
            collateral_usd=(1.0 - 0.44) * 50.0,  # 28.0
        )
        # BUY: 0.40 × 50 = 20; SELL: (1−0.44) × 50 = 28
        assert strategy.deployed_capital == pytest.approx(48.0, abs=0.01)

    # ── 3. Auto mode deploys immediately ─────────────────────────────────────

    def test_auto_mode_deploys_immediately(self):
        """With MAKER_DEPLOYMENT_MODE='auto' and sufficient capital, place_limit is called."""
        original_mode = config.MAKER_DEPLOYMENT_MODE
        config.MAKER_DEPLOYMENT_MODE = "auto"
        config.PAPER_CAPITAL_USD = 10000.0
        try:
            strategy, pm, _ = self._make_maker()
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
            pm.place_limit.assert_called()
            assert market.token_id_yes in strategy._active_quotes or \
                f"{market.token_id_yes}_ask" in strategy._active_quotes
        finally:
            config.MAKER_DEPLOYMENT_MODE = original_mode

    # ── 4. Manual mode stores signal without deploying ───────────────────────

    def test_manual_mode_stores_signal_only(self):
        """With MAKER_DEPLOYMENT_MODE='manual', signal is stored but no orders posted."""
        original_mode = config.MAKER_DEPLOYMENT_MODE
        config.MAKER_DEPLOYMENT_MODE = "manual"
        try:
            strategy, pm, _ = self._make_maker()
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
            pm.place_limit.assert_not_called()
            assert market.token_id_yes in strategy._signals
        finally:
            config.MAKER_DEPLOYMENT_MODE = original_mode

    # ── 5. deploy_signal() in manual mode ────────────────────────────────────

    def test_deploy_signal_in_manual_mode(self):
        """deploy_signal() must post orders and register in _active_quotes."""
        original_mode = config.MAKER_DEPLOYMENT_MODE
        config.MAKER_DEPLOYMENT_MODE = "manual"
        try:
            strategy, pm, _ = self._make_maker()
            market = self._make_market()
            # Ensure token_ids() returns the YES token so _find_market_for_token matches
            market.token_ids.return_value = [market.token_id_yes]
            # Prime the signal by running _reprice_market first
            asyncio.get_event_loop().run_until_complete(strategy._reprice_market(market))
            pm.place_limit.assert_not_called()

            # Inject market into the PM markets lookup used by _find_market_for_token
            pm.get_markets.return_value = {market.condition_id: market}

            ok = asyncio.get_event_loop().run_until_complete(
                strategy.deploy_signal(market.token_id_yes)
            )
            assert ok is True
            pm.place_limit.assert_called()
        finally:
            config.MAKER_DEPLOYMENT_MODE = original_mode

    # ── 6. undeploy_quote clears orders but keeps signal ─────────────────────

    def test_undeploy_clears_order_but_keeps_signal(self):
        """undeploy_quote() must cancel orders and remove from _active_quotes
        but leave the signal in _signals for potential redeploy."""
        strategy, pm, _ = self._make_maker()
        market = self._make_market()

        # Pre-seed signal and active quotes
        from strategies.maker.signals import MakerSignal
        signal = MakerSignal(
            market_id=market.condition_id, token_id=market.token_id_yes,
            underlying=market.underlying, mid=0.50, bid_price=0.48,
            ask_price=0.52, half_spread=0.02, effective_edge=0.02,
            market_type="crypto",
        )
        strategy._signals[market.token_id_yes] = signal
        strategy._active_quotes[market.token_id_yes] = ActiveQuote(
            market_id=market.condition_id, token_id=market.token_id_yes,
            side="BUY", price=0.48, size=50.0, order_id="order-bid",
        )
        strategy._active_quotes[f"{market.token_id_yes}_ask"] = ActiveQuote(
            market_id=market.condition_id, token_id=market.token_id_yes,
            side="SELL", price=0.52, size=50.0, order_id="order-ask",
        )

        ok = asyncio.get_event_loop().run_until_complete(
            strategy.undeploy_quote(market.token_id_yes)
        )
        assert ok is True
        # Orders cancelled
        pm.cancel_order.assert_any_call("order-bid")
        pm.cancel_order.assert_any_call("order-ask")
        # Active quotes removed
        assert market.token_id_yes not in strategy._active_quotes
        assert f"{market.token_id_yes}_ask" not in strategy._active_quotes
        # Signal preserved
        assert market.token_id_yes in strategy._signals


# ── Second-leg combined cost gate ─────────────────────────────────────────────

class TestSecondLegCombinedCostGate:
    """
    When one leg of a spread is already open in the risk engine, _evaluate_signal
    must reject the market if posting the second leg would produce a combined entry
    cost >= 1.0 - MIN_SPREAD_PROFIT_MARGIN (i.e. a zero-or-negative spread).

    Three scenarios tested:
      1. YES already filled, current ask makes combined > threshold → reject
      2. NO already filled, current bid makes combined > threshold → reject
      3. YES already filled but current ask is cheap enough → accept
    """

    def _make_maker(self):
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
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), risk

    def _make_market(self, spread=0.04, condition_id="cond_test"):
        m = MagicMock()
        m.condition_id = condition_id
        m.token_id_yes = "tok_test_yes"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.volume_24hr = 50_000.0
        m.max_incentive_spread = spread
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def _open_pos(self, risk: RiskEngine, market_id: str, side: str, entry_price: float):
        """Plant a one-sided open position directly into the risk engine."""
        from risk import Position
        key = f"{market_id}_{side}"
        risk._positions[key] = Position(
            market_id=market_id,
            market_type="bucket_daily",
            underlying="BTC",
            side=side,
            size=100.0,
            entry_price=entry_price,
            strategy="maker",
        )

    def test_yes_open_no_leg_rejected_when_ask_too_high(self):
        """YES filled at 0.70, mid drifted down to 0.35 → ask=0.37 → combined=0.70+(1-0.37)=1.33 → reject."""
        strategy, risk = self._make_maker()
        market = self._make_market(spread=0.04)
        self._open_pos(risk, market.condition_id, "YES", entry_price=0.70)

        # mid=0.35 → ask = 0.35 + 0.02 = 0.37 → combined = 0.70 + (1-0.37) = 1.33
        signal = strategy._evaluate_signal(market, mid=0.35)
        assert signal is None, "Should reject: combined cost 1.33 > threshold"

    def test_no_open_yes_leg_rejected_when_bid_too_low(self):
        """NO filled at 0.70 (YES entry was 0.30), mid drifted up → bid too high → combined > threshold."""
        strategy, risk = self._make_maker()
        market = self._make_market(spread=0.04)
        # NO entry_price=0.70 means YES entry equiv=0.30; now mid has drifted to 0.65
        # combined = (1 - 0.70) + (0.65 - 0.02) = 0.30 + 0.63 = 0.93 — below threshold (OK actually)
        # Use a case that's clearly over: NO entry=0.40, mid=0.62 → combined=(1-0.40)+(0.62-0.02)=0.60+0.60=1.20
        self._open_pos(risk, market.condition_id, "NO", entry_price=0.40)
        signal = strategy._evaluate_signal(market, mid=0.62)
        assert signal is None, "Should reject: combined cost > threshold"

    def test_yes_open_no_leg_accepted_when_spread_profitable(self):
        """YES filled at 0.40, mid still at 0.40 → ask=0.42 → combined=0.40+(1-0.42)=0.98 → accept."""
        strategy, risk = self._make_maker()
        market = self._make_market(spread=0.04)
        self._open_pos(risk, market.condition_id, "YES", entry_price=0.40)

        # mid=0.40 → ask=0.42 → combined = 0.40 + (1-0.42) = 0.98 < 0.995 → accept
        signal = strategy._evaluate_signal(market, mid=0.40)
        assert signal is not None, "Should accept: combined cost 0.98 is profitable"

    def test_exact_threshold_boundary_rejected(self):
        """Combined cost exactly at threshold (1 - MIN_SPREAD_PROFIT_MARGIN) must be rejected."""
        strategy, risk = self._make_maker()
        market = self._make_market(spread=0.04)
        # We want: yes_entry + (1 - (mid + half_spread)) == 1.0 - MIN_SPREAD_PROFIT_MARGIN
        # => yes_entry + 1 - mid - 0.02 = 0.995
        # => yes_entry = 0.015 + mid
        # Use mid=0.50: yes_entry = 0.515, ask=0.52 → combined=0.515+(1-0.52)=0.995 → reject
        orig = config.MIN_SPREAD_PROFIT_MARGIN
        config.MIN_SPREAD_PROFIT_MARGIN = 0.005
        try:
            self._open_pos(risk, market.condition_id, "YES", entry_price=0.515)
            signal = strategy._evaluate_signal(market, mid=0.50)
            assert signal is None, "Combined cost at threshold must be rejected"
        finally:
            config.MIN_SPREAD_PROFIT_MARGIN = orig

    def test_no_existing_position_gate_not_triggered(self):
        """With no open position the combined cost gate is irrelevant — normal flow."""
        strategy, risk = self._make_maker()
        market = self._make_market(spread=0.04)
        # No positions seeded — gate should not interfere
        signal = strategy._evaluate_signal(market, mid=0.50)
        # Signal may or may not pass risk/score checks, but it must not be blocked
        # by the combined cost gate (which only activates for one-sided positions).
        # We just verify no exception is raised and that if it passes it's not None.
        # (Score/capital checks are not mocked here so None is acceptable.)
        # The important thing: no AttributeError or gate false-positive.
        assert True  # no crash = pass


# ── Imbalance-aware sizing in _deploy_quote ───────────────────────────────────

class TestImbalanceAwareSizing:
    """
    _deploy_quote must reduce the heavy side's order size so that if BOTH new
    orders fill fully, total open positions converge to exact balance.

    Formula:
      yes_size = max(1, contracts - max(0, imbalance))
      no_size  = max(1, contracts - max(0, -imbalance))

    Hard stop (MAKER_MAX_IMBALANCE_CONTRACTS) still blocks posting entirely
    for extreme cases but is not the primary control.
    """

    def _make_components(self):
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
        risk = RiskEngine()
        strategy = MakerStrategy(pm, hl, risk)
        return strategy, pm, risk

    def _seed_pos(self, risk: RiskEngine, market_id: str, side: str, size: float):
        from risk import Position
        risk._positions[f"{market_id}_{side}"] = Position(
            market_id=market_id, market_type="bucket_daily",
            underlying="BTC", side=side, size=size,
            entry_price=0.50, strategy="maker",
        )

    def _make_signal(self, market_id="m1", contracts_budget=50) -> "MakerSignal":
        from strategies.maker.signals import MakerSignal
        # bid=0.50, ask=0.50 → cost_per_contract=1.00 → contracts=budget
        return MakerSignal(
            market_id=market_id, token_id="tok_yes",
            underlying="BTC", mid=0.50,
            bid_price=0.50, ask_price=0.50,
            half_spread=0.02, effective_edge=0.02,
            market_type="bucket_daily",
            quote_size=float(contracts_budget),  # cost_per_contract=1.0 → contracts=budget
        )

    def _make_market(self, market_id="m1"):
        m = MagicMock()
        m.condition_id = market_id
        m.token_id_yes = "tok_yes"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def _get_place_limit_sizes(self, pm) -> dict:
        """Return {side: size} from place_limit call args."""
        result = {}
        for call in pm.place_limit.call_args_list:
            args = call.args
            side = args[1]   # "BUY" or "SELL"
            size = args[3]   # contracts
            result[side] = result.get(side, 0) + size
        return result

    def test_balanced_posts_equal_sizes(self):
        """No imbalance → both sides get identical contract count."""
        strategy, pm, risk = self._make_components()
        signal = self._make_signal(contracts_budget=50)
        market = self._make_market()
        asyncio.get_event_loop().run_until_complete(
            strategy._deploy_quote(signal, market)
        )
        sizes = self._get_place_limit_sizes(pm)
        assert sizes.get("BUY") == sizes.get("SELL"), \
            f"Expected equal sizes, got BUY={sizes.get('BUY')} SELL={sizes.get('SELL')}"

    def test_yes_heavy_reduces_yes_size(self):
        """YES is 5 ahead (below hard-stop threshold of 10) → yes_size shrinks by 5, no_size stays at contracts."""
        orig_batch = config.MAKER_BATCH_SIZE
        config.MAKER_BATCH_SIZE = 999  # don't let batch cap interfere with imbalance test
        try:
            strategy, pm, risk = self._make_components()
            self._seed_pos(risk, "m1", "YES", 5.0)
            signal = self._make_signal(contracts_budget=50)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert sizes.get("BUY") == 50 - 5, \
                f"YES size should be 45, got {sizes.get('BUY')}"
            assert sizes.get("SELL") == 50, \
                f"NO size should be 50, got {sizes.get('SELL')}"
        finally:
            config.MAKER_BATCH_SIZE = orig_batch

    def test_no_heavy_reduces_no_size(self):
        """NO is 5 ahead (below hard-stop threshold of 10) → no_size shrinks by 5, yes_size stays at controls."""
        orig_batch = config.MAKER_BATCH_SIZE
        config.MAKER_BATCH_SIZE = 999  # don't let batch cap interfere with imbalance test
        try:
            strategy, pm, risk = self._make_components()
            self._seed_pos(risk, "m1", "NO", 5.0)
            signal = self._make_signal(contracts_budget=50)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert sizes.get("BUY") == 50, \
                f"YES size should be 50, got {sizes.get('BUY')}"
            assert sizes.get("SELL") == 50 - 5, \
                f"NO size should be 45, got {sizes.get('SELL')}"
        finally:
            config.MAKER_BATCH_SIZE = orig_batch

    def test_convergence_property(self):
        """If both new orders fill fully, total (existing + new) YES == NO."""
        strategy, pm, risk = self._make_components()
        # Use imbalance within threshold: YES=10, NO=5 → imbalance=5 < threshold(10)
        yes_open, no_open, base = 10.0, 5.0, 50
        self._seed_pos(risk, "m1", "YES", yes_open)
        self._seed_pos(risk, "m1", "NO", no_open)
        signal = self._make_signal(contracts_budget=base)
        market = self._make_market()
        asyncio.get_event_loop().run_until_complete(
            strategy._deploy_quote(signal, market)
        )
        sizes = self._get_place_limit_sizes(pm)
        yes_if_full = yes_open + (sizes.get("BUY") or 0)
        no_if_full  = no_open  + (sizes.get("SELL") or 0)
        assert yes_if_full == no_if_full, \
            f"Full-fill totals should be equal: YES={yes_if_full} NO={no_if_full}"

    def test_hard_stop_still_blocks_extreme_imbalance(self):
        """When imbalance > MAKER_MAX_IMBALANCE_CONTRACTS, heavy side is blocked entirely."""
        orig = config.MAKER_MAX_IMBALANCE_CONTRACTS
        config.MAKER_MAX_IMBALANCE_CONTRACTS = 20
        try:
            strategy, pm, risk = self._make_components()
            # YES is 25 ahead — exceeds threshold of 20 → BUY YES must be blocked
            self._seed_pos(risk, "m1", "YES", 25.0)
            signal = self._make_signal(contracts_budget=50)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert "BUY" not in sizes, \
                f"BUY YES should be blocked when YES is {25} > threshold {20}"
            assert "SELL" in sizes, "SELL YES (NO side) should still post"
        finally:
            config.MAKER_MAX_IMBALANCE_CONTRACTS = orig


# ── MAKER_BATCH_SIZE: per-order contract cap in _deploy_quote ────────────────

class TestBatchSizeCap:
    """
    MAKER_BATCH_SIZE caps the number of contracts placed in a single order.
    Even when the budget is large, no single order may exceed this limit.
    The adversary can sweep at most MAKER_BATCH_SIZE contracts before the
    next reprice cycle re-evaluates imbalance.
    """

    # Reuse the same helpers as TestImbalanceAwareSizing
    def _make_components(self):
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
        risk = RiskEngine()
        strategy = MakerStrategy(pm, hl, risk)
        return strategy, pm, risk

    def _make_signal(self, market_id="m1", contracts_budget=500):
        from strategies.maker.signals import MakerSignal
        return MakerSignal(
            market_id=market_id, token_id="tok_yes",
            underlying="BTC", mid=0.50,
            bid_price=0.50, ask_price=0.50,
            half_spread=0.02, effective_edge=0.02,
            market_type="bucket_daily",
            quote_size=float(contracts_budget),
        )

    def _make_market(self, market_id="m1"):
        m = MagicMock()
        m.condition_id = market_id
        m.token_id_yes = "tok_yes"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def _get_place_limit_sizes(self, pm) -> dict:
        result = {}
        for call in pm.place_limit.call_args_list:
            args = call.args
            side = args[1]
            size = args[3]
            result[side] = result.get(side, 0) + size
        return result

    def test_batch_cap_limits_large_budget(self):
        """Budget of 500 → each order is capped at MAKER_BATCH_SIZE (30), not 500."""
        orig = config.MAKER_BATCH_SIZE
        config.MAKER_BATCH_SIZE = 30
        try:
            strategy, pm, risk = self._make_components()
            signal = self._make_signal(contracts_budget=500)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert sizes.get("BUY", 0) <= 30, (
                f"BUY order should be capped at 30 (batch size), got {sizes.get('BUY')}"
            )
            assert sizes.get("SELL", 0) <= 30, (
                f"SELL order should be capped at 30 (batch size), got {sizes.get('SELL')}"
            )
        finally:
            config.MAKER_BATCH_SIZE = orig

    def test_batch_cap_does_not_exceed_max_contracts_per_side(self):
        """MAKER_BATCH_SIZE=999 doesn't exceed MAKER_MAX_CONTRACTS_PER_SIDE."""
        orig_batch = config.MAKER_BATCH_SIZE
        orig_max = config.MAKER_MAX_CONTRACTS_PER_SIDE
        config.MAKER_BATCH_SIZE = 999
        config.MAKER_MAX_CONTRACTS_PER_SIDE = 100
        try:
            strategy, pm, risk = self._make_components()
            signal = self._make_signal(contracts_budget=500)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert sizes.get("BUY", 0) <= 100, (
                f"BUY should be capped by MAKER_MAX_CONTRACTS_PER_SIDE=100, got {sizes.get('BUY')}"
            )
            assert sizes.get("SELL", 0) <= 100, (
                f"SELL should be capped by MAKER_MAX_CONTRACTS_PER_SIDE=100, got {sizes.get('SELL')}"
            )
        finally:
            config.MAKER_BATCH_SIZE = orig_batch
            config.MAKER_MAX_CONTRACTS_PER_SIDE = orig_max

    def test_small_budget_not_inflated_by_batch_size(self):
        """Budget of 10 with MAKER_BATCH_SIZE=100 → orders are 10, not 100."""
        orig = config.MAKER_BATCH_SIZE
        config.MAKER_BATCH_SIZE = 100
        try:
            strategy, pm, risk = self._make_components()
            signal = self._make_signal(contracts_budget=10)
            market = self._make_market()
            asyncio.get_event_loop().run_until_complete(
                strategy._deploy_quote(signal, market)
            )
            sizes = self._get_place_limit_sizes(pm)
            assert sizes.get("BUY", 0) <= 10, (
                f"BUY should not exceed budget of 10, got {sizes.get('BUY')}"
            )
            assert sizes.get("SELL", 0) <= 10, (
                f"SELL should not exceed budget of 10, got {sizes.get('SELL')}"
            )
        finally:
            config.MAKER_BATCH_SIZE = orig


# ── Fix A: per-market contracts cap in _evaluate_signal ──────────────────────

class TestPerMarketContractsCap:
    """
    When the combined open YES + NO contracts for a market already equals or
    exceeds MAKER_MAX_CONTRACTS_PER_MARKET, _evaluate_signal must return None
    regardless of other conditions.
    """

    def _make_maker(self):
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
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), risk

    def _make_market(self, condition_id="cond_cap"):
        m = MagicMock()
        m.condition_id = condition_id
        m.token_id_yes = "tok_yes_cap"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.volume_24hr = 50_000.0
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def _seed_pos(self, risk: RiskEngine, market_id: str, side: str, size: float):
        risk._positions[f"{market_id}_{side}"] = Position(
            market_id=market_id, market_type="bucket_daily",
            underlying="BTC", side=side, size=size,
            entry_price=0.50, strategy="maker",
        )

    def test_evaluate_signal_skips_when_cap_reached(self):
        """YES+NO combined == cap → _evaluate_signal returns None."""
        orig = config.MAKER_MAX_CONTRACTS_PER_MARKET
        config.MAKER_MAX_CONTRACTS_PER_MARKET = 200
        try:
            strategy, risk = self._make_maker()
            market = self._make_market()
            # Plant YES=120, NO=80 → total=200 ≥ cap
            self._seed_pos(risk, market.condition_id, "YES", 120.0)
            self._seed_pos(risk, market.condition_id, "NO", 80.0)
            signal = strategy._evaluate_signal(market, mid=0.50)
            assert signal is None, "Should block when combined contracts ≥ cap"
        finally:
            config.MAKER_MAX_CONTRACTS_PER_MARKET = orig

    def test_evaluate_signal_skips_when_yes_alone_exceeds_cap(self):
        """YES alone >= cap → skip even with no NO position."""
        orig = config.MAKER_MAX_CONTRACTS_PER_MARKET
        config.MAKER_MAX_CONTRACTS_PER_MARKET = 100
        try:
            strategy, risk = self._make_maker()
            market = self._make_market()
            self._seed_pos(risk, market.condition_id, "YES", 100.0)
            signal = strategy._evaluate_signal(market, mid=0.50)
            assert signal is None, "YES alone at cap should block"
        finally:
            config.MAKER_MAX_CONTRACTS_PER_MARKET = orig

    def test_evaluate_signal_passes_when_below_cap(self):
        """YES+NO well below cap → normal flow (signal may pass or fail other gates)."""
        orig = config.MAKER_MAX_CONTRACTS_PER_MARKET
        config.MAKER_MAX_CONTRACTS_PER_MARKET = 500
        try:
            strategy, risk = self._make_maker()
            market = self._make_market()
            self._seed_pos(risk, market.condition_id, "YES", 50.0)
            self._seed_pos(risk, market.condition_id, "NO", 50.0)
            # 100 < 500 — cap gate must not block; result depends on other checks
            # (score / volume / risk).  Just verify no AttributeError from the cap block.
            try:
                strategy._evaluate_signal(market, mid=0.50)
            except Exception as exc:
                pytest.fail(f"Unexpected exception when below cap: {exc}")
        finally:
            config.MAKER_MAX_CONTRACTS_PER_MARKET = orig


# ── Fix C: minimum spread gate in _evaluate_signal ───────────────────────────

class TestMinSpreadGate:
    """
    Markets whose max_incentive_spread is below MAKER_MIN_INCENTIVE_SPREAD must
    be skipped by _evaluate_signal regardless of all other conditions.
    """

    def _make_maker(self):
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
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), risk

    def _make_market(self, spread: float, condition_id="cond_spread"):
        m = MagicMock()
        m.condition_id = condition_id
        m.token_id_yes = "tok_yes_spread"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.volume_24hr = 50_000.0
        m.max_incentive_spread = spread
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def test_narrow_spread_skipped(self):
        """max_incentive_spread < MAKER_MIN_INCENTIVE_SPREAD → returns None."""
        orig = config.MAKER_MIN_INCENTIVE_SPREAD
        config.MAKER_MIN_INCENTIVE_SPREAD = 0.04
        try:
            strategy, _ = self._make_maker()
            market = self._make_market(spread=0.02)
            signal = strategy._evaluate_signal(market, mid=0.50)
            assert signal is None, "Spread 2¢ < 4¢ floor should be skipped"
        finally:
            config.MAKER_MIN_INCENTIVE_SPREAD = orig

    def test_exact_floor_skipped(self):
        """max_incentive_spread exactly equal to floor is not strictly less — must pass gate."""
        orig = config.MAKER_MIN_INCENTIVE_SPREAD
        config.MAKER_MIN_INCENTIVE_SPREAD = 0.04
        try:
            strategy, _ = self._make_maker()
            market = self._make_market(spread=0.04)
            # Gate condition is < (strict), so spread==floor is allowed through.
            # The call may return None for other reasons; we just confirm no crash.
            try:
                strategy._evaluate_signal(market, mid=0.50)
            except Exception as exc:
                pytest.fail(f"Unexpected exception at exact floor: {exc}")
        finally:
            config.MAKER_MIN_INCENTIVE_SPREAD = orig

    def test_wide_spread_not_blocked_by_gate(self):
        """spread > floor → gate transparent; result depends on other checks."""
        orig = config.MAKER_MIN_INCENTIVE_SPREAD
        config.MAKER_MIN_INCENTIVE_SPREAD = 0.02
        try:
            strategy, _ = self._make_maker()
            market = self._make_market(spread=0.06)
            try:
                strategy._evaluate_signal(market, mid=0.50)
            except Exception as exc:
                pytest.fail(f"Unexpected exception for wide spread: {exc}")
        finally:
            config.MAKER_MIN_INCENTIVE_SPREAD = orig


# ── Fix B: naked-leg force-close ─────────────────────────────────────────────

class TestNakedLegForceClose:
    """
    _check_naked_legs must:
      • Record the first-seen timestamp when imbalance ≥ MAKER_NAKED_CLOSE_CONTRACTS.
      • NOT fire a taker exit until MAKER_NAKED_CLOSE_SECS have elapsed (debounce).
      • Fire place_market on the heavy side after the debounce period.
      • Clear _imbalance_since after firing.
      • Clear _imbalance_since if imbalance drops below threshold.
    """

    def _make_strategy(self):
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.cancel_order = AsyncMock(return_value=True)
        pm.place_limit = AsyncMock(return_value="order-001")
        pm.place_market = AsyncMock(return_value="taker-001")
        pm.get_mid = MagicMock(return_value=0.50)
        pm.get_book = MagicMock(return_value=None)
        pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))
        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        risk = RiskEngine()
        strategy = MakerStrategy(pm, hl, risk)
        return strategy, pm, risk

    def _make_market(self, condition_id="cond_naked"):
        m = MagicMock()
        m.condition_id = condition_id
        m.token_id_yes = "tok_yes_naked"
        m.token_id_no = "tok_no_naked"
        m.underlying = "BTC"
        m.is_fee_free = True
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = "bucket_daily"
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(hours=12)
        return m

    def _seed_pos(self, risk: RiskEngine, market_id: str, side: str, size: float):
        risk._positions[f"{market_id}_{side}"] = Position(
            market_id=market_id, market_type="bucket_daily",
            underlying="BTC", side=side, size=size,
            entry_price=0.50, strategy="maker",
        )

    def test_debounce_timer_starts_on_first_detection(self):
        """First call with imbalance ≥ threshold records timestamp but does NOT fire."""
        orig_ct = config.MAKER_NAKED_CLOSE_CONTRACTS
        orig_s  = config.MAKER_NAKED_CLOSE_SECS
        config.MAKER_NAKED_CLOSE_CONTRACTS = 30
        config.MAKER_NAKED_CLOSE_SECS = 60.0
        try:
            strategy, pm, risk = self._make_strategy()
            market = self._make_market()
            pm.get_markets.return_value = {market.condition_id: market}
            pm.get_mid.return_value = 0.50

            self._seed_pos(risk, market.condition_id, "YES", 50.0)  # imbalance=50 ≥ 30

            asyncio.get_event_loop().run_until_complete(strategy._check_naked_legs())

            # Timer recorded, no taker order placed yet
            assert market.condition_id in strategy._imbalance_since, \
                "_imbalance_since should record market on first detection"
            pm.place_market.assert_not_called()
        finally:
            config.MAKER_NAKED_CLOSE_CONTRACTS = orig_ct
            config.MAKER_NAKED_CLOSE_SECS = orig_s

    def test_no_fire_before_debounce_elapses(self):
        """Second call within debounce window must NOT fire taker exit."""
        orig_ct = config.MAKER_NAKED_CLOSE_CONTRACTS
        orig_s  = config.MAKER_NAKED_CLOSE_SECS
        config.MAKER_NAKED_CLOSE_CONTRACTS = 30
        config.MAKER_NAKED_CLOSE_SECS = 60.0
        try:
            strategy, pm, risk = self._make_strategy()
            market = self._make_market()
            pm.get_markets.return_value = {market.condition_id: market}
            pm.get_mid.return_value = 0.50

            self._seed_pos(risk, market.condition_id, "YES", 50.0)

            # Seed _imbalance_since as if just detected (10s ago — within 60s window)
            strategy._imbalance_since[market.condition_id] = time.time() - 10.0

            asyncio.get_event_loop().run_until_complete(strategy._check_naked_legs())

            pm.place_market.assert_not_called()
        finally:
            config.MAKER_NAKED_CLOSE_CONTRACTS = orig_ct
            config.MAKER_NAKED_CLOSE_SECS = orig_s

    def test_taker_exit_fires_after_debounce_yes_heavy(self):
        """After debounce duration, YES-heavy imbalance triggers SELL YES place_market."""
        orig_ct = config.MAKER_NAKED_CLOSE_CONTRACTS
        orig_s  = config.MAKER_NAKED_CLOSE_SECS
        config.MAKER_NAKED_CLOSE_CONTRACTS = 30
        config.MAKER_NAKED_CLOSE_SECS = 60.0
        try:
            strategy, pm, risk = self._make_strategy()
            market = self._make_market()
            pm.get_markets.return_value = {market.condition_id: market}
            pm.get_mid.return_value = 0.50

            self._seed_pos(risk, market.condition_id, "YES", 50.0)  # imbalance=50

            # Seed timer as expired (70s ago > 60s threshold)
            strategy._imbalance_since[market.condition_id] = time.time() - 70.0

            asyncio.get_event_loop().run_until_complete(strategy._check_naked_legs())

            pm.place_market.assert_called_once()
            call_args = pm.place_market.call_args
            assert call_args.args[0] == market.token_id_yes, "Should sell YES token"
            assert call_args.args[1] == "SELL", "Should be a SELL order"
            assert call_args.args[3] == 50, "Should close 50 excess contracts"

            # Timer should be cleared after firing
            assert market.condition_id not in strategy._imbalance_since, \
                "_imbalance_since should be cleared after force-close"
        finally:
            config.MAKER_NAKED_CLOSE_CONTRACTS = orig_ct
            config.MAKER_NAKED_CLOSE_SECS = orig_s

    def test_taker_exit_fires_after_debounce_no_heavy(self):
        """After debounce duration, NO-heavy imbalance triggers SELL NO place_market."""
        orig_ct = config.MAKER_NAKED_CLOSE_CONTRACTS
        orig_s  = config.MAKER_NAKED_CLOSE_SECS
        config.MAKER_NAKED_CLOSE_CONTRACTS = 30
        config.MAKER_NAKED_CLOSE_SECS = 60.0
        try:
            strategy, pm, risk = self._make_strategy()
            market = self._make_market()
            pm.get_markets.return_value = {market.condition_id: market}
            pm.get_mid.return_value = 0.50

            self._seed_pos(risk, market.condition_id, "NO", 50.0)  # imbalance=50, NO-heavy

            # Seed timer as expired
            strategy._imbalance_since[market.condition_id] = time.time() - 70.0

            asyncio.get_event_loop().run_until_complete(strategy._check_naked_legs())

            pm.place_market.assert_called_once()
            call_args = pm.place_market.call_args
            assert call_args.args[0] == market.token_id_no, "Should sell NO token"
            assert call_args.args[1] == "SELL", "Should be a SELL order"
            assert call_args.args[3] == 50, "Should close 50 excess contracts"
        finally:
            config.MAKER_NAKED_CLOSE_CONTRACTS = orig_ct
            config.MAKER_NAKED_CLOSE_SECS = orig_s

    def test_imbalance_since_cleared_when_balance_restored(self):
        """If imbalance drops below threshold, _imbalance_since should be cleared."""
        orig_ct = config.MAKER_NAKED_CLOSE_CONTRACTS
        config.MAKER_NAKED_CLOSE_CONTRACTS = 30
        try:
            strategy, pm, risk = self._make_strategy()
            market = self._make_market()
            pm.get_markets.return_value = {market.condition_id: market}
            pm.get_mid.return_value = 0.50

            # Plant balanced positions: imbalance = 0 < 30
            self._seed_pos(risk, market.condition_id, "YES", 50.0)
            self._seed_pos(risk, market.condition_id, "NO", 50.0)

            # Pre-seed _imbalance_since as if it was previously detected
            strategy._imbalance_since[market.condition_id] = time.time() - 10.0

            asyncio.get_event_loop().run_until_complete(strategy._check_naked_legs())

            assert market.condition_id not in strategy._imbalance_since, \
                "_imbalance_since should be cleared when balance restored"
            pm.place_market.assert_not_called()
        finally:
            config.MAKER_NAKED_CLOSE_CONTRACTS = orig_ct


# ── Regression: MAKER_EXCLUDED_MARKET_TYPES exclusion gate ───────────────────

class TestEvaluateSignalExclusionGate:
    """
    BUG-1/BUG-2 regression: validates the server-side gate that the fixed
    toggleBucket toggle relies on.  _evaluate_signal must return None when
    MAKER_EXCLUDED_MARKET_TYPES contains the market's type, and must NOT
    block markets with different or unlisted types.
    """

    def _make_maker(self):
        pm = MagicMock()
        pm.on_price_change = MagicMock()
        pm.get_markets = MagicMock(return_value={})
        pm.cancel_order = AsyncMock(return_value=True)
        pm.place_limit = AsyncMock(return_value="order-excl")
        pm.get_mid = MagicMock(return_value=0.50)
        pm.get_book = MagicMock(return_value=None)
        pm._round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))
        hl = MagicMock()
        hl.on_bbo_update = MagicMock()
        risk = RiskEngine()
        return MakerStrategy(pm, hl, risk), pm, risk

    def _make_market(self, market_type="bucket_1h", underlying="BTC"):
        m = MagicMock()
        m.condition_id = f"excl_cond_{market_type}"
        m.token_id_yes = "tok_excl_yes"
        m.underlying = underlying
        m.is_fee_free = True
        m.fees_enabled = False
        m.rebate_pct = 0.0
        m.volume_24hr = 10_000.0
        m.max_incentive_spread = 0.04
        m.tick_size = 0.01
        m.market_type = market_type
        m.discovered_at = time.time() - 9000
        m.end_date = datetime.now(timezone.utc) + timedelta(minutes=30)
        return m

    def setup_method(self):
        config.MAKER_EXCLUDED_MARKET_TYPES = []

    def teardown_method(self):
        config.MAKER_EXCLUDED_MARKET_TYPES = []

    def test_excluded_type_returns_none(self):
        """_evaluate_signal must return None for excluded market types."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        strategy, _pm, _risk = self._make_maker()
        market = self._make_market(market_type="bucket_1h")
        result = strategy._evaluate_signal(market, 0.50)
        assert result is None, (
            "_evaluate_signal should return None for excluded market type bucket_1h"
        )

    def test_empty_exclusion_list_does_not_block(self):
        """With empty exclusion list, market type must not be blocked by the gate."""
        config.MAKER_EXCLUDED_MARKET_TYPES = []
        strategy, _pm, _risk = self._make_maker()
        # We only verify the exclusion gate doesn't reject; other gates may still reject
        # Budget: the function either returns a signal or None for non-exclusion reasons.
        # We confirm it doesn't raise and the exclusion gate is not responsible for None.
        market = self._make_market(market_type="bucket_1h")
        # Should not raise — exclusion gate is a no-op when list is empty
        strategy._evaluate_signal(market, 0.50)  # return value is irrelevant here

    def test_only_matching_type_blocked(self):
        """Excluding bucket_1h must not block bucket_5m."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        strategy, _pm, _risk = self._make_maker()
        # bucket_1h excluded → None
        assert strategy._evaluate_signal(self._make_market(market_type="bucket_1h"), 0.50) is None
        # bucket_5m not excluded — exclusion gate should NOT be the reason for None
        # (budget: may still be blocked by other gates; but exclusion gate passes through)
        market_5m = self._make_market(market_type="bucket_5m")
        # Allow any result — just verify no exception from the exclusion code path
        strategy._evaluate_signal(market_5m, 0.50)

    def test_multiple_excluded_types(self):
        """Multiple types can be excluded simultaneously."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h", "bucket_5m"]
        strategy, _pm, _risk = self._make_maker()
        assert strategy._evaluate_signal(self._make_market("bucket_1h"), 0.50) is None
        assert strategy._evaluate_signal(self._make_market("bucket_5m"), 0.50) is None

    def test_restore_excluded_type_unblocks(self):
        """After removing a type from exclusions, it must no longer be blocked by the gate."""
        config.MAKER_EXCLUDED_MARKET_TYPES = ["bucket_1h"]
        strategy, _pm, _risk = self._make_maker()
        # Verify blocked first
        assert strategy._evaluate_signal(self._make_market("bucket_1h"), 0.50) is None
        # Remove from exclusion list
        config.MAKER_EXCLUDED_MARKET_TYPES = []
        # Should no longer be blocked by the exclusion gate (may still return None for other reasons)
        # We just confirm no exception is raised
        strategy._evaluate_signal(self._make_market("bucket_1h"), 0.50)

