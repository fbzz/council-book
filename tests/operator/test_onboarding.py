"""Onboarding probes and commands against FakeEtoro only (m5-readiness §10 M5-C).

Green and red paths of every gate the package records: write scope on the READ token, two
portfolios, a 5-day expiry, rates 403, the feed skipped while LC1 is off, an ambiguous symbol, a
what-if above the floor, a minimum above the copy floor, a GBX unit, a whole-unit instrument, SL
bounds; no amount, id or token in stdout or the records (canary scan); fixtures never under
REPO_ROOT; `instruments.json` append-only.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from council import cli, paths
from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeEtoro, agent_portfolio, agent_token, leverage_config
from council.broker.instruments import (
    InstrumentIdentityChanged,
    InstrumentMap,
    UnknownPriceUnit,
    scale_price,
    unit_of,
)
from council.operator import onboarding, readiness
from council.policy import Policy
from tests.cli.operator_sim import simulate_operator

NOW = datetime(2026, 10, 1, 8, 50, tzinfo=UTC)
BALANCE = 12_345.0
READ_KEY = "canary-read-token-7Qx"
WRITE_KEY = "canary-write-token-9Zk"
HEADLINE = "CANARY HEADLINE licensed text"
IDS = {"CNDX.L": 90417, "BTC": 90418, "OIL": 90419, "QQQ": 90420, "NSDQ100": 90422}
CANARIES = [READ_KEY, WRITE_KEY, HEADLINE, "12345", "12,345", *map(str, IDS.values()), "90421"]
REAL_LONG = [leverage_config(settlement="REAL", direction="LONG", leverage_values=(1,), min_sl_pct=5.0,
                             max_sl_pct=50.0, min_position_amount=10.0)]


def _policy() -> Policy:
    policy = Policy.load(include_sleeve=False)
    keep = [ln for ln in policy.universe.lines if ln.symbol in ("NDX", "BTC", "OIL")]
    return policy.model_copy(update={"universe": policy.universe.model_copy(update={"lines": keep})})


def _tokens(*, read_scopes=("etoro-public:trade.real:read",),
            write_scopes=("etoro-public:trade.real:read", "etoro-public:trade.real:write"),
            expiry_days=90, read_ips=(), write_ips=()):
    exp = NOW + timedelta(days=expiry_days) if expiry_days is not None else None
    return [agent_token("council-read", scopes=read_scopes, expires_at=exp, ips=read_ips),
            agent_token("council-write", scopes=write_scopes, expires_at=exp, ips=write_ips)]


@pytest.fixture
def fake() -> FakeEtoro:
    f = FakeEtoro(clock=lambda: NOW, credit=BALANCE, api_key="test-app-key", user_keys=(READ_KEY, WRITE_KEY),
                  write_user_keys=(WRITE_KEY,))
    f.agent_portfolios = [agent_portfolio(virtual_balance=BALANCE, tokens=_tokens())]
    # NDX: EQQQ.L absent, CNDX.L real in GBX with whole units; QQQ ambiguous; NSDQ100 CFD
    f.add_instrument("CNDX.L", IDS["CNDX.L"], bid=1200.0, ask=1201.0, cost_bps=2.0,
                     configs=REAL_LONG, currency="GBX", units_quantity_type="WholeUnits",
                     min_position_exposure=10.0)
    f.add_instrument("QQQ", IDS["QQQ"], bid=500.0, ask=500.1, currency="USD")
    f.add_instrument("QQQ", 90421, bid=500.0, ask=500.1, currency="USD")
    f.add_instrument("NSDQ100", IDS["NSDQ100"], bid=20000.0, ask=20001.0, currency="USD")
    f.add_instrument("BTC", IDS["BTC"], bid=60000.0, ask=60010.0, cost_bps=100.0,
                     configs=REAL_LONG, currency="USD")
    f.add_instrument("OIL", IDS["OIL"], bid=70.0, ask=70.05, cost_bps=5.0, currency="USD")
    f.news = [{"id": "n1", "message": {"text": HEADLINE}}]
    return f


def _client(fake: FakeEtoro, key: str = READ_KEY) -> EtoroReadClient:
    return EtoroReadClient("test-app-key", key, transport=fake.transport(), sleep=lambda s: None)


@pytest.fixture
def state(tmp_path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    return root


def _codes(outcome) -> dict[str, tuple[str, str]]:
    return {r.gate: (r.state, r.code) for r in outcome.rows if r.gate != "info"}


def _no_canary(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, canary


# ------------------------------------------------------------------------------------ units
def test_gbx_prices_scale_to_gbp_through_one_function():
    assert scale_price(1234.0, "GBX") == pytest.approx((12.34, "GBP"))
    assert scale_price(1234.0, "GBp") == pytest.approx((12.34, "GBP"))
    assert scale_price(10.0, "usd") == (10.0, "USD")
    with pytest.raises(UnknownPriceUnit):
        scale_price(1.0, "XYZ")
    assert unit_of({"currency": "GBX"}) == ("GBP", "GBX")
    assert unit_of({"currency": "GBP", "priceUnit": "GBX"}) == ("GBP", "GBX")
    assert unit_of({"currency": "USD", "priceUnit": "GBX"}) == ("USD", None)     # mismatch: unknown
    assert unit_of({}) == (None, None)


# ------------------------------------------------------------------------------------ keys
def test_keys_verify_green_and_onboarded(fake):
    out = onboarding.probe_keys(_client(fake), _client(fake, WRITE_KEY), now=NOW, scopes_attested=False)
    assert _codes(out) == {"K1": ("green", "one_portfolio"), "K2": ("green", "scopes_ok"),
                           "K3": ("green", "expiry_ok"), "K4": ("green", "no_ip_whitelist")}
    assert out.onboarded
    assert fake.count("GET", "/api/v1/agent-portfolios") == 2
    assert all(r.method == "GET" for r in fake.requests)
    _no_canary("\n".join(out.report_lines()))


@pytest.mark.parametrize(("tokens", "gate", "expected"), [
    (dict(read_scopes=("etoro-public:trade.real:read", "etoro-public:trade.real:write")), "K2",
     ("red", "read_token_has_write_scope")),
    (dict(write_scopes=("etoro-public:trade.real:read",)), "K2", ("red", "write_scope_missing")),
    (dict(read_scopes=None, write_scopes=None), "K2", ("red", "unattested:token-scopes")),
    (dict(expiry_days=5), "K3", ("red", "expiry_under_7d")),
    (dict(expiry_days=20), "K3", ("amber", "expiry_under_30d")),
    (dict(read_ips=("203.0.113.9",)), "K4", ("red", "read_ip_whitelist_set")),
    (dict(write_ips=("203.0.113.9",)), "K4", ("amber", "write_ip_whitelist_set")),
])
def test_keys_verify_red_and_amber_paths(fake, tokens, gate, expected):
    fake.agent_portfolios = [agent_portfolio(virtual_balance=BALANCE, tokens=_tokens(**tokens))]
    out = onboarding.probe_keys(_client(fake), _client(fake, WRITE_KEY), now=NOW, scopes_attested=False)
    assert _codes(out)[gate] == expected
    assert not out.onboarded or gate != "K2"


def test_scopes_attested_when_the_api_exposes_none(fake):
    fake.agent_portfolios = [agent_portfolio(virtual_balance=BALANCE, tokens=_tokens(read_scopes=None,
                                                                                    write_scopes=None))]
    out = onboarding.probe_keys(_client(fake), _client(fake, WRITE_KEY), now=NOW, scopes_attested=True)
    assert _codes(out)["K2"] == ("green", "scopes_attested")


def test_two_portfolios_and_foreign_token_names_are_red(fake):
    fake.agent_portfolios = [agent_portfolio("A", tokens=_tokens()), agent_portfolio("B", tokens=_tokens())]
    out = onboarding.probe_keys(_client(fake), _client(fake, WRITE_KEY), now=NOW, scopes_attested=False)
    assert _codes(out)["K1"] == ("red", "portfolio_count:2")
    assert not out.onboarded
    fake.agent_portfolios = [agent_portfolio(tokens=[agent_token("other")])]
    out = onboarding.probe_keys(_client(fake), _client(fake, WRITE_KEY), now=NOW, scopes_attested=False)
    assert _codes(out)["K1"] == ("red", "token_names_missing")


def test_onboarded_file_is_private_and_outside_the_repo(state):
    path = onboarding.write_onboarded(state, now=NOW)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.resolve().is_relative_to(paths.REPO_ROOT)
    assert set(json.loads(path.read_text())) == {"version", "onboarded_at"}


# ------------------------------------------------------------------------------------ live read
def test_live_read_green_path_skips_the_feed_while_lc1_is_off(fake, state):
    onboarding.mirror_from_broker(_client(fake), state_dir=state, funding_usd=1000.0, now=NOW)
    out = onboarding.probe_live_read(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=True, feed_licensed=False)
    codes = _codes(out)
    assert codes["K5"] == ("green", "identity_ok")
    assert codes["K6"] == ("green", "pnl_ok")
    assert codes["K7"] == ("green", "rates_ok")
    assert codes["K8"] == ("amber", "skipped_licence")
    assert codes["K9"] == ("green", "costs_within_floors")
    assert codes["K10"] == ("green", "minimum_within_copy_floor")
    assert codes["K12"] == ("green", "mirror_from_broker")
    assert codes["K17"] == ("amber", "cancel_route_unverified")
    assert fake.count("GET", "/api/v1/feeds/news") == 0
    assert not any(r.method in ("PATCH",) or "/execution/" in r.path for r in fake.requests)
    _no_canary("\n".join(out.report_lines()))


def test_live_read_probes_the_feed_only_when_lc1_is_green(fake, state):
    out = onboarding.probe_live_read(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=True, feed_licensed=True)
    assert _codes(out)["K8"] == ("green", "feed_ok")
    news = [r for r in fake.requests if r.path == "/api/v1/feeds/news"]
    assert len(news) == 1 and news[0].params["take"] == "1"
    _no_canary("\n".join(out.report_lines()))
    fake.requests.clear()
    out = onboarding.probe_live_read(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=False, feed_licensed=True)
    assert _codes(out)["K8"] == ("amber", "skipped_feed_off")
    assert fake.count("GET", "/api/v1/feeds/news") == 0


def test_live_read_red_paths(fake, state):
    fake.inject("GET", "/api/v2/market-data/rates", 403, times=5)
    fake.instrument("OIL").cost_bps = 40.0                     # what-if above the 8 bps commodity floor
    fake.instrument("BTC").row["minPositionExposure"] = 5_000.0  # above the copy floor in virtual USD
    fake.add_position("BTC", is_buy=True, units=0.01, sl_rate=50_000.0, settlement="real")
    out = onboarding.probe_live_read(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=False, feed_licensed=False)
    codes = _codes(out)
    assert codes["K7"] == ("red", "rates_denied")
    assert codes["K9"][0] == "red" and codes["K9"][1].startswith("whatif_above_floor:")
    assert codes["K10"] == ("red", "minimum_above_copy_floor:1")
    assert codes["K5"] == ("red", "positions_present")
    assert codes["K12"] == ("red", "mirror_not_from_broker")
    _no_canary("\n".join(out.report_lines()))


def test_identity_red_when_equity_is_not_the_virtual_balance(fake, state):
    fake.agent_portfolios = [agent_portfolio(virtual_balance=BALANCE * 3, tokens=_tokens())]
    out = onboarding.probe_live_read(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=False, feed_licensed=False)
    assert _codes(out)["K5"] == ("red", "equity_not_virtual_balance")


# ------------------------------------------------------------------------------------ instruments
def test_instruments_resolve_report_and_gates(fake, state):
    onboarding.mirror_from_broker(_client(fake), state_dir=state, funding_usd=1000.0, now=NOW)
    out = onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW)
    codes = _codes(out)
    assert codes["K11"] == ("amber", "ambiguous_symbols:1")
    assert codes["K19"] == ("green", "units_known")
    assert codes["P2"] == ("green", "size_floor_ok")
    text = "\n".join(out.report_lines())
    assert "NDX      CNDX.L     real GBP/GBX" in text
    assert "whole units" in text and "SL 5–50%" in text
    assert "held until cfd_long" in text                   # OIL: the overlay CFD waits for cfd_long
    assert "QQQ: ambiguous" in text
    _no_canary(text)
    imap = InstrumentMap.load(state / "instruments.json")
    assert imap.get("CNDX.L") == IDS["CNDX.L"] and "QQQ" not in imap and "EQQQ.L" not in imap
    assert stat.S_IMODE((state / "instruments.json").stat().st_mode) == 0o600
    # every candidate of every line in ONE eligibility request
    assert fake.count("POST", "/api/v2/trading/info/eligibility") == 1


def test_instruments_resolve_marks_units_unknown_and_binding_floors(fake, state):
    fake.instrument("BTC").row.pop("currency")
    fake.instrument("CNDX.L").row["minPositionExposure"] = 1_000.0     # > 2% of the NAV
    out = onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW)
    codes = _codes(out)
    assert codes["K19"] == ("red", "unit_unknown:1")
    assert codes["P2"] == ("amber", "size_floor_binding:1")
    assert "unit unknown (not planned)" in "\n".join(out.lines)


def test_capability_releases_the_held_cfd_line(fake, state):
    (state / "account").mkdir()
    (state / "account" / "capabilities.json").write_text(json.dumps({"capabilities": {"cfd_long": True}}))
    del fake.instruments[90421]                                       # QQQ no longer ambiguous
    out = onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW)
    assert _codes(out)["K11"] == ("green", "every_line_resolved")


def test_instruments_json_is_append_only(fake, state):
    onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW)
    before = (state / "instruments.json").read_bytes()
    inst = fake.instruments.pop(IDS["BTC"])
    inst.instrument_id = 99999
    inst.row["instrumentId"] = 99999
    fake.instruments[99999] = inst
    with pytest.raises(InstrumentIdentityChanged):
        onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW)
    assert (state / "instruments.json").read_bytes() == before


def test_dry_run_writes_nothing(fake, state):
    out = onboarding.probe_instruments(_client(fake), policy=_policy(), state_dir=state, now=NOW, dry_run=True)
    assert not (state / "instruments.json").exists()
    assert _codes(out)["K19"] == ("green", "units_known")


# ------------------------------------------------------------------------------------ mirror
def test_set_mirror_from_broker_uses_the_virtual_balance(fake, state):
    out, config = onboarding.mirror_from_broker(_client(fake), state_dir=state, funding_usd=2469.0, now=NOW)
    assert config.mirror_ratio == pytest.approx(2469.0 / BALANCE)
    assert config.source == "broker"
    assert _codes(out) == {"K12": ("green", "mirror_from_broker")}
    fake.agent_portfolios = [agent_portfolio("A"), agent_portfolio("B")]
    with pytest.raises(onboarding.OnboardingError):
        onboarding.mirror_from_broker(_client(fake), state_dir=state, funding_usd=2469.0, now=NOW)


# ------------------------------------------------------------------------------------ fixtures
def test_record_fixtures_skeletons_the_feed_while_lc1_is_off(fake, state):
    out = onboarding.record_fixtures(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                                     feed_on=True, feed_licensed=False)
    assert _codes(out) == {"K13": ("green", "fixtures_recorded")}
    directory = state / "licensed" / "fixtures" / "2026-10-01"
    files = sorted(p.name for p in directory.iterdir())
    assert files == ["agent-portfolios.json", "costs.json", "eligibility.json", "feed.json", "pnl.json", "rates.json"]
    for p in directory.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert not p.resolve().is_relative_to(paths.REPO_ROOT)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    feed = (directory / "feed.json").read_text()
    assert HEADLINE not in feed and '"str"' in feed
    _no_canary("\n".join(out.report_lines()))


def test_record_fixtures_never_requests_the_feed_while_off(fake, state):
    onboarding.record_fixtures(_client(fake), policy=_policy(), state_dir=state, now=NOW,
                               feed_on=False, feed_licensed=False)
    assert fake.count("GET", "/api/v1/feeds/news") == 0
    assert not (state / "licensed" / "fixtures" / "2026-10-01" / "feed.json").exists()


def test_record_fixtures_refuses_a_state_dir_inside_the_repo(fake):
    with pytest.raises(Exception, match="inside the public repo"):
        onboarding.record_fixtures(_client(fake), policy=_policy(), state_dir=paths.REPO_ROOT / "state-x",
                                   now=NOW, feed_on=False, feed_licensed=False)
    assert not (paths.REPO_ROOT / "state-x").exists()


def test_skeleton_keeps_the_shape_only():
    assert onboarding.skeleton({"a": [{"t": "x", "n": 1.5}], "b": None, "c": True, "d": []}) == \
        {"a": [{"t": "str", "n": "float"}], "b": "null", "c": "bool", "d": []}


# ------------------------------------------------------------------------------------ CLI
@pytest.fixture
def cli_world(fake, state, monkeypatch):
    import council.context as context
    from council.operator import keychain

    simulate_operator(monkeypatch)
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(state))
    monkeypatch.setattr(context, "read_broker", lambda settings: _client(fake))
    secrets = {keychain.API_KEY_SERVICE: "test-app-key", keychain.WRITE_SERVICE: WRITE_KEY}
    monkeypatch.setattr(keychain, "read_secret", lambda service, *a, **k: secrets[service])
    monkeypatch.setattr(keychain, "unlock_write_keychain", lambda **k: None)
    monkeypatch.setattr(keychain, "lock_write_keychain", lambda **k: None)
    monkeypatch.setattr(cli, "_onboarding_transport", lambda: fake.transport())
    monkeypatch.setattr(cli, "_onboarding_policy", _policy)
    monkeypatch.setattr(cli, "_onboarding_now", lambda: NOW)
    monkeypatch.setattr(readiness.Probes, "head", lambda self: "a" * 40)
    return fake


def _invoke(argv):
    return CliRunner().invoke(cli.app, argv)


def test_cli_token_day_sequence_writes_value_free_records(cli_world, state):
    fake = cli_world
    steps = [["keys", "verify"],
             ["account", "set-mirror", "--funding-usd", "1000", "--from-broker"],
             ["instruments", "resolve"],
             ["doctor", "--record-fixtures"]]
    output = ""
    for argv in steps:
        result = _invoke(argv)
        output += result.output
        assert result.exit_code in (0, 1), (argv, result.output, result.exception)
    _no_canary(output)
    keys = json.loads((state / "readiness" / "keys.json").read_text())
    assert keys["gates"]["K1"] == {"state": "green", "code": "one_portfolio"}
    live = (state / "readiness" / "live-read.json").read_text()
    for gate in ("K11", "K12", "K13", "K19", "P2"):
        assert f'"{gate}"' in live
    _no_canary(live + json.dumps(keys))
    assert (state / "account" / "onboarded.json").is_file()
    assert stat.S_IMODE((state / "readiness" / "keys.json").stat().st_mode) == 0o600
    assert not any(r.method in ("PATCH",) or "/execution/" in r.path for r in fake.requests)


def test_cli_instruments_identity_change_refuses_and_writes_nothing(cli_world, state):
    assert _invoke(["instruments", "resolve"]).exit_code in (0, 1)
    before = (state / "instruments.json").read_bytes()
    inst = cli_world.instruments.pop(IDS["BTC"])
    inst.instrument_id = 99999
    inst.row["instrumentId"] = 99999
    cli_world.instruments[99999] = inst
    result = _invoke(["instruments", "resolve"])
    assert result.exit_code == 2 and "identity" in result.output
    assert (state / "instruments.json").read_bytes() == before
    _no_canary(result.output)


@pytest.mark.parametrize("argv", [["keys", "verify"], ["instruments", "resolve"], ["doctor", "--record-fixtures"],
                                  ["account", "set-mirror", "--funding-usd", "1000", "--from-broker"]])
def test_onboarding_commands_refuse_an_agent(argv, cli_world, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    result = _invoke(argv)
    assert result.exit_code == 2 and "refused" in result.output
    assert cli_world.requests == []


# ------------------------------------------------------------------------ review fixes (fail closed)
def test_k1_red_when_neither_portfolio_carries_an_identity():
    port = {"userTokens": [{"userTokenName": "council-read"}, {"userTokenName": "council-write"}]}
    out = onboarding.verify_keys({"agentPortfolios": [port]}, {"agentPortfolios": [dict(port)]},
                                 now=NOW, scopes_attested=True)
    assert _codes(out)["K1"] == ("red", "portfolio_identity_unread")
    assert not out.onboarded


def test_an_unparseable_second_row_makes_the_symbol_ambiguous(fake):
    from council.broker.fake import eligibility_row

    good = eligibility_row("BTC", IDS["BTC"], configs=REAL_LONG, currency="USD")

    class Stub:
        def post_read(self, _path, _body):
            return {"eligibilities": [good, {"symbol": "BTC", "instrumentId": "not-an-id"}]}

    batch = onboarding.eligibility_batch(Stub(), ["BTC"], [], now=NOW)
    assert "BTC" not in batch.found and "BTC" in batch.ambiguous
    assert "BTC" not in batch.rows
