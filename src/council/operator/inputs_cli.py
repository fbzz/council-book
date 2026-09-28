"""`council inputs <cycle> [--role R]`: exactly what each agent saw (operator terminal only).

Commands (registered on the main CLI by `register(app)`):
  council inputs <cycle> [--role bear] [--replicate 0] [--attempt 0] [--section desk.full.lines]
                         [--system] [--replies] [--reading] [--html] [--rehearsal]
      Swing roles (swing-book.md §7.1): --role scout | skeptic | swing_bull | swing_bear | swing_pm.
      With --role scout (or no role on a swing slot) the Scout's reading list follows, one
      disposition per item: idea (a Scout idea's catalyst), cited (by a later swing role) or
      not_used; citations come from the ledger record's private swing record.
      Prints the exact private input of every call (the user message byte for byte, the system
      prompt with --system, the correction turn, the raw replies with --replies) and the news
      reading list (one disposition per item: used / ignored and why). --html writes a local page
      under `<state dir>/inputs-view/` (0700 / 0600) with every shared section rendered once, each
      value highlighted with its evidence id and sources, the reading list, and the difference
      between the code desk (news, macro, control) and the full desk (debate, PM).
  council inputs verify <cycle>     re-checks every hash and commit of the private capture and that
                                    every ledger call's input hash was captured.
  council inputs prune --before D   deletes private captures of cycles before date D.
  council purge-licensed ...        see council.operator.purge.

Why operator-only: the output can hold eToro Licensed Content (feed text, broker quotes). Pasting
it into a coding agent's context, a prompt or a public page would be onward transmission. Every
command calls `assert_current_process_is_operator` first; agents use the public record instead.
Broker items are marked "Source: eToro" wherever they are displayed, as the terms require.
"""

from __future__ import annotations

import difflib
import html
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from council.deliberation.capture import (
    calls_path,
    check_cycle_id,
    licensed_path,
    load_inputs,
    load_licensed,
    section_parts,
    section_text,
    user_text,
    verify,
    write_private,
)
from council.deliberation.reading import (
    SWING_ROLES,
    Reading,
    citations_from_record,
    counts,
    public_links,
    reading_list,
    reads_from_inputs,
    scout_counts,
    scout_reading_list,
    swing_citations,
    swing_reads_from_inputs,
    with_links,
)
from council.models.inputs import CallInput, CycleInputs, LicensedInputs

BANNER = "PRIVATE: may contain licensed broker text; do not paste publicly"
ETORO_MARK = "Source: eToro"
VIEW_DIR = "inputs-view"
BROKER_LABELS = frozenset({"broker_feed", "etoro_feed", "broker", "etoro", "broker_quote"})


def require_operator() -> None:
    """Refuse (exit 2) unless this is the human operator's interactive terminal."""
    from council.operator import guards

    try:
        guards.assert_current_process_is_operator()
    except guards.GuardError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc


# ------------------------------------------------------------------------------------ loading
def load(state_dir: Path, cycle_id: str) -> tuple[CycleInputs, LicensedInputs | None]:
    check_cycle_id(cycle_id)
    return load_inputs(state_dir, cycle_id), load_licensed(state_dir, cycle_id)


def ledger_record(state_dir: Path, cycle_id: str) -> dict[str, Any] | None:
    """The cycle's ledger record (citations, role calls), or None when there is no ledger."""
    from council.ledger.db import LEDGER_FILE, Ledger

    path = Path(state_dir) / LEDGER_FILE
    if not path.exists():
        return None
    try:
        return Ledger(path).get_cycle(cycle_id)
    except Exception:
        return None


def readings_for(inputs: CycleInputs, licensed: LicensedInputs | None,
                 record: Mapping[str, Any] | None) -> list[Reading]:
    items, read_by = reads_from_inputs(inputs, licensed)
    readings = reading_list(items, read_by, citations_from_record(record or {}))
    return with_links(readings, public_links(record))


def scout_readings_for(inputs: CycleInputs, licensed: LicensedInputs | None,
                       record: Mapping[str, Any] | None) -> list[Reading]:
    """The swing Scout's reading list (empty when the cycle had no swing slot)."""
    items, read_by = swing_reads_from_inputs(inputs, licensed)
    swing = ((record or {}).get("extras") or {}).get("swing") if isinstance(record, Mapping) else None
    readings = scout_reading_list(items, read_by, swing_citations(swing if isinstance(swing, Mapping) else None))
    return with_links(readings, public_links(record))


def select_calls(inputs: CycleInputs, *, role: str | None = None, replicate: int | None = None,
                 attempt: int | None = None) -> list[CallInput]:
    return [c for c in inputs.calls
            if (role is None or c.role == role) and (replicate is None or c.replicate == replicate)
            and (attempt is None or c.attempt == attempt)]


def _broker_refs(inputs: CycleInputs, call: CallInput) -> list[str]:
    refs: list[str] = []
    for key in call.sections:
        sec = inputs.sections.get(key)
        if sec is None:
            continue
        for i in sec.licensed:
            ref = sec.items[i].ref
            if ref not in refs:
                refs.append(ref)
    return refs


def _licence_note(inputs: CycleInputs) -> str:
    if not inputs.licensed_items:
        return "eToro licensed items: none"
    if inputs.licensed_purged_at is not None:
        return (f"eToro licensed items: {inputs.licensed_items}, purged "
                f"{inputs.licensed_purged_at:%Y-%m-%d %H:%M} UTC (their text can no longer be shown)")
    return (f"eToro licensed items: {inputs.licensed_items} (held under licensed/, purged after "
            "7 days)")


# --------------------------------------------------------------------------------- text view
def render_text(
    inputs: CycleInputs,
    licensed: LicensedInputs | None,
    *,
    role: str | None = None,
    replicate: int | None = None,
    attempt: int | None = None,
    section: str | None = None,
    system: bool = False,
    replies: bool = False,
    readings: Sequence[Reading] | None = None,
    scout_readings: Sequence[Reading] | None = None,
) -> str:
    calls = select_calls(inputs, role=role, replicate=replicate, attempt=attempt)
    out = [BANNER, f"cycle {inputs.cycle_id} · captured {inputs.captured_at:%Y-%m-%d %H:%M} UTC · "
                   f"{len(inputs.calls)} calls · {len(inputs.sections)} sections",
           _licence_note(inputs)]
    if inputs.flags:
        out.append("capture flags: " + ", ".join(inputs.flags))
    if not calls:
        out.append("no call matches the filter")
    for call in calls:
        out += ["", f"=== {call.call_key} · {call.prompt_id} · prompt sha {call.prompt_sha[:12]} · "
                    f"seed {call.seed} · num_predict {call.num_predict} · status {call.status}"]
        if call.error:
            out.append(f"error: {call.error}")
        if call.retries:
            out.append("retried (the same messages resent): " + "; ".join(call.retries))
        out.append("sections: " + " · ".join(call.sections))
        if system:
            out += ["--- system prompt ---", call.system]
        if section is not None:
            if section not in call.sections:
                out.append(f"(this call did not read section {section})")
            else:
                text, complete = section_text(inputs.sections[section], licensed)
                out += [f"--- section {section} (exact) ---", text]
                if not complete:
                    out.append("(licensed text purged: shown with placeholders)")
        else:
            text, complete = user_text(inputs, call, licensed)
            out += ["--- user message (exact) ---", text]
            if not complete:
                out.append("(licensed text purged: this input can no longer be rebuilt byte for byte)")
        broker = _broker_refs(inputs, call)
        if broker:
            out.append(f"{ETORO_MARK}: eToro Licensed Content in this input: {', '.join(broker)}")
        if call.correction:
            out += ["--- correction turn ---", "the first reply, as sent back:", call.sent_assistant,
                    "the correction message:", call.correction]
            if call.errors:
                out.append("checker errors: " + "; ".join(call.errors))
        if replies:
            out.append(f"--- replies ({len(call.replies)}) ---")
            for i, reply in enumerate(call.replies, start=1):
                out += [f"[reply {i}]", reply]
    if readings is not None:
        out += ["", *reading_text(readings)]
    if scout_readings:
        out += ["", *scout_reading_text(scout_readings)]
    return "\n".join(out) + "\n"


def scout_reading_text(readings: Sequence[Reading]) -> list[str]:
    c = scout_counts(readings)
    out = [f"--- swing Scout reading list: {c['read']} read · {c['idea']} became ideas · "
           f"{c['cited']} cited later · {c['not_used']} not used ---"]
    for r in readings:
        mark = f" ({ETORO_MARK})" if r.source in BROKER_LABELS else ""
        out.append(f"{r.id} {r.source}{mark}: {r.title}")
        out.append(f"    {'USED' if r.used else 'IGNORED'} ({r.disposition}): {r.why}")
        if r.link:
            out.append(f"    link: {r.link}")
    return out


def reading_text(readings: Sequence[Reading]) -> list[str]:
    c = counts(readings)
    out = [f"--- news reading list: {c['read']} read · {c['card']} made into cards · "
           f"{c['cited']} cited · {c['not_cited']} not used ---"]
    for r in readings:
        mark = f" ({ETORO_MARK})" if r.source in BROKER_LABELS else ""
        used = "USED" if r.used else "IGNORED"
        out.append(f"{r.id} [{r.symbols or '-'}] {r.age} {r.source}{mark}: {r.title}")
        out.append(f"    {used} ({r.disposition}): {r.why}")
        if r.link:
            out.append(f"    link: {r.link}")
    return out


# --------------------------------------------------------------------------------- html view
_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e0d8;--warn:#b42318;--fact:#e8f1fb;
--cell:#eef7ee;--news:#fff4e0;--card:#f3ecfb;--claim:#fdeef1;--lic:#ffe1d6;--code:#f4f4f5}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--muted:#a0a0a8;--line:#2e2e33;
--warn:#ff8a7a;--fact:#1d2a3a;--cell:#1d2e22;--news:#3a2e16;--card:#2c2240;--claim:#3a1f27;
--lic:#4a2418;--code:#232327}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;
margin:0 auto;max-width:1100px;padding:16px}
.banner{border:2px solid var(--warn);color:var(--warn);padding:8px 12px;font-weight:600}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--code);padding:10px;
border-radius:6px;font:12.5px/1.45 ui-monospace,Menlo,monospace}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);
padding:4px 6px;text-align:left;vertical-align:top}.muted{color:var(--muted)}
.it{border-radius:3px}.k-fact{background:var(--fact)}.k-cell,.k-band{background:var(--cell)}
.k-news,.k-event{background:var(--news)}.k-card{background:var(--card)}.k-claim{background:var(--claim)}
.l-broker_licensed{background:var(--lic);outline:1px dashed var(--warn)}
.mark{font-size:10px;color:var(--warn);font-weight:600;margin-left:2px}
.purged{color:var(--warn);font-style:italic}.chip{padding:1px 6px;border-radius:9px;font-size:12px}
.card{background:var(--card)}.cited{background:var(--cell)}.not_cited{background:var(--code)}
.add{color:#1a7f37}.del{color:var(--warn)}details{margin:6px 0}summary{cursor:pointer}
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _anchor(key: str) -> str:
    return "sec-" + re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def _section_html(key: str, inputs: CycleInputs, licensed: LicensedInputs | None) -> str:
    sec = inputs.sections[key]
    parts = []
    for text, idx, available in section_parts(sec, licensed):
        if idx is None:
            parts.append(_e(text))
            continue
        item = sec.items[idx]
        if not available:
            parts.append(f'<span class="purged" title="{_e(item.ref)}">{_e(text)}</span>')
            continue
        title = " · ".join(x for x in (item.ref, item.field or "", "sources: " + ", ".join(item.sources),
                                       f"licence: {item.licence}") if x)
        span = (f'<span class="it k-{_e(item.kind)} l-{_e(item.licence)}" title="{_e(title)}">'
                f"{_e(text)}</span>")
        if item.licence == "broker_licensed":
            span += f'<sup class="mark">{ETORO_MARK}</sup>'
        parts.append(span)
    purged = f' · licensed text purged {sec.purged_at:%Y-%m-%d}' if sec.purged_at else ""
    return (f'<section id="{_anchor(key)}"><h3>{_e(key)} <span class="muted">({_e(sec.kind)}, '
            f"{len(sec.items)} values{purged})</span></h3><pre>{''.join(parts)}</pre></section>")


def _desk_text(inputs: CycleInputs, licensed: LicensedInputs | None, variant: str) -> str:
    keys = []
    for call in inputs.calls:
        for key in call.sections:
            if key.startswith(f"desk.{variant}.") and key not in keys:
                keys.append(key)
    return "".join(section_text(inputs.sections[k], licensed)[0] for k in keys)


def desk_diff(inputs: CycleInputs, licensed: LicensedInputs | None) -> list[str]:
    """Unified diff of the code desk (news, macro, control) against the full desk (debate, PM)."""
    code, full = _desk_text(inputs, licensed, "code"), _desk_text(inputs, licensed, "full")
    if not code or not full:
        return []
    return list(difflib.unified_diff(code.splitlines(), full.splitlines(), "desk.code", "desk.full",
                                     lineterm="", n=0))


def render_html(
    inputs: CycleInputs,
    licensed: LicensedInputs | None,
    *,
    readings: Sequence[Reading] = (),
    role: str | None = None,
) -> str:
    calls = select_calls(inputs, role=role)
    keys: list[str] = []
    for call in calls:
        keys += [k for k in call.sections if k not in keys and k in inputs.sections]
    rows = []
    for call in calls:
        links = " ".join(f'<a href="#{_anchor(k)}">{_e(k)}</a>' for k in call.sections)
        rows.append(f'<tr><td><a href="#call-{_e(call.call_key)}">{_e(call.call_key)}</a></td>'
                    f"<td>{_e(call.prompt_id)}</td><td>{_e(call.status)}</td>"
                    f"<td>{'yes' if call.correction else ''}</td><td>{links}</td></tr>")
    body = [
        f'<p class="banner">{_e(BANNER)}</p>',
        f"<h1>What each agent saw: {_e(inputs.cycle_id)}</h1>",
        f'<p class="muted">captured {inputs.captured_at:%Y-%m-%d %H:%M} UTC · {len(inputs.calls)} '
        f"calls · {len(inputs.sections)} sections · {_e(_licence_note(inputs))}</p>",
        "<h2>Calls</h2><table><tr><th>call</th><th>prompt</th><th>status</th><th>corrected</th>"
        f"<th>sections read, in order</th></tr>{''.join(rows)}</table>",
    ]
    if readings:
        c = counts(readings)
        trs = []
        for r in readings:
            mark = f' <span class="mark">{ETORO_MARK}</span>' if r.source in BROKER_LABELS else ""
            title = _e(r.title) if r.available else '<span class="purged">[licensed text purged]</span>'
            if r.available and r.link:
                title = f'<a href="{_e(r.link)}" rel="noopener noreferrer">{title}</a>'
            trs.append(
                f"<tr><td>{_e(r.id)}</td><td>{_e(r.source)}{mark}</td><td>{_e(r.age)}</td>"
                f"<td>{_e(r.symbols)}</td><td>{title}</td>"
                f'<td><span class="chip {_e(r.disposition)}">{"used" if r.used else "ignored"}: '
                f"{_e(r.disposition)}</span></td><td>{_e(', '.join(r.read_by))}</td>"
                f"<td>{_e(r.why)}</td></tr>")
        body.append(
            f"<h2>News reading list</h2><p>{c['read']} read · {c['card']} made into cards · "
            f"{c['cited']} cited · {c['not_cited']} not used</p><table><tr><th>id</th><th>source</th>"
            "<th>age</th><th>lines</th><th>headline</th><th>disposition</th><th>read by</th>"
            f"<th>why</th></tr>{''.join(trs)}</table>")
    diff = desk_diff(inputs, licensed)
    if diff:
        lines = "\n".join(
            f'<span class="{"add" if d.startswith("+") else "del" if d.startswith("-") else "muted"}">'
            f"{_e(d)}</span>" for d in diff)
        body.append("<h2>Code desk versus full desk</h2><p class='muted'>The news analyst, the macro "
                    "analyst and the single-agent control read the code desk; the debate and the "
                    f"manager read the full desk.</p><pre>{lines}</pre>")
    body.append("<h2>Sections (each rendered once)</h2>")
    body += [_section_html(k, inputs, licensed) for k in keys]
    body.append("<h2>Each call</h2>")
    for call in calls:
        text, complete = user_text(inputs, call, licensed)
        note = "" if complete else " (licensed text purged)"
        parts = [f'<section id="call-{_e(call.call_key)}"><h3>{_e(call.call_key)} · '
                 f"{_e(call.prompt_id)} · status {_e(call.status)}</h3>",
                 f'<p class="muted">prompt sha {_e(call.prompt_sha[:16])} · seed {call.seed} · '
                 f"input hash {_e(call.input_hash[:16])}…</p>",
                 (f'<p class="muted">retried (the same messages resent): {_e("; ".join(call.retries))}</p>'
                  if call.retries else ""),
                 f"<details><summary>system prompt</summary><pre>{_e(call.system)}</pre></details>",
                 f"<details><summary>exact user message ({len(text)} characters){note}</summary>"
                 f"<pre>{_e(text)}</pre></details>"]
        broker = _broker_refs(inputs, call)
        if broker:
            parts.append(f'<p><span class="mark">{ETORO_MARK}</span> eToro Licensed Content in this '
                         f"input: {_e(', '.join(broker))}</p>")
        if call.correction:
            parts.append("<details><summary>correction turn</summary><p>the first reply, as sent "
                         f"back:</p><pre>{_e(call.sent_assistant)}</pre><p>the correction message:</p>"
                         f"<pre>{_e(call.correction)}</pre></details>")
        for i, reply in enumerate(call.replies, start=1):
            parts.append(f"<details><summary>reply {i}</summary><pre>{_e(reply)}</pre></details>")
        parts.append("</section>")
        body.append("".join(parts))
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
            "style-src 'unsafe-inline'\">"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Agent inputs {_e(inputs.cycle_id)}</title><style>{_CSS}</style></head>"
            f"<body>{''.join(body)}</body></html>\n")


def write_view(state_dir: Path, cycle_id: str, page: str, *, role: str | None = None) -> Path:
    """Write the page under `<state dir>/inputs-view/` (0700 directory, 0600 file)."""
    check_cycle_id(cycle_id)
    suffix = f".{re.sub(r'[^a-z0-9_]', '_', role)}" if role else ""
    root = Path(state_dir)
    return write_private(root, root / VIEW_DIR / f"{cycle_id}{suffix}.html", page.encode())


# ---------------------------------------------------------------------------------- pruning
def prune(state_dir: Path, before: date) -> int:
    """Delete the private captures (and any licensed texts) of cycles dated before `before`."""
    removed = 0
    root = Path(state_dir)
    for base in (root / "calls", root / "licensed" / "calls"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json.gz")):
            cycle_id = path.name.removesuffix(".json.gz")
            try:
                check_cycle_id(cycle_id)
                day = date.fromisoformat(cycle_id[:10])
            except ValueError:
                continue
            if day < before:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


# ---------------------------------------------------------------------------------- commands
class _ShowByDefault(TyperGroup):
    """`council inputs <cycle> ...` means `council inputs show <cycle> ...`."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["show", *args]
        return super().parse_args(ctx, args)


inputs_app = typer.Typer(cls=_ShowByDefault, add_completion=False, no_args_is_help=True,
                         help="Exactly what each agent saw (operator terminal only; private).")


def _root(rehearsal: bool) -> Path:
    from council import paths

    return paths.state_dir() / "rehearsal" if rehearsal else paths.state_dir()


def _load_or_exit(root: Path, cycle_id: str) -> tuple[CycleInputs, LicensedInputs | None]:
    try:
        return load(root, cycle_id)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except FileNotFoundError as exc:
        typer.echo(f"no private capture for cycle {cycle_id}", err=True)
        raise typer.Exit(1) from exc


@inputs_app.command("show")
def show_command(
    cycle_id: Annotated[str, typer.Argument(help="Cycle id, e.g. 2026-10-01T1440Z.")],
    role: Annotated[str | None, typer.Option(help="Only this role's calls (news, bear, pm, scout, skeptic, "
                                                   "swing_bull, swing_bear, swing_pm, ...).")] = None,
    replicate: Annotated[int | None, typer.Option(help="Only this replicate.")] = None,
    attempt: Annotated[int | None, typer.Option(help="Only this stage attempt.")] = None,
    section: Annotated[str | None, typer.Option(help="Print one section, e.g. desk.full.lines.")] = None,
    system: Annotated[bool, typer.Option("--system", help="Include the system prompt.")] = False,
    replies: Annotated[bool, typer.Option("--replies", help="Include every raw reply.")] = False,
    reading: Annotated[bool, typer.Option("--reading", help="Include the news reading list.")] = False,
    as_html: Annotated[bool, typer.Option("--html", help="Write a local page instead.")] = False,
    rehearsal: Annotated[bool, typer.Option("--rehearsal", help="Read the rehearsal state.")] = False,
) -> None:
    """Print (or render) the exact input of every model call of a cycle."""
    require_operator()
    root = _root(rehearsal)
    inputs, licensed = _load_or_exit(root, cycle_id)
    record = ledger_record(root, cycle_id)
    readings = readings_for(inputs, licensed, record)
    if as_html:
        path = write_view(root, cycle_id, render_html(inputs, licensed, readings=readings, role=role),
                          role=role)
        typer.echo(f"{BANNER}\nwrote {path}")
        return
    scout = scout_readings_for(inputs, licensed, record) if role in (None, "scout") or reading else []
    typer.echo(render_text(inputs, licensed, role=role, replicate=replicate, attempt=attempt,
                           section=section, system=system, replies=replies,
                           readings=readings if (reading or role in (None, "news")) and role not in SWING_ROLES else None,
                           scout_readings=scout), nl=False)


@inputs_app.command("verify")
def verify_command(
    cycle_id: Annotated[str, typer.Argument(help="Cycle id.")],
    rehearsal: Annotated[bool, typer.Option("--rehearsal", help="Read the rehearsal state.")] = False,
) -> None:
    """Re-check every hash and commit of a cycle's private capture."""
    require_operator()
    root = _root(rehearsal)
    inputs, licensed = _load_or_exit(root, cycle_id)
    record = ledger_record(root, cycle_id) or {}
    problems, notes = verify(inputs, licensed, record.get("calls") or [])
    for note in notes:
        typer.echo(f"note: {note}")
    for problem in problems:
        typer.echo(f"FAIL: {problem}")
    typer.echo(f"{len(inputs.calls)} calls, {len(inputs.sections)} sections: "
               + ("verified" if not problems else f"{len(problems)} problem(s)"))
    if problems:
        raise typer.Exit(1)


@inputs_app.command("prune")
def prune_command(
    before: Annotated[str, typer.Option("--before", help="Delete captures of cycles before YYYY-MM-DD.")],
    rehearsal: Annotated[bool, typer.Option("--rehearsal", help="Prune the rehearsal state.")] = False,
) -> None:
    """Delete private captures older than a date (licensed texts go with them)."""
    require_operator()
    try:
        day = date.fromisoformat(before)
    except ValueError as exc:
        raise typer.BadParameter("use YYYY-MM-DD") from exc
    typer.echo(f"removed {prune(_root(rehearsal), day)} capture file(s)")


def register(app: typer.Typer) -> None:
    """Add `inputs` and `purge-licensed` to the main CLI (the stage-2 hook in cli.py)."""
    from council.operator.purge import purge_licensed_command

    app.add_typer(inputs_app, name="inputs")
    app.command("purge-licensed")(purge_licensed_command)


__all__ = [
    "BANNER", "ETORO_MARK", "calls_path", "desk_diff", "inputs_app", "licensed_path", "load",
    "prune", "readings_for", "register", "render_html", "render_text", "require_operator",
    "write_view",
]
