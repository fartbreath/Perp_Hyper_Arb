"""
strategies.Momentum — Strategy 3: Momentum / price-confirmation taker.

Scans PM bucket markets for tokens at 80-90c where spot delta confirms the
high-probability side. See MomentumStrategy.md for full specification.
"""
from strategies.Momentum.scanner import MomentumScanner
from strategies.Momentum.signal import MomentumSignal
from strategies.Momentum.vol_fetcher import VolFetcher

__all__ = ["MomentumScanner", "MomentumSignal", "VolFetcher"]
