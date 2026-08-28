"""Domain models for the decision core.

These are plain dataclasses with no I/O. Everything the agent reasons about is
expressed here, which is what lets `decide()` stay a pure function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class ActionKind(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    TRIM = "TRIM"
    ADD = "ADD"
    ADJUST_STOP = "ADJUST_STOP"
    CANCEL = "CANCEL"
    # No HOLD: holding is the absence of an action, not one of them.

    @property
    def increases_risk(self) -> bool:
        """Risk-increasing actions are the only ones the constitution may veto.

        Defensive actions must never be blocked -- a constraint layer that can
        stop the agent from closing a position is a liability, not a safeguard.
        """
        return self in (ActionKind.OPEN, ActionKind.ADD)


class SetupType(str, Enum):
    BREAKOUT = "breakout"
    PULLBACK = "pullback"
    MEAN_REVERSION = "mean_reversion"


class EntryType(str, Enum):
    CLOSE_CONFIRM = "close_confirm"
    RESTING_LIMIT = "resting_limit"


class Regime(str, Enum):
    TREND = "trend"
    CHOP = "chop"
    HIGH_VOL = "high_vol"


@dataclass(frozen=True)
class EventFlags:
    """Structured output of the semantic engine. Negative authority only."""

    structural_invalidation: bool = False
    binary_event_in_window: bool = False
    event_type: str | None = None
    event_date: date | None = None
    confidence: str = "low"
    rationale: str = ""

    @property
    def penalty(self) -> float:
        """0.0 (clean) to 1.0 (disqualifying)."""
        if self.structural_invalidation:
            return 1.0
        if self.binary_event_in_window:
            return 1.0 if self.confidence == "high" else 0.5
        return 0.0


@dataclass(frozen=True)
class Position:
    """A live holding, joined from broker truth + database annotations."""

    ticker: str
    qty: int
    entry_price: float
    current_price: float
    stop: float
    initial_stop: float
    target: float
    sector: str
    setup_type: SetupType
    entry_score: float
    opened_at: date
    thesis: str = ""
    policy_version: str = "unknown"
    atr: float | None = None
    event_flags: EventFlags = field(default_factory=EventFlags)

    # (planned entry - initial stop) as it stood when the order was sized.
    # R is denominated in the risk we BUDGETED, not in whatever the fill turned
    # out to be -- otherwise a bad fill silently rescales every R statistic, and
    # a fill below the stop divides by zero.
    planned_risk: float | None = None

    @property
    def initial_risk_per_share(self) -> float:
        """R is always denominated in the ORIGINAL risk, not the trailed stop."""
        if self.planned_risk and self.planned_risk > 0:
            return self.planned_risk
        return max(self.entry_price - self.initial_stop, 1e-9)

    @property
    def unrealized_r(self) -> float:
        return (self.current_price - self.entry_price) / self.initial_risk_per_share

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def risk_dollars(self) -> float:
        """Open risk to the CURRENT stop. Never negative: a stop above the
        market is locked-in profit, not negative exposure."""
        return self.qty * max(0.0, self.current_price - self.stop)

    def days_held(self, as_of: date) -> int:
        return (as_of - self.opened_at).days


@dataclass(frozen=True)
class Portfolio:
    """Broker truth. Exposure figures are derived, never stored, so they cannot
    drift from the position list."""

    positions: list[Position] = field(default_factory=list)
    cash: float = 0.0
    equity: float = 0.0
    day_pnl_pct: float = 0.0
    week_pnl_pct: float = 0.0
    halted: bool = False

    @property
    def tickers(self) -> set[str]:
        return {p.ticker for p in self.positions}

    @property
    def open_heat_dollars(self) -> float:
        return sum(p.risk_dollars for p in self.positions)

    @property
    def open_heat_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        return self.open_heat_dollars / self.equity

    @property
    def sector_exposure(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.sector] = out.get(p.sector, 0.0) + p.market_value
        return out

    def position_for(self, ticker: str) -> Position | None:
        return next((p for p in self.positions if p.ticker == ticker), None)


@dataclass(frozen=True)
class Candidate:
    """A setup emitted by the signal engine.

    `setup_quality` is the engine's own 0-100 read on the pattern. The composite
    `score` is computed by `scoring.py` so that the tunable weights live in
    policy, where the adaptive layer can reach them.
    """

    ticker: str
    setup_type: SetupType
    entry_type: EntryType
    entry: float
    stop: float
    target: float
    sector: str
    setup_quality: float
    atr: float | None = None
    features: dict = field(default_factory=dict)
    event_flags: EventFlags = field(default_factory=EventFlags)
    score: float = 0.0

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop

    @property
    def reward_risk(self) -> float:
        if self.risk_per_share <= 0:
            return 0.0
        return (self.target - self.entry) / self.risk_per_share

    @property
    def is_valid(self) -> bool:
        return self.entry > 0 and self.stop < self.entry < self.target


@dataclass(frozen=True)
class MarketContext:
    as_of: date
    regime: Regime = Regime.TREND
    breadth: float = 0.5
    volatility_pct: float = 0.15


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    ticker: str
    qty: int = 0
    limit: float | None = None
    stop: float | None = None
    target: float | None = None
    entry_type: EntryType | None = None
    reason: str = ""
    score: float = 0.0
    policy_version: str = "unknown"

    # Carried through to the position so attribution can group by setup, and so
    # the trailing stop has an ATR to work with. Inferring either downstream
    # loses information the decision already had.
    setup_type: SetupType | None = None
    atr: float | None = None


@dataclass(frozen=True)
class Rejection:
    action: Action
    rule: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    """Output of the constraint layer: what survived, and why the rest didn't."""

    approved: list[Action] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
