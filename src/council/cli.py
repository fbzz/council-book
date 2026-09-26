"""`council` command line. Imports are lazy on purpose: the unattended runner must never import the
broker writer, and only `approve`/`flatten` (operator terminal) can reach it."""

from __future__ import annotations

import json
from pathlib import Path

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
app.add_typer(keys, name="keys")
app.add_typer(site, name="site")
app.add_typer(ops, name="ops")
app.add_typer(account, name="account")
app.add_typer(stocks, name="stocks")


def _ctx(*, mode: str, stub_llm: bool = False, publish: str = "preview"):
    from council.context import build_context

    return build_context(mode=mode, stub_llm=stub_llm, publish=publish)  # type: ignore[arg-type]


@app.command()
def cycle(
    slot: str = typer.Option("auto", help="'auto' runs the due slot (late <= 120 min)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No pushes, no notifications; publish to site-preview/."),
    rehearsal: bool = typer.Option(False, "--rehearsal", help="No broker: run the council and PUBLISH the cycle labelled REHEARSAL (own ledger, nothing traded)."),
    stub_llm: bool = typer.Option(False, "--stub-llm", help="Canned replies that hold the reference (no model calls)."),
    force: bool = typer.Option(False, help="Re-run a slot that already has a record."),
) -> None:
    """Run the council cycle for the current 4-hour slot."""
    from council.cycle import run_cycle
    from council.settings import Settings

    settings = Settings.from_env()
    if rehearsal:
        from council import paths
        from council.context import build_context

        ctx = build_context(mode="dry_run", stub_llm=stub_llm, publish="push",
                            state_dir=paths.state_dir() / "rehearsal",
                            publisher_dir=paths.state_dir() / "publisher-clone")
    elif dry_run or settings.mode != "live":
        ctx = _ctx(mode="dry_run", stub_llm=stub_llm, publish="preview")
    else:
        ctx = _ctx(mode="live", stub_llm=stub_llm, publish="push")
    outcome = run_cycle(ctx, force=force)
    typer.echo(json.dumps(outcome.__dict__, default=str, indent=1))


@app.command()
def watch() -> None:
    """Read-only watch: expiries, reveals, execution records, kill switch, heartbeat."""
    from council.settings import Settings
    from council.watch import run_watch

    settings = Settings.from_env()
    ctx = _ctx(mode="live" if settings.mode == "live" else "dry_run",
               publish="push" if settings.mode == "live" else "preview")
    out = run_watch(ctx)
    typer.echo(json.dumps(out.__dict__, default=str, indent=1))


@app.command()
def inbox() -> None:
    """Pending proposals."""
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
def show(decision_id: str) -> None:
    """Show a proposal's legs in percent and x."""
    from council.context import build_context
    from council.models.plan import Plan
    from council.operator.approve import ApprovalDeps, _screen

    ctx = build_context(mode="stub", publish="none")
    d = ctx.ledger.get_decision(decision_id)
    typer.echo(f"{d.decision_id}  {d.kind}  {d.state}  {_deadline(d)}")
    if d.plan:
        plan = Plan.model_validate(d.plan)
        deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=None, write_factory=lambda: None,
                            state_dir=ctx.state_dir, print_fn=typer.echo)
        _screen(plan, deps, drift=0.0, gross=plan.gross_after, deadline=d.valid_until)


@app.command()
def approve(decision_id: str) -> None:
    """Approve and execute a proposal (operator terminal only)."""
    from council.context import build_context, read_broker
    from council.operator.approve import ApprovalDeps, write_client_factory
    from council.operator.approve import approve as do_approve
    from council.settings import Settings

    settings = Settings.from_env()
    if settings.role != "operator":
        typer.echo("refused: COUNCIL_ROLE must be 'operator' (use the operator terminal)", err=True)
        raise typer.Exit(2)
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
    do_approve(decision_id, deps)


@app.command()
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
def ops_resolve(
    decision_id: str,
    filled: bool = typer.Option(False, "--filled", help="The held order(s) filled (checked in the broker)."),
    cancelled: bool = typer.Option(False, "--cancelled", help="The held order(s) were cancelled."),
) -> None:
    """Record what happened to orders held for a closed market (operator terminal, ledger only)."""
    from council.operator.approve import ApprovalDeps, resolve_waiting
    from council.settings import Settings

    if filled == cancelled:
        typer.echo("refused: pass exactly one of --filled or --cancelled", err=True)
        raise typer.Exit(2)
    settings = Settings.from_env()
    if settings.role != "operator":
        typer.echo("refused: COUNCIL_ROLE must be 'operator' (use the operator terminal)", err=True)
        raise typer.Exit(2)
    root, ledger = _ledger_only()          # ledger only: no policy load, no broker, nothing sent
    deps = ApprovalDeps(ledger=ledger, policy=None, read=None, write_factory=lambda: None,
                        state_dir=root, print_fn=typer.echo)
    resolve_waiting(decision_id, "filled" if filled else "cancelled", deps)


@app.command()
def doctor(live_read: bool = typer.Option(False, "--live-read", help="Probe the broker with the READ token.")) -> None:
    """Health checks: policy invariants, prompts, keychain entries, Ollama model, disk, jobs."""
    import shutil
    import subprocess

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
            try:
                rb.pnl()
                line("broker pnl read", True)
            except Exception as exc:
                line("broker pnl read", False, type(exc).__name__)
            from council.clock import utcnow
            from council.stocks.commands import default_repo, doctor_stock_sample

            for name, good, detail in doctor_stock_sample(state_dir=paths.state_dir(), repo=default_repo(),
                                                          broker=rb, now=utcnow()):
                line(name, good, detail)
    raise typer.Exit(0 if ok else 1)


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
def _operator_only() -> None:
    from council.settings import Settings

    if Settings.from_env().role != "operator":
        typer.echo("refused: COUNCIL_ROLE must be 'operator'", err=True)
        raise typer.Exit(2)


@keys.command("init-write-keychain")
def keys_init() -> None:
    """Create the separate, auto-locking keychain that holds only the WRITE token."""
    _operator_only()
    from council.operator.keychain import create_write_keychain

    typer.echo(f"created {create_write_keychain()}")


@keys.command("store-read")
def keys_store_read() -> None:
    """Store the developer app key and the Agent Portfolio READ token (no echo)."""
    _operator_only()
    from council.operator.keychain import API_KEY_SERVICE, READ_SERVICE, store_token_interactive

    store_token_interactive(API_KEY_SERVICE)
    store_token_interactive(READ_SERVICE)
    typer.echo("stored")


@keys.command("store-write")
def keys_store_write() -> None:
    """Store the Agent Portfolio WRITE token in the separate write keychain (no echo)."""
    _operator_only()
    from council.operator.keychain import (
        WRITE_SERVICE,
        store_token_interactive,
        write_keychain_path,
    )

    store_token_interactive(WRITE_SERVICE, keychain=write_keychain_path())
    typer.echo("stored")


# --------------------------------------------------------------------------------- account
@account.command("set-mirror")
def account_set_mirror(
    ratio: float = typer.Option(None, "--ratio", help="Real funding / virtual NAV."),
    funding_usd: float = typer.Option(None, "--funding-usd", help="Real funding in USD (with --virtual-nav-usd)."),
    virtual_nav_usd: float = typer.Option(None, "--virtual-nav-usd", help="Agent Portfolio NAV in USD."),
) -> None:
    """Store the mirror ratio privately (state_dir/account/mirror.json, 0600). It prices the $1 fixed
    fee and the real-dollar trade floor as NAV shares; never published, never sent anywhere."""
    _operator_only()
    from council import paths
    from council.operator.mirror import MirrorError, set_mirror

    try:
        config = set_mirror(paths.state_dir(), ratio=ratio, funding_usd=funding_usd,
                            virtual_nav_usd=virtual_nav_usd)
    except MirrorError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"mirror ratio stored ({config.mirror_ratio:.4g}); private, used for fee and trade-size checks")


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
                                        help="Skip the broker gate (lines stay unchecked; live runs refuse them)."),
    policy_overlay: Path = typer.Option(None, "--policy-overlay",
                                        help="Directory of go-live drafts (e.g. the re-based universe.yaml) "
                                             "validated on top of the committed policy."),
    ai_list: Path = typer.Option(None, "--ai-list", help="AI-adjacent list when policy/ has none yet."),
    allow_off_anchor: bool = typer.Option(False, "--allow-off-anchor", help="Rank on a non-anchor date."),
    no_prefetch: bool = typer.Option(False, "--no-prefetch", help="Skip the history prefetch for new names."),
) -> None:
    """Rank the universe and write a proposed stock-sleeve.yaml under the state dir (never policy/)."""
    from datetime import date

    from council import paths
    from council.clock import utcnow
    from council.settings import Settings
    from council.stocks import commands, sleeve_file

    day = date.fromisoformat(asof) if asof else sleeve_file.latest_anchor(utcnow().date())

    def body():
        root = paths.state_dir()
        services = commands.live_rank_services(root, Settings.from_env(), eligibility=not no_eligibility,
                                               prefetch=not no_prefetch)
        return commands.run_rank(day, services, state_dir=root, repo=commands.default_repo(),
                                 eligibility=not no_eligibility, overlay_dir=policy_overlay, ai_list=ai_list,
                                 allow_off_anchor=allow_off_anchor)

    _stocks_outcome(_stocks_run(body))


@stocks.command("onboard")
def stocks_onboard() -> None:
    """After the sleeve is committed and tagged: resolve its instruments and re-run the broker gate."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True)
    _stocks_outcome(_stocks_run(lambda: commands.run_onboard(
        state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb, now=utcnow())))


@stocks.command("adopt")
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
def stocks_prune() -> None:
    """Propose moving flat, untouched retiring lines to the retired registry."""
    from council import paths
    from council.clock import utcnow
    from council.stocks import commands

    rb = _read_client(required=True)
    _stocks_outcome(_stocks_run(lambda: commands.run_prune(
        state_dir=paths.state_dir(), repo=commands.default_repo(), broker=rb, now=utcnow())))


# --------------------------------------------------------------------------------- site
@site.command("build")
def site_build(out: Path = typer.Option(Path("_site")), journal_dir: Path = typer.Option(Path("journal"))) -> None:
    """Build the static site from journal/ (what CI deploys to Pages)."""
    import runpy

    from council import paths

    mod = runpy.run_path(str(paths.REPO_ROOT / "site" / "build.py"))
    code = mod["main"](["--journal", str(journal_dir), "--out", str(out)])
    raise typer.Exit(code or 0)


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
