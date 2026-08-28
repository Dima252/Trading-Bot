"""Alpaca market data.

Imports of `alpaca` are deferred to call time so that the backtest, the signal
engine and the whole test suite run with no credentials and no SDK installed.

FEED NOTE: the free tier serves IEX, which is a fraction of consolidated volume.
That is fine for nightly daily-bar work, but it makes intraday volume figures
and the 15:30 close-confirmation check unreliable. Verify what your account
actually returns before trusting either (README section 16).
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from .cache import OVERLAP_DAYS, BarCache
from .models import Bar, BarSeries

KEY_ENV = "APCA_API_KEY_ID"
SECRET_ENV = "APCA_API_SECRET_KEY"


class MissingCredentials(RuntimeError):
    pass


def credentials() -> tuple[str, str]:
    key = os.environ.get(KEY_ENV)
    secret = os.environ.get(SECRET_ENV)
    if not key or not secret:
        raise MissingCredentials(
            f"set {KEY_ENV} and {SECRET_ENV} in the environment "
            "(paper keys from alpaca.markets)"
        )
    return key, secret


def have_credentials() -> bool:
    return bool(os.environ.get(KEY_ENV) and os.environ.get(SECRET_ENV))


class AlpacaData:
    """Thin wrapper over the historical bars endpoint."""

    def __init__(self, key: str | None = None, secret: str | None = None) -> None:
        if key is None or secret is None:
            key, secret = credentials()
        from alpaca.data.historical import StockHistoricalDataClient

        self.client = StockHistoricalDataClient(key, secret)

    def daily_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, BarSeries]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        response = self.client.get_stock_bars(request)

        out: dict[str, BarSeries] = {}
        for symbol in symbols:
            raw = response.data.get(symbol, []) if hasattr(response, "data") else []
            bars = [
                Bar(
                    day=b.timestamp.date(),
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                )
                for b in raw
            ]
            # de-duplicate defensively: the API occasionally repeats a session
            seen: dict[date, Bar] = {}
            for bar in bars:
                seen[bar.day] = bar
            if seen:
                out[symbol] = BarSeries(symbol, [seen[d] for d in sorted(seen)])
        return out


    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price per symbol.

        The 15:30 close-confirmation check depends on this being current. On a
        delayed feed it is not, and the check silently reads stale prices --
        verify what your account returns before relying on it.
        """
        from alpaca.data.requests import StockLatestTradeRequest

        if not symbols:
            return {}
        trades = self.client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=list(symbols))
        )
        return {
            symbol: float(trade.price)
            for symbol, trade in trades.items()
            if getattr(trade, "price", None)
        }


def refresh_cache(
    cache: BarCache,
    symbols: list[str],
    start: date,
    end: date,
    batch_size: int = 50,
    client: AlpacaData | None = None,
    incremental: bool = True,
) -> dict[str, int]:
    """Download what the cache is missing and store it.

    Incremental by default: for symbols already cached, only the tail since the
    last stored bar is fetched.
    """
    client = client or AlpacaData()
    written: dict[str, int] = {}

    full: list[str] = []
    tails: dict[date, list[str]] = {}
    for symbol in symbols:
        cov = cache.coverage(symbol) if incremental else None
        if cov is None:
            full.append(symbol)
        else:
            # From `last - OVERLAP`, not `last + 1`. Starting after the last
            # stored bar never revisits it, so a bar cached mid-session keeps its
            # provisional close forever and no later fetch can repair it.
            tails.setdefault(cov[1] - timedelta(days=OVERLAP_DAYS), []).append(symbol)

    def pull(batch: list[str], from_day: date) -> None:
        for i in range(0, len(batch), batch_size):
            chunk = batch[i : i + batch_size]
            for symbol, series in client.daily_bars(chunk, from_day, end).items():
                written[symbol] = written.get(symbol, 0) + cache.store(series)

    if full:
        pull(full, start)
    for from_day, batch in tails.items():
        pull(batch, from_day)

    return written


def trading_calendar(start: date, end: date) -> list[date]:
    """Real session dates. Every job checks this first -- otherwise three cron
    jobs fire on Thanksgiving."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    key, secret = credentials()
    client = TradingClient(key, secret, paper=True)
    days = client.get_calendar(GetCalendarRequest(start=start, end=end))
    return [d.date for d in days]
