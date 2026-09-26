"""The book map's "Colour by" toggle (site v4): 1-day move (the default), P/L since open (a live book
only) and asset class, switched without script by radio inputs and ":checked ~" selectors. Each
tile carries its bin for every mode and prints the value; the fills keep tile text at WCAG AA; the
diverging scale has a grey midpoint and symmetric arms; the asset-class palette passes the data-viz
palette checks on the card surface."""

from __future__ import annotations

import importlib.util
import itertools
import json
import math
import re
import sys
from datetime import UTC, datetime

import pytest

from council.paths import POLICY_DIR, PROMPTS_DIR, REPO_ROOT
from council.publish.public_models import PublicBook

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "site_journal" / "journal"
SITE_BUILD = REPO_ROOT / "site" / "build.py"
CSS = REPO_ROOT / "site" / "static" / "style.css"
NOW = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)
BINS = ("d3", "d2", "d1", "n", "u1", "u2", "u3")
CLASSES = ("stock", "fund", "crypto", "commodity", "fx")


def _load_site():
    spec = importlib.util.spec_from_file_location("council_site_build_v4_colours", SITE_BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["council_site_build_v4_colours"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site():
    return _load_site()


@pytest.fixture(scope="module")
def home(site, tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("site_v4_colours") / "site"
    site.build(FIXTURE, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    return (out / "index.html").read_text()


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    css = CSS.read_text()
    block = css[css.index(':root,\n:root[data-theme="dark"] {'):]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block[:block.index("}")]))


# ------------------------------------------------------------------------------ colour maths
def _lin(hex_colour: str) -> tuple[float, float, float]:
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((0.2126 * x[0] + 0.7152 * x[1] + 0.0722 * x[2] for x in (_lin(a), _lin(b))), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _oklab(lin: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = lin
    lms = [0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
           0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
           0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b]
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in lms)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


# Machado, Oliveira & Fernandes (2009), severity 1.0, on linear RGB: the data-viz validator's model
MACHADO = {"protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
           "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))}


def _delta_e(a: str, b: str, kind: str | None = None) -> float:
    """Euclidean OKLab distance x100, optionally under simulated protan/deutan vision."""
    def sim(h: str) -> tuple[float, float, float]:
        lin = _lin(h)
        if kind is None:
            return lin
        return tuple(max(0.0, min(1.0, sum(m * c for m, c in zip(row, lin, strict=True)))) for row in MACHADO[kind])
    return 100 * math.dist(_oklab(sim(a)), _oklab(sim(b)))


def _lch(h: str) -> tuple[float, float]:
    lightness, a, b = _oklab(_lin(h))
    return lightness, math.hypot(a, b)


def _tile(home: str, key: str) -> tuple[str, str]:
    m = re.search(rf'<li class="tm tm-{key}( [^"]*)?">(.*?)</li>', home, re.S)
    assert m, key
    return m.group(1) or "", m.group(2)


# ------------------------------------------------------------------------------ the toggle
def test_the_colour_toggle_is_radio_inputs_and_css(home):
    fig = home[home.index('<figure class="bmap-fig">'):home.index("</figure>")]
    inputs = re.findall(r'<input class="cb-in" type="radio" name="cb" id="cb-([a-z]+)" value="\1"( checked)?>', fig)
    assert [k for k, _ in inputs] == ["day", "pnl", "class"]
    assert [k for k, c in inputs if c] == ["day"]                              # 1-day move is the default
    for key, label in (("day", "1-day move"), ("pnl", "P/L since open"), ("class", "Asset class")):
        assert f'<label class="cb-opt" for="cb-{key}"><span class="sr">Colour the map by </span>{label}</label>' in fig
    # the inputs come before the options, the legend and the map, so ":checked ~" reaches all three
    last_input = fig.rindex('<input class="cb-in"')
    assert last_input < fig.index('class="cb-head"') < fig.index('class="cbk"') < fig.index('<ul class="bmap"')
    assert "<script" not in home.lower() and not re.search(r"\sstyle\s*=", home, re.I)
    css = CSS.read_text()
    # visually hidden but still focusable (never display:none or visibility:hidden), with a visible ring
    cb_in = css[css.index(".cb-in {"):css.index("}", css.index(".cb-in {"))]
    assert "opacity: 0" in cb_in and "display: none" not in cb_in and "visibility" not in cb_in
    for key in ("day", "pnl", "class"):
        assert f'#cb-{key}:focus-visible ~ .cb-head [for="cb-{key}"]' in css
        assert f'#cb-{key}:checked ~ .cb-head [for="cb-{key}"]' in css
        assert f"#cb-{key}:checked ~ .cbk .cbk-{key}" in css
        assert f"#cb-{key}:checked ~ .bmap .tm-dv-{key}" in css
    assert "outline: 2px solid var(--link); outline-offset: 2px; }" in css[css.index('#cb-day:focus-visible'):]
    # the fill rules only set a background colour: a short's coral edge and hatch stay in every mode
    for key, fill in (("day", "--fd"), ("pnl", "--fp"), ("class", "--fc")):
        assert f"#cb-{key}:checked ~ .bmap .tm:not(.cash) .tm-in {{ background-color: var({fill}); }}" in css
    # ... and a 1px dark rule between the edge and the fill keeps the edge visible on a coral or teal fill
    assert (".tm.short .tm-in { box-shadow: inset 3px 0 0 var(--short), inset 4px 0 0 var(--bg); "
            "background-image: repeating-linear-gradient(") in css
    assert ".tm-c .seat { width: 8px; height: 8px; box-shadow: 0 0 0 2px var(--bg); }" in css
    # switching modes never moves the map: the legends share one grid cell
    assert ".cbk { display: grid;" in css and ".cbk-mode { grid-area: 1 / 1;" in css
    # any transition is off when the reader asks for reduced motion
    assert ("@media (prefers-reduced-motion: no-preference) {\n  .tm-in { transition: box-shadow 0.18s var(--ease), "
            "background-color 0.2s var(--ease); }\n}") in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_the_legend_changes_with_the_mode(home, site):
    cbk = home[home.index('<div class="cbk">'):home.index('<ul class="bmap"')]
    day = cbk[cbk.index('cbk-mode cbk-day'):cbk.index('cbk-mode cbk-pnl')]
    pnl = cbk[cbk.index('cbk-mode cbk-pnl'):cbk.index('cbk-mode cbk-class')]
    cls = cbk[cbk.index('cbk-mode cbk-class'):]
    assert "For a short, an up move is a loss." in day                         # 1-day = the instrument's move
    assert "own P/L since it opened" in pnl and "A short that gains is teal." in pnl
    assert re.findall(r'<li class="cbk-b bd-([a-z0-9]+)"', day) == list(BINS)
    assert re.findall(r'<li class="cbk-b bp-([a-z0-9]+)"', pnl) == list(BINS)
    # edge ticks outside the midpoint; the grey bin gets one centred "±" tick (two edge ticks one
    # narrow bin apart collide on a 360px phone)
    ticks = r'<span class="cbk-t( cbk-t-mid)?" aria-hidden="true">([^<]+)</span>'
    assert re.findall(ticks, day) == [("", "−2.5%"), ("", "−1%"), (" cbk-t-mid", "±0.25%"), ("", "+1%"), ("", "+2.5%")]
    assert re.findall(ticks, pnl) == [("", "−15%"), ("", "−5%"), (" cbk-t-mid", "±1%"), ("", "+5%"), ("", "+15%")]
    assert re.search(r'<li class="cbk-b bd-n" title="within ±0.25%"><span class="cbk-sw" aria-hidden="true"></span>'
                     r'<span class="sr">within ±0.25%</span><span class="cbk-t cbk-t-mid"', day)
    css = CSS.read_text()
    assert ".cbk-t-mid { right: auto; left: 50%; transform: translateX(-50%); }" in css
    assert '<span class="sr">within ±0.25%</span>' in day and '<span class="sr">down 2.5% or more</span>' in day
    assert '<span class="sr">gain of 5% to 15%</span>' in pnl
    assert "cbk-nd" not in day and "cbk-nd" not in pnl                         # every fixture line has both values
    assert "cbk-empty" not in cbk
    assert 'aria-label="1-day move, from the biggest fall to the biggest rise"' in day
    assert 'aria-label="P/L since open, from the biggest loss to the biggest gain"' in pnl
    # asset class: the classes on the map, in the list's filter order, each with its square
    assert re.findall(r'<li class="cbk-c bc-([a-z]+)"><span class="cbk-sq"', cls) == list(CLASSES)
    assert "which the boxes and their names also show" in cls


# ------------------------------------------------------------------------------ the bins
def test_bins_follow_the_printed_value(site):
    day, pnl = site.DAY_EDGES, site.PNL_EDGES
    assert day == (0.25, 1.0, 2.5) and pnl == (1.0, 5.0, 15.0)
    cases = {None: "nd", 0.0: "n", 0.244: "n", 0.249: "u1", -0.25: "d1", 0.99: "u1", 1.0: "u2", -2.499: "d3",
             2.5: "u3", -7.0: "d3"}
    for v, b in cases.items():
        assert site.move_bin(v, day) == b, v
    assert [site.move_bin(v, pnl) for v in (-0.99, 1.0, -4.99, 5.0, 14.99, 15.0, -30.0)] == \
        ["n", "u1", "d1", "u2", "u2", "u3", "d3"]


def test_each_tile_carries_its_bin_in_every_mode_and_prints_the_value(site, home):
    book = PublicBook.model_validate_json((FIXTURE / "book" / "latest.json").read_text())
    held = {k: b for k, b in book.lines.items() if abs(b.weight_x) > 1e-9}
    expected = {"AMD": ("u3", "u2"), "ETH": ("d3", "d2"), "GBPUSD": ("n", "n"), "SPX": ("n", "u1"),
                "BTC": ("u2", "u2"), "SMCI": ("d3", "d2"), "NVDA": ("u2", "n"), "META": ("d1", "d1")}
    for k, b in held.items():
        classes, body = _tile(home, k)
        bd = re.search(r"\bbd-([a-z0-9]+)\b", classes).group(1)
        bp = re.search(r"\bbp-([a-z0-9]+)\b", classes).group(1)
        assert bd == site.move_bin(b.day_change_pct, site.DAY_EDGES), k
        assert bp == site.move_bin(b.pnl_since_open_pct, site.PNL_EDGES), k
        if k in expected:
            assert (bd, bp) == expected[k], k
        # the number the colour stands for is on the tile (colour is never the only cue)
        day = re.search(r'<span class="tm-dv tm-dv-day"><span class="tm-dk">1d</span>(.*?)</span></span>', body, re.S)
        pnl = re.search(r'<span class="tm-dv tm-dv-pnl"><span class="tm-dk">P/L</span>(.*?)</span></span>', body, re.S)
        assert day and site.fmt_signed(b.day_change_pct) in day.group(1), k
        assert pnl and site.fmt_signed(b.pnl_since_open_pct) in pnl.group(1), k
        assert re.search(r'<span class="tm-dv tm-dv-class">(Stock|ETF|Index|Crypto|Commodity|FX)</span>', body), k
    # a short keeps its pill and minus sign whatever the colouring
    classes, body = _tile(home, "GBPUSD")
    assert " short" in classes and '<span class="tm-s">short</span>' in body and '<span class="tm-w">−6.2%</span>' in body
    # the class boxes carry the class hook the class mode reads
    for key in CLASSES:
        assert re.search(rf'<li class="tgrp tgrp-{key} bc-{key}[ "]', home), key


def test_a_tile_without_a_value_gets_no_data_not_the_flat_bin(site):
    geo = site.Geometry()
    base = {"weight": 0.2, "ac": "index", "group": "fund", "page": "", "ref": None, "council_moved": False,
            "pnl": "—", "pnl_dir": "none", "pnl_raw": None}
    rows = [{**base, "line": "NDX", "ticker": "NDX", "name": "Nasdaq-100", "day": "+0.10%", "day_raw": 0.1,
             "day_dir": "up"},
            {**base, "line": "SPX", "ticker": "SPX", "name": "S&P 500", "day": "—", "day_raw": None, "day_dir": "none"}]
    bmap = site.book_map({"held": rows, "cash_x": 0.6, "show_pnl": True}, geo)
    tiles = {t["key"]: t for t in bmap["tiles"]}
    assert (tiles["NDX"]["bd"], tiles["SPX"]["bd"]) == ("n", "nd") and tiles["SPX"]["bp"] == "nd"
    assert bmap["nodata"] == {"day": True, "pnl": True} and bmap["default"] == "day"
    assert tiles["cash"]["bd"] == "" and tiles["cash"]["bp"] == ""             # cash keeps its own dashed look
    # nothing to colour by a move: the map opens on asset class instead of an all "no data" map
    only = site.book_map({"held": rows[1:], "show_pnl": False}, site.Geometry())
    assert only["default"] == "class" and [m["key"] for m in only["modes"]] == ["day", "class"]
    assert only["empty"]["day"] and not bmap["empty"]["day"]                   # its legend says so, no unused scale
    css = CSS.read_text()
    assert ".bd-nd { --fd: transparent; }" in css and ".bp-nd { --fp: transparent; }" in css
    assert "#cb-pnl:checked ~ .bmap .bp-nd .tm-in { border: 1px solid var(--line-strong); }" in css


def _target_journal(tmp_path):
    """The fixture without its live book: the home page maps the latest run's target."""
    journal = tmp_path / "journal"
    for p in FIXTURE.rglob("*"):
        if p.is_file():
            dest = journal / p.relative_to(FIXTURE)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    (journal / "book" / "latest.json").unlink()                                # no live book: the target
    status = json.loads((journal / "status.json").read_text())
    (journal / "status.json").write_text(json.dumps({**status, "state": "AWAITING_ACCOUNT"}))
    return journal


def test_a_target_book_offers_no_pnl_colouring(site, tmp_path):
    journal = _target_journal(tmp_path)
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    assert '<span class="chip state-rehearsal">TARGET</span>' in home
    fig = home[home.index('<figure class="bmap-fig">'):home.index("</figure>")]
    assert re.findall(r'<input class="cb-in" type="radio" name="cb" id="cb-([a-z]+)"', fig) == ["day", "class"]
    assert "P/L since open" not in fig and "cbk-pnl" not in fig and "tm-dv-pnl" not in fig
    assert not re.search(r"\bbp-[a-z0-9]+\b", fig)
    assert re.search(r'<li class="tm tm-NDX bd-[a-z0-9]+">', fig)


def test_a_book_without_daily_moves_opens_on_asset_class_and_says_why(site, tmp_path):
    """The rehearsal case: no line has a daily move. The map opens on asset class; the 1-day option
    stays, and its legend says every tile is an empty frame instead of showing an unused scale."""
    journal = _target_journal(tmp_path)
    for p in (journal / "cycles").rglob("*.json"):
        p.write_text(re.sub(r'"day_change_pct":\s*-?[0-9.]+', '"day_change_pct": null', p.read_text()))
    out = tmp_path / "out"
    site.build(journal, PROMPTS_DIR, POLICY_DIR, out, now=NOW)
    home = (out / "index.html").read_text()
    fig = home[home.index('<figure class="bmap-fig">'):home.index("</figure>")]
    inputs = re.findall(r'<input class="cb-in" type="radio" name="cb" id="cb-([a-z]+)" value="\1"( checked)?>', fig)
    assert inputs == [("day", ""), ("class", " checked")]
    day = fig[fig.index("cbk-mode cbk-day"):fig.index("cbk-mode cbk-class")]
    assert "every tile is an empty frame" in day and "cbk-scale" not in day and "cbk-nd" not in day
    assert re.findall(r'<li class="tm tm-[A-Z_]+ bd-([a-z0-9]+)', fig) and \
        set(re.findall(r'<li class="tm tm-[A-Z_]+ bd-([a-z0-9]+)', fig)) == {"nd"}


def test_a_value_is_printed_only_where_its_digits_fit(site):
    """Narrow tiles drop the arrow first, then the value; a short's padding and an 8-character value
    need a wider tile (container queries on the tile's real size), so a number is never clipped."""
    base = {"weight": 0.2, "ac": "stock", "group": "stock", "page": "", "ref": None, "council_moved": False,
            "day": "+0.10%", "day_raw": 0.1, "day_dir": "up"}
    rows = [{**base, "line": "A", "ticker": "A", "name": "A", "pnl": "+123.45%", "pnl_raw": 123.45, "pnl_dir": "up"},
            {**base, "line": "B", "ticker": "B", "name": "B", "pnl": "−13.78%", "pnl_raw": -13.78, "pnl_dir": "down"}]
    tiles = {t["key"]: t for t in site.book_map({"held": rows, "show_pnl": True}, site.Geometry())["tiles"]}
    assert (tiles["A"]["pw"], tiles["A"]["dw"], tiles["B"]["pw"]) == (True, False, False)
    assert (tiles["A"]["bp"], tiles["B"]["bp"]) == ("u3", "d2")
    css = CSS.read_text()
    assert "@container tile (width < 96px) { .tm-d .mv-arrow { display: none; } }" in css
    assert "@container tile (height < 76px) or (width < 68px) { .tm-d { display: none; } }" in css
    assert "@container tile (width < 88px) { .tm.short .tm-d { display: none; } }" in css
    assert ("@container tile (width < 112px) { #cb-day:checked ~ .bmap .tm.dw .tm-d, "
            "#cb-pnl:checked ~ .bmap .tm.pw .tm-d { display: none; } }") in css
    # a narrow class box keeps its whole name rather than the class square (its frame wears the colour):
    # the square never truncates a name that fits without it
    assert "@container tgroup (width < 208px) { #cb-class:checked ~ .bmap .tgrp-h::before { display: none; } }" in css


# ------------------------------------------------------------------------------ the colours
def test_every_bin_and_class_fill_keeps_tile_text_at_wcag_aa(tokens):
    fills = [f"--dv-{b}" for b in BINS] + [f"--ac-{c}-fill" for c in CLASSES]
    for fill in fills:
        for text in ("--ink-strong", "--tile-ink2"):                          # ticker, weight, value · name, keys
            ratio = _contrast(tokens[text], tokens[fill])
            assert ratio >= 4.5, (text, fill, round(ratio, 2))
    # a no-data tile is transparent over the class box (--inset); cash sits on --bg
    for bg in ("--inset", "--bg", "--raised"):
        for text in ("--ink-strong", "--tile-ink2"):
            assert _contrast(tokens[text], tokens[bg]) >= 4.5, (text, bg)
    css = CSS.read_text()
    # moves on a tile are ink, not teal/coral, set explicitly (an inherited colour inside the tile's link
    # renders as the browser's link blue in Chrome once a container query restyles the arrow)
    assert ".tm .mv, .tm .mv-arrow { color: var(--ink-strong); }" in css


def test_the_diverging_scale_is_grey_in_the_middle_and_symmetric(tokens):
    lch = {b: _lch(tokens[f"--dv-{b}"]) for b in BINS}
    assert lch["n"][1] < 0.02                                                  # grey midpoint, never a hue
    for arm in ("u", "d"):
        steps = [lch["n"][0]] + [lch[f"{arm}{i}"][0] for i in (1, 2, 3)]
        assert all(b - a >= 0.045 for a, b in itertools.pairwise(steps)), (arm, steps)   # monotone, visible steps
        # within an arm, the validator's ordinal step: adjacent OKLCH L at least 0.06 apart
        assert all(b - a >= 0.06 for a, b in itertools.pairwise(steps[1:])), (arm, steps)
        chroma = [lch["n"][1]] + [lch[f"{arm}{i}"][1] for i in (1, 2, 3)]
        assert chroma == sorted(chroma), arm
    for i in (1, 2, 3):                                                        # equal lightness per step on both arms
        assert abs(lch[f"u{i}"][0] - lch[f"d{i}"][0]) <= 0.005, i
    # the arms are teal and coral: the hues of --up and --down
    def hue(h: str) -> float:
        _, a, b = _oklab(_lin(h))
        return math.degrees(math.atan2(b, a)) % 360
    assert abs(hue(tokens["--dv-u3"]) - hue(tokens["--up"])) < 12
    assert abs(hue(tokens["--dv-d3"]) - hue(tokens["--down"])) < 12
    # neighbouring bins stay apart for full-colour readers
    for a, b in itertools.pairwise(BINS):
        assert _delta_e(tokens[f"--dv-{a}"], tokens[f"--dv-{b}"]) >= 5.0, (a, b)


def test_the_asset_class_palette_passes_the_dataviz_checks(tokens):
    """The data-viz validator's computable checks (dark mode, on --card), all pairs, since any two
    class boxes can touch: OKLCH L 0.48-0.67, chroma >= 0.10, CVD dE >= 8 (protan, deutan), normal
    vision dE >= 15, contrast >= 3:1. The hues also keep away from the seats and from teal/coral."""
    marks = {c: tokens[f"--ac-{c}"] for c in CLASSES}
    for c, h in marks.items():
        lightness, chroma = _lch(h)
        assert 0.48 <= lightness <= 0.67 and chroma >= 0.10, (c, lightness, chroma)
        assert _contrast(h, tokens["--card"]) >= 3.0, c
    for a, b in itertools.combinations(CLASSES, 2):
        assert min(_delta_e(marks[a], marks[b], k) for k in ("protan", "deutan")) >= 8.0, (a, b)
        assert _delta_e(marks[a], marks[b]) >= 15.0, (a, b)
    seats = [v for k, v in tokens.items() if k.startswith("--seat-") and k != "--seat-none"]
    for c, h in marks.items():
        assert min(_delta_e(h, s) for s in seats) >= 6.0, c
        assert min(_delta_e(h, tokens[k]) for k in ("--long", "--short")) >= 10.0, c
    # a tile's class fill is the same hue as its mark, darker (the frame and the squares wear the mark)
    for c in CLASSES:
        fill = tokens[f"--ac-{c}-fill"]
        assert _lch(fill)[0] < _lch(marks[c])[0]
        _, a1, b1 = _oklab(_lin(fill))
        _, a2, b2 = _oklab(_lin(marks[c]))
        assert abs((math.degrees(math.atan2(b1, a1) - math.atan2(b2, a2)) + 180) % 360 - 180) < 6, c


def test_the_page_has_no_sideways_scroll_hooks():
    """Phone: the scale is capped to its container and the options wrap."""
    css = CSS.read_text()
    assert ".cbk-scale { display: flex; width: min(100%, 392px);" in css
    assert ".cb-head { display: flex; flex-wrap: wrap;" in css and ".cb-opts { display: flex; flex-wrap: wrap;" in css
    assert ".cbk-row { display: flex; flex-wrap: wrap;" in css and ".cbk-classes { display: flex; flex-wrap: wrap;" in css


def test_the_class_order_is_the_list_filters_order(site):
    assert [k for k, *_ in site.FILTER_GROUPS] == list(CLASSES)
