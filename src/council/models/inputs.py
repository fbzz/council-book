"""PRIVATE record of what every model call read (transparency-v2 §2.2). Never published.

Stored gzipped under the state dir by `council.deliberation.capture`:
  - `calls/<yyyy>/<mm>/<cycle>.json.gz`: `CycleInputs`. Sections are stored once and shared by the
    calls that read them; a section keeps its runs and items, so the exact text is rebuilt by
    joining them. Items licensed from the broker (`licence == "broker_licensed"`) are stored with
    an empty text here; their text lives only in the licensed file.
  - `licensed/calls/<yyyy>/<mm>/<cycle>.json.gz`: `LicensedInputs`, the broker-licensed item texts,
    kept at most 7 days (`council purge-licensed`); after the purge `purged_at` is set and the
    salted commits stay, so the input can be attested but no longer rebuilt.
Hashes: `input_hash = sha256(system NUL user)` equals the ledger's `RoleCall.input_hash`;
`input_commit = sha256(salt || system NUL user)` and each section's `commit =
sha256(salt || text)` use 32 random bytes per call / section, so a masked value cannot be brute
forced from a published commit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from council.deliberation.segments import Item, Run
from council.models.common import Strict

INPUTS_SCHEMA = "council-book/private-inputs/v1"
LICENSED_SCHEMA = "council-book/private-licensed-inputs/v1"
CallInputStatus = Literal["sent", "ok", "parse_fail", "timeout", "transport", "cached", "skipped",
                          "invalid"]


class PrivateSection(Strict):
    key: str
    kind: str
    runs: list[Run]
    items: list[Item]
    licensed: list[int] = Field(default_factory=list)   # item indices whose text is held apart
    sha256: str                                          # of the exact section text
    salt: str
    commit: str                                          # sha256(salt || exact text)
    purged_at: datetime | None = None                    # licensed texts removed


class CallInput(Strict):
    call_key: str                          # "role:replicate:attempt", e.g. "bear:0:0", "pm:2:0"
    role: str
    replicate: int = 0
    attempt: int = 0
    seed: int
    num_predict: int
    prompt_id: str
    prompt_sha: str
    ctx: dict[str, Any]                    # prompt_context(policy): public policy numbers
    system: str
    sections: list[str]                    # section keys, in order; the user text is their join
    input_hash: str                        # sha256(system NUL user) == RoleCall.input_hash
    salt: str
    input_commit: str                      # sha256(salt || system NUL user)
    status: CallInputStatus = "sent"       # "sent" until the gateway answers (a cancelled call stays)
    error: str = ""
    errors: list[str] = Field(default_factory=list)   # the checker's errors on the first reply
    correction: str = ""                   # the exact correction message sent back
    sent_assistant: str = ""               # the first reply AS SENT back (truncated to 8000 chars)
    replies: list[str] = Field(default_factory=list)  # every raw reply, in order
    retries: list[str] = Field(default_factory=list)  # failed HTTP attempts that resent the same messages
    messages_commit: str = ""              # sha256(salt || canonical JSON of the final message list)
    replies_filtered: bool = False         # replies were filtered against licensed texts at purge


class CycleInputs(Strict):
    schema_id: Literal["council-book/private-inputs/v1"] = INPUTS_SCHEMA
    cycle_id: str
    captured_at: datetime
    sections: dict[str, PrivateSection] = Field(default_factory=dict)
    calls: list[CallInput] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    licensed_items: int = 0                # broker-licensed item texts held in the licensed file
    licensed_purged_at: datetime | None = None


class LicensedInputs(Strict):
    schema_id: Literal["council-book/private-licensed-inputs/v1"] = LICENSED_SCHEMA
    cycle_id: str
    captured_at: datetime
    texts: dict[str, dict[str, str]] = Field(default_factory=dict)   # section key -> item index -> text
