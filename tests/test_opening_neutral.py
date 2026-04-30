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
        else:
            book.best_ask = no_ask
            book.best_bid = round((no_ask or 0.0) - 0.01, 4)
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
    market = _make_market(tte_seconds=3500.0, duration_seconds=3600.0)
    # combined = 0.55 + 0.50 = 1.05 > 1.01
    scanner = _make_scanner(yes_ask=0.55, no_ask=0.50, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.01),
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 300),
        patch.object(config, "OPENING_NEUTRAL_MAX_CONCURRENT", 3),
    ):
        _run(scanner._evaluate_entry(market))

    assert len(scanner._signals) == 1
    assert scanner._signals[0]["result"] == "too_expensive"
    assert scanner._entering_markets == set()


# ── test: entry_attempt ───────────────────────────────────────────────────────

def test_scan_once_enters_qualifying(monkeypatch):
    """When combined <= COMBINED_COST_MAX, result should be 'entry_attempt' and market queued."""
    market = _make_market(tte_seconds=3500.0, duration_seconds=3600.0)
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
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 300),
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
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 300),
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
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 120),
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
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 300),
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
    market = _make_market(tte_seconds=3500.0, duration_seconds=3600.0)
    # YES=0.12, NO=0.89 → combined=1.01 (would pass old filter), but YES < 0.40 → skewed
    scanner = _make_scanner(yes_ask=0.12, no_ask=0.89, markets=[market])

    with (
        patch.object(config, "OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "OPENING_NEUTRAL_COMBINED_COST_MAX", 1.02),
        patch.object(config, "OPENING_NEUTRAL_ENTRY_WINDOW_SECS", 300),
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
    After both BUY legs fill, _register_pair must place GTC resting SELL orders on
    both YES and NO tokens at LOSER_EXIT_PRICE immediately, and spawn the
    _monitor_exit_fills background task.  No price monitoring or deferred trigger.
    Per PLAN.md §2: resting SELLs go into the book the moment entry fills.
    """
    scanner = _make_scanner()
    scanner._pm._paper_mode = False
    scanner._pm.place_limit = AsyncMock(side_effect=["yes_exit_oid", "no_exit_oid"])
    market = _make_market("cond_reg_exit_001")

    yes_result = {"filled": True, "price": 0.50, "size": 2.0, "order_id": "yes_buy_oid"}
    no_result  = {"filled": True, "price": 0.50, "size": 2.0, "order_id": "no_buy_oid"}

    with (
        patch.object(config, "OPENING_NEUTRAL_DRY_RUN", False),
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", 0.35),
        patch.object(config, "OPENING_NEUTRAL_EXIT_ORDER_TIMEOUT_SECS", 300),
    ):
        _run(scanner._register_pair(
            "pair_reg_exit", market, yes_result, no_result,
            market.token_id_yes, market.token_id_no,
        ))
        _run(asyncio.sleep(0.05))  # allow background task to start

    # Two place_limit calls: SELL YES and SELL NO at $0.35
    assert scanner._pm.place_limit.call_count == 2, (
        f"Expected 2 place_limit calls (exit SELLs), got {scanner._pm.place_limit.call_count}"
    )
    calls = scanner._pm.place_limit.call_args_list
    sides = {c.kwargs.get("side") for c in calls}
    assert sides == {"SELL"}, f"Both calls must be SELL, got: {sides}"
    prices = {c.kwargs.get("price") for c in calls}
    assert prices == {0.35}, f"Both SELLs must be at 0.35, got: {prices}"

    # Order IDs stored in pair dict
    pair = scanner._active_pairs.get("pair_reg_exit")
    assert pair is not None
    assert pair["yes_exit_order_id"] == "yes_exit_oid"
    assert pair["no_exit_order_id"] == "no_exit_oid"


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

            # Only one trade row should have been written (first call only)
            import csv as csv_module
            with temp_csv.open(newline="") as f:
                rows = list(csv_module.DictReader(f))
            assert len(rows) == 1, f"Expected 1 trade row (idempotent), got {len(rows)}"
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

    # Intercept _append_csv to capture rows synchronously.
    captured_rows: list[dict] = []

    def sync_capture(row: dict) -> None:
        captured_rows.append(dict(row))
        real_risk._write_csv_row(row)

    real_risk._append_csv = sync_capture  # type: ignore[method-assign]

    # Use a fill price slightly below the target to verify actual price is recorded.
    actual_fill_price = 0.33

    with (
        patch.object(config, "OPENING_NEUTRAL_LOSER_EXIT_PRICE", exit_price),
        patch.object(config, "MOMENTUM_PROB_SL_ENABLED", True),
        patch.object(config, "MOMENTUM_PROB_SL_PCT", 0.15),
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

    # ── 4. Trade CSV row semantic checks ─────────────────────────────────────
    assert len(captured_rows) == 1, (
        f"Exactly 1 trade row expected; got {len(captured_rows)}"
    )
    row = captured_rows[0]
    assert row["strategy"] == "opening_neutral", (
        f"CSV strategy must be 'opening_neutral' (loser strategy at close time), "
        f"got '{row['strategy']}'. Check strategy not mutated before close_position."
    )
    assert row["side"] == "NO"
    assert row["market_id"] == "cond_e2e_rest_001"
    csv_pnl = float(row["pnl"])
    assert csv_pnl < 0
    assert abs(csv_pnl - expected_pnl) < 1e-9, (
        f"CSV pnl {csv_pnl:.6f} != expected {expected_pnl:.6f}"
    )

    # ── 5. CSV file on disk ───────────────────────────────────────────────────
    import csv as csv_module
    with temp_csv.open(newline="") as f:
        rows_on_disk = list(csv_module.DictReader(f))
    assert len(rows_on_disk) == 1
    assert rows_on_disk[0]["strategy"] == "opening_neutral"
    assert rows_on_disk[0]["side"] == "NO"
    assert float(rows_on_disk[0]["pnl"]) < 0


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
