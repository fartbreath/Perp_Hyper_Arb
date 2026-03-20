"""
strategies.maker.math — Pure math helpers for the maker strategy.

No imports from market_data or other strategies — only stdlib and typing.
This module can be unit-tested in isolation without any bot dependencies.
"""
from __future__ import annotations

import math
import re
from typing import Optional


# ── Standard normal helpers ───────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_ppf(p: float) -> float:
    """Standard normal inverse CDF (Acklam rational approximation). Valid for 0 < p < 1."""
    p = max(1e-10, min(1.0 - 1e-10, p))
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)


# ── Strike parsing ────────────────────────────────────────────────────────────

def parse_strike_from_title(title: str) -> Optional[float]:
    """
    Extract a directional strike from a market title.

    Handles "above $90,000" / "below $70,000" / "over $X" / "under $X".
    Returns None for "between" range markets or unrecognised formats.
    """
    m = re.search(
        r'\b(?:above|below|over|under)\s+\$?([\d,]+(?:\.\d+)?)',
        title,
        re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return None


# ── Black-Scholes digital option helpers ──────────────────────────────────────

def implied_sigma(
    p: float,
    S: float,
    K: float,
    T: float,
) -> Optional[float]:
    """
    Back-solve Black-Scholes digital-call implied volatility from observed price p = N(d2).

    Uses the closed-form solution to d2 = N^{-1}(p) = (ln(S/K) - σ²T/2) / (σ√T):
        sigma * sqrt(T) = d2 + sqrt(d2² + 2·ln(S/K))

    Returns None if inputs are invalid or the solution is outside a sane range.
    """
    if T <= 0.0 or S <= 0.0 or K <= 0.0 or not (0.005 < p < 0.995):
        return None
    try:
        d2 = _norm_ppf(p)
        log_moneyness = math.log(S / K)
        discriminant = d2 * d2 + 2.0 * log_moneyness
        if discriminant < 0.0:
            return None
        u = -d2 + math.sqrt(discriminant)
        if u <= 0.001:
            return None
        sigma = u / math.sqrt(T)
        return sigma if 0.01 <= sigma <= 50.0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def bs_digital_coins(
    notional_usd: float,
    p: float,
    S: float,
    K: float,
    T: float,
    sigma: float,
) -> float:
    """
    Compute delta-hedge coins using the Black-Scholes digital-call delta.

    The hedge in coins that offsets a $1 spot move for a binary position is:
        coins = notional × n(d2) / (S × σ × √T)

    Falls back to binary_delta(p) × notional / S when inputs are invalid.
    """
    if S <= 0.0 or K <= 0.0 or T <= 0.0 or sigma <= 0.0:
        return max(1e-8, binary_delta(p) * notional_usd / S) if S > 0 else 0.0
    try:
        sqrt_T = math.sqrt(T)
        d2 = (math.log(S / K) - 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
        n_d2 = _norm_pdf(d2)
        coins = notional_usd * n_d2 / (S * sigma * sqrt_T)
        return max(1e-8, coins)
    except (ValueError, ZeroDivisionError, OverflowError):
        return max(1e-8, binary_delta(p) * notional_usd / S)


def binary_delta(pm_price: float) -> float:
    """
    Fallback delta: PM price as a proxy for N(d2).
    Used when strike/expiry/sigma are unavailable.
    """
    return max(0.0, min(1.0, pm_price))


def hedge_size_coins(
    pm_notional_usd: float,
    pm_price: float,
    hl_price: float,
) -> float:
    """Naive hedge size (fallback). Use bs_digital_coins() when S, K, T are known."""
    if hl_price <= 0:
        return 0.0
    return round(binary_delta(pm_price) * pm_notional_usd / hl_price, 6)
