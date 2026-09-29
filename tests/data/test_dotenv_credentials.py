"""Data credentials from the git-ignored .env (user decision 2026-09-29)."""

import os
import subprocess
from pathlib import Path

import pytest

from council.data import alpaca, credentials

ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    os.chmod(path, mode)
    return path


def _no_keychain(*_a, **_k):
    return subprocess.CompletedProcess([], 44, "", "")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("COUNCIL_ENV_FILE", str(path))
    monkeypatch.setenv("COUNCIL_MODE", "dry_run")
    monkeypatch.setattr(credentials.subprocess, "run", _no_keychain)
    return path


def test_dotenv_supplies_data_credentials(env_file):
    kid, sec = "K" + "x" * 15, "S" + "y" * 31
    _write(env_file, f"# data keys\nexport COUNCIL_ALPACA_KEY_ID={kid}\nCOUNCIL_ALPACA_SECRET='{sec}'\n"
                     "COUNCIL_TIINGO_TOKEN=\"t0k3nvalue\"\nCOUNCIL_SEC_USER_AGENT=council-book a@b.org\n")
    keys = alpaca.load_keys(runner=_no_keychain)
    assert keys is not None and keys.key_id == kid and keys.secret == sec
    assert credentials.secret(credentials.TIINGO) == "t0k3nvalue"
    assert credentials.sec_user_agent() == "council-book a@b.org"
    assert kid not in repr(keys)


def test_group_readable_dotenv_is_ignored(env_file):
    _write(env_file, "COUNCIL_TIINGO_TOKEN=abc12345\n", mode=0o644)
    assert credentials.secret(credentials.TIINGO) is None


def test_stub_mode_never_reads_dotenv(env_file, monkeypatch):
    _write(env_file, "COUNCIL_TIINGO_TOKEN=abc12345\n")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    assert credentials.dotenv_secret(credentials.TIINGO) is None


def test_broker_tokens_are_never_read_from_dotenv(env_file):
    _write(env_file, "COUNCIL_ETORO_WRITE_TOKEN=abc12345\nETORO_API=abc12345\n")
    assert all("ETORO" not in n for n in credentials.DOTENV_NAMES.values())
    assert credentials._dotenv_values(env_file) == {}


def test_dotenv_is_git_ignored():
    out = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False)
    assert out.returncode == 0
