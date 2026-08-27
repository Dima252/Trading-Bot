"""Event-driven backtest.

Replays `decide()` and the constraint layer bar by bar over history. It calls
the SAME `find_setups`, `decide`, and `ConstraintLayer` that live trading calls
-- if those ever diverge, the backtest is fiction (README section 10).

Order of operations within a day mirrors the live schedule:

    open      resting limit orders from yesterday may fill
    intraday  broker OCO legs may take positions out
    15:30     decide() runs on data through today's close, and acts

No LLM here. The backtest measures the technical system alone, which is exactly
the baseline the semantic engine must later be shown to improve on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from ..core.constraints import ConstraintLayer
from ..core.decide import decide
from ..core.models import (
    ActionKind,
    Candidate,
    EntryType,
    MarketContext,
    Portfolio,
    Position,
    Regime,
    SetupType,
)
from ..core.policy import Policy
from ..core.scoring import rank_candidates
from ..data.models import BarSeries
from ..signals.engine import Indicators, scan
from ..signals.regime import breadth_of, classify
from .fills import FillModel, check_exit, check_exit_intraday, check_limit_fill
from .records import BacktestResult, EquityPoint, ShadowRecord, TradeRecord
from .simulate import simulate_forward


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    starting_equity: float = 100_000.0
    benchmark: str = "SPY"
    slippage_bps: float = 5.0
    commission_per_share: float = 0.0
    shadow_max_days: int = 40
    record_shadow: bool = True


@dataclass
class _SimPosition:
    ticker: str
    sector: str
    setup_type: SetupType
    qty: int
    entry_price: float
    entry_day: date
    stop: float
    initial_stop: float
    target: float
    entry_score: float
    atr: float | None
    regime_at_entry: Regime
    policy_version: str
    last_price: float
    highest: float
    lowest: float
    planned_risk: float = 0.0
    opened_today: bool = False

    @property
    def initial_risk(self) -> float:
        if self.planned_risk > 0:
            return self.planned_risk
        return max(self.entry_price - self.initial_stop, 1e-9)

    def mark(self, high: float, low: float, close: float) -> None:
        self.highest = max(self.highest, high)
        self.lowest = min(self.lowest, low)
        self.last_price = close

    def to_core(self, as_of: date) -> Position:
        return Position(
            ticker=self.ticker,
            qty=self.qty,
            entry_price=self.entry_price,
            current_price=self.last_price,
            stop=self.stop,
            initial_stop=self.initial_stop,
            target=self.target,
            sector=self.sector,
            setup_type=self.setup_type,
            entry_score=self.entry_score,
            opened_at=self.entry_day,
            atr=self.atr,
            policy_version=self.policy_version,
            planned_risk=self.planned_risk or None,
        )


class Backtest:
    def __init__(
        self,
        universe: dict[str, BarSeries],
        sectors: dict[str, str],
        config: BacktestConfig,
        policy: Policy,
    ) -> None:
        if config.benchmark not in universe:
            raise ValueError(f"benchmark {config.benchmark!r} missing from universe")
        self.universe = universe
        self.sectors = sectors
        self.config = config
        self.policy = policy
        self.fills = FillModel(config.slippage_bps, config.commission_per_share)

        # Tradeable names exclude the benchmark itself.
        self.tradeable = {
            s: b for s, b in universe.items() if s != config.benchmark
        }
        self.indicators: dict[str, Indicators] = {
            s: Indicators.compute(b) for s, b in universe.items()
        }

        self.cash = config.starting_equity
        self._regime = Regime.TREND
        self.aborted_fills = 0
        self.positions: dict[str, _SimPosition] = {}
        self.pending: list = []
        self.result = BacktestResult(
            starting_equity=config.starting_equity,
            universe_size=len(self.tradeable),
        )

    # ------------------------------------------------------------------ #

    def run(self) -> BacktestResult:
        benchmark = self.universe[self.config.benchmark]
        days = [
            d
            for d in benchmark.days
            if self.config.start <= d <= self.config.end
        ]

        for day in days:
            self._fill_resting_orders(day)
            self._process_exits(day)
            context = self._context(day, benchmark)
            # set before acting: positions opened today are stamped with today's
            # regime, not yesterday's
            self._regime = context.regime
            self._think(day, context)
            self._record_equity(day, context)

        self._close_out(days[-1] if days else self.config.end)
        self.result.final_cash = self.cash
        self._attach_post_exit_paths()
        return self.result

    # ------------------------------------------------------------------ #

    def _fill_resting_orders(self, day: date) -> None:
        """DAY orders: they fill today or they die."""
        for action in self.pending:
            series = self.tradeable.get(action.ticker)
            i = series.index_of(day) if series else None
            if i is None:
                continue
            price = check_limit_fill(series[i], action.limit)
            if price is None:
                continue
            self._open(action, self.fills.buy(price), day, opened_today=True)
        self.pending = []

    def _process_exits(self, day: date) -> None:
        for ticker, pos in list(self.positions.items()):
            series = self.tradeable[ticker]
            i = series.index_of(day)
            if i is None:  # halted or delisted -- carry at the last known price
                continue
            bar = series[i]
            pos.mark(bar.high, bar.low, bar.close)

            checker = check_exit_intraday if pos.opened_today else check_exit
            fill = checker(bar, pos.stop, pos.target)
            if fill is not None:
                self._close(pos, self.fills.sell(fill.price), day, fill.reason)
            pos.opened_today = False

    def _context(self, day: date, benchmark: BarSeries) -> MarketContext:
        return classify(
            benchmark,
            day,
            self.indicators[self.config.benchmark],
            breadth=breadth_of(self.tradeable, day),
        )

    def _think(self, day: date, context: MarketContext) -> None:
        portfolio = self._portfolio(day)
        candidates = scan(
            self.tradeable, day, self.sectors, self.indicators, self.policy
        )

        actions = decide(portfolio, candidates, context, self.policy)
        verdict = ConstraintLayer(self.policy, sectors=self.sectors).validate(
            actions, portfolio
        )

        opened: set[str] = set()
        for action in verdict.approved:
            if action.kind is ActionKind.CLOSE:
                pos = self.positions.get(action.ticker)
                if pos is not None:
                    self._close(
                        pos, self.fills.sell(pos.last_price), day, action.reason
                    )
            elif action.kind is ActionKind.ADJUST_STOP:
                pos = self.positions.get(action.ticker)
                if pos is not None and action.stop is not None:
                    pos.stop = action.stop
            elif action.kind is ActionKind.OPEN:
                if action.entry_type is EntryType.RESTING_LIMIT:
                    self.pending.append(action)
                else:
                    self._open(action, self.fills.buy(action.limit), day)
                opened.add(action.ticker)

        if self.config.record_shadow:
            self._record_shadow(day, context, candidates, verdict, opened)

    # ------------------------------------------------------------------ #

    def _portfolio(self, day: date) -> Portfolio:
        market_value = sum(p.qty * p.last_price for p in self.positions.values())
        return Portfolio(
            positions=[p.to_core(day) for p in self.positions.values()],
            cash=self.cash,
            equity=self.cash + market_value,
        )

    def _open(
        self, action, fill_price: float, day: date, opened_today: bool = False
    ) -> None:
        cost = action.qty * fill_price + self.fills.commission(action.qty)
        if action.qty <= 0 or cost > self.cash:
            return

        # A resting limit can fill on a gap far below where it was placed --
        # potentially below its own stop. Such a bracket would be flushed the
        # instant it reached the broker, so the position is never taken. Without
        # this guard the exit checker "sells at the stop" while the market sits
        # well below it, manufacturing profit out of a gap DOWN.
        if action.stop is not None and fill_price <= action.stop:
            self.aborted_fills += 1
            return
        self.cash -= cost
        self.positions[action.ticker] = _SimPosition(
            ticker=action.ticker,
            sector=self.sectors.get(action.ticker, "UNKNOWN"),
            setup_type=action.setup_type or SetupType.BREAKOUT,
            qty=action.qty,
            entry_price=fill_price,
            entry_day=day,
            stop=action.stop,
            initial_stop=action.stop,
            target=action.target,
            entry_score=action.score,
            atr=action.atr,
            regime_at_entry=self._regime,
            policy_version=action.policy_version,
            last_price=fill_price,
            highest=fill_price,
            lowest=fill_price,
            planned_risk=max((action.limit or fill_price) - action.stop, 0.0),
            opened_today=opened_today,
        )

    def _close(
        self, pos: _SimPosition, fill_price: float, day: date, reason: str
    ) -> None:
        proceeds = pos.qty * fill_price - self.fills.commission(pos.qty)
        self.cash += proceeds
        risk = pos.initial_risk

        self.result.trades.append(
            TradeRecord(
                ticker=pos.ticker,
                sector=pos.sector,
                setup_type=pos.setup_type,
                regime_at_entry=pos.regime_at_entry,
                entry_day=pos.entry_day,
                entry_price=round(pos.entry_price, 4),
                exit_day=day,
                exit_price=round(fill_price, 4),
                qty=pos.qty,
                initial_stop=pos.initial_stop,
                target=pos.target,
                exit_reason=reason,
                realized_r=round((fill_price - pos.entry_price) / risk, 3),
                pnl=round((fill_price - pos.entry_price) * pos.qty, 2),
                mfe_r=round((pos.highest - pos.entry_price) / risk, 3),
                mae_r=round((pos.lowest - pos.entry_price) / risk, 3),
                days_held=(day - pos.entry_day).days,
                entry_score=pos.entry_score,
                policy_version=pos.policy_version,
            )
        )
        self.positions.pop(pos.ticker, None)

    # ------------------------------------------------------------------ #

    def _record_equity(self, day: date, context: MarketContext) -> None:
        benchmark = self.universe[self.config.benchmark]
        i = benchmark.index_asof(day)
        if i is not None:
            self.result.benchmark.append((day, benchmark[i].close))

        portfolio = self._portfolio(day)
        self.result.curve.append(
            EquityPoint(
                day=day,
                equity=round(portfolio.equity, 2),
                cash=round(self.cash, 2),
                positions=len(self.positions),
                heat_pct=round(portfolio.open_heat_pct, 4),
                regime=context.regime,
            )
        )

    def _record_shadow(
        self,
        day: date,
        context: MarketContext,
        candidates: list[Candidate],
        verdict,
        opened: set[str],
    ) -> None:
        rejected = {r.action.ticker: r.rule for r in verdict.rejected}
        ranked = rank_candidates(candidates, context, self.policy)

        for cand in ranked:
            if cand.ticker in opened:
                # Recorded too, under its own reason. The scoring diagnostic
                # needs the high end of the range, and a forward simulation is
                # the only outcome measure comparable across taken and untaken.
                reason = "taken"
            elif cand.ticker in self.positions:
                reason = "already_held"
            elif cand.ticker in rejected:
                reason = rejected[cand.ticker]
            elif cand.event_flags.penalty >= 1.0:
                reason = "event_veto"
            elif cand.score < self.policy.min_candidate_score:
                reason = "below_score_floor"
            else:
                reason = "ranked_out"

            series = self.tradeable[cand.ticker]
            i = series.index_of(day)
            outcome = (
                simulate_forward(
                    series,
                    i,
                    cand.entry,
                    cand.stop,
                    cand.target,
                    self.config.shadow_max_days,
                )
                if i is not None
                else None
            )

            self.result.shadow.append(
                ShadowRecord(
                    ticker=cand.ticker,
                    sector=cand.sector,
                    setup_type=cand.setup_type,
                    regime=context.regime,
                    day=day,
                    score=round(cand.score, 2),
                    entry=cand.entry,
                    stop=cand.stop,
                    target=cand.target,
                    not_taken_reason=reason,
                    setup_quality=cand.setup_quality,
                    reward_risk=round(cand.reward_risk, 3),
                    features=dict(cand.features),
                    outcome=outcome.outcome if outcome else "unresolved",
                    hypothetical_r=outcome.r_multiple if outcome else None,
                    days_to_outcome=outcome.days if outcome else None,
                )
            )

    def _close_out(self, day: date) -> None:
        """Mark remaining positions out at the last price so the equity curve and
        the trade list agree."""
        for pos in list(self.positions.values()):
            self._close(pos, self.fills.sell(pos.last_price), day, "end_of_backtest")

    def _attach_post_exit_paths(self) -> None:
        """Forensic only: what the name did for 10 days AFTER we were out.

        Reveals targets that exit too early. Never available to a decision.
        """
        updated = []
        for trade in self.result.trades:
            series = self.tradeable.get(trade.ticker)
            i = series.index_of(trade.exit_day) if series else None
            risk = max(trade.entry_price - trade.initial_stop, 1e-9)
            post = None
            if i is not None and i + 10 < len(series):
                post = round((series[i + 10].close - trade.exit_price) / risk, 3)
            updated.append(replace(trade, post_exit_r_10d=post))
        self.result.trades = updated


def run_backtest(
    universe: dict[str, BarSeries],
    sectors: dict[str, str],
    config: BacktestConfig,
    policy: Policy | None = None,
) -> BacktestResult:
    return Backtest(universe, sectors, config, policy or Policy()).run()
