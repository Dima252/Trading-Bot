"""Fill modelling.

Optimism here is the most common way a backtest lies. Three rules:

1. Buys fill above the reference price, sells below it, always.
2. A gap through a stop fills at the OPEN, not the stop price. This is where
   stop-based position sizing actually breaks, so the model has to show it.
3. When a bar touches both the stop and the target, assume the STOP. Intraday
   sequence is unknowable from daily bars, and guessing favourably would
   manufacture an edge that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.models import Bar


@dataclass(frozen=True)
class FillModel:
    slippage_bps: float = 5.0
    commission_per_share: float = 0.0

    def buy(self, price: float) -> float:
        return price * (1.0 + self.slippage_bps / 10_000.0)

    def sell(self, price: float) -> float:
        return price * (1.0 - self.slippage_bps / 10_000.0)

    def commission(self, qty: int) -> float:
        return qty * self.commission_per_share


@dataclass(frozen=True)
class ExitFill:
    price: float
    reason: str


def check_exit(bar: Bar, stop: float, target: float) -> ExitFill | None:
    """Did this bar take the position out, and at what price?"""
    # 1. gapped through the stop overnight -- the worst and most realistic case
    if bar.open <= stop:
        return ExitFill(bar.open, "gap_through_stop")

    # 2. gapped through the target
    if bar.open >= target:
        return ExitFill(bar.open, "gap_through_target")

    # 3. touched the stop intraday. Checked before the target on purpose.
    if bar.low <= stop:
        return ExitFill(stop, "stop")

    # 4. touched the target intraday
    if bar.high >= target:
        return ExitFill(target, "target")

    return None


def check_exit_intraday(bar: Bar, stop: float, target: float) -> ExitFill | None:
    """Exit check for a position entered DURING this bar.

    The open-gap branches don't apply -- the gap happened before we owned it.
    Stop still takes precedence over target for the same reason as above.
    """
    if bar.low <= stop:
        return ExitFill(stop, "stop")
    if bar.high >= target:
        return ExitFill(target, "target")
    return None


def check_limit_fill(bar: Bar, limit: float) -> float | None:
    """A resting buy limit fills if price trades down to it."""
    if bar.open <= limit:
        return bar.open  # opened below the limit -- a better fill than asked
    if bar.low <= limit:
        return limit
    return None
