"""A watch alert the notifier refuses still arrives: scrubbed, else as a fixed text.

The watch's "positions without a stop-loss" alert names each position's symbol; a position on an
instrument no line owns is keyed `UNMAPPED_<id>`, which the notifier's leak scan refuses. Before,
the refusal was suppressed and the URGENT alert was silently lost.
"""

from __future__ import annotations

from types import SimpleNamespace

from council import watch
from council.operator.notify import Notifier, NotifyRefused


class _Recorder(Notifier):
    def __init__(self) -> None:
        super().__init__(ntfy_topic=None, sender=lambda channel, message, topic: None, macos=False)
        self.sent: list[tuple[str, str]] = []

    def send(self, title, body, priority="default", click_url=None):  # type: ignore[override]
        self.check(title, body, click_url)          # the real leak-scan refusal
        self.sent.append((body, priority))
        return None


def test_an_unmapped_key_is_scrubbed_and_the_urgent_alert_arrives():
    notifier = _Recorder()
    ctx = SimpleNamespace(notifier=notifier)
    watch._alert(ctx, "URGENT positions without a stop-loss: SMH.L, UNMAPPED_1234567")
    assert notifier.sent == [("positions without a stop-loss: SMH.L, an unmapped instrument", "urgent")]


def test_a_text_that_cannot_be_scrubbed_falls_back_to_the_fixed_text():
    class Refuses(_Recorder):
        def send(self, title, body, priority="default", click_url=None):  # type: ignore[override]
            if body != watch.ALERT_WITHHELD:
                raise NotifyRefused("notification refused (canary)")
            return super().send(title, body, priority, click_url)

    notifier = Refuses()
    watch._alert(SimpleNamespace(notifier=notifier), "URGENT stop-loss hit on SMH")
    assert notifier.sent == [(watch.ALERT_WITHHELD, "urgent")]


def test_a_clean_alert_is_sent_as_written_and_failures_never_raise():
    notifier = _Recorder()
    watch._alert(SimpleNamespace(notifier=notifier), "WARN drawdown below 80% of the lifetime peak: no new risk")
    assert notifier.sent == [("WARN drawdown below 80% of the lifetime peak: no new risk", "default")]

    class Broken:
        def send(self, *a, **k):
            raise RuntimeError("down")

    watch._alert(SimpleNamespace(notifier=Broken()), "URGENT stop-loss hit on SMH")   # no exception
    watch._alert(SimpleNamespace(notifier=None), "URGENT stop-loss hit on SMH")
