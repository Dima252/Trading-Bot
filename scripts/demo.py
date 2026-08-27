"""Run the decision core against a hand-built book. No broker, no network.

    python scripts/demo.py

Shows the whole loop: defensive exits, stop maintenance, rotation, allocation,
and the constitution vetoing what doesn't fit.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot.core import (  # noqa: E402
    ActionKind,
    Candidate,
    ConstraintLayer,
    EntryType,
    EventFlags,
    MarketContext,
    Policy,
    Portfolio,
    Position,
    Regime,
    SetupType,
    decide,
    score_holding,
)

TODAY = date(2026, 3, 2)


def build_book() -> Portfolio:
    def pos(ticker, sector, qty, entry, current, stop, days, score, **kw):
        return Position(
            ticker=ticker,
            qty=qty,
            entry_price=entry,
            current_price=current,
            stop=stop,
            initial_stop=kw.get("initial_stop", stop),
            target=entry * 1.25,
            sector=sector,
            setup_type=SetupType.BREAKOUT,
            entry_score=score,
            opened_at=TODAY - timedelta(days=days),
            atr=kw.get("atr"),
            event_flags=kw.get("event_flags", EventFlags()),
        )

    return Portfolio(
        positions=[
            # working, far enough ahead to start trailing
            pos("NVDA", "Technology", 120, 100.0, 118.0, 95.0, 12, 82.0,
                initial_stop=95.0, atr=3.0),
            # going nowhere for two weeks -> time stop
            pos("KO", "Staples", 300, 60.0, 60.5, 57.0, 14, 55.0),
            # earnings landed inside the hold window -> defensive exit
            pos("CRM", "Technology", 90, 250.0, 262.0, 238.0, 6, 70.0,
                event_flags=EventFlags(
                    binary_event_in_window=True,
                    event_type="earnings",
                    event_date=TODAY + timedelta(days=4),
                    confidence="high",
                )),
            # mediocre and fully invested -> rotation candidate
            pos("XOM", "Energy", 400, 105.0, 104.0, 99.0, 4, 48.0),
        ],
        cash=12_000.0,
        equity=200_000.0,
        day_pnl_pct=-0.004,
        week_pnl_pct=0.011,
    )


def build_candidates() -> list[Candidate]:
    def cand(ticker, sector, setup, entry, stop, target, quality, **kw):
        return Candidate(
            ticker=ticker,
            setup_type=setup,
            entry_type=kw.get(
                "entry_type",
                EntryType.CLOSE_CONFIRM
                if setup is SetupType.BREAKOUT
                else EntryType.RESTING_LIMIT,
            ),
            entry=entry,
            stop=stop,
            target=target,
            sector=sector,
            setup_quality=quality,
            atr=kw.get("atr", 2.0),
            event_flags=kw.get("event_flags", EventFlags()),
        )

    return [
        cand("AVGO", "Technology", SetupType.BREAKOUT, 180.0, 168.0, 216.0, 92.0),
        cand("UNH", "Healthcare", SetupType.PULLBACK, 480.0, 455.0, 555.0, 78.0),
        cand("CAT", "Industrials", SetupType.BREAKOUT, 340.0, 322.0, 394.0, 71.0),
        cand("PFE", "Healthcare", SetupType.MEAN_REVERSION, 27.0, 25.5, 30.0, 64.0),
        # strong setup, but the semantic engine found a binary event
        cand("MRNA", "Healthcare", SetupType.BREAKOUT, 95.0, 88.0, 116.0, 96.0,
             event_flags=EventFlags(
                 binary_event_in_window=True,
                 event_type="FDA decision",
                 confidence="high",
                 rationale="PDUFA date in 6 days",
             )),
    ]


def main() -> None:
    policy_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "policy.yaml",
    )
    try:
        policy = Policy.from_yaml(policy_path)
        source = "config/policy.yaml"
    except ImportError:
        policy = Policy()
        source = "built-in defaults (pip install pyyaml to load the file)"

    portfolio = build_book()
    candidates = build_candidates()
    context = MarketContext(as_of=TODAY, regime=Regime.TREND, breadth=0.62)

    print(f"\npolicy {policy.version} from {source}")
    print(f"as of {TODAY}, regime={context.regime.value}")
    print(
        f"equity ${portfolio.equity:,.0f}  cash ${portfolio.cash:,.0f}  "
        f"heat {portfolio.open_heat_pct:.2%} of {policy.max_portfolio_heat:.0%} cap"
    )

    print("\n--- book ---")
    for p in sorted(portfolio.positions, key=lambda x: x.ticker):
        held = score_holding(p, None, context, policy)
        print(
            f"  {p.ticker:<5} {p.qty:>4} sh  ${p.market_value:>9,.0f}  "
            f"{p.unrealized_r:>+5.2f}R  {p.days_held(TODAY):>2}d  "
            f"score {held:>5.1f}  {p.sector}"
        )

    print("\n--- candidates ---")
    from trading_bot.core import rank_candidates

    for c in rank_candidates(candidates, context, policy):
        flag = " [event]" if c.event_flags.penalty >= 1.0 else ""
        print(
            f"  {c.ticker:<5} {c.setup_type.value:<14} score {c.score:>5.1f}  "
            f"{c.reward_risk:.1f}R:R  {c.sector}{flag}"
        )

    actions = decide(portfolio, candidates, context, policy)

    print(f"\n--- decide() proposed {len(actions)} actions ---")
    for a in actions:
        qty = f"{a.qty} sh" if a.qty else ""
        print(f"  {a.kind.value:<12} {a.ticker:<5} {qty:<9} {a.reason}")

    sectors = {c.ticker: c.sector for c in candidates}
    verdict = ConstraintLayer(policy, sectors=sectors).validate(actions, portfolio)

    print(f"\n--- constitution: {len(verdict.approved)} approved ---")
    for a in verdict.approved:
        if a.kind is ActionKind.OPEN:
            print(
                f"  OPEN {a.ticker:<5} {a.qty} sh @ {a.limit} "
                f"stop {a.stop} target {a.target} ({a.entry_type.value})"
            )
        else:
            print(f"  {a.kind.value:<12} {a.ticker}")

    if verdict.rejected:
        print(f"\n--- vetoed: {len(verdict.rejected)} ---")
        for r in verdict.rejected:
            print(f"  {r.action.kind.value} {r.action.ticker}: [{r.rule}] {r.detail}")
    print()


if __name__ == "__main__":
    main()
