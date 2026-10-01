"""The private trace of a `council cycle --paper --trace-all` swing slot (paper only).

`trace_record(...)` turns the swing council's trace (`SwingCouncilResult.trace`, built by
`council.swing.council._trace_rest`) plus the two S-rules final passes (the REAL entries and every
TRACED entry, same book, same rules) into one JSON-safe dict stored at
`CycleRecord.extras["swing"]["trace"]` in the PAPER ledger. `council paper report` renders it.

Per idea: `real_gate_outcome` (what the real pipeline would have done, and at which stage) beside
`traced_outcome` (where the idea ended when nothing blocked), and, for every traced entry, the
would-be paper leg (size % NAV, stop %, target %, time stop) the planner would place if the swing
book were live (SWING_BOOK_LIVE stays False: nothing is placed).

Never stores licensed feed text: a broker / RSS (`N:`) reading item keeps its id, source and age
only (its headline lives in the 7-day licensed capture, which the report reads separately).
Pure: no I/O, no broker, no network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

SCHEMA = "council-book/private-swing-trace/v1"
SUMMARY_MAX = 600
STAGE_ORDER = ("scout", "gate", "skeptic", "pm", "rules", "leg")


def _leg(v: Any) -> dict[str, Any]:
    out = {"ok": bool(v.ok), "code": v.code, "rule": getattr(v, "rule", None), "flags": list(v.flags or ())}
    if v.ok:
        out.update(size_nav_pct=round(float(v.size_nav) * 100.0, 3),
                   stop_pct=round(float(v.stop_pct or 0) * 100.0, 3),
                   target_pct=round(float(v.target_pct or 0) * 100.0, 3),
                   time_stop_days=v.time_stop_days,
                   time_stop_date=v.time_stop_date.isoformat() if v.time_stop_date else None,
                   cost_rt_pct=round(float(v.cost_rt_pct), 3) if v.cost_rt_pct is not None else None)
    return out


def _reading(items: Sequence[Any], slot: datetime) -> list[dict[str, Any]]:
    rows = []
    for i in sorted(items, key=lambda x: (x.available_at, x.id), reverse=True):
        nid = str(i.id)
        licensed = nid.startswith("N:")
        row: dict[str, Any] = {
            "id": nid, "source": str(getattr(i, "source", "") or ""), "feed": getattr(i, "feed", None),
            "age_h": round(max(0.0, (slot - i.available_at).total_seconds() / 3600.0), 1),
            "symbols": list(getattr(i, "symbols", []) or []), "licensed": licensed,
            "form": getattr(i, "form", None), "items": list(getattr(i, "items", []) or []),
        }
        if not licensed:                         # public-domain text only; licensed text stays in the capture
            row["title"] = str(i.title)
            row["summary"] = str(getattr(i, "summary", "") or "")[:SUMMARY_MAX]
            if getattr(i, "link", None):
                row["link"] = str(i.link)
        rows.append(row)
    return rows


def _trade(t: Any) -> dict[str, Any]:
    return {"ref": t.ref, "ticker": t.ticker, "side": t.side, "days_held": t.days_held,
            "to_stop_pct": t.to_stop_pct, "to_target_pct": t.to_target_pct, "triggers": list(t.triggers)}


def _budget(bd: Any) -> dict[str, Any] | None:
    if bd is None:
        return None
    pct = float(bd.pct)
    return {"swing_pct": round(pct, 3), "core_pct": round(100.0 - pct, 3), "median_pct": bd.median_pct,
            "votes": bd.votes, "fallback": bool(bd.fallback), "open_pct": round(float(bd.open_pct), 3)}


def trace_record(result: Any, inputs: Any, *, real_rules: tuple[Sequence[Any], Sequence[Any]],
                 traced_rules: tuple[Sequence[Any], Sequence[Any]] | None, rule_codes: Mapping[str, str],
                 budget: Any, slot: datetime, policy: Any = None) -> dict[str, Any]:
    """The trace dict (see the module docstring)."""
    tr = getattr(result, "trace", None) or {}
    ideas_tr: Mapping[str, Any] = tr.get("ideas") or {}
    real_ok = {v.ref: v for v in real_rules[0]}
    t_ok, t_drop = ({v.ref: v for v in traced_rules[0]}, {v.ref: v for v in traced_rules[1]}) if traced_rules else ({}, {})
    ideas = []
    refs = sorted(set(ideas_tr) | set(result.ideas), key=lambda r: int(r.split(":")[1]) if ":" in r else 0)
    for ref in refs:
        t = dict(ideas_tr.get(ref) or {})
        real = dict(t.get("real") or {"stage": "unknown", "code": None, "note": ""})
        traced = dict(t.get("traced") or {"stage": "unknown", "code": None, "note": ""})
        if real.get("stage") == "pm" and real.get("code") == "enter":     # the real S-rules pass
            if ref in rule_codes:
                real = {"stage": "rules", "code": rule_codes[ref], "note": real.get("note", "")}
            elif ref in real_ok:
                real = {"stage": "leg", "code": "paper_leg", "note": real.get("note", "")}
        leg = None
        if traced.get("stage") == "pm" and traced.get("code") == "enter":
            v = t_ok.get(ref) or t_drop.get(ref)
            if v is not None:
                leg = _leg(v)
                traced = ({"stage": "leg", "code": "paper_leg", "note": traced.get("note", "")} if v.ok
                          else {"stage": "rules", "code": v.code, "note": traced.get("note", "")})
        idea = result.ideas.get(ref)
        x = getattr(idea, "idea", None)
        ideas.append({
            "ref": ref, "ticker": t.get("ticker") or getattr(idea, "ticker", ""),
            "side": getattr(x, "side", ""), "setup": getattr(x, "setup", ""),
            "has_card": getattr(idea, "card", None) is not None, "batch": t.get("batch"),
            "real_gate_outcome": real, "traced_outcome": traced, "paper_leg": leg,
            "real_leg": _leg(real_ok[ref]) if ref in real_ok else None,
            "same": (real.get("stage"), real.get("code")) == (traced.get("stage"), traced.get("code")),
        })
    chosen = [i for i in ideas if (i["paper_leg"] or {}).get("ok")]
    return {
        "schema": SCHEMA, "slot": slot.isoformat(), "batch_size": tr.get("batch_size"),
        "inputs": {
            "reading": _reading(list(getattr(inputs, "reading", []) or []), slot),
            "screen": [dict(r) for r in getattr(inputs, "screen_rows", []) or []],
            "context": [{"id": c.id, "text": c.text} for c in getattr(inputs, "context", []) or []],
            "open_trades": [_trade(t) for t in getattr(inputs, "open_trades", []) or []],
            "book_map": [{"id": c.id, "text": c.text} for c in getattr(inputs, "book_map", []) or []],
            "carried": [{"ticker": c.idea.ticker, "side": c.idea.side, "wait_day": c.wait_day}
                        for c in getattr(inputs, "carried", []) or []],
            "recent_ideas": list(getattr(inputs, "recent_ideas", []) or []),
            "core_summary": str(getattr(inputs, "core_summary", "") or ""),
            "max_entries": getattr(inputs, "max_entries", None),
        },
        "ideas": ideas,
        "scout_passed": list(getattr(result, "scout_passed", []) or []),
        "batches": list(tr.get("batches") or []),
        "real_budget_flags": list(tr.get("real_budget_flags") or []),
        "budget": _budget(budget),
        "chosen": [{"ref": i["ref"], "ticker": i["ticker"], "side": i["side"], **(i["paper_leg"] or {})}
                   for i in chosen],
        "real_chosen": [i["ref"] for i in ideas if i["real_gate_outcome"].get("code") == "paper_leg"],
    }


__all__ = ["SCHEMA", "STAGE_ORDER", "trace_record"]
