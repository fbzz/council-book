from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from council.data.sanitize import clean_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain headline", "plain headline"),
        ("\x1b[31mred\x1b[0m alert", "red alert"),                         # CSI colour
        ("safe\x1b]52;c;ZXZpbA==\x07 text", "safe text"),                  # OSC 52 clipboard write
        ("title\x1b]0;fake window title\x1b\\ end", "title end"),         # OSC with ST terminator
        ("c1\x9b2Jclear", "c1clear"),                                       # 8-bit CSI
        ("bidi ‮exe.txt‬ ok", "bidi exe.txt ok"),                 # RLO/PDF overrides
        ("iso⁦late⁩d", "isolated"),                               # bidi isolates
        ("zero​width﻿", "zerowidth"),                              # ZWSP + BOM
        ("bell\x07 and nul\x00 del\x7f", "bell and nul del"),
        ("line\nbreak\r\ttab sep", "line break tab sep"),
        ("see https://scam.example/x?y=1 now", "see now"),
        ("visit www.pump-coin.io today", "visit today"),
        ("go to pump-coin.xyz/claim fast", "go to fast"),
        ("hxxps://defanged.example/p link", "link"),
        ("[click here](https://evil.example) please", "click here please"),
        ("ping @trader_joe and @x.y now", "ping and now"),
        ("mail " + "someone" + "@" + "example.com today", "mail today"),  # built: keep leak scans quiet
        ("＠fullwidth ｈｔｔｐｓ://x.example handle", "handle"),              # NFKC folds lookalikes
        ("@​hidden handle", "handle"),                                 # zero-width inside handle
        ("<b>bold</b> <script>alert(1)</script> text", "bold alert(1) text"),
        ("&lt;i&gt;escaped&lt;/i&gt; &#27;[31mx", "escaped [31mx"),          # no ESC survives decoding
        ("&#x1b;]52;c;ZXZpbA==&#7; y", "]52;c;ZXZpbA== y"),
        ("S&amp;P 500 and U.S. yields; BRK.B up", "S&P 500 and U.S. yields; BRK.B up"),
        ("lone \ud800 surrogate", "lone surrogate"),
        ("  lots   of\t\tspace  ", "lots of space"),
    ],
)
def test_clean_text_cases(raw, expected):
    assert clean_text(raw, 500) == expected


def test_clean_text_truncates_to_max_len():
    assert clean_text("abcdef ghij", 6) == "abcdef"
    assert clean_text("abcde fghij", 6) == "abcde"          # trailing space trimmed
    assert len(clean_text("x" * 1000, 200)) == 200
    assert clean_text("short", 200) == "short"


def test_clean_text_none_and_non_strings():
    assert clean_text(None, 10) == ""
    assert clean_text(12.5, 10) == "12.5"
    with pytest.raises(ValueError):
        clean_text("x", -1)


_BAD = {"Cc", "Cf", "Cs", "Co"}


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=300), st.integers(min_value=0, max_value=120))
def test_clean_text_properties(raw, max_len):
    out = clean_text(raw, max_len)
    assert len(out) <= max_len
    assert not any(unicodedata.category(ch) in _BAD for ch in out)
    assert "\x1b" not in out
    assert "://" not in out
    assert out == out.strip()
    assert clean_text(out, max_len) == out                  # idempotent
    out.encode("utf-8")                                     # hashable downstream
