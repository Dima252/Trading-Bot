"""Telling a human when something is wrong.

The realistic failure of a six-month unattended run is not a bad trade. It is a
cron job that quietly stopped firing and nobody noticing for three weeks. So
this module handles two different problems, because they need different answers:

**Something went wrong while running.** An exception, a tripped breaker, a
reconciliation warning. The process is alive and can shout -- that is `notify()`.

**Nothing ran at all.** No in-process notifier can catch this: dead code sends no
alerts. The only fix is an EXTERNAL watchdog that expects to hear from you and
alarms when it does not. `heartbeat()` pings such a service after a successful
job; configure one (healthchecks.io, Better Stack, a cron monitor) and it will
tell you when the pings stop.

Everything here is opt-in via environment variables and fails silent-but-logged.
A notifier that can crash a trading job is worse than no notifier.
"""

from __future__ import annotations

import logging
import os
from enum import Enum

log = logging.getLogger("trading_bot.notify")

WEBHOOK_ENV = "TRADING_BOT_WEBHOOK"      # Slack / Discord / any JSON endpoint
HEARTBEAT_ENV = "TRADING_BOT_HEARTBEAT"  # base URL of an external cron monitor
TIMEOUT = 10


class Level(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"

    @property
    def emoji(self) -> str:
        return {"INFO": "•", "WARN": "▲", "ERROR": "✕"}[self.value]


def configured() -> bool:
    return bool(os.environ.get(WEBHOOK_ENV))


def notify(level: Level, title: str, message: str = "") -> bool:
    """Send one alert. Returns whether it actually went anywhere.

    Never raises. A failed notification must not take down the job it was
    reporting on -- that would turn a warning into an outage.
    """
    text = f"{level.emoji} [{level.value}] {title}"
    if message:
        text += f"\n{message}"

    getattr(log, {"INFO": "info", "WARN": "warning", "ERROR": "error"}[level.value])(
        "%s %s", title, message
    )

    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        return False

    try:
        import requests

        # `text` is what Slack and Discord both read; `content` is Discord's
        # field name. Sending both means one payload works for either.
        requests.post(
            url,
            json={"text": text, "content": text},
            timeout=TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        return True
    except Exception as exc:
        log.warning("notification failed (%s): %s", type(exc).__name__, exc)
        return False


def heartbeat(job: str, failed: bool = False) -> bool:
    """Ping an external monitor so IT can alarm when we go silent.

    Set TRADING_BOT_HEARTBEAT to a monitor URL; each job pings `<url>/<job>`,
    and `/fail` on failure. This is the only mechanism that catches the job
    never running -- the process being dead is precisely why it cannot report.
    """
    base = os.environ.get(HEARTBEAT_ENV)
    if not base:
        return False

    url = f"{base.rstrip('/')}/{job}" + ("/fail" if failed else "")
    try:
        import requests

        requests.get(url, timeout=TIMEOUT)
        return True
    except Exception as exc:
        log.warning("heartbeat failed (%s): %s", type(exc).__name__, exc)
        return False


def format_summary(result) -> str:
    """A JobResult rendered for a chat message."""
    lines = [f"{result.job} {result.day}: {result.summary}"]
    for note in result.notes[:12]:
        lines.append(f"  {note}")
    if len(result.notes) > 12:
        lines.append(f"  ... and {len(result.notes) - 12} more")
    return "\n".join(lines)


def describe_config() -> str:
    """What is wired, for `status` to print. Silence is not proof of health."""
    parts = []
    parts.append(
        f"webhook:   {'configured' if os.environ.get(WEBHOOK_ENV) else f'unset ({WEBHOOK_ENV})'}"
    )
    parts.append(
        f"heartbeat: {'configured' if os.environ.get(HEARTBEAT_ENV) else f'unset ({HEARTBEAT_ENV})'}"
    )
    if not os.environ.get(HEARTBEAT_ENV):
        parts.append(
            "  without a heartbeat, a job that never runs raises no alarm at all"
        )
    return "\n".join(parts)
