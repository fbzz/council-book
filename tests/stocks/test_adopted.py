"""`council.stocks.adopted`: the recorded override (SQ-8, no overlay, 50% / 45%) loads, matches the
tagged study, and fails closed on any difference in the record, the variants file or a live policy value.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from council import invariants
from council.paths import POLICY_DIR
from council.policy import policy_sha256
from council.stocks import adopted
from council.stocks.adopted import AdoptedRuleError, load_adopted

# The operator's private run folder (never in CI). Tests pin COUNCIL_STATE_DIR to a temp dir, so the
# default location is spelled out here.
PRIVATE_RUN = (Path.home() / "Library" / "Application Support" / "council-book" / "backtests"
               / "stock-sleeve" / "results" / adopted.RUN_ID)


def test_the_record_loads_as_the_adopted_rule():
    rule = load_adopted()
    assert (rule.decision, rule.gate_verdict) == ("user_override", "not_adopted")
    assert dict(rule.gate_checks) == {"G1_pool": "pass", "G2_random": "fail", "G3_regimes": "fail",
                                      "B1_book": "fail", "B2_index_sleeve": "fail"}
    assert (rule.cell, rule.variant, rule.names, rule.overlay) == ("SQ-8", "SQ", 8, "none")
    assert (rule.sleeve_share, rule.core_share) == (0.50, 0.45)
    assert rule.unit == pytest.approx(0.0625)
    assert rule.core_scale == pytest.approx(0.45 / 0.95)
    assert (rule.spec_tag, rule.run_id) == ("stock-sleeve-spec", "run-20260926T012203Z")
    # parameters as the tagged variants file states them
    assert (rule.score, rule.constraint, rule.sector_cap, rule.hold_buffer_multiple) == ("sector", "quota", 3, 2)
    assert dict(rule.overlay_levels) == {"up": 1.0, "mixed": 1.0, "down": 1.0}
    assert (rule.deadband_level, rule.deadband_min_nav_share) == (0.25, 0.02)
    assert rule.budget == "trim_held_above_target"
    assert rule.rebalance_anchors == ("03-20", "05-20", "08-20", "11-20")
    assert (rule.peer_group, rule.min_peer_group, rule.rank_score) == ("ff12", 5, "rank_average")
    assert rule.universe_filters["exclude_sectors"] == ("Money",)
    # L9: the AI list is ranked too, recorded as a named divergence; GC is never live
    assert rule.headline_indexes == ("sp500", "nasdaq100")
    assert rule.ai_list == "ranked_mechanically"
    assert sorted(rule.divergences, key=lambda k: int(k[1:])) == [f"L{i}" for i in range(1, 15)]
    assert rule.divergences["L9"] == "named_divergence" and rule.divergences["L8"] == "never_live"
    assert adopted.adopted_rule() == rule


def test_the_single_stock_cap_cannot_bind_on_the_rule_path():
    """L5: a unit plus the drift band stays below the 10% stock line cap."""
    rule = load_adopted()
    assert rule.unit + max(rule.deadband_level * rule.unit, rule.deadband_min_nav_share) < 0.10


def test_the_record_is_outside_the_live_policy_hash(tmp_path):
    """variants/ is research: the adoption record does not change `policy_sha` or any live cycle."""
    copy = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, copy, ignore=shutil.ignore_patterns("variants"))
    assert policy_sha256(copy) == policy_sha256(POLICY_DIR)
    assert invariants.STOCK_SLEEVE_LIVE is False


# ------------------------------------------------------------------------------ fail closed
@pytest.fixture
def policy_copy(tmp_path) -> Path:
    dest = tmp_path / "policy"
    (dest / "variants").mkdir(parents=True)
    for name in ("stock-sleeve-adopted.yaml", "stock-sleeve-variants-v1.yaml"):
        shutil.copyfile(POLICY_DIR / "variants" / name, dest / "variants" / name)
    return dest


def _edit_record(policy_dir: Path, change) -> None:
    path = policy_dir / adopted.ADOPTED_FILE
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_an_unchanged_copy_loads(policy_copy):
    assert load_adopted(policy_copy) == load_adopted()


def _set(*keys, value):
    def change(data):
        node = data
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return change


def _drop(*keys):
    def change(data):
        node = data
        for key in keys[:-1]:
            node = node[key]
        del node[keys[-1]]
    return change


@pytest.mark.parametrize("change", [
    _set("decision", value="adopt"),
    _set("gate", "verdict", value="adopt"),
    _set("gate", "checks", "G2_random", value="pass"),
    _drop("gate", "checks", "B2_index_sleeve"),
    _set("adopted", "cell", value="SQ-10"),
    _set("adopted", "names", value=10),
    _set("adopted", "names", value="8"),
    _set("adopted", "names", value=8.0),
    _set("adopted", "variant", value="SC"),
    _set("adopted", "overlay", value="down_only"),
    _set("adopted", "sleeve_share", value=0.45),
    _set("adopted", "core_share", value=0.5),
    _set("adopted", "universe", "ai_list", value="excluded"),
    _set("adopted", "universe", "headline", value=["sp500"]),
    _set("spec", "tag", value="stock-sleeve-v2"),
    _set("spec", "variants_sha256", value="0" * 64),
    _set("run", "id", value="run-20260101T000000Z"),
    _set("run", "result_sha256", value="f" * 64),
    _set("run", "gate_sha256", value="e" * 64),
    _set("run", "synthetic", value=True),
    _set("divergences", "L9", value="as_studied"),
    _drop("divergences", "L14"),
    _set("divergences", "L15", value="as_studied"),
    _set("extra", value=1),
    _drop("adopted"),
    _set("version", value=2),
], ids=lambda c: "change")
def test_any_change_to_the_record_fails_closed(policy_copy, change):
    _edit_record(policy_copy, change)
    with pytest.raises(AdoptedRuleError):
        load_adopted(policy_copy)


def test_a_changed_variants_file_fails_closed(policy_copy):
    path = policy_copy / adopted.VARIANTS_FILE
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AdoptedRuleError, match="not the tagged file"):
        load_adopted(policy_copy)


def test_a_missing_record_fails_closed(policy_copy):
    (policy_copy / adopted.ADOPTED_FILE).unlink()
    with pytest.raises(AdoptedRuleError, match="unreadable"):
        load_adopted(policy_copy)


def test_a_malformed_record_fails_closed(policy_copy):
    (policy_copy / adopted.ADOPTED_FILE).write_text("adopted: [unclosed\n")
    with pytest.raises(AdoptedRuleError, match="malformed"):
        load_adopted(policy_copy)


# ------------------------------------------------------------------ checks on live policy values
def test_reference_sleeve_check():
    rule = load_adopted()
    good = {"weight": 0.5, "names": 8, "variant": "SQ", "overlay": "none",
            "deadband": {"level": 0.25, "min_nav_share": 0.02}}
    assert adopted.reference_sleeve_errors(good, rule) == []
    assert adopted.reference_sleeve_errors(rule.reference_sleeve(), rule) == []
    assert adopted.reference_sleeve_errors(None, rule)
    for key, bad in (("weight", 0.45), ("names", 10), ("names", True), ("names", 8.5), ("variant", "SC"),
                     ("overlay", "down_only"), ("deadband", {"level": 0.25})):
        errors = adopted.reference_sleeve_errors({**good, key: bad}, rule)
        assert errors and all(e.startswith("reference.sleeve") for e in errors), (key, bad)
    assert adopted.reference_sleeve_errors({**good, "extra": 1}, rule) == ["reference.sleeve.extra: unexpected key"]
    missing = {k: v for k, v in good.items() if k != "overlay"}
    assert adopted.reference_sleeve_errors(missing, rule) == [
        "reference.sleeve.overlay: missing (the adopted rule says 'none')"]


def test_stock_rank_check():
    rule = load_adopted()
    good = {"shortlist_size": 12, "tiingo_symbol_cap": 40, "rule": rule.rank_rule()}
    assert adopted.stock_rank_errors(good, rule) == []
    assert adopted.stock_rank_errors({"shortlist_size": 12}, rule) == ["stock-rank.yaml has no rule: section"]
    for key, bad in (("names", 10), ("variant", "GC"), ("sector_cap", 4), ("hold_buffer_multiple", 1),
                     ("features", ["revenue_growth_yoy"]), ("anchors", ["03-20"]),
                     ("universe", {"headline": ["sp500", "nasdaq100"], "ai_list": "excluded"})):
        errors = adopted.stock_rank_errors({**good, "rule": {**rule.rank_rule(), key: bad}}, rule)
        assert errors, (key, bad)


# ------------------------------------------------------------ the operator's run (local only)
def test_the_private_run_matches_the_record():
    """Operator-only: the run folder's result and gate files hash to the recorded SHA-256 and say what
    the record says. No value from them is printed."""
    result_path, gate_path = PRIVATE_RUN / "result.json", PRIVATE_RUN / "gate.json"
    if not (result_path.is_file() and gate_path.is_file()):
        pytest.skip("the private study run folder is not on this machine")
    result_raw, gate_raw = result_path.read_bytes(), gate_path.read_bytes()
    assert hashlib.sha256(result_raw).hexdigest() == adopted.RESULT_SHA256
    assert hashlib.sha256(gate_raw).hexdigest() == adopted.GATE_SHA256
    gate, result = json.loads(gate_raw), json.loads(result_raw)
    recorded = {k: "pass" if v else "fail" for k, v in gate["gate"]["checks"].items()}
    assert recorded == dict(adopted.GATE_CHECKS) and gate["gate"]["adopt"] is False
    assert (gate["selected_cell"], gate["overlay"]) == (adopted.CELL, adopted.OVERLAY)
    assert result["synthetic"] is False
    assert result["tag_commit"] == adopted.SPEC_COMMIT
    assert result["spec_sha256"] == adopted.VARIANTS_SHA256
    assert (result["selected_cell"], result["overlay"]) == (adopted.CELL, adopted.OVERLAY)
