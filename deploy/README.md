# Deployment

Two phases, deliberately different. The dry run wants to be in front of you; the
trial wants to be somewhere that never sleeps.

## Phase 1 — dry run (your own machine is fine)

Two weeks, reading the decision log daily. You are checking that the *reasoning*
looks sane, not the P&L. Missed days cost nothing here.

```bash
python -m trading_bot fetch            # must precede evening
python -m trading_bot evening --dry-run
python -m trading_bot dashboard --out out/dashboard.html
```

`--dry-run` decides, logs, and prints — and sends nothing to the broker.

### Run it after the closing bell, not before

**16:00 ET is 23:00 in Israel** (22:00 in winter). Both commands have to happen
after that, and the bot now enforces it rather than trusting you to remember:

- `evening` **refuses** to run while the session is open, because a daily bar
  exists from the opening bell with a "close" that is merely the last trade.
  Every indicator computed from it would be fiction.
- `fetch` warns, and re-reads the last cached session on its next run — so a
  fetch you ran too early is repaired by running it again later, not frozen in.

This bit on 2026-08-28: a fetch at 14:11 ET wrote 472 mid-session bars. Nothing
complained, because a provisional bar and a settled one are indistinguishable.

If staying up past 23:00 daily is not realistic, that is the argument for moving
to Phase 2 sooner — a host in New York time does this while you sleep.

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

- **Earnings calendar.** Deliberately unwired for the trial — the backtest ran
  without it too, so wiring it now would make the trial test a system that was
  never validated.
- **Data-feed latency.** The 15:30 confirmation reads live prices; on a delayed
  feed it reads 15-minute-old data.
- **Rotate the paper keys**, and generate separate live keys.
