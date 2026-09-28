"""`council stocks rank` (design §4, D16, WP-D acceptance): it writes ONLY under the state dir and never
touches `policy/` (the trees are snapshotted), a proposal that breaks a validator exits non-zero, the
diff and the two-phase retirement against the committed sleeve, an ineligible name replaced within
its sector, `--no-eligibility` + the live preflight refusal, the history prefetch for new names, the
AI list (L9), and the CLI's exit codes. The broker is the FakeEtoro behind the READ client; the rank
inputs are synthetic; nothing touches the network, the Keychain or an LLM."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import date

import pytest
import yaml
from typer.testing import CliRunner

from council import paths
from council.cli import app
from council.ledger.db import Ledger
from council.paths import POLICY_DIR
from council.policy import SLEEVE_FILE, STOCK_RANK_FILE
from council.publish.leakscan import scan
from council.reference.report import UnsafePublicText, assert_public_safe
from council.stocks import commands, sleeve_file
from council.stocks import eligibility as gate
from council.stocks.universe import Candidate, RankInputs
from tests.stocks import cli_support as cs

D, NOW, Q = cs.D, cs.NOW, cs.Q


class Env:
    """A committed checkout, the synthetic book, a broker that lists every name, the services."""

    def __init__(self, tmp_path, *, broker_overrides=None, **repo_kw):
        self.tmp = tmp_path
        self.state = paths.state_dir()
        self.repo = cs.make_repo(tmp_path, **repo_kw)
        self.book = cs.sector_book()
        self.keys = [c.key for c in self.book.cands]
        self.broker = cs.broker([sleeve_file.broker_symbol_guess(k) for k in self.keys], **(broker_overrides or {}))
        self.fakes = cs.FakeServices(self.book, read=self.broker.read)

    def run(self, *, prefetch: bool = False, eligibility: bool = True, **kw) -> commands.RankRun:
        return commands.run_rank(kw.pop("asof", D), self.fakes.services(prefetch=prefetch), state_dir=self.state,
                                 repo=self.repo, eligibility=eligibility, **kw)

    def proposal(self) -> sleeve_file.StockSleeveFile:
        return sleeve_file.load(commands.proposal_dir(self.state, Q) / SLEEVE_FILE)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """The selection and shortlist on the synthetic book with every name eligible."""
    import os

    tmp = tmp_path_factory.mktemp("baseline")
    old = os.environ.get("COUNCIL_STATE_DIR")
    os.environ["COUNCIL_STATE_DIR"] = str(tmp / "state")
    try:
        run = Env(tmp).run()
    finally:
        if old is None:
            os.environ.pop("COUNCIL_STATE_DIR", None)
        else:
            os.environ["COUNCIL_STATE_DIR"] = old
    assert run.ok, run.errors
    return run


def roles(sleeve) -> dict[str, str]:
    return {ln.symbol: ln.role for ln in sleeve.lines}


# ------------------------------------------------------------------------------------ writes


def test_rank_writes_only_under_the_state_dir_and_never_touches_policy(env):
    repo_before, policy_before = cs.tree_digest(env.repo), cs.tree_digest(POLICY_DIR)
    head, tags = cs.git(env.repo, "rev-parse", "HEAD"), cs.git(env.repo, "tag", "--list")
    run = env.run()
    assert run.ok, run.errors
    assert cs.tree_digest(env.repo) == repo_before and cs.tree_digest(POLICY_DIR) == policy_before
    assert cs.git(env.repo, "rev-parse", "HEAD") == head and cs.git(env.repo, "tag", "--list") == tags
    assert cs.git(env.repo, "status", "--porcelain") == ""
    out = commands.proposal_dir(env.state, Q)
    assert run.directory == out and set(run.files) == {
        SLEEVE_FILE, f"ranking-{Q}.md", f"rank-{Q}.csv", "summary.json", "CHANGELOG-snippet.md"}
    for path in run.files.values():
        assert path.parent == out and (path.stat().st_mode & 0o777) == 0o600
    assert env.state in out.parents
    written = {p for p in env.state.rglob("*") if p.is_file()}
    assert all(env.state in p.parents for p in written)
    assert env.broker.eligibility_posts() == 1 and env.broker.writes() == 0
    text = "\n".join(run.report_lines())
    assert "nothing was written to policy/, committed or tagged" in text
    assert run.commands[0] == f"cp {out / SLEEVE_FILE} policy/{SLEEVE_FILE}"
    assert f"git tag stocks-{Q}" in run.commands and run.commands[-1] == "council stocks onboard"
    assert any(c.startswith("git commit -m ") and "8 in, 0 out, 0 retiring, 0 pruned" in c for c in run.commands)


def test_the_proposal_is_the_rule_selection_with_broker_symbols_and_stamps(env):
    run = env.run()
    sleeve = env.proposal()
    assert [ln.symbol for ln in sleeve.lines if ln.role == "selected"] == list(run.result.selected)
    assert [ln.symbol for ln in sleeve.lines if ln.role == "shortlist"] == list(run.result.shortlist)
    assert len(run.result.selected) == 8 and len(run.result.shortlist) == 8
    assert (sleeve.quarter, sleeve.rank_asof, sleeve.sleeve_weight, sleeve.names_target) == (Q, D, 0.5, 8)
    assert all(ln.eligibility_checked_at == NOW and ln.etoro_symbol == ln.symbol for ln in sleeve.lines)
    assert all(ln.cik == sleeve_file.cik10(int(run.result.eligible.at[ln.symbol, "cik"])) for ln in sleeve.lines)
    summary = json.loads((run.directory / "summary.json").read_text())
    assert summary["valid"] and summary["policy_commit"] == cs.git(env.repo, "rev-parse", "HEAD")
    assert summary["rank_config_sha256"] == sleeve.rank_config_sha256
    v = sleeve_file.validate(commands.committed(env.state, env.repo).directory,
                             {SLEEVE_FILE: (run.directory / SLEEVE_FILE).read_bytes()}, workdir=env.state / "chk")
    assert v.ok and gate.preflight_errors(v.policy) == []


def test_a_proposal_that_breaks_a_validator_is_rejected_and_the_overlay_fixes_it(tmp_path):
    env = Env(tmp_path, rebased=False)                  # today's 0.95 core + a 0.50 sleeve > 0.95
    run = env.run()
    assert not run.ok and run.commands == [] and any("reference_gross_max" in e for e in run.errors)
    out = run.directory
    assert (out / f"{SLEEVE_FILE}.rejected").exists() and not (out / SLEEVE_FILE).exists()
    assert not (out / "CHANGELOG-snippet.md").exists()
    assert run.report_lines()[0].endswith("REJECTED")
    fixed = env.run(overlay_dir=cs.rebased_overlay(tmp_path))
    assert fixed.ok, fixed.errors
    assert (out / SLEEVE_FILE).exists() and not (out / f"{SLEEVE_FILE}.rejected").exists()
    assert any("overlay" in c for c in fixed.commands)
    with pytest.raises(commands.StocksError, match="not a directory"):
        env.run(overlay_dir=tmp_path / "missing")


# ------------------------------------------------------------------------------------ eligibility


def test_an_ineligible_selected_name_is_replaced_within_its_sector(tmp_path, baseline):
    bad = baseline.result.selected[0]
    env = Env(tmp_path, broker_overrides={sleeve_file.broker_symbol_guess(bad): {"requiresW8Ben": True}})
    run = env.run()
    assert run.ok, run.errors
    ((old, new),) = run.replaced
    sector = run.result.eligible["sector"]
    assert old == bad and sector[new] == sector[bad]
    same_sector = [k for k in run.result.order if sector[k] == sector[bad] and k != bad]
    assert new == next(k for k in same_sector if k not in baseline.result.selected)
    sleeve = env.proposal()
    assert bad not in roles(sleeve) and roles(sleeve)[new] == "selected"
    assert sum(1 for r in roles(sleeve).values() if r == "selected") == 8
    assert run.rejected == {bad: "requires_w8ben"}
    summary = json.loads((run.directory / "summary.json").read_text())          # private: the reason is kept
    assert summary["rejected"] == {bad: "requires_w8ben"} and summary["replaced"] == [[bad, new]]
    doc = (run.directory / f"ranking-{Q}.md").read_text()                      # public: only the count
    assert "stop-loss: 1 (" in doc and "w8ben" not in doc.lower()
    assert "Broker-eligibility replacements: 1" in (run.directory / "CHANGELOG-snippet.md").read_text()
    assert env.broker.eligibility_posts() == 1


def test_no_eligibility_stamps_null_and_live_runs_refuse_the_lines(env):
    env.fakes.read = None
    run = env.run(eligibility=False)
    assert run.ok and any("without the broker gate" in w for w in run.warnings)
    sleeve = env.proposal()
    assert all(ln.eligibility_checked_at is None for ln in sleeve.lines)
    v = sleeve_file.validate(commands.committed(env.state, env.repo).directory,
                             {SLEEVE_FILE: (run.directory / SLEEVE_FILE).read_bytes()}, workdir=env.state / "chk")
    assert v.ok and len(gate.preflight_errors(v.policy)) == 16
    assert gate.preflight_blockers(v.policy) == ["satellite:stock_eligibility_unchecked"]
    assert env.broker.eligibility_posts() == 0
    with pytest.raises(commands.StocksError, match="no READ token"):
        env.run(eligibility=True)


# ------------------------------------------------------------------------------------ diff and pruning


def test_the_diff_retires_held_names_and_prunes_only_flat_untouched_ones(env, baseline):
    keep = baseline.result.selected[0]
    keep_cik = int(baseline.result.eligible.at[keep, "cik"])
    previous = cs.sleeve([
        cs.line(keep, keep_cik), cs.line("OLDH", 7001), cs.line("OLDF", 7002, role="shortlist", rank=9),
        cs.line("RETB", 7003, role="retiring"), cs.line("RETF", 7004, role="retiring"),
    ], retired=[{"cik": sleeve_file.cik10(7005), "symbol": "GONE", "name": "Test Gone", "sector": "Shops",
                 "from": "2025Q4", "to": "2026Q1", "vehicles": ["GONE"]}])
    cs.commit_sleeve(env.repo, previous)
    ids = {s: env.broker.add(s) for s in ("OLDH", "OLDF", "RETB", "RETF")}
    env.broker.hold("OLDH")
    cs.save_instruments(env.state, ids)
    ledger = Ledger(env.state / commands.LEDGER_FILE)
    ledger.migrate()
    ledger.create_decision(decision_id="d-in-flight", kind="rebalance", valid_until=NOW.replace(hour=23),
                           plan={"legs": [{"line": "RETB"}]}, now=NOW)
    run = env.run()
    assert run.ok, run.errors
    d = run.diff
    assert d.selected_out == ("OLDH",) and d.retiring == ("OLDH",) and d.still_retiring == ("RETB",)
    assert d.pruned == ("OLDF", "RETF") and keep not in d.selected_in and keep in run.result.kept
    sleeve = env.proposal()
    assert roles(sleeve)["OLDH"] == roles(sleeve)["RETB"] == "retiring" and "OLDF" not in roles(sleeve)
    registry = {r.symbol: (r.from_, r.to) for r in sleeve.retired}
    assert registry == {"GONE": ("2025Q4", "2026Q1"), "OLDF": ("2026Q2", "2026Q2"), "RETF": ("2026Q2", "2026Q2")}
    lines = "\n".join(run.report_lines())
    assert "retiring: OLDH" in lines and "pruned to the registry: OLDF, RETF" in lines
    assert "1 retiring, 2 pruned" in " ".join(run.commands)


def test_a_failed_portfolio_read_prunes_nothing(env):
    previous = cs.sleeve([cs.line("RETF", 7004, role="retiring")])
    cs.commit_sleeve(env.repo, previous)
    cs.save_instruments(env.state, {"RETF": env.broker.add("RETF")})
    env.broker.fake.inject("GET", "/api/v1/trading/info/real/pnl", 401, times=5)
    run = env.run()
    assert run.ok and run.diff.pruned == () and run.diff.still_retiring == ("RETF",)
    assert any("portfolio read failed" in w for w in run.warnings)


def test_held_names_are_the_committed_selection_matched_by_cik():
    inputs = RankInputs(candidates=(Candidate(key="NEWN", symbol="NEWN", cik=42), Candidate(key="SAME", symbol="SAME",
                                                                                            cik=43)))
    previous = cs.sleeve([cs.line("OLDN", 42), cs.line("SAME", 43), cs.line("GONE", 44),
                          cs.line("SHORT", 45, role="shortlist"), cs.line("RET", 46, role="retiring")])
    assert commands.held_keys(previous, inputs) == ["NEWN", "SAME", "GONE"]
    assert commands.held_keys(None, inputs) == []


# ------------------------------------------------------------------------------------ prefetch


def test_history_is_prefetched_for_new_names_and_gaps_are_flagged(env, baseline):
    old = baseline.result.selected[:3]
    cs.commit_sleeve(env.repo, cs.sleeve([cs.line(k, int(baseline.result.eligible.at[k, "cik"])) for k in old]))
    new_names = [k for k in (*baseline.result.selected, *baseline.result.shortlist) if k not in old]
    short, missing, failed = new_names[0], new_names[1], new_names[2]
    env.fakes.history = {k: cs.bars(260) for k in new_names[3:]} | {short: cs.bars(50)}
    env.fakes.history_flags = [f"history_failed:{failed}"]
    run = env.run(prefetch=True)
    assert run.ok, run.errors
    ((kind, asked),) = [c for c in env.fakes.calls if c[0] == "prefetch"]
    assert sorted(asked) == sorted(new_names) and not set(asked) & set(old)
    assert f"history_short:{short}:50<201" in run.flags and f"history_missing:{missing}" in run.flags
    assert f"history_failed:{failed}" in run.flags and f"history_missing:{failed}" not in run.flags
    assert list(run.result.selected) == list(baseline.result.selected)   # never replaced for data
    assert all(f"flag: {f}" in run.report_lines() for f in run.flags)


def test_a_failing_prefetch_never_fails_the_rank(env):
    def boom(policy, now):
        raise RuntimeError("provider down")

    services = replace(env.fakes.services(), prefetch=boom)
    run = commands.run_rank(D, services, state_dir=env.state, repo=env.repo)
    assert run.ok and run.flags == ["history_prefetch_failed:RuntimeError"]


# ------------------------------------------------------------------------------------ dates, settings, AI list


def test_dates_off_the_rule_anchors_or_in_the_future_are_refused(env):
    with pytest.raises(commands.StocksError, match="not a rule anchor"):
        env.run(asof=date(2026, 8, 18))
    with pytest.raises(commands.StocksError, match="future"):
        env.run(asof=date(2026, 11, 20))
    off = env.run(asof=date(2026, 8, 19), allow_off_anchor=True)
    assert off.ok and off.quarter == "2026Q3"


def test_a_missing_stock_rank_file_is_proposed_with_the_sleeve(tmp_path):
    env = Env(tmp_path, stock_rank=False)
    run = env.run()
    assert run.ok, run.errors
    assert (run.directory / STOCK_RANK_FILE).read_text() == cs.rank_settings_text()
    assert f"cp {run.directory / STOCK_RANK_FILE} policy/{STOCK_RANK_FILE}" in run.commands
    assert any(f"policy/{STOCK_RANK_FILE}" in c for c in run.commands if c.startswith("git add"))
    assert yaml.safe_load(cs.rank_settings_text())["rule"]["cell"] == "SQ-8"


def test_the_ai_list_is_ranked_mechanically_and_must_be_given(tmp_path):
    env = Env(tmp_path, ai_list=False)
    with pytest.raises(commands.StocksError, match="--ai-list"):
        env.run()
    extra = tmp_path / "ai.yaml"
    extra.write_text("tickers: [ON, AIX]\n")                        # unquoted ON stays a ticker
    run = env.run(ai_list=extra)
    assert run.ok, run.errors
    assert ("inputs", (D, ("AIX", "ON"), "SQ-8")) in env.fakes.calls
    proposed = run.directory / commands.AI_LIST_FILE
    assert proposed.exists() and '"ON"' in proposed.read_text()
    proposed_bytes = proposed.read_bytes()
    assert any(c.startswith("cp ") and c.endswith(f"policy/{commands.AI_LIST_FILE}") for c in run.commands)
    committed_env = Env(tmp_path / "second")
    run2 = committed_env.run()
    assert run2.ok and not (run2.directory / commands.AI_LIST_FILE).exists()   # the stale copy is removed
    assert ("inputs", (D, ("AIX", "ON"), "SQ-8")) in committed_env.fakes.calls
    rank_bytes = cs.rank_settings_text().encode()                     # the hash covers the files to commit
    assert run.sleeve.rank_config_sha256 == sleeve_file.rank_config_sha256(
        [(STOCK_RANK_FILE, rank_bytes), (commands.AI_LIST_FILE, proposed_bytes)])
    assert run2.sleeve.rank_config_sha256 == sleeve_file.rank_config_sha256(
        [(STOCK_RANK_FILE, rank_bytes), (commands.AI_LIST_FILE, cs.AI_LIST_TEXT.encode())])


# ------------------------------------------------------------------------------------ public outputs


def test_the_ranking_document_and_the_snippet_are_public_safe(env):
    run = env.run()
    doc = (run.directory / f"ranking-{Q}.md").read_text()
    snippet = (run.directory / "CHANGELOG-snippet.md").read_text()
    for text in (doc, snippet):
        assert_public_safe(text)
        assert scan(text) == []
        assert str(env.broker.ids[run.result.selected[0]]) not in text           # no instrument id
        assert sleeve_file.cik10(int(run.result.eligible.iloc[0]["cik"])) not in text
    for token in ("SQ-8", "did not pass its adoption gate", "recorded override", "CC BY-SA 4.0", "SEC EDGAR"):
        assert token in doc, token
    for token in ("1000000000", "1,000,000,000", "e+0", "revenue_L", "USD"):   # no revenue level, no amount
        assert token not in doc, token
    assert "policy change" in snippet and f"stocks-{Q}" in snippet


def test_an_unsafe_document_writes_nothing_and_echoes_nothing(env, monkeypatch):
    def unsafe(*_a, **_k):
        raise UnsafePublicText("public text contains a currency sign: '$9,999'")

    monkeypatch.setattr(commands, "render_ranking", unsafe)
    with pytest.raises(commands.StocksError) as err:
        env.run()
    assert "9,999" not in str(err.value) and "nothing was written" in str(err.value)
    assert not commands.proposal_dir(env.state, Q).exists()


# ------------------------------------------------------------------------------------ CLI


def _cli(monkeypatch, env: Env, args: list[str]):
    from tests.cli.operator_sim import simulate_operator

    simulate_operator(monkeypatch)       # `stocks rank` with the broker gate is an operator command (M5-B)
    monkeypatch.setattr(commands, "live_rank_services",
                        lambda root, settings, *, eligibility, prefetch=True: env.fakes.services())
    monkeypatch.setattr(commands, "default_repo", lambda: env.repo)
    return CliRunner().invoke(app, ["stocks", "rank", *args])


# SW-5b retargeted `council stocks rank` to the SQ-8 paper benchmark (swing-book.md §6.2): its CLI
# is tested in tests/swing/test_sw5b_cli.py; `commands.run_rank` above keeps its own tests.


def test_the_cli_lists_the_five_commands():
    res = CliRunner().invoke(app, ["stocks", "--help"])
    assert res.exit_code == 0
    for name in ("rank", "onboard", "adopt", "status", "prune"):
        assert name in res.output


def test_the_stock_commands_never_import_the_broker_writer():
    code = ("import sys, council.cli, council.stocks.commands, council.stocks.corporate, council.stocks.report, "
            "council.stocks.eligibility, council.stocks.sleeve_file\n"
            "assert 'council.broker.etoro_write' not in sys.modules, 'writer imported'\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=paths.REPO_ROOT)
    assert done.returncode == 0, done.stderr


def test_source_and_broker_failures_are_refusals_not_crashes(env):
    def down(asof, ai_symbols, config):
        raise ConnectionError("membership source unreachable")

    with pytest.raises(commands.StocksError, match="rank inputs could not be built: ConnectionError"):
        commands.run_rank(D, replace(env.fakes.services(), build_inputs=down), state_dir=env.state, repo=env.repo)
    env.broker.fake.inject("POST", "/api/v2/trading/info/eligibility", 500, times=20)
    with pytest.raises(commands.StocksError, match="eligibility request failed"):
        env.run()
    assert not commands.proposal_dir(env.state, Q).exists()
