"""Per-agent card scores. Descriptive only: no score changes any authority automatically.

Rules:
- A card is scored at 1, 5 and 20 trading days on the forward return of its scope.
- Hit: risk_up with a positive forward return, or risk_down with a negative one. Neutral cards
  are counted but never scored. A zero return is a miss.
- Normalised score = sign(direction) * r / (sigma_daily * sqrt(h)).
- Hit rates carry a Wilson 95% interval.
- Everything is labelled "not yet meaningful" below 60 directional cards or 26 weeks of history.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

HORIZONS: tuple[int, ...] = (1, 5, 20)
MIN_CARDS = 60
MIN_WEEKS = 26
NOT_YET = "not yet meaningful"

Direction = Literal["risk_up", "risk_down", "neutral"]


@dataclass(frozen=True)
class CardOutcome:
    card_id: str
    role: str
    card_type: str
    direction: Direction
    created_at: datetime
    forward: Mapping[int, float | None] = field(default_factory=dict)   # horizon -> return
    sigma_daily: float | None = None


def hit(direction: Direction, ret: float | None) -> bool | None:
    if ret is None or direction == "neutral":
        return None
    return ret > 0 if direction == "risk_up" else ret < 0


def normalised_score(direction: Direction, ret: float | None, sigma_daily: float | None, h: int) -> float | None:
    if ret is None or direction == "neutral" or not sigma_daily or sigma_daily <= 0:
        return None
    sign = 1.0 if direction == "risk_up" else -1.0
    return sign * ret / (sigma_daily * math.sqrt(h))


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class AgentScore:
    role: str
    cards: int
    directional: int
    span_weeks: float
    resolved: dict[int, int]
    hit_rate: dict[int, float | None]
    ci: dict[int, tuple[float, float] | None]
    mean_score: dict[int, float | None]
    meaningful: bool
    label: str


def meaningful(directional_cards: int, span_weeks: float) -> bool:
    return directional_cards >= MIN_CARDS and span_weeks >= MIN_WEEKS


def score_role(role: str, outcomes: list[CardOutcome], *, as_of: datetime | None = None) -> AgentScore:
    directional = [o for o in outcomes if o.direction != "neutral"]
    if outcomes:
        start = min(o.created_at for o in outcomes)
        end = as_of or max(o.created_at for o in outcomes)
        span = max(0.0, (end - start).total_seconds() / (7 * 86400))
    else:
        span = 0.0
    resolved, rates, cis, means = {}, {}, {}, {}
    for h in HORIZONS:
        hits = [hit(o.direction, o.forward.get(h)) for o in directional]
        scored = [x for x in hits if x is not None]
        n = len(scored)
        resolved[h] = n
        rates[h] = (sum(scored) / n) if n else None
        cis[h] = wilson(sum(scored), n)
        norms = [s for o in directional if (s := normalised_score(o.direction, o.forward.get(h), o.sigma_daily, h)) is not None]
        means[h] = (sum(norms) / len(norms)) if norms else None
    ok = meaningful(len(directional), span)
    return AgentScore(
        role=role, cards=len(outcomes), directional=len(directional), span_weeks=round(span, 2),
        resolved=resolved, hit_rate=rates, ci=cis, mean_score=means, meaningful=ok,
        label="" if ok else NOT_YET,
    )


def scoreboard(outcomes: Iterable[CardOutcome], *, as_of: datetime | None = None) -> list[AgentScore]:
    """One AgentScore per role, sorted by role name."""
    by_role: dict[str, list[CardOutcome]] = defaultdict(list)
    for o in outcomes:
        by_role[o.role].append(o)
    return [score_role(role, items, as_of=as_of) for role, items in sorted(by_role.items())]
