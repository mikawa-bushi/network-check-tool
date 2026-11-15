"""Text tables line up as the user's console draws them."""

import io
import sys

from netdiag.cli._app import harden_streams
from netdiag.inventory import Device
from netdiag.report import render_soak_text
from netdiag.soak import DeviceTrack, SoakRecorder, SoakReport

T0 = 1_760_000_040.0


def _use_cp932_console(monkeypatch):
    """stdout in a Shift_JIS locale (LANG=ja_JP.SJIS): double-byte = two cells.

    Called in the test body: pytest re-installs its own stdout after setup.
    """
    harden_streams()
    monkeypatch.setattr(
        sys,
        "stdout",
        io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="netdiag"),
    )


def _field_starts(line):
    """Cell offsets where each field starts, as a cp932 console draws the line."""
    shown = line.encode("cp932", errors="netdiag").decode("cp932")
    starts, cell, spaces = [], 0, 0
    for ch in shown:
        if ch != " " and spaces >= 2:
            starts.append(cell)
        spaces = spaces + 1 if ch == " " else 0
        cell += len(ch.encode("cp932"))
    return starts


def _soak(spec, duration=600.0, interval=1.0):
    tracks = [DeviceTrack(Device(name=n, host=h), h, 60.0) for n, h, _ in spec]
    rec = SoakRecorder(tracks, timeout=1.0, min_lost=2)
    for k in range(int(duration / interval)):
        off = k * interval
        for _name, host, downs in spec:
            lost = any(a <= off < b for a, b in downs)
            rec.feed(host, T0 + off, None if lost else 1.0)
    rec.flush()
    for t in tracks:
        t.finish()
    return SoakReport(
        tracks=tracks,
        started_at=T0,
        finished_at=T0 + duration,
        interval=interval,
        timeout=1.0,
        bucket_s=60.0,
        min_lost=2,
        nic_events=[],
        hostname="LINE-PC",
    )


def test_soak_table_columns_line_up_on_a_cp932_console(monkeypatch, ja):
    report = _soak(
        [
            ("第1ライン搬送PLC", "192.0.2.11", []),
            ("ﾛﾎﾞｯﾄ制御盤", "192.0.2.12", [(30, 40)]),
            ("検査装置ＨＭＩ（予備機・長い名前）", "192.0.2.13", [(50, 51)]),
            ("未接続の機器", "192.0.2.14", [(0, 10**9)]),
        ]
    )
    _use_cp932_console(monkeypatch)
    lines = render_soak_text(report, color=False, unicode=False).splitlines()
    head = next(i for i, line in enumerate(lines) if "名前" in line)
    header, rows = lines[head], lines[head + 1 : head + 5]
    assert any("…" in r for r in rows) and any("—" in r for r in rows)
    for row in rows:
        assert _field_starts(row)[1:] == _field_starts(header), row
