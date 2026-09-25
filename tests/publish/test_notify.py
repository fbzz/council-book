from __future__ import annotations

from datetime import UTC, datetime

import pytest

from council.operator.notify import ApprovalWindow, Notifier, NotifyRefused

WINDOW = {"tz": "Europe/Lisbon", "start": "08:00", "end": "23:00"}
DAY = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)       # 13:00 Lisbon
NIGHT = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)      # 03:00 Lisbon


class Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, channel, message, topic):
        self.sent.append((channel, message, topic))


def _notifier(now, recorder, topic="secret-topic"):
    return Notifier(topic, window=WINDOW, sender=recorder, clock=lambda: now)


def test_sends_to_ntfy_and_macos_inside_the_window():
    rec = Recorder()
    result = _notifier(DAY, rec).send("14:40Z proposal", "3 legs, gross 1.42->1.65, cost 0.18%", click_url="https://example.org/c")
    assert result.sent == ("ntfy", "macos") and not result.suppressed
    assert rec.sent[0][2] == "secret-topic" and rec.sent[0][1].priority == "default"


def test_default_priority_is_suppressed_outside_the_window():
    rec = Recorder()
    result = _notifier(NIGHT, rec).send("proposal", "2 legs, cost 0.10%")
    assert result.suppressed and rec.sent == []


def test_urgent_goes_out_at_night():
    rec = Recorder()
    result = _notifier(NIGHT, rec).send("HALTED", "NAV at 0.75 of peak: flatten proposal ready", priority="urgent")
    assert not result.suppressed and [c for c, _, _ in rec.sent] == ["ntfy", "macos"]


@pytest.mark.parametrize("body", ["equity $1,234.56", "USD 1000 at risk", "position 2951234567",
                                  "see /Users/someone/x", "token gho_abcdef", "a@b.com"])
def test_leaky_messages_are_refused_even_when_urgent(body):
    rec = Recorder()
    with pytest.raises(NotifyRefused):
        _notifier(DAY, rec).send("alert", body, priority="urgent")
    assert rec.sent == []


def test_canary_refused():
    rec = Recorder()
    n = Notifier("t", window=WINDOW, sender=rec, clock=lambda: DAY, canaries=(1234.56,))
    with pytest.raises(NotifyRefused):
        n.send("nav", "nav 1234.56")


def test_click_url_must_be_https():
    with pytest.raises(NotifyRefused):
        _notifier(DAY, Recorder()).send("x", "y", click_url="http://example.org")


def test_unknown_priority_rejected():
    with pytest.raises(ValueError):
        _notifier(DAY, Recorder()).send("x", "y", priority="high")


def test_no_topic_means_macos_only():
    rec = Recorder()
    assert _notifier(DAY, rec, topic=None).send("x", "cost 0.1%").sent == ("macos",)


def test_window_from_policy_and_midnight_crossing(policy):
    w = ApprovalWindow.from_policy(policy.risk["approval"]["window"])
    assert w.contains(DAY) and not w.contains(NIGHT)
    night_shift = ApprovalWindow.from_policy({"tz": "UTC", "start": "22:00", "end": "06:00"})
    assert night_shift.contains(NIGHT) and not night_shift.contains(DAY)
