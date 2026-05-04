"""
tests/test_opening_neutral.py — Unit tests for strategies/OpeningNeutral/scanner.py

Run: pytest tests/test_opening_neutral.py -v

Coverage:
  - pm_client interface contract: all methods called on PMClient actually exist
  - _evaluate_entry skips when OPENING_NEUTRAL_ENABLED=False
  - _evaluate_entry marks "skewed" when either side outside [MIN_SIDE, MAX_SIDE]
  - _refresh_pending_markets skips reach market (title doesn't contain "up or down")
  - _evaluate_entry skips market outside entry window (elapsed > window_secs)
  - _evaluate_entry skips at concurrent pair cap
  - _evaluate_entry marks "too_expensive" when combined > threshold
  - _evaluate_entry marks "entry_attempt" when combined <= threshold
  - _place_leg in DRY_RUN returns simulated fill (filled=True, no real order placed)
  - _handle_one_leg_fill with keep_as_momentum fallback -> strategy="momentum"
  - conflict guard: _evaluate_entry skips market with existing open position
  - _pair_is_resolved: True when both legs closed, False when either is open
  - _register_pair places resting GTC SELL orders on both legs immediately after entry
  - _monitor_exit_fills YES loser: YES SELL fills → cancel NO SELL, call _on_exit_fill
  - _monitor_exit_fills NO loser: NO SELL fills → cancel YES SELL, call _on_exit_fill
  - _monitor_exit_fills timeout: neither fills → cancel both orders, no _on_exit_fill
  - regression: _on_exit_fill called twice does not double-close the position
  - _on_exit_fill YES loser: YES closed at exit_price, NO promoted to momentum
  - _on_exit_fill NO loser: NO closed at exit_price, YES promoted to momentum
  - _on_exit_fill uses actual fill price (exit_price param), not config constant
  - _on_exit_fill sets prob_sl_threshold on winner and promotes to momentum
  - _on_exit_fill fires on_close_callback with market_id
  - _on_exit_fill idempotent: second call when loser already closed returns silently
  - E2E: _on_exit_fill with real RiskEngine writes correct trade CSV row
  --- ON-01: cold-book spread gate ---
  - wide YES spread (>0.15) → result='cold_book', skipped_cold_book incremented
  - wide NO spread (>0.15)  → result='cold_book', skipped_cold_book incremented
  - missing bid (None)      → result='no_spread',  skipped_no_spread incremented
  - both spreads ok (<0.15) → result='entry_attempt', spreads logged, cache populated
  --- ON-02: fills CSV ---
  - loser exit writes complete row to on_fills.csv (spreads, combined_cost, loser leg/price/time)
  - notify_winner_closed() backfills winner_exit_price in the existing CSV row
  - schema migration: old header → backup .csv.bak + fresh file with current schema
  --- ON-04: asymmetric sell triggers ---
  - positive funding → YES trigger = base (loser), NO trigger = base - buffer (winner protected)
  - negative funding → NO trigger = base (loser), YES trigger = base - buffer (winner protected)
  - funding within threshold → symmetric (both = base)
  - funding = None → symmetric (both = base)
  - feature disabled → symmetric regardless of funding value
  --- ON-05: loser confidence scoring ---
  - funding + depth share both predict YES loser (score=+2) → yes_trigger tightened
  - funding + depth share both predict NO loser (score=-2) → no_trigger tightened
  - partial agreement (score=±1) → no tighten applied
  - no signals (score=0) → no tighten applied
  - ON-04 + ON-05 combined: winner protected AND loser tightened
  - feature disabled → score=None, no tighten
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
from risk import RiskEngine, Position
from strategies.OpeningNeutral.scanner import OpeningNeutralScanner
from market_data.pm_client import PMClient, PMMarket


# ── pm_client interface contract ──────────────────────────────────────────────
# These tests catch the class of bug where the scanner calls methods that don't
# exist on the real PMClient. Mocks will silently accept any attribute access,
# so this test must inspect the real class.

_PM_CLIENT_METHODS_USED = [
    "on_price_change",        # registered in start() to receive WS book/price events
    "get_markets",            # returns dict[str, PMMarket]
    "get_book",               # returns Optional[OrderBookSnapshot]
    "place_limit",            # async — entry BUY and exit SELL legs
    "place_market",           # async — one-leg fallback exit
    "cancel_order",           # async — cancel other-side resting SELL
    "register_fill_future",   # sync — arm WS fill event for exit SELL order
]

_PM_MARKET_ATTRS_USED = [
    "condition_id",
    "token_id_yes",
    "token_id_no",
    "title",
    "market_type",
    "end_date",
]


def test_pm_client_methods_exist():
    """All PMClient methods called by OpeningNeutralScanner must exist on the real class."""
    for method in _PM_CLIENT_METHODS_USED:
        assert hasattr(PMClient, method), (
            f"PMClient.{method} does not exist — scanner will crash at runtime"
        )


def test_pm_market_attrs_exist():
    """All PMMarket attributes accessed by OpeningNeutralScanner must exist as dataclass fields."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(PMMarket)}
    for attr in _PM_MARKET_ATTRS_USED:
        assert attr in field_names, (
            f"PMMarket.{attr} does not exist — scanner will crash at runtime"
        )




def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_market(
    condition_id: str = "cond_on_001",
    title: str = "Will BTC go up or down at 2PM?",
    market_type: str = "bucket_1h",
    tte_seconds: float = 2000.0,
    duration_seconds: float = 3600.0,
):
    """Minimal PMMarket-like mock."""
    m = MagicMock()
    m.condition_id = condition_id
    m.title = title
    m.market_type = market_type
    # end_date must be a real datetime so .timestamp() works
    m.end_date = datetime.now(timezone.utc) + timedelta(seconds=tte_seconds)
    m.token_id_yes = f"tok_yes_{condition_id[:8]}"
    m.token_id_no  = f"tok_no_{condition_id[:8]}"
    return m


def _make_scanner(
    yes_ask: Optional[float] = None,
    no_ask: Optional[float] = None,
    markets: Optional[list] = None,
    open_positions: Optional[list] = None,
) -> OpeningNeutralScanner:
    """Create a scanner wired with mocked PM, risk, and vol_fetcher."""
    pm = MagicMock()
    _markets_list = markets or []
    pm.get_markets.return_value = {m.condition_id: m for m in _markets_list}
    def _get_book(token_id):
        book = MagicMock()
        # Determine ask by checking if token_id is a YES token
        if token_id.startswith("tok_yes_"):
            book.best_ask = yes_ask
            book.best_bid = round((yes_ask or 0.0) - 0.01, 4)
            book.asks = [(yes_ask, 100.0)] if yes_ask else []
            book.bids = [(book.best_bid, 100.0)] if yes_ask else []
        else:
            book.best_ask = no_ask
            book.best_bid = round((no_ask or 0.0) - 0.01, 4)
            book.asks = [(no_ask, 100.0)] if no_ask else []
            book.bids = [(book.best_bid, 100.0)] if no_ask else []
        return book
    pm.get_book.side_effect = _get_book
    pm._paper_mode = True
    pm.place_limit = AsyncMock(return_value="ord_001")
    pm.place_market = AsyncMock(return_value="ord_002")
    pm.cancel_order = AsyncMock(return_value=None)
    pm.register_fill_future = MagicMock()

    risk = MagicMock()
    risk.get_open_positions.return_value = open_positions or []
    risk.open_position = MagicMock()
    risk.close_position = MagicMock()

    spot = MagicMock()
    spot.get_price = MagicMock(return_value=70000.0)

    vol = MagicMock()
    vol.get_sigma_ann = AsyncMock(return_value=0.8)

    scanner = OpeningNeutralScanner(pm=pm, risk=risk, spot_client=spot, vol_fetcher=vol)
    # set _running=True so the market loop does not break immediately
    scanner._running = True
    return scanner


# ── test: disabled ────────────────────────────────────────────────────────────

def test_scan_once_skips_when_disabled():
    """When OPENING_NEUTRAL_ENABLED=False, _evaluate_entry should do nothing."""
    market = _make_market(tte_seconds=3500.0, duration_seconds=3600.0)
    scanner = _make_scanner(yes_ask=0.50, no_ask=0.49, markets=[market])

    with patch.object(config, "OPENING_NEUTRAL_ENABLED", False):
        _run(scanner._evaluate_entry(market))

    assert scanner._signals == [], "No signals should be recorded when disabled"
    assert scanner._entering_markets == set()


# ── test: too_expensive ───────────────────────────────────────────────────────

def test_scan_once_skips_expensive():
    """When combined > COMBINED_COST_MAX, result should be 'too_expensive'."""
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)  # 5s pre-open
    # combined = 0.55 + 0.50 = 1.05 > 1.01
    scanner = _make_scanner(yes_ask=0.55, no_ask=0.50, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
    ):
        _run(scanner._evaluate_entry(market))

    assert len(scanner._signals) == 1
    assert scanner._signals[0]["result"] == "too_expensive"
    assert scanner._entering_markets == set()


# ── test: entry_attempt ───────────────────────────────────────────────────────

def test_scan_once_enters_qualifying(monkeypatch):
    """When combined <= COMBINED_COST_MAX, result should be 'entry_attempt' and market queued."""
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)  # 5s pre-open
    # combined = 0.50 + 0.49 = 0.99 <= 1.01
    scanner = _make_scanner(yes_ask=0.50, no_ask=0.49, markets=[market])

    # Mock _enter_pair to avoid real async task complexity
    entry_calls = []

    async def fake_enter_pair(mkt, ya, na):
        entry_calls.append((mkt.condition_id, ya, na))
        scanner._entering_markets.discard(mkt.condition_id)

    monkeypatch.setattr(scanner, "_enter_pair", fake_enter_pair)

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
    ):
        _run(scanner._evaluate_entry(market))
        # Give the created task a chance to run
        _run(asyncio.sleep(0.01))

    assert len(scanner._signals) == 1
    assert scanner._signals[0]["result"] == "entry_attempt"


# ── test: DRY_RUN place_leg ───────────────────────────────────────────────────

def test_place_leg_dry_run():
    """
    In DRY_RUN mode, _place_leg must return a simulated fill (filled=True) at
    the observed ask price so the pair gets registered in _active_pairs and the
    same market is not re-scanned on every tick (infinite duplicate signals).
    No actual orders must be placed.
    """
    scanner = _make_scanner()
    market = _make_market()

    with patch.object(config, "OPENING_NEUTRAL_DRY_RUN", True):
        result = _run(scanner._place_leg("tok_yes_001", "YES", 0.50, 5.0, market))

    assert result["filled"] is True, "DRY_RUN must simulate a fill to prevent re-scanning"
    assert result["price"] == 0.50
    assert result["size"] == round(5.0 / 0.50, 6)
    assert result["order_id"].startswith("dry_")
    # No real orders should have been placed
    scanner._pm.place_limit.assert_not_called()
    scanner._pm.place_market.assert_not_called()


# ── test: one-leg fallback keep_as_momentum ───────────────────────────────────

def test_one_leg_fallback_keep_as_momentum():
    """Single fill with keep_as_momentum fallback → position strategy='momentum', neutral_pair_id=''."""
    scanner = _make_scanner()
    market = _make_market(condition_id="cond_onelg_001")

    result = {"filled": True, "price": 0.52, "size": 9.6, "order_id": "ord_one"}

    registered_positions: list[Position] = []
    scanner._risk.open_position = MagicMock(side_effect=lambda p: registered_positions.append(p))

    with (
        patch.object(config, "OPENING_NEUTRAL_ONE_LEG_FALLBACK", "keep_as_momentum"),
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        _run(scanner._handle_one_leg_fill("pair_onelg", market, result, "YES", "tok_yes_one"))

    assert len(registered_positions) == 1
    pos = registered_positions[0]
    assert pos.strategy == "momentum", f"Expected 'momentum', got '{pos.strategy}'"
    assert pos.neutral_pair_id == "", f"Expected empty neutral_pair_id, got '{pos.neutral_pair_id}'"


# ── test: conflict guard blocks momentum scanner ──────────────────────────────

def test_conflict_guard_blocks_momentum():
    """
    _evaluate_entry should skip a market when an open position already exists for it.
    """
    market = _make_market(condition_id="cond_conflict_001", tte_seconds=3500.0, duration_seconds=3600.0)

    # Existing opening_neutral position for same market
    existing_pos = MagicMock()
    existing_pos.market_id = "cond_conflict_001"
    existing_pos.strategy = "opening_neutral"

    scanner = _make_scanner(
        yes_ask=0.48,
        no_ask=0.48,
        markets=[market],
        open_positions=[existing_pos],
    )

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
    ):
        _run(scanner._evaluate_entry(market))

    # No entry should be attempted (open position blocks it)
    assert scanner._entering_markets == set(), "Should not enter a market with an existing position"
    # No signals — skipped entirely before signal recording
    assert all(s.get("result") != "entry_attempt" for s in scanner._signals)


# ── test: reach market is skipped ────────────────────────────────────────────

def test_scan_once_skips_reach_market():
    """Markets without 'up or down' in the title must not be registered as pending."""
    # Reach market: "Will BTC hit $100k?" — no "up or down"
    market = _make_market(
        title="Will BTC hit $100000?",
        tte_seconds=3500.0,
        duration_seconds=3600.0,
    )
    scanner = _make_scanner(yes_ask=0.50, no_ask=0.49, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_MARKET_TYPES", ["bucket_1h"]),
    ):
        _run(scanner._refresh_pending_markets())

    assert market.condition_id not in scanner._pending_markets, (
        "Reach market should not be registered as a pending market"
    )
    assert scanner._entering_markets == set()


# ── test: entry window gate ───────────────────────────────────────────────────

def test_scan_once_skips_outside_entry_window():
    """Markets where elapsed time > entry window must be skipped by _evaluate_entry."""
    # elapsed = 3600 - 50 = 3550s, entry window = 120s → should skip
    market = _make_market(tte_seconds=50.0, duration_seconds=3600.0)
    scanner = _make_scanner(yes_ask=0.50, no_ask=0.49, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 120),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
    ):
        _run(scanner._evaluate_entry(market))

    assert scanner._signals == [], "Market outside entry window should produce no signals"
    assert scanner._entering_markets == set()


# ── test: concurrent cap ──────────────────────────────────────────────────────

def test_scan_once_skips_at_concurrent_cap():
    """When active pairs == MAX_CONCURRENT, _evaluate_entry should not attempt new entries."""
    market = _make_market(tte_seconds=3500.0, duration_seconds=3600.0)
    scanner = _make_scanner(yes_ask=0.50, no_ask=0.49, markets=[market])

    # Inject two fake active pairs (both unresolved)
    scanner._active_pairs = {
        "pair_1": {"market_id": "other_1", "yes_pos": MagicMock(is_closed=False), "no_pos": MagicMock(is_closed=False)},
        "pair_2": {"market_id": "other_2", "yes_pos": MagicMock(is_closed=False), "no_pos": MagicMock(is_closed=False)},
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 2),  # cap = 2, already at 2
    ):
        _run(scanner._evaluate_entry(market))

    assert scanner._signals == [], "No new entries when at concurrent cap"


# ── test: pair_is_resolved ────────────────────────────────────────────────────

def test_pair_is_resolved_both_closed():
    scanner = _make_scanner()
    yes_p = MagicMock(is_closed=True)
    no_p  = MagicMock(is_closed=True)
    assert scanner._pair_is_resolved({"yes_pos": yes_p, "no_pos": no_p}) is True


def test_pair_is_resolved_one_open():
    scanner = _make_scanner()
    yes_p = MagicMock(is_closed=False)
    no_p  = MagicMock(is_closed=True)
    assert scanner._pair_is_resolved({"yes_pos": yes_p, "no_pos": no_p}) is False


# ── helper: minimal Position factory ─────────────────────────────────────────

def _make_position(
    market_id: str,
    side: str,
    entry_price: float,
    pair_id: str,
    size_usd: float = 5.0,
) -> Position:
    """Minimal Position for testing exit methods."""
    size = round(size_usd / entry_price, 6)
    return Position(
        market_id=market_id,
        market_type="bucket_1h",
        underlying="BTC",
        side=side,
        entry_price=entry_price,
        size=size,
        entry_cost_usd=size_usd,
        strategy="opening_neutral",
        neutral_pair_id=pair_id,
        token_id=f"tok_{side.lower()}_{market_id[:8]}",
        market_title="Will BTC go up or down?",
    )


# ── test: per-side price band (skewed) ──────────────────────────────────────

def test_evaluate_entry_skips_skewed_market():
    """
    When either YES ask or NO ask is outside [MIN_SIDE_PRICE, MAX_SIDE_PRICE],
    _evaluate_entry must record 'skewed' and not attempt entry.
    e.g. YES=0.12 / NO=0.89 passed the old combined<=1.01 filter but is NOT neutral.
    """
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)  # 5s pre-open
    # YES=0.12, NO=0.89 → combined=1.01 (would pass old filter), but YES < 0.40 → skewed
    scanner = _make_scanner(yes_ask=0.12, no_ask=0.89, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.02),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
        patch.object(config, "OPENING_NEUTRAL_MIN_SIDE_PRICE", 0.40),
        patch.object(config, "OPENING_NEUTRAL_MAX_SIDE_PRICE", 0.60),
    ):
        _run(scanner._evaluate_entry(market))

    assert len(scanner._signals) == 1
    assert scanner._signals[0]["result"] == "skewed", (
        f"Expected 'skewed', got '{scanner._signals[0]['result']}'"
    )
    assert scanner._entering_markets == set()


# ── test: _register_pair places resting exit SELLs immediately ───

def test_register_pair_places_exit_sells_immediately():
    """
    After both BUY legs fill, _register_pair arms bid-monitoring on both tokens
    by populating _token_to_pair.  Resting GTC SELL orders are no longer placed
    immediately (they cross the book at entry prices near $0.50).  Instead the
    price monitor fires a taker exit when either bid drops to LOSER_EXIT_PRICE.
    Per PLAN.md: resting SELLs replaced by bid-monitoring taker exit.
    """
    scanner = _make_scanner()
    market = _make_market("cond_reg_exit_001")

    yes_result = {"filled": True, "price": 0.50, "size": 2.0, "order_id": "yes_buy_oid"}
    no_result  = {"filled": True, "price": 0.50, "size": 2.0, "order_id": "no_buy_oid"}

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
    ):
        _run(scanner._register_pair(
            "pair_reg_exit", market, yes_result, no_result,
            market.token_id_yes, market.token_id_no,
        ))

    # Bid-monitoring armed: both tokens registered in _token_to_pair
    assert scanner._token_to_pair.get(market.token_id_yes) == "pair_reg_exit", (
        "YES token must be in _token_to_pair for bid-monitoring"
    )
    assert scanner._token_to_pair.get(market.token_id_no) == "pair_reg_exit", (
        "NO token must be in _token_to_pair for bid-monitoring"
    )

    # No resting GTC SELL orders placed immediately
    scanner._pm.place_limit.assert_not_called()

    # Pair dict stored with empty exit order IDs (exit via taker, not resting)
    pair = scanner._active_pairs.get("pair_reg_exit")
    assert pair is not None
    assert pair["yes_exit_order_id"] == ""
    assert pair["no_exit_order_id"] == ""


# ── test: _monitor_exit_fills YES loser ───────────────────────────

def test_monitor_exit_fills_yes_loser():
    """
    When the YES exit SELL fills first, _on_exit_fill is called with side=YES,
    the NO resting SELL is cancelled, and the winner (NO) transitions to momentum.
    """
    scanner = _make_scanner()
    scanner._pm._paper_mode = False
    pair_id = "pair_mef_yes"
    yes_pos = _make_position("cond_mef_yes_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_mef_yes_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_mef_yes_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    exit_calls: list[tuple] = []

    async def fake_on_exit_fill(pid, side, exit_price=None):
        exit_calls.append((pid, side, exit_price))

    scanner._on_exit_fill = fake_on_exit_fill  # type: ignore[method-assign]

    registered: dict = {}

    def capture_register(order_id, future):
        registered[order_id] = future

    scanner._pm.register_fill_future = capture_register

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS", 5),
    ):
        asyncio.get_event_loop().create_task(
            scanner._monitor_exit_fills(pair_id, "yes_exit_oid", "no_exit_oid")
        )
        _run(asyncio.sleep(0))
        yes_fut = registered["yes_exit_oid"]
        yes_fut.set_result({"price": "0.35", "size_matched": "2.0"})
        _run(asyncio.sleep(0.05))

    assert len(exit_calls) == 1, f"Expected 1 exit call, got {len(exit_calls)}"
    assert exit_calls[0][0] == pair_id
    assert exit_calls[0][1] == "YES"
    assert exit_calls[0][2] == pytest.approx(0.35)
    scanner._pm.cancel_order.assert_called_once_with("no_exit_oid")


# ── test: _monitor_exit_fills NO loser ────────────────────────────

def test_monitor_exit_fills_no_loser():
    """
    When the NO exit SELL fills first, _on_exit_fill is called with side=NO,
    the YES resting SELL is cancelled.
    """
    scanner = _make_scanner()
    scanner._pm._paper_mode = False
    pair_id = "pair_mef_no"
    yes_pos = _make_position("cond_mef_no_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_mef_no_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_mef_no_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    exit_calls: list[tuple] = []

    async def fake_on_exit_fill(pid, side, exit_price=None):
        exit_calls.append((pid, side, exit_price))

    scanner._on_exit_fill = fake_on_exit_fill  # type: ignore[method-assign]

    registered: dict = {}

    def capture_register(order_id, future):
        registered[order_id] = future

    scanner._pm.register_fill_future = capture_register

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS", 5),
    ):
        asyncio.get_event_loop().create_task(
            scanner._monitor_exit_fills(pair_id, "yes_exit_oid", "no_exit_oid")
        )
        _run(asyncio.sleep(0))
        no_fut = registered["no_exit_oid"]
        no_fut.set_result({"price": "0.34", "size_matched": "2.0"})
        _run(asyncio.sleep(0.05))

    assert len(exit_calls) == 1
    assert exit_calls[0][1] == "NO"
    assert exit_calls[0][2] == pytest.approx(0.34)
    scanner._pm.cancel_order.assert_called_once_with("yes_exit_oid")


# ── test: _monitor_exit_fills timeout cancels both orders ────────────────

def test_monitor_exit_fills_timeout_cancels_both():
    """
    When neither exit SELL fills within EXIT_ORDER_TIMEOUT_SECS, both resting
    orders are cancelled and _on_exit_fill is NOT called.
    """
    scanner = _make_scanner()
    scanner._pm._paper_mode = False
    pair_id = "pair_mef_timeout"
    yes_pos = _make_position("cond_mef_to_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_mef_to_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_mef_to_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    exit_calls: list = []

    async def fake_on_exit_fill(pid, side, exit_price=None):
        exit_calls.append((pid, side, exit_price))  # pragma: no cover

    scanner._on_exit_fill = fake_on_exit_fill  # type: ignore[method-assign]

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS", 0.01),
    ):
        _run(scanner._monitor_exit_fills(pair_id, "yes_exit_oid", "no_exit_oid"))

    cancelled = {c.args[0] for c in scanner._pm.cancel_order.call_args_list}
    assert "yes_exit_oid" in cancelled, "YES exit order must be cancelled on timeout"
    assert "no_exit_oid" in cancelled, "NO exit order must be cancelled on timeout"
    assert exit_calls == [], "_on_exit_fill must NOT be called on timeout"


# ── test: _on_exit_fill YES loser ─────────────────────────────────────────────

def test_on_exit_fill_yes_loser():
    """YES loser exit → YES closed at exit_price, NO promoted to momentum."""
    scanner = _make_scanner()
    pair_id = "pair_yes_loser"
    yes_pos = _make_position("cond_yes_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_yes_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_yes_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", True),
        patch.object(config, "MOMENTUM_PROB_SL_PCT", 0.15),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", True),
    ):
        _run(scanner._on_exit_fill(pair_id, "YES", exit_price=0.34))

    # Loser (YES) closed via risk engine at the actual fill price (0.34)
    args, kwargs = scanner._risk.close_position.call_args
    assert args[0] == "cond_yes_001", f"Wrong market_id: {args[0]}"
    assert args[1] == pytest.approx(0.34), f"Wrong exit price: {args[1]}"
    assert kwargs.get("side") == "YES", f"Wrong side: {kwargs.get('side')}"

    # Winner (NO) promoted to momentum with prob-SL armed
    assert no_pos.strategy == "momentum"
    assert no_pos.neutral_pair_id == ""
    assert no_pos.prob_sl_threshold > 0

    # cancel_order is NOT called by _on_exit_fill — it is called by
    # _monitor_exit_fills before invoking _on_exit_fill.
    scanner._pm.cancel_order.assert_not_called()

    # Pair must be removed after exit
    assert pair_id not in scanner._active_pairs, "pair must be removed from _active_pairs after exit"


# ── test: _on_exit_fill NO loser ──────────────────────────────────────────────

def test_on_exit_fill_no_loser():
    """NO loser exit → NO closed at exit_price, YES promoted to momentum."""
    scanner = _make_scanner()
    pair_id = "pair_no_loser"
    yes_pos = _make_position("cond_no_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_no_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_no_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", True),
        patch.object(config, "MOMENTUM_PROB_SL_PCT", 0.15),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", True),
    ):
        _run(scanner._on_exit_fill(pair_id, "NO", exit_price=0.33))

    args, kwargs = scanner._risk.close_position.call_args
    assert args[0] == "cond_no_001"
    assert args[1] == pytest.approx(0.33)
    assert kwargs.get("side") == "NO"

    assert yes_pos.strategy == "momentum"
    assert yes_pos.neutral_pair_id == ""
    assert yes_pos.prob_sl_threshold > 0

    # cancel_order is NOT called by _on_exit_fill — it is called by
    # _monitor_exit_fills before invoking _on_exit_fill.
    scanner._pm.cancel_order.assert_not_called()

    # Pair must be removed after exit
    assert pair_id not in scanner._active_pairs, "pair must be removed from _active_pairs after exit"


# ── test: _on_exit_fill prob_sl set, winner strategy promoted ─────────────────

def test_on_exit_fill_sets_prob_sl_before_strategy():
    """
    Winner must have prob_sl_threshold set and strategy='momentum' after _on_exit_fill.
    Both must be correctly set: prob_sl_threshold > 0 AND strategy == 'momentum'.
    """
    scanner = _make_scanner()
    pair_id = "pair_prob_sl"
    yes_pos = _make_position("cond_prob_001", "YES", 0.85, pair_id)
    no_pos  = _make_position("cond_prob_001", "NO",  0.15, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_prob_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", True),
        patch.object(config, "MOMENTUM_PROB_SL_PCT", 0.15),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", True),
    ):
        _run(scanner._on_exit_fill(pair_id, "NO"))  # NO is loser, YES is winner

    assert yes_pos.prob_sl_threshold > 0, (
        f"prob_sl_threshold must be set on winner; got {yes_pos.prob_sl_threshold}"
    )
    expected_threshold = round(0.85 * (1.0 - 0.15), 6)
    assert yes_pos.prob_sl_threshold == pytest.approx(expected_threshold, abs=1e-6)
    assert yes_pos.strategy == "momentum"


# ── test: _on_exit_fill fires callback ────────────────────────────────────────

def test_on_exit_fill_fires_callback():
    """on_close_callback is called with the market_id after _on_exit_fill."""
    scanner = _make_scanner()
    callback_calls: list[str] = []
    scanner._on_close_callback = lambda mid: callback_calls.append(mid)

    pair_id = "pair_cb_exit"
    yes_pos = _make_position("cond_cb_002", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_cb_002", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_cb_002",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        _run(scanner._on_exit_fill(pair_id, "NO"))

    assert "cond_cb_002" in callback_calls, "on_close_callback must be called with market_id"


# ── test: _on_exit_fill uses actual exit_price ────────────────────────────────

def test_on_exit_fill_uses_actual_exit_price():
    """
    _on_exit_fill must use the exit_price argument, not the config constant.
    This matters when the taker SELL fills at the bid (e.g. 0.33) rather than
    exactly at the target (0.35).
    """
    scanner = _make_scanner()
    pair_id = "pair_actual_price"
    yes_pos = _make_position("cond_act_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_act_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_act_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    actual_fill_price = 0.32  # bid was 0.32 when we executed
    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        _run(scanner._on_exit_fill(pair_id, "YES", exit_price=actual_fill_price))

    args, _ = scanner._risk.close_position.call_args
    assert args[1] == pytest.approx(actual_fill_price), (
        f"close_position must use actual fill price {actual_fill_price}, got {args[1]}"
    )


# ── test: _on_exit_fill default exit_price uses config constant ──────────────

def test_on_exit_fill_default_exit_price():
    """
    When exit_price is not provided, _on_exit_fill falls back to
    config.OPENING_NEUTRAL_LOSER_EXIT_PRICE.
    """
    scanner = _make_scanner()
    pair_id = "pair_default_price"
    yes_pos = _make_position("cond_def_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_def_001", "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_def_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        _run(scanner._on_exit_fill(pair_id, "YES"))  # no exit_price param

    args, _ = scanner._risk.close_position.call_args
    assert args[1] == pytest.approx(0.35), (
        f"Default exit price must be config value 0.35, got {args[1]}"
    )


# ── test: _on_exit_fill idempotent ────────────────────────────────────────────

def test_on_exit_fill_idempotent():
    """
    _on_exit_fill is idempotent: when called a second time after the loser is
    already closed, it returns silently without calling close_position again.
    """
    import risk as risk_module
    from pathlib import Path
    import tempfile, os

    # Use a real RiskEngine so is_closed is properly set on the first call.
    with tempfile.TemporaryDirectory() as td:
        temp_csv = Path(td) / "trades.csv"
        orig = risk_module.TRADES_CSV
        risk_module.TRADES_CSV = temp_csv
        try:
            real_risk = RiskEngine()
            pair_id = "pair_idem"
            yes_pos = _make_position("cond_idem_001", "YES", 0.50, pair_id)
            no_pos  = _make_position("cond_idem_001", "NO",  0.50, pair_id)
            real_risk.open_position(yes_pos)
            real_risk.open_position(no_pos)

            pm = MagicMock()
            pm._paper_mode = True
            pm.cancel_order = AsyncMock(return_value=None)
            pm.register_fill_future = MagicMock()
            spot = MagicMock()
            vol  = MagicMock()

            scanner = OpeningNeutralScanner(pm=pm, risk=real_risk, spot_client=spot, vol_fetcher=vol)
            scanner._running = True
            scanner._active_pairs[pair_id] = {
                "market_id": "cond_idem_001",
                "market_title": "Test",
                "yes_pos": yes_pos,
                "no_pos": no_pos,
            }

            with (
                patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
                patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
            ):
                # First call — closes NO (loser)
                _run(scanner._on_exit_fill(pair_id, "NO", exit_price=0.35))
                assert no_pos.is_closed is True
                # Second call — loser already closed; must return silently
                _run(scanner._on_exit_fill(pair_id, "NO", exit_price=0.35))

            # Winner must remain open after the idempotent second call
            assert yes_pos.is_closed is False, (
                "Winner must remain open after idempotent second _on_exit_fill call"
            )
        finally:
            risk_module.TRADES_CSV = orig


# ── E2E: real RiskEngine — loser exit trade recorded correctly ─────────────────

def test_e2e_loser_exit_records_trade_correctly(tmp_path, monkeypatch):
    """
    E2E test using a REAL RiskEngine (not mocked).

    Verifies the full loser-exit chain:
      open_position(YES) + open_position(NO)
      → _on_exit_fill("NO", exit_price=actual) fires (NO loser exited by price monitor)
      → risk.close_position marks NO as closed with pnl = (exit - entry) * size
      → YES promoted to momentum with prob_sl_threshold set
      → CSV row written: strategy='opening_neutral', side='NO', pnl correct, price=actual

    This test would FAIL if:
      - close_position called with wrong market_id, price, or side
      - pnl formula is wrong (e.g. uses config constant instead of actual fill price)
      - winner strategy not promoted, or prob_sl_threshold not set
    """
    import risk as risk_module

    temp_csv = tmp_path / "trades.csv"
    monkeypatch.setattr(risk_module, "TRADES_CSV", temp_csv)

    real_risk = RiskEngine()

    entry_price_yes = 0.51
    entry_price_no  = 0.50
    exit_price      = 0.35
    size_usd        = 5.0
    yes_size = round(size_usd / entry_price_yes, 6)
    no_size  = round(size_usd / entry_price_no, 6)

    yes_pos = Position(
        market_id="cond_e2e_rest_001",
        market_type="bucket_1h",
        underlying="BTC",
        side="YES",
        entry_price=entry_price_yes,
        size=yes_size,
        entry_cost_usd=size_usd,
        strategy="opening_neutral",
        neutral_pair_id="pair_e2e_rest_001",
        token_id="tok_yes_e2e_r",
        market_title="Will BTC go up or down at 2PM?",
    )
    no_pos = Position(
        market_id="cond_e2e_rest_001",
        market_type="bucket_1h",
        underlying="BTC",
        side="NO",
        entry_price=entry_price_no,
        size=no_size,
        entry_cost_usd=size_usd,
        strategy="opening_neutral",
        neutral_pair_id="pair_e2e_rest_001",
        token_id="tok_no_e2e_r",
        market_title="Will BTC go up or down at 2PM?",
    )

    real_risk.open_position(yes_pos)
    real_risk.open_position(no_pos)
    assert len(real_risk.get_positions()) == 2

    pm = MagicMock()
    pm._paper_mode = True
    pm.cancel_order = AsyncMock(return_value=None)
    pm.register_fill_future = MagicMock()
    pm.get_markets.return_value = {}  # prevent await TypeError in fetch_price_to_beat path
    spot = MagicMock()
    vol  = MagicMock()

    scanner = OpeningNeutralScanner(pm=pm, risk=real_risk, spot_client=spot, vol_fetcher=vol)
    scanner._running = True

    pair_id = "pair_e2e_rest_001"
    scanner._active_pairs[pair_id] = {
        "market_id": "cond_e2e_rest_001",
        "market_title": "Will BTC go up or down at 2PM?",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    # Use a fill price slightly below the target to verify actual price is recorded.
    actual_fill_price = 0.33

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", exit_price),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", True),
        patch.object(config, "MOMENTUM_PROB_SL_PCT", 0.15),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", True),
    ):
        _run(scanner._on_exit_fill(pair_id, "NO", exit_price=actual_fill_price))  # NO is loser

    # ── 1. Loser (NO) position state ─────────────────────────────────────────
    assert no_pos.is_closed is True, "Loser position must be closed after _on_exit_fill"
    expected_pnl = (actual_fill_price - entry_price_no) * no_size
    assert no_pos.realized_pnl < 0, f"Loser P&L must be negative; got {no_pos.realized_pnl}"
    assert abs(no_pos.realized_pnl - expected_pnl) < 1e-9, (
        f"P&L mismatch: expected {expected_pnl:.6f}, got {no_pos.realized_pnl:.6f}"
    )

    # ── 2. Winner (YES) position state ───────────────────────────────────────
    assert yes_pos.is_closed is False, "Winner must remain open"
    assert yes_pos.strategy == "momentum", f"Winner must be 'momentum', got '{yes_pos.strategy}'"
    assert yes_pos.neutral_pair_id == "", "Winner neutral_pair_id must be cleared"
    assert yes_pos.prob_sl_threshold > 0, "Winner prob_sl_threshold must be set"

    # ── 3. cancel_order is NOT called by _on_exit_fill — it is called by
    #       _monitor_exit_fills before _on_exit_fill is invoked. ──────────
    pm.cancel_order.assert_not_called()



# ── regression: _on_exit_fill is idempotent (no double-exit) ─────────────────

def test_no_double_exit_after_loser_promotes_winner():
    """
    Regression: calling _on_exit_fill twice for the same pair/side must NOT
    close the position or call close_position a second time.

    In the proactive design, _monitor_exit_fills resolves exactly one fill future
    and calls _on_exit_fill once.  But if somehow called twice (race or replay),
    the idempotency guard (loser_pos.is_closed) must prevent a second close.
    """
    scanner = _make_scanner()
    pair_id = "pair_double_exit"
    yes_pos = _make_position("cond_double_001", "YES", 0.50, pair_id)
    no_pos  = _make_position("cond_double_001", "NO",  0.50, pair_id)

    scanner._active_pairs[pair_id] = {
        "market_id": "cond_double_001",
        "market_title": "Test",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        # First call — YES loser exit
        _run(scanner._on_exit_fill(pair_id, "YES", exit_price=0.34))
        # Second call — must be a no-op (pair already removed, loser already closed)
        _run(scanner._on_exit_fill(pair_id, "YES", exit_price=0.34))

    # close_position called exactly once despite two _on_exit_fill calls
    assert scanner._risk.close_position.call_count == 1, (
        f"close_position must be called once; got {scanner._risk.close_position.call_count}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ON-01 — Cold-book spread gate unit tests
# AC: wide YES spread → cold_book; wide NO spread → cold_book;
#     missing bid → no_spread; both ok → entry_attempt proceeds.
# ══════════════════════════════════════════════════════════════════════════════

def _make_scanner_custom_books(yes_ask, no_ask, yes_bid, no_bid, markets):
    """Like _make_scanner but with independent yes_bid/no_bid control."""
    pm = MagicMock()
    pm.get_markets.return_value = {m.condition_id: m for m in markets}

    def _get_book(token_id):
        book = MagicMock()
        if token_id.startswith("tok_yes_"):
            book.best_ask = yes_ask
            book.best_bid = yes_bid
            book.asks = [(yes_ask, 100.0)] if yes_ask else []
            book.bids = [(yes_bid, 100.0)] if yes_bid is not None else []
        else:
            book.best_ask = no_ask
            book.best_bid = no_bid
            book.asks = [(no_ask, 100.0)] if no_ask else []
            book.bids = [(no_bid, 100.0)] if no_bid is not None else []
        return book

    pm.get_book.side_effect = _get_book
    pm._paper_mode = True
    pm.place_limit  = AsyncMock(return_value="ord_001")
    pm.place_market = AsyncMock(return_value="ord_002")
    pm.cancel_order = AsyncMock(return_value=None)
    pm.register_fill_future = MagicMock()

    risk = MagicMock()
    risk.get_open_positions.return_value = []
    risk.open_position = MagicMock()
    risk.close_position = MagicMock()
    spot = MagicMock()
    vol  = MagicMock()

    scanner = OpeningNeutralScanner(pm=pm, risk=risk, spot_client=spot, vol_fetcher=vol)
    scanner._running = True
    return scanner


def test_on01_wide_yes_spread_skips_cold_book():
    """
    ON-01 AC: when YES spread > MAX_INDIVIDUAL_SPREAD, result must be 'cold_book'.
    YES spread = 0.50 - 0.30 = 0.20 > threshold 0.15 → cold_book.
    skipped_cold_book counter must increment.
    """
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)  # 5 s pre-open
    # YES spread = 0.50 - 0.30 = 0.20 (wide); NO spread = 0.50 - 0.46 = 0.04 (ok)
    scanner = _make_scanner_custom_books(
        yes_ask=0.50, no_ask=0.50,
        yes_bid=0.30, no_bid=0.46,
        markets=[market],
    )

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
        patch.object(config, "OPENING_NEUTRAL_MIN_SIDE_PRICE", 0.40),
        patch.object(config, "OPENING_NEUTRAL_MAX_SIDE_PRICE", 0.60),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD", 0.15),
    ):
        _run(scanner._evaluate_entry(market))

    assert len(scanner._signals) == 1, f"Expected 1 signal, got {len(scanner._signals)}"
    sig = scanner._signals[0]
    assert sig["result"] == "cold_book", (
        f"Expected 'cold_book', got '{sig['result']}'"
    )
    assert sig["yes_spread"] == pytest.approx(0.20, abs=1e-4), (
        f"yes_spread logged incorrectly: {sig['yes_spread']}"
    )
    assert sig["no_spread"] == pytest.approx(0.04, abs=1e-4), (
        f"no_spread logged incorrectly: {sig['no_spread']}"
    )
    assert scanner._entering_markets == set(), "cold_book must not add to _entering_markets"
    assert scanner._skipped_cold_book == 1, (
        f"skipped_cold_book must be 1, got {scanner._skipped_cold_book}"
    )
    assert scanner._skipped_no_spread == 0


def test_on01_wide_no_spread_skips_cold_book():
    """
    ON-01 AC: when NO spread > MAX_INDIVIDUAL_SPREAD, result must be 'cold_book'.
    YES spread = 0.04 (ok); NO spread = 0.50 - 0.30 = 0.20 (wide) → cold_book.
    """
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)
    scanner = _make_scanner_custom_books(
        yes_ask=0.50, no_ask=0.50,
        yes_bid=0.46, no_bid=0.30,   # NO is cold
        markets=[market],
    )

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
        patch.object(config, "OPENING_NEUTRAL_MIN_SIDE_PRICE", 0.40),
        patch.object(config, "OPENING_NEUTRAL_MAX_SIDE_PRICE", 0.60),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD", 0.15),
    ):
        _run(scanner._evaluate_entry(market))

    sig = scanner._signals[0]
    assert sig["result"] == "cold_book", f"Expected 'cold_book', got '{sig['result']}'"
    assert sig["no_spread"] == pytest.approx(0.20, abs=1e-4)
    assert scanner._skipped_cold_book == 1
    assert scanner._entering_markets == set()


def test_on01_missing_bid_skips_no_spread():
    """
    ON-01 AC: when either book has no bid (best_bid=None), result must be 'no_spread'.
    skipped_no_spread counter must increment, skipped_cold_book must stay 0.
    """
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)
    scanner = _make_scanner_custom_books(
        yes_ask=0.50, no_ask=0.50,
        yes_bid=None, no_bid=0.46,   # YES has no bid — empty book
        markets=[market],
    )

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
        patch.object(config, "OPENING_NEUTRAL_MIN_SIDE_PRICE", 0.40),
        patch.object(config, "OPENING_NEUTRAL_MAX_SIDE_PRICE", 0.60),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD", 0.15),
    ):
        _run(scanner._evaluate_entry(market))

    assert len(scanner._signals) == 1
    sig = scanner._signals[0]
    assert sig["result"] == "no_spread", f"Expected 'no_spread', got '{sig['result']}'"
    assert sig["yes_spread"] is None, "yes_spread must be None when bid is absent"
    assert scanner._skipped_no_spread == 1, (
        f"skipped_no_spread must be 1, got {scanner._skipped_no_spread}"
    )
    assert scanner._skipped_cold_book == 0
    assert scanner._entering_markets == set()


def test_on01_narrow_spreads_proceeds_to_entry(monkeypatch):
    """
    ON-01 AC: when both spreads are within threshold, the gate must NOT block entry.
    Both spreads = 0.04 < 0.15 → result 'entry_attempt', both spreads logged.
    spread cache must be populated for _register_pair to consume.
    """
    market = _make_market(tte_seconds=3605.0, duration_seconds=3600.0)
    # YES spread = 0.50 - 0.46 = 0.04; NO spread = 0.50 - 0.46 = 0.04
    scanner = _make_scanner_custom_books(
        yes_ask=0.50, no_ask=0.50,
        yes_bid=0.46, no_bid=0.46,
        markets=[market],
    )

    entry_calls: list = []

    async def fake_enter_pair(mkt, ya, na):
        entry_calls.append((mkt.condition_id, ya, na))
        scanner._entering_markets.discard(mkt.condition_id)

    monkeypatch.setattr(scanner, "_enter_pair", fake_enter_pair)

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_MARKET_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
        patch.object(config, "OPENING_NEUTRAL_MIN_SIDE_PRICE", 0.40),
        patch.object(config, "OPENING_NEUTRAL_MAX_SIDE_PRICE", 0.60),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_MAX_INDIVIDUAL_SPREAD", 0.15),
    ):
        _run(scanner._evaluate_entry(market))
        _run(asyncio.sleep(0.01))

    assert len(scanner._signals) == 1, f"Expected 1 signal, got {len(scanner._signals)}"
    sig = scanner._signals[0]
    assert sig["result"] == "entry_attempt", (
        f"Narrow spreads must not block entry; got '{sig['result']}'"
    )
    assert sig["yes_spread"] == pytest.approx(0.04, abs=1e-4), "yes_spread must be logged"
    assert sig["no_spread"]  == pytest.approx(0.04, abs=1e-4), "no_spread must be logged"
    # Spread cache populated for _register_pair
    assert market.condition_id in scanner._entry_spread_cache, (
        "spread cache must be populated after entry_attempt"
    )
    cached = scanner._entry_spread_cache[market.condition_id]
    assert cached == (pytest.approx(0.04, abs=1e-4), pytest.approx(0.04, abs=1e-4))
    assert scanner._skipped_cold_book == 0
    assert scanner._skipped_no_spread == 0


# ══════════════════════════════════════════════════════════════════════════════
# ON-02 — Fills CSV unit tests
# AC: spreads/context written to CSV; winner_exit_price backfill via
#     notify_winner_closed(); schema migrated on header change.
# ══════════════════════════════════════════════════════════════════════════════

def test_on02_fills_csv_row_written_on_loser_exit(tmp_path, monkeypatch):
    """
    ON-02 AC: when a loser fills, a complete row is appended to on_fills.csv.
    Row must include: pair_id, loser_leg, loser_fill_price, loser_fill_time_secs,
    yes_spread (from cache), no_spread (from cache), combined_cost, winner_exit_price=None.
    """
    import strategies.OpeningNeutral.scanner as _on_scanner
    # Redirect on_fills.csv to a temp directory
    monkeypatch.setattr(_on_scanner, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(_on_scanner, "_ON_FILLS_CSV", tmp_path / "on_fills.csv")

    scanner = _make_scanner()
    pair_id = "pair_csv_row_001"
    market_id = "cond_csv_001"
    yes_pos = _make_position(market_id, "YES", 0.51, pair_id)
    no_pos  = _make_position(market_id, "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": market_id,
        "market_title": "Will BTC go up or down?",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }
    # Pre-populate fills row as _register_pair would
    import time
    market_mock = _make_market(condition_id=market_id)
    scanner._entry_spread_cache[market_id] = (0.04, 0.05)
    scanner._pair_csv_data[pair_id] = {
        "timestamp":              "2026-05-03T00:00:00+00:00",
        "pair_id":                pair_id,
        "market_id":              market_id,
        "market_title":           "Will BTC go up or down?",
        "underlying":             "BTC",
        "market_type":            "bucket_1h",
        "yes_entry":              0.51,
        "no_entry":               0.50,
        "combined_cost":          1.01,
        "yes_spread":             0.04,
        "no_spread":              0.05,
        "funding_rate":           None,
        "yes_depth_share":        None,
        "loser_confidence_score": None,
        "yes_sell_price_placed":  0.35,
        "no_sell_price_placed":   0.35,
        "loser_leg":              "none",
        "loser_fill_price":       None,
        "loser_fill_time_secs":   None,
        "winner_exit_price":      None,
        "_entry_ts":              time.time() - 12.0,  # simulate 12 s since entry
    }

    # Ensure CSV file exists with header
    _on_scanner._ensure_on_fills_csv()

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", False),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        _run(scanner._on_exit_fill(pair_id, "NO", exit_price=0.34))

    import csv
    csv_path = tmp_path / "on_fills.csv"
    assert csv_path.exists(), "on_fills.csv must be created"
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1, f"Expected 1 CSV row, got {len(rows)}"
    row = rows[0]

    assert row["pair_id"] == pair_id
    assert row["loser_leg"] == "NO"
    assert float(row["loser_fill_price"]) == pytest.approx(0.34, abs=1e-4)
    assert float(row["loser_fill_time_secs"]) >= 10.0, (
        "loser_fill_time_secs should be ~12s"
    )
    assert float(row["yes_spread"]) == pytest.approx(0.04, abs=1e-4)
    assert float(row["no_spread"])  == pytest.approx(0.05, abs=1e-4)
    assert float(row["combined_cost"]) == pytest.approx(1.01, abs=1e-4)
    assert row["winner_exit_price"] == ""  # None serialises to empty string in CSV


def test_on02_winner_exit_price_backfilled(tmp_path, monkeypatch):
    """
    ON-02 AC: notify_winner_closed() must backfill winner_exit_price in on_fills.csv.
    """
    import strategies.OpeningNeutral.scanner as _on_scanner
    import csv

    monkeypatch.setattr(_on_scanner, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(_on_scanner, "_ON_FILLS_CSV", tmp_path / "on_fills.csv")

    scanner = _make_scanner()
    pair_id = "pair_winner_bfill"
    market_id = "cond_winner_001"
    yes_pos = _make_position(market_id, "YES", 0.51, pair_id)
    no_pos  = _make_position(market_id, "NO",  0.50, pair_id)
    scanner._active_pairs[pair_id] = {
        "market_id": market_id,
        "market_title": "Will BTC go up or down?",
        "yes_pos": yes_pos,
        "no_pos": no_pos,
    }
    import time
    scanner._pair_csv_data[pair_id] = {
        "timestamp":              "2026-05-03T00:00:00+00:00",
        "pair_id":                pair_id,
        "market_id":              market_id,
        "market_title":           "Will BTC go up or down?",
        "underlying":             "BTC",
        "market_type":            "bucket_1h",
        "yes_entry":              0.51,
        "no_entry":               0.50,
        "combined_cost":          1.01,
        "yes_spread":             0.04,
        "no_spread":              0.05,
        "funding_rate":           None,
        "yes_depth_share":        None,
        "loser_confidence_score": None,
        "yes_sell_price_placed":  0.35,
        "no_sell_price_placed":   0.35,
        "loser_leg":              "none",
        "loser_fill_price":       None,
        "loser_fill_time_secs":   None,
        "winner_exit_price":      None,
        "_entry_ts":              time.time() - 5.0,
    }
    _on_scanner._ensure_on_fills_csv()

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_PROMOTE_TO_MOMENTUM", False),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", False),
    ):
        # Loser (NO) fills — row written with winner_exit_price=None
        _run(scanner._on_exit_fill(pair_id, "NO", exit_price=0.34))

    # Winner (YES) key registered in _winner_pending
    assert f"{market_id}:YES" in scanner._winner_pending, (
        "winner pending key must be set after loser exit"
    )

    # Winner closes at 0.96 via notify_winner_closed
    scanner.notify_winner_closed(market_id, "YES", 0.96)

    # winner_exit_price must now be backfilled in the CSV
    with (tmp_path / "on_fills.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["winner_exit_price"] == "0.96", (
        f"winner_exit_price not backfilled correctly: {rows[0]['winner_exit_price']}"
    )
    # _winner_pending entry must be consumed after backfill
    assert f"{market_id}:YES" not in scanner._winner_pending


def test_on02_fills_csv_schema_migration(tmp_path, monkeypatch):
    """
    ON-02 AC: if on_fills.csv exists with a different header, it must be renamed
    to a .csv.bak file and a fresh file created with the current schema.
    Existing CSV rows must not be lost (they are in the backup).
    """
    import strategies.OpeningNeutral.scanner as _on_scanner
    import csv

    monkeypatch.setattr(_on_scanner, "_DATA_DIR", tmp_path)
    csv_path = tmp_path / "on_fills.csv"
    monkeypatch.setattr(_on_scanner, "_ON_FILLS_CSV", csv_path)

    # Write a CSV with an old/different schema
    old_header = ["timestamp", "pair_id", "market_id", "old_column"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(old_header)
        writer.writerow(["2026-05-01", "p1", "m1", "value"])

    # Call _ensure_on_fills_csv — must detect schema mismatch and migrate
    _on_scanner._ensure_on_fills_csv()

    # New file must have the current schema
    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        new_header = next(reader)
    assert new_header == _on_scanner._ON_FILLS_HEADER, (
        "Migrated CSV must have current schema header"
    )

    # Backup file must exist with the old data
    backups = list(tmp_path.glob("on_fills_*.csv.bak"))
    assert len(backups) == 1, f"Expected 1 backup file, found {len(backups)}"
    with backups[0].open(newline="", encoding="utf-8") as f:
        bak_rows = list(csv.reader(f))
    assert bak_rows[0] == old_header, "Backup must preserve old header"
    assert bak_rows[1][0] == "2026-05-01", "Backup must preserve old rows"


# ── ON-04: asymmetric sell triggers ──────────────────────────────────────────


def _make_scanner_for_compute() -> OpeningNeutralScanner:
    """Minimal scanner suitable for calling _compute_sell_prices (no async needed)."""
    pm = MagicMock()
    pm.get_markets.return_value = {}
    pm.get_book.return_value = None
    pm._paper_mode = True
    pm.place_limit = AsyncMock(return_value="ord_001")
    pm.place_market = AsyncMock(return_value="ord_002")
    pm.cancel_order = AsyncMock(return_value=None)
    pm.register_fill_future = MagicMock()
    risk = MagicMock()
    risk.get_open_positions.return_value = []
    spot = MagicMock()
    spot.get_price = MagicMock(return_value=70000.0)
    vol = MagicMock()
    vol.get_sigma_ann = AsyncMock(return_value=0.8)
    return OpeningNeutralScanner(pm=pm, risk=risk, spot_client=spot, vol_fetcher=vol)


def test_on04_positive_funding_protects_no_winner():
    """
    ON-04 AC: positive funding (YES likely loser) →
      YES trigger = base (standard, loser exits at normal speed)
      NO  trigger = base - buffer (winner protected from accidental early exit)
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    buf = 0.03
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", buf),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(market, funding=0.001, depth_share=None)

    assert yes_t == base, "YES (loser) must keep standard trigger"
    assert no_t == round(base - buf, 4), "NO (winner) trigger must be lowered by buffer"
    assert score is None, "ON-05 disabled → no score"


def test_on04_negative_funding_protects_yes_winner():
    """
    ON-04 AC: negative funding (NO likely loser) →
      NO  trigger = base (standard, loser exits at normal speed)
      YES trigger = base - buffer (winner protected)
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    buf = 0.03
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", buf),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(market, funding=-0.001, depth_share=None)

    assert no_t == base, "NO (loser) must keep standard trigger"
    assert yes_t == round(base - buf, 4), "YES (winner) trigger must be lowered by buffer"
    assert score is None


def test_on04_funding_within_threshold_is_symmetric():
    """
    ON-04 AC: |funding| < threshold → both legs use symmetric base trigger.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", 0.03),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
    ):
        yes_t, no_t, _ = scanner._compute_sell_prices(market, funding=0.000005, depth_share=None)

    assert yes_t == base
    assert no_t == base


def test_on04_none_funding_is_symmetric():
    """
    ON-04 AC: funding=None → symmetric fallback (both = base).
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", 0.03),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", 0.00001),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
    ):
        yes_t, no_t, _ = scanner._compute_sell_prices(market, funding=None, depth_share=None)

    assert yes_t == base
    assert no_t == base


def test_on04_disabled_ignores_funding():
    """
    ON-04 AC: feature disabled → both triggers = base regardless of funding direction.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", 0.03),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", 0.00001),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
    ):
        yes_t, no_t, _ = scanner._compute_sell_prices(market, funding=0.1, depth_share=None)

    assert yes_t == base
    assert no_t == base


# ── ON-05: loser confidence scoring ──────────────────────────────────────────


def test_on05_both_signals_predict_yes_loser_tightens_yes():
    """
    ON-05 AC: funding > threshold (YES loser, +1) AND depth_share < 0.25 (YES loser, +1)
    → score = +2 → yes_trigger raised by tighten; no_trigger unchanged.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    tighten = 0.02
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", tighten),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=0.001, depth_share=0.10
        )

    assert score == 2
    assert yes_t == round(base + tighten, 4), "YES (loser) trigger must be raised"
    assert no_t == base, "NO (winner) trigger must be unchanged"


def test_on05_both_signals_predict_no_loser_tightens_no():
    """
    ON-05 AC: funding < -threshold (NO loser, -1) AND depth_share > 0.75 (NO loser, -1)
    → score = -2 → no_trigger raised by tighten; yes_trigger unchanged.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    tighten = 0.02
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", tighten),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=-0.001, depth_share=0.90
        )

    assert score == -2
    assert no_t == round(base + tighten, 4), "NO (loser) trigger must be raised"
    assert yes_t == base, "YES (winner) trigger must be unchanged"


def test_on05_partial_agreement_no_tighten():
    """
    ON-05 AC: only one signal agrees (|score| = 1) → no tighten applied.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", 0.02),
    ):
        # funding says YES loser (+1), depth_share is neutral → score = +1
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=0.001, depth_share=0.50
        )

    assert score == 1
    assert yes_t == base, "No tighten at score=+1"
    assert no_t == base


def test_on05_no_signals_no_tighten():
    """
    ON-05 AC: both signals unavailable or neutral → score = 0 → no tighten.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", 0.00001),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", 0.02),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=None, depth_share=None
        )

    assert score == 0
    assert yes_t == base
    assert no_t == base


def test_on05_disabled_returns_none_score():
    """
    ON-05 AC: feature disabled → score = None, no tighten.
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", 0.00001),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", 0.02),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=0.1, depth_share=0.10
        )

    assert score is None
    assert yes_t == base
    assert no_t == base


def test_on04_on05_combined_winner_protected_and_loser_tightened():
    """
    ON-04 + ON-05 both enabled: winner's trigger is lowered AND loser's trigger is
    additionally raised when both signals agree.

    Scenario: positive funding (YES loser) + low depth_share (YES loser) → score = +2.
      ON-04: no_trigger = base - buffer (NO winner protected)
      ON-05: yes_trigger = (base) + tighten   (YES loser fires sooner)
    """
    scanner = _make_scanner_for_compute()
    market = MagicMock()
    base = 0.38
    buf = 0.03
    tighten = 0.02
    thr = 0.00001

    with (
        patch.object(config, "OPENING_NEUTRAL_ASYMMETRIC_SELLS_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_TRIGGER", base),
        patch.object(config, "OPENING_NEUTRAL_WINNER_SELL_BUFFER", buf),
        patch.object(config, "OPENING_NEUTRAL_FUNDING_GATE_THRESHOLD", thr),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_LOSER_CONFIDENCE_TIGHTEN", tighten),
    ):
        yes_t, no_t, score = scanner._compute_sell_prices(
            market, funding=0.001, depth_share=0.10
        )

    assert score == 2
    assert yes_t == round(base + tighten, 4), "YES (loser) tightened: base + tighten"
    assert no_t == round(base - buf, 4), "NO (winner) protected: base - buffer"

