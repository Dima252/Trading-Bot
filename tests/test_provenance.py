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
