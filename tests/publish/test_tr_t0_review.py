"""T0 review fixes: the R15 check value needs every CHANGED line to be a value line."""

from __future__ import annotations

from council.models.facts import Fact
from council.publish.redact import public_cycle
from tests.publish.conftest import SLOT, make_pack
from tests.publish.test_tr_pre_token_safety import _with_holds, pack_with


def _changed(rec) -> list[str]:
    r = rec.risk
    return sorted(s for s in set(r.final_w) | set(r.base_w) if abs(r.final_w.get(s, 0.0) - r.base_w.get(s, 0.0)) > 1e-12)


def _pack(skip: str | None = None):
    base = make_pack()
    states = {s: st.model_copy(update={"history_source": "tiingo:X"}) for s, st in base.states.items()}
    costs = [Fact(id=f"C:{s}:per_side_bps", kind="cost", symbol=s, value=5.0, unit="bps", available_at=SLOT,
                  source="costs:floor") for s in base.states if s != skip]
    return pack_with(facts=[f for f in base.facts if not f.id.startswith("C:")] + costs, states=states)


def _r15(doc):
    return next(c for c in doc.risk.checks if c.rule_id == "R15").value


def test_r15_check_value_is_withheld_when_a_changed_line_has_no_public_cost(policy):
    rec = _with_holds()
    changed = [s for s in _changed(rec) if s in make_pack().states]   # lines the pack prices
    assert changed, "the fixture record must change at least one priced line"
    assert _r15(public_cycle(rec, _pack(), lines=policy.universe)) == 0.41
    assert _r15(public_cycle(rec, _pack(skip=changed[0]), lines=policy.universe)) is None
