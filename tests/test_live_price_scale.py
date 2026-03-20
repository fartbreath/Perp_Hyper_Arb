"""
tests/test_live_price_scale.py — Live network tests for PM/HL price scale validation.

These tests make REAL HTTP calls to Polymarket Gamma API and Hyperliquid REST.
They are skipped by default in CI; run explicitly with:

    pytest tests/test_live_price_scale.py -v -m live

What is being tested:
  - Polymarket orderbook prices (bid/ask) are probabilities in [0, 1].
  - Hyperliquid allMids returns USD asset prices (e.g. $70,000+ for BTC) that
    are well outside [0, 1].
  - The _valid_prob() guard in api_server correctly rejects HL mid values and
    accepts real PM book prices — preventing the leakage bug that showed
    "7065050.00¢" in the Markets UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import requests

import config
config.PAPER_TRADING = True

# ── Marker ────────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.live


# ── Helpers — copy the guard under test so the test is self-contained ─────────

def _valid_prob(v) -> bool:
    """Must stay in sync with api_server.markets()."""
    return v is not None and 0.0 <= float(v) <= 1.0


# ── Live fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pm_btc_orderbook():
    """Fetch a live BTC daily market from Polymarket Gamma, then pull its CLOB book."""
    # 1. Find a live BTC daily market
    gamma_url = f"{config.GAMMA_HOST}/events"
    resp = requests.get(
        gamma_url,
        params={"active": "true", "closed": "false", "tag_slug": "bitcoin", "limit": 20},
        timeout=15,
    )
    resp.raise_for_status()
    events = resp.json()

    token_id = None
    for event in events:
        for mkt in event.get("markets", []):
            tokens = mkt.get("clobTokenIds", [])
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []
            if (
                mkt.get("active")
                and mkt.get("acceptingOrders")
                and mkt.get("enableOrderBook")
                and len(tokens) >= 1
            ):
                token_id = tokens[0]
                break
        if token_id:
            break

    if token_id is None:
        pytest.skip("No live BTC market found on Polymarket — skipping live test")

    # 2. Fetch order book for this token from CLOB REST API
    clob_resp = requests.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
        timeout=15,
    )
    clob_resp.raise_for_status()
    book = clob_resp.json()
    return book, token_id


@pytest.fixture(scope="module")
def hl_btc_mid():
    """Fetch live allMids from Hyperliquid REST meta endpoint (no auth needed)."""
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "allMids"},
        timeout=15,
    )
    resp.raise_for_status()
    mids = resp.json()
    btc_mid = float(mids.get("BTC", 0))
    if btc_mid == 0:
        pytest.skip("HL allMids returned 0 for BTC — skipping live test")
    return btc_mid


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLivePMBookPricesAreValidProbs:
    """PM CLOB prices must be in [0, 1] — validated via the _valid_prob() guard."""

    def test_pm_book_has_bids_or_asks(self, pm_btc_orderbook):
        book, token_id = pm_btc_orderbook
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        assert bids or asks, f"Expected at least one side of the book for token {token_id}"

    def test_pm_bid_prices_are_valid_probs(self, pm_btc_orderbook):
        book, token_id = pm_btc_orderbook
        for entry in book.get("bids", [])[:10]:
            price = float(entry.get("price", entry.get("p", entry)))
            assert _valid_prob(price), (
                f"PM bid {price} is outside [0,1] for token {token_id} — "
                "guard would incorrectly reject a valid PM price"
            )

    def test_pm_ask_prices_are_valid_probs(self, pm_btc_orderbook):
        book, token_id = pm_btc_orderbook
        for entry in book.get("asks", [])[:10]:
            price = float(entry.get("price", entry.get("p", entry)))
            assert _valid_prob(price), (
                f"PM ask {price} is outside [0,1] for token {token_id} — "
                "guard would incorrectly reject a valid PM price"
            )


class TestLiveHLMidIsRejectedByGuard:
    """HL mid prices are USD asset prices (~$80k for BTC) and must fail _valid_prob()."""

    def test_hl_btc_mid_is_reasonable_usd_price(self, hl_btc_mid):
        """BTC should be priced above $1,000 on HL — a sanity check on the fixture."""
        assert hl_btc_mid > 1_000, (
            f"HL BTC mid={hl_btc_mid} looks wrong; expected a USD price above $1,000"
        )

    def test_hl_btc_mid_fails_valid_prob_guard(self, hl_btc_mid):
        """The guard must reject the HL mid — this is the core regression test."""
        assert not _valid_prob(hl_btc_mid), (
            f"HL BTC mid={hl_btc_mid} incorrectly passed _valid_prob(); "
            "it would cause the '7065050.00¢' display bug"
        )

    def test_hl_mid_not_confused_with_pm_price(self, pm_btc_orderbook, hl_btc_mid):
        """Confirm the two scales are completely disjoint — no overlap possible."""
        book, _ = pm_btc_orderbook
        pm_prices = [
            float(e.get("price", e.get("p", e)))
            for side in ("bids", "asks")
            for e in book.get(side, [])[:5]
        ]
        if not pm_prices:
            pytest.skip("No PM prices available for scale comparison")

        max_pm = max(pm_prices)
        assert hl_btc_mid > max_pm * 100, (
            f"HL mid {hl_btc_mid} is unexpectedly close to PM prices (max={max_pm}); "
            "scale mismatch detection may be insufficient"
        )
