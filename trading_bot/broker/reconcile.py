"""Reconciliation: the first line of every job.

Pulls broker truth and forces the database to agree with it. Two disagreements
matter:

* The broker holds something the database has no annotation for -- a manual
  trade, or an entry we submitted and then crashed before recording.
* The database annotates a position the broker no longer has -- an OCO leg
  filled while nothing was running. That is a completed trade nobody wrote down,
  and it is the normal case, not an error.

Everything downstream trusts this. If it is wrong, every risk calculation for
the rest of the session is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

from ..core.models import Portfolio, Position, SetupType
from ..db.repo import Repo, position_from_annotation
from .base import Broker, BrokerOrder, OrderStatus


@dataclass
class Reconciliation:
    portfolio: Portfolio
    adopted: list[str] = field(default_factory=list)
    closed_out: list[str] = field(default_factory=list)
    order_updates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.adopted and not self.warnings


def reconcile(
    broker: Broker,
    repo: Repo,
    day: date,
    default_sector: str = "UNKNOWN",
) -> Reconciliation:
    account = broker.account()
    broker_positions = {p.ticker: p for p in broker.positions()}
    annotations = repo.position_annotations()

    result = Reconciliation(portfolio=Portfolio())

    # --- 1. orders: status is driven from here, never from submission ---
    all_orders = broker.orders(open_only=False)
    by_client_id = {o.client_order_id: o for o in all_orders if o.client_order_id}
    for row in repo.open_orders():
        live = by_client_id.get(row["client_order_id"])
        if live is None:
            continue
        if live.status.value != row["status"]:
            repo.update_order_status(
                row["client_order_id"],
                live.status.value,
                live.filled_qty,
                live.filled_avg_price,
            )
            result.order_updates.append(
                f"{row['ticker']} {row['status']} -> {live.status.value}"
            )

    # --- 2. positions the broker has that we do not annotate ---
    for ticker, pos in broker_positions.items():
        if ticker in annotations:
            continue
        result.adopted.append(ticker)
        result.warnings.append(
            f"{ticker}: held at the broker with no annotation -- adopting with a "
            "synthetic stop; check whether this was a manual trade"
        )
        # A position with no recorded stop is unmanaged risk, so give it one
        # immediately rather than leaving it uncovered until someone notices.
        repo.save_position_annotation(
            ticker=ticker,
            sector=default_sector,
            setup_type=SetupType.BREAKOUT,
            entry_score=0.0,
            initial_stop=round(pos.avg_entry_price * 0.92, 2),
            target=round(pos.avg_entry_price * 1.24, 2),
            opened_at=day,
            policy_version="adopted",
            thesis="adopted during reconciliation; origin unknown",
        )
        annotations = repo.position_annotations()

    # --- 3. annotations with no position: the trade closed while we were off ---
    for ticker, row in list(annotations.items()):
        if ticker in broker_positions:
            continue
        exit_order = _latest_sell(all_orders, ticker)
        _record_closed_trade(repo, row, exit_order, day)
        repo.drop_position_annotation(ticker)
        result.closed_out.append(ticker)

    # --- 4. build the portfolio from broker truth + database intent ---
    annotations = repo.position_annotations()
    stops = _live_stops(all_orders)
    positions: list[Position] = []
    for ticker, pos in broker_positions.items():
        row = annotations.get(ticker)
        if row is None:
            continue
        positions.append(
            position_from_annotation(
                row,
                qty=pos.qty,
                avg_entry=pos.avg_entry_price,
                current_price=pos.current_price,
                live_stop=stops.get(ticker),
            )
        )

    portfolio = Portfolio(
        positions=positions,
        cash=account.cash,
        equity=account.equity,
        day_pnl_pct=account.day_pnl_pct,
        week_pnl_pct=repo.week_pnl_pct(day, account.equity),
        halted=repo.is_halted() or account.blocked,
    )
    result.portfolio = portfolio

    repo.record_equity(
        day,
        cash=account.cash,
        equity=account.equity,
        positions=len(positions),
        heat_pct=portfolio.open_heat_pct,
    )
    return result


# ---------------------------------------------------------------------- #


def _latest_sell(orders: list[BrokerOrder], ticker: str) -> BrokerOrder | None:
    fills = [
        o
        for o in orders
        if o.ticker == ticker
        and o.side == "sell"
        and o.status is OrderStatus.FILLED
        and o.filled_avg_price
    ]
    if not fills:
        return None
    return max(fills, key=lambda o: o.submitted_at or _EPOCH)


def _live_stops(orders: list[BrokerOrder]) -> dict[str, float]:
    """The stop the broker is actually holding -- which may differ from the one
    we last wrote down if a replace succeeded and the write didn't."""
    out: dict[str, float] = {}
    for order in orders:
        for leg in [order, *order.legs]:
            if (
                leg.side == "sell"
                and leg.is_stop_leg
                and leg.status.is_open
                and leg.stop_price
            ):
                out[leg.ticker] = leg.stop_price
    return out


def _record_closed_trade(repo: Repo, row, exit_order, day: date) -> None:
    entry_price = float(row["entry_price"] or 0.0)
    initial_stop = float(row["initial_stop"])
    qty = int(row["entry_qty"] or 0)

    if exit_order is not None and exit_order.filled_avg_price:
        exit_price = float(exit_order.filled_avg_price)
        qty = exit_order.filled_qty or exit_order.qty or qty
        reason = exit_order.order_type or "broker_exit"
    else:
        # No fill record to work from -- the exit happened outside anything we
        # can see. Record it anyway with the stop as the estimate: a missing
        # trade corrupts attribution far worse than an approximate one, and the
        # reason field marks it so the learning layer can exclude it.
        exit_price = initial_stop
        reason = "unreconciled_exit"

    if entry_price <= 0:
        # Pre-snapshot annotation (adopted position, or written by an older
        # schema). R is not computable, so record the trade without faking one.
        repo.record_trade(
            ticker=row["ticker"],
            sector=row["sector"],
            setup_type=row["setup_type"],
            regime_at_entry=row["regime_at_entry"],
            entry_day=row["opened_at"],
            entry_price=0.0,
            exit_day=day.isoformat(),
            exit_price=exit_price,
            qty=qty,
            initial_stop=initial_stop,
            target=float(row["target"]),
            exit_reason=f"{reason}:no_entry_snapshot",
            realized_r=0.0,
            pnl=0.0,
            days_held=(day - date.fromisoformat(row["opened_at"])).days,
            entry_score=row["entry_score"],
            policy_version=row["policy_version"],
        )
        return

    risk = max(entry_price - initial_stop, 1e-9)
    repo.record_trade(
        ticker=row["ticker"],
        sector=row["sector"],
        setup_type=row["setup_type"],
        regime_at_entry=row["regime_at_entry"],
        entry_day=row["opened_at"],
        entry_price=entry_price,
        exit_day=day.isoformat(),
        exit_price=exit_price,
        qty=qty,
        initial_stop=initial_stop,
        target=float(row["target"]),
        exit_reason=reason,
        realized_r=round((exit_price - entry_price) / risk, 3),
        pnl=round((exit_price - entry_price) * qty, 2),
        days_held=(day - date.fromisoformat(row["opened_at"])).days,
        entry_score=row["entry_score"],
        policy_version=row["policy_version"],
    )
