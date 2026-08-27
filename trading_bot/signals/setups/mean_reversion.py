"""Mean reversion: stretched 2 sigma below the 20-day mean, structure intact.

Entered on a RESTING LIMIT. The target is the mean itself rather than a fixed
R multiple -- this setup is a snap-back, not a trend ride, and holding for 3R
turns a good win rate into a bad one.
"""

from __future__ import annotations

from ...core.models import Candidate, EntryType, SetupType
from ...data.models import BarSeries

SIGMA = 2.0
RSI_MAX = 30.0
STOP_ATR = 2.0
MIN_REWARD_RISK = 1.0
MAX_STOP_PCT = 0.12


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(
    series: BarSeries, ind, i: int, sector: str, policy=None
) -> Candidate | None:
    bar = series[i]
    sma20 = ind.sma20[i]
    sma200 = ind.sma200[i]
    sd = ind.stdev20[i]
    rsi = ind.rsi14[i]
    atr = ind.atr14[i]

    if None in (sma20, sd, rsi, atr) or atr <= 0 or sd <= 0:
        return None

    # --- stretched ---
    lower_band = sma20 - SIGMA * sd
    if bar.close > lower_band:
        return None
    if rsi > RSI_MAX:
        return None

    # --- but not in freefall: only buy dips inside a longer-term uptrend ---
    if sma200 is not None:
        if bar.close < sma200:
            return None
    elif bar.close < sma20 * 0.80:
        return None

    # --- levels: the mean is the target ---
    entry = bar.close
    stop = entry - STOP_ATR * atr
    target = sma20
    if stop >= entry or target <= entry:
        return None

    risk = entry - stop
    if risk / entry > MAX_STOP_PCT:
        return None
    if (target - entry) / risk < MIN_REWARD_RISK:
        return None

    # --- quality ---
    z = (sma20 - bar.close) / sd
    stretch_score = _clamp01((z - SIGMA) / SIGMA)
    oversold_score = _clamp01((RSI_MAX - rsi) / RSI_MAX)
    quality = 40.0 + 60.0 * (0.55 * stretch_score + 0.45 * oversold_score)

    return Candidate(
        ticker=series.symbol,
        setup_type=SetupType.MEAN_REVERSION,
        entry_type=EntryType.RESTING_LIMIT,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        sector=sector,
        setup_quality=round(quality, 1),
        atr=round(atr, 4),
        features={
            "z_score": round(-z, 2),
            "rsi": round(rsi, 1),
            "sma20": round(sma20, 2),
            "lower_band": round(lower_band, 2),
        },
    )
