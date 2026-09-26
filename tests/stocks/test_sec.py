"""council.stocks.sec: pacing (<= 7 requests a second, retries included), the user agent (required,
never logged), the gzip cache (TTL, forced refresh), 404 handling and the trims that must leave the
frozen rule's output unchanged. No network: a fake EDGAR behind httpx.MockTransport."""

from __future__ import annotations

import copy
import gzip
import json
import logging
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from council.data import credentials
from council.data.cache import FileCache, request_key
from council.data.credentials import MissingCredential
from council.data.http import DataError
from council.paths import REPO_ROOT, state_dir
from council.stocks import pit, sec
from council.stocks.sec import (
    COMPANYFACTS_URL,
    NS_COMPANYFACTS,
    SEC_MAX_RPS,
    TRIM_PLACEHOLDER,
    SecClient,
    SecError,
    TokenBucket,
    parse_company_tickers,
    recent_filings,
    trim_companyfacts,
    trim_submissions,
)
from tests.stocks.conftest import UA, FakeClock, FakeEdgar, submissions_doc

FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_trimmed.json").read_text())


def _client(edgar: FakeEdgar, clock: FakeClock | None = None, **kw) -> SecClient:
    clock = clock or FakeClock()
    return SecClient(UA, transport=edgar.transport(), clock=clock, sleep=clock.sleep, **kw)


# ------------------------------------------------------------------------------------ pacing


def _max_in_window(times: list[float], width: float = 1.0) -> int:
    return max(sum(1 for u in times if t <= u < t + width - 1e-9) for t in times)


def test_token_bucket_allows_at_most_seven_requests_in_any_second():
    clock = FakeClock()
    bucket = TokenBucket(SEC_MAX_RPS, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(60):
        bucket.acquire()
        times.append(clock.t)
    assert _max_in_window(times) == 7
    assert times[-1] == pytest.approx(59 / 7)
    assert all(b - a >= 1 / 7 - 1e-9 for a, b in zip(times, times[1:], strict=False))


def test_token_bucket_has_no_burst_after_idling():
    clock = FakeClock()
    bucket = TokenBucket(7.0, clock=clock, sleep=clock.sleep)
    bucket.acquire()
    clock.t += 30.0                                   # a long idle period banks no extra tokens
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == pytest.approx(1 / 7)


@pytest.mark.parametrize("rate", [7.5, 10.0, 0.0, -1.0])
def test_a_rate_outside_the_fair_access_ceiling_is_refused(rate):
    assert SEC_MAX_RPS == 7.0
    with pytest.raises(ValueError):
        SecClient(UA, rate=rate, transport=FakeEdgar().transport())


def test_every_attempt_is_paced_including_retries():
    edgar = FakeEdgar(submissions={320193: submissions_doc(320193, "3571")},
                      fail={"/submissions/CIK0000320193.json": [503, 429]})
    clock = FakeClock()
    with _client(edgar, clock) as client:
        client.submissions(320193)
        assert client.requests == 3 and len(edgar.seen) == 3
    assert clock.sleeps == pytest.approx([1 / 7, 1 / 7])     # the two retries waited for a token


# ------------------------------------------------------------------------------------ user agent


def test_missing_user_agent_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("COUNCIL_SEC_USER_AGENT", raising=False)
    edgar = FakeEdgar()
    with pytest.raises(MissingCredential, match="council-book.sec-user-agent"):
        SecClient(transport=edgar.transport())
    monkeypatch.setenv("COUNCIL_MODE", "live")
    monkeypatch.setattr(credentials, "secret", lambda *a, **k: None)      # Keychain item absent
    with pytest.raises(MissingCredential):
        SecClient(transport=edgar.transport())
    assert edgar.seen == []


@pytest.mark.parametrize("bad", ["", "   ", "nocontact", "Name without-at-sign", "Name a@b\nX-Evil: 1",
                                 "x" * 190 + " a@b.org" + "y" * 10])
def test_a_malformed_user_agent_is_refused_without_echoing_it(monkeypatch, bad):
    monkeypatch.setenv("COUNCIL_SEC_USER_AGENT", bad)
    with pytest.raises(MissingCredential) as err:
        SecClient(transport=FakeEdgar().transport())
    assert not bad.strip() or bad.strip() not in str(err.value)


def test_user_agent_comes_from_the_env_override_and_is_sent_but_never_logged(monkeypatch, caplog):
    secret_ua = "Operator Name operator-contact@example.net"
    monkeypatch.setenv("COUNCIL_SEC_USER_AGENT", secret_ua)
    edgar = FakeEdgar(fail={"/submissions/CIK0000000042.json": [403]})
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    client = SecClient(transport=edgar.transport(), clock=clock, sleep=clock.sleep)
    with pytest.raises(DataError) as err:
        client.submissions(42)
    client.close()
    assert edgar.seen[0].headers["user-agent"] == secret_ua
    for text in (str(err.value), repr(client), caplog.text):
        assert secret_ua not in text and "operator-contact" not in text


# ------------------------------------------------------------------------------------ cache


def _facts(cik: int, label: str = "HAS") -> dict:
    doc = copy.deepcopy(FIXTURE["companies"][label]["companyfacts"])
    doc["cik"] = cik
    return doc


def test_companyfacts_is_cached_as_gzip_with_a_ttl_and_a_forced_refresh():
    edgar = FakeEdgar(companyfacts={46080: _facts(46080)})
    with _client(edgar) as client:
        first = client.companyfacts(46080)
        assert client.companyfacts(46080) == first
        assert edgar.paths().count("/api/xbrl/companyfacts/CIK0000046080.json") == 1      # served by the cache
        client.companyfacts(46080, refresh=True)
        assert edgar.paths().count("/api/xbrl/companyfacts/CIK0000046080.json") == 2      # forced refresh
        cache = FileCache(NS_COMPANYFACTS)
        key = request_key({"url": COMPANYFACTS_URL.format(cik=46080)})
        path = cache.directory / f"{key}.json.gz"
        assert path.exists() and state_dir() in path.parents
        assert json.loads(gzip.decompress(path.read_bytes()))["value"]["found"] is True
        stale = datetime.now(UTC) - timedelta(seconds=sec.TTL_COMPANYFACTS_S + 60)
        cache.put(key, {"found": True, "doc": first}, fmt="json.gz", now=stale)
        client.companyfacts(46080)
        assert edgar.paths().count("/api/xbrl/companyfacts/CIK0000046080.json") == 3      # expired entry


def test_a_filer_without_xbrl_facts_is_none_not_an_error():
    edgar = FakeEdgar()
    with _client(edgar) as client:
        assert client.companyfacts(99) is None
        assert client.companyfacts(99) is None
        assert len(edgar.seen) == 1                     # the 404 answer is cached too
        with pytest.raises(DataError, match="HTTP 404"):
            client.submissions(99)                       # submissions must exist


def test_company_tickers_parse_and_cache():
    edgar = FakeEdgar(tickers=[{"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"},
                               {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}])
    with _client(edgar) as client:
        rows = client.company_tickers()
        client.company_tickers()
        assert len(edgar.seen) == 1
    assert rows[0] == sec.TickerRow(1067983, "BRK-B", "Berkshire")


def test_parse_company_tickers_skips_bad_rows_and_refuses_empty_payloads():
    rows = parse_company_tickers([{"cik_str": "320193", "ticker": " AAPL ", "title": "Apple"},
                                  {"cik_str": None, "ticker": "X"}, {"cik_str": 5, "ticker": ""}, "junk"])
    assert rows == [sec.TickerRow(320193, "AAPL", "Apple")]
    for bad in ({}, [], "text", {"0": {"ticker": "A"}}):
        with pytest.raises(SecError):
            parse_company_tickers(bad)


# ------------------------------------------------------------------------------------ trims


def _padded(doc: dict) -> dict:
    """The fixture document with the noise a real companyfacts carries: labels, frames, concepts and
    taxonomies the rule never reads, a share count."""
    out = copy.deepcopy(doc)
    gaap = out["facts"]["us-gaap"]
    for node in gaap.values():
        node["label"], node["description"] = "Label", "Long description"
        for row in node["units"]["USD"]:
            row["frame"] = "CY2016Q1"
    gaap["AccountsPayableCurrent"] = {"label": "AP", "units": {"USD": [
        {"end": "2016-03-31", "val": 1.0, "accn": "a", "fy": 2016, "fp": "Q1", "form": "10-Q", "filed": "2016-05-01"}]}}
    out["facts"]["srt"] = {"Something": {"units": {"USD": []}}}
    out["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2016-04-15", "val": 125e6, "accn": "a", "fy": 2016, "fp": "Q1", "form": "10-Q", "filed": "2016-05-01"}]}}}
    return out


@pytest.mark.parametrize("label", ["HAS", "MSFT", "PM"])
def test_trimming_leaves_the_frozen_rule_output_unchanged(label):
    full = _padded(FIXTURE["companies"][label]["companyfacts"])
    trimmed = trim_companyfacts(full)
    assert "srt" not in trimmed["facts"] and "AccountsPayableCurrent" not in trimmed["facts"]["us-gaap"]
    assert all("label" not in n for n in trimmed["facts"]["us-gaap"].values())
    a, stats_a = pit.fundamentals_comparable("1", "1", full)
    b, stats_b = pit.fundamentals_comparable("1", "1", trimmed)
    pd.testing.assert_frame_equal(a, b)
    assert stats_a == stats_b
    asof = pd.Timestamp(FIXTURE["companies"][label]["filed_cutoff"]) + pd.Timedelta(days=1)
    fa = pit.comparable_features(a.assign(ticker="X"), "X", asof)
    fb = pit.comparable_features(b.assign(ticker="X"), "X", asof)
    assert {k: str(v) for k, v in fa.items()} == {k: str(v) for k, v in fb.items()}


def test_trimming_keeps_the_taxonomy_the_rule_would_detect():
    only_other = {"facts": {"us-gaap": {"AccountsPayableCurrent": {"units": {"USD": []}}},
                            "ifrs-full": {"Revenue": {"units": {"USD": []}}}}}
    empty_node = {"facts": {"us-gaap": {}}}
    for doc in (only_other, empty_node, {"facts": {}}):
        assert pit.detect_taxonomy(trim_companyfacts(doc)) == pit.detect_taxonomy(doc)
    assert TRIM_PLACEHOLDER in trim_companyfacts(only_other)["facts"]["us-gaap"]
    with pytest.raises(SecError):
        trim_companyfacts({"facts": ["not", "a", "mapping"]})


def test_submissions_trim_keeps_the_header_and_recent_filing_columns():
    doc = submissions_doc(320193, "3571", forms=("8-K", "10-Q", "10-K"))
    t = trim_submissions(doc)
    assert t["sic"] == "3571" and "addresses" not in t and "description" not in t
    assert set(t["filings"]["recent"]) == set(sec.RECENT_KEYS)
    rows = recent_filings(t)
    assert [r["form"] for r in rows] == ["8-K", "10-Q", "10-K"]
    assert rows[0]["acceptanceDateTime"] == "2026-08-01T16:05:00.000Z"
