"""`council` command line. Imports are lazy on purpose: the unattended runner must never import the
broker writer, and only `approve` (operator terminal) can reach it.

Operator commands (m5-readiness §9.1) carry `@operator_command(<path>, pinned=...)`: they refuse
unless `guards.assert_current_process_is_operator` passes (COUNCIL_ROLE=operator, both TTYs, no
agent or CI variable, no agent ancestor process, not under a council launchd job), and a pinned one
also refuses unless the code is the installed release (`operator.release.assert_release_code`; a
marked rehearsal sandbox also passes). Coding agents never run them; `ops assert-operator` lets the
ops scripts ask the same question."""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="LLM agent council bounded by code and a human approval gate.")
keys = typer.Typer(add_completion=False, no_args_is_help=True, help="Store broker tokens (operator only).")
site = typer.Typer(add_completion=False, no_args_is_help=True, help="Build the public site.")
ops = typer.Typer(add_completion=False, no_args_is_help=True, help="Operator bookkeeping (no broker writes).")
account = typer.Typer(add_completion=False, no_args_is_help=True,
                      help="Private account figures the costs need (operator only; no broker calls).")
stocks = typer.Typer(add_completion=False, no_args_is_help=True,
                     help="Quarterly stock sleeve: rank, onboard, corporate actions (READ token only; "
                          "never writes policy/, commits or tags).")
instruments = typer.Typer(add_completion=False, no_args_is_help=True,
                          help="Resolve the vehicles of every line through eligibility (operator only; READ token).")
notify = typer.Typer(add_completion=False, no_args_is_help=True,
                     help="Operator notifications (ntfy; the topic is private and never printed).")
smoke = typer.Typer(add_completion=False, no_args_is_help=True,
                    help="Onboarding smoke tickets S1–S7 (operator only; READ token; executed only "
                         "through `approve`).")
app.add_typer(keys, name="keys")
app.add_typer(notify, name="notify")
app.add_typer(site, name="site")
app.add_typer(ops, name="ops")
app.add_typer(account, name="account")
app.add_typer(stocks, name="stocks")
app.add_typer(instruments, name="instruments")
app.add_typer(smoke, name="smoke")
swing = typer.Typer(add_completion=False, no_args_is_help=True,
                    help="The swing book: status (operator terminal, ledger only; nothing is sent).")
app.add_typer(swing, name="swing")
rehearse = typer.Typer(add_completion=False, no_args_is_help=True,
                       help="Onboarding rehearsal against the fake broker (dev role; marked sandbox only).")
app.add_typer(rehearse, name="rehearse")

F = TypeVar("F", bound=Callable[..., Any])

# command path -> release-pinned: every operator command of §9.1 this CLI defines. Conditional ones
# (`show` without --why, `doctor --live-read`, `stocks rank` without --no-eligibility) call
# `require_operator` in their body and are listed here under that variant's path.
OPERATOR_COMMANDS: dict[str, bool] = {
    "show": False,
    "doctor --live-read": True,
    "doctor --record-fixtures": True,
    "account set-mirror --from-broker": True,
    "stocks rank": True,
    "purge-licensed": True,
}


def _refuse(message: str) -> NoReturn:
    typer.echo(f"refused: {message}", err=True)
    raise typer.Exit(2)


def require_operator(path: str, *, pinned: bool) -> None:
    """Refuse (exit 2) unless this is the human operator's terminal, and, when `pinned`, unless the
    running code is the installed release (or a marked rehearsal sandbox). `path` names the command
    in the refusal ("run it from the installed release: council-op <path> ...")."""
    from council.operator import guards

    try:
        guards.assert_current_process_is_operator()
    except guards.GuardError as exc:
        problems = str(exc).removeprefix("operator command refused: ")
        _refuse("operator command: COUNCIL_ROLE must be 'operator' in the operator's own terminal, "
                f"never an agent, CI or launchd ({problems})")
    if pinned:
        from council.operator import release

        try:
            release.assert_release_code(argv=[path, "..."])
        except release.ReleaseError as exc:
            _refuse(str(exc))


def operator_command(path: str, *, pinned: bool) -> Callable[[F], F]:
    """Mark a command as operator-only (§9.1); `pinned` also requires the installed release."""

    def decorate(fn: F) -> F:
        OPERATOR_COMMANDS[path] = pinned

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            require_operator(path, pinned=pinned)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def _refusable(fn: Callable[[], Any]) -> Any:
    """Run an operator body: ApprovalRefused / ExecutionError -> "refused: ..." and exit 2."""
    from council.execution.executor import ExecutionError
    from council.operator.approve import ApprovalRefused

    try:
        return fn()
    except (ApprovalRefused, ExecutionError) as exc:
        _refuse(str(exc))


def _register_private_views() -> None:
    """`council inputs <cycle> [--html]`, `council inputs verify|prune` and `council purge-licensed`
    (transparency-v2 T1v): exactly what each agent saw, and the licensed-content purge. Every one of
    them refuses outside the operator's interactive terminal (`guards.assert_current_process_is_
    operator`: COUNCIL_ROLE=operator, TTYs, no CLAUDECODE / CLAUDE_CODE_* / CI / agent ancestor),
    because their output can hold eToro Licensed Content. Neither module imports the broker writer."""
    from council.operator.inputs_cli import register

    register(app)


_register_private_views()


def _pin_registered(name: str) -> None:
    """Wrap a command another module registered (`purge-licensed`) in the operator decorator."""
    for info in app.registered_commands:
        if info.name == name and info.callback is not None:
            info.callback = operator_command(name, pinned=OPERATOR_COMMANDS[name])(info.callback)


_pin_registered("purge-licensed")


def _register_why() -> None:
    """`council why <cycle> [<line>]` (transparency-v2 T5a): the per-line decision trail. The
    revealed public record is agent-safe; the private ledger source runs only in the operator's
    terminal, and an unrevealed cycle is refused elsewhere (`council.operator.why`)."""
    from council.operator.why import register

    register(app)


_register_why()


def _ctx(*, mode: str, stub_llm: bool = False, publish: str = "preview"):
    from council.context import build_context

    return build_context(mode=mode, stub_llm=stub_llm, publish=publish)  # type: ignore[arg-type]


def _dress_context():
    """Inside a marked rehearsal sandbox (the dress rehearsal's [REHEARSAL] shell), `cycle` and
    `watch` never reach a model, a data vendor, eToro or the real remote: they run on the sandbox
    context (`rehearsal.onboarding.dress_cli_context`). None outside a sandbox."""
    from council.operator.release import is_marked_sandbox

    if not is_marked_sandbox():
        return None
    from council.rehearsal.onboarding import dress_cli_context

    return dress_cli_context()


@app.command()
def cycle(
    slot: str = typer.Option("auto", help="'auto' runs the due slot (late <= 120 min)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No pushes, no notifications; publish to site-preview/."),
    rehearsal: bool = typer.Option(False, "--rehearsal", help="No broker: run the council and PUBLISH the cycle labelled REHEARSAL (own ledger, nothing traded)."),
    stub_llm: bool = typer.Option(False, "--stub-llm", help="Canned replies that hold the reference (no model calls)."),
    paper: bool = typer.Option(False, "--paper", help="Paper run: real data and models, NO broker token, own state dir (<state>/paper), publishes nothing. Needs COUNCIL_MODE=dry_run."),
    force: bool = typer.Option(False, help="Re-run a slot that already has a record."),
    at: str = typer.Option(None, "--at", help="--paper only: run as if the clock read this UTC time "
                                             "(e.g. 2026-09-28T18:40Z), to replay a missed swing slot."),
    ideas: int = typer.Option(None, "--ideas", help="--paper only: a WIDE swing slot of N ideas (1..20), "
                                                   "each reviewed by the Skeptic; budget and deadline scale."),
) -> None:
    """Run the council cycle for the current 4-hour slot."""
    from council.cycle import run_cycle
    from council.settings import Settings

    settings = Settings.from_env()
    sandbox = _dress_context()
    if ideas is not None and (not paper or sandbox is not None):
        _refuse("--ideas is for --paper runs only (a live, rehearsal or dry-run slot keeps the policy caps)")
    if sandbox is not None:
        ctx = sandbox                         # [REHEARSAL] shell: stub model, fake broker, sandbox remote
    elif paper:
        ctx = paper_context(settings, stub_llm=stub_llm, ideas=ideas)
    elif rehearsal:
        from council import paths
        from council.context import build_context

        ctx = build_context(mode="dry_run", stub_llm=stub_llm, publish="push",
                            state_dir=paths.state_dir() / "rehearsal",
                            publisher_dir=paths.state_dir() / "publisher-clone")
    elif dry_run or settings.mode != "live":
        ctx = _ctx(mode="dry_run", stub_llm=stub_llm, publish="preview")
    else:
        ctx = _ctx(mode="live", stub_llm=stub_llm, publish="push")
    if at is not None:
        if not paper or sandbox is not None:
            _refuse("--at is for --paper runs only (a live or rehearsal slot runs on the wall clock)")
        ctx.clock = paper_clock(at)
    outcome = run_cycle(ctx, force=force)
    typer.echo(json.dumps(outcome.__dict__, default=str, indent=1))


PAPER_STATE = "paper"


def paper_clock(at: str):
    """A fixed clock for a replayed paper slot: an aware UTC time no later than now. The paper
    state dir and `publish="none"` keep it away from the live ledger and the public record."""
    from datetime import UTC, datetime

    from council.clock import utcnow

    try:
        ts = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        _refuse(f"--at: not an ISO time: {at!r}")
    if ts.tzinfo is None:
        _refuse("--at needs a UTC offset (e.g. 2026-09-28T18:40Z)")
    ts = ts.astimezone(UTC)
    if ts > utcnow():
        _refuse("--at may not be in the future")
    return lambda: ts


def paper_context(settings, *, stub_llm: bool = False, ideas: int | None = None):
    """`council cycle --paper` (SW-5c): a dry-run context that never loads a broker token, in its
    own state dir (never the live ledger, so a live slot is never consumed), publishing nothing.
    Real data needs `COUNCIL_MODE=dry_run` (stub mode never reads the Keychain); refused in stub
    mode unless the model is stubbed too. `ideas` (`--ideas N`) makes the swing slot WIDE (the only
    place `CycleContext.swing_wide` is set)."""
    from council import paths
    from council.context import build_context
    from council.swing.council import WIDE_MAX_IDEAS

    if ideas is not None and not 1 <= ideas <= WIDE_MAX_IDEAS:
        _refuse(f"--ideas must be 1..{WIDE_MAX_IDEAS}")
    if settings.mode == "stub" and not stub_llm:
        _refuse("--paper needs COUNCIL_MODE=dry_run (stub mode reads no Keychain item, so no data)")
    ctx = build_context(mode="dry_run", stub_llm=stub_llm, publish="none", no_broker=True,
                        state_dir=paths.state_dir() / PAPER_STATE)
    ctx.swing_wide = ideas
    return ctx


@app.command()
def watch() -> None:
    """Read-only watch: expiries, reveals, execution records, kill switch, heartbeat."""
    from council.settings import Settings
    from council.watch import run_watch

    settings = Settings.from_env()
    ctx = _dress_context() or _ctx(mode="live" if settings.mode == "live" else "dry_run",
                                   publish="push" if settings.mode == "live" else "preview")
    out = run_watch(ctx)
    typer.echo(json.dumps(out.__dict__, default=str, indent=1))


@app.command()
@operator_command("inbox", pinned=False)
def inbox() -> None:
    """Pending proposals (operator terminal)."""
    _root, ledger = _ledger_only()
    for d in ledger.pending():
        typer.echo(f"{d.decision_id}  {d.kind:<10}  {d.state:<20}  {_deadline(d)}")
    for d in ledger.decisions(states=["waiting_for_market"]):
        typer.echo(f"{d.decision_id}  {d.kind:<10}  {d.state:<20}  order(s) held until the market opens")


def _ledger_only():
    """(state dir, ledger) without loading policy: bookkeeping commands (`inbox`, `ops resolve`)
    keep working when the working-tree policy does not load."""
    from council.ledger.db import Ledger

    ledger = Ledger.default()            # the private state dir, never inside the repo
    return ledger.path.parent, ledger


def _deadline(d) -> str:
    """'approve by <UTC>' plus the markets the plan needs and the earliest leg deadline."""
    from council.models.plan import Plan

    text = f"approve by {d.valid_until:%Y-%m-%d %H:%MZ}"
    if d.plan:
        plan = Plan.model_validate(d.plan)
        stamps = [leg.valid_until for leg in plan.legs if leg.valid_until is not None]
        if plan.sessions:
            text += f"  markets {','.join(plan.sessions)}"
        if stamps and min(stamps) < d.valid_until:
            text += f"  (first leg expires {min(stamps):%H:%MZ})"
    return text


@app.command()
def show(
    decision_id: str = typer.Argument(..., help="Decision id (or, with --why, a cycle id)."),
    why: bool = typer.Option(False, "--why", help="Print why each line moved, or did not (the trail)."),
    line: str = typer.Option(None, "--line", help="With --why: only this line."),
    source: str = typer.Option("auto", "--source", help="With --why: auto | journal | ledger."),
    journal_dir: Path = typer.Option(None, "--journal", help="With --why: the journal/ directory."),
) -> None:
    """Show a proposal's legs and, per line with a leg, why it moves (with --why: every line's trail)."""
    if why:
        from council.operator.why import run_why

        raise typer.Exit(run_why(decision_id, line=line, source=source, journal_dir=journal_dir))
    require_operator("show", pinned=OPERATOR_COMMANDS["show"])        # ledger legs: operator terminal only
    from council.context import build_context
    from council.models.plan import Plan
    from council.operator.approve import ApprovalDeps, _screen, why_screen

    ctx = build_context(mode="stub", publish="none")
    d = ctx.ledger.get_decision(decision_id)
    typer.echo(f"{d.decision_id}  {d.kind}  {d.state}  {_deadline(d)}")
    if d.plan:
        plan = Plan.model_validate(d.plan)
        deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=None, write_factory=lambda: None,
                            state_dir=ctx.state_dir, print_fn=typer.echo)
        _screen(plan, deps, drift=0.0, gross=plan.gross_after, deadline=d.valid_until)
        why_screen(d, plan, deps)                         # M5-K: why each line with a leg moves


@app.command()
@operator_command("approve", pinned=True)
def approve(decision_id: str,
            skip: list[str] = typer.Option(None, "--skip",
                                           help="Drop a swing entry by its ref (idea:<k> or trade:<id>); repeatable.")
            ) -> None:
    """Approve and execute a proposal (operator terminal, installed release only)."""
    from council.context import build_context, read_broker
    from council.operator.approve import ApprovalDeps, write_client_factory
    from council.operator.approve import approve as do_approve
    from council.settings import Settings

    settings = Settings.from_env()
    # a rebalance's policy SHA is compared with the policy live cycles run on: the snapshot of the
    # committed HEAD, never the working tree (flatten and compliance execute under it too)
    ctx = build_context(mode="stub", publish="none", settings=settings, policy_from_head=True)
    read = read_broker(settings)
    if read is None:
        typer.echo("refused: no READ token in the keychain", err=True)
        raise typer.Exit(2)
    deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=read,
                        write_factory=write_client_factory(settings), state_dir=ctx.state_dir,
                        print_fn=typer.echo)
    do_approve(decision_id, deps, skip=tuple(skip or ()))


@app.command()
@operator_command("reject", pinned=False)
def reject(decision_id: str, reason: str = typer.Option(..., help="Published with the cycle.")) -> None:
    """Reject a proposal (the reason is published)."""
    from council.context import build_context
    from council.operator.approve import ApprovalDeps
    from council.operator.approve import reject as do_reject

    ctx = build_context(mode="stub", publish="none")
    deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=None, write_factory=lambda: None,
                        state_dir=ctx.state_dir, print_fn=typer.echo)
    do_reject(decision_id, reason, deps)
    typer.echo(f"rejected {decision_id}")


@ops.command("resolve")
@operator_command("ops resolve", pinned=True)
def ops_resolve(
    decision_id: str,
    filled: bool = typer.Option(False, "--filled", help="The held order(s) filled (checked in the broker)."),
    cancelled: bool = typer.Option(False, "--cancelled", help="The held order(s) were cancelled."),
) -> None:
    """Record what happened to orders held for a closed market (operator terminal, ledger only)."""
    from council.operator.approve import ApprovalDeps, resolve_waiting

    if filled == cancelled:
        _refuse("pass exactly one of --filled or --cancelled")
    root, ledger = _ledger_only()          # ledger only: no policy load, no broker, nothing sent
    deps = ApprovalDeps(ledger=ledger, policy=None, read=None, write_factory=_no_writer,
                        state_dir=root, print_fn=typer.echo)
    _refusable(lambda: resolve_waiting(decision_id, "filled" if filled else "cancelled", deps))


@ops.command("review")
@operator_command("ops review", pinned=True)
def ops_review(
    decision_id: str,
    reason: str = typer.Option(..., "--reason", help="Why no action is needed (published: no amounts or ids)."),
) -> None:
    """Clear a blocked decision with no active or waiting leg, after checking the broker (ledger only)."""
    from council.operator.approve import ApprovalDeps, review_blocked

    root, ledger = _ledger_only()          # ledger only: no policy load, no broker, nothing sent
    deps = ApprovalDeps(ledger=ledger, policy=None, read=None, write_factory=_no_writer,
                        state_dir=root, print_fn=typer.echo)
    _refusable(lambda: review_blocked(decision_id, reason, deps))


FEE_LEVELS = ("virtual", "mirror")


@ops.command("attest")
@operator_command("ops attest", pinned=True)
def ops_attest(
    item: str = typer.Argument(..., help="An attestation item, or fee-charged-on=<virtual,mirror>."),
    decision: str = typer.Option("", "--decision", help="The smoke decision a mirror check is about."),
    ref: str = typer.Option("", "--ref", help="etoro-licence: the ticket of eToro's written answer "
                                             "(checked for presence, never stored)."),
    no: bool = typer.Option(False, "--no", help="Record the item as checked and NOT true."),
) -> None:
    """Record a human check (m5-readiness §5, K6/K7/K12/K16): private readiness record `attest`
    (0600), and for mirror checks also `account/capabilities.json`. Codes and booleans only."""
    from council.operator import capabilities, readiness

    name, _, value = item.partition("=")
    if name not in readiness.ATTEST_ITEMS:
        _refuse(f"unknown attestation {name!r}; one of: {', '.join(readiness.ATTEST_ITEMS)}")
    if (name == "fee-charged-on") != bool(value):
        _refuse("fee-charged-on takes =<levels> (e.g. fee-charged-on=virtual,mirror); other items take none")
    if name == "etoro-licence" and not no and not ref.strip():
        _refuse("etoro-licence needs --ref <ticket> of eToro's written answer")
    if name in capabilities.MIRROR_ITEMS and not decision:
        _refuse(f"{name} needs --decision <smoke decision id>")
    head = readiness.Probes.default().head()
    gates: dict[str, dict[str, str]] = {}
    if name == "fee-charged-on":
        gates["K15"] = _fee_location_gate(value)
    try:
        if name in capabilities.MIRROR_ITEMS:
            capabilities.write_mirror_check(name, decision_id=decision, value=not no)
        readiness.write_record("attest", head=head, gates=gates, attested={name: not no})
    except (readiness.ReadinessError, capabilities.CapabilityError) as exc:
        _refuse(str(exc))
    suffix = f" ({gates['K15']['code']})" if gates else ""
    typer.echo(f"attested {name}: {'no' if no else 'yes'}{suffix}")


def _fee_location_gate(value: str) -> dict[str, str]:
    """K15: the attested fee levels against costs.yaml `fixed_commission_charged_on`. Equal = green;
    fewer levels than costs.yaml = amber (conservative until a tagged costs.yaml edit); a level
    costs.yaml does not count = red."""
    from council.policy import Policy
    from council.risk.config import cost_floors

    levels = {v.strip() for v in value.split(",") if v.strip()}
    if not levels or not levels <= set(FEE_LEVELS):
        _refuse(f"fee-charged-on levels must be among {', '.join(FEE_LEVELS)}")
    policy_levels = set(cost_floors(Policy.load()).fixed_commission_charged_on)
    if levels == policy_levels:
        return {"state": "green", "code": "fee_location_matches"}
    if levels < policy_levels:
        return {"state": "amber", "code": "fee_location_conservative"}
    return {"state": "red", "code": "fee_location_uncounted"}


@ops.command("capabilities")
@operator_command("ops capabilities", pinned=True)
def ops_capabilities() -> None:
    """Show the M5-D1 capability gates (private terminal; codes only). A false gate keeps its
    behaviour off; a record without a completed smoke decision with fills shows as unproven."""
    from council.operator import capabilities

    for text in capabilities.report_lines(capabilities.load()):
        typer.echo(text)


@ops.command("assert-operator")
def ops_assert_operator() -> None:
    """Exit 0 in the operator's own terminal, else 2 with every failed rule (for ops scripts)."""
    from council.operator import guards

    try:
        guards.assert_current_process_is_operator()
    except guards.GuardError as exc:
        _refuse(str(exc))
    typer.echo("operator context: ok")


@app.command("resume-exec")
@operator_command("resume-exec", pinned=True)
def resume_exec(decision_id: str) -> None:
    """Recover an interrupted, unknown or blocked execution with broker LOOKUPS only (never sends)."""
    from council.context import build_context, read_broker
    from council.operator.approve import ApprovalDeps
    from council.operator.approve import resume_exec as do_resume
    from council.settings import Settings

    settings = Settings.from_env()
    ctx = build_context(mode="stub", publish="none", settings=settings, policy_from_head=True)
    read = read_broker(settings)
    if read is None:
        _refuse("no READ token in the keychain")
    deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=read, write_factory=_no_writer,
                        state_dir=ctx.state_dir, print_fn=typer.echo)
    _refusable(lambda: do_resume(decision_id, deps))


@app.command("resume")
@operator_command("resume", pinned=True)
def resume(reason: str = typer.Option(..., "--reason", help="Why trading may resume (the peak stays).")) -> None:
    """Leave HALTED/FLAT after recovery. The lifetime peak is unchanged: the next check re-halts
    while equity is still below the halt line."""
    from council.operator.approve import ApprovalDeps, resume_kill_switch

    root, ledger = _ledger_only()
    deps = ApprovalDeps(ledger=ledger, policy=None, read=None, write_factory=_no_writer,
                        state_dir=root, print_fn=typer.echo)
    _refusable(lambda: resume_kill_switch(reason, deps))


def _no_writer() -> NoReturn:
    raise RuntimeError("this command never builds a broker writer")


@app.command()
def doctor(
    live_read: bool = typer.Option(False, "--live-read", help="Probe the broker with the READ token."),
    record_fixtures: bool = typer.Option(False, "--record-fixtures", help="Operator: store the broker's raw "
                                         "payloads privately under state_dir/licensed/fixtures/ (kept 7 days)."),
    ready: bool = typer.Option(False, "--ready", help="Readiness report: every M5 gate with its owner "
                               "(agent, user, token). Read-only; agents may run it."),
    track: str = typer.Option("core", "--track", help="With --ready: core (Track C) or stocks (Track S)."),
    post_token: bool = typer.Option(False, "--post-token", help="With --ready: the token gates must be green."),
    as_json: bool = typer.Option(False, "--json", help="With --ready: {id, owner, state, code, wp} per gate."),
    network: bool = typer.Option(False, "--network", help="With --ready: read origin and CI (git ls-remote, gh)."),
) -> None:
    """Health checks: policy invariants, prompts, keychain entries, Ollama model, disk, jobs.
    `--ready` prints the readiness gates instead (m5-readiness §6) and exits 0 ready, 1 not ready,
    2 on an internal error."""
    import shutil
    import subprocess

    if record_fixtures:                     # the READ token: operator terminal, installed release
        require_operator("doctor --record-fixtures", pinned=OPERATOR_COMMANDS["doctor --record-fixtures"])
        if ready or live_read or post_token or as_json or network or track != "core":
            _refuse("--record-fixtures runs alone")
        _onboarding_record_fixtures()
    if ready:                               # read-only, no broker or LLM import on this path
        if live_read:
            _refuse("--ready and --live-read are separate runs")
        if track not in ("core", "stocks"):
            _refuse("--track must be core or stocks")
        from council.operator.readiness import run_ready

        raise typer.Exit(run_ready(track=track, post_token=post_token, as_json=as_json, network=network,
                                   echo=typer.echo))
    if post_token or as_json or network or track != "core":
        _refuse("--track, --post-token, --json and --network need --ready")

    if live_read:                           # the READ token: operator terminal, installed release
        require_operator("doctor --live-read", pinned=OPERATOR_COMMANDS["doctor --live-read"])

    from council import paths
    from council.invariants import check_policy
    from council.llm.prompts import PromptRegistry
    from council.policy import Policy

    ok = True

    def line(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        typer.echo(f"{'ok ' if good else 'BAD'}  {name}{('  — ' + detail) if detail else ''}")

    policy = Policy.load()
    try:
        check_policy(policy)
        line("policy invariants", True, policy.sha256[:12])
    except Exception as exc:
        line("policy invariants", False, str(exc))
    reg = PromptRegistry()
    line("prompts manifest", bool(reg.manifest()), reg.manifest_sha()[:12])
    free = shutil.disk_usage(paths.state_dir().parent).free / 1024**3
    line("free disk", free >= 3, f"{free:.1f} GiB")
    power_good, power_detail = _power_check()
    line("power: AC sleep", power_good, power_detail)
    for service in ("council-book.tiingo", "council-book.etoro.api-key", "council-book.etoro.read"):
        found = subprocess.run(["security", "find-generic-password", "-s", service, "-a", "council"],
                               capture_output=True).returncode == 0
        line(f"keychain {service}", found or service != "council-book.tiingo",
             "present" if found else "missing (expected until onboarding)")
    from council.operator.mirror import MirrorError, load_mirror

    try:
        mirror_ok = load_mirror(paths.state_dir()) is not None
        mirror_detail = "set" if mirror_ok else "mirror_ratio_missing: the fee uses the policy's assumed ratio"
    except MirrorError:
        mirror_ok, mirror_detail = False, "mirror_ratio_invalid: run `council account set-mirror`"
    line("account mirror ratio", mirror_ok or mirror_detail.startswith("mirror_ratio_missing"), mirror_detail)
    tags = subprocess.run(["ollama", "list"], capture_output=True, text=True)
    line("ollama model", "deepseek-v4.1-flash:cloud" in tags.stdout, "deepseek-v4.1-flash:cloud")
    jobs = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    loaded = [j for j in ("com.fbzz.council.cycle", "com.fbzz.council.watch") if j in jobs]
    line("launchd jobs", True, f"loaded: {loaded or 'none (expected until the token exists)'}")
    if live_read:
        from council.context import read_broker
        from council.settings import Settings

        rb = read_broker(Settings.from_env())
        line("broker READ token", rb is not None)
        if rb is not None:
            ok &= _broker_refusal(lambda: _onboarding_live_read(rb))   # K5–K10, K12, K17 → live-read.json
            from council.clock import utcnow
            from council.stocks.commands import default_repo, doctor_stock_sample

            for name, good, detail in doctor_stock_sample(state_dir=paths.state_dir(), repo=default_repo(),
                                                          broker=rb, now=utcnow()):
                line(name, good, detail)
    raise typer.Exit(0 if ok else 1)


def _power_check() -> tuple[bool, str]:
    """`pmset -g custom`: the Mac must not sleep on AC power (m5-readiness E2), unless the operator
    attested `power-ok`. Read-only; the parser is the readiness probe's."""
    from council.operator.readiness import Probes

    probes = Probes.default()
    sleep = probes.ac_sleep()
    if sleep == 0:
        return True, "AC sleep 0"
    if probes.attested("power-ok"):
        return True, "AC sleep on; accepted by the power-ok attestation"
    if sleep is None:
        return False, "pmset -g custom gave no AC sleep value"
    return False, f"AC sleep {sleep} min: sudo pmset -c sleep 0, or council-op ops attest power-ok"


@app.command()
def verify(cycle_id: str, journal_dir: Path = typer.Option(Path("journal"))) -> None:
    """Re-hash a revealed cycle against its commitment."""
    from council.publish import journal
    from council.publish.commit_reveal import verify as verify_doc

    cycle = json.loads((journal_dir.parent / journal.cycle_path(cycle_id)).read_text())
    reveal = json.loads((journal_dir.parent / journal.reveal_path(cycle_id)).read_text())
    commitment = json.loads((journal_dir.parent / journal.commitment_path(cycle_id)).read_text())
    good = verify_doc(cycle, reveal["salt"], commitment["commitment_sha256"])
    typer.echo("verified" if good else "MISMATCH")
    raise typer.Exit(0 if good else 1)


# --------------------------------------------------------------------------------- keys
SANDBOX_VARIABLES = ("COUNCIL_STATE_DIR", "COUNCIL_KEYCHAIN_FILE", "COUNCIL_ETORO_BASE_URL")


def _key_store_target() -> Path | None:
    """Where eToro items go: None = the real keychains. Outside a marked rehearsal sandbox a
    leftover rehearsal variable refuses (it could redirect the state dir, the keychain or the
    broker); inside one, eToro items go ONLY to COUNCIL_KEYCHAIN_FILE (never the login keychain)."""
    from council.operator import release

    if release.is_marked_sandbox():
        keychain_file = os.environ.get("COUNCIL_KEYCHAIN_FILE", "").strip()
        if not keychain_file:
            _refuse("rehearsal sandbox without COUNCIL_KEYCHAIN_FILE: eToro items are never stored in "
                    "the login keychain under the REHEARSAL marker")
        return Path(keychain_file).expanduser()
    leftover = [name for name in SANDBOX_VARIABLES if name in os.environ]
    if leftover:
        _refuse(f"{', '.join(leftover)} set outside a marked rehearsal sandbox (a leftover rehearsal "
                "variable?): unset it and run the command again")
    return None


@keys.command("init-write-keychain")
@operator_command("keys init-write-keychain", pinned=True)
def keys_init() -> None:
    """Create the separate, auto-locking keychain that holds only the WRITE token."""
    _key_store_target()
    from council.operator.keychain import create_write_keychain

    typer.echo(f"created {create_write_keychain()}")


@keys.command("store-read")
@operator_command("keys store-read", pinned=True)
def keys_store_read() -> None:
    """Store the developer app key and the Agent Portfolio READ token (no echo)."""
    target = _key_store_target()
    from council.operator.keychain import API_KEY_SERVICE, READ_SERVICE, store_token_interactive

    store_token_interactive(API_KEY_SERVICE, target)        # None: the login keychain
    store_token_interactive(READ_SERVICE, target)
    typer.echo("stored")


@keys.command("store-write")
@operator_command("keys store-write", pinned=True)
def keys_store_write() -> None:
    """Store the Agent Portfolio WRITE token in the separate write keychain (no echo)."""
    _key_store_target()                  # refuses a leftover sandbox variable / an unmarked sandbox
    from council.operator.keychain import (
        WRITE_SERVICE,
        store_token_interactive,
        write_keychain_path,
    )

    # always the separate write keychain: inside a marked sandbox that is <sandbox>/council-write
    # .keychain-db (throwaway), never the throwaway READ file, so the rehearsal mirrors token day
    store_token_interactive(WRITE_SERVICE, keychain=write_keychain_path())
    typer.echo("stored")


@keys.command("store")
@operator_command("keys store", pinned=False)
def keys_store(name: str = typer.Argument(..., help="tiingo, fred, alpaca (key id + secret), "
                                          "alpaca-key-id, alpaca-secret, sec-user-agent, gov-user-agent, "
                                          "ntfy-topic (ntfy), healthcheck-url (healthcheck), soak-probe")) -> None:
    """Store one allow-listed non-broker Keychain item (no echo; the value never reaches argv)."""
    from council.operator import keystore
    from council.operator.keychain import KeychainError

    try:
        items = keystore.resolve(name)
    except KeychainError as exc:
        _refuse(str(exc))
    target = _key_store_target()                             # None: the login keychain
    for item in items:
        try:
            keystore.store_item(item, target)
        except KeychainError as exc:
            _refuse(str(exc))
        typer.echo(f"stored {item.service}")


# --------------------------------------------------------------------------------- notify
NOTIFY_TEST_TITLE = "council-book: notification test"
NOTIFY_TEST_BODY = ("If this reached your phone, ntfy works. Next: council-op ops attest ntfy-received")


@notify.command("test")
@operator_command("notify test", pinned=False)
def notify_test() -> None:
    """Send exactly one ntfy message to the configured topic (env, else the Keychain item
    council-book.ntfy-topic). Prints neither the topic nor a delivery error's text."""
    from datetime import time
    from zoneinfo import ZoneInfo

    from council.operator.notify import ApprovalWindow, Notifier
    from council.settings import Settings

    topic = Settings.from_env(keychain=True).ntfy_topic
    if not topic:
        typer.echo("no ntfy topic: council-op keys store ntfy-topic (council-book.ntfy-topic)", err=True)
        raise typer.Exit(1)
    always = ApprovalWindow(tz=ZoneInfo("UTC"), start=time(0), end=time.max)
    notifier = Notifier(topic, window=always, macos=False)
    del topic
    try:
        result = notifier.send(NOTIFY_TEST_TITLE, NOTIFY_TEST_BODY, priority="default")
    except Exception as exc:              # the topic may sit in a transport error: the type only
        typer.echo(f"ntfy delivery failed ({type(exc).__name__})", err=True)
        raise typer.Exit(1) from None
    if result.sent != ("ntfy",):
        typer.echo(f"not sent ({result.reason or 'no channel'})", err=True)
        raise typer.Exit(1)
    typer.echo("sent one test message; if it arrived: council-op ops attest ntfy-received")


# --------------------------------------------------------------------------------- account
@account.command("set-mirror")
@operator_command("account set-mirror", pinned=False)
def account_set_mirror(
    ratio: float = typer.Option(None, "--ratio", help="Real funding / virtual NAV."),
    funding_usd: float = typer.Option(None, "--funding-usd", help="Real funding in USD (with --virtual-nav-usd)."),
    virtual_nav_usd: float = typer.Option(None, "--virtual-nav-usd", help="Agent Portfolio NAV in USD."),
    from_broker: bool = typer.Option(False, "--from-broker", help="With --funding-usd: read the virtual NAV "
                                     "from the Agent Portfolio (READ token; installed release)."),
) -> None:
    """Store the mirror ratio privately (state_dir/account/mirror.json, 0600). It prices the $1 fixed
    fee and the real-dollar trade floor as NAV shares; never published, never sent anywhere.
    `--from-broker` takes the virtual NAV from the Agent Portfolio's virtual balance (gate K12)."""
    from council import paths
    from council.operator.mirror import MirrorError, set_mirror

    if from_broker:
        require_operator("account set-mirror --from-broker",
                         pinned=OPERATOR_COMMANDS["account set-mirror --from-broker"])
        if ratio is not None or virtual_nav_usd is not None or funding_usd is None:
            _refuse("--from-broker takes --funding-usd only (the virtual NAV comes from the broker)")
        _onboarding_set_mirror(funding_usd)
    try:
        config = set_mirror(paths.state_dir(), ratio=ratio, funding_usd=funding_usd,
                            virtual_nav_usd=virtual_nav_usd)
    except MirrorError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"mirror ratio stored ({config.mirror_ratio:.4g}); private, used for fee and trade-size checks")


# --------------------------------------------------------------------------------- onboarding (M5-C)
# Token day (m5-readiness §11.2): `keys verify`, `doctor --live-read`, `account set-mirror
# --from-broker`, `instruments resolve`, `doctor --record-fixtures`. Every one runs behind
# `@operator_command` / `require_operator` (release-pinned), reads through the READ client only and
# records codes in `state_dir/readiness/*.json` (`operator.onboarding`; no amount, id or token).
def _onboarding_transport():
    """The HTTP transport of the WRITE-token GET reader: None = the real network (tests inject)."""
    return None


def _onboarding_policy():
    """The working-tree policy; a committed stock sleeve only once STOCK_SLEEVE_LIVE is True."""
    from council import invariants
    from council.policy import Policy

    return Policy.load(include_sleeve=bool(invariants.STOCK_SLEEVE_LIVE))


def _onboarding_now():
    from council.clock import utcnow

    return utcnow()


def _onboarding_finish(outcome, *, exit_after: bool = True) -> None:
    """Record the outcome's gates (codes only), print its rows, exit 0 (no red) or 1."""
    from council.operator import readiness

    probes = readiness.Probes.default()
    gates = outcome.gates()
    if gates:
        try:
            readiness.write_record(outcome.record, head=probes.head(), gates=gates)
        except readiness.ReadinessError as exc:
            _refuse(f"readiness record not written: {exc}")
    for text in outcome.report_lines():
        typer.echo(text)
    if exit_after:
        raise typer.Exit(0 if outcome.ok else 1)


def _onboarding_feed() -> tuple[bool, bool]:
    """(feed on, LC1 green): the feed switch, and the `etoro-licence` attestation on top of it."""
    from council.operator import readiness

    probes = readiness.Probes.default()
    feed_on = probes.broker_feed_on()
    return feed_on, feed_on and probes.attested("etoro-licence")


def _onboarding_live_read(read_client) -> bool:
    from council import paths
    from council.operator import onboarding

    feed_on, licensed = _onboarding_feed()
    outcome = onboarding.probe_live_read(read_client, policy=_onboarding_policy(), state_dir=paths.state_dir(),
                                         now=_onboarding_now(), feed_on=feed_on, feed_licensed=licensed)
    _onboarding_finish(outcome, exit_after=False)
    return outcome.ok


def _write_token_reader():
    """A READ-client instance holding the WRITE token, for ONE GET of the portfolio list (`keys
    verify`). The write keychain is unlocked for the read and locked again at once."""
    from council.broker.etoro_read import EtoroReadClient
    from council.operator import keychain
    from council.settings import Settings

    keychain.unlock_write_keychain()
    try:
        token = keychain.read_secret(keychain.WRITE_SERVICE, keychain=keychain.write_keychain_path())
        api_key = keychain.read_secret(keychain.API_KEY_SERVICE)
    finally:
        keychain.lock_write_keychain()
    try:
        return EtoroReadClient(api_key, token, base_url=Settings.from_env().etoro_base_url,
                               transport=_onboarding_transport())
    finally:
        del token, api_key


def _broker_refusal(fn):
    """Run a probe: a broker or onboarding error -> "refused: <type>" (never its message: it may
    carry a payload) and exit 2."""
    from council.broker.http import BrokerError
    from council.broker.instruments import InstrumentIdentityChanged
    from council.operator.keychain import KeychainError
    from council.operator.onboarding import OnboardingError

    try:
        return fn()
    except InstrumentIdentityChanged:
        _refuse("instrument identity changed: nothing was written; freeze the instrument and check "
                "the broker (instruments.json is append-only)")
    except OnboardingError as exc:
        _refuse(str(exc))
    except (BrokerError, KeychainError) as exc:
        _refuse(f"broker read failed ({type(exc).__name__})")


@keys.command("verify")
@operator_command("keys verify", pinned=True)
def keys_verify() -> None:
    """Check both tokens (K1–K4): one Agent Portfolio holding council-read and council-write, READ
    scopes read-only, WRITE with trade.real:write, expiry, IP whitelist. Reads the WRITE token once,
    for a GET only. Writes readiness/keys.json and, once K1/K2 pass, account/onboarded.json."""
    from council import paths
    from council.operator import onboarding, readiness

    rb = _read_client(required=True)
    attested = readiness.Probes.default().attested("token-scopes")
    now = _onboarding_now()
    outcome = _broker_refusal(lambda: onboarding.probe_keys(rb, _write_token_reader(), now=now,
                                                            scopes_attested=attested))
    if outcome.onboarded:
        onboarding.write_onboarded(paths.state_dir(), now=now)
        outcome.lines.append("account/onboarded.json written: from now on a missing broker alerts")
    _onboarding_finish(outcome)


@instruments.command("resolve")
@operator_command("instruments resolve", pinned=True)
def instruments_resolve(dry_run: bool = typer.Option(False, "--dry-run", help="Report only; write nothing.")) -> None:
    """Every candidate of every line plus the held instruments in one eligibility batch: the vehicle,
    currency, price unit, whole-unit flag and SL bounds per line (K11, K19) and the size-floor code
    P2. instruments.json is append-only: an identity change refuses and writes nothing."""
    from council import paths
    from council.operator import onboarding

    rb = _read_client(required=True)
    outcome = _broker_refusal(lambda: onboarding.probe_instruments(
        rb, policy=_onboarding_policy(), state_dir=paths.state_dir(), now=_onboarding_now(), dry_run=dry_run))
    if dry_run:
        for text in outcome.report_lines():
            typer.echo(text)
        raise typer.Exit(0 if outcome.ok else 1)
    _onboarding_finish(outcome)


def _onboarding_set_mirror(funding_usd: float) -> None:
    from council import paths
    from council.operator import onboarding
    from council.operator.mirror import MirrorError

    rb = _read_client(required=True)
    try:
        outcome, config = _broker_refusal(lambda: onboarding.mirror_from_broker(
            rb, state_dir=paths.state_dir(), funding_usd=funding_usd, now=_onboarding_now()))
    except MirrorError as exc:
        _refuse(str(exc))
    outcome.lines.append(f"mirror ratio stored from the broker's virtual balance ({config.mirror_ratio:.4g}); private")
    _onboarding_finish(outcome)


def _onboarding_record_fixtures() -> None:
    from council import paths
    from council.operator import onboarding

    rb = _read_client(required=True)
    feed_on, licensed = _onboarding_feed()
    outcome = _broker_refusal(lambda: onboarding.record_fixtures(
        rb, policy=_onboarding_policy(), state_dir=paths.state_dir(), now=_onboarding_now(),
        feed_on=feed_on, feed_licensed=licensed))
    _onboarding_finish(outcome)


# --------------------------------------------------------------------------------- stocks
def _stocks_run(fn):
    """Run a `council stocks` command body: StocksError -> "refused: ..." and exit 2."""
    from council.stocks.commands import StocksError

    try:
        return fn()
    except StocksError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc


def _stocks_outcome(outcome) -> None:
    for text in outcome.report_lines():
        typer.echo(text)
    raise typer.Exit(0 if outcome.ok else 1)


def _read_client(required: bool):
    from council.context import read_broker
    from council.settings import Settings

    rb = read_broker(Settings.from_env())
    if rb is None and required:
        typer.echo("refused: no READ token in the keychain", err=True)
        raise typer.Exit(2)
    return rb


@stocks.command("rank")
def stocks_rank(
    asof: str = typer.Option(None, "--asof", help="Rank date YYYY-MM-DD (default: the latest rule anchor)."),
    no_eligibility: bool = typer.Option(False, "--no-eligibility",
                                        help="Accepted for compatibility: the benchmark never reads the broker."),
) -> None:
    """Rank the SQ-8 paper benchmark (tracked, never traded)."""
    from datetime import date

    if not no_eligibility:                  # kept: the retargeted rank is still an operator command
        require_operator("stocks rank", pinned=OPERATOR_COMMANDS["stocks rank"])

    from council import paths
    from council.benchmark import sq8
    from council.clock import utcnow
    from council.settings import Settings
    from council.stocks import commands, sleeve_file

    day = date.fromisoformat(asof) if asof else sleeve_file.latest_anchor(utcnow().date())

    def body():
        root = paths.state_dir()
        services = commands.live_rank_services(root, Settings.from_env(), eligibility=False, prefetch=False)
        return sq8.run_benchmark_rank(day, services.build_inputs, state_dir=root)

    try:
        result = _stocks_run(body)
    except sq8.BenchmarkError as exc:
        _refuse(str(exc))
    for text in result.report_lines():
        typer.echo(text)


@swing.command("status")
@operator_command("swing status", pinned=False)
def swing_status_cmd(
    asof: str = typer.Option(None, "--asof", help="Status date YYYY-MM-DD (default: today, UTC)."),
) -> None:
    """The swing book on one screen (private: open trades, missing take-profits, waiting ideas,
    the weekly counter, blockers, the Skeptic test, the pause rule and the SQ-8 paper benchmark)."""
    from datetime import date

    from council.clock import utcnow
    from council.swing.status import swing_status

    now = utcnow()
    _root, ledger = _ledger_only()          # ledger only: no policy load, no broker, nothing sent
    today = date.fromisoformat(asof) if asof else now.date()
    for text in swing_status(ledger, today=today, now=now).lines():
        typer.echo(text)
    from council.swing import brake

    for text in brake.status_lines(brake.load(ledger)):
        typer.echo(text)


@swing.command("brake")
@operator_command("swing brake", pinned=True)
def swing_brake_cmd(
    lift: bool = typer.Option(False, "--lift", help="Lift the pause (after review). Without it: show the state."),
    canary: bool = typer.Option(False, "--canary", help="The Skeptic canary pause instead of the S15 brake."),
    reason: str = typer.Option("", "--reason", help="Why it is lifted (published: no amounts, ids or links)."),
) -> None:
    """The swing book's pauses (S15 brake and the Skeptic canary pause): show them, or lift one after
    review (ledger only; the reason is published on a public ops row, never a number)."""
    from council.clock import utcnow
    from council.swing import brake

    _root, ledger = _ledger_only()          # ledger only: no policy load, no broker, nothing sent
    if not lift:
        if reason or canary:
            _refuse("--reason and --canary go with --lift")
        for text in brake.status_lines(brake.load(ledger)):
            typer.echo(text)
        return
    which = "canary" if canary else "s15"
    try:
        brake.lift_in_ledger(ledger, which, reason, utcnow())
    except brake.BrakeError as exc:
        _refuse(str(exc))
    typer.echo(f"lifted the {'canary pause' if canary else 'S15 brake'}; the watch publishes the reason. "
               "New swing entries resume at the next swing slot unless the pause engages again.")


@stocks.command("onboard")
@operator_command("stocks onboard", pinned=True)
def stocks_onboard() -> None:
    """After the sleeve is committed and tagged: resolve its instruments and re-run the broker gate."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True)
    _stocks_outcome(_stocks_run(lambda: commands.run_onboard(
        state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb, now=utcnow())))


@stocks.command("adopt")
@operator_command("stocks adopt", pinned=True)
def stocks_adopt(
    instrument_id: int,
    kind: str = typer.Option(None, "--kind", help="credit | rename | delisted (when the facts fit several)."),
    cik: str = typer.Option(None, "--cik", help="The company's SEC CIK when SEC does not know the ticker."),
    sector: str = typer.Option(None, "--sector", help="FF12 sector when the SIC code is unknown."),
) -> None:
    """Propose the sleeve-file edit for a corporate action on an instrument (credit, rename, delisting)."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True)
    _stocks_outcome(_stocks_run(lambda: commands.run_adopt(
        instrument_id, state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb,
        company_lookup=commands.sec_company_lookup(), now=utcnow(), kind=kind, cik=cik, sector=sector)))


@stocks.command("status")
@operator_command("stocks status", pinned=True)
def stocks_status(live_read: bool = typer.Option(False, "--live-read",
                                                 help="Check retiring lines against a READ snapshot.")) -> None:
    """Tag state, roles, unchecked lines, retiring flatness, corporate actions, budgets, fee drag."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True) if live_read else None
    _stocks_outcome(_stocks_run(lambda: commands.run_status(
        state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb, now=utcnow())))


@stocks.command("prune")
@operator_command("stocks prune", pinned=True)
def stocks_prune() -> None:
    """Propose moving flat, untouched retiring lines to the retired registry."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True)
    _stocks_outcome(_stocks_run(lambda: commands.run_prune(
        state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb, now=utcnow())))


# --------------------------------------------------------------------------------- smoke
# M5-D2 (m5-readiness §8): a smoke ticket is proposed here and executed only by `approve` (every
# re-check); the watch publishes its weightless ops row. READ token only; nothing here writes to
# the broker.
def _smoke_deps(*, read_required: bool):
    from council.context import build_context
    from council.operator.smoke import SmokeDeps
    from council.settings import Settings

    settings = Settings.from_env()
    ctx = build_context(mode="stub", publish="none", settings=settings, policy_from_head=True)
    return SmokeDeps(ledger=ctx.ledger, policy=ctx.policy, read=_read_client(required=read_required),
                     state_dir=ctx.state_dir, print_fn=typer.echo)


def _smoke_run(fn):
    from council.operator.smoke import SmokeRefused

    try:
        return _broker_refusal(fn)
    except SmokeRefused as exc:
        _refuse(str(exc))


@smoke.command("propose")
@operator_command("smoke propose", pinned=True)
def smoke_propose(
    step: str = typer.Argument(..., help="S1 … S7 (see `council smoke status`)."),
    preview: bool = typer.Option(False, "--preview", help="Print the exact write request; write nothing."),
) -> None:
    """Propose one minimum-size smoke ticket (refused while anything is pending, blocked, not NORMAL,
    or while the launchd jobs are loaded). Approve it with `council-op approve <id>`."""
    from council.operator import smoke as sm

    deps = _smoke_deps(read_required=True)
    _smoke_run(lambda: sm.propose(step, deps, preview=preview))


@smoke.command("verify")
@operator_command("smoke verify", pinned=True)
def smoke_verify(decision_id: str) -> None:
    """Automatic checks of a completed smoke ticket; all green records the step's capability."""
    from council.operator import smoke as sm

    deps = _smoke_deps(read_required=True)
    checks = _smoke_run(lambda: sm.verify(decision_id, deps))
    raise typer.Exit(0 if checks and all(c.ok for c in checks) else 1)


@smoke.command("status")
@operator_command("smoke status", pinned=True)
def smoke_status() -> None:
    """Every step's latest ticket, K20 (no smoke ticket pending, no smoke position open), K14 and S3;
    records them in readiness/smoke.json."""
    from council.operator import readiness
    from council.operator import smoke as sm

    deps = _smoke_deps(read_required=False)
    result = _smoke_run(lambda: sm.status(deps))
    try:
        readiness.write_record("smoke", head=readiness.Probes.default().head(), gates=result.gates)
    except readiness.ReadinessError as exc:
        _refuse(f"readiness record not written: {exc}")
    raise typer.Exit(0 if result.gates["K20"]["state"] == "green" else 1)


# --------------------------------------------------------------------------------- site
@site.command("build")
def site_build(out: Path = typer.Option(Path("_site")), journal_dir: Path = typer.Option(Path("journal"))) -> None:
    """Build the static site from journal/ (what CI deploys to Pages)."""
    import runpy

    from council import paths

    mod = runpy.run_path(str(paths.REPO_ROOT / "site" / "build.py"))
    code = mod["main"](["--journal", str(journal_dir), "--out", str(out)])
    raise typer.Exit(code or 0)


# --------------------------------------------------------------------------------- rehearse
@rehearse.command("fake-broker")
def rehearse_fake_broker(
    scenario_name: str = typer.Option("onboarding", "--scenario", help="The FakeEtoro scenario (onboarding)."),
    port: int = typer.Option(0, "--port", help="127.0.0.1 port (0 = any free port)."),
) -> None:
    """Serve the onboarding FakeEtoro on 127.0.0.1 inside the marked sandbox COUNCIL_STATE_DIR (L2 dress
    rehearsal). Writes <sandbox>/fake-broker.port and <sandbox>/rehearsal-tokens.txt (three FAKE
    tokens, 0600) and serves until interrupted. Refuses the real state dir and an unmarked one."""
    import signal
    import threading
    from datetime import UTC, datetime

    from council import paths
    from council.broker.fake_server import FakeBrokerServer, FakeServerError
    from council.operator.release import is_marked_sandbox
    from council.rehearsal import scenario

    if scenario_name != "onboarding":
        _refuse(f"unknown scenario {scenario_name!r} (onboarding)")
    state = paths.state_dir()
    if "COUNCIL_STATE_DIR" not in os.environ or not is_marked_sandbox(state):
        _refuse("the fake broker runs only with COUNCIL_STATE_DIR set to a marked rehearsal sandbox")
    tokens = scenario.Tokens.fresh()
    fake = scenario.build_fake(tokens, lambda: datetime.now(UTC), now=datetime.now(UTC))
    token_file = state / "rehearsal-tokens.txt"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"app-key {tokens.app}\nread {tokens.read}\nwrite {tokens.write}\n")
    try:
        server = FakeBrokerServer(fake, state, port=port).start()
    except (FakeServerError, OSError) as exc:
        _refuse(f"fake broker not started ({type(exc).__name__})")
    typer.echo(f"fake broker on {server.base_url} (sandbox only); stop with Ctrl-C")
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        token_file.unlink(missing_ok=True)


@rehearse.command("onboarding")
def rehearse_onboarding(keep: bool = typer.Option(False, "--keep", help="Keep the throwaway sandbox.")) -> None:
    """Run the automated token day (L1 steps) in a throwaway sandbox: fake keychain, loopback fake
    broker, stub model, local bare remote. Touches no real keychain, state dir, remote or network."""
    import tempfile

    from council.rehearsal import onboarding as ob
    from council.rehearsal import security as fake_security

    root = Path(tempfile.mkdtemp(prefix="council-rehearsal-"))
    saved = {name: os.environ.get(name) for name in (*SANDBOX_VARIABLES, "COUNCIL_MODE", "COUNCIL_ROLE")}
    box = ob.Sandbox.create(root)
    undo = fake_security.install(box.security)
    try:
        box.start_broker()
        box.activate(os.environ.__setitem__)
        os.environ["COUNCIL_ROLE"] = "dev"
        results = ob.run_all(box)
    finally:
        undo()
        box.stop()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if not keep:
            ob.remove(box)
    for line in box.log:
        typer.echo(line)
    ok = bool(results) and all(r.ok for r in results) and results[-1].step == "leak scan"
    typer.echo(("rehearsal passed" if ok else "rehearsal FAILED") + (f"; sandbox kept at {root}" if keep else ""))
    raise typer.Exit(0 if ok else 1)


@ops.command("record-dress")
@operator_command("ops record-dress", pinned=True)
def ops_record_dress(
    sandbox: Path = typer.Option(..., "--sandbox", help="The dress rehearsal's sandbox state dir."),
) -> None:
    """Record the human dress rehearsal (gate O5): checks the sandbox read-only (marked, keys verify
    green, a completed smoke ticket and a completed council decision) and writes readiness/dress.json
    in the real state dir. The only file the dress rehearsal writes outside its sandbox."""
    from council import paths
    from council.operator import readiness
    from council.operator.release import default_state_dir
    from council.rehearsal.dress import evidence

    if paths.state_dir().expanduser().resolve() != default_state_dir().expanduser().resolve():
        _refuse("run record-dress from your own terminal, outside the [REHEARSAL] shell")
    counts, problems = evidence(sandbox)
    gate = ({"state": "green", "code": "dress_ok"} if not problems
            else {"state": "red", "code": "dress_failed:" + problems[0]})   # one code per gate
    try:
        readiness.write_record("dress", head=readiness.Probes.default().head(), gates={"O5": gate})
    except readiness.ReadinessError as exc:
        _refuse(f"readiness record not written: {exc}")
    typer.echo(f"{gate['state']} O5 {gate['code']} (smoke {counts['smoke']}, council {counts['council']})")
    for problem in problems[1:]:
        typer.echo(f"  also: {problem}")
    raise typer.Exit(0 if not problems else 1)


@app.command()
def prompts() -> None:
    """Rewrite prompts/manifest.json (ids and sha256)."""
    from council import paths
    from council.llm.prompts import PromptRegistry

    PromptRegistry().write_manifest(paths.PROMPTS_DIR / "manifest.json")
    typer.echo("prompts/manifest.json updated")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
