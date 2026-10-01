"""The paper book (`council.paperbook`): a persisted paper broker so paper cycles build a portfolio,
plus the initial funding allowance (`risk.initial_build`). Stubs only: no LLM, no broker, no network."""

from __future__ import annotations

import json
import re
import stat
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from council import cycle as C
from council.context import hold_reference_stub
from council.ledger.db import Ledger
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.paperbook import PaperBook, paper_book_public
from council.policy import Policy
from council.runtime import CycleContext, Sources
from council.settings import Settings
from tests.integration import test_end_to_end as e2e

SLOT1 = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)      # Thursday
SLOT2 = SLOT1 + timedelta(hours=4)                      # 18:40, the swing slot
SLOT3 = datetime(2026, 10, 5, 14, 40, tzinfo=UTC)      # Monday: new bars, the swing target hit Friday
FUNDED = 2000.0
MONEY = re.compile(r"\$|usd|units|2000|position_id", re.I)


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _bars(day: date, o, h, lo, c) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(day)]).tz_localize("UTC")
    return pd.DataFrame({"open": [o], "high": [h], "low": [lo], "close": [c]}, index=idx)


def _swing_src():
    def daily_bars(tickers, day):
        return {"TAAA": pd.concat([_bars(date(2026, 10, 1), 100, 101, 99.5, 100.5),
                                   _bars(date(2026, 10, 2), 101, 109, 100.5, 108.5)])}

    return SimpleNamespace(daily_bars=daily_bars, reference_price=lambda t: 100.0 if t == "TAAA" else None,
                           unavailable=(), prepare=None, drain=lambda: [])


def _ctx(tmp_path, clk):
    state = tmp_path / "paper"
    state.mkdir(exist_ok=True)
    (state / "account").mkdir(exist_ok=True)
    (state / "account" / "swing.json").write_text(json.dumps({"funded_real_nav_usd": FUNDED}))
    ledger = Ledger(state / "ledger.sqlite3", clock=clk)
    ledger.migrate()
    ctx = CycleContext(policy=Policy.load(include_sleeve=False), settings=Settings(role="dev", mode="stub"),
                       ledger=ledger, gateway=StubGateway(hold_reference_stub()), registry=PromptRegistry(),
                       sources=Sources(history=e2e._history, events=e2e._no_events, broker=None),
                       publisher=None, clock=clk, state_dir=state)
    ctx.sources.swing = _swing_src()
    return ctx


def _entry():
    return C.SwingEntry(trade_id="trade:t1", idea_id="idea:1", ticker="TAAA", side="long", instrument_id=None,
                        size_nav=0.04, stop_pct=0.05, target_pct=0.08, time_stop_date="2026-10-16",
                        detail={"sigma_daily": 0.02, "beta": 1.0})


@pytest.fixture(scope="module")
def three_cycles(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("paperbook")
    mp = pytest.MonkeyPatch()
    enter = {"on": False}

    async def fake_run_swing(ctx, rec, **kw):
        out = C.SwingRun()
        if enter["on"]:
            out.slot_ok, out.entries = True, [_entry()]
        return out

    mp.setattr(C, "run_swing", fake_run_swing)
    clk = _Clock(SLOT1 + timedelta(minutes=3))
    ctx = _ctx(tmp, clk)
    outs, books = [], []
    for slot, swing_on in ((SLOT1, False), (SLOT2, True), (SLOT3, False)):
        clk.t = slot + timedelta(minutes=3)
        enter["on"] = swing_on
        outs.append(C.run_cycle(ctx))
        books.append(PaperBook.load(ctx.state_dir).to_json())
    mp.undo()
    return ctx, outs, books


def _rec(ctx, out):
    return ctx.ledger.get_cycle(out.cycle_id)


def _check(rec, rule, name):
    return next(c for c in rec["risk"]["checks"] if c["rule_id"] == rule and c["name"] == name)


def test_cycle1_initial_build_mounts_the_core(three_cycles):
    ctx, outs, books = three_cycles
    rec = _rec(ctx, outs[0])
    assert "initial_build" in rec["flags"] and "paper_book" in rec["flags"], rec["flags"]
    assert not any("current book taken as flat" in r for r in rec["risk"]["hold_reasons"])
    assert "initial build" in _check(rec, "R14", "cycle_cost_bps")["detail"]
    final = {s: w for s, w in rec["risk"]["final_w"].items() if abs(w) > 1e-9}
    assert not any("R14 cycle cost budget" in r or r.startswith("R13") for r in rec["risk"]["hold_reasons"])
    book = PaperBook.load(ctx.state_dir)
    assert sum(final.values()) > 0.5
    pub = book.public()
    # the paper book now holds ~ the decision's weights (marked at the same prices, minus the cost)
    for s, w in final.items():
        assert pub["core_weights_pct"][s] == pytest.approx(100 * w, abs=0.5)
    assert any(c["kind"] == "core_fill" for c in map(json.loads, (ctx.state_dir / "book_ledger.jsonl").read_text().splitlines()))


def test_cycle2_sees_the_held_book_and_opens_the_swing_entry(three_cycles):
    ctx, outs, books = three_cycles
    rec = _rec(ctx, outs[1])
    # build phase: the lines held since cycle 1 are no longer exempt (ETH, below the minimum trade
    # size, was never filled and keeps the phase open until max_cycles)
    assert set(books[1]["build_phase"]["filled"]) >= {"BTC", "NDX", "SEMIS", "SPX"}
    r14 = _check(rec, "R14", "cycle_cost_bps")
    assert r14["passed"] and "initial build" not in (r14["detail"] or "")
    assert rec["risk"]["base_w"]          # the held paper book, not flat
    assert any(abs(w) > 0.05 for w in rec["risk"]["base_w"].values())
    assert "SW_TAAA" in rec["risk"]["final_w"]
    t = books[1]["swing"]["trade:t1"]
    assert t["status"] == "open" and t["entry_day"] == "2026-10-01"
    split = rec["extras"]["book_split"]
    assert split                            # S18 saw the paper entry


def test_cycle3_marks_to_market_and_the_target_hits(three_cycles):
    ctx, outs, books = three_cycles
    rec = _rec(ctx, outs[2])
    assert "paper_swing_exit:target" in rec["flags"], rec["flags"]
    t = books[2]["swing"]["trade:t1"]
    assert t["status"] == "closed" and t["exit_reason"] == "target"
    assert t["net_ret"] == pytest.approx(0.08 - 0.025)
    assert books[2]["marked_at"] != books[1]["marked_at"]
    pub = paper_book_public(ctx.state_dir.parent)
    assert pub["swing_trades"][0]["return_net_pct"] == pytest.approx(5.5)
    assert pub["paper_return_pct"] != 0.0


def test_book_files_are_private(three_cycles):
    ctx, _, _ = three_cycles
    for name in ("book.json", "book_ledger.jsonl"):
        assert stat.S_IMODE((ctx.state_dir / name).stat().st_mode) == 0o600


# ------------------------------------------------------------------ PaperBook unit behaviour
NOW = datetime(2026, 10, 1, 18, 43, tzinfo=UTC)


def _book(tmp_path, nav=FUNDED):
    return PaperBook.start(tmp_path, nav, at=NOW)


def _open(book, side="long", stop=0.05, target=0.08):
    book.enter_swing(trade_id="trade:x", ticker="TBBB", side=side, line="SW_TBBB", size_nav=0.05, entry_ref=50.0,
                     stop_pct=stop, target_pct=target, entry_day="2026-10-01", time_stop_day="2026-10-06",
                     setup="breakout", at=NOW, cycle_id="c1")


def test_stop_gap_and_time_stop(tmp_path):
    book = _book(tmp_path)
    _open(book)
    book.settle_swing({"TBBB": _bars(date(2026, 10, 2), 46, 47, 45, 46.5)}, lambda t: 46.5, NOW)
    t = book.swing["trade:x"]
    assert t.exit_reason == "stop_gap" and t.net_ret == pytest.approx(46 / 50 - 1 - 0.025)
    book2 = _book(tmp_path / "b")
    _open(book2, side="short")
    bars = pd.concat([_bars(date(2026, 10, d), 50, 50.5, 49.5, 50.2) for d in (2, 5, 6)])
    book2.settle_swing({"TBBB": bars}, lambda t: 50.2, NOW)
    assert book2.swing["trade:x"].exit_reason == "time"


def test_open_swing_counts_as_exposure_and_weight(tmp_path):
    book = _book(tmp_path)
    _open(book)
    assert book.swing_exposure_nav() == pytest.approx(0.05, rel=0.02)
    assert book.signed_w()["SW_TBBB"] == pytest.approx(0.05, rel=0.02)
    snap = book.snapshot(NOW)
    assert snap.positions == [] and "paper_book" in snap.flags


def test_public_view_is_percent_only_and_nav_invariant(tmp_path):
    views = []
    for k, nav in enumerate((700.0, FUNDED, 1_000_000.0)):
        book = _book(tmp_path / str(k), nav)
        book.trade_core({"NDX": 0.35, "BTC": 0.13}, {"NDX": 400.0, "BTC": 60000.0}, lambda s, b, a: abs(a - b) * 10,
                        at=NOW, cycle_id="c1")
        _open(book)
        book.mark_core({"NDX": 420.0, "BTC": 57000.0}, NOW)
        book.settle_swing({}, lambda t: 52.0, NOW)
        pub = book.public()
        text = json.dumps(pub)
        assert not MONEY.search(text), text
        assert "400" not in text and "60000" not in text and "trade:x" not in text
        views.append({k: v for k, v in pub.items() if k not in ("started_at", "marked_at")})
    assert views[0] == views[1] == views[2]


def test_status_cli_prints_no_money(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from council.cli import app

    state = tmp_path / "paper"
    book = _book(state)
    book.trade_core({"NDX": 0.35}, {"NDX": 400.0}, lambda s, b, a: 5.0, at=NOW, cycle_id="c1")
    _open(book)
    book.save()
    monkeypatch.setenv("COUNCIL_STATE_DIR", str(tmp_path))
    res = CliRunner().invoke(app, ["paper", "status"])
    assert res.exit_code == 0, res.output
    assert "NDX" in res.output and "TBBB" in res.output and "%" in res.output
    assert not MONEY.search(res.output), res.output
    empty = CliRunner().invoke(app, ["paper", "status", "--state-dir", str(tmp_path / "nothing" / "paper")])
    assert empty.exit_code == 0 and "no paper book" in empty.output


# ------------------------------------------------------------------ the initial funding allowance
def test_initial_build_only_on_an_empty_book(tmp_path):
    from council.models.broker import ExposureSnapshot

    policy = Policy.load(include_sleeve=False)
    led = SimpleNamespace(pending_open_weights=lambda: {})

    def snap(w):
        return ExposureSnapshot(taken_at=NOW, equity_usd=1.0, credit_usd=1.0, positions=[], signed_w=w,
                                gross=0, net=0, margin_use=0)

    assert C.initial_build_of(policy, snap({}), "NORMAL", led) is True
    assert C.initial_build_of(policy, snap({"NDX": 0.1}), "NORMAL", led) is False
    assert C.initial_build_of(policy, None, "NORMAL", led) is False           # unknown book: never
    assert C.initial_build_of(policy, snap({}), "WARN", led) is False
    held = SimpleNamespace(pending_open_weights=lambda: {"NDX": 0.1})
    assert C.initial_build_of(policy, snap({}), "NORMAL", held) is False


def test_engine_ignores_initial_build_on_a_held_book():
    from council.risk.engine import RiskEngine, _Run

    eng = RiskEngine(Policy.load(include_sleeve=False))
    base = dict(levels={}, ref={}, bands={}, states={}, unit_weights={}, kill_state="NORMAL", cost_quotes={},
                events=[], last_change={}, turnover_7d=0.0, material_changed=False, basis="code_only", now=NOW,
                vol_fn=None, turnover_30d=None, cost_30d_bps=None, book_vol_ratio=None, stop_hits={},
                blockers=[], broker_min_share={}, nav_drawdown=None, pending_w={}, held_levels=None,
                copy_min_share=0.0, cost_30d_fee_bps=0.0, core_rescale=False, swing_lines=())
    from council.models.broker import ExposureSnapshot

    def snap(w):
        return ExposureSnapshot(taken_at=NOW, equity_usd=1.0, credit_usd=1.0, positions=[], signed_w=w,
                                gross=0, net=0, margin_use=0)

    assert _Run(eng, snapshot=snap({}), initial_build=True, **base).initial_exempt == {"R13", "R14", "R15"}
    assert _Run(eng, snapshot=snap({"NDX": 0.2}), initial_build=True, **base).initial_exempt == frozenset()
    assert _Run(eng, snapshot=snap({}), initial_build=False, **base).initial_exempt == frozenset()
    assert _Run(eng, snapshot=None, initial_build=True, **base).initial_exempt == frozenset()


def test_policy_may_exempt_only_churn_and_cost_rules():
    from council.invariants import InvariantViolation, check_policy

    policy = Policy.load(include_sleeve=False)
    assert tuple(policy.risk["initial_build"]["exempt"]) == ("R13", "R14", "R15")
    bad = dict(policy.risk, initial_build={"exempt": ["R13", "R7"]})
    with pytest.raises(InvariantViolation):
        check_policy(policy.model_copy(update={"risk": bad}))


def test_public_paper_record_carries_the_book(tmp_path):
    from council.publish.paper import paper_book_view

    book = _book(tmp_path)
    book.trade_core({"NDX": 0.35}, {"NDX": 400.0}, lambda s, b, a: 5.0, at=NOW, cycle_id="c1")
    _open(book)
    book.save()
    view = paper_book_view(paper_book_public(tmp_path))
    assert view is not None and view.core[0].line == "NDX" and view.swing_trades[0].ticker == "TBBB"
    text = view.model_dump_json()
    assert not MONEY.search(text) and "400" not in text
    assert paper_book_view({}) is None



# ------------------------------------------------------------------ the build phase (across sessions)
EVENING = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)     # Thursday 18:40: London ETFs closed, crypto open
NEXT_DAY = datetime(2026, 10, 2, 14, 40, tzinfo=UTC)    # Friday 14:40: London open


def test_build_phase_spans_market_sessions(tmp_path, monkeypatch):
    async def no_swing(ctx, rec, **kw):
        return C.SwingRun()

    monkeypatch.setattr(C, "run_swing", no_swing)
    clk = _Clock(EVENING + timedelta(minutes=3))
    ctx = _ctx(tmp_path, clk)
    out1 = C.run_cycle(ctx)
    rec1 = _rec(ctx, out1)
    held1 = {s for s, w in PaperBook.load(ctx.state_dir).signed_w().items() if abs(w) > 1e-6}
    assert "initial_build" in rec1["flags"]
    assert "BTC" in held1 and not held1 & {"NDX", "SEMIS", "SPX"}, held1      # crypto only at 18:40

    clk.t = NEXT_DAY + timedelta(minutes=3)
    out2 = C.run_cycle(ctx)
    rec2 = _rec(ctx, out2)
    book = PaperBook.load(ctx.state_dir)
    held2 = {s for s, w in book.signed_w().items() if abs(w) > 1e-6}
    assert "initial_build" in rec2["flags"], rec2["flags"]       # the next session still builds
    assert {"NDX", "SEMIS", "SPX"} <= held2, (held2, rec2["risk"]["hold_reasons"])
    assert not any(r.startswith(("R13", "R14 cycle", "R14 30-day")) for r in rec2["risk"]["hold_reasons"])
    assert "initial build" in _check(rec2, "R14", "cycle_cost_bps")["detail"]
    assert "BTC" in book.build_phase["filled"] and book.build_phase["open"] is True


def _snap(w):
    from council.models.broker import ExposureSnapshot

    return ExposureSnapshot(taken_at=NOW, equity_usd=1.0, credit_usd=1.0, positions=[], signed_w=w,
                            gross=0, net=0, margin_use=0)


def test_build_phase_exempts_only_never_filled_lines_and_closes():
    policy = Policy.load(include_sleeve=False)
    led = SimpleNamespace(pending_open_weights=lambda: {})
    pb = SimpleNamespace(build_phase={"open": True, "filled": ["ETH"], "cycles": 1,
                                      "targets": ["BTC", "ETH", "NDX", "SPX"]})
    phase, lines = C.build_phase_open(None, policy, _snap({"BTC": 0.13}), "NORMAL", led, pb)
    assert phase["open"] and {"NDX", "SPX", "SEMIS"} <= lines and not lines & {"BTC", "ETH"}
    assert C.build_phase_open(None, policy, _snap({"BTC": 0.13}), "WARN", led, pb)[1] == frozenset()
    # closes once every target line was filled at least once ...
    pb.build_phase = {"open": True, "filled": ["ETH", "NDX", "SPX"], "cycles": 2, "targets": ["BTC", "ETH", "NDX", "SPX"]}
    phase, lines = C.build_phase_open(None, policy, _snap({"BTC": 0.13}), "NORMAL", led, pb)
    assert phase["open"] is False and lines == frozenset()
    # ... or after max_cycles (policy key)
    pb.build_phase = {"open": True, "filled": [], "cycles": 5, "targets": ["NDX"]}
    assert C.build_phase_open(None, policy, _snap({}), "NORMAL", led, pb)[1] == frozenset()
    assert policy.risk["initial_build"]["max_cycles"] == 5
    # live: no stored phase -> open only on an empty book (ledger runtime key)
    store: dict = {}
    live = SimpleNamespace(pending_open_weights=lambda: {}, get_runtime=lambda k, d=None: store.get(k, d))
    assert C.build_phase_open(None, policy, _snap({"NDX": 0.2}), "NORMAL", live)[1] == frozenset()
    phase, lines = C.build_phase_open(None, policy, _snap({}), "NORMAL", live)
    assert phase["open"] and "NDX" in lines


def test_engine_build_lines_are_per_line():
    from council.risk.engine import RiskEngine, _Run

    eng = RiskEngine(Policy.load(include_sleeve=False))
    base = dict(levels={}, ref={}, bands={}, states={}, unit_weights={}, kill_state="NORMAL", cost_quotes={},
                events=[], last_change={}, turnover_7d=0.0, material_changed=False, basis="code_only", now=NOW,
                vol_fn=None, turnover_30d=None, cost_30d_bps=None, book_vol_ratio=None, stop_hits={},
                blockers=[], broker_min_share={}, nav_drawdown=None, pending_w={}, held_levels=None,
                copy_min_share=0.0, cost_30d_fee_bps=0.0, core_rescale=False, swing_lines=())
    run = _Run(eng, snapshot=_snap({"BTC": 0.13}), initial_build=frozenset({"NDX", "BTC"}), **base)
    assert run.build_lines == {"NDX"}                       # a held line is never exempt
    assert run.ib("R14", "NDX") and not run.ib("R14", "BTC") and not run.ib("R14", "SPX")
    closed = _Run(eng, snapshot=_snap({"BTC": 0.13}), initial_build=frozenset(), **base)
    assert closed.initial_exempt == frozenset() and not closed.ib("R14", "NDX")   # after closure R14 applies


def test_public_view_splits_out_the_declared_costs_paid(tmp_path):
    """cost_pct: every declared cost the paper fills paid (core legs + swing legs), % of the start
    NAV, summed from the private ledger; return = market - cost (percent only)."""
    book = _book(tmp_path / "c")
    assert book.public()["cost_pct"] == 0.0
    book.trade_core({"NDX": 0.3, "BTC": 0.1}, {"NDX": 400.0, "BTC": 60000.0}, lambda s, b, a: 4.0, at=NOW, cycle_id="c1")
    pub = book.public()
    assert pub["cost_pct"] == 0.08                                    # two legs x 4 bps
    assert pub["paper_return_pct"] == -0.08                           # nothing moved yet: the cost is the return
    book.enter_swing(trade_id="trade:y", ticker="ACME", side="long", line="SW_ACME", size_nav=0.08, entry_ref=50.0,
                     stop_pct=0.05, target_pct=0.08, entry_day="2026-10-01", time_stop_day="2026-10-15",
                     setup="news_continuation", at=NOW, cycle_id="c1")
    assert book.public()["cost_pct"] == pytest.approx(0.18, abs=0.011)   # + 1.25% of an 8% position
    assert not MONEY.search(json.dumps(book.public()))
