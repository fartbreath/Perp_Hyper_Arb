"""
market_data.kalshi_client — canonical import path for the Kalshi read-only client.
"""
from kalshi_client import (  # noqa: F401
    KalshiClient,
    KalshiMarket,
    KALSHI_API,
)

__all__ = ["KalshiClient", "KalshiMarket", "KALSHI_API"]
