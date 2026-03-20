"""
strategies.base — Abstract base class for all trading strategies.

Every strategy must implement start/stop/get_signals so main.py can manage
them uniformly and so adding a new strategy requires zero changes to existing
strategy files or the bot entry point.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseStrategy(ABC):
    """Minimal interface every strategy must satisfy."""

    @abstractmethod
    async def start(self) -> None:
        """Start the strategy (register callbacks, spawn background tasks)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the strategy."""

    @abstractmethod
    def get_signals(self) -> Any:
        """
        Return the current signal snapshot.

        Type is strategy-specific (dict for maker, list for mispricing scanner);
        callers should treat the return value as opaque and not depend on the
        concrete type unless they have a typed reference.
        """
