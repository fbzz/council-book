"""The cycle hook of transparency-v2 T5a-core: every cycle keeps, in its private ledger record, what the
per-line decision trail needs (structured drops, medoid fall-back lines, the bands before the
analysts' cards, the lines with new evidence, claim-to-line tags, publishable evidence values) plus a
summary of the trail, and `council why` rebuilds the full trail from it. The hook never stops a
cycle, and it never stores a value the public record would withhold."""

from __future__ import annotations

from datetime import timedelta

from council.cycle import run_cycle
from council.models.cycle import CycleRecord
from council.models.facts import Fact, FactPack
from council.operator import why
from council.publish import trail
from council.runtime import Sources
from tests.integration.test_end_to_end import NOW, SLOT, _ctx, _history, _no_events


def _run(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sources = Sources(history=_history, events=_no_events)
    return ctx, run_cycle(ctx)


def test_a_cycle_records_the_trail_privately(tmp_path):
    ctx, out = _run(tmp_path)
    raw = ctx.ledger.get_cycle(out.cycle_id)
    assert not any(f.startswith("trail_record_error") for f in raw["flags"])
    rec = CycleRecord.model_validate(raw)
    summary = raw["extras"]["trail"]
    assert summary and {row["line"] for row in summary} <= {ln.symbol for ln in ctx.policy.universe.lines}
    assert all(row["outcome"] in trail.OUTCOMES for row in summary)
    assert rec.evidence_values and set(rec.evidence_values) <= set(trail.cited_ids(rec))
    assert any(key.endswith(":c1") for key in rec.claim_lines)
    rebuilt = trail.summary(trail.record_trails(rec))
    key = lambda row: row["line"]  # noqa: E731
    assert sorted(rebuilt, key=key) == sorted(summary, key=key)   # the stored summary is the builder's


def test_why_reads_the_recorded_cycle_in_the_operator_terminal(tmp_path, monkeypatch):
    from council.operator import guards

    ctx, out = _run(tmp_path)
    monkeypatch.setattr(guards, "assert_current_process_is_operator", lambda: None)
    lines: list[str] = []
    code = why.run_why(out.cycle_id, state_dir=ctx.state_dir, journal_dir=tmp_path / "journal",
                       echo=lines.append)
    assert code == 0
    text = "\n".join(lines)
    assert "source: ledger" in text and "outcome:" in text


def test_a_failing_trail_hook_never_stops_the_cycle(tmp_path, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(trail, "record_trails", broken)
    ctx, out = _run(tmp_path)
    assert out.status == "on_time"
    raw = ctx.ledger.get_cycle(out.cycle_id)
    assert "trail_record_error:RuntimeError" in raw["flags"]
    assert raw["risk"] is not None


def test_evidence_values_keep_publishable_values_only():
    at = SLOT - timedelta(hours=2)
    facts = [
        Fact(id="F:NDX:mom10d", kind="market", symbol="NDX", value=-3.4, unit="pct", available_at=at,
             source="tiingo"),
        Fact(id="V:NDX:ewma5_60", kind="vol", symbol="NDX", value=2.31, unit="x", available_at=at,
             source="tiingo"),
        Fact(id="C:NDX:per_side_bps", kind="cost", symbol="NDX", value=6.0, unit="bps", available_at=at,
             source="costs:etoro_whatif"),
        Fact(id="F:NDX:dist_sma50", kind="market", symbol="NDX", value=4.1, unit="pct", available_at=at,
             source="unlabelled"),
    ]
    pack = FactPack(cycle_id="2026-10-01T1440Z", slot=SLOT, created_at=NOW, facts=facts, admitted=["NDX"],
                    states={})
    from council.policy import default_policy

    got = trail.evidence_values(pack, default_policy().universe.lines, [f.id for f in facts])
    assert got == {"F:NDX:mom10d": "-3.4%", "V:NDX:ewma5_60": "2.31x"}
