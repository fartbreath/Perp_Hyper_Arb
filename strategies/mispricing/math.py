"""
strategies.mispricing.math — Options math for the mispricing scanner.

No imports from market_data or other strategies — only stdlib.
The _norm_cdf implementation is intentionally independent (not imported from
strategies.maker.math) so the two strategies remain fully decoupled.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def options_implied_probability(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    iv: float,
    risk_free_rate: float = 0.05,
) -> float:
    """
    Compute N(d2) — the risk-neutral probability that spot > strike at expiry.
    This is the options-market implied probability for a digital call.

    Args:
        spot:                 Current price of the underlying
        strike:               Target price (PM market condition)
        time_to_expiry_years: Time until PM resolution in years
        iv:                   Annualised implied volatility
        risk_free_rate:       Risk-free rate (default 5%)

    Returns:
        Probability in [0, 1]
    """
    if spot <= 0 or strike <= 0 or time_to_expiry_years <= 0 or iv <= 0:
        return 0.0

    d1 = (
        math.log(spot / strike)
        + (risk_free_rate + 0.5 * iv ** 2) * time_to_expiry_years
    ) / (iv * math.sqrt(time_to_expiry_years))
    d2 = d1 - iv * math.sqrt(time_to_expiry_years)
    return _norm_cdf(d2)
