"""SW-5b CLI: `council swing status` (operator terminal, ledger only) and `council stocks rank`
retargeted to the SQ-8 paper benchmark (tracked, never traded), still an operator command."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from council import cli
from tests.cli.operator_sim import simulate_operator

DEV_REFUSAL = "operator command"


def test_swing_status_is_an_operator_command_and_prints_the_status(monkeypatch):
    res = CliRunner().invoke(cli.app, ["swing", "status"])
    assert res.exit_code == 2 and DEV_REFUSAL in res.output
    assert cli.OPERATOR_COMMANDS["swing status"] is False
    simulate_operator(monkeypatch)
    res = CliRunner().invoke(cli.app, ["swing", "status", "--asof", "2026-10-01"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("swing status 2026-10-01: CLEAN")
    assert "open trades: 0" in res.output and "paper benchmark" in res.output


def test_stocks_rank_ranks_the_sq8_benchmark(monkeypatch, tmp_path):
    from council.benchmark import sq8
    from council.stocks import commands

    seen: dict = {}

    def services(root, settings, *, eligibility, prefetch=True):
        seen.update(root=root, eligibility=eligibility, prefetch=prefetch)
        return SimpleNamespace(build_inputs="BUILD")

    def bench(day, build_inputs, *, state_dir):
        seen.update(day=day, build=build_inputs, state_dir=state_dir)
        return sq8.BenchmarkRank(quarter="2026Q3", asof=day, selected=("AAA", "BBB"), kept=(), files={})

    monkeypatch.setattr(commands, "live_rank_services", services)
    monkeypatch.setattr(sq8, "run_benchmark_rank", bench)
    res = CliRunner().invoke(cli.app, ["stocks", "rank", "--asof", "2026-08-20"])
    assert res.exit_code == 2 and DEV_REFUSAL in res.output          # the guard is kept
    assert not seen
    simulate_operator(monkeypatch)
    res = CliRunner().invoke(cli.app, ["stocks", "rank", "--asof", "2026-08-20"])
    assert res.exit_code == 0, res.output
    assert seen["eligibility"] is False and seen["prefetch"] is False and seen["build"] == "BUILD"
    assert seen["day"] == date(2026, 8, 20) and Path(seen["state_dir"]) == Path(seen["root"])
    assert "SQ-8 paper benchmark rank 2026Q3" in res.output and "tracked, never traded" in res.output
    assert "Rank the SQ-8 paper benchmark (tracked, never traded)" in (cli.stocks_rank.__doc__ or "")


def test_swing_commands_are_denied_to_agents_identically():
    root = Path(__file__).resolve().parents[2]
    settings = json.loads((root / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    rules = json.loads((root / "ops" / "claude" / "deny-rules.json").read_text())
    rules = rules["deny"] if isinstance(rules, dict) else rules
    for rule in ("Bash(council swing:*)", "Bash(uv run council swing:*)"):
        assert rule in settings and rule in rules


def test_approve_takes_skip_refs():
    res = CliRunner().invoke(cli.app, ["approve", "--help"])
    assert "--skip" in res.output
