"""WP-G: the stock lines' fundamentals facts (design §11.2). The four features are the live rank's
own computation on filings dated before the slot's UTC date; each fact is stamped with the SEC
acceptance time of the filing it comes from and the pack drops it before then (lookahead). Real SEC
companyfacts (public domain, tests/fixtures/sec) for MSFT, placed on a synthetic sleeve line."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime

import pandas as pd
import pytest
import yaml

from council.data.http import DataError
from council.facts.pack import admissible_fundamentals, build_fact_pack
from council.models.facts import Fact
from council.paths import REPO_ROOT
from council.policy import Policy
from council.stocks import fundamentals as FU
from council.stocks import pit
from council.stocks.rank import features as rank_features
from council.stocks.universe import Candidate, RankInputs
from tests.conftest import SLEEVE_FIXTURE, make_sleeve_policy_dir

MSFT = json.loads((REPO_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_trimmed.json").read_text())["companies"]["MSFT"]
CIK = 789019
Q3_ACCESSION = "0001564590-18-024893"          # 10-Q for 2018-09-30, filed 2018-10-24
Q2_ACCESSION = "0001564590-18-019062"          # 10-K for 2018-06-30, filed 2018-08-03


@pytest.fixture(scope="module")
def msft_policy(tmp_path_factory) -> Policy:
    """The sleeve fixture with TSTA carrying MSFT's CIK."""
    overlay = tmp_path_factory.mktemp("overlay_msft")
    for name in ("universe.yaml", "stock-rank.yaml"):
        shutil.copyfile(SLEEVE_FIXTURE / name, overlay / name)
    sleeve = yaml.safe_load((SLEEVE_FIXTURE / "stock-sleeve.yaml").read_text())
    sleeve["lines"][0]["cik"] = f"{CIK:010d}"
    (overlay / "stock-sleeve.yaml").write_text(yaml.safe_dump(sleeve, sort_keys=False))
    return Policy.load(make_sleeve_policy_dir(tmp_path_factory.mktemp("pol_msft"), overlay=overlay))


@pytest.fixture(scope="module")
def rows() -> pd.DataFrame:
    frame, stats = pit.fundamentals_comparable("TSTA", str(CIK), MSFT["companyfacts"])
    assert stats["taxonomy"] == "us-gaap"
    return frame


def submissions(accepted: dict[str, str]) -> dict:
    return {"filings": {"recent": {"accessionNumber": list(accepted), "acceptanceDateTime": list(accepted.values()),
                                   "form": ["10-Q"] * len(accepted), "filingDate": ["2018-10-24"] * len(accepted),
                                   "reportDate": [""] * len(accepted), "items": [""] * len(accepted)}}}


def slot(y, m, d, h) -> datetime:
    return datetime(y, m, d, h, 40, tzinfo=UTC)


def tsta(policy):
    return policy.universe.by_symbol()["TSTA"]


# ------------------------------------------------------------------------------------ features


def test_features_are_the_rank_computation_on_filings_dated_before_the_slot_date(msft_policy, rows):
    same_day = FU.line_features(tsta(msft_policy), rows, {}, slot=slot(2018, 10, 24, 22))
    assert same_day.latest_available_at == pd.Timestamp("2018-08-03").date()     # the Q3 filing is dated D
    next_day = FU.line_features(tsta(msft_policy), rows, {}, slot=slot(2018, 10, 25, 2))
    assert next_day.latest_available_at == pd.Timestamp("2018-10-24").date()
    c = Candidate(key="X", symbol="X", cik=CIK)
    expected = rank_features(c, RankInputs(candidates=(c,), fundamentals={CIK: rows}), pd.Timestamp("2018-10-25"))
    for column in pit.FUNDAMENTAL_COLUMNS:
        assert next_day.features[column] == pytest.approx(expected[column])
    assert FU.LineFeatures.from_json(json.loads(json.dumps(next_day.to_json()))) == next_day


def test_acceptance_times_are_new_york_wall_time():
    got = FU.acceptance_times(submissions({Q3_ACCESSION: "2018-10-24T16:07:12.000Z", "bad": "x"}))
    assert got == {Q3_ACCESSION: datetime(2018, 10, 24, 20, 7, 12, tzinfo=UTC)}


def test_fact_ids_units_values_and_available_at(msft_policy, rows):
    accepted = FU.acceptance_times(submissions({Q3_ACCESSION: "2018-10-24T16:07:12.000Z"}))
    at = slot(2018, 10, 25, 2)
    feats = FU.line_features(tsta(msft_policy), rows, accepted, slot=at)
    assert feats.accepted_at == datetime(2018, 10, 24, 20, 7, 12, tzinfo=UTC)
    facts = {f.id: f for f in FU.fundamental_facts(msft_policy, {"TSTA": feats}, slot=at) if f.symbol == "TSTA"}
    assert set(facts) == {"F:TSTA:rev_yoy", "F:TSTA:rev_accel", "F:TSTA:gm_chg", "F:TSTA:om_chg",
                          "F:TSTA:filing_age_d", "F:TSTA:sector"}
    rev = facts["F:TSTA:rev_yoy"]
    assert rev.kind == "fundamental" and rev.unit == "pct" and rev.source == "sec" and rev.symbol == "TSTA"
    assert rev.value == round(feats.features["revenue_growth_yoy"] * 100, 2) == 18.53
    assert rev.available_at == feats.accepted_at
    assert facts["F:TSTA:filing_age_d"].value == 1.0 and facts["F:TSTA:filing_age_d"].unit == "days"
    sector = facts["F:TSTA:sector"]
    assert sector.value == "BusEq" and sector.available_at == datetime(2026, 11, 20, tzinfo=UTC)   # rank date


def test_fundamentals_lookahead_the_pack_drops_facts_before_their_filing_is_public(msft_policy, rows):
    late = FU.acceptance_times(submissions({Q3_ACCESSION: "2018-10-24T23:30:00.000Z"}))   # 03:30Z on the 25th
    early = slot(2018, 10, 25, 2)
    feats = FU.line_features(tsta(msft_policy), rows, late, slot=early)
    facts = FU.fundamental_facts(msft_policy, {"TSTA": feats}, slot=early)
    assert admissible_fundamentals(facts, early, msft_policy) == []
    later = slot(2018, 10, 25, 6)
    kept = {f.id for f in admissible_fundamentals(facts, later, msft_policy)}
    assert "F:TSTA:rev_yoy" in kept and "F:TSTA:sector" not in kept             # the rank is years later
    # no acceptance time: the end of the filing date in New York (04:00Z on the 25th)
    unknown = FU.line_features(tsta(msft_policy), rows, {}, slot=early)
    assert FU.filing_available_at(unknown) == datetime(2018, 10, 25, 4, 0, tzinfo=UTC)
    assert admissible_fundamentals(FU.fundamental_facts(msft_policy, {"TSTA": unknown}, slot=early), early,
                                   msft_policy) == []


def test_the_pack_admits_fundamentals_only_as_fundamental_facts_of_stock_lines(msft_policy):
    at = slot(2027, 1, 5, 14)
    good = Fact(id="F:TSTA:rev_yoy", kind="fundamental", symbol="TSTA", value=12.5, unit="pct",
                available_at=datetime(2027, 1, 2, tzinfo=UTC), source="sec")
    pack = build_fact_pack(cycle_id="c", slot=at, now=at, policy=msft_policy, states={}, fundamental_facts=[good])
    assert any(f.id == "F:TSTA:rev_yoy" for f in pack.facts)
    bad = [good.model_copy(update={"kind": "market"}),
           good.model_copy(update={"id": "F:NDX:rev_yoy", "symbol": "NDX"}),
           good.model_copy(update={"id": "F:TSTA:trend"}),
           good.model_copy(update={"id": "F:TSTB:rev_yoy"})]
    for fact in bad:
        with pytest.raises(ValueError):
            admissible_fundamentals([fact], at, msft_policy)


# ------------------------------------------------------------------------------------ gather


class FakeSec:
    def __init__(self, docs: dict, subs: dict) -> None:
        self.docs, self.subs, self.calls, self.closed = docs, subs, [], False

    def companyfacts(self, cik):
        self.calls.append(("companyfacts", cik))
        if cik == 900002:
            raise DataError("boom")
        return self.docs.get(cik)

    def submissions(self, cik):
        self.calls.append(("submissions", cik))
        return self.subs.get(cik, {"filings": {"recent": {}}})

    def close(self):
        self.closed = True


def test_gather_computes_once_per_day_and_flags_problems(msft_policy):
    sec = FakeSec({CIK: MSFT["companyfacts"]},
                  {CIK: submissions({Q3_ACCESSION: "2018-10-24T16:07:12.000Z"})})
    at = slot(2018, 10, 25, 6)
    facts, flags = FU.gather_fundamentals(msft_policy, now=at, sec_factory=lambda: sec)
    ids = {f.id for f in facts}
    assert "F:TSTA:rev_yoy" in ids and "F:TSTB:rev_yoy" not in ids
    assert "fundamentals_failed:TSTB" in flags and "fundamentals_missing:TSTC_B" in flags
    assert sec.closed
    calls = len(sec.calls)
    again, _ = FU.gather_fundamentals(msft_policy, now=slot(2018, 10, 25, 14), sec_factory=lambda: sec)
    assert {f.id: f.value for f in again if f.symbol == "TSTA"} == {f.id: f.value for f in facts if f.symbol == "TSTA"}
    assert len([c for c in sec.calls[calls:] if c[1] == CIK]) == 0          # TSTA cached for the day
    FU.gather_fundamentals(msft_policy, now=slot(2018, 10, 26, 2), sec_factory=lambda: sec)
    assert ("companyfacts", CIK) in sec.calls[calls:]                       # a new UTC day recomputes


def test_gather_without_the_sec_user_agent_gives_a_flag_and_no_features(msft_policy, monkeypatch):
    monkeypatch.delenv("COUNCIL_SEC_USER_AGENT", raising=False)
    facts, flags = FU.gather_fundamentals(msft_policy, now=slot(2027, 1, 5, 14))
    assert flags == ["fundamentals_unavailable:no_sec_user_agent"]
    assert {f.id.rsplit(":", 1)[-1] for f in facts} == {"sector"}             # the rank's sector only
    assert FU.gather_fundamentals(Policy.load(include_sleeve=False), now=slot(2027, 1, 5, 14)) == ([], [])


def test_rank_scores_are_read_for_the_policy_quarter_only(msft_policy, tmp_path):
    path = tmp_path / FU.RANK_SCORES_FILE
    path.parent.mkdir(parents=True)
    doc = {"quarter": "2026Q4", "computed_at": "2026-11-20T16:00:00+00:00",
           "lines": {"TSTA": {"sector_pct": 87.456, "composite": 71.2}}}
    path.write_text(json.dumps(doc))
    scores = FU.load_rank_scores(tmp_path, "2026Q4")
    assert scores == ({"TSTA": {"sector_pct": 87.456, "composite": 71.2}}, datetime(2026, 11, 20, 16, tzinfo=UTC))
    assert FU.load_rank_scores(tmp_path, "2027Q1") is None
    facts = {f.id: f for f in FU.fundamental_facts(msft_policy, {}, slot=slot(2026, 12, 1, 14), rank_scores=scores)}
    assert facts["F:TSTA:sector_pct"].value == 87.5 and facts["F:TSTA:composite"].available_at == scores[1]
    path.write_text("{broken")
    assert FU.load_rank_scores(tmp_path, "2026Q4") is None


def test_fundamental_evidence_ids():
    from council.facts.evidence_ids import FUNDAMENTAL_FIELDS, MARKET_FIELDS, fundamental_id

    assert fundamental_id("BRK_B", "rev_yoy") == "F:BRK_B:rev_yoy" and fundamental_id("F", "sector") == "F:F:sector"
    assert not set(FUNDAMENTAL_FIELDS) & set(MARKET_FIELDS)
    for bad in (("BRK_B", "trend"), ("BRK.B:x", "rev_yoy"), ("", "rev_yoy")):
        with pytest.raises(ValueError):
            fundamental_id(*bad)
