"""`council paper report <cycle>`: one self-contained local HTML file of a PAPER cycle's whole flow.

Reads the PAPER state only (`<state>/paper`: its ledger record and its private input capture) and
writes ONE HTML file (inline CSS, no script, no external request: every link is a same-file anchor;
evidence URLs are shown as plain text). The file holds licensed feed text and the models' private
analyses: it is a local, private file (0600) and must never be published.

Top to bottom: what it chose and why (+ real gate vs traced outcome per idea), inputs, Scout, code
gate, Skeptic, debate, PM, S-rules / would-be paper legs / swing budget, core council. Works for any
paper cycle; the per-idea real-vs-traced columns and every idea's debate / vote need a
`--trace-all` cycle (`extras.swing.trace`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

WARNING = ("PRIVATE: this file contains licensed feed text and the models' private analyses. "
           "Keep it local; never publish, commit or share it.")
SEATS = {"bull": "#3fc7a0", "bear": "#f07a5f", "pm": "#a68bfa", "risk": "#7fa7c9", "news": "#e3b341",
         "macro": "#54a31b", "scout": "#3fb8e6", "skeptic": "#e05fb0", "gate": "#9aa5b1"}
KEY_FACTS = (
    ("move_since_news_live_pct", "move since news (live) %"), ("move_since_news_live_sigma", "move since news (live) σ"),
    ("move_since_news_close_pct", "move since news (close) %"), ("move_since_news_close_sigma", "move since news (close) σ"),
    ("move_today_live_pct", "move today (live) %"), ("news_age_sessions", "sessions since news"),
    ("sector_etf", "sector ETF"), ("sector_move_since_pct", "sector move since %"),
    ("spx_move_since_pct", "S&P 500 move since %"), ("ndx_move_since_pct", "Nasdaq-100 move since %"),
    ("rel_move_since_pct", "move vs sector %"), ("trend", "trend"), ("ret_5d", "5-day return %"),
    ("ret_20d", "20-day return %"), ("dist_52w_high_pct", "to 52w high %"), ("dist_52w_low_pct", "to 52w low %"),
    ("atr14_pct", "ATR14 %"), ("sigma_daily", "daily σ %"), ("vol_ratio_since", "volume ratio since"),
    ("short_interest_pct_float", "short interest % float"), ("days_to_cover", "days to cover"),
    ("earnings_next", "next earnings"), ("earnings_confirmed", "earnings confirmed"),
    ("earnings_last_sessions_ago", "last earnings (sessions ago)"), ("filing_age_d", "filing age (days)"),
    ("rev_yoy", "revenue YoY %"), ("adv_bucket", "liquidity bucket"), ("crowding", "crowding"), ("beta_60d", "beta 60d"),
)

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1d2330;--mute:#5b6577;--line:#dfe3ea;--ok:#1f9d6b;--no:#c8553d;--warnbg:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f27;--ink:#e6e9ef;--mute:#98a2b3;--line:#2b323d;
--ok:#3fc7a0;--no:#f07a5f;--warnbg:#3a3012}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
Segoe UI,Roboto,sans-serif}main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:.2em 0}h2{font-size:1.2rem;margin:0 0 .5em}h3{font-size:1rem;margin:.8em 0 .3em}
section{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--c,#888);border-radius:10px;
padding:14px 16px;margin:14px 0}.warn{background:var(--warnbg);border:1px solid #e3b341;border-radius:8px;padding:10px 12px;
font-weight:600}.mute{color:var(--mute)}.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.8rem;
border:1px solid var(--line);margin:0 4px 2px 0}.ok{color:var(--ok);font-weight:600}.no{color:var(--no);font-weight:600}
table{border-collapse:collapse;width:100%;margin:.4em 0;font-size:.9rem}th,td{border-bottom:1px solid var(--line);
padding:4px 6px;text-align:left;vertical-align:top}th{color:var(--mute);font-weight:600}
.wrap{overflow-x:auto}.idea{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0}
.seat{font-weight:700;color:var(--c)}details{margin:.4em 0}summary{cursor:pointer;color:var(--mute)}
pre{white-space:pre-wrap;word-break:break-word;font-size:.82rem;background:var(--bg);padding:8px;border-radius:6px}
nav a{margin-right:10px;color:var(--mute)}.diff{background:rgba(240,122,95,.12)}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _num(v: Any, nd: int = 2) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int | float):
        return f"{v:.{nd}f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    return _e(v) if v not in (None, "") else "n/a"


def _frac_pct(v: Any) -> str:
    return f"{float(v) * 100:.2f}%" if isinstance(v, int | float) and not isinstance(v, bool) else "n/a"


def _table(head: Sequence[str], rows: Iterable[Sequence[Any]], *, raw: bool = False) -> str:
    rows = list(rows)
    if not rows:
        return '<p class="mute">none</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c if raw else _e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><tr>{th}</tr>{body}</table></div>'


def _section(sid: str, title: str, seat: str, body: str) -> str:
    return f'<section id="{sid}" style="--c:{SEATS[seat]}"><h2>{_e(title)}</h2>{body}</section>'


def _outcome(o: Mapping[str, Any] | None) -> str:
    if not o:
        return "n/a"
    code = o.get("code")
    good = code in ("paper_leg", "enter")
    txt = f"{o.get('stage')}: {code}" if code else f"{o.get('stage')}: passed"
    note = f' <span class="mute">({_e(o["note"])})</span>' if o.get("note") else ""
    return f'<span class="{"ok" if good else "no"}">{_e(txt)}</span>{note}'


def _details(summary: str, text: str) -> str:
    return f"<details><summary>{_e(summary)}</summary><pre>{_e(text)}</pre></details>"


# --------------------------------------------------------------------------------- loading
def load_cycle(state_dir: Path, cycle_id: str) -> dict[str, Any]:
    from council.deliberation.capture import check_cycle_id
    from council.ledger.db import LEDGER_FILE, Ledger

    check_cycle_id(cycle_id)
    ledger = Ledger(Path(state_dir) / LEDGER_FILE)
    rec = ledger.get_cycle(cycle_id)
    if rec is None:
        raise FileNotFoundError(f"no cycle {cycle_id} in the paper ledger")
    return rec


def captured_inputs(state_dir: Path, cycle_id: str) -> dict[str, str]:
    """{call key: the exact user text} of the swing and core calls (licensed text included while
    the 7-day capture keeps it). Empty when nothing was captured."""
    from council.deliberation.capture import load_inputs, load_licensed, user_text

    try:
        inputs = load_inputs(Path(state_dir), cycle_id)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    try:
        lic = load_licensed(Path(state_dir), cycle_id)
    except (FileNotFoundError, ValueError, OSError):
        lic = None
    out: dict[str, str] = {}
    for call in inputs.calls:
        try:
            text, ok = user_text(inputs, call, lic)
        except (KeyError, ValueError):
            continue
        out[call.call_key] = text + ("" if ok else "\n[licensed text purged]")
    return out


# --------------------------------------------------------------------------------- sections
def _summary(rec: Mapping[str, Any], sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    ideas = {i["ref"]: i for i in sw.get("ideas", [])}
    parts = []
    chosen = (tr or {}).get("chosen") or []
    if tr is None:
        acc = [i for i in sw.get("ideas", []) if i.get("stage") in ("risk", "planned") and not i.get("rule_code")]
        chosen = [{"ref": i["ref"], "ticker": i["ticker"], "side": i["side"]} for i in acc]
    rows = []
    for c in chosen:
        i = ideas.get(c["ref"], {})
        why = _pm_reason(tr, c["ref"]) or (i.get("thesis") or "")
        v = (i.get("verdict") or {}).get("verdict") or {}
        rows.append([f"{c.get('ticker')} {c.get('side')}", _num(c.get("size_nav_pct")) + "% NAV",
                     _num(c.get("stop_pct")) + "%", _num(c.get("target_pct")) + "%",
                     c.get("time_stop_date") or "n/a", f"Skeptic {v.get('verdict', 'n/a')}; PM: {why}"])
    parts.append("<h3>What it chose (swing, would-be paper legs)</h3>")
    parts.append(_table(["idea", "size", "stop", "target", "time stop", "why"], rows) if rows
                 else '<p>No swing entry. Every idea ended before a paper leg (see the table below).</p>')
    risk = rec.get("risk") or {}
    final, base = risk.get("final_w") or {}, risk.get("base_w") or {}
    moves = [[k, _frac_pct(base.get(k)), _frac_pct(final.get(k))] for k in sorted(set(final) | set(base))
             if abs(float(final.get(k) or 0) - float(base.get(k) or 0)) > 1e-9]
    parts.append("<h3>Core council</h3>")
    parts.append(f"<p>Basis <b>{_e(risk.get('basis', 'n/a'))}</b>; {len(moves)} line(s) change weight this cycle."
                 + (f" Hold reasons: {_e(', '.join(risk.get('hold_reasons') or []))}." if risk.get("hold_reasons") else "")
                 + "</p>")
    if moves:
        parts.append(_table(["line", "current weight", "target weight (after the risk engine)"], moves))
    if tr is not None:
        rows2 = []
        for i in tr.get("ideas", []):
            cls = "" if i.get("same") else ' class="diff"'
            rows2.append(f"<tr{cls}><td><a href=\"#{_e(i['ref'].replace(':', '-'))}\">{_e(i['ref'])}</a></td>"
                         f"<td>{_e(i.get('ticker'))} {_e(i.get('side'))}</td><td>{_outcome(i.get('real_gate_outcome'))}</td>"
                         f"<td>{_outcome(i.get('traced_outcome'))}</td></tr>")
        parts.append("<h3>Each idea: what the real pipeline would have done vs the full trace</h3>")
        parts.append('<div class="wrap"><table><tr><th>idea</th><th>ticker</th><th>real gate outcome</th>'
                     f'<th>traced outcome</th></tr>{"".join(rows2)}</table></div>')
    return f'<section id="summary" style="--c:{SEATS["pm"]}"><h2>What it chose, and why</h2>{"".join(parts)}</section>'


def _pm_reason(tr: Mapping[str, Any] | None, ref: str) -> str:
    for b in (tr or {}).get("batches", []):
        for rep in b.get("pm_replicates", []):
            for a in rep.get("accepted_actions") or []:
                if a.get("ref") == ref and a.get("action") == "enter":
                    return str(a.get("reason") or "")
    return ""


def _inputs(sw: Mapping[str, Any], tr: Mapping[str, Any] | None, calls: Mapping[str, str]) -> str:
    inp = (tr or {}).get("inputs") or {}
    out = []
    reading = inp.get("reading") or []
    out.append("<h3>Reading list</h3>")
    out.append(_table(["id", "source", "age (h)", "tickers", "headline / summary"], (
        [r["id"], (r.get("source") or "") + (f" via {r['feed']}" if r.get("feed") else ""), _num(r.get("age_h"), 1),
         ", ".join(r.get("symbols") or []) or "market-wide",
         (r.get("title") or "") + (f" | {r['summary']}" if r.get("summary") else "") + (f" [{r['link']}]" if r.get("link") else "")
         if not r.get("licensed") else "(licensed: headline in the Scout's exact input below)"] for r in reading)))
    out.append("<h3>Movers screen</h3>")
    out.append(_table(["id", "move σ", "move %", "volume ×", "sector"], (
        [r.get("id"), _num(r.get("move_sigma")), _num(r.get("move_pct")), _num(r.get("vol_ratio")), r.get("sector") or "n/a"]
        for r in inp.get("screen") or [])))
    out.append("<h3>Market context</h3>")
    out.append(_table(["id", "fact"], ([c["id"], c["text"]] for c in inp.get("context") or [])))
    out.append("<h3>Open swing book</h3>")
    out.append(_table(["trade", "ticker", "side", "days held", "to stop %", "to target %", "review triggers"], (
        [t["ref"], t["ticker"], t["side"], t["days_held"], _num(t.get("to_stop_pct")), _num(t.get("to_target_pct")),
         ", ".join(t.get("triggers") or []) or "none"] for t in inp.get("open_trades") or [])))
    if inp.get("book_map"):
        out.append("<h3>Book map</h3>" + _table(["id", "fact"], ([c["id"], c["text"]] for c in inp["book_map"])))
    if inp.get("carried"):
        out.append("<h3>Carried waits (day-2)</h3>" + _table(["ticker", "side", "parked on"], (
            [c["ticker"], c["side"], c["wait_day"]] for c in inp["carried"])))
    if inp.get("core_summary"):
        out.append(f"<p><b>Core book:</b> {_e(inp['core_summary'])}</p>")
    scout = next((v for k, v in calls.items() if k.startswith("scout:")), None)
    if scout:
        out.append(_details("The Scout's exact input (licensed text included)", scout))
    if not inp:
        out.append('<p class="mute">Structured inputs need a --trace-all cycle.</p>')
    return _section("inputs", "a) Inputs", "news", "".join(out))


def _scout(sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    out = []
    for i in sw.get("ideas", []):
        cats = ", ".join(c.get("id", "") for c in i.get("catalysts") or [])
        out.append(f'<div class="idea" id="{_e(i["ref"].replace(":", "-"))}"><b>{_e(i["ref"])} · {_e(i["ticker"])} '
                   f'{_e(i["side"])}</b> <span class="pill">{_e(i.get("setup"))}</span>'
                   f'<span class="pill">catalysts {_e(cats)}</span>'
                   f'<p><b>Catalyst claim:</b> {_e(i.get("catalyst_claim"))}<br><b>Thesis:</b> {_e(i.get("thesis"))}<br>'
                   f'<b>Why not priced in:</b> {_e(i.get("why_not_priced_in"))}<br><b>Invalidation:</b> {_e(i.get("invalidation"))}</p>'
                   f'<p>stop {_frac_pct(i.get("stop_pct"))} · target {_frac_pct(i.get("target_pct"))} · time stop '
                   f'{_e(i.get("time_stop_days"))} sessions · entry {_e(i.get("entry"))}</p></div>')
    drops = [o for o in sw.get("outcomes", []) if o.get("stage") == "scout" and o.get("ref") not in
             {i["ref"] for i in sw.get("ideas", [])}]
    if drops:
        out.append("<h3>Dropped at the Scout check</h3>" + _table(["ref", "ticker", "code"],
                                                                   ([o["ref"], o["ticker"], o["code"]] for o in drops)))
    passed = (tr or {}).get("scout_passed") or []
    out.append(f"<p><b>Passed on:</b> {_e(', '.join(passed)) or 'none listed'}</p>")
    if not sw.get("ideas"):
        out.insert(0, '<p class="mute">The Scout pitched no idea.</p>')
    return _section("scout", "b) Scout", "scout", "".join(out))


def _gate(sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    real = {i["ref"]: i.get("real_gate_outcome") for i in (tr or {}).get("ideas", [])}
    out = []
    for i in sw.get("ideas", []):
        facts = i.get("facts") or {}
        r = real.get(i["ref"])
        if r is not None:
            res = _outcome(r) if r.get("stage") in ("scout", "gate") else '<span class="ok">gate: passed</span>'
        else:
            res = _outcome({"stage": "gate", "code": (i.get("gate") or {}).get("reason")}) if i.get(
                "stage") == "dropped_by_code" else '<span class="ok">gate: passed</span>'
        rows = [[label, _num(facts.get(k))] for k, label in KEY_FACTS if k in facts]
        flags = ", ".join((i.get("gate") or {}).get("card_flags") or [])
        out.append(f'<div class="idea"><b>{_e(i["ref"])} · {_e(i["ticker"])} {_e(i["side"])}</b> — {res}'
                   + (f' <span class="mute">card flags: {_e(flags)}</span>' if flags else "")
                   + (_table(["fact", "value"], rows) if rows else '<p class="mute">no fact card</p>') + "</div>")
    return _section("gate", "c) Code gate (fact card, chase, day-2, net reward/risk)", "gate", "".join(out) or
                    '<p class="mute">no idea reached the gate</p>')


def _skeptic(sw: Mapping[str, Any]) -> str:
    out = [f'<p class="mute">Skeptic model: {_e(sw.get("skeptic_model") or "n/a")}</p>']
    for i in sw.get("ideas", []):
        vo = i.get("verdict")
        if not vo:
            continue
        v = vo.get("verdict") or {}
        reasons = _table(["reason", "evidence"], ([r.get("text"), ", ".join(r.get("evidence_ids") or [])]
                                                  for r in v.get("reasons") or []))
        status = vo.get("status")
        out.append(f'<div class="idea"><b>{_e(i["ref"])} · {_e(i["ticker"])} {_e(i["side"])}</b> — '
                   f'<span class="{"ok" if status == "pass" else "no"}">{_e(status)}</span>'
                   + (f' <span class="pill">code {_e(vo.get("code"))}</span>' if vo.get("code") else "")
                   + f'<span class="pill">said {_e(vo.get("said") or v.get("verdict"))}</span>'
                   f'<span class="pill">priced in {_e(v.get("priced_in"))}</span><span class="pill">news {_e(v.get("news_status"))}</span>'
                   f'<span class="pill">regime {_e(v.get("regime"))}</span><span class="pill">crowding {_e(v.get("crowding"))}</span>'
                   + reasons + f'<p><b>What would change its mind:</b> {_e(v.get("what_would_change_my_mind"))}</p>'
                   + (f'<p><b>Second order:</b> {_e(v["second_order"])}</p>' if v.get("second_order") else "")
                   + (_details("private analysis", v["analysis"]) if v.get("analysis") else "")
                   + (f'<p class="mute">flags: {_e(", ".join(vo.get("flags") or []))}</p>' if vo.get("flags") else "")
                   + "</div>")
    return _section("skeptic", "d) Skeptic (blind)", "skeptic", "".join(out))


def _case(seat: str, title: str, case: Mapping[str, Any] | None) -> str:
    if not case:
        return f'<p class="mute">{_e(title)}: no case</p>'
    claims = _table(["claim", "idea", "text", "evidence"], ([c.get("claim_id"), c.get("ref"), c.get("text"),
                                                             ", ".join(c.get("evidence_ids") or [])] for c in case.get("claims") or []))
    reb = ""
    if case.get("rebuttals"):
        reb = "<h3>Rebuttals</h3>" + _table(["bull claim", "verdict", "text", "evidence"], (
            [r.get("claim_id"), r.get("verdict"), r.get("text"), ", ".join(r.get("evidence_ids") or [])]
            for r in case["rebuttals"]))
    return (f'<div class="idea" style="--c:{SEATS[seat]}"><span class="seat">{_e(title)}</span><p>{_e(case.get("argument"))}</p>'
            f'{claims}<p class="mute">strongest opposing fact: {_e(case.get("strongest_opposing_fact_id"))}</p>{reb}</div>')


def _debate(sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    batches = (tr or {}).get("batches") or []
    out = []
    if batches:
        for k, b in enumerate(batches):
            out.append(f"<h3>Batch {k + 1}: {_e(', '.join(b.get('refs') or []) or 'open trades only')}</h3>")
            out.append(_case("bull", "Bull", b.get("bull")) + _case("bear", "Bear", b.get("bear")))
    else:
        out.append(_case("bull", "Bull", sw.get("bull")) + _case("bear", "Bear", sw.get("bear")))
    return _section("debate", "e) Debate", "bull", "".join(out))


def _pm(sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    out = []
    for k, b in enumerate((tr or {}).get("batches") or []):
        out.append(f"<h3>Batch {k + 1}</h3>")
        rows = []
        for rep in b.get("pm_replicates") or []:
            acts = rep.get("accepted_actions")
            if acts is None:
                rows.append([f"#{rep['replicate']}", "—", "failed validation (counts as pass/hold)", "", ""])
                continue
            for a in acts:
                lv = (f"stop {_frac_pct(a.get('stop_pct'))}, target {_frac_pct(a.get('target_pct'))}, "
                      f"{a.get('time_stop_days')} sessions") if a.get("action") == "enter" else ""
                rows.append([f"#{rep['replicate']}", a.get("ref"), a.get("action"), lv, a.get("reason")])
            dec = rep.get("decision") or {}
            if dec.get("swing_budget_pct") is not None:
                rows.append([f"#{rep['replicate']}", "budget", f"{dec['swing_budget_pct']}% NAV",
                             f"vote {'valid' if rep.get('budget_vote') is not None else 'refused'}",
                             dec.get("swing_budget_reason")])
        out.append(_table(["replicate", "idea", "action", "levels", "reason"], rows))
        out.append("<p><b>Aggregated:</b> " + "; ".join(
            f"{_e(a.get('ref'))} → {_e(a.get('action'))} ({_e(a.get('votes_for'))} of {_e(a.get('replicates'))})"
            for a in b.get("actions") or []) + "</p>")
    if not out:
        out.append(_table(["idea", "action", "votes", "stop", "target", "time stop"], (
            [a.get("ref"), a.get("action"), f"{a.get('votes_for')} of {a.get('replicates')}", _frac_pct(a.get("stop_pct")),
             _frac_pct(a.get("target_pct")), a.get("time_stop_days")] for a in sw.get("actions") or [])))
    return _section("pm", "f) Swing PM (replicates and the vote)", "pm", "".join(out))


def _rules(sw: Mapping[str, Any], tr: Mapping[str, Any] | None) -> str:
    out = []
    rows = []
    for i in (tr or {}).get("ideas", []):
        leg = i.get("paper_leg")
        if leg is None:
            continue
        if leg.get("ok"):
            rows.append([f"{i['ref']} {i['ticker']} {i['side']}", '<span class="ok">all S-rules pass</span>',
                         _num(leg.get("size_nav_pct")) + "%", _num(leg.get("stop_pct")) + "%",
                         _num(leg.get("target_pct")) + "%", _e(leg.get("time_stop_date") or "n/a"),
                         _num(leg.get("cost_rt_pct")) + "%"])
        else:
            rows.append([f"{_e(i['ref'])} {_e(i['ticker'])} {_e(i['side'])}",
                         f'<span class="no">{_e(leg.get("rule") or "")} {_e(leg.get("code"))}</span>', "", "", "", "", ""])
    if tr is not None:
        out.append("<p>The S-rules ran on every traced PM entry against the same book (the paper NAV). "
                   "SWING_BOOK_LIVE is False: no leg is placed, these are the legs the planner would place.</p>")
        out.append(_table(["entry", "S-rules", "size (NAV)", "stop", "target", "time stop", "round-trip cost"], rows, raw=True))
    else:
        out.append(_table(["idea", "stage", "drop / rule code"], ([i["ref"], i.get("stage"), i.get("rule_code") or i.get("drop_code")]
                                                                  for i in sw.get("ideas", []))))
    bd = (tr or {}).get("budget")
    if bd:
        out.append(f"<p><b>Swing budget:</b> {_num(bd.get('swing_pct'))}% NAV swing / {_num(bd.get('core_pct'))}% core "
                   f"(median vote {_num(bd.get('median_pct'))}, {_e(bd.get('votes'))} valid vote(s), open swing "
                   f"{_num(bd.get('open_pct'))}%{', FALLBACK: last budget kept' if bd.get('fallback') else ''}).</p>")
    if (tr or {}).get("real_budget_flags"):
        out.append(f"<p class=\"mute\">The real slot's call budget would have applied: {_e(', '.join(tr['real_budget_flags']))}</p>")
    return _section("rules", "g) S-rules, would-be paper legs and the swing budget", "risk", "".join(out))


def _core(rec: Mapping[str, Any]) -> str:
    out = []
    macro = rec.get("macro") or {}
    if macro:
        out.append(f'<div class="idea" style="--c:{SEATS["macro"]}"><span class="seat">Macro</span> regime '
                   f'<b>{_e(macro.get("regime"))}</b>' + _table(["driver"], ([json.dumps(d, ensure_ascii=False)]
                                                                         for d in macro.get("drivers") or [])) + "</div>")
    cards = rec.get("cards") or []
    if cards:
        out.append(f'<div class="idea" style="--c:{SEATS["news"]}"><span class="seat">News / analyst cards</span>'
                   + _table(["card", "role", "scope", "direction", "claim", "evidence", "qualifying"], (
                       [c.get("card_id"), c.get("role"), ", ".join(c.get("scope") or []), c.get("direction"), c.get("claim"),
                        ", ".join(c.get("evidence_ids") or []), _num(c.get("qualifying"))] for c in cards)) + "</div>")
    deb = rec.get("debate") or {}
    for key, seat, title in (("bull_open", "bull", "Bull (opening)"), ("bear", "bear", "Bear"),
                             ("bull_rebuttal", "bull", "Bull (rebuttal)")):
        c = deb.get(key)
        if c:
            prop = ", ".join(f"{k} {_num(v)}" for k, v in (c.get("proposal") or {}).items())
            out.append(_case(seat, title, c).replace("</div>", f'<p class="mute">proposal: {_e(prop) or "none"}</p></div>', 1))
    for r in rec.get("pm") or []:
        d = r.get("decision") or {}
        devs = _table(["line", "level", "direction", "reason", "evidence"], (
            [x.get("symbol"), _num(x.get("level")), x.get("direction"), x.get("reason"), ", ".join(x.get("evidence_ids") or [])]
            for x in d.get("deviations") or []))
        out.append(f'<div class="idea" style="--c:{SEATS["pm"]}"><span class="seat">PM replicate #{_e(r.get("replicate"))}</span> '
                   f'{"valid" if r.get("valid") else "INVALID"} · sided with {_e(d.get("sided_with"))} · decisive fact: '
                   f'{_e((d.get("decisive_fact") or {}).get("text"))}{devs}'
                   + (f"<p>No-change reason: {_e(d['no_change_reason'])}</p>" if d.get("no_change_reason") else "") + "</div>")
    ref = (rec.get("reference") or {}).get("entries") or {}
    bands = rec.get("bands") or {}
    risk = rec.get("risk") or {}
    lines = sorted(set(ref) | set(bands) | set(risk.get("final_w") or {}))
    out.append("<h3>Core target weights vs the reference</h3>" + _table(
        ["line", "reference weight", "ref level", "band (lo – hi)", "PM level", "banded level", "current",
         "proposed", "final", "band reasons"], (
            [k, _frac_pct((ref.get(k) or {}).get("weight_ref")), _num((bands.get(k) or {}).get("ref_level")),
             f"{_num((bands.get(k) or {}).get('lo'))} – {_num((bands.get(k) or {}).get('hi'))}" if k in bands else "n/a",
             _num((risk.get("raw_levels") or {}).get(k)), _num((risk.get("banded_levels") or {}).get(k)),
             _frac_pct((risk.get("base_w") or {}).get(k)),
             _frac_pct((risk.get("proposed_w") or {}).get(k)), _frac_pct((risk.get("final_w") or {}).get(k)),
             "; ".join((bands.get(k) or {}).get("reasons") or [])] for k in lines)))
    if risk:
        failed = [c for c in risk.get("checks") or [] if not c.get("passed")]
        out.append(f'<div class="idea" style="--c:{SEATS["risk"]}"><span class="seat">Risk engine</span> basis '
                   f'<b>{_e(risk.get("basis"))}</b> · gross {_num(risk.get("gross"))} · net {_num(risk.get("net"))} · '
                   f'ex-ante vol {_num(risk.get("ex_ante_vol"))} · {len(failed)} failed check(s)'
                   + _table(["rule", "name", "value", "limit", "detail"], ([c.get("rule_id"), c.get("name"), _num(c.get("value")),
                                                                         _num(c.get("limit")), c.get("detail")] for c in failed))
                   + (f"<p>Hold reasons: {_e(', '.join(risk.get('hold_reasons') or []))}</p>" if risk.get("hold_reasons") else "")
                   + "</div>")
    if rec.get("plan"):
        out.append(_details("the plan (paper: nothing is sent)", json.dumps(rec["plan"], indent=1, ensure_ascii=False)))
    return _section("core", "h) Core council", "macro", "".join(out) or '<p class="mute">no core decision recorded</p>')


def render(rec: Mapping[str, Any], calls: Mapping[str, str] | None = None) -> str:
    calls = calls or {}
    sw = (rec.get("extras") or {}).get("swing") or {}
    tr = sw.get("trace")
    flags = rec.get("flags") or []
    nav = "".join(f'<a href="#{a}">{t}</a>' for a, t in (
        ("summary", "summary"), ("inputs", "inputs"), ("scout", "scout"), ("gate", "gate"), ("skeptic", "skeptic"),
        ("debate", "debate"), ("pm", "PM"), ("rules", "rules"), ("core", "core")))
    head = (f'<h1>Paper cycle {_e(rec.get("cycle_id"))}</h1><p class="mute">slot {_e(rec.get("slot"))} · mode '
            f'{_e(rec.get("mode"))} · model {_e(rec.get("model"))} · status {_e(rec.get("status"))}'
            f'{" · trace-all" if tr else ""}</p><p class="warn">{_e(WARNING)}</p><nav>{nav}</nav>')
    if not sw:
        swing = _section("scout", "Swing book", "scout", '<p class="mute">No swing stage in this cycle (not a swing slot; '
                         'replay one with --at).</p>')
    else:
        swing = (_inputs(sw, tr, calls) + _scout(sw, tr) + _gate(sw, tr) + _skeptic(sw) + _debate(sw, tr)
                 + _pm(sw, tr) + _rules(sw, tr))
    foot = _details("cycle flags", "\n".join(flags)) if flags else ""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>Paper cycle trace</title><style>{CSS}</style></head><body><main>{head}"
            f"{_summary(rec, sw, tr) if sw else ''}{swing}{_core(rec)}{foot}</main></body></html>")


def write_report(state_dir: Path, cycle_id: str, out: Path | None = None) -> Path:
    """Render the paper cycle `cycle_id` from `state_dir` (must be the paper state dir) to `out`
    (default `<state_dir>/reports/<cycle>.html`), mode 0600."""
    state_dir = Path(state_dir)
    rec = load_cycle(state_dir, cycle_id)
    html = render(rec, captured_inputs(state_dir, cycle_id))
    path = Path(out) if out is not None else state_dir / "reports" / f"{cycle_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.chmod(path, 0o600)
    return path


__all__ = ["SEATS", "WARNING", "captured_inputs", "load_cycle", "render", "write_report"]
