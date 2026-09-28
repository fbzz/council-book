"""Rehearsal variants (m5-readiness §7.2 table) on fresh sandboxes: each one walks the onboarding
steps it needs through the loopback fake broker, then breaks one thing.

Covered here: V1 (401 on pnl), V2a (feed unlicensed: 0 feed requests), V4 (403 on rates), V5 (READ token with a write scope), V9 (a base
URL outside the pin), V16 (marker without COUNCIL_KEYCHAIN_FILE), the S6 minimum above the 1% cap.
Elsewhere: V3/V11 `tests/integration/test_broker_containment.py`, V6/V8/V10 `tests/execution`,
V17 `tests/operator/test_smoke.py`, V12–V14 `tests/execution/test_planner.py`, V18 `tests/publish`."""

from __future__ import annotations

import pytest

from council.broker.http import BrokerConfigError, check_base_url
from council.operator import keychain as kc
from council.rehearsal import onboarding as ob
from council.rehearsal import scenario

pytestmark = pytest.mark.capability_gates


def _onboard(box: ob.Sandbox) -> None:
    for step in (ob.step_keys, ob.step_keys_verify, ob.step_set_mirror, ob.step_instruments):
        result = step(box)
        assert result.ok, (result.step, result.data.get("lines"))


def test_v1_expired_token_skips_the_cycle_and_alerts_once(sandbox):
    from council.cycle import run_cycle

    _onboard(sandbox)
    sandbox.fake.inject("GET", "/api/v1/trading/info/real/pnl", 401, times=50)
    sandbox.clock.advance((ob.FIRST_SLOT - sandbox.clock.now()).total_seconds() + 180)
    out = run_cycle(sandbox.cycle_context())
    assert out.status == "skipped_broker" and "broker_error:auth" in out.flags, (out.status, out.flags)
    urgent = [n for n in sandbox.notifier.sent if n[2] == "urgent"]
    assert len(urgent) == 1
    assert not ob.leak_findings(sandbox, [n[1] for n in sandbox.notifier.sent])


def test_v4_rates_403_turns_k7_red(sandbox):
    _onboard(sandbox)
    sandbox.fake.inject("GET", "/api/v2/market-data/rates", 403, times=50)
    result = ob.step_live_read(sandbox)
    assert result.data["gates"]["K7"]["state"] == "red" and not result.ok


def test_v5_a_read_token_with_a_write_scope_is_red(tmp_path, monkeypatch):
    from tests.rehearsal.conftest import use_sandbox

    box = ob.Sandbox.create(tmp_path / "v5", read_scopes=("etoro-public:trade.real:read",
                                                          "etoro-public:trade.real:write"))
    box.start_broker()
    try:
        use_sandbox(box, monkeypatch)
        ob.step_keys(box)
        result = ob.step_keys_verify(box)
        assert not result.ok and result.data["gates"]["K2"]["state"] == "red"
    finally:
        box.stop()


@pytest.mark.parametrize("url", ["https://example.invalid", "wrong-port"])
def test_v9_a_base_url_outside_the_pin_is_refused_before_any_socket(sandbox, url, monkeypatch):
    from council.context import UnavailableBroker, read_broker
    from council.settings import Settings
    from tests.cli.operator_sim import simulate_operator

    if url == "wrong-port":
        url = f"http://127.0.0.1:{sandbox.server.port + 1}"
    with pytest.raises(BrokerConfigError):
        check_base_url(url, sandbox.state_dir)
    ob.step_keys(sandbox)
    simulate_operator(monkeypatch)                       # clean ancestors: the keychain read may run
    monkeypatch.setenv("COUNCIL_ETORO_BASE_URL", url)
    before = len(sandbox.fake.requests)
    client = read_broker(Settings.from_env(keychain=False))
    assert isinstance(client, UnavailableBroker)
    with pytest.raises(BrokerConfigError):
        client.pnl()
    assert len(sandbox.fake.requests) == before          # nothing reached the fake server


def test_v16_marker_without_the_throwaway_file_raises_before_security(sandbox, monkeypatch):
    monkeypatch.delenv("COUNCIL_KEYCHAIN_FILE")
    before = len(sandbox.security.argv_log)
    for service in (kc.READ_SERVICE, kc.API_KEY_SERVICE):
        with pytest.raises(kc.KeychainError, match="COUNCIL_KEYCHAIN_FILE"):
            kc.read_secret(service, runner=sandbox.security, env={"COUNCIL_ROLE": "dev"},
                           ancestors=list(ob.OPERATOR_ANCESTORS))
    with pytest.raises(kc.KeychainError, match="COUNCIL_KEYCHAIN_FILE"):
        kc.store_token_interactive(kc.READ_SERVICE, None, getpass_fn=lambda _p: "x" * 20,
                                   runner=sandbox.security)
    assert len(sandbox.security.argv_log) == before and sandbox.fake.requests == []


def test_v16b_under_the_marker_a_foreign_keychain_is_refused(sandbox, tmp_path):
    with pytest.raises(kc.KeychainError, match="only COUNCIL_KEYCHAIN_FILE"):
        kc.read_secret(kc.READ_SERVICE, keychain=tmp_path / "other.keychain-db", runner=sandbox.security,
                       env=sandbox.operator_env(), ancestors=list(ob.OPERATOR_ANCESTORS))
    # a non-broker item is not redirected (it never reaches the fake broker either way)
    assert kc._sandbox_keychain("council-book.tiingo", None) is None


def test_s6_minimum_above_the_smoke_cap_is_refused(sandbox):
    from council.operator import smoke

    _onboard(sandbox)
    scenario.min_above_cap(sandbox.fake)
    out: list[str] = []
    with pytest.raises(smoke.SmokeRefused, match="smoke_min_above_cap"):
        smoke.propose("S6", sandbox.smoke_deps(out))
    assert sandbox.ledger.pending() == []


def test_v2a_unlicensed_feed_cycle_runs_on_public_items_with_zero_feed_requests(sandbox):
    """The feed constant is on (user decision) but the licence is not attested: LC1 not green, so
    the broker feed is never requested and the news role reads the public `P:` items only."""
    from council.cycle import run_cycle

    _onboard(sandbox)
    assert ob.feed_state(sandbox) == (True, False)
    assert ob.step_live_read(sandbox).data["feed"] == 0                     # doctor honours LC1
    sandbox.clock.advance((ob.FIRST_SLOT - sandbox.clock.now()).total_seconds() + 180)
    out = run_cycle(sandbox.cycle_context())
    assert out.cycle_id and sandbox.public_news.calls >= 1
    assert not ob.leak_findings(sandbox, [n[1] for n in sandbox.notifier.sent])
    assert sandbox.feed_requests() == 0                                     # the cycle must honour it too
