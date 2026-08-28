"""Adaptive policy tuning -- proposals only.

A self-adjusting agent with no brakes overfits to its last ten trades. The
guardrails here are the point of the module, not an afterthought:

* nothing changes silently -- everything lands in `strategy_changes` with its
  evidence and sample size
* no proposal from a bucket with fewer than `MIN_SAMPLE` trades
* one change at a time, rate-limited -- change two and you can attribute neither
* the policy is versioned, so every trade records what produced it

This does not apply changes. A human (or a later, better-evidenced process)
promotes a proposal by activating a new policy version.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..backtest.metrics import Report
from ..core.policy import Policy
from ..db.repo import Repo

MIN_SAMPLE = 30
MIN_DAYS_BETWEEN_CHANGES = 30


def propose(repo: Repo, report: Report, policy: Policy) -> list[str]:
    """Look for changes the evidence actually supports. Returns descriptions."""
    if _changed_recently(repo):
        return ["rate limited: a change was proposed within the last 30 days"]

    proposals: list[str] = []
    diag = report.diagnostics
    overall = report.overall

    if overall.trades < MIN_SAMPLE:
        return [
            f"insufficient evidence: {overall.trades} trades, need {MIN_SAMPLE}"
        ]

    # --- stops inside the noise ---
    if diag.stops_too_tight:
        proposals.append(
            _record(
                repo,
                "setups.MIN_STOP_ATR",
                1.0,
                1.5,
                f"{diag.pct_losers_that_reached_1r:.0%} of losers first reached "
                f"+1R (avg MFE {diag.avg_mfe_of_losers:.2f}R) -- the stop is "
                "being hit by noise before the thesis resolves",
                overall.trades,
            )
        )

    # --- targets cutting winners short ---
    elif diag.targets_too_tight:
        proposals.append(
            _record(
                repo,
                "setups.REWARD_RISK",
                3.0,
                3.5,
                f"names ran a further {diag.avg_post_exit_r_after_target:.2f}R "
                "in the 10 days after a target exit",
                overall.trades,
            )
        )

    # --- a setup that does not work in a regime ---
    else:
        worst = _worst_bucket(report)
        if worst is not None:
            name, stats = worst
            proposals.append(
                _record(
                    repo,
                    f"regime_fit.{name}",
                    "current",
                    "reduce",
                    f"{name}: {stats.trades} trades, expectancy "
                    f"{stats.expectancy_r:+.3f}R, total {stats.total_r:+.1f}R",
                    stats.trades,
                )
            )

    # --- a filter that is costing more than it saves ---
    for verdict in report.shadow:
        if verdict.count < MIN_SAMPLE:
            continue
        if verdict.expectancy_r > overall.expectancy_r + 0.15:
            proposals.append(
                _record(
                    repo,
                    f"filter.{verdict.reason}",
                    "enabled",
                    "review",
                    f"declined trades under '{verdict.reason}' returned "
                    f"{verdict.expectancy_r:+.3f}R over {verdict.count} samples "
                    f"vs {overall.expectancy_r:+.3f}R for trades actually taken",
                    verdict.count,
                )
            )
            break  # one at a time

    return proposals or ["no change supported by the current evidence"]


def _worst_bucket(report: Report):
    candidates = [
        (name, stats)
        for name, stats in {**report.by_setup, **report.by_regime}.items()
        if stats.is_meaningful and stats.expectancy_r < -0.10
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda kv: kv[1].expectancy_r)


def _record(
    repo: Repo, field: str, from_value, to_value, evidence: str, sample: int
) -> str:
    repo.propose_change(field, from_value, to_value, evidence, sample)
    return f"{field}: {from_value} -> {to_value} ({sample} samples) -- {evidence}"


def _changed_recently(repo: Repo) -> bool:
    rows = repo.conn.execute(
        "SELECT proposed_at FROM strategy_changes ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not rows:
        return False
    last = datetime.fromisoformat(rows["proposed_at"])
    return (datetime.now(UTC) - last) < timedelta(
        days=MIN_DAYS_BETWEEN_CHANGES
    )


def snapshot_policy(repo: Repo, policy: Policy, note: str = "") -> None:
    """Persist the running policy so every trade's `policy_version` resolves."""
    from dataclasses import asdict

    repo.save_policy(policy.version, asdict(policy), note)
    repo.activate_policy(policy.version)
