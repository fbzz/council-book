"""Sanitization of LLM strings and feed text."""

from __future__ import annotations

import pytest

from council.llm.sanitize import sanitize_obj, sanitize_text, scrub_amounts

AT = chr(64)
DOLLAR = chr(36)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain text", "plain text"),
        ("\x1b[31mred\x1b[0m", "red"),                        # ANSI sequence removed whole
        ("bell\x07 nul\x00 del\x7f c1\x9b", "bell nul del c1"),  # C0, DEL, C1 removed
        ("line\nbreak\ttab", "line break tab"),
        ("left‮right⁦x⁩", "leftrightx"),         # bidi override/isolates
        ("zero​width﻿", "zerowidth"),
        ("see https://evil.example/path?q=1 now", "see now"),
        ("visit www.example.com today", "visit today"),
        ("docs at example.com/x here", "docs at here"),
        (f"ping {AT}trader please", "ping please"),
        (f"mail someone{AT}example.org now", "mail now"),
    ],
)
def test_sanitize_text(raw, clean):
    assert sanitize_text(raw) == clean


def test_evidence_ids_survive_sanitization():
    for eid in ("F:NDX:dist_sma50", "M:DGS10@2026-09-30", "N:1a2b3c4d", "S:0001-26#p3", "K:vol:1"):
        assert sanitize_text(eid) == eid


def test_sanitize_never_lengthens_and_is_idempotent():
    raw = f"\x1b[1m{AT}a https://x.example/y‮ z\n\n  w"
    once = sanitize_text(raw)
    assert len(once) <= len(raw)
    assert sanitize_text(once) == once


def test_sanitize_obj_walks_keys_and_values():
    obj = {"k‮": ["a\x1bb", {"x": "https://e.example"}], "n": 1, "b": True, "z": None}
    assert sanitize_obj(obj) == {"k": ["ab", {"x": ""}], "n": 1, "b": True, "z": None}


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (f"raised {DOLLAR}5bn today", "raised [amount] today"),
        (f"costs {DOLLAR}1,250.50 each", "costs [amount] each"),
        (f"US{DOLLAR} 40 million", "[amount]"),
        ("up 5% on the day", "up 5% on the day"),
        (f"{DOLLAR}NDX ticker", f"{DOLLAR}NDX ticker"),
    ],
)
def test_scrub_amounts(raw, clean):
    assert scrub_amounts(raw) == clean
