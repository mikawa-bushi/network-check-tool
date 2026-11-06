"""Long-duration measurement: outage detection, grouping and the diagnosis.

Synthetic sample streams reproduce the field patterns that matter:
all devices dropping together (this PC / common switch), a subset together
(a shared switch), one device alone (its cable/port), sporadic single losses,
a device that never answers, and the PC's own link flapping.
"""

import json
import re
import threading

import pytest
from click.testing import CliRunner

from netdiag import netinfo
from netdiag.cli import cli
from netdiag.core import Status
from netdiag.inventory import Device
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
)
from netdiag.report.soak import (
    render_soak_csv,
    render_soak_html,
    render_soak_json,
    render_soak_text,
)
from netdiag.soak import (
    DeviceTrack,
    NicEvent,
    SoakRecorder,
    SoakReport,
    group_incidents,
    parse_duration,
)

T0 = 1_760_000_040.0  # a fixed wall-clock origin, aligned to a minute


def _tracks(*names):
    return [
        DeviceTrack(Device(name=n, host=f"10.0.0.{i + 1}"), f"10.0.0.{i + 1}", 60.0)
        for i, n in enumerate(names)
    ]


def _feed(rec, track_index, times_lost, start=0.0, end=60.0, step=0.5):
    """Feed one device: replies every *step* s except at the lost offsets."""
    addr = rec.tracks[track_index].address
    t = start
    while t < end:
        lost = any(a <= t < b for a, b in times_lost)
        rec.feed(addr, T0 + t, None if lost else 0.5, None)
        t += step


def _report(tracks, nic=None, duration=60.0):
    for t in tracks:
        t.finish()
    return SoakReport(
        tracks=tracks,
        started_at=T0,
        finished_at=T0 + duration,
        interval=0.5,
        timeout=1.0,
        bucket_s=60.0,
        min_lost=2,
        nic_events=nic or [],
        hostname="LINE1-PC",
    )


def test_outage_detected_in_send_order_despite_late_loss_reports():
    tracks = _tracks("PLC")
    rec = SoakRecorder(tracks, timeout=1.0)
    # Losses are reported after the timeout, replies immediately: feed the
    # replies first, then the losses, as the pinger would.
    addr = tracks[0].address
    for t in (0.0, 0.5, 3.0, 3.5):
        rec.feed(addr, T0 + t, 0.4, None)
    for t in (1.0, 1.5, 2.0, 2.5):
        rec.feed(addr, T0 + t, None, None)
    rec.flush()
    tr_ = tracks[0]
    assert (tr_.sent, tr_.lost) == (8, 4)
    assert len(tr_.outages) == 1
    o = tr_.outages[0]
    assert (o.start, o.end, o.lost) == (T0 + 1.0, T0 + 3.0, 4)
    assert o.duration() == 2.0


def test_single_loss_is_not_an_outage_and_reports_sporadic_loss():
    tracks = _tracks("PLC")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [(10.0, 10.5), (40.0, 40.5)])
    rec.flush()
    rep = _report(tracks)
    assert tracks[0].outages == [] and tracks[0].lost == 2
    assert rep.status == Status.WARN and rep.exit_code == 2
    assert [f.code for f in rep.findings()] == ["soak.sporadic_loss"]


def test_all_devices_dropping_together_points_at_this_pc_or_the_switch():
    tracks = _tracks("A", "B", "C")
    rec = SoakRecorder(tracks, timeout=1.0)
    for i in range(3):
        _feed(rec, i, [(20.0, 26.0)])
    rec.flush()
    rep = _report(tracks, nic=[NicEvent(ts=T0 + 30, interface="eth0", errors=7)])
    incs = rep.incidents
    assert (
        len(incs) == 1
        and incs[0].kind == "all"
        and sorted(incs[0].devices) == ["A", "B", "C"]
    )
    codes = [f.code for f in rep.findings()]
    assert codes[0] == "soak.common_outage"
    assert "soak.device_outage" not in codes  # not blamed on each device
    common = rep.findings()[0]
    assert any("eth0" in e for e in common.evidence)  # NIC errors at the same time
    assert rep.exit_code == 1


def test_one_device_alone_is_blamed_on_its_own_link():
    tracks = _tracks("A", "B", "ROBOT")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [])
    _feed(rec, 1, [])
    _feed(rec, 2, [(5.0, 8.0), (30.0, 31.0)])
    rec.flush()
    rep = _report(tracks)
    assert [i.kind for i in rep.incidents] == ["single", "single"]
    f = [f for f in rep.findings() if f.code == "soak.device_outage"]
    assert len(f) == 1 and "ROBOT" in f[0].subject
    assert "2" in f[0].summary


def test_a_subset_together_points_at_a_shared_switch():
    tracks = _tracks("A", "B", "C", "D", "E")
    tracks[0].device.group = tracks[1].device.group = "2ライン"
    rec = SoakRecorder(tracks, timeout=1.0)
    for i in range(5):
        _feed(rec, i, [(10.0, 14.0)] if i < 2 else [])
    rec.flush()
    incs = group_incidents(tracks, T0 + 60, tolerance=1.0)
    assert len(incs) == 1 and incs[0].kind == "some"
    rep = _report(tracks)
    (f,) = rep.findings()
    assert f.code == "soak.group_outage"
    assert any(
        "2ライン" in e for e in f.evidence
    )  # the list's own grouping names the suspect


def test_never_replying_device_and_link_flap():
    tracks = _tracks("OK", "DEAD")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [])
    _feed(rec, 1, [(0.0, 60.0)])
    rec.flush()
    assert tracks[1].outages == []  # never answered: not a "drop"
    rep = _report(tracks, nic=[NicEvent(ts=T0 + 10, interface="eth0", link_drops=1)])
    codes = [f.code for f in rep.findings()]
    assert codes[0] == "soak.pc_link_flap"
    assert "soak.never_replied" in codes


def test_healthy_run_quotes_the_confidence_bound():
    tracks = _tracks("A")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [], end=60.0, step=0.1)
    rec.flush()
    rep = _report(tracks)
    assert rep.status == Status.OK and rep.exit_code == 0
    assert "0.6" in rep.findings()[0].summary  # 0 of 600 -> below ~0.64 %


def test_buckets_close_and_summarise_per_minute():
    tracks = _tracks("A")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [(70.0, 72.0)], end=130.0)
    rec.flush()
    tracks[0].finish()
    buckets = sorted(tracks[0].buckets.values(), key=lambda b: b.start)
    assert [b.sent for b in buckets] == [120, 120, 20]
    assert buckets[1].lost == 4 and buckets[0].median == 0.5 and buckets[0].rtts == []


def test_completed_minutes_are_reported_while_measuring():
    # The live per-minute line and the progressive CSV rely on this.
    tracks = _tracks("A")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [], end=90.0)
    rec.drain(T0 + 80.0)
    assert [b.start for b in tracks[0].close_until(T0 + 80.0)] == [T0]
    assert tracks[0].close_until(T0 + 80.0) == []
    assert [b.start for b in tracks[0].finish()] == [T0 + 60.0]


@pytest.mark.parametrize(
    "text, seconds",
    [("600", 600), ("90s", 90), ("10m", 600), ("2h", 7200), ("1h30m", 5400)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("10 minutes")


def test_renderers(ja):
    tracks = _tracks("A", "B")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [(20.0, 23.0)])
    _feed(rec, 1, [])
    rec.flush()
    rep = _report(tracks)
    text = render_soak_text(rep, unicode=True)
    assert "長時間測定" in text and "通信断の発生時刻" in text and "単独" in text
    obj = json.loads(render_soak_json(rep))
    assert (
        obj["kind"] == "soak" and obj["devices"][0]["outages"][0]["duration_s"] == 3.0
    )
    html = render_soak_html(rep)
    assert html.startswith("<!doctype html>") and "<script" not in html
    assert html.count("<rect") == 2  # one minute x two devices
    rows = render_soak_csv(rep).splitlines()
    assert rows[0].startswith("時刻") and len(rows) == 3


def test_heatmap_draws_a_run_of_alike_minutes_as_one_cell():
    tracks = _tracks("A")
    rec = SoakRecorder(tracks, timeout=1.0)
    _feed(rec, 0, [(130.0, 133.0)], end=240.0)  # ok, ok, lossy, ok
    rec.flush()
    html = render_soak_html(_report(tracks, duration=240.0))
    assert html.count("<rect") == 3
    assert "sent 240 lost 0 (0.0%)" in html  # the first two minutes, merged


@pytest.fixture
def fake_soak(monkeypatch):
    """A wired PC and a ping stream in which PLC=10.0.0.2 drops 6 requests."""
    import netdiag.measure as measure

    hn = HostNetwork(
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                addresses=[Address("10.0.0.100", 24)],
            )
        ]
    )
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(netinfo, "link_stats", lambda: {"eth0": LinkStats(rx_errors=0)})

    def fake_stream(targets, on_sample, stop, interval, timeout, **kw):
        import time as _t

        k = 0
        while not stop.is_set():
            now = _t.time()
            for t in targets:
                lost = t == "10.0.0.2" and 10 <= k < 16
                on_sample(t, now, None if lost else 0.4, None)
            k += 1
            stop.wait(0.01)
        return {}

    monkeypatch.setattr(measure, "ping_stream", fake_stream)
    fast = ["--duration", "1", "--interval", "0.01", "--timeout", "0.05"]
    return lambda *args: CliRunner().invoke(
        cli, ["soak", "10.0.0.1", "PLC=10.0.0.2", *fast, *args]
    )


def test_soak_command_end_to_end_with_a_fake_stream(fake_soak, tmp_path):
    out = tmp_path / "soak.json"
    res = fake_soak("-o", str(out))
    assert res.exit_code == 1, res.output
    obj = json.loads(out.read_text(encoding="utf-8"))
    plc = next(d for d in obj["devices"] if d["device"]["name"] == "PLC")
    assert len(plc["outages"]) == 1 and plc["outages"][0]["lost"] == 6
    assert "dropped" in res.output  # live event line (stderr is mixed in)


def test_soak_csv_file_is_written_while_measuring(fake_soak, tmp_path):
    out = tmp_path / "soak.csv"
    res = fake_soak("--duration", "2", "--bucket", "0.2", "-o", str(out))
    assert res.exit_code == 1, res.output
    # completed cells are reported live
    assert re.search(r"^\d\d:\d\d (loss: |no loss on any device)", res.output, re.M)
    rows = out.read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("time,name,host") and len(rows) > 2


def test_soak_csv_goes_to_stdout_without_a_file(fake_soak):
    res = fake_soak("--format", "csv", "--quiet")
    assert res.exit_code == 1
    assert (
        res.output.splitlines()[0]
        == "time,name,host,sent,lost,loss_pct,rtt_median_ms,rtt_p95_ms,rtt_max_ms"
    )


def test_soak_requires_devices():
    res = CliRunner().invoke(cli, ["soak"])
    assert res.exit_code == 2


def test_track_threads_are_safe():
    tracks = _tracks("A")
    rec = SoakRecorder(tracks, timeout=1.0)

    def feeder(off):
        for i in range(500):
            rec.feed("10.0.0.1", T0 + off + i * 0.001, 0.3, None)

    th = [threading.Thread(target=feeder, args=(o,)) for o in (0.0, 0.0005)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    rec.flush()
    assert tracks[0].sent == 1000


def test_devices_given_by_name_are_measured_at_their_resolved_address(monkeypatch):
    import socket

    import netdiag.measure as measure
    from netdiag.soak import run_soak

    def getaddrinfo(host, port, family=0, *a, **k):
        if host == "plc.line1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 0))]
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: HostNetwork())
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    streamed = []

    def fake_stream(targets, on_sample, stop, interval, timeout, **kw):
        streamed.extend(targets)
        stop.wait()
        return {}

    monkeypatch.setattr(measure, "ping_stream", fake_stream)
    devices = [
        Device(name="PLC", host="plc.line1"),
        Device(name="GONE", host="gone.line1"),
        Device(name="LIT", host="10.0.0.3"),
    ]
    rep = run_soak(devices, duration=0.05, tick=0.01)
    assert streamed == ["10.0.0.2", "10.0.0.3"]
    assert [(t.address, t.error) for t in rep.tracks] == [
        ("10.0.0.2", None),
        (None, "name resolution failed"),
        ("10.0.0.3", None),
    ]
