# Deployment

Two phases, deliberately different. The dry run wants to be in front of you; the
trial wants to be somewhere that never sleeps.

## Phase 1 — dry run (your own machine is fine)

Two weeks, reading the decision log daily. You are checking that the *reasoning*
looks sane, not the P&L. Missed days cost nothing here.

```bash
python -m trading_bot daily
```

That is the whole thing: fetch, scan, render — in that order, stopping at the
first failure. It sends nothing to the broker unless you pass `--arm`.

The three steps used to be three commands, which invited running them out of
order or stopping after two. Order is not cosmetic: the scanner matches session
dates exactly, so a scan against an unrefreshed cache sees no symbols at all and
reports a clean zero.

### Run it after the closing bell, not before

**16:00 ET is 23:00 local** (22:00 in winter). The bot enforces this rather than
trusting you to remember, because all three of these failed silently once:

- **`daily` refuses to run before the bell** — and refuses *before* fetching,
  since the fetch takes minutes and its output would be thrown away. A daily bar
  exists from the opening bell with a "close" that is merely the last trade, and
  every indicator computed from it would be fiction.
- **A fetch run too early repairs itself.** The refresh re-reads the last cached
  session, so running again after the close overwrites the provisional bars
  rather than freezing them in.
- **The trading day is New York's, not this machine's.** Running at 00:30 local
  still means the previous session. It used to mean *tomorrow*, which is not a
  trading day, so the run would skip and the night was lost.

This bit on 2026-08-28: a fetch at 14:11 ET wrote 472 mid-session bars. Nothing
complained, because a provisional bar and a settled one are indistinguishable.

If staying up past 23:00 daily is not realistic, that is the argument for moving
to Phase 2 sooner — a host on New York time does this while you sleep.

## Phase 2 — the six-month trial (a host that stays on)

```bash
BOT_DIR=/opt/trading-bot bash deploy/setup.sh   # fills in .env, then re-run
sed 's#/opt/trading-bot#/opt/trading-bot#' deploy/crontab.template | crontab -
```

`setup.sh` is idempotent and verifies each step rather than assuming it worked.
It refuses to continue if the test suite fails, because every number this system
produces depends on it.

### Why not Windows Task Scheduler

Two reasons, both quiet rather than dramatic:

**A laptop sleeps.** A closed lid runs nothing, and "wake to run" does not work
from shutdown or reliably on battery.

**DST drift.** The schedule is New York time. Task Scheduler fires at *local*
times, so it shifts by an hour twice a year — and since the US and EU change on
different dates, there are two weeks annually when it is simply wrong. `CRON_TZ`
handles this automatically.

Neither is dangerous. Both quietly shrink your sample, and the stopping rule
needs 100+ trades to mean anything.

## Timing, and the one ordering that matters

| ET | Job | If it is missed |
|---|---|---|
| 09:00 | `premarket` | No event veto |
| 10:00 | `open` | Pullback entries not placed |
| 15:30 | `close` | Breakout entries not placed |
| 18:00 | `fetch` | **Cache goes stale → `evening` errors** |
| 18:15 | `evening` | No watchlist tomorrow |

**`fetch` must run before `evening`.** The scanner matches session dates exactly,
so an unrefreshed cache makes every symbol invisible and the scan returns a clean
zero. The evening job now errors rather than reporting a quiet market — but that
still costs the session.

Missing a run is never *dangerous*: once a bracket is submitted, the stop and
target live at Alpaca. If the host is off for a week, open positions are still
protected. You lose opportunity, not safety. And expectancy — the metric the
stopping rule uses — survives missed sessions; only trade count and total return
are understated.

## Alerting

Two different problems needing two different answers:

| Env var | Catches |
|---|---|
| `TRADING_BOT_WEBHOOK` | Failures, tripped breakers, book changes — while the process is alive to shout |
| `TRADING_BOT_HEARTBEAT` | **Nothing running at all.** No in-process notifier can catch this; dead code sends no alerts. An external monitor expects a ping and alarms when it stops. |

The second is the one not to skip. `python -m trading_bot status` prints which
are wired, because silence is not proof of health.

## Publishing it publicly

```bash
bash deploy/publish.sh
```

Renders the dashboard to `docs/index.html`, commits it and pushes. GitHub Pages
serves `docs/` on the default branch, so publishing is a commit — no hosting to
pay for and no port to expose.

**Enable it once**, in the repository: Settings → Pages → Source *Deploy from a
branch* → branch `main`, folder `/docs`. The URL is then
`https://<user>.github.io/<repo>/`.

What gets published is the **static** dashboard. It has no halt switch and no
route that could reach an order — that control lives only in `serve`, which
stays on localhost. Two tests enforce the distinction.

On a host this needs push credentials. A **deploy key scoped to this one
repository** is the right shape; a personal access token with account-wide
access is not.

The script skips the commit when the numbers have not moved, so an idle week
does not fill the history with empty commits.

## Dashboard

```bash
python -m trading_bot serve --token <pick-something>
ssh -L 8080:localhost:8080 user@host      # from your laptop
```

Binds `127.0.0.1` by default and **refuses** to bind a public interface without a
token — an unauthenticated kill switch reachable from the network is a worse
failure than the server not starting. Halt and resume are the only mutations;
there is no path from this page to an order.

## Before real money

Paper trading is safe to start now. These are the items to close before any real
capital, from PLAN.md §6:

- **Earnings calendar.** Now wired: `python -m trading_bot earnings` writes
  `config/earnings.json`. Re-run it weekly, since dates move. Note that the
  backtest ran *without* it, so the live system is now marginally more
  conservative than the one that was validated — it declines a few entries the
  backtest would have taken.
- **Data-feed latency.** The 15:30 confirmation reads live prices; on a delayed
  feed it reads 15-minute-old data.
- **Rotate the paper keys**, and generate separate live keys.
