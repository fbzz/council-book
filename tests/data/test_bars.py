from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from council.data import bars as B
from tests.data.synth import bars_from_closes, day_index, utc


def _bars(end="2026-10-01", n=5):
    return bars_from_closes([100.0 + i for i in range(n)], day_index(end, n))


def test_completed_only_boundary_pass_and_fail():
    df = _bars()                                              # starts Sep 27 .. Oct 1
    exactly = B.completed_only(df, "1d", utc(2026, 10, 1, 0, 0))
    assert exactly.index[-1] == pd.Timestamp("2026-09-30", tz="UTC")   # start+1d == now: complete
    before = B.completed_only(df, "1d", utc(2026, 9, 30, 23, 59))
    assert before.index[-1] == pd.Timestamp("2026-09-29", tz="UTC")    # Sep 30 still in progress


def test_completed_only_four_hour_and_etoro_alias():
    idx = pd.date_range("2026-10-01 00:00", periods=6, freq="4h", tz="UTC")
    df = bars_from_closes(range(1, 7), idx)
    assert len(B.completed_only(df, "4h", utc(2026, 10, 1, 14, 40))) == 3      # 00, 04, 08
    assert len(B.completed_only(df, "FourHours", utc(2026, 10, 1, 16, 0))) == 4
    with pytest.raises(ValueError):
        B.completed_only(df, "1w", utc(2026, 10, 1))


def test_tiingo_availability_is_20h_new_york_in_both_offsets():
    summer = B.tiingo_available_at(pd.Timestamp("2026-09-30", tz="UTC"))
    winter = B.tiingo_available_at(pd.Timestamp("2026-12-01", tz="UTC"))
    assert summer == pd.Timestamp("2026-10-01 00:00", tz="UTC")          # EDT = UTC-4
    assert winter == pd.Timestamp("2026-12-02 01:00", tz="UTC")          # EST = UTC-5
    df = _bars("2026-10-01", 3)
    at_1440 = B.available_only(df, source="tiingo", interval="1d", now=utc(2026, 10, 1, 14, 40))
    assert at_1440.index[-1] == pd.Timestamp("2026-09-30", tz="UTC")
    just_before = B.available_only(df, source="tiingo", interval="1d", now=utc(2026, 9, 30, 23, 59))
    assert just_before.index[-1] == pd.Timestamp("2026-09-29", tz="UTC")


def test_available_times_by_source():
    idx = day_index("2026-10-01", 2)
    assert list(B.available_times(idx, source="binance", interval="1d")) == list(idx + pd.Timedelta(days=1))
    assert list(B.available_times(idx, source="etoro", interval="4h")) == list(idx + pd.Timedelta(hours=4))
    with pytest.raises(ValueError):
        B.available_times(idx, source="tiingo", interval="4h")
    with pytest.raises(ValueError):
        B.available_times(idx, source="yahoo", interval="1d")
    assert B.last_available_at(B.empty_bars(), source="binance", interval="1d") is None


def test_normalize_sorts_dedupes_and_drops_bad_closes():
    idx = pd.DatetimeIndex(
        ["2026-10-02", "2026-10-01", "2026-10-02", "2026-10-03"], tz="UTC"
    )
    df = pd.DataFrame(
        {"open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4], "close": [10, 20, 30, -1]},
        index=idx,
    )
    out = B.normalize_bars(df)
    assert list(out.columns) == list(B.COLUMNS)
    assert list(out.index) == [pd.Timestamp("2026-10-01", tz="UTC"), pd.Timestamp("2026-10-02", tz="UTC")]
    assert out.loc[pd.Timestamp("2026-10-02", tz="UTC"), "close"] == 30.0     # last duplicate wins
    assert (out["volume"] == 0.0).all() and out.dtypes.eq("float64").all()


def test_naive_times_are_refused():
    with pytest.raises(ValueError):
        B.to_utc(datetime(2026, 10, 1))
    naive = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2026-10-01"]))
    with pytest.raises(ValueError):
        B.normalize_bars(naive)


def test_json_round_trip_is_exact():
    df = bars_from_closes([100.0 / 3, 2.0**0.5, 1e-9 + 7], day_index("2026-10-01", 3))
    back = B.bars_from_json(B.bars_to_json(df))
    pd.testing.assert_frame_equal(back, B.normalize_bars(df), check_exact=True, check_freq=False)
    assert B.bars_from_json({"start": []}).empty
