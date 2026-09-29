"""The weekly Skeptic canary's event provider (design swing-book.md rev 2, §1.5; SW-4b).

`provider(state_dir)` returns the `SwingSources.canary_event(slot)` callable. Each week it plants ONE
already-resolved past event whose outcome is known: a public-domain SEC 8-K item 2.02 filing after
which the stock had ALREADY moved >= 3 sigma in the idea's direction, >= 5 sessions later. The only
right answer is `wait` or `reject` with `priced_in` mostly or fully (`canary.grade_canary`).

- The set: `state_dir/swing/canary_events.json` (the operator's private set, preferred: a list of
  records shaped like `BUILTIN_EVENTS`) when present and valid, else the checked-in `BUILTIN_EVENTS`.
  An invalid private file never breaks the cycle: the built-in set is used and the flag
  `swing_canary_set_invalid` is added.
- The rotation is by ISO week (`(iso year x 53 + iso week) mod n`); an event that does not qualify
  (`canary.qualifies`) is skipped for the next one; none qualifying -> None (`swing_canary_no_event`).
- The fact card is the event's recorded post-event reaction (the move since the pre-event close, in
  percent and in pre-event sigma, the opening gap and the volume ratio), dated at the canary slot:
  the catalyst reads as `age_sessions` US sessions before the slot. The model sees only what the
  blind `skeptic_input` renders; the canary flag stays code-side (H11).
- BUILTIN_EVENTS: large, widely reported post-earnings reactions of US large caps. The reaction
  figures are rounded approximations from public daily closes (the grade depends only on the
  direction and on the move being far beyond 3 sigma, which each of them was); the claims restate
  the filings' public content. Public-domain SEC items only; no licensed text.

No network, no clock of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from council.models.facts import public_news_id
from council.stocks.universe import try_normalise_id
from council.swing.canary import PastEvent, qualifies
from council.swing.facts import FactCard
from council.swing.roles import CatalystMeta

PRIVATE_SET = Path("swing") / "canary_events.json"
FLAG_INVALID = "swing_canary_set_invalid"
ITEM_202 = ("2.02",)
TITLE_202 = ("Results of Operations and Financial Condition",)
AVAILABLE_UTC = time(21, 5)                       # after the US close of the filing day
MAX_BACK_DAYS = 40

BUILTIN_EVENTS: tuple[dict[str, Any], ...] = (
    {"ticker": "NVDA", "side": "long", "filed": "2023-05-24", "age_sessions": 6, "sector_etf": "XLK",
     "claim": "8-K item 2.02 quarterly results filed; next-quarter revenue outlook far above the prior quarter",
     "move_pct": 27.0, "move_sigma": 4.6, "gap_pct": 26.0, "vol_ratio_since": 3.1},
    {"ticker": "META", "side": "long", "filed": "2023-02-01", "age_sessions": 5, "sector_etf": "XLC",
     "claim": "8-K item 2.02 quarterly results filed; revenue above the outlook, cost cuts and a larger buyback",
     "move_pct": 26.0, "move_sigma": 4.2, "gap_pct": 20.0, "vol_ratio_since": 2.8},
    {"ticker": "META", "side": "short", "filed": "2022-02-02", "age_sessions": 5, "sector_etf": "XLC",
     "claim": "8-K item 2.02 quarterly results filed; daily users fell and the revenue outlook was below trend",
     "move_pct": -28.0, "move_sigma": -5.0, "gap_pct": -24.0, "vol_ratio_since": 3.4},
    {"ticker": "NFLX", "side": "short", "filed": "2022-04-19", "age_sessions": 6, "sector_etf": "XLC",
     "claim": "8-K item 2.02 quarterly results filed; paid memberships declined and a further decline was guided",
     "move_pct": -38.0, "move_sigma": -6.0, "gap_pct": -37.0, "vol_ratio_since": 3.9},
    {"ticker": "PYPL", "side": "short", "filed": "2022-02-01", "age_sessions": 5, "sector_etf": "XLF",
     "claim": "8-K item 2.02 quarterly results filed; the revenue and user growth outlook was cut",
     "move_pct": -27.0, "move_sigma": -4.8, "gap_pct": -20.0, "vol_ratio_since": 3.0},
    {"ticker": "SNAP", "side": "long", "filed": "2022-02-03", "age_sessions": 5, "sector_etf": "XLC",
     "claim": "8-K item 2.02 quarterly results filed; first quarterly net profit and revenue above the outlook",
     "move_pct": 48.0, "move_sigma": 3.6, "gap_pct": 45.0, "vol_ratio_since": 3.5},
    {"ticker": "PLTR", "side": "long", "filed": "2023-05-08", "age_sessions": 6, "sector_etf": "XLK",
     "claim": "8-K item 2.02 quarterly results filed; a profitable quarter and a raised full-year profit outlook",
     "move_pct": 35.0, "move_sigma": 3.9, "gap_pct": 20.0, "vol_ratio_since": 3.2},
    {"ticker": "NVDA", "side": "long", "filed": "2024-02-21", "age_sessions": 5, "sector_etf": "XLK",
     "claim": "8-K item 2.02 quarterly results filed; data-center revenue and the outlook above expectations",
     "move_pct": 19.0, "move_sigma": 3.4, "gap_pct": 14.0, "vol_ratio_since": 2.2},
    {"ticker": "INTC", "side": "short", "filed": "2024-08-01", "age_sessions": 6, "sector_etf": "XLK",
     "claim": "8-K item 2.02 quarterly results filed; a weak outlook, the dividend suspended and a cost plan",
     "move_pct": -30.0, "move_sigma": -5.2, "gap_pct": -26.0, "vol_ratio_since": 3.6},
)
_REQUIRED = ("ticker", "side", "filed", "age_sessions", "claim", "move_pct", "move_sigma")


def _valid(rec: Any) -> bool:
    if not isinstance(rec, Mapping) or any(k not in rec for k in _REQUIRED):
        return False
    if rec["side"] not in ("long", "short") or try_normalise_id(rec["ticker"]) is None:
        return False
    try:
        date.fromisoformat(str(rec["filed"]))
    except ValueError:
        return False
    nums = [rec["age_sessions"], rec["move_pct"], rec["move_sigma"]]
    if any(isinstance(n, bool) or not isinstance(n, int | float) for n in nums) or not isinstance(rec["age_sessions"], int):
        return False
    return isinstance(rec["claim"], str) and 0 < len(rec["claim"]) <= 200


def load_set(state_dir: Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    """(events, flags): the private set when present and every record valid, else the built-in one."""
    path = state_dir / PRIVATE_SET if state_dir is not None else None
    if path is None or not path.exists():
        return [dict(e) for e in BUILTIN_EVENTS], []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return [dict(e) for e in BUILTIN_EVENTS], [FLAG_INVALID]
    if not isinstance(raw, list) or not raw or not all(_valid(r) for r in raw):
        return [dict(e) for e in BUILTIN_EVENTS], [FLAG_INVALID]
    return [dict(r) for r in raw], []


def _sessions_back(day: date, n: int) -> date | None:
    from council.swing.rules import is_session

    d, left = day, n
    for _ in range(MAX_BACK_DAYS * 2):
        d -= timedelta(days=1)
        if is_session(d):
            left -= 1
            if left == 0:
                return d
    return None


def build_event(rec: Mapping[str, Any], slot: datetime) -> PastEvent | None:
    """One record as the planted event at `slot` (None when it cannot be dated or is malformed)."""
    if not _valid(rec):
        return None
    line = try_normalise_id(rec["ticker"])
    age = int(rec["age_sessions"])
    filed_day = _sessions_back(slot.astimezone(UTC).date(), age)
    if line is None or filed_day is None:
        return None
    cid = public_news_id("sec", f"canary {line} 8-K {rec['filed']}")
    items = tuple(rec.get("items") or ITEM_202)
    titles = tuple(rec.get("titles") or TITLE_202)
    fields: dict[str, Any] = {
        "news_age_sessions": age,
        "move_since_news_close_pct": float(rec["move_pct"]),
        "move_since_news_close_sigma": float(rec["move_sigma"]),
    }
    for key, name in (("gap_pct", "gap_pct"), ("vol_ratio_since", "vol_ratio_since")):
        if isinstance(rec.get(key), int | float) and not isinstance(rec.get(key), bool):
            fields[name] = float(rec[key])
    if isinstance(rec.get("sector_etf"), str):
        fields["sector_etf"] = rec["sector_etf"]
    fields["trend"] = "up" if rec["side"] == "long" else "down"
    card = FactCard(line_id=line, side=str(rec["side"]), slot=slot.isoformat(), ok=True, fields=fields,
                    catalyst_items=[{"id": cid, "form": "8-K", "items": list(items), "titles": list(titles)}])
    meta = CatalystMeta(id=cid, available_at=datetime.combine(filed_day, AVAILABLE_UTC, tzinfo=UTC),
                        symbols=frozenset({line}), title=f"8-K: {titles[0]}", form="8-K", items=items,
                        # optional filing-text summary, so a canary reads like a real SEC catalyst
                        summary=str(rec["summary"])[:1200] if isinstance(rec.get("summary"), str) else "")
    return PastEvent(ticker=str(rec["ticker"]), side=str(rec["side"]), catalysts=(meta,),
                     claim=str(rec["claim"]), card=card)


def pick(events: Sequence[Mapping[str, Any]], slot: datetime) -> PastEvent | None:
    """This ISO week's qualifying event (rotation; a non-qualifying one is skipped)."""
    if not events:
        return None
    year, week, _ = slot.isocalendar()
    start = (year * 53 + week) % len(events)
    for k in range(len(events)):
        ev = build_event(events[(start + k) % len(events)], slot)
        if ev is not None and qualifies(ev) is None:
            return ev
    return None


def provider(state_dir: Path | None, flags: list[str] | None = None) -> Callable[[datetime], PastEvent | None]:
    """The `SwingSources.canary_event` callable (the set is read at each call: a private set the
    operator adds takes effect at the next canary). Load flags go to `flags` when given."""

    def canary_event(slot: datetime) -> PastEvent | None:
        events, load_flags = load_set(state_dir)
        if flags is not None:
            flags.extend(f for f in load_flags if f not in flags)
        return pick(events, slot)

    return canary_event
