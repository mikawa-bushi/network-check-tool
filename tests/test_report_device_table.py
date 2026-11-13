"""The device table shows whole values: reply counts, small losses, addresses."""

from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis
from netdiag.inventory import Device
from netdiag.report import render_check_text
from netdiag.runner import CheckReport, DeviceReport

T0 = 1_760_000_040.0


def _device(name, host, ping):
    diag = Diagnosis(
        results=[
            CheckResult("route", Status.OK, "on-link", detail={"address": host}),
            CheckResult("reach", Status.OK, "ok", detail={"ping": ping}),
        ],
        hypotheses=[],
        healthy=True,
    )
    return DeviceReport(Device(name=name, host=host), diag)


def _ping(sent, received, loss_upper_pct=None):
    return {
        "sent": sent,
        "received": received,
        "loss_pct": (sent - received) / sent * 100,
        "loss_upper_pct": loss_upper_pct,
        "rtt": {"median": 0.04, "p95": 0.06, "max": 0.2, "jitter": 0.01},
    }


def _device_rows(*devices):
    report = CheckReport(
        host=Diagnosis([], [], True),
        devices=list(devices),
        started_at=T0,
        hostname="LINE-PC",
        platform="test",
    )
    lines = render_check_text(report, color=False, unicode=True).splitlines()
    head = next(i for i, line in enumerate(lines) if "Replies" in line)
    return lines[head + 1 : head + 1 + len(devices)]


def test_device_table_shows_whole_reply_counts_and_small_loss_bounds():
    (row,) = _device_rows(_device("PLC-1", "192.0.2.10", _ping(1000, 1000, 0.2991)))
    assert "1000/1000" in row
    assert "≤0.3%" in row and "≤0%" not in row


def test_device_table_never_rounds_a_loss_to_zero():
    (row,) = _device_rows(_device("PLC-1", "192.0.2.10", _ping(10000, 9999)))
    assert "9999/10000" in row and "0.01%" in row


def test_device_table_shows_whole_ipv6_addresses():
    (row,) = _device_rows(_device("HMI", "2001:db8:10:20::1234", _ping(10, 10, 25.9)))
    assert "2001:db8:10:20::1234" in row
