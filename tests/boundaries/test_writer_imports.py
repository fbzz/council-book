"""Structural boundaries, checked on the source tree (AST), not by convention.

- Only council/operator/ and council/execution/ may import the broker writer.
- The public-record packages (publish/, scoring/) import neither the broker nor the Keychain.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from council.paths import REPO_ROOT

SRC = REPO_ROOT / "src" / "council"
WRITER = "council.broker.etoro_write"
ALLOWED_WRITER_DIRS = ("operator", "execution")


def _module_name(path: Path, src: Path = SRC) -> str:
    rel = path.relative_to(src.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve(node: ast.ImportFrom, module: str, is_pkg: bool) -> str:
    if node.level == 0:
        return node.module or ""
    base = module.split(".") if is_pkg else module.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def imported_names(path: Path, src: Path = SRC) -> set[str]:
    """Every module a file imports: `import a.b`, `from a import b` (as a.b too), relative imports,
    and string literals passed to importlib.import_module / __import__."""
    tree = ast.parse(path.read_text(), filename=str(path))
    module = _module_name(path, src)
    is_pkg = path.name == "__init__.py"
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node, module, is_pkg)
            names.add(base)
            names |= {f"{base}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant):
                names.add(str(node.args[0].value))
    return names


def _python_files():
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _top_dir(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else ""


def test_only_operator_and_execution_import_the_writer():
    offenders = []
    for path in _python_files():
        if _top_dir(path) in ALLOWED_WRITER_DIRS or _module_name(path) == WRITER:
            continue
        if any(name == WRITER or name.startswith(WRITER + ".") for name in imported_names(path)):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"modules importing the broker writer outside operator/ and execution/: {offenders}"


@pytest.mark.parametrize("package", ["publish", "scoring"])
def test_public_record_packages_never_touch_the_broker_or_keychain(package):
    offenders = []
    for path in _python_files():
        if _top_dir(path) != package:
            continue
        for name in imported_names(path):
            if name.startswith("council.broker") or name.startswith("council.operator.keychain"):
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {name}")
    assert offenders == []


def test_detector_catches_every_import_form(tmp_path):
    samples = {
        "a.py": "import council.broker.etoro_write\n",
        "b.py": "from council.broker import etoro_write\n",
        "c.py": "from council.broker.etoro_write import place\n",
        "d.py": "import importlib\nimportlib.import_module('council.broker.etoro_write')\n",
    }
    fake_src = tmp_path / "council"
    (fake_src / "cycle").mkdir(parents=True)
    for name, code in samples.items():
        (fake_src / "cycle" / name).write_text(code)
    (fake_src / "cycle" / "e.py").write_text("from ..broker import etoro_write\n")
    found = {p.name for p in (fake_src / "cycle").glob("*.py")
             if any(n == WRITER or n.startswith(WRITER + ".") for n in imported_names(p, fake_src))}
    assert found == {"a.py", "b.py", "c.py", "d.py", "e.py"}
