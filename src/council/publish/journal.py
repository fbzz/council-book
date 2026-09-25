"""Journal layout and document writers. The journal is written ONLY by the publisher.

Layout (every path is under `journal/`; `cycle_id` is the only key):
  journal/status.json
  journal/commitments/YYYY/MM/<cycle>.json         sealed before approval
  journal/cycles/YYYY/MM/<cycle>.json              revealed cycle: the EXACT sealed bytes
  journal/cycles/YYYY/MM/<cycle>.reveal.json       its salt
  journal/executions/YYYY/MM/<cycle>.json          final outcome of an executed decision
  journal/book/latest.json
  journal/performance/index.jsonl                  one row per day (as_of)
  journal/ops/cycles.jsonl                         one row per cycle
  journal/incidents/INC-####.md

Writers return `{relpath: bytes}` for `Publisher.publish`; they never touch git themselves.
A reveal is emitted only if the exact sealed bytes re-hash to the published commitment. The
sealed document is never rebuilt: the final decision outcome is published in the ops row
(`ops_files`) and, for executed decisions, the execution file (`execution_files`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from council.publish import commit_reveal
from council.publish.public_models import (
    CYCLE_ID_PATTERN,
    PublicBook,
    PublicCommitment,
    PublicCycleV1,
    PublicExecution,
    PublicIncident,
    PublicOpsRow,
    PublicPerformancePoint,
    PublicReveal,
    PublicStatus,
)

JOURNAL = "journal"
STATUS_PATH = f"{JOURNAL}/status.json"
BOOK_PATH = f"{JOURNAL}/book/latest.json"
PERFORMANCE_PATH = f"{JOURNAL}/performance/index.jsonl"
OPS_PATH = f"{JOURNAL}/ops/cycles.jsonl"
INCIDENTS_DIR = f"{JOURNAL}/incidents"
_CYCLE_ID = re.compile(CYCLE_ID_PATTERN)
_INCIDENT_ID = re.compile(r"^INC-(\d{4})$")


class JournalError(ValueError):
    pass


def _month(cycle_id: str) -> str:
    if not _CYCLE_ID.match(cycle_id):
        raise JournalError(f"not a cycle id: {cycle_id!r}")
    return f"{cycle_id[0:4]}/{cycle_id[5:7]}"


def commitment_path(cycle_id: str) -> str:
    return f"{JOURNAL}/commitments/{_month(cycle_id)}/{cycle_id}.json"


def cycle_path(cycle_id: str) -> str:
    return f"{JOURNAL}/cycles/{_month(cycle_id)}/{cycle_id}.json"


def reveal_path(cycle_id: str) -> str:
    return f"{JOURNAL}/cycles/{_month(cycle_id)}/{cycle_id}.reveal.json"


def execution_path(cycle_id: str) -> str:
    return f"{JOURNAL}/executions/{_month(cycle_id)}/{cycle_id}.json"


def incident_path(incident_id: str) -> str:
    if not _INCIDENT_ID.match(incident_id):
        raise JournalError(f"not an incident id: {incident_id!r}")
    return f"{INCIDENTS_DIR}/{incident_id}.md"


def dump_json(doc: BaseModel) -> bytes:
    """Readable, stable JSON (sorted keys, 2-space indent, trailing newline)."""
    return (json.dumps(doc.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _jsonl_line(doc: BaseModel) -> str:
    return json.dumps(doc.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------------------ writers
def commitment_files(commitment: PublicCommitment) -> dict[str, bytes]:
    return {commitment_path(commitment.cycle_id): dump_json(commitment)}


def reveal_files(sealed: bytes, salt_hex: str, commitment: PublicCommitment | str) -> dict[str, bytes]:
    """The revealed cycle (the EXACT sealed bytes, unchanged) and its salt.

    `commitment` is the PUBLISHED commitment (or its sha256). Refuses, before anything is written,
    bytes that do not open the commitment, that are not the canonical JSON of a valid
    `PublicCycleV1`, or whose cycle id differs from the commitment's."""
    if not isinstance(sealed, bytes | bytearray):
        raise JournalError("a reveal takes the exact sealed bytes")
    sealed = bytes(sealed)
    sha = commitment.commitment_sha256 if isinstance(commitment, PublicCommitment) else str(commitment)
    if not commit_reveal.verify_bytes(sealed, salt_hex, sha):
        raise JournalError("sealed bytes and salt do not open the commitment")
    try:
        cycle = PublicCycleV1.model_validate_json(sealed)
    except ValidationError as exc:
        raise JournalError(f"sealed bytes are not a valid public cycle ({exc.error_count()} errors)") from None
    if commit_reveal.canonical_json(cycle) != sealed:
        raise JournalError(f"sealed bytes for {cycle.cycle_id} are not the canonical public document")
    if isinstance(commitment, PublicCommitment) and commitment.cycle_id != cycle.cycle_id:
        raise JournalError("commitment and cycle ids differ")
    reveal = PublicReveal(cycle_id=cycle.cycle_id, salt=salt_hex.lower(), commitment_sha256=sha.lower())
    return {cycle_path(cycle.cycle_id): sealed, reveal_path(cycle.cycle_id): dump_json(reveal)}


def status_files(status: PublicStatus) -> dict[str, bytes]:
    return {STATUS_PATH: dump_json(status)}


def book_files(book: PublicBook) -> dict[str, bytes]:
    return {BOOK_PATH: dump_json(book)}


def execution_files(execution: PublicExecution) -> dict[str, bytes]:
    return {execution_path(execution.cycle_id): dump_json(execution)}


def _upsert_jsonl(existing: bytes | None, rows: Iterable[BaseModel], key: str) -> bytes:
    by_key: dict[str, str] = {}
    for raw in (existing or b"").decode().splitlines():
        if raw.strip():
            obj = json.loads(raw)
            by_key[str(obj[key])] = raw.strip()
    for row in rows:
        obj = row.model_dump(mode="json")
        by_key[str(obj[key])] = _jsonl_line(row)
    return ("".join(by_key[k] + "\n" for k in sorted(by_key))).encode()


def ops_files(existing: bytes | None, rows: Iterable[PublicOpsRow]) -> dict[str, bytes]:
    """Append or replace ops rows by cycle_id (idempotent)."""
    return {OPS_PATH: _upsert_jsonl(existing, rows, "cycle_id")}


def performance_files(existing: bytes | None, points: Iterable[PublicPerformancePoint]) -> dict[str, bytes]:
    """Append or replace performance points by day (idempotent)."""
    return {PERFORMANCE_PATH: _upsert_jsonl(existing, points, "as_of")}


def incident_markdown(incident: PublicIncident) -> bytes:
    """Markdown with a YAML front matter holding the structured fields."""
    meta = incident.model_dump(mode="json", exclude={"summary"})
    front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).strip()
    body = f"# {incident.incident_id} · {incident.title}\n\n{incident.summary.strip()}\n"
    return f"---\n{front}\n---\n\n{body}".encode()


def incident_files(incident: PublicIncident) -> dict[str, bytes]:
    return {incident_path(incident.incident_id): incident_markdown(incident)}


def parse_incident(text: str) -> PublicIncident:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise JournalError("incident file has no front matter")
    meta: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    body = re.sub(r"^#[^\n]*\n+", "", body, count=1).strip()
    return PublicIncident.model_validate({**meta, "summary": body})


def next_incident_id(existing: Iterable[str]) -> str:
    """Next INC-#### after the highest existing id (file names or ids)."""
    numbers = [int(m.group(1)) for name in existing if (m := re.search(r"INC-(\d{4})", name))]
    return f"INC-{(max(numbers) + 1) if numbers else 1:04d}"


# ------------------------------------------------------------------------------ local writing
def write_files(root: Path, files: Mapping[str, bytes]) -> list[Path]:
    """Write `{relpath: bytes}` under `root` (dry runs and site previews). Paths stay inside root."""
    root = root.resolve()
    written = []
    for rel, data in files.items():
        target = (root / rel).resolve()
        if root not in target.parents:
            raise JournalError(f"path escapes the output root: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
    return written
