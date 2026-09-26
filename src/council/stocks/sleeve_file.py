"""`policy/stock-sleeve.yaml`: read, write, diff, the two-phase retirement, and validation through a
temporary policy directory (design §3.2, §3.5, D16).

Rules:
- The writer quotes every string (a ticker such as ON, YES or NO can never become a YAML boolean)
  and checks that its output reads back to the same model; the reader is the policy loader's own
  strict schema (`council.policy.StockSleeveFile`).
- Two-phase retirement: a company in the previous sleeve that the new rank neither selects nor
  shortlists becomes `retiring` (target 0) and keeps its line id. It moves to the append-only
  retired registry (keyed by CIK, never a line) only when it is FLAT in a fresh READ snapshot and no
  in-flight decision touches it; "unknown" (no snapshot, an unresolved instrument) is never flat.
  A company is re-keyed by CIK: a new ticker for a live company keeps its history under `aliases`.
  A registry company that comes back drops its registry row. A new line may reuse a registry
  ticker of ANOTHER company (a recycled ticker) but never the id of a live or retiring line.
- Nothing here writes into `policy/`: `validate` copies a committed policy tree and the proposed
  files into a temporary directory under the caller's (private) work directory, loads it through
  every `Universe` validator and the adoption checks, and deletes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml

from council import clock
from council.policy import (
    SLEEVE_FILE,
    STOCK_RANK_FILE,
    Policy,
    RetiredStock,
    SleeveLine,
    StockSleeveFile,
)

SLEEVE_VERSION = 1
ANCHORS = ("03-20", "05-20", "08-20", "11-20")      # the spec's rebalance anchors (checked by a test)
TAG_PREFIX = "stocks-"
IN_FLIGHT_STATES = ("awaiting_publication", "proposed", "approved", "executing", "waiting_for_market",
                    "execution_unknown", "blocked")
GIT_TIMEOUT_S = 60
FlatFn = Callable[[SleeveLine], bool | None]


class ProposalError(ValueError):
    """The rank's result cannot be written as a sleeve file (e.g. a ticker two companies claim)."""


# ------------------------------------------------------------------------------------ calendar


def quarter_of(day: date) -> str:
    """The sleeve quarter of a rank date: its calendar quarter ("2026-11-20" -> "2026Q4")."""
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def us_session(day: date) -> bool:
    return day.weekday() < 5 and day not in clock.US_HOLIDAYS


def anchor_dates(year: int, anchors: Sequence[str] = ANCHORS) -> list[date]:
    """D for each anchor of `year`: the first US trading session on or after it."""
    out = []
    for mmdd in anchors:
        month, dom = (int(x) for x in mmdd.split("-"))
        day = date(year, month, dom)
        while not us_session(day):
            day += timedelta(days=1)
        out.append(day)
    return out


def latest_anchor(on_or_before: date) -> date:
    days = [d for y in (on_or_before.year - 1, on_or_before.year) for d in anchor_dates(y) if d <= on_or_before]
    return max(days)


def next_anchor(after: date) -> date:
    days = [d for y in (after.year, after.year + 1) for d in anchor_dates(y) if d > after]
    return min(days)


# ------------------------------------------------------------------------------------ identity


def signal_ticker(line_id: str) -> str:
    """The history-source ticker of a line id: the class separator written "-" (BRK_B -> BRK-B)."""
    return line_id.replace("_", "-")


def broker_symbol_guess(line_id: str) -> str:
    """The eToro symbol asked for at eligibility: the class separator written "." (BRK_B -> BRK.B).
    The sleeve file records the symbol eligibility RETURNED; this guess only stands when the rank
    ran without eligibility (then the line is unchecked and live runs refuse it)."""
    return line_id.replace("_", ".")


def cik10(cik: int | str) -> str:
    value = int(cik)
    if not 0 < value < 10**10:
        raise ValueError(f"bad CIK {cik!r}")
    return f"{value:010d}"


# ------------------------------------------------------------------------------------ read / write


def loads(text: str) -> StockSleeveFile:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{SLEEVE_FILE} must contain a mapping")
    return StockSleeveFile.model_validate(data)


def load(path: Path) -> StockSleeveFile:
    return loads(Path(path).read_text())


def _q(value: str) -> str:
    return json.dumps(str(value))           # a JSON string is a valid double-quoted YAML scalar


def _stamp(value: datetime | None) -> str:
    return "null" if value is None else _q(value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _flow(values: Iterable[str]) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def dump(sleeve: StockSleeveFile, *, comment: str = "") -> str:
    """The sleeve as YAML with every string quoted. Raises if the text does not read back equal."""
    out = [f"# {c}".rstrip() for c in comment.splitlines()]
    out += [
        f"version: {sleeve.version}",
        f"quarter: {_q(sleeve.quarter)}",
        f"rank_asof: {_q(sleeve.rank_asof.isoformat())}",
        f"rank_config_sha256: {_q(sleeve.rank_config_sha256)}",
        f"sleeve_weight: {sleeve.sleeve_weight!r}",
        f"names_target: {sleeve.names_target}",
        "lines:" if sleeve.lines else "lines: []",
    ]
    for row in sleeve.lines:
        out += [
            f"  - symbol: {_q(row.symbol)}",
            f"    name: {_q(row.name)}",
            f"    role: {_q(row.role)}",
            f"    sector: {_q(row.sector)}",
            f"    cik: {_q(row.cik)}",
            f"    rank: {'null' if row.rank is None else row.rank}",
            f"    signal_ticker: {_q(row.signal_ticker)}",
            f"    etoro_symbol: {_q(row.etoro_symbol)}",
            f"    eligibility_checked_at: {_stamp(row.eligibility_checked_at)}",
            f"    credited: {'null' if row.credited is None else _q(row.credited)}",
            f"    aliases: {_flow(row.aliases)}",
        ]
    out.append("retired:" if sleeve.retired else "retired: []")
    for r in sleeve.retired:
        out.append(f"  - {{cik: {_q(r.cik)}, symbol: {_q(r.symbol)}, name: {_q(r.name)}, sector: {_q(r.sector)}, "
                   f"from: {_q(r.from_)}, to: {_q(r.to)}, vehicles: {_flow(r.vehicles)}}}")
    text = "\n".join(out) + "\n"
    if loads(text) != sleeve:
        raise ProposalError("the sleeve writer's output does not read back to the same file")
    return text


def whole_seconds(value: datetime | None) -> datetime | None:
    """A timestamp as the sleeve file writes it: UTC, whole seconds."""
    return None if value is None else value.astimezone(UTC).replace(microsecond=0)


def _line_fields(row: SleeveLine | Mapping[str, object]) -> dict[str, object]:
    out = dict(row.model_dump(by_alias=True) if isinstance(row, SleeveLine) else row)
    stamp = out.get("eligibility_checked_at")
    if isinstance(stamp, datetime):
        out["eligibility_checked_at"] = whole_seconds(stamp)
    return out


def sleeve_model(**fields: object) -> StockSleeveFile:
    """A StockSleeveFile from plain fields (`lines` / `retired` may hold models), fully validated.
    Eligibility stamps are truncated to whole UTC seconds (what the writer can express)."""
    lines = [_line_fields(r) for r in fields.pop("lines", [])]  # type: ignore[union-attr]
    retired = [r.model_dump(by_alias=True) if isinstance(r, RetiredStock) else r
               for r in fields.pop("retired", [])]  # type: ignore[union-attr]
    return StockSleeveFile.model_validate({"version": SLEEVE_VERSION, **fields, "lines": lines, "retired": retired})


def replace_lines(sleeve: StockSleeveFile, lines: Sequence[SleeveLine],
                  retired: Sequence[RetiredStock] | None = None) -> StockSleeveFile:
    """The same header with new lines (and registry), re-validated."""
    head = sleeve.model_dump(by_alias=True, exclude={"lines", "retired", "version"})
    return sleeve_model(**head, lines=list(lines), retired=list(sleeve.retired if retired is None else retired))


# ------------------------------------------------------------------------------------ building


@dataclass(frozen=True)
class NewLine:
    """One name the rank chose (after eligibility replacements)."""

    symbol: str
    name: str
    role: Literal["selected", "shortlist"]
    sector: str
    cik: str
    rank: int | None
    signal_ticker: str
    etoro_symbol: str
    eligibility_checked_at: datetime | None = None


@dataclass(frozen=True)
class SleeveDiff:
    """What changed between two sleeve files (line ids; companies matched by CIK)."""

    selected_in: tuple[str, ...] = ()
    selected_out: tuple[str, ...] = ()
    new_lines: tuple[str, ...] = ()         # companies not in the previous file at all
    returning: tuple[str, ...] = ()         # registry companies back (their registry row dropped)
    revived: tuple[str, ...] = ()           # retiring before, selected or shortlisted now
    retiring: tuple[str, ...] = ()          # newly retiring (target 0 until flat)
    still_retiring: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()            # moved to the retired registry
    rekeyed: tuple[tuple[str, str], ...] = ()   # (old id, new id) of the same company
    notes: tuple[str, ...] = field(default=())

    def summary(self) -> str:
        return (f"{len(self.selected_in)} in, {len(self.selected_out)} out, {len(self.retiring)} retiring, "
                f"{len(self.pruned)} pruned")

    def lines(self) -> list[str]:
        """Human-readable diff lines for the terminal."""
        out = []
        for label, items in (("selected in", self.selected_in), ("selected out", self.selected_out),
                             ("new lines", self.new_lines), ("returning", self.returning),
                             ("revived", self.revived), ("retiring", self.retiring),
                             ("still retiring", self.still_retiring), ("pruned to the registry", self.pruned)):
            if items:
                out.append(f"{label}: {', '.join(items)}")
        out += [f"re-keyed: {old} -> {new} (old id kept as an alias)" for old, new in self.rekeyed]
        out += list(self.notes)
        return out or ["no change"]


def _registry_row(line: SleeveLine, previous_quarter: str, first_quarter: Mapping[str, str]) -> RetiredStock:
    start = min(first_quarter.get(line.cik, previous_quarter), previous_quarter)
    return RetiredStock.model_validate({"cik": line.cik, "symbol": line.symbol, "name": line.name,
                                        "sector": line.sector, "from": start, "to": previous_quarter,
                                        "vehicles": [line.etoro_symbol]})


def _prunable(line: SleeveLine, flat: FlatFn | None, touched: Iterable[str]) -> bool:
    busy = set(touched)
    names = {line.symbol, *line.aliases}
    return flat is not None and flat(line) is True and not (names & busy)


def _order(rows: Iterable[SleeveLine]) -> list[SleeveLine]:
    group = {"selected": 0, "shortlist": 1, "retiring": 2}
    big = 10**9
    return sorted(rows, key=lambda r: (group[r.role], r.rank if r.rank is not None else big, r.symbol))


def build_sleeve(
    previous: StockSleeveFile | None,
    chosen: Sequence[NewLine],
    *,
    quarter: str,
    rank_asof: date,
    rank_config_sha256: str,
    sleeve_weight: float,
    names_target: int,
    flat: FlatFn | None = None,
    touched: Iterable[str] = (),
    first_quarter: Mapping[str, str] | None = None,
) -> tuple[StockSleeveFile, SleeveDiff]:
    """The next quarter's sleeve from the previous committed one and the rank's chosen names.

    `flat(line)` answers from a fresh READ snapshot (True flat, False held, None unknown); without it
    nothing is pruned. `touched` names the lines of in-flight decisions. `first_quarter` maps a CIK
    to the first quarter it was live (the registry's `from`)."""
    first = dict(first_quarter or {})
    prev_lines = list(previous.lines) if previous is not None else []
    prev_by_cik = {row.cik: row for row in prev_lines}
    registry = list(previous.retired) if previous is not None else []
    registry_ciks = {r.cik for r in registry}
    seen: set[str] = set()
    rows: list[SleeveLine] = []
    d: dict[str, list] = {k: [] for k in ("selected_in", "selected_out", "new_lines", "returning", "revived",
                                         "retiring", "still_retiring", "pruned", "rekeyed")}
    for c in chosen:
        if c.cik in seen:
            raise ProposalError(f"CIK {c.cik} chosen twice")
        seen.add(c.cik)
        old = prev_by_cik.get(c.cik)
        aliases = list(old.aliases) if old is not None else []
        if old is not None and old.symbol != c.symbol:
            d["rekeyed"].append((old.symbol, c.symbol))
            aliases.append(old.symbol)
        aliases = list(dict.fromkeys(a for a in aliases if a != c.symbol))
        rows.append(SleeveLine.model_validate({
            "symbol": c.symbol, "name": c.name, "role": c.role, "sector": c.sector, "cik": c.cik, "rank": c.rank,
            "signal_ticker": c.signal_ticker, "etoro_symbol": c.etoro_symbol,
            "eligibility_checked_at": c.eligibility_checked_at, "credited": None, "aliases": aliases}))
        if old is None:
            d["new_lines"].append(c.symbol)
        elif old.role == "retiring":
            d["revived"].append(c.symbol)
        if c.cik in registry_ciks:
            d["returning"].append(c.symbol)
        if c.role == "selected" and (old is None or old.role != "selected"):
            d["selected_in"].append(c.symbol)
    registry = [r for r in registry if r.cik not in seen]
    in_use = {r.symbol for r in rows}
    for old in prev_lines:
        if old.cik in seen:
            continue
        if old.role == "selected":
            d["selected_out"].append(old.symbol)
        if _prunable(old, flat, touched):
            registry.append(_registry_row(old, previous.quarter, first))  # type: ignore[union-attr]
            d["pruned"].append(old.symbol)
            continue
        if old.symbol in in_use:
            raise ProposalError(f"line id {old.symbol}: a new line of another company takes the id of a line "
                                "that is still held; it must be flat and pruned first")
        rows.append(old.model_copy(update={"role": "retiring", "rank": None}))
        d["retiring" if old.role != "retiring" else "still_retiring"].append(old.symbol)
    sleeve = sleeve_model(quarter=quarter, rank_asof=rank_asof, rank_config_sha256=rank_config_sha256,
                          sleeve_weight=sleeve_weight, names_target=names_target, lines=_order(rows),
                          retired=registry)
    diff = SleeveDiff(**{k: tuple(v) for k, v in d.items()})
    return sleeve, diff


def prune(current: StockSleeveFile, *, flat: FlatFn, touched: Iterable[str] = (),
          first_quarter: Mapping[str, str] | None = None) -> tuple[StockSleeveFile, tuple[str, ...]]:
    """`council stocks prune`: the current quarter's file with every retiring line that is flat and
    untouched moved to the registry (`to` = this quarter). Other lines are unchanged."""
    first = dict(first_quarter or {})
    keep: list[SleeveLine] = []
    registry = list(current.retired)
    pruned: list[str] = []
    for line in current.lines:
        if line.role == "retiring" and _prunable(line, flat, touched):
            registry.append(_registry_row(line, current.quarter, first))
            pruned.append(line.symbol)
        else:
            keep.append(line)
    return replace_lines(current, keep, registry), tuple(pruned)


# ------------------------------------------------------------------------------------ flat / in flight


def flat_from_positions(positions: Iterable[object] | None, instrument_id: Callable[[str], int | None]) -> FlatFn:
    """`flat(line)` from a fresh READ snapshot's positions: False when a position is on the line's
    instrument (or carries its broker symbol or line id), True when none is and the line's
    instrument id is known, None (unknown, never flat) without a snapshot or an instrument id."""
    held = list(positions) if positions is not None else None

    def flat(line: SleeveLine) -> bool | None:
        if held is None:
            return None
        names = {line.symbol, line.etoro_symbol}
        iid = instrument_id(line.etoro_symbol)
        if any(getattr(p, "symbol", None) in names or (iid is not None and getattr(p, "instrument_id", None) == iid)
               for p in held):
            return False
        return True if iid is not None else None

    return flat


def in_flight_lines(ledger: object) -> set[str]:
    """Lines touched by a decision still in flight (IN_FLIGHT_STATES): its legs and its plan's legs."""
    out: set[str] = set()
    for d in ledger.decisions(states=IN_FLIGHT_STATES, limit=1000):  # type: ignore[attr-defined]
        out |= {leg.line for leg in ledger.legs(d.decision_id)}  # type: ignore[attr-defined]
        for leg in (d.plan or {}).get("legs", []) or []:
            if isinstance(leg, Mapping) and leg.get("line"):
                out.add(str(leg["line"]))
    return out


# ------------------------------------------------------------------------------------ hashing


def rank_config_sha256(files: Sequence[tuple[str, bytes]]) -> str:
    """sha256 over (name, bytes) of the rank's configuration files (stock-rank.yaml and the extra
    list), in the order given."""
    digest = hashlib.sha256()
    for name, data in files:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


# ------------------------------------------------------------------------------------ validation


@dataclass
class Validation:
    policy: Policy | None
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return self.policy is not None and not self.errors


def _copy_tree(src: Path, dest: Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise ProposalError(f"policy tree contains a link: {path.name}")
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def validate(base_dir: Path, files: Mapping[str, bytes], *, workdir: Path,
             overlay_dir: Path | None = None) -> Validation:
    """Load `base_dir` (a committed policy tree) + the top-level YAML files of `overlay_dir` + `files`
    ({top-level name: bytes}) from a temporary copy under `workdir`, through `Policy.load` with the
    sleeve, `invariants.check_policy` and the adoption checks (`stocks/adopted.py`). The copy is
    deleted afterwards; `base_dir` and `overlay_dir` are only read."""
    from council.invariants import InvariantViolation, check_policy
    from council.stocks import adopted

    errors: list[str] = []
    warnings: list[str] = []
    for name in files:
        if "/" in name or name.startswith(".") or not name.endswith(".yaml"):
            raise ProposalError(f"proposed file {name!r} is not a top-level policy YAML file")
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="policy-check-", dir=workdir) as tmp:
        root = Path(tmp) / "policy"
        root.mkdir()
        _copy_tree(base_dir, root)
        if overlay_dir is not None:
            for path in sorted(Path(overlay_dir).glob("*.yaml")):
                if path.name == SLEEVE_FILE:
                    raise ProposalError(f"the overlay may not carry {SLEEVE_FILE}: the rank writes it")
                shutil.copyfile(path, root / path.name)
        for name, data in files.items():
            (root / name).write_bytes(data)
        try:
            policy = Policy.load(root, include_sleeve=True)
        except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as exc:
            return Validation(None, [f"policy does not load: {exc}"], warnings)
        try:
            check_policy(policy)
        except InvariantViolation as exc:
            errors.append(f"invariant: {exc}")
        sleeve = policy.universe.stock_sleeve
        if sleeve is None or not policy.universe.stock_lines():
            errors.append(f"{SLEEVE_FILE} gives no stock lines")
        try:
            rule = adopted.load_adopted(root)
        except adopted.AdoptedRuleError as exc:
            errors.append(f"adoption record: {exc}")
        else:
            errors += adopted.stock_rank_errors(policy.stocks, rule)
            if sleeve is not None:
                if abs(sleeve.sleeve_weight - rule.sleeve_share) > 1e-12:
                    errors.append(f"sleeve_weight {sleeve.sleeve_weight} is not the adopted share {rule.sleeve_share}")
                if sleeve.names_target != rule.names:
                    errors.append(f"names_target {sleeve.names_target} is not the adopted N {rule.names}")
            ref = policy.reference.get("sleeve")
            if ref is None:
                warnings.append("reference.yaml has no sleeve: section yet (the go-live commit adds it)")
            else:
                errors += adopted.reference_sleeve_errors(ref, rule)
        from council.data.alpaca import alpaca_symbol

        for line in policy.universe.stock_lines():
            try:
                alpaca_symbol(line.signal.ticker)
            except ValueError:
                errors.append(f"line {line.symbol}: history ticker {line.signal.ticker!r} is not usable")
        if STOCK_RANK_FILE not in {p.name for p in root.glob("*.yaml")}:
            errors.append(f"{STOCK_RANK_FILE} is missing")
    return Validation(policy, errors, warnings)


# ------------------------------------------------------------------------------------ git (read-only)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, env=env,
                              timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_out(repo: Path, *args: str) -> str | None:
    done = _git(repo, *args)
    if done is None or done.returncode != 0:
        return None
    return done.stdout.decode().strip()


def committed_blob(repo: Path, rev: str) -> str | None:
    """Blob id of `policy/stock-sleeve.yaml` at `rev` (a commit or `refs/tags/<tag>`), or None."""
    return _git_out(repo, "rev-parse", "--verify", "-q", f"{rev}:policy/{SLEEVE_FILE}")


def tag_state(repo: Path, quarter: str) -> str:
    """"tagged" when HEAD's sleeve file is the blob at `stocks-<quarter>`; "untagged" when the tag
    exists with another blob; "no_tag" when there is no such tag; "no_sleeve" without a file."""
    head = committed_blob(repo, "HEAD")
    if head is None:
        return "no_sleeve"
    tagged = committed_blob(repo, f"refs/tags/{TAG_PREFIX}{quarter}")
    if tagged is None:
        return "no_tag"
    return "tagged" if tagged == head else "untagged"


def tagged_first_quarters(repo: Path) -> dict[str, str]:
    """{CIK: the earliest quarter whose `stocks-<quarter>` tag lists it as a line} (registry `from`)."""
    listed = _git_out(repo, "tag", "--list", f"{TAG_PREFIX}*")
    out: dict[str, str] = {}
    for tag in sorted((listed or "").split()):
        quarter = tag.removeprefix(TAG_PREFIX)
        text = _git_out(repo, "show", f"refs/tags/{tag}:policy/{SLEEVE_FILE}")
        if not text:
            continue
        try:
            sleeve = loads(text)
        except (ValueError, yaml.YAMLError):
            continue
        for line in sleeve.lines:
            if line.cik not in out or quarter < out[line.cik]:
                out[line.cik] = quarter
    return out
