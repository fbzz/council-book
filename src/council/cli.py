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
app.add_typer(keys, name="keys")
app.add_typer(site, name="site")


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
    from council.context import build_context

    ctx = build_context(mode="stub", publish="none")
    for d in ctx.ledger.pending():
        typer.echo(f"{d.decision_id}  {d.kind:<10}  {d.state:<20}  valid until {d.valid_until:%Y-%m-%d %H:%MZ}")


@app.command()
def show(decision_id: str) -> None:
    """Show a proposal's legs in percent and x."""
    from council.context import build_context
    from council.models.plan import Plan
    from council.operator.approve import ApprovalDeps, _screen

    ctx = build_context(mode="stub", publish="none")
    d = ctx.ledger.get_decision(decision_id)
    typer.echo(f"{d.decision_id}  {d.kind}  {d.state}  valid until {d.valid_until:%Y-%m-%d %H:%MZ}")
    if d.plan:
        plan = Plan.model_validate(d.plan)
        deps = ApprovalDeps(ledger=ctx.ledger, policy=ctx.policy, read=None, write_factory=lambda: None,
                            state_dir=ctx.state_dir, print_fn=typer.echo)
        _screen(plan, deps, drift=0.0, gross=plan.gross_after)


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
    ctx = build_context(mode="stub", publish="none", settings=settings)
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
