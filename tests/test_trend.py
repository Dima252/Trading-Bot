"""Sleeve C: multi-asset time-series momentum.

The properties that matter are the ones that would silently flatter a backtest:
no lookahead, volatility sizing that actually inverts with volatility, a gross
budget that cannot be exceeded, and costs charged on every weight change rather
than only on entries.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_bot.data.models import Bar, BarSeries
from trading_bot.strategies.trend import (
    LOOKBACKS,
    MAX_WEIGHT,
    momentum_sign,
    realised_volatility,
    signal_for,
    target_weights,
)
from trading_bot.strategies.trend_backtest import (
    monthly_returns,
    run_trend,
    summarise,
)

DAY = date(2010, 1, 4)


def ramp(symbol: str, n: int, start: float = 100.0, step: float = 0.1) -> BarSeries:
    """A steadily rising (or falling) series."""
    bars = [
        Bar(DAY + timedelta(days=i), start + i * step, start + i * step + 0.5,
            start + i * step - 0.5, start + i * step, 1_000_000.0)
        for i in range(n)
    ]
    return BarSeries(symbol, bars)


def noisy(symbol: str, n: int, amplitude: float) -> BarSeries:
    """Flat on average, oscillating by `amplitude` each session."""
    bars = []
    for i in range(n):
        close = 100.0 + (amplitude if i % 2 else -amplitude)
        bars.append(
            Bar(DAY + timedelta(days=i), close, close + 0.1, close - 0.1,
                close, 1_000_000.0)
        )
    return BarSeries(symbol, bars)


# --- signals ---------------------------------------------------------------- #


def test_momentum_reads_the_sign_not_the_magnitude() -> None:
    """Scaling by the size of a past move loads the book onto whatever moved
    most recently, which is what volatility targeting exists to prevent."""
    up = ramp("UP", 600, step=0.1)
    steep = ramp("STEEP", 600, step=5.0)

    assert momentum_sign(up, 599, 250) == momentum_sign(steep, 599, 250) == 1.0


def test_a_falling_series_votes_down() -> None:
    down = ramp("DOWN", 600, start=200.0, step=-0.1)
    assert momentum_sign(down, 599, 250) == -1.0


def test_the_signal_cannot_see_the_future() -> None:
    """The invariant the whole repo holds itself to: the verdict at bar i is
    identical whether or not bars after i exist."""
    full = ramp("AAA", 800)
    truncated = BarSeries("AAA", full.bars[:601])

    assert signal_for(full, 600).strength == signal_for(truncated, 600).strength
    assert realised_volatility(full, 600) == realised_volatility(truncated, 600)


def test_a_short_series_produces_no_signal_rather_than_a_partial_one() -> None:
    """Blending three horizons means all three must exist. Falling back to the
    ones that fit would silently change the strategy for young assets."""
    assert signal_for(ramp("NEW", 300), 299) is None
    assert signal_for(ramp("OLD", 600), 599) is not None


def test_the_middle_horizon_is_deliberately_absent() -> None:
    """125 days measured worst on both return and drawdown and correlated 0.84
    with the 250-day band. Its absence is a finding, not an oversight."""
    assert 125 not in LOOKBACKS
    assert LOOKBACKS == (20, 250, 500)


# --- volatility ------------------------------------------------------------- #


def test_volatility_is_annualised() -> None:
    """`amplitude=1.0` alternates the close between 101 and 99, so the daily
    move is ~2% -- not 1%. Annualised that is 0.02 * sqrt(252) ~= 0.317."""
    series = noisy("OSC", 200, amplitude=1.0)
    assert realised_volatility(series, 199) == pytest.approx(0.317, abs=0.02)


def test_volatility_scales_with_the_size_of_the_swings() -> None:
    calm = realised_volatility(noisy("CALM", 200, amplitude=0.5), 199)
    wild = realised_volatility(noisy("WILD", 200, amplitude=2.0), 199)
    assert wild > calm * 3


def test_a_flat_series_has_no_volatility() -> None:
    flat = BarSeries("FLAT", [
        Bar(DAY + timedelta(days=i), 100.0, 100.0, 100.0, 100.0, 1e6)
        for i in range(200)
    ])
    assert realised_volatility(flat, 199) == 0.0


# --- sizing ----------------------------------------------------------------- #


def test_a_quieter_asset_gets_more_weight() -> None:
    """The mechanism behind the published Sharpe ratios: a calm bond ETF and a
    violent commodity ETF should contribute comparable RISK, not comparable
    dollars."""
    calm = ramp("CALM", 600, step=0.05)
    wild = BarSeries("WILD", [
        Bar(b.day, b.close, b.close, b.close,
            b.close * (1.05 if i % 2 else 0.95), 1e6)
        for i, b in enumerate(ramp("WILD", 600, step=0.05).bars)
    ])

    weights = target_weights({"CALM": calm, "WILD": wild}, calm[599].day)
    if "WILD" in weights and "CALM" in weights:
        assert weights["CALM"].weight > weights["WILD"].weight


def test_no_single_position_exceeds_the_concentration_cap() -> None:
    """Without a cap, a very quiet asset asks for many times the account."""
    calm = ramp("CALM", 600, step=0.001)
    weights = target_weights({"CALM": calm}, calm[599].day)
    assert all(abs(s.weight) <= MAX_WEIGHT + 1e-9 for s in weights.values())


def test_gross_exposure_stays_within_budget() -> None:
    """The sleeve is unlevered: cash-equity margin costs far more than the
    futures financing the published results assume."""
    uni = {f"S{i}": ramp(f"S{i}", 600, step=0.01 + i * 0.001) for i in range(20)}
    weights = target_weights(uni, ramp("S0", 600)[599].day)
    assert sum(abs(s.weight) for s in weights.values()) <= 1.0 + 1e-9


def test_long_only_declines_rather_than_shorting() -> None:
    down = ramp("DOWN", 600, start=200.0, step=-0.1)
    assert target_weights({"DOWN": down}, down[599].day, long_only=True) == {}
    shorts = target_weights({"DOWN": down}, down[599].day, long_only=False)
    assert shorts["DOWN"].weight < 0


# --- the backtest ----------------------------------------------------------- #


def test_costs_are_charged_on_every_weight_change() -> None:
    """Not only on entries. A sleeve that rebalances weekly pays on the way
    down as well as up, and ignoring that is how turnover disappears."""
    uni = {"UP": ramp("UP", 700)}
    cal = uni["UP"].days

    cheap = run_trend(uni, cal, cal[550], cal[699], cost_bps=0.0)
    dear = run_trend(uni, cal, cal[550], cal[699], cost_bps=100.0)

    assert dear.final_equity < cheap.final_equity
    assert dear.total_costs > cheap.total_costs == 0.0


def test_idle_cash_earns_the_prevailing_rate() -> None:
    """A sleeve sitting 40% in cash looks far worse than it was if the cash
    earns nothing -- the same correction that was worth ~2pp to sleeve A."""
    uni = {"DOWN": ramp("DOWN", 700, start=300.0, step=-0.1)}
    cal = uni["DOWN"].days
    rates = BarSeries("^IRX", [Bar(d, 5.0, 5.0, 5.0, 5.0, 0.0) for d in cal])

    idle = run_trend(uni, cal, cal[550], cal[699])
    paid = run_trend(uni, cal, cal[550], cal[699], cash_rates=rates)

    assert paid.final_equity > idle.final_equity


def test_monthly_returns_are_month_over_month() -> None:
    days = [date(2020, 1, 31), date(2020, 2, 28), date(2020, 3, 31)]
    monthly = monthly_returns(days, [100.0, 110.0, 99.0])

    assert monthly["2020-02"] == pytest.approx(0.10)
    assert monthly["2020-03"] == pytest.approx(-0.10)


def test_summary_of_an_empty_run_does_not_explode() -> None:
    assert summarise(run_trend({}, [], DAY, DAY)) == {}
