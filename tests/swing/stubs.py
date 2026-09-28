"""Stub fixtures for the swing council (SW-3): news, fact cards, role replies, the injected gate.
No network, no LLM, no broker. Values are chosen to be distinctive so the blind-Skeptic test can
search the rendered text for them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from council.models.facts import NewsItem
from council.swing.council import ContextRow, GateResult, OpenTrade, SwingInputs
from council.swing.facts import FactCard

SLOT = datetime(2026, 9, 29, 14, 40, tzinfo=UTC)
THESIS = "Zebra-quill thesis: the guidance raise is underappreciated by the street"
WHY_NOT = "Octarine reason: flows have not caught up with the revision"
STOP, TARGET, TSTOP = 0.0733, 0.1777, 11


def news(nid: str, *, symbols: list[str], hours_ago: float = 20, title: str = "8-K: Results of Operations",
         source: str = "sec", form: str | None = "8-K", items: list[str] | None = None) -> NewsItem:
    t = SLOT - timedelta(hours=hours_ago)
    kw: dict[str, Any] = {"form": form, "items": items if items is not None else ["2.02"]} if source == "sec" else {}
    return NewsItem(id=nid, title=title, symbols=symbols, published_at=t, available_at=t, source=source, **kw)


P_ACME = "P:0a1b2c3d"
P_WIDG = "P:1b2c3d4e"
P_GLOBEX = "P:2c3d4e5f"
P_FED = "P:3d4e5f60"
N_ACME = "N:4e5f6071"


def reading() -> list[NewsItem]:
    return [
        news(P_ACME, symbols=["ACME"]),
        news(P_WIDG, symbols=["WIDG"], hours_ago=30),
        news(P_GLOBEX, symbols=["GLOBEX"], hours_ago=10),
        news(P_FED, symbols=[], title="Federal Reserve statement", source="fed_board", form=None, items=[]),
        news(N_ACME, symbols=["ACME"], title="Acme wins a large contract", source="etoro_feed", form=None),
    ]


def card(line: str, side: str = "long", *, sigma: float = 0.8, age: int = 1, ok: bool = True,
         live_sigma: float | None = None, **extra: Any) -> FactCard:
    fields: dict[str, Any] = {
        "news_age_sessions": age, "move_since_news_close_pct": 1.2, "move_since_news_close_sigma": sigma,
        "gap_pct": 0.4, "vol_ratio_since": 1.1, "rel_move_since_pct": 0.3, "sector_move_since_pct": 0.5,
        "atr14_pct": 2.1, "dist_52w_high_pct": -9.5, "trend": "up", "adv_bucket": ">200M",
        "crowding": "unknown", "rev_yoy": 12.5, "earnings_next": None, "earnings_confirmed": False,
        "sector_etf": "XLK",
    }
    if live_sigma is not None:
        fields["move_since_news_live_sigma"] = live_sigma
    fields.update(extra)
    c = FactCard(line_id=line, side=side, slot=SLOT.isoformat(), ok=ok, reason=None if ok else "no_facts",
                 fields=fields)
    c.catalyst_items.append({"id": P_ACME, "form": "8-K", "items": ["2.02"], "titles": ["Results of Operations"]})
    return c


def idea(ticker: str = "ACME", *, side: str = "long", setup: str = "post_earnings_drift",
         catalysts: list[str] | None = None, claim: str = "8-K item 2.02 results filed; FY guide raised") -> dict[str, Any]:
    return {"ticker": ticker, "side": side, "setup": setup, "catalyst_ids": catalysts or [P_ACME],
            "catalyst_claim": claim, "thesis": THESIS, "why_not_priced_in": WHY_NOT, "entry": "now",
            "stop_pct": STOP, "target_pct": TARGET, "time_stop_days": TSTOP,
            "invalidation": "A close back below the pre-filing level."}


def scout(*ideas: dict[str, Any]) -> dict[str, Any]:
    return {"ideas": list(ideas), "passed": []}


def verdict(ref: str = "idea:1", *, verdict: str = "pass", priced_in: str = "partly", news_status: str = "new",
            supports: bool = True, side_ok: bool = True, ids: list[str] | None = None,
            line: str = "ACME") -> dict[str, Any]:
    ids = ids or [f"X:{line}:rev_yoy", f"X:{line}:dist_52w_high_pct"]
    return {"idea_ref": ref, "catalyst_supports_claim": supports, "claim_supports_side": side_ok,
            "verdict": verdict, "priced_in": priced_in, "news_status": news_status, "regime": "neutral",
            "crowding": "unknown",
            "reasons": [{"text": "Revenue growth is a fact the move does not contain.", "evidence_ids": ids[:1]},
                        {"text": "Room to the 52-week high.", "evidence_ids": ids[1:2] or ids[:1]}],
            "what_would_change_my_mind": "A restatement.", "second_order": None}


def case(ref: str = "idea:1", line: str = "ACME", bear: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"argument": "Numbers.", "strongest_opposing_fact_id": f"X:{line}:dist_52w_high_pct",
                           "claims": [{"claim_id": "c1", "ref": ref, "text": "Small move since the filing.",
                                       "evidence_ids": [f"X:{line}:move_since_news_close_sigma"]}]}
    if bear:
        out["rebuttals"] = [{"claim_id": "c1", "verdict": "concede", "text": "True.", "evidence_ids": []}]
    return out


def pm(*actions: tuple[str, str], line: str = "ACME") -> dict[str, Any]:
    acts = []
    for ref, action in actions:
        a: dict[str, Any] = {"ref": ref, "action": action, "evidence_ids": [f"X:{line}:rev_yoy"], "reason": "r"}
        if action == "enter":
            a.update(stop_pct=0.06, target_pct=0.15, time_stop_days=10)
        acts.append(a)
    return {"actions": acts, "decisive_fact": {"text": "t", "evidence_id": f"X:{line}:rev_yoy"}, "dismissed": []}


def inputs(**kw: Any) -> SwingInputs:
    base: dict[str, Any] = {
        "slot": SLOT, "reading": reading(),
        "context": [ContextRow("F:SPX:ret_5d", "S&P 500 5-day return 0.8%"),
                    ContextRow("F:SEMIS:trend", "semiconductors trend up")],
        "core_summary": "core book near its reference levels",
    }
    base.update(kw)
    return SwingInputs(**base)


def gate_with(cards: dict[str, FactCard], fail: dict[str, str] | None = None):
    """An injected code gate: ref -> card by the idea's line id; `fail` maps a line id to a drop."""
    seen: list[list[str]] = []

    async def gate(ideas):
        seen.append([i.ref for i in ideas])
        out = {}
        for i in ideas:
            if fail and i.line_id in fail:
                out[i.ref] = GateResult(False, fail[i.line_id])
            else:
                out[i.ref] = GateResult(True, None, cards.get(i.line_id, card(i.line_id, i.idea.side)))
        return out

    gate.seen = seen  # type: ignore[attr-defined]
    return gate


def trade(ref: str = "trade:t1", ticker: str = "NVDA", triggers: tuple[str, ...] = ("thesis_break",)) -> OpenTrade:
    return OpenTrade(ref=ref, ticker=ticker, side="long", days_held=4, to_stop_pct=3.3, to_target_pct=6.6,
                     triggers=triggers, facts=(ContextRow("X:NVDA:ret_5d", "ret_5d = -2.1"),))
