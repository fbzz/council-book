"""`council doctor --ready` (m5-readiness §6, M5-H): a red and a green case for every gate, the
exit codes, the JSON schema, "not built", CI read only with --network, no `security -w`, no broker
or LLM import, and the readiness records' writer rules."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from council import paths
from council.operator import readiness as rd
from council.operator.readiness import (
    ACCEPTANCE,
    ATTEST_ITEMS,
    BASE_URL_ENV,
    HEALTHCHECK_ITEM,
    INVALID,
    KEYCHAIN_CORE,
    KEYCHAIN_OPTIONAL,
    KEYCHAIN_STOCKS,
    NTFY_ITEM,
    RECORDS,
    REQUIRED_OPERATOR,
    RUNTIME_BACKUP,
    RUNTIME_HEALTHCHECK,
    WRITE_SERVICE,
    CheckRun,
    Completed,
    LedgerSummary,
    PlistInfo,
    Probes,
    PublisherInfo,
    ReadinessError,
    ReleaseInfo,
    WriteKeychainInfo,
    evaluate,
    gates_for,
    green,
    red,
    validate_record,
    write_record,
)

HEAD = "a" * 40
OLD = "b" * 40
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ISO = NOW.isoformat()
ALL_GATE_IDS = [g.id for g in gates_for("stocks")]


def _record(gate_ids, *, head: str = HEAD, attested=()) -> dict[str, Any]:
    data: dict[str, Any] = {"at": ISO, "head": head,
                            "gates": {g: {"state": "green", "code": "ok"} for g in gate_ids}}
    if attested:
        data["attested"] = {item: {"value": True, "at": ISO} for item in attested}
    return data


@dataclass
class World:
    """Everything the fake probes answer; the defaults are a machine that is READY (Track S,
    post-token, --network)."""

    home: Path
    state: Path
    missing: set[str] = field(default_factory=set)
    release: ReleaseInfo = field(default_factory=lambda: ReleaseInfo(True, True, False, HEAD, ("council-spec-v1",), HEAD))
    origin_tag: str | None = HEAD
    head: str = HEAD
    ci: list[CheckRun] | None = field(default_factory=lambda: [
        CheckRun("test", "completed", "success"), CheckRun("m5-acceptance", "completed", "success")])
    policy: rd.Result = field(default_factory=lambda: green("policy_prompts_ok"))
    uses: list[str] = field(default_factory=lambda: ["actions/checkout@" + "c" * 40, "./local-action"])
    env: dict[str, str] = field(default_factory=dict)
    plists: list[PlistInfo] | None = None
    keychain: set[str] = field(default_factory=lambda: {*KEYCHAIN_CORE, *KEYCHAIN_STOCKS, *KEYCHAIN_OPTIONAL,
                                                        NTFY_ITEM, HEALTHCHECK_ITEM})
    write_kc: WriteKeychainInfo = field(default_factory=lambda: WriteKeychainInfo(True, False, 60))
    rules: dict[str, str | None] | None = None
    registry: tuple[set[str], dict[str, bool]] = field(default_factory=lambda: (
        {"approve", "inbox"}, {"approve": True, "inbox": False}))
    deny: tuple[list[str] | None, list[str] | None] = (["Bash(x:*)"], ["Bash(x:*)", "Bash(y:*)"])
    scripts: dict[str, str | None] = field(default_factory=lambda: {
        rel: "council ops assert-operator\nread -r answer </dev/tty\n" for rel in rd.GUARDED_SCRIPTS})
    disk: float = 50.0
    sleep: int | None = 0
    ollama: tuple[str, bool | None] = ("deepseek-v4.1-flash:cloud", True)
    trips: list[datetime] = field(default_factory=list)
    publisher: PublisherInfo = field(default_factory=lambda: PublisherInfo(
        True, True, "git@github.com:fbzz/council-book.git", True, True))
    snapshot: rd.Result = field(default_factory=lambda: green("head_snapshot_ok"))
    ledger: LedgerSummary = field(default_factory=lambda: LedgerSummary(True, True, 3, True, 2, 1))
    loaded: set[str] = field(default_factory=set)
    runtime: dict[str, dict] = field(default_factory=lambda: {
        RUNTIME_BACKUP: {"at": ISO, "licensed_free": True}, RUNTIME_HEALTHCHECK: {"at": ISO, "ok": True}})
    records: dict[str, Any] = field(default_factory=lambda: {
        "soak": _record(["O3", "T1", "T2", "T3"]),
        "dress": _record(["O5"]),
        "keys": _record(RECORDS["keys"].gates),
        "live-read": _record(RECORDS["live-read"].gates),
        "smoke": _record(RECORDS["smoke"].gates),
        "attest": _record(["K15"], attested=ATTEST_ITEMS),
        "stocks": _record(["S4"]),
    })
    feed_on: bool = True
    stock_tags: list[str] = field(default_factory=lambda: ["stocks-2026q4"])
    sleeve_live: bool = True

    def __post_init__(self) -> None:
        release_dir = self.state / "releases" / "current"
        if self.plists is None:
            self.plists = [PlistInfo("com.fbzz.council.cycle.plist",
                                     {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "COUNCIL_ROLE": "runner",
                                      "COUNCIL_MODE": "live", "COUNCIL_AGENT_CONTEXT": "1"},
                                     ("/usr/bin/caffeinate", "-i", f"{release_dir}/.venv/bin/council", "cycle"),
                                     str(release_dir))]
        if self.rules is None:
            text = " ".join(rd.AGENT_RULE_TOKENS)
            self.rules = {name: text for name in rd.AGENT_RULE_FILES}


class FakeProbes(Probes):
    def __init__(self, world: World, *, network: bool = True) -> None:
        super().__init__(state_dir=world.state, repo=world.home / "repo", home=world.home, env=world.env,
                         now=NOW, network=network, run=self._refuse_run)
        self.w = world

    @staticmethod
    def _refuse_run(argv, **_):
        raise AssertionError(f"the fake probes never run a subprocess: {argv}")

    def landed(self, wp):                      return wp not in self.w.missing
    def release_state(self):                   return self.w.release
    def origin_tag_commit(self, tag):          return self.w.origin_tag
    def head(self):                            return self.w.head
    def ci_runs(self, sha):                    return self.w.ci
    def policy_prompts(self):                  return self.w.policy
    def workflow_uses(self):                   return self.w.uses
    def plists(self):                          return self.w.plists
    def security_has(self, service):           return service in self.w.keychain
    def write_keychain(self):                  return self.w.write_kc
    def agent_rules(self):                     return self.w.rules
    def operator_registry(self):               return self.w.registry
    def deny_rules(self):                      return self.w.deny
    def scripts(self):                         return self.w.scripts
    def disk_free_gib(self):                   return self.w.disk
    def ac_sleep(self):                        return self.w.sleep
    def ollama_model(self):                    return self.w.ollama
    def rate_limit_trips(self, since):         return [t for t in self.w.trips if t >= since]
    def publisher(self):                       return self.w.publisher
    def head_snapshot(self):                   return self.w.snapshot
    def ledger_summary(self):                  return self.w.ledger
    def launchd_loaded(self):                  return self.w.loaded
    def runtime(self, key):                    return self.w.runtime.get(key)
    def broker_feed_on(self):                  return self.w.feed_on
    def release_tags(self, pattern):           return self.w.stock_tags
    def release_sleeve_live(self):             return self.w.sleeve_live

    def record(self, name):
        data = self.w.records.get(name)
        if data is None:
            return None
        try:
            validate_record(name, data)
        except ReadinessError:
            return INVALID
        return data


@pytest.fixture
def world(tmp_path: Path) -> World:
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    return World(home=tmp_path / "home", state=tmp_path / "state")


def _run(world: World, *, track: str = "stocks", post_token: bool = True, network: bool = True) -> rd.Report:
    return evaluate(FakeProbes(world, network=network), track=track, post_token=post_token)


def _by_id(report: rd.Report) -> dict[str, rd.GateResult]:
    return {r.gate.id: r for r in report.results}


# --------------------------------------------------------------------------- green and red per gate
def test_the_registry_has_every_section_6_gate_with_an_owner():
    core = {g.id for g in gates_for("core")}
    expected = ({f"R{i}" for i in range(1, 7)} | {f"B{i}" for i in range(1, 10)} | {f"E{i}" for i in range(1, 9)}
                | {f"O{i}" for i in range(1, 6)} | {"LC1", "LC2", "LC3", "P1", "P2", "T1", "T2", "T3"}
                | {f"K{i}" for i in range(1, 22)})
    assert core == expected
    assert set(ALL_GATE_IDS) == expected | {f"S{i}" for i in range(1, 7)}
    owners = {g.id: g.owner for g in gates_for("stocks")}
    assert owners["R1"] == owners["B8"] == owners["E1"] == "user"
    assert owners["R6"] == owners["B6"] == owners["LC2"] == owners["T1"] == "agent"
    assert all(owners[f"K{i}"] == "token" for i in range(1, 22)) and owners["O4"] == owners["P2"] == "token"


@pytest.mark.parametrize("gate_id", ALL_GATE_IDS)
def test_every_gate_is_green_on_a_ready_machine(world, gate_id):
    result = _by_id(_run(world))[gate_id].result
    assert result.state == "green", (gate_id, result)


def test_a_ready_machine_exits_0(world):
    report = _run(world)
    assert report.exit_code == 0 and report.ready
    assert report.lines()[0].startswith("READY (Track S, post-token): 0 red")


def _soak(state: str, gate: str):
    def mutate(w: World):
        w.records["soak"]["gates"][gate] = {"state": state, "code": "below_threshold"}
    return mutate


def _gate_record(name: str, gate: str):
    def mutate(w: World):
        w.records[name]["gates"][gate] = {"state": "red", "code": "check_failed"}
    return mutate


def _unattest(item: str):
    def mutate(w: World):
        del w.records["attest"]["attested"][item]
    return mutate


def _set(**changes):
    def mutate(w: World):
        for key, value in changes.items():
            setattr(w, key, value)
    return mutate


def _marker(w: World):
    real = w.home / "Library" / "Application Support" / "council-book"
    real.mkdir(parents=True)
    (real / "REHEARSAL").write_text("")


def _bad_plist(w: World):
    w.plists[0].env["COUNCIL_ETORO_TOKEN"] = "x"


def _stale_backup(w: World):
    w.runtime[RUNTIME_BACKUP] = {"at": (NOW - timedelta(hours=30)).isoformat(), "licensed_free": True}


def _failing_pings(w: World):
    w.loaded = {"com.fbzz.council.watch"}
    w.runtime[RUNTIME_HEALTHCHECK] = {"at": ISO, "ok": False}


def _no_keychain(*items):
    def mutate(w: World):
        w.keychain -= set(items)
    return mutate


FAILED_ACCEPTANCE = [CheckRun("test", "completed", "success"), CheckRun("m5-acceptance", "completed", "failure")]

# gate -> (mutation, expected state, expected code, post_token). "amber!" = an amber that is NOT accepted
# (E2 has no red in §6: its failing state is an unaccepted amber, which also exits 1).
FAILING: dict[str, tuple[Any, str, str, bool]] = {
    "R1": (_set(release=ReleaseInfo(True, True, True, HEAD, ("council-spec-v1",), HEAD)), "red", "release_dirty", True),
    "R2": (_set(ci=[CheckRun("test", "completed", "failure")]), "red", "ci_failed", True),
    "R3": (_set(ci=FAILED_ACCEPTANCE), "red", "ci_failed", True),
    "R4": (_set(policy=red("policy_invariant_failed")), "red", "policy_invariant_failed", True),
    "R5": (_set(uses=["actions/checkout@v4"]), "red", "action_not_pinned", True),
    "R6": (_set(missing={"M5-J"}), "red", "not_built", True),
    "B1": (_set(env={BASE_URL_ENV: "https://example.invalid"}), "red", "base_url_in_shell", True),
    "B2": (lambda w: w.keychain.add(WRITE_SERVICE), "red", "write_token_on_search_list", True),
    "B3": (_bad_plist, "red", "plist_bad", True),
    "B4": (_marker, "red", "rehearsal_marker_in_state_dir", True),
    "B5": (_set(rules={"CLAUDE.md": "never approve", "AGENTS.md": None}), "red", "agent_rules_incomplete", True),
    "B6": (_set(registry=({"approve"}, {})), "red", "operator_command_unguarded", True),
    "B7": (_set(env={"HTTPS_PROXY": "http://proxy.invalid"}), "red", "proxy_or_cert_env", True),
    "B8": (_set(deny=(["Bash(x:*)", "Bash(z:*)"], ["Bash(x:*)"])), "red", "deny_rules_missing", True),
    "B9": (_set(scripts={rel: None for rel in rd.GUARDED_SCRIPTS}), "red", "script_unguarded", True),
    "E1": (_set(disk=1.5), "red", "disk_full", True),
    "E2": (lambda w: (setattr(w, "sleep", 10), _unattest("power-ok")(w)), "amber!", "ac_sleep_on", True),
    "E3": (_set(ollama=("deepseek-v4.1-flash:cloud", False)), "red", "model_not_listed", True),
    "E4": (_no_keychain("council-book.tiingo"), "red", "keychain_missing", True),
    "E5": (_set(trips=[NOW - timedelta(days=1)]), "red", "rate_limit_trip_7d", True),
    "E6": (_set(publisher=PublisherInfo(True, False, "git@github.com:fbzz/council-book.git", True, True)),
           "red", "publisher_dirty", True),
    "E7": (_set(snapshot=red("head_snapshot_failed")), "red", "head_snapshot_failed", True),
    "E8": (_set(ledger=LedgerSummary(True, True, 0, True, 0, 0)), "red", "ledger_has_nav_state", False),
    "O1": (_no_keychain(NTFY_ITEM), "red", "ntfy_absent", True),
    "O2": (_failing_pings, "red", "pings_failing", True),
    "O3": (_soak("red", "O3"), "red", "below_threshold", True),
    "O4": (_stale_backup, "red", "backup_stale", True),
    "O5": (lambda w: w.records.pop("dress"), "red", "dress_missing", True),
    "LC1": (_unattest("etoro-licence"), "red", "feed_on_unattested", True),
    "LC2": (_set(ci=FAILED_ACCEPTANCE), "red", "ci_failed", True),
    "LC3": (_set(ci=FAILED_ACCEPTANCE), "red", "ci_failed", True),
    "P1": (_set(ci=FAILED_ACCEPTANCE), "red", "ci_failed", True),
    "P2": (_gate_record("live-read", "P2"), "red", "check_failed", True),
    "T1": (_soak("red", "T1"), "red", "below_threshold", True),
    "T2": (_soak("red", "T2"), "red", "below_threshold", True),
    "T3": (_soak("red", "T3"), "red", "below_threshold", True),
    **{f"K{i}": (_gate_record("keys", f"K{i}"), "red", "check_failed", True) for i in range(1, 5)},
    **{g: (_gate_record("live-read", g), "red", "check_failed", True)
       for g in ("K5", "K6", "K7", "K8", "K9", "K10", "K11", "K12", "K13", "K17", "K19")},
    "K14": (_gate_record("smoke", "K14"), "red", "check_failed", True),
    "K15": (_gate_record("attest", "K15"), "red", "check_failed", True),
    "K16": (_unattest("copy-stop-loss"), "red", "unattested:copy-stop-loss", True),
    "K18": (_set(ledger=LedgerSummary(True, True, 2, True, 1, 0)), "red", "no_live_cycle_published", True),
    "K20": (_gate_record("smoke", "K20"), "red", "check_failed", True),
    "K21": (_unattest("terms-version"), "red", "unattested:terms-version", True),
    "S1": (_set(missing={"WP-K"}), "red", "not_built", True),
    "S2": (_no_keychain("council-book.alpaca-secret"), "red", "alpaca_keys_missing", True),
    "S3": (_gate_record("smoke", "S3"), "red", "check_failed", True),
    "S4": (_set(stock_tags=[]), "red", "sleeve_untagged", True),
    "S5": (_set(sleeve_live=False), "red", "sleeve_not_live", True),
    "S6": (_unattest("w8ben-na"), "red", "unattested:w8ben-na", True),
}


def test_every_gate_has_a_failing_case():
    assert set(FAILING) == set(ALL_GATE_IDS)


@pytest.mark.parametrize("gate_id", ALL_GATE_IDS)
def test_every_gate_fails_on_its_failing_case(world, gate_id):
    mutate, state, code, post_token = FAILING[gate_id]
    mutate(world)
    report = _run(world, post_token=post_token)
    got = _by_id(report)[gate_id]
    shown = "amber!" if got.unaccepted else got.result.state
    assert (shown, got.result.code) == (state, code), got.result
    assert report.exit_code == 1


AMBER: list[tuple[str, Any, str, bool]] = [
    ("E1", _set(disk=5.0), "disk_low", True),
    ("E2", lambda w: (setattr(w, "sleep", 10)), "power_ok_attested", True),       # power-ok is attested
    ("E2", lambda w: (setattr(w, "sleep", 10), _unattest("power-ok")(w)), "ac_sleep_on", False),
    ("E5", _unattest("tiingo-dedicated"), "tiingo_unattested", True),
    ("E5", _set(trips=[NOW - timedelta(days=20)]), "rate_limit_trip_30d", True),
    ("E6", _set(publisher=PublisherInfo(True, True, "https://github.com/fbzz/council-book.git", False, True)),
     "publisher_https", True),
    ("B2", _set(write_kc=WriteKeychainInfo(False)), "write_keychain_absent", True),
    ("B3", _set(plists=[]), "plists_not_rendered", True),
    ("O2", _no_keychain(HEALTHCHECK_ITEM), "healthcheck_absent", True),
    ("O3", lambda w: w.records["soak"].update(head=OLD), "soak_older_release", True),
    ("O5", lambda w: w.records["dress"].update(head=OLD), "dress_older_commit", True),
    ("LC1", _set(feed_on=False), "feed_off", True),
    ("P2", lambda w: w.records["live-read"]["gates"].update(P2={"state": "amber", "code": "size_floor_some_lines"}),
     "size_floor_some_lines", True),
]


@pytest.mark.parametrize("gate_id, mutate, code, accepted", AMBER)
def test_amber_states_and_whether_they_are_accepted(world, gate_id, mutate, code, accepted):
    mutate(world)
    report = _run(world)
    got = _by_id(report)[gate_id].result
    assert (got.state, got.code, got.accepted) == ("amber", code, accepted)
    assert report.exit_code == (0 if accepted else 1)


def test_b4_ignores_the_rehearsal_directory_of_cycle_rehearsal(world):
    """`state_dir/rehearsal/` answers to REHEARSAL on a case-insensitive volume: only a FILE is the marker."""
    real = world.home / "Library" / "Application Support" / "council-book"
    (real / "rehearsal").mkdir(parents=True)
    assert _by_id(_run(world))["B4"].result.code == "no_marker"


def test_e2_green_without_attestation_when_ac_sleep_is_off(world):
    _unattest("power-ok")(world)
    assert _by_id(_run(world))["E2"].result.code == "ac_sleep_off"


def test_e4_core_track_does_not_need_the_alpaca_pair_and_fred_is_info(world):
    _no_keychain(*KEYCHAIN_STOCKS, *KEYCHAIN_OPTIONAL)(world)
    core = _by_id(_run(world, track="core"))["E4"].result
    assert core.state == "green" and "council-book.fred absent (optional)" in core.detail
    assert _by_id(_run(world, track="stocks"))["E4"].result.code == "keychain_missing"


def test_b1_allows_a_base_url_inside_a_marked_sandbox(world):
    world.env = {BASE_URL_ENV: "http://127.0.0.1:9"}
    (world.state / "REHEARSAL").write_text("")
    assert _by_id(_run(world))["B1"].result.code == "sandbox"


# ---------------------------------------------------------------------- before the token, exit codes
def _pre_token_world(world: World) -> World:
    for name in ("keys", "live-read", "smoke"):
        world.records.pop(name)
    world.records["attest"] = _record([], attested=("power-ok", "ntfy-received", "tiingo-dedicated", "etoro-licence"))
    world.runtime = {}
    world.ledger = LedgerSummary(False)
    return world


def test_without_the_token_the_token_gates_wait_and_the_report_can_be_ready(world):
    report = _run(_pre_token_world(world), track="core", post_token=False)
    by_id = _by_id(report)
    token = [g.id for g in gates_for("core") if g.owner == "token"]
    assert token and all(by_id[g].result.state == "wait" for g in token)
    assert all(by_id[g].result.code == "awaiting_token" for g in token)
    assert "council-op doctor --live-read" in by_id["K7"].result.detail
    assert by_id["E8"].result.code == "ledger_pristine"
    assert report.exit_code == 0
    assert report.lines()[0] == f"READY (Track C): 0 red, 0 amber, {len(token)} awaiting token, " \
                                f"{len(report.results) - len(token)} green"
    assert any(line.split()[:3] == ["K7", "wait", "token"] for line in report.lines())


def test_with_post_token_a_missing_record_is_red(world):
    report = _run(_pre_token_world(world), track="core", post_token=True)
    assert _by_id(report)["K7"].result.code == "no_record:live-read"
    assert _by_id(report)["O4"].result.code == "backup_missing"
    assert report.exit_code == 1


def test_an_invalid_record_is_red_never_trusted(world):
    world.records["keys"]["gates"]["K1"] = {"state": "green", "code": "balance 1234.50"}
    assert _by_id(_run(world))["K1"].result.code == "record_invalid:keys"


def test_a_gate_that_raises_is_an_internal_error_exit_2(world, monkeypatch):
    def boom(self):
        raise KeyError("x")
    monkeypatch.setattr(FakeProbes, "ac_sleep", boom)
    report = _run(world)
    got = _by_id(report)["E2"].result
    assert (got.state, got.code, got.detail) == ("red", "internal_error", "KeyError")
    assert report.exit_code == 2


def test_run_ready_returns_2_when_evaluation_itself_fails(world, monkeypatch):
    monkeypatch.setattr(rd, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out: list[str] = []
    assert rd.run_ready(probes=FakeProbes(world), echo=out.append) == 2
    assert out == ["internal error: RuntimeError"]


# ------------------------------------------------------------------------------- not built, CI, JSON
def test_an_unbuilt_package_shows_not_built_on_every_gate_that_needs_it(world):
    world.missing = {"M5-C"}
    report = _run(world, post_token=False)
    by_id = _by_id(report)
    for gate in ("K1", "K7", "P2"):
        assert (by_id[gate].result.state, by_id[gate].result.detail) == ("red", "not built: M5-C")
    assert by_id["R6"].result.detail == "not built: M5-C (onboarding commands)"
    assert report.summary()["not_built"] == ["M5-C"]
    assert report.exit_code == 1


def test_track_s_packages_are_not_required_on_track_c(world):
    world.missing = {"M5-L", "WP-J"}
    assert _by_id(_run(world, track="core"))["R6"].result.state == "green"
    stocks = _by_id(_run(world, track="stocks"))
    assert stocks["R6"].result.detail == "not built: M5-L (Track S rehearsal variant)"
    assert stocks["S1"].result.detail.startswith("not built: WP-J")


@pytest.mark.parametrize("gate_id", ["R2", "R3", "R6", "LC2", "LC3", "P1"])
def test_ci_gates_are_accepted_amber_without_network_and_read_ci_with_it(world, gate_id):
    world.ci = None                                    # would be unreadable, but is never asked for
    offline = _by_id(_run(world, network=False))[gate_id].result
    assert (offline.state, offline.code, offline.accepted) == ("amber", "ci_unknown", True)
    online = _by_id(_run(world, network=True))[gate_id].result
    assert (online.state, online.code, online.accepted) == ("amber", "ci_unreachable", False)


def test_r1_rechecks_origin_only_with_network(world):
    world.origin_tag = OLD
    assert _by_id(_run(world, network=False))["R1"].result.code == "release_ok"
    assert _by_id(_run(world, network=True))["R1"].result.code == "tag_not_on_origin"


def test_ci_pending_and_absent_jobs(world):
    world.ci = [CheckRun("m5-acceptance", "in_progress", None)]
    got = _by_id(_run(world))["R6"]
    assert got.result.code == "ci_pending" and got.unaccepted
    world.ci = [CheckRun("test", "completed", "success")]
    assert _by_id(_run(world))["R3"].result.code == "ci_job_absent"


def test_json_schema_is_stable_and_carries_the_owner(world):
    out: list[str] = []
    code = rd.run_ready(probes=FakeProbes(world), track="stocks", post_token=True, as_json=True, echo=out.append)
    data = json.loads("".join(out))
    assert code == 0 and data["exit"] == 0 and data["ready"] is True
    assert set(data) == {"schema", "ready", "exit", "track", "post_token", "network", "checkout", "summary", "gates"}
    assert data["schema"] == 1
    assert set(data["summary"]) == {"red", "red_by_owner", "amber", "amber_unaccepted", "wait", "green", "not_built"}
    assert [g["id"] for g in data["gates"]] == ALL_GATE_IDS
    for gate in data["gates"]:
        assert set(gate) == {"id", "owner", "state", "code", "wp", "accepted", "title", "detail"}
        assert gate["owner"] in rd.OWNERS and gate["state"] in rd.STATES and gate["wp"]


def test_text_report_counts_red_by_owner(world):
    world.disk = 1.0                                         # user
    world.uses = ["actions/checkout@v4"]                     # agent
    lines = _run(world).lines()
    assert lines[0].startswith("NOT READY (Track S, post-token): 2 red (agent: 1, user: 1)")
    assert lines[2].split()[:3] in (["R5", "red", "agent"], ["E1", "red", "user"])


# ---------------------------------------------------------------- the default probes, on the runner
class Recorder:
    """A subprocess stand-in: records every argv and answers by prefix (default: exit 1)."""

    def __init__(self, answers: dict[tuple[str, ...], Completed] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=20.0):
        self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        return Completed(1, "", "")


def _real_probes(tmp_path: Path, run: Recorder, *, repo: Path | None = None, network: bool = True,
                 env: dict[str, str] | None = None) -> Probes:
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return Probes(state_dir=tmp_path / "state", repo=repo or paths.REPO_ROOT, home=tmp_path / "home",
                  env=env or {}, now=NOW, network=network, run=run)


def test_security_is_never_called_with_w_or_g(tmp_path):
    run = Recorder({("/usr/bin/security", "find-generic-password"): Completed(0, "attributes only", "")})
    report = evaluate(_real_probes(tmp_path, run), track="stocks", post_token=True)
    assert not report.internal_error, [r.result for r in report.results if r.result.code == "internal_error"]
    security = [c for c in run.calls if Path(c[0]).name == "security"]
    assert security, "presence checks go through security"
    assert all("-w" not in c and "-g" not in c for c in security)
    with pytest.raises(ReadinessError):
        _real_probes(tmp_path, run).run(["/usr/bin/security", "find-generic-password", "-s", "x", "-w"])
    with pytest.raises(ReadinessError):
        _real_probes(tmp_path, run).run(["security", "find-generic-password", "-g", "-s", "x"])
    for cluster in ("-gw", "-wa", "-Dw"):
        with pytest.raises(ReadinessError):
            _real_probes(tmp_path, run).run(["security", "find-generic-password", cluster, "-s", "x"])


def test_default_probes_survive_a_machine_where_every_command_fails(tmp_path):
    report = evaluate(_real_probes(tmp_path, Recorder()), track="stocks", post_token=True)
    assert not report.internal_error
    by_id = _by_id(report)
    assert by_id["E3"].result.code == "ollama_unavailable"
    assert by_id["R2"].result.code == "ci_unreachable"
    assert by_id["E6"].result.code == "publisher_missing"


def test_ac_sleep_reads_the_ac_section_only(tmp_path):
    out = "Battery Power:\n lidwake 1\n sleep                1\nAC Power:\n displaysleep 10\n sleep                0\n"
    assert _real_probes(tmp_path, Recorder({("pmset",): Completed(0, out)})).ac_sleep() == 0
    assert _real_probes(tmp_path, Recorder({("pmset",): Completed(0, out.replace("0\n", "15\n"))})).ac_sleep() == 15
    assert _real_probes(tmp_path, Recorder()).ac_sleep() is None


def test_write_keychain_search_list_and_lock_timeout(tmp_path):
    kc = tmp_path / "state" / rd.WRITE_KEYCHAIN_NAME
    run = Recorder({
        ("/usr/bin/security", "list-keychains"): Completed(0, '    "/Users/x/Library/Keychains/login.keychain-db"\n'),
        ("/usr/bin/security", "show-keychain-info"): Completed(0, "", f'Keychain "{kc}" lock-on-sleep timeout=60s\n'),
    })
    probes = _real_probes(tmp_path, run)
    assert probes.write_keychain() == WriteKeychainInfo(False)
    kc.write_bytes(b"")
    assert probes.write_keychain() == WriteKeychainInfo(True, False, 60)
    run.answers[("/usr/bin/security", "list-keychains")] = Completed(0, f'    "{kc}"\n')
    run.answers[("/usr/bin/security", "show-keychain-info")] = Completed(0, "", f'Keychain "{kc}" no-timeout\n')
    assert probes.write_keychain() == WriteKeychainInfo(True, True, None)


def test_ollama_ci_and_origin_parsing(tmp_path):
    runs = {"check_runs": [{"name": "m5-acceptance", "status": "completed", "conclusion": "success"}]}
    run = Recorder({
        ("ollama", "list"): Completed(0, "NAME ID SIZE MODIFIED\ndeepseek-v4.1-flash:cloud 1 - now\n"),
        ("gh", "api"): Completed(0, json.dumps(runs)),
        ("git", "ls-remote"): Completed(0, f"{OLD}\trefs/tags/council-spec-v1\n{HEAD}\trefs/tags/council-spec-v1^{{}}\n"),
    })
    probes = _real_probes(tmp_path, run)
    assert probes.ollama_model() == ("deepseek-v4.1-flash:cloud", True)
    assert probes.ci_runs(HEAD) == [CheckRun("m5-acceptance", "completed", "success")]
    assert probes.origin_tag_commit("council-spec-v1") == HEAD          # the peeled commit of an annotated tag
    assert probes.ci_runs("not-a-sha") is None


def test_plists_are_parsed_and_checked(tmp_path):
    probes = _real_probes(tmp_path, Recorder())
    agents = probes.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    release = probes.release_dir
    plist = {"Label": "com.fbzz.council.watch",
             "ProgramArguments": [f"{release}/.venv/bin/council", "watch"], "WorkingDirectory": str(release),
             "EnvironmentVariables": {"COUNCIL_ROLE": "runner", "COUNCIL_AGENT_CONTEXT": "1", "PATH": "/usr/bin"}}
    (agents / "com.fbzz.council.watch.plist").write_bytes(plistlib.dumps(plist))
    (agents / "com.other.thing.plist").write_bytes(plistlib.dumps({"Label": "x"}))
    assert [p.name for p in probes.plists()] == ["com.fbzz.council.watch.plist"]
    assert rd.b3_plists(probes).code == "plists_ok"
    plist["EnvironmentVariables"]["HTTPS_PROXY"] = "http://proxy.invalid"
    (agents / "com.fbzz.council.watch.plist").write_bytes(plistlib.dumps(plist))
    assert rd.b3_plists(probes).code == "plist_bad"
    assert rd.b1_base_url(probes).code == "base_url_pinned"


def test_ledger_is_read_read_only(tmp_path):
    from council.ledger.db import Ledger

    probes = _real_probes(tmp_path, Recorder())
    assert probes.ledger_summary() == LedgerSummary(False)
    ledger = Ledger(probes.state_dir / rd.LEDGER_FILE)
    assert probes.ledger_summary() == LedgerSummary(True, True, 0, False, 0, 0)
    assert rd.e8_ledger(probes, post_token=False).code == "ledger_pristine"
    ledger.set_runtime("nav_state", {"peak": 1.0})
    ledger.set_runtime(RUNTIME_BACKUP, {"at": ISO, "licensed_free": True})
    ledger.record_cycle({"cycle_id": "c1", "slot": "2026-09-26T10:40:00Z", "status": "done",
                         "flags": ["history_rate_limited:SPY"]}, now=NOW - timedelta(days=2))
    ledger.record_cycle({"cycle_id": "c0", "slot": "2026-08-01T10:40:00Z", "status": "done",
                         "flags": ["history_breaker:QQQ"]}, now=NOW - timedelta(days=40))
    assert probes.ledger_summary().nav_state is True
    assert rd.e8_ledger(probes, post_token=False).code == "ledger_has_nav_state"
    assert probes.runtime(RUNTIME_BACKUP) == {"at": ISO, "licensed_free": True}
    assert probes.rate_limit_trips(NOW - timedelta(days=30)) == [NOW - timedelta(days=2)]
    before = (probes.state_dir / rd.LEDGER_FILE).stat().st_mtime_ns
    probes.ledger_summary()
    assert (probes.state_dir / rd.LEDGER_FILE).stat().st_mtime_ns == before


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def test_r1_on_a_real_throwaway_release(tmp_path):
    probes = _real_probes(tmp_path, rd.default_run, network=False)
    assert rd.r1_release(probes).code == "release_missing"
    release = probes.release_dir
    release.mkdir(parents=True)
    _git(release, "init", "-q")
    (release / "README.md").write_text("x")
    _git(release, "add", ".")
    _git(release, "commit", "-q", "-m", "x")
    assert rd.r1_release(probes).code == "release_untagged"
    _git(release, "tag", "-a", "council-spec-v1", "-m", "v1")
    head = subprocess.run(["git", "-C", str(release), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert rd.r1_release(probes).code == "release_record_mismatch"
    (probes.state_dir / "releases" / "installed.json").write_text(json.dumps({"commit": head, "tag": "council-spec-v1"}))
    assert rd.r1_release(probes).code == "release_ok"
    (release / "README.md").write_text("changed")
    assert rd.r1_release(probes).code == "release_dirty"


# ------------------------------------------------------------------------ the real repository files
def test_this_repository_passes_its_agent_gates(tmp_path):
    probes = _real_probes(tmp_path, Recorder(), network=False)
    for check, code in ((rd.r4_policy, "policy_prompts_ok"), (rd.r5_pinned, "actions_pinned"),
                        (rd.b5_agent_rules, "agent_rules_ok"), (rd.b6_operator_guard, "operator_commands_guarded"),
                        (rd.b8_deny_rules, "deny_rules_present"), (rd.e7_head_policy, "head_snapshot_ok")):
        assert check(probes).code == code, check.__name__


def test_the_operator_registry_is_read_from_cli_source_without_importing_it():
    defined, registry = rd.operator_registry_from_source((paths.REPO_ROOT / "src/council/cli.py").read_text())
    assert {"doctor", "doctor --live-read", "approve", "ops review", "keys store-read", "resume-exec"} <= defined
    assert registry["approve"] is True and registry["doctor --live-read"] is True
    assert registry["reject"] is False and registry["purge-licensed"] is True
    assert all(registry[path] for path, pinned in REQUIRED_OPERATOR.items() if pinned and path in registry)


def test_b6_finds_an_unguarded_or_unpinned_command_in_source():
    source = textwrap.dedent('''
        import typer
        app = typer.Typer()
        ops = typer.Typer()
        app.add_typer(ops, name="ops")
        OPERATOR_COMMANDS: dict[str, bool] = {"show": False}

        @app.command()
        def approve(decision_id: str) -> None: ...

        @ops.command("attest")
        @operator_command("ops attest", pinned=False)
        def ops_attest() -> None: ...

        @app.command()
        def doctor(record: bool = typer.Option(False, "--record-fixtures")) -> None:
            require_operator("doctor --record-fixtures", pinned=OPERATOR_COMMANDS["show"])
    ''')
    defined, registry = rd.operator_registry_from_source(source)
    assert {"approve", "ops attest", "doctor", "doctor --record-fixtures"} <= defined
    assert registry == {"show": False, "ops attest": False, "doctor --record-fixtures": False}

    class P(FakeProbes):
        def operator_registry(self):
            return defined, registry
    got = rd.b6_operator_guard(P(World(home=Path("/nonexistent"), state=Path("/nonexistent"))))
    assert got.code == "operator_command_unguarded"
    assert "unguarded: approve" in got.detail and "ops attest" in got.detail and "doctor --record-fixtures" in got.detail


def test_acceptance_ids_list_the_existing_node_ids_for_ci():
    ids = rd.acceptance_ids()
    assert "tests/operator/test_readiness.py" in ids
    assert all(Path(paths.REPO_ROOT, i.partition("::")[0]).is_file() for i in ids)
    everything = rd.acceptance_ids(include_missing=True)
    assert set(ids) <= set(everything)
    assert "tests/rehearsal/test_onboarding_rehearsal.py" in everything
    assert all(not ACCEPTANCE[wp].acceptance or set(ACCEPTANCE[wp].tests) <= set(everything) for wp in ACCEPTANCE)


# ------------------------------------------------------------------------------------- CLI and imports
def test_cli_doctor_ready_exit_codes_and_flags(world, monkeypatch):
    from council.cli import app

    monkeypatch.setattr(rd.Probes, "default", classmethod(lambda cls, network=False: FakeProbes(world, network=network)))
    runner = CliRunner()
    ok = runner.invoke(app, ["doctor", "--ready", "--track", "stocks", "--post-token", "--network", "--json"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["ready"] is True
    world.disk = 1.0
    bad = runner.invoke(app, ["doctor", "--ready", "--track", "stocks", "--post-token", "--network"])
    assert bad.exit_code == 1 and bad.output.startswith("NOT READY")
    assert runner.invoke(app, ["doctor", "--ready", "--track", "nope"]).exit_code == 2
    assert runner.invoke(app, ["doctor", "--json"]).exit_code == 2
    assert runner.invoke(app, ["doctor", "--ready", "--live-read"]).exit_code == 2


_IMPORT_PROBE = textwrap.dedent('''
    import json, sys
    from datetime import UTC, datetime
    from pathlib import Path
    from council.operator import readiness as rd

    def run(argv, **_):
        return rd.Completed(1, "", "")
    rd.default_run = run                     # no real subprocess on the machine running the tests
    mode, state, home = sys.argv[1:4]
    if mode == "cli":
        from typer.testing import CliRunner
        from council.cli import app
        result = CliRunner().invoke(app, ["doctor", "--ready", "--json", "--track", "stocks"])
        code = result.exit_code
    else:
        probes = rd.Probes(state_dir=Path(state), repo=rd.paths.REPO_ROOT, home=Path(home), env={},
                           now=datetime.now(UTC), network=False)
        code = rd.evaluate(probes, track="stocks", post_token=True).exit_code
    print(json.dumps({"code": code, "modules": sorted(sys.modules)}))
''')


@pytest.mark.parametrize("mode", ["module", "cli"])
def test_no_broker_or_llm_client_is_imported_on_the_ready_path(tmp_path, mode):
    env = {**os.environ, "COUNCIL_STATE_DIR": str(tmp_path / "state"), "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    out = subprocess.run([sys.executable, "-c", _IMPORT_PROBE, mode, str(tmp_path / "state"), str(tmp_path / "home")],
                         capture_output=True, text=True, env=env, timeout=90)
    assert out.returncode == 0, out.stderr[-2000:]
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["code"] in (0, 1)
    forbidden = ["council.broker", "council.execution"]
    if mode == "module":                     # council.cli itself registers views that import the gateway
        forbidden += ["council.llm.gateway", "council.llm.stub", "council.deliberation"]
    hits = [m for m in data["modules"] if any(m == f or m.startswith(f + ".") for f in forbidden)]
    assert hits == []


# ------------------------------------------------------------------------------------------- records
def _allow() -> None:
    return None


def test_write_record_is_0600_merges_and_validates(tmp_path):
    state = tmp_path / "state"
    path = write_record("attest", head=HEAD, attested={"power-ok": True}, state_dir=state, now=NOW,
                        assert_operator=_allow, assert_release=_allow)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    write_record("attest", head=HEAD, attested={"ntfy-received": False}, gates={"K15": {"state": "green",
                 "code": "fee_location_matches"}}, state_dir=state, now=NOW, assert_operator=_allow,
                 assert_release=_allow)
    data = json.loads(path.read_text())
    assert data["attested"] == {"power-ok": {"value": True, "at": ISO}, "ntfy-received": {"value": False, "at": ISO}}
    assert data["gates"] == {"K15": {"state": "green", "code": "fee_location_matches"}}
    probes = _real_probes(tmp_path, Recorder())
    assert probes.attested("power-ok") is True and probes.attested("ntfy-received") is False


@pytest.mark.parametrize("name, kwargs", [
    ("nope", {"gates": {}}),
    ("keys", {"gates": {"K7": {"state": "green", "code": "ok"}}}),           # K7 belongs to live-read
    ("keys", {"gates": {"K1": {"state": "green", "code": "equity 1234.5"}}}),
    ("keys", {"gates": {"K1": {"state": "green", "code": "portfolio_12345678"}}}),
    ("keys", {"gates": {"K1": {"state": "wait", "code": "ok"}}}),
    ("attest", {"attested": {"not-an-item": True}}),
])
def test_write_record_refuses_values_and_foreign_gates(tmp_path, name, kwargs):
    with pytest.raises(ReadinessError):
        write_record(name, head=HEAD, state_dir=tmp_path / "state", now=NOW, assert_operator=_allow,
                     assert_release=_allow, **kwargs)
    with pytest.raises(ReadinessError):
        write_record("keys", head="HEAD", state_dir=tmp_path / "state", assert_operator=_allow, assert_release=_allow)


def test_write_record_refuses_outside_the_operator_terminal(tmp_path):
    from council.operator.guards import GuardError

    with pytest.raises(GuardError):              # pytest: no TTY, COUNCIL_ROLE=dev
        write_record("keys", head=HEAD, gates={"K1": {"state": "green", "code": "ok"}}, state_dir=tmp_path / "state")
    assert not (tmp_path / "state" / "readiness").exists()


def test_write_record_refuses_outside_the_installed_release(tmp_path):
    from council.operator.release import ReleaseError

    with pytest.raises(ReleaseError):
        write_record("dress", head=HEAD, gates={"O5": {"state": "green", "code": "dress_ok"}},
                     state_dir=tmp_path / "state", assert_operator=_allow)


def test_only_the_launchd_rehearsal_watch_writes_soak(tmp_path):
    runner_env = {"COUNCIL_ROLE": "runner", "XPC_SERVICE_NAME": "com.fbzz.council.rehearsal.watch",
                  "COUNCIL_AGENT_CONTEXT": "1"}
    gates = {"O3": {"state": "green", "code": "soak_ok"}}
    for env in ({**runner_env, "COUNCIL_ROLE": "operator"}, {**runner_env, "XPC_SERVICE_NAME": "com.fbzz.council.watch"},
                {**runner_env, "CLAUDECODE": "1"}, {**runner_env, "CLAUDE_CODE_ENTRYPOINT": "cli"}):
        with pytest.raises(ReadinessError):
            write_record("soak", head=HEAD, gates=gates, state_dir=tmp_path / "state", env=env)
    path = write_record("soak", head=HEAD, gates=gates, state_dir=tmp_path / "state", env=runner_env, now=NOW)
    assert json.loads(path.read_text())["gates"] == gates


def test_records_are_read_back_through_the_schema(tmp_path):
    probes = _real_probes(tmp_path, Recorder())
    folder = probes.state_dir / "readiness"
    folder.mkdir(parents=True)
    (folder / "keys.json").write_text(json.dumps(_record(["K1"])))
    assert probes.record("keys")["gates"]["K1"]["code"] == "ok"
    (folder / "dress.json").write_text("{not json")
    assert probes.record("dress") is INVALID
    assert probes.record("smoke") is None
    assert replace(ReleaseInfo(False), exists=True).exists


def test_ci_runs_the_registry_in_job_m5_acceptance():
    import yaml

    ci = yaml.safe_load((paths.REPO_ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"][rd.CI_ACCEPTANCE_JOB]
    assert job["name"] == rd.CI_ACCEPTANCE_JOB                  # the check-run name `gh` reports
    steps = "\n".join(str(step) for step in job["steps"])
    assert "python -m council.operator.readiness acceptance-ids" in steps
    assert 'pytest -m "not live" "${ids[@]}"' in steps
    out = subprocess.run([sys.executable, "-m", "council.operator.readiness", "acceptance-ids"],
                         capture_output=True, text=True, check=True, cwd=paths.REPO_ROOT)
    assert out.stdout.split() == rd.acceptance_ids()
    assert "tests/rehearsal/test_onboarding_rehearsal.py" in rd.acceptance_ids()   # R3: L1 runs in this job


def test_o1_o2_ignore_a_shell_only_value(world):
    """A topic or URL exported only in the operator shell never reaches the launchd runner: O1 stays
    red and O2 amber until the Keychain item exists."""
    world.env.update({"COUNCIL_NTFY_TOPIC": "shell-only-topic-123",
                      "COUNCIL_HEALTHCHECK_URL": "https://hc-ping.example/x"})
    world.keychain -= {NTFY_ITEM, HEALTHCHECK_ITEM}
    by_id = _by_id(_run(world))
    assert (by_id["O1"].result.state, by_id["O1"].result.code) == ("red", "ntfy_absent")
    assert (by_id["O2"].result.state, by_id["O2"].result.code) == ("amber", "healthcheck_absent")
    assert "launchd runner" in by_id["O1"].result.detail
    assert "shell-only-topic-123" not in by_id["O1"].result.detail
