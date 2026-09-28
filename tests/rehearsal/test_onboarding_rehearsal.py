"""L1: the automated onboarding rehearsal (m5-readiness §7.2), token day in runbook order.

One sandbox for the whole module; each test is one runbook step and asserts its own result, so a
failure names the step (later steps then fail on their missing precondition). Everything runs
against the loopback FakeEtoro through the production clients, the production keychain module on
the in-memory `security`, the stub model and a local bare remote. No non-loopback socket opens
(`network_guard`)."""

from __future__ import annotations

import json
import os
import re
import stat

import pytest
from typer.testing import CliRunner

from council import cli, paths
from council.operator import guards
from council.operator import keychain as kc
from council.rehearsal import onboarding as ob
from council.rehearsal import scenario
from tests.cli.operator_sim import simulate_operator
from tests.rehearsal.conftest import use_sandbox

pytestmark = pytest.mark.capability_gates          # the real, fail-closed capability load


@pytest.fixture(scope="module")
def box(tmp_path_factory):
    sandbox = ob.Sandbox.create(tmp_path_factory.mktemp("rehearsal-l1"))
    sandbox.start_broker()
    yield sandbox
    sandbox.stop()


@pytest.fixture(autouse=True)
def _active(box, monkeypatch):
    use_sandbox(box, monkeypatch)
    yield


def _no_canary(box: ob.Sandbox, text: str) -> None:
    for canary in box.canaries():
        assert canary not in text, "a canary reached operator output"


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------------- 1. readiness
def test_01_pre_token_token_gates_are_awaiting(box):
    from council.operator import readiness

    report = readiness.evaluate(ob.sandbox_probes(box), post_token=False)
    token = [r for r in report.results if r.gate.owner == "token" and r.gate.id.startswith("K")]
    assert token and all(r.result.state == "wait" for r in token), \
        {r.gate.id: r.result.state for r in token if r.result.state != "wait"}
    lc1 = next(r for r in report.results if r.gate.id == "LC1").result
    on, _licensed = ob.feed_state(box)
    # the feed switch is on (user decision): LC1 stays red until `ops attest etoro-licence`
    assert (lc1.state, lc1.code) == (("red", "feed_on_unattested") if on else ("amber", "feed_off"))


# ---------------------------------------------------------------------------------- 2. keys
def test_02_keys_through_the_cli_in_the_simulated_operator_terminal(box, monkeypatch):
    simulate_operator(monkeypatch)
    box.activate(monkeypatch.setenv)
    answers = iter([box.tokens.app, box.tokens.read, box.tokens.write])
    monkeypatch.setitem(kc.store_token_interactive.__kwdefaults__, "getpass_fn", lambda _p: next(answers))
    for argv in (["keys", "init-write-keychain"], ["keys", "store-read"], ["keys", "store-write"]):
        result = CliRunner().invoke(cli.app, argv)
        assert result.exit_code == 0, (argv, result.output, result.exception)   # store-read never raises (G25)
        _no_canary(box, result.output)
    sec = box.security
    assert not any(t in sec.argv_text() for t in box.tokens.all()), "a token reached argv"
    write_kc = str(kc.write_keychain_path().resolve())
    assert sec.services(write_kc) == {kc.WRITE_SERVICE}                  # WRITE alone, READ absent
    assert sec.services(box.keychain_file) == {kc.API_KEY_SERVICE, kc.READ_SERVICE}
    assert write_kc not in sec.search_list                               # off the search list
    assert sec.items[sec.login] == {}                                    # the login keychain untouched


# ---------------------------------------------------------------------------------- 3-7. onboarding
def test_03_keys_verify_writes_private_records(box):
    result = ob.step_keys_verify(box)
    assert result.ok, result.data["lines"]
    assert result.data["gates"]["K1"]["state"] == "green"
    assert result.data["gates"]["K2"]["state"] in ("green", "amber")      # scopes: API-exposed or attested
    assert result.data["gates"]["K3"]["state"] == "green"                 # expiry >= 30 days
    for rel in (("readiness", "keys.json"), ("account", "onboarded.json")):
        path = box.state_dir.joinpath(*rel)
        assert path.is_file() and _mode(path) == 0o600
        assert paths.REPO_ROOT not in path.resolve().parents
    _no_canary(box, "\n".join(result.data["lines"]))


def test_04_set_mirror_from_the_broker_uses_the_canary_funding(box):
    result = ob.step_set_mirror(box)
    assert result.ok
    assert result.data["ratio"] == pytest.approx(scenario.CANARY_FUNDING_USD / scenario.VIRTUAL_BALANCE)


def test_05_live_read_reads_the_feed_only_once_licensed(box):
    before = box.feed_requests()
    on, licensed = ob.feed_state(box)
    assert not licensed and before == 0
    attest = ob.step_attest_licence(box)                                  # scripted `ops attest etoro-licence`
    assert attest.ok
    on, licensed = ob.feed_state(box)
    result = ob.step_live_read(box)
    gates = result.data["gates"]
    assert result.ok, [ln for ln in result.data["lines"] if ln.startswith("red")]
    assert result.data["feed"] == (1 if licensed else 0)                  # take=1, body discarded
    assert gates["K8"]["state"] == "green" if licensed else gates["K8"]["code"].startswith("skipped")
    for gate in ("K5", "K6", "K7", "K9"):
        assert gates[gate]["state"] == "green", (gate, gates[gate])
    _no_canary(box, "\n".join(result.data["lines"]))


def test_06_instruments_resolve_reports_every_designed_hole(box):
    result = ob.step_instruments(box)
    text = "\n".join(result.data["lines"])
    mapping = json.loads((box.state_dir / "instruments.json").read_text())
    assert _mode(box.state_dir / "instruments.json") == 0o600
    assert re.search(r"NDX\s+CNDX\.L", text), text                        # EQQQ.L absent -> CNDX.L
    assert "GBP/GBX" in text                                              # the GBX unit recorded
    assert f"{scenario.AMBIGUOUS}: ambiguous" in text
    assert result.data["gates"]["K11"]["code"].startswith("ambiguous_symbols")
    assert "P2" in result.data["gates"] and "K19" in result.data["gates"]
    assert scenario.ABSENT not in json.dumps(mapping)


def test_07_record_fixtures_stay_private_under_licensed(box):
    result = ob.step_record_fixtures(box)
    assert result.ok and result.data.get("gates", {}).get("K13", {}).get("state") == "green"
    fixtures = box.state_dir / "licensed" / "fixtures"
    files = [p for p in fixtures.rglob("*") if p.is_file()]
    assert files and all(paths.REPO_ROOT not in p.resolve().parents for p in files)
    assert all(_mode(p) == 0o600 for p in files)


# ---------------------------------------------------------------------------------- 8. install
def test_08_install_renders_the_release_plists_without_a_secret_and_loads_nothing(box, tmp_path):
    """`ops/install.sh <tag>` under a tmp HOME with fake git/launchctl/plutil/uv and a pty answer
    (the M5-F harness): rendered with the release path and the runner role, carrying no canary."""
    from tests.boundaries import test_install_script as ins

    home, fakebin = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    fakebin.mkdir()
    for name, body in (("git", ins.FAKE_GIT), ("launchctl", ins.FAKE_LAUNCHCTL), ("plutil", ins.FAKE_PLUTIL),
                       ("uv", ins.FAKE_UV)):
        ins._script(fakebin / name, body)
    run = ins._run((home, fakebin, tmp_path / "calls.log"), ins.INSTALL, ins.TAG)
    assert run.rc == 0, run.err
    assert not run.called("launchctl", "bootstrap") and not run.called("launchctl", "enable")
    agents = home / "Library" / "LaunchAgents"
    for label in ins.LIVE:
        text = (agents / f"{label}.plist").read_text()
        _no_canary(box, text)
        assert "COUNCIL_KEYCHAIN_FILE" not in text and "COUNCIL_ETORO_BASE_URL" not in text
        assert str(box.state_dir) not in text                             # the sandbox never leaks in
        plist = ins._plist(run, label)
        assert plist["EnvironmentVariables"]["COUNCIL_ROLE"] == "runner"
        assert str(run.state / "releases" / "current") in " ".join(plist["ProgramArguments"])


# ---------------------------------------------------------------------------------- 9. smoke
def test_09_smoke_tickets_s1_to_s6(box):
    from council.operator import capabilities, smoke
    from council.publish import smoke_row

    result = ob.step_smoke(box)
    ids = result.data["ids"]
    assert not box.fake.positions, "every smoke position was closed again"
    caps = capabilities.read_file(box.state_dir)["capabilities"]
    assert {c: caps[c]["step"] for c in caps} == {
        "real_etf": "S1", "sl_modify": "S2", "partial_close": "S3", "crypto_real": "S5", "cfd_short": "S6"}
    rows = [json.loads(line) for line in (box.clone / smoke_row.SMOKE_PATH).read_text().splitlines()]
    assert {r["id"] for r in rows} == set(ids.values())
    assert all(set(r) == {"id", "type", "step", "state", "commitment"} for r in rows)   # weightless
    assert not (box.clone / "journal" / "status.json").exists()
    assert not (box.clone / "journal" / "book").exists()
    # one smoke decision at a time: a second propose while one is pending is refused
    out: list[str] = []
    first = smoke.propose("S1", box.smoke_deps(out))
    with pytest.raises(smoke.SmokeRefused, match="decision_pending"):
        smoke.propose("S5", box.smoke_deps(out))
    box.ledger.transition(first.decision_id, "rejected", "rehearsal: withdrawn", actor="operator")
    _no_canary(box, "\n".join(result.data["out"]))


# ---------------------------------------------------------------------------------- 10-13. first cycle
def test_10_first_live_cycle_is_sealed_and_published(box):
    result = ob.step_first_cycle(box)
    out = result.data["outcome"]
    assert result.ok, (out.decision_state, out.flags, result.data["feed"])
    assert result.data["feed"] <= 1                                        # one feed request per slot
    assert box.public_news.calls >= 1                                      # the P: items path ran
    status = json.loads((box.clone / "journal" / "status.json").read_text())
    assert status["state"] == "LIVE"
    from council.publish import journal

    assert not (box.clone / journal.cycle_path(out.cycle_id)).exists(), "nothing revealed before execution"
    assert (box.clone / journal.commitment_path(out.cycle_id)).exists(), "the sealed commitment is public"


def test_11_approve_uses_the_real_guard_and_the_typed_nonce(box):
    # the same guard on this (agent / CI) process's real environment must fail
    with pytest.raises(guards.GuardError):
        guards.assert_operator_context(env=dict(os.environ), stdin_isatty=False, stdout_isatty=False,
                                       ancestors=guards.process_ancestors())
    decision_id = box.results[-1].data["outcome"].decision_id
    result = ob.step_approve(box, decision_id)
    assert result.ok, (result.detail, result.data["report"].reasons)
    assert all(p.sl_rate for p in box.fake.positions.values()), "every position carries a stop-loss"
    assert any(line.startswith("prompt: ") and "Type " in line for line in result.data["out"])


def test_13_watch_reveals_the_cycle_and_publishes_the_execution(box):
    cycle = next(r for r in box.results if r.step == "first live cycle").data["outcome"]
    result = ob.step_watch(box)
    from council.publish import journal

    assert cycle.cycle_id in result.data["watch"].revealed
    assert (box.clone / journal.cycle_path(cycle.cycle_id)).exists()
    assert (box.clone / journal.execution_path(cycle.cycle_id)).exists(), result.data["watch"]
    remote_log = ob._git("log", "--oneline", "main", cwd=box.remote)
    assert len(remote_log.splitlines()) > 3                                # pushed to the local remote only


# ---------------------------------------------------------------------------------- 14. leak scan
def test_14_no_canary_in_any_public_file_or_log(box):
    result = ob.step_leak_scan(box, extra_text=[n[1] for n in box.notifier.sent])
    assert result.ok, result.data["findings"]


# ---------------------------------------------------------------------------------- 15. transparency
def test_15_why_and_inputs_explain_the_cycle_in_the_operator_terminal(box, monkeypatch):
    cycle = next(r for r in box.results if r.step == "first live cycle").data["outcome"]
    simulate_operator(monkeypatch)
    why = CliRunner().invoke(cli.app, ["show", cycle.cycle_id, "--why", "--source", "journal",
                                       "--journal", str(box.clone / "journal")])
    assert why.exit_code == 0, why.output
    assert why.output.strip()
    _no_canary(box, why.output)
    inputs = CliRunner().invoke(cli.app, ["inputs", "show", cycle.cycle_id, "--role", "news", "--reading"])
    assert inputs.exit_code == 0, inputs.output
    for title in scenario.PUBLIC_TITLES:                                   # the P: items the news role read
        assert title in inputs.output


# ---------------------------------------------------------------------------------- 16. purge
def test_16_purge_dry_run_lists_then_the_purge_leaves_no_licensed_file(box):
    from council.operator import purge

    dry = purge.purge_licensed(box.state_dir, now=box.clock.now(), purge_all=True, dry_run=True)
    assert not dry.errors
    assert (box.state_dir / "licensed").exists()                          # a dry run deletes nothing
    real = purge.purge_licensed(box.state_dir, now=box.clock.now(), purge_all=True)
    assert not real.errors, real.errors
    licensed = box.state_dir / "licensed"
    assert not licensed.exists() or not [p for p in licensed.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------------- 17. post-token
def test_17_post_token_records_are_green(box):
    from council.operator import readiness

    report = readiness.evaluate(ob.sandbox_probes(box), post_token=True)
    by_id = {r.gate.id: r.result for r in report.results}
    for gate in ("K1", "K3", "K5", "K6", "K7", "K9", "K12", "K13"):
        assert by_id[gate].state == "green", (gate, by_id[gate])
    assert by_id["LC1"].state == "green"                                  # feed on, licence attested
