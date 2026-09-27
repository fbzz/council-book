"""council.models.facts news items: the two id kinds (`P:` public, keyed-hash `N:` broker), the source
and licence fields, the typed link (redteam §5.3 #6) and serialisation that leaves every broker item,
and the pack hash over it, exactly as it was before public-domain items existed."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from council.facts.evidence_ids import news_id
from council.models.facts import (
    DEFAULT_LICENCE,
    NEWS_LINK_HOSTS,
    PUBLIC_NEWS_SOURCES,
    Fact,
    FactPack,
    NewsItem,
    broker_news_id,
    check_https_link,
    public_news_id,
)

AT = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
OLD_KEYS = {"id", "title", "summary", "symbols", "published_at", "available_at", "source", "earnings_date",
            "before_market_open"}


def broker(**kw) -> NewsItem:
    return NewsItem(**{"id": "N:1a2b3c4d", "title": "Chip export limits", "published_at": AT, "available_at": AT,
                       **kw})


def public(**kw) -> NewsItem:
    return NewsItem(**{"id": public_news_id("fed_board", "guid-1"), "title": "Board issues statement",
                       "published_at": AT, "available_at": AT, "source": "fed_board", **kw})


# ------------------------------------------------------------------------------------ ids


@pytest.mark.parametrize("good", ["N:1a2b3c4d", "P:00000000", "P:abcdef12"])
def test_both_id_kinds_are_accepted(good):
    assert broker(id=good).id == good


@pytest.mark.parametrize("bad", ["X:1a2b3c4d", "P:1a2b3c4", "P:1A2B3C4D", "N:1a2b3c4d5", "P:", "p:1a2b3c4d",
                                 "P:1a2b3c4d\n"])
def test_other_ids_are_refused(bad):
    with pytest.raises(ValidationError):
        broker(id=bad)


def test_public_ids_are_a_plain_hash_of_source_and_key():
    assert public_news_id("sec", "0001045810-26-000077") == "P:" + hashlib.sha256(
        b"sec\x000001045810-26-000077").hexdigest()[:8]
    assert public_news_id("bls", "k") != public_news_id("bea", "k")
    assert public_news_id("bls", "k") == public_news_id("bls", "k")
    with pytest.raises(ValueError):
        public_news_id("etoro_feed", "k")
    with pytest.raises(ValueError):
        public_news_id("bls", "")


def test_broker_ids_are_keyed_hashes():
    key_a, key_b = b"a" * 32, b"b" * 32
    assert broker_news_id("post-1", key_a) == broker_news_id("post-1", key_a)
    assert broker_news_id("post-1", key_a) != broker_news_id("post-1", key_b)
    assert broker_news_id("post-1", key_a) != broker_news_id("post-2", key_a)
    assert broker_news_id("post-1", key_a) != news_id("post-1")            # not the old unkeyed hash
    assert broker_news_id("post-1", key_a) == "N:" + hmac.new(key_a, b"post-1", hashlib.sha256).hexdigest()[:8]
    assert broker(id=broker_news_id("post-1", key_a)).id.startswith("N:")
    with pytest.raises(ValueError):
        broker_news_id("post-1", b"short")
    with pytest.raises(ValueError):
        broker_news_id("", key_a)


def test_facts_learn_the_public_prefix():
    fact = Fact(id="P:1a2b3c4d", kind="news", value=None, unit="text", available_at=AT, source="bls")
    assert fact.id == "P:1a2b3c4d"


# ------------------------------------------------------------------------------------ source and licence


def test_the_licence_defaults_from_the_source():
    assert broker().source == "etoro_feed" and broker().licence == "broker_licensed"
    assert public().licence == "public_domain"
    assert public(source="treasury").licence == "federal_work_unverified"
    assert set(DEFAULT_LICENCE) == PUBLIC_NEWS_SOURCES | {"etoro_feed"}


def test_an_explicit_licence_is_kept_even_when_it_does_not_match():
    # publication decides on prefix + source + licence together, so a mismatch must be constructible
    odd = public(source="etoro_feed", licence="broker_licensed")
    assert (odd.id[:2], odd.source, odd.licence) == ("P:", "etoro_feed", "broker_licensed")


@pytest.mark.parametrize("field", [{"source": "reuters"}, {"licence": "cc_by"}, {"form": "10-K"},
                                   {"items": ["2.2"]}, {"items": ["Item 2.02"]}])
def test_unknown_sources_licences_forms_and_item_codes_are_refused(field):
    with pytest.raises(ValidationError):
        public(**field)


# ------------------------------------------------------------------------------------ serialisation


def test_a_broker_item_serialises_exactly_as_before():
    item = broker(summary="s", symbols=["NDX"])
    dumped = item.model_dump(mode="json")
    assert set(dumped) == OLD_KEYS
    assert NewsItem.model_validate(dumped) == item
    assert set(json.loads(item.model_dump_json())) == OLD_KEYS


def test_the_pack_hash_over_broker_news_is_unchanged():
    item = broker(summary="s", symbols=["NDX"])
    pack = FactPack(cycle_id="c", slot=AT, created_at=AT, admitted=["NDX"], states={}, news=[item])
    legacy = pack.model_dump(mode="json", exclude={"input_hash", "created_at"})
    legacy["news"] = [{k: v for k, v in n.items() if k in OLD_KEYS} for n in legacy["news"]]
    blob = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    assert pack.compute_hash() == hashlib.sha256(blob).hexdigest()


def test_a_public_item_round_trips_with_its_new_fields():
    item = public(source="sec", id=public_news_id("sec", "acc"), form="8-K/A", items=["5.02", "9.01"],
                  link="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=TSTA&type=8-K%2FA",
                  symbols=["TSTA"])
    dumped = item.model_dump(mode="json")
    assert dumped["licence"] == "public_domain" and dumped["form"] == "8-K/A" and dumped["items"] == ["5.02", "9.01"]
    assert NewsItem.model_validate(dumped) == item
    bare = public().model_dump(mode="json")
    assert "link" not in bare and "form" not in bare and "items" not in bare and bare["licence"] == "public_domain"


# ------------------------------------------------------------------------------------ links (§5.3 #6)


@pytest.mark.parametrize("link", [
    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260917a.htm",
    "https://www.bls.gov/news.release/archives/empsit_09052025.htm",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=NVDA&type=8-K",
    "https://www.treasurydirect.gov/instit/annceresult/press/preanre/2026/A_20260917_2.pdf",
    "https://www.eia.gov/todayinenergy/detail.php?id=68204",
])
def test_allowed_links(link):
    assert check_https_link(link) == link
    assert public(link=link).link == link


@pytest.mark.parametrize("link", [
    "http://www.federalreserve.gov/newsevents/pressreleases/monetary20260917a.htm",
    "https://evil.example/monetary.htm",
    "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000077/x-index.htm",
    "https://www.bls.gov:8443/x.htm",
    "https://user@www.bls.gov/x.htm",
    "https://www.bls.gov/a b.htm",
    "https://www.bls.gov/x.htm#frag",
    "https://www.newyorkfed.org/x.htm",
    "https://www.bls.gov/" + "a" * 300,
    "javascript:alert(1)",
    "",
])
def test_refused_links(link):
    with pytest.raises(ValueError):
        check_https_link(link)
    with pytest.raises(ValidationError):
        public(link=link)


def test_the_link_hosts_are_the_federal_sources_only():
    expected = {"www.sec.gov", "www.federalreserve.gov", "www.bls.gov", "www.bea.gov", "apps.bea.gov",
                "home.treasury.gov", "www.treasurydirect.gov", "www.eia.gov"}
    assert expected == NEWS_LINK_HOSTS
