"""The 48-hour launchd soak (m5-readiness M5-F, gate O3).

`ops/install.sh <tag> --rehearsal --load` loads two jobs from the installed release:

- `com.fbzz.council.rehearsal.cycle`, hourly at :40: `soak cycle` runs `run_cycle` on the rehearsal
  state (`state_dir/rehearsal`, its own ledger, no broker) with the stub LLM, except in the 10:40 UTC
  slot, which runs the real model once a day (the slot's record makes the later hours
  `already_done`, so at most one real cycle per day).
- `com.fbzz.council.rehearsal.watch`, every 15 min and at load: `soak watch` runs the dry-run watch
  on the same state, and once a day (and on the first run after a failure or a fresh soak) probes
  `git push --dry-run origin HEAD:main` on the REAL publisher clone (authenticates as a push, sends
  nothing) and `read_secret("council-book.soak-probe")` through the production Keychain path (the
  value is discarded, never logged). It then writes `readiness/soak.json` (O3) as the runner.

Both publish only to `state_dir/rehearsal/remote.git`, a local bare repository: the rehearsal
publisher clone is refused unless its `origin` is exactly that path and it has no push URL. Nothing
here imports the broker, the broker writer or the approval path. Every event is a code in
`state_dir/rehearsal/soak-log.jsonl`; `install.sh --rehearsal` moves an old log aside so each soak
is judged on its own window.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from council import clock, paths

REHEARSAL = "rehearsal"
REMOTE = "remote.git"
CLONE = "publisher-clone"
LOG = "soak-log.jsonl"
LABEL_PREFIX = "com.fbzz.council.rehearsal."
PROBE_SERVICE = "council-book.soak-probe"
REAL_LLM_SLOT_HOUR = 10                 # the 10:40 UTC slot runs the real model once a day
MIN_WINDOW = timedelta(hours=48)
ON_TIME_MIN = 0.95
PROBE_EVERY = timedelta(hours=24)
RAN = ("on_time", "late")               # a cycle record that actually ran the council
_IDENT = ("-c", "user.name=council-rehearsal", "-c", "user.email=rehearsal@localhost",
          "-c", "commit.gpgsign=false")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class SoakError(RuntimeError):
    """The soak refuses to run (the message is fixed text, never a URL or a secret)."""


# ------------------------------------------------------------------------------------ paths / log
def rehearsal_root(state_dir: Path | None = None) -> Path:
    return (state_dir if state_dir is not None else paths.state_dir()) / REHEARSAL


def _launchd(env: Mapping[str, str]) -> bool:
    return env.get("XPC_SERVICE_NAME", "").startswith(LABEL_PREFIX)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def append_event(root: Path, event: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOG
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(dict(event), sort_keys=True) + "\n")


def read_events(root: Path) -> list[dict[str, Any]]:
    try:
        lines = (root / LOG).read_text().splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and _parse(item.get("at")) is not None:
            events.append(item)
    return events


def _parse(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts.astimezone(UTC) if ts.tzinfo else None


# ------------------------------------------------------------------------------------ local remote
def ensure_local_remote(root: Path, *, runner: Runner = subprocess.run) -> Path:
    """Create `root/remote.git` (bare) and `root/publisher-clone` on first use; refuse a clone whose
    origin is anything but that local path (the soak never publishes to GitHub)."""
    remote, clone = root / REMOTE, root / CLONE

    def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        return runner(["git", *_IDENT, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)

    root.mkdir(parents=True, exist_ok=True)
    if not remote.exists() and git("init", "--bare", "-q", "-b", "main", str(remote)).returncode != 0:
        raise SoakError("cannot create the rehearsal remote")
    if not clone.exists():
        steps = (("clone", "-q", str(remote), str(clone)), ("checkout", "-q", "-B", "main"),
                 ("commit", "-q", "--allow-empty", "-m", "rehearsal remote"),
                 ("push", "-q", "origin", "HEAD:refs/heads/main"))
        for step in steps:
            if git(*step, cwd=None if step[0] == "clone" else clone).returncode != 0:
                raise SoakError(f"cannot set up the rehearsal publisher clone (git {step[0]})")
    assert_local_origin(root, runner=runner)
    return clone


def assert_local_origin(root: Path, *, runner: Runner = subprocess.run) -> None:
    clone, remote = root / CLONE, (root / REMOTE).resolve()

    def config(*args: str) -> subprocess.CompletedProcess[str]:
        return runner(["git", "-C", str(clone), "config", *args], capture_output=True, text=True, check=False)

    url = config("--get", "remote.origin.url")
    if url.returncode != 0 or Path(url.stdout.strip()).expanduser().resolve() != remote:
        raise SoakError("the rehearsal publisher clone's origin is not state_dir/rehearsal/remote.git")
    if config("--get-all", "remote.origin.pushurl").stdout.strip():
        raise SoakError("the rehearsal publisher clone has a push URL; the soak publishes locally only")
    remotes = runner(["git", "-C", str(clone), "remote"], capture_output=True, text=True, check=False)
    if remotes.stdout.split() != ["origin"]:
        raise SoakError("the rehearsal publisher clone must have exactly one remote (origin)")


# ------------------------------------------------------------------------------------ cycle job
def choose_llm(now: datetime, events: Sequence[Mapping[str, Any]] = ()) -> str:
    """"real" only in the 10:40 UTC slot and only if no real-model attempt is logged for that slot's
    UTC day yet: a failed or killed 10:40 run is not retried with the real model at 11:40-13:40
    (those hours fall back to the stub; the day's O3 check then stays red rather than spend more
    model calls than the approved one real cycle a day)."""
    slot = clock.classify(now).slot
    if slot.hour != REAL_LLM_SLOT_HOUR:
        return "stub"
    day = slot.astimezone(UTC).date()
    for e in events:
        at = _parse(e.get("at"))
        if e.get("kind") in ("real_attempt", "cycle") and e.get("llm") == "real" and at is not None \
                and clock.classify(at).slot.astimezone(UTC).date() == day:
            return "stub"
    return "real"


def _default_build(**kwargs: Any) -> Any:
    from council.context import build_context

    return build_context(**kwargs)


def _default_cycle(ctx: Any) -> Any:
    from council.cycle import run_cycle

    return run_cycle(ctx)


def _default_watch(ctx: Any) -> Any:
    from council.watch import run_watch

    return run_watch(ctx)


def _context(build: Callable[..., Any], root: Path, clone: Path, *, stub_llm: bool) -> Any:
    return build(mode="dry_run", stub_llm=stub_llm, publish="push", state_dir=root, publisher_dir=clone)


def run_soak_cycle(
    *,
    state_dir: Path | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    build: Callable[..., Any] = _default_build,
    cycle: Callable[[Any], Any] = _default_cycle,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    root = rehearsal_root(state_dir)
    llm = choose_llm(now, read_events(root))
    event: dict[str, Any] = {"kind": "cycle", "at": _iso(now), "llm": llm, "launchd": _launchd(env)}
    try:
        clone = ensure_local_remote(root, runner=runner)
        if llm == "real":           # logged BEFORE the model runs, so a crash cannot earn a retry
            append_event(root, {"kind": "real_attempt", "at": _iso(now), "llm": "real"})
        outcome = cycle(_context(build, root, clone, stub_llm=llm == "stub"))
    except Exception as exc:
        event["status"] = f"error:{type(exc).__name__.lower()}"
        append_event(root, event)
        raise
    event.update(cycle_id=str(getattr(outcome, "cycle_id", "")), status=str(getattr(outcome, "status", "")),
                 published=bool(getattr(outcome, "published", False)))
    append_event(root, event)
    return event


# ------------------------------------------------------------------------------------ watch job
def probe_push(state_dir: Path, *, runner: Runner = subprocess.run) -> str:
    """`git push --dry-run origin HEAD:main` on the real publisher clone. Output is discarded (it
    can carry the remote URL); only a code is kept."""
    clone = state_dir / "publisher-clone"
    if not (clone / ".git").exists():
        return "push_auth_no_clone"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    key = state_dir / "deploy_key"
    if key.exists():
        import shlex

        env["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes"
    try:
        result = runner(["git", "-C", str(clone), "push", "--dry-run", "-q", "origin", "HEAD:main"],
                        env=env, capture_output=True, text=True, check=False, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return "push_auth_failed"
    return "push_auth_ok" if result.returncode == 0 else "push_auth_failed"


def probe_keychain(reader: Callable[[str], str] | None = None) -> str:
    """Read the soak-probe item through the production path; the value is dropped at once."""
    if reader is None:
        from council.operator.keychain import read_secret as reader
    try:
        ok = bool(reader(PROBE_SERVICE))
    except Exception:  # noqa: BLE001 - any failure is a code, never a message that could echo input
        return "keychain_failed"
    return "keychain_ok" if ok else "keychain_empty"


def probes_due(events: Sequence[Mapping[str, Any]], now: datetime) -> bool:
    probes = [e for e in events if e.get("kind") == "probe"]
    if not probes:
        return True
    last = probes[-1]
    if last.get("push") != "push_auth_ok" or last.get("keychain") != "keychain_ok":
        return True
    at = _parse(last.get("at"))
    return at is None or now - at >= PROBE_EVERY


def expected_slots(start: datetime, now: datetime) -> list[str]:
    """Cycle ids of every slot from the first at/after `start` to the last whose on-time window
    (10 min) has closed by `now`."""
    slot = clock.slot_at_or_before(start)
    if slot < start:
        slot += timedelta(hours=4)
    ids = []
    while slot + timedelta(minutes=10) <= now:
        ids.append(clock.cycle_id_for(slot))
        slot += timedelta(hours=4)
    return ids


def evaluate(events: Sequence[Mapping[str, Any]], now: datetime) -> tuple[str, str]:
    """O3 (§6): >= 48 h of launchd rehearsal cycles, >= 95 % of slots on time, >= 1 real-LLM cycle
    per soak day, >= 1 push-auth check and >= 1 soak-probe Keychain read from launchd."""
    cycles = [e for e in events if e.get("kind") == "cycle" and e.get("launchd") is True]
    if not cycles:
        return "red", "soak_missing"
    start = min(_parse(e["at"]) for e in cycles)            # type: ignore[type-var]
    assert start is not None
    window = now - start
    if window < MIN_WINDOW:
        return "red", "soak_short"
    slots = expected_slots(start, now)
    on_time = {e.get("cycle_id") for e in cycles if e.get("status") == "on_time"}
    if not slots or sum(1 for s in slots if s in on_time) / len(slots) < ON_TIME_MIN:
        return "red", "soak_late"
    for day in range(int(window / timedelta(hours=24))):
        lo, hi = start + timedelta(hours=24 * day), start + timedelta(hours=24 * (day + 1))
        if not any(e.get("llm") == "real" and e.get("status") in RAN and lo <= _parse(e["at"]) < hi  # type: ignore[operator]
                   for e in cycles):
            return "red", "soak_no_real_llm"
    probes = [e for e in events if e.get("kind") == "probe" and e.get("launchd") is True]
    if not any(e.get("push") == "push_auth_ok" for e in probes):
        return "red", "soak_no_push_auth"
    if not any(e.get("keychain") == "keychain_ok" for e in probes):
        return "red", "soak_no_keychain"
    return "green", "soak_ok"


def release_head(*, runner: Runner = subprocess.run) -> str:
    result = runner(["git", "-C", str(paths.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                    check=False)
    head = result.stdout.strip().lower()
    if result.returncode != 0 or len(head) != 40:
        raise SoakError("cannot read the release commit")
    return head


def _default_write(**kwargs: Any) -> Path:
    from council.operator.readiness import write_record

    return write_record("soak", **kwargs)


def run_soak_watch(
    *,
    state_dir: Path | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    build: Callable[..., Any] = _default_build,
    watch: Callable[[Any], Any] = _default_watch,
    push_probe: Callable[[Path], str] | None = None,
    keychain_probe: Callable[[], str] = probe_keychain,
    write: Callable[..., Path] = _default_write,
    head: Callable[[], str] = release_head,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    state = state_dir if state_dir is not None else paths.state_dir()
    root = rehearsal_root(state)
    clone = ensure_local_remote(root, runner=runner)
    status = "error"
    try:
        status = str(getattr(watch(_context(build, root, clone, stub_llm=True)), "status", ""))
    finally:
        append_event(root, {"kind": "watch", "at": _iso(now), "status": status, "launchd": _launchd(env)})
    probe: dict[str, Any] | None = None
    if probes_due(read_events(root), now):
        push = (push_probe or (lambda s: probe_push(s, runner=runner)))(state)
        probe = {"kind": "probe", "at": _iso(now), "push": push, "keychain": keychain_probe(),
                 "launchd": _launchd(env)}
        append_event(root, probe)
    state_code = evaluate(read_events(root), now)
    write(head=head(), gates={"O3": {"state": state_code[0], "code": state_code[1]}}, state_dir=state,
          now=now, env=env)
    return {"watch": status, "probe": probe, "O3": state_code}


# ------------------------------------------------------------------------------------ entry points
def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["cycle"]:
        print(json.dumps(run_soak_cycle()))
        return 0
    if args == ["watch"]:
        print(json.dumps(run_soak_watch(), default=str))
        return 0
    print("usage: python -m council.operator.soak cycle|watch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
