"""The public, weightless ops row of an onboarding smoke ticket (m5-readiness §8.4, M5-D2).

Rules:
- One row per smoke decision, upserted by `id` in `journal/ops/smoke.jsonl` (its own file: the
  cycle ops file, punctuality, the heartbeat, `first_cycle_of_utc_day` and scoring never see it).
- Fields: `id` (`<UTC minute>-smoke-<step>`, which never matches the cycle-id pattern), `type`
  ("smoke_test"), `step`, `state` (proposed | completed | blocked | rejected) and `commitment` =
  sha256(salt ‖ canonical private plan). Nothing that carries a weight, a size, a symbol, a price
  or an amount: the plan, legs and fills stay in the private ledger.
- The commitment is never revealed publicly (a reveal would publish the weight and so the NAV);
  the private reveal file stays in `state_dir/salts/smoke/` for audit.
- The same step at two funding levels gives byte-identical rows (the row has no NAV input apart
  from the commitment, whose salt is random either way).

The site (T6) labels these rows "onboarding smoke test"; this module does not touch `site/`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import Field

from council.publish.journal import JOURNAL
from council.publish.public_models import Hex64, PublicModel

SMOKE_STEP_PATTERN = r"^S[1-8][a-z]{0,2}$"
SMOKE_ID_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{4}Z-smoke-S[1-8][a-z]{0,2}$"
SMOKE_PATH = f"{JOURNAL}/ops/smoke.jsonl"
SmokeState = Literal["proposed", "completed", "blocked", "rejected"]
SMOKE_STATES: tuple[str, ...] = ("proposed", "completed", "blocked", "rejected")
_ID = re.compile(SMOKE_ID_PATTERN)


class PublicSmokeRow(PublicModel):
    """The weightless public row of one smoke ticket."""

    id: Annotated[str, Field(pattern=SMOKE_ID_PATTERN)]
    type: Literal["smoke_test"] = "smoke_test"
    step: Annotated[str, Field(pattern=SMOKE_STEP_PATTERN)]
    state: SmokeState
    commitment: Hex64


def is_smoke_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def smoke_files(existing: bytes | None, rows: Iterable[PublicSmokeRow]) -> dict[str, bytes]:
    """Append or replace smoke rows by id (idempotent; sorted by id)."""
    by_id: dict[str, str] = {}
    for raw in (existing or b"").decode().splitlines():
        if raw.strip():
            by_id[str(json.loads(raw)["id"])] = raw.strip()
    for row in rows:
        by_id[row.id] = json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False)
    return {SMOKE_PATH: "".join(by_id[k] + "\n" for k in sorted(by_id)).encode()}


def public_smoke_state(decision_state: str, *, previous: str | None = None, executed: bool = False) -> str | None:
    """The public state of a smoke ticket in `decision_state`, or None while it is approved,
    executing or waiting for its market (the row keeps its last state). A partial completion is
    `blocked` (not a clean proof); expired and superseded read as `rejected`; an operator review
    keeps `blocked` for a ticket that reached the broker."""
    if decision_state in ("awaiting_publication", "proposed"):
        return "proposed"
    if decision_state == "completed":
        return "completed"
    if decision_state in ("completed_partial", "blocked", "execution_unknown"):
        return "blocked"
    if decision_state in ("rejected", "expired", "superseded"):
        return "rejected"
    if decision_state == "reviewed_no_action":
        return "blocked" if executed or previous == "blocked" else "rejected"
    return None
