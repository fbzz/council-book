"""TokenBucket: at most 18 writes in any 60 s window; Retry-After pauses every sender."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from council.broker.fake import FakeClock
from council.execution.ratelimit import TokenBucket


def _bucket(clock: FakeClock, **kw) -> TokenBucket:
    return TokenBucket(clock=clock.monotonic, sleep=clock.sleep, **kw)


def test_defaults_are_18_per_60s():
    bucket = TokenBucket()
    assert (bucket.capacity, bucket.per_seconds) == (18, 60.0)


def test_eighteen_immediate_then_the_19th_waits_for_the_window():
    clock = FakeClock()
    bucket = _bucket(clock)
    for _ in range(18):
        assert bucket.acquire() == 0.0                 # pass: 18 within capacity
    assert not bucket.try_acquire()                    # fail: 19th refused without waiting
    waited = bucket.acquire()
    assert waited == pytest.approx(60.0)
    assert clock.slept == [pytest.approx(60.0)]


def test_spaced_sends_never_wait():
    clock = FakeClock()
    bucket = _bucket(clock)
    for _ in range(50):
        assert bucket.acquire() == 0.0
        clock.advance(60 / 18 + 0.01)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=10.0), min_size=1, max_size=80))
def test_no_window_ever_holds_more_than_capacity(gaps):
    clock = FakeClock()
    bucket = _bucket(clock)
    sent: list[float] = []
    for gap in gaps:
        clock.advance(gap)
        bucket.acquire()
        sent.append(clock.monotonic())
    for i, start in enumerate(sent):
        in_window = [t for t in sent[i:] if t - start < 60.0]
        assert len(in_window) <= 18


def test_pause_holds_every_sender_for_retry_after():
    clock = FakeClock()
    bucket = _bucket(clock)
    bucket.pause(7.0)
    assert bucket.acquire() == pytest.approx(7.0)
    assert bucket.acquire() == 0.0


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0)
    with pytest.raises(ValueError):
        TokenBucket(per_seconds=0)
