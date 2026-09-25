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


# ------------------------------------------------------------------------------ exact sealed bytes
def test_seal_bytes_returns_the_exact_hashed_bytes(doc):
    commitment, salt, sealed = commit_reveal.seal_bytes(doc, sealed_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    assert sealed == commit_reveal.canonical_json(doc)
    assert hashlib.sha256(bytes.fromhex(salt) + sealed).hexdigest() == commitment.commitment_sha256
    assert commit_reveal.verify_bytes(sealed, salt, commitment.commitment_sha256)
    assert commit_reveal.verify(doc, salt, commitment.commitment_sha256)
    assert not commit_reveal.verify_bytes(sealed + b" ", salt, commitment.commitment_sha256)
    assert not commit_reveal.verify_bytes(sealed, "zz", commitment.commitment_sha256)


def test_seal_and_seal_bytes_agree_for_a_given_salt(doc):
    salt = "11" * 32
    c1, s1 = commit_reveal.seal(doc, salt_hex=salt)
    c2, s2, _ = commit_reveal.seal_bytes(doc, salt_hex=salt)
    assert (s1, c1.commitment_sha256) == (s2, c2.commitment_sha256)


def test_private_seal_store_round_trips_bytes_exactly(doc, tmp_path):
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    record = commit_reveal.SealedCycle.build(commitment, salt, sealed)
    path = commit_reveal.save_sealed(record, tmp_path / "salts")
    assert path.name == f"{doc.cycle_id}.json" and (path.stat().st_mode & 0o777) == 0o600
    back = commit_reveal.load_sealed(doc.cycle_id, tmp_path / "salts")
    assert back.sealed_bytes == sealed and back.salt == salt and back.commitment_sha == commitment.commitment_sha256
    assert back.cycle_id == doc.cycle_id
    assert set(json.loads(path.read_text())) == {"salt", "sealed_hex", "commitment_sha"}   # the orchestrator's format


def test_private_seal_store_defaults_to_the_state_dir(doc, tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path / "state"))
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    path = commit_reveal.save_sealed(commit_reveal.SealedCycle.build(commitment, salt, sealed))
    assert path == tmp_path / "state" / "salts" / f"{doc.cycle_id}.json"
    assert commit_reveal.load_sealed(doc.cycle_id).sealed_bytes == sealed


def test_private_seal_store_refuses_a_second_different_seal(doc, tmp_path):
    first = commit_reveal.SealedCycle.build(*commit_reveal.seal_bytes(doc))
    commit_reveal.save_sealed(first, tmp_path)
    commit_reveal.save_sealed(first, tmp_path)                        # the same seal again is fine
    second = commit_reveal.SealedCycle.build(*commit_reveal.seal_bytes(doc))
    with pytest.raises(FileExistsError):
        commit_reveal.save_sealed(second, tmp_path)
    commit_reveal.save_sealed(second, tmp_path, replace=True)
    assert commit_reveal.load_sealed(doc.cycle_id, tmp_path).salt == second.salt


def test_private_seal_store_refuses_bad_input(doc, tmp_path):
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    with pytest.raises(ValueError):
        commit_reveal.SealedCycle.build(commitment, salt, sealed + b"x")
    with pytest.raises(ValueError):
        commit_reveal.load_sealed("../escape", tmp_path)
    from council.paths import REPO_ROOT

    with pytest.raises(RuntimeError):
        commit_reveal.save_sealed(commit_reveal.SealedCycle.build(commitment, salt, sealed), REPO_ROOT / "journal")
