"""The adopted stock-sleeve rule: SQ-8, no overlay, the sleeve at 50% of NAV (a recorded user override).

The pre-registered study (docs/stock-sleeve-spec.md, tag `stock-sleeve-spec`) returned NOT ADOPTED
(G1 pass; G2, G3, B1, B2 fail). On 2026-09-25 the user adopted the rule sleeve anyway, as a recorded
override of the gate (docs/stock-sleeve-study.md). This module is the one place live code takes the
adopted rule's constants from (spec item L10):

- the constants below are the adoption record, in code;
- `load_adopted()` reads `policy/variants/stock-sleeve-adopted.yaml` and checks that it says exactly
  the same; checks the tagged variants file byte for byte (SHA-256) and that the adopted cell,
  overlay and shares exist in it; and returns the rule's parameters as that frozen file states them.
  Any difference raises `AdoptedRuleError`: fail closed. Nothing here reads git at run time.
- `reference_sleeve_errors` and `stock_rank_errors` are the checks the policy loader applies to
  `reference.yaml` `sleeve:` and to `stock-rank.yaml` `rule:` once stock lines are live (engineering
  design, validator 8). They return messages; the caller decides the scope of the failure.

Nothing live imports this module before the go-live commit (`invariants.STOCK_SLEEVE_LIVE` is False).
Changing a constant here is a policy change: it needs the matching record edit and a CHANGELOG entry.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
)

from council.paths import POLICY_DIR

ADOPTED_FILE = Path("variants") / "stock-sleeve-adopted.yaml"      # under the policy directory
VARIANTS_FILE = Path("variants") / "stock-sleeve-variants-v1.yaml"

# ---------------------------------------------------------------------------- the record, in code
DECISION = "user_override"
DECIDED_ON = "2026-09-25"
SPEC_TAG = "stock-sleeve-spec"
SPEC_TAG_OBJECT = "50530df4e8f7322c93443cd8012566f9ff2073ab"
SPEC_COMMIT = "fc8fb39337740fe3f6bb640b033d90a770d4cada"
SPEC_DOC = "docs/stock-sleeve-spec.md"
VARIANTS_PATH = "policy/variants/stock-sleeve-variants-v1.yaml"
VARIANTS_SHA256 = "00c155985e3aaaf226877ac737445d11d9318452bfc396397d088b2fcb1aaf28"
RUN_ID = "run-20260926T012203Z"
RESULT_SHA256 = "df14f00158bb40f35d399e20556d88e9761c5de843259dc1712690b0ae6d0cdb"
GATE_SHA256 = "268b3654bdb4d55a2e144abd1af221c3ce7604fab6e054617650d5bb73f86e53"
GATE_VERDICT = "not_adopted"
GATE_CHECKS: Mapping[str, str] = MappingProxyType({
    "G1_pool": "pass",
    "G2_random": "fail",
    "G3_regimes": "fail",
    "B1_book": "fail",
    "B2_index_sleeve": "fail",
})
CELL = "SQ-8"
VARIANT = "SQ"
NAMES = 8
OVERLAY = "none"
SLEEVE_SHARE = 0.50
CORE_SHARE = 0.45
REBASE_FROM_GROSS = 0.95          # core base weights x CORE_SHARE / REBASE_FROM_GROSS (spec section 11)
HEADLINE_INDEXES = ("sp500", "nasdaq100")
AI_LIST = "ranked_mechanically"   # L9: the AI-adjacent list is ranked too (untested extension)
DIVERGENCES: Mapping[str, str] = MappingProxyType({
    "L1": "as_studied", "L2": "as_studied", "L3": "named_divergence", "L4": "named_divergence",
    "L5": "named_divergence", "L6": "named_divergence", "L7": "as_studied", "L8": "never_live",
    "L9": "named_divergence", "L10": "as_studied", "L11": "as_studied", "L12": "as_studied",
    "L13": "as_studied", "L14": "as_studied",
})

_FLOAT_TOL = 1e-12
_Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
_GitSha = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
_Check = Literal["pass", "fail"]
_Status = Literal["as_studied", "named_divergence", "never_live"]


class AdoptedRuleError(ValueError):
    """The adoption record, the tagged variants file or a live policy value disagrees with the adopted
    rule. Callers fail closed (the stock sleeve does not trade)."""


# ------------------------------------------------------------------------- the record file schema
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Spec(_Strict):
    tag: StrictStr
    tag_object: _GitSha
    commit: _GitSha
    doc: StrictStr
    variants_file: StrictStr
    variants_sha256: _Sha256


class _Run(_Strict):
    id: StrictStr
    synthetic: bool
    result_sha256: _Sha256
    gate_sha256: _Sha256


class _Gate(_Strict):
    verdict: Literal["not_adopted", "adopt"]
    checks: dict[StrictStr, _Check]


class _Universe(_Strict):
    headline: list[StrictStr]
    ai_list: Literal["ranked_mechanically", "excluded"]


class _Adopted(_Strict):
    cell: StrictStr
    variant: StrictStr
    names: StrictInt
    overlay: StrictStr
    sleeve_share: StrictFloat
    core_share: StrictFloat
    universe: _Universe


class _Record(_Strict):
    version: Literal[1]
    study: Literal["stock-sleeve"]
    decision: Literal["user_override", "adopt"]
    decided_on: StrictStr
    spec: _Spec
    run: _Run
    gate: _Gate
    adopted: _Adopted
    divergences: dict[StrictStr, _Status]


# ------------------------------------------------------------------------------- the adopted rule
@dataclass(frozen=True)
class AdoptedRule:
    """The adopted cell with its parameters as the tagged variants file states them."""

    decision: str
    decided_on: str
    spec_tag: str
    spec_commit: str
    variants_sha256: str
    run_id: str
    result_sha256: str
    gate_sha256: str
    gate_verdict: str
    gate_checks: Mapping[str, str]
    cell: str
    variant: str
    names: int
    overlay: str
    sleeve_share: float
    core_share: float
    headline_indexes: tuple[str, ...]
    ai_list: str
    divergences: Mapping[str, str]
    # from the tagged variants file
    score: str                              # "sector": the within-sector score orders names
    constraint: str                         # "quota": sector quotas (largest remainder)
    sector_cap: int                         # each quota at most this many names
    hold_buffer_multiple: int               # a held name stays inside the top multiple x quota
    overlay_levels: Mapping[str, float]     # the adopted overlay option's level per SPX trend state
    deadband_level: float                   # fraction of a unit
    deadband_min_nav_share: float           # of book NAV
    budget: str                             # "trim_held_above_target": a rebalance never borrows
    rank_score: str                         # "rank_average"
    features: tuple[str, ...]
    data_layer: str
    peer_group: str
    min_peer_group: int
    rebalance_anchors: tuple[str, ...]
    universe_filters: Mapping[str, Any]     # the study script's filters, which the live rank reproduces

    @property
    def unit(self) -> float:
        """One name's equal weight of NAV: sleeve share / N (0.0625)."""
        return self.sleeve_share / self.names

    @property
    def core_scale(self) -> float:
        """The pro-rata factor on in-reference core base weights: 0.45 / 0.95."""
        return self.core_share / REBASE_FROM_GROSS

    def reference_sleeve(self) -> dict[str, Any]:
        """The `reference.yaml` `sleeve:` keys the go-live commit writes and the loader checks."""
        return {
            "weight": self.sleeve_share,
            "names": self.names,
            "variant": self.variant,
            "overlay": self.overlay,
            "deadband": {"level": self.deadband_level, "min_nav_share": self.deadband_min_nav_share},
        }

    def rank_rule(self) -> dict[str, Any]:
        """The `stock-rank.yaml` `rule:` block the live rank runs and the loader checks."""
        return {
            "cell": self.cell,
            "variant": self.variant,
            "names": self.names,
            "score": self.score,
            "constraint": self.constraint,
            "sector_cap": self.sector_cap,
            "hold_buffer_multiple": self.hold_buffer_multiple,
            "rank_score": self.rank_score,
            "features": list(self.features),
            "data_layer": self.data_layer,
            "peer_group": self.peer_group,
            "min_peer_group": self.min_peer_group,
            "anchors": list(self.rebalance_anchors),
            "universe": {"headline": list(self.headline_indexes), "ai_list": self.ai_list},
        }


def _same_float(a: Any, b: float) -> bool:
    return isinstance(a, int | float) and not isinstance(a, bool) and math.isclose(
        float(a), b, rel_tol=0.0, abs_tol=_FLOAT_TOL)


def _record_errors(rec: _Record) -> list[str]:
    expected: list[tuple[str, Any, Any]] = [
        ("decision", rec.decision, DECISION),
        ("decided_on", rec.decided_on, DECIDED_ON),
        ("spec.tag", rec.spec.tag, SPEC_TAG),
        ("spec.tag_object", rec.spec.tag_object, SPEC_TAG_OBJECT),
        ("spec.commit", rec.spec.commit, SPEC_COMMIT),
        ("spec.doc", rec.spec.doc, SPEC_DOC),
        ("spec.variants_file", rec.spec.variants_file, VARIANTS_PATH),
        ("spec.variants_sha256", rec.spec.variants_sha256, VARIANTS_SHA256),
        ("run.id", rec.run.id, RUN_ID),
        ("run.synthetic", rec.run.synthetic, False),
        ("run.result_sha256", rec.run.result_sha256, RESULT_SHA256),
        ("run.gate_sha256", rec.run.gate_sha256, GATE_SHA256),
        ("gate.verdict", rec.gate.verdict, GATE_VERDICT),
        ("gate.checks", dict(rec.gate.checks), dict(GATE_CHECKS)),
        ("adopted.cell", rec.adopted.cell, CELL),
        ("adopted.variant", rec.adopted.variant, VARIANT),
        ("adopted.names", rec.adopted.names, NAMES),
        ("adopted.overlay", rec.adopted.overlay, OVERLAY),
        ("adopted.universe.headline", tuple(rec.adopted.universe.headline), HEADLINE_INDEXES),
        ("adopted.universe.ai_list", rec.adopted.universe.ai_list, AI_LIST),
        ("divergences", dict(rec.divergences), dict(DIVERGENCES)),
    ]
    errors = [f"{key} is {got!r}, the adopted rule says {want!r}" for key, got, want in expected if got != want]
    for key, got, want in (("adopted.sleeve_share", rec.adopted.sleeve_share, SLEEVE_SHARE),
                           ("adopted.core_share", rec.adopted.core_share, CORE_SHARE)):
        if not _same_float(got, want):
            errors.append(f"{key} is {got!r}, the adopted rule says {want!r}")
    if rec.adopted.cell != f"{rec.adopted.variant}-{rec.adopted.names}":
        errors.append(f"adopted.cell {rec.adopted.cell!r} is not variant-names "
                      f"({rec.adopted.variant}-{rec.adopted.names})")
    return errors


def _get(tree: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(tree, Mapping) or key not in tree:
            raise AdoptedRuleError(f"{VARIANTS_PATH}: missing {'.'.join(keys)}")
        tree = tree[key]
    return tree


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


def _variants_rule(raw: bytes) -> dict[str, Any]:
    """The adopted cell's parameters from the tagged variants file, after checking its bytes and that
    it defines the adopted cell, overlay and shares."""
    digest = hashlib.sha256(raw).hexdigest()
    if digest != VARIANTS_SHA256:
        raise AdoptedRuleError(f"{VARIANTS_PATH} is not the tagged file (sha256 {digest[:12]}..., "
                               f"expected {VARIANTS_SHA256[:12]}...)")
    data = yaml.safe_load(raw)
    errors: list[str] = []
    if _get(data, "tag") != SPEC_TAG:
        errors.append(f"tag {_get(data, 'tag')!r} is not {SPEC_TAG!r}")
    variant = _get(data, "variants", VARIANT)
    if variant.get("selectable") is not True:
        errors.append(f"variant {VARIANT} is not selectable")
    if NAMES not in _get(data, "n_names"):
        errors.append(f"N = {NAMES} is not a studied name count")
    options = _get(data, "overlay", "options")
    if OVERLAY not in options:
        errors.append(f"overlay {OVERLAY!r} is not a studied option")
    for keys, want in ((("sleeve", "share_of_nav"), SLEEVE_SHARE),
                       (("whole_book", "sleeve_share"), SLEEVE_SHARE),
                       (("whole_book", "core_share"), CORE_SHARE)):
        if not _same_float(_get(data, *keys), want):
            errors.append(f"{'.'.join(keys)} is not {want}")
    if errors:
        raise AdoptedRuleError(f"{VARIANTS_PATH} does not define the adopted rule: " + "; ".join(errors))
    deadband = _get(data, "sleeve", "deadband")
    rule = _get(data, "rule")
    return {
        "score": variant["score"],
        "constraint": variant["constraint"],
        "sector_cap": variant["cap"],
        "hold_buffer_multiple": _get(data, "hold_buffer_multiple"),
        "overlay_levels": MappingProxyType({k: float(v) for k, v in options[OVERLAY].items()}),
        "deadband_level": float(deadband["level"]),
        "deadband_min_nav_share": float(deadband["min_nav_share"]),
        "budget": _get(data, "sleeve", "budget"),
        "rank_score": rule["score"],
        "features": tuple(rule["features"]),
        "data_layer": rule["data_layer"],
        "peer_group": rule["peer_group"],
        "min_peer_group": rule["min_peer_group"],
        "rebalance_anchors": tuple(_get(data, "rebalance", "anchors")),
        "universe_filters": _freeze(_get(data, "universe")),
    }


def load_adopted(policy_dir: Path | None = None) -> AdoptedRule:
    """Read and check the adoption record and the tagged variants file under `policy_dir` (default:
    the repository's `policy/`; a live context passes its HEAD snapshot). Raises `AdoptedRuleError`
    on any difference from the constants above."""
    root = policy_dir or POLICY_DIR
    record_path, variants_path = root / ADOPTED_FILE, root / VARIANTS_FILE
    try:
        record_raw = record_path.read_bytes()
        variants_raw = variants_path.read_bytes()
    except OSError as exc:
        name = Path(exc.filename).name if exc.filename else "?"
        raise AdoptedRuleError(f"adoption record unreadable: {exc.__class__.__name__}: {name}") from None
    try:
        record = _Record.model_validate(yaml.safe_load(record_raw))
    except (yaml.YAMLError, ValidationError) as exc:
        raise AdoptedRuleError(f"{ADOPTED_FILE} is malformed: {exc}") from None
    errors = _record_errors(record)
    if errors:
        raise AdoptedRuleError(f"{ADOPTED_FILE} disagrees with the adopted rule: " + "; ".join(errors))
    try:
        frozen = _variants_rule(variants_raw)
    except (yaml.YAMLError, KeyError, TypeError, AttributeError) as exc:
        raise AdoptedRuleError(f"{VARIANTS_PATH} is malformed: {exc!r}") from None
    return AdoptedRule(
        decision=record.decision,
        decided_on=record.decided_on,
        spec_tag=record.spec.tag,
        spec_commit=record.spec.commit,
        variants_sha256=record.spec.variants_sha256,
        run_id=record.run.id,
        result_sha256=record.run.result_sha256,
        gate_sha256=record.run.gate_sha256,
        gate_verdict=record.gate.verdict,
        gate_checks=MappingProxyType(dict(record.gate.checks)),
        cell=record.adopted.cell,
        variant=record.adopted.variant,
        names=record.adopted.names,
        overlay=record.adopted.overlay,
        sleeve_share=record.adopted.sleeve_share,
        core_share=record.adopted.core_share,
        headline_indexes=tuple(record.adopted.universe.headline),
        ai_list=record.adopted.universe.ai_list,
        divergences=MappingProxyType(dict(record.divergences)),
        **frozen,
    )


@lru_cache(maxsize=1)
def adopted_rule() -> AdoptedRule:
    """The repository's adopted rule, loaded and checked once per process."""
    return load_adopted()


# ------------------------------------------------------------------ checks on the live policy files
def _diff(where: str, got: Any, want: Any) -> list[str]:
    """Messages for every key of `want` that `got` lacks or states differently; extra keys in `got`
    are refused too (an unknown key would be a setting the study never saw)."""
    if isinstance(want, Mapping):
        if not isinstance(got, Mapping):
            return [f"{where} must be a mapping"]
        errors = [f"{where}.{k}: unexpected key" for k in got if k not in want]
        for key, value in want.items():
            if key not in got:
                errors.append(f"{where}.{key}: missing (the adopted rule says {value!r})")
            else:
                errors += _diff(f"{where}.{key}", got[key], value)
        return errors
    if isinstance(want, float):
        return [] if _same_float(got, want) else [f"{where} is {got!r}, the adopted rule says {want!r}"]
    if isinstance(want, int) and (isinstance(got, bool) or not isinstance(got, int) or got != want):
        return [f"{where} is {got!r}, the adopted rule says {want!r}"]
    if isinstance(want, list):
        if not isinstance(got, list | tuple) or list(got) != want:
            return [f"{where} is {got!r}, the adopted rule says {want!r}"]
        return []
    return [] if (type(got) is type(want) and got == want) else [
        f"{where} is {got!r}, the adopted rule says {want!r}"]


def reference_sleeve_errors(sleeve: Any, rule: AdoptedRule | None = None) -> list[str]:
    """`reference.yaml` `sleeve:` against the adopted rule (weight, names, variant, overlay, deadband)."""
    if sleeve is None:
        return ["reference.yaml has no sleeve: section"]
    return _diff("reference.sleeve", sleeve, (rule or adopted_rule()).reference_sleeve())


def stock_rank_errors(stocks: Mapping[str, Any], rule: AdoptedRule | None = None) -> list[str]:
    """`stock-rank.yaml` `rule:` against the adopted rule. The file's other keys are live-only settings
    (membership sources, shortlist size, history symbol cap) and are not checked here."""
    if not isinstance(stocks, Mapping) or "rule" not in stocks:
        return ["stock-rank.yaml has no rule: section"]
    return _diff("stock_rank.rule", stocks["rule"], (rule or adopted_rule()).rank_rule())
