"""The constitution.

Validates a proposed action list. It takes no part in strategy -- it only says
no. Two properties matter:

1. It only ever vetoes RISK-INCREASING actions. A safety layer that can block a
   CLOSE is a liability, not a safeguard.
2. It evaluates actions against a running projection, so the fifth OPEN in a
   batch is checked against the capital the first four already consumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Action, ActionKind, Portfolio, Rejection, Verdict
from .policy import Policy
from .sizing import size_position


@dataclass
class _Projection:
    """Mutable running state as approved actions are applied."""

    cash: float
    equity: float
    heat_dollars: float
    sector_dollars: dict[str, float]
    held: set[str]
    new_positions: int = 0
    freed: list[str] = field(default_factory=list)

    @classmethod
    def from_portfolio(cls, p: Portfolio) -> "_Projection":
        return cls(
            cash=p.cash,
            equity=p.equity,
            heat_dollars=p.open_heat_dollars,
            sector_dollars=dict(p.sector_exposure),
            held=set(p.tickers),
        )


class ConstraintLayer:
    """`sectors` supplies ticker->sector for tickers not currently held.

    Without it, sector limits silently never fire on new positions -- the kind
    of failure you don't notice until you're concentrated.
    """

    def __init__(self, policy: Policy, sectors: dict[str, str] | None = None) -> None:
        self.policy = policy
        self.sectors = dict(sectors or {})

    # ------------------------------------------------------------------ #

    def validate(self, actions: list[Action], portfolio: Portfolio) -> Verdict:
        approved: list[Action] = []
        rejected: list[Rejection] = []
        proj = _Projection.from_portfolio(portfolio)

        # Pass 1: defensive and neutral actions always pass, and they free
        # capital that pass 2 is then allowed to spend.
        for action in actions:
            if action.kind.increases_risk:
                continue
            approved.append(action)
            self._apply_defensive(action, portfolio, proj)

        # Pass 2: risk-increasing actions, in the order decide() ranked them.
        halt = self._account_level_block(portfolio)
        for action in actions:
            if not action.kind.increases_risk:
                continue
            if halt is not None:
                rejected.append(Rejection(action, halt[0], halt[1]))
                continue
            failure = self._check(action, portfolio, proj)
            if failure is None:
                approved.append(action)
                self._apply_risk(action, portfolio, proj)
            else:
                rejected.append(Rejection(action, failure[0], failure[1]))

        return Verdict(approved=approved, rejected=rejected)

    # ------------------------------------------------------------------ #

    def _account_level_block(self, p: Portfolio) -> tuple[str, str] | None:
        """Conditions that block every risk-increasing action outright."""
        if p.halted:
            return ("kill_switch", "HALT flag is set")
        if p.equity <= 0:
            return ("no_equity", "equity is zero or negative")
        if p.day_pnl_pct <= self.policy.daily_loss_breaker:
            return (
                "daily_loss_breaker",
                f"day P&L {p.day_pnl_pct:.2%} <= {self.policy.daily_loss_breaker:.2%}",
            )
        if p.week_pnl_pct <= self.policy.weekly_loss_breaker:
            return (
                "weekly_loss_breaker",
                f"week P&L {p.week_pnl_pct:.2%} <= "
                f"{self.policy.weekly_loss_breaker:.2%}",
            )
        return None

    def _check(
        self, a: Action, portfolio: Portfolio, proj: _Projection
    ) -> tuple[str, str] | None:
        pol = self.policy
        eps = 1e-6

        if a.qty <= 0:
            return ("zero_quantity", "quantity must be positive")
        if a.limit is None or a.stop is None:
            return ("missing_levels", "risk-increasing action needs limit and stop")
        if a.stop >= a.limit:
            return ("invalid_stop", f"stop {a.stop} must sit below entry {a.limit}")

        if a.kind is ActionKind.OPEN and a.ticker in proj.held:
            return ("duplicate_position", f"already holding {a.ticker}")
        if a.kind is ActionKind.ADD and a.ticker not in proj.held:
            return ("no_position_to_add", f"not holding {a.ticker}")

        if (
            a.kind is ActionKind.OPEN
            and proj.new_positions >= pol.max_new_positions_per_day
        ):
            return (
                "max_new_positions",
                f"{proj.new_positions} already opened, "
                f"cap {pol.max_new_positions_per_day}",
            )

        # Recompute size independently -- never trust an upstream quantity.
        allowed = size_position(a.limit, a.stop, proj.equity, proj.cash, pol)
        if a.qty > allowed.qty:
            return (
                "oversized",
                f"qty {a.qty} exceeds {allowed.qty} "
                f"(binding: {allowed.binding_constraint})",
            )

        notional = a.qty * a.limit
        risk = a.qty * (a.limit - a.stop)

        if risk > proj.equity * pol.max_risk_per_trade + eps:
            return (
                "max_risk_per_trade",
                f"risk {risk / proj.equity:.2%} > {pol.max_risk_per_trade:.2%}",
            )

        heat_after = (proj.heat_dollars + risk) / proj.equity
        if heat_after > pol.max_portfolio_heat + eps:
            return (
                "max_portfolio_heat",
                f"heat would reach {heat_after:.2%}, cap {pol.max_portfolio_heat:.2%}",
            )

        sector = self._sector_for(a.ticker, portfolio)
        sector_after = (proj.sector_dollars.get(sector, 0.0) + notional) / proj.equity
        if sector_after > pol.max_sector_exposure + eps:
            return (
                "max_sector_exposure",
                f"{sector} would reach {sector_after:.2%}, "
                f"cap {pol.max_sector_exposure:.2%}",
            )

        existing = portfolio.position_for(a.ticker)
        held_value = existing.market_value if existing else 0.0
        position_after = (held_value + notional) / proj.equity
        if position_after > pol.max_position_pct + eps:
            return (
                "max_position_size",
                f"{a.ticker} would reach {position_after:.2%}, "
                f"cap {pol.max_position_pct:.2%}",
            )

        if proj.cash - notional < proj.equity * pol.min_cash_reserve - eps:
            return (
                "min_cash_reserve",
                f"cash would fall to {(proj.cash - notional) / proj.equity:.2%}, "
                f"floor {pol.min_cash_reserve:.2%}",
            )

        return None

    # ------------------------------------------------------------------ #

    def _apply_defensive(
        self, a: Action, portfolio: Portfolio, proj: _Projection
    ) -> None:
        pos = portfolio.position_for(a.ticker)
        if pos is None:
            return

        if a.kind is ActionKind.CLOSE:
            proj.cash += pos.market_value
            proj.heat_dollars -= pos.risk_dollars
            proj.sector_dollars[pos.sector] = max(
                0.0, proj.sector_dollars.get(pos.sector, 0.0) - pos.market_value
            )
            proj.held.discard(a.ticker)
            proj.freed.append(a.ticker)

        elif a.kind is ActionKind.TRIM and a.qty > 0:
            shares = min(a.qty, pos.qty)
            value = shares * pos.current_price
            proj.cash += value
            proj.heat_dollars -= shares * max(0.0, pos.current_price - pos.stop)
            proj.sector_dollars[pos.sector] = max(
                0.0, proj.sector_dollars.get(pos.sector, 0.0) - value
            )

        elif a.kind is ActionKind.ADJUST_STOP and a.stop is not None:
            old = pos.risk_dollars
            new = pos.qty * max(0.0, pos.current_price - a.stop)
            proj.heat_dollars += new - old

    def _apply_risk(
        self, a: Action, portfolio: Portfolio, proj: _Projection
    ) -> None:
        assert a.limit is not None and a.stop is not None
        notional = a.qty * a.limit
        sector = self._sector_for(a.ticker, portfolio)

        proj.cash -= notional
        proj.heat_dollars += a.qty * (a.limit - a.stop)
        proj.sector_dollars[sector] = proj.sector_dollars.get(sector, 0.0) + notional
        if a.kind is ActionKind.OPEN:
            proj.held.add(a.ticker)
            proj.new_positions += 1

    def _sector_for(self, ticker: str, portfolio: Portfolio) -> str:
        pos = portfolio.position_for(ticker)
        if pos is not None:
            return pos.sector
        return self.sectors.get(ticker, "UNKNOWN")
