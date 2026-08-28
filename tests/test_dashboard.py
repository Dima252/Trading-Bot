"""The dashboard.

It is read-only and it is the thing a human looks at to decide whether the agent
is behaving, so the tests care about two properties above all: it must render
from an empty database without blowing up, and it must never be able to place an
order.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import make_candidate
from trading_bot.core.models import SetupType
from trading_bot.core.policy import Policy
from trading_bot.data.cache import BarCache
from trading_bot.data.models import Bar, BarSeries
from trading_bot.db.repo import Repo
from trading_bot.ui import dashboard

DAY = date(2025, 6, 10)


@pytest.fixture
def repo() -> Repo:
    return Repo(":memory:")


@pytest.fixture
def cache(tmp_path) -> BarCache:
    c = BarCache(tmp_path / "bars.db")
    bars = [
        Bar(DAY - timedelta(days=30 - i), 400 + i, 402 + i, 398 + i, 400 + i, 1e6)
        for i in range(30)
    ]
    c.store(BarSeries("SPY", bars))
    return c


def populate(repo: Repo) -> None:
    repo.save_candidates(DAY, [make_candidate("AAA"), make_candidate("BBB")], "v1")
    repo.set_candidate_status(DAY, "BBB", "Cancelled", "earnings 2025-06-14")
    repo.save_position_annotation(
        ticker="AAA",
        sector="Technology",
        setup_type=SetupType.BREAKOUT,
        entry_score=72.0,
        initial_stop=90.0,
        target=130.0,
        opened_at=DAY - timedelta(days=6),
        policy_version="v1",
        entry_price=100.0,
        entry_qty=120,
        thesis="breakout in trend: score 72.0",
    )
    for i in range(25):
        repo.record_equity(
            DAY - timedelta(days=25 - i),
            cash=50_000,
            equity=100_000 + i * 120,
            positions=3,
            heat_pct=0.031,
            regime="trend",
        )
    run_id = repo.start_run("evening", DAY)
    repo.finish_run(run_id, "ok", "1xOPEN")
    repo.record_trade(
        ticker="ZZZ",
        sector="Energy",
        setup_type="pullback",
        regime_at_entry="trend",
        entry_day=(DAY - timedelta(days=12)).isoformat(),
        entry_price=50.0,
        exit_day=DAY.isoformat(),
        exit_price=56.0,
        qty=100,
        initial_stop=48.0,
        target=56.0,
        exit_reason="target",
        realized_r=3.0,
        pnl=600.0,
        mfe_r=3.1,
        mae_r=-0.4,
        days_held=12,
        entry_score=68.0,
        policy_version="v1",
    )


# --- robustness ----------------------------------------------------------- #


def test_renders_from_an_empty_database(repo: Repo) -> None:
    """Day one, before anything has run at all."""
    html = dashboard.render(repo)
    assert "<!doctype html>" in html
    assert "No open positions." in html
    assert "No equity history yet." in html
    assert "never run" in html  # every job flagged as not yet fired


def test_renders_a_populated_database(repo: Repo, cache: BarCache) -> None:
    populate(repo)
    html = dashboard.render(repo, cache, Policy(), as_of=DAY)

    assert "AAA" in html
    assert "breakout in trend" in html  # the thesis survives to the page
    assert "earnings 2025-06-14" in html  # and so does a cancellation reason
    assert "Decision log" in html


def test_writes_a_self_contained_file(repo: Repo, cache: BarCache, tmp_path) -> None:
    populate(repo)
    out = dashboard.write(repo, tmp_path / "sub" / "d.html", cache, as_of=DAY)
    text = out.read_text(encoding="utf-8")

    assert out.exists()
    # nothing may be fetched from outside the file
    for marker in ("http://", "https://", "<script", "src=", "@import"):
        assert marker not in text, f"external asset or script: {marker}"


# --- the parts that matter most ------------------------------------------- #


def test_a_stale_job_is_flagged(repo: Repo) -> None:
    """A cron job that silently stopped firing is the likeliest failure of the
    whole system, so it must be visible rather than buried."""
    run_id = repo.start_run("evening", DAY - timedelta(days=30))
    repo.finish_run(run_id, "ok")

    html = dashboard.render(repo, as_of=DAY)
    assert "30d ago" in html
    assert 'class="job warn"' in html


def test_a_failed_job_is_flagged(repo: Repo) -> None:
    run_id = repo.start_run("open", DAY)
    repo.finish_run(run_id, "error", "boom")
    html = dashboard.render(repo, as_of=DAY)
    assert 'class="job bad"' in html


def test_the_kill_switch_is_visible(repo: Repo) -> None:
    repo.set_halt(True, "investigating")
    html = dashboard.render(repo, as_of=DAY)
    assert "ENGAGED" in html
    assert "investigating" in html


def test_vetoed_actions_appear_with_the_rule_that_fired(repo: Repo) -> None:
    """The decision log is the centrepiece: a veto with no reason is useless."""
    from trading_bot.core.models import Action, ActionKind, Rejection

    run_id = repo.start_run("evening", DAY)
    action = Action(ActionKind.OPEN, "XYZ", qty=10, limit=100.0, stop=90.0)
    repo.record_decisions(
        run_id, DAY, [], [Rejection(action, "max_portfolio_heat", "would reach 7.2%")]
    )

    html = dashboard.render(repo, as_of=DAY)
    assert "VETOED" in html
    assert "max_portfolio_heat" in html
    assert "would reach 7.2%" in html


def test_content_is_escaped(repo: Repo) -> None:
    """Thesis and reason strings are free text and end up in the page."""
    repo.save_position_annotation(
        ticker="XSS",
        sector="Technology",
        setup_type=SetupType.BREAKOUT,
        entry_score=50.0,
        initial_stop=90.0,
        target=130.0,
        opened_at=DAY,
        policy_version="v1",
        entry_price=100.0,
        entry_qty=1,
        thesis="<script>alert(1)</script>",
    )
    html = dashboard.render(repo, as_of=DAY)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_page_cannot_touch_the_broker() -> None:
    """It is a read-only view; nothing in it should reach an order path."""
    import inspect

    source = inspect.getsource(dashboard)
    for forbidden in ("submit_bracket", "close_position", "cancel_order", "Broker"):
        assert forbidden not in source
