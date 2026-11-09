"""Tests for issue #31 (M10 phase 4): CLI command coverage.

Covers the ``netdiag`` CLI's deterministic, no-network commands via click's
CliRunner.
"""

import re

from click.testing import CliRunner

from netdiag.cli import cli as netdiag_cli


def test_netdiag_version():
    result = CliRunner().invoke(netdiag_cli, ["--version"])
    assert result.exit_code == 0, result.output
    # Don't pin the number (installed dist vs in-tree __version__ differ); just
    # require a semver-like token.
    assert re.search(r"\d+\.\d+\.\d+", result.output)


def test_netdiag_help_lists_commands():
    result = CliRunner().invoke(netdiag_cli, ["--help"])
    assert result.exit_code == 0
    assert "explain" in result.output


def test_netdiag_monitor_and_list_probes_are_gone():
    for args in (["monitor"], ["list-probes"]):
        result = CliRunner().invoke(netdiag_cli, args)
        assert result.exit_code == 2
        assert "No such command" in result.output


def test_netdiag_explain_no_arg_lists():
    result = CliRunner().invoke(netdiag_cli, ["explain"])
    assert result.exit_code == 0, result.output
    assert "Available checks" in result.output


def test_netdiag_explain_known_probe():
    result = CliRunner().invoke(netdiag_cli, ["explain", "gateway"])
    assert result.exit_code == 0, result.output
    assert "What:" in result.output
    assert "Why:" in result.output


def test_netdiag_explain_unknown_probe():
    result = CliRunner().invoke(netdiag_cli, ["explain", "no_such_probe"])
    assert result.exit_code == 1
    # The message is written to stderr; depending on the click version it surfaces
    # in .output (combined) or .stderr (separated) — accept either.
    streams = result.output
    if getattr(result, "stderr_bytes", None):
        streams += result.stderr
    assert "unknown probe" in streams
