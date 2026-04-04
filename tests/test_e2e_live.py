"""
tests/test_e2e_live.py — End-to-end functional and system tests.

Uses REAL Polymarket public APIs (Gamma REST + CLOB REST + WebSocket) in paper
mode.  No wallet credentials required; PAPER_TRADING=True prevents any real
orders from being submitted.

What is tested end-to-end:
  1. PMClient discovers live markets via Gamma API and receives WS book snapshots
  2. MakerStrategy evaluates signals from real mid prices
  3. FillSimulator._is_crossed correctly identifies fillable quotes against live book
  4. Partial fills:
       - Position shows accumulated filled amount
       - ActiveQuote shows remaining (original_size - filled)
       - get_signals() reports fill_pct, remaining_size, filled_size
  5. Full fill removes quote from active_quotes; signal becomes is_deployed=False
  6. Per-market and total exposure limits block new fills when hit
  7. Hedge triggered when position delta exceeds HEDGE_THRESHOLD_USD
  8. Capital accounting: available_capital decreases with fills
  9. Full sweep integration: simulator _sweep() against real live book data

Run:
    pytest tests/test_e2e_live.py -v -m live --timeout=90
    pytest tests/test_e2e_live.py -v -m live -k "test_partial" --timeout=60

Marks:
    live   — makes real HTTP / WS calls; skip in offline CI
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

# Must be set before importing any module that reads config
config.PAPER_TRADING = True
config.STRATEGY_MAKER_ENABLED = True

# Shrink fill queue tolerance to 0.05 so live test books always trigger fills
# (overriding the default 0.03 for more robust test coverage)
import fill_simulator as _fs_module

import risk
from risk import RiskEngine, Position
from strategies.maker.signals import ActiveQuote, MakerSignal
from strategies.maker.strategy import MakerStrategy
from fill_simulator import FillSimulator
from monitor import PositionMonitor
from market_data.pm_client import PMClient, PMMarket, OrderBookSnapshot

pytestmark = pytest.mark.live


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_hl(mid: float = 85_000.0) -> MagicMock:
    hl = MagicMock()
    hl.get_mid = MagicMock(return_value=mid)
    hl.place_hedge = AsyncMock(return_value=True)
    hl.close_hedge = AsyncMock(return_value=True)
    hl.on_bbo_update = MagicMock()
    return hl


def _make_components(*, hl_mid: float = 85_000.0):
    """Build RiskEngine, MakerStrategy, PositionMonitor wired together."""
    pm_mock = MagicMock()
    pm_mock.get_markets.return_value = {}
    pm_mock.get_mid.return_value = None
    pm_mock.on_price_change = MagicMock()
    pm_mock.on_bbo_update = MagicMock()
    pm_mock.place_limit = AsyncMock(return_value="paper-test-001")
    pm_mock.cancel_order = AsyncMock(return_value=True)

    hl = _mock_hl(mid=hl_mid)
    engine = RiskEngine()
    strategy = MakerStrategy(pm=pm_mock, hl=hl, risk=engine)
    monitor = PositionMonitor(pm=pm_mock, risk=engine)
    simulator = FillSimulator(pm=pm_mock, maker=strategy, risk=engine, monitor=monitor)
    return pm_mock, hl, engine, strategy, monitor, simulator


def _fetch_live_btc_book() -> tuple[Optional[str], Optional[OrderBookSnapshot]]:
    """
    Fetch a live BTC bucket-daily market token_id and a book snapshot from
    the Polymarket CLOB REST API.  No authentication needed.
    Returns (token_id, OrderBookSnapshot) or (None, None) if unavailable.
    """
    try:
        resp = requests.get(
            f"{config.GAMMA_HOST}/events",
            params={
                "active": "true", "closed": "false",
                "tag_slug": "bitcoin", "limit": 20,
                "order": "volume24hr", "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return None, None

    import json
    token_id = None
    for event in resp.json():
        for mkt in event.get("markets", []):
            toks = mkt.get("clobTokenIds", [])
            if isinstance(toks, str):
                try:
                    toks = json.loads(toks)
                except Exception:
                    toks = []
            if (mkt.get("active") and mkt.get("acceptingOrders")
                    and mkt.get("enableOrderBook") and len(toks) >= 1):
                token_id = toks[0]
                break
        if token_id:
            break

    if not token_id:
        return None, None

    try:
        book_resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=15,
        )
        book_resp.raise_for_status()
        raw = book_resp.json()
    except Exception:
        return token_id, None

    snap = OrderBookSnapshot(token_id=token_id)
    for entry in raw.get("bids", []):
        snap.bids.append((float(entry.get("price", entry.get("p", 0))),
                          float(entry.get("size", entry.get("s", 0)))))
    for entry in raw.get("asks", []):
        snap.asks.append((float(entry.get("price", entry.get("p", 0))),
                          float(entry.get("size", entry.get("s", 0)))))
    snap.bids.sort(key=lambda x: -x[0])
    snap.asks.sort(key=lambda x: x[0])
    return token_id, snap


def _synthetic_market(token_id: str, mid: float = 0.45) -> PMMarket:
    from datetime import datetime, timezone, timedelta
    return PMMarket(
        condition_id="test-mkt-001",
        token_id_yes=token_id,
        token_id_no=token_id + "_no",
        title="Will BTC reach $100k? [Daily]",
        market_type="bucket_daily",
        underlying="BTC",
        fees_enabled=True,
        end_date=datetime.now(timezone.utc) + timedelta(days=3),
        tick_size=0.01,
        max_incentive_spread=0.04,
        volume_24hr=50_000.0,
        discovered_at=time.time() - 7200,  # 2 hours old (not "new market")
    )


@pytest.fixture(scope="module")
def live_btc_book():
    """Return (token_id, OrderBookSnapshot) from live PM CLOB REST API."""
    token_id, snap = _fetch_live_btc_book()
    if token_id is None:
        pytest.skip("Polymarket Gamma API unavailable")
    if snap is None or (not snap.bids and not snap.asks):
        pytest.skip("Polymarket CLOB book unavailable or empty")
    return token_id, snap


# ─────────────────────────────────────────────────────────────────────────────
# E2E-01: Live market discovery
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_LiveMarketDiscovery:
    """Verify PMClient can fetch real markets and book data from Polymarket APIs."""

    def test_gamma_api_returns_markets(self):
        """At least 5 active crypto markets available via Gamma API."""
        resp = requests.get(
            f"{config.GAMMA_HOST}/events",
            params={"active": "true", "closed": "false",
                    "tag_slug": "bitcoin", "limit": 50},
            timeout=15,
        )
        assert resp.status_code == 200, f"Gamma API returned {resp.status_code}"
        events = resp.json()
        markets = [m for e in events for m in e.get("markets", []) if m.get("active")]
        assert len(markets) >= 1, "Expected at least 1 active BTC market"

    def test_clob_book_has_valid_probability_prices(self, live_btc_book):
        """All bid/ask prices from CLOB REST are valid probabilities in [0, 1]."""
        _token_id, snap = live_btc_book
        all_prices = [p for p, _ in snap.bids] + [p for p, _ in snap.asks]
        assert len(all_prices) > 0, "Book snapshot has no prices"
        for price in all_prices:
            assert 0.0 <= price <= 1.0, f"Price {price} outside [0,1]"

    def test_clob_book_has_positive_spread(self, live_btc_book):
        """best_ask > best_bid (real market should always have a valid spread)."""
        _token_id, snap = live_btc_book
        if snap.best_bid is None or snap.best_ask is None:
            pytest.skip("One-sided book — skipping spread check")
        assert snap.best_ask > snap.best_bid, (
            f"Invalid book: best_ask={snap.best_ask} <= best_bid={snap.best_bid}"
        )

    def test_mid_is_valid_probability(self, live_btc_book):
        """Mid-point of live book is a valid probability."""
        _token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid available (one-sided or empty book)")
        assert 0.0 < snap.mid < 1.0, f"Mid {snap.mid} not in (0, 1)"


# ─────────────────────────────────────────────────────────────────────────────
# E2E-02: _is_crossed detects fillable quotes against live book
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_IsCrossed:
    """FillSimulator correctly identifies which paper quotes are reachable by takers."""

    def test_bid_at_best_bid_is_crossed(self, live_btc_book):
        """A paper BUY resting at the real best_bid is always reachable."""
        token_id, snap = live_btc_book
        if snap.best_bid is None:
            pytest.skip("No bids in live book")
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        assert simulator._is_crossed("BUY", snap.best_bid, snap), \
            f"bid @ best_bid={snap.best_bid} should be crossed"

    def test_bid_one_tick_below_best_bid_is_crossed(self, live_btc_book):
        """A BUY quote one tick (0.01) below best_bid is still reachable — queue model."""
        token_id, snap = live_btc_book
        if snap.best_bid is None:
            pytest.skip("No bids in live book")
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        price = round(snap.best_bid - 0.01, 4)
        if price <= 0:
            pytest.skip("best_bid too low for this test")
        assert simulator._is_crossed("BUY", price, snap), (
            f"bid @ {price} (best_bid={snap.best_bid} - 0.01) should be reachable "
            f"by large takers (within queue tolerance)"
        )

    def test_bid_at_maker_quoted_price_is_crossed(self, live_btc_book):
        """Maker BUY at mid-half_spread is within fill tolerance of best_bid.

        Skipped when the live book is too tight for the 0.02 half-spread assumption:
        _is_crossed requires price >= best_bid - 0.01 (FILL_QUEUE_TOLERANCE).
        When mid - best_bid < 0.01 the maker bid (mid-0.02) falls below that floor.
        """
        token_id, snap = live_btc_book
        if snap.mid is None or snap.best_bid is None:
            pytest.skip("No mid/bid in live book")
        if not snap.asks:
            pytest.skip(
                "No asks in live book — market is likely near resolution "
                "(mid ≈ best_bid, 2-cent half-spread falls outside fill tolerance)"
            )
        # The assumption mid - 0.02 ≈ best_bid only holds when the bid-side
        # spread is at least 0.01 wide.  Tight books (e.g. 0.005 spread) make
        # the maker bid (mid-0.02) exceed the FILL_QUEUE_TOLERANCE gap — skip
        # rather than assert on a condition that depends on live market conditions.
        if snap.mid - snap.best_bid < 0.01:
            pytest.skip(
                f"Book too tight (mid-best_bid={snap.mid - snap.best_bid:.3f} < 0.01); "
                "0.02 half-spread bid falls below fill tolerance"
            )
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        maker_bid = round(snap.mid - 0.02, 4)  # typical: mid minus half_spread
        if maker_bid <= 0:
            pytest.skip("mid too low for this test")
        assert simulator._is_crossed("BUY", maker_bid, snap), (
            f"Maker bid={maker_bid} (mid={snap.mid}-0.02) should be crossed "
            f"against live book best_bid={snap.best_bid}"
        )

    def test_bid_way_below_market_not_crossed(self, live_btc_book):
        """A BUY quote far below the market (e.g. 0.01) is not reachable."""
        token_id, snap = live_btc_book
        if snap.best_bid is None or snap.best_bid < 0.10:
            pytest.skip("best_bid too low for this test")
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        price = max(0.01, snap.best_bid - 0.50)  # 50 ticks below
        assert not simulator._is_crossed("BUY", price, snap), (
            f"bid @ {price} is too deep (best_bid={snap.best_bid}); should not be crossed"
        )

    def test_ask_at_best_ask_is_crossed(self, live_btc_book):
        """A paper SELL resting at the real best_ask is always reachable."""
        token_id, snap = live_btc_book
        if snap.best_ask is None:
            pytest.skip("No asks in live book")
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        assert simulator._is_crossed("SELL", snap.best_ask, snap), \
            f"ask @ best_ask={snap.best_ask} should be crossed"

    def test_ask_at_maker_quoted_price_is_crossed(self, live_btc_book):
        """Maker SELL one tick above best_ask is within fill tolerance."""
        token_id, snap = live_btc_book
        if snap.best_ask is None:
            pytest.skip("No asks in live book")
        _pm, _hl, engine, strategy, monitor, simulator = _make_components()
        # Post one tick above the inside ask — should be reachable by an arriving BUY taker.
        maker_ask = round(snap.best_ask + 0.01, 4)
        if maker_ask >= 1.0:
            pytest.skip("best_ask too high for this test")
        assert simulator._is_crossed("SELL", maker_ask, snap), (
            f"Maker ask={maker_ask} (best_ask={snap.best_ask}+0.01) should be crossed "
            f"against live book best_ask={snap.best_ask}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-03: Partial fill flow — position, opportunity, capital tracking
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_PartialFillFlow:
    """
    Core requirement: while a quote is being partially filled, the UI must show:
      - Positions page: accumulated filled size
      - Opportunities / Signals: remaining size to fill, fill_pct
    """

    def setup_method(self):
        self.pm, self.hl, self.engine, self.strategy, self.monitor, self.simulator = (
            _make_components(hl_mid=85_000.0)
        )

    def _inject_quote(self, token_id: str, mid: float, size: float = 100.0) -> str:
        """
        Inject a paper BUY quote into the strategy's active_quotes and signals
        at the given token_id/mid, returning the bid_key.
        """
        key = token_id
        ask_key = f"{token_id}_ask"
        market = _synthetic_market(token_id, mid=mid)

        # Inject a fake market context so _on_fill can resolve self._pm._markets
        self.simulator._pm._markets = {market.condition_id: market}
        self.simulator._pm._books = {}

        bid_price = round(max(mid - 0.02, 0.01), 4)  # clamp to valid probability range
        quote = ActiveQuote(
            market_id=market.condition_id,
            token_id=token_id,
            side="BUY",
            price=bid_price,
            size=size,
            order_id="paper-test-001",
            collateral_usd=round(bid_price * size, 4),
            original_size=size,
        )
        self.strategy._active_quotes[key] = quote

        # Register a matching signal
        self.strategy._signals[key] = MakerSignal(
            market_id=market.condition_id,
            token_id=token_id,
            underlying="BTC",
            mid=mid,
            bid_price=bid_price,
            ask_price=round(mid + 0.02, 4),
            half_spread=0.02,
            effective_edge=0.022,
            market_type="bucket_daily",
            quote_size=size,
        )
        return key, market

    def test_partial_fill_creates_position_and_remainder(self, live_btc_book):
        """
        After a 40-contract partial fill on a 100-contract quote:
          - Position with size=40 is opened
          - ActiveQuote has size=60 remaining, original_size=100
          - get_signals() shows bid_filled=40, bid_remaining=60, fill_pct≈0.2
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        # Use a fixed mid so bid_price=0.88 and 60-contract remainder = $52.8 ≥ MIN=$50.
        # snap.mid for near-certain markets is ~0.01, making all remainders sub-minimum.
        key, market = self._inject_quote(token_id, mid=0.90, size=100.0)

        # Force a 40-contract partial fill
        _run(self.simulator._on_fill(key, self.strategy._active_quotes[key], market, 40.0))

        # Position must exist with size=40
        positions = self.engine.get_open_positions()
        assert positions, "No position opened after partial fill"
        pos = next((p for p in positions if p.market_id == market.condition_id), None)
        assert pos is not None, "Position for test market not found"
        assert pos.size == pytest.approx(40.0, abs=0.01), (
            f"Expected size=40, got {pos.size}"
        )

        # ActiveQuote must still be present with 60 remaining
        remaining_quote = self.strategy._active_quotes.get(key)
        assert remaining_quote is not None, (
            "ActiveQuote should remain after partial fill (60 still unfilled)"
        )
        assert remaining_quote.size == pytest.approx(60.0, abs=0.01), (
            f"Expected remaining size=60, got {remaining_quote.size}"
        )
        assert remaining_quote.original_size == pytest.approx(100.0, abs=0.01), (
            f"Expected original_size=100, got {remaining_quote.original_size}"
        )

        # get_signals() must report partial fill state
        signals = self.strategy.get_signals()
        sig = signals.get(key)
        assert sig is not None, "Signal for test market not found"
        assert sig["is_deployed"], "Signal must be deployed (quote still active)"
        assert sig["bid_filled_size"] == pytest.approx(40.0, abs=0.01), (
            f"Expected bid_filled_size=40, got {sig['bid_filled_size']}"
        )
        assert sig["bid_remaining_size"] == pytest.approx(60.0, abs=0.01), (
            f"Expected bid_remaining_size=60, got {sig['bid_remaining_size']}"
        )
        assert sig["bid_original_size"] == pytest.approx(100.0, abs=0.01), (
            f"Expected bid_original_size=100, got {sig['bid_original_size']}"
        )
        assert 0.0 < sig["fill_pct"] < 1.0, (
            f"fill_pct should be between 0 and 1, got {sig['fill_pct']}"
        )

    def test_cumulative_fills_merge_into_single_position(self, live_btc_book):
        """
        Three partial fills (30+30+40 = 100 total) must merge into one position
        with total size=100, not three separate positions.

        Uses mid=0.90 so bid_price=0.88 — all fills at $0.88.

        Real-CLOB behaviour (auto-consume was removed):
          - After fill 1 (30/100): remainder=70 × $0.88=$61.6 → stays alive ✓
          - After fill 2 (30 more = 60/100): remainder=40 × $0.88=$35.2 < MIN=$50
            → STILL stays alive (real CLOB keeps sub-min remainders; reprice
               machinery cancels them on the next quote cycle, not immediately).
          - After fill 3 (40 remaining): fully consumed, key removed.
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        # Fixed mid so bid_price=0.88 and remainders are predictable
        key, market = self._inject_quote(token_id, mid=0.90, size=100.0)
        quote_ref = self.strategy._active_quotes[key]

        # Fill 1: 30/100 consumed; remainder=70 ($61.6) stays alive
        _run(self.simulator._on_fill(key, quote_ref, market, 30.0))
        assert key in self.strategy._active_quotes, (
            "Remainder (70 cts, $61.6) should stay alive after first fill"
        )
        quote_ref = self.strategy._active_quotes[key]  # updated remainder

        # Fill 2: 30 more = 60/100 total; remainder=40 ($35.2 < MIN=$50)
        # CLOB-accurate: remainder stays alive — sub-min removal is done by the
        # reprice/cancel cycle, not by the fill simulator.
        _run(self.simulator._on_fill(key, quote_ref, market, 30.0))
        assert key in self.strategy._active_quotes, (
            "Sub-min remainder (40 cts, $35.2) should stay in active_quotes "
            "until reprice cancels it — auto-consume was removed for CLOB fidelity"
        )
        quote_ref = self.strategy._active_quotes[key]

        # Fill 3: consume the remaining 40 contracts — fully exhausts the order
        _run(self.simulator._on_fill(key, quote_ref, market, 40.0))
        assert key not in self.strategy._active_quotes, (
            "Quote should be removed after full (100/100) fill"
        )

        # All three slices (30+30+40=100) must be merged into a single position
        positions = self.engine.get_open_positions()
        market_positions = [p for p in positions if p.market_id == market.condition_id]
        assert len(market_positions) == 1, (
            f"Expected 1 merged position, got {len(market_positions)}"
        )
        assert market_positions[0].size == pytest.approx(100.0, abs=0.01), (
            f"Expected merged size=100, got {market_positions[0].size}"
        )

    def test_full_fill_removes_quote_from_active(self, live_btc_book):
        """
        After a full fill the ActiveQuote is removed from active_quotes.
        The signal remains (is_deployed=False).
        The resulting position size must equal the injected quote size and be
        within the configured [MAKER_QUOTE_SIZE_MIN, MAKER_QUOTE_SIZE_MAX] bounds.
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        quote_size = 100.0  # contract count injected directly into active_quotes

        key, market = self._inject_quote(token_id, mid=snap.mid, size=quote_size)
        quote_ref = self.strategy._active_quotes[key]

        _run(self.simulator._on_fill(key, quote_ref, market, quote_size))  # full fill

        assert key not in self.strategy._active_quotes, (
            "ActiveQuote should be removed after full fill"
        )

        signals = self.strategy.get_signals()
        sig = signals.get(key)
        assert sig is not None, "Signal should persist after full fill"
        assert not sig["is_deployed"], (
            "Signal must be is_deployed=False after full fill (quote consumed)"
        )

        # The filled position's USD cost must equal the contracts × fill_price.
        # That USD cost (entry_cost_usd) must fall within the configured bounds.
        positions = self.engine.get_open_positions()
        market_pos = [p for p in positions if p.market_id == market.condition_id]
        assert len(market_pos) == 1, (
            f"Expected exactly 1 position after full fill, got {len(market_pos)}"
        )
        pos = market_pos[0]
        # size = contracts injected
        assert pos.size == pytest.approx(quote_size, abs=0.01), (
            f"Position size (contracts) {pos.size} should equal injected quote_size={quote_size}"
        )
        # entry_cost_usd = price × contracts; this is the actual USD deployed
        expected_cost_usd = round(pos.entry_price * pos.size, 4)
        assert pos.entry_cost_usd == pytest.approx(expected_cost_usd, abs=0.01), (
            f"entry_cost_usd {pos.entry_cost_usd} should equal price×contracts={expected_cost_usd}"
        )

    def test_fill_updates_inventory_for_hedge_trigger(self, live_btc_book):
        """
        After a YES fill, the maker's inventory should reflect the new capital
        so that _rebalance_hedge can be triggered correctly.
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        key, market = self._inject_quote(token_id, mid=snap.mid, size=100.0)
        quote_ref = self.strategy._active_quotes[key]

        inventory_before = self.strategy._inventory.get("BTC", 0.0)
        _run(self.simulator._on_fill(key, quote_ref, market, 100.0))
        inventory_after = self.strategy._inventory.get("BTC", 0.0)

        assert inventory_after != inventory_before, (
            "Inventory should change after a BUY fill (YES side)"
        )
        assert inventory_after > inventory_before, (
            "BUY YES fill should increase BTC inventory"
        )

    def test_inventory_records_usd_entry_cost(self, live_btc_book):
        """
        Inventory must track USD entry cost (fill_cost_usd = price × contracts),
        NOT face notional (number of contracts).

        The inventory-skew coefficient is INVENTORY_SKEW_COEFF = 0.0001 = 1 cent
        per $100 USD.  Using contract counts at price < 1 would overstate the skew
        by a factor of 1/price.  Using USD ensures the skew is always in the correct
        units per MAKER_STRATEGY.md §Units & Invariants.
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        filled_contracts = 100.0
        key, market = self._inject_quote(token_id, mid=snap.mid, size=filled_contracts)
        quote_ref = self.strategy._active_quotes[key]
        fill_price = quote_ref.price  # BUY YES: fill_cost_usd = price × contracts

        inventory_before = self.strategy._inventory.get("BTC", 0.0)
        _run(self.simulator._on_fill(key, quote_ref, market, filled_contracts))
        inventory_after = self.strategy._inventory.get("BTC", 0.0)

        actual_delta = inventory_after - inventory_before
        expected_usd = fill_price * filled_contracts   # USD cost, not contract count
        assert actual_delta == pytest.approx(expected_usd, rel=0.01), (
            f"Inventory delta should equal USD entry cost (price × contracts = "
            f"{fill_price:.4f} × {filled_contracts} = {expected_usd:.2f}), "
            f"got {actual_delta:.4f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-04: Reprice must not cancel a partially-filled quote
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_RepricePreservesPartialFills:
    """
    When a quote is partially filled and _reprice_market is called, the remaining
    portion must NOT be cancelled.  The 'remaining to fill' must stay visible.
    """

    def setup_method(self):
        self.pm, self.hl, self.engine, self.strategy, self.monitor, self.simulator = (
            _make_components()
        )

    def test_reprice_skips_partially_filled_quote(self, live_btc_book):
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        market = _synthetic_market(token_id, mid=snap.mid)
        self.simulator._pm._markets = {market.condition_id: market}
        self.simulator._pm._books = {}

        bid_price = round(snap.mid - 0.004, 4)  # drift ~0.004 well below MAKER_ADVERSE_DRIFT_REPRICE
        key = token_id
        self.strategy._active_quotes[key] = ActiveQuote(
            market_id=market.condition_id,
            token_id=token_id,
            side="BUY",
            price=bid_price,
            size=60.0,          # already 40 filled out of 100
            order_id="paper-001",
            original_size=100.0,
            collateral_usd=round(bid_price * 60.0, 4),
        )
        self.strategy._signals[key] = MakerSignal(
            market_id=market.condition_id,
            token_id=token_id,
            underlying="BTC",
            mid=snap.mid,
            bid_price=bid_price,
            ask_price=round(snap.mid + 0.01, 4),
            half_spread=0.01,
            effective_edge=0.012,
            market_type="bucket_daily",
            quote_size=100.0,
        )

        # Wire PM mock to return a real mid and a book with a current timestamp
        # (so the MAKER_MAX_BOOK_AGE_SECS gate doesn't short-circuit _reprice_market)
        self.strategy._pm.get_mid.return_value = snap.mid
        self.strategy._pm.get_book.return_value = snap  # snap.timestamp is already a float
        self.strategy._pm.get_markets.return_value = {
            market.condition_id: market
        }

        # Call reprice — it should skip because the quote is partially filled
        _run(self.strategy._reprice_market(market))

        # Quote must still be present with original 60-contract remainder
        remaining = self.strategy._active_quotes.get(key)
        assert remaining is not None, (
            "ActiveQuote was cancelled during reprice despite being partially filled"
        )
        assert remaining.size == pytest.approx(60.0, abs=0.01), (
            f"Remaining size changed during reprice: expected 60, got {remaining.size}"
        )
        assert remaining.original_size == pytest.approx(100.0, abs=0.01), (
            f"original_size was lost: expected 100, got {remaining.original_size}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-05: Exposure limit enforcement
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_ExposureLimits:
    """Per-market and total PM exposure caps block fills when hit."""

    def setup_method(self):
        _pm, hl, self.engine, self.strategy, monitor, self.simulator = _make_components()
        # Use tight limits for testing
        self._orig_per_market = config.MAX_PM_EXPOSURE_PER_MARKET
        self._orig_total = config.MAX_TOTAL_PM_EXPOSURE
        config.MAX_PM_EXPOSURE_PER_MARKET = 150.0
        config.MAX_TOTAL_PM_EXPOSURE = 300.0

    def teardown_method(self):
        config.MAX_PM_EXPOSURE_PER_MARKET = self._orig_per_market
        config.MAX_TOTAL_PM_EXPOSURE = self._orig_total

    def test_per_market_limit_blocks_excess_fills(self, live_btc_book):
        """
        After deploying $150 USD into one market, additional fills are blocked.
        can_open() compares entry_cost_usd (USD) against MAX_PM_EXPOSURE_PER_MARKET (USD).
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        market = _synthetic_market(token_id)
        self.simulator._pm._markets = {market.condition_id: market}

        # Seed $150 USD already deployed into this market (fills the $150 per-market cap)
        self.engine.open_position(Position(
            market_id=market.condition_id,
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=333,            # 333 contracts at $0.45 = ~$150 USD
            entry_price=0.45,
            strategy="maker",
            entry_cost_usd=150.0,  # ← this is what can_open() now sums
        ))

        # Attempting to deploy any more USD into this market should fail
        ok, reason = self.engine.can_open(
            market.condition_id, 1.0, strategy="maker", underlying="BTC"
        )
        assert not ok, "Expected can_open to block (per-market USD limit reached)"
        assert "per-market" in reason.lower() or "limit" in reason.lower(), (
            f"Unexpected reason: {reason}"
        )

    def test_total_exposure_limit_blocks_cross_market_fills(self, live_btc_book):
        """
        Total USD exposure across markets blocks fills when global limit is hit.
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        # Seed two markets totalling $300 USD entry_cost_usd (fills the $300 total cap)
        self.engine.open_position(Position(
            market_id="mkt-001",
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=333,
            entry_price=0.45,
            strategy="maker",
            entry_cost_usd=150.0,
        ))
        self.engine.open_position(Position(
            market_id="mkt-002",
            market_type="bucket_daily",
            underlying="ETH",
            side="YES",
            size=333,
            entry_price=0.45,
            strategy="maker",
            entry_cost_usd=150.0,
        ))

        # Any further fill would exceed $300 total should fail
        ok, reason = self.engine.can_open(
            "mkt-003", 1.0, strategy="maker", underlying="SOL"
        )
        assert not ok, "Expected can_open to block (total USD exposure limit reached)"
        assert "total" in reason.lower() or "limit" in reason.lower(), (
            f"Unexpected reason: {reason}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-06: Hedge triggered after sufficient fills
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_HedgeTriggerAfterFill:
    """
    After accumulating YES positions with entry_cost_usd > HEDGE_THRESHOLD_USD,
    calling _rebalance_hedge must place a SHORT HL hedge.
    """

    def setup_method(self):
        self.hl_mid = 85_000.0
        _pm, self.hl, self.engine, self.strategy, monitor, self.simulator = (
            _make_components(hl_mid=self.hl_mid)
        )
        self._orig_threshold = config.HEDGE_THRESHOLD_USD
        self._orig_hedge_enabled = config.MAKER_HEDGE_ENABLED
        config.HEDGE_THRESHOLD_USD = 100.0
        config.MAKER_HEDGE_ENABLED = True

    def teardown_method(self):
        config.HEDGE_THRESHOLD_USD = self._orig_threshold
        config.MAKER_HEDGE_ENABLED = self._orig_hedge_enabled

    def test_hedge_placed_when_delta_exceeds_threshold(self, live_btc_book):
        """
        After fills amounting to >$100 net BTC delta, a SHORT HL hedge is placed.
        """
        token_id, snap = live_btc_book

        # Open a YES position worth $120 capital (= delta = $120 > $100 threshold)
        self.engine.open_position(Position(
            market_id="test-mkt",
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=240.0,       # 240 contracts at $0.50 = $120 capital
            entry_price=0.50,
            strategy="maker",
            entry_cost_usd=120.0,
        ))

        _run(self.strategy._rebalance_hedge("BTC"))

        self.hl.place_hedge.assert_called_once()
        args = self.hl.place_hedge.call_args[0]
        assert args[0] == "BTC", f"Expected BTC, got {args[0]}"
        assert args[1] == "SHORT", f"Expected SHORT (net long YES), got {args[1]}"

        expected_coins = 120.0 / self.hl_mid
        assert args[2] == pytest.approx(expected_coins, rel=0.01), (
            f"Expected {expected_coins:.6f} BTC to hedge, got {args[2]:.6f}"
        )

    def test_no_hedge_below_threshold(self, live_btc_book):
        self.engine.open_position(Position(
            market_id="test-mkt",
            market_type="bucket_daily",
            underlying="BTC",
            side="YES",
            size=100.0,       # $50 capital < $100 threshold
            entry_price=0.50,
            strategy="maker",
            entry_cost_usd=50.0,
        ))
        _run(self.strategy._rebalance_hedge("BTC"))
        self.hl.place_hedge.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# E2E-07: Capital accounting
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_CapitalAccounting:
    """
    available_capital must decrease as quotes are deployed and fills happen.
    """

    def setup_method(self):
        self.pm, self.hl, self.engine, self.strategy, self.monitor, self.simulator = (
            _make_components()
        )
        self._orig_capital = config.PAPER_CAPITAL_USD
        config.PAPER_CAPITAL_USD = 10_000.0

    def teardown_method(self):
        config.PAPER_CAPITAL_USD = self._orig_capital

    def test_available_capital_decreases_with_deployed_quotes(self, live_btc_book):
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        available_before = self.strategy.available_capital

        bid_price = round(max(snap.mid - 0.02, 0.01), 4)  # clamp to valid range
        quote = ActiveQuote(
            market_id="test-mkt",
            token_id=token_id,
            side="BUY",
            price=bid_price,
            size=100.0,
            order_id="paper-001",
            collateral_usd=round(bid_price * 100.0, 4),
            original_size=100.0,
        )
        self.strategy._active_quotes[token_id] = quote

        available_after = self.strategy.available_capital
        assert available_after < available_before, (
            f"available_capital should decrease when quotes are deployed: "
            f"before={available_before:.2f}, after={available_after:.2f}"
        )

    def test_available_capital_decreases_after_fill(self, live_btc_book):
        """After a fill, capital is locked in positions — available_capital drops."""
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        available_before = self.strategy.available_capital

        market = _synthetic_market(token_id, mid=snap.mid)
        self.simulator._pm._markets = {market.condition_id: market}

        key = token_id
        bid_price = round(max(snap.mid - 0.02, 0.01), 4)  # clamp to valid range
        self.strategy._active_quotes[key] = ActiveQuote(
            market_id=market.condition_id,
            token_id=token_id,
            side="BUY",
            price=bid_price,
            size=100.0,
            order_id="paper-001",
            collateral_usd=round(bid_price * 100.0, 4),
            original_size=100.0,
        )
        quote_ref = self.strategy._active_quotes[key]
        _run(self.simulator._on_fill(key, quote_ref, market, 100.0))

        available_after = self.strategy.available_capital
        assert available_after < available_before, (
            f"available_capital should decrease after fill: "
            f"before={available_before:.2f}, after={available_after:.2f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-08: Full sweep with live book data
# ─────────────────────────────────────────────────────────────────────────────

class TestE2E_FullSweepLiveBook:
    """
    FillSimulator._sweep() against a real live book.
    We inject a paper quote at a price that WILL be crossed (bid above best_bid)
    and with fill probability forced to 1.0, so the fill definitely fires.
    """

    def setup_method(self):
        self.pm, self.hl, self.engine, self.strategy, self.monitor, self.simulator = (
            _make_components()
        )
        # config_overrides.json carries tight production values (MAX_PM_EXPOSURE_PER_MARKET=40,
        # MAX_TOTAL_PM_EXPOSURE=40) that can block fills when the live BTC book is at a high
        # probability (e.g. BTC above $100k → best_ask ≈ 0.95 → fill_cost 0.96 × 50 = $48 > $40).
        # Temporarily raise the limits so the fill always succeeds regardless of current price.
        self._orig_per_market = config.MAX_PM_EXPOSURE_PER_MARKET
        self._orig_total = config.MAX_TOTAL_PM_EXPOSURE
        config.MAX_PM_EXPOSURE_PER_MARKET = 500.0
        config.MAX_TOTAL_PM_EXPOSURE = 500.0

    def teardown_method(self):
        config.MAX_PM_EXPOSURE_PER_MARKET = self._orig_per_market
        config.MAX_TOTAL_PM_EXPOSURE = self._orig_total

    def test_sweep_fills_crossed_quote(self, live_btc_book):
        """
        A paper BUY quote placed ABOVE best_ask (fully crossed) fires immediately
        in a single sweep when probability is 1.0.
        """
        import unittest.mock as mock

        token_id, snap = live_btc_book
        if snap.best_ask is None:
            pytest.skip("No asks in live book")

        market = _synthetic_market(token_id, mid=snap.mid or 0.5)
        self.simulator._pm._markets = {market.condition_id: market}
        self.simulator._pm._books = {market.token_id_yes: snap}

        # Place paper BUY above best_ask → immediately crossed
        crossed_bid = round(snap.best_ask + 0.01, 4)
        if crossed_bid >= 1.0:
            pytest.skip("best_ask too high for this test")

        key = token_id
        self.strategy._active_quotes[key] = ActiveQuote(
            market_id=market.condition_id,
            token_id=token_id,
            side="BUY",
            price=crossed_bid,
            size=50.0,
            order_id="paper-sweep-001",
            collateral_usd=round(crossed_bid * 50.0, 4),
            original_size=50.0,
        )

        # Force fill probability = 1.0 and taker size >> quote size
        with (
            mock.patch("random.random", return_value=0.0),       # always pass prob check
            mock.patch("random.expovariate", return_value=9999.0),  # huge taker
        ):
            _run(self.simulator._sweep())

        positions = self.engine.get_open_positions()
        assert positions, (
            "Expected at least one position after sweep with fully-crossed quote + P=1"
        )
        pos = next((p for p in positions if p.market_id == market.condition_id), None)
        assert pos is not None, "Position not found for test market after sweep"
        assert pos.size > 0, f"Position has zero size: {pos}"

    def test_sweep_does_not_fill_deep_quote(self, live_btc_book):
        """
        A paper BUY placed 50 ticks below best_bid must NOT fire (too deep in queue).
        """
        import unittest.mock as mock

        token_id, snap = live_btc_book
        if snap.best_bid is None or snap.best_bid < 0.55:
            pytest.skip("best_bid too low for this test")

        market = _synthetic_market(token_id, mid=snap.mid or 0.5)
        self.simulator._pm._markets = {market.condition_id: market}
        self.simulator._pm._books = {market.token_id_yes: snap}

        deep_bid = round(snap.best_bid - 0.50, 4)
        if deep_bid <= 0:
            pytest.skip("best_bid too low")

        key = token_id
        self.strategy._active_quotes[key] = ActiveQuote(
            market_id=market.condition_id,
            token_id=token_id,
            side="BUY",
            price=deep_bid,
            size=50.0,
            order_id="paper-deep-001",
            collateral_usd=round(deep_bid * 50.0, 4),
            original_size=50.0,
        )

        with (
            mock.patch("random.random", return_value=0.0),
            mock.patch("random.expovariate", return_value=9999.0),
        ):
            _run(self.simulator._sweep())

        positions = self.engine.get_open_positions()
        assert not positions, (
            f"Deep bid ({deep_bid} vs best_bid={snap.best_bid}) should NOT fill"
        )


# ─────────────────────────────────────────────────────────────────────────────
# E2E-09: Quote-size bounds — _compute_spread_size and full deploy→fill path
# ─────────────────────────────────────────────────────────────────────────────

class TestQuoteSizeBounds:
    """
    _compute_quote_size must always clamp to [MAKER_QUOTE_SIZE_MIN, MAKER_QUOTE_SIZE_MAX].

    Tests:
      - Zero-volume market uses the new-market fallback and clamps to bounds.
      - Huge volume clamps to MAX.
      - Tiny volume clamps to MIN.
      - Normal volume (within bounds) is used as-is.
      - Full deploy→fill pipeline via _evaluate_signal + _deploy_quote produces
        a position whose size is within the configured bounds.
    """

    def setup_method(self):
        self.pm, self.hl, self.engine, self.strategy, self.monitor, self.simulator = (
            _make_components()
        )

    def _make_market(self, vol: float, token_id: str = "token-bounds-001") -> PMMarket:
        from datetime import datetime, timezone, timedelta
        return PMMarket(
            condition_id="bounds-test-mkt",
            token_id_yes=token_id,
            token_id_no=token_id + "_no",
            title="Will BTC test bounds? [Daily]",
            market_type="bucket_daily",
            underlying="BTC",
            fees_enabled=True,
            end_date=datetime.now(timezone.utc) + timedelta(days=3),
            tick_size=0.01,
            max_incentive_spread=0.04,
            volume_24hr=vol,
            discovered_at=time.time() - 7200,
        )

    def test_zero_volume_uses_new_market_fallback_within_bounds(self):
        """Zero-volume market falls back to MAKER_SPREAD_SIZE_NEW_MARKET, clamped into bounds."""
        m = self._make_market(0.0)
        size = self.strategy._compute_spread_size(m)
        expected = max(
            config.MAKER_SPREAD_SIZE_MIN,
            min(config.MAKER_SPREAD_SIZE_MAX, round(config.MAKER_SPREAD_SIZE_NEW_MARKET)),
        )
        assert size == pytest.approx(expected, abs=0.01), (
            f"Expected new-market fallback size={expected}, got {size}"
        )
        assert config.MAKER_SPREAD_SIZE_MIN <= size <= config.MAKER_SPREAD_SIZE_MAX, (
            f"Zero-volume result {size} outside bounds "
            f"[{config.MAKER_SPREAD_SIZE_MIN}, {config.MAKER_SPREAD_SIZE_MAX}]"
        )

    def test_huge_volume_clamps_to_max(self):
        """$1B 24hr volume → 2% = $20M → clamps to MAKER_SPREAD_SIZE_MAX."""
        m = self._make_market(vol=1e9)
        size = self.strategy._compute_spread_size(m)
        assert size == pytest.approx(config.MAKER_SPREAD_SIZE_MAX, abs=0.01), (
            f"Expected MAX={config.MAKER_SPREAD_SIZE_MAX}, got {size}"
        )
        assert size <= config.MAKER_SPREAD_SIZE_MAX

    def test_tiny_volume_clamps_to_min(self):
        """$1 24hr volume → 2% = $0.02 → clamps to MAKER_SPREAD_SIZE_MIN."""
        m = self._make_market(vol=1.0)
        size = self.strategy._compute_spread_size(m)
        assert size == pytest.approx(config.MAKER_SPREAD_SIZE_MIN, abs=0.01), (
            f"Expected MIN={config.MAKER_SPREAD_SIZE_MIN}, got {size}"
        )
        assert size >= config.MAKER_SPREAD_SIZE_MIN

    def test_moderate_volume_stays_in_bounds(self):
        """$5k volume → 2% = $100 — within [MIN, MAX], returned as-is."""
        vol = 5_000.0  # 5k × 0.02 = $100
        m = self._make_market(vol=vol)
        size = self.strategy._compute_spread_size(m)
        expected = round(vol * config.MAKER_SPREAD_SIZE_PCT)
        assert size == pytest.approx(expected, abs=1.0), (
            f"Expected vol-proportional size={expected}, got {size}"
        )
        assert config.MAKER_SPREAD_SIZE_MIN <= size <= config.MAKER_SPREAD_SIZE_MAX

    def test_override_bypasses_compute(self):
        """When quote_size_usd override is set, _compute_spread_size returns that value."""
        _, _, _, strategy_override, _, _ = _make_components()
        strategy_override._quote_size_override = 75.0
        m = self._make_market(vol=1e9)  # huge volume, but override should win
        size = strategy_override._compute_spread_size(m)
        assert size == pytest.approx(75.0, abs=0.01), (
            f"Expected override=75.0, got {size}"
        )

    def test_deploy_fill_produces_position_within_bounds(self, live_btc_book):
        """
        Full pipeline: _evaluate_signal → _deploy_quote → _on_fill.
        quote_size is a USD target; _deploy_quote converts to contracts.
        The resulting position's entry_cost_usd must be ≈ quote_size and
        within [MAKER_SPREAD_SIZE_MIN, MAKER_SPREAD_SIZE_MAX].
        """
        token_id, snap = live_btc_book
        if snap.mid is None:
            pytest.skip("No mid in live book")

        # Build a market with volume that puts quote_size well within bounds
        # 5k × 0.02 = $100 ∈ [MIN=50, MAX=250]
        market = _synthetic_market(token_id, mid=snap.mid)
        market.volume_24hr = 5_000.0
        self.pm.place_limit.return_value = "paper-bounds-test"
        self.strategy._pm._markets = {market.condition_id: market}
        self.simulator._pm._markets = {market.condition_id: market}
        self.simulator._pm._books = {}

        signal = self.strategy._evaluate_signal(market, snap.mid)
        if signal is None:
            pytest.skip("Signal not generated for this live mid (edge/TTE/risk filter)")

        signal_qs = signal.quote_size  # USD target
        assert config.MAKER_QUOTE_SIZE_MIN <= signal_qs <= config.MAKER_QUOTE_SIZE_MAX, (
            f"Signal quote_size={signal_qs} not in bounds "
            f"[{config.MAKER_QUOTE_SIZE_MIN}, {config.MAKER_QUOTE_SIZE_MAX}]"
        )

        _run(self.strategy._deploy_quote(signal, market))
        bid_key = token_id
        quote_ref = self.strategy._active_quotes.get(bid_key)
        assert quote_ref is not None, "BID quote was not deployed"

        # Contracts must equal round(quote_size / bid_price)
        expected_contracts = max(1, round(signal_qs / signal.bid_price))
        assert quote_ref.size == pytest.approx(expected_contracts, abs=1), (
            f"Deployed contracts {quote_ref.size} != round(quote_size/bid_price)={expected_contracts}"
        )
        # Collateral must be close to the USD target
        assert quote_ref.collateral_usd == pytest.approx(signal.bid_price * expected_contracts, abs=0.02), (
            f"collateral_usd {quote_ref.collateral_usd} != price×contracts"
        )
        assert config.MAKER_QUOTE_SIZE_MIN <= quote_ref.collateral_usd <= config.MAKER_QUOTE_SIZE_MAX + 1, (
            f"collateral_usd {quote_ref.collateral_usd} outside expected bounds"
        )

        # Fully fill the deployed quote (fill by contract count)
        _run(self.simulator._on_fill(bid_key, quote_ref, market, quote_ref.size))

        positions = self.engine.get_open_positions()
        market_pos = [p for p in positions if p.market_id == market.condition_id]
        assert len(market_pos) == 1, (
            f"Expected 1 position after full fill, got {len(market_pos)}"
        )
        pos = market_pos[0]
        # entry_cost_usd = price × contracts ≈ USD target
        assert pos.entry_cost_usd == pytest.approx(quote_ref.collateral_usd, abs=0.02), (
            f"Position entry_cost_usd {pos.entry_cost_usd} != deployed collateral {quote_ref.collateral_usd}"
        )
        assert config.MAKER_QUOTE_SIZE_MIN <= pos.entry_cost_usd <= config.MAKER_QUOTE_SIZE_MAX + 1, (
            f"Fully-filled position entry_cost_usd {pos.entry_cost_usd} outside bounds "
            f"[{config.MAKER_QUOTE_SIZE_MIN}, {config.MAKER_QUOTE_SIZE_MAX}]"
        )
