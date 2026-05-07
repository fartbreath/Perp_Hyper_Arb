"""
strategies.ReverseOpenNeutral — Strategy 5b: Reverse Opening Neutral.

Entry is identical to OpeningNeutral (simultaneous YES+NO buy at open).
Exit is INVERTED: when the loser bid drops to the trigger threshold,
the WINNER is sold at market (take-profit) and the LOSER is held to
market resolution instead of being exited.

Gate: config.REVERSE_OPENING_NEUTRAL_ENABLED must be True.
See strategies/ReverseOpenNeutral/scanner.py for full implementation.
"""
from strategies.ReverseOpenNeutral.scanner import ReverseOpenNeutralScanner

__all__ = ["ReverseOpenNeutralScanner"]
