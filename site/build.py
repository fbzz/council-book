"""Static site for the public record, built from journal/, prompts/ and policy/ only.

Usage: uv run python site/build.py [--journal journal] [--prompts prompts] [--policy policy] [--out _site]

Pages: Portfolio (index.html: the book as a map of tiles sized by weight and a broker-style list,
then the council in speaking order with what each agent said last, then the latest runs) · Lines
(assets/<line>.html, one per line that ever appeared: position, weight across runs, trend, what the
agents said about it, its trades and its facts) · Runs (cycles.html and one page per run under
cycles/: a per-agent transcript in execution order) · Agents (agents/index.html and one page per
agent under agents/, the human operator included) · How it works (how.html) · Rules (rules.html) ·
Record (record.html).
The old names (council.html, book.html, failures.html) are tiny redirect pages.

Rules:
- Jinja2 autoescape is ON and undefined variables fail the build; model text is never rendered as
  HTML or markdown. There is no JavaScript at all: the holdings filter and the map's "Colour by"
  toggle are radio inputs and CSS.
- A strict Content-Security-Policy meta tag on every page; no external request, script or tracker.
  The typefaces are served from static/fonts (font-src 'self').
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
import math
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
from council.publish.paper import (
    PaperReveal,
    PublicPaperBookAfter,
    PublicPaperCycle,
    PublicPaperDecisionRow,
    PublicPaperLatest,
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
    PublicSwingBook,
    PublicSwingSection,
)
from council.publish.redact import _clip
from council.swing.rules import RULE_OF as SWING_RULE_CODES

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
DATA = HERE / "data"
EPS = 1e-9

# No script at all: the CSP forbids every script, inline or not.
CSP = (
    "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
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
    ("Data steward", "Builds the percentage-only fact pack from completed bars; freezes stale or closed markets.", "data"),
    ("Reference book", "The weight the rules alone would hold on each line: the default position, the centre of the "
                       "council's allowed range and the fallback.", "reference"),
    ("Event officer", "Blocks adds around scheduled macro events; never forces a sale.", "event"),
    ("Vol officer", "Writes volatility-shock cards and trips the volatility breaker.", "vol"),
    ("Cost desk", "Prices every leg (spread, fees, overnight carry) and runs the net-of-cost gate.", "costs"),
    ("Consistency auditor", "Reverts uncited or self-contradicting changes; discards broken PM replicates.", "audit"),
    ("Risk officer", "Final authority: enforces every rule in policy/risk.yaml and builds the order legs.", "risk"),
    ("Scribe", "Builds this public record from allow-listed fields only.", "neutral"),
    ("Scorekeeper", "Computes controls and card scores. Descriptive only.", "neutral"),
)
LLM_ROLES = {
    "news": ("News analyst", "Writes evidence cards from public RSS feeds and SEC filings, cited by id. Licensed text is never republished.", "ADVISES", "news"),
    "macro": ("Macro analyst", "Describes the macro regime and its drivers. Context only.", "CONTEXT", "macro"),
    "filings": ("Filings analyst", "Reads company filings (arrives with single stocks).", "ADVISES", "news"),
    "sector": ("Sector analyst", "Ranks names inside a peer group (arrives with single stocks).", "CONTEXT", "news"),
    "bull": ("Bull advocate", "Opens the debate, then answers the bear's rebuttal.", "ADVISES", "bull"),
    "bear": ("Bear advocate", "Rebuts the bull's specific claims, citing evidence.", "ADVISES", "bear"),
    "pm": ("Portfolio manager", "Proposes at most three changes to the reference, inside ranges that code enforces. Three independent attempts; the most typical one (the medoid) is used.", "DECIDES", "pm"),
    "single_agent_control": ("Single-agent control", "One agent, the same facts, no analysts and no debate. Published as a control; it never trades.", "CONTEXT", "control"),
}
# The swing book's roster (how.html): (name, what, authority, accent); replicates come from policy/swing.yaml.
SWING_LLM_ROLES = {
    "scout": ("Scout", "Reads public RSS feeds, SEC filings and a screen of the day's movers, and pitches swing ideas: a US "
                       "stock, long or short, held 3 to 15 sessions, citing each news item by its id.", "ADVISES", "scout"),
    "skeptic": ("Skeptic", "Blind to the pitch: sees only the ticker, the side, the cited items, a one-line claim and the "
                           "fact card, and answers pass, wait or reject on whether the news is already in the price.",
                "ADVISES", "skeptic"),
    "swing_bull": ("Bull · swing", "Argues for the trade from the fact card and the cited news.", "ADVISES", "bull"),
    "swing_bear": ("Bear · swing", "Rebuts the bull's claims one by one.", "ADVISES", "bear"),
    "swing_pm": ("Manager · swing", "Three independent attempts; an idea enters only if two of the three vote for it. "
                                    "Also sets the swing budget, 0-50% of the portfolio.", "DECIDES", "pm"),
}
SWING_CODE_ROLES = (
    ("Movers screen", "After each US close, lists unusual movers, volume spikes, unmoved filers and sector "
                      "laggards from completed daily bars: facts for the Scout, not picks.", "data"),
    ("Code gate", "Checks the ticker, builds the fact card from completed daily bars and drops ideas that chase a "
                  "move or whose reward against risk is too thin after cost.", "risk"),
    ("Swing risk officer", "Enforces every swing rule (S1-S18): size, stops, targets, earnings, shorts, the loss brake "
                           "and the drawdown scaling.", "risk"),
    ("Paper tracker", "Tracks every idea, entered or not, the same way on paper, net of the declared cost.", "neutral"),
)
ROSTER_AGENT = {"news": "news", "macro": "macro", "bull": "bull", "bear": "bear", "pm": "pm",
                "single_agent_control": "control"}
PROMPT_ROLE_ALIASES = {"bull_open": "bull", "bull_rebuttal": "bull", "single_agent": "single_agent_control"}

RULE_TITLES = {
    "gross": "Gross exposure caps",
    "net": "Net exposure range and short gross cap",
    "killswitch": "Soft kill: warn, then halt, as a fraction of the lifetime peak",
    "catastrophe_stop": "Every open carries a catastrophe stop-loss",
    "reentry_cooloff_days": "Cool-off before re-entering after a stop hit",
    "caps": "Per-line and cluster caps",
    "leverage_caps": "Leverage caps by asset class",
    "margin_use_max": "Margin use cap (keeps a cash reserve)",
    "ex_ante_vol_hard": "Ex-ante book volatility hard cap",
    "vol_breaker": "Volatility breaker",
    "authority": "Council authority bands around the reference",
    "deadband": "Deadband: changes too small to be worth trading are skipped",
    "min_hold_days": "Minimum holding periods",
    "churn": "Turnover limits",
    "cost_budget": "Cost and carry budgets",
    "net_of_cost_gate": "Net-of-cost gate",
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
# Plan skips publish collapsed (M5-N): a size skip is `R11`, anything unexplained `not_ordered`.
# `below_broker_minimum` stays for journals sealed before the collapse.
SKIP_WORDS = {"R11": "too small to trade", "not_ordered": "not ordered",
              "below_broker_minimum": "below the broker's minimum order size",
              "swing_book_not_live": "the swing book is paper-only for now"}
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
    swing: PublicSwingBook | None = None                     # journal/swing/latest.json (swing-book §7.4)
    # journal/paper/: the paper runs' public record (numbered decisions, the paper portfolio)
    paper_rows: list[PublicPaperDecisionRow] = field(default_factory=list)
    paper_cycles: dict[int, PublicPaperCycle] = field(default_factory=dict)
    paper_verified: dict[int, bool] = field(default_factory=dict)
    paper_latest: PublicPaperLatest | None = None
    paper_books: dict[int, PublicPaperBookAfter] = field(default_factory=dict)   # journal/paper/books/<n>.json

    @property
    def has_swing(self) -> bool:
        return (self.swing is not None or any(cv.doc.swing is not None for cv in self.cycles)
                or any(d.swing is not None for d in self.paper_cycles.values()))


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
        executions[execution.key] = (execution, rel)   # a flatten or smoke file never matches a cycle
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
    swing_file = journal_dir / "swing" / "latest.json"
    if swing_file.exists():
        view.swing = PublicSwingBook.model_validate_json(swing_file.read_text())
        view.copies["journal/swing/latest.json"] = swing_file
    load_paper(journal_dir, view)
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


def load_paper(journal_dir: Path, view: JournalView) -> None:
    """journal/paper/: decisions.jsonl (numbered, append-only), each revealed paper cycle (the exact
    sealed bytes + its salt, verified against the row's commitment) and latest.json."""
    paper = journal_dir / "paper"
    if not paper.exists():
        return
    view.paper_rows = sorted((PublicPaperDecisionRow.model_validate(r) for r in _jsonl(paper / "decisions.jsonl")),
                             key=lambda r: r.decision_no)
    if (paper / "decisions.jsonl").exists():
        view.copies["journal/paper/decisions.jsonl"] = paper / "decisions.jsonl"
    for row in view.paper_rows:
        file = journal_dir.parent / row.path if (journal_dir.parent / row.path).exists() else None
        if file is None:
            continue
        data = file.read_bytes()
        doc = PublicPaperCycle.model_validate_json(data)
        if doc.decision_no != row.decision_no or doc.cycle_id != row.cycle_id:
            raise SiteBuildError(f"paper decision #{row.decision_no} does not match its file")
        view.paper_cycles[row.decision_no] = doc
        view.copies[row.path] = file
        reveal = file.with_name(file.name[: -len(".json")] + ".reveal.json")
        ok = False
        if reveal.exists():
            rv = PaperReveal.model_validate_json(reveal.read_text())
            ok = rv.commitment_sha256 == row.commitment_sha256 and commit_reveal.verify_bytes(
                data, rv.salt, row.commitment_sha256)
            view.copies[row.path[: -len(".json")] + ".reveal.json"] = reveal
        view.paper_verified[row.decision_no] = ok
    if (paper / "latest.json").exists():
        view.paper_latest = PublicPaperLatest.model_validate_json((paper / "latest.json").read_text())
        view.copies["journal/paper/latest.json"] = paper / "latest.json"
    for f in sorted((paper / "books").glob("*.json")) if (paper / "books").is_dir() else []:
        b = PublicPaperBookAfter.model_validate_json(f.read_text())
        if f.stem != str(b.decision_no):
            raise SiteBuildError(f"paper book {f.name} does not match its decision number")
        view.paper_books[b.decision_no] = b
        view.copies[f"journal/paper/books/{f.name}"] = f


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
        name, what, authority, accent = LLM_ROLES.get(role, (role.replace("_", " ").title(), "", "ADVISES", "news"))
        llm.append({
            "role": role, "name": name, "what": what, "authority": authority, "accent": accent,
            "enabled": bool(cfg.get("enabled", True)), "replicates": int(cfg.get("replicates", 1)),
            "cadence": CADENCE_WORDS.get(str(cfg.get("cadence", "every_cycle")),
                                         str(cfg.get("cadence", "every_cycle")).replace("_", " ")),
            "prompts": manifest.get(role, []),
            "page": f"agents/{ROSTER_AGENT[role]}.html" if role in ROSTER_AGENT else "",
        })
    code = [{"name": n, "what": w, "accent": a} for n, w, a in CODE_ROLES]
    swing_file = policy_dir / "swing.yaml"
    sw_llm = ((yaml.safe_load(swing_file.read_text()) or {}) if swing_file.exists() else {}).get("llm") or {}
    swing_reps = {"skeptic": f"up to {sw_llm.get('max_skeptic_calls', 1)}",
                  "swing_pm": str(sw_llm.get("pm_replicates", 3))}
    swing = [{"role": role, "name": name, "what": what, "authority": authority, "accent": accent,
              "replicates": swing_reps.get(role, "1"), "prompts": manifest.get(role, []),
              "model": str(sw_llm.get("skeptic_model") or council.get("model", "")) if role == "skeptic"
              else str(council.get("model", ""))}
             for role, (name, what, authority, accent) in SWING_LLM_ROLES.items()]
    swing_code = [{"name": n, "what": w, "accent": a} for n, w, a in SWING_CODE_ROLES]
    return {
        "code": code, "llm": llm, "swing": swing, "swing_code": swing_code,
        "pm_entry_votes": sw_llm.get("pm_entry_votes", 2), "model": str(council.get("model", "")),
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
            return (f"All positions added together (long and short) stay under {_p(v['proposal_max'])} of the portfolio; "
                    f"code refuses anything above {_p(v['hard_max'])}.")
        if key == "net":
            return (f"Long minus short stays between {_p(v['min'])} and {_p(v['max'])} of the portfolio; "
                    f"shorts together at most {_p(v['short_gross_max'])}.")
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
            return (f"Changes smaller than {_p(v['level'])} of a line's full size, or under {_share0(v['min_nav_share'])} "
                    "of the portfolio, are not traded.")
        if key == "min_hold_days":
            return (f"A position is kept at least {v['default']} days ({v['crypto']} for crypto) before it is reversed; "
                    "moving back to the reference is always allowed.")
        if key == "churn":
            return (f"Trading is limited to {_p(v['turnover_7d_max'])} of the portfolio in 7 days and "
                    f"{_p(v['turnover_30d_max'])} in 30 days.")
        if key == "cost_budget":
            return (f"One run may spend at most {_bp(v['cycle_max_bps'])} of the portfolio on trading costs, and "
                    f"{_bp(v['discretionary_30d_max_bps'])} over 30 days on discretionary trades.")
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
    vehicle: str | None = None      # the policy's preferred long vehicle: "real" or "cfd"


LINE_ID = re.compile(r"[A-Z0-9](?:[A-Z0-9_]{0,10}[A-Z0-9])?")    # the public line pattern (BRK_B, V)


def policy_vehicle(raw: dict[str, Any]) -> str | None:
    """The settlement of a policy line's first long vehicle candidate ("real" or "cfd")."""
    longs = ((raw.get("vehicles") or {}).get("long") or [])
    first = longs[0] if longs and isinstance(longs[0], dict) else {}
    value = str(first.get("settlement") or "")
    return value if value in ("real", "cfd") else None


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
                asset_class=raw.get("asset_class"), session=policy_session(raw), vehicle=policy_vehicle(raw),
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
                    session=known.session or b.session, vehicle=known.vehicle,
                )

    def seen(self, keys: Any) -> None:
        """Add the lines a run describes that neither the policy nor the book knows (a line that
        left the book): named by their ticker, after the policy's lines. Only public line ids."""
        for sym in keys:
            if sym not in self.info and LINE_ID.fullmatch(str(sym)) and not str(sym).startswith("UNMAPPED"):
                self.info[sym] = LineInfo(symbol=sym, name=ticker(sym), sleeve="other", in_reference=True,
                                          council=True)

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
    Values are percentages of the containing track, rounded to 0.01%. The book map's tiles get one
    class each, with a desktop and a phone rectangle (two squarified layouts)."""

    PROPS = {"width": "gw", "left": "gl", "right": "gr", "top": "gt", "height": "gh"}
    PHONE = "(max-width: 699px)"

    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self.tiles: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}

    def tile(self, key: str, desk: tuple[float, float, float, float], phone: tuple[float, float, float, float],
             prefix: str = "tm-") -> str:
        """A book-map box's class (a tile "tm-", a class group "tgrp-"): left, top, width, height in %
        of its parent, per layout."""
        name = prefix + re.sub(r"[^A-Za-z0-9_]", "", key)
        self.tiles[name] = (desk, phone)
        return name

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

    @staticmethod
    def _rect(name: str, r: tuple[float, ...]) -> str:
        left, top, width, height = (max(0.0, min(100.0, v)) for v in r)
        return f".{name} {{ left: {left:.2f}%; top: {top:.2f}%; width: {width:.2f}%; height: {height:.2f}%; }}"

    def css(self) -> str:
        head = ("/* Generated by site/build.py: bar widths, tick positions, ring fills and the book map's "
                "tiles (the CSP forbids inline styles). */\n")
        out = head + "\n".join(self.rules[k] for k in sorted(self.rules, key=lambda k: (k[:2], int(k[3:])))) + "\n"
        if self.tiles:
            names = sorted(self.tiles)
            out += "\n".join(self._rect(n, self.tiles[n][0]) for n in names) + "\n"
            out += f"@media {self.PHONE} {{\n" + "\n".join("  " + self._rect(n, self.tiles[n][1]) for n in names) + "\n}\n"
        return out


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
CARD_AUTHOR = {"vol": "vol", "event": "event", "news": "news", "macro": "macro"}   # K:<role>:n -> the agent


def evidence_label(ref: Any, lines: Lines) -> dict[str, str]:
    """A plain label for an evidence reference; `raw` is the id, kept in a title attribute."""
    kind = getattr(ref, "kind", "")
    if kind == "broker_feed":
        return {"label": "news item", "raw": f"{ref.id} · a public RSS feed or SEC filing item, cited by id; licensed text is not republished",
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
        # a card chip takes the colour of the agent that wrote it (ev-by-vol, ev-by-news, ...)
        by = CARD_AUTHOR.get(parts[1], "")
        return {"label": f"{CARD_ROLES.get(parts[1], parts[1] + ' card')} {parts[2]}", "raw": rid,
                "css": "card" + (f" ev-by-{by}" if by else "")}
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
    ("market", "Market", "data"), ("vol", "Volatility", "vol"), ("cost", "Costs", "costs"),
    ("macro", "Macro (FRED)", "macro"), ("event", "Scheduled events", "event"),
    ("news", "Broker news items (ids only)", "news"), ("filing", "Company filings (ids only)", "news"),
    ("fundamental", "Fundamentals", "data"),
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
    "broker_feed": "broker feed", "rss": "licensed RSS headline", "filing": "filing", "unknown": "unknown",
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

    def __init__(self, doc: PublicCycleV1, lines: Lines, base: str = "", asset_root: str = "../"):
        self.lines = lines
        self.base = base
        self.asset_root = asset_root          # every page that shows chips sits one folder deep
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
        out = {**base, "value": "", "note": "", "href": self.href(rid), "line": "", "line_label": "", "what": "",
               "asset_href": ""}
        parts = rid.split(":")
        if parts[0] in ("F", "V", "C") and len(parts) >= 3 and parts[1] in self.lines.info and asset_page(parts[1]):
            name = self.lines.name(parts[1])
            if base["label"].startswith(name + " · "):
                out.update(line=parts[1], line_label=name, what=base["label"][len(name) + 3:],
                           asset_href=self.asset_root + asset_page(parts[1]))
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
            "line_id": f.line or "",
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
    short: str = ""                  # the roster's one line
    phase: str = ""                  # the home page's phase of a run (PHASES)


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("data", "Data steward", "CODE", "data", "Code officers",
              "Builds the percentage-only fact pack from completed daily bars and freezes stale or closed markets.",
              source="src/council/facts/pack.py",
              more="Every fact carries the time it became available; a run may only use facts available at its "
                   "slot start, and only completed bars. A lookahead test checks it.",
              short="Turns completed daily bars into a percentage-only fact pack.", phase="read"),
    AgentSpec("reference", "Reference book", "CODE", "reference", "Code officers",
              "Computes the weight the rules alone would hold on each line: the default, the benchmark and the fallback.",
              source="src/council/reference/book.py",
              more="A fixed base weight per line, scaled by its trend and trimmed when the line is unusually "
                   "volatile. The book is long-only and never levered.",
              short="Sets the weight the rules alone would hold on each line.", phase="read"),
    AgentSpec("vol", "Volatility officer", "CODE", "vol", "Code officers",
              "Writes a volatility-shock card when a line's short-term volatility jumps, and trips the breaker.",
              source="src/council/deliberation/officers.py",
              more="A volatility card is one of the two kinds of card that allow the council to cut a line in an "
                   "uptrend.",
              short="Flags volatility shocks and trips the breaker.", phase="read"),
    AgentSpec("event", "Event officer", "CODE", "event", "Code officers",
              "Writes event cards for scheduled macro releases and blocks adds in the window around them.",
              source="src/council/deliberation/officers.py",
              more="It never forces a sale: selling is always allowed inside an event window.",
              short="Blocks adds around scheduled macro releases.", phase="read"),
    AgentSpec("news", "News analyst", "LLM", "news", "Analysts",
              "Reads public RSS feeds and SEC filings and writes evidence cards that cite them by id.",
              roles=("news",), source="prompts/news.md",
              more="The news text itself is licensed and never republished: a card cites a feed item by its id "
                   "only, and the analyst's own short paraphrase is shown.",
              short="Turns public RSS feeds and SEC filings, cited by id, into evidence cards.", phase="evidence"),
    AgentSpec("macro", "Macro analyst", "LLM", "macro", "Analysts",
              "Describes the macro regime and its drivers from public macro data; context only.",
              roles=("macro",), source="prompts/macro.md",
              more="It runs on the first run of each UTC day. Code never acts on its regime or tilts; the "
                   "advocates and the manager may cite it.",
              short="Describes the macro regime. Context only.", phase="evidence"),
    AgentSpec("bull", "Bull", "LLM", "bull", "Debate",
              "Opens the debate with a case for a set of positions, then answers the bear.",
              roles=("bull_open", "bull_rebuttal"), source="prompts/bull_open.md",
              more="Two turns per run: the opening (claims the bear must answer) and the rebuttal (after the "
                   "bear). It has no authority; the manager decides.",
              short="Makes the case for a set of positions, then answers the bear.", phase="debate"),
    AgentSpec("bear", "Bear", "LLM", "bear", "Debate",
              "Answers the bull's claims one by one, conceding or contesting each, and argues its own case.",
              roles=("bear",), source="prompts/bear.md",
              more="It must answer the bull's specific claims by their ids, with evidence. It has no authority.",
              short="Answers the bull claim by claim and argues its own case.", phase="debate"),
    AgentSpec("pm", "Portfolio manager", "LLM", "pm", "Decision",
              "Makes three separate attempts at a decision of at most three changes; the most typical one is used.",
              roles=("pm",), source="prompts/pm.md",
              more="Each attempt may move at most three lines, inside ranges that code sets. The attempt closest "
                   "to the others (the medoid) is used: a real decision, never an average.",
              short="Decides within limits: three attempts, the most typical is used.", phase="decide"),
    AgentSpec("control", "Single-agent control", "LLM", "control", "Decision",
              "One agent with the same facts, no analysts and no debate, as a comparison; it never trades.",
              roles=("single_agent",), source="prompts/single_agent.md",
              more="Published so readers can see what the council's analysts and debate change.",
              short="One agent, no debate: a comparison that never trades.", phase="decide"),
    AgentSpec("audit", "Auditor and bands", "CODE", "audit", "Checks",
              "Reverts uncited or contradictory changes, discards broken attempts and clips levels into their range.",
              source="src/council/deliberation/audit.py",
              more="The allowed range (band) of each line comes from its trend; the auditor reverts a change that "
                   "cites evidence the pack does not hold.",
              short="Reverts uncited changes and clips each line into its range.", phase="check"),
    AgentSpec("risk", "Risk engine", "CODE", "risk", "Checks",
              "Checks every limit in the risk policy and can hold a change back; it has the final word.",
              source="src/council/risk/engine.py",
              more="Gross and net exposure, caps, margin, volatility breakers, deadband, minimum holds, churn, "
                   "costs and the kill switch. Rules live in code, not in prompts.",
              short="Checks every limit in the risk policy and has the final word.", phase="check"),
    AgentSpec("costs", "Cost desk and plan", "CODE", "costs", "Checks",
              "Prices every order (spread, fees, overnight carry) and turns the decision into order legs.",
              source="src/council/execution/planner.py",
              more="Costs are shown in basis points of the portfolio (1 bp = 0.01%), never as amounts.",
              short="Prices every order and turns the decision into a plan.", phase="check"),
    AgentSpec("human", "Human operator", "HUMAN", "human", "Approval",
              "Approves or rejects every order in a separate operator terminal; nothing trades on its own.",
              source="src/council/operator/approve.py",
              more="The unattended runner can only read. Orders are approved in a separate operator terminal that "
                   "refuses to run under automation or an agent and asks for a typed confirmation code.",
              short="Approves or rejects every order. Nothing trades on its own.", phase="approve"),
)
AGENT_BY_SLUG = {a.slug: a for a in AGENT_SPECS}
# The phases of a run, in speaking order (the home page's council and the agents index).
PHASES = (
    ("read", "Read the market", "Code turns prices, calendars and volatility into facts and a mechanical reference book."),
    ("evidence", "Gather evidence", "Two language models turn news and macro data into evidence cards that must cite the facts."),
    ("debate", "Debate", "A bull and a bear argue over the book, claim by claim, citing evidence."),
    ("decide", "Decide", "The manager decides within ranges set by code; a lone agent answers the same question as a control."),
    ("check", "Check", "Code audits the decision, checks every risk limit and prices each order."),
    ("approve", "Approve", "A person approves or rejects every order."),
)
ROLE_AGENT = {r: a.slug for a in AGENT_SPECS for r in a.roles}
ROLE_WORDS = {"bull_open": "opening", "bull_rebuttal": "rebuttal", "single_agent": "control"}
# call role -> (the agent's name as the page says it, its accent, its run-page anchor)
CALL_AGENT = {
    "news": ("News analyst", "news", "a-news"), "macro": ("Macro analyst", "macro", "a-macro"),
    "bull_open": ("Bull · opening", "bull", "a-bull"), "bear": ("Bear", "bear", "a-bear"),
    "bull_rebuttal": ("Bull · rebuttal", "bull", "a-rebuttal"), "pm": ("Portfolio manager", "pm", "a-pm"),
    "single_agent": ("Control", "control", "a-control"),
    # the swing book's roles (swing-book §7.4): their run-page section is "Swing ideas"
    "scout": ("Scout", "scout", "swing-ideas"), "skeptic": ("Skeptic", "skeptic", "swing-ideas"),
    "swing_bull": ("Bull · swing", "bull", "swing-ideas"), "swing_bear": ("Bear · swing", "bear", "swing-ideas"),
    "swing_pm": ("Manager · swing", "pm", "swing-ideas"),
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
    name, accent, anchor = CALL_AGENT.get(call.role, (call.role.replace("_", " ").capitalize(), "neutral", ""))
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
    """A skipped-leg code in words: "GOLD: R11" -> "Gold: too small to trade"."""
    m = re.match(r"^([A-Za-z0-9_.]+): ([A-Za-z0-9_ ]+?)\s*(?:\(.*\))?$", code)
    if not m:
        return {"text": SKIP_WORDS.get(code, code.replace("_", " ")), "raw": code}
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


def change_parts(changes: dict[str, tuple[float, float]], lines: Lines, weights: Weights) -> list[dict[str, Any]]:
    """Changes as structured parts, so a page can link each line to its page: [{line, verb, name,
    from, to, size}] in universe order. `from`/`to` are portfolio shares, else sizes."""
    out = []
    for k in lines.sort(changes):
        before, after = changes[k]
        verb = verb_for(before, after)
        if k in weights:
            wb, wa = (fmt_share(v) for v in weights[k])
            out.append({"line": k, "verb": verb, "short_verb": verb.replace("add to", "add"), "name": lines.name(k),
                        "from": wb, "to": wa, "size": False})
        else:
            out.append({"line": k, "verb": verb, "short_verb": verb.replace("add to", "add"), "name": lines.name(k),
                        "from": f"size {fmt_level(before)}", "to": fmt_level(after), "size": True})
    return out


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
    ("data", "Data", "CODE", "data", "reads prices for every line"),
    ("officers", "Officers", "CODE", "vol", "flag volatility shocks and scheduled events"),
    ("analysts", "Analysts", "LLM", "news", "turn news into cited evidence cards"),
    ("bull", "Bull", "LLM", "bull", "makes the case for a set of positions (it may still cut a line)"),
    ("bear", "Bear", "LLM", "bear", "attacks the bull's case, claim by claim"),
    ("pm", "Portfolio manager ×3", "LLM", "pm", "decides, within limits: three separate attempts, the most typical is used"),
    ("risk", "Risk engine", "CODE", "risk", "code that checks every limit and can hold a change back"),
    ("decision", "Plan & costs", "CODE", "costs", "prices each order and builds the proposal, or finds nothing to do"),
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

    def seg(lead: str, a: PublicAdvocate | None) -> dict[str, Any]:
        """One stance as a segment: plain words and the changes as parts (linked on the page)."""
        if a is None:
            return {"lead": lead, "parts": [], "plain": "no valid answer", "text": f"{lead}no valid answer"}
        ch = change_list(a.proposal_levels, ref_levels)
        text = stance(ch, lines, weights_for(ch))
        text = text[:1].lower() + text[1:]
        return {"lead": lead, "parts": change_parts(ch, lines, weights_for(ch)), "plain": "" if ch else text,
                "text": lead + text}

    def step(who: str, accent: str, anchor: str, segs: list[dict[str, Any]], tail: str = "") -> dict[str, Any]:
        return {"who": who, "accent": accent, "anchor": anchor, "segs": segs, "tail": tail,
                "text": " → ".join(s["text"] for s in segs) + tail}

    # the bull speaks twice: when its rebuttal asks for something else than its opening, both are named
    bull_open_ch = change_list(bull.proposal_levels, ref_levels) if bull is not None else None
    reply_ch = change_list(reply.proposal_levels, ref_levels) if reply is not None else None
    if reply is not None and bull is not None and reply_ch != bull_open_ch:
        bull_segs = [seg("opened: ", bull), seg("rebuttal: ", reply)]
        bull_anchor = "a-rebuttal"
    else:
        bull_segs = [seg("asked to ", reply if reply is not None else bull)]
        bull_anchor = "a-rebuttal" if reply is not None else "a-bull"
    steps = [step("Bull", "bull", bull_anchor, bull_segs), step("Bear", "bear", "a-bear", [seg("asked to ", bear)])]
    used = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
    total = len(c.pm.replicates)
    if not c.pm.replicates or c.pm.valid_replicates == 0 or used is None:
        pm_segs = [{"lead": "", "parts": [], "plain": "no usable answer: the reference applied",
                    "text": "no usable answer: the reference applied"}]
        pm_tail = ""
    else:
        decided = stance(council_changes, lines, council_w)
        decided = decided[:1].lower() + decided[1:]
        same = sum(1 for r in c.pm.replicates if r.valid and r.sided_with == used.sided_with)
        side = SIDED_WORDS.get(used.sided_with or "", "")
        pm_segs = [{"lead": "decided to ", "parts": change_parts(council_changes, lines, council_w),
                    "plain": "" if council_changes else decided, "text": "decided to " + decided}]
        pm_tail = f" (sided with {side}, {same} of {total} attempts)" if side else ""
    steps.append(step("Manager", "pm", "a-pm", pm_segs, pm_tail))
    def plain(text: str) -> list[dict[str, Any]]:
        return [{"lead": "", "parts": [], "plain": text, "text": text}]

    if c.risk is None:
        risk_step = step("Risk engine", "risk", "a-risk", plain("did not run"))
    else:
        risk_step = step("Risk engine", "risk", "a-risk", plain(f"{passed}/{n_checks} checks passed"))
        if held_lines:
            risk_step["text"] += ", held back " + join_words([r["name"] for r in held_lines])
    risk_step["held"] = [r["line"] for r in held_lines] if c.risk is not None else []
    steps.append(risk_step)
    legs = len(c.plan.legs) if c.plan else 0
    if cv.rehearsal:
        human = "not needed: rehearsal"
    elif cv.final_state in HUMAN_WORDS and (legs or cv.final_state not in ("proposed", "awaiting_publication")):
        human = HUMAN_WORDS[cv.final_state]
    else:
        human = "nothing to approve"
    steps.append(step("Human", "human", "a-decision", plain(human)))
    for s in steps:
        s.setdefault("held", [])
    return steps



# Redaction markers written by publish/redact.py ("[value removed]", "[figure withheld]", …) read as
# one styled "redacted" chip on every page (text nodes only, never inside a tag or attribute).
_REDACTION_MARKER = re.compile(r"\[(?:value removed|figure withheld|level removed|amount removed|money removed|"
                               r"withheld: overlaps licensed feed text)\]")
_REDACTED_CHIP = ('<span class="redacted" title="Redacted: a licensed market-data figure or quoted licensed '
                  'text the public record may not carry">redacted</span>')


def redacted_chips(html: str) -> str:
    parts = re.split(r"(<[^>]+>)", html)
    return "".join(p if p.startswith("<") else _REDACTION_MARKER.sub(_REDACTED_CHIP, p) for p in parts)

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
        detail = ""
        if reply is not None:
            reply_ch_ = change_list(reply.proposal_levels, ref_levels)
            then_ = (phrase_changes(reply_ch_, lines, compact=True, weights=weights_for(reply_ch_))
                     if reply_ch_ else "holds the reference")
            detail = (f"rebuttal: {then_}, conceding {len(reply.concessions)}" if reply_ch_ != bull_ch
                      else f"answered the bear, conceding {len(reply.concessions)}")
        bull_node = _node("bull", "ok", "ok", f"opening: {want} · {plural(len(bull.claims), 'claim')}"
                          if reply is not None else f"{want} · {plural(len(bull.claims), 'claim')}", detail)
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

    reply_ch = change_list(reply.proposal_levels, ref_levels) if reply else None
    then = (f", then, answering the bear, to {wants(reply_ch)}"
            if reply_ch is not None and bull_ch is not None and reply_ch != bull_ch else "")
    if bull_ch is not None and bear_ch is not None and bull_ch == bear_ch and not then:
        debate = f"The bull and the bear both argued to {wants(bull_ch)}"
    elif bull_ch is not None and bear_ch is not None:
        debate = f"The bull argued to {wants(bull_ch)}{then}; the bear to {wants(bear_ch)}"
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
    if then:
        s2 = f"{debate}. {pm_s[:1].upper()}{pm_s[1:]}."
    else:
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
        # the lines a card is about, as ids (linked on the page) or, for a non-line scope, words
        "scope": [{"line": s, "label": lines.name(s)} if s in lines.info
                  else {"line": "", "label": lines.name(s) if LINE_ID.fullmatch(s) else s} for s in k.scope],
        "chips": [fx.chip(r) for r in k.evidence],
        "corroborated": [{"label": evidence_label(SimpleNamespace(kind="card", id=x), lines)["label"],
                          "href": fx.href(x)} for x in k.corroborated_by],
        "cited_by": [name for slug, name in cited.get(k.card_id, []) if slug not in (k.role,)],
    }


AGENT_SHORT = {"a-news": "news", "a-macro": "macro", "a-bull": "bull", "a-bear": "bear", "a-rebuttal": "bull reply",
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
        "stance_parts": change_parts(ch, lines, weights_for(ch)),
        "did_parts": change_parts(used_ch, lines, weights_for(used_ch)),
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
            amount = f"from {fmt_share(w[d.line][0])} to {fmt_share(w[d.line][1])}"
            size = f"size {fmt_level(rl)} → {fmt_level(d.level)}"
        else:
            amount = f"to size {fmt_level(d.level)}"
            size = ""
        changes.append({"words": f"{d.direction} {lines.name(d.line)} {amount}", "line": d.line, "amount": amount,
                        "size": size, "reason": d.reason, "chips": [fx.chip(e) for e in d.evidence],
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
    shocked = [s["label"] for k in vol_cards for s in k["scope"]]
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
              "decision_parts": change_parts(council_changes, lines, weights_for(council_changes, raw_x)),
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
                differs.append({"line": k, "name": lines.name(k), "council": fmt_level(council_levels[k]),
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
        band_rows.append({"line": k, "name": lines.name(k), "ticker": ticker(k), "trend": b.trend,
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
        body={"legs": [{"leg": leg, "why": why_leg[leg.seq],
                        "order": order_view(leg.kind, leg.direction, leg.weight_before_x, leg.weight_after_x)}
                       for leg in legs],
              "why_leg": why_leg,
              "order_by_seq": {leg.seq: order_view(leg.kind, leg.direction, leg.weight_before_x, leg.weight_after_x)
                               for leg in legs},
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
            "ac_words": words, "group": AC_GROUP.get(ac or "", "other"), "page": asset_page(k),
            "session": chip_[0] if chip_ else "", "session_title": chip_[1] if chip_ else ""}


def asset_page(k: str) -> str:
    """A line's page, relative to the site root (line ids are [A-Z0-9_] only)."""
    return f"assets/{k}.html" if LINE_ID.fullmatch(k) else ""


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
                         "bar": {"side": "zero", "w": "", "ref": None}, "notes": [], "council_moved": False,
                         "title": f"{lines.name(k)}: not held yet"})
        filters, empty_flat, empty_held = _filters([], flat)
        return {"basis": "skeleton", "label": "", "when": "", "run_id": "", "held": [], "flat": flat,
                "filters": filters, "empty_flat": empty_flat, "empty_held": empty_held, "strip": None,
                "scale": "", "target": True, "show_day": False, "show_pnl": False, "cash_x": None}
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
        council_moved = rl is not None and cl is not None and abs(cl - rl) > EPS
        if council_moved:
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
            "bar": weight_bar(w, ref, scale, geo), "notes": notes, "council_moved": council_moved,
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
        "cash_x": cash,
    }


# ------------------------------------------------------------------------------ the book map (home)
# A two-level squarified treemap: the asset classes first, then the lines inside each class, in
# reference boxes of the map's two aspect ratios (desktop, phone). Positions are percentages of the
# parent box; what a tile shows at its real size is decided by CSS container queries, so the labels
# fit at every width (everything is also in the list below).
MAP_DESK = (960.0, 400.0)      # aspect 12:5, from 700 px wide
MAP_PHONE = (358.0, 448.0)     # aspect 4:5, below 700 px
GROUP_HEAD = 20.0              # a class group's header strip, px (it does not scale)

# "Colour by": the map's three colourings, switched by radio inputs and CSS (no script). The two
# moves share one diverging binned scale, coral (down) - neutral grey - teal (up), three steps a
# side; the edges are |x| in percent, each bin a half-open interval [edge, next edge).
DAY_EDGES = (0.25, 1.0, 2.5)   # the instrument's last daily market move
PNL_EDGES = (1.0, 5.0, 15.0)   # the position's own P/L since open
BIN_ORDER = ("d3", "d2", "d1", "n", "u1", "u2", "u3")
MAP_CLASS_WORD = {"stock": "Stock", "etf": "ETF", "index": "Index", "crypto": "Crypto", "commodity": "Commodity",
                  "fx": "FX"}
MAP_CLASS_ORDER = {key: i for i, (key, *_rest) in enumerate(FILTER_GROUPS)}


def move_bin(v: float | None, edges: tuple[float, float, float], digits: int = 2) -> str:
    """A signed percent's bin on the diverging scale: "n" (neutral), "u1".."u3" up, "d1".."d3" down,
    "nd" when unknown. The value is binned as printed (rounded to `digits`), so the colour never
    disagrees with the number on the tile."""
    if v is None:
        return "nd"
    r = round(v, digits)
    step = sum(1 for e in edges if abs(r) >= e)
    return "n" if step == 0 else ("u" if r > 0 else "d") + str(step)


def bin_legend(edges: tuple[float, float, float], down: str, up: str) -> list[dict[str, Any]]:
    """The seven bins of a diverging legend, left (most down) to right (most up): the bin, its tick
    and its range in words (for screen readers and the swatch's title). The outer ticks sit on the
    bins' edges; the grey midpoint gets one centred "±a%" rather than two edge ticks one narrow bin
    apart, which collide on a phone."""
    a, b, c = (f"{e:g}" for e in edges)
    text = {"d3": f"{down} {c}% or more", "d2": f"{down} {b}% to {c}%", "d1": f"{down} {a}% to {b}%",
            "n": f"within ±{a}%", "u1": f"{up} {a}% to {b}%", "u2": f"{up} {b}% to {c}%", "u3": f"{up} {c}% or more"}
    ticks = {"d3": f"−{c}%", "d2": f"−{b}%", "d1": "", "n": f"±{a}%", "u1": f"+{b}%", "u2": f"+{c}%", "u3": ""}
    return [{"key": k, "text": text[k], "tick": ticks[k], "mid": k == "n"} for k in BIN_ORDER]


def squarify(values: list[float], width: float, height: float) -> list[tuple[float, float, float, float]]:
    """Squarified treemap (Bruls, Huizing, van Wijk): rectangles (x, y, w, h) for values sorted
    largest first, filling a width x height box; rows are laid along the shorter side."""
    positive = [i for i, v in enumerate(values) if v > 0]
    out_all: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0) for _ in values]
    if len(positive) < len(values):             # zeros (and negatives) get an empty box
        for i, rect in zip(positive, squarify([values[i] for i in positive], width, height), strict=True):
            out_all[i] = rect
        return out_all
    total = sum(values)
    if total <= 0 or width <= 0 or height <= 0:
        return out_all
    areas = [v * width * height / total for v in values]
    out: list[tuple[float, float, float, float]] = []
    x, y, w, h = 0.0, 0.0, width, height

    def worst(row: list[float], side: float) -> float:
        s = sum(row)
        return max(max(side * side * r / (s * s), (s * s) / (side * side * r)) for r in row)

    i = 0
    while i < len(areas):
        side = min(w, h)
        row = [areas[i]]
        j = i + 1
        while j < len(areas) and worst(row + [areas[j]], side) <= worst(row, side):
            row.append(areas[j])
            j += 1
        s = sum(row)
        if w >= h:                               # a column along the left edge
            col = s / h if h else 0.0
            yy = y
            for a in row:
                rh = a / col if col else 0.0
                out.append((x, yy, col, rh))
                yy += rh
            x += col
            w -= col
        else:                                    # a row along the top edge
            rh = s / w if w else 0.0
            xx = x
            for a in row:
                cw = a / rh if rh else 0.0
                out.append((xx, y, cw, rh))
                xx += cw
            y += rh
            h -= rh
        i = j
    return out


def _pct(r: tuple[float, float, float, float], box: tuple[float, float]) -> tuple[float, float, float, float]:
    return (100 * r[0] / box[0], 100 * r[1] / box[1], 100 * r[2] / box[0], 100 * r[3] / box[1])


def book_split(latest: Any) -> dict[str, Any] | None:
    """S18: the latest run's split for the book map's caption ("Swing x% / Core y%") and the swing
    budget the council set; percent of NAV only. None before the first run that published one."""
    sp = getattr(getattr(latest, "doc", None), "split", None)
    if sp is None:
        return None
    return {"swing": fmt_pct1(sp.swing_pct), "core": fmt_pct1(sp.core_pct), "budget": fmt_pct1(sp.swing_budget_pct),
            "fallback": bool(sp.budget_fallback),
            "text": f"Swing {fmt_pct1(sp.swing_pct)} / Core {fmt_pct1(sp.core_pct)}"}


def book_map(holdings: dict[str, Any], geo: Geometry) -> dict[str, Any] | None:
    """The home page's map of the book, a two-level treemap: one box per asset class (a header strip
    with its name and share), inside it one tile per held line sized by |weight|, plus a cash tile.
    A "Colour by" toggle (radio inputs and CSS) colours the tiles by the instrument's 1-day move (the
    default), by the position's P/L since open (a live book only) or by asset class. Every tile
    carries its bin for each mode (bd-*, bp-*) and prints that mode's value, so colour is never the
    only cue; asset class is also the box, its name and a word on the tile. Shorts keep a coral
    edge, a hatch band, the word and a minus sign in every mode. None when nothing is held."""
    held = [r for r in holdings.get("held", []) if abs(r["weight"]) > EPS]
    if not held:
        return None
    has_pnl = bool(holdings.get("show_pnl"))
    items: list[dict[str, Any]] = []
    for r in held:
        short = r["weight"] < -EPS
        pnl_raw = r.get("pnl_raw") if has_pnl else None
        title = (f"{r['ticker']} · {r['name']}: {fmt_share1(abs(r['weight']))} of the book, "
                 f"{'short' if short else 'long'}" + (f"; last day {r['day']}" if r["day_raw"] is not None else "")
                 + (f"; P/L since open {r['pnl']}" if pnl_raw is not None else ""))
        ref = r["ref"]
        # the manager's change, from the latest run's decision (never from drift between runs)
        moved = bool(r.get("council_moved")) and ref is not None
        if moved:
            title += f"; the manager moved it away from the reference ({fmt_share1(ref)})"
        items.append({"moved": f"ref {fmt_share1(ref)}" if moved else "",
                      "key": r["line"], "value": abs(r["weight"]), "ticker": r["ticker"], "name": r["name"],
                      "ac": r["ac"], "group": r["group"], "page": r["page"], "short": short,
                      "weight": fmt_share1(r["weight"]),
                      "day": r["day"] if r["day_raw"] is not None else "", "day_dir": r["day_dir"], "title": title,
                      "bd": move_bin(r["day_raw"], DAY_EDGES),
                      "pnl": r["pnl"] if pnl_raw is not None else "",
                      "pnl_dir": r["pnl_dir"] if pnl_raw is not None else "none",
                      "bp": move_bin(pnl_raw, PNL_EDGES), "cls_word": MAP_CLASS_WORD.get(r["ac"], "Other"),
                      # an 8-character value ("+123.45%") needs a wider tile before it is printed
                      "dw": r["day_raw"] is not None and len(r["day"]) >= 8,
                      "pw": pnl_raw is not None and len(r["pnl"]) >= 8,
                      "cash": False})
    labels = {key: label for key, label, _acs, _phrase in FILTER_GROUPS}
    groups: dict[str, dict[str, Any]] = {}
    for t in items:
        g = groups.setdefault(t["group"], {"key": t["group"], "label": labels.get(t["group"], "Other"),
                                           "tiles": [], "cash": False})
        g["tiles"].append(t)
    cash = holdings.get("cash_x")
    if cash is not None and cash > 0.004:
        groups["cash"] = {"key": "cash", "label": "Cash", "cash": True, "tiles": [{
            "moved": "", "key": "cash", "value": cash, "ticker": "Cash", "name": "not invested", "ac": "cash",
            "group": "cash", "page": "", "short": False, "weight": fmt_share1(cash), "day": "", "day_dir": "none",
            "bd": "", "pnl": "", "pnl_dir": "none", "bp": "", "cls_word": "", "dw": False, "pw": False,
            "title": f"Cash: {fmt_share1(cash)} of the book, not invested", "cash": True}]}
    order = list(groups.values())
    for g in order:
        g["tiles"].sort(key=lambda t: (-t["value"], t["key"]))
        g["value"] = sum(t["value"] for t in g["tiles"])
    order.sort(key=lambda g: (-g["value"], g["key"]))
    total = sum(g["value"] for g in order)
    boxes = {"desk": MAP_DESK, "phone": MAP_PHONE}
    rects = {layout: squarify([g["value"] for g in order], *box) for layout, box in boxes.items()}
    for i, g in enumerate(order):
        # a header strip on a class box tall and wide enough for one in both layouts (a class name
        # never depends on the width); cash never needs one: its tile says "Cash"
        g["head"] = not g["cash"] and all(r[i][3] >= 2.2 * GROUP_HEAD and r[i][2] >= 44 for r in rects.values())
        for layout, box in boxes.items():
            rect = rects[layout][i]
            g[layout] = _pct(rect, box)
            inner = (rect[2], max(rect[3] - (GROUP_HEAD if g["head"] else 0.0), 1.0))
            for t, r in zip(g["tiles"], squarify([t["value"] for t in g["tiles"]], *inner), strict=True):
                t[layout] = _pct(r, inner)
    for g in order:
        g["cls"] = geo.tile(g["key"], g["desk"], g["phone"], prefix="tgrp-")
        g["share"] = fmt_share1(g["value"])
        g["title"] = f"{g['label']}: {g['share']} of the book"
        for t in g["tiles"]:
            t["cls"] = geo.tile("cash" if t["cash"] else t["key"], t["desk"], t["phone"])
    tiles = [t for g in order for t in g["tiles"] if not t["cash"]]
    # the toggle: 1-day move (the default), P/L since open (only a live book has one), asset class.
    # Should no held line have a daily move, the map opens on asset class rather than all "no data".
    modes = [{"key": "day", "label": "1-day move"}]
    if has_pnl:
        modes.append({"key": "pnl", "label": "P/L since open"})
    modes.append({"key": "class", "label": "Asset class"})
    return {"groups": order, "tiles": [t for g in order for t in g["tiles"]],
            "shorts": any(t["short"] for g in order for t in g["tiles"]),
            "moved": any(t["moved"] for g in order for t in g["tiles"]),
            "cash": "cash" in groups, "total": fmt_share1(total),
            "modes": modes, "default": "day" if any(t["bd"] != "nd" for t in tiles) else "class",
            "has_pnl": has_pnl,
            "legend": {"day": bin_legend(DAY_EDGES, "down", "up"), "pnl": bin_legend(PNL_EDGES, "loss of", "gain of")},
            "nodata": {"day": any(t["bd"] == "nd" for t in tiles), "pnl": any(t["bp"] == "nd" for t in tiles)},
            # no line has a value in this mode: its legend says so instead of showing an unused scale
            "empty": {"day": all(t["bd"] == "nd" for t in tiles), "pnl": all(t["bp"] == "nd" for t in tiles)},
            "classes": sorted(({"key": g["key"], "label": g["label"]} for g in order if not g["cash"]),
                              key=lambda c: (MAP_CLASS_ORDER.get(c["key"], len(MAP_CLASS_ORDER)), c["key"]))}


# ------------------------------------------------------------------------------ the council (home)
def compact_changes(changes: dict[str, tuple[float, float]], lines: Lines) -> str:
    """Changes as a few words with tickers: "cut SEMIS · short GBPUSD"; empty without changes."""
    return " · ".join(f"{verb_for(*changes[k]).replace('add to', 'add')} {ticker(k)}" for k in lines.sort(changes))


def agent_verdicts(cv: CycleView, run: dict[str, Any], tr: dict[str, Any],
                   lines: Lines) -> tuple[dict[str, str], dict[str, dict[str, str] | None]]:
    """What each agent said or did in one run, in a few words (fixed templates over the run's JSON),
    and its status mark when its call failed or it fell back (None otherwise)."""
    verdicts: dict[str, str] = {}
    marks: dict[str, dict[str, str] | None] = {}
    c = cv.doc
    ref_levels = {k: r.level_ref for k, r in c.reference.items()}
    by_id = tr["by_id"]
    data = by_id["a-data"]["body"]
    verdicts["data"] = (f"{plural(data['n_lines'], 'line')} read" + (f" · {plural(data['n_facts'], 'fact')}"
                                                                      if data["n_facts"] else "")
                        if c.reference else "no data this run")
    if data["stale"]:
        verdicts["data"] = "data too old: " + join_words(data["stale"])
    ref = by_id["a-reference"]["body"]
    verdicts["reference"] = (f"{ref['held']} of {plural(ref['n'], 'line')} · {ref['gross']} invested"
                             if ref["rows"] else "no reference this run")
    vol = [k for k in c.cards if k.card_type == "vol_shock"]
    shocked = lines.sort({s for k in vol for s in k.scope if s in lines.info})
    verdicts["vol"] = ("shock on " + ", ".join(ticker(k) for k in shocked)) if shocked else (
        plural(len(vol), "volatility card") if vol else "no volatility shock")
    ev = by_id["a-event"]["body"]
    verdicts["event"] = ("adds blocked on " + join_words(ev["windows"]) if ev["windows"] else
                         plural(len(ev["cards"]), "event card") if ev["cards"] else "no event nearby")
    news = by_id["a-news"]
    verdicts["news"] = (plural(len(news["body"]["cards"]), "card") if news["body"]["cards"] else "no cards")
    macro = by_id["a-macro"]
    if macro["body"]["macro"]:
        verdicts["macro"] = macro["body"]["macro"]["regime"]
    else:
        verdicts["macro"] = ("not scheduled this run" if not macro["calls"] else
                             "no macro context" if macro["failure"] else "no regime recorded")

    def stance_words(a: PublicAdvocate | None, hold: str = "hold the reference") -> str:
        if a is None:
            return "no valid answer"
        return compact_changes(change_list(a.proposal_levels, ref_levels), lines) or hold

    # the bull speaks twice: when its rebuttal asks for something else, both turns are named
    bull, reply = c.debate.bull, c.debate.rebuttal
    if bull is not None and reply is not None and \
            change_list(bull.proposal_levels, ref_levels) != change_list(reply.proposal_levels, ref_levels):
        verdicts["bull"] = f"{stance_words(bull, 'hold ref')} → {stance_words(reply, 'hold ref')}"
    else:
        verdicts["bull"] = stance_words(reply if reply is not None else bull)
    verdicts["bear"] = stance_words(c.debate.bear)
    valid, total = c.pm.valid_replicates, len(c.pm.replicates)
    council = change_list(dict(c.pm.levels), ref_levels)
    words = run["agreement_words"]
    agree = (words.replace(" of ", "/") + " agree") if " of " in words else (f"{words} agree" if words != "—" else "")
    if total == 0 or valid == 0 or c.basis in ("fallback_parse", "fallback_disagreement", "council_unavailable"):
        verdicts["pm"] = "no usable answer: reference used"
    else:
        used = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
        if used is not None and used.valid and not used.deviations:
            council = {}                         # no deviation claimed: level drift is not a decision
        verdicts["pm"] = (compact_changes(council, lines) or "hold the reference") + (f" · {agree}" if agree else "")
    ctrl = c.single_agent
    if ctrl is None:
        verdicts["control"] = "did not run"
    elif not ctrl.levels:
        verdicts["control"] = "no valid answer"
    else:
        differs = [k for k in lines.sort(set(ctrl.levels) & set(c.pm.levels))
                   if abs(ctrl.levels[k] - c.pm.levels[k]) > EPS]
        verdicts["control"] = ("differs on " + ", ".join(ticker(k) for k in differs)) if differs else "same as the council"
    audit = by_id["a-audit"]["body"]
    fixes = len(audit["notes"]) + len(audit["clipped"])
    verdicts["audit"] = plural(fixes, "correction") if fixes else "nothing reverted"
    if c.risk is None:
        verdicts["risk"] = "did not run"
    else:
        held = [r["line"] for r in run["rows"] if r["held"]]
        verdicts["risk"] = f"{run['checks_passed']}/{run['checks_total']} checks pass" + (
            " · held " + ", ".join(ticker(k) for k in held) if held else "")
    costs = by_id["a-costs"]["body"]
    if cv.rehearsal:
        dn = next(n for n in run["nodes"] if n["key"] == "decision")
        verdicts["costs"] = dn["detail"].replace("a live run would need", "would need") or "no orders"
    elif costs["legs"]:
        cost = c.plan.cost_bp_total if c.plan else 0.0
        verdicts["costs"] = plural(len(costs["legs"]), "order") + (f" · ≈{cost / 100:.2f}% cost" if cost else "")
    else:
        verdicts["costs"] = "no orders needed"
    verdicts["human"] = run["chain"][-1]["text"]
    for a in tr["agents"]:
        if a["slug"] in marks:
            continue
        sections = [x for x in tr["agents"] if x["slug"] == a["slug"]]
        st = sections[0]["status"] if len(sections) == 1 else _merge_status(sections)
        marks[a["slug"]] = st if st["css"] in ("failed", "timeout", "fallback") else None
    dstat = tr["decision_status"]
    marks["human"] = dstat if dstat["css"] in ("fallback", "failed") else None
    return verdicts, marks


def build_roster(cv: CycleView | None, run: dict[str, Any] | None, tr: dict[str, Any] | None,
                 lines: Lines) -> list[dict[str, Any]]:
    """The council in speaking order, grouped by phase: each agent's job in one line and what it
    said or did in the latest run, in a few words (fixed templates over the run's JSON), with a
    status mark when its latest call failed or it fell back."""
    verdicts: dict[str, str] = {}
    marks: dict[str, dict[str, str] | None] = {}
    if cv is not None and run is not None and tr is not None:
        verdicts, marks = agent_verdicts(cv, run, tr, lines)
    phases = []
    for key, title, what in PHASES:
        members = []
        for spec in AGENT_SPECS:
            if spec.phase != key:
                continue
            members.append({
                "slug": spec.slug, "name": spec.name, "kind": spec.kind, "short": spec.short,
                "verdict": verdicts.get(spec.slug, "not run yet"), "mark": marks.get(spec.slug),
                "icon": AGENT_ICONS.get(spec.slug, "cpu"), "page": f"agents/{spec.slug}.html",
                "seat": AGENT_SPECS.index(spec) + 1,
            })
        phases.append({"key": key, "title": title, "what": what, "n": len(phases) + 1, "members": members})
    return phases


# ------------------------------------------------------------------------------ asset pages
def mentions_line(text: str, k: str, lines: Lines) -> bool:
    """Does a text name the line: its id or ticker (3+ characters) or its name (4+, or its singular
    when the name ends in "s"), as a word?"""
    if not text:
        return False
    name = lines.name(k)
    terms = [(k, 0), (ticker(k), 0), (name, re.I)]
    if len(name) >= 5 and name.endswith("s"):          # "Semiconductor volatility" names Semiconductors
        terms.append((name[:-1], re.I))
    for term, flags in terms:
        long_enough = bool(term) and (len(term) >= 4 or (len(term) >= 3 and term.upper() == term))
        if long_enough and re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", text, flags):
            return True
    return False


def ref_line(ref: Any) -> str:
    """The line an evidence reference is about ("F:GOLD:trend" -> "GOLD"), else ""."""
    rid = ref_id(ref)
    parts = rid.split(":")
    if parts[0] in ("F", "V", "C") and len(parts) >= 3:
        return parts[1]
    if parts[0] == "E":
        body = rid[2:].partition("@")[0]
        return body.partition(":")[2]
    return ""


def cites_line(refs: Any, k: str) -> bool:
    return any(r is not None and ref_line(r) == k for r in refs)


NOTHING_TRADED = ("rejected", "expired", "superseded", "reviewed_no_action")


def executed_weight(cv: CycleView, k: str) -> float | None:
    """The weight the real book held on a line after a run, only where the record says so:
    - completed: the achieved weight, else the target (every leg filled);
    - completed_partial: the achieved weight, else the unchanged book when the plan had a leg on
      the line (that leg's fill is unknown), else the target (no leg: nothing moved it);
    - rejected, expired, superseded, reviewed_no_action: the unchanged book (nothing traded);
    - any other state (blocked, execution unknown, approved, executing, proposed, sealed): the
      achieved weight if an execution record has it, else None: the outcome is not known yet.
    None for a rehearsal: nothing was traded."""
    c = cv.doc
    if cv.rehearsal or c.risk is None:
        return None
    state = cv.final_state
    achieved = cv.execution.achieved_x if cv.execution is not None else {}
    if state == "completed":
        return achieved.get(k, c.risk.final_x.get(k, 0.0))
    if state == "completed_partial":
        if k in achieved:
            return achieved[k]
        legs = {leg.line for leg in (c.plan.legs if c.plan else [])}
        return c.risk.base_x.get(k, 0.0) if k in legs else c.risk.final_x.get(k, 0.0)
    if state in NOTHING_TRADED:
        return c.risk.base_x.get(k, 0.0)
    return achieved.get(k)


SETTLEMENT_WORDS = {"real": "real", "cfd": "CFD", "realFutures": "futures", "marginTrade": "margin"}
STEP_SERIES = (
    # key, legend label, css, what it is
    ("ref", "Reference", "s-ref", "the weight the rules alone would hold"),
    ("council", "Council", "s-council", "the council's decision, before the risk engine"),
    ("held", "Executed book", "s-held", "what the real book held after the run (empty for a rehearsal or while "
                                        "the outcome is unknown)"),
)
CHART_CAP = 60                  # a line page's chart and table show the last 60 runs


def weight_chart(points: list[dict[str, Any]], geo: Geometry) -> dict[str, Any] | None:
    """A step chart of a line's weight across the published runs: one column per run, each series
    holding its value until the next run. SVG in a 0-100 box stretched to the plot (strokes do not
    scale); tick, end and column labels and the executed book's markers are HTML placed by geometry
    classes. A zero baseline always shows. A line at 0% in every series gets no chart ("flat"); a
    single run gets its three figures in words ("single")."""
    if not points:
        return None
    n = len(points)
    present = [key for key, *_ in STEP_SERIES if any(p[key] is not None for p in points)]
    vals = [p[key] for p in points for key, *_ in STEP_SERIES if p[key] is not None]
    if all(abs(v) < 5e-4 for v in vals):
        return {"flat": True, "single": False, "n": n, "present": present}
    if n == 1:
        p = points[0]
        return {"flat": False, "single": True, "n": 1, "present": present,
                "figures": [{"label": label, "css": css, "value": fmt_share1(p[key]) if p[key] is not None else None}
                            for key, label, css, _what in STEP_SERIES]}
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    if hi - lo < EPS:
        hi = lo + 0.05
    step = next((s for s in (0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0) if (hi - lo) / s <= 4), 1.0)
    lo = step * math.floor(lo / step + 1e-6)
    hi = step * math.ceil(hi / step - 1e-6)
    if hi - lo < EPS:
        hi = lo + step

    def fy(v: float) -> float:
        return 4.0 + 92.0 * (1.0 - (v - lo) / (hi - lo))

    series = []
    for key, label, css, what in STEP_SERIES:
        d, pen = [], False
        for i, p in enumerate(points):
            v = p[key]
            x0, x1 = 100.0 * i / n, 100.0 * (i + 1) / n
            if v is None:
                pen = False
                continue
            y = fy(v)
            d.append(f"{'L' if pen else 'M'}{x0:.2f},{y:.2f} H{x1:.2f}")
            pen = True
        last = next((p[key] for p in reversed(points) if p[key] is not None), None)
        if not d:
            continue
        series.append({"key": key, "label": label, "css": css, "what": what, "d": " ".join(d), "last": last,
                       "y": fy(last) if last is not None else None})
    # end labels: equal values share one label; the others keep at least 11% of the plot apart
    ends: list[dict[str, Any]] = []
    for s in sorted((s for s in series if s["last"] is not None), key=lambda s: s["y"]):
        same = next((e for e in ends if abs(e["value"] - s["last"]) < 5e-4), None)
        if same is not None:
            same["labels"].append(s["label"])
            same["css"].append(s["css"])
            continue
        ends.append({"value": s["last"], "y": s["y"], "labels": [s["label"]], "css": [s["css"]]})
    for i, e in enumerate(ends):
        if i:
            e["y"] = max(e["y"], ends[i - 1]["y"] + 11.0)
    over = (ends[-1]["y"] - 100.0) if ends else 0.0
    if over > 0:
        for e in ends:
            e["y"] -= over
    for e in ends:
        e["top"] = geo.cls("top", e["y"])
        e["text"] = " = ".join(e["labels"]) + " " + fmt_share1(e["value"])
    ticks = []
    v = lo
    while v <= hi + EPS:
        ticks.append({"y": f"{fy(v):.2f}", "top": geo.cls("top", fy(v)), "label": fmt_share(v) if abs(v) > EPS else "0%",
                      "zero": abs(v) < EPS})
        v += step
    cols = []
    for i, p in enumerate(points):
        parts = [f"{label} {fmt_share1(p[key]) if p[key] is not None else '—'}" for key, label, *_ in STEP_SERIES]
        outcome = p.get("outcome", "")
        cols.append({"x": f"{100.0 * i / n:.2f}", "w": f"{100.0 / n:.2f}", "rehearsal": p.get("rehearsal", False),
                     "left": geo.cls("left", 100.0 * i / n),
                     "title": f"{p['when']}" + (f" ({outcome})" if outcome else "") + ": " + " · ".join(parts)})
    # a date under every column when there are few (desktop), else the first and the last only
    xlabels = []
    for i, p in enumerate(points):
        edge = "first" if i == 0 else "last" if i == n - 1 else "mid"
        if edge != "mid" or n <= 6:
            xlabels.append({"left": geo.cls("left", 100.0 * (i + 0.5) / n), "text": p["short"], "cls": edge})
    # the executed book's value marked at the start of each column, so a 0% on the baseline shows
    dots = [{"left": geo.cls("left", 100.0 * i / n), "top": geo.cls("top", fy(p["held"])),
             "title": f"{p['when']}: executed book {fmt_share1(p['held'])}"}
            for i, p in enumerate(points) if p["held"] is not None]
    return {"flat": False, "single": False, "series": series, "ends": ends, "ticks": ticks, "cols": cols,
            "xlabels": xlabels, "dots": dots, "zero_y": f"{fy(0.0):.2f}", "n": n, "present": present,
            "rehearsals": any(p.get("rehearsal") for p in points)}


def order_view(kind: str, direction: str | None, before: float | None, after: float | None) -> dict[str, str]:
    """An order in words: the verb (buy or sell), the kind, and the position it works on. The
    position chip is teal or coral only when the order opens or adds to it; a close is neutral."""
    b, a = abs(before or 0.0), abs(after or 0.0)
    grows = a > b + EPS
    side = direction if direction in ("long", "short") else ("short" if (after or 0.0) < -EPS else "long")
    verb = ("buy" if grows else "sell") if side == "long" else ("sell" if grows else "buy")
    return {"verb": verb, "kind": kind.replace("_", " "), "position": f"{side} position",
            "css": side if grows else "neutral"}


def _trend_groups(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive runs with the same trend, size, range, reasons and qualifying cards, as one item."""
    out: list[dict[str, Any]] = []
    for e in entries:
        if out and out[-1]["sig"] == e["sig"]:
            g = out[-1]
            g["runs"] += 1
            g["last"] = e["when"]
            g["href"] = e["href"]
            g["cards"] = e["cards"]
            continue
        out.append({**e, "runs": 1, "first": e["when"], "last": e["when"]})
    return out


def build_assets(view: JournalView, lines: Lines, holdings: dict[str, Any], geo: Geometry,
                 linked: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One page per line that ever appeared (the policy's, the book's and every run's): its
    position, its weight across runs, its trend, what the agents said about it, its trades and its
    latest facts. Every fact comes from the published JSON; every sentence is a fixed template.
    `linked` holds each run's transcript with links into the run page (claims and their fates)."""
    rows = {r["line"]: r for r in holdings.get("held", []) + holdings.get("flat", [])}
    chrono = list(reversed(view.cycles))          # oldest first
    out = []
    for k in lines.sort(lines.info):
        if not asset_page(k):
            continue
        asset = _asset(k, lines)
        row = rows.get(k)
        book_line = view.book.lines.get(k) if view.book is not None else None
        # ---- weight across runs (the last CHART_CAP runs)
        points = []
        for cv in chrono:
            c = cv.doc
            if k not in c.reference and not (c.risk and (k in c.risk.final_x or k in c.risk.raw_x)):
                continue
            ref = c.reference[k].weight_ref_x if k in c.reference else None
            council = c.risk.raw_x.get(k, ref) if c.risk else None
            points.append({"cid": c.cycle_id, "when": fmt_when(c.slot), "short": fmt_short_when(c.slot),
                           "mode": c.mode, "ref": ref, "council": council, "held": executed_weight(cv, k),
                           "href": f"../cycles/{c.cycle_id}.html", "rehearsal": cv.rehearsal,
                           "chip": cv.chip, "outcome": cv.chip["label"].lower()})
        older_points = max(0, len(points) - CHART_CAP)
        points = points[-CHART_CAP:]
        chart = weight_chart(points, geo)
        # ---- trend states, oldest first, consecutive identical runs merged
        trend_entries = []
        for cv in chrono:
            c = cv.doc
            if k not in c.reference:
                continue
            band = c.bands.get(k)
            units = unit_weights(c)
            fx = FactIndex(c, lines, f"../cycles/{c.cycle_id}.html")
            reasons = list(band.reasons) if band else []
            qualifying = list(band.qualifying_cards) if band else []
            rng = f"{fmt_level(band.lo)} to {fmt_level(band.hi)}" if band else ""
            rng_pct = (f"{fmt_share(units[k] * band.lo)} to {fmt_share(units[k] * band.hi)}"
                       if band and k in units else "")
            level = fmt_level(c.reference[k].level_ref)
            trend_entries.append({
                "when": f"{c.slot.day} {MONTHS[c.slot.month - 1]} {c.slot:%H:%M}", "trend": c.reference[k].trend,
                "level": level,
                "weight": fmt_share(c.reference[k].weight_ref_x), "range": rng, "range_pct": rng_pct,
                "fixed": bool(band) and abs(band.hi - band.lo) < EPS,
                "reasons": "; ".join(reasons), "href": f"../cycles/{c.cycle_id}.html#a-audit",
                "cards": [fx.chip(SimpleNamespace(kind="card", id=cid)) for cid in qualifying],
                "sig": (c.reference[k].trend, level, rng, rng_pct, tuple(reasons), tuple(qualifying)),
            })
        trends = _trend_groups(trend_entries)
        # ---- what the agents said about it (newest run first)
        said = []
        for cv in view.cycles[:HISTORY_CAP]:
            c = cv.doc
            cid_ = c.cycle_id
            base = f"../cycles/{cid_}.html"
            fx = FactIndex(c, lines, base)
            tr = linked.get(cid_, {})
            by_id = tr.get("by_id", {})
            ref_levels = {x: r.level_ref for x, r in c.reference.items()}
            ref_x = {x: r.weight_ref_x for x, r in c.reference.items()}
            units = unit_weights(c)
            turns = debate_turns(c)
            claims_by_turn = {t: {cl.claim_id for cl in x.claims} for t, x in turns.items() if x is not None}
            claim_obj = {(t, cl.claim_id): cl for t, x in turns.items() if x is not None for cl in x.claims}
            items: list[dict[str, Any]] = []

            def about(text: str, refs: Any, k: str = k) -> bool:
                return mentions_line(text, k, lines) or cites_line(refs, k)

            def item(slug: str, who: str, kind: str, text: str, href: str, *, verdict: str = "", cid: str = "",
                     chips: list[dict[str, str]] | None = None, fates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
                return {"slug": slug, "who": who, "kind": kind, "text": text, "href": href, "verdict": verdict,
                        "cid": cid, "chips": chips or [], "fates": fates or []}

            for kc in c.cards:
                if k in kc.scope or about(kc.claim, kc.evidence):
                    slug = {"vol": "vol", "event": "event", "news": "news", "macro": "macro"}.get(kc.role, "news")
                    items.append(item(slug, AGENT_BY_SLUG[slug].name, "card", kc.claim,
                                      f"{base}#{anchor_slug('k', kc.card_id)}", chips=[fx.chip(e) for e in kc.evidence]))
            shown: set[tuple[str, str]] = set()
            for turn, a in turns.items():
                if a is None:
                    continue
                slug = "bear" if turn == "bear" else "bull"
                who = {"bull_open": "Bull · opening", "bear": "Bear", "bull_rebuttal": "Bull · rebuttal"}[turn]
                body = (by_id.get(TURN_ANCHOR[turn]) or {}).get("body") or {}
                fates = {cl["id"]: cl["fates"] for cl in body.get("claims", [])}
                if k in a.proposal_levels and k in ref_levels and abs(a.proposal_levels[k] - ref_levels[k]) > EPS:
                    before, after = ref_levels[k], a.proposal_levels[k]
                    wb = ref_x.get(k)
                    wa = units[k] * after if k in units else None
                    amount = (f" from {fmt_share(wb)} to {fmt_share(wa)}" if wb is not None and wa is not None
                              else f" from size {fmt_level(before)} to {fmt_level(after)}")
                    items.append(item(slug, who, "asked", f"Asked to {verb_for(before, after)} it{amount}.",
                                      f"{base}#{TURN_ANCHOR[turn]}"))
                for cl in a.claims:
                    if about(cl.text, cl.evidence):
                        shown.add((turn, cl.claim_id))
                        items.append(item(slug, who, "claim", cl.text, f"{base}#{claim_anchor(turn, cl.claim_id)}",
                                          cid=cl.claim_id, chips=[fx.chip(e) for e in cl.evidence],
                                          fates=fates.get(cl.claim_id, [])))
                for rb in a.rebuttals:
                    if ("bull_open", rb.claim_id) in shown:
                        continue                       # shown under the claim it answers
                    if about(rb.text, rb.evidence):
                        items.append(item(slug, who, "answer", rb.text,
                                          f"{base}#{anchor_slug('rb', f'{turn}-{rb.claim_id}')}", verdict=rb.verdict,
                                          cid=f"bull's {rb.claim_id}", chips=[fx.chip(e) for e in rb.evidence]))
                for text in a.concessions:
                    m = re.match(r"^\s*(c\d+)\s*[:.\-–]\s*(.*)$", text)
                    target = claim_obj.get(("bear", m.group(1))) if m and turn == "bull_rebuttal" else None
                    if m and ("bear", m.group(1)) in shown and target is not None:
                        continue                       # shown under the claim it concedes
                    words = m.group(2) if m and target is not None else text
                    if mentions_line(text, k, lines) or (target is not None and about(target.text, target.evidence)):
                        href = (f"{base}#{claim_anchor('bear', m.group(1))}" if target is not None and m
                                else f"{base}#{TURN_ANCHOR[turn]}")
                        items.append(item(slug, who, "concession", words, href, verdict="concede",
                                          cid=f"bear's {m.group(1)}" if target is not None and m else ""))
            for block, slug, anchor in ((c.pm, "pm", "a-pm"), (c.single_agent, "control", "a-control")):
                if block is None:
                    continue
                same: dict[tuple[str, float, str], dict[str, Any]] = {}     # identical attempts share one item
                for r in block.replicates:
                    used = slug == "pm" and r.replicate == block.medoid
                    for d in r.deviations:
                        if d.line != k:
                            continue
                        key = (d.direction, round(d.level, 4), d.reason)
                        if key in same:
                            same[key]["attempts"].append((r.replicate + 1, used))
                            continue
                        rl = ref_levels.get(k)
                        wb = ref_x.get(k)
                        wa = units[k] * d.level if k in units else None
                        amount = (f" from {fmt_share(wb)} to {fmt_share(wa)}" if wb is not None and wa is not None
                                  else f" to size {fmt_level(d.level)}")
                        same[key] = {**item(slug, "", "decision",
                                            f"{d.direction.capitalize()}{amount}" + (f" (size {fmt_level(rl)} → "
                                            f"{fmt_level(d.level)})" if rl is not None else "") + f": {d.reason}",
                                            f"{base}#{anchor}-{r.replicate + 1}", chips=[fx.chip(e) for e in d.evidence]),
                                     "attempts": [(r.replicate + 1, used)]}
                        items.append(same[key])
                for it in same.values():
                    nums = [f"{n}{' (used)' if u else ''}" for n, u in it["attempts"]]
                    it["who"] = (f"{AGENT_BY_SLUG[slug].name}, attempt{'s' if len(nums) > 1 else ''} "
                                 + join_words(nums))
                    it["verdict"] = "used" if any(u for _, u in it["attempts"]) else ""
                decisive: dict[str, dict[str, Any]] = {}
                for r in block.replicates:
                    used = slug == "pm" and r.replicate == block.medoid
                    who = f"{AGENT_BY_SLUG[slug].name}, attempt {r.replicate + 1}" + (" (used)" if used else "")
                    df = r.decisive_fact
                    if df is not None and df.evidence is not None and cites_line([df.evidence], k):
                        if df.text in decisive:
                            decisive[df.text]["who"] += f", {r.replicate + 1}" + (" (used)" if used else "")
                        else:
                            decisive[df.text] = item(slug, who, "decisive fact", df.text,
                                                     f"{base}#{anchor}-{r.replicate + 1}", chips=[fx.chip(df.evidence)])
                            items.append(decisive[df.text])
                    for dm in r.dismissed if slug == "pm" else []:
                        targets = resolve_claim(dm.claim_id, r.sided_with, claims_by_turn)
                        firm = [(t, x) for t, x, how in targets if how != "ambiguous"]
                        if any((t, x) in shown for t, x in firm):
                            continue                   # shown under the claim it sets aside
                        on_line = any(about(claim_obj[(t, x)].text, claim_obj[(t, x)].evidence) for t, x in firm
                                      if (t, x) in claim_obj)
                        if on_line or mentions_line(dm.why, k, lines):
                            label = (f"{TURN_WORDS[firm[0][0]]} {firm[0][1]}" if len(firm) == 1 else dm.claim_id)
                            items.append(item(slug, who, "set aside", dm.why, f"{base}#{anchor}-{r.replicate + 1}",
                                              verdict="dismissed", cid=label))
            if c.risk is not None:
                holds, _ = split_holds(c.risk.hold_reasons)
                for h in holds.get(k, []):
                    items.append(item("risk", "Risk engine", "held back", f"Held back: {h['text']}.", f"{base}#a-risk"))
            for r in c.pm.replicates:
                for v in [*r.violations, *r.reverted]:
                    if v.startswith(f"{k}:"):
                        items.append(item("audit", "Auditor", "correction",
                                          f"Attempt {r.replicate + 1}: " + plain_violation(v, lines)["text"] + ".",
                                          f"{base}#a-audit"))
            if items:
                said.append({"cv": cv, "when": fmt_when(c.slot), "href": base, "notes": items})
        # ---- trades
        trades = []
        for cv in view.cycles:
            c = cv.doc
            legs = [leg for leg in (c.plan.legs if c.plan else []) if leg.line == k]
            if not legs:
                continue
            council = change_list(dict(c.pm.levels), {x: r.level_ref for x, r in c.reference.items()})
            ref_x = {x: r.weight_ref_x for x, r in c.reference.items()}
            fills = {f.seq: f for f in (cv.execution.fills if cv.execution else []) if f.line == k}
            if cv.rehearsal:
                outcome = {"label": "not sent", "css": "stone", "title": "Rehearsal: nothing was sent"}
            else:
                outcome = cv.chip
            for leg in legs:
                f = fills.get(leg.seq)
                trades.append({"when": fmt_short_when(c.slot), "href": f"../cycles/{c.cycle_id}.html#a-costs",
                               "order": order_view(leg.kind, leg.direction, leg.weight_before_x, leg.weight_after_x),
                               "move": f"{fmt_share(leg.weight_before_x)} → {fmt_share(leg.weight_after_x)}",
                               "vehicle": f"{SETTLEMENT_WORDS.get(leg.settlement or '', leg.settlement or '—')} · {leg.leverage}x",
                               "cost": fmt_bp(leg.cost_bp),
                               "why": leg_reason(leg, council, ref_x, lines, c.pm.medoid), "outcome": outcome,
                               "fill": ({"state": f.state.replace("_", " "), "filled": fmt_signed(100 * f.weight_filled_x, 1)
                                         if f.weight_filled_x is not None else "—",
                                         "slippage": fmt_bp(f.slippage_bp), "ok": f.state == "filled"} if f else None)})
        # ---- latest facts
        facts = None
        for cv in view.cycles:
            mine = [f for f in cv.doc.facts if f.line == k]
            if mine:
                fx = FactIndex(cv.doc, lines, f"../cycles/{cv.doc.cycle_id}.html")
                facts = {"when": fmt_when(cv.doc.slot), "href": f"../cycles/{cv.doc.cycle_id}.html#facts",
                         "rows": [{**fx.row(f), "href": fx.href(f.id)} for f in mine]}
                break
        ids_only = facts is None and any(not cv.doc.facts for cv in view.cycles)
        vehicle = ""
        if book_line is not None and book_line.settlement:
            vehicle = {"real": "real", "cfd": "CFD"}.get(book_line.settlement, book_line.settlement) + (
                f" · {book_line.leverage}x" if book_line.leverage else "")
        elif lines.info[k].vehicle:
            vehicle = {"real": "real", "cfd": "CFD"}[lines.info[k].vehicle] + " (preferred)"
        out.append({"line": k, "asset": asset, "row": row, "vehicle": vehicle, "chart": chart, "points": points,
                    "older_points": older_points, "trends": trends, "said": said,
                    "said_capped": len(view.cycles) > HISTORY_CAP, "trades": trades, "facts": facts,
                    "ids_only": ids_only, "runs": len(points), "in_reference": lines.info[k].in_reference,
                    "council_may": lines.info[k].council, "sleeve": lines.info[k].sleeve})
    return out


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


def _set_aside(cv: CycleView, turn_of: tuple[str, ...]) -> int:
    """How many of an advocate's claims the used manager attempt set aside in a run."""
    c = cv.doc
    used = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
    if used is None:
        return 0
    claims = {t: {cl.claim_id for cl in x.claims} for t, x in debate_turns(c).items() if x is not None}
    hit = set()
    for d in used.dismissed:
        for t, cid, how in resolve_claim(d.claim_id, used.sided_with, claims):
            if t in turn_of and how != "ambiguous":
                hit.add((t, cid))
    return len(hit)


def build_agents(view: JournalView, transcripts: dict[str, dict[str, Any]], prompts: dict[str, dict[str, str]],
                 model: str, runs: dict[str, dict[str, Any]] | None = None,
                 lines: Lines | None = None, sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One entry per agent: its spec, its numbers over every published meeting (live runs and paper
    decisions, `agent_sources`), an overview row per meeting (what it said or did, whose side the
    manager took, the outcome) and its history (newest first; the transcript sections of each
    meeting, with links into the run or decision page)."""
    out = []
    if sources is None:
        sources = [{"kind": "rehearsal" if cv.rehearsal else "live", "cv": cv, "no": None,
                    "run": (runs or {}).get(cv.doc.cycle_id), "tr": transcripts[cv.doc.cycle_id],
                    "href": f"../cycles/{cv.doc.cycle_id}.html", "core_anchor": ""} for cv in view.cycles]
    verdicts: dict[str, dict[str, str]] = {}
    if lines is not None:
        for src in sources:
            if src["run"] is not None:
                verdicts[src["cv"].doc.cycle_id + str(src["no"] or "")] = agent_verdicts(
                    src["cv"], src["run"], src["tr"], lines)[0]
                if src["kind"] == "paper":
                    verdicts[src["cv"].doc.cycle_id + str(src["no"])]["human"] = "not needed: a paper run"
    for spec in AGENT_SPECS:
        calls = [call_view(x) for src in sources for x in src["cv"].doc.calls if x.role in spec.roles]
        n = len(calls)
        ok = sum(1 for x in calls if not x["failed"])
        # every failed call, newest meeting first, linking to its place in the run (or the decision)
        failed = [{"when": fmt_short_when(src["cv"].doc.slot), "word": v["word"], "error": v["error_word"],
                   "what": v["fail_phrase"], "css": v["css"], "no": src["no"],
                   "attempt": v["replicate"] if x.role in ("pm", "single_agent") else 0,
                   "href": src["href"] + "#" + (src["core_anchor"] or v["anchor"])}
                  for src in sources for x in src["cv"].doc.calls if x.role in spec.roles
                  for v in [call_view(x)] if v["failed"]]
        kinds: dict[str, int] = {}
        for f in failed:
            kinds[f["word"]] = kinds.get(f["word"], 0) + 1
        entries = []
        for src in sources:
            cv, tr = src["cv"], src["tr"]
            meta = {"kind": src["kind"], "no": src["no"], "key": cv.doc.cycle_id + str(src["no"] or ""),
                    "chip": PAPER_CHIP if src["kind"] == "paper" else cv.chip}
            if spec.slug == "human":
                # the person is not a section of the transcript: its entry is the run's decision
                legs = len(cv.doc.plan.legs) if cv.doc.plan else 0
                entries.append({"cv": cv, "sections": [], "href": src["href"], **meta,
                                "anchor": src["core_anchor"] or "a-decision", "chain": tr["chain"],
                                "status": tr["decision_status"],
                                "decision": {"chip": meta["chip"], "legs": legs, "reason": cv.decision_reason,
                                             "paper": src["kind"] == "paper",
                                             "approved": fmt_when(cv.approved_slot) if cv.approved_slot else ""}})
                continue
            sections = [a for a in tr["agents"] if a["slug"] == spec.slug]
            if not sections:
                continue
            entries.append({"cv": cv, "sections": sections, "href": src["href"], **meta,
                            "anchor": src["core_anchor"] or sections[0]["id"],
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
            # the same record in plain words (the index card and the page's numbers)
            stats["agree_words"] = (f"Manager agreed with it {got} of {plural(decided, 'time')}" if decided
                                    else "No usable manager decision yet")
            stats["points_words"] = (f"{dismissed} of its {plural(claims, 'point')} rejected" if claims else "")
            stats["got_n"], stats["decided_n"], stats["dismissed_n"], stats["claims_n"] = got, decided, dismissed, claims
        elif spec.slug == "pm":
            valid = [r for src in sources for r in src["cv"].doc.pm.replicates]
            stats["valid"] = _share_words(sum(1 for r in valid if r.valid), len(valid))
            stats["changed"] = _share_words(
                sum(1 for src in sources if src["tr"]["by_id"]["a-pm"]["body"]["decision"]), len(sources))
        elif spec.slug == "control":
            stats["differs"] = _share_words(sum(1 for src in sources if src["cv"].control_agrees is False),
                                            sum(1 for src in sources if src["cv"].control_agrees is not None))
        elif spec.slug == "human":
            asked = [cv for cv in view.cycles if not cv.rehearsal and cv.doc.plan and cv.doc.plan.legs]
            stats["asked"] = len(asked)
            stats["approved"] = sum(1 for cv in asked if cv.final_state in ("approved", "executing", "completed",
                                                                            "completed_partial"))
            stats["rejected"] = sum(1 for cv in asked if cv.final_state == "rejected")
            stats["expired"] = sum(1 for cv in asked if cv.final_state == "expired")
        stems = [r for r in spec.roles if r in prompts] if spec.roles else []
        # one overview row per run: what it said or did, whose side the manager took, the outcome
        overview = []
        for e in entries[:HISTORY_CAP]:
            cv = e["cv"]
            c = cv.doc
            used = next((r for r in c.pm.replicates if r.replicate == c.pm.medoid), None)
            sided = SIDED_WORDS.get(used.sided_with or "", "—") if used is not None and used.valid else "—"
            row = {"cv": cv, "href": e["href"], "anchor": e["anchor"], "status": e["status"],
                   "said": verdicts.get(e["key"], {}).get(spec.slug, ""), "sided": sided, "set_aside": None,
                   "chip": e["chip"], "kind": e["kind"], "no": e["no"],
                   "risk_said": verdicts.get(e["key"], {}).get("risk", ""),
                   "core_verb": core_line(c, lines)["verb"] if lines is not None else ""}
            if spec.slug == "bull":
                row["set_aside"] = _set_aside(cv, ("bull_open", "bull_rebuttal"))
            elif spec.slug == "bear":
                row["set_aside"] = _set_aside(cv, ("bear",))
            elif spec.slug == "pm":
                row["set_aside"] = len(used.dismissed) if used is not None else 0
            overview.append(row)
        path = spec.source
        out.append({
            "spec": spec, "slug": spec.slug, "name": spec.name, "kind": spec.kind, "accent": spec.accent,
            "icon": AGENT_ICONS.get(spec.slug, "cpu"),
            "job": spec.job, "more": spec.more, "model": model if spec.kind == "LLM" else "code",
            "source": f"{REPO_URL}/blob/main/{spec.source}" if spec.source else "",
            "source_path": path, "source_dir": path.rpartition("/")[0] + "/" if "/" in path else "",
            "source_file": path.rpartition("/")[2],
            "prompts": [{"stem": s, **prompts[s]} for s in stems],
            "stats": stats, "entries": entries[:HISTORY_CAP], "older": entries[HISTORY_CAP:], "overview": overview,
            "debate": spec.slug in ("bull", "bear", "pm"),
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


def performance_chart(points: list[Any], width: int = 690, height: int = 240,
                      labels: bool = True, spec: tuple[tuple[str, ...], ...] | None = None) -> dict[str, Any] | None:
    """Polylines for each control with at least two values, a recessive grid at round index values
    and a label at the end of each line (identity never by colour alone). Without `labels` (the
    phone shape) the end labels are left out and the legend under the chart names every line."""
    if len(points) < 2:
        return None
    pad_l, pad_r, pad_y = 44, (150 if labels else 12), 14
    spec = spec or CONTROL_SERIES
    values = [getattr(p, key) for p in points for key, *_ in spec if getattr(p, key) is not None]
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
    for key, name, css, short, what in spec:
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
        "first": fmt_day(points[0].as_of), "last": fmt_day(points[-1].as_of), "labels": labels,
    }


# ------------------------------------------------------------------------------ swing book (SW-7)
# swing-book.md rev 2, §7.4 / §8.4. Pages exist only once the journal holds a swing document or a
# run with a swing part (so a core-only record builds exactly as before). Every value is a
# percentage, an R multiple or a word; PAPER-only rows (a paper-only setup, a paper trade, the
# SQ-8 benchmark, every funnel group) say PAPER in a chip, never by colour alone.
SWING_NAV = {"key": "swing", "href": "swing/index.html", "label": "Swing"}
PREREG_URL = f"{REPO_URL}/blob/main/docs/swing-book-prereg.md"
SWING_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("scout", "Scout", "LLM", "scout", "Swing book",
              "Reads the news and proposes swing ideas: a stock, long or short, held for days to a few weeks.",
              roles=("scout",), source="prompts/scout.md",
              more="It must cite the news item or filing behind each idea by its id. Code then checks the ticker, "
                   "builds a fact card from completed daily bars and drops ideas that chase a move already made.",
              short="Reads the news and proposes swing ideas.", phase="swing"),
    AgentSpec("skeptic", "Skeptic", "LLM", "skeptic", "Swing book",
              "Checks, without seeing the pitch, whether the move is already in the price, and looks at the bigger "
              "picture.",
              roles=("skeptic",), source="prompts/skeptic.md",
              more="It runs on the same model as the Scout and stays blind by its input: it sees only the ticker, the "
                   "side, the cited items (the catalyst), a one-line factual claim and the fact card, never the Scout's "
                   "thesis or levels. It answers pass, wait or reject; "
                   "code turns a 'mostly priced in' pass into a wait. A weekly canary (a past event whose move was "
                   "already in the price) checks that it still says no.",
              short="Checks, blind to the pitch, whether the news is already priced in.", phase="swing"),
)
SWING_ROLES_ALL = ("scout", "skeptic", "swing_bull", "swing_bear", "swing_pm")
SWING_ROLE_WORDS = {"scout": ("Scout", "scout"), "skeptic": ("Skeptic", "skeptic"),
                    "swing_bull": ("Bull · swing", "bull"), "swing_bear": ("Bear · swing", "bear"),
                    "swing_pm": ("Manager · swing", "pm")}
SWING_STAGE = {   # stage -> (words, chip css)
    "dropped_by_code": ("dropped by code", "stone"), "skeptic": ("stopped by the Skeptic", "halted"),
    "waiting": ("waiting (Skeptic)", "warn"), "debate": ("debated", "proposed"), "pm": ("manager passed", "stone"),
    "risk": ("stopped by a swing rule", "stone"), "planned": ("planned", "proposed"),
    "approved": ("approved", "executed"), "executed": ("executed", "executed"), "missed": ("missed", "stone"),
    "expired": ("expired", "stone"),
}
VERDICT_CSS = {"pass": "executed", "wait": "warn", "reject": "halted", "failed": "stone"}
DROP_WORDS = {
    "setup_paper_only": "a paper-only setup: tracked on paper, never traded",
    "skeptic_wait": "the Skeptic said wait", "skeptic_reject": "the Skeptic rejected it",
    "skeptic_failed": "the Skeptic's reply could not be used", "catalyst_misread": "the cited item does not support the claim",
    "chased": "the move since the news was already too large", "not_best_3": "not among the best three ideas",
    "budget_no_skeptic": "no Skeptic call left in the slot's budget", "budget_no_pm": "no manager call left",
    "pm_pass": "the manager passed", "swing_book_paper_only": "the swing book is paper-only for now",
    "stage_aborted": "the slot stopped before this step", "no_facts": "no fact card (stale or missing bars)",
    "catalyst_not_admitted": "the cited item was not in the slot's reading list",
    "catalyst_not_about_ticker": "the cited item is not about this company",
    "reproposal_limit": "proposed again too often after a missed entry: expired",
    "brake_on": "the swing brake is on (30-day net loss limit): no new entries until the operator lifts it",
    "brake_engaged": "new entries are paused after the Skeptic canary check failed, until the operator lifts it",
    "brake_unknown": "the swing brake could not be checked, so no new entry",
    "day2_unconfirmed": "day-2 check: the price has not yet confirmed the news, so it waits another day",
    "net_rr_below_min": "the reward is too small for the risk once trading costs are counted",
    "S6:net_rr_below_min": "the reward is too small for the risk once trading costs are counted",
    "skeptic_wait_debated": "the Skeptic said wait; the bull, bear and manager heard it and did not enter",
    "swing_budget_full": "the swing budget the council set this slot is full: no room for another entry",
    "S18:swing_budget_full": "the swing budget the council set this slot is full: no room for another entry",
}
SETUP_WORDS = {"news_continuation": "news continuation", "post_earnings_drift": "post-earnings drift",
               "second_order": "second-order effect",
               "day2_confirmation": "day-2 confirmation: a Skeptic 'wait' re-checked a day later"}
# Swing-book cycle flags (`trace_rules.SWING_FLAG_CODES`; a test checks every key has words).
SWING_FLAG_WORDS = {
    "swing_source_unavailable:*": "a swing data source had no credential, so the swing council did not run",
    "swing_source_error:*": "a swing data request failed; the ideas it fed were left out",
    "swing_eligibility_unverified": "paper run: broker eligibility could not be checked (no broker connected)",
    "swing_paper_assumed_book": "paper run: the swing rules assumed a flat book at its peak",
    "paper_reference_last_close": "paper entry priced at the last close (no fresher reference)",
    "swing_screen_missing": "no after-close movers screen for this session",
    "swing_drop:reproposal_limit": "an idea proposed again too often after a missed entry was expired",
    "swing_book_not_live": "the swing book is paper-only for now: no swing order was sent",
    "swing_paused:s15": "the swing brake is on (30-day net loss limit): new entries paused, exits continue",
    "swing_paused:canary": "new swing entries paused after the Skeptic canary check failed; exits continue",
    "swing_brake_unknown": "the swing brake could not be checked this cycle, so no new swing entry",
    "swing_brake_twice_60d": "the swing brake engaged twice in 60 days: a stop-at-once condition for review",
    "swing_exit_unapproved": "a time-stop exit went unapproved for several swing slots; the operator was alerted",
    "swing_canary_set_invalid": "the private canary event list was unreadable; the built-in past events were used",
    "day2_catalyst_gone": "a waiting idea was dropped: the news it relied on is no longer available",
    "day2_superseded": "a waiting idea was replaced by a fresh pitch on the same stock",
    "swing_wide:*": "paper test run: the Scout was allowed more ideas than usual",
    "swing_trace_all": "paper test run: every idea was sent through every stage to show the whole flow",
    "llm_billing_error": "the model provider refused a call (billing or access); the operator was alerted",
    "news_source_backoff:rss:*": "a news feed asked us to slow down, so it was skipped for the rest of the day",
    "swing_budget_fallback": "the manager gave no usable swing budget, so the last budget was kept",
}


def flag_words(flag: str) -> str:
    """The plain words of a swing flag ("" for any other flag: it shows as its code only)."""
    from council.publish.trace_rules import swing_flag_key

    key = swing_flag_key(flag)
    return SWING_FLAG_WORDS.get(key, "") if key else ""
OVERRIDE_WORDS = {"skeptic_mostly_wait": "code: mostly priced in → wait", "skeptic_stale_wait": "code: old news → wait",
                  "skeptic_prior_wait": "code: big move, no independent fact → wait",
                  "skeptic_incoherent": "code: fully priced in → reject", "catalyst_misread": "code: catalyst misread"}
EXIT_WORDS = {"stop": "stop", "target": "target", "time": "time stop", "exit": "decision", "halt": "kill switch",
              "external": "outside"}
GROUP_WORDS = {"executed": "Executed", "pm_passed": "Manager passed", "skeptic_rejected": "Skeptic rejected",
               "skeptic_wait": "Skeptic said wait", "skeptic_wait_debated": "Skeptic wait, debated (Manager passed)",
               "code_dropped": "Dropped by code (eligible names)",
               "paper_only": "Paper-only setups", "missed": "Missed entries"}
SWING_SERIES = (
    ("sq8", "SQ-8 mechanical rule (PAPER)", "c3", "SQ-8", "the stock rule we did not adopt (it failed its test)"),
    ("matched_index", "Matched index (beta x sector)", "c4a", "Matched", "the same trades' sector-and-beta benchmark"),
    ("index_hold", "Index held", "c2", "Index", "the index, held"),
)
REACTION_FACTS = (   # (field, label, unit)
    ("news_age_sessions", "news age", "sessions"), ("gap_pct", "gap", "%"),
    ("move_since_news_close_pct", "move since the news", "%"), ("move_since_news_close_sigma", "in sigma", "σ"),
    ("move_since_news_live_pct", "move today (live)", "%"), ("move_since_news_live_sigma", "live, in sigma", "σ"),
    ("vol_ratio_since", "volume vs normal", "x"), ("rel_move_since_pct", "vs sector and beta", "%"),
    ("sector_move_since_pct", "sector move", "%"), ("trend", "trend", ""), ("atr14_pct", "ATR 14", "%"),
    ("dist_52w_high_pct", "from 52-week high", "%"), ("earnings_next", "next earnings", ""),
)
SWING_WITHHELD_WORDS = {"broker_data": "withheld: broker data", "unknown_source": "withheld until the data licence is widened",
                        "licensed_series": "withheld: licensed", "not_publishable": "withheld"}
SWING_WINDOW_DAYS = 90


def swing_header(book: PublicSwingBook | None, closed: int) -> dict[str, Any]:
    """The §8.4 sentence block, built from fixed words and the document's dates and counts."""
    live_since = fmt_day(book.live_since) if book is not None and book.live_since else ""
    paper_since = fmt_day(book.paper_since) if book is not None and book.paper_since else ""
    cost = book.declared_cost_pct_per_leg if book is not None else 1.25
    return {"live_since": live_since, "paper_since": paper_since, "closed": closed,
            "cost": trim_number(cost, 2), "prereg": PREREG_URL, "live": bool(book is not None and book.live)}


def _fact_value(key: str, value: Any, unit: str) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        digits = 0 if unit == "sessions" else 2
        text = f"{value:+.{digits}f}" if unit in ("%", "σ") else f"{value:.{digits}f}"
        return f"{text}{'' if unit in ('', 'sessions') else unit if unit != 'x' else 'x'}" + (" sessions" if unit == "sessions" else "")
    return str(value).replace("_", " ")


def swing_idea_view(i: Any) -> dict[str, Any]:
    facts = []
    withheld: dict[str, list[str]] = {}
    for key, label, unit in REACTION_FACTS:
        if key in i.facts:
            facts.append({"label": label, "value": _fact_value(key, i.facts[key], unit)})
        elif key in i.facts_withheld:
            withheld.setdefault(SWING_WITHHELD_WORDS.get(i.facts_withheld[key], "withheld"), []).append(label)
    cats = []
    for c in i.catalysts:
        if c.kind == "broker_feed":
            cats.append({"label": c.id, "note": "a public RSS feed or SEC filing item, cited by id", "href": "", "css": "feed"})
        elif c.kind == "licensed_news":
            cats.append({"label": c.id, "note": f"{c.source} headline (licensed), id only", "href": "", "css": "feed"})
        elif c.kind == "public_news":
            cats.append({"label": c.title or c.id, "note": c.id, "href": c.link or "", "css": "event"})
        elif c.kind == "filing":
            label = " ".join(x for x in (c.form or "filing", ", ".join(c.items)) if x)
            cats.append({"label": label, "note": c.id, "href": "", "css": "event"})
        else:
            cats.append({"label": "movers screen", "note": c.id, "href": "", "css": "market"})
    v = i.verdict
    verdict = None
    if v is not None:
        verdict = {"word": v.verdict, "css": VERDICT_CSS.get(v.verdict, "stone"), "priced_in": v.discounted,
                   "news": v.news_status.replace("_", " "), "regime": v.regime, "crowding": v.crowding,
                   "said": v.said, "override": OVERRIDE_WORDS.get(v.code_override or "", (v.code_override or "").replace("_", " ")),
                   "reasons": v.reasons, "mind": v.what_would_change_my_mind, "same_family": v.same_family,
                   "family": v.model_family}
    stage_words, stage_css = SWING_STAGE.get(i.stage_reached, (i.stage_reached, "stone"))
    return {"i": i, "facts": facts, "withheld": sorted(withheld.items()), "cats": cats, "verdict": verdict, "stage": stage_words, "stage_css": stage_css,
            "drop": DROP_WORDS.get(i.drop_code or "", (i.drop_code or "").replace("_", " ")),
            "setup": SETUP_WORDS.get(i.setup, i.setup.replace("_", " ")), "paper": not i.live_setup}


def swing_trade_view(t: Any, geo: Geometry, scale: float) -> dict[str, Any]:
    """A trade with its stop and target on one axis (the entry in the middle): widths in % of half
    the track, `scale` = the largest distance on the page."""
    half = 50.0 / scale if scale > EPS else 0.0
    return {"t": t, "stop_cls": geo.cls("width", t.stop_pct * half), "target_cls": geo.cls("width", t.target_pct * half),
            "exit": EXIT_WORDS.get(t.exit_kind or "", ""), "closed": t.state.startswith("closed_"),
            "state": t.state.replace("_", " "), "paper": not t.live,
            "r": f"{t.r_declared:+.2f}R".replace("-", "−") if t.r_declared is not None else "—",
            "net": fmt_signed(t.net_declared_pct) if t.net_declared_pct is not None else "—",
            "contrib": fmt_bp(t.contribution_declared_bp).replace("-", "−") if t.contribution_declared_bp is not None else "—",
            "r_css": "up" if (t.r_declared or 0) > 0 else "down" if (t.r_declared or 0) < 0 else "flat"}


def _trade_scale(trades: list[Any]) -> float:
    return max([max(t.stop_pct, t.target_pct) for t in trades] + [1.0])


def health_view(h: Any) -> dict[str, Any] | None:
    if h is None:
        return None
    canary = {"caught": ("last canary caught", "executed"), "missed": ("last canary MISSED", "halted"),
              "none_yet": ("no canary yet", "stone")}[h.canary_last]
    return {"h": h, "canary": canary[0], "canary_css": canary[1],
            "pass_rate": f"{h.pass_share_20_pct:.0f}%" if h.pass_share_20_pct is not None else "—",
            "alarm": h.alarm}


def swing_run_view(sec: PublicSwingSection | None, geo: Geometry, book: PublicSwingBook | None) -> dict[str, Any] | None:
    if sec is None:
        return None
    scale = _trade_scale(sec.trades)
    closed = book.metrics.n_closed if book is not None else 0
    return {"sec": sec, "ideas": [swing_idea_view(i) for i in sec.ideas],
            "trades": [swing_trade_view(t, geo, scale) for t in sec.trades],
            "bull": sec.bull, "bear": sec.bear, "health": health_view(sec.health),
            "header": swing_header(book, closed), "paper": not sec.live, "flags": sec.flags,
            "cost": trim_number(sec.declared_cost_pct_per_leg, 2)}


def _interval_words(iv: Any, unit: str = "R") -> str:
    if iv.mean is None:
        return "—"
    text = f"{iv.mean:+.2f}{unit}"
    if iv.low is not None and iv.high is not None:
        text += f" [{iv.low:+.2f}, {iv.high:+.2f}]"
    return text


def swing_page_view(view: JournalView, geo: Geometry) -> dict[str, Any]:
    book = view.swing
    open_t = list(book.open_trades) if book is not None else []
    closed_t = list(book.closed_trades) if book is not None else []
    scale = _trade_scale(open_t)
    m = book.metrics if book is not None else None
    funnel = []
    for g in (book.funnel if book is not None else []):
        funnel.append({"g": g, "label": GROUP_WORDS.get(g.group, g.group), "r": _interval_words(g.r_declared),
                       "hit": fmt_pct1(g.hit_rate_pct) if g.hit_rate_pct is not None else "—"})
    points = [SimpleNamespace(as_of=p.day, sq8=p.sq8, matched_index=p.matched_index, index_hold=p.index_hold)
              for p in (book.benchmarks if book is not None else [])]
    chart = performance_chart(points, width=1000, height=260, spec=SWING_SERIES)
    chart_narrow = performance_chart(points, width=360, height=240, labels=False, spec=SWING_SERIES)
    runs = [cv for cv in view.cycles if cv.doc.swing is not None]
    metrics = None
    if m is not None:
        metrics = {"m": m, "expectancy": _interval_words(m.expectancy_r),
                   "hit": fmt_pct1(m.hit_rate_pct) if m.hit_rate_pct is not None else "—",
                   "payoff": f"{m.payoff:.2f}" if m.payoff is not None else "—",
                   "contrib": fmt_bp(m.contribution_declared_bp),
                   "matched": fmt_bp(m.matched_contribution_declared_bp) if m.matched_contribution_declared_bp is not None else "—",
                   "vs_matched": fmt_signed(m.vs_matched_pct) if m.vs_matched_pct is not None else "—",
                   "days": f"{m.avg_days_held:.1f}" if m.avg_days_held is not None else "—",
                   "se": f"{m.standard_error_r:.2f}R" if m.standard_error_r is not None else "—",
                   "mix": [(EXIT_WORDS.get(k, k) if k != "discretionary" else "decision", fmt_pct1(v)) for k, v in m.exit_mix_pct.items()]}
    return {"book": book, "open": [swing_trade_view(t, geo, scale) for t in open_t],
            "closed": [swing_trade_view(t, geo, 1.0) for t in reversed(closed_t)],
            "metrics": metrics, "funnel": funnel, "chart": chart, "chart_narrow": chart_narrow,
            "health": health_view(book.health if book is not None else None),
            "header": swing_header(book, m.n_closed if m is not None else 0),
            "runs": [{"cv": cv, "ideas": len(cv.doc.swing.ideas),
                      "planned": sum(1 for i in cv.doc.swing.ideas if i.stage_reached in ("planned", "approved", "executed"))}
                     for cv in runs[:20]],
            "agents": swing_agent_cards(view)}


def swing_agent_cards(view: JournalView) -> list[dict[str, Any]]:
    """The Scout's and the Skeptic's cards: job, calls and usable share over every swing meeting,
    paper and live (`role_stats`); the latest verdict from the newest meeting with a swing part."""
    out = []
    calls_all = role_calls(view)
    latest: list[tuple[datetime, int, str, Any]] = []      # (slot, decision no, href, swing ideas)
    for cv in view.cycles:
        if cv.doc.swing is not None:
            latest.append((cv.doc.slot, 0, f"cycles/{cv.doc.cycle_id}.html#swing-ideas", list(cv.doc.swing.ideas)))
    for no, doc in view.paper_cycles.items():
        if doc.swing is not None:
            latest.append((doc.slot, no, paper_href(no) + "#d-ideas", [p.idea for p in doc.swing.ideas]))
    latest.sort(key=lambda t: (t[0], t[1]), reverse=True)
    last = latest[0] if latest else None
    for spec in SWING_AGENT_SPECS:
        st = role_stats(view, spec.roles, calls_all)
        verdict = ""
        if last is not None and spec.slug == "scout":
            verdict = plural(len(last[3]), "idea")
        elif last is not None:
            words = [i.verdict.verdict for i in last[3] if i.verdict is not None]
            verdict = ", ".join(f"{words.count(w)} {w}" for w in ("pass", "wait", "reject") if words.count(w)) or "no verdict"
        out.append({"spec": spec, "slug": spec.slug, "name": spec.name, "accent": spec.accent,
                    "icon": {"scout": "file-search", "skeptic": "flask-conical"}[spec.slug], "kind": spec.kind,
                    "short": spec.short, "job": spec.job, "more": spec.more, "calls": st["calls"], "ok": st["ok"],
                    "ok_pct": st["ok_pct"], "verdict": verdict, "stats": st,
                    "last": last, "last_href": last[2] if last else "", "last_slot": last[0] if last else None,
                    "last_paper": bool(last and last[1]),
                    "source": f"{REPO_URL}/blob/main/{spec.source}", "source_path": spec.source,
                    "page": f"agents/{spec.slug}.html"})
    return out


def swing_assets(view: JournalView, now: datetime) -> dict[str, dict[str, Any]]:
    """{line id: its swing ideas and trades}, for a ticker with an idea or a trade in the last 90 days."""
    since = now - timedelta(days=SWING_WINDOW_DAYS)
    out: dict[str, dict[str, Any]] = {}
    for cv in view.cycles:
        if cv.doc.swing is None or cv.doc.slot < since:
            continue
        for i in cv.doc.swing.ideas:
            row = out.setdefault(i.ticker, {"ticker": i.ticker, "ideas": [], "trades": []})
            row["ideas"].append({"cv": cv, "v": swing_idea_view(i)})
    trades = []
    if view.swing is not None:
        trades = list(view.swing.open_trades) + list(view.swing.closed_trades)
    for t in trades:
        opened = parse_cycle_id(t.opened_cycle) if t.opened_cycle else None
        if t.state.startswith("closed_") and (opened is None or opened < since):
            continue
        row = out.setdefault(t.ticker, {"ticker": t.ticker, "ideas": [], "trades": []})
        row["trades"].append(swing_trade_view(t, Geometry(), 1.0))
    return out


# ------------------------------------------------------------------------------ rendering
# ------------------------------------------------------------------------------ record page
# "How it's doing, and what went wrong" (approved 2026-10-01, SCOREBOARD FIRST): the paper book's
# headline numbers with their sample size, the paper book against its controls, the idea funnel and
# the Skeptic's health over every paper decision, then the honesty log (incidents, smaller problems
# found in the logs, withdrawn claims). Percent and counts only.
SWING_MIN_CLOSED = 20        # docs/swing-book-prereg.md §6: the first review with >= 20 closed trades
CONTROLS_CHART_MIN = 10      # fewer daily points than this: a small table, not a chart
RECORD_SERIES = (
    ("paper", "Paper book (PAPER)", "c0", "Paper", "the paper book after its declared costs"),
    *SWING_SERIES,
)
FUNNEL_STOPS = (   # (stop index, key, who stopped it, seat colour)
    (0, "scout", "the Scout check", "scout"), (1, "gate", "the code gate", "risk"),
    (2, "skeptic", "the Skeptic", "skeptic"), (3, "debate", "the debate", "bull"),
    (4, "pm", "the manager", "pm"), (5, "rules", "the swing rules", "risk"),
    (6, "entered", "entered on paper", "entered"),
)
FUNNEL_STAGES = (  # (label, minimum stop index to count as having got this far)
    ("Ideas pitched", 0), ("Past the code gate", 2), ("Past the Skeptic", 3),
    ("Reached the manager", 4), ("Manager said enter", 5), ("Passed the rules · entered", 6),
)
SKEPTIC_SHARES = (("pass", "pass", "executed"), ("wait", "wait", "warn"), ("reject", "reject", "halted"),
                  ("failed", "no usable verdict", "stone"))
# cycle flags that mean an input was degraded or a step fell back (the "smaller problems" rows)
MINOR_FLAG_WORDS = {
    "calendar:release_dates_skipped_no_fred_key": CALENDAR_UNLOADED,
    "paper_no_reference": "an idea could not be tracked on paper (no reference price), so its outcome is not measured",
    "llm_billing_error": "the model provider refused a call (billing or access)",
    "swing_budget_fallback": "the manager gave no usable swing budget, so the last budget was kept",
}
MINOR_FLAG_PREFIXES = {
    "news_source_error:": "a news source failed and its items were left out",
    "news_source_backoff:": "a news feed asked us to slow down, so it was skipped for the rest of the day",
    "swing_source_error:": "a swing data request failed; the ideas it fed were left out",
    "calendar:release_dates_failed:": "some economic-release dates could not be loaded",
}


def _minor_flag(flag: str) -> str:
    if flag in MINOR_FLAG_WORDS:
        return MINOR_FLAG_WORDS[flag]
    return next((w for p, w in MINOR_FLAG_PREFIXES.items() if flag.startswith(p)), "")


def _unusable_words(calls: list[Any]) -> str:
    bad = [c for c in calls if c.status != "ok"]
    if not bad:
        return ""
    roles = ", ".join(dict.fromkeys(ROLE_WORDS_SHORT.get(c.role, c.role.replace("_", " ")) for c in bad))
    return f"{plural(len(bad), 'model reply', 'model replies')} of {len(calls)} unusable ({roles})"


ROLE_WORDS_SHORT = {"skeptic": "Skeptic", "scout": "Scout", "pm": "manager", "swing_pm": "swing manager",
                    "news": "news", "macro": "macro", "bull_open": "bull", "bull_rebuttal": "bull", "bear": "bear",
                    "swing_bull": "swing bull", "swing_bear": "swing bear", "single_agent": "single agent"}


def minor_problems(view: JournalView) -> list[dict[str, Any]]:
    """Smaller problems read straight from the logs, newest first: late starts, unusable model
    replies, a fallback basis, withheld runs and degraded inputs, for live runs (journal/ops) and
    paper decisions (journal/paper)."""
    rows: list[dict[str, Any]] = []
    kinds = {cv.doc.cycle_id: ("REHEARSAL" if cv.rehearsal else "LIVE") for cv in view.cycles}
    for r in view.ops:
        what = []
        if r.late_by_min > 0:
            what.append(f"started {fmt_late(r.late_by_min)} late")
        bad = r.calls - r.calls_ok
        if bad > 0:
            what.append(f"{plural(bad, 'model reply', 'model replies')} of {r.calls} unusable"
                        + (f" ({r.parse_fail} unreadable, {r.timeouts} timed out)" if r.parse_fail and r.timeouts else ""))
        if r.basis and r.basis != "council":
            what.append(f"fell back to the {str(r.basis).replace('_', ' ')}")
        what += [w for w in dict.fromkeys(_minor_flag(f) for f in r.flags) if w]
        if what:
            rows.append({"slot": r.slot, "badge": kinds.get(r.cycle_id, "LIVE"), "label": f"Run {fmt_when(r.slot)}",
                         "href": f"cycles/{r.cycle_id}.html" if r.cycle_id in kinds else "", "what": what})
    for no, doc in view.paper_cycles.items():
        c, sw = doc.core, doc.swing
        what = []
        if c is not None and c.late_by_min > 0:
            what.append(f"started {fmt_late(c.late_by_min)} late")
        calls = list(c.calls) if c is not None else []
        if (u := _unusable_words(calls)):
            what.append(u)
        if c is not None and c.basis and c.basis != "council":
            what.append(f"fell back to the {str(c.basis).replace('_', ' ')}")
        if sw is not None and sw.budget is not None and sw.budget.fallback:
            what.append(MINOR_FLAG_WORDS["swing_budget_fallback"])
        flags = list(doc.flags) + (list(c.flags) if c is not None else []) + (list(sw.flags) if sw is not None else [])
        what += [w for w in dict.fromkeys(_minor_flag(f) for f in flags) if w and w not in what]
        if what:
            rows.append({"slot": doc.slot, "badge": "PAPER", "label": f"Decision #{no} · {fmt_when(doc.slot)}", "href": paper_href(no),
                         "what": what})
    return sorted(rows, key=lambda x: x["slot"], reverse=True)


def _paper_ideas(view: JournalView) -> list[tuple[int, Any, Any]]:
    return [(no, doc, p) for no, doc in sorted(view.paper_cycles.items()) if doc.swing is not None
            for p in doc.swing.ideas]


def idea_funnel(view: JournalView, geo: Geometry) -> dict[str, Any] | None:
    """Where every paper idea really stopped (not the traced run): stage counts with bar widths, and
    one group per seat that stopped ideas, each idea linked to its card on the decision page."""
    ideas = _paper_ideas(view)
    if not ideas:
        return None
    total = len(ideas)
    stops = [(no, p, _stop_index(p)) for no, _, p in ideas]
    stages = []
    for label, need in FUNNEL_STAGES:
        n = sum(1 for _, _, s in stops if s >= need)
        stages.append({"label": label, "n": n, "w": geo.cls("width", 100.0 * n / total), "share": f"{100.0 * n / total:.0f}%"})
    groups = []
    for idx, key, who, seat in FUNNEL_STOPS:
        mine = [(no, p) for no, p, s in stops if s == idx]
        if not mine:
            continue
        items = []
        for no, p in mine:
            o = p.real_outcome
            code = (o.code if o is not None else None) or p.idea.drop_code or ""
            reason = code_words(code) if idx < 6 else "a paper leg was built"
            if idx == 2 and p.idea.verdict is not None:
                vv = p.idea.verdict.verdict
                reason = ("the Skeptic's reply could not be used" if vv == "failed"
                          else f"the Skeptic {VERDICT_WORDS.get(vv, vv)}")
            items.append({"no": no, "ticker": p.idea.ticker, "side": p.idea.side, "reason": reason, "code": code if idx < 6 else "",
                          "href": paper_href(no) + "#sw-" + p.idea.ref.replace(":", "-")})
        groups.append({"key": key, "who": who, "seat": seat, "n": len(mine), "ideas": items,
                       "w": geo.cls("width", 100.0 * len(mine) / total)})
    traced = any(doc.swing is not None and doc.swing.trace_all for _, doc, _ in ideas)
    return {"total": total, "stages": stages, "groups": groups, "traced": traced,
            "decisions": len({no for no, _, _ in ideas})}


def skeptic_health(view: JournalView, geo: Geometry) -> dict[str, Any] | None:
    """The Skeptic's verdict shares over every paper idea it judged, and its unusable replies."""
    verdicts = [p.idea.verdict.verdict for _, _, p in _paper_ideas(view) if p.idea.verdict is not None]
    calls = [x for doc in view.paper_cycles.values() if doc.core is not None for x in doc.core.calls if x.role == "skeptic"]
    if not verdicts and not calls:
        return None
    n = len(verdicts)
    shares = []
    for key, label, css in SKEPTIC_SHARES:
        k = sum(1 for v in verdicts if v == key)
        shares.append({"key": key, "label": label, "css": css, "n": k,
                       "pct": f"{100.0 * k / n:.0f}%" if n else "—", "w": geo.cls("width", 100.0 * k / n if n else 0.0)})
    bad = sum(1 for x in calls if x.status != "ok")
    return {"n": n, "shares": [s for s in shares if s["n"]], "calls": len(calls), "bad": bad,
            "bad_pct": f"{100.0 * bad / len(calls):.0f}%" if calls else "—"}


def record_controls(view: JournalView) -> dict[str, Any]:
    """The paper book (base 100 after its declared costs, one point per day: its last decision)
    against the swing controls (SQ-8 paper rule, matched index, index held)."""
    days: dict[date, dict[str, float | None]] = {}
    books = sorted(view.paper_books.values(), key=lambda b: b.as_of)
    if view.paper_latest is not None and view.paper_latest.book is not None:
        books.append(SimpleNamespace(as_of=view.paper_latest.as_of, book=view.paper_latest.book))
    for b in books:
        days.setdefault(b.as_of.date(), {})["paper"] = 100.0 + b.book.paper_return_pct
    for p in (view.swing.benchmarks if view.swing is not None else []):
        d = days.setdefault(p.day, {})
        d.update(sq8=p.sq8, matched_index=p.matched_index, index_hold=p.index_hold)
    points = [SimpleNamespace(as_of=d, **{k: v.get(k) for k, *_ in RECORD_SERIES}) for d, v in sorted(days.items())]
    have = {k for p in points for k, *_ in RECORD_SERIES if getattr(p, k) is not None}
    chart = chart_narrow = None
    if len(points) >= CONTROLS_CHART_MIN:
        chart = performance_chart(points, width=1000, height=260, spec=RECORD_SERIES)
        chart_narrow = performance_chart(points, width=360, height=240, labels=False, spec=RECORD_SERIES)
    rows = [{"day": fmt_day(p.as_of), "vals": [f"{getattr(p, k):.1f}" if getattr(p, k) is not None else "—"
                                               for k, *_ in RECORD_SERIES]} for p in points[-10:][::-1]]
    return {"n": len(points), "need": CONTROLS_CHART_MIN, "chart": chart, "chart_narrow": chart_narrow, "rows": rows,
            "series": [{"key": k, "label": name, "css": css, "what": what, "has": k in have}
                       for k, name, css, _, what in RECORD_SERIES]}


def record_view(view: JournalView, geo: Geometry) -> dict[str, Any]:
    latest = view.paper_latest
    book = latest.book if latest is not None else None
    head = None
    if latest is not None or view.paper_rows:
        n_dec = len(view.paper_rows) or (latest.decisions if latest is not None else 0)
        trades = list(book.swing_trades) if book is not None else []
        closed = sum(1 for t in trades if t.status == "closed")
        ret = book.paper_return_pct if book is not None else None
        cost = book.cost_pct if book is not None and book.cost_pct is not None else None
        started = book.started if book is not None else None
        as_of = latest.as_of.date() if latest is not None else None
        head = {"decisions": n_dec, "ret": fmt_signed(ret) if ret is not None else "—",
                "up": ret is not None and ret >= 0,
                "market": fmt_signed(ret + cost) if ret is not None and cost is not None else "—",
                "market_up": ret is not None and cost is not None and ret + cost >= 0,
                "cost": fmt_signed(-cost) if cost is not None else "—",
                "open": sum(1 for t in trades if t.status == "open"), "closed": closed, "need": SWING_MIN_CLOSED,
                "thin": closed < SWING_MIN_CLOSED,
                "days": (as_of - started).days if started and as_of else None,
                "since": fmt_day(started) if started else "—", "as_of": fmt_when(latest.as_of) if latest else "—"}
    return {"head": head, "controls": record_controls(view), "funnel": idea_funnel(view, geo),
            "skeptic": skeptic_health(view, geo), "minor": minor_problems(view),
            "swing_page": view.has_swing}


# ------------------------------------------------------------------------------ paper decisions
SCREEN_MOVE_WORDS = {"down_3s": "down sharply (3σ or more)", "down_2s": "down (2–3σ)", "down": "down (under 2σ)",
                     "flat": "flat (under 0.5σ)", "up": "up (under 2σ)", "up_2s": "up (2–3σ)", "up_3s": "up sharply (3σ or more)"}
PAPER_STATUS = ("PAPER · NO BROKER", "rehearsal")     # the header chip on paper pages (and site-wide while paper runs lead)
DECISIONS_NAV = {"key": "decisions", "href": "decisions/index.html", "label": "Decisions"}
PAPER_STAGE = {"scout": "Scout check", "gate": "code gate", "skeptic": "Skeptic", "pm": "manager",
               "rules": "S-rules", "leg": "paper leg", "unknown": "not recorded"}


def outcome_view(o: Any) -> dict[str, str]:
    """A real / traced outcome in words and a chip colour (never colour alone)."""
    if o is None:
        return {"text": "—", "css": "stone"}
    stage = PAPER_STAGE.get(o.stage, o.stage)
    if o.stage == "leg":
        return {"text": "paper leg (would trade)", "css": "executed", "note": o.note}
    if o.stage == "pm" and o.code == "enter":
        return {"text": "manager: enter", "css": "proposed", "note": o.note}
    code = (o.code or "passed").replace("_", " ")
    return {"text": f"{stage}: {code}", "css": "halted" if o.stage in ("skeptic", "rules") else "stone",
            "note": o.note}


def leg_view(leg: Any) -> dict[str, Any] | None:
    if leg is None:
        return None
    if not leg.ok:
        return {"ok": False, "text": " ".join(x for x in ((leg.rule or ""), (leg.code or "dropped").replace("_", " ")) if x)}
    return {"ok": True, "size": "—" if leg.size_nav_pct is None else f"{leg.size_nav_pct:g}%",
            "stop": "—" if leg.stop_pct is None else f"{leg.stop_pct:g}%",
            "target": "—" if leg.target_pct is None else f"{leg.target_pct:g}%",
            "time": fmt_day(leg.time_stop_date) if leg.time_stop_date else (
                f"{leg.time_stop_days} sessions" if leg.time_stop_days is not None else "—")}


def paper_href(no: int) -> str:
    return f"decisions/{no}/index.html"


def chosen_words(chosen: list[Any]) -> str:
    return ", ".join(f"{c.ticker.replace('_', '.')} {c.side}" + (f" {c.size_nav_pct:g}%" if c.size_nav_pct is not None else "")
                     for c in chosen) or "no trade"


def decision_rows(view: JournalView) -> list[dict[str, Any]]:
    """The numbered history, newest first: every paper decision (#n) and every live run with a
    swing part (no number: live runs are listed on the Runs page)."""
    rows = []
    for r in view.paper_rows:
        rows.append({"no": r.decision_no, "slot": r.slot, "badge": "PAPER", "ideas": r.ideas,
                     "chosen": chosen_words(list(r.chosen)), "why": r.why, "href": paper_href(r.decision_no),
                     "verified": view.paper_verified.get(r.decision_no, False)})
    for cv in view.cycles:
        sec = cv.doc.swing
        if sec is None or cv.doc.mode != "live":
            continue
        chosen = [i for i in sec.ideas if i.stage_reached in ("planned", "approved", "executed")]
        rows.append({"no": None, "slot": cv.doc.slot, "badge": "LIVE", "ideas": len(sec.ideas),
                     "chosen": ", ".join(f"{i.ticker.replace('_', '.')} {i.side}" for i in chosen) or "no trade",
                     "why": f"{len(sec.ideas)} idea(s) reviewed", "href": f"cycles/{cv.doc.cycle_id}.html",
                     "verified": cv.verified})
    rows.sort(key=lambda r: r["slot"], reverse=True)
    return rows


def _hold_words(h: str) -> dict[str, str]:
    m = HOLD_LINE.match(h)
    line, rest = (m.group(1), m.group(2)) if m else ("", h)
    text = SKIP_WORDS.get(rest.strip()) or plain_hold(rest)
    if re.fullmatch(r"R\d+[a-z]?", text):
        text = "held by the risk engine's rule " + text
    return {"line": line, "text": text, "raw": h}


def paper_core_view(c: PublicCycleV1, lines: Lines) -> dict[str, Any]:
    r = c.risk
    moves = []
    if r is not None:
        for k in lines.sort(set(r.final_x) | set(r.base_x)):
            before, after = r.base_x.get(k, 0.0), r.final_x.get(k, 0.0)
            prop = r.proposed_x.get(k)
            moves.append({"line": k, "before": fmt_pct1(before * 100.0), "after": fmt_pct1(after * 100.0),
                          "proposed": fmt_pct1(prop * 100.0) if prop is not None else "—",
                          "moved": abs(after - before) > EPS})
    pm = c.pm
    return {"basis": BASIS_WORDS.get(c.basis or "", (c.basis or "—").replace("_", " ")), "moves": moves,
            "moved": sum(1 for m in moves if m["moved"]), "hold": list(r.hold_reasons) if r else [],
            "holds": [_hold_words(h) for h in (r.hold_reasons if r else [])],
            "macro": c.macro.regime.replace("_", " ") if c.macro else None,
            "bull": c.debate.bull.argument if c.debate.bull else "", "bear": c.debate.bear.argument if c.debate.bear else "",
            "reps": [{"n": x.replicate, "valid": x.valid, "sided": x.sided_with or "—",
                      "fact": x.decisive_fact.text if x.decisive_fact else "", "why": x.no_change_reason,
                      "devs": [f"{d.line} {d.direction}: {d.reason}" for d in x.deviations]}
                     for x in pm.replicates],
            "valid": pm.valid_replicates, "cards": len(c.cards)}


# live-only stage codes: on a paper decision the idea card shows the paper stage instead
LIVE_ONLY_DROPS = frozenset({"swing_book_paper_only", "swing_book_not_live"})


def paper_idea_view(p: Any) -> dict[str, Any]:
    """`swing_idea_view` for an idea of a paper decision: the stage chip is the paper outcome (the
    idea's real outcome on this paper run), and the live-only "the swing book is paper-only" stage is
    not shown (every paper decision is paper)."""
    v = swing_idea_view(p.idea)
    out = outcome_view(p.real_outcome)
    if p.real_outcome is not None:
        v["stage"], v["stage_css"] = f"paper: {out['text']}", out["css"]
    if (p.idea.drop_code or "") in LIVE_ONLY_DROPS:
        v["drop"] = ""
        if p.real_outcome is None:
            v["stage"], v["stage_css"] = "paper: accepted", "proposed"
    return v


def paper_book_home(book: Any, as_of: datetime | None, geo: Geometry) -> dict[str, Any] | None:
    """The paper BOOK (`PaperBookView`) as THE portfolio: one holdings table (core lines + open swing
    trades, weight bars), the core / swing / cash split bar, the paper return since start and the
    closed paper swing trades. Percent only; widths are Geometry classes (no inline style)."""
    if book is None:
        return None
    open_ = [t for t in book.swing_trades if t.status == "open"]
    weights = [abs(h.weight_pct) for h in book.core] + [abs(t.weight_pct) for t in open_]
    scale = max([*weights, 1.0])
    rows = []
    for h in sorted(book.core, key=lambda h: -abs(h.weight_pct)):
        rows.append({"kind": "core", "seat": "pm", "line": h.line, "side": "long" if h.weight_pct >= 0 else "short",
                     "weight": fmt_pct1(h.weight_pct), "bar": geo.cls("width", 100.0 * abs(h.weight_pct) / scale),
                     "detail": "core line", "ret": None})
    for t in sorted(open_, key=lambda t: -abs(t.weight_pct)):
        rows.append({"kind": "swing", "seat": "scout", "line": t.ticker, "side": t.side,
                     "weight": fmt_pct1(t.weight_pct), "bar": geo.cls("width", 100.0 * abs(t.weight_pct) / scale),
                     "detail": " · ".join(x for x in (SETUP_WORDS.get(t.setup or "", (t.setup or "").replace("_", " ")),
                                                      f"stop {t.stop_pct:g}%", f"target {t.target_pct:g}%",
                                                      plural(t.days_held, "day") + " held") if x),
                     "ret": fmt_signed(t.return_net_pct)})
    parts = [("core", "Core", max(book.core_pct, 0.0)), ("swing", "Swing", max(book.swing_pct, 0.0)),
             ("cash", "Cash", max(book.cash_pct, 0.0))]
    total = sum(v for _, _, v in parts) or 1.0
    split = [{"key": k, "label": label, "text": fmt_pct1(v), "w": geo.cls("width", 100.0 * v / total)}
             for k, label, v in parts]
    closed = [{"line": t.ticker, "side": t.side, "ret": fmt_signed(t.return_net_pct), "days": t.days_held,
               "why": EXIT_WORDS.get(t.exit_reason or "", (t.exit_reason or "closed").replace("_", " "))}
              for t in book.swing_trades if t.status == "closed"]
    cost = getattr(book, "cost_pct", None)
    parts_ret = None
    if cost is not None:     # the return = market move - declared costs paid (percent only)
        parts_ret = {"market": fmt_signed(book.paper_return_pct + cost), "cost": fmt_signed(-cost),
                     "market_up": book.paper_return_pct + cost >= 0}
    return {"rows": rows, "split": split, "closed": closed[-10:][::-1], "ret": fmt_signed(book.paper_return_pct),
            "ret_parts": parts_ret,
            "up": book.paper_return_pct >= 0, "since": fmt_day(book.started) if book.started else "—",
            "as_of": as_of}


def decision_view(doc: PublicPaperCycle, verified: bool, lines: Lines) -> dict[str, Any]:
    sw = doc.swing
    ideas = []
    for i in (sw.ideas if sw else []):
        ideas.append({"p": i, "v": paper_idea_view(i), "real": outcome_view(i.real_outcome),
                      "traced": outcome_view(i.traced_outcome), "leg": leg_view(i.leg),
                      "tleg": leg_view(i.traced_leg)})
    chosen = [x for x in ideas if x["p"].chosen]
    reading = []
    for r in (sw.inputs.reading if sw else []):
        it = r.item
        if it.kind in ("licensed_news", "broker_feed"):
            label, note = it.id, (f"{it.source} headline (licensed): id and source only" if it.source
                                  else "licensed feed item: id only")
        elif it.kind == "filing":
            label, note = " ".join(x for x in (it.form or "filing", ", ".join(it.items)) if x), it.id
        else:
            label, note = it.title or it.id, it.id
        reading.append({"label": label, "note": note, "href": it.link or "", "age": f"{r.age_h:g} h",
                        "tickers": ", ".join(t.replace("_", ".") for t in r.tickers) or "market-wide",
                        "licensed": it.kind in ("licensed_news", "broker_feed")})
    catalog = _catalog(sw) if sw else {}
    journeys = [journey_view(x["p"], sw, catalog, lines) for x in ideas]
    journeys.sort(key=lambda j: (-j["stop"], -(j["votes"][0] if j["votes"] else -1)))
    cited = {c.id for x in ideas for c in x["p"].idea.catalysts}
    cited |= {e for x in ideas if x["p"].idea.verdict for r in x["p"].idea.verdict.reasons for e in r.evidence}
    cited |= {e for b in (sw.batches if sw else []) for case in (b.bull, b.bear) if case for c in case.claims
              for e in c.evidence}
    for r, src in zip(reading, (sw.inputs.reading if sw else []), strict=True):
        r["cited"] = src.item.id in cited
        r["id"] = src.item.id
        r["source"] = src.item.source or ("broker feed" if src.item.kind == "broker_feed" else "")
    reading.sort(key=lambda r: not r["cited"])
    counts: dict[str, int] = {}
    for r in reading:
        key = ("licensed headline" if r["licensed"] else "SEC filing" if r["id"].startswith("S:")
               else r["source"] or "public item")
        counts[key] = counts.get(key, 0) + 1
    core = paper_core_view(doc.core, lines)
    cl = core_line(doc.core, lines)
    return {"doc": doc, "sw": sw, "ideas": ideas, "chosen": chosen, "reading": reading, "run_href": "",
            "core": core, "core_line": cl, "verified": verified, "journeys": journeys,
            "banner": verdict_banner(journeys, cl, sw),
            "inputs": {"counts": sorted(counts.items(), key=lambda t: -t[1]),
                       "licensed": sum(1 for r in reading if r["licensed"]),
                       "cited": sum(1 for r in reading if r["cited"]), "total": len(reading)},
            "differ": sum(1 for x in ideas if not x["p"].same)}


# ---------------------------------------------------------------- decision page: journey cards
# Each idea's way through the swing pipeline as six seat-coloured steps (Scout -> Gate -> Skeptic ->
# Debate -> PM -> Rules); the step where it stopped carries its plain reason. Stage order for "the
# idea that got furthest": Scout < Gate < Skeptic < Debate < PM < Rules < Entered (ties by PM votes).
JOURNEY_STEPS = (   # (key, label, seat)
    ("scout", "Scout", "scout"), ("gate", "Gate", "risk"), ("skeptic", "Skeptic", "skeptic"),
    ("debate", "Debate", "bull"), ("pm", "PM", "pm"), ("rules", "Rules", "risk"),
)
JOURNEY_NAMES = {"scout": "the Scout check", "gate": "the code gate", "skeptic": "the Skeptic", "debate": "the debate",
                 "pm": "the manager", "rules": "the swing rules", "entered": "entered"}
_OUTCOME_STOP = {"scout": 0, "gate": 1, "skeptic": 2, "pm": 4, "rules": 5, "leg": 6}
_STAGE_STOP = {"dropped_by_code": 1, "skeptic": 2, "waiting": 2, "debate": 3, "pm": 4, "risk": 5, "planned": 6,
               "approved": 6, "executed": 6, "missed": 5, "expired": 5}
RULE_CODE_WORDS = {   # swing rule codes (`council.swing.rules.RULE_OF`) in plain words
    "stop_too_wide_for_size": "the stop is too wide for the position size",
    "max_open": "the book already holds the most swing trades allowed", "max_short": "the book holds the most shorts allowed",
    "already_open": "a swing trade on this stock is already open", "weekly_cap": "the weekly cap on new swing trades is reached",
    "open_risk": "the open risk across swing trades is at its limit", "stop_missing": "no stop level",
    "stop_out_of_range": "the stop is outside the allowed range", "stop_inside_atr": "the stop sits inside one day's normal range",
    "atr_unknown": "the stock's normal daily range is unknown, so the stop cannot be checked",
    "target_missing": "no target level", "vol_unknown": "the stock's volatility is unknown",
    "vol_too_high": "the stock is too volatile", "target_too_small": "the target is too small",
    "target_beyond_vol": "the target is further than the stock usually moves in the time allowed",
    "cost_unavailable": "the trading cost could not be priced", "time_stop_out_of_range": "the time stop is out of range",
    "illiquid": "too little trading volume", "price_too_low": "the share price is too low",
    "earnings_window": "earnings fall inside the trade's window", "earnings_window_estimated": "estimated earnings fall inside the trade's window",
    "post_earnings_wait": "too soon after earnings", "bucket_full": "the book is already full of similar trades",
    "swing_net_beta": "the swing book's market exposure is at its limit", "fee_budget": "over the fee budget",
    "short_new_listing": "a recent listing cannot be shorted", "short_crowded": "too crowded a short",
    "short_takeover_target": "a takeover target cannot be shorted", "short_into_flush": "a short into a flush is not allowed",
    "short_squeeze_risk": "squeeze risk on a short", "cooloff": "a cool-off after a recent loss on this stock",
    "swing_entry_ran": "the price ran past the entry", "swing_entry_stopped": "the price hit the stop before entry",
    "expired": "the approval expired", "kill_state": "the risk engine's kill switch blocks new entries",
    "drawdown_unknown": "the drawdown could not be checked", "swing_blocker": "a blocker is open",
    "vehicle_owned_by_core": "the core council already holds this stock",
    "enter": "the manager entered, but no paper leg was built", "paper_leg": "a paper leg was built",
}
SWING_FIELD_WORDS = {   # fact-card fields not in REACTION_FACTS
    "move_since_news_close_pct": "move since the news", "move_since_news_close_sigma": "move since the news (σ)",
    "move_since_news_live_pct": "live move since the news", "move_since_news_live_sigma": "live move since the news (σ)",
    "move_today_live_sigma": "move today (live, σ)", "vol_ratio_since": "volume since the news vs normal",
    "rel_move_since_pct": "move vs sector and beta", "sector_move_since_pct": "sector move since the news",
    "news_age_sessions": "news age (sessions)", "filing_age_d": "filing age (days)", "fundamentals_age_d": "latest results age (days)",
    "earnings_confirmed": "earnings date confirmed", "earnings_last_sessions_ago": "sessions since last earnings",
    "earnings_next": "next earnings", "crowding": "crowding", "sector_etf": "sector fund", "rev_yoy": "revenue vs a year ago",
    "rev_accel": "revenue growth change", "gm_chg": "gross margin change", "om_chg": "operating margin change",
    "short_interest_pct_float": "short interest", "days_to_cover": "days to cover", "beta_60d": "beta (60 days)",
    "ret_5d": "5-day return", "ret_20d": "20-day return", "ret_60d": "60-day return", "sigma_daily": "daily volatility",
    "vol_ratio_last": "last day's volume vs normal", "spx_move_since_pct": "S&P 500 since the news",
    "ndx_move_since_pct": "Nasdaq 100 since the news", "dist_52w_low_pct": "from 52-week low",
    "move_today_live_pct": "move today (live)",
    "adv_bucket": "trading volume bucket", "px_ge_10": "share price at least 10", "corr_60d_max": "highest correlation",
}
SWING_F_WORDS = {"ret1d_sigma": "1-day move", "mom10d": "10-day change", "mom63d": "3-month change",
                 "dd52": "drop from 1-year high", "dist_sma50": "vs 50-day average", "dist_sma200": "vs 200-day average",
                 "trend": "trend", "data_age_h": "data age", "market_open": "market open"}
BUCKET_ROWS = (   # (key, label, {word: (plain, css)})
    ("reaction", "Reaction since the news", {"strongly_against": ("strongly against the idea (≤ −2σ)", "down"),
                                             "against": ("against the idea", "down"),
                                             "flat": ("flat (under 0.5σ either way)", "flat"),
                                             "with": ("with the idea", "up"),
                                             "strongly_with": ("strongly with the idea (≥ 2σ)", "up")}),
    ("volume", "Volume since the news", {"normal": ("normal", "flat"), "elevated": ("elevated (≥ 1.5× normal)", "warn"),
                                         "climax": ("climax (≥ 3× normal)", "warn")}),
    ("vs_sector", "Versus its sector", {"lagging": ("lagging", "down"), "in_line": ("in line", "flat"),
                                        "leading": ("leading", "up")}),
    ("trend", "Trend", {"up": ("up", "up"), "down": ("down", "down"), "mixed": ("mixed", "flat")}),
    ("range_52w", "52-week position", {"near_high": ("near the high", "up"), "mid": ("mid-range", "flat"),
                                       "near_low": ("near the low", "down")}),
)
VERDICT_WORDS = {"pass": "passed it", "wait": "said wait", "reject": "rejected it", "failed": "reply unusable"}


def code_words(code: str | None) -> str:
    """A drop / rule / outcome code in plain words (`S8:illiquid` -> "too little trading volume")."""
    if not code:
        return ""
    bare = code.split(":", 1)[1] if re.match(r"^S?[A-Z]*\d+[a-z]?:", code) else code
    return (DROP_WORDS.get(code) or DROP_WORDS.get(bare) or RULE_CODE_WORDS.get(bare)
            or bare.replace("_", " "))


def swing_ev(eid: str, catalog: dict[str, dict[str, str]], lines: Lines) -> dict[str, str]:
    """A swing evidence id as a readable chip: {label, raw (title attribute), href, css}."""
    parts = eid.split(":")
    prefix = parts[0]
    if prefix == "X" and len(parts) >= 3:
        field_ = parts[2]
        label = SWING_FIELD_WORDS.get(field_) or next((lab + (f" ({u})" if u in ("sessions",) else "")
                                                       for k, lab, u in REACTION_FACTS if k == field_), field_.replace("_", " "))
        return {"label": label, "raw": eid, "href": "", "css": "market"}
    if prefix == "F" and len(parts) >= 3:
        what = SWING_F_WORDS.get(parts[2]) or MARKET_FIELDS.get(parts[2], parts[2].replace("_", " "))
        return {"label": f"{ticker(parts[1])} {what}", "raw": eid, "href": "", "css": "market"}
    if prefix in ("P", "S"):
        c = catalog.get(eid)
        if c:
            return {"label": c["label"], "raw": eid, "href": c.get("href", ""), "css": "event"}
        return {"label": "public filing" if prefix == "S" else "public news item", "raw": eid, "href": "", "css": "event"}
    if prefix == "N":
        src = (catalog.get(eid) or {}).get("source", "")
        return {"label": "licensed headline" + (f" · {src}" if src else ""), "raw": f"{eid} · licensed: id and source only",
                "href": "", "css": "feed"}
    if prefix == "M":
        return {"label": "movers screen row", "raw": eid, "href": "", "css": "market"}
    lab = evidence_label(SimpleNamespace(id=eid, kind=""), lines)
    return {"label": lab["label"], "raw": lab["raw"], "href": "", "css": lab["css"]}


def _catalog(sw: Any) -> dict[str, dict[str, str]]:
    """{evidence id: label/href/source} from the reading list and every idea's catalysts."""
    out: dict[str, dict[str, str]] = {}
    items = [r.item for r in sw.inputs.reading] + [c for i in sw.ideas for c in i.idea.catalysts]
    for it in items:
        if it.kind in ("licensed_news", "broker_feed"):
            out.setdefault(it.id, {"label": "licensed headline", "source": it.source or "broker feed"})
        elif it.kind == "filing":
            out.setdefault(it.id, {"label": " ".join(x for x in (it.form or "filing", ", ".join(it.items)) if x)})
        elif it.kind == "public_news":
            out.setdefault(it.id, {"label": it.title or "public news item", "href": it.link or ""})
    return out


def _pm_votes(ref: str, batches: list[Any]) -> tuple[int, int] | None:
    for b in batches:
        for t in b.tally:
            if t.ref == ref and t.action == "enter":
                return t.votes_for, t.replicates
    for b in batches:
        if any(t.ref == ref for t in b.tally):
            n = next(t.replicates for t in b.tally if t.ref == ref)
            return 0, n
    return None


def _stop_index(p: Any) -> int:
    if p.leg is not None and p.leg.ok:
        return 6
    o = p.real_outcome
    if o is not None and o.stage in _OUTCOME_STOP:
        if o.stage == "pm" and o.code == "enter":
            return 5
        return _OUTCOME_STOP[o.stage]
    return _STAGE_STOP.get(p.idea.stage_reached, 1)


def journey_view(p: Any, sw: Any, catalog: dict[str, dict[str, str]], lines: Lines) -> dict[str, Any]:
    """One idea's journey card: the six steps with their state and the content behind each."""
    i = p.idea
    ref = i.ref
    stop = _stop_index(p)
    o = p.real_outcome
    votes = _pm_votes(ref, sw.batches)
    code = (o.code if o is not None else None) or i.drop_code
    if stop == 5 and p.leg is not None and not p.leg.ok:
        code = f"{p.leg.rule}:{p.leg.code}" if p.leg.rule and p.leg.code else (p.leg.code or code)
    reason = code_words(code) if stop < 6 else ""
    if stop == 2 and i.verdict is not None and (code or "").split(":")[-1] in ("", "skeptic_reject", "skeptic_wait",
                                                                                "skeptic_failed"):
        reason = f"the Skeptic {VERDICT_WORDS.get(i.verdict.verdict, i.verdict.verdict)}"
    ev = lambda e: swing_ev(e, catalog, lines)                              # noqa: E731
    sub = {"scout": "pitched", "gate": "passed", "skeptic": (i.verdict.verdict if i.verdict else "—"),
           "debate": "argued", "pm": (f"{votes[0]}/{votes[1]}" if votes else "—"), "rules": "pass"}
    steps = []
    for n, (key, label, seat) in enumerate(JOURNEY_STEPS):
        state = "ok" if n < stop else "stop" if n == stop else "skip"
        word = sub[key] if state != "skip" else "not reached"
        if state == "stop":
            rule = (p.leg.rule if p.leg is not None and p.leg.rule else
                    code.split(":", 1)[0] if code and re.match(r"^S\d+:", code) else "stopped")
            word = {"gate": "stopped", "rules": rule, "debate": "stopped"}.get(key, word)
        steps.append({"key": key, "label": label, "seat": seat, "state": state, "word": word,
                      "glyph": {"ok": "✓", "stop": "✗", "skip": "—"}[state],
                      "sr": {"ok": "passed", "stop": "stopped here", "skip": "not reached"}[state]})
    # the content behind each step
    v = paper_idea_view(p)
    facts = list(v["facts"])
    buckets = []
    for key, label, words in BUCKET_ROWS:
        w = i.fact_buckets.get(key) if i.fact_buckets else None
        if w:
            text, css = words.get(w, (w.replace("_", " "), "flat"))
            buckets.append({"label": label, "value": text, "css": css})
    extra = [{"label": SWING_FIELD_WORDS.get(k, k.replace("_", " ")),
              "value": (f"{val:g}" if isinstance(val, float) else _fact_value(k, val, ""))}
             for k, val in sorted(i.facts.items()) if k not in {f for f, _, _ in REACTION_FACTS} and k in SWING_FIELD_WORDS]
    verdict = None
    if i.verdict is not None:
        s = i.verdict
        verdict = {"word": s.verdict, "plain": VERDICT_WORDS.get(s.verdict, s.verdict), "css": VERDICT_CSS.get(s.verdict, "stone"),
                   "priced_in": s.discounted, "news": s.news_status.replace("_", " "), "regime": s.regime,
                   "crowding": s.crowding, "mind": s.what_would_change_my_mind,
                   "override": v["verdict"]["override"] if v["verdict"] else "", "said": s.said,
                   "reasons": [{"text": r.text, "ev": [ev(e) for e in r.evidence]} for r in s.reasons]}
    batch = sw.batches[p.batch] if p.batch is not None and p.batch < len(sw.batches) else None
    if batch is None:
        batch = next((b for b in sw.batches if ref in b.refs), None)
    debate = []
    if batch is not None:
        for case, seat, name in ((batch.bull, "bull", "Bull"), (batch.bear, "bear", "Bear")):
            if case is None:
                continue
            claims = [{"id": c.claim_id, "text": c.text, "ev": [ev(e) for e in c.evidence]} for c in case.claims if c.ref == ref]
            debate.append({"seat": seat, "name": name, "argument": case.argument, "claims": claims,
                           "shared": len(batch.refs) > 1, "refs": batch.refs})
    pm = []
    if batch is not None:
        for rp in batch.replicates:
            acts = [a for a in rp.actions if a.ref == ref]
            if not rp.valid:
                pm.append({"n": rp.replicate + 1, "action": "unusable", "levels": "", "reason": "reply unusable (counts as a pass)"})
            for a in acts:
                levels = ""
                if a.action == "enter":
                    levels = " · ".join(x for x in (
                        f"stop {a.stop_pct:g}%" if a.stop_pct is not None else "",
                        f"target {a.target_pct:g}%" if a.target_pct is not None else "",
                        f"{a.time_stop_days} sessions" if a.time_stop_days is not None else "") if x)
                pm.append({"n": rp.replicate + 1, "action": a.action, "levels": levels, "reason": a.reason})
    leg = p.leg if p.leg is not None else None
    tleg = p.traced_leg
    rules = None
    shown = leg if leg is not None else tleg
    if shown is not None:
        rules = {"ok": shown.ok, "rule": shown.rule or "", "code": shown.code or "",
                 "plain": code_words(shown.code) if not shown.ok else "every swing rule passed",
                 "leg": leg_view(shown) if shown.ok else None, "traced": leg is None and stop < 5}
    trace = None
    to = p.traced_outcome
    same_end = (to is not None and o is not None and _OUTCOME_STOP.get(to.stage) == _OUTCOME_STOP.get(o.stage)
                and (to.code or "").split(":")[-1] == (o.code or "").split(":")[-1])
    if sw.trace_all and not p.same and to is not None and not same_end:
        where = JOURNEY_NAMES.get({"leg": "entered"}.get(to.stage, to.stage), to.stage)
        tv = _pm_votes(ref, sw.batches)
        trace = {"where": where, "why": code_words(to.code) if to.stage != "leg" else "a paper leg would have been built",
                 "votes": f"{tv[0]}/{tv[1]}" if tv and to.stage in ("pm", "rules", "leg") else "", "note": to.note}
    stop_key = "entered" if stop == 6 else JOURNEY_STEPS[stop][0]
    return {"p": p, "i": i, "ref": ref, "anchor": "sw-" + ref.replace(":", "-"), "stop": stop, "stop_key": stop_key,
            "stop_name": JOURNEY_NAMES[stop_key], "stop_label": "Entered" if stop == 6 else JOURNEY_STEPS[stop][1],
            "reason": reason, "code": code if stop < 6 else "", "votes": votes, "steps": steps,
            "setup": v["setup"], "cats": [ev(c.id) for c in i.catalysts], "facts": facts, "buckets": buckets, "extra": extra,
            "withheld_n": sum(1 for _ in i.facts_withheld), "verdict": verdict, "debate": debate, "pm": pm,
            "rules": rules, "trace": trace, "chosen": p.chosen, "debate_traced": stop < 3, "pm_traced": stop < 4, "leg": leg_view(leg) if leg is not None and leg.ok else None,
            "paper_setup": v["paper"], "carried": i.carried_from, "text_withheld": i.text_withheld}


def verdict_banner(journeys: list[dict[str, Any]], core: dict[str, Any], sw: Any) -> dict[str, Any]:
    """The banner: the entries (or "No trade"), the idea that got closest, the core line, the budget."""
    entered = [j for j in journeys if j["stop"] == 6]
    closest = journeys[0] if journeys and not entered else None
    budget = None
    if sw is not None and sw.budget is not None:
        budget = {"swing": f"{sw.budget.swing_pct:g}%", "core": f"{sw.budget.core_pct:g}%", "votes": sw.budget.votes,
                  "fallback": sw.budget.fallback}
    return {"entered": entered, "closest": closest, "core": core, "budget": budget}


def core_line(c: PublicCycleV1, lines: Lines) -> dict[str, Any]:
    """"Core: built / held / changed …" with the final weights in %."""
    r = c.risk
    if r is None:
        return {"verb": "no risk decision", "weights": [], "moved": 0}
    keys = lines.sort(set(r.final_x) | set(r.base_x))
    moved = [k for k in keys if abs(r.final_x.get(k, 0.0) - r.base_x.get(k, 0.0)) > EPS]
    built = not any(abs(v) > EPS for v in r.base_x.values()) and any(abs(v) > EPS for v in r.final_x.values())
    verb = "built" if built else "held" if not moved else f"changed {plural(len(moved), 'line')}"
    weights = sorted(((k, r.final_x.get(k, 0.0)) for k in keys if abs(r.final_x.get(k, 0.0)) > EPS), key=lambda t: -abs(t[1]))
    return {"verb": verb, "weights": [{"line": k, "w": fmt_pct1(x * 100.0)} for k, x in weights[:6]],
            "more": max(0, len(weights) - 6), "moved": len(moved)}


# ------------------------------------------------------------------------------ the Runs page
# One merged list of every council meeting (paper, rehearsal, live), newest first, as compact cards.
# Plain words only: "unreadable reply" (never "parse fail"), "2/3", "yes / no", whole percents.
PLAIN_FAIL = {   # failed call status -> (one, many)
    "parse_fail": ("unreadable reply", "unreadable replies"), "timeout": ("timeout", "timeouts"),
    "transport": ("service error", "service errors"), "skipped": ("skipped call", "skipped calls"),
}
PLAIN_STATUS = {"parse_fail": "unreadable reply", "timeout": "timed out", "transport": "service error"}
SWING_ROLE_SLUG = {"scout": "scout", "skeptic": "skeptic", "swing_bull": "bull", "swing_bear": "bear", "swing_pm": "pm"}
_LIVE_STAGE_STEP = {"dropped_by_code": 1, "skeptic": 2, "waiting": 2, "debate": 3, "pm": 4, "risk": 5, "missed": 5,
                    "expired": 5}


def council_health(calls: list[PublicCall], run_href: str = "") -> dict[str, Any]:
    """One line on the meeting's model agents ("7 agents · 1 unreadable reply") and one row per agent
    for the <details>. An agent is a CALL_AGENT name (the bull's opening and rebuttal are two)."""
    groups: dict[str, list[tuple[PublicCall, dict[str, Any]]]] = {}
    for x in calls:
        groups.setdefault(CALL_AGENT.get(x.role, (x.role.replace("_", " ").capitalize(),))[0], []).append(
            (x, call_view(x)))
    agents, fails = [], {}
    for name, rows in groups.items():
        role = rows[0][0].role
        views = [v for _, v in rows]
        st = status_of(views)
        bad = [v for v in views if v["failed"]]
        for v in bad:
            fails[v["status"]] = fails.get(v["status"], 0) + 1
        word = PLAIN_STATUS.get(st["key"], st["word"])
        if st["key"] == "partial":
            kinds = {v["status"] for v in bad}
            what = PLAIN_FAIL.get(kinds.pop(), ("failed call", "failed calls")) if len(kinds) == 1 else (
                "failed call", "failed calls")
            word = f"{len(bad)} {what[0] if len(bad) == 1 else what[1]} of {len(views)}, the rest ok"
        slug = ROLE_AGENT.get(role) or SWING_ROLE_SLUG.get(role, "")
        anchor = CALL_AGENT.get(role, ("", "", ""))[2]
        href = (f"{run_href}#{anchor}" if run_href and anchor else f"agents/{slug}.html" if slug else "")
        agents.append({"name": name, "accent": CALL_AGENT.get(role, ("", "neutral"))[1], "word": word,
                       "css": st["css"], "calls": len(views), "href": href})
    issues = [f"{n} {PLAIN_FAIL.get(k, (k.replace('_', ' '), k.replace('_', ' ')))[0 if n == 1 else 1]}"
              for k, n in sorted(fails.items(), key=lambda t: -t[1])]
    if not agents:
        summary = "no model call recorded"
    else:
        summary = " · ".join([plural(len(agents), "agent")] + (issues or ["every reply usable"]))
    return {"summary": summary, "ok": not issues, "agents": agents, "calls": len(calls)}


def _agree_words(c: PublicCycleV1) -> dict[str, Any] | None:
    """The manager attempts agreeing on the line where they agreed least: {"n": 2, "of": 3}."""
    values = list(c.pm.agreement_pct.values())
    valid = c.pm.valid_replicates
    if not values or not valid:
        return None
    low = min(values)
    for of in dict.fromkeys((valid, len(c.pm.replicates))):    # the share's denominator, as a count
        n = low * of / 100.0
        if of and abs(n - round(n)) < 0.02:
            return {"text": f"{round(n)}/{of}", "full": round(n) == of}
    return {"text": f"{low:.0f}%", "full": False}


def _solo_agrees(c: PublicCycleV1) -> bool | None:
    control = c.single_agent
    if control is None or not control.levels or not c.pm.levels:
        return None
    common = set(control.levels) & set(c.pm.levels)
    return all(abs(control.levels[k] - c.pm.levels[k]) < 1e-9 for k in common)


def _core_words(cl: dict[str, Any]) -> str:
    verb = cl["verb"]
    if cl["moved"]:                     # building from cash re-weights every line it buys
        return f"core: {plural(cl['moved'], 'line')} re-weighted"
    return "core: no decision" if verb == "no risk decision" else f"core: {verb}"


def _late_words(c: PublicCycleV1) -> str:
    if c.status == "missed":
        return f"ran after its slot had passed ({fmt_late(c.late_by_min)} late, a catch-up)" if c.late_by_min else (
            "ran after its slot had passed (a catch-up)")
    return f"ran {fmt_late(c.late_by_min)} late" if c.late_by_min else ""


def _paper_moves(c: PublicCycleV1, lines: Lines) -> list[str]:
    r = c.risk
    if r is None:
        return []
    moved = [(k, r.base_x.get(k, 0.0), r.final_x.get(k, 0.0)) for k in lines.sort(set(r.final_x) | set(r.base_x))
             if abs(r.final_x.get(k, 0.0) - r.base_x.get(k, 0.0)) > EPS]
    parts = [f"{ticker(k)} {fmt_pct1(b * 100.0)} → {fmt_pct1(a * 100.0)}" for k, b, a in moved[:4]]
    if len(moved) > 4:
        parts.append(f"{len(moved) - 4} more")
    return parts


def meeting_cards(view: JournalView, lines: Lines, runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`meetings()` with what a Runs card shows: the verdict sentence, the idea closest to trading and
    where it stopped, what changed in the core, manager agreement, the solo agent, the council's
    health, lateness and the seal."""
    live = {cv.doc.cycle_id: cv for cv in view.cycles}
    out = []
    for m in meetings(view, lines):
        card = dict(m)
        closest, entered = None, m["chosen"] != "no trade"
        if m["kind"] == "paper":
            doc = view.paper_cycles.get(m["decision_no"])
            c = doc.core if doc is not None else None
            has_swing = doc is not None and doc.swing is not None
            if doc is not None:
                dv = decision_view(doc, m["verified"], lines)
                bc = dv["banner"]["closest"]
                if bc is not None:
                    closest = {"ticker": bc["i"].ticker, "side": bc["i"].side, "where": bc["stop_label"],
                               "why": bc["reason"],
                               "votes": f"{bc['votes'][0]}/{bc['votes'][1]}" if bc["votes"] and bc["stop"] >= 4 else "",
                               "href": f"{m['decision_href']}#{bc['anchor']}"}
            changed = _paper_moves(c, lines) if c is not None else []
            card["seal"] = "verified" if m["verified"] else "sealed"
            card["decision"] = None
        else:
            cv = live[m["cycle_id"]]
            c = cv.doc
            has_swing = c.swing is not None
            ideas = list(c.swing.ideas) if c.swing else []
            if ideas and not entered:
                best = max(ideas, key=lambda i: _LIVE_STAGE_STEP.get(i.stage_reached, 1))
                step = _LIVE_STAGE_STEP.get(best.stage_reached, 1)
                closest = {"ticker": best.ticker, "side": best.side, "where": JOURNEY_STEPS[step][1],
                           "why": SWING_STAGE.get(best.stage_reached, (best.stage_reached.replace("_", " "),))[0],
                           "votes": "", "href": f"{m['run_href']}#swing-ideas"}
            changed = [runs[c.cycle_id]["change_words"]] if c.cycle_id in runs else []
            card["seal"] = "verified" if cv.verified else "sealed" if cv.commitment else "none"
            outcome = cv.human_outcome
            card["decision"] = {"chip": cv.chip, "href": f"{m['run_href']}#a-decision",
                                "words": "" if cv.rehearsal or outcome in ("none", "pending", "no_action")
                                else outcome.replace("_", " ")}
            card["mode_chip"] = cv.mode_chip
        swing = (f"Entered {m['chosen']}" if entered else "No swing trade") if has_swing or entered else ""
        core = _core_words(core_line(c, lines)) if c is not None else ""
        card["verdict"] = " · ".join(x for x in (swing, core) if x) if swing else (core[:1].upper() + core[1:])
        card["has_swing"] = has_swing
        card["closest"] = closest
        card["changed"] = changed
        card["agree"] = _agree_words(c) if c is not None else None
        card["solo"] = _solo_agrees(c) if c is not None else None
        card["late"] = _late_words(c) if c is not None else ""
        card["health"] = council_health(list(c.calls) if c is not None else [], m["run_href"])
        card["anchor"] = f"m-{m['kind']}-{m['cycle_id']}"
        out.append(card)
    return out


def missed_slots(view: JournalView) -> list[dict[str, str]]:
    """Slots on the operations log that produced no published meeting (skipped, aborted, halted)."""
    shown = {cv.doc.cycle_id for cv in view.cycles} | {d.cycle_id for d in view.paper_cycles.values()}
    rows = [r for r in view.ops if r.cycle_id not in shown and r.status not in ("on_time", "late", "missed")]
    rows.sort(key=lambda r: r.slot, reverse=True)
    return [{"slot": r.slot, "words": STATUS_WORDS.get(r.status, r.status.replace("_", " "))} for r in rows]


def paper_home(view: JournalView, lines: Lines, geo: Geometry | None = None) -> dict[str, Any] | None:
    if view.paper_latest is None and not view.paper_rows:
        return None
    latest = view.paper_latest
    pbook = paper_book_home(latest.book, latest.as_of, geo or Geometry()) if latest is not None else None
    last = view.paper_rows[-1] if view.paper_rows else None
    doc = view.paper_cycles.get(last.decision_no) if last else None
    ideas = []
    for i in (doc.swing.ideas if doc and doc.swing else []):
        ideas.append({"ticker": i.idea.ticker, "side": i.idea.side, "chosen": i.chosen, "why": i.why,
                      "out": outcome_view(i.real_outcome)})
    perf = latest.performance if latest else None
    return {"latest": latest, "last": last, "ideas": ideas, "book": pbook,
            "href": paper_href(last.decision_no) if last else "",
            "chosen": chosen_words(list(last.chosen)) if last else "",
            "perf": {"swing": fmt_signed(perf.swing_return_pct), "legs": perf.swing_closed_legs,
                     "since": fmt_day(perf.since) if perf.since else "—", "note": perf.note} if perf else None}


# ------------------------------------------------------------------------------ shared view model
# Helpers for every page (FOUNDATION). Paper runs are the current reality, so pages read these
# instead of `view.cycles` alone:
#
#   meetings(view, lines) -> list[dict]         every council meeting, newest first: live runs
#       (kind "live" / "rehearsal", from journal/cycles) and paper decisions (kind "paper", from
#       journal/paper). Keys: kind, badge ("LIVE" | "REHEARSAL" | "PAPER"), cycle_id, slot,
#       decision_no (int | None), verdict (one sentence), chosen (words, "no trade"), ideas (int),
#       core_moves (int), core_verb ("built" / "held" / "changed 2 lines"), href (the decision page
#       for paper, the run page for live; relative to the site root), run_href (the live run page
#       or ""), decision_href (the paper decision page or ""), verified (bool).
#   role_calls(view) -> list[dict]              every model call of every meeting: {kind, cycle_id,
#       slot, decision_no, href, call (PublicCall)}; newest meeting first.
#   role_stats(view, roles) -> dict             one agent's numbers over paper + live calls whose
#       role is in `roles`: calls, ok, failed, ok_pct ("83%" | "—"), ok_num, runs (meetings it
#       spoke in), by_kind {paper, live, rehearsal}, latency (median, words), last (the newest
#       role_calls row or None).
#   agent_role_stats(view) -> {slug: role_stats} for every agent page slug (core specs + scout,
#       skeptic) and the swing seats (swing_bull, swing_bear, swing_pm).
#   rule_anchor(code) -> "S8" | "R14" | "MC" | None   the rules.html card id for a rule / reason
#       code ("S8:illiquid", "R14", "NDX: R11", "net_rr_below_min" -> "S6"); None when unknown.
#   rule_href(code, root="") -> "rules.html#S8" | ""; Jinja: `rule_link(code, root, cls)` renders
#       the code chip as a link to its rule card (a plain <code> when unknown).
#   AGENT_OF_STEP: journey step key -> agent page slug (scout, skeptic, bull, pm; gate/rules -> "").
SWING_RULE_OF: dict[str, str] = dict(SWING_RULE_CODES)
CORE_RULE_WORDS = {"material_change": "MC", "material_change_required": "MC", "not_material": "MC",
                   "initial_build": "IB", "build_phase": "IB", "approval": "AP", "reconcile": "RC",
                   "priority": "PR"}
RULE_ID = re.compile(r"(?:^|[\s:(])((?:SB|S|R)\d{1,2}[a-z]?)(?=$|[\s:),.])")
AGENT_OF_STEP = {"scout": "scout", "skeptic": "skeptic", "debate": "bull", "pm": "pm", "gate": "", "rules": ""}
AGENT_ROLE_SLUGS = {**{a.slug: tuple(a.roles) for a in AGENT_SPECS if a.roles},
                    "scout": ("scout",), "skeptic": ("skeptic",), "swing_bull": ("swing_bull",),
                    "swing_bear": ("swing_bear",), "swing_pm": ("swing_pm",)}


def rule_anchor(code: str | None) -> str | None:
    if not code:
        return None
    text = str(code).strip()
    m = RULE_ID.search(text)
    if m:
        return m.group(1)
    bare = text.split(":")[-1].strip()
    return SWING_RULE_OF.get(bare) or CORE_RULE_WORDS.get(bare)


def rule_href(code: str | None, root: str = "") -> str:
    a = rule_anchor(code)
    return f"{root}rules.html#{a}" if a else ""


def rule_link(code: str | None, root: str = "", cls: str = "jc-code") -> Markup:
    if not code:
        return Markup("")
    href = rule_href(code, root)
    if not href:
        return Markup('<code class="{}">{}</code>').format(cls, code)
    return Markup('<a class="{} rule-link" href="{}" title="Rule {}: what it says and its numbers">{}</a>').format(
        cls, href, rule_anchor(code), code)


def _meeting_verdict(chosen: str, ideas: int | None, core_verb: str) -> str:
    swing = ("no swing trade" if chosen == "no trade" else f"entered {chosen}")
    if ideas is not None:
        swing += f" from {plural(ideas, 'idea')}"
    return f"{swing[:1].upper()}{swing[1:]}; core {core_verb}."


def meetings(view: JournalView, lines: Lines) -> list[dict[str, Any]]:
    live_ids = {cv.doc.cycle_id for cv in view.cycles}
    out: list[dict[str, Any]] = []
    for cv in view.cycles:
        c = cv.doc
        sec = c.swing
        chosen_i = [i for i in (sec.ideas if sec else []) if i.stage_reached in ("planned", "approved", "executed")]
        chosen = ", ".join(f"{ticker(i.ticker)} {i.side}" for i in chosen_i) or "no trade"
        cl = core_line(c, lines)
        kind = "rehearsal" if cv.rehearsal else "live"
        href = f"cycles/{c.cycle_id}.html"
        out.append({"kind": kind, "badge": kind.upper(), "cycle_id": c.cycle_id, "slot": c.slot,
                    "decision_no": None, "chosen": chosen, "ideas": len(sec.ideas) if sec else 0,
                    "core_moves": cl["moved"], "core_verb": cl["verb"],
                    "verdict": _meeting_verdict(chosen, len(sec.ideas) if sec else None, cl["verb"]),
                    "href": href, "run_href": href, "decision_href": "", "verified": cv.verified})
    for r in view.paper_rows:
        doc = view.paper_cycles.get(r.decision_no)
        cl = core_line(doc.core, lines) if doc is not None else {"verb": "held" if not r.core_moves else
                                                                  f"changed {plural(r.core_moves, 'line')}",
                                                                  "moved": r.core_moves}
        chosen = chosen_words(list(r.chosen))
        has_swing = doc is not None and doc.swing is not None
        out.append({"kind": "paper", "badge": "PAPER", "cycle_id": r.cycle_id, "slot": r.slot,
                    "decision_no": r.decision_no, "chosen": chosen, "ideas": r.ideas,
                    "core_moves": r.core_moves, "core_verb": cl["verb"],
                    "verdict": _meeting_verdict(chosen, r.ideas if has_swing or r.ideas else None, cl["verb"]),
                    "href": paper_href(r.decision_no),
                    "run_href": f"cycles/{r.cycle_id}.html" if r.cycle_id in live_ids else "",
                    "decision_href": paper_href(r.decision_no),
                    "verified": view.paper_verified.get(r.decision_no, False)})
    out.sort(key=lambda m: (m["slot"], m["decision_no"] or 0), reverse=True)
    return out


def role_calls(view: JournalView) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cv in view.cycles:
        kind = "rehearsal" if cv.rehearsal else "live"
        for x in cv.doc.calls:
            rows.append({"kind": kind, "cycle_id": cv.doc.cycle_id, "slot": cv.doc.slot, "decision_no": None,
                         "href": f"cycles/{cv.doc.cycle_id}.html", "call": x})
    for no, doc in view.paper_cycles.items():
        for x in doc.core.calls:
            rows.append({"kind": "paper", "cycle_id": doc.cycle_id, "slot": doc.slot, "decision_no": no,
                         "href": paper_href(no), "call": x})
    rows.sort(key=lambda r: (r["slot"], r["decision_no"] or 0), reverse=True)
    return rows


def role_stats(view: JournalView, roles: tuple[str, ...] | list[str] | set[str],
               calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    mine = [r for r in (calls if calls is not None else role_calls(view)) if r["call"].role in set(roles)]
    views = [call_view(r["call"]) for r in mine]
    n = len(views)
    ok = sum(1 for v in views if not v["failed"])
    by_kind = {k: sum(1 for r in mine if r["kind"] == k) for k in ("paper", "live", "rehearsal")}
    lat = median([float(r["call"].latency_ms) for r in mine if r["call"].latency_ms is not None])
    return {"calls": n, "ok": ok, "failed": n - ok, "ok_pct": f"{100.0 * ok / n:.0f}%" if n else "—",
            "ok_num": 100.0 * ok / n if n else None, "runs": len({(r["kind"], r["cycle_id"]) for r in mine}),
            "by_kind": by_kind, "latency": fmt_secs(int(lat)) if lat is not None else "—",
            "last": mine[0] if mine else None}


def agent_role_stats(view: JournalView) -> dict[str, dict[str, Any]]:
    calls = role_calls(view)
    return {slug: role_stats(view, roles, calls) for slug, roles in AGENT_ROLE_SLUGS.items()}


# ------------------------------------------------------------------------------ the agents pages
# Council seating cards (approved 2026-10-01): one card per seat in the order a decision flows
# (Scout -> Skeptic -> Bull / Bear -> Manager -> analysts -> Risk -> You), code officers in a compact
# "Machinery" band, and agent pages that open with a verdict banner and a ✓/✗/— strip of recent calls.
PAPER_CHIP = {"label": "PAPER", "css": "rehearsal", "title": "A paper run: real data and models, nothing traded"}
# The agents' shorthand in published argument text, in plain words (the claim-chip translations
# of MARKET_FIELDS / SWING_FIELD_WORDS, worded for a sentence). Only identifier-like tokens are
# rewritten, never plain English words.
TERM_WORDS: tuple[tuple[str, str], ...] = (
    (r"\bmom10d\b", "10-day momentum"), (r"\bmom63d\b", "3-month momentum"),
    (r"\bdd52\b", "drop from 52-week high"), (r"\bSMA ?(\d{2,3})\b", r"\1-day average"),
    (r"\bdist_sma(\d{2,3})(?:_pct)?\b", r"distance from the \1-day average"),
    (r"\bsigma_ann\b", "yearly volatility"), (r"\bret1d_sigma\b", "last day's move vs normal"),
    (r"\bdist_52w_high(?:_pct)?\b", "distance from 52-week high"),
    (r"\bdist_52w_low(?:_pct)?\b", "distance from 52-week low"), (r"\brel_move(?:_since_pct)?\b", "move vs sector"),
    (r"\bret_(\d{1,3})d\b", r"\1-day return"), (r"\bvol_ratio_last\b", "last day's volume vs normal"),
    (r"\bvol_ratio_since\b", "volume since the news vs normal"),
    (r"\bshort_interest_pct_float\b", "short interest"), (r"\bdays_to_cover\b", "days to cover"),
    (r"\bnews_age_sessions\b", "news age"), (r"\bfiling_age_d\b", "filing age"),
    (r"\brev_yoy\b", "revenue vs a year ago"), (r"\brev_accel\b", "revenue growth change"),
    (r"\bgm_chg\b", "gross margin change"), (r"\bom_chg\b", "operating margin change"),
    (r"\bbeta_60d\b", "60-day beta"), (r"\bsigma_daily\b", "daily volatility"),
    (r"(\d)\s?bps?\b", r"\1 basis points"), (r"\bbps?\b", "basis points"), (r"\byoy\b", "year on year"),
)
_TERM_RES = tuple((re.compile(p), w) for p, w in TERM_WORDS)
_FIELD_TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")     # identifier-like: move_since_news_close_sigma


def plain_terms(text: Any, book: str = "core") -> str:
    """Agent shorthand in plain words: `mom10d` -> "10-day momentum", `SMA50` -> "50-day average",
    `40bps` -> "40 basis points", and any other fact-field name the claim chips translate. In the
    swing book `vol_ratio` is a volume ratio; in the core it is the volatility ratio. Redaction
    markers ("[value removed]") pass through untouched."""
    out = "" if text is None else str(text)
    for rx, words in _TERM_RES:
        out = rx.sub(words, out)
    swing = book == "swing"
    fields = {**MARKET_FIELDS, **VOL_FIELDS, **SWING_FIELD_WORDS,
              "vol_ratio": "volume vs normal" if swing else VOL_FIELDS.get("vol_ratio", "volatility ratio")}
    return _FIELD_TOKEN.sub(lambda m: fields.get(m.group(0), m.group(0)), out)


_RAW_TAGS = ("pre", "code", "script", "style", "title", "head")


def plain_html(html: str, book: str = "core") -> str:
    """`plain_terms` over a rendered page's text nodes only (never a tag, an attribute, or the
    contents of <pre>/<code>/<head>), so every argument, rebuttal and attempt reads in words."""
    parts = re.split(r"(<[^>]+>)", html)
    raw = 0
    out = []
    for p in parts:
        if p.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", p)
            if m and m.group(2).lower() in _RAW_TAGS and not p.endswith("/>"):
                raw += -1 if m.group(1) else 1
                raw = max(raw, 0)
            out.append(p)
        else:
            out.append(p if raw else plain_terms(p, book))
    return "".join(out)


def agent_sources(view: JournalView, lines: Lines, runs: dict[str, dict[str, Any]],
                  linked: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every meeting an agent page reads, newest first: the live runs (with their run pages) and the
    paper decisions, whose core council goes through the same run and transcript views (its
    links land on the decision page's core section)."""
    out: list[dict[str, Any]] = []
    for cv in view.cycles:
        cid = cv.doc.cycle_id
        out.append({"kind": "rehearsal" if cv.rehearsal else "live", "cv": cv, "no": None, "run": runs[cid],
                    "tr": linked[cid], "href": f"../cycles/{cid}.html", "core_anchor": "", "doc": None})
    for no, doc in sorted(view.paper_cycles.items()):
        cv = CycleView(doc=doc.core, path="", verified=view.paper_verified.get(no, False))
        run = build_run_view(cv, lines)
        tr = build_transcript(cv, lines, run, base=f"../{paper_href(no)}")
        for st in tr["chain"]:                    # a paper run is not a rehearsal
            for part in (st, *st.get("segs", [])):
                for k in ("text", "plain"):
                    if isinstance(part.get(k), str):
                        part[k] = part[k].replace("not needed: rehearsal", "not needed: a paper run")
        out.append({"kind": "paper", "cv": cv, "no": no, "run": run, "tr": tr, "href": f"../{paper_href(no)}",
                    "core_anchor": "d-core", "doc": doc})
    out.sort(key=lambda s: (s["cv"].doc.slot, s["no"] or 0), reverse=True)
    return out


def _glyph(state: str) -> str:
    return {"ok": "✓", "no": "✗", "none": "—"}[state]


def _hchip(state: str, word: str, entry: dict[str, Any], href: str) -> dict[str, str]:
    """One mini-history chip: a glyph AND a word (never colour alone), linked to the meeting."""
    no = entry.get("no")
    return {"state": state, "glyph": _glyph(state), "word": word, "href": href,
            "label": f"#{no}" if no else fmt_short_when(entry["cv"].doc.slot),
            "badge": "PAPER" if entry.get("kind") == "paper" else "LIVE" if entry.get("kind") == "live" else "REHEARSAL"}


def _latest_prompt_note(calls: list[Any], prompts: dict[str, dict[str, str]]) -> str:
    """"This call ran X; the prompt in force is now Y" when the newest call's prompt differs from
    the manifest's (a versioned policy change since)."""
    for x in calls:
        meta = prompts.get(x.role)
        if not meta or not x.prompt_id:
            continue
        ran_sha = short_sha(x.prompt_sha) if x.prompt_sha else ""
        if x.prompt_id != meta["id"] or (ran_sha and meta["sha"] and ran_sha != meta["sha"]):
            return (f"This call ran the prompt {x.prompt_id}" + (f" ({ran_sha})" if ran_sha else "")
                    + f"; the prompt in force is now {meta['id']}" + (f" ({meta['sha']})" if meta["sha"] else "")
                    + ". The next call uses the new one.")
    return ""


def agent_track(agent: dict[str, Any], prompts: dict[str, dict[str, str]]) -> dict[str, Any]:
    """A core agent page's banner (the latest call: what it said -> what happened, its meeting) and
    the ✓/✗/— strip of its last calls, with the strip's meaning in words."""
    slug = agent["slug"]
    rows = agent["overview"]
    chips = []
    for e, o in zip(agent["entries"], rows, strict=False):
        href = f"{e['href']}#{e['anchor']}"
        body = next((a["body"] for a in reversed(e["sections"]) if a["body"]), None)
        st = e["status"]["css"]
        if slug in ("bull", "bear"):
            if body and body.get("used_ok"):
                chips.append(_hchip("ok", "manager agreed", e, href) if body.get("used_got")
                             else _hchip("no", "not followed", e, href))
            else:
                chips.append(_hchip("none", "no usable decision", e, href))
        elif slug == "risk":
            held = "held" in (o["said"] or "")
            chips.append(_hchip("no" if held else "ok", "held a change" if held else "all checks pass", e, href))
        elif slug == "human":
            chips.append(_hchip("none", "not needed" if e["kind"] != "live" else (e["chip"]["label"] or "").lower(),
                                e, href))
        elif agent["kind"] == "LLM":
            chips.append(_hchip("no", e["status"]["word"], e, href) if st in ("failed", "timeout") else
                         _hchip("none", "not run", e, href) if st == "idle" else _hchip("ok", "usable", e, href))
        else:
            chips.append(_hchip("ok" if st == "ok" else "none", o["said"] or e["status"]["word"], e, href))
    legend = {"bull": "✓ the manager did what it asked · ✗ the manager went another way · — no usable manager decision",
              "bear": "✓ the manager did what it asked · ✗ the manager went another way · — no usable manager decision",
              "risk": "✓ every check passed · ✗ it held a change back (its job)",
              "human": "— no approval needed (rehearsal or paper)"}.get(
        slug, "✓ a usable reply · ✗ the call failed · — it did not run" if agent["kind"] == "LLM" else "✓ it ran")
    banner = None
    if agent["entries"]:
        e, o = agent["entries"][0], rows[0] if rows else {}
        body = next((a["body"] for a in reversed(e["sections"]) if a["body"]), None)
        c = e["cv"].doc
        if slug in ("bull", "bear"):
            happened = (("the manager did what it asked" if body.get("used_got") else f"the manager chose to {body['did']}")
                        if body and body.get("used_ok") else "no usable manager decision")
        elif slug == "human":
            happened = "nothing to approve: a paper run trades nothing" if e["kind"] == "paper" else (e["chip"]["label"] or "").lower()
        elif slug == "pm":
            happened = "the risk engine: " + (o.get("risk_said") or "checked it")
        else:
            happened = "the core " + o["core_verb"] if o.get("core_verb") else ""
        calls = [x for x in c.calls if x.role in agent["spec"].roles]
        banner = {"said": o.get("said") or e["status"]["word"], "happened": happened, "no": e["no"],
                  "kind": e["kind"], "href": f"{e['href']}#{e['anchor']}", "when": c.slot,
                  "prompt_note": _latest_prompt_note(calls, prompts)}
    return {"chips": chips[:10], "legend": legend, "banner": banner}


SEAT_TEXT = {   # slug -> (reads, decides)
    "scout": ("Public news, SEC filings, licensed headlines (by id only) and a screen of big movers.",
              "Which stocks to pitch as swing ideas, long or short, each with a stop, a target and a time limit."),
    "skeptic": ("Only the ticker, the side, the cited items, a one-line claim and the fact card; never the pitch.",
                "Pass, wait or reject: is the news already in the price?"),
    "bull": ("The fact pack, the analysts' cards and, in the swing book, the ideas the Skeptic let through.",
             "Nothing: it argues for positions (core weights and swing entries); the manager decides."),
    "bear": ("The bull's claims and the same evidence.",
             "Nothing: it contests the bull claim by claim and argues its own case."),
    "pm": ("The debate, the evidence and the ranges code allows.",
           "Core: up to three line changes, three attempts, the most typical used. Swing: enter or pass (two of "
           "three attempts must enter) and the swing budget."),
    "news": ("Public RSS feeds and SEC filings.", "Nothing: it writes evidence cards that cite their sources by id."),
    "macro": ("Public macro data: rates, the dollar, the VIX.", "Nothing: it describes the regime, as context only."),
    "risk": ("Every proposal, the risk policy and the book.",
             "The final word: it holds back any change that breaks a limit. Code, not a language model."),
    "human": ("A live proposal, in a separate operator terminal.",
              "Approves or rejects every live order. Paper runs trade nothing, so they need no approval."),
}
SEAT_ORDER = ("scout", "skeptic", "bull", "bear", "pm", "news", "macro", "risk", "human")
SEAT_ROLES = {"scout": ("scout",), "skeptic": ("skeptic",), "bull": ("bull_open", "bull_rebuttal", "swing_bull"),
              "bear": ("bear", "swing_bear"), "pm": ("pm", "swing_pm"), "news": ("news",), "macro": ("macro",)}
SEAT_SWING_PAGE = {"bull": "swing_bull", "bear": "swing_bear", "pm": "swing_pm"}
MACHINERY = ("data", "reference", "vol", "event", "audit", "costs", "control")


def _reliability(st: dict[str, Any], failed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The small reliability line: calls, usable share, the paper / live split, failures in words."""
    if not st["calls"]:
        return {"text": "no model call yet", "fails": [], "fail_words": ""}
    bk = st["by_kind"]
    split = " · ".join(x for x in (f"{bk['paper']} paper" if bk["paper"] else "",
                                   f"{bk['live']} live" if bk["live"] else "",
                                   f"{bk['rehearsal']} rehearsal" if bk["rehearsal"] else "") if x)
    kinds: dict[str, int] = {}
    for f in failed or []:
        kinds[f["word"]] = kinds.get(f["word"], 0) + 1
    words = ", ".join(plural(n, w, w if w in ("skipped", "invalid", "not run") else None) for w, n in kinds.items())
    return {"text": f"{plural(st['calls'], 'call')} · {st['ok_pct']} usable · {split}", "fails": failed or [],
            "fail_words": words}


def swing_seat_history(view: JournalView, lines: Lines) -> dict[str, list[dict[str, Any]]]:
    """{swing role: one row per paper decision with a swing part, newest first}: what the role said,
    what happened next, a ✓/✗/— chip, and the decision page's journeys behind it."""
    out: dict[str, list[dict[str, Any]]] = {r: [] for r in SWING_ROLES_ALL}
    for no, doc in sorted(view.paper_cycles.items(), reverse=True):
        sw = doc.swing
        if sw is None:
            continue
        dv = decision_view(doc, view.paper_verified.get(no, False), lines)
        journeys = dv["journeys"]
        tk = {j["ref"]: f"{ticker(j['i'].ticker)} {j['i'].side}" for j in journeys}
        bn = dv["banner"]
        if bn["entered"]:
            outcome = {"label": "ENTERED", "css": "executed", "title": "",
                       "text": "entered " + ", ".join(tk[j["ref"]] for j in bn["entered"])}
        else:
            outcome = {"label": "NO TRADE", "css": "stone", "title": "", "text": "no swing trade"}
            if bn["closest"]:
                c = bn["closest"]
                outcome["text"] += f"; closest was {tk[c['ref']]}, stopped at {c['stop_label']} ({c['reason']})"
        href = f"../{paper_href(no)}"
        base = {"no": no, "slot": doc.slot, "kind": "paper", "href": href + "#d-ideas", "decision_href": href,
                "outcome": outcome, "journeys": journeys, "sw": sw, "tk": tk,
                "budget_reason": next((rp.budget_reason for b in sw.batches for rp in b.replicates
                                       if rp.valid and rp.budget_reason), "")}
        calls = {r: [x for x in doc.core.calls if x.role == r] for r in SWING_ROLES_ALL}

        def status(role: str, calls: dict[str, list[Any]] = calls) -> tuple[str, str]:
            cs = calls[role]
            if not cs:
                return "none", "not run"
            bad = sum(1 for x in cs if x.status not in ("ok", "cached"))
            return ("no", f"{bad} of {plural(len(cs), 'reply', 'replies')} unusable") if bad else ("ok", "usable")

        entered_pm = [t.ref for b in sw.batches for t in b.tally if t.action == "enter"]
        # the Scout
        st, word = status("scout")
        names = [tk[j["ref"]] for j in journeys]
        said = (f"pitched {plural(len(names), 'idea')}: " + ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
                if names else "pitched no idea")
        out["scout"].append({**base, "said": said, "chip": {"state": st, "word": word}, "calls": calls["scout"]})
        # the Skeptic
        st, word = status("skeptic")
        verdicts = [j["verdict"]["word"] for j in journeys if j["verdict"]]
        vw = {"pass": "passed", "wait": "said wait to", "reject": "rejected", "failed": "could not judge"}
        said = (", ".join(f"{vw[w]} {verdicts.count(w)}" for w in ("pass", "wait", "reject", "failed") if verdicts.count(w))
                or "judged no idea")
        out["skeptic"].append({**base, "said": said, "chip": {"state": st, "word": word}, "calls": calls["skeptic"]})
        # the swing bull and bear: the ideas their claims are about; ✓ when the manager went their way
        for role, attr in (("swing_bull", "bull"), ("swing_bear", "bear")):
            refs = list(dict.fromkeys(cl.ref for b in sw.batches for case in [getattr(b, attr)] if case
                                      for cl in case.claims if cl.ref))
            st, word = status(role)
            if not calls[role] and not refs:
                continue
            verb = "argued for" if attr == "bull" else "argued against"
            said = (f"{verb} " + ", ".join(tk.get(r, r) for r in refs)) if refs else "made no claim"
            mine = [r for r in entered_pm if r in refs]
            if st == "no" and not refs:
                chip = {"state": "no", "word": word}
            elif attr == "bull":
                chip = ({"state": "ok", "word": "manager voted to enter " + ", ".join(tk.get(r, r) for r in mine)}
                        if mine else {"state": "no", "word": "manager passed on all"})
            else:
                chip = ({"state": "no", "word": "manager voted to enter " + ", ".join(tk.get(r, r) for r in mine)}
                        if mine else {"state": "ok", "word": "manager passed on all"})
            out[role].append({**base, "said": said, "chip": chip, "calls": calls[role]})
        # the swing manager
        st, word = status("swing_pm")
        if calls["swing_pm"] or sw.batches:
            enters = [f"{tk.get(t.ref, t.ref)} ({t.votes_for} of {t.replicates})" for b in sw.batches for t in b.tally
                      if t.action == "enter"]
            passes = sum(1 for b in sw.batches for t in b.tally if t.action != "enter")
            said = ("voted to enter " + ", ".join(enters) if enters else "passed on every idea") + (
                f"; passed on {passes}" if enters and passes else "")
            if sw.budget is not None:
                said += f"; swing budget {sw.budget.swing_pct:g}%"
            out["swing_pm"].append({**base, "said": said, "chip": {"state": st, "word": word}, "calls": calls["swing_pm"]})
    return out


SWING_SEAT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("swing_bull", "Bull · swing", "LLM", "bull", "Swing book",
              "Argues for the swing ideas the Skeptic let through, claim by claim, citing the fact card.",
              roles=("swing_bull",), source="prompts/swing_bull.md",
              more="The same seat as the core bull, run on the swing ideas of a slot in one batch. It has no "
                   "authority: the swing manager decides.",
              short="Argues for the swing ideas the Skeptic let through."),
    AgentSpec("swing_bear", "Bear · swing", "LLM", "bear", "Swing book",
              "Answers the swing bull's claims and argues against the ideas, citing the fact card.",
              roles=("swing_bear",), source="prompts/swing_bear.md",
              more="The same seat as the core bear, run on the swing ideas. It has no authority.",
              short="Contests the swing bull, idea by idea."),
    AgentSpec("swing_pm", "Manager · swing", "LLM", "pm", "Swing book",
              "Makes three separate attempts at enter-or-pass for each swing idea; two of three must enter. Also "
              "votes the swing budget.",
              roles=("swing_pm",), source="prompts/swing_pm.md",
              more="Each attempt sets a stop, a target and a time limit for an entry; code then checks the swing "
                   "rules. The budget is the median of the valid votes; idle swing money stays in the core.",
              short="Decides enter or pass on each swing idea, and the swing budget."),
)
SWING_SEAT_ICON = {"scout": "file-search", "skeptic": "flask-conical", "swing_bull": "trending-up",
                   "swing_bear": "trending-down", "swing_pm": "briefcase"}
SWING_SEAT_LEGEND = {
    "scout": "✓ a usable reply · ✗ the call failed",
    "skeptic": "✓ every reply usable · ✗ a reply could not be read (that idea is stopped)",
    "swing_bull": "✓ the manager voted to enter an idea it argued for · ✗ the manager passed on all of them",
    "swing_bear": "✓ the manager passed on every idea · ✗ the manager voted to enter one it argued against",
    "swing_pm": "✓ usable replies · ✗ a reply could not be read (it counts as a pass)",
}


def swing_seat_pages(view: JournalView, lines: Lines, prompts: dict[str, dict[str, str]],
                     calls_all: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The five swing seats' pages (Scout, Skeptic, swing bull, bear and manager): banner, the ✓/✗/—
    strip, numbers over paper and live calls, and one history entry per paper decision."""
    calls_all = calls_all if calls_all is not None else role_calls(view)
    hist = swing_seat_history(view, lines)
    out = []
    for spec in (*SWING_AGENT_SPECS, *SWING_SEAT_SPECS):
        st = role_stats(view, spec.roles, calls_all)
        rows = hist[spec.slug]
        failed = []
        for r in calls_all:
            if r["call"].role in spec.roles:
                v = call_view(r["call"])
                if v["failed"]:
                    failed.append({"what": v["fail_phrase"], "word": v["word"], "css": v["css"],
                                   "no": r["decision_no"], "when": fmt_short_when(r["slot"]),
                                   "href": "../" + r["href"] + ("#d-ideas" if r["decision_no"] else "#swing-ideas")})
        chips = [{"state": r["chip"]["state"], "glyph": _glyph(r["chip"]["state"]), "word": r["chip"]["word"],
                  "href": r["href"], "label": f"#{r['no']}", "badge": "PAPER"} for r in rows[:10]]
        banner = None
        if rows:
            r = rows[0]
            banner = {"said": r["said"], "happened": r["outcome"]["text"], "no": r["no"], "kind": "paper",
                      "href": r["href"], "when": r["slot"], "prompt_note": _latest_prompt_note(r["calls"], prompts)}
        out.append({"spec": spec, "slug": spec.slug, "name": spec.name, "accent": spec.accent, "kind": "LLM",
                    "icon": SWING_SEAT_ICON[spec.slug], "job": spec.job, "more": spec.more, "short": spec.short,
                    "stats": st, "rel": _reliability(st, failed), "failed": failed, "rows": rows[:HISTORY_CAP],
                    "chips": chips, "legend": SWING_SEAT_LEGEND[spec.slug], "banner": banner,
                    "source": f"{REPO_URL}/blob/main/{spec.source}", "source_path": spec.source,
                    "prompt": prompts.get(spec.roles[0]), "page": f"agents/{spec.slug}.html",
                    "core_page": {"swing_bull": "bull.html", "swing_bear": "bear.html", "swing_pm": "pm.html"}.get(spec.slug, "")})
    return out


def seat_cards(view: JournalView, agents: list[dict[str, Any]], swing_pages: list[dict[str, Any]],
               sources: list[dict[str, Any]], lines: Lines, calls_all: list[dict[str, Any]]) -> dict[str, Any]:
    """The agents index: one seating card per seat in decision-flow order, then the machinery band."""
    by = {a["slug"]: a for a in agents}
    sw_by = {a["slug"]: a for a in swing_pages}
    latest = sources[0] if sources else None
    verdicts = agent_verdicts(latest["cv"], latest["run"], latest["tr"], lines)[0] if latest else {}
    if latest is not None and latest["kind"] == "paper":
        verdicts["human"] = "not needed: a paper run trades nothing"
    seats = []
    for slug in SEAT_ORDER:
        a = by.get(slug)
        s = sw_by.get(slug) or sw_by.get(SEAT_SWING_PAGE.get(slug, ""))
        if a is None and slug not in sw_by:
            continue
        reads, decides = SEAT_TEXT[slug]
        if slug in sw_by:                        # Scout, Skeptic: the swing record
            card = sw_by[slug]
            row = card["rows"][0] if card["rows"] else None
            last = ({"text": row["said"][:1].upper() + row["said"][1:] + ".", "chip": row["outcome"], "no": row["no"],
                     "kind": "paper", "href": row["href"]} if row else None)
            st, rel = card["stats"], card["rel"]
            name, accent, icon, page, kind = card["name"], card["accent"], card["icon"], f"{slug}.html", "LLM"
            also = None
        else:
            spoke = [x for x in a["entries"] if any(sec["calls"] for sec in x["sections"])] if a["kind"] == "LLM" else a["entries"]
            e = spoke[0] if spoke else None
            last = None
            if e is not None and latest is not None:
                said = verdicts.get(slug, "") if e["key"] == latest["cv"].doc.cycle_id + str(latest["no"] or "") else (
                    a["overview"][0]["said"] if a["overview"] else "")
                text = said[:1].upper() + said[1:] if said else e["status"]["word"].capitalize()
                if slug in ("bull", "bear", "pm") and s is not None and s["rows"] and s["rows"][0]["no"] == e["no"]:
                    text = f"Core: {said or e['status']['word']}. Swing: {s['rows'][0]['said']}"
                body = next((x["body"] for x in reversed(e["sections"]) if x["body"]), None)
                if slug in ("bull", "bear"):
                    ch = ({"label": "MANAGER AGREED", "css": "executed", "title": ""} if body and body.get("used_ok") and body.get("used_got")
                          else {"label": "NOT FOLLOWED", "css": "halted", "title": ""} if body and body.get("used_ok")
                          else {"label": "NO DECISION", "css": "stone", "title": ""})
                elif slug == "pm" or slug == "risk":
                    held = "held" in verdicts.get("risk", "")
                    ch = {"label": "RISK HELD SOME" if held else "PASSED RISK", "css": "warn" if held else "executed", "title": ""}
                elif slug == "human":
                    ch = {"label": "NOT NEEDED", "css": "stone", "title": ""} if e["kind"] != "live" else e["chip"]
                else:
                    ch = {"label": "CONTEXT", "css": "stone", "title": "Evidence for the debate; code never acts on it"}
                last = {"text": text.rstrip(".") + ".", "chip": ch, "no": e["no"], "kind": e["kind"],
                        "href": f"{e['href']}#{e['anchor']}"}
            roles = SEAT_ROLES.get(slug)
            st = role_stats(view, roles, calls_all) if roles else None
            fails = [f for f in a["stats"]["failed"]] + ([f for f in s["failed"]] if s and slug in SEAT_SWING_PAGE else [])
            rel = _reliability(st, fails) if st else {
                "text": (f"code · {plural(a['stats']['runs'], 'meeting')}" if a["kind"] == "CODE" else
                         f"a person · {plural(a['stats'].get('asked', 0), 'live proposal')} to decide"),
                "fails": [], "fail_words": ""}
            name, accent, icon, page, kind = a["name"], a["accent"], a["icon"], f"{slug}.html", a["kind"]
            also = ({"href": f"{SEAT_SWING_PAGE[slug]}.html", "name": s["name"]} if s and slug in SEAT_SWING_PAGE else None)
        seats.append({"slug": slug, "name": name, "accent": accent, "icon": icon, "page": page, "kind": kind,
                      "reads": reads, "decides": decides, "last": last, "rel": rel, "also": also,
                      "worst": a["stats"]["worst"] if a is not None and slug not in sw_by else None,
                      "record": (" · ".join(x for x in (a["stats"]["agree_words"], a["stats"]["points_words"]) if x)
                                 if a is not None and "agree_words" in a["stats"] else ""),
                      "swing": slug in ("scout", "skeptic") or also is not None})
    machinery = []
    for slug in MACHINERY:
        a = by.get(slug)
        if a is None:
            continue
        said = verdicts.get(slug, "")
        machinery.append({"slug": slug, "name": a["name"], "accent": a["accent"], "icon": a["icon"], "kind": a["kind"],
                          "job": a["spec"].short or a["job"], "said": said, "page": f"{slug}.html"})
    return {"seats": seats, "machinery": machinery, "latest": latest}


# ------------------------------------------------------------------------------ the rules page
def _p(frac: Any, digits: int = 2) -> str:
    """A fraction as a percent in words (0.08 -> "8%", 1.9 -> "190%")."""
    return f"{trim_number(float(frac) * 100.0, digits)}%"


def _bp(bps: Any) -> str:
    """Basis points as a percent ("40" -> "0.4%")."""
    return f"{trim_number(float(bps) / 100.0, 3)}%"


def _n(v: Any) -> str:
    return trim_number(float(v), 2) if isinstance(v, int | float) and not isinstance(v, bool) else str(v)


def _yes(v: Any) -> str:
    return "yes" if v else "no"


def _core_numbers(key: str, v: Any) -> list[tuple[str, str]]:
    """The labelled numbers of one core rule (percent of the portfolio unless stated)."""
    if key == "gross":
        return [("Proposals stay under", _p(v["proposal_max"])), ("Code refuses above", _p(v["hard_max"])),
                ("On a watch, cut back to", _p(v["watch_derisk_to"]))]
    if key == "net":
        return [("Net exposure at least", _p(v["min"])), ("Net exposure at most", _p(v["max"])),
                ("All shorts together at most", _p(v["short_gross_max"]))]
    if key == "killswitch":
        return [("Warn (no new risk) at a fall of", _p(1 - float(v["warn_at"]))),
                ("Halt at a fall of", _p(1 - float(v["halt_at"]))), ("Confirming reads", _n(v["confirm_reads"])),
                ("Seconds between reads", _n(v["confirm_gap_s"])), ("Measured from", f"the {v['peak']} peak")]
    if key == "catastrophe_stop":
        out = [("Stop distance", f"the larger of the class floor and {_n(v['sigma_mult'])} × daily volatility × √{_n(v['horizon_days'])}"),
               ("Furthest stop", _p(v["cap"]))]
        out += [(f"Floor: {k.replace('_', ' ')}", _p(x)) for k, x in v.get("floors", {}).items()]
        return out
    if key == "reentry_cooloff_days":
        return [("Wait after a stop hit", plural(int(v["default"]), "day")), ("Crypto", plural(int(v["crypto"]), "day"))]
    if key == "caps":
        out = [(f"Line cap: {k}", _p(x)) for k, x in v["line"].items()]
        return out + [("Crypto together", _p(v["crypto_total"])), ("Currencies together", _p(v["fx_total"])),
                      ("Equity lines together (" + ", ".join(v["equity_beta_cluster"]["members"]) + ")",
                       _p(v["equity_beta_cluster"]["max"]))]
    if key == "leverage_caps":
        return [(f"Leverage: {k}", f"{_n(x)}x") for k, x in v.items()]
    if key == "margin_use_max":
        return [("Margin in use at most", _p(v)), ("Cash reserve at least", _p(1 - float(v)))]
    if key == "ex_ante_vol_hard":
        return [("Expected yearly volatility below", _p(v))]
    if key == "vol_breaker":
        return [("One line: short-term vs usual volatility", f"{_n(v['instrument_ratio'])}×"),
                ("Whole book", f"{_n(v['book_ratio'])}×"), ("Volatility card from", f"{_n(v['card_ratio'])}×")]
    if key == "authority":
        return [("Lines changed per run, at most", _n(v["max_deviations_per_cycle"])),
                ("Uptrend: cut with a qualifying card, up to", _p(v["up"]["cut_with_qualifying_card"]) + " of the line's full size"),
                ("Uptrend: extra leverage, up to", _p(v["up"]["leverage_extension"]) + " (only if it passes the cost gate)"),
                ("Mixed trend: range", f"{_p(v['mixed']['lo'])} to {_p(v['mixed']['hi'])}"),
                ("Downtrend: range", f"{_p(v['down']['lo'])} to {_p(v['down']['hi'])} (shorts need a cited risk card)"),
                ("Expired cards return the line to the reference", _yes(v.get("card_expiry_returns_to_reference")))]
    if key == "deadband":
        return [("Skip changes under", _p(v["level"]) + " of the line's full size"), ("Crypto", _p(v["level_crypto"])),
                ("Or under", _p(v["min_nav_share"]) + " of the portfolio")]
    if key == "min_hold_days":
        return [("Hold at least", plural(int(v["default"]), "day")), ("Crypto", plural(int(v["crypto"]), "day")),
                ("No flip from long to short within", f"{_n(v['no_flip_hours'])} h"),
                ("Moving back to the reference is always allowed", _yes(v.get("toward_reference_exempt")))]
    if key == "churn":
        return [("Added in one run, at most", _p(v["cycle_increase_max"])), ("Traded in 7 days, at most", _p(v["turnover_7d_max"])),
                ("Traded in 30 days, at most", _p(v["turnover_30d_max"]))]
    if key == "cost_budget":
        return [("Trading costs in one run, at most", _bp(v["cycle_max_bps"])),
                ("Discretionary costs in 30 days, at most", _bp(v["discretionary_30d_max_bps"])),
                ("Overnight financing a proposal may add, a day", _bp(v["carry_proposal_max_bps_day"])),
                ("Financing that puts the book on watch, a day", _bp(v["carry_watch_max_bps_day"]))]
    if key == "net_of_cost_gate":
        return [("Break-even Sharpe ratio, reference trades, at most", _n(v["reference_max_srbe"])),
                ("Break-even Sharpe ratio, council trades, at most", _n(v["council_max_srbe"])),
                ("Assumed holding period", f"{v['hold_days']['default']} days ({v['hold_days']['crypto']} crypto)")]
    if key == "event_block":
        return [("No adds before a macro release", f"{_n(v['macro_before_h'])} h"), ("… and after it", f"{_n(v['macro_after_h'])} h"),
                ("Before a company's earnings", f"{_n(v['earnings_before_h'])} h"), ("… and after", f"{_n(v['earnings_after_h'])} h"),
                ("An estimated earnings date counts as", f"± {_n(v['earnings_estimate_window_days'])} trading days")]
    if key == "anti_chase_sigma":
        return [("No adds after a one-day move larger than", f"{_n(v)}σ (its usual daily move)")]
    if key == "freshness":
        return [("Daily bars at most", f"{_n(v['daily_bar_max_h'])} h old"), ("4-hour bars at most", f"{_n(v['four_hour_bar_max_h'])} h old"),
                ("Quotes at most", f"{_n(v['quote_max_s'])} s old"),
                ("Frozen reference share at most", _p(v["frozen_reference_share_max"]))]
    if key == "material_change_required":
        return [("Required", _yes(v))]
    if key == "initial_build":
        return [("Rules paused per never-filled line", ", ".join(v["exempt"])), ("At most", plural(int(v["max_cycles"]), "run")),
                ("R14 overnight financing", "still applies")]
    if key == "proposal":
        return [("Risk-adding and discretionary orders, at most", _n(v["max_legs"])), ("All orders, at most", _n(v["max_legs_total"]))]
    if key == "approval":
        w = v["window"]
        return [("Approval window", f"{w['start']}–{w['end']} Lisbon time"),
                ("Price moved since the run, at most", f"{_n(v['open_price_guard_sigma4h'])}σ of 4 hours"),
                ("Book drift since the run, at most", _p(v["drift_l1_max"])), ("Order size may differ by", _p(v["amount_tolerance"])),
                ("Cost may rise to", f"{_n(v['cost_tolerance_mult'])}× plus {_bp(v['cost_tolerance_add_bps'])}"),
                ("Exposure after the fills, within", _p(v["post_fill_exposure_tolerance"]))]
    if key == "reconcile":
        return [("Gap between the real book and the plan, flagged above", _p(v["drift_max"])),
                ("Stop-loss level tolerance", _p(v["sl_rate_tolerance"]))]
    if key == "priority":
        return [("Order", " > ".join(str(x).replace("_", " ") for x in v))]
    return [(k.replace(".", " · ").replace("_", " "), val) for k, val in _flatten(v, "" if isinstance(v, dict) else key)]


CORE_EXTRA = {   # id -> (title, plain), rules without a block of numbers in policy/risk.yaml
    "R19": ("Closed markets", "A line whose market is closed is held where it is; nothing is bought or sold on it "
            "until the market reopens."),
    "R20": ("Open blockers", "While an operator blocker is open (an unresolved incident or check), the lines it covers "
            "are held and no new risk is added; swing ideas are dropped too."),
}
CORE_IDS = {"material_change_required": "MC", "initial_build": "IB", "approval": "AP", "reconcile": "RC", "priority": "PR"}
INITIAL_BUILD_PLAIN = ("While the book is first built, a line that has never been filled is exempt from {exempt} "
                       "(churn, cost budget and the net-of-cost gate), until every target line has been filled once or "
                       "{n} runs have passed; R14's overnight-financing limit still applies.")


def core_rule_cards(policy_dir: Path) -> list[dict[str, Any]]:
    text = (policy_dir / "risk.yaml").read_text()
    data = yaml.safe_load(text) or {}
    ids: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z_]+):.*?#\s*(R\d+[a-z]?)\b", line)
        if m:
            ids[m.group(1)] = m.group(2)
    cards = []
    for key, value in data.items():
        if key == "version":
            continue
        rid = ids.get(key) or CORE_IDS.get(key) or key.upper()[:3]
        plain = rule_plain(key, value)
        if key == "initial_build":
            plain = INITIAL_BUILD_PLAIN.format(exempt=join_words(list(value.get("exempt", []))),
                                               n=SPELLED.get(int(value.get("max_cycles", 5)), value.get("max_cycles")))
        cards.append({"id": rid, "key": key, "title": RULE_TITLES.get(key, key.replace("_", " ").capitalize()),
                      "plain": plain or RULE_TITLES.get(key, key), "numbers": _core_numbers(key, value),
                      "codes": [], "seat": "risk"})
        if key == "freshness":
            for xid, (title, plain_x) in CORE_EXTRA.items():
                cards.append({"id": xid, "key": "", "title": title, "plain": plain_x, "numbers": [], "codes": [],
                              "seat": "risk"})
    return cards


SWING_TITLES = {
    "S0": "Not a core holding", "S1": "Position size", "S2": "Open trades", "S3": "Weekly cap on new trades",
    "S4": "Open risk", "S5": "Stops", "S6": "Targets and reward against risk", "S7": "Time stop", "S8": "Liquidity",
    "S9": "Earnings", "S10": "Similar trades and market exposure", "S11": "No chasing", "S12": "Fees",
    "S13": "Shorts", "S14": "Cool-off", "S15": "Loss brake", "S16": "Entry guard at approval",
    "S17": "Drawdown scaling", "S18": "The swing budget is full", "SB16": "Which setups may trade",
}


def _swing_text(rid: str, s: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    g = lambda *path: _dig(s, path)       # noqa: E731
    if rid == "S0":
        return ("A stock the core council already holds as a line cannot also be a swing trade.", [])
    if rid == "S1":
        return (f"Each swing trade is about {_p(g('size', 'target_nav'))} of the portfolio, cut so that hitting the stop "
                f"loses at most {_p(g('size', 'max_loss_nav_at_stop'))} ({_p(g('size', 'short_max_loss_nav_at_stop'))} for a short).",
                [("Target size", _p(g("size", "target_nav"))), ("Smallest size after the stop cut", _p(g("size", "min_nav"))),
                 ("Loss at the stop, long, at most", _p(g("size", "max_loss_nav_at_stop"))),
                 ("Loss at the stop, short, at most", _p(g("size", "short_max_loss_nav_at_stop"))),
                 ("Short with unknown short interest", f"{_p(g('size', 'short_si_unknown_mult'))} of the size")])
    if rid == "S2":
        return (f"At most {_n(g('capacity', 'max_open'))} swing trades open at once, {_n(g('capacity', 'max_short'))} of them "
                "short, and one per stock.",
                [("Open trades, at most", _n(g("capacity", "max_open"))), ("Shorts, at most", _n(g("capacity", "max_short"))),
                 ("Per stock", "1")])
    if rid == "S3":
        return (f"At most {_n(g('capacity', 'max_new_7d'))} new swing trades in any seven days: a ceiling, not a quota.",
                [("New trades in a rolling 7 days, at most", _n(g("capacity", "max_new_7d")))])
    if rid == "S4":
        return (f"All open and proposed swing trades together may lose at most {_p(g('capacity', 'max_open_risk_nav'))} "
                "of the portfolio at their stops, allowing for gaps.",
                [("Open risk at the stops, at most", _p(g("capacity", "max_open_risk_nav"))),
                 ("Gap allowance", f"{_n(g('capacity', 'open_risk_gap_mult'))}× the stop distance")])
    if rid == "S5":
        return (f"Every trade has a stop between {_p(g('stops', 'min_pct'))} and {_p(g('stops', 'max_long_pct'))} away "
                f"({_p(g('stops', 'max_short_pct'))} for a short), outside one day's normal range; stops only ever move toward safety.",
                [("Closest stop", _p(g("stops", "min_pct"))), ("Furthest stop, long", _p(g("stops", "max_long_pct"))),
                 ("Furthest stop, short", _p(g("stops", "max_short_pct"))),
                 ("At least", f"{_n(g('stops', 'min_atr_mult'))} × the average daily range")])
    if rid == "S6":
        return (f"The target must pay at least {_n(g('targets', 'min_net_rr'))} times what the stop risks, after costs, and be "
                "a move the stock can plausibly make in the time allowed. The code gate checks the Scout's levels before the Skeptic sees them.",
                [("Net reward against risk, at least", _n(g("targets", "min_net_rr"))),
                 ("Target, at least", f"{_n(g('targets', 'min_cost_mult'))} × the round-trip cost"),
                 ("Target, at most", _p(g("targets", "max_pct"))),
                 ("Target, at most", f"{_n(g('targets', 'max_vol_mult'))} × daily volatility × √sessions"),
                 ("Daily volatility, at most", _p(g("targets", "max_sigma_daily")))])
    if rid == "S7":
        return (f"A trade closes after {_n(g('time_stop', 'min_sessions'))} to {_n(g('time_stop', 'max_sessions'))} sessions; "
                f"the manager may extend it once, with a new cited fact, to at most {_n(g('time_stop', 'max_total_sessions'))}.",
                [("Time stop", f"{_n(g('time_stop', 'min_sessions'))}–{_n(g('time_stop', 'max_sessions'))} sessions"),
                 ("One extension, at most", f"{_n(g('time_stop', 'max_extension_sessions'))} sessions"),
                 ("In total, at most", f"{_n(g('time_stop', 'max_total_sessions'))} sessions")])
    if rid == "S8":
        return ("Only stocks that trade enough every day and are not penny stocks; shorts need much more trading volume.",
                [("Thresholds", "a minimum average daily traded value and share price, higher for shorts; the amounts are in "
                                "policy/swing.yaml (this site shows no money amounts)")])
    if rid == "S9":
        return (f"No entry within {_n(g('earnings', 'no_entry_within_sessions'))} sessions of earnings, an exit proposal "
                f"{_n(g('earnings', 'exit_before_sessions'))} session before them, and a {_n(g('earnings', 'post_report_wait_h'))}-hour wait after a report.",
                [("No entry within", f"{_n(g('earnings', 'no_entry_within_sessions'))} sessions"),
                 ("Exit proposal before", f"{_n(g('earnings', 'exit_before_sessions'))} session"),
                 ("Wait after a report", f"{_n(g('earnings', 'post_report_wait_h'))} h"),
                 ("An estimated date counts as", f"± {_n(g('earnings', 'estimated_window_days'))} days")])
    if rid == "S10":
        return (f"At most {_n(g('correlation', 'max_open_per_bucket'))} trades on the same kind of bet, and the swing book's "
                "net market exposure stays small.",
                [("Trades per bucket, at most", _n(g("correlation", "max_open_per_bucket"))),
                 ("Counts as the same bet above a correlation of", _n(g("correlation", "same_bet_corr"))),
                 ("Swing book's net beta, at most", _n(g("correlation", "max_swing_net_beta")))])
    if rid == "S11":
        return (f"An idea is dropped if the stock already moved more than {_n(g('chase', 'max_move_since_news_sigma'))}σ since "
                f"the news; from {_n(g('chase', 'prior_wait_sigma'))}σ the Skeptic leans towards wait.",
                [("Dropped above", f"{_n(g('chase', 'max_move_since_news_sigma'))}σ since the news"),
                 ("The Skeptic's wait prior from", f"{_n(g('chase', 'prior_wait_sigma'))}σ")])
    if rid == "S12":
        enforce = g("fees", "mode") == "enforce"
        return (("Fees are " + ("enforced" if enforce else "measured and published, never a brake") +
                 f": a budget of {_bp(g('fees', 'budget_30d_nav_bps'))} of the portfolio over 30 days."),
                [("Mode", "enforced" if enforce else "reported only"), ("Fee budget over 30 days", _bp(g("fees", "budget_30d_nav_bps"))),
                 ("Fees against gross gains, at most", _p(g("fees", "max_fee_to_gross"), 1)),
                 ("Measured after", plural(int(g("fees", "min_closed_for_ratio")), "closed trade"))])
    if rid == "S13":
        return ("Shorts are 1x with a hard stop, and never on a recent listing, a crowded or squeeze-prone name, a takeover "
                "target or a stock that just flushed.",
                [("Listed at least", f"{_n(g('shorts', 'min_listing_days'))} days"),
                 ("Short interest, at most", f"{_n(g('shorts', 'max_si_pct_float'))}% of the float"),
                 ("No short after a fall of", f"{_n(g('shorts', 'no_short_after_down_sigma'))}σ"),
                 ("No short after a 20-day rise above", _p(g("shorts", "no_short_ret20_above"))),
                 ("Squeeze risk: price within this far of its high", _p(g("shorts", "near_high_pct"))),
                 ("… with volume above", f"{_n(g('shorts', 'near_high_vol_ratio'))}× normal"),
                 ("Takeover news looked back", f"{_n(g('shorts', 'takeover_lookback_days'))} days")])
    if rid == "S14":
        return (f"After a stop is hit the stock waits {_n(g('cooloff', 'after_stop_sessions'))} sessions; after any other exit "
                f"{_n(g('cooloff', 'after_exit_sessions'))}.",
                [("After a stop", f"{_n(g('cooloff', 'after_stop_sessions'))} sessions"),
                 ("After another exit", f"{_n(g('cooloff', 'after_exit_sessions'))} sessions")])
    if rid == "S15":
        return (f"A net loss of {_p(abs(float(g('brake', 'pnl_nav'))))} of the portfolio over {_n(g('brake', 'window_days'))} days, "
                "after all costs, stops new swing trades; only the operator lifts it.",
                [("Loss that stops new trades", _p(g("brake", "pnl_nav"))), ("Window", f"{_n(g('brake', 'window_days'))} days"),
                 ("After all costs", _yes(g("brake", "net_of_all_costs")))])
    if rid == "S16":
        return (f"An approved entry is a market order within {_n(g('entry_guard', 'valid_minutes'))} minutes of the run; it "
                "is dropped if the price already ran too far or hit the stop.",
                [("Approve within", f"{_n(g('entry_guard', 'valid_minutes'))} min"),
                 ("Price run, at most", f"{_p(g('entry_guard', 'max_run_stop_frac'))} of the stop distance or {_p(g('entry_guard', 'max_run_pct'))}"),
                 ("Re-proposals, at most", _n(g("entry_guard", "max_reproposals")))])
    if rid == "S17":
        return (f"From a fall of {_p(abs(float(g('drawdown_scale', 'from_peak'))))} below the peak, trades shrink to "
                f"{_p(g('drawdown_scale', 'size_nav'))} with at most {_n(g('drawdown_scale', 'max_open'))} open; the core kill "
                "switch's warn or halt blocks new entries.",
                [("Starts at a fall of", _p(abs(float(g("drawdown_scale", "from_peak"))))),
                 ("Size then", _p(g("drawdown_scale", "size_nav"))), ("Open trades then, at most", _n(g("drawdown_scale", "max_open")))])
    if rid == "S18":
        return ("An entry is refused if it would push the swing trades above the swing budget the council set (below).",
                [("Budget", "see the swing budget card")])
    if rid == "SB16":
        live = ", ".join(SETUP_WORDS.get(x, x.replace("_", " ")) for x in s.get("setups_live", []))
        paper = ", ".join(SETUP_WORDS.get(x, x.replace("_", " ")) for x in s.get("setups_paper_only", []))
        return ("Only some setups may trade; the rest are tracked on paper only, to measure them.",
                [("May trade", live or "—"), ("Paper only", paper or "—")])
    return ("", [])


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    for k in path:
        d = (d or {}).get(k)
    return d


def swing_rule_cards(policy_dir: Path) -> list[dict[str, Any]]:
    path = policy_dir / "swing.yaml"
    s = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    if not s:
        return []
    order = [f"S{n}" for n in range(19)] + ["SB16"]
    cards = []
    for rid in order:
        try:
            plain, nums = _swing_text(rid, s)
        except (KeyError, TypeError, ValueError, AttributeError):
            plain, nums = "", []
        codes = [{"code": c, "words": code_words(c)} for c, r in SWING_RULE_OF.items() if r == rid]
        cards.append({"id": rid, "key": "", "title": SWING_TITLES.get(rid, rid), "plain": plain or SWING_TITLES.get(rid, rid),
                      "numbers": nums, "codes": codes, "seat": "skeptic" if rid == "S11" else "scout"})
        if rid == "S18":
            b = s.get("budget") or {}
            try:
                cards.append({"id": "budget", "key": "budget", "title": "The swing budget (set by the council)",
                              "plain": (f"The swing manager's attempts each vote a swing budget from 0 to {_n(b['max_pct'])}% of "
                                        f"the portfolio in steps of {_n(b['step_pct'])}; code takes the median and clamps it. "
                                        "Unused swing money is invested in the core."),
                              "numbers": [("Range", f"0–{_n(b['max_pct'])}%"), ("Steps", f"{_n(b['step_pct'])}%"),
                                          ("Decided by", "the median of the swing manager's valid votes"),
                                          ("Before the first valid vote", f"{_n(b['default_pct'])}%; later the last budget is kept"),
                                          ("Code ceiling", f"{_n(invariants.SWING_MAX_BUDGET_PCT)}%"),
                                          ("Core re-sized when swing exposure moved by", _p(b["core_rescale_deadband_nav"]))],
                              "codes": [], "seat": "pm", "badge": "Budget"})
            except (KeyError, TypeError, ValueError):
                continue
    return cards


def hard_limit_cards() -> list[dict[str, Any]]:
    i = invariants
    g = lambda name, default=None: getattr(i, name, default)    # noqa: E731
    cards = [
        {"id": "H1", "title": "Total exposure", "plain": f"All positions together stay at most {_p(i.GROSS_HARD_MAX)} of the portfolio.",
         "numbers": [("Gross exposure, at most", _p(i.GROSS_HARD_MAX))]},
        {"id": "H2", "title": "Kill switch", "plain": "Whatever the policy file says, code stops adding risk and proposes selling "
                                                      f"everything at a fall of {_p(1 - i.HALT_AT_PEAK_FRACTION)} from the best value ever reached, at the latest.",
         "numbers": [("Halt at a fall of", _p(1 - i.HALT_AT_PEAK_FRACTION)), ("Measured from", "the lifetime peak")]},
        {"id": "H3", "title": "A stop-loss on every opening order", "plain": "Every order that opens a position carries a stop-loss at the broker.",
         "numbers": [("Required", _yes(i.STOP_LOSS_ON_EVERY_OPEN))]},
        {"id": "H4", "title": "A person approves every order", "plain": "No code path places an order without the operator's approval.",
         "numbers": [("Required", _yes(i.HUMAN_APPROVAL_REQUIRED))]},
        {"id": "H5", "title": "Only the agent portfolio", "plain": "Only the dedicated agent portfolio is ever touched; never the main account.",
         "numbers": [("Enforced", _yes(i.NEVER_TOUCH_MAIN_ACCOUNT))]},
        {"id": "H6", "title": "Swing ceilings", "plain": "The swing policy may be stricter than these, never looser.",
         "numbers": [("Open swing trades, at most", _n(g("SWING_MAX_OPEN"))), ("Shorts, at most", _n(g("SWING_MAX_SHORT"))),
                     ("One trade's size, at most", _p(g("SWING_MAX_SIZE_NAV"))), ("New trades in 7 days, at most", _n(g("SWING_MAX_NEW_7D"))),
                     ("Loss at the stop, long, at most", _p(g("SWING_MAX_LONG_LOSS_NAV"))),
                     ("Loss at the stop, short, at most", _p(g("SWING_MAX_SHORT_LOSS_NAV"))),
                     ("Stop distance, long, at most", _p(g("SWING_MAX_LONG_STOP_PCT"))),
                     ("Stop distance, short, at most", _p(g("SWING_MAX_SHORT_STOP_PCT"))),
                     ("Open risk at the stops, at most", _p(g("SWING_MAX_OPEN_RISK_NAV"))),
                     ("Gap allowance, at least", f"{_n(g('SWING_MIN_GAP_MULT'))}×"),
                     ("Time stop including the extension, at most", f"{_n(g('SWING_MAX_TOTAL_SESSIONS'))} sessions"),
                     ("Net reward against risk, at least", _n(g("SWING_MIN_NET_RR"))),
                     ("An approved entry is stale after", f"{_n(g('SWING_MAX_ENTRY_VALID_MIN'))} min"),
                     ("Model calls per slot, at most", _n(g("SWING_MAX_LLM_CALLS_PER_SLOT"))),
                     ("Declared cost per leg, at least", f"{_n(g('SWING_MIN_DECLARED_COST_PCT_PER_LEG'))}%"),
                     ("Loss brake fires no deeper than", _p(g("SWING_BRAKE_MIN_PNL_NAV"))),
                     ("Drawdown scaling starts no later than", _p(g("SWING_DD_SCALE_MIN_FROM_PEAK"))),
                     ("Size under drawdown scaling, at most", _p(g("SWING_DD_SCALE_MAX_SIZE_NAV")))]},
        {"id": "H7", "title": "Swing budget ceiling", "plain": f"The council sets the swing budget itself, but code never lets it exceed {_n(g('SWING_MAX_BUDGET_PCT'))}% of the portfolio.",
         "numbers": [("Swing budget, at most", f"{_n(g('SWING_MAX_BUDGET_PCT'))}%")]},
        {"id": "H8", "title": "Go-live switches", "plain": "Constants in the code decide what may trade at all; changing one is a code change, not a setting.",
         "numbers": [("Swing book trades live", _yes(g("SWING_BOOK_LIVE"))), ("Stock sleeve live", _yes(g("STOCK_SLEEVE_LIVE"))),
                     ("Broker news feed allowed", _yes(g("BROKER_FEED_ENABLED")))]},
    ]
    for c in cards:
        c.update(key="", codes=[], seat="code")
    return cards


def rule_book(policy_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """The rules page: hard limits (code constants), the core rules (policy/risk.yaml, R1-R21 + MC,
    IB, AP, RC, PR) and the swing rules (policy/swing.yaml, S0-S18, SB16 and the swing budget). Each
    card: id (the anchor, e.g. "R14", "S8"), title, plain (one sentence), numbers [(label, value)]
    in % or plain units (never a raw yaml key), codes [{code, words}] and seat (badge colour)."""
    return {"hard": hard_limit_cards(), "core": core_rule_cards(policy_dir), "swing": swing_rule_cards(policy_dir)}


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
        asset=lambda k: asset_page(k) if k in line_set.info else "",
        flag_words=flag_words, plain_terms=plain_terms,
    )
    env.globals["move_words"] = lambda b: SCREEN_MOVE_WORDS.get(b or "", "—")
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
        swing_page=lambda k: "",         # replaced in build() once the swing asset pages are known
        rule_link=rule_link, rule_href=rule_href, rule_anchor=rule_anchor, agent_of_step=AGENT_OF_STEP,
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
    # paper runs newer than the last live/rehearsal cycle: the project is running on paper
    last_paper = view.paper_rows[-1].slot if view.paper_rows else None
    paper_now = (st.state == "AWAITING_ACCOUNT" and last_paper is not None
                 and (latest is None or last_paper >= latest.doc.slot))
    if paper_now:
        label, css = PAPER_STATUS
    elif mode == "rehearsal" and st.state == "AWAITING_ACCOUNT":
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
        "mode": mode, "mode_chip": chip(MODE_CHIP, mode), "paper": paper_now,
        "prelive": st.state == "AWAITING_ACCOUNT" or mode in (None, "rehearsal"),
        "built": fmt_when(now), "built_clock": fmt_clock(now),
    }


def hemicycle_seats(radius: float = 40.0, dot: float = 4.2) -> dict[str, Any]:
    """The council as a hemicycle: one seat per agent in speaking order, left to right along a
    half circle (the brand mark and the home page's council)."""
    n = len(AGENT_SPECS)
    dots = []
    for i, spec in enumerate(AGENT_SPECS):
        angle = math.pi - i * math.pi / (n - 1)
        dots.append({"slug": spec.slug, "name": spec.name, "n": i + 1,
                     "cx": f"{radius * math.cos(angle):.2f}", "cy": f"{-radius * math.sin(angle):.2f}"})
    pad = dot + 1.0
    return {"dots": dots, "r": f"{dot:.1f}",
            "box": f"{-radius - pad:.1f} {-radius - pad:.1f} {2 * (radius + pad):.1f} {radius + 2 * pad:.1f}"}


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
    for cv in view.cycles:                      # a line that left the book still gets its page
        c = cv.doc
        lines.seen(lines.sort(set(c.reference) | set(c.risk.final_x if c.risk else {})
                              | {leg.line for leg in (c.plan.legs if c.plan else [])}))
    env = make_env(lines)
    geo = Geometry()
    env.globals["ring_cls"] = geo.ring
    status = _status_context(view, now)
    disclaimer = load_disclaimer(policy_dir.parent)
    if status["prelive"]:
        disclaimer = prelive_disclaimer(disclaimer, rehearsal=status["mode"] == "rehearsal")
    nav = list(NAV)
    if view.has_swing:                          # the swing page joins the menu once there is a swing record
        nav.insert(2, SWING_NAV)
    if view.paper_rows or view.has_swing:       # the numbered decisions page (paper and live)
        nav.insert(1, DECISIONS_NAV)
    common = {
        "csp": Markup(CSP),               # a constant; single quotes must not be entity-escaped
        "nav": tuple(nav),
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
        "seats": hemicycle_seats(),
        "teaser_seats": hemicycle_seats(40.0, 4.7),
        "said_cap": HISTORY_CAP,
        "brand_seats": hemicycle_seats(40.0, 7.0),
    }
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "cycles").mkdir(parents=True)
    written: list[Path] = []

    def render(template: str, target: str, root: str, page: str, **ctx: Any) -> None:
        html = redacted_chips(env.get_template(template).render(**common, root=root, page=page, **ctx))
        if target.startswith("agents/"):        # agent shorthand in words (text nodes only)
            html = plain_html(html, "swing" if target[7:-5] in SWING_ROLES_ALL else "core")
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
    prompts = prompt_files(prompts_dir)
    sources = agent_sources(view, lines, runs, linked)        # live runs + paper decisions, newest first
    agents = build_agents(view, linked, prompts, str(council_cfg.get("model", "")), runs, lines, sources)
    for agent in agents:
        agent["track"] = agent_track(agent, prompts)
    calls_all = role_calls(view)
    swing_pages = swing_seat_pages(view, lines, prompts, calls_all) if view.has_swing else []
    latest = view.cycles[0] if view.cycles else None
    ops_on_time = sum(1 for r in view.ops if r.status == "on_time")
    # the performance chart in two shapes: wide from 641 px, narrow on phones (text stays legible)
    chart = performance_chart(view.performance, width=1000, height=260)
    chart_narrow = performance_chart(view.performance, width=360, height=240, labels=False)
    sealed = sealed_runs(view)
    holdings = build_holdings(view, lines, geo, risk.get("killswitch", {}), status)
    assets = build_assets(view, lines, holdings, geo, linked)
    sw_assets = swing_assets(view, now) if view.has_swing else {}
    asset_pages = {a["asset"]["line"] for a in assets} | {k for k in sw_assets if asset_page(k)}
    env.globals["swing_page"] = lambda k: asset_page(k) if k in asset_pages else ""
    roster = build_roster(latest, runs[latest.doc.cycle_id] if latest else None,
                          transcripts[latest.doc.cycle_id] if latest else None, lines)
    render("index.html.j2", "index.html", "", "portfolio", latest=latest, bmap=book_map(holdings, geo),
           paper=paper_home(view, lines, geo),
           split=book_split(latest),
           roster=roster,
           pending=[dict(x, chip=chip(DECISION_CHIP, x["state"] or "awaiting_publication")) for x in sealed if x["state"] in PENDING_STATES],
           executing=[x for x in sealed if x["state"] in EXECUTING_STATES],
           run=runs[latest.doc.cycle_id] if latest else None,
           tr_latest=transcripts[latest.doc.cycle_id] if latest else None,
           holdings=holdings,
           recent=[(cv, runs[cv.doc.cycle_id], transcripts[cv.doc.cycle_id]) for cv in view.cycles[:5]],
           cycles_count=len(view.cycles), ops_count=len(view.ops), ops_on_time=ops_on_time,
           chart=chart, chart_narrow=chart_narrow, points=view.performance[-10:][::-1],
           controls=[sr for sr in CONTROL_SERIES if any(x["key"] == sr[0] for x in (chart or {}).get("series", []))])
    render("cycles.html.j2", "cycles.html", "", "runs", meetings=meeting_cards(view, lines, runs),
           gaps=missed_slots(view))
    for cv in view.cycles:
        render("cycle.html.j2", f"cycles/{cv.doc.cycle_id}.html", "../", "runs", cv=cv, c=cv.doc,
               run=runs[cv.doc.cycle_id], tr=transcripts[cv.doc.cycle_id],
               sw=swing_run_view(cv.doc.swing, geo, view.swing))
    seating = seat_cards(view, agents, swing_pages, sources, lines, calls_all)
    stats_all = agent_role_stats(view)
    llm_calls = sum(stats_all[k]["calls"] for k in stats_all)
    llm_ok = sum(stats_all[k]["ok"] for k in stats_all)
    render("agents.html.j2", "agents/index.html", "../", "agents", agents=agents, seating=seating,
           meetings_count=len(sources), paper_count=len(view.paper_cycles), cycles_count=len(view.cycles),
           llm_calls=llm_calls, llm_ok=llm_ok, swing_agents=swing_pages,
           model=str(council_cfg.get("model", "")), think=bool(council_cfg.get("think", False)))
    for agent in agents:
        render("agent.html.j2", f"agents/{agent['slug']}.html", "../", "agents", agent=agent, agents=agents,
               swing_agents=swing_pages)
    for card in swing_pages:
        render("swing_agent.html.j2", card["page"], "../", "agents", agent=card, agents=agents,
               swing_agents=swing_pages)
    for a in assets:
        render("asset.html.j2", a["asset"]["page"], "../", "portfolio", a=a, book_target=holdings["target"],
               others=[{"page": x["asset"]["page"], "ticker": x["asset"]["ticker"], "ac": x["asset"]["ac"],
                        "held": bool(x["row"] and abs(x["row"]["weight"]) > EPS)} for x in assets],
               swing=sw_assets.get(a["asset"]["line"]))
    have = {a["asset"]["line"] for a in assets}
    for k, row in sorted(sw_assets.items()):
        if k not in have and asset_page(k):
            render("swing_asset.html.j2", asset_page(k), "../", "swing", s=row)
    if view.has_swing:
        render("swing.html.j2", "swing/index.html", "../", "swing", sp=swing_page_view(view, geo))
    if view.paper_rows or view.has_swing:
        render("decisions.html.j2", "decisions/index.html", "../", "decisions", rows=decision_rows(view),
               paper=paper_home(view, lines, geo))
        for no, doc in sorted(view.paper_cycles.items()):
            after = view.paper_books.get(no)
            if after is None and view.paper_latest is not None and view.paper_latest.decision_no == no:
                after = SimpleNamespace(book=view.paper_latest.book, as_of=view.paper_latest.as_of)
            dv = decision_view(doc, view.paper_verified.get(no, False), lines)
            if doc.cycle_id in runs:            # a live run of the same slot has its own page
                dv["run_href"] = f"cycles/{doc.cycle_id}.html"
            render("decision.html.j2", paper_href(no), "../../", "decisions", d=dv,
                   book=paper_book_home(after.book, after.as_of, geo) if after is not None else None)
    render("how.html.j2", "how.html", "", "how", roster=load_roster(prompts_dir, policy_dir),
           paper_no=max(view.paper_cycles) if view.paper_cycles else None)
    render("rules.html.j2", "rules.html", "", "rules", rules=load_rules(policy_dir), book=rule_book(policy_dir))
    render("record.html.j2", "record.html", "", "record", incidents=view.incidents, withdrawn=load_withdrawn(),
           rv=record_view(view, geo))
    for old, new, name in REDIRECTS:
        path = out_dir / old
        path.write_text(redirect_page(new, name), encoding="utf-8")
        written.append(path)

    static_out = out_dir / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for src in sorted(STATIC.rglob("*")):
        if src.is_file() and not src.name.startswith("."):
            dest = static_out / src.relative_to(STATIC)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(dest)
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
