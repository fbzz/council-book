from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from council.publish import commit_reveal
from council.publish.redact import public_cycle


@pytest.fixture
def doc(record, pack, policy):
    return public_cycle(record, pack, lines=policy.universe)


def test_round_trip_verifies(doc):
    commitment, salt = commit_reveal.seal(doc, sealed_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    assert commitment.cycle_id == doc.cycle_id
    assert commitment.policy_sha == doc.policy_sha
    assert commit_reveal.verify(doc, salt, commitment.commitment_sha256)
    reveal = commit_reveal.reveal(commitment, salt)
    assert reveal.salt == salt and reveal.commitment_sha256 == commitment.commitment_sha256


def test_verify_from_the_published_file_bytes(doc):
    """A third party re-hashes the revealed JSON file itself."""
    commitment, salt = commit_reveal.seal(doc)
    published = json.dumps(doc.model_dump(mode="json"), indent=2)          # any formatting
    raw = json.loads(published)
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(bytes.fromhex(salt) + canonical).hexdigest() == commitment.commitment_sha256


def test_tampered_document_fails(doc):
    commitment, salt = commit_reveal.seal(doc)
    data = doc.model_dump(mode="json")
    data["decision"]["human_outcome"] = "rejected"
    assert not commit_reveal.verify(data, salt, commitment.commitment_sha256)


def test_wrong_or_malformed_salt_fails(doc):
    commitment, salt = commit_reveal.seal(doc)
    other = ("00" if salt[:2] != "00" else "11") + salt[2:]
    assert not commit_reveal.verify(doc, other, commitment.commitment_sha256)
    assert not commit_reveal.verify(doc, "zz", commitment.commitment_sha256)
    assert not commit_reveal.verify(doc, salt[:10], commitment.commitment_sha256)


def test_canonical_json_ignores_key_order():
    assert commit_reveal.canonical_json({"b": 1, "a": [1, 2]}) == commit_reveal.canonical_json({"a": [1, 2], "b": 1})
    assert commit_reveal.canonical_json({"a": 1}) == b'{"a":1}'


def test_salts_are_fresh_and_32_bytes(doc):
    (c1, s1), (c2, s2) = commit_reveal.seal(doc), commit_reveal.seal(doc)
    assert s1 != s2 and c1.commitment_sha256 != c2.commitment_sha256
    assert len(bytes.fromhex(s1)) == 32


def test_nan_is_refused():
    with pytest.raises(ValueError):
        commit_reveal.canonical_json({"cycle_id": "x", "v": float("nan")})
