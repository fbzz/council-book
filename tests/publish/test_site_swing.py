"""SW-7 site (swing-book.md rev 2, §7.4 / §8.4): the run page's "Swing ideas" and "Swing book", the
/swing/ page, the Scout and Skeptic cards and pages, and the asset page of a swing ticker. No
JavaScript, the fixed CSP on every page, percent-only, PAPER rows labelled in words."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish import leakscan
from tests.swing import public_fixture as F

SITE_BUILD = REPO_ROOT / "site" / "build.py"
CSS = REPO_ROOT / "site" / "static" / "style.css"
NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "script-src 'none'; base-uri 'none'; form-action 'none'")
SURFACES = ("--bg", "--card", "--raised", "--inset")


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_swing", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_swing"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_site()


@pytest.fixture(scope="module")
def journal_dir(tmp_path_factory, _core_policy):
    return F.make_swing_journal(tmp_path_factory.mktemp("swing_journal"), _core_policy)


@pytest.fixture(scope="module")
def built(site, journal_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("swing_site") / "site"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return out, {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*.html")}


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_site_builds_with_the_fixed_csp_and_no_script(built):
    out, pages = built
    assert {"swing/index.html", "agents/scout.html", "agents/skeptic.html"} <= set(pages)
    for name, html in pages.items():
        if name in ("council.html", "book.html", "failures.html"):
            continue
        assert f'<meta http-equiv="Content-Security-Policy" content="{CSP}">' in html, name
        assert "<script" not in html and " style=" not in html, name
    assert leakscan.scan_paths([out]) == []


def test_swing_joins_the_menu(built):
    _, pages = built
    assert '<a href="swing/index.html">Swing</a>' in pages["index.html"]
    assert '<a href="../swing/index.html" aria-current="page">Swing</a>' in pages["swing/index.html"]


def test_run_page_shows_swing_ideas_and_book(built):
    _, pages = built
    run = pages[f"cycles/{F.SWING_CYCLE}.html"]
    assert 'id="swing-ideas"' in run and 'id="swing-book"' in run
    text = _text(run)
    for word in ("ACME", "WIDG", "GLOBEX", "HOOLI", "planned", "waiting (Skeptic)", "stopped by the Skeptic",
                 "priced in: mostly", "said pass", "3 of 3", "paper-only setup", "Skeptic health"):
        assert word in text, word
    assert "PAPER" in run and "live setup" in text
    assert F.N_GLOBEX in run and "broker news item, id only" in text
    assert F.FEED_TITLE not in run and F.LIVE_VALUE not in run
    assert "rules written before the first trade" in text and "1.25% per trade leg" in text
    assert 'class="sw-bar"' in run and "stop 6.00% · target 15.00%" in text


def test_swing_page_has_trades_metrics_benchmarks_funnel_and_health(built):
    _, pages = built
    text = _text(pages["swing/index.html"])
    for word in ("Open trades", "Closed trades", "Pre-registered metrics", "90% bootstrap interval (n = 2)",
                 "SQ-8 mechanical rule (PAPER)", "Matched index", "Index held", "Idea funnel",
                 "Skeptic said wait", "Dropped by code (eligible names)", "Paper-only setups", "Missed entries",
                 "Manager passed", "Skeptic rejected", "Executed", "last canary caught", "+2.50R", "−1.50R",
                 "target", "stop", "live since 21 Sep 2026"):
        assert word in text, word
    assert pages["swing/index.html"].count(">PAPER<") >= 7                 # every funnel group says PAPER


def test_scout_and_skeptic_cards_explain_themselves(built):
    _, pages = built
    idx = pages["agents/index.html"]
    assert 'class="agent-card accent-scout"' in idx and 'class="agent-card accent-skeptic"' in idx
    assert "Reads the news and proposes swing ideas." in _text(idx)
    assert "blind to the pitch, whether the news is already priced in" in _text(idx)
    assert "bigger picture" in _text(pages["agents/skeptic.html"])
    assert 'href="../agents/scout.html"' in pages["swing/index.html"]


def test_asset_pages_exist_for_swing_tickers_in_the_last_90_days(site, built, journal_dir, tmp_path):
    _, pages = built
    for k in ("GLOBEX", "HOOLI", "WIDG", "ACME"):
        assert f"assets/{k}.html" in pages, k
    assert 'id="swing"' in pages["assets/NVDA.html"]                     # an existing line page gains a section
    later = tmp_path / "later"
    site.build(journal_dir, PROMPTS_DIR, POLICY_DIR, later, now=NOW + timedelta(days=120))
    assert not (later / "assets" / "GLOBEX.html").exists()               # idea too old, no trade
    assert 'id="swing"' in (later / "assets" / "NVDA.html").read_text()   # the open trade keeps it


def test_a_core_only_journal_builds_without_swing_pages(site, tmp_path):
    fixture = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"
    out = tmp_path / "core"
    site.build(fixture, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    assert not (out / "swing").exists() and not (out / "agents" / "scout.html").exists()
    assert "swing/index.html" not in (out / "index.html").read_text()


# ------------------------------------------------------------------------------ colours
def _tokens() -> dict[str, str]:
    css = CSS.read_text()
    block = css[css.index(':root,\n:root[data-theme="dark"] {'):]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block[:block.index("}")]))


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_scout_and_skeptic_colours_are_the_users_and_meet_contrast_on_the_dark_surfaces():
    t = _tokens()
    assert t["--seat-scout"].lower() == "#3fb8e6" and t["--seat-skeptic"].lower() == "#e05fb0"
    for seat in ("--seat-scout", "--seat-skeptic"):
        for surface in SURFACES:
            assert _contrast(t[seat], t[surface]) >= 4.5, (seat, surface)   # WCAG AA as text
    seats = {k: v.lower() for k, v in t.items() if k.startswith("--seat-")}
    assert len(set(seats.values())) == len(seats)                           # no two agents share a colour
    css = CSS.read_text()
    assert ".accent-scout { --accent: var(--seat-scout); }" in css
    assert ".accent-skeptic { --accent: var(--seat-skeptic); }" in css
