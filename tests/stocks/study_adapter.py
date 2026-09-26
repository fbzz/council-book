"""The study's point-in-time inputs (scripts/stock_sleeve_study.py `Inputs`) as live `RankInputs`.

Identity is the study's own (`Universe.live_members`, `_security`, `_cik`, `cik_chain`), and so are
the price facts (first and last bar in the panel, the 63-session median dollar volume) and the
fundamentals, so a parity test compares the live rank's FILTERS, scores and selection with
`Universe.at` and `select_rule` on exactly the same inputs (spec header: "a test must pin its
eligible set to the tagged script's on the same inputs")."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd

from council.stocks.universe import AI, Candidate, RankInputs


def rank_inputs(inp: Any, uni: Any, when: pd.Timestamp, *, include_ai: bool = False,
                held: Sequence[str] = ()) -> RankInputs:
    first = inp.first_bar()
    closes = inp.all_closes
    dv = inp.dollar_volume.loc[:when].tail(int(uni.u["dedupe_volume_sessions"]))

    def last_bar(key: str) -> pd.Timestamp | None:
        if key not in closes.columns:
            return None
        s = closes[key].loc[:when].dropna()
        return None if s.empty else s.index[-1]

    def dollar_volume(key: str) -> float | None:
        if key not in dv.columns:
            return None
        v = dv[key].median()
        return float(v) if v == v else None

    def security(key: str) -> tuple[str, bool]:
        if str(key).startswith("AI:"):
            return "common", False               # the study treats AI-only price series as common stock
        if key not in inp.securities.index:
            return "missing", False
        s = inp.securities.loc[key]
        return str(s["security_type"]), bool(s["is_adr"])

    def candidate(key: str, symbol: str, cik: int | None, sources: frozenset[str], chain_key: str | None) -> Candidate:
        chain = uni.cik_chain(cik, when, chain_key) if cik is not None else ()
        kind, adr = security(key)
        return Candidate(key=key, symbol=symbol, cik=cik, ciks=chain, sources=sources, security_type=kind,
                         is_adr=adr, first_bar=first.get(key), last_bar=last_bar(key), dollar_volume=dollar_volume(key))

    cands: list[Candidate] = []
    unmapped: list[tuple[str, str]] = []
    members = uni.live_members(when)
    for s in members:
        key = uni._security(s, when)
        if key is None:
            unmapped.append((s, "no_identity"))
            continue
        cands.append(candidate(key, s, uni._cik(key, when), frozenset({"index"}), key))
    if include_ai and inp.ai is not None:
        known = set(members)
        for a in inp.ai.itertuples(index=False):
            if a.symbol in known or a.security_id is None:
                continue
            cik = int(a.cik) if a.cik is not None and a.cik == a.cik else None
            # A NaN security id passes the study's `is not None` test, counts as mapped and then fails
            # the common-stock step (it is in no security table): give it a unique key that does too.
            key = str(a.security_id) if a.security_id == a.security_id else f"NOSEC:{a.symbol}"
            cands.append(candidate(key, a.symbol, cik, frozenset({AI}), None if key.startswith("AI:") else key))
    ciks = {k for c in cands for k in c.chain}
    return RankInputs(candidates=tuple(cands), sic=inp.sic, taxonomy=inp.taxonomy,
                      fundamentals={k: inp.fund(k) for k in ciks}, held=tuple(held), unmapped=tuple(unmapped),
                      panel_start=inp.closes.index.min())
