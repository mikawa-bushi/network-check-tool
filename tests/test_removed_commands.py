"""Commands and options removed in 0.6.0 end as usage errors, not as a run."""

import pytest

from netdiag.cli import main


def _exit_code(args):
    with pytest.raises(SystemExit) as exc:
        main(args, prog_name="netdiag")
    return exc.value.code


def test_check_save_is_a_usage_error(monkeypatch, capsys):
    def run_check(*a, **kw):
        raise AssertionError("check ran")

    monkeypatch.setattr("netdiag.runner.run_check", run_check)
    assert _exit_code(["check", "--save"]) == 4
    assert "--save" in capsys.readouterr().err
