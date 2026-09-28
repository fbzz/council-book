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

`decision_why` (M5-K) is the operator's screen for `council show <decision>` and the approval
screen: per line with a leg, the same trail over the SEALED, not yet revealed public document in
`state_dir/salts/` (verified against its commitment), then the ledger legs in percent and x. After
the reveal its trail blocks equal `council why --source journal` on the same document.

An unrevealed cycle has no public document, and its trail would show the council's leanings before
the human decision, so outside the operator's terminal it is refused (exit 2) before anything is
read. Both sources print the same fixed words (engine notes and plan skips go through
`publish.trace_rules`, so no size floor, fee or broker-derived number is ever shown).
Swing book (swing-book.md §7.1): `council why <cycle> <TICKER>` also prints the swing chain of
an idea or trade on that ticker (`council.swing.trail`): scout -> code gate -> skeptic -> debate
-> PM votes -> S-rules -> engine -> plan -> approval -> fill -> exits, from the private swing record
in the operator's terminal, else from the revealed public swing section.
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
    execution, ops = journal_outcome(journal_dir, cycle_id)
    return trail.trails(_json(path), execution, ops, names=names)


def journal_outcome(journal_dir: Path, cycle_id: str) -> tuple[Any, Any]:
    """The cycle's public execution file and latest ops row under `journal_dir` (None when absent):
    what carries the decision outcome, for the revealed and the sealed document alike."""
    from council.publish import journal

    root = Path(journal_dir).parent
    execution_path = root / journal.execution_path(cycle_id)
    execution = _json(execution_path) if execution_path.is_file() else None
    return execution, ops_row(root, cycle_id)


# ---------------------------------------------------------------------------------- sealed
def sealed_document(state_dir: Path, cycle_id: str) -> Any | None:
    """PRIVATE until the reveal: the exact sealed public cycle document kept in
    `state_dir/salts/<cycle>.json`, or None when there is none. Raises WhyError when the kept
    bytes and salt no longer open their commitment (a tampered or corrupt seal is never shown)."""
    from council.publish import commit_reveal

    try:
        sealed = commit_reveal.load_sealed(cycle_id, Path(state_dir) / "salts")
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise WhyError(f"sealed document for {cycle_id} is unreadable ({type(exc).__name__})") from exc
    if not commit_reveal.verify_bytes(sealed.sealed_bytes, sealed.salt, sealed.commitment_sha):
        raise WhyError(f"sealed document for {cycle_id} does not open its commitment")
    doc = json.loads(sealed.sealed_bytes)
    if doc.get("cycle_id") != cycle_id:
        raise WhyError(f"sealed document for {cycle_id} names cycle {doc.get('cycle_id')!r}")
    return doc


def sealed_trails(state_dir: Path, journal_dir: Path, cycle_id: str, *,
                  names: Mapping[str, str] | None = None) -> list[trail.LineTrail] | None:
    """The trails of the sealed, not yet revealed public document (M5-K), with the same public
    outcome files `journal_trails` reads, so after the reveal the two are identical. Public models
    only (the document passed the allow-list when it was sealed); shown only in the operator's
    terminal because it shows the council's leanings before the human decision."""
    doc = sealed_document(state_dir, cycle_id)
    if doc is None:
        return None
    execution, ops = journal_outcome(journal_dir, cycle_id)
    return trail.trails(doc, execution, ops, names=names)


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


# ---------------------------------------------------------------------------------- screen
SEALED_NOTE = "private until the reveal: the council's leanings before your decision; do not share"
NO_TRAIL = "no trail for this line in the public document"


def leg_row(leg: Any) -> str:
    """One ledger leg in percent of NAV and in x (weights are signed fractions of NAV)."""
    before, after = float(leg.weight_before or 0.0), float(leg.weight_after or 0.0)
    text = (f"   leg {leg.seq} {leg.kind} {leg.symbol} {leg.direction} x{leg.leverage}: "
            f"{before:+.1%} → {after:+.1%} of NAV ({before:+.3f}x → {after:+.3f}x)")
    if leg.stop_distance:
        text += f", stop {float(leg.stop_distance):.1%}"
    return text


def _has_trail(t: trail.LineTrail | None) -> bool:
    return t is not None and bool(t.steps or t.why_not)


def decision_why(
    decision: Any,
    plan: Any,
    *,
    state_dir: Path,
    journal_dir: Path | None = None,
    names: Mapping[str, str] | None = None,
    echo: Callable[[str], Any] = typer.echo,
) -> list[str]:
    """M5-K: the operator's "why" screen for `council show <decision>` and the approval screen. For
    every line with a leg, the line's trail (`publish.trail` over the sealed public document, or the
    revealed one once it is in the journal) followed by that line's ledger legs in percent and x.
    Public-model content only: no private input and no licensed text is read. Returns the lines
    with a leg but no trail (empty when every leg is explained); never raises."""
    from council import paths

    cycle_id = getattr(decision, "cycle_id", None)
    legs = list(getattr(plan, "legs", None) or [])
    if not legs:
        return []
    if not cycle_id:
        kind = getattr(decision, "kind", "decision")
        echo(f"why: no council trail; this {kind} comes from code, not from a council cycle")
        return []
    journal_root = Path(journal_dir) if journal_dir is not None else paths.JOURNAL_DIR
    names = names if names is not None else line_names()
    try:
        found = sealed_trails(state_dir, journal_root, cycle_id, names=names)
        source = "sealed public document, not yet revealed"
        if found is None:
            found = journal_trails(journal_root, cycle_id, names=names)
            source = "revealed public record"
    except Exception as exc:     # a display aid: a broken seal is reported, never a crash
        echo(f"why: trail unavailable for {cycle_id} ({exc if isinstance(exc, WhyError) else type(exc).__name__})")
        return sorted({str(leg.line or leg.symbol) for leg in legs})
    if found is None:
        echo(f"why: no sealed or revealed public document for {cycle_id}; trail unavailable")
        return sorted({str(leg.line or leg.symbol) for leg in legs})
    by_line = {t.line: t for t in found}
    leg_lines = {str(leg.line or leg.symbol) for leg in legs}
    order = [t.line for t in found if t.line in leg_lines]           # `council why`'s order
    order += sorted(leg_lines - set(order))
    echo("")
    echo(f"{cycle_id} · why each line with a leg · source: {source}")
    if source.startswith("sealed"):
        echo(SEALED_NOTE)
    missing: list[str] = []
    for key in order:
        t = by_line.get(key)
        echo("")
        if _has_trail(t):
            for text in trail.render_trail(t):
                echo(text)
        else:
            missing.append(key)
            echo(f"{t.headline if t is not None else key}")
            echo(f"   {NO_TRAIL}")
        for leg in legs:
            if str(leg.line or leg.symbol) == key:
                echo(leg_row(leg))
    return missing


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
    swing_text = None
    if line is not None:
        swing_text = swing_why(cycle_id, line, operator=operator and header.endswith(PRIVATE_NOTE),
                               state_dir=Path(state_dir) if state_dir is not None else paths.state_dir(),
                               journal_dir=journal_root)
    if line is not None and line not in {t.line for t in found}:
        if swing_text:
            echo(swing_text)
            return 0
        echo(f"no line {line} in cycle {cycle_id}")
        return 1
    echo(trail.render_text(found, header=header, line=line).rstrip("\n"))
    if swing_text:
        echo(swing_text)
    return 0


def swing_why(cycle_id: str, ticker: str, *, operator: bool, state_dir: Path, journal_dir: Path) -> str | None:
    """The swing chain for `ticker` in one cycle, or None: from the private ledger record (operator
    only) or from the revealed public document."""
    from council.swing import trail as swing_trail

    lines = None
    source = ""
    if operator:
        from council.ledger.db import LEDGER_FILE, Ledger

        path = Path(state_dir) / LEDGER_FILE
        if path.is_file():
            ledger = Ledger(path)
            raw = ledger.get_cycle(cycle_id)
            if raw is not None:
                decision = legs = None
                hold: list[str] = list(((raw.get("risk") or {}).get("hold_reasons")) or [])
                try:
                    decision = ledger.get_decision(raw["decision_id"]) if raw.get("decision_id") else None
                    legs = ledger.legs(raw["decision_id"]) if raw.get("decision_id") else []
                except Exception:  # noqa: BLE001 - an older ledger: the chain stops at the plan
                    decision, legs = None, []
                try:
                    trades, events = ledger.swing_trades(), ledger.swing_events()
                except Exception:  # noqa: BLE001 - a ledger without the swing tables
                    trades, events = [], []
                lines = swing_trail.ledger_lines(raw, ticker, hold_reasons=hold, legs=legs or [],
                                                 decision=decision, trades=trades, events=events)
                source = f"source: ledger\n{PRIVATE_NOTE}"
    if lines is None:
        doc = _public_doc(journal_dir, cycle_id)
        lines = swing_trail.public_lines(getattr(doc, "swing", None), ticker) if doc is not None else None
        source = "source: revealed public record"
    if not lines:
        return None
    return "\n".join([f"{cycle_id} · swing chain for {ticker.upper()} · {source}", *lines]).rstrip("\n")


def _public_doc(journal_dir: Path, cycle_id: str) -> Any:
    from council.publish import journal
    from council.publish.public_models import PublicCycleV1

    path = Path(journal_dir).parent / journal.cycle_path(cycle_id)
    if not path.is_file():
        return None
    try:
        return PublicCycleV1.model_validate_json(path.read_text())
    except ValueError:
        return None


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
    "NO_TRAIL", "PRIVATE_NOTE", "SEALED_NOTE", "WhyError", "cycle_of", "decision_why", "journal_outcome",
    "journal_trails", "leg_row", "ledger_state", "ledger_trails", "operator_context_ok", "register",
    "run_why", "sealed_document", "sealed_trails", "swing_why", "why_command",
]
