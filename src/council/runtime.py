"""Runtime wiring shared by `cycle`, `watch` and the CLI: injectable context, data sources, cost
quotes from policy floors, the material-change fingerprint, and the single-instance lock.

Nothing here can place an order. The broker source exposes READ methods only.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from council import paths
from council.clock import utcnow
from council.models.broker import CostQuote, EligibilityRow
from council.models.cards import EvidenceCard
from council.models.facts import EventItem, FactPack, NewsItem
from council.policy import LineSpec, Policy
from council.settings import Settings

MIN_FREE_BYTES = 3 * 1024**3


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


@dataclass
class Sources:
    """Where a cycle's inputs come from. Every function receives the SLOT, never the wall clock."""

    history: HistoryFn
    events: EventsFn
    macro: MacroFn | None = None
    news: NewsFn | None = None
    broker: BrokerRead | None = None          # None = AWAITING ACCOUNT (no snapshot, no plan)


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


# ------------------------------------------------------------------------------- cost quotes
def default_settlement(line: LineSpec, direction: str, leverage: int) -> str:
    """Before eligibility is known: long 1x uses the first long candidate; shorts and leverage
    are CFDs by construction (the only vehicles that support them)."""
    if direction == "long" and leverage == 1 and line.vehicles.long:
        return line.vehicles.long[0].settlement
    return "cfd"


def vehicle_asset_class(line: LineSpec, settlement: str) -> str:
    """Cost floors are keyed by the VEHICLE's class: an index line traded as a UCITS ETF is 'etf'."""
    if line.asset_class == "crypto":
        return "crypto"
    if settlement == "real":
        return "etf"
    return line.asset_class


def floor_cost_quotes(policy: Policy, *, quoted_at: datetime) -> dict[tuple[str, str, int], CostQuote]:
    """Cost quotes from policy floors for every (line, direction, leverage) the council may use.

    Used before the broker is connected and as the floor under broker what-if quotes afterwards."""
    from council.risk.costs import carry_bps_day, per_side_bps

    out: dict[tuple[str, str, int], CostQuote] = {}
    for line in policy.universe.lines:
        combos: list[tuple[str, int]] = [("long", 1)]
        if line.council_deviations:
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
            )
    return out


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
def material_fingerprint(pack: FactPack, cards: Sequence[EvidenceCard], kill_state: str) -> str:
    """What must change before a NEW discretionary target is executable: trend states, the
    admitted set, qualifying card content (not per-cycle card numbers), active events, kill state."""
    trend = {s: st.trend for s, st in sorted(pack.states.items())}
    qual = sorted(
        (c.card_type, tuple(sorted(c.scope)), c.direction)
        for c in cards if c.qualifying
    )
    events = sorted(e.id for e in pack.events)
    blob = json.dumps({"trend": trend, "admitted": sorted(pack.admitted), "cards": qual,
                       "events": events, "kill": kill_state}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def first_cycle_of_utc_day(last_macro_day: str | None, slot: datetime) -> bool:
    return last_macro_day != slot.date().isoformat()


def window(slot: datetime, *, back_h: int = 24, ahead_days: int = 7) -> tuple[datetime, datetime]:
    return slot - timedelta(hours=back_h), slot + timedelta(days=ahead_days)
