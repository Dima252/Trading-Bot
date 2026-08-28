"""18:00 ET -- the main think.

Reconcile, review every holding, scan, score everything on one scale, and write
tomorrow's candidates. Executes nothing: the market is shut, and an order sent
now would queue into an opening auction we have not priced.

The defensive intents this job produces are persisted as decisions and re-derived
by the 10:00 job against live prices, rather than being fired blind at the open.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..backtest.simulate import simulate_forward
from ..core.decide import decide
from ..core.scoring import rank_candidates
from ..data.universe import BENCHMARK, liquidity_screen
from ..market_hours import SESSION_CLOSE, now_exchange, session_is_final
from ..signals.engine import MIN_HISTORY, Indicators, scan
from ..signals.regime import breadth_of, classify
from .base import AgentContext, JobResult, constrain, run_job

NAME = "evening"
LOOKBACK_DAYS = 500


def run(ctx: AgentContext) -> JobResult:
    return run_job(NAME, ctx, _body)


def _body(ctx: AgentContext, result: JobResult, recon, run_id: int) -> None:
    # First, and before loading 500 days of history for 500 symbols: if the
    # session has not closed, nothing downstream can be trusted. A bar exists
    # from the opening bell onward with a "close" that is only the last trade,
    # and the staleness guard further down cannot tell the difference -- the bar
    # is present, so it passes. The two failures also want opposite responses:
    # a stale cache says "run fetch", an open session says "do not run fetch
    # yet", because fetching now is what stores the provisional bar.
    if not session_is_final(ctx.day):
        result.status = "error"
        result.note(
            f"SESSION STILL OPEN: {ctx.day} has not closed "
            f"({now_exchange():%H:%M} ET, bell at {SESSION_CLOSE:%H:%M}). "
            "Scanning now would price today off a moving quote. Wait for the "
            "close, then run `fetch` and re-run this job."
        )
        return

    start = ctx.day - timedelta(days=LOOKBACK_DAYS)
    symbols = ctx.cache.symbols()
    universe = ctx.cache.load_many(symbols, start, ctx.day, min_bars=MIN_HISTORY)

    if BENCHMARK not in universe:
        result.status = "error"
        result.note(f"benchmark {BENCHMARK} missing from the cache -- run `fetch`")
        return

    benchmark = universe[BENCHMARK]

    # The scanner matches bars to the session date EXACTLY, so a cache that has
    # not been refreshed past today makes every symbol invisible and the scan
    # returns a clean, silent zero. That is indistinguishable from "no setups
    # today" in the logs, and over months it would look like a quiet market
    # rather than a broken pipeline. Fail loudly instead.
    if benchmark.index_of(ctx.day) is None:
        last = benchmark[-1].day if len(benchmark) else "never"
        result.status = "error"
        result.note(
            f"STALE DATA: no {BENCHMARK} bar for {ctx.day} (cache ends {last}). "
            "Run `fetch` after the close and before this job -- scanning now "
            "would silently find nothing."
        )
        return

    tradeable = {s: b for s, b in universe.items() if s != BENCHMARK}

    liquid = set(liquidity_screen(tradeable, ctx.day))
    tradeable = {s: b for s, b in tradeable.items() if s in liquid}
    result.note(f"{len(tradeable)} of {len(universe) - 1} names pass the liquidity screen")

    context = classify(
        benchmark,
        ctx.day,
        Indicators.compute(benchmark),
        breadth=breadth_of(tradeable, ctx.day),
    )
    result.note(
        f"regime={context.regime.value} breadth={context.breadth:.0%} "
        f"vol={context.volatility_pct:.1%}"
    )

    candidates = scan(tradeable, ctx.day, ctx.sectors, policy=ctx.policy)
    ranked = rank_candidates(candidates, context, ctx.policy)
    result.note(f"{len(ranked)} setups found")

    portfolio = recon.portfolio
    actions = decide(portfolio, ranked, context, ctx.policy)
    approved, rejected = constrain(ctx, actions, portfolio)
    result.rejected = rejected

    ctx.repo.record_decisions(run_id, ctx.day, approved, rejected)
    ctx.repo.save_candidates(ctx.day, ranked, ctx.policy.version)
    ctx.repo.record_equity(
        ctx.day,
        cash=portfolio.cash,
        equity=portfolio.equity,
        positions=len(portfolio.positions),
        heat_pct=portfolio.open_heat_pct,
        regime=context.regime.value,
    )

    planned = [a for a in approved if a.kind.value in ("OPEN", "CLOSE", "ADJUST_STOP")]
    for action in planned:
        result.note(f"planned {action.kind.value} {action.ticker}: {action.reason}")

    _record_shadow(ctx, result, ranked, approved, rejected, portfolio, context)
    _resolve_shadow(ctx, result)


def _record_shadow(
    ctx: AgentContext,
    result: JobResult,
    ranked,
    approved,
    rejected,
    portfolio,
    context,
) -> None:
    """Log every candidate we are NOT taking, and why.

    The outcome is filled in later by `_resolve_shadow`. An agent's mistakes
    include the trades it wrongly skipped, and those are invisible unless
    recorded deliberately.
    """
    intended = {a.ticker for a in approved if a.kind.value == "OPEN"}
    vetoed = {r.action.ticker: r.rule for r in rejected}
    held = portfolio.tickers

    written = 0
    for cand in ranked:
        if cand.ticker in intended:
            continue
        if cand.ticker in held:
            reason = "already_held"
        elif cand.ticker in vetoed:
            reason = vetoed[cand.ticker]
        elif cand.event_flags.penalty >= 1.0:
            reason = "event_veto"
        elif cand.score < ctx.policy.min_candidate_score:
            reason = "below_score_floor"
        else:
            reason = "ranked_out"

        ctx.repo.record_shadow(
            day=ctx.day.isoformat(),
            ticker=cand.ticker,
            sector=cand.sector,
            setup_type=cand.setup_type.value,
            regime=context.regime.value,
            score=round(cand.score, 2),
            entry=cand.entry,
            stop=cand.stop,
            target=cand.target,
            not_taken_reason=reason,
        )
        written += 1

    if written:
        result.note(f"shadow book: {written} candidates recorded as not taken")


def _resolve_shadow(ctx: AgentContext, result: JobResult) -> None:
    """Score the candidates we declined, once enough time has passed.

    This is what makes the filters falsifiable -- without it there is no way to
    learn whether declining them was right.
    """
    cutoff = ctx.day - timedelta(days=ctx.policy.max_hold_days)
    pending = ctx.repo.unresolved_shadow(cutoff)
    if not pending:
        return

    resolved = 0
    for row in pending:
        series = ctx.cache.load(row["ticker"])
        i = series.index_of(date.fromisoformat(row["day"]))
        if i is None:
            continue
        outcome = simulate_forward(
            series,
            i,
            row["entry"],
            row["stop"],
            row["target"],
            ctx.policy.max_hold_days,
        )
        if outcome is None:
            continue
        ctx.repo.resolve_shadow(
            row["id"], outcome.outcome, outcome.r_multiple, outcome.days
        )
        resolved += 1

    if resolved:
        result.note(f"resolved {resolved} shadow-book entries")
