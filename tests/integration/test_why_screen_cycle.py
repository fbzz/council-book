"""M5-K end to end: a connected stub cycle proposes, the approval screen explains every leg from the
sealed public document before the nonce, and after the watch reveals the cycle the same screen's
trail blocks equal `council why` on the revealed document."""

from __future__ import annotations

import importlib
import re

from council.cycle import run_cycle
from council.operator import why
from council.publish import trail
from council.publish.gitops import Publisher
from council.watch import run_watch
from tests.integration.test_end_to_end import (  # noqa: F401  (pytest fixtures)
    API_KEY,
    READ_KEY,
    WRITE_KEY,
    _ctx,
    fake_broker,
    remote_clone,
)


def _blocks(text: list[str]) -> list[str]:
    return [t for t in text if t and not t.startswith("   leg ") and "· source:" not in t
            and t != why.SEALED_NOTE]


def test_the_approval_screen_explains_every_leg_and_matches_why_after_the_reveal(
        tmp_path, remote_clone, fake_broker, monkeypatch):  # noqa: F811
    from council.broker.etoro_read import EtoroReadClient
    from council.models.plan import Plan
    from council.operator.approve import ApprovalDeps, approve

    fake, fclock = fake_broker
    read = EtoroReadClient(API_KEY, READ_KEY, transport=fake.transport(), sleep=fclock.sleep)
    ctx = _ctx(tmp_path, broker=read, publisher=Publisher(remote_clone, push=True))
    out = run_cycle(ctx)
    assert out.decision_state == "proposed" and out.legs > 0, out.flags
    public_journal = remote_clone / "journal"
    assert why.journal_trails(public_journal, out.cycle_id) is None        # not revealed yet

    monkeypatch.setenv("COUNCIL_ROLE", "dev")
    monkeypatch.setenv("COUNCIL_MODE", "stub")
    etoro_write = importlib.import_module("council.broker.etoro_write")
    printed: list[str] = []
    at_nonce: list[str] = []

    def answer(prompt: str) -> str:
        at_nonce.extend(printed)
        return re.search(r"Type (\S+) to approve", prompt).group(1)

    deps = ApprovalDeps(
        ledger=ctx.ledger, policy=ctx.policy, read=read,
        write_factory=lambda: etoro_write.EtoroWriteClient(API_KEY, WRITE_KEY, transport=fake.transport()),
        state_dir=ctx.state_dir, input_fn=answer, print_fn=printed.append, now_fn=fclock.now,
        guard_fn=lambda: None, journal_dir=public_journal,
        executor_kwargs={"clock": fclock.now, "sleep": fclock.sleep, "_skip_guard_for_tests": True},
    )
    d = ctx.ledger.get_decision(out.decision_id)
    plan = Plan.model_validate(d.plan)
    approve(out.decision_id, deps)

    screen = "\n".join(at_nonce)
    assert "source: sealed public document, not yet revealed" in screen
    assert "WARNING: no trail" not in screen and why.NO_TRAIL not in screen
    for leg in plan.legs:
        assert f"   leg {leg.seq} {leg.kind} {leg.symbol} " in screen, leg

    w = run_watch(ctx)
    assert out.cycle_id in w.revealed
    revealed = why.journal_trails(public_journal, out.cycle_id, names=why.line_names())
    assert revealed is not None
    after: list[str] = []
    assert why.decision_why(d, plan, state_dir=ctx.state_dir, journal_dir=public_journal,
                            echo=after.append) == []
    lines = {leg.line or leg.symbol for leg in plan.legs}
    expected = [t for tl in revealed if tl.line in lines for t in trail.render_trail(tl)]
    assert _blocks(after) == expected
    why_text = trail.render_text(revealed)
    assert all(t in why_text for t in expected)
