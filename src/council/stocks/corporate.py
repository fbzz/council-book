"""Corporate actions on stock lines (design §3.6): detection, the `council stocks adopt` flows, the
vanished-position classification, and the credited-position reconcile rule. Read-only toward the
broker; nothing here writes `policy/` (adopt proposes a sleeve-file edit under the state dir).

Detection (run by the cycle start, `cycle.corporate_actions`, and `council stocks status`; `detect` =
`pending_actions` + `blockers` + `retired_held` + `alerts`; only while `sleeve_active`):
- A broker position whose instrument maps to no line (`UNMAPPED_<id>`, or a symbol no line owns)
  and that no open leg of ours created is a pending corporate action (a spin-off or stock-for-stock
  credit). It raises ONE satellite-scoped blocker, `satellite:corporate_action_pending`: the stock
  sleeve is held, the core keeps running. The instrument ids go only into the operator's URGENT
  alert (`alerts`), never into a blocker string (blocker strings reach the public record).
- A position on a vehicle of the retired registry raises `satellite:retired_line_held:<line>`.

`plan_adopt` (instrument id, READ token): eligibility by instrument, the SEC CIK of its symbol,
then exactly one of (inferred, or forced with `kind` and checked):
- credit: a position is held and no line owns the instrument or the company: a new `retiring` line
  with `credited: corporate_action` (sold at the next US session as a reference-origin full exit).
- rename: a line owns the instrument (same instrument id under another symbol) or the company (same
  CIK) and the broker symbol changed: the line keeps its id for the quarter, its broker and history
  symbols change; a same-id rename is recorded for `council stocks onboard`, which writes the
  explicit alias into instruments.json (never inferred). The next rank re-keys the line by CIK.
  The renamed instrument must pass the full gate; one that can only be closed becomes `retiring`
  (sold at the next US session), never an unchecked line (live runs refuse those).
- delisted: a line's instrument is gone or closed to opens and no position is held (a cash
  takeover): the line becomes `retiring` (target 0; the sleeve keeps the cash until the next rank,
  as the spec's delisting rule) and the private record makes it `untradable:<line>`.
  (The sleeve schema has no `delisted` role yet; `retiring` gives the same book: target 0, band
  [0, 0], no re-buy.)

Vanished positions (`classify_vanished`): a stock position that disappeared without one of our
closes is a stop hit only when the last observed bid was at or below its stop-loss rate or above it
by at most 2 x the 4-hour sigma, or the trading history says the stop closed it; otherwise
`vanished_not_stop` (URGENT, no R4d cool-off). Missing data is ambiguous and counts as a stop hit
(conservative).

Reconcile (`reconcile_credited`): a position on a line with `credited: corporate_action` and no
stop-loss is the warning `credited_no_sl:<line>`, not a blocker; every other missing stop still
blocks. `reconcile_corporate` adds the pending actions: an unknown position no leg of ours opened
is the satellite-scoped `corporate_action_pending`, not a whole-book `blocked` decision.

The private record (`<state_dir>/stocks/corporate.json`, 0600) keeps the actions adopt proposed.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from council.ledger.states import LEG_STATES, SATELLITE_BLOCKER_PREFIX
from council.models.broker import EligibilityRow, Position
from council.paths import assert_outside_repo
from council.policy import Policy, SleeveLine, StockSleeveFile
from council.stocks import sleeve_file
from council.stocks.eligibility import Verdict

PENDING = "corporate_action_pending"
RETIRED_HELD = "retired_line_held"
CREDITED_NO_SL = "credited_no_sl"
VANISHED_NOT_STOP = "vanished_not_stop"
STOP_HIT = "stop_hit"
UNTRADABLE = "untradable"
STOP_BAND_SIGMAS = 2.0
RECORD_FILE = Path("stocks") / "corporate.json"
UNMAPPED_PREFIX = "UNMAPPED_"
Kind = Literal["credit", "rename", "delisted"]
KINDS: tuple[Kind, ...] = ("credit", "rename", "delisted")


class AdoptError(ValueError):
    """`council stocks adopt` cannot act: nothing to adopt, an ambiguous case, or a fact missing."""


# ------------------------------------------------------------------------------------ detection


@dataclass(frozen=True)
class PendingAction:
    instrument_id: int          # PRIVATE
    position_id: int            # PRIVATE


def opened_position_ids(ledger: Any) -> set[int]:
    """Position ids created by our own open legs (any leg state), from the ledger."""
    out: set[int] = set()
    for row in ledger.legs_in_states(sorted(LEG_STATES)):
        if row.kind != "open":
            continue
        out |= {int(p) for p in (row.position_ids or [])}
        if row.position_id is not None:
            out.add(int(row.position_id))
    return out


def _owner(policy: Policy) -> dict[str, str]:
    return policy.universe.vehicle_map()


def sleeve_active(policy: Policy) -> bool:
    """The policy has a stock sleeve (stock lines or a sleeve file): only then do the cycle start, the
    watch and the reconciles apply the corporate-action rules. A core-only book keeps today's rules:
    an unknown position blocks, a vanished position is a stop hit."""
    return policy.universe.stock_sleeve is not None or bool(policy.universe.stock_lines())


def pending_actions(positions: Iterable[Position], policy: Policy, *, opened: Iterable[int]) -> list[PendingAction]:
    """Positions on instruments no line owns that no open leg of ours created."""
    owners = _owner(policy)
    retired = {v for r in (policy.universe.stock_sleeve.retired if policy.universe.stock_sleeve else ())
               for v in r.vehicles}
    ours = {int(p) for p in opened}
    out = []
    for p in positions:
        mapped = not p.symbol.startswith(UNMAPPED_PREFIX) and p.symbol in owners
        if mapped or p.symbol in retired or p.position_id in ours:
            continue
        out.append(PendingAction(instrument_id=int(p.instrument_id), position_id=int(p.position_id)))
    return out


def blockers(pending: Sequence[PendingAction]) -> list[str]:
    """The satellite-scoped blocker for pending corporate actions (no identifiers in it)."""
    return [f"{SATELLITE_BLOCKER_PREFIX}{PENDING}"] if pending else []


def alerts(pending: Sequence[PendingAction]) -> list[str]:
    """URGENT operator alerts (private ntfy): each names the instrument and the command to run."""
    ids = sorted({a.instrument_id for a in pending})
    return [f"URGENT corporate action: an unknown position on instrument {i}; the stock sleeve is held. "
            f"Run `council stocks adopt {i}`" for i in ids]


def retired_held(positions: Iterable[Position], policy: Policy) -> list[str]:
    """`satellite:retired_line_held:<line>` for a position on a vehicle of the retired registry."""
    sleeve = policy.universe.stock_sleeve
    if sleeve is None:
        return []
    held = {p.symbol for p in positions}
    return sorted({f"{SATELLITE_BLOCKER_PREFIX}{RETIRED_HELD}:{r.symbol}" for r in sleeve.retired
                   if set(r.vehicles) & held})


@dataclass(frozen=True)
class Detection:
    """What the cycle start and the watch act on: satellite-scoped R20 blockers (public-safe codes)
    and the operator's URGENT alerts (private: they name instrument ids)."""

    pending: tuple[PendingAction, ...]
    blockers: tuple[str, ...]
    alerts: tuple[str, ...]


def detect(positions: Sequence[Position], policy: Policy, *, opened: Iterable[int]) -> Detection:
    """Unknown positions no leg of ours opened (a corporate action) and positions on retired
    vehicles, as satellite-scoped blockers; the core is never held by either."""
    pending = pending_actions(positions, policy, opened=opened)
    held = retired_held(positions, policy)
    notes = [f"URGENT {b.removeprefix(SATELLITE_BLOCKER_PREFIX)}: a position on a retired line; "
             "re-add the line as retiring with a tagged edit" for b in held]
    return Detection(pending=tuple(pending), blockers=tuple([*blockers(pending), *held]),
                     alerts=tuple([*alerts(pending), *notes]))


# ------------------------------------------------------------------------------------ vanished


@dataclass(frozen=True)
class Observation:
    """What the watch last saw of a position (PRIVATE: rates)."""

    symbol: str
    sl_rate: float | None = None
    bid: float | None = None


def classify_vanished(*, last_bid: float | None, sl_rate: float | None, sigma_4h: float | None,
                      closed_by_sl: bool | None = None) -> Literal["stop_hit", "vanished_not_stop"]:
    """Stop hit when the history says the stop closed it, when the last observed bid was at or
    below the stop rate (stock lines are long only: a gap through the stop, however deep, is the stop
    firing), or when it was above the stop by at most STOP_BAND_SIGMAS x the 4-hour sigma
    (relative); missing data counts as a stop hit (ambiguous -> conservative). Otherwise
    `vanished_not_stop` (no R4d cool-off): the bid was far ABOVE the stop, e.g. a cash takeover."""
    if closed_by_sl:
        return STOP_HIT
    values = (last_bid, sl_rate, sigma_4h)
    if any(v is None for v in values) or last_bid <= 0 or sl_rate <= 0 or sigma_4h <= 0:  # type: ignore[operator]
        return STOP_HIT
    bid, stop = float(last_bid), float(sl_rate)  # type: ignore[arg-type]
    if bid <= stop:
        return STOP_HIT
    distance = (bid - stop) / bid
    return STOP_HIT if distance <= STOP_BAND_SIGMAS * float(sigma_4h) else VANISHED_NOT_STOP  # type: ignore[arg-type]


@dataclass(frozen=True)
class Vanished:
    position_id: int
    symbol: str
    line: str
    outcome: Literal["stop_hit", "vanished_not_stop"]


def classify_vanished_positions(seen: Mapping[int, Observation], live_ids: Iterable[int], ours: Iterable[int],
                                policy: Policy, *, sigma_4h: Mapping[str, float | None],
                                closed_by_sl: Mapping[int, bool] | None = None) -> list[Vanished]:
    """Every expected position that vanished without one of our closes, classified. Non-stock lines
    keep today's rule (a stop hit); `sigma_4h` is keyed by line."""
    owners = _owner(policy)
    stocks = {ln.symbol for ln in policy.universe.stock_lines()}
    live, closes = set(live_ids), set(ours)
    history = closed_by_sl or {}
    out = []
    for pid, obs in sorted(seen.items()):
        if pid in live or pid in closes:
            continue
        line = owners.get(obs.symbol, obs.symbol)
        outcome: Literal["stop_hit", "vanished_not_stop"] = STOP_HIT
        if line in stocks:
            outcome = classify_vanished(last_bid=obs.bid, sl_rate=obs.sl_rate, sigma_4h=sigma_4h.get(line),
                                        closed_by_sl=history.get(pid))
        out.append(Vanished(position_id=pid, symbol=obs.symbol, line=line, outcome=outcome))
    return out


def vanished_alerts(items: Iterable[Vanished]) -> list[str]:
    return [f"URGENT {VANISHED_NOT_STOP}:{v.line}: a stock position vanished far from its stop "
            "(corporate action?); no cool-off recorded. Check the broker, then run "
            "`council stocks adopt <instrument id>`" for v in items if v.outcome == VANISHED_NOT_STOP]


# ------------------------------------------------------------------------------------ reconcile


def credited_symbols(policy: Policy) -> dict[str, str]:
    """{vehicle symbol or line id: line} for lines with `credited: corporate_action`."""
    out: dict[str, str] = {}
    for line in policy.universe.stock_lines():
        if line.stock is not None and line.stock.credited == "corporate_action":
            for sym in (line.symbol, *(v.symbol for v in line.vehicles.long)):
                out[sym] = line.symbol
    return out


def reconcile_credited(result: Any, positions: Iterable[Position], policy: Policy) -> tuple[Any, list[str]]:
    """(result with credited positions without a stop-loss taken out of `missing_sl` and `ok`
    recomputed, [`credited_no_sl:<line>` warnings]). `result` is an execution.reconcile.ReconcileResult."""
    credited = credited_symbols(policy)
    warned = sorted({credited[p.symbol] for p in positions
                     if p.symbol in credited and (p.sl_rate is None or p.sl_rate <= 0)})
    if not warned:
        return result, []
    exempt = {s for s, line in credited.items() if line in warned}
    missing = [s for s in result.missing_sl if s not in exempt]
    ok = result.drift_ok and not missing and not result.unknown_positions and not result.issues
    return result.model_copy(update={"missing_sl": missing, "ok": ok}), [f"{CREDITED_NO_SL}:{w}" for w in warned]


def reconcile_corporate(result: Any, positions: Sequence[Position], policy: Policy, *,
                        opened: Iterable[int]) -> tuple[Any, list[str], list[str]]:
    """What a post-execution (or watch) reconcile should act on when corporate actions are pending:
    (result, warnings, satellite-scoped blockers).

    - `reconcile_credited` first (a registered credit without a stop is a warning);
    - a pending corporate action (`pending_actions`: an instrument no line owns, a position no leg of
      ours opened, e.g. spin-off shares before `council stocks adopt`) is taken out of
      `unknown_positions` and `missing_sl` and returned as the satellite-scoped
      `corporate_action_pending` blocker instead, so a credit holds the stock sleeve, never the whole
      book. A symbol one of OUR positions also carries stays a problem, and so does every other
      unknown position or missing stop.
    Called by the executor's post-execution reconcile (`Executor._corporate_reconcile`, which
    `_final_state` judges) and by the watch's reconcile of held orders (`watch._final_after_wait`),
    both only while `sleeve_active`."""
    result, warnings = reconcile_credited(result, positions, policy)
    pending = pending_actions(positions, policy, opened=opened)
    if not pending:
        return result, warnings, []
    ids = {a.position_id for a in pending}
    theirs = {p.symbol for p in positions if p.position_id in ids}
    ours = {p.symbol for p in positions if p.position_id not in ids}
    exempt = theirs - ours
    unknown = [s for s in result.unknown_positions if s not in exempt]
    missing = [s for s in result.missing_sl if s not in exempt]
    ok = result.drift_ok and not missing and not unknown and not result.issues
    fixed = result.model_copy(update={"unknown_positions": unknown, "missing_sl": missing, "ok": ok})
    return fixed, warnings, blockers(pending)


# ------------------------------------------------------------------------------------ adopt


@dataclass(frozen=True)
class Company:
    """What SEC says about the instrument's symbol."""

    cik: str                  # 10 digits
    name: str
    sector: str | None        # FF12 (None: unknown SIC)


@dataclass(frozen=True)
class AdoptPlan:
    kind: Kind
    line: str
    sleeve: StockSleeveFile
    record: dict[str, Any]
    notes: tuple[str, ...] = ()


def _line_by(sleeve: StockSleeveFile, pred: Any) -> SleeveLine | None:
    found = [row for row in sleeve.lines if pred(row)]
    return found[0] if len(found) == 1 else None


def infer_kinds(sleeve: StockSleeveFile, *, row: EligibilityRow | None, known_symbol: str | None,
                holding: bool, company: Company | None) -> dict[str, SleeveLine | None]:
    """{kind: the line it acts on (None for a credit)} for every kind the facts support."""
    known = (known_symbol or "").upper()
    by_known = _line_by(sleeve, lambda r: known and r.etoro_symbol.upper() == known)
    by_row = _line_by(sleeve, lambda r: row is not None and r.etoro_symbol.upper() == row.symbol.upper())
    by_cik = _line_by(sleeve, lambda r: company is not None and r.cik == company.cik)
    out: dict[str, SleeveLine | None] = {}
    if row is not None:
        if by_known is not None and by_known.etoro_symbol.upper() != row.symbol.upper():
            out["rename"] = by_known
        elif by_cik is not None and by_cik.etoro_symbol.upper() != row.symbol.upper():
            out["rename"] = by_cik
    target = by_known or by_row or by_cik
    if target is not None and not holding and (row is None or not row.allow_open):
        out["delisted"] = target
    if holding and by_known is None and by_row is None and by_cik is None:
        out["credit"] = None
    return out


def plan_adopt(*, sleeve: StockSleeveFile, policy: Policy, instrument_id: int, row: EligibilityRow | None,
               known_symbol: str | None, holding: bool, company: Company | None, full: Verdict | None,
               closing: Verdict | None, now: datetime, kind: str | None = None,
               sector: str | None = None) -> AdoptPlan:
    """The sleeve-file edit for one corporate action (see the module docstring). `full` and
    `closing` are the stock gate on the instrument (`eligibility.instrument_verdicts`): a rename
    needs the full gate (else the line retires on the closing gate), a credit the closing gate.
    Raises AdoptError."""
    options = infer_kinds(sleeve, row=row, known_symbol=known_symbol, holding=holding, company=company)
    if kind is not None:
        if kind not in KINDS:
            raise AdoptError(f"unknown kind {kind!r} (credit, rename or delisted)")
        if kind not in options:
            raise AdoptError(f"the broker and SEC facts do not support a {kind} for instrument {instrument_id} "
                             f"(supported: {', '.join(options) or 'none'})")
        chosen: Kind = kind  # type: ignore[assignment]
    elif len(options) == 1:
        chosen = next(iter(options))  # type: ignore[assignment]
    elif not options:
        raise AdoptError(f"nothing to adopt for instrument {instrument_id}: no unknown position, no line "
                         "whose instrument changed or disappeared")
    else:
        raise AdoptError(f"instrument {instrument_id} fits {', '.join(options)}: pass --kind")
    record: dict[str, Any] = {"kind": chosen, "instrument_id": int(instrument_id), "quarter": sleeve.quarter,
                              "recorded_at": now.isoformat(), "status": "proposed"}
    notes: list[str] = []
    lines = list(sleeve.lines)
    retired = list(sleeve.retired)
    if chosen == "credit":
        new_line, notes = _credit_line(sleeve, policy, row, company, closing, now, sector)
        retired = [r for r in retired if r.cik != new_line.cik]
        lines.append(new_line)
        record.update(line=new_line.symbol, new_symbol=new_line.etoro_symbol)
        target = new_line.symbol
    elif chosen == "rename":
        old = options["rename"]
        assert old is not None and row is not None
        update: dict[str, Any] = {"etoro_symbol": row.symbol,
                                  "signal_ticker": sleeve_file.signal_ticker(_line_id(row.symbol))}
        if full is not None and full.ok:
            update["eligibility_checked_at"] = full.checked_at
        elif closing is not None and closing.ok:
            update.update(role="retiring", rank=None, eligibility_checked_at=closing.checked_at)
            notes.append(f"the renamed instrument fails the stock gate ({full.reason if full else 'unchecked'}); "
                         f"{old.symbol} becomes retiring and is sold at the next US session")
        else:
            raise AdoptError(f"the renamed instrument cannot be closed through the API "
                             f"({closing.reason if closing else 'unchecked'}); resolve it in the broker")
        edited = SleeveLine.model_validate({**old.model_dump(by_alias=True), **update})
        lines = [edited if r.symbol == old.symbol else r for r in lines]
        same_id = known_symbol is not None and known_symbol.upper() == old.etoro_symbol.upper()
        record.update(line=old.symbol, old_symbol=old.etoro_symbol, new_symbol=row.symbol, same_instrument=same_id)
        target = old.symbol
    else:
        old = options["delisted"]
        assert old is not None
        lines = [r.model_copy(update={"role": "retiring", "rank": None}) if r.symbol == old.symbol else r
                 for r in lines]
        record.update(line=old.symbol, old_symbol=old.etoro_symbol)
        notes.append(f"{old.symbol} is held at 0 until the next rank prunes it (untradable)")
        target = old.symbol
    edited_sleeve = sleeve_file.replace_lines(sleeve, lines, retired)
    return AdoptPlan(kind=chosen, line=target, sleeve=edited_sleeve, record=record, notes=tuple(notes))


def _line_id(symbol: str) -> str:
    from council.stocks.universe import normalise_id

    return normalise_id(symbol)


def _credit_line(sleeve: StockSleeveFile, policy: Policy, row: EligibilityRow | None, company: Company | None,
                 verdict: Verdict | None, now: datetime, sector: str | None) -> tuple[SleeveLine, list[str]]:
    """The credited instrument as a new `retiring` line (`verdict`: the closing-only gate)."""
    if row is None:
        raise AdoptError("the credited instrument has no eligibility row: it cannot be sold through the API")
    if company is None:
        raise AdoptError(f"SEC has no CIK for {row.symbol}: pass --cik (and --sector) to adopt it")
    if verdict is None or not verdict.ok:
        raise AdoptError(f"the credited instrument cannot be closed through the API "
                         f"({verdict.reason if verdict else 'unchecked'}); resolve it in the broker")
    live = {r.cik: r.symbol for r in sleeve.lines}
    if company.cik in live:
        raise AdoptError(f"CIK {company.cik} is already the line {live[company.cik]}")
    line_id = _line_id(row.symbol)
    owners = policy.universe.vehicle_map()
    if line_id in owners or row.symbol in owners:
        raise AdoptError(f"{line_id} is already a line or vehicle symbol ({owners.get(line_id) or owners.get(row.symbol)})")
    ff12 = sector or company.sector
    if not ff12:
        raise AdoptError(f"no FF12 sector for {row.symbol} (unknown SIC): pass --sector")
    line = SleeveLine.model_validate({
        "symbol": line_id, "name": (company.name or row.symbol)[:80], "role": "retiring", "sector": ff12,
        "cik": company.cik, "rank": None, "signal_ticker": sleeve_file.signal_ticker(line_id),
        "etoro_symbol": row.symbol, "eligibility_checked_at": now, "credited": "corporate_action", "aliases": []})
    return line, [f"{line_id}: credited shares become a retiring line, sold at the next US session"]


# ------------------------------------------------------------------------------------ the record


def record_path(state_dir: Path) -> Path:
    return state_dir / RECORD_FILE


def load_records(state_dir: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(record_path(state_dir).read_text())
    except (OSError, ValueError):
        return []
    actions = raw.get("actions") if isinstance(raw, dict) else None
    return [a for a in actions or [] if isinstance(a, dict)]


def save_records(state_dir: Path, records: Sequence[Mapping[str, Any]]) -> Path:
    path = record_path(state_dir)
    assert_outside_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".corporate.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": 1, "actions": list(records)}, fh, indent=1, sort_keys=True, default=str)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def add_record(state_dir: Path, record: Mapping[str, Any]) -> Path:
    records = [r for r in load_records(state_dir)
               if not (r.get("kind") == record.get("kind") and r.get("instrument_id") == record.get("instrument_id")
                       and r.get("status") == "proposed")]
    return save_records(state_dir, [*records, dict(record)])


def pending_aliases(records: Iterable[Mapping[str, Any]], policy: Policy) -> list[tuple[str, str, int]]:
    """(old symbol, new symbol, instrument id) of proposed same-instrument renames whose new symbol
    is now a committed stock vehicle (the edit was committed): for onboarding."""
    vehicles = {v.symbol for ln in policy.universe.stock_lines() for v in ln.vehicles.long}
    return [(str(r["old_symbol"]), str(r["new_symbol"]), int(r["instrument_id"])) for r in records
            if r.get("kind") == "rename" and r.get("status") == "proposed" and r.get("same_instrument")
            and r.get("new_symbol") in vehicles and r.get("old_symbol")]


def mark_applied(state_dir: Path, kind: str, instrument_ids: Iterable[int]) -> None:
    ids = {int(i) for i in instrument_ids}
    if not ids:
        return
    records = load_records(state_dir)
    for r in records:
        if r.get("kind") == kind and r.get("instrument_id") in ids:
            r["status"] = "applied"
    save_records(state_dir, records)


def delisted_lines(records: Iterable[Mapping[str, Any]]) -> set[str]:
    """Lines `council stocks adopt` recorded as delisted (a cash takeover): held at 0, untradable."""
    return {str(r["line"]) for r in records if r.get("kind") == "delisted" and r.get("line")}


def untradable_flags(records: Iterable[Mapping[str, Any]], policy: Policy) -> list[str]:
    """`untradable:<line>` for every delisted line the committed policy holds as retiring (a proposal
    that was never committed flags nothing)."""
    lines = {ln.symbol for ln in policy.universe.stock_lines() if ln.stock is not None and ln.stock.role == "retiring"}
    return sorted({f"{UNTRADABLE}:{r['line']}" for r in records
                   if r.get("kind") == "delisted" and r.get("line") in lines})
