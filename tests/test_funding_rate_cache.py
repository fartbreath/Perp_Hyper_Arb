"""
tests/test_funding_rate_cache.py — Unit tests for market_data/funding_rate_cache.py

Run:  pytest tests/test_funding_rate_cache.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import time
import pytest
from market_data.funding_rate_cache import FundingRateCache


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_cache(stale_s: float = 120.0) -> FundingRateCache:
    return FundingRateCache(stale_threshold_s=stale_s)


# ── on_ws_update / get ────────────────────────────────────────────────────────

class TestGet:
    def test_get_returns_none_before_any_update(self):
        cache = _make_cache()
        assert cache.get("BTC") is None

    def test_get_returns_rate_after_update(self):
        cache = _make_cache()
        cache.on_ws_update("BTC", 0.0001, time.time())
        assert cache.get("BTC") == pytest.approx(0.0001)

    def test_get_returns_none_when_stale(self):
        cache = _make_cache(stale_s=5.0)
        stale_ts = time.time() - 10.0  # 10 seconds ago — older than threshold
        cache.on_ws_update("BTC", 0.0001, stale_ts)
        assert cache.get("BTC") is None

    def test_get_returns_rate_when_fresh(self):
        cache = _make_cache(stale_s=60.0)
        cache.on_ws_update("ETH", 0.00005, time.time())
        assert cache.get("ETH") is not None

    def test_multiple_coins_independent(self):
        cache = _make_cache()
        now = time.time()
        cache.on_ws_update("BTC", 0.0001, now)
        cache.on_ws_update("ETH", -0.00005, now)
        assert cache.get("BTC") == pytest.approx(0.0001)
        assert cache.get("ETH") == pytest.approx(-0.00005)


# ── is_stale ──────────────────────────────────────────────────────────────────

class TestIsStale:
    def test_stale_when_never_updated(self):
        cache = _make_cache()
        assert cache.is_stale("BTC") is True

    def test_fresh_after_recent_update(self):
        cache = _make_cache(stale_s=120.0)
        cache.on_ws_update("BTC", 0.0001, time.time())
        assert cache.is_stale("BTC") is False

    def test_stale_after_old_update(self):
        cache = _make_cache(stale_s=5.0)
        cache.on_ws_update("BTC", 0.0001, time.time() - 10.0)
        assert cache.is_stale("BTC") is True


# ── get_direction ─────────────────────────────────────────────────────────────

class TestGetDirection:
    def test_none_before_update(self):
        cache = _make_cache()
        assert cache.get_direction("BTC") is None

    def test_positive_direction(self):
        cache = _make_cache()
        cache.on_ws_update("BTC", 0.0001, time.time())
        assert cache.get_direction("BTC") == "POSITIVE"

    def test_negative_direction(self):
        cache = _make_cache()
        cache.on_ws_update("BTC", -0.0001, time.time())
        assert cache.get_direction("BTC") == "NEGATIVE"

    def test_neutral_direction(self):
        cache = _make_cache()
        cache.on_ws_update("BTC", 0.0, time.time())
        assert cache.get_direction("BTC") == "NEUTRAL"

    def test_none_when_stale(self):
        cache = _make_cache(stale_s=5.0)
        cache.on_ws_update("BTC", 0.0001, time.time() - 10.0)
        # is_stale → True, get_direction should return None
        assert cache.get_direction("BTC") is None


# ── get_history ────────────────────────────────────────────────────────────────

class TestGetHistory:
    def test_empty_before_update(self):
        cache = _make_cache()
        assert cache.get_history("BTC") == []

    def test_history_grows(self):
        cache = _make_cache()
        t = time.time()
        cache.on_ws_update("BTC", 0.0001, t)
        cache.on_ws_update("BTC", 0.0002, t + 1)
        h = cache.get_history("BTC")
        assert len(h) == 2
        assert h[0][1] == pytest.approx(0.0001)
        assert h[1][1] == pytest.approx(0.0002)

    def test_history_capped_at_10(self):
        cache = _make_cache()
        t = time.time()
        for i in range(15):
            cache.on_ws_update("BTC", float(i) * 0.00001, t + i)
        assert len(cache.get_history("BTC")) == 10


# ── fresh_count ────────────────────────────────────────────────────────────────

class TestFreshCount:
    def test_zero_when_no_data(self):
        cache = _make_cache()
        assert cache.fresh_count(["BTC", "ETH"]) == 0

    def test_counts_fresh_coins(self):
        cache = _make_cache(stale_s=120.0)
        now = time.time()
        cache.on_ws_update("BTC", 0.0001, now)
        cache.on_ws_update("ETH", 0.00005, now)
        assert cache.fresh_count(["BTC", "ETH", "SOL"]) == 2

    def test_stale_not_counted(self):
        cache = _make_cache(stale_s=5.0)
        cache.on_ws_update("BTC", 0.0001, time.time() - 10.0)
        cache.on_ws_update("ETH", 0.00005, time.time())
        assert cache.fresh_count(["BTC", "ETH"]) == 1


# ── last_update_ts ────────────────────────────────────────────────────────────

class TestLastUpdateTs:
    def test_none_before_any_update(self):
        cache = _make_cache()
        assert cache.last_update_ts() is None

    def test_returns_most_recent_ts(self):
        cache = _make_cache()
        t1 = time.time() - 5.0
        t2 = time.time()
        cache.on_ws_update("BTC", 0.0001, t1)
        cache.on_ws_update("ETH", 0.00005, t2)
        # last_update_ts returns the most recent across all coins
        assert cache.last_update_ts() == pytest.approx(t2, abs=0.01)
