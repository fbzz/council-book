"""M5-E1 canary: the ntfy topic and the healthcheck URL never reach a log, stdout/stderr, a public
file, a state-dir file or the ledger, across a real (offline) cycle + watch that exercises both the
notifier and the dead-man ping. The test is not vacuous: it asserts the topic and the URL were used."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import time
from zoneinfo import ZoneInfo

import httpx

from council.cycle import run_cycle
from council.operator import healthcheck
from council.operator.notify import ApprovalWindow, Notifier
from council.publish.gitops import Publisher
from council.settings import Settings
from council.watch import run_watch
from tests.integration.test_end_to_end import _ctx

TOPIC = "canarytopic-7f3a91c2e4b5"
URL = "https://hc-ping.example/canary-ping-6d0e"


def test_topic_and_url_never_leave_their_channel(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    sent: list[tuple[str, str | None]] = []
    pinged: list[str] = []

    def sender(channel, message, topic):
        sent.append((channel, topic))

    def transport(request: httpx.Request) -> httpx.Response:
        pinged.append(str(request.url))
        return httpx.Response(200)

    real_ping = healthcheck.ping
    monkeypatch.setattr(healthcheck, "ping", lambda url, **kw: real_ping(
        url, **{**kw, "client": httpx.Client(transport=httpx.MockTransport(transport))}))

    preview = tmp_path / "preview"
    pub = Publisher(tmp_path / "state" / "publisher-clone", push=False, dry_run_dir=preview)
    always = ApprovalWindow(tz=ZoneInfo("UTC"), start=time(0), end=time.max)
    ctx = _ctx(tmp_path, publisher=pub)
    ctx = replace(ctx, settings=Settings(role="runner", mode="live", ntfy_topic=TOPIC, healthcheck_url=URL),
                  notifier=Notifier(TOPIC, window=always, sender=sender, macos=False))

    run_cycle(ctx)
    ctx.notifier.send("council watch", "canary probe", priority="urgent")   # the notifier path itself
    run_watch(ctx)

    assert ("ntfy", TOPIC) in sent                    # the topic was really handed to the sender
    assert any(p.startswith(URL) for p in pinged)     # the URL was really pinged

    out = capsys.readouterr()
    for text in (out.out, out.err, caplog.text, repr(ctx.settings)):
        assert TOPIC not in text and URL not in text
    host = URL.split("/")[2]
    for root in (tmp_path / "state", preview):
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            data = path.read_bytes()
            assert TOPIC.encode() not in data and host.encode() not in data, path.relative_to(tmp_path)
