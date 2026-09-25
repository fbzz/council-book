"""The risk engine: council levels in, a RiskDecision (final signed weights + R-rows) out.

Pipeline (every number from policy/risk.yaml; rule IDs as in its comments):
 1. Kill switch (R3): HALTED/FLAT -> every target 0 (a flatten proposal, basis "halted").
 2. Authority (R10): clip levels into their bands; at most max_deviations_per_cycle council
    deviations survive (the largest; ties by symbol).
 3. Compliance base: current weights, de-risked to gross.watch_derisk_to when current gross is above
    gross.hard_max (R1); that cycle's gross cap is then watch_derisk_to, not proposal_max. Filters
    revert discretionary moves to this base, so compliance is exempt from them.
 4. Boxes per line (weights = level x unit weight): band; |level| <= 1 where the class leverage
    cap is 1 (R6, hold-allowed); line cap with hold-allowed (R5); no-increase for WARN (R3), vol breakers (R9),
    macro event windows (R16), post-stop cool-off (R4d), missing catastrophe stop (R4), hedged
    lines; side-specific anti-chase (R17); hold for frozen or missing states (R18/R19), frozen
    reference share above the limit (R18), closed markets (R19) and blockers (R20).
    Freshness is the pack's call: a line holds when its state is `frozen` (any reason); the engine
    never re-derives staleness from the raw `data_age_h` (the pack's age skips weekends and
    holidays). The R18 frozen reference share counts only DATA freezes: a state frozen solely
    for `market_closed` is held (R19) but does not count, so a closed equity session can never
    hold the crypto lines.
 5. Projection: group caps (crypto_total, fx_total, equity_beta_cluster; R5) -> gross <=
    proposal_max (R1) -> short gross and net floor (R2) -> net ceiling (R2) -> margin sum(|w|/L) <=
    margin_use_max with L = 2 above level 1.0 (R7) -> ex-ante vol <= ex_ante_vol_hard (R8).
    Aggregate caps are max(limit, base value): a book already over a limit may be held or reduced.
 6. Execution filters per changed line: deadband (R11), minimum hold (R12), material change
    (MC), net-of-cost gate (R15); then budgets: net bounds a cut would break (R2; shrinking
    cannot fix those), cycle increase and turnover (R13), cycle and 30-day cost, carry (R14),
    legs (R21). A held line returns to the base and the projection is
    rerun until no new line is held (the held set only grows, so this terminates).
 7. Checks R1..R21 (+R4d, MC) are recomputed on the final book.
Moves toward the reference are exempt from R12 and R13 (TOWARD_REFERENCE_EXEMPT) and use the
reference cost threshold; they are NOT exempt from the cost gate, deadband, event block or
anti-chase.
Cost quotes: `cost_quotes` may be keyed (line, direction, leverage), (line, direction) or line.
A leg is priced with the quote of its direction and leverage (L = 2 when the line's level after
the move is above 1.0), falling back to the unlevered keys when no levered quote exists.
`RiskDecision.base_w` is the snapshot book the engine started from (zeros without a snapshot);
`models.risk.changed_lines(decision)` is the set of lines the planner may touch.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

import numpy as np

from council.invariants import check_policy
from council.models.broker import CostQuote, ExposureSnapshot
from council.models.facts import EventItem, MarketState
from council.models.risk import Band, RiskCheck, RiskDecision
from council.policy import LineSpec, Policy
from council.risk import checks as ck
from council.risk.authority import no_add_interval, restrict
from council.risk.churn import (
    anti_chase_block,
    deadband_ok,
    event_block,
    line_increase,
    min_hold_ok,
    reentry_blocked,
)
from council.risk.config import risk_limits
from council.risk.costs import gate_threshold, hold_days, round_trip_bps, srbe
from council.risk.exposure import is_unmapped
from council.risk.killswitch import LATCHED
from council.risk.projection import project_or_nearest
from council.risk.stops import catastrophe_stop_distance, stop_at_risk

EPS = 1e-9
TOWARD_REFERENCE_EXEMPT: frozenset[str] = frozenset({"R12", "R13"})
MARKET_CLOSED = "market_closed"   # the pack's session freeze: held, but not a data freeze
BOX_LABELS = {
    "warn": "R3 WARN no adds", "breaker": "R9 vol breaker", "event": "R16 event window",
    "cooloff": "R4d post-stop cool-off", "nostop": "R4 no catastrophe stop",
    "hedged": "hedged line", "chase_long": "R17 anti-chase", "chase_short": "R17 anti-chase",
    "stale": "R18 frozen data", "closed": "R19 market closed", "blocked": "R20 blocker",
    "cap": "R5 line cap", "leverage": "R6 leverage cap",
}
VolFn = Callable[[Mapping[str, float]], float]


class ForbiddenLegError(ValueError):
    """A leg the system must never produce (e.g. removing a stop-loss)."""


def classify_risk_increasing(
    before_w: float, after_w: float, *, sl_widened: bool = False, sl_removed: bool = False
) -> bool:
    """A leg reduces risk only if |w| drops without a sign flip. A new position, a flip, a larger
    |w| or a wider stop increase risk. Removing a stop is forbidden outright (raises)."""
    if sl_removed:
        raise ForbiddenLegError("removing a stop-loss is forbidden")
    if sl_widened:
        return True
    return ck.increased(before_w, after_w)


def freeze_reasons(state: MarketState) -> set[str]:
    """The pack's comma-separated `frozen_reason`, as a set (empty when none is given)."""
    return {r.strip() for r in (state.frozen_reason or "").split(",") if r.strip()}


def data_frozen(state: MarketState | None) -> bool:
    """R18 data freeze: a missing state, or a frozen one for any reason other than
    `market_closed` (a frozen state without a reason counts, fail closed)."""
    if state is None:
        return True
    if not state.frozen:
        return False
    reasons = freeze_reasons(state)
    return not reasons or bool(reasons - {MARKET_CLOSED})


def toward_reference(before: float, after: float, ref: float) -> bool:
    """True when the move goes toward the reference without passing it."""
    if abs(after - before) <= EPS:
        return False
    lo, hi = min(before, ref), max(before, ref)
    return lo - EPS <= after <= hi + EPS and abs(after - ref) < abs(before - ref) - EPS


class RiskEngine:
    """Deterministic risk officer. One instance per policy; `evaluate` is pure."""

    def __init__(self, policy: Policy) -> None:
        check_policy(policy)  # the policy may be stricter than the hard-coded invariants, never looser
        self.policy = policy
        self.limits = risk_limits(policy)
        self.specs: dict[str, LineSpec] = policy.universe.by_symbol()

    def evaluate(
        self,
        *,
        levels: Mapping[str, float],
        ref: Mapping[str, float],
        bands: Mapping[str, Band],
        states: Mapping[str, MarketState],
        snapshot: ExposureSnapshot | None,
        unit_weights: Mapping[str, float],
        kill_state: str,
        cost_quotes: Mapping[object, CostQuote],
        events: Iterable[EventItem],
        last_change: Mapping[str, datetime],
        turnover_7d: float,
        material_changed: bool,
        basis: str,
        now: datetime,
        ex_ante_vol_fn: VolFn | None = None,
        turnover_30d: float | None = None,
        cost_30d_bps: float | None = None,
        book_vol_ratio: float | None = None,
        stop_hits: Mapping[str, datetime] | None = None,
        blockers: Iterable[str] = (),
        broker_min_share: Mapping[str, float] | None = None,
        nav_drawdown: float | None = None,
    ) -> RiskDecision:
        """Run the pipeline in the module docstring.

        `levels` are post-enforce council levels, `ref` the reference LEVELS, `unit_weights` the
        weight of each line at level 1.0. `cost_quotes` maps (line, direction, leverage),
        (line, direction) or a line to its floored quote (see the module docstring). `turnover_7d`/`turnover_30d` are trailing discretionary turnover and
        `cost_30d_bps` trailing discretionary cost, as NAV shares / bps of NAV."""
        run = _Run(
            self,
            levels=levels, ref=ref, bands=bands, states=states, snapshot=snapshot,
            unit_weights=unit_weights, kill_state=kill_state, cost_quotes=cost_quotes,
            events=list(events), last_change=last_change, turnover_7d=turnover_7d,
            material_changed=material_changed, basis=basis, now=now, vol_fn=ex_ante_vol_fn,
            turnover_30d=turnover_30d, cost_30d_bps=cost_30d_bps, book_vol_ratio=book_vol_ratio,
            stop_hits=stop_hits or {}, blockers=list(blockers),
            broker_min_share=broker_min_share or {}, nav_drawdown=nav_drawdown,
        )
        return run.decide()


class _Run:
    """One evaluation. Holds the inputs and the per-line bookkeeping of each stage.

    The keyword inputs become attributes under the names `RiskEngine.evaluate` passes: levels,
    ref, bands, states, snapshot, unit_weights, kill_state, cost_quotes, events, last_change,
    turnover_7d, material_changed, basis, now, vol_fn, turnover_30d, cost_30d_bps,
    book_vol_ratio, stop_hits, blockers, broker_min_share, nav_drawdown."""

    def __init__(self, engine: RiskEngine, **kw) -> None:
        self.p: Policy = engine.policy
        self.lim = engine.limits
        self.specs = engine.specs
        self.__dict__.update(kw)
        if self.now.tzinfo is None:
            raise ValueError("naive datetime; council code uses aware UTC datetimes only")
        unknown = sorted(s for s in self.levels if s not in self.specs and not is_unmapped(s))
        if unknown:
            raise ValueError(f"unknown lines in levels: {unknown}")
        signed = self.snapshot.signed_w if self.snapshot is not None else {}
        self.cur = {s: float(w) for s, w in signed.items()}
        self.managed = list(self.specs)
        self.locked = sorted(s for s in self.cur if s not in self.specs)
        self.order = self.managed + self.locked
        self.cls = {s: spec.asset_class for s, spec in self.specs.items()}
        self.unit = {s: max(float(self.unit_weights.get(s, 0.0)), 0.0) for s in self.managed}
        self.ref_level = {s: float(self.ref.get(s, 0.0)) for s in self.managed}
        self.ref_w = {s: self.ref_level[s] * self.unit[s] for s in self.managed}
        self.raw = {s: float(self.levels.get(s, self.ref_level[s])) for s in self.managed}
        self.hedged = set(self.snapshot.hedged) if self.snapshot is not None else set()
        self.hold_reasons: list[str] = []
        self.compliance: list[str] = []
        self.srbe_seen: dict[str, float] = {}
        self.vol_detail = "covariance function" if self.vol_fn else "upper bound: rho = 1"
        if self.snapshot is None:
            self.hold_reasons.append("no broker snapshot: current book taken as flat")

    # ------------------------------------------------------------------ helpers
    def level_of(self, s: str, w: float) -> float:
        u = self.unit.get(s, 0.0)
        return w / u if u > EPS else 0.0

    def quote(self, s: str, direction: str, leverage: int = 1) -> CostQuote | None:
        """The quote for one leg: (line, direction, L) first; a levered leg without a levered
        quote falls back to the unlevered keys (line, direction, 1), (line, direction), line."""
        keys: list[object] = [(s, direction, leverage)]
        if leverage != 1:
            keys.append((s, direction, 1))
        keys.extend([(s, direction), s])
        for key in keys:
            q = self.cost_quotes.get(key)
            if q is not None and q.direction == direction:
                return q
        return None

    def leg_quote(self, s: str, before: float, after: float) -> CostQuote | None:
        """Quote for the move before -> after, in its direction. It is a lever leg (L = 2) when
        the traded side reaches the leverage extension: |level| above 1.0 after the move, or
        before it when the move stays on the same side (trimming the levered tranche)."""
        if s not in self.unit:
            return self.quote(s, self.direction(before, after))
        level = abs(self.level_of(s, after))
        if before * after > 0:
            level = max(level, abs(self.level_of(s, before)))
        return self.quote(s, self.direction(before, after), ck.line_leverage(level))

    def direction(self, before: float, after: float) -> str:
        if after > EPS:
            return "long"
        if after < -EPS:
            return "short"
        return "long" if before > 0 else "short"

    def carry_line(self, s: str, w: float) -> float:
        if abs(w) <= EPS or s not in self.specs:
            return 0.0
        q = self.leg_quote(s, 0.0, w)
        return abs(w) * q.carry_bps_day if q is not None else 0.0

    def carry(self, w: Mapping[str, float]) -> float:
        return float(sum(self.carry_line(s, v) for s, v in w.items()))

    def vol(self, w: Mapping[str, float]) -> float:
        if self.vol_fn is not None:
            return float(self.vol_fn(dict(w)))
        total = 0.0
        for s, v in w.items():
            st = self.states.get(s)
            if st is not None and st.sigma_ann:
                total += abs(v) * st.sigma_ann
        return total

    def data_frozen(self, s: str) -> bool:
        """R18: missing state or a data freeze from the pack (never re-derived from data_age_h)."""
        return data_frozen(self.states.get(s))

    def session_frozen(self, s: str) -> bool:
        """R19: the market is closed, or the pack froze the line only for market_closed."""
        st = self.states.get(s)
        return st is not None and (not st.market_open or (st.frozen and not data_frozen(st)))

    def groups(self) -> list[tuple[str, list[str], float]]:
        caps = self.lim.caps
        crypto = [s for s in self.managed if self.cls[s] == "crypto"]
        fx = [s for s in self.managed if self.cls[s] == "fx"]
        beta = [s for s in caps.equity_beta_cluster.members if s in self.specs]
        return [
            ("crypto_total", crypto, caps.crypto_total),
            ("fx_total", fx, caps.fx_total),
            ("equity_beta_cluster", beta, caps.equity_beta_cluster.max),
        ]

    def cushion(self) -> float:
        """Reported next to stop-at-risk: NAV share between now and the halt line."""
        dd = min(max(self.nav_drawdown or 0.0, 0.0), 0.999999)
        return max(0.0, 1.0 - self.lim.killswitch.halt_at / (1.0 - dd))

    def base_w(self) -> dict[str, float]:
        """The book the engine started from: snapshot line weights (before any R1 de-risk), every
        managed line present (0 when flat or without a snapshot), plus the locked lines."""
        return {s: self.cur.get(s, 0.0) for s in self.order}

    def changed(self, w: Mapping[str, float], held: Mapping[str, str] | None = None) -> list[str]:
        held = held or {}
        return [s for s in self.managed
                if s not in held and abs(w.get(s, 0.0) - self.base[s]) > EPS]

    # ------------------------------------------------------------------ stages
    def decide(self) -> RiskDecision:
        if self.kill_state in LATCHED:
            return self.flatten()
        self.authority()
        self.compliance_base()
        self.stop_distances()
        self.boxes()
        target = {s: self.banded[s] * self.unit[s] for s in self.managed}
        target.update({s: self.base[s] for s in self.locked})
        held: dict[str, str] = {}
        proposed: dict[str, float] | None = None
        w = dict(self.base)
        for _ in range(len(self.order) + 2):
            w = self.project(target, held)
            if proposed is None:
                proposed = dict(w)
            new = self.line_filters(w, held) or self.budget_filters(w, held)
            if not new:
                break
            held.update(new)
        final = {s: (0.0 if abs(v) < 1e-12 else float(v)) for s, v in w.items()}
        self.explain(target, final, held)
        checks = self.checks(final)
        return RiskDecision(
            raw_levels=self.raw,
            banded_levels=self.banded,
            base_w=self.base_w(),
            proposed_w=_clean(proposed or final),
            final_w=final,
            checks=checks,
            gross=ck.gross(final),
            net=ck.net(final),
            margin_use=ck.margin_use(final, self.unit),
            stop_budget_used=self.stop_used,
            stop_budget_limit=self.cushion(),
            carry_bps_day=self.carry(final),
            ex_ante_vol=self.vol(final),
            basis=self.basis,  # type: ignore[arg-type]
            hold_reasons=self.hold_reasons,
            compliance=self.compliance,
        )

    def flatten(self) -> RiskDecision:
        """R3 HALTED/FLAT: every line (unmapped included) targets 0; still needs approval."""
        final = {s: 0.0 for s in self.order}
        base = {s: self.cur.get(s, 0.0) for s in self.order}
        self.compliance.append(
            f"R3: kill switch {self.kill_state}: flatten every line (operator approval required)"
        )
        lim = self.lim
        checks = [
            ck.check_gross(final, base, lim),
            ck.check_net(final, base, lim),
            ck.check_short_gross(final, base, lim),
            RiskCheck(rule_id="R3", name="kill_switch", passed=True, value=self.kill_state,
                      limit="flatten", detail="all targets zero"),
            ck.check_line_caps(final, base, lim),
            ck.check_margin(final, base, self.unit, lim),
        ]
        return RiskDecision(
            raw_levels=self.raw, banded_levels={s: 0.0 for s in self.managed},
            base_w=self.base_w(), proposed_w=dict(final), final_w=final, checks=checks, gross=0.0, net=0.0,
            margin_use=0.0, stop_budget_used=0.0, stop_budget_limit=self.cushion(),
            carry_bps_day=0.0, ex_ante_vol=0.0, basis="halted", hold_reasons=[],
            compliance=self.compliance,
        )

    def authority(self) -> None:
        """R10: clip into bands; keep at most max_deviations_per_cycle council deviations."""
        self.no_band: set[str] = set()
        self.banded: dict[str, float] = {}
        moved: dict[str, str] = {}
        for s in self.managed:
            band = self.bands.get(s)
            if band is None:
                self.no_band.add(s)
                self.banded[s] = self.level_of(s, self.cur.get(s, 0.0))
                continue
            raw = self.raw[s]
            value = min(max(raw, band.lo), band.hi)
            if abs(value - raw) > EPS:
                moved[s] = f"R10 level {raw:+.2f} outside band [{band.lo:+.2f}, {band.hi:+.2f}]"
            self.banded[s] = value
        devs = [s for s in self.managed if s not in self.no_band
                and abs(self.raw[s] - self.ref_level[s]) > EPS
                and abs(self.banded[s] - self.ref_level[s]) > EPS]
        cap = self.lim.authority.max_deviations_per_cycle
        if len(devs) > cap:
            keep = set(sorted(devs, key=lambda s: (-abs(self.banded[s] - self.ref_level[s]), s))[:cap])
            for s in devs:
                if s not in keep:
                    band = self.bands[s]
                    self.banded[s] = min(max(self.ref_level[s], band.lo), band.hi)
                    moved[s] = f"R10 deviation beyond the {cap} allowed per cycle"
        self.deviations = min(len(devs), cap)
        self.hold_reasons.extend(f"{s}: {why}" for s, why in sorted(moved.items()))
        for s in sorted(self.no_band):
            self.hold_reasons.append(f"{s}: R10 no band: hold current")

    def compliance_base(self) -> None:
        """R1 watch: gross above hard_max -> reduce-only projection to watch_derisk_to."""
        base = {s: self.cur.get(s, 0.0) for s in self.order}
        g = ck.gross(base)
        self.derisk = g > self.lim.gross.hard_max + EPS
        self.gross_cap = self.lim.gross.watch_derisk_to if self.derisk else self.lim.gross.proposal_max
        if self.derisk:
            v = np.array([base[s] for s in self.order])
            lo = np.array([min(0.0, base[s]) if s in self.specs else base[s] for s in self.order])
            hi = np.array([max(0.0, base[s]) if s in self.specs else base[s] for s in self.order])
            w, ok = project_or_nearest(v, lo, hi, self.lim.gross.watch_derisk_to)
            base = dict(zip(self.order, (float(x) for x in w), strict=True))
            self.compliance.append(
                f"R1: gross {g:.2f}x above hard {self.lim.gross.hard_max:.2f}x: de-risk to "
                f"{self.lim.gross.watch_derisk_to:.2f}x" + ("" if ok else " (blocked by locked lines)")
            )
        self.base = base

    def stop_distances(self) -> None:
        self.stop_d: dict[str, float | None] = {}
        for s, spec in self.specs.items():
            st = self.states.get(s)
            try:
                self.stop_d[s] = catastrophe_stop_distance(st, spec, self.p) if st else None
            except ValueError:
                self.stop_d[s] = None
        distances = {s: d for s, d in self.stop_d.items() if d is not None}
        self.stop_used = 0.0
        self._distances = distances

    def book_ratio(self) -> float | None:
        """R9 book ratio: given, else the vol-weighted mean of instrument ratios over the base and
        reference books (a proxy when no book return series is available)."""
        if self.book_vol_ratio is not None:
            return self.book_vol_ratio
        num = den = 0.0
        for s in self.managed:
            st = self.states.get(s)
            if st is None or st.ewma5_60_ratio is None or not st.sigma_ann:
                continue
            weight = (abs(self.base[s]) + abs(self.ref_w[s])) * st.sigma_ann
            num += weight * st.ewma5_60_ratio
            den += weight
        return num / den if den > EPS else None

    def frozen_reference_share(self) -> float:
        """R18: share of reference weight on data-frozen lines (market_closed does not count)."""
        total = sum(abs(self.ref_w[s]) for s in self.managed if self.specs[s].in_reference)
        if total <= EPS:
            return 0.0
        frozen = sum(abs(self.ref_w[s]) for s in self.managed
                     if self.specs[s].in_reference and self.data_frozen(s))
        return frozen / total

    def boxes(self) -> None:
        lim, p, now = self.lim, self.p, self.now
        self.lo: dict[str, float] = {}
        self.hi: dict[str, float] = {}
        self.box_notes: dict[str, list[str]] = {}
        self.pinned: set[str] = set(self.locked)
        sets = ("warn", "breaker", "event", "cooloff", "nostop", "hedged", "chase_long",
                "chase_short", "stale", "closed", "blocked")
        self.sets: dict[str, set[str]] = {k: set() for k in sets}
        book_ratio = self.book_ratio()
        self.book_blocked = book_ratio is not None and book_ratio >= lim.vol_breaker.book_ratio
        self.frozen_share = self.frozen_reference_share()
        all_hold: list[str] = []
        if self.frozen_share > lim.freshness.frozen_reference_share_max + EPS:
            all_hold.append("R18 frozen reference share")
        if self.blockers:
            all_hold.append("R20 blocker")
        for s in self.managed:
            b, u, spec, st = self.base[s], self.unit[s], self.specs[s], self.states.get(s)
            notes: list[str] = []
            if u <= EPS or s in self.no_band:
                self.lo[s] = self.hi[s] = b
                self.pinned.add(s)
                if u <= EPS and abs(b) > EPS:
                    notes.append("no unit weight: hold")
                self.box_notes[s] = notes
                continue
            band = self.bands[s]
            lo, hi = band.lo * u, band.hi * u
            limits: list[str] = []
            lev_lo, lev_hi = min(-u, b), max(u, b)  # |level| <= 1, or held where it drifted
            if lim.leverage_caps.get(self.cls[s], 1) < 2 and (hi > lev_hi + EPS or lo < lev_lo - EPS):
                lo, hi = restrict(lo, hi, lev_lo, lev_hi)
                limits.append("leverage")
            cap = lim.caps.line.get(s)
            if cap is not None and (hi > max(cap, b) + EPS or lo < min(-cap, b) - EPS):
                lo, hi = restrict(lo, hi, min(-cap, b), max(cap, b))
                limits.append("cap")
            no_add = []
            if self.kill_state == "WARN":
                no_add.append("warn")
            ratio = st.ewma5_60_ratio if st is not None else None
            if self.book_blocked or (ratio is not None and ratio >= lim.vol_breaker.instrument_ratio):
                no_add.append("breaker")
            if event_block(spec, self.events, now, p):
                no_add.append("event")
            if reentry_blocked(self.stop_hits.get(s), now, self.cls[s], p):
                no_add.append("cooloff")
            if self.stop_d[s] is None:
                no_add.append("nostop")
            if s in self.hedged:
                no_add.append("hedged")
            if no_add:
                lo, hi = restrict(lo, hi, *no_add_interval(b))
            if anti_chase_block(st, True, False, p):
                lo, hi = restrict(lo, hi, -math.inf, max(b, 0.0))
                no_add.append("chase_long")
            if anti_chase_block(st, False, True, p):
                lo, hi = restrict(lo, hi, min(b, 0.0), math.inf)
                no_add.append("chase_short")
            holds = []
            if self.data_frozen(s):
                holds.append("stale")
            if self.session_frozen(s):
                holds.append("closed")
            if self.blockers:
                holds.append("blocked")
            if holds or all_hold:
                lo = hi = b
                self.pinned.add(s)
            for key in no_add + holds:
                self.sets[key].add(s)
            self.lo[s], self.hi[s] = lo, hi
            self.box_notes[s] = [BOX_LABELS[k] for k in limits + no_add + holds] + all_hold
        for s in self.locked:
            self.lo[s] = self.hi[s] = self.base[s]

    def project(self, target: Mapping[str, float], held: Mapping[str, str]) -> dict[str, float]:
        names = self.order
        base = self.base
        fixed = [s in held or s in self.pinned for s in names]
        band_lo = np.array([base[s] if f else self.lo[s] for s, f in zip(names, fixed, strict=True)])
        band_hi = np.array([base[s] if f else self.hi[s] for s, f in zip(names, fixed, strict=True)])
        w = np.clip(np.array([target[s] for s in names]), band_lo, band_hi)
        # Hard limits outrank authority: aggregate steps may shrink a line from its band toward
        # zero (never away from it), so a [ref, ref] band cannot make a cap unreachable.
        lo = np.where(fixed, band_lo, np.minimum(band_lo, 0.0))
        hi = np.where(fixed, band_hi, np.maximum(band_hi, 0.0))
        for _, members, cap in self.groups():
            idx = [i for i, s in enumerate(names) if s in members]
            if idx:
                cap_eff = max(cap, ck.group_gross(base, members))
                w[idx], _ = project_or_nearest(w[idx], lo[idx], hi[idx], cap_eff)
        w, _ = project_or_nearest(w, lo, hi, max(self.gross_cap, ck.gross(base)))
        w = self.settle(w, lo, hi)
        w = self.vol_step(w, lo, hi, held)
        w = self.settle(w, lo, hi)
        return dict(zip(names, (float(x) for x in w), strict=True))

    def settle(self, w: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """Alternate the net (R2) and margin (R7) steps until both hold. Every step only shrinks
        |w|, so earlier caps stay satisfied; the loop is needed because an unequal soft-threshold
        can move net, and a line pushed below level 1.0 loses its leverage (margin rises)."""
        for _ in range(2 * len(self.order) + 2):
            w = self.net_bounds(w, lo, hi)
            w = self.margin_step(w, lo, hi)
            if self.net_ok(w):
                break
        return w

    def net_limits(self) -> tuple[float, float, float]:
        """(net floor, net ceiling, short-gross cap), each widened to the base book's value so a
        book already outside a limit may be held or improved, never worsened."""
        lim, base = self.lim, self.base
        nb = ck.net(base)
        return (min(lim.net.min, nb), max(lim.net.max, nb),
                max(lim.net.short_gross_max, ck.short_gross(base)))

    def net_ok(self, w: np.ndarray) -> bool:
        floor, ceiling, short_cap = self.net_limits()
        shorts = float(-np.sum(w[w < 0]))
        net = float(np.sum(w))
        return shorts <= short_cap + EPS and floor - EPS <= net <= ceiling + EPS

    def net_bounds(self, w: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """R2: shorts <= min(short cap, longs - floor); longs <= shorts + ceiling. Shrinking can
        only fix an excess; a net pushed out by a CUT is fixed by the R2 filter (it holds the cut)."""
        floor, ceiling, short_cap = self.net_limits()
        neg = w < -EPS
        if neg.any():
            longs = float(np.sum(w[w > 0]))
            cap = max(min(short_cap, longs - floor), 0.0)
            w[neg], _ = project_or_nearest(w[neg], lo[neg], np.minimum(hi[neg], 0.0), cap)
        pos = w > EPS
        if pos.any():
            shorts = float(-np.sum(w[w < 0]))
            cap = max(shorts + ceiling, 0.0)
            w[pos], _ = project_or_nearest(w[pos], np.maximum(lo[pos], 0.0), hi[pos], cap)
        return w

    def margin_step(self, w: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """R7: weighted projection with cost 1/L; L re-read after each pass (a line pushed below
        level 1.0 loses its leverage), at most one pass per line."""
        names = self.order
        cap = max(self.lim.margin_use_max, ck.margin_use(self.base, self.unit))
        for _ in range(len(names) + 1):
            current = dict(zip(names, w, strict=True))
            if ck.margin_use(current, self.unit) <= cap + EPS:
                break
            cost = np.array([
                1.0 / (ck.line_leverage(self.level_of(s, current[s])) if s in self.unit else 1)
                for s in names
            ])
            w, _ = project_or_nearest(w, lo, hi, cap, cost=cost)
        return w

    def vol_step(self, w: np.ndarray, lo: np.ndarray, hi: np.ndarray,
                 held: Mapping[str, str]) -> np.ndarray:
        """R8: shrink the movable lines by a common factor until ex-ante vol <= the cap."""
        names = self.order
        cap = max(self.lim.ex_ante_vol_hard, self.vol(self.base))
        if self.vol(dict(zip(names, w, strict=True))) <= cap + EPS:
            return w
        flex = np.array([s in self.specs and s not in held and hi[i] - lo[i] > EPS
                         for i, s in enumerate(names)])

        def at(k: float) -> np.ndarray:
            out = w.copy()
            out[flex] = np.clip(k * w[flex], lo[flex], hi[flex])
            return out

        if self.vol(dict(zip(names, at(0.0), strict=True))) > cap + EPS:
            return at(0.0)
        k_lo, k_hi = 0.0, 1.0
        for _ in range(60):
            mid = (k_lo + k_hi) / 2
            if self.vol(dict(zip(names, at(mid), strict=True))) <= cap:
                k_lo = mid
            else:
                k_hi = mid
        return at(k_lo)

    # ------------------------------------------------------------------ filters
    def gate(self, s: str, before: float, after: float, toward: bool) -> tuple[bool, float | None, str]:
        """R15 for one changed line: (passes, SR_be, reason)."""
        limit = gate_threshold(toward, self.p)
        q = self.leg_quote(s, before, after)
        st = self.states.get(s)
        sigma = st.sigma_ann if st is not None else None
        if q is None:
            return False, None, "R15 no cost quote"
        if not sigma or sigma <= 0:
            return False, None, "R15 no volatility"
        carry = q.carry_bps_day if ck.increased(before, after) else 0.0
        value = srbe(round_trip_bps(q), carry, sigma,
                     hold_days(self.cls[s], self.p, toward_reference=toward))
        if value > limit + EPS:
            return False, value, f"R15 SR_be {value:.2f} above {limit:.2f}"
        return True, value, ""

    def line_reason(self, s: str, after: float) -> str | None:
        before, u, cls = self.base[s], self.unit[s], self.cls[s]
        toward = toward_reference(before, after, self.ref_w[s])
        min_share = max(self.lim.deadband.min_nav_share, self.broker_min_share.get(s, 0.0))
        if not deadband_ok((after - before) / u, after - before, to_zero=abs(after) <= EPS,
                           crypto=cls == "crypto", min_share=min_share, policy=self.p):
            return f"R11 deadband (level step {(after - before) / u:+.2f})"
        exempt = toward and "R12" in TOWARD_REFERENCE_EXEMPT
        if not min_hold_ok(s, self.last_change.get(s), self.now, sign_flip=before * after < 0,
                           toward_reference=exempt, asset_class=cls, policy=self.p):
            return "R12 minimum hold"
        if self.lim.material_change_required and not toward and not self.material_changed:
            return "MC no new material evidence"
        ok, value, why = self.gate(s, before, after, toward)
        if value is not None:
            self.srbe_seen[s] = value
        return None if ok else why

    def line_filters(self, w: Mapping[str, float], held: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in self.changed(w, held):
            why = self.line_reason(s, w[s])
            if why:
                out[s] = why
        return out

    def trim(self, contrib: Mapping[str, float], limit: float, reason: str,
             key: Callable[[str], tuple]) -> dict[str, str]:
        """Hold lines (in `key` order) until the summed contribution fits the limit."""
        total = sum(contrib.values())
        if total <= limit + EPS:
            return {}
        out: dict[str, str] = {}
        for s in sorted((s for s, c in contrib.items() if c > EPS), key=key):
            out[s] = reason
            total -= contrib[s]
            if total <= limit + EPS:
                break
        return out

    def budget_filters(self, w: Mapping[str, float], held: Mapping[str, str]) -> dict[str, str]:
        lim = self.lim
        changed = self.changed(w, held)
        if not changed:
            return {}
        base = self.base
        toward = {s: toward_reference(base[s], w[s], self.ref_w[s]) for s in changed}
        dw = {s: abs(w[s] - base[s]) for s in changed}

        def worst_first(s: str) -> tuple:
            return (toward[s], -self.srbe_seen.get(s, 0.0), -dw[s], s)

        def smallest_first(s: str) -> tuple:
            return (toward[s], dw[s], s)

        floor, ceiling, _ = self.net_limits()
        net = ck.net(w)
        if net < floor - EPS:
            down = {s: base[s] - w[s] for s in changed if w[s] < base[s]}
            found = self.trim(down, sum(down.values()) - (floor - net), "R2 net floor", worst_first)
            if found:
                return found
        if net > ceiling + EPS:
            up = {s: w[s] - base[s] for s in changed if w[s] > base[s]}
            found = self.trim(up, sum(up.values()) - (net - ceiling), "R2 net ceiling", worst_first)
            if found:
                return found
        r13 = {s: not (toward[s] and "R13" in TOWARD_REFERENCE_EXEMPT) for s in changed}
        inc = {s: line_increase(base[s], w[s]) for s in changed if r13[s]}
        found = self.trim(inc, lim.churn.cycle_increase_max, "R13 cycle increase budget", worst_first)
        if found:
            return found
        turn = {s: dw[s] for s in changed if r13[s] and ck.increased(base[s], w[s])}
        found = self.trim(turn, lim.churn.turnover_7d_max - self.turnover_7d,
                          "R13 7-day turnover budget", worst_first)
        if not found and self.turnover_30d is not None:
            found = self.trim(turn, lim.churn.turnover_30d_max - self.turnover_30d,
                              "R13 30-day turnover budget", worst_first)
        if found:
            return found
        cost = {}
        for s in changed:
            q = self.leg_quote(s, base[s], w[s])
            cost[s] = dw[s] * (q.per_side_bps if q is not None else 0.0)
        found = self.trim(cost, lim.cost_budget.cycle_max_bps, "R14 cycle cost budget", worst_first)
        if not found and self.cost_30d_bps is not None:
            disc = {s: c for s, c in cost.items() if not toward[s]}
            found = self.trim(disc, lim.cost_budget.discretionary_30d_max_bps - self.cost_30d_bps,
                              "R14 30-day cost budget", worst_first)
        if found:
            return found
        carry_cap = max(lim.cost_budget.carry_proposal_max_bps_day, self.carry(base))
        excess = self.carry(w) - carry_cap
        if excess > EPS:
            rise = {s: max(self.carry_line(s, w[s]) - self.carry_line(s, base[s]), 0.0)
                    for s in changed}
            found = self.trim(rise, sum(rise.values()) - excess, "R14 carry budget", worst_first)
            if found:
                return found
        legs = {s: 2.0 if base[s] * w[s] < 0 else 1.0 for s in changed}
        return self.trim(legs, float(lim.proposal.max_legs), "R21 too many legs", smallest_first)

    # ------------------------------------------------------------------ reporting
    def explain(self, target: Mapping[str, float], final: Mapping[str, float],
                held: Mapping[str, str]) -> None:
        for s in self.managed:
            if s in held:
                self.hold_reasons.append(f"{s}: {held[s]}")
            elif abs(target[s] - final[s]) > EPS and self.box_notes.get(s):
                notes = ", ".join(dict.fromkeys(self.box_notes[s]))
                self.hold_reasons.append(f"{s}: limited by {notes}")
            elif abs(target[s] - final[s]) > EPS:
                self.hold_reasons.append(f"{s}: scaled to fit aggregate limits (R1/R2/R5/R7/R8)")
        self.stop_used = stop_at_risk(final, self._distances)

    def checks(self, final: Mapping[str, float]) -> list[RiskCheck]:
        lim, base = self.lim, self.base
        managed_final = {s: final[s] for s in self.managed}
        changed = self.changed(final)
        toward = {s: toward_reference(base[s], final[s], self.ref_w[s]) for s in changed}
        rows = [
            ck.check_gross(final, base, lim, derisk=self.derisk),
            ck.check_net(final, base, lim),
            ck.check_short_gross(final, base, lim),
        ]
        if self.kill_state == "WARN":
            rows.append(ck.check_no_increase("R3", "kill_switch", final, base, self.managed,
                                             limit="WARN: no increases"))
        else:
            rows.append(RiskCheck(rule_id="R3", name="kill_switch", passed=True,
                                  value=self.kill_state, limit="NORMAL"))
        opens = [s for s in self.managed if ck.increased(base[s], final[s])]
        missing = sorted(s for s in opens if self.stop_d.get(s) is None)
        rows.append(RiskCheck(
            rule_id="R4", name="catastrophe_stop", passed=not missing,
            value=round(self.stop_used, 6), limit=round(self.cushion(), 6),
            detail="stop-at-risk vs cushion to the halt line (reported only)"
            + (f"; increases without a stop {missing}" if missing else ""),
        ))
        rows.append(ck.check_no_increase("R4d", "reentry_cooloff", final, base, self.sets["cooloff"]))
        rows.append(ck.check_line_caps(final, base, lim))
        rows.extend(ck.check_group_cap(name, members, cap, final, base)
                    for name, members, cap in self.groups())
        rows.append(ck.check_leverage(managed_final, self.unit, self.cls, lim, base=base))
        rows.append(ck.check_margin(final, base, self.unit, lim))
        rows.append(ck.check_vol(self.vol(final), self.vol(base), lim, self.vol_detail))
        rows.append(ck.check_no_increase("R9", "vol_breaker", final, base, self.sets["breaker"],
                                         limit=f"instrument {lim.vol_breaker.instrument_ratio}, "
                                               f"book {lim.vol_breaker.book_ratio}"))
        final_levels = {s: self.level_of(s, final[s]) for s in self.managed}
        base_levels = {s: self.level_of(s, base[s]) for s in self.managed}
        rows.append(ck.check_authority(
            final_levels, base_levels, {s: b for s, b in self.bands.items() if s in self.specs},
            self.deviations, lim))
        rows.extend(self.verify_line_rules(final, changed, toward))
        rows.extend(self.verify_budgets(final, changed, toward))
        rows.append(ck.check_no_increase("R16", "event_block", final, base, self.sets["event"]))
        rows.append(ck.check_side_no_increase("R17", "anti_chase", final, base,
                                              self.sets["chase_long"], self.sets["chase_short"]))
        stale_moved = sorted(s for s in changed if s in self.sets["stale"])
        share_ok = self.frozen_share <= lim.freshness.frozen_reference_share_max + EPS or not changed
        rows.append(RiskCheck(
            rule_id="R18", name="data_freshness", passed=not stale_moved and share_ok,
            value=round(self.frozen_share, 6), limit=lim.freshness.frozen_reference_share_max,
            detail=f"frozen reference share; daily bars <= {lim.freshness.daily_bar_max_h:g}h"
            + (f"; stale lines changed {stale_moved}" if stale_moved else ""),
        ))
        old = []
        for s in changed:
            q = self.leg_quote(s, base[s], final[s])
            if q is None or (self.now - q.quoted_at).total_seconds() > lim.freshness.quote_max_s:
                old.append(s)
        rows.append(RiskCheck(
            rule_id="R18", name="quote_freshness", passed=not old, value=float(len(old)),
            limit=lim.freshness.quote_max_s, kind="execution",
            detail="re-quoted at approval" + (f"; stale quotes {sorted(old)}" if old else ""),
        ))
        closed_moved = sorted(s for s in changed if s in self.sets["closed"])
        rows.append(RiskCheck(rule_id="R19", name="market_open", passed=not closed_moved,
                              value=float(len(closed_moved)), limit=0.0, kind="execution",
                              detail=f"changed while closed {closed_moved}" if closed_moved else ""))
        rows.append(RiskCheck(rule_id="R20", name="blockers", passed=not (self.blockers and changed),
                              value=float(len(self.blockers)), limit=0.0,
                              detail=", ".join(self.blockers)))
        legs = sum(2 if base[s] * final[s] < 0 else 1 for s in changed)
        rows.append(ck.check_budget("R21", "legs", float(legs), float(lim.proposal.max_legs)))
        undocumented = sorted(s for s in changed if not toward[s])
        mc_ok = not (lim.material_change_required and not self.material_changed and undocumented)
        rows.append(RiskCheck(rule_id="MC", name="material_change", passed=mc_ok,
                              value=str(self.material_changed), limit="required for deviations",
                              detail=f"non-reference changes {undocumented}" if undocumented else ""))
        return sorted(rows, key=_rule_order)

    def verify_line_rules(self, final: Mapping[str, float], changed: list[str],
                          toward: Mapping[str, bool]) -> list[RiskCheck]:
        """R11, R12 and R15 re-evaluated on the final changes."""
        base, lim = self.base, self.lim
        db_bad, hold_bad, gate_bad = [], [], []
        worst = 0.0
        for s in changed:
            before, after, u = base[s], final[s], self.unit[s]
            min_share = max(lim.deadband.min_nav_share, self.broker_min_share.get(s, 0.0))
            if u <= EPS or not deadband_ok((after - before) / u, after - before,
                                           to_zero=abs(after) <= EPS, crypto=self.cls[s] == "crypto",
                                           min_share=min_share, policy=self.p):
                db_bad.append(s)
            exempt = toward[s] and "R12" in TOWARD_REFERENCE_EXEMPT
            if not min_hold_ok(s, self.last_change.get(s), self.now, sign_flip=before * after < 0,
                               toward_reference=exempt, asset_class=self.cls[s], policy=self.p):
                hold_bad.append(s)
            ok, value, _ = self.gate(s, before, after, toward[s])
            worst = max(worst, value or 0.0)
            if not ok:
                gate_bad.append(s)
        return [
            RiskCheck(rule_id="R11", name="deadband", passed=not db_bad, value=float(len(db_bad)),
                      limit=f"level {lim.deadband.level}/{lim.deadband.level_crypto} crypto, "
                            f"{lim.deadband.min_nav_share:.0%} NAV",
                      detail=f"violations {sorted(db_bad)}" if db_bad else ""),
            RiskCheck(rule_id="R12", name="min_hold", passed=not hold_bad,
                      value=float(len(hold_bad)),
                      limit=f"{lim.min_hold_days.default:g}d/{lim.min_hold_days.crypto:g}d crypto",
                      detail=f"violations {sorted(hold_bad)}" if hold_bad else ""),
            RiskCheck(rule_id="R15", name="net_of_cost_gate", passed=not gate_bad,
                      value=round(worst, 6),
                      limit=f"reference {lim.net_of_cost_gate.reference_max_srbe}, "
                            f"council {lim.net_of_cost_gate.council_max_srbe}",
                      detail=f"violations {sorted(gate_bad)}" if gate_bad else "max SR_be"),
        ]

    def verify_budgets(self, final: Mapping[str, float], changed: list[str],
                       toward: Mapping[str, bool]) -> list[RiskCheck]:
        """R13 and R14 budgets on the final changes."""
        base, lim = self.base, self.lim
        r13 = [s for s in changed if not (toward[s] and "R13" in TOWARD_REFERENCE_EXEMPT)]
        inc = sum(line_increase(base[s], final[s]) for s in r13)
        turn = sum(abs(final[s] - base[s]) for s in r13 if ck.increased(base[s], final[s]))
        cost = 0.0
        disc = 0.0
        for s in changed:
            q = self.leg_quote(s, base[s], final[s])
            c = abs(final[s] - base[s]) * (q.per_side_bps if q is not None else 0.0)
            cost += c
            disc += 0.0 if toward[s] else c
        rows = [
            ck.check_budget("R13", "cycle_increase", inc, lim.churn.cycle_increase_max),
            ck.check_budget("R13", "turnover_7d", self.turnover_7d + turn, lim.churn.turnover_7d_max,
                            "trailing discretionary turnover plus this cycle",
                            base=self.turnover_7d),
        ]
        if self.turnover_30d is not None:
            rows.append(ck.check_budget("R13", "turnover_30d", self.turnover_30d + turn,
                                        lim.churn.turnover_30d_max, base=self.turnover_30d))
        rows.append(ck.check_budget("R14", "cycle_cost_bps", cost, lim.cost_budget.cycle_max_bps))
        if self.cost_30d_bps is not None:
            rows.append(ck.check_budget("R14", "cost_30d_bps", self.cost_30d_bps + disc,
                                        lim.cost_budget.discretionary_30d_max_bps,
                                        base=self.cost_30d_bps))
        carry_f, carry_b = self.carry(final), self.carry(base)
        watch = lim.cost_budget.carry_watch_max_bps_day
        rows.append(ck.check_budget(
            "R14", "carry_bps_day", carry_f, lim.cost_budget.carry_proposal_max_bps_day,
            f"watch {watch:g} bps/day" + (" exceeded" if carry_f > watch + EPS else ""),
            base=carry_b,
        ))
        return rows


def _rule_order(check: RiskCheck) -> tuple[int, str]:
    """R1 < R2 < ... < R21 (R4d after R4), MC last; stable within a rule."""
    digits = "".join(ch for ch in check.rule_id if ch.isdigit())
    return (int(digits) if digits else 99, check.rule_id)


def _clean(w: Mapping[str, float]) -> dict[str, float]:
    return {s: (0.0 if abs(v) < 1e-12 else float(v)) for s, v in w.items()}
