from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import commit_reveal, journal, leakscan
from council.publish.public_models import (
    BrokerFeedRef,
    FredRef,
    IdRef,
    PublicExecution,
    PublicFill,
    PublicIncident,
    PublicPerformancePoint,
)
from council.publish.redact import public_book, public_cycle, public_ops_row, public_status
from tests.publish.conftest import CANARIES, CYCLE_ID, SLOT

SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = SLOT + timedelta(hours=3, minutes=5)
PAGES = {"index.html", "cycles.html", "how.html", "rules.html", "record.html"}
REDIRECTS = {"council.html": "how.html", "book.html": "index.html", "failures.html": "record.html"}
# The CSP must not change: the only script is the hashed STALE badge, and styles come from files.
EXPECTED_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    "script-src 'sha256-djW30W9PGcW/Zms0WyQIL8hyqeA2fk538pnwRKebljE='; base-uri 'none'; form-action 'none'"
)
LINE_NAMES = ("Nasdaq-100", "Semiconductors", "S&amp;P 500", "Gold", "Bitcoin", "Ether", "Crude oil",
              "Euro / US dollar", "Pound / US dollar")


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


def _execution() -> PublicExecution:
    return PublicExecution(
        cycle_id=CYCLE_ID, decision_state="completed", approved_slot=SLOT, completed_slot=SLOT,
        fills=[PublicFill(seq=1, kind="partial_close", line="SEMIS", direction="long", settlement="real", leverage=1,
                          state="filled", weight_target_x=-0.075, cost_bp=0.6)],
        achieved_x={"SEMIS": 0.075}, achieved_drift_x=0.004, cost_bp_total=0.6,
    )


def _populated_journal(root: Path, record, pack, policy, *, sealed_state: str | None = None,
                       execution: bool = False, rehearsal: bool = False) -> Path:
    """Seal (optionally while the decision is still pending), reveal the exact sealed bytes, and
    publish the final outcome in the ops row (and the execution file). A rehearsal publishes no
    book and no performance, and the status stays AWAITING ACCOUNT."""
    if rehearsal:
        record = record.model_copy(update={"mode": "rehearsal", "decision_state": "reviewed_no_action",
                                           "decision_reason": "", "approved_at": None})
    sealed_rec = record if sealed_state is None else record.model_copy(
        update={"decision_state": sealed_state, "decision_reason": "", "approved_at": None})
    doc = public_cycle(sealed_rec, pack, lines=policy.universe)
    commitment, salt, sealed = commit_reveal.seal_bytes(doc, sealed_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
    files = {}
    files |= journal.commitment_files(commitment)
    files |= journal.reveal_files(sealed, salt, commitment)
    files |= journal.status_files(public_status("AWAITING_ACCOUNT" if rehearsal else "LIVE",
                                                last_cycle_id=CYCLE_ID, last_cycle_at=SLOT))
    files |= journal.ops_files(None, [public_ops_row(record)])
    if not rehearsal:
        files |= journal.book_files(public_book(CYCLE_ID, doc.risk.final_x, lines=policy.universe,
                                                reference_weights={k: v.weight_ref_x for k, v in doc.reference.items()}))
        files |= journal.performance_files(None, [
            PublicPerformancePoint(as_of=date(2026, 10, 1), c0=100.0, c2=100.0, c3=100.0, c4_spy=100.0),
            PublicPerformancePoint(as_of=date(2026, 10, 2), c0=100.4, c2=100.1, c3=99.8, c4_spy=100.9, drawdown_pct=0.0),
            PublicPerformancePoint(as_of=date(2026, 10, 3), c0=99.6, c2=99.9, c3=99.1, c4_spy=101.2, drawdown_pct=-0.8),
        ])
    if execution:
        files |= journal.execution_files(_execution())
    files |= journal.incident_files(PublicIncident(
        incident_id="INC-0001", opened_slot=SLOT, severity="low", status="resolved",
        title="Late cycle", summary="The laptop slept; the cycle ran 12 minutes late."))
    journal.write_files(root, files)
    return root / "journal"


def _build_empty(site, tmp_path) -> tuple[Path, dict[str, str]]:
    empty = tmp_path / "journal"
    empty.mkdir()
    out = tmp_path / "out"
    site.build(empty, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, _pages(out)


# ------------------------------------------------------------------------------ empty journal
def test_builds_with_zero_cycles(site, tmp_path):
    out, pages = _build_empty(site, tmp_path)
    assert set(pages) == PAGES | set(REDIRECTS)
    assert "AWAITING ACCOUNT" in pages["index.html"]
    assert "No runs yet" in pages["cycles.html"] and "No runs yet" in pages["index.html"]
    assert "Nothing held yet" in pages["index.html"] and "No performance data yet" in pages["index.html"]
    for title in ("What is it?", "Who&#39;s on the council?", "What can code stop?", "Who presses the button?",
                  "How do we know it isn&#39;t hindsight?", "What can this never prove?"):
        assert title in pages["how.html"], title
    assert leakscan.scan_paths([out]) == []


def test_empty_home_shows_the_pipeline_waiting(site, tmp_path):
    _, pages = _build_empty(site, tmp_path)
    home = pages["index.html"]
    assert home.count('<li class="node ') == 9
    assert home.count("not run yet") == 9
    assert '<li class="hrow">' not in home                      # nothing held, no holdings rows
    assert "REHEARSAL" not in home and 'class="banner"' not in home
    for name in LINE_NAMES:
        assert name in home, name                                # the lines it will hold


def test_every_page_has_a_strict_csp_and_one_hashed_script(site, tmp_path):
    out, pages = _build_empty(site, tmp_path)
    for name, html in pages.items():
        assert 'http-equiv="Content-Security-Policy"' in html, name
        assert "default-src 'none'" in html and site.script_hash() in html
        assert html.count("<script") == (0 if name in REDIRECTS else 1), name
        assert "fonts.googleapis" not in html and "http://" not in html
    for name in ("style.css", "geometry.css"):
        css = (out / "static" / name).read_text()
        assert "@import" not in css and "url(" not in css, name
    assert "prefers-color-scheme: dark" in (out / "static" / "style.css").read_text()


def test_csp_is_unchanged(site, tmp_path, record, pack, policy):
    assert site.CSP == EXPECTED_CSP
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    for name, html in _pages(out).items():
        assert f'content="{EXPECTED_CSP}"' in html, name


def test_no_inline_style_attributes_or_style_blocks(site, tmp_path, record, pack, policy):
    """The CSP (style-src 'self') would block them: every width comes from a class."""
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy, rehearsal=True)
    outs = [tmp_path / "out"]
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, outs[0], now=NOW)
    empty = tmp_path / "empty"
    empty.mkdir()
    outs.append(tmp_path / "out_empty")
    site.build(empty, PROMPTS_DIR, POLICY_DIR, outs[1], now=NOW)
    for out in outs:
        for name, html in _pages(out).items():
            assert not re.search(r"\sstyle\s*=", html, re.I), name
            assert "<style" not in html.lower(), name


def test_redirect_pages_keep_old_links_working(site, tmp_path):
    _, pages = _build_empty(site, tmp_path)
    for old, new in REDIRECTS.items():
        html = pages[old]
        assert f'<meta http-equiv="refresh" content="0; url={new}">' in html, old
        assert f'href="{new}"' in html


def test_how_and_rules_pages_list_roles_and_rules(site, tmp_path):
    _, pages = _build_empty(site, tmp_path)
    for text in ("Portfolio manager", "DECIDES", "ADVISES", "CONTEXT", "CODE", "Risk officer", "HUMAN"):
        assert text in pages["how.html"], text
    rules = pages["rules.html"]
    for text in ("R1", "R3", "R15", "In plain words"):
        assert text in rules, text
    assert "code refuses anything above 2.0x" in rules                       # plain words, policy numbers
    assert "−20%: no new risk · −25%: stop, and a proposal to sell everything goes to the human" in rules


def test_record_page_has_withdrawn_claims_and_disclaimer(site, tmp_path):
    _, pages = _build_empty(site, tmp_path)
    html = pages["record.html"]
    assert html.count("WITHDRAWN</span>") == 3
    assert "Not investment advice" in html
    assert "Real money will be at risk once live." in html and "nothing is at risk now" in html
    assert "Real money is at risk" not in html                            # nothing is live yet


# ------------------------------------------------------------------------------ a live cycle
def test_builds_with_one_cycle_and_passes_the_leak_scan(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    pages = _pages(out)
    cycle_page = pages[f"cycles/{CYCLE_ID}.html"]
    assert "SEAL VERIFIED" in cycle_page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in cycle_page and "<script>alert" not in cycle_page
    assert 'title="N:1a2b3c4d' in cycle_page and "Chipmakers" not in cycle_page
    assert "LIVE" in pages["index.html"] and "REHEARSAL" not in pages["index.html"]
    assert "VERIFIED" in pages["cycles.html"]
    assert "<polyline" in pages["index.html"] and "Semiconductors" in pages["index.html"]
    assert "INC-0001" in pages["record.html"]
    assert (out / "journal" / "cycles" / "2026" / "10" / f"{CYCLE_ID}.reveal.json").exists()
    assert leakscan.scan_paths([out], canaries=CANARIES) == []


def test_tampered_cycle_is_not_shown_as_verified(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    cycle_file = journal_dir / "cycles" / "2026" / "10" / f"{CYCLE_ID}.json"
    original = cycle_file.read_text()
    assert '"human_outcome":"approved"' in original                     # the exact canonical bytes
    cycle_file.write_text(original.replace('"human_outcome":"approved"', '"human_outcome":"rejected"'))
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
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
    # the checked-in shape: keyed by prompt stem, with a versioned id
    (prompts / "manifest.json").write_text(
        '{"bear": {"id": "council-bear/v1", "sha256": "' + "ab" * 32 + '"}, '
        '"single_agent": {"id": "council-single_agent/v1", "sha256": "' + "cd" * 32 + '"}}')
    roles = site.load_manifest(prompts)
    assert roles["bear"] == [{"prompt_id": "council-bear/v1", "sha": "ab" * 32}]
    assert roles["single_agent_control"][0]["prompt_id"] == "council-single_agent/v1"
    assert "v1" not in roles
    listed = prompts / "list.json"
    listed.write_text('[{"id": "council-pm/v2", "sha": "' + "ef" * 32 + '"}]')
    shutil.copy(listed, prompts / "manifest.json")
    assert site.load_manifest(prompts)["pm"][0]["prompt_id"] == "council-pm/v2"


def test_short_sha_never_looks_like_an_id(site):
    assert site.short_sha("1234567890123456abcdef") == "1234567890123456a"
    assert site.short_sha("abcdef0123456789") == "abcdef012345"
    assert not re.search(r"^\d{7,}$", site.short_sha("9" * 20 + "f"))


def test_page_layout_is_phone_safe():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert "minmax(0, 1fr)" in css and "overflow-x: auto" in css
    assert "@media (max-width: 880px)" in css and "@media (max-width: 640px)" in css
    assert "overflow-wrap: anywhere" not in css          # it collapses table columns to one letter
    assert "table.wide { min-width" in css
    assert ".pages { flex-wrap: wrap; gap: 2px 14px; }" in css      # the nav wraps: every page stays visible
    assert "table.stack tr { display: grid;" in css                   # run tables stack into blocks on phones
    assert "scroll-padding-top" in css                                # the sticky bar never covers a jump target


def test_hidden_badge_stays_hidden():
    """.chip sets display, which would otherwise override the hidden attribute on STALE."""
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert "[hidden] { display: none !important; }" in css


def test_lines_are_shown_in_universe_order(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    pages = _pages(out)
    run = pages[f"cycles/{CYCLE_ID}.html"]
    table = run[run.index('class="wide all-lines"'):]
    positions = [table.index(f'<span class="muted small">{s}</span>') for s in policy.universe.symbols()]
    assert positions == sorted(positions)
    home = pages["index.html"]
    rows = home[home.index('id="holdings"'):home.index('id="latest"')]
    positions = [rows.index(f"<strong>{name}</strong>") for name in LINE_NAMES]
    assert positions == sorted(positions)


def test_cli_entry_point(site, tmp_path):
    empty = tmp_path / "journal"
    empty.mkdir()
    assert site.main(["--journal", str(empty), "--out", str(tmp_path / "o")]) == 0
    shutil.rmtree(tmp_path / "o")


def test_stale_badge_threshold_is_five_hours(site):
    assert "Date.now()-t>5*36e5" in site.STALE_SCRIPT          # 5 x 3,600,000 ms
    assert site.script_hash().startswith("sha256-")


def test_revealed_cycle_file_is_the_exact_sealed_bytes(tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    doc = public_cycle(record, pack, lines=policy.universe)
    data = (journal_dir / "cycles" / "2026" / "10" / f"{CYCLE_ID}.json").read_bytes()
    assert data == commit_reveal.canonical_json(doc)


def test_cycle_sealed_pending_shows_the_final_outcome_from_ops_and_execution(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy, sealed_state="awaiting_publication",
                                     execution=True)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    pages = _pages(out)
    cycle_page = pages[f"cycles/{CYCLE_ID}.html"]
    assert "SEAL VERIFIED" in cycle_page and "EXECUTED" in cycle_page
    assert "sealed while the decision was pending" in cycle_page and "execution record" in cycle_page
    assert "Reason given: Agree with the cut" in cycle_page
    assert "1 Oct 2026, 14:40 UTC" in cycle_page and "<h3>Execution</h3>" in cycle_page
    assert "approved · executed" in cycle_page                           # the Human node of the diagram
    assert f"journal/executions/2026/10/{CYCLE_ID}.json" in cycle_page
    assert (out / "journal" / "executions" / "2026" / "10" / f"{CYCLE_ID}.json").exists()
    assert "EXECUTED" in pages["cycles.html"] and "approved" in pages["cycles.html"]
    assert "EXECUTED" in pages["index.html"]                             # recent runs: the final decision
    assert leakscan.scan_paths([out], canaries=CANARIES) == []


def test_final_outcome_from_the_ops_row_without_an_execution(site, tmp_path, record, pack, policy):
    rejected = record.model_copy(update={"decision_state": "rejected", "decision_reason": "Too soon after the stop",
                                         "approved_at": None})
    journal_dir = _populated_journal(tmp_path / "pub", rejected, pack, policy, sealed_state="proposed")
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    page = _pages(out)[f"cycles/{CYCLE_ID}.html"]
    assert "REJECTED" in page and "operations log" in page and "Too soon after the stop" in page
    assert "rejected: Too soon after the stop" in page                  # the Human node of the diagram
    assert "<h3>Execution</h3>" not in page


def test_cycle_page_renders_agreement_control_and_fingerprint(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    pages = _pages(out)
    page = pages[f"cycles/{CYCLE_ID}.html"]
    doc = public_cycle(record, pack, lines=policy.universe)
    assert "made the same call" in page and "67% of attempts" in page and "USED · MOST TYPICAL" in page
    assert "Compare every line with the control" in page and ">Single agent size</th>" in page
    glance = page[page.index('id="glance"'):page.index('id="changed"')]
    assert "One agent, no analysts or debate:" in glance
    assert page.count('class="control-strip') == 1                     # shown once, not twice
    assert "differs from the council on Semiconductors" in page
    assert "Material facts" in page and doc.material_fingerprint[:12] in page
    assert "10-year Treasury yield · 20-day change, 30 Sep: −12.5 bps" in page
    assert 'title="M:DGS10.chg20@2026-09-30"' in page
    assert "66.67%" in pages["cycles.html"] and "differs" in pages["cycles.html"]


def test_status_names_a_sealed_but_unrevealed_last_cycle(site, tmp_path, record):
    root = tmp_path / "pub"
    files = journal.status_files(public_status("LIVE", last_cycle_id=CYCLE_ID, last_cycle_at=SLOT))
    files |= journal.ops_files(None, [public_ops_row(record.model_copy(update={"decision_state": "proposed"}))])
    journal.write_files(root, files)
    out = tmp_path / "out"
    site.build(root / "journal", PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    index = _pages(out)["index.html"]
    assert "PROPOSED" in index and "The run of 1 Oct 2026, 14:40 UTC is sealed" in index
    assert "No runs yet" in _pages(out)["cycles.html"]


# ------------------------------------------------------------------------------ a rehearsal cycle
@pytest.fixture
def rehearsal_site(site, tmp_path, record, pack, policy) -> tuple[Path, dict[str, str]]:
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy, rehearsal=True)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, _pages(out)


def test_home_with_a_rehearsal_cycle_says_so(rehearsal_site):
    out, pages = rehearsal_site
    home = pages["index.html"]
    assert "Rehearsal — no broker account is connected yet." in home
    assert "These are the weights the council would hold; nothing has been traded." in home
    assert "REHEARSAL" in home and "Invested · target" in home
    assert "Last run 1 Oct 2026, 14:52 UTC (12 min after its 14:40 UTC slot) · page built 17:45 UTC" in home
    assert " h ago" not in home and 'data-last-cycle="2026-10-01T14:52:00Z"' in home
    assert "starts at go-live" in home                                   # no drawdown in rehearsal
    assert "REHEARSAL" in pages["cycles.html"]
    run = pages[f"cycles/{CYCLE_ID}.html"]
    assert "REHEARSAL" in run and "Rehearsal — no broker account is connected yet." in run
    assert leakscan.scan_paths([out], canaries=CANARIES) == []


def test_home_holdings_show_every_line(rehearsal_site):
    out, pages = rehearsal_site
    home = pages["index.html"]
    rows = re.findall(r'<li class="hrow">(.*?)</li>', home, re.S)
    assert len(rows) == 9
    for name in LINE_NAMES:
        assert any(f"<strong>{name}</strong>" in r for r in rows), name
    assert all('class="dbar"' in r for r in rows)
    assert "Council cut: 15% → 7.5% of the portfolio (size 1.00 → 0.50)" in home   # percent first, size in brackets
    assert "<strong>Size</strong> is how much of a line's full allocation is held" in home
    assert "Show as table" in home
    geometry = (out / "static" / "geometry.css").read_text()
    for cls in set(re.findall(r"\bg[wl]-\d+\b", home)):
        assert f".{cls} " in geometry, cls                               # every width class is defined


def test_home_diagram_has_nine_nodes_and_a_plain_summary(rehearsal_site):
    _, pages = rehearsal_site
    home = pages["index.html"]
    latest = home[home.index('id="latest"'):home.index('id="recent"')]
    assert latest.count('<li class="node ') == 9
    for kind in ("CODE", "LLM", "HUMAN"):
        assert f">{kind}</span>" in latest, kind
    for word in ("no trade: rehearsal", "not needed: rehearsal", "holds the reference · 1 claim", "cut Semiconductors"):
        assert word in latest, word
    assert "The council reviewed its 9 lines on 1 Oct 2026, 14:40 UTC — a rehearsal, so nothing was traded." in latest
    assert ("The bull argued to keep the reference and the bear to cut Semiconductors from 15% to 7.5% of the "
            "portfolio, and both valid portfolio-manager attempts chose to cut Semiconductors from 15% to 7.5% "
            "of the portfolio.") in latest
    assert "Semiconductors 15% → 7.5%" in latest                           # the manager step, in percent
    assert "One agent, no analysts or debate:" in latest
    assert "differs from the council on Semiconductors" in latest
    assert f'href="cycles/{CYCLE_ID}.html"' in latest and "Open the full run" in latest


def test_run_page_uses_human_evidence_labels(rehearsal_site):
    _, pages = rehearsal_site
    run = pages[f"cycles/{CYCLE_ID}.html"]
    assert run.count('<li class="node ') == 9
    for label, raw in (("Semiconductors · volatility vs its 1-year norm", "V:SEMIS:vol_ratio"),
                       ("Nasdaq-100 · vs 200-day average", "F:NDX:dist_sma200_pct"),
                       ("volatility card 1", "K:vol:1")):
        assert f'title="{raw}">{label}</span>' in run, label
    assert re.search(r'title="N:1a2b3c4d[^"]*">news item</span>', run)
    assert ">V:SEMIS:vol_ratio<" not in run                               # raw ids only in titles
    assert "What changed" in run and "7 lines stayed at the reference" in run and "change too small to trade" in run
    assert "Read the claim<" in run and "Read the 1 claims" not in run      # short arguments: claims only


def test_summary_is_deterministic(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy, rehearsal=True)
    a, b = tmp_path / "a", tmp_path / "b"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, a, now=NOW)
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, b, now=NOW)
    assert _pages(a) == _pages(b)
    assert (a / "static" / "geometry.css").read_text() == (b / "static" / "geometry.css").read_text()


def test_evidence_labels(site, policy):
    lines = site.Lines({"lines": [{"symbol": "GOLD", "name": "Gold"}, {"symbol": "NDX", "name": "Nasdaq-100"}]})

    def label(ref):
        return site.evidence_label(ref, lines)["label"]

    assert label(IdRef(kind="market", id="F:GOLD:dist_sma200")) == "Gold · vs 200-day average"
    assert label(IdRef(kind="vol", id="V:GOLD:ewma5_60")) == "Gold · volatility shock"
    assert label(IdRef(kind="cost", id="C:GOLD:per_side_bps")) == "Gold · cost per side"
    assert label(IdRef(kind="card", id="K:vol:1")) == "volatility card 1"
    assert label(IdRef(kind="event", id="E:fomc@2026-10-28")) == "FOMC 28 Oct"
    assert label(BrokerFeedRef(id="N:1a2b3c4d")) == "news item"
    fred = FredRef(series="DGS10", measure="chg20", as_of="2026-09-23", publishable=True)
    assert label(fred) == "10-year Treasury yield · 20-day change, 23 Sep"
    assert site.evidence_label(fred, lines)["raw"] == "M:DGS10.chg20@2026-09-23"
    assert label(FredRef(series="VIXCLS", publishable=False)) == "VIX"
    assert label(IdRef(kind="market", id="F:XYZ:new_field")) == "XYZ · new field"     # unknown: still readable


def test_diverging_bar_geometry(site):
    geo = site.Geometry()
    long = site.diverging_bar(0.2, 0.3, 0.4, geo)
    assert long["side"] == "long" and long["w"] == "gw-2500" and long["ref"] == "gl-8750"
    short = site.diverging_bar(-0.1, None, 0.4, geo)
    assert short["side"] == "short" and short["w"] == "gw-1250" and short["label"] == "−10%"
    near = site.diverging_bar(0.03, 0.09, 0.4, geo)                   # tick just past the tip: label stays at the tip
    assert near["at"] == "gl-5375" and near["ref"] == "gl-6125"
    assert site.nice_scale(0.35) == 0.4 and site.fmt_share(0.134) == "13.4%" and site.fmt_share(0.0) == "0%"
    assert ".gw-2500 { width: 25.00%; }" in geo.css()


# ------------------------------------------------------------------------------ the redesign, in words
def _lines(site):
    import yaml
    return site.Lines(yaml.safe_load((POLICY_DIR / "universe.yaml").read_text()))


def _rehearsal_doc(record, pack, policy):
    rec = record.model_copy(update={"mode": "rehearsal", "decision_state": "reviewed_no_action",
                                    "decision_reason": "", "approved_at": None})
    return public_cycle(rec, pack, lines=policy.universe)


def _run(site, doc):
    return site.build_run_view(site.CycleView(doc=doc, path="journal/x.json"), _lines(site))


def test_home_leads_with_one_sentence_and_the_summary(rehearsal_site):
    _, pages = rehearsal_site
    home = pages["index.html"]
    assert ("Latest run (1 Oct, 14:40 UTC): the council cut Semiconductors from 15% to 7.5% of the portfolio; "
            'the other 8 lines follow the rules. <a href="#latest">See the run ↓</a>') in home
    assert home.index('class="banner"') < home.index('class="headline"') < home.index('class="tiles"')
    latest = home[home.index('id="latest"'):home.index('id="recent"')]
    assert latest.index('class="story"') < latest.index('class="flow"')      # the words before the diagram
    run = pages[f"cycles/{CYCLE_ID}.html"]
    glance = run[run.index('id="glance"'):run.index('id="changed"')]
    assert glance.index('class="story"') < glance.index('class="flow"')


def test_rehearsal_speaks_with_one_voice(rehearsal_site):
    _, pages = rehearsal_site
    for name in ("index.html", "cycles.html", f"cycles/{CYCLE_ID}.html", "how.html", "record.html"):
        html = pages[name]
        assert "AWAITING ACCOUNT" not in html and "NO ACTION" not in html, name
        assert "No money is at risk yet: this is a rehearsal with no broker account." in html, name
        assert "Real money is at risk;" not in html, name
    assert "REHEARSAL · NO ACCOUNT" in pages["cycles.html"]                 # the top bar on other pages
    assert "REHEARSAL · NO ACCOUNT" not in pages["index.html"]              # the hero already says REHEARSAL
    assert 'title="Rehearsal: no broker account, nothing traded">NOT TRADED</span>' in pages["cycles.html"]


def test_empty_and_live_footers(site, tmp_path, record, pack, policy):
    (tmp_path / "e").mkdir()
    _, empty = _build_empty(site, tmp_path / "e")
    assert "No money is at risk yet: no broker account is connected." in empty["index.html"]
    assert empty["index.html"].count("AWAITING ACCOUNT") == 1                # once, in the hero
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    live = _pages(out)
    assert "Real money is at risk; the author holds the positions shown." in live["index.html"]
    assert "<strong>Real money is at risk.</strong>" in live["record.html"]
    # live: the council's change carries the decision's fate
    assert "Council cut: 15% → 7.5% of the portfolio (size 1.00 → 0.50), executed" in live["index.html"]


def test_rehearsal_decision_counts_the_orders_a_live_run_needs(site, record, pack, policy):
    doc = _rehearsal_doc(record, pack, policy).model_copy(update={"plan": None})   # a rehearsal has no plan
    run = _run(site, doc)
    decision = next(n for n in run["nodes"] if n["key"] == "decision")
    assert decision["result"] == "no trade: rehearsal"
    assert decision["detail"].startswith("a live run would need 1 order")         # from the risk engine's leg count
    text = " ".join(run["summary"]) + decision["detail"]
    assert "no order needed" not in text and "A live run would have needed 1 order" in text


def test_summary_uses_the_councils_weight_when_risk_holds_the_line(site, record, pack, policy):
    doc = _rehearsal_doc(record, pack, policy)
    risk = doc.risk.model_copy(update={"final_x": {**doc.risk.final_x, "SEMIS": 0.15},
                                       "hold_reasons": ["SEMIS: R19 market closed"]})
    run = _run(site, doc.model_copy(update={"risk": risk}))
    summary = " ".join(run["summary"])
    assert "15% → 15%" not in summary
    assert "chose to cut Semiconductors from 15% to 7.5% of the portfolio" in summary
    assert "held back Semiconductors (market closed)" in summary
    assert run["headline"].endswith("(the risk engine held back Semiconductors).")


def test_internal_codes_read_as_words(site, tmp_path, record, pack, policy):
    journal_dir = _populated_journal(tmp_path / "pub", record, pack, policy)
    out = tmp_path / "out"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    page = _pages(out)[f"cycles/{CYCLE_ID}.html"]
    assert "Why it met: a scheduled review, a volatility shock on Semiconductors." in page
    assert 'title="parse_fail">the answer could not be read</span>' in page and "Auditor: parse_fail" not in page
    assert "Gold: below the broker&#39;s minimum order size" in page and "below_broker_minimum" not in page.split("title=")[0]
    doc = _rehearsal_doc(record, pack, policy).model_copy(update={"flags": ["calendar:release_dates_skipped_no_fred_key"]})
    officers = next(n for n in _run(site, doc)["nodes"] if n["key"] == "officers")
    assert officers["state"] == "fallback" and officers["word"] == "calendar incomplete"
    assert "economic-release dates not loaded" in officers["title"]
    assert "calendar:" not in officers["title"] + officers["detail"]


def test_debate_reads_in_words(site, rehearsal_site):
    _, pages = rehearsal_site
    run = pages[f"cycles/{CYCLE_ID}.html"]
    debate = run[run.index('id="debate"'):run.index('id="pm"')]
    assert "<h3>Cut Semiconductors 15% → 7.5%</h3>" in debate and "<h3>Keep the reference</h3>" in debate
    assert "disputes 1: <q>Nasdaq is 6.2% above its 200-day average</q>" in debate
    defs = site.shorthand(["NDX dd52 -0.9% and vol 0.99x median; DGS10 +47bps, T10Y2Y -21bps"], _lines(site))
    terms = {d["term"]: d["meaning"] for d in defs}
    assert terms["dd52"] == "drop from 1-year high" and terms["DGS10"] == "10-year Treasury yield"
    assert "vol 0.99x" in terms and "bp, bps" in terms and terms["Tickers"] == "NDX Nasdaq-100"
    assert not any(t.startswith("SMA") for t in terms)                       # only terms that occur


def test_manager_attempts_collapse_when_they_agree(rehearsal_site):
    _, pages = rehearsal_site
    run = pages[f"cycles/{CYCLE_ID}.html"]
    pm = run[run.index('id="pm"'):run.index('id="cards"')]
    assert "Both valid attempts made the same call: cut Semiconductors from 15% to 7.5% (size 1.00 → 0.50)." in pm
    assert pm.count('<article class="rep') == 3 and "Show the other 2 attempts" in pm
    assert "Why, and the evidence" in pm and "Valid attempts: <span class=\"n\">2</span> of 3" in pm


def test_invested_is_one_number_on_both_pages(rehearsal_site):
    _, pages = rehearsal_site
    tile = re.search(r'Invested · target</p>\s*<p class="tile-value">([^<]+)</p>', pages["index.html"]).group(1)
    fact = re.search(r'<dt>Invested</dt><dd class="n">([^<]+?) <span', pages[f"cycles/{CYCLE_ID}.html"]).group(1)
    assert tile == fact


def test_holdings_axis_is_one_sided_for_a_long_only_book(site, rehearsal_site):
    _, pages = rehearsal_site
    assert '<div class="holdings one-sided">' in pages["index.html"]
    geo = site.Geometry()
    bar = site.diverging_bar(0.2, 0.3, 0.4, geo, short_scale=0.0)
    assert (bar["zero"], bar["start"], bar["w"], bar["ref"], bar["at"]) == ("gl-0", "gl-0", "gw-5000", "gl-7500", "gl-5000")
    assert [t["label"] for t in site.axis_ticks(0.4, 0.0, geo)] == ["0", "10%", "20%", "30%", "40%"]
    short = site.diverging_bar(-0.05, None, 0.4, geo, short_scale=0.1)       # zero at 20% of the track
    assert (short["zero"], short["start"], short["w"]) == ("gl-2000", "gr-8000", "gw-1000")
    assert site.diverging_bar(0.0, 0.0, 0.4, geo)["ref"] is None             # no tick on the zero line


def test_phone_tables_stack_and_scroll_regions_are_focusable(rehearsal_site):
    _, pages = rehearsal_site
    run = pages[f"cycles/{CYCLE_ID}.html"]
    for name, marker in (("index.html", 'class="wide stack runs"'), ("cycles.html", 'class="wide stack runs"'),
                         (f"cycles/{CYCLE_ID}.html", 'class="wide stack changes"'), ("rules.html", 'class="rules wide stack"')):
        assert marker in pages[name], name
    assert 'data-label="After risk"' in run and 'data-label="What changed"' in pages["index.html"]
    for name, html in pages.items():
        assert html.count('<div class="table-wrap"') == html.count('<div class="table-wrap" tabindex="0" role="region"'), name


def test_accessibility_and_plain_words(rehearsal_site):
    _, pages = rehearsal_site
    home = pages["index.html"]
    assert '<ol class="flow" role="list"' in home and '<ul class="hrows" role="list">' in home
    assert '<span class="num" aria-hidden="true">1</span>' in home
    assert 'aria-label="Fall from peak: not tracked until go-live"' in home
    assert "What each step does" in home and "Bull</strong> makes the case for a set of positions" in home
    gloss = "scaled by its trend (up = full, mixed = ¾, down = ¼) and trimmed when the line is unusually volatile"
    assert gloss in home and gloss in pages["how.html"]
    for name, html in pages.items():
        assert "Hover a step" not in html and "trend times volatility" not in html and "trend x volatility" not in html, name
    for old in REDIRECTS:
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in pages[old], old
