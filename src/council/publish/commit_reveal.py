"""Commit-reveal pre-commitment.

Rules:
- canonical JSON = UTF-8, keys sorted, no whitespace, no NaN/Infinity.
- commitment = sha256(salt || canonical_json(doc)), salt = 32 random bytes (hex in the reveal).
- The commitment is pushed BEFORE a proposal can be approved; the document and its salt are
  revealed once the decision is final. Anyone can re-hash the revealed file to check the seal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from council.clock import utcnow
from council.publish.public_models import COMMIT_ALGO, PublicCommitment, PublicReveal

SALT_BYTES = 32


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


def commitment_sha256(doc: BaseModel | dict[str, Any], salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    if len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be {SALT_BYTES} bytes")
    return hashlib.sha256(salt + canonical_json(doc)).hexdigest()


def seal(
    doc: BaseModel | dict[str, Any],
    *,
    sealed_at: datetime | None = None,
    code_commit: str = "",
    salt_hex: str | None = None,
) -> tuple[PublicCommitment, str]:
    """Seal a public cycle document. Returns the public commitment and the PRIVATE salt (hex);
    the salt is stored privately until the reveal."""
    obj = _as_json_obj(doc)
    salt_hex = salt_hex or secrets.token_bytes(SALT_BYTES).hex()
    commitment = PublicCommitment(
        cycle_id=obj["cycle_id"],
        commitment_sha256=commitment_sha256(obj, salt_hex),
        algo=COMMIT_ALGO,
        sealed_at=sealed_at or utcnow(),
        code_commit=code_commit,
        prompt_manifest_sha=obj.get("prompt_manifest_sha", "") or "",
        policy_sha=obj.get("policy_sha", "") or "",
        model_digest=obj.get("model_digest", "") or "",
    )
    return commitment, salt_hex


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
