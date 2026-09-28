"""SW-1: the Scout ticker resolver (design §1.4 step 1): exact symbol -> SEC CIK -> ONE eligibility
request by exact symbols with the idea's side -> capability gate -> InstrumentMap. Fails closed
without a broker; a recorded fixture labels passes `rehearsal_unverified`. FakeEtoro READ only."""

from __future__ import annotations

import json

import pytest

from council.broker.fake import leverage_config
from council.broker.instruments import InstrumentMap
from council.operator.capabilities import Capabilities
from council.stocks import eligibility as gate
from council.stocks.sec import TickerRow
from council.swing import resolve as rs
from tests.stocks import cli_support as cs

NOW = cs.NOW
SEC = [TickerRow(1045810, "NVDA", "NVIDIA"), TickerRow(1067983, "BRK-B", "Berkshire"),
       TickerRow(320193, "AAPL", "Apple"), TickerRow(789019, "MSFT", "Microsoft")]
STOPS = {"stop_min": 0.02, "stop_max_long": 0.12, "stop_max_short": 0.08}
LONG = leverage_config(settlement="REAL", direction="LONG", leverage_values=[1], min_sl_pct=0.0, max_sl_pct=100.0)
SHORT = leverage_config(settlement="CFD", direction="SHORT", leverage_values=[1], min_sl_pct=0.0, max_sl_pct=50.0)


class Caps:
    """Capabilities with the swing vehicles proven (SW-5 adds them to the registry)."""

    verified = frozenset({"stock_real_long", "stock_cfd_short"})

    def allows_vehicle(self, required):
        return all(c in self.verified for c in required)


@pytest.fixture
def cfg(policy):
    return gate.gate_config(policy, unit_share=0.08, virtual_nav_usd=10_000.0)


def _broker(**rows):
    b = cs.broker()
    for sym, configs in rows.items():
        b.add(sym.replace("_", "."), leverageConfigs=configs)
    return b


def test_exact_symbols_one_request_and_saved_to_instrument_map(cfg, tmp_path):
    b = _broker(NVDA=[LONG, SHORT], BRK_B=[LONG])
    imap = InstrumentMap({}, path=tmp_path / "instruments.json")
    ideas = [rs.IdeaRef("NVDA", "short"), rs.IdeaRef("BRK.B", "long"), rs.IdeaRef("NVDAA", "long")]
    out, new_map = rs.resolve_ideas(ideas, sec_tickers=SEC, cfg=cfg, now=NOW, read=b.read,
                                    capabilities=Caps(), instruments=imap, **STOPS)
    assert b.eligibility_posts() == 1 and b.writes() == 0
    nvda, brk, bad = out
    assert nvda.ok and nvda.tradeable and nvda.line_id == "NVDA" and nvda.cik == 1045810
    assert nvda.max_sl_pct == 50.0
    assert brk.ok and brk.line_id == "BRK_B" and brk.broker_symbol == "BRK.B"
    assert (bad.ok, bad.reason) == (False, "unresolved_symbol")
    assert new_map is not None and new_map.get("BRK_B") == b.ids["BRK.B"] and new_map.get("NVDA") == b.ids["NVDA"]


def test_no_cfd_short_config_is_not_eligible_short(cfg):
    b = _broker(AAPL=[LONG])
    (r,), _ = rs.resolve_ideas([rs.IdeaRef("AAPL", "short")], sec_tickers=SEC, cfg=cfg, now=NOW,
                               read=b.read, capabilities=Caps(), **STOPS)
    assert (r.ok, r.reason) == (False, "not_eligible_short") and "no_cfd_short_1x" in r.detail
    (r,), _ = rs.resolve_ideas([rs.IdeaRef("AAPL", "long")], sec_tickers=SEC, cfg=cfg, now=NOW,
                               read=b.read, capabilities=Caps(), **STOPS)
    assert r.ok


def test_sl_bounds_use_the_swing_stop_range(cfg):
    narrow = leverage_config(settlement="CFD", direction="SHORT", leverage_values=[1], min_sl_pct=5.0,
                             max_sl_pct=50.0)
    b = _broker(MSFT=[LONG, narrow])
    (r,), _ = rs.resolve_ideas([rs.IdeaRef("MSFT", "short")], sec_tickers=SEC, cfg=cfg, now=NOW,
                               read=b.read, capabilities=Caps(), **STOPS)
    assert r.reason == "not_eligible_short" and "sl_bounds" in r.detail


def test_returned_symbol_must_equal_request(cfg):
    b = cs.broker()
    b.add("NVDA.X", leverageConfigs=[LONG])     # the broker has no exact NVDA row
    (r,), _ = rs.resolve_ideas([rs.IdeaRef("NVDA", "long")], sec_tickers=SEC, cfg=cfg, now=NOW,
                               read=b.read, capabilities=Caps(), **STOPS)
    assert r.reason == "not_eligible_long" and r.detail == ("not_found",)


def test_capability_gate_fails_closed(cfg, tmp_path):
    b = _broker(NVDA=[LONG])
    imap = InstrumentMap({}, path=tmp_path / "i.json")
    for caps in (Capabilities(), None):
        (r,), new_map = rs.resolve_ideas([rs.IdeaRef("NVDA", "long")], sec_tickers=SEC, cfg=cfg, now=NOW,
                                         read=b.read, capabilities=caps, instruments=imap, **STOPS)
        assert r.reason == "capability_not_proven:stock_real_long" and new_map is None


def test_no_broker_fails_closed(cfg):
    out, new_map = rs.resolve_ideas([rs.IdeaRef("NVDA", "long"), rs.IdeaRef("XXXXQ", "long")],
                                    sec_tickers=SEC, cfg=cfg, now=NOW, **STOPS)
    assert [r.reason for r in out] == ["eligibility_unavailable", "unresolved_symbol"] and new_map is None


def test_recorded_fixture_labels_rehearsal_and_saves_nothing(cfg, tmp_path):
    row = cs.stock_row("NVDA", 20_001, leverageConfigs=[LONG, SHORT])
    rates = {"rates": [{"instrumentID": 20_001, "bid": 100.0, "ask": 100.1}]}
    recorded = rs.RecordedRead(json.loads(json.dumps({"eligibilities": [row]})), rates, fetched_at=NOW)
    imap = InstrumentMap({}, path=tmp_path / "i.json")
    (r,), new_map = rs.resolve_ideas([rs.IdeaRef("NVDA", "short")], sec_tickers=SEC, cfg=cfg, now=NOW,
                                     recorded=recorded, instruments=imap, **STOPS)
    assert r.ok and r.label == "rehearsal_unverified" and not r.tradeable and new_map is None


def test_quota_and_conflicting_sides(cfg):
    many = [TickerRow(1000 + i, f"T{i}A", "x") for i in range(10)]
    b = _broker(**{f"T{i}A": [LONG] for i in range(10)})
    out, _ = rs.resolve_ideas([rs.IdeaRef(f"T{i}A", "long") for i in range(10)], sec_tickers=many, cfg=cfg,
                              now=NOW, read=b.read, capabilities=Caps(), **STOPS)
    assert [r.reason for r in out][-2:] == ["resolve_quota", "resolve_quota"]
    assert b.eligibility_posts() == 1
    out, _ = rs.resolve_ideas([rs.IdeaRef("NVDA", "long"), rs.IdeaRef("NVDA", "short")], sec_tickers=SEC,
                              cfg=cfg, now=NOW, **STOPS)
    assert out[1].reason == "conflicting_sides"


def test_resolver_never_imports_the_writer():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rs))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert names and not [n for n in names if "write" in n or "execution" in n]


def test_a_failed_eligibility_read_fails_closed(cfg):
    class Broken:
        def eligibility(self, **_):
            raise RuntimeError("boom")

        def rates(self, ids):
            raise AssertionError("never asked")

    out, new_map = rs.resolve_ideas([rs.IdeaRef("NVDA", "long")], sec_tickers=SEC, cfg=cfg, now=NOW,
                                    read=Broken(), capabilities=Caps(), **STOPS)
    assert [r.reason for r in out] == ["eligibility_unavailable"] and not out[0].ok and new_map is None
