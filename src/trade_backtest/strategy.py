"""Strategy interface.

A strategy is pure decision logic: it sees each timestamp's bars and
returns signals. It never sees the future (the engine fills signaled
orders at the *next* bar's open), never touches cash or positions, and
keeps whatever history it needs internally. Starter strategies live in
the ``trade-strategies`` repo; ``examples/sma_crossover.py`` shows the
minimal shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .models import Bar, Signal


class Strategy(ABC):
    """Abstract trading strategy."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = [s.strip().upper() for s in symbols]
        if not self.symbols:
            raise ValueError("strategy needs at least one symbol")

    @abstractmethod
    def on_bar(self, timestamp: datetime, bars: dict[str, Bar]) -> list[Signal]:
        """Return signals for this timestamp (possibly empty)."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"{type(self).__name__}(symbols={self.symbols})"
