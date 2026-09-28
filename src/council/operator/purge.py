"""`council purge-licensed`: remove eToro Licensed Content from every private store.

Why: the broker's API terms let the operator use its feed and market data for their own account,
but not keep it: licensed payloads are kept at most `LICENSED_RETENTION_DAYS` (7) days, and all of
them must be deletable within 24 hours of a request (`--all` takes minutes). The public record
never holds licensed text (docs/data-rights.md), so only private stores are touched.

What each store gets (the registry below lists EVERY folder a state dir may hold; a test fails when
a new folder appears without an entry):
  - `licensed/calls/`  the broker-licensed item texts of a cycle's capture. Before the file is
    deleted, the cycle's model output (replies in `calls/` and `transcripts/`, and every string of
    the cycle's ledger record: card claims, arguments, reasons) is filtered against those texts
    (8-word runs, and whole short titles of 4-7 words with at least 20 characters of content
    words); an overlapping string is replaced by a placeholder. The capture in `calls/` keeps every
    salted commit and records `purged_at`.
  - `licensed/` (everything else: recorded fixtures, feed payloads), `broker_raw/` and any
    `licensed/` folder under `fixtures/`: files older than the cut-off are deleted.
  - `inputs-view/`: rendered private pages; deleted on every purge (they are rebuilt on demand).
  - nested state roots (`rehearsal/`, `dryrun/`) are purged the same way.
  - `backups/` hold the ledger only; after any ledger scrub (and on every `--all`) every backup
    is deleted and a fresh one taken (`backups_deleted`, `backups_taken`; M5-M).
A receipt with counts only is printed and written to `purge-receipts/` (never any content).
The unattended runner calls `maybe_daily_purge` every cycle: it purges on the first cycle of each
UTC day (no receipt for that day yet) with a cut-off one day under the retention, so no copy
outlives 7 days between two daily runs, and does nothing on later ones (best effort; a failure
becomes the flag `purge_error:<store>:<type>`). Nothing here imports the broker writer.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from council.deliberation.capture import (
    calls_path,
    load_inputs,
    write_private,
)
from council.models.inputs import CycleInputs, LicensedInputs
from council.publish.leakscan import LICENSED_NGRAM, LicensedMatcher

LICENSED_RETENTION_DAYS = 7          # the ceiling; invariants.LICENSED_RETENTION_DAYS may only be lower
REPLY_PLACEHOLDER = "[purged: overlapped licensed text]"
RECEIPTS_DIR = "purge-receipts"
NESTED_ROOTS = ("rehearsal", "dryrun")
NGRAM = LICENSED_NGRAM


def retention_days() -> int:
    """The retention in force: the invariant when it exists (stage-2 hook), never above 7."""
    try:
        from council import invariants

        value = int(getattr(invariants, "LICENSED_RETENTION_DAYS", LICENSED_RETENTION_DAYS))
    except Exception:
        value = LICENSED_RETENTION_DAYS
    return max(0, min(value, LICENSED_RETENTION_DAYS))


# ----------------------------------------------------------------------------- the text filter
# The purge scrub IS the publication gate's rule (the licensed-filter contract, M5-M): one class,
# so the leak scan refuses exactly what the purge would scrub.
LicensedFilter = LicensedMatcher


# ---------------------------------------------------------------------------------- the run
@dataclass
class PurgeReceipt:
    at: datetime
    mode: str                                  # "all" | "older_than"
    cutoff: datetime | None
    dry_run: bool
    counts: dict[str, int] = field(default_factory=dict)
    unregistered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def to_json(self) -> dict:
        return {
            "at": self.at.isoformat(), "mode": self.mode,
            "cutoff": self.cutoff.isoformat() if self.cutoff else None, "dry_run": self.dry_run,
            "counts": dict(sorted(self.counts.items())), "unregistered": sorted(self.unregistered),
            "errors": list(self.errors),
        }

    def summary(self) -> str:
        head = "purge-licensed" + (" (dry run)" if self.dry_run else "") + f": {self.mode}"
        if self.cutoff is not None:
            head += f", cut-off {self.cutoff:%Y-%m-%d %H:%M} UTC"
        rows = [head] + [f"  {k}: {v}" for k, v in sorted(self.counts.items())]
        if not self.counts:
            rows.append("  nothing to purge")
        rows += [f"  unregistered folder (check it holds no licensed content): {u}"
                 for u in sorted(self.unregistered)]
        rows += [f"  error: {e}" for e in self.errors]
        return "\n".join(rows)


@dataclass
class _Run:
    now: datetime
    cutoff: datetime | None                     # None = everything (--all)
    dry_run: bool
    receipt: PurgeReceipt
    scrubbed_roots: set[Path] = field(default_factory=set)   # roots whose ledger was scrubbed

    def due(self, when: datetime) -> bool:
        return self.cutoff is None or when < self.cutoff


@dataclass(frozen=True)
class Store:
    """One folder a state dir may hold, and what purge does with it."""

    name: str
    licensed: bool
    note: str
    handler: Callable[[_Run, Path], None] | None = None


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _delete_old_files(run: _Run, folder: Path, counter: str, *, skip: Sequence[Path] = ()) -> None:
    if not folder.is_dir():
        return
    skipped = [s.resolve() for s in skip]
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or any(s in path.resolve().parents for s in skipped):
            continue
        if run.due(_mtime(path)):
            run.receipt.add(counter)
            if not run.dry_run:
                path.unlink(missing_ok=True)


def _read_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def _filter_capture(run: _Run, root: Path, inputs: CycleInputs, flt: LicensedFilter | None) -> None:
    calls = []
    for call in inputs.calls:
        update: dict = {}
        if flt is not None:        # an orphan (texts already gone) cannot be filtered: left unmarked
            update["replies_filtered"] = True
            replies = [REPLY_PLACEHOLDER if flt.hits(r) else r for r in call.replies]
            n = sum(1 for a, b in zip(replies, call.replies, strict=True) if a != b)
            if n:
                run.receipt.add("capture_replies_filtered", n)
                update["replies"] = replies
            if call.sent_assistant and flt.hits(call.sent_assistant):
                run.receipt.add("capture_replies_filtered")
                update["sent_assistant"] = REPLY_PLACEHOLDER
            # the checker's errors, and the correction turn built from them, can echo the reply
            if call.correction and flt.hits(call.correction):
                run.receipt.add("capture_replies_filtered")
                update["correction"] = REPLY_PLACEHOLDER
            errors = [REPLY_PLACEHOLDER if flt.hits(e) else e for e in call.errors]
            if errors != call.errors:
                run.receipt.add("capture_replies_filtered",
                                sum(a != b for a, b in zip(errors, call.errors, strict=True)))
                update["errors"] = errors
            if call.error and flt.hits(call.error):
                run.receipt.add("capture_replies_filtered")
                update["error"] = REPLY_PLACEHOLDER
        calls.append(call.model_copy(update=update))
    sections = {}
    for key, sec in inputs.sections.items():
        held, items = list(sec.licensed), list(sec.items)
        if flt is not None:   # council text that copied a licensed text goes the same way
            for i, item in enumerate(items):
                if i not in held and item.text and flt.hits(item.text):
                    items[i] = item.model_copy(update={"text": ""})
                    held.append(i)
                    run.receipt.add("capture_items_filtered")
        if held and sec.purged_at is None:
            run.receipt.add("capture_sections_marked")
            sec = sec.model_copy(update={"items": items, "licensed": sorted(held),
                                         "purged_at": run.now})
        sections[key] = sec
    done = inputs.model_copy(update={"calls": calls, "sections": sections,
                                     "licensed_purged_at": run.now})
    if not run.dry_run:
        write_private(root, calls_path(root, inputs.cycle_id),
                      gzip.compress(done.model_dump_json().encode(), mtime=0))


def _filter_transcript(run: _Run, root: Path, cycle_id: str, flt: LicensedFilter) -> None:
    path = root / "transcripts" / f"{cycle_id}.json.gz"
    if not path.is_file():
        return
    data = json.loads(_read_gz(path))
    raw = data.get("raw") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return
    hits = [k for k, v in raw.items() if isinstance(v, str) and flt.hits(v)]
    if not hits:
        return
    run.receipt.add("transcript_replies_filtered", len(hits))
    if not run.dry_run:
        data["raw"] = {k: (REPLY_PLACEHOLDER if k in hits else v) for k, v in raw.items()}
        write_private(root, path, gzip.compress(json.dumps(data).encode(), mtime=0))


def _scrub(value: object, flt: LicensedFilter, hits: list[int]) -> object:
    if isinstance(value, str):
        if flt.hits(value):
            hits.append(1)
            return REPLY_PLACEHOLDER
        return value
    if isinstance(value, list):
        return [_scrub(v, flt, hits) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, flt, hits) for k, v in value.items()}
    return value


def _scrub_ledger(run: _Run, root: Path, cycle_id: str, flt: LicensedFilter) -> None:
    """Replace every string of the cycle's ledger record, and of the swing rows keyed by that
    origin cycle (`council.ledger.purge`, SW-2b), that copies a licensed text."""
    from council.ledger.db import LEDGER_FILE, Ledger
    from council.ledger.purge import scrub_swing_rows

    path = root / LEDGER_FILE
    if not path.is_file():
        return
    ledger = Ledger(path)
    swing_hits = scrub_swing_rows(ledger, cycle_id, flt.hits, REPLY_PLACEHOLDER, dry_run=run.dry_run)
    record = ledger.get_cycle(cycle_id)
    hits: list[int] = []
    scrubbed: object = None
    if record is not None:
        keep = {k: record[k] for k in ("cycle_id", "slot", "status") if k in record}
        scrubbed = _scrub(record, flt, hits)
        if hits and not run.dry_run and isinstance(scrubbed, dict):
            ledger.record_cycle({**scrubbed, **keep}, now=run.now)
    total = len(hits) + swing_hits
    if not total:
        return
    run.receipt.add("ledger_strings_scrubbed", total)
    run.scrubbed_roots.add(root)
    if not run.dry_run:
        _compact(path)


def _compact(path: Path) -> None:
    """Rewrite the ledger file so no free page or WAL frame keeps the replaced strings."""
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _purge_captures(run: _Run, root: Path) -> None:
    """licensed/calls/: filter the cycle's replies against its licensed texts, mark the capture,
    then delete the licensed file."""
    folder = root / "licensed" / "calls"
    held: set[str] = set()
    if folder.is_dir():
        for path in sorted(folder.rglob("*.json.gz")):
            try:
                lic = LicensedInputs.model_validate_json(_read_gz(path))
            except Exception as exc:   # unreadable: it still goes once it is past the cut-off
                run.receipt.errors.append(f"licensed-calls: {type(exc).__name__}")
                if run.due(_mtime(path)):
                    run.receipt.add("licensed_capture_files_deleted")
                    if not run.dry_run:
                        path.unlink(missing_ok=True)
                continue
            held.add(lic.cycle_id)
            if not run.due(lic.captured_at):
                continue
            flt = LicensedFilter(t for texts in lic.texts.values() for t in texts.values())
            try:
                try:
                    inputs = load_inputs(root, lic.cycle_id)
                except FileNotFoundError:
                    inputs = None
                if inputs is not None:
                    _filter_capture(run, root, inputs, flt)
                _filter_transcript(run, root, lic.cycle_id, flt)
                _scrub_ledger(run, root, lic.cycle_id, flt)
            except Exception as exc:
                # retention wins: the licensed file still goes; the error tells the operator that
                # this cycle's model output was not (fully) filtered
                run.receipt.errors.append(f"filter: {type(exc).__name__}")
            run.receipt.add("licensed_capture_files_deleted")
            if not run.dry_run:
                path.unlink(missing_ok=True)
    # captures whose licensed file is already gone (deleted by another sweeper) are marked too
    calls_root = root / "calls"
    if calls_root.is_dir():
        for path in sorted(calls_root.rglob("*.json.gz")):
            try:
                inputs = CycleInputs.model_validate_json(_read_gz(path))
                if (inputs.licensed_items and inputs.licensed_purged_at is None
                        and inputs.cycle_id not in held and run.due(inputs.captured_at)):
                    run.receipt.add("capture_orphans_marked")
                    _filter_capture(run, root, inputs, None)
            except Exception as exc:
                run.receipt.errors.append(f"calls: {type(exc).__name__}")


def _purge_licensed_tree(run: _Run, root: Path) -> None:
    _purge_captures(run, root)
    _delete_old_files(run, root / "licensed", "licensed_files_deleted",
                      skip=[root / "licensed" / "calls"])


def _purge_broker_raw(run: _Run, root: Path) -> None:
    _delete_old_files(run, root / "broker_raw", "broker_raw_files_deleted")


def _purge_fixtures(run: _Run, root: Path) -> None:
    base = root / "fixtures"
    if not base.is_dir():
        return
    for folder in sorted(p for p in base.rglob("licensed") if p.is_dir()):
        _delete_old_files(run, folder, "fixture_files_deleted")


def _purge_views(run: _Run, root: Path) -> None:
    folder = root / "inputs-view"
    if not folder.is_dir():
        return
    n = sum(1 for p in folder.rglob("*") if p.is_file())
    if n:
        run.receipt.add("inputs_view_files_deleted", n)
    if not run.dry_run:
        shutil.rmtree(folder, ignore_errors=True)


def _captures_only(run: _Run, root: Path) -> None:
    """`calls/` is handled with `licensed/calls/` (replies are filtered before texts go)."""
    if not (root / "licensed").exists():
        _purge_captures(run, root)


STORES: tuple[Store, ...] = (
    Store("calls", False, "private captures; broker-licensed texts are held in licensed/calls",
          _captures_only),
    Store("licensed", True, "every eToro payload: capture texts, recorded fixtures, feed payloads",
          _purge_licensed_tree),
    Store("transcripts", False, "raw model replies; filtered against licensed texts at purge"),
    Store("inputs-view", True, "rendered private pages (may show licensed text)", _purge_views),
    Store("fixtures", True, "recorded payloads; only their licensed/ folders", _purge_fixtures),
    Store("broker_raw", True, "raw broker payloads", _purge_broker_raw),
    Store("cache", False, "data-vendor caches (Tiingo, Binance, FRED, SEC, Alpaca, Wikipedia); "
          "the broker feed is never cached"),
    Store("salts", False, "sealed public documents and their salts (allow-listed models only)"),
    Store("backups", False, "the ledger only (M5-E contract: never calls/, transcripts/, "
          "fixtures/, licensed/)"),
    Store("keys", False, "the install key"),
    Store("account", False, "the operator's private account figures (no broker payloads)"),
    Store("releases", False, "installed release checkouts"),
    Store("publisher-clone", False, "a clone of the public repository"),
    Store("backtests", False, "research outputs from vendor data"),
    Store("design", False, "private design notes"),
    Store(RECEIPTS_DIR, False, "purge receipts (counts only)"),
)
STORE_NAMES = frozenset(s.name for s in STORES) | frozenset(NESTED_ROOTS)


def _purge_root(run: _Run, root: Path, top: Path) -> None:
    for store in STORES:
        if store.handler is None:
            continue
        try:
            store.handler(run, root)
        except Exception as exc:  # one store's failure never hides the others
            run.receipt.errors.append(f"{store.name}: {type(exc).__name__}")
    if root.is_dir():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.name not in STORE_NAMES and not child.name.startswith("."):
                run.receipt.unregistered.append(str(child.relative_to(top)))
    if root == top:
        for name in NESTED_ROOTS:
            nested = root / name
            if nested.is_dir():
                _purge_root(run, nested, top)


def purge_licensed(
    state_dir: Path,
    *,
    now: datetime,
    older_than_days: int | None = None,
    purge_all: bool = False,
    dry_run: bool = False,
    write_receipt: bool = True,
) -> PurgeReceipt:
    """Purge licensed content older than the cut-off (default: the retention) or everything."""
    days = retention_days() if older_than_days is None else int(older_than_days)
    cutoff = None if purge_all else now - timedelta(days=days)
    receipt = PurgeReceipt(at=now, mode="all" if purge_all else "older_than", cutoff=cutoff,
                           dry_run=dry_run)
    root = Path(state_dir)
    run = _Run(now=now, cutoff=cutoff, dry_run=dry_run, receipt=receipt)
    _purge_root(run, root, root)
    if root.is_dir():                    # every state root, nested ones included, has its own backups
        for base in (root, *(root / n for n in NESTED_ROOTS)):
            if base.is_dir() and (purge_all or base in run.scrubbed_roots):
                _refresh_backups(receipt, base, now, dry_run=dry_run)
    if write_receipt and not dry_run and root.is_dir():
        name = f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
        write_private(root, root / RECEIPTS_DIR / name, json.dumps(receipt.to_json()).encode())
    return receipt


def _refresh_backups(receipt: PurgeReceipt, root: Path, now: datetime, *, dry_run: bool) -> None:
    """Backups copy the ledger, so one taken before a scrub still holds the replaced strings: take a
    fresh backup of the scrubbed ledger FIRST, then delete every older backup (so a failed fresh
    backup never leaves the root with none while the scrub stands, yet the stale copies still go).
    Called after a ledger scrub of this root and on every `--all`; a day whose ledger needed no
    scrub keeps its backups (the daily sweep never erases history)."""
    from council.ops import backup

    folder = backup.backup_dir(root)
    old = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
    if dry_run:
        if old:
            receipt.add("backups_deleted", len(old))
        return
    fresh: Path | None = None
    try:
        from council.operator.licensed import backup_check

        result = backup.backup_ledger(root, now=now, licensed_check=backup_check(root))
    except Exception as exc:  # the receipt says so; the stale copies are deleted all the same
        receipt.errors.append(f"backups: {type(exc).__name__}")
        result = None
    if result is not None:
        fresh = result.path
        receipt.add("backups_taken")
        if not result.licensed_free:
            receipt.errors.append("backups: licensed_text_in_fresh_backup")
    stale = [p for p in old if fresh is None or p.resolve() != fresh.resolve()]
    deleted = 0
    for path in stale:
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            receipt.errors.append(f"backups: {type(exc).__name__}")
    if deleted:
        receipt.add("backups_deleted", deleted)


def daily_cutoff_days() -> int:
    """The age the once-a-day purge deletes from: one day under the retention. The purge runs only
    on the first cycle of each UTC day, so a cut-off of the full retention would let text captured
    just after one day's purge live until the purge eight days later; one day less keeps every
    licensed copy under `LICENSED_RETENTION_DAYS` (7) days."""
    return max(0, retention_days() - 1)


def daily_purge(state_dir: Path, now: datetime) -> list[str]:
    """The cycle hook (first UTC cycle of the day): purge everything that would pass the retention
    before the next daily run; never raises. Returns flags for the cycle record."""
    try:
        receipt = purge_licensed(state_dir, now=now, older_than_days=daily_cutoff_days())
    except Exception as exc:
        return [f"purge_error:{type(exc).__name__}"]
    return [f"purge_error:{e.replace(': ', ':')}" for e in receipt.errors]


def daily_purge_due(state_dir: Path, now: datetime) -> bool:
    """True until a purge receipt dated `now`'s UTC day exists (a dry run writes none)."""
    folder = Path(state_dir) / RECEIPTS_DIR
    day = f"{now.astimezone(UTC):%Y%m%d}T"
    try:
        return not any(p.name.startswith(day) for p in folder.glob("*.json"))
    except OSError:
        return True


def maybe_daily_purge(state_dir: Path, now: datetime) -> list[str]:
    """The one call the cycle hook makes: the daily purge on the first cycle of each UTC day,
    nothing on later cycles. Never raises; returns flags for the cycle record."""
    try:
        due = daily_purge_due(state_dir, now)
    except Exception as exc:
        return [f"purge_error:{type(exc).__name__}"]
    return daily_purge(state_dir, now) if due else []


# ------------------------------------------------------------------------------------ command
def parse_days(text: str) -> int:
    match = re.fullmatch(r"\s*(\d{1,4})\s*d?\s*", text or "")
    if not match:
        raise typer.BadParameter("use a number of days, e.g. 7d")
    return int(match.group(1))


def purge_licensed_command(
    purge_all: Annotated[bool, typer.Option("--all", help="Purge every licensed payload now "
                                                          "(e.g. on eToro's deletion request).")] = False,
    older_than: Annotated[str, typer.Option("--older-than", help="Cut-off age, e.g. 7d.")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Count only; change nothing.")] = False,
) -> None:
    """Remove eToro Licensed Content from every private store (operator terminal only)."""
    from council import paths
    from council.operator.inputs_cli import require_operator

    require_operator()
    days = parse_days(older_than) if older_than else None
    if days is not None and days > LICENSED_RETENTION_DAYS:
        raise typer.BadParameter(f"licensed content may not be kept longer than {LICENSED_RETENTION_DAYS} days")
    receipt = purge_licensed(paths.state_dir(), now=datetime.now(UTC), older_than_days=days,
                             purge_all=purge_all, dry_run=dry_run)
    typer.echo(receipt.summary())
    if receipt.errors:
        raise typer.Exit(1)
