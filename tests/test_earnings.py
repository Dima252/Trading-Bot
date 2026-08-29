"""The earnings calendar, and the rule that could never fire without it.

`StaticEarningsCalendar` has always read config/earnings.json and nothing ever
wrote it, so the hardest rule in the constitution -- the one that CLOSES a
position rather than merely declining to open one -- was silently inert. A rule
that cannot fire is worse than a missing rule: the design reads as though the
risk is covered.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from trading_bot.data.earnings import EarningsDate, write_calendar


def dates(**kw: str) -> dict[str, EarningsDate]:
    return {
        s: EarningsDate(s, date.fromisoformat(d), estimated=False)
        for s, d in kw.items()
    }


def test_a_calendar_round_trips_into_the_format_the_rule_reads(tmp_path) -> None:
    from trading_bot.semantic.client import StaticEarningsCalendar

    path = tmp_path / "earnings.json"
    write_calendar(dates(AAPL="2026-10-29", JPM="2026-10-13"), str(path))

    calendar = StaticEarningsCalendar(str(path))
    within = calendar.within(["AAPL", "JPM"], date(2026, 10, 1), days=20)

    assert within == {"JPM": date(2026, 10, 13)}, "only JPM is inside 20 days"


def test_estimated_dates_are_excluded_from_a_hard_rule(tmp_path) -> None:
    """Guessing a date and then closing a position on the guess is worse than
    not blocking at all."""
    path = tmp_path / "earnings.json"
    mixed = {
        "SURE": EarningsDate("SURE", date(2026, 10, 29), estimated=False),
        "GUESS": EarningsDate("GUESS", date(2026, 10, 30), estimated=True),
    }

    assert write_calendar(mixed, str(path)) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"SURE": "2026-10-29"}

    assert write_calendar(mixed, str(path), include_estimates=True) == 2


def test_a_thin_fetch_cannot_silently_empty_the_calendar(tmp_path) -> None:
    """Rate limiting or an expired crumb produces a partial fetch. Writing it
    would shrink the calendar, and a rule that stops firing looks exactly like
    a rule with nothing to fire on."""
    path = tmp_path / "earnings.json"
    write_calendar(dates(A="2026-10-01", B="2026-10-02", C="2026-10-03",
                         D="2026-10-04"), str(path))

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        write_calendar(dates(A="2026-10-01"), str(path))

    # the good file survives
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 4


def test_a_full_refresh_replaces_it_normally(tmp_path) -> None:
    path = tmp_path / "earnings.json"
    write_calendar(dates(A="2026-10-01", B="2026-10-02"), str(path))
    assert write_calendar(dates(A="2026-11-01", B="2026-11-02", C="2026-11-03"),
                          str(path)) == 3
    assert json.loads(path.read_text(encoding="utf-8"))["A"] == "2026-11-01"


def test_writing_into_a_missing_directory_works(tmp_path) -> None:
    path = tmp_path / "nested" / "deeper" / "earnings.json"
    assert write_calendar(dates(A="2026-10-01"), str(path)) == 1
    assert path.exists()


def test_the_absent_calendar_still_warns_rather_than_failing_quietly(tmp_path) -> None:
    """The pre-existing behaviour, pinned: an empty calendar must be loud,
    because that is the state in which the rule cannot fire."""
    from trading_bot.semantic.client import StaticEarningsCalendar

    calendar = StaticEarningsCalendar(str(tmp_path / "does-not-exist.json"))
    assert calendar.dates == {}
    assert calendar.within(["AAPL"], date(2026, 10, 1), days=20) == {}
