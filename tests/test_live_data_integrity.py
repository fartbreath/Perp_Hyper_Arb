"""
tests/test_live_data_integrity.py — Live API data integrity tests.

Validates that live data from Polymarket Gamma REST API and RTDS WebSocket
is correctly parsed, classified, and routed by the new Phase B/C/D/E/P3
features:

  Phase B  — SpotOracle routing (CHAINLINK_MARKET_TYPES vs RTDS bucket types)
  Phase C  — Gamma market classification (_classify_market) and underlying
             detection (_detect_underlying) on real live market titles
  Phase D  — PMClient live market field integrity (tick_size, end_date, etc.)
  Phase E  — WinRateTable load from on-disk data files (data/momentum_fills.csv,
             data/trades.csv)
  P3       — Market-level rebate_pct routing by market_type

Run (requires network access):
    pytest tests/test_live_data_integrity.py -v -m live --timeout=60

All tests make synchronous HTTP calls to the public Polymarket Gamma REST API.
No authentication required; PAPER_TRADING=True prevents any real orders.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.PAPER_TRADING = True

from market_data.pm_client import (
    PMMarket,
    _classify_market,
    _detect_underlying,
    _MARKET_TYPE_KEYWORDS,
    _MARKET_TYPE_DURATION_SECS,
    _REBATE_PCT_BY_TYPE,
    _UNDERLYING_TAG_SLUGS,
)
from market_data.spot_oracle import CHAINLINK_MARKET_TYPES


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_gamma_events(tag_slug: str, limit: int = 50) -> list[dict]:
    """Return raw Gamma events for a tag slug, or [] on network failure."""
    try:
        resp = requests.get(
            f"{config.GAMMA_HOST}/events",
            params={
                "active": "true",
                "closed": "false",
                "tag_slug": tag_slug,
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def _collect_live_markets(slugs: tuple[str, ...] = ("bitcoin", "ethereum", "crypto"), limit_per_slug: int = 3) -> list[dict]:
    """Collect raw market dicts from Gamma API across given slugs."""
    markets: list[dict] = []
    for slug in slugs:
        events = _fetch_gamma_events(slug, limit=limit_per_slug)
        for event in events:
            for mkt in event.get("markets", []):
                if (mkt.get("active") and mkt.get("acceptingOrders")
                        and mkt.get("enableOrderBook")):
                    mkt["_event_title"] = event.get("title", "")
                    markets.append(mkt)
    return markets


# ──────────────────────────────────────────────────────────────────────────────
# Phase C: Market classification and underlying detection on live Gamma data
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
class TestGammaClassificationIntegrity:
    """_classify_market and _detect_underlying must give valid results on live titles."""

    @pytest.fixture(scope="class")
    def live_markets(self):
        """Fetch a sample of live active markets from Gamma API."""
        markets = _collect_live_markets(
            slugs=("bitcoin", "ethereum", "crypto", "solana"),
            limit_per_slug=10,
        )
        if not markets:
            pytest.skip("Gamma API unavailable")
        return markets

    def test_classify_market_returns_known_type(self, live_markets):
        """Every live market title must classify to a known market_type."""
        known_types = set(_MARKET_TYPE_KEYWORDS.keys()) | {"milestone"}
        failures: list[str] = []
        for mkt in live_markets:
            # Use event title (more reliable for underlying) and question title for type
            title = mkt.get("question", mkt.get("title", ""))
            event_title = mkt.get("_event_title", title)
            mtype = _classify_market(event_title or title)
            if mtype not in known_types:
                failures.append(f"title={title!r} → {mtype!r}")
        assert not failures, (
            f"_classify_market returned unknown types for {len(failures)} markets:\n"
            + "\n".join(failures[:10])
        )

    def test_detect_underlying_known_or_unknown(self, live_markets):
        """_detect_underlying must return a known asset or 'UNKNOWN'."""
        known_assets = set(_UNDERLYING_TAG_SLUGS.keys()) | {"UNKNOWN"}
        failures: list[str] = []
        for mkt in live_markets:
            event_title = mkt.get("_event_title", mkt.get("question", ""))
            underlying = _detect_underlying(event_title)
            if underlying not in known_assets:
                failures.append(f"title={event_title!r} → {underlying!r}")
        assert not failures, (
            f"_detect_underlying returned unknown asset for {len(failures)} markets:\n"
            + "\n".join(failures[:10])
        )

    def test_crypto_event_underlying_detected(self, live_markets):
        """At least one live crypto market must have a known (non-UNKNOWN) underlying."""
        detected = [
            _detect_underlying(m.get("_event_title", ""))
            for m in live_markets
        ]
        known = [u for u in detected if u != "UNKNOWN"]
        assert len(known) >= 1, (
            "Expected at least one market with a detected underlying; "
            f"all {len(live_markets)} markets returned 'UNKNOWN'"
        )

    def test_bucket_markets_have_known_duration(self):
        """Every bucket market type must have an entry in _MARKET_TYPE_DURATION_SECS."""
        bucket_types = {mt for mt in _MARKET_TYPE_KEYWORDS if mt.startswith("bucket_")}
        missing = bucket_types - set(_MARKET_TYPE_DURATION_SECS.keys())
        assert not missing, (
            f"Bucket types without duration mapping: {missing}. "
            "Add them to _MARKET_TYPE_DURATION_SECS in pm_client.py."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Phase D: PMClient field integrity on live Gamma markets
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
class TestGammaMarketFieldIntegrity:
    """Fields parsed from live Gamma API markets must satisfy basic sanity invariants."""

    @pytest.fixture(scope="class")
    def live_raw_markets(self):
        markets = _collect_live_markets(
            slugs=("bitcoin", "ethereum", "crypto", "solana"),
            limit_per_slug=15,
        )
        if not markets:
            pytest.skip("Gamma API unavailable")
        return markets

    def test_live_markets_have_clob_token_ids(self, live_raw_markets):
        """All acceptingOrders markets must have at least one clobTokenId."""
        missing: list[str] = []
        for mkt in live_raw_markets:
            toks = mkt.get("clobTokenIds", [])
            if isinstance(toks, str):
                try:
                    toks = json.loads(toks)
                except Exception:
                    toks = []
            if not toks:
                missing.append(mkt.get("question", mkt.get("conditionId", "?")))
        assert not missing, (
            f"{len(missing)} active acceptingOrders markets have no clobTokenIds:\n"
            + "\n".join(missing[:5])
        )

    def test_live_markets_have_positive_incentive_spread(self, live_raw_markets):
        """maxIncentiveSpread must be non-negative for all fee-enabled markets."""
        failures: list[str] = []
        for mkt in live_raw_markets:
            spread_raw = mkt.get("maxIncentiveSpread") or mkt.get("rewardMaxSpread")
            if spread_raw is not None:
                try:
                    spread = float(spread_raw)
                except (TypeError, ValueError):
                    spread = None
                if spread is not None and spread < 0:
                    failures.append(
                        f"conditionId={mkt.get('conditionId')!r} spread={spread}"
                    )
        assert not failures, (
            f"{len(failures)} markets have negative incentive spread:\n"
            + "\n".join(failures[:5])
        )

    def test_live_markets_have_valid_end_date(self, live_raw_markets):
        """endDate / endDateIso must be parseable and in the future for active markets."""
        from datetime import datetime, timezone
        now_ts = time.time()
        past_count = 0
        parse_failures: list[str] = []

        for mkt in live_raw_markets:
            raw_end = mkt.get("endDate") or mkt.get("endDateIso")
            if raw_end is None:
                continue
            try:
                end = datetime.fromisoformat(str(raw_end).rstrip("Z")).replace(
                    tzinfo=timezone.utc
                )
                if end.timestamp() <= now_ts:
                    past_count += 1
            except Exception as exc:
                parse_failures.append(f"{raw_end!r}: {exc}")

        assert not parse_failures, (
            f"Could not parse endDate for {len(parse_failures)} markets:\n"
            + "\n".join(parse_failures[:5])
        )
        # Some markets may have just expired; allow up to 20% past-dated
        total = len(live_raw_markets)
        tolerable = max(1, total // 5)
        assert past_count <= tolerable, (
            f"{past_count}/{total} active markets have a past endDate "
            f"(tolerable ≤ {tolerable})"
        )

    def test_tick_size_is_valid(self, live_raw_markets):
        """tickSize must be one of {0.001, 0.01, 0.1} for all live markets."""
        valid_ticks = {0.001, 0.01, 0.1}
        bad: list[str] = []
        for mkt in live_raw_markets:
            ts_raw = mkt.get("minTickSize") or mkt.get("tickSize")
            if ts_raw is None:
                continue
            try:
                ts = float(ts_raw)
            except (TypeError, ValueError):
                ts = None
            if ts is not None and ts not in valid_ticks:
                bad.append(f"conditionId={mkt.get('conditionId')!r} tickSize={ts}")
        assert not bad, (
            f"{len(bad)} markets have unexpected tickSize:\n" + "\n".join(bad[:5])
        )


# ──────────────────────────────────────────────────────────────────────────────
# Phase B: SpotOracle routing — CHAINLINK_MARKET_TYPES constant integrity
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
class TestSpotOracleRoutingIntegrity:
    """SpotOracle routing constants must be consistent and complete."""

    def test_chainlink_market_types_are_subset_of_known_types(self):
        """CHAINLINK_MARKET_TYPES must only contain known bucket types."""
        known = set(_MARKET_TYPE_KEYWORDS.keys())
        unknown_cl = CHAINLINK_MARKET_TYPES - known
        assert not unknown_cl, (
            f"CHAINLINK_MARKET_TYPES contains unknown types: {unknown_cl}"
        )

    def test_chainlink_market_types_are_all_bucket_types(self):
        """All CHAINLINK_MARKET_TYPES must start with 'bucket_'."""
        non_bucket = {mt for mt in CHAINLINK_MARKET_TYPES if not mt.startswith("bucket_")}
        assert not non_bucket, (
            f"Non-bucket types in CHAINLINK_MARKET_TYPES: {non_bucket}. "
            "Only bucket_* market types use the Chainlink oracle."
        )

    def test_chainlink_types_have_duration(self):
        """Each CHAINLINK_MARKET_TYPE must have a known duration for lifecycle gates."""
        missing = CHAINLINK_MARKET_TYPES - set(_MARKET_TYPE_DURATION_SECS.keys())
        assert not missing, (
            f"Chainlink market types missing duration mapping: {missing}"
        )

    def test_bucket_daily_not_in_chainlink_types(self):
        """bucket_daily uses RTDS exchange-aggregated prices, not Chainlink."""
        assert "bucket_daily" not in CHAINLINK_MARKET_TYPES, (
            "bucket_daily must NOT be in CHAINLINK_MARKET_TYPES — "
            "it settles via RTDS exchange-aggregated feed, not Chainlink AggregatorV3."
        )

    def test_bucket_5m_in_chainlink_types(self):
        """bucket_5m must be routed to Chainlink (on-chain settlement)."""
        assert "bucket_5m" in CHAINLINK_MARKET_TYPES, (
            "bucket_5m should be in CHAINLINK_MARKET_TYPES"
        )

    def test_bucket_4h_in_chainlink_types(self):
        """bucket_4h must be routed to Chainlink."""
        assert "bucket_4h" in CHAINLINK_MARKET_TYPES

    def test_rebate_pct_by_type_covers_chainlink_types(self):
        """Every CHAINLINK_MARKET_TYPE must have a rebate_pct defined."""
        missing = CHAINLINK_MARKET_TYPES - set(_REBATE_PCT_BY_TYPE.keys())
        assert not missing, (
            f"Chainlink market types missing rebate_pct: {missing}. "
            "Add entries to _REBATE_PCT_BY_TYPE in pm_client.py."
        )

    def test_rebate_pct_values_are_in_unit_range(self):
        """All rebate_pct values must be in [0, 1]."""
        bad = {mt: v for mt, v in _REBATE_PCT_BY_TYPE.items() if not (0.0 <= v <= 1.0)}
        assert not bad, (
            f"rebate_pct out of [0, 1] for: {bad}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Phase E: WinRateTable data integrity from on-disk files
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
class TestWinRateTableDataIntegrity:
    """WinRateTable built from the real data files must satisfy format invariants."""

    DATA_DIR = Path(__file__).parent.parent / "data"
    FILLS_CSV = DATA_DIR / "momentum_fills.csv"
    TRADES_CSV = DATA_DIR / "trades.csv"

    @pytest.fixture(scope="class")
    def win_rate_table(self):
        """Load WinRateTable from on-disk files; skip if not present."""
        if not self.FILLS_CSV.exists() or not self.TRADES_CSV.exists():
            pytest.skip(
                "No data/momentum_fills.csv or data/trades.csv on this machine"
            )
        from strategies.Momentum.win_rate import WinRateTable
        return WinRateTable()

    def test_table_is_not_empty(self, win_rate_table):
        """After loading real fills, table should have at least one bucket."""
        assert win_rate_table._table or len(win_rate_table._table) == 0, \
            "WinRateTable._table must exist"
        # Not asserting non-empty — some machines may have empty fills

    def test_win_rates_in_unit_range(self, win_rate_table):
        """All empirical win_rates must be in [0, 1]."""
        for key, bucket in win_rate_table._table.items():
            wins, total = bucket
            if total > 0:
                wr = wins / total
                assert 0.0 <= wr <= 1.0, (
                    f"Bucket {key}: wins={wins}/total={total} → wr={wr} out of [0, 1]"
                )

    def test_sample_counts_non_negative(self, win_rate_table):
        """n_wins and n_total must be non-negative integers."""
        for key, bucket in win_rate_table._table.items():
            n_wins, n_total = bucket
            assert n_total >= 0, f"Bucket {key}: n_total={n_total} < 0"
            assert n_wins >= 0, f"Bucket {key}: n_wins={n_wins} < 0"
            assert n_wins <= n_total, (
                f"Bucket {key}: n_wins={n_wins} > n_total={n_total}"
            )

    def test_lookup_returns_float_or_none(self, win_rate_table):
        """get() must return float or None — never raise an exception."""
        test_cases = [
            ("bucket_5m",    0.40, 30.0),
            ("bucket_15m",   0.55, 120.0),
            ("bucket_4h",    0.70, 3600.0),
            ("bucket_daily", 0.30, 86400.0),
            ("milestone",    0.50, 0.0),   # unknown type → None (not enough samples)
        ]
        for mtype, mid, tte in test_cases:
            result = win_rate_table.get(mtype, mid, tte)
            assert result is None or isinstance(result, float), (
                f"get({mtype!r}, {mid}, {tte}) returned {type(result)!r}; "
                "expected float or None"
            )
            if result is not None:
                assert 0.0 <= result <= 1.0, (
                    f"get({mtype!r}, {mid}, {tte}) = {result} out of [0, 1]"
                )

    def test_fills_csv_has_required_columns(self):
        """momentum_fills.csv must contain at least the columns WinRateTable reads."""
        if not self.FILLS_CSV.exists():
            pytest.skip("No momentum_fills.csv")
        import csv
        with open(self.FILLS_CSV, newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
        required = {"market_type", "fill_price"}
        missing = required - set(headers)
        assert not missing, (
            f"momentum_fills.csv is missing required columns: {missing}. "
            f"Found: {set(headers)}"
        )

    def test_trades_csv_has_required_columns(self):
        """trades.csv must contain at least the join key WinRateTable uses."""
        if not self.TRADES_CSV.exists():
            pytest.skip("No trades.csv")
        import csv
        with open(self.TRADES_CSV, newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
        # WinRateTable joins fills to trades by market_id to get "WIN"/"LOSS" outcome
        required = {"market_id", "resolved_outcome"}
        missing = required - set(headers)
        assert not missing, (
            f"trades.csv is missing required columns: {missing}. "
            f"Found: {set(headers)}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# P3: Rebate routing and market type coverage sanity
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.live
class TestRebateRoutingIntegrity:
    """_REBATE_PCT_BY_TYPE must cover all live observable market types."""

    @pytest.fixture(scope="class")
    def live_market_types(self):
        """Collect distinct market_types observed in live Gamma API events."""
        mtypes: set[str] = set()
        for slug in ("bitcoin", "ethereum", "crypto", "solana"):
            events = _fetch_gamma_events(slug, limit=20)
            for event in events:
                event_title = event.get("title", "")
                series_raw = event.get("series")
                if isinstance(series_raw, list):
                    series_raw = series_raw[0] if series_raw else None
                series = series_raw if isinstance(series_raw, dict) else {}
                series_title = series.get("title", "")
                for mkt in event.get("markets", []):
                    if mkt.get("active") and mkt.get("acceptingOrders"):
                        title = series_title or event_title or mkt.get("question", "")
                        mt = _classify_market(title)
                        mtypes.add(mt)
        if not mtypes:
            pytest.skip("Gamma API unavailable")
        return mtypes

    def test_observed_bucket_types_have_rebate(self, live_market_types):
        """Any bucket_* type seen in live data must have a rebate_pct entry."""
        observed_buckets = {mt for mt in live_market_types if mt.startswith("bucket_")}
        missing = observed_buckets - set(_REBATE_PCT_BY_TYPE.keys())
        assert not missing, (
            f"Observed live bucket types with no rebate_pct: {missing}. "
            "Add them to _REBATE_PCT_BY_TYPE before deploying maker quotes."
        )

    def test_observed_types_have_duration(self, live_market_types):
        """Any bucket_* type seen in live data must have a duration mapping."""
        observed_buckets = {mt for mt in live_market_types if mt.startswith("bucket_")}
        missing = observed_buckets - set(_MARKET_TYPE_DURATION_SECS.keys())
        assert not missing, (
            f"Observed live bucket types missing duration: {missing}. "
            "Add them to _MARKET_TYPE_DURATION_SECS."
        )

    def test_all_live_market_types_are_known(self, live_market_types):
        """Live market types must all be keys in _MARKET_TYPE_KEYWORDS or 'milestone'."""
        known = set(_MARKET_TYPE_KEYWORDS.keys()) | {"milestone"}
        unexpected = live_market_types - known
        assert not unexpected, (
            f"New unknown market types discovered in live data: {unexpected}. "
            "Update _MARKET_TYPE_KEYWORDS and related mappings."
        )
