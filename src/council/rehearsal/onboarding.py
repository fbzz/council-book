"""Token day, step by step, over a throwaway sandbox (m5-readiness §7.1–§7.2).

`Sandbox.create(root)` builds a marked rehearsal state dir (`REHEARSAL`), a throwaway keychain file
path, an in-memory `FakeSecurity`, a local bare remote with its publisher clone, the onboarding
`FakeEtoro` behind the loopback server (`broker/fake_server.py`) and a `FakeClock` on Thursday
2026-10-01 08:50 UTC. Every step is a function over the sandbox that performs the runbook step
through the production modules and returns a `StepResult`; pytest (`tests/rehearsal`) and `council
rehearse onboarding` share them.

Boundaries (each one is asserted by a test):
- The sandbox refuses the real state dir; eToro Keychain items live only in the throwaway file and
  the sandbox write keychain, and every `security` call goes to the in-memory fake.
- The broker is reached only through `http://127.0.0.1:<fake-broker.port>` via the real
  `check_base_url` pin; the clients are the production read/write clients.
- The model is the stub; nothing publishes anywhere but the sandbox's bare remote.
- Approval is the production `operator.approve.approve()` with the REAL guard function fed a
  simulated operator terminal, and the nonce read from the prompt (never the CLI `approve`).
- The broker writer is built inside `approval_deps` only (the operator step, through
  `operator.rehearsal_writer`, sandbox + loopback only), never imported at load.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from council.rehearsal import scenario
from council.rehearsal.security import FakeSecurity

START = datetime(2026, 10, 1, 8, 50, tzinfo=UTC)         # Thursday, LSE open (summer time)
FIRST_SLOT = datetime(2026, 10, 1, 10, 40, tzinfo=UTC)
OPERATOR_ANCESTORS = ("-zsh", "login", "Terminal")
SMOKE_SEQUENCE = ("S1", "S2", "S3", "S4", "S5", "S5x", "S6", "S6x")
FEED_PATH = "/api/v1/feeds"
THROWAWAY_KEYCHAIN = "rehearsal-throwaway.keychain-db"
_GIT_IDENT = ("-c", "user.name=council-publisher", "-c",
              "user.email=18754232+fbzz@users.noreply.github.com")


class RehearsalError(RuntimeError):
    """A sandbox refusal or a failed step precondition (never carries a token)."""


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class CapturingNotifier:
    """Stands in for ntfy: records (title, body, priority); sends nothing."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, body: str, priority: str = "default") -> Any:
        self.sent.append((title, body, priority))

        class _Result:
            sent = ("capture",)
            reason = None

        return _Result()


def operator_guard() -> None:
    """The REAL guard function, fed the operator's own terminal (clean env, both TTYs, a login
    shell under Terminal)."""
    from council.operator import guards

    guards.assert_operator_context(env={"COUNCIL_ROLE": "operator"}, stdin_isatty=True,
                                   stdout_isatty=True, ancestors=list(OPERATOR_ANCESTORS))


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def synthetic_history(slot: datetime) -> tuple[dict[str, Any], list[str]]:
    """Up-trending synthetic daily bars for every core line, completed before the slot."""
    import numpy as np
    import pandas as pd

    from council.policy import Policy

    rng = np.random.default_rng(7)
    out: dict[str, Any] = {}
    for line in Policy.load(include_sleeve=False).universe.lines:
        crypto = line.asset_class == "crypto"
        end = pd.Timestamp((slot - timedelta(days=1)).date())
        idx = pd.date_range(end=end, periods=500, freq="D") if crypto else pd.bdate_range(end=end, periods=500)
        idx = pd.DatetimeIndex(idx).tz_localize("UTC")
        r = 0.0012 + rng.normal(0, 0.03 if crypto else 0.009, len(idx))
        close = 100 * np.exp(np.cumsum(r))
        openp = np.concatenate([[close[0]], close[:-1]])
        out[line.symbol] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.002,
                                         "low": np.minimum(openp, close) * 0.998, "close": close,
                                         "volume": 1000.0}, index=idx)
    return out, []


@dataclass
class Sandbox:
    root: Path
    state_dir: Path
    keychain_file: Path
    remote: Path
    clone: Path
    home: Path
    clock: Any
    fake: Any
    tokens: scenario.Tokens
    security: FakeSecurity
    notifier: CapturingNotifier
    funding_usd: float = scenario.CANARY_FUNDING_USD
    public_news: scenario.PublicNews = field(default_factory=scenario.PublicNews)
    server: Any = None
    log: list[str] = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)
    _ledger: Any = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(cls, root: Path, *, start: datetime = START, read_scopes: tuple[str, ...] | None = None,
               funding_usd: float = scenario.CANARY_FUNDING_USD) -> Sandbox:
        from council.broker.fake import FakeClock
        from council.operator.release import REHEARSAL_MARKER, default_state_dir

        root = root.expanduser().resolve()
        state = root / "state"
        real = default_state_dir().expanduser()
        with contextlib.suppress(OSError):
            real = real.resolve()
        if state == real or real in state.parents or state in real.parents:
            raise RehearsalError("the rehearsal sandbox must never resolve to the real state dir")
        state.mkdir(parents=True, exist_ok=True)
        (state / REHEARSAL_MARKER).write_text("rehearsal sandbox: never the real state dir\n")
        home = root / "home"
        home.mkdir(exist_ok=True)
        clock = FakeClock(start=start)
        tokens = scenario.Tokens.fresh()
        fake = scenario.build_fake(tokens, clock.now, now=start, read_scopes=read_scopes)
        remote, clone = root / "remote.git", state / "publisher-clone"
        box = cls(root=root, state_dir=state, keychain_file=root / THROWAWAY_KEYCHAIN, remote=remote,
                  clone=clone, home=home, clock=clock, fake=fake, tokens=tokens,
                  security=FakeSecurity(login=str(home / "Library" / "Keychains" / "login.keychain-db")),
                  notifier=CapturingNotifier(), funding_usd=funding_usd)
        box._init_remote()
        box.security.items[str(box.keychain_file)] = {}          # the throwaway file (created by the script)
        return box

    def _init_remote(self) -> None:
        _git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        _git("clone", "-q", str(self.remote), str(self.clone), cwd=self.root)
        (self.clone / "journal").mkdir(exist_ok=True)
        (self.clone / "journal" / ".keep").write_text("")
        _git("config", "user.name", "council-publisher", cwd=self.clone)
        _git("config", "user.email", "18754232+fbzz@users.noreply.github.com", cwd=self.clone)
        _git("add", "journal/.keep", cwd=self.clone)
        _git(*_GIT_IDENT, "commit", "-q", "-m", "rehearsal remote", cwd=self.clone)
        _git("push", "-q", "-u", "origin", "main", cwd=self.clone)
        push_urls = _git("remote", "-v", cwd=self.clone)
        if any(str(self.remote) not in line for line in push_urls.splitlines() if line.strip()):
            raise RehearsalError("the rehearsal publisher clone must push to the sandbox remote only")

    # ------------------------------------------------------------------ environment
    def env(self) -> dict[str, str]:
        """The variables the [REHEARSAL] shell carries (never exported into the user's shell)."""
        return {"COUNCIL_STATE_DIR": str(self.state_dir), "COUNCIL_KEYCHAIN_FILE": str(self.keychain_file),
                "COUNCIL_ETORO_BASE_URL": self.base_url, "COUNCIL_MODE": "stub"}

    def operator_env(self) -> dict[str, str]:
        return {**self.env(), "COUNCIL_ROLE": "operator"}

    def activate(self, setenv: Callable[[str, str], None]) -> None:
        """Point this process at the sandbox (pytest: `monkeypatch.setenv`)."""
        for name, value in self.env().items():
            setenv(name, value)

    @property
    def base_url(self) -> str:
        if self.server is None:
            raise RehearsalError("the fake broker is not running")
        return self.server.base_url

    # ------------------------------------------------------------------ broker
    def start_broker(self) -> None:
        from council.broker.fake_server import FakeBrokerServer

        if self.server is None:
            self.server = FakeBrokerServer(self.fake, self.state_dir).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self._ledger is not None:
            self._ledger = None

    def _secret(self, service: str, keychain: Path | None = None) -> str:
        from council.operator import keychain as kc

        return kc.read_secret(service, keychain=keychain, runner=self.security, env=self.operator_env(),
                              ancestors=list(OPERATOR_ANCESTORS))

    def read_client(self) -> Any:
        """The production READ client, keys from the throwaway keychain, URL through the pin."""
        from council.broker.etoro_read import EtoroReadClient
        from council.broker.http import check_base_url
        from council.operator import keychain as kc

        url = check_base_url(self.base_url, self.state_dir)
        return EtoroReadClient(self._secret(kc.API_KEY_SERVICE), self._secret(kc.READ_SERVICE),
                               base_url=url, sleep=lambda s: None)

    def write_token_reader(self) -> Any:
        """A READ-client instance holding the WRITE token (keys verify's one GET)."""
        from council.broker.etoro_read import EtoroReadClient
        from council.broker.http import check_base_url
        from council.operator import keychain as kc

        token = self._secret(kc.WRITE_SERVICE, kc.write_keychain_path())
        return EtoroReadClient(self._secret(kc.API_KEY_SERVICE), token,
                               base_url=check_base_url(self.base_url, self.state_dir), sleep=lambda s: None)

    def feed_requests(self) -> int:
        return self.fake.count("GET", FEED_PATH)

    # ------------------------------------------------------------------ council pieces
    @property
    def policy(self) -> Any:
        from council.policy import Policy

        return Policy.load(include_sleeve=False)

    @property
    def ledger(self) -> Any:
        from council.ledger.db import Ledger

        if self._ledger is None:
            self._ledger = Ledger(self.state_dir / "ledger.sqlite3", clock=self.clock.now)
            self._ledger.migrate()
        return self._ledger

    def cycle_context(self, *, mode: str = "live") -> Any:
        from council.context import hold_reference_stub, news_sources
        from council.llm.prompts import PromptRegistry
        from council.llm.stub import StubGateway
        from council.publish.gitops import Publisher
        from council.runtime import CycleContext, Sources
        from council.settings import Settings

        broker = self.read_client()
        news, _feed = news_sources(self.policy, broker=broker, state_dir=self.state_dir, public=self.public_news,
                                   clock=self.clock.now)
        return CycleContext(
            policy=self.policy, settings=Settings(role="dev", mode=mode), ledger=self.ledger,  # type: ignore[arg-type]
            gateway=StubGateway(hold_reference_stub()), registry=PromptRegistry(),
            sources=Sources(history=synthetic_history, events=lambda s, e: ([], []), news=news, broker=broker),
            publisher=Publisher(self.clone, push=True), notifier=self.notifier, clock=self.clock.now,
            state_dir=self.state_dir, canaries=tuple(self.canaries()))

    def smoke_deps(self, out: list[str]) -> Any:
        from council.operator import smoke

        return smoke.SmokeDeps(ledger=self.ledger, policy=self.policy, read=self.read_client(),
                               state_dir=self.state_dir, print_fn=out.append, now_fn=self.clock.now,
                               guard_fn=operator_guard, jobs_loaded=set, release_fn=lambda: None)

    def approval_deps(self, out: list[str], *, guard: Callable[[], None] = operator_guard,
                      nonce_from: Callable[[str], str] | None = None) -> Any:
        """The operator terminal: the writer is imported here only; the WRITE token comes from the
        sandbox write keychain through the production `read_secret`."""
        from council.operator import keychain as kc
        from council.operator.approve import ApprovalDeps

        def answer(prompt: str) -> str:
            out.append(f"prompt: {prompt}")              # the simulated operator reads the nonce
            found = re.search(r"Type (\S+) to approve", prompt)
            return found.group(1) if found else ""

        def write_factory() -> Any:
            from council.operator.rehearsal_writer import sandbox_write_client

            token = self._secret(kc.WRITE_SERVICE, kc.write_keychain_path())
            api = self._secret(kc.API_KEY_SERVICE)
            return sandbox_write_client(api, token, base_url=self.base_url, state_dir=self.state_dir)

        return ApprovalDeps(
            ledger=self.ledger, policy=self.policy, read=self.read_client(), write_factory=write_factory,
            state_dir=self.state_dir, input_fn=nonce_from or answer, print_fn=out.append,
            now_fn=self.clock.now, guard_fn=guard, journal_dir=self.clone / "journal",
            executor_kwargs={"clock": self.clock.now, "sleep": self.clock.sleep, "_skip_guard_for_tests": True})

    def canaries(self) -> list[str]:
        return scenario.canaries(self.tokens, funding_usd=self.funding_usd)

    def record(self, result: StepResult) -> StepResult:
        self.results.append(result)
        self.log.append(f"{'ok  ' if result.ok else 'FAIL'} {result.step} {result.detail}".rstrip())
        return result

    def public_files(self) -> list[Path]:
        return [p for p in (self.clone / "journal").rglob("*") if p.is_file() and p.name != ".keep"]


# ====================================================================== steps (runbook order)
def sandbox_probes(box: Sandbox) -> Any:
    """`doctor --ready` probes over the sandbox. Only `git` may run: `launchctl`, `security`,
    `pmset`, `ollama` and the rest answer "not found", so the machine's own keychain, launchd jobs
    and network are never consulted."""
    from council import paths
    from council.operator import readiness

    def run(argv: Any, *, timeout: float = 20.0, env: Any = None) -> Any:
        if argv and Path(str(argv[0])).name == "git":
            return readiness.default_run(argv, timeout=timeout, env=env)
        return readiness.Completed(127, "", "not found (rehearsal sandbox)")

    return readiness.Probes(state_dir=box.state_dir, repo=paths.REPO_ROOT, home=box.home, env=box.env(),
                            now=box.clock.now(), network=False, run=run)


def _write_record(box: Sandbox, name: str, gates: dict[str, dict[str, str]]) -> None:
    from council.operator import readiness

    if gates:
        readiness.write_record(name, head=sandbox_probes(box).head(), gates=gates,
                               state_dir=box.state_dir, now=box.clock.now(),
                               assert_operator=operator_guard, assert_release=lambda: None)


def feed_state(box: Sandbox) -> tuple[bool, bool]:
    """(feed on, LC1 green) exactly as the CLI computes them (`cli._onboarding_feed`)."""
    probes = sandbox_probes(box)
    on = probes.broker_feed_on()
    return on, on and probes.attested("etoro-licence")


def step_attest_licence(box: Sandbox) -> StepResult:
    """The scripted `ops attest etoro-licence --ref <ticket>` (the ticket is checked for presence
    and never stored), so the feed may be read privately (personal use, never published)."""
    from council.operator import readiness

    readiness.write_record("attest", head=sandbox_probes(box).head(), attested={"etoro-licence": True},
                           state_dir=box.state_dir, now=box.clock.now(),
                           assert_operator=operator_guard, assert_release=lambda: None)
    on, licensed = feed_state(box)
    return box.record(StepResult("ops attest etoro-licence", licensed or not on, f"feed on={on}"))


def step_keys(box: Sandbox, getpass_fn: Callable[[str], str] | None = None) -> StepResult:
    """keys init-write-keychain / store-read / store-write, through the production keychain module
    on the fake `security`. The hidden prompt answers with the sandbox's fake tokens."""
    from council.operator import keychain as kc

    answers = iter([box.tokens.app, box.tokens.read, box.tokens.write])
    prompt = getpass_fn or (lambda _p: next(answers))
    if not kc.write_keychain_path().exists() and str(kc.write_keychain_path().resolve()) not in box.security.items:
        kc.create_write_keychain(runner=box.security)
    for service, target in ((kc.API_KEY_SERVICE, None), (kc.READ_SERVICE, None),
                            (kc.WRITE_SERVICE, kc.write_keychain_path())):
        kc.store_token_interactive(service, target, getpass_fn=prompt, runner=box.security)
    return box.record(StepResult("keys", True, "tokens stored in the throwaway keychains"))


def step_keys_verify(box: Sandbox) -> StepResult:
    from council.operator import onboarding

    outcome = onboarding.probe_keys(box.read_client(), box.write_token_reader(), now=box.clock.now(),
                                    scopes_attested=False)
    _write_record(box, outcome.record, outcome.gates())
    if outcome.onboarded:
        onboarding.write_onboarded(box.state_dir, now=box.clock.now())
    return box.record(StepResult("keys verify", outcome.ok, "", {"gates": outcome.gates(),
                                                                  "lines": outcome.report_lines()}))


def step_live_read(box: Sandbox) -> StepResult:
    from council.operator import onboarding

    before = box.feed_requests()
    feed_on, licensed = feed_state(box)
    outcome = onboarding.probe_live_read(box.read_client(), policy=box.policy, state_dir=box.state_dir,
                                         now=box.clock.now(), feed_on=feed_on, feed_licensed=licensed)
    _write_record(box, outcome.record, outcome.gates())
    feed = box.feed_requests() - before
    allowed = 1 if licensed else 0                    # take=1, body discarded; none while unlicensed
    return box.record(StepResult("doctor --live-read", outcome.ok and feed <= allowed, f"feed requests {feed}",
                                 {"gates": outcome.gates(), "lines": outcome.report_lines(), "feed": feed}))


def step_set_mirror(box: Sandbox) -> StepResult:
    from council.operator import onboarding

    outcome, config = onboarding.mirror_from_broker(box.read_client(), state_dir=box.state_dir,
                                                    funding_usd=box.funding_usd, now=box.clock.now())
    _write_record(box, outcome.record, outcome.gates())
    expected = box.funding_usd / scenario.VIRTUAL_BALANCE
    ok = abs(config.mirror_ratio - expected) < 1e-12
    return box.record(StepResult("account set-mirror", ok, "", {"ratio": config.mirror_ratio}))


def step_instruments(box: Sandbox) -> StepResult:
    from council.operator import onboarding

    outcome = onboarding.probe_instruments(box.read_client(), policy=box.policy, state_dir=box.state_dir,
                                           now=box.clock.now())
    _write_record(box, outcome.record, outcome.gates())
    return box.record(StepResult("instruments resolve", outcome.ok, "",
                                 {"gates": outcome.gates(), "lines": outcome.report_lines()}))


def step_record_fixtures(box: Sandbox) -> StepResult:
    from council.operator import onboarding

    before = box.feed_requests()
    feed_on, licensed = feed_state(box)
    outcome = onboarding.record_fixtures(box.read_client(), policy=box.policy, state_dir=box.state_dir,
                                         now=box.clock.now(), feed_on=feed_on, feed_licensed=licensed)
    _write_record(box, outcome.record, outcome.gates())
    feed = box.feed_requests() - before
    allowed = 1 if licensed else 0
    return box.record(StepResult("doctor --record-fixtures", outcome.ok and feed <= allowed, f"feed requests {feed}",
                                 {"gates": outcome.gates()}))


def run_smoke_step(box: Sandbox, step: str, out: list[str]) -> str:
    """propose → the watch publishes the weightless ops row → approve in the simulated operator
    terminal → execution on the fake → watch → verify → the scripted mirror attestations."""
    from council.operator import capabilities, smoke
    from council.operator.approve import approve
    from council.watch import run_watch

    ticket = smoke.propose(step, box.smoke_deps(out))
    decision_id = ticket.decision_id
    if decision_id is None:
        raise RehearsalError(f"smoke {step}: no decision")
    ctx = box.cycle_context()
    run_watch(ctx)
    decision = box.ledger.get_decision(decision_id)
    if decision.state != "proposed":
        raise RehearsalError(f"smoke {step}: {decision.state} after the watch")
    report = approve(decision_id, box.approval_deps(out))
    if report.final_state != "completed":
        raise RehearsalError(f"smoke {step}: {report.final_state} {list(report.reasons)}")
    box.clock.advance(5)
    run_watch(ctx)
    checks = smoke.verify(decision_id, box.smoke_deps(out))
    failed = [c for c in checks if not c.ok]
    if failed:
        raise RehearsalError(f"smoke {step}: verify red {[getattr(c, 'code', '?') for c in failed]}")
    if smoke.step_of(step).is_open:
        for item in capabilities.MIRROR_ITEMS:                      # the scripted `ops attest`
            capabilities.write_mirror_check(item, decision_id=decision_id, state_dir=box.state_dir,
                                            now=box.clock.now(), assert_operator=operator_guard,
                                            assert_release=lambda: None)
    box.clock.advance(60)
    return decision_id


def step_smoke(box: Sandbox, steps: Iterable[str] = SMOKE_SEQUENCE) -> StepResult:
    out: list[str] = []
    ids = {step: run_smoke_step(box, step, out) for step in steps}
    return box.record(StepResult("smoke S1-S6", True, f"{len(ids)} tickets", {"ids": ids, "out": out}))


def step_first_cycle(box: Sandbox, slot: datetime = FIRST_SLOT) -> StepResult:
    from council.cycle import run_cycle

    if box.clock.now() < slot:
        box.clock.advance((slot + timedelta(minutes=3) - box.clock.now()).total_seconds())
    before = box.feed_requests()
    out = run_cycle(box.cycle_context())
    feed = box.feed_requests() - before
    _on, licensed = feed_state(box)
    ok = (bool(out.decision_id) and out.decision_state == "proposed" and bool(out.published)
          and feed <= (1 if licensed else 0))              # one feed request per slot, only when licensed
    return box.record(StepResult("first live cycle", ok, f"{out.decision_state} feed {feed}",
                                 {"outcome": out, "feed": feed}))


def step_approve(box: Sandbox, decision_id: str) -> StepResult:
    from council.operator.approve import approve

    out: list[str] = []
    report = approve(decision_id, box.approval_deps(out))
    stops = all(p.sl_rate for p in box.fake.positions.values())
    ok = report.final_state in ("completed", "completed_partial") and stops
    return box.record(StepResult("approve + execute", ok, report.final_state,
                                 {"report": report, "out": out}))


def step_watch(box: Sandbox) -> StepResult:
    from council.watch import run_watch

    box.clock.advance(300)
    w = run_watch(box.cycle_context())
    return box.record(StepResult("watch", True, f"revealed {len(w.revealed)}", {"watch": w}))


def leak_findings(box: Sandbox, extra_text: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Canary / leak-scan findings over every public file and every log line of the sandbox."""
    from council.publish.leakscan import scan

    canaries = box.canaries()
    found: list[tuple[str, str]] = []
    for path in box.public_files():
        for finding in scan(path.read_text(errors="replace"), canaries=canaries):
            found.append((path.relative_to(box.clone).as_posix(), finding.kind))
    logs = [*box.log, *extra_text]
    for source in (box.state_dir / "logs", box.root / "logs"):
        if source.is_dir():
            logs += [p.read_text(errors="replace") for p in source.rglob("*") if p.is_file()]
    for index, text in enumerate(logs):
        found += [(f"log[{index}]", c) for c in canaries if c in text]
    return found


def step_leak_scan(box: Sandbox, extra_text: Iterable[str] = ()) -> StepResult:
    found = leak_findings(box, extra_text)
    return box.record(StepResult("leak scan", not found, f"{len(found)} findings", {"findings": found}))


def run_all(box: Sandbox) -> list[StepResult]:
    """The automated token day (used by `council rehearse onboarding`): stops at the first failure."""
    box.start_broker()
    sequence: list[Callable[[], StepResult]] = [
        lambda: step_keys(box), lambda: step_keys_verify(box), lambda: step_set_mirror(box),
        lambda: step_attest_licence(box), lambda: step_live_read(box),
        lambda: step_instruments(box), lambda: step_record_fixtures(box),
        lambda: step_smoke(box), lambda: step_first_cycle(box),
    ]
    for run in sequence:
        try:
            result = run()
        except Exception as exc:                      # the type only: a message may carry a payload
            return [*box.results, box.record(StepResult("error", False, type(exc).__name__))]
        if not result.ok:
            return box.results
    cycle = box.results[-1].data["outcome"]
    for run in (lambda: step_approve(box, cycle.decision_id), lambda: step_watch(box),
                lambda: step_leak_scan(box)):
        try:
            if not run().ok:
                break
        except Exception as exc:
            box.record(StepResult("error", False, type(exc).__name__))
            break
    return box.results


def remove(box: Sandbox) -> None:
    box.stop()
    shutil.rmtree(box.root, ignore_errors=True)


def ensure_sandbox_env(env: dict[str, str] | None = None) -> None:
    """Refuse to run the rehearsal where a sandbox variable points at the real state dir."""
    from council.operator.release import default_state_dir

    environ = env if env is not None else dict(os.environ)
    state = environ.get("COUNCIL_STATE_DIR")
    if state and Path(state).expanduser().resolve() == default_state_dir().expanduser().resolve():
        raise RehearsalError("COUNCIL_STATE_DIR points at the real state dir")


def dress_cli_context(state_dir: Path | None = None, *, clock: Callable[[], datetime] | None = None) -> Any:
    """The context `council cycle` / `council watch` use inside a marked rehearsal sandbox (the
    human dress rehearsal's [REHEARSAL] shell): the stub model, synthetic history, no events, the
    fixed public-news fake, the production READ client on the pinned loopback fake broker, and the
    sandbox publisher clone (its remote is the sandbox's bare repo). Nothing reaches a model, a
    data vendor, eToro or the real remote. Refuses outside a marked sandbox."""
    from council import paths
    from council.broker.http import check_base_url
    from council.context import hold_reference_stub, news_sources, read_broker
    from council.ledger.db import Ledger
    from council.llm.prompts import PromptRegistry
    from council.llm.stub import StubGateway
    from council.operator.release import is_marked_sandbox
    from council.policy import Policy
    from council.publish.gitops import Publisher
    from council.runtime import CycleContext, Sources
    from council.settings import Settings

    root = (state_dir if state_dir is not None else paths.state_dir()).expanduser()
    if not is_marked_sandbox(root):
        raise RehearsalError("the dress-rehearsal context runs only inside a marked rehearsal sandbox")
    settings = Settings.from_env()
    url = check_base_url(settings.etoro_base_url, root)
    if not url.startswith("http://127.0.0.1:"):
        raise RehearsalError("the dress rehearsal talks only to the loopback fake broker")
    clone = root / "publisher-clone"
    push = _git("remote", "get-url", "--push", "origin", cwd=clone).strip()
    if Path(push).expanduser().resolve().parent != root.resolve().parent:
        raise RehearsalError("the rehearsal publisher clone must push to the sandbox remote only")
    policy = Policy.load(include_sleeve=False)
    ledger = Ledger(root / "ledger.sqlite3")
    ledger.migrate()
    broker = read_broker(settings)
    news, _feed = news_sources(policy, broker=broker, state_dir=root, public=scenario.PublicNews(), clock=clock)
    from dataclasses import replace

    extra = {} if clock is None else {"clock": clock}

    return CycleContext(
        policy=policy, settings=replace(settings, mode="live"), ledger=ledger,
        gateway=StubGateway(hold_reference_stub()), registry=PromptRegistry(),
        sources=Sources(history=synthetic_history, events=lambda s, e: ([], []), news=news, broker=broker),
        publisher=Publisher(clone, push=True), notifier=None, state_dir=root, **extra)
