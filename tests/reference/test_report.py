"""The public summary is percent-only and labelled; unsafe text is refused."""

from __future__ import annotations

from datetime import date

import pytest

from council.reference import report
from council.reference.backtest import BacktestConfig, run_backtest
from council.reference.report import LABEL, UnsafePublicText, assert_public_safe, render_markdown
from council.reference.synthetic import synthetic_closes

TICKERS = {"NDX": "QQQ", "SEMIS": "SOXX", "SPX": "SPY", "GOLD": "GLD", "BTC": "BTCUSDT", "ETH": "ETHUSDT"}


@pytest.fixture(scope="module")
def small_run():
    from council.policy import Policy

    policy = Policy.load()
    raw = synthetic_closes(["QQQ", "SOXX", "SPY", "GLD", "IEF"], start=date(2016, 1, 4), end=date(2019, 12, 31),
                           crypto_tickers=["BTCUSDT", "ETHUSDT"], crypto_start=date(2017, 8, 17), seed=21)
    closes = {sym: raw[t] for sym, t in TICKERS.items()}
    controls = {t: raw[t] for t in ("SPY", "QQQ", "IEF")}
    return run_backtest(closes, controls, policy, BacktestConfig(start=date(2018, 6, 1), end=date(2019, 12, 31)))


@pytest.mark.parametrize("bad", ["costs $5", "USD 100 fee", "EUR 3", "/Users/someone/x.csv", "C:\\Users\\me",
                                 "mail a.b@example.com", "~/Library/Application Support/council-book"])
def test_unsafe_public_text_is_refused(bad):
    with pytest.raises(UnsafePublicText):
        assert_public_safe(bad)


@pytest.mark.parametrize("ok", ["CAGR 5.0%", "EURUSD and BTCUSDT lines", "5.0 bps etf_real", "2019-06-04 WARN"])
def test_percent_text_passes(ok):
    assert_public_safe(ok)


def test_summary_is_labelled_percent_only_and_complete(small_run):
    text = render_markdown(small_run, generated=date(2026, 9, 25))
    assert LABEL in text and "SYNTHETIC" not in text
    for heading in ("## Results", "## Trend states", "## Soft-kill drill", "## Method", "Trend overlay contribution"):
        assert heading in text
    for title in report.BOOK_TITLES.values():
        assert title in text
    assert small_run.policy_sha in text
    assert "WARN episodes:" in text and "HALT episodes:" in text


def test_summary_refuses_to_render_unsafe_content(small_run, monkeypatch):
    monkeypatch.setitem(report.BOOK_TITLES, "reference", "Reference ($ amounts)")
    with pytest.raises(UnsafePublicText):
        render_markdown(small_run, generated=date(2026, 9, 25))


def test_synthetic_summary_is_marked(small_run):
    assert "SYNTHETIC DATA" in render_markdown(small_run, generated=date(2026, 9, 25), synthetic=True)
