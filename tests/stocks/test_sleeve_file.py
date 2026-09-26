"""`council.stocks.sleeve_file` (design §3.2, §3.5, D16): the quoted writer, the rank calendar, the
diff against the committed sleeve with two-phase retirement and CIK re-keying, pruning, the
flat/in-flight inputs, and validation through a temporary policy directory. Synthetic data only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError

from council.ledger.db import Ledger
from council.models.broker import Position
from council.policy import SLEEVE_FILE, STOCK_RANK_FILE
from council.stocks import sleeve_file as sf
from council.stocks.adopted import load_adopted
from tests.stocks import cli_support as cs

NOW = cs.NOW


def new(symbol: str, cik: int, role: str = "selected", *, rank: int | None = 1, sector: str = "BusEq",
        checked: datetime | None = NOW) -> sf.NewLine:
    return sf.NewLine(symbol=symbol, name=f"Test {symbol}", role=role, sector=sector, cik=sf.cik10(cik), rank=rank,
                      signal_ticker=sf.signal_ticker(symbol), etoro_symbol=sf.broker_symbol_guess(symbol),
                      eligibility_checked_at=checked)


def build(previous, chosen, **kw):
    return sf.build_sleeve(previous, chosen, quarter="2026Q3", rank_asof=date(2026, 8, 20),
                           rank_config_sha256="b" * 64, sleeve_weight=0.5, names_target=8, **kw)


def flat_of(**states):
    return lambda line: states.get(line.symbol)


def roles(sleeve) -> dict[str, str]:
    return {ln.symbol: ln.role for ln in sleeve.lines}


# ------------------------------------------------------------------------------------ calendar and identity


def test_quarters_and_the_rule_anchors():
    assert sf.quarter_of(date(2026, 11, 20)) == "2026Q4" and sf.quarter_of(date(2027, 3, 22)) == "2027Q1"
    assert sf.anchor_dates(2026) == [date(2026, 3, 20), date(2026, 5, 20), date(2026, 8, 20), date(2026, 11, 20)]
    assert sf.anchor_dates(2027)[0] == date(2027, 3, 22)                  # 03-20 is a Saturday
    assert sf.latest_anchor(date(2026, 9, 26)) == date(2026, 8, 20)
    assert sf.latest_anchor(date(2026, 3, 19)) == date(2025, 11, 20)
    assert sf.next_anchor(date(2026, 9, 26)) == date(2026, 11, 20)
    assert sf.next_anchor(date(2026, 11, 20)) == date(2027, 3, 22)
    assert tuple(load_adopted().rebalance_anchors) == sf.ANCHORS          # the studied anchors


def test_identity_spellings():
    assert sf.signal_ticker("BRK_B") == "BRK-B" and sf.broker_symbol_guess("BRK_B") == "BRK.B"
    assert sf.signal_ticker("F") == "F" and sf.cik10(320193) == "0000320193"
    for bad in (0, -1, 10**10):
        with pytest.raises(ValueError):
            sf.cik10(bad)


# ------------------------------------------------------------------------------------ writer


def test_the_writer_quotes_every_string_and_reads_back_equal():
    sleeve = cs.sleeve([cs.line("ON", 11), cs.line("YES", 12), cs.line("NO", 13, role="shortlist"),
                        cs.line("F", 14), cs.line("BRK_B", 15, role="retiring", aliases=("BRKB",))],
                       retired=[{"cik": sf.cik10(99), "symbol": "OFF", "name": "Test Off", "sector": "Shops",
                                 "from": "2025Q4", "to": "2026Q1", "vehicles": ["OFF"]}])
    text = sf.dump(sleeve, comment="two lines\nof comment")
    assert text.startswith("# two lines\n# of comment\n")
    assert sf.loads(text) == sleeve
    for token in ('symbol: "ON"', 'symbol: "YES"', 'symbol: "NO"', 'etoro_symbol: "BRK.B"', 'signal_ticker: "BRK-B"',
                  'aliases: ["BRKB"]', 'symbol: "OFF"', 'role: "retiring"', 'cik: "0000000011"'):
        assert token in text, token
    raw = yaml.safe_load(text)
    assert raw["lines"][0]["symbol"] == "ON"                             # a string, never True
    assert all(isinstance(v, str) for row in raw["lines"] for k, v in row.items()
               if k in ("symbol", "name", "role", "sector", "cik", "signal_ticker", "etoro_symbol"))


def test_an_unquoted_yaml_boolean_ticker_fails_the_schema():
    text = sf.dump(cs.sleeve([cs.line("ON", 11)])).replace('symbol: "ON"', "symbol: ON")
    with pytest.raises(ValidationError):
        sf.loads(text)


def test_stamps_are_whole_utc_seconds():
    odd = datetime(2026, 8, 20, 23, 5, 7, 123456, tzinfo=UTC) + timedelta()
    sleeve = cs.sleeve([cs.line("AAA", 1, checked=odd)])
    assert sleeve.lines[0].eligibility_checked_at == odd.replace(microsecond=0)
    assert '"2026-08-20T23:05:07Z"' in sf.dump(sleeve)


# ------------------------------------------------------------------------------------ diff and retirement


def test_the_first_quarter_is_all_new():
    s, d = build(None, [new("AAA", 1), new("BBB", 2), new("CCC", 3, "shortlist", rank=5)])
    assert roles(s) == {"AAA": "selected", "BBB": "selected", "CCC": "shortlist"}
    assert d.selected_in == ("AAA", "BBB") and d.new_lines == ("AAA", "BBB", "CCC")
    assert not (d.selected_out or d.retiring or d.pruned) and s.retired == ()
    assert d.summary() == "2 in, 0 out, 0 retiring, 0 pruned"


def test_diff_two_phase_retirement_and_the_registry():
    prev = cs.sleeve([
        cs.line("KEEP", 1), cs.line("DROPH", 2), cs.line("PROMO", 3, role="shortlist", rank=9),
        cs.line("DROPF", 4, role="shortlist", rank=10), cs.line("RETF", 5, role="retiring"),
        cs.line("RETH", 6, role="retiring"), cs.line("BACKR", 7, role="retiring"),
    ], retired=[{"cik": sf.cik10(8), "symbol": "ZED", "name": "Test Zed", "sector": "Shops", "from": "2025Q4",
                 "to": "2026Q1", "vehicles": ["ZED"]}])
    chosen = [new("KEEP", 1), new("PROMO", 3, rank=2), new("NEWB", 9, rank=3), new("BACKR", 7, rank=4),
              new("ZED", 8, "shortlist", rank=11, sector="Shops")]
    flat = flat_of(DROPH=False, DROPF=True, RETF=True, RETH=False)
    s, d = build(prev, chosen, flat=flat, first_quarter={sf.cik10(4): "2025Q3"})
    assert roles(s) == {"KEEP": "selected", "PROMO": "selected", "NEWB": "selected", "BACKR": "selected",
                        "ZED": "shortlist", "DROPH": "retiring", "RETH": "retiring"}
    assert d.selected_in == ("PROMO", "NEWB", "BACKR") and d.selected_out == ("DROPH",)
    assert d.new_lines == ("NEWB", "ZED") and d.returning == ("ZED",) and d.revived == ("BACKR",)
    assert d.retiring == ("DROPH",) and d.still_retiring == ("RETH",) and d.pruned == ("DROPF", "RETF")
    registry = {r.symbol: r for r in s.retired}
    assert set(registry) == {"DROPF", "RETF"}                           # ZED came back: its row is dropped
    assert (registry["DROPF"].from_, registry["DROPF"].to) == ("2025Q3", "2026Q2")
    assert (registry["RETF"].from_, registry["RETF"].to, registry["RETF"].vehicles) == ("2026Q2", "2026Q2", ("RETF",))
    by = {ln.symbol: ln for ln in s.lines}
    assert by["DROPH"].rank is None and by["DROPH"].eligibility_checked_at == NOW   # kept as it was
    assert [ln.role for ln in s.lines] == sorted([ln.role for ln in s.lines],
                                                 key=["selected", "shortlist", "retiring"].index)
    assert sf.loads(sf.dump(s)) == s


def test_nothing_is_pruned_without_a_snapshot_or_when_unknown_or_in_flight():
    prev = cs.sleeve([cs.line("GONE1", 1), cs.line("GONE2", 2, role="retiring", aliases=("OLDG",)),
                      cs.line("GONE3", 3, role="retiring")])
    s, d = build(prev, [new("NEWA", 9)])                                  # no snapshot at all
    assert d.pruned == () and set(d.retiring) == {"GONE1"} and set(d.still_retiring) == {"GONE2", "GONE3"}
    s, d = build(prev, [new("NEWA", 9)], flat=flat_of(GONE1=None, GONE2=True, GONE3=True), touched={"OLDG", "GONE3"})
    assert d.pruned == ()                                                 # unknown, and in flight (also by alias)
    assert roles(s)["GONE1"] == roles(s)["GONE2"] == roles(s)["GONE3"] == "retiring"


def test_a_company_is_rekeyed_by_cik_and_keeps_its_old_id_as_an_alias():
    prev = cs.sleeve([cs.line("FB", 42, aliases=("FBOOK",))])
    s, d = build(prev, [new("META", 42)])
    (meta,) = s.lines
    assert meta.symbol == "META" and meta.aliases == ("FBOOK", "FB") and d.rekeyed == (("FB", "META"),)
    assert d.selected_in == () and d.selected_out == () and d.new_lines == ()
    assert "re-keyed: FB -> META (old id kept as an alias)" in d.lines()
    back, _ = build(s.model_copy(update={"quarter": "2026Q3"}), [new("FB", 42)])   # renamed back: no self-alias
    assert back.lines[0].aliases == ("FBOOK", "META")


def test_a_recycled_ticker_needs_the_old_holder_flat_and_pruned():
    prev = cs.sleeve([cs.line("XYZ", 1)])
    with pytest.raises(sf.ProposalError, match="still held"):
        build(prev, [new("XYZ", 2)], flat=flat_of(XYZ=False))
    s, d = build(prev, [new("XYZ", 2)], flat=flat_of(XYZ=True))
    assert d.pruned == ("XYZ",) and s.lines[0].cik == sf.cik10(2) and s.retired[0].cik == sf.cik10(1)
    with pytest.raises(sf.ProposalError, match="chosen twice"):
        build(None, [new("AAA", 5), new("BBB", 5)])


def test_prune_moves_only_flat_untouched_retiring_lines():
    cur = cs.sleeve([cs.line("SEL", 1), cs.line("R1", 2, role="retiring"), cs.line("R2", 3, role="retiring"),
                     cs.line("R3", 4, role="retiring"), cs.line("R4", 5, role="retiring")], quarter="2026Q3")
    out, pruned = sf.prune(cur, flat=flat_of(SEL=True, R1=True, R2=False, R3=None, R4=True), touched={"R4"},
                           first_quarter={sf.cik10(2): "2026Q1"})
    assert pruned == ("R1",) and roles(out) == {"SEL": "selected", "R2": "retiring", "R3": "retiring",
                                                 "R4": "retiring"}
    assert (out.retired[0].symbol, out.retired[0].from_, out.retired[0].to) == ("R1", "2026Q1", "2026Q3")
    assert out.quarter == cur.quarter and out.rank_config_sha256 == cur.rank_config_sha256


# ------------------------------------------------------------------------------------ flat and in flight


def _pos(pid: int, iid: int, symbol: str) -> Position:
    return Position(position_id=pid, instrument_id=iid, symbol=symbol, is_buy=True, units=1.0, open_rate=10.0,
                    amount=10.0, settlement="real")


def test_flat_comes_only_from_a_fresh_snapshot_and_a_known_instrument():
    ids = {"AAA": 1, "BBB": 2}.get
    line = {ln.symbol: ln for ln in cs.sleeve([cs.line("AAA", 1), cs.line("BBB", 2), cs.line("CCC", 3)]).lines}
    assert sf.flat_from_positions(None, ids)(line["AAA"]) is None
    flat = sf.flat_from_positions([_pos(7, 1, "UNMAPPED_1"), _pos(8, 99, "CCC")], ids)
    assert flat(line["AAA"]) is False                                     # held, found by instrument id
    assert flat(line["BBB"]) is True                                      # known instrument, no position
    assert flat(line["CCC"]) is False                                     # held, found by symbol
    assert sf.flat_from_positions([], ids)(line["CCC"]) is None           # instrument never resolved


def test_in_flight_lines_come_from_pending_and_executing_decisions(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.migrate()
    ledger.create_decision(decision_id="d1", kind="rebalance", valid_until=NOW + timedelta(hours=1),
                           plan={"legs": [{"line": "RET1"}, {"line": "SPX"}]}, now=NOW)
    ledger.create_decision(decision_id="d2", kind="rebalance", valid_until=NOW + timedelta(hours=1),
                           plan={"legs": [{"line": "RET2"}]}, now=NOW)             # supersedes d1
    assert sf.in_flight_lines(ledger) == {"RET2"}
    ledger.transition("d2", "rejected", "test", actor="operator", now=NOW)
    assert sf.in_flight_lines(ledger) == set()


def test_the_rank_config_hash_names_and_orders_its_files():
    a = sf.rank_config_sha256([("stock-rank.yaml", b"x"), ("extra.yaml", b"y")])
    assert a != sf.rank_config_sha256([("extra.yaml", b"y"), ("stock-rank.yaml", b"x")])
    assert a != sf.rank_config_sha256([("stock-rank.yaml", b"xy"), ("extra.yaml", b"")])
    assert len(a) == 64


# ------------------------------------------------------------------------------------ validation


def proposal(**kw) -> dict[str, bytes]:
    lines = [cs.line(f"T{i}", 100 + i, rank=i + 1) for i in range(8)] + [cs.line("S0", 200, role="shortlist")]
    return {SLEEVE_FILE: sf.dump(cs.sleeve(lines, **kw)).encode()}


def test_validation_loads_every_validator_in_a_private_copy(tmp_path):
    base = cs.write_policy(tmp_path / "policy")
    before = cs.tree_digest(base)
    v = sf.validate(base, proposal(), workdir=tmp_path / "work")
    assert v.ok, v.errors
    assert [ln.symbol for ln in v.policy.universe.stock_lines()][:2] == ["T0", "T1"]
    assert any("no sleeve: section" in w for w in v.warnings)
    assert cs.tree_digest(base) == before and list((tmp_path / "work").iterdir()) == []


def test_validation_reports_weight_adoption_and_missing_settings(tmp_path):
    unbased = cs.write_policy(tmp_path / "p1", rebased=False)
    v = sf.validate(unbased, proposal(), workdir=tmp_path / "w")
    assert not v.ok and "reference_gross_max" in v.errors[0]
    base = cs.write_policy(tmp_path / "p2")
    wrong_n = proposal()
    wrong_n[SLEEVE_FILE] = wrong_n[SLEEVE_FILE].replace(b"names_target: 8", b"names_target: 9")
    v = sf.validate(base, wrong_n, workdir=tmp_path / "w")
    assert v.errors == ["names_target 9 is not the adopted N 8"]
    no_rank = cs.write_policy(tmp_path / "p3", stock_rank=False)
    v = sf.validate(no_rank, proposal(), workdir=tmp_path / "w")
    assert not v.ok and STOCK_RANK_FILE in v.errors[0]
    tampered = proposal()
    text = cs.rank_settings_text().replace("sector_cap: 3", "sector_cap: 4")
    v = sf.validate(base, {**tampered, STOCK_RANK_FILE: text.encode()}, workdir=tmp_path / "w")
    assert v.errors and all("sector_cap" in e for e in v.errors)


def test_validation_takes_go_live_drafts_from_an_overlay_but_never_a_sleeve(tmp_path):
    unbased = cs.write_policy(tmp_path / "p1", rebased=False)
    v = sf.validate(unbased, proposal(), workdir=tmp_path / "w", overlay_dir=cs.rebased_overlay(tmp_path))
    assert v.ok, v.errors
    bad = tmp_path / "bad-overlay"
    bad.mkdir()
    (bad / SLEEVE_FILE).write_bytes(proposal()[SLEEVE_FILE])
    with pytest.raises(sf.ProposalError):
        sf.validate(unbased, proposal(), workdir=tmp_path / "w", overlay_dir=bad)
    with pytest.raises(sf.ProposalError):
        sf.validate(unbased, {"../universe.yaml": b""}, workdir=tmp_path / "w")


# ------------------------------------------------------------------------------------ git (read-only)


def test_tag_state_and_first_quarters_read_git(tmp_path):
    repo = cs.make_repo(tmp_path)
    assert sf.tag_state(repo, "2026Q2") == "no_sleeve"
    first = cs.sleeve([cs.line("AAA", 1), cs.line("BBB", 2)], quarter="2026Q2")
    cs.commit_sleeve(repo, first, tagged=False)
    assert sf.tag_state(repo, "2026Q2") == "no_tag"
    cs.tag(repo, "stocks-2026Q2")
    assert sf.tag_state(repo, "2026Q2") == "tagged"
    second = cs.sleeve([cs.line("BBB", 2), cs.line("CCC", 3)], quarter="2026Q3")
    cs.commit_sleeve(repo, second, tagged=False)
    assert sf.tag_state(repo, "2026Q3") == "no_tag"
    cs.tag(repo, "stocks-2026Q3")
    edited = second.model_copy(update={"rank_config_sha256": "c" * 64})
    cs.commit_sleeve(repo, edited, tagged=False)
    assert sf.tag_state(repo, "2026Q3") == "untagged"
    assert sf.tagged_first_quarters(repo) == {sf.cik10(1): "2026Q2", sf.cik10(2): "2026Q2", sf.cik10(3): "2026Q3"}
