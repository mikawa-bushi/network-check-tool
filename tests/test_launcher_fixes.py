"""``netdiag.exe`` for people who never open a terminal: whatever they drop or
double-click, the window waits, the message is theirs to act on, and no report
is lost. Runs ``launcher.main`` with the measurement and the Enter prompt stubbed."""

import time

import pytest

from netdiag import launcher
from netdiag import runner as runner_mod
from netdiag.core import CheckResult
from netdiag.diagnosis import Diagnosis
from netdiag.runner import CheckReport

CSV = "名前,IPアドレス\r\nPLC-1,192.0.2.10\r\n"


@pytest.fixture
def exe(monkeypatch, tmp_path, capsys):
    """Run the exe with ARGS; return (exit code, printed text, state)."""
    state = {"devices": None, "paused": 0, "crash": None}

    def run_check(devices, config=None, progress=None, **kw):
        if state["crash"]:
            raise state["crash"]
        state["devices"] = devices
        ok = CheckResult.ok("interface", "eth0 up", code="interface.ok")
        return CheckReport(host=Diagnosis(results=[ok], hypotheses=[], healthy=True))

    monkeypatch.setattr(runner_mod, "run_check", run_check)
    monkeypatch.setattr(launcher, "_folder_of_exe", lambda: tmp_path)
    monkeypatch.setattr(
        launcher, "_pause", lambda: state.__setitem__("paused", state["paused"] + 1)
    )
    monkeypatch.setenv("NETDIAG_NO_OPEN", "1")

    def invoke(*args):
        with pytest.raises(SystemExit) as e:
            launcher.main(list(args))
        out, err = capsys.readouterr()
        return e.value.code, out + err

    invoke.state = state
    return invoke


def test_dropped_excel_workbook_says_to_save_it_as_csv_and_waits(exe, tmp_path):
    book = tmp_path / "1ライン.xlsx"
    book.write_bytes(b"PK\x03\x04")
    code, text = exe(str(book))
    assert code == 4 and exe.state["paused"] == 1 and exe.state["devices"] is None
    assert "CSV UTF-8" in text and "No such command" not in text


def test_two_dropped_lists_ask_for_one_at_a_time(exe, tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    for f in (a, b):
        f.write_text(CSV, encoding="utf-8")
    code, text = exe(str(a), str(b))
    assert code == 4 and exe.state["paused"] == 1
    assert "one device list at a time (2 items" in text


def test_dropped_folder_asks_for_the_list_file(exe, tmp_path):
    code, text = exe(str(tmp_path))
    assert code == 4 and exe.state["paused"] == 1
    assert "not a folder" in text


def test_dropped_file_of_another_kind_is_named(exe, tmp_path):
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF")
    code, text = exe(str(pdf))
    assert code == 4 and exe.state["paused"] == 1 and "manual.pdf" in text


def test_internal_error_is_explained_and_the_window_waits(exe):
    exe.state["crash"] = RuntimeError("kaboom")
    code, text = exe()
    assert code == 5 and exe.state["paused"] == 1
    assert "internal error" in text and "RuntimeError: kaboom" in text


def test_checking_again_in_the_same_second_keeps_the_first_report(
    exe, tmp_path, monkeypatch
):
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20261113-130000")
    assert exe()[0] == 0 and exe()[0] == 0
    names = sorted(p.name for p in tmp_path.glob("*.html"))
    assert names == [
        "this-pc_report_20261113-130000-2.html",
        "this-pc_report_20261113-130000.html",
    ]


def test_report_that_cannot_be_saved_anywhere_is_not_a_success(
    exe, tmp_path, monkeypatch
):
    monkeypatch.setattr(launcher, "_folder_of_exe", lambda: tmp_path / "gone")
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda c: tmp_path / "no"))
    code, text = exe()
    assert code == 4 and exe.state["paused"] == 1
    assert "could not save the report" in text and "Result:" in text


def test_typed_commands_get_the_cli_exit_codes(exe):
    assert exe("check", "--bogus")[0] == 4
    assert exe("no-such-command")[0] == 4
    assert exe.state["paused"] == 0
