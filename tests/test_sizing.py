"""Sizing is where a bug costs real money. Test the caps individually."""

from __future__ import annotations

import pytest

from trading_bot.core import Policy, size_position


def test_risk_cap_binds_on_a_wide_stop(policy: Policy) -> None:
    # $1,000 of risk (1% of 100k) / $10 per share = 100 shares
    s = size_position(entry=100.0, stop=90.0, equity=100_000, cash=100_000, policy=policy)
    assert s.qty == 100
    assert s.binding_constraint == "risk_per_trade"
    assert s.risk_dollars == pytest.approx(1_000.0)


def test_tight_stop_buys_more_shares_for_the_same_dollar_risk(policy: Policy) -> None:
    wide = size_position(100.0, 90.0, 100_000, 100_000, policy)
    tight = size_position(100.0, 98.0, 100_000, 100_000, policy)
    assert tight.qty > wide.qty
    # ...until the position-size cap takes over
    assert tight.binding_constraint == "position_size_cap"
    assert tight.qty == 150  # 15% of 100k / $100


def test_position_cap_binds_before_risk_on_a_tight_stop(policy: Policy) -> None:
    s = size_position(100.0, 99.0, 100_000, 100_000, policy)
    assert s.qty == 150
    assert s.notional == pytest.approx(15_000.0)


def test_cash_reserve_is_never_breached(policy: Policy) -> None:
    # 15k cash on 100k equity: only 5k is deployable above the 10% floor
    s = size_position(100.0, 90.0, 100_000, cash=15_000, policy=policy)
    assert s.qty == 50
    assert s.binding_constraint == "cash_reserve"
    assert s.notional <= 5_000


def test_no_capacity_when_cash_is_at_the_floor(policy: Policy) -> None:
    s = size_position(100.0, 90.0, 100_000, cash=10_000, policy=policy)
    assert s.qty == 0
    assert not s.is_tradeable
    assert s.binding_constraint == "no_capacity"


@pytest.mark.parametrize(
    "entry,stop,equity",
    [
        (100.0, 100.0, 100_000),  # zero risk per share
        (100.0, 105.0, 100_000),  # inverted stop
        (0.0, -1.0, 100_000),  # nonsense price
        (100.0, 90.0, 0),  # no equity
    ],
)
def test_invalid_inputs_size_to_zero(
    entry: float, stop: float, equity: float, policy: Policy
) -> None:
    s = size_position(entry, stop, equity, 100_000, policy)
    assert s.qty == 0
    assert not s.is_tradeable


def test_risk_stays_constant_across_stop_distances(policy: Policy) -> None:
    """The whole point of fixed fractional sizing."""
    for stop in (90.0, 95.0, 98.0):
        s = size_position(100.0, stop, 1_000_000, 1_000_000, policy)
        if s.binding_constraint == "risk_per_trade":
            assert s.risk_dollars == pytest.approx(10_000.0, rel=0.01)
