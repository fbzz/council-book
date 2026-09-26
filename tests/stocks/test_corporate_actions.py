"""Corporate actions and the operator commands around a committed sleeve (design §3.5, §3.6, WP-D):

- a spin-off credit: detected (unknown position no leg of ours opened) -> ONE satellite-scoped
  blocker (the core still trades) -> `council stocks adopt` -> a `retiring` credited line -> after
  the commit and tag the position maps to that line, the blocker clears and the engine sells it;
  the missing stop on the credit is a reconcile warning, not a blocker;
- a cash takeover: the position vanished far above its stop -> `vanished_not_stop` (no cool-off) ->
  the instrument is gone -> adopt marks the line delisted (retiring, held at 0, `untradable`) and
  the engine holds it at 0 with no data and no crash;
- a ticker rename: adopt keeps the line id for the quarter, onboarding records an EXPLICIT alias so
  the new symbol resolves to the same instrument with no identity error, the ledger keeps its rows,
  and the next rank re-keys the line by CIK with the old id as an alias;
- onboard, prune, status and the doctor sample.

The broker is the FakeEtoro behind the READ client; nothing places, closes or modifies an order."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from council import paths
from council.broker.instruments import InstrumentIdentityChanged, InstrumentMap, resolve
from council.execution.planner import vehicle_to_line
from council.execution.reconcile import ReconcileResult
from council.ledger.db import Ledger
from council.models.plan import Leg
from council.policy import SLEEVE_FILE
from council.stocks import commands, corporate, sleeve_file
from council.stocks.corporate import Company, Observation
from tests.risk.test_sleeve_data_rules import NO_DATA, TODAY, evaluate, market, notes_of, units_of
from tests.stocks import cli_support as cs

NOW, Q = cs.NOW, cs.Q
CIKS = {"TSTA": 900_001, "TSTB": 900_002, "OLDN": 900_003, "SPIN": 900_050}


class Book:
    """A committed, tagged 2026Q3 sleeve (TSTA, TSTB, OLDN selected), their instruments resolved,
    and a READ client."""

    def __init__(self, tmp_path):
        self.state = paths.state_dir()
        self.repo = cs.make_repo(tmp_path)
        lines = [cs.line(s, CIKS[s], rank=i + 1) for i, s in enumerate(("TSTA", "TSTB", "OLDN"))]
        self.sleeve = cs.sleeve(lines, quarter=Q, rank_asof=cs.D)
        cs.commit_sleeve(self.repo, self.sleeve)
        self.broker = cs.broker(["TSTA", "TSTB", "OLDN"])
        cs.save_instruments(self.state, self.broker.ids)
        self.companies: dict[str, Company] = {}

    def lookup(self, *, symbol=None, cik=None):
        return self.companies.get(symbol or "")

    def policy(self):
        return commands.committed(self.state, self.repo).with_sleeve()

    def positions(self):
        imap = InstrumentMap.load(self.state / commands.INSTRUMENTS_FILE)
        return list(self.broker.read.portfolio(imap.symbol_for).positions)

    def adopt(self, iid: int, **kw) -> commands.Outcome:
        return commands.run_adopt(iid, state_dir=self.state, repo=self.repo, broker=self.broker.read,
                                  company_lookup=kw.pop("lookup", self.lookup), now=NOW, **kw)

    def commit_proposal(self, out: commands.Outcome) -> None:
        cs.commit_sleeve(self.repo, (out.directory / SLEEVE_FILE).read_text(), message="corporate action")

    def onboard(self) -> commands.Outcome:
        return commands.run_onboard(state_dir=self.state, repo=self.repo, broker=self.broker.read, now=NOW)


@pytest.fixture
def book(tmp_path):
    return Book(tmp_path)


def weights(pol, **stocks):
    units = units_of(pol)
    return {**{s: units[s] for s in TODAY}, **stocks}


# ------------------------------------------------------------------------------------ spin-off credit


def test_a_spin_off_credit_from_detection_to_the_sale(book):
    spin = book.broker.add("SPIN", price=20.0)
    book.broker.hold("TSTA")
    book.broker.hold("SPIN", units=3.0)                              # credited by the broker, not by us
    pol = book.policy()

    found = corporate.detect(book.positions(), pol, opened=set())
    assert [a.instrument_id for a in found.pending] == [spin]
    assert found.blockers == ("satellite:corporate_action_pending",)   # one code, no identifier in it
    assert found.alerts == (f"URGENT corporate action: an unknown position on instrument {spin}; the stock "
                            f"sleeve is held. Run `council stocks adopt {spin}`",)
    units = units_of(pol)
    d = evaluate(pol, current=dict(TODAY), held={s: 1.0 for s in TODAY}, blockers=list(found.blockers))
    for s in TODAY:                                                   # the core still re-bases
        assert d.final_w[s] == pytest.approx(units[s]), (s, d.hold_reasons)
    for s in ("TSTA", "TSTB", "OLDN"):                                # the satellite is held
        assert d.final_w[s] == 0.0 and "R20" in notes_of(d, s)

    book.companies["SPIN"] = Company(cik=sleeve_file.cik10(CIKS["SPIN"]), name="Spun Off Co", sector="Manuf")
    out = book.adopt(spin)
    assert out.ok, out.lines
    assert out.lines[0] == "credit: SPIN" and f"git tag -f stocks-{Q}" in " ".join(out.commands)
    proposed = sleeve_file.load(out.directory / SLEEVE_FILE)
    spin_line = next(ln for ln in proposed.lines if ln.symbol == "SPIN")
    assert (spin_line.role, spin_line.credited, spin_line.etoro_symbol) == ("retiring", "corporate_action", "SPIN")
    assert spin_line.eligibility_checked_at == NOW and spin_line.cik == sleeve_file.cik10(CIKS["SPIN"])
    assert [r["kind"] for r in corporate.load_records(book.state)] == ["credit"]
    assert book.broker.writes() == 0

    book.commit_proposal(out)                                         # the human commits and tags
    onboard = book.onboard()
    assert onboard.ok, onboard.lines
    assert "ok   SPIN (retiring)" in onboard.lines
    pol = book.policy()
    positions = book.positions()
    assert {p.symbol for p in positions} == {"TSTA", "SPIN"}          # the credit now maps to its line
    assert corporate.detect(positions, pol, opened=set()).blockers == ()
    # a credit far below the rule's drift threshold (2% of NAV) is still sold in full: a zero-target
    # stock line exits whatever its drift (design §3.5, §17.2 #4), so it can be pruned afterwards
    for credit in (0.03, 0.004):
        current = weights(pol, TSTA=units_of(pol)["TSTA"], SPIN=credit)
        d = evaluate(pol, current=current, held={s: 1.0 for s in TODAY})
        assert d.final_w["SPIN"] == 0.0, (credit, d.hold_reasons)     # sold: a reference-origin full exit
        assert d.passed, [(c.rule_id, c.name) for c in d.checks if not c.passed]

    result = ReconcileResult(ok=False, drift=0.0, drift_max=0.02, missing_sl=["SPIN"])
    fixed, warnings = corporate.reconcile_credited(result, positions, pol)
    assert fixed.ok and fixed.missing_sl == [] and warnings == ["credited_no_sl:SPIN"]
    other = ReconcileResult(ok=False, drift=0.0, drift_max=0.02, missing_sl=["SPIN", "TSTA"])
    still, _ = corporate.reconcile_credited(other, positions, pol)
    assert not still.ok and still.missing_sl == ["TSTA"]              # every other missing stop still blocks


def test_a_pending_credit_holds_the_satellite_at_reconcile_never_the_whole_book(book):
    spin = book.broker.add("SPIN", price=20.0)
    ours = book.broker.hold("TSTA", sl_rate=80.0)
    book.broker.hold("SPIN", units=3.0)                              # credited, not adopted yet: UNMAPPED
    pol, positions = book.policy(), book.positions()
    unmapped = next(p.symbol for p in positions if p.instrument_id == spin)
    assert unmapped.startswith("UNMAPPED_")
    raw = ReconcileResult(ok=False, drift=0.0, drift_max=0.02, missing_sl=[unmapped], unknown_positions=[unmapped])
    fixed, warnings, blockers = corporate.reconcile_corporate(raw, positions, pol, opened={ours})
    assert fixed.ok and fixed.protected and warnings == []
    assert blockers == ["satellite:corporate_action_pending"]
    # an unknown position OUR leg opened, or another missing stop, still blocks
    every = {p.position_id for p in positions}
    mine, _, none = corporate.reconcile_corporate(raw, positions, pol, opened=every)
    assert not mine.ok and mine.unknown_positions == [unmapped] and none == []
    other = ReconcileResult(ok=False, drift=0.0, drift_max=0.02, missing_sl=[unmapped, "TSTA"],
                            unknown_positions=[unmapped])
    still, _, _ = corporate.reconcile_corporate(other, positions, pol, opened={ours})
    assert not still.ok and still.missing_sl == ["TSTA"] and still.unknown_positions == []


def test_our_own_open_is_never_a_corporate_action_and_retired_vehicles_are_named(book):
    stray = book.broker.add("STRAY")
    pid = book.broker.hold("STRAY")
    pol = book.policy()
    assert corporate.detect(book.positions(), pol, opened={pid}).blockers == ()
    retired = cs.sleeve([cs.line("TSTA", CIKS["TSTA"])], quarter=Q, rank_asof=cs.D, retired=[
        {"cik": sleeve_file.cik10(7), "symbol": "STRAY", "name": "Test Stray", "sector": "Shops", "from": "2026Q2",
         "to": "2026Q2", "vehicles": ["STRAY"]}])
    cs.commit_sleeve(book.repo, retired)
    cs.save_instruments(book.state, {"STRAY": stray})
    found = corporate.detect(book.positions(), book.policy(), opened=set())
    assert found.blockers == ("satellite:retired_line_held:STRAY",) and found.pending == ()


def test_adopt_refuses_what_the_facts_do_not_support(book):
    spin = book.broker.add("SPIN")
    with pytest.raises(commands.StocksError, match="nothing to adopt"):
        book.adopt(spin)                                              # no position, no line owns it
    book.broker.hold("SPIN")
    with pytest.raises(commands.StocksError, match="pass --cik"):
        book.adopt(spin)                                              # SEC does not know the ticker
    with pytest.raises(commands.StocksError, match="do not support a rename"):
        book.adopt(spin, kind="rename")
    book.broker.fake.instruments[spin].row["allowClosePosition"] = False
    book.companies["SPIN"] = Company(cik=sleeve_file.cik10(CIKS["SPIN"]), name="Spun", sector="Manuf")
    with pytest.raises(commands.StocksError, match="cannot be closed"):
        book.adopt(spin)
    assert not (book.state / commands.PROPOSALS).exists() or not any((book.state / commands.PROPOSALS).rglob("*.yaml"))


# ------------------------------------------------------------------------------------ cash takeover


def test_a_cash_takeover_is_not_a_stop_and_the_line_is_held_at_zero(book):
    pol = book.policy()
    seen = {501: Observation(symbol="TSTB", sl_rate=80.0, bid=131.0),    # taken over at a premium
            502: Observation(symbol="TSTA", sl_rate=80.0, bid=81.0),     # next to its stop
            503: Observation(symbol="OLDN", sl_rate=None, bid=None),     # no data: ambiguous
            504: Observation(symbol="EQQQ.L", sl_rate=80.0, bid=131.0)}  # a core line keeps today's rule
    out = corporate.classify_vanished_positions(seen, live_ids=[], ours=[], policy=pol,
                                                sigma_4h={"TSTB": 0.02, "TSTA": 0.02, "OLDN": 0.02})
    outcome = {v.line: v.outcome for v in out}
    assert outcome == {"TSTB": "vanished_not_stop", "TSTA": "stop_hit", "OLDN": "stop_hit", "NDX": "stop_hit"}
    assert corporate.classify_vanished(last_bid=131.0, sl_rate=80.0, sigma_4h=0.02, closed_by_sl=True) == "stop_hit"
    # a gap THROUGH the stop is the stop firing, however far below it the last bid was (long only)
    for bid in (80.0, 79.0, 40.0):
        assert corporate.classify_vanished(last_bid=bid, sl_rate=80.0, sigma_4h=0.02) == "stop_hit", bid
    assert corporate.classify_vanished(last_bid=83.1, sl_rate=80.0, sigma_4h=0.02) == "stop_hit"
    assert corporate.classify_vanished(last_bid=84.0, sl_rate=80.0, sigma_4h=0.02) == "vanished_not_stop"
    (alert,) = corporate.vanished_alerts(out)
    assert alert.startswith("URGENT vanished_not_stop:TSTB") and "council stocks adopt" in alert
    assert corporate.classify_vanished_positions(seen, live_ids=[501, 502, 503, 504], ours=[], policy=pol,
                                                 sigma_4h={}) == []

    gone = book.broker.ids["TSTB"]
    book.broker.fake.instruments.pop(gone)                            # the listing disappeared

    def sec_down(**_kw):
        raise TimeoutError("SEC unreachable")

    out = book.adopt(gone, lookup=sec_down)                           # a delisting needs no SEC answer
    assert out.ok and out.lines[0] == "delisted: TSTB"
    assert any("SEC lookup failed (TimeoutError)" in line for line in out.lines)
    proposed = sleeve_file.load(out.directory / SLEEVE_FILE)
    assert {ln.symbol: ln.role for ln in proposed.lines}["TSTB"] == "retiring"
    book.commit_proposal(out)
    pol = book.policy()
    assert corporate.untradable_flags(corporate.load_records(book.state), pol) == ["untradable:TSTB"]
    onboard = book.onboard()
    assert "ok   TSTB (delisted: held at 0 until the next rank removes it)" in onboard.lines and onboard.ok
    d = evaluate(pol, current=weights(pol), held={s: 1.0 for s in TODAY}, states=market(pol, TSTB=NO_DATA))
    assert d.final_w["TSTB"] == 0.0 and d.passed                      # held at 0, no data, no crash
    prune = commands.run_prune(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)
    assert prune.ok and "pruned: TSTB" in prune.lines                 # flat: removed at the next edit
    status = commands.run_status(state_dir=book.state, repo=book.repo, broker=None, now=NOW)
    assert "corporate action delisted TSTB: proposed" in status.lines and "untradable:TSTB" in status.lines


# ------------------------------------------------------------------------------------ ticker rename


def _filled_leg(ledger: Ledger, iid: int) -> None:
    leg = Leg(seq=1, kind="open", symbol="OLDN", line="OLDN", instrument_id=iid, direction="long",
              settlement="real", weight_before=0.0, weight_after=0.0625, risk_increasing=True, units=5.0,
              amount_usd=500.0, sl_rate=80.0, origin="reference")
    ledger.create_decision(decision_id="d-old", kind="rebalance", valid_until=NOW - timedelta(days=9),
                           now=NOW - timedelta(days=10))
    ledger.insert_legs("d-old", [leg], now=NOW - timedelta(days=10))
    ledger.update_leg("d-old", 1, state="submitting", now=NOW - timedelta(days=10))
    ledger.update_leg("d-old", 1, state="filled", resolved_at=NOW - timedelta(days=10), now=NOW - timedelta(days=10))


def test_a_ticker_rename_keeps_the_line_through_an_explicit_alias(book):
    iid = book.broker.ids["OLDN"]
    ledger = Ledger(book.state / commands.LEDGER_FILE)
    ledger.migrate()
    _filled_leg(ledger, iid)
    book.broker.hold("OLDN")
    inst = book.broker.fake.instruments[iid]                           # the broker renames the listing
    inst.symbol, inst.row = "NEWN", cs.stock_row("NEWN", iid)
    before = book.onboard()
    assert not before.ok and "BAD  OLDN: OLDN unresolved (not found)" in before.lines
    with pytest.raises(InstrumentIdentityChanged):
        resolve(book.broker.read, ["NEWN"], now=NOW, path=book.state / commands.INSTRUMENTS_FILE)

    out = book.adopt(iid)
    assert out.ok and out.lines[0] == "rename: OLDN", out.lines
    edited = next(ln for ln in sleeve_file.load(out.directory / SLEEVE_FILE).lines if ln.cik == sleeve_file.cik10(
        CIKS["OLDN"]))
    assert (edited.symbol, edited.etoro_symbol, edited.signal_ticker, edited.role) == ("OLDN", "NEWN", "NEWN",
                                                                                         "selected")
    (record,) = corporate.load_records(book.state)
    assert record["same_instrument"] and (record["old_symbol"], record["new_symbol"]) == ("OLDN", "NEWN")

    book.commit_proposal(out)
    onboard = book.onboard()
    assert onboard.ok, onboard.lines
    assert "ok   alias OLDN -> NEWN recorded (explicit rename)" in onboard.lines
    imap = InstrumentMap.load(book.state / commands.INSTRUMENTS_FILE)
    assert imap.symbol_for(iid) == "NEWN" and imap.get("OLDN") == iid and imap.aliases["OLDN"].new == "NEWN"
    assert corporate.load_records(book.state)[0]["status"] == "applied"
    pol = book.policy()
    assert vehicle_to_line(pol.universe)["NEWN"] == "OLDN"               # same line id, new vehicle
    assert [p.symbol for p in book.positions()] == ["NEWN"]
    assert corporate.detect(book.positions(), pol, opened=set()).blockers == ()
    assert ledger.last_change("OLDN") == NOW - timedelta(days=10)       # the ledger keeps its history
    assert book.onboard().ok                                           # idempotent: the alias is not re-added

    # the next quarter's rank re-keys the company by CIK and keeps the old id as an alias
    nxt, diff = sleeve_file.build_sleeve(
        commands.committed(book.state, book.repo).sleeve,
        [sleeve_file.NewLine(symbol="NEWN", name="Test NEWN", role="selected", sector="BusEq",
                             cik=sleeve_file.cik10(CIKS["OLDN"]), rank=1, signal_ticker="NEWN", etoro_symbol="NEWN",
                             eligibility_checked_at=NOW)],
        quarter="2026Q4", rank_asof=date(2026, 11, 20), rank_config_sha256="d" * 64, sleeve_weight=0.5,
        names_target=8)
    newn = next(ln for ln in nxt.lines if ln.symbol == "NEWN")
    assert newn.aliases == ("OLDN",) and diff.rekeyed == (("OLDN", "NEWN"),)


# ------------------------------------------------------------------------------------ onboard, prune, status


def test_onboard_needs_the_tag_and_refuses_unchecked_lines(book):
    assert book.onboard().ok
    unchecked = cs.sleeve([cs.line("TSTA", CIKS["TSTA"]), cs.line("TSTB", CIKS["TSTB"], checked=None)],
                          quarter=Q, rank_asof=cs.D)
    cs.commit_sleeve(book.repo, unchecked, tagged=False)
    out = book.onboard()
    assert not out.ok
    assert out.lines[0].startswith(f"BAD  tag stocks-{Q}: untagged")
    assert "BAD  stock_eligibility_unchecked:TSTB (live runs refuse it: re-rank with the broker gate)" in out.lines
    book.broker.fake.instruments[book.broker.ids["TSTA"]].row["requiresW8Ben"] = True
    assert "BAD  TSTA: gate fails (requires_w8ben)" in book.onboard().lines
    assert book.broker.writes() == 0


def test_onboard_refuses_an_identity_change_and_writes_nothing(book):
    iid = book.broker.ids["TSTA"]
    before = (book.state / commands.INSTRUMENTS_FILE).read_text()
    book.broker.fake.instruments.pop(iid)
    book.broker.fake.add_instrument("TSTA", 77_777, bid=100.0, ask=100.1, row=cs.stock_row("TSTA", 77_777))
    with pytest.raises(commands.StocksError, match="council stocks adopt"):
        book.onboard()
    assert (book.state / commands.INSTRUMENTS_FILE).read_text() == before


def test_prune_needs_a_fresh_snapshot_and_moves_only_flat_untouched_lines(book):
    retiring = cs.sleeve([cs.line("TSTA", CIKS["TSTA"]), cs.line("TSTB", CIKS["TSTB"], role="retiring"),
                          cs.line("OLDN", CIKS["OLDN"], role="retiring")], quarter=Q, rank_asof=cs.D)
    cs.commit_sleeve(book.repo, retiring)
    book.broker.hold("OLDN")
    with pytest.raises(commands.StocksError, match="fresh READ snapshot"):
        commands.run_prune(state_dir=book.state, repo=book.repo, broker=None, now=NOW)
    out = commands.run_prune(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)
    assert out.ok and "pruned: TSTB" in out.lines and "still retiring: OLDN" in out.lines
    pruned = sleeve_file.load(out.directory / SLEEVE_FILE)
    assert [r.symbol for r in pruned.retired] == ["TSTB"] and pruned.retired[0].to == Q
    assert out.commands[0].startswith("cp ") and any(c.startswith(f"git tag -f stocks-{Q}") for c in out.commands)
    head = cs.git(book.repo, "rev-parse", "HEAD")
    assert cs.git(book.repo, "status", "--porcelain") == "" and cs.git(book.repo, "rev-parse", "HEAD") == head
    book.broker.fake.inject("GET", "/api/v1/trading/info/real/pnl", 500, times=10)
    with pytest.raises(commands.StocksError, match="portfolio read failed"):
        commands.run_prune(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)


def test_status_reports_the_tag_roles_and_retiring_flatness(book):
    retiring = cs.sleeve([cs.line("TSTA", CIKS["TSTA"]), cs.line("TSTB", CIKS["TSTB"], role="retiring"),
                          cs.line("OLDN", CIKS["OLDN"], role="shortlist", checked=None)], quarter=Q, rank_asof=cs.D)
    cs.commit_sleeve(book.repo, retiring)
    out = commands.run_status(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)
    text = "\n".join(out.lines)
    assert not out.ok                                                  # an unchecked line
    for want in (f"quarter {Q} (rank date 2026-08-20); tag stocks-{Q}: tagged", "selected: TSTA",
                 "shortlist: OLDN", "retiring: TSTB", "retiring TSTB: flat (prunable)",
                 "BAD  stock_eligibility_unchecked:OLDN (live runs refuse it)", "alpaca budget: 0/",
                 "ledger: none yet", "next rank anchor: 2026-11-20"):
        assert want in text, want


def test_status_flags_a_corporate_action_whatever_the_roles(book):
    spin = book.broker.add("SPIN")
    book.broker.hold("SPIN")
    out = commands.run_status(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)
    assert not out.ok and "BAD  satellite:corporate_action_pending" in out.lines
    assert any(line.startswith("URGENT corporate action") and f"council stocks adopt {spin}" in line
               for line in out.lines)


def test_status_reads_the_anchors_and_the_fee_drag_from_the_ledger(book):
    ledger = Ledger(book.state / commands.LEDGER_FILE)
    ledger.migrate()
    ledger.set_runtime(f"sleeve_anchor:{Q}", {"TSTB": {"level": 0.0, "decision_id": "d1"}}, now=NOW)
    out = commands.run_status(state_dir=book.state, repo=book.repo, broker=None, now=NOW)
    assert out.ok and "council anchors this quarter: TSTB" in out.lines
    assert "real-account extra fee drag (lifetime): 0.000% of equity" in out.lines


# ------------------------------------------------------------------------------------ doctor


def test_the_doctor_stock_sample(book, tmp_path):
    rows = commands.doctor_stock_sample(state_dir=book.state, repo=book.repo, broker=book.broker.read, now=NOW)
    assert rows[0] == ("stock lines eligibility-checked", True, "all")
    assert [(n, g) for n, g, _ in rows[1:]] == [("stock gate TSTA", True), ("stock gate TSTB", True),
                                                ("stock gate OLDN", True)]
    bare = cs.make_repo(tmp_path, name="bare")
    b = cs.broker(["AAPL"])
    rows = commands.doctor_stock_sample(state_dir=book.state, repo=bare, broker=b.read, now=NOW)
    assert rows == [("stock gate AAPL", True, "ok"), ("stock gate MSFT", False, "not_found"),
                    ("stock gate JNJ", False, "not_found")]
    assert all(not any(ch.isdigit() for ch in detail) for _, _, detail in rows)
    broken = commands.doctor_stock_sample(state_dir=book.state, repo=tmp_path / "not-a-repo", broker=b.read, now=NOW)
    assert broken[0][0] == "stock sample" and broken[0][1] is False


# ------------------------------------------------------------------------------------ CLI


@pytest.fixture
def cli(book, monkeypatch):
    from typer.testing import CliRunner

    from council.cli import app

    reads = {"client": book.broker.read}
    monkeypatch.setattr("council.context.read_broker", lambda settings: reads["client"])
    monkeypatch.setattr(commands, "default_repo", lambda: book.repo)
    monkeypatch.setattr(commands, "sec_company_lookup", lambda: book.lookup)

    def invoke(*args: str):
        return CliRunner().invoke(app, list(args))

    invoke.reads = reads
    return invoke


def test_the_operator_commands_through_the_cli(cli, book):
    res = cli("stocks", "status")
    assert res.exit_code == 0 and f"tag stocks-{Q}: tagged" in res.output, res.output
    res = cli("stocks", "onboard")
    assert res.exit_code == 0 and "ok   TSTA (selected)" in res.output, res.output
    res = cli("stocks", "prune")
    assert res.exit_code == 0 and "pruned: none" in res.output
    spin = book.broker.add("SPIN")
    res = cli("stocks", "adopt", str(spin))
    assert res.exit_code == 2 and "refused: nothing to adopt" in res.output
    cli.reads["client"] = None
    for args in (("stocks", "onboard"), ("stocks", "prune"), ("stocks", "adopt", "1"),
                 ("stocks", "status", "--live-read")):
        res = cli(*args)
        assert res.exit_code == 2 and "refused: no READ token" in res.output, args
    assert book.broker.writes() == 0


def test_doctor_live_read_runs_the_stock_sample(cli, book, monkeypatch):
    import subprocess

    real = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] in ("security", "ollama", "launchctl"):      # no Keychain, no daemons in tests
            return subprocess.CompletedProcess(cmd, 0, stdout="deepseek-v4.1-flash:cloud", stderr="")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = cli("doctor", "--live-read")
    assert "ok   stock lines eligibility-checked  — all" in res.output, res.output
    for symbol in ("TSTA", "TSTB", "OLDN"):
        assert f"ok   stock gate {symbol}  — ok" in res.output
    book.broker.fake.instruments[book.broker.ids["TSTB"]].row["requiresW8Ben"] = True
    res = cli("doctor", "--live-read")
    assert res.exit_code == 1 and "BAD  stock gate TSTB  — requires_w8ben" in res.output
