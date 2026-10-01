"""Site v4, "council chamber", built from the fixture journal (19 lines, five runs): the book map,
the council roster in speaking order, one page per line, seat colours, self-hosted fonts, and the
rules every page keeps (no script, no inline style, percent only, relative links)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish.public_models import PublicBook

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"
SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
SPEAKING_ORDER = ("data", "reference", "vol", "event", "news", "macro", "bull", "bear", "pm", "control", "audit",
                  "risk", "costs", "human")


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_v4", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_v4"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_site()


@pytest.fixture(scope="module")
def built(site, tmp_path_factory) -> tuple[Path, dict[str, str]]:
    out = tmp_path_factory.mktemp("site_v4") / "site"
    site.build(FIXTURE, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.html")}


def _book() -> PublicBook:
    return PublicBook.model_validate_json((FIXTURE / "book" / "latest.json").read_text())


def _copy_fixture(tmp_path: Path, keep: set[str]) -> Path:
    journal = tmp_path / "journal"
    for p in FIXTURE.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(FIXTURE)
        if rel.parts[0] in ("cycles", "commitments", "executions") and not any(k in p.name for k in keep):
            continue
        dest = journal / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())
    ops = journal / "ops" / "cycles.jsonl"
    ops.write_text("".join(line + "\n" for line in ops.read_text().splitlines() if any(k in line for k in keep)))
    return journal


# ------------------------------------------------------------------------------ the book map
def test_squarify_tiles_the_box_in_proportion(site):
    values = [0.35, 0.15, 0.113, 0.067, 0.062, 0.06, 0.025, 0.02, 0.02, 0.01, 0.005]
    for box in (site.MAP_DESK, site.MAP_PHONE):
        rects = site.squarify(values, *box)
        total = box[0] * box[1]
        assert len(rects) == len(values)
        for v, (x, y, w, h) in zip(values, rects, strict=True):
            assert x >= -1e-6 and y >= -1e-6 and x + w <= box[0] + 1e-6 and y + h <= box[1] + 1e-6
            assert abs(w * h - v / sum(values) * total) < 1e-6 * total        # area in proportion
        for i, a in enumerate(rects):                                          # no two tiles overlap
            for b in rects[i + 1:]:
                ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                assert ox <= 1e-6 or oy <= 1e-6
    assert site.squarify([], 10, 10) == [] and site.squarify([0.0], 10, 10) == [(0.0, 0.0, 0.0, 0.0)]
    mixed = site.squarify([1.0, 0.0, 3.0], 10, 10)                             # a zero among positives: an empty box
    assert mixed[1] == (0.0, 0.0, 0.0, 0.0) and abs(mixed[0][2] * mixed[0][3] + mixed[2][2] * mixed[2][3] - 100) < 1e-9


def test_home_leads_with_a_map_of_every_held_line(built):
    out, pages = built
    home = pages["index.html"]
    book = _book()
    held = {k for k, b in book.lines.items() if abs(b.weight_x) > 1e-9}
    tiles = re.findall(r'<li class="tm (tm-[A-Za-z0-9_]+)([^"]*)">', home)
    groups = re.findall(r'<li class="tgrp (tgrp-[a-z]+)([^"]*)">', home)
    assert {t[0][3:] for t in tiles} == held | ({"cash"} if book.cash_x > 0.004 else set())   # every held line
    for k in held:
        assert f'<a class="tm-in" href="assets/{k}.html"' in home, k           # each tile opens its line
    # a two-level map: one box per asset class, with its name and share (class by grouping and a word)
    assert {g[0] for g in groups} >= {"tgrp-fund", "tgrp-stock", "tgrp-crypto", "tgrp-commodity", "tgrp-fx"}
    assert '<span class="tgrp-name">ETFs &amp; indices</span> <span class="tgrp-w">56.7%</span>' in home
    geometry = (out / "static" / "geometry.css").read_text()
    phone = geometry[geometry.index("@media (max-width: 699px)"):]
    for cls, *_ in tiles + groups:                                             # two layouts, both defined
        assert re.search(rf"^\.{cls} {{ left: [\d.]+%; top: [\d.]+%; width: [\d.]+%; height: [\d.]+%; }}$",
                         geometry[:geometry.index("@media")], re.M), cls
        assert f"  .{cls} {{ left:" in phone, cls
    assert home.index('class="bmap"') < home.index('class="htable"') < home.index('id="council"')
    # what a tile shows depends on its real size: CSS container queries, correct at every width
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert ".tm { position: absolute; padding: 1px; container: tile / size; }" in css
    assert "@container tile (height < 112px) { .tm-n { display: none; } }" in css
    assert "@container tile (height < 22px) or (width < 34px)" in css
    assert "tz-d-" not in home and "tz-p-" not in home


def test_asset_class_hues_only_colour_the_map_in_their_own_mode(built):
    """Hues belong to the agents and to long/short. The five asset-class hues exist for the map's
    "Colour by: asset class" mode only (fills, box frames, squares; never dots): the list's chips and
    monograms stay neutral. The palette itself is checked in test_site_v4_colours.py."""
    _, pages = built
    home = pages["index.html"]
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert set(re.findall(r"--ac-([a-z]+):", css)) == {"stock", "fund", "crypto", "commodity", "fx"}
    assert not re.search(r"--tile-(fund|stock|crypto|commodity|fx|other):", css)
    for rule in re.findall(r"^[^\n{]*\{[^}\n]*var\(--ac-[^}\n]*\}", css, re.M):   # only the .bc-* hooks read them
        assert rule.startswith(".bc-"), rule
    for rule in re.findall(r"^[^\n{]*\{[^}\n]*var\(--fcm?\)[^}\n]*\}", css, re.M):  # only the class mode shows them
        assert rule.startswith("#cb-class:checked ~ ") or rule.startswith(".cbk-sq"), rule
    assert 'class="ac-dot' not in home and "key-sw ac-stock" not in home
    assert ".mono { display: grid; place-items: center; flex: none; width: 36px; height: 36px; border-radius: 4px;" in css
    assert "background: var(--raised); border: 1px solid var(--line-strong); }" in css[css.index(".mono {"):]


def test_a_short_tile_says_so_without_colour(built):
    _, pages = built
    home = pages["index.html"]
    tile = re.search(r'<li class="tm tm-GBPUSD [^"]*">(.*?)</li>', home, re.S)
    assert tile and " short" in re.search(r'<li class="tm tm-GBPUSD([^"]*)"', home).group(1)
    assert '<span class="tm-s">short</span>' in tile.group(1)                # the word, not only the hatch
    assert '<span class="tm-w">−6.2%</span>' in tile.group(1)                # and a minus sign when the word hides
    assert "short" in re.search(r'title="([^"]+)"', tile.group(1)).group(1)
    assert "coral edge, “short” and a minus sign = a short" in home
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert (".tm.short .tm-in { box-shadow: inset 3px 0 0 var(--short), inset 4px 0 0 var(--bg); "
            "background-image: repeating-linear-gradient(") in css


def test_the_map_marks_the_managers_changes(built):
    _, pages = built
    home = pages["index.html"]
    for k, ref in (("SEMIS", "13.4%"), ("GBPUSD", "0.0%")):
        tile = re.search(rf'<li class="tm tm-{k}[^"]*">(.*?)</li>', home, re.S).group(1)
        assert f"council · ref {ref}" in tile, k
    ndx = re.search(r'<li class="tm tm-NDX[^"]*">(.*?)</li>', home, re.S).group(1)
    assert "tm-c" not in ndx
    assert "the portfolio manager moved it away from the reference" in home


def test_drift_is_never_pinned_on_the_manager(site, tmp_path):
    """A live book that drifted from the reference between runs does not claim a manager decision:
    the mark comes from the latest run's council change, not from the weight."""
    journal = _copy_fixture(tmp_path, keep={"2026-"})
    path = journal / "book" / "latest.json"
    book = json.loads(path.read_text())
    for k in ("NDX", "SPX", "BTC"):
        book["lines"][k]["weight_x"] = round(book["lines"][k]["weight_x"] * 1.02, 4)
    path.write_text(json.dumps(book))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    for k in ("NDX", "SPX", "BTC"):
        assert "tm-c" not in re.search(rf'<li class="tm tm-{k}[^"]*">(.*?)</li>', home, re.S).group(1), k
    for k in ("SEMIS", "GBPUSD"):
        assert "tm-c" in re.search(rf'<li class="tm tm-{k}[^"]*">(.*?)</li>', home, re.S).group(1), k


def test_a_cash_tile_when_the_book_holds_cash(site):
    geo = site.Geometry()
    row = {"line": "NDX", "weight": 0.35, "ticker": "NDX", "name": "Nasdaq-100", "ac": "index", "group": "fund",
           "page": "assets/NDX.html", "day": "+0.62%", "day_raw": 0.62, "day_dir": "up", "ref": 0.35,
           "council_moved": False}
    bmap = site.book_map({"held": [row], "cash_x": 0.65}, geo)
    assert bmap["cash"] and [g["key"] for g in bmap["groups"]] == ["cash", "fund"]
    assert "tm-cash" in geo.css() and "tgrp-cash" in geo.css()


def test_tiles_scale_with_the_book_not_a_fixed_count(site, tmp_path):
    """A rehearsal (target book, one old-format run) draws its own map from its own lines."""
    journal = _copy_fixture(tmp_path, keep={"2026-09-24T0640Z"})
    (journal / "book" / "latest.json").unlink()
    (journal / "status.json").write_text(json.dumps({**json.loads((journal / "status.json").read_text()),
                                                     "state": "AWAITING_ACCOUNT", "last_cycle_id": "2026-09-24T0640Z",
                                                     "last_cycle_at": "2026-09-24T06:40:00Z"}))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    assert '<span class="chip state-rehearsal">TARGET</span>' in home and "(target)" in home
    assert home.count('<li class="tm ') >= 10 and "Open P/L" not in home


# ------------------------------------------------------------------------------ the council
def test_home_council_is_every_agent_in_speaking_order(site, built):
    _, pages = built
    home = pages["index.html"]
    council = home[home.index('id="council"'):home.index('id="latest"')]
    cards = re.findall(r'<li class="agent-card accent-([a-z]+)">\s*<a class="ac-link" href="agents/([a-z]+)\.html">',
                       council)
    assert [c[0] for c in cards] == [c[1] for c in cards] == list(SPEAKING_ORDER)
    assert [s.slug for s in site.AGENT_SPECS] == list(SPEAKING_ORDER)
    assert council.count('<li class="phase" id="phase-') == len(site.PHASES)
    verdicts = dict(re.findall(r'href="agents/([a-z]+)\.html">.*?<span class="ac-verdict">([^<]+)</span>', council, re.S))
    assert verdicts["bear"] == "cut SEMIS · short GBPUSD"
    assert verdicts["pm"] == "cut SEMIS · short GBPUSD · 2/3 agree"
    assert verdicts["risk"] == "9/9 checks pass" and verdicts["human"] == "approved · executed"
    assert verdicts["control"] == "differs on GBPUSD" and verdicts["vol"] == "shock on SEMIS"
    assert "14 seats take turns on the book" in council and "a person approves every trade" in council
    # the hemicycle: one seat per agent, each linking to its page
    # nameplates: the seat number large in the seat colour, the name, the latest verdict, the job
    for n, slug in enumerate(SPEAKING_ORDER, start=1):
        card = council[council.index(f'<li class="agent-card accent-{slug}">'):]
        card = card[:card.index("</li>")]
        assert f'<span class="ac-n" aria-hidden="true">{n:02d}</span>' in card, slug
    # the first screen already has the cast: the teaser beside the title, the seats numbered in order
    head = home[:home.index('class="bmap"')]
    assert 'class="teaser"' in head and "14 seats: code reads the market and sets a reference" in head
    hemi = head[head.index('<svg class="hemi hemi-teaser"'):head.index("</svg>", head.index('<svg class="hemi'))]
    assert re.findall(r'<a href="agents/([a-z]+)\.html" class="hemi-a" tabindex="-1">', hemi) == list(SPEAKING_ORDER)
    assert re.findall(r'<li class="seat-n accent-([a-z]+)"', head) == list(SPEAKING_ORDER)


def test_a_failed_call_shows_on_the_agents_card(site, tmp_path):
    journal = _copy_fixture(tmp_path, keep={"2026-09-24T1040Z"})
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    news = home[home.index('<li class="agent-card accent-news">'):home.index('<li class="agent-card accent-macro">')]
    assert '<span class="ac-mark"><span class="mark mark-timeout">' in news and "timeout" in news
    start = home.index('<li class="agent-card accent-macro">')
    macro = home[start:home.index("</ul>", start)]
    assert "mark-failed" in macro and "parse fail" in macro
    pm = home[home.index('<li class="agent-card accent-pm">'):home.index('<li class="agent-card accent-control">')]
    assert "partly failed" in pm


def test_every_agent_owns_one_seat_colour(site):
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    seats = dict(re.findall(r"--seat-([a-z]+): (#[0-9a-f]{6});", css))
    approved = {"bull": "#3fc7a0", "bear": "#f07a5f", "pm": "#a68bfa", "risk": "#7fa7c9", "news": "#e3b341",
                "macro": "#54a31b"}
    for slug, hexa in approved.items():
        assert seats[slug] == hexa, slug
    for spec in site.AGENT_SPECS:
        assert spec.accent == spec.slug and spec.slug in seats, spec.slug
        assert f".accent-{spec.slug}" in css, spec.slug
    assert len({seats[s.slug] for s in site.AGENT_SPECS}) == len(site.AGENT_SPECS)   # no two share a colour
    assert "--long: #3fc7a0;" in css and "--short: #f07a5f;" in css                  # the bull's and bear's hues


def test_the_colour_of_an_agent_is_the_same_everywhere(built):
    _, pages = built
    run = pages["cycles/2026-09-25T1440Z.html"]
    toc = run[run.index('<nav class="toc"'):run.index("</nav>", run.index('<nav class="toc"'))]
    for anchor, slug in (("a-news", "news"), ("a-bear", "bear"), ("a-pm", "pm"), ("a-risk", "risk")):
        assert re.search(rf'href="#{anchor}"><span class="dot accent-{slug}"', toc), anchor
        assert f'<section class="card agent accent-{slug}" id="{anchor}"' in run, anchor
    bear = pages["agents/bear.html"]
    assert 'class="avatar accent-bear avatar-lg"' in bear
    assert 'class="said accent-bear"' in pages["assets/SEMIS.html"]


# ------------------------------------------------------------------------------ asset pages
def test_every_line_that_ever_appeared_has_a_page(site, built):
    out, pages = built
    book = _book()
    lines = set(book.lines)
    for p in (FIXTURE / "cycles").rglob("*.json"):
        if not p.name.endswith(".reveal.json"):
            lines |= set(json.loads(p.read_bytes())["reference"])
    assert {k for k in lines} <= {n[len("assets/"):-len(".html")] for n in pages if n.startswith("assets/")}
    assert len([n for n in pages if n.startswith("assets/")]) == 19
    home = pages["index.html"]
    for k in lines:
        assert f'href="assets/{k}.html"' in home, k                          # every row links to its page
    brk = pages["assets/BRK_B.html"]
    assert ">BRK.B</span>" in brk and "Berkshire Hathaway B" in brk


def test_a_line_that_left_the_book_still_gets_a_page(site):
    lines = site.Lines({"lines": [{"symbol": "NDX", "name": "Nasdaq-100", "asset_class": "index"}]})
    lines.seen(["NDX", "ZZZ", "BRK_B", "../etc", "UNMAPPED_1"])
    assert "ZZZ" in lines.info and lines.name("BRK_B") == "BRK.B"
    assert "../etc" not in lines.info and "UNMAPPED_1" not in lines.info     # only public line ids
    assert site.asset_page("BRK_B") == "assets/BRK_B.html" and site.asset_page("../x") == ""


def test_asset_page_has_position_chart_trend_voices_trades_and_facts(built):
    out, pages = built
    semis = pages["assets/SEMIS.html"]
    for anchor in ("position", "weights", "trend", "said", "trades", "facts"):
        assert f'id="{anchor}"' in semis, anchor
    assert "<dt>Weight</dt><dd class=\"lg-v\">6.7%</dd>" in semis and "<dt>Reference</dt><dd class=\"lg-v\">13.4%</dd>" in semis
    assert "vehicle: real · 1x" in semis and "LSE hours" in semis
    # the step chart: three series, a zero baseline, a hover title per run, and its table twin
    chart = semis[semis.index('<figure class="wchart">'):semis.index("</figure>", semis.index('<figure class="wchart">'))]
    for css in ("s-ref", "s-council", "s-held"):
        assert f'<path class="wc-line {css}" d="M' in chart, css
    assert 'class="wc-zero"' in chart and chart.count('<rect class="wc-hit"') == 5
    assert ("<title>25 Sep 2026, 14:40 UTC (executed): Reference 13.4% · Council 6.7% · Executed book 6.7%</title>"
            in chart)
    assert chart.count('<rect class="wc-reh"') == 1 and chart.count('class="wc-dot ') == 4   # rehearsal: no fill
    assert chart.count('class="wc-x wc-x-') == 5                              # a date under each of 5 runs
    assert 'preserveAspectRatio="none"' in chart and "<script" not in chart
    assert "Show as table" in semis and "<th class=\"n\">Executed book</th>" in semis
    # what the agents said about it: claims, answers, decisions, cards, each opening its place in the run
    said = semis[semis.index('id="said"'):semis.index('id="trades"')]
    assert 'href="../cycles/2026-09-25T1440Z.html#a-pm-1"' in said and "Portfolio manager, attempts 1 (used) and 2" in said
    assert 'href="../cycles/2026-09-25T1440Z.html#cl-bear-c1"' in said
    assert "Volatility officer" in said and "Asked to cut it from 13.4% to 6.7%." in said
    # its trades, with the why; the rejected proposal says it never traded
    trades = semis[semis.index('id="trades"'):semis.index('id="facts"')]
    assert "Council: cut (manager attempt 1)" in trades and ">EXECUTED</span>" in trades and ">REJECTED</span>" in trades
    facts = semis[semis.index('id="facts"'):]
    assert 'href="../cycles/2026-09-25T1440Z.html#f-' in facts


def test_a_short_line_page(built):
    _, pages = built
    gbp = pages["assets/GBPUSD.html"]
    assert '<span class="pos pos-short">Short</span>' in gbp and '<span class="lev">2x</span>' in gbp
    assert "vehicle: CFD · 2x" in gbp and "−6.2%" in gbp
    assert "Short from 0% to −6.2%" in gbp
    assert "CFD · 2x" in gbp[gbp.index('id="trades"'):]


def test_lines_are_linked_from_run_and_agent_pages(built):
    _, pages = built
    run = pages["cycles/2026-09-25T1440Z.html"]
    changed = run[run.index('id="changed"'):run.index('id="officers"')]
    assert '<a class="ln" href="../assets/GBPUSD.html">Pound / US dollar</a>' in changed
    costs = run[run.index('id="a-costs"'):run.index('id="a-decision"')]
    assert '<a class="ln" href="../assets/NVDA.html">NVIDIA</a>' in costs
    assert re.search(r'<a class="ev-ln" href="\.\./assets/SEMIS\.html">Semiconductors</a>', run)
    facts = run[run.index('id="facts"'):]
    assert '<a class="ln" href="../assets/NDX.html">Nasdaq-100</a>' in facts
    assert re.search(r'<a class="ev-ln" href="\.\./assets/[A-Z_]+\.html">', pages["agents/bear.html"])


def test_asset_pages_are_percent_only(built):
    _, pages = built
    for name, html in pages.items():
        if not name.startswith("assets/"):
            continue
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<head>.*?</head>", " ", html, flags=re.S))
        assert not re.search(r"[$€£]\s?\d", text), name
        assert not re.search(r"\b\d{7,}\b", text), name
        assert "None" not in text.split() and "Undefined" not in text, name


# ------------------------------------------------------------------------------ the human operator
def test_the_human_operator_has_a_page_of_decisions(built):
    _, pages = built
    human = pages["agents/human.html"]
    assert "Human operator" in human and "a person" in human
    assert human.count('<details class="entry" id="run-') == 5
    assert "Reason given: Oil short into the OPEC+ meeting" in human and ">REJECTED</span>" in human
    assert "Proposals to decide</dt><dd class=\"st-v\">2</dd>" in human
    assert 'href="../cycles/2026-09-25T1440Z.html#a-decision"' in human


# ------------------------------------------------------------------------------ fonts and notices
def test_fonts_are_self_hosted_and_noticed(built):
    out, _ = built
    css = (out / "static" / "style.css").read_text()
    faces = re.findall(r"@font-face \{([^}]*)\}", css)
    assert len(faces) == 6
    for face in faces:
        assert "font-display: swap" in face
        ref = re.search(r'url\("(fonts/[^"]+\.woff2)"\)', face).group(1)
        path = out / "static" / ref
        assert path.exists() and path.read_bytes()[:4] == b"wOF2", ref
    shipped = sorted(p.name for p in (out / "static" / "fonts").glob("*.woff2"))
    assert shipped == sorted(re.findall(r'url\("fonts/([^"]+)"\)', css))       # nothing unused is shipped
    # the licence travels with the fonts (OFL condition 2): notices and the full licence text
    ofl = (out / "static" / "fonts" / "OFL.txt").read_text()
    assert "SIL OPEN FONT LICENSE Version 1.1" in ofl and 'with Reserved Font Name "Plex"' in ofl
    assert "Copyright 2020 The Space Grotesk Project Authors" in ofl
    for family in ("Space Grotesk", "IBM Plex Sans", "IBM Plex Mono"):
        assert f'font-family: "{family}"' in css
    notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    assert "SIL OPEN FONT LICENSE Version 1.1" in notices
    assert "Copyright 2020 The Space Grotesk Project Authors" in notices and "Copyright 2017 IBM Corp." in notices
    for name in shipped:
        assert name in notices, name


def test_motion_is_subtle_and_can_be_turned_off():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-reduced-motion: no-preference)" in css
    for rule in re.findall(r"animation: [a-z-]+ ([\d.]+)s", css):
        assert float(rule) <= 0.5
    assert "box-shadow: 0 " not in css.replace("box-shadow: 0 0 0 ", "")      # no drop shadows for depth
    assert "backdrop-filter" not in css and "radial-gradient(1100px" not in css


# ------------------------------------------------------------------------------ review fixes (v4.1)
def test_executed_weight_is_only_stated_when_the_record_knows_it(site):
    """The executed book is the achieved weight (or the target) of a completed run, the unchanged book
    of a run that traded nothing, and unknown (None) while the outcome is not final."""
    cycles = site.load_journal(FIXTURE).cycles
    done = next(cv for cv in cycles if cv.final_state == "completed")
    rejected = next(cv for cv in cycles if cv.final_state == "rejected")
    rehearsal = next(cv for cv in cycles if cv.rehearsal)
    assert abs(site.executed_weight(done, "SEMIS") - done.execution.achieved_x["SEMIS"]) < 1e-9
    assert site.executed_weight(rejected, "SEMIS") == rejected.doc.risk.base_x["SEMIS"]
    assert site.executed_weight(rehearsal, "SEMIS") is None
    for state in ("blocked", "execution_unknown", "approved", "executing", "proposed", "awaiting_publication"):
        cv = site.CycleView(doc=done.doc.model_copy(update={"decision": done.doc.decision.model_copy(
            update={"state": state})}), path="x")
        assert site.executed_weight(cv, "SEMIS") is None, state            # no execution file: not known
    partial = site.CycleView(doc=done.doc, path="x", execution=done.execution.model_copy(update={
        "decision_state": "completed_partial", "achieved_x": {}}))
    assert partial.final_state == "completed_partial"
    assert site.executed_weight(partial, "SEMIS") == done.doc.risk.base_x["SEMIS"]   # a leg with no known fill
    assert site.executed_weight(partial, "NDX") == done.doc.risk.final_x["NDX"]      # no leg: nothing moved it


def test_trend_section_follows_the_policy_and_says_why(built):
    _, pages = built
    semis = pages["assets/SEMIS.html"]
    trend = semis[semis.index('id="trend"'):semis.index('id="said"')]
    assert "mixed = ¾" in trend and "three quarters" not in trend              # from policy/reference.yaml
    assert "Uptrend: cut allowed by a qualifying card" in trend and "ev-by-vol" in trend   # the reason and its card
    assert "(6.7% to 13.4% of the portfolio)" in trend                         # the range as portfolio shares
    assert re.search(r'<span class="tr-when">\d runs · ', trend)                # identical runs merged
    assert trend.index("24 Sep 06:40") < trend.index("25 Sep 14:40")          # oldest first, like the chart
    gbp = pages["assets/GBPUSD.html"]
    trend = gbp[gbp.index('id="trend"'):gbp.index('id="said"')]
    assert "Outside the reference book: its reference size is always 0." in trend
    gold = pages["assets/GOLD.html"]
    assert "size 0.75" in gold[gold.index('id="trend"'):gold.index('id="said"')]  # the fixture matches the policy


def test_lines_are_linked_wherever_structured_data_names_them(built):
    _, pages = built
    semis_link = '<a class="ln" href="../assets/SEMIS.html">Semiconductors</a>'
    gbp_link = '<a class="ln" href="../assets/GBPUSD.html">Pound / US dollar</a>'
    pm = pages["agents/pm.html"]
    assert f"Decision: <strong>cut {semis_link} from 13.4% to 6.7% and short {gbp_link}" in pm
    assert f"<p><strong>cut {semis_link} from 13.4% to 6.7%</strong>" in pm   # each attempt's change
    risk = pages["agents/risk.html"]
    assert 'Held back: <a class="ln" href="../assets/OIL.html">Crude oil</a>' in risk
    run = pages["cycles/2026-09-25T1440Z.html"]
    assert f"On {semis_link}" in run                                          # a card's scope
    bull = pages["agents/bull.html"]
    assert f"rebuttal: cut {semis_link} 13.4% → 10.1%" in bull              # the chain names the bull's turns


def test_the_agents_said_section_threads_claims_and_their_fates(built):
    _, pages = built
    ndx = pages["assets/NDX.html"]
    said = ndx[ndx.index('id="said"'):ndx.index('id="trades"')]
    first = said[:said.index("</details>")]
    c3 = first[first.index("A round trip on the real-settled index funds"):]
    c3 = c3[:c3.index("</li>\n")]
    assert "set aside</span> <a" in c3 and "Manager, attempt 1 (used)" in c3   # the dismissal, under its claim
    gbp = pages["assets/GBPUSD.html"]
    assert "the sterling short has a trend behind it" in gbp                 # a concession about the line
    semis = pages["assets/SEMIS.html"]
    assert "Semiconductor volatility is elevated" in semis                   # the name's singular matches
    assert "decisive fact" in semis


def test_agent_pages_fold_their_history_and_open_with_a_banner(built):
    _, pages = built
    bull = pages["agents/bull.html"]
    entries = re.findall(r'<details class="entry" id="run-[^"]+"( open)?>', bull)
    assert len(entries) == 5 and entries[0] == " open" and not any(entries[1:])
    banner = bull[bull.index('id="latest"'):bull.index('id="what"')]
    assert '<section class="card vb vb-agent accent-bull" id="latest"' in bull and "Latest:" in banner and "→" in banner
    chips = re.findall(r'<a class="hc hc-(ok|no|none)" href="[^"]+">', banner)
    assert len(chips) == 5                                                    # one per meeting, linked
    assert "manager agreed" in banner and "the manager did what it asked" in banner   # the glyphs' meaning in words
    assert 'id="overview"' not in bull and bull.index('id="latest"') < bull.index('id="history"')
    assert "<p class=\"big\">" not in bull[bull.index('id="what"'):bull.index('id="numbers"')]   # no repeated lead
    assert 'class="strip strip-agent strip-n6"' in bull


def test_every_page_puts_its_header_before_its_contents(built):
    _, pages = built
    for name in ("cycles/2026-09-25T1440Z.html", "agents/bear.html", "assets/GBPUSD.html", "how.html"):
        html = pages[name]
        assert html.index('<div class="page-hero">') < html.index('<nav class="toc"'), name
    gbp = pages["assets/GBPUSD.html"]
    assert '<li class="toc-phone"><details class="toc-more"><summary>Other lines (18)</summary>' in gbp


def test_a_flat_line_and_a_single_run_get_words_not_an_empty_chart(site, built, tmp_path):
    _, pages = built
    eur = pages["assets/EURUSD.html"]
    weights = eur[eur.index('id="weights"'):eur.index('id="trend"')]
    assert "0% in all 5 runs: reference, council and executed book." in weights and "wc-svg" not in weights
    journal = _copy_fixture(tmp_path, keep={"2026-24T0640Z", "2026-09-24T0640Z"})
    (journal / "book" / "latest.json").unlink()
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    ndx = (out / "assets" / "NDX.html").read_text()
    weights = ndx[ndx.index('id="weights"'):ndx.index('id="trend"')]
    assert "wc-svg" not in weights and "Reference <strong class=\"n\">35.0%</strong>" in weights
    assert "(rehearsal: nothing traded)" in weights


def test_orders_say_buy_or_sell_and_colour_only_opening_positions(built):
    _, pages = built
    semis = pages["assets/SEMIS.html"]
    trades = semis[semis.index('id="trades"'):semis.index('id="facts"')]
    assert '<strong class="ord-v">sell</strong> · partial close <span class="chip pos-chip pc-neutral">long position</span>' in trades
    gbp = pages["assets/GBPUSD.html"]
    assert '<strong class="ord-v">sell</strong> · open <span class="chip pos-chip pc-short">short position</span>' in gbp


def test_only_agents_wear_seat_colours(built):
    _, pages = built
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    assert ".ev-card { --c: var(--line-strong); }" in css and ".ev-by-vol { --c: var(--seat-vol); }" in css
    assert ".kind-llm, .kind-code, .kind-human, .auth-decides, .auth-advises, .auth-context, .auth-code { --c: var(--line-strong); }" in css
    assert ".ring-fallback { --rc: var(--muted); }" in css
    run = pages["cycles/2026-09-25T1440Z.html"]
    assert 'class="ev ev-card ev-by-vol"' in run                             # a card chip by its author
    semis = pages["assets/SEMIS.html"]
    assert '<section class="card accent-neutral" id="said"' in semis
    assert re.search(r'<a class="as accent-bull tg-ok" href="[^"]+#a-rebuttal"[^>]*>.*?bull reply', pages["index.html"], re.S)


def test_the_phone_roster_is_a_compact_list():
    css = (REPO_ROOT / "site" / "static" / "style.css").read_text()
    phone = css[css.index("@media (max-width: 640px)"):]
    assert ".phase-what, .ac-job, .ac-kind, .ac-label { display: none; }" in phone
    assert ".ac-link { grid-template-columns: 28px minmax(0, 1fr);" in phone
    assert ".pos-line { display: block; }" in phone and ".council-chamber { order: -1; }" not in css
