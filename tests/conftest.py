from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_bot.core import (
    Candidate,
    EntryType,
    EventFlags,
    MarketContext,
    Policy,
    Portfolio,
    Position,
    Regime,
    SetupType,
)

AS_OF = date(2026, 3, 2)


@pytest.fixture
def policy() -> Policy:
    return Policy()


@pytest.fixture
def context() -> MarketContext:
    return MarketContext(as_of=AS_OF, regime=Regime.TREND)


def make_position(
    ticker: str = "HELD",
    *,
    qty: int = 100,
    entry_price: float = 100.0,
    current_price: float = 100.0,
    stop: float = 95.0,
    initial_stop: float | None = None,
    target: float = 115.0,
    sector: str = "Technology",
    setup_type: SetupType = SetupType.BREAKOUT,
    entry_score: float = 60.0,
    days_held: int = 3,
    atr: float | None = None,
    event_flags: EventFlags | None = None,
) -> Position:
    return Position(
        ticker=ticker,
        qty=qty,
        entry_price=entry_price,
        current_price=current_price,
        stop=stop,
        initial_stop=initial_stop if initial_stop is not None else stop,
        target=target,
        sector=sector,
        setup_type=setup_type,
        entry_score=entry_score,
        opened_at=AS_OF - timedelta(days=days_held),
        atr=atr,
        event_flags=event_flags or EventFlags(),
    )


def make_candidate(
    ticker: str = "NEW",
    *,
    setup_type: SetupType = SetupType.BREAKOUT,
    entry_type: EntryType = EntryType.CLOSE_CONFIRM,
    entry: float = 100.0,
    stop: float = 90.0,
    target: float = 130.0,
    sector: str = "Healthcare",
    setup_quality: float = 80.0,
    atr: float | None = 2.0,
    event_flags: EventFlags | None = None,
) -> Candidate:
    return Candidate(
        ticker=ticker,
        setup_type=setup_type,
        entry_type=entry_type,
        entry=entry,
        stop=stop,
        target=target,
        sector=sector,
        setup_quality=setup_quality,
        atr=atr,
        event_flags=event_flags or EventFlags(),
    )


def make_portfolio(
    positions: list[Position] | None = None,
    *,
    cash: float = 100_000.0,
    equity: float = 100_000.0,
    day_pnl_pct: float = 0.0,
    week_pnl_pct: float = 0.0,
    halted: bool = False,
) -> Portfolio:
    return Portfolio(
        positions=positions or [],
        cash=cash,
        equity=equity,
        day_pnl_pct=day_pnl_pct,
        week_pnl_pct=week_pnl_pct,
        halted=halted,
    )
