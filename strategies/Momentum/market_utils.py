"""
Shared market-classification and strike-extraction utilities.

Split into this module to break the circular import between
scanner.py (which defines MomentumScanner) and spread.py (CalendarSpreadMixin),
both of which need these helpers.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Regex patterns ────────────────────────────────────────────────────────────

_STRIKE_PATTERNS = [
    r"\$([0-9,]+(?:\.[0-9]+)?)([kKmM]?)",                      # "$68,300" / "$68k" / "$1.5m"
    r"([0-9,]+(?:\.[0-9]+)?)([kKmM]?)\s*(?:above|below|at)",  # "68000 above"
    r"(?:above|below|at)\s+([0-9,]+(?:\.[0-9]+)?)([kKmM]?)",  # "above 64,200"
]

_UPDOWN_RE = re.compile(r'\bup\s+or\s+down\b', re.IGNORECASE)

_INVERTED_DIRECTION_RE = re.compile(
    r'\b(?:dip|drop|fall|decline|crash)s?\s+(?:to|below|under|beneath)\b'
    r'|\bdip\s+to\b'
    r'|\bfall\s+to\b'
    r'|\bdrop\s+to\b'
    r'|\bless\s+than\b'
    r'|\bunder\s+\$'
    r'|\bbelow\s+\$',
    re.IGNORECASE,
)

_RANGE_MARKET_RE = re.compile(
    r'\bbetween\b.{1,30}\band\b',
    re.IGNORECASE,
)


# ── Public helpers ────────────────────────────────────────────────────────────

def _is_updown_market(title: str) -> bool:
    """Return True if the market is a directional 'Up or Down' window market."""
    return bool(_UPDOWN_RE.search(title))


def _is_inverted_direction_market(title: str) -> bool:
    """Return True if YES resolves on a downward price move (dip/drop/fall/less-than markets)."""
    return bool(_INVERTED_DIRECTION_RE.search(title))


def _is_range_market(title: str) -> bool:
    """Return True if YES resolves on a RANGE condition (between X and Y)."""
    return bool(_RANGE_MARKET_RE.search(title))


_RANGE_BOUNDS_RE = re.compile(
    r'\bbetween\b\s*\$?([\d,]+(?:\.[0-9]+)?)([kKmM]?)'
    r'.{1,20}?\band\b\s*\$?([\d,]+(?:\.[0-9]+)?)([kKmM]?)',
    re.IGNORECASE,
)


def _extract_range_bounds(title: str) -> Optional[tuple[float, float]]:
    """
    Extract (lo, hi) from a range market title such as:
      'Will the price of Bitcoin be between $64,000 and $66,000 on April 5?'
    Returns None if the title is not a recognisable range market.
    """
    m = _RANGE_BOUNDS_RE.search(title.replace(",", ""))
    if not m:
        return None
    def _parse(val_str: str, suffix: str) -> float:
        v = float(val_str)
        s = suffix.lower()
        if s == "k":
            v *= 1_000
        elif s == "m":
            v *= 1_000_000
        return v
    lo = _parse(m.group(1), m.group(2))
    hi = _parse(m.group(3), m.group(4))
    if lo <= 0 or hi <= lo:
        return None
    return (lo, hi)


def _extract_strike(title: str, spot: float) -> Optional[float]:
    """
    Extract a numeric strike from a market title string.

    Handles '$68,300', '$68k', '$1.5m' and the like.
    Returns None if no plausible value is found.
    """
    for pattern in _STRIKE_PATTERNS:
        match = re.search(pattern, title.replace(",", ""))
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                suffix = match.group(2).lower()
                if suffix == "k":
                    value *= 1_000
                elif suffix == "m":
                    value *= 1_000_000
                # Sanity: must be at least 1% of current spot (catches unitless noise)
                if value > spot * 0.01:
                    return value
            except (ValueError, IndexError):
                continue
    return None
