"""The private views are on the main CLI and refuse any agent context (transparency-v2 T1v hook).

`council inputs <cycle> [--html]`, `council inputs verify|prune` and `council purge-licensed` can
print eToro Licensed Content, so each one refuses (exit 2, before reading anything) unless it runs
in the operator's interactive terminal; `CLAUDECODE=1` is always refused."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from council import cli

CYCLE = "2026-10-01T1440Z"
COMMANDS = [
    ["inputs", CYCLE],
    ["inputs", CYCLE, "--html"],
    ["inputs", "show", CYCLE, "--role", "news"],
    ["inputs", "verify", CYCLE],
    ["inputs", "prune", "--before", "2026-01-01"],
    ["purge-licensed", "--dry-run"],
    ["purge-licensed", "--all"],
]


def test_the_private_commands_are_registered():
    names = {c.name for c in cli.app.registered_commands}
    groups = {g.name for g in cli.app.registered_groups}
    assert "purge-licensed" in names and "inputs" in groups


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_claudecode_is_refused_before_anything_is_read(argv, tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    monkeypatch.setenv("CLAUDECODE", "1")
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 2, result.output
    assert "CLAUDECODE is set" in result.output
    assert not (tmp_path / "purge-receipts").exists() and not (tmp_path / "inputs-view").exists()
