"""The install key's first use is safe under concurrency: every caller gets the same complete key."""

from __future__ import annotations

import stat
import threading
from pathlib import Path

from council import paths
from council.publish import install_key


def test_concurrent_first_uses_agree_and_never_read_a_partial_key():
    root: Path = paths.state_dir() / "install-key-race"
    got: list[bytes] = []
    errors: list[BaseException] = []
    start = threading.Barrier(12)

    def first_use() -> None:
        start.wait()
        try:
            got.append(install_key.load_or_create(root))
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=first_use) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert len(got) == 12 and len(set(got)) == 1 and len(got[0]) == install_key.INSTALL_KEY_BYTES
    path = install_key.key_path(root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == [install_key.KEY_FILE]   # no temp file left
    assert install_key.load(root) == got[0]
