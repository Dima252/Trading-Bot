"""Local bar cache.

A backtest gets run hundreds of times. Hitting the network twice for the same
bar is both slow and non-reproducible -- two runs a week apart must replay
identical data or every comparison between them is meaningless.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Bar, BarSeries

# How far back an incremental refresh re-reads before its last stored bar.
#
# The last cached day is the one most likely to be wrong: if any fetch ran while
# the market was open, that bar's "close" is a mid-session quote. Re-reading it
# is what makes "run fetch again after the bell" actually repair the day. Stores
# are upserts, so the overlap costs bandwidth and nothing else.
OVERLAP_DAYS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT NOT NULL,
    day     TEXT NOT NULL,
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_day ON bars(symbol, day);

CREATE TABLE IF NOT EXISTS bar_meta (
    symbol     TEXT PRIMARY KEY,
    first_day  TEXT NOT NULL,
    last_day   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BarCache:
    def __init__(self, path: str | Path = "data/bars.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BarCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #

    def store(self, series: BarSeries) -> int:
        if not series.bars:
            return 0
        rows = [
            (
                series.symbol,
                b.day.isoformat(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
            )
            for b in series.bars
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO bar_meta VALUES (?,?,?,?)",
            (
                series.symbol,
                series.bars[0].day.isoformat(),
                series.bars[-1].day.isoformat(),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return len(rows)

    def load(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> BarSeries:
        sql = "SELECT day, open, high, low, close, volume FROM bars WHERE symbol = ?"
        params: list = [symbol]
        if start:
            sql += " AND day >= ?"
            params.append(start.isoformat())
        if end:
            sql += " AND day <= ?"
            params.append(end.isoformat())
        sql += " ORDER BY day"

        bars = [
            Bar(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5])
            for r in self.conn.execute(sql, params)
        ]
        return BarSeries(symbol, bars)

    def load_many(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        min_bars: int = 0,
    ) -> dict[str, BarSeries]:
        out = {}
        for symbol in symbols:
            series = self.load(symbol, start, end)
            if len(series) >= min_bars:
                out[symbol] = series
        return out

    # ------------------------------------------------------------------ #

    def symbols(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT symbol FROM bar_meta ORDER BY symbol")]

    def coverage(self, symbol: str) -> tuple[date, date] | None:
        row = self.conn.execute(
            "SELECT first_day, last_day FROM bar_meta WHERE symbol = ?", (symbol,)
        ).fetchone()
        if not row:
            return None
        return date.fromisoformat(row[0]), date.fromisoformat(row[1])

    def stats(self) -> dict[str, int]:
        bars = self.conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        syms = self.conn.execute("SELECT COUNT(*) FROM bar_meta").fetchone()[0]
        return {"symbols": syms, "bars": bars}
