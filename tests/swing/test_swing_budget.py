"""S18: the swing budget the council decides, the refusal above it, and the core sized to the room the
swing book does not use (user decision 2026-10-01; `swing.budget`, `policy/swing.yaml` budget)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from council import cycle as C
from council.models.reference import ReferenceBook, ReferenceEntry
from council.swing import budget as SB
from council.swing import rules as R
from council.swing.models import SwingPMDecision
from council.swing.roles import accept_budget
from tests.risk.helpers import default_ref, default_units, loose, run
from tests.swing.test_book_engine import core_of, entry
from tests.swing.test_rules import book, cand, flat_cost, trade


@pytest.fixture
def sp(policy):
    return policy.swing


class FakeLedger:
    """Runtime keys and active swing trades only (what the S18 helpers read)."""

    def __init__(self, sizes: list[float] = (), runtime: dict[str, Any] | None = None) -> None:
        self.rows = [SimpleNamespace(detail={"size_nav": s}) for s in sizes]
        self.runtime = dict(runtime or {})

    def swing_trades(self, states=None):
        return list(self.rows)

    def get_runtime(self, key, default=None):
        return self.runtime.get(key, default)

    def set_runtime(self, key, value, now=None):
        self.runtime[key] = value


# ------------------------------------------------------------------------------ the budget
def test_budget_median_floor_and_clamp(sp):
    b = sp.budget
    d = SB.decide_budget([50, 30, 10], open_pct=0.0, last_pct=None, budget=b)
    assert (d.pct, d.median_pct, d.votes, d.fallback) == (30.0, 30, 3, False)
    assert SB.decide_budget([40, 25], open_pct=0, last_pct=None, budget=b).pct == 30.0   # 32.5 -> step 30
    # off-grid / out-of-range / non-integer values are no vote
    d = SB.decide_budget([55, 7, "40", True, 20], open_pct=0, last_pct=None, budget=b)
    assert (d.pct, d.votes) == (20.0, 1)
    # never below the open swing exposure (it never forces a close), never above 50 by a vote
    assert SB.decide_budget([10, 10, 0], open_pct=24.0, last_pct=None, budget=b).pct == 24.0
    assert SB.decide_budget([50, 50, 50], open_pct=8.0, last_pct=None, budget=b).pct == 50.0


def test_failed_pm_keeps_last_budget_or_default_and_flags(sp):
    b = sp.budget
    first = SB.decide_budget([None, None, None], open_pct=0, last_pct=None, budget=b)
    assert (first.pct, first.fallback, first.flags()) == (40.0, True, ["swing_budget_fallback"])
    kept = SB.decide_budget([], open_pct=0, last_pct=15.0, budget=b)
    assert (kept.pct, kept.fallback) == (15.0, True)
    assert SB.decide_budget([None], open_pct=32.0, last_pct=15.0, budget=b).pct == 32.0   # clamped too


def test_a_replicate_budget_needs_an_admissible_id(sp):
    dec = SwingPMDecision.model_validate({
        "actions": [], "decisive_fact": {"text": "x", "evidence_id": "BK:swing_open"},
        "swing_budget_pct": 30, "swing_budget_reason": "thin slot", "swing_budget_evidence_ids": ["BK:swing_open"]})
    assert accept_budget(dec, admissible={"BK:swing_open"}, budget=sp.budget) == 30
    assert accept_budget(dec, admissible={"F:SPX:trend"}, budget=sp.budget) is None
    assert accept_budget(dec.model_copy(update={"swing_budget_pct": 35}), admissible={"BK:swing_open"},
                         budget=sp.budget) == 35
    assert accept_budget(dec.model_copy(update={"swing_budget_pct": 33}), admissible={"BK:swing_open"},
                         budget=sp.budget) is None
    assert accept_budget(None, admissible=set(), budget=sp.budget) is None
    # a decision without the budget still validates (its actions count): the field is optional
    bare = SwingPMDecision.model_validate({"actions": [], "decisive_fact": {"text": "x", "evidence_id": "e"}})
    assert bare.swing_budget_pct is None


def test_apply_swing_budget_persists_and_flags(policy):
    led = FakeLedger(runtime={SB.BUDGET_KEY: {"pct": 25.0}})
    out = C.SwingRun()
    b = C.apply_swing_budget(led, policy, out, book(trades=[trade("t1")]), [None, None, None],
                             cycle_id="2026-10-01T1840Z", now=None)
    assert b.budget_nav == pytest.approx(0.25) and out.flags == ["swing_budget_fallback"]
    assert led.runtime[SB.BUDGET_KEY]["pct"] == 25.0 and out.budget.fallback
    b = C.apply_swing_budget(led, policy, C.SwingRun(), book(), [20, 20, 45], cycle_id="c", now=None)
    assert b.budget_nav == pytest.approx(0.20) and led.runtime[SB.BUDGET_KEY]["pct"] == 20.0


# ------------------------------------------------------------------------------ S18 refusal
def test_entries_refused_above_the_budget(sp):
    cs = [cand(ref=f"idea:{i}", ticker=f"T{i}", sector=f"S{i}", beta_60d=0.1) for i in range(3)]
    b = book(trades=[trade("open1", size=0.08, beta=0.1)], budget_nav=0.20)
    ok, dropped = R.final_pass(cs, b, sp, flat_cost())
    assert [v.ref for v in ok] == ["idea:0"]                         # 8% open + 8% = 16% <= 20%
    assert [d.code for d in dropped] == ["swing_budget_full", "swing_budget_full"]
    assert R.public_code("swing_budget_full") == "S18:swing_budget_full"
    ok, _ = R.final_pass(cs, replace(b, budget_nav=None), sp, flat_cost())
    assert len(ok) == 3                                              # no budget set: S18 inactive
    ok, dropped = R.final_pass(cs[:1], replace(b, budget_nav=0.0), sp, flat_cost())
    assert not ok and dropped[0].code == "swing_budget_full"         # budget 0: no new entry


# ------------------------------------------------------------------------------ the core size
def _ref(gross: float = 0.95) -> ReferenceBook:
    e = {s: ReferenceEntry(symbol=s, sleeve="core", asset_class="index", in_reference=True, trend="up",
                           level_ref=1.0, unit_weight=gross / 2, weight_ref=gross / 2, sigma_ann=0.2)
         for s in ("NDX", "GOLD")}
    return ReferenceBook(cycle_id="c", entries=e, k=1.0, target_vol=0.1, ex_ante_vol=0.12, gross=gross)


def test_core_factor_and_deadband():
    assert SB.core_factor(0.0, ref_gross=0.95, gross_max=0.95) == 1.0          # swing empty: full core
    assert SB.core_factor(0.24, ref_gross=0.60, gross_max=0.95) == pytest.approx(0.76)   # NAV x (1 - s)
    f = SB.core_factor(0.24, ref_gross=0.95, gross_max=0.95)                     # + R7 cash reserve
    assert 0.95 * f + 0.24 == pytest.approx(0.95)
    assert SB.core_factor(0.50, ref_gross=0.95, gross_max=0.95) * 0.95 + 0.50 <= 0.95 + 1e-9
    assert SB.sticky_exposure(0.10, 0.08, 0.04) == (0.08, False)                 # moved 2% < 4%: keep
    assert SB.sticky_exposure(0.16, 0.08, 0.04) == (0.16, True)                  # one entry: re-size
    assert SB.sticky_exposure(0.04, 0.08, 0.04) == (0.04, True)                  # one exit: re-size


def test_core_sized_up_when_swing_empty_and_down_when_it_fills(policy):
    ref0 = _ref(0.6)
    unit0 = {s: e.unit_weight for s, e in ref0.entries.items()}
    led = FakeLedger(runtime={SB.CORE_SHARE_KEY: {"swing_nav": 0.0}})
    sz = C.core_sizing_start(led, policy, ref0)
    assert sz.ref.gross == pytest.approx(0.6)                                    # empty swing: full core
    # two 8% swing entries this slot -> the core re-sizes to NAV x (1 - 0.16), traded now
    swing = C.SwingRun(lines=[entry("A1"), entry("A2")])
    rec = SimpleNamespace(cycle_id="c1", reference=sz.ref, extras={}, flags=[])
    unit, rescale = C.core_sizing_after_swing(SimpleNamespace(policy=policy, ledger=led), rec, sz, swing,
                                              ref0=ref0, unit0=unit0, unit=dict(unit0), now=None)
    assert rescale and unit["NDX"] == pytest.approx(unit0["NDX"] * 0.84)
    assert rec.reference.gross == pytest.approx(0.6 * 0.84)
    assert led.runtime[SB.CORE_SHARE_KEY]["swing_nav"] == pytest.approx(0.16)
    assert rec.extras["book_split"] == {"swing_budget_pct": 40.0, "swing_pct": 16.0, "core_pct": 84.0,
                                        "budget_fallback": False}
    # next cycle: the trades are open (16%); a 2% move would not re-size (deadband), the start size holds
    led.rows = [SimpleNamespace(detail={"size_nav": 0.08}), SimpleNamespace(detail={"size_nav": 0.10})]
    sz2 = C.core_sizing_start(led, policy, ref0)
    assert sz2.ref.gross == pytest.approx(0.6 * 0.84)
    unit2, rescale2 = C.core_sizing_after_swing(SimpleNamespace(policy=policy, ledger=led), rec, sz2, C.SwingRun(),
                                                ref0=ref0, unit0=unit0, unit={}, now=None)
    assert not rescale2 and unit2 == {}
    # both trades closed: the core is sized back up to the whole NAV
    led.rows = []
    sz3 = C.core_sizing_start(led, policy, ref0)
    unit3, rescale3 = C.core_sizing_after_swing(SimpleNamespace(policy=policy, ledger=led), rec, sz3, C.SwingRun(),
                                                ref0=ref0, unit0=unit0, unit={}, now=None)
    assert rescale3 and unit3 == pytest.approx(unit0) and rec.extras["book_split"]["core_pct"] == 100.0


def test_engine_trades_the_core_resize_only_when_flagged_and_gross_holds(policy):
    lp = loose(policy)
    units = default_units(lp)
    ref = default_ref(lp, "up")
    cur = {s: ref[s] * u for s, u in units.items() if ref.get(s) and u}
    scaled = {s: u * 0.92 for s, u in units.items()}                 # one 8% swing entry
    held = run(lp, current=cur, unit_weights=scaled)                  # drift below R11: no churn
    moved = run(lp, current=cur, unit_weights=scaled, core_rescale=True, extra_lines=[entry()])
    assert any(abs(core_of(held).get(s, 0.0) - w) < 1e-9 for s, w in cur.items())
    for s, w in cur.items():
        assert core_of(moved)[s] == pytest.approx(w * 0.92, abs=1e-6)
    assert moved.final_w["SW_ACME"] == pytest.approx(0.08) and not moved.swing_dropped
    assert moved.gross <= 2.0 + 1e-9
    # the hard cap: six 8% entries on a rescaled core never break gross <= 2x
    six = [entry(f"T{i}") for i in range(6)]
    d = run(lp, current=cur, unit_weights={s: u * 0.52 for s, u in units.items()}, core_rescale=True,
            extra_lines=six)
    assert d.gross <= 2.0 + 1e-9


# ------------------------------------------------------------------------------ public record
def test_public_cycle_and_site_show_the_split(policy):
    from council.llm.prompts import PromptRegistry
    from council.publish import redact
    from tests.fixtures import make_site_journal as msj
    from tests.publish.test_site_v4 import _load_site

    uni = msj.universe()
    spec = msj.Spec("2026-10-01T1840Z", "live", "calm", "reviewed_no_action", "none", 0.3, 2)
    rec, pk = msj.record(spec, uni, PromptRegistry())
    assert redact.public_cycle(rec, pk, lines=uni).split is None
    rec.extras["book_split"] = SB.split(30.0, 0.16, 0.84, fallback=True).public()
    doc = redact.public_cycle(rec, pk, lines=uni)
    assert doc.split is not None and (doc.split.swing_pct, doc.split.core_pct) == (16.0, 84.0)
    assert doc.split.swing_budget_pct == 30.0 and doc.split.budget_fallback
    raw = doc.model_dump_json()
    assert '"split"' in raw and "$" not in raw
    caption = _load_site().book_split(SimpleNamespace(doc=doc))
    assert caption["text"] == "Swing 16.0% / Core 84.0%" and caption["budget"] == "30.0%"
