"""Order identity and construction.

The client order id is deterministic, which is the whole defence against cron
retries and double-fires: the second submission of the same intent is rejected
by the broker rather than doubling the position.
"""

from __future__ import annotations

import re
from datetime import date

from ..core.models import Action, ActionKind

_SAFE = re.compile(r"[^A-Za-z0-9.\-]")
MAX_ID_LEN = 48


def client_order_id(ticker: str, day: date, kind: ActionKind | str) -> str:
    """Deterministic and unique per (ticker, day, intent).

    Re-running the 15:30 job after a crash produces the same id, so Alpaca
    rejects the duplicate instead of buying twice.
    """
    kind_str = kind.value if isinstance(kind, ActionKind) else str(kind)
    raw = f"{_SAFE.sub('-', ticker)}-{day.isoformat()}-{kind_str}"
    return raw[:MAX_ID_LEN]


def validate_bracket(limit: float, stop: float, target: float) -> None:
    """A malformed bracket is rejected by the broker after the money has been
    committed to the entry leg, so check before sending."""
    if not (stop < limit < target):
        raise ValueError(
            f"bracket levels must satisfy stop < entry < target, "
            f"got stop={stop} entry={limit} target={target}"
        )
    if limit <= 0 or stop <= 0:
        raise ValueError("prices must be positive")


def bracket_from_action(action: Action) -> dict:
    """The payload a broker adapter needs to place one bracket order."""
    if action.kind is not ActionKind.OPEN:
        raise ValueError(f"{action.kind} is not an entry")
    if action.limit is None or action.stop is None or action.target is None:
        raise ValueError(f"{action.ticker}: entry action is missing bracket levels")

    validate_bracket(action.limit, action.stop, action.target)
    return {
        "ticker": action.ticker,
        "qty": action.qty,
        "limit": round(action.limit, 2),
        "stop": round(action.stop, 2),
        "target": round(action.target, 2),
    }
