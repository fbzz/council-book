"""FileCache `json.gz` entries (large SEC documents) and the forced refresh the quarterly rank uses."""

from __future__ import annotations

import gzip
import json
from datetime import timedelta

import pytest

from council.data.cache import FileCache, request_key
from tests.data.synth import utc

T0 = utc(2026, 11, 20, 14, 0)


def test_gzip_json_round_trips_with_a_ttl():
    cache = FileCache("sec-companyfacts")
    doc = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"val": 1.5, "filed": "2026-08-01"}]}}}}}
    path = cache.put("k", doc, fmt="json.gz", now=T0)
    assert path.name == "k.json.gz"
    assert json.loads(gzip.decompress(path.read_bytes()))["value"] == doc      # really gzip-compressed JSON
    assert cache.get("k", 3600, fmt="json.gz", now=T0 + timedelta(seconds=3600)) == doc
    assert cache.get("k", 3600, fmt="json.gz", now=T0 + timedelta(seconds=3601)) is None
    assert cache.get("k", 3600, now=T0) is None                                # the plain-json lookup misses


def test_gzip_bytes_are_deterministic():
    a = FileCache("a").put("k", {"x": 1}, fmt="json.gz", now=T0).read_bytes()
    b = FileCache("b").put("k", {"x": 1}, fmt="json.gz", now=T0).read_bytes()
    assert a == b


@pytest.mark.parametrize("garbage", [b"not gzip", gzip.compress(b"{not json"), gzip.compress(b"[]"), b""])
def test_corrupt_gzip_entries_are_misses(garbage):
    cache = FileCache("sec-submissions")
    cache.put("k", {"ok": True}, fmt="json.gz", now=T0)
    (cache.directory / "k.json.gz").write_bytes(garbage)
    assert cache.get("k", 60, fmt="json.gz", now=T0) is None


def test_forced_refresh_fetches_even_when_the_entry_is_fresh():
    cache = FileCache("sec-companyfacts")
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    assert cache.get_or_fetch({"cik": 1}, 3600, fetch, fmt="json.gz", now=T0) == {"n": 1}
    assert cache.get_or_fetch({"cik": 1}, 3600, fetch, fmt="json.gz", now=T0) == {"n": 1}
    assert cache.get_or_fetch({"cik": 1}, 3600, fetch, fmt="json.gz", now=T0, refresh=True) == {"n": 2}
    assert cache.get(request_key({"cik": 1}), 3600, fmt="json.gz", now=T0) == {"n": 2}   # overwritten
    assert len(calls) == 2


def test_unknown_format_is_refused():
    with pytest.raises(ValueError):
        FileCache("x").get("k", 60, fmt="yaml")  # type: ignore[arg-type]
