"""10:00 ET -- defensive exits and resting entries.

Runs after the opening range has settled rather than into the auction. Handles
everything the evening job could only plan, plus the pullback entries, which are
placed as resting limits so the broker watches the price instead of cron
guessing when to look.

Close-confirmed breakouts are NOT placed here: the whole point of that entry
style is that the level has to hold into the close.
"""

from __future__ import annotations

from ..core.decide import decide
from ..core.models import ActionKind, EntryType, EventFlags, MarketContext, Regime
from .base import AgentContext, JobResult, constrain, execute, permitted, run_job

NAME = "open"
ALLOWED = {ActionKind.CLOSE, ActionKind.ADJUST_STOP, ActionKind.CANCEL, ActionKind.OPEN}


def run(ctx: AgentContext, regime: Regime = Regime.TREND) -> JobResult:
    return run_job(NAME, ctx, lambda c, r, rec, rid: _body(c, r, rec, rid, regime))


def _body(
    ctx: AgentContext,
    result: JobResult,
    recon,
    run_id: int,
    regime: Regime,
) -> None:
    portfolio = recon.portfolio
    context = MarketContext(as_of=ctx.day, regime=regime)

    candidate_day = ctx.repo.latest_candidate_day(ctx.day)
    candidates = (
        ctx.repo.pending_candidates(candidate_day) if candidate_day else []
    )
    result.note(
        f"{len(candidates)} pending candidates from "
        f"{candidate_day or 'no prior session'}"
    )

    # Positions the premarket job flagged for exit: fold the verdict into the
    # position so decide() reaches the CLOSE on its own rather than being
    # bypassed. One brain, one path to an exit.
    portfolio = _apply_overnight_vetoes(ctx, portfolio, result)

    actions = decide(portfolio, candidates, context, ctx.policy)
    approved, rejected = constrain(ctx, actions, portfolio)
    result.rejected = rejected
    ctx.repo.record_decisions(run_id, ctx.day, approved, rejected)

    execute(
        ctx,
        permitted(approved, ALLOWED),
        result,
        entry_types={EntryType.RESTING_LIMIT},
    )

    deferred = [
        a
        for a in approved
        if a.kind is ActionKind.OPEN and a.entry_type is EntryType.CLOSE_CONFIRM
    ]
    if deferred:
        result.note(
            f"{len(deferred)} breakout entries deferred to the close-confirmation job"
        )


def _apply_overnight_vetoes(ctx: AgentContext, portfolio, result: JobResult):
    """Re-attach the semantic engine's overnight verdicts to held positions."""
    from dataclasses import replace

    updated = []
    changed = 0
    for position in portfolio.positions:
        reason = ctx.repo.get_flag(f"exit_{position.ticker}")
        if reason:
            updated.append(
                replace(
                    position,
                    event_flags=EventFlags(
                        structural_invalidation=True,
                        confidence="high",
                        rationale=reason,
                    ),
                )
            )
            ctx.repo.set_flag(f"exit_{position.ticker}", "")
            changed += 1
        else:
            updated.append(position)

    if changed:
        result.note(f"{changed} positions carry an overnight invalidation flag")
    return replace(portfolio, positions=updated)
