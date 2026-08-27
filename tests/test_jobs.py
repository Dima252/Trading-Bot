"""The four jobs, end to end, with no network and no credentials.

This is the test that proves the pieces fit: signal engine -> decide ->
constitution -> broker -> database -> reconcile, on the same code paths the live
system uses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.synthetic import random_universe

from trading_bot.broker.paper import PaperBroker
from trading_bot.core.models import Candidate, EntryType, EventFlags, SetupType
from trading_bot.core.policy import Policy
from trading_bot.data.cache import BarCache
from trading_bot.db.repo import Repo
from trading_bot.jobs import close_job, evening, open_job, premarket
from trading_bot.jobs.base import AgentContext
from trading_bot.semantic.client import NullSemanticEngine


@pytest.fixture
def market(tmp_path):
    universe, sectors = random_universe(n_symbols=10, n_bars=600, seed=3)
    cache = BarCache(tmp_path / "bars.db")
    for series in universe.values():
        cache.store(series)
    return universe, sectors, cache


@pytest.fixture
def ctx(market) -> AgentContext:
    universe, sectors, cache = market
    return AgentContext(
        repo=Repo(":memory:"),
        broker=PaperBroker(100_000.0),
        cache=cache,
        policy=Policy(),
        sectors=dict(sectors),
        day=universe["SPY"].days[450],
    )


def bar_for(universe, symbol, day):
    series = universe[symbol]
    return series[series.index_of(day)]


# --- evening -------------------------------------------------------------- #


def test_evening_writes_a_watchlist_and_trades_nothing(ctx, market) -> None:
    result = evening.run(ctx)

    assert result.status == "ok"
    assert ctx.repo.latest_candidate_day() == ctx.day
    assert ctx.repo.pending_candidates(ctx.day)
    assert ctx.broker.positions() == []  # the market is shut
    assert ctx.broker.orders(open_only=True) == []
    assert any("regime=" in n for n in result.notes)


def test_evening_records_a_run_and_an_equity_snapshot(ctx) -> None:
    evening.run(ctx)
    row = ctx.repo.last_run("evening")
    assert row["status"] == "ok"
    assert ctx.repo.equity_asof(ctx.day) == pytest.approx(100_000.0)


def test_evening_populates_the_shadow_book(ctx) -> None:
    """With no slots available every candidate is declined, so all of them must
    be recorded -- otherwise the filters are unfalsifiable."""
    starved = replace(ctx, policy=ctx.policy.with_changes(max_new_positions_per_day=0))
    evening.run(starved)

    rows = ctx.repo.conn.execute("SELECT * FROM shadow_book").fetchall()
    assert len(rows) == len(ctx.repo.pending_candidates(ctx.day))
    assert rows
    assert all(r["not_taken_reason"] for r in rows)
    assert all(r["outcome"] == "unresolved" for r in rows)


def test_unfilled_entries_land_in_the_shadow_book(ctx) -> None:
    """"Our limit was too low" is a mistake, and an invisible one without this."""
    ctx.repo.record_order(
        client_order_id="cid-x",
        day=ctx.day,
        ticker="S00",
        side="buy",
        qty=10,
        status="Submitted",
        policy_version="v1",
        limit_price=50.0,
        stop_price=45.0,
        target_price=65.0,
    )
    ctx.broker.submit_bracket("S00", 10, 50.0, 45.0, 65.0, "cid-x")

    close_job.run(ctx, prices={})

    rows = ctx.repo.conn.execute(
        "SELECT * FROM shadow_book WHERE not_taken_reason = 'unfilled'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "S00"


def test_evening_skips_a_non_session_day(ctx) -> None:
    saturday = date(2026, 3, 7)
    assert saturday.weekday() == 5
    result = evening.run(replace(ctx, day=saturday))
    assert result.status == "skipped"
    assert ctx.repo.last_run("evening") is None


# --- premarket ------------------------------------------------------------ #


class StubEngine(NullSemanticEngine):
    def __init__(self, earnings=None, verdicts=None):
        self._earnings = earnings or {}
        self._verdicts = verdicts or {}

    def earnings_within(self, tickers, day, days):
        return {t: d for t, d in self._earnings.items() if t in tickers}

    def assess(self, tickers, day):
        return {t: f for t, f in self._verdicts.items() if t in tickers}


def test_premarket_cancels_candidates_with_earnings_in_the_window(ctx) -> None:
    evening.run(ctx)
    pending = ctx.repo.pending_candidates(ctx.day)
    victim = pending[0].ticker

    next_day = ctx.day + timedelta(days=1)
    engine = StubEngine(earnings={victim: next_day + timedelta(days=3)})
    result = premarket.run(replace(ctx, day=next_day), engine)

    assert result.status == "ok"
    survivors = {c.ticker for c in ctx.repo.pending_candidates(ctx.day)}
    assert victim not in survivors
    assert any("earnings" in n for n in result.notes)


def test_premarket_cancels_on_structural_invalidation(ctx) -> None:
    evening.run(ctx)
    victim = ctx.repo.pending_candidates(ctx.day)[0].ticker

    engine = StubEngine(
        verdicts={
            victim: EventFlags(
                structural_invalidation=True,
                confidence="high",
                rationale="guidance withdrawn",
            )
        }
    )
    premarket.run(replace(ctx, day=ctx.day + timedelta(days=1)), engine)

    assert victim not in {c.ticker for c in ctx.repo.pending_candidates(ctx.day)}


def test_premarket_leaves_clean_candidates_alone(ctx) -> None:
    evening.run(ctx)
    before = {c.ticker for c in ctx.repo.pending_candidates(ctx.day)}
    premarket.run(replace(ctx, day=ctx.day + timedelta(days=1)), NullSemanticEngine())
    assert {c.ticker for c in ctx.repo.pending_candidates(ctx.day)} == before


# --- open ----------------------------------------------------------------- #


def test_open_places_resting_entries_only(ctx) -> None:
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    result = open_job.run(replace(ctx, day=next_day))

    assert result.status == "ok"
    submitted = ctx.broker.orders(open_only=True)
    if submitted:
        assert all(o.side == "buy" for o in submitted)
        # every entry carries a recorded annotation and order row
        for order in submitted:
            assert ctx.repo.order_exists(order.client_order_id)
            assert order.ticker in ctx.repo.position_annotations()


def test_open_defers_close_confirmed_breakouts(ctx, monkeypatch) -> None:
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    pending = ctx.repo.pending_candidates(ctx.day)
    forced = [replace(c, entry_type=EntryType.CLOSE_CONFIRM) for c in pending]
    monkeypatch.setattr(ctx.repo, "pending_candidates", lambda _d: forced)

    result = open_job.run(replace(ctx, day=next_day))
    assert ctx.broker.orders(open_only=True) == []
    assert any("deferred" in n for n in result.notes)


def test_open_is_idempotent(ctx) -> None:
    """A cron retry must not double the position."""
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    first = open_job.run(replace(ctx, day=next_day))
    count_after_first = len(ctx.broker.orders(open_only=False))

    second = open_job.run(replace(ctx, day=next_day))
    assert second.status == "ok"
    assert len(ctx.broker.orders(open_only=False)) == count_after_first
    if first.executed:
        assert any("already submitted" in n for n in second.notes)


def test_the_kill_switch_stops_entries_but_the_job_still_runs(ctx) -> None:
    evening.run(ctx)
    ctx.repo.set_halt(True, "test")

    result = open_job.run(replace(ctx, day=ctx.day + timedelta(days=1)))

    assert result.status == "ok"
    assert ctx.broker.orders(open_only=True) == []
    assert any(r.rule == "kill_switch" for r in result.rejected) or not result.rejected


# --- close ---------------------------------------------------------------- #


def make_candidate(ticker="AAA", entry=100.0, stop=90.0, target=130.0) -> Candidate:
    return Candidate(
        ticker=ticker,
        setup_type=SetupType.BREAKOUT,
        entry_type=EntryType.CLOSE_CONFIRM,
        entry=entry,
        stop=stop,
        target=target,
        sector="Technology",
        setup_quality=80.0,
        atr=2.0,
    )


def test_confirmation_lifts_the_entry_to_the_market() -> None:
    """A buy limit at last night's trigger sits below the market and never fills."""
    confirmed, skipped = close_job.confirm(
        [make_candidate(entry=100.0)], {"AAA": 101.0}, max_chase=0.02
    )
    assert not skipped
    assert confirmed[0].entry == 101.0
    assert confirmed[0].stop == 90.0  # structural, does not move


def test_confirmation_rejects_a_level_that_did_not_hold() -> None:
    _, skipped = close_job.confirm(
        [make_candidate(entry=100.0)], {"AAA": 98.0}, max_chase=0.02
    )
    assert "back below the trigger" in skipped["AAA"]


def test_confirmation_refuses_to_chase_an_extended_move() -> None:
    _, skipped = close_job.confirm(
        [make_candidate(entry=100.0)], {"AAA": 108.0}, max_chase=0.02
    )
    assert "extended" in skipped["AAA"]


def test_confirmation_needs_a_live_price() -> None:
    _, skipped = close_job.confirm([make_candidate()], {}, max_chase=0.02)
    assert skipped["AAA"] == "no live price"


def test_close_cancels_unfilled_entry_orders(ctx) -> None:
    ctx.broker.submit_bracket("S00", 10, 50.0, 45.0, 65.0, "cid-x")
    assert ctx.broker.orders(open_only=True)

    result = close_job.run(ctx, prices={})
    assert result.status == "ok"
    assert ctx.broker.orders(open_only=True) == []
    assert any("cancelled" in n for n in result.notes)


# --- the whole cycle ------------------------------------------------------ #


def test_a_full_day_cycle_keeps_the_database_and_broker_in_agreement(
    ctx, market
) -> None:
    universe, _, _ = market
    days = universe["SPY"].days
    start = days.index(ctx.day)

    evening.run(ctx)

    for offset in range(1, 6):
        day = days[start + offset]
        session = replace(ctx, day=day)

        premarket.run(session, NullSemanticEngine())
        open_job.run(session)

        # walk the market forward so resting orders fill and OCO legs fire
        for symbol in universe:
            if symbol == "SPY":
                continue
            ctx.broker.advance(symbol, bar_for(universe, symbol, day))

        prices = {
            c.ticker: bar_for(universe, c.ticker, day).close
            for c in ctx.repo.pending_candidates(ctx.repo.latest_candidate_day(day))
            if c.ticker in universe
        }
        close_job.run(session, prices=prices)
        ctx.broker.roll_day()
        evening.run(session)

    # the invariant that matters: every broker position has an annotation, and
    # every annotation the broker cannot confirm has been written off as a trade
    final = replace(ctx, day=days[start + 6])
    from trading_bot.broker.reconcile import reconcile

    result = reconcile(final.broker, final.repo, final.day)
    broker_tickers = {p.ticker for p in final.broker.positions()}
    annotated = set(final.repo.position_annotations())

    assert broker_tickers == annotated
    assert result.portfolio.equity > 0
    assert all(r["status"] in ("ok", "skipped") for r in _runs(final.repo))


def _runs(repo: Repo):
    return repo.conn.execute("SELECT * FROM runs").fetchall()
