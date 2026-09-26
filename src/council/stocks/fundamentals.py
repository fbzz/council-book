"""Per-cycle fundamentals facts of the stock lines (design §11.2): `F:<line>:<field>`, kind
"fundamental", for the fields in `council.facts.evidence_ids.FUNDAMENTAL_FIELDS`.

Rules:
- The four rule features are the live rank's own computation (`council.stocks.rank.features`, over
  the frozen `council.stocks.pit`) on the SEC companyfacts of the line's CIK, recomputed once per UTC
  day: filings are visible when their filing date is before the slot's UTC date (the rule's
  `available_at < D`), exactly as at the rank. rev_yoy, rev_accel, gm_chg and om_chg are percent
  (margins and acceleration in percentage points); filing_age_d is the latest visible filing's age.
- available_at: the SEC acceptance time of the filing of the latest quarter L (from the filer's
  submissions, read as New York wall time, the safe side if SEC's stamp were UTC); without one, the
  end of L's filing date in New York. The pack drops any fact stamped after the slot.
- sector: the FF12 sector the rank recorded in the sleeve file, available from the rank date.
  sector_pct and composite: the rank run's within-sector and global scores in percent, read from the
  private file `<state_dir>/stocks/rank-scores.json` when it belongs to the policy's quarter
  (written by the quarterly rank), available from its `computed_at`.
- Nothing here raises for a data problem: a line without data gets no facts and a flag
  (`fundamentals_failed:<line>`, `fundamentals_missing:<line>` when the filer has no XBRL facts,
  `fundamentals_time_budget:<line>`, `fundamentals_unavailable:no_sec_user_agent`).
- SEC data are US public domain; the public record still withholds fundamentals values until
  docs/data-rights.md lists them (publish.redact).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from council import paths
from council.data.bars import to_utc
from council.data.cache import FileCache, request_key
from council.data.credentials import MissingCredential
from council.data.http import DataError
from council.facts.evidence_ids import FUNDAMENTAL_FIELDS, FUNDAMENTAL_NOT_MATERIAL, fundamental_id
from council.models.facts import Fact
from council.policy import LineSpec, Policy
from council.stocks import pit
from council.stocks.rank import features as rank_features
from council.stocks.universe import Candidate, RankInputs

SOURCE = "sec"
NEW_YORK = ZoneInfo("America/New_York")
NS_FUNDAMENTALS = "fundamentals"
FUNDAMENTALS_TTL_S = 36 * 3600          # one computation per UTC day (the key holds the day)
TIME_BUDGET_S = 90.0
RANK_SCORES_FILE = Path("stocks") / "rank-scores.json"
FEATURE_FIELDS: dict[str, str] = {
    "revenue_growth_yoy": "rev_yoy",
    "revenue_growth_acceleration": "rev_accel",
    "gross_margin_change_yoy": "gm_chg",
    "operating_margin_change_yoy": "om_chg",
}
RANK_FIELDS = ("sector_pct", "composite")
NOT_MATERIAL = FUNDAMENTAL_NOT_MATERIAL     # left out of the material fingerprint (calendar-driven)
_DIGITS = {"rev_yoy": 2, "rev_accel": 2, "gm_chg": 2, "om_chg": 2, "sector_pct": 1, "composite": 1}
if set(FEATURE_FIELDS.values()) | set(RANK_FIELDS) | {"filing_age_d", "sector"} != set(FUNDAMENTAL_FIELDS):
    raise AssertionError("fundamentals fields out of step with evidence_ids.FUNDAMENTAL_FIELDS")
if tuple(FEATURE_FIELDS) != tuple(pit.FUNDAMENTAL_COLUMNS):
    raise AssertionError("the rule's features changed; the fundamentals facts must follow")


# ------------------------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class LineFeatures:
    """What one line's daily facts are built from (JSON-able through `to_json`)."""

    features: Mapping[str, float | None]      # pit.FUNDAMENTAL_COLUMNS -> fraction (None: missing)
    latest_available_at: date | None          # filing date of the latest visible quarter L
    accepted_at: datetime | None              # SEC acceptance time of L's filing (UTC)

    def to_json(self) -> dict[str, Any]:
        return {"features": dict(self.features),
                "latest_available_at": self.latest_available_at.isoformat() if self.latest_available_at else None,
                "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> LineFeatures:
        latest = raw.get("latest_available_at")
        accepted = raw.get("accepted_at")
        return cls(features={k: (None if v is None else float(v)) for k, v in (raw.get("features") or {}).items()},
                   latest_available_at=date.fromisoformat(latest) if latest else None,
                   accepted_at=datetime.fromisoformat(accepted) if accepted else None)


def visibility_day(slot: datetime) -> pd.Timestamp:
    """D for the rule's `available_at < D`: the slot's UTC date (naive midnight, as the rank's D)."""
    return pd.Timestamp(to_utc(slot).date())


def acceptance_times(submissions: Mapping[str, Any]) -> dict[str, datetime]:
    """{accession number: acceptance time (UTC)} from a filer's recent filings. SEC's
    `acceptanceDateTime` is read as New York wall time whatever its suffix: if it were UTC, this
    makes the fact available later than it was, never earlier."""
    from council.stocks.sec import recent_filings

    out: dict[str, datetime] = {}
    for row in recent_filings(submissions):
        accession, stamp = row.get("accessionNumber"), row.get("acceptanceDateTime")
        if not accession or not stamp:
            continue
        try:
            wall = datetime.fromisoformat(str(stamp).replace("Z", "").split(".")[0])
        except ValueError:
            continue
        out[str(accession)] = wall.replace(tzinfo=NEW_YORK).astimezone(UTC)
    return out


def line_features(line: LineSpec, rows: pd.DataFrame, accepted: Mapping[str, datetime], *,
                  slot: datetime) -> LineFeatures:
    """The rule's four features for `line` at the slot, through the live rank's `features` (the
    frozen pit computation on filings dated before the slot's UTC date), plus when L became public."""
    if line.stock is None:
        raise ValueError(f"{line.symbol} is not a stock line")
    cik = int(line.stock.cik)
    day = visibility_day(slot)
    candidate = Candidate(key=line.symbol, symbol=line.symbol, cik=cik)
    feats = rank_features(candidate, RankInputs(candidates=(candidate,), fundamentals={cik: rows}), day)
    values = {k: (float(feats[k]) if pd.notna(feats.get(k)) and math.isfinite(float(feats[k])) else None)
              for k in pit.FUNDAMENTAL_COLUMNS}
    latest = feats.get("latest_available_at")
    latest_day = pd.Timestamp(latest).date() if latest is not None and pd.notna(latest) else None
    accepted_at = None
    if latest_day is not None and not rows.empty:
        period = pd.Timestamp(feats["latest_period_end"])
        frame = rows.assign(period_end=pd.to_datetime(rows["period_end"]),
                            available_at=pd.to_datetime(rows["available_at"]))
        match = frame[(frame["period_end"] == period) & (frame["available_at"].dt.date == latest_day)]
        stamps = [accepted[a] for a in match["accession"].astype(str) if a in accepted]
        accepted_at = max(stamps) if stamps else None
    return LineFeatures(features=values, latest_available_at=latest_day, accepted_at=accepted_at)


# ------------------------------------------------------------------------------------ facts


def _at_midnight(day: date) -> datetime:
    return datetime.combine(day, dtime(0), tzinfo=UTC)


def filing_available_at(feats: LineFeatures) -> datetime | None:
    """When L's numbers became public: its acceptance time, else the end of its filing date in New
    York (the next day's New York midnight); None without a visible filing."""
    if feats.accepted_at is not None:
        return feats.accepted_at.astimezone(UTC)
    if feats.latest_available_at is None:
        return None
    end = datetime.combine(feats.latest_available_at + timedelta(days=1), dtime(0), tzinfo=NEW_YORK)
    return end.astimezone(UTC)


def line_facts(line: LineSpec, feats: LineFeatures | None, *, slot: datetime,
               scores: Mapping[str, float] | None = None, scores_at: datetime | None = None,
               sector_at: datetime | None = None) -> list[Fact]:
    """The F:<line>:<field> facts of one stock line (non-finite values are left out)."""
    sym = line.symbol
    out: list[Fact] = []

    def add(field: str, value: float | str, unit: str, at: datetime) -> None:
        out.append(Fact(id=fundamental_id(sym, field), kind="fundamental", symbol=sym, value=value,
                        unit=unit, available_at=at, source=SOURCE))  # type: ignore[arg-type]

    at = filing_available_at(feats) if feats is not None else None
    if feats is not None and at is not None:
        for column, field in FEATURE_FIELDS.items():
            value = feats.features.get(column)
            if value is not None and math.isfinite(value):
                add(field, round(value * 100.0, _DIGITS[field]) + 0.0, "pct", at)
        if feats.latest_available_at is not None:
            age = (to_utc(slot).date() - feats.latest_available_at).days
            add("filing_age_d", float(age), "days", at)
    if line.stock is not None and sector_at is not None:
        add("sector", line.stock.sector, "text", sector_at)
    if scores and scores_at is not None:
        for field in RANK_FIELDS:
            value = scores.get(field)
            if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value)):
                add(field, round(float(value), _DIGITS[field]) + 0.0, "pct", scores_at)
    return out


def load_rank_scores(state_dir: Path, quarter: str | None) -> tuple[dict[str, dict[str, float]], datetime] | None:
    """({line: {sector_pct, composite}}, computed_at) from `<state_dir>/stocks/rank-scores.json` when it
    belongs to `quarter`; None when absent, unreadable or for another quarter."""
    try:
        raw = json.loads((state_dir / RANK_SCORES_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or quarter is None or raw.get("quarter") != quarter:
        return None
    try:
        at = datetime.fromisoformat(str(raw["computed_at"]))
    except (KeyError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    lines = raw.get("lines")
    if not isinstance(lines, dict):
        return None
    out = {str(k): {f: float(v[f]) for f in RANK_FIELDS if isinstance(v, dict) and isinstance(v.get(f), int | float)}
           for k, v in lines.items()}
    return out, at.astimezone(UTC)


def fundamental_facts(policy: Policy, features: Mapping[str, LineFeatures | None], *, slot: datetime,
                      rank_scores: tuple[Mapping[str, Mapping[str, float]], datetime] | None = None) -> list[Fact]:
    """Facts for every stock line of the policy from `features` ({line: LineFeatures}), the policy's
    sectors and the rank's scores."""
    sleeve = policy.universe.stock_sleeve
    sector_at = _at_midnight(sleeve.rank_asof) if sleeve is not None else None
    scores, scores_at = rank_scores if rank_scores is not None else ({}, None)
    out: list[Fact] = []
    for line in policy.universe.stock_lines():
        out += line_facts(line, features.get(line.symbol), slot=slot, scores=scores.get(line.symbol),
                          scores_at=scores_at, sector_at=sector_at)
    return out


# ------------------------------------------------------------------------------------ gather


def gather_fundamentals(
    policy: Policy,
    *,
    now: datetime,
    sec: Any | None = None,
    sec_factory: Callable[[], Any] | None = None,
    state_dir: Path | None = None,
    cache: FileCache | None = None,
    time_budget_s: float = TIME_BUDGET_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[list[Fact], list[str]]:
    """(facts, flags) for the policy's stock lines at the slot `now`. Each line's features are
    computed once per UTC day and cached; the SEC client (default `SecClient()`, which needs the SEC
    user agent) is created only for a cache miss. Never raises for a data problem."""
    lines = policy.universe.stock_lines()
    if not lines:
        return [], []
    asof = to_utc(now).to_pydatetime()
    root = state_dir or paths.state_dir()
    cache_root = state_dir / "cache" if state_dir is not None else None
    store = cache or FileCache(NS_FUNDAMENTALS, root=cache_root)
    day = visibility_day(asof).date().isoformat()
    deadline = monotonic() + float(time_budget_s)
    flags: list[str] = []
    feats: dict[str, LineFeatures | None] = {}
    client = sec
    owned = False
    unavailable = False
    try:
        for line in lines:
            assert line.stock is not None
            key = request_key({"cik": line.stock.cik, "day": day, "v": 1})
            cached = store.get(key, FUNDAMENTALS_TTL_S, now=asof)
            if isinstance(cached, dict):
                feats[line.symbol] = LineFeatures.from_json(cached)
                continue
            if unavailable:
                continue
            if monotonic() >= deadline:
                flags.append(f"fundamentals_time_budget:{line.symbol}")
                continue
            if client is None:
                try:
                    if sec_factory is not None:
                        client = sec_factory()
                    else:
                        from council.stocks.sec import SecClient

                        client = SecClient(cache_root=cache_root)
                    owned = True
                except MissingCredential:
                    flags.append("fundamentals_unavailable:no_sec_user_agent")
                    unavailable = True
                    continue
            try:
                doc = client.companyfacts(int(line.stock.cik))
                if doc is None:
                    flags.append(f"fundamentals_missing:{line.symbol}")
                    continue
                rows, _stats = pit.fundamentals_comparable(line.symbol, str(int(line.stock.cik)), doc)
                accepted = acceptance_times(client.submissions(int(line.stock.cik)))
                value = line_features(line, rows, accepted, slot=asof)
            except (DataError, ValueError, KeyError, TypeError):
                flags.append(f"fundamentals_failed:{line.symbol}")
                continue
            store.put(key, value.to_json(), now=asof)
            feats[line.symbol] = value
    finally:
        if owned and client is not None:
            client.close()
    sleeve = policy.universe.stock_sleeve
    scores = load_rank_scores(root, sleeve.quarter if sleeve is not None else None)
    return fundamental_facts(policy, feats, slot=asof, rank_scores=scores), flags
