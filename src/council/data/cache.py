"""TTL file cache under state_dir()/cache/<namespace>/, keyed by a SHA-256 of the request.

Rules:
- The cache lives in the private state dir, never inside the repository.
- Entries record their own `stored_at`; an entry older than the TTL (or unreadable) is a miss.
- Writes are atomic (temp file + rename), so a crashed cycle never leaves a half-written entry.
- JSON is the default format; pickle is available for local objects and is only ever read from
  the private cache directory this process wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from council.paths import assert_outside_repo, state_dir

Format = Literal["json", "pickle"]
T = TypeVar("T")
_SAFE_NAMESPACE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def request_key(request: Any) -> str:
    """Stable key for any JSON-able request description (dict key order does not matter)."""
    blob = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("naive datetime")
    return now.astimezone(UTC)


class FileCache:
    """One namespace of cached responses (e.g. `history`, `macro`)."""

    def __init__(self, namespace: str, *, root: Path | None = None) -> None:
        if not namespace or set(namespace) - _SAFE_NAMESPACE:
            raise ValueError(f"bad cache namespace {namespace!r}")
        self.namespace = namespace
        self._root = root

    @property
    def directory(self) -> Path:
        return (self._root or state_dir() / "cache") / self.namespace

    def _path(self, key: str, fmt: Format) -> Path:
        return self.directory / f"{key}.{'json' if fmt == 'json' else 'pkl'}"

    def get(self, key: str, ttl_s: float, *, fmt: Format = "json", now: datetime | None = None) -> Any:
        """The cached value, or None on a miss (absent, expired, corrupt or future-dated)."""
        path = self._path(key, fmt)
        try:
            if fmt == "json":
                entry = json.loads(path.read_text())
            else:
                with path.open("rb") as fh:
                    entry = pickle.load(fh)  # private cache written by this process
            stored_at = datetime.fromisoformat(entry["stored_at"])
            value = entry["value"]
        except (OSError, ValueError, KeyError, TypeError, EOFError, pickle.UnpicklingError):
            return None
        age = (_now(now) - stored_at).total_seconds()
        if age < 0 or age > ttl_s:
            return None
        return value

    def put(self, key: str, value: Any, *, fmt: Format = "json", now: datetime | None = None) -> Path:
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        assert_outside_repo(directory)
        entry = {"stored_at": _now(now).isoformat(), "value": value}
        path = self._path(key, fmt)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                if fmt == "json":
                    fh.write(json.dumps(entry, separators=(",", ":")).encode())
                else:
                    pickle.dump(entry, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def get_or_fetch(
        self,
        request: Any,
        ttl_s: float,
        fetch: Callable[[], T],
        *,
        fmt: Format = "json",
        now: datetime | None = None,
    ) -> T:
        """Return the cached value for `request`, or call `fetch`, store and return its result."""
        key = request_key(request)
        cached = self.get(key, ttl_s, fmt=fmt, now=now)
        if cached is not None:
            return cached
        value = fetch()
        self.put(key, value, fmt=fmt, now=now)
        return value
