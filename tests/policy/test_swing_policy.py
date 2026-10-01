"""SW-0: `policy/swing.yaml` loads, validates and is hashed; the code ceilings in
`council.invariants` refuse a looser file; `SWING_BOOK_LIVE` and `STOCK_SLEEVE_LIVE` stay False.
Synthetic edits of a copied policy directory only; no network, no broker, no model."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from council import invariants
from council.invariants import InvariantViolation, check_policy, check_swing_policy
from council.paths import POLICY_DIR
from council.policy import SLEEVE_FILE, Policy, policy_sha256
from council.swing.policy import SWING_FILE, SwingPolicy


def _raw() -> dict:
    return yaml.safe_load((POLICY_DIR / SWING_FILE).read_text())


def _with(path: tuple[str, ...], value) -> dict:
    data = _raw()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


def _copy_policy(tmp_path: Path) -> Path:
    dest = tmp_path / "policy"
    dest.mkdir()
    for src in POLICY_DIR.glob("*.yaml"):
        shutil.copyfile(src, dest / src.name)
    return dest


# ------------------------------------------------------------------ the repository file
def test_repository_swing_policy_loads_with_the_users_decisions():
    policy = Policy.load(include_sleeve=False)
    swing = policy.swing
    assert swing is not None
    check_policy(policy)                                     # includes check_swing_policy
    assert swing.capacity.max_open == 6
    assert swing.capacity.max_short == 2
    assert swing.capacity.max_new_7d == 6                    # the user's 6/week (2026-09-27)
    assert swing.size.target_nav == 0.08
    assert swing.size.max_loss_nav_at_stop == 0.008
    assert swing.size.short_max_loss_nav_at_stop == 0.005
    assert swing.stops.max_short_pct == 0.08
    assert swing.targets.min_net_rr == 1.2
    assert swing.fees.mode == "report"                       # a reported metric, not a brake
    assert swing.entry_guard.valid_minutes == 60
    assert swing.slots.winter_utc == ["18:40"]
    assert swing.slots.summer_utc == ["14:40", "18:40"]
    assert swing.earnings.exit_mode == "proposal"
    assert swing.budget.idle == "cash"
    assert swing.llm.skeptic_model == "deepseek-v4.1-flash:cloud"
    assert swing.llm.skeptic_model_family == "same"
    assert swing.llm.skeptic_model == policy.council["model"]  # user 2026-09-29: the Scout's model
    assert swing.llm.max_calls_per_slot == 12                # user 2026-10-01: 5 Skeptic reviews
    assert swing.llm.max_skeptic_calls == 5 and swing.llm.deadline_s == 450
    assert swing.public_record.declared_cost_pct_per_leg == 1.25
    assert swing.tracking.paper_track_every_idea is True
    assert swing.tracking.paper_run_before_live is False
    assert swing.setups_live == ["news_continuation", "post_earnings_drift", "second_order", "day2_confirmation"]


def test_swing_file_is_part_of_the_policy_hash(tmp_path):
    work = _copy_policy(tmp_path)
    before = policy_sha256(work)
    assert before == policy_sha256(POLICY_DIR)
    data = _with(("capacity", "max_new_7d"), 5)
    (work / SWING_FILE).write_text(yaml.safe_dump(data))
    assert policy_sha256(work) != before
    assert Policy.load(work, include_sleeve=False).swing.capacity.max_new_7d == 5


def test_policy_without_swing_file_still_loads(tmp_path):
    """A HEAD snapshot from before SW-0 has no swing.yaml: the policy loads with `swing=None`."""
    work = _copy_policy(tmp_path)
    (work / SWING_FILE).unlink()
    policy = Policy.load(work, include_sleeve=False)
    assert policy.swing is None
    check_policy(policy)


def test_runtime_default_policy_never_merges_a_sleeve_and_flags_stay_off():
    assert invariants.STOCK_SLEEVE_LIVE is False
    assert invariants.SWING_BOOK_LIVE is False
    assert invariants.STOCK_LONGS_REAL_1X is True
    assert invariants.STOCK_SHORTS_CFD_1X_WITH_STOP is True
    assert invariants.STOCKS_REAL_LONG_1X is invariants.STOCK_LONGS_REAL_1X
    assert not (POLICY_DIR / SLEEVE_FILE).exists() or not Policy.load(include_sleeve=False).universe.stock_lines()


def test_code_ceilings_match_the_design():
    assert invariants.SWING_MAX_OPEN == 6
    assert invariants.SWING_MAX_SHORT == 2
    assert invariants.SWING_MAX_SIZE_NAV == 0.08
    assert invariants.SWING_MAX_NEW_7D == 6
    assert invariants.SWING_MAX_SHORT_LOSS_NAV == 0.005
    assert invariants.SWING_MAX_LONG_LOSS_NAV == 0.008
    assert invariants.SWING_MAX_SHORT_STOP_PCT == 0.08


# ------------------------------------------------------------------ looser than the code: refused
@pytest.mark.parametrize(("path", "value"), [
    (("capacity", "max_open"), 7),
    (("capacity", "max_short"), 3),
    (("capacity", "max_new_7d"), 7),
    (("capacity", "max_open_risk_nav"), 0.05),
    (("capacity", "open_risk_gap_mult"), 1.2),
    (("size", "target_nav"), 0.09),
    (("size", "max_loss_nav_at_stop"), 0.01),
    (("size", "short_max_loss_nav_at_stop"), 0.006),
    (("stops", "max_long_pct"), 0.15),
    (("stops", "max_short_pct"), 0.10),
    (("targets", "min_net_rr"), 1.0),
    (("entry_guard", "valid_minutes"), 90),
    (("llm", "max_calls_per_slot"), 13),
    (("public_record", "declared_cost_pct_per_leg"), 1.0),
    (("brake", "pnl_nav"), -0.5),
    (("brake", "window_days"), 5),
    (("drawdown_scale", "from_peak"), -0.5),
])
def test_swing_policy_looser_than_an_invariant_fails(path, value):
    swing = SwingPolicy.model_validate(_with(path, value))
    with pytest.raises(InvariantViolation, match=path[-1]):
        check_swing_policy(swing)


def test_looser_swing_file_fails_check_policy(tmp_path):
    work = _copy_policy(tmp_path)
    (work / SWING_FILE).write_text(yaml.safe_dump(_with(("capacity", "max_new_7d"), 8)))
    with pytest.raises(InvariantViolation, match="max_new_7d"):
        check_policy(Policy.load(work, include_sleeve=False))


def test_stricter_swing_policy_passes():
    data = _with(("capacity", "max_new_7d"), 3)
    data["capacity"]["max_open"] = 4
    data["size"]["target_nav"] = 0.06
    data["targets"]["min_net_rr"] = 1.5
    check_swing_policy(SwingPolicy.model_validate(data))


def test_drawdown_scaling_cannot_be_relaxed_to_full_size():
    data = _with(("drawdown_scale", "size_nav"), 0.06)
    data["drawdown_scale"]["max_open"] = 5
    with pytest.raises(InvariantViolation) as err:
        check_swing_policy(SwingPolicy.model_validate(data))
    assert "drawdown_scale.size_nav" in str(err.value) and "drawdown_scale.max_open" in str(err.value)


def test_skeptic_family_must_match_the_model_choice(tmp_path):
    work = _copy_policy(tmp_path)
    scout = yaml.safe_load((work / "council.yaml").read_text())["model"]

    def write(model, family):
        data = _with(("llm", "skeptic_model"), model)
        data["llm"]["skeptic_model_family"] = family
        (work / SWING_FILE).write_text(yaml.safe_dump(data))

    write(scout, "other")
    with pytest.raises(InvariantViolation, match="skeptic_model_family 'other'"):
        check_policy(Policy.load(work, include_sleeve=False))
    write("glm-5.3-flash:cloud", "same")
    with pytest.raises(InvariantViolation, match="skeptic_model_family 'same'"):
        check_policy(Policy.load(work, include_sleeve=False))
    write("glm-5.3-flash:cloud", "other")
    check_policy(Policy.load(work, include_sleeve=False))
    write(scout, "same")
    check_policy(Policy.load(work, include_sleeve=False))   # the declared same-model fallback


def test_shorts_disabled_in_code_refuse_a_short_capacity(monkeypatch):
    monkeypatch.setattr(invariants, "STOCK_SHORTS_CFD_1X_WITH_STOP", False)
    with pytest.raises(InvariantViolation, match="shorts are disabled"):
        check_swing_policy(SwingPolicy.model_validate(_raw()))
    check_swing_policy(SwingPolicy.model_validate(_with(("capacity", "max_short"), 0)))


# ------------------------------------------------------------------ schema validation
@pytest.mark.parametrize(("path", "value"), [
    (("capacity", "max_new_7d"), "6"),               # strings are not numbers
    (("capacity", "max_open"), True),                # nor are booleans
    (("size", "target_nav"), "0.08"),
    (("size", "target_nav"), 0),
    (("size", "min_nav"), 0.1),                      # above target
    (("capacity", "max_short"), 9),                  # above max_open
    (("stops", "min_pct"), 0.2),                     # above the stop maxima
    (("time_stop", "max_total_sessions"), 18),       # 15 + 5 extension does not fit
    (("chase", "prior_wait_sigma"), 4.5),            # above the hard chase drop (4 sigma)
    (("liquidity", "short_min_adv_usd"), 1_000_000),
    (("earnings", "exit_mode"), "automated"),        # Q-S5: no automated pre-earnings write
    (("fees", "mode"), "off"),
    (("brake", "pnl_nav"), 0.05),
    (("brake", "net_of_all_costs"), False),
    (("slots", "winter_utc"), ["18:40", "14:40"]),   # unsorted
    (("slots", "winter_utc"), ["25:40"]),
    (("llm", "pm_entry_votes"), 1),                  # not a majority of 3
    (("llm", "max_skeptic_calls"), 7),               # 3 + 7 + 3 > 12
    (("llm", "skeptic_model"), "GLM 5.3"),
    (("budget", "idle"), "bonds"),
    (("public_record", "percent_only"), False),
    (("tracking", "paper_track_every_idea"), False),
    (("setups_live",), ["news_continuation", "gap_fade"]),   # gap_fade is paper-only
    (("version",), 2),
    (("drawdown_scale", "max_open"), 7),
])
def test_invalid_swing_policy_is_refused(path, value):
    with pytest.raises(ValidationError):
        SwingPolicy.model_validate(_with(path, value))


def test_unknown_keys_are_refused():
    data = _raw()
    data["capacity"]["max_new_30d"] = 20
    with pytest.raises(ValidationError):
        SwingPolicy.model_validate(data)
    data = _raw()
    data["leverage"] = 2
    with pytest.raises(ValidationError):
        SwingPolicy.model_validate(data)


def test_invalid_swing_file_fails_policy_load(tmp_path):
    work = _copy_policy(tmp_path)
    (work / SWING_FILE).write_text(yaml.safe_dump(_with(("size", "target_nav"), "eight")))
    with pytest.raises(ValidationError):
        Policy.load(work, include_sleeve=False)


def test_swing_policy_is_frozen():
    swing = Policy.load(include_sleeve=False).swing
    with pytest.raises(ValidationError):
        swing.capacity.max_open = 99  # type: ignore[misc]


def test_swing_policy_is_percent_only():
    """No account amounts in a public policy file: the only USD figures are liquidity thresholds."""
    text = (POLICY_DIR / SWING_FILE).read_text().lower()
    for word in ("nav_usd", "funding", "balance", "equity_usd", "units"):
        assert word not in text
