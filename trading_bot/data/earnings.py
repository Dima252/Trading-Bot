"""Upcoming earnings dates, for the rule that has never been able to fire.

`StaticEarningsCalendar` has always read `config/earnings.json` and nothing has
ever written it. That makes the earnings block -- the hardest rule in the
constitution, the one that closes a position rather than merely declining to
open one -- silently inert. A rule that cannot fire is worse than a missing
rule, because the design reads as though the risk is covered.

The source needs a cookie-and-crumb handshake; without it the endpoint returns
401. That is fragile by nature, so:

* every date carries whether the provider called it an ESTIMATE, and estimates
  are kept out of a hard-blocking rule by default -- guessing a date and then
  closing a position on the guess is worse than not blocking
* the writer refuses to overwrite a good file with a mostly-empty one, since a
  silently emptied calendar restores exactly the failure this module fixes
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

log = logging.getLogger("trading_bot.earnings")

CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_URL = "https://fc.yahoo.com"
SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

REQUEST_PAUSE = 0.4

# A refresh that collects less than this fraction of what the existing file
# holds is treated as a failed run, not as a new calendar.
MIN_COVERAGE_RATIO = 0.5


@dataclass(frozen=True)
class EarningsDate:
    symbol: str
    day: date
    estimated: bool


class YahooEarnings:
    """Next scheduled earnings date per symbol."""

    def __init__(self, pause: float = REQUEST_PAUSE, timeout: int = 20) -> None:
        self.pause = pause
        self.timeout = timeout
        self._opener: urllib.request.OpenerDirector | None = None
        self._crumb: str | None = None

    def _session(self) -> tuple[urllib.request.OpenerDirector, str]:
        """Cookie first, then crumb. The endpoint 401s without both."""
        if self._opener is not None and self._crumb:
            return self._opener, self._crumb

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        opener.addheaders = [("User-Agent", USER_AGENT)]
        # The cookie is set even when this request itself errors, which it
        # routinely does -- the response body is not what we came for.
        with contextlib.suppress(Exception):
            opener.open(COOKIE_URL, timeout=self.timeout)

        crumb = opener.open(CRUMB_URL, timeout=self.timeout).read().decode()
        if not crumb or "<" in crumb:
            raise RuntimeError(f"could not obtain a crumb (got {crumb[:40]!r})")

        self._opener, self._crumb = opener, crumb
        return opener, crumb

    def next_date(self, symbol: str) -> EarningsDate | None:
        opener, crumb = self._session()
        url = SUMMARY_URL.format(symbol=symbol.replace(".", "-"))
        url += f"?modules=calendarEvents&crumb={crumb}"

        raw = json.load(opener.open(url, timeout=self.timeout))
        results = (raw.get("quoteSummary") or {}).get("result") or []
        if not results:
            return None

        earnings = (results[0].get("calendarEvents") or {}).get("earnings") or {}
        stamps = earnings.get("earningsDate") or []
        if not stamps:
            return None

        when = datetime.fromtimestamp(
            stamps[0]["raw"], tz=UTC
        ).date()
        return EarningsDate(
            symbol=symbol,
            day=when,
            estimated=bool(earnings.get("isEarningsDateEstimate")),
        )

    def fetch(
        self, symbols: list[str], progress: bool = True
    ) -> dict[str, EarningsDate]:
        out: dict[str, EarningsDate] = {}
        for i, symbol in enumerate(symbols, 1):
            try:
                found = self.next_date(symbol)
            except Exception as exc:
                # One bad symbol must not end a 500-symbol run.
                log.warning("%s: %s", symbol, exc)
                found = None
            if found is not None:
                out[symbol] = found
            if progress and i % 25 == 0:
                print(f"  [{i}/{len(symbols)}] {len(out)} dates so far", flush=True)
            time.sleep(self.pause)
        return out


def write_calendar(
    dates: dict[str, EarningsDate],
    path: str = "config/earnings.json",
    include_estimates: bool = False,
) -> int:
    """Write the calendar, refusing to replace a good file with a thin one.

    A partial fetch -- rate limiting, an expired crumb, a network blip -- would
    otherwise quietly shrink the calendar, and a rule that stops firing looks
    exactly like a rule with nothing to fire on.
    """
    keep = {
        s: d for s, d in dates.items() if include_estimates or not d.estimated
    }

    file = Path(path)
    if file.exists():
        existing = json.loads(file.read_text(encoding="utf-8"))
        if existing and len(keep) < len(existing) * MIN_COVERAGE_RATIO:
            raise RuntimeError(
                f"refusing to overwrite {path}: got {len(keep)} dates against "
                f"{len(existing)} already there. That looks like a failed fetch, "
                "and a silently emptied calendar disables a hard rule."
            )

    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps({s: d.day.isoformat() for s, d in sorted(keep.items())}, indent=2),
        encoding="utf-8",
    )
    return len(keep)
