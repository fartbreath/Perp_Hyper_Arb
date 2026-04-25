"""
tests/test_spot_oracle.py — Unit tests for SpotOracle.get_mid_resolution_oracle (Phase B).

Run: pytest tests/test_spot_oracle.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from market_data.rtds_client import SpotPrice
from market_data.spot_oracle import SpotOracle, CHAINLINK_MARKET_TYPES


def _make_snap(coin: str, price: float, ts: float = 1_000_000.0) -> SpotPrice:
    return SpotPrice(coin=coin, price=price, timestamp=ts)


def _make_oracle(
    rtds_chainlink_snap: SpotPrice | None,
    cl_ws_snap: SpotPrice | None,
    streams_snap: SpotPrice | None = None,
) -> SpotOracle:
    rtds = MagicMock()
    rtds.get_chainlink_spot = MagicMock(return_value=rtds_chainlink_snap)
    rtds.get_mid = MagicMock(
        return_value=rtds_chainlink_snap.price if rtds_chainlink_snap else None
    )
    rtds.get_spot = MagicMock(return_value=rtds_chainlink_snap)
    rtds.get_spot_age = MagicMock(return_value=0.5)
    rtds.on_chainlink_update = MagicMock()
    rtds.on_price_update = MagicMock()
    rtds.all_mids = MagicMock(return_value={})

    cl = MagicMock()
    cl.get_spot = MagicMock(return_value=cl_ws_snap)
    cl.on_price_update = MagicMock()

    streams = None
    if streams_snap is not None:
        streams = MagicMock()
        streams.get_spot = MagicMock(return_value=streams_snap)
        streams.on_price_update = MagicMock()

    return SpotOracle(rtds=rtds, chainlink=cl, streams=streams)


# ── get_mid_resolution_oracle ──────────────────────────────────────────────────

class TestGetMidResolutionOracle:
    """Phase B: AggregatorV3-only feed for near-expiry resolution matching."""

    def test_chainlink_market_non_hype_returns_cl_ws_price(self):
        """For bucket_5m BTC, should return ChainlinkWSClient price only."""
        cl_snap = _make_snap("BTC", 70_000.0, ts=2_000_000.0)
        rtds_snap = _make_snap("BTC", 70_100.0, ts=2_000_001.0)  # fresher but RTDS relay
        oracle = _make_oracle(rtds_chainlink_snap=rtds_snap, cl_ws_snap=cl_snap)

        result = oracle.get_mid_resolution_oracle("BTC", "bucket_5m")

        # Must use CL WS only — not the fresher RTDS relay
        assert result == pytest.approx(70_000.0)
        oracle._cl.get_spot.assert_called_once_with("BTC")
        oracle._rtds.get_chainlink_spot.assert_not_called()

    def test_returns_cl_ws_price_for_all_chainlink_types(self):
        for mtype in CHAINLINK_MARKET_TYPES:
            cl_snap = _make_snap("ETH", 3_500.0)
            oracle = _make_oracle(rtds_chainlink_snap=None, cl_ws_snap=cl_snap)
            assert oracle.get_mid_resolution_oracle("ETH", mtype) == pytest.approx(3_500.0)

    def test_hype_falls_back_to_get_mid(self):
        """HYPE has no Polygon AggregatorV3 — must fall back to freshest-wins."""
        rtds_snap = _make_snap("HYPE", 25.0, ts=2_000_001.0)
        oracle = _make_oracle(rtds_chainlink_snap=rtds_snap, cl_ws_snap=None)

        result = oracle.get_mid_resolution_oracle("HYPE", "bucket_5m")

        # Falls back to standard get_mid; RTDS snap is fresh so RTDS wins — CL never called
        assert result == pytest.approx(25.0)
        oracle._cl.get_spot.assert_not_called()

    def test_non_chainlink_market_type_falls_back_to_get_mid(self):
        """bucket_1h is RTDS-only — resolution oracle falls back to standard."""
        rtds = MagicMock()
        rtds.get_mid = MagicMock(return_value=70_000.0)
        rtds.get_spot = MagicMock(return_value=_make_snap("BTC", 70_000.0))
        rtds.get_spot_age = MagicMock(return_value=1.0)
        rtds.get_chainlink_spot = MagicMock(return_value=None)
        rtds.on_chainlink_update = MagicMock()
        rtds.on_price_update = MagicMock()
        rtds.all_mids = MagicMock(return_value={})

        cl = MagicMock()
        cl.get_spot = MagicMock(return_value=None)
        cl.on_price_update = MagicMock()

        oracle = SpotOracle(rtds=rtds, chainlink=cl)

        result = oracle.get_mid_resolution_oracle("BTC", "bucket_1h")

        # Not a Chainlink market type → fall back to get_mid → RTDS
        assert result == pytest.approx(70_000.0)
        rtds.get_mid.assert_called_once_with("BTC")

    def test_returns_none_when_cl_ws_has_no_data_yet(self):
        """If ChainlinkWSClient has no snapshot yet, return None (no silent fallback)."""
        oracle = _make_oracle(rtds_chainlink_snap=_make_snap("BTC", 70_000.0), cl_ws_snap=None)
        result = oracle.get_mid_resolution_oracle("BTC", "bucket_5m")
        assert result is None

    def test_cl_ws_snap_with_none_price_handled(self):
        """Edge: cl.get_spot returns None (never seeded) → None."""
        oracle = _make_oracle(rtds_chainlink_snap=None, cl_ws_snap=None)
        assert oracle.get_mid_resolution_oracle("ETH", "bucket_15m") is None

    def test_differs_from_get_mid_when_rtds_is_fresher(self):
        """Demonstrate the key Phase B behavioural difference:
        get_mid returns RTDS; get_mid_resolution_oracle returns AggregatorV3."""
        cl_snap = _make_snap("BTC", 70_000.0, ts=1_000_000.0)
        rtds_snap = _make_snap("BTC", 70_100.0, ts=2_000_000.0)  # 1M seconds newer
        oracle = _make_oracle(rtds_chainlink_snap=rtds_snap, cl_ws_snap=cl_snap)

        standard = oracle.get_mid("BTC", "bucket_5m")
        resolution = oracle.get_mid_resolution_oracle("BTC", "bucket_5m")

        assert standard == pytest.approx(70_100.0)   # explicit priority: streams(None) → RTDS wins
        assert resolution == pytest.approx(70_000.0)  # AggregatorV3 only


# ── get_mid uses explicit priority (not broken by Phase B) ───────────────────

class TestGetMidUnchanged:
    def test_get_mid_still_picks_rtds_when_no_streams(self):
        cl_snap = _make_snap("ETH", 3_400.0, ts=1_000.0)
        rtds_snap = _make_snap("ETH", 3_450.0, ts=2_000.0)
        oracle = _make_oracle(rtds_chainlink_snap=rtds_snap, cl_ws_snap=cl_snap)
        assert oracle.get_mid("ETH", "bucket_15m") == pytest.approx(3_450.0)

    def test_get_mid_chainlink_non_hype_rtds_before_cl_ws(self):
        cl_snap = _make_snap("BTC", 68_000.0, ts=5_000.0)
        rtds_snap = _make_snap("BTC", 68_500.0, ts=4_000.0)  # older timestamp but higher priority
        oracle = _make_oracle(rtds_chainlink_snap=rtds_snap, cl_ws_snap=cl_snap)
        # Explicit priority: streams (None) → RTDS → cl_ws; RTDS wins regardless of timestamp
        assert oracle.get_mid("BTC", "bucket_4h") == pytest.approx(68_500.0)
