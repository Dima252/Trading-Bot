"""Walk-forward: fold construction and the consistency verdict.

The verdict logic is the whole point of the module -- a variant that wins on the
aggregate but only in one fold must be called out as luck, not promoted.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.synthetic import random_universe

from trading_bot.backtest.metrics import BenchmarkStats, CurveStats, Stats
from trading_bot.backtest.walkforward import (
    Fold,
    FoldResult,
    WalkForward,
    format_walkforward,
    make_folds,
    run_walkforward,
    verdicts,
)
from trading_bot.core.policy import Policy


def days_from(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def fold_result(variant: str, label: str, expectancy: float, trades: int = 60):
    return FoldResult(
        variant=variant,
        fold=Fold(label, date(2024, 1, 1), date(2024, 6, 1)),
        stats=Stats(trades=trades, expectancy_r=expectancy),
        curve=CurveStats(total_return=0.1, max_drawdown=0.1),
        benchmark=BenchmarkStats(total_return=0.05, available=True),
    )


# --- folds ---------------------------------------------------------------- #


def test_folds_are_sequential_and_do_not_overlap() -> None:
    folds, holdout = make_folds(days_from(date(2020, 1, 1), 2000), n_folds=4)
    assert len(folds) == 4
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.end < later.start
    assert holdout is not None
    assert holdout.start > folds[-1].end


def test_the_holdout_is_after_every_fold() -> None:
    """It exists so one stretch of history stays untouched by every decision."""
    folds, holdout = make_folds(days_from(date(2020, 1, 1), 2000), n_folds=3)
    assert all(f.end < holdout.start for f in folds)


def test_folds_start_after_the_indicator_warmup() -> None:
    start = date(2020, 1, 1)
    folds, _ = make_folds(days_from(start, 2000), n_folds=4, warmup_days=400)
    assert folds[0].start >= start + timedelta(days=400)


def test_holdout_can_be_disabled() -> None:
    folds, holdout = make_folds(
        days_from(date(2020, 1, 1), 2000), n_folds=4, holdout=False
    )
    assert holdout is None
    assert len(folds) == 4


def test_too_little_history_is_refused() -> None:
    with pytest.raises(ValueError):
        make_folds(days_from(date(2020, 1, 1), 50), n_folds=4)


# --- the verdict logic ---------------------------------------------------- #


def test_a_variant_winning_every_fold_is_promoted() -> None:
    wf = WalkForward(baseline="baseline")
    for i, label in enumerate(["F1", "F2", "F3", "F4"]):
        wf.results.append(fold_result("baseline", label, 0.0))
        wf.results.append(fold_result("better", label, 0.1))

    v = {x.name: x for x in verdicts(wf)}["better"]
    assert v.folds_won == 4
    assert "EVERY fold" in v.verdict


def test_a_one_fold_wonder_is_called_luck() -> None:
    """The failure mode this module exists to catch: a big win in one period
    carrying a variant that loses everywhere else."""
    wf = WalkForward(baseline="baseline")
    wf.results.append(fold_result("baseline", "F1", 0.00))
    wf.results.append(fold_result("baseline", "F2", 0.00))
    wf.results.append(fold_result("baseline", "F3", 0.00))
    wf.results.append(fold_result("baseline", "F4", 0.00))
    wf.results.append(fold_result("lucky", "F1", 2.00))  # one huge period
    wf.results.append(fold_result("lucky", "F2", -0.05))
    wf.results.append(fold_result("lucky", "F3", -0.05))
    wf.results.append(fold_result("lucky", "F4", -0.05))

    v = {x.name: x for x in verdicts(wf)}["lucky"]
    assert v.delta > 0  # wins on the aggregate
    assert v.folds_won == 1
    assert "NOT consistently" in v.verdict


def test_a_variant_that_does_not_improve_is_rejected() -> None:
    wf = WalkForward(baseline="baseline")
    for label in ["F1", "F2", "F3"]:
        wf.results.append(fold_result("baseline", label, 0.1))
        wf.results.append(fold_result("worse", label, 0.0))
    assert {x.name: x for x in verdicts(wf)}["worse"].verdict == "no improvement"


def test_a_thin_variant_is_not_judged() -> None:
    wf = WalkForward(baseline="baseline")
    for label in ["F1", "F2"]:
        wf.results.append(fold_result("baseline", label, 0.0, trades=60))
        wf.results.append(fold_result("thin", label, 0.5, trades=5))
    assert "too few trades" in {x.name: x for x in verdicts(wf)}["thin"].verdict


def test_the_baseline_is_its_own_reference() -> None:
    wf = WalkForward(baseline="baseline")
    wf.results.append(fold_result("baseline", "F1", 0.1))
    assert {x.name: x for x in verdicts(wf)}["baseline"].verdict == "reference"


# --- end to end ----------------------------------------------------------- #


def test_walkforward_runs_over_a_synthetic_universe() -> None:
    universe, sectors = random_universe(n_symbols=6, n_bars=700, seed=5)
    folds, holdout = make_folds(universe["SPY"].days, n_folds=2, warmup_days=300)

    base = Policy()
    wf = run_walkforward(
        universe,
        sectors,
        {"baseline": base, "shallow": base.with_changes(pullback_favour_shallow=True)},
        folds,
        progress=False,
    )
    wf.holdout = holdout

    assert len(wf.results) == 4  # 2 folds x 2 variants
    assert wf.variants() == ["baseline", "shallow"]

    text = format_walkforward(wf)
    assert "WALK-FORWARD" in text
    assert "HOLDOUT" in text
    assert "Consistency is the test" in text


def test_the_shallow_flag_actually_changes_the_scores() -> None:
    """A variant that silently does nothing would look like a null result."""
    from trading_bot.signals.engine import Indicators, find_setups
    from tests.synthetic import pullback_series

    series = pullback_series()
    ind = Indicators.compute(series)
    i = len(series) - 1

    normal = find_setups(series, ind, i, "X", Policy())
    flipped = find_setups(
        series, ind, i, "X", Policy(pullback_favour_shallow=True)
    )

    assert normal and flipped
    assert normal[0].setup_quality != flipped[0].setup_quality


# --- the shared indicator cache ------------------------------------------- #


def test_sharing_indicators_does_not_change_results() -> None:
    """An optimisation that alters the answer is a bug, not an optimisation."""
    from trading_bot.backtest.engine import BacktestConfig, run_backtest
    from trading_bot.signals.engine import Indicators

    universe, sectors = random_universe(n_symbols=6, n_bars=600, seed=9)
    days = universe["SPY"].days
    cfg = BacktestConfig(start=days[300], end=days[-1], starting_equity=100_000.0)

    fresh = run_backtest(universe, sectors, cfg)
    shared = run_backtest(
        universe,
        sectors,
        cfg,
        indicators={s: Indicators.compute(b) for s, b in universe.items()},
    )
    assert fresh.trades == shared.trades
    assert fresh.curve == shared.curve


def test_breadth_matches_with_and_without_the_cache() -> None:
    from trading_bot.signals.engine import Indicators
    from trading_bot.signals.regime import breadth_of

    universe, _ = random_universe(n_symbols=8, n_bars=400, seed=4)
    indicators = {s: Indicators.compute(b) for s, b in universe.items()}
    for day in universe["SPY"].days[300::40]:
        assert breadth_of(universe, day) == pytest.approx(
            breadth_of(universe, day, indicators)
        )


def test_precomputed_candidates_match_scanning() -> None:
    """Hoisting the scan out of the daily loop is a rearrangement, not a change.

    If these ever diverge, every walk-forward result is measuring something the
    live path would not produce.
    """
    from trading_bot.backtest.engine import precompute_candidates
    from trading_bot.signals.engine import Indicators, scan

    universe, sectors = random_universe(n_symbols=6, n_bars=500, seed=12)
    tradeable = {s: b for s, b in universe.items() if s != "SPY"}
    indicators = {s: Indicators.compute(b) for s, b in tradeable.items()}
    days = universe["SPY"].days[260::11]

    ahead = precompute_candidates(tradeable, indicators, sectors, Policy(), days)
    for day in days:
        live = scan(tradeable, day, sectors, indicators, Policy())
        assert sorted(ahead[day], key=lambda c: c.ticker) == sorted(
            live, key=lambda c: c.ticker
        )


def test_precomputing_does_not_change_backtest_results() -> None:
    from trading_bot.backtest.engine import (
        BacktestConfig,
        precompute_candidates,
        run_backtest,
    )
    from trading_bot.signals.engine import Indicators

    universe, sectors = random_universe(n_symbols=6, n_bars=600, seed=13)
    days = universe["SPY"].days
    cfg = BacktestConfig(start=days[300], end=days[-1], starting_equity=100_000.0)

    tradeable = {s: b for s, b in universe.items() if s != "SPY"}
    indicators = {s: Indicators.compute(b) for s, b in universe.items()}
    span = [d for d in days if cfg.start <= d <= cfg.end]

    plain = run_backtest(universe, sectors, cfg)
    hoisted = run_backtest(
        universe,
        sectors,
        cfg,
        indicators=indicators,
        candidates_by_day=precompute_candidates(
            tradeable, indicators, sectors, Policy(), span
        ),
    )
    assert plain.trades == hoisted.trades
    assert plain.curve == hoisted.curve
