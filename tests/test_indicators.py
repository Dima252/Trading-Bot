"""Indicators are load-bearing: a wrong one silently changes every decision."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_bot.data.models import Bar
from trading_bot.signals.indicators import (
    adx,
    atr,
    ema,
    realized_volatility,
    rolling_max_prior,
    rolling_min_prior,
    rsi,
    sma,
    stdev,
    true_range,
)


def bars_from(prices: list[float], spread: float = 1.0) -> list[Bar]:
    day = date(2026, 1, 1)
    return [
        Bar(day + timedelta(days=i), p, p + spread, p - spread, p, 1_000_000.0)
        for i, p in enumerate(prices)
    ]


# --- warm-up alignment -------------------------------------------------- #


def test_series_are_aligned_and_none_before_warmup() -> None:
    values = [float(i) for i in range(10)]
    out = sma(values, 4)
    assert len(out) == len(values)
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(1.5)  # (0+1+2+3)/4


def test_sma_matches_hand_calculation() -> None:
    assert sma([2.0, 4.0, 6.0, 8.0], 2)[-1] == pytest.approx(7.0)
    assert sma([2.0, 4.0, 6.0, 8.0], 4)[-1] == pytest.approx(5.0)


def test_ema_seeds_on_the_simple_average() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema(values, 3)
    assert out[2] == pytest.approx(2.0)  # seed = (1+2+3)/3
    # then 4 * 0.5 + 2 * 0.5 = 3.0
    assert out[3] == pytest.approx(3.0)


def test_stdev_of_a_flat_series_is_zero() -> None:
    assert stdev([5.0] * 10, 5)[-1] == pytest.approx(0.0)


# --- the lookahead trap ------------------------------------------------- #


def test_rolling_max_prior_excludes_the_current_bar() -> None:
    """The single most common lookahead bug in breakout code."""
    values = [1.0, 2.0, 3.0, 99.0]
    out = rolling_max_prior(values, 3)
    assert out[3] == pytest.approx(3.0)  # not 99
    assert out[:3] == [None, None, None]


def test_rolling_min_prior_excludes_the_current_bar() -> None:
    values = [10.0, 9.0, 8.0, 1.0]
    assert rolling_min_prior(values, 3)[3] == pytest.approx(8.0)


def test_a_breakout_close_is_strictly_above_its_prior_high() -> None:
    closes = [10.0] * 20 + [12.0]
    highs = rolling_max_prior(closes, 20)
    assert closes[20] > highs[20]


# --- true range and ATR -------------------------------------------------- #


def test_true_range_uses_the_previous_close_on_a_gap() -> None:
    bars = [
        Bar(date(2026, 1, 1), 10.0, 10.5, 9.5, 10.0, 1.0),
        Bar(date(2026, 1, 2), 20.0, 21.0, 19.5, 20.0, 1.0),  # gap up
    ]
    # high-low is 1.5, but high - prior close is 11.0
    assert true_range(bars)[1] == pytest.approx(11.0)


def test_atr_seed_is_the_mean_of_the_first_n_true_ranges() -> None:
    bars = bars_from([100.0] * 20, spread=1.0)
    out = atr(bars, 14)
    assert out[0] is None
    assert out[14] == pytest.approx(2.0)  # every TR is high-low = 2.0


def test_atr_rises_with_volatility() -> None:
    calm = atr(bars_from([100.0] * 40, spread=0.5), 14)[-1]
    wild = atr(bars_from([100.0] * 40, spread=5.0), 14)[-1]
    assert wild > calm


# --- RSI ----------------------------------------------------------------- #


def test_rsi_is_bounded() -> None:
    import random

    random.seed(7)
    closes = [100.0]
    for _ in range(200):
        closes.append(max(1.0, closes[-1] * (1 + random.uniform(-0.03, 0.03))))
    for v in rsi(closes, 14):
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_rsi_pins_at_100_when_nothing_falls() -> None:
    closes = [100.0 + i for i in range(40)]
    assert rsi(closes, 14)[-1] == pytest.approx(100.0)


def test_rsi_pins_near_zero_when_nothing_rises() -> None:
    closes = [200.0 - i for i in range(40)]
    assert rsi(closes, 14)[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral_or_undefined() -> None:
    out = rsi([100.0] * 40, 14)[-1]
    # no gains and no losses -> the 0/0 case resolves to the 100 branch
    assert out is not None


# --- ADX ------------------------------------------------------------------ #


def test_adx_is_high_in_a_clean_trend() -> None:
    closes = [100.0 + 2 * i for i in range(80)]
    out = adx(bars_from(closes), 14)[-1]
    assert out is not None and out > 40


def test_adx_is_low_in_chop() -> None:
    closes = [100.0 + (2.0 if i % 2 else -2.0) for i in range(80)]
    out = adx(bars_from(closes), 14)[-1]
    assert out is not None and out < 25


def test_adx_is_bounded_and_needs_warmup() -> None:
    bars = bars_from([100.0 + i * 0.5 for i in range(80)])
    out = adx(bars, 14)
    assert out[10] is None
    for v in out:
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_adx_returns_all_none_when_history_is_too_short() -> None:
    assert all(v is None for v in adx(bars_from([100.0] * 10), 14))


# --- volatility ------------------------------------------------------------ #


def test_realized_volatility_rises_with_noise() -> None:
    calm = [100.0 + 0.01 * i for i in range(80)]
    noisy = [100.0 + (5.0 if i % 2 else -5.0) for i in range(80)]
    assert realized_volatility(noisy, 20)[-1] > realized_volatility(calm, 20)[-1]
