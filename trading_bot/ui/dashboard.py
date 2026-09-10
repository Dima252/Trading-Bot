"""A single self-contained HTML page showing what the agent is doing.

Deliberately a STATIC FILE, not a server. It renders from the state database and
writes one file with no external assets, so:

  * nothing new runs in the trading path, and nothing here can break a job
  * cron can regenerate it after the evening job
  * it works over `scp`, `file://`, or any static host, with no port to secure

The centrepiece is the decision log, not the P&L. Every action the agent took
carries the reason it took it, and every action the constitution vetoed carries
the rule that fired -- that is the part you cannot get from a broker statement,
and it is already in the database.

The status bar leads with the last run time per job, red when stale: a cron job
that has silently stopped firing is the most likely failure mode of the system,
and it should be the first thing visible.
"""

from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..core.policy import Policy
from ..data.cache import BarCache
from ..db.repo import Repo
from ..market_hours import today_exchange

JOBS = ("evening", "premarket", "open", "close")
STALE_DAYS = 4


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}%}"


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.0f}"


def _r(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}R"


def _cls(value: float | None) -> str:
    if value is None:
        return ""
    return "pos" if value > 0 else ("neg" if value < 0 else "")


# --------------------------------------------------------------------- #


def _sparkline(
    equity: list[tuple[date, float]],
    benchmark: list[tuple[date, float]] | None = None,
    width: int = 900,
    height: int = 260,
) -> str:
    """Inline SVG. Both series normalised to 100 at the start, so the comparison
    is like for like regardless of account size."""
    if len(equity) < 2:
        return '<p class="empty">No equity history yet.</p>'

    def normalise(series):
        base = series[0][1] or 1.0
        return [(d, v / base * 100.0) for d, v in series]

    eq = normalise(equity)
    bm = normalise(benchmark) if benchmark and len(benchmark) > 1 else []

    values = [v for _, v in eq] + [v for _, v in bm]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = 24

    def points(series) -> str:
        n = len(series) - 1 or 1
        return " ".join(
            f"{pad + i / n * (width - 2 * pad):.1f},"
            f"{height - pad - (v - lo) / span * (height - 2 * pad):.1f}"
            for i, (_, v) in enumerate(series)
        )

    bm_line = (
        f'<polyline class="bm" points="{points(bm)}" />' if bm else ""
    )
    final_eq = eq[-1][1] - 100
    final_bm = (bm[-1][1] - 100) if bm else None

    legend = (
        f'<tspan class="k-eq">agent {final_eq:+.1f}%</tspan>'
        + (
            f'  <tspan class="k-bm">benchmark {final_bm:+.1f}%</tspan>'
            if final_bm is not None
            else ""
        )
    )

    # Area under the agent's line, closed along the baseline. Reads as a level
    # rather than a squiggle, which is what a balance is.
    eq_pts = points(eq)
    first_x = pad
    last_x = width - pad
    floor = height - pad
    area = f"{first_x},{floor} {eq_pts} {last_x},{floor}"

    # Horizontal guides at quarters of the range -- enough to read a level from,
    # faint enough not to compete with the series.
    grid = "".join(
        f'<line class="grid" x1="{pad}" y1="{y:.1f}" x2="{width - pad}" y2="{y:.1f}" />'
        for y in (
            height - pad - f * (height - 2 * pad) for f in (0.25, 0.5, 0.75, 1.0)
        )
    )

    ex, ey = eq_pts.rsplit(" ", 1)[-1].split(",") if " " in eq_pts else (last_x, floor)

    return f"""<div class="chartwrap">
<svg viewBox="0 0 {width} {height}" class="chart"
     preserveAspectRatio="none" role="img" aria-label="equity curve">
  {grid}
  <line class="axis" x1="{pad}" y1="{floor}" x2="{width - pad}" y2="{floor}" />
  <polygon class="eqfill" points="{area}" />
  {bm_line}
  <polyline class="eq" points="{eq_pts}" />
  <circle class="dot" cx="{ex}" cy="{ey}" r="4" />
  <text x="{pad}" y="18" class="legend">{legend}</text>
</svg>
</div>
<p class="cap">{esc(eq[0][0])} &rarr; {esc(eq[-1][0])} · both rebased to 100</p>"""


# --------------------------------------------------------------------- #


def _freshness(as_of: date) -> str:
    """Say so when the page is not showing current activity.

    A visitor arriving from a link reads whatever is on the page as what the bot
    is doing now. Before the first scheduled run this page renders a historical
    replay, and "as of 2025-06-10" in small type under the title is not enough
    to correct that impression.
    """
    age = (today_exchange() - as_of).days
    if age <= 7:
        return ""
    return (
        f' <strong>Showing a {as_of} replay, not current activity</strong> '
        f"— the scheduled run has not published since then ({age} days)."
    )


def _headline(repo: Repo, today: date) -> str:
    """Account value, with the change since the run began.

    This is the number anyone opens the page for. It was previously one tile
    among six, at the same size as `heat`.
    """
    history = repo.equity_history()
    if not history:
        return "—"

    now = history[-1]["equity"]
    start = history[0]["equity"] or 0.0
    if start <= 0:
        return _money(now)

    change = (now - start) / start
    cls = _cls(change)
    return (
        f"{_money(now)}"
        f'<span class="delta {cls}">{change:+.2%}</span>'
    )


def _status_bar(repo: Repo, policy: Policy, today: date) -> str:
    history = repo.equity_history()
    latest = history[-1] if history else None
    equity = latest["equity"] if latest else None
    cash = latest["cash"] if latest else None
    heat = latest["heat_pct"] if latest else None
    # The last row is not necessarily the last MEASURED regime: a run that
    # errored writes an equity snapshot with a null one, and the tile then reads
    # "—" on exactly the days when knowing the regime matters most. On a chop
    # day this field is the whole explanation for why nothing was traded.
    regime = repo.last_regime(today)

    week = None
    if equity is not None:
        base = repo.equity_asof(today - timedelta(days=7))
        if base:
            week = (equity - base) / base

    halted = repo.is_halted()
    halt_reason = repo.get_flag("HALT_REASON") or ""

    tiles = [
        ("equity", _money(equity), ""),
        ("cash", _money(cash), ""),
        (
            "heat",
            f"{heat:.2%} / {policy.max_portfolio_heat:.1%}" if heat is not None else "—",
            "warn" if heat and heat > policy.max_portfolio_heat else "",
        ),
        ("7-day", _pct(week), _cls(week)),
        ("regime", esc(regime or "—"), ""),
        (
            "kill switch",
            f"ENGAGED — {esc(halt_reason)}" if halted else "clear",
            "bad" if halted else "ok",
        ),
    ]

    cells = "".join(
        f'<div class="tile {c}"><span class="k">{k}</span>'
        f'<span class="v">{v}</span></div>'
        for k, v, c in tiles
    )
    return f'<div class="tiles">{cells}</div>{_job_health(repo, today)}'


def _job_health(repo: Repo, today: date) -> str:
    """A job that quietly stopped firing is the likeliest failure of the whole
    system, so it goes at the top rather than buried in a log."""
    rows = []
    for job in JOBS:
        run = repo.last_run(job)
        if run is None:
            rows.append(
                f'<div class="job bad"><span class="k">{job}</span>'
                f'<span class="v">never run</span></div>'
            )
            continue
        day = date.fromisoformat(run["day"])
        age = (today - day).days
        stale = age > STALE_DAYS
        cls = "bad" if run["status"] == "error" else ("warn" if stale else "ok")
        note = f"{run['status']} · {day}"
        if stale:
            note += f" · {age}d ago"
        rows.append(
            f'<div class="job {cls}"><span class="k">{job}</span>'
            f'<span class="v">{esc(note)}</span></div>'
        )
    return f'<div class="jobs">{"".join(rows)}</div>'


def _positions(repo: Repo, today: date) -> str:
    rows = repo.position_annotations()
    if not rows:
        return '<p class="empty">No open positions.</p>'

    body = ""
    for ticker, r in sorted(rows.items()):
        entry = r["entry_price"] or 0.0
        risk = max(entry - r["initial_stop"], 1e-9)
        held = (today - date.fromisoformat(r["opened_at"])).days
        body += f"""<tr>
  <td class="sym">{esc(ticker)}</td>
  <td>{esc(r['setup_type'])}</td>
  <td class="num">{r['entry_qty'] or 0}</td>
  <td class="num">{entry:.2f}</td>
  <td class="num">{r['initial_stop']:.2f}</td>
  <td class="num">{r['target']:.2f}</td>
  <td class="num">{(r['target'] - entry) / risk:.1f}R</td>
  <td class="num">{held}d</td>
  <td>{esc(r['sector'])}</td>
  <td class="why">{esc(r['thesis'])}</td>
</tr>"""
    return f"""<table>
<thead><tr><th>ticker</th><th>setup</th><th>qty</th><th>entry</th><th>stop</th>
<th>target</th><th>R:R</th><th>held</th><th>sector</th><th>thesis</th></tr></thead>
<tbody>{body}</tbody></table>"""


def _watchlist(repo: Repo) -> str:
    day = repo.latest_candidate_day()
    if day is None:
        return '<p class="empty">No watchlist written yet.</p>'

    rows = repo.candidates_on(day)
    body = ""
    for r in rows:
        status = r["status"]
        cls = {"Pending": "ok", "Cancelled": "muted", "Submitted": "warn"}.get(
            status, ""
        )
        body += f"""<tr class="{cls}">
  <td class="sym">{esc(r['ticker'])}</td>
  <td>{esc(r['setup_type'])}</td>
  <td>{esc(r['entry_type'])}</td>
  <td class="num">{r['score']:.1f}</td>
  <td class="num">{r['entry']:.2f}</td>
  <td class="num">{r['stop']:.2f}</td>
  <td class="num">{r['target']:.2f}</td>
  <td>{esc(status)}</td>
  <td class="why">{esc(r['status_reason'] or '')}</td>
</tr>"""
    return f"""<p class="cap">Written {esc(day)} — {len(rows)} candidates.</p>
<table>
<thead><tr><th>ticker</th><th>setup</th><th>entry type</th><th>score</th>
<th>entry</th><th>stop</th><th>target</th><th>status</th><th>why</th></tr></thead>
<tbody>{body}</tbody></table>"""


def _decisions(repo: Repo, limit: int = 60) -> str:
    rows = repo.recent_decisions(limit)
    if not rows:
        return '<p class="empty">No decisions recorded yet.</p>'

    body = ""
    for r in rows:
        approved = bool(r["approved"])
        cls = "ok" if approved else "muted"
        verdict = (
            "approved"
            if approved
            else f"VETOED · {esc(r['rejected_rule'])}"
        )
        why = r["reason"] if approved else (r["rejected_detail"] or r["reason"])
        body += f"""<tr class="{cls}">
  <td class="num dim">{esc(r['day'])}</td>
  <td>{esc(r['job'] or '')}</td>
  <td class="kind">{esc(r['kind'])}</td>
  <td class="sym">{esc(r['ticker'])}</td>
  <td class="num">{r['qty'] or ''}</td>
  <td class="num">{f"{r['score']:.1f}" if r['score'] else ''}</td>
  <td>{verdict}</td>
  <td class="why">{esc(why)}</td>
</tr>"""
    return f"""<table>
<thead><tr><th>day</th><th>job</th><th>action</th><th>ticker</th><th>qty</th>
<th>score</th><th>verdict</th><th>reason</th></tr></thead>
<tbody>{body}</tbody></table>"""


def _trades(repo: Repo, limit: int = 40) -> str:
    rows = repo.trades(limit)
    if not rows:
        return '<p class="empty">No closed trades yet.</p>'

    body = ""
    for r in rows:
        body += f"""<tr>
  <td class="sym">{esc(r['ticker'])}</td>
  <td>{esc(r['setup_type'])}</td>
  <td>{esc(r['regime_at_entry'] or '')}</td>
  <td class="num dim">{esc(r['entry_day'])}</td>
  <td class="num dim">{esc(r['exit_day'])}</td>
  <td class="num">{r['days_held'] or 0}d</td>
  <td class="num {_cls(r['realized_r'])}">{_r(r['realized_r'])}</td>
  <td class="num {_cls(r['pnl'])}">{_money(r['pnl'])}</td>
  <td class="num dim">{_r(r['mfe_r'])}</td>
  <td class="num dim">{_r(r['mae_r'])}</td>
  <td class="why">{esc(r['exit_reason'])}</td>
</tr>"""
    return f"""<table>
<thead><tr><th>ticker</th><th>setup</th><th>regime</th><th>in</th><th>out</th>
<th>held</th><th>R</th><th>P&amp;L</th><th>MFE</th><th>MAE</th>
<th>exit</th></tr></thead>
<tbody>{body}</tbody></table>"""


def _attribution(repo: Repo) -> str:
    from ..learning.attribution import build_live_report

    report = build_live_report(repo)
    if not report.overall.trades:
        return '<p class="empty">Not enough trades to attribute yet.</p>'

    def table(title: str, buckets) -> str:
        if not buckets:
            return ""
        body = "".join(
            f"<tr><td>{esc(name)}</td><td class='num'>{st.trades}</td>"
            f"<td class='num'>{st.win_rate:.0%}</td>"
            f"<td class='num {_cls(st.expectancy_r)}'>{st.expectancy_r:+.3f}</td>"
            f"<td class='num'>{st.total_r:+.1f}</td>"
            f"<td class='dim'>{'' if st.is_meaningful else 'thin'}</td></tr>"
            for name, st in sorted(buckets.items(), key=lambda kv: -kv[1].trades)
        )
        return f"""<h3>{title}</h3><table class="tight">
<thead><tr><th>bucket</th><th>n</th><th>win%</th><th>exp R</th>
<th>total R</th><th></th></tr></thead><tbody>{body}</tbody></table>"""

    o, d = report.overall, report.diagnostics
    head = f"""<div class="tiles">
  <div class="tile"><span class="k">trades</span><span class="v">{o.trades}</span></div>
  <div class="tile"><span class="k">win rate</span>
    <span class="v">{o.win_rate:.0%}</span></div>
  <div class="tile"><span class="k">expectancy</span>
    <span class="v {_cls(o.expectancy_r)}">{o.expectancy_r:+.3f}R</span></div>
  <div class="tile"><span class="k">profit factor</span>
    <span class="v">{o.profit_factor:.2f}</span></div>
</div>"""

    warnings = ""
    if d.stops_too_tight:
        warnings += (
            "<p class='warn-line'>Stops look too tight: "
            f"{d.pct_losers_that_reached_1r:.0%} of losers first reached +1R.</p>"
        )
    if d.targets_too_tight:
        warnings += (
            "<p class='warn-line'>Targets look too tight: names ran a further "
            f"{d.avg_post_exit_r_after_target:.2f}R after exit.</p>"
        )

    shadow = ""
    if report.shadow:
        body = "".join(
            f"<tr><td>{esc(v.reason)}</td><td class='num'>{v.count}</td>"
            f"<td class='num {_cls(v.expectancy_r)}'>{v.expectancy_r:+.3f}</td>"
            f"<td class='why'>{esc(v.verdict_vs(o))}</td></tr>"
            for v in report.shadow
        )
        shadow = f"""<h3>Shadow book — what was declined</h3><table class="tight">
<thead><tr><th>reason</th><th>n</th><th>exp R</th><th>verdict</th></tr></thead>
<tbody>{body}</tbody></table>"""

    return (
        head
        + warnings
        + table("By setup", report.by_setup)
        + table("By regime at entry", report.by_regime)
        + shadow
    )


# --------------------------------------------------------------------- #

CSS = """
/* Self-contained by design: no font link, no external asset. The stacks below
   resolve to the best face already on the machine, because the page has to
   survive being opened over file:// or scp with no network. */
:root{
  --bg:#f4f5f7; --panel:#fff; --fg:#14171c; --dim:#606a7a; --faint:#8b95a5;
  --line:#e2e6ec; --hair:#eef1f5;
  --pos:#0d7a4f; --neg:#b3261e; --warn:#8a6410; --accent:#2f5fd0; --bm:#a3abb8;
  --pos-bg:#e7f3ec; --neg-bg:#fbeae8; --warn-bg:#fbf2e0;
  --shadow:0 1px 2px rgba(20,23,28,.05),0 10px 28px -20px rgba(20,23,28,.35);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1116; --panel:#161a21; --fg:#e4e8ee; --dim:#97a1b0; --faint:#6b7583;
  --line:#252b34; --hair:#1c222a;
  --pos:#4ec38a; --neg:#ef8279; --warn:#d9ab4c; --accent:#7d9ef5; --bm:#5c6675;
  --pos-bg:#14251d; --neg-bg:#271a19; --warn-bg:#26200f;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 10px 28px -20px rgba(0,0,0,.9);
}}
*{box-sizing:border-box}
body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI Variable Text","Segoe UI",
  system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:36px 28px 80px}

/* masthead ------------------------------------------------------------- */
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;
  gap:20px;padding-bottom:20px;border-bottom:2px solid var(--fg);margin-bottom:26px}
h1{font-size:15px;margin:0 0 10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);font-weight:600}
.headline{font-size:clamp(34px,6vw,46px);font-weight:650;line-height:1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums;margin:0}
.headline .delta{font-size:16px;font-weight:600;margin-left:12px;letter-spacing:0}
.about{max-width:74ch;color:var(--dim);font-size:13.5px;line-height:1.6;
  margin:0 0 24px;padding:14px 18px;background:var(--panel);
  border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:0 10px 10px 0}
.about a{color:var(--accent)}
.about strong{color:var(--fg)}
.sub{color:var(--faint);margin:8px 0 0;font-size:12.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.sub code{background:none;padding:0;color:var(--dim)}

/* metric strip --------------------------------------------------------- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;margin-bottom:14px;box-shadow:var(--shadow)}
.tile{background:var(--panel);padding:13px 16px}
.tile .k{display:block;font-size:10.5px;color:var(--faint);text-transform:uppercase;
  letter-spacing:.09em;font-weight:600}
.tile .v{display:block;font-size:19px;font-weight:620;margin-top:3px;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.tile.ok .v{color:var(--pos)}.tile.bad .v{color:var(--neg)}
.tile.warn .v{color:var(--warn)}

/* job pills ------------------------------------------------------------ */
.jobs{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 6px}
.job{border:1px solid var(--line);border-radius:999px;padding:5px 13px 5px 10px;
  background:var(--panel);font-size:12px;display:inline-flex;align-items:center;gap:8px}
.job::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--faint);
  flex:none}
.job .k{color:var(--dim);font-weight:600}
.job .v{color:var(--faint);font-variant-numeric:tabular-nums}
.job.ok::before{background:var(--pos)}
.job.warn::before{background:var(--warn)}
.job.bad::before{background:var(--neg)}

/* sections ------------------------------------------------------------- */
h2{font-size:12px;margin:38px 0 12px;padding-bottom:8px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--dim);font-weight:700;
  border-bottom:1px solid var(--line)}
h3{font-size:12px;margin:20px 0 7px;color:var(--faint);font-weight:600;
  letter-spacing:.05em;text-transform:uppercase}

/* panels + tables ------------------------------------------------------ */
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse;font-size:12.5px;
  font-variant-numeric:tabular-nums}
th{text-align:left;font-weight:600;color:var(--faint);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.07em;padding:11px 14px;
  border-bottom:1px solid var(--line);white-space:nowrap;background:var(--hair)}
td{padding:10px 14px;border-bottom:1px solid var(--hair);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--hair)}
.num{text-align:right;white-space:nowrap}
.sym{font-weight:650;letter-spacing:-.01em}
.kind{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
  font-weight:600;padding:2px 7px;border-radius:4px;background:var(--hair);
  color:var(--dim);white-space:nowrap}
.why{color:var(--dim);max-width:46ch;line-height:1.45}
.dim{color:var(--faint)}
.pos{color:var(--pos);font-weight:600}.neg{color:var(--neg);font-weight:600}
tr.muted td{opacity:.5}
.empty{color:var(--faint);font-style:italic;padding:18px;background:var(--panel);
  border:1px dashed var(--line);border-radius:10px}
.cap{color:var(--faint);font-size:11.5px;margin:8px 2px 0;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.warn-line{color:var(--warn);background:var(--warn-bg);border-radius:8px;
  padding:10px 14px;margin:8px 0;font-size:12.5px}

/* chart ---------------------------------------------------------------- */
.chartwrap{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:8px 4px 4px;box-shadow:var(--shadow)}
.chart{width:100%;height:260px;display:block}
.chart .eq{fill:none;stroke:var(--accent);stroke-width:2.25;
  stroke-linejoin:round;stroke-linecap:round}
.chart .eqfill{fill:var(--accent);opacity:.09;stroke:none}
.chart .bm{fill:none;stroke:var(--bm);stroke-width:1.5;stroke-dasharray:5 4}
.chart .axis{stroke:var(--line)}
.chart .grid{stroke:var(--hair);stroke-width:1}
.chart .legend{font-size:11.5px;fill:var(--dim);font-weight:600}
.chart .k-eq{fill:var(--accent)}.chart .k-bm{fill:var(--bm)}
.chart .dot{fill:var(--accent)}

footer{margin-top:48px;padding-top:16px;border-top:2px solid var(--fg);
  color:var(--faint);font-size:11.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}

@media print{body{background:#fff}.scroll,.chartwrap,.tiles{box-shadow:none}}
"""


def render(
    repo: Repo,
    cache: BarCache | None = None,
    policy: Policy | None = None,
    as_of: date | None = None,
    benchmark: str = "SPY",
) -> str:
    policy = policy or Policy()
    today = as_of or today_exchange()

    history = repo.equity_history()
    equity = [(date.fromisoformat(r["day"]), r["equity"]) for r in history]

    bench: list[tuple[date, float]] = []
    if cache is not None and equity:
        series = cache.load(benchmark, equity[0][0], equity[-1][0])
        days = {d for d, _ in equity}
        bench = [(b.day, b.close) for b in series.bars if b.day in days]

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Bot — {esc(today)}</title>
<style>{CSS}</style></head><body>
<div class="wrap">

<div class="top">
  <div>
    <h1>Trading Bot</h1>
    <p class="headline">{_headline(repo, today)}</p>
  </div>
  <p class="sub">policy {esc(policy.version)} · as of {esc(today)}<br>
  generated {esc(generated)}</p>
</div>

<p class="about">An autonomous swing-trading agent for US equities: it scans
~500 names nightly, decides what the portfolio should look like tomorrow, and
places bracketed orders through a broker API on a schedule — no daemon, no
manual input. <strong>Trading a paper account. No real money is involved.</strong>
Every figure below comes from its own state database.{_freshness(today)}
<a href="https://github.com/Dima252/Trading-Bot">Source and research log</a>.</p>

{_status_bar(repo, policy, today)}

<h2>Equity vs benchmark</h2>
{_sparkline(equity, bench)}

<h2>Open positions</h2>
<div class="scroll">{_positions(repo, today)}</div>

<h2>Watchlist</h2>
<div class="scroll">{_watchlist(repo)}</div>

<h2>Decision log</h2>
<p class="cap">Every action, and every action the constitution vetoed with the
rule that fired.</p>
<div class="scroll">{_decisions(repo)}</div>

<h2>Closed trades</h2>
<div class="scroll">{_trades(repo)}</div>

<h2>Attribution</h2>
<div class="scroll">{_attribution(repo)}</div>

<footer>Read-only view of the state database. Nothing on this page can place,
modify or cancel an order.</footer>
</div>
</body></html>"""


def write(
    repo: Repo,
    path: str | Path,
    cache: BarCache | None = None,
    policy: Policy | None = None,
    as_of: date | None = None,
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(repo, cache, policy, as_of), encoding="utf-8")
    return out
