"""The site fixture (tests/fixtures/site_journal): a LIVE journal with ~20 lines, five cycles
(rehearsal, macro parse failure and timeouts, rejected, no action, approved and executed), the
macro output, the facts table and a described book. It is generated through the real public
models by tests/fixtures/make_site_journal.py; the site must build from it."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import commit_reveal, leakscan
from council.publish.public_models import (
    PublicBook,
    PublicCommitment,
    PublicCycleV1,
    PublicExecution,
    PublicOpsRow,
    PublicPerformancePoint,
    PublicReveal,
    PublicStatus,
)
from tests.fixtures import make_site_journal as fixture

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"
SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)


def _site():
    spec = importlib.util.spec_from_file_location("council_site_build_fixture", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_fixture"] = module
    spec.loader.exec_module(module)
    return module


def _cycles(root) -> dict[str, PublicCycleV1]:
    return {p.stem: PublicCycleV1.model_validate_json(p.read_bytes())
            for p in sorted((root / "cycles").rglob("*.json")) if not p.name.endswith(".reveal.json")}


def test_every_fixture_file_loads_through_the_public_models():
    status = PublicStatus.model_validate_json((FIXTURE / "status.json").read_text())
    assert status.state == "LIVE" and status.last_cycle_id == "2026-09-25T1440Z"
    book = PublicBook.model_validate_json((FIXTURE / "book" / "latest.json").read_text())
    assert len(book.lines) >= 19
    for p in (FIXTURE / "executions").rglob("*.json"):
        PublicExecution.model_validate_json(p.read_text())
    for line in (FIXTURE / "ops" / "cycles.jsonl").read_text().splitlines():
        PublicOpsRow.model_validate_json(line)
    for line in (FIXTURE / "performance" / "index.jsonl").read_text().splitlines():
        PublicPerformancePoint.model_validate_json(line)
    assert len(_cycles(FIXTURE)) == 5


def test_every_fixture_cycle_opens_its_commitment():
    for cycle_id in _cycles(FIXTURE):
        month = f"{cycle_id[:4]}/{cycle_id[5:7]}"
        sealed = (FIXTURE / "cycles" / month / f"{cycle_id}.json").read_bytes()
        reveal = PublicReveal.model_validate_json((FIXTURE / "cycles" / month / f"{cycle_id}.reveal.json").read_text())
        commitment = PublicCommitment.model_validate_json(
            (FIXTURE / "commitments" / month / f"{cycle_id}.json").read_text())
        assert commit_reveal.verify_bytes(sealed, reveal.salt, commitment.commitment_sha256), cycle_id


def test_the_fixture_covers_what_the_redesign_needs():
    cycles = _cycles(FIXTURE)
    ops = {json.loads(r)["cycle_id"]: json.loads(r) for r in (FIXTURE / "ops" / "cycles.jsonl").read_text().splitlines()}
    assert {r["decision_state"] for r in ops.values()} == {"reviewed_no_action", "rejected", "completed"}
    assert {c.mode for c in cycles.values()} == {"live", "rehearsal"}
    kinds = {c.error_kind for cv in cycles.values() for c in cv.calls}
    assert {"timeout", "schema", "corrected"} <= kinds
    roles = [c.role for c in cycles["2026-09-25T0640Z"].calls]
    for role in ("news", "macro", "bull_open", "bear", "bull_rebuttal"):
        assert roles.count(role) == 1, role
    assert roles.count("pm") == 3 and roles.count("single_agent") == 3
    assert any(c.macro is not None for c in cycles.values())
    for c in cycles.values():
        assert c.facts and all(900 <= len(a.argument) <= 1500 for a in (c.debate.bull, c.debate.bear, c.debate.rebuttal))
        assert all(r.dismissed for r in c.pm.replicates if r.valid)
        assert all(ln.day_change_pct is not None for ln in c.reference.values())
    book = PublicBook.model_validate_json((FIXTURE / "book" / "latest.json").read_text())
    stocks = [k for k, v in book.lines.items() if v.asset_class == "stock"]
    assert len(stocks) == 10 and {"flat", "short", "long"} == {v.direction for v in book.lines.values()}
    short = [v for v in book.lines.values() if v.direction == "short"][0]
    assert (short.settlement, short.leverage) == ("cfd", 2)
    pnl = [v.pnl_since_open_pct for v in book.lines.values() if v.pnl_since_open_pct is not None]
    assert min(pnl) < 0 < max(pnl)


def test_the_fixture_carries_no_private_value():
    findings = leakscan.scan_paths([FIXTURE], canaries=fixture.canaries(),
                                   licensed_texts=[f"{t} {s}" for t, s in fixture.NEWS_TEXT.values()])
    assert findings == []


def test_the_site_builds_from_the_fixture(tmp_path):
    site = _site()
    out = tmp_path / "site"
    written = site.build(FIXTURE, PROMPTS_DIR, POLICY_DIR, out, now=NOW)   # runs the leak scan too
    assert (out / "index.html").exists() and len(written) > 10
    pages = {p.name for p in (out / "cycles").glob("*.html")}
    assert pages == {f"{c}.html" for c in _cycles(FIXTURE)}
    assert leakscan.scan_paths([out], canaries=fixture.canaries()) == []


def test_the_generator_is_deterministic_and_matches_the_committed_file_set(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    fixture.build(a)
    fixture.build(b)
    files_a = {p.relative_to(a).as_posix(): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b).as_posix(): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert files_a == files_b
    committed = {p.relative_to(FIXTURE.parent).as_posix() for p in FIXTURE.rglob("*") if p.is_file()}
    assert set(files_a) == committed


@pytest.mark.parametrize("cycle_id", ["2026-09-24T1840Z", "2026-09-25T1440Z"])
def test_proposals_were_sealed_before_the_human_decision(cycle_id):
    cycle = _cycles(FIXTURE)[cycle_id]
    assert cycle.decision.state == "awaiting_publication" and cycle.plan is not None and cycle.plan.legs
