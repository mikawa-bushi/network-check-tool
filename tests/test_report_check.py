"""Rendering a full check (this PC + devices) for the terminal, JSON, HTML and CSV."""

import csv
import io
import json

from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis, Hypothesis
from netdiag.inventory import Device
from netdiag.report import (
    render_check_csv,
    render_check_html,
    render_check_json,
    render_check_text,
)
from netdiag.runner import CheckReport, DeviceReport


def _res(name, status, summary, code="", detail=None, layer="L3", title=None):
    return CheckResult(
        name=name,
        status=status,
        summary=summary,
        code=code or f"{name}.{status.value}",
        detail=detail or {},
        layer=layer,
        title=title or name,
    )


def _ping_detail(received, sent=10):
    rtt = {"median": 0.4, "p95": 0.9, "max": 1.2, "jitter": 0.1} if received else None
    upper = 27.75 if received == sent else None
    return {
        "address": "192.168.3.39",
        "ping": {
            "sent": sent,
            "received": received,
            "loss_pct": (sent - received) / sent * 100,
            "loss_upper_pct": upper,
            "rtt": rtt,
        },
        "mac": "00:26:92:aa:bb:cc" if received else None,
        "vendor": "Mitsubishi Electric" if received else None,
    }


def _report():
    host = Diagnosis(
        results=[
            _res(
                "interface",
                Status.OK,
                "eth0 192.168.3.100/24",
                layer="L2",
                title="Network interface",
            ),
            _res(
                "tcp",
                Status.SKIP,
                "no TCP targets configured",
                code="tcp.none",
                layer="L4",
            ),
        ],
        hypotheses=[],
        healthy=True,
    )
    ok_dev = Diagnosis(
        results=[
            _res("route", Status.OK, "on-link", detail={"address": "192.168.3.39"}),
            _res("reach", Status.OK, "10/10", detail=_ping_detail(10)),
            _res(
                "ports",
                Status.OK,
                "502 open",
                layer="L4",
                detail={
                    "address": "192.168.3.39",
                    "ports": [{"port": 502, "state": "open"}],
                },
            ),
        ],
        hypotheses=[],
        healthy=True,
    )
    bad = _res(
        "reach",
        Status.FAIL,
        "no reply, no ARP",
        code="reach.no_arp",
        detail=_ping_detail(0),
    )
    bad_dev = Diagnosis(
        results=[
            _res("route", Status.OK, "on-link", detail={"address": "192.168.3.41"}),
            bad,
        ],
        hypotheses=[
            Hypothesis(
                cause="reach",
                confidence="high",
                summary="No device answers for 192.168.3.41",
                evidence=["no reply, no ARP"],
                consequences=["ports"],
                runbook=["Check the device power", "Reseat the cable"],
                code="reach.no_arp",
            )
        ],
        healthy=False,
    )
    return CheckReport(
        host=host,
        devices=[
            DeviceReport(
                Device(name="PLC-1", host="192.168.3.39", ports=[502], group="L1"),
                ok_dev,
            ),
            DeviceReport(Device(name="ROBOT", host="192.168.3.41"), bad_dev),
            DeviceReport(
                Device(name="SPARE", host="192.168.3.42"),
                None,
                skipped_reason="not evaluated",
            ),
        ],
        started_at=1_760_000_000.0,
        finished_at=1_760_000_003.0,
        hostname="LINE1-PC",
        platform="test",
        settings={"count": 10},
    )


def test_status_and_exit_code():
    rep = _report()
    assert rep.status == Status.FAIL and rep.exit_code == 1
    assert [d.status for d in rep.devices] == [Status.OK, Status.FAIL, Status.SKIP]


def test_text_report_is_conclusion_first_english():
    out = render_check_text(_report(), color=False, unicode=True)
    lines = out.splitlines()
    assert "LINE1-PC" in lines[0]
    assert lines[1].startswith("✘ Result:") and "ROBOT" in lines[1]
    assert "PLC-1" in out and "0.40/0.90/1.20" in out and "502 open" in out
    assert "no ARP reply" in out  # short finding in the device table
    assert "1. Check the device power" in out
    assert "no TCP targets" not in out  # not-applicable rows hidden by default
    assert "not-applicable" in out


def test_text_report_japanese_and_full_width_alignment(ja):
    out = render_check_text(_report(), color=False, unicode=True)
    assert "結論:" in out and "機器 3台" in out
    assert "ARP応答なし" in out
    header = next(ln for ln in out.splitlines() if "アドレス" in ln and "応答" in ln)
    row = next(ln for ln in out.splitlines() if "192.168.3.39" in ln and "PLC-1" in ln)
    from netdiag.report.common import width

    # The address column starts at the same display column in header and row.
    assert width(header[: header.index("アドレス")]) == width(
        row[: row.index("192.168.3.39")]
    )


def test_text_report_show_all_and_ascii():
    out = render_check_text(_report(), color=False, show_all=True, unicode=False)
    assert "no TCP targets configured" in out
    assert "✘" not in out and "NG" in out


def test_json_report_is_machine_readable():
    obj = json.loads(render_check_json(_report()))
    assert obj["status"] == "fail" and obj["exit_code"] == 1
    assert obj["device_counts"]["fail"] == 1
    robot = obj["devices"][1]
    assert robot["diagnosis"]["hypotheses"][0]["code"] == "reach.no_arp"
    assert robot["diagnosis"]["hypotheses"][0]["runbook"][0] == "Check the device power"


def test_html_report_is_self_contained_and_escaped():
    rep = _report()
    rep.devices[0].device.note = "<script>alert(1)</script>"
    out = render_check_html(rep)
    assert out.startswith("<!doctype html>")
    assert "<script" not in out.replace("&lt;script", "")
    assert "http://" not in out and "https://" not in out
    assert "ROBOT" in out and "Mitsubishi Electric" in out


def test_csv_report_one_row_per_device():
    rows = list(csv.reader(io.StringIO(render_check_csv(_report()))))
    assert rows[0][0] == "status" and len(rows) == 4
    plc = rows[1]
    assert plc[1] == "PLC-1" and plc[0] == "ok"
    assert "502/Modbus/TCP" in ",".join(plc)
    assert rows[2][0] == "fail" and "Check the device power" in rows[2]


def test_csv_report_japanese_headers(ja):
    rows = list(csv.reader(io.StringIO(render_check_csv(_report()))))
    assert rows[0][:3] == ["状態", "名前", "ホスト"]
