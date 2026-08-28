"""Broker interface.

Everything above this line is broker-agnostic. `PaperBroker` implements the same
protocol in memory, which is what lets the four jobs be tested end to end with
no network and no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class OrderStatus(str, Enum):
    SUBMITTED = "Submitted"
    ACCEPTED = "Accepted"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELED = "Canceled"
    EXPIRED = "Expired"
    REJECTED = "Rejected"

    @property
    def is_open(self) -> bool:
        return self in (
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        )

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


@dataclass(frozen=True)
class Account:
    cash: float
    equity: float
    buying_power: float
    last_equity: float = 0.0
    blocked: bool = False

    @property
    def day_pnl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return (self.equity - self.last_equity) / self.last_equity


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    qty: int
    avg_entry_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    ticker: str
    qty: int
    side: str  # buy | sell
    status: OrderStatus
    order_type: str = "limit"
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: int = 0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    legs: list[BrokerOrder] = field(default_factory=list)

    @property
    def is_stop_leg(self) -> bool:
        return self.order_type in ("stop", "stop_limit")


class BrokerError(RuntimeError):
    pass


@runtime_checkable
class Broker(Protocol):
    def account(self) -> Account: ...

    def positions(self) -> list[BrokerPosition]: ...

    def orders(self, open_only: bool = True) -> list[BrokerOrder]: ...

    def submit_bracket(
        self,
        ticker: str,
        qty: int,
        limit: float,
        stop: float,
        target: float,
        client_order_id: str,
        extended_day_order: bool = True,
    ) -> BrokerOrder: ...

    def close_position(self, ticker: str) -> BrokerOrder: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def replace_stop(self, ticker: str, new_stop: float) -> BrokerOrder | None: ...

    def is_trading_day(self, day: date) -> bool: ...
