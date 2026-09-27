"""`council keys store <name>`: the operator stores one non-broker Keychain item (m5-readiness §9.1).

Rules:
- Only the allow-listed items below; broker items (`council-book.etoro.*`) are never reachable here
  (they have their own release-pinned commands).
- The value is typed at a no-echo prompt, validated per item, and handed to `security -i` on stdin,
  never on a command line (visible to `ps`), never printed, logged or put in an exception message.
- Items go to the login keychain, or, under a marked rehearsal sandbox, only to the sandbox keychain
  file the CLI passes in (`cli._key_store_target`).
"""

from __future__ import annotations

import getpass
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from council.operator.keychain import DEFAULT_ACCOUNT, SECURITY, KeychainError, Runner
from council.settings import valid_healthcheck_url, valid_ntfy_topic

_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,512}$")
_UA_FORBIDDEN = set("'\"\\`")


def _token(value: str) -> bool:
    return bool(_TOKEN.match(value))


def _user_agent(value: str) -> bool:
    """One printable ASCII line of 8-200 characters with a name and a contact e-mail (SEC and the
    government sites refuse anonymous clients). No quote or backslash, so the `security -i` line
    needs nothing beyond plain quoting."""
    return (8 <= len(value) <= 200 and all(32 <= ord(ch) < 127 for ch in value)
            and not (_UA_FORBIDDEN & set(value)) and " " in value and "@" in value)


@dataclass(frozen=True)
class Item:
    name: str
    service: str
    valid: Callable[[str], bool]
    rule: str                         # shown on refusal (never the value)


ITEMS: dict[str, Item] = {item.name: item for item in (
    Item("tiingo", "council-book.tiingo", _token, "one token of 8-512 characters"),
    Item("fred", "council-book.fred", _token, "one key of 8-512 characters"),
    Item("alpaca-key-id", "council-book.alpaca-key-id", _token, "one key of 8-512 characters"),
    Item("alpaca-secret", "council-book.alpaca-secret", _token, "one key of 8-512 characters"),
    Item("sec-user-agent", "council-book.sec-user-agent", _user_agent,
         "one printable line with a name and a contact e-mail, no quotes"),
    Item("gov-user-agent", "council-book.gov-user-agent", _user_agent,
         "one printable line with a name and a contact e-mail, no quotes"),
    Item("ntfy-topic", "council-book.ntfy-topic", valid_ntfy_topic,
         "12-64 letters, digits, '-' or '_' (e.g. council-<16 hex>)"),
    Item("healthcheck-url", "council-book.healthcheck-url", valid_healthcheck_url,
         "an https:// ping URL"),
    Item("soak-probe", "council-book.soak-probe", _token, "one random value of 8-512 characters"),
)}
# short names and pairs the runbook uses
ALIASES: dict[str, tuple[str, ...]] = {
    "ntfy": ("ntfy-topic",),
    "healthcheck": ("healthcheck-url",),
    "alpaca": ("alpaca-key-id", "alpaca-secret"),
}


def names() -> list[str]:
    return sorted(ITEMS) + sorted(ALIASES)


def resolve(name: str) -> tuple[Item, ...]:
    """The items `name` stores, or KeychainError naming the allow-list."""
    key = name.strip().lower()
    if key in ITEMS:
        return (ITEMS[key],)
    if key in ALIASES:
        return tuple(ITEMS[k] for k in ALIASES[key])
    raise KeychainError(f"not an allow-listed item: choose one of {', '.join(names())}")


def store_item(
    item: Item,
    keychain: Path | str | None,
    *,
    account: str = DEFAULT_ACCOUNT,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    runner: Runner = subprocess.run,
) -> None:
    """Prompt without echo, validate, store with `security -i` (value on stdin only)."""
    value = getpass_fn(f"Enter the value for {item.service} (input hidden): ").strip()
    if not item.valid(value):
        del value
        raise KeychainError(f"value rejected for {item.service}: expected {item.rule}")
    parts = ["add-generic-password", "-U", "-s", item.service, "-a", account, "-w", value]
    if keychain is not None:
        parts.append(str(keychain))
    command = " ".join(shlex.quote(p) for p in parts) + "\n"
    result = runner([SECURITY, "-i"], input=command, capture_output=True, text=True, check=False)
    del value, command
    if result.returncode != 0 or "error" in (result.stderr or "").lower():
        raise KeychainError(f"storing {item.service!r} failed")
