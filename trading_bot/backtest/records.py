"""What a backtest produces.

`TradeRecord` and `ShadowRecord` are the same shapes the live system writes to
the `trades` and `shadow_book` tables, so the attribution code in
`learning/` runs unchanged over either source (README section 10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..core.models import Regime, SetupType


@dataclass(frozen=True)
class TradeRecord:
    ticker: str
    sector: str
    setup_type: SetupType
    regime_at_entry: Regime

    entry_day: date
    entry_price: float
    exit_day: date
    exit_price: float
    qty: int

    initial_stop: float
    target: float
    exit_reason: str

    realized_r: float
    pnl: float
    # MFE/MAE describe the PRICE PATH while the position was held; realized_r
    # describes the actual fill. Slippage means realized_r can sit marginally
    # outside [mae_r, mfe_r]. Keeping execution cost out of the path metrics is
    # what makes the stop/target placement diagnostics readable.
    mfe_r: float
    mae_r: float
    days_held: int
    entry_score: float
    policy_version: str

    # forensic only -- computed after the fact, never available to a decision
    post_exit_r_10d: float | None = None

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass(frozen=True)
class ShadowRecord:
    """A candidate the agent did not take, and what it would have done.

    Without this, filters are unfalsifiable: you cannot know whether a veto
    helped unless you track what the vetoed setups went on to do.
    """

    ticker: str
    sector: str
    setup_type: SetupType
    regime: Regime
    day: date
    score: float
    entry: float
    stop: float
    target: float
    not_taken_reason: str

    # The inputs that produced `score`. Without them a diagnostic can show that
    # ranking is broken but not which heuristic broke it.
    setup_quality: float = 0.0
    reward_risk: float = 0.0
    features: dict = field(default_factory=dict)

    outcome: str = "unresolved"  # target | stop | timeout | unresolved
    hypothetical_r: float | None = None
    days_to_outcome: int | None = None


@dataclass(frozen=True)
class EquityPoint:
    day: date
    equity: float
    cash: float
    positions: int
    heat_pct: float
    regime: Regime


@dataclass
class BacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    shadow: list[ShadowRecord] = field(default_factory=list)
    curve: list[EquityPoint] = field(default_factory=list)
    # (day, benchmark close) over the same window. Without this the headline
    # return is unreadable: a long-only system in a bull market looks brilliant
    # whether or not it has any edge.
    benchmark: list[tuple] = field(default_factory=list)
    starting_equity: float = 0.0
    universe_size: int = 0
    survivorship_warning: bool = True

    # Cash after every position has been liquidated at the end of the run.
    # The curve itself stays pure mark-to-market so that a short run and a long
    # run agree bar for bar; this is where the closing costs land.
    final_cash: float | None = None

    # Mean annualised cash rate over the window. Sharpe is meaningless without
    # it: the same 12% return is a triumph against 0% cash and unremarkable
    # against 5%, and this window spans both.
    avg_cash_rate: float | None = None

    @property
    def final_equity(self) -> float:
        if self.final_cash is not None:
            return round(self.final_cash, 2)
        return self.curve[-1].equity if self.curve else self.starting_equity
