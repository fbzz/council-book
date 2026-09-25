"""Euclidean projection onto a signed box intersected with a (weighted) L1 ball.

Rule: the risk engine never rescales a book by hand. It projects the council's target onto
{lo <= w <= hi, sum(c_i * |w_i|) <= cap}, which moves every coordinate toward zero by the same
soft-threshold lambda (scaled by c_i) and then clips it into its box. The KKT conditions of the
separable problem  min 1/2||w - v||^2 + lambda * sum(c_i |w_i|)  s.t. lo <= w <= hi  give exactly

    w_i(lambda) = clip(sign(v_i) * max(|v_i| - lambda * c_i, 0), lo_i, hi_i)

and sum(c_i |w_i(lambda)|) is non-increasing in lambda, so bisection finds the smallest lambda that
satisfies the cap. Ported from the idea of `capped_simplex_projection` (bisection on a shift) in
the lab's risk module, generalised from a simplex to a signed box plus L1 ball.
"""

from __future__ import annotations

import numpy as np

_TOL = 1e-12


def _as_arrays(
    v: np.ndarray, lo: np.ndarray, hi: np.ndarray, cost: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    v = np.asarray(v, dtype=np.float64)
    lo = np.asarray(lo, dtype=np.float64)
    hi = np.asarray(hi, dtype=np.float64)
    c = np.ones_like(v) if cost is None else np.asarray(cost, dtype=np.float64)
    if v.ndim != 1 or v.shape != lo.shape or v.shape != hi.shape or v.shape != c.shape:
        raise ValueError("projection arrays must be 1-D with identical shapes")
    if not (np.all(np.isfinite(v)) and np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        raise ValueError("projection inputs must be finite")
    if np.any(lo > hi + _TOL):
        raise ValueError("box lower bound above upper bound")
    if np.any(~np.isfinite(c)) or np.any(c <= 0):
        raise ValueError("L1 cost weights must be positive and finite")
    return v, lo, np.maximum(hi, lo), c


def weighted_l1(w: np.ndarray, cost: np.ndarray | None = None) -> float:
    """sum(c_i * |w_i|); c defaults to ones (plain gross)."""
    w = np.asarray(w, dtype=np.float64)
    c = np.ones_like(w) if cost is None else np.asarray(cost, dtype=np.float64)
    return float(np.sum(c * np.abs(w)))


def min_l1_in_box(lo: np.ndarray, hi: np.ndarray, cost: np.ndarray | None = None) -> float:
    """Smallest reachable sum(c_i |w_i|) inside the box: each coordinate at the point nearest 0."""
    lo = np.asarray(lo, dtype=np.float64)
    hi = np.asarray(hi, dtype=np.float64)
    return weighted_l1(np.clip(0.0, lo, hi), cost)


def project_signed_box_l1(
    v: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    gross_cap: float,
    *,
    cost: np.ndarray | None = None,
    iters: int = 200,
) -> np.ndarray:
    """Project `v` onto {lo <= w <= hi, sum(cost * |w|) <= gross_cap}.

    - Unchanged (just clipped) when clip(v, lo, hi) already satisfies the cap.
    - Feasible: the result always lies in the box and satisfies the cap (bisection keeps the
      feasible end of the bracket).
    - Idempotent: projecting the output again returns it unchanged.
    Raises ValueError when the box alone makes the cap unreachable.
    """
    v, lo, hi, c = _as_arrays(v, lo, hi, cost)
    if gross_cap < 0 or np.isnan(gross_cap):
        raise ValueError("gross cap must be non-negative")

    clipped = np.clip(v, lo, hi)
    if weighted_l1(clipped, c) <= gross_cap:
        return clipped
    if min_l1_in_box(lo, hi, c) > gross_cap + _TOL:
        raise ValueError("infeasible: the box alone exceeds the L1 cap")

    def at(lam: float) -> np.ndarray:
        shrunk = np.sign(v) * np.maximum(np.abs(v) - lam * c, 0.0)
        return np.clip(shrunk, lo, hi)

    lam_lo, lam_hi = 0.0, float(np.max(np.abs(v) / c)) + 1.0
    for _ in range(iters):
        mid = (lam_lo + lam_hi) / 2
        if weighted_l1(at(mid), c) > gross_cap:
            lam_lo = mid
        else:
            lam_hi = mid
        if lam_hi - lam_lo <= 1e-15 * max(1.0, lam_hi):
            break
    return at(lam_hi)


def project_or_nearest(
    v: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    gross_cap: float,
    *,
    cost: np.ndarray | None = None,
) -> tuple[np.ndarray, bool]:
    """Like `project_signed_box_l1`, but never raises on an unreachable cap: it returns the box
    point nearest zero (the smallest reachable L1) and `False`. The engine then reports the
    failed check instead of crashing a cycle."""
    try:
        return project_signed_box_l1(v, lo, hi, gross_cap, cost=cost), True
    except ValueError:
        v_arr, lo_arr, hi_arr, _ = _as_arrays(v, lo, hi, cost)
        return np.clip(np.zeros_like(v_arr), lo_arr, hi_arr), False
