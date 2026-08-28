"""The command line, which is the only thing cron and the operator ever touch.

Nothing here needs a network or credentials. The point is the wiring: a typo in
a subcommand, a default that resolves on the wrong clock, or a step that runs
out of order are all failures that produce no error at all -- just a job that
quietly does nothing, or does it to the wrong day.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trading_bot.cli import _regime_on_file, build_parser, cmd_daily
from trading_bot.core.models import Regime
from trading_bot.core.policy import Policy
from trading_bot.db.repo import Repo
from trading_bot.market_hours import today_exchange


def subcommands(parser: argparse.ArgumentParser) -> dict:
    action = next(a for a in parser._actions if getattr(a, "choices", None))
    return dict(action.choices)


# --- wiring --------------------------------------------------------------- #


def test_every_subcommand_dispatches_somewhere() -> None:
    """A subparser with no `func` raises AttributeError at run time -- after
    cron has already reported success starting it."""
    for name, sub in subcommands(build_parser()).items():
        assert callable(sub.get_default("func")), f"{name} has no handler"


def test_the_scheduled_commands_all_exist() -> None:
    names = set(subcommands(build_parser()))
    assert {"fetch", "evening", "premarket", "open", "close", "daily"} <= names


def test_day_defaults_to_the_date_in_new_york() -> None:
    """Not `date.today()`. From UTC+3 the host's date rolls over at 17:00 ET,
    and a job that asks for tomorrow finds no session and skips."""
    args = build_parser().parse_args(["evening"])
    assert args.day == today_exchange()


@pytest.mark.parametrize("command", ["evening", "daily", "close"])
def test_day_is_still_overridable(command: str) -> None:
    args = build_parser().parse_args([command, "--day", "2026-03-02"])
    assert args.day == date(2026, 3, 2)


def test_daily_is_a_dry_run_unless_armed() -> None:
    """The default has to be the safe one: six months of live decisions should
    start with a word you typed, not a flag you forgot."""
    assert build_parser().parse_args(["daily"]).arm is False
    assert build_parser().parse_args(["daily", "--arm"]).arm is True


def test_live_trading_needs_an_explicit_flag() -> None:
    assert build_parser().parse_args(["evening"]).live is False


# --- daily ---------------------------------------------------------------- #


class Args:
    """Stand-in for parsed arguments, so cmd_daily can run without a network."""

    def __init__(self, day: date, **kw) -> None:
        self.day = day
        self.arm = False
        self.dry_run = True
        self.live = False
        self.days = 30
        self.source = "yahoo"
        self.full = False
        self.out = "out/unused.html"
        self.policy = "config/policy.yaml"
        self.bars = "data/bars.db"
        self.state = ":memory:"
        self.__dict__.update(kw)


def test_daily_refuses_before_the_closing_bell(capsys) -> None:
    """And refuses BEFORE fetching -- the fetch takes minutes, and its result
    would have to be thrown away anyway."""
    open_session = today_exchange() + timedelta(days=1)

    assert cmd_daily(Args(day=open_session)) == 1

    out = capsys.readouterr().out
    assert "has not closed yet" in out
    assert "refreshing bars" not in out, "it must not fetch first"


def test_daily_stops_when_the_fetch_fails(monkeypatch, capsys) -> None:
    """Continuing would scan a stale cache and report a confident zero."""
    import trading_bot.cli as cli

    monkeypatch.setattr(cli, "cmd_fetch", lambda _a: 1)
    scanned = []
    monkeypatch.setattr(
        cli, "_context", lambda *a, **k: scanned.append(1)  # pragma: no cover
    )

    settled = today_exchange() - timedelta(days=7)
    assert cmd_daily(Args(day=settled)) == 1
    assert not scanned, "scanned despite a failed fetch"
    assert "stopping before the scan" in capsys.readouterr().out


def test_daily_fetches_before_it_scans(monkeypatch) -> None:
    """The one ordering that matters: the scanner matches session dates exactly,
    so an unrefreshed cache makes every symbol invisible."""
    import trading_bot.cli as cli

    order: list[str] = []
    monkeypatch.setattr(cli, "cmd_fetch", lambda _a: order.append("fetch") or 0)

    class Result:
        job, status, summary, notes = "evening", "ok", "", []

    ctx = type("Ctx", (), {"repo": None, "cache": None, "policy": None})()
    monkeypatch.setattr(cli, "_context", lambda *a, **k: ctx)
    monkeypatch.setattr(
        "trading_bot.jobs.evening.run",
        lambda _ctx: order.append("scan") or Result(),
    )
    monkeypatch.setattr(
        "trading_bot.ui.dashboard.write",
        lambda *a, **k: order.append("render") or _FakePath(),
    )

    cmd_daily(Args(day=today_exchange() - timedelta(days=7)))
    assert order == ["fetch", "scan", "render"]


class _FakePath:
    def stat(self):
        return type("st", (), {"st_size": 1024})()

    def __str__(self) -> str:
        return "out/unused.html"


# --- the regime the intraday jobs inherit --------------------------------- #


def test_regime_is_none_when_no_scan_has_run() -> None:
    """None must not resolve to the permissive answer downstream."""
    ctx = type("Ctx", (), {"repo": Repo(":memory:"), "day": date(2026, 3, 2)})()
    assert _regime_on_file(ctx) is None


def test_regime_round_trips_from_what_evening_recorded() -> None:
    repo = Repo(":memory:")
    repo.record_equity(date(2026, 3, 2), cash=1.0, equity=1.0, regime="chop")
    ctx = type("Ctx", (), {"repo": repo, "day": date(2026, 3, 5)})()

    assert _regime_on_file(ctx) is Regime.CHOP


def test_an_unrecognised_regime_declines_rather_than_guesses() -> None:
    """Schema drift is not a trading signal."""
    repo = Repo(":memory:")
    repo.record_equity(date(2026, 3, 2), cash=1.0, equity=1.0, regime="sideways?")
    ctx = type("Ctx", (), {"repo": repo, "day": date(2026, 3, 5)})()

    assert _regime_on_file(ctx) is None


def test_the_shipped_policy_still_opens_only_in_trend() -> None:
    """The frozen config is what the holdout validated. If this ever fails, the
    live system is no longer the tested one (PLAN 5d)."""
    policy = Policy.from_yaml("config/policy.yaml")

    assert policy.may_open_in("trend")
    assert not policy.may_open_in("chop")
    assert not policy.may_open_in("high_vol")


def test_the_frozen_policy_matches_the_version_it_claims() -> None:
    policy = Policy.from_yaml("config/policy.yaml")
    assert policy.version == "v2-holdout"
    assert policy.max_risk_per_trade == pytest.approx(0.016)
    assert policy.max_portfolio_heat == pytest.approx(0.096)


def test_local_midnight_does_not_move_the_trading_day() -> None:
    """End to end for the operator's actual situation: running at 00:30 local
    still means the previous New York session."""
    late = datetime(2026, 8, 29, 0, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert today_exchange(late) == date(2026, 8, 28)
