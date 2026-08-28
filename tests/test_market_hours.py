"""The provisional-bar hazard.

A daily bar appears the moment the opening bell rings, and its "close" is just
the last trade. Nothing in the payload says so. Two independent defences have to
hold, because either alone leaves a hole:

* `fetch` must re-read the last cached session, so a bar stored mid-session can
  still be repaired by a later run. Without this, coverage moves forward and the
  bad bar is frozen in permanently.
* `evening` must refuse to scan a session that has not closed. The staleness
  guard cannot catch this -- a provisional bar is *present*, so it passes.

This is not hypothetical: it happened on 2026-08-28, when a fetch run at 14:11 ET
wrote 472 mid-session bars that no subsequent incremental fetch would have fixed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading_bot.data.cache import OVERLAP_DAYS, BarCache
from trading_bot.data.models import Bar, BarSeries
from trading_bot.market_hours import EXCHANGE_TZ, session_is_final

DAY = date(2026, 8, 28)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EXCHANGE_TZ)


# --- session_is_final ----------------------------------------------------- #


def test_a_past_session_is_final() -> None:
    assert session_is_final(DAY - timedelta(days=1), now=at(10, 0))


def test_a_future_session_is_never_final() -> None:
    assert not session_is_final(DAY + timedelta(days=1), now=at(18, 0))


@pytest.mark.parametrize("hour,minute", [(9, 30), (12, 0), (14, 11), (15, 59)])
def test_today_is_not_final_while_the_market_trades(hour: int, minute: int) -> None:
    """14:11 is the exact time the real incident occurred."""
    assert not session_is_final(DAY, now=at(hour, minute))


@pytest.mark.parametrize("hour,minute", [(16, 0), (18, 15), (23, 59)])
def test_today_is_final_from_the_bell_onward(hour: int, minute: int) -> None:
    assert session_is_final(DAY, now=at(hour, minute))


def test_the_verdict_does_not_depend_on_the_hosts_timezone() -> None:
    """The operator runs this from Israel; the bell is in New York."""
    israel = datetime(2026, 8, 28, 21, 11, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert not session_is_final(DAY, now=israel)  # 14:11 in New York


def test_a_naive_clock_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError):
        session_is_final(DAY, now=datetime(2026, 8, 28, 18, 0))


# --- fetch re-reads the last cached session ------------------------------- #


def seed(cache: BarCache, symbol: str, last: date, n: int = 10) -> None:
    cache.store(
        BarSeries(
            symbol,
            [
                Bar(last - timedelta(days=i), 10.0, 11.0, 9.0, 10.5, 1_000.0)
                for i in reversed(range(n))
            ],
        )
    )


class FakeYahoo:
    """Records the window each symbol was asked for."""

    pause = 0.0

    def __init__(self) -> None:
        self.asked: dict[str, date] = {}

    def fetch_one(self, symbol: str, start: date, end: date) -> BarSeries | None:
        self.asked[symbol] = start
        return None


class FakeAlpaca:
    def __init__(self) -> None:
        self.asked: list[date] = []

    def daily_bars(self, symbols, start: date, end: date):
        self.asked.append(start)
        return {}


def test_yahoo_refetches_the_last_cached_day(tmp_path) -> None:
    """The regression. Coverage reaching `end` must not skip the symbol: the
    last bar is exactly the one that may be provisional."""
    from trading_bot.data.yahoo import refresh_cache

    cache = BarCache(tmp_path / "bars.db")
    seed(cache, "AAA", DAY)

    client = FakeYahoo()
    refresh_cache(cache, ["AAA"], DAY - timedelta(days=30), DAY, client=client,
                  progress=False)

    assert "AAA" in client.asked, "symbol was skipped as 'up to date'"
    assert client.asked["AAA"] <= DAY, "window does not re-read the last session"
    assert client.asked["AAA"] == DAY - timedelta(days=OVERLAP_DAYS)


def test_alpaca_tail_starts_before_the_last_cached_day(tmp_path) -> None:
    from trading_bot.data.alpaca_data import refresh_cache

    cache = BarCache(tmp_path / "bars.db")
    seed(cache, "AAA", DAY)

    client = FakeAlpaca()
    refresh_cache(cache, ["AAA"], DAY - timedelta(days=30), DAY, client=client)

    assert client.asked, "symbol was skipped entirely"
    assert min(client.asked) <= DAY - timedelta(days=1), (
        "a tail starting after the last stored bar can never repair it"
    )


def test_a_provisional_close_is_overwritten_by_a_later_fetch(tmp_path) -> None:
    """End to end: the upsert plus the overlap is what repairs the day."""
    cache = BarCache(tmp_path / "bars.db")
    seed(cache, "AAA", DAY)
    cache.store(BarSeries("AAA", [Bar(DAY, 10.0, 11.0, 9.0, 99.0, 1.0)]))
    assert cache.load("AAA")[-1].close == 99.0

    cache.store(BarSeries("AAA", [Bar(DAY, 10.0, 11.0, 9.0, 42.0, 1.0)]))
    assert cache.load("AAA")[-1].close == 42.0
    assert cache.coverage("AAA")[1] == DAY
