"""Sector of a US stock: its SEC SIC code mapped to Fama-French 12, FROZEN by the pre-registered
stock-sleeve study (docs/stock-sleeve-spec.md section 3, tag `stock-sleeve-spec`).

Moved unchanged from scripts/stock_sleeve_study.py. The study's universe filter and the live
quarterly rank import this map; nothing re-implements it, and a change here needs a new
pre-registration. The peer groups of the sector score (`council.stocks.score`) are these sectors;
FF12 "Money" is excluded from the universe.
"""

from __future__ import annotations

from typing import Any

# Ken French data library, Siccodes12 (Siccodes12.zip, 2020-01-08). Code 12 "Other" is every SIC not
# listed. Embedded so the study does not depend on the zip; a test checks it against the file.
FF12_RANGES: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("NoDur", ((100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989))),
    ("Durbl", ((2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716),
               (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999))),
    ("Manuf", ((2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629),
               (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799),
               (3830, 3839), (3860, 3899))),
    ("Enrgy", ((1200, 1399), (2900, 2999))),
    ("Chems", ((2800, 2829), (2840, 2899))),
    ("BusEq", ((3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379))),
    ("Telcm", ((4800, 4899),)),
    ("Utils", ((4900, 4949),)),
    ("Shops", ((5000, 5999), (7200, 7299), (7600, 7699))),
    ("Hlth", ((2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099))),
    ("Money", ((6000, 6999),)),
)


def ff12(sic: Any) -> str | None:
    """Fama-French 12 industry of a SEC SIC code; None when the SIC is missing or zero."""
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    if code <= 0:
        return None
    for name, ranges in FF12_RANGES:
        if any(lo <= code <= hi for lo, hi in ranges):
            return name
    return "Other"


def parse_siccodes12(text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse the Ken French Siccodes12.txt layout into {short name: [(lo, hi), ...]}."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].isdigit() and len(parts) > 1 and not parts[1][0].isdigit():
            current = parts[1]
            out[current] = []
        elif current is not None and "-" in parts[0]:
            lo, hi = parts[0].split("-")
            out[current].append((int(lo), int(hi)))
    return out
