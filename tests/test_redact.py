"""Credentials must not survive into anything published.

A failing job stores its traceback in `runs.detail`, and the Actions workflow
commits that database to a PUBLIC repository. GitHub masks registered secrets in
workflow logs automatically and does nothing for a file the workflow commits, so
the scrubbing has to happen before the write.
"""

from __future__ import annotations

import pytest

from trading_bot.ops.redact import PLACEHOLDER, redact

KEY = "PKE74BAIUTXUHGRHU5U3XOPJ3K"
SECRET = "HYkBGMEmmaXGKDdnzaDx9sFj15CpvTy232cQpYuSAK3T"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", KEY)
    monkeypatch.setenv("APCA_API_SECRET_KEY", SECRET)


# --- the exact values this process holds ---------------------------------- #


def test_a_key_from_the_environment_is_removed_wherever_it_appears(keyed) -> None:
    """The only certain match. Replacing the literal catches the value however
    it reached the string -- message, URL, header dump."""
    out = redact(f"APIError: auth failed for key {KEY}")
    assert KEY not in out
    assert "APCA_API_KEY_ID" in out


def test_the_secret_is_removed_too(keyed) -> None:
    out = redact(f"GET https://api/?secret={SECRET} -> 401")
    assert SECRET not in out


def test_a_multi_line_traceback_is_scrubbed_throughout(keyed) -> None:
    trace = f"""Traceback (most recent call last):
  File "job.py", line 3, in run
    client.authenticate({KEY!r}, {SECRET!r})
APIError: rejected {KEY}
"""
    out = redact(trace)
    assert KEY not in out and SECRET not in out
    assert "Traceback" in out, "the diagnostic value must survive"


# --- shapes this process never held --------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdef0123456789abcdef",
        'api_key="sk-abcdefghijklmnopqrstuv"',
        "token = 9f8e7d6c5b4a3f2e1d0c",
        "PKZZZZZZZZZZZZZZZZZZZZ was rejected",
    ],
)
def test_credential_shapes_are_removed_even_when_unknown(text: str) -> None:
    """A credential belonging to something else in the traceback is still a
    credential, and this file becomes public."""
    assert PLACEHOLDER in redact(text)


# --- and the part that matters just as much ------------------------------- #


def test_ordinary_diagnostics_survive_intact(keyed) -> None:
    """A scrubber that eats the traceback makes failures unreadable, which is
    its own kind of outage."""
    trace = (
        'Traceback (most recent call last):\n'
        '  File "evening.py", line 88, in _body\n'
        "    raise ValueError('no SPY bar for 2026-08-28')\n"
        "ValueError: no SPY bar for 2026-08-28\n"
    )
    assert redact(trace) == trace


def test_tickers_and_numbers_are_not_mistaken_for_secrets() -> None:
    line = "OPEN AAPL 100 @ 258.57 stop 230.57 target 325.83 score 70.5"
    assert redact(line) == line


def test_an_empty_or_short_env_value_does_not_match_everything(monkeypatch) -> None:
    """An empty key would replace every empty string; a two-character one would
    shred the text. Below eight characters it is ignored."""
    monkeypatch.setenv("APCA_API_KEY_ID", "")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "abc")
    text = "a perfectly ordinary line mentioning abc"
    assert redact(text) == text


def test_redacting_nothing_returns_nothing() -> None:
    assert redact("") == ""


# --- wired into the path that actually persists --------------------------- #


def test_the_job_runner_scrubs_before_it_stores(keyed) -> None:
    """The whole point: `runs.detail` is committed to a public repo."""
    import inspect

    from trading_bot.jobs import base

    source = inspect.getsource(base.run_job)
    assert "redact(traceback.format_exc())" in source, (
        "the stored traceback must be scrubbed"
    )


def test_alerts_are_scrubbed_too(keyed) -> None:
    """A webhook posts to Slack or Discord, which is another place a key should
    not land."""
    import inspect

    from trading_bot.jobs import base

    assert "redact(f\"{type(exc).__name__}" in inspect.getsource(base.run_job)
