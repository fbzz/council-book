"""Runtime wiring shared by `cycle`, `watch` and the CLI: injectable context, data sources, cost
quotes from policy floors, the material-change fingerprint, the single-instance lock, and the live
policy snapshot (live cycles load policy from git HEAD, never from the working tree).

Nothing here can place an order. The broker source exposes READ methods only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import pandas as pd

from council import paths
from council.clock import utcnow
from council.models.broker import CostQuote, EligibilityRow
from council.models.cards import EvidenceCard
from council.models.facts import EventItem, Fact, FactPack, NewsItem
from council.policy import SLEEVE_FILE, UNIVERSE_FILE, LineSpec, Policy
from council.settings import Settings

MIN_FREE_BYTES = 3 * 1024**3
POLICY_SNAPSHOTS = "policy-snapshots"
LAST_SNAPSHOT_FILE = "last.json"             # policy-snapshots/last.json: the last verified snapshot
SLEEVE_POLICY_UNTAGGED = "sleeve_policy_untagged"
POLICY_SNAPSHOT_UNAVAILABLE = "policy_snapshot_unavailable"
GIT_TIMEOUT_S = 60
BlockerScope = Literal["all", "satellite"]
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True)
class PolicyBlocker:
    """A blocker found while loading policy. `scope="satellite"` holds only the stock sleeve (the core
    keeps running); `"all"` holds every line."""

    code: str
    scope: BlockerScope
    detail: str = ""


class BrokerRead(Protocol):
    """The read-only broker surface the runner uses (implemented by EtoroReadClient)."""

    def pnl(self) -> dict[str, Any]: ...
    def eligibility(self, symbols: Sequence[str] | None = None,
                    instrument_ids: Sequence[int] | None = None) -> list[EligibilityRow]: ...
    def rates(self, ids: Sequence[int]) -> dict[str, Any]: ...
    def feeds_news(self, take: int = 50, offset: int = 0) -> dict[str, Any]: ...


HistoryFn = Callable[[datetime], tuple[dict[str, pd.DataFrame], list[str]]]
MacroFn = Callable[[datetime], tuple[dict[str, pd.Series], list[str]]]
EventsFn = Callable[[datetime, datetime], tuple[list[EventItem], list[str]]]
NewsFn = Callable[[datetime], list[NewsItem]]
FundamentalsFn = Callable[[datetime], tuple[list[Fact], list[str]]]


@dataclass
class Sources:
    """Where a cycle's inputs come from. Every function receives the SLOT, never the wall clock."""

    history: HistoryFn
    events: EventsFn
    macro: MacroFn | None = None
    news: NewsFn | None = None
    broker: BrokerRead | None = None          # None = AWAITING ACCOUNT (no snapshot, no plan)
    fundamentals: FundamentalsFn | None = None   # stock lines' F:<line>:<field> facts (SEC)
    swing: Any = None                         # swing.sources.SwingSources (None: swing_sources_missing)


@dataclass
class CycleContext:
    policy: Policy
    settings: Settings
    ledger: Any                               # council.ledger.db.Ledger
    gateway: Any                              # council.llm.gateway.Gateway (real or stub)
    registry: Any                             # council.llm.prompts.PromptRegistry
    sources: Sources
    publisher: Any | None = None              # council.publish.gitops.Publisher
    notifier: Any | None = None               # council.operator.notify.Notifier
    clock: Callable[[], datetime] = utcnow
    state_dir: Path = field(default_factory=paths.state_dir)
    budget_s: float = 25 * 60
    run_single_agent: bool = True
    canaries: tuple[str, ...] = ()
    code_commit: str = ""
    policy_commit: str = ""                   # the commit a live policy snapshot was taken from
    policy_blockers: tuple[PolicyBlocker, ...] = ()   # e.g. sleeve_policy_untagged (satellite)
    # `council cycle --paper --ideas N` only (cli.paper_context): a WIDE paper swing slot of N ideas.
    # The cycle refuses it on any context that is not a paper run (cycle.swing_wide_of).
    swing_wide: int | None = None
    # `council cycle --paper --trace-all` only: every idea with a fact card goes through every swing
    # stage (the real outcome is recorded beside it). Refused off paper (cycle.swing_trace_of).
    swing_trace_all: bool = False
    # `council cycle --paper --publish` only: the repo dir the paper run's PUBLIC record is written
    # into (journal/paper/...; never committed or pushed here). Refused off paper (cycle.paper_publish_of).
    paper_publish_dir: Path | None = None


# --------------------------------------------------------------------------------------- lock
class LockBusy(RuntimeError):
    pass


@contextmanager
def instance_lock(name: str = "council.lock", state_dir: Path | None = None) -> Iterator[Path]:
    """Non-blocking exclusive lock shared by cycle and watch: only one may create proposals."""
    root = state_dir or paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    fh = path.open("a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(str(path)) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def disk_ok(path: Path, min_free: int = MIN_FREE_BYTES) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free >= min_free


# ---------------------------------------------------------------------- live policy snapshot
class PolicySnapshotError(RuntimeError):
    """The committed policy could not be materialised. It never falls back to the working tree: a
    live run continues only on the last VERIFIED snapshot, with every line held
    (`last_verified_snapshot`), and refuses to start when there is none."""


@dataclass(frozen=True)
class PolicySnapshot:
    policy: Policy
    commit: str                               # the commit HEAD named when the snapshot was taken
    tree: str                                 # git tree id of `<commit>:policy` (the directory name)
    directory: Path                           # state_dir/policy-snapshots/<tree>
    blockers: tuple[PolicyBlocker, ...] = ()
    fallback: bool = False                    # True: git failed; this is the last verified snapshot


def _sleeve_live(include_sleeve: bool | None) -> bool:
    """`None` means the hard-coded go-live switch `invariants.STOCK_SLEEVE_LIVE` (read at call time)."""
    if include_sleeve is not None:
        return include_sleeve
    from council import invariants

    return bool(invariants.STOCK_SLEEVE_LIVE)


def _git_env() -> dict[str, str]:
    """The environment without any `GIT_*` variable: an inherited `GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_INDEX_FILE` or `GIT_OBJECT_DIRECTORY` would make `git -C <repo>` read ANOTHER repository."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Read-only git (`rev-parse`, `ls-tree`, `cat-file`). A missing or broken binary, or a hang, is
    a `PolicySnapshotError`, never an unhandled OSError."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False,
                              env=_git_env(), timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicySnapshotError(f"live policy snapshot: cannot run `git {' '.join(args)}`: {exc}") from exc


def _git_text(repo: Path, *args: str) -> str:
    done = _git(repo, *args)
    if done.returncode != 0:
        err = done.stderr.decode(errors="replace").strip() or "not found"
        raise PolicySnapshotError(f"live policy snapshot: `git {' '.join(args)}` failed in {repo}: {err}")
    return done.stdout.decode().strip()


def _blob_id(data: bytes, oid_len: int) -> str:
    """Git's object id of a blob with these bytes (SHA-1 repositories, or SHA-256 ones)."""
    digest = hashlib.sha1 if oid_len == 40 else hashlib.sha256
    return digest(b"blob %d\0" % len(data) + data).hexdigest()


def _policy_blobs(repo: Path, tree: str) -> dict[str, str]:
    """{relative path: blob id} of every file under the policy tree. Only regular files: a symlink
    or submodule under policy/ is refused, and so is a tree without `universe.yaml`."""
    out: dict[str, str] = {}
    listed = _git(repo, "ls-tree", "--full-tree", "-r", "-z", tree)
    if listed.returncode != 0:
        raise PolicySnapshotError(f"live policy snapshot: cannot list policy tree {tree}")
    for entry in filter(None, listed.stdout.decode().split("\0")):
        meta, rel = entry.split("\t", 1)
        mode, kind, oid = meta.split()
        parts = PurePosixPath(rel).parts
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PolicySnapshotError(f"live policy snapshot: policy/{rel} is not a regular file ({mode} {kind})")
        if not parts or PurePosixPath(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
            raise PolicySnapshotError(f"live policy snapshot: unsafe path policy/{rel}")
        out[rel] = oid
    if UNIVERSE_FILE not in out:
        raise PolicySnapshotError(f"live policy snapshot: policy tree {tree} has no {UNIVERSE_FILE}")
    return out


def _snapshot_matches(directory: Path, blobs: Mapping[str, str], oid_len: int) -> bool:
    """The directory holds exactly the committed files with exactly the committed bytes."""
    if not directory.is_dir() or directory.is_symlink():
        return False
    found = list(directory.rglob("*"))
    if any(p.is_symlink() for p in found):
        return False
    files = {p.relative_to(directory).as_posix(): p for p in found if p.is_file()}
    if set(files) != set(blobs):
        return False
    return all(_blob_id(files[rel].read_bytes(), oid_len) == oid for rel, oid in blobs.items())


def _materialise(repo: Path, directory: Path, blobs: Mapping[str, str], oid_len: int) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{directory.name[:12]}-", dir=directory.parent))
    try:
        for rel, oid in blobs.items():
            done = _git(repo, "cat-file", "blob", oid)
            if done.returncode != 0:
                raise PolicySnapshotError(f"live policy snapshot: cannot read blob {oid} (policy/{rel})")
            target = tmp.joinpath(*PurePosixPath(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(done.stdout)
        if directory.is_symlink() or directory.is_file():
            directory.unlink()                # a planted link or file: remove it, never follow it
        elif directory.exists() and not _snapshot_matches(directory, blobs, oid_len):
            shutil.rmtree(directory)          # partial or edited copy: replace it
        try:
            tmp.rename(directory)
        except OSError:                       # another process finished the same snapshot first
            if not _snapshot_matches(directory, blobs, oid_len):
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sleeve_blockers(repo: Path, policy: Policy, blobs: Mapping[str, str]) -> tuple[PolicyBlocker, ...]:
    """The committed sleeve must be the blob at its quarter's tag `stocks-<quarter>`, else the stock
    sleeve is held (satellite scope) while the core runs."""
    sleeve = policy.universe.stock_sleeve
    if SLEEVE_FILE not in blobs or sleeve is None:
        return ()
    tag = f"stocks-{sleeve.quarter}"          # quarter is validated as YYYYQn
    done = _git(repo, "rev-parse", "--verify", "-q", f"refs/tags/{tag}:policy/{SLEEVE_FILE}")
    tagged = done.stdout.decode().strip() if done.returncode == 0 else None
    if tagged == blobs[SLEEVE_FILE]:
        return ()
    why = (f"tag {tag} not found" if tagged is None
           else f"policy/{SLEEVE_FILE} at HEAD differs from its blob at tag {tag}")
    return (PolicyBlocker(SLEEVE_POLICY_UNTAGGED, "satellite", why),)


def _record_last(snapshots: Path, repo: Path, commit: str, tree: str, blobs: Mapping[str, str]) -> None:
    """Atomically write `policy-snapshots/last.json` = {repo, commit, tree, blobs} after a verified
    snapshot. Best effort: a full disk must not fail a snapshot that was just verified."""
    record = {"repo": str(repo.resolve()), "commit": commit, "tree": tree, "blobs": dict(sorted(blobs.items()))}
    text = json.dumps(record, indent=1, sort_keys=True) + "\n"
    target = snapshots / LAST_SNAPSHOT_FILE
    tmp: str | None = None
    try:
        if target.is_file() and not target.is_symlink() and target.read_text() == text:
            return
        fd, tmp = tempfile.mkstemp(prefix=".last-", suffix=".json", dir=snapshots)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            with suppress(OSError):
                os.unlink(tmp)


def verify_repo_top(repo: Path) -> None:
    """`repo` must be the top of its own checkout: a directory nested inside ANOTHER repository
    would otherwise read that repository's HEAD."""
    top = _git_text(repo, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != repo.resolve():
        raise PolicySnapshotError(f"live policy snapshot: {repo} is not the top of a git checkout (top: {top})")


def head_policy_snapshot(state_dir: Path | None = None, *, repo: Path | None = None,
                         rev: str = "HEAD", include_sleeve: bool | None = None) -> PolicySnapshot:
    """Load the policy COMMITTED at `rev` (default HEAD), never the working tree.

    The files of `<rev>:policy/` are written to `state_dir/policy-snapshots/<tree id>/` (reused
    while their bytes still equal the committed blobs, rewritten otherwise) and `Policy.load` reads
    them there, so an uncommitted edit in the working tree (another agent, a stray write) can
    never reach a live cycle, and `policy.sha256` is the hash of committed content.

    `include_sleeve` (default: `invariants.STOCK_SLEEVE_LIVE`) decides whether a committed
    `stock-sleeve.yaml` becomes stock lines; only then is its `stocks-<quarter>` tag checked.
    A verified HEAD snapshot is recorded in `policy-snapshots/last.json` for
    `last_verified_snapshot`. Every failure (git, the file system) is a `PolicySnapshotError`."""
    repo = repo or paths.REPO_ROOT
    root = state_dir or paths.state_dir()
    paths.assert_outside_repo(root)
    sleeve_live = _sleeve_live(include_sleeve)
    verify_repo_top(repo)
    commit = _git_text(repo, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
    tree = _git_text(repo, "rev-parse", "--verify", "-q", f"{commit}:policy")
    blobs = _policy_blobs(repo, tree)
    directory = root / POLICY_SNAPSHOTS / tree
    try:
        if not _snapshot_matches(directory, blobs, len(tree)):
            _materialise(repo, directory, blobs, len(tree))
            if not _snapshot_matches(directory, blobs, len(tree)):
                raise PolicySnapshotError(f"live policy snapshot {directory} does not match commit {commit}")
    except OSError as exc:
        raise PolicySnapshotError(f"live policy snapshot: cannot write {directory}: {exc}") from exc
    policy = Policy.load(directory, include_sleeve=sleeve_live)
    blockers = _sleeve_blockers(repo, policy, blobs) if sleeve_live else ()
    if rev == "HEAD":
        _record_last(directory.parent, repo, commit, tree, blobs)
    return PolicySnapshot(policy=policy, commit=commit, tree=tree, directory=directory, blockers=blockers)


def last_verified_snapshot(state_dir: Path | None = None, *, repo: Path | None = None,
                           include_sleeve: bool | None = None, reason: str = "") -> PolicySnapshot | None:
    """When git cannot give `HEAD:policy/`: the last snapshot `head_policy_snapshot` verified for
    THIS checkout, re-verified byte for byte against its recorded blob ids, carrying a whole-book
    `policy_snapshot_unavailable` blocker (R20 holds every line; the kill switch, stops and
    flatten proposals, which bypass the engine, keep working). None when there is no record, it
    names another checkout, or the directory no longer matches it. Never the working tree."""
    repo = repo or paths.REPO_ROOT
    root = state_dir or paths.state_dir()
    snapshots = root / POLICY_SNAPSHOTS
    try:
        record = json.loads((snapshots / LAST_SNAPSHOT_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    tree, commit, blobs = record.get("tree"), record.get("commit"), record.get("blobs")
    if not (isinstance(tree, str) and _OID.fullmatch(tree) and isinstance(commit, str) and _OID.fullmatch(commit)
            and isinstance(blobs, dict) and UNIVERSE_FILE in blobs
            and all(isinstance(k, str) and isinstance(v, str) and _OID.fullmatch(v) and len(v) == len(tree)
                    for k, v in blobs.items())):
        return None
    if record.get("repo") != str(repo.resolve()):
        return None
    directory = snapshots / tree
    try:
        if not _snapshot_matches(directory, blobs, len(tree)):
            return None
        policy = Policy.load(directory, include_sleeve=_sleeve_live(include_sleeve))
    except (OSError, ValueError):
        return None
    detail = f"git could not provide HEAD:policy/; running on the last verified snapshot (commit {commit[:12]})"
    if reason:
        detail += f": {reason}"
    return PolicySnapshot(policy=policy, commit=commit, tree=tree, directory=directory,
                          blockers=(PolicyBlocker(POLICY_SNAPSHOT_UNAVAILABLE, "all", detail),), fallback=True)


def release_checkout(state_dir: Path | None = None) -> Path | None:
    """The installed release the launchd jobs run (`<state dir>/releases/current`, written by
    `ops/install.sh`), when there is one."""
    current = (state_dir or paths.state_dir()) / "releases" / "current"
    return current if (current / ".git").exists() else None


# ------------------------------------------------------------------------------- cost quotes
def default_settlement(line: LineSpec, direction: str, leverage: int) -> str:
    """Before eligibility is known: long 1x uses the first long candidate; shorts and leverage
    are CFDs by construction (the only vehicles that support them)."""
    if direction == "long" and leverage == 1 and line.vehicles.long:
        return line.vehicles.long[0].settlement
    return "cfd"


def vehicle_asset_class(line: LineSpec, settlement: str) -> str:
    """Cost floors are keyed by the VEHICLE's class: an index line traded as a UCITS ETF is 'etf';
    a stock line is 'stock' (real shares, floor `stock_real`)."""
    if line.asset_class in ("crypto", "stock"):
        return line.asset_class
    if settlement == "real":
        return "etf"
    return line.asset_class


def floor_cost_quotes(policy: Policy, *, quoted_at: datetime,
                      fee_bps: float = 0.0) -> dict[tuple[str, str, int], CostQuote]:
    """Cost quotes from policy floors for every (line, direction, leverage) the council may use.

    Used before the broker is connected and as the floor under broker what-if quotes afterwards.
    `fee_bps` is the cycle's PRIVATE fixed-fee scalar (`TradeEconomics.fee_nav_bps`); it is set on
    every real non-crypto quote as `fixed_fee_nav_bps` and never enters `per_side_bps` (which is
    published as a cost fact). Stock lines get only the ("long", 1) quote: real shares, never levered
    or short."""
    from council.risk.costs import carry_bps_day, fee_applies, per_side_bps

    out: dict[tuple[str, str, int], CostQuote] = {}
    for line in policy.universe.lines:
        combos: list[tuple[str, int]] = [("long", 1)]
        if line.council_deviations and line.asset_class != "stock":
            combos.append(("long", 2))
            if line.shortable:
                combos.append(("short", 1))
        for direction, lev in combos:
            settlement = default_settlement(line, direction, lev)
            cls = vehicle_asset_class(line, settlement)
            side = per_side_bps(settlement, cls, None, None, policy)
            carry = carry_bps_day(direction, settlement, lev, cls, None, policy)
            out[(line.symbol, direction, lev)] = CostQuote(
                symbol=line.symbol, direction=direction, settlement=settlement, leverage=lev,  # type: ignore[arg-type]
                per_side_bps=side, what_if_bps=None, carry_bps_day=carry, floor_applied=True,
                quoted_at=quoted_at,
                fixed_fee_nav_bps=max(float(fee_bps), 0.0) if fee_applies(settlement, cls) else 0.0,
            )
    return out


def cycle_trade_economics(policy: Policy, state_dir: Path, virtual_nav_usd: float | None) -> Any:
    """The cycle's PRIVATE fee and trade-floor figures (`risk.costs.TradeEconomics`): the virtual
    NAV of the snapshot (the policy's assumed NAV before the broker is connected) and the mirror
    ratio the operator stored with `council account set-mirror`. A missing file gives the assumed
    ratio and the flag `mirror_ratio_missing`; an unreadable one also `mirror_ratio_invalid`."""
    from council.operator.mirror import MirrorError, load_mirror
    from council.risk.costs import trade_economics

    flags: tuple[str, ...] = ()
    ratio: float | None = None
    try:
        mirror = load_mirror(state_dir)
        ratio = mirror.mirror_ratio if mirror is not None else None
    except MirrorError:
        flags = ("mirror_ratio_invalid",)
    return trade_economics(policy, virtual_nav_usd=virtual_nav_usd, mirror_ratio=ratio, flags=flags)


def cost_hints(quotes: Mapping[tuple[str, str, int], CostQuote]) -> dict[str, dict[str, float]]:
    """Per-line hints for the desk pack (the 1x long quote)."""
    return {line: {"per_side_bps": q.per_side_bps, "carry_bps_day": q.carry_bps_day}
            for (line, direction, lev), q in quotes.items() if direction == "long" and lev == 1}


def engine_quotes(quotes: Mapping[tuple[str, str, int], CostQuote]) -> dict[object, CostQuote]:
    """The engine accepts (line, direction, leverage) keys and (line, direction) for 1x."""
    out: dict[object, CostQuote] = dict(quotes)
    for (line, direction, lev), q in quotes.items():
        if lev == 1:
            out[(line, direction)] = q
    return out


# ------------------------------------------------------------------------------ fingerprint
MATERIAL_GLOBAL = "_global"


def material_fingerprint(pack: FactPack, cards: Sequence[EvidenceCard], kill_state: str) -> str:
    """LEGACY single fingerprint (before design §11.4): trend states, the admitted set, qualifying
    card content, active events, kill state. Kept to read a ledger that stored only it
    (`material_changes(legacy_equal=...)`); cycles use `material_fingerprints`."""
    trend = {s: st.trend for s, st in sorted(pack.states.items())}
    qual = sorted(
        (c.card_type, tuple(sorted(c.scope)), c.direction)
        for c in cards if c.qualifying
    )
    events = sorted(e.id for e in pack.events)
    blob = json.dumps({"trend": trend, "admitted": sorted(pack.admitted), "cards": qual,
                       "events": events, "kill": kill_state}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def material_fingerprints(pack: FactPack, cards: Sequence[EvidenceCard], kill_state: str) -> dict[str, str]:
    """What must change before a NEW discretionary target on a line is executable, per line
    (design §11.4): {"_global": kill state, market-wide events (no symbols) and qualifying cards that
    name no line; <line>: its trend, its data-freeze status (market_closed excluded), the qualifying
    cards whose scope names it, the events on it, and its fundamentals values (filing age excluded:
    it moves with the calendar)}. Session admission is not material."""
    from council.facts.evidence_ids import FUNDAMENTAL_NOT_MATERIAL

    lines = sorted(pack.states)
    known = set(lines)
    qual = [c for c in cards if c.qualifying]
    out = {MATERIAL_GLOBAL: _digest({
        "kill": kill_state,
        "events": sorted(e.id for e in pack.events if not e.symbols),
        "cards": sorted((c.card_type, tuple(sorted(c.scope)), c.direction)
                        for c in qual if not set(c.scope) & known),
    })}
    for s in lines:
        state = pack.states[s]
        freeze = sorted(r for r in (state.frozen_reason or "").split(",") if r and r != "market_closed")
        if state.frozen and not state.frozen_reason:
            freeze = ["upstream"]
        fundamentals = sorted((f.id, f.value) for f in pack.facts
                              if f.kind == "fundamental" and f.symbol == s
                              and f.id.rsplit(":", 1)[-1] not in FUNDAMENTAL_NOT_MATERIAL)
        out[s] = _digest({
            "trend": state.trend,
            "freeze": freeze,
            "cards": sorted((c.card_type, tuple(sorted(c.scope)), c.direction) for c in qual if s in c.scope),
            "events": sorted(e.id for e in pack.events if s in e.symbols),
            "fundamentals": fundamentals,
        })
    return out


def material_key(fingerprints: Mapping[str, str], line: str) -> str:
    """A line's material evidence as MC compares it: its own fingerprint together with the market-wide
    one, so every line consumes market-wide evidence for itself (`consume_fingerprints`)."""
    return _digest({MATERIAL_GLOBAL: fingerprints.get(MATERIAL_GLOBAL), "line": fingerprints.get(line)})


def consume_fingerprints(current: Mapping[str, str], stored: Mapping[str, str] | None,
                         lines: Iterable[str]) -> dict[str, str]:
    """The map the ledger stores when a proposal issues ({line: material_key, "_global": the current
    market-wide fingerprint, for reference only}).

    Rule: evidence is consumed per line, and only by a line that could act on it in that cycle
    (`lines`: admitted by the pack and not held by a blocker, `cycle.evidence_lines`); every other
    line keeps its stored key. New daily bars land at the 02:40 slot, while the London and US
    sessions are closed: a proposal issued then (a crypto leg) must not use up the equity and stock
    lines' new evidence before their markets open, and a session opening alone is still not
    evidence. With no stored map (the first write, or a ledger that kept only the legacy single
    fingerprint) every line consumes, as the single fingerprint did. Lines no longer in `current`
    are dropped."""
    names = [s for s in current if s != MATERIAL_GLOBAL]
    can_act = set(names) if not stored else set(lines)
    out = {s: str(stored[s]) for s in names if stored and s in stored and s not in can_act}
    out.update({s: material_key(current, s) for s in names if s in can_act})
    out[MATERIAL_GLOBAL] = current[MATERIAL_GLOBAL]
    return dict(sorted(out.items()))


def material_changes(current: Mapping[str, str], stored: Mapping[str, str] | None, *,
                     legacy_equal: bool | None = None) -> dict[str, bool]:
    """{line: new material evidence since the line last consumed it}: its `material_key` (its own
    fingerprint with the market-wide one) differs from the key stored for it by
    `consume_fingerprints` (a line with no stored key counts as changed). With no stored map, a
    ledger that kept only the legacy fingerprint answers for every line (`legacy_equal`: it still
    matches, so nothing changed); with neither, every line counts as changed (the first cycle)."""
    lines = [s for s in current if s != MATERIAL_GLOBAL]
    if not stored:
        changed = legacy_equal is not True
        return {s: changed for s in lines}
    return {s: material_key(current, s) != stored.get(s) for s in lines}


def fingerprints_digest(fingerprints: Mapping[str, str]) -> str:
    """One hash of the per-line map (the cycle record's `material_fingerprint`)."""
    return _digest(dict(fingerprints))


def first_cycle_of_utc_day(last_macro_day: str | None, slot: datetime) -> bool:
    return last_macro_day != slot.date().isoformat()


def window(slot: datetime, *, back_h: int = 24, ahead_days: int = 7) -> tuple[datetime, datetime]:
    return slot - timedelta(hours=back_h), slot + timedelta(days=ahead_days)
