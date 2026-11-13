"""What the command line promises: options, output files, messages, exit codes.

The measurement layer is replaced by a fake; every test drives the real
``netdiag.cli.main`` entry point, the same one the console script and the exe use.
"""

import csv
import io
import json
import sys
from pathlib import Path

import pytest

import netdiag.runner as runner_mod
from netdiag.cli import main
from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis
from netdiag.runner import CheckReport


def _report(status=Status.OK):
    result = CheckResult(
        name="interface",
        status=status,
        summary="eth0 — ≤1 ms",
        code="interface.x",
        timestamp=1e9,
    )
    healthy = status == Status.OK
    host = Diagnosis(results=[result], hypotheses=[], healthy=healthy)
    return CheckReport(
        host=host, hostname="pc", platform="test", started_at=1e9, finished_at=1e9
    )


@pytest.fixture
def run(monkeypatch, capsys):
    """Run ``netdiag ARGS`` against a fake measurement; return (code, out, err)."""
    seen = {}

    def fake_run_check(devices, config=None, **kw):
        seen.update(devices=devices, config=config, kw=kw)
        return _report(seen.get("status", Status.OK))

    monkeypatch.setattr(runner_mod, "run_check", fake_run_check)

    def invoke(*args):
        with pytest.raises(SystemExit) as exc:
            main(list(args), prog_name="netdiag")
        out, err = capsys.readouterr()
        return exc.value.code, out, err

    invoke.seen = seen
    return invoke


# --------------------------------------------------------------- --http-url


def test_plain_http_url_does_not_ask_for_a_tls_check(run):
    code, _, _ = run("check", "--http-url", "http://192.0.2.10:8080/")
    config = run.seen["config"]
    assert code == 0
    assert config["http_url"] == "http://192.0.2.10:8080/"
    assert "tls_target" not in config


def test_https_url_is_also_the_tls_target(run):
    run("check", "--http-url", "https://192.0.2.10/")
    assert run.seen["config"]["tls_target"] == "https://192.0.2.10/"


def test_url_without_a_scheme_means_plain_http(run):
    run("check", "--http-url", "192.0.2.10:8080/status")
    config = run.seen["config"]
    assert config["http_url"] == "http://192.0.2.10:8080/status"
    assert "tls_target" not in config


@pytest.mark.parametrize("url", ["ftp://192.0.2.10/", "http://", "http://h:99999/"])
def test_unusable_http_url_is_a_usage_error(run, url):
    code, out, err = run("check", "--http-url", url)
    assert code == 4 and "config" not in run.seen
    assert "http:// or https://" in err


# ------------------------------------------------------------ report output


def _windows_redirected(run, *args):
    """Run with stdout as Python sets it up on Japanese Windows when redirected
    to a file (the ANSI code page, newline translation); return its bytes."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp932", newline="\r\n")
    real, sys.stdout = sys.stdout, stream
    try:
        assert run(*args)[0] == 0
        stream.flush()
        return stream.buffer.getvalue()
    finally:
        sys.stdout = real


@pytest.mark.parametrize("fmt", ["html", "json", "csv"])
def test_machine_formats_reach_redirected_stdout_as_the_file_bytes(run, tmp_path, fmt):
    saved = tmp_path / f"r.{fmt}"
    assert run("check", "-o", str(saved))[0] == 0
    piped = _windows_redirected(run, "check", "--format", fmt)
    assert piped.rstrip(b"\r\n") == saved.read_bytes().rstrip(b"\r\n")


def test_csv_on_redirected_stdout_has_one_line_end_per_row(run):
    data = _windows_redirected(run, "check", "--format", "csv")
    assert data.startswith(b"\xef\xbb\xbf") and b"\r\r\n" not in data
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    assert [] not in rows and len(rows) == 2


def test_output_dash_is_stdout(run, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = run("--lang", "en", "check", "-o", "-")
    assert code == 0 and "Result:" in out and "report written" not in err
    assert not (tmp_path / "-").exists()


def test_output_into_a_missing_folder_is_refused_before_measuring(run, tmp_path):
    code, _, err = run("check", "-o", str(tmp_path / "nodir" / "r.html"))
    assert code == 4 and "config" not in run.seen
    assert "does not exist" in err


def test_report_never_overwrites_the_device_list_it_reads(run, tmp_path):
    ledger = tmp_path / "ledger.csv"
    ledger.write_text("name,host\nPLC-1,192.0.2.10\n", encoding="utf-8")
    before = ledger.read_bytes()
    code, _, err = run("check", "-f", str(ledger), "-o", str(ledger))
    assert code == 4 and "config" not in run.seen
    assert "device list being read" in err
    assert ledger.read_bytes() == before


def test_report_that_cannot_be_saved_still_reaches_the_user(run, tmp_path, monkeypatch):
    locked = tmp_path / "r.json"

    def refuse(self, data):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(Path, "write_text", refuse)
    code, out, err = run("check", "-o", str(locked))
    assert code == 4
    assert json.loads(out)["status"] == "ok"
    assert err.strip() == (
        f"Error: could not save the report to {locked} (Permission denied). "
        "It went to standard output instead."
    )


def test_saved_message_follows_lang(run, tmp_path, monkeypatch):
    monkeypatch.setenv("NETDIAG_LANG", "en")
    _, _, err = run("--lang", "ja", "check", "-o", str(tmp_path / "r.json"))
    assert "レポートを保存しました" in err


# ------------------------------------------------------------------ --no-host


def test_no_host_without_devices_is_a_usage_error(run):
    code, out, err = run("check", "--no-host")
    assert code == 4 and "config" not in run.seen and out == ""
    assert "nothing to check" in err


def test_no_host_with_devices_checks_only_the_devices(run):
    assert run("check", "--no-host", "192.0.2.10")[0] == 0
    assert run.seen["kw"]["include_host"] is False
    assert [d.host for d in run.seen["devices"]] == ["192.0.2.10"]


# --------------------------------------------------------------------- --lang


def test_help_follows_lang_not_the_environment(run, monkeypatch):
    monkeypatch.setenv("NETDIAG_LANG", "en")
    code, out, _ = run("--lang", "ja", "check", "--help")
    assert code == 0
    assert "機器台帳" in out and "Device list" not in out
    monkeypatch.setenv("NETDIAG_LANG", "ja")
    _, out, _ = run("--lang", "en", "--help")
    assert "Display language" in out and "表示言語" not in out
    _, out, _ = run("--lang=en", "snapshot", "--help")
    assert "Usage: netdiag snapshot" in out


def test_help_without_lang_follows_the_environment(run, monkeypatch):
    monkeypatch.setenv("NETDIAG_LANG", "ja")
    _, out, _ = run("check", "--help")
    assert "機器台帳" in out


def test_usage_error_after_lang_is_in_that_language(run, monkeypatch):
    monkeypatch.setenv("NETDIAG_LANG", "en")
    code, _, err = run("--lang", "ja", "--bogus")
    assert code == 4 and err.startswith("指定に誤りがあります")
