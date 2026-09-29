"""SW-1: the after-close movers screen (design §1.2): completed SIP bars only, four lists, its own
pacing / budget / breaker (a 429 never trips the stock-line client's breaker), no live-rate route."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from urllib.parse import parse_qs

import pytest

from council.facts.market import RequestBudget
from council.swing import screen as sc
from tests.swing import panel

D = date(2026, 9, 25)                                   # Friday
NOW = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)           # D 21:00 New York
DAYS = panel.sessions(D, 50)


def _data():
    data = {e: panel.path(DAYS, seed=100 + i, sigma=0.01) for i, e in enumerate(sorted(set(sc.SECTOR_ETF.values())))}
    data["XLK"] = panel.path(DAYS, seed=7, sigma=0.01, last_move=0.04)           # BusEq moves ~4 sigma
    data["BIG"] = panel.path(DAYS, seed=1, sigma=0.01, last_move=0.09)
    data["VOLX"] = panel.path(DAYS, seed=2, sigma=0.01, last_move=0.001, last_volume_mult=6.0)
    data["CATX"] = panel.path(DAYS, seed=3, sigma=0.02, last_move=0.002, volume=5_000_000)
    data["LAG"] = panel.path(DAYS, seed=4, sigma=0.01, last_move=0.0005)
    data["QUIET"] = panel.path(DAYS, seed=5, sigma=0.01, last_move=0.001)
    data["BRK.B"] = panel.path(DAYS, seed=6, sigma=0.01, last_move=-0.05)
    return data


UNIVERSE = sc.build_universe(["BIG", "VOLX", "CATX", "LAG", "QUIET", "BRK.B", "bad ticker!!"],
                             {"BIG": "BusEq", "LAG": "BusEq", "VOLX": "Hlth", "CATX": "Shops", "QUIET": "Hlth",
                              "BRK_B": "Money"})


def _run(tmp_path, fake, **kw):
    sleeps: list[float] = []
    clock = iter(range(0, 10_000))
    out = sc.run_screen(UNIVERSE, now=kw.pop("now", NOW), keys=panel.keys(), state_dir=tmp_path,
                        client=fake.client(), sleep=sleeps.append, monotonic=lambda: float(next(clock)) * 0.1,
                        budget_now=lambda: NOW, **kw)
    return out, sleeps


def test_universe_normalises_and_skips_bad():
    assert [n.line_id for n in UNIVERSE] == ["BIG", "VOLX", "CATX", "LAG", "QUIET", "BRK_B"]


def test_four_lists_and_fact_ids(tmp_path):
    fake = panel.FakeAlpaca(_data())
    filings = [("CATX", datetime(2026, 9, 25, 15, 0, tzinfo=UTC)), ("QUIET", datetime(2026, 9, 24, 12, 0, tzinfo=UTC))]
    out, sleeps = _run(tmp_path, fake, filings=filings)
    ids = lambda k: [r["line_id"] for r in out.lists[k]]            # noqa: E731
    assert out.ready and out.session == "2026-09-25" and out.names_scored == 6
    assert ids("movers") == ["BIG", "BRK_B"]
    assert ids("volume") == ["VOLX"]
    assert ids("unmoved") == ["CATX"]                                 # QUIET's filing is before D-1's close
    assert ids("laggard") == ["LAG"] and out.lists["laggard"][0]["sector_etf"] == "XLK"
    assert "M:BRK_B:movers" in out.fact_ids()
    assert out.lists["movers"][0]["move_pct"] == pytest.approx(9.0, abs=1e-6)
    assert "dollar_volume" not in str(out.to_json())
    assert len(fake.requests) == 1 and not sleeps


def test_sip_only_bars_host_only_and_no_live_rates(tmp_path):
    fake = panel.FakeAlpaca(_data())
    _run(tmp_path, fake)
    for req in fake.requests:
        q = parse_qs(req.url.query.decode())
        assert req.url.host == "data.alpaca.markets" and req.url.path == "/v2/stocks/bars"
        assert q["feed"] == ["sip"] and q["timeframe"] == ["1Day"]
    src = inspect.getsource(sc)
    assert "rates" not in src and "etoro" not in src.lower()


def test_pacing_between_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "SYMBOLS_PER_REQUEST", 5)
    fake = panel.FakeAlpaca(_data())
    out, sleeps = _run(tmp_path, fake)
    assert len(fake.requests) == out.requests == 4
    assert len(sleeps) == 3 and all(0 < s <= sc.PACE_S for s in sleeps)


def test_429_trips_only_the_screen_breaker(tmp_path):
    fake = panel.FakeAlpaca(_data(), status=429)
    out, _ = _run(tmp_path, fake)
    assert "screen_rate_limited" in out.flags and len(fake.requests) == 1
    assert RequestBudget(sc.PROVIDER, tmp_path / f"{sc.PROVIDER}_budget.json", sc.LIMITS,
                         now_fn=lambda: NOW).breaker_open()
    stock_line = RequestBudget.for_provider("alpaca", tmp_path, now_fn=lambda: NOW)
    assert not stock_line.breaker_open() and not (tmp_path / "alpaca_budget.json").exists()
    again, _ = _run(tmp_path, panel.FakeAlpaca(_data()))
    assert again.flags == ["screen_breaker_open"] and not again.ready


def test_partial_day_bar_never_used(tmp_path):
    # At D+1 (Monday 28 Sep) 18:00 New York, the broker returns a bar for the 28th; it is not
    # completed, so the screen stays on D (Friday) with identical lists.
    data = _data()
    base, _ = _run(tmp_path / "a", panel.FakeAlpaca(data))
    monday = date(2026, 9, 28)
    extended = {s: rows + [(monday, rows[-1][1] * 1.2, rows[-1][2] * 0.1)] for s, rows in data.items()}
    later = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
    out, _ = _run(tmp_path / "b", panel.FakeAlpaca(extended), now=later)
    assert out.session == "2026-09-25" and out.content_hash() == base.content_hash()


def test_no_keys_and_save_load(tmp_path):
    out = sc.run_screen(UNIVERSE, now=NOW, keys=None, state_dir=tmp_path)
    assert out.flags == ["screen_no_alpaca_keys"] and not out.ready
    built, _ = _run(tmp_path, panel.FakeAlpaca(_data()))
    sc.save(built, tmp_path)
    assert sc.load(tmp_path, built.session).content_hash() == built.content_hash()


def test_screen_session_timing():
    assert sc.screen_session(datetime(2026, 9, 25, 23, 0, tzinfo=UTC)) == date(2026, 9, 24)   # D 19:00 NY
    assert sc.screen_session(datetime(2026, 9, 26, 0, 30, tzinfo=UTC)) == date(2026, 9, 25)   # D 20:30 NY
    assert sc.screen_session(datetime(2026, 9, 27, 12, 0, tzinfo=UTC)) == date(2026, 9, 25)   # Sunday


def test_a_later_sessions_bar_never_shifts_the_screen_day():
    import numpy as np
    import pandas as pd

    from council.data.bars import bars_from_rows

    days = panel.sessions(date(2026, 9, 25), 30) + [date(2026, 9, 28)]
    closes = 100.0 * np.cumprod([1.0 + (0.01 if i % 2 else -0.01) for i in range(len(days))])
    rows = [(pd.Timestamp(d).tz_localize("UTC"), c, c, c, c, 1e6) for d, c in zip(days, closes, strict=True)]
    bars = bars_from_rows(rows)
    with_next = sc.day_metric("TSTA", bars, date(2026, 9, 25))
    alone = sc.day_metric("TSTA", bars.iloc[:-1], date(2026, 9, 25))
    assert with_next is not None and with_next == alone


def test_screen_fits_a_month_of_two_slots(tmp_path):
    """SW-7b: a ~600-name universe (+ the sector and market-context ETFs), 23 sessions x 2 swing slots, the screen
    rebuilt at BOTH slots (worse than the cached once-per-session): never out of budget. The request
    counts once per multi-symbol call and the month counts distinct symbols."""
    from datetime import timedelta

    names = [f"N{i:03d}" for i in range(600)]
    universe = sc.build_universe(names, {})
    fake = panel.FakeAlpaca({})
    month = [d for d in panel.sessions(date(2026, 10, 30), 23)]
    assert len(month) == 23
    etfs = len(set(sc.SECTOR_ETF.values()) | set(sc.CONTEXT_ETFS))       # sector + market-context ETFs
    per_screen = -(-(len(names) + etfs) // sc.SYMBOLS_PER_REQUEST)
    total = 0
    for day in month:
        for hour in (1, 2):                          # after D's close, two builds (21:00 / 22:00 NY)
            now = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC) + timedelta(days=1)
            clock = iter(range(0, 100_000))
            out = sc.run_screen(universe, now=now, keys=panel.keys(), state_dir=tmp_path, client=fake.client(),
                                sleep=lambda _s: None, monotonic=lambda c=clock: float(next(c)) * 0.1,
                                budget_now=lambda n=now: n)
            assert out.session == day.isoformat(), (out.session, day)
            assert not {"screen_budget", "screen_request_cap", "screen_time_budget"} & set(out.flags), out.flags
            assert out.requests == per_screen
            total += out.requests
    assert total == 23 * 2 * per_screen <= 23 * sc.LIMITS.requests_per_day
    budget = RequestBudget(sc.PROVIDER, tmp_path / f"{sc.PROVIDER}_budget.json", sc.LIMITS)
    for seen in budget.state["months"].values():      # a calendar month's distinct symbols
        assert len(seen) == 600 + etfs <= sc.LIMITS.symbols_per_month
