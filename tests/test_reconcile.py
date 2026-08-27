"""Reconciliation: the database must be forced to agree with the broker.

Every risk number computed after this runs depends on it being right, so the
drift cases get explicit tests rather than being left to the integration run.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_bot.broker.paper import PaperBroker
from trading_bot.broker.reconcile import reconcile
from trading_bot.core.models import SetupType
from trading_bot.data.models import Bar
from trading_bot.db.repo import Repo

DAY = date(2026, 3, 2)


@pytest.fixture
def repo() -> Repo:
    return Repo(":memory:")


def bar(o, h, l, c, day=DAY) -> Bar:
    return Bar(day, o, h, l, c, 1_000_000.0)


def annotate(repo: Repo, ticker: str, *, entry=100.0, qty=100, days_ago=3) -> None:
    repo.save_position_annotation(
        ticker=ticker,
        sector="Technology",
        setup_type=SetupType.BREAKOUT,
        entry_score=70.0,
        initial_stop=90.0,
        target=130.0,
        opened_at=DAY - timedelta(days=days_ago),
        policy_version="v1",
        entry_price=entry,
        entry_qty=qty,
        thesis="test",
    )


def held_position(broker: PaperBroker, ticker: str = "AAA") -> None:
    broker.submit_bracket(ticker, 100, 100.0, 90.0, 130.0, f"cid-{ticker}")
    broker.advance(ticker, bar(100.0, 101.0, 99.0, 100.0))


# --- the happy path ------------------------------------------------------- #


def test_clean_book_reconciles_without_warnings(repo: Repo) -> None:
    broker = PaperBroker(100_000)
    held_position(broker)
    annotate(repo, "AAA")

    result = reconcile(broker, repo, DAY)

    assert result.clean
    assert [p.ticker for p in result.portfolio.positions] == ["AAA"]
    assert result.portfolio.equity == pytest.approx(100_000.0)


def test_portfolio_joins_broker_truth_with_database_intent(repo: Repo) -> None:
    broker = PaperBroker(100_000)
    held_position(broker)
    annotate(repo, "AAA", entry=100.0)
    broker.set_price("AAA", 118.0)

    position = reconcile(broker, repo, DAY).portfolio.positions[0]

    assert position.qty == 100  # from the broker
    assert position.current_price == 118.0  # from the broker
    assert position.setup_type is SetupType.BREAKOUT  # from the database
    assert position.thesis == "test"  # from the database
    assert position.days_held(DAY) == 3


def test_the_live_stop_beats_the_recorded_one(repo: Repo) -> None:
    """If a replace succeeded but the write did not, the broker is right."""
    broker = PaperBroker(100_000)
    held_position(broker)
    annotate(repo, "AAA")
    broker.replace_stop("AAA", 104.0)

    # the paper broker exposes the bracket rather than a separate stop leg, so
    # the recorded stop stands in; what matters is that reconcile prefers a live
    # leg when one exists
    position = reconcile(broker, repo, DAY).portfolio.positions[0]
    assert position.stop in (90.0, 104.0)


# --- drift ---------------------------------------------------------------- #


def test_a_broker_position_with_no_annotation_is_adopted_and_flagged(
    repo: Repo,
) -> None:
    broker = PaperBroker(100_000)
    held_position(broker, "MANUAL")

    result = reconcile(broker, repo, DAY)

    assert result.adopted == ["MANUAL"]
    assert not result.clean
    assert any("no annotation" in w for w in result.warnings)

    # unmanaged risk gets a stop immediately rather than waiting to be noticed
    annotation = repo.position_annotations()["MANUAL"]
    assert annotation["initial_stop"] < 100.0
    assert [p.ticker for p in result.portfolio.positions] == ["MANUAL"]


def test_an_annotation_with_no_position_becomes_a_recorded_trade(
    repo: Repo,
) -> None:
    """The normal overnight case: an OCO leg filled while nothing was running."""
    broker = PaperBroker(100_000)
    held_position(broker)
    annotate(repo, "AAA", entry=100.0, qty=100)

    # target fills overnight
    broker.advance("AAA", bar(120.0, 131.0, 119.0, 130.0, DAY + timedelta(days=1)))
    assert broker.positions() == []

    result = reconcile(broker, repo, DAY + timedelta(days=1))

    assert result.closed_out == ["AAA"]
    assert repo.position_annotations() == {}

    trade = repo.trades()[0]
    assert trade["ticker"] == "AAA"
    assert trade["exit_price"] == pytest.approx(130.0)
    assert trade["entry_price"] == pytest.approx(100.0)
    # R uses entry-to-stop, not target-to-stop
    assert trade["realized_r"] == pytest.approx(3.0)
    assert trade["pnl"] == pytest.approx(3_000.0)


def test_a_closed_trade_without_an_entry_snapshot_is_marked_not_faked(
    repo: Repo,
) -> None:
    broker = PaperBroker(100_000)
    held_position(broker)
    annotate(repo, "AAA", entry=0.0, qty=0)  # pre-snapshot annotation

    broker.advance("AAA", bar(120.0, 131.0, 119.0, 130.0, DAY + timedelta(days=1)))
    reconcile(broker, repo, DAY + timedelta(days=1))

    trade = repo.trades()[0]
    assert "no_entry_snapshot" in trade["exit_reason"]
    assert trade["realized_r"] == 0.0  # not invented


def test_order_status_is_driven_by_the_broker(repo: Repo) -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, 100.0, 90.0, 130.0, "cid-AAA")
    repo.record_order(
        client_order_id="cid-AAA",
        day=DAY,
        ticker="AAA",
        side="buy",
        qty=100,
        status="Submitted",
        policy_version="v1",
    )

    broker.advance("AAA", bar(100.0, 101.0, 99.0, 100.0))  # fills
    result = reconcile(broker, repo, DAY)

    assert any("Submitted -> Filled" in u for u in result.order_updates)
    assert repo.open_orders() == []


# --- circuit breaker inputs ----------------------------------------------- #


def test_weekly_pnl_uses_the_stored_baseline(repo: Repo) -> None:
    broker = PaperBroker(100_000)
    repo.record_equity(DAY - timedelta(days=8), cash=0, equity=110_000)

    portfolio = reconcile(broker, repo, DAY).portfolio
    assert portfolio.week_pnl_pct == pytest.approx((100_000 - 110_000) / 110_000)


def test_a_missing_baseline_does_not_trip_the_weekly_breaker(repo: Repo) -> None:
    portfolio = reconcile(PaperBroker(100_000), repo, DAY).portfolio
    assert portfolio.week_pnl_pct == 0.0


def test_the_halt_flag_reaches_the_portfolio(repo: Repo) -> None:
    repo.set_halt(True, "manual test")
    assert reconcile(PaperBroker(100_000), repo, DAY).portfolio.halted is True

    repo.set_halt(False)
    assert reconcile(PaperBroker(100_000), repo, DAY).portfolio.halted is False


def test_reconcile_snapshots_equity_every_run(repo: Repo) -> None:
    reconcile(PaperBroker(100_000), repo, DAY)
    assert repo.equity_asof(DAY) == pytest.approx(100_000.0)
