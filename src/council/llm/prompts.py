"""Versioned prompt registry. Prompts are files, hashed, and every cycle records the manifest SHA.

Rules:
  - `prompts/<role>.md` starts with the line `Prompt ID: council-<role>/v<N>`; files starting with
    `_` are shared partials (e.g. `_desk_brief.md`, ID `council-desk_brief/v<N>`) pulled in with
    `{% include "_desk_brief.md" %}`. The ID line is metadata and is not sent to the model.
  - A role's `sha256` covers its own file bytes AND the bytes of every partial it includes, so a
    change to the shared brief changes every dependent role's hash (and the manifest SHA).
  - Rendering uses Jinja2 with StrictUndefined: a missing variable is an error, never blank text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jinja2

from council.paths import PROMPTS_DIR

_HEADER = re.compile(r"^Prompt ID: (council-([a-z_]+)/v(\d+))\s*$")
_INCLUDE = re.compile(r"""{%-?\s*include\s+["']([^"']+)["']\s*-?%}""")
MANIFEST_NAME = "manifest.json"


class PromptError(ValueError):
    pass


def _strip_header(source: str) -> str:
    first, sep, rest = source.partition("\n")
    return rest if _HEADER.match(first) else source


class _PromptLoader(jinja2.BaseLoader):
    """Loads prompt files from one directory and drops the `Prompt ID:` header line."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get_source(
        self, environment: jinja2.Environment, template: str
    ) -> tuple[str, str | None, Callable[[], bool] | None]:
        path = self.directory / template
        if not path.is_file() or path.parent != self.directory:
            raise jinja2.TemplateNotFound(template)
        source = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
        return _strip_header(source), str(path), lambda: path.stat().st_mtime == mtime


class PromptRegistry:
    """All prompts in one directory, with IDs, hashes and a manifest."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else PROMPTS_DIR
        self._env = jinja2.Environment(
            loader=_PromptLoader(self.directory),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            keep_trailing_newline=False,
            trim_blocks=False,
        )
        self._ids: dict[str, str] = {}
        self._bytes: dict[str, bytes] = {}
        for path in sorted(self.directory.glob("*.md")):
            name = path.stem
            data = path.read_bytes()
            first = data.decode("utf-8").partition("\n")[0]
            match = _HEADER.match(first)
            expected = name.lstrip("_")
            if not match or match.group(2) != expected:
                raise PromptError(
                    f"{path.name}: first line must be 'Prompt ID: council-{expected}/v<N>'"
                )
            self._ids[name] = match.group(1)
            self._bytes[name] = data

    # -- identity --------------------------------------------------------------------------------
    def roles(self) -> list[str]:
        """Renderable roles (partials excluded), sorted."""
        return sorted(n for n in self._ids if not n.startswith("_"))

    def names(self) -> list[str]:
        return sorted(self._ids)

    def prompt_id(self, role: str) -> str:
        self._require(role)
        return self._ids[role]

    def _includes(self, name: str, seen: set[str] | None = None) -> list[str]:
        seen = set() if seen is None else seen
        out: list[str] = []
        for inc in _INCLUDE.findall(self._bytes[name].decode("utf-8")):
            stem = Path(inc).stem
            if stem in self._bytes and stem not in seen:
                seen.add(stem)
                out.append(stem)
                out.extend(self._includes(stem, seen))
        return sorted(set(out))

    def sha256(self, role: str) -> str:
        """SHA-256 over the role file bytes plus the bytes of each partial it includes."""
        self._require(role)
        digest = hashlib.sha256(self._bytes[role])
        for inc in self._includes(role):
            digest.update(b"\0" + inc.encode() + b"\0" + self._bytes[inc])
        return digest.hexdigest()

    def manifest(self) -> dict[str, dict[str, str]]:
        """{name: {id, sha256}} for every prompt file, partials included."""
        return {name: {"id": self._ids[name], "sha256": self.sha256(name)} for name in self.names()}

    def manifest_sha(self) -> str:
        blob = json.dumps(self.manifest(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def manifest_json(self) -> str:
        return json.dumps(self.manifest(), sort_keys=True, indent=2) + "\n"

    def write_manifest(self, path: Path | str | None = None) -> Path:
        """Write `prompts/manifest.json` (or `path`). Deterministic bytes."""
        target = Path(path) if path is not None else self.directory / MANIFEST_NAME
        target.write_text(self.manifest_json(), encoding="utf-8")
        return target

    # -- rendering -------------------------------------------------------------------------------
    def render(self, role: str, **ctx: Any) -> str:
        """Render a role's system prompt. Missing variables raise (StrictUndefined)."""
        self._require(role)
        return self._env.get_template(f"{role}.md").render(**ctx).strip() + "\n"

    def _require(self, role: str) -> None:
        if role not in self._ids:
            raise PromptError(f"unknown prompt {role!r}")
