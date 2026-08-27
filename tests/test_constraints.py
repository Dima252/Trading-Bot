"""The constitution must be un-bypassable, and must never block a defensive act."""

from __future__ import annotations

from tests.conftest import make_portfolio, make_position

from trading_bot.core import Action, ActionKind, ConstraintLayer, Policy

# Distinct sectors, so batch tests exercise the limit they mean to rather than
# tripping the sector cap first.
SPREAD = {f"T{i}": f"Sector{i}" for i in range(12)}


def open_action(ticker: str = "NEW", qty: int = 100, **kw) -> Action:
    return Action(
        kind=ActionKind.OPEN,
        ticker=ticker,
        qty=qty,
        limit=kw.get("limit", 100.0),
        stop=kw.get("stop", 90.0),
        target=kw.get("target", 130.0),
        reason="test",
    )


def rules(verdict) -> set[str]:
    return {r.rule for r in verdict.rejected}


# --- defensive actions are never vetoed -------------------------------- #


def test_kill_switch_blocks_entries_but_not_exits(policy: Policy) -> None:
    held = make_position("HELD")
    pf = make_portfolio([held], halted=True)
    close = Action(ActionKind.CLOSE, "HELD", qty=held.qty, reason="thesis broken")

    v = ConstraintLayer(policy).validate([close, open_action()], pf)

    assert close in v.approved
    assert rules(v) == {"kill_switch"}


def test_loss_breakers_block_entries(policy: Policy) -> None:
    pf = make_portfolio(day_pnl_pct=-0.04)
    v = ConstraintLayer(policy).validate([open_action()], pf)
    assert rules(v) == {"daily_loss_breaker"}

    pf = make_portfolio(week_pnl_pct=-0.07)
    v = ConstraintLayer(policy).validate([open_action()], pf)
    assert rules(v) == {"weekly_loss_breaker"}


def test_adjust_stop_and_cancel_always_pass(policy: Policy) -> None:
    held = make_position("HELD")
    pf = make_portfolio([held], halted=True)
    actions = [
        Action(ActionKind.ADJUST_STOP, "HELD", qty=held.qty, stop=98.0),
        Action(ActionKind.CANCEL, "STALE"),
    ]
    v = ConstraintLayer(policy).validate(actions, pf)
    assert len(v.approved) == 2
    assert not v.rejected


# --- individual limits -------------------------------------------------- #


def test_portfolio_heat_cap(policy: Policy) -> None:
    # 550 shares risking $10 each = $5,500 of open heat on 100k equity
    hot = make_position("HOT", qty=550, current_price=100.0, stop=90.0)
    pf = make_portfolio([hot], cash=40_000)
    assert pf.open_heat_pct == 0.055

    # another $1,000 of risk would reach 6.5%, over the 6% cap
    v = ConstraintLayer(policy).validate([open_action()], pf)
    assert rules(v) == {"max_portfolio_heat"}


def test_sector_exposure_cap(policy: Policy) -> None:
    held = make_position("TECH1", qty=200, current_price=100.0, sector="Technology")
    pf = make_portfolio([held], cash=40_000)

    layer = ConstraintLayer(policy, sectors={"NEW": "Technology"})
    v = layer.validate([open_action()], pf)  # +$10k -> 30% of equity in Technology
    assert rules(v) == {"max_sector_exposure"}


def test_sector_cap_does_not_fire_across_different_sectors(policy: Policy) -> None:
    held = make_position("TECH1", qty=200, current_price=100.0, sector="Technology")
    pf = make_portfolio([held], cash=40_000)

    layer = ConstraintLayer(policy, sectors={"NEW": "Healthcare"})
    v = layer.validate([open_action()], pf)
    assert v.approved and not v.rejected


def test_oversized_quantity_is_rejected(policy: Policy) -> None:
    pf = make_portfolio()
    v = ConstraintLayer(policy).validate([open_action(qty=500)], pf)
    assert rules(v) == {"oversized"}


def test_duplicate_position_is_rejected(policy: Policy) -> None:
    pf = make_portfolio([make_position("NEW")], cash=50_000)
    v = ConstraintLayer(policy).validate([open_action("NEW")], pf)
    assert rules(v) == {"duplicate_position"}


def test_add_to_a_position_we_do_not_hold_is_rejected(policy: Policy) -> None:
    pf = make_portfolio()
    add = Action(ActionKind.ADD, "GHOST", qty=10, limit=100.0, stop=90.0)
    v = ConstraintLayer(policy).validate([add], pf)
    assert rules(v) == {"no_position_to_add"}


def test_max_new_positions_per_day(policy: Policy) -> None:
    pf = make_portfolio(cash=100_000)
    actions = [open_action(f"T{i}") for i in range(5)]
    v = ConstraintLayer(policy, sectors=SPREAD).validate(actions, pf)
    assert len([a for a in v.approved if a.kind is ActionKind.OPEN]) == 3
    assert rules(v) == {"max_new_positions"}


def test_invalid_stop_is_rejected(policy: Policy) -> None:
    pf = make_portfolio()
    v = ConstraintLayer(policy).validate([open_action(stop=110.0)], pf)
    assert rules(v) == {"invalid_stop"}


# --- the projection: batches are checked cumulatively ------------------- #


def test_capital_is_consumed_across_a_batch(policy: Policy) -> None:
    """The third open is checked against what the first two already spent."""
    loose = policy.with_changes(max_new_positions_per_day=5)
    pf = make_portfolio(cash=35_000)  # $25k deployable above the 10% floor

    actions = [open_action(f"T{i}") for i in range(3)]  # $10k notional each
    v = ConstraintLayer(loose, sectors=SPREAD).validate(actions, pf)

    assert len(v.approved) == 2
    assert len(v.rejected) == 1
    assert v.rejected[0].rule == "oversized"
    assert "cash_reserve" in v.rejected[0].detail


def test_closes_free_capital_for_opens_in_the_same_batch(policy: Policy) -> None:
    """A rotation is only coherent if the exit funds the entry."""
    held = make_position("OLD", qty=500, current_price=100.0, stop=95.0)
    pf = make_portfolio([held], cash=10_000)  # nothing deployable on its own

    without_close = ConstraintLayer(policy).validate([open_action()], pf)
    assert without_close.rejected[0].rule == "oversized"

    close = Action(ActionKind.CLOSE, "OLD", qty=500, reason="rotation")
    with_close = ConstraintLayer(policy).validate([close, open_action()], pf)
    assert len(with_close.approved) == 2
    assert not with_close.rejected


def test_heat_accumulates_across_a_batch(policy: Policy) -> None:
    loose = policy.with_changes(max_new_positions_per_day=10)
    pf = make_portfolio(cash=100_000)

    # each open risks $1,000; the 6% heat cap allows six
    actions = [open_action(f"T{i}") for i in range(8)]
    v = ConstraintLayer(loose, sectors=SPREAD).validate(actions, pf)

    assert len(v.approved) == 6
    assert rules(v) == {"max_portfolio_heat"}
