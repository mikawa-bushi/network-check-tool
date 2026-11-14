"""``netdiag.exe`` launcher: double-click / drag-and-drop behaviour."""

import pytest

from netdiag import launcher
from netdiag import runner as runner_mod
from netdiag.core import CheckResult
from netdiag.diagnosis import Diagnosis
from netdiag.runner import CheckReport

CSV = "名前,IPアドレス,グループ\r\nPLC-1,192.168.3.39,1ライン\r\nHMI-1,192.168.3.40,1ライン\r\n"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    calls = {"devices": None, "paused": 0}

    def run_check(devices, config=None, progress=None, **kw):
        calls["devices"] = devices
        calls["config"] = config
        ok = CheckResult.ok("interface", "eth0 up", code="interface.ok")
        return CheckReport(host=Diagnosis(results=[ok], hypotheses=[], healthy=True))

    monkeypatch.setattr(runner_mod, "run_check", run_check)
    monkeypatch.setattr(launcher, "_folder_of_exe", lambda: tmp_path)
    monkeypatch.setattr(
        launcher, "_pause", lambda: calls.__setitem__("paused", calls["paused"] + 1)
    )
    monkeypatch.setenv("NETDIAG_NO_OPEN", "1")
    monkeypatch.setenv("NETDIAG_LANG", "ja")
    return calls


def test_double_click_checks_this_pc_and_saves_report_next_to_exe(
    fake, tmp_path, capsys
):
    with pytest.raises(SystemExit) as e:
        launcher.main([])
    assert e.value.code == 0 and fake["paused"] == 1
    assert fake["devices"] == [] and fake["config"]["internet"] is False
    (report,) = tmp_path.glob("このPC_点検結果_*.html")
    assert "<html" in report.read_text(encoding="utf-8").lower()
    assert "レポートを保存しました" in capsys.readouterr().out


def test_dropped_device_list_is_checked_and_reported_beside_it(fake, tmp_path):
    lists = tmp_path / "lists"
    lists.mkdir()
    csv = lists / "1ライン.csv"
    csv.write_bytes(CSV.encode("cp932"))  # as saved by Excel on Japanese Windows
    with pytest.raises(SystemExit):
        launcher.main([str(csv)])
    assert [d.name for d in fake["devices"]] == ["PLC-1", "HMI-1"]
    assert len(list(lists.glob("1ライン_点検結果_*.html"))) == 1
    assert fake["paused"] == 1


def test_unreadable_list_explains_and_waits(fake, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\r\n1,2\r\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        launcher.main([str(bad)])
    assert e.value.code == 2 and fake["paused"] == 1 and fake["devices"] is None
    assert "台帳を読めませんでした" in capsys.readouterr().out


def test_report_falls_back_to_home_when_folder_is_read_only(
    fake, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        launcher, "_folder_of_exe", lambda: tmp_path / "missing" / "dir"
    )
    with pytest.raises(SystemExit):
        launcher.main([])
    assert len(list(home.glob("*.html"))) == 1


def test_anything_else_is_the_normal_cli(fake):
    with pytest.raises(SystemExit) as e:
        launcher.main(["--version"])
    assert e.value.code == 0 and fake["paused"] == 0 and fake["devices"] is None
