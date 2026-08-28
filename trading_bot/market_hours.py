"""When a session's bars can be trusted as closing values.

A daily bar exists from the opening bell onward. Its "close" is simply the last
trade so far, and nothing in the payload marks it as provisional -- a bar fetched
at 14:00 looks exactly like one fetched at 18:00. So every guard that asks "do we
have today's bar?" answers yes the moment the market opens, and the scanner
happily computes ATR, RSI and rolling highs against a price that is still moving.

The only defence is refusing to treat today as finished until it is.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")

# Regular close. Early closes (13:00 ET, around some holidays) settle sooner, so
# this is conservative in the safe direction: it withholds trust for three extra
# hours on a handful of days a year. The evening job runs at 18:15 regardless,
# so in practice that costs nothing.
SESSION_CLOSE = time(16, 0)


def now_exchange() -> datetime:
    """Wall clock at the exchange, whatever the host's timezone is set to."""
    return datetime.now(EXCHANGE_TZ)


def session_is_final(day: date, now: datetime | None = None) -> bool:
    """True once `day` has closed and its bars are settled values.

    Past sessions are always final; future ones never are. Today turns final at
    the closing bell.
    """
    now = now or now_exchange()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware to compare against the bell")
    now = now.astimezone(EXCHANGE_TZ)

    if day != now.date():
        return day < now.date()
    return now.time() >= SESSION_CLOSE
