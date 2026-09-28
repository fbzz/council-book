"""Capability gates (m5-readiness §5, package M5-D1).

Some broker behaviour can be proven only with the Agent Portfolio token: a real fill and the
human's look at the mirror, or a recorded token-day fixture. Every such behaviour is a capability
that stays OFF (fail closed) until it is proven.

Two kinds of proof:

- **Smoke capabilities** (`SMOKE_STEPS`): `state_dir/account/capabilities.json` (0600, private,
  schema-versioned) holds `{verified, at, decision_id, step, how}` per capability, plus the human
  mirror checks (`mirror-copied`, `mirror-sl-equal`) per smoke decision. It is written only by
  `council smoke verify` (M5-D2) and `council ops attest`, both operator-only and release-pinned
  (`write_capability` / `write_mirror_check` repeat both checks). A capability counts as true only
  when (M-5) the record says verified AND the ledger holds a COMPLETED decision of kind `smoke` with
  that decision id, whose `target_json.smoke_step` is the record's step, with at least one filled
  leg, AND both human mirror checks are attested for that same decision. A record that fails the
  cross-check reads false and is reported as `capability_unproven:<cap>`.
- **Evidence capabilities** (`EVIDENCE`): the other token-only unknowns (rates entitlement, `/costs`
  behaviour, the status-11 cancel route, where the $1 fee is charged, currency and price units, the
  terms version). Each is true only when the operator-written readiness record says so: a green gate
  of `doctor --live-read` (which records the private fixtures) or of `ops attest`, or an attested
  item. Records are written only through `readiness.write_record`, which carries the operator guard.

A missing or unreadable file, record or ledger means every capability is false. Before the token
there is no broker, and the cycle, planner and watch consult the gates only with a connected broker,
so nothing changes before the token. Public output carries codes only (`capability_missing:<cap>`,
`capability_unproven:<cap>`), never numbers. This module never imports the broker writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from council import paths

SCHEMA_VERSION = 1
FILE = Path("account") / "capabilities.json"
LEDGER_FILE = "ledger.sqlite3"            # council.ledger.db.LEDGER_FILE (not imported: keep this light)
SMOKE_KIND = "smoke"
SMOKE_STEP_KEY = "smoke_step"             # M5-D2 contract: target_json["smoke_step"] of a smoke decision
FILLED_LEG_STATES = ("filled", "partially_filled")
MIRROR_ITEMS = ("mirror-copied", "mirror-sl-equal")
MISSING = "capability_missing"
UNPROVEN = "capability_unproven"

# capability -> the smoke steps that prove it (§5 table)
SMOKE_STEPS: dict[str, tuple[str, ...]] = {
    "real_etf": ("S1",),
    "sl_modify": ("S2",),
    "partial_close": ("S3",),
    "crypto_real": ("S5",),
    "cfd_long": ("S6", "S6a"),
    "cfd_short": ("S6",),
    "cfd_leverage": ("S6b",),
    "stock_fractional": ("S7",),
    # swing book (swing-book.md rev 2, SW-5): every one stays false until its Track S smoke step is
    # verified and both mirror checks are attested. `tp_on_open` is the design's
    # `tp_on_open_or_patch`: it is proven when S7's open body carried the take-profit and the position
    # kept it (the planner then sends it in the body); without it the planner uses the ledgered
    # `modify_tp` PATCH, whose route S7t proves together with the broker minimum (`tp_min_pct`).
    "stock_real_long": ("S7",),
    "tp_on_open": ("S7",),
    "tp_min_pct": ("S7", "S7t"),
    "stock_cfd_short": ("S8",),
    "cfd_short_mirror": ("S8",),
    "stock_short_carry": ("S8x",),
    "closed_trade_route": ("S7x", "S8x"),
}
SWING_CAPABILITIES: tuple[str, ...] = ("stock_real_long", "tp_on_open", "tp_min_pct", "stock_cfd_short",
                                       "cfd_short_mirror", "stock_short_carry", "closed_trade_route")


@dataclass(frozen=True)
class Evidence:
    record: str            # readiness record name
    gate: str | None = None           # green gate id in that record
    attested: str | None = None       # attested item in that record
    code_flag: bool = False           # also needs the code flip (cancel route)
    what: str = ""


EVIDENCE: dict[str, Evidence] = {
    "rates_entitled": Evidence("live-read", gate="K7", what="agent token entitled to market-data rates"),
    "costs_whatif": Evidence("live-read", gate="K9", what="/costs what-if within the floors"),
    "cancel_route": Evidence("live-read", gate="K17", code_flag=True, what="status-11 cancel route"),
    "fee_location": Evidence("attest", gate="K15", what="$1 fee location equals costs.yaml"),
    "price_units": Evidence("live-read", gate="K19", what="currency, price unit, whole units, SL bounds"),
    "terms_version": Evidence("attest", attested="terms-version", what="terms version reviewed"),
}

CAPABILITIES: tuple[str, ...] = (*SMOKE_STEPS, *EVIDENCE)
# the capabilities a connected cycle needs to plan any open at all
OPEN_PREREQUISITES = ("rates_entitled", "price_units")
HOWS = ("smoke_verify", "attest")


class CapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capabilities:
    """The effective gates. `verified` holds the capabilities that passed every check; `unproven`
    those whose record claims verified but fails the ledger / mirror cross-check."""

    verified: frozenset[str] = frozenset()
    unproven: frozenset[str] = frozenset()
    connected: bool = True
    notes: Mapping[str, str] = field(default_factory=dict)

    def has(self, cap: str) -> bool:
        if cap not in CAPABILITIES:
            raise CapabilityError(f"unknown capability {cap!r}")
        return cap in self.verified

    def missing(self, caps: Iterable[str] | None = None) -> list[str]:
        return [c for c in (CAPABILITIES if caps is None else caps) if c not in self.verified]

    def flags(self, caps: Iterable[str] | None = None) -> list[str]:
        """`capability_missing:<cap>` for every false one, plus `capability_unproven:<cap>`."""
        chosen = list(CAPABILITIES if caps is None else caps)
        out = [f"{MISSING}:{c}" for c in chosen if c not in self.verified]
        out += [f"{UNPROVEN}:{c}" for c in chosen if c in self.unproven]
        return out

    def allows_vehicle(self, required: Iterable[str]) -> bool:
        return all(c in self.verified for c in required)

    @classmethod
    def all_verified(cls) -> Capabilities:
        """Tests and the rehearsal only (never read from the environment)."""
        return cls(verified=frozenset(CAPABILITIES))


NONE = Capabilities()


# ------------------------------------------------------------------------------ file
def path_for(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else paths.state_dir()) / FILE


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def validate(data: Any) -> dict[str, Any]:
    """The file's schema; raises CapabilityError. Codes, ids, booleans and times only."""
    if not isinstance(data, dict) or set(data) - {"schema", "capabilities", "mirror"}:
        raise CapabilityError("capabilities.json keys must be schema, capabilities, mirror")
    if data.get("schema") != SCHEMA_VERSION:
        raise CapabilityError("capabilities.json schema version mismatch")
    caps = data.get("capabilities", {})
    if not isinstance(caps, dict):
        raise CapabilityError("'capabilities' must be a mapping")
    for cap, rec in caps.items():
        if cap not in SMOKE_STEPS:
            raise CapabilityError(f"unknown smoke capability {cap!r}")
        if not isinstance(rec, dict) or set(rec) != {"verified", "at", "decision_id", "step", "how"}:
            raise CapabilityError(f"{cap}: needs verified, at, decision_id, step, how")
        if not isinstance(rec["verified"], bool) or not _valid_time(rec["at"]) \
                or not isinstance(rec["decision_id"], str) or not rec["decision_id"] \
                or rec["step"] not in SMOKE_STEPS[cap] or rec["how"] not in HOWS:
            raise CapabilityError(f"{cap}: malformed record")
    mirror = data.get("mirror", {})
    if not isinstance(mirror, dict):
        raise CapabilityError("'mirror' must be a mapping")
    for decision_id, items in mirror.items():
        if not isinstance(decision_id, str) or not isinstance(items, dict):
            raise CapabilityError("mirror checks must be {decision_id: {item: {value, at}}}")
        for item, entry in items.items():
            if item not in MIRROR_ITEMS or not isinstance(entry, dict) or set(entry) != {"value", "at"} \
                    or not isinstance(entry["value"], bool) or not _valid_time(entry["at"]):
                raise CapabilityError(f"mirror check {item!r}: malformed")
    return data


def read_file(state_dir: Path | None = None) -> dict[str, Any]:
    """The validated file, or an empty one when missing or invalid (everything false)."""
    path = path_for(state_dir)
    try:
        return validate(json.loads(path.read_text()))
    except (OSError, ValueError, CapabilityError):
        return {"schema": SCHEMA_VERSION, "capabilities": {}, "mirror": {}}


def _write_file(state_dir: Path | None, data: dict[str, Any]) -> Path:
    validate(data)
    path = path_for(state_dir)
    paths.assert_outside_repo(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def _guard(root: Path, assert_operator: Callable[[], None] | None,
           assert_release: Callable[[], None] | None) -> None:
    if assert_operator is None:
        from council.operator import guards

        assert_operator = guards.assert_current_process_is_operator
    assert_operator()
    if assert_release is None:
        from council.operator.release import assert_release_code

        def assert_release() -> None:
            assert_release_code(state_dir=root, argv=["(capabilities record)"])
    assert_release()


def write_capability(cap: str, *, decision_id: str, step: str, how: str = "smoke_verify",
                     verified: bool = True, state_dir: Path | None = None, now: datetime | None = None,
                     assert_operator: Callable[[], None] | None = None,
                     assert_release: Callable[[], None] | None = None) -> Path:
    """Record one smoke capability (`council smoke verify`, M5-D2). Operator terminal and installed
    release only; unknown capabilities and steps are refused."""
    if cap not in SMOKE_STEPS:
        raise CapabilityError(f"unknown smoke capability {cap!r}")
    if step not in SMOKE_STEPS[cap]:
        raise CapabilityError(f"{cap} is proven by {'/'.join(SMOKE_STEPS[cap])}, not {step}")
    root = state_dir if state_dir is not None else paths.state_dir()
    _guard(root, assert_operator, assert_release)
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    data = read_file(root)
    data["capabilities"][cap] = {"verified": bool(verified), "at": at, "decision_id": decision_id,
                                 "step": step, "how": how}
    return _write_file(root, data)


def write_mirror_check(item: str, *, decision_id: str, value: bool = True, state_dir: Path | None = None,
                       now: datetime | None = None, assert_operator: Callable[[], None] | None = None,
                       assert_release: Callable[[], None] | None = None) -> Path:
    """Record one human mirror check for a smoke decision (`council ops attest mirror-copied
    --decision <id>`). Operator terminal and installed release only."""
    if item not in MIRROR_ITEMS:
        raise CapabilityError(f"unknown mirror check {item!r}")
    if not decision_id:
        raise CapabilityError("a mirror check needs --decision <smoke decision id>")
    root = state_dir if state_dir is not None else paths.state_dir()
    _guard(root, assert_operator, assert_release)
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    data = read_file(root)
    data["mirror"].setdefault(decision_id, {})[item] = {"value": bool(value), "at": at}
    return _write_file(root, data)


# ------------------------------------------------------------------------------ cross-checks
def smoke_proof(state_dir: Path, decision_id: str, step: str) -> bool:
    """A completed `smoke` decision with this id and step, with at least one filled leg (read-only)."""
    path = state_dir / LEDGER_FILE
    if not path.is_file():
        return False
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("SELECT kind, state, target_json FROM decisions WHERE decision_id = ?",
                          (decision_id,)).fetchone()
        if row is None or row[0] != SMOKE_KIND or row[1] != "completed":
            return False
        try:
            target = json.loads(row[2] or "{}")
        except ValueError:
            return False
        if not isinstance(target, dict) or target.get(SMOKE_STEP_KEY) != step:
            return False
        marks = ",".join("?" for _ in FILLED_LEG_STATES)
        fills = con.execute(f"SELECT COUNT(*) FROM legs WHERE decision_id = ? AND state IN ({marks})",
                            (decision_id, *FILLED_LEG_STATES)).fetchone()[0]
        return int(fills) > 0
    except sqlite3.Error:
        return False
    finally:
        con.close()


def _readiness_record(state_dir: Path, name: str) -> dict[str, Any]:
    from council.operator import readiness

    path = state_dir / "readiness" / f"{name}.json"
    try:
        data = json.loads(path.read_text())
        readiness.validate_record(name, data)
    except (OSError, ValueError, readiness.ReadinessError):
        return {}
    return data if isinstance(data, dict) else {}


def _evidence_ok(state_dir: Path, ev: Evidence, records: dict[str, dict[str, Any]],
                 code_flag: Callable[[], bool]) -> bool:
    rec = records.setdefault(ev.record, _readiness_record(state_dir, ev.record))
    if ev.gate is not None and ((rec.get("gates") or {}).get(ev.gate) or {}).get("state") != "green":
        return False
    if ev.attested is not None and ((rec.get("attested") or {}).get(ev.attested) or {}).get("value") is not True:
        return False
    return not ev.code_flag or code_flag()


def _cancel_route_code() -> bool:
    from council.operator.onboarding import cancel_route_verified  # ast read: no writer import

    return cancel_route_verified()


def load(state_dir: Path | None = None, *, code_flag: Callable[[], bool] | None = None) -> Capabilities:
    """The effective capabilities. Never raises: anything unreadable is false."""
    root = state_dir if state_dir is not None else paths.state_dir()
    verified: set[str] = set()
    unproven: set[str] = set()
    notes: dict[str, str] = {}
    try:
        data = read_file(root)
        for cap, rec in data["capabilities"].items():
            if not rec["verified"]:
                continue
            mirror = data["mirror"].get(rec["decision_id"], {})
            human = all((mirror.get(i) or {}).get("value") is True for i in MIRROR_ITEMS)
            if smoke_proof(root, rec["decision_id"], rec["step"]) and human:
                verified.add(cap)
            else:
                unproven.add(cap)
                notes[cap] = "no_completed_smoke_fill" if human else "mirror_unattested"
        records: dict[str, dict[str, Any]] = {}
        for cap, ev in EVIDENCE.items():
            if _evidence_ok(root, ev, records, code_flag or _cancel_route_code):
                verified.add(cap)
    except Exception:  # noqa: BLE001 - a gate read never raises; everything stays false
        return Capabilities(notes={"*": "unreadable"})
    return Capabilities(verified=frozenset(verified), unproven=frozenset(unproven), notes=notes)


def alert_unproven(ctx: Any, caps: Capabilities, now: datetime) -> None:
    """One URGENT alert per unproven capability per window (codes only). Never raises."""
    if not caps.unproven:
        return
    try:
        from council.operator import urgent

        for cap in sorted(caps.unproven):
            urgent.send_once(ctx, f"{UNPROVEN}:{cap}", "council capability",
                             f"URGENT {UNPROVEN}:{cap}: capabilities.json claims it but the ledger has no "
                             "completed smoke decision with fills (or the mirror check is missing). "
                             "It is treated as false.", now)
    except Exception:  # noqa: BLE001 - an alert failure never stops a cycle; the flag is still published
        return


def report_lines(caps: Capabilities) -> list[str]:
    """`council ops capabilities`: one line per capability, codes only."""
    out = []
    for cap in CAPABILITIES:
        if cap in caps.verified:
            state = "ok  "
        elif cap in caps.unproven:
            state = "BAD "
        else:
            state = "off "
        how = (f"smoke {'/'.join(SMOKE_STEPS[cap])}" if cap in SMOKE_STEPS
               else f"{EVIDENCE[cap].record} {EVIDENCE[cap].gate or EVIDENCE[cap].attested}")
        tail = f" ({UNPROVEN}: {caps.notes.get(cap, '')})" if cap in caps.unproven else ""
        out.append(f"{state} {cap:<17} proven by {how}{tail}")
    return out
