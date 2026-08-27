"""Technical indicators, implemented directly.

Written by hand rather than pulled from a TA library for three reasons: the set
needed is small, the implementations are auditable (a wrong indicator silently
changes every decision the agent makes), and it keeps the signal engine free of
a dependency whose version churn would eventually break a backtest.

Every function returns a series aligned to the input, with `None` where there is
not yet enough history. Aligned output is what lets the backtest index by bar
position instead of re-slicing, and it makes off-by-one lookahead visible.
"""

from __future__ import annotations

from math import sqrt

from ..data.models import Bar


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= n:
            running -= values[i - n]
        if i >= n - 1:
            out[i] = running / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def stdev(values: list[float], n: int) -> list[float | None]:
    """Population standard deviation over a trailing window."""
    out: list[float | None] = [None] * len(values)
    if n <= 1:
        return out
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        mean = sum(window) / n
        var = sum((v - mean) ** 2 for v in window) / n
        out[i] = sqrt(var)
    return out


def rolling_max_prior(values: list[float], n: int) -> list[float | None]:
    """Highest of the n bars BEFORE each position -- the current bar excluded.

    Breakout logic needs "broke above the prior 20-day high". Including the
    current bar makes the condition trivially self-satisfying and is the most
    common lookahead bug in this kind of code.
    """
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = max(values[i - n : i])
    return out


def rolling_min_prior(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        out[i] = min(values[i - n : i])
    return out


def true_range(bars: list[Bar]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        b, prev_close = bars[i], bars[i - 1].close
        out[i] = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low - prev_close),
        )
    return out


def _wilder(values: list[float | None], n: int, start: int) -> list[float | None]:
    """Wilder's smoothing: seed with a simple average, then s += (x - s) / n."""
    out: list[float | None] = [None] * len(values)
    end_seed = start + n
    if len(values) < end_seed:
        return out
    seed_window = [v for v in values[start:end_seed] if v is not None]
    if len(seed_window) < n:
        return out
    s = sum(seed_window) / n
    out[end_seed - 1] = s
    for i in range(end_seed, len(values)):
        v = values[i]
        if v is None:
            out[i] = s
            continue
        s = s + (v - s) / n
        out[i] = s
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float | None]:
    return _wilder(true_range(bars), n, start=1)


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out

    gains: list[float | None] = [None] * len(closes)
    losses: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)

    avg_gain = _wilder(gains, n, start=1)
    avg_loss = _wilder(losses, n, start=1)

    for i in range(len(closes)):
        g, loss = avg_gain[i], avg_loss[i]
        if g is None or loss is None:
            continue
        if loss == 0:
            out[i] = 100.0
        else:
            rs = g / loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def adx(bars: list[Bar], n: int = 14) -> list[float | None]:
    """Average Directional Index. Needs roughly 2n bars before it reports."""
    size = len(bars)
    out: list[float | None] = [None] * size
    if size < 2 * n + 1:
        return out

    plus_dm: list[float | None] = [None] * size
    minus_dm: list[float | None] = [None] * size
    for i in range(1, size):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr_s = _wilder(true_range(bars), n, start=1)
    plus_s = _wilder(plus_dm, n, start=1)
    minus_s = _wilder(minus_dm, n, start=1)

    dx: list[float | None] = [None] * size
    for i in range(size):
        tr_v, p, m = tr_s[i], plus_s[i], minus_s[i]
        if tr_v is None or p is None or m is None or tr_v == 0:
            continue
        plus_di = 100.0 * p / tr_v
        minus_di = 100.0 * m / tr_v
        denom = plus_di + minus_di
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom

    first_dx = next((i for i, v in enumerate(dx) if v is not None), None)
    if first_dx is None:
        return out
    return _wilder(dx, n, start=first_dx)


def realized_volatility(closes: list[float], n: int = 20) -> list[float | None]:
    """Annualised standard deviation of daily log-ish returns."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < 2:
        return out
    rets = [0.0] + [
        (closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] else 0.0
        for i in range(1, len(closes))
    ]
    sd = stdev(rets, n)
    for i, v in enumerate(sd):
        if v is not None and i >= n:
            out[i] = v * sqrt(252)
    return out
