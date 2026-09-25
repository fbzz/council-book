"""The private ledger: one SQLite file (WAL) outside the repository.

Rules:
- Every state change goes through `transition`, which enforces ledger/states.py and is a
  compare-and-set (a concurrent writer cannot move a decision from a state it no longer has).
- `create_decision` applies priority atomically: flatten > compliance > rebalance. A new decision
  supersedes every PENDING decision of equal or lower priority; if a pending decision of HIGHER
  priority exists the new one is refused (PriorityConflict) and nothing is written.
- `expire_stale(now)` is ONE `UPDATE … RETURNING` (no read-then-write race), run eagerly.
- `has_blocker()` is true while any decision is blocked / execution_unknown or any leg is unknown.
- Leg rows are written once per decision (state `planned`) and then only moved along the leg
  state machine; a leg's request_id is UNIQUE across the ledger.
- Broker payloads are stored with credential-like keys redacted. The file never lives in the repo.
"""

from __future__ import annotations

import json
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
    BLOCKER_STATES,
    DECISION_STATES,
    LEG_STATES,
    LEG_TERMINAL_STATES,
    PENDING_STATES,
    PRIORITY,
    DecisionKind,
    can_transition,
    can_transition_leg,
)
from council.models.broker import Position
from council.models.plan import Leg
from council.paths import assert_outside_repo, state_dir

SCHEMA_VERSION = 1
LEDGER_FILE = "ledger.sqlite3"
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_SENSITIVE_KEYS = ("authorization", "api_key", "api-key", "user_key", "user-key", "token", "secret", "password")


class LedgerError(RuntimeError):
    pass


class InvalidTransition(LedgerError):
    pass


class PriorityConflict(LedgerError):
    """A pending decision of higher priority exists; the new one was not created."""


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
                conn.executescript(schema)
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
        now: datetime | None = None,
    ) -> list[str]:
        """Create a pending decision; returns the ids it superseded. See the module rules."""
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
                conn.execute(
                    "INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at) VALUES (?, ?, 'superseded', ?, ?)",
                    (r["decision_id"], r["state"], f"superseded by {decision_id}", stamp),
                )
            conn.execute(
                """INSERT INTO decisions (decision_id, cycle_id, kind, priority, state, created_at,
                     updated_at, valid_until, target_json, plan_json, commitment_sha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id, cycle_id, kind, priority, state, stamp, stamp, ts(valid_until),
                    _dump(dict(target or {})), _dump(plan) if plan is not None else None,
                    commitment_sha,
                ),
            )
            conn.execute(
                "INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at) VALUES (?, NULL, ?, 'created', ?)",
                (decision_id, state, stamp),
            )
        return [r["decision_id"] for r in superseded]

    def get_decision(self, decision_id: str) -> DecisionRow:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown decision {decision_id}")
        return _decision(row)

    def transition(self, decision_id: str, to_state: str, reason: str, *, now: datetime | None = None) -> DecisionRow:
        """Move a decision along ALLOWED_TRANSITIONS (compare-and-set) and log the event."""
        if to_state not in DECISION_STATES:
            raise InvalidTransition(f"unknown decision state {to_state!r}")
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
            conn.execute(
                "INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (decision_id, from_state, to_state, reason, stamp),
            )
        return self.get_decision(decision_id)

    def note(self, decision_id: str, reason: str, *, now: datetime | None = None) -> None:
        """Log an event without a state change."""
        state = self.get_decision(decision_id).state
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (decision_id, state, state, reason, self._now(now)),
            )

    def events(self, decision_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT from_state, to_state, reason, created_at FROM decision_events WHERE decision_id = ? ORDER BY event_id",
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
                conn.execute(
                    "INSERT INTO decision_events (decision_id, from_state, to_state, reason, created_at) VALUES (?, ?, 'expired', 'valid_until passed', ?)",
                    (r["decision_id"], r["prev_state"], stamp),
                )
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
        """Decision ids that halt new risk: blocked / execution_unknown, or with an unknown leg."""
        placeholders = ",".join("?" * len(BLOCKER_STATES))
        with self._read() as conn:
            rows = conn.execute(
                f"""SELECT decision_id FROM decisions WHERE state IN ({placeholders})
                    UNION SELECT decision_id FROM legs WHERE state = 'unknown'""",
                tuple(BLOCKER_STATES),
            ).fetchall()
        return sorted(r["decision_id"] for r in rows)

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

    # ------------------------------------------------------------------ legs
    def insert_legs(
        self,
        decision_id: str,
        legs: Sequence[Leg],
        *,
        line_of: Callable[[str], str] | Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write a decision's legs once, all `planned` (line weights kept in `detail`)."""
        lookup: Callable[[str], str]
        if line_of is None:
            lookup = lambda s: s  # noqa: E731
        elif isinstance(line_of, Mapping):
            lookup = lambda s: line_of.get(s, s)  # type: ignore[union-attr]  # noqa: E731
        else:
            lookup = line_of
        stamp = self._now(now)
        with self._tx() as conn:
            exists = conn.execute("SELECT 1 FROM legs WHERE decision_id = ? LIMIT 1", (decision_id,)).fetchone()
            if exists:
                raise LedgerError(f"legs for {decision_id} were already written")
            for leg in legs:
                conn.execute(
                    """INSERT INTO legs (leg_id, decision_id, seq, kind, line, vehicle_symbol,
                         instrument_id, direction, settlement, leverage, units, amount_usd, sl_rate,
                         position_id, depends_on_json, risk_increasing, state, detail_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)""",
                    (
                        f"{decision_id}:{leg.seq}", decision_id, leg.seq, leg.kind, lookup(leg.symbol),
                        leg.symbol, leg.instrument_id, leg.direction, leg.settlement, leg.leverage,
                        leg.units, leg.amount_usd, leg.sl_rate, leg.position_id,
                        json.dumps(leg.depends_on), int(leg.risk_increasing),
                        _dump({
                            "weight_before": leg.weight_before, "weight_after": leg.weight_after,
                            "reason": leg.reason,
                        }),
                        stamp,
                    ),
                )

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
