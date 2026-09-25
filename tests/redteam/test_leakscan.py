"""Red-team fixtures: things that MUST be caught, and ordinary public text that must NOT be."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from council.publish import leakscan
from council.publish.redact import clean_text

FIXTURES = Path(__file__).parent / "fixtures"
LICENSED = (FIXTURES / "licensed.txt").read_text()
CANARY_NAV = 1234.56


# ------------------------------------------------------------------------------ must be caught
@pytest.mark.parametrize("text, rule", [
    ("fees were $123.45", "dollar_amount"),
    ("fees were $ 5", "dollar_amount"),
    ("a deposit of USD 1000", "currency_amount"),
    ("paid 1,234.50 EUR", "currency_suffix"),
    ("paid €20", "euro_pound_amount"),
    ("see /Users/x/y", "local_path"),
    ("id 5b0f2c4e-9a1d-4c3b-8e7f-0123456789ab", "uuid"),
    ("token gho_xxx", "github_token"),
    ("pat github_pat_11ABC", "github_pat"),
    ("key sk-abcdefgh12345678", "api_secret"),
    ("position 2951234567", "long_number"),
    ('"positionID":2951234567', "long_number"),
    ("gcid 12345678", "long_number"),
    ("whitelist 203.0.113.7", "ipv4"),
    ("mail someone@example.com", "email"),
    ("jwt eyJhbGciOi.eyJzdWIiOi.sig", "jwt"),
    ("Authorization: Bearer abcdefgh12345678", "bearer"),
    ("send x-user-key with it", "broker_header"),
    ("https://claude.ai/code/artifact/abc", "private_artifact"),
    ("skipped UNMAPPED_100123: no line", "unmapped_id"),
    ('{"UNMAPPED_42": 0.01}', "unmapped_id"),
])
def test_value_leaks_are_caught(text, rule):
    rules = {f.rule for f in leakscan.scan(text)}
    assert rule in rules, rules


@pytest.mark.parametrize("fmt", ["1234.56", "1,234.56", "1234", "NAV 1234.6"])
def test_canary_nav_is_caught_in_several_formats(fmt):
    assert {f.rule for f in leakscan.scan(f"the book is {fmt} today", canaries=[CANARY_NAV])} == {"canary"}


def test_canary_in_structured_values():
    assert [f.rule for f in leakscan.scan({"nav_x": 1234.56}, canaries=[CANARY_NAV])] == ["canary"]
    assert [f.rule for f in leakscan.scan({"v": 1234}, canaries=["1234.56"])] == ["canary"]


def test_canary_does_not_match_inside_other_numbers():
    assert leakscan.scan("0.1234 and 11234.567 and 51234", canaries=[CANARY_NAV]) == []


def test_text_canary():
    assert [f.rule for f in leakscan.scan("built by Someuser", canaries=["someuser"])] == ["canary"]
    with pytest.raises(ValueError):
        leakscan.canary_patterns("abc")


def test_eight_word_ngram_of_licensed_text_is_caught():
    copied = "Analysts said the largest contract foundry raised its full year revenue guidance."
    assert leakscan.ngram_overlap(copied, [LICENSED])
    findings = leakscan.scan(copied, licensed_texts=[LICENSED])
    assert [f.rule for f in findings].count("licensed_text") >= 1


def test_seven_words_are_not_an_ngram_match():
    assert leakscan.ngram_overlap("the largest contract foundry raised its full", [LICENSED]) == []


@pytest.mark.parametrize("key", ["amount", "amount_usd", "equity", "balance", "cash", "unrealizedPnL", "price",
                                 "open_rate", "units", "notional", "margin", "positionID", "position_id", "orderId",
                                 "request_id", "gcid", "cid", "mirrorID", "token", "accountCurrencyId", "ip",
                                 "ipsWhitelist", "sl_rate"])
def test_denied_keys(key):
    assert leakscan.key_denied(key)


@pytest.mark.parametrize("key", ["margin_use_pct", "cash_x", "cost_bp", "weight_before_x", "tokens_in",
                                 "tokens_out", "carry_bp_day", "prompt_sha", "skipped", "replicate", "generated",
                                 "description", "EURUSD", "GBPUSD", "stop_distance_pct", "late_by_min", "id"])
def test_allowed_keys(key):
    assert not leakscan.key_denied(key)


def test_findings_never_echo_the_secret():
    findings = leakscan.scan("position 2951234567 and $123.45")
    assert findings and all("2951234567" not in str(f) and "123.45" not in str(f) for f in findings)


# ------------------------------------------------------------------------------ must NOT be caught
@pytest.mark.parametrize("text", [
    "risk-increasing legs wait for the commitment; the desk-brief and task-list are code.",
    "EURUSD 0.25x and GBPUSD -0.10x; gross 1.42->1.65, cost 0.18%, valid to 18:35Z",
    "Cycle 2026-10-01T1440Z sealed at 2026-10-01T15:00:00Z",
    "evidence N:12345678 and S:0001234567-26-000123#p1 and K:news:12",
    "sha 1234567abcdef0123456789abcdef0123456789abcdef0123456789abcdef01",
    "a Bearer token is never stored here; USD-denominated lines are reported in x",
    "vol ratio 2.3, 50/200-day trend, 16 years of data, 0.1234567 rounding",
    "instrument universe of 123456 names and 999999 bars",      # 6 digits: below the id threshold
    "UNMAPPED: 1 position outside the universe (unmapped_symbols_dropped:1)",
])
def test_ordinary_public_text_is_clean(text):
    assert leakscan.scan(text) == [], leakscan.scan(text)


# ------------------------------------------------------------------------------ CLI
def test_cli_flags_leaky_fixtures(capsys):
    assert leakscan.main([str(FIXTURES / "leaky_cycle.json"), str(FIXTURES / "leaky_notes.md")]) == 1
    out = capsys.readouterr().out
    for rule in ("long_number", "denied_key", "dollar_amount", "local_path", "uuid", "github_token", "currency_amount"):
        assert rule in out, rule
    assert "2951234567" not in out and "/Users/x/y" not in out


def test_cli_passes_clean_fixture_and_skips_missing(capsys, tmp_path):
    assert leakscan.main([str(FIXTURES / "clean_cycle.json"), str(tmp_path / "missing")]) == 0


def test_cli_canary_from_env(monkeypatch, tmp_path):
    f = tmp_path / "status.json"
    f.write_text('{"note": "1,234.56"}')
    monkeypatch.setenv("COUNCIL_LEAK_CANARIES", "1234.56;somebody")
    assert leakscan.main([str(f)]) == 1
    assert leakscan.main([str(f), "--canary", "ab"]) == 2


def test_cli_scans_directories_and_skips_binaries(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ok.txt").write_text("fine")
    (tmp_path / "a" / "img.png").write_bytes(b"$1 \x00\xff")
    assert leakscan.main([str(tmp_path)]) == 0
    (tmp_path / "a" / "bad.jsonl").write_text('{"x": 1}\n{"amount": 3}\n')
    assert leakscan.main([str(tmp_path)]) == 1


# ------------------------------------------------------------------------------ untrusted text
@given(st.text(max_size=300))
def test_clean_text_output_has_no_control_or_bidi_characters(raw):
    out = clean_text(raw, 200)
    assert len(out) <= 200
    assert not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069
                   for c in out)


@given(st.from_regex(r"\$\s?\d{1,6}(\.\d{2})?", fullmatch=True))
def test_clean_text_never_lets_a_dollar_amount_through(amount):
    assert not [f for f in leakscan.scan(clean_text(f"cost {amount} today")) if f.rule == "dollar_amount"]


@given(st.integers(min_value=1, max_value=10**12))
def test_clean_text_never_lets_an_unmapped_instrument_id_through(instrument_id):
    out = clean_text(f"skipped UNMAPPED_{instrument_id}: no line")
    assert str(instrument_id) not in out and leakscan.scan(out) == []
