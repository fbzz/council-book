"""Segmented text: model input built from structured parts (transparency-v2 §2.1, T-D2).

Every user message a role reads is a list of `Segmented` sections. A section is a list of runs:
a literal run holds code text (labels, headers, indentation, policy numbers); an item run points
at an `Item`, one value the model read, with the evidence it names and EVERY source it derives
from. The model's message is `"".join(section.text() for section in sections)`, so what is
recorded is exactly what was sent.

Rules:
  - Every data value is an item: pack values, book levels, cards, the clock, the cycle id. `lit()`
    is only for code text and policy numbers (the literal registry in the golden tests lists every
    literal a reviewer has read).
  - A derived item lists every input in `sources` (masking later takes the strictest one).
  - `licence` says how long the private copy of the text may be kept: `broker_licensed` text is
    moved out of the main capture into `licensed/` and purged after 7 days; `restricted` text
    (licensed non-broker series) is never published; `public` / `public_domain` may be.
  - Scrubbing: the old renderers scrubbed currency amounts once over the whole text. `build()`
    scrubs each piece and requires the join to equal the whole-text scrub; if a currency sign sits
    on a piece boundary the section falls back to one opaque item holding the exact scrubbed text
    (flag `desk_scrub_crossed_sections`), so the model input never changes.
Pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

ItemKind = Literal["fact", "cell", "event", "news", "card", "claim", "flag", "band", "text"]
Licence = Literal["public", "public_domain", "broker_licensed", "restricted"]
SectionKind = Literal["desk", "news_detail", "case", "transcript", "tail", "literal", "raw"]
Run = tuple[Literal["t"], str] | tuple[Literal["i"], int]

# strictest first: the licence a derived value inherits from its inputs
LICENCE_ORDER: tuple[Licence, ...] = ("broker_licensed", "restricted", "public_domain", "public")
SCRUB_CROSSED_FLAG = "desk_scrub_crossed_sections"


class Item(BaseModel):
    """One value the model read, with the exact substring it saw."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ItemKind
    ref: str                              # evidence id | card id | "bull_open:c1" | line | "cycle_id"
    line: str | None = None
    field: str | None = None              # cell field (trend, vs_sma50, cost_side, ...) or part name
    sources: tuple[str, ...] = ()
    licence: Licence = "public"
    text: str


class Segmented(BaseModel):
    """One section of a user message: literal runs and item runs, in order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str                              # "desk.full.lines", "news_detail", "case.bull_open.plain", ...
    kind: SectionKind
    runs: tuple[Run, ...] = ()
    items: tuple[Item, ...] = ()

    def text(self) -> str:
        return "".join(r[1] if r[0] == "t" else self.items[r[1]].text for r in self.runs)

    def sha256(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()

    def literals(self) -> list[str]:
        return [r[1] for r in self.runs if r[0] == "t"]

    def licensed_indices(self) -> list[int]:
        return [i for i, it in enumerate(self.items) if it.licence == "broker_licensed"]

    @property
    def opaque(self) -> bool:
        return self.key.endswith(".opaque")


def strictest(licences: Iterable[str]) -> Licence:
    """The strictest licence among `licences` ("public" when empty)."""
    seen = set(licences)
    for lic in LICENCE_ORDER:
        if lic in seen:
            return lic
    return "public"


def joined(sections: Sequence[Segmented]) -> str:
    """The exact user message for a list of sections."""
    return "".join(s.text() for s in sections)


class TextBuilder:
    """Append literals and items; `build()` returns the section (see the module rules)."""

    def __init__(self, key: str, kind: SectionKind, *, scrub: Callable[[str], str] | None = None) -> None:
        self.key = key
        self.kind: SectionKind = kind
        self._scrub = scrub
        self._runs: list[list] = []        # ["t", text] | ["i", index]
        self._items: list[Item] = []

    # -- appending ---------------------------------------------------------------------------
    def lit(self, text: str) -> TextBuilder:
        if not text:
            return self
        if self._runs and self._runs[-1][0] == "t":
            self._runs[-1][1] += text
        else:
            self._runs.append(["t", text])
        return self

    def nl(self) -> TextBuilder:
        return self.lit("\n")

    def item(
        self,
        kind: ItemKind,
        ref: str,
        text: str,
        *,
        sources: Iterable[str] = (),
        licence: Licence = "public",
        line: str | None = None,
        field: str | None = None,
    ) -> TextBuilder:
        """Append one value. An empty text still records the item (the model read "nothing")."""
        self._items.append(Item(kind=kind, ref=ref, line=line, field=field,
                                sources=tuple(dict.fromkeys(sources)), licence=licence, text=text))
        self._runs.append(["i", len(self._items) - 1])
        return self

    def join(self, parts: Sequence[Callable[[TextBuilder], object]], sep: str) -> TextBuilder:
        """Call each part-appender, with the literal `sep` between them."""
        for i, part in enumerate(parts):
            if i:
                self.lit(sep)
            part(self)
        return self

    def extend(self, other: Segmented) -> TextBuilder:
        """Append another section's runs and items (keys and kinds of `other` are dropped)."""
        for run in other.runs:
            if run[0] == "t":
                self.lit(run[1])
            else:
                it = other.items[run[1]]
                self._items.append(it)
                self._runs.append(["i", len(self._items) - 1])
        return self

    # -- output ------------------------------------------------------------------------------
    def raw_text(self) -> str:
        return "".join(r[1] if r[0] == "t" else self._items[r[1]].text for r in self._runs)

    def build(self) -> Segmented:
        runs = [(r[0], r[1]) for r in self._runs]
        items = list(self._items)
        if self._scrub is None:
            return Segmented(key=self.key, kind=self.kind, runs=tuple(runs), items=tuple(items))
        whole = self._scrub(self.raw_text())
        s_items = [it if self._scrub(it.text) == it.text
                   else it.model_copy(update={"text": self._scrub(it.text)}) for it in items]
        s_runs = [(k, self._scrub(v)) if k == "t" else (k, v) for k, v in runs]
        section = Segmented(key=self.key, kind=self.kind, runs=tuple(s_runs), items=tuple(s_items))
        if section.text() == whole:
            return section
        return opaque(self.key, self.kind, whole, items)


def opaque(key: str, kind: SectionKind, text: str, items: Sequence[Item] = ()) -> Segmented:
    """One opaque item holding `text`; it inherits every source and the strictest licence."""
    sources = tuple(dict.fromkeys(s for it in items for s in it.sources)) or ("unknown",)
    item = Item(kind="text", ref=key, field="opaque", sources=sources,
                licence=strictest(it.licence for it in items), text=text)
    base = key if key.endswith(".opaque") else f"{key}.opaque"
    return Segmented(key=base, kind=kind, runs=(("i", 0),), items=(item,))


def literal(key: str, text: str, kind: SectionKind = "literal") -> Segmented:
    """A section of code text only (tails, separators, headers)."""
    return Segmented(key=key, kind=kind, runs=(("t", text),) if text else ())


def raw(key: str, text: str, *, sources: Iterable[str] = ("unknown",)) -> Segmented:
    """A caller-supplied string whose parts are not known (legacy `desk_text=` callers)."""
    item = Item(kind="text", ref=key, field="raw", sources=tuple(sources), text=text)
    return Segmented(key=key, kind="raw", runs=(("i", 0),), items=(item,))


def prefixed(sections: Sequence[Segmented], prefix: str) -> list[Segmented]:
    """The same sections with `prefix.` in front of each key (desk variants: desk.code.*)."""
    return [s.model_copy(update={"key": f"{prefix}.{s.key}"}) for s in sections]
