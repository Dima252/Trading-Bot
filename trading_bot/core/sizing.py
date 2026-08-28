"""Fixed fractional position sizing.

Risk per trade is constant; position size is not. A tight stop buys more shares
than a wide one for the same dollar risk.

Sized off CASH and EQUITY -- never `buying_power`, which includes margin and
would silently lever the account.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .policy import Policy


@dataclass(frozen=True)
class Sizing:
    qty: int
    risk_dollars: float
    notional: float
    binding_constraint: str

    @property
    def is_tradeable(self) -> bool:
        return self.qty > 0


def size_position(
    entry: float,
    stop: float,
    equity: float,
    cash: float,
    policy: Policy,
) -> Sizing:
    """Shares to buy, capped three ways: risk, position size, deployable cash."""
    risk_per_share = entry - stop
    if entry <= 0 or risk_per_share <= 0 or equity <= 0:
        return Sizing(0, 0.0, 0.0, "invalid_input")

    by_risk = math.floor((equity * policy.max_risk_per_trade) / risk_per_share)
    by_position_cap = math.floor((equity * policy.max_position_pct) / entry)

    deployable = cash - (equity * policy.min_cash_reserve)
    by_cash = math.floor(deployable / entry) if deployable > 0 else 0

    caps = {
        "risk_per_trade": by_risk,
        "position_size_cap": by_position_cap,
        "cash_reserve": by_cash,
    }
    binding = min(caps, key=lambda k: caps[k])
    qty = max(0, caps[binding])

    return Sizing(
        qty=qty,
        risk_dollars=qty * risk_per_share,
        notional=qty * entry,
        binding_constraint=binding if qty > 0 else "no_capacity",
    )
