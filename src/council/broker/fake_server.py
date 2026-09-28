"""Loopback HTTP front for `FakeEtoro` (the onboarding rehearsal, m5-readiness §7.1).

A stdlib `http.server` bound to 127.0.0.1 only. Every request is turned into an `httpx.Request`
and handed to `FakeEtoro._handle`, so the real read/write clients talk to the fake through the
pinned loopback base URL (`broker/http.py::check_base_url`) exactly as they would to eToro.

Rules:
- Binds 127.0.0.1 only; any other host is refused before a socket is created.
- Writes its port to `<sandbox>/fake-broker.port` (0600) and only inside a marked rehearsal
  sandbox, never the real state dir; `stop()` removes the file.
- Never logs a request line or a header (the key pair travels in headers): `log_message` is
  silenced and errors carry only the status code.
- Dev role only: nothing here reads a Keychain item or knows a real token.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from council.broker.fake import FakeEtoro
from council.broker.http import FAKE_BROKER_PORT_FILE, LOOPBACK_HOST


class FakeServerError(RuntimeError):
    """Refusal to start the loopback fake broker."""


def port_file(state_dir: Path) -> Path:
    return state_dir.joinpath(*FAKE_BROKER_PORT_FILE)


def _handler_for(fake: FakeEtoro, lock: threading.Lock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "council-fake-broker"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return                                               # never log request lines/headers

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            url = f"http://{LOOPBACK_HOST}:{self.server.server_address[1]}{self.path}"
            headers = [(k, v) for k, v in self.headers.items() if k.lower() not in ("host", "content-length")]
            request = httpx.Request(self.command, url, headers=headers, content=body)
            try:
                with lock:
                    response = fake._handle(request)
                    payload = response.read()
                status, extra = response.status_code, dict(response.headers)
            except httpx.TransportError:
                self.close_connection = True                     # an injected transport failure
                return
            self.send_response(status)
            for key, value in extra.items():
                if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve  # noqa: N815

    return Handler


class FakeBrokerServer:
    """`start()` binds 127.0.0.1:<port> (0 = any free port), writes the port file and serves in a
    daemon thread; `stop()` shuts down and removes the port file."""

    def __init__(self, fake: FakeEtoro, state_dir: Path, *, host: str = LOOPBACK_HOST, port: int = 0) -> None:
        from council.operator.release import is_marked_sandbox

        if host != LOOPBACK_HOST:
            raise FakeServerError("the fake broker binds 127.0.0.1 only")
        if not is_marked_sandbox(state_dir):
            raise FakeServerError("the fake broker runs only inside a marked rehearsal sandbox")
        self.fake = fake
        self.state_dir = state_dir
        self._host, self._port = host, port
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise FakeServerError("not started")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"

    def start(self) -> FakeBrokerServer:
        self._server = ThreadingHTTPServer((self._host, self._port), _handler_for(self.fake, self._lock))
        self._server.daemon_threads = True
        if self._server.server_address[0] != LOOPBACK_HOST:        # defence in depth
            self._server.server_close()
            raise FakeServerError("the fake broker bound a non-loopback address")
        path = port_file(self.state_dir)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{self.port}\n")
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-broker", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        port_file(self.state_dir).unlink(missing_ok=True)

    def __enter__(self) -> FakeBrokerServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
