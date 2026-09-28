"""Evidence for `council ops record-dress` (gate O5, m5-readiness §7.3).

After the human dress rehearsal (`ops/rehearse-onboarding.sh`) the script calls the guarded
`council-op ops record-dress --sandbox <sandbox state dir>` from the operator's own terminal. This
module checks, read-only, that the sandbox really walked token day: it is a marked sandbox (never
the real state dir), `keys verify` recorded K1 green there, at least one smoke ticket and one
council decision completed on the fake broker. Only codes leave this module; the only file written
outside the sandbox is `readiness/dress.json` (by the CLI, through `readiness.write_record`).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

LEDGER_FILE = "ledger.sqlite3"
DONE = ("completed", "completed_partial")


def _count(con: sqlite3.Connection, sql: str) -> int:
    row = con.execute(sql, DONE).fetchone()
    return int(row[0]) if row else 0


def evidence(sandbox_state: Path) -> tuple[dict[str, int], list[str]]:
    """({"smoke": n, "council": n}, problems). Empty problems = the dress rehearsal passed."""
    from council.operator.release import is_marked_sandbox

    problems: list[str] = []
    counts = {"smoke": 0, "council": 0}
    root = sandbox_state.expanduser()
    if not is_marked_sandbox(root):
        return counts, ["not_a_marked_sandbox"]
    try:
        keys = json.loads((root / "readiness" / "keys.json").read_text())
        k1 = keys.get("gates", {}).get("K1", {}).get("state")
    except (OSError, ValueError, AttributeError):
        k1 = None
    if k1 != "green":
        problems.append("keys_verify_not_green")
    ledger = root / LEDGER_FILE
    if not ledger.is_file():
        return counts, [*problems, "no_sandbox_ledger"]
    try:
        con = sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)
        try:
            counts["smoke"] = _count(con, "SELECT count(*) FROM decisions WHERE kind = 'smoke' AND state IN (?, ?)")
            counts["council"] = _count(con, "SELECT count(*) FROM decisions WHERE kind != 'smoke' AND state IN (?, ?)")
        finally:
            con.close()
    except sqlite3.Error:
        return counts, [*problems, "sandbox_ledger_unreadable"]
    if counts["smoke"] < 1:
        problems.append("no_completed_smoke_ticket")
    if counts["council"] < 1:
        problems.append("no_completed_council_decision")
    return counts, problems
