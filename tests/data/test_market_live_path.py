"""WP-G acceptance, the history half (design §11.1): stock lines come from Alpaca only (never Tiingo),
one fetch per symbol per trading day across the six slots, the request budget, the 429 breaker (per
provider, for the hour), the per-gather time budget with 40 stock lines, and the fallback to a still
fresh copy. Synthetic payloads, mock transports, fake clocks; no network, no Keychain."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest
import yaml

from council.data import alpaca
from council.facts.market import (
    BudgetLimits,
    RequestBudget,
    expected_day,
    gather_history,
    history_source,
    history_sources,
)
from council.policy import Policy
from tests.conftest import SLEEVE_FIXTURE, make_sleeve_policy_dir
from tests.data.synth import binance_payload, daily_bars, tiingo_payload, utc

KEYS = alpaca.AlpacaKeys(key_id="PKTESTKEYID0001", secret="sEcReTvAlUe-never-logged-1")
NOW = utc(2026, 10, 1, 14, 40)                        # Thursday, 10:40 New York
TIINGO, BINANCE, ALPACA = "api.tiingo.com", "data-api.binance.vision", "data.alpaca.markets"


def sleeve_policy_with(tmp_path_factory, n_selected: int, n_shortlist: int) -> Policy:
    """The re-based fixture universe with a synthetic sleeve of n_selected + n_shortlist names."""
    overlay = tmp_path_factory.mktemp(f"overlay{n_selected}")
    for name in ("universe.yaml", "stock-rank.yaml"):
        shutil.copyfile(SLEEVE_FIXTURE / name, overlay / name)
    sleeve = yaml.safe_load((SLEEVE_FIXTURE / "stock-sleeve.yaml").read_text())
    sleeve["names_target"] = n_selected
    sleeve["lines"] = [
        {"symbol": f"S{i:03d}", "name": f"Synthetic {i}", "role": "selected" if i < n_selected else "shortlist",
         "sector": "BusEq", "cik": f"{900200 + i:010d}", "rank": i + 1, "signal_ticker": f"S{i:03d}",
         "etoro_symbol": f"S{i:03d}", "eligibility_checked_at": "2026-11-20T15:02:00Z", "credited": None,
         "aliases": []}
        for i in range(n_selected + n_shortlist)
    ]
    (overlay / "stock-sleeve.yaml").write_text(yaml.safe_dump(sleeve, sort_keys=False))
    return Policy.load(make_sleeve_policy_dir(tmp_path_factory.mktemp(f"pol{n_selected}"), overlay=overlay))


@pytest.fixture(scope="module")
def forty(tmp_path_factory) -> Policy:
    return sleeve_policy_with(tmp_path_factory, 32, 8)


def ny_midnight(ts) -> str:
    """Alpaca's daily stamp: the trading date's New York midnight, in UTC."""
    local = pd.Timestamp(pd.Timestamp(ts).date()).tz_localize("America/New_York")
    return local.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def alpaca_bars(symbols, end="2026-10-01", n=300):
    out = {}
    for k, sym in enumerate(symbols):
        frame = daily_bars(end, n, seed=100 + k, weekdays_only=True)
        out[sym] = [{"t": ny_midnight(ts), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
                    for ts, r in frame.iterrows()]
    return out


class Router:
    """Mock transport for Tiingo, Binance and Alpaca. `status[host]` is a list of statuses returned
    (in order) before the normal answer; `cost_s` advances `clock` per request; `end` is the last
    trading day each provider has published."""

    def __init__(self, *, clock=None, cost_s: float = 0.0, end: str = "2026-10-01") -> None:
        self.calls: list[httpx.Request] = []
        self.status: dict[str, list[int]] = {}
        self.clock, self.cost_s, self.end = clock, cost_s, end

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.clock is not None:
            self.clock.t += self.cost_s
        host = request.url.host
        queue = self.status.get(host)
        if queue:
            return httpx.Response(queue.pop(0))
        if host == TIINGO:
            return httpx.Response(200, json=tiingo_payload(daily_bars(self.end, 300, seed=3, weekdays_only=True)))
        if host == BINANCE:
            return httpx.Response(200, json=binance_payload(daily_bars(self.end, 300, seed=4, weekdays_only=False)))
        if host == ALPACA:
            symbols = request.url.params["symbols"].split(",")
            return httpx.Response(200, json={"bars": alpaca_bars(symbols, self.end), "next_page_token": None})
        return httpx.Response(404)

    def count(self) -> Counter:
        return Counter(r.url.host for r in self.calls)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def gather(policy, router, mock_client, now=NOW, **kw):
    kw.setdefault("tiingo_token", "tok")
    kw.setdefault("alpaca_keys", KEYS)
    return gather_history(policy, now=now, client=mock_client(router), **kw)


# ------------------------------------------------------------------------------------ routing


def test_stock_lines_come_from_alpaca_and_never_from_tiingo(sleeve_policy, mock_client):
    router = Router()
    history, flags = gather(sleeve_policy, router, mock_client)
    stocks = [ln.symbol for ln in sleeve_policy.universe.stock_lines()]
    assert flags == [] and set(history) == set(sleeve_policy.universe.symbols())
    tiingo_paths = {r.url.path for r in router.calls if r.url.host == TIINGO}
    assert all("tst" not in p and "/f/" not in p for p in tiingo_paths)       # core tickers only
    assert len(tiingo_paths) == 7 and router.count()[ALPACA] == 1             # six stocks, one batch
    requested = router.calls[-1].url.params["symbols"].split(",")
    assert sorted(requested) == ["F", "TSTA", "TSTB", "TSTC.B", "TSTD", "TSTE"]
    assert history["TSTC_B"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")
    assert {history_source(ln) for ln in sleeve_policy.universe.stock_lines()} == {"alpaca"}
    assert history_sources(sleeve_policy) == dict.fromkeys(stocks, "alpaca")
    assert history_sources(Policy.load(include_sleeve=False)) == {}


def test_without_alpaca_keys_stock_lines_have_no_history_and_the_core_is_unaffected(sleeve_policy, mock_client):
    router = Router()
    history, flags = gather(sleeve_policy, router, mock_client, alpaca_keys=None)
    stocks = [ln.symbol for ln in sleeve_policy.universe.stock_lines()]
    assert flags == [f"history_missing:{s}:no_alpaca_keys" for s in stocks]
    assert not set(stocks) & set(history) and len(history) == 9
    assert router.count()[ALPACA] == 0 and router.count()[TIINGO] == 7


def test_the_state_carries_the_alpaca_label_and_availability(sleeve_policy, mock_client):
    from council.facts.features import market_states

    history, _ = gather(sleeve_policy, Router(), mock_client)
    states = market_states(sleeve_policy, history, now=NOW, sources=history_sources(sleeve_policy))
    assert states["TSTA"].history_source == "alpaca" and states["NDX"].history_source == "tiingo:QQQ"
    assert states["TSTA"].bar_available_at == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)   # Sep 30 20:00 NY


# ------------------------------------------------------------------------------------ day key


def test_expected_day_follows_each_source_rule():
    assert expected_day("tiingo", utc(2026, 10, 1, 14, 40)) == pd.Timestamp("2026-09-30").date()
    assert expected_day("alpaca", utc(2026, 10, 2, 2, 40)) == pd.Timestamp("2026-10-01").date()   # 22:40 NY
    assert expected_day("alpaca", utc(2026, 10, 3, 14, 40)) == pd.Timestamp("2026-10-02").date()  # Saturday
    assert expected_day("tiingo", utc(2026, 11, 27, 14, 40)) == pd.Timestamp("2026-11-25").date()  # Thanksgiving
    assert expected_day("binance", utc(2026, 10, 1, 2, 40)) == pd.Timestamp("2026-09-30").date()


def test_one_fetch_per_symbol_per_day_across_the_six_slots(sleeve_policy, mock_client):
    router = Router()
    client = mock_client(router)
    slots = [utc(2026, 10, 2, h, 40) for h in (2, 6, 10, 14, 18, 22)]   # one New York trading day's key
    for slot in slots:
        history, flags = gather_history(sleeve_policy, now=slot, tiingo_token="tok", alpaca_keys=KEYS,
                                        client=client)
        assert flags == [] and len(history) == 15
    assert router.count() == Counter({TIINGO: 7, BINANCE: 2, ALPACA: 1})
    gather_history(sleeve_policy, now=utc(2026, 10, 3, 2, 40), tiingo_token="tok", alpaca_keys=KEYS,
                   client=client)                                        # Oct 2's bars are due now
    assert router.count() == Counter({TIINGO: 14, BINANCE: 4, ALPACA: 2})


def test_a_late_publication_is_used_but_asked_again_next_slot(sleeve_policy, mock_client):
    router = Router(end="2026-09-30")                                   # Oct 1 not published yet
    client = mock_client(router)
    history, flags = gather_history(sleeve_policy, now=utc(2026, 10, 2, 2, 40), tiingo_token="tok",
                                    alpaca_keys=KEYS, client=client)
    assert flags == [] and history["TSTA"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")
    gather_history(sleeve_policy, now=utc(2026, 10, 2, 6, 40), tiingo_token="tok", alpaca_keys=KEYS,
                   client=client)
    assert router.count()[ALPACA] == 2 and router.count()[TIINGO] == 14
    router.end = "2026-10-01"                                           # published: cached from now on
    for hour in (10, 14):
        gather_history(sleeve_policy, now=utc(2026, 10, 2, hour, 40), tiingo_token="tok", alpaca_keys=KEYS,
                       client=client)
    assert router.count()[ALPACA] == 3 and router.count()[TIINGO] == 21


# ------------------------------------------------------------------------------------ breaker


def test_a_tiingo_429_on_the_first_symbol_stops_tiingo_for_the_hour(sleeve_policy, mock_client, _no_sleep):
    router = Router()
    router.status[TIINGO] = [429]
    wall = {"now": datetime(2026, 10, 1, 14, 41, tzinfo=UTC)}

    def budgets(state_dir):
        return {p: RequestBudget.for_provider(p, state_dir, now_fn=lambda: wall["now"])
                for p in ("tiingo", "alpaca")}

    from council.paths import state_dir

    history, flags = gather(sleeve_policy, router, mock_client, budgets=budgets(state_dir()))
    assert router.count()[TIINGO] == 1 and _no_sleep == []              # never retried, no sleep
    core_tiingo = ["NDX", "SEMIS", "SPX", "GOLD", "OIL", "EURUSD", "GBPUSD"]
    assert flags == ["history_rate_limited:NDX"] + [f"history_breaker:{s}" for s in core_tiingo[1:]]
    assert set(history) == {"BTC", "ETH"} | {ln.symbol for ln in sleeve_policy.universe.stock_lines()}
    assert router.count()[ALPACA] == 1                                  # Alpaca has its own breaker
    saved = json.loads((state_dir() / "tiingo_budget.json").read_text())
    assert saved["breaker_until"] == "2026-10-01T15:41:00+00:00"

    wall["now"] = datetime(2026, 10, 1, 15, 20, tzinfo=UTC)              # the next run, same hour
    _, flags = gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 1, 18, 40),
                      budgets=budgets(state_dir()))
    assert router.count()[TIINGO] == 1 and "history_breaker:NDX" in flags
    wall["now"] = datetime(2026, 10, 1, 15, 45, tzinfo=UTC)              # an hour later: calls resume
    _, flags = gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 1, 18, 40),
                      budgets=budgets(state_dir()))
    assert router.count()[TIINGO] == 8 and flags == []


def test_an_alpaca_429_with_40_stock_lines_stops_after_one_request_inside_the_budget(forty, mock_client):
    clock = Clock()
    router = Router(clock=clock, cost_s=20.0)
    router.status[ALPACA] = [429]
    history, flags = gather(forty, router, mock_client, monotonic=clock)
    stocks = [ln.symbol for ln in forty.universe.stock_lines()]
    assert len(stocks) == 40
    assert router.count()[ALPACA] == 1                                  # the first batch only
    first = stocks[:alpaca.SYMBOLS_PER_REQUEST]
    assert [f for f in flags if f.startswith("history_rate_limited")] == [f"history_rate_limited:{s}" for s in first]
    assert [f for f in flags if f.startswith("history_breaker")] == [f"history_breaker:{s}" for s in stocks[12:]]
    assert not set(stocks) & set(history) and len(history) == 9          # the core is complete
    assert clock.t <= 300.0                                              # 10 requests x 20 s


def test_the_time_budget_stops_requests_to_a_slow_source(forty, mock_client):
    clock = Clock()
    router = Router(clock=clock, cost_s=70.0)                           # every request takes 70 s
    history, flags = gather(forty, router, mock_client, monotonic=clock)
    assert len(router.calls) == 5 and clock.t == 350.0                  # the 6th would start past 300 s
    skipped = [f.split(":")[1] for f in flags if f.startswith("history_time_budget")]
    assert skipped[:4] == ["ETH", "OIL", "EURUSD", "GBPUSD"] and len(skipped) == 4 + 40
    assert set(history) == {"NDX", "SEMIS", "SPX", "GOLD", "BTC"}      # sent before the budget ran out


# ------------------------------------------------------------------------------------ budget


def test_a_budget_refusal_skips_the_request_and_is_recorded_before_sending(sleeve_policy, mock_client):
    from council.paths import state_dir

    router = Router()
    tight = {"tiingo": RequestBudget("tiingo", state_dir() / "tiingo_budget.json", BudgetLimits(3, 400, 60)),
             "alpaca": RequestBudget("alpaca", state_dir() / "alpaca_budget.json", BudgetLimits(600, 5000, 5))}
    history, flags = gather(sleeve_policy, router, mock_client, budgets=tight)
    assert router.count()[TIINGO] == 3 and router.count()[ALPACA] == 0
    stocks = [ln.symbol for ln in sleeve_policy.universe.stock_lines()]
    assert [f for f in flags if f.startswith("history_budget")] == (
        [f"history_budget:{s}" for s in ("GOLD", "OIL", "EURUSD", "GBPUSD")]
        + [f"history_budget:{s}" for s in stocks])                      # 6 symbols > 5 a month
    saved = json.loads((state_dir() / "tiingo_budget.json").read_text())
    assert list(saved["hours"].values()) == [3] and sorted(saved["months"].popitem()[1]) == ["QQQ", "SOXX", "SPY"]
    assert KEYS.secret not in (state_dir() / "tiingo_budget.json").read_text()
    assert not set(stocks) & set(history)                               # no_data: the satellite is held


def test_request_budget_limits_and_buckets(tmp_path):
    wall = {"now": datetime(2026, 10, 1, 14, 59, tzinfo=UTC)}
    b = RequestBudget("tiingo", tmp_path / "b.json", BudgetLimits(2, 3, 2), now_fn=lambda: wall["now"])
    assert b.reserve(["A"]) and b.reserve(["A"]) and not b.reserve(["A"])      # 2 an hour
    wall["now"] += timedelta(minutes=2)                                        # the next hour
    assert not b.reserve(["B", "C"])                                           # 3 symbols > 2 a month
    assert b.reserve(["B"]) and not b.reserve(["A"])                           # 3 a day
    reloaded = RequestBudget("tiingo", tmp_path / "b.json", BudgetLimits(2, 3, 2), now_fn=lambda: wall["now"])
    assert not reloaded.reserve(["A"]) and not reloaded.breaker_open()
    (tmp_path / "b.json").write_text("{not json")
    assert RequestBudget("tiingo", tmp_path / "b.json", BudgetLimits(2, 3, 2), now_fn=lambda: wall["now"]).reserve(["A"])


# ------------------------------------------------------------------------------------ fallback


def test_a_skipped_line_uses_its_last_copy_only_while_fresh(sleeve_policy, mock_client):
    router = Router()
    gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 1, 14, 40))       # a good copy of Sep 30
    router.status[TIINGO] = [429]
    router.status[ALPACA] = [429]
    history, flags = gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 2, 2, 40))
    assert "history_rate_limited:NDX" in flags and "history_cached:NDX" in flags
    assert history["NDX"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")      # still fresh (< 30 h)
    assert "history_cached:TSTA" in flags and "TSTA" in history
    # two trading days later the copy is stale: the line is left out (no_data), not served stale
    history, flags = gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 3, 2, 40))
    assert "history_breaker:NDX" in flags and "history_cached:NDX" not in flags and "NDX" not in history


def test_an_empty_answer_keeps_a_still_fresh_copy(sleeve_policy, mock_client):
    router = Router()
    gather(sleeve_policy, router, mock_client, now=utc(2026, 10, 1, 14, 40))       # a good copy of Sep 30

    def empty_tiingo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[]) if request.url.host == TIINGO else router(request)

    history, flags = gather(sleeve_policy, empty_tiingo, mock_client, now=utc(2026, 10, 2, 2, 40))
    assert "history_empty:NDX" in flags and "history_cached:NDX" in flags
    assert history["NDX"].index[-1] == pd.Timestamp("2026-09-30", tz="UTC")      # not discarded as empty


def test_an_unexpected_stock_source_error_holds_only_the_satellite(sleeve_policy, mock_client, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(alpaca, "fetch_daily", broken)
    history, flags = gather(sleeve_policy, Router(), mock_client)
    stocks = [ln.symbol for ln in sleeve_policy.universe.stock_lines()]
    assert flags == [f"history_failed:{s}" for s in stocks]
    assert not set(stocks) & set(history) and len(history) == 9                  # the core is complete
