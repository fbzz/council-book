"""AST pin: etoro_read.py defines no write-like methods and cannot reach a write route."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import council.broker.etoro_read as etoro_read

SOURCE = Path(etoro_read.__file__).read_text()
TREE = ast.parse(SOURCE)

READ_METHODS = {
    "get_json", "post_read", "pnl", "portfolio", "eligibility", "costs", "candles", "rates",
    "feeds_news", "agent_portfolios", "order_lookup", "close_order_info", "disconnect",
}
WRITE_LIKE = re.compile(
    r"(^|_)(open|place|submit|execute|execution|trade|buy|sell|short|patch|put|delete|cancel|"
    r"modify|edit|update|create|write|set|flatten|close_position|market_close|clear)(_|$)",
    re.IGNORECASE,
)


def _class(name: str) -> ast.ClassDef:
    return next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == name)


def test_public_methods_are_exactly_the_read_set():
    methods = {
        n.name for n in _class("EtoroReadClient").body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    }
    assert methods == READ_METHODS


def test_no_function_anywhere_has_a_write_like_name():
    names = [n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    offenders = [n for n in names if WRITE_LIKE.search(n.strip("_"))]
    assert offenders == []


def test_no_write_verbs_or_write_routes_in_literals():
    strings = [n.value for n in ast.walk(TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any(s.upper() in ("PATCH", "PUT", "DELETE") for s in strings)
    assert not any("/execution/" in s or "/trading/positions" in s for s in strings)


def test_post_is_only_sent_from_post_read():
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef):
            posts = [c for c in ast.walk(node) if isinstance(c, ast.Constant) and c.value == "POST"]
            assert not posts or node.name == "post_read", node.name


def test_module_never_imports_the_writer():
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("etoro_write" in m for m in imported)


def test_runtime_client_has_no_write_attributes():
    for name in ("open_order", "close_position", "patch_stop_loss", "place_trade", "execute"):
        assert not hasattr(etoro_read.EtoroReadClient, name)
