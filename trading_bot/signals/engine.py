"""Signal engine.

`Indicators.compute()` runs once per symbol and returns aligned series; setups
then read by bar index. That keeps a full backtest linear in the number of bars
instead of quadratic, and it is the same code path live -- which is the whole
point (README section 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..core.models import Candidate
from ..core.policy import Policy
from ..data.models import BarSeries
from . import indicators as ta

# Longest warm-up any indicator needs before it reports a value.
MIN_HISTORY = 220


@dataclass(frozen=True)
class Indicators:
    sma20: list[float | None]
    sma50: list[float | None]
    sma200: list[float | None]
    ema20: list[float | None]
    rsi14: list[float | None]
    atr14: list[float | None]
    adx14: list[float | None]
    stdev20: list[float | None]
    vol_sma20: list[float | None]
    high20_prior: list[float | None]
    low10_prior: list[float | None]
    low20_prior: list[float | None]

    @classmethod
    def compute(cls, series: BarSeries) -> Indicators:
        closes = series.closes
        return cls(
            sma20=ta.sma(closes, 20),
            sma50=ta.sma(closes, 50),
            sma200=ta.sma(closes, 200),
            ema20=ta.ema(closes, 20),
            rsi14=ta.rsi(closes, 14),
            atr14=ta.atr(series.bars, 14),
            adx14=ta.adx(series.bars, 14),
            stdev20=ta.stdev(closes, 20),
            vol_sma20=ta.sma(series.volumes, 20),
            high20_prior=ta.rolling_max_prior(series.highs, 20),
            low10_prior=ta.rolling_min_prior(series.lows, 10),
            low20_prior=ta.rolling_min_prior(series.lows, 20),
        )


def find_setups(
    series: BarSeries,
    ind: Indicators,
    i: int,
    sector: str = "UNKNOWN",
    policy: Policy | None = None,
) -> list[Candidate]:
    """Every setup that fires on bar `i`. Nothing here may read past `i`."""
    from ..core.policy import Policy
    from .setups import breakout, mean_reversion, pullback

    policy = policy or Policy()
    found = []
    for module in (breakout, pullback, mean_reversion):
        cand = module.detect(series, ind, i, sector, policy)
        if cand is not None and cand.is_valid:
            found.append(cand)
    return found


def scan(
    universe: dict[str, BarSeries],
    day: date,
    sectors: dict[str, str] | None = None,
    indicator_cache: dict[str, Indicators] | None = None,
    policy: Policy | None = None,
) -> list[Candidate]:
    """Run every setup across every symbol for one day.

    `indicator_cache` lets a backtest compute indicators once per symbol for the
    whole history and reuse them on every bar.
    """
    sectors = sectors or {}
    out: list[Candidate] = []

    for symbol, series in universe.items():
        i = series.index_of(day)
        if i is None or i < MIN_HISTORY:
            continue
        ind = (
            indicator_cache.get(symbol)
            if indicator_cache is not None
            else Indicators.compute(series)
        )
        if ind is None:
            ind = Indicators.compute(series)
            if indicator_cache is not None:
                indicator_cache[symbol] = ind
        out.extend(
            find_setups(series, ind, i, sectors.get(symbol, "UNKNOWN"), policy)
        )

    return out
