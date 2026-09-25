from __future__ import annotations

import pytest

from council.paths import REPO_ROOT
from council.publish.gitops import Publisher, PublishError, check_journal_path
from tests.publish.conftest import git

NOREPLY = "18754232+fbzz@users.noreply.github.com"
FILES = {"journal/status.json": b'{"state": "LIVE"}\n',
         "journal/ops/cycles.jsonl": b'{"cycle_id":"2026-10-01T1440Z"}\n'}


def _publisher(clone, **kw):
    kw.setdefault("push", False)
    return Publisher(clone, sleep=lambda s: None, **kw)


def test_commits_only_journal_paths_with_a_noreply_identity(publisher_clone):
    clone, _ = publisher_clone
    result = _publisher(clone).publish(FILES, "journal: cycle 2026-10-01T1440Z")
    assert result.commit_sha and not result.pushed
    assert git(clone, "log", "-1", "--format=%an|%ae|%cn|%ce").strip() == \
        f"council-publisher|{NOREPLY}|council-publisher|{NOREPLY}"
    changed = git(clone, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(changed) == sorted(FILES)


def test_pushes_to_origin(publisher_clone):
    clone, origin = publisher_clone
    result = _publisher(clone, push=True).publish(FILES, "journal: status")
    assert result.pushed
    assert git(origin, "rev-parse", "main").strip() == result.commit_sha


def test_push_retries_after_a_rejected_push(publisher_clone, tmp_path):
    clone, origin = publisher_clone
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "journal" / "incidents").mkdir(parents=True)
    (other / "journal" / "incidents" / "INC-0001.md").write_text("x\n")
    git(other, "add", "journal/incidents/INC-0001.md")
    git(other, "commit", "-q", "-m", "other publisher")
    git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
    result = _publisher(clone, push=True).publish(FILES, "journal: status")
    assert result.pushed
    assert git(origin, "rev-parse", "main").strip() == result.commit_sha
    assert "other publisher" in git(origin, "log", "--format=%s", "main")


@pytest.mark.parametrize("path", [
    "src/council/evil.py", "README.md", "journal", "journal/", "/journal/x.json", "journal/../src/x.py",
    "journal/./x.json", "journal\\x.json", "journalx/y.json", "",
])
def test_refuses_paths_outside_journal(publisher_clone, path):
    clone, _ = publisher_clone
    before = git(clone, "rev-parse", "HEAD")
    with pytest.raises(PublishError):
        _publisher(clone).publish({path: b"x"}, "journal: x")
    assert git(clone, "rev-parse", "HEAD") == before


def test_refuses_when_something_else_is_already_staged(publisher_clone):
    clone, _ = publisher_clone
    (clone / "src.py").write_text("print('x')\n")
    git(clone, "add", "src.py")
    before = git(clone, "rev-parse", "HEAD")
    with pytest.raises(PublishError, match="already has staged changes"):
        _publisher(clone).publish(FILES, "journal: x")
    assert git(clone, "rev-parse", "HEAD") == before


def test_leak_in_a_file_aborts_before_git(publisher_clone):
    clone, _ = publisher_clone
    before = git(clone, "rev-parse", "HEAD")
    with pytest.raises(PublishError, match="leak scan"):
        _publisher(clone).publish({"journal/status.json": b'{"note": "equity $1,234.56"}'}, "journal: x")
    assert git(clone, "rev-parse", "HEAD") == before
    assert not (clone / "journal" / "status.json").exists()


def test_leak_in_the_commit_message_aborts(publisher_clone):
    clone, _ = publisher_clone
    with pytest.raises(PublishError):
        _publisher(clone).publish(FILES, "journal: position 2951234567")


def test_canaries_are_enforced(publisher_clone):
    clone, _ = publisher_clone
    with pytest.raises(PublishError):
        _publisher(clone, canaries=(1234.56,)).publish({"journal/status.json": b'{"note": "1,234.56"}'}, "journal: x")


def test_symlink_out_of_journal_is_refused(publisher_clone, tmp_path):
    clone, _ = publisher_clone
    outside = tmp_path / "outside"
    outside.mkdir()
    (clone / "journal" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PublishError, match="link"):
        _publisher(clone).publish({"journal/link/x.json": b"{}"}, "journal: x")
    assert not (outside / "x.json").exists()


def test_unchanged_files_make_no_commit(publisher_clone):
    clone, _ = publisher_clone
    _publisher(clone).publish(FILES, "journal: first")
    result = _publisher(clone).publish(FILES, "journal: same again")
    assert result.commit_sha is None


def test_dry_run_writes_to_a_directory_and_never_runs_git(tmp_path):
    def runner(*a, **k):
        raise AssertionError("git must not run in dry-run mode")

    out = tmp_path / "preview"
    result = Publisher(tmp_path / "no-clone", push=True, dry_run_dir=out, runner=runner).publish(FILES, "journal: x")
    assert result.dry_run and result.commit_sha is None
    assert (out / "journal" / "status.json").read_bytes() == FILES["journal/status.json"]


def test_refuses_the_development_tree():
    with pytest.raises(PublishError, match="development tree"):
        Publisher(REPO_ROOT, push=False).publish(FILES, "journal: x")


@pytest.mark.parametrize("author", [("council-publisher", "someone@outlook.com"), ("", NOREPLY),
                                    ("a<b>", NOREPLY), ("x", "a b@users.noreply.github.com")])
def test_identity_must_be_noreply(tmp_path, author):
    with pytest.raises(PublishError):
        Publisher(tmp_path, push=False, author=author)


def test_check_journal_path_normalises():
    assert check_journal_path("journal/cycles/2026/10/x.json") == "journal/cycles/2026/10/x.json"


def test_push_gives_up_after_three_attempts(publisher_clone):
    clone, _ = publisher_clone
    git(clone, "remote", "set-url", "origin", str(clone.parent / "does-not-exist.git"))
    sleeps = []
    result = Publisher(clone, push=True, sleep=sleeps.append).publish(FILES, "journal: status")
    assert result.commit_sha and not result.pushed
    assert sleeps == [2.0, 4.0]                       # 3 attempts, 2 waits
