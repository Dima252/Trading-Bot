"""Forward simulation of a single hypothetical trade.

Used by the shadow book: what would this candidate have done if we had taken it?
That is the only way to find out whether a filter is earning its keep.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.models import BarSeries
from .fills import check_exit


@dataclass(frozen=True)
class ForwardOutcome:
    outcome: str  # target | stop | timeout
    r_multiple: float
    days: int


def simulate_forward(
    series: BarSeries,
    start_index: int,
    entry: float,
    stop: float,
    target: float,
    max_days: int = 40,
) -> ForwardOutcome | None:
    """Walk forward from the bar AFTER `start_index` until the trade resolves."""
    risk = entry - stop
    if risk <= 0:
        return None

    end = min(start_index + max_days, len(series) - 1)
    if end <= start_index:
        return None

    for offset in range(1, end - start_index + 1):
        i = start_index + offset
        fill = check_exit(series[i], stop, target)
        if fill is not None:
            return ForwardOutcome(
                outcome="stop" if "stop" in fill.reason else "target",
                r_multiple=round((fill.price - entry) / risk, 3),
                days=offset,
            )

    final = series[end].close
    return ForwardOutcome(
        outcome="timeout",
        r_multiple=round((final - entry) / risk, 3),
        days=end - start_index,
    )
