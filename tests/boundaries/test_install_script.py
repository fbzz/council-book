"""ops/install.sh and ops/uninstall.sh in a sandbox (m5-readiness M5-F acceptance).

HOME is a tmp dir; `git`, `launchctl`, `plutil`, `uv` and the release's `council` are fakes on PATH
that log their argv; the operator's terminal is a real pty. Nothing touches launchd, the network or
the real state dir.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from council.paths import REPO_ROOT

INSTALL = REPO_ROOT / "ops" / "install.sh"
UNINSTALL = REPO_ROOT / "ops" / "uninstall.sh"
TEMPLATES = REPO_ROOT / "ops" / "launchd"
ORIGIN = "https://github.com/fbzz/council-book.git"
TAG = "council-spec-v1"
COMMIT = "".join("0123456789abcdef"[(i * 7) % 16] for i in range(40))
LIVE = ("com.fbzz.council.cycle", "com.fbzz.council.watch")
SOAK = ("com.fbzz.council.rehearsal.cycle", "com.fbzz.council.rehearsal.watch")

pytestmark = pytest.mark.skipif(not Path("/dev/ptmx").exists(), reason="needs a pty")

FAKE_GIT = r'''
import json, os, shutil, sys
from pathlib import Path
env = os.environ
with open(env["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["git", *sys.argv[1:]]) + "\n")
args = sys.argv[1:]
if args[0] == "ls-remote":
    if env.get("FAKE_LSREMOTE_FAIL"):
        sys.exit(128)
    tag = env["FAKE_TAG"]
    if env.get("FAKE_TAG_ABSENT"):
        sys.exit(0)
    print("f" * 40 + "\trefs/tags/" + tag)
    print(env["FAKE_COMMIT"] + "\trefs/tags/" + tag + "^{}")
    sys.exit(0)
if args[0] == "clone":
    dest = Path(args[-1])
    (dest / ".git").mkdir(parents=True)
    (dest / ".fakeorigin").write_text(args[-2])
    (dest / ".fakehead").write_text(env.get("FAKE_CLONE_HEAD", env["FAKE_COMMIT"]))
    shutil.copytree(env["FAKE_TEMPLATES"], dest / "ops" / "launchd")
    sys.exit(0)
if args[0] == "-C":
    repo, cmd = Path(args[1]), args[2:]
    if cmd[:2] == ["config", "--get"]:
        print((repo / ".fakeorigin").read_text())
    elif cmd[:2] == ["rev-parse", "HEAD"]:
        print((repo / ".fakehead").read_text())
    elif cmd[0] == "status":
        print(env.get("FAKE_DIRTY", ""), end="")
    elif cmd[0] == "fetch":
        pass
    else:
        sys.exit(1)
    sys.exit(0)
sys.exit(1)
'''

FAKE_LAUNCHCTL = r'''
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["launchctl", *sys.argv[1:]]) + "\n")
if sys.argv[1] == "print":
    label = sys.argv[2].rsplit("/", 1)[-1]
    sys.exit(0 if label in os.environ.get("FAKE_LOADED", "").split() else 113)
sys.exit(0)
'''

FAKE_PLUTIL = r'''
import plistlib, sys
with open(sys.argv[-1], "rb") as fh:
    plistlib.load(fh)
'''

FAKE_COUNCIL = r'''
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["council", *sys.argv[1:]]) + "\n")
if sys.argv[1:3] == ["ops", "assert-operator"]:
    sys.exit(int(os.environ.get("FAKE_ASSERT_RC", "0")))
if sys.argv[1] == "doctor":
    sys.exit(int(os.environ.get("FAKE_DOCTOR_RC", "0")))
sys.exit(0)
'''

FAKE_UV = r'''
import json, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(["uv", *sys.argv[1:]]) + "\n")
bindir = Path.cwd() / ".venv" / "bin"
bindir.mkdir(parents=True, exist_ok=True)
council = bindir / "council"
council.write_text("#!" + sys.executable + "\n" + os.environ["FAKE_COUNCIL_SRC"])
council.chmod(0o755)
'''


def _script(path: Path, body: str) -> None:
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def sandbox(tmp_path: Path):
    home, fakebin = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    fakebin.mkdir()
    for name, body in (("git", FAKE_GIT), ("launchctl", FAKE_LAUNCHCTL), ("plutil", FAKE_PLUTIL), ("uv", FAKE_UV)):
        _script(fakebin / name, body)
    return home, fakebin, tmp_path / "calls.log"


class Run:
    def __init__(self, proc: subprocess.CompletedProcess[str], log: Path, home: Path):
        self.rc, self.out, self.err = proc.returncode, proc.stdout, proc.stderr
        self.calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.home = home

    def called(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]

    @property
    def state(self) -> Path:
        return self.home / "Library" / "Application Support" / "council-book"


def _run(sandbox, script: Path, *args: str, answer: str | None = COMMIT[:8], tty: str | None = "pty",
         **fake: str) -> Run:
    home, fakebin, log = sandbox
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "LC_ALL": "C",
        "FAKE_LOG": str(log), "FAKE_TAG": TAG, "FAKE_COMMIT": COMMIT, "FAKE_TEMPLATES": str(TEMPLATES),
        "FAKE_COUNCIL_SRC": FAKE_COUNCIL, **fake,
    }
    master = slave = None
    if tty == "pty":
        master, slave = os.openpty()
        env["COUNCIL_INSTALL_TTY"] = os.ttyname(slave)
        if answer is not None:
            os.write(master, (answer + "\n").encode())
    elif tty is not None:
        env["COUNCIL_INSTALL_TTY"] = tty
    try:
        proc = subprocess.run(["/bin/sh", str(script), *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60, check=False)
    finally:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
    return Run(proc, log, home)


def _plist(run: Run, label: str) -> dict:
    return plistlib.loads((run.home / "Library" / "LaunchAgents" / f"{label}.plist").read_bytes())


# ------------------------------------------------------------------------------------ happy paths
def test_render_only_pins_release_from_origin_and_loads_nothing(sandbox):
    run = _run(sandbox, INSTALL, TAG)
    assert run.rc == 0, run.err
    assert run.called("git", "ls-remote", ORIGIN, f"refs/tags/{TAG}")
    clone = run.called("git", "clone")
    assert clone and clone[0][-2] == ORIGIN and "--branch" in clone[0]
    rel = str(run.state / "releases" / TAG)
    assert ["git", "-C", rel, "fetch", "--quiet", "origin", "refs/tags/stocks-*:refs/tags/stocks-*"] in run.calls
    record = json.loads((run.state / "releases" / "installed.json").read_text())
    assert record["commit"] == COMMIT and record["tag"] == TAG and record["origin"] == ORIGIN
    assert stat.S_IMODE((run.state / "releases" / "installed.json").stat().st_mode) == 0o600
    assert (run.state / "releases" / "current").resolve() == Path(rel).resolve()
    assert run.called("council", "ops", "assert-operator")
    assert not run.called("launchctl", "bootstrap") and not run.called("council", "doctor")
    for label in LIVE:
        pl = _plist(run, label)
        assert pl["Label"] == label
        assert pl["EnvironmentVariables"]["COUNCIL_ROLE"] == "runner"
        assert pl["EnvironmentVariables"]["COUNCIL_MODE"] == "live"
        assert all("{{" not in a for a in pl["ProgramArguments"])
        assert any(a == str(run.state / "releases" / "current" / ".venv" / "bin" / "council")
                   for a in pl["ProgramArguments"])
    assert f"commit {COMMIT}" in run.out


def test_live_load_runs_the_readiness_gate_then_bootout_bootstrap(sandbox):
    run = _run(sandbox, INSTALL, TAG, "--load")
    assert run.rc == 0, run.err
    assert run.called("council", "doctor", "--ready", "--track", "core", "--post-token")
    boot = [c for c in run.calls if c[:2] in (["launchctl", "bootout"], ["launchctl", "bootstrap"])]
    uid = str(os.getuid())
    assert boot == [["launchctl", "bootout", f"gui/{uid}/{LIVE[0]}"],
                    ["launchctl", "bootstrap", f"gui/{uid}", str(run.home / "Library/LaunchAgents" / f"{LIVE[0]}.plist")],
                    ["launchctl", "bootout", f"gui/{uid}/{LIVE[1]}"],
                    ["launchctl", "bootstrap", f"gui/{uid}", str(run.home / "Library/LaunchAgents" / f"{LIVE[1]}.plist")]]


def test_stocks_track_gate(sandbox):
    run = _run(sandbox, INSTALL, TAG, "--load", "--track", "stocks")
    assert run.rc == 0, run.err
    assert run.called("council", "doctor", "--ready", "--track", "stocks", "--post-token")


def test_rehearsal_renders_and_loads_soak_jobs_only(sandbox):
    old = sandbox[0] / "Library" / "Application Support" / "council-book" / "rehearsal"
    old.mkdir(parents=True)
    (old / "soak-log.jsonl").write_text("{}\n")
    run = _run(sandbox, INSTALL, TAG, "--rehearsal", "--load", "--publish", "local")
    assert run.rc == 0, run.err
    assert not run.called("council", "doctor")
    loaded = [c[-1].rsplit("/", 1)[-1] for c in run.called("launchctl", "bootstrap")]
    assert loaded == [f"{label}.plist" for label in SOAK]
    assert not (run.home / "Library/LaunchAgents" / f"{LIVE[0]}.plist").exists()
    assert not (old / "soak-log.jsonl").exists() and list(old.glob("soak-log.*.jsonl"))
    release_python = str(run.state / "releases" / "current" / ".venv" / "bin" / "python")
    cycle, watch = _plist(run, SOAK[0]), _plist(run, SOAK[1])
    for pl, job in ((cycle, "cycle"), (watch, "watch")):
        env = pl["EnvironmentVariables"]
        assert env["COUNCIL_ROLE"] == "runner" and env["COUNCIL_MODE"] == "dry_run"
        assert env["COUNCIL_AGENT_CONTEXT"] == "1"
        assert pl["ProgramArguments"][-4:] == [release_python, "-m", "council.operator.soak", job]
        assert pl["StandardOutPath"].endswith(f"rehearsal-{job}.out.log")
        assert not any(v.startswith(("http://", "https://")) for v in env.values())
    assert cycle["StartCalendarInterval"] == {"Minute": 40} and cycle["RunAtLoad"] is False
    assert watch["StartInterval"] == 900 and watch["RunAtLoad"] is True


# ------------------------------------------------------------------------------------ refusals
@pytest.mark.parametrize(("args", "fake", "needle"), [
    ((), {"FAKE_CLONE_HEAD": "e" * 40}, "is not council-spec-v1 on origin"),
    ((), {"FAKE_TAG_ABSENT": "1"}, "not on origin"),
    ((), {"FAKE_LSREMOTE_FAIL": "1"}, "cannot read tags from origin"),
    ((), {"FAKE_DIRTY": " M policy/risk.yaml\n"}, "local changes"),
    ((), {"FAKE_ASSERT_RC": "2"}, "ops assert-operator"),
    (("--load",), {"FAKE_DOCTOR_RC": "1"}, "no override"),
    (("--load",), {"FAKE_LOADED": SOAK[1]}, "soak jobs are loaded"),
    (("--rehearsal", "--load"), {"FAKE_LOADED": LIVE[0]}, "live jobs are loaded"),
    ((), {"FAKE_LOADED": LIVE[1]}, "use --load (the readiness gate)"),
    (("--rehearsal",), {"FAKE_LOADED": LIVE[0]}, "use --load (the readiness gate)"),
    (("--rehearsal", "--publish", "github"), {}, "rehearsal/remote.git only"),
    (("--force-load",), {}, "removed"),
    (("--load", "--track", "crypto"), {}, "--track must be"),
    ((), {"COUNCIL_STATE_DIR": "/tmp/elsewhere"}, "COUNCIL_STATE_DIR"),
])
def test_refusals_load_nothing(sandbox, args, fake, needle):
    run = _run(sandbox, INSTALL, TAG, *args, **fake)
    assert run.rc != 0
    assert needle in run.err
    assert not run.called("launchctl", "bootstrap") and not run.called("launchctl", "enable")
    assert not (run.state / "releases" / "installed.json").exists()      # a red gate unpins too
    assert not (run.state / "releases" / "current").exists()


def test_red_gate_restores_the_previous_pin(sandbox):
    _install(sandbox)
    state = sandbox[0] / "Library" / "Application Support" / "council-book" / "releases"
    before = ((state / "installed.json").read_text(), os.readlink(state / "current"))
    (state / "current").unlink()
    (state / "current").symlink_to(state / "older")         # the previously pinned release
    before = (before[0], str(state / "older"))
    run = _run(sandbox, INSTALL, TAG, "--load", FAKE_DOCTOR_RC="1")
    assert run.rc != 0 and "no override" in run.err
    assert ((state / "installed.json").read_text(), os.readlink(state / "current")) == before
    assert not run.called("launchctl", "bootstrap")


def test_rendered_plists_stay_disabled_until_the_gate_passes(sandbox):
    run = _run(sandbox, INSTALL, TAG)
    uid = os.getuid()
    assert run.called("launchctl", "disable") == [["launchctl", "disable", f"gui/{uid}/{label}"] for label in LIVE]
    assert not run.called("launchctl", "enable")
    run = _run(sandbox, INSTALL, TAG, "--load")
    assert run.rc == 0, run.err
    seq = [c[:2] for c in run.calls if c[:2] in (["launchctl", "enable"], ["launchctl", "bootstrap"])]
    assert seq == [["launchctl", "enable"], ["launchctl", "bootstrap"]] * 2
    doctor = next(i for i, c in enumerate(run.calls) if c[:2] == ["council", "doctor"])
    assert all(i > doctor for i, c in enumerate(run.calls) if c[:2] == ["launchctl", "enable"])


def test_live_load_refuses_while_a_soak_plist_is_rendered(sandbox):
    agents = sandbox[0] / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{SOAK[0]}.plist").write_text("x")
    run = _run(sandbox, INSTALL, TAG, "--load")
    assert run.rc != 0 and "uninstall.sh" in run.err
    assert not run.called("launchctl", "bootstrap")


def test_wrong_typed_prefix_refuses(sandbox):
    run = _run(sandbox, INSTALL, TAG, answer="deadbeef")
    assert run.rc != 0 and "does not match" in run.err
    assert not (run.state / "releases" / "installed.json").exists()
    assert not run.called("uv") and not run.called("council")


@pytest.mark.parametrize("tty", [None, "/dev/null", "__file__"])
def test_no_terminal_refuses(sandbox, tmp_path, tty):
    if tty == "__file__":
        fake_tty = tmp_path / "answer"
        fake_tty.write_text(COMMIT[:8] + "\n")
        tty = str(fake_tty)
    if tty is None and _has_controlling_tty():
        pytest.skip("this test process has a controlling terminal; /dev/tty would answer")
    run = _run(sandbox, INSTALL, TAG, tty=tty)      # None: /dev/tty with no controlling terminal
    assert run.rc != 0 and "no terminal" in run.err
    assert not run.called("git", "ls-remote") or not run.called("uv")
    assert not (run.state / "releases" / "installed.json").exists()


def _has_controlling_tty() -> bool:
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY))
    except OSError:
        return False
    return True


# ------------------------------------------------------------------------------------ uninstall
def _install(sandbox) -> None:
    assert _run(sandbox, INSTALL, TAG).rc == 0


@pytest.mark.parametrize(("flag", "labels"), [((), LIVE), (("--rehearsal",), SOAK), (("--all",), LIVE + SOAK)])
def test_uninstall_each_set(sandbox, flag, labels):
    _install(sandbox)
    agents = sandbox[0] / "Library" / "LaunchAgents"
    for label in LIVE + SOAK:
        (agents / f"{label}.plist").write_text("x")
    sandbox[2].unlink()
    run = _run(sandbox, UNINSTALL, *flag, answer="unload")
    assert run.rc == 0, run.err
    assert run.called("council", "ops", "assert-operator")
    uid = os.getuid()
    assert [c[-1] for c in run.called("launchctl", "bootout")] == [f"gui/{uid}/{label}" for label in labels]
    for label in LIVE + SOAK:
        assert (agents / f"{label}.plist").exists() == (label not in labels)


def test_uninstall_refuses_outside_operator_context(sandbox):
    _install(sandbox)
    sandbox[-1].write_text("")          # forget the install's own launchctl print/disable calls
    run = _run(sandbox, UNINSTALL, "--all", answer="unload", FAKE_ASSERT_RC="2")
    assert run.rc != 0 and "nothing unloaded" in run.err
    assert not run.called("launchctl")


def test_uninstall_refuses_without_release_and_bad_flag(sandbox):
    assert _run(sandbox, UNINSTALL, answer="unload").rc != 0
    assert _run(sandbox, UNINSTALL, "--live", tty=None).rc == 64


@pytest.mark.parametrize("tty", ["/nonexistent/tty", "/dev/null", "/dev/ttyNOSUCH"])
def test_uninstall_refuses_without_a_terminal(sandbox, tty):
    _install(sandbox)
    sandbox[-1].write_text("")
    run = _run(sandbox, UNINSTALL, "--all", tty=tty)
    assert run.rc != 0 and "no terminal" in run.err
    assert not run.called("launchctl") and not run.called("council")


@pytest.mark.parametrize("answer", ["", "yes", "UNLOAD"])
def test_uninstall_unloads_nothing_unless_the_operator_types_unload(sandbox, answer):
    _install(sandbox)
    sandbox[-1].write_text("")
    run = _run(sandbox, UNINSTALL, "--all", answer=answer)
    assert run.rc != 0 and "nothing unloaded" in run.err
    assert not run.called("launchctl")
