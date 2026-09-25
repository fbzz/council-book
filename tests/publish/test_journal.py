from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from council.publish import commit_reveal, journal
from council.publish.public_models import PublicIncident, PublicOpsRow, PublicPerformancePoint
from council.publish.redact import public_cycle, public_ops_row, public_status

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)


def test_layout_paths():
    cid = "2026-10-01T1440Z"
    assert journal.commitment_path(cid) == "journal/commitments/2026/10/2026-10-01T1440Z.json"
    assert journal.cycle_path(cid) == "journal/cycles/2026/10/2026-10-01T1440Z.json"
    assert journal.reveal_path(cid) == "journal/cycles/2026/10/2026-10-01T1440Z.reveal.json"
    assert journal.execution_path(cid) == "journal/executions/2026/10/2026-10-01T1440Z.json"
    assert journal.incident_path("INC-0007") == "journal/incidents/INC-0007.md"
    assert journal.STATUS_PATH == "journal/status.json"
    assert journal.BOOK_PATH == "journal/book/latest.json"


@pytest.mark.parametrize("bad", ["../2026-10-01T1440Z", "2026-10-01", "x/../../etc", ""])
def test_bad_cycle_ids_are_refused(bad):
    with pytest.raises(journal.JournalError):
        journal.cycle_path(bad)


def test_reveal_files_publish_the_exact_sealed_bytes(record, pack, policy):
    doc = public_cycle(record, pack, lines=policy.universe)
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    files = journal.reveal_files(sealed, salt, commitment)
    assert files[journal.cycle_path(doc.cycle_id)] == sealed                 # unchanged, byte for byte
    raw = json.loads(files[journal.cycle_path(doc.cycle_id)])
    assert commit_reveal.verify(raw, salt, commitment.commitment_sha256)
    reveal = json.loads(files[journal.reveal_path(doc.cycle_id)])
    assert reveal["salt"] == salt and reveal["commitment_sha256"] == commitment.commitment_sha256
    assert journal.reveal_files(sealed, salt, commitment.commitment_sha256) == files   # sha alone works too


def test_reveal_files_require_a_matching_commitment(record, pack, policy):
    doc = public_cycle(record, pack, lines=policy.universe)
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    other = commit_reveal.seal_bytes(doc)[0]
    with pytest.raises(journal.JournalError):
        journal.reveal_files(sealed, salt, other)                          # a different commitment
    with pytest.raises(journal.JournalError):
        journal.reveal_files(sealed, "00" * 32, commitment)                # a different salt
    with pytest.raises(journal.JournalError):
        journal.reveal_files(doc, salt, commitment)                        # a rebuilt document


def test_reveal_files_refuse_bytes_that_are_not_the_canonical_public_cycle(record, pack, policy):
    doc = public_cycle(record, pack, lines=policy.universe)
    pretty = journal.dump_json(doc)                                        # same content, not the sealed bytes
    commitment, salt, _ = commit_reveal.seal_bytes(doc)
    sha = commit_reveal.bytes_commitment_sha256(pretty, salt)
    with pytest.raises(journal.JournalError, match="canonical"):
        journal.reveal_files(pretty, salt, sha)
    data = doc.model_dump(mode="json") | {"amount_usd": 1.0}               # outside the allow-list
    bad = commit_reveal.canonical_json(data)
    with pytest.raises(journal.JournalError, match="not a valid public cycle"):
        journal.reveal_files(bad, salt, commit_reveal.bytes_commitment_sha256(bad, salt))
    renamed = commit_reveal.seal_bytes(doc, salt_hex=salt)[0].model_copy(update={"cycle_id": "2026-10-01T1840Z"})
    with pytest.raises(journal.JournalError, match="ids differ"):
        journal.reveal_files(commit_reveal.canonical_json(doc), salt, renamed)


def test_final_outcome_is_published_without_touching_the_sealed_cycle(record, pack, policy):
    """Sealed while pending; the final state goes to the ops row, the cycle bytes never change."""
    sealed_rec = record.model_copy(update={"decision_state": "awaiting_publication", "approved_at": None,
                                           "decision_reason": ""})
    doc = public_cycle(sealed_rec, pack, lines=policy.universe)
    commitment, salt, sealed = commit_reveal.seal_bytes(doc)
    ops = journal.ops_files(None, [public_ops_row(sealed_rec)])[journal.OPS_PATH]
    ops = journal.ops_files(ops, [public_ops_row(record)])[journal.OPS_PATH]      # final outcome upserted
    row = json.loads(ops.decode().splitlines()[0])
    assert (row["decision_state"], row["human_outcome"]) == ("completed", "approved")
    revealed = journal.reveal_files(sealed, salt, commitment)[journal.cycle_path(doc.cycle_id)]
    assert json.loads(revealed)["decision"]["human_outcome"] == "pending"


def test_jsonl_upsert_is_idempotent_and_keyed(record):
    row = public_ops_row(record)
    first = journal.ops_files(None, [row])[journal.OPS_PATH]
    again = journal.ops_files(first, [row])[journal.OPS_PATH]
    assert first == again and first.count(b"\n") == 1
    other = PublicOpsRow.model_validate({**row.model_dump(), "cycle_id": "2026-10-01T1840Z",
                                         "slot": datetime(2026, 10, 1, 18, 40, tzinfo=UTC)})
    both = journal.ops_files(again, [other])[journal.OPS_PATH]
    assert both.count(b"\n") == 2
    point = PublicPerformancePoint(as_of=date(2026, 10, 1), c0=100.0, c2=100.0)
    perf = journal.performance_files(None, [point, point.model_copy(update={"c0": 101.0})])[journal.PERFORMANCE_PATH]
    assert perf.count(b"\n") == 1 and b'"c0":101.0' in perf


def test_incident_round_trip():
    inc = PublicIncident(incident_id="INC-0001", opened_slot=SLOT, severity="medium", title="Cycle withheld",
                         summary="The leak scan refused the cycle; a stub was published instead.",
                         cycles=["2026-10-01T1440Z"], withheld=True)
    text = journal.incident_files(inc)["journal/incidents/INC-0001.md"].decode()
    assert text.startswith("---\n") and "# INC-0001 · Cycle withheld" in text
    assert journal.parse_incident(text) == inc


def test_next_incident_id():
    assert journal.next_incident_id([]) == "INC-0001"
    assert journal.next_incident_id(["INC-0001.md", "journal/incidents/INC-0012.md"]) == "INC-0013"


def test_status_document(tmp_path):
    files = journal.status_files(public_status("LIVE", last_cycle_id="2026-10-01T1440Z", last_cycle_at=SLOT))
    written = journal.write_files(tmp_path, files)
    assert json.loads(written[0].read_text())["state"] == "LIVE"


def test_write_files_refuses_escape(tmp_path):
    with pytest.raises(journal.JournalError):
        journal.write_files(tmp_path, {"../outside.json": b"{}"})
