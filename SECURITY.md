# Security

This repository is public and contains a system that can place real orders.
Everything below is about keeping that from going wrong.

## If you are cloning this

**Nothing here is configured to trade your money, and you have to work to make
it.** Paper is the default at every layer: `AlpacaBroker(paper=not args.live)`,
so real trading needs an explicit `--live` on the command line. There is no
config file setting that flips it, deliberately — a flag you have to type is
harder to enable by accident than a value you might copy from an example.

Run it on paper for months before considering anything else. The system's own
measured expectation is a **~5.5% annual return after adjusting for survivorship
bias**, which is below what an index fund returns. Read `PLAN.md` before
assuming otherwise.

## Credentials

Keys are read from the environment only — `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY`. They are never written to a file by this code, never
logged, and never printed. `load_env()` reads a local `.env` into the
environment and lets real environment variables win.

`.env` is gitignored, along with `.env.*`, `*.pem`, `*.key`, `*.p12` and
`credentials*.json`. `deploy/setup.sh` creates `.env` with `chmod 600`.

**Alpaca issues separate key pairs for paper and live, and a paper key cannot
authenticate against the live endpoint.** That is a useful property: a bug that
somehow tried to trade for real with paper keys fails rather than succeeds.

### Rotate anything that has ever been pasted somewhere

Keys shared in a chat window, a terminal recording, a screenshot or an issue
should be treated as compromised regardless of where they ended up. Alpaca lets
you regenerate paper keys freely, so there is no cost to doing it.

## What the scheduled workflows publish

`.github/workflows/` runs the bot on GitHub Actions and **commits `data/state.db`
to this repository on every run.** That is deliberate -- the broker owns
positions and cash, this database owns intent, and losing it would leave the
reconciler adopting positions with synthetic stops and no record of why anything
was bought. It also means the contents are public.

What is in it: the watchlist, the decision log with the reason for every action
and every veto, closed trades with their P&L, and the equity history. All of it
for a **paper** account. Publishing it is the point of the project; know that it
is happening.

**Tracebacks are scrubbed before they are stored.** A failing job writes
`traceback.format_exc()` into `runs.detail`, and GitHub masks registered secrets
in workflow *logs* but does nothing for a file the workflow commits.
`ops/redact.py` removes the exact values of `APCA_API_KEY_ID`,
`APCA_API_SECRET_KEY` and `ANTHROPIC_API_KEY` from the environment, plus
anything credential-shaped that this process never held. Thirteen tests cover
it, including that ordinary diagnostics survive intact -- a scrubber that eats
the traceback is its own kind of outage.

**Neither workflow can be triggered by a fork.** They run on `schedule` and
`workflow_dispatch` only; there is no `pull_request` or `pull_request_target`
trigger, so a pull request from a fork cannot reach the secrets. Nor can either
pass `--live`; a test asserts it.

## The dashboard server

`python -m trading_bot serve` exposes the halt/resume control. It:

* binds `127.0.0.1` by default
* **refuses to start** on a non-local interface without `--token`, because an
  unauthenticated kill switch reachable from the network is a worse failure than
  the server not starting
* compares the token with `secrets.compare_digest`, not `==`
* exposes no route that can place, modify or cancel an order — halt and resume
  are the only state it can change

Two limitations worth knowing:

**On localhost with no token, anything local can halt or resume it.** That is
fine on a single-user VPS and is not fine on a shared machine. Pass `--token`
if anyone else has an account.

**There is no CSRF token.** A page you visit in a browser could POST to a
localhost dashboard. Halting is fail-safe, but *resuming* a deliberately halted
bot is not. Bind it only while you are using it, or reach it over an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@host
```

## What the code does not do

No `eval`, no `exec`, no `pickle`, no `subprocess`, no shell invocation. Every
SQL statement uses bound parameters; none is built by string formatting. The
network calls are to Alpaca and to Yahoo's public endpoints, and nothing accepts
input from an untrusted party.

## Dependencies

Declared in `pyproject.toml` with lower bounds rather than exact pins, so
security patches are not frozen out. The decision path — models, policy, sizing,
scoring, the constitution, `decide()`, every indicator, the backtest engine and
the state store — is standard library only.

## Reporting something

Open an issue. This is a personal research project with no users to endanger, so
there is no embargo process; please just do not include working credentials in
the report.
