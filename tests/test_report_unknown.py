"""A check with unmeasured parts says so in every report format."""

import csv
import io
import json

from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis
from netdiag.inventory import Device
from netdiag.report import (
    render_check_csv,
    render_check_html,
    render_check_json,
    render_check_text,
)
from netdiag.runner import CheckReport, DeviceReport


class _Report(CheckReport):
    @property
    def host_status(self):
        return self.host.status


class _Device(DeviceReport):
    @property
    def status(self):
        return self.diagnosis.status


def _res(name, status, summary, code=None, detail=None):
    return CheckResult(
        name=name,
        status=status,
        summary=summary,
        code=code or f"{name}.{status.value}",
        detail=detail or {},
        layer="L3",
        title=name,
    )


def _diag(*results):
    return Diagnosis(results=list(results), hypotheses=[], healthy=True)


def _report(host):
    route = _res("route", Status.OK, "on-link", detail={"address": "192.0.2.39"})
    measured = _diag(route, _res("reach", Status.OK, "10/10 replies"))
    unmeasured = _diag(
        route,
        _res("reach", Status.UNKNOWN, "ping not available", code="reach.unmeasured"),
    )
    return _Report(
        host=host,
        devices=[
            _Device(Device(name="PLC-1", host="192.0.2.39"), measured),
            _Device(Device(name="HMI-1", host="192.0.2.40"), unmeasured),
        ],
        started_at=1_760_000_000.0,
        finished_at=1_760_000_003.0,
        hostname="LINE1-PC",
        platform="test",
    )


def _partly_unmeasured():
    return _report(
        _diag(
            _res("interface", Status.OK, "eth0 192.0.2.100/24"),
            _res("gateway", Status.UNKNOWN, "could not be measured (no ping tool)"),
        )
    )


def _headline(report, **kw):
    return render_check_text(report, color=False, **kw).splitlines()[1]


def test_text_headline_says_what_was_not_measured():
    assert _headline(_partly_unmeasured(), unicode=True) == (
        "? Result: Not measured — 1 check(s) on this PC not measured; 1 not measured"
    )
    assert _headline(_partly_unmeasured(), unicode=False).startswith(
        "?? Result: Not measured"
    )


def test_text_headline_in_japanese(ja):
    assert _headline(_partly_unmeasured(), unicode=True) == (
        "? 結論: 測定不可 — このPCの 1 項目は測定不可、1台は測定不可"
    )


def test_nothing_measured_on_this_pc_is_named():
    host = _diag(_res("wifi", Status.SKIP, "no Wi-Fi adapter", code="wifi.none"))
    assert "nothing on this PC was measured" in _headline(_report(host))


def test_unmeasured_rows_are_never_hidden():
    text = render_check_text(_partly_unmeasured(), color=False, unicode=True)
    assert "? [L3]" in text and "could not be measured (no ping tool)" in text


def test_html_verdict_counts_unmeasured_devices():
    page = render_check_html(_partly_unmeasured())
    assert (
        "<div class='verdict unknown'>? Not measured — 0 failing, 0 warning, "
        "1 not measured of 2 devices (and findings on this PC)</div>"
    ) in page
    assert ".verdict.unknown" in page


def test_json_and_csv_carry_unknown_and_exit_3():
    report = _partly_unmeasured()
    obj = json.loads(render_check_json(report))
    assert obj["status"] == "unknown" and obj["exit_code"] == 3
    assert obj["device_counts"]["unknown"] == 1
    assert obj["host"]["status"] == "unknown"
    rows = list(csv.reader(io.StringIO(render_check_csv(report))))
    assert [r[0] for r in rows[1:]] == ["ok", "unknown"]
