"""journal/paper/whatif.json: "if we had bought every idea" (percent only).

Built from `council.swing.whatif` outcomes for the PUBLISHED paper decisions only (an idea is
matched to its decision by origin cycle, and to its public idea ref by ticker + side in that
decision's document), leak-scanned (canaries: the private slot references) and written. Never a
price: the model has no field that could hold one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from council.publish.paper import (
    DECLARED_COST_PCT_PER_LEG,
    PAPER_DIR,
    PublicPaperCycle,
    dump,
    read_rows,
    scan_files,
)
from council.publish.public_models import Code, Line, Pct, PublicModel, SwingSide, UtcDatetime
from council.publish.redact import swing_line

WHATIF_PATH = f"{PAPER_DIR}/whatif.json"
Status = Literal["open", "stop", "target", "time", "corporate_action"]
Share = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
IdeaRef = Annotated[str, Field(pattern=r"^idea:\d{1,4}$")]


class WhatIfIdea(PublicModel):
    decision_no: int = Field(ge=1)
    ref: IdeaRef                       # the idea's public ref on its decision page (idea:N)
    ticker: Line
    side: SwingSide
    group: Code
    drop_code: Code | None = None
    skeptic_verdict: Code | None = None
    ref_prior_close: bool = False      # priced at the previous close, not at the slot
    status: Status
    days_held: int = Field(ge=0, le=400)
    gross_pct: Pct | None = None
    net_pct: Pct | None = None
    corporate_action: Code | None = None


class WhatIfAgg(PublicModel):
    key: Code
    n: int = Field(ge=0)
    mean_net_pct: Pct | None = None
    median_net_pct: Pct | None = None
    mean_gross_pct: Pct | None = None
    hit_rate_pct: Share | None = None
    open: int = Field(ge=0)
    resolved: int = Field(ge=0)
    excluded: int = Field(ge=0)


class PublicPaperWhatIf(PublicModel):
    schema_id: Literal["council-book/paper-whatif/v1"] = "council-book/paper-whatif/v1"
    mode: Literal["paper"] = "paper"
    as_of: UtcDatetime
    marked_to: date | None = None              # the last completed session the marks use
    declared_cost_pct_per_leg: Pct = DECLARED_COST_PCT_PER_LEG
    decisions: int = Field(ge=0)
    ideas: list[WhatIfIdea] = Field(default_factory=list, max_length=2000)
    groups: list[WhatIfAgg] = Field(default_factory=list, max_length=64)
    drop_codes: list[WhatIfAgg] = Field(default_factory=list, max_length=200)
    verdicts: list[WhatIfAgg] = Field(default_factory=list, max_length=16)


def _public_refs(root: Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[int, dict[tuple[str, str], str]]]:
    """{cycle_id: (decision_no, {(line, side): public idea ref})} of the published paper decisions."""
    out: dict[str, tuple[int, dict[tuple[str, str], str]]] = {}
    for r in rows:
        refs: dict[tuple[str, str], str] = {}
        path = Path(root) / str(r.get("path") or "")
        if path.is_file():
            doc = PublicPaperCycle.model_validate_json(path.read_bytes())
            for p in (doc.swing.ideas if doc.swing else []):
                refs.setdefault((p.idea.ticker, p.idea.side), p.idea.ref)
        out[str(r["cycle_id"])] = (int(r["decision_no"]), refs)
    return out


def whatif_doc(items: Sequence[Any], *, root: Path, as_of: datetime, marked_to: date | None = None) -> PublicPaperWhatIf:
    """The public document for the outcomes (`council.swing.whatif.WhatIf`) of published decisions."""
    from council.swing.whatif import summary

    pub = _public_refs(root, read_rows(Path(root)))
    keep, ideas = [], []
    for it in items:
        hit = pub.get(it.cycle_id)
        line = swing_line(it.ticker)
        if hit is None or line is None:
            continue
        no, refs = hit
        ref = refs.get((line, it.side))
        if ref is None or not ref.startswith("idea:"):
            continue
        keep.append(it)
        ideas.append(WhatIfIdea(
            decision_no=no, ref=ref, ticker=line, side=it.side, group=it.group, drop_code=it.drop_code,
            skeptic_verdict=it.skeptic_verdict, ref_prior_close=it.ref_source == "prior_close", status=it.status,
            days_held=min(400, max(0, int(it.days_held))),
            gross_pct=round(it.gross_pct, 2) if it.gross_pct is not None else None,
            net_pct=round(it.net_pct, 2) if it.net_pct is not None else None,
            corporate_action=it.corporate_action))
    s = summary(keep)
    agg = lambda xs: [WhatIfAgg(**a) for a in xs]          # noqa: E731
    ideas.sort(key=lambda x: (x.decision_no, int(x.ref.split(":")[1])))
    return PublicPaperWhatIf(as_of=as_of, marked_to=marked_to, decisions=len({x.decision_no for x in ideas}),
                             ideas=ideas, groups=agg(s["groups"]), drop_codes=agg(s["drop_codes"]),
                             verdicts=agg(s["verdicts"]))


def whatif_files(items: Sequence[Any], *, root: Path, as_of: datetime, marked_to: date | None = None,
                 canaries: Sequence[str | float] = ()) -> dict[str, bytes]:
    """{WHATIF_PATH: bytes}, leak-scanned (raises `PaperPublishError`, nothing written)."""
    files = {WHATIF_PATH: dump(whatif_doc(items, root=root, as_of=as_of, marked_to=marked_to))}
    scan_files(files, canaries=canaries)
    return files


def publish_whatif(items: Sequence[Any], *, root: Path, as_of: datetime, marked_to: date | None = None,
                   canaries: Sequence[str | float] = ()) -> list[Path]:
    from council.publish.paper import write

    return write(Path(root), whatif_files(items, root=root, as_of=as_of, marked_to=marked_to, canaries=canaries))


__all__ = ["WHATIF_PATH", "PublicPaperWhatIf", "WhatIfAgg", "WhatIfIdea", "publish_whatif", "whatif_doc",
           "whatif_files"]
