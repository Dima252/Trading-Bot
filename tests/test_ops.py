"""Notifications and the control server.

Two properties matter more than the features. A notifier must never be able to
take down the job it is reporting on, and the server must never be able to place
an order.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from trading_bot.core.models import SetupType
from trading_bot.db.repo import Repo
from trading_bot.ops import notify as N
from trading_bot.ui import server as S

DAY = date(2025, 6, 10)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(N.WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(N.HEARTBEAT_ENV, raising=False)


# --- notifications --------------------------------------------------------- #


def test_unconfigured_notify_is_a_no_op() -> None:
    assert N.configured() is False
    assert N.notify(N.Level.ERROR, "boom") is False


def test_notify_posts_when_a_webhook_is_set(monkeypatch) -> None:
    sent = {}

    class FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None, headers=None):
            sent.update(url=url, payload=json)

    monkeypatch.setenv(N.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)

    assert N.notify(N.Level.WARN, "title", "body") is True
    assert sent["url"] == "https://example.invalid/hook"
    # both field names, so one payload works for Slack or Discord
    assert "title" in sent["payload"]["text"]
    assert sent["payload"]["text"] == sent["payload"]["content"]


def test_a_failing_webhook_never_raises(monkeypatch) -> None:
    """A broken notifier must not turn a warning into an outage."""

    class Exploding:
        @staticmethod
        def post(*a, **k):
            raise RuntimeError("network down")

    monkeypatch.setenv(N.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setitem(__import__("sys").modules, "requests", Exploding)

    assert N.notify(N.Level.ERROR, "still fine") is False


def test_heartbeat_pings_the_configured_monitor(monkeypatch) -> None:
    calls = []

    class FakeRequests:
        @staticmethod
        def get(url, timeout=None):
            calls.append(url)

    monkeypatch.setenv(N.HEARTBEAT_ENV, "https://hc.invalid/ping/")
    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)

    N.heartbeat("evening")
    N.heartbeat("evening", failed=True)
    assert calls == [
        "https://hc.invalid/ping/evening",
        "https://hc.invalid/ping/evening/fail",
    ]


def test_a_failing_heartbeat_never_raises(monkeypatch) -> None:
    class Exploding:
        @staticmethod
        def get(*a, **k):
            raise RuntimeError("no route")

    monkeypatch.setenv(N.HEARTBEAT_ENV, "https://hc.invalid/ping/")
    monkeypatch.setitem(__import__("sys").modules, "requests", Exploding)
    assert N.heartbeat("evening") is False


def test_status_says_when_nothing_is_wired() -> None:
    """Silence is not proof of health, so the absence must be visible."""
    text = N.describe_config()
    assert "unset" in text
    assert "raises no alarm at all" in text


# --- the control server ---------------------------------------------------- #


def seed(path) -> Repo:
    repo = Repo(path)
    repo.save_position_annotation(
        ticker="AAA",
        sector="Technology",
        setup_type=SetupType.BREAKOUT,
        entry_score=70.0,
        initial_stop=90.0,
        target=130.0,
        opened_at=DAY - timedelta(days=4),
        policy_version="v2-holdout",
        entry_price=100.0,
        entry_qty=50,
        thesis="breakout in trend",
    )
    repo.record_equity(DAY, cash=50_000, equity=101_000, positions=1, heat_pct=0.02)
    return repo


@pytest.fixture
def live(tmp_path):
    state = str(tmp_path / "state.db")
    seed(state)
    httpd = S.build_server(
        "127.0.0.1", 0, state, str(tmp_path / "bars.db"), "config/policy.yaml", "s3cret"
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


def post(url: str, data: str):
    req = urllib.request.Request(
        url, data=data.encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urllib.request.urlopen(req, timeout=5)


def test_a_public_bind_without_a_token_is_refused(tmp_path) -> None:
    """An unauthenticated kill switch on a public interface is worse than the
    server failing to start, so this is enforced rather than advised."""
    with pytest.raises(ValueError, match="refusing to bind"):
        S.build_server("0.0.0.0", 8080, str(tmp_path / "s.db"), "b.db", "p.yaml", "")


def test_localhost_without_a_token_is_allowed(tmp_path) -> None:
    httpd = S.build_server(
        "127.0.0.1", 0, str(tmp_path / "s.db"), "b.db", "config/policy.yaml", ""
    )
    httpd.server_close()


def test_the_page_renders_with_controls(live) -> None:
    base, _ = live
    body = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    assert "Trading Bot" in body
    assert "Engage kill switch" in body
    assert "AAA" in body


def test_health_endpoint(live) -> None:
    base, _ = live
    data = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
    assert data == {"ok": True, "halted": False}


def test_halt_and_resume_round_trip(live) -> None:
    base, state = live

    post(base + "/halt", "token=s3cret")
    assert Repo(state).is_halted() is True

    post(base + "/resume", "token=s3cret")
    assert Repo(state).is_halted() is False


def test_a_bad_token_cannot_halt(live) -> None:
    base, state = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/halt", "token=wrong")
    assert exc.value.code == 403
    assert Repo(state).is_halted() is False


def test_unknown_paths_are_404(live) -> None:
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/nope", timeout=5)
    assert exc.value.code == 404


def test_the_server_has_no_path_to_an_order() -> None:
    """Halt and resume are the only mutations. Nothing here trades."""
    import inspect

    source = inspect.getsource(S)
    for forbidden in ("submit_bracket", "close_position", "cancel_order", "decide("):
        assert forbidden not in source


# --- a disconnected machine ------------------------------------------------ #


class OfflineBroker:
    """Every call raises, the way a broker adapter does with no network."""

    def __init__(self):
        self.calls = 0

    def _die(self, *a, **k):
        self.calls += 1
        raise ConnectionError("getaddrinfo failed")

    is_trading_day = _die
    account = _die
    positions = _die
    orders = _die


def test_a_job_with_no_network_fails_cleanly(tmp_path, monkeypatch):
    """Offline must produce a logged, notified failure -- not a traceback.

    The calendar check needs the network, so with it outside the try block a
    disconnected machine crashed before opening a run: no record, no alert, no
    failed heartbeat. Exactly the three things that would tell you it happened.
    """
    from datetime import date as _date

    from trading_bot.core.policy import Policy
    from trading_bot.data.cache import BarCache
    from trading_bot.jobs import evening
    from trading_bot.jobs.base import AgentContext

    monkeypatch.setattr("trading_bot.jobs.base._with_retry", lambda fn, **k: fn())

    repo = Repo(str(tmp_path / "s.db"))
    ctx = AgentContext(
        repo=repo,
        broker=OfflineBroker(),
        cache=BarCache(tmp_path / "b.db"),
        policy=Policy(),
        sectors={},
        day=_date(2026, 8, 27),
    )

    result = evening.run(ctx)  # must not raise

    assert result.status == "error"
    assert any("FAILED" in n for n in result.notes)

    row = repo.last_run("evening")
    assert row is not None and row["status"] == "error"
    assert "ConnectionError" in (row["detail"] or "")


def test_a_transient_blip_is_retried(monkeypatch):
    """One flaky call must not cost a whole session -- these run once a day."""
    from trading_bot.jobs.base import _with_retry

    monkeypatch.setattr("time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("blip")
        return True

    assert _with_retry(flaky, attempts=3, delay=0) is True
    assert attempts["n"] == 3


def test_retry_gives_up_and_propagates(monkeypatch):
    from trading_bot.jobs.base import _with_retry

    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(ConnectionError):
        _with_retry(lambda: (_ for _ in ()).throw(ConnectionError("down")),
                    attempts=2, delay=0)
