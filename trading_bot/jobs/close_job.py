"""15:30 ET -- close confirmation and cleanup.

Breakout entries are taken here, and only if the level is still holding 30
minutes before the bell. That filters intraday fakeouts at the cost of a worse
fill, which is the trade this setup deliberately makes.

Two things need care:

* A buy limit at last night's trigger sits BELOW the market once the level has
  been cleared, so it would never fill. The entry is lifted to the live price --
  but only up to `max_entry_chase_pct`, past which the move has already happened
  and the setup is abandoned rather than bought late.
* Unfilled DAY orders are cancelled before the close, so nothing rests overnight
  that we did not intend to be resting.
"""

from __future__ import annotations

from dataclasses import replace

from ..core.decide import decide
from ..core.models import ActionKind, Candidate, EntryType, MarketContext, Regime
from .base import AgentContext, JobResult, constrain, execute, permitted, run_job

NAME = "close"

# A watchlist older than this is not acted on. If the evening job has been
# failing, its entry prices are days stale and placing orders against them
# would be trading yesterday's analysis at today's prices. Four days covers a
# long weekend.
MAX_WATCHLIST_AGE_DAYS = 4
ALLOWED = {ActionKind.OPEN, ActionKind.CANCEL, ActionKind.CLOSE, ActionKind.ADJUST_STOP}


def run(
    ctx: AgentContext,
    prices: dict[str, float] | None = None,
    regime: Regime = Regime.TREND,
) -> JobResult:
    return run_job(
        NAME, ctx, lambda c, r, rec, rid: _body(c, r, rec, rid, prices or {}, regime)
    )


def _body(
    ctx: AgentContext,
    result: JobResult,
    recon,
    run_id: int,
    prices: dict[str, float],
    regime: Regime,
) -> None:
    portfolio = recon.portfolio
    context = MarketContext(as_of=ctx.day, regime=regime)

    candidate_day = ctx.repo.latest_candidate_day(ctx.day)
    pending = ctx.repo.pending_candidates(candidate_day) if candidate_day else []
    if candidate_day is not None and (ctx.day - candidate_day).days > MAX_WATCHLIST_AGE_DAYS:
        result.note(
            f"STALE WATCHLIST: {candidate_day} is "
            f"{(ctx.day - candidate_day).days} days old -- declining entries."
        )
        pending = []
    breakouts = [c for c in pending if c.entry_type is EntryType.CLOSE_CONFIRM]

    confirmed, skipped = confirm(breakouts, prices, ctx.policy.max_entry_chase_pct)
    for ticker, why in skipped.items():
        ctx.repo.set_candidate_status(ctx.day, ticker, "Cancelled", why)
        result.note(f"skipped {ticker}: {why}")

    result.note(
        f"{len(confirmed)} of {len(breakouts)} breakouts confirmed into the close"
    )

    actions = decide(portfolio, confirmed, context, ctx.policy)
    approved, rejected = constrain(ctx, actions, portfolio)
    result.rejected = rejected
    ctx.repo.record_decisions(run_id, ctx.day, approved, rejected)

    execute(
        ctx,
        permitted(approved, ALLOWED),
        result,
        entry_types={EntryType.CLOSE_CONFIRM},
    )

    # The entries just submitted are exempt from the sweep below. A marketable
    # limit fills in seconds, but "unfilled" is true for the instant between
    # submitting and the fill being reported -- cancelling there would mean the
    # close-confirmation job could never actually open a position.
    from ..broker.orders import client_order_id

    just_submitted = {
        client_order_id(a.ticker, ctx.day, a.kind)
        for a in result.executed
        if a.kind is ActionKind.OPEN
    }
    _cancel_unfilled(ctx, result, exempt=just_submitted)


def confirm(
    candidates: list[Candidate],
    prices: dict[str, float],
    max_chase: float,
) -> tuple[list[Candidate], dict[str, str]]:
    """Which breakouts are still valid at the close, repriced to the market.

    Pure, so the rule is testable without a broker.
    """
    confirmed: list[Candidate] = []
    skipped: dict[str, str] = {}

    for cand in candidates:
        price = prices.get(cand.ticker)
        if price is None:
            skipped[cand.ticker] = "no live price"
            continue
        if price < cand.entry:
            skipped[cand.ticker] = (
                f"back below the trigger ({price:.2f} < {cand.entry:.2f})"
            )
            continue
        if price > cand.entry * (1 + max_chase):
            skipped[cand.ticker] = (
                f"extended {price / cand.entry - 1:.1%} past the trigger, "
                f"cap {max_chase:.0%}"
            )
            continue
        if price <= cand.stop:
            skipped[cand.ticker] = "price is already at or below the stop"
            continue

        # Lift the entry to the market. The stop is structural and does not
        # move, so the risk per share grows slightly and position size shrinks
        # to compensate -- which is the correct response to a worse entry.
        confirmed.append(replace(cand, entry=round(price, 2)))

    return confirmed, skipped


def _cancel_unfilled(
    ctx: AgentContext, result: JobResult, exempt: set[str] | None = None
) -> None:
    """Nothing rests overnight that we did not mean to leave resting.

    An entry that never filled is a trade we did not take, so it goes in the
    shadow book alongside the ones we declined -- otherwise "our limit was too
    low" is a mistake the agent can never learn about.
    """
    exempt = exempt or set()
    cancelled = 0
    for order in ctx.broker.orders(open_only=True):
        if order.side != "buy" or order.client_order_id in exempt:
            continue
        if ctx.dry_run:
            result.note(f"[dry run] would cancel {order.ticker}")
            continue

        ctx.broker.cancel_order(order.broker_order_id)
        ctx.repo.update_order_status(order.client_order_id, "Canceled")
        _shadow_unfilled(ctx, order.client_order_id)
        # The annotation was written optimistically at submission; with no fill
        # it describes a position that does not exist.
        ctx.repo.drop_position_annotation(order.ticker)
        cancelled += 1

    if cancelled:
        result.note(f"cancelled {cancelled} unfilled entry orders")


def _shadow_unfilled(ctx: AgentContext, client_order_id: str) -> None:
    row = ctx.repo.conn.execute(
        "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
    ).fetchone()
    if row is None or not row["limit_price"] or not row["stop_price"]:
        return
    ctx.repo.record_shadow(
        day=ctx.day.isoformat(),
        ticker=row["ticker"],
        sector=ctx.sectors.get(row["ticker"], "UNKNOWN"),
        setup_type="unknown",
        score=0.0,
        entry=row["limit_price"],
        stop=row["stop_price"],
        target=row["target_price"] or row["limit_price"],
        not_taken_reason="unfilled",
    )
