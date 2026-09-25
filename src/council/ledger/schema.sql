-- council-book private ledger (SQLite, WAL). Lives OUTSIDE the repository; never published.
-- Timestamps are UTC ISO-8601 strings with a fixed format, so string order = time order.

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id     TEXT PRIMARY KEY,
    slot         TEXT NOT NULL,
    status       TEXT NOT NULL,
    record_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_calls (
    call_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     TEXT NOT NULL,
    role         TEXT NOT NULL,
    replicate    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    record_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS role_calls_by_cycle ON role_calls (cycle_id);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id      TEXT PRIMARY KEY,
    cycle_id         TEXT,
    kind             TEXT NOT NULL CHECK (kind IN ('rebalance', 'flatten', 'compliance')),
    priority         INTEGER NOT NULL,
    state            TEXT NOT NULL,
    prev_state       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    target_json      TEXT NOT NULL DEFAULT '{}',
    plan_json        TEXT,
    commitment_sha   TEXT,
    published_commit TEXT,
    superseded_by    TEXT
);
CREATE INDEX IF NOT EXISTS decisions_by_state ON decisions (state);

-- actor: who moved the decision (operator, executor, runner, system); process_role: the writing
-- process's COUNCIL_ROLE. Only actor 'operator' may move a decision to 'approved' (schema v2).
CREATE TABLE IF NOT EXISTS decision_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id  TEXT NOT NULL REFERENCES decisions (decision_id),
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT 'system',
    process_role TEXT
);
CREATE INDEX IF NOT EXISTS decision_events_by_decision ON decision_events (decision_id);

CREATE TABLE IF NOT EXISTS legs (
    leg_id            TEXT PRIMARY KEY,
    decision_id       TEXT NOT NULL REFERENCES decisions (decision_id),
    seq               INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    line              TEXT NOT NULL,
    vehicle_symbol    TEXT NOT NULL,
    instrument_id     INTEGER,
    direction         TEXT NOT NULL,
    settlement        TEXT,
    leverage          INTEGER NOT NULL DEFAULT 1,
    units             REAL,
    amount_usd        REAL,
    sl_rate           REAL,
    position_id       INTEGER,
    depends_on_json   TEXT NOT NULL DEFAULT '[]',
    risk_increasing   INTEGER NOT NULL DEFAULT 0,
    attempt           INTEGER NOT NULL DEFAULT 0,
    request_id        TEXT UNIQUE,
    order_id          INTEGER,
    position_ids_json TEXT NOT NULL DEFAULT '[]',
    state             TEXT NOT NULL,
    broker_status     TEXT,
    error             TEXT,
    submitted_at      TEXT,
    resolved_at       TEXT,
    detail_json       TEXT NOT NULL DEFAULT '{}',
    updated_at        TEXT NOT NULL,
    UNIQUE (decision_id, seq)
);
CREATE INDEX IF NOT EXISTS legs_by_state ON legs (state);
CREATE INDEX IF NOT EXISTS legs_by_line ON legs (line);

CREATE TABLE IF NOT EXISTS positions_observed (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at    TEXT NOT NULL,
    decision_id    TEXT,
    source         TEXT NOT NULL DEFAULT '',
    positions_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    decision_id  TEXT,
    seq          INTEGER,
    kind         TEXT NOT NULL,
    request_id   TEXT,
    http_status  INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS broker_events_by_decision ON broker_events (decision_id);
CREATE INDEX IF NOT EXISTS broker_events_by_kind ON broker_events (kind, at);

CREATE TABLE IF NOT EXISTS equity_marks (
    mark_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    equity_usd  REAL NOT NULL,
    credit_usd  REAL,
    flow_usd    REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS equity_marks_by_time ON equity_marks (at);

CREATE TABLE IF NOT EXISTS runtime_state (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
