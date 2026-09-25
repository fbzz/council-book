"""Static site for the public record, built from journal/, prompts/ and policy/ only.

Usage: uv run python site/build.py [--journal journal] [--prompts prompts] [--policy policy] [--out _site]

Pages: Portfolio (index.html: holdings + a diagram of the latest run) · Runs (cycles.html and one
page per run under cycles/) · How it works (how.html) · Rules (rules.html) · Record (record.html).
The old names (council.html, book.html, failures.html) are tiny redirect pages.

Rules:
- Jinja2 autoescape is ON and undefined variables fail the build; model text is never rendered as
  HTML or markdown. The only script is a constant inline "stale" badge, allowed by its CSP hash.
- A strict Content-Security-Policy meta tag on every page; no external fonts, scripts or trackers.
  The CSP forbids inline style attributes, so data-driven widths (bars, meters) are classes defined
  in a stylesheet generated at build time (static/geometry.css).
- Every sentence that describes a run is built here from the JSON with fixed templates; no model
  text is paraphrased or summarised by a model.
- The site must build with zero cycles (status AWAITING ACCOUNT) and every output file must pass
  the leak scan, otherwise the build fails and nothing is deployed.
- A cycle file is the exact sealed document, sealed BEFORE the human decision. The final outcome
  shown for a cycle comes from its execution file, else its ops row, else the sealed document.
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
    PublicExecution,
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
EPS = 1e-9

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
    {"key": "portfolio", "href": "index.html", "label": "Portfolio"},
    {"key": "runs", "href": "cycles.html", "label": "Runs"},
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
    "macro": ("Macro analyst", "Describes the macro regime and its drivers. Context only.", "CONTEXT", "teal"),
    "filings": ("Filings analyst", "Reads company filings (arrives with single stocks).", "ADVISES", "analyst"),
    "sector": ("Sector analyst", "Ranks names inside a peer group (arrives with single stocks).", "CONTEXT", "teal"),
    "bull": ("Bull advocate", "Opens the debate, then answers the bear's rebuttal.", "ADVISES", "bull"),
    "bear": ("Bear advocate", "Rebuts the bull's specific claims, citing evidence.", "ADVISES", "bear"),
    "pm": ("Portfolio manager", "Proposes at most three changes to the reference, inside ranges that code enforces. Three independent attempts; the most typical one (the medoid) is used.", "DECIDES", "pm"),
    "single_agent_control": ("Single-agent control", "One agent, the same facts, no analysts and no debate. Published as a control; it never trades.", "CONTEXT", "stone"),
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

# Evidence ids -> plain labels. The raw id always stays in the title attribute for auditors.
MARKET_FIELDS = {
    "trend": "trend", "dist_sma50": "vs 50-day average", "dist_sma200": "vs 200-day average",
    "dist_sma200_pct": "vs 200-day average", "dist_sma50_pct": "vs 50-day average",
    "mom10d": "10-day change", "mom63d": "3-month change", "dd52": "drop from 1-year high",
    "ret1d_sigma": "last day's move vs normal", "data_age_h": "age of the data", "market_open": "market open or closed",
}
VOL_FIELDS = {"sigma_ann": "yearly volatility", "vol_ratio": "volatility vs its 1-year norm",
              "ewma5_60": "volatility shock"}
COST_FIELDS = {"per_side_bps": "cost per side", "bps_side": "cost per side", "carry_bps_day": "overnight cost"}
FRED_SERIES = {
    "DGS10": "10-year Treasury yield", "DGS2": "2-year Treasury yield", "T10Y2Y": "10y–2y yield curve",
    "DFF": "Fed funds rate", "DTWEXBGS": "broad US dollar index", "VIXCLS": "VIX",
}
FRED_MEASURES = {"chg20": "20-day change"}
EVENT_KINDS = {"fomc": "FOMC", "cpi": "US inflation (CPI)", "nfp": "US jobs report", "pce": "US inflation (PCE)",
               "earnings": "earnings"}
CARD_ROLES = {"vol": "volatility card", "news": "news card", "macro": "macro card", "event": "event card",
              "filings": "filing card", "sector": "sector card"}
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
HOLD_LINE = re.compile(r"^([A-Z0-9]{2,12}): (.+)$")
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
    "parse_fail": "the answer could not be read", "over_max_deviations": "more changes than allowed",
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


class Lines:
    """The universe's lines, in policy order, with their plain names."""

    def __init__(self, universe: dict[str, Any]):
        self.info: dict[str, LineInfo] = {}
        for raw in universe.get("lines", []) or []:
            sym = str(raw.get("symbol"))
            self.info[sym] = LineInfo(
                symbol=sym, name=str(raw.get("name") or sym), sleeve=str(raw.get("sleeve") or "core"),
                in_reference=bool(raw.get("in_reference", True)),
                council=bool(raw.get("council_deviations", True)),
            )
        self.order = {sym: i for i, sym in enumerate(self.info)}

    def name(self, sym: str) -> str:
        info = self.info.get(sym)
        return info.name if info else sym

    def sort(self, keys: Any) -> list[str]:
        return sorted(set(keys), key=lambda k: (self.order.get(k, len(self.order)), k))


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

    def css(self) -> str:
        head = "/* Generated by site/build.py: bar widths and tick positions (the CSP forbids inline styles). */\n"
        return head + "\n".join(self.rules[k] for k in sorted(self.rules, key=lambda k: (k[:2], int(k[3:])))) + "\n"


AXIS_STEPS = (0.05, 0.1, 0.2, 0.25, 0.4, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0)


def nice_scale(max_abs: float) -> float:
    return next((s for s in AXIS_STEPS if s >= max_abs - EPS), AXIS_STEPS[-1])


def diverging_bar(weight: float, ref: float | None, scale: float, geo: Geometry,
                  short_scale: float | None = None) -> dict[str, Any]:
    """An HTML/CSS bar on one shared scale: long to the right (teal), short to the left (orange),
    a thin tick at the mechanical reference, the value label always at the bar's tip.

    `scale` is the long side of the axis, `short_scale` the short side (default: the same, so zero
    sits in the middle; 0 for a long-only book, so zero sits at the left edge and bars use the whole
    track). Widths and positions are shares of the whole track."""
    lo = scale if short_scale is None else max(0.0, short_scale)
    total = scale + lo
    zero = 100.0 * lo / total

    def pos(v: float) -> float:
        return zero + 100.0 * max(-lo, min(scale, v)) / total

    side = "long" if weight > EPS else "short" if weight < -EPS else "zero"
    width = 100.0 * min(abs(weight), scale if side != "short" else lo) / total
    out: dict[str, Any] = {
        "side": side, "w": geo.cls("width", width), "label": fmt_share(weight), "ref": None,
        "zero": geo.cls("left", zero),
        "start": geo.cls("right", 100.0 - zero) if side == "short" else geo.cls("left", zero),
        "at": geo.cls("left", pos(weight)),
    }
    if ref is not None and abs(ref) > EPS:       # a tick at zero would only thicken the zero line
        out["ref"] = geo.cls("left", pos(ref))
    return out


def axis_ticks(scale: float, short_scale: float, geo: Geometry) -> list[dict[str, str]]:
    """Five labels on the holdings axis; the in-between ones (t1, t3) hide on narrow phones."""
    total = scale + short_scale
    zero = 100.0 * short_scale / total
    if short_scale <= EPS:
        values = [0.0, scale / 4, scale / 2, 3 * scale / 4, scale]
    else:
        values = [-short_scale, -short_scale / 2, 0.0, scale / 2, scale]
    ticks = []
    for i, v in enumerate(values):
        edge = " edge-start" if i == 0 and short_scale <= EPS else ""
        ticks.append({"label": "0" if abs(v) < EPS else fmt_share(v), "cls": f"t{i}{edge}",
                      "at": geo.cls("left", zero + 100.0 * v / total)})
    return ticks


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
    ("decision", "Decision", "CODE", "human", "a proposal, or nothing to do"),
    ("human", "Human", "HUMAN", "human", "approves every order"),
)
MARKS = {"ok": "✓", "fallback": "!", "failed": "✗", "idle": "–", "waiting": "…"}


def _node(key: str, state: str, word: str, result: str, detail: str = "", title: str = "") -> dict[str, str]:
    spec = next(f for f in FLOW if f[0] == key)
    return {"key": key, "name": spec[1], "kind": spec[2], "accent": spec[3], "state": state,
            "mark": MARKS[state], "word": word, "result": result, "detail": detail,
            "title": title or f"{spec[1]}: {spec[4]}", "what": spec[4]}


def empty_flow(n_lines: int) -> list[dict[str, str]]:
    nodes = []
    for key, _name, _kind, _accent, what in FLOW:
        text = f"reads prices for {n_lines} lines" if key == "data" else what
        nodes.append(_node(key, "waiting", "not run yet", text))
    return nodes


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
    steady = [r for r in rows if not r["differs"]]

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

    # ---- the debate: three turns, each with a short excerpt, a stance and (bear) its verdicts in words
    bull_claims = {cl.claim_id: cl for cl in bull.claims} if bull else {}
    bear_verdicts = {r.claim_id: r for r in bear.rebuttals} if bear else {}
    turns = []
    for role, a, accent, key in (("Bull · opening", bull, "bull", "bull"), ("Bear · answer", bear, "bear", "bear"),
                                 ("Bull · reply", reply, "bull", "rebuttal")):
        if a is None:
            turns.append({"role": role, "accent": accent, "a": None})
            continue
        ch = change_list(a.proposal_levels, ref_levels)
        short, cut = excerpt(a.argument)
        verdict_words = ""
        disputed = []
        if key == "bear" and a.rebuttals:
            n_bull = len(bull_claims)
            of = f" of the bull's {plural(n_bull, 'point')}" if n_bull else (" point" if len(concede) == 1 else " points")
            verdict_words = f"Accepts {len(concede)}{of}; disputes {len(refute)}"
            for cid in refute[:2]:
                if cid in bull_claims:
                    disputed.append(excerpt(bull_claims[cid].text, 80)[0])
        turns.append({
            "role": role, "accent": accent, "a": a, "key": key, "summary": short, "cut": cut,
            "stance": stance(ch, lines, weights_for(ch)),
            "verdicts": bear_verdicts if key == "bull" else {},
            "verdict_words": verdict_words, "disputed": disputed,
            "more_disputed": max(0, len(refute) - len(disputed)),
        })
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
        "turns": turns, "reps": reps, "pm_same": pm_same, "pm_lead": pm_lead,
        "agreement": agreement, "agreement_rest": agreement_rest,
        "status_words": status_words, "why": [plain_why(w, lines) for w in c.why_we_met],
        "nodes": nodes, "summary": summary, "headline": headline, "control": control, "rows": rows,
        "changed_rows": changed_rows, "steady": steady, "general_holds": general_holds, "change_words": change_words,
        "checks_passed": passed, "checks_total": len(checks),
        "failed_checks": [ch for ch in checks if not ch.passed], "agreement_words": low_words,
        "council_changes": council_changes, "when": when, "n_lines": n_lines,
        "gross": sum(abs(v) for v in final_x.values()), "net": sum(final_x.values()),
        "shorthand": shorthand(debate_texts, lines),
        "skipped": [plain_skip(s, lines) for s in (c.plan.skipped if c.plan else [])],
    }


# ------------------------------------------------------------------------------ portfolio view
def build_portfolio(view: JournalView, lines: Lines, geo: Geometry, kill: dict[str, Any]) -> dict[str, Any] | None:
    """Holdings by line and the four stat tiles. None when there is nothing to show yet."""
    latest = view.cycles[0] if view.cycles else None
    if latest is None and view.book is None:
        return None
    doc = latest.doc if latest is not None else None
    weights: dict[str, float] = {}
    refs: dict[str, float] = {}
    trends: dict[str, str | None] = {}
    basis = ""
    gross = net = cash = None
    when = ""
    if doc is not None:
        refs = {k: r.weight_ref_x for k, r in doc.reference.items()}
        trends = {k: r.trend for k, r in doc.reference.items()}
    if latest is not None and latest.rehearsal:
        weights = dict(doc.risk.final_x) if doc is not None and doc.risk else {}
        basis = "target"
        when = fmt_when(latest.doc.slot)
    elif view.book is not None:
        weights = {k: b.weight_x for k, b in view.book.lines.items()}
        refs = {**refs, **{k: b.reference_weight_x for k, b in view.book.lines.items()
                           if b.reference_weight_x is not None}}
        gross, net, cash = view.book.gross_x, view.book.net_x, view.book.cash_x
        basis = "book"
        when = fmt_when(parse_cycle_id(view.book.as_of_cycle_id))
    elif doc is not None and doc.risk is not None:
        weights = dict(doc.risk.base_x)
        basis = "start"
        when = fmt_when(doc.slot)
    # one source for both pages: the run page's "Invested" is the same sum of |weights|
    gross = sum(abs(v) for v in weights.values()) if gross is None else gross
    net = sum(weights.values()) if net is None else net
    cash = max(0.0, 1.0 - gross) if cash is None else cash
    values = list(weights.values()) + list(refs.values())
    has_short = any(v < -EPS for v in values)
    scale = nice_scale(max([v for v in values if v > 0] + [0.05]))
    short_scale = nice_scale(max(-v for v in values if v < -EPS)) if has_short else 0.0

    per_line, _ = split_holds(doc.risk.hold_reasons if doc is not None and doc.risk else [])
    ref_levels = {k: r.level_ref for k, r in doc.reference.items()} if doc is not None else {}
    council = dict(doc.pm.levels) if doc is not None else {}
    units = unit_weights(doc) if doc is not None else {}
    raw_x = dict(doc.risk.raw_x) if doc is not None and doc.risk else {}
    medoid = next((r for r in doc.pm.replicates if r.replicate == doc.pm.medoid), None) if doc is not None else None
    dev_dir = {d.line: d.direction for d in medoid.deviations} if medoid else {}
    verbs = {"cut": "cut", "add": "added", "short": "went short", "cover": "covered", "lever": "levered up"}
    decided = "" if latest is None or latest.rehearsal else DECISION_NOTE.get(latest.final_state or "", "")

    groups = []
    flat = []
    keys = lines.sort(set(lines.info) | set(weights) | set(refs))
    for sleeve, label in (*SLEEVES, ("other", "Other")):
        members = [k for k in keys if (lines.info[k].sleeve if k in lines.info else "other") == sleeve]
        if not members:
            continue
        infos = [lines.info[k] for k in members if k in lines.info]
        note = ""
        if infos and all(not i.in_reference for i in infos):
            note = "Optional extras: the rules hold none; the council may add a small position when the trend allows."
        elif infos and all(not i.council for i in infos):
            note = ("Reference only: the council may not change these lines, because trading costs are too high; "
                    "they follow the rules.")
        rows = []
        for k in members:
            w = weights.get(k, 0.0)
            ref = refs.get(k)
            notes = []
            rl, cl = ref_levels.get(k), council.get(k)
            if rl is not None and cl is not None and abs(cl - rl) > EPS:
                verb = dev_dir.get(k) or verb_for(rl, cl).split(" ")[0]
                verb = verbs.get(verb, verb)
                wb = ref if ref is not None else (units[k] * rl if k in units else None)
                wa = raw_x.get(k, units[k] * cl if k in units else None)
                if wb is not None and wa is not None:
                    text = (f"Council {verb}: {fmt_share(wb)} → {fmt_share(wa)} of the portfolio "
                            f"(size {fmt_level(rl)} → {fmt_level(cl)})")
                else:
                    text = f"Council {verb}: size {fmt_level(rl)} → {fmt_level(cl)}"
                notes.append(text + (f", {decided}" if decided else ""))
            for h in per_line.get(k, []):
                notes.append(f"Held: {h['text']}")
            info = lines.info.get(k)
            marker = ("reference only" if info is not None and not info.council else
                      "council only" if info is not None and not info.in_reference else "")
            row = {
                "line": k, "name": lines.name(k), "trend": trends.get(k), "weight": w, "share": fmt_share(w),
                "ref": ref, "ref_share": fmt_share(ref), "notes": notes, "ref_level": rl, "council_level": cl,
                "bar": diverging_bar(w, ref, scale, geo, short_scale=short_scale),
                "marker": marker,
                "title": f"{lines.name(k)}: {fmt_share(w)} of the portfolio ({fmt_x(w)})"
                         + (f"; mechanical reference {fmt_share(ref)}" if ref is not None else ""),
            }
            rows.append(row)
            flat.append(row)
        if note:                       # the group note already says it for every row
            for row in rows:
                row["marker"] = ""
        groups.append({"key": sleeve, "label": label, "rows": rows, "note": note,
                       "share": fmt_share(sum(abs(weights.get(k, 0.0)) for k in members))})

    halt_dd = (1 - float(kill.get("halt_at", 0.75))) * 100
    warn_dd = (1 - float(kill.get("warn_at", 0.80))) * 100
    dd = None
    rehearsal = latest is not None and latest.rehearsal
    if not rehearsal:
        dd = next((p.drawdown_pct for p in reversed(view.performance) if p.drawdown_pct is not None), None)
    warn_label, halt_label = f"−{warn_dd:.0f}%", f"−{halt_dd:.0f}%"
    if dd is None:
        meter = {"value": "—", "fill": geo.cls("width", 0), "state": "none", "word": "",
                 "sub": "starts at go-live" if rehearsal else "no data yet",
                 "aria": "Fall from peak: not tracked until go-live" if rehearsal else "Fall from peak: no data yet"}
    else:
        depth = abs(min(dd, 0.0))
        state = "halted" if depth >= halt_dd - EPS else "warn" if depth >= warn_dd - EPS else "ok"
        value = f"−{depth:.1f}%" if depth >= 0.05 else "0%"
        meter = {"value": value, "fill": geo.cls("width", 100.0 * min(depth, halt_dd) / halt_dd), "state": state,
                 "word": {"ok": "normal", "warn": "no new risk", "halted": "stop"}[state],
                 "sub": "below the best value so far",
                 "aria": f"Fall from peak {value}; no new risk at {warn_label}, stop at {halt_label}"}
    meter.update({"warn_at": geo.cls("left", 100.0 * warn_dd / halt_dd), "warn_label": warn_label,
                  "halt_label": halt_label})
    tiles = {
        "invested": fmt_share(gross), "net": fmt_share(net), "cash": fmt_share(cash),
        "all_long": not any(v < -EPS for v in weights.values()),
        "held": sum(1 for v in weights.values() if abs(v) > EPS), "n_lines": len(keys), "meter": meter,
    }
    return {
        "basis": basis, "groups": groups, "rows": flat, "one_sided": not has_short, "has_short": has_short,
        "axis": axis_ticks(scale, short_scale, geo), "tiles": tiles, "when": when,
    }


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
        "<meta name=\"color-scheme\" content=\"light dark\">\n"
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
    env = make_env(lines)
    geo = Geometry()
    status = _status_context(view, now)
    disclaimer = load_disclaimer(policy_dir.parent)
    if status["prelive"]:
        disclaimer = prelive_disclaimer(disclaimer, rehearsal=status["mode"] == "rehearsal")
    common = {
        "csp": Markup(CSP),               # a constant; single quotes must not be entity-escaped
        "stale_script": Markup(STALE_SCRIPT),
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
    latest = view.cycles[0] if view.cycles else None
    ops_on_time = sum(1 for r in view.ops if r.status == "on_time")
    chart = performance_chart(view.performance)
    render("index.html.j2", "index.html", "", "portfolio", latest=latest,
           run=runs[latest.doc.cycle_id] if latest else None,
           portfolio=build_portfolio(view, lines, geo, risk.get("killswitch", {})),
           recent=[(cv, runs[cv.doc.cycle_id]) for cv in view.cycles[:5]], cycles_count=len(view.cycles),
           ops_count=len(view.ops), ops_on_time=ops_on_time,
           chart=chart, points=view.performance[-10:][::-1],
           controls=[sr for sr in CONTROL_SERIES if any(x["key"] == sr[0] for x in (chart or {}).get("series", []))])
    render("cycles.html.j2", "cycles.html", "", "runs", cycles=[(cv, runs[cv.doc.cycle_id]) for cv in view.cycles])
    for cv in view.cycles:
        render("cycle.html.j2", f"cycles/{cv.doc.cycle_id}.html", "../", "runs", cv=cv, c=cv.doc,
               run=runs[cv.doc.cycle_id])
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
