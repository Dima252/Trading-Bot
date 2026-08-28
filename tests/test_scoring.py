"""One scale for candidates and holdings, or rotation is not well defined."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.conftest import make_candidate, make_position
from trading_bot.core import (
    EventFlags,
    MarketContext,
    Policy,
    Regime,
    SetupType,
    rank_candidates,
    score_candidate,
    score_holding,
)


def test_composite_score(context: MarketContext, policy: Policy) -> None:
    # 0.6*80 (quality) + 0.2*100 (breakout in a trend) + 0.2*100 (3:1 R:R)
    assert score_candidate(make_candidate(), context, policy) == pytest.approx(88.0)


def test_regime_fit_penalises_the_wrong_market(
    context: MarketContext, policy: Policy
) -> None:
    trend = replace(context, regime=Regime.TREND)
    chop = replace(context, regime=Regime.CHOP)
    breakout = make_candidate(setup_type=SetupType.BREAKOUT)

    assert score_candidate(breakout, chop, policy) < score_candidate(
        breakout, trend, policy
    )

    # ...and the reverse for mean reversion
    mr = make_candidate(setup_type=SetupType.MEAN_REVERSION)
    assert score_candidate(mr, chop, policy) > score_candidate(mr, trend, policy)


def test_event_risk_is_a_penalty_not_a_veto(
    context: MarketContext, policy: Policy
) -> None:
    soft = make_candidate(
        event_flags=EventFlags(binary_event_in_window=True, confidence="low")
    )
    hard = make_candidate(
        event_flags=EventFlags(binary_event_in_window=True, confidence="high")
    )
    assert score_candidate(soft, context, policy) == pytest.approx(63.0)
    assert score_candidate(hard, context, policy) == pytest.approx(38.0)


def test_malformed_candidate_scores_zero(
    context: MarketContext, policy: Policy
) -> None:
    assert score_candidate(
        make_candidate(entry=100.0, stop=105.0), context, policy
    ) == 0.0


def test_holding_uses_the_fresh_setup_when_one_exists(
    context: MarketContext, policy: Policy
) -> None:
    pos = make_position("AAA", entry_price=100.0, current_price=100.0)
    fresh = make_candidate("AAA")
    assert score_holding(pos, fresh, context, policy) == pytest.approx(88.0)


def test_holding_score_decays_when_the_setup_is_gone(
    context: MarketContext, policy: Policy
) -> None:
    pos = make_position("AAA", entry_score=60.0, days_held=10)
    assert score_holding(pos, None, context, policy) == pytest.approx(54.0)

    older = make_position("AAA", entry_score=60.0, days_held=20)
    assert score_holding(older, None, context, policy) == pytest.approx(48.6)


def test_a_working_position_keeps_its_claim_on_capital(
    context: MarketContext, policy: Policy
) -> None:
    """Decay alone would rotate out winners whose pattern has already resolved."""
    flat = make_position("AAA", entry_score=60.0, days_held=10, current_price=100.0)
    winner = make_position(
        "AAA",
        entry_score=60.0,
        days_held=10,
        entry_price=100.0,
        current_price=110.0,
        stop=95.0,
        initial_stop=95.0,
    )
    assert score_holding(winner, None, context, policy) > score_holding(
        flat, None, context, policy
    )
    # 2R unrealized at 5 points per R
    assert score_holding(winner, None, context, policy) == pytest.approx(64.0)


def test_ranking_is_deterministic(context: MarketContext, policy: Policy) -> None:
    cands = [
        make_candidate("LOW", setup_quality=40.0),
        make_candidate("HIGH", setup_quality=95.0),
        make_candidate("MID", setup_quality=70.0),
    ]
    ranked = rank_candidates(cands, context, policy)
    assert [c.ticker for c in ranked] == ["HIGH", "MID", "LOW"]
    assert ranked[0].score > ranked[1].score > ranked[2].score
    assert rank_candidates(cands, context, policy) == ranked
