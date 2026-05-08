"""
tests/test_reverse_opening_neutral.py â€” Unit tests for ReverseOpenNeutralScanner.

Run: pytest tests/test_reverse_opening_neutral.py -v

Coverage:
  - _on_on_entry_received: blocked when REVERSE_OPENING_NEUTRAL_ENABLED=False
  - _on_on_entry_received: creates paper pair with correct entry prices
  - _on_on_entry_received: arms bid-monitoring on both YES and NO tokens
  - _on_on_entry_received: stores on_pair_id in _pair_csv_data for CSV join
  - _execute_loser_exit YES loser: winner_sold_price = NO best_bid (simulated)
  - _execute_loser_exit NO loser:  winner_sold_price = YES best_bid (simulated)
  - _execute_loser_exit: NO real orders placed (place_market not called)
  - _execute_loser_exit: NO risk engine writes (risk.close_position not called)
  - _execute_loser_exit: pair removed from _active_pairs after exit
  - _execute_loser_exit: ron_fills.csv row written with correct fields + on_pair_id
  - _execute_loser_exit: winner price falls back to winner_bid
  - _execute_loser_exit: fallback to (1 - trigger_bid) when no book available
  - _execute_loser_exit: missing pair_id â†’ no-op, loser token unblocked
  - _execute_loser_exit: missing winner token_id â†’ no-op, loser token unblocked
  - _execute_loser_exit: on_close_callback fired with market_id
  - _execute_loser_exit: double-down simulation when RON_DOUBLE_DOWN_USD > 0
  - notify_winner_closed: no-op (does not raise, does not modify state)
"""

from __future__ import annotations

import asyncio
import csv
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import config
from risk import Position
from strategies.ReverseOpenNeutral.scanner import (
    ReverseOpenNeutralScanner,
    _RON_FILLS_CSV,
    _RON_FILLS_HEADER,
)


# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_market(
    condition_id: str = "cond_ron_001",
    title: str = "Will BTC go up or down at 2PM?",
    market_type: str = "bucket_1h",
    tte_seconds: float = 2000.0,
    underlying: str = "BTC",
):
    m = MagicMock()
    m.condition_id = condition_id
    m.title = title
    m.market_type = market_type
    m.end_date = datetime.now(timezone.utc) + timedelta(seconds=tte_seconds)
    m.token_id_yes = f"tok_yes_{condition_id[:8]}"
    m.token_id_no  = f"tok_no_{condition_id[:8]}"
    m.market_slug  = "will-btc-go-up-or-down"
    m.underlying   = underlying
    m.event_start_time = ""
    return m


def _make_position(
    market_id: str,
    side: str,
    entry_price: float,
    pair_id: str,
    size_usd: float = 5.0,
    underlying: str = "BTC",
    market_type: str = "bucket_1h",
) -> Position:
    size = round(size_usd / entry_price, 6)
    return Position(
        market_id=market_id,
        market_type=market_type,
        underlying=underlying,
        side=side,
        entry_price=entry_price,
        size=size,
        entry_cost_usd=size_usd,
        strategy="opening_neutral",
        neutral_pair_id=pair_id,
        token_id=f"tok_{side.lower()}_{market_id[:8]}",
        market_title="Will BTC go up or down?",
    )


def _make_scanner(
    winner_bid: Optional[float] = None,
    loser_ask: Optional[float] = None,
    markets: Optional[list] = None,
) -> ReverseOpenNeutralScanner:
    """Create a ReverseOpenNeutralScanner with standard mocks."""
    pm = MagicMock()
    _markets_list = markets or []
    pm.get_markets.return_value = {m.condition_id: m for m in _markets_list}

    def _get_book(token_id):
        book = MagicMock()
        book.best_bid = winner_bid
        book.best_ask = loser_ask
        book.asks = [(loser_ask, 100.0)] if loser_ask else []
        book.bids = [(winner_bid, 100.0)] if winner_bid else []
        return book

    pm.get_book.side_effect = _get_book
    pm._paper_mode = True
    pm.place_limit  = AsyncMock(return_value="ord_001")
    pm.place_market = AsyncMock(return_value="ord_winner_sell")
    pm.cancel_order = AsyncMock(return_value=None)
    pm.on_price_change = MagicMock()
    pm.register_fill_future = MagicMock()
    pm.get_depth_share = MagicMock(return_value=None)
    pm.fetch_price_to_beat = AsyncMock(return_value=None)
    pm.fetch_crypto_price_ptb = AsyncMock(return_value=None)
    pm.get_token_balance = AsyncMock(return_value=None)

    risk = MagicMock()
    risk.get_open_positions.return_value = []
    risk.open_position   = MagicMock()
    risk.close_position  = MagicMock()
    risk.hard_stop_triggered = False

    spot = MagicMock()
    spot.get_price = MagicMock(return_value=70000.0)
    spot.get_mid   = MagicMock(return_value=70000.0)

    vol = MagicMock()
    vol.get_sigma_ann = AsyncMock(return_value=0.8)

    with patch("strategies.ReverseOpenNeutral.scanner._ensure_ron_fills_csv"):
        scanner = ReverseOpenNeutralScanner(
            pm=pm, risk=risk, spot_client=spot, vol_fetcher=vol
        )
    scanner._running = True
    return scanner


def _prime_pair(
    scanner: ReverseOpenNeutralScanner,
    pair_id: str,
    market_id: str,
    yes_pos: Position,
    no_pos: Position,
    on_pair_id: str = "on_pair_ref_001",
) -> None:
    """Populate _active_pairs and _pair_csv_data as _on_on_entry_received would."""
    scanner._active_pairs[pair_id] = {
        "market_id":         market_id,
        "market_title":      "Test",
        "yes_pos":           yes_pos,
        "no_pos":            no_pos,
        "yes_exit_order_id": "",
        "no_exit_order_id":  "",
        "entry_ts":          time.time() - 5.0,
        "yes_trigger":       0.38,
        "no_trigger":        0.38,
    }
    scanner._pair_csv_data[pair_id] = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "pair_id":      pair_id,
        "on_pair_id":   on_pair_id,
        "market_id":    market_id,
        "market_title": "Test",
        "underlying":   "BTC",
        "market_type":  "bucket_1h",
        "yes_entry":    0.50,
        "no_entry":     0.50,
        "combined_cost": 1.00,
        "_entry_ts":    time.time() - 5.0,
    }
    scanner._token_to_pair[yes_pos.token_id] = pair_id
    scanner._token_to_pair[no_pos.token_id]  = pair_id


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 1.  _on_on_entry_received â€” callback from ON scanner
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_on_on_entry_received_blocked_when_disabled():
    """When REVERSE_OPENING_NEUTRAL_ENABLED=False, callback must be a no-op."""
    scanner  = _make_scanner()
    market   = _make_market()
    yes_pos  = _make_position(market.condition_id, "YES", 0.50, "on_pair_001")
    no_pos   = _make_position(market.condition_id, "NO",  0.50, "on_pair_001")

    with patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", False):
        _run(scanner._on_on_entry_received(market, "on_pair_001", yes_pos, no_pos))

    assert len(scanner._active_pairs) == 0, "No pair must be created when disabled"
    assert len(scanner._token_to_pair) == 0


def test_on_on_entry_received_creates_paper_pair():
    """Callback must create a paper pair at the same entry prices as ON."""
    scanner = _make_scanner()
    market  = _make_market(condition_id="cond_abc", underlying="ETH")
    yes_pos = _make_position("cond_abc", "YES", 0.51, "on_pair_001", underlying="ETH")
    no_pos  = _make_position("cond_abc", "NO",  0.49, "on_pair_001", underlying="ETH")

    with patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True):
        _run(scanner._on_on_entry_received(market, "on_pair_001", yes_pos, no_pos))

    assert len(scanner._active_pairs) == 1
    pair_id, pair = next(iter(scanner._active_pairs.items()))
    assert pair["market_id"] == "cond_abc"
    assert pair["yes_pos"].entry_price == pytest.approx(0.51)
    assert pair["no_pos"].entry_price  == pytest.approx(0.49)
    assert pair["yes_pos"].strategy == "reverse_opening_neutral"
    assert pair["no_pos"].strategy  == "reverse_opening_neutral"


def test_on_on_entry_received_arms_bid_monitoring():
    """Both token_ids must be registered in _token_to_pair."""
    scanner = _make_scanner()
    market  = _make_market(condition_id="cond_bid")
    yes_pos = _make_position("cond_bid", "YES", 0.50, "on_pair_002")
    no_pos  = _make_position("cond_bid", "NO",  0.50, "on_pair_002")

    with patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True):
        _run(scanner._on_on_entry_received(market, "on_pair_002", yes_pos, no_pos))

    assert yes_pos.token_id in scanner._token_to_pair, "YES token not in _token_to_pair"
    assert no_pos.token_id  in scanner._token_to_pair, "NO token not in _token_to_pair"


def test_on_on_entry_received_stores_on_pair_id():
    """on_pair_id must be stored in _pair_csv_data for CSV join with on_fills.csv."""
    scanner = _make_scanner()
    market  = _make_market(condition_id="cond_join")
    yes_pos = _make_position("cond_join", "YES", 0.50, "on_pair_xyz")
    no_pos  = _make_position("cond_join", "NO",  0.50, "on_pair_xyz")

    with patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True):
        _run(scanner._on_on_entry_received(market, "on_pair_xyz", yes_pos, no_pos))

    pair_id = next(iter(scanner._active_pairs))
    assert scanner._pair_csv_data[pair_id]["on_pair_id"] == "on_pair_xyz"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 2.  _execute_loser_exit â€” paper simulation
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_execute_loser_exit_yes_loser_no_real_order():
    """
    YES bid drops to trigger:
      - place_market must NOT be called (paper mode â€” no real orders).
      - risk.close_position must NOT be called.
      - CSV row must be written with loser_leg=YES, winner_side=NO.
    """
    scanner  = _make_scanner(winner_bid=0.65)
    pair_id  = "pair_ye_loser"
    market_id = "cond_ye_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
        ))

    scanner._pm.place_market.assert_not_called()
    scanner._risk.close_position.assert_not_called()
    assert len(written_rows) == 1
    assert written_rows[0]["loser_leg"]   == "YES"
    assert written_rows[0]["winner_side"] == "NO"


def test_execute_loser_exit_no_loser_no_real_order():
    """
    NO bid drops to trigger:
      - place_market must NOT be called.
      - risk.close_position must NOT be called.
      - CSV row must be written with loser_leg=NO, winner_side=YES.
    """
    scanner  = _make_scanner(winner_bid=0.65)
    pair_id  = "pair_no_loser"
    market_id = "cond_no_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(no_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "NO", no_pos.token_id, no_pos, trigger_bid=0.36
        ))

    scanner._pm.place_market.assert_not_called()
    scanner._risk.close_position.assert_not_called()
    assert len(written_rows) == 1
    assert written_rows[0]["loser_leg"]   == "NO"
    assert written_rows[0]["winner_side"] == "YES"


def test_execute_loser_exit_pair_removed_from_active_pairs():
    """After exit, pair_id must be removed from _active_pairs and _token_to_pair."""
    scanner  = _make_scanner(winner_bid=0.66)
    pair_id  = "pair_cleanup"
    market_id = "cond_cl_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row"),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.35
        ))

    assert pair_id not in scanner._active_pairs
    assert yes_pos.token_id not in scanner._token_to_pair
    assert no_pos.token_id  not in scanner._token_to_pair


def test_execute_loser_exit_writes_ron_fills_row():
    """CSV row must include all expected fields with correct values."""
    scanner  = _make_scanner(winner_bid=0.66)
    pair_id  = "pair_csv_row"
    market_id = "cond_csv_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos, on_pair_id="on_ref_abc")
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
        ))

    assert len(written_rows) == 1
    row = written_rows[0]
    assert row["loser_leg"]          == "YES"
    assert row["loser_trigger_bid"]  == pytest.approx(0.36)
    assert row["winner_side"]        == "NO"
    assert row["winner_sold_price"]  == pytest.approx(0.66, abs=0.01)
    assert row["winner_sold_time_secs"] >= 0
    assert row["on_pair_id"]         == "on_ref_abc"


def test_execute_loser_exit_price_fallback_to_winner_bid():
    """winner_sold_price in CSV must equal the winner's best_bid."""
    scanner  = _make_scanner(winner_bid=0.67)
    pair_id  = "pair_bid_fb"
    market_id = "cond_bf_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.35
        ))

    assert len(written_rows) == 1
    assert written_rows[0]["winner_sold_price"] == pytest.approx(0.67, abs=0.01)


def test_execute_loser_exit_price_fallback_to_inversion_when_no_book():
    """When get_book returns None, winner_sold_price must fall back to (1 - trigger_bid)."""
    scanner = _make_scanner()
    scanner._pm.get_book = MagicMock(return_value=None)

    pair_id   = "pair_inv_fb"
    market_id = "cond_if_001"
    yes_pos   = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos    = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.35
        ))

    assert len(written_rows) == 1
    expected_fallback = round(1.0 - 0.35, 4)
    assert written_rows[0]["winner_sold_price"] == pytest.approx(expected_fallback, abs=0.01)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 3.  _execute_loser_exit â€” failure / guard cases
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_execute_loser_exit_missing_pair_id_no_op():
    """
    If pair_id is no longer in _active_pairs, call must not raise and must
    unblock the loser token.
    """
    scanner   = _make_scanner()
    pair_id   = "stale_pair"
    market_id = "cond_stale"
    yes_pos   = _make_position(market_id, "YES", 0.50, pair_id)

    scanner._exiting_legs.add(yes_pos.token_id)

    _run(scanner._execute_loser_exit(
        pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
    ))

    assert yes_pos.token_id not in scanner._exiting_legs
    scanner._pm.place_market.assert_not_called()
    scanner._risk.close_position.assert_not_called()


def test_execute_loser_exit_missing_winner_token_id_no_op():
    """If winner Position has no token_id, call must abort and unblock the loser."""
    scanner   = _make_scanner(winner_bid=0.66)
    pair_id   = "pair_no_tok"
    market_id = "cond_nt_001"
    yes_pos   = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos    = _make_position(market_id, "NO",  0.50, pair_id)
    no_pos.token_id = ""  # strip winner token_id

    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    _run(scanner._execute_loser_exit(
        pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
    ))

    assert yes_pos.token_id not in scanner._exiting_legs
    scanner._pm.place_market.assert_not_called()
    scanner._risk.close_position.assert_not_called()


def test_execute_loser_exit_fires_on_close_callback():
    """on_close_callback must be called with market_id after the paper exit."""
    scanner  = _make_scanner(winner_bid=0.65)
    callback_calls: list[str] = []
    scanner._on_close_callback = lambda mid: callback_calls.append(mid)

    pair_id   = "pair_cb"
    market_id = "cond_cb_001"
    yes_pos   = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos    = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    with patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row"):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
        ))

    assert callback_calls == [market_id]


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 4.  Double-down simulation
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_execute_loser_exit_double_down_written_to_csv():
    """
    When RON_DOUBLE_DOWN_USD > 0, double_down_size and double_down_price must
    be recorded in the CSV row.  No real order must be placed.
    """
    scanner  = _make_scanner(winner_bid=0.66, loser_ask=0.32)
    pair_id  = "pair_dd"
    market_id = "cond_dd_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "RON_DOUBLE_DOWN_USD",             5.0),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
        ))

    scanner._pm.place_market.assert_not_called()
    assert len(written_rows) == 1
    row = written_rows[0]
    # double_down_price = loser best_ask = 0.32
    assert row["double_down_price"] == pytest.approx(0.32, abs=0.01)
    # double_down_size = 5.0 / 0.32 â‰ˆ 15.625
    assert row["double_down_size"]  == pytest.approx(5.0 / 0.32, abs=0.1)


def test_execute_loser_exit_no_double_down_when_disabled():
    """When RON_DOUBLE_DOWN_USD=0, double_down fields must be 0 in the CSV row."""
    scanner  = _make_scanner(winner_bid=0.66)
    pair_id  = "pair_no_dd"
    market_id = "cond_ndd_001"
    yes_pos  = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos   = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)
    scanner._exiting_legs.add(yes_pos.token_id)

    written_rows: list[dict] = []

    with (
        patch.object(config, "REVERSE_OPENING_NEUTRAL_ENABLED", True),
        patch.object(config, "RON_DOUBLE_DOWN_USD",             0.0),
        patch("strategies.ReverseOpenNeutral.scanner._write_ron_fills_row",
              side_effect=lambda r: written_rows.append(dict(r))),
    ):
        _run(scanner._execute_loser_exit(
            pair_id, "YES", yes_pos.token_id, yes_pos, trigger_bid=0.36
        ))

    assert written_rows[0]["double_down_size"]  == 0.0
    assert written_rows[0]["double_down_price"] == 0.0


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 5.  notify_winner_closed â€” no-op
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_notify_winner_closed_is_noop():
    """
    notify_winner_closed must not raise and must not modify scanner state.
    """
    scanner   = _make_scanner()
    pair_id   = "pair_noop"
    market_id = "cond_noop_001"
    yes_pos   = _make_position(market_id, "YES", 0.50, pair_id)
    no_pos    = _make_position(market_id, "NO",  0.50, pair_id)
    _prime_pair(scanner, pair_id, market_id, yes_pos, no_pos)

    before_pairs = dict(scanner._active_pairs)

    scanner.notify_winner_closed(market_id, "NO", 0.70)

    assert scanner._active_pairs == before_pairs
    scanner._risk.close_position.assert_not_called()

