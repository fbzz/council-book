"""The four-feature fundamentals score and the sleeve's name selection, FROZEN by the pre-registered
stock-sleeve study (docs/stock-sleeve-spec.md sections 4 and 5, tag `stock-sleeve-spec`).

Moved unchanged from scripts/stock_sleeve_study.py. The study and the live quarterly rank import
these functions; nothing re-implements them, and a change here needs a new pre-registration.

- Global score: the ported `rank_average` over the whole eligible set at the decision date.
- Sector score: the same rule inside each peer group; a peer group is an FF12 sector, and sectors
  with fewer than `min_peer` eligible names are ranked together in one pooled group.
- Order: best first by the variant's score, then the global score, then the symbol.
- Hold buffer: a held, still-eligible name stays while it ranks inside the top buffer x N (quota
  variant: buffer x its sector's quota); kept names count toward the cap or quota.
- Cap variants: walk the order, at most `cap` names per sector; fill without the cap only if fewer
  than N names fit. Quota variant: largest-remainder sector quotas, each at most min(cap, count).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from council.stocks import pit

FEATURES = tuple(pit.FUNDAMENTAL_COLUMNS)
POOLED = "Pooled"


def peer_groups(sectors: pd.Series, min_peer: int) -> pd.Series:
    counts = sectors.value_counts()
    small = set(counts[counts < min_peer].index)
    return sectors.map(lambda s: POOLED if s in small else s)


def add_scores(elig: pd.DataFrame, min_peer: int) -> pd.DataFrame:
    """g_score = the rule over the whole eligible cross-section; s_score = the rule inside each
    peer group (FF12 sector, small sectors pooled). Both via the ported rank_average."""
    out = elig.copy()
    if out.empty:
        out["g_score"] = pd.Series(dtype=float)
        out["s_score"] = pd.Series(dtype=float)
        out["peer"] = pd.Series(dtype=object)
        return out
    out["g_score"] = pit.rank_average(out, FEATURES)
    out["peer"] = peer_groups(out["sector"], min_peer)
    s = pd.Series(np.nan, index=out.index)
    for _, g in out.groupby("peer"):
        s.loc[g.index] = pit.rank_average(g, FEATURES)
    out["s_score"] = s
    return out


def ordered(elig: pd.DataFrame, score: str) -> list[str]:
    """Best first: the variant's score, then the global score, then the symbol."""
    col = "s_score" if score == "sector" else "g_score"
    frame = elig.assign(_k=-elig[col], _g=-elig["g_score"])
    return list(frame.sort_values(["_k", "_g", "symbol"]).index)


def sector_quotas(counts: Mapping[str, int], n: int, cap: int) -> dict[str, int]:
    """Largest-remainder quotas proportional to eligible counts, each at most min(cap, count)."""
    total = sum(counts.values())
    if total == 0:
        return {}
    raw = {s: n * c / total for s, c in counts.items()}
    limit = {s: min(cap, c) for s, c in counts.items()}
    q = {s: min(math.floor(raw[s]), limit[s]) for s in counts}
    remaining = n - sum(q.values())
    while remaining > 0:
        open_ = [s for s in counts if q[s] < limit[s]]
        if not open_:
            break
        s = sorted(open_, key=lambda k: (-(raw[k] - q[k]), -counts[k], k))[0]
        q[s] += 1
        remaining -= 1
    return q


def select_rule(elig: pd.DataFrame, held: Iterable[str], variant: Mapping[str, Any], n: int,
                buffer_mult: float) -> list[str]:
    """The pre-registered selection (spec section 'From ranks to names')."""
    if elig.empty:
        return []
    held = [h for h in held if h in elig.index]
    cap = int(variant["cap"])
    order = ordered(elig, variant["score"])
    sector = elig["sector"].to_dict()
    if variant["constraint"] == "quota":
        q = sector_quotas(elig["sector"].value_counts().to_dict(), n, cap)
        chosen: list[str] = []
        for s in sorted(q):
            in_s = [k for k in order if sector[k] == s]
            pos = {k: i for i, k in enumerate(in_s)}
            keep = sorted([h for h in held if sector[h] == s and pos[h] < buffer_mult * q[s]], key=pos.get)[: q[s]]
            fill = [k for k in in_s if k not in keep][: q[s] - len(keep)]
            chosen += keep + fill
        rank = {k: i for i, k in enumerate(order)}
        return sorted(chosen, key=rank.get)
    pos = {k: i for i, k in enumerate(order)}
    chosen = [h for h in held if pos[h] < buffer_mult * n]
    counts: dict[str, int] = {}
    for k in chosen:
        counts[sector[k]] = counts.get(sector[k], 0) + 1
    for k in order:
        if len(chosen) >= n:
            break
        if k in chosen or counts.get(sector[k], 0) >= cap:
            continue
        chosen.append(k)
        counts[sector[k]] = counts.get(sector[k], 0) + 1
    for k in order:                                   # only when the cap left slots empty
        if len(chosen) >= n:
            break
        if k not in chosen:
            chosen.append(k)
    return sorted(chosen, key=pos.get)
