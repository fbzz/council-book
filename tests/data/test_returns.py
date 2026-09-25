from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from council.facts.returns import returns_matrix
from tests.data.synth import bars_from_closes, day_index


def test_alignment_on_common_dates_then_differencing():
    crypto_idx = day_index("2026-10-05", 15)                      # every day, ends Monday
    equity_idx = day_index("2026-10-05", 11, weekdays_only=True)
    crypto = bars_from_closes(np.arange(1, 16, dtype=float) * 10, crypto_idx)
    equity = bars_from_closes(np.linspace(100, 110, 11), equity_idx)
    m = returns_matrix({"BTC": crypto, "SPX": equity})
    assert list(m.columns) == ["BTC", "SPX"]
    assert all(ts.weekday() < 5 for ts in m.index)
    monday = pd.Timestamp("2026-10-05", tz="UTC")
    friday = pd.Timestamp("2026-10-02", tz="UTC")
    btc_close = crypto["close"]
    assert m.loc[monday, "BTC"] == pytest.approx(math.log(btc_close[monday] / btc_close[friday]))
    assert len(m) == len(set(crypto_idx) & set(equity_idx)) - 1


def test_window_and_edge_cases():
    idx = day_index("2026-10-01", 30)
    bars = bars_from_closes(100 * 1.01 ** np.arange(30), idx)
    m = returns_matrix({"A": bars, "B": bars}, window=5)
    assert len(m) == 5 and np.allclose(m.to_numpy(), math.log(1.01))
    assert returns_matrix({}).empty
    assert returns_matrix({"A": pd.DataFrame()}).empty
    with pytest.raises(ValueError):
        returns_matrix({"A": bars}, window=0)
