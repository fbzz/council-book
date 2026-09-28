"""Opt-in: the rehearsal keychain rule on a REAL throwaway macOS keychain (m5-readiness §7.1, M5-G).

Runs only on macOS and only when selected explicitly: `uv run pytest -m macos_keychain
tests/rehearsal/test_keychain_throwaway.py`. It creates a keychain file under tmp_path with a random
throwaway password, stores a FAKE READ token through the production keychain module inside a marked
sandbox, reads it back, and restores the user keychain search list byte for byte in `finally`. The
login keychain is never written; build agents and CI never select it.
"""

from __future__ import annotations

import secrets
import subprocess
import sys

import pytest

from council.operator import keychain as kc
from council.operator.release import REHEARSAL_MARKER

pytestmark = pytest.mark.macos_keychain
SECURITY = "/usr/bin/security"


@pytest.fixture(autouse=True)
def _opt_in(request):
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    if "macos_keychain" not in (request.config.option.markexpr or ""):
        pytest.skip("opt-in: select with -m macos_keychain")


def _search_list() -> str:
    return subprocess.run([SECURITY, "list-keychains", "-d", "user"], capture_output=True, text=True,
                          check=True).stdout


def _restore(saved: str) -> None:
    entries = [line.strip().strip('"') for line in saved.splitlines() if line.strip()]
    subprocess.run([SECURITY, "list-keychains", "-d", "user", "-s", *entries], check=True)


def test_throwaway_keychain_holds_the_rehearsal_read_item_and_the_search_list_is_restored(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / REHEARSAL_MARKER).write_text("rehearsal sandbox\n")
    throwaway = tmp_path / "rehearsal-throwaway.keychain-db"
    env = {"COUNCIL_STATE_DIR": str(state), "COUNCIL_KEYCHAIN_FILE": str(throwaway), "COUNCIL_ROLE": "dev"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fake_token = "fake-" + secrets.token_hex(8)
    saved = _search_list()
    try:
        subprocess.run([SECURITY, "create-keychain", "-p", secrets.token_hex(12), str(throwaway)], check=True)
        _restore(saved)
        assert str(throwaway) not in _search_list()
        kc.store_token_interactive(kc.READ_SERVICE, None, getpass_fn=lambda _p: fake_token)
        assert kc.read_secret(kc.READ_SERVICE, env=env, ancestors=["-zsh", "login", "Terminal"]) == fake_token
    finally:
        subprocess.run([SECURITY, "delete-keychain", str(throwaway)], capture_output=True, check=False)
        _restore(saved)
    assert _search_list() == saved
