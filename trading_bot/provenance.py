"""Which stretches of history have already informed a decision.

A backtest is only evidence about data the process has never seen. That property
is easy to state and very easy to lose: it is destroyed silently, by a default
date range, months before anyone notices the results stopped meaning anything.

It has already cost this project once. The `all_three` variant cleared 4 of 4
walk-forward folds at +0.169R and then went -0.132R on the holdout, because the
folds it "passed" had all been visible to the process that selected it. Fold
consistency was necessary and not sufficient, and the only thing that revealed
the difference was a window nothing had touched.

So the windows live here, in code, and the research commands check them. A rule
written only in a document is a rule that gets broken by a convenient default.
"""

from __future__ import annotations

from datetime import date

# The first bar of the original cache. Everything from here forward was visible
# during variant selection -- every walk-forward fold, every diagnostic run.
CONTAMINATED_FROM = date(2019, 7, 16)

# The holdout, spent once on 2026-08-28. Contaminated twice over.
HOLDOUT_FROM = date(2025, 6, 11)

# The deep history, fetched 2026-08-29 and never examined before that. This is
# where development now happens, because it is the only data nobody has seen.
DEVELOPMENT = (date(1993, 1, 1), date(2013, 12, 31))
VALIDATION = (date(2014, 1, 1), date(2019, 7, 15))


def describe_window(start: date, end: date) -> str:
    """One line naming what kind of evidence a run over this window can produce."""
    if end < CONTAMINATED_FROM:
        if end <= DEVELOPMENT[1]:
            return "development window -- unseen, free to explore"
        return "validation window -- unseen, spend it sparingly and log every look"
    if start >= HOLDOUT_FROM:
        return "THE SPENT HOLDOUT -- produces no evidence at all"
    if start >= CONTAMINATED_FROM:
        return "contaminated -- every variant was selected on this data"
    return "straddles clean and contaminated data -- the result is not out-of-sample"


def contamination(start: date, end: date) -> str | None:
    """A warning if this window cannot support a selection decision, else None.

    Returns None only when the whole window predates anything the process has
    seen. A range that merely *starts* clean is still compromised: results are
    aggregated across it, so contaminated days contribute to the number a
    decision would be read off.
    """
    if end < CONTAMINATED_FROM:
        return None

    clean_part = "" if start >= CONTAMINATED_FROM else (
        f" Only {start} -> {CONTAMINATED_FROM} is clean."
    )
    return (
        f"{start} -> {end} overlaps data that already informed a decision "
        f"(contaminated from {CONTAMINATED_FROM}).{clean_part} "
        "Results here can confirm the code runs; they cannot support a choice "
        "between variants."
    )
