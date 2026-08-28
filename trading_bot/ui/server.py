"""A local, read-mostly web view of the agent, with a kill switch.

Deliberately stdlib-only and bound to localhost. The page is regenerated from
the state database on every request, so it is never stale and there is no cache
to invalidate.

SECURITY POSTURE, stated plainly because this is the one component that can
change the system's behaviour:

  * binds 127.0.0.1 by default -- reach it from elsewhere over an SSH tunnel
    (`ssh -L 8080:localhost:8080 user@host`), not by opening a port
  * the only mutating endpoints are halt and resume, and they need a token when
    one is set. There is no endpoint that can place, size, or cancel an order
  * refusing to bind a public interface without a token is enforced, not advised

The kill switch is here because it is the one control worth having at 2am on a
phone. Everything else is a decision for the morning.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..core.policy import Policy
from ..data.cache import BarCache
from ..db.repo import Repo
from . import dashboard

log = logging.getLogger("trading_bot.server")

CONTROLS = """
<form method="post" action="/halt" style="display:inline">
  <input type="hidden" name="token" value="{token}">
  <button class="btn danger" {halt_disabled}>Engage kill switch</button>
</form>
<form method="post" action="/resume" style="display:inline">
  <input type="hidden" name="token" value="{token}">
  <button class="btn" {resume_disabled}>Release</button>
</form>
<style>
.btn{{font:inherit;padding:7px 14px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);background:var(--card);color:var(--fg)}}
.btn:disabled{{opacity:.4;cursor:not-allowed}}
.btn.danger{{border-color:var(--neg);color:var(--neg)}}
.controls{{margin:14px 0 4px;display:flex;gap:8px;align-items:center}}
.controls .hint{{color:var(--dim);font-size:12px}}
</style>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "trading-bot"

    # injected by serve()
    state_path = "data/state.db"
    bars_path = "data/bars.db"
    policy_path = "config/policy.yaml"
    token = ""

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(self._render())
        elif path == "/health":
            self._send_json({"ok": True, "halted": self._repo().is_halted()})
        else:
            self._send_html("<h1>404</h1>", status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/halt", "/resume"):
            self._send_html("<h1>404</h1>", status=404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        supplied = (form.get("token") or [""])[0]

        # Constant-time compare: this is the only gate on the one control that
        # changes behaviour.
        if self.token and not secrets.compare_digest(supplied, self.token):
            self._send_html("<h1>403 — bad token</h1>", status=403)
            return

        repo = self._repo()
        if path == "/halt":
            repo.set_halt(True, "engaged from the dashboard")
            log.warning("kill switch ENGAGED from %s", self.address_string())
        else:
            repo.set_halt(False)
            log.warning("kill switch released from %s", self.address_string())

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    # ------------------------------------------------------------------ #

    def _repo(self) -> Repo:
        return Repo(self.state_path)

    def _render(self) -> str:
        repo = self._repo()
        try:
            policy = Policy.from_yaml(self.policy_path)
        except Exception:  # noqa: BLE001
            policy = Policy()

        page = dashboard.render(
            repo, BarCache(self.bars_path), policy, as_of=date.today()
        )
        halted = repo.is_halted()
        controls = CONTROLS.format(
            token=html.escape(self.token),
            halt_disabled="disabled" if halted else "",
            resume_disabled="" if halted else "disabled",
        )
        banner = (
            '<div class="controls">' + controls
            + '<span class="hint">Halting blocks new entries. '
            "Exits and stop management continue.</span></div>"
        )
        return page.replace("<h2>Equity vs benchmark</h2>", banner + "<h2>Equity vs benchmark</h2>")

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_server(
    host: str,
    port: int,
    state: str,
    bars: str,
    policy: str,
    token: str,
) -> ThreadingHTTPServer:
    """Refuses to bind a non-local interface without a token.

    Enforced rather than documented: an unauthenticated kill switch reachable
    from the network is a worse failure than the server not starting.
    """
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise ValueError(
            f"refusing to bind {host} without a token -- the halt control would "
            "be open to anyone who can reach the port. Pass --token, or bind "
            "127.0.0.1 and use an SSH tunnel."
        )

    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "state_path": state,
            "bars_path": bars,
            "policy_path": policy,
            "token": token,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    state: str = "data/state.db",
    bars: str = "data/bars.db",
    policy: str = "config/policy.yaml",
    token: str = "",
) -> None:
    httpd = build_server(host, port, state, bars, policy, token)
    print(f"dashboard on http://{host}:{port}")
    if not token:
        print("  no token set -- halt/resume are open to anything reaching this port")
    if host in ("127.0.0.1", "localhost"):
        print(f"  remote access:  ssh -L {port}:localhost:{port} user@host")
    print("  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
