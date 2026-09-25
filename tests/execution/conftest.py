"""Fixtures for execution tests: a fake clock, FakeEtoro, real clients on its transport, a ledger."""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest

from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeClock, FakeEtoro
from council.execution.executor import Executor
from council.execution.ratelimit import TokenBucket
from council.ledger.db import Ledger
from tests.execution.helpers import API_KEY, INSTRUMENTS, NAV, READ_KEY, WRITE_KEY


@pytest.fixture
def fclock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(fclock: FakeClock) -> FakeEtoro:
    broker = FakeEtoro(clock=fclock.now, credit=NAV, write_user_keys={WRITE_KEY})
    for symbol, (iid, bid, ask) in INSTRUMENTS.items():
        broker.add_instrument(symbol, iid, bid=bid, ask=ask)
    return broker


@pytest.fixture
def read_client(fake: FakeEtoro, fclock: FakeClock) -> EtoroReadClient:
    return EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)


@pytest.fixture
def write_client(fake: FakeEtoro):
    module = importlib.import_module("council.broker.etoro_write")  # after the env fixture ran
    return module.EtoroWriteClient(API_KEY, WRITE_KEY, transport=fake.transport())


@pytest.fixture
def ledger(tmp_path, fclock: FakeClock) -> Ledger:
    return Ledger(tmp_path / "state" / "ledger.sqlite3", clock=fclock.now)


@pytest.fixture
def limiter(fclock: FakeClock) -> TokenBucket:
    return TokenBucket(clock=fclock.monotonic, sleep=fclock.sleep)


@pytest.fixture
def make_executor(write_client, read_client, ledger, limiter, fclock, policy):
    def _make(**kwargs) -> Executor:
        kwargs.setdefault("write", write_client)
        kwargs.setdefault("symbol_for", {iid: sym for sym, (iid, _b, _a) in INSTRUMENTS.items()})
        write = kwargs.pop("write")
        kwargs.setdefault("_skip_guard_for_tests", True)   # pytest is not an operator terminal
        return Executor(
            write, read_client, ledger, limiter, clock=fclock.now, sleep=fclock.sleep,
            policy=policy, **kwargs,
        )

    return _make


@pytest.fixture
def approve(ledger: Ledger, fclock: FakeClock):
    def _approve(decision_id: str = "d1", kind: str = "rebalance") -> str:
        ledger.create_decision(
            decision_id=decision_id, kind=kind, cycle_id="2026-10-01T1440Z",
            valid_until=fclock.now() + timedelta(hours=4),
        )
        ledger.transition(decision_id, "approved", "operator approved", actor="operator")
        return decision_id

    return _approve
