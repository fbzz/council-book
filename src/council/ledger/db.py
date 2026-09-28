"""The private ledger: one SQLite file (WAL) outside the repository.

Rules:
- Every state change goes through `transition`, which enforces ledger/states.py and is a
  compare-and-set (a concurrent writer cannot move a decision from a state it no longer has).
- `create_decision` applies priority atomically: flatten > compliance > rebalance. A new decision
  supersedes every PENDING decision of equal or lower priority; if a pending decision of HIGHER
  priority exists the new one is refused (PriorityConflict) and nothing is written.
- `expire_stale(now)` is ONE `UPDATE … RETURNING` (no read-then-write race), run eagerly.
- `has_blocker()` is true while any decision is blocked / execution_unknown / waiting_for_market
  or any leg is unknown. `blockers()` names them; a waiting_for_market decision whose waiting legs
  are all stock orders is reported as "satellite:<id>" (it holds only the satellite sleeve), and so
  is such a hold after it times out to blocked (a broken fill resets its scope to `all`).
- Each decision stores the `policy_sha` it was made under (approval re-checks it for rebalances).
- Leg rows are written once per decision (state `planned`) and then only moved along the leg
  state machine; a leg's request_id is UNIQUE across the ledger.
- Only actor "operator" may move a decision to `approved` (ApprovalRefused otherwise). Every
  decision event records its actor (operator, executor, runner, system) and the writing
  process's COUNCIL_ROLE.
- A leg row's line is `Leg.line`; a leg without one falls back to `line_of`, then to
  `vehicle_to_line(universe)`; a symbol that maps to no line is refused unless it is
  `UNMAPPED_<id>` (its own line). Never the vehicle symbol by accident.
- Cycle queries read only resolved legs that moved exposure (filled, partially_filled,
  rejected_partial; never modify_sl): `last_change`, `turnover_since`, `cost_bps_since`,
  `fee_bps_since`, `reference_fills`, `level_resets`, `real_fee_drag`, and `stop_hits_since` from
  `stop_hit` broker events. A leg's origin ("reference" | "discretionary") is kept in its detail;
  a leg written before origins existed counts as discretionary (`origins=` filters). Costs: the
  detail's `cost_bps_nav` is the variable cost and `fee_bps_nav` the private fixed fee;
  `cost_bps_since` sums both unless `include_fee=False`.
- Smoke tickets (m5-readiness §8.1, M-3): every fill-derived cycle query above ignores legs of
  `kind=smoke` decisions (R12 hold timers, held levels, R4d cool-off, turnover, R13/R14 budgets,
  costs, the real fee drag). `smoke_positions` / `smoke_active` say whether a smoke ticket is
  pending or in flight or a smoke-opened position is still open; live cycles refuse to seal a
  proposal meanwhile. Schema v4 admits the `smoke` kind (a v3 ledger's decisions table is rebuilt
  once, rows kept).
- Swing book (schema v5, SW-2b; design swing-book.md rev 2 §4.5): `swing_ideas`, `swing_trades`
  (with `position_ids`), `swing_events`, `benchmark_days`, `paper_trades`. A trade's state moves only
  along `swing.models.TRANSITIONS` (`transition_swing_trade`, compare-and-set, one `swing_events`
  row per move); a closed/missed trade is immutable (API and SQL trigger). `record_swing_fill`
  creates or advances a trade from a FILLED swing open leg whatever its decision's state (blocked,
  execution_unknown, resumed, waiting-for-market). A swing leg is one a trade row points at, or
  whose detail says `sleeve: swing`. Swing-scoped holds are reported by `blockers()` as
  "swing:<id>" (they halt new swing entries only; `swing_blockers()`, `swing_entries_blocked()`).
  v5 only adds tables, indexes and triggers: every older row is kept.
- Broker payloads are stored with credential-like keys redacted. The file never lives in the repo.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from council.clock import utcnow
from council.ledger.states import (
    BLOCKER_SCOPES,
    BLOCKER_STATES,
    DECISION_STATES,
    LEG_ACTIVE_STATES,
    LEG_STATES,
    LEG_TERMINAL_STATES,
    PENDING_STATES,
    PRIORITY,
    SATELLITE_BLOCKER_PREFIX,
    SMOKE_KIND,
    SWING_BLOCKER_PREFIX,
    SWING_BLOCKING_STATES,
    TRADE_CLOSED_STATES,
    TRADE_TERMINAL_STATES,
    WAITING_STATE,
    DecisionKind,
    IllegalTransition,
    can_transition,
    can_transition_leg,
    check_trade_transition,
)
from council.models.broker import Position
from council.models.plan import Leg
from council.paths import assert_outside_repo, state_dir
from council.policy import Universe
from council.risk.nav import NavState

SCHEMA_VERSION = 5
LEDGER_FILE = "ledger.sqlite3"
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_SENSITIVE_KEYS = ("authorization", "api_key", "api-key", "user_key", "user-key", "token", "secret", "password")
# Columns added after v1: (table, column, DDL used by ALTER TABLE on an older ledger).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("decision_events", "actor", "TEXT NOT NULL DEFAULT 'unrecorded'"),
    ("decision_events", "process_role", "TEXT"),
    ("decisions", "policy_sha", "TEXT"),
    ("decisions", "blocker_scope", "TEXT"),
)

OPERATOR_ACTOR = "operator"
SYSTEM_ACTOR = "system"
UNMAPPED_PREFIX = "UNMAPPED_"
# Resolved leg states in which some exposure actually moved.
FILLED_LEG_STATES: tuple[str, ...] = ("filled", "partially_filled", "rejected_partial")
STOP_HIT_EVENT = "stop_hit"
NAV_STATE_KEY = "nav_state"
KILL_STATE_KEY = "kill_state"
SMOKE_BASELINE_RESET_KEY = "smoke_baseline_reset"          # when the post-smoke NAV baseline restarted
MATERIAL_FINGERPRINT_KEY = "last_material_fingerprint"      # legacy single fingerprint (read only)
MATERIAL_FINGERPRINTS_KEY = "last_material_fingerprints"    # per line + "_global" (design §11.4)
KILL_STATES: frozenset[str] = frozenset({"NORMAL", "WARN", "HALTED", "FLAT", "RESUMED"})


class LedgerError(RuntimeError):
    pass


class InvalidTransition(LedgerError):
    pass


class PriorityConflict(LedgerError):
    """A pending decision of higher priority exists; the new one was not created."""


class ApprovalRefused(InvalidTransition):
    """Only the operator may move a decision to `approved`."""


def ts(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime; the ledger stores aware UTC timestamps only")
    return value.astimezone(UTC).strftime(_TS_FORMAT)


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)


def redact(value: Any) -> Any:
    """Drop credential-like keys from a payload before it is stored."""
    if isinstance(value, Mapping):
        return {
            k: "[REDACTED]" if any(s in str(k).lower() for s in _SENSITIVE_KEYS) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _process_role() -> str | None:
    return os.environ.get("COUNCIL_ROLE")


def _check_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise LedgerError("a decision event needs a non-empty actor")
    return actor


def _log_event(
    conn: sqlite3.Connection, decision_id: str, from_state: str | None, to_state: str,
    reason: str, stamp: str, actor: str,
) -> None:
    conn.execute(
        """INSERT INTO decision_events
             (decision_id, from_state, to_state, reason, created_at, actor, process_role)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, from_state, to_state, reason, stamp, actor, _process_role()),
    )


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)


def _same_planned_legs(rows: Sequence[LegRow], legs: Sequence[Leg], lines: Sequence[str]) -> bool:
    """True when stored rows are exactly these legs, all still `planned`."""
    if len(rows) != len(legs):
        return False
    pairs = sorted(zip(legs, lines, strict=True), key=lambda pair: pair[0].seq)
    for row, (leg, line) in zip(rows, pairs, strict=True):
        same = (
            row.state == "planned" and row.seq == leg.seq and row.kind == leg.kind
            and row.line == line and row.vehicle_symbol == leg.symbol
            and row.instrument_id == leg.instrument_id and row.direction == leg.direction
            and row.settlement == leg.settlement and row.leverage == leg.leverage
            and row.position_id == leg.position_id and row.depends_on == list(leg.depends_on)
            and row.risk_increasing == leg.risk_increasing
            and _close(row.units, leg.units) and _close(row.amount_usd, leg.amount_usd)
            and _close(row.sl_rate, leg.sl_rate)
            and _close(row.detail.get("weight_before"), leg.weight_before)
            and _close(row.detail.get("weight_after"), leg.weight_after)
            and row.detail.get("swing_trade_id") == leg.swing_trade_id
            and _close(row.detail.get("tp_rate"), leg.tp_rate)
        )
        if not same:
            return False
    return True


def _swing_detail(leg: Leg) -> dict[str, Any]:
    """The swing marks a swing leg carries in its detail from the moment it is written (SW-5): the
    ledger reads `sleeve` / `swing_trade_id` to scope a hold, and `record_swing_fill` reads
    `tp_rate` / `time_stop_date`. Core legs get none of these keys."""
    if not (leg.sleeve == "swing" or leg.swing_trade_id):
        return {}
    return {"sleeve": "swing", "swing_trade_id": leg.swing_trade_id, "tp_rate": leg.tp_rate,
            "tp_mode": leg.tp_mode, "time_stop_date": leg.time_stop_date}


def leg_origin(detail: Mapping[str, Any]) -> str:
    """A stored leg's origin; legs written before origins existed count as discretionary."""
    return "reference" if detail.get("origin") == "reference" else "discretionary"


def _fill_fraction(state: str, detail: Mapping[str, Any]) -> float:
    """Share of a leg's planned weight change that happened: 1 when filled; for a partial fill
    units_filled / units_sent when both are known, else 1 (conservative for budgets)."""
    if state == "filled":
        return 1.0
    try:
        filled = float(detail.get("units_filled"))  # type: ignore[arg-type]
        sent = float(detail.get("units_sent"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if sent <= 0 or filled < 0:
        return 1.0
    return min(1.0, filled / sent)


@dataclass(frozen=True)
class DecisionRow:
    decision_id: str
    cycle_id: str | None
    kind: str
    priority: int
    state: str
    prev_state: str | None
    created_at: datetime
    updated_at: datetime
    valid_until: datetime
    target: dict[str, Any]
    plan: dict[str, Any] | None
    commitment_sha: str | None
    published_commit: str | None
    superseded_by: str | None
    policy_sha: str | None = None
    blocker_scope: str | None = None


@dataclass(frozen=True)
class LegRow:
    leg_id: str
    decision_id: str
    seq: int
    kind: str
    line: str
    vehicle_symbol: str
    instrument_id: int | None
    direction: str
    settlement: str | None
    leverage: int
    units: float | None
    amount_usd: float | None
    sl_rate: float | None
    position_id: int | None
    depends_on: list[int]
    risk_increasing: bool
    attempt: int
    request_id: str | None
    order_id: int | None
    position_ids: list[int]
    state: str
    broker_status: str | None
    error: str | None
    submitted_at: datetime | None
    resolved_at: datetime | None
    detail: dict[str, Any]
    updated_at: datetime


def _decision(row: sqlite3.Row) -> DecisionRow:
    return DecisionRow(
        decision_id=row["decision_id"], cycle_id=row["cycle_id"], kind=row["kind"],
        priority=row["priority"], state=row["state"], prev_state=row["prev_state"],
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
        valid_until=parse_ts(row["valid_until"]),  # type: ignore[arg-type]
        target=json.loads(row["target_json"] or "{}"),
        plan=json.loads(row["plan_json"]) if row["plan_json"] else None,
        commitment_sha=row["commitment_sha"], published_commit=row["published_commit"],
        superseded_by=row["superseded_by"],
        policy_sha=row["policy_sha"], blocker_scope=row["blocker_scope"],
    )


def _leg(row: sqlite3.Row) -> LegRow:
    return LegRow(
        leg_id=row["leg_id"], decision_id=row["decision_id"], seq=row["seq"], kind=row["kind"],
        line=row["line"], vehicle_symbol=row["vehicle_symbol"],
        instrument_id=row["instrument_id"], direction=row["direction"],
        settlement=row["settlement"], leverage=row["leverage"], units=row["units"],
        amount_usd=row["amount_usd"], sl_rate=row["sl_rate"], position_id=row["position_id"],
        depends_on=json.loads(row["depends_on_json"]), risk_increasing=bool(row["risk_increasing"]),
        attempt=row["attempt"], request_id=row["request_id"], order_id=row["order_id"],
        position_ids=json.loads(row["position_ids_json"]), state=row["state"],
        broker_status=row["broker_status"], error=row["error"],
        submitted_at=parse_ts(row["submitted_at"]), resolved_at=parse_ts(row["resolved_at"]),
        detail=json.loads(row["detail_json"] or "{}"),
        updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
    )


_LEG_FIELDS = frozenset({
    "request_id", "attempt", "order_id", "position_ids", "broker_status", "error",
    "submitted_at", "resolved_at", "detail", "units",
})


def _admit_smoke_kind(conn: sqlite3.Connection, schema: str) -> None:
    """v4: rebuild a pre-v4 `decisions` table whose kind CHECK lacks 'smoke' (SQLite cannot alter a
    CHECK). The documented table-rebuild: foreign keys off, copy every row, swap, check keys."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone()
    if row is None or f"'{SMOKE_KIND}'" in (row["sql"] or ""):
        return
    start = schema.index("CREATE TABLE IF NOT EXISTS decisions (")
    ddl = schema[start:schema.index(");", start) + 2].replace(
        "CREATE TABLE IF NOT EXISTS decisions (", "CREATE TABLE decisions_v4 (")
    columns = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(decisions)"))
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(ddl)
            conn.execute(f"INSERT INTO decisions_v4 ({columns}) SELECT {columns} FROM decisions")
            conn.execute("DROP TABLE decisions")
            conn.execute("ALTER TABLE decisions_v4 RENAME TO decisions")
            conn.execute("CREATE INDEX IF NOT EXISTS decisions_by_state ON decisions (state)")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise LedgerError("foreign key check failed while admitting the smoke kind")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


class Ledger:
    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.path = Path(path)
        assert_outside_repo(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.migrate()

    @classmethod
    def default(cls) -> Ledger:
        return cls(state_dir() / LEDGER_FILE)

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction: all-or-nothing, serialised against other writers."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _now(self, now: datetime | None) -> str:
        return ts(now or self._clock())

    def migrate(self) -> int:
        """Create/upgrade the schema; returns the schema version."""
        schema = resources.files("council.ledger").joinpath("schema.sql").read_text()
        conn = self._connect()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise LedgerError(f"ledger schema {version} is newer than this code ({SCHEMA_VERSION})")
            if version < SCHEMA_VERSION:
                conn.executescript(schema)          # creates whatever is missing (IF NOT EXISTS)
                for table, column, ddl in _ADDED_COLUMNS:
                    present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in present:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                _admit_smoke_kind(conn, schema)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            conn.close()
        return SCHEMA_VERSION

    def journal_mode(self) -> str:
        with self._read() as conn:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]

    # ------------------------------------------------------------------ cycles
    def record_cycle(self, record: BaseModel | Mapping[str, Any], *, now: datetime | None = None) -> None:
        """Insert or update a cycle record (keyed by cycle_id)."""
        data = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
        stamp = self._now(now)
        slot = data["slot"] if isinstance(data["slot"], str) else ts(data["slot"])
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO cycles (cycle_id, slot, status, record_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (cycle_id) DO UPDATE SET status = excluded.status,
                     record_json = excluded.record_json, updated_at = excluded.updated_at""",
                (data["cycle_id"], slot, data["status"], _dump(data), stamp, stamp),
            )

    def cycle_exists(self, cycle_id: str) -> bool:
        with self._read() as conn:
            return conn.execute("SELECT 1 FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone() is not None

    def get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT record_json FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return json.loads(row["record_json"]) if row else None

    def record_role_call(self, cycle_id: str, call: BaseModel | Mapping[str, Any], *, now: datetime | None = None) -> None:
        data = call.model_dump(mode="json") if isinstance(call, BaseModel) else dict(call)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO role_calls (cycle_id, role, replicate, status, record_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cycle_id, data["role"], int(data.get("replicate", 0)), data["status"], _dump(data), self._now(now)),
            )

    def role_calls(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT record_json FROM role_calls WHERE cycle_id = ? ORDER BY call_id", (cycle_id,)
            ).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    # ------------------------------------------------------------------ decisions
    def create_decision(
        self,
        *,
        decision_id: str,
        kind: DecisionKind,
        valid_until: datetime,
        cycle_id: str | None = None,
        target: Mapping[str, Any] | None = None,
        plan: BaseModel | Mapping[str, Any] | None = None,
        state: str = "proposed",
        commitment_sha: str | None = None,
        actor: str = SYSTEM_ACTOR,
        now: datetime | None = None,
        policy_sha: str | None = None,
    ) -> list[str]:
        """Create a pending decision; returns the ids it superseded. See the module rules."""
        actor = _check_actor(actor)
        if kind not in PRIORITY:
            raise LedgerError(f"unknown decision kind {kind!r}")
        if state not in PENDING_STATES:
            raise LedgerError("a new decision starts pending (awaiting_publication or proposed)")
        priority = PRIORITY[kind]
        stamp = self._now(now)
        placeholders = ",".join("?" * len(PENDING_STATES))
        with self._tx() as conn:
            pending = conn.execute(
                f"SELECT decision_id, priority, state FROM decisions WHERE state IN ({placeholders})",
                tuple(PENDING_STATES),
            ).fetchall()
            higher = [r["decision_id"] for r in pending if r["priority"] > priority]
            if higher:
                raise PriorityConflict(
                    f"pending higher-priority decision(s) {', '.join(sorted(higher))}; {kind} not created"
                )
            superseded = [r for r in pending if r["priority"] <= priority]
            for r in superseded:
                conn.execute(
                    """UPDATE decisions SET prev_state = state, state = 'superseded',
                       superseded_by = ?, updated_at = ? WHERE decision_id = ? AND state = ?""",
                    (decision_id, stamp, r["decision_id"], r["state"]),
                )
                _log_event(conn, r["decision_id"], r["state"], "superseded",
                           f"superseded by {decision_id}", stamp, actor)
            conn.execute(
                """INSERT INTO decisions (decision_id, cycle_id, kind, priority, state, created_at,
                     updated_at, valid_until, target_json, plan_json, commitment_sha, policy_sha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id, cycle_id, kind, priority, state, stamp, stamp, ts(valid_until),
                    _dump(dict(target or {})), _dump(plan) if plan is not None else None,
                    commitment_sha, policy_sha,
                ),
            )
            _log_event(conn, decision_id, None, state, "created", stamp, actor)
        return [r["decision_id"] for r in superseded]

    def get_decision(self, decision_id: str) -> DecisionRow:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown decision {decision_id}")
        return _decision(row)

    def transition(
        self,
        decision_id: str,
        to_state: str,
        reason: str,
        *,
        actor: str = SYSTEM_ACTOR,
        now: datetime | None = None,
    ) -> DecisionRow:
        """Move a decision along ALLOWED_TRANSITIONS (compare-and-set) and log the event with its
        actor. Only actor "operator" may move a decision to `approved` (ApprovalRefused)."""
        if to_state not in DECISION_STATES:
            raise InvalidTransition(f"unknown decision state {to_state!r}")
        actor = _check_actor(actor)
        if to_state == "approved" and actor != OPERATOR_ACTOR:
            raise ApprovalRefused(f"{decision_id}: only the operator may approve (actor {actor!r})")
        stamp = self._now(now)
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown decision {decision_id}")
            from_state = row["state"]
            if not can_transition(from_state, to_state):
                raise InvalidTransition(f"{decision_id}: {from_state} -> {to_state} is not allowed")
            cur = conn.execute(
                "UPDATE decisions SET prev_state = state, state = ?, updated_at = ? WHERE decision_id = ? AND state = ?",
                (to_state, stamp, decision_id, from_state),
            )
            if cur.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                raise InvalidTransition(f"{decision_id}: state changed concurrently")
            _log_event(conn, decision_id, from_state, to_state, reason, stamp, actor)
        return self.get_decision(decision_id)

    def note(self, decision_id: str, reason: str, *, actor: str = SYSTEM_ACTOR, now: datetime | None = None) -> None:
        """Log an event without a state change."""
        actor = _check_actor(actor)
        state = self.get_decision(decision_id).state
        with self._tx() as conn:
            _log_event(conn, decision_id, state, state, reason, self._now(now), actor)

    def events(self, decision_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT from_state, to_state, reason, created_at, actor, process_role
                   FROM decision_events WHERE decision_id = ? ORDER BY event_id""",
                (decision_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def expire_stale(self, now: datetime | None = None) -> list[str]:
        """Expire every pending decision whose valid_until has passed — one atomic UPDATE."""
        stamp = self._now(now)
        placeholders = ",".join("?" * len(PENDING_STATES))
        with self._tx() as conn:
            rows = conn.execute(
                f"""UPDATE decisions SET prev_state = state, state = 'expired', updated_at = ?
                    WHERE state IN ({placeholders}) AND valid_until <= ?
                    RETURNING decision_id, prev_state""",
                (stamp, *PENDING_STATES, stamp),
            ).fetchall()
            for r in rows:
                _log_event(conn, r["decision_id"], r["prev_state"], "expired", "valid_until passed",
                           stamp, SYSTEM_ACTOR)
        return sorted(r["decision_id"] for r in rows)

    def pending(self) -> list[DecisionRow]:
        placeholders = ",".join("?" * len(PENDING_STATES))
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM decisions WHERE state IN ({placeholders}) ORDER BY priority DESC, created_at DESC",
                tuple(PENDING_STATES),
            ).fetchall()
        return [_decision(r) for r in rows]

    def decisions(self, *, states: Iterable[str] | None = None, limit: int = 100) -> list[DecisionRow]:
        with self._read() as conn:
            if states is None:
                rows = conn.execute("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                wanted = tuple(states)
                rows = conn.execute(
                    f"SELECT * FROM decisions WHERE state IN ({','.join('?' * len(wanted))}) ORDER BY created_at DESC LIMIT ?",
                    (*wanted, limit),
                ).fetchall()
        return [_decision(r) for r in rows]

    def blockers(self) -> list[str]:
        """Decisions that halt new risk: blocked / execution_unknown / waiting_for_market, or with
        an unknown leg. Every id holds the whole book, except "satellite:<id>" for a
        waiting_for_market decision whose blocker scope is `satellite` (stock orders only) or a
        blocked one that kept that scope (a stock-only hold that timed out; the watch resets the
        scope to `all` on a broken fill), and "swing:<id>" (halts new swing entries only) for a
        decision of scope `swing` whose every unresolved leg is a swing leg, plus "swing:<trade_id>"
        for a swing trade in entry_unknown / open_tp_missing. A swing-scoped decision that still has
        an unresolved non-swing leg holds the whole book. Otherwise an unknown leg or
        execution_unknown holds the whole book."""
        states = (*sorted(BLOCKER_STATES), WAITING_STATE)
        placeholders = ",".join("?" * len(states))
        with self._read() as conn:
            unknown = {r["decision_id"] for r in conn.execute(
                "SELECT decision_id FROM legs WHERE state = 'unknown'").fetchall()}
            rows = conn.execute(
                f"SELECT decision_id, state, blocker_scope FROM decisions WHERE state IN ({placeholders})",
                states,
            ).fetchall()
            scopes = {r["decision_id"]: r["blocker_scope"] for r in rows}
            for decision_id in unknown - set(scopes):
                row = conn.execute("SELECT blocker_scope FROM decisions WHERE decision_id = ?",
                                   (decision_id,)).fetchone()
                scopes[decision_id] = row["blocker_scope"] if row is not None else None
            swing_scoped = {d for d, scope in scopes.items()
                            if scope == "swing" and self._only_swing_unresolved(conn, d)}
            trade_holds = [r["trade_id"] for r in conn.execute(
                f"SELECT trade_id FROM swing_trades WHERE state IN "
                f"({','.join('?' * len(SWING_BLOCKING_STATES))})",
                sorted(SWING_BLOCKING_STATES)).fetchall()]
        out: set[str] = set()
        for decision_id in unknown:
            out.add(f"{SWING_BLOCKER_PREFIX}{decision_id}" if decision_id in swing_scoped else decision_id)
        for r in rows:
            decision_id = r["decision_id"]
            if decision_id in unknown:
                continue
            if decision_id in swing_scoped:
                out.add(f"{SWING_BLOCKER_PREFIX}{decision_id}")
                continue
            satellite = r["state"] in ("blocked", WAITING_STATE) and r["blocker_scope"] == "satellite"
            out.add(f"{SATELLITE_BLOCKER_PREFIX}{decision_id}" if satellite else decision_id)
        out.update(f"{SWING_BLOCKER_PREFIX}{trade_id}" for trade_id in trade_holds)
        return sorted(out)

    def swing_blockers(self) -> list[str]:
        """The "swing:" blockers only (they halt new swing entries, never the core)."""
        return [b for b in self.blockers() if b.startswith(SWING_BLOCKER_PREFIX)]

    def swing_entries_blocked(self) -> bool:
        """New swing entries are halted by ANY blocker except a satellite-only one: a swing-scoped
        hold, or a whole-book one (a core-scoped unknown still halts everything)."""
        return any(not b.startswith(SATELLITE_BLOCKER_PREFIX) for b in self.blockers())

    @staticmethod
    def _swing_leg_keys(conn: sqlite3.Connection, decision_id: str) -> set[int]:
        """Seqs of the decision's swing legs: a trade row points at it, or its detail says so."""
        seqs = {r["entry_seq"] for r in conn.execute(
            "SELECT entry_seq FROM swing_trades WHERE decision_id = ? AND entry_seq IS NOT NULL",
            (decision_id,)).fetchall()}
        for r in conn.execute("SELECT seq, detail_json FROM legs WHERE decision_id = ?", (decision_id,)):
            detail = json.loads(r["detail_json"] or "{}")
            if detail.get("sleeve") == "swing" or detail.get("swing_trade_id"):
                seqs.add(r["seq"])
        return seqs

    def _only_swing_unresolved(self, conn: sqlite3.Connection, decision_id: str) -> bool:
        """True when the decision has at least one unresolved (planned, active, unknown or waiting)
        leg and every such leg is a swing leg. Fail-closed: a hold with nothing unresolved (a post-fill
        exposure mismatch, a reconcile failure after every leg resolved) is not caused by a swing leg,
        so it holds the whole book whatever scope was recorded earlier; a planned leg left behind by a
        dead process counts as unresolved (it may be a core leg nobody marked)."""
        rows = conn.execute("SELECT seq, state FROM legs WHERE decision_id = ?", (decision_id,)).fetchall()
        unresolved = {r["seq"] for r in rows
                      if r["state"] in LEG_ACTIVE_STATES | {WAITING_STATE, "planned"}}
        if not unresolved:     # nothing swing explains the hold: the whole book stays held
            return False
        return unresolved <= self._swing_leg_keys(conn, decision_id)

    def has_blocker(self) -> bool:
        return bool(self.blockers())

    def _set_decision_field(self, decision_id: str, column: str, value: Any, now: datetime | None) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE decisions SET {column} = ?, updated_at = ? WHERE decision_id = ?",
                (value, self._now(now), decision_id),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"unknown decision {decision_id}")

    def set_commitment(self, decision_id: str, sha: str, *, now: datetime | None = None) -> None:
        self._set_decision_field(decision_id, "commitment_sha", sha, now)

    def set_published_commit(self, decision_id: str, commit: str, *, now: datetime | None = None) -> None:
        self._set_decision_field(decision_id, "published_commit", commit, now)

    def set_plan(self, decision_id: str, plan: BaseModel | Mapping[str, Any], *, now: datetime | None = None) -> None:
        self._set_decision_field(decision_id, "plan_json", _dump(plan), now)

    def set_blocker_scope(self, decision_id: str, scope: str, *, now: datetime | None = None) -> None:
        """`all` (hold every line), `satellite` (hold only the satellite sleeve) or `swing` (hold only
        new swing entries; refused while any unresolved leg of the decision is not a swing leg)."""
        if scope not in BLOCKER_SCOPES:
            raise LedgerError(f"unknown blocker scope {scope!r}")
        if scope == "swing":
            with self._read() as conn:
                if not self._only_swing_unresolved(conn, decision_id):
                    raise LedgerError(f"{decision_id}: an unresolved non-swing leg; scope stays whole-book")
        self._set_decision_field(decision_id, "blocker_scope", scope, now)

    def pending_open_weights(self) -> dict[str, float]:
        """Signed line weight still held at the broker by OPEN legs in waiting_for_market
        (Σ weight_after − weight_before per line). The engine counts it as held."""
        out: dict[str, float] = {}
        for row in self.legs_in_states([WAITING_STATE]):
            if row.kind != "open":
                continue
            before, after = row.detail.get("weight_before"), row.detail.get("weight_after")
            if before is None or after is None:
                continue
            out[row.line] = out.get(row.line, 0.0) + float(after) - float(before)
        return out

    # ------------------------------------------------------------------ smoke tickets
    def smoke_positions(self) -> dict[int, str]:
        """{position id: smoke decision id} of every position a smoke OPEN leg filled that no
        filled full close (a smoke ticket's or any other decision's, e.g. a flatten) has closed
        since (a partial close keeps it open). Ledger view only:
        pass the live ids to `smoke_active` to drop positions the broker no longer holds."""
        with self._read() as conn:
            rows = conn.execute(
                """SELECT l.decision_id, l.kind, l.state, l.position_id, l.position_ids_json
                   FROM legs l JOIN decisions d ON d.decision_id = l.decision_id
                   WHERE d.kind = ? OR l.kind = 'close'
                   ORDER BY COALESCE(l.resolved_at, l.updated_at), l.seq""",
                (SMOKE_KIND,),
            ).fetchall()
            smoke_ids = {r[0] for r in conn.execute("SELECT decision_id FROM decisions WHERE kind = ?",
                                                    (SMOKE_KIND,))}
        opened: dict[int, str] = {}
        for r in rows:
            if r["state"] not in FILLED_LEG_STATES:
                continue
            if r["kind"] == "open":
                if r["decision_id"] not in smoke_ids:
                    continue
                for pid in json.loads(r["position_ids_json"] or "[]"):
                    opened[int(pid)] = r["decision_id"]
            elif r["kind"] == "close" and r["position_id"] is not None:
                opened.pop(int(r["position_id"]), None)
        return opened

    def smoke_active(self, live_position_ids: Iterable[int] | None = None) -> list[str]:
        """Why a live cycle may not seal a proposal now (gate K20; empty = no smoke activity):
        `smoke_pending:<id>` (awaiting publication or proposed), `smoke_in_flight:<id>` (approved,
        executing, execution_unknown or waiting for its market) and `smoke_position:<id>` (a
        smoke-opened position still open; only those the broker still holds when `live_position_ids`
        is given, e.g. after a stop hit)."""
        in_flight = ("approved", "executing", "execution_unknown", WAITING_STATE)
        wanted = (*sorted(PENDING_STATES), *in_flight)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT decision_id, state FROM decisions WHERE kind = ? AND state IN ({','.join('?' * len(wanted))})",
                (SMOKE_KIND, *wanted),
            ).fetchall()
        out = [f"smoke_{'pending' if r['state'] in PENDING_STATES else 'in_flight'}:{r['decision_id']}"
               for r in rows]
        live = None if live_position_ids is None else {int(i) for i in live_position_ids}
        for pid, decision_id in sorted(self.smoke_positions().items()):
            if live is None or pid in live:
                out.append(f"smoke_position:{decision_id}")
        return sorted(dict.fromkeys(out))

    def smoke_baseline_due(self) -> bool:
        """True once, when the NAV baseline must restart after the token-day smoke tickets: some
        smoke leg reached the broker, no other decision was ever created (the first live cycle has
        not proposed yet) and the reset was not done (`smoke_baseline_reset`). Track S's S7 runs
        after go-live and never resets the baseline."""
        if self.get_runtime(SMOKE_BASELINE_RESET_KEY):
            return False
        with self._read() as conn:
            others = conn.execute("SELECT COUNT(*) FROM decisions WHERE kind != ?", (SMOKE_KIND,)).fetchone()[0]
            sent = conn.execute(
                """SELECT COUNT(*) FROM legs l JOIN decisions d ON d.decision_id = l.decision_id
                   WHERE d.kind = ? AND l.submitted_at IS NOT NULL""", (SMOKE_KIND,)).fetchone()[0]
        return int(others) == 0 and int(sent) > 0

    # ------------------------------------------------------------------ legs
    def insert_legs(
        self,
        decision_id: str,
        legs: Sequence[Leg],
        *,
        line_of: Callable[[str], str | None] | Mapping[str, str] | None = None,
        universe: Universe | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write a decision's legs once, all `planned` (line weights kept in `detail`).

        Each row's line is `Leg.line`; for a leg without one, `line_of(symbol)` when given, then
        `vehicle_to_line(universe)` (default: the default policy's universe); an `UNMAPPED_<id>`
        symbol is its own line; any other unresolvable symbol raises LedgerError.

        Idempotent for the SAME legs (the cycle writes them at proposal, the executor again at
        execution): when rows exist and every one is still `planned` and matches the given legs
        field by field, nothing is written; any difference raises LedgerError."""
        lines = [self._leg_line(leg, line_of, universe) for leg in legs]
        stamp = self._now(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM legs WHERE decision_id = ? ORDER BY seq", (decision_id,)
            ).fetchall()
            if existing:
                if _same_planned_legs([_leg(r) for r in existing], legs, lines):
                    return
                raise LedgerError(f"legs for {decision_id} were already written (and differ)")
            for leg, line in zip(legs, lines, strict=True):
                conn.execute(
                    """INSERT INTO legs (leg_id, decision_id, seq, kind, line, vehicle_symbol,
                         instrument_id, direction, settlement, leverage, units, amount_usd, sl_rate,
                         position_id, depends_on_json, risk_increasing, state, detail_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)""",
                    (
                        f"{decision_id}:{leg.seq}", decision_id, leg.seq, leg.kind, line,
                        leg.symbol, leg.instrument_id, leg.direction, leg.settlement, leg.leverage,
                        leg.units, leg.amount_usd, leg.sl_rate, leg.position_id,
                        json.dumps(leg.depends_on), int(leg.risk_increasing),
                        _dump({
                            "weight_before": leg.weight_before, "weight_after": leg.weight_after,
                            "reason": leg.reason, "cost_bps_nav": leg.cost_bps_nav,
                            "whole_units": leg.whole_units, "session": leg.session,
                            "valid_until": ts(leg.valid_until) if leg.valid_until else None,
                            "origin": leg.origin, "ref_level": leg.ref_level,
                            "fee_bps_nav": leg.fee_bps_nav, "fee_drag": leg.fee_drag,
                            **_swing_detail(leg),
                        }),
                        stamp,
                    ),
                )

    def _leg_line(
        self,
        leg: Leg,
        line_of: Callable[[str], str | None] | Mapping[str, str] | None,
        universe: Universe | None,
    ) -> str:
        if leg.line:
            return leg.line
        if line_of is not None:
            found = line_of.get(leg.symbol) if isinstance(line_of, Mapping) else line_of(leg.symbol)
            if found:
                return found
        found = self._vehicle_lines(universe).get(leg.symbol)
        if found:
            return found
        if leg.symbol.startswith(UNMAPPED_PREFIX):
            return leg.symbol
        raise LedgerError(f"leg {leg.seq}: no line for symbol {leg.symbol!r}")

    @staticmethod
    def _vehicle_lines(universe: Universe | None) -> dict[str, str]:
        # imported lazily: the ledger sits below execution in the dependency order
        from council.execution.planner import vehicle_to_line
        from council.policy import default_policy

        return vehicle_to_line(universe if universe is not None else default_policy().universe)

    def legs(self, decision_id: str) -> list[LegRow]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM legs WHERE decision_id = ? ORDER BY seq", (decision_id,)).fetchall()
        return [_leg(r) for r in rows]

    def get_leg(self, decision_id: str, seq: int) -> LegRow:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM legs WHERE decision_id = ? AND seq = ?", (decision_id, seq)).fetchone()
        if row is None:
            raise LedgerError(f"unknown leg {decision_id}:{seq}")
        return _leg(row)

    def leg_by_request_id(self, request_id: str) -> LegRow | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM legs WHERE request_id = ?", (request_id,)).fetchone()
        return _leg(row) if row else None

    def legs_in_states(self, states: Iterable[str]) -> list[LegRow]:
        wanted = tuple(states)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM legs WHERE state IN ({','.join('?' * len(wanted))}) ORDER BY decision_id, seq",
                wanted,
            ).fetchall()
        return [_leg(r) for r in rows]

    def update_leg(
        self,
        decision_id: str,
        seq: int,
        *,
        state: str | None = None,
        now: datetime | None = None,
        **fields: Any,
    ) -> LegRow:
        """Update a leg's execution fields and, optionally, move it along LEG_TRANSITIONS.
        `detail` is merged into the stored detail. Terminal legs never change."""
        unknown = set(fields) - _LEG_FIELDS
        if unknown:
            raise LedgerError(f"unknown leg field(s): {', '.join(sorted(unknown))}")
        if state is not None and state not in LEG_STATES:
            raise InvalidTransition(f"unknown leg state {state!r}")
        stamp = self._now(now)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, detail_json FROM legs WHERE decision_id = ? AND seq = ?", (decision_id, seq)
            ).fetchone()
            if row is None:
                raise LedgerError(f"unknown leg {decision_id}:{seq}")
            current = row["state"]
            if current in LEG_TERMINAL_STATES:
                raise InvalidTransition(f"leg {decision_id}:{seq} is terminal ({current})")
            if state is not None and state != current and not can_transition_leg(current, state):
                raise InvalidTransition(f"leg {decision_id}:{seq}: {current} -> {state} is not allowed")
            columns: dict[str, Any] = {"state": state or current, "updated_at": stamp}
            for key, value in fields.items():
                if key == "position_ids":
                    columns["position_ids_json"] = json.dumps(list(value))
                elif key == "detail":
                    merged = json.loads(row["detail_json"] or "{}")
                    merged.update(redact(dict(value)))
                    columns["detail_json"] = _dump(merged)
                elif key in ("submitted_at", "resolved_at"):
                    columns[key] = ts(value) if value is not None else None
                else:
                    columns[key] = value
            assignments = ", ".join(f"{k} = ?" for k in columns)
            try:
                conn.execute(
                    f"UPDATE legs SET {assignments} WHERE decision_id = ? AND seq = ? AND state = ?",
                    (*columns.values(), decision_id, seq, current),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"leg {decision_id}:{seq}: {exc}") from exc
        return self.get_leg(decision_id, seq)

    # ------------------------------------------------------------------ observations
    def record_positions(
        self,
        observed_at: datetime,
        positions: Sequence[Position],
        *,
        decision_id: str | None = None,
        source: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO positions_observed (observed_at, decision_id, source, positions_json) VALUES (?, ?, ?, ?)",
                (ts(observed_at), decision_id, source, _dump([p.model_dump(mode="json") for p in positions])),
            )

    def positions_observed(self, decision_id: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if decision_id is None:
                rows = conn.execute("SELECT * FROM positions_observed ORDER BY observation_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM positions_observed WHERE decision_id = ? ORDER BY observation_id", (decision_id,)
                ).fetchall()
        return [{**dict(r), "positions": json.loads(r["positions_json"])} for r in rows]

    def record_broker_event(
        self,
        *,
        kind: str,
        decision_id: str | None = None,
        seq: int | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
        payload: Any = None,
        now: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO broker_events (at, decision_id, seq, kind, request_id, http_status, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self._now(now), decision_id, seq, kind, request_id, http_status, _dump(redact(payload or {}))),
            )

    def broker_events(self, decision_id: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if decision_id is None:
                rows = conn.execute("SELECT * FROM broker_events ORDER BY event_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM broker_events WHERE decision_id = ? ORDER BY event_id", (decision_id,)
                ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    def add_equity_mark(
        self,
        at: datetime,
        equity_usd: float,
        *,
        credit_usd: float | None = None,
        flow_usd: float = 0.0,
        source: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO equity_marks (at, equity_usd, credit_usd, flow_usd, source) VALUES (?, ?, ?, ?, ?)",
                (ts(at), float(equity_usd), credit_usd, float(flow_usd), source),
            )

    def equity_marks(self, since: datetime | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if since is None:
                rows = conn.execute("SELECT * FROM equity_marks ORDER BY at, mark_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM equity_marks WHERE at >= ? ORDER BY at, mark_id", (ts(since),)
                ).fetchall()
        return [{**dict(r), "at": parse_ts(r["at"])} for r in rows]

    def latest_equity_mark(self) -> dict[str, Any] | None:
        marks = self.equity_marks()
        return marks[-1] if marks else None

    # ------------------------------------------------------------------ cycle queries
    def _moved_legs(
        self,
        *,
        since: datetime | None = None,
        line: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        """Resolved legs that moved exposure (FILLED_LEG_STATES, never modify_sl, never a smoke
        ticket's), with `done_at`
        = resolved_at (else updated_at); optionally since a time, for one line, or for some
        decision kinds."""
        states = ",".join("?" * len(FILLED_LEG_STATES))
        sql = [
            f"""SELECT l.line, l.state, l.kind, l.detail_json, d.kind AS decision_kind,
                       COALESCE(l.resolved_at, l.updated_at) AS done_at
                FROM legs l JOIN decisions d ON d.decision_id = l.decision_id
                WHERE l.state IN ({states}) AND l.kind != 'modify_sl' AND d.kind != ?"""
        ]
        args: list[Any] = [*FILLED_LEG_STATES, SMOKE_KIND]     # M-3: smoke never feeds the book
        if since is not None:
            sql.append("AND COALESCE(l.resolved_at, l.updated_at) >= ?")
            args.append(ts(since))
        if line is not None:
            sql.append("AND l.line = ?")
            args.append(line)
        if kinds is not None:
            wanted = tuple(kinds)
            if not wanted:
                return []
            sql.append(f"AND d.kind IN ({','.join('?' * len(wanted))})")
            args.extend(wanted)
        with self._read() as conn:
            return conn.execute(" ".join(sql) + " ORDER BY done_at", args).fetchall()

    def last_change(self, line: str) -> datetime | None:
        """When the line's exposure last changed: the latest resolved (filled or partly filled)
        leg on it, of any decision kind. None if it never traded."""
        rows = self._moved_legs(line=line)
        return parse_ts(rows[-1]["done_at"]) if rows else None

    def last_changes(self) -> dict[str, datetime]:
        """`last_change` for every line that ever traded (the engine's `last_change` input)."""
        out: dict[str, datetime] = {}
        for r in self._moved_legs():
            out[r["line"]] = parse_ts(r["done_at"])  # type: ignore[assignment]  # ordered: last wins
        return out

    def _moved_details(
        self,
        *,
        since: datetime | None = None,
        kinds: Iterable[str] | None = None,
        origins: Iterable[str] | None = None,
    ) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
        """`_moved_legs` rows with their parsed detail, optionally only some leg origins."""
        wanted = None if origins is None else frozenset(origins)
        out = []
        for r in self._moved_legs(since=since, kinds=kinds):
            detail = json.loads(r["detail_json"] or "{}")
            if wanted is not None and leg_origin(detail) not in wanted:
                continue
            out.append((r, detail))
        return out

    def turnover_since(self, since: datetime, *, kinds: Iterable[str] | None = None,
                       origins: Iterable[str] | None = None) -> float:
        """Σ |weight_after − weight_before| over legs resolved at or after `since` that moved
        exposure (a partial fill counts its filled share when known, else in full). Pass
        kinds=("rebalance",), origins=("discretionary",) for R13's discretionary turnover."""
        total = 0.0
        for r, detail in self._moved_details(since=since, kinds=kinds, origins=origins):
            before, after = detail.get("weight_before"), detail.get("weight_after")
            if before is None or after is None:
                continue
            total += abs(float(after) - float(before)) * _fill_fraction(r["state"], detail)
        return total

    def cost_bps_since(self, since: datetime, *, kinds: Iterable[str] | None = None,
                       origins: Iterable[str] | None = None, include_fee: bool = True) -> float:
        """Σ planned cost (bps of NAV) of the legs `turnover_since` counts: the variable cost plus,
        unless `include_fee=False`, the private fixed fee (legs written before costs were stored
        count 0)."""
        total = 0.0
        for r, detail in self._moved_details(since=since, kinds=kinds, origins=origins):
            cost = float(detail.get("cost_bps_nav") or 0.0)
            if include_fee:
                cost += float(detail.get("fee_bps_nav") or 0.0)
            total += cost * _fill_fraction(r["state"], detail)
        return total

    def fee_bps_since(self, since: datetime, *, kinds: Iterable[str] | None = None,
                      origins: Iterable[str] | None = None) -> float:
        """Σ private fixed fees (bps of NAV) of the legs `cost_bps_since` counts."""
        total = 0.0
        for r, detail in self._moved_details(since=since, kinds=kinds, origins=origins):
            total += float(detail.get("fee_bps_nav") or 0.0) * _fill_fraction(r["state"], detail)
        return total

    def reference_fills(self) -> dict[str, tuple[float, datetime]]:
        """(reference level, when) of the latest filled REFERENCE-origin leg per line that recorded
        its level: the held reference level (`risk.held_levels`)."""
        out: dict[str, tuple[float, datetime]] = {}
        for r, detail in self._moved_details(origins=("reference",)):
            level = detail.get("ref_level")
            if level is None:
                continue
            out[r["line"]] = (float(level), parse_ts(r["done_at"]))  # type: ignore[assignment]  # ordered
        return out

    def level_resets(self) -> dict[str, datetime]:
        """When each line's position was last taken away from the rule: the latest broker stop-loss
        hit, or filled close of a kill-switch flatten (the held reference level becomes 0)."""
        out = dict(self.stop_hits_since(datetime(1970, 1, 1, tzinfo=UTC)))
        for r in self._moved_legs(kinds=("flatten",)):
            if r["kind"] in ("close", "partial_close"):
                at = parse_ts(r["done_at"])
                if at is not None and (r["line"] not in out or at > out[r["line"]]):
                    out[r["line"]] = at
        return out

    def real_fee_drag(self) -> float:
        """The real account's cumulative extra fee drag (D19, private): 1 − Π(1 − fee_drag × filled
        share) over every filled leg that recorded one."""
        keep = 1.0
        for r, detail in self._moved_details():
            drag = float(detail.get("fee_drag") or 0.0)
            if drag > 0 and math.isfinite(drag):
                keep *= max(0.0, 1.0 - min(drag, 1.0) * _fill_fraction(r["state"], detail))
        return 1.0 - keep

    def record_stop_hit(
        self,
        *,
        line: str,
        symbol: str | None = None,
        position_id: int | None = None,
        at: datetime | None = None,
    ) -> None:
        """Record a broker stop-loss hit (a `stop_hit` broker event) at `at` (default now)."""
        if not line:
            raise LedgerError("a stop hit needs its line")
        self.record_broker_event(
            kind=STOP_HIT_EVENT,
            payload={"line": line, "symbol": symbol, "position_id": position_id},
            now=at,
        )

    def stop_hits_since(self, since: datetime, *, universe: Universe | None = None) -> dict[str, datetime]:
        """Latest `stop_hit` broker event per line at or after `since`. The line is the payload's
        `line`, else its vehicle `symbol` mapped with vehicle_to_line(universe); events naming
        neither are ignored."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT at, payload_json FROM broker_events WHERE kind = ? AND at >= ? ORDER BY at, event_id",
                (STOP_HIT_EVENT, ts(since)),
            ).fetchall()
        v2l: dict[str, str] | None = None
        out: dict[str, datetime] = {}
        for r in rows:
            payload = json.loads(r["payload_json"] or "{}")
            line = payload.get("line") if isinstance(payload, dict) else None
            symbol = payload.get("symbol") if isinstance(payload, dict) else None
            if not line and symbol:
                if v2l is None:
                    v2l = self._vehicle_lines(universe)
                line = v2l.get(symbol) or (symbol if symbol.startswith(UNMAPPED_PREFIX) else None)
            if line:
                out[str(line)] = parse_ts(r["at"])  # type: ignore[assignment]
        return out

    # ------------------------------------------------------------------ runtime state
    def get_runtime(self, key: str, default: Any = None) -> Any:
        with self._read() as conn:
            row = conn.execute("SELECT value_json FROM runtime_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_runtime(self, key: str, value: Any, *, now: datetime | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO runtime_state (key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json,
                     updated_at = excluded.updated_at""",
                (key, _dump(value), self._now(now)),
            )

    def get_nav_state(self) -> NavState | None:
        """The persisted NAV state (lifetime peak) or None before the first equity read."""
        raw = self.get_runtime(NAV_STATE_KEY)
        return NavState.model_validate(raw) if raw is not None else None

    def set_nav_state(self, state: NavState | Mapping[str, Any], *, now: datetime | None = None) -> None:
        value = state if isinstance(state, NavState) else NavState.model_validate(state)
        self.set_runtime(NAV_STATE_KEY, value.model_dump(mode="json"), now=now)

    def get_kill_state_record(self) -> dict[str, Any] | None:
        """{"state", "reason", "at"} as last stored, or None."""
        raw = self.get_runtime(KILL_STATE_KEY)
        if raw is None:
            return None
        return {"state": raw, "reason": "", "at": None} if isinstance(raw, str) else dict(raw)

    def get_kill_state(self) -> str:
        """The persisted kill state; NORMAL when none was ever stored."""
        record = self.get_kill_state_record()
        state = record.get("state") if record else None
        if state is None:
            return "NORMAL"
        if state not in KILL_STATES:
            raise LedgerError(f"stored kill state {state!r} is not a kill state")
        return str(state)

    def set_kill_state(self, state: str, *, reason: str = "", now: datetime | None = None) -> None:
        if state not in KILL_STATES:
            raise LedgerError(f"unknown kill state {state!r}")
        stamp = self._now(now)
        self.set_runtime(KILL_STATE_KEY, {"state": state, "reason": reason, "at": stamp}, now=now)

    def get_material_fingerprint(self) -> str | None:
        """The material fingerprint of the last EXECUTED decision, or None."""
        value = self.get_runtime(MATERIAL_FINGERPRINT_KEY)
        return str(value) if value else None

    def set_material_fingerprint(self, fingerprint: str, *, now: datetime | None = None) -> None:
        if not fingerprint:
            raise LedgerError("empty material fingerprint")
        self.set_runtime(MATERIAL_FINGERPRINT_KEY, fingerprint, now=now)

    def get_material_fingerprints(self) -> dict[str, str] | None:
        """The per-line material fingerprints ({"_global": ..., line: ...}) stored when the last
        proposal was issued, or None (never stored, or not a mapping of strings)."""
        value = self.get_runtime(MATERIAL_FINGERPRINTS_KEY)
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in value.items()):
            return None
        return dict(value)

    def set_material_fingerprints(self, fingerprints: Mapping[str, str], *, now: datetime | None = None) -> None:
        if not fingerprints or not all(isinstance(k, str) and isinstance(v, str) and v
                                       for k, v in fingerprints.items()):
            raise LedgerError("material fingerprints must be a non-empty map of line -> fingerprint")
        self.set_runtime(MATERIAL_FINGERPRINTS_KEY, dict(sorted(fingerprints.items())), now=now)

    # ------------------------------------------------------------------ swing book (SW-2b)
    def add_swing_idea(
        self, idea_id: str, *, origin_cycle: str, ticker: str, side: str, status: str,
        setup: str | None = None, record: BaseModel | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """One Scout idea (private; its text is scrubbed by the 7-day purge by origin cycle)."""
        if not idea_id.startswith("idea:"):
            raise LedgerError(f"swing idea id must start with 'idea:': {idea_id!r}")
        stamp = self._now(now)
        with self._tx() as conn:
            try:
                conn.execute(
                    """INSERT INTO swing_ideas (idea_id, origin_cycle, ticker, side, setup, status,
                         record_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (idea_id, origin_cycle, ticker, side, setup, status, _dump(record or {}),
                     stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"swing idea {idea_id}: {exc}") from exc

    def update_swing_idea(
        self, idea_id: str, *, status: str | None = None,
        record: BaseModel | Mapping[str, Any] | None = None, carry_cycle: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Change an idea's status / record, or note a cycle that carried it forward."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM swing_ideas WHERE idea_id = ?", (idea_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown swing idea {idea_id}")
            carry = json.loads(row["carry_cycles_json"] or "[]")
            if carry_cycle and carry_cycle != row["origin_cycle"] and carry_cycle not in carry:
                carry.append(carry_cycle)
            conn.execute(
                """UPDATE swing_ideas SET status = ?, record_json = ?, carry_cycles_json = ?,
                     updated_at = ? WHERE idea_id = ?""",
                (status or row["status"], _dump(record) if record is not None else row["record_json"],
                 json.dumps(carry), self._now(now), idea_id))
        return self.swing_idea(idea_id)  # type: ignore[return-value]

    def swing_idea(self, idea_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM swing_ideas WHERE idea_id = ?", (idea_id,)).fetchone()
        return _swing_idea(row) if row is not None else None

    def swing_ideas(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM swing_ideas ORDER BY created_at, idea_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM swing_ideas WHERE status = ? ORDER BY created_at, idea_id",
                                    (status,)).fetchall()
        return [_swing_idea(r) for r in rows]

    def create_swing_trade(
        self, trade_id: str, *, ticker: str, side: str, idea_id: str | None = None,
        origin_cycle: str | None = None, decision_id: str | None = None, entry_seq: int | None = None,
        instrument_id: int | None = None, sl_rate: float | None = None, tp_rate: float | None = None,
        time_stop_date: str | None = None, detail: Mapping[str, Any] | None = None,
        actor: str = SYSTEM_ACTOR, now: datetime | None = None,
    ) -> SwingTradeRow:
        """A proposed swing trade (state `proposed`). Fills move it with `record_swing_fill`."""
        stamp = self._now(now)
        with self._tx() as conn:
            self._insert_trade(conn, {
                "trade_id": trade_id, "idea_id": idea_id, "origin_cycle": origin_cycle,
                "decision_id": decision_id, "entry_seq": entry_seq, "ticker": ticker,
                "instrument_id": instrument_id, "side": side, "state": "proposed",
                "sl_rate": sl_rate, "tp_rate": tp_rate, "time_stop_date": time_stop_date,
                "detail_json": _dump(_check_trade_detail(dict(detail or {}))),
            }, stamp)
            self._swing_event(conn, "transition", stamp, trade_id=trade_id, idea_id=idea_id,
                              origin_cycle=origin_cycle, to_state="proposed", reason="created",
                              actor=actor)
        return self.swing_trade(trade_id)  # type: ignore[return-value]

    @staticmethod
    def _insert_trade(conn: sqlite3.Connection, values: Mapping[str, Any], stamp: str) -> None:
        if not str(values["trade_id"]).startswith("trade:"):
            raise LedgerError(f"swing trade id must start with 'trade:': {values['trade_id']!r}")
        if values["side"] not in ("long", "short"):
            raise LedgerError(f"swing trade side must be long or short: {values['side']!r}")
        columns = {**values, "created_at": stamp, "updated_at": stamp}
        try:
            conn.execute(
                f"INSERT INTO swing_trades ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(columns.values()))
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"swing trade {values['trade_id']}: {exc}") from exc

    @staticmethod
    def _swing_event(
        conn: sqlite3.Connection, kind: str, stamp: str, *, trade_id: str | None = None,
        idea_id: str | None = None, origin_cycle: str | None = None, from_state: str | None = None,
        to_state: str | None = None, reason: str = "", payload: Mapping[str, Any] | None = None,
        actor: str = SYSTEM_ACTOR,
    ) -> None:
        conn.execute(
            """INSERT INTO swing_events (trade_id, idea_id, origin_cycle, kind, from_state, to_state,
                 reason, payload_json, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, idea_id, origin_cycle, kind, from_state, to_state, reason,
             _dump(redact(dict(payload or {}))), _check_actor(actor), stamp))

    def add_swing_event(
        self, kind: str, *, trade_id: str | None = None, idea_id: str | None = None,
        origin_cycle: str | None = None, reason: str = "", payload: Mapping[str, Any] | None = None,
        actor: str = SYSTEM_ACTOR, now: datetime | None = None,
    ) -> None:
        """A watch flag (`time_stop_due`, `earnings_exit_due`, `target_reached_unplaced`) or a note."""
        if kind == "transition":
            raise LedgerError("transition events are written by transition_swing_trade only")
        with self._tx() as conn:
            if trade_id is not None:        # an unknown trade id would leave text the purge cannot key
                trade = self._trade_row(conn, trade_id)
                if trade is None:
                    raise LedgerError(f"unknown swing trade {trade_id}")
                idea_id = idea_id or trade["idea_id"]
                origin_cycle = origin_cycle or trade["origin_cycle"]
            if origin_cycle is None and idea_id is None and (reason or payload):
                raise LedgerError("a swing event with text needs an origin cycle, idea or trade (7-day purge)")
            self._swing_event(conn, kind, self._now(now), trade_id=trade_id, idea_id=idea_id,
                              origin_cycle=origin_cycle, reason=reason, payload=payload, actor=actor)

    def swing_events(self, trade_id: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if trade_id is None:
                rows = conn.execute("SELECT * FROM swing_events ORDER BY event_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM swing_events WHERE trade_id = ? ORDER BY event_id",
                                    (trade_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in rows]

    def swing_trade(self, trade_id: str) -> SwingTradeRow | None:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM swing_trades WHERE trade_id = ?", (trade_id,)).fetchone()
        return _swing_trade(row) if row is not None else None

    def swing_trades(self, *, states: Iterable[str] | None = None) -> list[SwingTradeRow]:
        with self._read() as conn:
            if states is None:
                rows = conn.execute("SELECT * FROM swing_trades ORDER BY created_at, trade_id").fetchall()
            else:
                wanted = tuple(states)
                if not wanted:
                    return []
                rows = conn.execute(
                    f"SELECT * FROM swing_trades WHERE state IN ({','.join('?' * len(wanted))}) "
                    "ORDER BY created_at, trade_id", wanted).fetchall()
        return [_swing_trade(r) for r in rows]

    def transition_swing_trade(
        self, trade_id: str, to_state: str, *, reason: str = "", actor: str = SYSTEM_ACTOR,
        close_rate: float | None = None, cycle_id: str | None = None, now: datetime | None = None,
    ) -> SwingTradeRow:
        """Move a trade along `swing.models.TRANSITIONS` (compare-and-set). Illegal moves and any
        move of a closed/missed trade raise `IllegalTransition`. `cycle_id` is the cycle whose run
        writes `reason` (default: the trade's origin cycle); the 7-day purge scrubs the event's text
        against that cycle's feed texts, so a later cycle's PM reason must name its own cycle."""
        stamp = self._now(now)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM swing_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown swing trade {trade_id}")
            self._move_trade(conn, row, to_state, stamp, reason=reason, actor=actor, cycle_id=cycle_id,
                             extra={"close_rate": close_rate} if close_rate is not None else None)
        return self.swing_trade(trade_id)  # type: ignore[return-value]

    def _move_trade(
        self, conn: sqlite3.Connection, row: sqlite3.Row, to_state: str, stamp: str, *,
        reason: str, actor: str, extra: Mapping[str, Any] | None = None, cycle_id: str | None = None,
    ) -> None:
        current = row["state"]
        check_trade_transition(current, to_state)
        columns: dict[str, Any] = {"state": to_state, "updated_at": stamp, **(extra or {})}
        if to_state in TRADE_CLOSED_STATES:
            columns["closed_at"] = stamp
        assignments = ", ".join(f"{k} = ?" for k in columns)
        cur = conn.execute(f"UPDATE swing_trades SET {assignments} WHERE trade_id = ? AND state = ?",
                           (*columns.values(), row["trade_id"], current))
        if cur.rowcount != 1:
            raise IllegalTransition(f"swing trade {row['trade_id']} moved concurrently (was {current})")
        self._swing_event(conn, "transition", stamp, trade_id=row["trade_id"], idea_id=row["idea_id"],
                          origin_cycle=cycle_id or row["origin_cycle"], from_state=current, to_state=to_state,
                          reason=reason, actor=actor)

    _TRADE_FIELDS = frozenset({"position_ids", "units", "open_rate", "sl_rate", "tp_rate",
                               "time_stop_date", "instrument_id", "detail"})

    def update_swing_trade(self, trade_id: str, *, now: datetime | None = None, **fields: Any) -> SwingTradeRow:
        """Change a live trade's execution fields (`position_ids` are merged, never dropped;
        `detail` is merged). A closed/missed trade raises `IllegalTransition`."""
        unknown = set(fields) - self._TRADE_FIELDS
        if unknown:
            raise LedgerError(f"unknown swing trade field(s): {', '.join(sorted(unknown))}")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM swing_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown swing trade {trade_id}")
            if row["state"] in TRADE_TERMINAL_STATES:
                raise IllegalTransition(f"swing trade {trade_id} is terminal ({row['state']})")
            columns: dict[str, Any] = {"updated_at": self._now(now)}
            for key, value in fields.items():
                if key == "position_ids":
                    columns["position_ids_json"] = json.dumps(
                        _merge_ids(json.loads(row["position_ids_json"]), value))
                elif key == "detail":
                    columns["detail_json"] = _dump({**json.loads(row["detail_json"] or "{}"),
                                                    **_check_trade_detail(redact(dict(value)))})
                else:
                    columns[key] = value
            assignments = ", ".join(f"{k} = ?" for k in columns)
            conn.execute(f"UPDATE swing_trades SET {assignments} WHERE trade_id = ? AND state = ?",
                         (*columns.values(), trade_id, row["state"]))
        return self.swing_trade(trade_id)  # type: ignore[return-value]

    def record_swing_fill(self, decision_id: str, seq: int, *, actor: str = SYSTEM_ACTOR,
                          now: datetime | None = None) -> SwingTradeRow:
        """Create or advance the swing trade of a FILLED swing open leg (design §4.5): the leg's
        state decides, never its decision's (blocked, execution_unknown, resumed, waiting-for-market
        are all fine). filled -> `open`, partially_filled / rejected_partial -> `partial`; a trade
        already open keeps its state and gains the leg's position ids. The trade is the row whose
        (decision_id, entry_seq) is this leg, else the leg detail's `swing_trade_id` (created here
        when no row exists)."""
        stamp = self._now(now)
        with self._tx() as conn:
            leg = conn.execute("SELECT * FROM legs WHERE decision_id = ? AND seq = ?",
                               (decision_id, seq)).fetchone()
            if leg is None:
                raise LedgerError(f"unknown leg {decision_id}:{seq}")
            if leg["kind"] != "open":
                raise LedgerError(f"leg {decision_id}:{seq} is a {leg['kind']} leg, not a swing entry")
            if leg["state"] not in FILLED_LEG_STATES:
                raise LedgerError(f"leg {decision_id}:{seq} is {leg['state']}, not filled")
            detail = json.loads(leg["detail_json"] or "{}")
            row = conn.execute("SELECT * FROM swing_trades WHERE decision_id = ? AND entry_seq = ?",
                               (decision_id, seq)).fetchone()
            trade_id = detail.get("swing_trade_id")
            if row is None and trade_id:
                row = conn.execute("SELECT * FROM swing_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            target = "open" if leg["state"] == "filled" else "partial"
            ids = _merge_ids(json.loads(leg["position_ids_json"] or "[]"),
                             [leg["position_id"]] if leg["position_id"] is not None else [])
            units = detail.get("units_filled", leg["units"])
            open_rate = detail.get("fill_rate", detail.get("avg_price"))
            if row is None:
                if not trade_id:
                    raise LedgerError(f"leg {decision_id}:{seq} is not a swing leg (no trade, no swing_trade_id)")
                cycle = conn.execute("SELECT cycle_id FROM decisions WHERE decision_id = ?",
                                     (decision_id,)).fetchone()
                origin = cycle["cycle_id"] if cycle is not None else None
                self._insert_trade(conn, {
                    "trade_id": trade_id, "idea_id": detail.get("swing_idea_id"),
                    "origin_cycle": origin, "decision_id": decision_id, "entry_seq": seq,
                    "ticker": detail.get("ticker") or leg["vehicle_symbol"],
                    "instrument_id": leg["instrument_id"], "side": leg["direction"], "state": target,
                    "position_ids_json": json.dumps(ids), "units": units, "open_rate": open_rate,
                    "sl_rate": leg["sl_rate"], "tp_rate": detail.get("tp_rate"),
                    "time_stop_date": detail.get("time_stop_date"),
                    "opened_at": leg["resolved_at"] or stamp, "detail_json": "{}",
                }, stamp)
                self._swing_event(conn, "transition", stamp, trade_id=trade_id,
                                  idea_id=detail.get("swing_idea_id"), origin_cycle=origin,
                                  to_state=target, reason=f"created_from_filled_leg:{decision_id}:{seq}",
                                  actor=actor)
                return self._trade_in(conn, trade_id)
            trade_id = row["trade_id"]
            if row["state"] in TRADE_TERMINAL_STATES:
                raise IllegalTransition(f"swing trade {trade_id} is terminal ({row['state']})")
            if leg["direction"] != row["side"] or (
                    row["instrument_id"] is not None and leg["instrument_id"] is not None
                    and int(row["instrument_id"]) != int(leg["instrument_id"])):
                raise LedgerError(f"leg {decision_id}:{seq} ({leg['direction']}, instrument "
                                  f"{leg['instrument_id']}) does not match swing trade {trade_id}")
            if row["decision_id"] is None:     # link the leg the first time a fill names this trade
                conn.execute("UPDATE swing_trades SET decision_id = ?, entry_seq = ? WHERE trade_id = ?",
                             (decision_id, seq, trade_id))
            path = {"proposed": ["entry_executing", target], "entry_executing": [target],
                    "entry_unknown": [target]}.get(row["state"], [])
            reason = f"filled_leg:{decision_id}:{seq}"
            for step in path:
                self._move_trade(conn, self._trade_row(conn, trade_id), step, stamp,
                                 reason=reason, actor=actor)
            fresh = self._trade_row(conn, trade_id)
            conn.execute(
                """UPDATE swing_trades SET position_ids_json = ?, units = COALESCE(?, units),
                     open_rate = COALESCE(open_rate, ?), opened_at = COALESCE(opened_at, ?),
                     updated_at = ? WHERE trade_id = ?""",
                (json.dumps(_merge_ids(json.loads(fresh["position_ids_json"]), ids)),
                 units if path else None, open_rate, leg["resolved_at"] or stamp, stamp, trade_id))
            return self._trade_in(conn, trade_id)

    @staticmethod
    def _trade_row(conn: sqlite3.Connection, trade_id: str) -> sqlite3.Row:
        return conn.execute("SELECT * FROM swing_trades WHERE trade_id = ?", (trade_id,)).fetchone()

    def _trade_in(self, conn: sqlite3.Connection, trade_id: str) -> SwingTradeRow:
        return _swing_trade(self._trade_row(conn, trade_id))

    # benchmark and paper tracking (SW-6 fills them; code only, no calls)
    def record_benchmark_day(
        self, day: str, *, sq8_ret: float | None, matched_idx_ret: float | None,
        idx_hold_ret: float | None, detail: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """One paper-benchmark day (returns as fractions); re-recording a day replaces it."""
        for value in (sq8_ret, matched_idx_ret, idx_hold_ret):
            if value is not None and not math.isfinite(float(value)):
                raise LedgerError(f"benchmark return must be finite: {value!r}")
        _check_public_detail(dict(detail or {}), "benchmark day detail")
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO benchmark_days (day, sq8_ret, matched_idx_ret, idx_hold_ret, detail_json,
                     recorded_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (day) DO UPDATE SET sq8_ret = excluded.sq8_ret,
                     matched_idx_ret = excluded.matched_idx_ret, idx_hold_ret = excluded.idx_hold_ret,
                     detail_json = excluded.detail_json, recorded_at = excluded.recorded_at""",
                (day, sq8_ret, matched_idx_ret, idx_hold_ret, _dump(dict(detail or {})), self._now(now)))

    def benchmark_days(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM benchmark_days ORDER BY day").fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"] or "{}")} for r in rows]

    def add_paper_trade(
        self, paper_id: str, *, origin_cycle: str, ticker: str, side: str, opened_at: datetime,
        idea_id: str | None = None, entry_ref: float | None = None, stop_pct: float | None = None,
        target_pct: float | None = None, time_stop_date: str | None = None,
        record: Mapping[str, Any] | None = None, now: datetime | None = None,
    ) -> None:
        """A paper-tracked idea (every idea is paper-tracked, traded or not)."""
        stamp = self._now(now)
        with self._tx() as conn:
            try:
                conn.execute(
                    """INSERT INTO paper_trades (paper_id, idea_id, origin_cycle, ticker, side, status,
                         entry_ref, stop_pct, target_pct, time_stop_date, opened_at, record_json,
                         created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (paper_id, idea_id, origin_cycle, ticker, side, entry_ref, stop_pct, target_pct,
                     time_stop_date, ts(opened_at), _dump(dict(record or {})), stamp, stamp))
            except sqlite3.IntegrityError as exc:
                raise LedgerError(f"paper trade {paper_id}: {exc}") from exc

    def close_paper_trade(self, paper_id: str, *, exit_reason: str, ret_pct: float,
                          closed_at: datetime, now: datetime | None = None) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                """UPDATE paper_trades SET status = 'closed', exit_reason = ?, ret_pct = ?, closed_at = ?,
                     updated_at = ? WHERE paper_id = ? AND status = 'open'""",
                (exit_reason, ret_pct, ts(closed_at), self._now(now), paper_id))
            if cur.rowcount != 1:
                raise LedgerError(f"paper trade {paper_id} is unknown or already closed")

    def paper_trades(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._read() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM paper_trades ORDER BY opened_at, paper_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM paper_trades WHERE status = ? ORDER BY opened_at, paper_id",
                                    (status,)).fetchall()
        return [{**dict(r), "record": json.loads(r["record_json"] or "{}")} for r in rows]


_CODE_STRING = re.compile(r"[A-Za-z0-9_.:+\-]{0,64}")
# keys a public-facing row never carries (percent-only record, design §7.3)
_PRIVATE_KEY_PARTS = ("price", "units", "amount", "usd", "position_id", "instrument_id", "open_rate",
                      "close_rate", "sl_rate", "tp_rate", "fill_rate", "equity", "balance")


def _check_trade_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """`swing_trades.detail_json` holds numbers, booleans and short id/code strings only: a trade
    row becomes immutable when closed, so free text (which may copy licensed feed text) could never be
    scrubbed by the 7-day purge; such text belongs in `swing_events`."""
    def walk(value: Any, where: str) -> None:
        if isinstance(value, str):
            if not _CODE_STRING.fullmatch(value):
                raise LedgerError(f"swing trade detail {where}: free text is not allowed (ids/codes only)")
        elif isinstance(value, float) and not math.isfinite(value):
            raise LedgerError(f"swing trade detail {where}: non-finite number")
        elif isinstance(value, Mapping):
            for k, v in value.items():
                walk(str(k), f"{where}.key")
                walk(v, f"{where}.{k}")
        elif isinstance(value, list | tuple):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]")
        elif value is not None and not isinstance(value, bool | int | float):
            raise LedgerError(f"swing trade detail {where}: unsupported {type(value).__name__}")
    walk(detail, "detail")
    return detail


def _check_public_detail(detail: Mapping[str, Any], what: str) -> None:
    """A public-facing row's detail never names a price, units, amounts or broker ids (§7.3)."""
    for key, value in detail.items():
        low = str(key).lower()
        if any(part in low for part in _PRIVATE_KEY_PARTS):
            raise LedgerError(f"{what}: key {key!r} is private (percent-only public record)")
        if isinstance(value, Mapping):
            _check_public_detail(value, what)
        elif isinstance(value, list | tuple):
            for item in value:
                if isinstance(item, Mapping):
                    _check_public_detail(item, what)


def _merge_ids(existing: Iterable[Any], new: Iterable[Any]) -> list[int]:
    """Union of broker position ids, first-seen order (a trade never loses a position id)."""
    out: dict[int, None] = {}
    for value in (*existing, *new):
        if value is not None:
            out.setdefault(int(value), None)
    return list(out)


@dataclass(frozen=True)
class SwingTradeRow:
    trade_id: str
    idea_id: str | None
    origin_cycle: str | None
    decision_id: str | None
    entry_seq: int | None
    ticker: str
    instrument_id: int | None
    side: str
    state: str
    position_ids: list[int]
    units: float | None
    open_rate: float | None
    sl_rate: float | None
    tp_rate: float | None
    time_stop_date: str | None
    close_rate: float | None
    opened_at: datetime | None
    closed_at: datetime | None
    detail: dict[str, Any]
    created_at: datetime
    updated_at: datetime


def _swing_trade(row: sqlite3.Row) -> SwingTradeRow:
    return SwingTradeRow(
        trade_id=row["trade_id"], idea_id=row["idea_id"], origin_cycle=row["origin_cycle"],
        decision_id=row["decision_id"], entry_seq=row["entry_seq"], ticker=row["ticker"],
        instrument_id=row["instrument_id"], side=row["side"], state=row["state"],
        position_ids=json.loads(row["position_ids_json"] or "[]"), units=row["units"],
        open_rate=row["open_rate"], sl_rate=row["sl_rate"], tp_rate=row["tp_rate"],
        time_stop_date=row["time_stop_date"], close_rate=row["close_rate"],
        opened_at=parse_ts(row["opened_at"]), closed_at=parse_ts(row["closed_at"]),
        detail=json.loads(row["detail_json"] or "{}"),
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
    )


def _swing_idea(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["record"] = json.loads(out.pop("record_json") or "{}")
    out["carry_cycles"] = json.loads(out.pop("carry_cycles_json") or "[]")
    return out
