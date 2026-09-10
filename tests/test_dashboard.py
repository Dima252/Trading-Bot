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
    # Nothing may be FETCHED from outside the file. An anchor is fine -- it
    # loads nothing until someone clicks it, and a page meant to be linked
    # from elsewhere should be able to link back.
    for marker in ("<script", "src=", "@import", "<link", "url("):
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


def test_the_regime_survives_a_failed_run(tmp_path) -> None:
    """The tile read the LAST equity row, and a run that errors writes one with
    a null regime -- so it showed "—" on exactly the day the operator most needs
    it. On a chop day the regime is the entire explanation for why the bot did
    nothing, and a dash reads as "broken" rather than "correctly sitting out".
    """
    repo = Repo(":memory:")
    repo.record_equity(DAY, cash=100_000, equity=100_000, regime="chop")
    repo.record_equity(DAY + timedelta(days=1), cash=100_000, equity=100_000)

    page = dashboard.render(repo, None, Policy(), as_of=DAY + timedelta(days=1))

    assert ">chop<" in page


def test_the_heat_limit_is_not_rounded_away(tmp_path) -> None:
    """The shipped cap is 9.6%. Displaying it as "10%" misstates a risk limit
    the operator is reading in order to trust it."""
    repo = Repo(":memory:")
    repo.record_equity(DAY, cash=100_000, equity=100_000, heat_pct=0.0)
    policy = Policy.from_yaml("config/policy.yaml")

    page = dashboard.render(repo, None, policy, as_of=DAY)

    assert "9.6%" in page
    assert "/ 10%" not in page


# --- the presentation layer ----------------------------------------------- #


def test_the_balance_is_the_headline(repo: Repo, cache: BarCache) -> None:
    """It is the number anyone opens the page for, and it used to be one tile
    among six at the same size as `heat`."""
    populate(repo)
    html = dashboard.render(repo, cache, as_of=DAY)

    assert 'class="headline"' in html
    i = html.index('class="headline"')
    assert "$" in html[i:i + 200], "the headline should carry a money figure"


def test_the_headline_shows_the_change_since_inception(repo: Repo) -> None:
    repo.record_equity(DAY - timedelta(days=10), cash=0, equity=100_000)
    repo.record_equity(DAY, cash=0, equity=110_000)

    html = dashboard.render(repo, None, as_of=DAY)
    assert "+10.00%" in html


def test_an_empty_database_still_renders_a_headline(repo: Repo) -> None:
    """The page must survive a host that has never run a job."""
    html = dashboard.render(repo, None, as_of=DAY)
    assert 'class="headline"' in html
    assert "Trading Bot" in html


def test_the_page_stays_self_contained_after_restyling(
    repo: Repo, cache: BarCache, tmp_path
) -> None:
    """The design has no font link and no external asset on purpose: the page
    has to open over file:// or scp with no network."""
    populate(repo)
    out = dashboard.write(repo, tmp_path / "d.html", cache, as_of=DAY)
    text = out.read_text(encoding="utf-8")

    for marker in ("<script", "src=", "@import", "<link", "fonts.", "url("):
        assert marker not in text, f"external dependency: {marker}"


def test_both_themes_define_every_colour(repo: Repo) -> None:
    """A token defined only inside the dark block renders one theme's text on
    the other theme's ground."""
    html = dashboard.render(repo, None, as_of=DAY)
    light = html[html.index(":root{"):html.index("@media (prefers-color-scheme:dark)")]
    dark = html[html.index("@media (prefers-color-scheme:dark)"):]
    dark = dark[:dark.index("}}")]

    def names(block: str) -> set[str]:
        return {
            line.split(":")[0].strip()
            for line in block.replace("{", ";").replace("}", ";").split(";")
            if line.strip().startswith("--")
        }

    assert names(light) == names(dark), "the two palettes must define the same tokens"


def test_a_stale_page_says_so(repo: Repo) -> None:
    """A visitor arriving from a link reads whatever is on the page as what the
    bot is doing now. Before the first scheduled run it renders a replay, and
    "as of <date>" in small type is not enough to correct that."""
    old = date(2020, 1, 6)
    repo.record_equity(old, cash=50_000, equity=104_452)

    html = dashboard.render(repo, None, as_of=old)
    assert "replay, not current activity" in html


def test_a_current_page_carries_no_such_warning(repo: Repo) -> None:
    """The notice must disappear once the schedule is publishing, or it becomes
    noise that gets ignored on the day it matters."""
    from trading_bot.market_hours import today_exchange

    today = today_exchange()
    repo.record_equity(today, cash=50_000, equity=104_452)

    html = dashboard.render(repo, None, as_of=today)
    assert "replay, not current activity" not in html
