"""Sleeve C: multi-asset time-series momentum, volatility targeted.

Every axis of this differs from sleeve A, which is the point. Sleeve A ranks 500
stocks against each other and buys the best few; this asks each of ~25 ETFs a
question about its *own* history and sizes the answer by that asset's own
volatility. The two share the data layer and nothing else.

Evidence it is built on:

* Moskowitz, Ooi & Pedersen (2012) found positive time-series momentum in all 58
  liquid futures markets they tested, 52 significant at 5%, with persistence
  over 1-12 months.
* A 2015-2025 study of horizon structure found the 500-day and 250-day lookbacks
  clearly best (Sharpe 0.47 and 0.42), and the **125-day band redundant** -- 0.84
  correlated with 250-day and worst on return-to-drawdown. So the signal blends
  short and long and deliberately skips the middle.
* Those recent Sharpes are far below the 1.2+ of the original paper. Trend
  following spent 2010-2019 in a drawdown. This is built expecting ~0.4, not the
  headline number.

Why it complements sleeve A specifically: sleeve A is capped and selective, and
in the 2009-13 bull it made 54.8% while the index made 141%. Time-series
momentum is long in uptrends -- it participates in exactly the regime sleeve A
concedes, and steps aside in the one sleeve A already handles well.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from statistics import stdev

from ..data.models import BarSeries

# Sessions. The 125-day band is missing on purpose -- see the module docstring.
LOOKBACKS: tuple[int, ...] = (20, 250, 500)

# Sessions used to estimate each asset's volatility for position sizing.
VOL_WINDOW = 60

# Annualised volatility each *position* is scaled toward. The portfolio target
# is lower, since positions rarely all fire at once and are not fully correlated.
TARGET_VOL = 0.15

# No position may exceed this share of equity however quiet the asset. Without
# it, a low-volatility bond ETF asks for many times the account.
MAX_WEIGHT = 0.25

# Total gross exposure. Above 1.0 this would need margin, which for cash
# equities costs far more than the futures financing the published results
# assume, so the sleeve stays unlevered.
MAX_GROSS = 1.0

TRADING_DAYS = 252


@dataclass(frozen=True)
class AssetSignal:
    """One asset's verdict on one day, with the parts kept separate.

    The blend is what gets traded, but the individual horizons are what make a
    disappointing result diagnosable rather than merely disappointing.
    """

    symbol: str
    strength: float                       # -1.0 .. +1.0, the blended signal
    volatility: float                     # annualised, from VOL_WINDOW
    weight: float = 0.0                   # target share of equity, post-sizing
    by_lookback: dict[int, float] = field(default_factory=dict)


def realised_volatility(series: BarSeries, i: int, window: int = VOL_WINDOW) -> float:
    """Annualised standard deviation of daily returns ending at bar `i`."""
    if i < window:
        return 0.0
    closes = [b.close for b in series.bars[i - window : i + 1]]
    rets = [(b - a) / a for a, b in itertools.pairwise(closes) if a > 0]
    if len(rets) < 2:
        return 0.0
    return stdev(rets) * (TRADING_DAYS ** 0.5)


def momentum_sign(series: BarSeries, i: int, lookback: int) -> float | None:
    """+1 if the asset is up over the window, -1 if down, None if too short.

    The sign, not the magnitude. Scaling by the size of a past move loads the
    position on whatever moved most recently, which is the opposite of what
    volatility targeting is for.
    """
    if i < lookback:
        return None
    then, now = series[i - lookback].close, series[i].close
    if then <= 0:
        return None
    return 1.0 if now > then else -1.0


def signal_for(series: BarSeries, i: int) -> AssetSignal | None:
    """Blend the horizons for one asset on one bar."""
    votes: dict[int, float] = {}
    for lookback in LOOKBACKS:
        vote = momentum_sign(series, i, lookback)
        if vote is None:
            return None            # not enough history for the full blend
        votes[lookback] = vote

    vol = realised_volatility(series, i)
    return AssetSignal(
        symbol=series.symbol,
        strength=sum(votes.values()) / len(votes),
        volatility=vol,
        by_lookback=votes,
    )


def target_weights(
    universe: dict[str, BarSeries],
    day: date,
    long_only: bool = True,
    target_vol: float = TARGET_VOL,
    max_weight: float = MAX_WEIGHT,
    max_gross: float = MAX_GROSS,
) -> dict[str, AssetSignal]:
    """What the sleeve wants to hold, as a fraction of equity per symbol.

    Sizing is inverse to each asset's own volatility, which is the mechanism
    behind the published Sharpe ratios: a quiet bond ETF and a violent commodity
    ETF contribute comparable risk rather than comparable dollars.
    """
    signals: dict[str, AssetSignal] = {}

    for symbol, series in universe.items():
        i = series.index_of(day)
        if i is None:
            continue
        sig = signal_for(series, i)
        if sig is None or sig.volatility <= 0:
            continue

        strength = max(sig.strength, 0.0) if long_only else sig.strength
        if strength == 0.0:
            continue

        raw = strength * (target_vol / sig.volatility)
        weight = max(-max_weight, min(max_weight, raw))
        signals[symbol] = AssetSignal(
            symbol=sig.symbol,
            strength=sig.strength,
            volatility=sig.volatility,
            weight=weight,
            by_lookback=sig.by_lookback,
        )

    # Scale back if the book would exceed its gross budget. Scaling preserves
    # the relative sizing the volatility targeting produced; truncating the
    # tail would quietly bias the sleeve toward whatever sorted first.
    gross = sum(abs(s.weight) for s in signals.values())
    if gross > max_gross and gross > 0:
        scale = max_gross / gross
        signals = {
            sym: AssetSignal(
                symbol=s.symbol,
                strength=s.strength,
                volatility=s.volatility,
                weight=s.weight * scale,
                by_lookback=s.by_lookback,
            )
            for sym, s in signals.items()
        }

    return signals
