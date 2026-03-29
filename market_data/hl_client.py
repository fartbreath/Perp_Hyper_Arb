"""
market_data.hl_client — canonical import path for the Hyperliquid client.
"""
from hl_client import (  # noqa: F401
    HLClient,
    BBO,
    FundingSnapshot,
)

__all__ = ["HLClient", "BBO", "FundingSnapshot"]
