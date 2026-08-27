"""Alpaca adapter.

`alpaca` imports are deferred to call time so the rest of the system -- and the
whole test suite -- runs without the SDK or credentials.

Bracket orders carry their own OCO exit legs, which is what lets a stateless
cron system hold positions safely: once submitted, the stop and target are the
broker's responsibility even if every job fails for a week.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..data.alpaca_data import credentials
from .base import Account, BrokerError, BrokerOrder, BrokerPosition, OrderStatus

# Alpaca's order states mapped onto ours. Anything unmapped is treated as open,
# which is the safe direction: we would rather re-check than assume done.
_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
    "stopped": OrderStatus.FILLED,
}


def _status(raw) -> OrderStatus:
    value = getattr(raw, "value", str(raw)).lower()
    return _STATUS_MAP.get(value, OrderStatus.SUBMITTED)


class AlpacaBroker:
    def __init__(
        self, key: str | None = None, secret: str | None = None, paper: bool = True
    ) -> None:
        if key is None or secret is None:
            key, secret = credentials()
        from alpaca.trading.client import TradingClient

        self.client = TradingClient(key, secret, paper=paper)

    # ------------------------------------------------------------ reads

    def account(self) -> Account:
        a = self.client.get_account()
        return Account(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
            last_equity=float(a.last_equity or 0),
            blocked=bool(getattr(a, "trading_blocked", False)),
        )

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                ticker=p.symbol,
                qty=int(float(p.qty)),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price or p.avg_entry_price),
            )
            for p in self.client.get_all_positions()
        ]

    def orders(self, open_only: bool = True) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN if open_only else QueryOrderStatus.ALL,
            nested=True,  # bring the OCO child legs back with the parent
            limit=500,
        )
        return [self._to_order(o) for o in self.client.get_orders(request)]

    def _to_order(self, o) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(o.id),
            client_order_id=o.client_order_id or "",
            ticker=o.symbol,
            qty=int(float(o.qty or 0)),
            side=getattr(o.side, "value", str(o.side)),
            status=_status(o.status),
            order_type=getattr(o.order_type, "value", str(o.order_type)),
            limit_price=float(o.limit_price) if o.limit_price else None,
            stop_price=float(o.stop_price) if o.stop_price else None,
            filled_qty=int(float(o.filled_qty or 0)),
            filled_avg_price=(
                float(o.filled_avg_price) if o.filled_avg_price else None
            ),
            submitted_at=o.submitted_at,
            legs=[self._to_order(leg) for leg in (o.legs or [])],
        )

    # ------------------------------------------------------------ writes

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
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        if not (stop < limit < target):
            raise BrokerError(
                f"{ticker}: bracket must satisfy stop < entry < target "
                f"({stop} / {limit} / {target})"
            )

        request = LimitOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit, 2),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            client_order_id=client_order_id,
        )
        return self._to_order(self.client.submit_order(request))

    def close_position(self, ticker: str) -> BrokerOrder:
        # Alpaca cancels the resting OCO legs itself when the position is closed
        # through this endpoint; closing by submitting a plain sell would leave
        # them orphaned and able to short the account on the next fill.
        return self._to_order(self.client.close_position(ticker))

    def cancel_order(self, broker_order_id: str) -> None:
        self.client.cancel_order_by_id(broker_order_id)

    def replace_stop(self, ticker: str, new_stop: float) -> BrokerOrder | None:
        """Move the stop leg of an existing bracket.

        The stop is a child order, so trailing means replacing that leg -- not
        submitting anything new.
        """
        from alpaca.trading.requests import ReplaceOrderRequest

        leg = self._find_stop_leg(ticker)
        if leg is None:
            return None
        replaced = self.client.replace_order_by_id(
            leg.broker_order_id,
            ReplaceOrderRequest(stop_price=round(new_stop, 2)),
        )
        return self._to_order(replaced)

    def _find_stop_leg(self, ticker: str) -> BrokerOrder | None:
        for order in self.orders(open_only=True):
            for candidate in [order, *order.legs]:
                if (
                    candidate.ticker == ticker
                    and candidate.side == "sell"
                    and candidate.is_stop_leg
                    and candidate.status.is_open
                ):
                    return candidate
        return None

    # ------------------------------------------------------------ calendar

    def is_trading_day(self, day: date) -> bool:
        """First line of every job. Otherwise all four fire on Thanksgiving."""
        from alpaca.trading.requests import GetCalendarRequest

        sessions = self.client.get_calendar(
            GetCalendarRequest(start=day - timedelta(days=1), end=day + timedelta(days=1))
        )
        return any(s.date == day for s in sessions)
