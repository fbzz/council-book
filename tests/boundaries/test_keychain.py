"""Keychain wrapper. `security` is always mocked: no test reads or writes a real Keychain."""

from __future__ import annotations

import subprocess

import pytest

from council import paths
from council.operator import keychain
from council.operator.keychain import KeychainError

TOKEN = "tok_" + "synthetic" + "-not-a-secret"  # built at runtime; see .gitleaksignore


class FakeSecurity:
    def __init__(self, stdout="", returncode=0, search_list=None):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode
        self.search_list = search_list or []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if cmd[1:3] == ["list-keychains", "-d"] and len(cmd) == 4:
            out = "".join(f'    "{k}"\n' for k in self.search_list)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_read_secret_builds_the_find_command():
    fake = FakeSecurity(stdout=TOKEN + "\n")
    assert keychain.read_secret(keychain.READ_SERVICE, runner=fake) == TOKEN
    cmd = fake.calls[0][0]
    assert cmd == ["/usr/bin/security", "find-generic-password", "-s", "council-book.etoro.read", "-a", "council", "-w"]


def test_missing_item_raises_without_leaking():
    fake = FakeSecurity(stdout=TOKEN, returncode=44)
    with pytest.raises(KeychainError) as err:
        keychain.read_secret(keychain.READ_SERVICE, runner=fake)
    assert TOKEN not in str(err.value)


def test_write_token_needs_the_operator_role():
    fake = FakeSecurity(stdout=TOKEN)
    with pytest.raises(KeychainError, match="operator"):
        keychain.read_secret(keychain.WRITE_SERVICE, runner=fake, env={"COUNCIL_ROLE": "runner"})
    with pytest.raises(KeychainError, match="operator"):
        keychain.read_secret("anything", keychain=keychain.write_keychain_path(), runner=fake, env={})
    assert fake.calls == []
    assert keychain.read_secret(keychain.WRITE_SERVICE, keychain=keychain.write_keychain_path(), runner=fake,
                                env={"COUNCIL_ROLE": "operator"}) == TOKEN
    assert fake.calls[0][0][-1] == str(keychain.write_keychain_path())


def test_write_keychain_lives_in_private_state_outside_the_repo():
    path = keychain.write_keychain_path()
    assert path.parent == paths.state_dir() and path.name == "council-write.keychain-db"
    assert paths.REPO_ROOT not in path.resolve().parents


def test_create_write_keychain_is_interactive_autolocking_and_off_the_search_list():
    login = "/tmp/keychains/login.keychain-db"
    fake = FakeSecurity(search_list=[login, str(keychain.write_keychain_path())])
    path = keychain.create_write_keychain(runner=fake)
    cmds = [c for c, _ in fake.calls]
    assert cmds[0] == ["/usr/bin/security", "create-keychain", str(path)]
    assert cmds[1] == ["/usr/bin/security", "set-keychain-settings", "-l", "-u", "-t", "60", str(path)]
    assert cmds[-1] == ["/usr/bin/security", "list-keychains", "-d", "user", "-s", login]
    assert all("-p" not in c for c in cmds)                  # the password is typed at the prompt


def test_search_list_untouched_when_it_cannot_be_parsed():
    fake = FakeSecurity(search_list=[])
    keychain.create_write_keychain(runner=fake)
    assert not any("-s" in c for c, _ in fake.calls if "list-keychains" in c)


def test_create_refuses_to_overwrite(tmp_path):
    path = keychain.write_keychain_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    with pytest.raises(KeychainError):
        keychain.create_write_keychain(runner=FakeSecurity())


def test_unlock_is_interactive_and_lock_is_explicit():
    path = keychain.write_keychain_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    fake = FakeSecurity()
    keychain.unlock_write_keychain(runner=fake)
    keychain.lock_write_keychain(runner=fake)
    assert [c for c, _ in fake.calls] == [["/usr/bin/security", "unlock-keychain", str(path)],
                                          ["/usr/bin/security", "lock-keychain", str(path)]]
    assert "capture_output" not in fake.calls[0][1]          # the prompt reaches the terminal
    with pytest.raises(KeychainError):
        keychain.unlock_write_keychain(runner=FakeSecurity(returncode=51))


def test_unlock_without_keychain_raises():
    with pytest.raises(KeychainError):
        keychain.unlock_write_keychain(runner=FakeSecurity())


def test_store_token_never_puts_the_secret_on_a_command_line():
    fake = FakeSecurity()
    keychain.store_token_interactive(keychain.WRITE_SERVICE, keychain.write_keychain_path(),
                                     getpass_fn=lambda prompt: TOKEN, runner=fake)
    cmd, kwargs = fake.calls[0]
    assert cmd == ["/usr/bin/security", "-i"]
    assert TOKEN not in " ".join(cmd)
    assert TOKEN in kwargs["input"] and kwargs["capture_output"] is True
    assert "add-generic-password" in kwargs["input"] and "council-book.etoro.write" in kwargs["input"]


@pytest.mark.parametrize("bad", ["", "q7Zx", "has space in it", "quote'inside-token"])
def test_store_token_rejects_malformed_values(bad):
    fake = FakeSecurity()
    with pytest.raises(KeychainError) as err:
        keychain.store_token_interactive("svc", None, getpass_fn=lambda p: bad, runner=fake)
    assert fake.calls == [] and (not bad or bad not in str(err.value))


def test_store_token_failure_does_not_echo_the_value():
    fake = FakeSecurity(returncode=1)
    with pytest.raises(KeychainError) as err:
        keychain.store_token_interactive("svc", None, getpass_fn=lambda p: TOKEN, runner=fake)
    assert TOKEN not in str(err.value)
