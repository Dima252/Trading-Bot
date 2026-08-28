"""Backtest correctness.

The results themselves are meaningless here -- the fixtures are random walks.
What is being tested is that the machine does not cheat, does not lose money it
never had, and produces the same answer twice.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.synthetic import random_universe

from trading_bot.backtest.engine import BacktestConfig, run_backtest
from trading_bot.backtest.fills import (
    FillModel,
    check_exit,
    check_exit_intraday,
    check_limit_fill,
)
from trading_bot.backtest.metrics import build_report, format_report
from trading_bot.backtest.simulate import simulate_forward
from trading_bot.core import Policy
from trading_bot.data.models import Bar, BarSeries


@pytest.fixture(scope="module")
def market():
    return random_universe(n_symbols=10, n_bars=600, seed=3)


def config_for(universe, first: int = 240, last: int = -1) -> BacktestConfig:
    days = universe["SPY"].days
    return BacktestConfig(
        start=days[first], end=days[last], starting_equity=100_000.0
    )


# --- fill model: the place backtests usually lie ------------------------- #


def test_gap_through_the_stop_fills_at_the_open_not_the_stop() -> None:
    bar = Bar(date(2026, 1, 5), open=90.0, high=92.0, low=88.0, close=91.0, volume=1)
    fill = check_exit(bar, stop=95.0, target=120.0)
    assert fill.price == 90.0  # not 95 -- this is where stop-based sizing breaks
    assert fill.reason == "gap_through_stop"


def test_stop_wins_when_a_bar_touches_both_stop_and_target() -> None:
    """Daily bars can't tell us the intraday order, so assume the bad one."""
    bar = Bar(date(2026, 1, 5), open=100.0, high=121.0, low=94.0, close=110.0, volume=1)
    assert check_exit(bar, stop=95.0, target=120.0).reason == "stop"
    assert check_exit_intraday(bar, stop=95.0, target=120.0).reason == "stop"


def test_intraday_checker_ignores_the_opening_gap() -> None:
    """A position entered during the bar cannot have been gapped out at the open."""
    bar = Bar(date(2026, 1, 5), open=90.0, high=105.0, low=99.0, close=104.0, volume=1)
    assert check_exit(bar, stop=95.0, target=120.0).reason == "gap_through_stop"
    assert check_exit_intraday(bar, stop=95.0, target=120.0) is None


def test_limit_fills_at_the_open_when_it_gaps_below() -> None:
    bar = Bar(date(2026, 1, 5), open=48.0, high=52.0, low=47.0, close=51.0, volume=1)
    assert check_limit_fill(bar, limit=50.0) == 48.0  # better than asked
    bar2 = Bar(date(2026, 1, 5), open=52.0, high=53.0, low=49.5, close=51.0, volume=1)
    assert check_limit_fill(bar2, limit=50.0) == 50.0
    bar3 = Bar(date(2026, 1, 5), open=52.0, high=53.0, low=51.0, close=51.5, volume=1)
    assert check_limit_fill(bar3, limit=50.0) is None


def test_slippage_always_hurts() -> None:
    fm = FillModel(slippage_bps=10.0)
    assert fm.buy(100.0) > 100.0
    assert fm.sell(100.0) < 100.0


# --- forward simulation (the shadow book's engine) ----------------------- #


def test_simulate_forward_detects_the_target() -> None:
    bars = [
        Bar(date(2026, 1, i + 1), 100.0, 101.0, 99.0, 100.0, 1) for i in range(5)
    ]
    bars.append(Bar(date(2026, 1, 6), 100.0, 130.0, 99.5, 129.0, 1))
    out = simulate_forward(BarSeries("X", bars), 0, entry=100.0, stop=95.0, target=120.0)
    assert out.outcome == "target"
    assert out.r_multiple == pytest.approx(4.0)


def test_simulate_forward_times_out() -> None:
    bars = [
        Bar(date(2026, 1, i + 1), 100.0, 101.0, 99.0, 100.0, 1) for i in range(20)
    ]
    out = simulate_forward(
        BarSeries("X", bars), 0, entry=100.0, stop=95.0, target=120.0, max_days=10
    )
    assert out.outcome == "timeout"


def test_simulate_forward_rejects_an_inverted_stop() -> None:
    bars = [Bar(date(2026, 1, 1), 100.0, 101.0, 99.0, 100.0, 1)] * 1
    assert simulate_forward(BarSeries("X", bars), 0, 100.0, 105.0, 120.0) is None


# --- engine invariants --------------------------------------------------- #


def test_backtest_runs_and_produces_records(market) -> None:
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    assert result.trades, "expected at least one trade on this fixture"
    assert result.curve
    assert result.shadow


def test_backtest_is_deterministic(market) -> None:
    universe, sectors = market
    cfg = config_for(universe)
    a = run_backtest(universe, sectors, cfg)
    b = run_backtest(universe, sectors, cfg)
    assert a.trades == b.trades
    assert a.curve == b.curve


def test_cash_is_never_negative(market) -> None:
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    assert all(point.cash >= -1e-6 for point in result.curve)


def test_equity_reconciles_with_realised_pnl(market) -> None:
    """Final equity must equal starting equity plus the sum of every trade's P&L
    -- if it doesn't, money is being created or destroyed somewhere."""
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    expected = result.starting_equity + sum(t.pnl for t in result.trades)
    assert result.final_equity == pytest.approx(expected, abs=1.0)


def test_every_trade_is_internally_consistent(market) -> None:
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))

    # MFE/MAE measure the price path; realized_r includes slippage, so it can
    # land marginally outside the path range. The tolerance covers that -- it is
    # still tight enough to catch a genuine ordering bug.
    SLIPPAGE_R = 0.05

    for t in result.trades:
        assert t.qty > 0
        assert t.exit_day >= t.entry_day
        assert t.initial_stop < t.entry_price
        assert t.mae_r <= 1e-9
        assert t.mfe_r >= t.mae_r
        assert t.mfe_r >= t.realized_r - SLIPPAGE_R
        assert t.mae_r <= t.realized_r + SLIPPAGE_R


def test_risk_limits_hold_across_the_whole_run(market) -> None:
    universe, sectors = market
    policy = Policy()
    result = run_backtest(universe, sectors, config_for(universe), policy)
    for point in result.curve:
        assert point.heat_pct <= policy.max_portfolio_heat + 0.005


# --- the lookahead guarantee, at the engine level ------------------------ #


def test_extending_the_end_date_does_not_change_earlier_days(market) -> None:
    """The strongest anti-lookahead test available: a run that ends on day D must
    produce exactly the equity path of a longer run, up to day D."""
    universe, sectors = market
    days = universe["SPY"].days

    short = run_backtest(universe, sectors, config_for(universe, 240, 450))
    long = run_backtest(universe, sectors, config_for(universe, 240, 560))

    cutoff = days[450]
    long_prefix = [p for p in long.curve if p.day <= cutoff]
    assert short.curve == long_prefix


# --- reporting ----------------------------------------------------------- #


def test_report_renders(market) -> None:
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    text = format_report(build_report(result), result.universe_size)
    assert "BACKTEST REPORT" in text
    assert "survivorship bias" in text


def test_shadow_book_records_why_each_candidate_was_declined(market) -> None:
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    reasons = {r.not_taken_reason for r in result.shadow}
    assert reasons  # every declined candidate carries a reason
    assert all(r.hypothetical_r is not None for r in result.shadow[:50])


# --- the gap-below-stop pathology ---------------------------------------- #


def test_a_limit_that_gaps_in_below_its_own_stop_is_never_taken(market) -> None:
    """A bracket whose entry fills below its stop would be flushed on arrival.

    Without this guard the exit checker "sells at the stop" while the market sits
    far below it, manufacturing profit out of a gap DOWN -- and the R denominator
    goes negative, clamps to 1e-9, and blows every statistic to nine figures.
    """
    from trading_bot.backtest.engine import Backtest, BacktestConfig
    from trading_bot.core.models import Action, ActionKind, EntryType, SetupType

    universe, sectors = market
    bt = Backtest(universe, sectors, config_for(universe), Policy())
    entry = Action(
        kind=ActionKind.OPEN,
        ticker="S00",
        qty=10,
        limit=100.0,
        stop=95.0,
        target=130.0,
        entry_type=EntryType.RESTING_LIMIT,
        setup_type=SetupType.PULLBACK,
    )

    bt._open(entry, fill_price=80.0, day=date(2026, 3, 2))  # gapped in below 95

    assert bt.positions == {}
    assert bt.aborted_fills == 1
    assert bt.cash == 100_000.0  # no capital committed


def test_r_is_denominated_by_planned_risk_not_the_fill(market) -> None:
    from trading_bot.backtest.engine import Backtest, BacktestConfig
    from trading_bot.core.models import Action, ActionKind, EntryType, SetupType

    universe, sectors = market
    bt = Backtest(universe, sectors, config_for(universe), Policy())
    entry = Action(
        kind=ActionKind.OPEN,
        ticker="S00",
        qty=10,
        limit=100.0,
        stop=95.0,
        target=115.0,
        entry_type=EntryType.RESTING_LIMIT,
        setup_type=SetupType.PULLBACK,
    )

    # a favourable fill, well above the stop
    bt._open(entry, fill_price=97.0, day=date(2026, 3, 2))
    position = bt.positions["S00"]

    # risk stays the $5 we budgeted, not the $2 the fill happened to produce
    assert position.initial_risk == pytest.approx(5.0)
    assert position.to_core(date(2026, 3, 2)).initial_risk_per_share == pytest.approx(5.0)


def test_no_trade_reports_an_absurd_r(market) -> None:
    """The regression guard: a single bad denominator poisons every statistic."""
    universe, sectors = market
    result = run_backtest(universe, sectors, config_for(universe))
    for trade in result.trades:
        assert -20.0 < trade.realized_r < 20.0
        assert -20.0 < trade.mfe_r < 20.0
        assert -20.0 < trade.mae_r < 20.0


# --- idle cash must earn the risk-free rate ------------------------------- #


def rate_series(days, annual_pct: float) -> BarSeries:
    return BarSeries(
        "^IRX",
        [
            Bar(d, annual_pct, annual_pct, annual_pct, annual_pct, 0.0)
            for d in days
        ],
    )


def test_idle_cash_earns_interest(market) -> None:
    """A backtest paying 0% on cash badly understates any strategy that is only
    deployed part of the time -- which is what a defensive profile is."""
    universe, sectors = market
    days = universe["SPY"].days
    with_rate = dict(universe)
    with_rate["^IRX"] = rate_series(days, 5.0)

    cfg = config_for(universe)
    flat = run_backtest(universe, sectors, cfg)
    paid = run_backtest(
        with_rate,
        sectors,
        BacktestConfig(
            start=cfg.start, end=cfg.end, starting_equity=cfg.starting_equity,
            cash_rate_symbol="^IRX",
        ),
    )
    assert paid.final_equity > flat.final_equity


def test_no_rate_series_means_no_interest(market) -> None:
    universe, sectors = market
    a = run_backtest(universe, sectors, config_for(universe))
    cfg = config_for(universe)
    b = run_backtest(
        universe,
        sectors,
        BacktestConfig(
            start=cfg.start, end=cfg.end, starting_equity=cfg.starting_equity,
            cash_rate_symbol="^IRX",  # named but absent from the universe
        ),
    )
    assert a.final_equity == pytest.approx(b.final_equity)


def test_a_rate_series_is_never_traded(market) -> None:
    """An index or rate series is an input, never a position."""
    from trading_bot.backtest.engine import Backtest

    universe, sectors = market
    with_rate = dict(universe)
    with_rate["^IRX"] = rate_series(universe["SPY"].days, 5.0)

    bt = Backtest(with_rate, sectors, config_for(universe), Policy())
    assert "^IRX" not in bt.tradeable
    assert "SPY" not in bt.tradeable


def test_a_zero_rate_pays_nothing(market) -> None:
    from trading_bot.backtest.engine import Backtest

    universe, sectors = market
    with_rate = dict(universe)
    with_rate["^IRX"] = rate_series(universe["SPY"].days, 0.0)
    cfg = config_for(universe)
    bt = Backtest(
        with_rate,
        sectors,
        BacktestConfig(
            start=cfg.start, end=cfg.end, starting_equity=cfg.starting_equity,
            cash_rate_symbol="^IRX",
        ),
        Policy(),
    )
    bt.run()
    assert bt.interest_earned == 0.0
