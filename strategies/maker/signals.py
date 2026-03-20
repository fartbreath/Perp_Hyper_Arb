"""
strategies.maker.signals — Signal and quote dataclasses for the maker strategy.

Kept separate from strategy.py so other modules (agent, api_server, tests)
can import these lightweight types without pulling in the full MakerStrategy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActiveQuote:
    """A live resting limit order on the PM CLOB."""
    market_id: str
    token_id: str        # which side (YES or NO token)
    side: str            # BUY or SELL
    price: float
    size: float
    order_id: Optional[str] = None
    posted_at: float = field(default_factory=time.time)
    # USDC locked at placement: BUY = price×size; SELL = (1−price)×size
    collateral_usd: float = 0.0
    # Original size at deployment — never changed; used to track filled_so_far.
    # filled_so_far = original_size - size (current remaining)
    original_size: float = 0.0
    # Signal quality score 0–100 stamped at deployment time; survives key lookup gaps.
    score: float = 0.0


@dataclass
class MakerSignal:
    """
    Evaluated market opportunity.

    Capital-free — computed by _evaluate_signal() regardless of whether quotes
    have been deployed.  The is_deployed flag is added by get_signals() when
    building the API snapshot.
    """
    market_id: str
    token_id: str           # token_id_yes (bid_key)
    underlying: str
    mid: float
    bid_price: float
    ask_price: float
    half_spread: float
    effective_edge: float
    market_type: str
    quote_size: float = 50.0  # USD notional to deploy per side; contracts = round(quote_size / price)
    ts: float = field(default_factory=time.time)
    # Signal quality score 0–100 (computed by strategies.scoring.score_maker)
    score: float = 0.0
    # Competing contracts at our quote level (min of bid/ask depth); 0 = sole maker
    depth: float = 0.0
