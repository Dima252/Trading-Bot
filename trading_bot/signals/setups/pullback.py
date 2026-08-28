"""Pullback: an intact uptrend retracing to the 20 EMA with a reset RSI.

Entered on a RESTING LIMIT placed the next morning. Close confirmation would
miss most of these -- price dips to support intraday and recovers by the bell,
so a 15:30 check sees a bar that never looks like an entry (README section 7.1).
"""

from __future__ import annotations

from ...core.models import Candidate, EntryType, SetupType
from ...data.models import BarSeries

RSI_LOW = 35.0
RSI_HIGH = 60.0
TOUCH_TOLERANCE = 1.005  # within 0.5% of the EMA counts as a touch
REWARD_RISK = 2.5
STOP_ATR_BUFFER = 0.25
MAX_STOP_PCT = 0.12

# See breakout.py: a stop inside one ATR of the entry is inside the noise, and
# sizes the position to the per-name cap on a hair trigger.
MIN_STOP_ATR = 1.0
MAX_STOP_ATR = 3.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(
    series: BarSeries, ind, i: int, sector: str, policy=None
) -> Candidate | None:
    bar = series[i]
    ema20 = ind.ema20[i]
    sma20 = ind.sma20[i]
    sma50 = ind.sma50[i]
    rsi = ind.rsi14[i]
    atr = ind.atr14[i]
    low10 = ind.low10_prior[i]

    if None in (ema20, sma20, sma50, rsi, atr, low10) or atr <= 0:
        return None

    # --- trend must be intact ---
    if not (sma20 > sma50):
        return None
    if bar.close < sma50:
        return None

    # --- retraced to the moving average, but not broken ---
    if bar.low > ema20 * TOUCH_TOLERANCE:
        return None
    if bar.close < ema20 * 0.97:  # closed well below -> this is a breakdown
        return None
    if not (RSI_LOW <= rsi <= RSI_HIGH):
        return None

    # --- levels: buy the dip, so the limit sits at or below the EMA ---
    entry = min(bar.close, ema20)
    structural = min(low10, bar.low) - STOP_ATR_BUFFER * atr
    stop = min(structural, entry - MIN_STOP_ATR * atr)
    stop = max(stop, entry - MAX_STOP_ATR * atr)
    if stop >= entry:
        return None

    risk = entry - stop
    if risk / entry > MAX_STOP_PCT:
        return None

    target = entry + REWARD_RISK * risk

    # --- quality ---
    trend_score = _clamp01((sma20 / sma50 - 1.0) / 0.05)
    depth = _clamp01((ema20 - bar.low) / (2.0 * atr))
    reset = _clamp01((RSI_HIGH - rsi) / (RSI_HIGH - RSI_LOW))

    # As built, quality rises with a DEEPER dip and a LOWER RSI. The diagnostic
    # says both are backwards; the flag inverts them so it can be measured.
    favour_shallow = bool(policy and getattr(policy, "pullback_favour_shallow", False))
    reset_score = (1.0 - reset) if favour_shallow else reset
    depth_score = (1.0 - depth) if favour_shallow else depth
    w_trend = getattr(policy, "pullback_w_trend", 0.40) if policy else 0.40
    w_reset = getattr(policy, "pullback_w_reset", 0.35) if policy else 0.35
    w_depth = getattr(policy, "pullback_w_depth", 0.25) if policy else 0.25
    total = (w_trend + w_reset + w_depth) or 1.0
    quality = 40.0 + 60.0 * (
        (w_trend * trend_score + w_reset * reset_score + w_depth * depth_score)
        / total
    )

    return Candidate(
        ticker=series.symbol,
        setup_type=SetupType.PULLBACK,
        entry_type=EntryType.RESTING_LIMIT,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        sector=sector,
        setup_quality=round(quality, 1),
        atr=round(atr, 4),
        features={
            "rsi": round(rsi, 1),
            "ema20": round(ema20, 2),
            "trend_spread_pct": round((sma20 / sma50 - 1) * 100, 2),
            "depth_atr": round((ema20 - bar.low) / atr, 2),
        },
    )
