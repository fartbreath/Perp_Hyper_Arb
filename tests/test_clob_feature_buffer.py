"""
tests/test_clob_feature_buffer.py — Unit tests for models/clob_feature_buffer.py (ML-01)

Tests cover all 6 acceptance criteria from ML_PRD.md ML-01:
  1. buffer-receives-events
  2. features-computed-after-30s-of-events
  3. premarket-baseline-captured
  4. buffer-cleared-on-close
  5. error-isolation (exception in callback never propagates)
  6. disabled-is-clean (when CLOB_FEATURE_BUFFER_ENABLED=False, no side effects)

Run:  pytest tests/test_clob_feature_buffer.py -v
"""
import asyncio
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from models.clob_feature_buffer import CLOBFeatureBuffer, _NULL_FEATURES


# ── Test doubles ──────────────────────────────────────────────────────────────

@dataclass
class _FakePMMarket:
    condition_id: str
    token_id_yes: str
    token_id_no: str


@dataclass
class _FakeOrderBookSnapshot:
    token_id: str
    bids: list = field(default_factory=list)  # list of (price, size)
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None


class _FakePMClient:
    """Minimal PMClient stand-in for unit tests."""

    def __init__(self):
        self._books: dict[str, _FakeOrderBookSnapshot] = {}
        self._markets: dict[str, _FakePMMarket] = {}
        self._price_callbacks: list = []

    def on_price_change(self, callback):
        self._price_callbacks.append(callback)

    def get_book(self, token_id: str) -> Optional[_FakeOrderBookSnapshot]:
        return self._books.get(token_id)

    def get_markets(self) -> dict:
        return dict(self._markets)

    def set_book(self, token_id: str, bids: list[tuple[float, float]]) -> None:
        self._books[token_id] = _FakeOrderBookSnapshot(token_id=token_id, bids=bids)

    def add_market(self, condition_id: str, tid_yes: str, tid_no: str) -> None:
        self._markets[condition_id] = _FakePMMarket(
            condition_id=condition_id,
            token_id_yes=tid_yes,
            token_id_no=tid_no,
        )

    async def fire_price_update(self, token_id: str, mid: float) -> None:
        for cb in self._price_callbacks:
            await cb(token_id, mid)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _enabled_buffer(pm: _FakePMClient, maxlen: int = 600) -> CLOBFeatureBuffer:
    """Create a CLOBFeatureBuffer with the enabled flag patched True."""
    with patch.object(config, "CLOB_FEATURE_BUFFER_ENABLED", True), \
         patch.object(config, "CLOB_BUFFER_MAXLEN", maxlen):
        buf = CLOBFeatureBuffer()
        buf.register(pm)
    return buf


def _inject_ticks(
    buf: CLOBFeatureBuffer,
    token_id: str,
    ticks: list[tuple[float, float, float]],  # (ts, best_bid, total_bid_size)
) -> None:
    """Directly inject pre-fabricated ticks into the buffer's internal deque."""
    if token_id not in buf._buffers:
        buf._buffers[token_id] = deque(maxlen=buf._maxlen)
    buf._buffers[token_id].extend(ticks)


# ── AC1: buffer-receives-events ───────────────────────────────────────────────

class TestBufferReceivesEvents:
    def test_single_event_stored(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        pm.set_book("yes1", [(0.70, 100.0)])
        buf = _enabled_buffer(pm)

        _run(pm.fire_price_update("yes1", 0.70))

        assert len(buf._buffers.get("yes1", [])) == 1

    def test_multiple_events_accumulate(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        pm.set_book("yes1", [(0.70, 100.0)])
        buf = _enabled_buffer(pm)

        for price in [0.70, 0.68, 0.65]:
            pm.set_book("yes1", [(price, 100.0)])
            _run(pm.fire_price_update("yes1", price))

        assert len(buf._buffers["yes1"]) == 3

    def test_depth_method_reflects_buffer(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        pm.set_book("yes1", [(0.70, 100.0)])
        buf = _enabled_buffer(pm)

        _run(pm.fire_price_update("yes1", 0.70))
        _run(pm.fire_price_update("yes1", 0.68))

        assert buf.depth("mkt1") == 2

    def test_total_bid_size_stored(self):
        """total_bid_size is the sum of all bid sizes, not just best bid."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        # Three bid levels: sizes 50 + 75 + 25 = 150
        pm.set_book("yes1", [(0.70, 50.0), (0.69, 75.0), (0.68, 25.0)])
        buf = _enabled_buffer(pm)

        _run(pm.fire_price_update("yes1", 0.70))

        ts, best_bid, total_size = buf._buffers["yes1"][-1]
        assert best_bid == pytest.approx(0.70)
        assert total_size == pytest.approx(150.0)

    def test_maxlen_enforced(self):
        """Buffer never exceeds CLOB_BUFFER_MAXLEN entries."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        pm.set_book("yes1", [(0.70, 100.0)])
        buf = _enabled_buffer(pm, maxlen=5)

        for _ in range(10):
            _run(pm.fire_price_update("yes1", 0.70))

        assert len(buf._buffers["yes1"]) == 5

    def test_no_book_no_crash(self):
        """No book in pm_client → no tick stored, no exception."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        # Deliberately not calling set_book
        buf = _enabled_buffer(pm)

        _run(pm.fire_price_update("yes1", 0.70))  # must not raise

        assert len(buf._buffers.get("yes1", [])) == 0


# ── AC2: features-computed-after-30s-of-events ───────────────────────────────

class TestFeaturesComputed:
    def _make_buf_with_collapse(self) -> tuple[CLOBFeatureBuffer, _FakePMClient]:
        """Buffer with 70s of ticks simulating a bid collapse on YES."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        now = time.time()
        # YES: bid falls from 0.70 → 0.35 over 70s; depth falls 500→50
        # 11 ticks, 7s apart, spanning now-70 → now
        yes_ticks = [
            (now - 70 + i * 7, 0.70 - i * 0.035, 500.0 - i * 45.0)
            for i in range(11)
        ]
        # NO: stable bid ~0.30
        no_ticks = [
            (now - 70 + i * 7, 0.30, 400.0)
            for i in range(11)
        ]
        _inject_ticks(buf, "yes1", yes_ticks)
        _inject_ticks(buf, "no1", no_ticks)
        return buf, pm

    def test_bid_slope_is_negative_on_collapse(self):
        buf, pm = self._make_buf_with_collapse()
        features = buf.compute_features("mkt1")
        assert features["yes_bid_slope_30s"] is not None
        assert features["yes_bid_slope_30s"] < 0, "YES bid is collapsing — slope must be negative"

    def test_stable_bid_slope_near_zero(self):
        buf, pm = self._make_buf_with_collapse()
        features = buf.compute_features("mkt1")
        assert features["no_bid_slope_30s"] is not None
        assert abs(features["no_bid_slope_30s"]) < 0.001, "NO bid is stable — slope should be near 0"

    def test_depth_delta_negative_on_collapse(self):
        buf, pm = self._make_buf_with_collapse()
        features = buf.compute_features("mkt1")
        assert features["yes_depth_delta_60s"] is not None
        assert features["yes_depth_delta_60s"] < 0, "YES depth is draining — delta must be negative"

    def test_bid_at_level_is_current(self):
        buf, pm = self._make_buf_with_collapse()
        features = buf.compute_features("mkt1")
        now = time.time()
        # Most recent YES tick: bid ≈ 0.70 - 10 * 0.035 = 0.35
        assert features["yes_bid_at_level"] == pytest.approx(0.35, abs=0.001)

    def test_returns_null_when_fewer_than_2_ticks(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        # Inject only one tick — should not be enough for slope computation
        _inject_ticks(buf, "yes1", [(time.time(), 0.70, 100.0)])

        features = buf.compute_features("mkt1")
        # bid_slope requires ≥2 ticks in the 30s window — should be None
        assert features["yes_bid_slope_30s"] is None

    def test_returns_null_when_no_ticks(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)

        features = buf.compute_features("mkt1")
        for v in features.values():
            assert v is None

    def test_unknown_market_returns_null_dict(self):
        pm = _FakePMClient()
        buf = _enabled_buffer(pm)

        features = buf.compute_features("does_not_exist")
        assert features == dict(_NULL_FEATURES)


# ── AC3: premarket-baseline-captured ─────────────────────────────────────────

class TestPremarketBaseline:
    def test_baseline_stored_on_set(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        _inject_ticks(buf, "yes1", [(time.time(), 0.50, 200.0)])
        _inject_ticks(buf, "no1", [(time.time(), 0.50, 200.0)])

        buf.set_premarket_baseline("mkt1")

        assert "mkt1" in buf._premarket_baseline
        assert buf._premarket_baseline["mkt1"]["yes"] is not None
        assert buf._premarket_baseline["mkt1"]["no"] is not None

    def test_bid_vs_baseline_above_one_when_bid_rises(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        # Baseline: YES bid = 0.50
        _inject_ticks(buf, "yes1", [(time.time() - 10, 0.50, 200.0)])
        buf.set_premarket_baseline("mkt1")
        # Current: YES bid = 0.70 (rose)
        _inject_ticks(buf, "yes1", [(time.time(), 0.70, 200.0)])

        features = buf.compute_features("mkt1")
        assert features["yes_bid_vs_premarket_baseline"] == pytest.approx(0.70 / 0.50)

    def test_bid_vs_baseline_below_one_when_bid_falls(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        # Baseline: YES bid = 0.70
        _inject_ticks(buf, "yes1", [(time.time() - 10, 0.70, 200.0)])
        buf.set_premarket_baseline("mkt1")
        # Current: YES bid = 0.35 (collapsed)
        _inject_ticks(buf, "yes1", [(time.time(), 0.35, 50.0)])

        features = buf.compute_features("mkt1")
        assert features["yes_bid_vs_premarket_baseline"] == pytest.approx(0.35 / 0.70)

    def test_baseline_none_when_no_ticks(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        # No ticks yet — baseline tick will be None
        buf.set_premarket_baseline("mkt1")
        _inject_ticks(buf, "yes1", [(time.time(), 0.70, 200.0)])

        features = buf.compute_features("mkt1")
        assert features["yes_bid_vs_premarket_baseline"] is None

    def test_baseline_on_unknown_market_is_noop(self):
        pm = _FakePMClient()
        buf = _enabled_buffer(pm)
        buf.set_premarket_baseline("nonexistent")  # must not raise


# ── AC4: buffer-cleared-on-close ─────────────────────────────────────────────

class TestBufferClearedOnClose:
    def test_clear_empties_both_sides(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        _inject_ticks(buf, "yes1", [(time.time(), 0.70, 200.0)])
        _inject_ticks(buf, "no1", [(time.time(), 0.30, 200.0)])

        buf.clear("mkt1")

        assert buf.depth("mkt1") == 0
        assert "yes1" not in buf._buffers
        assert "no1" not in buf._buffers

    def test_clear_removes_premarket_baseline(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        _inject_ticks(buf, "yes1", [(time.time(), 0.50, 200.0)])
        buf.set_premarket_baseline("mkt1")
        assert "mkt1" in buf._premarket_baseline

        buf.clear("mkt1")

        assert "mkt1" not in buf._premarket_baseline

    def test_compute_returns_null_after_clear(self):
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)
        _inject_ticks(buf, "yes1", [(time.time() - 5, 0.70, 200.0), (time.time(), 0.68, 180.0)])
        _inject_ticks(buf, "no1", [(time.time() - 5, 0.30, 200.0), (time.time(), 0.30, 200.0)])

        buf.clear("mkt1")
        features = buf.compute_features("mkt1")

        for v in features.values():
            assert v is None

    def test_clear_unknown_market_is_noop(self):
        pm = _FakePMClient()
        buf = _enabled_buffer(pm)
        buf.clear("nonexistent")  # must not raise


# ── AC5: error-isolation ──────────────────────────────────────────────────────

class TestErrorIsolation:
    def test_broken_get_book_does_not_propagate(self):
        """If get_book raises, the exception must not propagate to the caller."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")

        def _boom(token_id: str):
            raise RuntimeError("simulated get_book failure")

        pm.get_book = _boom
        buf = _enabled_buffer(pm)

        # Should not raise; exception is caught internally
        _run(pm.fire_price_update("yes1", 0.70))

    def test_broken_callback_does_not_affect_other_callbacks(self):
        """A raise in our callback must not break a second registered callback."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")

        sentinel = []
        async def _good_callback(token_id: str, mid: float) -> None:
            sentinel.append((token_id, mid))

        pm.on_price_change(_good_callback)
        # Register a buffer whose get_book will raise
        def _boom(token_id: str):
            raise RuntimeError("simulated failure")
        pm.get_book = _boom
        buf = _enabled_buffer(pm)

        _run(pm.fire_price_update("yes1", 0.70))

        assert len(sentinel) == 1, "good callback must still have fired"

    def test_compute_features_exception_returns_null(self):
        """If PMClient raises inside compute_features, return null dict."""
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        buf = _enabled_buffer(pm)

        def _boom():
            raise RuntimeError("simulated get_markets failure")

        pm.get_markets = _boom

        result = buf.compute_features("mkt1")
        assert result == dict(_NULL_FEATURES)


# ── AC6: disabled-is-clean ────────────────────────────────────────────────────

class TestDisabledIsClean:
    def _disabled_buffer(self) -> tuple[CLOBFeatureBuffer, _FakePMClient]:
        pm = _FakePMClient()
        pm.add_market("mkt1", "yes1", "no1")
        with patch.object(config, "CLOB_FEATURE_BUFFER_ENABLED", False):
            buf = CLOBFeatureBuffer()
            buf.register(pm)
        return buf, pm

    def test_no_callback_registered_when_disabled(self):
        buf, pm = self._disabled_buffer()
        assert len(pm._price_callbacks) == 0

    def test_compute_returns_null_when_disabled(self):
        buf, pm = self._disabled_buffer()
        result = buf.compute_features("mkt1")
        assert result == dict(_NULL_FEATURES)

    def test_depth_returns_zero_when_disabled(self):
        buf, pm = self._disabled_buffer()
        assert buf.depth("mkt1") == 0

    def test_clear_is_noop_when_disabled(self):
        buf, pm = self._disabled_buffer()
        buf.clear("mkt1")  # must not raise

    def test_set_premarket_baseline_is_noop_when_disabled(self):
        buf, pm = self._disabled_buffer()
        buf.set_premarket_baseline("mkt1")  # must not raise
        assert buf._premarket_baseline == {}
