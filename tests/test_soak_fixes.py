"""Regressions from the 2026-11-13 soak bug hunt.

A soak must never call time or devices it did not measure clean, must not
blame the wrong device, and must never lose its results.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from netdiag import netinfo
from netdiag.cli import main
from netdiag.core import Status
from netdiag.inventory import Device
from netdiag.measure.icmp import PingStats
from netdiag.measure.stats import loss_upper_bound_pct
from netdiag.netinfo.model import HostNetwork, LinkStats
from netdiag.report.soak import (
    render_soak_csv,
    render_soak_html,
    render_soak_json,
    render_soak_text,
)
from netdiag.soak import DeviceTrack, NicEvent, SoakRecorder, SoakReport, run_soak

T0 = 1_760_000_040.0  # a minute boundary
H = 3600.0


def _soak(spec, duration, step=0.5, gaps=(), bucket=60.0, start=T0, holes=None):
    """Feed every device every *step* s; spec is ``[(name, host, lost_spans)]``.

    A request sent inside a lost span is lost. Inside a gap nothing is sent to
    any device, inside ``holes[host]`` nothing to that one.
    """
    holes = holes or {}
    tracks = [
        DeviceTrack(Device(name=name, host=host), host, bucket)
        for name, host, _lost in spec
    ]
    rec = SoakRecorder(tracks, timeout=1.0)
    for k in range(int(duration / step)):
        t = k * step
        if any(a <= t < b for a, b in gaps):
            continue
        for (_name, host, lost), track in zip(spec, tracks):
            if any(a <= t < b for a, b in holes.get(host, ())):
                continue
            down = any(a <= t < b for a, b in lost)
            rec.feed(host, start + t, None if down else 0.5)
    rec.flush()
    for t in tracks:
        t.finish()
    return SoakReport(
        tracks=tracks,
        started_at=start,
        finished_at=start + duration,
        interval=step,
        timeout=1.0,
        bucket_s=bucket,
        min_lost=2,
        hostname="LINE1-PC",
    )


def _codes(rep):
    return [f.code for f in rep.findings()]


# ------------------------------------------------- nothing measured is not OK


def test_unmeasured_stretch_is_reported_and_drawn_grey():
    rep = _soak(
        [("PLC1", "192.0.2.1", []), ("PLC2", "192.0.2.2", [])],
        duration=8 * H,
        step=1.0,
        gaps=[(1 * H, 7 * H)],
    )
    assert rep.status == Status.UNKNOWN and rep.exit_code == 3
    (gap,) = [f for f in rep.findings() if f.code == "soak.unmeasured_time"]
    assert gap.subject == "All devices" and "6h00m" in gap.summary
    assert ("soak.ok", "All devices") not in [
        (f.code, f.subject) for f in rep.findings()
    ]
    obj = json.loads(render_soak_json(rep))
    assert abs(obj["devices"][0]["unmeasured_s"] - 6 * H) <= 2  # sample to sample
    html = render_soak_html(rep)
    assert "not measured</title>" in html  # the 6 h hole is grey, with a tooltip
    assert "Result: time not measured" in render_soak_text(rep)


def test_a_hole_in_one_device_names_that_device():
    rep = _soak(
        [("PLC1", "192.0.2.1", []), ("PLC2", "192.0.2.2", [])],
        duration=600.0,
        holes={"192.0.2.2": [(100.0, 160.0)]},
    )
    gaps = [f for f in rep.findings() if f.code == "soak.unmeasured_time"]
    assert [f.subject for f in gaps] == ["PLC2 (192.0.2.2)"]
    assert rep.exit_code == 3


def test_a_short_pause_is_not_a_gap():
    rep = _soak([("PLC1", "192.0.2.1", [])], 600.0, gaps=[(100.0, 103.0)])
    assert rep.exit_code == 0


def test_html_heatmap_has_a_cell_for_every_minute_of_the_run():
    rep = _soak([("PLC1", "192.0.2.1", [])], 8 * H, step=1.0, gaps=[(H, 7 * H)])
    html = render_soak_html(rep)
    widths = [
        int(w) + 1 for w in re.findall(r'<rect x="\d+" y="\d+" width="(\d+)"', html)
    ]
    cell = max(4, min(18, int(820 / 480)))
    assert sum(widths) == 480 * cell


# ------------------------------------------------------------ grouping drops


def test_a_long_outage_does_not_swallow_other_devices_drops():
    rep = _soak(
        [
            ("PLC-A", "192.0.2.1", [(600, 600 + H)]),
            ("HMI-B", "192.0.2.2", [(900, 905), (2000, 2005), (3500, 3505)]),
            ("REF", "192.0.2.3", []),
        ],
        duration=2 * H,
        step=1.0,
    )
    assert [i.kind for i in rep.incidents] == ["single"] * 4
    codes = _codes(rep)
    assert "soak.common_outage" not in codes and "soak.group_outage" not in codes
    b = next(f for f in rep.findings() if f.subject.startswith("HMI-B"))
    assert b.code == "soak.device_outage" and "3x" in b.summary


def test_drops_that_only_overlap_in_a_chain_are_separate():
    rep = _soak(
        [
            ("A", "192.0.2.1", [(600, 2400)]),
            ("B", "192.0.2.2", [(1800, 3000)]),
            ("C", "192.0.2.3", [(2700, 3600)]),
            ("REF", "192.0.2.4", []),
        ],
        duration=2 * H,
        step=1.0,
    )
    assert [(i.kind, i.devices) for i in rep.incidents] == [
        ("single", ["A"]),
        ("single", ["B"]),
        ("single", ["C"]),
    ]


def test_devices_sharing_a_name_are_told_apart():
    rep = _soak(
        [("PLC", "192.0.2.1", [(20, 26)]), ("PLC", "192.0.2.2", [(20, 26)])],
        duration=60.0,
    )
    (inc,) = rep.incidents
    assert inc.kind == "all"
    assert inc.devices == ["PLC (192.0.2.1)", "PLC (192.0.2.2)"]
    assert _codes(rep) == ["soak.common_outage"]


def test_a_single_device_drop_is_not_blamed_on_the_device():
    rep = _soak([("PLC", "192.0.2.1", [(20, 26)])], duration=60.0)
    assert [i.kind for i in rep.incidents] == ["unlocated"]
    (f,) = rep.findings()
    assert f.code == "soak.outage_unlocated" and rep.exit_code == 1
    assert "Only this device" not in render_soak_text(rep)
    assert "cannot be told apart" in f.summary


def test_a_drop_while_no_other_device_answers_is_unlocated():
    rep = _soak(
        [("A", "192.0.2.1", [(100, 3000)]), ("B", "192.0.2.2", [(1000, 1010)])],
        duration=H,
        step=1.0,
    )
    kinds = {i.devices[0]: i.kind for i in rep.incidents}
    assert kinds == {"A": "single", "B": "unlocated"}


def test_a_device_down_when_the_soak_starts_is_an_outage():
    rep = _soak(
        [("PLC", "192.0.2.1", [(0, 5)]), ("REF", "192.0.2.2", [])], duration=60.0
    )
    assert rep.exit_code == 1
    assert "soak.sporadic_loss" not in _codes(rep)
    (o,) = rep.tracks[0].outages
    assert (o.start, o.end, o.lost) == (T0, T0 + 5.0, 10)
    assert _codes(rep) == ["soak.device_outage"]


def test_every_device_down_at_the_start_points_at_this_pc():
    rep = _soak(
        [("A", "192.0.2.1", [(0, 5)]), ("B", "192.0.2.2", [(0, 5)])], duration=60.0
    )
    assert _codes(rep) == ["soak.common_outage"]


# ------------------------------------------------------------------- reports


def test_headline_names_this_pcs_link_drop():
    rep = _soak([("PLC", "192.0.2.1", [])], 60.0)
    rep.nic_events = [NicEvent(ts=T0 + 10, interface="eth0", link_drops=1)]
    text = render_soak_text(rep)
    assert "Result: this PC's link dropped" in text
    assert "Result: fail" not in text


def test_unresolved_device_has_a_code_and_a_translated_reason(ja):
    track = DeviceTrack(Device(name="PLC9", host="plc9.invalid"), None, 60.0)
    rep = SoakReport([track], T0, T0 + 60, 0.2, 1.0, 60.0, 2)
    (f,) = rep.findings()
    assert f.code == "soak.not_measured" and f.status == Status.UNKNOWN
    assert f.summary == "測定できません（名前解決に失敗）"
    assert rep.exit_code == 3
    assert json.loads(render_soak_json(rep))["devices"][0]["status"] == "unknown"


def test_bucket_size_shows_in_csv_times_and_html_legend():
    rep = _soak([("PLC", "192.0.2.1", [(30, 33)])], 60.0, bucket=10.0)
    times = [row.split(",")[0] for row in render_soak_csv(rep).splitlines()[1:]]
    assert len(set(times)) == 6 and all(
        re.fullmatch(r".* \d\d:\d\d:\d\d", t) for t in times
    )
    html = render_soak_html(rep)
    assert "one cell = 10 s" in html and "Loss per 10 s" in html


def test_a_run_over_midnight_dates_its_times():
    rep = _soak(
        [
            ("PLC", "192.0.2.1", [(16 * H, 16 * H + 20), (40 * H, 40 * H + 20)]),
            ("REF", "192.0.2.2", []),
        ],
        duration=48 * H,
        step=2.0,
    )
    text = render_soak_text(rep)

    def day(off):
        return time.strftime("%m/%d %H:%M:%S", time.localtime(T0 + off))

    assert rep.exit_code == 1
    assert day(16 * H) in text and day(40 * H) in text
    start, end = (
        time.strftime("%Y-%m-%d", time.localtime(T0 + o)) for o in (0, 48 * H)
    )
    assert start in text.splitlines()[0] and end in text.splitlines()[0]


def test_html_says_when_the_incident_list_is_cut():
    lost = [(10 + 30 * i, 13 + 30 * i) for i in range(205)]
    rep = _soak([("PLC", "192.0.2.1", lost), ("REF", "192.0.2.2", [])], 205 * 30.0 + 30)
    assert len(rep.incidents) == 205
    assert "5 more" in render_soak_html(rep)


def test_all_clear_quotes_the_weakest_device_bound():
    tracks = [
        DeviceTrack(Device(name=n, host=h), h, 60.0)
        for n, h in (("BUSY", "192.0.2.1"), ("RARE", "192.0.2.2"))
    ]
    rec = SoakRecorder(tracks, timeout=1.0)
    for k in range(500):
        rec.feed("192.0.2.1", T0 + k * 0.01, 0.5)
    for k in range(10):
        rec.feed("192.0.2.2", T0 + k * 0.5, 0.5)
    rec.flush()
    rep = SoakReport(tracks, T0, T0 + 5.0, 0.01, 1.0, 60.0, 2)
    (ok,) = rep.findings()
    assert ok.code == "soak.ok"
    assert f"{loss_upper_bound_pct(0, 10):.3f}%" in ok.summary


# ------------------------------------------------------------- live runs


@pytest.fixture
def quiet_pc(monkeypatch):
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: HostNetwork())
    monkeypatch.setattr(netinfo, "link_stats", lambda: {"eth0": LinkStats()})


def _stream(lost=None, end_after=None, error=None, crash_after=None, after_stop=0):
    """A fake ``ping_stream``: one request per target every 10 ms."""

    def stream(targets, on_sample, stop, interval, timeout, **kw):
        k = 0
        while not stop.is_set():
            if end_after is not None and k >= end_after:
                return {t: PingStats(target=t, error=error) for t in targets}
            if crash_after is not None and k >= crash_after:
                raise RuntimeError("socket exploded")
            now = time.time()
            for t in targets:
                on_sample(t, now, None if lost and lost(t, k) else 0.4, None)
            k += 1
            stop.wait(0.01)
        for _ in range(after_stop):  # requests that were never sent
            for t in targets:
                on_sample(t, time.time() + 0.5, None, None)
        return {t: PingStats(target=t) for t in targets}

    return stream


def _main(monkeypatch, args, stream=None):
    import netdiag.measure as measure

    if stream is not None:
        monkeypatch.setattr(measure, "ping_stream", stream)
    with pytest.raises(SystemExit) as exc:
        main(["soak", *args], prog_name="netdiag")
    return exc.value.code


def test_nothing_resolvable_is_not_measured_and_ends_at_once(
    monkeypatch, quiet_pc, capsys
):
    def no_name(*a, **k):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", no_name)
    began = time.monotonic()
    assert _main(monkeypatch, ["plc1.invalid", "--duration", "30s"]) == 3
    assert time.monotonic() - began < 10
    out = capsys.readouterr()
    assert "Not measured (name resolution failed)" in out.out
    assert "No drops" not in out.out
    assert "is not measured: name resolution failed" in out.err


def test_a_device_without_samples_makes_the_soak_incomplete(
    monkeypatch, quiet_pc, tmp_path
):
    def stream(targets, on_sample, stop, interval, timeout, **kw):
        while not stop.wait(0.01):
            on_sample("192.0.2.1", time.time(), 0.4, None)
        return {t: PingStats(target=t) for t in targets}

    out = tmp_path / "soak.json"
    args = ["192.0.2.1", "192.0.2.2", "--duration", "1", "-o", str(out)]
    assert _main(monkeypatch, args, stream) == 3
    obj = json.loads(out.read_text(encoding="utf-8"))
    assert [d["status"] for d in obj["devices"]] == ["ok", "unknown"]
    ok = next(f for f in obj["findings"] if f["code"] == "soak.ok")
    assert ok["subject"] == "What was measured"


def test_a_pinger_that_stops_early_is_reported(monkeypatch, quiet_pc, tmp_path):
    out = tmp_path / "soak.json"
    stream = _stream(end_after=20, error="connect: Network is unreachable")
    began = time.monotonic()
    assert (
        _main(monkeypatch, ["192.0.2.1", "--duration", "1h", "-o", str(out)], stream)
        == 3
    )
    assert time.monotonic() - began < 15
    obj = json.loads(out.read_text(encoding="utf-8"))
    assert obj["interrupted"] is False
    assert obj["stopped_early"] == "connect: Network is unreachable"
    (f,) = [f for f in obj["findings"] if f["code"] == "soak.stopped_early"]
    assert f["status"] == "unknown" and "were not measured" in f["summary"]


def test_a_pinger_crash_is_reported_not_raised(monkeypatch, quiet_pc, tmp_path):
    out = tmp_path / "soak.json"
    stream = _stream(crash_after=5)
    assert (
        _main(monkeypatch, ["192.0.2.1", "--duration", "1h", "-o", str(out)], stream)
        == 3
    )
    obj = json.loads(out.read_text(encoding="utf-8"))
    assert obj["stopped_early"] == "RuntimeError: socket exploded"


def test_an_outage_before_the_pinger_stops_still_exits_1(
    monkeypatch, quiet_pc, tmp_path
):
    out = tmp_path / "soak.json"
    args = ["REF=192.0.2.1", "PLC=192.0.2.2", "--duration", "1h", "-o", str(out)]
    stream = _stream(
        lost=lambda t, k: t == "192.0.2.2" and 5 <= k < 10,
        end_after=40,
        error="icmp socket unavailable",
    )
    assert _main(monkeypatch, args, stream) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["stopped_early"]


def test_requests_after_the_stop_are_not_counted(monkeypatch, quiet_pc, tmp_path):
    out = tmp_path / "soak.json"
    stream = _stream(after_stop=5)
    assert (
        _main(monkeypatch, ["192.0.2.1", "--duration", "1", "-o", str(out)], stream)
        == 0
    )
    dev = json.loads(out.read_text(encoding="utf-8"))["devices"][0]
    assert dev["sent"] > 0 and dev["lost"] == 0


@pytest.mark.parametrize("step", [3600.0, -30.0])
def test_a_clock_change_neither_ends_nor_stretches_the_run(monkeypatch, quiet_pc, step):
    import netdiag.measure as measure

    real = time.time
    began = real()
    monkeypatch.setattr(
        time, "time", lambda: real() + (step if real() - began > 0.5 else 0)
    )
    monkeypatch.setattr(measure, "ping_stream", _stream())
    rep = run_soak([Device(name="PLC", host="192.0.2.1")], duration=2.0, tick=0.01)
    elapsed = real() - began
    assert 1.9 < elapsed < 6
    assert abs(rep.finished_at - rep.started_at - 2.0) < 0.1
    assert abs(rep.clock_step_s - step) < 1
    assert rep.status == Status.OK
    assert "clock was changed" in render_soak_text(rep)


def test_late_loss_reports_do_not_land_in_closed_cells(monkeypatch, quiet_pc):
    """The system ping reports a loss up to max(timeout, 1 s pace) + 0.3 s late."""
    import netdiag.measure as measure

    def stream(targets, on_sample, stop, interval, timeout, **kw):
        pending = []
        k = 0
        while not stop.is_set():
            now = time.time()
            if k % 10 == 5:
                pending.append(now)
            else:
                on_sample(targets[0], now, 0.4, None)
            while pending and pending[0] < now - 1.2:
                on_sample(targets[0], pending.pop(0), None, None)
            k += 1
            stop.wait(0.05)
        for sent in pending:
            on_sample(targets[0], sent, None, None)
        return {t: PingStats(target=t) for t in targets}

    at_close = {}

    def on_event(kind, data):
        if kind == "bucket":
            for b in data["buckets"]:
                at_close[b.start] = (b.sent, b.lost)

    monkeypatch.setattr(measure, "ping_stream", stream)
    rep = run_soak(
        [Device(name="PLC", host="192.0.2.1")],
        duration=5.0,
        timeout=0.05,
        bucket=0.5,
        on_event=on_event,
        tick=0.01,
    )
    final = {b.start: (b.sent, b.lost) for b in rep.tracks[0].buckets.values()}
    assert at_close and all(final[s] == v for s, v in at_close.items())


# ------------------------------------------------------- options and output


@pytest.mark.parametrize(
    "option",
    [
        ["--bucket", "0"],
        ["--bucket", "nan"],
        ["--timeout", "nan"],
        ["--timeout", "-1"],
        ["--interval", "0"],
        ["--interval", "inf"],
        ["--min-lost", "0"],
        ["--duration", "0"],
        ["--duration", "10 minutes"],
    ],
)
def test_bad_options_are_usage_errors(monkeypatch, quiet_pc, option):
    stream = _stream()
    assert _main(monkeypatch, ["192.0.2.1", "--duration", "1", *option], stream) == 4


def test_output_naming_the_device_list_is_refused(monkeypatch, quiet_pc, tmp_path):
    ledger = tmp_path / "devices.csv"
    ledger.write_text("name,host\nPLC,192.0.2.1\n", encoding="utf-8")
    before = ledger.read_bytes()
    args = ["-f", str(ledger), "-o", str(ledger), "--duration", "1"]
    assert _main(monkeypatch, args, _stream()) == 4
    assert ledger.read_bytes() == before


def test_unwritable_output_is_refused_before_measuring(
    monkeypatch, quiet_pc, tmp_path, capsys
):
    began = time.monotonic()
    target = tmp_path / "no-such-dir" / "soak.html"
    args = ["192.0.2.1", "--duration", "3", "-o", str(target)]
    assert _main(monkeypatch, args, _stream()) == 4
    assert time.monotonic() - began < 2.5
    assert "cannot write to" in capsys.readouterr().err


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs file permissions"
)
def test_a_csv_that_becomes_unwritable_keeps_the_run_and_the_report(
    monkeypatch, quiet_pc, tmp_path, capsys
):
    out = tmp_path / "soak.csv"
    plain = _stream()

    def stream(targets, on_sample, stop, interval, timeout, **kw):
        os.chmod(out, 0o444)  # what Excel's lock looks like to the soak
        return plain(targets, on_sample, stop, interval, timeout)

    args = ["192.0.2.1", "--duration", "3", "--bucket", "0.2", "-o", str(out)]
    assert _main(monkeypatch, args, stream) == 4
    err = capsys.readouterr().err
    assert "cannot append to" in err and "Measuring continues" in err
    (spare,) = [p for p in tmp_path.iterdir() if p != out]
    assert "saved it to" in err and spare.name in err
    rows = spare.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("time,name,host") and len(rows) > 5


def test_live_line_names_the_cell_it_summarises(
    monkeypatch, quiet_pc, tmp_path, capsys
):
    out = tmp_path / "soak.json"
    stream = _stream(lost=lambda t, k: k % 7 == 3)
    args = ["192.0.2.1", "--duration", "3", "--bucket", "0.5", "-o", str(out)]
    assert _main(monkeypatch, args, stream) == 2
    buckets = json.loads(out.read_text(encoding="utf-8"))["devices"][0]["buckets"]
    lossy = {
        time.strftime("%H:%M:%S", time.localtime(b["start"]))
        for b in buckets
        if b["lost"]
    }
    stamps = re.findall(r"^(\d\d:\d\d:\d\d) loss: ", capsys.readouterr().err, re.M)
    assert stamps and set(stamps) <= lossy


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_second_ctrl_c_does_not_lose_the_report(tmp_path):
    script = textwrap.dedent("""
        import sys, time
        import netdiag.measure as measure
        from netdiag import netinfo
        from netdiag.measure.icmp import PingStats
        from netdiag.netinfo.model import HostNetwork

        netinfo.snapshot = lambda max_age=5.0: HostNetwork()
        netinfo.link_stats = lambda: {}

        def stream(targets, on_sample, stop, interval, timeout, **kw):
            while not stop.wait(0.01):
                for t in targets:
                    on_sample(t, time.time(), 0.4, None)
            time.sleep(2)  # a slow stop, like a ping still waiting for replies
            return {t: PingStats(target=t) for t in targets}

        measure.ping_stream = stream
        from netdiag.cli import main
        main(["soak", "192.0.2.1", "--duration", "1h", "--timeout", "5", "--format", "json"])
        """)
    p = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, NETDIAG_LANG="en", HOME=str(tmp_path)),
    )
    assert "Measuring 1 device" in p.stderr.readline()
    time.sleep(2.5)
    p.send_signal(signal.SIGINT)
    time.sleep(0.8)
    p.send_signal(signal.SIGINT)  # while it waits for the last replies
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert "Stopping" in err
    obj = json.loads(out)
    assert obj["interrupted"] is True and obj["devices"][0]["sent"] > 0
