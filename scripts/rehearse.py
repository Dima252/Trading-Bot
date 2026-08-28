"""Replay the four jobs day by day over real bars against the paper broker.

This is the dry-run rehearsal from PLAN.md C4, compressed. It exercises the live
job code -- reconcile, decide, constitution, order submission, idempotency, the
decision log -- against real price action rather than fixtures, and leaves a
populated state database for the dashboard to render.

    python scripts/rehearse.py --days 60

IMPORTANT: this deliberately runs over an ALREADY-USED fold period. Rehearsing
across the holdout (2025-06-11 onward) would contaminate the one stretch of
history no decision has touched.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot.broker.paper import PaperBroker
from trading_bot.core.policy import Policy
from trading_bot.data.cache import BarCache
from trading_bot.data.universe import BENCHMARK, SECTORS
from trading_bot.db.repo import Repo
from trading_bot.jobs import close_job, evening, open_job, premarket
from trading_bot.jobs.base import AgentContext
from trading_bot.semantic.client import NullSemanticEngine

# The last stretch of fold F4. Never crosses into the holdout.
DEFAULT_END = date(2025, 6, 10)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bars", default="data/bars.db")
    ap.add_argument("--state", default="data/rehearsal.db")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--shallow", action="store_true", help="use the shallow variant")
    ap.add_argument("--trend-only", action="store_true", help="gate to trend regimes")
    args = ap.parse_args()

    if args.end > date(2025, 6, 10):
        sys.exit("refusing to rehearse across the holdout period -- see PLAN.md")

    if os.path.exists(args.state):
        os.remove(args.state)

    cache = BarCache(args.bars)
    symbols = cache.symbols()
    if BENCHMARK not in symbols:
        sys.exit(f"no {BENCHMARK} in {args.bars} -- run `fetch` first")

    universe = {s: cache.load(s) for s in symbols}
    sessions = [d for d in universe[BENCHMARK].days if d <= args.end][-args.days :]

    policy = Policy(
        pullback_favour_shallow=args.shallow,
        tradeable_regimes=["trend"] if args.trend_only else ["trend", "chop", "high_vol"],
    )
    broker = PaperBroker(args.equity)
    repo = Repo(args.state)

    def context(day: date) -> AgentContext:
        return AgentContext(
            repo=repo,
            broker=broker,
            cache=cache,
            policy=policy,
            sectors=dict(SECTORS),
            day=day,
        )

    print(f"rehearsing {len(sessions)} sessions, {sessions[0]} -> {sessions[-1]}")
    print(f"policy: shallow={args.shallow} trend_only={args.trend_only}\n")

    def advance(day: date) -> list[str]:
        events = []
        for symbol, series in universe.items():
            if symbol == BENCHMARK:
                continue
            i = series.index_of(day)
            if i is not None:
                events += broker.advance(symbol, series[i])
        return events

    def closes_on(day: date) -> dict[str, float]:
        out = {}
        for symbol, series in universe.items():
            i = series.index_of(day)
            if i is not None:
                out[symbol] = series[i].close
        return out

    # Seed a watchlist from the session before the window opens.
    evening.run(context(sessions[0]))

    for n, day in enumerate(sessions[1:], 1):
        ctx = context(day)

        premarket.run(ctx, NullSemanticEngine())
        open_result = open_job.run(ctx)
        events = advance(day)                      # resting limits fill, OCO fires
        close_result = close_job.run(ctx, prices=closes_on(day))
        events += advance(day)                     # close-confirmed entries fill
        evening.run(ctx)

        broker.last_equity = broker.account().equity

        account = broker.account()
        traded = [
            a.kind.value + " " + a.ticker
            for a in open_result.executed + close_result.executed
        ]
        line = (
            f"  [{n:>3}/{len(sessions) - 1}] {day}  "
            f"equity ${account.equity:>10,.0f}  "
            f"pos {len(broker.positions()):>2}"
        )
        if traded:
            line += "  " + ", ".join(traded[:3])
        if events:
            line += "  |  " + "; ".join(e for e in events if "filled" not in e)[:70]
        print(line.rstrip(" |"))

    account = broker.account()
    trades = repo.trades(10_000)
    wins = [t for t in trades if t["pnl"] > 0]
    print()
    print(f"final equity   ${account.equity:,.2f}  ({account.equity / args.equity - 1:+.2%})")
    print(f"open positions {len(broker.positions())}")
    print(f"closed trades  {len(trades)}", end="")
    if trades:
        print(f"  ({len(wins) / len(trades):.0%} winners)")
    else:
        print()
    print(f"decisions      {len(repo.recent_decisions(10_000))}")
    print(f"\nstate written to {args.state}")
    print(f"  python -m trading_bot dashboard --state {args.state} --day {sessions[-1]}")


if __name__ == "__main__":
    main()
