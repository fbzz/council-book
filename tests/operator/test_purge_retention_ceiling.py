"""The once-a-day purge keeps every licensed copy under 7 days, and private folders are 0700.

The daily purge runs only on the first cycle of each UTC day. With a cut-off of the full 7 days,
a capture made just after one day's purge would survive until the purge eight days later; the
daily cut-off is therefore one day under the retention.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from council import paths
from council.deliberation.capture import licensed_path, write_private
from council.operator.purge import (
    LICENSED_RETENTION_DAYS,
    daily_cutoff_days,
    maybe_daily_purge,
)

from .conftest import capture_cycle


@pytest.fixture
def root() -> Path:
    r = paths.state_dir()
    r.mkdir(parents=True, exist_ok=True)
    return r


def test_daily_cutoff_is_one_day_under_the_retention():
    assert daily_cutoff_days() == LICENSED_RETENTION_DAYS - 1


def test_no_licensed_copy_outlives_seven_days_between_daily_runs(root):
    day0 = datetime(2026, 10, 1, 0, 40, tzinfo=UTC)
    assert maybe_daily_purge(root, day0) == []
    # captured just after day 0's purge
    cap = capture_cycle(root, slot=day0 + timedelta(hours=4), canary=True)
    assert licensed_path(root, cap.cycle_id).exists()
    for d in range(1, 8):
        now = day0 + timedelta(days=d)
        assert maybe_daily_purge(root, now) == []
        age = now - (day0 + timedelta(hours=4))
        if licensed_path(root, cap.cycle_id).exists():
            # still held: the next daily run (24h later) must not find it past 7 days
            assert age + timedelta(days=1) <= timedelta(days=LICENSED_RETENTION_DAYS)
    assert not licensed_path(root, cap.cycle_id).exists()


def test_an_existing_loose_private_folder_is_tightened(root):
    loose = root / "calls"
    loose.mkdir(mode=0o755, exist_ok=True)
    os.chmod(loose, 0o755)
    target = loose / "2026" / "10" / "x.json.gz"
    write_private(root, target, b"x")
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
