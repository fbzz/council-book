"""Operator notifications: ntfy (phone) and a macOS notification. Percentages only.

Rules:
- Every title, body and click URL is leak-scanned; a message containing a money amount, an id,
  a path, an e-mail or a token is REFUSED (NotifyRefused), never trimmed and sent.
- Outside the approval window (risk.approval.window, local time) only `urgent` messages go out.
- Priority is "default" (proposals) or "urgent" (kill switch, stop hit, execution unknown, ...).
- The ntfy topic is private configuration; the confirmation nonce is never sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from council.clock import utcnow
from council.publish import leakscan

Priority = Literal["default", "urgent"]
Channel = Literal["ntfy", "macos"]
NTFY_BASE = "https://ntfy.sh"


class NotifyRefused(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: Priority
    click_url: str | None = None


@dataclass(frozen=True)
class NotifyResult:
    sent: tuple[Channel, ...]
    suppressed: bool
    reason: str = ""


@dataclass(frozen=True)
class ApprovalWindow:
    tz: ZoneInfo
    start: time
    end: time

    @classmethod
    def from_policy(cls, window: Mapping[str, Any]) -> ApprovalWindow:
        return cls(
            tz=ZoneInfo(str(window["tz"])),
            start=time.fromisoformat(str(window["start"])),
            end=time.fromisoformat(str(window["end"])),
        )

    def contains(self, ts: datetime) -> bool:
        local = ts.astimezone(self.tz).time()
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end    # window crossing midnight


Sender = Callable[[Channel, Message, str | None], None]


def _latin1(text: str) -> str:
    """HTTP header values must be latin-1; keep them readable."""
    text = text.replace("→", "->").replace("–", "-").replace("—", "-").replace("×", "x")
    return text.encode("latin-1", "replace").decode("latin-1")


def default_sender(channel: Channel, message: Message, topic: str | None) -> None:
    """Real delivery. Not used by tests (they inject a sender)."""
    if channel == "ntfy":
        import httpx

        headers = {"Title": _latin1(message.title), "Priority": message.priority}
        if message.click_url:
            headers["Click"] = message.click_url
        httpx.post(f"{NTFY_BASE}/{topic}", content=message.body.encode(), headers=headers, timeout=10.0)
    else:
        import subprocess

        script = ['-e', 'on run argv', '-e', 'display notification (item 2 of argv) with title (item 1 of argv)', '-e', 'end run']
        subprocess.run(["/usr/bin/osascript", *script, message.title, message.body],
                       check=False, capture_output=True, timeout=10)


class Notifier:
    def __init__(
        self,
        ntfy_topic: str | None,
        *,
        window: ApprovalWindow | Mapping[str, Any] | None = None,
        sender: Sender | None = None,
        clock: Callable[[], datetime] = utcnow,
        macos: bool = True,
        canaries: tuple[str | float | int, ...] = (),
    ):
        if window is None:
            from council.policy import default_policy

            window = default_policy().risk["approval"]["window"]
        self.window = window if isinstance(window, ApprovalWindow) else ApprovalWindow.from_policy(window)
        self.topic = ntfy_topic
        self.sender = sender or default_sender
        self.clock = clock
        self.macos = macos
        self.canaries = canaries

    def check(self, title: str, body: str, click_url: str | None) -> None:
        """Refuse anything the public leak scan would refuse (money amounts included)."""
        findings = leakscan.scan({"title": title, "body": body, "click": click_url or ""}, canaries=self.canaries)
        if findings:
            rules = sorted({f.rule for f in findings})
            raise NotifyRefused(f"notification refused ({', '.join(rules)})")
        if click_url and not click_url.startswith("https://"):
            raise NotifyRefused("click URL must be https")

    def send(
        self,
        title: str,
        body: str,
        priority: Priority = "default",
        click_url: str | None = None,
    ) -> NotifyResult:
        if priority not in ("default", "urgent"):
            raise ValueError(f"unknown priority {priority!r}")
        title, body = title.strip()[:120], body.strip()[:1000]
        self.check(title, body, click_url)
        if priority != "urgent" and not self.window.contains(self.clock()):
            return NotifyResult(sent=(), suppressed=True, reason="outside the approval window")
        message = Message(title=title, body=body, priority=priority, click_url=click_url)
        sent: list[Channel] = []
        if self.topic:
            self.sender("ntfy", message, self.topic)
            sent.append("ntfy")
        if self.macos:
            self.sender("macos", message, None)
            sent.append("macos")
        return NotifyResult(sent=tuple(sent), suppressed=False)
