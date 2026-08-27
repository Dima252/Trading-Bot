"""Paper broker, order identity, and bracket construction."""

from __future__ import annotations

from datetime import date

import pytest

from trading_bot.broker.base import Broker, OrderStatus
from trading_bot.broker.orders import (
    bracket_from_action,
    client_order_id,
    validate_bracket,
)
from trading_bot.broker.paper import PaperBroker
from trading_bot.core.models import Action, ActionKind, EntryType
from trading_bot.data.models import Bar

DAY = date(2026, 3, 2)


def bar(o, h, l, c, day=DAY) -> Bar:
    return Bar(day, o, h, l, c, 1_000_000.0)


# --- order identity ------------------------------------------------------ #


def test_client_order_id_is_deterministic() -> None:
    a = client_order_id("AAPL", DAY, ActionKind.OPEN)
    b = client_order_id("AAPL", DAY, ActionKind.OPEN)
    assert a == b == "AAPL-2026-03-02-OPEN"


def test_client_order_id_separates_intents_and_days() -> None:
    assert client_order_id("AAPL", DAY, ActionKind.OPEN) != client_order_id(
        "AAPL", DAY, ActionKind.CLOSE
    )
    assert client_order_id("AAPL", DAY, ActionKind.OPEN) != client_order_id(
        "AAPL", date(2026, 3, 3), ActionKind.OPEN
    )


def test_client_order_id_sanitises_symbols() -> None:
    assert "/" not in client_order_id("BRK/B", DAY, ActionKind.OPEN)


# --- bracket validation --------------------------------------------------- #


@pytest.mark.parametrize(
    "limit,stop,target",
    [(100.0, 105.0, 130.0), (100.0, 90.0, 95.0), (0.0, -1.0, 5.0)],
)
def test_malformed_brackets_are_refused(limit, stop, target) -> None:
    with pytest.raises(ValueError):
        validate_bracket(limit, stop, target)


def test_bracket_payload_rounds_prices() -> None:
    action = Action(
        ActionKind.OPEN,
        "AAPL",
        qty=10,
        limit=100.123456,
        stop=90.987654,
        target=130.5,
        entry_type=EntryType.CLOSE_CONFIRM,
    )
    payload = bracket_from_action(action)
    assert payload == {
        "ticker": "AAPL",
        "qty": 10,
        "limit": 100.12,
        "stop": 90.99,
        "target": 130.5,
    }


def test_bracket_from_a_non_entry_is_refused() -> None:
    with pytest.raises(ValueError):
        bracket_from_action(Action(ActionKind.CLOSE, "AAPL", qty=10))


# --- the paper broker fulfils the protocol -------------------------------- #


def test_paper_broker_satisfies_the_protocol() -> None:
    assert isinstance(PaperBroker(), Broker)


def test_duplicate_client_order_id_is_refused() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 10, 100.0, 90.0, 130.0, "cid-1")
    with pytest.raises(Exception):
        broker.submit_bracket("AAA", 10, 100.0, 90.0, 130.0, "cid-1")


def test_resting_limit_fills_then_the_target_takes_it_out() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, limit=100.0, stop=90.0, target=130.0,
                          client_order_id="cid-1")

    # day 1: dips to the limit and fills
    events = broker.advance("AAA", bar(102.0, 103.0, 99.0, 101.0))
    assert any("entry filled" in e for e in events)
    assert broker.positions()[0].qty == 100
    assert broker.account().cash == pytest.approx(90_000.0)

    # day 2: runs to the target
    events = broker.advance("AAA", bar(120.0, 131.0, 119.0, 130.0, date(2026, 3, 3)))
    assert any("target hit" in e for e in events)
    assert broker.positions() == []
    assert broker.account().cash == pytest.approx(103_000.0)


def test_a_gap_through_the_stop_fills_at_the_open() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, 100.0, 90.0, 130.0, "cid-1")
    broker.advance("AAA", bar(100.0, 101.0, 99.0, 100.0))
    assert broker.positions()

    events = broker.advance("AAA", bar(80.0, 82.0, 78.0, 81.0, date(2026, 3, 3)))
    assert any("gapped through stop" in e for e in events)
    assert broker.account().cash == pytest.approx(98_000.0)  # filled at 80, not 90


def test_replace_stop_moves_the_oco_leg() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, 100.0, 90.0, 130.0, "cid-1")
    broker.advance("AAA", bar(100.0, 101.0, 99.0, 100.0))

    assert broker.replace_stop("AAA", 95.0) is not None
    # the raised stop now takes the position out on a dip that 90 would not have
    events = broker.advance("AAA", bar(99.0, 100.0, 94.0, 96.0, date(2026, 3, 3)))
    assert any("stopped out @ 95" in e for e in events)


def test_replace_stop_on_an_unheld_name_returns_none() -> None:
    assert PaperBroker().replace_stop("NOPE", 10.0) is None


def test_unfilled_day_orders_expire_on_the_roll() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, 50.0, 45.0, 65.0, "cid-1")
    broker.advance("AAA", bar(100.0, 101.0, 99.0, 100.0))  # never trades to 50
    assert broker.orders(open_only=True)

    broker.roll_day()
    assert broker.orders(open_only=True) == []
    assert broker.orders(open_only=False)[0].status is OrderStatus.EXPIRED


def test_closing_a_position_returns_the_cash() -> None:
    broker = PaperBroker(100_000)
    broker.submit_bracket("AAA", 100, 100.0, 90.0, 130.0, "cid-1")
    broker.advance("AAA", bar(100.0, 101.0, 99.0, 110.0))
    broker.close_position("AAA")
    assert broker.positions() == []
    assert broker.account().cash == pytest.approx(101_000.0)


def test_entry_is_rejected_when_cash_is_short() -> None:
    broker = PaperBroker(1_000)
    broker.submit_bracket("AAA", 100, 100.0, 90.0, 130.0, "cid-1")
    events = broker.advance("AAA", bar(100.0, 101.0, 99.0, 100.0))
    assert any("insufficient cash" in e for e in events)
    assert broker.positions() == []
