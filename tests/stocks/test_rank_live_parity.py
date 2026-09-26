"""OPT-IN parity of the LIVE rank builder with the pre-registered study (design §4.1, WP-D acceptance).

As of the study's last rank date D, the live builder reads the S&P 500 and Nasdaq-100 member lists
from the MediaWiki REVISIONS as of D (`rvstart`), maps tickers to CIKs through SEC EDGAR and reads
SEC companyfacts (filings with available_at < D), then ranks with the adopted cell. The price facts
(first and last bar, dollar volume) come from the study's frozen bundle, so what is compared is the
live membership, identity and fundamentals path. It must reproduce the study's eligible set (at
least 98% overlap; every difference is listed with its cause) and the study's SQ-8 selection.

Never in CI (marker `live`); it runs only with COUNCIL_LIVE_CANARY=1, the operator's private study
bundle on this machine, and the SEC user agent in the Keychain (`council-book.sec-user-agent`, never
printed). Public data only; read-only:

    COUNCIL_LIVE_CANARY=1 uv run pytest -m live tests/stocks/test_rank_live_parity.py
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

from council import paths
from council.stocks.rank import RankConfig, rank
from council.stocks.universe import (
    INDEXES,
    PriceFacts,
    build_rank_inputs,
    fetch_membership,
    try_normalise_id,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("COUNCIL_LIVE_CANARY") != "1", reason="opt-in: set COUNCIL_LIVE_CANARY=1"),
]

MIN_OVERLAP = 0.98


@pytest.fixture(scope="module")
def study(monkeypatch_module):
    sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
    import stock_sleeve_study as S

    from tests.stocks.test_rank_parity import _bundle_or_skip

    spec = S.load_spec()
    inputs_dir, selections = _bundle_or_skip(spec)
    inp = S.Inputs.load(inputs_dir)
    sq = selections[selections["cell"] == "SQ-8"].reset_index(drop=True)
    return S, spec, inp, S.Universe(inp, spec), sq


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("COUNCIL_MODE", "dry_run")                   # lets the credential module read the Keychain
    yield mp
    mp.undo()


def _price_facts(inp, uni, symbols, when: pd.Timestamp, sessions: int) -> dict[str, PriceFacts]:
    """{line id: price facts} from the frozen bundle, through the study's own identity tables."""
    first = inp.first_bar()
    closes = inp.all_closes
    dv = inp.dollar_volume.loc[:when].tail(sessions)
    out: dict[str, PriceFacts] = {}
    for symbol in symbols:
        lid, key = try_normalise_id(symbol), uni._security(symbol, when)
        if lid is None or key is None or key not in closes.columns:
            continue
        s = closes[key].loc[:when].dropna()
        v = dv[key].median() if key in dv.columns else None
        out[lid] = PriceFacts(first_bar=first.get(key), last_bar=None if s.empty else s.index[-1],
                              dollar_volume=float(v) if v is not None and v == v else None)
    return out


def test_the_live_builder_reproduces_the_studys_eligible_set_and_selection(study):
    from council.data.credentials import MissingCredential, sec_user_agent
    from council.stocks.sec import SecClient

    try:
        sec_user_agent()
    except MissingCredential:
        pytest.skip("no SEC user agent configured")
    _S, spec, inp, uni, sq = study
    i = len(sq) - 1
    when, prev = pd.Timestamp(sq.loc[i, "date"]), pd.Timestamp(sq.loc[i - 1, "date"])
    config = RankConfig.from_variants(spec, "SQ-8")
    members = [fetch_membership(ix, asof=when.date()) for ix in INDEXES]
    assert all(m.as_of.date() <= when.date() for m in members)
    symbols = sorted({s for m in members for s in m.symbols})
    facts = _price_facts(inp, uni, symbols, when, config.dedupe_volume_sessions)
    held = [lid for s in sq.loc[i - 1, "names"].split() if (lid := try_normalise_id(s))]
    with SecClient() as sec:
        inputs = build_rank_inputs(when.date(), memberships=members, sec=sec, price_facts=facts, held=held,
                                   refresh=False)
    res = rank(when, inputs, config)

    elig, _funnel = uni.at(when)
    study_ids = {lid for s in elig["symbol"] if (lid := try_normalise_id(str(s)))}
    live_ids = set(res.eligible.index)
    union = study_ids | live_ids
    overlap = len(study_ids & live_ids) / max(1, len(union))
    live_members = {try_normalise_id(s) for s in symbols}
    study_members = {try_normalise_id(s) for s in uni.live_members(when)}
    unmapped = {try_normalise_id(s): why for s, why in inputs.unmapped}

    def cause(lid: str) -> str:
        if lid in live_ids:
            return "study excluded it" + ("" if lid in study_members else " (not a study member)")
        if lid not in live_members:
            return "not a MediaWiki member at D"
        if lid in unmapped:
            return f"live identity: {unmapped[lid]}"
        return f"live filter: {res.excluded.get(lid, 'unknown')}"

    differences = {lid: cause(lid) for lid in sorted(union - (study_ids & live_ids))}
    detail = "\n".join(f"  {k}: {v}" for k, v in differences.items())
    assert overlap >= MIN_OVERLAP, f"eligible overlap {overlap:.3f} < {MIN_OVERLAP}; differences:\n{detail}"
    assert all(v != "live filter: unknown" for v in differences.values()), detail
    recorded = [lid for s in sq.loc[i, "names"].split() if (lid := try_normalise_id(s))]
    assert list(res.selected) == recorded, f"selection differs (prev {prev.date()}); differences:\n{detail}"
