"""SW-7 fixtures: one stub swing slot run through the real council, record and redaction.

`slot_record()` runs `run_swing_stage` with stub gateways (no LLM, no broker) on four Scout ideas:
  idea:1 ACME   post-earnings drift  -> Skeptic pass -> PM 3 of 3 enter -> planned (live)
  idea:2 WIDG   news continuation    -> Skeptic wait (restated, priced in mostly); a reason cites the live layer
  idea:3 GLOBEX news continuation    -> an `N:` (broker feed) catalyst; Skeptic reject
  idea:4 HOOLI breakout            -> a paper-only setup (no call)
WIDG is a carried-forward idea (it waited in `ORIGIN`); its thesis quotes the origin cycle's feed
text in the `quoting` variant.

`trade_rows(nav)` builds ledger swing trades whose PRIVATE fields depend on the account (units,
amounts, fees, ids); the public view must not.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

from council.ledger.db import SwingTradeRow
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.models.facts import NewsItem
from council.policy import Policy
from council.swing import council as sc
from council.swing.record import swing_record
from council.swing.roles import catalyst_index
from tests.swing import stubs as s

CYCLE = "2026-09-29T1440Z"
ORIGIN = "2026-09-28T1440Z"
N_GLOBEX = "N:5f607182"
P_HOOLI = "P:6a7b8c9d"
FEED_TITLE = "Globex shares jump after a large licensing deal is announced"
ORIGIN_FEED = ("Widget Corp raises its full year outlook as orders from data center customers "
               "accelerate sharply in the third quarter")
LIVE_VALUE = "1.873"              # the live-layer sigma the Skeptic quotes; must never be published
MAIN, OTHER = "deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"


def reading() -> list[NewsItem]:
    base = s.reading()
    t = s.SLOT - timedelta(hours=6)
    base.append(NewsItem(id=N_GLOBEX, title=FEED_TITLE, symbols=["GLOBEX"], published_at=t, available_at=t,
                         source="etoro_feed"))
    base.append(s.news(P_HOOLI, symbols=["HOOLI"], hours_ago=12, title="8-K: Other Events", items=["8.01"]))
    return base


def _skeptic(user: str, rep: int) -> dict[str, Any]:
    if "IDEA TO REVIEW: idea:1\n" in user:
        return s.verdict("idea:1", line="ACME")
    if "IDEA TO REVIEW: idea:2\n" in user:
        v = s.verdict("idea:2", line="WIDG", priced_in="mostly", news_status="restated",
                      ids=["X:WIDG:move_since_news_live_sigma", "X:WIDG:rev_yoy"])
        v["reasons"][0]["text"] = f"The stock is already up {LIVE_VALUE} sigma today on the news."
        return v
    if "IDEA TO REVIEW: idea:3\n" in user:
        v = s.verdict("idea:3", line="GLOBEX", verdict="reject", priced_in="fully")
        v["what_would_change_my_mind"] = "A second customer confirming the licensing terms."
        return v
    raise AssertionError("unknown idea")


def run_slot(policy: Policy, *, quoting: bool = False, sink: Any = None) -> tuple[Any, dict[str, Any]]:
    widg = s.idea("WIDG", setup="news_continuation", catalysts=[s.P_WIDG])
    if quoting:
        widg["thesis"] = "As the feed said, " + ORIGIN_FEED.lower()
    ideas = [s.idea("ACME"), widg,
             s.idea("GLOBEX", setup="news_continuation", catalysts=[N_GLOBEX],
                    claim="Broker feed reports a licensing deal"),
             s.idea("HOOLI", setup="breakout", catalysts=[P_HOOLI])]
    main = {"scout": s.scout(*ideas), "swing_bull": s.case(), "swing_bear": s.case(bear=True),
            "swing_pm": lambda user, rep: s.pm(("idea:1", "enter"))}
    gw = StubGateway(responses=main, model=MAIN)
    skg = StubGateway(responses={"skeptic": _skeptic}, model=OTHER)
    cards = {"WIDG": s.card("WIDG", live_sigma=float(LIVE_VALUE)), "GLOBEX": s.card("GLOBEX")}
    inputs = s.inputs(reading=reading())
    res = asyncio.run(sc.run_swing_stage(gw, PromptRegistry(), policy, inputs, gate=s.gate_with(cards),
                                         skeptic_gw=skg, sink=sink))
    cats = catalyst_index(inputs.reading, inputs.screen_rows, slot=inputs.slot)
    return res, cats


def slot_record(policy: Policy, *, quoting: bool = False, live: bool = True) -> dict[str, Any]:
    res, cats = run_slot(policy, quoting=quoting)
    accepted = [a.ref for a in res.entries()]
    return swing_record(res, catalysts=cats, live=live, accepted=accepted,
                        idea_ids={r: f"idea:20260929_{r.split(':')[1]}" for r in res.ideas},
                        carried_from={"idea:2": [ORIGIN]},
                        links={s.P_ACME: "https://www.sec.gov/Archives/edgar/data/1/0001.htm"},
                        live_setups=policy.swing.setups_live)


def texts(*, origin_ok: bool = True) -> dict[str, list[str] | None]:
    """{cycle: licensed feed texts its agents saw}: this cycle's broker item, the origin's."""
    return {CYCLE: [FEED_TITLE], ORIGIN: [ORIGIN_FEED] if origin_ok else None}


# ------------------------------------------------------------------------------ trades
NAVS = {"low": (1500.0, 2000.0), "ref": (2000.0, 2000.0), "big": (20000.0, 20000.0)}   # (nav, peak)


def trade_rows(nav: float, peak: float = 0.0) -> list[SwingTradeRow]:
    """An open, a closed-at-target and a closed-at-stop trade. Units, amounts, fees, ids and the
    actual cost scale with the account; the percent-only fields do not."""
    k = nav / 2000.0
    t0 = datetime(2026, 9, 21, 18, 45, tzinfo=UTC)

    def row(tid: str, ticker: str, side: str, state: str, open_rate: float, close_rate: float | None,
            *, days: int, stop: float, target: float, extra: dict[str, Any] | None = None) -> SwingTradeRow:
        detail: dict[str, Any] = {"size_nav": 0.08, "stop_pct": stop, "target_pct": target, "tp_mode": "on_open",
                                  "live": True, "beta": 1.1, "sector_etf_ret": 0.01,
                                  "amount_usd": round(0.08 * nav, 2), "fee_usd": round(1.0 + 0.001 * nav, 2),
                                  "actual_cost_pct": round(0.9 + 300.0 / nav, 4), "nav_usd": nav, "peak_usd": peak}
        detail.update(extra or {})
        return SwingTradeRow(
            trade_id=tid, idea_id=f"idea:{tid[6:]}", origin_cycle="2026-09-21T1840Z", decision_id=f"dec-{tid}",
            entry_seq=1, ticker=ticker, instrument_id=int(1000 + 7 * k), side=side, state=state,
            position_ids=[int(2_000_000_000 + 13 * k)], units=round(0.08 * nav / open_rate, 6),
            open_rate=open_rate, sl_rate=open_rate * (1 - stop if side == "long" else 1 + stop),
            tp_rate=open_rate * (1 + target if side == "long" else 1 - target),
            time_stop_date=(t0.date() + timedelta(days=days + 14)).isoformat(), close_rate=close_rate,
            opened_at=t0, closed_at=(t0 + timedelta(days=days)) if close_rate else None,
            detail=detail, created_at=t0, updated_at=t0)

    return [
        row("trade:open_1", "NVDA", "long", "open", 181.37, None, days=0, stop=0.06, target=0.15),
        row("trade:won_1", "ACME", "long", "closed_target", 57.19, 65.77, days=6, stop=0.05, target=0.15),
        row("trade:lost_1", "BRK.B", "short", "closed_stop", 482.11, 506.22, days=3, stop=0.05, target=0.12),
    ]


def paper_rows() -> list[dict[str, Any]]:
    groups = [("executed", 1.2), ("pm_passed", -0.4), ("skeptic_rejected", 0.3), ("skeptic_wait", -1.0),
              ("code_dropped", 0.5), ("paper_only", -0.2), ("missed", 0.8), ("skeptic_wait", 0.6)]
    out = []
    for k, (g, r) in enumerate(groups):
        out.append({"paper_id": f"p{k}", "ticker": "ACME", "status": "closed", "stop_pct": 0.05,
                    "ret_pct": 100.0 * 0.05 * r, "exit_reason": "time", "record": {"group": g}})
    out.append({"paper_id": "p_open", "ticker": "NVDA", "status": "open", "stop_pct": 0.06, "ret_pct": None,
                "record": {"group": "executed"}})
    return out


def benchmark_days() -> list[dict[str, Any]]:
    d0 = date(2026, 9, 21)
    return [{"day": (d0 + timedelta(days=i)).isoformat(), "sq8_ret": 0.002 * ((-1) ** i),
             "matched_idx_ret": 0.001, "idx_hold_ret": 0.0015 if i else None} for i in range(6)]


# ------------------------------------------------------------------------------ a site journal
SWING_CYCLE = "2026-09-25T1840Z"
SITE_JOURNAL = "tests/fixtures/site_journal/journal"


def health() -> Any:
    from council.publish import redact

    return redact.public_skeptic_health(["caught", "caught", "missed", "caught"],
                                        ["pass", "wait", "reject", "pass", "reject", "wait", "pass", "reject"])


def swing_book_doc(nav: float = 2000.0, peak: float = 2000.0) -> Any:
    from council.publish import redact

    return redact.public_swing_book(
        trade_rows(nav, peak), as_of=datetime(2026, 9, 25, 20, 30, tzinfo=UTC), paper_rows=paper_rows(),
        benchmark_days=benchmark_days(), health=health(), live=True, live_since=date(2026, 9, 21),
        today=date(2026, 9, 25), resamples=2000)


def make_swing_journal(out_root: Any, policy: Policy) -> Any:
    """The site fixture journal plus one sealed swing slot (the real record -> public_cycle ->
    seal path) and journal/swing/latest.json. Returns the journal directory."""
    import shutil
    from pathlib import Path

    from council.paths import REPO_ROOT
    from council.publish import commit_reveal, journal, redact
    from tests.fixtures import make_site_journal as msj

    root = Path(out_root)
    target = root / "journal"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(REPO_ROOT / SITE_JOURNAL, target)
    uni = msj.universe()
    spec = msj.Spec(SWING_CYCLE, "live", "calm", "reviewed_no_action", "none", 0.3, 2)
    rec, pk = msj.record(spec, uni, PromptRegistry())
    rec.extras["swing"] = slot_record(policy)
    swing_texts = texts()
    swing_texts[SWING_CYCLE] = swing_texts.pop(CYCLE)
    doc = redact.public_cycle(rec, pk, lines=uni, swing_texts=swing_texts, swing_trades=trade_rows(2000.0, 2000.0),
                              swing_health=health())
    commitment, salt, sealed = commit_reveal.seal_bytes(doc, sealed_at=rec.slot + timedelta(minutes=3),
                                                        salt_hex="ab" * 32)
    files = journal.commitment_files(commitment) | journal.reveal_files(sealed, salt, commitment)
    ops_path = target / "ops" / "cycles.jsonl"
    files |= journal.ops_files(ops_path.read_bytes(), [redact.public_ops_row(rec)])
    files |= journal.swing_files(swing_book_doc())
    journal.write_files(root, files)
    return target
