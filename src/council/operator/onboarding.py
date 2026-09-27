"""Token-day onboarding probes (m5-readiness §10 M5-C; §4 K1–K16; §6 gates K1–K13, K17, K19, P2).

Pure probes over a READ client (`broker.etoro_read.EtoroReadClient`, or FakeEtoro behind it in
tests). Each returns an `Outcome`: report rows (gate, state, code, detail) plus the readiness record
they feed. The CLI (`council keys verify`, `doctor --live-read`, `instruments resolve`, `doctor
--record-fixtures`, `account set-mirror --from-broker`) runs them only after `@operator_command`
/ `require_operator` passed, and writes the record through `readiness.write_record`.

Rules:
- Read-only. This module imports no broker writer and calls only GET routes and the allow-listed
  read POSTs (eligibility, costs what-if). `keys verify` reads the WRITE token's portfolio list with
  a GET through a READ-client instance; nothing here can place, close or modify.
- Value-free output. Codes, symbols, day counts and NAV shares only: never an amount, an
  instrument id, a portfolio or token id, a token, or feed text (a canary test scans stdout and the
  records). Broker amounts stay inside the probe.
- The feed is probed only when LC1 is green (feed on AND `etoro-licence` attested): `take=1`, the
  status is recorded, the body is discarded. `--record-fixtures` stores the feed body as a key-shape
  skeleton (values replaced by their types) while LC1 is not green; with the feed off it is never
  requested.
- Fixtures go to `state_dir/licensed/fixtures/<UTC date>/` (0600 files, 0700 directories,
  `paths.assert_outside_repo`), where the licensed-content sweeper removes them after 7 days.
- `instruments.json` is append-only: an identity change raises InstrumentIdentityChanged and
  nothing is written.
- Capabilities (M5-D1) are read from `state_dir/account/capabilities.json`, either
  `{"capabilities": {"cfd_long": true, ...}}` or a flat mapping; anything else means none. A CFD
  vehicle waits for `cfd_long` ("held until cfd_long").
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from council import paths

READ_TOKEN_NAME = "council-read"
WRITE_TOKEN_NAME = "council-write"
WRITE_SCOPE_SUFFIX = "trade.real:write"
EXPIRY_GREEN_DAYS = 30
EXPIRY_RED_DAYS = 7
IDENTITY_TOLERANCE = 0.01          # first read's equity within 1% of the virtual balance
WHATIF_PROBE_USD = 1000.0          # hypothetical notional of the costs what-if (nothing is placed)
WHATIF_TRADE_COSTS = ("markup", "marketspread", "transactionfee", "sdrt")
FIXTURES_REL = ("licensed", "fixtures")
ONBOARDED_REL = ("account", "onboarded.json")
CAPABILITIES_REL = ("account", "capabilities.json")
ELIGIBILITY_BATCH = 100
RATES_SAMPLE = 3


class OnboardingError(RuntimeError):
    """A probe cannot run (no portfolio, a refused answer). Carries no value."""


@dataclass(frozen=True)
class Row:
    gate: str                       # a readiness gate id, or "info" (reported, never recorded)
    state: str                      # green | amber | red | info
    code: str
    detail: str = ""


@dataclass
class Outcome:
    record: str                     # the readiness record these rows feed ("keys", "live-read")
    rows: list[Row] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)   # extra report lines (per-line resolution)
    onboarded: bool = False

    def add(self, gate: str, state: str, code: str, detail: str = "") -> None:
        self.rows.append(Row(gate, state, code, detail))

    @property
    def ok(self) -> bool:
        return all(r.state != "red" for r in self.rows)

    def gates(self) -> dict[str, dict[str, str]]:
        return {r.gate: {"state": r.state, "code": r.code} for r in self.rows
                if r.state in ("green", "amber", "red") and r.gate != "info"}

    def report_lines(self) -> list[str]:
        out = [f"{r.state:<6} {r.gate:<5} {r.code}{('  — ' + r.detail) if r.detail else ''}" for r in self.rows]
        return [*out, *self.lines]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _failure_code(prefix: str, exc: BaseException) -> str:
    from council.broker.http import BrokerAuthError

    if isinstance(exc, BrokerAuthError):
        return f"{prefix}_denied"
    return f"{prefix}_failed:{_snake(type(exc).__name__)[:40]}"


def _portfolios(payload: Any) -> list[Mapping[str, Any]]:
    rows = payload.get("agentPortfolios") if isinstance(payload, Mapping) else None
    return [p for p in rows if isinstance(p, Mapping)] if isinstance(rows, list) else []


def _tokens(portfolio: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    tokens = portfolio.get("userTokens")
    out: dict[str, Mapping[str, Any]] = {}
    for tok in tokens if isinstance(tokens, list) else []:
        if isinstance(tok, Mapping) and isinstance(tok.get("userTokenName"), str):
            out[tok["userTokenName"]] = tok
    return out


def _identity(portfolio: Mapping[str, Any]) -> str | None:
    """The portfolio's id (else its name) as text; None when it carries neither (fail closed)."""
    for key in ("agentPortfolioId", "agentPortfolioName"):
        value = portfolio.get(key)
        if value is not None and str(value).strip():
            return f"{key}:{str(value).strip()}"
    return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def virtual_balance(payload: Any) -> float:
    """The one Agent Portfolio's `agentPortfolioVirtualBalance`; OnboardingError otherwise."""
    ports = _portfolios(payload)
    if len(ports) != 1:
        raise OnboardingError(f"expected exactly one Agent Portfolio, found {len(ports)}")
    value = ports[0].get("agentPortfolioVirtualBalance")
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise OnboardingError("the Agent Portfolio carries no virtual balance") from exc
    if not math.isfinite(out) or out <= 0:
        raise OnboardingError("the Agent Portfolio's virtual balance is not positive")
    return out


# ================================================================================= keys verify
def verify_keys(read_payload: Any, write_payload: Any, *, now: datetime, scopes_attested: bool) -> Outcome:
    """Gates K1–K4 from the portfolio list as each token sees it (GET /api/v1/agent-portfolios)."""
    out = Outcome("keys")
    ports, write_ports = _portfolios(read_payload), _portfolios(write_payload)
    tokens: dict[str, Mapping[str, Any]] = {}
    if len(ports) != 1:
        out.add("K1", "red", f"portfolio_count:{min(len(ports), 99)}", "the READ token must see exactly one")
    elif len(write_ports) != 1:
        out.add("K1", "red", f"write_portfolio_count:{min(len(write_ports), 99)}",
                "the WRITE token must see exactly one")
    elif _identity(ports[0]) is None or _identity(ports[0]) != _identity(write_ports[0]):
        out.add("K1", "red", "tokens_in_different_portfolios"
                if _identity(ports[0]) is not None else "portfolio_identity_unread")
    else:
        tokens = _tokens(ports[0])
        missing = [n for n in (READ_TOKEN_NAME, WRITE_TOKEN_NAME) if n not in tokens]
        if missing:
            out.add("K1", "red", "token_names_missing", f"expected {', '.join(missing)} in the portfolio")
        else:
            out.add("K1", "green", "one_portfolio", "council-read and council-write belong to it")
    read_tok, write_tok = tokens.get(READ_TOKEN_NAME), tokens.get(WRITE_TOKEN_NAME)
    if read_tok is None or write_tok is None:
        for gate in ("K2", "K3", "K4"):
            out.add(gate, "red", "tokens_unverified", "fix K1 first")
        return out

    # K2 scopes
    read_scopes, write_scopes = read_tok.get("scopeNames"), write_tok.get("scopeNames")
    if not isinstance(read_scopes, list) or not isinstance(write_scopes, list):
        if scopes_attested:
            out.add("K2", "green", "scopes_attested", "the API exposes no scopes; token-scopes attested")
        else:
            out.add("K2", "red", "unattested:token-scopes",
                    "the API exposes no scopes: check both tokens in the eToro UI, then "
                    "council-op ops attest token-scopes")
    elif any(str(s).endswith(":write") for s in read_scopes):
        out.add("K2", "red", "read_token_has_write_scope", "recreate council-read with read scopes only")
    elif not any(str(s).endswith(WRITE_SCOPE_SUFFIX) for s in write_scopes):
        out.add("K2", "red", "write_scope_missing", "council-write needs trade.real:write")
    else:
        out.add("K2", "green", "scopes_ok", "READ read-only; WRITE has trade.real:write")

    # K3 expiry: the nearest of both tokens
    days = [(ts - now).total_seconds() / 86400 for ts in
            (_parse_time(read_tok.get("expiresAt")), _parse_time(write_tok.get("expiresAt"))) if ts is not None]
    if not days:
        out.add("K3", "green", "no_expiry", "no expiry set")
    else:
        left = min(days)
        whole = max(int(left), 0)
        if left < EXPIRY_RED_DAYS:
            out.add("K3", "red", "expiry_under_7d", f"{whole} days left: rotate the token")
        elif left < EXPIRY_GREEN_DAYS:
            out.add("K3", "amber", "expiry_under_30d", f"{whole} days left")
        else:
            out.add("K3", "green", "expiry_ok", f"{whole} days left")

    # K4 IP whitelist (§6: READ none green, WRITE set amber, READ set red)
    if read_tok.get("ipsWhitelist"):
        out.add("K4", "red", "read_ip_whitelist_set", "the runner's READ token must not be IP-bound")
    elif write_tok.get("ipsWhitelist"):
        out.add("K4", "amber", "write_ip_whitelist_set", "WRITE bound to the operator's IP")
    else:
        out.add("K4", "green", "no_ip_whitelist")

    states = {r.gate: r.state for r in out.rows}
    out.onboarded = states.get("K1") == "green" and states.get("K2") == "green"
    return out


def probe_keys(read_client: Any, write_reader: Any, *, now: datetime, scopes_attested: bool) -> Outcome:
    """`keys verify`: one GET of the portfolio list with each token. `write_reader` is a READ-client
    instance holding the WRITE token (GET only; it has no write methods)."""
    return verify_keys(read_client.agent_portfolios(), write_reader.agent_portfolios(),
                       now=now, scopes_attested=scopes_attested)


def _write_private(path: Path, text: str) -> Path:
    paths.assert_outside_repo(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        path.unlink()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_onboarded(state_dir: Path, *, now: datetime) -> Path:
    """`account/onboarded.json` (0600): from now on a missing broker is a failure, never
    AWAITING_ACCOUNT (`context.is_onboarded`). Holds a timestamp only."""
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    return _write_private(state_dir.joinpath(*ONBOARDED_REL),
                          json.dumps({"version": 1, "onboarded_at": stamp}, sort_keys=True) + "\n")


def load_capabilities(state_dir: Path) -> dict[str, bool]:
    path = state_dir.joinpath(*CAPABILITIES_REL)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(data, Mapping) and isinstance(data.get("capabilities"), Mapping):
        data = data["capabilities"]
    if not isinstance(data, Mapping):
        return {}
    return {str(k): v is True or (isinstance(v, Mapping) and v.get("value") is True) for k, v in data.items()}


# ============================================================================ eligibility batch
@dataclass
class Batch:
    """One eligibility batch over every candidate of every line plus the held instruments."""

    raw: list[Mapping[str, Any]]
    rows: dict[str, Any]                              # upper symbol -> its one parsed row
    raw_by_symbol: dict[str, Mapping[str, Any]]       # upper symbol -> its one raw row
    found: dict[str, int]
    missing: list[str]
    ambiguous: list[str]


def candidate_symbols(policy: Any) -> list[str]:
    out: list[str] = []
    for line in policy.universe.lines:
        for vehicle in (*line.vehicles.long, *line.vehicles.short):
            out.append(vehicle.symbol)
    return list(dict.fromkeys(out))


def eligibility_batch(read_client: Any, symbols: Sequence[str], held_ids: Iterable[int], *,
                      now: datetime) -> Batch:
    from council.broker.eligibility import parse_eligibility_row
    from council.broker.etoro_read import ELIGIBILITY_PATH
    from council.broker.instruments import found_from_rows

    items: list[tuple[str, Any]] = [("symbols", s) for s in symbols]
    items += [("instrumentIds", int(i)) for i in dict.fromkeys(held_ids)]
    raw: list[Mapping[str, Any]] = []
    for start in range(0, len(items), ELIGIBILITY_BATCH):
        chunk = items[start:start + ELIGIBILITY_BATCH]
        body: dict[str, Any] = {"currency": "USD"}
        ids = [v for k, v in chunk if k == "instrumentIds"]
        syms = [v for k, v in chunk if k == "symbols"]
        if ids:
            body["instrumentIds"] = ids
        if syms:
            body["symbols"] = syms
        payload = read_client.post_read(ELIGIBILITY_PATH, body)
        got = payload.get("eligibilities") if isinstance(payload, Mapping) else None
        raw.extend(r for r in (got or []) if isinstance(r, Mapping))
    # a row asked for twice (by symbol and by held id) is one instrument, not an ambiguity
    unique: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for r in raw:
        unique[(str(r.get("symbol", "")).upper(), r.get("instrumentId"))] = r
    parsed_pairs = [(r, parse_eligibility_row(r, now)) for r in unique.values()]
    parsed = [p for _, p in parsed_pairs if p is not None]
    found, missing, ambiguous = found_from_rows(parsed, symbols)
    # exactly one broker row per symbol, counting rows the parser drops: an unparseable second row
    # must not make the parseable one look unique (fail closed)
    raw_count: dict[str, int] = {}
    for key_sym, _iid in unique:
        if key_sym:
            raw_count[key_sym] = raw_count.get(key_sym, 0) + 1
    for sym in list(found):
        if raw_count.get(sym.upper(), 0) > 1:
            del found[sym]
            ambiguous.append(sym)
    groups: dict[str, list[tuple[Mapping[str, Any], Any]]] = {}
    for r, p in parsed_pairs:
        if p is not None:
            groups.setdefault(p.symbol.upper(), []).append((r, p))
    single = {sym: g[0] for sym, g in groups.items() if len(g) == 1 and raw_count.get(sym, 0) <= 1}
    held = set(int(i) for i in held_ids)
    for sym, (_r, p) in single.items():             # held instruments outside the candidates
        if p.instrument_id in held and not any(s.upper() == sym for s in found):
            found[p.symbol] = p.instrument_id
    return Batch(raw=list(unique.values()), rows={s: p for s, (_r, p) in single.items()},
                 raw_by_symbol={s: r for s, (r, _p) in single.items()},
                 found=found, missing=missing, ambiguous=ambiguous)


def _expected_cost(policy: Any) -> Callable[[Any, Any, Any], float]:
    from council.risk.costs import carry_bps_day, per_side_bps
    from council.runtime import vehicle_asset_class

    owners = policy.universe.vehicle_map()
    by_line = policy.universe.by_symbol()

    def cost(vehicle: Any, _row: Any, config: Any) -> float:
        cls = vehicle_asset_class(by_line[owners[vehicle.symbol]], vehicle.settlement)
        side = per_side_bps(vehicle.settlement, cls, None, None, policy)
        lev = max(config.leverage_values) if config.leverage_values else 1
        return 2 * side + 20 * carry_bps_day(config.direction, vehicle.settlement, lev, cls, None, policy)

    return cost


def resolve_lines(policy: Any, batch: Batch, capabilities: Mapping[str, bool]) -> list[Any]:
    from council.broker.instruments import resolution_report

    return resolution_report(policy.universe.lines, batch.rows, batch.raw_by_symbol,
                             _expected_cost(policy), capabilities)


def _copy_floor_virtual_usd(policy: Any, mirror_ratio: float) -> float:
    from council.risk.costs import cost_floors

    return cost_floors(policy).min_trade.floor_real_usd / mirror_ratio


def mirror_ratio_for(policy: Any, state_dir: Path) -> float:
    from council.operator.mirror import MirrorError, load_mirror
    from council.risk.costs import cost_floors

    try:
        config = load_mirror(state_dir)
    except MirrorError:
        config = None
    return config.mirror_ratio if config is not None else cost_floors(policy).assumed.mirror_ratio


# ================================================================================ doctor --live-read
def cancel_route_verified() -> bool:
    """`broker.etoro_write.CANCEL_ROUTE_VERIFIED`, read from the module's source with `ast` so this
    module never imports the broker writer. Anything but a literal True reads as False."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("council.broker.etoro_write")
    try:
        tree = ast.parse(Path(spec.origin).read_text()) if spec and spec.origin else None
    except (OSError, SyntaxError):
        return False
    for node in tree.body if tree else []:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CANCEL_ROUTE_VERIFIED"
                                                for t in node.targets):
            return isinstance(node.value, ast.Constant) and node.value.value is True
    return False


def probe_live_read(read_client: Any, *, policy: Any, state_dir: Path, now: datetime,
                    feed_on: bool, feed_licensed: bool, held_ids: Iterable[int] = ()) -> Outcome:
    """Gates K5–K10, K12, K17 (§6 numbering) plus the candles row (info). The feed is requested only
    when LC1 is green, with take=1, and its body is discarded."""
    from council.broker.instruments import InstrumentMap
    from council.operator.mirror import MirrorError, load_mirror
    from council.risk.costs import floor_bps
    from council.runtime import vehicle_asset_class

    out = Outcome("live-read")
    imap = InstrumentMap.load(state_dir / "instruments.json")

    # K6 pnl
    portfolio = None
    try:
        portfolio = read_client.portfolio(imap.symbol_for)
        out.add("K6", "green", "pnl_ok")
    except Exception as exc:
        out.add("K6", "red", _failure_code("pnl", exc), type(exc).__name__)

    # K5 identity: the first read's equity equals the virtual balance within 1%, with 0 positions
    try:
        balance = virtual_balance(read_client.agent_portfolios())
    except Exception as exc:
        balance = None
        out.add("K5", "red", "identity_unread" if isinstance(exc, OnboardingError) else
                _failure_code("agent_portfolios", exc), type(exc).__name__)
    if balance is not None:
        if portfolio is None:
            out.add("K5", "red", "identity_unread", "no pnl read")
        elif portfolio.positions:
            out.add("K5", "red", "positions_present", "the Agent Portfolio must start with 0 positions")
        elif abs(portfolio.equity_usd - balance) > IDENTITY_TOLERANCE * balance:
            out.add("K5", "red", "equity_not_virtual_balance", "not the Agent Portfolio? (main account)")
        else:
            out.add("K5", "green", "identity_ok", "equity = virtual balance, 0 positions")

    # one eligibility batch: the vehicles every later probe uses
    held = [*held_ids, *([p.instrument_id for p in portfolio.positions] if portfolio else [])]
    resolutions: list[Any] = []
    try:
        batch = eligibility_batch(read_client, candidate_symbols(policy), held, now=now)
        resolutions = [r for r in resolve_lines(policy, batch, load_capabilities(state_dir)) if r.vehicle]
    except Exception as exc:
        batch = None
        out.add("info", "info", _failure_code("eligibility", exc), type(exc).__name__)
    ids = [batch.found[r.vehicle] for r in resolutions if batch and r.vehicle in batch.found]

    # K7 rates
    if not ids:
        out.add("K7", "red", "no_instrument_for_rates", "no candidate resolved: council-op instruments resolve")
    else:
        try:
            rates = read_client.rates(ids[:RATES_SAMPLE])
            got = rates.get("rates") if isinstance(rates, Mapping) else None
            out.add("K7", "green" if got else "red", "rates_ok" if got else "rates_empty")
        except Exception as exc:
            out.add("K7", "red", _failure_code("rates", exc),
                    "denied = no trading at all: contact eToro; never size from Tiingo closes")

    # K8 feed (LC1)
    if not feed_on:
        out.add("K8", "amber", "skipped_feed_off", "broker feed off: news from public sources only")
    elif not feed_licensed:
        out.add("K8", "amber", "skipped_licence", "etoro-licence not attested: the feed is not probed")
    else:
        try:
            read_client.feeds_news(take=1)                # status only: the body is discarded
            out.add("K8", "green", "feed_ok")
        except Exception as exc:
            out.add("K8", "red", _failure_code("feed", exc), type(exc).__name__)

    # K9 costs what-if <= floors (never lowers a floor); K10 minimum <= copy floor
    by_line = policy.universe.by_symbol()
    above, failed = 0, 0
    for res in resolutions:
        if batch is None or res.vehicle not in batch.found:
            continue
        body = {"action": "open", "transaction": "buy", "instrumentId": batch.found[res.vehicle],
                "settlementType": res.settlement, "orderType": "mkt", "leverage": 1,
                "amount": WHATIF_PROBE_USD, "orderCurrency": "usd"}
        try:
            payload = read_client.costs(body)
        except Exception:
            failed += 1
            continue
        costs = payload.get("costs") if isinstance(payload, Mapping) else None
        total = sum(float(c.get("amount") or 0.0) for c in costs or []
                    if isinstance(c, Mapping) and str(c.get("costType", "")).lower() in WHATIF_TRADE_COSTS)
        whatif_bps = 1e4 * total / WHATIF_PROBE_USD
        floor = floor_bps(res.settlement, vehicle_asset_class(by_line[res.line], res.settlement), policy)
        if whatif_bps > floor + 1e-9:
            above += 1
    if not resolutions:
        out.add("K9", "red", "no_resolved_vehicles")
    elif above:
        out.add("K9", "red", f"whatif_above_floor:{above}", "raise costs.yaml in a tagged commit before go-live")
    elif failed:
        out.add("K9", "red", f"whatif_failed:{failed}")
    else:
        out.add("K9", "green", "costs_within_floors")

    ratio = mirror_ratio_for(policy, state_dir)
    copy_floor = _copy_floor_virtual_usd(policy, ratio)
    over = sum(1 for r in resolutions if r.min_position_usd is not None and r.min_position_usd > copy_floor + 1e-9)
    if not resolutions:
        out.add("K10", "red", "no_resolved_vehicles")
    elif over:
        out.add("K10", "red", f"minimum_above_copy_floor:{over}", "broker minimum above the copy floor")
    else:
        out.add("K10", "green", "minimum_within_copy_floor")

    # K12 mirror ratio from the broker
    try:
        mirror = load_mirror(state_dir)
    except MirrorError:
        mirror = None
    if mirror is not None and mirror.source == "broker":
        out.add("K12", "green", "mirror_from_broker")
    else:
        out.add("K12", "red", "mirror_not_from_broker",
                "council-op account set-mirror --funding-usd <N> --from-broker")

    # K17 cancel route (never probed deliberately)
    if cancel_route_verified():
        out.add("K17", "green", "cancel_route_verified")
    else:
        out.add("K17", "amber", "cancel_route_unverified", "fail-closed waiting_for_market path active")

    # K14 (§4): candles entitlement, informational
    if ids:
        try:
            read_client.candles(ids[0], "OneDay", 1)
            out.add("info", "info", "candles_ok", "not used: history comes from Tiingo/Binance/Alpaca")
        except Exception as exc:
            out.add("info", "info", _failure_code("candles", exc), "not used")
    return out


# ============================================================================= instruments resolve
def probe_instruments(read_client: Any, *, policy: Any, state_dir: Path, now: datetime,
                      dry_run: bool = False) -> Outcome:
    """`instruments resolve`: every candidate of every line plus the held instruments in one
    eligibility batch; the map is merged append-only and saved (not with `dry_run`); gates K11, K19
    and P2 (computed privately; only its code is recorded)."""
    from council.broker.instruments import InstrumentMap
    from council.risk.config import risk_limits

    out = Outcome("live-read")
    path = state_dir / "instruments.json"
    imap = InstrumentMap.load(path)
    portfolio = read_client.portfolio(imap.symbol_for)
    held = [p.instrument_id for p in portfolio.positions]
    batch = eligibility_batch(read_client, candidate_symbols(policy), held, now=now)
    merged = imap.merged(batch.found, now, unresolved=[*batch.missing, *batch.ambiguous],
                         ambiguous=batch.ambiguous)           # raises on an identity change
    if not dry_run:
        merged.save()
    report = resolve_lines(policy, batch, load_capabilities(state_dir))

    nav = portfolio.equity_usd
    min_share = float(risk_limits(policy).deadband.min_nav_share)
    copy_floor = _copy_floor_virtual_usd(policy, mirror_ratio_for(policy, state_dir))
    binding = 0
    for res in report:
        if not res.vehicle:
            out.lines.append(f"  {res.line:<8} no eligible vehicle (unplannable)")
            continue
        floor_share = max(copy_floor, res.min_position_usd or 0.0) / nav if nav > 0 else math.inf
        if floor_share > min_share + 1e-12:
            binding += 1
        unit = f"{res.currency}/{res.price_unit}" if res.price_unit else "unit unknown (not planned)"
        bits = [f"  {res.line:<8} {res.vehicle:<10} {res.settlement:<4} {unit}",
                "whole units" if res.whole_units else "fractional",
                f"SL {res.sl_min_pct:g}–{res.sl_max_pct:g}%",
                f"floor {floor_share:.2%} NAV" if math.isfinite(floor_share) else "floor n/a"]
        if res.held_until:
            bits.append(f"held until {res.held_until}")
        out.lines.append("  ".join(bits))
    for sym in batch.ambiguous:
        out.lines.append(f"  {sym}: ambiguous (more than one broker row), left unresolved")
    for sym in batch.missing:
        out.lines.append(f"  {sym}: not found")

    unresolved = sum(1 for r in report if not r.vehicle)
    held_lines = sum(1 for r in report if r.held_until)
    if unresolved:
        out.add("K11", "red", f"lines_unresolved:{unresolved}", "a line without an eligible vehicle is unplannable")
    elif batch.ambiguous:
        out.add("K11", "amber", f"ambiguous_symbols:{len(batch.ambiguous)}", "left unresolved (fail closed)")
    elif held_lines:
        out.add("K11", "amber", f"lines_held:{held_lines}", "held by design until the capability is attested")
    else:
        out.add("K11", "green", "every_line_resolved")
    unknown = sum(1 for r in report if r.vehicle and r.price_unit is None)
    if unknown:
        out.add("K19", "red", f"unit_unknown:{unknown}", "currency or price unit unknown: not planned")
    else:
        out.add("K19", "green", "units_known", "currency, price unit, whole units and SL bounds recorded")
    if nav <= 0:
        out.add("P2", "red", "nav_unread")
    elif binding:
        out.add("P2", "amber", f"size_floor_binding:{binding}",
                "size floor above the public deadband share on some lines (count only)")
    else:
        out.add("P2", "green", "size_floor_ok")
    out.add("info", "info", "dry_run" if dry_run else "instruments_saved",
            f"{len(batch.found)} symbols mapped" if not dry_run else "nothing written")
    return out


# ============================================================================ account set-mirror
def mirror_from_broker(read_client: Any, *, state_dir: Path, funding_usd: float, now: datetime) -> tuple[Outcome, Any]:
    """`account set-mirror --funding-usd N --from-broker`: ratio = funding / the Agent Portfolio's
    virtual balance (read through the READ client). Returns (K12 outcome, MirrorConfig)."""
    from council.operator.mirror import set_mirror

    balance = virtual_balance(read_client.agent_portfolios())
    config = set_mirror(state_dir, funding_usd=funding_usd, virtual_nav_usd=balance, now=now, source="broker")
    out = Outcome("live-read")
    out.add("K12", "green", "mirror_from_broker")
    return out, config


# ============================================================================ doctor --record-fixtures
def skeleton(value: Any) -> Any:
    """The key shape of a payload: every value replaced by its type name; a list keeps one element."""
    if isinstance(value, Mapping):
        return {str(k): skeleton(v) for k, v in value.items()}
    if isinstance(value, list):
        return [skeleton(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def fixtures_dir(state_dir: Path, now: datetime) -> Path:
    return state_dir.joinpath(*FIXTURES_REL, now.astimezone(UTC).strftime("%Y-%m-%d"))


def record_fixtures(read_client: Any, *, policy: Any, state_dir: Path, now: datetime,
                    feed_on: bool, feed_licensed: bool) -> Outcome:
    """Raw payloads to `state_dir/licensed/fixtures/<UTC date>/` (0600), then the parsers run on
    them (gate K13: value-free — parse succeeds, fields present). The feed body is a key-shape
    skeleton unless LC1 is green, and is not requested while the feed is off."""
    from council.broker.eligibility import parse_eligibility
    from council.broker.etoro_read import ELIGIBILITY_PATH
    from council.broker.parsing import parse_pnl, parse_rates

    out = Outcome("live-read")
    directory = fixtures_dir(state_dir, now)
    paths.assert_outside_repo(directory)
    payloads: dict[str, Any] = {}
    failures: list[str] = []

    def fetch(name: str, fn: Callable[[], Any]) -> None:
        try:
            payloads[name] = fn()
        except Exception:
            failures.append(name)

    fetch("pnl", read_client.pnl)
    fetch("agent-portfolios", read_client.agent_portfolios)
    symbols = candidate_symbols(policy)[:ELIGIBILITY_BATCH]
    fetch("eligibility", lambda: read_client.post_read(ELIGIBILITY_PATH, {"currency": "USD", "symbols": symbols}))
    elig = payloads.get("eligibility")
    ids = [int(r["instrumentId"]) for r in (elig or {}).get("eligibilities", [])
           if isinstance(r, Mapping) and isinstance(r.get("instrumentId"), int)][:RATES_SAMPLE]
    if ids:
        fetch("rates", lambda: read_client.rates(ids))
        fetch("costs", lambda: read_client.costs({"action": "open", "transaction": "buy", "instrumentId": ids[0],
                                                  "settlementType": "cfd", "orderType": "mkt", "leverage": 1,
                                                  "amount": WHATIF_PROBE_USD, "orderCurrency": "usd"}))
    if feed_on:
        fetch("feed", lambda: read_client.feeds_news(take=1))
        if "feed" in payloads and not feed_licensed:
            payloads["feed"] = skeleton(payloads["feed"])
    for name, payload in payloads.items():
        _write_private(directory / f"{name}.json", json.dumps(payload, indent=1, sort_keys=True) + "\n")

    parse_failed: list[str] = []
    checks: dict[str, Callable[[Any], bool]] = {
        "pnl": lambda p: parse_pnl(p) is not None,
        "agent-portfolios": lambda p: len(_portfolios(p)) >= 1,
        "eligibility": lambda p: bool(parse_eligibility(p, now)),
        "rates": lambda p: isinstance(parse_rates(p), dict),
    }
    for name, check in checks.items():
        if name not in payloads:
            continue
        try:
            if not check(payloads[name]):
                parse_failed.append(name)
        except Exception:
            parse_failed.append(name)
    if failures:
        out.add("K13", "red", f"fixture_fetch_failed:{_snake(failures[0]).replace('-', '_')}",
                f"{len(failures)} payload(s) not fetched")
    elif parse_failed:
        out.add("K13", "red", f"fixture_parse_failed:{parse_failed[0].replace('-', '_')}")
    else:
        out.add("K13", "green", "fixtures_recorded", f"{len(payloads)} payloads; kept 7 days")
    feed_note = ("feed not requested (off)" if not feed_on else
                 "feed body stored in full (LC1 green)" if feed_licensed else "feed stored as a key-shape skeleton")
    out.add("info", "info", "fixtures_dir", f"state_dir/licensed/fixtures/{directory.name}/; {feed_note}")
    return out
