"""Vehicle symbol → broker instrument id, persisted privately at `<state_dir>/instruments.json`.

Rules:
- Resolution is by EXACT symbol through the eligibility route (search entitlement may be blocked).
- The map is immutable: re-verification may refresh `verified_at`, but a symbol whose instrument id
  changes — or a new symbol claiming an id another symbol owns — raises InstrumentIdentityChanged
  and nothing is written (the caller freezes that instrument).
- The file is written atomically (temp file + rename), mode 0600, and never inside the repo.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from council.models.broker import EligibilityRow
from council.models.common import Frozen
from council.paths import assert_outside_repo, state_dir

FILE_NAME = "instruments.json"
FORMAT_VERSION = 1


class InstrumentIdentityChanged(RuntimeError):
    def __init__(self, symbol: str, old_id: int | None, new_id: int, detail: str = "") -> None:
        super().__init__(detail or f"instrument identity changed for {symbol}")
        self.symbol = symbol
        self.old_id = old_id
        self.new_id = new_id


class InstrumentEntry(Frozen):
    instrument_id: int
    resolved_at: datetime
    verified_at: datetime


class EligibilityReader(Protocol):
    def eligibility(
        self, symbols: list[str] | None = None, instrument_ids: list[int] | None = None
    ) -> list[EligibilityRow]: ...


def default_path() -> Path:
    return state_dir() / FILE_NAME


class InstrumentMap:
    """Read-only mapping; `merged` returns a new map, `save` persists it."""

    def __init__(
        self,
        entries: Mapping[str, InstrumentEntry] | None = None,
        *,
        path: Path | None = None,
        unresolved: Iterable[str] = (),
    ) -> None:
        self._entries = MappingProxyType(dict(entries or {}))
        self.path = path or default_path()
        self.unresolved: tuple[str, ...] = tuple(unresolved)

    # --------------------------------------------------------------------------- queries
    @property
    def entries(self) -> Mapping[str, InstrumentEntry]:
        return self._entries

    def get(self, symbol: str) -> int | None:
        entry = self._entries.get(symbol)
        return entry.instrument_id if entry else None

    def symbol_for(self, instrument_id: int) -> str | None:
        for symbol, entry in self._entries.items():
            if entry.instrument_id == instrument_id:
                return symbol
        return None

    def ids(self) -> dict[str, int]:
        return {s: e.instrument_id for s, e in self._entries.items()}

    def symbols_by_id(self) -> dict[int, str]:
        return {e.instrument_id: s for s, e in self._entries.items()}

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    # --------------------------------------------------------------------------- updates
    def merged(self, found: Mapping[str, int], now: datetime, *, unresolved: Iterable[str] = ()) -> InstrumentMap:
        """A new map with `found` added/verified. Identity changes raise; nothing is mutated."""
        entries = dict(self._entries)
        owners = {e.instrument_id: s for s, e in entries.items()}
        for symbol, instrument_id in found.items():
            existing = entries.get(symbol)
            if existing is not None and existing.instrument_id != instrument_id:
                raise InstrumentIdentityChanged(symbol, existing.instrument_id, instrument_id)
            owner = owners.get(instrument_id)
            if owner is not None and owner != symbol:
                raise InstrumentIdentityChanged(
                    symbol, None, instrument_id,
                    f"instrument id already mapped to {owner}; refusing {symbol}",
                )
            entries[symbol] = InstrumentEntry(
                instrument_id=instrument_id,
                resolved_at=existing.resolved_at if existing else now,
                verified_at=now,
            )
            owners[instrument_id] = symbol
        return InstrumentMap(entries, path=self.path, unresolved=unresolved)

    # --------------------------------------------------------------------------- persistence
    @classmethod
    def load(cls, path: Path | None = None) -> InstrumentMap:
        path = path or default_path()
        if not path.exists():
            return cls({}, path=path)
        data = json.loads(path.read_text())
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(f"unsupported instruments.json version {data.get('version')!r}")
        entries = {
            symbol: InstrumentEntry.model_validate(raw)
            for symbol, raw in (data.get("instruments") or {}).items()
        }
        return cls(entries, path=path)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": FORMAT_VERSION,
            "instruments": {
                s: e.model_dump(mode="json") for s, e in sorted(self._entries.items())
            },
        }

    def save(self) -> Path:
        assert_outside_repo(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".instruments.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.to_json(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path


def resolve(
    read_client: EligibilityReader,
    symbols: Iterable[str],
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> InstrumentMap:
    """Resolve exact symbols through eligibility, verify against the stored map, persist.

    Returns the new map; symbols the broker did not return are listed in `.unresolved`."""
    wanted = list(dict.fromkeys(symbols))
    current = InstrumentMap.load(path)
    if not wanted:
        return current
    rows = read_client.eligibility(symbols=wanted)
    by_upper: dict[str, EligibilityRow] = {}
    for row in rows:
        by_upper.setdefault(row.symbol.upper(), row)
    found: dict[str, int] = {}
    missing: list[str] = []
    for symbol in wanted:
        row = by_upper.get(symbol.upper())
        if row is None:
            missing.append(symbol)
        else:
            found[symbol] = row.instrument_id
    updated = current.merged(found, now or datetime.now(UTC), unresolved=missing)
    updated.save()
    return updated
