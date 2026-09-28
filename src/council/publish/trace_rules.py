"""What an engine code may publish: ONE closed table (transparency-v2 §4.2, T-D10; m5-readiness M5-N).

The risk engine's private notes name the rule that held a line and, for some rules, the numbers
behind it. Some of those numbers, and some of the words, encode the account's NAV or a broker's
cost quote, so the public record never copies a note: it maps each one through this table.

| Code | Public value / limit | Public text |
|---|---|---|
| Every R11 variant (deadband, reference rule, size floor, copy floor, broker minimum) and every plan skip for size | never | `R11`: one code, no subtype, so a size-floor hold (which bounds the NAV) reads exactly like a deadband hold |
| R10 band clip, deviation cap | levels asked and band edges (public), the policy's cap | as written |
| Box limits (R3 WARN, R4, R4d, R5, R6, R9, R16, R17, R18, R19, R20, hedged, retiring) | none | as written (`BOX_NOTES`) |
| R12, MC, R2, R13, R14, R21 budget trims; "R15 no cost quote / no volatility" | none (policy words only) | as written (`FIXED_LINE_TEXTS`) |
| R15 | SR_be and its limit only when every input is public: the line's cost from the policy floors (`costs:floor`) and its volatility from Tiingo / Binance history (SR_be = (RT + carry x hold) / (sigma x hold / 365), so with sigma and hold public it gives the round trip back) | otherwise the bare `R15` |
| R15_fee, R14_fee | never (D18: the fee in bps of NAV encodes the NAV) | the bare code |
| Swing book (SW-4): `swing_book_limit:<rule>` (a whole swing entry removed by a book limit) and a swing S-rule drop `<rule>:<code>` (`swing.rules.public_code`) | never | as written (codes only) |
| Swing-book cycle flags and plan skips (SW-7b, `SWING_FLAG_CODES`): `swing_source_unavailable:<source>`, `swing_source_error:<source>:<type>`, `swing_eligibility_unverified`, `swing_paper_assumed_book`, `paper_reference_last_close`, `swing_screen_missing`, `swing_drop:reproposal_limit`, the skip `swing_book_not_live` | never | as written (codes only; a source name outside `[a-z0-9_]` becomes `other`); the site's words: `site/build.py` `SWING_FLAG_WORDS` |
| Anything else | never | its bare rule code, else `held` (fail closed) |

The same table serves the structured trace (T5b) through `public_trace_code` / `value_allowed`:
a step's value is published only when its code allows one and every declared input is public.
Pure: no I/O, and nothing here imports the engine, the ledger or the broker (a test checks that
the table covers every note the engine can write).
"""

from __future__ import annotations

import math
import re
from collections.abc import Container, Iterable

R11 = "R11"
HELD = "held"                      # a note with no recognisable rule code
NOT_ORDERED = "not_ordered"        # a plan skip whose reason is not a plain code word

# Inputs a trace value may be derived from. The first set is public; the rest never is.
PUBLIC_INPUTS: frozenset[str] = frozenset({
    "policy", "public_weights", "public_dates", "costs:floor", "open_history",
})
PRIVATE_INPUTS: frozenset[str] = frozenset({
    "broker_quote", "broker_history", "fixed_fee", "size_floor", "nav",
})

# Plan skips that happen because an order is too small for the account: published as R11.
SIZE_SKIPS: frozenset[str] = frozenset({"below_broker_minimum", "below_real_minimum"})

# The engine's box notes (`risk.engine.BOX_LABELS`, the R18 share holds and the no-unit note).
BOX_NOTES: frozenset[str] = frozenset({
    "R3 WARN no adds", "R9 vol breaker", "R16 event window", "R4d post-stop cool-off",
    "R4 no catastrophe stop", "hedged line", "R17 anti-chase", "R18 frozen data",
    "R19 market closed", "R20 blocker", "R5 line cap", "R6 leverage cap",
    "retiring line: reduce only", "R18 frozen data: reduce only", "R18 frozen reference share",
    "R18 frozen satellite share", "no unit weight: hold",
})
# Per-line notes with no number but the policy's own words.
FIXED_LINE_TEXTS: frozenset[str] = frozenset({
    "R10 no band: hold current", "R12 minimum hold", "MC no new material evidence",
    "R15 no cost quote", "R15 no volatility",
    "R2 net floor", "R2 net ceiling",
    "R13 cycle increase budget", "R13 7-day turnover budget", "R13 30-day turnover budget",
    "R14 cycle cost budget", "R14 30-day cost budget", "R14 carry budget",
    "R21 too many legs", "R21 too many legs in total",
    "scaled to fit aggregate limits (R1/R2/R5/R7/R8)",
    "deadband",                    # the older code form some records carry (no number)
})
# Notes that name no line.
FIXED_GENERAL_TEXTS: frozenset[str] = frozenset({"no broker snapshot: current book taken as flat"})

# Rule codes the engine uses, and the box keys a trace step may carry (`risk.engine.BOX_LABELS`).
RULE_CODES: frozenset[str] = frozenset({
    "R1", "R2", "R3", "R4", "R4d", "R5", "R6", "R7", "R8", "R9", "R10", "R11", "R12", "R13", "R14",
    "R14_fee", "R15", "R15_fee", "R16", "R17", "R18", "R19", "R20", "R21", "MC",
})
BOX_CODES: frozenset[str] = frozenset({
    "warn", "breaker", "event", "cooloff", "nostop", "hedged", "chase_long", "chase_short", "stale",
    "closed", "blocked", "cap", "leverage", "retiring", "stale_reduce", "R18_core", "R18_satellite",
})

# Which inputs a code's value may come from for the value to be public; None = never.
_BOXED = frozenset({"policy", "public_weights"})
_VALUE_INPUTS: dict[str, frozenset[str] | None] = {
    "R11": None, "R14_fee": None, "R15_fee": None, "MC": None,
    "R10": _BOXED,
    "R12": frozenset({"policy", "public_dates"}),
    "R15": frozenset({"policy", "public_weights", "costs:floor", "open_history"}),
    **{code: _BOXED for code in ("R1", "R2", "R3", "R4", "R4d", "R5", "R6", "R7", "R8", "R9",
                                 "R13", "R14", "R16", "R17", "R18", "R19", "R20", "R21")},
    **{code: _BOXED for code in BOX_CODES},
}

_CODE = re.compile(r"^(R\d{1,2}[a-z]?(?:_fee)?|MC)(?![A-Za-z0-9])")
_LINE_NOTE = re.compile(r"^([A-Z0-9](?:[A-Z0-9_.]{0,22}[A-Z0-9])?): (.+)$")
_R10_CLIP = re.compile(
    r"^R10 level [+-]\d{1,2}\.\d{2} outside band \[[+-]\d{1,2}\.\d{2}, [+-]\d{1,2}\.\d{2}\]$")
_R10_CAP = re.compile(r"^R10 deviation beyond the \d{1,2} allowed per cycle$")
_R15_VALUE = re.compile(r"^R15 SR_be \d{1,4}\.\d{2} above \d{1,4}\.\d{2}$")
_SWING_LIMIT = re.compile(r"^swing_book_limit:R\d{1,2}$")
_SWING_RULE = re.compile(r"^(?:S\d{1,2}|SB\d{1,2}|R20):[a-z][a-z_]{0,39}$")
_SKIP_NOTE = re.compile(r"^([A-Za-z0-9_.]{1,40}): (.+)$")
_SKIP_WORD = re.compile(r"^[a-z][a-z_ ]{0,39}$")


# Swing-book cycle flags (and the planner skip `swing_book_not_live`): the public table keys. A key
# ending in ":*" stands for the source-named family; the site gives each key its words.
SWING_FLAG_CODES: frozenset[str] = frozenset({
    "swing_source_unavailable:*", "swing_source_error:*", "swing_eligibility_unverified",
    "swing_paper_assumed_book", "paper_reference_last_close", "swing_screen_missing",
    "swing_drop:reproposal_limit", "swing_book_not_live",
})
_SWING_SOURCE_FLAG = re.compile(r"^(swing_source_(?:unavailable|error)):(.*)$")
_SWING_SOURCE_PART = re.compile(r"^[a-z0-9_]{1,40}$")
_SWING_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")


def swing_flag_key(flag: str | None) -> str | None:
    """The `SWING_FLAG_CODES` key of a swing flag, else None."""
    raw = _squash(flag)
    m = _SWING_SOURCE_FLAG.match(raw)
    if m is not None:
        return f"{m.group(1)}:*"
    return raw if raw in SWING_FLAG_CODES else None


def public_swing_flag(flag: str) -> str:
    """A swing source flag as a public code: `swing_source_unavailable:<source>` and
    `swing_source_error:<source>:<type>` keep a plain source word and exception type name (else
    `other`); every other flag is returned unchanged."""
    raw = _squash(flag)
    m = _SWING_SOURCE_FLAG.match(raw)
    if m is None:
        return raw
    family, rest = m.groups()
    source, _, kind = rest.partition(":")
    source = source if _SWING_SOURCE_PART.match(source) else "other"
    if family.endswith("unavailable"):
        return f"{family}:{source}"
    return f"{family}:{source}:{kind if _SWING_ERROR_TYPE.match(kind) else 'other'}"


def _squash(text: str | None) -> str:
    return " ".join(str(text or "").split())


def code_of(text: str | None) -> str | None:
    """The rule code a note starts with ("R11", "R4d", "R15_fee", "MC"), else None."""
    m = _CODE.match(_squash(text))
    return m.group(1) if m else None


def public_code(text: str | None, default: str = HELD) -> str:
    """The bare public code of a note or code: every R11 variant is `R11`; any other rule code or
    box key stands as itself; anything without one is `default`."""
    raw = _squash(text)
    code = code_of(raw) or (raw if raw in BOX_CODES else None)
    if code is None:
        return default
    return R11 if code.startswith(R11) else code


def listed(note: str | None) -> bool:
    """True when a per-line note matches a row of the table other than the fail-closed fallback
    (R11 variants, fixed texts, R10 texts, R15 with a value, box notes, the fee codes)."""
    raw = _squash(note)
    if raw.startswith("limited by "):
        return all(n in BOX_NOTES for n in raw[len("limited by "):].split(", "))
    code = code_of(raw)
    return bool(
        (code is not None and (code.startswith(R11) or code in ("R14_fee", "R15_fee")))
        or raw in FIXED_LINE_TEXTS or raw in FIXED_GENERAL_TEXTS or raw in BOX_NOTES
        or _R10_CLIP.match(raw) or _R10_CAP.match(raw) or _R15_VALUE.match(raw)
        or swing_note(raw)
    )


def swing_note(note: str | None) -> bool:
    """A swing-book note that stands as written (codes only, no number)."""
    raw = _squash(note)
    return bool(_SWING_LIMIT.match(raw) or _SWING_RULE.match(raw))


def _public_note(note: str, value_ok: bool) -> str:
    code = code_of(note)
    if code is not None and code.startswith(R11):
        return R11
    if note in FIXED_LINE_TEXTS or _R10_CLIP.match(note) or _R10_CAP.match(note) or swing_note(note):
        return note
    if _R15_VALUE.match(note):
        return note if value_ok else "R15"
    if note.startswith("limited by "):
        parts = [n if n in BOX_NOTES else public_code(n, "other limit")
                 for n in note[len("limited by "):].split(", ")]
        return "limited by " + ", ".join(dict.fromkeys(parts))
    return public_code(note)


def public_hold_reason(text: str | None, *, value_lines: Container[str] = frozenset()) -> str:
    """One private hold reason in its public form. `value_lines` are the lines whose R15 SR_be may
    be shown (every input public, see the module table); an R15 note on any other line becomes
    the bare `R15`. "SMH: R11 below the minimum trade size" -> "SMH: R11"."""
    raw = _squash(text)
    m = _LINE_NOTE.match(raw)
    if m is None:
        return raw if raw in FIXED_GENERAL_TEXTS else public_code(raw)
    line, note = m.groups()
    return f"{line}: {_public_note(note, line in value_lines)}"


def public_plan_skip(text: str | None) -> str:
    """One planner skip note in its public form: a size skip (below the broker's or the real
    trade floor) is `R11`; a plain code word stands; anything else is `not_ordered`. A trailing
    parenthesis (older notes carried amounts there) is dropped."""
    raw = _squash(text)
    m = _SKIP_NOTE.match(raw)
    if m is None:
        return NOT_ORDERED
    line, rest = m.groups()
    reason = rest.split(" (", 1)[0].strip()
    if reason in SIZE_SKIPS:
        return f"{line}: {R11}"
    if _SKIP_WORD.match(reason):
        return f"{line}: {reason}"
    return f"{line}: {NOT_ORDERED}"


def public_check_value(rule_id: str, value: float | str | None, *, r15_public: bool) -> float | str | None:
    """A risk check's value in public: R15's (the largest SR_be of the changed lines) only when
    every input of every line is public (`r15_public`); fee codes and counts stand as they are."""
    if rule_id == "R15" and isinstance(value, int | float) and not isinstance(value, bool) and not r15_public:
        return None
    return value


# ------------------------------------------------------------------------------ trace steps (T5b)
def public_trace_code(code: str, inputs: Iterable[str]) -> str:
    """A trace step's public code: R11 variants are `R11`; a bound derived from a size floor, a fee
    or the NAV is `R11` too (§4.2: such a bound would give the floor back); unknown codes are
    bare."""
    base = public_code(code)
    if set(inputs) & {"size_floor", "fixed_fee", "nav"} and base not in ("R14_fee", "R15_fee"):
        return R11
    return base


def value_allowed(code: str, inputs: Iterable[str]) -> bool:
    """True when a step with this code may publish its value and limit: the code allows a value
    and every declared input is one of the code's public inputs (no inputs declared: never)."""
    used = set(inputs)
    allowed = _VALUE_INPUTS.get(public_trace_code(code, used))
    return allowed is not None and bool(used) and used <= allowed


def public_step_values(code: str, value: float | None, limit: float | None,
                       inputs: Iterable[str]) -> tuple[str, float | None, float | None]:
    """(public code, value, limit) of one trace step: the numbers survive only where
    `value_allowed` says so, and only when finite."""
    used = tuple(inputs)
    public = public_trace_code(code, used)
    if not value_allowed(public, used):
        return public, None, None

    def finite(v: float | None) -> float | None:
        return v if v is not None and math.isfinite(float(v)) else None

    return public, finite(value), finite(limit)
