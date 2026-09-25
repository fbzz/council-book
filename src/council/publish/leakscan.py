"""Leak scan: the last gate before anything becomes public. Fail closed on any finding.

Rules:
- VALUE patterns: money amounts, 7+ digit ids, UUIDs, IPv4, e-mails, local home paths, GitHub /
  API tokens, JWTs, bearer credentials, broker header names and private artifact links.
- KEY denylist (structured documents only): a key naming money, prices, units, margin or a
  broker/account/order/request id is refused unless it ends in `_pct`, `_bp` or `_x`.
- CANARIES: exact private values (NAV, cash, ...) in several formats (1234.56, 1,234.56, 1234).
- LICENSED TEXT: any shared 8-word n-gram with a licensed text (broker feed items).

Findings never echo the secret: excerpts are masked.

CLI: `python -m council.publish.leakscan [--canary V ...] <paths...>` exits 1 on findings.
Canaries may also come from `COUNCIL_LEAK_CANARIES` (values separated by `;` or newlines).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.where}: {self.rule} ({self.excerpt})"


# ------------------------------------------------------------------------------ value patterns
VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dollar_amount", re.compile(r"\$\s?\d")),
    ("currency_amount", re.compile(r"\b(?:USD|EUR|GBP)\s?\d")),
    ("currency_suffix", re.compile(r"\d[\d,]*\.\d{2}\s?(?:USD|EUR|GBP)\b")),
    ("euro_pound_amount", re.compile(r"[€£]\s?\d|\d\s?[€£]")),
    # 7+ digit numbers look like position/order/account ids. Evidence-id hashes (N:1234abcd) and
    # hex digests are not ids and are excluded by the look-behind.
    ("long_number", re.compile(r"(?<![\w.])(?<!\b[NSMECFVK]:)\d{7,}(?![\w])")),
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("local_path", re.compile(r"/Users/|/home/[a-z]|\\Users\\|/var/folders/")),
    ("github_token", re.compile(r"\bgh[opsu]_[A-Za-z0-9]")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]")),
    ("api_secret", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("broker_header", re.compile(r"(?i)\bx-user-key\b|\bx-api-key\s*[:=]")),
    ("private_artifact", re.compile(r"(?i)claude\.ai/code/artifact")),
)

# ------------------------------------------------------------------------------ key denylist
_ALLOWED_SUFFIXES = ("_pct", "_bp", "_x")
# Substring terms are unambiguous; matched on the key with separators removed.
_KEY_SUBSTRINGS = (
    "amount", "equity", "balance", "notional", "pnl", "mirror", "gcid", "positionid", "orderid",
    "requestid", "account", "cash", "price", "margin",
)
# Segment terms are short or ambiguous ("rate" in "generated", "ip" in "skipped"); matched as a
# whole snake_case segment.
_KEY_SEGMENTS = frozenset({"usd", "rate", "rates", "units", "token", "ip", "ips", "cid"})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake(key: str) -> str:
    return _CAMEL.sub("_", key).lower()


def key_denied(key: str) -> bool:
    """True if a document key names private money/position data (unless it is a %/bp/x field)."""
    snake = _snake(key)
    if snake.endswith(_ALLOWED_SUFFIXES):
        return False
    compact = re.sub(r"[^a-z0-9]", "", snake)
    if any(term in compact for term in _KEY_SUBSTRINGS):
        return True
    segments = [s for s in re.split(r"[^a-z0-9]+", snake) if s]
    return any(seg in _KEY_SEGMENTS for seg in segments)


# ------------------------------------------------------------------------------ canaries
def canary_patterns(value: str | float | int) -> list[re.Pattern[str]]:
    """Regexes for one private value. Numbers match as 1234.56, 1,234.56 and (when the integer
    part has 4+ digits) 1234 / 1,234; strings match case-insensitively as written."""
    if isinstance(value, bool):
        raise TypeError("a canary cannot be a bool")
    number: float | None = None
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = value.strip()
        try:
            number = float(text.replace(",", ""))
        except ValueError:
            if len(text) < 4:
                raise ValueError("text canaries must be at least 4 characters") from None
            return [re.compile(re.escape(text), re.IGNORECASE)]
    number = abs(number)
    variants = {f"{number:.2f}", f"{number:,.2f}"}
    whole = int(number)
    if whole >= 1000 or number == whole:
        variants |= {f"{whole}", f"{whole:,}", f"{round(number):d}", f"{round(number):,d}"}
    if number != whole:
        variants.add(f"{number:.1f}")
        variants.add(f"{number:,.1f}")
    return [
        re.compile(r"(?<![\d.,])" + re.escape(v) + r"(?![\d])")
        for v in sorted(variants, key=len, reverse=True)
        if len(v.replace(",", "").replace(".", "")) >= 3
    ]


# ------------------------------------------------------------------------------ n-grams
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def ngrams(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def ngram_overlap(text: str, licensed_texts: Iterable[str], n: int = 8) -> list[str]:
    """Shared n-word sequences between `text` and any licensed text (lower-cased words)."""
    mine = ngrams(text, n)
    if not mine:
        return []
    shared: set[tuple[str, ...]] = set()
    for other in licensed_texts:
        shared |= mine & ngrams(other, n)
    return sorted(" ".join(g) for g in shared)


# ------------------------------------------------------------------------------ scanning
def _mask(snippet: str) -> str:
    snippet = snippet.strip()
    if len(snippet) <= 2:
        return "*" * len(snippet)
    return snippet[:2] + "*" * min(len(snippet) - 2, 12)


class _Scanner:
    def __init__(self, canaries: Sequence[str | float | int], licensed: Sequence[str], n: int):
        self.canaries = [p for c in canaries for p in canary_patterns(c)]
        self.licensed_grams: set[tuple[str, ...]] = set()
        for text in licensed:
            self.licensed_grams |= ngrams(text, n)
        self.n = n
        self.findings: list[Finding] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, rule: str, where: str, excerpt: str) -> None:
        key = (rule, where, excerpt)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(Finding(rule, where or "<root>", _mask(excerpt)))

    def text(self, value: str, where: str) -> None:
        for rule, pattern in VALUE_PATTERNS:
            for match in pattern.finditer(value):
                self.add(rule, where, match.group(0))
        for pattern in self.canaries:
            for match in pattern.finditer(value):
                self.add("canary", where, match.group(0))
        if self.licensed_grams:
            for gram in ngrams(value, self.n) & self.licensed_grams:
                self.add("licensed_text", where, " ".join(gram))

    def number(self, value: float | int, where: str) -> None:
        if isinstance(value, int) and abs(value) >= 1_000_000:
            self.add("long_number", where, str(value))
        elif isinstance(value, float) and abs(value) >= 1_000_000:
            self.add("long_number", where, f"{value:.0f}")
        rendered = [repr(value), str(value)]
        if isinstance(value, float):
            rendered += [f"{value:.2f}", f"{value:,.2f}"]
        if any(pattern.search(text) for text in rendered for pattern in self.canaries):
            self.add("canary", where, rendered[0])        # one finding per value, whatever the format

    def walk(self, obj: Any, where: str) -> None:
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(mode="json")
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                key_s = str(key)
                path = f"{where}.{key_s}" if where else key_s
                if key_denied(key_s):
                    self.add("denied_key", path, key_s)
                self.text(key_s, path)
                self.walk(value, path)
        elif isinstance(obj, list | tuple):
            for i, value in enumerate(obj):
                self.walk(value, f"{where}[{i}]")
        elif isinstance(obj, bool) or obj is None:
            return
        elif isinstance(obj, int | float):
            self.number(obj, where)
        elif isinstance(obj, str):
            self.text(obj, where)
        else:
            self.text(str(obj), where)


def scan(
    obj_or_text: Any,
    *,
    canaries: Sequence[str | float | int] = (),
    licensed_texts: Sequence[str] = (),
    n: int = 8,
    where: str = "",
) -> list[Finding]:
    """Scan a string, a JSON-like object or a pydantic model. Returns findings (empty = clean)."""
    scanner = _Scanner(canaries, licensed_texts, n)
    if isinstance(obj_or_text, str | bytes):
        text = obj_or_text.decode("utf-8", "replace") if isinstance(obj_or_text, bytes) else obj_or_text
        scanner.text(text, where)
    else:
        scanner.walk(obj_or_text, where)
    return scanner.findings


_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf",
    ".gz", ".zip", ".pyc",
})


def scan_bytes(
    name: str,
    data: bytes,
    *,
    canaries: Sequence[str | float | int] = (),
    licensed_texts: Sequence[str] = (),
) -> list[Finding]:
    """Scan one file's content. JSON / JSONL are scanned structurally (keys and values); anything
    that fails to parse is scanned as text, which is stricter, never looser."""
    suffix = Path(name).suffix.lower()
    if suffix in _BINARY_SUFFIXES:
        return []
    text = data.decode("utf-8", "replace")
    kw = {"canaries": canaries, "licensed_texts": licensed_texts}
    if suffix == ".json":
        try:
            return scan(json.loads(text), where=name, **kw)
        except json.JSONDecodeError:
            return scan(text, where=name, **kw)
    if suffix == ".jsonl":
        findings: list[Finding] = []
        for i, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                findings += scan(json.loads(line), where=f"{name}:{i}", **kw)
            except json.JSONDecodeError:
                findings += scan(line, where=f"{name}:{i}", **kw)
        return findings
    return scan(text, where=name, **kw)


_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"})


def iter_files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not (_SKIP_DIRS & set(child.relative_to(path).parts)):
                    yield child
        elif path.is_file():
            yield path


def scan_paths(
    paths: Iterable[Path],
    *,
    canaries: Sequence[str | float | int] = (),
    licensed_texts: Sequence[str] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    for file in iter_files(paths):
        findings += scan_bytes(str(file), file.read_bytes(), canaries=canaries, licensed_texts=licensed_texts)
    return findings


def env_canaries(environ: Mapping[str, str] | None = None) -> list[str]:
    raw = (environ if environ is not None else os.environ).get("COUNCIL_LEAK_CANARIES", "")
    return [c.strip() for c in re.split(r"[;\n]", raw) if c.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m council.publish.leakscan", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--canary", action="append", default=[], help="private value that must not appear")
    args = parser.parse_args(argv)
    canaries = [*args.canary, *env_canaries()]
    try:
        for c in canaries:
            canary_patterns(c)
    except (TypeError, ValueError) as exc:
        print(f"leakscan: bad canary: {exc}", file=sys.stderr)
        return 2
    existing = []
    for p in args.paths:
        if p.exists():
            existing.append(p)
        else:
            print(f"leakscan: skip (missing): {p}", file=sys.stderr)
    findings = scan_paths(existing, canaries=canaries)
    for f in findings:
        print(f"LEAK {f}")
    if findings:
        print(f"leakscan: {len(findings)} finding(s); nothing may be published", file=sys.stderr)
        return 1
    count = sum(1 for _ in iter_files(existing))
    print(f"leakscan: clean ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
