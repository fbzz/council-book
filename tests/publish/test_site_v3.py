"""Site v3, built from the ~20-line fixture journal (tests/fixtures/site_journal): the broker-style
holdings list, the per-agent run transcript, the agents pages, the dark OpenSourceUI-derived look,
and the rules every page keeps (no script, no inline style, percent only, relative links)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish.public_models import PublicBook, PublicCycleV1

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"
SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
TRANSCRIPT_ORDER = ("a-data", "a-reference", "a-vol", "a-event", "a-news", "a-macro", "a-bull", "a-bear",
                    "a-rebuttal", "a-pm", "a-control", "a-audit", "a-risk", "a-costs", "a-decision", "facts", "seal")


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_v3", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_v3"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_site()


@pytest.fixture(scope="module")
def built(site, tmp_path_factory) -> tuple[Path, dict[str, str]]:
    out = tmp_path_factory.mktemp("site_v3") / "site"
    site.build(FIXTURE, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.html")}


def _cycle(cycle_id: str) -> PublicCycleV1:
    return PublicCycleV1.model_validate_json(
        (FIXTURE / "cycles" / cycle_id[:4] / cycle_id[5:7] / f"{cycle_id}.json").read_bytes())


def _visible_text(html: str) -> str:
    html = re.sub(r"<svg\b.*?</svg>", " ", html, flags=re.S)
    html = re.sub(r"<head>.*?</head>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


# ------------------------------------------------------------------------------ every page
def test_every_agent_has_a_page_and_the_nav_links_to_agents(site, built):
    out, pages = built
    assert "agents/index.html" in pages
    for spec in site.AGENT_SPECS:
        assert f"agents/{spec.slug}.html" in pages, spec.slug
        assert f'href="{spec.slug}.html"' in pages["agents/index.html"], spec.slug
    for name, html in pages.items():
        if name in ("council.html", "book.html", "failures.html"):
            continue
        assert re.search(r'<a href="(\.\./)?agents/index\.html"[^>]*>Agents</a>', html), name


def test_no_script_no_inline_style_and_no_money_on_any_page(built):
    _, pages = built
    for name, html in pages.items():
        low = html.lower()
        assert "<script" not in low and "<style" not in low, name
        assert not re.search(r"\sstyle\s*=", html, re.I), name
        text = _visible_text(html)
        assert not re.search(r"[$€£]\s?\d", text), name                  # never an amount
        assert not re.search(r"\b\d{7,}\b", text), name                   # never an id-like number (hashes aside)
        assert "built-in method" not in text and not re.search(r"\b0x[0-9a-f]{6,}", text), name
        assert "Undefined" not in text and "None" not in text.split(), name


def test_every_relative_link_and_fragment_resolves(built):
    """Links are relative so the site also works from file://, and every #fragment exists."""
    out, pages = built
    ids = {name: set(re.findall(r'\sid="([^"]+)"', html)) for name, html in pages.items()}
    for name, html in pages.items():
        base = (out / name).parent
        for href in re.findall(r'\s(?:href|src)="([^"]+)"', html):
            if re.match(r"^(https?:|mailto:)", href):
                continue
            path, _, frag = href.partition("#")
            assert not path.startswith("/"), (name, href)                   # never root-absolute
            target = (base / unquote(path)).resolve() if path else (out / name).resolve()
            assert target.exists(), (name, href)
            if frag and target.suffix == ".html":
                rel = target.relative_to(out.resolve()).as_posix()
                assert frag in ids[rel], (name, href)


def test_every_geometry_class_is_defined(built):
    out, pages = built
    css = (out / "static" / "geometry.css").read_text()
    used = {c for html in pages.values() for c in re.findall(r"\bg[wlrp]-\d+\b", html)}
    assert any(c.startswith("gp-") for c in used)                            # progress rings are used
    for cls in used:
        assert f".{cls} " in css, cls


def test_attribution_is_on_every_page_and_in_the_notices(built):
    _, pages = built
    for name, html in pages.items():
        if name in ("council.html", "book.html", "failures.html"):
            continue
        assert "Interface styles adapted from OpenSourceUI (MIT)" in html, name
        assert "THIRD_PARTY_NOTICES.md" in html, name
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert "OpenSourceUI" in notices and "MIT License" in notices and "Copyright (c) 2026 Bidyut Kundu" in notices
    assert "Lucide" in notices and "ISC License" in notices


def test_icons_are_inline_decorative_svg(site):
    icons = site.load_icons()
    for name in set(site.STATUS_ICONS.values()) | set(site.AGENT_ICONS.values()) | {"terminal", "lock", "flask-conical"}:
        assert name in icons, name
        svg = str(site.icon_svg(icons, name))
        assert svg.startswith('<svg class="i') and 'aria-hidden="true"' in svg and "style" not in svg, name
    assert "<path" not in str(site.icon_svg(icons, "no-such-icon"))          # unknown: an empty, harmless svg


# ------------------------------------------------------------------------------ the dark look
def _tokens(css: str, selector: str) -> dict[str, str]:
    block = css[css.index(selector + " {"):]
    block = block[:block.index("}")]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_dark_is_the_default_theme():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    dark = _tokens(css, ':root,\n:root[data-theme="dark"]')
    assert dark["--bg"] == "#0a0a0a" and dark["--card"] == "#141414"
    assert '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"])' in css
    light = _tokens(css, ':root[data-theme="light"]')
    assert light["--bg"] == "#fafafa"
    base = (REPO_ROOT / "site" / "templates" / "base.html.j2").read_text()
    assert '<meta name="color-scheme" content="dark">' in base and "data-theme" not in base


def test_text_colours_meet_wcag_aa_on_every_surface():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    for selector, surfaces in ((':root,\n:root[data-theme="dark"]', ("--bg", "--card", "--raised", "--inset")),
                               (':root[data-theme="light"]', ("--card", "--raised", "--inset"))):
        t = _tokens(css, selector)
        texts = ["--ink", "--ink-strong", "--muted", "--link", "--up", "--down", "--warn", "--halted",
                 "--executed", "--proposed", "--code", "--analyst", "--bull", "--bear", "--pm", "--risk", "--human"]
        for fg in texts:
            for bg in surfaces:
                assert _contrast(t[fg], t[bg]) >= 4.5, (selector, fg, bg, round(_contrast(t[fg], t[bg]), 2))
    term = _tokens(css, ':root,\n:root[data-theme="dark"]')
    for fg in ("--term-ink", "--term-muted", "--term-cmd", "--term-ok", "--term-err", "--term-fb", "--term-link"):
        assert _contrast(term[fg], term["--term-bg"]) >= 4.5, fg


# ------------------------------------------------------------------------------ holdings (home)
def test_home_leads_with_a_broker_style_holdings_list(built):
    _, pages = built
    home = pages["index.html"]
    book = PublicBook.model_validate_json((FIXTURE / "book" / "latest.json").read_text())
    assert home.index('id="holdings"') < home.index('id="latest"')
    rows = re.findall(r'<tr class="hr g-([a-z]+)( flat)?"[^>]*>(.*?)</tr>', home, re.S)
    assert len(rows) == len(book.lines) == 19
    held = [r for r in rows if not r[1]]
    flat = [r for r in rows if r[1]]
    assert len(held) == sum(1 for b in book.lines.values() if abs(b.weight_x) > 1e-9)
    assert '<span class="cnt cnt-all">17</span>' in home and "Not held (" in home
    weights = [abs(float(w.replace("−", "-").rstrip("%"))) for w in
               (re.search(r'<span class="wv">([^<]+)</span>', r[2]).group(1) for r in held)]
    assert weights == sorted(weights, reverse=True)                        # sorted by weight
    notheld = home.index('<details class="notheld')
    assert len(flat) == 2 and all(home.index(r[2]) > notheld for r in flat)  # flat lines fold under "Not held"
    assert all(home.index(r[2]) < notheld for r in held)
    short = next(r[2] for r in rows if ">GBPUSD<" in r[2])
    assert '<span class="pos pos-short">Short</span>' in short and '<span class="lev">2x</span>' in short
    assert ">CFD</span>" in short
    for session in ("24/7", "24/5", "US hours"):
        assert f">{session}</span>" in home, session
    assert 'class="mono ac-stock"' in home and 'class="mono ac-crypto"' in home
    assert re.search(r'class="mv mv-down"><span class="mv-arrow" aria-hidden="true">▼</span>−\d', home)
    assert "LIVE BOOK" in home and "P/L since open" in home


def test_every_asset_class_filter_matches_its_rows(site, built):
    out, pages = built
    home = pages["index.html"]
    css = (out / "static" / "style.css").read_text()
    for key, label, _classes, _phrase in site.FILTER_GROUPS:
        n = home.count(f'<tr class="hr g-{key}')
        if n:
            assert f'<label class="hf-chip" for="hf-{key}">' in home and label.replace("&", "&amp;") in home, key
            assert f"#hf-{key}:checked ~ .hx-body .hr:not(.g-{key})" in css, key
    assert home.count('<tr class="hr g-stock') == 10                         # the ten single stocks


def test_pending_proposal_is_announced_but_not_revealed(site, tmp_path):
    """A sealed run whose decision is pending is announced on the home page, without its content."""
    journal = tmp_path / "journal"
    for p in FIXTURE.rglob("*"):
        if p.is_file():
            dest = journal / p.relative_to(FIXTURE)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    cid = "2026-09-25T1840Z"
    (journal / "commitments" / "2026" / "09" / f"{cid}.json").write_text(
        (journal / "commitments" / "2026" / "09" / "2026-09-25T1440Z.json").read_text().replace("2026-09-25T1440Z", cid))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    assert "1 proposal awaiting the operator" in home and "The run of 25 Sep 2026, 18:40 UTC is sealed" in home
    assert f"journal/commitments/2026/09/{cid}.json" in home and not (out / "cycles" / f"{cid}.html").exists()


# ------------------------------------------------------------------------------ the run page
def test_run_page_is_a_transcript_in_execution_order(built):
    _, pages = built
    run = pages["cycles/2026-09-25T0640Z.html"]
    positions = [run.index(f'id="{anchor}"') for anchor in TRANSCRIPT_ORDER]
    assert positions == sorted(positions)
    toc = run[run.index('<nav class="toc"'):run.index("</nav>", run.index('<nav class="toc"'))]
    for anchor in TRANSCRIPT_ORDER[:-2]:
        assert f'href="#{anchor}"' in toc, anchor
    assert toc.count('class="tg tg-') >= 15                                   # a status glyph per agent
    for anchor in ("a-news", "a-macro", "a-bull", "a-bear", "a-rebuttal", "a-pm", "a-control"):
        section = run[run.index(f'id="{anchor}"'):]
        section = section[:section.index("</section>")]
        assert '<figure class="term"' in section, anchor                        # its call log
        for sub in ("What it saw", "What it said"):
            assert sub in section, (anchor, sub)


def test_arguments_are_published_whole_and_evidence_resolves_to_facts(built):
    _, pages = built
    doc = _cycle("2026-09-25T0640Z")
    run = pages["cycles/2026-09-25T0640Z.html"]
    for adv in (doc.debate.bull, doc.debate.bear, doc.debate.rebuttal):
        text = adv.argument.replace("&", "&amp;").replace("'", "&#39;").replace('"', "&#34;")
        assert text in run                                                     # the FULL argument
    anchors = set(re.findall(r'\sid="([^"]+)"', run))
    chips = re.findall(r'<a class="ev ev-[a-z]+" href="#(f-[^"]+)"', run)
    assert chips and all(c in anchors for c in chips)
    assert re.search(r'<a class="ev ev-[a-z]+" href="#f-[^"]+" title="[^"]+">[^<]+: <strong class="ev-val">', run)
    assert "Facts the council saw" in run and "licensed series: cited by name, value not republished" in run


def test_manager_dismissals_cross_link_to_claims(built):
    _, pages = built
    run = pages["cycles/2026-09-25T1440Z.html"]
    pm = run[run.index('id="a-pm"'):run.index('id="a-control"')]
    targets = re.findall(r'<a class="cref" href="#(cl-[^"]+)"', pm)
    assert targets and all(f'id="{t}"' in run for t in targets)
    bull = run[run.index('id="a-bull"'):run.index('id="a-bear"')]
    assert "set aside</span>" in bull and '<sup class="dag">†</sup>' in bull   # a bare number, read by the side taken
    assert "The manager wrote only the claim number" in bull


def test_an_ambiguous_claim_number_is_shown_once_never_as_a_firm_fate(built):
    """"c2" when both advocates have a c2: one muted chip on the attempt, nothing under either claim."""
    _, pages = built
    run = pages["cycles/2026-09-25T0640Z.html"]
    for anchor, end in (("a-bull", "a-bear"), ("a-bear", "a-rebuttal")):
        assert "set aside</span>" not in run[run.index(f'id="{anchor}"'):run.index(f'id="{end}"')], anchor
    pm = run[run.index('id="a-pm"'):run.index('id="a-control"')]
    assert pm.count('<span class="chip unclear"') >= 1 and "may mean either" not in run
    control = run[run.index('id="a-control"'):run.index('id="a-audit"')]
    assert "Set aside" not in control                                      # the control saw no debate
    bear = pages["agents/bear.html"]
    assert "Claims set aside</dt><dd class=\"st-v\">0 of 12</dd>" in bear


def test_failed_calls_say_what_went_wrong_and_what_happened_instead(built):
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    news = run[run.index('id="a-news"'):run.index('id="a-macro"')]
    assert "<strong>What went wrong:</strong> The model did not answer within the time limit" in news
    assert "<strong>What the system did instead:</strong> No news cards this run" in news
    assert 'class="tl tl-to"' in news and "timeout" in news and 'class="tl tl-fb"' in news     # timeouts in amber
    macro = run[run.index('id="a-macro"'):run.index('id="a-bull"')]
    assert "did not match the required format" in macro and "[schema]" in macro
    toc = run[run.index('<nav class="toc"'):run.index("</nav>", run.index('<nav class="toc"'))]
    assert 'class="tg tg-timeout" title="timeout"' in toc and 'class="tg tg-failed"' in toc
    assert "3 calls failed" in run


def test_run_header_has_stat_tiles_and_a_progress_ring(built):
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    head = run[:run.index('id="glance"')]
    assert 'class="alert alert-run"' in head and "Model calls" in head and "Manager agreement" in head
    assert re.search(r'<span class="ring ring-fallback gp-73" role="img" aria-label="8 of 11 calls usable">', head)


# ------------------------------------------------------------------------------ the agents pages
def test_agents_index_is_a_team_table_with_records(site, built):
    _, pages = built
    idx = pages["agents/index.html"]
    assert idx.count('<table class="team stack">') == 5                     # one per group
    news_calls = sum(1 for p in (FIXTURE / "cycles").rglob("*.json") if not p.name.endswith(".reveal.json")
                     for c in json.loads(p.read_bytes())["calls"] if c["role"] == "news")
    news = idx[idx.index('id="ag-news"'):idx.index('id="ag-macro"')]
    assert f'data-label="Calls">{news_calls}</td>' in news
    assert 'class="avatar accent-analyst"' in news and "deepseek" not in news   # the model is said once, above
    assert "deepseek" in idx[:idx.index('id="ag-data"')]
    bull = idx[idx.index('id="ag-bull"'):idx.index('id="ag-bear"')]
    assert "The manager did what it asked in 3 of 5 runs" in bull and "claims set aside by the used attempt" in bull


def test_agent_pages_show_history_newest_first(site, built):
    _, pages = built
    bull = pages["agents/bull.html"]
    whens = re.findall(r'<article class="entry" id="run-([^"]+)">', bull)
    assert len(whens) == 5 and whens == sorted(whens, reverse=True)
    assert len(whens) <= site.HISTORY_CAP
    assert "Bull · opening" in bull and "Bull · rebuttal" in bull and '<figure class="term"' in bull
    assert "prompts/bull_open.md" in bull
    pm = pages["agents/pm.html"]
    assert pm.count('<article class="rep') >= 15 and "Valid attempts" in pm
    data = pages["agents/data.html"]
    assert "code" in data and '<figure class="term"' not in data            # code agents make no model call


# ------------------------------------------------------------------------------ generic over lines
def test_single_stock_line_ids_are_generic(site):
    assert site.ticker("BRK_B") == "BRK.B" and site.monogram("BRK_B", "stock") == "BRKB"
    assert site.monogram("EURUSD", "fx") == "EUR"
    per_line, general = site.split_holds(["BRK_B: deadband", "scaled to fit aggregate limits"])
    assert per_line["BRK_B"][0]["text"] == "change too small to trade" and general
    lines = site.Lines({"lines": [{"symbol": "NDX", "name": "Nasdaq-100", "asset_class": "index"}]})
    assert lines.sort(["BRK_B", "NDX", "AAPL"]) == ["NDX", "AAPL", "BRK_B"]   # unknown lines after the policy's


# ------------------------------------------------------------------------------ review fixes (site v3)
def _copy_fixture(tmp_path: Path, keep: set[str] | None = None) -> Path:
    """A copy of the fixture journal; with `keep`, only those runs (cycles, seals, ops rows)."""
    journal = tmp_path / "journal"
    for p in FIXTURE.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(FIXTURE)
        if keep is not None and rel.parts[0] in ("cycles", "commitments", "executions") and \
                not any(k in p.name for k in keep):
            continue
        dest = journal / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())
    if keep is not None:
        ops = journal / "ops" / "cycles.jsonl"
        ops.write_text("".join(line + "\n" for line in ops.read_text().splitlines()
                               if any(k in line for k in keep)))
    return journal


def test_no_css_rule_pairs_full_width_with_a_side_margin():
    """`width: 100%` plus a left or right margin is wider than its box: the page scrolls sideways
    (it did at 390 px on every run page)."""
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"(?<![-\w])width:\s*100%", body):
            continue
        margins = re.findall(r"margin(-left|-right|-inline[-\w]*)?:\s*([^;]+)", body)
        for side, value in margins:
            parts = value.split()
            horizontal = parts if side else (parts[1:2] + parts[3:4] if len(parts) > 1 else parts)
            assert all(re.fullmatch(r"0(px|%)?|auto", v) for v in horizontal), selector.strip()
    phone = css[css.index("@media (max-width: 640px)"):]
    rule = re.search(r"\.h-status \{([^}]*)\}", phone).group(1)
    assert "width: 100%" not in rule and "margin: 0;" in rule


def test_no_page_repeats_an_element_id(built):
    _, pages = built
    for name, html in pages.items():
        ids = re.findall(r'\sid="([^"]+)"', html)
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, (name, sorted(dupes)[:5])


def test_a_rehearsal_never_shows_a_book_move(site, tmp_path):
    """Before go-live the 1-day tile is labelled as the target's, hypothetical, and there is no P/L column."""
    journal = _copy_fixture(tmp_path, keep={"2026-09-24T0640Z"})
    (journal / "book" / "latest.json").unlink()
    (journal / "status.json").write_text(json.dumps({**json.loads((journal / "status.json").read_text()),
                                                     "state": "AWAITING_ACCOUNT", "last_cycle_id": "2026-09-24T0640Z",
                                                     "last_cycle_at": "2026-09-24T06:40:00Z"}))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    assert "Book, last day" not in home and "Target, last day" in home
    assert "hypothetical: target weights × last daily move; nothing traded" in home
    assert "P/L since open" not in home and "Open P/L" not in home
    assert 'class="hx no-pnl"' in home


def test_live_holdings_line_up_and_add_up(built):
    _, pages = built
    home = pages["index.html"]
    values = re.findall(r'<span class="wv">([^<]+)</span>', home)
    assert values and all(re.fullmatch(r"−?\d+\.\d%", v) for v in values)          # one fixed decimal
    assert "Open P/L" in home and "Book, last day" in home and 'class="hx"' in home
    assert 'title="Bars run from 0 to 40% of the portfolio"' in home and "bars run 0–40% of the portfolio" in home
    assert ">BRK.B</strong>" in home and "Berkshire Hathaway B" in home              # a class-share line id
    assert "No FX pairs held." not in home or 'class="hf-empty only-fx"' in home
    assert "No fx held" not in home and "No etfs &amp; indices held" not in home


def test_every_failed_call_is_listed_with_what_the_run_did_instead(built):
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    head = run[:run.index('id="glance"')]
    alert = head[head.index('class="alert alert-warn"'):]
    assert "3 model calls failed; the run fell back safely" in alert
    assert re.search(r'<a href="#a-news">News analyst</a> timed out after 1 min 31 s .*no news cards', alert)
    assert '<a href="#a-pm-3">Portfolio manager attempt 3</a> timed out after 2 min' in alert
    assert "decision taken on the 2 valid attempts, which agreed" in alert
    glance = run[run.index('id="glance"'):run.index('id="changed"')]
    assert "3 model calls failed and the run fell back safely" in glance
    assert '<a class="chip state-warn" href="#calls">' in head and 'id="calls"' in run


def test_a_timed_out_attempt_is_explained_by_its_call(built):
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    assert "the answer could not be read" not in run
    pm = run[run.index('id="a-pm"'):run.index('id="a-control"')]
    assert "No usable answer: it timed out after 2 min." in pm and "(timed out)" not in pm
    assert "The failed attempt was discarded; the decision used the 2 valid attempts, which agreed." in pm
    audit = run[run.index('id="a-audit"'):run.index('id="a-risk"')]
    assert "discarded: <span" in audit and "no usable answer (it timed out after 2 min)" in audit


def test_identical_attempts_fold_under_the_used_one(built):
    _, pages = built
    run = pages["cycles/2026-09-25T0640Z.html"]
    pm = run[run.index('id="a-pm"'):run.index('id="a-control"')]
    assert pm.count('<details class="rep-same"') == 2 and 'id="a-pm-2"' in pm and 'id="a-pm-3"' in pm
    assert "Attempt 2: same call as attempt 1" in pm
    control = run[run.index('id="a-control"'):run.index('id="a-audit"')]
    assert "How it differs from the council" in control                     # it disagreed: attempts shown
    agree = pages["cycles/2026-09-24T1040Z.html"]
    control = agree[agree.index('id="a-control"'):agree.index('id="a-audit"')]
    assert "How it compares with the council" in control and '<details class="more">' in control


def test_code_officers_share_one_card_and_the_run_page_is_shorter(built):
    _, pages = built
    run = pages["cycles/2026-09-25T1440Z.html"]
    card = run[run.index('id="officers"'):run.index('id="a-news"')]
    for anchor in ("a-data", "a-reference", "a-vol", "a-event"):
        assert '<article class="officer' in card and f'id="{anchor}"' in card, anchor
    assert "Facts it cited" not in run


def test_every_order_says_why(built):
    _, pages = built
    run = pages["cycles/2026-09-25T1440Z.html"]
    costs = run[run.index('id="a-costs"'):run.index('id="a-decision"')]
    assert "Council: cut (manager attempt 1)" in costs and "Council: short (manager attempt 1)" in costs
    assert "Reference: not yet held, bought up to its reference weight (2%)" in costs
    execution = run[run.index('id="a-execution"'):]
    assert "Reference: not yet held, bought up to its reference weight (2%)" in execution
    changed = run[run.index('id="changed"'):run.index('id="officers"')]
    assert "Also traded to reach the reference: <strong>NVIDIA 0% → 2%</strong>" in changed
    steady = re.search(r"stayed at the reference: ([^<]+)\.</p>", changed).group(1)
    assert "NVIDIA" not in steady


def test_what_happened_to_an_advocate_goes_by_outcome(built):
    """The bull asked to keep the reference and the manager kept it, labelling its side "the
    reference": that is doing what the bull asked, not "sided with the bull in 0 runs"."""
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    bull = run[run.index('id="a-bull"'):run.index('id="a-bear"')]
    assert "The used attempt did what the bull asked (keep the reference); the manager labelled its side “the reference”." in bull
    assert "sided with the bull in" not in bull
    semis = pages["cycles/2026-09-25T1440Z.html"]
    bear = semis[semis.index('id="a-bear"'):semis.index('id="a-rebuttal"')]
    assert "The used attempt did what the bear asked" in bear and "named the bear as the side it took" in bear


def test_agent_pages_end_with_the_outcome(built):
    _, pages = built
    bear = pages["agents/bear.html"]
    assert bear.count('<ol class="chain"') == 5
    chain = bear[bear.index('<ol class="chain"'):bear.index("</ol>", bear.index('<ol class="chain"'))]
    for step in ("Bull asked", "Bear asked", "Manager", "Risk engine", "Human"):
        assert step in chain, step
    assert "approved · executed" in chain                                   # the newest run was executed
    assert '<q class="muted">' in bear                                     # the bull's claim a rebuttal answers
    assert '<p class="list-label">Concedes</p>' in bear and ".; " not in bear
    assert "Open in run →" in bear
    pm = pages["agents/pm.html"]
    assert "Failed calls" in pm and 'href="../cycles/2026-09-24T1040Z.html#a-pm-3"' in pm
    idx = pages["agents/index.html"]
    news = idx[idx.index('id="ag-news"'):idx.index('id="ag-macro"')]
    assert "1 timeout" in news and 'href="../cycles/2026-09-24T1040Z.html#a-news"' in news
    toc = idx[idx.index('<nav class="toc"'):idx.index("</nav>", idx.index('<nav class="toc"'))]
    assert '<span class="tg-w">timeout</span>' in toc                       # the worst status in the window


def test_legacy_arguments_say_they_were_shortened(site, tmp_path):
    journal = _copy_fixture(tmp_path)
    path = journal / "cycles" / "2026" / "09" / "2026-09-25T0640Z.json"
    doc = json.loads(path.read_bytes())
    doc["debate"]["bull"]["argument"] = doc["debate"]["bull"]["argument"][:599] + "…"
    path.write_text(json.dumps(doc))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    run = (out / "cycles" / "2026-09-25T0640Z.html").read_text()
    bull = run[run.index('id="a-bull"'):run.index('id="a-bear"')]
    assert "Shortened when published: early runs kept only the first 600 characters" in bull
    bear = run[run.index('id="a-bear"'):run.index('id="a-rebuttal"')]
    assert "Shortened when published" not in bear


def test_the_call_log_names_agents_as_the_page_does(built):
    _, pages = built
    run = pages["cycles/2026-09-24T1040Z.html"]
    log = run[run.index('id="calls"'):]
    for name in ("News analyst", "Bull · opening", "Bull · rebuttal", "Portfolio manager", "Control"):
        assert name in log, name
    assert ">opening</a>" not in log and ">pm</a>" not in log
    assert "total 2 min" in run and "median" in run                          # a 2-minute timeout is not hidden
