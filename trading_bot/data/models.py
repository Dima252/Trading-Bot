"""Bar data structures.

Deliberately plain Python rather than DataFrames. The signal engine runs
identically in backtest and live, and a list of floats behaves the same in both;
a DataFrame invites `.iloc[-1]` habits that silently read the future.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dollar_volume(self) -> float:
        return self.close * self.volume


@dataclass(frozen=True)
class BarSeries:
    """Chronologically ordered bars for one symbol."""

    symbol: str
    bars: list[Bar]

    def __post_init__(self) -> None:
        days = [b.day for b in self.bars]
        if days != sorted(days):
            raise ValueError(f"{self.symbol}: bars must be in chronological order")
        if len(set(days)) != len(days):
            raise ValueError(f"{self.symbol}: duplicate bar dates")

    def __len__(self) -> int:
        return len(self.bars)

    def __getitem__(self, i: int) -> Bar:
        return self.bars[i]

    @property
    def days(self) -> list[date]:
        return [b.day for b in self.bars]

    @property
    def opens(self) -> list[float]:
        return [b.open for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [b.volume for b in self.bars]

    def index_of(self, day: date) -> int | None:
        """Exact match only."""
        i = bisect_right(self.days, day) - 1
        if i >= 0 and self.bars[i].day == day:
            return i
        return None

    def index_asof(self, day: date) -> int | None:
        """Latest bar at or before `day`. The only safe lookup in a backtest."""
        i = bisect_right(self.days, day) - 1
        return i if i >= 0 else None
