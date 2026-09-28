"""The 7-day licensed scrub, extended to the swing-book rows (design swing-book.md rev 2, H9, SW-2b).

`council.operator.purge._scrub_ledger` filters a cycle's ledger record against the cycle's licensed
feed texts once its capture passes the cut-off. The swing rows that may copy feed text are keyed by
origin cycle: `swing_ideas.record_json` (an idea also matches every cycle that carried it forward),
`swing_events.reason` / `payload_json` and `paper_trades.record_json` (rows of that cycle, or of an
idea that matches it). Every string that copies a licensed text becomes the placeholder; ids,
numbers and states are kept. `swing_trades` hold no free text (schema v5) and are never touched, so
a closed trade stays immutable. Pure ledger code: nothing here imports a broker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from council.ledger.db import Ledger

Hits = Callable[[str], bool]


def _scrub(value: Any, hits: Hits, placeholder: str, count: list[int]) -> Any:
    if isinstance(value, str):
        if hits(value):
            count.append(1)
            return placeholder
        return value
    if isinstance(value, list):
        return [_scrub(v, hits, placeholder, count) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, hits, placeholder, count) for k, v in value.items()}
    return value


def _scrub_json(text: str | None, hits: Hits, placeholder: str, count: list[int]) -> str | None:
    if not text:
        return text
    before = len(count)
    scrubbed = _scrub(json.loads(text), hits, placeholder, count)
    return json.dumps(scrubbed, sort_keys=True, default=str) if len(count) > before else text


def scrub_swing_rows(ledger: Ledger, cycle_id: str, hits: Hits, placeholder: str, *,
                     dry_run: bool = False) -> int:
    """Replace every swing-row string of `cycle_id` that `hits`; returns the number replaced."""
    count: list[int] = []
    with ledger._tx() as conn:   # one transaction: the scrub is all-or-nothing
        ideas = [r["idea_id"] for r in conn.execute(
            """SELECT idea_id FROM swing_ideas WHERE origin_cycle = ?
               OR EXISTS (SELECT 1 FROM json_each(swing_ideas.carry_cycles_json) WHERE value = ?)""",
            (cycle_id, cycle_id)).fetchall()]
        marks = ",".join("?" * len(ideas)) or "NULL"
        for row in conn.execute(
                f"SELECT idea_id, record_json FROM swing_ideas WHERE idea_id IN ({marks})", ideas).fetchall():
            new = _scrub_json(row["record_json"], hits, placeholder, count)
            if new is not row["record_json"] and not dry_run:
                conn.execute("UPDATE swing_ideas SET record_json = ? WHERE idea_id = ?",
                             (new, row["idea_id"]))
        for row in conn.execute(
                f"""SELECT event_id, reason, payload_json FROM swing_events
                    WHERE origin_cycle = ? OR idea_id IN ({marks})""", (cycle_id, *ideas)).fetchall():
            reason = _scrub(row["reason"], hits, placeholder, count)
            payload = _scrub_json(row["payload_json"], hits, placeholder, count)
            if (reason is not row["reason"] or payload is not row["payload_json"]) and not dry_run:
                conn.execute("UPDATE swing_events SET reason = ?, payload_json = ? WHERE event_id = ?",
                             (reason, payload, row["event_id"]))
        for row in conn.execute(
                f"""SELECT paper_id, record_json FROM paper_trades
                    WHERE origin_cycle = ? OR idea_id IN ({marks})""", (cycle_id, *ideas)).fetchall():
            new = _scrub_json(row["record_json"], hits, placeholder, count)
            if new is not row["record_json"] and not dry_run:
                conn.execute("UPDATE paper_trades SET record_json = ? WHERE paper_id = ?",
                             (new, row["paper_id"]))
    return len(count)
