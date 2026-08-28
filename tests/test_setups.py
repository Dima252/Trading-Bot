"""Setup detection, and the lookahead guarantee that makes backtests meaningful."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.synthetic import (
    breakout_series,
    chop_series,
    flat_series,
    mean_reversion_series,
    pullback_series,
    series_from_closes,
    uptrend,
)

from trading_bot.core.models import SetupType
from trading_bot.data.models import BarSeries
from trading_bot.signals.engine import Indicators, find_setups


def last_setups(series: BarSeries, sector: str = "Technology"):
    ind = Indicators.compute(series)
    return find_setups(series, ind, len(series) - 1, sector)


def only(series: BarSeries) -> object:
    found = last_setups(series)
    assert len(found) == 1, [c.setup_type.value for c in found]
    return found[0]


# --- each setup fires on its own shape ---------------------------------- #


def test_breakout_fires(monkeypatch) -> None:
    c = only(breakout_series())
    assert c.setup_type is SetupType.BREAKOUT
    assert c.features["adx"] > 25
    assert c.features["volume_ratio"] >= 1.5


def test_pullback_fires() -> None:
    c = only(pullback_series())
    assert c.setup_type is SetupType.PULLBACK
    assert 35 <= c.features["rsi"] <= 60


def test_mean_reversion_fires() -> None:
    c = only(mean_reversion_series())
    assert c.setup_type is SetupType.MEAN_REVERSION
    assert c.features["rsi"] < 30
    # the target is the mean, not a fixed R multiple
    assert c.target == pytest.approx(c.features["sma20"])


# --- and stays quiet otherwise ------------------------------------------- #


def test_nothing_fires_on_a_flat_tape() -> None:
    assert last_setups(flat_series()) == []


def test_nothing_fires_in_chop() -> None:
    assert last_setups(chop_series()) == []


def test_breakout_needs_volume_confirmation() -> None:
    s = breakout_series()
    quiet = BarSeries(s.symbol, s.bars[:-1] + [replace(s.bars[-1], volume=900_000.0)])
    assert last_setups(quiet) == []


def test_breakout_needs_a_close_above_the_prior_high() -> None:
    s = breakout_series()
    ind = Indicators.compute(s)
    i = len(s) - 1
    below = s[i].high - (s[i].high - ind.high20_prior[i]) - 0.5
    weak = BarSeries(s.symbol, s.bars[:-1] + [replace(s.bars[-1], close=below)])
    assert not [
        c for c in last_setups(weak) if c.setup_type is SetupType.BREAKOUT
    ]


# --- level sanity -------------------------------------------------------- #


@pytest.mark.parametrize(
    "factory", [breakout_series, pullback_series, mean_reversion_series]
)
def test_levels_are_ordered_and_stops_are_sane(factory) -> None:
    c = only(factory())
    assert c.stop < c.entry < c.target
    assert c.is_valid
    # never inside one ATR of noise, never further than three
    assert 1.0 <= (c.entry - c.stop) / c.atr <= 3.01
    assert c.reward_risk >= 1.0
    assert 40.0 <= c.setup_quality <= 100.0


# --- the guarantee that makes a backtest worth running ------------------- #


@pytest.mark.parametrize(
    "factory", [breakout_series, pullback_series, mean_reversion_series]
)
def test_no_lookahead_truncating_the_future_changes_nothing(factory) -> None:
    """Detection at bar i must be identical whether or not bars after i exist.

    If this passes, no indicator and no setup can be reading the future -- which
    is the difference between a backtest and a fiction.
    """
    full = factory()
    i = len(full) - 1

    from_full = find_setups(full, Indicators.compute(full), i, "Technology")

    truncated = BarSeries(full.symbol, full.bars[: i + 1])
    from_truncated = find_setups(
        truncated, Indicators.compute(truncated), i, "Technology"
    )

    assert from_full == from_truncated


def test_no_lookahead_across_many_bars() -> None:
    """Same guarantee, swept over a long stretch rather than one bar."""
    full = series_from_closes("SWEEP", uptrend(300, slope=0.4))
    ind_full = Indicators.compute(full)

    for i in range(220, len(full), 17):
        truncated = BarSeries(full.symbol, full.bars[: i + 1])
        assert find_setups(full, ind_full, i, "X") == find_setups(
            truncated, Indicators.compute(truncated), i, "X"
        )


# --- pullback quality weights are policy, so they can be validated -------- #


def test_pullback_weights_shift_the_quality_score() -> None:
    from trading_bot.core.policy import Policy

    series = pullback_series()
    ind = Indicators.compute(series)
    i = len(series) - 1

    even = find_setups(series, ind, i, "X", Policy())[0]
    rsi_heavy = find_setups(
        series, ind, i, "X",
        Policy(pullback_w_trend=0.2, pullback_w_reset=0.5, pullback_w_depth=0.3),
    )[0]

    assert even.setup_quality != rsi_heavy.setup_quality
    assert 40.0 <= rsi_heavy.setup_quality <= 100.0


def test_pullback_weights_are_normalised() -> None:
    """Weights that do not sum to 1 must not push quality outside its range."""
    from trading_bot.core.policy import Policy

    series = pullback_series()
    ind = Indicators.compute(series)
    i = len(series) - 1

    c = find_setups(
        series, ind, i, "X",
        Policy(pullback_w_trend=2.0, pullback_w_reset=5.0, pullback_w_depth=3.0),
    )[0]
    assert 40.0 <= c.setup_quality <= 100.0
