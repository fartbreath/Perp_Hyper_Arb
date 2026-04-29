"""
strategies.OpeningNeutral — Strategy 5: Opening Neutral.

Simultaneously buys YES and NO of the same bucket market within
OPENING_NEUTRAL_ENTRY_WINDOW_SECS of the market opening.  When both legs
fill (combined cost ≤ 1 + slip), the pair is guaranteed-profitable at
resolution.  When only one leg fills it is promoted to a standard momentum
position.  See strategies/OpeningNeutral/PLAN.md for full specification.
"""
from strategies.OpeningNeutral.scanner import OpeningNeutralScanner
