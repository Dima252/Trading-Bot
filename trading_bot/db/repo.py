"""Repository over the SQLite state store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ..core.models import (
    Action,
    Candidate,
    EntryType,
    EventFlags,
    Position,
    Rejection,
    SetupType,
)
from .schema import SCHEMA

HALT_FLAG = "HALT"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Repo:
    def __init__(self, path: str | Path = "data/state.db") -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Repo:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- runs

    def start_run(self, job: str, day: date) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (job, day, started_at) VALUES (?,?,?)",
            (job, day.isoformat(), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at=?, status=?, detail=? WHERE id=?",
            (_now(), status, detail[:2000], run_id),
        )
        self.conn.commit()

    def last_run(self, job: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE job=? ORDER BY id DESC LIMIT 1", (job,)
        ).fetchone()

    # ---------------------------------------------------------- candidates

    def save_candidates(
        self, day: date, candidates: list[Candidate], policy_version: str
    ) -> int:
        rows = [
            (
                day.isoformat(),
                c.ticker,
                c.setup_type.value,
                c.entry_type.value,
                c.entry,
                c.stop,
                c.target,
                c.sector,
                c.setup_quality,
                c.score,
                json.dumps(c.features),
                json.dumps(_flags_to_dict(c.event_flags)),
                "Pending",
                None,
                policy_version,
            )
            for c in candidates
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def latest_candidate_day(self, on_or_before: date | None = None) -> date | None:
        """The most recent day the evening job wrote a watchlist for.

        The morning jobs act on the PREVIOUS session's candidates, so they must
        not assume today's date has any.
        """
        sql = "SELECT MAX(day) AS d FROM candidates"
        params: list = []
        if on_or_before:
            sql += " WHERE day <= ?"
            params.append(on_or_before.isoformat())
        row = self.conn.execute(sql, params).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def pending_candidates(self, day: date) -> list[Candidate]:
        rows = self.conn.execute(
            "SELECT * FROM candidates WHERE day=? AND status='Pending' "
            "ORDER BY score DESC",
            (day.isoformat(),),
        ).fetchall()
        return [_row_to_candidate(r) for r in rows]

    def set_candidate_status(
        self,
        day: date,
        ticker: str,
        status: str,
        reason: str = "",
        setup_type: str | None = None,
    ) -> None:
        sql = "UPDATE candidates SET status=?, status_reason=? WHERE day=? AND ticker=?"
        params: list = [status, reason[:500], day.isoformat(), ticker]
        if setup_type:
            sql += " AND setup_type=?"
            params.append(setup_type)
        self.conn.execute(sql, params)
        self.conn.commit()

    def update_candidate_flags(
        self, day: date, ticker: str, flags: EventFlags
    ) -> None:
        self.conn.execute(
            "UPDATE candidates SET event_flags=? WHERE day=? AND ticker=?",
            (json.dumps(_flags_to_dict(flags)), day.isoformat(), ticker),
        )
        self.conn.commit()

    # ----------------------------------------------------------- positions

    def save_position_annotation(
        self,
        ticker: str,
        sector: str,
        setup_type: SetupType,
        entry_score: float,
        initial_stop: float,
        target: float,
        opened_at: date,
        policy_version: str,
        entry_price: float = 0.0,
        entry_qty: int = 0,
        thesis: str = "",
        atr: float | None = None,
        regime_at_entry: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticker,
                sector,
                setup_type.value,
                entry_score,
                thesis,
                entry_price,
                entry_qty,
                initial_stop,
                target,
                atr,
                opened_at.isoformat(),
                regime_at_entry,
                policy_version,
            ),
        )
        self.conn.commit()

    def position_annotations(self) -> dict[str, sqlite3.Row]:
        return {
            r["ticker"]: r
            for r in self.conn.execute("SELECT * FROM positions").fetchall()
        }

    def drop_position_annotation(self, ticker: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
        self.conn.commit()

    # ----------------------------------------------------------- decisions

    def record_decisions(
        self,
        run_id: int,
        day: date,
        approved: list[Action],
        rejected: list[Rejection],
    ) -> None:
        rows = [
            (
                run_id,
                day.isoformat(),
                a.kind.value,
                a.ticker,
                a.qty,
                a.limit,
                a.stop,
                a.target,
                a.score,
                a.reason[:500],
                1,
                None,
                None,
                a.policy_version,
            )
            for a in approved
        ] + [
            (
                run_id,
                day.isoformat(),
                r.action.kind.value,
                r.action.ticker,
                r.action.qty,
                r.action.limit,
                r.action.stop,
                r.action.target,
                r.action.score,
                r.action.reason[:500],
                0,
                r.rule,
                r.detail[:500],
                r.action.policy_version,
            )
            for r in rejected
        ]
        self.conn.executemany(
            "INSERT INTO decisions (run_id, day, kind, ticker, qty, limit_price, "
            "stop_price, target_price, score, reason, approved, rejected_rule, "
            "rejected_detail, policy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    # -------------------------------------------------------------- orders

    def record_order(
        self,
        client_order_id: str,
        day: date,
        ticker: str,
        side: str,
        qty: int,
        status: str,
        policy_version: str,
        broker_order_id: str | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders (client_order_id, broker_order_id, day, "
            "ticker, side, qty, limit_price, stop_price, target_price, status, "
            "submitted_at, updated_at, policy_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                client_order_id,
                broker_order_id,
                day.isoformat(),
                ticker,
                side,
                qty,
                limit_price,
                stop_price,
                target_price,
                status,
                _now(),
                _now(),
                policy_version,
            ),
        )
        self.conn.commit()

    def update_order_status(
        self,
        client_order_id: str,
        status: str,
        filled_qty: int = 0,
        filled_avg_price: float | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE orders SET status=?, filled_qty=?, filled_avg_price=?, "
            "updated_at=? WHERE client_order_id=?",
            (status, filled_qty, filled_avg_price, _now(), client_order_id),
        )
        self.conn.commit()

    def open_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN "
            "('Submitted','PartiallyFilled','Accepted','New')"
        ).fetchall()

    def order_exists(self, client_order_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            is not None
        )

    # -------------------------------------------------------------- trades

    def record_trade(self, **kw) -> int:
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        cur = self.conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({marks})", list(kw.values())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def trades(self, limit: int = 1000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades ORDER BY exit_day DESC LIMIT ?", (limit,)
        ).fetchall()

    # --------------------------------------------------------- shadow book

    def record_shadow(self, **kw) -> None:
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        self.conn.execute(
            f"INSERT OR IGNORE INTO shadow_book ({cols}) VALUES ({marks})",
            list(kw.values()),
        )
        self.conn.commit()

    def unresolved_shadow(self, older_than: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM shadow_book WHERE outcome='unresolved' AND day <= ?",
            (older_than.isoformat(),),
        ).fetchall()

    def resolve_shadow(
        self, shadow_id: int, outcome: str, r: float, days: int
    ) -> None:
        self.conn.execute(
            "UPDATE shadow_book SET outcome=?, hypothetical_r=?, days_to_outcome=?, "
            "resolved_at=? WHERE id=?",
            (outcome, r, days, _now(), shadow_id),
        )
        self.conn.commit()

    def shadow_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM shadow_book WHERE outcome != 'unresolved'"
        ).fetchall()

    # -------------------------------------------------------------- policy

    def save_policy(self, version: str, payload: dict, note: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO policy_versions (version, payload, active, "
            "created_at, note) VALUES (?,?,?,?,?)",
            (version, json.dumps(payload), 0, _now(), note),
        )
        self.conn.commit()

    def activate_policy(self, version: str) -> None:
        self.conn.execute("UPDATE policy_versions SET active=0")
        self.conn.execute(
            "UPDATE policy_versions SET active=1 WHERE version=?", (version,)
        )
        self.conn.commit()

    def active_policy(self) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM policy_versions WHERE active=1"
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def propose_change(
        self,
        field: str,
        from_value,
        to_value,
        evidence: str,
        sample_size: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO strategy_changes (proposed_at, field, from_value, "
            "to_value, evidence, sample_size) VALUES (?,?,?,?,?,?)",
            (_now(), field, str(from_value), str(to_value), evidence, sample_size),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def proposals(self, status: str = "proposed") -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM strategy_changes WHERE status=? ORDER BY id DESC",
            (status,),
        ).fetchall()

    # ------------------------------------------------------ equity history

    def record_equity(
        self,
        day: date,
        cash: float,
        equity: float,
        positions: int = 0,
        heat_pct: float | None = None,
        regime: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_history VALUES (?,?,?,?,?,?)",
            (day.isoformat(), cash, equity, positions, heat_pct, regime),
        )
        self.conn.commit()

    def last_regime(self, on_or_before: date) -> str | None:
        """The most recent regime the evening scan actually measured.

        The intraday jobs cannot compute this themselves: at 10:00 today's bar
        does not exist yet, and the regime is a property of settled closes. So
        they read the last one classified from real data rather than assuming.
        Returns None when no scan has ever recorded one -- which callers must
        treat as "do not open", not as "probably fine".
        """
        row = self.conn.execute(
            "SELECT regime FROM equity_history WHERE day <= ? AND regime IS NOT NULL "
            "ORDER BY day DESC LIMIT 1",
            (on_or_before.isoformat(),),
        ).fetchone()
        return row[0] if row else None

    def equity_asof(self, day: date) -> float | None:
        row = self.conn.execute(
            "SELECT equity FROM equity_history WHERE day <= ? ORDER BY day DESC LIMIT 1",
            (day.isoformat(),),
        ).fetchone()
        return row["equity"] if row else None

    def week_pnl_pct(self, day: date, equity_now: float) -> float:
        """Change against the equity of roughly a week ago.

        Returns 0.0 when there is no baseline yet -- a missing history must not
        trip the weekly breaker on day one.
        """
        baseline = self.equity_asof(day - timedelta(days=7))
        if not baseline:
            return 0.0
        return (equity_now - baseline) / baseline

    # ----------------------------------------------------- read-only views

    def equity_history(self, limit: int = 2000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM equity_history ORDER BY day DESC LIMIT ?", (limit,)
        ).fetchall()[::-1]

    def recent_decisions(self, limit: int = 60) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT d.*, r.job FROM decisions d "
            "LEFT JOIN runs r ON r.id = d.run_id "
            "ORDER BY d.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def candidates_on(self, day: date) -> list[sqlite3.Row]:
        """Every candidate for a day, whatever its status -- the cancelled ones
        are half the story."""
        return self.conn.execute(
            "SELECT * FROM candidates WHERE day = ? ORDER BY score DESC",
            (day.isoformat(),),
        ).fetchall()

    def recent_runs(self, limit: int = 40) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --------------------------------------------------------------- flags

    def set_flag(self, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO flags VALUES (?,?,?)", (name, value, _now())
        )
        self.conn.commit()

    def get_flag(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM flags WHERE name=?", (name,)
        ).fetchone()
        return row["value"] if row else None

    def is_halted(self) -> bool:
        return (self.get_flag(HALT_FLAG) or "0") not in ("0", "", "false", "False")

    def set_halt(self, on: bool, reason: str = "") -> None:
        self.set_flag(HALT_FLAG, "1" if on else "0")
        if reason:
            self.set_flag("HALT_REASON", reason)


# ---------------------------------------------------------------------- #


def _flags_to_dict(flags: EventFlags) -> dict:
    data = asdict(flags)
    if data.get("event_date") is not None:
        data["event_date"] = data["event_date"].isoformat()
    return data


def _flags_from_json(raw: str | None) -> EventFlags:
    if not raw:
        return EventFlags()
    data = json.loads(raw)
    if data.get("event_date"):
        data["event_date"] = date.fromisoformat(data["event_date"])
    return EventFlags(**data)


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        ticker=row["ticker"],
        setup_type=SetupType(row["setup_type"]),
        entry_type=EntryType(row["entry_type"]),
        entry=row["entry"],
        stop=row["stop"],
        target=row["target"],
        sector=row["sector"],
        setup_quality=row["setup_quality"],
        features=json.loads(row["features"] or "{}"),
        event_flags=_flags_from_json(row["event_flags"]),
        score=row["score"],
    )


def position_from_annotation(
    row: sqlite3.Row,
    qty: int,
    avg_entry: float,
    current_price: float,
    live_stop: float | None = None,
) -> Position:
    """Join broker truth (qty, prices) with database intent (thesis, setup)."""
    return Position(
        ticker=row["ticker"],
        qty=qty,
        entry_price=avg_entry,
        current_price=current_price,
        stop=live_stop if live_stop is not None else row["initial_stop"],
        initial_stop=row["initial_stop"],
        target=row["target"],
        sector=row["sector"],
        setup_type=SetupType(row["setup_type"]),
        entry_score=row["entry_score"],
        opened_at=date.fromisoformat(row["opened_at"]),
        thesis=row["thesis"] or "",
        policy_version=row["policy_version"],
        atr=row["atr"],
    )
