"""M5-J: runbook v2 and docs (m5-readiness §9.2, §10 M5-J). The public runbook names every operator
command the design lists, carries no funding figure, and the docs point to the licensed-content rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = (ROOT / "docs" / "runbook.md").read_text()


def _section(title: str) -> str:
    parts = re.split(r"^## ", RUNBOOK, flags=re.MULTILINE)
    matches = [p for p in parts if p.split("\n", 1)[0].strip().endswith(title)]
    assert len(matches) == 1, title
    return matches[0]


def test_the_runbook_has_the_v2_sections_in_order():
    heads = [h.strip() for h in re.findall(r"^## \d+\. (.+)$", RUNBOOK, flags=re.MULTILINE)]
    wanted = ["Before the token", "Token day (core track)", "Every proposal", "Kill switch", "Incidents",
              "Weekly (10 minutes)"]
    assert [h for h in heads if h in wanted] == wanted


def test_every_proposal_names_inbox_show_approve_reject_and_why():
    text = _section("Every proposal")
    for cmd in ("council-op inbox", "council-op show <id>", "council-op approve <id>",
                "council-op reject <id> --reason", "council why <cycle>"):
        assert cmd in text, cmd


@pytest.mark.parametrize("situation, commands", [
    ("execution_unknown", ("resume-exec",)),
    ("An order waiting", ("ops resolve", "--filled", "--cancelled")),
    ("blocked", ("ops review", "--reason")),
    ("HALT", ("§4",)),
    ("Token rejected", ("keys store-read", "keys store-write", "keys verify")),
    ("Keychain locked", ("next cycle recovers",)),
    ("Licensed Content", ("purge-licensed --all", "24 hours", "receipt")),
    ("Leak in the public repository", ("ops/uninstall.sh", "rotate")),
])
def test_the_incident_table_has_a_row_and_command_per_situation(situation, commands):
    rows = [line for line in _section("Incidents").splitlines() if line.startswith("| ")]
    (row,) = [r for r in rows if situation in r.split("|")[1]]
    for cmd in commands:
        assert cmd in row, (situation, cmd)


def test_halt_recovery_keeps_the_lifetime_peak():
    text = _section("Kill switch")
    assert "council-op resume --reason" in text and "lifetime peak stays" in text


def test_the_weekly_check_runs_the_post_token_doctor():
    text = _section("Weekly (10 minutes)")
    for item in ("doctor --ready --post-token", "ops page", "mirror", "stocks status"):
        assert item in text, item


def test_no_funding_figure_in_the_public_runbook():
    assert "--funding-usd <N>" in RUNBOOK
    assert re.search(r"--funding-usd\s+\d", RUNBOOK) is None
    assert re.search(r"(?:\$|USD\s?)\d", RUNBOOK) is None


def test_data_rights_has_the_licensed_content_paragraph():
    rights = (ROOT / "docs" / "data-rights.md").read_text()
    assert re.search(r"^## Licensed content \(eToro\)$", rights, flags=re.MULTILINE)
    body = rights.split("## Licensed content (eToro)", 1)[1]
    for phrase in ("Licensed Content", "never published", "7 days", "council purge-licensed --all"):
        assert phrase in body, phrase


def test_agent_rule_files_list_the_new_operator_commands():
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (ROOT / name).read_text()
        for cmd in ("purge-licensed", "smoke propose", "doctor --ready", "rehearse onboarding"):
            assert cmd in text, (name, cmd)
