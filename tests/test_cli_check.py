"""``netdiag check`` / ``ping`` / ``scan`` / ``scenario`` command wiring.

The measurement layer is replaced by fakes; these tests pin what the commands
do with their options: device lists, ports, output formats/files, exit codes,
language and the closed-network default.
"""

import json

from click.testing import CliRunner

import netdiag.runner as runner_mod
from netdiag.cli import cli
from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis, Hypothesis
from netdiag.measure import PingStats
from netdiag.runner import CheckReport, DeviceReport


def _fake_report(devices, status=Status.OK):
    host = Diagnosis(
        results=[
            CheckResult(
                name="interface",
                status=Status.OK,
                summary="eth0",
                code="interface.ok",
                layer="L2",
            )
        ],
        hypotheses=[],
        healthy=True,
    )
    reps = []
    for d in devices:
        if status == Status.OK:
            reach = CheckResult(
                name="reach", status=Status.OK, summary="10/10", code="reach.ok"
            )
            diag = Diagnosis(results=[reach], hypotheses=[], healthy=True)
        else:
            diag = Diagnosis(
                results=[
                    CheckResult(
                        name="reach",
                        status=Status.FAIL,
                        summary="down",
                        code="reach.no_arp",
                    )
                ],
                hypotheses=[
                    Hypothesis(
                        "reach", "high", "down", code="reach.no_arp", runbook=["power"]
                    )
                ],
                healthy=False,
            )
        reps.append(DeviceReport(d, diag))
    return CheckReport(host=host, devices=reps, hostname="pc", platform="test")


def _patch_run(monkeypatch, status=Status.OK):
    seen = {}

    def fake_run_check(devices, config=None, **kw):
        seen["devices"], seen["config"], seen["kw"] = devices, config, kw
        return _fake_report(devices, status)

    monkeypatch.setattr(runner_mod, "run_check", fake_run_check)
    return seen


def test_check_collects_targets_files_and_ports(monkeypatch, tmp_path):
    seen = _patch_run(monkeypatch)
    lst = tmp_path / "line.csv"
    lst.write_text("name,ip,ports\nPLC,192.168.3.39,502\n", encoding="utf-8")
    res = CliRunner().invoke(
        cli, ["check", "-f", str(lst), "HMI=192.168.3.40:5900", "-p", "opcua"]
    )
    assert res.exit_code == 0, res.output
    devs = seen["devices"]
    assert [(d.name, d.host, d.ports) for d in devs] == [
        ("PLC", "192.168.3.39", [502, 4840]),
        ("HMI", "192.168.3.40", [5900, 4840]),
    ]
    # closed equipment networks are the default: no internet checks
    assert seen["config"]["internet"] is False


def test_check_internet_flags(monkeypatch):
    seen = _patch_run(monkeypatch)
    CliRunner().invoke(cli, ["check", "--internet"])
    assert seen["config"]["internet"] is True
    CliRunner().invoke(cli, ["check", "--external-ip", "10.0.0.1"])
    assert (
        seen["config"]["internet"] is True
        and seen["config"]["external_ip"] == "10.0.0.1"
    )


def test_check_duration_sets_count(monkeypatch):
    seen = _patch_run(monkeypatch)
    CliRunner().invoke(cli, ["check", "--duration", "60", "--interval", "0.5"])
    assert seen["config"]["count"] == 120


def test_duration_uses_windows_ping_cadence(monkeypatch):
    import netdiag.measure as measure
    import netdiag.measure.icmp as icmpmod

    seen = _patch_run(monkeypatch)
    monkeypatch.setattr(icmpmod, "is_windows", lambda: True)
    monkeypatch.setattr(measure, "precise_ping_available", lambda: False)
    CliRunner().invoke(cli, ["check", "--duration", "60"])
    assert seen["config"]["count"] == 60  # ~1 ping/s with ping.exe, not 300


def test_check_exit_code_and_bad_device_list(monkeypatch, tmp_path):
    _patch_run(monkeypatch, status=Status.FAIL)
    assert CliRunner().invoke(cli, ["check", "192.168.3.39"]).exit_code == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("name,host,ports\nA,10.0.0.1,nonsense\n", encoding="utf-8")
    res = CliRunner().invoke(cli, ["check", "-f", str(bad)])
    assert res.exit_code == 2 and "line 2" in res.output


def test_check_output_file_format_from_extension(monkeypatch, tmp_path):
    _patch_run(monkeypatch)
    html_path = tmp_path / "report.html"
    res = CliRunner().invoke(cli, ["check", "192.168.3.39", "-o", str(html_path)])
    assert res.exit_code == 0, res.output
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    csv_path = tmp_path / "report.csv"
    CliRunner().invoke(cli, ["check", "192.168.3.39", "-o", str(csv_path)])
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM for Excel
    res = CliRunner().invoke(cli, ["check", "192.168.3.39", "--format", "json"])
    assert json.loads(res.output)["devices"][0]["device"]["host"] == "192.168.3.39"


def _patch_problem_on_this_pc(monkeypatch):
    seen = {}

    def fake_run_check(devices, config=None, **kw):
        seen["devices"], seen["kw"] = devices, kw
        host = Diagnosis(
            results=[
                CheckResult(
                    name="gateway",
                    status=Status.FAIL,
                    summary="no reply from 192.168.3.1",
                    code="gateway.down",
                    layer="L3",
                )
            ],
            hypotheses=[],
            healthy=False,
        )
        return CheckReport(host=host, hostname="pc", platform="test")

    monkeypatch.setattr(runner_mod, "run_check", fake_run_check)
    return seen


def test_bare_netdiag_checks_this_pc_then_suggests_next_commands(monkeypatch):
    seen = _patch_problem_on_this_pc(monkeypatch)
    res = CliRunner().invoke(cli, [])
    assert seen["devices"] == [] and seen["kw"]["include_host"] is True
    assert "no reply from 192.168.3.1" in res.stdout
    for cmd in ("netdiag check -f", "netdiag scan", "netdiag soak", "netdiag --help"):
        assert cmd in res.stderr
    assert res.exit_code == 1


def test_bare_netdiag_takes_the_group_options(monkeypatch):
    _patch_problem_on_this_pc(monkeypatch)
    res = CliRunner().invoke(cli, ["--lang", "ja"])
    assert "結論" in res.stdout and "netdiag check -f 台帳.csv" in res.stderr
    res = CliRunner().invoke(cli, ["--lang", "en"])
    assert "Result:" in res.stdout and "netdiag check -f devices.csv" in res.stderr


def test_netdiag_help_does_not_run_the_check(monkeypatch):
    seen = _patch_run(monkeypatch)
    for flag in ("--help", "-h"):
        res = CliRunner().invoke(cli, [flag])
        assert res.exit_code == 0 and "Usage:" in res.stdout
    assert seen == {}


def test_check_lang_option(monkeypatch):
    _patch_run(monkeypatch)
    res = CliRunner().invoke(cli, ["--lang", "ja", "check", "192.168.3.39"])
    assert "結論" in res.output
    res = CliRunner().invoke(cli, ["--lang", "en", "check", "192.168.3.39"])
    assert "Result:" in res.output


def test_ping_command_statistics(monkeypatch):
    import netdiag.measure as measure

    st = PingStats(
        target="h", sent=10, received=10, rtts_ms=[0.5] * 10, method="icmp-socket"
    )
    monkeypatch.setattr(measure, "ping", lambda host, **kw: st)
    res = CliRunner().invoke(cli, ["ping", "192.168.3.39"])
    assert res.exit_code == 0
    assert "10/10" in res.output and "95% upper" in res.output
    down = PingStats(target="h", sent=10, received=0)
    monkeypatch.setattr(measure, "ping", lambda host, **kw: down)
    assert CliRunner().invoke(cli, ["ping", "192.168.3.39"]).exit_code == 1
    res = CliRunner().invoke(cli, ["ping", "--json", "192.168.3.39"])
    assert json.loads(res.output)["received"] == 0


def test_scan_command_and_save(monkeypatch, tmp_path):
    import netdiag.scan as scan_mod

    hits = [
        scan_mod.ScanHit(
            ip="192.168.3.39",
            mac="00:26:92:aa:bb:cc",
            vendor="Mitsubishi Electric",
            replied=True,
            rtt_ms=0.4,
        ),
        scan_mod.ScanHit(ip="192.168.3.40", mac="00:00:0a:11:22:33", arp_only=True),
    ]
    monkeypatch.setattr(
        scan_mod,
        "scan",
        lambda nets, **kw: scan_mod.ScanResult(
            networks=[str(n) for n in nets], hits=hits, probed=254
        ),
    )
    out = tmp_path / "found.csv"
    res = CliRunner().invoke(cli, ["scan", "192.168.3.0/24", "--save", str(out)])
    assert res.exit_code == 0, res.output
    assert "2 of 254" in res.output and "ARP only" in res.output
    saved = out.read_bytes().decode("utf-8-sig")
    assert "192.168.3.39" in saved and "00:26:92:aa:bb:cc" in saved
    res = CliRunner().invoke(cli, ["scan", "192.168.3.0/24", "--format", "json"])
    assert json.loads(res.output)["found"] == 2


def test_scan_rejects_bad_ports_and_networks():
    assert CliRunner().invoke(cli, ["scan", "-p", "nope"]).exit_code == 2
    assert CliRunner().invoke(cli, ["scan", "not-a-network"]).exit_code == 2


def test_scenario_with_devices_runs_the_device_check(monkeypatch, tmp_path):
    seen = _patch_run(monkeypatch)
    sc = tmp_path / "line1.json"
    sc.write_text(
        json.dumps(
            {
                "name": "ライン1",
                "checks": ["interface", "link"],
                "devices": [{"name": "PLC", "host": "192.168.3.39", "ports": "modbus"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    res = CliRunner().invoke(cli, ["scenario", str(sc)])
    assert res.exit_code == 0, res.output
    assert seen["devices"][0].ports == [502]
    assert seen["kw"]["only"] == ["interface", "link"]
    assert "ライン1" in res.output


def test_help_mentions_device_lists():
    res = CliRunner().invoke(cli, ["check", "--help"])
    assert (
        "--file" in res.output and "--port" in res.output and "--internet" in res.output
    )


def test_output_never_crashes_on_legacy_encodings():
    from netdiag.cli._app import harden_streams

    harden_streams()  # registers the "netdiag" error handler
    text = "結論 — 95%上限 ≤28% ✔ ⚠ ✘ ·"
    assert (
        text.encode("cp932", "netdiag").decode("cp932")
        == "結論 ― 95%上限 ≦28% ○ ! × ・"
    )
    assert "a — b ✘ ≥".encode("ascii", "netdiag") == b"a - b NG >="
    assert "日本".encode("cp1252", "netdiag") == b"??"
