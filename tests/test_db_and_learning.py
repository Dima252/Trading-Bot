"""State store round-trips, and the learning layer's guardrails."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import make_candidate
from trading_bot.core.models import EventFlags, SetupType
from trading_bot.core.policy import Policy
from trading_bot.data.cache import BarCache
from trading_bot.data.models import Bar, BarSeries
from trading_bot.db.repo import Repo
from trading_bot.learning.attribution import build_live_report, trades_from_db
from trading_bot.learning.tune import MIN_SAMPLE, propose, snapshot_policy

DAY = date(2026, 3, 2)


@pytest.fixture
def repo() -> Repo:
    return Repo(":memory:")


# --- bar cache ------------------------------------------------------------ #


def test_bar_cache_round_trips(tmp_path) -> None:
    cache = BarCache(tmp_path / "bars.db")
    bars = [
        Bar(DAY + timedelta(days=i), 10.0, 11.0, 9.0, 10.5, 1_000.0) for i in range(5)
    ]
    cache.store(BarSeries("AAA", bars))

    loaded = cache.load("AAA")
    assert len(loaded) == 5
    assert loaded.days == [b.day for b in bars]
    assert cache.coverage("AAA") == (bars[0].day, bars[-1].day)
    assert cache.symbols() == ["AAA"]


def test_bar_cache_is_idempotent(tmp_path) -> None:
    """Re-storing overlapping data must not duplicate bars."""
    cache = BarCache(tmp_path / "bars.db")
    bars = [Bar(DAY + timedelta(days=i), 10, 11, 9, 10.5, 1) for i in range(5)]
    cache.store(BarSeries("AAA", bars))
    cache.store(BarSeries("AAA", bars[2:]))
    assert len(cache.load("AAA")) == 5


# --- candidates ----------------------------------------------------------- #


def test_candidates_round_trip(repo: Repo) -> None:
    cands = [make_candidate("AAA"), make_candidate("BBB")]
    repo.save_candidates(DAY, cands, "v1")

    loaded = repo.pending_candidates(DAY)
    assert {c.ticker for c in loaded} == {"AAA", "BBB"}
    assert loaded[0].setup_type is cands[0].setup_type
    assert loaded[0].entry == cands[0].entry


def test_cancelling_a_candidate_removes_it_from_pending(repo: Repo) -> None:
    repo.save_candidates(DAY, [make_candidate("AAA")], "v1")
    repo.set_candidate_status(DAY, "AAA", "Cancelled", "earnings")
    assert repo.pending_candidates(DAY) == []


def test_event_flags_survive_a_round_trip(repo: Repo) -> None:
    repo.save_candidates(DAY, [make_candidate("AAA")], "v1")
    flags = EventFlags(
        binary_event_in_window=True,
        event_type="earnings",
        event_date=DAY + timedelta(days=4),
        confidence="high",
        rationale="Q3 print",
    )
    repo.update_candidate_flags(DAY, "AAA", flags)

    loaded = repo.pending_candidates(DAY)[0]
    assert loaded.event_flags.event_date == DAY + timedelta(days=4)
    assert loaded.event_flags.penalty == 1.0


def test_latest_candidate_day_ignores_the_future(repo: Repo) -> None:
    repo.save_candidates(DAY, [make_candidate("AAA")], "v1")
    repo.save_candidates(DAY + timedelta(days=5), [make_candidate("BBB")], "v1")
    assert repo.latest_candidate_day(DAY + timedelta(days=1)) == DAY
    assert repo.latest_candidate_day() == DAY + timedelta(days=5)
    assert Repo(":memory:").latest_candidate_day() is None


# --- flags and equity ----------------------------------------------------- #


def test_kill_switch_round_trips(repo: Repo) -> None:
    assert repo.is_halted() is False
    repo.set_halt(True, "drawdown")
    assert repo.is_halted() is True
    assert repo.get_flag("HALT_REASON") == "drawdown"
    repo.set_halt(False)
    assert repo.is_halted() is False


def test_equity_history_finds_the_nearest_earlier_day(repo: Repo) -> None:
    repo.record_equity(DAY - timedelta(days=10), cash=0, equity=90_000)
    repo.record_equity(DAY - timedelta(days=2), cash=0, equity=95_000)
    assert repo.equity_asof(DAY) == 95_000
    assert repo.equity_asof(DAY - timedelta(days=5)) == 90_000
    assert repo.equity_asof(DAY - timedelta(days=30)) is None


# --- attribution ---------------------------------------------------------- #


def seed_trades(repo: Repo, n: int, realized_r: float, mfe_r: float = 0.2) -> None:
    for i in range(n):
        repo.record_trade(
            ticker=f"T{i}",
            sector="Technology",
            setup_type=SetupType.BREAKOUT.value,
            regime_at_entry="trend",
            entry_day=(DAY - timedelta(days=20)).isoformat(),
            entry_price=100.0,
            exit_day=(DAY - timedelta(days=10)).isoformat(),
            exit_price=100.0 + realized_r * 5,
            qty=100,
            initial_stop=95.0,
            target=115.0,
            exit_reason="stop" if realized_r < 0 else "target",
            realized_r=realized_r,
            pnl=realized_r * 500,
            mfe_r=mfe_r,
            mae_r=-0.5,
            days_held=10,
            entry_score=70.0,
            policy_version="v1",
        )


def test_trades_load_back_as_records(repo: Repo) -> None:
    seed_trades(repo, 3, realized_r=1.5)
    records = trades_from_db(repo)
    assert len(records) == 3
    assert records[0].setup_type is SetupType.BREAKOUT
    assert records[0].realized_r == 1.5


def test_unreconstructable_trades_are_excluded_not_distorted(repo: Repo) -> None:
    seed_trades(repo, 2, realized_r=1.0)
    repo.record_trade(
        ticker="GHOST",
        sector="Technology",
        setup_type="breakout",
        entry_day=DAY.isoformat(),
        entry_price=0.0,
        exit_day=DAY.isoformat(),
        exit_price=50.0,
        qty=0,
        initial_stop=45.0,
        target=65.0,
        exit_reason="unreconciled_exit:no_entry_snapshot",
        realized_r=0.0,
        pnl=0.0,
        policy_version="v1",
    )
    assert {r.ticker for r in trades_from_db(repo)} == {"T0", "T1"}


def test_live_report_builds_from_the_database(repo: Repo) -> None:
    seed_trades(repo, 10, realized_r=1.2)
    seed_trades(repo, 5, realized_r=-1.0)
    repo.record_equity(DAY - timedelta(days=30), cash=0, equity=100_000)
    repo.record_equity(DAY, cash=0, equity=112_000)

    report = build_live_report(repo)
    assert report.overall.trades == 15
    assert report.overall.win_rate == pytest.approx(10 / 15, abs=0.01)
    assert "breakout" in report.by_setup


# --- tuning guardrails ---------------------------------------------------- #


def test_no_proposal_without_enough_evidence(repo: Repo) -> None:
    seed_trades(repo, 5, realized_r=-1.0)
    out = propose(repo, build_live_report(repo), Policy())
    assert "insufficient evidence" in out[0]
    assert repo.proposals() == []


def test_a_tight_stop_pattern_produces_a_proposal(repo: Repo) -> None:
    """Most losers reaching +1R first is the signature of a stop inside the noise."""
    seed_trades(repo, MIN_SAMPLE + 5, realized_r=-1.0, mfe_r=1.4)
    out = propose(repo, build_live_report(repo), Policy())

    assert any("MIN_STOP_ATR" in line for line in out)
    saved = repo.proposals()
    assert len(saved) == 1
    assert saved[0]["sample_size"] >= MIN_SAMPLE
    assert "first reached" in saved[0]["evidence"]


def test_proposals_are_rate_limited(repo: Repo) -> None:
    """Change two things at once and you can attribute neither."""
    seed_trades(repo, MIN_SAMPLE + 5, realized_r=-1.0, mfe_r=1.4)
    report = build_live_report(repo)

    propose(repo, report, Policy())
    second = propose(repo, report, Policy())

    assert "rate limited" in second[0]
    assert len(repo.proposals()) == 1


def test_nothing_is_ever_applied_silently(repo: Repo) -> None:
    seed_trades(repo, MIN_SAMPLE + 5, realized_r=-1.0, mfe_r=1.4)
    policy = Policy()
    propose(repo, build_live_report(repo), policy)

    # the proposal exists, the running policy is untouched
    assert repo.proposals()
    assert policy == Policy()
    assert all(p["status"] == "proposed" for p in repo.proposals())


def test_policy_snapshot_makes_versions_resolvable(repo: Repo) -> None:
    snapshot_policy(repo, Policy(), note="initial")
    active = repo.active_policy()
    assert active["version"] == "v1"
    assert active["max_risk_per_trade"] == 0.01


# --- universe loading ----------------------------------------------------- #


def test_the_wide_universe_loads_with_sectors() -> None:
    from trading_bot.data.universe import load_sectors

    wide = load_sectors(wide=True)
    assert len(wide) > 400
    assert all(isinstance(v, str) and v for v in wide.values())
    assert "AAPL" in wide


def test_the_narrow_list_is_the_fallback() -> None:
    """A missing snapshot must fall back, never trade with no sector map."""
    from trading_bot.data import universe as u

    narrow = u.load_sectors(wide=False)
    assert 50 < len(narrow) < 200
    assert set(narrow.values())  # every name has a sector


def test_a_missing_snapshot_falls_back(monkeypatch) -> None:
    from trading_bot.data import universe as u

    monkeypatch.setattr(u, "WIDE_UNIVERSE_FILE", "does/not/exist.json")
    assert u.load_sectors(wide=True) == u.load_sectors(wide=False)


def test_index_and_rate_series_are_not_tradeable() -> None:
    from trading_bot.data.universe import is_tradeable

    assert not is_tradeable("SPY")
    assert not is_tradeable("^IRX")
    assert is_tradeable("AAPL")
