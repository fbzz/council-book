"""The rank's production services without the network: Alpaca price facts (batches of 12, paced, the
shared breaker honoured and tripped on a 429, only bars on or before the rank date) and the refusals
of `live_rank_services` when a credential is missing. Keys never appear in an error."""

from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
import pytest

from council import paths
from council.data import alpaca
from council.facts.market import RequestBudget
from council.stocks import commands
from tests.data.test_alpaca import KEYS, bar, days

NOW = pd.Timestamp("2026-08-20T21:05:00Z").to_pydatetime()
LATER = pd.Timestamp("2026-08-25T21:05:00Z").to_pydatetime()      # the bar of the 21st is available by then


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_price_facts_come_in_paced_batches_from_bars_up_to_the_rank_date():
    seen: list[list[str]] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["symbols"].split(",")
        seen.append(symbols)
        rows = {s: [bar(d, 10.0 + i, hour=0) for i, d in enumerate(days("2026-08-21", 300))] for s in symbols if s != "NODATA"}
        return httpx.Response(200, json={"bars": rows, "next_page_token": None})

    ids = [f"S{i:02d}" for i in range(25)] + ["BRK_B", "NODATA"]
    facts = commands.alpaca_price_facts(ids, date(2026, 8, 20), keys=KEYS, lookback_days=290,
                                        state_dir=paths.state_dir(), client=client(handler), sleep=waits.append,
                                        now=LATER)
    assert [len(s) for s in seen] == [12, 12, 3] and waits == [commands.PRICE_PACE_S] * 2
    assert "BRK.B" in {s for batch in seen for s in batch}                 # the class separator Alpaca spells
    assert facts["BRK_B"].last_bar == pd.Timestamp("2026-08-20")          # the bar of the 21st is never used
    assert facts["S00"].first_bar == pd.Timestamp(days("2026-08-21", 300)[0])
    assert facts["S00"].dollar_volume == pytest.approx(pd.Series([10.0 + i for i in range(236, 299)]).median() * 1000)
    assert facts["NODATA"].last_bar is None                              # no bars: the rank excludes it


def test_a_429_trips_the_shared_breaker_and_an_open_breaker_refuses():
    def limited(request):
        return httpx.Response(429, headers={"Retry-After": "30"})

    with pytest.raises(commands.StocksError, match="429") as err:
        commands.alpaca_price_facts(["AAA"], date(2026, 8, 20), keys=KEYS, lookback_days=290,
                                    state_dir=paths.state_dir(), client=client(limited), sleep=lambda _s: None,
                                    now=NOW)
    assert KEYS.secret not in str(err.value) and KEYS.key_id not in str(err.value)
    assert RequestBudget.for_provider(alpaca.SOURCE, paths.state_dir()).breaker_open()
    calls: list[int] = []
    with pytest.raises(commands.StocksError, match="breaker"):
        commands.alpaca_price_facts(["AAA"], date(2026, 8, 20), keys=KEYS, lookback_days=290,
                                    state_dir=paths.state_dir(), client=client(lambda r: calls.append(1)),
                                    sleep=lambda _s: None, now=NOW)
    assert calls == []


def test_too_many_symbols_are_refused_before_any_request():
    with pytest.raises(commands.StocksError, match="price requests"):
        commands.alpaca_price_facts([f"S{i}" for i in range(12 * commands.MAX_PRICE_REQUESTS + 1)], date(2026, 8, 20),
                                    keys=KEYS, lookback_days=290, state_dir=paths.state_dir(),
                                    client=client(lambda r: pytest.fail("no request expected")))


def test_live_services_refuse_without_credentials(monkeypatch):
    for name in ("COUNCIL_ALPACA_KEY_ID", "COUNCIL_ALPACA_SECRET", "COUNCIL_SEC_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(commands.StocksError, match="no Alpaca keys"):
        commands.live_rank_services(paths.state_dir(), None, eligibility=False)
    monkeypatch.setenv("COUNCIL_ALPACA_KEY_ID", KEYS.key_id)
    monkeypatch.setenv("COUNCIL_ALPACA_SECRET", KEYS.secret)
    with pytest.raises(commands.StocksError) as err:
        commands.live_rank_services(paths.state_dir(), None, eligibility=False)
    assert KEYS.secret not in str(err.value)
    monkeypatch.setenv("COUNCIL_SEC_USER_AGENT", "Council Book Tests tests@example.org")
    services = commands.live_rank_services(paths.state_dir(), None, eligibility=False, prefetch=False)
    assert services.broker is None and services.prefetch is None
