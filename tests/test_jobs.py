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
from trading_bot.core.models import (
    ActionKind,
    Candidate,
    EntryType,
    EventFlags,
    Regime,
    SetupType,
)
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

    close_job.run(ctx, prices={}, regime=Regime.TREND)

    rows = ctx.repo.conn.execute(
        "SELECT * FROM shadow_book WHERE not_taken_reason = 'unfilled'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "S00"


def test_evening_refuses_a_session_that_has_not_closed(ctx) -> None:
    """The provisional-bar guard.

    A bar exists from the opening bell, so the staleness check passes and the
    scan proceeds against a close that is really a moving quote. A future date
    stands in for "today, mid-session": both are sessions that have not settled.
    """
    future = date(2027, 6, 16)
    assert future.weekday() < 5
    result = evening.run(replace(ctx, day=future))

    assert result.status == "error"
    assert "SESSION STILL OPEN" in " ".join(result.notes)


def test_evening_skips_a_non_session_day(ctx) -> None:
    saturday = date(2026, 3, 7)
    assert saturday.weekday() == 5
    result = evening.run(replace(ctx, day=saturday))
    assert result.status == "skipped"
    # A skip is recorded rather than silent: otherwise the dashboard's staleness
    # indicator false-alarms across every long holiday weekend.
    row = ctx.repo.last_run("evening")
    assert row is not None and row["status"] == "skipped"


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

    result = open_job.run(replace(ctx, day=next_day), Regime.TREND)

    assert result.status == "ok"
    submitted = ctx.broker.orders(open_only=True)
    if submitted:
        assert all(o.side == "buy" for o in submitted)
        # every entry carries a recorded annotation and order row
        for order in submitted:
            assert ctx.repo.order_exists(order.client_order_id)
            assert order.ticker in ctx.repo.position_annotations()


def test_open_does_not_enter_when_the_regime_is_untradeable(ctx) -> None:
    """The 2026-08-27 bug, exactly.

    The evening scan measured `chop`, correctly opened nothing, and still wrote
    113 candidates as Pending. The next morning this job ran with a hardcoded
    TREND and would have entered all of them -- trading the one regime the
    frozen policy excludes, which no backtest ever validated.
    """
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)
    assert ctx.repo.pending_candidates(ctx.day), "fixture must offer candidates"

    # The shipped config restricts opens to `trend`; the library default stays
    # permissive so the research baseline is unchanged, so the restriction has
    # to be stated here or this test would pass for the wrong reason.
    shipped = replace(
        ctx,
        day=next_day,
        policy=ctx.policy.with_changes(tradeable_regimes=["trend"]),
    )
    assert not shipped.policy.may_open_in("chop")

    result = open_job.run(shipped, Regime.CHOP)

    assert result.status == "ok"
    assert [a for a in result.executed if a.kind is ActionKind.OPEN] == []
    assert all(o.side != "buy" for o in ctx.broker.orders(open_only=True))


def test_open_declines_entries_when_no_regime_is_known(ctx) -> None:
    """Unknown must not resolve to the permissive answer. A missing regime means
    the evening scan has not run, so nothing has been classified from settled
    data -- and entering on that basis is a guess."""
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    result = open_job.run(replace(ctx, day=next_day), None)

    assert result.status == "ok", "declining to enter is not an error"
    assert [a for a in result.executed if a.kind is ActionKind.OPEN] == []
    assert any("NO REGIME" in n for n in result.notes)


def test_close_declines_entries_when_no_regime_is_known(ctx, monkeypatch) -> None:
    evening.run(ctx)
    pending = ctx.repo.pending_candidates(ctx.day)
    forced = [replace(c, entry_type=EntryType.CLOSE_CONFIRM) for c in pending]
    monkeypatch.setattr(ctx.repo, "pending_candidates", lambda _d: forced)

    result = close_job.run(
        ctx, prices={c.ticker: c.entry for c in forced}, regime=None
    )

    assert [a for a in result.executed if a.kind is ActionKind.OPEN] == []
    assert any("NO REGIME" in n for n in result.notes)


def test_the_intraday_jobs_can_read_the_regime_the_evening_scan_recorded(
    ctx,
) -> None:
    """The two halves have to meet: evening persists it, the CLI reads it back."""
    evening.run(ctx)
    recorded = ctx.repo.last_regime(ctx.day)

    assert recorded is not None, "evening must persist what it classified"
    assert Regime(recorded) in set(Regime)
    # and it is still findable from a later session, since 10:00 today inherits
    # the regime measured off yesterday's settled closes
    assert ctx.repo.last_regime(ctx.day + timedelta(days=3)) == recorded


def test_open_defers_close_confirmed_breakouts(ctx, monkeypatch) -> None:
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    pending = ctx.repo.pending_candidates(ctx.day)
    forced = [replace(c, entry_type=EntryType.CLOSE_CONFIRM) for c in pending]
    monkeypatch.setattr(ctx.repo, "pending_candidates", lambda _d: forced)

    result = open_job.run(replace(ctx, day=next_day), Regime.TREND)
    assert ctx.broker.orders(open_only=True) == []
    assert any("deferred" in n for n in result.notes)


def test_open_is_idempotent(ctx) -> None:
    """A cron retry must not double the position."""
    evening.run(ctx)
    next_day = ctx.day + timedelta(days=1)

    first = open_job.run(replace(ctx, day=next_day), Regime.TREND)
    count_after_first = len(ctx.broker.orders(open_only=False))

    second = open_job.run(replace(ctx, day=next_day), Regime.TREND)
    assert second.status == "ok"
    assert len(ctx.broker.orders(open_only=False)) == count_after_first
    if first.executed:
        assert any("already submitted" in n for n in second.notes)


def test_the_kill_switch_stops_entries_but_the_job_still_runs(ctx) -> None:
    evening.run(ctx)
    ctx.repo.set_halt(True, "test")

    result = open_job.run(replace(ctx, day=ctx.day + timedelta(days=1)), Regime.TREND)

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

    result = close_job.run(ctx, prices={}, regime=Regime.TREND)
    assert result.status == "ok"
    assert ctx.broker.orders(open_only=True) == []
    assert any("cancelled" in n for n in result.notes)


# --- the whole cycle ------------------------------------------------------ #


def test_the_books_reconcile_across_a_full_cycle(ctx, market) -> None:
    """Every dollar the broker holds is explained by a recorded trade.

        equity == starting equity + sum(recorded pnl) + unrealised on open

    The backtest engine has had this invariant under test from the beginning.
    The live path -- paper broker, reconciler, repo -- did not, and three
    separate defects lived in that gap for as long as it was untested:

    * the executor dropped the position annotation the moment it sent a CLOSE,
      and the reconciler writes trades by finding annotations whose position has
      vanished -- so every deliberate exit (time stop, invalidated thesis,
      rotation) was missing from `trades` entirely;
    * closed trades were booked at the price the order ASKED for rather than the
      price it got, which biases P&L in one direction because a limit only ever
      fills better than its price;
    * an entry that never filled still had an optimistic annotation, and that
      was written up as a loss at the stop -- a fabricated trade, indistinguish-
      able downstream from a real one.

    None of them raised anything. The equity number stayed right, because the
    broker was never wrong; only the record of WHY was.
    """
    universe, _, _ = market
    days = universe["SPY"].days
    start = days.index(ctx.day)
    starting_equity = ctx.broker.account().equity

    evening.run(ctx)

    for offset in range(1, 6):
        day = days[start + offset]
        session = replace(ctx, day=day)

        premarket.run(session, NullSemanticEngine())
        open_job.run(session, Regime.TREND)
        for symbol in universe:
            if symbol != "SPY":
                ctx.broker.advance(symbol, bar_for(universe, symbol, day))

        prices = {
            c.ticker: bar_for(universe, c.ticker, day).close
            for c in ctx.repo.pending_candidates(ctx.repo.latest_candidate_day(day))
            if c.ticker in universe
        }
        close_job.run(session, prices=prices, regime=Regime.TREND)
        ctx.broker.roll_day()
        evening.run(session)

        realised = sum(t["pnl"] for t in ctx.repo.trades())
        unrealised = sum(
            (bar_for(universe, p.ticker, day).close - p.avg_entry_price) * p.qty
            for p in ctx.broker.positions()
            if p.ticker in universe
        )
        equity = ctx.broker.account().equity

        assert equity == pytest.approx(
            starting_equity + realised + unrealised, abs=1.0
        ), (
            f"{day}: broker holds ${equity:,.2f} but the record explains "
            f"${starting_equity + realised + unrealised:,.2f} "
            f"(realised {realised:,.2f}, unrealised {unrealised:,.2f})"
        )


def test_every_exit_is_recorded_exactly_once(ctx, market) -> None:
    """A trade the broker closed must appear in `trades` -- once, not zero times
    and not twice. Attribution, the shadow book and the tuning proposals all
    read that table, so a gap there is invisible and permanent."""
    universe, _, _ = market
    days = universe["SPY"].days
    start = days.index(ctx.day)

    exits: list[str] = []
    original = type(ctx.broker)._liquidate

    def spy(self, ticker, price, reason):
        exits.append(ticker)
        return original(self, ticker, price, reason)

    type(ctx.broker)._liquidate = spy
    try:
        evening.run(ctx)
        for offset in range(1, 6):
            day = days[start + offset]
            session = replace(ctx, day=day)
            premarket.run(session, NullSemanticEngine())
            open_job.run(session, Regime.TREND)
            for symbol in universe:
                if symbol != "SPY":
                    ctx.broker.advance(symbol, bar_for(universe, symbol, day))
            close_job.run(session, prices={}, regime=Regime.TREND)
            ctx.broker.roll_day()
            evening.run(session)
    finally:
        type(ctx.broker)._liquidate = original

    recorded = [t["ticker"] for t in ctx.repo.trades()]
    still_open = {p.ticker for p in ctx.broker.positions()}

    assert sorted(recorded) == sorted(exits), (
        f"broker closed {sorted(exits)} but {sorted(recorded)} was recorded"
    )
    assert not (set(recorded) & still_open), "recorded a trade for a live position"


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
        open_job.run(session, Regime.TREND)

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
        close_job.run(session, prices=prices, regime=Regime.TREND)
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


def test_close_does_not_cancel_the_entry_it_just_submitted(ctx, monkeypatch) -> None:
    """A marketable limit is briefly "unfilled" between submission and the fill
    being reported. Sweeping it there would mean the close-confirmation job
    could never open a position at all."""
    evening.run(ctx)
    day = ctx.day
    pending = ctx.repo.pending_candidates(day)
    forced = [replace(c, entry_type=EntryType.CLOSE_CONFIRM) for c in pending]
    monkeypatch.setattr(ctx.repo, "pending_candidates", lambda _d: forced)

    prices = {c.ticker: c.entry for c in forced}
    result = close_job.run(ctx, prices=prices, regime=Regime.TREND)

    opened = [a for a in result.executed if a.kind.value == "OPEN"]
    assert opened, "expected at least one confirmed breakout"
    live = {o.ticker for o in ctx.broker.orders(open_only=True)}
    assert {a.ticker for a in opened} <= live


# --- stale data must never look like a quiet market ----------------------- #


def test_evening_fails_loudly_when_the_cache_is_stale(ctx, market) -> None:
    """The scanner matches session dates exactly, so an unrefreshed cache makes
    every symbol invisible and the scan returns a clean zero. Indistinguishable
    from "no setups today" unless the job says so."""
    universe, _, _ = market
    future = universe["SPY"].days[-1] + timedelta(days=4)
    while future.weekday() >= 5:  # must be a session, or the job just skips
        future += timedelta(days=1)

    result = evening.run(replace(ctx, day=future))

    assert result.status == "error"
    assert any("STALE DATA" in n for n in result.notes)
    assert ctx.repo.latest_candidate_day() is None  # nothing written


def test_open_declines_a_stale_watchlist(ctx) -> None:
    """If the evening job has been failing, its entry prices are days old."""
    evening.run(ctx)
    assert ctx.repo.pending_candidates(ctx.day)

    much_later = ctx.day + timedelta(days=open_job.MAX_WATCHLIST_AGE_DAYS + 3)
    result = open_job.run(replace(ctx, day=much_later), Regime.TREND)

    assert any("STALE WATCHLIST" in n for n in result.notes)
    assert ctx.broker.orders(open_only=True) == []


def test_a_fresh_watchlist_is_still_acted_on(ctx) -> None:
    evening.run(ctx)
    result = open_job.run(replace(ctx, day=ctx.day + timedelta(days=1)), Regime.TREND)
    assert not any("STALE WATCHLIST" in n for n in result.notes)
