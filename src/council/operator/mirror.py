"""The mirror ratio: how the Agent Portfolio (virtual book) maps to the real account that copies it.

`council account set-mirror` (operator terminal only) stores it in `state_dir/account/mirror.json`
(0600, directory 0700, never inside the repository). The runner only READS it
(`runtime.cycle_trade_economics`) to price the $1 fixed fee and the real-dollar trade floor as NAV
shares; a missing file means the policy's assumed ratio and the flag `mirror_ratio_missing`.

Rules:
- `mirror_ratio` = real funding / virtual NAV, a positive finite number <= 10. Given directly
  (`--ratio`), or as `--funding-usd` / `--virtual-nav-usd`.
- The file is private account data (it bounds the real NAV). Nothing here prints, logs or
  publishes it; the command echoes only what the operator typed back to the operator's terminal.
- Writes are atomic (temp file + rename) and never follow a symlink.

This module imports no broker client and places nothing: the unattended runner may import it.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from council.clock import utcnow
from council.paths import assert_outside_repo

MIRROR_DIR = "account"
MIRROR_FILE = "mirror.json"
MAX_RATIO = 10.0
VERSION = 1


class MirrorError(ValueError):
    """The mirror input or file is unusable."""


@dataclass(frozen=True)
class MirrorConfig:
    mirror_ratio: float
    set_at: datetime
    funding_usd: float | None = None
    virtual_nav_usd: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"version": VERSION, "mirror_ratio": self.mirror_ratio, "funding_usd": self.funding_usd,
                "virtual_nav_usd": self.virtual_nav_usd, "set_at": self.set_at.isoformat()}


def mirror_path(state_dir: Path) -> Path:
    return state_dir / MIRROR_DIR / MIRROR_FILE


def _positive(name: str, value: Any, *, upper: float | None = None) -> float:
    if isinstance(value, bool):
        raise MirrorError(f"{name} must be a number")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise MirrorError(f"{name} must be a number") from exc
    if not math.isfinite(out) or out <= 0:
        raise MirrorError(f"{name} must be positive and finite")
    if upper is not None and out > upper:
        raise MirrorError(f"{name} must be at most {upper:g}")
    return out


def resolve_ratio(*, ratio: float | None = None, funding_usd: float | None = None,
                  virtual_nav_usd: float | None = None) -> tuple[float, float | None, float | None]:
    """(ratio, funding, virtual NAV) from exactly one way of stating it."""
    if ratio is not None:
        if virtual_nav_usd is not None and funding_usd is None:
            raise MirrorError("--virtual-nav-usd needs --funding-usd")
        value = _positive("ratio", ratio, upper=MAX_RATIO)
        funding = _positive("funding", funding_usd) if funding_usd is not None else None
        nav = _positive("virtual NAV", virtual_nav_usd) if virtual_nav_usd is not None else None
        if funding is not None and nav is not None and not math.isclose(funding / nav, value, rel_tol=1e-6):
            raise MirrorError("ratio differs from funding / virtual NAV")
        return value, funding, nav
    if funding_usd is None or virtual_nav_usd is None:
        raise MirrorError("pass --ratio, or both --funding-usd and --virtual-nav-usd")
    funding = _positive("funding", funding_usd)
    nav = _positive("virtual NAV", virtual_nav_usd)
    return _positive("ratio", funding / nav, upper=MAX_RATIO), funding, nav


def load_mirror(state_dir: Path) -> MirrorConfig | None:
    """The stored mirror ratio, or None when no file exists. Raises MirrorError when the file is
    unreadable, a symlink, or holds an invalid ratio (the caller then uses the assumed ratio)."""
    path = mirror_path(state_dir)
    if path.is_symlink():
        raise MirrorError("mirror file is a symlink")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MirrorError("mirror file is unreadable") from exc
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise MirrorError("mirror file has an unknown format")
    ratio = _positive("ratio", data.get("mirror_ratio"), upper=MAX_RATIO)
    try:
        set_at = datetime.fromisoformat(str(data.get("set_at")))
    except ValueError as exc:
        raise MirrorError("mirror file has no valid set_at") from exc
    funding = data.get("funding_usd")
    nav = data.get("virtual_nav_usd")
    return MirrorConfig(
        mirror_ratio=ratio, set_at=set_at,
        funding_usd=_positive("funding", funding) if funding is not None else None,
        virtual_nav_usd=_positive("virtual NAV", nav) if nav is not None else None,
    )


def set_mirror(state_dir: Path, *, ratio: float | None = None, funding_usd: float | None = None,
               virtual_nav_usd: float | None = None, now: datetime | None = None) -> MirrorConfig:
    """Validate and store the mirror ratio atomically (0600 file in a 0700 directory)."""
    value, funding, nav = resolve_ratio(ratio=ratio, funding_usd=funding_usd, virtual_nav_usd=virtual_nav_usd)
    stamp = now or utcnow()
    if stamp.tzinfo is None:
        raise MirrorError("naive datetime; council code uses aware UTC datetimes only")
    config = MirrorConfig(mirror_ratio=value, set_at=stamp, funding_usd=funding, virtual_nav_usd=nav)
    path = mirror_path(state_dir)
    assert_outside_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        path.unlink()                      # never write through a planted link
    fd, tmp = tempfile.mkstemp(prefix=".mirror-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(config.to_json(), fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return config
