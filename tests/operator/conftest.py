"""A captured stub cycle in the sandbox state dir, for the private viewer and purge tests."""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from council import paths
from council.deliberation.capture import InputSink, write_cycle_inputs
from council.deliberation.council import CouncilResult, run_council
from council.ledger.db import LEDGER_FILE, Ledger
from council.llm.prompts import PromptRegistry
from council.llm.stub import StubGateway
from council.models.cycle import CycleRecord
from council.models.facts import FactPack, NewsItem
from council.policy import default_policy

from ..council.factories import (
    REF_LEVELS,
    SLOT,
    bear_reply,
    build_bands,
    build_pack,
    build_ref,
    clip_enforce,
    stub_responses,
)

CANARY_TITLE = "Canarybird quarterly outlook stuns zebrafish analysts everywhere tonight"
CANARY_SUMMARY = "Canarybird summary text that must never outlive seven days in any private store"


@dataclass
class Captured:
    root: Path
    cycle_id: str
    result: CouncilResult
    sink: InputSink
    pack: FactPack


def cycle_id_for(slot: datetime) -> str:
    return f"{slot:%Y-%m-%dT%H%MZ}"


def capture_cycle(
    root: Path,
    *,
    slot: datetime = SLOT,
    canary: bool = False,
    ledger: bool = True,
    transcript: bool = True,
    responses: dict[str, Any] | None = None,
) -> Captured:
    """Run the stub council on a pack with four news items and write the private capture:
    N:1a2b3c4d and N:5e6f7a8b become news cards, N:00c0ffee is cited by the bear only and
    N:0000beef is read by everyone and cited by nobody. With `canary`, N:0000beef carries canary
    text and the news analyst copies it verbatim into a card claim."""
    base = build_pack()
    avail = slot - timedelta(hours=1)
    extra = [
        NewsItem(id="N:00c0ffee", title="Refiners flag a tighter autumn market", symbols=["OIL"],
                 published_at=avail, available_at=avail),
        NewsItem(id="N:0000beef",
                 title=CANARY_TITLE if canary else "Shipping rates ease for a third week",
                 summary=CANARY_SUMMARY if canary else "", symbols=[],
                 published_at=avail - timedelta(minutes=5), available_at=avail - timedelta(minutes=5)),
    ]
    pack = base.model_copy(update={
        "cycle_id": cycle_id_for(slot), "slot": slot, "created_at": slot,
        "news": [n.model_copy(update={"published_at": avail, "available_at": avail}) for n in base.news]
        + extra,
    })
    bear = bear_reply()
    bear["claims"] = [*bear["claims"], {"claim_id": "c2", "text": "Refiners are nervous.",
                                        "evidence_ids": ["N:00c0ffee"]}]
    replies = {**stub_responses(), "bear": bear, **(responses or {})}
    if canary:
        news = replies["news"]
        news["cards"][1]["claim"] = CANARY_TITLE
    sink = InputSink()
    policy = default_policy()
    lines = list(policy.universe.lines)

    async def instant(_s: float) -> None:
        return None

    result = asyncio.run(run_council(
        gw=StubGateway(replies), reg=PromptRegistry(), pack=pack, ref=build_ref(),
        bands=build_bands(), current_levels={**REF_LEVELS, "OIL": -0.25},
        cost_hints={ln.symbol: {"per_side_bps": 5.0, "carry_bps_day": 0.0} for ln in lines},
        lines=lines, policy=policy, enforce=clip_enforce, now=slot, sleep=instant,
        input_sink=sink,
    ))
    cycle_id = pack.cycle_id
    assert write_cycle_inputs(root, sink, cycle_id=cycle_id, captured_at=slot) == []
    if ledger:
        Ledger(root / LEDGER_FILE).record_cycle(CycleRecord(
            cycle_id=cycle_id, slot=slot, started_at=slot, status="dry_run", mode="dry_run",
            policy_sha="0" * 64, model="stub", cards=result.cards, macro=result.macro,
            debate=result.debate, pm=result.pm, single_agent=result.single_agent,
            calls=result.calls,
        ), now=slot)
    if transcript:
        folder = root / "transcripts"
        folder.mkdir(parents=True, exist_ok=True)
        with gzip.open(folder / f"{cycle_id}.json.gz", "wt") as fh:
            json.dump({"cycle_id": cycle_id, "raw": result.raw}, fh)
    return Captured(root, cycle_id, result, sink, pack)


@pytest.fixture
def state() -> Path:
    root = paths.state_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def captured(state) -> Captured:
    return capture_cycle(state)


@pytest.fixture
def as_operator(monkeypatch):
    """Pretend the guard passed (CliRunner has no TTY); the refusal tests do not use this."""
    from council.operator import guards

    monkeypatch.setattr(guards, "assert_current_process_is_operator", lambda: None)
