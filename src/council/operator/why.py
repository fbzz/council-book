"""`council why <cycle> [<line>]` and `council show <id> --why`: why each line moved, or did not.

Prints the per-line decision trail (`council.publish.trail`): for every line that moved, that
someone asked to move or that code could have moved, each stage from the reference to execution,
and the first gate that stopped a change nobody made; one sentence for the lines nobody asked about.

Sources (`--source`):
  journal  the REVEALED public documents: `journal/cycles/…/<cycle>.json`, its execution file and
           its ops row (the final decision). Agent-safe: public models only, nothing licensed.
  ledger   the PRIVATE ledger record plus the ledger's current decision, leg and execution state.
           It also holds what is not published yet (the structured drops, the bands before the
           analysts' cards, the medoid's fall-back lines, claim-to-line tags). Operator terminal only.
  auto     (default) the ledger in the operator's terminal, otherwise the journal.

An unrevealed cycle has no public document, and its trail would show the council's leanings before
the human decision, so outside the operator's terminal it is refused (exit 2) before anything is
read. Both sources print the same fixed words (engine notes and plan skips go through
`publish.trace_rules`, so no size floor, fee or broker-derived number is ever shown).
This module reads files only; it never imports the broker, the writer or the gateway.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from council.publish import trail

CYCLE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}Z")
SOURCES = ("auto", "journal", "ledger")
PRIVATE_NOTE = "private: built from the ledger; share it only once the cycle is revealed"


class WhyError(ValueError):
    pass


def cycle_of(identifier: str) -> str:
    """The cycle id of a cycle id or a decision id (`<cycle>-<kind>-<hex>`)."""
    m = CYCLE_ID.match((identifier or "").strip())
    if m is None:
        raise WhyError(f"not a cycle or decision id: {identifier!r}")
    return m.group(0)


def operator_context_ok() -> bool:
    """True in the human operator's interactive terminal (`guards`); never raises."""
    from council.operator import guards

    try:
        guards.assert_current_process_is_operator()
    except Exception:
        return False
    return True


def line_names() -> dict[str, str]:
    """Line symbol -> plain name from the policy (cosmetic; empty when the policy does not load)."""
    try:
        from council.policy import default_policy

        return {ln.symbol: ln.name for ln in default_policy().universe.lines}
    except Exception:
        return {}


# ---------------------------------------------------------------------------------- journal
def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ops_row(root: Path, cycle_id: str) -> dict[str, Any] | None:
    """The cycle's latest ops row (it carries the final decision), or None."""
    from council.publish import journal

    path = root / journal.OPS_PATH
    if not path.is_file():
        return None
    found = None
    for text in path.read_text(encoding="utf-8").splitlines():
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if row.get("cycle_id") == cycle_id:
            found = row
    return found


def journal_trails(journal_dir: Path, cycle_id: str, *,
                   names: Mapping[str, str] | None = None) -> list[trail.LineTrail] | None:
    """The trails from the revealed public documents under `journal_dir`, or None when the cycle
    is not revealed there (its cycle document is written only at the reveal)."""
    from council.publish import journal

    root = Path(journal_dir).parent
    path = root / journal.cycle_path(cycle_id)
    if not path.is_file():
        return None
    execution_path = root / journal.execution_path(cycle_id)
    execution = _json(execution_path) if execution_path.is_file() else None
    return trail.trails(_json(path), execution, ops_row(root, cycle_id), names=names)


# ---------------------------------------------------------------------------------- ledger
def ledger_state(ledger: Any, decision_id: str | None) -> dict[str, Any]:
    """The decision's current state, the operator's reason, the approval time, each line's leg
    states and the achieved weights, read from the ledger (empty when there is no decision)."""
    out: dict[str, Any] = {}
    if not decision_id:
        return out
    try:
        decision = ledger.get_decision(decision_id)
    except Exception:
        return out
    out["decision_state"] = decision.state
    for event in ledger.events(decision_id):
        if event.get("to_state") == "rejected" and event.get("reason"):
            out["reason"] = str(event["reason"])
        if event.get("to_state") == "approved":
            out["approved_at"] = event.get("created_at")
    states: dict[str, list[str]] = {}
    for row in ledger.legs(decision_id):
        states.setdefault(row.line, []).append(row.state)
    out["leg_states"] = states
    report = ledger.get_runtime(f"exec_report:{decision_id}") or {}
    reconcile = ((report.get("report") or {}).get("reconcile") or {}) if isinstance(report, dict) else {}
    achieved = reconcile.get("achieved_w") if isinstance(reconcile, dict) else None
    if isinstance(achieved, dict):
        out["achieved_x"] = {str(k): float(v) for k, v in achieved.items()}
    return out


def ledger_trails(state_dir: Path, cycle_id: str, *,
                  names: Mapping[str, str] | None = None) -> list[trail.LineTrail] | None:
    """The trails from the PRIVATE ledger record (operator only), or None without a record."""
    from council.ledger.db import LEDGER_FILE, Ledger
    from council.models.cycle import CycleRecord

    path = Path(state_dir) / LEDGER_FILE
    if not path.is_file():
        return None
    ledger = Ledger(path)
    raw = ledger.get_cycle(cycle_id)
    if raw is None:
        return None
    rec = CycleRecord.model_validate(raw)
    return trail.record_trails(rec, names=names, **ledger_state(ledger, rec.decision_id))


# ---------------------------------------------------------------------------------- command
def run_why(
    identifier: str,
    *,
    line: str | None = None,
    source: str = "auto",
    journal_dir: Path | None = None,
    state_dir: Path | None = None,
    echo: Callable[[str], Any] = typer.echo,
) -> int:
    """Print the trails of one cycle (or one line of it). Returns the exit code: 0 printed,
    1 nothing found, 2 refused or bad arguments."""
    from council import paths

    if source not in SOURCES:
        echo(f"error: --source must be one of {', '.join(SOURCES)}")
        return 2
    try:
        cycle_id = cycle_of(identifier)
    except WhyError as exc:
        echo(f"error: {exc}")
        return 2
    operator = source != "journal" and operator_context_ok()
    if source == "ledger" and not operator:
        from council.operator import guards

        try:
            guards.assert_current_process_is_operator()
        except guards.GuardError as exc:
            echo(f"refused: {exc}")
            return 2
    names = line_names()
    journal_root = Path(journal_dir) if journal_dir is not None else paths.JOURNAL_DIR
    found: list[trail.LineTrail] | None = None
    header = ""
    if operator:
        found = ledger_trails(Path(state_dir) if state_dir is not None else paths.state_dir(), cycle_id,
                              names=names)
        if found is not None:
            header = f"{cycle_id} · why each line moved · source: ledger\n{PRIVATE_NOTE}"
        elif source == "ledger":
            echo(f"no ledger record for cycle {cycle_id}")
            return 1
    if found is None:
        found = journal_trails(journal_root, cycle_id, names=names)
        if found is None:
            if operator:
                echo(f"cycle {cycle_id} has no ledger record and is not revealed in {journal_root}")
                return 1
            echo(f"refused: cycle {cycle_id} is not revealed in {journal_root}; before the reveal its "
                 "trail is shown only in the operator's terminal")
            return 2
        header = f"{cycle_id} · why each line moved · source: revealed public record"
    if line is not None and line not in {t.line for t in found}:
        echo(f"no line {line} in cycle {cycle_id}")
        return 1
    echo(trail.render_text(found, header=header, line=line).rstrip("\n"))
    return 0


def why_command(
    cycle_id: Annotated[str, typer.Argument(help="Cycle id (e.g. 2026-10-01T1440Z) or decision id.")],
    line: Annotated[str | None, typer.Argument(help="Only this line (e.g. SEMIS).")] = None,
    source: Annotated[str, typer.Option(help="auto | journal (revealed, agent-safe) | ledger (operator).")] = "auto",
    journal_dir: Annotated[Path | None, typer.Option("--journal", help="The journal/ directory.")] = None,
    rehearsal: Annotated[bool, typer.Option("--rehearsal", help="Read the rehearsal ledger.")] = False,
) -> None:
    """Why each line moved, or did not: the per-line decision trail from reference to execution."""
    from council import paths

    state = paths.state_dir() / "rehearsal" if rehearsal else None
    raise typer.Exit(run_why(cycle_id, line=line, source=source, journal_dir=journal_dir, state_dir=state))


def register(app: typer.Typer) -> None:
    """Add `council why` to the main CLI (`council show <id> --why` calls `run_why` directly)."""
    app.command("why")(why_command)


__all__ = [
    "PRIVATE_NOTE", "WhyError", "cycle_of", "journal_trails", "ledger_state", "ledger_trails",
    "operator_context_ok", "register", "run_why", "why_command",
]
