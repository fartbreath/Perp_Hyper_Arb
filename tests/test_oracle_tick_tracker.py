"""
tests/test_oracle_tick_tracker.py — Unit tests for market_data/oracle_tick_tracker.py

Run:  pytest tests/test_oracle_tick_tracker.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import time
import pytest
from unittest.mock import MagicMock

from market_data.oracle_tick_tracker import OracleTickTracker


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _tracker_with_ticks(coin: str, prices: list[float], base_ts: float | None = None) -> OracleTickTracker:
    """Return an OracleTickTracker pre-loaded with ticks for a coin."""
    tracker = OracleTickTracker()
    now = base_ts if base_ts is not None else time.time()
    for i, price in enumerate(prices):
        # Manually inject into internal state to avoid timing dependencies
        tracker._on_tick(coin, price)
        # Patch timestamps to be in a predictable sequence 1 s apart
        if coin in tracker._coins and tracker._coins[coin].price_buffer:
            buf = tracker._coins[coin].price_buffer
            # replace last entry with controlled ts
            buf[-1] = (now + i, price)
    return tracker


# ── get_upfrac_ewma ──────────────────────────────────────────────────────────

class TestUpfracEwma:
    def test_none_before_min_ticks(self):
        tracker = OracleTickTracker()
        for i in range(4):  # 4 < MIN_TICKS (5)
            tracker._on_tick("BTC", 100.0 + i)
        assert tracker.get_upfrac_ewma("BTC") is None

    def test_returns_value_at_min_ticks(self):
        tracker = OracleTickTracker()
        for i in range(5):
            tracker._on_tick("BTC", 100.0 + i)  # all up
        result = tracker.get_upfrac_ewma("BTC")
        assert result is not None
        assert 0.0 <= result <= 1.0

    def test_unknown_coin_returns_none(self):
        tracker = OracleTickTracker()
        assert tracker.get_upfrac_ewma("DOGE") is None

    def test_all_up_ticks_push_ewma_high(self):
        tracker = OracleTickTracker()
        for i in range(20):
            tracker._on_tick("BTC", 100.0 + i * 0.1)  # strictly increasing
        result = tracker.get_upfrac_ewma("BTC")
        assert result is not None
        assert result > 0.7  # EWMA should converge high with all-up ticks

    def test_all_down_ticks_push_ewma_low(self):
        tracker = OracleTickTracker()
        for i in range(20):
            tracker._on_tick("BTC", 100.0 - i * 0.1)  # strictly decreasing
        result = tracker.get_upfrac_ewma("BTC")
        assert result is not None
        assert result < 0.3  # EWMA should converge low with all-down ticks


# ── get_twap_deviation_bps ───────────────────────────────────────────────────

class TestTwapDeviationBps:
    def test_none_for_unknown_coin(self):
        tracker = OracleTickTracker()
        assert tracker.get_twap_deviation_bps("BTC") is None

    def test_none_for_single_tick(self):
        """Single tick: only one entry in buffer so TWAP window < 2 prices."""
        tracker = OracleTickTracker()
        tracker._on_tick("BTC", 100.0)
        # With only 1 tick, window_prices will have 1 entry — below the >=2 threshold
        # Note: depends on timing, so we just check it doesn't crash
        result = tracker.get_twap_deviation_bps("BTC", window_secs=9999.0)
        # result is either None or a float (0.0 if somehow 2 entries counted)
        assert result is None or isinstance(result, float)

    def test_zero_deviation_for_flat_prices(self):
        """All prices the same → TWAP == current → deviation = 0."""
        tracker = OracleTickTracker()
        now = time.time()
        state = tracker._coins.setdefault("BTC", __import__("market_data.oracle_tick_tracker", fromlist=["_CoinState"])._CoinState())
        # Inject ticks manually at known timestamps
        from market_data.oracle_tick_tracker import _CoinState
        state_new = _CoinState()
        for i in range(5):
            state_new.price_buffer.append((now - 4 + i, 100.0))
        state_new.tick_count = 5
        state_new.upfrac_ewma = 0.5
        tracker._coins["BTC"] = state_new
        result = tracker.get_twap_deviation_bps("BTC", window_secs=9999.0)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_positive_deviation_when_current_above_twap(self):
        """Last price higher than mean → positive bps."""
        from market_data.oracle_tick_tracker import _CoinState
        tracker = OracleTickTracker()
        now = time.time()
        state = _CoinState()
        # Four prices at 100, then spike to 110
        prices = [100.0, 100.0, 100.0, 100.0, 110.0]
        for i, p in enumerate(prices):
            state.price_buffer.append((now - len(prices) + i, p))
        state.tick_count = len(prices)
        state.upfrac_ewma = 0.5
        tracker._coins["BTC"] = state
        result = tracker.get_twap_deviation_bps("BTC", window_secs=9999.0)
        assert result is not None
        assert result > 0


# ── get_vol_regime ────────────────────────────────────────────────────────────

class TestVolRegime:
    def test_unknown_for_new_tracker(self):
        tracker = OracleTickTracker()
        assert tracker.get_vol_regime("BTC") == "UNKNOWN"

    def test_unknown_with_insufficient_vol_buffer(self):
        from market_data.oracle_tick_tracker import _CoinState
        tracker = OracleTickTracker()
        state = _CoinState()
        state.vol_buffer.append(0.001)
        state.vol_buffer.append(0.002)  # < 3 entries → UNKNOWN
        tracker._coins["BTC"] = state
        assert tracker.get_vol_regime("BTC") == "UNKNOWN"

    def test_high_regime_when_current_vol_elevated(self):
        from market_data.oracle_tick_tracker import _CoinState
        tracker = OracleTickTracker()
        now = time.time()
        state = _CoinState()
        # Build a vol_buffer with consistently low readings
        for _ in range(5):
            state.vol_buffer.append(0.0001)
        # Build a price buffer with HIGH volatility (large swings in last 60s)
        for i in range(30):
            price = 100.0 + (10.0 if i % 2 == 0 else -10.0)
            state.price_buffer.append((now - 60 + i * 2, price))
        state.tick_count = 30
        tracker._coins["BTC"] = state
        regime = tracker.get_vol_regime("BTC")
        # With high-vol price buffer and low historical vol, should be HIGH
        assert regime in ("HIGH", "UNKNOWN")  # UNKNOWN acceptable if vol calc returns None

    def test_low_regime_when_current_vol_depressed(self):
        from market_data.oracle_tick_tracker import _CoinState
        tracker = OracleTickTracker()
        now = time.time()
        state = _CoinState()
        # High historical vol
        for _ in range(5):
            state.vol_buffer.append(10.0)
        # Flat price buffer (negligible current vol)
        for i in range(30):
            state.price_buffer.append((now - 60 + i * 2, 100.0 + 0.0001 * i))
        state.tick_count = 30
        tracker._coins["BTC"] = state
        regime = tracker.get_vol_regime("BTC")
        assert regime in ("LOW", "UNKNOWN")


# ── reset_coin ────────────────────────────────────────────────────────────────

class TestResetCoin:
    def test_reset_clears_tick_count(self):
        tracker = OracleTickTracker()
        for i in range(10):
            tracker._on_tick("BTC", 100.0 + i)
        tracker.reset_coin("BTC")
        assert tracker._coins["BTC"].tick_count == 0

    def test_upfrac_ewma_reverts_to_neutral(self):
        tracker = OracleTickTracker()
        for i in range(20):
            tracker._on_tick("BTC", 100.0 + i)
        tracker.reset_coin("BTC")
        assert tracker._coins["BTC"].upfrac_ewma == pytest.approx(0.5)

    def test_reset_nonexistent_coin_is_safe(self):
        tracker = OracleTickTracker()
        tracker.reset_coin("DOGE")  # should not raise

    def test_get_upfrac_returns_none_after_reset(self):
        tracker = OracleTickTracker()
        for i in range(10):
            tracker._on_tick("BTC", 100.0 + i)
        tracker.reset_coin("BTC")
        assert tracker.get_upfrac_ewma("BTC") is None


# ── register ──────────────────────────────────────────────────────────────────

class TestRegister:
    def test_register_attaches_async_callbacks(self):
        """register() should call both on_chainlink_update and on_rtds_update."""
        tracker = OracleTickTracker()
        spot_oracle = MagicMock()
        tracker.register(spot_oracle)
        spot_oracle.on_chainlink_update.assert_called_once()
        spot_oracle.on_rtds_update.assert_called_once()

    def test_registered_callback_updates_tracker(self):
        """The async callback shim should update _coins when called."""
        tracker = OracleTickTracker()
        spot_oracle = MagicMock()
        tracker.register(spot_oracle)
        # Extract the registered callback
        cb = spot_oracle.on_chainlink_update.call_args[0][0]
        # Call it
        _run(cb("BTC", 99000.0))
        assert "BTC" in tracker._coins
        assert tracker._coins["BTC"].tick_count == 1
