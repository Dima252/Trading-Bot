"""The research discipline, enforced by code rather than by memory.

A backtest is evidence only about data the process has never seen, and that
property is lost silently -- by a default date range, months before anyone
notices the numbers stopped meaning anything.

It has already cost this project one holdout. `all_three` cleared 4 of 4 folds
at +0.169R and then went -0.132R out of sample, because the folds it passed had
all been visible to the process that chose it. These tests exist so the next
version of that mistake fails loudly at the command line instead.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading_bot.provenance import (
    CONTAMINATED_FROM,
    DEVELOPMENT,
    HOLDOUT_FROM,
    VALIDATION,
    contamination,
    describe_window,
)

CLEAN_START, CLEAN_END = date(1995, 1, 1), date(2005, 1, 1)


# --- the windows are ordered and non-overlapping --------------------------- #


def test_the_windows_do_not_overlap() -> None:
    assert DEVELOPMENT[0] < DEVELOPMENT[1] < VALIDATION[0] < VALIDATION[1]
    assert VALIDATION[1] < CONTAMINATED_FROM
    assert CONTAMINATED_FROM < HOLDOUT_FROM


def test_development_and_validation_predate_anything_the_process_saw() -> None:
    """If this ever fails, the development set has been contaminated and every
    result derived from it is worthless."""
    assert DEVELOPMENT[1] < CONTAMINATED_FROM
    assert VALIDATION[1] < CONTAMINATED_FROM


# --- contamination ---------------------------------------------------------- #


def test_a_window_entirely_before_the_cache_is_clean() -> None:
    assert contamination(CLEAN_START, CLEAN_END) is None


def test_a_window_inside_the_old_cache_is_contaminated() -> None:
    note = contamination(date(2020, 1, 1), date(2024, 1, 1))
    assert note is not None
    assert "already informed a decision" in note


def test_a_window_that_merely_starts_clean_is_still_contaminated() -> None:
    """Results are aggregated across the window, so contaminated days
    contribute to the number a decision would be read off. Starting clean is
    not the same as being clean."""
    note = contamination(CLEAN_START, date(2024, 1, 1))
    assert note is not None
    assert "Only" in note, "should say which part is usable"


def test_the_boundary_day_is_contaminated() -> None:
    """The first cached bar counts as seen. Off by one here means a whole
    fold of contaminated data slipping into a development run."""
    day_before = CONTAMINATED_FROM - timedelta(days=1)
    assert contamination(CLEAN_START, CONTAMINATED_FROM) is not None
    assert contamination(CLEAN_START, day_before) is None


def test_the_spent_holdout_is_named_as_producing_no_evidence() -> None:
    text = describe_window(HOLDOUT_FROM, date(2026, 8, 27))
    assert "HOLDOUT" in text and "no evidence" in text


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (DEVELOPMENT[0], DEVELOPMENT[1], "development"),
        (VALIDATION[0], VALIDATION[1], "validation"),
        (date(2020, 1, 1), date(2024, 1, 1), "contaminated"),
        (CLEAN_START, date(2024, 1, 1), "straddles"),
    ],
)
def test_each_window_is_described_by_what_it_can_prove(
    start: date, end: date, expected: str
) -> None:
    assert expected in describe_window(start, end)


# --- the shipped config is testable as itself ------------------------------- #


def test_walkforward_can_test_the_config_that_actually_ships() -> None:
    """Reconstructing the deployed policy from a variant definition invites
    drift between the two. The go/no-go has to run the real file."""
    from trading_bot.cli import _load_policy, _variants

    shipped = _load_policy("config/policy.yaml")
    variants = _variants(shipped)

    assert "shipped" in variants
    assert variants["shipped"].version == shipped.version == "v2-holdout"


def test_adding_the_shipped_config_leaves_the_baseline_pinned() -> None:
    """`baseline` is the reference every recorded result was measured against.
    If it moved with the config file, the research record would silently stop
    being reproducible."""
    from trading_bot.cli import _load_policy, _variants
    from trading_bot.core.policy import Policy

    with_shipped = _variants(_load_policy("config/policy.yaml"))
    assert with_shipped["baseline"].version == Policy().version
    assert with_shipped["baseline"].max_risk_per_trade == Policy().max_risk_per_trade


# --- sleeve B variants isolate their hypotheses ----------------------------- #


def _sleeves():
    from trading_bot.cli import _load_policy, _variants

    return _variants(_load_policy("config/policy.yaml"))


def test_each_hypothesis_is_testable_on_its_own() -> None:
    """Bundling changes and reading one number is what made the `defensive`
    variant uninformative -- five changes at once, and no way to tell which one
    moved it. H1 and H2 are offered separately as well as combined."""
    v = _sleeves()
    assert {"sleeve_b_horizon", "sleeve_b_regime", "sleeve_b"} <= set(v)

    h1, h2 = v["sleeve_b_horizon"], v["sleeve_b_regime"]
    assert h1.setup_max_hold_days and not h1.setup_regimes, "H1 must vary alone"
    assert h2.setup_regimes and not h2.setup_max_hold_days, "H2 must vary alone"


def test_the_combined_sleeve_is_exactly_both_hypotheses() -> None:
    v = _sleeves()
    combined, h1, h2 = v["sleeve_b"], v["sleeve_b_horizon"], v["sleeve_b_regime"]

    assert combined.setup_max_hold_days == h1.setup_max_hold_days
    assert combined.setup_time_stop_days == h1.setup_time_stop_days
    assert combined.setup_regimes == h2.setup_regimes


def test_the_sleeve_changes_nothing_for_the_trend_setups() -> None:
    """Sleeve B must add a return stream, not perturb sleeve A. If it altered
    the breakout or pullback horizons the comparison would measure two things."""
    v = _sleeves()
    shipped, combined = v["shipped"], v["sleeve_b"]

    for setup in ("breakout", "pullback"):
        assert combined.max_hold_for(setup) == shipped.max_hold_for(setup)
        assert combined.time_stop_for(setup) == shipped.time_stop_for(setup)
        assert combined.may_open_in("chop", setup) == shipped.may_open_in("chop", setup)


def test_the_sleeve_builds_on_the_deployed_config_not_the_baseline() -> None:
    """The question is what the DEPLOYED system gains, so the sleeve inherits
    the shipped risk envelope rather than the library defaults."""
    from trading_bot.core.policy import Policy

    v = _sleeves()
    assert v["sleeve_b"].max_risk_per_trade == v["shipped"].max_risk_per_trade
    assert v["sleeve_b"].max_risk_per_trade != Policy().max_risk_per_trade
