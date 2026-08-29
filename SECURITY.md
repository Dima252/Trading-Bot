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
