"""Deterministic synthetic price series that trigger (or deliberately miss) each
setup. Used by the setup tests and the backtest tests so neither needs network
access or a cached data file."""

from __future__ import annotations

import random
from datetime import date, timedelta

from trading_bot.data.models import Bar, BarSeries

START = date(2024, 1, 1)


def _bar(day: date, close: float, prev: float, vol: float, wiggle: float) -> Bar:
    high = max(close, prev) + wiggle
    low = min(close, prev) - wiggle
    return Bar(day=day, open=prev, high=high, low=low, close=close, volume=vol)


def series_from_closes(
    symbol: str,
    closes: list[float],
    volumes: list[float] | None = None,
    wiggle_pct: float = 0.004,
) -> BarSeries:
    volumes = volumes or [1_000_000.0] * len(closes)
    bars: list[Bar] = []
    day = START
    prev = closes[0]
    for i, c in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(_bar(day, c, prev, volumes[i], c * wiggle_pct))
        prev = c
        day += timedelta(days=1)
    return BarSeries(symbol, bars)


def uptrend(n: int = 260, start: float = 100.0, slope: float = 0.35) -> list[float]:
    """Steady, low-noise advance -- produces a high ADX."""
    rng = random.Random(11)
    out, level = [], start
    for _ in range(n):
        level += slope + rng.uniform(-slope * 0.25, slope * 0.25)
        out.append(round(level, 2))
    return out


def chop(n: int = 260, start: float = 100.0, amplitude: float = 2.0) -> list[float]:
    rng = random.Random(5)
    return [
        round(start + amplitude * ((i % 4) - 1.5) + rng.uniform(-0.3, 0.3), 2)
        for i in range(n)
    ]


def breakout_series(symbol: str = "BRK") -> BarSeries:
    """Uptrend, a tight consolidation, then a decisive close above the range on
    heavy volume."""
    closes = uptrend(230)
    plateau = closes[-1]
    closes += [round(plateau - 1.0 + (i % 3) * 0.4, 2) for i in range(25)]
    closes.append(round(plateau * 1.05, 2))  # the breakout bar

    volumes = [1_000_000.0] * (len(closes) - 1) + [3_000_000.0]
    return series_from_closes(symbol, closes, volumes)


def pullback_series(symbol: str = "PLB") -> BarSeries:
    """Strong uptrend, then a three-day retracement into the 20 EMA."""
    closes = uptrend(250, slope=0.55)
    peak = closes[-1]
    closes += [
        round(peak * 0.985, 2),
        round(peak * 0.973, 2),
        round(peak * 0.966, 2),
    ]
    return series_from_closes(symbol, closes)


def mean_reversion_series(symbol: str = "MRV") -> BarSeries:
    """Long uptrend above the 200-day, then a sharp two-week flush."""
    closes = uptrend(250, slope=0.5)
    peak = closes[-1]
    closes += [round(peak * (1 - 0.022 * (i + 1)), 2) for i in range(8)]
    return series_from_closes(symbol, closes)


def random_universe(
    n_symbols: int = 10,
    n_bars: int = 600,
    seed: int = 3,
) -> tuple[dict[str, BarSeries], dict[str, str]]:
    """A deterministic pseudo-market: a benchmark plus correlated-ish names.

    Volume varies, otherwise the breakout setup's volume-surge filter can never
    fire and the backtest silently tests only two of the three setups.
    """
    rng = random.Random(seed)

    def walk(bars: int, start: float, drift: float, vol: float) -> list[float]:
        out, level = [], start
        for _ in range(bars):
            level *= 1 + drift + rng.gauss(0, vol)
            out.append(round(max(level, 1.0), 2))
        return out

    def volumes(bars: int) -> list[float]:
        return [
            round(1_000_000 * max(0.2, rng.lognormvariate(0, 0.45)), 0)
            for _ in range(bars)
        ]

    universe = {
        "SPY": series_from_closes("SPY", walk(n_bars, 400.0, 0.0004, 0.008))
    }
    sectors: dict[str, str] = {}
    for k in range(n_symbols):
        symbol = f"S{k:02d}"
        universe[symbol] = series_from_closes(
            symbol,
            walk(n_bars, 40.0 + k * 12, 0.0006, 0.018),
            volumes(n_bars),
        )
        sectors[symbol] = f"Sector{k % 5}"
    return universe, sectors


def flat_series(symbol: str = "FLAT", n: int = 260) -> BarSeries:
    return series_from_closes(symbol, [100.0] * n)


def chop_series(symbol: str = "CHOP") -> BarSeries:
    return series_from_closes(symbol, chop())
