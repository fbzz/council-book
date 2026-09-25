from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from council.data.cache import FileCache, request_key
from council.paths import state_dir
from tests.data.synth import utc

T0 = utc(2026, 10, 1, 14, 40)


def test_key_is_stable_and_order_independent():
    assert request_key({"a": 1, "b": [1, 2]}) == request_key({"b": [1, 2], "a": 1})
    assert request_key({"a": 1}) != request_key({"a": 2})
    assert len(request_key("x")) == 64


def test_json_ttl_pass_and_fail():
    cache = FileCache("history")
    cache.put("k", {"v": [1.5, 2]}, now=T0)
    assert cache.get("k", 3600, now=T0 + timedelta(seconds=3600)) == {"v": [1.5, 2]}
    assert cache.get("k", 3600, now=T0 + timedelta(seconds=3601)) is None     # expired
    assert cache.get("k", 3600, now=T0 - timedelta(seconds=1)) is None        # future-dated
    assert cache.directory == state_dir() / "cache" / "history"


def test_pickle_format_round_trips_frames():
    cache = FileCache("frames")
    frame = pd.DataFrame({"x": [1.0, 2.0]}, index=pd.DatetimeIndex(["2026-10-01", "2026-10-02"], tz="UTC"))
    cache.put("f", frame, fmt="pickle", now=T0)
    pd.testing.assert_frame_equal(cache.get("f", 60, fmt="pickle", now=T0), frame)
    assert cache.get("f", 60, now=T0) is None                     # json lookup misses pickle entry


def test_corrupt_or_missing_entries_are_misses():
    cache = FileCache("history")
    assert cache.get("absent", 60, now=T0) is None
    cache.put("k", 1, now=T0)
    (cache.directory / "k.json").write_text("{not json")
    assert cache.get("k", 60, now=T0) is None


def test_get_or_fetch_fetches_once_within_ttl():
    cache = FileCache("history")
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    assert cache.get_or_fetch({"q": 1}, 60, fetch, now=T0) == {"n": 1}
    assert cache.get_or_fetch({"q": 1}, 60, fetch, now=T0 + timedelta(seconds=30)) == {"n": 1}
    assert cache.get_or_fetch({"q": 1}, 60, fetch, now=T0 + timedelta(seconds=61)) == {"n": 2}
    assert len(calls) == 2


def test_bad_namespace_and_naive_now_rejected():
    with pytest.raises(ValueError):
        FileCache("../escape")
    with pytest.raises(ValueError):
        FileCache("history").put("k", 1, now=T0.replace(tzinfo=None))
