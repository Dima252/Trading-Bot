"""The brain.

A pure function: no broker calls, no database writes, no clock. Given the same
inputs it returns the same actions, forever. That single property is what makes
the system debuggable, backtestable, and replayable.

`decide()` proposes; the constraint layer disposes. The capacity arithmetic here
answers a different question than the one in `constraints.py` -- this asks "do I
need to free capital to take this?", that asks "is this permitted?". They are
deliberately separate: strategy may be wrong without being unsafe.

Not emitted yet: ADD. Layering shares onto a position with live OCO exit legs is
a three-step broker transaction (cancel exits, add, re-place exits) and belongs
with the executor in phase 2.
"""

from __future__ import annotations

from .models import (
    Action,
    ActionKind,
    Candidate,
    MarketContext,
    Portfolio,
    Position,
)
from .policy import Policy
from .scoring import rank_candidates, score_holding
from .sizing import size_position


def decide(
    portfolio: Portfolio,
    candidates: list[Candidate],
    context: MarketContext,
    policy: Policy,
) -> list[Action]:
    """Return the ordered action list that moves the portfolio to where it
    should be. Order matters: defensive exits come first because they free the
    capital that later opens spend."""

    ranked = rank_candidates(candidates, context, policy)
    fresh_by_ticker = {c.ticker: c for c in ranked}

    actions: list[Action] = []

    # --- 1. DEFENSIVE ------------------------------------------------- #
    closing: set[str] = set()
    for pos in portfolio.positions:
        reason = exit_reason(pos, context, policy)
        if reason is not None:
            actions.append(
                Action(
                    kind=ActionKind.CLOSE,
                    ticker=pos.ticker,
                    qty=pos.qty,
                    reason=reason,
                    policy_version=policy.version,
                )
            )
            closing.add(pos.ticker)

    # --- 6. MAINTAIN (emitted early so freed heat is visible downstream) - #
    for pos in portfolio.positions:
        if pos.ticker in closing:
            continue
        new_stop = trailed_stop(pos, policy)
        if new_stop is not None:
            actions.append(
                Action(
                    kind=ActionKind.ADJUST_STOP,
                    ticker=pos.ticker,
                    qty=pos.qty,
                    stop=new_stop,
                    reason=(
                        f"trail: {pos.unrealized_r:.2f}R, "
                        f"stop {pos.stop:.2f} -> {new_stop:.2f}"
                    ),
                    policy_version=policy.version,
                )
            )

    # --- 2a. REGIME GATE ------------------------------------------------ #
    # Placed after the defensive pass and stop maintenance, before anything that
    # spends capital: sitting out a regime must never mean sitting on a broken
    # position. An agent that cannot decline to trade is compulsive, not
    # autonomous (README section 5).
    # Asks whether ANY setup may trade here, not whether all may: a policy that
    # permits only mean reversion in chop still has work to do in chop. Which
    # setups specifically is settled per candidate, below.
    if not policy.any_setup_may_open_in(context.regime.value):
        return actions

    # --- 3. RANK: holdings compete on the same scale as candidates ----- #
    survivors = [p for p in portfolio.positions if p.ticker not in closing]
    hold_scores: dict[str, float] = {
        p.ticker: score_holding(p, fresh_by_ticker.get(p.ticker), context, policy)
        for p in survivors
    }

    # --- 2. CAPACITY --------------------------------------------------- #
    cash = portfolio.cash + sum(
        p.market_value for p in portfolio.positions if p.ticker in closing
    )
    heat = portfolio.open_heat_dollars - sum(
        p.risk_dollars for p in portfolio.positions if p.ticker in closing
    )
    heat_cap = portfolio.equity * policy.max_portfolio_heat
    slots = policy.max_new_positions_per_day

    tradeable = [
        c
        for c in ranked
        if c.ticker not in portfolio.tickers
        and c.score >= policy.min_candidate_score
        and c.event_flags.penalty < 1.0
        and c.is_valid
        and policy.may_open_in(context.regime.value, c.setup_type.value)
    ]

    # --- 4 + 5. ROTATE AND ALLOCATE ------------------------------------ #
    min_risk = portfolio.equity * policy.max_risk_per_trade * policy.min_risk_fraction

    def _fits(sizing, risk: float) -> bool:
        """Does this candidate deserve capital right now?

        A CAPACITY shortfall produces a starved allocation -- a few shares that
        pay full spread for a fraction of the intended exposure and burn a daily
        slot. Those are worth refusing, and rotating to fund properly.

        A position capped by the per-name concentration limit is a different
        thing entirely: that is the policy working as designed, and the position
        is full size by the rule that matters. Refusing it would mean never
        taking any setup whose stop is tight enough for the risk budget to buy
        more than 15% of equity -- which is most good setups.
        """
        if sizing.qty <= 0 or (heat + risk) > heat_cap:
            return False
        # Left uncollapsed on purpose: each rejection is a distinct reason,
        # and merging them into one boolean loses which one fired.
        if sizing.binding_constraint == "cash_reserve" and risk < min_risk:  # noqa: SIM103
            return False
        return True

    for cand in tradeable:
        if slots <= 0:
            break

        sizing = size_position(cand.entry, cand.stop, portfolio.equity, cash, policy)
        risk = sizing.qty * cand.risk_per_share

        if not _fits(sizing, risk):
            victim = _rotation_victim(cand, hold_scores, survivors, policy)
            if victim is None:
                continue
            actions.append(
                Action(
                    kind=ActionKind.CLOSE,
                    ticker=victim.ticker,
                    qty=victim.qty,
                    reason=(
                        f"rotation: {cand.ticker} scores {cand.score:.1f} vs "
                        f"{hold_scores[victim.ticker]:.1f}, "
                        f"premium {policy.switching_premium}x"
                    ),
                    score=hold_scores[victim.ticker],
                    policy_version=policy.version,
                )
            )
            closing.add(victim.ticker)
            hold_scores.pop(victim.ticker, None)
            survivors = [p for p in survivors if p.ticker != victim.ticker]
            cash += victim.market_value
            heat -= victim.risk_dollars

            sizing = size_position(
                cand.entry, cand.stop, portfolio.equity, cash, policy
            )
            risk = sizing.qty * cand.risk_per_share
            if not _fits(sizing, risk):
                continue

        actions.append(
            Action(
                kind=ActionKind.OPEN,
                ticker=cand.ticker,
                qty=sizing.qty,
                limit=round(cand.entry, 2),
                stop=round(cand.stop, 2),
                target=round(cand.target, 2),
                entry_type=cand.entry_type,
                reason=(
                    f"{cand.setup_type.value} in {context.regime.value}: "
                    f"score {cand.score:.1f}, {cand.reward_risk:.1f}R:R, "
                    f"sized by {sizing.binding_constraint}"
                ),
                score=cand.score,
                policy_version=policy.version,
                setup_type=cand.setup_type,
                atr=cand.atr,
            )
        )
        cash -= sizing.notional
        heat += risk
        slots -= 1

    return actions


# ---------------------------------------------------------------------- #


def exit_reason(pos: Position, ctx: MarketContext, policy: Policy) -> str | None:
    """Non-negotiable exits, checked in order of severity."""
    if pos.event_flags.structural_invalidation:
        return f"thesis invalidated: {pos.event_flags.rationale or 'semantic veto'}"

    if pos.event_flags.binary_event_in_window:
        return (
            f"binary event in hold window "
            f"({pos.event_flags.event_type or 'unknown'}): a gap makes the stop "
            "meaningless"
        )

    if pos.current_price <= pos.stop:
        return (
            f"stop breached at {pos.current_price:.2f} <= {pos.stop:.2f} "
            "and the broker leg did not fill"
        )

    days = pos.days_held(ctx.as_of)
    setup = pos.setup_type.value

    # The horizon belongs to the setup, not to the book. A snap-back thesis is
    # either right within days or it was wrong; a trend ride needs weeks.
    max_hold = policy.max_hold_for(setup)
    if days >= max_hold:
        return f"max hold reached: {days}d (limit {max_hold}d for {setup})"

    time_stop = policy.time_stop_for(setup)
    if days >= time_stop and pos.unrealized_r < policy.time_stop_min_r:
        return f"time stop: {pos.unrealized_r:.2f}R after {days}d ({setup})"

    return None


def trailed_stop(pos: Position, policy: Policy) -> float | None:
    """Ratchet only -- a stop never moves down, and never below breakeven once
    the trail has triggered."""
    if pos.atr is None or pos.atr <= 0:
        return None
    if pos.unrealized_r < policy.trail_trigger_r:
        return None

    proposed = max(
        pos.current_price - policy.trail_atr_mult * pos.atr,
        pos.entry_price,
    )
    if proposed <= pos.stop + 1e-9 or proposed >= pos.current_price:
        return None
    return round(proposed, 2)


def _rotation_victim(
    cand: Candidate,
    hold_scores: dict[str, float],
    survivors: list[Position],
    policy: Policy,
) -> Position | None:
    """The weakest holding, but only if the candidate clears it by the switching
    premium. Without that hysteresis a portfolio optimizer churns daily on score
    noise and pays its entire edge away in spreads."""
    if not survivors:
        return None

    weakest = min(survivors, key=lambda p: (hold_scores[p.ticker], p.ticker))
    threshold = hold_scores[weakest.ticker] * policy.switching_premium
    if cand.score <= threshold:
        return None
    return weakest
