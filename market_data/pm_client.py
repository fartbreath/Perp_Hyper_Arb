"""
market_data.pm_client — canonical import path for the Polymarket CLOB/Gamma client.

Strategies and core modules should import from here, not from the root pm_client.
The implementation lives in the root pm_client.py (unchanged) to avoid large-file
duplication during the refactor.

Public API: PMClient, PMMarket, OrderBookSnapshot.
Private symbols (prefixed _) are re-exported for tests that need them; they are
intentionally excluded from __all__ to signal they are implementation details.
"""
from pm_client import (  # noqa: F401
    PMClient,
    PMMarket,
    OrderBookSnapshot,
    _classify_market,
    _detect_underlying,
    _MARKET_TYPE_KEYWORDS,
    _MARKET_TYPE_DURATION_SECS,
    _RECURRENCE_TO_MARKET_TYPE,
    _UNDERLYING_PATTERNS,
    _UNDERLYING_TAG_SLUGS,
    _ALL_TAG_SLUGS,
    _REBATE_PCT_BY_TYPE,
)

# Only the public surface is listed here.
# Private symbols above are importable but not part of the documented API.
__all__ = [
    "PMClient",
    "PMMarket",
    "OrderBookSnapshot",
]
