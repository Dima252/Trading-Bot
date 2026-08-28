"""The deployment artifacts.

A crontab is code that runs unattended for six months. The ordering invariant it
encodes is not obvious from reading it, so it gets a test: `fetch` must precede
`evening`, or the scanner sees a stale cache, finds nothing, and the session is
lost.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
CRONTAB = DEPLOY / "crontab.template"


def parse() -> tuple[dict[str, str], list[tuple[int, int, str]]]:
    env: dict[str, str] = {}
    jobs: list[tuple[int, int, str]] = []

    for line in CRONTAB.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[A-Z_]+=", line):
            key, _, value = line.partition("=")
            env[key] = value
            continue

        minute, hour, _dom, _mon, _dow, command = line.split(None, 5)
        name = "rotate"
        if "trading_bot " in command:
            name = command.split("trading_bot ")[-1].split()[0]
        jobs.append((int(hour), int(minute), name))

    return env, jobs


def test_the_template_exists_and_parses() -> None:
    env, jobs = parse()
    assert jobs, "no scheduled entries"
    assert all(0 <= h <= 23 and 0 <= m <= 59 for h, m, _ in jobs)


def test_cron_tz_is_set() -> None:
    """The schedule is New York time. Without CRON_TZ it drifts an hour at every
    DST changeover, and US/EU switch on different dates."""
    env, _ = parse()
    assert env.get("CRON_TZ") == "America/New_York"


def test_fetch_runs_before_evening() -> None:
    """The invariant the whole schedule turns on.

    The scanner matches session dates exactly, so an unrefreshed cache makes
    every symbol invisible and the scan returns a clean zero. The evening job
    errors rather than reporting a quiet market -- but the session is still lost.
    """
    _, jobs = parse()
    at = {name: (h, m) for h, m, name in jobs}

    assert "fetch" in at and "evening" in at
    assert at["fetch"] < at["evening"], (
        f"fetch at {at['fetch']} must precede evening at {at['evening']}"
    )


def test_all_four_jobs_are_scheduled() -> None:
    _, jobs = parse()
    names = {name for _, _, name in jobs}
    assert {"premarket", "open", "close", "evening"} <= names


def test_the_jobs_run_in_session_order() -> None:
    """premarket -> open -> close, mirroring the trading day."""
    _, jobs = parse()
    at = {name: (h, m) for h, m, name in jobs}
    assert at["premarket"] < at["open"] < at["close"]


def test_scheduled_commands_are_real_subcommands() -> None:
    """A typo in the crontab is a job that never runs and never complains."""
    from trading_bot.cli import build_parser

    _, jobs = parse()
    subparsers = [
        a for a in build_parser()._actions if hasattr(a, "choices") and a.choices
    ]
    known = set(subparsers[0].choices)

    for _, _, name in jobs:
        if name != "rotate":
            assert name in known, f"{name!r} is not a trading_bot subcommand"


@pytest.mark.parametrize("name", ["setup.sh", "crontab.template", "README.md"])
def test_deploy_artifacts_are_present(name: str) -> None:
    assert (DEPLOY / name).is_file()


def test_setup_refuses_to_proceed_on_a_failing_test_suite() -> None:
    """Provisioning a host whose tests fail would ship a system whose numbers
    cannot be trusted."""
    source = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
    assert "pytest -q || die" in source
    assert "set -euo pipefail" in source
