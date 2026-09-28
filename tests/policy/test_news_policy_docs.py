"""T3b's policy, invariants and documents stay in step (transparency-v2 §3, §5.4).

- `policy/council.yaml` `news`: a quota for every news source, the public quotas sum to 21, 40 at
  most in all, the broker feed on (the user's decision of 2026-09-26), and a CHANGELOG policy entry.
- `invariants`: the broker feed ceiling is on and licensed copies live at most 7 days.
- `docs/data-rights.md`: a row per public-domain source with its attribution and the SHA-256 of its
  archived licence page, and the broker feed row's rules (never its text, 7-day private copies).
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

from council import invariants
from council.data.gov_news import SOURCES, attribution
from council.models.facts import NewsSource
from council.operator.purge import retention_days

ROOT = Path(__file__).resolve().parents[2]
DATA_RIGHTS = (ROOT / "docs" / "data-rights.md").read_text()
MANIFEST = json.loads((ROOT / "tests" / "fixtures" / "news" / "licences" / "manifest.json").read_text())["pages"]
PUBLIC = ("sec", "fed_board", "bls", "bea", "treasury", "eia")


def test_every_news_source_has_a_quota(policy):
    news = policy.council["news"]
    keys = {"broker_feed" if s == "etoro_feed" else s for s in typing.get_args(NewsSource)}
    assert set(news["quotas"]) == keys
    assert sum(news["quotas"][s] for s in PUBLIC) == 21
    assert news["max_items"] == 40 and news["lookback_h"] == 48
    assert news["broker_feed"] is True


def test_the_invariants_hold_the_feed_decision():
    assert invariants.BROKER_FEED_ENABLED is True         # flipping it is a CHANGELOG policy entry
    assert invariants.LICENSED_RETENTION_DAYS == 7 and retention_days() == 7


def test_the_changelog_records_the_news_policy_change():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    # Found by content: later docs-only entries may sit above it.
    entries = [e for e in changelog.split("\n## ")[1:] if "`policy/council.yaml` gains `news:`" in e]
    assert len(entries) == 1
    assert "policy change" in entries[0].splitlines()[0].lower()


def test_data_rights_has_a_row_per_public_source():
    rows = [line for line in DATA_RIGHTS.splitlines() if line.startswith("| ")]
    for key in PUBLIC:
        sha = MANIFEST[key]["sha256"]
        expected = attribution(key).replace("(undated)", "(<release date>)") if key == "eia" else attribution(key)
        matching = [r for r in rows if sha in r]
        assert len(matching) == 1, key
        assert expected.split(" (")[0] in matching[0], key
        assert SOURCES[key].licence in ("public_domain", "federal_work_unverified")
    treasury = next(r for r in rows if MANIFEST["treasury"]["sha256"] in r)
    assert "no summary" in treasury and "federal_work_unverified" in treasury
    sec = next(r for r in rows if MANIFEST["sec"]["sha256"] in r)
    assert "Never filing text" in sec


def test_data_rights_states_the_broker_feed_rules():
    row = next(line for line in DATA_RIGHTS.splitlines() if line.startswith("| Broker news feed"))
    for phrase in ("BROKER_FEED_ENABLED", "news.broker_feed: false", "Never the feed's text",
                   "install key", "7 days", "council purge-licensed"):
        assert phrase in row, phrase
    assert "U.S. federal public-domain material" in DATA_RIGHTS
