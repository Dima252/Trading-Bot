"""One scale for everything competing for capital.

Candidates and held positions must be scored comparably, otherwise "rotate into
something better" is not a well-defined operation.
"""

from __future__ import annotations

from .models import Candidate, MarketContext, Position
from .policy import Policy


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_candidate(c: Candidate, ctx: MarketContext, policy: Policy) -> float:
    """Composite 0-100. Used for RANKING ONLY -- never to scale position size.

    Sizing off a poorly calibrated score concentrates capital in whichever
    signals are most overfit.
    """
    if not c.is_valid:
        return 0.0

    fit = policy.fit_for(c.setup_type.value, ctx.regime.value)
    rr_norm = min(c.reward_risk, policy.rr_cap) / policy.rr_cap

    positive = (
        policy.w_setup * c.setup_quality
        + policy.w_regime * fit * 100.0
        + policy.w_rr * rr_norm * 100.0
    )
    penalty = policy.w_event * c.event_flags.penalty * 100.0
    return _clamp(positive - penalty)


def score_holding(
    pos: Position,
    fresh: Candidate | None,
    ctx: MarketContext,
    policy: Policy,
) -> float:
    """Re-score a holding as if entering it fresh, today, at today's price.

    If the agent would not buy it today, it has no reason to keep holding it.
    This is what makes the ranking honest instead of biased toward whatever is
    already owned.

    When the signal engine still sees a valid setup on the ticker, that fresh
    score is authoritative. When it doesn't, the entry score decays with time
    held, adjusted by how the trade is actually performing -- a position working
    well keeps its claim on capital even after the original pattern has resolved.
    """
    if fresh is not None:
        base = score_candidate(fresh, ctx, policy)
    else:
        periods = pos.days_held(ctx.as_of) / 10.0
        base = pos.entry_score * (policy.stale_score_decay**periods)

    performance = policy.w_performance * pos.unrealized_r
    return _clamp(base + performance)


def rank_candidates(
    candidates: list[Candidate], ctx: MarketContext, policy: Policy
) -> list[Candidate]:
    """Score and sort, best first. Returns new Candidate objects with `score` set."""
    from dataclasses import replace

    scored = [replace(c, score=score_candidate(c, ctx, policy)) for c in candidates]
    scored.sort(key=lambda c: (-c.score, c.ticker))
    return scored
