"""Synthetic private cycle records for the publish tests. Every private value below is a CANARY:
it must never appear in anything public."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from council.models.cards import EvidenceCard
from council.models.cycle import CycleRecord, Debate, PMReplicate, RoleCall
from council.models.debate import AdvocateCase, BearCase, Claim, Rebuttal
from council.models.facts import Fact, FactPack, MarketState, NewsItem
from council.models.plan import Leg, Plan
from council.models.pm import DecisiveFact, Deviation, PMDecision
from council.models.reference import ReferenceBook, ReferenceEntry
from council.models.risk import Band, RiskCheck, RiskDecision

SLOT = datetime(2026, 10, 1, 14, 40, tzinfo=UTC)
CYCLE_ID = "2026-10-01T1440Z"

# canaries: private values that must never be published
PRIVATE_NAV = 1234.56
PRIVATE_AMOUNT = 187.43
PRIVATE_UNITS = 3.2109
PRIVATE_SL_RATE = 412.77
PRIVATE_POSITION_ID = 2951234567
PRIVATE_INSTRUMENT_ID = 100123
PRIVATE_DECISION_ID = "5b0f2c4e-9a1d-4c3b-8e7f-0123456789ab"
PRIVATE_NOTE = "equity 1234.56 USD on the main book"
PRIVATE_FINGERPRINT = "trend:NDX=up|SEMIS=up|cards:K:vol:1|events:none|admitted:NDX,SEMIS"
APPROVED_AT = datetime(2026, 10, 1, 15, 7, 13, tzinfo=UTC)
CANARIES = (PRIVATE_NAV, PRIVATE_AMOUNT, PRIVATE_UNITS, PRIVATE_SL_RATE, str(PRIVATE_POSITION_ID),
            str(PRIVATE_INSTRUMENT_ID))

LICENSED_TITLE = "Chipmakers rally as the largest foundry lifts guidance"
LICENSED_SUMMARY = (
    "Shares of chip designers climbed on Tuesday after the largest contract foundry raised its "
    "full year revenue guidance on strong demand for accelerator chips"
)
COPIED_CLAIM = "The largest contract foundry raised its full year revenue guidance on strong demand"

SHA_A = "ab" * 32
SHA_B = "cd" * 32
SHA_C = "0e" * 32

LEVELS = {"NDX": 1.0, "SEMIS": 1.0, "SPX": 1.0, "GOLD": 0.5, "BTC": 1.0, "ETH": 0.5, "OIL": 0.0,
          "EURUSD": 0.0, "GBPUSD": 0.0}
UNIT = {"NDX": 0.35, "SEMIS": 0.15, "SPX": 0.15, "GOLD": 0.12, "BTC": 0.13, "ETH": 0.05, "OIL": 0.10,
        "EURUSD": 0.25, "GBPUSD": 0.25}
TRENDS = {"NDX": "up", "SEMIS": "up", "SPX": "up", "GOLD": "mixed", "BTC": "up", "ETH": "mixed",
          "OIL": "down", "EURUSD": "mixed", "GBPUSD": "mixed"}


def make_pack() -> FactPack:
    facts = [
        Fact(id="F:NDX:dist_sma200_pct", kind="market", symbol="NDX", value=6.2, unit="pct", available_at=SLOT, source="tiingo"),
        Fact(id="V:SEMIS:vol_ratio", kind="vol", symbol="SEMIS", value=2.3, unit="ratio", available_at=SLOT, source="code"),
        Fact(id="C:SEMIS:bps_side", kind="cost", symbol="SEMIS", value=5.0, unit="bps", available_at=SLOT, source="code"),
        Fact(id="M:DGS10@2026-09-30", kind="macro", value=4.11, unit="pct", available_at=SLOT, source="fred"),
        Fact(id="M:DGS10.chg20@2026-09-30", kind="macro", value=-12.5, unit="bps", available_at=SLOT, source="fred"),
        Fact(id="M:VIXCLS@2026-09-30", kind="macro", value=17.3, unit="x", available_at=SLOT, source="fred"),
    ]
    news = [NewsItem(id="N:1a2b3c4d", title=LICENSED_TITLE, summary=LICENSED_SUMMARY, symbols=["SEMIS"],
                     published_at=SLOT - timedelta(hours=3), available_at=SLOT - timedelta(hours=3))]
    states = {s: MarketState(symbol=s, asset_class="index", trend=TRENDS[s]) for s in LEVELS}
    return FactPack(cycle_id=CYCLE_ID, slot=SLOT, created_at=SLOT, admitted=list(LEVELS), states=states,
                    facts=facts, news=news).sealed()


def _decision(level: float, sided: str = "bear") -> PMDecision:
    return PMDecision(
        deviations=[Deviation(symbol="SEMIS", level=level, direction="cut", evidence_ids=["K:vol:1", "V:SEMIS:vol_ratio"],
                              reason="Vol shock on semis; cut to half until the ratio normalises")],
        decisive_fact=DecisiveFact(text="Semis 5d/60d volatility ratio is 2.3", evidence_id="V:SEMIS:vol_ratio"),
        sided_with=sided,
    )


def make_record(**overrides) -> CycleRecord:
    reference = ReferenceBook(
        cycle_id=CYCLE_ID,
        entries={
            s: ReferenceEntry(symbol=s, sleeve="core", asset_class="index", in_reference=s not in ("OIL", "EURUSD", "GBPUSD"),
                              trend=TRENDS[s], level_ref=LEVELS[s], unit_weight=UNIT[s], weight_ref=LEVELS[s] * UNIT[s],
                              sigma_ann=0.2, stop_distance=0.1)
            for s in LEVELS
        },
        k=1.0, target_vol=0.22, ex_ante_vol=0.18, gross=0.93,
    )
    enforced = {**LEVELS, "SEMIS": 0.5}
    record = CycleRecord(
        cycle_id=CYCLE_ID,
        slot=SLOT,
        started_at=SLOT + timedelta(minutes=12),
        finished_at=SLOT + timedelta(minutes=19),
        status="late",
        late_by_s=720,
        mode="live",
        input_hash=SHA_A,
        policy_sha=SHA_B,
        prompt_manifest_sha=SHA_C,
        model="deepseek-v4-flash:cloud",
        model_digest="6ca9e29c41de",
        think=False,
        why_we_met=["scheduled", "vol_shock:SEMIS"],
        kill_state="NORMAL",
        reference=reference,
        cards=[
            EvidenceCard(card_id="K:news:1", role="news", scope=["SEMIS", "market"], card_type="news_material",
                         direction="risk_up", claim="Foundry guidance raised; chip demand strong (paraphrase)",
                         evidence_ids=["N:1a2b3c4d", "F:NDX:dist_sma200_pct"], horizon_days=5,
                         falsifier="Semis close below the 50-day average within 5 days"),
            EvidenceCard(card_id="K:news:2", role="news", scope=["SEMIS"], card_type="news_context", direction="neutral",
                         claim=COPIED_CLAIM, evidence_ids=["N:1a2b3c4d"], horizon_days=1),
            EvidenceCard(card_id="K:vol:1", role="vol", scope=["SEMIS"], card_type="vol_shock", direction="risk_down",
                         claim="Semis volatility ratio 2.3 (EWMA5/EWMA60)", evidence_ids=["V:SEMIS:vol_ratio"],
                         horizon_days=5, qualifying=True),
            EvidenceCard(card_id="K:macro:1", role="macro", scope=["market"], card_type="macro_context", direction="neutral",
                         claim="10y yield steady; implied vol calm. See https://example.com/x and @someone",
                         evidence_ids=["M:DGS10@2026-09-30", "M:VIXCLS@2026-09-30", "M:DGS10.chg20@2026-09-30"],
                         horizon_days=20),
        ],
        bands={s: Band(symbol=s, trend=TRENDS[s], ref_level=LEVELS[s], lo=max(-0.5, LEVELS[s] - 0.5), hi=LEVELS[s])
               for s in LEVELS},
        debate=Debate(
            bull_open=AdvocateCase(
                argument="Trend intact on every equity line; costs are low; stay at reference.",
                proposal={"NDX": 1.0, "SEMIS": 1.0}, strongest_opposing_fact_id="V:SEMIS:vol_ratio",
                claims=[Claim(claim_id="c1", text="Nasdaq is 6.2% above its 200-day average", evidence_ids=["F:NDX:dist_sma200_pct"])],
            ),
            bear=BearCase(
                argument="Semis vol has more than doubled; cut the semis line to half.",
                proposal={"SEMIS": 0.5}, strongest_opposing_fact_id="F:NDX:dist_sma200_pct",
                claims=[Claim(claim_id="c1", text="Vol ratio 2.3 on semis", evidence_ids=["V:SEMIS:vol_ratio"])],
                rebuttals=[Rebuttal(claim_id="c1", verdict="refute", text="Trend distance says nothing about vol risk",
                                    evidence_ids=["V:SEMIS:vol_ratio"])],
            ),
            bull_rebuttal=AdvocateCase(
                argument="\x1b[2J\x1b]52;c;ZXZpbA==\x07Concede semis; keep the rest. <script>alert(1)</script>",
                proposal={"SEMIS": 0.75}, strongest_opposing_fact_id="V:SEMIS:vol_ratio", claims=[],
            ),
        ),
        pm=[
            PMReplicate(replicate=0, seed=42, decision=_decision(0.5), valid=True, enforced_levels=enforced),
            PMReplicate(replicate=1, seed=43, decision=_decision(0.5), valid=True, enforced_levels=enforced),
            PMReplicate(replicate=2, seed=44, decision=None, valid=False, audit_violations=["parse_fail"]),
        ],
        medoid_replicate=0,
        agreement={"SEMIS": 1.0, "NDX": 2 / 3},
        single_agent=[
            PMReplicate(replicate=0, seed=7, decision=_decision(0.75, "reference"), valid=True),
            PMReplicate(replicate=1, seed=8, decision=None, valid=False, audit_violations=["parse_fail"]),
        ],
        single_agent_levels={**LEVELS, "SEMIS": 0.75},
        material_fingerprint=PRIVATE_FINGERPRINT,
        risk=RiskDecision(
            raw_levels={**LEVELS, "SEMIS": 0.5},
            banded_levels={**LEVELS, "SEMIS": 0.5},
            proposed_w={s: v * UNIT[s] for s, v in enforced.items()},
            final_w={**{s: v * UNIT[s] for s, v in enforced.items() if s != "SPX"}, "CSPX.L": 0.15,
                     f"UNMAPPED_{PRIVATE_POSITION_ID}": 0.01},
            checks=[
                RiskCheck(rule_id="R1", name="gross", passed=True, value=0.855, limit=1.9),
                RiskCheck(rule_id="R21", name="legs", passed=True, value=1, limit=8,
                          detail=f"amount {PRIVATE_AMOUNT} USD above the {PRIVATE_NAV} minimum"),
                RiskCheck(rule_id="R7", name="broker minimum", passed=True, value=f"${PRIVATE_AMOUNT}", limit=None),
            ],
            gross=0.855, net=0.855, margin_use=0.4321, stop_budget_used=0.0712, stop_budget_limit=0.25,
            carry_bps_day=0.4, ex_ante_vol=0.176, basis="council",
            hold_reasons=["OIL: deadband"],
        ),
        plan=Plan(
            legs=[Leg(
                seq=1, kind="partial_close", symbol="SMH.L", instrument_id=PRIVATE_INSTRUMENT_ID, direction="long",
                settlement="real", leverage=1, weight_before=0.15, weight_after=0.075, stop_distance=0.1,
                sl_margin_pct=10.0, cost_bps_nav=0.6, risk_increasing=False, reason="cut per K:vol:1",
                amount_usd=PRIVATE_AMOUNT, units=PRIVATE_UNITS, sl_rate=PRIVATE_SL_RATE, position_id=PRIVATE_POSITION_ID,
            )],
            gross_before=0.93, gross_after=0.855, net_before=0.93, net_after=0.855, cost_bps_nav=0.6,
            carry_bps_day_nav=0.4, skipped=[f"GOLD: below_broker_minimum ({PRIVATE_AMOUNT} USD)",
                                            f"UNMAPPED_{PRIVATE_INSTRUMENT_ID}: no line"],
        ),
        decision_id=PRIVATE_DECISION_ID,
        decision_state="completed",
        decision_reason="Agree with the cut",
        approved_at=APPROVED_AT,
        calls=[
            RoleCall(role="pm", replicate=0, seed=42, prompt_id="pm@v1", prompt_sha=SHA_A, input_hash=SHA_B,
                     latency_ms=2300, tokens_in=9000, tokens_out=400, status="ok"),
            RoleCall(role="news", replicate=0, prompt_id="news@v1", prompt_sha="sha256:" + SHA_C, input_hash=SHA_B,
                     latency_ms=40000, status="timeout", error="ConnectTimeout http://localhost:11434 /Users/someone/x"),
        ],
        flags=["late"],
        extras={"private_note": PRIVATE_NOTE},
    )
    return record.model_copy(update=overrides) if overrides else record


@pytest.fixture
def record() -> CycleRecord:
    return make_record()


@pytest.fixture
def pack() -> FactPack:
    return make_pack()


# ------------------------------------------------------------------------------ git helpers
@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """Isolate git from the user's global/system config (identity, hooks, signing)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    return cfg


def git(cwd: Path, *args: str) -> str:
    env_id = {"GIT_AUTHOR_NAME": "setup", "GIT_AUTHOR_EMAIL": "1+setup@users.noreply.github.com",
              "GIT_COMMITTER_NAME": "setup", "GIT_COMMITTER_EMAIL": "1+setup@users.noreply.github.com"}
    import os

    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                            env={**os.environ, **env_id})
    return result.stdout


@pytest.fixture
def publisher_clone(tmp_path, git_env) -> tuple[Path, Path]:
    """A bare 'origin' and a publisher clone with one initial commit pushed."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "publisher"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "README.md").write_text("publisher clone\n")
    (clone / "journal").mkdir()
    (clone / "journal" / ".keep").write_text("")
    git(clone, "add", "README.md", "journal/.keep")
    git(clone, "commit", "-q", "-m", "init")
    git(clone, "push", "-q", "origin", "HEAD:refs/heads/main")
    return clone, origin
