"""Inside the dress rehearsal's [REHEARSAL] shell, `council cycle` / `watch` run on the sandbox
context: stub model, synthetic bars, the loopback fake broker, the sandbox remote (no network)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from council import cli
from council.llm.stub import StubGateway
from council.rehearsal import onboarding as ob

pytestmark = pytest.mark.capability_gates


@pytest.fixture
def dress(sandbox, monkeypatch):
    """The production `read_broker` refuses broker items under an agent ancestor (this test run);
    the sandbox's READ client is the same production client on the same throwaway keychain."""
    import council.context as context

    monkeypatch.setattr(context, "read_broker", lambda settings: sandbox.read_client())
    return sandbox


def test_outside_a_sandbox_cycle_and_watch_use_the_normal_context():
    assert cli._dress_context() is None


def test_dress_context_refuses_outside_a_marked_sandbox(tmp_path):
    with pytest.raises(ob.RehearsalError):
        ob.dress_cli_context(tmp_path)


def test_dress_context_is_stub_fake_broker_and_sandbox_remote(dress, sandbox):
    assert ob.step_keys(sandbox).ok
    ctx = cli._dress_context()
    assert isinstance(ctx.gateway, StubGateway)
    assert ctx.sources.history is ob.synthetic_history
    assert ctx.notifier is None
    assert ctx.sources.broker is not None
    assert ctx.state_dir.resolve() == sandbox.state_dir.resolve()


def test_dress_context_refuses_a_non_loopback_broker_url(sandbox, monkeypatch):
    monkeypatch.setenv("COUNCIL_ETORO_BASE_URL", "https://" + "example" + ".invalid")
    from council.broker.http import BrokerConfigError

    with pytest.raises((BrokerConfigError, ob.RehearsalError)):
        ob.dress_cli_context()


def test_first_cycle_in_the_dress_shell_proposes_and_publishes_only_to_the_sandbox(dress, sandbox):
    for step in (ob.step_keys, ob.step_keys_verify, ob.step_set_mirror, ob.step_attest_licence,
                 ob.step_live_read, ob.step_instruments, ob.step_record_fixtures, ob.step_smoke):
        assert step(sandbox).ok, sandbox.results[-1]
    from council.cycle import run_cycle

    if sandbox.clock.now() < ob.FIRST_SLOT:
        sandbox.clock.advance((ob.FIRST_SLOT + timedelta(minutes=3) - sandbox.clock.now()).total_seconds())
    out = run_cycle(ob.dress_cli_context(clock=sandbox.clock.now))
    assert out.decision_state == "proposed" and out.decision_id and out.published
