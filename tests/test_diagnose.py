"""The ranking diagnostic.

A diagnostic that lies is worse than none -- it would send the next month of
work in the wrong direction. So the statistics get tested against cases with
known answers.
"""

from __future__ import annotations

from datetime import date

import pytest

from trading_bot.backtest.records import ShadowRecord
from trading_bot.core.models import Regime, SetupType
from trading_bot.learning.diagnose import (
    bucketize,
    diagnose,
    diagnose_feature,
    format_diagnosis,
    spearman,
)


def record(score: float, r: float | None, **features) -> ShadowRecord:
    return ShadowRecord(
        ticker="AAA",
        sector="Technology",
        setup_type=SetupType.PULLBACK,
        regime=Regime.TREND,
        day=date(2026, 3, 2),
        score=score,
        entry=100.0,
        stop=95.0,
        target=115.0,
        not_taken_reason="ranked_out",
        setup_quality=score,
        reward_risk=3.0,
        features=features,
        outcome="target" if r and r > 0 else "stop",
        hypothetical_r=r,
    )


# --- rank correlation ----------------------------------------------------- #


def test_spearman_is_one_for_a_perfect_ordering() -> None:
    assert spearman([(1, 10), (2, 20), (3, 30), (4, 40)]) == pytest.approx(1.0)


def test_spearman_is_minus_one_when_inverted() -> None:
    assert spearman([(1, 40), (2, 30), (3, 20), (4, 10)]) == pytest.approx(-1.0)


def test_spearman_ignores_outlier_magnitude() -> None:
    """Rank, not Pearson: R multiples have fat tails and one +80R winner must
    not decide the answer."""
    monotonic = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]
    with_outlier = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 800)]
    assert spearman(monotonic) == pytest.approx(spearman(with_outlier))


def test_spearman_handles_ties() -> None:
    assert spearman([(1, 5), (1, 5), (1, 5), (1, 5)]) == 0.0


def test_spearman_of_a_short_sample_is_zero() -> None:
    assert spearman([(1, 2)]) == 0.0


# --- bucketing ------------------------------------------------------------ #


def test_bucketize_splits_into_equal_counts() -> None:
    pairs = [(float(i), float(i)) for i in range(400)]
    buckets = bucketize(pairs, n_buckets=10)
    assert len(buckets) == 10
    assert all(b.n == 40 for b in buckets)
    assert buckets[0].lo == 0.0
    assert buckets[-1].hi == 399.0


def test_bucketize_orders_by_the_input_not_the_outcome() -> None:
    pairs = [(float(i), float(-i)) for i in range(400)]
    buckets = bucketize(pairs, n_buckets=10)
    assert buckets[0].mean_r > buckets[-1].mean_r  # inverted, as constructed


def test_bucketize_refuses_a_sample_too_small_to_split() -> None:
    assert bucketize([(1.0, 1.0)] * 5, n_buckets=10) == []
    # 100 points over 10 buckets is 10 each -- below MIN_BUCKET, so nothing
    # is reported rather than reporting ten noisy cells
    assert bucketize([(float(i), float(i)) for i in range(100)]) == []


def test_significance_scales_with_sample_size() -> None:
    """A fixed rho cutoff calls noise a finding on small samples and misses
    real effects on large ones."""
    small = [record(float(i), float(i) / 100) for i in range(200)]
    d_small = diagnose_feature(small, "x", lambda r: r.score)

    # same correlation, far more evidence behind it
    big = [record(float(i), float(i) / 100) for i in range(5000)]
    d_big = diagnose_feature(big, "x", lambda r: r.score)

    assert d_big.z_score > d_small.z_score
    assert d_small.n < d_big.n


# --- verdicts ------------------------------------------------------------- #


def test_a_predictive_input_is_labelled_predictive() -> None:
    records = [record(float(i), float(i) / 100) for i in range(200)]
    d = diagnose_feature(records, "score", lambda r: r.score)
    assert d.correlation > 0.5
    assert d.verdict == "predictive"
    assert d.top_minus_bottom > 0


def test_an_inverted_input_is_called_out() -> None:
    """The finding this whole module exists to surface."""
    records = [record(float(i), -float(i) / 100) for i in range(200)]
    d = diagnose_feature(records, "score", lambda r: r.score)
    assert d.correlation < -0.5
    assert "ANTI-PREDICTIVE" in d.verdict
    assert d.top_minus_bottom < 0


def test_a_useless_input_is_called_noise_not_signal() -> None:
    import random

    rng = random.Random(11)
    records = [record(rng.uniform(0, 100), rng.gauss(0, 1)) for _ in range(2000)]
    d = diagnose_feature(records, "score", lambda r: r.score)
    assert abs(d.correlation) < 0.1
    assert "no signal" in d.verdict


def test_unresolved_candidates_are_excluded() -> None:
    records = [record(float(i), float(i) / 100) for i in range(100)]
    records += [record(50.0, None) for _ in range(50)]
    d = diagnose_feature(records, "score", lambda r: r.score)
    assert d.n == 100


# --- end to end ----------------------------------------------------------- #


def test_diagnose_covers_the_composite_and_every_numeric_feature() -> None:
    records = [
        record(float(i), float(i) / 100, adx=float(i), rsi=float(100 - i))
        for i in range(300)
    ]
    names = {d.name.strip() for d in diagnose(records)}
    assert "score (composite)" in names
    assert "setup_quality" in names
    assert "feature: adx" in names
    assert "feature: rsi" in names


def test_the_report_renders_and_states_a_conclusion() -> None:
    records = [record(float(i), -float(i) / 100) for i in range(300)]
    text = format_diagnosis(diagnose(records), records)
    assert "RANKING DIAGNOSTIC" in text
    assert "ANTI-PREDICTIVE" in text
    assert "what to do" in text
