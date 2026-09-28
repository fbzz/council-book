"""The launchd soak jobs (m5-readiness M5-F, gate O3), with fake cycles, watches and probes: no
model, no broker, no launchd; the local remote is a real bare repository in tmp."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from council import clock
from council.operator import readiness, soak
from council.paths import REPO_ROOT
from tests.boundaries.test_writer_imports import imported_names

LAUNCHD = {"COUNCIL_ROLE": "runner", "XPC_SERVICE_NAME": "com.fbzz.council.rehearsal.watch"}
T0 = datetime(2026, 10, 5, 2, 40, tzinfo=UTC)
HEAD = "ab" * 20


def test_real_model_only_in_the_1040_utc_slot():
    assert soak.choose_llm(datetime(2026, 10, 5, 10, 41, tzinfo=UTC)) == "real"
    assert soak.choose_llm(datetime(2026, 10, 5, 11, 40, tzinfo=UTC)) == "real"     # same slot, already_done
    tried = [{"kind": "real_attempt", "at": "2026-10-05T10:40:00+00:00", "llm": "real"}]
    assert soak.choose_llm(datetime(2026, 10, 5, 11, 40, tzinfo=UTC), tried) == "stub"   # no real retry
    assert soak.choose_llm(datetime(2026, 10, 6, 10, 40, tzinfo=UTC), tried) == "real"   # next day
    assert soak.choose_llm(datetime(2026, 10, 5, 14, 40, tzinfo=UTC)) == "stub"
    assert soak.choose_llm(datetime(2026, 10, 5, 9, 40, tzinfo=UTC)) == "stub"


def test_local_remote_is_created_and_a_foreign_origin_refused(tmp_path):
    root = tmp_path / "rehearsal"
    clone = soak.ensure_local_remote(root)
    assert clone == root / "publisher-clone" and (root / "remote.git" / "HEAD").is_file()
    assert soak.ensure_local_remote(root) == clone                       # idempotent
    git = ["git", "-C", str(clone)]
    subprocess.run([*git, "remote", "set-url", "--push", "origin", "https://example.invalid/x.git"], check=True)
    with pytest.raises(soak.SoakError, match="push URL"):
        soak.assert_local_origin(root)
    subprocess.run([*git, "config", "--unset", "remote.origin.pushurl"], check=True)
    subprocess.run([*git, "remote", "set-url", "origin", "https://example.invalid/x.git"], check=True)
    with pytest.raises(soak.SoakError, match="origin is not"):
        soak.ensure_local_remote(root)


def test_cycle_job_builds_a_rehearsal_context_and_logs_a_code(tmp_path):
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return "ctx"

    def cycle(ctx):
        return SimpleNamespace(cycle_id="2026-10-05T1040Z", status="on_time", published=True)

    now = datetime(2026, 10, 5, 10, 41, tzinfo=UTC)
    event = soak.run_soak_cycle(state_dir=tmp_path, now=now, env=LAUNCHD, build=build, cycle=cycle)
    root = tmp_path / "rehearsal"
    assert seen == {"mode": "dry_run", "stub_llm": False, "publish": "push", "state_dir": root,
                    "publisher_dir": root / "publisher-clone"}
    assert event["llm"] == "real" and event["launchd"] is True and event["status"] == "on_time"
    assert soak.read_events(root) == [{"kind": "real_attempt", "at": event["at"], "llm": "real"}, event]

    def boom(ctx):
        raise RuntimeError("model down")

    with pytest.raises(RuntimeError):
        soak.run_soak_cycle(state_dir=tmp_path, now=now + timedelta(hours=4), env={}, build=build, cycle=boom)
    last = soak.read_events(root)[-1]
    assert last["status"] == "error:runtimeerror" and last["launchd"] is False and last["llm"] == "stub"


def _soak_events(hours: int, *, real: bool = True, skip: int = 0, probes: bool = True) -> list[dict]:
    events, slot = [], T0
    while slot <= T0 + timedelta(hours=hours):
        if skip and len(events) == skip:
            skip, slot = 0, slot + timedelta(hours=4)
            continue
        llm = "real" if real and slot.hour == 10 else "stub"
        events.append({"kind": "cycle", "at": slot.isoformat(), "llm": llm, "launchd": True,
                       "cycle_id": clock.cycle_id_for(slot), "status": "on_time"})
        slot += timedelta(hours=4)
    if probes:
        events.append({"kind": "probe", "at": T0.isoformat(), "push": "push_auth_ok",
                       "keychain": "keychain_ok", "launchd": True})
    return events


@pytest.mark.parametrize(("events", "code"), [
    (_soak_events(49), "soak_ok"),
    (_soak_events(30), "soak_late"),                # the jobs stopped after 30 h
    (_soak_events(49, skip=3), "soak_late"),
    (_soak_events(49, real=False), "soak_no_real_llm"),
    (_soak_events(49, probes=False), "soak_no_push_auth"),
    ([e | {"launchd": False} for e in _soak_events(49)], "soak_missing"),
])
def test_o3_thresholds(events, code):
    state, got = soak.evaluate(events, T0 + timedelta(hours=49))
    assert got == code and state == ("green" if code == "soak_ok" else "red")


def test_short_soak():
    assert soak.evaluate(_soak_events(30), T0 + timedelta(hours=31)) == ("red", "soak_short")


def test_keychain_probe_needed_too():
    events = _soak_events(49)
    events[-1]["keychain"] = "keychain_failed"
    assert soak.evaluate(events, T0 + timedelta(hours=49)) == ("red", "soak_no_keychain")


def test_watch_probes_daily_and_after_a_failure_and_writes_o3(tmp_path):
    writes, pushes = [], []

    def push(state):
        pushes.append(state)
        return "push_auth_failed" if len(pushes) == 1 else "push_auth_ok"

    def run(now):
        return soak.run_soak_watch(state_dir=tmp_path, now=now, env=LAUNCHD, build=lambda **k: "ctx",
                                   watch=lambda ctx: SimpleNamespace(status="ok"), push_probe=push,
                                   keychain_probe=lambda: "keychain_ok",
                                   write=lambda **k: writes.append(k) or tmp_path, head=lambda: HEAD)

    first = run(T0)
    assert first["probe"]["push"] == "push_auth_failed" and pushes == [tmp_path]
    assert run(T0 + timedelta(minutes=15))["probe"]["push"] == "push_auth_ok"      # retried after a failure
    assert run(T0 + timedelta(minutes=30))["probe"] is None                         # then once a day
    assert run(T0 + timedelta(hours=24, minutes=30))["probe"] is not None
    assert writes[-1]["head"] == HEAD and writes[-1]["env"] == LAUNCHD
    assert writes[-1]["gates"] == {"O3": {"state": "red", "code": "soak_missing"}}


def test_soak_record_is_written_only_by_the_launchd_rehearsal_job(tmp_path):
    with pytest.raises(readiness.ReadinessError, match="rehearsal"):
        soak._default_write(head=HEAD, gates={"O3": {"state": "red", "code": "soak_short"}},
                            state_dir=tmp_path, env={"COUNCIL_ROLE": "runner"})
    path = soak._default_write(head=HEAD, gates={"O3": {"state": "red", "code": "soak_short"}},
                               state_dir=tmp_path, env=LAUNCHD)
    record = json.loads(Path(path).read_text())
    readiness.validate_record("soak", record)
    assert record["gates"]["O3"] == {"state": "red", "code": "soak_short"}


def test_probes_keep_codes_only():
    token = "probe-" + "x" * 8
    assert soak.probe_keychain(lambda service: token) == "keychain_ok"
    assert soak.probe_keychain(lambda service: "") == "keychain_empty"

    def fail(service):
        raise OSError(token)

    assert soak.probe_keychain(fail) == "keychain_failed"


def test_push_probe_is_a_dry_run_on_the_real_publisher_clone(tmp_path):
    assert soak.probe_push(tmp_path) == "push_auth_no_clone"
    (tmp_path / "publisher-clone" / ".git").mkdir(parents=True)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert soak.probe_push(tmp_path, runner=runner) == "push_auth_ok"
    assert calls == [["git", "-C", str(tmp_path / "publisher-clone"), "push", "--dry-run", "-q", "origin", "HEAD:main"]]
    fail = lambda cmd, **k: subprocess.CompletedProcess(cmd, 128, "", "")  # noqa: E731
    assert soak.probe_push(tmp_path, runner=fail) == "push_auth_failed"


def test_soak_never_imports_the_broker_or_the_approval_path():
    names = imported_names(REPO_ROOT / "src" / "council" / "operator" / "soak.py")
    assert not {n for n in names if n.startswith(("council.broker", "council.operator.approve", "council.execution"))}
