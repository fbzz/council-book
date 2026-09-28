"""`FakeSecurity`: an in-memory stand-in for `/usr/bin/security` (rehearsal L1 and CI).

It models exactly the calls `operator/keychain.py` makes — `create-keychain`,
`set-keychain-settings`, `list-keychains -d user [-s ...]`, `unlock-keychain`, `lock-keychain`,
`find-generic-password ... -w [keychain]` and `-i` with `add-generic-password` on stdin — keyed by
keychain path, with `LOGIN` standing for the login keychain. It records every argv so tests can
assert that no token ever reached a command line, and it never touches a real keychain.

`install(fake)` points every runner default of `operator.keychain` at the fake and returns an undo
function (pytest uses `monkeypatch` through `install_with(monkeypatch, fake)`).
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGIN = "login.keychain-db"


@dataclass
class FakeSecurity:
    login: str = f"/fake-home/Library/Keychains/{LOGIN}"
    items: dict[str, dict[tuple[str, str], str]] = field(default_factory=dict)
    search_list: list[str] = field(default_factory=list)
    unlocked: set[str] = field(default_factory=set)
    argv_log: list[list[str]] = field(default_factory=list)
    fail_reads: bool = False                         # V3: the keychain is locked / unreadable

    def __post_init__(self) -> None:
        self.items.setdefault(self.login, {})
        if not self.search_list:
            self.search_list = [self.login]

    # ------------------------------------------------------------------ helpers
    def _kc(self, name: str | None) -> str:
        return self.login if name is None else str(Path(name).expanduser().resolve()) if "/" in name else name

    def has(self, service: str, keychain: str | Path | None = None) -> bool:
        key = self._kc(None if keychain is None else str(keychain))
        return any(s == service for s, _ in self.items.get(key, {}))

    def services(self, keychain: str | Path | None = None) -> set[str]:
        key = self._kc(None if keychain is None else str(keychain))
        return {s for s, _ in self.items.get(key, {})}

    def argv_text(self) -> str:
        return "\n".join(" ".join(a) for a in self.argv_log)

    @staticmethod
    def _done(cmd: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, code, out, err)

    # ------------------------------------------------------------------ the runner
    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = [str(c) for c in cmd]
        self.argv_log.append(list(cmd))
        args = cmd[1:]
        if args == ["-i"]:
            return self._interactive(cmd, kwargs.get("input") or "")
        verb = args[0] if args else ""
        if verb == "create-keychain":
            path = self._kc(args[-1])
            if path in self.items:
                return self._done(cmd, 48, err="already exists")
            self.items[path] = {}
            self.search_list.append(path)             # like the real one: joins the search list
            self.unlocked.add(path)
            return self._done(cmd)
        if verb == "set-keychain-settings":
            return self._done(cmd)
        if verb == "list-keychains":
            if "-s" in args:
                self.search_list = [self._kc(k) for k in args[args.index("-s") + 1:]]
                return self._done(cmd)
            return self._done(cmd, out="".join(f'    "{k}"\n' for k in self.search_list))
        if verb == "unlock-keychain":
            path = self._kc(args[-1])
            if path not in self.items:
                return self._done(cmd, 45)
            self.unlocked.add(path)
            return self._done(cmd)
        if verb == "lock-keychain":
            self.unlocked.discard(self._kc(args[-1]))
            return self._done(cmd)
        if verb == "delete-keychain":
            path = self._kc(args[-1])
            self.items.pop(path, None)
            self.search_list = [k for k in self.search_list if k != path]
            return self._done(cmd)
        if verb == "find-generic-password":
            return self._find(cmd, args)
        return self._done(cmd, 1, err="unsupported")

    def _find(self, cmd: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        if self.fail_reads:
            return self._done(cmd, 51, err="locked")
        service = args[args.index("-s") + 1]
        account = args[args.index("-a") + 1]
        tail = args[-1] if not args[-1].startswith("-") and args[-2] not in ("-s", "-a") else None
        chains = [self._kc(tail)] if tail is not None else list(self.search_list)
        for chain in chains:
            value = self.items.get(chain, {}).get((service, account))
            if value is not None:
                return self._done(cmd, out=value + "\n")
        return self._done(cmd, 44, err="not found")

    def _interactive(self, cmd: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
        for line in stdin.splitlines():
            parts = shlex.split(line)
            if not parts or parts[0] != "add-generic-password":
                return self._done(cmd, 1, err="error: unsupported")
            service = parts[parts.index("-s") + 1]
            account = parts[parts.index("-a") + 1]
            value = parts[parts.index("-w") + 1]
            tail = parts[-1] if parts[-2] != "-w" else None
            chain = self._kc(tail) if tail is not None else self.login
            if chain not in self.items:
                return self._done(cmd, 1, err="error: no such keychain")
            self.items[chain][(service, account)] = value
        return self._done(cmd)


_RUNNER_FUNCS = ("read_secret", "create_write_keychain", "unlock_write_keychain",
                 "lock_write_keychain", "store_token_interactive")


def install(fake: FakeSecurity, *, getpass_fn: Callable[[str], str] | None = None,
            setitem: Callable[[dict[str, Any], str, Any], None] | None = None) -> Callable[[], None]:
    """Point every `runner=` default of `operator.keychain` at `fake` (and optionally the hidden
    prompt at `getpass_fn`). Returns an undo function. `setitem` = `monkeypatch.setitem` in tests."""
    from council.operator import keychain

    saved: list[tuple[dict[str, Any], str, Any]] = []

    def put(defaults: dict[str, Any], key: str, value: Any) -> None:
        if setitem is not None:
            setitem(defaults, key, value)
        else:
            saved.append((defaults, key, defaults[key]))
            defaults[key] = value

    for name in _RUNNER_FUNCS:
        defaults = getattr(keychain, name).__kwdefaults__
        put(defaults, "runner", fake)
    if getpass_fn is not None:
        put(keychain.store_token_interactive.__kwdefaults__, "getpass_fn", getpass_fn)

    def undo() -> None:
        for defaults, key, value in reversed(saved):
            defaults[key] = value

    return undo
