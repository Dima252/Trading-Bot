"""Command line entry point.

    python -m trading_bot <command>

The four job commands are what cron calls. Everything else is operator tooling.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from datetime import date, timedelta

from .core.policy import Policy
from .data.cache import BarCache
from .db.repo import Repo
from .market_hours import today_exchange

DEFAULT_POLICY = "config/policy.yaml"
DEFAULT_BARS = "data/bars.db"
DEFAULT_STATE = "data/state.db"


def load_env(path: str = ".env") -> int:
    """Read KEY=VALUE lines from a .env file into the environment.

    Existing environment variables always win, so an explicit `export` or a
    systemd EnvironmentFile overrides the file rather than being silently
    ignored. Ten lines instead of a dependency.
    """
    import os

    file = pathlib.Path(path)
    if not file.exists():
        return 0

    loaded = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _load_policy(path: str) -> Policy:
    try:
        return Policy.from_yaml(path)
    except FileNotFoundError:
        print(f"no policy at {path}, using built-in defaults", file=sys.stderr)
        return Policy()


def _context(args, dry_run: bool | None = None):
    from .broker.alpaca import AlpacaBroker
    from .data.universe import SECTORS
    from .jobs.base import AgentContext

    return AgentContext(
        repo=Repo(args.state),
        broker=AlpacaBroker(paper=not args.live),
        cache=BarCache(args.bars),
        policy=_load_policy(args.policy),
        sectors=dict(SECTORS),
        day=args.day,
        dry_run=args.dry_run if dry_run is None else dry_run,
    )


# --------------------------------------------------------------------- #


def cmd_fetch(args) -> int:
    """Populate the bar cache.

    Defaults to Yahoo, which needs no credentials -- the real backtest does not
    have to wait for broker keys. Use `--source alpaca` once they exist and you
    want the same feed live trading will see.
    """
    from .data.universe import all_symbols
    from .market_hours import SESSION_CLOSE, now_exchange, session_is_final

    cache = BarCache(args.bars)
    symbols = all_symbols()
    start = args.day - timedelta(days=args.days)
    print(
        f"fetching {len(symbols)} symbols from {start} to {args.day} "
        f"via {args.source}"
    )
    if not session_is_final(args.day):
        # Not fatal: the refresh re-reads the last cached session, so running
        # again after the bell overwrites whatever is stored now. Worth saying
        # out loud anyway, because the bar looks identical either way and
        # `evening` will refuse to scan until the session has settled.
        print(
            f"  note: {args.day} is still open ({now_exchange():%H:%M} ET, bell "
            f"at {SESSION_CLOSE:%H:%M}). Today's bars will be provisional -- "
            "re-run this after the close."
        )
    print()

    if args.source == "yahoo":
        from .data.yahoo import refresh_cache

        written = refresh_cache(
            cache, symbols, start, args.day, incremental=not args.full
        )
    else:
        from .data.alpaca_data import refresh_cache as alpaca_refresh

        written = alpaca_refresh(
            cache, symbols, start, args.day, incremental=not args.full
        )

    stats = cache.stats()
    missing = sorted(set(symbols) - set(cache.symbols()))
    print()
    print(f"wrote {sum(written.values())} bars")
    print(f"cache holds {stats['bars']} bars across {stats['symbols']} symbols")
    if missing:
        print(f"missing ({len(missing)}): {', '.join(missing)}")
    return 0


def cmd_backtest(args) -> int:
    from .backtest.engine import BacktestConfig, run_backtest
    from .backtest.metrics import build_report, format_report
    from .data.universe import BENCHMARK, CASH_RATE, SECTORS
    from .signals.engine import MIN_HISTORY

    cache = BarCache(args.bars)
    symbols = cache.symbols()
    if BENCHMARK not in symbols:
        print(f"no {BENCHMARK} in the cache -- run `fetch` first", file=sys.stderr)
        return 1

    universe = cache.load_many(symbols, min_bars=MIN_HISTORY)
    start = args.start or (min(universe[BENCHMARK].days) + timedelta(days=400))
    config = BacktestConfig(
        start=start,
        end=args.day,
        starting_equity=args.equity,
        benchmark=BENCHMARK,
        slippage_bps=args.slippage,
        cash_rate_symbol=CASH_RATE if CASH_RATE in universe else None,
    )
    print(f"backtesting {len(universe) - 1} symbols, {start} -> {args.day}\n")

    result = run_backtest(universe, dict(SECTORS), config, _load_policy(args.policy))
    print(format_report(build_report(result), result.universe_size))
    return 0


def cmd_diagnose(args) -> int:
    """Does a higher score actually predict a better outcome?"""
    from .backtest.engine import BacktestConfig, run_backtest
    from .data.universe import BENCHMARK, SECTORS
    from .learning.diagnose import diagnose, format_diagnosis
    from .signals.engine import MIN_HISTORY

    cache = BarCache(args.bars)
    symbols = cache.symbols()
    if BENCHMARK not in symbols:
        print(f"no {BENCHMARK} in the cache -- run `fetch` first", file=sys.stderr)
        return 1

    universe = cache.load_many(symbols, min_bars=MIN_HISTORY)
    start = args.start or (min(universe[BENCHMARK].days) + timedelta(days=400))
    print(f"replaying {len(universe) - 1} symbols, {start} -> {args.day}")
    print()

    result = run_backtest(
        universe,
        dict(SECTORS),
        BacktestConfig(start=start, end=args.day, benchmark=BENCHMARK),
        _load_policy(args.policy),
    )
    print(format_diagnosis(diagnose(result.shadow), result.shadow))
    return 0


# The variants under test. Each is a hypothesis stated BEFORE the run, derived
# from the ranking diagnostic -- not a parameter found by searching.
#
# These are built from the PINNED library defaults, never from config/policy.yaml.
# Once the shipping config was frozen to the holdout-tested variant, deriving
# them from the live config would have silently redefined "baseline" and made
# every recorded research result unreproducible.
def _variants(_ignored: Policy | None = None) -> dict[str, Policy]:
    base = Policy()
    return {
        "baseline": base,
        # depth_atr z=-12.1, rsi z=+16.4: the pullback quality score rewards
        # deep dips and low RSI, and the data says both signs are inverted.
        "shallow": base.with_changes(
            version=f"{base.version}-shallow", pullback_favour_shallow=True
        ),
        # untouched candidates returned +0.25R against -0.009R for managed ones;
        # the 10-day time stop may be cutting trades before they resolve.
        "long_hold": base.with_changes(
            version=f"{base.version}-longhold", time_stop_days=40, max_hold_days=60
        ),
        "both": base.with_changes(
            version=f"{base.version}-both",
            pullback_favour_shallow=True,
            time_stop_days=40,
            max_hold_days=60,
        ),
        # trend +0.129R vs chop -0.144R over ~535 trades each: the system bleeds
        # steadily in chop and should not be trading there at all.
        "trend_only": base.with_changes(
            version=f"{base.version}-trendonly", tradeable_regimes=["trend"]
        ),
        # all three corrections together
        "all_three": base.with_changes(
            version=f"{base.version}-all3",
            pullback_favour_shallow=True,
            time_stop_days=40,
            max_hold_days=60,
            tradeable_regimes=["trend"],
        ),
        # ONE change against all_three: scale the whole risk envelope by 1.6x.
        # all_three uses ~8% of a 15% drawdown tolerance, so the budget is half
        # spent. R multiples are size-invariant, so if this is purely a sizing
        # change expectancy should be UNCHANGED and returns should scale.
        # Expectancy moving is the tell that something else changed.
        "risk_1p6": base.with_changes(
            version=f"{base.version}-risk16",
            pullback_favour_shallow=True,
            time_stop_days=40,
            max_hold_days=60,
            tradeable_regimes=["trend"],
            max_risk_per_trade=0.016,
            max_portfolio_heat=0.096,
            max_position_pct=0.24,
        ),
        # NOTE: `defensive` below bundles five changes at once (risk envelope AND
        # the pullback weights) and its result is therefore uninformative about
        # which one mattered -- it violates rule 3 in PLAN section 7. Kept only
        # as a record of that mistake.
        # all_three earns +4.87% CAGR at a 9.17% worst drawdown -- it leaves
        # most of a defensive risk budget unused, and cash-like returns are not
        # worth running a bot for. Scale risk to fill the envelope and put the
        # weight on the one validated selection feature.
        "defensive": base.with_changes(
            version=f"{base.version}-def",
            pullback_favour_shallow=True,
            time_stop_days=40,
            max_hold_days=60,
            tradeable_regimes=["trend"],
            max_risk_per_trade=0.0175,
            max_portfolio_heat=0.10,
            max_position_pct=0.20,
            max_new_positions_per_day=4,
            pullback_w_trend=0.20,
            pullback_w_reset=0.50,
            pullback_w_depth=0.30,
        ),
        # The regime gate improves per-trade expectancy but shuts the book 54%
        # of the time, and SPY's forward 20-day return barely differs by regime
        # label (chop +0.98% vs trend +1.24%). Hypothesis: for total return the
        # gate is net negative even though it flatters expectancy.
        "defensive_open": base.with_changes(
            version=f"{base.version}-defopen",
            pullback_favour_shallow=True,
            time_stop_days=40,
            max_hold_days=60,
            max_risk_per_trade=0.0175,
            max_portfolio_heat=0.10,
            max_position_pct=0.20,
            max_new_positions_per_day=4,
            pullback_w_trend=0.20,
            pullback_w_reset=0.50,
            pullback_w_depth=0.30,
        ),
    }


def cmd_walkforward(args) -> int:
    from .backtest.walkforward import (
        format_walkforward,
        make_folds,
        run_walkforward,
    )
    from .data.universe import BENCHMARK, CASH_RATE, SECTORS
    from .signals.engine import MIN_HISTORY

    cache = BarCache(args.bars)
    symbols = cache.symbols()
    if BENCHMARK not in symbols:
        print(f"no {BENCHMARK} in the cache -- run `fetch` first", file=sys.stderr)
        return 1

    universe = cache.load_many(symbols, min_bars=MIN_HISTORY)
    if CASH_RATE not in universe:
        print(f"note: no {CASH_RATE} cached -- idle cash will earn 0%")
    folds, holdout = make_folds(
        universe[BENCHMARK].days, n_folds=args.folds, holdout=not args.no_holdout
    )

    base = _load_policy(args.policy)
    variants = _variants(base)
    if args.only:
        variants = {k: v for k, v in variants.items() if k in {"baseline", *args.only}}

    print(f"{len(folds)} folds x {len(variants)} variants over {len(universe) - 1} symbols")
    if holdout:
        print(f"holding out {holdout.span} -- untouched by every run below")
    print()

    wf = run_walkforward(
        universe, dict(SECTORS), variants, folds, benchmark=BENCHMARK,
        cash_rate_symbol=CASH_RATE,
    )
    wf.holdout = holdout
    print()
    print(format_walkforward(wf))
    return 0


def cmd_holdout(args) -> int:
    """Spend the holdout. Once.

    Everything else in this tool can be re-run freely. This cannot: the value of
    the holdout is that no decision has been fitted to it, and running a variant
    here consumes that for the variant. The result is written to disk so the
    event is on the record rather than in someone's memory.
    """
    from .backtest.walkforward import (
        format_walkforward,
        make_folds,
        run_walkforward,
    )
    from .data.universe import BENCHMARK, CASH_RATE, SECTORS
    from .signals.engine import MIN_HISTORY

    cache = BarCache(args.bars)
    universe = cache.load_many(cache.symbols(), min_bars=MIN_HISTORY)
    if BENCHMARK not in universe:
        print(f"no {BENCHMARK} in the cache -- run `fetch` first", file=sys.stderr)
        return 1

    _, holdout = make_folds(universe[BENCHMARK].days, n_folds=args.folds)
    if holdout is None:
        print("no holdout period available", file=sys.stderr)
        return 1

    base = _load_policy(args.policy)
    variants = {k: v for k, v in _variants(base).items() if k in args.variants}
    if not variants:
        print(f"unknown variants: {args.variants}", file=sys.stderr)
        return 1

    print("=" * 78)
    print("SPENDING THE HOLDOUT")
    print("=" * 78)
    print(f"  period   {holdout.span}")
    print(f"  variants {', '.join(variants)}")
    print("  This period has not informed any decision. After this run it has.")
    print()

    wf = run_walkforward(
        universe, dict(SECTORS), variants, [holdout],
        baseline=args.variants[0], benchmark=BENCHMARK, cash_rate_symbol=CASH_RATE,
    )
    text = format_walkforward(wf)
    print()
    print(text)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = "\n".join(
        [
            f"HOLDOUT SPENT {today_exchange()}",
            f"period {holdout.span}",
            f"variants {', '.join(variants)}",
            "",
            "",
        ]
    )
    out.write_text(header + text + "\n", encoding="utf-8")
    print()
    print(f"recorded to {out}")
    return 0


def cmd_job(args) -> int:
    from .jobs import close_job, evening, open_job, premarket
    from .jobs.base import regime_on_file

    ctx = _context(args)
    if args.command == "evening":
        result = evening.run(ctx)
    elif args.command == "premarket":
        engine = None
        if not args.no_llm:
            try:
                from .semantic.client import ClaudeSemanticEngine

                engine = ClaudeSemanticEngine()
            except Exception as exc:
                print(f"semantic engine unavailable ({exc}); running calendar-only")
        result = premarket.run(ctx, engine)
    elif args.command == "open":
        result = open_job.run(ctx, regime_on_file(ctx.repo, ctx.day))
    else:
        from .data.alpaca_data import AlpacaData

        day = ctx.repo.latest_candidate_day(ctx.day)
        pending = ctx.repo.pending_candidates(day) if day else []
        prices = AlpacaData().latest_prices([c.ticker for c in pending])
        result = close_job.run(ctx, prices, regime_on_file(ctx.repo, ctx.day))

    print(f"\n[{result.job}] {result.status}: {result.summary}")
    for note in result.notes:
        print(f"  {note}")
    return 0 if result.status in ("ok", "skipped") else 1


def cmd_daily(args) -> int:
    """The nightly routine as one command: fetch, think, render.

    This is what the dry run actually consists of, and running it as three
    separate commands invites two failure modes -- doing them out of order, and
    doing only the first two. Order matters: the scanner matches session dates
    exactly, so `evening` against an unrefreshed cache sees nothing at all.

    Dry run is the DEFAULT. Sending real orders takes `--arm`, because the
    difference between a rehearsal and six months of live decisions should be a
    word you had to type, not a flag you forgot.
    """
    from .jobs import evening
    from .market_hours import SESSION_CLOSE, now_exchange, session_is_final
    from .ui.dashboard import write

    if not session_is_final(args.day):
        print(
            f"{args.day} has not closed yet -- it is {now_exchange():%H:%M} in "
            f"New York and the bell is at {SESSION_CLOSE:%H:%M}."
        )
        print(
            "Nothing to do until then: today's bars would be provisional, and "
            "the scan would price off a quote that is still moving."
        )
        return 1

    mode = "ARMED -- orders will be sent" if args.arm else "dry run -- nothing sent"
    print(f"daily run for {args.day}  ({mode})")
    print("=" * 64)

    print()
    print("[1/3] refreshing bars")
    rc = cmd_fetch(args)
    if rc:
        print(
            "fetch failed -- stopping before the scan, which would otherwise "
            "read a stale cache and report a clean zero."
        )
        return rc

    print()
    print("[2/3] scanning")
    ctx = _context(args, dry_run=not args.arm)
    result = evening.run(ctx)
    print()
    print(f"[{result.job}] {result.status}: {result.summary}")
    for note in result.notes:
        print(f"  {note}")

    print()
    print("[3/3] rendering the dashboard")
    out = write(
        ctx.repo, args.out, cache=ctx.cache, policy=ctx.policy, as_of=args.day
    )
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print()
    print(f"read it:  {'start' if sys.platform == 'win32' else 'open'} {out}")

    return 0 if result.status in ("ok", "skipped") else 1


def cmd_dashboard(args) -> int:
    """Render the state database as one self-contained HTML page."""
    from .ui.dashboard import write

    out = write(
        Repo(args.state),
        args.out,
        cache=BarCache(args.bars),
        policy=_load_policy(args.policy),
        as_of=args.day,
    )
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)")
    print(f"open it with:  start {out}" if sys.platform == "win32" else f"  open {out}")
    return 0


def cmd_serve(args) -> int:
    from .ui.server import serve

    serve(
        host=args.host, port=args.port, state=args.state,
        bars=args.bars, policy=args.policy, token=args.token,
    )
    return 0


def cmd_report(args) -> int:
    from .learning.attribution import format_live_report

    print(format_live_report(Repo(args.state)))
    return 0


def cmd_propose(args) -> int:
    from .learning.attribution import build_live_report
    from .learning.tune import propose

    repo = Repo(args.state)
    for line in propose(repo, build_live_report(repo), _load_policy(args.policy)):
        print(f"  {line}")
    return 0


def cmd_halt(args) -> int:
    repo = Repo(args.state)
    repo.set_halt(not args.off, args.reason)
    state = "RELEASED" if args.off else "ENGAGED"
    print(f"kill switch {state}")
    if not args.off:
        print("  new entries are blocked; exits and stop management continue")
    return 0


def cmd_status(args) -> int:
    repo = Repo(args.state)
    cache = BarCache(args.bars)
    stats = cache.stats()

    print(f"halted:      {repo.is_halted()}")
    print(f"bar cache:   {stats['bars']} bars / {stats['symbols']} symbols")
    print(f"annotations: {len(repo.position_annotations())} positions")
    print(f"open orders: {len(repo.open_orders())}")
    day = repo.latest_candidate_day()
    print(f"watchlist:   {len(repo.pending_candidates(day)) if day else 0} pending ({day})")
    print(f"trades:      {len(repo.trades(100000))}")

    from .ops.notify import describe_config

    print()
    print("alerting:")
    for line in describe_config().splitlines():
        print(f"  {line}")

    print("\nrecent runs:")
    for job in ("evening", "premarket", "open", "close"):
        row = repo.last_run(job)
        if row:
            print(f"  {job:<10} {row['day']} {row['status']:<8} {row['detail'] or ''}")
        else:
            print(f"  {job:<10} never run")
    return 0


# --------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser so they are accepted on either side
    # of the subcommand -- `--bars X backtest` and `backtest --bars X` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--policy", default=DEFAULT_POLICY)
    common.add_argument("--bars", default=DEFAULT_BARS)
    common.add_argument("--state", default=DEFAULT_STATE)
    # today AT THE EXCHANGE, not on this host. From UTC+3 the local date rolls
    # over at 17:00 ET, so a run started after midnight local would ask for
    # tomorrow's session, find it is not a trading day, and skip -- losing the
    # session to a clock rather than to the market.
    common.add_argument(
        "--day",
        type=date.fromisoformat,
        default=today_exchange(),
        help="YYYY-MM-DD (defaults to today in New York)",
    )
    common.add_argument("--live", action="store_true", help="use live, not paper")
    common.add_argument("--dry-run", action="store_true", help="decide but do not send")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(
        prog="trading_bot", description=__doc__, parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str):
        return sub.add_parser(name, help=help_text, parents=[common])

    fetch = add("fetch", "refresh the local bar cache")
    fetch.add_argument("--days", type=int, default=2600)
    fetch.add_argument(
        "--source", choices=("yahoo", "alpaca"), default="yahoo",
        help="yahoo needs no credentials (default)",
    )
    fetch.add_argument("--full", action="store_true", help="ignore what is cached")
    fetch.set_defaults(func=cmd_fetch)

    bt = add("backtest", "replay decide() over history")
    bt.add_argument("--start", type=date.fromisoformat, default=None)
    bt.add_argument("--equity", type=float, default=100_000.0)
    bt.add_argument("--slippage", type=float, default=5.0)
    bt.set_defaults(func=cmd_backtest)

    for name, help_text in [
        ("evening", "18:00 -- reconcile, scan, write the watchlist"),
        ("premarket", "09:00 -- event gate on pending candidates"),
        ("open", "10:00 -- defensive exits and resting entries"),
        ("close", "15:30 -- close-confirmed entries and cleanup"),
    ]:
        job = add(name, help_text)
        if name == "premarket":
            job.add_argument("--no-llm", action="store_true")
        job.set_defaults(func=cmd_job)

    wf = add("walkforward", "does a change hold up across independent periods?")
    wf.add_argument("--folds", type=int, default=4)
    wf.add_argument("--no-holdout", action="store_true")
    wf.add_argument("--only", nargs="*", help="restrict to named variants")
    wf.set_defaults(func=cmd_walkforward)

    hold = add("holdout", "spend the holdout on named variants -- ONE SHOT")
    hold.add_argument("variants", nargs="+", help="first name is the reference")
    hold.add_argument("--folds", type=int, default=4)
    hold.add_argument("--out", default="out/holdout_result.txt")
    hold.set_defaults(func=cmd_holdout)

    diag = add("diagnose", "does the ranking function predict anything?")
    diag.add_argument("--start", type=date.fromisoformat, default=None)
    diag.set_defaults(func=cmd_diagnose)

    daily = add("daily", "the nightly routine: fetch, scan, render (dry run)")
    daily.add_argument("--days", type=int, default=2600)
    daily.add_argument(
        "--source", choices=("yahoo", "alpaca"), default="yahoo",
        help="yahoo needs no credentials (default)",
    )
    daily.add_argument("--full", action="store_true", help="ignore what is cached")
    daily.add_argument("--out", default="out/dashboard.html")
    daily.add_argument(
        "--arm", action="store_true",
        help="actually send orders (default is a dry run)",
    )
    daily.set_defaults(func=cmd_daily)

    dash = add("dashboard", "render the state database as an HTML page")
    dash.add_argument("--out", default="out/dashboard.html")
    dash.set_defaults(func=cmd_dashboard)

    srv = add("serve", "live dashboard with a kill switch (localhost)")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--token", default="", help="required to bind non-local")
    srv.set_defaults(func=cmd_serve)

    add("report", "live attribution").set_defaults(func=cmd_report)
    add("propose", "evidence-backed policy proposals").set_defaults(func=cmd_propose)

    halt = add("halt", "engage or release the kill switch")
    halt.add_argument("--off", action="store_true", help="release it")
    halt.add_argument("--reason", default="manual")
    halt.set_defaults(func=cmd_halt)

    add("status", "operational snapshot").set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
