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


def test_home_shows_the_paper_portfolio_and_the_latest_decision(built):
    _, _, pages = built
    home = _text(pages["index.html"])
    assert "The paper portfolio" in home and "Latest decision #2" in home and "what we chose" in home
    assert "Swing / core" in home and "Open paper swing trades" in home
    assert 'href="decisions/2/index.html"' in pages["index.html"]
    assert home.index("The paper portfolio") < home.index("The book")         # the paper portfolio comes first


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
    t = _text(pages["decisions/1/index.html"])
    for words in ("What we chose", "Reading list", "Movers screen", "Market context", "Scout ideas and fact cards",
                  "Code gate", "Skeptic", "Bull and bear", "Manager attempts and the vote", "Tally",
                  "S-rules and would-be paper legs", "Swing budget", "Real outcome vs the full trace",
                  "Core council", "What would change its mind"):
        assert words in t, words
    html = pages["decisions/1/index.html"]
    for seat in ("bull", "bear", "pm", "risk", "news", "macro", "scout", "skeptic"):
        assert f"accent-{seat}" in html, seat
    assert "prnewswire_all headline (licensed)" in t                         # N: id + source only


def test_nothing_licensed_or_in_money_terms_in_any_paper_output(built):
    _, dest, pages = built
    for name, html in pages.items():
        assert SYNTH_RSS not in html and "investor day" not in html, name
    files = [p for p in dest.rglob("*") if p.is_file() and ("decisions" in p.parts or "paper" in p.parts)]
    assert files
    for p in files:
        assert leakscan.scan_bytes(p.name, p.read_bytes(), licensed_texts=[SYNTH_RSS]) == [], p
        assert "$" not in p.read_text()
