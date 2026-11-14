"""What the command line promises: options, output files, messages, exit codes.

The measurement layer is replaced by a fake; every test drives the real
``netdiag.cli.main`` entry point, the same one the console script and the exe use.
"""

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import netdiag.runner as runner_mod
from netdiag.cli import main
from netdiag.core import CheckResult, Context, ExitCode, Probe, Status
from netdiag.diagnosis import Diagnosis
from netdiag.measure import PingStats
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


# ------------------------------------------------- Windows console (simulated)


class _Console(io.TextIOWrapper):
    def isatty(self):
        return True

    def fileno(self):
        return 1


class _Kernel32:
    """GetConsoleMode/SetConsoleMode of a console that may refuse VT mode."""

    def __init__(self, vt_supported):
        self.vt_supported = vt_supported
        self.mode = 0x0003

    def GetConsoleMode(self, handle, mode_ref):
        mode_ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        if not self.vt_supported:
            return 0
        self.mode = mode
        return 1


def _check_on_windows_console(run, monkeypatch, vt_supported):
    import ctypes

    from netdiag.cli import _app

    kernel32 = _Kernel32(vt_supported)
    monkeypatch.setattr(_app, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=id))
    console = _Console(io.BytesIO(), encoding="utf-8")
    real, sys.stdout = sys.stdout, console
    try:
        assert run("check")[0] == 0
        console.flush()
        return console.buffer.getvalue().decode("utf-8"), kernel32
    finally:
        sys.stdout = real


def test_windows_console_is_switched_to_vt_mode_before_colour(run, monkeypatch):
    out, kernel32 = _check_on_windows_console(run, monkeypatch, vt_supported=True)
    assert kernel32.mode & 0x0004
    assert "\x1b[" in out


def test_windows_console_without_vt_mode_gets_no_escape_codes(run, monkeypatch):
    out, _ = _check_on_windows_console(run, monkeypatch, vt_supported=False)
    assert "Result:" in out and "\x1b" not in out


# ------------------------------------------------------------- next steps


@pytest.mark.parametrize("lang, ledger", [("ja", "台帳.csv"), ("en", "devices.csv")])
def test_next_steps_start_by_drafting_the_device_list(run, lang, ledger):
    _, _, err = run("--lang", lang)
    commands = [
        line.split("netdiag", 1)[1] for line in err.splitlines() if "netdiag" in line
    ]
    assert commands[0].strip() == f"scan --save {ledger}"
    assert commands.index(f" check -f {ledger}") > 0
    soak = next(c for c in commands if c.startswith(" soak"))
    assert f"-f {ledger}" in soak and not re.search(r"\d+\.\d+\.\d+\.\d+", soak)


# ------------------------------------------------------- -v and --log-file


class _Crashing(Probe):
    name = "crashing"

    def check(self, ctx):
        raise RuntimeError("parser fell over")


@pytest.fixture
def crashing_check(monkeypatch):
    """A check run in which one probe crashes; restores the netdiag logger."""
    lg = logging.getLogger("netdiag")
    handlers, level = lg.handlers[:], lg.level

    def fake_run_check(devices, config=None, **kw):
        result = _Crashing().run(Context(config))
        host = Diagnosis(results=[result], hypotheses=[], healthy=True)
        return CheckReport(host=host, hostname="pc", platform="test")

    monkeypatch.setattr(runner_mod, "run_check", fake_run_check)
    yield
    for h in lg.handlers[:]:
        if h not in handlers:
            h.close()
    lg.handlers[:] = handlers
    lg.setLevel(level)


def _main(*args):
    with pytest.raises(SystemExit) as exc:
        main(list(args), prog_name="netdiag")
    return exc.value.code


def test_verbose_shows_the_traceback_of_a_crashed_check(crashing_check, capsys):
    _main("check")
    assert "Traceback" not in capsys.readouterr().err
    _main("-v", "check")
    err = capsys.readouterr().err
    assert "Traceback" in err and "RuntimeError: parser fell over" in err


def test_log_file_gets_the_debug_log(crashing_check, tmp_path):
    log = tmp_path / "netdiag.log"
    _main("--log-file", str(log), "check")
    logging.getLogger("netdiag").handlers[-1].flush()
    assert "RuntimeError: parser fell over" in log.read_text(encoding="utf-8")


def test_unusable_log_file_is_a_usage_error(crashing_check, tmp_path, capsys):
    assert _main("--log-file", str(tmp_path / "nodir" / "x.log"), "check") == 4
    assert capsys.readouterr().err.startswith("Error: cannot open the log file")


# ---------------------------------------------------------------- explain

DOCS = Path(__file__).resolve().parents[1] / "docs" / "netdiag.md"


def _documented_checks():
    text = DOCS.read_text(encoding="utf-8")
    checks = text.split("\n## Checks", 1)[1].split("\n## ", 1)[0]
    first_cells = [row.split("|")[1] for row in checks.splitlines() if row[:3] == "| `"]
    return sorted(
        {name for cell in first_cells for name in re.findall(r"`(\w+)`", cell)}
    )


def _fresh_netdiag(*args):
    """A new interpreter, so only what the CLI itself imports is registered."""
    return subprocess.run(
        [sys.executable, "-m", "netdiag", "--lang", "en", *args],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        encoding="utf-8",
    )


def test_every_documented_check_is_explained_by_a_fresh_netdiag():
    names = _documented_checks()
    assert "link_errors" in names and "route" in names
    for name in names:
        res = _fresh_netdiag("explain", name)
        assert res.returncode == 0 and "What:" in res.stdout, (name, res.stderr)


def test_explain_lists_link_errors_in_a_fresh_netdiag():
    res = _fresh_netdiag("explain")
    assert re.search(r"^  link_errors +\[L1", res.stdout, re.M)


# ------------------------------------------------- exit codes, ping, numbers


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_check_help_lists_every_exit_code(run, lang):
    _, out, _ = run("--lang", lang, "check", "--help")
    listed = {int(m) for m in re.findall(r"^ +(\d+) +\S", out, re.M)}
    assert listed == {int(code) for code in ExitCode}


@pytest.mark.parametrize(
    "stats, code",
    [
        (PingStats(target="h", sent=10, received=10, rtts_ms=[0.5] * 10), 0),
        (PingStats(target="h", sent=10, received=0), 1),
        (PingStats(target="h", sent=10, received=7, rtts_ms=[0.5] * 7), 2),
        (PingStats(target="h", error="name resolution failed"), 3),
    ],
)
def test_ping_exit_code_follows_its_verdict(run, monkeypatch, stats, code):
    import netdiag.measure as measure

    monkeypatch.setattr(measure, "ping", lambda host, **kw: stats)
    assert run("ping", "192.0.2.10")[0] == code
    assert run("ping", "--json", "192.0.2.10")[0] == code


@pytest.mark.parametrize(
    "args",
    [
        ("check", "--count", "0"),
        ("check", "--count", "-5"),
        ("check", "--interval", "0"),
        ("check", "--duration", "0"),
        ("check", "--timeout", "-1"),
        ("check", "--jobs", "0"),
        ("ping", "192.0.2.10", "-c", "0"),
        ("ping", "192.0.2.10", "-i", "0"),
        ("ping", "192.0.2.10", "--timeout", "0"),
    ],
)
def test_counts_and_seconds_must_be_positive(run, args):
    code, _, err = run(*args)
    assert code == 4 and "config" not in run.seen
    assert "Invalid value" in err
