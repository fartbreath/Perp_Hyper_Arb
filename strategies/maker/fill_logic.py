"""
strategies/maker/fill_logic.py — shared fill-processing logic.

Used by both FillSimulator (paper) and LiveFillHandler (live) so that the
risk-check → position-open → inventory-update → rebate sequence is maintained
identically for both execution paths.

Callers are responsible for:
  - logging (their own prefix / log-level policy)
  - writing to fills.csv
  - scheduling any HL hedge rebalance (applies in paper mode only currently)
  - triggering an immediate quote reprice after a full fill
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from risk import RiskEngine, Position


@dataclass
class FillResult:
    """Return value of open_position_from_fill on success."""
    pos: Position
    consumed: object        # ActiveQuote returned by MakerStrategy.consume_fill
    fill_price: float
    actual_filled: float
    position_side: str      # "YES" or "NO"
    fill_cost_usd: float
    rebate_usd: float = 0.0  # estimated USDC rebate earned on this fill


def open_position_from_fill(
    *,
    maker,
    risk: RiskEngine,
    monitor,
    key: str,
    fill_price: float,
    filled_size: float,
    market,
) -> Optional[FillResult]:
    """
    Consume *filled_size* contracts from the resting quote at *key*, run the
    risk gate, and open a Position.

    Returns a FillResult on success, or None if:
    - the fill was already consumed by a concurrent call (silent); or
    - the risk engine blocked the fill (logged at WARNING by this function).

    Caller must handle:
    - logging the successful fill (log prefix, CSV write, HL tracking etc.)
    - calling maker.schedule_hedge_rebalance(market.underlying) if desired
    - calling maker.trigger_post_fill_reprice(key, market) after this returns
    """
    from logger import get_bot_logger
    log = get_bot_logger(__name__)

    consumed = maker.consume_fill(key, filled_size)
    if consumed is None:
        return None   # race condition — already consumed

    actual_filled = consumed.size
    # Determine position side from the quote key, not the order direction:
    #   bid key (no "_ask" suffix): BUY YES  → YES position
    #   ask key ("_ask" suffix):    BUY NO   → NO position
    position_side = "YES" if not key.endswith("_ask") else "NO"
    # fill_price is always the traded token's price (YES price for bid, NO price
    # for ask). Both represent actual USDC cost per contract.
    fill_cost_usd = round(fill_price * actual_filled, 4)

    ok, reason = risk.can_open(
        consumed.market_id, fill_cost_usd, strategy="maker", underlying=market.underlying
    )
    if not ok:
        log.warning(
            "Fill blocked by risk engine",
            market_id=consumed.market_id,
            reason=reason,
            fill_cost_usd=fill_cost_usd,
            contracts=round(actual_filled, 4),
        )
        return None

    # entry_price is the actual fill price of the held token for both sides.
    #   BUY YES: entry_price = fill_price  (actual YES token price)
    #   BUY NO:  entry_price = fill_price  (actual NO token price)
    entry_price_stored = fill_price

    pos = Position(
        market_id=consumed.market_id,
        market_title=market.title,
        market_type=market.market_type,
        underlying=market.underlying,
        side=position_side,
        size=actual_filled,
        entry_price=entry_price_stored,
        strategy="maker",
        opened_at=datetime.now(timezone.utc),
        entry_cost_usd=round(fill_cost_usd, 4),
        order_id=consumed.order_id or "",
        signal_score=round(consumed.score, 2),
        token_id=market.token_id_yes if position_side == "YES" else market.token_id_no,
    )
    risk.open_position(pos)
    risk.free_slot(consumed.market_id)
    monitor.record_entry_deviation(consumed.market_id, market.max_incentive_spread / 2)

    # BUY YES increases net YES exposure; BUY NO decreases it (short YES equivalent)
    inventory_side = "YES_BUY" if position_side == "YES" else "YES_SELL"
    maker.record_fill(
        consumed.market_id,
        market.underlying,
        inventory_side,
        round(fill_cost_usd, 4),
    )

    rebate_usd = 0.0
    if market.fees_enabled and market.rebate_pct > 0.0:
        token_price = fill_price  # actual token price for both YES and NO
        rebate_usd = round(
            actual_filled * config.PM_FEE_COEFF
            * token_price * (1.0 - token_price)
            * market.rebate_pct * config.PAPER_REBATE_CAPTURE_RATE,
            6,
        )
        risk.record_rebate(consumed.market_id, rebate_usd, side=position_side)

    return FillResult(
        pos=pos,
        consumed=consumed,
        fill_price=entry_price_stored,   # actual token fill price
        actual_filled=actual_filled,
        position_side=position_side,
        fill_cost_usd=fill_cost_usd,
        rebate_usd=rebate_usd,
    )
