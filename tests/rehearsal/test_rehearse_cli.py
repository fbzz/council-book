"""`council rehearse onboarding|fake-broker` (dev role) and `council ops record-dress` (operator)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from council import cli, paths
from council.operator import keychain as kc
from council.operator import release
from council.rehearsal import onboarding as ob
from tests.cli.operator_sim import simulate_operator
from tests.rehearsal.conftest import use_sandbox

pytestmark = pytest.mark.capability_gates


def _run(argv):
    return CliRunner().invoke(cli.app, argv)


def test_rehearse_onboarding_passes_and_leaves_no_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    state_before = os.environ["COUNCIL_STATE_DIR"]
    result = _run(["rehearse", "onboarding"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "rehearsal passed" in result.output
    assert os.environ["COUNCIL_STATE_DIR"] == state_before                  # env restored
    assert "COUNCIL_KEYCHAIN_FILE" not in os.environ and "COUNCIL_ETORO_BASE_URL" not in os.environ
    assert kc.read_secret.__kwdefaults__["runner"] is subprocess.run        # fake `security` undone
    assert not paths.state_dir().exists() or not any(paths.state_dir().iterdir())
    assert list((tmp_path / "tmp").iterdir()) == []                          # the sandbox was removed


def test_rehearse_fake_broker_refuses_outside_a_marked_sandbox():
    result = _run(["rehearse", "fake-broker"])
    assert result.exit_code == 2 and "marked rehearsal sandbox" in result.output


def _real_state(monkeypatch):
    real = paths.state_dir()
    real.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(release, "default_state_dir", lambda: real)
    return real


def test_record_dress_green_after_a_full_walk(tmp_path, monkeypatch):
    real = _real_state(monkeypatch)
    box = ob.Sandbox.create(tmp_path / "dress")
    box.start_broker()
    try:
        with monkeypatch.context() as m:
            use_sandbox(box, m)
            results = ob.run_all(box)
            assert all(r.ok for r in results), box.log
    finally:
        box.stop()
    simulate_operator(monkeypatch)
    for name in ("COUNCIL_KEYCHAIN_FILE", "COUNCIL_ETORO_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    result = _run(["ops", "record-dress", "--sandbox", str(box.state_dir)])
    assert result.exit_code == 0, result.output
    record = json.loads((real / "readiness" / "dress.json").read_text())
    assert record["gates"]["O5"] == {"state": "green", "code": "dress_ok"}
    assert not (real / release.REHEARSAL_MARKER).exists()


def test_record_dress_is_red_for_an_unwalked_sandbox_and_refuses_inside_it(tmp_path, monkeypatch):
    real = _real_state(monkeypatch)
    box = ob.Sandbox.create(tmp_path / "empty")
    simulate_operator(monkeypatch)
    result = _run(["ops", "record-dress", "--sandbox", str(box.state_dir)])
    assert result.exit_code == 1 and "dress_failed" in result.output, result.output
    record = json.loads((real / "readiness" / "dress.json").read_text())
    assert record["gates"]["O5"]["state"] == "red"
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(box.state_dir))             # inside the [REHEARSAL] shell
    inside = _run(["ops", "record-dress", "--sandbox", str(box.state_dir)])
    assert inside.exit_code == 2 and "outside the [REHEARSAL] shell" in inside.output
