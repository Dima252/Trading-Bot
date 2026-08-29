"""Replay the trend sleeve over history.

Deliberately not sleeve A's engine. That one models entries, stops, targets and
OCO brackets because sleeve A takes discrete trades with defined risk. This
sleeve holds continuous target weights and rebalances on a schedule, so a
trade-oriented engine would be the wrong shape and would smuggle sleeve A's
assumptions in with it.

What it does model, because each would flatter the result if skipped:

* costs on every weight change, not just on entries
* idle cash earning the prevailing T-bill rate, as sleeve A's engine does
* rebalancing on a fixed schedule rather than continuously, so the turnover is
  the turnover a real account would actually pay
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date

from ..data.models import BarSeries
from .trend import target_weights

# One side. A rebalance that halves a position pays this on the half it sells.
COST_BPS = 5.0


@dataclass
class TrendResult:
    days: list[date] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    weights: list[dict[str, float]] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    starting_equity: float = 100_000.0
    total_costs: float = 0.0
    rebalances: int = 0

    @property
    def final_equity(self) -> float:
        return self.equity[-1] if self.equity else self.starting_equity


def run_trend(
    universe: dict[str, BarSeries],
    calendar: list[date],
    start: date,
    end: date,
    cash_rates: BarSeries | None = None,
    rebalance_days: int = 5,
    starting_equity: float = 100_000.0,
    cost_bps: float = COST_BPS,
    long_only: bool = True,
) -> TrendResult:
    """Walk the calendar, rebalancing every `rebalance_days` sessions."""
    result = TrendResult(starting_equity=starting_equity)
    sessions = [d for d in calendar if start <= d <= end]
    if not sessions:
        return result

    equity = starting_equity
    held: dict[str, float] = {}     # symbol -> weight actually held

    for n, day in enumerate(sessions):
        # --- mark the book to today ------------------------------------ #
        if n > 0:
            prev = sessions[n - 1]
            invested = 0.0
            portfolio_return = 0.0
            for symbol, weight in held.items():
                series = universe.get(symbol)
                if series is None:
                    continue
                i, j = series.index_of(prev), series.index_of(day)
                if i is None or j is None or series[i].close <= 0:
                    continue
                asset_return = (series[j].close - series[i].close) / series[i].close
                portfolio_return += weight * asset_return
                invested += abs(weight)

            # Whatever is not invested earns the prevailing short rate. Without
            # this a sleeve that is 60% in cash looks far worse than it was.
            if cash_rates is not None and invested < 1.0:
                k = cash_rates.index_asof(day)
                if k is not None:
                    annual = cash_rates[k].close / 100.0
                    if annual > 0:
                        portfolio_return += (1.0 - invested) * annual / 252.0

            equity *= (1.0 + portfolio_return)

        # --- rebalance on schedule -------------------------------------- #
        if n % rebalance_days == 0:
            wanted = {
                sym: sig.weight
                for sym, sig in target_weights(
                    universe, day, long_only=long_only
                ).items()
            }
            traded = sum(
                abs(wanted.get(s, 0.0) - held.get(s, 0.0))
                for s in set(wanted) | set(held)
            )
            cost = traded * (cost_bps / 10_000.0)
            equity *= (1.0 - cost)
            result.total_costs += cost
            result.turnover.append(traded)
            result.rebalances += 1
            held = wanted

        result.days.append(day)
        result.equity.append(equity)
        result.weights.append(dict(held))

    return result


def summarise(result: TrendResult, risk_free: float = 0.0) -> dict[str, float]:
    """Return, volatility, Sharpe and drawdown from the equity curve."""
    from statistics import stdev

    if len(result.equity) < 2:
        return {}

    rets = [
        (b - a) / a
        for a, b in zip(result.equity, result.equity[1:], strict=False)
        if a > 0
    ]
    years = (result.days[-1] - result.days[0]).days / 365.25 or 1.0
    total = result.final_equity / result.starting_equity - 1.0
    cagr = (1.0 + total) ** (1.0 / years) - 1.0

    peak, max_dd = result.starting_equity, 0.0
    for value in result.equity:
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak)

    vol = stdev(rets) * (252 ** 0.5) if len(rets) > 1 else 0.0
    # Same floor as the main metrics module: a near-zero denominator produces a
    # Sharpe in the thousands, which is not a large number but an undefined one.
    sharpe = (cagr - risk_free) / vol if vol > 0.001 else 0.0

    invested = [sum(abs(w) for w in day.values()) for day in result.weights]

    return {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "years": years,
        "rebalances": float(result.rebalances),
        "avg_turnover": (
            sum(result.turnover) / len(result.turnover) if result.turnover else 0.0
        ),
        "total_costs": result.total_costs,
        "avg_invested": sum(invested) / len(invested) if invested else 0.0,
    }


def monthly_returns(days: list[date], equity: list[float]) -> dict[str, float]:
    """Month-end returns, keyed `YYYY-MM` -- the series correlations are
    measured on. Daily correlation is dominated by microstructure noise and
    overstates how alike two strategies really are."""
    by_month: dict[str, float] = {}
    for day, value in zip(days, equity, strict=False):
        by_month[f"{day.year:04d}-{day.month:02d}"] = value

    months = sorted(by_month)
    return {
        m: (by_month[m] - by_month[p]) / by_month[p]
        for p, m in itertools.pairwise(months)
        if by_month[p] > 0
    }
