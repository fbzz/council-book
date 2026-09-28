"""The broker writer for the onboarding rehearsal's simulated operator terminal (M5-G).

Only `operator/` and `execution/` may import `broker.etoro_write` (tests/boundaries/
test_writer_imports.py), so the rehearsal harness builds its writer here. It refuses outside a
marked rehearsal sandbox and accepts only the pinned loopback URL (`check_base_url`), so this path
can never reach eToro. The tokens come from the caller (the sandbox reads them through the
production `keychain.read_secret` on the fake `security`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class RehearsalWriterError(RuntimeError):
    """Refused: not a marked rehearsal sandbox."""


def sandbox_write_client(api_key: str, token: str, *, base_url: str, state_dir: Path) -> Any:
    from council.broker.http import check_base_url
    from council.operator.release import is_marked_sandbox

    if not is_marked_sandbox(state_dir):
        raise RehearsalWriterError("the rehearsal writer runs only inside a marked rehearsal sandbox")
    url = check_base_url(base_url, state_dir)
    if not url.startswith("http://127.0.0.1:"):
        raise RehearsalWriterError("the rehearsal writer talks only to the loopback fake broker")
    from council.broker.etoro_write import EtoroWriteClient

    return EtoroWriteClient(api_key, token, base_url=url)
