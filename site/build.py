"""Static site for the public record, built from journal/, prompts/ and policy/ only.

Usage: uv run python site/build.py [--journal journal] [--prompts prompts] [--policy policy] [--out _site]

Rules:
- Jinja2 autoescape is ON and undefined variables fail the build; model text is never rendered as
  HTML or markdown. The only script is a constant inline "stale" badge, allowed by its CSP hash.
- A strict Content-Security-Policy meta tag on every page; no external fonts, scripts or trackers.
- The site must build with zero cycles (status AWAITING ACCOUNT) and every output file must pass
  the leak scan, otherwise the build fails and nothing is deployed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from council import invariants
from council.publish import commit_reveal, leakscan
from council.publish.journal import parse_incident
from council.publish.public_models import (
    PublicBook,
    PublicCommitment,
    PublicCycleV1,
    PublicIncident,
    PublicOpsRow,
    PublicPerformancePoint,
    PublicReveal,
    PublicStatus,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
DATA = HERE / "data"

# The only script on the site: show the STALE badge when the last cycle is more than 5 h old.
STALE_SCRIPT = (
    "(function(){var b=document.body,t=Date.parse(b.getAttribute('data-last-cycle')||'');"
    "if(!isNaN(t)&&Date.now()-t>5*36e5){var s=document.getElementById('stale');if(s){s.hidden=false;}}})();"
)


def script_hash(script: str = STALE_SCRIPT) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()


CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    f"script-src '{script_hash()}'; base-uri 'none'; form-action 'none'"
)

NAV = (
    {"key": "index", "href": "index.html", "label": "What this is"},
    {"key": "council", "href": "council.html", "label": "Council & rules"},
    {"key": "cycles", "href": "cycles.html", "label": "Cycles"},
    {"key": "book", "href": "book.html", "label": "Book & performance"},
    {"key": "failures", "href": "failures.html", "label": "Failures & disclaimers"},
)

STATUS_CHIP = {
    "AWAITING_ACCOUNT": ("AWAITING ACCOUNT", "awaiting"),
    "LIVE": ("LIVE", "executed"),
    "WARN": ("WARN", "warn"),
    "HALTED": ("HALTED", "halted"),
    "FLAT": ("FLAT", "stone"),
}
DECISION_CHIP = {
    None: ("NO DECISION", "awaiting"),
    "awaiting_publication": ("SEALED", "sealed"),
    "proposed": ("PROPOSED", "proposed"),
    "approved": ("APPROVED", "proposed"),
    "executing": ("EXECUTING", "proposed"),
    "completed": ("EXECUTED", "executed"),
    "completed_partial": ("PARTLY EXECUTED", "executed"),
    "rejected": ("REJECTED", "stone"),
    "expired": ("EXPIRED", "stone"),
    "superseded": ("SUPERSEDED", "stone"),
    "blocked": ("BLOCKED", "warn"),
    "execution_unknown": ("EXECUTION UNKNOWN", "warn"),
    "reviewed_no_action": ("NO ACTION", "sealed"),
}
KILL_CHIP = {
    "NORMAL": ("NORMAL", "sealed"), "WARN": ("WARN", "warn"), "HALTED": ("HALTED", "halted"),
    "FLAT": ("FLAT", "stone"), "RESUMED": ("RESUMED", "proposed"),
}

CODE_ROLES = (
    ("Data steward", "Builds the percentage-only fact pack from completed bars; freezes stale or closed markets.", "code"),
    ("Reference book", "Mechanical trend x volatility book: the default position, the band centre and the fallback.", "code"),
    ("Event officer", "Blocks adds around scheduled macro events; never forces a sale.", "code"),
    ("Vol officer", "Writes volatility-shock cards and trips the volatility breaker.", "code"),
    ("Cost desk", "Prices every leg (spread, fees, overnight carry) and runs the net-of-cost gate.", "code"),
    ("Consistency auditor", "Reverts uncited or self-contradicting changes; discards broken PM replicates.", "code"),
    ("Risk officer", "Final authority: enforces every rule in policy/risk.yaml and builds the order legs.", "risk"),
    ("Scribe", "Builds this public record from allow-listed fields only.", "code"),
    ("Scorekeeper", "Computes controls and card scores. Descriptive only.", "code"),
)
LLM_ROLES = {
    "news": ("News analyst", "Writes evidence cards from broker news items. Their text is never republished.", "ADVISES", "analyst"),
    "macro": ("Macro analyst", "Describes the macro regime and its drivers. Context only.", "CONTEXT", "teal"),
    "filings": ("Filings analyst", "Reads company filings (arrives with single stocks).", "ADVISES", "analyst"),
    "sector": ("Sector analyst", "Ranks names inside a peer group (arrives with single stocks).", "CONTEXT", "teal"),
    "bull": ("Bull advocate", "Opens the debate, then answers the bear's rebuttal.", "ADVISES", "bull"),
    "bear": ("Bear advocate", "Rebuts the bull's specific claims, citing evidence.", "ADVISES", "bear"),
    "pm": ("Portfolio manager", "Proposes at most three deviations from the reference, inside bands that code enforces. Three independent replicates; the medoid replicate is used.", "DECIDES", "pm"),
    "single_agent_control": ("Single-agent control", "One agent, same facts, no debate. Published as a control; it never trades.", "CONTEXT", "stone"),
}
PROMPT_ROLE_ALIASES = {"bull_open": "bull", "bull_rebuttal": "bull", "single_agent": "single_agent_control"}

RULE_TITLES = {
    "gross": "Gross exposure caps (sum of absolute weights, x NAV)",
    "net": "Net exposure range and short gross cap",
    "killswitch": "Soft kill: warn, then halt, as a fraction of the lifetime peak",
    "catastrophe_stop": "Every open carries a catastrophe stop-loss",
    "reentry_cooloff_days": "Cool-off before re-entering after a stop hit",
    "caps": "Per-line and cluster caps (absolute weight, x NAV)",
    "leverage_caps": "Leverage caps by asset class",
    "margin_use_max": "Margin use cap (keeps a cash reserve)",
    "ex_ante_vol_hard": "Ex-ante book volatility hard cap",
    "vol_breaker": "Volatility breaker (short vs long EWMA ratio)",
    "authority": "Council authority bands around the reference",
    "deadband": "Deadband: changes too small to be worth trading are skipped",
    "min_hold_days": "Minimum holding periods",
    "churn": "Turnover limits",
    "cost_budget": "Cost and carry budgets",
    "net_of_cost_gate": "Net-of-cost gate (break-even Sharpe)",
    "event_block": "No adds around scheduled macro events",
    "anti_chase_sigma": "Anti-chase: no adds right after a large one-day move",
    "freshness": "Data freshness limits",
    "material_change_required": "Executable changes need new material facts",
    "proposal": "Legs per proposal",
    "approval": "Re-checks at approval time and the notification window",
    "reconcile": "Post-trade reconciliation tolerances",
    "priority": "Proposal priority (a lower one never supersedes a higher one)",
}

CONTROL_SERIES = (
    ("c0", "C0 as executed", "c0"),
    ("c2", "C2 reference", "c2"),
    ("c2x", "C2x reference, exposure-matched", "c2x"),
    ("c3", "C3 hold", "c3"),
    ("c4_spy", "C4 buy-and-hold SPY", "c4a"),
    ("c4_btc", "C4 buy-and-hold BTC", "c4b"),
)


class SiteBuildError(RuntimeError):
    pass


# ------------------------------------------------------------------------------ formatting
def short_sha(value: str | None, n: int = 12) -> str:
    """A short digest that always contains a letter (an all-digit prefix would look like an id)."""
    v = value or ""
    if not v:
        return "—"
    k = min(n, len(v))
    while k < len(v) and not re.search(r"[a-f]", v[:k]):
        k += 1
    return v[:k]


def fmt_x(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}x" if abs(v) >= 0.1 or v == 0 else f"{v:.3f}x"


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}%"


def fmt_bp(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f} bp"


def fmt_level(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def fmt_slot(ts: datetime | None) -> str:
    return "—" if ts is None else ts.strftime("%Y-%m-%d %H:%MZ")


def fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def chip(mapping: dict, key: Any) -> dict[str, str]:
    label, css = mapping.get(key, (str(key).upper(), "awaiting"))
    return {"label": label, "css": css}


# ------------------------------------------------------------------------------ loading
@dataclass
class CycleView:
    doc: PublicCycleV1
    path: str                       # relative path of the cycle JSON inside the site copy
    commitment: PublicCommitment | None = None
    reveal: PublicReveal | None = None
    verified: bool = False

    @property
    def chip(self) -> dict[str, str]:
        return chip(DECISION_CHIP, self.doc.decision.state)


@dataclass
class JournalView:
    status: PublicStatus
    cycles: list[CycleView] = field(default_factory=list)
    book: PublicBook | None = None
    performance: list[PublicPerformancePoint] = field(default_factory=list)
    incidents: list[PublicIncident] = field(default_factory=list)
    ops: list[PublicOpsRow] = field(default_factory=list)
    copies: dict[str, Path] = field(default_factory=dict)   # site path -> journal source file


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_journal(journal_dir: Path) -> JournalView:
    status_file = journal_dir / "status.json"
    status = PublicStatus.model_validate_json(status_file.read_text()) if status_file.exists() else PublicStatus()
    view = JournalView(status=status)
    cycles_dir = journal_dir / "cycles"
    for file in sorted(cycles_dir.rglob("*.json")) if cycles_dir.exists() else []:
        if file.name.endswith(".reveal.json"):
            continue
        raw = json.loads(file.read_text())
        doc = PublicCycleV1.model_validate(raw)
        rel = file.relative_to(journal_dir).as_posix()
        cv = CycleView(doc=doc, path=f"journal/{rel}")
        view.copies[cv.path] = file
        reveal_file = file.with_name(file.name[: -len(".json")] + ".reveal.json")
        month = f"{doc.cycle_id[0:4]}/{doc.cycle_id[5:7]}"
        commitment_file = journal_dir / "commitments" / month / f"{doc.cycle_id}.json"
        if reveal_file.exists():
            cv.reveal = PublicReveal.model_validate_json(reveal_file.read_text())
            view.copies[f"journal/{reveal_file.relative_to(journal_dir).as_posix()}"] = reveal_file
        if commitment_file.exists():
            cv.commitment = PublicCommitment.model_validate_json(commitment_file.read_text())
            view.copies[f"journal/{commitment_file.relative_to(journal_dir).as_posix()}"] = commitment_file
        if cv.reveal and cv.commitment and cv.reveal.commitment_sha256 == cv.commitment.commitment_sha256:
            cv.verified = commit_reveal.verify(raw, cv.reveal.salt, cv.commitment.commitment_sha256)
        view.cycles.append(cv)
    view.cycles.sort(key=lambda c: c.doc.slot, reverse=True)
    book_file = journal_dir / "book" / "latest.json"
    if book_file.exists():
        view.book = PublicBook.model_validate_json(book_file.read_text())
    view.performance = sorted(
        (PublicPerformancePoint.model_validate(r) for r in _jsonl(journal_dir / "performance" / "index.jsonl")),
        key=lambda p: p.as_of,
    )
    view.ops = [PublicOpsRow.model_validate(r) for r in _jsonl(journal_dir / "ops" / "cycles.jsonl")]
    incidents_dir = journal_dir / "incidents"
    if incidents_dir.exists():
        view.incidents = sorted(
            (parse_incident(p.read_text()) for p in incidents_dir.glob("INC-*.md")),
            key=lambda i: i.incident_id, reverse=True,
        )
    return view


def load_manifest(prompts_dir: Path) -> dict[str, list[dict[str, str]]]:
    """prompts/manifest.json -> {role: [{prompt_id, sha}]}. Tolerant of the manifest's shape:
    {"prompts": {...}} or a flat mapping, entries as dicts or bare digests, or a list."""
    path = prompts_dir / "manifest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    entries = data.get("prompts", data) if isinstance(data, dict) else data
    items: list[tuple[str, Any]]
    if isinstance(entries, dict):
        items = list(entries.items())
    elif isinstance(entries, list):
        items = [(str(e.get("prompt_id") or e.get("id") or e.get("name") or e.get("file") or ""), e)
                 for e in entries if isinstance(e, dict)]
    else:
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for key, value in items:
        meta = value if isinstance(value, dict) else {"sha256": value}
        name = str(meta.get("prompt_id") or meta.get("id") or key)
        stem = re.split(r"[@:]", Path(str(meta.get("file") or name)).stem)[0]
        role = str(meta.get("role") or PROMPT_ROLE_ALIASES.get(stem, stem))
        sha = str(meta.get("sha256") or meta.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{8,64}", sha):
            sha = ""
        out.setdefault(role, []).append({"prompt_id": name[:64], "sha": sha})
    return out


def load_roster(prompts_dir: Path, policy_dir: Path) -> dict[str, Any]:
    council = yaml.safe_load((policy_dir / "council.yaml").read_text()) or {}
    manifest = load_manifest(prompts_dir)
    llm = []
    for role, cfg in (council.get("roles") or {}).items():
        name, what, authority, accent = LLM_ROLES.get(role, (role.replace("_", " ").title(), "", "ADVISES", "analyst"))
        llm.append({
            "role": role, "name": name, "what": what, "authority": authority, "accent": accent,
            "enabled": bool(cfg.get("enabled", True)), "replicates": int(cfg.get("replicates", 1)),
            "cadence": str(cfg.get("cadence", "every_cycle")).replace("_", " "),
            "prompts": manifest.get(role, []),
        })
    code = [{"name": n, "what": w, "accent": a} for n, w, a in CODE_ROLES]
    return {
        "code": code, "llm": llm, "model": str(council.get("model", "")),
        "think": bool(council.get("think", False)), "temperature": council.get("temperature", 0),
        "seeds": council.get("seeds", {}), "max_calls": council.get("max_calls_per_cycle"),
        "slots": council.get("slots_utc_hours", []), "slot_minute": council.get("slot_minute", 0),
    }


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for k, v in value.items():
            out += _flatten(v, f"{prefix}{k}." if isinstance(v, dict) else f"{prefix}{k}")
        return out
    if isinstance(value, list):
        return [(prefix.rstrip("."), ", ".join(fmt_value(v) for v in value))]
    return [(prefix.rstrip("."), fmt_value(value))]


def load_rules(policy_dir: Path) -> list[dict[str, Any]]:
    text = (policy_dir / "risk.yaml").read_text()
    data = yaml.safe_load(text) or {}
    ids: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z_]+):.*?#\s*(R\d+[a-z]?)\b", line)
        if m:
            ids[m.group(1)] = m.group(2)
    rows = []
    for key, value in data.items():
        if key == "version":
            continue
        rows.append({
            "rule_id": ids.get(key, ""), "key": key,
            "title": RULE_TITLES.get(key, key.replace("_", " ").capitalize()),
            "numbers": _flatten(value, "" if isinstance(value, dict) else key),
        })
    return rows


def load_withdrawn(path: Path = DATA / "withdrawn.yaml") -> list[dict[str, str]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [{k: str(v) for k, v in item.items()} for item in data.get("claims", [])]


def load_disclaimer(repo_root: Path) -> list[dict[str, str]]:
    path = repo_root / "DISCLAIMER.md"
    if not path.exists():
        path = REPO / "DISCLAIMER.md"
    if not path.exists():
        return [{"title": "Not investment advice.", "text": "This is a personal experiment."}]
    items: list[dict[str, str]] = []
    for line in path.read_text().splitlines():
        m = re.match(r"^- \*\*(.+?)\*\*\s*(.*)$", line)
        if m:
            items.append({"title": m.group(1), "text": m.group(2).strip()})
        elif items and line.startswith("  ") and line.strip():
            items[-1]["text"] = (items[-1]["text"] + " " + line.strip()).strip()
    return items


# ------------------------------------------------------------------------------ charts
def performance_chart(points: list[PublicPerformancePoint], width: int = 640, height: int = 240) -> dict[str, Any] | None:
    """Polylines (as coordinate strings) for each control with at least two values."""
    if len(points) < 2:
        return None
    pad = 28
    xs = {p.as_of: i for i, p in enumerate(points)}
    values = [getattr(p, key) for p in points for key, _, _ in CONTROL_SERIES if getattr(p, key) is not None]
    if not values:
        return None
    lo, hi = min(values + [100.0]), max(values + [100.0])
    span = (hi - lo) or 1.0
    n = len(points) - 1

    def xy(p: PublicPerformancePoint, v: float) -> str:
        x = pad + (width - 2 * pad) * xs[p.as_of] / n
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        return f"{x:.1f},{y:.1f}"

    series = []
    for key, label, css in CONTROL_SERIES:
        coords = [xy(p, getattr(p, key)) for p in points if getattr(p, key) is not None]
        if len(coords) >= 2:
            last = next(getattr(p, key) for p in reversed(points) if getattr(p, key) is not None)
            series.append({"key": key, "label": label, "css": css, "points": " ".join(coords), "last": last})
    base_y = pad + (height - 2 * pad) * (1 - (100.0 - lo) / span)
    return {
        "width": width, "height": height, "series": series, "base_y": f"{base_y:.1f}",
        "lo": lo, "hi": hi, "first": points[0].as_of.isoformat(), "last": points[-1].as_of.isoformat(),
    }


def weight_bar(weight: float, max_abs: float) -> dict[str, str]:
    """Horizontal bar around a centre line: long to the right (teal), short to the left (orange)."""
    scale = max(max_abs, 1e-9)
    half = 50.0 * min(abs(weight), scale) / scale
    x = 50.0 if weight >= 0 else 50.0 - half
    return {"x": f"{x:.2f}", "w": f"{half:.2f}", "css": "bar-long" if weight > 0 else "bar-short" if weight < 0 else "bar-cash"}


# ------------------------------------------------------------------------------ rendering
def make_env(line_order: list[str] | None = None) -> Environment:
    order = {sym: i for i, sym in enumerate(line_order or [])}

    def sort_lines(keys: Any) -> list[str]:
        return sorted(keys, key=lambda k: (order.get(k, len(order)), k))

    def by_line(mapping: dict[str, Any]) -> list[tuple[str, Any]]:
        """Line-keyed dicts in universe order (journal files store keys sorted alphabetically)."""
        return [(k, mapping[k]) for k in sort_lines(mapping)]

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        x=fmt_x, pct=fmt_pct, bp=fmt_bp, level=fmt_level, slot=fmt_slot, sha=short_sha, value=fmt_value,
        by_line=by_line, sort_lines=sort_lines,
    )
    env.globals.update(
        decision_chip=lambda state: chip(DECISION_CHIP, state),
        kill_chip=lambda state: chip(KILL_CHIP, state),
    )
    return env


def _status_context(view: JournalView) -> dict[str, Any]:
    st = view.status
    last_at = st.last_cycle_at or (view.cycles[0].doc.slot if view.cycles else None)
    label, css = STATUS_CHIP[st.state]
    return {
        "state": st.state, "label": label, "css": css, "note": st.note, "kill": st.kill_state,
        "last_cycle_id": st.last_cycle_id or (view.cycles[0].doc.cycle_id if view.cycles else None),
        "last_cycle_at": last_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_at else "",
    }


def build(journal_dir: Path, prompts_dir: Path, policy_dir: Path, out_dir: Path) -> list[Path]:
    """Render the five pages (plus one page per cycle) into `out_dir`. Returns the files written."""
    journal_dir, prompts_dir, policy_dir, out_dir = map(Path, (journal_dir, prompts_dir, policy_dir, out_dir))
    view = load_journal(journal_dir)
    risk = yaml.safe_load((policy_dir / "risk.yaml").read_text()) or {}
    universe = yaml.safe_load((policy_dir / "universe.yaml").read_text()) or {}
    env = make_env([str(line.get("symbol")) for line in universe.get("lines", [])])
    common = {
        "csp": Markup(CSP),               # a constant; single quotes must not be entity-escaped
        "stale_script": Markup(STALE_SCRIPT),
        "nav": NAV,
        "status": _status_context(view),
        "disclaimer": load_disclaimer(policy_dir.parent),
        "kill": risk.get("killswitch", {}),
        "gross": risk.get("gross", {}),
        "invariants": {
            "gross": invariants.GROSS_HARD_MAX, "halt": invariants.HALT_AT_PEAK_FRACTION,
            "stop": invariants.STOP_LOSS_ON_EVERY_OPEN, "human": invariants.HUMAN_APPROVAL_REQUIRED,
            "main_account": invariants.NEVER_TOUCH_MAIN_ACCOUNT,
        },
        "lines": universe.get("lines", []),
        "reference_gross_max": universe.get("reference_gross_max"),
        "max_deviations": risk.get("authority", {}).get("max_deviations_per_cycle", 3),
    }
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "cycles").mkdir(parents=True)
    written: list[Path] = []

    def render(template: str, target: str, root: str, page: str, **ctx: Any) -> None:
        html = env.get_template(template).render(**common, root=root, page=page, **ctx)
        path = out_dir / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        written.append(path)

    latest = view.cycles[0] if view.cycles else None
    ops_on_time = sum(1 for r in view.ops if r.status == "on_time")
    render("index.html.j2", "index.html", "", "index", latest=latest, cycles_count=len(view.cycles),
           ops_count=len(view.ops), ops_on_time=ops_on_time)
    render("council.html.j2", "council.html", "", "council", roster=load_roster(prompts_dir, policy_dir),
           rules=load_rules(policy_dir))
    render("cycles.html.j2", "cycles.html", "", "cycles", cycles=view.cycles)
    for cv in view.cycles:
        render("cycle.html.j2", f"cycles/{cv.doc.cycle_id}.html", "../", "cycles", cv=cv, c=cv.doc)
    book_rows = []
    if view.book is not None:
        max_abs = max([abs(b.weight_x) for b in view.book.lines.values()] + [
            abs(b.reference_weight_x or 0.0) for b in view.book.lines.values()] + [0.1])
        book_rows = [{"line": k, "b": b, "bar": weight_bar(b.weight_x, max_abs),
                      "ref": weight_bar(b.reference_weight_x, max_abs) if b.reference_weight_x is not None else None}
                     for k, b in env.filters["by_line"](view.book.lines)]
    render("book.html.j2", "book.html", "", "book", book=view.book, book_rows=book_rows,
           chart=performance_chart(view.performance), points=view.performance[-10:][::-1],
           controls=CONTROL_SERIES)
    render("failures.html.j2", "failures.html", "", "failures", incidents=view.incidents,
           withdrawn=load_withdrawn())

    static_out = out_dir / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for src in STATIC.iterdir():
        if src.is_file():
            shutil.copy2(src, static_out / src.name)
            written.append(static_out / src.name)
    for rel, src in view.copies.items():
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        written.append(dest)
    (out_dir / ".nojekyll").write_text("")

    findings = leakscan.scan_paths([out_dir])
    if findings:
        raise SiteBuildError("site output failed the leak scan: " + "; ".join(str(f) for f in findings[:20]))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the council-book static site.")
    parser.add_argument("--journal", type=Path, default=REPO / "journal")
    parser.add_argument("--prompts", type=Path, default=REPO / "prompts")
    parser.add_argument("--policy", type=Path, default=REPO / "policy")
    parser.add_argument("--out", type=Path, default=REPO / "_site")
    args = parser.parse_args(argv)
    try:
        files = build(args.journal, args.prompts, args.policy, args.out)
    except SiteBuildError as exc:
        print(f"site build failed: {exc}", file=sys.stderr)
        return 1
    print(f"site: {len(files)} files written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
