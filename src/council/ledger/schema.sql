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
    kind             TEXT NOT NULL CHECK (kind IN ('rebalance', 'flatten', 'compliance', 'smoke')),
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
    superseded_by    TEXT,
    policy_sha       TEXT,             -- policy the decision was made under (schema v3)
    blocker_scope    TEXT              -- 'all' | 'satellite' while waiting_for_market (schema v3)
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

-- ------------------------------------------------------------------ swing book (schema v5, SW-2b)
-- Private rows of the agent-driven swing book (design swing-book.md rev 2). Free text that may copy
-- licensed feed text (thesis, claims, reasons) lives ONLY in swing_ideas.record_json,
-- swing_events.reason / payload_json and paper_trades.record_json; the 7-day purge scrubs those by
-- origin cycle (and every carry-forward cycle of the idea; ledger/purge.py). swing_trades holds no
-- free text, so a closed trade can stay immutable.

CREATE TABLE IF NOT EXISTS swing_ideas (
    idea_id           TEXT PRIMARY KEY,           -- "idea:<id>"
    origin_cycle      TEXT NOT NULL,
    carry_cycles_json TEXT NOT NULL DEFAULT '[]', -- later cycles that carried it forward (<= 7 days)
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('long', 'short')),
    setup             TEXT,
    status            TEXT NOT NULL,              -- scout / wait / rejected / proposed / missed / ...
    record_json       TEXT NOT NULL DEFAULT '{}',  -- the idea, verdicts, votes (private text)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS swing_ideas_by_cycle ON swing_ideas (origin_cycle);

CREATE TABLE IF NOT EXISTS swing_trades (
    trade_id          TEXT PRIMARY KEY,           -- "trade:<id>"
    idea_id           TEXT,
    origin_cycle      TEXT,
    decision_id       TEXT,                       -- the decision holding the entry leg
    entry_seq         INTEGER,                    -- that leg's seq
    ticker            TEXT NOT NULL,
    instrument_id     INTEGER,
    side              TEXT NOT NULL CHECK (side IN ('long', 'short')),
    state             TEXT NOT NULL CHECK (state IN ('proposed', 'entry_executing', 'open', 'open_tp_missing', 'partial', 'entry_unknown', 'missed', 'exit_pending', 'closed_stop', 'closed_target', 'closed_time', 'closed_exit', 'closed_halt', 'closed_external', 'closed_unclassified')),
    position_ids_json TEXT NOT NULL DEFAULT '[]', -- one trade may span several broker positions
    units             REAL,
    open_rate         REAL,
    sl_rate           REAL,
    tp_rate           REAL,
    time_stop_date    TEXT,
    close_rate        REAL,
    opened_at         TEXT,
    closed_at         TEXT,
    detail_json       TEXT NOT NULL DEFAULT '{}',  -- numbers and ids only (never free text)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (decision_id, entry_seq)
);
CREATE INDEX IF NOT EXISTS swing_trades_by_state ON swing_trades (state);

-- A closed (or missed) trade is immutable, even to a writer that bypasses Ledger.
CREATE TRIGGER IF NOT EXISTS swing_trades_terminal_no_update
BEFORE UPDATE ON swing_trades
WHEN OLD.state IN ('closed_exit', 'closed_external', 'closed_halt', 'closed_stop', 'closed_target', 'closed_time', 'closed_unclassified', 'missed')
BEGIN
    SELECT RAISE(ABORT, 'swing trade is terminal');
END;
CREATE TRIGGER IF NOT EXISTS swing_trades_terminal_no_delete
BEFORE DELETE ON swing_trades
WHEN OLD.state IN ('closed_exit', 'closed_external', 'closed_halt', 'closed_stop', 'closed_target', 'closed_time', 'closed_unclassified', 'missed')
BEGIN
    SELECT RAISE(ABORT, 'swing trade is terminal');
END;

CREATE TABLE IF NOT EXISTS swing_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      TEXT,
    idea_id       TEXT,
    origin_cycle  TEXT,
    kind          TEXT NOT NULL,                  -- transition / time_stop_due / earnings_exit_due / ...
    from_state    TEXT,
    to_state      TEXT,
    reason        TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    actor         TEXT NOT NULL DEFAULT 'system',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS swing_events_by_trade ON swing_events (trade_id);
CREATE INDEX IF NOT EXISTS swing_events_by_cycle ON swing_events (origin_cycle);

CREATE TABLE IF NOT EXISTS benchmark_days (
    day              TEXT PRIMARY KEY,            -- US trading date (YYYY-MM-DD)
    sq8_ret          REAL,
    matched_idx_ret  REAL,
    idx_hold_ret     REAL,
    detail_json      TEXT NOT NULL DEFAULT '{}',
    recorded_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    paper_id        TEXT PRIMARY KEY,
    idea_id         TEXT,
    origin_cycle    TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('long', 'short')),
    status          TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    entry_ref       REAL,
    stop_pct        REAL,
    target_pct      REAL,
    time_stop_date  TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    exit_reason     TEXT,
    ret_pct         REAL,
    record_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_trades_by_cycle ON paper_trades (origin_cycle);
