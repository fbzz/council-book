"""The install key: 32 random bytes that key every public hash of a value the public does not know.

Rules (transparency-v2 T-D5, T-D15):
- One key per installation, at `state_dir()/keys/install.key` (file 0600, folder 0700), created on
  first use from `secrets.token_bytes`. It is PRIVATE state like the commit-reveal salts: never
  under the repository, never in a prompt, a log line, an error message or the journal.
- It keys the material-change fingerprint (`redact.short_fingerprint(..., key=...)`) and, once the
  broker feed is wired, the `N:` news ids: an unkeyed hash of a low-entropy private value (a
  rounded fundamentals value, a time-based broker post id) could be brute-forced from the public
  record; an HMAC under this key cannot.
- Rotating the key changes every keyed hash: the fingerprint then reads "changed" once, and `N:`
  ids of the same post differ across the rotation. A malformed key file raises; it is never
  silently replaced (that would be a rotation nobody chose).
- Values are never echoed: errors name the path's role, not its content.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

from council import paths

INSTALL_KEY_BYTES = 32
KEY_DIR = "keys"
KEY_FILE = "install.key"


class InstallKeyError(RuntimeError):
    """The install key file is unusable. The message never contains key material."""


def key_path(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else paths.state_dir()) / KEY_DIR / KEY_FILE


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallKeyError("the install key is not a regular file")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o600)              # tighten, never loosen
        data = os.read(fd, INSTALL_KEY_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) != INSTALL_KEY_BYTES:
        raise InstallKeyError(f"the install key must be {INSTALL_KEY_BYTES} bytes")
    return data


def load(state_dir: Path | None = None) -> bytes:
    """The existing install key; FileNotFoundError when there is none, InstallKeyError when the
    file is unusable."""
    return _read(key_path(state_dir))


def load_or_create(state_dir: Path | None = None) -> bytes:
    """The install key, created (0600, in a 0700 folder) on first use. Refuses a path inside the
    public repository. Two concurrent first uses agree: the loser of the exclusive create reads the
    winner's key."""
    path = key_path(state_dir)
    paths.assert_outside_repo(path)
    try:
        return _read(path)
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    key = secrets.token_bytes(INSTALL_KEY_BYTES)
    # Written whole under a private temporary name, then hard-linked into place: the link either
    # creates the key file complete or fails because another first use won, so a concurrent
    # reader never sees an empty or partial key (an exclusive create of the key file itself would
    # expose it empty until the write lands).
    tmp = path.with_name(f".{KEY_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read(path)
    finally:
        tmp.unlink(missing_ok=True)
    return key


def keyed_hex(key: bytes, data: str | bytes, chars: int = 16) -> str:
    """The first `chars` hex characters of HMAC-SHA256(key, data)."""
    if len(key) < 16:
        raise InstallKeyError("the install key is too short")
    if not 1 <= chars <= 64:
        raise ValueError("chars must be between 1 and 64")
    raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else bytes(data)
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:chars]
