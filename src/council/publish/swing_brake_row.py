"""The public ops row of a swing-book pause (design swing-book.md rev 2, §3.1 S15 and §1.5; SW-4b).

Rules:
- One row per pause event, upserted by `id` in `journal/ops/swing_brake.jsonl` (its own file: the
  cycle ops file, punctuality, the heartbeat and scoring never see it).
- Fields: `id` (`<UTC minute>-swing-brake-<brake>-<event>`, never the cycle-id pattern), `type`
  ("swing_brake"), `brake` (`s15`: the 30-day net P&L brake; `canary`: the Skeptic canary pause),
  `event` (engaged | lifted), `cause` (a fixed code, engaged rows only) and `reason` (the operator's
  words, lifted rows only, checked publishable when typed: no amount, number run, id, link, path or
  e-mail). No P&L figure, no weight, no size, no symbol, no price: the brake's number stays private.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import Field

from council.publish.journal import JOURNAL
from council.publish.public_models import PublicModel

SWING_BRAKE_PATH = f"{JOURNAL}/ops/swing_brake.jsonl"
SWING_BRAKE_ID_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{4}Z-swing-brake-(?:s15|canary)-(?:engaged|lifted)$"
BRAKE_CAUSES = ("s15_net_loss", "missed_canaries", "pass_rate_alarms")
_ID = re.compile(SWING_BRAKE_ID_PATTERN)


class PublicSwingBrakeRow(PublicModel):
    """The public row of one swing pause event (codes and the operator's checked words only)."""

    id: Annotated[str, Field(pattern=SWING_BRAKE_ID_PATTERN)]
    type: Literal["swing_brake"] = "swing_brake"
    brake: Literal["s15", "canary"]
    event: Literal["engaged", "lifted"]
    cause: Literal["s15_net_loss", "missed_canaries", "pass_rate_alarms"] | None = None
    reason: Annotated[str, Field(max_length=180)] | None = None


def row_id(minute: str, brake: str, event: str) -> str:
    """`minute` = `YYYY-MM-DDTHHMMZ`."""
    return f"{minute}-swing-brake-{brake}-{event}"


def is_swing_brake_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def swing_brake_files(existing: bytes | None, rows: Iterable[PublicSwingBrakeRow]) -> dict[str, bytes]:
    """Append or replace rows by id (idempotent; sorted by id)."""
    by_id: dict[str, str] = {}
    for raw in (existing or b"").decode().splitlines():
        if raw.strip():
            by_id[str(json.loads(raw)["id"])] = raw.strip()
    for row in rows:
        by_id[row.id] = json.dumps(row.model_dump(mode="json", exclude_none=True), sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False)
    return {SWING_BRAKE_PATH: "".join(by_id[k] + "\n" for k in sorted(by_id)).encode()}
