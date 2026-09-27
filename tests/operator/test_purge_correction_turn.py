"""The purge also filters the correction turn of a capture (the checker's errors and the correction
message built from them can echo a reply that copied licensed text)."""

from __future__ import annotations

import gzip
from datetime import timedelta
from pathlib import Path

import pytest

from council import paths
from council.deliberation.capture import calls_path, load_inputs, write_private
from council.operator.purge import REPLY_PLACEHOLDER, purge_licensed

from ..council.factories import SLOT
from .conftest import CANARY_TITLE, capture_cycle

NOW = SLOT + timedelta(days=1)


@pytest.fixture
def root() -> Path:
    r = paths.state_dir()
    r.mkdir(parents=True, exist_ok=True)
    return r


def test_the_correction_turn_is_filtered_like_the_replies(root):
    cap = capture_cycle(root, canary=True)
    inputs = load_inputs(root, cap.cycle_id)
    echo = f"claims.0.claim: too long: {CANARY_TITLE}"
    calls = [c.model_copy(update={"errors": [echo, "claims.1: missing"],
                                  "correction": f"Problems:\n- {echo}", "error": echo})
             if c.role == "bear" else c for c in inputs.calls]
    write_private(root, calls_path(root, cap.cycle_id),
                  gzip.compress(inputs.model_copy(update={"calls": calls}).model_dump_json().encode(), mtime=0))

    receipt = purge_licensed(root, now=NOW, purge_all=True)
    assert receipt.errors == []
    bear = next(c for c in load_inputs(root, cap.cycle_id).calls if c.role == "bear")
    assert bear.errors == [REPLY_PLACEHOLDER, "claims.1: missing"]
    assert bear.correction == REPLY_PLACEHOLDER and bear.error == REPLY_PLACEHOLDER
    raw = gzip.decompress(calls_path(root, cap.cycle_id).read_bytes()).decode().lower()
    assert "canarybird" not in raw
