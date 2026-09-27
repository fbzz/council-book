"""OPT-IN live canary for the public-domain news sources (Federal Reserve Board, BLS, BEA, Treasury,
EIA, SEC current filings). Never in CI (marker `live`) and skipped unless COUNCIL_LIVE_CANARY=1. The
SEC/BLS contact user agent comes from the Keychain item `council-book.sec-user-agent` (the canary
runs in dry_run mode so credentials may read it); it is never printed:

    COUNCIL_LIVE_CANARY=1 uv run pytest -m live tests/data/test_gov_news_live_canary.py

Re-recording the parser fixtures and the dated licence archives (tests/fixtures/news/) is a second
opt-in on top of the first; it rewrites the files and `licences/manifest.json`:

    COUNCIL_LIVE_CANARY=1 COUNCIL_RECORD_NEWS_FIXTURES=1 uv run pytest -m live \
        tests/data/test_gov_news_live_canary.py -k record
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from council.data import gov_news
from council.data.credentials import MissingCredential, sec_user_agent
from council.paths import REPO_ROOT
from council.policy import Policy
from council.stocks import sec_news
from council.stocks.sec import SecClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("COUNCIL_LIVE_CANARY") != "1", reason="opt-in: set COUNCIL_LIVE_CANARY=1"),
]

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "news"
# Licence pages are archived without their script, style and noscript elements: the statement is page
# text, and the scripts carry per-request tokens and third-party keys (the served bytes differ on every
# request, so only the archived file's hash is reproducible; the served hash is recorded alongside).
_SCRIPTS = re.compile(rb"(?is)<(script|style|noscript)\b.*?</\1\s*>")
ARCHIVE_TRANSFORM = "script, style and noscript elements removed"
SEC_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
               "&owner=include&start=0&count=100&output=atom")


@pytest.fixture
def contact_ua(monkeypatch) -> str:
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")         # lets credentials read the Keychain item
    try:
        return sec_user_agent()
    except MissingCredential:
        pytest.skip("no SEC contact user agent configured")


def test_live_public_news_canary(contact_ua):
    now = datetime.now(UTC)
    fetch = gov_news.gather_public_news(Policy.load(include_sleeve=False), now)
    for key in gov_news.GOV_SOURCES:
        report = fetch.sources[key]
        assert report.error is None and report.feeds_ok == len(gov_news.SOURCES[key].feeds), (key, report.error)
    for item in fetch.items:
        assert item.id.startswith("P:") and item.available_at < now
        assert not gov_news.trips_leak_scan(item.title, item.summary)


def test_live_sec_current_filings_canary(contact_ua):
    with SecClient() as client:
        for form in sec_news.CURRENT_FORMS:
            filings = sec_news.fetch_current(client, form)
            assert filings and {f.form for f in filings} <= sec_news.FORMS
            assert all(f.accepted_at.tzinfo is not None and f.cik > 0 for f in filings)


def _get(client: httpx.Client, url: str, agent: str) -> httpx.Response:
    try:
        return client.get(url, headers=gov_news.request_headers(agent))
    except httpx.HTTPError as exc:                       # recorded as status 0; the others go on
        return httpx.Response(0, text=type(exc).__name__)


@pytest.mark.skipif(os.environ.get("COUNCIL_RECORD_NEWS_FIXTURES") != "1",
                    reason="opt-in: set COUNCIL_RECORD_NEWS_FIXTURES=1 to re-record fixtures")
def test_record_news_fixtures(contact_ua):
    out = Path(os.environ.get("COUNCIL_NEWS_FIXTURE_DIR") or FIXTURES)
    (out / "feeds").mkdir(parents=True, exist_ok=True)
    (out / "licences").mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest: dict[str, dict[str, object]] = {}
    status: dict[str, int] = {}
    with httpx.Client(timeout=gov_news.FETCH_TIMEOUT, follow_redirects=True) as client:
        for key, spec in gov_news.SOURCES.items():
            agent = contact_ua if spec.contact_ua else gov_news.GENERIC_USER_AGENT
            for feed in spec.feeds:
                response = _get(client, feed.url, agent)
                status[f"{key}/{feed.label}"] = response.status_code
                if response.status_code == 200:
                    (out / "feeds" / f"{key}-{feed.label}.xml").write_bytes(response.content)
            response = _get(client, spec.licence_url, agent)
            status[f"{key}/licence"] = response.status_code
            if response.status_code == 200:
                archived = _SCRIPTS.sub(b"", response.content)
                (out / "licences" / spec.licence_archive).write_bytes(archived)
                manifest[key] = {
                    "file": spec.licence_archive, "url": spec.licence_url, "final_url": str(response.url),
                    "retrieved": retrieved, "licence": spec.licence,
                    "sha256": hashlib.sha256(archived).hexdigest(), "bytes": len(archived),
                    "served_sha256": hashlib.sha256(response.content).hexdigest(),
                    "served_bytes": len(response.content), "transform": ARCHIVE_TRANSFORM,
                }
        for form in ("8-K", "6-K"):
            response = _get(client, SEC_CURRENT.format(form=form), contact_ua)
            status[f"sec/current-{form}"] = response.status_code
            if response.status_code == 200:
                (out / "feeds" / f"sec-current-{form}.xml").write_bytes(response.content)
    (out / "licences" / "manifest.json").write_text(json.dumps(
        {"checked": retrieved[:10], "pages": dict(sorted(manifest.items()))}, indent=2, sort_keys=True) + "\n")
    print(json.dumps(status, indent=1, sort_keys=True))
    assert status, "nothing was requested"
