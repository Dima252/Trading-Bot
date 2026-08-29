"""Turning a backtest into answers.

Two things matter more than the headline return:

* The MFE/MAE diagnostics, which tell you whether stops and targets are placed
  well -- learnable from ~30 trades rather than 3,000 (README section 9.3).
* The shadow book, which is the only evidence that a filter is worth having.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, stdev

from .records import BacktestResult, ShadowRecord, TradeRecord

# Annualised volatility below which a Sharpe ratio is meaningless rather than
# merely large. 0.1% a year is far below anything a traded book produces.
MIN_VOLATILITY = 0.001


@dataclass(frozen=True)
class Stats:
    trades: int = 0
    win_rate: float = 0.0
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    profit_factor: float = 0.0
    total_r: float = 0.0
    avg_days_held: float = 0.0

    @property
    def is_meaningful(self) -> bool:
        """Below this, a bucket is noise -- the same floor the adaptive layer
        uses before it will act on an observation."""
        return self.trades >= 30


def stats_for(trades: list[TradeRecord]) -> Stats:
    if not trades:
        return Stats()

    rs = [t.realized_r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return Stats(
        trades=len(trades),
        win_rate=round(len(wins) / len(trades), 4),
        expectancy_r=round(mean(rs), 4),
        avg_win_r=round(mean(wins), 3) if wins else 0.0,
        avg_loss_r=round(mean(losses), 3) if losses else 0.0,
        profit_factor=round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
        total_r=round(sum(rs), 2),
        avg_days_held=round(mean([t.days_held for t in trades]), 1),
    )


@dataclass(frozen=True)
class Diagnostics:
    """Placement quality, not profitability."""

    avg_mfe_of_losers: float = 0.0
    pct_losers_that_reached_1r: float = 0.0
    avg_mae_of_winners: float = 0.0
    avg_post_exit_r_after_target: float = 0.0
    stops_too_tight: bool = False
    targets_too_tight: bool = False


def diagnostics_for(trades: list[TradeRecord]) -> Diagnostics:
    if not trades:
        return Diagnostics()

    losers = [t for t in trades if not t.is_win]
    winners = [t for t in trades if t.is_win]
    target_exits = [
        t
        for t in trades
        if "target" in t.exit_reason and t.post_exit_r_10d is not None
    ]

    avg_mfe_losers = round(mean([t.mfe_r for t in losers]), 3) if losers else 0.0
    reached_1r = (
        round(len([t for t in losers if t.mfe_r >= 1.0]) / len(losers), 3)
        if losers
        else 0.0
    )
    avg_mae_winners = round(mean([t.mae_r for t in winners]), 3) if winners else 0.0
    avg_post = (
        round(mean([t.post_exit_r_10d for t in target_exits]), 3)
        if target_exits
        else 0.0
    )

    return Diagnostics(
        avg_mfe_of_losers=avg_mfe_losers,
        pct_losers_that_reached_1r=reached_1r,
        avg_mae_of_winners=avg_mae_winners,
        avg_post_exit_r_after_target=avg_post,
        # a third of losers getting to +1R first says the exit is inside the noise
        stops_too_tight=reached_1r > 0.33,
        # winners routinely running further after we sold
        targets_too_tight=avg_post > 0.5,
    )


@dataclass(frozen=True)
class CurveStats:
    total_return: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    days: int = 0
    final_equity: float = 0.0
    exposure: float = 0.0

    # Annualised standard deviation of daily equity returns, and the Sharpe
    # ratio measured against the cash rate that actually prevailed. These are
    # what a leverage decision is made from -- return alone says nothing about
    # how much of it you could have afforded to take.
    volatility: float = 0.0
    sharpe: float = 0.0


def curve_stats(result: BacktestResult) -> CurveStats:
    if not result.curve:
        return CurveStats()

    start_eq = result.starting_equity
    final = result.final_equity
    days = (result.curve[-1].day - result.curve[0].day).days or 1

    peak, max_dd = start_eq, 0.0
    for point in result.curve:
        peak = max(peak, point.equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - point.equity) / peak)

    years = days / 365.25
    cagr = ((final / start_eq) ** (1 / years) - 1) if years > 0 and start_eq > 0 else 0.0

    # Daily equity returns -> annualised volatility -> Sharpe against the cash
    # rate the run actually saw. A hardcoded risk-free would be wrong by whole
    # points across a window that spans 6% rates and 0% rates.
    rets = [
        (b.equity - a.equity) / a.equity
        for a, b in zip(result.curve, result.curve[1:], strict=False)
        if a.equity > 0
    ]
    vol = (stdev(rets) * (252 ** 0.5)) if len(rets) > 1 else 0.0
    rf = result.avg_cash_rate or 0.0
    # A floor, not `vol > 0`. A curve with near-identical daily returns has a
    # volatility of ~1e-16 and would report a Sharpe in the trillions -- the
    # same divide-by-almost-zero that once produced 23,720,292R of expectancy.
    # Below this the ratio is not small, it is undefined.
    sharpe = (cagr - rf) / vol if vol > MIN_VOLATILITY else 0.0

    return CurveStats(
        volatility=round(vol, 4),
        sharpe=round(sharpe, 2),
        total_return=round(final / start_eq - 1, 4),
        cagr=round(cagr, 4),
        max_drawdown=round(max_dd, 4),
        days=days,
        final_equity=round(final, 2),
        exposure=round(
            mean([1.0 if p.positions else 0.0 for p in result.curve]), 3
        ),
    )


@dataclass(frozen=True)
class BenchmarkStats:
    """Buy-and-hold over the identical window.

    The first question about any long-only strategy is whether it beat simply
    owning the index. A system that returns 20% while the benchmark returned 30%
    destroyed value, however good the trade statistics look.
    """

    total_return: float = 0.0
    cagr: float = 0.0
    max_drawdown: float = 0.0
    available: bool = False

    def excess_return(self, strategy: CurveStats) -> float:
        return strategy.total_return - self.total_return

    def verdict(self, strategy: CurveStats) -> str:
        if not self.available:
            return "no benchmark data"
        excess = self.excess_return(strategy)
        if excess <= 0:
            return "UNDERPERFORMED buy-and-hold -- the trading added nothing"
        if strategy.max_drawdown > self.max_drawdown and excess < 0.05:
            return "beat buy-and-hold, but with deeper drawdowns -- marginal"
        return "beat buy-and-hold"


def benchmark_stats(result: BacktestResult) -> BenchmarkStats:
    path = [price for _, price in result.benchmark if price and price > 0]
    if len(path) < 2:
        return BenchmarkStats()

    first, last = path[0], path[-1]
    days = (result.benchmark[-1][0] - result.benchmark[0][0]).days or 1
    years = days / 365.25

    peak, max_dd = first, 0.0
    for price in path:
        peak = max(peak, price)
        max_dd = max(max_dd, (peak - price) / peak)

    return BenchmarkStats(
        total_return=round(last / first - 1, 4),
        cagr=round((last / first) ** (1 / years) - 1, 4) if years > 0 else 0.0,
        max_drawdown=round(max_dd, 4),
        available=True,
    )


def risk_adjusted(total_return: float, max_drawdown: float) -> float:
    """Return per unit of drawdown. Crude, but it is the comparison that stops
    leverage from looking like skill."""
    return round(total_return / max_drawdown, 2) if max_drawdown > 0 else 0.0


@dataclass(frozen=True)
class ShadowVerdict:
    reason: str
    count: int
    expectancy_r: float
    win_rate: float

    def verdict_vs(self, taken: Stats) -> str:
        if self.count < 30:
            return "insufficient sample"
        if self.expectancy_r > taken.expectancy_r:
            return "FILTER IS COSTING YOU -- skipped trades beat taken ones"
        return "filter looks justified"


def shadow_verdicts(shadow: list[ShadowRecord]) -> list[ShadowVerdict]:
    """Expectancy of the trades we declined, grouped by why we declined them."""
    buckets: dict[str, list[ShadowRecord]] = {}
    for record in shadow:
        if record.hypothetical_r is None or record.not_taken_reason == "taken":
            continue
        buckets.setdefault(record.not_taken_reason, []).append(record)

    out = []
    for reason, records in sorted(buckets.items()):
        rs = [r.hypothetical_r for r in records]
        out.append(
            ShadowVerdict(
                reason=reason,
                count=len(records),
                expectancy_r=round(mean(rs), 4),
                win_rate=round(len([r for r in rs if r > 0]) / len(rs), 3),
            )
        )
    return sorted(out, key=lambda v: -v.count)


@dataclass
class Report:
    overall: Stats = field(default_factory=Stats)
    curve: CurveStats = field(default_factory=CurveStats)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    by_setup: dict[str, Stats] = field(default_factory=dict)
    by_regime: dict[str, Stats] = field(default_factory=dict)
    by_exit: dict[str, Stats] = field(default_factory=dict)
    shadow: list[ShadowVerdict] = field(default_factory=list)
    benchmark: BenchmarkStats = field(default_factory=BenchmarkStats)


def build_report(result: BacktestResult) -> Report:
    trades = result.trades

    def group(key) -> dict[str, Stats]:
        buckets: dict[str, list[TradeRecord]] = {}
        for t in trades:
            buckets.setdefault(key(t), []).append(t)
        return {k: stats_for(v) for k, v in sorted(buckets.items())}

    return Report(
        overall=stats_for(trades),
        curve=curve_stats(result),
        diagnostics=diagnostics_for(trades),
        by_setup=group(lambda t: t.setup_type.value),
        by_regime=group(lambda t: t.regime_at_entry.value),
        by_exit=group(lambda t: t.exit_reason.split(":")[0][:28]),
        shadow=shadow_verdicts(result.shadow),
        benchmark=benchmark_stats(result),
    )


def format_report(report: Report, universe_size: int = 0) -> str:
    lines: list[str] = []
    c, s, d = report.curve, report.overall, report.diagnostics

    lines.append("=" * 74)
    lines.append("BACKTEST REPORT")
    lines.append("=" * 74)
    lines.append(
        f"  final equity   ${c.final_equity:>12,.0f}   "
        f"total return {c.total_return:>8.2%}"
    )
    lines.append(
        f"  CAGR           {c.cagr:>13.2%}   max drawdown {c.max_drawdown:>8.2%}"
    )
    lines.append(
        f"  days           {c.days:>13}   time invested{c.exposure:>8.1%}"
    )

    b = report.benchmark
    if b.available:
        lines.append("")
        lines.append("  vs buy-and-hold")
        lines.append(
            f"    {'':<14}{'strategy':>12}{'benchmark':>12}{'diff':>12}"
        )
        lines.append(
            f"    {'total return':<14}{c.total_return:>11.2%}"
            f"{b.total_return:>12.2%}{b.excess_return(c):>+12.2%}"
        )
        lines.append(
            f"    {'CAGR':<14}{c.cagr:>11.2%}{b.cagr:>12.2%}"
            f"{c.cagr - b.cagr:>+12.2%}"
        )
        lines.append(
            f"    {'max drawdown':<14}{c.max_drawdown:>11.2%}"
            f"{b.max_drawdown:>12.2%}{c.max_drawdown - b.max_drawdown:>+12.2%}"
        )
        lines.append(
            f"    {'return / DD':<14}"
            f"{risk_adjusted(c.total_return, c.max_drawdown):>11.2f}"
            f"{risk_adjusted(b.total_return, b.max_drawdown):>12.2f}"
        )
        lines.append(f"    >> {b.verdict(c)}")

    lines.append("")
    lines.append(f"  trades         {s.trades:>13}   win rate     {s.win_rate:>8.1%}")
    lines.append(
        f"  expectancy     {s.expectancy_r:>12.3f}R   profit factor{s.profit_factor:>8.2f}"
    )
    lines.append(
        f"  avg win        {s.avg_win_r:>12.2f}R   avg loss     {s.avg_loss_r:>7.2f}R"
    )
    lines.append(
        f"  total          {s.total_r:>12.1f}R   avg hold     {s.avg_days_held:>6.1f}d"
    )

    def table(title: str, buckets: dict[str, Stats]) -> None:
        if not buckets:
            return
        lines.append("")
        lines.append(f"  {title}")
        lines.append(
            f"    {'bucket':<26}{'n':>5}{'win%':>8}{'exp R':>9}{'total R':>10}"
        )
        for name, st in sorted(buckets.items(), key=lambda kv: -kv[1].trades):
            mark = "" if st.is_meaningful else "  (thin)"
            lines.append(
                f"    {name:<26}{st.trades:>5}{st.win_rate:>8.1%}"
                f"{st.expectancy_r:>9.3f}{st.total_r:>10.1f}{mark}"
            )

    table("by setup", report.by_setup)
    table("by regime at entry", report.by_regime)
    table("by exit reason", report.by_exit)

    lines.append("")
    lines.append("  placement diagnostics")
    lines.append(f"    avg MFE of losers            {d.avg_mfe_of_losers:>7.2f}R")
    lines.append(
        f"    losers that first hit +1R    {d.pct_losers_that_reached_1r:>7.1%}"
    )
    lines.append(f"    avg MAE of winners           {d.avg_mae_of_winners:>7.2f}R")
    lines.append(
        f"    avg move 10d after a target  {d.avg_post_exit_r_after_target:>7.2f}R"
    )
    if d.stops_too_tight:
        lines.append("    >> stops look too tight: losers are being shaken out")
    if d.targets_too_tight:
        lines.append("    >> targets look too tight: winners keep running after exit")

    if report.shadow:
        lines.append("")
        lines.append("  shadow book -- what we did NOT take")
        lines.append(f"    {'reason':<26}{'n':>7}{'exp R':>9}{'win%':>8}   verdict")
        for v in report.shadow:
            lines.append(
                f"    {v.reason:<26}{v.count:>7}{v.expectancy_r:>9.3f}"
                f"{v.win_rate:>8.1%}   {v.verdict_vs(s)}"
            )

    lines.append("")
    lines.append("  NOTE: universe is a fixed present-day list, so these results")
    lines.append("  carry survivorship bias. Trust RELATIVE comparisons between")
    lines.append("  setups, regimes and filters; discount absolute returns.")
    lines.append("=" * 74)
    return "\n".join(lines)
