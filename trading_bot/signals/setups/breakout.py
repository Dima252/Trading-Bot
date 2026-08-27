"""Breakout: close above the prior 20-day high, in a trend, on volume.

Entered on CLOSE CONFIRMATION -- the 15:30 job checks whether the level held
into the close rather than buying the first intraday touch. That filters
fakeouts at the cost of a worse fill (README section 7.1).
"""

from __future__ import annotations

from ...core.models import Candidate, EntryType, SetupType
from ...data.models import BarSeries

MIN_ADX = 25.0
VOLUME_SURGE = 1.5
REWARD_RISK = 3.0
MAX_STOP_ATR = 3.0
MAX_STOP_PCT = 0.15

# A stop closer than one ATR sits inside the instrument's ordinary daily noise.
# In a strong trend the structural low can be a fraction of a percent below the
# entry, which sizes the position to the per-name cap on a hair trigger -- large
# and near-certain to be stopped out for no reason.
MIN_STOP_ATR = 1.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(
    series: BarSeries, ind, i: int, sector: str, policy=None
) -> Candidate | None:
    bar = series[i]
    prior_high = ind.high20_prior[i]
    adx = ind.adx14[i]
    vol_avg = ind.vol_sma20[i]
    atr = ind.atr14[i]
    sma50 = ind.sma50[i]
    low10 = ind.low10_prior[i]

    if None in (prior_high, adx, vol_avg, atr, sma50, low10):
        return None
    if atr <= 0 or vol_avg <= 0:
        return None

    # --- entry conditions ---
    if bar.close <= prior_high:
        return None
    if adx < MIN_ADX:
        return None
    if bar.volume < VOLUME_SURGE * vol_avg:
        return None
    if bar.close < sma50:  # long only, and only with the trend
        return None

    # --- levels ---
    entry = bar.close
    # Bounded on both sides: never further than MAX_STOP_ATR (which would make
    # the sizing denominator huge), never closer than MIN_STOP_ATR (which would
    # make it tiny).
    structural = max(low10, entry - MAX_STOP_ATR * atr)
    stop = min(structural, entry - MIN_STOP_ATR * atr)
    if stop >= entry:
        return None

    risk = entry - stop
    if risk / entry > MAX_STOP_PCT:
        return None

    target = entry + REWARD_RISK * risk

    # --- quality: 40 (passed the filters) to 100 ---
    adx_score = _clamp01((adx - MIN_ADX) / 25.0)
    vol_score = _clamp01((bar.volume / vol_avg - VOLUME_SURGE) / VOLUME_SURGE)
    margin_score = _clamp01((bar.close / prior_high - 1.0) / 0.03)
    quality = 40.0 + 60.0 * (
        0.50 * adx_score + 0.30 * vol_score + 0.20 * margin_score
    )

    return Candidate(
        ticker=series.symbol,
        setup_type=SetupType.BREAKOUT,
        entry_type=EntryType.CLOSE_CONFIRM,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        sector=sector,
        setup_quality=round(quality, 1),
        atr=round(atr, 4),
        features={
            "adx": round(adx, 2),
            "volume_ratio": round(bar.volume / vol_avg, 2),
            "prior_high": round(prior_high, 2),
            "breakout_margin_pct": round((bar.close / prior_high - 1) * 100, 2),
            "stop_atr": round((entry - stop) / atr, 2),
        },
    )
