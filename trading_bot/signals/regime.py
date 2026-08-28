"""Market regime.

Feeds `regime_fit` scoring and, more importantly, gives the agent permission to
hold cash. A system that cannot decide to sit out is compulsive, not autonomous.

Long-only, so there is no bear regime: a benchmark below its 200-day average is
classified CHOP, which down-weights breakouts and leaves mean reversion as the
only setup with a decent fit. That is the intended behaviour, not a gap.
"""

from __future__ import annotations

from datetime import date

from ..core.models import MarketContext, Regime
from ..data.models import BarSeries
from .engine import Indicators
from .indicators import realized_volatility

HIGH_VOL_THRESHOLD = 0.30  # annualised
TREND_ADX = 20.0


def classify(
    benchmark: BarSeries,
    day: date,
    ind: Indicators | None = None,
    breadth: float | None = None,
) -> MarketContext:
    i = benchmark.index_asof(day)
    if i is None:
        return MarketContext(as_of=day, regime=Regime.CHOP)

    ind = ind or Indicators.compute(benchmark)
    vol_series = realized_volatility(benchmark.closes, 20)

    close = benchmark[i].close
    sma200 = ind.sma200[i]
    adx = ind.adx14[i]
    vol = vol_series[i]

    if vol is not None and vol > HIGH_VOL_THRESHOLD:
        regime = Regime.HIGH_VOL
    elif sma200 is not None and adx is not None and close > sma200 and adx > TREND_ADX:
        regime = Regime.TREND
    else:
        regime = Regime.CHOP

    return MarketContext(
        as_of=day,
        regime=regime,
        breadth=breadth if breadth is not None else 0.5,
        volatility_pct=round(vol, 4) if vol is not None else 0.15,
    )


def breadth_of(
    universe: dict[str, BarSeries],
    day: date,
    indicators: dict[str, Indicators] | None = None,
) -> float:
    """Fraction of the universe trading above its own 50-day average.

    Pass `indicators` to reuse the precomputed SMA50. Without it this recomputes
    a 50-bar mean for every symbol on every session, which is the difference
    between a backtest that finishes and one that does not once the universe is
    a few hundred names.
    """
    above = total = 0
    for symbol, series in universe.items():
        i = series.index_asof(day)
        if i is None or i < 50:
            continue

        mean = None
        if indicators is not None:
            ind = indicators.get(symbol)
            if ind is not None:
                mean = ind.sma50[i]
        if mean is None:
            window = series.closes[i - 49 : i + 1]
            if len(window) < 50:
                continue
            mean = sum(window) / 50

        total += 1
        if series[i].close > mean:
            above += 1
    return above / total if total else 0.5
