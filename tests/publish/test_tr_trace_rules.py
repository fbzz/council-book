"""The closed trace table (`council.publish.trace_rules`, transparency-v2 §4.2): what each engine
note and code may publish, and that the table covers every note the risk engine can write."""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from council.models.risk import Band
from council.publish import trace_rules as tr
from council.risk import engine
from tests.risk.helpers import default_ref, default_units, run, states_for

# ------------------------------------------------------------------------------ hold reasons
R11_NOTES = (
    "R11 below the minimum trade size",
    "R11 reference rule (level unchanged, drift below the threshold)",
    "R11 deadband (level step +0.25)",
    "R11 deadband (level step -0.50)",
    "R11 deadband (no unit weight)",
    "R11 broker minimum 0.037 of NAV",          # a future variant: still the bare code
    "R11",
)


@pytest.mark.parametrize("note", R11_NOTES)
def test_every_r11_variant_publishes_the_bare_code(note):
    assert tr.public_hold_reason(f"SMH: {note}") == "SMH: R11"
    assert tr.public_hold_reason(f"SMH: {note}", value_lines={"SMH"}) == "SMH: R11"


def test_r15_keeps_its_value_only_on_a_line_whose_inputs_are_public():
    note = "SPX: R15 SR_be 0.32 above 0.20"
    assert tr.public_hold_reason(note, value_lines={"SPX"}) == note
    assert tr.public_hold_reason(note, value_lines={"NDX"}) == "SPX: R15"
    assert tr.public_hold_reason(note) == "SPX: R15"
    assert tr.public_hold_reason("NDX: R15_fee net-of-cost gate (fixed fee)", value_lines={"NDX"}) == "NDX: R15_fee"


@pytest.mark.parametrize("note", sorted(tr.FIXED_LINE_TEXTS) + [
    "R10 level +0.75 outside band [+0.50, +1.00]", "R10 level -0.25 outside band [+0.00, +1.00]",
    "R10 deviation beyond the 2 allowed per cycle",
])
def test_listed_notes_publish_as_written(note):
    assert tr.listed(note)
    assert tr.public_hold_reason(f"GOLD: {note}") == f"GOLD: {note}"


def test_box_limits_keep_known_notes_and_reduce_unknown_ones_to_their_code():
    known = "GOLD: limited by R5 line cap, R16 event window"
    assert tr.public_hold_reason(known) == known
    assert tr.public_hold_reason("GOLD: limited by R5 line cap, R11 size floor 0.04 of NAV") == \
        "GOLD: limited by R5 line cap, R11"
    assert tr.public_hold_reason("GOLD: limited by a new box at 1234 USD") == "GOLD: limited by other limit"


def test_unknown_notes_fail_closed_to_a_bare_code_or_held():
    assert tr.public_hold_reason("GOLD: R16 blocked by an event at 14:30 on 5,000 units") == "GOLD: R16"
    assert tr.public_hold_reason("GOLD: something new at 1,234.56") == "GOLD: held"
    assert tr.public_hold_reason("no broker snapshot: current book taken as flat") == \
        "no broker snapshot: current book taken as flat"
    assert tr.public_hold_reason("equity 1234.56 USD on the main book") == "held"
    assert tr.public_hold_reason("") == "held"


def test_legacy_deadband_code_stays_readable():
    assert tr.public_hold_reason("OIL: deadband") == "OIL: deadband"


# ------------------------------------------------------------------------------ coverage of the engine
_NOTE_START = re.compile(r"^(?:R\d{1,2}[a-z]?(?:_fee)?|MC) \S")


def _sample(node: ast.JoinedStr, names: dict[str, object]) -> str | None:
    """An f-string rendered with representative values; None when it starts with a non-constant."""
    out = []
    for i, part in enumerate(node.values):
        if isinstance(part, ast.Constant):
            out.append(str(part.value))
            continue
        spec = ""
        if part.format_spec is not None:
            spec = "".join(str(p.value) for p in part.format_spec.values if isinstance(p, ast.Constant))
        value = names.get(part.value.id) if isinstance(part.value, ast.Name) else None
        if isinstance(value, str):
            out.append(value)
        elif spec:
            out.append(format(0.5, spec))
        elif i == 0:
            return None                       # "{s}: {why}" and similar: the parts are covered elsewhere
        else:
            out.append("2")
    return "".join(out)


def _engine_notes() -> list[str]:
    tree = ast.parse(inspect.getsource(engine))
    names = {k: v for k, v in vars(engine).items() if isinstance(v, str)}
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    docstrings |= {id(part) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for part in n.values}
    found = []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = _sample(node, names)
        else:
            continue
        if text and _NOTE_START.match(text):
            found.append(text)
    return sorted(set(found))


def test_the_table_lists_every_note_the_engine_writes():
    notes = _engine_notes()
    assert "R11 below the minimum trade size" in notes and "R15 SR_be 0.50 above 0.50" in notes
    assert len(notes) >= 25
    for note in notes:
        assert tr.listed(note), note
        assert tr.code_of(note) in tr.RULE_CODES, note
    for label in (*engine.BOX_LABELS.values(), engine.R18_CORE_HOLD, engine.R18_SATELLITE_HOLD):
        assert label in tr.BOX_NOTES, label
    for code in (engine.R14_FEE, engine.R15_FEE):
        assert code in tr.RULE_CODES and tr.public_code(code) == code


def test_every_box_key_of_the_engine_is_a_trace_code():
    assert set(engine.BOX_LABELS) <= tr.BOX_CODES


def _scenario_holds(policy, **kw) -> list[str]:
    return run(policy, **kw).hold_reasons


def test_real_engine_notes_are_all_listed(policy):
    units, ref = default_units(policy), default_ref(policy, "up")
    cur = {s: ref[s] * units[s] for s in units}
    wide = {s: Band(symbol=s, trend="up", ref_level=ref[s], lo=ref[s] - 0.5, hi=ref[s]) for s in units}
    holds = []
    # size floor (reference and discretionary), deadband, R10 clip, R15 on an expensive quote
    holds += _scenario_holds(policy, levels=dict(ref) | {"GOLD": 0.75, "ETH": 0.75, "SEMIS": 1.5}, bands=wide,
                             current=cur | {"SPX": 0.5 * units["SPX"]}, held_levels=dict(ref) | {"SPX": 0.5},
                             broker_min_share={s: 0.05 for s in units},
                             states=states_for(policy, "up", GOLD={"sigma_ann": 0.4}))
    holds += _scenario_holds(policy, levels=dict(ref) | {"NDX": 0.5}, bands=wide, current=cur,
                             cost_quotes={(s, d): q.model_copy(update={"per_side_bps": 400.0})
                                          for (s, d), q in run.__globals__["quotes_for"](policy).items()})
    holds += _scenario_holds(policy, levels=dict(ref) | {"NDX": 0.5}, current=cur, bands={})
    holds += _scenario_holds(policy, levels=dict(ref), current=None)
    notes = [h.split(": ", 1)[1] if re.match(r"^[A-Z0-9_]+: ", h) else h for h in holds]
    assert any(n.startswith("R11 below") for n in notes) and any(n.startswith("R11 deadband") for n in notes)
    assert any(n.startswith("R15 SR_be") for n in notes) and any(n.startswith("R10") for n in notes)
    for note in notes:
        assert tr.listed(note) or note in tr.FIXED_GENERAL_TEXTS, note


# ------------------------------------------------------------------------------ plan skips
@pytest.mark.parametrize("note, public", [
    ("GOLD: below_broker_minimum", "GOLD: R11"),
    ("GOLD: below_broker_minimum (187.43 USD)", "GOLD: R11"),
    ("SPX: below_real_minimum", "SPX: R11"),
    ("SPX: no_quote", "SPX: no_quote"),
    ("UNMAPPED_100123: no line", "UNMAPPED_100123: no line"),
    ("SPX: capped at 1234 units", "SPX: not_ordered"),
    ("garbage", "not_ordered"),
])
def test_plan_skips_collapse_size_reasons_to_r11(note, public):
    assert tr.public_plan_skip(note) == public


# ------------------------------------------------------------------------------ checks and trace steps
def test_r15_check_value_needs_public_inputs():
    assert tr.public_check_value("R15", 0.41, r15_public=False) is None
    assert tr.public_check_value("R15", 0.41, r15_public=True) == 0.41
    assert tr.public_check_value("R15", "R15_fee", r15_public=False) == "R15_fee"
    assert tr.public_check_value("R14", 12.5, r15_public=False) == 12.5


def test_trace_steps_publish_values_only_from_public_inputs():
    assert tr.public_step_values("R15", 0.12, 0.2, ("policy", "public_weights", "costs:floor", "open_history")) == \
        ("R15", 0.12, 0.2)
    assert tr.public_step_values("R15", 0.12, 0.2, ("policy", "broker_quote")) == ("R15", None, None)
    assert tr.public_step_values("R11 below the minimum trade size", 0.03, 0.04, ("policy",)) == ("R11", None, None)
    assert tr.public_step_values("cap", 0.3, 0.4, ("policy", "size_floor")) == ("R11", None, None)
    assert tr.public_step_values("cap", 0.3, 0.4, ("policy", "public_weights")) == ("cap", 0.3, 0.4)
    assert tr.public_step_values("R15_fee", 1.0, 0.2, ("policy",)) == ("R15_fee", None, None)
    assert tr.public_step_values("R10", 0.75, 1.0, ()) == ("R10", None, None)      # undeclared inputs: never
    assert tr.public_step_values("R99", 1.0, 2.0, ("policy",)) == ("R99", None, None)
    assert tr.public_step_values("R13", float("nan"), 0.6, ("policy",)) == ("R13", None, 0.6)
    assert not tr.value_allowed("MC", ("policy",))
    assert tr.value_allowed("R12", ("policy", "public_dates"))
