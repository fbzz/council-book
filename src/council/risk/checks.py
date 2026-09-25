"""Book aggregates and the R-rows the engine publishes. Checks VERIFY the final book independently
of the pipeline that produced it, so a bug in a filter shows up as a failed row.

Semantics of "passed" for aggregate limits: the final value is within the limit, OR it is not
worse than the base book (current positions after compliance de-risking). Positions already over a
limit may be held or reduced, never increased (the gross hard limit is handled by compliance).
Rule IDs follow the comments in policy/risk.yaml; `MC` is the material-change requirement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from council.models.risk import Band, RiskCheck
from council.risk.config import RiskLimits

EPS = 1e-9


def gross(w: Mapping[str, float]) -> float:
    return float(sum(abs(v) for v in w.values()))


def net(w: Mapping[str, float]) -> float:
    return float(sum(w.values()))


def short_gross(w: Mapping[str, float]) -> float:
    return float(sum(-v for v in w.values() if v < 0))


def long_gross(w: Mapping[str, float]) -> float:
    return float(sum(v for v in w.values() if v > 0))


def group_gross(w: Mapping[str, float], members: Iterable[str]) -> float:
    return float(sum(abs(w.get(s, 0.0)) for s in members))


def line_leverage(level: float) -> int:
    """Leverage used by a line: 1 up to level 1.0, 2 for the leverage extension above it."""
    return 2 if abs(level) > 1.0 + EPS else 1


def margin_use(w: Mapping[str, float], units: Mapping[str, float]) -> float:
    """sum(|w| / L) with L from the line's level (lines without a unit count at L = 1)."""
    total = 0.0
    for s, v in w.items():
        unit = units.get(s, 0.0)
        lev = line_leverage(v / unit) if unit > EPS else 1
        total += abs(v) / lev
    return float(total)


def not_worse(final: float, base: float, cap: float) -> bool:
    return final <= cap + EPS or final <= base + EPS


def increased(before: float, after: float) -> bool:
    """|w| grew, or the sign flipped."""
    return before * after < -EPS * EPS or abs(after) > abs(before) + EPS


def _row(rule_id: str, name: str, passed: bool, value, limit, detail: str = "",
         kind: str = "policy") -> RiskCheck:
    if isinstance(value, float):
        value = round(value, 6)
    if isinstance(limit, float):
        limit = round(limit, 6)
    return RiskCheck(rule_id=rule_id, name=name, passed=passed, value=value, limit=limit,
                     detail=detail, kind=kind)  # type: ignore[arg-type]


def check_gross(final: Mapping[str, float], base: Mapping[str, float], lim: RiskLimits,
                *, derisk: bool = False) -> RiskCheck:
    """R1: gross <= proposal_max (watch_derisk_to in a compliance de-risk cycle), or held/reduced
    from a base that is itself <= hard_max."""
    g, gb = gross(final), gross(base)
    cap = lim.gross.watch_derisk_to if derisk else lim.gross.proposal_max
    ok = g <= cap + EPS or (g <= gb + EPS and gb <= lim.gross.hard_max + EPS)
    return _row("R1", "gross", ok, g, cap,
                f"base {gb:.2%}; hard {lim.gross.hard_max:.2f}" + ("; de-risk cycle" if derisk else ""))


def check_net(final: Mapping[str, float], base: Mapping[str, float], lim: RiskLimits) -> RiskCheck:
    """R2: net inside [net.min, net.max], or no further outside than the base."""
    n, nb = net(final), net(base)
    lo, hi = min(lim.net.min, nb), max(lim.net.max, nb)
    ok = lo - EPS <= n <= hi + EPS
    return _row("R2", "net", ok, n, f"[{lim.net.min:.2f}, {lim.net.max:.2f}]", f"base {nb:.2%}")


def check_short_gross(final: Mapping[str, float], base: Mapping[str, float],
                      lim: RiskLimits) -> RiskCheck:
    """R2: short gross <= net.short_gross_max (or not worse than the base)."""
    s, sb = short_gross(final), short_gross(base)
    return _row("R2", "short_gross", not_worse(s, sb, lim.net.short_gross_max), s,
                lim.net.short_gross_max, f"base {sb:.2%}")


def check_line_caps(final: Mapping[str, float], base: Mapping[str, float],
                    lim: RiskLimits) -> RiskCheck:
    """R5: |w| <= caps.line per line (a line already over its cap may only shrink)."""
    breaches = []
    worst = 0.0
    for s, cap in lim.caps.line.items():
        f, b = final.get(s, 0.0), base.get(s, 0.0)
        worst = max(worst, abs(f) / cap if cap > 0 else 0.0)
        if abs(f) > cap + EPS and increased(b, f):
            breaches.append(s)
    return _row("R5", "line_caps", not breaches, worst, 1.0,
                "utilisation of the tightest cap" + (f"; breached {breaches}" if breaches else ""))


def check_group_cap(name: str, members: Iterable[str], cap: float,
                    final: Mapping[str, float], base: Mapping[str, float]) -> RiskCheck:
    """R5: sum(|w|) over a group <= cap (or not worse than the base)."""
    members = list(members)
    g, gb = group_gross(final, members), group_gross(base, members)
    return _row("R5", name, not_worse(g, gb, cap), g, cap, f"members {members}")


def check_leverage(final: Mapping[str, float], units: Mapping[str, float],
                   asset_class: Mapping[str, str], lim: RiskLimits,
                   base: Mapping[str, float] | None = None) -> RiskCheck:
    """R6: leverage implied by each line's level <= leverage_caps[asset class]. A line whose level
    drifted above 1.0 (its unit weight shrank) may be held or reduced, never increased."""
    base = base or {}
    breaches = []
    worst = 1
    for s, v in final.items():
        unit = units.get(s, 0.0)
        if unit <= EPS or s not in asset_class:
            continue
        lev = line_leverage(v / unit)
        worst = max(worst, lev)
        grew = increased(base.get(s, 0.0), v)
        if lev > lim.leverage_caps.get(asset_class[s], 1) and grew:
            breaches.append(s)
    return _row("R6", "leverage", not breaches, float(worst), "leverage_caps by class",
                f"breached {breaches}" if breaches else "")


def check_margin(final: Mapping[str, float], base: Mapping[str, float],
                 units: Mapping[str, float], lim: RiskLimits) -> RiskCheck:
    """R7: sum(|w|/L) <= margin_use_max (or not worse than the base)."""
    m, mb = margin_use(final, units), margin_use(base, units)
    return _row("R7", "margin_use", not_worse(m, mb, lim.margin_use_max), m, lim.margin_use_max,
                f"base {mb:.2%}")


def check_vol(vol_final: float, vol_base: float, lim: RiskLimits, detail: str) -> RiskCheck:
    """R8: ex-ante vol <= ex_ante_vol_hard (or not worse than the base)."""
    return _row("R8", "ex_ante_vol", not_worse(vol_final, vol_base, lim.ex_ante_vol_hard),
                vol_final, lim.ex_ante_vol_hard, detail)


def check_no_increase(rule_id: str, name: str, final: Mapping[str, float],
                      base: Mapping[str, float], lines: Iterable[str], limit: str = "no increase",
                      kind: str = "policy") -> RiskCheck:
    """Generic 'no |w| increase and no flip' check over a set of lines."""
    lines = sorted(set(lines))
    bad = [s for s in lines if increased(base.get(s, 0.0), final.get(s, 0.0))]
    detail = f"lines {lines}" + (f"; increased {bad}" if bad else "") if lines else "none"
    return _row(rule_id, name, not bad, float(len(bad)), limit, detail, kind)


def check_side_no_increase(rule_id: str, name: str, final: Mapping[str, float],
                           base: Mapping[str, float], long_lines: Iterable[str],
                           short_lines: Iterable[str]) -> RiskCheck:
    """R17: longs in `long_lines` may not grow; shorts in `short_lines` may not grow."""
    bad = []
    for s in set(long_lines):
        if final.get(s, 0.0) > max(base.get(s, 0.0), 0.0) + EPS:
            bad.append(s)
    for s in set(short_lines):
        if final.get(s, 0.0) < min(base.get(s, 0.0), 0.0) - EPS:
            bad.append(s)
    return _row(rule_id, name, not bad, float(len(bad)), "no chase",
                f"violations {sorted(bad)}" if bad else "")


def check_authority(final_levels: Mapping[str, float], base_levels: Mapping[str, float],
                    bands: Mapping[str, Band], deviations: int, lim: RiskLimits) -> RiskCheck:
    """R10: every level inside its band, held at the base, or pulled from the band toward zero by
    a hard limit (never beyond it); and at most max_deviations_per_cycle council deviations."""
    outside = []
    for s, band in bands.items():
        f = final_levels.get(s, 0.0)
        held = abs(f - base_levels.get(s, 0.0)) <= EPS
        inside = band.lo - EPS <= f <= band.hi + EPS
        shrunk = (-EPS <= f < band.lo) or (band.hi < f <= EPS)
        if not (inside or held or shrunk):
            outside.append(s)
    cap = lim.authority.max_deviations_per_cycle
    ok = not outside and deviations <= cap
    detail = f"deviations {deviations}" + (f"; outside band {outside}" if outside else "")
    return _row("R10", "authority", ok, float(deviations), float(cap), detail)


def check_budget(rule_id: str, name: str, value: float, limit: float, detail: str = "",
                 base: float | None = None) -> RiskCheck:
    """A simple '<= limit' budget row (optionally 'or not worse than base')."""
    ok = value <= limit + EPS if base is None else not_worse(value, base, limit)
    return _row(rule_id, name, ok, value, limit, detail)
