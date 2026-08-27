"""Semantic engine.

Bounded judgment with NEGATIVE AUTHORITY ONLY. It can veto a setup; it can never
create one, and it never touches position sizing. A hallucinated size costs
money; a hallucinated veto costs an opportunity, which is the survivable
direction to be wrong in.

No numerical sentiment score. LLM sentiment floats are not calibrated, drift
with prompt phrasing, and generic sentiment is already in the price. What the
model is asked for instead is categorical event judgment -- the thing it is
actually good at (README section 6).

Earnings avoidance is a calendar lookup and a hard rule, applied before a single
token is spent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Protocol

from ..core.models import EventFlags

log = logging.getLogger("trading_bot.semantic")

MODEL = "claude-opus-5"

SYSTEM = """You screen swing-trade setups for event risk.

A technical setup has already been identified by a separate system. Your only \
job is to decide whether something in the news makes holding this name for the \
next few weeks unwise. You cannot approve a trade and you are not asked to.

Judge two things:

1. structural_invalidation -- has something broken the premise of owning this? \
Guidance withdrawn or cut, accounting or fraud allegations, CEO or CFO \
departure under duress, major dilution, regulatory action, a failed acquisition \
the price had assumed. Ordinary bad news, analyst downgrades, and broad market \
weakness are NOT structural invalidation.

2. binary_event_in_window -- is there a scheduled event in the next few weeks \
whose outcome is a coin flip that gaps the stock? Earnings, an FDA decision, a \
court ruling, a merger vote. A gap makes a stop meaningless, which breaks the \
position sizing entirely.

Be conservative about invalidation and liberal about flagging binary events. \
Missing a trade is cheap; holding through a gap is not. If the news is thin or \
routine, say so and return both flags false."""

SCHEMA = {
    "type": "object",
    "properties": {
        "structural_invalidation": {"type": "boolean"},
        "binary_event_in_window": {"type": "boolean"},
        "event_type": {
            "type": ["string", "null"],
            "description": "earnings | fda | legal | merger | regulatory | other",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {
            "type": "string",
            "description": "One sentence. Cite the specific item, not a summary.",
        },
    },
    "required": [
        "structural_invalidation",
        "binary_event_in_window",
        "event_type",
        "confidence",
        "rationale",
    ],
    "additionalProperties": False,
}


class SemanticEngine(Protocol):
    def assess(self, tickers: list[str], day: date) -> dict[str, EventFlags]: ...

    def earnings_within(
        self, tickers: list[str], day: date, days: int
    ) -> dict[str, date]: ...


class NullSemanticEngine:
    """No-op engine. Used by the backtest and the tests.

    The backtest deliberately runs without it: measuring the technical system
    alone is what gives the semantic engine a baseline it must be shown to
    improve on.
    """

    def assess(self, tickers: list[str], day: date) -> dict[str, EventFlags]:
        return {}

    def earnings_within(
        self, tickers: list[str], day: date, days: int
    ) -> dict[str, date]:
        return {}


class StaticEarningsCalendar:
    """Earnings dates from a local JSON file: {"AAPL": "2026-10-29", ...}.

    OPEN ITEM: this is a hard-blocking rule, so it needs a reliable feed. Alpaca
    does not publish an earnings calendar; wire a real provider here before
    trading live. Until then an empty calendar means the rule silently never
    fires, so the loader warns rather than returning quietly.
    """

    def __init__(self, path: str = "config/earnings.json") -> None:
        self.path = path
        self.dates: dict[str, date] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self.dates = {k: date.fromisoformat(v) for k, v in raw.items()}
        else:
            log.warning(
                "no earnings calendar at %s -- the earnings block cannot fire. "
                "This is a hard rule; wire a provider before trading live.",
                path,
            )

    def within(self, tickers: list[str], day: date, days: int) -> dict[str, date]:
        horizon = day + timedelta(days=days)
        return {
            t: self.dates[t]
            for t in tickers
            if t in self.dates and day <= self.dates[t] <= horizon
        }


class ClaudeSemanticEngine:
    """Reads recent headlines per ticker and returns structured event flags."""

    def __init__(
        self,
        calendar: StaticEarningsCalendar | None = None,
        model: str = MODEL,
        lookback_days: int = 3,
        max_headlines: int = 25,
    ) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.calendar = calendar or StaticEarningsCalendar()
        self.lookback_days = lookback_days
        self.max_headlines = max_headlines

    # ------------------------------------------------------------------ #

    def earnings_within(
        self, tickers: list[str], day: date, days: int
    ) -> dict[str, date]:
        return self.calendar.within(tickers, day, days)

    def assess(self, tickers: list[str], day: date) -> dict[str, EventFlags]:
        out: dict[str, EventFlags] = {}
        news = self._headlines(tickers, day)
        for ticker in tickers:
            items = news.get(ticker, [])
            if not items:
                continue  # no news is not a veto
            try:
                out[ticker] = self._assess_one(ticker, items)
            except Exception as exc:  # noqa: BLE001
                # A failed call must not silently approve. Log and leave the
                # ticker unflagged -- the technical filters still apply, and the
                # run log records that judgment was unavailable.
                log.warning("semantic assessment failed for %s: %s", ticker, exc)
        return out

    def _assess_one(self, ticker: str, headlines: list[str]) -> EventFlags:
        joined = "\n".join(f"- {h}" for h in headlines[: self.max_headlines])
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Ticker: {ticker}\n"
                        f"Recent headlines:\n{joined}\n\n"
                        "Assess this setup for event risk."
                    ),
                }
            ],
        )

        if response.stop_reason == "refusal":
            raise RuntimeError(f"refused: {getattr(response, 'stop_details', None)}")

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return EventFlags(
            structural_invalidation=bool(data["structural_invalidation"]),
            binary_event_in_window=bool(data["binary_event_in_window"]),
            event_type=data.get("event_type"),
            confidence=data.get("confidence", "low"),
            rationale=data.get("rationale", "")[:300],
        )

    # ------------------------------------------------------------------ #

    def _headlines(self, tickers: list[str], day: date) -> dict[str, list[str]]:
        try:
            from alpaca.data.historical.news import NewsClient
            from alpaca.data.requests import NewsRequest

            from ..data.alpaca_data import credentials

            key, secret = credentials()
            client = NewsClient(key, secret)
            response = client.get_news(
                NewsRequest(
                    symbols=",".join(tickers),
                    start=day - timedelta(days=self.lookback_days),
                    end=day,
                    limit=50,
                    exclude_contentless=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("news fetch failed: %s", exc)
            return {}

        out: dict[str, list[str]] = {}
        for item in getattr(response, "data", {}).get("news", []) or []:
            for symbol in getattr(item, "symbols", []) or []:
                if symbol in tickers:
                    out.setdefault(symbol, []).append(item.headline)
        return out
