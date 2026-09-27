"""Live cycles load policy from a snapshot of the committed `HEAD:policy/`, never the working tree;
a git failure falls back only to the last VERIFIED snapshot (every line held, URGENT alert); a
committed sleeve stays out of every runtime policy until `invariants.STOCK_SLEEVE_LIVE`, and once live,
a sleeve that is not the blob at its `stocks-<quarter>` tag holds only the satellite."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from council import invariants, paths
from council import policy as policy_module
from council.clock import utcnow
from council.context import (
    POLICY_ALERT_EVERY,
    alert_policy_snapshot,
    build_context,
    eligibility_blockers,
)
from council.cycle import engine_blockers
from council.invariants import check_policy
from council.operator import notify
from council.paths import POLICY_DIR
from council.policy import (
    SLEEVE_FILE,
    Policy,
    default_policy,
    install_default_policy,
    policy_sha256,
)
from council.runtime import (
    LAST_SNAPSHOT_FILE,
    POLICY_SNAPSHOT_UNAVAILABLE,
    POLICY_SNAPSHOTS,
    SLEEVE_POLICY_UNTAGGED,
    PolicyBlocker,
    PolicySnapshotError,
    Sources,
    head_policy_snapshot,
    last_verified_snapshot,
)
from council.settings import Settings
from council.stocks.eligibility import UNCHECKED
from tests.conftest import make_sleeve_policy_dir


def _git(root: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    """A throwaway checkout whose policy/ is a copy of the repository's top-level policy files."""
    root = tmp_path / "checkout"
    make_sleeve_policy_dir(root / "policy", overlay=tmp_path / "no-overlay")
    (root / "README").write_text("fixture\n")
    _git(root, "init", "-q")
    _commit(root, "policy")
    return root


UNCHECKED_STAMP = "    eligibility_checked_at: null\n"
CHECKED_STAMP = '    eligibility_checked_at: "2026-11-20T15:02:00Z"\n'


@pytest.fixture
def sleeve_repo(tmp_path) -> Path:
    """The fixture sleeve with every line eligibility-checked (TSTE stamped like the others), so
    only the tag decides the snapshot's blockers."""
    root = tmp_path / "checkout"
    make_sleeve_policy_dir(root / "policy")
    _bump(root / "policy" / SLEEVE_FILE, UNCHECKED_STAMP, CHECKED_STAMP)
    _git(root, "init", "-q")
    _commit(root, "policy with a stock sleeve")
    return root


@pytest.fixture
def unchecked_sleeve_repo(tmp_path) -> Path:
    """The fixture sleeve as it is: TSTE was never eligibility-checked (a `--no-eligibility` rank)."""
    root = tmp_path / "checkout"
    make_sleeve_policy_dir(root / "policy")
    assert UNCHECKED_STAMP in (root / "policy" / SLEEVE_FILE).read_text()
    _git(root, "init", "-q")
    _commit(root, "policy with an unchecked stock line")
    _git(root, "tag", "stocks-2026Q4")
    return root


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def sleeve_live(monkeypatch):
    """The go-live switch flipped (as the go-live commit will): a committed sleeve becomes stock lines."""
    monkeypatch.setattr(invariants, "STOCK_SLEEVE_LIVE", True)


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str, str, str | None]]:
    """Every notification a Notifier would deliver: (channel, title, priority, topic)."""
    out: list[tuple[str, str, str, str | None]] = []
    monkeypatch.setattr(notify, "default_sender",
                        lambda channel, message, topic: out.append((channel, message.title, message.priority, topic)))
    return out


def _no_git(monkeypatch, tmp_path) -> None:
    """The runner's git is gone (a broken xcrun shim or Homebrew git): nothing called `git` on PATH."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def _bump(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


# --------------------------------------------------------------------------- HEAD, not the worktree
def test_the_snapshot_is_the_committed_policy(repo, state):
    snap = head_policy_snapshot(state, repo=repo)
    assert snap.commit == _git(repo, "rev-parse", "HEAD")
    assert snap.tree == _git(repo, "rev-parse", "HEAD:policy")
    assert snap.directory == state / POLICY_SNAPSHOTS / snap.tree
    assert snap.policy.sha256 == policy_sha256(repo / "policy") == Policy.load(repo / "policy").sha256
    assert snap.blockers == ()
    check_policy(snap.policy)


def test_a_dirty_working_tree_does_not_change_the_live_policy_or_its_sha(repo, state):
    committed = head_policy_snapshot(state, repo=repo).policy
    _bump(repo / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.60")
    make_sleeve_policy_dir(repo / "policy")          # an untracked sleeve + a re-based universe on disk
    dirty = Policy.load(repo / "policy")
    assert dirty.sha256 != committed.sha256 and dirty.universe.stock_lines()
    again = head_policy_snapshot(state, repo=repo)
    assert again.policy.sha256 == committed.sha256
    assert again.policy.risk == committed.risk and again.policy.universe == committed.universe
    assert again.policy.universe.stock_lines() == [] and again.blockers == ()


def test_a_new_commit_is_picked_up_and_old_snapshots_stay_keyed_by_tree(repo, state):
    first = head_policy_snapshot(state, repo=repo)
    _bump(repo / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.25")
    _commit(repo, "tighter vol")
    second = head_policy_snapshot(state, repo=repo)
    assert second.tree != first.tree and second.directory != first.directory
    assert second.policy.risk["ex_ante_vol_hard"] == 0.25 and second.policy.sha256 != first.policy.sha256
    (repo / "README").write_text("an unrelated commit\n")
    _commit(repo, "docs only")
    third = head_policy_snapshot(state, repo=repo)
    assert third.commit != second.commit and third.tree == second.tree        # same policy, same snapshot


def test_an_edited_or_partial_snapshot_is_rewritten_from_git(repo, state):
    snap = head_policy_snapshot(state, repo=repo)
    _bump(snap.directory / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.90")
    (snap.directory / "extra.yaml").write_text("version: 1\n")
    (snap.directory / "costs.yaml").unlink()
    again = head_policy_snapshot(state, repo=repo)
    assert again.policy.sha256 == snap.policy.sha256
    assert again.policy.risk["ex_ante_vol_hard"] == 0.30 and not (again.directory / "extra.yaml").exists()


def test_without_a_git_checkout_the_live_snapshot_fails_closed(tmp_path, state):
    plain = tmp_path / "plain"
    shutil.copytree(POLICY_DIR, plain / "policy")
    with pytest.raises(PolicySnapshotError, match="rev-parse"):
        head_policy_snapshot(state, repo=plain)


def test_a_symlink_under_policy_is_refused(repo, state):
    (repo / "policy" / "link.yaml").symlink_to("risk.yaml")
    _commit(repo, "a symlink")
    with pytest.raises(PolicySnapshotError, match="not a regular file"):
        head_policy_snapshot(state, repo=repo)


# ------------------------------------------------------------------------------ sleeve tag check
def test_an_untagged_sleeve_holds_only_the_satellite(sleeve_repo, state, sleeve_live):
    snap = head_policy_snapshot(state, repo=sleeve_repo)
    assert snap.blockers == (PolicyBlocker(SLEEVE_POLICY_UNTAGGED, "satellite", "tag stocks-2026Q4 not found"),)
    # the policy still loads: the core runs, the engine holds the stock lines (satellite scope)
    assert snap.policy.universe.symbols()[:9] == ["NDX", "SEMIS", "SPX", "GOLD", "BTC", "ETH", "OIL", "EURUSD", "GBPUSD"]
    assert [ln.symbol for ln in snap.policy.universe.stock_lines()] == ["TSTA", "TSTB", "TSTC_B", "F", "TSTD", "TSTE"]
    assert all(b.scope == "satellite" for b in snap.blockers)


@pytest.mark.parametrize("annotated", [False, True])
def test_the_sleeve_blob_at_its_quarter_tag_clears_the_blocker(sleeve_repo, state, annotated, sleeve_live):
    _git(sleeve_repo, "tag", *(["-a", "-m", "stocks 2026Q4"] if annotated else []), "stocks-2026Q4")
    assert head_policy_snapshot(state, repo=sleeve_repo).blockers == ()
    # a later edit of the sleeve without a new tag: the committed blob no longer equals the tagged one
    _bump(sleeve_repo / "policy" / SLEEVE_FILE, 'name: "Test Alpha Inc"', 'name: "Test Alpha Incorporated"')
    _commit(sleeve_repo, "untagged sleeve edit")
    snap = head_policy_snapshot(state, repo=sleeve_repo)
    assert [(b.code, b.scope) for b in snap.blockers] == [(SLEEVE_POLICY_UNTAGGED, "satellite")]
    assert "differs from its blob at tag stocks-2026Q4" in snap.blockers[0].detail
    # an unrelated core commit after a correct tag keeps the sleeve blob, so no blocker
    _git(sleeve_repo, "tag", "-f", "stocks-2026Q4")
    (sleeve_repo / "notes.txt").write_text("later\n")
    _commit(sleeve_repo, "unrelated")
    assert head_policy_snapshot(state, repo=sleeve_repo).blockers == ()


def test_a_tag_of_another_quarter_does_not_count(sleeve_repo, state, sleeve_live):
    _git(sleeve_repo, "tag", "stocks-2026Q3")
    assert [b.code for b in head_policy_snapshot(state, repo=sleeve_repo).blockers] == [SLEEVE_POLICY_UNTAGGED]
    data = yaml.safe_load((sleeve_repo / "policy" / SLEEVE_FILE).read_text())
    assert data["quarter"] == "2026Q4"


# ---------------------------------------------------------------------------------- build_context
def _sources() -> Sources:
    return Sources(history=lambda slot: ({}, []), events=lambda start, end: ([], []))


def test_a_live_context_runs_on_head_and_installs_it_as_the_default(repo, state, monkeypatch):
    committed = head_policy_snapshot(state, repo=repo).policy
    _bump(repo / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.60")
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    ctx = build_context(mode="live", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                        settings=Settings(role="runner", mode="live"))
    assert ctx.policy.sha256 == committed.sha256 and ctx.policy.risk["ex_ante_vol_hard"] == 0.30
    assert ctx.policy_commit == _git(repo, "rev-parse", "HEAD") and ctx.policy_blockers == ()
    assert default_policy() is ctx.policy


def test_a_live_context_carries_the_untagged_sleeve_blocker(sleeve_repo, state, monkeypatch, sleeve_live):
    monkeypatch.setattr(paths, "REPO_ROOT", sleeve_repo)
    ctx = build_context(mode="live", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                        settings=Settings(role="runner", mode="live"))
    assert [(b.code, b.scope) for b in ctx.policy_blockers] == [(SLEEVE_POLICY_UNTAGGED, "satellite")]
    assert ctx.policy.universe.stock_lines()


def test_dry_run_and_stub_contexts_keep_the_working_tree_policy(state, policy):
    """Outside a live context the policy is the process default (in tests: the core-only pin; in
    production: the working tree loaded once), so there is one policy object per process."""
    for mode in ("dry_run", "stub"):
        ctx = build_context(mode=mode, stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                            settings=Settings(role="dev", mode="stub"))
        assert ctx.policy is default_policy() is policy
        assert ctx.policy_commit == "" and ctx.policy_blockers == ()
    assert default_policy() is policy                  # nothing installed outside a live context


def test_a_live_context_without_git_refuses_to_start(tmp_path, state, monkeypatch):
    plain = tmp_path / "plain"
    shutil.copytree(POLICY_DIR, plain / "policy")
    monkeypatch.setattr(paths, "REPO_ROOT", plain)
    with pytest.raises(PolicySnapshotError):
        build_context(mode="live", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                      settings=Settings(role="runner", mode="live"))


def test_the_repository_head_snapshot_loads():
    """In a git checkout (CI's is shallow), HEAD:policy/ materialises and passes the invariants."""
    probe = subprocess.run(["git", "-C", str(paths.REPO_ROOT), "rev-parse", "--verify", "-q", "HEAD:policy"],
                           capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("not a git checkout")
    snap = head_policy_snapshot()
    check_policy(snap.policy)
    assert snap.tree == probe.stdout.decode().strip()


# ------------------------------------------------------------------ go-live switch: the sleeve stays out
def test_the_sleeve_switch_is_off_until_the_go_live_commit():
    assert invariants.STOCK_SLEEVE_LIVE is False       # flips only in the go-live commit (CHANGELOG policy entry)


def _approve_ctx(state: Path, **kw):
    """The operator's approval path: a stub context whose policy comes from the committed HEAD."""
    return build_context(mode="stub", publish="none", sources=_sources(), state_dir=state,
                         settings=Settings(role="operator", mode="stub"), policy_from_head=True, **kw)


def _live_ctx(state: Path, *, topic: str | None = None):
    return build_context(mode="live", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                         settings=Settings(role="runner", mode="live", ntfy_topic=topic))


def _dry_ctx(state: Path):
    return build_context(mode="dry_run", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                         settings=Settings(role="dev", mode="stub"))


def _unpin_working_tree(monkeypatch, policy_dir: Path) -> None:
    """Production's `default_policy()`: the working tree (here `policy_dir`) loaded once per process."""
    install_default_policy(None)
    policy_module.default_policy.cache_clear()
    monkeypatch.setattr(policy_module, "POLICY_DIR", policy_dir)


def test_a_committed_tagged_sleeve_stays_out_of_live_dry_run_and_approval(sleeve_repo, state, monkeypatch):
    _git(sleeve_repo, "tag", "stocks-2026Q4")
    core_sha = Policy.load(sleeve_repo / "policy", include_sleeve=False).sha256
    snap = head_policy_snapshot(state, repo=sleeve_repo)
    assert snap.policy.universe.stock_lines() == [] and snap.blockers == () and snap.policy.sha256 == core_sha
    monkeypatch.setattr(paths, "REPO_ROOT", sleeve_repo)
    live, approve = _live_ctx(state), _approve_ctx(state)
    _unpin_working_tree(monkeypatch, sleeve_repo / "policy")
    dry = _dry_ctx(state)                                # dry run and rehearsal read the working tree
    for ctx in (live, approve, dry):
        assert ctx.policy.universe.stock_lines() == [] and ctx.policy.universe.stock_sleeve is None
        assert ctx.policy.sha256 == core_sha and ctx.policy_blockers == ()
    # the go-live commit flips the switch: the same checkout's sleeve becomes live everywhere
    monkeypatch.setattr(invariants, "STOCK_SLEEVE_LIVE", True)
    _unpin_working_tree(monkeypatch, sleeve_repo / "policy")
    assert [ln.symbol for ln in _dry_ctx(state).policy.universe.stock_lines()][:3] == ["TSTA", "TSTB", "TSTC_B"]
    for ctx in (_live_ctx(state), _approve_ctx(state)):
        assert ctx.policy.universe.stock_lines() and ctx.policy_blockers == ()


# ------------------------------------------------- live refusal of never-checked stock lines (WP-D)
def test_a_live_context_holds_the_satellite_for_an_unchecked_stock_line(unchecked_sleeve_repo, state, monkeypatch,
                                                                        sleeve_live):
    monkeypatch.setattr(paths, "REPO_ROOT", unchecked_sleeve_repo)
    snap = head_policy_snapshot(state, repo=unchecked_sleeve_repo)
    assert snap.blockers == ()                                          # tagged: the snapshot itself is fine
    for ctx in (_live_ctx(state), _approve_ctx(state)):
        assert ctx.policy.universe.stock_lines()
        (blocker,) = ctx.policy_blockers
        assert (blocker.code, blocker.scope) == (UNCHECKED, "satellite") and "TSTE" in blocker.detail
        # R20 gets the code only (no line id in a blocker string), scoped to the satellite
        assert engine_blockers(ctx) == [f"satellite:{UNCHECKED}"]


def test_an_unchecked_line_and_an_untagged_sleeve_are_two_satellite_blockers(tmp_path, state, monkeypatch,
                                                                            sleeve_live):
    root = tmp_path / "checkout"
    make_sleeve_policy_dir(root / "policy")
    _git(root, "init", "-q")
    _commit(root, "untagged, with an unchecked line")
    monkeypatch.setattr(paths, "REPO_ROOT", root)
    ctx = _live_ctx(state)
    assert [(b.code, b.scope) for b in ctx.policy_blockers] == [(SLEEVE_POLICY_UNTAGGED, "satellite"),
                                                                (UNCHECKED, "satellite")]
    assert engine_blockers(ctx) == [f"satellite:{SLEEVE_POLICY_UNTAGGED}", f"satellite:{UNCHECKED}"]


def test_the_eligibility_blocker_needs_a_stock_line(policy, sleeve_policy):
    assert eligibility_blockers(policy) == ()                           # core-only: nothing to check
    (blocker,) = eligibility_blockers(sleeve_policy)
    assert blocker == PolicyBlocker(UNCHECKED, "satellite",
                                    "stock lines never eligibility-checked: TSTE; re-rank with the broker gate")


def test_before_the_switch_an_unchecked_line_raises_no_blocker(unchecked_sleeve_repo, state, monkeypatch):
    monkeypatch.setattr(paths, "REPO_ROOT", unchecked_sleeve_repo)
    ctx = _live_ctx(state)
    assert ctx.policy.universe.stock_lines() == [] and ctx.policy_blockers == ()


def test_before_the_switch_an_untagged_sleeve_raises_no_blocker(sleeve_repo, state):
    snap = head_policy_snapshot(state, repo=sleeve_repo)
    assert snap.policy.universe.stock_lines() == [] and snap.blockers == ()
    assert head_policy_snapshot(state, repo=sleeve_repo, include_sleeve=True).blockers != ()


def test_a_runtime_context_refuses_stock_lines_before_the_switch(state, sleeve_policy):
    install_default_policy(sleeve_policy)            # e.g. a policy object built around the loader
    with pytest.raises(invariants.InvariantViolation, match="go-live switch"):
        build_context(mode="stub", stub_llm=True, publish="none", sources=_sources(), state_dir=state,
                      settings=Settings(role="dev", mode="stub"))


# ---------------------------------------------------------- git failure: the last verified snapshot
def test_a_verified_head_snapshot_is_recorded_as_the_last_one(repo, state):
    snap = head_policy_snapshot(state, repo=repo)
    record = json.loads((state / POLICY_SNAPSHOTS / LAST_SNAPSHOT_FILE).read_text())
    assert (record["repo"], record["commit"], record["tree"]) == (str(repo.resolve()), snap.commit, snap.tree)
    assert set(record["blobs"]) == {p.name for p in (repo / "policy").glob("*.yaml")}
    assert record["blobs"]["risk.yaml"] == _git(repo, "rev-parse", "HEAD:policy/risk.yaml")


def test_without_git_a_live_run_holds_every_line_on_the_last_verified_snapshot(repo, state, monkeypatch, tmp_path,
                                                                                sent):
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    first = _live_ctx(state, topic="topic-x")
    assert first.policy_blockers == () and sent == []
    _bump(repo / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.60")   # dirty tree
    _no_git(monkeypatch, tmp_path)
    with pytest.raises(PolicySnapshotError, match="cannot run `git"):
        head_policy_snapshot(state, repo=repo)
    ctx = _live_ctx(state, topic="topic-x")
    assert ctx.policy.sha256 == first.policy.sha256 and ctx.policy.risk["ex_ante_vol_hard"] == 0.30
    assert ctx.policy_commit == first.policy_commit and default_policy() is ctx.policy
    assert [(b.code, b.scope) for b in ctx.policy_blockers] == [(POLICY_SNAPSHOT_UNAVAILABLE, "all")]
    assert engine_blockers(ctx) == [POLICY_SNAPSHOT_UNAVAILABLE]        # whole book: R20 holds every line
    assert sorted(sent) == [("macos", "Council: live policy snapshot unavailable", "urgent", None),
                            ("ntfy", "Council: live policy snapshot unavailable", "urgent", "topic-x")]
    _live_ctx(state, topic="topic-x")                                   # the watch 15 minutes later
    assert len(sent) == 2                                               # one alert per window
    later = utcnow() + POLICY_ALERT_EVERY + timedelta(minutes=1)
    assert alert_policy_snapshot(state, Settings(role="runner", mode="live", ntfy_topic="topic-x"), "held", now=later)
    assert len(sent) == 4


def test_without_git_or_a_verified_snapshot_a_live_run_alerts_then_refuses(repo, state, monkeypatch, tmp_path, sent):
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    snap = head_policy_snapshot(state, repo=repo)
    _bump(snap.directory / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.90")   # edited since
    _no_git(monkeypatch, tmp_path)
    assert last_verified_snapshot(state, repo=repo) is None
    with pytest.raises(PolicySnapshotError):
        _live_ctx(state, topic="topic-x")
    assert [(c, title, p) for c, title, p, _ in sent if c == "ntfy"] == [("ntfy", "Council: live runs stopped", "urgent")]


def test_the_fallback_is_only_this_checkouts_own_verified_snapshot(repo, state, tmp_path):
    head_policy_snapshot(state, repo=repo)
    fallback = last_verified_snapshot(state, repo=repo, reason="git gone")
    assert fallback is not None and fallback.fallback and "git gone" in fallback.blockers[0].detail
    other = tmp_path / "copy-of-checkout"
    shutil.copytree(repo, other)
    assert last_verified_snapshot(state, repo=other) is None             # another checkout's record
    assert last_verified_snapshot(tmp_path / "fresh-state", repo=repo) is None
    (state / POLICY_SNAPSHOTS / LAST_SNAPSHOT_FILE).write_text("{not json")
    assert last_verified_snapshot(state, repo=repo) is None


def test_the_approval_path_falls_back_only_when_asked_and_never_alerts(repo, state, monkeypatch, tmp_path, sent):
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    head_policy_snapshot(state, repo=repo)
    _no_git(monkeypatch, tmp_path)
    with pytest.raises(PolicySnapshotError):
        _approve_ctx(state)
    ctx = _approve_ctx(state, policy_fallback=True)
    assert [b.code for b in ctx.policy_blockers] == [POLICY_SNAPSHOT_UNAVAILABLE]
    assert sent == []


# ----------------------------------------------------------------------- git and file-system hygiene
def test_an_inherited_git_dir_cannot_redirect_the_snapshot(repo, state, tmp_path, monkeypatch):
    other = tmp_path / "other"
    make_sleeve_policy_dir(other / "policy", overlay=tmp_path / "no-overlay")
    _bump(other / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.99")
    _git(other, "init", "-q")
    _commit(other, "another repository")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert head_policy_snapshot(state, repo=repo).policy.risk["ex_ante_vol_hard"] == 0.30


def test_a_directory_nested_inside_another_checkout_is_refused(tmp_path, state):
    outer = tmp_path / "outer"
    make_sleeve_policy_dir(outer / "nested" / "policy", overlay=tmp_path / "no-overlay")
    _git(outer, "init", "-q")
    _commit(outer, "outer")
    with pytest.raises(PolicySnapshotError, match="not the top of a git checkout"):
        head_policy_snapshot(state, repo=outer / "nested")


def test_a_policy_tree_without_the_universe_is_refused(repo, state):
    (repo / "policy" / "universe.yaml").unlink()
    _commit(repo, "no universe")
    with pytest.raises(PolicySnapshotError, match="has no universe.yaml"):
        head_policy_snapshot(state, repo=repo)


def test_a_symlinked_snapshot_directory_is_replaced_not_followed(repo, state, tmp_path):
    tree = _git(repo, "rev-parse", "HEAD:policy")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not policy\n")
    (state / POLICY_SNAPSHOTS).mkdir(parents=True)
    (state / POLICY_SNAPSHOTS / tree).symlink_to(elsewhere, target_is_directory=True)
    snap = head_policy_snapshot(state, repo=repo)
    assert not snap.directory.is_symlink() and (snap.directory / "universe.yaml").is_file()
    assert [p.name for p in elsewhere.iterdir()] == ["keep.txt"]


def test_file_system_failures_are_snapshot_errors_and_the_record_is_best_effort(repo, state, monkeypatch):
    from council import runtime

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(runtime, "_materialise", full)
        with pytest.raises(PolicySnapshotError, match="No space left"):
            head_policy_snapshot(state, repo=repo)
    head_policy_snapshot(state, repo=repo)
    (state / POLICY_SNAPSHOTS / LAST_SNAPSHOT_FILE).unlink()
    with monkeypatch.context() as m:
        m.setattr(runtime.tempfile, "mkstemp", full)
        assert head_policy_snapshot(state, repo=repo).blockers == ()    # verified; only the record is lost
    assert not (state / POLICY_SNAPSHOTS / LAST_SNAPSHOT_FILE).exists()


# ------------------------------------------------------------- approval reads the installed release
def test_the_approval_path_reads_the_installed_release_not_the_operators_checkout(repo, state, tmp_path,
                                                                                   monkeypatch):
    release = tmp_path / "releases-v1"
    _git(tmp_path, "clone", "-q", str(repo), str(release))
    (state / "releases").mkdir(parents=True)
    (state / "releases" / "current").symlink_to(release, target_is_directory=True)
    _bump(repo / "policy" / "risk.yaml", "ex_ante_vol_hard: 0.30", "ex_ante_vol_hard: 0.25")
    _commit(repo, "the operator's dev checkout moves on")
    monkeypatch.setattr(paths, "REPO_ROOT", repo)
    approve = _approve_ctx(state)
    assert approve.policy.risk["ex_ante_vol_hard"] == 0.30
    assert approve.policy_commit == _git(release, "rev-parse", "HEAD")
    live = _live_ctx(state)                              # the runner reads its own checkout
    assert live.policy.risk["ex_ante_vol_hard"] == 0.25
