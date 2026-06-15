"""Tests for issue #37 (M7): no deprecated datetime.utcnow() in the package.

A shared ``utcnow()`` helper replaces ``datetime.utcnow()`` (deprecated in Python
3.12) and returns the same naive-UTC instant, so stored timestamps and the
dashboard's time-range comparisons are unchanged.
"""

import ast
from datetime import datetime, timezone
from pathlib import Path

from network_check_tool.utils import utcnow

_PKG = Path(__file__).resolve().parent.parent / "network_check_tool"


def test_utcnow_is_naive_utc_and_current():
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is None  # naive (drop-in for datetime.utcnow())
    # Within a couple of seconds of the real UTC now.
    delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - now).total_seconds())
    assert delta < 2


def test_no_deprecated_utcnow_in_package_source():
    # Flag actual `.utcnow` attribute access (calls or callable refs), parsed via
    # AST so prose mentions in docstrings/comments (e.g. utils.py's own docstring)
    # don't count.
    offenders = []
    for p in _PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                offenders.append(f"{p.relative_to(_PKG)}:{node.lineno}")
    assert offenders == [], f"deprecated .utcnow usage found in: {offenders}"
