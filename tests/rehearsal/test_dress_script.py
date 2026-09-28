"""`ops/rehearse-onboarding.sh` (L2 dress rehearsal, m5-readiness §7.3) run with fakes only.

The council command, `security` and the [REHEARSAL] shell are replaced by small scripts through the
script's REHEARSE_* test hooks, HOME and TMPDIR are temporary: no real keychain, no real state dir,
no network, no launchd. Only local `git` runs (the sandbox's bare remote).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from council import paths

SCRIPT = paths.REPO_ROOT / "ops" / "rehearse-onboarding.sh"

FAKE_COUNCIL = r"""#!/bin/sh
echo "argv=$* state=${COUNCIL_STATE_DIR:-unset} role=${COUNCIL_ROLE:-unset}" >>"$FAKE_LOG"
case "$1 $2" in
  "ops assert-operator") exit "${FAKE_OPERATOR_EXIT:-0}" ;;
  "rehearse fake-broker")
    [ "${FAKE_BROKER_FAILS:-0}" = 1 ] && exit 3
    echo 45678 >"$COUNCIL_STATE_DIR/fake-broker.port"
    printf 'app-key fake-app\nread fake-read\nwrite fake-write\n' >"$COUNCIL_STATE_DIR/rehearsal-tokens.txt"
    trap 'exit 0' TERM
    while :; do sleep 0.1; done ;;
  "ops record-dress") exit 0 ;;
esac
exit 0
"""

FAKE_SECURITY = r"""#!/bin/sh
LIST="$FAKE_DIR/search-list"
[ -f "$LIST" ] || printf '    "%s"\n' "$HOME/Library/Keychains/login.keychain-db" >"$LIST"
echo "security $*" >>"$FAKE_LOG"
case "$1" in
  list-keychains)
    if [ "${4:-}" = "-s" ]; then shift 4; : >"$LIST"; for k in "$@"; do printf '    "%s"\n' "$k" >>"$LIST"; done
    else cat "$LIST"; fi ;;
  create-keychain) : >"$2"; printf '    "%s"\n' "$2" >>"$LIST" ;;
  delete-keychain) rm -f "$2"; grep -v -F "$2" "$LIST" >"$LIST.tmp" || true; mv "$LIST.tmp" "$LIST" ;;
esac
"""

FAKE_SHELL = r"""#!/bin/sh
env | sort >"$HOME/subshell.env"          # env -i: only HOME tells the fake where to write
: >"$COUNCIL_STATE_DIR/council-write.keychain-db"       # as `keys init-write-keychain` would
exit 0
"""


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def rig(tmp_path):
    master, slave = os.openpty()
    try:
        yield _rig(tmp_path, os.ttyname(slave))
    finally:
        os.close(master)
        os.close(slave)


def _rig(tmp_path, tty):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp), "TERM": "dumb",
        "REHEARSE_COUNCIL": str(_exe(fakes / "council", FAKE_COUNCIL)),
        "REHEARSE_SECURITY": str(_exe(fakes / "security", FAKE_SECURITY)),
        "REHEARSE_SHELL": str(_exe(fakes / "zsh", FAKE_SHELL)),
        "FAKE_DIR": str(fakes), "FAKE_LOG": str(fakes / "log"),
        "LEAKY_PARENT_VARIABLE": "must-not-reach-the-subshell", "REHEARSE_TTY": tty,
    }
    return {"env": env, "fakes": fakes, "home": home, "tmp": tmp}


def _run(rig, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT), *args], env={**rig["env"], **extra}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60, check=False)


def _log(rig) -> str:
    log = rig["fakes"] / "log"
    return log.read_text() if log.exists() else ""


def test_opens_a_clean_rehearsal_shell_and_cleans_up(rig):
    before = (rig["fakes"] / "search-list")
    result = _run(rig)
    assert result.returncode == 0, result.stderr
    sub = dict(line.split("=", 1) for line in (rig["home"] / "subshell.env").read_text().splitlines() if "=" in line)
    assert sub["COUNCIL_ETORO_BASE_URL"] == "http://127.0.0.1:45678"
    assert sub["COUNCIL_ROLE"] == "operator" and sub["COUNCIL_MODE"] == "stub"
    state = Path(sub["COUNCIL_STATE_DIR"])
    assert state.name == "state" and str(state).startswith(str(rig["tmp"].resolve()))
    assert Path(sub["COUNCIL_KEYCHAIN_FILE"]).parent == state.parent
    assert "LEAKY_PARENT_VARIABLE" not in sub and "REHEARSE_COUNCIL" not in sub
    assert sub["PATH"].split(":")[0] == str(state.parent / "bin")          # council-op wrapper first
    assert sub["PROMPT"] == "[REHEARSAL] "                                  # zsh -f prompt (PS1 too)
    assert "fake-read" in result.stdout                                     # the FAKE tokens are shown
    log = _log(rig)
    assert "argv=ops assert-operator state=unset role=operator" in log
    assert f"argv=rehearse fake-broker --scenario onboarding state={state} role=dev" in log
    assert "argv=ops record-dress --sandbox " + str(state) + " state=unset role=operator" in log
    assert f"security create-keychain {sub['COUNCIL_KEYCHAIN_FILE']}" in log
    assert f"security delete-keychain {state / 'council-write.keychain-db'}" in log
    assert before.read_text().count('"') == 2                               # search list = login only
    assert "keychain search list unchanged" in result.stdout
    assert list(rig["tmp"].iterdir()) == []                                 # the sandbox is removed
    assert not (rig["home"] / "Library" / "Application Support").exists()   # the real state dir untouched


def test_keep_keeps_the_sandbox_without_tokens(rig):
    result = _run(rig, "--keep")
    assert result.returncode == 0, result.stderr
    [box] = list(rig["tmp"].iterdir())
    assert (box / "state" / "REHEARSAL").is_file()
    assert not (box / "state" / "rehearsal-tokens.txt").exists()
    assert (box / "remote.git").is_dir()


@pytest.mark.parametrize("name", ["COUNCIL_STATE_DIR", "COUNCIL_KEYCHAIN_FILE", "COUNCIL_ETORO_BASE_URL"])
def test_refuses_a_leftover_rehearsal_variable(rig, name):
    result = _run(rig, **{name: "/somewhere"})
    assert result.returncode == 1 and name in result.stderr
    assert _log(rig) == "" and list(rig["tmp"].iterdir()) == []


def test_refuses_an_agent_context(rig):
    result = _run(rig, FAKE_OPERATOR_EXIT="2")
    assert result.returncode == 1 and "not an operator terminal" in result.stderr
    assert "fake-broker" not in _log(rig) and list(rig["tmp"].iterdir()) == []


def test_refuses_a_sandbox_inside_the_real_state_dir(rig):
    real = rig["home"] / "Library" / "Application Support" / "council-book"
    real.mkdir(parents=True)
    result = _run(rig, TMPDIR=str(real))
    assert result.returncode == 1 and "real state dir" in result.stderr
    assert list(real.iterdir()) == []


def test_a_dead_fake_broker_stops_the_rehearsal_and_records_nothing(rig):
    result = _run(rig, FAKE_BROKER_FAILS="1")
    assert result.returncode == 1 and "fake broker did not start" in result.stderr
    assert "record-dress" not in _log(rig) and list(rig["tmp"].iterdir()) == []
    assert not (rig["home"] / "subshell.env").exists()


@pytest.mark.parametrize("tty", ["/dev/null", "/dev/ttyNOPE"])
def test_refuses_without_an_operator_terminal(rig, tty):
    result = _run(rig, REHEARSE_TTY=tty)
    assert result.returncode != 0 and "no terminal" in result.stderr
    assert list(rig["tmp"].iterdir()) == []                   # nothing created
