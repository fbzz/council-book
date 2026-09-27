"""What code removed from an agent's output, as structured records (transparency-v2 §4.3).

Every analyst, advocate and manager reply passes code checks before anything acts on it. A card
draft that cites an id the pack never had, a macro driver without known evidence, a proposal for a
line that is not admitted this cycle, a rebuttal of a claim the bull never made: code drops each
one, and until now only a free-text note (`CycleRecord.dropped_cards`) said so. A `Drop` says the
same thing in fields, so the per-line decision trail (`council.publish.trail`) can show it next to
the line it concerned, and a later package can publish it (as `PublicDrop`, through the licensed
text filter).

Fields:
  role    the agent whose output was cut: news, macro, bull_open, bear, bull_rebuttal, ...
  what    which kind of output (card_draft, macro_driver, macro_tilt, proposal_entry, rebuttal;
          triage and deviation are reserved for the triage package and the auditor)
  index   its 1-based position in the agent's reply (draft 3, the second proposal entry, ...)
  code    one closed code (DROP_CODES); the free-text note keeps the old wording
  ids     the evidence ids involved (for unknown_evidence: the ids the pack did not have)
  target  the non-evidence subject: a scope symbol list, a sleeve, a claim id, a line
  lines   the lines it concerned (a draft's scope, a proposal entry's symbol), for the trail
  draft   the dropped card draft itself (PRIVATE; the public form passes its claim through the
          licensed-text filter)

PRIVATE: the record stays in the ledger. Pure: no I/O.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import Field

from council.models.cards import CardDraft
from council.models.common import Strict

DropWhat = Literal[
    "card_draft", "macro_driver", "macro_tilt", "proposal_entry", "rebuttal", "triage", "deviation",
]
DropCode = Literal[
    "type_not_allowed", "scope_not_admitted", "unknown_evidence", "unknown_sleeve",
    "non_admitted_line", "unknown_bull_claim", "not_read", "duplicate", "already_card",
    "missing_code",
]
DROP_CODES: frozenset[str] = frozenset(get_args(DropCode))


class Drop(Strict):
    role: str
    what: DropWhat
    index: int = Field(ge=0)
    code: DropCode
    ids: list[str] = Field(default_factory=list)
    target: str = ""
    lines: list[str] = Field(default_factory=list)
    draft: CardDraft | None = None


def drop_from_problem(
    problem: str, *, role: str, index: int, draft: CardDraft | None = None
) -> Drop:
    """A `Drop` from one of `roles.draft_problem`'s reasons ("unknown_evidence N:1,N:2",
    "scope_not_admitted SEMIS", "type_not_allowed macro_context"). The draft's scope is its lines."""
    code, _, rest = problem.partition(" ")
    subjects = [s for s in rest.split(",") if s]
    lines = list(draft.scope) if draft is not None else []
    if code == "unknown_evidence":
        return Drop(role=role, what="card_draft", index=index, code="unknown_evidence", ids=subjects,
                    lines=lines, draft=draft)
    if code == "scope_not_admitted":
        return Drop(role=role, what="card_draft", index=index, code="scope_not_admitted",
                    target=",".join(subjects), lines=lines, draft=draft)
    return Drop(role=role, what="card_draft", index=index, code="type_not_allowed", target=rest,
                lines=lines, draft=draft)
