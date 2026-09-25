"""Commit-reveal pre-commitment.

Rules:
- canonical JSON = UTF-8, keys sorted, no whitespace, no NaN/Infinity.
- commitment = sha256(salt || canonical_json(doc)), salt = 32 random bytes (hex in the reveal).
- The commitment is pushed BEFORE a proposal can be approved; the document and its salt are
  revealed once the decision is final. Anyone can re-hash the revealed file to check the seal.
- The reveal publishes the EXACT sealed bytes, never a rebuilt document: `seal_bytes` returns
  them, the orchestrator keeps them with the salt in the private state dir (`save_sealed`, under
  `state_dir()/salts/<cycle_id>.json`, mode 0600), and `journal.reveal_files` writes them
  unchanged after re-verifying. The final decision outcome goes to the ops and execution files.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from council import paths
from council.clock import utcnow
from council.models.common import Strict
from council.publish.public_models import (
    COMMIT_ALGO,
    CYCLE_ID_PATTERN,
    PublicCommitment,
    PublicReveal,
)

SALT_BYTES = 32
_CYCLE_ID = re.compile(CYCLE_ID_PATTERN)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _as_json_obj(doc: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(doc, BaseModel):
        return doc.model_dump(mode="json")
    if not isinstance(doc, dict):
        raise TypeError("a sealed document must be a pydantic model or a dict")
    return doc


def canonical_json(doc: BaseModel | dict[str, Any]) -> bytes:
    """Deterministic bytes for hashing: same content -> same bytes, whatever the key order."""
    return json.dumps(
        _as_json_obj(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _salt(salt_hex: str) -> bytes:
    salt = bytes.fromhex(salt_hex)
    if len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be {SALT_BYTES} bytes")
    return salt


def commitment_sha256(doc: BaseModel | dict[str, Any], salt_hex: str) -> str:
    return bytes_commitment_sha256(canonical_json(doc), salt_hex)


def bytes_commitment_sha256(sealed: bytes, salt_hex: str) -> str:
    """sha256(salt || sealed bytes): the commitment over bytes exactly as they were sealed."""
    return hashlib.sha256(_salt(salt_hex) + bytes(sealed)).hexdigest()


def seal_bytes(
    doc: BaseModel | dict[str, Any],
    *,
    sealed_at: datetime | None = None,
    code_commit: str = "",
    salt_hex: str | None = None,
) -> tuple[PublicCommitment, str, bytes]:
    """Seal a public cycle document. Returns the public commitment, the PRIVATE salt (hex) and the
    EXACT bytes that were hashed (canonical JSON). Keep the bytes and the salt privately and reveal
    those bytes unchanged later; never rebuild the document for the reveal."""
    sealed = canonical_json(doc)
    obj = json.loads(sealed)
    salt_hex = salt_hex or secrets.token_bytes(SALT_BYTES).hex()
    commitment = PublicCommitment(
        cycle_id=obj["cycle_id"],
        commitment_sha256=bytes_commitment_sha256(sealed, salt_hex),
        algo=COMMIT_ALGO,
        sealed_at=sealed_at or utcnow(),
        code_commit=code_commit,
        prompt_manifest_sha=obj.get("prompt_manifest_sha", "") or "",
        policy_sha=obj.get("policy_sha", "") or "",
        model_digest=obj.get("model_digest", "") or "",
    )
    return commitment, salt_hex, sealed


def seal(
    doc: BaseModel | dict[str, Any],
    *,
    sealed_at: datetime | None = None,
    code_commit: str = "",
    salt_hex: str | None = None,
) -> tuple[PublicCommitment, str]:
    """Seal a public cycle document. Returns the public commitment and the PRIVATE salt (hex).
    Prefer `seal_bytes`: the reveal needs the exact sealed bytes."""
    commitment, salt, _ = seal_bytes(doc, sealed_at=sealed_at, code_commit=code_commit, salt_hex=salt_hex)
    return commitment, salt


def reveal(commitment: PublicCommitment, salt_hex: str) -> PublicReveal:
    return PublicReveal(
        cycle_id=commitment.cycle_id, salt=salt_hex, commitment_sha256=commitment.commitment_sha256,
    )


def verify(doc: BaseModel | dict[str, Any], salt: str, commitment_sha: str) -> bool:
    """True iff sha256(salt || canonical_json(doc)) equals the published commitment."""
    try:
        actual = commitment_sha256(doc, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, commitment_sha.lower())


def verify_bytes(sealed: bytes, salt: str, commitment_sha: str) -> bool:
    """True iff sha256(salt || sealed) equals the published commitment (no re-serialisation)."""
    try:
        actual = bytes_commitment_sha256(sealed, salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, str(commitment_sha).lower())


# ------------------------------------------------------------------------------ private store
class SealedCycle(Strict):
    """PRIVATE: what the orchestrator keeps between seal and reveal, as stored at
    `state_dir()/salts/<cycle_id>.json` (the same three keys the orchestrator writes)."""

    salt: str
    sealed_hex: str                 # hex of the exact sealed bytes (lossless)
    commitment_sha: str

    @classmethod
    def build(cls, commitment: PublicCommitment, salt_hex: str, sealed: bytes) -> SealedCycle:
        if not verify_bytes(sealed, salt_hex, commitment.commitment_sha256):
            raise ValueError("sealed bytes and salt do not open the commitment")
        return cls(salt=salt_hex, sealed_hex=bytes(sealed).hex(), commitment_sha=commitment.commitment_sha256)

    @property
    def sealed_bytes(self) -> bytes:
        return bytes.fromhex(self.sealed_hex)

    @property
    def cycle_id(self) -> str:
        return str(json.loads(self.sealed_bytes)["cycle_id"])


def salts_dir() -> Path:
    return paths.state_dir() / "salts"


def _sealed_path(cycle_id: str, directory: Path | None) -> Path:
    if not _CYCLE_ID.match(cycle_id):
        raise ValueError(f"not a cycle id: {cycle_id!r}")
    return (directory if directory is not None else salts_dir()) / f"{cycle_id}.json"


def save_sealed(sealed: SealedCycle, directory: Path | None = None, *, replace: bool = False) -> Path:
    """Write the private seal record (0600, atomic). Refuses a path inside the public repo, and
    refuses to overwrite a DIFFERENT seal for the same cycle unless `replace` (a second seal
    would orphan an already published commitment)."""
    if not (_HEX64.match(sealed.salt) and _HEX64.match(sealed.commitment_sha)):
        raise ValueError("salt and commitment must be 64 lowercase hex characters")
    if not verify_bytes(sealed.sealed_bytes, sealed.salt, sealed.commitment_sha):
        raise ValueError("sealed bytes and salt do not open the commitment")
    path = _sealed_path(sealed.cycle_id, directory)
    paths.assert_outside_repo(path)
    if path.exists() and not replace:
        existing = SealedCycle.model_validate_json(path.read_text())
        if existing.commitment_sha != sealed.commitment_sha:
            raise FileExistsError(f"a different seal for {sealed.cycle_id} already exists")
        return path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(sealed.model_dump_json())
    os.replace(tmp, path)
    return path


def load_sealed(cycle_id: str, directory: Path | None = None) -> SealedCycle:
    """Read the private seal record back; raises FileNotFoundError when there is none."""
    return SealedCycle.model_validate_json(_sealed_path(cycle_id, directory).read_text())
