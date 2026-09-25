from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import commit_reveal, journal, leakscan
from council.publish.public_models import PublicIncident, PublicPerformancePoint
from council.publish.redact import public_book, public_cycle, public_ops_row, public_status
from tests.publish.conftest import CANARIES, CYCLE_ID, SLOT

SITE_BUILD = REPO_ROOT / "site" / "build.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("council_site_build", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build"] = module      # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_builder()


def _pages(out: Path) -> dict[str, str]:
    return {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.html")}


def _populated_journal(root: Path, record, pack, policy) -> Path:
    doc = public_cycle(record, pack, lines=policy.universe)
    commitment, salt = commit_reveal.seal(doc, sealed_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    files = {}
    files |= journal.commitment_files(commitment)
    files |= journal.reveal_files(doc, commit_reveal.reveal(commitment, salt))
    files |= journal.status_files(public_status("LIVE", last_cycle_id=CYCLE_ID, last_cycle_at=SLOT))
    files |= journal.book_files(public_book(CYCLE_ID, doc.risk.final_x, lines=policy.universe,
                                            reference_weights={k: v.weight_ref_x for k, v in doc.reference.items()}))
    files |= journal.ops_files(None, [public_ops_row(record)])
    files |= journal.performance_files(None, [
        PublicPerformancePoint(as_of=date(2026, 10, 1), c0=100.0, c2=100.0, c3=100.0, c4_spy=100.0),
        PublicPerformancePoint(as_of=date(2026, 10, 2), c0=100.4, c2=100.1, c3=99.8, c4_spy=100.9, drawdown_pct=0.0),
        PublicPerformancePoint(as_of=date(2026, 10, 3), c0=99.6, c2=99.9, c3=99.1, c4_spy=101.2, drawdown_pct=-0.8),
    ])
    files |= journal.incident_files(PublicIncident(
        incident_id="INC-0001", opened_slot=SLOT, severity="low", status="resolved",
        title="Late cycle", summary="The laptop slept; the cycle ran 12 minutes late."))
    journal.write_files(root, files)
    return root / "journal"


def test_builds_with_zero_cycles(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    out = tmp_path / "out"
    site.build(empty, PROMPTS_DIR, POLICY_DIR, out)
    pages = _pages(out)
    assert set(pages) == {"index.html", "council.html", "cycles.html", "book.html", "failures.html"}
    assert "AWAITING ACCOUNT" in pages["index.html"]
    assert "No cycles yet" in pages["cycles.html"]
    assert "No book yet" in pages["book.html"] and "No performance data yet" in pages["book.html"]
    for title in ("What is it?", "Who&#39;s on the council?", "What can code stop?", "Who presses the button?",
                  "How do we know it isn&#39;t hindsight?", "What can this never prove?"):
        assert title in pages["index.html"], title
    assert leakscan.scan_paths([out]) == []


def test_every_page_has_a_strict_csp_and_one_hashed_script(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    out = tmp_path / "out"
    site.build(empty, PROMPTS_DIR, POLICY_DIR, out)
    for name, html in _pages(out).items():
        assert 'http-equiv="Content-Security-Policy"' in html, name
        assert "default-src 'none'" in html and site.script_hash() in html
        assert html.count("<script") == 1, name
        assert "fonts.googleapis" not in html and "http://" not in html
    css = (out / "static" / "style.css").read_text()
    assert "@import" not in css and "url(" not in css
    assert "prefers-color-scheme: dark" in css


def test_council_page_lists_roles_and_rules(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    out = tmp_path / "out"
    site.build(empty, PROMPTS_DIR, POLICY_DIR, out)
    html = _pages(out)["council.html"]
    for text in ("Portfolio manager", "DECIDES", "ADVISES", "CONTEXT", "CODE", "Risk officer", "R1", "R3", "R15"):
        assert text in html, text


def test_failures_page_has_withdrawn_claims_and_disclaimer(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    out = tmp_path / "out"
    site.build(empty, PROMPTS_DIR, POLICY_DIR, out)
    html = _pages(out)["failures.html"]
    assert html.count("WITHDRAWN</span>") == 3
    assert "Not investment advice" in html and "Real money is at risk" in html


def test_builds_with_one_cycle_and_passes_the_leak_scan(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out)
    pages = _pages(out)
    cycle_page = pages[f"cycles/{CYCLE_ID}.html"]
    assert "SEAL VERIFIED" in cycle_page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in cycle_page and "<script>alert" not in cycle_page
    assert "N:1a2b3c4d" in cycle_page and "Chipmakers" not in cycle_page
    assert "LIVE" in pages["index.html"]
    assert "VERIFIED" in pages["cycles.html"]
    assert "<polyline" in pages["book.html"] and "SEMIS" in pages["book.html"]
    assert "INC-0001" in pages["failures.html"]
    assert (out / "journal" / "cycles" / "2026" / "10" / f"{CYCLE_ID}.reveal.json").exists()
    assert leakscan.scan_paths([out], canaries=CANARIES) == []


def test_tampered_cycle_is_not_shown_as_verified(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    cycle_file = journal_dir / "cycles" / "2026" / "10" / f"{CYCLE_ID}.json"
    cycle_file.write_text(cycle_file.read_text().replace('"human_outcome": "approved"', '"human_outcome": "rejected"'))
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out)
    assert "SEAL VERIFIED" not in _pages(out)[f"cycles/{CYCLE_ID}.html"]


def test_a_leak_in_the_output_fails_the_build(site, tmp_path, monkeypatch):
    empty = tmp_path / "journal"
    empty.mkdir()
    withdrawn = tmp_path / "withdrawn.yaml"
    withdrawn.write_text("claims:\n  - {id: W-9, claim: 'A claim', why: 'cost was $1,234.56'}\n")
    real = site.load_withdrawn
    monkeypatch.setattr(site, "load_withdrawn", lambda path=withdrawn: real(path))
    with pytest.raises(site.SiteBuildError):
        site.build(empty, PROMPTS_DIR, POLICY_DIR, tmp_path / "out")


def test_manifest_shapes_are_tolerated(site, tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "manifest.json").write_text(
        '{"prompts": {"pm.md": {"sha256": "' + "ab" * 32 + '"}, "bull_open.md": "' + "cd" * 32 + '"}}')
    roles = site.load_manifest(prompts)
    assert roles["pm"][0]["sha"] == "ab" * 32 and roles["bull"][0]["prompt_id"] == "bull_open.md"
    (prompts / "manifest.json").write_text('[{"role": "pm", "id": "pm@v1", "sha": "not-a-sha"}]')
    assert site.load_manifest(prompts)["pm"] == [{"prompt_id": "pm@v1", "sha": ""}]


def test_short_sha_never_looks_like_an_id(site):
    assert site.short_sha("1234567890123456abcdef") == "1234567890123456a"
    assert site.short_sha("abcdef0123456789") == "abcdef012345"
    assert not re.search(r"^\d{7,}$", site.short_sha("9" * 20 + "f"))


def test_page_layout_is_phone_safe():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert "minmax(0, 1fr)" in css and "overflow-x: auto" in css
    assert "@media (max-width: 880px)" in css
    assert "overflow-wrap: anywhere" not in css          # it collapses table columns to one letter
    assert "table.wide { min-width" in css


def test_hidden_badge_stays_hidden():
    """.chip sets display, which would otherwise override the hidden attribute on STALE."""
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert "[hidden] { display: none !important; }" in css


def test_lines_are_shown_in_universe_order(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out)
    html = _pages(out)[f"cycles/{CYCLE_ID}.html"]
    start = html.index("<h3>Reference book</h3>")
    ref = html[start:html.index("id=\"cards\"", start)]
    positions = [ref.index(f"<strong>{s}</strong>") for s in policy.universe.symbols()]
    assert positions == sorted(positions)


def test_cli_entry_point(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    assert site.main(["--journal", str(empty), "--out", str(tmp_path / "o")]) == 0
    shutil.rmtree(tmp_path / "o")


def test_stale_badge_threshold_is_five_hours(site):
    assert "Date.now()-t>5*36e5" in site.STALE_SCRIPT          # 5 x 3,600,000 ms
    assert site.script_hash().startswith("sha256-")
