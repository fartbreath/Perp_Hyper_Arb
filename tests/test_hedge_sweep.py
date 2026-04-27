"""
Fast parametric sweep — 10 entry prices × 20 buy notionals × hedge price ladder.
Pure math, no I/O, no mocks.  Runs in under 1 second.

Invariants checked at every non-blocked hedge price:
  I1  PM $1 minimum      — notional >= $1.00  (when budget >= $1)
  I2  No over-hedge       — notional <= budget  (pnl - MIN_RETAIN)
  I3  Profitability       — retained = pnl - notional >= MIN_RETAIN
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import config

MIN_RETAIN = config.MOMENTUM_HEDGE_MIN_RETAIN_USD   # live value
HEDGE_PCT  = config.MOMENTUM_HEDGE_CONTRACTS_PCT     # 1.0
TICK       = 0.01

# 10 entry prices: 0.65, 0.67, 0.69 … 0.83
ENTRY_PRICES  = [round(0.65 + i * 0.02, 2) for i in range(10)]
# 20 buy notionals: $1 … $20
BUY_NOTIONALS = list(range(1, 21))
# Hedge price sweep: 1¢ to 94¢ in 1¢ steps
HEDGE_PRICES  = [round(p / 100, 2) for p in range(1, 95)]


def _hedge_notional(natural: float, hp: float, pnl: float):
    """
    Apply scanner + monitor hedge sizing math for a single hedge price.
    Returns (notional, branch) or (None, 'blocked').
    """
    budget    = pnl - MIN_RETAIN
    price_cap = round(min(budget / natural, 1.0 - TICK), 4)

    if hp > price_cap:
        return None, "blocked"

    if natural * hp >= 1.0:
        notional = natural * hp          # natural branch
        branch   = "natural"
    else:
        notional = 1.0                   # floor branch
        branch   = "floor"

    notional = min(notional, budget)     # PnL ceiling backstop
    return notional, branch


# ── 200 parametrised tests (10 prices × 20 notionals) ─────────────────────────

@pytest.mark.parametrize("entry_price", ENTRY_PRICES)
@pytest.mark.parametrize("buy_notional", BUY_NOTIONALS)
def test_hedge_math(buy_notional: int, entry_price: float):
    contracts = buy_notional / entry_price
    pnl       = round(contracts * (1.0 - entry_price), 6)

    if pnl <= MIN_RETAIN:
        pytest.skip(
            f"entry={entry_price} buy=${buy_notional}: "
            f"pnl=${pnl:.3f} <= MIN_RETAIN=${MIN_RETAIN} — hedge not viable"
        )

    natural = contracts * HEDGE_PCT
    budget  = pnl - MIN_RETAIN

    for hp in HEDGE_PRICES:
        notional, branch = _hedge_notional(natural, hp, pnl)

        if notional is None:
            continue  # correctly blocked by price_cap

        retained = pnl - notional

        # I1 — PM $1 minimum (only enforceable when budget itself >= $1)
        if budget >= 1.0:
            assert notional >= 1.0 - 1e-9, (
                f"[entry={entry_price} buy=${buy_notional} hp={hp:.2f} {branch}] "
                f"notional ${notional:.4f} < $1.00  (VIOLATES $1 MIN)"
            )

        # I2 — never exceeds budget (no over-hedge)
        assert notional <= budget + 1e-9, (
            f"[entry={entry_price} buy=${buy_notional} hp={hp:.2f} {branch}] "
            f"notional ${notional:.4f} > budget ${budget:.4f}  (OVER-HEDGE)"
        )

        # I3 — always profitable (MIN_RETAIN preserved)
        assert retained >= MIN_RETAIN - 1e-9, (
            f"[entry={entry_price} buy=${buy_notional} hp={hp:.2f} {branch}] "
            f"retained ${retained:.4f} < MIN_RETAIN ${MIN_RETAIN}  (UNPROFITABLE)"
        )
