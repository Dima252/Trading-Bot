"""In-memory broker.

Implements the same protocol as the Alpaca adapter, including OCO exit legs, so
the four jobs can be exercised end to end with no network and no credentials.
Prices are driven by `set_price`, and `advance(bar)` walks a day forward so a
test can watch a bracket actually take a position out.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import count

from ..data.models import Bar
from .base import Account, BrokerError, BrokerOrder, BrokerPosition, OrderStatus


class PaperBroker:
    def __init__(self, cash: float = 100_000.0) -> None:
        self.cash = cash
        self.start_equity = cash
        self.last_equity = cash
        self._positions: dict[str, BrokerPosition] = {}
        self._orders: dict[str, BrokerOrder] = {}
        self._brackets: dict[str, dict] = {}  # ticker -> {stop, target, ids}
        self._prices: dict[str, float] = {}
        self._ids = count(1)
        self._closed_days: set[date] = set()

    # ------------------------------------------------------------ helpers

    def set_price(self, ticker: str, price: float) -> None:
        self._prices[ticker] = price
        pos = self._positions.get(ticker)
        if pos:
            self._positions[ticker] = BrokerPosition(
                pos.ticker, pos.qty, pos.avg_entry_price, price
            )

    def price_of(self, ticker: str) -> float:
        return self._prices.get(ticker, 0.0)

    def mark_closed(self, day: date) -> None:
        self._closed_days.add(day)

    def _next_id(self) -> str:
        return f"paper-{next(self._ids)}"

    # ------------------------------------------------------------ protocol

    def account(self) -> Account:
        market_value = sum(p.market_value for p in self._positions.values())
        equity = self.cash + market_value
        return Account(
            cash=round(self.cash, 2),
            equity=round(equity, 2),
            buying_power=round(self.cash * 2, 2),  # margin, deliberately not used
            last_equity=self.last_equity,
        )

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    def orders(self, open_only: bool = True) -> list[BrokerOrder]:
        out = list(self._orders.values())
        return [o for o in out if o.status.is_open] if open_only else out

    def submit_bracket(
        self,
        ticker: str,
        qty: int,
        limit: float,
        stop: float,
        target: float,
        client_order_id: str,
        extended_day_order: bool = True,
    ) -> BrokerOrder:
        if client_order_id in self._orders:
            raise BrokerError(f"duplicate client_order_id {client_order_id}")
        if ticker in self._positions:
            raise BrokerError(f"already holding {ticker}")
        if qty <= 0:
            raise BrokerError("qty must be positive")

        order = BrokerOrder(
            broker_order_id=self._next_id(),
            client_order_id=client_order_id,
            ticker=ticker,
            qty=qty,
            side="buy",
            status=OrderStatus.SUBMITTED,
            order_type="limit",
            limit_price=limit,
            submitted_at=datetime.now(UTC),
        )
        self._orders[client_order_id] = order
        self._brackets[ticker] = {
            "stop": stop,
            "target": target,
            "client_order_id": client_order_id,
        }
        return order

    def close_position(self, ticker: str) -> BrokerOrder:
        pos = self._positions.get(ticker)
        if pos is None:
            raise BrokerError(f"no position in {ticker}")
        price = self.price_of(ticker) or pos.current_price
        return self._liquidate(ticker, price, "manual_close")

    def cancel_order(self, broker_order_id: str) -> None:
        for cid, order in list(self._orders.items()):
            if order.broker_order_id == broker_order_id and order.status.is_open:
                self._orders[cid] = _with_status(order, OrderStatus.CANCELED)
                self._brackets.pop(order.ticker, None)

    def replace_stop(self, ticker: str, new_stop: float) -> BrokerOrder | None:
        bracket = self._brackets.get(ticker)
        if bracket is None:
            return None
        bracket["stop"] = new_stop
        return self._orders.get(bracket["client_order_id"])

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._closed_days

    # ------------------------------------------------------- simulation

    def advance(self, ticker: str, bar: Bar) -> list[str]:
        """Walk one bar forward: fill resting entries, then check OCO legs.

        Returns a list of human-readable events, which tests assert on.
        """
        events: list[str] = []
        self.set_price(ticker, bar.close)

        # 1. resting entry limits
        for cid, order in list(self._orders.items()):
            if order.ticker != ticker or not order.status.is_open:
                continue
            if order.side != "buy" or order.limit_price is None:
                continue
            fill = None
            if bar.open <= order.limit_price:
                fill = bar.open
            elif bar.low <= order.limit_price:
                fill = order.limit_price
            if fill is None:
                continue
            bracket = self._brackets.get(ticker)
            if bracket and fill <= bracket["stop"]:
                # gapped in below our own stop: the bracket would be flushed on
                # arrival, so the entry is abandoned rather than taken
                self._orders[cid] = _with_status(order, OrderStatus.CANCELED)
                self._brackets.pop(ticker, None)
                events.append(f"{ticker} entry abandoned: gapped below the stop")
                continue
            cost = fill * order.qty
            if cost > self.cash:
                self._orders[cid] = _with_status(order, OrderStatus.REJECTED)
                events.append(f"{ticker} entry rejected: insufficient cash")
                continue
            self.cash -= cost
            self._positions[ticker] = BrokerPosition(ticker, order.qty, fill, bar.close)
            self._orders[cid] = _with_status(
                order, OrderStatus.FILLED, filled_qty=order.qty, filled_avg_price=fill
            )
            events.append(f"{ticker} entry filled {order.qty} @ {fill:.2f}")

        # 2. OCO legs on any open position
        pos = self._positions.get(ticker)
        bracket = self._brackets.get(ticker)
        if pos and bracket:
            stop, target = bracket["stop"], bracket["target"]
            if bar.open <= stop:
                self._liquidate(ticker, bar.open, "gap_through_stop")
                events.append(f"{ticker} gapped through stop @ {bar.open:.2f}")
            elif bar.low <= stop:
                self._liquidate(ticker, stop, "stop")
                events.append(f"{ticker} stopped out @ {stop:.2f}")
            elif bar.high >= target:
                self._liquidate(ticker, target, "target")
                events.append(f"{ticker} target hit @ {target:.2f}")

        return events

    def roll_day(self) -> None:
        """Expire unfilled DAY orders and reset the daily P&L baseline."""
        for cid, order in list(self._orders.items()):
            if order.status.is_open and order.side == "buy":
                self._orders[cid] = _with_status(order, OrderStatus.EXPIRED)
                self._brackets.pop(order.ticker, None)
        self.last_equity = self.account().equity

    # ------------------------------------------------------------ internal

    def _liquidate(self, ticker: str, price: float, reason: str) -> BrokerOrder:
        pos = self._positions.pop(ticker)
        self.cash += price * pos.qty
        self._brackets.pop(ticker, None)
        order = BrokerOrder(
            broker_order_id=self._next_id(),
            client_order_id=f"exit-{ticker}-{next(self._ids)}",
            ticker=ticker,
            qty=pos.qty,
            side="sell",
            status=OrderStatus.FILLED,
            order_type=reason,
            filled_qty=pos.qty,
            filled_avg_price=price,
            submitted_at=datetime.now(UTC),
        )
        self._orders[order.client_order_id] = order
        return order


def _with_status(
    order: BrokerOrder,
    status: OrderStatus,
    filled_qty: int = 0,
    filled_avg_price: float | None = None,
) -> BrokerOrder:
    from dataclasses import replace

    return replace(
        order,
        status=status,
        filled_qty=filled_qty or order.filled_qty,
        filled_avg_price=filled_avg_price or order.filled_avg_price,
    )
