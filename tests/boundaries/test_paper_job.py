"""The paper swing launchd job (user decision 2026-10-01; code only, the operator installs and loads
it): weekday 14:52 / 18:52 UTC, a winter 14:52 start is skipped, the command is the dry-run paper
cycle, the label stays outside the live `com.fbzz.council.*` checks, and agents are denied the script.
The script runs here only in its echo mode with a replaced clock."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess

import pytest

from council.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "ops" / "paper-cycle.sh"
TEMPLATE = REPO_ROOT / "ops" / "launchd" / "com.fbzz.council-paper.cycle.plist.tmpl"
RUNBOOK = REPO_ROOT / "docs" / "runbook.md"


def _render() -> dict:
    text = (TEMPLATE.read_text().replace("{{REPO}}", "/tmp/repo").replace("{{HOME}}", "/tmp/home")
            .replace("{{LOGS}}", "/tmp/logs"))
    return plistlib.loads(text.encode())


def test_the_template_runs_the_script_at_the_swing_slots_on_weekdays():
    pl = _render()
    assert pl["Label"] == "com.fbzz.council-paper.cycle" and not pl["Label"].startswith("com.fbzz.council.")
    assert pl["ProgramArguments"][-1] == "/tmp/repo/ops/paper-cycle.sh" and pl["RunAtLoad"] is False
    slots = {(d["Weekday"], d["Hour"], d["Minute"]) for d in pl["StartCalendarInterval"]}
    assert slots == {(w, h, 52) for w in range(1, 6) for h in (14, 18)}
    env = pl["EnvironmentVariables"]
    assert env["COUNCIL_AGENT_CONTEXT"] == "1" and env["COUNCIL_ROLE"] == "runner"
    assert "COUNCIL_MODE" not in env                       # the script pins dry_run itself
    assert not any(v.lower().startswith(("http://", "https://")) for v in env.values())


def _run(tmp_path, hour: str, dow: str, zone: str) -> tuple[str, str]:
    env = {**os.environ, "COUNCIL_STATE_DIR": str(tmp_path), "PAPER_CYCLE_ECHO": "1",
           "PAPER_CYCLE_UTC_HOUR": hour, "PAPER_CYCLE_UTC_DOW": dow, "PAPER_CYCLE_NY_ZONE": zone,
           "COUNCIL_REPO": str(REPO_ROOT)}
    out = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20, check=True)
    log = tmp_path / "paper" / "logs" / "paper-cycle.log"
    return out.stdout.strip(), log.read_text() if log.exists() else ""


@pytest.mark.parametrize("hour, dow, zone, runs", [
    ("14", "3", "EDT", True),      # summer: both slots
    ("18", "3", "EDT", True),
    ("14", "3", "EST", False),     # winter (from 1 Nov 2026): 18:52 only
    ("18", "3", "EST", True),
    ("18", "6", "EST", False),     # weekend
])
def test_the_script_runs_the_dry_run_paper_cycle_only_at_a_swing_slot(tmp_path, hour, dow, zone, runs):
    stdout, log = _run(tmp_path, hour, dow, zone)
    if runs:
        assert stdout.startswith("COUNCIL_MODE=dry_run ") and stdout.endswith("council cycle --paper")
    else:
        assert stdout == "" and "skipped" in log


def test_the_script_never_trades_or_loads_jobs():
    text = SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for word in ("approve", "launchctl", "COUNCIL_MODE=live", "resume-exec", "council-op"):
        assert word not in code, word
    assert "COUNCIL_MODE=dry_run" in code and "cycle --paper" in code
    assert os.access(SCRIPT, os.X_OK)


def test_agents_are_denied_the_script_in_both_rule_files():
    rules = set(json.loads((REPO_ROOT / "ops" / "claude" / "deny-rules.json").read_text())["deny"])
    deny = set(json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())["permissions"]["deny"])
    for rule in ("Bash(ops/paper-cycle.sh:*)", "Bash(./ops/paper-cycle.sh:*)"):
        assert rule in rules and rule in deny


def test_the_runbook_has_the_operator_commands():
    text = RUNBOOK.read_text()
    assert "com.fbzz.council-paper.cycle" in text and "launchctl bootstrap" in text
    assert "COUNCIL_FRED_TOKEN" in text and "council-op keys store ntfy-topic" in text
    assert "llm_billing_error" in text
