# Where we stopped, and what happens next

Session ended 2026-08-28. Everything below reflects the state of the repo at that
point. Architecture reference is [README.md](README.md); this file is the
working log and forward plan.

---

## 1. State of the build

| | |
|---|---|
| Implementation | ~7,400 lines across `trading_bot/` |
| Tests | **212 passing**, ~12s, no network, no credentials |
| Market data | 162,890 real daily bars, 91 symbols, 2019-07 → 2026-08, in `data/bars.db` |
| Phases complete | 0, 2, 3, 4, 5, 6 (see README §14) |
| Git | Everything **staged but NOT committed** — first commit still to be made |

### What runs today

```bash
python -m pytest -q                    # 212 tests
python scripts/demo.py                 # decision core on a hand-built book
python -m trading_bot fetch            # real bars, no API key needed (Yahoo)
python -m trading_bot backtest         # full report vs SPY
python -m trading_bot diagnose         # is the ranking predictive?
python -m trading_bot walkforward      # does a change hold across periods?
```

The four cron jobs (`evening`, `premarket`, `open`, `close`) are written, tested
end to end against an in-memory broker, and have **never been run against a real
account** because no credentials exist yet.

---

## 2. What the research found

Three runs, in order. Each is recorded in full in README §16.

**1. The strategy as built had no edge.** −0.009R over 1,107 trades; +3.75% total
return against SPY's +148.29% over the same six years, with a *deeper* drawdown.

**2. The ranking function was doing nothing.** Over 25,018 candidates the
composite score had rho = 0.001 — completely flat across every decile. Cause:
`setup_quality` carries 60% of the weight and measured anti-predictive at
z = −7.4, because two invented heuristics both encoded *buy the deepest dip*.
RSI (z = +16.4) is the strongest signal in the data and the scorer was using it
upside down.

**3. Both corrections held across four independent periods.** The combined
variant turned −0.042R into **+0.111R, positive in all four folds**.

**But it still loses to SPY in three folds of four.** It beats the index only in
the bear period. What the corrections produced is a lower-drawdown, lower-return
long-only profile — reduced beta, not alpha.

### Bugs the real data exposed

Recorded here because they are the kind that silently invalidate everything
downstream, and the regression tests for them must never be deleted.

| Bug | Effect | Guard |
|---|---|---|
| Entry filling below its own stop | Backtest "sold at the stop" while the market was 15 points lower — **manufactured profit from a gap down**. Inflated 6-year return from 3.75% to 10.04%. | `test_a_limit_that_gaps_in_below_its_own_stop_is_never_taken` |
| R denominated by fill price | A fill below the stop divided by ~0, producing nine-figure R multiples | `test_no_trade_reports_an_absurd_r` |
| Fixed significance threshold in the diagnostic | Rank-correlation noise scales as 1/√n; a constant cutoff labelled random noise "ANTI-PREDICTIVE" and produced two false findings | `test_significance_scales_with_sample_size` |
| Premarket read candidates under the wrong date | Every earnings veto and LLM veto silently updated zero rows | `test_premarket_cancels_candidates_with_earnings_in_the_window` |

---

## 2b. Session 2 progress (A1 and B1 complete)

### A1 — the regime gate held, and so did everything else

All three corrections are now pre-registered variants and all three held in
**every** fold:

| Variant | Folds won | Mean exp R | vs baseline |
|---|---|---|---|
| baseline | — | −0.042 | — |
| `shallow` | 3/4 | +0.057 | +0.099 |
| `long_hold` | 4/4 | +0.015 | +0.057 |
| `both` | 4/4 | +0.111 | +0.153 |
| `trend_only` | 4/4 | +0.074 | +0.116 |
| **`all_three`** | **4/4** | **+0.174** | **+0.216** |

`all_three` (shallow + long_hold + trend_only) is the strongest: −0.042R becomes
**+0.174R, positive in all four periods**, with drawdowns of 5–9% against the
index's 24.5%. Trade count falls from 891 to 302, so it is also far more
selective.

### But measured against the §5 bar it clears only 2 folds of 4

`return / max drawdown`, per fold:

| Fold | `all_three` | SPY | Verdict |
|---|---|---|---|
| F1 2020-08→2021-10 | 3.27 | 4.06 | fail |
| F2 2021-10→2023-01 | −0.33 | −0.50 | **pass** |
| F3 2023-01→2024-03 | 0.27 | 3.32 | fail |
| F4 2024-03→2025-06 | 1.11 | 0.89 | **pass** |

The pattern is consistent and structural, not noise: **it wins risk-adjusted in
weak and choppy markets and loses in strong ones.** F3 is the clearest case —
+2.08% while SPY did +33%.

What has been built is a **defensive profile**, not an index-beater. More
parameter work will not change that; it is what a long-only system that sits out
chop and caps position size does. The walk-forward report now prints this bar
per fold automatically.

### B1 — the dashboard is built

`python -m trading_bot dashboard --out out/dashboard.html` renders one
self-contained HTML file from the state database: status bar with per-job
staleness, equity vs benchmark, open positions, watchlist, **decision log with
every veto and the rule that fired**, closed trades with MFE/MAE, attribution,
and shadow-book verdicts. No server, no external assets, no path to an order.

`scripts/rehearse.py` replays the four jobs day by day over real bars against
the paper broker (PLAN C4, compressed) and leaves a populated database for it to
render. A 70-session run over 2025 exercised the whole live pipeline for the
first time: real fills, stops, targets, rotations, 49 closed trades, 272 logged
decisions.

### Bug found while wiring the rehearsal

`close_job` submitted its breakout entries and then swept all unfilled buy
orders — **cancelling the orders it had just placed**. A marketable limit fills
in seconds, but "unfilled" is true for the instant in between, so the
close-confirmation path could never actually open a position. Fixed; guarded by
`test_close_does_not_cancel_the_entry_it_just_submitted`.

---

## 3. The open decision

**Nothing has touched the holdout period (2025-06-11 → 2026-08-27).** It can be
spent exactly once. The rehearsal script refuses to run past 2025-06-10 for this
reason.

The regime gate has now been tested (§2b) and `all_three` is the candidate. The
question is no longer *which* variant — it is whether spending the holdout is
worth it yet.

**Recommendation: do not spend it on `all_three` as it stands.** In-fold it
clears the §5 bar in 2 of 4 periods, so the expected holdout result is close to
a coin flip, and that is a poor use of the only clean test available.

The prior question is strategic, not technical: **is a defensive profile what
you want?** The evidence is now clear and consistent that this is what the
system produces. If the answer is yes, spend the holdout to confirm it. If the
answer is "it must beat buy-and-hold", this approach will not, and the honest
move is a different game — a less efficient universe, a longer holding period,
or a different edge type — rather than more tuning of this one.

---

## 4. Roadmap

Three tracks. **A** decides whether this is worth trading at all; **B** makes it
observable; **C** goes live. A must reach a verdict before C starts. B can run
in parallel and is useful either way.

### Track A — Does this have an edge? (research)

| Step | What | Size | Owner |
|---|---|---|---|
| A1 | Add regime gate as a 5th variant; re-run walk-forward | small | me |
| A2 | Monte Carlo bootstrap on the trade sequence — is the equity curve luck? Gives a realistic worst-case drawdown that a single path understates | medium | me |
| A3 | Parameter sensitivity sweep — plateau (real) vs knife-edge (fitted) | medium | me |
| A4 | **Spend the holdout** on the single best variant. One shot. | small | you approve, me run |
| A5 | Verdict against the pre-set bar (§5) | — | you |

**If A fails the bar:** the honest options are to change the game, not the
parameters — a less efficient universe (small/mid caps), a longer holding period
(current average is 8 days, which is short-term noise more than swing), or a
different edge type. That is a new Track A, not a patch.

### Track B — The UI layer

The most valuable thing to see is **not** a P&L number. It is *why the bot did
what it did, and what it declined* — that data already exists in `decisions`,
`shadow_book`, and `runs` and is currently only readable via SQL.

**B1 — Static HTML dashboard** (do this first)

`python -m trading_bot dashboard --out dashboard.html` renders one
self-contained file from the state database. No server, no runtime dependency in
the trading path, nothing that can break the bot. Cron regenerates it after the
evening job.

Sections, in priority order:

1. **Status bar** — equity, day/week P&L, heat vs cap, halt state, and **last run
   time per job, red when stale**. A job that silently stopped firing is the
   most likely failure mode of the whole system; it belongs at the top.
2. **Equity curve vs SPY** — inline SVG, no chart library, no external assets.
3. **Open positions** — qty, entry, current, unrealized R, days held, stop,
   target, sector, and the original thesis text.
4. **Tonight's watchlist** — candidates with score, setup, levels, status, and
   why anything was cancelled.
5. **Decision log** — the last N actions with their `reason` string, plus vetoed
   actions with the constraint rule that fired. This is the centrepiece.
6. **Recent trades** — realized R, MFE/MAE, exit reason, days held.
7. **Attribution** — expectancy by setup and regime, thin buckets marked.
8. **Shadow book verdicts** — is each filter earning its keep.

**B2 — Live server with controls**

`python -m trading_bot serve --port 8080`. Same rendering, plus:
- auto-refresh
- **halt / resume buttons** wired to the existing kill switch
- force-run a job with `--dry-run` and see the output

Needs auth before it is exposed beyond localhost — bind to 127.0.0.1 and reach
it over an SSH tunnel rather than opening a port.

**B3 — Notifications.** A message when a job fails, when a breaker trips, or when
the agent opens or closes a position. Email or a webhook; small.

### Track C — Going live

Only after A5 returns a positive verdict.

| Step | What | Owner |
|---|---|---|
| C1 | Alpaca paper keys in the environment | you |
| C2 | `fetch --source alpaca` and re-run the backtest on the broker's own feed — confirm the result survives a different data source | me |
| C3 | Wire a real earnings calendar (§6) | you choose provider, me wire |
| C4 | Two weeks of all four jobs with `--dry-run`, reading the decision log daily | both |
| C5 | Drop `--dry-run`. **Change nothing for a month.** | you |
| C6 | `python -m trading_bot report` — first real attribution | me |

---

## 5. Set the bar before you look at the results

Written down now, while nothing is riding on it:

> A variant is worth paper trading if it beats SPY on **return-per-drawdown**,
> **on the holdout**, without any parameter that has to be exactly right.

Current best (`both`, in-fold): positive expectancy, drawdowns of 8–12% against
the index's 24.5%, but absolute return below SPY in 3 of 4 periods.

**The strategic question only you can answer:** is a lower-drawdown,
lower-return long-only profile something you would actually run? If the goal is
to beat buy-and-hold, the current answer is no, and a 60/40 index-and-cash split
gives a similar shape with no code. If the goal is a smoother ride with positive
expectancy, it is closer than it looks.

---

## 6. What only you can do

| # | Item | Why it blocks |
|---|---|---|
| 1 | **Answer the §5 question** | Determines whether Track A continues or restarts with a different game |
| 2 | **Approve spending the holdout** | One-shot, irreversible |
| 3 | **Alpaca paper keys** (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) | Blocks all of Track C. Free. |
| 4 | **Earnings calendar provider** | Hard-blocking rule that currently **cannot fire** — `config/earnings.json` does not exist, so the code logs a warning and passes everything. Costs money; needs a decision. |
| 5 | **Confirm your data feed's latency** | The 15:30 close-confirmation check reads live prices. On a delayed feed it is reading 15-minute-old data and the confirmation is meaningless. |
| 6 | **A VPS** | Cron on a laptop that sleeps is the most likely failure mode. ~$5/month. |
| 7 | `ANTHROPIC_API_KEY` (optional) | Only for the semantic engine. Unrelated to Alpaca. |

---

## 7. Rules of engagement

These exist because the system is now capable of fooling us, and the tooling to
avoid that only works if it is used.

1. **State the hypothesis before the run.** A variant invented after looking at
   fold results is fitted, whatever the numbers say.
2. **Consistency across folds, never the aggregate.** A variant that wins on the
   total but only in one period is luck wearing a disguise.
3. **One change at a time.** Change two and you can attribute neither.
4. **The holdout is spent once.** After that there is no clean test left.
5. **Read every number against the benchmark.** A long-only system in a bull
   market looks brilliant whether or not it has any edge.
6. **Never delete the regression tests in §2.** Each one guards a bug that made
   the system look profitable when it was not.

---

## 8. First thing next session

```bash
cd Trading-Bot
python -m pytest -q                      # expect 212 passed
python -m trading_bot backtest --bars data/bars.db
```

Then pick up at **A1** (regime gate as a fifth variant) or **B1** (the
dashboard) — they are independent and either is a clean start.

**Note:** the repo has no commits yet. All 75 files are staged. Commit before
doing anything else.
