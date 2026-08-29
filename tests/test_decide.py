"""The brain. Every test here is also a specification of intended behaviour."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.conftest import make_candidate, make_portfolio, make_position
from trading_bot.core import (
    ActionKind,
    ConstraintLayer,
    EventFlags,
    MarketContext,
    Policy,
    decide,
    exit_reason,
    trailed_stop,
)
from trading_bot.core.models import SetupType


def kinds(actions) -> list[ActionKind]:
    return [a.kind for a in actions]


def first(actions, kind: ActionKind, ticker: str | None = None):
    return next(
        a
        for a in actions
        if a.kind is kind and (ticker is None or a.ticker == ticker)
    )


# --- 1. defensive exits ------------------------------------------------- #


def test_structural_invalidation_closes_the_position(
    context: MarketContext, policy: Policy
) -> None:
    pos = make_position(
        "BAD",
        event_flags=EventFlags(
            structural_invalidation=True, rationale="guidance withdrawn"
        ),
    )
    actions = decide(make_portfolio([pos]), [], context, policy)
    assert kinds(actions) == [ActionKind.CLOSE]
    assert "guidance withdrawn" in actions[0].reason


def test_binary_event_in_the_window_closes_the_position(
    context: MarketContext, policy: Policy
) -> None:
    pos = make_position(
        "ERN",
        event_flags=EventFlags(binary_event_in_window=True, event_type="earnings"),
    )
    actions = decide(make_portfolio([pos]), [], context, policy)
    assert kinds(actions) == [ActionKind.CLOSE]
    assert "earnings" in actions[0].reason


def test_time_stop_recalls_capital_that_is_not_working(
    context: MarketContext, policy: Policy
) -> None:
    stalled = make_position(
        "FLAT", entry_price=100.0, current_price=101.0, stop=95.0, days_held=15
    )
    actions = decide(make_portfolio([stalled]), [], context, policy)
    assert kinds(actions) == [ActionKind.CLOSE]
    assert "time stop" in actions[0].reason


def test_time_stop_spares_a_position_that_is_working(
    context: MarketContext, policy: Policy
) -> None:
    winner = make_position(
        "RUN", entry_price=100.0, current_price=110.0, stop=95.0, days_held=15
    )
    assert exit_reason(winner, context, policy) is None
    assert decide(make_portfolio([winner]), [], context, policy) == []


def test_breached_stop_is_caught_even_though_the_broker_owns_the_exit(
    context: MarketContext, policy: Policy
) -> None:
    breached = make_position("GAP", current_price=94.0, stop=95.0)
    actions = decide(make_portfolio([breached]), [], context, policy)
    assert kinds(actions) == [ActionKind.CLOSE]
    assert "stop breached" in actions[0].reason


def test_max_hold_days(context: MarketContext, policy: Policy) -> None:
    ancient = make_position(
        "OLD", entry_price=100.0, current_price=115.0, stop=95.0, days_held=45
    )
    actions = decide(make_portfolio([ancient]), [], context, policy)
    assert "max hold" in actions[0].reason


# --- 5b. per-setup exit horizons ---------------------------------------- #


def test_a_setup_can_carry_its_own_time_stop(
    context: MarketContext, policy: Policy
) -> None:
    """One global horizon has to be wrong for at least one setup. A mean
    reversion entry targets the 20-day mean because its own docstring says
    "holding for 3R turns a good win rate into a bad one" -- its thesis
    resolves in days. The shipped config held it for up to 60."""
    quick = policy.with_changes(setup_time_stop_days={"mean_reversion": 5})
    stalled = make_position(
        "SNAP", entry_price=100.0, current_price=101.0, stop=95.0, days_held=7,
        setup_type=SetupType.MEAN_REVERSION,
    )

    # the global time stop is 10 days, so without the override this is fine
    assert exit_reason(stalled, context, policy) is None
    assert "time stop" in (exit_reason(stalled, context, quick) or "")


def test_the_override_is_scoped_to_its_own_setup(
    context: MarketContext, policy: Policy
) -> None:
    """Shortening mean reversion must not shorten the trend setups it sits
    alongside -- that would silently reintroduce the 10-day time stop that
    measured worse than holding to 40."""
    quick = policy.with_changes(setup_time_stop_days={"mean_reversion": 5})
    trend_trade = make_position(
        "RIDE", entry_price=100.0, current_price=101.0, stop=95.0, days_held=7,
        setup_type=SetupType.BREAKOUT,
    )

    assert exit_reason(trend_trade, context, quick) is None


def test_an_unset_policy_behaves_exactly_as_before(
    context: MarketContext, policy: Policy
) -> None:
    """The override defaults to empty, so every existing config keeps the
    horizons it was validated with."""
    assert policy.setup_time_stop_days == {}
    assert policy.time_stop_for("mean_reversion") == policy.time_stop_days
    assert policy.max_hold_for("breakout") == policy.max_hold_days


def test_the_reason_names_which_horizon_fired(
    context: MarketContext, policy: Policy
) -> None:
    """With per-setup horizons the decision log has to say whose limit was
    reached, or a 5-day exit next to a 40-day one is unreadable."""
    quick = policy.with_changes(setup_max_hold_days={"mean_reversion": 8})
    old = make_position(
        "SNAP", entry_price=100.0, current_price=115.0, stop=95.0, days_held=9,
        setup_type=SetupType.MEAN_REVERSION,
    )

    reason = exit_reason(old, context, quick) or ""
    assert "max hold" in reason
    assert "mean_reversion" in reason and "8d" in reason


# --- 5c. per-setup regime permissions ------------------------------------ #


def test_one_setup_can_trade_a_regime_the_others_cannot(
    context: MarketContext, policy: Policy
) -> None:
    """`regime_fit` rates mean_reversion 0.90 in chop and 0.40 in trend, but a
    global `tradeable_regimes: [trend]` lets it fire only where it is rated
    worst. Opening chop for everything would admit breakout, rated 0.30 there.
    """
    from trading_bot.core.models import Regime

    sleeve = policy.with_changes(
        tradeable_regimes=["trend"],
        setup_regimes={"mean_reversion": ["trend", "chop"]},
    )
    chop = replace(context, regime=Regime.CHOP)

    assert sleeve.may_open_in("chop", "mean_reversion")
    assert not sleeve.may_open_in("chop", "breakout")
    assert sleeve.any_setup_may_open_in("chop"), "chop is no longer a dead regime"

    snap = make_candidate("SNAP", setup_quality=90.0,
                          setup_type=SetupType.MEAN_REVERSION)
    ride = make_candidate("RIDE", setup_quality=95.0,
                          setup_type=SetupType.BREAKOUT)
    actions = decide(make_portfolio([]), [snap, ride], chop, sleeve)

    opened = [a.ticker for a in actions if a.kind is ActionKind.OPEN]
    assert opened == ["SNAP"], "the higher-scoring breakout must still be barred"


def test_a_regime_no_setup_allows_still_returns_early(
    context: MarketContext, policy: Policy
) -> None:
    """The cheap gate has to keep working, or every untradeable session pays
    for a full ranking pass over 500 names."""
    from trading_bot.core.models import Regime

    sleeve = policy.with_changes(
        tradeable_regimes=["trend"], setup_regimes={"mean_reversion": ["chop"]}
    )
    assert not sleeve.any_setup_may_open_in("high_vol")

    high_vol = replace(context, regime=Regime.HIGH_VOL)
    cand = make_candidate("ANY", setup_quality=99.0,
                          setup_type=SetupType.MEAN_REVERSION)
    assert decide(make_portfolio([]), [cand], high_vol, sleeve) == []


def test_an_unset_policy_gates_exactly_as_before(policy: Policy) -> None:
    assert policy.setup_regimes == {}
    for regime in ("trend", "chop", "high_vol"):
        for setup in ("breakout", "pullback", "mean_reversion"):
            assert policy.may_open_in(regime, setup) == policy.may_open_in(regime)


def test_the_regime_gate_never_blocks_an_exit(
    context: MarketContext, policy: Policy
) -> None:
    """Sitting out a regime must never mean sitting on a broken position."""
    from trading_bot.core.models import Regime

    shut = policy.with_changes(tradeable_regimes=[], setup_regimes={})
    broken = make_position("BAD", current_price=94.0, stop=95.0)

    actions = decide(
        make_portfolio([broken]), [], replace(context, regime=Regime.CHOP), shut
    )
    assert kinds(actions) == [ActionKind.CLOSE]


# --- 6. stop maintenance ------------------------------------------------ #


def test_stop_trails_once_the_trigger_is_cleared(policy: Policy) -> None:
    pos = make_position(
        "RUN",
        entry_price=100.0,
        current_price=110.0,
        stop=95.0,
        initial_stop=95.0,
        atr=2.0,
    )
    assert pos.unrealized_r == pytest.approx(2.0)
    assert trailed_stop(pos, policy) == 106.0  # 110 - 2*ATR


def test_stop_never_moves_down(policy: Policy) -> None:
    pos = make_position(
        "RUN",
        entry_price=100.0,
        current_price=110.0,
        stop=108.0,
        initial_stop=95.0,
        atr=2.0,
    )
    assert trailed_stop(pos, policy) is None


def test_trail_floors_at_breakeven(policy: Policy) -> None:
    """A wide ATR would otherwise propose a stop below the entry price."""
    pos = make_position(
        "RUN",
        entry_price=100.0,
        current_price=110.0,
        stop=95.0,
        initial_stop=95.0,
        atr=10.0,
    )
    assert trailed_stop(pos, policy) == 100.0


def test_no_trail_before_the_trigger(policy: Policy) -> None:
    pos = make_position(
        "MEH",
        entry_price=100.0,
        current_price=103.0,
        stop=95.0,
        initial_stop=95.0,
        atr=2.0,
    )
    assert pos.unrealized_r == pytest.approx(0.6)
    assert trailed_stop(pos, policy) is None


# --- 5. allocation ------------------------------------------------------ #


def test_opens_the_best_candidates_first(
    context: MarketContext, policy: Policy
) -> None:
    cands = [
        make_candidate("MID", setup_quality=60.0, sector="A"),
        make_candidate("BEST", setup_quality=95.0, sector="B"),
        make_candidate("GOOD", setup_quality=80.0, sector="C"),
    ]
    actions = decide(make_portfolio(), cands, context, policy)
    assert [a.ticker for a in actions] == ["BEST", "GOOD", "MID"]
    assert all(a.kind is ActionKind.OPEN for a in actions)


def test_weak_candidates_are_ignored(
    context: MarketContext, policy: Policy
) -> None:
    weak = make_candidate("WEAK", setup_quality=10.0)  # scores 46 -> above floor
    junk = make_candidate("JUNK", setup_quality=0.0, target=101.0)  # scores below 40
    actions = decide(make_portfolio(), [weak, junk], context, policy)
    assert [a.ticker for a in actions] == ["WEAK"]


def test_event_flagged_candidates_are_never_opened(
    context: MarketContext, policy: Policy
) -> None:
    flagged = make_candidate(
        "ERN",
        setup_quality=100.0,
        event_flags=EventFlags(binary_event_in_window=True, confidence="high"),
    )
    assert decide(make_portfolio(), [flagged], context, policy) == []


def test_a_held_ticker_is_not_opened_again(
    context: MarketContext, policy: Policy
) -> None:
    pf = make_portfolio([make_position("HELD", sector="Technology")], cash=90_000)
    actions = decide(pf, [make_candidate("HELD")], context, policy)
    assert not [a for a in actions if a.kind is ActionKind.OPEN]


def test_daily_new_position_cap_is_respected(
    context: MarketContext, policy: Policy
) -> None:
    cands = [make_candidate(f"T{i}", sector=f"S{i}") for i in range(6)]
    actions = decide(make_portfolio(), cands, context, policy)
    assert len(actions) == policy.max_new_positions_per_day


def test_heat_cap_stops_allocation(context: MarketContext, policy: Policy) -> None:
    loose = policy.with_changes(max_new_positions_per_day=10)
    cands = [make_candidate(f"T{i}", sector=f"S{i}") for i in range(10)]
    actions = decide(make_portfolio(), cands, context, loose)
    # each position risks 1% of equity; the 6% heat cap allows six
    assert len(actions) == 6


def test_starved_allocations_are_skipped(
    context: MarketContext, policy: Policy
) -> None:
    """A 4-share position pays full spread for 10% of the intended exposure."""
    pf = make_portfolio(cash=12_000)  # only $2k deployable above the floor
    pricey = make_candidate("UNH", entry=480.0, stop=455.0, target=555.0)

    assert decide(pf, [pricey], context, policy) == []

    # ...and the rule is what suppresses it, not a sizing failure
    permissive = policy.with_changes(min_risk_fraction=0.0)
    actions = decide(pf, [pricey], context, permissive)
    assert actions[0].kind is ActionKind.OPEN and actions[0].qty == 4


def test_a_starved_candidate_triggers_rotation_instead(
    context: MarketContext, policy: Policy
) -> None:
    """Freeing capital for a real position beats taking a token one."""
    weak = make_position(
        "WEAK",
        qty=400,
        entry_price=100.0,
        current_price=100.0,
        stop=95.0,
        entry_score=45.0,
        days_held=4,
        sector="Energy",
    )
    pf = make_portfolio([weak], cash=12_000, equity=100_000)
    pricey = make_candidate("UNH", entry=480.0, stop=455.0, target=555.0)

    actions = decide(pf, [pricey], context, policy)
    assert first(actions, ActionKind.CLOSE, "WEAK")
    opened = first(actions, ActionKind.OPEN, "UNH")
    assert opened.qty == 31  # $15k position cap / $480, not a token 4 shares


# --- 4. rotation -------------------------------------------------------- #


def test_rotates_out_of_a_weak_holding_to_fund_a_better_setup(
    context: MarketContext, policy: Policy
) -> None:
    weak = make_position(
        "WEAK",
        qty=500,
        entry_price=100.0,
        current_price=100.0,
        stop=95.0,
        entry_score=50.0,
        days_held=5,
    )
    pf = make_portfolio([weak], cash=10_000)  # nothing deployable
    strong = make_candidate(
        "STRONG", entry=50.0, stop=45.0, target=65.0, setup_quality=90.0, sector="Energy"
    )

    actions = decide(pf, [strong], context, policy)

    closed = first(actions, ActionKind.CLOSE, "WEAK")
    opened = first(actions, ActionKind.OPEN, "STRONG")
    assert "rotation" in closed.reason
    assert opened.qty == 200  # $1,000 risk / $5 per share
    assert actions.index(closed) < actions.index(opened)


def test_no_rotation_without_the_switching_premium(
    context: MarketContext, policy: Policy
) -> None:
    """Hysteresis: a marginally better candidate must not trigger a swap."""
    weak = make_position(
        "WEAK",
        qty=500,
        entry_price=100.0,
        current_price=100.0,
        stop=95.0,
        entry_score=50.0,
        days_held=5,
    )
    pf = make_portfolio([weak], cash=10_000)
    marginal = make_candidate(
        "MEH", entry=50.0, stop=45.0, target=65.0, setup_quality=30.0, sector="Energy"
    )
    assert decide(pf, [marginal], context, policy) == []


def test_rotation_is_disabled_by_a_very_high_premium(
    context: MarketContext, policy: Policy
) -> None:
    strict = policy.with_changes(switching_premium=99.0)
    weak = make_position(
        "WEAK", qty=500, current_price=100.0, stop=95.0, entry_score=50.0, days_held=5
    )
    pf = make_portfolio([weak], cash=10_000)
    strong = make_candidate("STRONG", entry=50.0, stop=45.0, target=65.0, sector="Energy")
    assert decide(pf, [strong], context, strict) == []


# --- purity and integration --------------------------------------------- #


def test_decide_is_deterministic(context: MarketContext, policy: Policy) -> None:
    pf = make_portfolio(
        [make_position("A", days_held=15, current_price=101.0)], cash=60_000
    )
    cands = [make_candidate(f"T{i}", sector=f"S{i}") for i in range(4)]
    assert decide(pf, cands, context, policy) == decide(pf, cands, context, policy)


def test_decide_does_not_mutate_its_inputs(
    context: MarketContext, policy: Policy
) -> None:
    pf = make_portfolio([make_position("A")], cash=60_000)
    cands = [make_candidate("T1", sector="S1")]
    before = (pf.cash, pf.positions[0].stop, cands[0].score)
    decide(pf, cands, context, policy)
    assert (pf.cash, pf.positions[0].stop, cands[0].score) == before


def test_everything_decide_proposes_survives_the_constitution(
    context: MarketContext, policy: Policy
) -> None:
    """decide() and constraints.py compute capacity separately; on a clean book
    they must agree."""
    cands = [make_candidate(f"T{i}", sector=f"S{i}") for i in range(3)]
    pf = make_portfolio(cash=100_000)

    actions = decide(pf, cands, context, policy)
    verdict = ConstraintLayer(
        policy, sectors={c.ticker: c.sector for c in cands}
    ).validate(actions, pf)

    assert verdict.approved == actions
    assert not verdict.rejected


def test_rotation_survives_the_constitution(
    context: MarketContext, policy: Policy
) -> None:
    weak = make_position(
        "WEAK",
        qty=500,
        entry_price=100.0,
        current_price=100.0,
        stop=95.0,
        entry_score=50.0,
        days_held=5,
    )
    pf = make_portfolio([weak], cash=10_000)
    strong = make_candidate(
        "STRONG", entry=50.0, stop=45.0, target=65.0, setup_quality=90.0, sector="Energy"
    )

    actions = decide(pf, [strong], context, policy)
    verdict = ConstraintLayer(policy, sectors={"STRONG": "Energy"}).validate(actions, pf)

    assert not verdict.rejected
    assert len(verdict.approved) == 2


# --- the regime gate ------------------------------------------------------ #


def test_the_regime_gate_blocks_new_positions(
    context: MarketContext, policy: Policy
) -> None:
    from dataclasses import replace as _replace

    from trading_bot.core.models import Regime

    gated = policy.with_changes(tradeable_regimes=["trend"])
    cands = [make_candidate(f"T{i}", sector=f"S{i}") for i in range(3)]

    in_trend = decide(make_portfolio(), cands, context, gated)
    assert len(in_trend) == 3

    in_chop = decide(
        make_portfolio(), cands, _replace(context, regime=Regime.CHOP), gated
    )
    assert in_chop == []


def test_the_regime_gate_never_traps_a_position(
    context: MarketContext, policy: Policy
) -> None:
    """Sitting out a regime must not mean sitting on a broken position."""
    from dataclasses import replace as _replace

    from trading_bot.core.models import Regime

    gated = policy.with_changes(tradeable_regimes=["trend"])
    broken = make_position("BAD", days_held=15, current_price=101.0)  # time stop
    pf = make_portfolio([broken], cash=90_000)

    actions = decide(pf, [], _replace(context, regime=Regime.CHOP), gated)

    assert kinds(actions) == [ActionKind.CLOSE]
    assert "time stop" in actions[0].reason


def test_the_regime_gate_still_trails_stops(
    context: MarketContext, policy: Policy
) -> None:
    from dataclasses import replace as _replace

    from trading_bot.core.models import Regime

    gated = policy.with_changes(tradeable_regimes=["trend"])
    winner = make_position(
        "RUN", entry_price=100.0, current_price=110.0, stop=95.0,
        initial_stop=95.0, atr=2.0, days_held=5,
    )
    actions = decide(
        make_portfolio([winner], cash=50_000),
        [],
        _replace(context, regime=Regime.CHOP),
        gated,
    )
    assert kinds(actions) == [ActionKind.ADJUST_STOP]


def test_the_default_policy_trades_every_regime(policy: Policy) -> None:
    assert all(policy.may_open_in(r) for r in ("trend", "chop", "high_vol"))
