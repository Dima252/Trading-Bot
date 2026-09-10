"""The GitHub Actions schedule.

GitHub's scheduler is UTC only and offers no timezone support, so a New York
schedule has to be expressed in UTC and survive two DST changeovers a year. Get
it wrong and the jobs fire an hour early or late for months -- silently, since a
job that runs at the wrong hour still exits cleanly.

This project has already lost a session to a local clock standing in for the
exchange's, so the schedule gets the same treatment the code did.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
NY, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")

# Mid-summer and mid-winter: the two DST states, well away from a changeover.
EDT, EST = (2026, 7, 15), (2026, 1, 15)


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # "on" is the YAML 1.1 boolean True, so it can arrive under either key.
    data["triggers"] = data.get("on", data.get(True))
    return data


def crons(name: str) -> list[str]:
    return [c["cron"] for c in load(name)["triggers"]["schedule"]]


def utc_times(name: str) -> list[tuple[int, int]]:
    """(hour, minute) pairs a cron expression fires at."""
    out = []
    for expr in crons(name):
        minute, hour = expr.split()[0], expr.split()[1]
        for h in hour.split(","):
            for m in minute.split(","):
                out.append((int(h), int(m)))
    return sorted(out)


def which_job(now: datetime) -> str:
    """The guard from intraday.yml, mirrored so it can be tested.

    Kept in step with the workflow by `test_the_guard_matches_the_workflow`.
    """
    if now.hour == 9:
        return "premarket"
    if now.hour == 10:
        return "open"
    if now.hour == 15 and now.minute >= 20:
        return "close"
    return ""


# --- the evening job needs no guard --------------------------------------- #


@pytest.mark.parametrize("state", [EDT, EST])
def test_the_evening_job_lands_after_the_bell_in_both_dst_states(state) -> None:
    """23:00 UTC is the one time that works unchanged year-round: 19:00 EDT and
    18:00 EST, against a 16:00 bell. That is why it needs a single cron entry
    where every other job needs two."""
    y, m, d = state
    for hour, minute in utc_times("evening.yml"):
        et = datetime(y, m, d, hour, minute, tzinfo=UTC).astimezone(NY)
        assert et.hour >= 16, f"{hour:02d}:{minute:02d} UTC is {et:%H:%M} ET"


def test_the_evening_job_has_exactly_one_schedule() -> None:
    assert crons("evening.yml") == ["0 23 * * 1-5"]


# --- the intraday guard --------------------------------------------------- #


@pytest.mark.parametrize("state,label", [(EDT, "EDT"), (EST, "EST")])
def test_each_intraday_job_fires_exactly_once_a_day(state, label) -> None:
    """Every candidate UTC time is scheduled and the guard picks the one that
    belongs to the hour. Firing twice would double-submit; firing zero times
    loses the session."""
    y, m, d = state
    fired = [
        which_job(datetime(y, m, d, h, mi, tzinfo=UTC).astimezone(NY))
        for h, mi in utc_times("intraday.yml")
    ]
    assert sorted(j for j in fired if j) == ["close", "open", "premarket"], (
        f"{label}: got {fired}"
    )


def test_the_ambiguous_hour_is_resolved_by_the_exchange_clock() -> None:
    """14:00 UTC is premarket under EST and open under EDT. Counting hours off
    UTC cannot tell them apart; asking New York can."""
    edt = datetime(2026, 7, 15, 14, 0, tzinfo=UTC).astimezone(NY)
    est = datetime(2026, 1, 15, 14, 0, tzinfo=UTC).astimezone(NY)

    assert which_job(edt) == "open"
    assert which_job(est) == "premarket"


def test_no_job_runs_outside_market_hours() -> None:
    for hour in list(range(0, 9)) + list(range(16, 24)):
        at = datetime(2026, 7, 15, hour, 0, tzinfo=NY)
        assert which_job(at) == "", f"{hour:02d}:00 ET should run nothing"


def test_the_guard_matches_the_workflow() -> None:
    """The mirrored guard above must stay in step with the real one."""
    source = (WORKFLOWS / "intraday.yml").read_text(encoding="utf-8")
    for line in (
        'if now.hour == 9:',
        'print("premarket")',
        'if now.hour == 10:',
        'print("open")',
        'if now.hour == 15 and now.minute >= 20:',
        'print("close")',
    ):
        assert line in source, f"the workflow no longer contains: {line}"


def test_the_guard_reads_the_exchange_clock_not_a_reimplementation() -> None:
    """A second timezone implementation in YAML is a second thing to get wrong."""
    source = (WORKFLOWS / "intraday.yml").read_text(encoding="utf-8")
    assert "from trading_bot.market_hours import now_exchange" in source


# --- the things that would break it silently ------------------------------ #


@pytest.mark.parametrize("name", ["evening.yml", "intraday.yml"])
def test_both_workflows_serialise_on_the_state_database(name: str) -> None:
    """Both commit the state database. Concurrent runs race, and one loses."""
    data = load(name)
    assert data["concurrency"]["group"] == "bot-state"
    assert data["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize("name", ["evening.yml", "intraday.yml"])
def test_neither_workflow_can_trade_live(name: str) -> None:
    """Paper is the default and `--live` is the only way past it. It must not
    appear in anything that runs unattended."""
    assert "--live" not in (WORKFLOWS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["evening.yml", "intraday.yml"])
def test_credentials_come_from_secrets(name: str) -> None:
    source = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "secrets.APCA_API_KEY_ID" in source
    assert "secrets.APCA_API_SECRET_KEY" in source
    # never inline
    assert "APCA_API_KEY_ID: PK" not in source


def test_the_schedule_keeps_itself_alive() -> None:
    """GitHub disables scheduled workflows after 60 days without repository
    activity. The evening run commits every day, which counts."""
    source = (WORKFLOWS / "evening.yml").read_text(encoding="utf-8")
    assert "git commit" in source and "git push" in source
