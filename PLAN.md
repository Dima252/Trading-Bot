# Where we stopped, and what happens next

Last updated 2026-08-28. Architecture reference is [README.md](README.md);
deployment is [deploy/README.md](deploy/README.md). This file is the working log
and forward plan.

---

## 1. State of the build

| | |
|---|---|
| Implementation | ~8,900 lines, 58 modules |
| Tests | **326 passing**, ~22s, 84% coverage, `ruff` clean |
| Market data | 887,235 daily bars, 505 symbols, 2019-07 → 2026-08 |
| Shipping config | `config/policy.yaml` **v2-holdout** — frozen, out-of-sample tested |
| Broker | Alpaca paper connected and verified ($100k account) |
| Research | **Closed.** The holdout is spent; historical data is exhausted |
| Next | Two weeks of `--dry-run`, then six months unchanged (§4) |

### What runs today

```bash
python -m pytest -q                    # 326 tests
python -m ruff check .                 # lint, incl. the datetime rules
python scripts/demo.py                 # decision core on a hand-built book
python -m trading_bot fetch            # real bars, no API key needed (Yahoo)
python -m trading_bot backtest         # full report vs SPY
python -m trading_bot diagnose         # is the ranking predictive?
python -m trading_bot walkforward      # does a change hold across periods?
python -m trading_bot dashboard        # one self-contained HTML file
python -m trading_bot serve            # live view + kill switch
python -m trading_bot daily            # THE DRY RUN: fetch, scan, render
```

The four jobs have run against the real paper account. They have **never placed
an order** — every run so far has been `--dry-run` or paper-broker rehearsal.

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

## 2c. Data integrity — three things trusted without checking

All three are silent by nature: nothing looks broken, and the scan returns a
plausible answer every time. They are recorded here because none is discoverable
by reading the code that suffers from it — each lives in the gap between two
components that individually look correct.

### Guard 1 — a stale cache (found session 4)

The scanner matches bars to the session date **exactly**, so a cache not
refreshed past today makes every symbol invisible and the scan returns a clean
zero. Indistinguishable in the logs from "a quiet market", and over months it
would read as one. `evening` now errors instead.

### Guard 2 — a provisional bar (found 2026-08-28, live)

Worse than guard 1, because guard 1 does not catch it. **A daily bar exists from
the opening bell onward, and its "close" is just the last trade.** Nothing in the
payload marks it as unsettled — a bar fetched at 14:00 is byte-identical in shape
to one fetched at 18:00. The staleness guard passes, because a bar *is* present.

What actually happened: a `fetch` started at 14:11 ET wrote 472 mid-session bars
for 2026-08-28. Two compounding defects:

| Defect | Consequence |
|---|---|
| `evening` trusted any present bar | Would have scanned 505 names off moving quotes and written a watchlist from fiction |
| Incremental refresh skipped symbols whose coverage already reached `end` (Yahoo), or started tails at `last + 1 day` (Alpaca — **zero overlap**) | No later fetch would ever revisit that day. The bad bar was **permanent** |

Fixed on all three counts:

- `trading_bot/market_hours.py` — `session_is_final(day)`, exchange-clock aware,
  so the verdict does not depend on the host's timezone (it is UTC+3 here).
- `evening` refuses to run before the bell, as its **first** check, ahead of the
  500-day universe load.
- Both refresh paths re-read from `last_cached - OVERLAP_DAYS`. There is
  deliberately **no** "already up to date, skip" branch: coverage reaching `end`
  is not evidence the last bar is any good. Stores are upserts, so re-running
  after the close repairs the day.

The 472 rows were deleted and coverage rolled back to 2026-08-27; a backup sits
at `data/bars.db.bak`. `tests/test_market_hours.py` pins all of it.

### Guard 3 — a fabricated regime (found the same day, while checking guard 2)

The evening scan of 2026-08-27 measured **`chop`**, correctly opened nothing, and
recorded the regime to `equity_history`. It also wrote its 113 setups as
`status='Pending'`.

The next morning `open_job` would have entered all 113 — because its signature
read `regime: Regime = Regime.TREND`, and the CLI never passed one. TREND is the
only regime the frozen policy permits trading in, so the gate always opened. The
backtest never behaved this way: `engine.py` feeds `decide()` the regime it
actually measured.

That is a **live-vs-backtest divergence in the permissive direction**, in exactly
the regime the shipped config excludes. The holdout number describes a system
that does not trade chop; the deployed one would have.

Fixed at the root — the fabricated default, not the candidate rows:

- `open_job.run` / `close_job.run` take `Regime | None` with **no default**.
- `Repo.last_regime(day)` reads back what the evening scan measured; the CLI
  passes it. The intraday jobs cannot classify it themselves — at 10:00 today's
  bar is still forming, and a regime read off an unsettled close is not a regime.
- `None` means *unknown*, and unknown **declines to enter** while defensive work
  continues. Unknown must never resolve to the permissive answer.

Candidates are still written as Pending in an untradeable regime, deliberately:
the decision to decline them is then recorded as a rejection with a reason,
which is a better audit trail than never writing them at all.

**The general lesson, and the reason all three are in the plan rather than in
commit messages:** each was a *default that looked like a fact*. A bar's presence
stood in for it being settled; coverage reaching `end` stood in for it being
correct; a parameter default stood in for a measurement. None announced itself,
and the tests passed throughout. Worth asking of anything added later: **is this
value measured, or merely assumed?**

---

## 3. The decision that was open, and how it resolved

Kept because the reasoning matters more than the outcome.

**The question was:** spend the holdout on `all_three`, which cleared 4/4 folds?
I recommended waiting, on the grounds that clearing the §5 bar in only 2 of 4
periods made the expected holdout result close to a coin flip.

**What actually happened:** the universe turned out to be the binding
constraint (§5c). On 503 names instead of 85, `all_three` cleared the bar in
**4/4** folds at 13.64% CAGR. It looked finished. Then the holdout was spent —
and it went **negative**.

**The lesson, in one line:** four-for-four across independent periods was not
enough. Fold consistency is necessary and not sufficient, which is exactly why
the holdout existed and exactly why it can only be spent once.

---

## 4. What remains

Track A (does this have an edge?) is **closed** — the historical data is spent.
What is left is operational.

### Ready and waiting on you

| Step | What | Owner |
|---|---|---|
| **C1** | Alpaca paper keys | **done** — connected, $100k paper account verified |
| **C2** | Two weeks of `python -m trading_bot daily`, reading the decision log | you, ~5 min/day |
| **C3** | `daily --arm`. Six months. Change nothing. | you |
| **C4** | `report` — the first real attribution | me, at the end |

**Decided (2026-08-28): C2 runs on this machine**, and moves to a host once it
is clearly working.

One command does the whole nightly routine, in order, stopping at the first
failure:

```bash
python -m trading_bot daily
```

Three requirements used to be things to remember. All three are now enforced by
the code, because each one failed silently at least once (§2c):

1. **After the closing bell.** 16:00 ET is **23:00 local** (22:00 in winter).
   `daily` refuses to do anything earlier — and refuses *before* fetching, since
   the fetch takes minutes and its output would be discarded anyway.
2. **`fetch` before the scan**, or the scan meets a stale cache and reports a
   confident zero. `daily` sequences them and stops if the fetch fails.
3. **The trading day is New York's**, not this host's. Running at 00:30 local
   still means the previous session; it used to mean tomorrow, and tomorrow is
   not a trading day.

**The practical risk with C2 on a laptop is the 23:00 slot**, not the software.
Fourteen consecutive late nights is a habit that breaks, and missed sessions
shrink the sample the stopping rule depends on. If the first week feels like a
chore, that is the signal to bring the host forward rather than to push through.

### Deployment is ready

`deploy/` holds a validated crontab template, an idempotent `setup.sh` that
verifies each step and refuses to proceed on a failing test suite, and a README
covering the local-vs-host tradeoff. `tests/test_deploy.py` guards the one
invariant that is not obvious from reading the crontab: **`fetch` must precede
`evening`**, or the scanner meets a stale cache and the session is lost.

### Built but not yet configured

| Item | Why it matters | What it needs |
|---|---|---|
| Webhook alerts | Failures, tripped breakers, book changes | one URL → `TRADING_BOT_WEBHOOK` |
| Heartbeat monitor | **The only thing that catches a job never running.** Dead code sends no alerts. | healthchecks.io free tier → `TRADING_BOT_HEARTBEAT` |
| An always-on host | A laptop misses sessions; cron with `CRON_TZ` also fixes the DST drift Windows Task Scheduler has | ~$5/month VPS |

### Not built, deliberately

| Item | Why it is not done |
|---|---|
| Earnings calendar | **Before real money, not before paper.** The backtest ran without it, so wiring it now would make the trial test a system that was never validated (§5d) |
| `ADD` to a position | Three-step broker transaction (cancel OCO → add → re-place) with rollback. `decide()` never emits it; the constraint layer already validates it |
| Fixing `regime_fit` | Known inert and unvalidated. Changing it changes the tested system — revisit only with live data (§5d) |
| More backtesting | The historical data is exhausted. It can only produce overfitting now |

---

## 5. The bar — a defensive mandate, made concrete

**Decision taken (session 3): a defensive profile is what we want.** That
settles the strategic question and changes what "good" means. It does NOT mean
the bar gets lower.

### Why the current best is not good enough

`all_three` compounds to **+4.87% CAGR at a 9.17% worst drawdown** over the
tested 4.8 years. Over that same window the 13-week T-bill averaged **2.89%**.

A defensive strategy earning ~5% while cash earned ~3% — and taking a 9%
drawdown to do it — is not worth running. That is the real problem, and it is
not the same problem as "doesn't beat SPY".

### What it has to clear

| Metric | Target | `all_three` today |
|---|---|---|
| CAGR | **≥ 8%** | 4.87% |
| Max drawdown | **≤ 15%** | 9.17% |
| Return / drawdown (annualised) | **≥ 0.7** | 0.53 |
| Excess over cash | **≥ 4pp** | ~2pp |
| Positive folds | **≥ 3 / 4** | 3 / 4 |

Note the drawdown line: the system is **under-risked**, not over-risked. It uses
9% of a 15% budget. For a defensive mandate that headroom is wasted capacity.

### Where the missing return is — three diagnosed causes

1. **Idle cash earned nothing.** The backtest paid 0% while the strategy sat in
   cash most of the time. Fixed: `^IRX` is now cached and accrued daily. This is
   a correctness fix, not an optimisation.
2. **The risk budget is half-used.** 1% risk per trade and a 6% heat cap produce
   a 9% drawdown against a 15% tolerance. Scaling risk scales return roughly
   proportionally and stays inside the envelope.
3. **The regime gate shuts the book 54% of the time for a thin edge.** Measured
   over the tested window: SPY's forward 20-day return is **+0.98% on chop days
   vs +1.24% on trend days** — and `high_vol` days, which the gate also blocks,
   were the *best* forward periods at +2.84%. The gate lifts per-trade
   expectancy but may cost more in unused capital than it saves. Tested both
   ways as `defensive` and `defensive_open`.

## 5b. Session 3 result — the bar is met, with caveats

Two fixes and two refutations, then one clean test.

| Variant | CAGR | worst DD | ret/DD | vs cash | criteria |
|---|---|---|---|---|---|
| `all_three` (cash yield on) | 6.87% | 8.03% | 0.86 | +3.98pp | 3/5 |
| **`risk_1p6`** (1.6× envelope) | **8.09%** | **7.97%** | **1.01** | **+5.20pp** | **5/5** |

*(SPY over the same window: ~16% CAGR at 24.5% drawdown, ret/DD 0.65.)*

### What worked

**Cash now earns the T-bill rate.** A correctness fix, not an optimisation —
the backtest paid 0% on idle cash while the strategy sat in cash most of the
time. Worth ~2pp of CAGR on its own.

**Scaling the whole risk envelope 1.6×** (risk 1.6%, heat 9.6%, position cap
24%) — one coherent change, nothing else touched.

### What was refuted

**Removing the regime gate is worse.** `defensive_open` won 2/4 folds — "likely
luck". Despite SPY's forward return barely differing by regime label, the gate
earns its keep on the setups we actually trade.

**`defensive` bundled five changes at once and is therefore uninformative.**
That violates rule 3 in §7 and it was my error. The tell: R multiples are
size-invariant, so a pure sizing change leaves expectancy alone. Expectancy fell
from +0.155 to +0.039, which means selection changed — most likely the pullback
reweighting, not the sizing. Kept in the code as a record of the mistake.

### Three reasons not to trust 5/5 yet

1. **It is not a pure sizing change.** Expectancy fell +0.155 → +0.122 and fold
   consistency 4/4 → 3/4. Bigger positions hit the capacity caps, so fewer
   trades are taken (78→61, 98→69). The book is more concentrated, which is the
   opposite of what a defensive mandate wants.
2. **The drawdown did not scale, and it should have.** 1.6× the risk should give
   roughly 1.6× the drawdown (8.0% → ~12.8%). It stayed at 8.0%. An unexplained
   free lunch in a backtest is usually a warning, not a discovery.
3. **Eight variants have now been tested against the same four folds.** Finding
   one that clears a five-part bar after eight attempts is weak evidence. This
   is exactly the multiple-comparison problem §7 warns about.

### Therefore: stop tuning

Further variants make the in-fold evidence weaker, not stronger. The holdout
(2025-06-11 → 2026-08-27) exists for precisely this moment.

**Proposed, needing approval:** run `risk_1p6` AND `all_three` on the holdout,
once, reporting both. Decision rule fixed in advance — `risk_1p6` is the primary
candidate because it met the bar; `all_three` is the reference. If `risk_1p6`
fails the holdout while `all_three` holds, that is evidence `risk_1p6` was
fitted to the folds.

### If the holdout disappoints, the next lever is structural, not parametric

With the regime gate on and only 85 names, `all_three` takes 78–98 trades per
fold against baseline's 216–229 — it is starved of candidates exactly when it is
allowed to trade. Widening to ~400 liquid names gives a deeper pool to select
from and more opportunities during the 46% of days the gate is open. Yahoo
fetching is free; it is a ticker-list change and a longer backtest.

---

## 5c. Session 4 — the universe, and the holdout SPENT

### The universe was the real constraint

`all_three` was starved of candidates: 78-98 trades per fold from 85 names. On
the 503-name S&P 500 snapshot, in-fold:

| Universe | Variant | CAGR | maxDD | ret/DD | folds clearing bar |
|---|---|---|---|---|---|
| Narrow (85) | `all_three` | 6.87% | 8.03% | 0.86 | 2/4 |
| Narrow (85) | `risk_1p6` | 8.09% | 7.97% | 1.01 | 2/4 |
| Wide (503) | `all_three` | 13.64% | 7.64% | 1.79 | **4/4** |
| Wide (503) | `risk_1p6` | 11.87% | 11.36% | 1.04 | 3/4 |
| — | SPY | 16.31% | 24.50% | 0.65 | — |

It also showed the earlier `risk_1p6` win was fixing the wrong problem: with a
deep pool, scaling risk is *worse* than not (11.87% at 11.4% DD against 13.64%
at 7.6%). Concentration was compensating for starvation.

### THE HOLDOUT IS SPENT (2025-06-11 -> 2026-08-27)

Recorded in `out/holdout_result.txt`. `all_three` was the primary candidate on
in-fold evidence; `risk_1p6` was the reference.

| | in-fold exp R | holdout exp R | drift | holdout CAGR | maxDD | ret/DD |
|---|---|---|---|---|---|---|
| `all_three` | +0.169 | **-0.132** | **-0.301** | 3.56% | 8.69% | 0.41 |
| `risk_1p6` | +0.120 | **+0.127** | **+0.007** | 12.20% | 6.83% | 1.79 |

**`all_three` was overfit and the holdout caught it.** 4/4 fold consistency did
not generalise: expectancy swung from +0.169R to **negative**. This is precisely
the failure the holdout exists to detect, and the variant I would have shipped
had I trusted the fold evidence.

**`risk_1p6` generalised.** Expectancy moved +0.007R between in-fold and
out-of-sample — the *stability* matters more than the level. On absolute targets
it passes all four: 12.20% CAGR, 6.83% drawdown, ret/DD 1.79, +8pp over cash.

Neither beat SPY risk-adjusted, but the holdout was an exceptional bull run for
the index (+30% at an 8.88% drawdown, ret/DD 3.38). No defensive profile clears
that, and requiring it would mean requiring the strategy to beat the index in
the index's best conditions -- which is not what a defensive mandate is for.

### What this means, stated carefully

I ran two variants and one survived. **Picking the survivor now is selecting on
the holdout**, which is a milder form of the error the holdout exists to
prevent. `risk_1p6` is the best-supported candidate, not a validated one.

**The historical data is now exhausted for decision-making.** Every period has
informed a choice. More backtesting cannot produce new evidence -- it can only
produce more overfitting. The next real evidence has to come from data that does
not exist yet.

---

## 5d. THE STOPPING RULE — written before the trial, on purpose

Committed 2026-08-28, before a single paper trade. The point of writing it now
is that it cannot be renegotiated later by a version of us that has watched the
equity curve for a month.

> **Six months of paper trading on `config/policy.yaml` v2-holdout.**
> **No changes to strategy, sizing, universe, or exits during that window.**
>
> **Stop, and do not restart, if either:**
> - realized expectancy is below zero after 100+ closed trades, or
> - live expectancy diverges from the holdout's +0.127R by more than 0.15R
>
> **Do not change anything mid-trial** — not the regime-fit priors, not the
> scoring weights, not the universe. A change resets the clock to zero, because
> the sample stops being one sample.
>
> Bugs and operational failures are exempt: fixing a crash is not a strategy
> change. Anything that alters which trades are taken is.

### Why this specific rule

`all_three` was consistent across four independent periods and still went
negative out of sample. Fold consistency was not enough. The only test left is
data that does not exist yet, and it is only a test if nothing moves while it
runs.

The temptation once it is live will be to adjust after a bad week. That
temptation is the single most likely way this project produces a confidently
wrong answer.

### What "no changes" does not cover

The earnings calendar is **not** wired, and that is deliberate for the trial:
the backtest ran without it too (`NullSemanticEngine`, no `config/earnings.json`),
so the paper trial tests the same system the 12.20% CAGR came from. Wire it
before real money, not before paper.

---

## 5e. Repo audit (session 4)

Removed as unused, verified by reference scan:

| Removed | Why |
|---|---|
| `notify.payload_for_test` | 0 refs; documented a `status --test-alerts` flag that never existed |
| `universe.sector_of` | 0 refs; `SECTORS` is read directly |
| `BarSeries.through()` | 0 refs |
| `Bar.range` | 0 refs |
| `sizing.heat_contribution` | exported and tested but used by nothing — a test for dead code is still dead |
| `ActionKind.HOLD` | never emitted; holding is the *absence* of an action, and an enum member implied otherwise |
| `scripts/seed_demo_data.py` | superseded — Yahoo `fetch` needs no credentials, so synthetic bars have no remaining purpose |

Also deduplicated: the "index and rate series are never positions" rule was
written out in both the backtest engine and the walk-forward. Both now call
`universe.is_tradeable`.

Kept deliberately:

- **`ActionKind.TRIM` / `ADD`** — not emitted yet, but the constraint layer
  validates them and they are the documented path for position management.
- **The `defensive` variant**, despite being an invalid test (five changes at
  once). Deleting it would make `records/walkforward_defensive.txt`
  unreproducible; the comment marks it as a record of the mistake.
- **`scripts/demo.py`** — runs the decision core on a hand-built book with no
  data, credentials or network. The fastest way to see the brain work.

---

## 6. What only you can do

Only the first is blocking. The rest are before-real-money items.

| # | Item | Status |
|---|---|---|
| 1 | **Alpaca paper keys** — `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | **BLOCKING.** Free, ~5 minutes at alpaca.markets. Nothing in Track C can start without them. |
| 2 | **An always-on host** | Blocking for the real trial. ~$5/month. Cron on a laptop that sleeps is the likeliest failure mode. |
| 3 | **A webhook URL** for alerts (`TRADING_BOT_WEBHOOK`) | Optional but strongly advised. Slack or Discord incoming webhook; one URL. |
| 4 | **A heartbeat monitor** (`TRADING_BOT_HEARTBEAT`) | Optional but strongly advised. healthchecks.io free tier. **This is the only thing that catches a job never running at all** — dead code sends no alerts. |
| 5 | **Confirm your data feed's latency** | Before real money. The 15:30 close-confirmation check reads live prices; on a delayed feed it reads 15-minute-old data. |
| 6 | **Earnings calendar provider** | Before real money, NOT before paper — see §5d. |
| 7 | `ANTHROPIC_API_KEY` | Optional. Semantic engine only; unrelated to Alpaca. |

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
