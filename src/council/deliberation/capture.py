"""Private capture of every model call's exact input (transparency-v2 §2.2, T-D1, T-D3).

`InputSink` is handed to `run_council(input_sink=...)`. `call_role` registers each call's sections
and appends a `CallInput` with status `sent` BEFORE it awaits the gateway, so a call that times
out or is cancelled still has its input; the gateway's result then fills the status, every raw
reply, the checker's errors, the correction message, the first reply as sent back and every
failed HTTP attempt that resent the same messages (the gateway's timeout ladder).

Rules:
  - Capture never stops a cycle: any failure inside the sink sets `inputs_capture_error:<type>`
    and the council carries on (T-D11).
  - Sections are stored once per cycle and shared by key. A key that comes back with different
    content (a stage retry that got a new bull opening) is stored as `<key>#2`, `#3`, ...
  - Files are private: 0700 directories and 0600 files under the state dir, never inside the
    repository. Broker-licensed item texts go to `licensed/calls/` only (7-day retention, purged by
    `council purge-licensed`); the main file keeps their salted commits.
  - Salts are 32 random bytes per call and per section (`secrets.token_hex(32)`; injectable for
    tests). Nothing here reads a clock: the caller passes `captured_at`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from council.deliberation.segments import Segmented, joined
from council.models.inputs import CallInput, CycleInputs, LicensedInputs, PrivateSection

CALLS_DIR = "calls"
LICENSED_CALLS = ("licensed", "calls")
CAPTURE_ERROR = "inputs_capture_error"
PURGED_PLACEHOLDER = "[licensed text purged]"
_CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z(?:-[a-z0-9]+)*$")


def commit(salt_hex: str, text: str) -> str:
    """sha256(salt bytes || text): a commitment that a published digest cannot be brute forced from."""
    return hashlib.sha256(bytes.fromhex(salt_hex) + text.encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_messages(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps(list(messages), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def messages_for(system: str, user: str, *, sent_assistant: str = "", correction: str = "") -> list[dict[str, str]]:
    """The exact message list a gateway sent (the correction turn adds the reply as sent back)."""
    out = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if correction:
        out += [{"role": "assistant", "content": sent_assistant}, {"role": "user", "content": correction}]
    return out


class InputSink:
    """Collects the sections and calls of one council run (in memory; see `write_cycle_inputs`)."""

    def __init__(self, *, salt: Callable[[], str] | None = None) -> None:
        self._salt = salt or (lambda: secrets.token_hex(32))
        self.sections: dict[str, Segmented] = {}
        self.section_salts: dict[str, str] = {}
        self.calls: list[CallInput] = []
        self._users: list[str] = []
        self.flags: list[str] = []

    # -- recording ---------------------------------------------------------------------------
    def _flag(self, exc: BaseException) -> None:
        flag = f"{CAPTURE_ERROR}:{type(exc).__name__}"
        if flag not in self.flags:
            self.flags.append(flag)

    def register(self, section: Segmented) -> str:
        """The key this section is stored under (deduplicated by content)."""
        key, n = section.key, 1
        while key in self.sections:
            stored = self.sections[key]
            if stored.runs == section.runs and stored.items == section.items and stored.kind == section.kind:
                return key
            n += 1
            key = f"{section.key}#{n}"
        self.sections[key] = section if key == section.key else section.model_copy(update={"key": key})
        self.section_salts[key] = self._salt()
        return key

    def begin(
        self,
        *,
        role: str,
        replicate: int,
        attempt: int,
        seed: int,
        num_predict: int,
        prompt_id: str,
        prompt_sha: str,
        ctx: Mapping[str, Any],
        system: str,
        sections: Sequence[Segmented],
        input_hash: str,
    ) -> int | None:
        """Record a call about to be sent. Returns its index, or None when capture failed."""
        try:
            keys = [self.register(s) for s in sections]
            user = joined(sections)
            salt = self._salt()
            self.calls.append(CallInput(
                call_key=f"{role}:{replicate}:{attempt}", role=role, replicate=replicate,
                attempt=attempt, seed=int(seed), num_predict=int(num_predict), prompt_id=prompt_id,
                prompt_sha=prompt_sha, ctx=dict(ctx), system=system, sections=keys,
                input_hash=input_hash, salt=salt, input_commit=commit(salt, f"{system}\0{user}"),
            ))
            self._users.append(user)
            return len(self.calls) - 1
        except Exception as exc:  # capture must never stop the council
            self._flag(exc)
            return None

    def finish(self, index: int | None, result: Any) -> None:
        """Fill a recorded call from the gateway's `LLMResult`."""
        if index is None:
            return
        try:
            call = self.calls[index]
            raw = str(getattr(result, "raw", "") or "")
            turns = [str(t) for t in (getattr(result, "turns", ()) or ())] or ([raw] if raw else [])
            correction = str(getattr(result, "correction", "") or "")
            sent = str(getattr(result, "sent_assistant", "") or "")
            messages = messages_for(call.system, self._users[index], sent_assistant=sent,
                                    correction=correction)
            self.calls[index] = call.model_copy(update={
                "status": result.call.status,
                "error": result.call.error,
                "errors": [str(e) for e in (getattr(result, "errors", ()) or ())],
                "correction": correction,
                "sent_assistant": sent,
                "replies": turns,
                "retries": [str(r) for r in (getattr(result, "retries", ()) or ())],
                "messages_commit": commit(call.salt, canonical_messages(messages)),
            })
        except Exception as exc:
            self._flag(exc)

    # -- output ------------------------------------------------------------------------------
    def build(self, *, cycle_id: str, captured_at: datetime) -> tuple[CycleInputs, LicensedInputs | None]:
        """The private record, and the broker-licensed texts held apart (None when there are none)."""
        sections: dict[str, PrivateSection] = {}
        texts: dict[str, dict[str, str]] = {}
        for key, sec in self.sections.items():
            text = sec.text()
            salt = self.section_salts[key]
            held = sec.licensed_indices()
            items = [it.model_copy(update={"text": ""}) if i in held else it
                     for i, it in enumerate(sec.items)]
            sections[key] = PrivateSection(
                key=key, kind=sec.kind, runs=list(sec.runs), items=items, licensed=held,
                sha256=sha256_text(text), salt=salt, commit=commit(salt, text),
            )
            if held:
                texts[key] = {str(i): sec.items[i].text for i in held}
        n_held = sum(len(v) for v in texts.values())
        inputs = CycleInputs(cycle_id=cycle_id, captured_at=captured_at, sections=sections,
                             calls=list(self.calls), flags=list(self.flags), licensed_items=n_held)
        licensed = (LicensedInputs(cycle_id=cycle_id, captured_at=captured_at, texts=texts)
                    if texts else None)
        return inputs, licensed

    def user_of(self, index: int) -> str:
        return self._users[index]


# ------------------------------------------------------------------------------------ storage
def check_cycle_id(cycle_id: str) -> str:
    if not isinstance(cycle_id, str) or not _CYCLE_ID.match(cycle_id):
        raise ValueError("not a cycle id (YYYY-MM-DDTHHMMZ)")
    return cycle_id


def _month_parts(cycle_id: str) -> tuple[str, str]:
    check_cycle_id(cycle_id)
    return cycle_id[:4], cycle_id[5:7]


def calls_path(state_dir: Path, cycle_id: str) -> Path:
    yyyy, mm = _month_parts(cycle_id)
    return Path(state_dir) / CALLS_DIR / yyyy / mm / f"{cycle_id}.json.gz"


def licensed_path(state_dir: Path, cycle_id: str) -> Path:
    yyyy, mm = _month_parts(cycle_id)
    return Path(state_dir).joinpath(*LICENSED_CALLS) / yyyy / mm / f"{cycle_id}.json.gz"


def _private_dirs(root: Path, target_dir: Path) -> None:
    """Create `target_dir` under `root`, every new directory 0700."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rel = target_dir.resolve().relative_to(root.resolve())
    current = root
    for part in rel.parts:
        current = current / part
        if not current.exists():
            current.mkdir(mode=0o700)
        if current.is_dir() and (current.stat().st_mode & 0o777) != 0o700:
            os.chmod(current, 0o700)   # a folder created earlier (or by another tool) is tightened


def write_private(root: Path, path: Path, data: bytes) -> Path:
    """Write `data` to `path` (under `root`, outside the repo) atomically as a 0600 file."""
    from council.paths import assert_outside_repo

    assert_outside_repo(path)
    _private_dirs(root, path.parent)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def dump_gz(model: Any) -> bytes:
    return gzip.compress(model.model_dump_json().encode(), mtime=0)


def write_inputs(state_dir: Path, inputs: CycleInputs, licensed: LicensedInputs | None) -> Path:
    """Write the private record (and the licensed texts, if any). Returns the main file's path."""
    root = Path(state_dir)
    if licensed is not None:
        write_private(root, licensed_path(root, inputs.cycle_id), dump_gz(licensed))
    return write_private(root, calls_path(root, inputs.cycle_id), dump_gz(inputs))


def write_cycle_inputs(state_dir: Path, sink: InputSink, *, cycle_id: str, captured_at: datetime) -> list[str]:
    """The cycle hook's entry point: build and write the capture; never raises. Returns the flags
    to add to the cycle record (the sink's own capture errors plus any write error)."""
    flags = list(sink.flags)
    try:
        inputs, licensed = sink.build(cycle_id=cycle_id, captured_at=captured_at)
        write_inputs(state_dir, inputs, licensed)
    except Exception as exc:
        flag = f"{CAPTURE_ERROR}:{type(exc).__name__}"
        if flag not in flags:
            flags.append(flag)
    return flags


def _load_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def load_inputs(state_dir: Path, cycle_id: str) -> CycleInputs:
    """The private record of one cycle (FileNotFoundError when none was captured)."""
    return CycleInputs.model_validate_json(_load_gz(calls_path(state_dir, cycle_id)))


def load_licensed(state_dir: Path, cycle_id: str) -> LicensedInputs | None:
    path = licensed_path(state_dir, cycle_id)
    if not path.exists():
        return None
    return LicensedInputs.model_validate_json(_load_gz(path))


# ------------------------------------------------------------------------------ reconstruction
def section_parts(
    section: PrivateSection, licensed: LicensedInputs | None
) -> list[tuple[str, int | None, bool]]:
    """The section as (text, item index or None for a literal, available) pieces, with the held
    licensed texts restored when the licensed file still has them."""
    held = (licensed.texts.get(section.key, {}) if licensed is not None else {})
    out: list[tuple[str, int | None, bool]] = []
    for kind, value in section.runs:
        if kind == "t":
            out.append((str(value), None, True))
            continue
        idx = int(value)
        if idx in section.licensed:
            text = held.get(str(idx))
            out.append((text if text is not None else PURGED_PLACEHOLDER, idx, text is not None))
        else:
            out.append((section.items[idx].text, idx, True))
    return out


def section_text(section: PrivateSection, licensed: LicensedInputs | None) -> tuple[str, bool]:
    """(exact text, complete). Incomplete when a licensed text was purged: a placeholder stands in."""
    parts = section_parts(section, licensed)
    return "".join(p[0] for p in parts), all(p[2] for p in parts)


def user_text(inputs: CycleInputs, call: CallInput, licensed: LicensedInputs | None) -> tuple[str, bool]:
    texts = [section_text(inputs.sections[k], licensed) for k in call.sections]
    return "".join(t for t, _ in texts), all(ok for _, ok in texts)


def exact_messages(
    inputs: CycleInputs, call: CallInput, licensed: LicensedInputs | None
) -> list[dict[str, str]] | None:
    """The exact message list the gateway sent for `call` (None when licensed text was purged).
    A future `council inputs replay` resends exactly this."""
    user, complete = user_text(inputs, call, licensed)
    if not complete:
        return None
    return messages_for(call.system, user, sent_assistant=call.sent_assistant,
                        correction=call.correction)


def verify(
    inputs: CycleInputs,
    licensed: LicensedInputs | None,
    role_calls: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[str], list[str]]:
    """Private checks (§2.6). Returns (problems, notes): every section's sha256 and commit, every
    call's input hash, commit and messages commit, and every ledger `RoleCall.input_hash` that is
    not empty must belong to a captured call. Purged licensed text makes a check a note, not a
    problem (the input can be attested by its commit but no longer rebuilt)."""
    problems: list[str] = []
    notes: list[str] = []
    for key, sec in inputs.sections.items():
        text, complete = section_text(sec, licensed)
        if not complete:
            notes.append(f"section {key}: licensed text purged; commit kept, text not re-checkable")
            continue
        if sha256_text(text) != sec.sha256:
            problems.append(f"section {key}: sha256 mismatch")
        if commit(sec.salt, text) != sec.commit:
            problems.append(f"section {key}: commit mismatch")
    for call in inputs.calls:
        missing = [k for k in call.sections if k not in inputs.sections]
        if missing:
            problems.append(f"call {call.call_key}: missing sections {', '.join(missing)}")
            continue
        user, complete = user_text(inputs, call, licensed)
        if not complete:
            notes.append(f"call {call.call_key}: licensed text purged; input not re-checkable")
            continue
        if sha256_text(f"{call.system}\0{user}") != call.input_hash:
            problems.append(f"call {call.call_key}: input hash mismatch")
        if commit(call.salt, f"{call.system}\0{user}") != call.input_commit:
            problems.append(f"call {call.call_key}: input commit mismatch")
        if call.messages_commit:
            msgs = messages_for(call.system, user, sent_assistant=call.sent_assistant,
                                correction=call.correction)
            if commit(call.salt, canonical_messages(msgs)) != call.messages_commit:
                problems.append(f"call {call.call_key}: messages commit mismatch")
    captured = {c.input_hash for c in inputs.calls}
    for row in role_calls:
        ih = str(row.get("input_hash") or "")
        if ih and ih not in captured:
            problems.append(f"ledger call {row.get('role')}:{row.get('replicate', 0)}: input not captured")
    return problems, notes
