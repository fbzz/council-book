"""The licensed-filter contract (M5-M): the publication gate refuses exactly what the purge scrubs
(one implementation), and redaction withholds nothing the gate would let through as licensed."""
from __future__ import annotations

import pytest

from council.operator import purge
from council.publish import leakscan, redact

FEED = [
    "Chipmaker shares slide after regulators open a broad inquiry into supply contracts overseas",
    "Quarterly Refinery Output Surges Unexpectedly",          # a bare 5-word title
]
CASES = [
    ("a council paraphrase: chip stocks fell on a regulatory probe", False),
    ("Note: regulators open a broad inquiry into supply contracts overseas today", True),   # 8-gram
    ('Headline was "quarterly refinery output surges unexpectedly", we think', True),       # title
    ("quarterly refinery output rose", False),
    ("", False),
]


def test_purge_filter_is_the_leakscan_matcher():
    assert purge.LicensedFilter is leakscan.LicensedMatcher
    assert purge.NGRAM == leakscan.LICENSED_NGRAM == 8


@pytest.mark.parametrize(("text", "licensed"), CASES)
def test_gate_and_purge_agree(text, licensed):
    scrub = purge.LicensedFilter(FEED).hits(text)
    gate = any(f.rule == "licensed_text" for f in leakscan.scan(text, licensed_texts=FEED))
    assert scrub is gate is licensed


@pytest.mark.parametrize(("text", "licensed"), CASES)
def test_redaction_withholds_every_ngram_copy(text, licensed):
    """Redaction (8-gram today) must never let a copied run reach the gate as clean text; bare
    titles are refused by the gate (fail closed) until redaction adopts the matcher (T2)."""
    cleaner = redact._Text(FEED)
    out = cleaner(text, 500)
    if leakscan.ngram_overlap(text, FEED):
        assert out == redact.WITHHELD_LICENSED[:500]
    if out != redact.WITHHELD_LICENSED[:500] and licensed:
        assert any(f.rule == "licensed_text" for f in leakscan.scan(out, licensed_texts=FEED))


def test_findings_never_echo_licensed_text():
    findings = leakscan.scan(CASES[1][0], licensed_texts=FEED)
    assert findings and all("regulators open a broad" not in f.excerpt for f in findings)
