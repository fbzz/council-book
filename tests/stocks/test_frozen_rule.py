"""The adopted rule runs the studied bytes (spec item L7).

The four rule modules, the stock package's init file, the study script, the spec and the variants file
must stay byte for byte what the tag `stock-sleeve-spec` froze. The pins are git blob SHA-1s taken with
`git rev-parse stock-sleeve-spec:<path>` when the rule was adopted; the main test hashes the working
files itself (no git call), so it also runs in a shallow CI checkout without tags. A bug found in any of
these files needs a new pre-registration (v2), never an in-place edit.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess

import pytest
import yaml

from council.paths import REPO_ROOT
from council.stocks import adopted

TAG = "stock-sleeve-spec"

RULE_MODULES = {
    "src/council/stocks/__init__.py": "d83cdd508bf4db45442b682d450671d88d3b513d",
    "src/council/stocks/pit.py": "e1ba0256e52f804ff2a19d0bff90390b4e6a76c5",
    "src/council/stocks/score.py": "51ba173686e42557e628414c62292efa1c2a01d9",
    "src/council/stocks/sectors.py": "fd9e8619617efa2e5aae188cf718b35f5a904c04",
    "src/council/reference/sleeve.py": "6e767cec2e99658d7d6dd671caf247ce290f5043",
}
STUDY_RECORD = {
    "scripts/stock_sleeve_study.py": "044c5f8c379670899e878b406eb5fcd96befa695",
    "docs/stock-sleeve-spec.md": "5b2316b505c2bfffc5eeef2bfa763a86c8c95d6e",
    "policy/variants/stock-sleeve-variants-v1.yaml": "a0caf92ccb35268f249989e2e42e8a8d3c693514",
}
FROZEN = {**RULE_MODULES, **STUDY_RECORD}


def git_blob_sha1(data: bytes) -> str:
    """The object id git gives a file's bytes (`git hash-object`)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.mark.parametrize("path", sorted(FROZEN))
def test_frozen_file_is_the_tagged_blob(path):
    data = (REPO_ROOT / path).read_bytes()
    assert git_blob_sha1(data) == FROZEN[path], (
        f"{path} differs from its blob at tag {TAG}: the adopted stock rule is frozen; "
        "a change needs a new pre-registration (v2), not an edit")


def test_the_study_script_names_exactly_these_rule_modules():
    text = (REPO_ROOT / "scripts" / "stock_sleeve_study.py").read_text()
    match = re.search(r"^RULE_MODULES\b[^=]*=\s*\((.*?)\)", text, re.S | re.M)
    assert match, "RULE_MODULES not found in the study script"
    named = {m.replace("\\", "/") for m in re.findall(r"[\"']([^\"']+\.py)[\"']", match.group(1))}
    assert named == set(RULE_MODULES) - {"src/council/stocks/__init__.py"}


def test_rule_module_sha256_match_the_frozen_inputs_of_the_variants_file():
    """A second, independent pin: the variants file (itself pinned above) recorded the SHA-256 of the
    study script and the rule modules just before the tag."""
    variants = yaml.safe_load((REPO_ROOT / "policy/variants/stock-sleeve-variants-v1.yaml").read_text())
    code = variants["frozen_inputs"]["code_sha256"]
    assert set(code) == set(RULE_MODULES) - {"src/council/stocks/__init__.py"} | {"scripts/stock_sleeve_study.py"}
    for path, digest in code.items():
        assert hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() == digest, path


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                              check=False, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def test_pins_are_the_tag_blobs_when_the_tag_is_present():
    """Where the tag exists locally (not in CI's shallow clone), the constants above are exactly
    `git rev-parse stock-sleeve-spec:<path>` and the tag points at the recorded commit."""
    commit = _git("rev-parse", "--verify", "--quiet", f"{TAG}^{{commit}}")
    if commit is None or commit.returncode != 0:
        pytest.skip(f"tag {TAG} is not available in this checkout")
    assert commit.stdout.strip() == adopted.SPEC_COMMIT
    tag_object = _git("rev-parse", "--verify", "--quiet", f"refs/tags/{TAG}")
    assert tag_object is not None and tag_object.stdout.strip() == adopted.SPEC_TAG_OBJECT
    for path, sha in FROZEN.items():
        blob = _git("rev-parse", "--verify", "--quiet", f"{TAG}:{path}")
        assert blob is not None and blob.returncode == 0, path
        assert blob.stdout.strip() == sha, path
