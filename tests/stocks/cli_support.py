"""Helpers for the `council stocks` tests (WP-D). No network, no Keychain, no LLM, no broker writes.

- `make_repo`: a throwaway git checkout whose COMMITTED `policy/` holds the repository's top-level
  policy files, the adoption record (`variants/`), optionally the re-based fixture universe, a
  `stock-rank.yaml` with the adopted `rule:` block, the AI-adjacent list and a sleeve file.
- `broker`: the FakeEtoro behind the real READ client (`EtoroReadClient`); stock rows are real,
  long, 1x, with a stop-loss range, units allowed, fractional shares.
- `Book` / `quarters` (tests/stocks/test_rank.py) build synthetic rank inputs; `services` wraps them
  in `RankServices` fakes that record what the command asked for.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from council.broker.etoro_read import EtoroReadClient
from council.broker.fake import FakeEtoro, eligibility_row, leverage_config
from council.broker.instruments import InstrumentMap
from council.paths import POLICY_DIR
from council.policy import SLEEVE_FILE, STOCK_RANK_FILE, StockSleeveFile
from council.stocks import commands, sleeve_file
from council.stocks.adopted import load_adopted
from council.stocks.rank import RankConfig
from council.stocks.report import Source, mediawiki_source
from council.stocks.universe import AI, RankInputs
from tests.conftest import SLEEVE_FIXTURE
from tests.stocks.test_rank import Book, quarters

D = date(2026, 8, 20)                                     # a rule anchor (first US session on/after 08-20)
NOW = datetime(2026, 8, 20, 21, 5, tzinfo=UTC)            # after the close of D
Q = "2026Q3"
ADOPTED_FILES = ("stock-sleeve-adopted.yaml", "stock-sleeve-variants-v1.yaml")
AI_LIST_TEXT = '# test AI-adjacent list\ntickers:\n  - "AIX"\n  - "ON"\n'
SECTORS = (("B", "3674"), ("H", "2834"), ("S", "5311"))  # BusEq, Hlth, Shops


# ------------------------------------------------------------------------------------ git


def git(root: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def tag(root: Path, name: str, *, force: bool = False) -> None:
    git(root, "tag", *(["-f"] if force else []), name)


def tree_digest(root: Path) -> dict[str, bytes]:
    """Every file under `root` (outside .git) with its bytes: a snapshot to compare before/after."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


# ------------------------------------------------------------------------------------ policy


def rank_settings_text() -> str:
    return commands._rank_settings_yaml(load_adopted())


def write_policy(dest: Path, *, rebased: bool = True, stock_rank: bool = True, ai_list: bool = True,
                 sleeve: str | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for src in sorted(POLICY_DIR.glob("*.yaml")):
        shutil.copyfile(src, dest / src.name)
    (dest / "variants").mkdir(exist_ok=True)
    for name in ADOPTED_FILES:
        shutil.copyfile(POLICY_DIR / "variants" / name, dest / "variants" / name)
    if rebased:
        shutil.copyfile(SLEEVE_FIXTURE / "universe.yaml", dest / "universe.yaml")
    if stock_rank:
        (dest / STOCK_RANK_FILE).write_text(rank_settings_text())
    if ai_list:
        (dest / commands.AI_LIST_FILE).write_text(AI_LIST_TEXT)
    if sleeve is not None:
        (dest / SLEEVE_FILE).write_text(sleeve)
    return dest


def make_repo(tmp_path: Path, name: str = "checkout", **kw: Any) -> Path:
    root = tmp_path / name
    write_policy(root / "policy", **kw)
    (root / "README").write_text("fixture\n")
    git(root, "init", "-q")
    commit(root, "policy")
    return root


def rebased_overlay(tmp_path: Path) -> Path:
    """A go-live draft directory: the re-based universe only."""
    out = tmp_path / "overlay"
    out.mkdir(exist_ok=True)
    shutil.copyfile(SLEEVE_FIXTURE / "universe.yaml", out / "universe.yaml")
    return out


def commit_sleeve(root: Path, sleeve: StockSleeveFile | str, *, tagged: bool = True, message: str = "sleeve") -> None:
    text = sleeve if isinstance(sleeve, str) else sleeve_file.dump(sleeve)
    (root / "policy" / SLEEVE_FILE).write_text(text)
    commit(root, message)
    if tagged:
        quarter = sleeve_file.loads(text).quarter
        tag(root, f"stocks-{quarter}", force=True)


def line(symbol: str, cik: int, *, role: str = "selected", sector: str = "BusEq", rank: int | None = 1,
         etoro: str | None = None, checked: datetime | None = NOW, credited: str | None = None,
         aliases: Sequence[str] = ()) -> dict[str, Any]:
    return {"symbol": symbol, "name": f"Test {symbol}", "role": role, "sector": sector,
            "cik": sleeve_file.cik10(cik), "rank": rank if role != "retiring" else None,
            "signal_ticker": sleeve_file.signal_ticker(symbol),
            "etoro_symbol": etoro or sleeve_file.broker_symbol_guess(symbol), "eligibility_checked_at": checked,
            "credited": credited, "aliases": list(aliases)}


def sleeve(lines: Iterable[Mapping[str, Any]], *, quarter: str = "2026Q2", retired: Sequence[Mapping[str, Any]] = (),
           rank_asof: date = date(2026, 5, 20)) -> StockSleeveFile:
    return sleeve_file.sleeve_model(quarter=quarter, rank_asof=rank_asof, rank_config_sha256="a" * 64,
                                    sleeve_weight=0.5, names_target=8, lines=list(lines), retired=list(retired))


# ------------------------------------------------------------------------------------ broker


def stock_row(symbol: str, iid: int, /, **overrides: Any) -> dict[str, Any]:
    """A raw eligibility row for a US stock the gate accepts; `overrides` replace raw keys."""
    row = eligibility_row(symbol, iid, configs=[leverage_config(settlement="REAL", direction="LONG",
                                                                leverage_values=[1], min_sl_pct=0.0,
                                                                max_sl_pct=100.0)])
    row.update(overrides)
    return row


@dataclass
class Broker:
    fake: FakeEtoro
    read: EtoroReadClient
    ids: dict[str, int] = field(default_factory=dict)

    def add(self, symbol: str, /, *, price: float = 100.0, **overrides: Any) -> int:
        iid = 20_000 + len(self.fake.instruments) + 1
        self.fake.add_instrument(symbol, iid, bid=price, ask=price * 1.001, row=stock_row(symbol, iid, **overrides))
        self.ids[symbol] = iid
        return iid

    def hold(self, symbol: str, *, units: float = 5.0, sl_rate: float | None = None) -> int:
        return self.fake.add_position(symbol, units=units, settlement="real", sl_rate=sl_rate).position_id

    def eligibility_posts(self) -> int:
        return self.fake.count("POST", "/api/v2/trading/info/eligibility")

    def writes(self) -> int:
        return sum(self.fake.count(m, p) for m, p in (("POST", "/api/v3/"), ("POST", "/api/v1/trading/execution"),
                                                     ("PATCH", "/api/v2/trading/positions")))


def broker(symbols: Iterable[str] = (), **per_symbol: dict[str, Any]) -> Broker:
    fake = FakeEtoro(clock=lambda: NOW)
    out = Broker(fake, EtoroReadClient("test-app-key", "test-read-key", transport=fake.transport(),
                                       sleep=lambda _s: None))
    for s in symbols:
        out.add(s, **per_symbol.get(s, {}))
    return out


def save_instruments(state_dir: Path, ids: Mapping[str, int]) -> None:
    InstrumentMap({}, path=state_dir / commands.INSTRUMENTS_FILE).merged(dict(ids), NOW).save()


# ------------------------------------------------------------------------------------ rank inputs


def sector_book(n_per_sector: int = 6, *, seed: int = 5, extra: Iterable[tuple[str, str, int]] = ()) -> Book:
    """Three sectors of `n_per_sector` clean names (keys B00.., H00.., S00..; CIK 5000 + 100·ord + i),
    plus `extra` (key, sector letter, cik) names, and one AI-only name (AIX)."""
    b = Book()
    rng = np.random.default_rng(seed)
    sic = dict(SECTORS)
    for sector, code in SECTORS:
        for i in range(n_per_sector):
            cik = cik_of(sector, i)
            b.add(f"{sector}{i:02d}", sic=code, cik=cik,
                  fund=quarters(cik, growth=float(rng.uniform(0.0, 0.06)), accel=float(rng.normal(0, 0.002)),
                                gm_step=float(rng.normal(0, 0.003))))
    for key, sector, cik in extra:
        b.add(key, sic=sic[sector], cik=cik, fund=quarters(cik, growth=float(rng.uniform(0.0, 0.06))))
    b.add("AIX", sic="3674", cik=9_990, sources=frozenset({AI}), fund=quarters(9_990, growth=0.001))
    return b


def cik_of(sector: str, i: int) -> int:
    return 5000 + 100 * ord(sector) + i


@dataclass
class FakeServices:
    """RankServices fakes: canned inputs, the READ client, an optional history prefetch."""

    book: Book
    read: Any = None
    history: Mapping[str, Any] | None = None
    history_flags: list[str] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def build_inputs(self, asof: date, ai_symbols: Sequence[str], config: RankConfig) -> tuple[RankInputs, list[Source]]:
        self.calls.append(("inputs", (asof, tuple(ai_symbols), config.cell)))
        return self.book.inputs(), [mediawiki_source("List of S&P 500 companies", asof)]

    def prefetch(self, policy: Any, now: datetime) -> tuple[Mapping[str, Any], list[str]]:
        asked = [ln.symbol for ln in policy.universe.lines]
        self.calls.append(("prefetch", asked))
        data = self.history or {}
        return {k: v for k, v in data.items() if k in asked}, list(self.history_flags)

    def services(self, *, prefetch: bool = False) -> commands.RankServices:
        return commands.RankServices(build_inputs=self.build_inputs, broker=self.read,
                                     prefetch=self.prefetch if prefetch else None, now=lambda: NOW)


def bars(n: int) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-08-20", periods=n)
    return pd.DataFrame({"close": np.linspace(90, 110, n), "volume": 1e6}, index=idx)
