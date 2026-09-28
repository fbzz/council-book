"""Synthetic completed-bar panels and a fake Alpaca bars transport for the swing tests (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import numpy as np
import pandas as pd

from council.clock import NEW_YORK, session_hours
from council.data import alpaca
from council.data.bars import bars_from_rows


def sessions(end: date, n: int) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if session_hours("us", d) is not None:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def path(days: list[date], *, seed: int, sigma: float = 0.01, last_move: float | None = None,
         base: float = 100.0, volume: float = 1_000_000.0, last_volume_mult: float = 1.0,
         moves: Mapping[int, float] | None = None) -> list[tuple[date, float, float]]:
    """(day, close, volume) with iid returns of `sigma`; `last_move` overrides the last return,
    `moves` overrides returns by position."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, sigma, len(days))
    rets[0] = 0.0
    for pos, r in (moves or {}).items():
        rets[pos] = r
    if last_move is not None:
        rets[-1] = last_move
    closes = base * np.cumprod(1.0 + rets)
    vols = np.full(len(days), volume) * rng.uniform(0.8, 1.2, len(days))
    vols[-1] = volume * last_volume_mult
    return [(d, float(c), float(v)) for d, c, v in zip(days, closes, vols, strict=True)]


def frame(rows: list[tuple[date, float, float]]) -> pd.DataFrame:
    return bars_from_rows([(pd.Timestamp(d).tz_localize("UTC"), c, c * 1.01, c * 0.99, c, v)
                           for d, c, v in rows])


def alpaca_rows(rows: list[tuple[date, float, float]]) -> list[dict]:
    out = []
    for d, c, v in rows:
        stamp = datetime(d.year, d.month, d.day, tzinfo=NEW_YORK).isoformat()
        out.append({"t": stamp, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": v})
    return out


class FakeAlpaca:
    """A MockTransport over {alpaca symbol: rows}; records every request; `status` forces a code."""

    def __init__(self, data: Mapping[str, list[tuple[date, float, float]]], *, status: int = 200,
                 on_request: Callable[[httpx.Request], None] | None = None) -> None:
        self.data = dict(data)
        self.status = status
        self.requests: list[httpx.Request] = []
        self.on_request = on_request

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request:
            self.on_request(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "x"})
        q = parse_qs(request.url.query.decode())
        syms = q["symbols"][0].split(",")
        body = {"bars": {s: alpaca_rows(self.data[s]) for s in syms if s in self.data}, "next_page_token": None}
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def keys() -> alpaca.AlpacaKeys:
    part = "fake" + "token"
    return alpaca.AlpacaKeys(key_id=part + "-id-0001", secret=part + "-sec-0002")
