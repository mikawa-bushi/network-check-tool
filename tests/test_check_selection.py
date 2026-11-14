"""Choosing checks with --only / --skip: a name that selects nothing is an error."""

import pytest

from netdiag.cli import main
from netdiag.core import InputError
from netdiag.diagnosis import Engine


def _main_with_engine(monkeypatch, args):
    def run_check(devices, config=None, only=None, skip=None, **kw):
        Engine(config=config).run(only=only, skip=skip)
        raise AssertionError("the engine accepted an unusable check name")

    monkeypatch.setattr("netdiag.runner.run_check", run_check)
    with pytest.raises(SystemExit) as exc:
        main(args, prog_name="netdiag")
    return exc.value.code


def test_misspelled_only_names_exit_4_and_list_the_valid_names(monkeypatch, capsys):
    assert _main_with_engine(monkeypatch, ["check", "--only", "gatway,dsn"]) == 4
    err = capsys.readouterr().err
    assert "gatway (did you mean gateway?)" in err
    assert "dsn (did you mean dns?)" in err
    assert "Valid names:" in err and "interface" in err


def test_misspelled_skip_name_exits_4(monkeypatch, capsys):
    assert _main_with_engine(monkeypatch, ["check", "--skip", "wifii"]) == 4
    assert "wifii (did you mean wifi?)" in capsys.readouterr().err


def test_device_check_without_a_device_exits_4(monkeypatch, capsys):
    assert _main_with_engine(monkeypatch, ["check", "--only", "route,reach"]) == 4
    err = capsys.readouterr().err
    assert "route, reach: device checks" in err


def test_unknown_name_message_is_japanese_in_japanese_mode(ja):
    with pytest.raises(InputError) as exc:
        Engine().select(only=["gatway"])
    assert "不明なチェック名: gatway（gateway のことですか？）" in str(exc.value)


def test_valid_names_still_select():
    assert set(Engine().select(only=["interface", "dns"])) == {"interface", "dns"}
    assert "wifi" not in Engine().select(skip=["wifi"])


def test_device_checks_select_when_the_run_has_a_device():
    engine = Engine(config={"target": {"host": "192.0.2.10"}})
    assert engine.select(only=["route", "reach"]) == ["route", "reach"]
