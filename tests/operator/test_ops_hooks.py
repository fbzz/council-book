"""M5-E2: ops hooks. Healthcheck ping from the watch (loopback fake), daily backup, runner flag, G24."""
from __future__ import annotations

import dataclasses
import http.server
import os
import stat
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from council import context, watch
from council.cycle import RUNNER_LAUNCHD, runner_flags
from council.ops import backup
from council.publish.gitops import Publisher
from tests.integration.test_end_to_end import NOW, _ctx

SECRET = "secret-uuid-canary-7f3e"


class _Hits(http.server.BaseHTTPRequestHandler):
    hits: list[tuple[str, bytes]] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).hits.append((self.path, body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def hc_server():
    handler = type("H", (_Hits,), {"hits": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{SECRET}", handler.hits
    finally:
        server.shutdown()
        server.server_close()


def _live(tmp_path, url, clock=lambda: NOW):
    ctx = _ctx(tmp_path, clock=clock)
    ctx.settings = dataclasses.replace(ctx.settings, mode="live", healthcheck_url=url)
    return ctx


def test_success_ping_at_end_of_run(tmp_path, hc_server):
    url, hits = hc_server
    ctx = _live(tmp_path, url)
    out = watch.run_watch(ctx)
    assert out.fail_code is None
    assert hits == [(f"/{SECRET}", b"")]
    assert ctx.ledger.get_runtime("ops.healthcheck")["ok"] is True


def test_fail_ping_on_urgent_alert(tmp_path, hc_server):
    url, hits = hc_server
    ctx = _live(tmp_path, url)
    ctx.ledger.set_runtime("last_cycle", {"cycle_id": "2026-09-30T0040Z",
                                          "at": (NOW - timedelta(hours=9)).isoformat()})
    out = watch.run_watch(ctx)
    assert out.fail_code == "urgent_alert"
    assert hits == [(f"/{SECRET}/fail", b"urgent_alert")]
    assert ctx.ledger.get_runtime("ops.healthcheck")["ok"] is False


def test_fail_ping_on_exception_then_reraises(tmp_path, hc_server, monkeypatch):
    url, hits = hc_server
    ctx = _live(tmp_path, url)

    def boom(_ctx):
        raise RuntimeError(f"detail {SECRET} /Users/x")

    monkeypatch.setattr(watch, "_run", boom)
    with pytest.raises(RuntimeError):
        watch.run_watch(ctx)
    assert hits == [(f"/{SECRET}/fail", b"watch_exception:runtimeerror")]


def test_no_ping_outside_live_or_without_url(tmp_path, hc_server):
    url, hits = hc_server
    ctx = _ctx(tmp_path)
    ctx.settings = dataclasses.replace(ctx.settings, mode="dry_run", healthcheck_url=url)
    watch.run_watch(ctx)
    ctx.settings = dataclasses.replace(ctx.settings, mode="live", healthcheck_url=None)
    watch.run_watch(ctx)
    assert hits == []


def test_ping_failure_never_breaks_the_watch(tmp_path):
    ctx = _live(tmp_path, "http://127.0.0.1:9/" + SECRET)       # nothing listens on port 9
    assert watch.run_watch(ctx).status == "ok"


def test_daily_backup_once_per_day(tmp_path):
    ctx = _ctx(tmp_path)
    watch.run_watch(ctx)
    watch.run_watch(ctx)
    assert len(backup.list_backups(ctx.state_dir)) == 1


def test_backup_failure_is_one_urgent_per_window_and_a_fail_code(tmp_path, monkeypatch):
    sent: list[tuple[str, str]] = []

    class Note:
        def send(self, title, text, priority="default"):
            sent.append((text, priority))

    ctx = _ctx(tmp_path)
    ctx.notifier = Note()
    monkeypatch.setattr(backup, "maybe_daily_backup", lambda *a, **k: ["backup_error:OSError"])
    out = watch.run_watch(ctx)
    assert out.fail_code == "backup_error"
    watch.run_watch(ctx)                          # the failing backup retries: no second URGENT
    hits = [t for t, p in sent if p == "urgent" and "backup_error:OSError" in t]
    assert len(hits) == 1


def test_no_ping_from_a_marked_rehearsal_sandbox(tmp_path, hc_server):
    from council.operator.release import REHEARSAL_MARKER

    url, hits = hc_server
    ctx = _live(tmp_path, url)
    (ctx.state_dir / REHEARSAL_MARKER).write_text("rehearsal")
    watch.run_watch(ctx)
    assert hits == []


# ------------------------------------------------------------------------------ runner flag
def test_runner_flag_from_launchd_label():
    assert runner_flags({"XPC_SERVICE_NAME": "com.fbzz.council.cycle"}) == [RUNNER_LAUNCHD]
    assert runner_flags({"XPC_SERVICE_NAME": "com.apple.Terminal"}) == []
    assert runner_flags({}) == []


def test_runner_flag_survives_a_skipped_broker_cycle(tmp_path, monkeypatch):
    from council.cycle import run_cycle

    monkeypatch.setenv("XPC_SERVICE_NAME", "com.fbzz.council.cycle")
    ctx = _ctx(tmp_path)
    ctx.settings = dataclasses.replace(ctx.settings, mode="live")
    (ctx.state_dir / "account").mkdir(parents=True, exist_ok=True)
    (ctx.state_dir / "account" / "onboarded.json").write_text("{}")
    out = run_cycle(ctx)
    assert out.status == "skipped_broker"
    assert RUNNER_LAUNCHD in out.flags and "keychain_unavailable" in out.flags


# ------------------------------------------------------------------------------ G24
def _fake_ssh(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    ssh = bin_dir / "ssh"
    ssh.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{log}"; done\nexit 1\n')
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)


def test_deploy_key_path_with_a_space_reaches_ssh_intact(tmp_path):
    root = tmp_path / "Application Support" / "council"
    clone = root / "publisher-clone"
    clone.mkdir(parents=True)
    (root / "deploy_key").write_text("not a key")
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "git@example.invalid:x/y.git"],
                   check=True)
    log = tmp_path / "argv.txt"
    _fake_ssh(tmp_path / "bin", log)
    pub = Publisher(clone, push=True, ssh_command=context._ssh_command(root))
    env = pub._env() if hasattr(pub, "_env") else dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    subprocess.run(["git", "-C", str(clone), "ls-remote", "origin"], env=env, capture_output=True,
                   check=False, timeout=30)
    argv = log.read_text().splitlines()
    assert str(root / "deploy_key") in argv
    assert argv[argv.index("-i") + 1] == str(root / "deploy_key")
    assert context.deploy_key_warning(clone) is None


def test_warns_when_deploy_key_exists_but_remote_is_https(tmp_path):
    root = tmp_path / "state"
    clone = root / "publisher-clone"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin",
                    f"https://x-access-token:{SECRET}@github.com/fbzz/council-book.git"], check=True)
    assert context.deploy_key_warning(clone) is None                  # no key: nothing to warn
    (root / "deploy_key").write_text("k")
    warning = context.deploy_key_warning(clone)
    assert warning == context.DEPLOY_KEY_HTTPS_WARNING and SECRET not in warning


# ------------------------------------------------------------------------------ T0 install key
def test_install_key_failure_is_flagged_and_never_crashes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from council.cycle import cycle_install_key
    from council.publish import install_key

    ctx = _ctx(tmp_path)
    rec = SimpleNamespace(flags=[])
    assert cycle_install_key(ctx, rec) == install_key.load_or_create(ctx.state_dir) and rec.flags == []

    def denied(_state_dir):
        raise PermissionError("no")

    monkeypatch.setattr(install_key, "load_or_create", denied)
    key = cycle_install_key(ctx, rec)
    assert rec.flags == ["install_key_error:PermissionError"] and len(key) == 32
