"""The site's paper pages: the home paper panel ("Latest decision #N — what we chose"), /decisions/
(the numbered history) and /decisions/<n>/ (the whole flow). No JavaScript, the fixed CSP on every
page, PAPER badged, nothing licensed and nothing in money terms. Synthetic fixtures only."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta

import pytest

from council.models.cycle import CycleRecord
from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import leakscan
from council.publish import paper as P
from tests.swing import public_fixture as F
from tests.swing.test_paper_publish import SYNTH_RSS, published  # noqa: F401 - the fixture

SITE_BUILD = REPO_ROOT / "site" / "build.py"
NOW = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "script-src 'none'; base-uri 'none'; form-action 'none'")


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_paper", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_paper"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(published, tmp_path_factory, _core_policy):  # noqa: F811
    ctx, out, repo = published
    root = tmp_path_factory.mktemp("paper_site")
    journal = F.make_swing_journal(root, _core_policy)
    shutil.copytree(repo / "journal" / "paper", journal / "paper")
    # a second paper decision, four hours later, with no swing slot: "no trade"
    rec = CycleRecord.model_validate(ctx.ledger.get_cycle(out.cycle_id))
    slot = rec.slot + timedelta(hours=4)
    cid = slot.strftime("%Y-%m-%dT%H%MZ")
    r = rec.model_copy(update={"cycle_id": cid, "slot": slot,
                               "extras": {k: v for k, v in rec.extras.items() if k != "swing"}})
    files, _ = P.paper_files(lambda no: P.build_paper_cycle(r, None, lines=ctx.policy.universe, decision_no=no,
                                                            own_texts=[]), journal.parent, cid,
                             sealed_at=NOW)
    P.write(journal.parent, files)
    site = _load_site()
    dest = root / "site"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, dest, now=NOW)
    return out, dest, {p.relative_to(dest).as_posix(): p.read_text() for p in dest.rglob("*.html")}


@pytest.fixture(scope="module")
def site():
    return _load_site()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_the_paper_pages_build_with_the_fixed_csp_and_no_script(built):
    _, _, pages = built
    assert {"decisions/index.html", "decisions/1/index.html", "decisions/2/index.html"} <= set(pages)
    for name in ("index.html", "decisions/index.html", "decisions/1/index.html", "decisions/2/index.html"):
        html = pages[name]
        assert f'content="{CSP}"' in html, name
        assert "<script" not in html.lower(), name
        assert "PAPER" in html, name


def test_home_has_a_path_to_the_decisions_and_no_second_portfolio(built):
    _, _, pages = built
    html = pages["index.html"]
    home = _text(html)
    assert "Latest decision #2" in home and "Open decision #2" in home and "All decisions" in home
    assert 'class="dpath"' in html and 'href="decisions/2/index.html"' in html and 'href="decisions/1/index.html"' in html
    assert html.count('class="dpath-list"') == 1 and "#1" in home and "#2" in home
    # the old paper panel and its "what we chose" block are gone: the decision page carries them
    assert "The paper portfolio" not in home and "what we chose" not in home and "paper-panel" not in html
    assert home.index("The book") < home.index("Latest decision #2") < home.index("The council")


def test_the_decisions_table_is_numbered_newest_first(built):
    _, _, pages = built
    html = pages["decisions/index.html"]
    t = _text(html)
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    assert body.index(">#2<") < body.index(">#1<")
    assert 'href="../decisions/1/index.html"' in html and "VERIFIED" in t
    assert "ACME long 8%" in t and "no trade" in t
    assert 'href="decisions/index.html"' in pages["index.html"]                # in the menu


def test_a_decision_page_shows_the_whole_flow(built):
    _, _, pages = built
    html = pages["decisions/1/index.html"]
    t = _text(html)
    for words in ("Closest to trading" if "No trade" in t else "Entered", "Core", "Swing budget",
                  "Every idea's journey", "Scout", "Gate", "Skeptic", "Debate", "PM", "Rules",
                  "What would change its mind", "Core council", "Manager proposed", "Inputs",
                  "The full reading list", "Movers screen and market context"):
        assert words in t.replace("&#39;", "'"), words
    for seat in ("bull", "bear", "pm", "risk", "news", "macro", "scout", "skeptic"):
        assert f"accent-{seat}" in html, seat
    assert html.count('class="jc') >= 1 and "<details" in html                # journey cards, no script
    assert "licensed headline · prnewswire_all" in t or "licensed headline" in t   # N: id + source only
    assert "SKEPTIC: SKEPTIC" not in t.upper().replace("  ", " ")             # no duplicated labels
    assert not re.search(r"(S\d+:[a-z_]+)\W+\1", t)
    assert "Withheld until the data licence is widened" not in t


def test_paper_pages_carry_the_paper_status_chip(built):
    _, _, pages = built
    for name in ("decisions/1/index.html", "decisions/2/index.html"):
        html = pages[name]
        assert "PAPER · NO BROKER" in html, name
        assert "REHEARSAL · NO ACCOUNT" not in html, name


def test_no_external_request_from_a_decision_page(built):
    _, _, pages = built
    for name in ("decisions/1/index.html", "decisions/2/index.html"):
        html = pages[name]
        assert not re.search(r'<(?:img|link|script|iframe|source|video|audio)[^>]+(?:src|href)="(?:https?:)?//', html)
        assert 'style="' not in html
        for href in re.findall(r'href="(https?://[^"]+)"', html):           # links only: .gov sources, the repo
            assert re.match(r"https://([A-Za-z0-9.-]+\.gov/|github\.com/)", href), href


def _journey_fixture(built):
    """Decision #1 with its first idea rewritten as "stopped at the swing rules (S8 illiquid) after
    the manager voted 3 of 3 to enter" and another idea stopped at the Skeptic."""
    out, dest, _ = built
    site = _load_site()
    raw = next((dest / "journal" / "paper" / "cycles").rglob(f"{out.cycle_id}.json")).read_bytes()
    doc = P.PublicPaperCycle.model_validate_json(raw)
    sw = doc.swing
    first, *rest = sw.ideas
    ref = first.idea.ref
    rules = first.model_copy(update={"real_outcome": P.PaperOutcome(stage="rules", code="S8:illiquid"), "leg": None,
                                     "traced_leg": P.PaperLeg(ok=False, rule="S8", code="illiquid"), "chosen": False,
                                     "batch": 0, "same": True})
    others = [r.model_copy(update={"real_outcome": P.PaperOutcome(stage="skeptic", code="skeptic_reject"), "leg": None,
                                   "chosen": False}) for r in rest]
    b0 = sw.batches[0]
    tally = [t for t in b0.tally if t.ref != ref] + [P.PaperTally(ref=ref, action="enter", votes_for=3, replicates=3)]
    batches = [b0.model_copy(update={"tally": tally, "refs": list(dict.fromkeys([ref, *b0.refs]))}), *sw.batches[1:]]
    doc = doc.model_copy(update={"swing": sw.model_copy(update={"ideas": [*others, rules], "batches": batches})})
    return site, site.decision_view(doc, True, site.Lines({"lines": [{"symbol": s} for s in ("NDX", "SPX")]})), ref


def test_journey_states_for_an_idea_stopped_at_the_rules_after_pm_3_of_3(built):
    _, d, ref = _journey_fixture(built)
    j = next(x for x in d["journeys"] if x["ref"] == ref)
    assert [s["state"] for s in j["steps"]] == ["ok", "ok", "ok", "ok", "ok", "stop"]
    assert [s["key"] for s in j["steps"]] == ["scout", "gate", "skeptic", "debate", "pm", "rules"]
    assert j["votes"] == (3, 3) and j["steps"][4]["word"] == "3/3" and j["steps"][5]["word"] == "S8"
    assert j["reason"] == "too little trading volume" and j["code"] == "S8:illiquid"
    assert j["stop_label"] == "Rules"
    if len(d["journeys"]) > 1:
        sk = next(x for x in d["journeys"] if x["ref"] != ref)
        assert [s["state"] for s in sk["steps"]] == ["ok", "ok", "stop", "skip", "skip", "skip"]


def test_banner_picks_the_idea_that_got_furthest(built):
    _, d, ref = _journey_fixture(built)
    assert d["journeys"][0]["ref"] == ref                                     # furthest first
    bn = d["banner"]
    assert not bn["entered"] and bn["closest"]["ref"] == ref
    assert d["core_line"]["verb"] in ("built", "held") or d["core_line"]["verb"].startswith("changed")


def test_journey_page_renders_the_banner_and_the_stop_reason(built):
    site, d, ref = _journey_fixture(built)
    env = site.make_env(["NDX", "SPX"])
    tpl = env.get_template("decision.html.j2")
    status = {"css": "rehearsal", "label": "x", "prelive": True, "mode": None, "paper": True, "built": ""}
    html = tpl.render(d=d, book=None, root="../../", page="decisions", nav=[], status=status, csp="",
                      brand_seats=site.hemicycle_seats())
    t = _text(html)
    assert "Closest to trading:" in t and "stopped at Rules (too little trading volume)" in t
    assert "after the manager voted 3 of 3 to enter" in t
    assert 'aria-current="step"' in html and "jp-stop" in html and "S8:illiquid" in html
    assert t.count("S8:illiquid") <= 2                                       # the code chip once per card


def test_nothing_licensed_or_in_money_terms_in_any_paper_output(built):
    _, dest, pages = built
    for name, html in pages.items():
        assert SYNTH_RSS not in html and "investor day" not in html, name
    files = [p for p in dest.rglob("*") if p.is_file() and ("decisions" in p.parts or "paper" in p.parts)]
    assert files
    for p in files:
        assert leakscan.scan_bytes(p.name, p.read_bytes(), licensed_texts=[SYNTH_RSS]) == [], p
        assert "$" not in p.read_text()


# ------------------------------------------------------------------ the paper BOOK as the portfolio
@pytest.fixture(scope="module")
def built_book(published, tmp_path_factory, _core_policy):  # noqa: F811
    from council.paperbook import PaperBook, paper_book_public

    ctx, out, repo = published
    root = tmp_path_factory.mktemp("paper_site_book")
    journal = F.make_swing_journal(root, _core_policy)
    state = root / "state" / "paper"
    book = PaperBook.start(state, 2000.0, at=NOW)
    book.trade_core({"NDX": 0.3, "SEMIS": 0.2, "BTC": 0.13}, {"NDX": 400.0, "SEMIS": 250.0, "BTC": 60000.0},
                    lambda s, b, a: 4.0, at=NOW, cycle_id="c1")
    book.enter_swing(trade_id="trade:x", ticker="ACME", side="long", line="SW_ACME", size_nav=0.08, entry_ref=50.0,
                     stop_pct=0.05, target_pct=0.08, entry_day="2026-10-01", time_stop_day="2026-10-15",
                     setup="news_continuation", at=NOW, cycle_id="c1")
    book.save()
    rec = CycleRecord.model_validate(ctx.ledger.get_cycle(out.cycle_id))
    files, _ = P.paper_files(lambda no: P.build_paper_cycle(rec, None, lines=ctx.policy.universe, decision_no=no,
                                                            own_texts=[]), journal.parent, rec.cycle_id,
                             sealed_at=NOW, book=paper_book_public(state))
    assert P.book_file(1) in files
    P.write(journal.parent, files)
    site = _load_site()
    dest = root / "site"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, dest, now=NOW)
    return dest, {p.relative_to(dest).as_posix(): p.read_text() for p in dest.rglob("*.html")}


def test_home_renders_the_paper_book_in_the_book_map_and_the_list(built_book):
    import json

    dest, pages = built_book
    html = pages["index.html"]
    home = _text(html)
    latest = json.loads((dest / "journal" / "paper" / "latest.json").read_text())
    book = latest["book"]
    # one book only: the council-chamber map, PAPER badged, as of the latest decision
    assert "paper-badge" in html.split('id="holdings-h"')[0].split('class="book"')[1]
    assert f"as of decision #{latest['decision_no']}" in home and "Paper for now" in home
    assert "Target book" not in home and "REHEARSAL" not in html.split('id="council"')[0].split('class="book"')[1]
    assert "pbook" not in html and "The paper portfolio" not in home and "paper-panel" not in html
    # the weights come from the paper book (map tiles and list rows), swing trades in their own group
    for h in book["core"]:
        w = f"{h['weight_pct']:.1f}%"
        assert re.search(rf"{h['line']}.{{0,400}}?{re.escape(w)}", home, re.S), (h["line"], w)
    assert "Swing trades" in home and "ACME" in home and "tgrp" in html and "bc-swing" in html
    assert f"{book['cash_pct']:.1f}%" in home                                # the cash tile / Cash stat
    assert "Swing / core" in home and "swing budget" in home and "Kill switch" in home and "Paper return" in home
    assert html.count('class="htable"') <= 2                                 # the list (+ its Not held fold) only
    assert f'content="{CSP}"' in html and "<script" not in html.lower() and 'style="' not in html


def test_home_falls_back_to_the_target_without_paper(_core_policy, tmp_path):
    root = tmp_path / "j"
    journal = F.make_swing_journal(root, _core_policy)
    site = _load_site()
    dest = tmp_path / "site"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, dest, now=NOW)
    html = (dest / "index.html").read_text()
    head = html.split('id="council"')[0]
    assert "paper-badge" not in head and 'class="dpath"' not in head
    assert ">TARGET<" in head or ">LIVE BOOK<" in head


def test_decision_page_shows_the_book_after_it(built_book):
    _, pages = built_book
    t = _text(pages["decisions/1/index.html"])
    assert "The book after this decision" in t and "NDX" in t and "ACME" in t


def test_paper_book_output_is_percent_only(built_book):
    dest, pages = built_book
    raw = (dest / "journal" / "paper" / "books" / "1.json")
    assert raw.exists()
    for blob in (pages["index.html"], pages["decisions/1/index.html"], raw.read_text(),
                 (dest / "journal" / "paper" / "latest.json").read_text()):
        assert "$" not in blob and "2000" not in blob and "60000" not in blob and "position_id" not in blob
        assert not re.search(r"\b(usd|units)\b", blob, re.I)


def test_paper_idea_cards_hide_the_live_only_stage(built, built_book):
    for pages in (built[2], built_book[1]):
        html = pages["decisions/1/index.html"]
        t = _text(html)
        assert "swing book is paper-only" not in t and "swing_book_paper_only" not in html
        assert "Stopped at" in t or "Entered" in t                           # the journey card's outcome instead



def test_site_wide_chip_says_paper_while_paper_runs_lead():
    from types import SimpleNamespace as NS

    site = _load_site()
    old = datetime(2026, 9, 25, 14, 40, tzinfo=UTC)
    cyc = NS(doc=NS(slot=old, cycle_id="2026-09-25T1440Z", mode="rehearsal", late_by_min=0), chip=None)
    st = NS(state="AWAITING_ACCOUNT", last_cycle_at=old, last_cycle_id="2026-09-25T1440Z", note="", kill_state="NORMAL")
    paper = [NS(slot=datetime(2026, 10, 1, 18, 40, tzinfo=UTC))]
    s = site._status_context(NS(status=st, cycles=[cyc], ops=[], paper_rows=paper), NOW)
    assert (s["label"], s["paper"]) == ("PAPER · NO BROKER", True)
    s = site._status_context(NS(status=st, cycles=[cyc], ops=[], paper_rows=[]), NOW)
    assert s["label"] == "REHEARSAL · NO ACCOUNT" and not s["paper"]
    live = NS(state="LIVE", last_cycle_at=old, last_cycle_id="2026-09-25T1440Z", note="", kill_state="NORMAL")
    assert site._status_context(NS(status=live, cycles=[cyc], ops=[], paper_rows=paper), NOW)["label"] != "PAPER · NO BROKER"


def test_redaction_markers_render_as_one_chip_outside_tags_only():
    mod = _load_site()
    html = '<p title="[value removed]">News is [value removed] old; SI [figure withheld]-beta; [withheld: overlaps licensed feed text]</p>'
    out = mod.redacted_chips(html)
    assert 'title="[value removed]"' in out                     # attributes untouched
    assert out.count('class="redacted"') == 3
    assert "[figure withheld]" not in out.split(">", 1)[1]


# ------------------------------------------------------------------ foundation: shared helpers, rules, links
def test_paper_return_splits_into_market_and_costs_once(built_book):
    _, pages = built_book
    home = _text(pages["index.html"])
    assert re.search(r"market [+−]?\d+\.\d+% · costs −\d+\.\d+%", home), home
    assert home.count("costs −") == 1 and home.count("Paper return") == 1


def test_rule_anchor_maps_every_kind_of_code():
    site = _load_site()
    for code, want in (("S8:illiquid", "S8"), ("R14", "R14"), ("NDX: R11", "R11"), ("net_rr_below_min", "S6"),
                       ("setup_paper_only", "SB16"), ("swing_blocker", "R20"), ("R20 blocker", "R20"), ("R4d", "R4d"),
                       ("material_change_required", "MC")):
        assert site.rule_anchor(code) == want, code
    assert site.rule_anchor("nothing_known") is None and site.rule_href("S8:illiquid", "../") == "../rules.html#S8"
    assert 'href="rules.html#S8"' in str(site.rule_link("S8:illiquid"))
    assert str(site.rule_link("odd")).startswith("<code")


def test_rules_page_has_a_card_per_rule_with_numbers_in_percent(built):
    _, dest, pages = built
    html = pages["rules.html"]
    t = _text(html)
    for rid in [f"R{n}" for n in range(1, 22) if n not in (19, 20)] + ["R19", "R20", "R4d", "MC", "IB", "AP", "RC", "PR",
                                                                        "H1", "H7", "budget", "SB16"] + [f"S{n}" for n in range(19)]:
        if rid in ("R21",) or rid.startswith("R") and rid not in t:
            continue
        assert f'id="{rid}"' in html, rid
    assert 'id="R14"' in html and 'id="S8"' in html and 'id="R19"' in html
    assert "cannot change" not in pages["rules.html"].split("<h1>")[1].split("</h1>")[0]
    assert "Show the numbers" in t and "Hard limits" in t and "Core rules" in t and "Swing rules" in t
    assert "S8:illiquid" in t and "too little trading volume" in t                  # codes a rule can emit
    assert "rb-risk" in html and "rb-scout" in html and "rb-skeptic" in html
    for raw in ("proposal_max", "turnover_7d_max", "max_open_risk_nav", "cycle_max_bps", "min_adv_usd"):
        assert raw not in html, raw
    assert "exempt from R13, R14 and R15" in t and "overnight-financing limit still applies" in t
    assert "$" not in html and "50000000" not in html and "50_000_000" not in html
    assert f'content="{CSP}"' in html and "<script" not in html.lower() and 'style="' not in html


def test_decision_pages_deep_link_rules_agents_and_meetings(built):
    _, _, pages = built
    html = pages["decisions/1/index.html"]
    assert re.search(r'class="jc-code rule-link" href="\.\./\.\./rules\.html#(S|SB|R)\d+', html), \
        re.findall(r'jc-code[^>]*>', html)
    for slug in ("scout", "skeptic", "pm", "bull"):
        assert f'href="../../agents/{slug}.html"' in html, slug
    assert 'href="../../cycles.html"' in html and 'href="../index.html"' in html


def test_paper_swing_builds_the_scout_and_skeptic_pages(built):
    _, _, pages = built
    for slug in ("scout", "skeptic"):
        assert f"agents/{slug}.html" in pages, slug
        assert "paper" in _text(pages[f"agents/{slug}.html"])


def test_every_swing_seat_has_a_page_with_paper_data(built):
    _, _, pages = built
    for slug in ("scout", "skeptic", "swing_bull", "swing_bear", "swing_pm"):
        html = pages[f"agents/{slug}.html"]
        assert f'content="{CSP}"' in html and "<script" not in html.lower() and 'style="' not in html, slug
        banner = html[html.index('id="latest"'):html.index('id="job"')]
        assert "Latest:" in banner and "→" in banner, slug                  # what it said -> what happened
        assert ">PAPER<" in banner and 'href="../decisions/1/index.html#d-ideas">Decision #1</a>' in banner, slug
        assert re.search(r'<a class="hc hc-(ok|no|none)" href="\.\./decisions/1/index\.html#d-ideas">', banner), slug
        assert '<details class="entry" id="d-1" open>' in html, slug          # one entry per paper decision
        t = _text(html)
        assert not re.search(r"[$€£]\s?\d", t), slug                           # percent-only
    skeptic = _text(pages["agents/skeptic.html"])
    assert "different model family" not in skeptic and "same model as the Scout" in skeptic
    assert re.search(r"(passed|said wait to|rejected|could not judge) \d", skeptic)
    assert "Open decision #1" in skeptic


def test_agents_index_is_seating_cards_in_decision_order_with_paper(built):
    _, _, pages = built
    idx = pages["agents/index.html"]
    seats = re.findall(r'<li class="seat-card accent-[a-z]+" id="ag-([a-z]+)">', idx)
    assert seats == ["scout", "skeptic", "bull", "bear", "pm", "news", "macro", "risk", "human"]
    scout = idx[idx.index('id="ag-scout"'):idx.index('id="ag-skeptic"')]
    assert "Last call</dt>" in scout and ">PAPER<" in scout and "decision #" in scout
    assert re.search(r"\d+ calls? · \d+% usable · \d+ paper", _text(scout))
    for slug, page in (("bull", "swing_bull"), ("bear", "swing_bear"), ("pm", "swing_pm")):
        card = idx[idx.index(f'id="ag-{slug}"'):]
        assert f'href="{page}.html"' in card[:card.index("</li>")], slug     # the seat's swing page
    assert "swing book" in _text(re.search(r'<p class="lede">.*?</p>', idx, re.S).group(0))
    assert 'id="machinery"' in idx and "— — —" not in idx
    for jargon in ("used attempt", "named it as its side", "set aside"):
        assert jargon not in idx, jargon


def test_core_agent_pages_read_paper_decisions(built):
    _, _, pages = built
    bull = pages["agents/bull.html"]
    assert re.search(r'<details class="entry" id="run-[^"]+-p1"', bull)      # the paper decision's core debate
    assert "Open decision #1 →" in bull and 'href="../decisions/1/index.html#d-core"' in bull
    human = pages["agents/human.html"]
    start = re.search(r'<details class="entry" id="run-[^"]+-p1"', human).start()
    paper = _text(human[start:human.index("</details>", start)])
    assert "nothing is traded, so no one had to approve anything" in paper and "not needed: rehearsal" not in paper


def test_agent_shorthand_reads_in_words(site):
    assert site.plain_terms("mom10d -6.7% and dd52 -13.1%, +3% over SMA50, carry 0.96 bps/day") == (
        "10-day momentum -6.7% and drop from 52-week high -13.1%, +3% over 50-day average, "
        "carry 0.96 basis points/day")
    assert site.plain_terms("vol_ratio 2.1x", "swing") == "volume vs normal 2.1x"
    assert site.plain_terms("[value removed] trend up") == "[value removed] trend up"   # markers and words untouched
    html = '<p title="F:NDX:mom10d">mom10d</p><code>mom10d</code>'
    assert site.plain_html(html) == '<p title="F:NDX:mom10d">10-day momentum</p><code>mom10d</code>'


def test_a_changed_prompt_is_noted_against_the_call(site):
    call = site.SimpleNamespace(role="pm", prompt_id="council-pm/v1", prompt_sha="a" * 64)
    note = site._latest_prompt_note([call], {"pm": {"id": "council-pm/v2", "sha": "b" * 12, "href": ""}})
    assert "council-pm/v1" in note and "now council-pm/v2" in note
    assert site._latest_prompt_note([call], {"pm": {"id": "council-pm/v1", "sha": "a" * 12, "href": ""}}) == ""


def test_meetings_and_role_stats_count_paper_runs(published, tmp_path, _core_policy):  # noqa: F811
    _, _, repo = published
    site = _load_site()
    view = site.load_journal(repo / "journal")
    lines = site.Lines({"lines": []})
    rows = site.meetings(view, lines)
    paper = [r for r in rows if r["kind"] == "paper"]
    assert paper and paper[0]["decision_no"] == max(r.decision_no for r in view.paper_rows)
    assert paper[0]["href"].startswith("decisions/") and paper[0]["verdict"].endswith(".")
    assert rows == sorted(rows, key=lambda r: (r["slot"], r["decision_no"] or 0), reverse=True)
    stats = site.agent_role_stats(view)
    assert stats["scout"]["by_kind"]["paper"] >= 1 and stats["scout"]["calls"] >= 1
    assert {"skeptic", "swing_bull", "swing_bear", "swing_pm", "pm", "bull"} <= set(stats)
    assert view.has_swing


def test_how_page_links_the_latest_paper_decision_with_a_paper_badge(built):
    _, _, pages = built
    how = pages["how.html"]
    nos = sorted(int(m) for m in re.findall(r"^decisions/(\d+)/index\.html$", "\n".join(pages), re.M))
    assert f'href="decisions/{nos[-1]}/index.html">the latest paper decision</a>' in how
    assert "paper-badge" in how
