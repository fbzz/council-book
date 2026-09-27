"""The parsers against the operator's recorded broker payloads (m5-readiness §10 M5-C, gate K13).

`council-op doctor --record-fixtures` stores raw payloads in `state_dir/licensed/fixtures/<date>/`.
The operator runs, in the operator terminal:

    COUNCIL_PRIVATE_FIXTURES=<that directory> uv run pytest -m private_fixtures --tb=no -q tests/contract

Rules: skipped unless COUNCIL_PRIVATE_FIXTURES names a directory outside the repository AND
`guards.assert_current_process_is_operator` passes (never in CI or an agent); assertions are
value-free (parse succeeds, field present, type matches) and no message carries a payload value;
run with `--tb=no` so no local variable is printed either.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from council import paths

pytestmark = pytest.mark.private_fixtures


def _directory() -> Path:
    raw = os.environ.get("COUNCIL_PRIVATE_FIXTURES", "").strip()
    if not raw:
        pytest.skip("COUNCIL_PRIVATE_FIXTURES not set")
    from council.operator import guards

    try:
        guards.assert_current_process_is_operator()
    except guards.GuardError:
        pytest.skip("not the operator terminal")
    directory = Path(raw).expanduser().resolve()
    if directory.is_relative_to(paths.REPO_ROOT.resolve()):
        pytest.fail("private fixtures must live outside the repository", pytrace=False)
    if not directory.is_dir():
        pytest.skip("fixture directory missing")
    return directory


def _load(name: str):
    path = _directory() / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"{name}.json not recorded")
    try:
        return json.loads(path.read_text())
    except ValueError:
        pytest.fail(f"{name}.json is not JSON", pytrace=False)


def _check(ok: bool, what: str) -> None:
    if not ok:
        pytest.fail(what, pytrace=False)          # a fixed message: never a payload value


def test_pnl_parses():
    from council.broker.parsing import parse_pnl

    payload = _load("pnl")
    try:
        read = parse_pnl(payload)
    except Exception as exc:
        pytest.fail(f"parse_pnl raised {type(exc).__name__}", pytrace=False)
    _check(isinstance(read.equity_usd, float), "equity is not a float")
    _check(isinstance(read.positions, list), "positions is not a list")


def test_agent_portfolios_carry_the_fields_keys_verify_reads():
    payload = _load("agent-portfolios")
    _check(isinstance(payload, dict) and isinstance(payload.get("agentPortfolios"), list), "agentPortfolios list")
    for port in payload["agentPortfolios"]:
        _check(isinstance(port.get("agentPortfolioVirtualBalance"), int | float), "virtual balance type")
        _check(isinstance(port.get("userTokens"), list), "userTokens list")
        for tok in port["userTokens"]:
            _check(isinstance(tok.get("userTokenName"), str), "userTokenName type")
            _check("scopeNames" not in tok or isinstance(tok["scopeNames"], list), "scopeNames type")
            _check("expiresAt" not in tok or isinstance(tok["expiresAt"], str | None), "expiresAt type")
            _check("ipsWhitelist" not in tok or isinstance(tok["ipsWhitelist"], list | None), "ipsWhitelist type")


def test_eligibility_parses_and_names_a_unit():
    from council.broker.eligibility import parse_eligibility
    from council.broker.instruments import unit_of

    payload = _load("eligibility")
    try:
        rows = parse_eligibility(payload, datetime.now(UTC))
    except Exception as exc:
        pytest.fail(f"parse_eligibility raised {type(exc).__name__}", pytrace=False)
    _check(bool(rows), "no eligibility row parsed")
    raw = payload.get("eligibilities") or []
    known = sum(1 for r in raw if unit_of(r)[1] is not None)
    # K15/K19: until a currency field is confirmed here, every vehicle stays `unit_unknown`
    _check(known == len(raw), f"{len(raw) - known} of {len(raw)} rows name no known currency/price unit")


def test_rates_parse():
    from council.broker.parsing import parse_rates

    payload = _load("rates")
    try:
        quotes = parse_rates(payload)
    except Exception as exc:
        pytest.fail(f"parse_rates raised {type(exc).__name__}", pytrace=False)
    _check(isinstance(quotes, dict), "rates did not parse to a mapping")


def test_costs_what_if_has_cost_rows():
    payload = _load("costs")
    _check(isinstance(payload, dict) and isinstance(payload.get("costs"), list), "costs list")
    for row in payload["costs"]:
        _check(isinstance(row.get("costType"), str), "costType type")
        _check(isinstance(row.get("amount"), int | float), "amount type")
