"""Build tests/fixtures/site_journal/journal/: a synthetic public journal for the site.

Every file goes through the real code path, never hand-written JSON: private records
(CycleRecord, FactPack, Plan, Position, ExecutionReport) -> council.publish.redact -> the
allow-listed public models -> commit_reveal (seal) -> the council.publish.journal writers.

Content (two days; status LIVE):
  2026-09-24T0640Z  rehearsal run (no account yet), no action; the macro analyst ran
  2026-09-24T1040Z  live, no action; the macro reply was unreadable, the news analyst and one PM
                    replicate timed out, the bear needed its correction turn
  2026-09-24T1840Z  live, proposal (semiconductors cut, crude oil short) REJECTED by the operator
  2026-09-25T0640Z  live, no action; the macro analyst ran; the single-agent control disagreed
  2026-09-25T1440Z  live, proposal APPROVED and EXECUTED: semiconductors cut, a short sterling CFD
                    at leverage 2, NVIDIA opened
The book: the 9 core lines (crude oil and the euro flat) plus 10 single stocks (one of them a
class share, BRK_B, whose line id writes "." as "_"), mixed P/L % and day moves.

Deterministic: salts and times derive from the cycle id, so a re-run reproduces the same bytes.
The private values below (NAV, amounts, rates, units, ids) are CANARIES: the tests scan the
output for them.

Usage: uv run python tests/fixtures/make_site_journal.py [--out tests/fixtures/site_journal]
"""

from __future__ import annotations

import argparse
import hashlib
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from council import clock
from council.data import fred
from council.execution.executor import ExecutionReport, LegResult
from council.execution.reconcile import ReconcileResult
from council.facts.pack import market_facts
from council.llm.prompts import PromptRegistry
from council.models.broker import Position
from council.models.cards import CardDraft, EvidenceCard, MacroAnalystOutput, MacroDriver
from council.models.cycle import CycleRecord, Debate, PMReplicate, RoleCall
from council.models.debate import AdvocateCase, BearCase, Claim, Rebuttal
from council.models.facts import EventItem, Fact, FactPack, MarketState, NewsItem
from council.models.plan import Leg, Plan
from council.models.pm import DecisiveFact, Deviation, Dismissal, PMDecision
from council.models.reference import ReferenceBook, ReferenceEntry
from council.models.risk import Band, RiskCheck, RiskDecision
from council.policy import LineSpec, Universe, default_policy
from council.publish import commit_reveal, journal, redact
from council.publish.public_models import PublicPerformancePoint

OUT = Path(__file__).resolve().parent / "site_journal"
MODEL = "deepseek-v4.1-flash:cloud"
MODEL_DIGEST = "651377bfd3e5e4e7aefe381d2cca7644c0c659d75f821151794b9f7c34d8d534"

# ------------------------------------------------------------------------------ private canaries
NAV_USD = 18437.52
POSITION_ID_BASE = 3104558800
INSTRUMENT_ID_BASE = 7300
ORDER_ID_BASE = 9912345600
DECISION_IDS = {"2026-09-24T1840Z": "2026-09-24T1840Z-rebalance-a1b2c3",
                "2026-09-25T1440Z": "2026-09-25T1440Z-rebalance-d4e5f6"}
NEWS_TEXT = {
    "N:3f2a9c1e": ("Accelerator orders lift chip stocks as hyperscalers expand capacity plans",
                   "Semiconductor shares advanced after several cloud providers signalled larger data-center "
                   "budgets for next year."),
    "N:7b41d0aa": ("Palantir secures multiyear renewal with federal agency",
                   "The software company said the renewal extends an existing analytics program."),
}

# ------------------------------------------------------------------------------ universe
STOCKS: tuple[tuple[str, str], ...] = (
    ("NVDA", "NVIDIA"), ("AVGO", "Broadcom"), ("MSFT", "Microsoft"), ("META", "Meta Platforms"),
    ("AMD", "AMD"), ("ANET", "Arista Networks"), ("VRT", "Vertiv"), ("PLTR", "Palantir"),
    ("BRK_B", "Berkshire Hathaway B"), ("SMCI", "Super Micro Computer"),
)


def universe() -> Universe:
    """The policy's lines this fixture has market data for, plus the ten single-stock lines
    (the policy's own spec wins when it already lists a stock)."""
    base = default_policy().universe
    lines = [ln for ln in base.lines if ln.symbol in MARKET]
    have = {ln.symbol for ln in lines}
    extra = [
        LineSpec.model_validate({
            "symbol": sym, "name": name, "asset_class": "stock", "sleeve": "satellite", "in_reference": True,
            "base_weight": 0.02, "signal": {"source": "tiingo", "ticker": TICKERS.get(sym, sym)},
            "vehicles": {"long": [{"symbol": sym.replace("_", "."), "settlement": "real"}],
                         "short": [{"symbol": sym.replace("_", "."), "settlement": "cfd"}]},
        })
        for sym, name in STOCKS if sym not in have
    ]
    return base.model_copy(update={"lines": [*lines, *extra]})


# line: trend, vs 50d %, vs 200d %, 10d %, 3m %, drop from 1y high %, yearly vol %, vol ratio, shock ratio, 1d %
MARKET: dict[str, tuple[str, float, float, float, float, float, float, float, float, float]] = {
    "NDX": ("up", 4.2, 11.7, 4.7, 8.9, -0.9, 21.4, 0.99, 0.94, 0.62),
    "SEMIS": ("up", 7.7, 26.7, 9.5, -9.4, -13.5, 38.2, 1.12, 1.71, -1.84),
    "SPX": ("up", 1.0, 7.4, 1.9, 5.1, -1.1, 15.8, 0.88, 0.91, 0.21),
    "GOLD": ("mixed", -1.2, 9.8, -2.3, 4.4, -4.0, 16.9, 1.05, 1.10, -0.47),
    "BTC": ("up", 3.1, 14.2, 5.5, 12.3, -8.2, 52.0, 0.93, 0.87, 1.95),
    "ETH": ("mixed", -2.4, 6.1, -3.8, 2.2, -21.7, 71.5, 1.08, 1.02, -2.61),
    "OIL": ("down", -3.9, -8.7, -5.1, -11.6, -24.3, 33.1, 1.21, 1.30, -1.12),
    "EURUSD": ("mixed", 0.3, -0.6, 0.2, -1.1, -3.2, 7.4, 0.95, 0.98, 0.08),
    "GBPUSD": ("down", -0.9, -1.8, -1.2, -2.6, -4.9, 8.1, 1.02, 1.04, -0.23),
    "NVDA": ("up", 6.8, 31.5, 8.1, 18.2, -2.2, 49.8, 1.04, 1.12, 2.37),
    "AVGO": ("up", 5.2, 28.4, 6.3, 14.7, -3.1, 44.6, 0.97, 1.01, 1.14),
    "MSFT": ("up", 1.8, 9.9, 2.1, 6.3, -2.8, 22.1, 0.91, 0.89, 0.36),
    "META": ("mixed", -1.4, 12.2, -0.8, 3.9, -7.6, 34.0, 1.06, 1.15, -0.72),
    "AMD": ("up", 9.3, 22.8, 12.4, 16.9, -6.4, 55.2, 1.18, 1.42, 3.18),
    "ANET": ("up", 4.4, 25.6, 5.0, 11.8, -4.3, 46.9, 1.02, 1.08, 0.94),
    "VRT": ("up", 7.1, 38.9, 9.8, 21.5, -5.0, 58.3, 1.09, 1.23, 1.66),
    "PLTR": ("mixed", -3.2, 19.4, -4.6, 7.8, -14.9, 67.4, 1.25, 1.36, -3.05),
    "BRK_B": ("up", 2.6, 17.3, 3.3, 9.1, -6.8, 21.2, 0.96, 0.99, 0.51),
    "SMCI": ("down", -8.4, -15.2, -10.9, -22.6, -48.1, 88.7, 1.31, 1.58, -4.42),
}
UNIT = {"NDX": 0.35, "SEMIS": 0.134, "SPX": 0.15, "GOLD": 0.12, "BTC": 0.113, "ETH": 0.05, "OIL": 0.10,
        "EURUSD": 0.25, "GBPUSD": 0.25}
LEVEL_BY_TREND = {"up": 1.0, "mixed": 0.5, "down": 0.25}
TICKERS = {"NDX": "QQQ", "SEMIS": "SOXX", "SPX": "SPY", "GOLD": "GLD", "OIL": "USO", "EURUSD": "FXE",
           "GBPUSD": "FXB", "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BRK_B": "BRK-B"}

# The book before and after the executed proposal (signed weight, x NAV). Crude oil and the euro
# are flat; sterling is the short CFD at leverage 2.
BOOK_BEFORE = {"NDX": 0.35, "SEMIS": 0.134, "SPX": 0.15, "GOLD": 0.06, "BTC": 0.113, "ETH": 0.025,
               "AVGO": 0.02, "MSFT": 0.02, "META": 0.01, "AMD": 0.02, "ANET": 0.02, "VRT": 0.02, "PLTR": 0.01,
               "BRK_B": 0.02, "SMCI": 0.005}
BOOK_AFTER = {**BOOK_BEFORE, "SEMIS": 0.067, "GBPUSD": -0.0625, "NVDA": 0.02}
# vehicle, open rate, current rate, leverage, settlement, is_buy: PRIVATE (the P/L % is published)
HOLDINGS = {
    "NDX": ("EQQQ.L", 1030.2, 1098.4, 1, "real", True), "SEMIS": ("SMH.L", 312.8, 301.9, 1, "real", True),
    "SPX": ("CSPX.L", 612.4, 633.1, 1, "real", True), "GOLD": ("SGLN.L", 3890.0, 3952.0, 1, "real", True),
    "BTC": ("BTC", 58210.0, 63480.0, 1, "real", True), "ETH": ("ETH", 2911.0, 2640.0, 1, "real", True),
    "GBPUSD": ("GBPUSD", 1.3412, 1.3398, 2, "cfd", False), "NVDA": ("NVDA", 180.9, 181.2, 1, "real", True),
    "AVGO": ("AVGO", 339.0, 352.7, 1, "real", True), "MSFT": ("MSFT", 505.2, 512.9, 1, "real", True),
    "META": ("META", 771.3, 742.6, 1, "real", True), "AMD": ("AMD", 158.9, 171.6, 1, "real", True),
    "ANET": ("ANET", 139.8, 146.1, 1, "real", True), "VRT": ("VRT", 128.4, 137.9, 1, "real", True),
    "PLTR": ("PLTR", 181.6, 169.3, 1, "real", True), "BRK_B": ("BRK.B", 478.2, 486.0, 1, "real", True),
    "SMCI": ("SMCI", 47.9, 41.3, 1, "real", True),
}


@dataclass(frozen=True)
class Spec:
    cycle_id: str
    mode: str               # live | rehearsal
    scenario: str           # calm | semis
    final: str              # the decision's FINAL state
    macro: str              # ok | parse_fail | none
    move: float             # multiplier on the base one-day moves
    late_min: int
    overlay: str = "GBPUSD"  # the overlay the bear shorts in the semis scenario


CYCLES: tuple[Spec, ...] = (
    Spec("2026-09-24T0640Z", "rehearsal", "calm", "reviewed_no_action", "ok", 0.6, 3),
    Spec("2026-09-24T1040Z", "live", "calm", "reviewed_no_action", "parse_fail", -0.4, 47),
    Spec("2026-09-24T1840Z", "live", "semis", "rejected", "none", 0.8, 2, overlay="OIL"),
    Spec("2026-09-25T0640Z", "live", "calm", "reviewed_no_action", "ok", -0.7, 5),
    Spec("2026-09-25T1440Z", "live", "semis", "completed", "none", 1.0, 4),
)
REJECT_REASON = "Oil short into the OPEC+ meeting and a single volatility reading: wait one cycle."
APPROVE_REASON = "Agree with the semiconductor cut; sterling short at quarter size is fine."


def slot_of(cycle_id: str) -> datetime:
    return datetime.strptime(cycle_id, "%Y-%m-%dT%H%MZ").replace(tzinfo=UTC)


def _hex(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ------------------------------------------------------------------------------ text
def _n(v: float) -> str:
    return f"{v:+.1f}" if v else "0.0"


def _vars(spec: Spec) -> dict[str, str]:
    m = MARKET
    overlay = {"GBPUSD": "sterling", "OIL": "crude oil"}[spec.overlay]
    sentence = {
        "GBPUSD": "Sterling is the second point: the pound line is in a downtrend below both averages, and a small "
                  "short costs about 1 bp a day in carry, which the trend has paid for over the last three months.",
        "OIL": f"Crude oil is the second point: the line is in a downtrend, {m['OIL'][5]:.1f}% below its high, and a "
               "small short through a CFD costs about 1 bp a day in carry, which the trend has paid for over three "
               "months.",
    }[spec.overlay]
    return {
        "ndx_d50": f"{m['NDX'][1]:.1f}", "ndx_d200": f"{m['NDX'][2]:.1f}", "ndx_m10": f"{m['NDX'][3]:.1f}",
        "ndx_vr": f"{m['NDX'][7]:.2f}", "spx_vr": f"{m['SPX'][7]:.2f}", "spx_d200": f"{m['SPX'][2]:.1f}",
        "btc_d200": f"{m['BTC'][2]:.1f}", "semis_ew": f"{m['SEMIS'][8]:.2f}", "semis_m63": f"{m['SEMIS'][4]:.1f}",
        "semis_m10": f"{m['SEMIS'][3]:.1f}", "semis_d200": f"{m['SEMIS'][2]:.1f}",
        "semis_r1": _n(m["SEMIS"][9] * spec.move), "semis_dd": f"{-m['SEMIS'][5]:.1f}",
        "smci_dd": f"{-m['SMCI'][5]:.1f}", "smci_vr": f"{m['SMCI'][7]:.2f}", "smci_r1": _n(m["SMCI"][9] * spec.move),
        "pltr_m10": f"{m['PLTR'][3]:.1f}", "eth_dd": f"{-m['ETH'][5]:.1f}", "nvda_r1": _n(m["NVDA"][9] * spec.move),
        "overlay_name": overlay, "overlay_sentence": sentence,
    }


BULL = {
    "semis": (
        "Every equity line is still in an uptrend and the reference already holds each one at full size. The "
        "Nasdaq-100 sits {ndx_d50}% above its 50-day and {ndx_d200}% above its 200-day average, ten-day momentum is "
        "+{ndx_m10}% and volatility is at its one-year norm. The S&P 500 tells the same story at lower volatility. "
        "Semiconductors are the only line with a live warning: the five-day to sixty-day volatility ratio has jumped "
        "to {semis_ew} and the three-month change is {semis_m63}%. But the ten-day tape is +{semis_m10}% and the line "
        "is {semis_d200}% above its 200-day average, so the drawdown is being repaired rather than extended. Among "
        "the single stocks, NVIDIA, Broadcom and Arista lead with three-month gains between 11.8% and 18.2% and "
        "volatility near normal. The cost of trading is the other argument for patience: a round trip on the "
        "real-settled index funds costs about 10 bp, which a single four-hour view cannot earn back. I would hold "
        "every line at its reference, add nothing that needs leverage, and let the volatility card on "
        "semiconductors be confirmed at the next run rather than acting on one reading."
    ),
    "calm": (
        "Nothing material has changed since the last run, and the reference is the right book. Trends are intact on "
        "every line the reference holds at full size: the Nasdaq-100 is {ndx_d200}% above its 200-day average, the "
        "S&P 500 {spx_d200}% and bitcoin {btc_d200}%, each with volatility at or below its one-year norm. Gold is "
        "mixed, which the reference already reflects by holding half a unit. The single stocks split the same way: "
        "NVIDIA, Broadcom, Vertiv and AMD are in clean uptrends with three-month gains above 14%, while Palantir and "
        "Super Micro are the weak names and are already held at reduced levels by the trend rule. The macro backdrop "
        "is supportive rather than threatening: the 10-year yield is lower over twenty observations and the dollar "
        "index is softer. With no card that qualifies a cut and costs of about 10 bp a round trip, any change here "
        "would be trading for its own sake. The bear may point at Super Micro's one-day move, but a single session on a "
        "line the book barely holds is noise, not evidence. I propose no deviations."
    ),
}
BEAR = {
    "semis": (
        "The bull is right that the index lines are healthy, and I concede the Nasdaq-100 and S&P 500 cases. The "
        "disagreement is semiconductors. A code volatility card now qualifies a cut there: five-day volatility is "
        "running at {semis_ew} times its sixty-day level, the line moved {semis_r1}% in the last session, and it is "
        "{semis_dd}% below its one-year high after a three-month change of {semis_m63}%. A recovering ten-day tape "
        "inside a volatility shock is exactly the pattern where a half-size line costs little and protects the most "
        "exposed part of the book. The same logic applies, more mildly, to Super Micro, which is in a downtrend, "
        "{smci_dd}% below its high, with the highest volatility ratio in the universe. {overlay_sentence} I propose "
        "semiconductors at half the unit weight, a quarter-size short in {overlay_name}, and no change elsewhere. The "
        "bull's cost argument is fair for the index lines but does not apply to a cut, which only removes exposure."
    ),
    "calm": (
        "I agree that no line has a qualifying card, so I will not argue for a cut this run; my concern is "
        "concentration. The equity lines together, counting the single stocks, are the bulk of the book, and they "
        "move together: the Nasdaq-100, semiconductors and NVIDIA all fell in the same session last week. Super "
        "Micro is {smci_dd}% below its one-year high in a downtrend with a volatility ratio of {smci_vr}, and "
        "Palantir's ten-day change is {pltr_m10}%. Neither is large, but both are the kind of line where a stop is "
        "more likely than a recovery. Ether is the other soft spot: mixed trend, {eth_dd}% below its high. I concede "
        "c1 and c2, the index trends are real, and I concede that costs argue against small changes. My proposal is "
        "therefore modest: keep the book at reference, and have the manager name what would make it cut the weak "
        "stock lines at the next run, so the decision is prepared rather than improvised. If a volatility card fires "
        "on either name, the cut should already be written down."
    ),
}
REBUTTAL = {
    "semis": (
        "c1: the semiconductor volatility card is real and qualifies a cut, so I accept a smaller semiconductor "
        "line. c3: the {overlay_name} short has a trend behind it, but it is an overlay with a thin edge and it needs "
        "the cost desk to price it inside the budget. Beyond those concessions, the bear's case does not reach the "
        "rest of the book: the Nasdaq-100 and S&P 500 volatility ratios are {ndx_vr} and {spx_vr}, below the card "
        "threshold, and no news item in the pack argues for less risk in the index lines. NVIDIA's volatility is "
        "near its norm and its last session moved {nvda_r1}% on a day when chip designers traded on accelerator "
        "demand, which argues for keeping, not cutting, the stock line. I would set semiconductors to three quarters "
        "of the unit weight as a compromise, keep NVIDIA and the other single stocks at their reference, and accept "
        "the {overlay_name} short only at a quarter of the unit weight and only if the cost gate passes."
    ),
    "calm": (
        "c1: concentration is real, and I accept that the equity lines move together; the reference caps each line "
        "and the risk engine caps the equity cluster, so it is already priced in. c2: Super Micro and Palantir are "
        "weak, but the trend rule has already cut them to a quarter and a half of their unit weight; cutting further "
        "would put a second rule on top of the first with no new evidence. Ether's drawdown is the same story. The "
        "one-day moves in the pack are ordinary: none is beyond two of its own daily standard deviations, and the "
        "largest, Super Micro at {smci_r1}%, is on a line the book barely holds. The bear asks for a prepared "
        "decision, which is fair: the falsifier for my case is a volatility card on any equity line or a close below "
        "the 50-day average on the Nasdaq-100. Neither is present. I keep the proposal at no deviations and ask the "
        "manager to hold the reference, with the weak names on watch rather than on the block."
    ),
}


def debate(spec: Spec) -> Debate:
    v = _vars(spec)
    ov = spec.overlay
    day = _macro_day(slot_of(spec.cycle_id))
    if spec.scenario == "semis":
        bull = AdvocateCase(
            argument=BULL["semis"].format(**v), proposal={}, strongest_opposing_fact_id="V:SEMIS:ewma5_60",
            claims=[
                Claim(claim_id="c1", text="The Nasdaq-100 is in an uptrend with volatility at its one-year norm.",
                      evidence_ids=["F:NDX:trend", "V:NDX:vol_ratio", "F:NDX:dist_sma200"]),
                Claim(claim_id="c2", text=f"Semiconductors are repairing their drawdown: the ten-day change is "
                                          f"+{v['semis_m10']}%.", evidence_ids=["F:SEMIS:mom10d", "F:SEMIS:dist_sma200"]),
                Claim(claim_id="c3", text="A round trip on the real-settled index funds costs about 10 bp.",
                      evidence_ids=["C:NDX:per_side_bps"]),
            ],
            concessions=["Semiconductor volatility is elevated against its sixty-day level."],
        )
        bear = BearCase(
            argument=BEAR["semis"].format(**v), proposal={"SEMIS": 0.5, ov: -0.25},
            strongest_opposing_fact_id="F:NDX:trend",
            claims=[
                Claim(claim_id="c1", text="A qualifying volatility card covers semiconductors this cycle.",
                      evidence_ids=["K:vol:1", "V:SEMIS:ewma5_60"]),
                Claim(claim_id="c2", text="Super Micro is in a downtrend far below its one-year high.",
                      evidence_ids=["F:SMCI:trend", "F:SMCI:dd52"]),
                Claim(claim_id="c3", text=f"The {v['overlay_name']} line is in a downtrend below both averages.",
                      evidence_ids=[f"F:{ov}:trend", f"F:{ov}:mom63d"]),
            ],
            concessions=["The Nasdaq-100 and S&P 500 uptrends are intact."],
            rebuttals=[
                Rebuttal(claim_id="c1", verdict="concede", text="The Nasdaq-100 trend and volatility support the "
                                                                 "reference.", evidence_ids=["F:NDX:trend"]),
                Rebuttal(claim_id="c2", verdict="refute", text="Ten-day strength inside a volatility shock is not a "
                                                                "repair; the card qualifies a cut.",
                         evidence_ids=["K:vol:1"]),
                Rebuttal(claim_id="c3", verdict="concede", text="Costs matter for adds, not for a cut that only "
                                                                 "removes exposure.", evidence_ids=["C:NDX:per_side_bps"]),
            ],
        )
        rebuttal = AdvocateCase(
            argument=REBUTTAL["semis"].format(**v), proposal={"SEMIS": 0.75}, strongest_opposing_fact_id="K:vol:1",
            claims=[
                Claim(claim_id="c1", text="The volatility card covers semiconductors only; the index lines show no "
                                          "shock.", evidence_ids=["K:vol:1", "V:NDX:ewma5_60"]),
                Claim(claim_id="c2", text="NVIDIA's volatility is near its one-year norm.",
                      evidence_ids=["V:NVDA:vol_ratio"]),
            ],
            concessions=["c1: the semiconductor volatility card qualifies a cut.",
                         f"c3: the {v['overlay_name']} short has a trend behind it, at a quarter of the unit weight."],
        )
    else:
        bull = AdvocateCase(
            argument=BULL["calm"].format(**v), proposal={}, strongest_opposing_fact_id="F:SMCI:dd52",
            claims=[
                Claim(claim_id="c1", text="The Nasdaq-100, S&P 500 and bitcoin are in uptrends with normal "
                                          "volatility.", evidence_ids=["F:NDX:trend", "F:SPX:trend", "F:BTC:trend"]),
                Claim(claim_id="c2", text="The weak single stocks are already held at reduced levels by the trend "
                                          "rule.", evidence_ids=["F:SMCI:trend", "F:PLTR:trend"]),
                Claim(claim_id="c3", text="The 10-year yield fell over twenty observations.",
                      evidence_ids=[f"M:DGS10.chg20@{day}"]),
            ],
        )
        bear = BearCase(
            argument=BEAR["calm"].format(**v), proposal={}, strongest_opposing_fact_id="F:NDX:trend",
            claims=[
                Claim(claim_id="c1", text="The equity lines, single stocks included, move together.",
                      evidence_ids=["F:NDX:ret1d_sigma", "F:SEMIS:ret1d_sigma", "F:NVDA:ret1d_sigma"]),
                Claim(claim_id="c2", text="Super Micro and Palantir are the weakest lines, with rising volatility.",
                      evidence_ids=["F:SMCI:dd52", "V:SMCI:vol_ratio", "F:PLTR:mom10d"]),
            ],
            concessions=["The index trends are real.", "Costs argue against small changes."],
            rebuttals=[
                Rebuttal(claim_id="c1", verdict="concede", text="The index trends support the reference.",
                         evidence_ids=["F:NDX:trend"]),
                Rebuttal(claim_id="c2", verdict="concede", text="The trend rule already reduces the weak names.",
                         evidence_ids=["F:SMCI:trend"]),
            ],
        )
        rebuttal = AdvocateCase(
            argument=REBUTTAL["calm"].format(**v), proposal={}, strongest_opposing_fact_id="F:SMCI:dd52",
            claims=[Claim(claim_id="c1", text="No volatility card is present on any equity line.",
                          evidence_ids=["V:NDX:ewma5_60", "V:SEMIS:ewma5_60"])],
            concessions=["c1: the equity lines move together.", "c2: Super Micro and Palantir are weak."],
        )
    return Debate(bull_open=bull, bear=bear, bull_rebuttal=rebuttal)


def pm_decisions(spec: Spec, *, control: bool = False) -> list[PMDecision | None]:
    ov = spec.overlay
    if spec.scenario == "semis":
        cut = Deviation(symbol="SEMIS", level=0.5, direction="cut", evidence_ids=["K:vol:1", "V:SEMIS:ewma5_60"],
                        reason="Qualifying volatility card; half size until the shock ratio normalises.")
        short = Deviation(symbol=ov, level=-0.25, direction="short", evidence_ids=[f"F:{ov}:trend", f"F:{ov}:mom63d"],
                          reason="Downtrend below both averages; a quarter-size short passes the cost gate.")
        fact = DecisiveFact(text=f"Semiconductor five-day volatility is {MARKET['SEMIS'][8]:.2f} times its sixty-day "
                                 "level.", evidence_id="V:SEMIS:ewma5_60")
        full = PMDecision(
            deviations=[cut, short], decisive_fact=fact, sided_with="bear",
            dismissed=[Dismissal(claim_id="c2", why="Ten-day strength does not offset a qualifying volatility card."),
                       Dismissal(claim_id="c3", why="Costs apply to adds; a cut only removes exposure.")],
        )
        partial = PMDecision(
            deviations=[cut.model_copy(update={"level": 0.75})], decisive_fact=fact, sided_with="neither",
            dismissed=[Dismissal(claim_id="c3", why="The overlay short has too thin an edge for this cycle.")],
        )
        if control:
            return [PMDecision(deviations=[cut], decisive_fact=fact, sided_with="reference"),
                    PMDecision(deviations=[cut], decisive_fact=fact, sided_with="reference"),
                    PMDecision(decisive_fact=fact, sided_with="reference",
                               no_change_reason="One volatility reading is not enough to move the book.")]
        return [full, full, partial]
    hold = PMDecision(
        decisive_fact=DecisiveFact(text="No line has a qualifying card this cycle.", evidence_id="V:NDX:vol_ratio"),
        sided_with="reference",
        dismissed=[Dismissal(claim_id="c2", why="The weak stock lines are already cut by the trend rule.")],
        no_change_reason="Trends intact, no qualifying card, and costs exceed any expected gain.",
    )
    if control and spec.cycle_id == "2026-09-25T0640Z":
        pltr = PMDecision(
            deviations=[Deviation(symbol="PLTR", level=0.25, direction="cut",
                                  evidence_ids=["F:PLTR:mom10d", "V:PLTR:ewma5_60"],
                                  reason="Weak ten-day tape and a rising volatility ratio.")],
            decisive_fact=DecisiveFact(text="Palantir's shock ratio is the second highest in the book.",
                                       evidence_id="V:PLTR:ewma5_60"),
            sided_with="reference")
        return [pltr, pltr, hold]
    if not control and spec.macro == "parse_fail":
        return [hold, hold, None]                      # replicate 2 timed out
    return [hold, hold, hold]


# ------------------------------------------------------------------------------ pack
def _bar_at(slot: datetime) -> datetime:
    return slot - timedelta(hours=8)


def states(spec: Spec, uni: Universe) -> dict[str, MarketState]:
    slot = slot_of(spec.cycle_id)
    out = {}
    for ln in uni.lines:
        trend, d50, d200, m10, m63, dd, vol, vr, ew, r1 = MARKET[ln.symbol]
        days = 365 if ln.asset_class == "crypto" else 252
        sigma_d = vol / 100.0 / math.sqrt(days)
        ret = r1 * spec.move / 100.0
        source = "binance" if ln.asset_class == "crypto" else "tiingo"
        out[ln.symbol] = MarketState(
            symbol=ln.symbol, asset_class=ln.asset_class, trend=trend, dist_sma50_pct=d50, dist_sma200_pct=d200,
            sigma_ann=vol / 100.0, sigma_daily=round(sigma_d, 8), vol_ratio_1y=vr, ewma5_60_ratio=ew,
            mom10d_pct=m10, mom63d_pct=m63, dd52_pct=dd, ret1d_sigma=round(math.log1p(ret) / sigma_d, 4),
            market_open=clock.market_open(ln.asset_class, slot, ln.session), data_age_h=8.0,
            history_source=f"{source}:{TICKERS.get(ln.symbol, ln.symbol)}", bar_available_at=_bar_at(slot),
        )
    return out


def _macro_day(slot: datetime) -> str:
    return (slot - timedelta(hours=36)).date().isoformat()


def macro_facts(slot: datetime) -> list[Fact]:
    day = _macro_day(slot)
    at = fred.available_at(datetime.fromisoformat(day).replace(tzinfo=UTC))
    rows = (("DGS10", 4.11, "pct"), ("DGS10.chg20", -12.5, "bps"), ("DGS2", 3.58, "pct"), ("DGS2.chg20", -9.8, "bps"),
            ("T10Y2Y", 0.53, "pct"), ("T10Y2Y.chg20", -2.7, "bps"), ("DFF", 4.08, "pct"),
            ("DTWEXBGS.chg20", -0.824, "pct"), ("VIXCLS", 15.9, "pct"))
    return [Fact(id=f"M:{sid}@{day}", kind="macro", value=value, unit=unit, available_at=at, source="fred",
                 publishable=fred.is_publishable(sid.split(".")[0])) for sid, value, unit in rows]


def pack_for(spec: Spec, uni: Universe) -> FactPack:
    slot = slot_of(spec.cycle_id)
    st = states(spec, uni)
    facts: list[Fact] = []
    for sym, state in st.items():
        facts += market_facts(state, available_at=state.bar_available_at, slot=slot)
        spec_line = next(ln for ln in uni.lines if ln.symbol == sym)
        per_side = 50.0 if spec_line.asset_class == "crypto" else 5.0 if spec_line.asset_class != "stock" else 8.0
        carry = 0.0 if spec_line.asset_class in ("crypto", "stock", "etf") else 0.42
        facts += [
            Fact(id=f"C:{sym}:per_side_bps", kind="cost", symbol=sym, value=per_side, unit="bps", available_at=slot,
                 source="costs:floor"),
            Fact(id=f"C:{sym}:carry_bps_day", kind="cost", symbol=sym, value=carry, unit="bps_day",
                 available_at=slot, source="costs:floor"),
        ]
    facts += macro_facts(slot)
    news_at = slot - timedelta(hours=5)
    news = [NewsItem(id=nid, title=title, summary=summary, symbols=syms, published_at=news_at, available_at=news_at)
            for (nid, (title, summary)), syms in zip(NEWS_TEXT.items(), (["SEMIS", "NVDA"], ["PLTR"]), strict=True)]
    events = [EventItem(id="E:pce@2026-09-26", kind="pce", at_utc=datetime(2026, 9, 26, 12, 30, tzinfo=UTC),
                        severity=2, source="fred_release_54")]
    return FactPack(cycle_id=spec.cycle_id, slot=slot, created_at=slot + timedelta(minutes=spec.late_min),
                    admitted=[ln.symbol for ln in uni.lines], states=st, facts=sorted(facts, key=lambda f: f.id),
                    news=news, events=events).sealed()


# ------------------------------------------------------------------------------ cards and macro
def cards(spec: Spec) -> list[EvidenceCard]:
    slot = slot_of(spec.cycle_id)
    out = []
    if spec.scenario == "semis":
        out.append(EvidenceCard(
            card_id="K:vol:1", role="vol", scope=["SEMIS"], card_type="vol_shock", direction="risk_down",
            claim=f"Semiconductors: five-day volatility {MARKET['SEMIS'][8]:.2f} times its sixty-day level "
                  "(card threshold 1.5).", evidence_ids=["V:SEMIS:ewma5_60", "V:SEMIS:vol_ratio"], horizon_days=5,
            qualifying=True, issued_at=slot))
    out.append(EvidenceCard(
        card_id="K:event:1", role="event", scope=["market"], card_type="event_binary", direction="neutral",
        claim="US inflation (PCE) release on 26 Sep: adds are blocked in the window around it.",
        evidence_ids=["E:pce@2026-09-26"], horizon_days=1, issued_at=slot))
    if spec.macro != "parse_fail":        # the news analyst timed out in that cycle
        out += [
            EvidenceCard(card_id="K:news:1", role="news", scope=["SEMIS", "NVDA"], card_type="news_material",
                         direction="risk_up",
                         claim="Chip designers traded higher on reports of stronger accelerator demand (paraphrase).",
                         evidence_ids=["N:3f2a9c1e", "F:NVDA:ret1d_sigma"], horizon_days=5,
                         falsifier="NVIDIA closes below its 50-day average within five days.",
                         corroborated_by=["K:vol:1"] if spec.scenario == "semis" else [], issued_at=slot),
            EvidenceCard(card_id="K:news:2", role="news", scope=["PLTR"], card_type="news_context", direction="neutral",
                         claim="A federal contract renewal for Palantir was reported; guidance is unchanged "
                               "(paraphrase).", evidence_ids=["N:7b41d0aa"], horizon_days=20, issued_at=slot),
        ]
    if spec.macro == "ok":
        out.append(_macro_card(spec))
    return out


def _macro_card(spec: Spec) -> EvidenceCard:
    day = _macro_day(slot_of(spec.cycle_id))
    return EvidenceCard(
        card_id="K:macro:1", role="macro", scope=["market"], card_type="macro_context", direction="risk_up",
        claim="Lower long yields and a softer dollar are a mild tailwind for gold and crypto.",
        evidence_ids=[f"M:DGS10.chg20@{day}", f"M:DTWEXBGS.chg20@{day}"], horizon_days=20,
        issued_at=slot_of(spec.cycle_id))


def macro_output(spec: Spec) -> MacroAnalystOutput | None:
    if spec.macro != "ok":
        return None
    day = _macro_day(slot_of(spec.cycle_id))
    card = _macro_card(spec)
    return MacroAnalystOutput(
        regime="risk_on" if spec.cycle_id.startswith("2026-09-25") else "neutral",
        drivers=[
            MacroDriver(text="The 10-year Treasury yield is 4.11% after falling 12.5 bp over twenty observations, "
                             "easing pressure on long-duration equities.",
                        evidence_ids=[f"M:DGS10@{day}", f"M:DGS10.chg20@{day}"]),
            MacroDriver(text="The broad dollar index slipped about 0.8% over twenty observations; a softer dollar "
                             "supports gold and crypto.", evidence_ids=[f"M:DTWEXBGS.chg20@{day}"]),
            MacroDriver(text="The curve is positively sloped at 53 bp and the policy rate is steady.",
                        evidence_ids=[f"M:T10Y2Y@{day}", f"M:DFF@{day}"]),
            MacroDriver(text="Implied equity volatility is calm by its own history.",
                        evidence_ids=[f"M:VIXCLS@{day}"]),
        ],
        sleeve_tilts={"core": 0, "crypto": 1, "overlay": -1, "satellite": 0},
        cards=[CardDraft(**card.model_dump(include=set(CardDraft.model_fields)))],
    )


# ------------------------------------------------------------------------------ calls
def calls(spec: Spec, reg: PromptRegistry) -> list[RoleCall]:
    k = int(spec.cycle_id[11:13])

    def call(role: str, *, rep: int = 0, status: str = "ok", error: str = "", latency: int = 0,
             tin: int | None = None, tout: int | None = None) -> RoleCall:
        return RoleCall(role=role, replicate=rep, seed=42 + rep, prompt_id=reg.prompt_id(role),
                        prompt_sha=reg.sha256(role), input_hash=_hex(spec.cycle_id, role, str(rep)),
                        latency_ms=latency or 1400 + 97 * k + 311 * rep,
                        tokens_in=5200 + 41 * k if tin is None else tin,
                        tokens_out=900 + 13 * rep if tout is None else tout,
                        status=status, error=error)  # type: ignore[arg-type]

    out: list[RoleCall] = []
    if spec.macro == "parse_fail":
        out.append(call("news", status="timeout", error="timeout after 90s", latency=91012, tin=0, tout=0))
        out.append(call("macro", status="parse_fail", latency=4120, tin=10673, tout=1400,
                        error="reply is not a JSON object | after correction: regime: Input should be 'risk_on', "
                              "'neutral' or 'risk_off'"))
    else:
        out.append(call("news", latency=2210, tout=420))
        if spec.macro == "ok":
            out.append(call("macro", latency=3874, tin=10673, tout=640))
    out.append(call("bull_open", latency=3005, tout=1054))
    bear_error = "corrected: claims.2.text: String should have at most 300 characters" \
        if spec.macro == "parse_fail" else ""
    out.append(call("bear", latency=4508, tout=1368, error=bear_error))
    out.append(call("bull_rebuttal", latency=3803, tout=1062))
    for rep in range(3):
        if spec.macro == "parse_fail" and rep == 2:
            out.append(call("pm", rep=rep, status="timeout", error="timeout after 120s", latency=120450, tin=0,
                            tout=0))
        else:
            out.append(call("pm", rep=rep, latency=1614 + 90 * rep, tin=8285, tout=322 + 11 * rep))
    for rep in range(3):
        out.append(call("single_agent", rep=rep, latency=1380 + 70 * rep, tin=4410, tout=298 + 9 * rep))
    return out


# ------------------------------------------------------------------------------ book, risk, plan
def reference(spec: Spec, uni: Universe) -> ReferenceBook:
    entries = {}
    for ln in uni.lines:
        trend = MARKET[ln.symbol][0]
        unit = UNIT.get(ln.symbol, 0.02)
        level = LEVEL_BY_TREND[trend] if ln.in_reference else 0.0
        entries[ln.symbol] = ReferenceEntry(
            symbol=ln.symbol, sleeve=ln.sleeve, asset_class=ln.asset_class, in_reference=ln.in_reference,
            trend=trend, level_ref=level, unit_weight=unit, weight_ref=round(level * unit, 4),
            sigma_ann=MARKET[ln.symbol][6] / 100.0, stop_distance=0.12)
    return ReferenceBook(cycle_id=spec.cycle_id, entries=entries, k=1.0, target_vol=0.14, ex_ante_vol=0.127,
                         gross=round(sum(e.weight_ref for e in entries.values()), 4))


def bands(ref: ReferenceBook, uni: Universe, qualifying: set[str]) -> dict[str, Band]:
    out = {}
    for ln in uni.lines:
        e = ref.entries[ln.symbol]
        trend = e.trend
        if not ln.council_deviations:
            lo = hi = e.level_ref
            reasons = ["reference-only line"]
        elif not ln.in_reference:
            lo, hi = {"up": (0.0, 0.5), "mixed": (-0.25, 0.25), "down": (-0.5, 0.0)}[trend]
            reasons = ["overlay: trend-following band"]
        elif trend == "up":
            lo = e.level_ref - 0.5 if ln.symbol in qualifying else e.level_ref
            hi = e.level_ref
            reasons = ["uptrend: cut allowed by a qualifying card" if ln.symbol in qualifying
                       else "uptrend: a cut needs a qualifying card"]
        elif trend == "mixed":
            lo, hi, reasons = 0.0, 1.0, ["mixed trend"]
        else:
            lo, hi, reasons = -0.5, 0.25, ["downtrend"]
        out[ln.symbol] = Band(symbol=ln.symbol, trend=trend, ref_level=e.level_ref, lo=lo, hi=hi, reasons=reasons,
                              qualifying_cards=["K:vol:1"] if ln.symbol in qualifying else [])
    return out


def _levels(ref: ReferenceBook, decision: PMDecision | None) -> dict[str, float]:
    base = {s: e.level_ref for s, e in ref.entries.items()}
    return decision.levels(base) if decision is not None else base


def risk(spec: Spec, ref: ReferenceBook, levels: dict[str, float]) -> RiskDecision:
    unit = {s: e.unit_weight for s, e in ref.entries.items()}
    base = {} if spec.mode == "rehearsal" else dict(BOOK_BEFORE)
    target = {s: round(lv * unit[s], 4) for s, lv in levels.items()}
    if spec.mode == "rehearsal":
        final = target                                          # no account: the target book
    elif spec.scenario == "semis":
        changed = {"SEMIS": target["SEMIS"], spec.overlay: target[spec.overlay]}
        if spec.final == "completed":
            changed = {"SEMIS": 0.067, "GBPUSD": -0.0625, "NVDA": 0.02}
        final = {**base, **changed}
    else:
        final = dict(base)
    gross = sum(abs(v) for v in final.values())
    net = sum(final.values())
    checks = [
        RiskCheck(rule_id="R1", name="gross", passed=True, value=round(gross, 3), limit=1.9),
        RiskCheck(rule_id="R2", name="net", passed=True, value=round(net, 3), limit=1.5),
        RiskCheck(rule_id="R6", name="line_caps", passed=True, value=0.35, limit=0.4),
        RiskCheck(rule_id="R8", name="ex_ante_vol", passed=True, value=0.131, limit=0.25),
        RiskCheck(rule_id="R10", name="authority", passed=True, value=2, limit=3),
        RiskCheck(rule_id="R12", name="deadband", passed=True),
        RiskCheck(rule_id="R15", name="net_of_cost_gate", passed=True, value=0.61, limit=0.3),
        RiskCheck(rule_id="R16", name="cycle_cost_bps", passed=True, value=1.6, limit=25),
        RiskCheck(rule_id="MC", name="material_change", passed=spec.scenario == "semis"),
    ]
    holds = [] if spec.scenario == "semis" else ["OIL: deadband", "PLTR: R10 no band: hold current"]
    return RiskDecision(
        raw_levels=levels, banded_levels=levels, base_w=base, proposed_w=final, final_w=final, checks=checks,
        gross=gross, net=net, margin_use=0.58 if spec.mode == "live" else 0.0, stop_budget_used=0.071,
        stop_budget_limit=0.25, carry_bps_day=0.36 if spec.final == "completed" else 0.12, ex_ante_vol=0.131,
        basis="council", hold_reasons=holds)


def _vehicle(line: str) -> tuple[str, str]:
    return {"SEMIS": ("SMH.L", "real"), "OIL": ("OIL", "cfd"), "GBPUSD": ("GBPUSD", "cfd"),
            "NVDA": ("NVDA", "real")}[line]


def plan(spec: Spec, decision: RiskDecision) -> Plan | None:
    if spec.scenario != "semis":
        return None
    legs = []
    lines = ("SEMIS", spec.overlay, *(("NVDA",) if spec.final == "completed" else ()))
    for seq, line in enumerate(lines, start=1):
        before, after = decision.base_w.get(line, 0.0), decision.final_w[line]
        vehicle, settlement = _vehicle(line)
        lev = 2 if line == "GBPUSD" else 1
        short = after < 0
        notional = abs(after - before) * NAV_USD
        price = {"SEMIS": 301.9, "OIL": 71.35, "GBPUSD": 1.3412, "NVDA": 181.4}[line]
        legs.append(Leg(
            seq=seq, kind="partial_close" if line == "SEMIS" else "open", symbol=vehicle, line=line,
            instrument_id=INSTRUMENT_ID_BASE + seq, direction="short" if short else "long", settlement=settlement,
            leverage=lev, weight_before=before, weight_after=after, stop_distance=None if line == "SEMIS" else 0.06,
            sl_margin_pct=None if line == "SEMIS" else 6.0 * lev, cost_bps_nav=round(abs(after - before) * 8.0, 2),
            carry_bps_day_nav=0.2 if settlement == "cfd" else 0.0, risk_increasing=line != "SEMIS",
            reason=f"{line}: council deviation", amount_usd=round(notional, 2), units=round(notional / price, 4),
            sl_rate=None if line == "SEMIS" else round(price * (1.06 if short else 0.94), 4),
            position_id=POSITION_ID_BASE + 1 if line == "SEMIS" else None,
        ))
    gb = sum(abs(v) for v in decision.base_w.values())
    ga = sum(abs(v) for v in decision.final_w.values())
    return Plan(legs=legs, gross_before=gb, gross_after=ga, net_before=sum(decision.base_w.values()),
                net_after=sum(decision.final_w.values()), cost_bps_nav=round(sum(leg.cost_bps_nav for leg in legs), 2),
                carry_bps_day_nav=0.36)


def execution(spec: Spec, the_plan: Plan, uni: Universe, approved_at: datetime):
    legs = []
    for leg in the_plan.legs:
        slip = 1.0004 if leg.direction == "long" else 0.9997
        price = (leg.amount_usd / leg.units) * slip
        legs.append(LegResult(
            seq=leg.seq, kind=leg.kind, symbol=leg.symbol, line=leg.line or "", state="filled", attempts=1,
            order_id=ORDER_ID_BASE + leg.seq, position_ids=[POSITION_ID_BASE + 10 + leg.seq],
            units_requested=leg.units, units_filled=round(leg.units * 0.998, 4), fill_price=round(price, 4)))
    report = ExecutionReport(
        decision_id=DECISION_IDS[spec.cycle_id], final_state="completed", legs=legs,
        reconcile=ReconcileResult(ok=True, drift=0.0031, drift_max=0.02,
                                  achieved_w={"SEMIS": 0.0668, "GBPUSD": -0.0619, "NVDA": 0.0199}),
        equity_before=NAV_USD, equity_after=NAV_USD - 3.1, writes_sent=len(legs) * 2)
    return redact.public_execution(report, cycle_id=spec.cycle_id, lines=uni, nav_usd=NAV_USD, plan=the_plan,
                                   approved_at=approved_at, completed_at=approved_at + timedelta(minutes=6))


def positions(uni: Universe) -> list[Position]:
    out = []
    for i, (line, weight) in enumerate(sorted(BOOK_AFTER.items())):
        if line not in HOLDINGS or not weight:
            continue
        vehicle, open_rate, now, lev, settlement, is_buy = HOLDINGS[line]
        exposure = abs(weight) * NAV_USD
        units = exposure / now
        out.append(Position(
            position_id=POSITION_ID_BASE + 100 + i, instrument_id=INSTRUMENT_ID_BASE + 100 + i, symbol=vehicle,
            is_buy=is_buy, leverage=lev, units=round(units, 6), open_rate=open_rate,
            amount=round(units * open_rate / lev, 2), sl_rate=round(now * (0.85 if is_buy else 1.15), 4),
            settlement=settlement, opened_at=datetime(2026, 9, 10, 15, tzinfo=UTC), exposure_usd=round(exposure, 2),
            close_rate=now))
    return out


# ------------------------------------------------------------------------------ assemble
def record(spec: Spec, uni: Universe, reg: PromptRegistry) -> tuple[CycleRecord, FactPack]:
    slot = slot_of(spec.cycle_id)
    pk = pack_for(spec, uni)
    ref = reference(spec, uni)
    qualifying = {"SEMIS"} if spec.scenario == "semis" else set()
    decisions = pm_decisions(spec)
    # the control sees no debate, so it has no advocate claims to set aside (prompts/single_agent.md)
    control = [d.model_copy(update={"dismissed": []}) if d is not None else None
               for d in pm_decisions(spec, control=True)]
    levels = _levels(ref, decisions[0])
    reps = [PMReplicate(replicate=i, seed=42 + i, decision=d, valid=d is not None,
                        audit_violations=[] if d is not None else ["no_decision"],   # as audit() records it
                        enforced_levels=_levels(ref, d) if d is not None else {})
            for i, d in enumerate(decisions)]
    sa = [PMReplicate(replicate=i, seed=42 + i, decision=d, valid=True, enforced_levels=_levels(ref, d))
          for i, d in enumerate(control)]
    agree = 2 / 3 if spec.scenario == "semis" else 1.0
    agreement = {"SEMIS": agree, spec.overlay: 2 / 3} if spec.scenario == "semis" else {"NDX": agree}
    decision = risk(spec, ref, levels)
    the_plan = plan(spec, decision) if spec.mode == "live" else None
    proposal = the_plan is not None and bool(the_plan.legs)
    approved_at = slot + timedelta(minutes=33) if spec.final == "completed" else None
    rec = CycleRecord(
        cycle_id=spec.cycle_id, slot=slot, started_at=slot + timedelta(minutes=spec.late_min),
        finished_at=slot + timedelta(minutes=spec.late_min, seconds=48), status="late" if spec.late_min > 10 else "on_time",
        late_by_s=spec.late_min * 60, mode=spec.mode, input_hash=pk.input_hash,
        policy_sha=default_policy().sha256, prompt_manifest_sha=reg.manifest_sha(), model=MODEL,
        model_digest=MODEL_DIGEST, why_we_met=["scheduled", *(["vol_shock:SEMIS"] if spec.scenario == "semis" else [])],
        reference=ref, cards=cards(spec), macro=macro_output(spec),
        bands=bands(ref, uni, qualifying), debate=debate(spec), pm=reps, medoid_replicate=0, agreement=agreement,
        single_agent=sa, single_agent_levels=_levels(ref, control[0]), risk=decision, plan=the_plan,
        decision_id=DECISION_IDS.get(spec.cycle_id) if proposal else None,
        decision_state=spec.final,
        decision_reason={"rejected": REJECT_REASON, "completed": APPROVE_REASON}.get(spec.final, ""),
        approved_at=approved_at, material_fingerprint=_hex("fingerprint", spec.cycle_id),
        calls=calls(spec, reg),
        flags=["calendar:release_dates_skipped_no_fred_key"] if spec.mode == "rehearsal" else [],
    )
    return rec, pk


def build(out_root: Path = OUT) -> list[Path]:
    """Write the fixture journal under `out_root/journal` (replacing it). Returns the files."""
    uni = universe()
    reg = PromptRegistry()
    files: dict[str, bytes] = {}
    ops = []
    latest_pack: FactPack | None = None
    latest_ref: dict[str, float] = {}
    latest_levels: dict[str, float] = {}
    for spec in CYCLES:
        rec, pk = record(spec, uni, reg)
        proposal = rec.decision_id is not None
        # sealed BEFORE the human decision: a proposal is sealed while it awaits publication
        sealed_rec = rec.model_copy(update={"decision_state": "awaiting_publication", "decision_reason": "",
                                            "approved_at": None}) if proposal else rec
        doc = redact.public_cycle(sealed_rec, pk, lines=uni)
        commitment, salt, sealed = commit_reveal.seal_bytes(
            doc, sealed_at=rec.slot + timedelta(minutes=spec.late_min + 1), salt_hex=_hex("fixture-salt", spec.cycle_id))
        files |= journal.commitment_files(commitment)
        files |= journal.reveal_files(sealed, salt, commitment)
        ops.append(redact.public_ops_row(rec))
        if spec.final == "completed" and rec.plan is not None and rec.approved_at is not None:
            files |= journal.execution_files(execution(spec, rec.plan, uni, rec.approved_at))
        latest_pack = pk
        latest_ref = rec.reference.weights() if rec.reference else {}
        latest_levels = dict(rec.risk.banded_levels) if rec.risk else {}
    last = CYCLES[-1]
    files |= journal.ops_files(None, ops)
    files |= journal.status_files(redact.public_status(
        "LIVE", last_cycle_id=last.cycle_id, last_cycle_at=slot_of(last.cycle_id), kill_state="NORMAL"))
    files |= journal.book_files(redact.public_book(
        last.cycle_id, BOOK_AFTER, lines=uni, reference_weights=latest_ref, levels=latest_levels,
        kill_state="NORMAL", positions=positions(uni), pack=latest_pack))
    files |= journal.performance_files(None, [
        PublicPerformancePoint(as_of=datetime(2026, 9, 23).date(), c0=100.0, c2=100.0, c2x=100.0, c3=100.0,
                               c4_spy=100.0, c4_btc=100.0, drawdown_pct=0.0, segment="live"),
        PublicPerformancePoint(as_of=datetime(2026, 9, 24).date(), cycle_id="2026-09-24T1840Z", c0=100.42, c1=100.39,
                               c2=100.31, c2x=100.35, c3=100.12, c4_spy=100.55, c4_btc=101.9, drawdown_pct=0.0,
                               segment="live"),
        PublicPerformancePoint(as_of=datetime(2026, 9, 25).date(), cycle_id="2026-09-25T1440Z", c0=100.18, c1=100.2,
                               c2=100.05, c2x=100.1, c3=99.93, c4_spy=100.21, c4_btc=103.4, drawdown_pct=-0.24,
                               segment="live"),
    ])
    target = out_root / "journal"
    if target.exists():
        shutil.rmtree(target)
    return journal.write_files(out_root, files)


def canaries() -> list[str | float]:
    """Private values that must never appear in the fixture journal or the site built from it."""
    values: list[str | float] = [NAV_USD, *DECISION_IDS.values()]
    values += [str(POSITION_ID_BASE + i) for i in (1, 11, 12, 13, *range(100, 120))]
    values += [str(ORDER_ID_BASE + i) for i in (1, 2, 3)]
    for _, (_, open_rate, now, *_rest) in HOLDINGS.items():
        values += [open_rate, now]
    for _, (title, summary) in NEWS_TEXT.items():
        values += [title, summary]
    return [v for v in values if not isinstance(v, float) or v >= 100.0]   # tiny rates collide with percentages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    written = build(args.out)
    print(f"site fixture: {len(written)} files under {args.out / 'journal'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
