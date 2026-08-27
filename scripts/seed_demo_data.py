"""Populate the bar cache with synthetic data so the CLI can be exercised
before API keys exist.

    python scripts/seed_demo_data.py
    python -m trading_bot backtest --bars data/demo_bars.db

The prices are random walks. The RESULTS ARE MEANINGLESS as evidence about the
strategy -- this exists to prove the plumbing runs end to end, nothing more.
Replace it with `python -m trading_bot fetch` once credentials are set.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot.data.cache import BarCache  # noqa: E402
from trading_bot.data.models import Bar, BarSeries  # noqa: E402
from trading_bot.data.universe import SECTORS  # noqa: E402

BARS = 900
OUT = "data/demo_bars.db"


def walk(rng: random.Random, n: int, start: float, drift: float, vol: float):
    out, level = [], start
    for _ in range(n):
        level *= 1 + drift + rng.gauss(0, vol)
        out.append(round(max(level, 1.0), 2))
    return out


def series_for(symbol: str, closes: list[float], rng: random.Random) -> BarSeries:
    bars: list[Bar] = []
    day = date.today() - timedelta(days=int(BARS * 1.45))
    prev = closes[0]
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        wiggle = close * rng.uniform(0.003, 0.012)
        bars.append(
            Bar(
                day=day,
                open=round(prev + rng.uniform(-wiggle, wiggle), 2),
                high=round(max(close, prev) + wiggle, 2),
                low=round(min(close, prev) - wiggle, 2),
                close=close,
                volume=round(2_000_000 * max(0.25, rng.lognormvariate(0, 0.5))),
            )
        )
        prev = close
        day += timedelta(days=1)
    return BarSeries(symbol, bars)


def main() -> None:
    rng = random.Random(20260828)
    cache = BarCache(OUT)

    symbols = ["SPY"] + sorted(SECTORS)
    for i, symbol in enumerate(symbols):
        drift = 0.0004 if symbol == "SPY" else rng.uniform(-0.0002, 0.0009)
        vol = 0.008 if symbol == "SPY" else rng.uniform(0.012, 0.026)
        start = 400.0 if symbol == "SPY" else rng.uniform(30, 400)
        cache.store(series_for(symbol, walk(rng, BARS, start, drift, vol), rng))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(symbols)} symbols")

    stats = cache.stats()
    print(f"\nwrote {stats['bars']} bars for {stats['symbols']} symbols -> {OUT}")
    print("\nSYNTHETIC DATA -- random walks. Use it to check the pipeline runs,")
    print("never as evidence about the strategy.\n")
    print(f"  python -m trading_bot backtest --bars {OUT}")


if __name__ == "__main__":
    main()
