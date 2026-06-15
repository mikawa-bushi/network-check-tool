"""Tests for netdiag.cli._unit_text (issue #8: M6 dead-code removal).

`_unit_text` was previously untested. These pin its behaviour for both the user
and system unit variants, plus a guard that the unused `user_line` local has been
removed and is not reintroduced.
"""

import inspect

from netdiag.cli import _unit_text

EXEC = "/usr/bin/netdiag watch --interval 30 --external-ip 1.1.1.1 --dns-name example.com"


def test_unit_text_user_variant():
    text = _unit_text(EXEC, system=False)
    assert "WantedBy=default.target" in text
    assert f"ExecStart={EXEC}" in text
    assert "Description=netdiag always-on network diagnosis watcher" in text


def test_unit_text_system_variant():
    text = _unit_text(EXEC, system=True)
    assert "WantedBy=multi-user.target" in text
    assert f"ExecStart={EXEC}" in text


def test_unit_text_has_no_dead_user_line():
    # M6 (issue #8): the unused `user_line = "" if system else ""` was removed.
    src = inspect.getsource(_unit_text)
    assert "user_line" not in src
