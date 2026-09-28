"""SW-6: the SQ-8 paper benchmark (design swing-book.md rev 2, §6.2, §6.4).

Parity: the incremental paper sleeve equals the frozen study simulator (`simulate_budgeted` over
`sleeve_plan` in scripts/stock_sleeve_study.py) on the same inputs; the order decision is the frozen
`council.reference.sleeve.pending_trades`; parameters come from the adoption record."""

from __future__ import annotations

import ast
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from council import paths
from council.benchmark import sq8
from council.reference import sleeve as sleeve_rule
from council.stocks import adopted

sys.path.insert(0, str(paths.REPO_ROOT / "scripts"))
import stock_sleeve_study as S  # noqa: E402


def _panel(seed: int = 3, days: int = 260, names: int = 20):
    rng = np.random.default_rng(seed)
    rows = pd.bdate_range("2025-01-02", periods=days)
    syms = [f"N{i:02d}" for i in range(names)]
    rets = pd.DataFrame(rng.normal(0.0004, 0.02, size=(days, names)), index=rows, columns=syms)
    rets.iloc[0] = np.nan
    rets.iloc[40:45, 3] = np.nan                        # a gap: carried as zero return
    decisions = {}
    for k, i in enumerate((0, 63, 126, 189, 250)):
        decisions[rows[i]] = list(rng.choice(syms, size=8, replace=False)) if k != 2 else list(syms[:6])
    return rows, rets, decisions


def _study(rows, rets, decisions, params: sq8.SQ8Params):
    plan = S.sleeve_plan(rows, decisions, share=params.share, n=params.names, level=None,
                         deadband_level=params.deadband_level, min_share=params.min_share, name="sq8")
    cols = list(plan.targets.columns)
    costs = S.flat_costs(cols, params.per_side, params.fixed, "stock_real")
    return S.simulate_budgeted(plan, rets, costs, sleeve_cols=cols, sleeve_budget=params.share)


@pytest.mark.parametrize("seed,fixed", [(3, 0.0), (7, 0.001), (11, 0.0)])
def test_parity_with_the_study_simulator(seed, fixed):
    rows, rets, decisions = _panel(seed)
    params = sq8.SQ8Params.from_adopted(fixed=fixed)
    ref = _study(rows, rets, decisions, params)
    nav, weights = sq8.simulate(rows, decisions, rets, params)
    np.testing.assert_allclose(nav.to_numpy(), ref.nav.to_numpy(), rtol=0, atol=1e-12)
    w = weights.reindex(columns=ref.weights.columns).fillna(0.0)
    np.testing.assert_allclose(w.to_numpy(), ref.weights.to_numpy(), rtol=0, atol=1e-12)
    assert (ref.costs > 0).any()                                    # the fixture really trades


def test_parity_after_a_restart_from_the_saved_book(tmp_path):
    rows, rets, decisions = _panel(5)
    params = sq8.SQ8Params.from_adopted()
    ref = _study(rows, rets, decisions, params)
    book = sq8.PaperSleeve()
    for i, t in enumerate(rows):
        if i == 100:                                                # the watch restarts
            sq8.save_book(tmp_path, book)
            book = sq8.load_book(tmp_path)
        sel = decisions.get(t)
        sq8.step(book, params, t.strftime("%Y-%m-%d"), rets.loc[t].to_dict() if i else {},
                 selection=None if sel is None else list(sel))
    assert book.nav == pytest.approx(float(ref.nav.iloc[-1]), abs=1e-12)


def test_params_are_the_adopted_rule():
    rule = adopted.load_adopted()
    p = sq8.SQ8Params.from_adopted(rule)
    assert (p.names, p.share) == (8, 1.0)
    assert p.unit == pytest.approx(0.125)
    assert p.threshold == pytest.approx(sleeve_rule.drift_threshold(0.125, rule.deadband_level,
                                                                    rule.deadband_min_nav_share / rule.sleeve_share))
    assert p.per_side == pytest.approx(0.0125)                      # the declared cost per leg
    with pytest.raises(sq8.BenchmarkError):
        sq8.SQ8Params.from_adopted(SimpleNamespace(cell="SC-8", overlay="none"))


def test_the_order_decision_is_the_frozen_pending_trades(monkeypatch):
    calls = []
    real = sleeve_rule.pending_trades

    def spy(*a, **kw):
        calls.append(kw.get("budget"))
        return real(*a, **kw)

    monkeypatch.setattr(sleeve_rule, "pending_trades", spy)
    book = sq8.PaperSleeve()
    sq8.step(book, sq8.SQ8Params(), "2026-10-01", {}, selection=["A", "B"])
    assert calls == [1.0] and set(book.pending) == {"A", "B"}
    src = Path(sq8.__file__).read_text()
    names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
    assert not names & {"select_rule", "add_scores", "sector_quotas", "pending_trades", "rank_average"}


def test_lag_one_selection_and_cost():
    p = sq8.SQ8Params()
    book = sq8.PaperSleeve()
    r0 = sq8.step(book, p, "2026-10-01", {}, selection=[f"S{i}" for i in range(8)])
    assert r0.traded == () and book.held == {}                       # decided at D, not traded at D
    r1 = sq8.step(book, p, "2026-10-02", {"S0": 0.5})
    assert len(r1.traded) == 8 and r1.cost == pytest.approx(1.0 * 0.0125)
    assert book.nav == pytest.approx(1 - 0.0125)
    with pytest.raises(sq8.BenchmarkError):
        sq8.step(book, p, "2026-10-02", {})
    with pytest.raises(ValueError):
        sq8.step(book, p, "2026-10-03", {}, selection=["A"] * 2)


def test_index_lines():
    assert sq8.matched_trade_return("long", 1.2, 0.05) == pytest.approx(0.06 - 0.025)
    assert sq8.matched_trade_return("short", 1.0, 0.05) == pytest.approx(-0.05 - 0.025)
    legs = [sq8.MatchedLeg("long", 1.0, 0.01, 0.2, entry_day=True), sq8.MatchedLeg("short", 2.0, 0.01, 0.1)]
    assert sq8.matched_index_day(legs) == pytest.approx(0.2 * 0.01 - 0.2 * 0.0125 - 0.1 * 0.02)
    assert sq8.index_hold_day(0.01, first_day=True) == pytest.approx(-0.0125)
    assert sq8.index_hold_day(0.01, first_day=False) == 0.01
    assert sq8.index_hold_day(None, first_day=False) is None
    assert sq8.cumulative([{"a": 0.1}, {"a": None}, {"a": -0.1}], "a") == pytest.approx(-0.01)


def test_benchmark_row_goes_through_the_ledger_api(tmp_path):
    from council.ledger.db import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite3")
    book = sq8.PaperSleeve()
    res = sq8.step(book, sq8.SQ8Params(), "2026-10-01", {}, selection=["AAA"])
    row = sq8.benchmark_row(book, res, matched_idx_ret=None, idx_hold_ret=-0.0125)
    sq8.record_day(ledger, row)
    days = ledger.benchmark_days()
    assert days[0]["day"] == "2026-10-01" and days[0]["detail"]["names"] == ["AAA"]
    assert days[0]["detail"]["sq8_index"] == 100.0


def test_selection_files_and_public_page(tmp_path):
    sel = [f"T{i}" for i in range(8)]
    files = sq8.write_selection(tmp_path, date(2026, 11, 20), sel, dict.fromkeys(sel, "Hlth"))
    text = files["public"].read_text()
    assert sq8.LABEL in text and "12.5%" in text and "$" not in text
    assert sq8.load_selections(tmp_path) == {"2026-11-20": sel}
    sel2 = [*sel[:7], "NEW"]
    files2 = sq8.write_selection(tmp_path, date(2027, 2, 22), sel2, dict.fromkeys(sel2, "Hlth"))
    t2 = files2["public"].read_text()
    assert "In this quarter: NEW." in t2 and "Out this quarter: T7." in t2
    assert sq8.previous_keys(tmp_path, date(2027, 5, 20)) == sel2


def test_run_benchmark_rank_holds_the_previous_selection(tmp_path):
    sel = [f"T{i}" for i in range(8)]
    sq8.write_selection(tmp_path, date(2026, 11, 20), sel, dict.fromkeys(sel, "Hlth"))
    seen = {}

    def fake_rank(asof, inputs, cfg):
        seen["held"] = inputs.held
        elig = pd.DataFrame({"symbol": [f"U{i}" for i in range(8)], "sector": ["Enrgy"] * 8},
                            index=[f"U{i}" for i in range(8)])
        return SimpleNamespace(selected=tuple(elig.index), kept=(), eligible=elig)

    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class In:
        candidates: tuple = ()
        held: tuple = ()

    out = sq8.run_benchmark_rank(date(2027, 2, 22), lambda a, b, c: (In(), []), state_dir=tmp_path,
                                 config=object(), rank_fn=fake_rank)
    assert seen["held"] == tuple(sel)
    assert out.quarter == "2027Q1" and out.selected[0] == "U0"
    assert sq8.LABEL in out.report_lines()[0]


def test_benchmark_never_imports_a_broker():
    for mod in ("council/benchmark/sq8.py", "council/swing/paper.py", "council/swing/metrics.py",
                "council/swing/status.py"):
        tree = ast.parse((paths.REPO_ROOT / "src" / mod).read_text())
        imported = {getattr(n, "module", None) or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("council.broker", "council.execution", "council.operator")) for m in imported), mod
