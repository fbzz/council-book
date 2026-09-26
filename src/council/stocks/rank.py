"""The live quarterly stock rank: `rank(asof, inputs, config) -> RankResult`. Pure: no network, no
files, no clock.

It reproduces the pre-registered study's universe filters (docs/stock-sleeve-spec.md section 3;
`Universe.at` in scripts/stock_sleeve_study.py, which stays in the frozen script) step by step and in
the same order, then scores and selects with the FROZEN modules it imports and never copies:
`council.stocks.pit.comparable_features` (the four features on filings with available_at < D),
`council.stocks.sectors.ff12`, and `council.stocks.score.add_scores`, `ordered` and `select_rule`
(called through the module, so a test can prove there is no private copy). A test pins the eligible
set, the funnel and the selections to the tagged script on the same inputs.

Recorded override (user decision, 2026-09-25): the study was NOT ADOPTED (G1 passed; G2, G3, B1 and
B2 failed) and the user adopted the rule sleeve anyway with the selected cell SQ-8 (sector score,
sector quotas, 8 names), no overlay. The AI-adjacent list is ranked with the index members (L9).
`RankConfig.selected()` takes the rule's constants from the adoption record (`council.stocks.adopted`,
spec L10); `RankConfig.from_variants` reads a cell straight from the tagged variants file (tests).

Live-only additions, none of which changes the selection:
- the shortlist: the next `shortlist_size` names chosen by the same frozen `select_rule` over the
  eligible names left after the selection (so the same sector constraint applies);
- `order`: every eligible name by the variant's score (`score.ordered`), for eligibility
  replacements;
- per-name exclusion reasons and the count of selected names that are in the universe only through
  the AI list;
- membership freshness: a source older than `max_source_age_days` (120) or dated after the rank
  date is refused.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from council.paths import POLICY_DIR
from council.stocks import pit, score, sectors
from council.stocks.universe import Candidate, RankInputs, dedupe_by_cik

VARIANTS_PATH = POLICY_DIR / "variants" / "stock-sleeve-variants-v1.yaml"
DEFAULT_CELL = "SQ-8"            # the user's recorded override (module docstring; = adopted.CELL)
DEFAULT_SHORTLIST = 8
MAX_SOURCE_AGE_DAYS = 120
FEATURES = score.FEATURES
FUNNEL = ("member", "mapped", "common", "priced", "listed", "one_per_cik", "sector_known", "not_money",
          "companyfacts", "us_gaap", "visible", "domestic", "fresh", "all_features", "revenue_floor", "plausible")
QUARTERS = ("revenue_L", "revenue_P", "revenue_Y", "revenue_PY")
FRAME_COLUMNS = ("key", "symbol", "cik", "sector", *FEATURES, *QUARTERS, "latest_available_at",
                 "latest_period_end")
EXTRA_COLUMNS = ("line_id", "ai_only")

# Spec values the live rank implements in only one way, as the study script's `_FIXED_BY_CODE`.
_FIXED_REBALANCE: tuple[tuple[str, ...], Any] = (("rebalance", "filings_visible"), "filed_before_D")
_FIXED: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("universe", "dedupe"), "cik"),
    (("universe", "sector"), "ff12_from_sic"),
    (("universe", "require_all_features"), True),
    (("universe", "fundamentals_over"), "cik_chain"),
    (("universe", "plausibility"), {"gross_profit_within_revenue": True, "max_abs_operating_margin": 1.0}),
    (("rule", "score"), "rank_average"),
    (("rule", "peer_group"), "ff12"),
    (("rule", "data_layer"), "comparable_year_ago"),
)


def _check_fixed(spec: Mapping[str, Any], checks: tuple[tuple[tuple[str, ...], Any], ...]) -> None:
    for keys, expected in checks:
        node: Any = spec
        for k in keys:
            node = node.get(k) if isinstance(node, Mapping) else None
        if node != expected:
            raise ValueError(f"spec {'.'.join(keys)} = {node!r}; the live rank implements only {expected!r}")
    if tuple(spec["rule"]["features"]) != tuple(pit.FUNDAMENTAL_COLUMNS):
        raise ValueError("spec rule.features differ from the frozen rule")


def _plain(value: Any) -> Any:
    """Read-only mappings and tuples (the adoption record's frozen view) as plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


class RankError(RuntimeError):
    """The rank cannot run on these inputs (stale or future membership, a lookahead bar)."""


# ------------------------------------------------------------------------------------ config


@dataclass(frozen=True)
class RankConfig:
    """The rule's parameters: the selected cell and the spec's universe block, plus live settings."""

    cell: str = DEFAULT_CELL
    variant: Mapping[str, Any] = field(default_factory=lambda: {"score": "sector", "constraint": "quota", "cap": 3})
    n: int = 8
    hold_buffer_multiple: float = 2.0
    min_peer_group: int = 5
    security_types: tuple[str, ...] = ("common",)
    exclude_adr: bool = True
    max_bar_age_days: int = 7
    min_listing_days: int = 290
    dedupe_volume_sessions: int = 63
    exclude_sectors: tuple[str, ...] = ("Money",)
    taxonomy: str = "us-gaap"
    domestic_forms: tuple[str, ...] = ("10-Q", "10-K", "10-Q/A", "10-K/A", "10-QT", "10-KT")
    max_filing_age_days: int = 120
    min_quarter_revenue_usd: float = 50_000_000.0
    shortlist_size: int = DEFAULT_SHORTLIST
    max_source_age_days: int = MAX_SOURCE_AGE_DAYS

    def __post_init__(self) -> None:
        v = self.variant
        if v.get("score") not in ("sector", "global") or v.get("constraint") not in ("cap", "quota"):
            raise ValueError(f"unknown variant {dict(v)}")
        if int(v.get("cap", 0)) < 1 or self.n < 1 or self.shortlist_size < 0 or self.hold_buffer_multiple < 1:
            raise ValueError("cap and n must be positive, the shortlist non-negative, the buffer at least 1")

    @classmethod
    def from_variants(cls, spec: Mapping[str, Any], cell: str = DEFAULT_CELL, *,
                      shortlist_size: int = DEFAULT_SHORTLIST,
                      max_source_age_days: int = MAX_SOURCE_AGE_DAYS) -> RankConfig:
        """The config of `cell` ("SQ-8") in the pre-registered variants file. Refuses a cell the spec
        does not define, a non-selectable (control) variant, and spec values this code does not
        implement."""
        _check_fixed(spec, (_FIXED_REBALANCE, *_FIXED))
        return cls._build(spec, cell, shortlist_size=shortlist_size, max_source_age_days=max_source_age_days)

    @classmethod
    def from_adopted(cls, rule: Any, *, shortlist_size: int = DEFAULT_SHORTLIST,
                     max_source_age_days: int = MAX_SOURCE_AGE_DAYS) -> RankConfig:
        """The config of the adopted rule, a `council.stocks.adopted.AdoptedRule`: the one place live
        code takes the rule's constants from (spec L10). `load_adopted` has already checked the
        record against the tagged variants file byte for byte, which also pins `filings_visible`."""
        spec = {
            "universe": _plain(rule.universe_filters),
            "rule": {"features": list(rule.features), "min_peer_group": int(rule.min_peer_group),
                     "score": rule.rank_score, "peer_group": rule.peer_group, "data_layer": rule.data_layer},
            "variants": {rule.variant: {"score": rule.score, "constraint": rule.constraint,
                                        "cap": int(rule.sector_cap), "selectable": True}},
            "n_names": [int(rule.names)],
            "hold_buffer_multiple": rule.hold_buffer_multiple,
        }
        _check_fixed(spec, _FIXED)
        return cls._build(spec, rule.cell, shortlist_size=shortlist_size, max_source_age_days=max_source_age_days)

    @classmethod
    def _build(cls, spec: Mapping[str, Any], cell: str, *, shortlist_size: int,
               max_source_age_days: int) -> RankConfig:
        name, _, n_text = cell.partition("-")
        variant = spec["variants"].get(name)
        if variant is None or not n_text.isdigit() or int(n_text) not in [int(x) for x in spec["n_names"]]:
            raise ValueError(f"cell {cell!r} is not in the pre-registered spec")
        if not variant.get("selectable"):
            raise ValueError(f"variant {name} is a reported control, never live")
        u = spec["universe"]
        return cls(
            cell=cell, variant={k: variant[k] for k in ("score", "constraint", "cap")}, n=int(n_text),
            hold_buffer_multiple=float(spec["hold_buffer_multiple"]), min_peer_group=int(spec["rule"]["min_peer_group"]),
            security_types=tuple(u["security_types"]), exclude_adr=bool(u.get("exclude_adr", True)),
            max_bar_age_days=int(u["max_bar_age_days"]), min_listing_days=int(u["min_listing_days"]),
            dedupe_volume_sessions=int(u["dedupe_volume_sessions"]), exclude_sectors=tuple(u["exclude_sectors"]),
            taxonomy=str(u["taxonomy"]), domestic_forms=tuple(u["domestic_forms"]),
            max_filing_age_days=int(u["max_filing_age_days"]),
            min_quarter_revenue_usd=float(u["min_quarter_revenue_usd"]),
            shortlist_size=shortlist_size, max_source_age_days=max_source_age_days,
        )

    @classmethod
    def selected(cls, **kw: Any) -> RankConfig:
        """The adopted rule's config, from `council.stocks.adopted` (fails closed when the adoption
        record or the tagged variants file disagrees with it)."""
        from council.stocks import adopted  # the adoption record; imported only when asked for

        return cls.from_adopted(adopted.adopted_rule(), **kw)

    def digest(self) -> str:
        """sha256 of the config's canonical JSON (recorded with a rank)."""
        doc = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in asdict(self).items()}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------------------------ result


@dataclass(frozen=True)
class RankResult:
    asof: pd.Timestamp
    config: RankConfig
    selected: tuple[str, ...]            # keys, in select_rule's order (best first)
    shortlist: tuple[str, ...]
    order: tuple[str, ...]               # every eligible key by the variant's score
    kept: tuple[str, ...]                # held names the buffer kept
    eligible: pd.DataFrame               # scored eligible set (PRIVATE: revenue levels)
    funnel: dict[str, int]
    excluded: dict[str, str]             # key -> reason
    unmapped: tuple[tuple[str, str], ...]
    ai_only: frozenset[str]              # keys in the universe only through the AI list

    @property
    def exclusion_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(self.excluded.values()).items()))

    @property
    def ai_selected(self) -> tuple[str, ...]:
        """Selected keys that come only from the AI list (L9: reported every quarter)."""
        return tuple(k for k in self.selected if k in self.ai_only)

    def symbols(self, keys: tuple[str, ...] | list[str]) -> list[str]:
        return [str(self.eligible.at[k, "symbol"]) for k in keys]

    def role(self, key: str) -> str | None:
        if key in self.selected:
            return "selected"
        if key in self.shortlist:
            return "shortlist"
        return None


# ------------------------------------------------------------------------------------ the rank


def _day(value: Any) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def _empty_fundamentals() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype=object) for c in pit.COMPARABLE_COLUMNS})
    for c in ("period_end", "available_at", "ya_period_end"):
        frame[c] = pd.Series(dtype="datetime64[ns]")
    return frame


def check_sources(inputs: RankInputs, when: pd.Timestamp, max_age_days: int) -> None:
    """Every membership source is dated on or before the rank date and at most `max_age_days` old."""
    for s in inputs.sources:
        as_of = pd.Timestamp(s.as_of).normalize()
        if as_of > when:
            raise RankError(f"membership source {s.name} is dated {as_of.date()}, after the rank date "
                            f"{when.date()}: read the revision as of the rank date")
        if (when - as_of).days > max_age_days:
            raise RankError(f"membership source {s.name} is {(when - as_of).days} days old "
                            f"(at most {max_age_days})")


def features(c: Candidate, inputs: RankInputs, when: pd.Timestamp) -> dict[str, Any]:
    """The four features of one candidate at D: the filings of every CIK in its chain, concatenated
    in CIK order, restricted to available_at < D (strict: a filing dated D is after the decision),
    then the frozen `pit.comparable_features`."""
    parts = [inputs.fundamentals[k] for k in c.chain if k in inputs.fundamentals and not inputs.fundamentals[k].empty]
    fund = pd.concat(parts, ignore_index=True) if parts else _empty_fundamentals()
    fund = fund.assign(ticker="X", available_at=pd.to_datetime(fund["available_at"]))
    visible = fund[fund["available_at"] < when]
    return pit.comparable_features(visible, "X", when)


def rank(asof: date | datetime | str | pd.Timestamp, inputs: RankInputs, config: RankConfig) -> RankResult:
    """Filters (study order) -> scores -> the frozen selection, the shortlist and the full order."""
    when = _day(asof)
    if when is None:
        raise ValueError("rank date is required")
    check_sources(inputs, when, config.max_source_age_days)
    funnel = dict.fromkeys(FUNNEL, 0)
    excluded: dict[str, str] = {}
    rows: list[Candidate] = list(inputs.candidates)
    keys = [c.key for c in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("candidate keys must be unique")

    def keep(pred: Callable[[Candidate], bool], reason: str, step: str | None) -> None:
        nonlocal rows
        out = []
        for c in rows:
            if pred(c):
                out.append(c)
            else:
                excluded.setdefault(c.key, reason)
        rows = out
        if step is not None:
            funnel[step] = len(rows)

    funnel["member"] = len(rows) + len(inputs.unmapped)
    funnel["mapped"] = len(rows)
    keep(lambda c: c.security_type in config.security_types and not (config.exclude_adr and c.is_adr),
         "not_common", "common")
    oldest = when - pd.Timedelta(days=config.max_bar_age_days)
    for c in rows:
        last = _day(c.last_bar)
        if last is not None and last > when:
            raise RankError(f"{c.key}: last bar {last.date()} is after the rank date (lookahead)")
    keep(lambda c: _day(c.last_bar) is not None and _day(c.last_bar) >= oldest, "not_priced", "priced")
    listed_by = when - pd.Timedelta(days=config.min_listing_days)
    start = _day(inputs.panel_start)
    keep(lambda c: _day(c.first_bar) is not None and (_day(c.first_bar) <= listed_by or _day(c.first_bar) == start),
         "not_listed", "listed")
    keep(lambda c: c.cik is not None, "no_cik", None)
    rows, dropped = dedupe_by_cik(rows)
    for c in dropped:
        excluded.setdefault(c.key, "duplicate_cik")
    funnel["one_per_cik"] = len(rows)
    sector = {c.key: sectors.ff12(inputs.sic.get(int(c.cik))) for c in rows}   # type: ignore[arg-type]
    keep(lambda c: sector[c.key] is not None, "no_sector", "sector_known")
    keep(lambda c: sector[c.key] not in config.exclude_sectors, "excluded_sector", "not_money")
    tax = inputs.taxonomy
    keep(lambda c: tax.get(int(c.cik), "no_companyfacts") != "no_companyfacts", "no_companyfacts", "companyfacts")  # type: ignore[arg-type]
    keep(lambda c: tax.get(int(c.cik)) == config.taxonomy, "not_us_gaap", "us_gaap")  # type: ignore[arg-type]
    feats = {c.key: features(c, inputs, when) for c in rows}
    keep(lambda c: feats[c.key].get("quarters_of_history", 0) > 0, "no_visible_quarter", "visible")
    keep(lambda c: feats[c.key].get("form_L") in set(config.domestic_forms), "not_domestic_form", "domestic")
    max_age = pd.Timedelta(days=config.max_filing_age_days)
    keep(lambda c: pd.notna(feats[c.key]["latest_available_at"]) and when - feats[c.key]["latest_available_at"] <= max_age,
         "stale_filing", "fresh")
    keep(lambda c: all(np.isfinite(float(feats[c.key][k])) for k in FEATURES), "missing_feature", "all_features")
    floor = float(config.min_quarter_revenue_usd)
    keep(lambda c: all(float(feats[c.key][k]) >= floor for k in QUARTERS), "revenue_floor", "revenue_floor")
    keep(lambda c: bool(feats[c.key].get("plausible")), "implausible", "plausible")

    records = [{"key": c.key, "symbol": c.symbol, "cik": int(c.cik), "sector": sector[c.key],  # type: ignore[arg-type]
                **{k: feats[c.key].get(k) for k in (*FEATURES, *QUARTERS, "latest_available_at", "latest_period_end")},
                "line_id": c.line_id, "ai_only": c.ai_only} for c in rows]
    frame = pd.DataFrame(records, columns=[*FRAME_COLUMNS, *EXTRA_COLUMNS]).set_index("key", drop=False)
    scored = score.add_scores(frame, config.min_peer_group)
    variant = dict(config.variant)
    held = list(inputs.held)
    selected = score.select_rule(scored, held, variant, config.n, config.hold_buffer_multiple)
    order = score.ordered(scored, variant["score"]) if not scored.empty else []
    rest = scored.drop(index=list(selected))
    shortlist = (score.select_rule(rest, [], variant, config.shortlist_size, config.hold_buffer_multiple)
                 if config.shortlist_size > 0 else [])
    return RankResult(
        asof=when, config=config, selected=tuple(selected), shortlist=tuple(shortlist), order=tuple(order),
        kept=tuple(k for k in selected if k in set(held)), eligible=scored, funnel=funnel, excluded=excluded,
        unmapped=tuple(inputs.unmapped), ai_only=frozenset(c.key for c in inputs.candidates if c.ai_only),
    )
