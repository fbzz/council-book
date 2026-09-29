"""The private swing record of one swing slot (design swing-book.md rev 2, §7.1-§7.3; SW-7).

`swing_record(...)` turns a `SwingCouncilResult` (plus the S-rules final pass and the ledger idea
ids) into one JSON-safe dict for the ledger's `CycleRecord.extras["swing"]`. It is the single
source for:
  - `council why <cycle> <TICKER>` (`swing.trail`), the operator's full chain per idea;
  - the Scout reading list (`deliberation.reading.swing_citations`);
  - the public swing section (`publish.redact.public_swing_section`), which re-reads it field by
    field through allow-listed models.

What it keeps: tickers, sides, setups, the cited ids, the Scout's and the roles' own text, the
fact card's fields (live layer included: the ledger is private; redaction withholds it), the gate
and code outcomes, the Skeptic's verdict and what it said, the debate, the PM votes and the S-rule
code. What it never keeps: licensed feed text (an `N:` catalyst is its id only; its title stays in
the 7-day licensed capture) and anything the model never saw (no NAV, amounts or units).

`origin_texts(state_dir, cycles)` returns the licensed feed texts each earlier cycle's agents saw,
from the private capture, for the carried-forward leak scan (H9). A cycle whose licensed texts
were purged or never captured maps to None, and redaction then withholds (fail closed).

Pure except `origin_texts` (reads the private capture). No broker, no network.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA = "council-book/private-swing/v1"
# Card fields the record never keeps (money amounts; the prompt reads the bucket).
_NEVER = frozenset({"adv_usd_20d", "catalyst_items_feed"})
_OVERRIDES = ("catalyst_misread", "skeptic_incoherent", "skeptic_mostly_wait", "skeptic_stale_wait",
              "skeptic_prior_wait")


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json") if model is not None and hasattr(model, "model_dump") else None


def _catalyst(cid: str, meta: Any, links: Mapping[str, str]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": cid}
    if cid.startswith("N:") or meta is None:
        return row                                   # licensed: id only, never its title
    if cid.startswith("P:"):
        row["title"] = str(getattr(meta, "title", "") or "")
        if cid in links:
            row["link"] = links[cid]
    elif cid.startswith("S:"):
        row["form"] = getattr(meta, "form", None)
        row["items"] = list(getattr(meta, "items", ()) or ())
        row["title"] = str(getattr(meta, "title", "") or "")
    return row


def _verdict(outcome: Any, model: str) -> dict[str, Any] | None:
    if outcome is None:
        return None
    flags = list(getattr(outcome, "flags", ()) or ())
    code = getattr(outcome, "code", None)
    override = next((f for f in _OVERRIDES if f in flags or f == code), None)
    return {"status": outcome.status, "code": code, "said": getattr(outcome, "said", None),
            "override": override, "flags": flags, "model": model, "verdict": _dump(outcome.verdict)}


def _votes(result: Any, ref: str) -> dict[str, int] | None:
    agg = getattr(result, "aggregate", None)
    if agg is None:
        return None
    a = next((x for x in agg.actions if x.ref == ref), None)
    if a is None:
        return None
    enter = a.votes_for if a.action == "enter" else max(0, a.replicates - a.votes_for - a.failed_replicates)
    return {"enter": int(enter), "replicates": int(a.replicates), "failed": int(a.failed_replicates),
            "stop_pct": a.stop_pct, "target_pct": a.target_pct, "time_stop_days": a.time_stop_days}


def stage_of(outcome: Any, verdict: Any, *, paper_only: bool, entered: bool, accepted: bool,
             rule_code: str | None, live: bool) -> tuple[str, str | None]:
    """(stage_reached, drop_code) at seal time (before the human decision)."""
    if paper_only:
        return "dropped_by_code", "setup_paper_only"
    if accepted:
        return ("planned", None) if live else ("risk", "swing_book_paper_only")
    if rule_code:
        return "risk", rule_code
    stage = getattr(outcome, "stage", None)
    code = getattr(outcome, "code", None)
    if outcome is None and not entered:                  # the slot stopped (timeout, error) mid-way
        status = getattr(verdict, "status", None)
        if status == "pass":
            return "debate", "stage_aborted"
        if status is not None:
            return ("waiting" if status == "wait" else "skeptic"), getattr(verdict, "code", None)
        return "dropped_by_code", "stage_aborted"
    if stage in ("scout", "gate") and not entered:
        return "dropped_by_code", code or "dropped"
    if stage == "skeptic":
        status = getattr(verdict, "status", None)
        return ("waiting" if status == "wait" else "skeptic"), code
    if stage == "pm":
        return "pm", code
    return "pm", code


_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_:.\-]{0,63}$")


def safe_code(code: Any) -> str | None:
    """A drop code as stored on ledger rows: a code token only (never free text or a value)."""
    if code is None:
        return None
    return str(code) if _CODE.match(str(code)) else "unknown"


def drop_code_of(result: Any, ref: str, *, accepted: bool, rule_code: str | None, live: bool) -> str | None:
    """The idea's seal-time drop code (`stage_of`) for the ledger rows (swing_ideas / paper_trades)."""
    entered = ({a.ref for a in result.entries()} if getattr(result, "aggregate", None) is not None else set())
    outcome = next((o for o in getattr(result, "outcomes", []) if o.ref == ref), None)
    paper_only = any(i.ref == ref for i in getattr(result, "paper_only", []))
    idea = result.ideas.get(ref)
    _, drop = stage_of(outcome, getattr(idea, "verdict", None), paper_only=paper_only, entered=ref in entered,
                       accepted=accepted, rule_code=rule_code, live=live)
    return safe_code(drop)


def swing_record(
    result: Any,
    *,
    catalysts: Mapping[str, Any],
    live: bool,
    accepted: Iterable[str] = (),
    rule_codes: Mapping[str, str] | None = None,
    idea_ids: Mapping[str, str] | None = None,
    carried_from: Mapping[str, Sequence[str]] | None = None,
    links: Mapping[str, str] | None = None,
    live_setups: Iterable[str] = (),
) -> dict[str, Any]:
    """The private record (see the module docstring). `accepted`: idea refs that passed the S-rules
    final pass; `rule_codes`: {ref: S-rule public code} of those it dropped; `idea_ids`: {ref:
    ledger idea id}; `carried_from`: {ref: earlier cycle ids}; `links`: {P: id: public link}."""
    accepted = set(accepted)
    rule_codes = dict(rule_codes or {})
    idea_ids = dict(idea_ids or {})
    carried_from = {k: list(v) for k, v in (carried_from or {}).items()}
    links = dict(links or {})
    live_set = set(live_setups)
    entered = {a.ref for a in result.entries()} if getattr(result, "aggregate", None) is not None else set()
    outcomes = {o.ref: o for o in getattr(result, "outcomes", [])}
    paper_only = {i.ref for i in getattr(result, "paper_only", [])}
    ideas = []
    for ref, idea in sorted(result.ideas.items(), key=lambda kv: int(kv[0].split(":")[1])):
        if getattr(idea, "canary", False):
            continue                                     # H11: a canary never reaches any record
        x = idea.idea
        card = getattr(idea, "card", None)
        fields = {k: v for k, v in (card.fields if card is not None else {}).items() if k not in _NEVER}
        stage, drop = stage_of(outcomes.get(ref), idea.verdict, paper_only=ref in paper_only,
                               entered=ref in entered, accepted=ref in accepted,
                               rule_code=rule_codes.get(ref), live=live)
        ideas.append({
            "ref": ref, "idea_id": idea_ids.get(ref), "ticker": x.ticker, "line_id": idea.line_id,
            "side": x.side, "setup": x.setup,
            "live_setup": (x.setup in live_set) if live_set else ref not in paper_only,
            "catalysts": [_catalyst(c, catalysts.get(c), links) for c in x.catalyst_ids],
            "catalyst_claim": x.catalyst_claim, "thesis": x.thesis, "why_not_priced_in": x.why_not_priced_in,
            "invalidation": x.invalidation, "entry": x.entry,
            "stop_pct": x.stop_pct, "target_pct": x.target_pct, "time_stop_days": x.time_stop_days,
            "gate": {"ok": card is not None and bool(card.ok),
                     "reason": getattr(outcomes.get(ref), "code", None) if stage == "dropped_by_code" else None,
                     "card_flags": list(card.flags) if card is not None else []},
            "facts": fields,
            "verdict": _verdict(idea.verdict, getattr(result, "skeptic_model", "")),
            "votes": _votes(result, ref),
            "stage": stage, "drop_code": drop, "rule_code": rule_codes.get(ref),
            "carried_from": carried_from.get(ref, []),
        })
    return {
        "schema": SCHEMA, "slot": result.slot, "live": bool(live),
        "skeptic_model": getattr(result, "skeptic_model", ""),
        "ideas": ideas,
        "outcomes": [{"ref": o.ref, "ticker": o.ticker, "stage": o.stage, "code": o.code}
                     for o in getattr(result, "outcomes", [])],
        "bull": _dump(getattr(result, "bull", None)),
        "bear": _dump(getattr(result, "bear", None)),
        "actions": [_dump(a) for a in (result.aggregate.actions if getattr(result, "aggregate", None) else [])],
        "code_exits": list(getattr(result, "code_exits", [])),
        "flags": list(getattr(result, "flags", [])),
    }


def origin_texts(state_dir: Path, cycles: Iterable[str]) -> dict[str, list[str] | None]:
    """{cycle id: the licensed feed texts its agents saw}; None when they cannot be read (purged,
    never captured): the caller must then treat the carried text as unverifiable (withhold it)."""
    from council.deliberation.capture import load_inputs, load_licensed

    out: dict[str, list[str] | None] = {}
    for cid in dict.fromkeys(cycles):
        try:
            inputs = load_inputs(Path(state_dir), cid)
        except (FileNotFoundError, ValueError, OSError):
            out[cid] = None
            continue
        if not inputs.licensed_items:
            out[cid] = []
            continue
        if inputs.licensed_purged_at is not None:
            out[cid] = None
            continue
        try:
            lic = load_licensed(Path(state_dir), cid)
        except (FileNotFoundError, ValueError, OSError):
            lic = None
        out[cid] = None if lic is None else [t for sec in lic.texts.values() for t in sec.values() if t]
    return out


__all__ = ["SCHEMA", "drop_code_of", "origin_texts", "safe_code", "stage_of", "swing_record"]
