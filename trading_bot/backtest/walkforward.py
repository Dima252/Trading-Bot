"""Walk-forward variant testing.

This system fits nothing, so "walk-forward" here means the thing that actually
guards against self-deception: **does a pre-registered change hold up across
independent time periods, or does it come from one lucky stretch?**

A variant that improves the aggregate is worth nothing on its own -- with six
years and a handful of knobs you can always find one. A variant that improves
four folds out of five is a candidate for belief.

Two rules the caller is expected to honour, because no code can enforce them:

1. State the hypothesis BEFORE the run. A variant invented after looking at
   fold results is fitted, whatever the numbers say.
2. Keep a holdout. `--holdout` reserves the most recent period and excludes it
   from every fold, so there is one period left that no decision has touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..core.policy import Policy
from ..data.models import BarSeries
from .engine import BacktestConfig, run_backtest
from .metrics import (
    BenchmarkStats,
    CurveStats,
    Stats,
    benchmark_stats,
    curve_stats,
    stats_for,
)


@dataclass(frozen=True)
class Fold:
    label: str
    start: date
    end: date

    @property
    def span(self) -> str:
        return f"{self.start} -> {self.end}"


@dataclass(frozen=True)
class FoldResult:
    variant: str
    fold: Fold
    stats: Stats
    curve: CurveStats
    benchmark: BenchmarkStats

    @property
    def excess(self) -> float:
        return self.benchmark.excess_return(self.curve)

    @property
    def return_per_dd(self) -> float:
        from .metrics import risk_adjusted

        return risk_adjusted(self.curve.total_return, self.curve.max_drawdown)

    @property
    def benchmark_return_per_dd(self) -> float:
        from .metrics import risk_adjusted

        return risk_adjusted(
            self.benchmark.total_return, self.benchmark.max_drawdown
        )

    @property
    def beats_benchmark_risk_adjusted(self) -> bool:
        """The bar set in PLAN.md section 5, evaluated per fold."""
        return self.return_per_dd > self.benchmark_return_per_dd


@dataclass
class WalkForward:
    baseline: str
    folds: list[Fold] = field(default_factory=list)
    results: list[FoldResult] = field(default_factory=list)
    holdout: Fold | None = None

    def for_variant(self, name: str) -> list[FoldResult]:
        return [r for r in self.results if r.variant == name]

    def variants(self) -> list[str]:
        seen: list[str] = []
        for r in self.results:
            if r.variant not in seen:
                seen.append(r.variant)
        return seen


def make_folds(
    days: list[date], n_folds: int = 4, warmup_days: int = 400, holdout: bool = True
) -> tuple[list[Fold], Fold | None]:
    """Sequential, non-overlapping periods after the indicator warm-up.

    The final period is held out entirely when `holdout` is set -- it exists so
    that after all the experimenting there is still one stretch of history no
    decision has been fitted to.
    """
    usable = [d for d in days if d >= days[0] + _days(warmup_days)]
    if len(usable) < (n_folds + 1) * 30:
        raise ValueError("not enough history to split into folds")

    total = n_folds + (1 if holdout else 0)
    size = len(usable) // total

    folds = [
        Fold(f"F{i + 1}", usable[i * size], usable[(i + 1) * size - 1])
        for i in range(n_folds)
    ]
    held = (
        Fold("HOLDOUT", usable[n_folds * size], usable[-1]) if holdout else None
    )
    return folds, held


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def run_walkforward(
    universe: dict[str, BarSeries],
    sectors: dict[str, str],
    variants: dict[str, Policy],
    folds: list[Fold],
    baseline: str = "baseline",
    benchmark: str = "SPY",
    starting_equity: float = 100_000.0,
    progress: bool = True,
) -> WalkForward:
    out = WalkForward(baseline=baseline, folds=folds)

    for fold in folds:
        for name, policy in variants.items():
            if progress:
                print(f"  {fold.label} {fold.span}  {name}...", flush=True)
            result = run_backtest(
                universe,
                sectors,
                BacktestConfig(
                    start=fold.start,
                    end=fold.end,
                    starting_equity=starting_equity,
                    benchmark=benchmark,
                    # the shadow book dominates runtime and answers a different
                    # question; a variant comparison does not need it
                    record_shadow=False,
                ),
                policy,
            )
            out.results.append(
                FoldResult(
                    variant=name,
                    fold=fold,
                    stats=stats_for(result.trades),
                    curve=curve_stats(result),
                    benchmark=benchmark_stats(result),
                )
            )
    return out


# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class VariantVerdict:
    name: str
    folds_won: int
    folds_total: int
    mean_expectancy: float
    baseline_expectancy: float
    total_trades: int

    @property
    def delta(self) -> float:
        return round(self.mean_expectancy - self.baseline_expectancy, 4)

    @property
    def verdict(self) -> str:
        if self.name == "baseline":
            return "reference"
        if self.total_trades < 100:
            return "too few trades to judge"
        if self.delta <= 0:
            return "no improvement"
        if self.folds_won == self.folds_total:
            return "held in EVERY fold -- worth testing on the holdout"
        if self.folds_won >= self.folds_total - 1:
            return "held in most folds -- promising"
        return "improved on aggregate but NOT consistently -- likely luck"


def verdicts(wf: WalkForward) -> list[VariantVerdict]:
    base = wf.for_variant(wf.baseline)
    base_by_fold = {r.fold.label: r for r in base}
    base_mean = _mean([r.stats.expectancy_r for r in base])

    out: list[VariantVerdict] = []
    for name in wf.variants():
        results = wf.for_variant(name)
        won = sum(
            1
            for r in results
            if r.fold.label in base_by_fold
            and r.stats.expectancy_r > base_by_fold[r.fold.label].stats.expectancy_r
        )
        out.append(
            VariantVerdict(
                name=name,
                folds_won=won,
                folds_total=len(results),
                mean_expectancy=round(
                    _mean([r.stats.expectancy_r for r in results]), 4
                ),
                baseline_expectancy=round(base_mean, 4),
                total_trades=sum(r.stats.trades for r in results),
            )
        )
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def format_walkforward(wf: WalkForward) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("WALK-FORWARD")
    lines.append("=" * 78)
    lines.append(f"  {len(wf.folds)} folds, {len(wf.variants())} variants")
    for fold in wf.folds:
        lines.append(f"    {fold.label}  {fold.span}")
    if wf.holdout:
        lines.append(f"    {wf.holdout.label}  {wf.holdout.span}  (untouched)")

    for name in wf.variants():
        lines.append("")
        lines.append(f"  {name}")
        lines.append(
            f"    {'fold':<7}{'trades':>7}{'exp R':>8}{'win%':>7}"
            f"{'return':>9}{'vs SPY':>9}{'maxDD':>8}{'bmDD':>8}"
            f"{'ret/DD':>8}{'bm':>6}   bar"
        )
        for r in wf.for_variant(name):
            lines.append(
                f"    {r.fold.label:<7}{r.stats.trades:>7}"
                f"{r.stats.expectancy_r:>8.3f}{r.stats.win_rate:>7.1%}"
                f"{r.curve.total_return:>9.2%}{r.excess:>+9.2%}"
                f"{r.curve.max_drawdown:>8.2%}"
                f"{r.benchmark.max_drawdown:>8.2%}"
                f"{r.return_per_dd:>8.2f}{r.benchmark_return_per_dd:>6.2f}"
                f"   {'PASS' if r.beats_benchmark_risk_adjusted else 'fail'}"
            )
        passes = sum(
            1 for r in wf.for_variant(name) if r.beats_benchmark_risk_adjusted
        )
        lines.append(
            f"    {'':<7}clears the PLAN section 5 bar in "
            f"{passes}/{len(wf.for_variant(name))} folds"
        )

    lines.append("")
    lines.append("  verdicts")
    lines.append(
        f"    {'variant':<22}{'folds won':>11}{'mean exp R':>12}"
        f"{'vs base':>10}   conclusion"
    )
    for v in verdicts(wf):
        won = "-" if v.name == wf.baseline else f"{v.folds_won}/{v.folds_total}"
        lines.append(
            f"    {v.name:<22}{won:>11}{v.mean_expectancy:>12.3f}"
            f"{v.delta:>+10.3f}   {v.verdict}"
        )

    lines.append("")
    lines.append("  Consistency is the test, not the aggregate. A variant that wins")
    lines.append("  on the total but only in one fold is a fitted result wearing a")
    lines.append("  disguise. Nothing here has touched the holdout.")
    lines.append("=" * 78)
    return "\n".join(lines)
