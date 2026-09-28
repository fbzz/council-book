"""`council why <cycle> <TICKER>` for a swing idea or trade (design swing-book.md rev 2, §7.1; SW-7).

The chain per idea, in the design's order:
    scout -> code gate (resolve + facts, chase, best 3, budget: each rule pass / fail / not reached)
    -> skeptic (verdict, priced_in, reasons, code override) -> debate claims -> PM votes (the
    replicates) -> S-rules final -> engine book limits -> plan -> approval (entry guard) -> fill
    -> exits
and, for a trade on the ticker, every review trigger and action since its entry (the ledger's
swing events).

Two sources, the same words:
  - `ledger_lines`: the PRIVATE record (`CycleRecord.extras["swing"]`, `council.swing.record`) plus
    the ledger's decision, legs, trades and events. Operator terminal only (the caller checks).
  - `public_lines`: the revealed public swing section (agent-safe; no PM levels, no ledger state).
Percent distances only: no price, rate, unit or amount is ever printed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

GATE_RULES = (
    ("resolve", "resolve the ticker (eToro instrument, SEC registrant) and build the fact card",
     ("not_eligible", "no_facts", "no_catalyst_id", "unresolved", "not_tradable")),
    ("chase", "the move since the news is under the hard sigma cap", ("chased",)),
    ("best", "among the best ideas that get a Skeptic call", ("not_best_3",)),
    ("budget", "the slot's call budget leaves a Skeptic call", ("budget_no_skeptic",)),
)
SCOUT_CODES = ("setup_paper_only", "catalyst_not_admitted", "catalyst_not_about_ticker", "recently_rejected")


def _pct(v: Any) -> str:
    return f"{100.0 * float(v):.2f}%" if isinstance(v, int | float) else "n/a"


def _gate_lines(idea: Mapping[str, Any]) -> list[str]:
    stage, code = idea.get("stage"), idea.get("drop_code")
    out = []
    if (stage == "dropped_by_code" and code in SCOUT_CODES) or code == "setup_paper_only":
        out.append(f"  code gate: not reached (dropped at the Scout check: {code})")
        return out
    failed_at = None
    if stage == "dropped_by_code":
        for i, (_, _, codes) in enumerate(GATE_RULES):
            if code in codes or any(str(code or "").startswith(c) for c in codes):
                failed_at = i
                break
        if failed_at is None:
            failed_at = 0
    for i, (key, words, _) in enumerate(GATE_RULES):
        if failed_at is None or i < failed_at:
            mark = "pass"
        elif i == failed_at:
            mark = f"FAIL ({code})"
        else:
            mark = "not reached"
        out.append(f"  code gate · {key}: {mark} — {words}")
    flags = (idea.get("gate") or {}).get("card_flags") or []
    if flags:
        out.append(f"  fact card flags: {', '.join(map(str, flags))}")
    return out


def _skeptic_lines(v: Mapping[str, Any] | None) -> list[str]:
    if not v:
        return ["  skeptic: not reached"]
    body = v.get("verdict") or {}
    head = f"  skeptic ({v.get('model') or 'model n/a'}): {v.get('status')}"
    if v.get("said") and v.get("said") != v.get("status"):
        head += f" (said {v.get('said')}; code: {v.get('override') or v.get('code')})"
    elif v.get("code"):
        head += f" ({v.get('code')})"
    out = [head]
    if body:
        out.append(f"    priced in {body.get('priced_in')} · news {body.get('news_status')} · regime "
                   f"{body.get('regime')} · crowding {body.get('crowding')} · catalyst supports claim "
                   f"{body.get('catalyst_supports_claim')} · claim supports side {body.get('claim_supports_side')}")
        for r in body.get("reasons") or []:
            out.append(f"    - {r.get('text')} [{', '.join(r.get('evidence_ids') or [])}]")
        if body.get("what_would_change_my_mind"):
            out.append(f"    would change its mind: {body['what_would_change_my_mind']}")
    return out


def _debate_lines(record: Mapping[str, Any], ref: str) -> list[str]:
    out = []
    for role in ("bull", "bear"):
        case = record.get(role) or {}
        for c in case.get("claims") or []:
            if c.get("ref") == ref:
                out.append(f"  debate · {role} {c.get('claim_id')}: {c.get('text')} "
                           f"[{', '.join(c.get('evidence_ids') or [])}]")
        for r in case.get("rebuttals") or []:
            out.append(f"  debate · {role} rebuttal of {r.get('claim_id')}: {r.get('verdict')} — {r.get('text')}")
    return out or ["  debate: no claim about this idea"]


def _idea_lines(record: Mapping[str, Any], idea: Mapping[str, Any]) -> list[str]:
    ref = str(idea.get("ref"))
    cats = ", ".join(str(c.get("id")) for c in idea.get("catalysts") or [])
    out = [f"{idea.get('ticker')} {idea.get('side')} · {ref} ({idea.get('idea_id') or 'no ledger id'}) · setup "
           f"{idea.get('setup')}{'' if idea.get('live_setup') else ' (PAPER-only setup)'}",
           f"  scout: catalysts {cats or 'none'}; claim: {idea.get('catalyst_claim')}",
           f"    thesis: {idea.get('thesis')}",
           f"    levels: stop {_pct(idea.get('stop_pct'))}, target {_pct(idea.get('target_pct'))}, time stop "
           f"{idea.get('time_stop_days')} sessions"]
    if idea.get("carried_from"):
        out.append(f"    carried from: {', '.join(idea['carried_from'])}")
    out += _gate_lines(idea)
    stage = idea.get("stage")
    if stage == "dropped_by_code":
        out.append(f"  stopped: {idea.get('drop_code')}")
        return out
    out += _skeptic_lines(idea.get("verdict"))
    if stage in ("skeptic", "waiting"):
        out.append(f"  stopped at the Skeptic: {idea.get('drop_code')}")
        return out
    out += _debate_lines(record, ref)
    votes = idea.get("votes")
    if votes:
        out.append(f"  PM: enter {votes.get('enter')} of {votes.get('replicates')} replicates"
                   + (f" ({votes.get('failed')} failed)" if votes.get("failed") else "")
                   + (f"; median levels stop {_pct(votes.get('stop_pct'))}, target {_pct(votes.get('target_pct'))}"
                      if votes.get("stop_pct") is not None else ""))
    else:
        out.append("  PM: no vote recorded")
    if stage == "pm":
        out.append(f"  stopped at the PM: {idea.get('drop_code')}")
        return out
    rule = idea.get("rule_code")
    out.append(f"  S-rules final: {'FAIL (' + str(rule) + ')' if rule else 'pass'}")
    if stage == "risk" and rule:
        return out
    if stage == "risk":
        out.append(f"  not planned: {idea.get('drop_code')}")
    return out


def _execution_lines(ticker: str, *, hold_reasons: Iterable[str], legs: Sequence[Any], decision: Any,
                     trade: Any | None) -> list[str]:
    from council.swing.book import line_id

    try:
        line = line_id(ticker)
    except ValueError:
        line = f"SW_{ticker}"
    limits = [h for h in hold_reasons if str(h).startswith(f"{line}:")]
    out = [f"  engine book limits: {'; '.join(map(str, limits)) if limits else 'pass'}"]
    mine = [leg for leg in legs if getattr(leg, "line", None) == line or getattr(leg, "symbol", None) == ticker]
    out.append("  plan: " + ("; ".join(f"leg {getattr(x, 'seq', '?')} {getattr(x, 'kind', '?')}" for x in mine)
                             if mine else "no leg"))
    if decision is not None:
        out.append(f"  approval: decision {getattr(decision, 'state', 'n/a')}"
                   + (f" ({getattr(decision, 'reason', '')})" if getattr(decision, "reason", "") else ""))
    if trade is not None:
        out.append(f"  fill: trade {trade.trade_id} state {trade.state}")
    return out


def trade_lines(trade: Any, events: Sequence[Mapping[str, Any]]) -> list[str]:
    d = trade.detail or {}
    out = [f"trade {trade.trade_id} {trade.ticker} {trade.side} · state {trade.state} · size "
           f"{_pct(d.get('size_nav'))} of NAV · stop {_pct(d.get('stop_pct'))}, target {_pct(d.get('target_pct'))}"
           f" · time stop {trade.time_stop_date or 'n/a'}"]
    for e in events:
        to_state = e.get("to_state")
        move = f" {e.get('from_state') or '-'} -> {to_state}" if to_state else ""
        out.append(f"  {str(e.get('created_at'))[:16]} {e.get('kind')}{move}"
                   + (f": {e.get('reason')}" if e.get("reason") else ""))
    if "r_declared" in d:
        out.append(f"  outcome: {float(d['r_declared']):+.2f}R net of the declared cost ({d.get('exit_kind', 'exit')})")
    return out


def ledger_lines(record: Mapping[str, Any], ticker: str, *, hold_reasons: Iterable[str] = (),
                 legs: Sequence[Any] = (), decision: Any = None, trades: Sequence[Any] = (),
                 events: Sequence[Mapping[str, Any]] = ()) -> list[str] | None:
    """The private chain for `ticker` (None when the cycle has no idea and no trade on it)."""
    swing = (record.get("extras") or {}).get("swing") if isinstance(record, Mapping) else None
    ticker = ticker.upper().replace("_", ".")
    ideas = [i for i in ((swing or {}).get("ideas") or []) if str(i.get("ticker", "")).upper() == ticker]
    mine = [t for t in trades if str(t.ticker).upper() == ticker]
    if not ideas and not mine:
        return None
    out: list[str] = []
    for idea in ideas:
        out += _idea_lines(swing or {}, idea)
        if idea.get("stage") in ("planned", "approved", "executed", "missed", "expired"):
            trade = next((t for t in mine if t.idea_id == idea.get("idea_id")), None)
            out += _execution_lines(ticker, hold_reasons=hold_reasons, legs=legs, decision=decision, trade=trade)
        out.append("")
    for t in mine:
        out += trade_lines(t, [e for e in events if e.get("trade_id") == t.trade_id])
        out.append("")
    return out


def public_lines(section: Any, ticker: str) -> list[str] | None:
    """The same chain from the revealed public section (`PublicSwingSection`)."""
    if section is None:
        return None
    line = ticker.upper().replace(".", "_").replace("-", "_")
    out: list[str] = []
    for i in section.ideas:
        if i.ticker != line:
            continue
        out.append(f"{i.ticker} {i.side} · {i.ref} · setup {i.setup}{'' if i.live_setup else ' (PAPER-only setup)'}")
        out.append(f"  scout: catalysts {', '.join(c.id for c in i.catalysts) or 'none'}; claim: {i.catalyst_claim}")
        out.append(f"    levels: stop {i.stop_pct:.2f}%, target {i.target_pct:.2f}%, time stop {i.time_stop_days} sessions")
        if i.verdict is not None:
            v = i.verdict
            out.append(f"  skeptic: {v.verdict}" + (f" (said {v.said}; code: {v.code_override})" if v.said else "")
                       + f" · priced in {v.discounted} · news {v.news_status} · regime {v.regime}")
            out += [f"    - {r.text} [{', '.join(r.evidence)}]" for r in v.reasons]
        for case, role in ((section.bull, "bull"), (section.bear, "bear")):
            for c in (case.claims if case is not None else []):
                if c.ref == i.ref:
                    out.append(f"  debate · {role} {c.claim_id}: {c.text}")
        if i.votes is not None:
            out.append(f"  PM: enter {i.votes.enter} of {i.votes.replicates} replicates")
        out.append(f"  reached: {i.stage_reached}" + (f" ({i.drop_code})" if i.drop_code else ""))
        out.append("")
    for t in section.trades:
        if t.ticker == line:
            out.append(f"trade {t.trade_id} {t.side} · {t.state} · {t.days_held} sessions held · stop "
                       f"{t.stop_pct:.2f}%, target {t.target_pct:.2f}%")
    return out or None


__all__ = ["GATE_RULES", "ledger_lines", "public_lines", "trade_lines"]
