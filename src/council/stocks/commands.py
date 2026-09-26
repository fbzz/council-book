"""The `council stocks` commands (design §3.5, §3.6, §4, D16): rank, onboard, adopt, status, prune.

Boundaries, for every command:
- Broker: the READ client only (eligibility, rates, the portfolio read). Nothing here imports the
  broker writer or sends an order.
- Policy: read from a snapshot of the COMMITTED policy (`git HEAD`, materialised under the state
  dir by `runtime.head_policy_snapshot`), never from the working tree. Nothing here writes into
  `policy/`, commits, tags or pushes. A proposal is loaded, with the committed tree (plus an
  optional overlay of go-live drafts), through every `Universe` validator, the invariants and the
  adoption checks in a temporary directory, then written to `<state_dir>/stocks/proposals/<name>/`;
  the command prints the copy / commit / tag commands the human runs.
- Secrets: the SEC user agent and the Alpaca keys come from their credential modules (the Keychain
  outside stub mode) and never appear in an output, a file or an error.
- Private outputs (the full ranked list with revenue levels, broker verdicts with reasons, the
  diff) stay in the state dir; the ranking document and the CHANGELOG snippet are public-safe.

The rank (`run_rank`), in order: membership + AI list -> SEC identity and fundamentals + price facts
(`RankServices.build_inputs`) -> the live rank over the FROZEN rule (`council.stocks.rank`) with
"held" = the committed sleeve's `selected` names (matched by CIK) -> the broker gate on selected +
shortlist + 15 reserves in ONE eligibility request (a failing name is replaced by the next eligible
one of its sector in the rule's order; `--no-eligibility` stamps null and live runs refuse those
lines) -> the diff and two-phase retirement against the committed sleeve (pruning only with a fresh
READ snapshot) -> validation -> history prefetch for new names (flagged, never replaced) -> files.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shlex
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from council import clock, paths
from council.policy import SLEEVE_FILE, STOCK_RANK_FILE, Policy, StockSleeveFile
from council.reference.report import UnsafePublicText
from council.stocks import corporate, sleeve_file
from council.stocks import eligibility as gate
from council.stocks.rank import RankConfig, RankResult, rank
from council.stocks.report import Source, check_public, ticker
from council.stocks.report import render as render_ranking
from council.stocks.universe import PriceFacts, RankInputs, load_ai_list, price_facts_from_bars

PROPOSALS = Path("stocks") / "proposals"
WORK = Path("stocks") / "tmp"
INSTRUMENTS_FILE = "instruments.json"
LEDGER_FILE = "ledger.sqlite3"
AI_LIST_FILE = "stock-universe-extra.yaml"
SNIPPET_FILE = "CHANGELOG-snippet.md"
PROPOSAL_FILES = (SLEEVE_FILE, f"{SLEEVE_FILE}.rejected", STOCK_RANK_FILE, AI_LIST_FILE, SNIPPET_FILE)
DEFAULT_SHORTLIST = 8
DEFAULT_SYMBOL_CAP = 40
DEFAULT_SOURCE_AGE_DAYS = 120
PRICE_PACE_S = 0.4                 # <= 150 bar requests a minute (Alpaca's free plan allows 200)
MAX_PRICE_REQUESTS = 100           # one rank: ~50 requests for ~600 names at 12 symbols a request
PRICE_EXTRA_DAYS = 60              # history window = min_listing_days + this, before the rank date
DOCTOR_SAMPLE = ("AAPL", "MSFT", "JNJ")   # large US stocks, before a sleeve is committed
DOCTOR_SAMPLE_SIZE = 3


class StocksError(RuntimeError):
    """A command refused; the message says why (never a secret)."""


def default_repo() -> Path:
    """The checkout whose committed policy the commands read (this repository)."""
    return paths.REPO_ROOT


def git_text(repo: Path, *args: str) -> str | None:
    return sleeve_file._git_out(repo, *args)


# ------------------------------------------------------------------------------------ committed policy


@dataclass(frozen=True)
class Committed:
    """A snapshot of `<commit>:policy/` under the state dir (never the working tree)."""

    commit: str
    directory: Path
    policy: Policy                          # core-only (the sleeve file ignored)
    sleeve: StockSleeveFile | None

    def with_sleeve(self) -> Policy:
        """The committed policy WITH its stock lines (for these tools; runtimes decide separately,
        under `invariants.STOCK_SLEEVE_LIVE`)."""
        try:
            return Policy.load(self.directory, include_sleeve=True)
        except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as exc:
            raise StocksError(f"the committed policy with its sleeve does not load: {exc}") from None


def committed(state_dir: Path, repo: Path) -> Committed:
    from council.runtime import PolicySnapshotError, head_policy_snapshot

    try:          # every command writes (snapshots, validation copies, proposals) under the state dir only
        paths.assert_outside_repo(state_dir)
    except RuntimeError as exc:
        raise StocksError(str(exc)) from None
    commit = git_text(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if not commit:
        raise StocksError(f"{repo} has no HEAD commit")
    try:      # rev = the commit id, not "HEAD": a tool run never records the runner's last snapshot
        snap = head_policy_snapshot(state_dir, repo=repo, rev=commit, include_sleeve=False)
    except PolicySnapshotError as exc:
        raise StocksError(str(exc)) from None
    path = snap.directory / SLEEVE_FILE
    try:
        sleeve = sleeve_file.load(path) if path.exists() else None
    except (ValueError, yaml.YAMLError) as exc:
        raise StocksError(f"the committed {SLEEVE_FILE} does not load: {exc}") from None
    return Committed(commit=commit, directory=snap.directory, policy=snap.policy, sleeve=sleeve)


def _adopted(directory: Path) -> Any:
    from council.stocks import adopted

    try:
        return adopted.load_adopted(directory)
    except adopted.AdoptedRuleError as exc:
        raise StocksError(f"the committed adoption record does not check out: {exc}") from None


# ------------------------------------------------------------------------------------ files


def _write_private(path: Path, data: bytes) -> None:
    paths.assert_outside_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_edit(directory: Path, files: Mapping[str, bytes]) -> dict[str, Path]:
    """An `adopt` / `prune` proposal: the edited sleeve file (or its `.rejected` copy), nothing stale."""
    return write_proposal(directory, files, stale=[n for n in PROPOSAL_FILES if n not in files])


def proposal_dir(state_dir: Path, name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"bad proposal name {name!r}")
    out = state_dir / PROPOSALS / name
    paths.assert_outside_repo(out)
    return out


def write_proposal(directory: Path, files: Mapping[str, bytes], *, stale: Iterable[str] = ()) -> dict[str, Path]:
    """Write every file (0600, atomically) and remove `stale` names left by an earlier run."""
    for name in stale:
        (directory / name).unlink(missing_ok=True)
    out = {}
    for name, data in files.items():
        _write_private(directory / name, data)
        out[name] = directory / name
    return out


def _short(exc: BaseException, limit: int = 240) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _q(path: Path) -> str:
    return shlex.quote(str(path))


def _tag_command(repo: Path, quarter: str) -> str:
    tag = f"{sleeve_file.TAG_PREFIX}{quarter}"
    exists = git_text(repo, "rev-parse", "--verify", "-q", f"refs/tags/{tag}")
    return (f"git tag -f {tag}    # moves the quarter's tag to the edited file (the runtime checks HEAD's "
            "sleeve against it)") if exists else f"git tag {tag}"


def _validation_lines(v: sleeve_file.Validation) -> list[str]:
    return [*(f"error: {e}" for e in v.errors), *(f"warning: {w}" for w in v.warnings)]


# ------------------------------------------------------------------------------------ rank settings


def _rank_settings_yaml(rule: Any) -> str:
    """A `stock-rank.yaml` for a committed policy that has none yet (the go-live commit): the adopted
    rule's `rule:` block, which the loader checks against `stocks/adopted.py`, and the live-only
    settings."""
    head = ("# Live quarterly rank settings (`council stocks rank`). `rule:` is the adopted rule and must\n"
            "# equal src/council/stocks/adopted.py (checked when the policy loads); the other keys are\n"
            "# live-only settings. Changing any value is a policy change (CHANGELOG entry).\n")
    body = {
        "version": 1,
        "shortlist_size": DEFAULT_SHORTLIST,
        "tiingo_symbol_cap": DEFAULT_SYMBOL_CAP,      # distinct stock history tickers (name kept for the loader)
        "history_source": "tiingo",                   # Signal.source label; stock history is routed to Alpaca
        "max_source_age_days": DEFAULT_SOURCE_AGE_DAYS,
        "rule": rule.rank_rule(),
    }
    text = head + yaml.safe_dump(body, sort_keys=False, default_flow_style=None)
    if yaml.safe_load(text)["rule"] != rule.rank_rule():
        raise StocksError("the generated stock-rank.yaml does not read back to the adopted rule")
    return text


def _ai_list_yaml(tickers: Sequence[str]) -> str:
    head = ("# The AI-adjacent tickers ranked with the index members (user decision, spec L9: the same rule\n"
            "# and filters; an untested extension of the studied universe, a hindsight list). Quoted so a\n"
            "# ticker such as ON stays a string.\n")
    return head + "tickers:\n" + "".join(f"  - {json.dumps(t)}\n" for t in tickers)


def _first_existing(*candidates: Path | None) -> Path | None:
    return next((p for p in candidates if p is not None and p.is_file()), None)


# ------------------------------------------------------------------------------------ rank


@dataclass
class RankServices:
    """What the rank reads from outside: in production `live_rank_services`; in tests, fakes.

    `build_inputs(asof, ai_symbols, config)` -> (RankInputs, the membership sources to credit);
    `broker`: the READ client (None without a token); `prefetch(policy, now)` -> ({line: bars},
    flags) through the cycle's day-keyed history cache."""

    build_inputs: Callable[[date, Sequence[str], RankConfig], tuple[RankInputs, list[Source]]]
    broker: Any | None = None
    prefetch: Callable[[Policy, datetime], tuple[Mapping[str, Any], list[str]]] | None = None
    now: Callable[[], datetime] = clock.utcnow


@dataclass
class RankRun:
    quarter: str
    asof: date
    directory: Path
    ok: bool
    errors: list[str]
    warnings: list[str]
    diff: sleeve_file.SleeveDiff
    flags: list[str]
    replaced: list[tuple[str, str]]
    rejected: dict[str, str]
    commands: list[str]
    files: dict[str, Path]
    result: RankResult | None = None
    sleeve: StockSleeveFile | None = None

    def report_lines(self) -> list[str]:
        out = [f"stock rank {self.quarter} as of {self.asof}: {'VALID' if self.ok else 'REJECTED'}",
               *self.diff.lines()]
        out += [f"eligibility: {old} replaced by {new}" for old, new in self.replaced]
        out += [f"eligibility: {k} fails ({why})" for k, why in sorted(self.rejected.items())]
        out += [f"flag: {f}" for f in self.flags]
        out += [f"warning: {w}" for w in self.warnings]
        out += [f"error: {e}" for e in self.errors]
        out.append(f"files: {self.directory}")
        if self.commands:
            out += ["", "review the files, then run (nothing was written to policy/, committed or tagged):",
                    *(f"  {c}" for c in self.commands)]
        return out


def held_keys(previous: StockSleeveFile | None, inputs: RankInputs) -> list[str]:
    """The previous rule selection (the committed sleeve's `selected` names, independent of anchors,
    stops and council moves) as rank keys: the line id when it is a candidate, else the candidate of
    the same company (CIK; a ticker change), else the line id (not eligible: nothing to keep)."""
    if previous is None:
        return []
    keys = {c.key for c in inputs.candidates}
    by_cik: dict[int, list[str]] = {}
    for c in inputs.candidates:
        if c.cik is not None:
            by_cik.setdefault(int(c.cik), []).append(c.key)
    out = []
    for line in previous.lines:
        if line.role != "selected":
            continue
        if line.symbol in keys:
            out.append(line.symbol)
        else:
            same = sorted(by_cik.get(int(line.cik), []))
            out.append(same[0] if same else line.symbol)
    return list(dict.fromkeys(out))


def _ledger_lines(state_dir: Path) -> set[str] | None:
    path = state_dir / LEDGER_FILE
    if not path.exists():
        return set()
    from council.ledger.db import Ledger

    try:
        return sleeve_file.in_flight_lines(Ledger(path))
    except Exception:          # an unreadable ledger: treat every line as busy (nothing is pruned)
        return None


def _touched(state_dir: Path, sleeve: StockSleeveFile | None, warnings: list[str]) -> set[str]:
    lines = _ledger_lines(state_dir)
    if lines is None:
        warnings.append("the ledger could not be read: no line is pruned")
        return {ln.symbol for ln in (sleeve.lines if sleeve else ())} | {
            a for ln in (sleeve.lines if sleeve else ()) for a in ln.aliases}
    return lines


def _portfolio(read: Any, state_dir: Path, warnings: list[str]) -> tuple[list[Any] | None, float | None]:
    from council.broker.instruments import InstrumentMap

    imap = InstrumentMap.load(state_dir / INSTRUMENTS_FILE)
    try:
        port = read.portfolio(imap.symbol_for)
    except Exception as exc:
        warnings.append(f"portfolio read failed ({type(exc).__name__}): nothing is pruned; the gate uses "
                        "the policy's assumed NAV")
        return None, None
    return list(port.positions), float(port.equity_usd)


def _instrument_ids(state_dir: Path) -> Callable[[str], int | None]:
    from council.broker.instruments import InstrumentMap

    return InstrumentMap.load(state_dir / INSTRUMENTS_FILE).get


def _new_lines(result: RankResult, inputs: RankInputs, keys_by_role: Sequence[tuple[str, Sequence[str]]],
               verdicts: Mapping[str, gate.Verdict]) -> list[sleeve_file.NewLine]:
    frame = result.eligible
    position = {k: i + 1 for i, k in enumerate(result.order)}
    names = {c.key: c.name for c in inputs.candidates}
    out = []
    for role, keys in keys_by_role:
        for key in keys:
            v = verdicts.get(key)
            name = (str(names.get(key) or "").strip() or ticker(key))[:80]
            out.append(sleeve_file.NewLine(
                symbol=key, name=name, role=role, sector=str(frame.at[key, "sector"]),  # type: ignore[arg-type]
                cik=sleeve_file.cik10(int(frame.at[key, "cik"])), rank=position.get(key),
                signal_ticker=sleeve_file.signal_ticker(key),
                etoro_symbol=v.symbol if v is not None and v.symbol else sleeve_file.broker_symbol_guess(key),
                eligibility_checked_at=v.checked_at if v is not None else None))
    return out


def private_canaries(state_dir: Path, nav: float | None) -> list[str | float]:
    """Private values the public outputs must never contain: the snapshot's NAV, the stored mirror
    figures (real funding, virtual NAV) and `COUNCIL_LEAK_CANARIES`. Numbers below 1,000 are left
    out: they would match ordinary counts and index names (S&P 500, Nasdaq-100)."""
    from council.operator.mirror import MirrorError, load_mirror
    from council.publish.leakscan import env_canaries

    values: list[float | None] = [nav]
    try:
        mirror = load_mirror(state_dir)
    except MirrorError:
        mirror = None
    if mirror is not None:
        values += [mirror.funding_usd, mirror.virtual_nav_usd]
    out: list[str | float] = [float(v) for v in values if v is not None and float(v) >= 1000]
    return [*out, *env_canaries()]


def _changelog(quarter: str, asof: date, diff: sleeve_file.SleeveDiff, sleeve: StockSleeveFile,
               replaced: int, sha: str, today: date, *, canaries: Sequence[str | float] = ()) -> str:
    def names(role: str) -> str:
        found = [ticker(ln.symbol) for ln in sleeve.lines if ln.role == role]
        return ", ".join(found) or "none"

    text = "\n".join([
        f"## stock sleeve {quarter} — policy change ({today:%Y-%m-%d})",
        f"- **Policy change**: `policy/stock-sleeve.yaml` for {quarter} (tag `stocks-{quarter}`), ranked as "
        f"of {asof:%Y-%m-%d}: {len(diff.selected_in)} in, {len(diff.selected_out)} out, "
        f"{len(diff.retiring)} retiring, {len(diff.pruned)} removed to the retired registry. Rank "
        f"configuration sha256 `{sha}`.",
        f"- Selected: {names('selected')}. Shortlist: {names('shortlist')}. Retiring: {names('retiring')}.",
        f"- Broker-eligibility replacements: {replaced} (a live-versus-study divergence).",
        f"- Ranking document: `docs/stocks/ranking-{quarter}.md`.",
        "",
    ])
    check_public(text, canaries=canaries)
    return text


def _private_rank_csv(result: RankResult) -> bytes:
    buf = io.StringIO()
    frame = result.eligible.copy()
    frame["role"] = [result.role(str(k)) or "" for k in frame.index]
    frame.to_csv(buf, index=False)
    writer = csv.writer(buf)
    writer.writerow([])
    writer.writerow(["excluded_key", "reason"])
    for key, why in sorted(result.excluded.items()):
        writer.writerow([key, why])
    return buf.getvalue().encode()


def run_rank(
    asof: date,
    services: RankServices,
    *,
    state_dir: Path,
    repo: Path,
    eligibility: bool = True,
    overlay_dir: Path | None = None,
    ai_list: Path | None = None,
    allow_off_anchor: bool = False,
) -> RankRun:
    """The quarterly rank (module docstring). Raises StocksError when it cannot run; returns a run
    whose `ok` is False when the proposal fails validation (nothing to copy then)."""
    now = services.now()
    if asof > now.date():
        raise StocksError(f"rank date {asof} is in the future")
    if not allow_off_anchor and asof not in sleeve_file.anchor_dates(asof.year):
        raise StocksError(f"{asof} is not a rule anchor date (the first US session on or after "
                          f"{', '.join(sleeve_file.ANCHORS)}); the latest is {sleeve_file.latest_anchor(asof)}. "
                          "Pass --allow-off-anchor to rank anyway (a divergence from the studied rule)")
    if overlay_dir is not None and not overlay_dir.is_dir():
        raise StocksError(f"policy overlay {overlay_dir} is not a directory")
    base = committed(state_dir, repo)
    rule = _adopted(base.directory)
    warnings: list[str] = []
    flags: list[str] = []
    proposed: dict[str, bytes] = {}

    rank_path = _first_existing(overlay_dir / STOCK_RANK_FILE if overlay_dir else None, base.directory / STOCK_RANK_FILE)
    if rank_path is None:
        rank_bytes = _rank_settings_yaml(rule).encode()
        proposed[STOCK_RANK_FILE] = rank_bytes
        warnings.append(f"the committed policy has no {STOCK_RANK_FILE}: a proposal is written (go-live)")
    else:
        rank_bytes = rank_path.read_bytes()
    settings = yaml.safe_load(rank_bytes) or {}
    config = RankConfig.from_adopted(rule, shortlist_size=int(settings.get("shortlist_size", DEFAULT_SHORTLIST)),
                                     max_source_age_days=int(settings.get("max_source_age_days",
                                                                          DEFAULT_SOURCE_AGE_DAYS)))
    ai_symbols: tuple[str, ...] = ()
    config_files = [(STOCK_RANK_FILE, rank_bytes)]
    if rule.ai_list == "ranked_mechanically":
        committed_list = _first_existing(overlay_dir / AI_LIST_FILE if overlay_dir else None,
                                         base.directory / AI_LIST_FILE)
        source = ai_list or committed_list
        if source is None:
            raise StocksError(f"the adopted rule ranks the AI-adjacent list (L9) but no {AI_LIST_FILE} is "
                              "committed: pass --ai-list PATH (it is proposed for policy/ with the sleeve)")
        try:
            ai_symbols = load_ai_list(source)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            raise StocksError(f"AI list: {exc}") from None
        if ai_list is not None and (committed_list is None or load_ai_list(committed_list) != ai_symbols):
            proposed[AI_LIST_FILE] = _ai_list_yaml(ai_symbols).encode()
        config_files.append((AI_LIST_FILE, proposed.get(AI_LIST_FILE) or source.read_bytes()))
    sha = sleeve_file.rank_config_sha256(config_files)
    quarter = sleeve_file.quarter_of(asof)
    previous = base.sleeve

    try:
        inputs, sources = services.build_inputs(asof, ai_symbols, config)
    except StocksError:
        raise
    except Exception as exc:          # data-layer messages never carry a secret (SEC UA, Alpaca keys)
        raise StocksError(f"the rank inputs could not be built: {_short(exc)}") from None
    inputs = replace(inputs, held=tuple(held_keys(previous, inputs)))
    flags += list(inputs.notes)
    try:
        result = rank(asof, inputs, config)
    except (ValueError, RuntimeError) as exc:
        raise StocksError(f"the rank cannot run: {exc}") from None

    selected, shortlist = list(result.selected), list(result.shortlist)
    verdicts: dict[str, gate.Verdict] = {}
    positions: list[Any] | None = None
    nav: float | None = None
    if eligibility:
        read = services.broker
        if read is None:
            raise StocksError("no READ token in the keychain: the broker gate cannot run (pass --no-eligibility "
                              "to rank without it; live runs then refuse the unchecked lines)")
        positions, nav = _portfolio(read, state_dir, warnings)
        cfg = gate.gate_config(base.policy, unit_share=rule.unit, virtual_nav_usd=nav)
        reserves = [k for k in result.order if k not in set(selected) | set(shortlist)][:gate.RESERVES]
        wanted = [*selected, *shortlist, *reserves]
        guess = {k: sleeve_file.broker_symbol_guess(k) for k in wanted}
        try:
            found = gate.check_symbols(read, [guess[k] for k in wanted], cfg, now=now)
        except Exception as exc:
            raise StocksError(f"the broker eligibility request failed ({type(exc).__name__})") from None
        verdicts = {k: found[guess[k]] for k in wanted}

        def passes(key: str) -> bool:
            return key in verdicts and verdicts[key].ok
    else:
        warnings.append("ranked without the broker gate: every line is unchecked and live runs refuse it")

        def passes(key: str) -> bool:
            return True
    sector = result.eligible["sector"].to_dict() if not result.eligible.empty else {}
    final_sel, replaced = gate.replace_failures(selected, list(result.order), sector, passes)
    final_short, _ = gate.replace_failures(shortlist, list(result.order), sector, passes, used=final_sel)
    if len(final_sel) < len(selected):
        warnings.append(f"only {len(final_sel)} of {len(selected)} selected slots could be filled with eligible names")
    rejected = {k: v.reason for k, v in verdicts.items() if not v.ok and k in set(selected) | set(shortlist)}
    chosen = _new_lines(result, inputs, (("selected", final_sel), ("shortlist", final_short)),
                        verdicts if eligibility else {})

    flat = sleeve_file.flat_from_positions(positions, _instrument_ids(state_dir)) if positions is not None else None
    touched = _touched(state_dir, previous, warnings)
    try:
        sleeve, diff = sleeve_file.build_sleeve(
            previous, chosen, quarter=quarter, rank_asof=asof, rank_config_sha256=sha,
            sleeve_weight=rule.sleeve_share, names_target=rule.names, flat=flat, touched=touched,
            first_quarter=sleeve_file.tagged_first_quarters(repo))
    except (sleeve_file.ProposalError, ValueError) as exc:
        raise StocksError(f"the proposal cannot be built: {exc}") from None
    comment = (f"Stock sleeve {quarter}, written by `council stocks rank` (rank date {asof:%Y-%m-%d}).\n"
               "Change it only through `council stocks rank`, `adopt` or `prune`; every string is quoted.")
    files = {SLEEVE_FILE: sleeve_file.dump(sleeve, comment=comment).encode(), **proposed}
    validation = sleeve_file.validate(base.directory, files, workdir=state_dir / WORK, overlay_dir=overlay_dir)
    errors = list(validation.errors)
    warnings += validation.warnings
    if validation.policy is None and not errors:
        errors.append("the proposed policy does not load")

    if validation.ok and services.prefetch is not None:
        flags += _prefetch(validation.policy, sleeve, previous, services, now)  # type: ignore[arg-type]

    today = now.date()
    canaries = private_canaries(state_dir, nav)
    try:
        ranking = render_ranking(
            result, quarter=quarter, roles={ln.symbol: ln.role for ln in sleeve.lines if ln.role != "retiring"},
            counts={"in": len(diff.selected_in), "out": len(diff.selected_out), "retiring": len(diff.retiring),
                    "pruned": len(diff.pruned)},
            sources=sources, rank_config_sha256=sha, rule_cell=rule.cell, sector_cap=rule.sector_cap,
            replaced=len(replaced), generated=today, canaries=canaries, ai_list=bool(ai_symbols))
        snippet = _changelog(quarter, asof, diff, sleeve, len(replaced), sha, today, canaries=canaries)
    except UnsafePublicText:           # never echo the offending text: it could be a private value
        raise StocksError("the ranking document or the CHANGELOG snippet fails the public-safety check; "
                          "nothing was written") from None
    summary = {
        "quarter": quarter, "rank_asof": asof.isoformat(), "valid": not errors, "errors": errors,
        "warnings": warnings, "flags": flags, "diff": diff.lines(), "replaced": replaced, "rejected": rejected,
        "funnel": result.funnel, "exclusions": result.exclusion_counts, "rank_config_sha256": sha,
        "policy_commit": base.commit, "overlay": str(overlay_dir) if overlay_dir else None,
        "eligibility": eligibility, "sources": [s.line() for s in sources],
    }
    out_files = {
        f"ranking-{quarter}.md": ranking.encode(),
        f"rank-{quarter}.csv": _private_rank_csv(result),
        "summary.json": (json.dumps(summary, indent=1, sort_keys=True, default=str) + "\n").encode(),
    }
    directory = proposal_dir(state_dir, quarter)
    commands: list[str] = []
    if errors:
        out_files[f"{SLEEVE_FILE}.rejected"] = files[SLEEVE_FILE]
    else:
        out_files.update(files)
        out_files[SNIPPET_FILE] = snippet.encode()
    # a file an earlier run of the same quarter left and this run does not write is removed, so the
    # directory never offers a stale policy file next to the current proposal
    written = write_proposal(directory, out_files, stale=[n for n in PROPOSAL_FILES if n not in out_files])
    if not errors:
        commands = [f"cp {_q(directory / name)} policy/{name}" for name in files]
        if overlay_dir is not None:
            commands.append(f"# the validation included the overlay {overlay_dir}: commit those files with this one")
        commands += [
            f"mkdir -p docs/stocks && cp {_q(directory / f'ranking-{quarter}.md')} docs/stocks/",
            f"# paste {directory / SNIPPET_FILE} at the top of CHANGELOG.md (a policy change)",
            "git add " + " ".join([*(f"policy/{n}" for n in files), f"docs/stocks/ranking-{quarter}.md",
                                   "CHANGELOG.md"]),
            f"git commit -m {shlex.quote(f'stock sleeve {quarter}: {diff.summary()}')}",
            _tag_command(repo, quarter),
            "council stocks onboard",
        ]
    return RankRun(quarter=quarter, asof=asof, directory=directory, ok=not errors, errors=errors,
                   warnings=warnings, diff=diff, flags=flags, replaced=replaced, rejected=rejected,
                   commands=commands, files=written, result=result, sleeve=sleeve)


def _prefetch(policy: Policy, sleeve: StockSleeveFile, previous: StockSleeveFile | None,
              services: RankServices, now: datetime) -> list[str]:
    """History for every NEW selected / shortlist name into the day-keyed cache; a name without it,
    or with too little for a trend state, is flagged (it enters at level 0 with no_data; never
    replaced, which would change the studied selection)."""
    known = {ln.cik for ln in previous.lines} if previous is not None else set()
    new = {ln.symbol for ln in sleeve.lines if ln.role in ("selected", "shortlist") and ln.cik not in known}
    if not new or services.prefetch is None:
        return []
    lines = [ln for ln in policy.universe.lines if ln.symbol in new]
    subset = policy.model_copy(update={"universe": policy.universe.model_copy(update={"lines": lines})})
    try:
        history, flags = services.prefetch(subset, now)
    except Exception as exc:          # a prefetch never fails the rank
        return [f"history_prefetch_failed:{type(exc).__name__}"]
    need = int(policy.reference.get("trend", {}).get("slow_sma", 200)) + 1
    out = list(flags)
    for line in sorted(new):
        bars = history.get(line)
        if bars is not None and len(bars) < need:
            out.append(f"history_short:{line}:{len(bars)}<{need}")
        elif bars is None and not any(f.split(":")[1:2] == [line] for f in flags if ":" in f):
            out.append(f"history_missing:{line}")
    return out


# ------------------------------------------------------------------------------------ live services


def alpaca_price_facts(line_ids: Iterable[str], asof: date, *, keys: Any, lookback_days: int, state_dir: Path,
                       client: Any = None, sleep: Callable[[float], None] = time.sleep,
                       now: datetime | None = None) -> dict[str, PriceFacts]:
    """{line id: first and last bar on or before `asof`, 63-session median dollar volume} from Alpaca
    daily bars (12 symbols a request, paced, at most MAX_PRICE_REQUESTS). It honours the shared
    Alpaca breaker and trips it on a 429 (then refuses). A symbol Alpaca cannot spell or returns
    nothing for has no facts (the rank then excludes it as not priced)."""
    from council.data import alpaca
    from council.data.http import DataError, RateLimited, client_scope
    from council.facts.market import RequestBudget

    tickers: dict[str, str] = {}
    for lid in dict.fromkeys(line_ids):
        t = sleeve_file.signal_ticker(lid)
        try:
            alpaca.alpaca_symbol(t)
        except ValueError:
            continue
        tickers[lid] = t
    ids = list(tickers)
    size = alpaca.SYMBOLS_PER_REQUEST
    chunks = [ids[i:i + size] for i in range(0, len(ids), size)]
    if len(chunks) > MAX_PRICE_REQUESTS:
        raise StocksError(f"{len(ids)} symbols need {len(chunks)} price requests (at most {MAX_PRICE_REQUESTS})")
    breaker = RequestBudget.for_provider(alpaca.SOURCE, state_dir)
    start = asof - timedelta(days=int(lookback_days) + PRICE_EXTRA_DAYS)
    out = {lid: PriceFacts() for lid in dict.fromkeys(line_ids)}
    with client_scope(client) as http:
        for i, chunk in enumerate(chunks):
            if breaker.breaker_open():
                raise StocksError("Alpaca's breaker is open after a recent 429: run the rank again in an hour")
            try:
                bars = alpaca.fetch_daily([tickers[lid] for lid in chunk], start, keys=keys, client=http, now=now)
            except RateLimited:
                breaker.trip()
                raise StocksError("Alpaca answered 429 (rate limited): the breaker is set for an hour") from None
            except DataError as exc:
                raise StocksError(f"Alpaca price history failed: {exc}") from None
            for lid in chunk:
                out[lid] = price_facts_from_bars(bars.get(tickers[lid]), asof)
            if i + 1 < len(chunks):
                sleep(PRICE_PACE_S)
    return out


def live_rank_services(state_dir: Path, settings: Any, *, eligibility: bool, prefetch: bool = True) -> RankServices:
    """The production services: MediaWiki membership (the revision as of the rank date when it is in
    the past), SEC EDGAR (user agent from the Keychain), Alpaca price facts and history (keys from
    the Keychain), and the READ client."""
    from council.data import alpaca
    from council.data.credentials import MissingCredential, sec_user_agent
    from council.stocks.report import mediawiki_source
    from council.stocks.sec import SecClient
    from council.stocks.universe import (
        INDEXES,
        build_rank_inputs,
        cross_check,
        fetch_membership,
        index_constitution_membership,
        try_normalise_id,
    )

    keys = alpaca.load_keys()
    if keys is None:
        raise StocksError("no Alpaca keys (Keychain council-book.alpaca-key-id / council-book.alpaca-secret): "
                          "the rank's price filters need them")
    try:
        sec_user_agent()
    except MissingCredential as exc:
        raise StocksError(str(exc)) from None
    broker = None
    if eligibility:
        from council.context import read_broker

        broker = read_broker(settings)

    def build_inputs(asof: date, ai_symbols: Sequence[str], config: RankConfig) -> tuple[RankInputs, list[Source]]:
        current = asof >= clock.utcnow().date()
        members = [fetch_membership(ix, asof=None if current else asof) for ix in INDEXES]
        notes: list[str] = []
        if current:
            for m in members:
                other = index_constitution_membership(m.index)
                if other is not None:
                    check = cross_check(m, other)
                    if check.overlap < 0.98:
                        notes.append(f"membership_cross_check:{m.index}:{check.overlap:.3f}")
        ids = {lid for m in members for s in m.symbols if (lid := try_normalise_id(s))}
        ids |= {lid for s in ai_symbols if (lid := try_normalise_id(s))}
        facts = alpaca_price_facts(sorted(ids), asof, keys=keys, lookback_days=config.min_listing_days,
                                   state_dir=state_dir)
        with SecClient() as sec:
            inputs = build_rank_inputs(asof, memberships=members, sec=sec, price_facts=facts,
                                       ai_symbols=ai_symbols, refresh=True)
        inputs = replace(inputs, notes=(*inputs.notes, *notes))
        return inputs, [mediawiki_source(m.title, m.as_of.date()) for m in members]

    def history(policy: Policy, now: datetime) -> tuple[Mapping[str, Any], list[str]]:
        from council.facts.market import gather_history

        return gather_history(policy, now=now, tiingo_token=None, alpaca_keys=keys, state_dir=state_dir)

    return RankServices(build_inputs=build_inputs, broker=broker, prefetch=history if prefetch else None)


def sec_company_lookup() -> Callable[..., corporate.Company | None]:
    """`lookup(symbol=..., cik=...)` -> the company from SEC EDGAR (tickers file, submissions): its CIK,
    name and FF12 sector. None when SEC does not know the ticker."""
    from council.stocks.sec import SecClient
    from council.stocks.universe import sector_of, ticker_map, try_normalise_id

    def lookup(*, symbol: str | None = None, cik: str | None = None) -> corporate.Company | None:
        with SecClient() as sec:
            number: int | None = int(cik) if cik else None
            title = ""
            if number is None and symbol:
                lid = try_normalise_id(symbol)
                row = ticker_map(sec.company_tickers()).get(lid) if lid else None
                if row is None:
                    return None
                number, title = int(row.cik), row.title
            if number is None:
                return None
            sub = sec.submissions(number)
            return corporate.Company(cik=sleeve_file.cik10(number), name=str(sub.get("name") or title or symbol),
                                     sector=sector_of(sub.get("sic")))

    return lookup


# ------------------------------------------------------------------------------------ onboard


class _OneEligibilityCall:
    """Serves one eligibility answer to both the instrument resolver and the gate (ONE POST)."""

    def __init__(self, read: Any, symbols: Sequence[str]) -> None:
        self._read = read
        self._rows = read.eligibility(symbols=list(symbols))

    def eligibility(self, symbols: Any = None, instrument_ids: Any = None, **_: Any) -> list[Any]:
        return list(self._rows)

    def rates(self, ids: Any) -> Any:
        return self._read.rates(ids)


@dataclass
class Outcome:
    ok: bool
    lines: list[str] = field(default_factory=list)
    directory: Path | None = None
    commands: list[str] = field(default_factory=list)

    def report_lines(self) -> list[str]:
        out = list(self.lines)
        if self.directory is not None:
            out.append(f"files: {self.directory}")
        if self.commands:
            out += ["", "review the files, then run (nothing was written to policy/, committed or tagged):",
                    *(f"  {c}" for c in self.commands)]
        return out


def _unit_share(policy: Policy) -> float:
    sleeve = policy.universe.stock_sleeve
    return sleeve.sleeve_weight / sleeve.names_target if sleeve is not None else 0.0625


def run_onboard(*, state_dir: Path, repo: Path, broker: Any, now: datetime) -> Outcome:
    """After the sleeve is committed and tagged: record committed renames as explicit aliases,
    resolve every stock vehicle into instruments.json (exactly one row; append-only) and re-run the
    gate (closing-only for retiring lines). Non-zero when a line fails, is unresolved, or was never
    eligibility-checked (live runs refuse it)."""
    from council.broker.instruments import InstrumentIdentityChanged, InstrumentMap, resolve

    base = committed(state_dir, repo)
    if base.sleeve is None:
        raise StocksError(f"no committed policy/{SLEEVE_FILE}")
    if broker is None:
        raise StocksError("no READ token in the keychain")
    policy = base.with_sleeve()
    lines: list[str] = []
    ok = True
    tag = sleeve_file.tag_state(repo, base.sleeve.quarter)
    if tag != "tagged":
        ok = False
        lines.append(f"BAD  tag stocks-{base.sleeve.quarter}: {tag} (live cycles hold the stock sleeve until "
                     "HEAD's sleeve file is the tagged blob)")
    path = state_dir / INSTRUMENTS_FILE
    imap = InstrumentMap.load(path)
    renames = corporate.pending_aliases(corporate.load_records(state_dir), policy)
    applied = []
    for old, new, iid in renames:
        if imap.aliases.get(old) is not None and imap.aliases[old].new == new:
            applied.append(iid)
            continue
        try:
            imap = imap.with_alias(old, new, iid, now)
        except InstrumentIdentityChanged as exc:
            raise StocksError(f"alias {old} -> {new} refused: {exc}; nothing was written") from None
        applied.append(iid)
        lines.append(f"ok   alias {old} -> {new} recorded (explicit rename)")
    if applied:
        imap.save()
        corporate.mark_applied(state_dir, "rename", applied)
    stock = policy.universe.stock_lines()
    if not stock:
        return Outcome(ok=ok, lines=[*lines, "no stock lines in the committed sleeve"])
    symbol_of = {ln.symbol: ln.vehicles.long[0].symbol for ln in stock}
    symbols = list(dict.fromkeys(symbol_of.values()))
    try:
        once = _OneEligibilityCall(broker, symbols)
    except Exception as exc:
        raise StocksError(f"the broker eligibility request failed ({type(exc).__name__})") from None
    try:
        resolved = resolve(once, symbols, now=now, path=path)
    except InstrumentIdentityChanged as exc:
        raise StocksError(f"{exc.symbol}: the broker maps it to a different instrument than instruments.json; "
                          "nothing was written. If it is a corporate action run `council stocks adopt "
                          "<instrument id>`") from None
    warnings: list[str] = []
    _, nav = _portfolio(broker, state_dir, warnings)
    lines += [f"warning: {w}" for w in warnings]
    cfg = gate.gate_config(policy, unit_share=_unit_share(policy), virtual_nav_usd=nav)
    retiring = [symbol_of[ln.symbol] for ln in stock if ln.stock is not None and ln.stock.role == "retiring"]
    verdicts = gate.check_symbols(once, symbols, cfg, now=now, closing_only=retiring)
    unresolved = set(resolved.unresolved)
    delisted = corporate.delisted_lines(corporate.load_records(state_dir))
    for ln in stock:
        sym = symbol_of[ln.symbol]
        v = verdicts[sym]
        iid = None if sym in unresolved else resolved.get(sym)
        if ln.stock is not None and ln.stock.role == "retiring" and ln.symbol in delisted and not v.ok:
            lines.append(f"ok   {ln.symbol} (delisted: held at 0 until the next rank removes it)")
        elif iid is None:
            ok = False
            lines.append(f"BAD  {ln.symbol}: {sym} unresolved ({'ambiguous' if sym in resolved.ambiguous else 'not found'})")
        elif not v.ok:
            ok = False
            lines.append(f"BAD  {ln.symbol}: gate fails ({v.reason})")
        elif v.instrument_id != iid or (v.symbol or "").upper() != sym.upper():
            ok = False
            lines.append(f"BAD  {ln.symbol}: the gate's instrument differs from instruments.json")
        else:
            lines.append(f"ok   {ln.symbol} ({ln.stock.role if ln.stock else '?'})")
    for problem in gate.preflight_errors(policy):
        ok = False
        lines.append(f"BAD  {problem} (live runs refuse it: re-rank with the broker gate)")
    return Outcome(ok=ok, lines=lines)


# ------------------------------------------------------------------------------------ adopt


def run_adopt(instrument_id: int, *, state_dir: Path, repo: Path, broker: Any,
              company_lookup: Callable[..., corporate.Company | None], now: datetime, kind: str | None = None,
              cik: str | None = None, sector: str | None = None) -> Outcome:
    """`council stocks adopt <instrumentId>` (design §3.6): the broker gate by instrument, the SEC
    company, then a credit, a rename or a delisting as a proposed sleeve-file edit (validated;
    written under the state dir) plus a private corporate-action record."""
    from council.broker.instruments import InstrumentMap

    base = committed(state_dir, repo)
    if base.sleeve is None:
        raise StocksError(f"no committed policy/{SLEEVE_FILE}")
    if broker is None:
        raise StocksError("no READ token in the keychain")
    policy = base.with_sleeve()
    imap = InstrumentMap.load(state_dir / INSTRUMENTS_FILE)
    warnings: list[str] = []
    positions, nav = _portfolio(broker, state_dir, warnings)
    if positions is None:
        raise StocksError("the portfolio read failed: adopt needs a fresh READ snapshot")
    cfg = gate.gate_config(policy, unit_share=_unit_share(policy), virtual_nav_usd=nav)
    try:
        found = gate.instrument_verdicts(broker, int(instrument_id), cfg, now=now)
    except Exception as exc:
        raise StocksError(f"the broker eligibility request failed ({type(exc).__name__})") from None
    holding = any(int(p.instrument_id) == int(instrument_id) for p in positions)
    known = imap.symbol_for(int(instrument_id))
    try:
        company = company_lookup(symbol=found.row.symbol if found.row is not None else known, cik=cik)
    except Exception as exc:          # a rename by instrument or a delisting needs no SEC answer
        company = None
        warnings.append(f"SEC lookup failed ({type(exc).__name__}): pass --cik (and --sector) if a credit or a "
                        "same-company rename needs it")
    try:
        plan = corporate.plan_adopt(sleeve=base.sleeve, policy=policy, instrument_id=int(instrument_id),
                                    row=found.row, known_symbol=known, holding=holding, company=company,
                                    full=found.full, closing=found.closing, now=now, kind=kind, sector=sector)
    except (corporate.AdoptError, sleeve_file.ProposalError, ValueError) as exc:
        raise StocksError(str(exc)) from None
    quarter = base.sleeve.quarter
    comment = (f"Stock sleeve {quarter}: corporate action ({plan.kind} {plan.line}) proposed by "
               "`council stocks adopt`.\nChange it only through `council stocks rank`, `adopt` or `prune`.")
    files = {SLEEVE_FILE: sleeve_file.dump(plan.sleeve, comment=comment).encode()}
    validation = sleeve_file.validate(base.directory, files, workdir=state_dir / WORK)
    directory = proposal_dir(state_dir, f"{quarter}-adopt-{plan.kind}-{plan.line}")
    lines = [f"{plan.kind}: {plan.line}", *plan.notes, *(f"warning: {w}" for w in warnings),
             *_validation_lines(validation)]
    if not validation.ok:
        _write_edit(directory, {f"{SLEEVE_FILE}.rejected": files[SLEEVE_FILE]})
        return Outcome(ok=False, lines=lines, directory=directory)
    _write_edit(directory, files)
    corporate.add_record(state_dir, plan.record)
    message = f"stock sleeve {quarter}: corporate action ({plan.kind} {plan.line}) — policy change"
    commands = [
        f"cp {_q(directory / SLEEVE_FILE)} policy/{SLEEVE_FILE}",
        f"# add a CHANGELOG.md entry: {message}",
        f"git add policy/{SLEEVE_FILE} CHANGELOG.md",
        f"git commit -m {shlex.quote(message)}",
        _tag_command(repo, quarter),
        "council stocks onboard",
    ]
    return Outcome(ok=True, lines=lines, directory=directory, commands=commands)


# ------------------------------------------------------------------------------------ prune


def run_prune(*, state_dir: Path, repo: Path, broker: Any, now: datetime) -> Outcome:
    """`council stocks prune`: every retiring line that is flat in a fresh READ snapshot and touched
    by no in-flight decision moves to the retired registry (a proposed sleeve-file edit)."""
    base = committed(state_dir, repo)
    if base.sleeve is None:
        raise StocksError(f"no committed policy/{SLEEVE_FILE}")
    if broker is None:
        raise StocksError("no READ token in the keychain: pruning needs a fresh READ snapshot")
    warnings: list[str] = []
    positions, _ = _portfolio(broker, state_dir, warnings)
    if positions is None:
        raise StocksError("the portfolio read failed: nothing can be pruned without a fresh READ snapshot")
    flat = sleeve_file.flat_from_positions(positions, _instrument_ids(state_dir))
    touched = _touched(state_dir, base.sleeve, warnings)
    new, pruned = sleeve_file.prune(base.sleeve, flat=flat, touched=touched,
                                    first_quarter=sleeve_file.tagged_first_quarters(repo))
    retiring = [ln.symbol for ln in base.sleeve.lines if ln.role == "retiring"]
    kept = [s for s in retiring if s not in pruned]
    lines = [*(f"warning: {w}" for w in warnings), f"pruned: {', '.join(pruned) or 'none'}",
             f"still retiring: {', '.join(kept) or 'none'}"]
    if not pruned:
        return Outcome(ok=True, lines=lines)
    quarter = base.sleeve.quarter
    comment = (f"Stock sleeve {quarter}: flat retiring lines moved to the registry by `council stocks prune`.\n"
               "Change it only through `council stocks rank`, `adopt` or `prune`.")
    files = {SLEEVE_FILE: sleeve_file.dump(new, comment=comment).encode()}
    validation = sleeve_file.validate(base.directory, files, workdir=state_dir / WORK)
    directory = proposal_dir(state_dir, f"{quarter}-prune")
    lines += _validation_lines(validation)
    if not validation.ok:
        _write_edit(directory, {f"{SLEEVE_FILE}.rejected": files[SLEEVE_FILE]})
        return Outcome(ok=False, lines=lines, directory=directory)
    _write_edit(directory, files)
    message = f"stock sleeve {quarter}: prune {', '.join(pruned)} to the retired registry — policy change"
    commands = [
        f"cp {_q(directory / SLEEVE_FILE)} policy/{SLEEVE_FILE}",
        f"# add a CHANGELOG.md entry: {message}",
        f"git add policy/{SLEEVE_FILE} CHANGELOG.md",
        f"git commit -m {shlex.quote(message)}",
        _tag_command(repo, quarter),
    ]
    return Outcome(ok=True, lines=lines, directory=directory, commands=commands)


# ------------------------------------------------------------------------------------ status


def _budget_line(state_dir: Path, now: datetime) -> str:
    from council.data import alpaca
    from council.facts.market import RequestBudget

    budget = RequestBudget.for_provider(alpaca.SOURCE, state_dir, now_fn=lambda: now)
    hour, day, month = now.astimezone(UTC).strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    lim = budget.limits
    state = budget.state
    return (f"alpaca budget: {state['hours'].get(hour, 0)}/{lim.requests_per_hour} this hour, "
            f"{state['days'].get(day, 0)}/{lim.requests_per_day} today, "
            f"{len(state['months'].get(month, []))}/{lim.symbols_per_month} symbols this month"
            f"{', BREAKER OPEN' if budget.breaker_open() else ''}")


def run_status(*, state_dir: Path, repo: Path, broker: Any | None, now: datetime) -> Outcome:
    """The weekly check (design WP-N): tag state, roles, unchecked lines, retiring flatness (with a
    READ snapshot), corporate-action records, council anchors, the Alpaca budget, the real-account fee
    drag and the next anchor date. Private terminal output only."""
    base = committed(state_dir, repo)
    lines = [f"committed policy: {base.commit[:12]}", f"next rank anchor: {sleeve_file.next_anchor(now.date())}"]
    if base.sleeve is None:
        lines.append(f"no committed policy/{SLEEVE_FILE}")
        lines.append(_budget_line(state_dir, now))
        return Outcome(ok=True, lines=lines)
    sleeve = base.sleeve
    policy = base.with_sleeve()
    ok = True
    tag = sleeve_file.tag_state(repo, sleeve.quarter)
    ok &= tag == "tagged"
    roles = {r: [ln.symbol for ln in sleeve.lines if ln.role == r] for r in ("selected", "shortlist", "retiring")}
    lines += [f"quarter {sleeve.quarter} (rank date {sleeve.rank_asof}); tag stocks-{sleeve.quarter}: {tag}",
              *(f"{r}: {', '.join(v) or 'none'}" for r, v in roles.items()),
              f"retired registry: {len(sleeve.retired)} companies"]
    unchecked = gate.preflight_errors(policy)
    ok &= not unchecked
    lines += [f"BAD  {u} (live runs refuse it)" for u in unchecked]
    if broker is not None:
        warnings: list[str] = []
        positions, _ = _portfolio(broker, state_dir, warnings)
        flat = sleeve_file.flat_from_positions(positions, _instrument_ids(state_dir))
        for ln in sleeve.lines:
            if ln.role == "retiring":
                state = {True: "flat (prunable)", False: "still held", None: "unknown"}[flat(ln)]
                lines.append(f"retiring {ln.symbol}: {state}")
        if positions is not None:          # corporate actions and retired vehicles, whatever the roles
            found = corporate.detect(positions, policy, opened=_opened(state_dir))
            ok &= not found.blockers
            lines += [f"BAD  {b}" for b in found.blockers] + list(found.alerts)
        else:
            ok = False
        lines += [f"warning: {w}" for w in warnings]
    records = corporate.load_records(state_dir)
    lines += [f"corporate action {r.get('kind')} {r.get('line', '?')}: {r.get('status')}" for r in records
              if r.get("quarter") == sleeve.quarter]
    lines += corporate.untradable_flags(records, policy)
    lines.append(_budget_line(state_dir, now))
    lines += _ledger_status(state_dir, sleeve.quarter)
    return Outcome(ok=ok, lines=lines)


def _opened(state_dir: Path) -> set[int]:
    path = state_dir / LEDGER_FILE
    if not path.exists():
        return set()
    from council.ledger.db import Ledger

    return corporate.opened_position_ids(Ledger(path))


def _ledger_status(state_dir: Path, quarter: str) -> list[str]:
    path = state_dir / LEDGER_FILE
    if not path.exists():
        return ["ledger: none yet"]
    from council.ledger.db import Ledger

    try:
        ledger = Ledger(path)
        anchors = ledger.get_runtime(f"sleeve_anchor:{quarter}") or {}
        drag = float(ledger.real_fee_drag())
    except Exception as exc:
        return [f"ledger: unreadable ({type(exc).__name__})"]
    return [f"council anchors this quarter: {', '.join(sorted(anchors)) or 'none'}",
            f"real-account extra fee drag (lifetime): {100.0 * drag:.3f}% of equity"]


# ------------------------------------------------------------------------------------ doctor


def doctor_stock_sample(*, state_dir: Path, repo: Path, broker: Any, now: datetime) -> list[tuple[str, bool, str]]:
    """`council doctor --live-read`: the full stock gate on a few names (the committed sleeve's
    selected lines, else DOCTOR_SAMPLE), and the unchecked-line refusal. (name, good, detail) rows;
    details are reason codes, never numbers."""
    rows: list[tuple[str, bool, str]] = []
    try:
        base = committed(state_dir, repo)
    except StocksError as exc:
        return [("stock sample", False, str(exc))]
    policy = base.policy
    symbols: list[str] = list(DOCTOR_SAMPLE)
    if base.sleeve is not None:
        policy = base.with_sleeve()
        chosen = [ln.etoro_symbol for ln in base.sleeve.lines if ln.role == "selected"][:DOCTOR_SAMPLE_SIZE]
        symbols = chosen or symbols
        unchecked = gate.preflight_errors(policy)
        rows.append(("stock lines eligibility-checked", not unchecked, ", ".join(unchecked) or "all"))
    cfg = gate.gate_config(policy, unit_share=_unit_share(policy))
    try:
        verdicts = gate.doctor_sample(broker, symbols, cfg, now=now)
    except Exception as exc:
        return [*rows, ("stock sample", False, type(exc).__name__)]
    rows += [(f"stock gate {v.requested}", v.ok, v.reason) for v in verdicts]
    return rows


