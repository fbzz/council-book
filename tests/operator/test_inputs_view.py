"""`council inputs <cycle>`: the operator's private view of exactly what each agent saw (T1v)."""

from __future__ import annotations

import stat
from datetime import date

import pytest
import typer
from typer.testing import CliRunner

from council.deliberation.capture import calls_path, licensed_path, load_inputs, load_licensed
from council.deliberation.reading import (
    Citation,
    NewsRead,
    citations_from_models,
    citations_from_record,
    counts,
    reading_list,
    reads_from_inputs,
)
from council.operator import inputs_cli
from council.operator.inputs_cli import (
    BANNER,
    ETORO_MARK,
    ledger_record,
    readings_for,
    register,
    render_html,
    render_text,
    write_view,
)

from .conftest import capture_cycle


def _app() -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def noop() -> None:  # a second command so typer keeps the sub-commands as a group
        pass

    register(app)
    return app


runner = CliRunner()


# ------------------------------------------------------------------------------- the guard
@pytest.mark.parametrize("argv", [
    ["inputs", "2026-10-01T1440Z"],
    ["inputs", "show", "2026-10-01T1440Z", "--role", "bear"],
    ["inputs", "2026-10-01T1440Z", "--html"],
    ["inputs", "verify", "2026-10-01T1440Z"],
    ["inputs", "prune", "--before", "2026-01-01"],
    ["purge-licensed", "--all"],
])
def test_every_private_command_refuses_an_agent_context(argv, captured, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("COUNCIL_ROLE", "operator")
    res = runner.invoke(_app(), argv)
    assert res.exit_code == 2, res.output
    assert "refused" in res.output and "CLAUDECODE is set" in res.output
    assert "DESK PACK" not in res.output
    assert calls_path(captured.root, captured.cycle_id).exists()        # nothing was purged


# --------------------------------------------------------------------------------- the text
def test_show_role_bear_prints_the_bears_exact_user_text(captured, as_operator):
    res = runner.invoke(_app(), ["inputs", captured.cycle_id, "--role", "bear"])
    assert res.exit_code == 0, res.output
    sink = captured.sink
    index = next(i for i, c in enumerate(sink.calls) if c.role == "bear")
    assert res.output.startswith(BANNER)
    assert sink.user_of(index) in res.output
    assert "=== bear:0:0" in res.output and "=== pm:0:0" not in res.output
    assert f"{ETORO_MARK}: eToro Licensed Content in this input:" in res.output


def test_show_filters_sections_system_and_replies(captured, as_operator):
    res = runner.invoke(_app(), ["inputs", "show", captured.cycle_id, "--role", "pm", "--replicate",
                                 "1", "--section", "transcript", "--system", "--replies"])
    assert res.exit_code == 0, res.output
    assert "=== pm:1:0" in res.output and "pm:0:0" not in res.output
    assert "--- section transcript (exact) ---" in res.output and "DEBATE TRANSCRIPT" in res.output
    assert "--- system prompt ---" in res.output and "--- replies (1) ---" in res.output
    assert "DESK PACK" not in res.output


def test_a_missing_capture_is_a_clean_error(state, as_operator):
    res = runner.invoke(_app(), ["inputs", "2026-10-02T0240Z"])
    assert res.exit_code == 1 and "no private capture" in res.output
    res = runner.invoke(_app(), ["inputs", "../../etc"])
    assert res.exit_code == 2


def test_rendered_text_marks_a_purged_input(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    text = render_text(inputs, None, role="news")
    assert "[licensed text purged]" in text
    assert "can no longer be rebuilt byte for byte" in text


# --------------------------------------------------------------------------------- the page
def test_the_html_page_is_private_and_marks_broker_items(captured, as_operator):
    res = runner.invoke(_app(), ["inputs", captured.cycle_id, "--html"])
    assert res.exit_code == 0, res.output
    page = captured.root / "inputs-view" / f"{captured.cycle_id}.html"
    assert str(page) in res.output
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    assert stat.S_IMODE(page.parent.stat().st_mode) == 0o700
    body = page.read_text()
    assert BANNER in body and ETORO_MARK in body
    assert "Chip export limits announced for advanced parts" in body
    assert body.count('id="sec-desk.full.lines"') == 1          # shared sections render once
    assert "Code desk versus full desk" in body and "News reading list" in body
    assert "<script" not in body


def test_the_page_can_be_written_for_one_role(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    lic = load_licensed(captured.root, captured.cycle_id)
    page = write_view(captured.root, captured.cycle_id, render_html(inputs, lic, role="news"), role="news")
    assert page.name == f"{captured.cycle_id}.news.html"
    body = page.read_text()
    assert "call-news:0:0" in body and "call-pm:0:0" not in body


def test_the_desk_diff_shows_what_the_full_desk_adds(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    diff = inputs_cli.desk_diff(inputs, load_licensed(captured.root, captured.cycle_id))
    added = [d for d in diff if d.startswith("+") and not d.startswith("+++")]
    assert any("K:news:1" in d for d in added)        # the analysts' cards are in the full desk only


# ----------------------------------------------------------------------------- reading list
def test_every_read_item_has_exactly_one_disposition(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    lic = load_licensed(captured.root, captured.cycle_id)
    readings = readings_for(inputs, lic, ledger_record(captured.root, captured.cycle_id))
    ids = [r.id for r in readings]
    assert sorted(ids) == sorted({"N:1a2b3c4d", "N:5e6f7a8b", "N:00c0ffee", "N:0000beef"})
    assert len(ids) == len(set(ids))
    by = {r.id: r for r in readings}
    assert by["N:1a2b3c4d"].disposition == "card" and by["N:1a2b3c4d"].cards == ("K:news:1",)
    assert by["N:1a2b3c4d"].why == "made into card K:news:1"
    assert by["N:00c0ffee"].disposition == "cited" and by["N:00c0ffee"].used
    assert by["N:00c0ffee"].why == "no card; cited by bear:c2"
    ignored = by["N:0000beef"]
    assert ignored.disposition == "not_cited" and not ignored.used
    assert ignored.read_by[0] == "news" and "pm" in ignored.read_by and "bear" in ignored.read_by
    assert ignored.why == f"read by {', '.join(ignored.read_by)}; no card, claim or decision cited it"
    assert ignored.title == "Shipping rates ease for a third week" and ignored.age == "-1.1h"
    assert counts(readings) == {"read": 4, "card": 2, "cited": 1, "not_cited": 1}


def test_the_reading_list_in_the_terminal(captured, as_operator):
    res = runner.invoke(_app(), ["inputs", captured.cycle_id, "--reading", "--role", "news"])
    assert res.exit_code == 0
    assert "news reading list: 4 read · 2 made into cards · 1 cited · 1 not used" in res.output
    assert "IGNORED (not_cited)" in res.output and "USED (card)" in res.output


def test_citations_come_from_the_councils_models(captured):
    res = captured.result
    cites = citations_from_models(cards=res.cards, debate=res.debate, pm=res.pm,
                                  single_agent=res.single_agent, macro=res.macro)
    kinds = {c.kind for c in cites}
    assert {"card", "claim", "rebuttal", "strongest", "decisive", "deviation"} <= kinds
    assert Citation("bear:c2", "claim", "bear", ("N:00c0ffee",)) in cites


def test_triage_adds_the_analysts_reason_to_an_ignored_item():
    items = [NewsRead(id="N:0000beef", title="t"), NewsRead(id="N:0000beef", title="dup")]
    out = reading_list(items, {"N:0000beef": ("news",)}, [],
                       triage={"N:0000beef": ("ignore", "off_universe", "")})
    assert len(out) == 1
    assert out[0].why == "read by news; no card, claim or decision cited it; news analyst: ignore (off_universe)"


def test_reads_survive_a_purged_licensed_file(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    items, read_by = reads_from_inputs(inputs, None)
    beef = next(i for i in items if i.id == "N:0000beef")
    assert not beef.available and beef.title == "[licensed text purged]"
    assert beef.age == "-1.1h" and read_by["N:0000beef"][0] == "news"


# ------------------------------------------------------------------------------- verify/prune
def test_verify_passes_and_catches_a_capture_missing_a_ledger_call(captured, as_operator):
    res = runner.invoke(_app(), ["inputs", "verify", captured.cycle_id])
    assert res.exit_code == 0, res.output
    assert "verified" in res.output
    from council.ledger.db import LEDGER_FILE, Ledger

    ledger = Ledger(captured.root / LEDGER_FILE)
    rec = ledger.get_cycle(captured.cycle_id)
    rec["calls"][0]["input_hash"] = "f" * 64
    ledger.record_cycle(rec)
    res = runner.invoke(_app(), ["inputs", "verify", captured.cycle_id])
    assert res.exit_code == 1 and "input not captured" in res.output


def test_prune_deletes_captures_before_a_date(state, as_operator):
    from datetime import UTC, datetime

    old = capture_cycle(state, slot=datetime(2026, 9, 1, 6, 40, tzinfo=UTC), ledger=False)
    new = capture_cycle(state, slot=datetime(2026, 10, 1, 14, 40, tzinfo=UTC), ledger=False)
    res = runner.invoke(_app(), ["inputs", "prune", "--before", "2026-09-15"])
    assert res.exit_code == 0 and "removed 2 capture file(s)" in res.output
    assert not calls_path(state, old.cycle_id).exists() and not licensed_path(state, old.cycle_id).exists()
    assert calls_path(state, new.cycle_id).exists()
    assert inputs_cli.prune(state, date(2020, 1, 1)) == 0


def test_rehearsal_captures_are_read_from_the_rehearsal_state(state, as_operator):
    capture_cycle(state / "rehearsal", ledger=False)
    res = runner.invoke(_app(), ["inputs", "2026-10-01T1440Z", "--rehearsal", "--role", "news"])
    assert res.exit_code == 0 and "=== news:0:0" in res.output


def test_the_reading_list_rebuilds_from_the_pack_without_a_capture(captured):
    """The redaction layer (and a cycle with no capture) gets the same list from the fact pack."""
    from council.deliberation.reading import reads_from_pack

    inputs = load_inputs(captured.root, captured.cycle_id)
    lic = load_licensed(captured.root, captured.cycle_id)
    from_capture = reads_from_inputs(inputs, lic)
    roles = list(dict.fromkeys(c.role for c in inputs.calls))
    from_pack = reads_from_pack(captured.pack, roles)
    assert from_pack == from_capture
    record = ledger_record(captured.root, captured.cycle_id)
    cites = citations_from_record(record)
    assert reading_list(*from_pack, cites) == reading_list(*from_capture, cites)
    # a council whose news analyst did not run read no summaries
    no_news, read_by = reads_from_pack(captured.pack, ["bull_open", "bear", "pm"])
    assert all(r.summary == "" for r in no_news)
    assert set(read_by.values()) == {("bull_open", "bear", "pm")}


def test_the_views_show_transport_retries(captured):
    inputs = load_inputs(captured.root, captured.cycle_id)
    lic = load_licensed(captured.root, captured.cycle_id)
    calls = [c.model_copy(update={"retries": ["first 1: http 503"]}) if c.role == "bear" else c
             for c in inputs.calls]
    inputs = inputs.model_copy(update={"calls": calls})
    text = render_text(inputs, lic, role="bear")
    assert "retried (the same messages resent): first 1: http 503" in text
    assert "retried (the same messages resent): first 1: http 503" in render_html(inputs, lic)
