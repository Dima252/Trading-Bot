"""Database schema.

The database holds INTENT and MEMORY. It never holds quantities, cash or fills
as truth -- Alpaca owns those, and anything here that the broker does not
confirm is a bug you want to see immediately (README section 8).

`positions` is therefore an annotations table: why we entered, what the model
said, which policy produced it. It is joined against live broker positions at
runtime and never consulted for size.
"""

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Nightly scan output. One row per candidate per day.
CREATE TABLE IF NOT EXISTS candidates (
    day           TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    setup_type    TEXT NOT NULL,
    entry_type    TEXT NOT NULL,
    entry         REAL NOT NULL,
    stop          REAL NOT NULL,
    target        REAL NOT NULL,
    sector        TEXT NOT NULL,
    setup_quality REAL NOT NULL,
    score         REAL NOT NULL,
    features      TEXT,
    event_flags   TEXT,
    status        TEXT NOT NULL DEFAULT 'Pending',
    status_reason TEXT,
    policy_version TEXT NOT NULL,
    PRIMARY KEY (day, ticker, setup_type)
);
CREATE INDEX IF NOT EXISTS idx_candidates_day_status
    ON candidates(day, status);

-- Annotations on live holdings.
--
-- entry_price and entry_qty are SNAPSHOTS taken at fill, not authority: while
-- the position is live, quantity and average price come from the broker. They
-- exist because a position that closes overnight is gone from the broker by the
-- time anything runs, and a trade cannot be reconstructed without them.
CREATE TABLE IF NOT EXISTS positions (
    ticker         TEXT PRIMARY KEY,
    sector         TEXT NOT NULL,
    setup_type     TEXT NOT NULL,
    entry_score    REAL NOT NULL,
    thesis         TEXT,
    entry_price    REAL NOT NULL DEFAULT 0,
    entry_qty      INTEGER NOT NULL DEFAULT 0,
    initial_stop   REAL NOT NULL,
    target         REAL NOT NULL,
    atr            REAL,
    opened_at      TEXT NOT NULL,
    regime_at_entry TEXT,
    policy_version TEXT NOT NULL
);

-- Every action the agent emitted, including the ones the constitution vetoed.
CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL,
    day            TEXT NOT NULL,
    kind           TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    qty            INTEGER,
    limit_price    REAL,
    stop_price     REAL,
    target_price   REAL,
    score          REAL,
    reason         TEXT,
    approved       INTEGER NOT NULL,
    rejected_rule  TEXT,
    rejected_detail TEXT,
    policy_version TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_day ON decisions(day);

-- Submitted orders. Status is driven by reconciliation, never by the fact
-- that we sent a request.
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    day             TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    limit_price     REAL,
    stop_price      REAL,
    target_price    REAL,
    status          TEXT NOT NULL,
    filled_qty      INTEGER DEFAULT 0,
    filled_avg_price REAL,
    submitted_at    TEXT NOT NULL,
    updated_at      TEXT,
    policy_version  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Closed trades, with the forensics the learning layer needs.
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    sector          TEXT NOT NULL,
    setup_type      TEXT NOT NULL,
    regime_at_entry TEXT,
    entry_day       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_day        TEXT NOT NULL,
    exit_price      REAL NOT NULL,
    qty             INTEGER NOT NULL,
    initial_stop    REAL NOT NULL,
    target          REAL NOT NULL,
    exit_reason     TEXT NOT NULL,
    realized_r      REAL NOT NULL,
    pnl             REAL NOT NULL,
    mfe_r           REAL,
    mae_r           REAL,
    days_held       INTEGER,
    entry_score     REAL,
    post_exit_r_10d REAL,
    policy_version  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_setup ON trades(setup_type, regime_at_entry);

-- Candidates we did NOT take, and what they went on to do. Without this,
-- every filter in the system is unfalsifiable.
CREATE TABLE IF NOT EXISTS shadow_book (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    day              TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    sector           TEXT NOT NULL,
    setup_type       TEXT NOT NULL,
    regime           TEXT,
    score            REAL NOT NULL,
    entry            REAL NOT NULL,
    stop             REAL NOT NULL,
    target           REAL NOT NULL,
    not_taken_reason TEXT NOT NULL,
    outcome          TEXT DEFAULT 'unresolved',
    hypothetical_r   REAL,
    days_to_outcome  INTEGER,
    resolved_at      TEXT,
    UNIQUE (day, ticker, setup_type)
);
CREATE INDEX IF NOT EXISTS idx_shadow_unresolved
    ON shadow_book(outcome) WHERE outcome = 'unresolved';

-- Versioned config. Every trade references the version that produced it.
CREATE TABLE IF NOT EXISTS policy_versions (
    version    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    note       TEXT
);

-- Proposed adaptations. Nothing changes silently.
CREATE TABLE IF NOT EXISTS strategy_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_at  TEXT NOT NULL,
    field        TEXT NOT NULL,
    from_value   TEXT,
    to_value     TEXT,
    evidence     TEXT NOT NULL,
    sample_size  INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'proposed',
    decided_at   TEXT
);

-- Job execution log. Answers "did the 18:00 job actually run last night?"
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job        TEXT NOT NULL,
    day        TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    status     TEXT NOT NULL DEFAULT 'running',
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job_day ON runs(job, day);

-- Daily equity snapshots. The broker reports yesterday's equity but not last
-- week's, and the weekly circuit breaker needs a baseline.
CREATE TABLE IF NOT EXISTS equity_history (
    day       TEXT PRIMARY KEY,
    cash      REAL NOT NULL,
    equity    REAL NOT NULL,
    positions INTEGER NOT NULL DEFAULT 0,
    heat_pct  REAL,
    regime    TEXT
);

-- Operational flags: the kill switch lives here.
CREATE TABLE IF NOT EXISTS flags (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
