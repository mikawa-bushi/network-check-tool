"""Every text file the packages read or write names its encoding.

Without one, Python uses the locale's: cp1252 on an English Windows, which can't
hold the Japanese in alerts, logs and snapshots. CI on Linux never sees that.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TEXT_IO = {"open", "read_text", "write_text", "FileHandler"}
_NOT_FILES = {"os", "webbrowser", "api", "self.api"}  # api: the Windows ICMP handle


def _unencoded_text_io(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        if ast.unparse(fn.value) in _NOT_FILES:
            return False
        name = fn.attr
    else:
        name = getattr(fn, "id", None)
    if name not in _TEXT_IO or any(k.arg == "encoding" for k in call.keywords):
        return False
    modes = [a.value for a in call.args if isinstance(a, ast.Constant)]
    modes += [k.value.value for k in call.keywords if k.arg == "mode"]
    return not any(isinstance(m, str) and "b" in m for m in modes)


def test_every_text_file_access_names_an_encoding():
    offenders = [
        f"{path.relative_to(_ROOT)}:{node.lineno}"
        for pkg in ("netdiag", "network_check_tool")
        for path in sorted((_ROOT / pkg).rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and _unencoded_text_io(node)
    ]
    assert offenders == []
