"""Free daily bars, no API key.

Alpaca credentials are needed to TRADE. They are not needed to answer the only
question that matters before trading: does this have an edge? This adapter gets
real history now so the backtest can be run today.

Two things it gets right that a naive fetch does not:

* **Back-adjustment.** Raw OHLC is unadjusted, so a 4:1 split reads as a 75%
  crash and every indicator downstream is wrong. Each bar is scaled by
  `adjclose / close`, which folds in both splits and dividends.
* **Volume.** Scaled by the inverse factor, so `close * volume` -- the number
  the liquidity screen actually uses -- survives the adjustment unchanged.

CAVEATS, because this is not an official product:
  - unofficial endpoint; it can rate-limit, change shape, or disappear
  - adjusted prices are not prices anyone could have traded (a $200 name was
    $50 before its split). Fine for relative technical signals, which is all
    this system uses; not fine if you ever add an absolute price rule.
  - it does not fix survivorship bias -- the ticker list is still present-day
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta

from .cache import OVERLAP_DAYS, BarCache
from .models import Bar, BarSeries

log = logging.getLogger("trading_bot.yahoo")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot research)"}
REQUEST_PAUSE = 0.6  # be a considerate client


def to_yahoo_symbol(symbol: str) -> str:
    """`BRK.B` is `BRK-B` upstream."""
    return symbol.replace(".", "-")


class YahooData:
    def __init__(self, pause: float = REQUEST_PAUSE, timeout: int = 30) -> None:
        self.pause = pause
        self.timeout = timeout

    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, BarSeries]:
        out: dict[str, BarSeries] = {}
        for i, symbol in enumerate(symbols):
            try:
                series = self.fetch_one(symbol, start, end)
            except Exception as exc:
                log.warning("%s: fetch failed (%s)", symbol, exc)  # kill the run
                continue
            if series is not None and len(series):
                out[symbol] = series
            if i + 1 < len(symbols):
                time.sleep(self.pause)
        return out

    def fetch_one(
        self, symbol: str, start: date, end: date, retries: int = 3
    ) -> BarSeries | None:
        import requests

        params = {
            "period1": int(
                datetime.combine(start, datetime.min.time(), UTC).timestamp()
            ),
            "period2": int(
                datetime.combine(
                    end + timedelta(days=1), datetime.min.time(), UTC
                ).timestamp()
            ),
            "interval": "1d",
            "events": "div,split",
        }

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = requests.get(
                    CHART_URL.format(symbol=to_yahoo_symbol(symbol)),
                    params=params,
                    headers=HEADERS,
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    time.sleep(2 ** (attempt + 1))
                    continue
                response.raise_for_status()
                return _parse(symbol, response.json())
            except Exception as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))

        if last_error:
            raise last_error
        return None


def _parse(symbol: str, payload: dict) -> BarSeries | None:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    results = chart.get("result") or []
    if not results:
        return None

    data = results[0]
    stamps = data.get("timestamp") or []
    quote = (data.get("indicators", {}).get("quote") or [{}])[0]
    adjclose = (data.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")

    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[Bar] = []
    seen: set[date] = set()
    for i, stamp in enumerate(stamps):
        o, h, lo, c = _at(opens, i), _at(highs, i), _at(lows, i), _at(closes, i)
        v = _at(volumes, i)
        if None in (o, h, lo, c) or c <= 0:
            continue  # a halted or missing session, not a zero-priced one

        # back-adjust for splits and dividends
        factor = 1.0
        if adjclose is not None:
            adj = _at(adjclose, i)
            if adj is not None and c > 0:
                factor = adj / c

        day = datetime.fromtimestamp(stamp, UTC).date()
        if day in seen:
            continue
        seen.add(day)

        bars.append(
            Bar(
                day=day,
                open=round(o * factor, 4),
                high=round(h * factor, 4),
                low=round(lo * factor, 4),
                close=round(c * factor, 4),
                # inverse-scaled so close * volume is unchanged by adjustment
                volume=round((v or 0.0) / factor if factor else 0.0, 2),
            )
        )

    bars.sort(key=lambda b: b.day)

    # Drop sessions where nothing traded. A zero-volume daily bar is a carried
    # forward quote, not a session: the price is stale, the range is usually
    # zero, and nothing could have been bought or sold at it. Left in, they
    # flatten ATR, understate average volume, and manufacture enormous returns
    # on the day real trading resumes -- NVR shows +2633% on 1993-10-01 purely
    # because two zero-volume bars at $0.38 preceded its real $10.25 open.
    #
    # Rate and index series are exempt: zero volume is their normal state, and
    # filtering them would delete the series. `^IRX` is 99.5% zero-volume and
    # it is the cash yield the whole backtest earns on idle balances.
    #
    # The test is the `^` prefix, matching `universe.is_tradeable`, and NOT a
    # rule inferred from the volume itself. Inferring it looks more robust and
    # is worse: `^IRX` carries 45 spurious non-zero volume readings out of
    # 8,515, so "does this series ever show volume?" answers yes and deletes
    # 8,470 bars of it. That mistake was made here before this comment existed.
    if not symbol.startswith("^"):
        bars = [b for b in bars if b.volume > 0]

    return BarSeries(symbol, bars) if bars else None


def _at(values, i):
    try:
        return values[i]
    except (IndexError, TypeError):
        return None


def refresh_cache(
    cache: BarCache,
    symbols: list[str],
    start: date,
    end: date,
    client: YahooData | None = None,
    incremental: bool = True,
    progress: bool = True,
) -> dict[str, int]:
    """Download what the cache is missing and store it."""
    client = client or YahooData()
    written: dict[str, int] = {}

    for i, symbol in enumerate(symbols, 1):
        from_day = start
        if incremental:
            cov = cache.coverage(symbol)
            if cov is not None:
                # Deliberately no "already up to date, skip" branch. Coverage
                # reaching `end` is not evidence the last bar is any good -- a
                # fetch during market hours stores a provisional close and moves
                # coverage forward, and skipping on that basis would freeze the
                # bad bar in place permanently.
                from_day = cov[1] - timedelta(days=OVERLAP_DAYS)

        try:
            series = client.fetch_one(symbol, from_day, end)
        except Exception as exc:
            print(f"  [{i}/{len(symbols)}] {symbol:<6} FAILED: {exc}")
            continue

        count = cache.store(series) if series else 0
        written[symbol] = count
        if progress:
            span = f"{series[0].day} -> {series[-1].day}" if series else "no data"
            print(f"  [{i}/{len(symbols)}] {symbol:<6} {count:>5} bars  {span}")
        time.sleep(client.pause)

    return written
