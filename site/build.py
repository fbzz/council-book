"""Static site for the public record, built from journal/, prompts/ and policy/ only.

Usage: uv run python site/build.py [--journal journal] [--prompts prompts] [--policy policy] [--out _site]

Pages: Portfolio (index.html: a broker-style holdings list, then a diagram of the latest run) ·
Runs (cycles.html and one page per run under cycles/: a per-agent transcript in execution order) ·
Agents (agents/index.html and one page per agent under agents/: its job, prompt and history) ·
How it works (how.html) · Rules (rules.html) · Record (record.html).
The old names (council.html, book.html, failures.html) are tiny redirect pages.

Rules:
- Jinja2 autoescape is ON and undefined variables fail the build; model text is never rendered as
  HTML or markdown. There is no JavaScript at all: the holdings filter is radio inputs and CSS.
- A strict Content-Security-Policy meta tag on every page; no external fonts, scripts or trackers.
  The CSP forbids inline style attributes, so data-driven widths (bars, meters) are classes defined
  in a stylesheet generated at build time (static/geometry.css).
- Links between pages are relative, so the site also works from file://.
- Every sentence that describes a run is built here from the JSON with fixed templates; no model
  text is paraphrased or summarised by a model.
- The site must build with zero cycles (status AWAITING ACCOUNT) and every output file must pass
  the leak scan, otherwise the build fails and nothing is deployed.
- A cycle file is the exact sealed document, sealed BEFORE the human decision. The final outcome
  shown for a cycle comes from its execution file, else its ops row, else the sealed document.
- Everything is generic over the set of lines: the policy's lines plus any line the book or a run
  describes (single stocks use the plain ticker as the line id, "." written "_").
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from council import invariants
from council.publish import commit_reveal, leakscan
from council.publish.journal import parse_incident
from council.publish.labels import (
    CARD_ROLES,
    COST_FIELDS,
    EVENT_KINDS,
    FRED_MEASURES,
    FRED_SERIES,
    MARKET_FIELDS,
    VOL_FIELDS,
)
from council.publish.public_models import (
    PublicAdvocate,
    PublicBook,
    PublicCall,
    PublicCard,
    PublicCommitment,
    PublicCycleV1,
    PublicExecution,
    PublicFact,
    PublicIncident,
    PublicOpsRow,
    PublicPerformancePoint,
    PublicPM,
    PublicPMReplicate,
    PublicReveal,
    PublicStatus,
)
from council.publish.redact import _clip

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
DATA = HERE / "data"
EPS = 1e-9

# No script at all: the CSP forbids every script, inline or not.
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    "script-src 'none'; base-uri 'none'; form-action 'none'"
)
REPO_URL = "https://github.com/fbzz/council-book"

NAV = (
    {"key": "portfolio", "href": "index.html", "label": "Portfolio"},
    {"key": "runs", "href": "cycles.html", "label": "Runs"},
    {"key": "agents", "href": "agents/index.html", "label": "Agents"},
    {"key": "how", "href": "how.html", "label": "How it works"},
    {"key": "rules", "href": "rules.html", "label": "Rules"},
    {"key": "record", "href": "record.html", "label": "Record"},
)
# Old page names keep working as redirects: (old file, new file, new page's name).
REDIRECTS = (
    ("council.html", "how.html", "How it works"),
    ("book.html", "index.html", "Portfolio"),
    ("failures.html", "record.html", "Record"),
)

STATUS_CHIP = {
    "AWAITING_ACCOUNT": ("AWAITING ACCOUNT", "awaiting"),
    "LIVE": ("LIVE", "executed"),
    "WARN": ("WARN", "warn"),
    "HALTED": ("HALTED", "halted"),
    "FLAT": ("FLAT", "stone"),
}
MODE_CHIP = {
    "live": ("LIVE", "executed"),
    "rehearsal": ("REHEARSAL", "rehearsal"),
    None: ("AWAITING ACCOUNT", "awaiting"),
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
# A rehearsal run never trades, whatever its sealed state: one chip that says so.
REHEARSAL_DECISION = {"label": "NOT TRADED", "css": "stone", "title": "Rehearsal: no broker account, nothing traded"}
# The note after a council change on the Portfolio page, by the latest run's final decision (live only).
DECISION_NOTE = {
    "awaiting_publication": "proposed, awaiting approval", "proposed": "proposed, awaiting approval",
    "approved": "approved", "executing": "approved, executing", "completed": "executed",
    "completed_partial": "partly executed", "rejected": "rejected, not traded", "expired": "expired, not traded",
    "superseded": "superseded, not traded", "blocked": "blocked, under review",
    "execution_unknown": "under review",
}
KILL_CHIP = {
    "NORMAL": ("NORMAL", "sealed"), "WARN": ("WARN", "warn"), "HALTED": ("HALTED", "halted"),
    "FLAT": ("FLAT", "stone"), "RESUMED": ("RESUMED", "proposed"),
}
BASIS_WORDS = {
    "council": "the council's decision",
    "council_partial_reference": "the council's decision, reference on some lines",
    "fallback_parse": "fallback: the council's answer could not be read",
    "fallback_disagreement": "fallback: the manager's runs disagreed",
    "council_unavailable": "fallback: the council was unavailable",
    "halted": "halted: no new risk",
    "code_only": "code only",
}

CODE_ROLES = (
    ("Data steward", "Builds the percentage-only fact pack from completed bars; freezes stale or closed markets.", "code"),
    ("Reference book", "The weight the rules alone would hold on each line: the default position, the centre of the "
                       "council's allowed range and the fallback.", "code"),
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
    "macro": ("Macro analyst", "Describes the macro regime and its drivers. Context only.", "CONTEXT", "macro"),
    "filings": ("Filings analyst", "Reads company filings (arrives with single stocks).", "ADVISES", "analyst"),
    "sector": ("Sector analyst", "Ranks names inside a peer group (arrives with single stocks).", "CONTEXT", "teal"),
    "bull": ("Bull advocate", "Opens the debate, then answers the bear's rebuttal.", "ADVISES", "bull"),
    "bear": ("Bear advocate", "Rebuts the bull's specific claims, citing evidence.", "ADVISES", "bear"),
    "pm": ("Portfolio manager", "Proposes at most three changes to the reference, inside ranges that code enforces. Three independent attempts; the most typical one (the medoid) is used.", "DECIDES", "pm"),
    "single_agent_control": ("Single-agent control", "One agent, the same facts, no analysts and no debate. Published as a control; it never trades.", "CONTEXT", "stone"),
}
ROSTER_AGENT = {"news": "news", "macro": "macro", "bull": "bull", "bear": "bear", "pm": "pm",
                "single_agent_control": "control"}
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
    # key, name, css, short end-of-line label, what it is
    ("c0", "As executed", "c0", "Executed",
     "The real book, from the broker's equity marks, time-weighted so deposits and withdrawals do not count as returns."),
    ("c2", "Reference", "c2", "Reference",
     "The mechanical reference book with the same costs and deadband. The council is scored against this."),
    ("c2x", "Reference, exposure-matched", "c2x", "Ref. matched",
     "The reference scaled to the council's average exposure, so \"held less risk\" is not mistaken for skill."),
    ("c3", "Hold", "c3", "Hold", "The starting book, never traded again."),
    ("c4_spy", "Buy-and-hold SPY", "c4a", "SPY", "The S&P 500 fund SPY, bought once and held."),
    ("c4_btc", "Buy-and-hold BTC", "c4b", "BTC", "Bitcoin, bought once and held."),
)

# Evidence ids -> plain labels (MARKET_FIELDS, VOL_FIELDS, COST_FIELDS, FRED_SERIES, FRED_MEASURES,
# EVENT_KINDS, CARD_ROLES) live in council.publish.labels, shared with the facts table of the
# public record. The raw id always stays in the title attribute for auditors.
CHECK_NAMES = {
    "gross": "Total exposure", "net": "Net exposure", "short_gross": "Total short", "kill_switch": "Kill switch",
    "catastrophe_stop": "Loss if every stop hits", "reentry_cooloff": "Cool-off after a stop",
    "line_caps": "Largest line vs its cap", "crypto_total": "Crypto total", "fx_total": "Currencies total",
    "equity_beta_cluster": "Equity lines together", "leverage": "Leverage", "margin_use": "Margin use",
    "ex_ante_vol": "Expected yearly volatility", "vol_breaker": "Volatility breaker",
    "authority": "Council changes allowed", "deadband": "Deadband", "min_hold": "Minimum hold",
    "cycle_increase": "Risk added this run", "turnover_7d": "Traded in 7 days", "turnover_30d": "Traded in 30 days",
    "cycle_cost_bps": "Cost this run (bp)", "cost_30d_bps": "Cost over 30 days (bp)",
    "carry_bps_day": "Overnight cost (bp a day)", "net_of_cost_gate": "Worth its cost",
    "event_block": "Event window", "anti_chase": "No chasing", "data_freshness": "Data freshness",
    "quote_freshness": "Quote freshness", "market_open": "Market open", "blockers": "Blockers",
    "legs": "Orders in the proposal", "material_change": "Something material changed",
}
HOLD_WORDS = (
    (re.compile(r"^R10 level .* outside band .*$"), "asked for more than its allowed range; clipped"),
    (re.compile(r"^R10 deviation beyond the (\d+) allowed per cycle$"), r"over the limit of \1 changes per run"),
    (re.compile(r"^R10 no band: hold current$"), "no allowed range: kept as it is"),
    (re.compile(r"^R18 frozen data$"), "data too old"),
    (re.compile(r"^R19 market closed$"), "market closed"),
    (re.compile(r"^R20 blocker$"), "blocked"),
    (re.compile(r"^scaled to fit aggregate limits.*$"), "scaled down to fit the book's limits"),
    (re.compile(r"^deadband$"), "change too small to trade"),
)
HOLD_LINE = re.compile(r"^([A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?): (.+)$")   # public line ids (BRK_B, V)
STATUS_WORDS = {
    "on_time": "ran on time", "late": "ran late", "missed": "ran after its slot had passed (a catch-up)",
    "skipped_overlap": "skipped (overlap)",
    "skipped_disk": "skipped (disk)", "skipped_broker": "skipped (broker)", "aborted": "aborted",
    "halted": "halted", "dry_run": "dry run",
}
SLEEVES = (("core", "Core"), ("crypto", "Crypto"), ("overlay", "Overlays"))
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SPELLED = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
# Internal codes -> plain words. The raw code only ever appears in a title attribute.
AUDIT_WORDS = {
    "parse_fail": "the answer could not be read", "no_decision": "no usable answer",
    "over_max_deviations": "more changes than allowed",
    "unknown_line": "a line that does not exist", "not_admitted": "a line without fresh data",
    "reference_only": "a line the council may not change", "duplicate": "the same line twice",
    "unknown_evidence": "cited evidence that is not in the fact pack",
    "direction_mismatch": "the direction does not match the size",
    "short_without_risk_down_card": "a short without a card that argues for less risk",
}
SKIP_WORDS = {"below_broker_minimum": "below the broker's minimum order size"}
WHY_WORDS = {"scheduled": "a scheduled review", "vol_shock": "a volatility shock", "event": "a scheduled event",
             "manual": "a manual run", "kill_switch": "the kill switch"}
CADENCE_WORDS = {"every_cycle": "every run", "first_cycle_of_utc_day": "first run of each UTC day"}
CALENDAR_UNLOADED = "economic-release dates not loaded, so data-release blocks could not be checked"
TREND_FRACTIONS = {1.0: "full", 0.75: "¾", 0.5: "½", 0.25: "¼", 0.0: "none"}


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
    return "—" if v is None else f"{v:.2f}".replace("-", "−")


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


def fmt_pct1(v: float | None) -> str:
    """A percent with one decimal: 82.79 -> "82.8%"."""
    return "—" if v is None else f"{v:.1f}%".replace("-", "−")


def fmt_late(minutes: int) -> str:
    """206 -> "3 h 26 min", 12 -> "12 min"."""
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "")


def fmt_clock(ts: datetime | None) -> str:
    return "—" if ts is None else f"{ts.astimezone(UTC):%H:%M} UTC"


def fmt_short_when(ts: datetime | None) -> str:
    """25 Sep, 10:40 UTC"""
    if ts is None:
        return "—"
    ts = ts.astimezone(UTC)
    return f"{ts.day} {MONTHS[ts.month - 1]}, {ts:%H:%M} UTC"


def parse_cycle_id(cycle_id: str) -> datetime | None:
    try:
        return datetime.strptime(cycle_id, "%Y-%m-%dT%H%MZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def fmt_share(v: float | None) -> str:
    """A multiple of NAV as a share of the portfolio: 0.35 -> "35%", 0.134 -> "13.4%"."""
    if v is None:
        return "—"
    p = round(v * 100, 1)
    if abs(p) < 0.05:
        return "0%"
    text = f"{abs(p):.1f}".rstrip("0").rstrip(".")
    return ("−" if p < 0 else "") + text + "%"


def fmt_share1(v: float | None) -> str:
    """A weight with one fixed decimal, so a right-aligned column lines up: 0.35 -> "35.0%"."""
    if v is None:
        return "—"
    p = round(v * 100, 1)
    if abs(p) < 0.05:
        return "0.0%"
    return ("−" if p < 0 else "") + f"{abs(p):.1f}%"


def fmt_when(ts: datetime | None) -> str:
    """25 Sep 2026, 10:40 UTC"""
    if ts is None:
        return "—"
    ts = ts.astimezone(UTC)
    return f"{ts.day} {MONTHS[ts.month - 1]} {ts.year}, {ts:%H:%M} UTC"


def fmt_day(value: date | datetime | str | None, year: bool = True) -> str:
    if value is None:
        return "—"
    try:
        d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def fmt_signed(v: float | None, digits: int = 2) -> str:
    """A signed percent: 1.95 -> "+1.95%", -2.61 -> "−2.61%", 0 -> "0.00%"."""
    if v is None:
        return "—"
    r = round(v, digits)
    if abs(r) < 10 ** -digits / 2:
        return f"{0:.{digits}f}%"
    return ("+" if r > 0 else "−") + f"{abs(r):.{digits}f}%"


def move_dir(v: float | None, digits: int = 2) -> str:
    """"up", "down" or "flat" for a signed percent at the shown precision; "none" when unknown."""
    if v is None:
        return "none"
    r = round(v, digits)
    return "up" if r > 0 else "down" if r < 0 else "flat"


def fmt_int(n: int | None) -> str:
    """5610 -> "5,610" (a count, never an amount)."""
    return "—" if n is None else f"{n:,d}"


def fmt_secs(ms: int | None) -> str:
    """3005 -> "3.0 s", 91012 -> "1 min 31 s"."""
    if ms is None:
        return "—"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f} s"
    m, rest = divmod(int(round(s)), 60)
    return f"{m} min" + (f" {rest} s" if rest else "")


def trim_number(v: float, digits: int) -> str:
    """A number with at most `digits` decimals and no trailing zeros; minus as "−"."""
    text = f"{abs(v):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return ("−" if v < 0 and text not in ("0", "") else "") + (text or "0")


def median(values: list[float]) -> float | None:
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def chip(mapping: dict, key: Any, title: str = "") -> dict[str, str]:
    label, css = mapping.get(key, (str(key).upper(), "awaiting"))
    return {"label": label, "css": css, "title": title}


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def join_words(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def clip(text: str, limit: int) -> str:
    """A short excerpt that ends at a sentence or word boundary, marked "…" (the publisher's own
    cut, council.publish.redact._clip)."""
    return _clip(" ".join(text.split()), limit)


def excerpt(text: str, limit: int = 280) -> tuple[str, bool]:
    """The first ~limit characters, cut at a word boundary. Returns (text, was_cut)."""
    text = " ".join(text.split())
    if len(text) <= limit + 20:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(",;:—- ") + "…", True


# ------------------------------------------------------------------------------ loading
@dataclass
class CycleView:
    doc: PublicCycleV1
    path: str                       # relative path of the cycle JSON inside the site copy
    commitment: PublicCommitment | None = None
    reveal: PublicReveal | None = None
    verified: bool = False
    ops: PublicOpsRow | None = None
    execution: PublicExecution | None = None
    execution_path: str | None = None

    # The sealed document predates the human decision: later sources win.
    @property
    def final_state(self) -> str | None:
        if self.execution is not None:
            return self.execution.decision_state
        if self.ops is not None and self.ops.decision_state is not None:
            return self.ops.decision_state
        return self.doc.decision.state

    @property
    def human_outcome(self) -> str:
        if self.ops is not None and self.ops.human_outcome != "none":
            return self.ops.human_outcome
        if self.execution is not None:
            return "approved"
        return self.doc.decision.human_outcome

    @property
    def decision_reason(self) -> str:
        return (self.ops.decision_reason if self.ops is not None else "") or self.doc.decision.reason

    @property
    def approved_slot(self) -> datetime | None:
        for value in (self.execution.approved_slot if self.execution else None,
                      self.ops.approved_slot if self.ops else None, self.doc.decision.approved_slot):
            if value is not None:
                return value
        return None

    @property
    def min_agreement_pct(self) -> float | None:
        values = list(self.doc.pm.agreement_pct.values())
        return min(values) if values else None

    @property
    def control_agrees(self) -> bool | None:
        """Did the single-agent control land on the council's levels? None without a control."""
        control = self.doc.single_agent
        if control is None or not control.levels or not self.doc.pm.levels:
            return None
        lines = set(control.levels) & set(self.doc.pm.levels)
        return all(abs(control.levels[k] - self.doc.pm.levels[k]) < 1e-9 for k in lines)

    @property
    def chip(self) -> dict[str, str]:
        if self.rehearsal:
            return dict(REHEARSAL_DECISION)
        return chip(DECISION_CHIP, self.final_state)

    @property
    def ran_at(self) -> datetime:
        """When the run actually happened: its slot plus how late it started."""
        return self.doc.slot + timedelta(minutes=self.doc.late_by_min)

    @property
    def mode_chip(self) -> dict[str, str]:
        return chip(MODE_CHIP, self.doc.mode)

    @property
    def rehearsal(self) -> bool:
        return self.doc.mode == "rehearsal"


@dataclass
class JournalView:
    status: PublicStatus
    cycles: list[CycleView] = field(default_factory=list)
    book: PublicBook | None = None
    performance: list[PublicPerformancePoint] = field(default_factory=list)
    incidents: list[PublicIncident] = field(default_factory=list)
    ops: list[PublicOpsRow] = field(default_factory=list)
    copies: dict[str, Path] = field(default_factory=dict)   # site path -> journal source file
    commitments: dict[str, PublicCommitment] = field(default_factory=dict)   # every sealed run, revealed or not


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_journal(journal_dir: Path) -> JournalView:
    status_file = journal_dir / "status.json"
    status = PublicStatus.model_validate_json(status_file.read_text()) if status_file.exists() else PublicStatus()
    view = JournalView(status=status)
    view.ops = [PublicOpsRow.model_validate(r) for r in _jsonl(journal_dir / "ops" / "cycles.jsonl")]
    ops_by_cycle = {r.cycle_id: r for r in view.ops}
    executions: dict[str, tuple[PublicExecution, str]] = {}
    executions_dir = journal_dir / "executions"
    for file in sorted(executions_dir.rglob("*.json")) if executions_dir.exists() else []:
        execution = PublicExecution.model_validate_json(file.read_text())
        rel = f"journal/{file.relative_to(journal_dir).as_posix()}"
        executions[execution.cycle_id] = (execution, rel)
        view.copies[rel] = file
    cycles_dir = journal_dir / "cycles"
    for file in sorted(cycles_dir.rglob("*.json")) if cycles_dir.exists() else []:
        if file.name.endswith(".reveal.json"):
            continue
        data = file.read_bytes()
        raw = json.loads(data)
        doc = PublicCycleV1.model_validate(raw)
        rel = file.relative_to(journal_dir).as_posix()
        cv = CycleView(doc=doc, path=f"journal/{rel}", ops=ops_by_cycle.get(doc.cycle_id))
        if doc.cycle_id in executions:
            cv.execution, cv.execution_path = executions[doc.cycle_id]
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
            # The file is the exact sealed bytes; an older pretty-printed file re-hashes canonically.
            cv.verified = commit_reveal.verify_bytes(data, cv.reveal.salt, cv.commitment.commitment_sha256) or \
                commit_reveal.verify(raw, cv.reveal.salt, cv.commitment.commitment_sha256)
        view.cycles.append(cv)
    view.cycles.sort(key=lambda c: c.doc.slot, reverse=True)
    commitments_dir = journal_dir / "commitments"
    for file in sorted(commitments_dir.rglob("*.json")) if commitments_dir.exists() else []:
        commitment = PublicCommitment.model_validate_json(file.read_text())
        view.commitments[commitment.cycle_id] = commitment
        view.copies[f"journal/{file.relative_to(journal_dir).as_posix()}"] = file
    book_file = journal_dir / "book" / "latest.json"
    if book_file.exists():
        view.book = PublicBook.model_validate_json(book_file.read_text())
    view.performance = sorted(
        (PublicPerformancePoint.model_validate(r) for r in _jsonl(journal_dir / "performance" / "index.jsonl")),
        key=lambda p: p.as_of,
    )
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
        # The role comes from the file name, else the manifest key, else the id. An id such as
        # "council-bear/v1" would give the stem "v1", so a bare version falls back to its prefix.
        source = str(meta.get("file") or key or name)
        stem = re.split(r"[@:]", Path(source).stem)[0]
        if re.fullmatch(r"v\d+", stem):
            stem = re.sub(r"^council-", "", source.split("/")[0])
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
            "cadence": CADENCE_WORDS.get(str(cfg.get("cadence", "every_cycle")),
                                         str(cfg.get("cadence", "every_cycle")).replace("_", " ")),
            "prompts": manifest.get(role, []),
            "page": f"agents/{ROSTER_AGENT[role]}.html" if role in ROSTER_AGENT else "",
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


def _share0(v: Any) -> str:
    return f"{float(v) * 100:.0f}%"


def kill_phrase(kill: dict[str, Any]) -> str:
    """The one wording of the kill switch, used on every page."""
    warn = (1 - float(kill.get("warn_at", 0.80))) * 100
    halt = (1 - float(kill.get("halt_at", 0.75))) * 100
    return f"−{warn:.0f}%: no new risk · −{halt:.0f}%: stop, and a proposal to sell everything goes to the human"


def reference_gloss(reference: dict[str, Any]) -> str:
    """What the mechanical reference is, in words, with the trend levels from policy/reference.yaml."""
    levels = (reference.get("trend") or {}).get("levels") or {"up": 1.0, "mixed": 0.75, "down": 0.25}
    parts = [f"{k} = {TREND_FRACTIONS.get(float(levels[k]), f'{float(levels[k]):.2f}')}"
             for k in ("up", "mixed", "down") if k in levels]
    return ("the weight the rules alone would hold: a fixed base weight per line, scaled by its trend ("
            + ", ".join(parts) + ") and trimmed when the line is unusually volatile")


def rule_plain(key: str, v: Any) -> str:
    """One plain sentence per rule, with the numbers from policy/risk.yaml. Empty if unknown."""
    try:
        if key == "gross":
            return (f"All positions added together (long and short) stay under {v['proposal_max']}x the portfolio; "
                    f"code refuses anything above {v['hard_max']}x.")
        if key == "net":
            return (f"Long minus short stays between {v['min']}x and {v['max']}x the portfolio; "
                    f"shorts together at most {v['short_gross_max']}x.")
        if key == "killswitch":
            return f"Measured from the best value ever reached: {kill_phrase(v)}."
        if key == "catastrophe_stop":
            return (f"Every new position carries a stop-loss order set well away from the price (at most {_share0(v['cap'])} away), "
                    "so one bad day cannot sink the book.")
        if key == "reentry_cooloff_days":
            return (f"After a stop-loss is hit, the line waits {v['default']} days ({v['crypto']} for crypto) "
                    "before it can be bought again.")
        if key == "caps":
            biggest = max(float(x) for x in v["line"].values())
            return (f"Each line has its own cap (the largest is {_share0(biggest)} of the portfolio); crypto together at most "
                    f"{_share0(v['crypto_total'])}, currencies {_share0(v['fx_total'])}, the three equity lines "
                    f"{_share0(v['equity_beta_cluster']['max'])}.")
        if key == "leverage_caps":
            return (f"Borrowing is capped by asset class: at most {v['crypto']}x on crypto, {v['index']}x on indices, "
                    f"{v['fx']}x on currencies.")
        if key == "margin_use_max":
            return f"At most {_share0(v)} of the account may be tied up as margin, which keeps a cash reserve."
        if key == "ex_ante_vol_hard":
            return f"The book's expected yearly swings stay below {_share0(v)}."
        if key == "vol_breaker":
            return (f"If short-term volatility jumps to {v['instrument_ratio']} times its usual level on a line "
                    f"({v['book_ratio']} times for the whole book), no risk is added.")
        if key == "authority":
            return (f"The council may change at most {v['max_deviations_per_cycle']} lines per run, each inside a range "
                    "that code sets from the line's trend.")
        if key == "deadband":
            return (f"Changes smaller than {v['level']} of a line's full size, or under {_share0(v['min_nav_share'])} "
                    "of the portfolio, are not traded.")
        if key == "min_hold_days":
            return (f"A position is kept at least {v['default']} days ({v['crypto']} for crypto) before it is reversed; "
                    "moving back to the reference is always allowed.")
        if key == "churn":
            return (f"Trading is limited to {v['turnover_7d_max']}x the portfolio in 7 days and "
                    f"{v['turnover_30d_max']}x in 30 days.")
        if key == "cost_budget":
            return (f"One run may spend at most {v['cycle_max_bps']} basis points (hundredths of a percent) of the portfolio "
                    f"on trading costs, and {v['discretionary_30d_max_bps']} over 30 days.")
        if key == "net_of_cost_gate":
            return "A trade must be expected to earn clearly more than it costs, after spread, fees and overnight financing."
        if key == "event_block":
            return (f"No adds from {v['macro_before_h']} h before to {v['macro_after_h']} h after a scheduled macro event "
                    "(Fed decisions, inflation, jobs); selling is always allowed.")
        if key == "anti_chase_sigma":
            return f"No buying right after a one-day jump larger than {v} times the usual daily move."
        if key == "freshness":
            return (f"Prices must be recent: daily bars at most {v['daily_bar_max_h']} h old, quotes at most "
                    f"{v['quote_max_s']} s old.")
        if key == "material_change_required":
            return "A council change is traded only if some fact actually changed since the last decision."
        if key == "proposal":
            return f"A proposal holds at most {v['max_legs']} orders."
        if key == "approval":
            return (f"A person approves every order, only between {v['window']['start']} and {v['window']['end']} "
                    "Lisbon time; prices and costs are checked again at that moment.")
        if key == "reconcile":
            return (f"After trading, the real book is compared with the plan; a gap above {_share0(v['drift_max'])} "
                    "is flagged.")
        if key == "priority":
            return "A sell-everything proposal always outranks a compliance fix, which outranks a routine rebalance."
    except (KeyError, TypeError, ValueError, AttributeError):
        return ""
    return ""


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
            "plain": rule_plain(key, value),
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


# ------------------------------------------------------------------------------ lines
@dataclass(frozen=True)
class LineInfo:
    symbol: str
    name: str
    sleeve: str
    in_reference: bool
    council: bool                   # may the council deviate from the reference on this line?
    asset_class: str | None = None
    session: str | None = None


def policy_session(raw: dict[str, Any]) -> str | None:
    """The trading session of a policy line's preferred long vehicle (as `LineSpec.session`):
    London-listed '.L' vehicles on London hours, crypto 24/7, index / commodity / FX 24/5, the
    rest (stocks, US ETF CFDs) on US hours."""
    ac = raw.get("asset_class")
    if ac is None:
        return None
    if ac == "crypto":
        return "crypto"
    longs = ((raw.get("vehicles") or {}).get("long") or [])
    first = str(longs[0].get("symbol", "")) if longs and isinstance(longs[0], dict) else str(raw.get("symbol", ""))
    if first.endswith(".L"):
        return "lse"
    if ac in ("index", "commodity", "fx"):
        return "fx24x5"
    return "us"


class Lines:
    """The lines, in policy order, with their plain names, asset classes and sessions. Lines the
    policy does not list (single stocks added later) are described by the book when it knows
    them, else by their ticker; they sort after the policy's lines."""

    def __init__(self, universe: dict[str, Any]):
        self.info: dict[str, LineInfo] = {}
        for raw in universe.get("lines", []) or []:
            sym = str(raw.get("symbol"))
            self.info[sym] = LineInfo(
                symbol=sym, name=str(raw.get("name") or sym), sleeve=str(raw.get("sleeve") or "core"),
                in_reference=bool(raw.get("in_reference", True)),
                council=bool(raw.get("council_deviations", True)),
                asset_class=raw.get("asset_class"), session=policy_session(raw),
            )
        self.order = {sym: i for i, sym in enumerate(self.info)}

    def describe(self, book: PublicBook | None) -> None:
        """Add the book's own description of each line (name, asset class, session): it names
        lines the policy does not list and fills what the policy leaves out."""
        if book is None:
            return
        for sym, b in book.lines.items():
            known = self.info.get(sym)
            if known is None:
                ac = b.asset_class
                self.info[sym] = LineInfo(
                    symbol=sym, name=b.name or ticker(sym), sleeve="stock" if ac == "stock" else "other",
                    in_reference=True, council=True, asset_class=ac, session=b.session,
                )
            elif known.asset_class is None or known.session is None:
                self.info[sym] = LineInfo(
                    symbol=sym, name=known.name, sleeve=known.sleeve, in_reference=known.in_reference,
                    council=known.council, asset_class=known.asset_class or b.asset_class,
                    session=known.session or b.session,
                )

    def name(self, sym: str) -> str:
        info = self.info.get(sym)
        return info.name if info else ticker(sym)

    def asset_class(self, sym: str) -> str | None:
        info = self.info.get(sym)
        return info.asset_class if info else None

    def session(self, sym: str) -> str | None:
        info = self.info.get(sym)
        return info.session if info else None

    def sort(self, keys: Any) -> list[str]:
        return sorted(set(keys), key=lambda k: (self.order.get(k, len(self.order)), k))


def ticker(sym: str) -> str:
    """A line id as a ticker: single stocks write "." as "_" in their id (BRK_B -> BRK.B)."""
    return sym.replace("_", ".")


# ------------------------------------------------------------------------------ geometry
class Geometry:
    """Data-driven widths and offsets as CSS classes (the CSP forbids inline style attributes).
    Values are percentages of the containing track, rounded to 0.01%."""

    PROPS = {"width": "gw", "left": "gl", "right": "gr"}

    def __init__(self) -> None:
        self.rules: dict[str, str] = {}

    def cls(self, prop: str, pct: float) -> str:
        n = round(max(0.0, min(100.0, pct)) * 100)
        name = f"{self.PROPS[prop]}-{n}"
        self.rules[name] = f".{name} {{ {prop}: {n / 100:.2f}%; }}"
        return name

    def ring(self, pct: float | None) -> str:
        """A progress ring's fill (a conic-gradient reads --p), in whole percent."""
        n = 0 if pct is None else round(max(0.0, min(100.0, float(pct))))
        name = f"gp-{n}"
        self.rules[name] = f".{name} {{ --p: {n}%; }}"
        return name

    def css(self) -> str:
        head = ("/* Generated by site/build.py: bar widths, tick positions and ring fills "
                "(the CSP forbids inline styles). */\n")
        return head + "\n".join(self.rules[k] for k in sorted(self.rules, key=lambda k: (k[:2], int(k[3:])))) + "\n"


# ------------------------------------------------------------------------------ icons
ICON_FILE = DATA / "icons.json"
# A status's css class -> its icon (the word is always shown next to it).
STATUS_ICONS = {"ok": "circle-check", "failed": "circle-x", "timeout": "clock-3", "fallback": "circle-alert",
                "idle": "circle-minus", "waiting": "hourglass"}
# Each agent's avatar icon; the bull's rebuttal has its own.
AGENT_ICONS = {"data": "database", "reference": "scale", "vol": "activity", "event": "calendar",
               "news": "newspaper", "macro": "globe", "bull": "trending-up", "bear": "trending-down",
               "pm": "briefcase", "control": "git-compare-arrows", "audit": "file-check", "risk": "shield-check",
               "costs": "calculator", "human": "user", "rebuttal": "message-square-quote"}


def load_icons(path: Path = ICON_FILE) -> dict[str, list[list[Any]]]:
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text()).get("icons", {}))


def icon_svg(icons: dict[str, list[list[Any]]], name: str, cls: str = "") -> Markup:
    """An inline, decorative SVG icon (Lucide geometry, ISC): 24x24, 2px round stroke in the
    current text colour. Presentation attributes only; never a style attribute."""
    parts = []
    for tag, attrs in icons.get(name, []):
        if not re.fullmatch(r"[a-z]+", str(tag)):
            continue
        body = " ".join(f'{k}="{escape(v)}"' for k, v in attrs.items() if re.fullmatch(r"[a-z]+", str(k)))
        parts.append(f"<{tag} {body}/>")
    klass = "i" + (f" {escape(cls)}" if cls else "") + (f" i-{name}" if name in icons else "")
    return Markup(
        f'<svg class="{klass}" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        + "".join(parts) + "</svg>")


AXIS_STEPS = (0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0)


def nice_scale(max_abs: float) -> float:
    return next((s for s in AXIS_STEPS if s >= max_abs - EPS), AXIS_STEPS[-1])



# ------------------------------------------------------------------------------ evidence labels
def evidence_label(ref: Any, lines: Lines) -> dict[str, str]:
    """A plain label for an evidence reference; `raw` is the id, kept in a title attribute."""
    kind = getattr(ref, "kind", "")
    if kind == "broker_feed":
        return {"label": "news item", "raw": f"{ref.id} · broker news item, cited by id; its text is not republished",
                "css": "feed"}
    if kind == "fred":
        raw = f"M:{ref.series}" + (f".{ref.measure}" if ref.measure else "") + (f"@{ref.as_of}" if ref.as_of else "")
        label = FRED_SERIES.get(ref.series, ref.series)
        if ref.measure:
            label += " · " + FRED_MEASURES.get(ref.measure, ref.measure)
        if ref.as_of:
            label += f", {fmt_day(ref.as_of, year=False)}"
        if ref.publishable and ref.value is not None:
            unit = {"pct": "%", "bps": " bps", "x": "x", "ratio": ""}.get(ref.unit or "", "")
            label += f": {ref.value:g}{unit}".replace("-", "−")
        if not ref.publishable:
            raw += " · value not publishable"
        return {"label": label, "raw": raw, "css": "fred"}
    rid = str(getattr(ref, "id", ""))
    parts = rid.split(":")
    prefix = parts[0]
    if prefix in ("F", "V", "C") and len(parts) >= 3:
        table = {"F": MARKET_FIELDS, "V": VOL_FIELDS, "C": COST_FIELDS}[prefix]
        what = table.get(parts[2], parts[2].replace("_", " "))
        return {"label": f"{lines.name(parts[1])} · {what}", "raw": rid,
                "css": {"F": "market", "V": "vol", "C": "cost"}[prefix]}
    if prefix == "K" and len(parts) >= 3:
        return {"label": f"{CARD_ROLES.get(parts[1], parts[1] + ' card')} {parts[2]}", "raw": rid, "css": "card"}
    if prefix == "E":
        body, _, day = rid[2:].partition("@")
        kind_, _, sym = body.partition(":")
        label = EVENT_KINDS.get(kind_, kind_.upper())
        if sym:
            label += f" {lines.name(sym)}"
        if day:
            label += f" {fmt_day(day, year=False)}"
        return {"label": label, "raw": rid, "css": "event"}
    if prefix == "S":
        return {"label": "company filing", "raw": rid, "css": "filing"}
    return {"label": rid, "raw": rid, "css": kind or "market"}


# ------------------------------------------------------------------------------ the facts table
FACT_DIGITS = {"pct": 2, "sigma": 2, "ratio": 3, "x": 3, "bps": 1, "bps_day": 2, "hours": 1, "days": 1}
FACT_SUFFIX = {"pct": "%", "x": "x", "ratio": "", "bps": " bp", "bps_day": " bp a day", "days": " days",
               "hours": " h", "sigma": "σ"}
FACT_KINDS = (
    # kind, heading, accent
    ("market", "Market", "code"), ("vol", "Volatility", "code"), ("cost", "Costs", "risk"),
    ("macro", "Macro (FRED)", "macro"), ("event", "Scheduled events", "human"),
    ("news", "Broker news items (ids only)", "analyst"), ("filing", "Company filings (ids only)", "analyst"),
    ("fundamental", "Fundamentals", "code"),
)
WITHHELD_WORDS = {
    "licensed_series": "licensed series: cited by name, value not republished",
    "not_publishable": "not publishable",
    "broker_data": "broker data: not republished",
    "unknown_source": "unknown source: not shown",
}
SOURCE_WORDS = {
    "tiingo": "Tiingo history", "binance": "Binance history", "broker": "broker", "fred": "FRED",
    "clock": "market clock", "policy": "cost policy", "calendar": "public calendar",
    "broker_feed": "broker feed", "filing": "filing", "unknown": "unknown",
}


def fmt_fact_value(value: Any, unit: str | None, field: str = "") -> str:
    """A facts-table value in words: 11.7 pct -> "11.7%", True market_open -> "open"."""
    if value is None:
        return ""
    if isinstance(value, bool):
        if field == "market_open":
            return "open" if value else "closed"
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return trim_number(float(value), FACT_DIGITS.get(unit or "", 3)) + FACT_SUFFIX.get(unit or "", "")
    return str(value).replace("_", " ")


def anchor_slug(prefix: str, raw: str) -> str:
    """A fragment id from an evidence id: "F:NDX:trend" -> "f-F-NDX-trend"."""
    return prefix + "-" + (re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-") or "x")


class FactIndex:
    """One run's facts table, looked up by evidence id. `chip(ref)` resolves an evidence reference
    to "label: value" with a link to its row in the run page's facts table (or, for a card, to the
    card); `base` is "" on the run page and "<root>cycles/<id>.html" elsewhere."""

    def __init__(self, doc: PublicCycleV1, lines: Lines, base: str = ""):
        self.lines = lines
        self.base = base
        self.facts = {f.id: f for f in doc.facts}
        self.cards = {k.card_id: k for k in doc.cards}

    def fact_anchor(self, rid: str) -> str:
        return anchor_slug("f", rid)

    def href(self, rid: str) -> str:
        if rid in self.cards:
            return f"{self.base}#{anchor_slug('k', rid)}"
        if rid in self.facts:
            return f"{self.base}#{self.fact_anchor(rid)}"
        return ""

    def value_words(self, f: PublicFact) -> str:
        field = f.id.split(":")[-1] if f.id[:1] in "FVC" else ""
        return fmt_fact_value(f.value, f.unit, field)

    def chip(self, ref: Any) -> dict[str, str]:
        base = evidence_label(ref, self.lines)
        kind = getattr(ref, "kind", "")
        rid = base["raw"].split(" · ")[0]
        out = {**base, "value": "", "note": "", "href": self.href(rid)}
        f = self.facts.get(rid)
        if kind == "fred":
            if not getattr(ref, "publishable", True):
                out["note"] = "value not republished"
            return out
        if f is None:
            return out
        if f.value is not None and kind not in ("broker_feed", "card"):
            out["value"] = self.value_words(f)
        elif f.withheld is not None:
            out["note"] = "not public"
            out["raw"] = f"{rid} · {WITHHELD_WORDS.get(f.withheld, f.withheld)}"
        return out

    def row(self, f: PublicFact) -> dict[str, Any]:
        return {
            "id": f.id, "anchor": self.fact_anchor(f.id), "label": f.label,
            "line": self.lines.name(f.line) if f.line else "", "ticker": ticker(f.line) if f.line else "",
            "value": self.value_words(f) if f.value is not None else "",
            "withheld": WITHHELD_WORDS.get(f.withheld, f.withheld) if f.withheld else "",
            "source": SOURCE_WORDS.get(f.source or "", f.source or ""),
            "as_of": fmt_short_when(f.as_of) if f.as_of else "",
            "kind": f.kind,
        }


def ref_id(ref: Any) -> str:
    """The evidence id of a reference, as the facts table keys it."""
    if getattr(ref, "kind", "") == "fred":
        return (f"M:{ref.series}" + (f".{ref.measure}" if ref.measure else "")
                + (f"@{ref.as_of}" if ref.as_of else ""))
    return str(getattr(ref, "id", ""))


# ------------------------------------------------------------------------------ agents and calls
@dataclass(frozen=True)
class AgentSpec:
    slug: str                        # page name (agents/<slug>.html) and run-page anchor (a-<slug>)
    name: str
    kind: str                        # CODE | LLM | HUMAN
    accent: str
    group: str                       # the run page's contents groups
    job: str                         # one sentence
    roles: tuple[str, ...] = ()      # the call roles this agent answers for
    source: str = ""                 # a repository path: the prompt or the code
    more: str = ""                   # the longer description on its own page


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("data", "Data steward", "CODE", "code", "Code officers",
              "Builds the percentage-only fact pack from completed daily bars and freezes stale or closed markets.",
              source="src/council/facts/pack.py",
              more="Every fact carries the time it became available; a run may only use facts available at its "
                   "slot start, and only completed bars. A lookahead test checks it."),
    AgentSpec("reference", "Reference book", "CODE", "code", "Code officers",
              "Computes the weight the rules alone would hold on each line: the default, the benchmark and the fallback.",
              source="src/council/reference/book.py",
              more="A fixed base weight per line, scaled by its trend and trimmed when the line is unusually "
                   "volatile. The book is long-only and never levered."),
    AgentSpec("vol", "Volatility officer", "CODE", "code", "Code officers",
              "Writes a volatility-shock card when a line's short-term volatility jumps, and trips the breaker.",
              source="src/council/deliberation/officers.py",
              more="A volatility card is one of the two kinds of card that allow the council to cut a line in an "
                   "uptrend."),
    AgentSpec("event", "Event officer", "CODE", "code", "Code officers",
              "Writes event cards for scheduled macro releases and blocks adds in the window around them.",
              source="src/council/deliberation/officers.py",
              more="It never forces a sale: selling is always allowed inside an event window."),
    AgentSpec("news", "News analyst", "LLM", "analyst", "Analysts",
              "Reads broker news items and writes evidence cards that cite them by id.",
              roles=("news",), source="prompts/news.md",
              more="The news text itself is licensed and never republished: a card cites a feed item by its id "
                   "only, and the analyst's own short paraphrase is shown."),
    AgentSpec("macro", "Macro analyst", "LLM", "macro", "Analysts",
              "Describes the macro regime and its drivers from public macro data; context only.",
              roles=("macro",), source="prompts/macro.md",
              more="It runs on the first run of each UTC day. Code never acts on its regime or tilts; the "
                   "advocates and the manager may cite it."),
    AgentSpec("bull", "Bull", "LLM", "bull", "Debate",
              "Opens the debate with a case for a set of positions, then answers the bear.",
              roles=("bull_open", "bull_rebuttal"), source="prompts/bull_open.md",
              more="Two turns per run: the opening (claims the bear must answer) and the rebuttal (after the "
                   "bear). It has no authority; the manager decides."),
    AgentSpec("bear", "Bear", "LLM", "bear", "Debate",
              "Answers the bull's claims one by one, conceding or contesting each, and argues its own case.",
              roles=("bear",), source="prompts/bear.md",
              more="It must answer the bull's specific claims by their ids, with evidence. It has no authority."),
    AgentSpec("pm", "Portfolio manager", "LLM", "pm", "Decision",
              "Makes three separate attempts at a decision of at most three changes; the most typical one is used.",
              roles=("pm",), source="prompts/pm.md",
              more="Each attempt may move at most three lines, inside ranges that code sets. The attempt closest "
                   "to the others (the medoid) is used: a real decision, never an average."),
    AgentSpec("control", "Single-agent control", "LLM", "stone", "Decision",
              "One agent with the same facts, no analysts and no debate, as a comparison; it never trades.",
              roles=("single_agent",), source="prompts/single_agent.md",
              more="Published so readers can see what the council's analysts and debate change."),
    AgentSpec("audit", "Auditor and bands", "CODE", "code", "Checks",
              "Reverts uncited or contradictory changes, discards broken attempts and clips levels into their range.",
              source="src/council/deliberation/audit.py",
              more="The allowed range (band) of each line comes from its trend; the auditor reverts a change that "
                   "cites evidence the pack does not hold."),
    AgentSpec("risk", "Risk engine", "CODE", "risk", "Checks",
              "Checks every limit in the risk policy and can hold a change back; it has the final word.",
              source="src/council/risk/engine.py",
              more="Gross and net exposure, caps, margin, volatility breakers, deadband, minimum holds, churn, "
                   "costs and the kill switch. Rules live in code, not in prompts."),
    AgentSpec("costs", "Cost desk and plan", "CODE", "risk", "Checks",
              "Prices every order (spread, fees, overnight carry) and turns the decision into order legs.",
              source="src/council/execution/planner.py",
              more="Costs are shown in basis points of the portfolio (1 bp = 0.01%), never as amounts."),
)
AGENT_BY_SLUG = {a.slug: a for a in AGENT_SPECS}
ROLE_AGENT = {r: a.slug for a in AGENT_SPECS for r in a.roles}
ROLE_WORDS = {"bull_open": "opening", "bull_rebuttal": "rebuttal", "single_agent": "control"}
# call role -> (the agent's name as the page says it, its accent, its run-page anchor)
CALL_AGENT = {
    "news": ("News analyst", "analyst", "a-news"), "macro": ("Macro analyst", "macro", "a-macro"),
    "bull_open": ("Bull · opening", "bull", "a-bull"), "bear": ("Bear", "bear", "a-bear"),
    "bull_rebuttal": ("Bull · rebuttal", "bull", "a-rebuttal"), "pm": ("Portfolio manager", "pm", "a-pm"),
    "single_agent": ("Control", "stone", "a-control"),
}

# Call status -> (glyph, word, css). The glyph is decoration; the word is always shown or read.
STATUS_GLYPHS = {
    "ok": ("✓", "ok", "ok"), "corrected": ("✓", "ok after a correction", "ok"),
    "cached": ("✓", "cached", "ok"), "partial": ("!", "partly failed", "fallback"),
    "parse_fail": ("✗", "parse fail", "failed"), "timeout": ("◷", "timeout", "timeout"),
    "transport": ("✗", "service error", "failed"), "skipped": ("–", "skipped", "idle"),
    "invalid": ("✗", "invalid", "failed"), "not_run": ("–", "not run", "idle"),
    "fallback": ("!", "fallback", "fallback"), "none": ("–", "nothing to do", "idle"),
}
# error_kind -> (short word, what it means). The private error text is never published.
ERROR_WORDS = {
    "timeout": ("timed out", "The model did not answer within the time limit, so the call was abandoned."),
    "not_json": ("unreadable reply", "The reply was not JSON, even after one correction turn."),
    "schema": ("wrong format", "The reply was JSON but did not match the required format, even after one "
                               "correction turn."),
    "correction_failed": ("correction failed", "The reply was unusable, and the correction call itself failed."),
    "corrected": ("corrected", "The first reply was unusable; one correction turn fixed it. What is shown is "
                               "the corrected reply."),
    "http_429": ("rate limited", "The model service refused the call: too many requests at once."),
    "http_4xx": ("request refused", "The model service rejected the request."),
    "http_5xx": ("service error", "The model service failed with an internal error."),
    "server_error": ("service error", "The model service reported an error instead of a reply."),
    "bad_response": ("bad response", "The model service answered with something that was not a model reply."),
    "connection": ("no connection", "The model service could not be reached."),
    "internal_error": ("internal error", "Our own code failed while handling the call."),
    "call_budget": ("skipped: call budget", "Not run: the run had already used its budget of model calls."),
    "council_unavailable": ("skipped: outage", "Not run: the model service was down, so the council stood down."),
    "skipped": ("skipped", "Not run."),
    "invalid": ("invalid record", "The call's record could not be read."),
}
# What a failed call did, as the end of a sentence that starts with the agent's name.
FAIL_PHRASES = {
    "not_json": "gave a reply that could not be read", "schema": "replied in the wrong format",
    "correction_failed": "gave an unusable reply, and its correction failed",
    "http_429": "was refused: too many requests", "http_4xx": "was refused by the model service",
    "http_5xx": "hit a model-service error", "server_error": "hit a model-service error",
    "bad_response": "got an answer that was not a model reply", "connection": "could not reach the model service",
    "internal_error": "failed in our own code", "call_budget": "was skipped: the call budget was used up",
    "council_unavailable": "was skipped: the model service was down", "skipped": "was skipped",
    "invalid": "left an unreadable record", "parse_fail": "gave a reply that could not be read",
    "transport": "hit a model-service error",
}
STATUS_ERROR = {   # older cycles carry no error_kind: explain the status itself
    "parse_fail": ("unreadable reply", "The reply could not be read as the required JSON, even after a "
                                       "correction turn."),
    "timeout": ERROR_WORDS["timeout"],
    "transport": ("service error", "The model service could not be reached or answered with an error."),
    "skipped": ERROR_WORDS["skipped"], "invalid": ERROR_WORDS["invalid"],
}
# What the system did instead when an agent gave no usable output (fixed text per role).
FALLBACK_WORDS = {
    "news": "No news cards this run: the advocates and the manager worked from the code facts and cards only.",
    "macro": "No macro context this run. Nothing depends on it: code never acts on the macro analyst.",
    "bull_open": "The bear still argued its own case, with no bull claims to answer; the manager saw the "
                 "bear's case and the bull's rebuttal, if any.",
    "bear": "The bull's rebuttal was skipped (there was nothing to answer); the manager saw the bull's opening only.",
    "bull_rebuttal": "The manager saw the bull's opening and the bear's answer, without the bull's reply.",
    "pm": "This attempt was discarded; the used attempt is chosen among the valid ones. With fewer than two "
          "valid attempts every line falls back to the mechanical reference.",
    "single_agent": "The control has fewer attempts this run. It never trades, so nothing else changes.",
}


def prompt_href(prompt_id: str, role: str, commit: str = "") -> str:
    """The prompt file on GitHub: "council-bull_open/v1" -> prompts/bull_open.md, at the sealed
    code commit when the commitment names one."""
    m = re.match(r"^council-([a-z_]+)/v\d+$", prompt_id or "")
    stem = m.group(1) if m else role
    if not re.fullmatch(r"[a-z_]{1,32}", stem or ""):
        return ""
    ref = commit if re.fullmatch(r"[0-9a-f]{7,40}", commit or "") else "main"
    return f"{REPO_URL}/blob/{ref}/prompts/{stem}.md"


def call_view(call: PublicCall, commit: str = "") -> dict[str, Any]:
    """One model call, for the per-agent headers and the call log."""
    key = "corrected" if call.error_kind == "corrected" else call.status
    glyph, word, css = STATUS_GLYPHS.get(key, ("!", key.replace("_", " "), "fallback"))
    if call.error_kind:
        short, explain = ERROR_WORDS.get(call.error_kind, (call.error_kind.replace("_", " "), ""))
    elif call.status in STATUS_ERROR:
        short, explain = STATUS_ERROR[call.status]
    else:
        short, explain = "", ""
    failed = call.status not in ("ok", "cached")
    name, accent, anchor = CALL_AGENT.get(call.role, (call.role.replace("_", " ").capitalize(), "code", ""))
    if call.role in ("pm", "single_agent"):
        anchor = f"{anchor}-{call.replicate + 1}"
    if call.status == "timeout" or call.error_kind == "timeout":
        fail = f"timed out after {fmt_secs(call.latency_ms)}"
    else:
        fail = FAIL_PHRASES.get(call.error_kind or call.status, f"failed ({(call.error_kind or call.status).replace('_', ' ')})")
    return {
        "role": call.role, "role_words": ROLE_WORDS.get(call.role, call.role.replace("_", " ")),
        "agent_name": name, "accent": accent, "anchor": anchor, "fail_phrase": fail if failed else "",
        "agent": ROLE_AGENT.get(call.role, ""), "replicate": call.replicate + 1,
        "status": key, "glyph": glyph, "word": word, "css": css, "failed": failed,
        "error_kind": call.error_kind or "", "error_word": short, "explain": explain,
        "latency": fmt_secs(call.latency_ms), "latency_ms": call.latency_ms,
        "tokens_in": fmt_int(call.tokens_in), "tokens_out": fmt_int(call.tokens_out),
        "prompt_id": call.prompt_id, "prompt_sha": short_sha(call.prompt_sha),
        "prompt_href": prompt_href(call.prompt_id, call.role, commit),
    }


def status_of(calls: list[dict[str, Any]], *, ran: bool = True) -> dict[str, str]:
    """An agent's status from its calls: all ok -> ok (or "ok after a correction"), all failed ->
    the failure, a mix -> partly failed; no call -> not run."""
    if not calls:
        key = "not_run" if not ran else "none"
    else:
        bad = [c for c in calls if c["failed"]]
        if not bad:
            key = "corrected" if any(c["status"] == "corrected" for c in calls) else "ok"
        elif len(bad) < len(calls):
            key = "partial"
        else:
            kinds = {c["status"] for c in bad}
            key = kinds.pop() if len(kinds) == 1 else "parse_fail"
    glyph, word, css = STATUS_GLYPHS.get(key, ("!", key, "fallback"))
    return {"key": key, "glyph": glyph, "word": word, "css": css}


def code_status(state: str, word: str = "") -> dict[str, str]:
    glyph, default, css = STATUS_GLYPHS[state]
    return {"key": state, "glyph": glyph, "word": word or default, "css": css}


# ------------------------------------------------------------------------------ run view
def plain_hold(text: str) -> str:
    for pattern, words in HOLD_WORDS:
        if pattern.match(text):
            return pattern.sub(words, text)
    return re.sub(r"\bR\d+[a-z]?\b\s*", "", text).strip() or text


def split_holds(reasons: list[str]) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """Risk-engine hold reasons -> ({line: [{text, raw}]}, [general notes])."""
    per_line: dict[str, list[dict[str, str]]] = {}
    general: list[dict[str, str]] = []
    for r in reasons:
        m = HOLD_LINE.match(r)
        if m:
            per_line.setdefault(m.group(1), []).append({"text": plain_hold(m.group(2)), "raw": r})
        else:
            general.append({"text": plain_hold(r), "raw": r})
    return per_line, general


def _subject(sym: str, lines: Lines) -> str:
    if sym in lines.info:
        return lines.name(sym)
    if sym.startswith("UNMAPPED"):
        return "a position outside the lines"
    return {"decisive_fact": "decisive fact", "replicate": "attempt"}.get(sym, sym)


def plain_violation(code: str, lines: Lines) -> dict[str, str]:
    """An auditor code in words: "GOLD: unknown_evidence F:X" -> "Gold: cited evidence that is not in the fact pack"."""
    if code.startswith("replicate:"):
        n = re.search(r"(\d+) of (\d+)", code)
        return {"text": f"{n.group(1)} of {n.group(2)} changes reverted" if n else "most changes reverted", "raw": code}
    m = re.match(r"^([A-Za-z0-9_]+): ([a-z_]+)\b(.*)$", code)
    if m:
        sym, what, _rest = m.groups()
        return {"text": f"{_subject(sym, lines)}: {AUDIT_WORDS.get(what, what.replace('_', ' '))}", "raw": code}
    return {"text": AUDIT_WORDS.get(code, code.replace("_", " ")), "raw": code}


def plain_skip(code: str, lines: Lines) -> dict[str, str]:
    """A skipped-leg code in words: "GOLD: below_broker_minimum (...)" -> "Gold: below the broker's minimum order size"."""
    m = re.match(r"^([A-Za-z0-9_.]+): ([a-z_ ]+?)\s*(?:\(.*\))?$", code)
    if not m:
        return {"text": code.replace("_", " "), "raw": code}
    sym, what = m.groups()
    if sym.startswith("UNMAPPED"):
        return {"text": "a position that belongs to no line", "raw": code}
    return {"text": f"{_subject(sym, lines)}: {SKIP_WORDS.get(what, what.replace('_', ' '))}", "raw": code}


def plain_why(code: str, lines: Lines) -> str:
    """Why the council met: "vol_shock:SEMIS" -> "a volatility shock on Semiconductors"."""
    kind, _, sym = code.partition(":")
    words = WHY_WORDS.get(kind, kind.replace("_", " "))
    return f"{words} on {lines.name(sym)}" if sym else words


def calendar_words(flags: list[str]) -> str:
    out = []
    for f in flags:
        if f == "calendar:release_dates_skipped_no_fred_key":
            out.append(CALENDAR_UNLOADED)
        elif f.startswith("calendar:release_dates_failed:"):
            out.append(f"{f.rsplit(':', 1)[1].upper()} release dates could not be loaded")
        else:
            out.append("the economic calendar is incomplete")
    return "; ".join(dict.fromkeys(out))


def verb_for(before: float, after: float) -> str:
    if after < before - EPS:
        return "short" if after < -EPS else "cover" if before < -EPS else "cut"
    if after > 1.0 + EPS and after > before:
        return "lever up"
    return "cover" if before < -EPS else "add to"


def change_list(levels: dict[str, float], ref_levels: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Lines whose level differs from the reference: {line: (reference, level)}."""
    return {k: (ref_levels[k], v) for k, v in levels.items()
            if k in ref_levels and abs(v - ref_levels[k]) > EPS}


Weights = dict[str, tuple[float, float]]


def unit_weights(c: PublicCycleV1) -> dict[str, float]:
    """The weight (x the portfolio) of one full size (1.00) per line, from the run's own numbers."""
    out: dict[str, float] = {}
    for k, r in c.reference.items():
        if abs(r.level_ref) > EPS:
            out[k] = r.weight_ref_x / r.level_ref
    if c.risk is not None:
        for levels, xs in ((c.risk.raw_levels, c.risk.raw_x), (c.risk.banded_levels, c.risk.banded_x)):
            for k, v in levels.items():
                if k not in out and abs(v) > EPS and k in xs:
                    out[k] = xs[k] / v
    return out


def phrase_changes(changes: dict[str, tuple[float, float]], lines: Lines, *, compact: bool = False,
                   weights: Weights | None = None) -> str:
    """Changes in words. With weights, portfolio percentages lead ("cut Gold from 9% to 3% of the
    portfolio"); without them (or for a line whose weight is unknown), sizes are used."""
    weights = weights or {}
    keys = lines.sort(changes)
    all_weighted = bool(keys) and all(k in weights for k in keys)
    out = []
    for k in keys:
        before, after = changes[k]
        name = lines.name(k)
        if k in weights:
            wb, wa = (fmt_share(v) for v in weights[k])
            out.append(f"{name} {wb} → {wa}" if compact else
                       f"{verb_for(before, after)} {name} from {wb} to {wa}" + ("" if all_weighted else " of the portfolio"))
        else:
            out.append(f"{name} size {fmt_level(before)} → {fmt_level(after)}" if compact else
                       f"{verb_for(before, after)} {name} from size {fmt_level(before)} to {fmt_level(after)}")
    if compact:
        return ", ".join(out)
    return join_words(out) + (" of the portfolio" if all_weighted else "")


def stance(changes: dict[str, tuple[float, float]], lines: Lines, weights: Weights) -> str:
    """A card heading: "Cut Gold 9% → 3%" or "Keep the reference"."""
    if not changes:
        return "Keep the reference"
    parts = []
    for k in lines.sort(changes):
        verb = verb_for(*changes[k]).replace("add to", "add")
        amount = (" → ".join(fmt_share(v) for v in weights[k]) if k in weights
                  else f"size {fmt_level(changes[k][0])} → {fmt_level(changes[k][1])}")
        parts.append(f"{verb} {lines.name(k)} {amount}")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:]


def count_words(pct: float | None, valid: int) -> str:
    """Agreement in words: "3 of 3" when the share is a whole number of valid attempts, else a percent."""
    if pct is None:
        return "—"
    if valid > 0:
        n = pct * valid / 100.0
        if abs(n - round(n)) < 0.02:
            return f"{round(n)} of {valid}"
    return f"{pct:.0f}%"


def _agreement(c: PublicCycleV1, focus: list[str]) -> tuple[float | None, str]:
    """Lowest agreement over the focus lines (else over all lines) and its words."""
    shares = c.pm.agreement_pct
    keys = [k for k in focus if k in shares] or list(shares)
    if not keys:
        return None, "—"
    low = min(shares[k] for k in keys)
    return low, count_words(low, c.pm.valid_replicates)


def _check_number(checks: list[Any], name: str) -> float | None:
    for ch in checks:
        if ch.name == name and isinstance(ch.value, (int, float)) and not isinstance(ch.value, bool):
            return float(ch.value)
    return None


def shorthand(texts: list[str], lines: Lines) -> list[dict[str, str]]:
    """The shorthand the agents used in this debate, each with a plain gloss (only terms that occur)."""
    blob = " ".join(texts)
    entries: list[tuple[str, str | None, str]] = [
        # regex, the term shown (None: the matches themselves), its meaning
        (r"\bdd52\b", "dd52", MARKET_FIELDS["dd52"]),
        (r"\bmom10d\b", "mom10d", "10-day price change"),
        (r"\bmom63d\b", "mom63d", "3-month price change"),
        (r"\bSMA ?\d+\b", None, "the average price over the last 50 or 200 days"),
        (r"\bsigma_ann\b", "sigma_ann", "yearly volatility: how much the price typically swings in a year"),
        (r"\bvol(?:atility)?(?: ratio)? \d+(?:\.\d+)?x\b|\b\d+(?:\.\d+)?x (?:vol|median)\b", "first",
         "volatility as a multiple of its usual level; 1x is normal"),
        (r"\bEWMA\d*\b", "EWMA", "an average that counts recent days more; used for volatility"),
        (r"\d ?bps?\b|\bbps?\b", "bp, bps", "basis points: hundredths of a percent (100 bps = 1%)"),
    ]
    glosses = {"T10Y2Y": "the gap between the 10-year and 2-year Treasury yields",
               "VIXCLS": "the VIX: how much US stocks are expected to swing"}
    for series, name in FRED_SERIES.items():
        pattern = r"\bVIX(?:CLS)?\b" if series == "VIXCLS" else rf"\b{series}\b"
        entries.append((pattern, "VIX" if series == "VIXCLS" else series, glosses.get(series, name)))
    out = []
    for pattern, term, meaning in entries:
        found = [m.group(0).strip() for m in re.finditer(pattern, blob)]
        if found:
            shown = found[0] if term == "first" else term or ", ".join(list(dict.fromkeys(found))[:3])
            out.append({"term": shown, "meaning": meaning})
    tickers = [f"{sym} {info.name}" for sym, info in lines.info.items()
               if info.name.lower() != sym.lower() and re.search(rf"\b{re.escape(sym)}\b", blob)]
    if tickers:
        out.append({"term": "Tickers", "meaning": " · ".join(tickers)})
    return out


FLOW = (
    # key, name, kind, accent, what it does (shown under the diagram and before the first run)
    ("data", "Data", "CODE", "code", "reads prices for every line"),
    ("officers", "Officers", "CODE", "code", "flag volatility shocks and scheduled events"),
    ("analysts", "Analysts", "LLM", "analyst", "turn news into cited evidence cards"),
    ("bull", "Bull", "LLM", "bull", "makes the case for a set of positions (it may still cut a line)"),
    ("bear", "Bear", "LLM", "bear", "attacks the bull's case, claim by claim"),
    ("pm", "Portfolio manager ×3", "LLM", "pm", "decides, within limits: three separate attempts, the most typical is used"),
    ("risk", "Risk engine", "CODE", "risk", "code that checks every limit and can hold a change back"),
    ("decision", "Plan & costs", "CODE", "risk", "prices each order and builds the proposal, or finds nothing to do"),
    ("human", "Human approval", "HUMAN", "human", "approves every order"),
)
MARKS = {"ok": "✓", "fallback": "!", "failed": "✗", "idle": "–", "waiting": "…"}


# Each step of the diagram links to its agent's section of the run page.
NODE_ANCHOR = {"data": "a-data", "officers": "officers", "analysts": "a-news", "bull": "a-bull", "bear": "a-bear",
               "pm": "a-pm", "risk": "a-risk", "decision": "a-costs", "human": "a-decision"}


def _node(key: str, state: str, word: str, result: str, detail: str = "", title: str = "") -> dict[str, str]:
    spec = next(f for f in FLOW if f[0] == key)
    return {"key": key, "name": spec[1], "kind": spec[2], "accent": spec[3], "state": state,
            "mark": MARKS[state], "word": word, "result": result, "detail": detail,
            "title": title or f"{spec[1]}: {spec[4]}", "what": spec[4], "anchor": NODE_ANCHOR[key]}


def empty_flow(n_lines: int) -> list[dict[str, str]]:
    nodes = []
    for key, _name, _kind, _accent, what in FLOW:
        text = f"reads prices for {n_lines} lines" if key == "data" else what
        nodes.append(_node(key, "waiting", "not run yet", text))
    return nodes


HUMAN_WORDS = {
    "completed": "approved · executed", "completed_partial": "approved · partly executed", "approved": "approved",
    "executing": "approved · executing", "rejected": "rejected", "expired": "expired, no answer in time",
    "proposed": "awaiting approval", "awaiting_publication": "awaiting approval", "superseded": "superseded",
    "blocked": "blocked, under review", "execution_unknown": "under review",
}


def outcome_chain(cv: CycleView, lines: Lines, bull: PublicAdvocate | None, bear: PublicAdvocate | None,
                  reply: PublicAdvocate | None, council_changes: dict[str, tuple[float, float]], council_w: Weights,
                  weights_for: Any, ref_levels: dict[str, float], passed: int, n_checks: int,
                  held_lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One run in one line, step by step: what each advocate asked, what the manager decided (and
    whose side it named), what the risk engine and the human then did. Each step links to its
    section of the run page."""
    c = cv.doc

    def asked(a: PublicAdvocate | None) -> str:
        if a is None:
            return "no valid answer"
        ch = change_list(a.proposal_levels, ref_levels)
        text = stance(ch, lines, weights_for(ch))
        return text[:1].lower() + text[1:]

    final_bull = reply if reply is not None else bull
    steps = [
        {"who": "Bull asked", "accent": "bull", "text": asked(final_bull),
         "anchor": "a-rebuttal" if reply is not None else "a-bull"},
        {"who": "Bear asked", "accent": "bear", "text": asked(bear), "anchor": "a-bear"},
    ]
    used = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
    total = len(c.pm.replicates)
    if not c.pm.replicates or c.pm.valid_replicates == 0 or used is None:
        pm_text = "no usable answer: the reference applied"
    else:
        decided = stance(council_changes, lines, council_w)
        same = sum(1 for r in c.pm.replicates if r.valid and r.sided_with == used.sided_with)
        side = SIDED_WORDS.get(used.sided_with or "", "")
        pm_text = decided[:1].lower() + decided[1:] + (f" (sided with {side}, {same} of {total} attempts)" if side else "")
    steps.append({"who": "Manager", "accent": "pm", "text": pm_text, "anchor": "a-pm"})
    if c.risk is None:
        risk_text = "did not run"
    else:
        risk_text = f"{passed}/{n_checks} checks passed"
        if held_lines:
            risk_text += ", held back " + join_words([r["name"] for r in held_lines])
    steps.append({"who": "Risk engine", "accent": "risk", "text": risk_text, "anchor": "a-risk"})
    legs = len(c.plan.legs) if c.plan else 0
    if cv.rehearsal:
        human = "not needed: rehearsal"
    elif cv.final_state in HUMAN_WORDS and (legs or cv.final_state not in ("proposed", "awaiting_publication")):
        human = HUMAN_WORDS[cv.final_state]
    else:
        human = "nothing to approve"
    steps.append({"who": "Human", "accent": "human", "text": human, "anchor": "a-decision"})
    return steps


def build_run_view(cv: CycleView, lines: Lines) -> dict[str, Any]:
    """Everything the diagram, the summary and the run page need, computed from the JSON."""
    c = cv.doc
    risk = c.risk
    ref_levels = {k: r.level_ref for k, r in c.reference.items()}
    ref_x = {k: r.weight_ref_x for k, r in c.reference.items()}
    units = unit_weights(c)
    council_levels = dict(c.pm.levels) or (dict(risk.raw_levels) if risk else {})
    council_changes = change_list(council_levels, ref_levels)
    holds, general_holds = split_holds(risk.hold_reasons if risk else [])
    final_x = dict(risk.final_x) if risk else {}
    proposed_x = dict(risk.proposed_x) if risk else {}
    raw_x = dict(risk.raw_x) if risk else {}
    medoid = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
    medoid_devs = {d.line: d for d in medoid.deviations} if medoid else {}
    all_lines = lines.sort(set(c.reference) | set(council_levels) | set(final_x))
    n_lines = len(all_lines)

    def weights_for(changes: dict[str, tuple[float, float]], own: dict[str, float] | None = None) -> Weights:
        """Portfolio weights for level changes: the reference weight, then the proposer's own
        weight when published (the council's raw_x), else one full size times the level."""
        out: Weights = {}
        for k, (before, after) in changes.items():
            b = ref_x.get(k, units[k] * before if k in units else None)
            a = (own or {}).get(k, units[k] * after if k in units else None)
            if b is not None and a is not None:
                out[k] = (b, a)
        return out

    council_w = weights_for(council_changes, raw_x)

    # ---- per-line changes (reference -> council -> after risk)
    rows = []
    for k in all_lines:
        rl, cl = ref_levels.get(k), council_levels.get(k)
        by_council = rl is not None and cl is not None and abs(cl - rl) > EPS
        held = holds.get(k, [])
        by_risk = (k in proposed_x and k in final_x and abs(proposed_x[k] - final_x[k]) > EPS) or bool(held) or (
            risk is not None and k in risk.raw_levels and k in risk.banded_levels
            and abs(risk.raw_levels[k] - risk.banded_levels[k]) > EPS)
        why = []
        if by_council and k in medoid_devs:
            why.append({"who": "Manager", "text": medoid_devs[k].reason, "raw": "", "evidence": list(medoid_devs[k].evidence)})
        for h in held:
            why.append({"who": "Risk engine", "text": h["text"], "raw": h["raw"], "evidence": []})
        rows.append({
            "line": k, "name": lines.name(k), "ref_level": rl, "ref_x": ref_x.get(k), "council_level": cl,
            "council_x": raw_x.get(k), "final_x": final_x.get(k),
            "risk_level": risk.banded_levels.get(k) if risk else None,
            "base_x": risk.base_x.get(k) if risk else None, "differs": by_council or by_risk,
            "verb": verb_for(rl, cl) if by_council and rl is not None and cl is not None else "",
            "why": why, "held": [h["text"] for h in held], "held_raw": [h["raw"] for h in held],
        })
    changed_rows = [r for r in rows if r["differs"]]
    # orders that no agent asked for: a line bought up to (or back to) its reference weight
    changed_lines = {r["line"] for r in changed_rows}
    ref_trades = []
    for leg in (c.plan.legs if c.plan else []):
        if leg.line in changed_lines or any(t["line"] == leg.line for t in ref_trades):
            continue
        ref_trades.append({"line": leg.line, "name": lines.name(leg.line),
                           "words": f"{lines.name(leg.line)} {fmt_share(leg.weight_before_x)} → "
                                    f"{fmt_share(leg.weight_after_x)}"})
    traded = {t["line"] for t in ref_trades}
    steady = [r for r in rows if not r["differs"] and r["line"] not in traded]

    # ---- the nine nodes
    trends = [r.trend for r in c.reference.values() if r.trend]
    trend_words = " · ".join(f"{trends.count(t)} {t}" for t in ("up", "mixed", "down") if trends.count(t))
    stale = [k for k, hs in holds.items() if any(h["text"] == "data too old" for h in hs)]
    closed = [k for k, hs in holds.items() if any(h["text"] == "market closed" for h in hs)]
    data_detail = "trend: " + trend_words + (f" · {len(closed)} closed" if closed else "") if trend_words else ""
    if not c.reference:
        data_node = _node("data", "failed", "no data", "no reference book this run")
    elif stale:
        data_node = _node("data", "fallback", "stale data", f"{n_lines} lines · {len(stale)} too old", data_detail)
    else:
        data_node = _node("data", "ok", "ok", f"{plural(n_lines, 'line')} read", data_detail)

    vol_cards = [k for k in c.cards if k.card_type == "vol_shock"]
    event_cards = [k for k in c.cards if k.card_type == "event_binary"]
    event_lines = [k for k, b in c.bands.items() if any("event window" in r for r in b.reasons)]
    checks = risk.checks if risk else []
    breaker = [ch for ch in checks if ch.name == "vol_breaker" and not ch.passed]
    shocked = lines.sort({s for k in vol_cards for s in k.scope if s in lines.info})
    vol_text = ("volatility shock: " + join_words([lines.name(s) for s in shocked])) if shocked else (
        plural(len(vol_cards), "volatility card") if vol_cards else "no volatility shock")
    if breaker:
        vol_text += " · breaker tripped"
    event_text = (f"event window on {plural(len(event_lines), 'line')}" if event_lines
                  else plural(len(event_cards), "event card") if event_cards else "no event block")
    calendar_flags = [f for f in c.flags if f.startswith("calendar:")]
    if calendar_flags:
        officers_node = _node("officers", "fallback", "calendar incomplete", vol_text,
                              f"{event_text} · release dates not loaded (release blocks unchecked)",
                              title="Officers: " + calendar_words(calendar_flags))
    else:
        officers_node = _node("officers", "ok", "ok", vol_text, event_text)

    analyst_calls = [x for x in c.calls if x.role in ("news", "macro", "filings", "sector")]
    news_cards = [k for k in c.cards if k.role == "news"]
    macro_cards = [k for k in c.cards if k.role == "macro"]
    macro_ran = any(x.role == "macro" for x in analyst_calls)
    macro_text = (f"macro: {macro_cards[0].direction.replace('_', ' ')}" if macro_cards
                  else "macro: no card" if macro_ran else "macro not run")
    news_text = plural(len(news_cards), "news card") if news_cards else "no news cards"
    failed_calls = [x for x in analyst_calls if x.status not in ("ok", "cached")]
    if not analyst_calls and not news_cards and not macro_cards:
        analysts_node = _node("analysts", "failed", "skipped", "not run this time", macro_text)
    elif failed_calls:
        analysts_node = _node("analysts", "fallback", f"{plural(len(failed_calls), 'call')} failed", news_text, macro_text)
    else:
        analysts_node = _node("analysts", "ok", "ok", news_text, macro_text)

    bull, bear, reply = c.debate.bull, c.debate.bear, c.debate.rebuttal
    bull_ch = change_list(bull.proposal_levels, ref_levels) if bull else None
    bear_ch = change_list(bear.proposal_levels, ref_levels) if bear else None
    if bull is None:
        bull_node = _node("bull", "failed", "no valid output", "no argument this run")
    else:
        want = phrase_changes(bull_ch or {}, lines, compact=True, weights=weights_for(bull_ch or {})) \
            if bull_ch else "holds the reference"
        bull_node = _node("bull", "ok", "ok", f"{want} · {plural(len(bull.claims), 'claim')}",
                          f"answered the bear, conceding {len(reply.concessions)}" if reply is not None else "")
    concede = [r.claim_id for r in bear.rebuttals if r.verdict == "concede"] if bear else []
    refute = [r.claim_id for r in bear.rebuttals if r.verdict == "refute"] if bear else []
    if bear is None:
        bear_node = _node("bear", "failed", "no valid output", "no argument this run")
    else:
        want = (join_words([f"{verb_for(*bear_ch[k])} {lines.name(k)}" for k in lines.sort(bear_ch)])
                if bear_ch else "holds the reference")
        bear_node = _node("bear", "ok", "ok", want, f"accepts {len(concede)} of the bull's points, disputes {len(refute)}")

    valid, total = c.pm.valid_replicates, len(c.pm.replicates)
    low, low_words = _agreement(c, list(council_changes))
    if low is not None and " of " in low_words:
        agree = f"{low_words.replace(' of ', '/')} agree"
    elif low is not None:
        agree = f"{low_words} agree"
    else:
        agree = f"{valid} of {total} valid"
    pm_failed = total == 0 or valid == 0 or c.basis in ("fallback_parse", "fallback_disagreement", "council_unavailable")
    if total == 0:
        pm_node = _node("pm", "failed", "not run", "no manager output")
    elif pm_failed:
        pm_node = _node("pm", "fallback", "fallback", "reference used instead", BASIS_WORDS.get(c.basis or "", ""))
    else:
        pm_result = (f"{agree} · {phrase_changes(council_changes, lines, compact=True, weights=council_w)}"
                     if council_changes else f"{agree}: hold the reference")
        pm_node = _node("pm", "ok" if valid == total else "fallback",
                        "ok" if valid == total else f"{plural(total - valid, 'attempt')} invalid", pm_result)

    passed = sum(1 for ch in checks if ch.passed)
    held_lines = [r for r in rows if r["held"] or (r["line"] in proposed_x and r["line"] in final_x
                                                    and abs(proposed_x[r["line"]] - final_x[r["line"]]) > EPS)]
    held_text = ("held: " + join_words([f"{r['name']}" + (f" ({r['held'][0]})" if r["held"] else "")
                                        for r in held_lines])) if held_lines else "nothing held back"
    if risk is None:
        risk_node = _node("risk", "failed", "did not run", "no risk decision")
    else:
        risk_node = _node("risk", "ok" if passed == len(checks) else "fallback",
                          "ok" if passed == len(checks) else "limits applied",
                          f"{passed} of {len(checks)} checks pass", held_text)

    # Orders: the plan's legs; a rehearsal has no plan, so it reads the risk engine's leg count.
    legs = len(c.plan.legs) if c.plan else 0
    cost_bp = c.plan.cost_bp_total if c.plan and c.plan.legs else None
    if cv.rehearsal and not legs:
        legs = int(_check_number(checks, "legs") or 0)
        cost_bp = _check_number(checks, "cycle_cost_bps")
    from_empty = risk is not None and bool(risk.base_x) and all(abs(v) < EPS for v in risk.base_x.values())
    cost_words = f", about {cost_bp / 100:.2f}% of the portfolio in costs" if cost_bp else ""
    cost_short = f" (≈{cost_bp / 100:.2f}% in costs)" if cost_bp else ""
    state = cv.final_state
    if cv.rehearsal:
        need = (f"a live run would need {plural(legs, 'order')}" + (" from an empty book" if from_empty else "")
                + cost_short) if legs else "a live run would not need any order"
        decision_node = _node("decision", "ok", "ok", "no trade: rehearsal", need)
    elif legs:
        decision_node = _node("decision", "ok", "ok", f"proposal · {plural(legs, 'order')}",
                              f"cost {cost_bp / 100:.2f}% of the portfolio" if cost_bp else "")
    elif c.basis == "halted":
        decision_node = _node("decision", "fallback", "halted", "no new risk: halted")
    else:
        decision_node = _node("decision", "ok", "ok", "no change needed", "")

    outcome = cv.human_outcome
    if cv.rehearsal:
        human_node = _node("human", "idle", "not needed", "not needed: rehearsal")
    elif state in ("completed", "completed_partial"):
        human_node = _node("human", "ok", "approved", "approved · executed" if state == "completed"
                           else "approved · partly executed")
    elif state in ("approved", "executing"):
        human_node = _node("human", "ok", "approved", "approved")
    elif state == "rejected" or outcome == "rejected":
        human_node = _node("human", "ok", "decided", "rejected" + (f": {cv.decision_reason}" if cv.decision_reason else ""))
    elif state == "expired" or outcome == "expired":
        human_node = _node("human", "fallback", "expired", "expired: no answer in time")
    elif state in ("proposed", "awaiting_publication") or outcome == "pending":
        human_node = _node("human", "waiting", "waiting", "awaiting approval")
    elif state in ("blocked", "execution_unknown"):
        human_node = _node("human", "fallback", "review", DECISION_CHIP[state][0].lower())
    else:
        human_node = _node("human", "idle", "not needed", "not needed")

    nodes = [data_node, officers_node, analysts_node, bull_node, bear_node, pm_node, risk_node,
             decision_node, human_node]

    # ---- the single-agent control, in words
    control = None
    ctrl = c.single_agent
    if ctrl is not None:
        if ctrl.levels:
            own = change_list(dict(ctrl.levels), ref_levels)
            action = "held the reference" if not own else phrase_changes(own, lines, weights=weights_for(own))
            differs = [k for k in lines.sort(set(ctrl.levels) & set(council_levels))
                       if abs(ctrl.levels[k] - council_levels[k]) > EPS]
            rel = ("same as the council" if not differs else
                   "differs from the council on " + join_words([lines.name(k) for k in differs]))
            control = {"text": f"{action} — {rel}.", "differs": bool(differs)}
        else:
            control = {"text": "no valid answer this run.", "differs": False}

    # ---- the plain-language summary (fixed templates, no model text)
    when = fmt_when(c.slot)
    s1 = f"The council reviewed its {plural(n_lines, 'line')} on {when}"
    s1 += " — a rehearsal, so nothing was traded." if cv.rehearsal else "."

    def wants(ch: dict[str, tuple[float, float]]) -> str:
        return phrase_changes(ch, lines, weights=weights_for(ch)) if ch else "keep the reference"

    if bull_ch is not None and bear_ch is not None and bull_ch == bear_ch:
        debate = f"The bull and the bear both argued to {wants(bull_ch)}"
    elif bull_ch is not None and bear_ch is not None:
        debate = f"The bull argued to {wants(bull_ch)} and the bear to {wants(bear_ch)}"
    elif bull_ch is not None or bear_ch is not None:
        debate = f"Only one advocate answered, arguing to {wants(bull_ch or bear_ch or {})}"
    else:
        debate = "There was no debate"
    if pm_failed:
        pm_s = "the portfolio manager gave no usable answer, so the mechanical reference was used"
    elif council_changes:
        valid_word = "" if valid == total else " valid"
        if low is not None and low >= 100 - EPS:
            attempts = (f"both{valid_word} portfolio-manager attempts" if valid == 2 else
                        f"all {SPELLED.get(valid, str(valid))}{valid_word} portfolio-manager attempts")
        elif " of " in low_words:
            attempts = f"{low_words}{valid_word} portfolio-manager attempts"
        else:
            attempts = f"{low_words} of the{valid_word} portfolio-manager attempts"
        if bull_ch == council_changes == bear_ch:
            pm_s = f"{attempts} agreed"
        else:
            pm_s = f"{attempts} chose to {phrase_changes(council_changes, lines, weights=council_w)}"
    else:
        pm_s = "the portfolio manager kept every line at the mechanical reference"
    s2 = f"{debate}, and {pm_s}." if not pm_s.startswith("the portfolio manager") else f"{debate}; {pm_s}."
    if risk is None:
        s3 = "The risk engine did not run."
    else:
        tally = f"all {len(checks)}" if passed == len(checks) else f"{passed} of {len(checks)}"
        s3 = f"The risk engine passed {tally} checks"
        if held_lines:
            s3 += " and held back " + join_words([r["name"] + (f" ({r['held'][0]})" if r["held"] else "")
                                                   for r in held_lines])
        else:
            s3 += " and changed nothing"
        s3 += "."
    if cv.rehearsal:
        s4 = (f"A live run{' starting from an empty book' if from_empty else ''} would have needed "
              f"{plural(legs, 'order')}{cost_words}.") if legs else "A live run would not have needed any order."
    elif state in ("completed", "completed_partial"):
        s4 = "The human approved it and it was executed." if state == "completed" else \
            "The human approved it; it was partly executed."
    elif state in ("approved", "executing"):
        s4 = "The human approved it."
    elif state == "rejected":
        s4 = "The human rejected it" + (f": {cv.decision_reason}" if cv.decision_reason else "") + "."
    elif state == "expired":
        s4 = "The proposal expired without an answer."
    elif legs:
        s4 = f"A proposal with {plural(legs, 'order')} went to the human for approval."
    else:
        s4 = "No trade was needed."
    summary = [s1, s2, s3 + (" " + s4 if s4 else "")]

    # ---- one sentence for the Portfolio page's hero
    if pm_failed:
        did = "the council gave no usable answer, so every line follows the rules"
    elif council_changes:
        rest = n_lines - len(council_changes)
        did = f"the council {phrase_changes(council_changes, lines, weights=council_w)}"
        if rest:
            did += "; the other line follows the rules" if rest == 1 else f"; the other {rest} lines follow the rules"
        held_back = [lines.name(k) for k in lines.sort(council_changes) if holds.get(k)]
        if held_back:
            did += f" (the risk engine held back {join_words(held_back)})"
    else:
        did = "the council kept every line at the mechanical reference"
    headline = f"Latest run ({fmt_short_when(c.slot)}): {did}."

    # ---- one line of words for tables: what changed
    if council_changes:
        parts = []
        for k in lines.sort(council_changes):
            verb = verb_for(*council_changes[k]).replace("add to", "add")
            amount = (" → ".join(fmt_share(v) for v in council_w[k]) if k in council_w
                      else f"size {fmt_level(council_changes[k][0])} → {fmt_level(council_changes[k][1])}")
            parts.append(f"{lines.name(k)} {verb} {amount}")
        change_words = "; ".join(parts)
    elif total == 0 or valid == 0:
        change_words = "No council answer: reference used"
    else:
        change_words = "No change: held the reference"
    if held_lines:
        change_words += " · held: " + ", ".join(r["name"] for r in held_lines)

    debate_texts = [t for a in (bull, bear, reply) if a is not None
                    for t in [a.argument, *(cl.text for cl in a.claims), *(r.text for r in a.rebuttals)]]

    # ---- the manager's attempts, in words
    sided = {"bull": "the bull", "bear": "the bear", "neither": "neither advocate", "reference": "the reference"}
    reps = []
    for r in c.pm.replicates:
        changes = []
        for d in r.deviations:
            words = f"{d.direction} {lines.name(d.line)}"
            rl = ref_levels.get(d.line)
            w = weights_for({d.line: (rl, d.level)}) if rl is not None else {}
            if d.line in w:
                words += (f" from {fmt_share(w[d.line][0])} to {fmt_share(w[d.line][1])} "
                          f"(size {fmt_level(rl)} → {fmt_level(d.level)})")
            else:
                words += f" to size {fmt_level(d.level)}"
            changes.append({"d": d, "words": words})
        reps.append({
            "r": r, "n": r.replicate + 1, "used": r.replicate == c.pm.medoid,
            "sided": sided.get(r.sided_with or "", "—"), "changes": changes,
            "violations": [plain_violation(v, lines) for v in r.violations],
            "reverted": [plain_violation(v, lines) for v in r.reverted],
        })
    valid_reps = [rp for rp in reps if rp["r"].valid]
    calls = {tuple(sorted((d.line, round(d.level, 4)) for d in rp["r"].deviations)) for rp in valid_reps}
    pm_same = len(valid_reps) >= 2 and len(calls) == 1
    pm_lead = ""
    if pm_same:
        first = valid_reps[0]["changes"]
        made = join_words([ch["words"] for ch in first]) if first else "keep every line at the reference"
        valid_word = "valid " if len(valid_reps) < len(reps) else ""
        who = f"Both {valid_word}attempts" if len(valid_reps) == 2 else f"All {len(valid_reps)} {valid_word}attempts"
        pm_lead = f"{who} made the same call: {made}."
    agreement = []
    for k in lines.sort(c.pm.agreement_pct):
        share = c.pm.agreement_pct[k]
        if k in council_changes or share < 100 - EPS:
            agreement.append({"name": lines.name(k), "words": count_words(share, valid), "pct": share,
                              "call": verb_for(*council_changes[k]) if k in council_changes else "hold"})
    agreement_rest = len(c.pm.agreement_pct) - len(agreement)

    status_words = STATUS_WORDS.get(c.status, c.status.replace("_", " "))
    if c.late_by_min:
        status_words += (f": slot {fmt_clock(c.slot)}, ran {fmt_clock(cv.ran_at)} "
                         f"({fmt_late(c.late_by_min)} late)")

    return {
        "reps": reps, "pm_same": pm_same, "pm_lead": pm_lead,
        "agreement": agreement, "agreement_rest": agreement_rest,
        "status_words": status_words, "why": [plain_why(w, lines) for w in c.why_we_met],
        "nodes": nodes, "summary": summary, "headline": headline, "control": control, "rows": rows,
        "changed_rows": changed_rows, "steady": steady, "general_holds": general_holds, "change_words": change_words,
        "ref_trades": ref_trades, "chain": outcome_chain(cv, lines, bull, bear, reply, council_changes, council_w,
                                                         weights_for, ref_levels, passed, len(checks), held_lines),
        "checks_passed": passed, "checks_total": len(checks),
        "failed_checks": [ch for ch in checks if not ch.passed], "agreement_words": low_words,
        "council_changes": council_changes, "when": when, "n_lines": n_lines,
        "gross": sum(abs(v) for v in final_x.values()), "net": sum(final_x.values()),
        "shorthand": shorthand(debate_texts, lines),
        "skipped": [plain_skip(s, lines) for s in (c.plan.skipped if c.plan else [])],
    }


# ------------------------------------------------------------------------------ the run transcript
TURNS = (
    # debate turn, run-page anchor, heading, the agent page's words
    ("bull_open", "a-bull", "Bull · opening", "the bull's opening"),
    ("bear", "a-bear", "Bear · answer", "the bear's answer"),
    ("bull_rebuttal", "a-rebuttal", "Bull · rebuttal", "the bull's rebuttal"),
)
TURN_ANCHOR = {t: a for t, a, _, _ in TURNS}
TURN_WORDS = {t: w for t, _, _, w in TURNS}
TURN_ROLE = {"bull_open": "bull", "bear": "bear", "bull_rebuttal": "bull"}
CLAIM_PREFIX = re.compile(
    r"^\s*(?:(bull_open|bull_rebuttal|bull|bear|rebuttal|opening)\s*[:/.\-]\s*)?(c\d+)\s*$", re.I)
PREFIX_TURN = {"bull_open": "bull_open", "bull": "bull_open", "opening": "bull_open", "bear": "bear",
               "bull_rebuttal": "bull_rebuttal", "rebuttal": "bull_rebuttal"}
SIDED_WORDS = {"bull": "the bull", "bear": "the bear", "neither": "neither advocate", "reference": "the reference"}
REGIME_WORDS = {"risk_on": ("risk on", "executed"), "neutral": ("neutral", "sealed"), "risk_off": ("risk off", "warn")}
TILT_WORDS = {-1: "lean less", 0: "neutral", 1: "lean more"}
CARD_TYPE_WORDS = {
    "news_material": "material news", "news_context": "news context", "filing_material": "material filing",
    "filing_context": "filing context", "macro_context": "macro context", "event_binary": "scheduled event",
    "vol_shock": "volatility shock", "sector_rank": "sector rank",
}


def claim_anchor(turn: str, claim_id: str) -> str:
    return anchor_slug("cl", f"{turn}-{claim_id}")


def debate_turns(c: PublicCycleV1) -> dict[str, PublicAdvocate | None]:
    return {"bull_open": c.debate.bull, "bear": c.debate.bear, "bull_rebuttal": c.debate.rebuttal}


def resolve_claim(claim_id: str, sided: str | None,
                  claims: dict[str, set[str]]) -> list[tuple[str, str, str]]:
    """Which advocate's claim a manager dismissal names: [(turn, claim id, how)]. `how` is "named"
    ("bear:c2"), "only" (one advocate has that id), "inferred" (an unnamed id read as the claim of
    the side the attempt did not take) or "ambiguous" (several advocates have it)."""
    m = CLAIM_PREFIX.match(claim_id or "")
    if not m:
        return []
    prefix, cid = m.group(1), m.group(2).lower()
    if prefix:
        turn = PREFIX_TURN[prefix.lower()]
        return [(turn, cid, "named")] if cid in claims.get(turn, set()) else []
    owners = [t for t, _, _, _ in TURNS if cid in claims.get(t, set())]
    if len(owners) == 1:
        return [(owners[0], cid, "only")]
    if sided == "bear" and "bull_open" in owners:
        return [("bull_open", cid, "inferred")]
    if sided == "bull" and "bear" in owners:
        return [("bear", cid, "inferred")]
    return [(t, cid, "ambiguous") for t in owners]


HOW_WORDS = {   # the footnotes (†) for a claim the manager named by its bare number
    "named": "",
    "only": "",
    "inferred": "The manager wrote only the claim number; it sided with the other advocate, so the number is "
                "read as that advocate's claim.",
    "ambiguous": "The manager wrote only the claim number, and more than one advocate has a claim with that "
                 "number, so it is not shown under either claim.",
}


def citations(c: PublicCycleV1) -> dict[str, list[tuple[str, str]]]:
    """Evidence id -> the agents that cited it this run: [(agent slug, short name)], in run order."""
    out: dict[str, list[tuple[str, str]]] = {}

    def add(refs: Any, slug: str, name: str) -> None:
        for r in refs:
            if r is None:
                continue
            rid = ref_id(r)
            seen = out.setdefault(rid, [])
            if (slug, name) not in seen:
                seen.append((slug, name))

    card_agent = {"vol": ("vol", "Volatility officer"), "event": ("event", "Event officer"),
                  "news": ("news", "News analyst"), "macro": ("macro", "Macro analyst")}
    for k in c.cards:
        slug, name = card_agent.get(k.role, (k.role, k.role.replace("_", " ").capitalize()))
        add(k.evidence, slug, name)
    if c.macro is not None:
        for d in c.macro.drivers:
            add(d.evidence, "macro", "Macro analyst")
    for turn, a in debate_turns(c).items():
        if a is None:
            continue
        slug, name = {"bull_open": ("bull", "Bull"), "bear": ("bear", "Bear"),
                      "bull_rebuttal": ("rebuttal", "Bull (rebuttal)")}[turn]
        for cl in a.claims:
            add(cl.evidence, slug, name)
        for rb in a.rebuttals:
            add(rb.evidence, slug, name)
        add([a.strongest_opposing], slug, name)
    for block, slug, name in ((c.pm, "pm", "Manager"), (c.single_agent, "control", "Control")):
        if block is None:
            continue
        for r in block.replicates:
            for d in r.deviations:
                add(d.evidence, slug, name)
            if r.decisive_fact is not None:
                add([r.decisive_fact.evidence], slug, name)
    return out


def card_view(k: PublicCard, lines: Lines, fx: FactIndex, cited: dict[str, list[tuple[str, str]]],
              pre: str = "") -> dict[str, Any]:
    label = evidence_label(SimpleNamespace(kind="card", id=k.card_id), lines)["label"]
    return {
        "id": k.card_id, "anchor": pre + anchor_slug("k", k.card_id), "label": label,
        "type": CARD_TYPE_WORDS.get(k.card_type, k.card_type.replace("_", " ")), "direction": k.direction,
        "claim": k.claim, "falsifier": k.falsifier, "horizon": k.horizon_days, "qualifying": k.qualifying,
        "scope": [lines.name(s) if s in lines.info or re.fullmatch(r"[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?", s) else s for s in k.scope],
        "chips": [fx.chip(r) for r in k.evidence],
        "corroborated": [{"label": evidence_label(SimpleNamespace(kind="card", id=x), lines)["label"],
                          "href": fx.href(x)} for x in k.corroborated_by],
        "cited_by": [name for slug, name in cited.get(k.card_id, []) if slug not in (k.role,)],
    }


AGENT_SHORT = {"a-news": "news", "a-macro": "macro", "a-bull": "bull", "a-bear": "bear", "a-rebuttal": "reply",
               "a-pm": "PM", "a-control": "control"}


def _agent(slug: str, **extra: Any) -> dict[str, Any]:
    spec = AGENT_BY_SLUG[slug]
    out = {"slug": slug, "id": f"a-{slug}", "name": spec.name, "kind": spec.kind, "accent": spec.accent,
           "group": spec.group, "job": spec.job, "calls": [], "meta": "", "failure": None, "result": "",
           "compact": False,
           "page": f"agents/{slug}.html", "icon": AGENT_ICONS.get(slug, "cpu"), **extra}
    out["short"] = AGENT_SHORT.get(out["id"], out["name"])
    return out


def _calls_meta(calls: list[dict[str, Any]], raw: list[PublicCall]) -> str:
    """The footer of an agent's call log: "3 calls · median 1.7 s · 24,855 tokens in / 981 out"
    ("1 call · 3.0 s · ..." for one call); empty without calls."""
    if not raw:
        return ""
    tokens = f"{fmt_int(sum(x.tokens_in for x in raw))} tokens in / {fmt_int(sum(x.tokens_out for x in raw))} out"
    if len(raw) == 1:
        return f"1 call · {fmt_secs(raw[0].latency_ms)} · {tokens}"
    lat = [float(x.latency_ms) for x in raw]
    med = median(lat) or 0.0
    slow = any(c["failed"] for c in calls) or max(lat) > 10 * med
    total = f"total {fmt_secs(int(sum(lat)))} · " if slow else ""
    return f"{len(raw)} calls · {total}median {fmt_secs(int(med))} · {tokens}"


def _failure(role: str, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    bad = [x for x in calls if x["failed"]]
    if not bad:
        return None
    what = list(dict.fromkeys(x["explain"] for x in bad if x["explain"]))
    return {"what": " ".join(what), "instead": FALLBACK_WORDS.get(role, "")}


def _proposal(levels: dict[str, float], ref_levels: dict[str, float]) -> dict[str, float]:
    """A set of levels as the call it makes: {line: level} for the lines away from the reference."""
    return {k: round(after, 3) for k, (_before, after) in change_list(levels, ref_levels).items()}


def _replicate_call(r: PublicPMReplicate, ref_levels: dict[str, float]) -> dict[str, float]:
    return _proposal({d.line: d.level for d in r.deviations}, ref_levels)


def advocate_view(turn: str, a: PublicAdvocate | None, c: PublicCycleV1, lines: Lines, fx: FactIndex,
                  weights_for: Any, ref_levels: dict[str, float]) -> dict[str, Any] | None:
    """One debate turn: its argument in full, its claims (each with an anchor and what happened to
    it), its answers to the other side, its concessions, and whether the manager did what it asked."""
    if a is None:
        return None
    turns = debate_turns(c)
    claims_by_turn = {t: {cl.claim_id for cl in x.claims} for t, x in turns.items() if x is not None}
    ch = change_list(a.proposal_levels, ref_levels)
    fates: dict[str, list[dict[str, Any]]] = {cl.claim_id: [] for cl in a.claims}
    # the bear answers the bull's opening claims
    if turn == "bull_open" and turns["bear"] is not None:
        for rb in turns["bear"].rebuttals:
            if rb.claim_id in fates:
                fates[rb.claim_id].append({"who": "Bear", "href": f"{fx.base}#{anchor_slug('rb', 'bear-' + rb.claim_id)}",
                                           "verdict": rb.verdict, "text": rb.text, "dagger": False})
    # the bull's rebuttal concedes bear claims as "c<N>: why"
    if turn == "bear" and turns["bull_rebuttal"] is not None:
        for text in turns["bull_rebuttal"].concessions:
            m = re.match(r"^\s*(c\d+)\s*[:.\-–]\s*(.*)$", text)
            if m and m.group(1) in fates:
                fates[m.group(1)].append({"who": "Bull (rebuttal)", "href": f"{fx.base}#a-rebuttal", "verdict": "concede",
                                          "text": m.group(2), "dagger": False})
    # the manager's attempts set a claim aside (dismissed). A bare claim number that more than one
    # advocate used is ambiguous: it is shown once, on the attempt, never as a firm fate here.
    medoid = c.pm.medoid
    for r in c.pm.replicates:
        for d in r.dismissed:
            for t, cid, how in resolve_claim(d.claim_id, r.sided_with, claims_by_turn):
                if t == turn and cid in fates and how != "ambiguous":
                    used = " (used)" if r.replicate == medoid else ""
                    fates[cid].append({"who": f"Manager, attempt {r.replicate + 1}{used}",
                                       "href": f"{fx.base}#a-pm-{r.replicate + 1}", "verdict": "dismissed",
                                       "text": d.why, "dagger": how == "inferred"})
    used_rep = next((r for r in c.pm.replicates if r.replicate == medoid), None)
    used_ids: set[str] = set()
    if used_rep is not None:
        for d in used_rep.deviations:
            used_ids |= {ref_id(e) for e in d.evidence}
        if used_rep.decisive_fact is not None and used_rep.decisive_fact.evidence is not None:
            used_ids.add(ref_id(used_rep.decisive_fact.evidence))
    claims = []
    for cl in a.claims:
        shared = [e for e in cl.evidence if ref_id(e) in used_ids]
        dismissed_by_used = any(f["verdict"] == "dismissed" and "(used)" in f["who"] for f in fates[cl.claim_id])
        if shared and not dismissed_by_used:
            fates[cl.claim_id].append({
                "who": "Manager, the used attempt", "href": f"{fx.base}#a-pm-{(medoid or 0) + 1}", "verdict": "used",
                "text": "cited the same evidence: " + ", ".join(fx.chip(e)["label"] for e in shared[:3]),
                "dagger": False})
        claims.append({"id": cl.claim_id, "anchor": claim_anchor(turn, cl.claim_id), "text": cl.text,
                       "short": clip(cl.text, 90), "chips": [fx.chip(e) for e in cl.evidence],
                       "fates": fates[cl.claim_id]})
    rebuttals = []
    for rb in a.rebuttals:
        target = turns["bull_open"]
        target_claim = next((x for x in (target.claims if target else []) if x.claim_id == rb.claim_id), None)
        rebuttals.append({
            "claim_id": rb.claim_id, "verdict": rb.verdict, "text": rb.text, "chips": [fx.chip(e) for e in rb.evidence],
            "anchor": anchor_slug("rb", f"{turn}-{rb.claim_id}"),
            "target_href": f"{fx.base}#{claim_anchor('bull_open', rb.claim_id)}" if target_claim else "",
            "target_text": clip(target_claim.text, 110) if target_claim else "",
        })
    concessions = []
    for text in a.concessions:
        m = re.match(r"^\s*(c\d+)\s*[:.\-–]\s*(.*)$", text) if turn == "bull_rebuttal" else None
        bear_ids = claims_by_turn.get("bear", set())
        if m and m.group(1) in bear_ids:
            concessions.append({"text": m.group(2), "ref": m.group(1),
                                "href": f"{fx.base}#{claim_anchor('bear', m.group(1))}"})
        else:
            concessions.append({"text": text, "ref": "", "href": ""})
    # did the manager do what this turn asked? By outcome (the call the attempt made), and by the
    # side the attempt named. An empty proposal asks to keep the reference.
    side = TURN_ROLE[turn]
    asked = _proposal(a.proposal_levels, ref_levels)
    valid = [r for r in c.pm.replicates if r.valid]
    got = sum(1 for r in valid if _replicate_call(r, ref_levels) == asked)
    sided = sum(1 for r in valid if r.sided_with == side)
    used_ok = used_rep is not None and used_rep.valid
    used_call = _replicate_call(used_rep, ref_levels) if used_ok and used_rep is not None else {}
    used_ch = {k: (ref_levels[k], v) for k, v in used_call.items()}
    did = stance(used_ch, lines, weights_for(used_ch))
    text = stance(ch, lines, weights_for(ch))
    # published before the full-text record: arguments were cut at 600 characters
    legacy_cut = a.argument.rstrip().endswith("…") and (len(a.argument) == 600 or not c.facts)
    return {
        "turn": turn, "argument": a.argument, "stance": text, "changes": bool(ch), "legacy_cut": legacy_cut,
        "claims": claims, "rebuttals": rebuttals, "concessions": concessions,
        "strongest": fx.chip(a.strongest_opposing) if a.strongest_opposing else None,
        "asked": text[:1].lower() + text[1:], "did": did[:1].lower() + did[1:],
        "got": got, "sided": sided, "valid": len(valid), "used_ok": used_ok,
        "used_got": used_ok and used_call == asked,
        "used_sided": used_ok and used_rep is not None and used_rep.sided_with == side,
        "used_label": SIDED_WORDS.get(used_rep.sided_with or "", "") if used_ok and used_rep is not None else "",
        "who": "the bear" if side == "bear" else "the bull",
        "daggers": any(f["dagger"] for cl in claims for f in cl["fates"]),
        "concede": sum(1 for r in a.rebuttals if r.verdict == "concede"),
        "refute": sum(1 for r in a.rebuttals if r.verdict == "refute"),
    }


def _unique_refs(refs: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for r in refs:
        rid = ref_id(r)
        if rid not in seen:
            seen.add(rid)
            out.append(r)
    return out


def replicate_view(r: PublicPMReplicate, block: PublicPM, calls: dict[int, dict[str, Any]], c: PublicCycleV1,
                   lines: Lines, fx: FactIndex, weights_for: Any, ref_levels: dict[str, float],
                   anchor: str, *, control: bool = False, pre: str = "") -> dict[str, Any]:
    """One manager (or control) attempt, with its own call, its changes and what it set aside.
    The control sees no debate, so its claim ids are never resolved against the advocates' claims.
    `pre` prefixes the element id (the agents' pages repeat a run's attempts; ids stay unique)."""
    claims_by_turn = {t: {cl.claim_id for cl in x.claims} for t, x in debate_turns(c).items() if x is not None}
    changes = []
    for d in r.deviations:
        rl = ref_levels.get(d.line)
        w = weights_for({d.line: (rl, d.level)}) if rl is not None else {}
        if d.line in w:
            words = (f"{d.direction} {lines.name(d.line)} from {fmt_share(w[d.line][0])} to "
                     f"{fmt_share(w[d.line][1])}")
            size = f"size {fmt_level(rl)} → {fmt_level(d.level)}"
        else:
            words = f"{d.direction} {lines.name(d.line)} to size {fmt_level(d.level)}"
            size = ""
        changes.append({"words": words, "size": size, "reason": d.reason, "chips": [fx.chip(e) for e in d.evidence],
                        "direction": d.direction})
    dismissed = []
    for d in ([] if control else r.dismissed):
        targets = [{"href": f"{fx.base}#{claim_anchor(t, cid)}", "label": f"{TURN_WORDS[t]} {cid}", "how": how}
                   for t, cid, how in resolve_claim(d.claim_id, r.sided_with, claims_by_turn)]
        hows = {x["how"] for x in targets}
        dismissed.append({"claim_id": d.claim_id, "why": d.why, "targets": targets,
                          "ambiguous": "ambiguous" in hows, "dagger": "inferred" in hows})
    call = calls.get(r.replicate)
    failure = ""
    if not r.valid:
        if call is not None and call["failed"]:
            failure = f"No usable answer: it {call['fail_phrase']}"
        else:
            what = "; ".join(v["text"] for v in (plain_violation(x, lines) for x in r.violations)) or "broke a rule"
            failure = f"Discarded by the auditor: {what}"
    return {
        "n": r.replicate + 1, "anchor": f"{pre}{anchor}-{r.replicate + 1}", "used": r.replicate == block.medoid,
        "valid": r.valid, "call": call, "sided": SIDED_WORDS.get(r.sided_with or "", "—"), "control": control,
        "failure": failure, "same_as": 0, "sig": (tuple(sorted((d.line, round(d.level, 3)) for d in r.deviations)),
                                                   r.sided_with) if r.valid else None,
        "changes": changes, "dismissed": dismissed, "no_change": r.no_change_reason,
        "decisive": ({"text": r.decisive_fact.text,
                      "chip": fx.chip(r.decisive_fact.evidence) if r.decisive_fact.evidence else None}
                     if r.decisive_fact else None),
        "violations": [plain_violation(v, lines) for v in r.violations],
        "reverted": [plain_violation(v, lines) for v in r.reverted],
    }


def _mark_same(reps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attempts that made exactly the same call as the used one (same changes, same side named)
    are marked `same_as`: the page shows them folded, the used one in full."""
    lead = next((rp for rp in reps if rp["used"] and rp["valid"]), None) or next((rp for rp in reps if rp["valid"]), None)
    if lead is None:
        return reps
    for rp in reps:
        if rp is not lead and rp["valid"] and rp["sig"] == lead["sig"]:
            rp["same_as"] = lead["n"]
    return reps


def leg_reason(leg: Any, council_changes: dict[str, tuple[float, float]], ref_x: dict[str, float], lines: Lines,
               medoid: int | None) -> str:
    """Why an order exists: the council's change, a line bought up to its reference, or a
    rebalance back to the reference."""
    line = leg.line
    ref = ref_x.get(line)
    if line in council_changes:
        verb = verb_for(*council_changes[line]).replace("add to", "add")
        return f"Council: {verb} (manager attempt {(medoid or 0) + 1})"
    if ref is not None and abs(leg.weight_before_x) < EPS and abs(ref) > EPS and abs(leg.weight_after_x - ref) < 5e-4:
        return f"Reference: not yet held, bought up to its reference weight ({fmt_share(ref)})"
    if ref is not None and abs(leg.weight_after_x - ref) < 5e-4:
        return "Reference: back to its reference weight (drift beyond the deadband)"
    return "Risk engine: rebalanced to fit the book's limits"


IN_SENTENCE = {"news": "the news analyst", "macro": "the macro analyst", "bull_open": "the bull's opening",
               "bear": "the bear", "bull_rebuttal": "the bull's rebuttal", "pm": "manager attempt {n}",
               "single_agent": "control attempt {n}"}
FAIL_THEN = {
    "news": "no news cards", "macro": "no macro context (nothing depends on it)",
    "bull_open": "no bull opening; the bear argued its own case", "bear": "no bear answer; the rebuttal was skipped",
    "bull_rebuttal": "the manager saw no bull rebuttal", "single_agent": "the control has fewer attempts (it never trades)",
}


def build_transcript(cv: CycleView, lines: Lines, run: dict[str, Any], base: str = "") -> dict[str, Any]:
    """The run page as a transcript: every agent in execution order, each with its status, its
    calls, what it saw, what it said or did, and what happened to it; then the facts table."""
    c = cv.doc
    fx = FactIndex(c, lines, base)
    # the agents' pages repeat a run's cards and attempts: their element ids get the run's id
    pre = f"{c.cycle_id}-" if base else ""
    commit = cv.commitment.code_commit if cv.commitment else ""
    cited = citations(c)
    ref_levels = {k: r.level_ref for k, r in c.reference.items()}
    ref_x = {k: r.weight_ref_x for k, r in c.reference.items()}
    units = unit_weights(c)
    raw_x = dict(c.risk.raw_x) if c.risk else {}

    def weights_for(changes: dict[str, tuple[float, float]], own: dict[str, float] | None = None) -> Weights:
        out: Weights = {}
        for k, (before, after) in changes.items():
            b = ref_x.get(k, units[k] * before if k in units else None)
            a = (own or {}).get(k, units[k] * after if k in units else None)
            if b is not None and a is not None:
                out[k] = (b, a)
        return out

    by_role: dict[str, list[PublicCall]] = {}
    for call in c.calls:
        by_role.setdefault(call.role, []).append(call)
    views = {id(call): call_view(call, commit) for call in c.calls}

    def calls_for(*roles: str) -> tuple[list[dict[str, Any]], list[PublicCall]]:
        raw = [call for r in roles for call in by_role.get(r, [])]
        return [views[id(x)] for x in raw], raw

    cards = {k.card_id: card_view(k, lines, fx, cited, pre) for k in c.cards}
    agents: list[dict[str, Any]] = []
    risk = c.risk
    checks = risk.checks if risk else []

    # ---- 1 data steward
    trends = [r.trend for r in c.reference.values() if r.trend]
    kinds = {}
    for f in c.facts:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    holds, _general = split_holds(risk.hold_reasons if risk else [])
    stale = [lines.name(k) for k in lines.sort(holds) if any(h["text"] == "data too old" for h in holds[k])]
    closed = [lines.name(k) for k in lines.sort(holds) if any(h["text"] == "market closed" for h in holds[k])]
    cal = calendar_words([f for f in c.flags if f.startswith("calendar:")])
    data_state = ("failed" if not c.reference else "fallback" if stale or cal else "ok")
    n_facts = len(c.facts)
    agents.append(_agent(
        "data", status=code_status("ok" if data_state == "ok" else "fallback" if data_state == "fallback"
                                   else "invalid", {"ok": "ok", "fallback": "incomplete", "failed": "no data"}[data_state]),
        body={
            "n_lines": len(c.reference), "trends": " · ".join(f"{trends.count(t)} {t}" for t in ("up", "mixed", "down")
                                                           if trends.count(t)),
            "kinds": [(heading, kinds[k]) for k, heading, _ in FACT_KINDS if kinds.get(k)],
            "n_facts": len(c.facts), "stale": stale, "closed": closed, "calendar": cal, "why": run["why"],
        },
        result=(f"read {plural(len(c.reference), 'line')}"
                + (f" (trend {' · '.join(f'{trends.count(t)} {t}' for t in ('up', 'mixed', 'down') if trends.count(t))})"
                   if trends else "")
                + (f" and built {plural(n_facts, 'fact')}" if n_facts else "")),
        compact=data_state == "ok"))

    # ---- 2 reference book
    ref_rows = []
    for k in lines.sort(c.reference):
        r = c.reference[k]
        ref_rows.append({"line": k, "ticker": ticker(k), "name": lines.name(k), "trend": r.trend,
                         "day": fmt_signed(r.day_change_pct), "day_dir": move_dir(r.day_change_pct),
                         "level": fmt_level(r.level_ref), "weight": fmt_share(r.weight_ref_x)})
    ref_gross = sum(abs(r.weight_ref_x) for r in c.reference.values())
    held_ref = sum(1 for r in c.reference.values() if abs(r.weight_ref_x) > EPS)
    agents.append(_agent("reference", status=code_status("ok" if c.reference else "invalid",
                                                         "ok" if c.reference else "no book"),
                         body={"rows": ref_rows, "gross": fmt_share(ref_gross), "held": held_ref,
                               "n": len(ref_rows)},
                         result=f"would hold {held_ref} of {plural(len(ref_rows), 'line')}, {fmt_share(ref_gross)} of "
                                "the portfolio in total",
                         compact=bool(c.reference)))

    # ---- 3 volatility officer
    vol_cards = [cards[k.card_id] for k in c.cards if k.card_type == "vol_shock"]
    breaker = next((ch for ch in checks if ch.name == "vol_breaker"), None)
    shocked = [s for k in vol_cards for s in k["scope"]]
    agents.append(_agent(
        "vol", status=code_status("ok", plural(len(vol_cards), "card") if vol_cards else "no shock"),
        body={"cards": vol_cards, "breaker": breaker, "windows": []},
        result=("volatility shock on " + join_words(shocked) if shocked else "no volatility shock")
               + ("; the breaker tripped" if breaker is not None and not breaker.passed else ""),
        compact=True))

    # ---- 4 event officer
    event_cards = [cards[k.card_id] for k in c.cards if k.card_type == "event_binary"]
    windows = [lines.name(k) for k in lines.sort(c.bands) if any("event window" in r for r in c.bands[k].reasons)]
    agents.append(_agent(
        "event", status=code_status("fallback" if cal else "ok",
                                    "calendar incomplete" if cal else
                                    plural(len(event_cards), "card") if event_cards else "no event"),
        body={"cards": event_cards, "windows": windows, "calendar": cal},
        result=(plural(len(event_cards), "event card") if event_cards else "no scheduled event near this run")
               + (f"; adds blocked on {join_words(windows)}" if windows else ""),
        compact=not cal))

    # ---- 5 news analyst
    news_calls, news_raw = calls_for("news")
    news_cards = [cards[k.card_id] for k in c.cards if k.role == "news"]
    agents.append(_agent(
        "news", status=status_of(news_calls, ran=bool(news_cards)), calls=news_calls,
        meta=_calls_meta(news_calls, news_raw), failure=_failure("news", news_calls),
        body={"cards": news_cards, "n_items": kinds.get("news", 0),
              "qualifying": [x for x in news_cards if x["qualifying"]]}))

    # ---- 6 macro analyst
    macro_calls, macro_raw = calls_for("macro")
    macro_cards = [cards[k.card_id] for k in c.cards if k.role == "macro"]
    macro = None
    if c.macro is not None:
        regime, css = REGIME_WORDS.get(c.macro.regime, (c.macro.regime, "sealed"))
        macro = {"regime": regime, "css": css,
                 "drivers": [{"text": d.text, "chips": [fx.chip(e) for e in d.evidence]} for d in c.macro.drivers],
                 "tilts": [{"sleeve": k.replace("_", " "), "tilt": v, "words": TILT_WORDS.get(v, str(v))}
                           for k, v in c.macro.sleeve_tilts.items()]}
    ok_calls = [x for x in macro_calls if not x["failed"]]
    if not macro_calls:
        macro_note = "Not scheduled this run: the macro analyst runs on the first run of each UTC day."
    elif macro is None and ok_calls:
        macro_note = "Its structured output is not part of this older record; its cards, if any, are below."
    else:
        macro_note = ""
    agents.append(_agent(
        "macro", status=status_of(macro_calls, ran=False), calls=macro_calls,
        meta=_calls_meta(macro_calls, macro_raw), failure=_failure("macro", macro_calls),
        body={"macro": macro, "cards": macro_cards, "note": macro_note,
              "fx_count": kinds.get("macro", 0)}))

    # ---- 7-9 the debate
    for turn, anchor, heading, _words in TURNS:
        role_calls, raw = calls_for(turn)
        a = debate_turns(c)[turn]
        view = advocate_view(turn, a, c, lines, fx, weights_for, ref_levels)
        slug = "bull" if turn != "bear" else "bear"
        failure = _failure(turn, role_calls)
        if a is None and failure is None:
            if turn == "bull_rebuttal" and c.debate.bear is None:
                failure = {"what": "Not run: the bear gave no usable answer, so there was nothing to reply to.",
                           "instead": FALLBACK_WORDS["bear"]}
            else:
                failure = {"what": "No usable output was recorded for this turn.",
                           "instead": FALLBACK_WORDS.get(turn, "")}
        status = status_of(role_calls, ran=a is not None)
        if a is None and not role_calls:
            status = code_status("not_run", "skipped" if turn == "bull_rebuttal" else "no output")
        agents.append(_agent(slug, id=anchor, name=heading,
                             status=status, calls=role_calls, meta=_calls_meta(role_calls, raw),
                             failure=failure, turn=turn, body=view,
                             **({"icon": AGENT_ICONS["rebuttal"]} if turn == "bull_rebuttal" else {})))

    # ---- 10 portfolio manager
    pm_calls, pm_raw = calls_for("pm")
    pm_by_rep = {x["replicate"] - 1: x for x in pm_calls}
    reps = _mark_same([replicate_view(r, c.pm, pm_by_rep, c, lines, fx, weights_for, ref_levels, "a-pm", pre=pre)
                       for r in c.pm.replicates])
    council_levels = dict(c.pm.levels)
    council_changes = change_list(council_levels, ref_levels)
    pm_failure = _failure("pm", pm_calls)
    n_valid = c.pm.valid_replicates
    if pm_failure is not None and n_valid >= 2:
        pm_failure["instead"] = (f"The failed attempt was discarded; the decision used the {n_valid} valid attempts"
                                 + (", which agreed." if run["pm_same"] else "."))
    if c.basis in ("fallback_parse", "fallback_disagreement", "council_unavailable"):
        pm_failure = {"what": pm_failure["what"] if pm_failure else "",
                      "instead": "Fallback: " + BASIS_WORDS.get(c.basis, c.basis) + ". Every line was set to the "
                                 "mechanical reference."}
    agreement = []
    for k in lines.sort(c.pm.agreement_pct):
        share = c.pm.agreement_pct[k]
        if k in council_changes or share < 100 - EPS:
            agreement.append({"name": lines.name(k), "words": count_words(share, c.pm.valid_replicates),
                              "call": verb_for(*council_changes[k]) if k in council_changes else "hold"})
    agents.append(_agent(
        "pm", name=f"Portfolio manager ×{len(c.pm.replicates) or 3}",
        status=status_of(pm_calls, ran=bool(c.pm.replicates)), calls=pm_calls, meta=_calls_meta(pm_calls, pm_raw),
        failure=pm_failure,
        body={"reps": reps, "valid": c.pm.valid_replicates, "total": len(c.pm.replicates),
              "daggers": any(d["dagger"] for rp in reps for d in rp["dismissed"]),
              "ambiguous": any(d["ambiguous"] for rp in reps for d in rp["dismissed"]),
              "agreement": agreement, "agreement_rest": len(c.pm.agreement_pct) - len(agreement),
              "decision": (phrase_changes(council_changes, lines, weights=weights_for(council_changes, raw_x))
                           if council_changes else ""),
              "basis": BASIS_WORDS.get(c.basis or "", ""), "pm_lead": run["pm_lead"], "pm_same": run["pm_same"],
              "cards": list(cards.values())}))

    # ---- 11 single-agent control
    sa_calls, sa_raw = calls_for("single_agent")
    ctrl = c.single_agent
    ctrl_reps = []
    differs = []
    if ctrl is not None:
        sa_by_rep = {x["replicate"] - 1: x for x in sa_calls}
        ctrl_reps = _mark_same([replicate_view(r, ctrl, sa_by_rep, c, lines, fx, weights_for, ref_levels,
                                               "a-control", control=True, pre=pre) for r in ctrl.replicates])
        for k in lines.sort(set(ctrl.levels) & set(council_levels)):
            if abs(ctrl.levels[k] - council_levels[k]) > EPS:
                differs.append({"name": lines.name(k), "council": fmt_level(council_levels[k]),
                                "control": fmt_level(ctrl.levels[k]),
                                "council_w": fmt_share(weights_for({k: (ref_levels.get(k, 0.0), council_levels[k])})
                                                       .get(k, (0, None))[1]),
                                "control_w": fmt_share(weights_for({k: (ref_levels.get(k, 0.0), ctrl.levels[k])})
                                                       .get(k, (0, None))[1])})
    agents.append(_agent(
        "control", name=f"Single-agent control ×{len(ctrl.replicates) if ctrl else 3}",
        status=status_of(sa_calls, ran=ctrl is not None), calls=sa_calls, meta=_calls_meta(sa_calls, sa_raw),
        failure=_failure("single_agent", sa_calls),
        body={"reps": ctrl_reps, "control": run["control"], "differs": differs,
              "valid": ctrl.valid_replicates if ctrl else 0, "total": len(ctrl.replicates) if ctrl else 0,
              "present": ctrl is not None, "agrees": cv.control_agrees is True}))

    # ---- 12 auditor and bands
    audit_notes = []
    for r in c.pm.replicates:
        call = pm_by_rep.get(r.replicate)
        if not r.valid and call is not None and call["failed"]:
            audit_notes.append({"n": r.replicate + 1, "kind": "discarded", "raw": "; ".join(r.violations),
                                "text": f"no usable answer (it {call['fail_phrase']})"})
            continue
        for v in r.violations:
            audit_notes.append({"n": r.replicate + 1, "kind": "discarded" if not r.valid else "flagged",
                                **plain_violation(v, lines)})
        for v in r.reverted:
            audit_notes.append({"n": r.replicate + 1, "kind": "reverted", **plain_violation(v, lines)})
    band_rows = []
    clipped = []
    for k in lines.sort(c.bands):
        b = c.bands[k]
        raw_l = risk.raw_levels.get(k) if risk else None
        band_l = risk.banded_levels.get(k) if risk else None
        was_clipped = raw_l is not None and band_l is not None and abs(raw_l - band_l) > EPS
        if was_clipped:
            clipped.append({"name": lines.name(k), "asked": fmt_level(raw_l), "got": fmt_level(band_l)})
        band_rows.append({"name": lines.name(k), "ticker": ticker(k), "trend": b.trend,
                          "range": f"{fmt_level(b.lo)} to {fmt_level(b.hi)}", "ref": fmt_level(b.ref_level),
                          "reasons": "; ".join(b.reasons), "qualifying": b.qualifying_cards,
                          "fixed": abs(b.hi - b.lo) < EPS, "clipped": was_clipped})
    audit_state = "fallback" if audit_notes or clipped else "ok"
    agents.append(_agent(
        "audit", status=code_status(audit_state, "ok" if audit_state == "ok" else
                                    plural(len(audit_notes) + len(clipped), "correction")),
        body={"notes": audit_notes, "bands": band_rows, "clipped": clipped,
              "open": sum(1 for b in band_rows if not b["fixed"])}))

    # ---- 13 risk engine
    risk_state = "invalid" if risk is None else "ok" if run["checks_passed"] == run["checks_total"] else "fallback"
    agents.append(_agent(
        "risk", status=code_status(risk_state, {"invalid": "did not run", "ok": "all checks pass",
                                                "fallback": "limits applied"}[risk_state]),
        body={"held": [r for r in run["rows"] if r["held"]]}))

    # ---- 14 cost desk and plan
    legs = c.plan.legs if c.plan else []
    why_leg = {leg.seq: leg_reason(leg, council_changes, ref_x, lines, c.pm.medoid) for leg in legs}
    agents.append(_agent(
        "costs", status=code_status("ok" if legs else "none", plural(len(legs), "order") if legs else "no orders"),
        body={"legs": [{"leg": leg, "why": why_leg[leg.seq]} for leg in legs], "why_leg": why_leg,
              "skipped": run["skipped"]}))

    # ---- facts table
    cited_rows, rest = [], {}
    for f in c.facts:
        row = fx.row(f)
        who = cited.get(f.id, [])
        row["cited_by"] = [{"name": n, "href": f"{fx.base}#a-{s}"} for s, n in who]
        if who:
            cited_rows.append(row)
        else:
            rest.setdefault(f.kind, []).append(row)
    heading = {k: h for k, h, _ in FACT_KINDS}
    facts = {
        "cited": cited_rows, "total": len(c.facts),
        "rest": [(heading.get(k, k), rows) for k in [x for x, _, _ in FACT_KINDS] + sorted(rest)
                 if (rows := rest.pop(k, None))],
        "withheld": sum(1 for f in c.facts if f.withheld),
    }

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for a in agents:
        if not groups or groups[-1][0] != a["group"]:
            groups.append((a["group"], []))
        groups[-1][1].append(a)
    all_calls = [views[id(x)] for x in c.calls]
    state = cv.final_state
    if cv.rehearsal:
        decision = code_status("none", "not traded")
    elif state in ("completed", "completed_partial"):
        decision = code_status("ok", "executed" if state == "completed" else "partly executed")
    elif state in ("approved", "executing"):
        decision = code_status("ok", "approved")
    elif state in ("blocked", "execution_unknown"):
        decision = code_status("fallback", "under review")
    elif state in ("proposed", "awaiting_publication"):
        decision = {"key": "waiting", "glyph": "…", "word": "awaiting approval", "css": "waiting"}
    else:
        decision = code_status("none", (DECISION_CHIP.get(state, ("no decision", ""))[0]).lower())
    n_ok = sum(1 for x in all_calls if not x["failed"])
    # every failed call in one list, each with what the run did instead
    failures = []
    for x in all_calls:
        if not x["failed"]:
            continue
        who = x["agent_name"] + (f" attempt {x['replicate']}" if x["role"] in ("pm", "single_agent") else "")
        if x["role"] == "pm":
            then = (f"decision taken on the {n_valid} valid attempts" + (", which agreed" if run["pm_same"] else "")
                    if n_valid >= 2 else "every line fell back to the mechanical reference")
        else:
            then = FAIL_THEN.get(x["role"], "")
        failures.append({"who": who, "what": x["fail_phrase"], "then": then, "href": f"{base}#{x['anchor']}",
                         "css": x["css"], "in_sentence": IN_SENTENCE.get(x["role"], who).format(n=x["replicate"])})
    fail_sentence = ""
    if failures:
        parts = [f"{f['in_sentence']} {f['what']}" for f in failures]
        fail_sentence = (f"{plural(len(failures), 'model call')} failed and the run fell back safely: "
                         + join_words(parts) + ".")
    return {
        "failures": failures, "fail_sentence": fail_sentence, "chain": run["chain"],
        "decision_status": decision,
        "agents": agents, "by_id": {a["id"]: a for a in agents}, "groups": groups, "facts": facts,
        "calls": all_calls, "fx": fx, "cards": cards,
        "failed_calls": len(all_calls) - n_ok,
        "totals": {
            "calls": len(all_calls), "ok": n_ok,
            "ok_pct": 100.0 * n_ok / len(all_calls) if all_calls else None,
            "tokens_in": fmt_int(sum(x.tokens_in for x in c.calls)),
            "tokens_out": fmt_int(sum(x.tokens_out for x in c.calls)),
            "model_time": fmt_secs(sum(x.latency_ms for x in c.calls)) if c.calls else "—",
            "foot": _calls_meta(all_calls, list(c.calls)),
        },
    }


# ------------------------------------------------------------------------------ holdings (home)
FILTER_GROUPS = (
    # key, chip label, asset classes, the words for "No … held."
    ("stock", "Stocks", ("stock",), "stocks"),
    ("fund", "ETFs & indices", ("etf", "index"), "ETFs or indices"),
    ("crypto", "Crypto", ("crypto",), "crypto"),
    ("commodity", "Commodities", ("commodity",), "commodities"),
    ("fx", "FX", ("fx",), "FX pairs"),
)
AC_GROUP = {ac: key for key, _, acs, _ in FILTER_GROUPS for ac in acs}
AC_WORDS = {"stock": "Stock", "etf": "ETF", "index": "Index", "crypto": "Crypto", "commodity": "Commodity",
            "fx": "Currency pair"}
SESSION_CHIP = {
    "crypto": ("24/7", "Trades around the clock, every day"),
    "fx24x5": ("24/5", "Trades around the clock on weekdays"),
    "us": ("US hours", "Trades during US market hours"),
    "lse": ("LSE hours", "Held through a London-listed fund: trades during London Stock Exchange hours"),
}
PENDING_STATES = (None, "awaiting_publication", "proposed")
EXECUTING_STATES = ("approved", "executing")


def monogram(sym: str, asset_class: str | None) -> str:
    """A neutral tile's letters: the ticker, at most four characters (three for a currency pair)."""
    letters = re.sub(r"[^A-Z0-9]", "", ticker(sym).upper())
    return letters[:3] if asset_class == "fx" and len(letters) == 6 else letters[:4]


def weight_bar(weight: float, ref: float | None, scale: float, geo: Geometry) -> dict[str, Any]:
    """A thin magnitude bar for a table cell (long teal, short orange) with a tick at the reference."""
    side = "long" if weight > EPS else "short" if weight < -EPS else "zero"
    out: dict[str, Any] = {"side": side, "w": geo.cls("width", 100.0 * min(abs(weight), scale) / scale), "ref": None}
    if ref is not None and abs(ref) > EPS:
        out["ref"] = geo.cls("left", 100.0 * min(abs(ref), scale) / scale)
    return out


def sealed_runs(view: JournalView) -> list[dict[str, Any]]:
    """Runs that are sealed but not yet revealed (a commitment or an ops row without a cycle file),
    newest first, with their final state when the ops log already has it."""
    revealed = {cv.doc.cycle_id for cv in view.cycles}
    ops = {r.cycle_id: r for r in view.ops}
    ids = (set(view.commitments) | set(ops)) - revealed
    out = []
    for cid in sorted(ids, reverse=True):
        row = ops.get(cid)
        out.append({"cycle_id": cid, "slot": parse_cycle_id(cid), "state": row.decision_state if row else None,
                    "legs": row.legs if row else None, "sealed": cid in view.commitments})
    return out


def _filters(held: list[dict[str, Any]], flat: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """The asset-class filter chips with their counts; the classes with no flat line; those with no held line."""
    present = [(key, label, phrase) for key, label, _, phrase in FILTER_GROUPS
               if any(r["group"] == key for r in held + flat)]
    counts = {"all": (len(held), len(flat))}
    for key, _label, _phrase in present:
        counts[key] = (sum(1 for r in held if r["group"] == key), sum(1 for r in flat if r["group"] == key))
    filters = [{"key": "all", "label": "All", "phrase": "lines", "held": counts["all"][0], "total": sum(counts["all"])}]
    filters += [{"key": key, "label": label, "phrase": phrase, "held": counts[key][0], "total": sum(counts[key])}
                for key, label, phrase in present]
    empty_flat = [f["key"] for f in filters if counts[f["key"]][1] == 0]
    empty_held = [f for f in filters if counts[f["key"]][0] == 0]
    return filters, empty_flat, empty_held


def _asset(k: str, lines: Lines) -> dict[str, Any]:
    """A line's asset cell: ticker, name, monogram, class, filter group and session chip."""
    ac = lines.asset_class(k)
    session = lines.session(k)
    chip_ = SESSION_CHIP.get(session or "")
    words = AC_WORDS.get(ac or "", "")
    if session == "lse":
        words = (words + " · " if words else "") + "via a London-listed fund"
    return {"line": k, "ticker": ticker(k), "name": lines.name(k), "mono": monogram(k, ac), "ac": ac or "other",
            "ac_words": words, "group": AC_GROUP.get(ac or "", "other"),
            "session": chip_[0] if chip_ else "", "session_title": chip_[1] if chip_ else ""}


def build_holdings(view: JournalView, lines: Lines, geo: Geometry, kill: dict[str, Any],
                   status: dict[str, Any]) -> dict[str, Any]:
    """The home page's holdings list, modelled on a broker's portfolio list but percent-only: one
    row per line (asset, 1-day move, position, P/L % since open, weight with the reference tick,
    reference weight), held lines by weight, flat lines folded into "Not held". Before any run or
    book it is a skeleton: every line of the policy, nothing held."""
    latest = view.cycles[0] if view.cycles else None
    if latest is None and view.book is None:
        flat = []
        for k in lines.sort(lines.info):
            flat.append({**_asset(k, lines), "day": "—", "day_dir": "none", "day_raw": None, "direction": "flat",
                         "position": "—", "lev": "", "settlement": "", "pnl": "—", "pnl_dir": "none", "weight": 0.0,
                         "share1": "—", "ref": None, "ref_share1": "—", "ref_above": False, "show_bar": False,
                         "bar": {"side": "zero", "w": "", "ref": None}, "notes": [],
                         "title": f"{lines.name(k)}: not held yet"})
        filters, empty_flat, empty_held = _filters([], flat)
        return {"basis": "skeleton", "label": "", "when": "", "run_id": "", "held": [], "flat": flat,
                "filters": filters, "empty_flat": empty_flat, "empty_held": empty_held, "strip": None,
                "scale": "", "target": True, "show_day": False, "show_pnl": False}
    doc = latest.doc if latest is not None else None
    book = view.book
    if latest is not None and latest.rehearsal:
        basis = "target"
    elif book is not None:
        basis = "book"
    else:
        basis = "target"
    rows: dict[str, dict[str, Any]] = {}
    refs: dict[str, float] = {k: r.weight_ref_x for k, r in doc.reference.items()} if doc is not None else {}
    days: dict[str, float] = ({k: r.day_change_pct for k, r in doc.reference.items() if r.day_change_pct is not None}
                              if doc is not None else {})
    if basis == "book" and book is not None:
        as_of = next((cv.doc for cv in view.cycles if cv.doc.cycle_id == book.as_of_cycle_id), None)
        book_days = {k: r.day_change_pct for k, r in as_of.reference.items()
                     if r.day_change_pct is not None} if as_of is not None else {}
        weights = {k: b.weight_x for k, b in book.lines.items()}
        refs = {**refs, **{k: b.reference_weight_x for k, b in book.lines.items() if b.reference_weight_x is not None}}
        for k, b in book.lines.items():
            rows[k] = {"direction": b.direction, "settlement": b.settlement, "leverage": b.leverage,
                       "pnl": b.pnl_since_open_pct,
                       "day": b.day_change_pct if b.day_change_pct is not None else book_days.get(k)}
        gross, net, cash = book.gross_x, book.net_x, book.cash_x
        when = fmt_when(parse_cycle_id(book.as_of_cycle_id))
        run_id = book.as_of_cycle_id
    else:
        weights = dict(doc.risk.final_x) if doc is not None and doc.risk else {}
        gross = sum(abs(v) for v in weights.values())
        net = sum(weights.values())
        cash = max(0.0, 1.0 - gross)
        when = fmt_when(doc.slot) if doc is not None else ""
        run_id = doc.cycle_id if doc is not None else ""
    keys = lines.sort(set(lines.info) | set(weights) | set(refs))
    scale = nice_scale(max([abs(v) for v in list(weights.values()) + list(refs.values())] + [0.05]))

    # the latest run's council changes and risk holds, as a note under the line's name
    per_line, _ = split_holds(doc.risk.hold_reasons if doc is not None and doc.risk else [])
    ref_levels = {k: r.level_ref for k, r in doc.reference.items()} if doc is not None else {}
    council = dict(doc.pm.levels) if doc is not None else {}
    units = unit_weights(doc) if doc is not None else {}
    raw_x = dict(doc.risk.raw_x) if doc is not None and doc.risk else {}
    medoid = next((r for r in doc.pm.replicates if r.replicate == doc.pm.medoid), None) if doc is not None else None
    dev_dir = {d.line: d.direction for d in medoid.deviations} if medoid else {}
    verbs = {"cut": "cut", "add": "added", "short": "went short", "cover": "covered", "lever": "levered up"}
    decided = "" if latest is None or latest.rehearsal else DECISION_NOTE.get(latest.final_state or "", "")

    held, flat = [], []
    for k in keys:
        w = weights.get(k, 0.0)
        ref = refs.get(k)
        extra = rows.get(k, {})
        day = extra.get("day", days.get(k)) if basis == "book" else days.get(k)
        direction = extra.get("direction") or ("long" if w > EPS else "short" if w < -EPS else "flat")
        lev = extra.get("leverage")
        settlement = extra.get("settlement")
        pnl = extra.get("pnl") if basis == "book" else None
        notes = []
        rl, cl = ref_levels.get(k), council.get(k)
        if rl is not None and cl is not None and abs(cl - rl) > EPS:
            verb = verbs.get(dev_dir.get(k) or verb_for(rl, cl).split(" ")[0], verb_for(rl, cl))
            wb = ref if ref is not None else (units[k] * rl if k in units else None)
            wa = raw_x.get(k, units[k] * cl if k in units else None)
            text = (f"Council {verb}: {fmt_share(wb)} → {fmt_share(wa)}" if wb is not None and wa is not None
                    else f"Council {verb}: size {fmt_level(rl)} → {fmt_level(cl)}")
            notes.append(text + (f", {decided}" if decided else ""))
        for h in per_line.get(k, []):
            notes.append(f"Held back: {h['text']}")
        is_held = abs(w) > EPS
        row = {
            **_asset(k, lines),
            "day": fmt_signed(day), "day_dir": move_dir(day), "day_raw": day,
            "direction": direction, "position": {"long": "Long", "short": "Short"}.get(direction, "—"),
            "lev": f"{lev}x" if lev and lev > 1 else "", "lev_n": lev or 1,
            "settlement": {"real": "real", "cfd": "CFD", "realFutures": "futures", "marginTrade": "margin"}.get(
                settlement or "", ""),
            "pnl": fmt_signed(pnl), "pnl_dir": move_dir(pnl), "pnl_raw": pnl,
            "weight": w, "share1": fmt_share1(w) if is_held else "0.0%",
            "ref": ref, "ref_share1": fmt_share1(ref) if ref is not None else "—",
            "ref_above": not is_held and ref is not None and abs(ref) > EPS,
            "show_bar": is_held or (ref is not None and abs(ref) > EPS),
            "bar": weight_bar(w, ref, scale, geo), "notes": notes,
            "title": f"{lines.name(k)}: {fmt_share(w)} of the portfolio"
                     + (f"; the mechanical reference would hold {fmt_share(ref)}" if ref is not None else ""),
        }
        (held if is_held else flat).append(row)
    order = {k: i for i, k in enumerate(keys)}
    held.sort(key=lambda r: (-abs(r["weight"]), order[r["line"]]))
    flat.sort(key=lambda r: (-(r["ref"] or 0.0), order[r["line"]]))
    filters, empty_flat, empty_held = _filters(held, flat)

    # the book's 1-day move: the weighted sum of the held lines' last daily returns
    known = [(r["weight"], r["day_raw"]) for r in held if r["day_raw"] is not None]
    book_day = sum(w * d for w, d in known) if known else None
    # the book's P/L since open: each line's P/L % weighted by the amount invested in it (exposure / leverage)
    inv = [(abs(r["weight"]) / max(float(r["lev_n"]), 1.0), r["pnl_raw"]) for r in held if r["pnl_raw"] is not None]
    pnl_book = (sum(i * p for i, p in inv) / sum(i for i, _ in inv)) if inv and sum(i for i, _ in inv) > EPS else None

    halt_dd = (1 - float(kill.get("halt_at", 0.75))) * 100
    warn_dd = (1 - float(kill.get("warn_at", 0.80))) * 100
    dd = None if basis == "target" else next((p.drawdown_pct for p in reversed(view.performance)
                                               if p.drawdown_pct is not None), None)
    if dd is None:
        meter = {"value": "—", "fill": geo.cls("width", 0), "state": "none",
                 "sub": "tracked from go-live" if basis == "target" else "no data yet",
                 "aria": "Fall from peak: " + ("not tracked until go-live" if basis == "target" else "no data yet")}
    else:
        depth = abs(min(dd, 0.0))
        state = "halted" if depth >= halt_dd - EPS else "warn" if depth >= warn_dd - EPS else "ok"
        value = f"−{depth:.1f}%" if depth >= 0.05 else "0%"
        meter = {"value": value, "fill": geo.cls("width", 100.0 * min(depth, halt_dd) / halt_dd), "state": state,
                 "sub": "below the best value so far", "aria": f"Fall from peak {value}"}
    meter.update({"warn_at": geo.cls("left", 100.0 * warn_dd / halt_dd), "warn_label": f"−{warn_dd:.0f}%",
                  "halt_label": f"−{halt_dd:.0f}%"})

    rehearsal = latest is not None and latest.rehearsal
    if basis == "target":
        label = ("Target book — no account connected, nothing traded" if rehearsal or status["state"] == "AWAITING_ACCOUNT"
                 else "Target book of the latest run — the live book is not published yet")
    else:
        label = "Live book"
    if rehearsal:
        account = {"label": "REHEARSAL", "css": "rehearsal", "sub": "no broker account"}
    elif status["state"] == "AWAITING_ACCOUNT":
        account = {"label": "AWAITING ACCOUNT", "css": "awaiting", "sub": "no broker account yet"}
    else:
        account = {"label": status["label"], "css": status["css"], "sub": "agent portfolio"}
    kc = chip(KILL_CHIP, status["kill"])
    if basis == "target":
        day_label = "Target, last day"
        day_sub = ("hypothetical: target weights × last daily move; nothing traded" if known
                   else "no daily moves published")
    else:
        day_label = "Book, last day"
        day_sub = (f"weight × last daily move, {plural(len(known), 'line')}" if known
                   else "no daily moves published")
    return {
        "basis": basis, "label": label, "when": when, "run_id": run_id, "held": held, "flat": flat,
        "filters": filters, "empty_flat": empty_flat, "empty_held": empty_held,
        "strip": {
            "account": account, "gross": fmt_x(gross), "gross_share": fmt_share(gross), "net": fmt_x(net),
            "net_share": fmt_share(net), "cash": fmt_share(cash), "held": len(held), "lines": len(held) + len(flat),
            "day": fmt_signed(book_day), "day_dir": move_dir(book_day), "day_known": len(known),
            "day_label": day_label, "day_sub": day_sub,
            "pnl": fmt_signed(pnl_book), "pnl_dir": move_dir(pnl_book), "pnl_known": len(inv),
            "kill": kc, "meter": meter, "all_long": not any(r["weight"] < -EPS for r in held),
        },
        "scale": fmt_share(scale),
        "target": basis == "target",
        "show_pnl": basis == "book",
        "show_day": any(r["day_raw"] is not None for r in held + flat),
    }


# ------------------------------------------------------------------------------ agents pages
HISTORY_CAP = 30


def prompt_files(prompts_dir: Path) -> dict[str, dict[str, str]]:
    """prompts/manifest.json keyed by file stem -> {id, sha, href}; tolerant of a missing file."""
    path = prompts_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    out: dict[str, dict[str, str]] = {}
    if isinstance(data, dict):
        for stem, meta in data.items():
            if not isinstance(meta, dict) or not re.fullmatch(r"[a-z_]{1,32}", str(stem)):
                continue
            sha = str(meta.get("sha256") or meta.get("sha") or "")
            out[str(stem)] = {"id": str(meta.get("id") or stem)[:64],
                              "sha": short_sha(sha) if re.fullmatch(r"[0-9a-f]{8,64}", sha) else "",
                              "href": f"{REPO_URL}/blob/main/prompts/{stem}.md"}
    return out


def _share_words(n: int, total: int) -> str:
    return "—" if total == 0 else f"{n} of {total}"


def ring_css(pct: float | None) -> str:
    """A ring's colour by how much of the whole it covers: ok (≥ 90%), fallback (≥ 60%), failed."""
    if pct is None:
        return "idle"
    return "ok" if pct >= 90 - EPS else "fallback" if pct >= 60 - EPS else "failed"


def build_agents(view: JournalView, transcripts: dict[str, dict[str, Any]], prompts: dict[str, dict[str, str]],
                 model: str) -> list[dict[str, Any]]:
    """One entry per agent: its spec, its numbers over every published run and its history
    (newest first; the transcript sections of each run, with links into the run page)."""
    out = []
    for spec in AGENT_SPECS:
        calls = [call_view(x) for cv in view.cycles for x in cv.doc.calls if x.role in spec.roles]
        n = len(calls)
        ok = sum(1 for x in calls if not x["failed"])
        # every failed call, newest run first, linking to its place in the run
        failed = [{"when": fmt_short_when(cv.doc.slot), "word": v["word"], "error": v["error_word"],
                   "what": v["fail_phrase"], "css": v["css"],
                   "attempt": v["replicate"] if x.role in ("pm", "single_agent") else 0,
                   "href": f"../cycles/{cv.doc.cycle_id}.html#{v['anchor']}"}
                  for cv in view.cycles for x in cv.doc.calls if x.role in spec.roles
                  for v in [call_view(x)] if v["failed"]]
        kinds: dict[str, int] = {}
        for f in failed:
            kinds[f["word"]] = kinds.get(f["word"], 0) + 1
        entries = []
        for cv in view.cycles:
            tr = transcripts[cv.doc.cycle_id]
            sections = [a for a in tr["agents"] if a["slug"] == spec.slug]
            if not sections:
                continue
            entries.append({"cv": cv, "sections": sections, "href": f"../cycles/{cv.doc.cycle_id}.html",
                            "chain": tr["chain"] if spec.slug in ("bull", "bear", "pm", "control") else [],
                            "status": sections[0]["status"] if len(sections) == 1 else _merge_status(sections)})
        seen = [e for e in entries if any(a["calls"] for a in e["sections"])] if spec.roles else entries
        stats = {
            "runs": len(entries), "calls": n, "ok": ok, "ok_pct": f"{100.0 * ok / n:.0f}%" if n else "—",
            "ok_num": 100.0 * ok / n if n else None, "ok_css": ring_css(100.0 * ok / n if n else None),
            "parse_fail": sum(1 for x in calls if x["status"] == "parse_fail"),
            "timeouts": sum(1 for x in calls if x["status"] == "timeout"),
            "other_fail": sum(1 for x in calls if x["failed"] and x["status"] not in ("parse_fail", "timeout")),
            "corrected": sum(1 for x in calls if x["status"] == "corrected"),
            "latency": fmt_secs(int(m)) if (m := median([float(x["latency_ms"]) for x in calls])) is not None else "—",
            "last": seen[0] if seen else None, "failed": failed,
            "fail_words": [plural(k, w, w if w in ("skipped", "invalid", "not run") else None)
                           for w, k in kinds.items()],
            "worst": _worst([e["status"] for e in entries]),
        }
        if spec.slug in ("bull", "bear"):
            # did the used attempt do what the advocate finally asked (the bull: its rebuttal, when
            # it gave one)? And did it name the advocate as its side?
            got = named = decided = 0
            for e in entries:
                bodies = [a["body"] for a in e["sections"] if a["body"]]
                if not bodies or not bodies[-1]["used_ok"]:
                    continue
                decided += 1
                got += bool(bodies[-1]["used_got"])
                named += bool(bodies[-1]["used_sided"])
            claims = dismissed = 0
            for e in entries:
                for a in e["sections"]:
                    if a.get("turn") == "bull_rebuttal" or not a["body"]:
                        continue
                    for cl in a["body"]["claims"]:
                        claims += 1
                        if any(f["verdict"] == "dismissed" and "(used)" in f["who"] for f in cl["fates"]):
                            dismissed += 1
            stats["got"] = f"{got} of {plural(decided, 'run')}" if decided else "—"
            stats["named"] = named
            stats["dismissed"] = _share_words(dismissed, claims)
        elif spec.slug == "pm":
            valid = [r for cv in view.cycles for r in cv.doc.pm.replicates]
            stats["valid"] = _share_words(sum(1 for r in valid if r.valid), len(valid))
            stats["changed"] = _share_words(
                sum(1 for cv in view.cycles if transcripts[cv.doc.cycle_id]["by_id"]["a-pm"]["body"]["decision"]),
                len(view.cycles))
        elif spec.slug == "control":
            stats["differs"] = _share_words(sum(1 for cv in view.cycles if cv.control_agrees is False),
                                            sum(1 for cv in view.cycles if cv.control_agrees is not None))
        stems = [r for r in spec.roles if r in prompts] if spec.roles else []
        out.append({
            "spec": spec, "slug": spec.slug, "name": spec.name, "kind": spec.kind, "accent": spec.accent,
            "icon": AGENT_ICONS.get(spec.slug, "cpu"),
            "job": spec.job, "more": spec.more, "model": model if spec.kind == "LLM" else "code",
            "source": f"{REPO_URL}/blob/main/{spec.source}" if spec.source else "",
            "source_path": spec.source, "prompts": [{"stem": s, **prompts[s]} for s in stems],
            "stats": stats, "entries": entries[:HISTORY_CAP], "older": entries[HISTORY_CAP:],
        })
    return out


STATUS_RANK = {"failed": 4, "timeout": 3, "fallback": 2, "waiting": 1, "idle": 1, "ok": 0}


def _worst(statuses: list[dict[str, str]]) -> dict[str, str] | None:
    """The worst status over a window of runs (a failure anywhere shows, not only the last run)."""
    return max(statuses, key=lambda s: STATUS_RANK.get(s["css"], 1), default=None)


def _merge_status(sections: list[dict[str, Any]]) -> dict[str, str]:
    """One status for an agent with several sections in a run (the bull's two turns): ok when
    every turn that ran was ok, the failure when every turn failed, else "partly failed"."""
    okish, idle = {"ok", "corrected", "cached"}, {"not_run", "none"}
    keys = [a["status"]["key"] for a in sections]
    if all(k in okish | idle for k in keys):
        ran = [k for k in keys if k in okish]
        return code_status("corrected" if "corrected" in ran else "ok") if ran else sections[0]["status"]
    bad = [a["status"] for a in sections if a["status"]["key"] not in okish | idle]
    if not any(k in okish for k in keys):
        return bad[0]
    return code_status("partial")


# ------------------------------------------------------------------------------ charts
GRID_STEPS = (0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)


def performance_chart(points: list[PublicPerformancePoint], width: int = 690, height: int = 240) -> dict[str, Any] | None:
    """Polylines for each control with at least two values, a recessive grid at round index values
    and a label at the end of each line (identity never by colour alone)."""
    if len(points) < 2:
        return None
    pad_l, pad_r, pad_y = 44, 120, 14
    values = [getattr(p, key) for p in points for key, *_ in CONTROL_SERIES if getattr(p, key) is not None]
    if not values:
        return None
    lo, hi = min(values + [100.0]), max(values + [100.0])
    step = next((s for s in GRID_STEPS if (hi - lo) / s <= 4), GRID_STEPS[-1])
    lo, hi = step * (lo // step), step * -(-hi // step)
    if hi - lo < EPS:
        lo, hi = lo - step, hi + step
    span = hi - lo
    n = len(points) - 1
    plot_w, plot_h = width - pad_l - pad_r, height - 2 * pad_y

    def fx(i: int) -> float:
        return pad_l + plot_w * i / n

    def fy(v: float) -> float:
        return pad_y + plot_h * (1 - (v - lo) / span)

    series = []
    for key, name, css, short, what in CONTROL_SERIES:
        coords = [(fx(i), fy(getattr(p, key))) for i, p in enumerate(points) if getattr(p, key) is not None]
        if len(coords) < 2:
            continue
        last = next(getattr(p, key) for p in reversed(points) if getattr(p, key) is not None)
        series.append({"key": key, "label": name, "css": css, "short": short, "what": what, "last": last,
                       "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in coords),
                       "end_x": f"{coords[-1][0] + 6:.1f}", "y": coords[-1][1]})
    # end labels at least 13 units apart, kept inside the plot
    placed = sorted(series, key=lambda s: s["y"])
    for i, s in enumerate(placed):
        s["label_y"] = max(s["y"], placed[i - 1]["label_y"] + 13) if i else max(s["y"], pad_y)
    overflow = (placed[-1]["label_y"] - (height - 4)) if placed else 0
    if overflow > 0:
        for s in placed:
            s["label_y"] -= overflow
    for s in series:
        s["label_y"] = f"{s['label_y']:.1f}"
    grid = []
    v = lo
    while v <= hi + EPS:
        grid.append({"y": f"{fy(v):.1f}", "label": f"{round(v, 2):g}", "base": abs(v - 100.0) < EPS})
        v += step
    return {
        "width": width, "height": height, "series": series, "grid": grid,
        "x0": pad_l, "x1": width - pad_r, "base_y": f"{fy(100.0):.1f}",
        "first": fmt_day(points[0].as_of), "last": fmt_day(points[-1].as_of),
    }


# ------------------------------------------------------------------------------ rendering
def make_env(lines: Lines | list[str] | None = None) -> Environment:
    if not isinstance(lines, Lines):
        lines = Lines({"lines": [{"symbol": s} for s in (lines or [])]})
    line_set = lines

    def by_line(mapping: dict[str, Any]) -> list[tuple[str, Any]]:
        """Line-keyed dicts in universe order (journal files store keys sorted alphabetically)."""
        return [(k, mapping[k]) for k in line_set.sort(mapping)]

    icons = load_icons()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        x=fmt_x, pct=fmt_pct, bp=fmt_bp, level=fmt_level, slot=fmt_slot, sha=short_sha, value=fmt_value,
        by_line=by_line, sort_lines=line_set.sort, share=fmt_share, when=fmt_when, day=fmt_day,
        line_name=line_set.name, hold=plain_hold, pct1=fmt_pct1, late=fmt_late,
        signed=fmt_signed, move=move_dir, count=fmt_int, secs=fmt_secs, ticker=ticker, short_when=fmt_short_when,
        share1=fmt_share1, cap=lambda s: str(s)[:1].upper() + str(s)[1:],
    )
    env.globals.update(
        decision_chip=lambda state: chip(DECISION_CHIP, state),
        kill_chip=lambda state: chip(KILL_CHIP, state),
        mode_chip=lambda mode: chip(MODE_CHIP, mode),
        ev=lambda ref: evidence_label(ref, line_set),
        check_name=lambda name: CHECK_NAMES.get(name, name.replace("_", " ").capitalize()),
        basis_words=lambda basis: BASIS_WORDS.get(basis or "", (basis or "—").replace("_", " ")),
        agreement_words=count_words,
        plural=plural,
        id_label=lambda rid: evidence_label(SimpleNamespace(kind="card", id=rid), line_set),
        repo_url=REPO_URL,
        icon=lambda name, cls="": icon_svg(icons, name, cls),
        status_icon=lambda css: STATUS_ICONS.get(css, "circle-dot"),
        ring_cls=Geometry().ring,        # replaced by the build's own geometry in build()
        ring_css=ring_css,
        how_words=HOW_WORDS,
    )
    return env


def _status_context(view: JournalView, now: datetime) -> dict[str, Any]:
    st = view.status
    slot = st.last_cycle_at or (view.cycles[0].doc.slot if view.cycles else None)
    label, css = STATUS_CHIP[st.state]
    last_id = st.last_cycle_id or (view.cycles[0].doc.cycle_id if view.cycles else None)
    last_view = next((c for c in view.cycles if c.doc.cycle_id == last_id), None)
    last_ops = next((r for r in view.ops if r.cycle_id == last_id), None)
    if last_view is not None:
        last_decision: dict[str, str] | None = last_view.chip
    elif last_ops is not None:
        last_decision = chip(DECISION_CHIP, last_ops.decision_state)
    else:
        last_decision = None
    # When the last run actually happened: its slot plus how late it started (a catch-up can be hours late).
    late = last_view.doc.late_by_min if last_view is not None else last_ops.late_by_min if last_ops is not None else 0
    ran = slot + timedelta(minutes=late) if slot else None
    latest = view.cycles[0] if view.cycles else None
    if latest is not None:
        mode = latest.doc.mode
    else:
        mode = "live" if st.state != "AWAITING_ACCOUNT" else None
    if mode == "rehearsal" and st.state == "AWAITING_ACCOUNT":
        label, css = "REHEARSAL · NO ACCOUNT", "rehearsal"
    return {
        "state": st.state, "label": label, "css": css, "note": st.note, "kill": st.kill_state,
        "last_cycle_id": last_id,
        "last_cycle_at": ran.strftime("%Y-%m-%dT%H:%M:%SZ") if ran else "",
        "last_when": fmt_when(ran) if ran else "",
        "last_slot_when": fmt_when(slot) if slot else "",
        "last_late": f"{fmt_late(late)} after its {fmt_clock(slot)} slot" if ran and late else "",
        "last_cycle_revealed": last_view is not None,
        "last_decision": last_decision,
        "mode": mode, "mode_chip": chip(MODE_CHIP, mode),
        "prelive": st.state == "AWAITING_ACCOUNT" or mode in (None, "rehearsal"),
        "built": fmt_when(now), "built_clock": fmt_clock(now),
    }


def prelive_disclaimer(items: list[dict[str, str]], rehearsal: bool) -> list[dict[str, str]]:
    """Before go-live, the "real money is at risk" item says that nothing is at risk yet."""
    out = []
    for item in items:
        if item["title"].lower().startswith("real money is at risk"):
            why = "this is a rehearsal with no broker account" if rehearsal else "no broker account is connected yet"
            text = item["text"].replace("The author holds the positions shown.",
                                        "Once live, the author holds the positions shown.")
            item = {"title": "Real money will be at risk once live.",
                    "text": f"Not yet: {why}, so nothing is at risk now. {text}"}
        out.append(item)
    return out


def redirect_page(target: str, name: str) -> str:
    """A tiny page that sends an old link to its new home (no script: a meta refresh)."""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        "<meta name=\"color-scheme\" content=\"dark\">\n"
        f"<meta http-equiv=\"refresh\" content=\"0; url={target}\">\n"
        f"<link rel=\"canonical\" href=\"{target}\">\n"
        "<link rel=\"stylesheet\" href=\"static/style.css\">\n"
        f"<title>Moved to {name} · council-book</title>\n</head>\n"
        f"<body class=\"moved\"><p>This page moved to <a href=\"{target}\">{name}</a>.</p></body>\n</html>\n"
    )


def build(journal_dir: Path, prompts_dir: Path, policy_dir: Path, out_dir: Path,
          now: datetime | None = None) -> list[Path]:
    """Render every page into `out_dir`. Returns the files written."""
    journal_dir, prompts_dir, policy_dir, out_dir = map(Path, (journal_dir, prompts_dir, policy_dir, out_dir))
    now = (now or datetime.now(UTC)).astimezone(UTC)
    view = load_journal(journal_dir)
    risk = yaml.safe_load((policy_dir / "risk.yaml").read_text()) or {}
    universe = yaml.safe_load((policy_dir / "universe.yaml").read_text()) or {}
    reference_file = policy_dir / "reference.yaml"
    reference = (yaml.safe_load(reference_file.read_text()) or {}) if reference_file.exists() else {}
    lines = Lines(universe)
    lines.describe(view.book)
    env = make_env(lines)
    geo = Geometry()
    env.globals["ring_cls"] = geo.ring
    status = _status_context(view, now)
    disclaimer = load_disclaimer(policy_dir.parent)
    if status["prelive"]:
        disclaimer = prelive_disclaimer(disclaimer, rehearsal=status["mode"] == "rehearsal")
    common = {
        "csp": Markup(CSP),               # a constant; single quotes must not be entity-escaped
        "nav": NAV,
        "status": status,
        "disclaimer": disclaimer,
        "kill": risk.get("killswitch", {}),
        "kill_phrase": kill_phrase(risk.get("killswitch", {})),
        "reference_gloss": reference_gloss(reference),
        "gross": risk.get("gross", {}),
        "invariants": {
            "gross": invariants.GROSS_HARD_MAX, "halt": invariants.HALT_AT_PEAK_FRACTION,
            "stop": invariants.STOP_LOSS_ON_EVERY_OPEN, "human": invariants.HUMAN_APPROVAL_REQUIRED,
            "main_account": invariants.NEVER_TOUCH_MAIN_ACCOUNT,
        },
        "lines": universe.get("lines", []),
        "reference_gross_max": universe.get("reference_gross_max"),
        "max_deviations": risk.get("authority", {}).get("max_deviations_per_cycle", 3),
        "flow_empty": empty_flow(len(lines.info)),
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

    runs = {cv.doc.cycle_id: build_run_view(cv, lines) for cv in view.cycles}
    transcripts = {cid: build_transcript(cv, lines, runs[cid])
                   for cv in view.cycles for cid in [cv.doc.cycle_id]}
    # the same transcripts with links into the run pages, for the agents' history pages
    linked = {cid: build_transcript(cv, lines, runs[cid], base=f"../cycles/{cid}.html")
              for cv in view.cycles for cid in [cv.doc.cycle_id]}
    council_cfg = yaml.safe_load((policy_dir / "council.yaml").read_text()) or {}
    agents = build_agents(view, linked, prompt_files(prompts_dir), str(council_cfg.get("model", "")))
    latest = view.cycles[0] if view.cycles else None
    ops_on_time = sum(1 for r in view.ops if r.status == "on_time")
    chart = performance_chart(view.performance)
    sealed = sealed_runs(view)
    render("index.html.j2", "index.html", "", "portfolio", latest=latest,
           pending=[dict(x, chip=chip(DECISION_CHIP, x["state"] or "awaiting_publication")) for x in sealed if x["state"] in PENDING_STATES],
           executing=[x for x in sealed if x["state"] in EXECUTING_STATES],
           run=runs[latest.doc.cycle_id] if latest else None,
           tr_latest=transcripts[latest.doc.cycle_id] if latest else None,
           holdings=build_holdings(view, lines, geo, risk.get("killswitch", {}), status),
           recent=[(cv, runs[cv.doc.cycle_id], transcripts[cv.doc.cycle_id]) for cv in view.cycles[:5]],
           cycles_count=len(view.cycles), ops_count=len(view.ops), ops_on_time=ops_on_time,
           chart=chart, points=view.performance[-10:][::-1],
           controls=[sr for sr in CONTROL_SERIES if any(x["key"] == sr[0] for x in (chart or {}).get("series", []))])
    render("cycles.html.j2", "cycles.html", "", "runs",
           cycles=[(cv, runs[cv.doc.cycle_id], transcripts[cv.doc.cycle_id]) for cv in view.cycles])
    for cv in view.cycles:
        render("cycle.html.j2", f"cycles/{cv.doc.cycle_id}.html", "../", "runs", cv=cv, c=cv.doc,
               run=runs[cv.doc.cycle_id], tr=transcripts[cv.doc.cycle_id])
    render("agents.html.j2", "agents/index.html", "../", "agents", agents=agents, cycles_count=len(view.cycles),
           model=str(council_cfg.get("model", "")), think=bool(council_cfg.get("think", False)))
    for agent in agents:
        render("agent.html.j2", f"agents/{agent['slug']}.html", "../", "agents", agent=agent, agents=agents)
    render("how.html.j2", "how.html", "", "how", roster=load_roster(prompts_dir, policy_dir))
    render("rules.html.j2", "rules.html", "", "rules", rules=load_rules(policy_dir))
    render("record.html.j2", "record.html", "", "record", incidents=view.incidents, withdrawn=load_withdrawn())
    for old, new, name in REDIRECTS:
        path = out_dir / old
        path.write_text(redirect_page(new, name), encoding="utf-8")
        written.append(path)

    static_out = out_dir / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for src in STATIC.iterdir():
        if src.is_file():
            shutil.copy2(src, static_out / src.name)
            written.append(static_out / src.name)
    (static_out / "geometry.css").write_text(geo.css(), encoding="utf-8")
    written.append(static_out / "geometry.css")
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
