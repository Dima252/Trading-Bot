"""Shared job scaffolding.

Every job follows the same skeleton:

    calendar check -> reconcile -> decide -> constrain -> act -> log

The calendar check is first because otherwise all four jobs fire on
Thanksgiving. Reconcile is second because every risk number computed afterwards
depends on it being right.

Which actions a job may actually execute is the only thing that differs. The
evening job cannot trade at all (the market is shut); the open job handles
defensive exits and resting entries; the close job handles close-confirmed
entries. One brain, four wake-ups with different authority.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import date

from ..broker.base import Broker, BrokerError
from ..broker.orders import bracket_from_action, client_order_id
from ..broker.reconcile import Reconciliation, reconcile
from ..core.constraints import ConstraintLayer
from ..core.models import Action, ActionKind, EntryType, Portfolio, Rejection
from ..core.policy import Policy
from ..data.cache import BarCache
from ..db.repo import Repo
from ..ops.notify import Level, format_summary, heartbeat, notify

log = logging.getLogger("trading_bot")


@dataclass
class AgentContext:
    repo: Repo
    broker: Broker
    cache: BarCache
    policy: Policy
    sectors: dict[str, str]
    day: date
    dry_run: bool = False


@dataclass
class JobResult:
    job: str
    day: date
    status: str = "ok"
    executed: list[Action] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reconciliation: Reconciliation | None = None

    def note(self, message: str) -> None:
        self.notes.append(message)
        log.info("%s: %s", self.job, message)

    @property
    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for action in self.executed:
            kinds[action.kind.value] = kinds.get(action.kind.value, 0) + 1
        parts = [f"{v}x{k}" for k, v in sorted(kinds.items())] or ["no actions"]
        if self.rejected:
            parts.append(f"{len(self.rejected)} vetoed")
        return ", ".join(parts)


def _with_retry(fn, attempts: int = 3, delay: float = 4.0):
    """Retry a read through a transient network blip.

    These jobs run once a day. Losing a whole session to a two-second DNS
    hiccup is a poor trade against a few seconds of waiting -- but if the
    connection is genuinely down, this fails fast enough to still be logged.
    """
    import time

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts - 1:
                log.warning("retrying after %s: %s", type(exc).__name__, exc)
                time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def run_job(name: str, ctx: AgentContext, body) -> JobResult:
    """Wrap a job body with calendar gating, run logging and error capture."""
    result = JobResult(job=name, day=ctx.day)

    # The run is opened BEFORE the calendar check, and the check lives inside
    # the try. Reaching the calendar needs the network, so with the check
    # outside a disconnected machine produced an uncaught traceback: no run
    # record, no alert, no failed heartbeat -- the three things that would tell
    # you it happened.
    run_id = ctx.repo.start_run(name, ctx.day)
    try:
        if not _with_retry(lambda: ctx.broker.is_trading_day(ctx.day)):
            result.status = "skipped"
            result.note(f"{ctx.day} is not a trading session")
            ctx.repo.finish_run(run_id, "skipped", "not a trading session")
            heartbeat(name)
            return result

        recon = reconcile(ctx.broker, ctx.repo, ctx.day)
        result.reconciliation = recon
        for warning in recon.warnings:
            result.note(f"reconcile: {warning}")
        for ticker in recon.closed_out:
            result.note(f"reconcile: {ticker} closed at the broker, trade recorded")

        body(ctx, result, recon, run_id)

        ctx.repo.finish_run(run_id, result.status, result.summary)
        _alert_on_notable(ctx, result, recon)
        heartbeat(name)
    except Exception as exc:  # noqa: BLE001 -- a job must never die silently
        result.status = "error"
        result.note(f"FAILED: {exc}")
        ctx.repo.finish_run(run_id, "error", traceback.format_exc())
        log.exception("%s failed", name)
        notify(
            Level.ERROR,
            f"{name} failed on {ctx.day}",
            f"{type(exc).__name__}: {exc}",
        )
        heartbeat(name, failed=True)
    return result


def _alert_on_notable(ctx: AgentContext, result: JobResult, recon) -> None:
    """Alert on things a human needs to see, not on every routine run.

    A notifier that fires daily gets muted, and a muted notifier is worse than
    none -- so this stays quiet unless the book actually changed or something
    needs attention.
    """
    if ctx.repo.is_halted():
        notify(
            Level.WARN,
            f"{result.job}: kill switch is ENGAGED",
            ctx.repo.get_flag("HALT_REASON") or "",
        )
        return

    if recon is not None and recon.warnings:
        notify(
            Level.WARN,
            f"{result.job}: reconciliation warnings",
            "\n".join(recon.warnings[:5]),
        )

    traded = [a for a in result.executed if a.kind is not ActionKind.ADJUST_STOP]
    if traded:
        notify(Level.INFO, f"{result.job}: book changed", format_summary(result))


def constrain(
    ctx: AgentContext, actions: list[Action], portfolio: Portfolio
) -> tuple[list[Action], list[Rejection]]:
    verdict = ConstraintLayer(ctx.policy, sectors=ctx.sectors).validate(
        actions, portfolio
    )
    return verdict.approved, verdict.rejected


def permitted(actions: list[Action], kinds: set[ActionKind]) -> list[Action]:
    return [a for a in actions if a.kind in kinds]


# ---------------------------------------------------------------------- #


def execute(
    ctx: AgentContext,
    actions: list[Action],
    result: JobResult,
    entry_types: set[EntryType] | None = None,
) -> None:
    """Send approved actions to the broker.

    Order matters here too: exits are sent before entries so the cash they
    release is available to the broker when the entries land.
    """
    exits = [a for a in actions if a.kind is not ActionKind.OPEN]
    entries = [a for a in actions if a.kind is ActionKind.OPEN]
    if entry_types is not None:
        entries = [a for a in entries if a.entry_type in entry_types]

    for action in exits + entries:
        try:
            _execute_one(ctx, action, result)
        except (BrokerError, ValueError) as exc:
            result.note(f"{action.kind.value} {action.ticker} failed: {exc}")


def _execute_one(ctx: AgentContext, action: Action, result: JobResult) -> None:
    if ctx.dry_run:
        result.executed.append(action)
        result.note(f"[dry run] {action.kind.value} {action.ticker} -- {action.reason}")
        return

    if action.kind is ActionKind.CLOSE:
        ctx.broker.close_position(action.ticker)
        ctx.repo.drop_position_annotation(action.ticker)
        result.executed.append(action)
        result.note(f"CLOSE {action.ticker}: {action.reason}")

    elif action.kind is ActionKind.ADJUST_STOP:
        moved = ctx.broker.replace_stop(action.ticker, action.stop)
        if moved is None:
            result.note(f"ADJUST_STOP {action.ticker}: no live stop leg to replace")
            return
        result.executed.append(action)
        result.note(f"ADJUST_STOP {action.ticker} -> {action.stop}")

    elif action.kind is ActionKind.OPEN:
        _submit_entry(ctx, action, result)

    elif action.kind is ActionKind.CANCEL:
        for order in ctx.broker.orders(open_only=True):
            if order.ticker == action.ticker:
                ctx.broker.cancel_order(order.broker_order_id)
        result.executed.append(action)
        result.note(f"CANCEL {action.ticker}")


def _submit_entry(ctx: AgentContext, action: Action, result: JobResult) -> None:
    payload = bracket_from_action(action)
    cid = client_order_id(action.ticker, ctx.day, action.kind)

    # Idempotency: a cron retry re-derives the same id, and the second attempt
    # is refused instead of doubling the position.
    if ctx.repo.order_exists(cid):
        result.note(f"OPEN {action.ticker}: {cid} already submitted, skipping")
        return

    order = ctx.broker.submit_bracket(
        ticker=payload["ticker"],
        qty=payload["qty"],
        limit=payload["limit"],
        stop=payload["stop"],
        target=payload["target"],
        client_order_id=cid,
    )

    ctx.repo.record_order(
        client_order_id=cid,
        day=ctx.day,
        ticker=action.ticker,
        side="buy",
        qty=action.qty,
        status=order.status.value,
        policy_version=action.policy_version,
        broker_order_id=order.broker_order_id,
        limit_price=payload["limit"],
        stop_price=payload["stop"],
        target_price=payload["target"],
    )

    # The annotation is written now so that a fill overnight can still be
    # reconstructed tomorrow; entry price is refined on reconciliation.
    if action.setup_type is not None:
        ctx.repo.save_position_annotation(
            ticker=action.ticker,
            sector=ctx.sectors.get(action.ticker, "UNKNOWN"),
            setup_type=action.setup_type,
            entry_score=action.score,
            initial_stop=payload["stop"],
            target=payload["target"],
            opened_at=ctx.day,
            policy_version=action.policy_version,
            entry_price=payload["limit"],
            entry_qty=action.qty,
            thesis=action.reason,
            atr=action.atr,
        )

    ctx.repo.set_candidate_status(ctx.day, action.ticker, "Submitted", cid)
    result.executed.append(action)
    result.note(
        f"OPEN {action.ticker} {action.qty} @ {payload['limit']} "
        f"stop {payload['stop']} target {payload['target']}"
    )
