"""The loopback fake broker (m5-readiness §7.1): 127.0.0.1 only, marked sandbox only, a 0600 port
file removed on stop, and no request line or header ever logged."""

from __future__ import annotations

import stat

import httpx
import pytest

from council.broker.fake import FakeEtoro
from council.broker.fake_server import FakeBrokerServer, FakeServerError, port_file
from council.broker.http import check_base_url
from council.operator.release import REHEARSAL_MARKER


def _marked(tmp_path):
    state = tmp_path / "state-sandbox"
    state.mkdir()
    (state / REHEARSAL_MARKER).write_text("")
    return state


def test_refuses_a_non_loopback_host_and_an_unmarked_state_dir(tmp_path):
    with pytest.raises(FakeServerError, match="127.0.0.1 only"):
        FakeBrokerServer(FakeEtoro(), _marked(tmp_path), host="0.0.0.0")
    unmarked = tmp_path / "plain"
    unmarked.mkdir()
    with pytest.raises(FakeServerError, match="marked rehearsal sandbox"):
        FakeBrokerServer(FakeEtoro(), unmarked)


def test_serves_the_fake_through_the_pinned_url_and_logs_nothing(tmp_path, capfd):
    state = _marked(tmp_path)
    app, user = (f"srv-{n}-{tmp_path.name[-6:]}" for n in ("app", "usr"))
    fake = FakeEtoro(api_key=app, user_keys=(user,))
    with FakeBrokerServer(fake, state) as server:
        path = port_file(state)
        assert path.read_text().strip() == str(server.port)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        url = check_base_url(server.base_url, state)                  # the real pin accepts it
        headers = {"x-api-key": app, "x-user-key": user, "x-request-id": "r-1"}
        ok = httpx.get(f"{url}/api/v1/agent-portfolios", headers=headers, timeout=5)
        assert ok.status_code == 200 and "agentPortfolios" in ok.json()
        bad = httpx.get(f"{url}/api/v1/agent-portfolios", headers={**headers, "x-user-key": "nope"}, timeout=5)
        assert bad.status_code == 401
        fake.inject("GET", "/api/v1/trading/info/real/pnl", 503)
        assert httpx.get(f"{url}/api/v1/trading/info/real/pnl", headers=headers, timeout=5).status_code == 503
    assert not port_file(state).exists()
    captured = capfd.readouterr()
    assert app not in captured.err + captured.out and user not in captured.err + captured.out
    assert "GET /api" not in captured.err                             # no request line logged


def test_the_rehearsal_writer_refuses_an_unmarked_state_dir_and_a_foreign_url(tmp_path):
    from council.broker.http import BrokerConfigError
    from council.operator.rehearsal_writer import RehearsalWriterError, sandbox_write_client

    with pytest.raises(RehearsalWriterError):
        sandbox_write_client("a", "b", base_url="http://127.0.0.1:1", state_dir=tmp_path)
    (tmp_path / "REHEARSAL").write_text("x")
    (tmp_path / "fake-broker.port").write_text("45678")
    with pytest.raises((BrokerConfigError, RehearsalWriterError)):
        sandbox_write_client("a", "b", base_url="https://example.invalid", state_dir=tmp_path)
