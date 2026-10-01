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


def test_home_renders_the_paper_book_as_the_portfolio(built_book):
    dest, pages = built_book
    html = pages["index.html"]
    home = _text(html)
    assert "The paper portfolio" in home and "as of" in home and "PAPER" in home
    assert "paper return since" in home
    for line in ("NDX", "SEMIS", "BTC", "ACME"):
        assert line in home, line
    assert "Core 6" in home or "Core" in home
    assert "pbook-split" in html and "seg-core" in html and "seg-swing" in html and "seg-cash" in html
    assert "accent-scout" in html and "accent-pm" in html                    # seat colours
    assert home.index("The paper portfolio") < home.index("The book")
    assert f'content="{CSP}"' in html and "<script" not in html.lower() and 'style="' not in html
    assert "Core lines (target weight)" not in home                          # the book replaces the target list


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
        assert "paper: " in t                                                 # the paper stage chip instead

