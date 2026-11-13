"""Exit codes of runs that end without a verdict: usage, internal error, Ctrl-C."""

import pytest

from netdiag.cli import main
from netdiag.core import InputError


def _run_main(monkeypatch, args, run_check=None):
    if run_check is not None:
        monkeypatch.setattr("netdiag.runner.run_check", run_check)
    with pytest.raises(SystemExit) as exc:
        main(args, prog_name="netdiag")
    return exc.value.code


def _raise(exc):
    def run_check(*a, **kw):
        raise exc

    return run_check


def test_click_usage_error_exits_4(monkeypatch, capsys):
    assert _run_main(monkeypatch, ["check", "--no-such-option"]) == 4
    err = capsys.readouterr().err
    assert "No such option" in err and "--help" in err


def test_click_usage_error_has_a_japanese_headline(monkeypatch, capsys):
    assert _run_main(monkeypatch, ["--lang", "ja", "check", "--count", "abc"]) == 4
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("指定に誤りがあります")
    assert "netdiag check --help" in err[0]
    assert any("'abc' is not a valid integer" in line for line in err[1:])


def test_input_error_exits_4_with_its_message(monkeypatch, capsys):
    code = _run_main(monkeypatch, ["check"], _raise(InputError("bad ledger row 3")))
    assert code == 4
    assert capsys.readouterr().err.strip() == "Error: bad ledger row 3"


def test_unexpected_exception_exits_5_with_a_sentence_then_the_traceback(
    monkeypatch, capsys
):
    code = _run_main(monkeypatch, ["check"], _raise(RuntimeError("kaboom")))
    assert code == 5
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("netdiag stopped because of an internal error.")
    assert err[1] == "Traceback (most recent call last):"
    assert err[-1] == "RuntimeError: kaboom"


def test_ctrl_c_before_a_verdict_exits_130_with_one_line(monkeypatch, capsys):
    code = _run_main(monkeypatch, ["check"], _raise(KeyboardInterrupt()))
    assert code == 130
    err = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert err == ["Interrupted."]


def test_help_and_version_exit_0(monkeypatch, capsys):
    assert _run_main(monkeypatch, ["--help"]) == 0
    assert _run_main(monkeypatch, ["--version"]) == 0
