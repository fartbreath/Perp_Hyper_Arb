"""
strategies.mispricing.signals — Signal dataclass for the mispricing scanner.

Kept separate from strategy.py so agent.py and api_server.py can import this
lightweight type without pulling in the full scanner (DeribitFetcher, aiohttp, etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MispricingSignal:
    market_id: str
    market_title: str
    underlying: str
    pm_price: float                       # current PM binary price (Yes token)
    implied_prob: float                   # Deribit-derived probability
    deviation: float                      # |pm_price - implied_prob|
    direction: str                        # "BUY_YES" | "BUY_NO"
    fee_hurdle: float                     # min edge required at this probability
    deribit_iv: float                     # IV used in calculation
    deribit_instrument: str               # e.g. "BTC-30MAY26-100000-C"
    spot_price: float
    strike: float
    tte_years: float
    fees_enabled: bool
    suggested_size_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)

    # Kalshi layer (None when KALSHI_ENABLED=False or no matching market found)
    kalshi_price: Optional[float] = None
    kalshi_ticker: Optional[str] = None
    kalshi_deviation: Optional[float] = None
    signal_source: str = "nd2_only"  # "kalshi_confirmed" | "kalshi_only" | "nd2_only"

    # Signal quality score 0–100 (computed by strategies.scoring.score_mispricing)
    score: float = 0.0
    # 24h PM volume for this market — carried here so CSV/logs record it at signal time
    volume_24hr: float = 0.0

    @property
    def is_actionable(self) -> bool:
        return self.deviation > self.fee_hurdle

    def summary(self) -> str:
        return (
            f"[SIGNAL] {self.market_title}\n"
            f"  PM price: {self.pm_price:.3f}  |  Implied prob: {self.implied_prob:.3f}\n"
            f"  Deviation: {self.deviation:.3f}  |  Fee hurdle: {self.fee_hurdle:.4f}\n"
            f"  Direction: {self.direction}  |  Fees enabled: {self.fees_enabled}\n"
            f"  Deribit IV: {self.deribit_iv:.1%}  |  Instrument: {self.deribit_instrument}\n"
            f"  Suggested size: ${self.suggested_size_usd:.0f}"
        )
