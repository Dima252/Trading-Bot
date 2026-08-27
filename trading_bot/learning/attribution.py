"""Monthly attribution over live trades.

Deliberately reuses the backtest's `TradeRecord` / `ShadowRecord` shapes so the
same analysis runs unchanged over either source. Grouped statistics are
actionable at ~40 samples and they tell you *why*; a model would need three
orders of magnitude more and would not.
"""

from __future__ import annotations

from datetime import date

from ..backtest.metrics import Report, build_report, format_report
from ..backtest.records import BacktestResult, ShadowRecord, TradeRecord
from ..core.models import Regime, SetupType
from ..db.repo import Repo


def _safe_enum(enum_cls, value, fallback):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return fallback


def trades_from_db(repo: Repo, limit: int = 5000) -> list[TradeRecord]:
    out: list[TradeRecord] = []
    for row in repo.trades(limit):
        if row["exit_reason"] and "no_entry_snapshot" in row["exit_reason"]:
            continue  # R is not computable for these; excluding beats distorting
        out.append(
            TradeRecord(
                ticker=row["ticker"],
                sector=row["sector"],
                setup_type=_safe_enum(
                    SetupType, row["setup_type"], SetupType.BREAKOUT
                ),
                regime_at_entry=_safe_enum(
                    Regime, row["regime_at_entry"], Regime.TREND
                ),
                entry_day=date.fromisoformat(row["entry_day"]),
                entry_price=row["entry_price"],
                exit_day=date.fromisoformat(row["exit_day"]),
                exit_price=row["exit_price"],
                qty=row["qty"],
                initial_stop=row["initial_stop"],
                target=row["target"],
                exit_reason=row["exit_reason"],
                realized_r=row["realized_r"],
                pnl=row["pnl"],
                mfe_r=row["mfe_r"] or 0.0,
                mae_r=row["mae_r"] or 0.0,
                days_held=row["days_held"] or 0,
                entry_score=row["entry_score"] or 0.0,
                policy_version=row["policy_version"],
                post_exit_r_10d=row["post_exit_r_10d"],
            )
        )
    return out


def shadow_from_db(repo: Repo) -> list[ShadowRecord]:
    return [
        ShadowRecord(
            ticker=row["ticker"],
            sector=row["sector"],
            setup_type=_safe_enum(SetupType, row["setup_type"], SetupType.BREAKOUT),
            regime=_safe_enum(Regime, row["regime"], Regime.TREND),
            day=date.fromisoformat(row["day"]),
            score=row["score"],
            entry=row["entry"],
            stop=row["stop"],
            target=row["target"],
            not_taken_reason=row["not_taken_reason"],
            outcome=row["outcome"],
            hypothetical_r=row["hypothetical_r"],
            days_to_outcome=row["days_to_outcome"],
        )
        for row in repo.shadow_rows()
    ]


def build_live_report(repo: Repo) -> Report:
    trades = trades_from_db(repo)
    shadow = shadow_from_db(repo)
    curve = repo.conn.execute(
        "SELECT day, cash, equity, positions, heat_pct, regime "
        "FROM equity_history ORDER BY day"
    ).fetchall()

    from ..backtest.records import EquityPoint

    points = [
        EquityPoint(
            day=date.fromisoformat(r["day"]),
            equity=r["equity"],
            cash=r["cash"],
            positions=r["positions"],
            heat_pct=r["heat_pct"] or 0.0,
            regime=_safe_enum(Regime, r["regime"], Regime.TREND),
        )
        for r in curve
    ]

    result = BacktestResult(
        trades=trades,
        shadow=shadow,
        curve=points,
        starting_equity=points[0].equity if points else 0.0,
    )
    if points:
        result.final_cash = points[-1].equity
    return build_report(result)


def format_live_report(repo: Repo) -> str:
    text = format_report(build_live_report(repo))
    return text.replace("BACKTEST REPORT", "LIVE ATTRIBUTION").replace(
        "  NOTE: universe is a fixed present-day list, so these results\n"
        "  carry survivorship bias. Trust RELATIVE comparisons between\n"
        "  setups, regimes and filters; discount absolute returns.",
        "  NOTE: live results. No survivorship bias here -- but small samples\n"
        "  still dominate, so read buckets marked (thin) as noise.",
    )
