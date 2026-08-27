"""09:00 ET -- the defensive gate.

Reads last night's pending candidates, asks the semantic engine whether anything
overnight invalidates them, and cancels what it must. Executes no trades: the
market is shut, and the exits this implies are re-derived against live prices by
the 10:00 job.

The engine has negative authority only. It can cancel a setup; it can never
create one, and it never touches sizing.
"""

from __future__ import annotations

from ..core.models import EventFlags
from ..semantic.client import SemanticEngine, NullSemanticEngine
from .base import AgentContext, JobResult, run_job

NAME = "premarket"


def run(ctx: AgentContext, engine: SemanticEngine | None = None) -> JobResult:
    engine = engine or NullSemanticEngine()
    return run_job(NAME, ctx, lambda c, r, rec, rid: _body(c, r, rec, rid, engine))


def _body(
    ctx: AgentContext,
    result: JobResult,
    recon,
    run_id: int,
    engine: SemanticEngine,
) -> None:
    portfolio = recon.portfolio

    # The watchlist is written by the PREVIOUS session's evening job, so it is
    # filed under that day -- not today. Reading and updating under today's date
    # silently touches nothing.
    candidate_day = ctx.repo.latest_candidate_day(ctx.day)
    candidates = ctx.repo.pending_candidates(candidate_day) if candidate_day else []
    held = [p.ticker for p in portfolio.positions]

    if not candidates and not held:
        result.note("nothing pending and nothing held")
        return
    result.note(f"{len(candidates)} candidates from {candidate_day}")

    # --- 1. calendar first: earnings avoidance is a hard rule and costs
    #        nothing to check before spending a token on anything.
    blocked = engine.earnings_within(
        [c.ticker for c in candidates] + held, ctx.day, ctx.policy.time_stop_days
    )
    for ticker, when in blocked.items():
        flags = EventFlags(
            binary_event_in_window=True,
            event_type="earnings",
            event_date=when,
            confidence="high",
            rationale=f"earnings {when} inside the hold window",
        )
        ctx.repo.update_candidate_flags(candidate_day, ticker, flags)
        ctx.repo.set_candidate_status(
            candidate_day, ticker, "Cancelled", f"earnings {when}"
        )
        result.note(f"cancelled {ticker}: earnings {when} inside the hold window")

    # --- 2. the ambiguous cases the calendar cannot answer ---
    survivors = [c for c in candidates if c.ticker not in blocked]
    if not survivors:
        result.note("no candidates left for the semantic engine")
        return

    verdicts = engine.assess([c.ticker for c in survivors], ctx.day)
    cancelled = 0
    for ticker, flags in verdicts.items():
        ctx.repo.update_candidate_flags(candidate_day, ticker, flags)
        if flags.penalty >= 1.0:
            ctx.repo.set_candidate_status(
                candidate_day, ticker, "Cancelled", flags.rationale[:200]
            )
            result.note(f"cancelled {ticker}: {flags.rationale}")
            cancelled += 1

    result.note(
        f"semantic engine reviewed {len(survivors)} candidates, cancelled {cancelled}"
    )

    # --- 3. held positions whose thesis broke overnight ---
    if held:
        held_verdicts = engine.assess(held, ctx.day)
        for ticker, flags in held_verdicts.items():
            if flags.structural_invalidation:
                ctx.repo.set_flag(f"exit_{ticker}", flags.rationale[:200])
                result.note(
                    f"flagged {ticker} for exit at the open: {flags.rationale}"
                )
