"""Long-duration measurement for intermittent faults ("ときどき切れる").

A spot check cannot catch a link that drops for eight seconds twice an hour.
``netdiag soak`` pings every device continuously (see
:func:`netdiag.measure.ping_stream`) and records, per device:

* **outages** — two or more consecutive lost requests, with start/end time and
  duration (the moments a cyclic PLC connection would have timed out);
* **per-minute statistics** — sent, lost, median/p95/max RTT;
* **unmeasured stretches** — time in which the device got no request at all
  (the PC slept, the process stalled, the pinger stopped);
* the PC's own **NIC error counters and link drops**, sampled every minute.

Outages that start together are grouped into incidents, and the grouping is
the diagnosis: all devices at once points at this PC's cable/NIC or the common
switch/upstream; a subset at a switch they share; a single device at its own
cable, port or the device itself. Raw RTTs are kept only for the open minute
plus a fixed-size reservoir; what accumulates is one small summary per device
and minute, so overnight runs are fine.
"""

from __future__ import annotations

import heapq
import math
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import Status
from .i18n import tr
from .inventory import Device
from .measure.stats import loss_upper_bound_pct, summarize
from .netinfo import LinkStats

DEFAULT_BUCKET = 60.0
MIN_LOST_FOR_OUTAGE = 2
RESERVOIR = 4000
MIN_GAP_S = 5.0  # no request for this long is a stretch that was not measured
CLOCK_STEP_NOTE_S = 2.0

EventFn = Callable[[str, Dict[str, Any]], None]
Span = Tuple[float, float]

if hasattr(time, "CLOCK_BOOTTIME"):  # Linux: CLOCK_MONOTONIC stops while asleep

    def _monotonic() -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)

elif sys.platform == "darwin":  # time.monotonic() stops while asleep there

    def _monotonic() -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC)

else:
    _monotonic = time.monotonic


class SoakClock:
    """The run's timeline: the wall clock at the start, then a steady clock.

    A clock change (NTP, w32time) then cannot end the run early, stretch it, or
    fake a gap, and every time shown reads like the PC clock did at the start.
    Sleep keeps counting, so a sleeping PC shows up as unmeasured time.
    """

    def __init__(self) -> None:
        self.wall0 = time.time()
        self.mono0 = _monotonic()

    def now(self) -> float:
        return self.wall0 + (_monotonic() - self.mono0)

    def step(self) -> float:
        """How far the PC clock has been moved since the start."""
        return time.time() - self.now()

    def from_wall(self, wall: float) -> float:
        return wall - self.step()


@dataclass(slots=True)  # one per device and minute: keep them small
class Bucket:
    start: float
    sent: int = 0
    lost: int = 0
    rtts: List[float] = field(default_factory=list)
    median: Optional[float] = None
    p95: Optional[float] = None
    max: Optional[float] = None
    closed: bool = False

    @property
    def loss_pct(self) -> float:
        return self.lost / self.sent * 100.0 if self.sent else 0.0

    def close(self) -> None:
        s = summarize(self.rtts)
        if s is not None:
            self.median, self.p95, self.max = s.median, s.p95, s.max
        self.rtts = []
        self.closed = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "sent": self.sent,
            "lost": self.lost,
            "loss_pct": round(self.loss_pct, 3),
            "rtt_median_ms": _r(self.median),
            "rtt_p95_ms": _r(self.p95),
            "rtt_max_ms": _r(self.max),
        }


@dataclass
class Outage:
    name: str
    host: str
    start: float
    end: Optional[float] = None
    lost: int = 0

    def duration(self, now: Optional[float] = None) -> float:
        return max(
            0.0,
            (self.end if self.end is not None else (now or time.time())) - self.start,
        )

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        return {
            "device": self.name,
            "host": self.host,
            "start": self.start,
            "end": self.end,
            "duration_s": round(self.duration(now), 3),
            "lost": self.lost,
            "ongoing": self.end is None,
        }


@dataclass
class Incident:
    start: float
    end: float
    devices: List[str]
    kind: str  # "all" | "some" | "single" | "unlocated"
    members: List[Tuple["DeviceTrack", Outage]] = field(
        default_factory=list, repr=False, compare=False
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "duration_s": round(self.end - self.start, 3),
            "devices": list(self.devices),
            "kind": self.kind,
        }


@dataclass
class NicEvent:
    ts: float
    interface: str
    errors: int = 0
    link_drops: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "interface": self.interface,
            "errors": self.errors,
            "link_drops": self.link_drops,
        }


class DeviceTrack:
    """Everything recorded for one device, fed samples in send-time order."""

    def __init__(
        self,
        device: Device,
        address: Optional[str],
        bucket: float,
        gap_s: float = MIN_GAP_S,
    ) -> None:
        self.device = device
        self.address = address
        self.error: Optional[str] = None if address else "name resolution failed"
        self.bucket_s = bucket
        self.gap_s = gap_s
        self.sent = 0
        self.lost = 0
        self.buckets: Dict[float, Bucket] = {}
        self.outages: List[Outage] = []
        self.gaps: List[Span] = []
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.streak = 0
        self.streak_start: Optional[float] = None
        self.open: Optional[Outage] = None
        self.first_reply: Optional[float] = None
        self._reservoir: List[float] = []
        self._seen_rtts = 0
        self._rng = random.Random(0)

    @property
    def name(self) -> str:
        return self.device.name or self.device.host

    @property
    def identity(self) -> str:
        """Two list rows that ping the same address are one device."""
        return self.address or self.device.host

    @property
    def received(self) -> int:
        return self.sent - self.lost

    def add(
        self, ts: float, rtt: Optional[float], min_lost: int
    ) -> List[Tuple[str, Outage]]:
        events: List[Tuple[str, Outage]] = []
        if self.last_ts is not None and ts - self.last_ts > self.gap_s:
            self._unmeasured(self.last_ts, ts)
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        key = math.floor(ts / self.bucket_s) * self.bucket_s
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = Bucket(start=key)
        b.sent += 1
        self.sent += 1
        if rtt is None:
            b.lost += 1
            self.lost += 1
            if self.streak == 0:
                self.streak_start = ts
            self.streak += 1
            if self.open is not None:
                self.open.lost += 1
            elif self.streak >= min_lost and self.first_reply is not None:
                self.open = Outage(
                    self.name, self.device.host, self.streak_start, lost=self.streak
                )
                self.outages.append(self.open)
                events.append(("outage_start", self.open))
            return events
        if self.first_reply is None:
            self.first_reply = ts
            if self.streak >= min_lost:
                # Down from the start: it is an outage once the device answers.
                # One that never answers is reported as unreachable instead.
                down = Outage(
                    self.name,
                    self.device.host,
                    self.streak_start,
                    end=ts,
                    lost=self.streak,
                )
                self.outages.append(down)
                events.append(("outage_end", down))
        if not b.closed:
            b.rtts.append(rtt)
        self._sample(rtt)
        if self.open is not None:
            self.open.end = ts
            events.append(("outage_end", self.open))
            self.open = None
        self.streak = 0
        self.streak_start = None
        return events

    def _unmeasured(self, start: float, end: float) -> None:
        self.gaps.append((start, end))
        # Losses on both sides of a gap are not one drop, and an open drop has
        # no known end: it is cut where measuring stopped.
        if self.open is not None:
            self.open.end = start
            self.open = None
        self.streak = 0
        self.streak_start = None

    def _sample(self, rtt: float) -> None:
        self._seen_rtts += 1
        if len(self._reservoir) < RESERVOIR:
            self._reservoir.append(rtt)
        else:
            i = self._rng.randrange(self._seen_rtts)
            if i < RESERVOIR:
                self._reservoir[i] = rtt

    def close_until(self, ts: float) -> List[Bucket]:
        """Close (and return) the buckets that end at or before *ts*."""
        closed = []
        for b in self.buckets.values():
            if not b.closed and b.start + self.bucket_s <= ts:
                b.close()
                closed.append(b)
        return closed

    def finish(self) -> List[Bucket]:
        return self.close_until(math.inf)

    # -- summaries --------------------------------------------------------------
    @property
    def loss_pct(self) -> float:
        return self.lost / self.sent * 100.0 if self.sent else 0.0

    @property
    def loss_upper_pct(self) -> Optional[float]:
        return loss_upper_bound_pct(self.lost, self.sent)

    @property
    def spacing(self) -> float:
        """Average time between two requests to this device."""
        if self.sent < 2 or self.first_ts is None or self.last_ts is None:
            return 0.0
        return (self.last_ts - self.first_ts) / (self.sent - 1)

    def rtt_summary(self) -> Dict[str, Optional[float]]:
        s = summarize(self._reservoir)
        if s is None:
            return {"median": None, "p95": None, "max": None}
        # The reservoir is a sample; the true maximum comes from the minutes.
        maxima = [b.max for b in self.buckets.values() if b.max is not None]
        return {"median": s.median, "p95": s.p95, "max": max(maxima, default=s.max)}

    @property
    def never_replied(self) -> bool:
        return self.sent > 0 and self.received == 0

    @property
    def sporadic_loss(self) -> bool:
        return self.lost > 0 and not self.outages and self.received > 0

    def answering_at(self, ts: float, tolerance: float) -> bool:
        """Was this device being measured, and answering, around *ts*?"""
        if self.first_reply is None or self.first_ts is None or self.last_ts is None:
            return False
        if not self.first_ts - tolerance <= ts <= self.last_ts:
            return False
        if any(s < ts < e for s, e in self.gaps):
            return False
        return not any(
            o.start <= ts and (o.end is None or ts < o.end) for o in self.outages
        )

    def longest_outage(self, now: float) -> float:
        return max((o.duration(now) for o in self.outages), default=0.0)

    def downtime(self, now: float) -> float:
        return sum(o.duration(now) for o in self.outages)

    def to_dict(
        self, now: float, status: Status, unmeasured: List[Span]
    ) -> Dict[str, Any]:
        return {
            "device": self.device.to_dict(),
            "address": self.address,
            "error": self.error,
            "status": status.value,
            "sent": self.sent,
            "lost": self.lost,
            "loss_pct": round(self.loss_pct, 4),
            "loss_upper_pct": _r(self.loss_upper_pct),
            "rtt": {k: _r(v) for k, v in self.rtt_summary().items()},
            "outages": [o.to_dict(now) for o in self.outages],
            "longest_outage_s": round(self.longest_outage(now), 3),
            "downtime_s": round(self.downtime(now), 3),
            "unmeasured": [_span_dict(s) for s in unmeasured],
            "unmeasured_s": round(sum(e - s for s, e in unmeasured), 3),
            "buckets": [
                b.to_dict()
                for b in sorted(self.buckets.values(), key=lambda b: b.start)
            ],
        }


def _r(v: Optional[float], digits: int = 3) -> Optional[float]:
    return round(v, digits) if v is not None else None


def _span_dict(span: Span) -> Dict[str, float]:
    return {
        "start": span[0],
        "end": span[1],
        "duration_s": round(span[1] - span[0], 3),
    }


class SoakRecorder:
    """Thread-safe sample sink with a reorder buffer.

    Replies are reported as soon as they arrive, losses only after the reply
    timeout, so samples reach us out of order. They are buffered per device
    and processed in send-time order once older than the timeout window.
    """

    def __init__(
        self,
        tracks: List[DeviceTrack],
        timeout: float,
        min_lost: int = MIN_LOST_FOR_OUTAGE,
        on_event: Optional[EventFn] = None,
        clock: Optional[SoakClock] = None,
        interval: float = 0.0,
    ) -> None:
        self.tracks = tracks
        self.by_addr: Dict[str, List[DeviceTrack]] = {}
        for t in tracks:
            if t.address:
                self.by_addr.setdefault(t.address, []).append(t)
        # A loss is reported up to max(timeout, ping pace) + 0.3 s after its
        # send; ping.exe keeps a 1 s pace.
        self.window = max(timeout, interval, 1.0) + 0.5
        self.min_lost = max(1, min_lost)
        self.on_event = on_event
        self.clock = clock
        self._heap: List[Tuple[float, int, str, Optional[float], Optional[str]]] = []
        self._n = 0
        self._lock = threading.Lock()

    def feed(
        self,
        target: str,
        sent_at: float,
        rtt: Optional[float],
        error: Optional[str] = None,
    ) -> None:
        ts = self.clock.from_wall(sent_at) if self.clock is not None else sent_at
        with self._lock:
            self._n += 1
            heapq.heappush(self._heap, (ts, self._n, target, rtt, error))

    def drain(self, watermark: float) -> None:
        """Process every buffered sample sent before *watermark*."""
        ready = []
        with self._lock:
            while self._heap and self._heap[0][0] <= watermark:
                ready.append(heapq.heappop(self._heap))
        for ts, _n, target, rtt, error in ready:
            for track in self.by_addr.get(target, []):
                if error:
                    track.error = error
                for kind, outage in track.add(ts, rtt, self.min_lost):
                    if self.on_event is not None:
                        self.on_event(kind, {"outage": outage})

    def flush(self, until: float = math.inf) -> None:
        """Process everything sent up to *until*; drop what was sent later."""
        self.drain(until)
        with self._lock:
            self._heap.clear()


# --------------------------------------------------------------------- analysis


def _labels(tracks: List[DeviceTrack]) -> Dict[int, str]:
    """Display name per track; a name two devices share gets its host added."""
    names: Dict[str, set] = {}
    for t in tracks:
        names.setdefault(t.name, set()).add(t.identity)
    return {
        id(t): t.name if len(names[t.name]) == 1 else f"{t.name} ({t.device.host})"
        for t in tracks
    }


def group_incidents(
    tracks: List[DeviceTrack], now: float, tolerance: float
) -> List[Incident]:
    """Group outages that start within *tolerance* seconds of each other.

    Only a common start says "one cause"; a long outage on one device does not
    pull in drops elsewhere that merely happen while it lasts. The scope is
    judged against the devices that were answering at that moment.
    """
    labels = _labels(tracks)
    starts = sorted(
        ((o.start, k, t, o) for k, t in enumerate(tracks) for o in t.outages),
        key=lambda e: (e[0], e[1]),
    )
    incidents: List[Incident] = []
    for start, _k, t, o in starts:
        end = o.end if o.end is not None else now
        if incidents and start <= incidents[-1].start + tolerance:
            inc = incidents[-1]
            inc.end = max(inc.end, end)
            inc.members.append((t, o))
        else:
            incidents.append(Incident(start, end, [], "single", members=[(t, o)]))
    for inc in incidents:
        down = {t.identity for t, _o in inc.members}
        inc.devices = list(dict.fromkeys(labels[id(t)] for t, _o in inc.members))
        witnesses = {
            t.identity
            for t in tracks
            if t.identity not in down and t.answering_at(inc.start, tolerance)
        }
        if len(down) == 1:
            inc.kind = "single" if witnesses else "unlocated"
        elif len(down) >= math.ceil(0.8 * (len(down) + len(witnesses))):
            inc.kind = "all"
        else:
            inc.kind = "some"
    return incidents


def _intersect(a: List[Span], b: List[Span]) -> List[Span]:
    out: List[Span] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a: List[Span], b: List[Span]) -> List[Span]:
    out: List[Span] = []
    for s, e in a:
        for bs, be in b:
            if be <= s or bs >= e:
                continue
            if bs > s:
                out.append((s, bs))
            s = max(s, be)
        if s < e:
            out.append((s, e))
    return out


@dataclass
class Finding:
    status: Status
    subject: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "subject": self.subject,
            "summary": self.summary,
            "evidence": self.evidence,
            "steps": self.steps,
            "code": self.code,
        }


def hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_secs(s: float) -> str:
    if s < 60:
        return tr(f"{s:.1f}秒", f"{s:.1f}s")
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return tr(f"{m}分{sec:02d}秒", f"{m}m{sec:02d}s")
    h, m = divmod(m, 60)
    return tr(f"{h}時間{m:02d}分", f"{h}h{m:02d}m")


@dataclass
class SoakReport:
    tracks: List[DeviceTrack]
    started_at: float
    finished_at: float
    interval: float
    timeout: float
    bucket_s: float
    min_lost: int
    nic_events: List[NicEvent] = field(default_factory=list)
    hostname: str = ""
    interrupted: bool = False
    # Why measuring ended on its own before the planned end (None: it did not).
    stopped_early: Optional[str] = None
    planned_end: Optional[float] = None
    clock_step_s: float = 0.0

    @cached_property
    def incidents(self) -> List[Incident]:
        spacing = max((t.spacing for t in self.tracks), default=0.0)
        tolerance = max(2 * self.interval, 2 * spacing, 1.0)
        return group_incidents(self.tracks, self.finished_at, tolerance)

    @property
    def status(self) -> Status:
        return Status.worst(f.status for f in self.findings())

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    @property
    def multi_day(self) -> bool:
        day = time.localtime(self.started_at)[:3]
        return time.localtime(self.finished_at)[:3] != day

    def at(self, ts: float) -> str:
        """A time of day, dated when the run spans more than one day."""
        fmt = "%m/%d %H:%M:%S" if self.multi_day else "%H:%M:%S"
        return time.strftime(fmt, time.localtime(ts))

    def unmeasured(self, t: DeviceTrack) -> List[Span]:
        """Stretches of the run in which *t* got no request at all."""
        if t.first_ts is None or t.last_ts is None:
            return [(self.started_at, self.finished_at)]
        out: List[Span] = []
        if t.first_ts - self.started_at > t.gap_s:
            out.append((self.started_at, t.first_ts))
        out.extend(t.gaps)
        if self.finished_at - t.last_ts > t.gap_s:
            out.append((t.last_ts, self.finished_at))
        return out

    def device_status(self, t: DeviceTrack) -> Status:
        if not t.sent:
            return Status.UNKNOWN
        if t.never_replied or t.outages:
            return Status.FAIL
        if self.unmeasured(t):
            return Status.UNKNOWN
        if t.lost:
            return Status.WARN
        return Status.OK

    def _nic_near(self, start: float, end: float) -> List[NicEvent]:
        lo, hi = start - self.bucket_s, end + self.bucket_s
        return [e for e in self.nic_events if lo <= e.ts <= hi]

    def _spans_text(self, spans: List[Span], limit: int = 5) -> str:
        return ", ".join(
            f"{self.at(s)}〜{self.at(e)}({fmt_secs(e - s)})" for s, e in spans[:limit]
        )

    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        now = self.finished_at
        this_pc = tr("このPC", "This PC")
        labels = _labels(self.tracks)
        flaps = [e for e in self.nic_events if e.link_drops]
        if flaps:
            times = ", ".join(f"{self.at(e.ts)} {e.interface}" for e in flaps[:5])
            out.append(
                Finding(
                    Status.FAIL,
                    this_pc,
                    tr(
                        "測定中にこのPCのLANリンクが切断されました",
                        "This PC's LAN link went down during the measurement",
                    ),
                    [tr(f"リンク切断を検出: {times}", f"link drops at: {times}")],
                    [
                        tr(
                            "PC〜スイッチ間のLANケーブルを交換する",
                            "Replace the PC-to-switch cable",
                        ),
                        tr(
                            "スイッチの別ポートに挿し替えて再測定する",
                            "Move to another switch port and re-measure",
                        ),
                        tr(
                            "ノートPCの省電力設定（NICの電源管理）を無効にする",
                            "Disable NIC power saving on the laptop",
                        ),
                    ],
                    "soak.pc_link_flap",
                )
            )
        incidents = self.incidents
        common = [i for i in incidents if i.kind == "all"]
        some = [i for i in incidents if i.kind == "some"]
        own: Dict[int, List[Outage]] = {}
        unlocated: Dict[int, List[Outage]] = {}
        for i in incidents:
            if i.kind in ("single", "unlocated"):
                into = own if i.kind == "single" else unlocated
                for t, o in i.members:
                    into.setdefault(id(t), []).append(o)
        if common:
            times = ", ".join(
                f"{self.at(i.start)}〜{self.at(i.end)}({fmt_secs(i.end - i.start)})"
                for i in common[:5]
            )
            nic = [
                e for i in common for e in self._nic_near(i.start, i.end) if e.errors
            ]
            evidence = [
                tr(
                    f"全機器が同時に断: {times}",
                    f"all devices dropped together: {times}",
                )
            ]
            if nic:
                grew = f"{nic[0].interface} +{sum(e.errors for e in nic)}"
                evidence.append(
                    tr(
                        f"同じ時間帯にこのPCのNICエラーが増加（{grew}）",
                        f"this PC's NIC errors grew at the same time ({grew})",
                    )
                )
            out.append(
                Finding(
                    Status.FAIL,
                    tr("全機器", "All devices"),
                    tr(
                        f"全機器が同時に通信断（{len(common)}回）— このPCの接続、または全機器に共通のスイッチ・上流の問題です",
                        f"All devices dropped at the same time ({len(common)}x) — this PC's connection or the shared switch/uplink",
                    ),
                    evidence,
                    [
                        tr(
                            "このPCのLANケーブルとスイッチのポートを替えて再測定する（このPC側の切り分け）",
                            "Swap this PC's cable and switch port and re-measure (rules out this PC's side)",
                        ),
                        tr(
                            "別のPCでも同じ時刻に切れるか、同時に測定して比べる",
                            "Measure from a second PC at the same time and compare",
                        ),
                        tr(
                            "スイッチの電源・温度・ログ（リンクダウン、ループ検知、ストーム）を確認する",
                            "Check the switch: power, temperature, logs (link down, loop detection, storms)",
                        ),
                    ],
                    "soak.common_outage",
                )
            )
        if some:
            groups: Dict[Tuple[str, ...], List[Incident]] = {}
            for i in some:
                groups.setdefault(tuple(sorted(i.devices)), []).append(i)
            group_of = {labels[id(t)]: t.device.group for t in self.tracks}
            for names, incs in groups.items():
                times = ", ".join(
                    f"{self.at(i.start)}({fmt_secs(i.end - i.start)})" for i in incs[:5]
                )
                evidence = [tr(f"同時断: {times}", f"simultaneous drops: {times}")]
                shared = sorted({group_of.get(n) for n in names} - {None, ""})
                if shared:
                    evidence.append(
                        tr(
                            f"台帳のグループ: {', '.join(shared)}",
                            f"device-list group: {', '.join(shared)}",
                        )
                    )
                out.append(
                    Finding(
                        Status.FAIL,
                        ", ".join(names),
                        tr(
                            f"{len(names)}台が同時に通信断（{len(incs)}回）— これらの機器が共有するスイッチ・ケーブル・電源を確認",
                            f"{len(names)} devices dropped together ({len(incs)}x) — check the switch, cabling or power they share",
                        ),
                        evidence,
                        [
                            tr(
                                "これらの機器がつながるスイッチ（またはカスケード接続）を特定し、ポート・電源・ケーブルを確認する",
                                "Identify the switch (or cascade) these devices share; check its ports, power and uplink cable",
                            )
                        ],
                        "soak.group_outage",
                    )
                )
        common_gaps = self._common_unmeasured()
        for t in self.tracks:
            subject = f"{t.name} ({t.device.host})"
            if not t.sent:
                out.append(self._not_measured(t, subject))
                continue
            if t.never_replied:
                out.append(
                    Finding(
                        Status.FAIL,
                        subject,
                        tr(
                            "期間中ずっと応答がありません",
                            "No reply during the whole measurement",
                        ),
                        (
                            [tr(f"エラー: {t.error}", f"error: {t.error}")]
                            if t.error
                            else []
                        ),
                        [
                            tr(
                                f"netdiag check {t.device.host} で経路・ARP・ポートから原因を確認する",
                                f"Run netdiag check {t.device.host} to find out why (route, ARP, ports)",
                            )
                        ],
                        "soak.never_replied",
                    )
                )
                continue
            alone = own.get(id(t), [])
            if alone:
                out.append(self._own_outages(t, subject, alone))
            if unlocated.get(id(t)):
                out.append(self._unlocated_outages(t, subject, unlocated[id(t)]))
            if t.sporadic_loss:
                out.append(
                    Finding(
                        Status.WARN,
                        subject,
                        tr(
                            f"散発的なパケットロス {t.lost}/{t.sent}（{t.loss_pct:.2f}%）— 連続した断はなし",
                            f"Sporadic loss {t.lost}/{t.sent} ({t.loss_pct:.2f}%) — no consecutive drops",
                        ),
                        [],
                        [
                            tr(
                                "同じスイッチの他の機器にも同程度のロスがあるか比べる",
                                "Compare with other devices on the same switch",
                            ),
                            tr(
                                "ケーブル・ノイズ源を確認し、より長く測って傾向を見る",
                                "Check cabling/noise sources and measure longer to see the trend",
                            ),
                        ],
                        "soak.sporadic_loss",
                    )
                )
            gaps = [
                s
                for s in _subtract(self.unmeasured(t), common_gaps)
                if s[1] - s[0] > t.gap_s
            ]
            if gaps:
                evidence = [
                    tr(
                        f"未測定: {self._spans_text(gaps)}",
                        f"not measured: {self._spans_text(gaps)}",
                    )
                ]
                if t.error:
                    evidence.append(tr(f"エラー: {t.error}", f"error: {t.error}"))
                total = fmt_secs(sum(e - s for s, e in gaps))
                out.append(
                    Finding(
                        Status.UNKNOWN,
                        subject,
                        tr(
                            f"この機器を測定できていない時間帯があります（合計 {total}）— その間の通信断は分かりません",
                            f"This device was not measured for {total} — drops in that time are unknown",
                        ),
                        evidence,
                        code="soak.unmeasured_time",
                    )
                )
        nic_err = [e for e in self.nic_events if e.errors and not e.link_drops]
        if nic_err and not common:
            total = sum(e.errors for e in nic_err)
            out.append(
                Finding(
                    Status.WARN,
                    this_pc,
                    tr(
                        f"測定中にこのPCのNICエラーが増加（合計 +{total}）— ケーブル/コネクタ/ノイズ",
                        f"This PC's NIC errors grew during the measurement (+{total}) — cable/connector/noise",
                    ),
                    [
                        ", ".join(
                            f"{self.at(e.ts)} {e.interface} +{e.errors}"
                            for e in nic_err[:5]
                        )
                    ],
                    [
                        tr(
                            "PC〜スイッチ間のケーブルを交換して再測定する",
                            "Replace the PC-to-switch cable and re-measure",
                        )
                    ],
                    "soak.nic_errors",
                )
            )
        if common_gaps:
            total = fmt_secs(sum(e - s for s, e in common_gaps))
            out.append(
                Finding(
                    Status.UNKNOWN,
                    tr("全機器", "All devices"),
                    tr(
                        f"測定できていない時間帯があります（合計 {total}）— その間の通信断は分かりません",
                        f"Nothing was measured for {total} — drops in that time are unknown",
                    ),
                    [
                        tr(
                            f"未測定: {self._spans_text(common_gaps)}",
                            f"not measured: {self._spans_text(common_gaps)}",
                        )
                    ],
                    [
                        tr(
                            "測定中はPCのスリープ・休止状態を無効にする",
                            "Turn off sleep and hibernation on this PC while measuring",
                        ),
                        tr(
                            "netdiag のウィンドウを閉じたり一時停止したりしない",
                            "Do not close or pause the netdiag window while it measures",
                        ),
                    ],
                    "soak.unmeasured_time",
                )
            )
        if self.stopped_early is not None:
            rest = (
                tr(
                    f"予定の終了まで残り {fmt_secs(self.planned_end - now)} は測定していません",
                    f"the remaining {fmt_secs(self.planned_end - now)} were not measured",
                )
                if self.planned_end is not None and self.planned_end > now
                else tr("それ以降は測定していません", "nothing after that was measured")
            )
            out.append(
                Finding(
                    Status.UNKNOWN,
                    tr("測定", "Measurement"),
                    tr(
                        f"測定が {self.at(now)} に止まりました。{rest}",
                        f"Measuring stopped at {self.at(now)}; {rest}",
                    ),
                    [
                        tr(
                            f"理由: {self.stopped_early}",
                            f"reason: {self.stopped_early}",
                        )
                    ],
                    [
                        tr(
                            "原因を取り除いてから、もう一度測定する",
                            "Fix the cause and measure again",
                        )
                    ],
                    "soak.stopped_early",
                )
            )
        if not any(f.status in (Status.FAIL, Status.WARN) for f in out):
            ok = self._no_loss()
            if ok is not None:
                out.append(ok)
        return out

    def _common_unmeasured(self) -> List[Span]:
        """Stretches in which no measured device got a request."""
        measured = [t for t in self.tracks if t.sent]
        if not measured:
            return []
        spans = self.unmeasured(measured[0])
        for t in measured[1:]:
            spans = _intersect(spans, self.unmeasured(t))
        gap_s = min(t.gap_s for t in measured)
        return [s for s in spans if s[1] - s[0] > gap_s]

    def _not_measured(self, t: DeviceTrack, subject: str) -> Finding:
        if not t.address:
            reason = tr("名前解決に失敗", "name resolution failed")
        elif t.error:
            reason = t.error
        else:
            reason = tr("要求を1回も送れませんでした", "no request was sent")
        return Finding(
            Status.UNKNOWN,
            subject,
            tr(f"測定できません（{reason}）", f"Not measured ({reason})"),
            [],
            [
                tr(
                    f"netdiag check {t.device.host} で名前・経路を確認する",
                    f"Run netdiag check {t.device.host} to check the name and the route",
                )
            ],
            "soak.not_measured",
        )

    def _outage_figures(self, outages: List[Outage]) -> Tuple[str, str, str]:
        now = self.finished_at
        longest = fmt_secs(max(o.duration(now) for o in outages))
        total = fmt_secs(sum(o.duration(now) for o in outages))
        times = ", ".join(
            f"{self.at(o.start)}({fmt_secs(o.duration(now))})" for o in outages[:6]
        )
        return longest, total, times

    def _own_outages(
        self, t: DeviceTrack, subject: str, outages: List[Outage]
    ) -> Finding:
        longest, total, times = self._outage_figures(outages)
        return Finding(
            Status.FAIL,
            subject,
            tr(
                f"この機器だけが通信断（{len(outages)}回、最長 {longest}、合計 {total}）",
                f"Only this device dropped ({len(outages)}x, longest {longest}, total {total})",
            ),
            [tr(f"断の時刻: {times}", f"drops at: {times}")],
            [
                tr(
                    "この機器のLANケーブル・コネクタ（振動・可動部・動力線との並走）を確認・交換する",
                    "Check/replace this device's cable and connectors (vibration, moving parts, power-cable runs)",
                ),
                tr(
                    "スイッチ側のポートを替えて再測定する",
                    "Move it to another switch port and re-measure",
                ),
                tr(
                    "断の時刻と設備の動作（起動・負荷・大電流機器の運転）を照合する",
                    "Compare the drop times with machine activity (start-up, load, heavy motors)",
                ),
            ],
            "soak.device_outage",
        )

    def _unlocated_outages(
        self, t: DeviceTrack, subject: str, outages: List[Outage]
    ) -> Finding:
        longest, total, times = self._outage_figures(outages)
        return Finding(
            Status.FAIL,
            subject,
            tr(
                f"通信断（{len(outages)}回、最長 {longest}、合計 {total}）— 同時に応答していた機器がないため、このPC・スイッチ・この機器のどこが原因かは分かりません",
                f"Dropped ({len(outages)}x, longest {longest}, total {total}) — no other device was answering then, so this PC, the switch and the device cannot be told apart",
            ),
            [tr(f"断の時刻: {times}", f"drops at: {times}")],
            [
                tr(
                    "同じスイッチにつながる別の機器（スイッチ自身のIPなど）も一緒に測定する",
                    "Measure another device on the same switch as well (the switch's own IP, for example)",
                ),
                tr(
                    "このPCのLANケーブルとスイッチのポートを替えて再測定する",
                    "Swap this PC's cable and switch port and re-measure",
                ),
            ],
            "soak.outage_unlocated",
        )

    def _no_loss(self) -> Optional[Finding]:
        """The all-clear, quoting the weakest per-device confidence bound."""
        measured = [t for t in self.tracks if t.sent]
        if not measured or any(t.lost for t in measured):
            return None
        fewest = min(t.sent for t in measured)
        upper = loss_upper_bound_pct(0, fewest)
        complete = len(measured) == len(self.tracks) and not any(
            self.unmeasured(t) for t in measured
        )
        subject = (
            tr("全機器", "All devices")
            if complete
            else tr("測定できた範囲", "What was measured")
        )
        sent = sum(t.sent for t in measured)
        return Finding(
            Status.OK,
            subject,
            tr(
                f"通信断・ロスなし（{len(measured)}台・{sent}回中0回。どの機器も真の損失率は95%の確度で {upper:.3f}% 以下）",
                f"No drops or loss (0 of {sent} on {len(measured)} device(s); each device's true loss rate is below {upper:.3f}% at 95% confidence)",
            ),
            code="soak.ok",
        )

    def to_dict(self) -> Dict[str, Any]:
        now = self.finished_at
        return {
            "kind": "soak",
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 3),
            "interrupted": self.interrupted,
            "stopped_early": self.stopped_early,
            "planned_end": self.planned_end,
            "clock_step_s": round(self.clock_step_s, 3),
            "hostname": self.hostname,
            "settings": {
                "interval": self.interval,
                "timeout": self.timeout,
                "bucket_s": self.bucket_s,
                "min_lost": self.min_lost,
            },
            "devices": [
                t.to_dict(now, self.device_status(t), self.unmeasured(t))
                for t in self.tracks
            ],
            "incidents": [i.to_dict() for i in self.incidents],
            "nic_events": [e.to_dict() for e in self.nic_events],
            "findings": [f.to_dict() for f in self.findings()],
        }


# ---------------------------------------------------------------------- running


def nic_deltas(
    before: Dict[str, LinkStats],
    after: Dict[str, LinkStats],
    wired: List[str],
    ts: float,
) -> List[NicEvent]:
    events = []
    for name in wired:
        if name in before and name in after:
            d = after[name].delta(before[name])
            errs, drops = d.errors_total(), d.link_drops()
            if errs > 0 or drops > 0:
                events.append(
                    NicEvent(ts=ts, interface=name, errors=errs, link_drops=drops)
                )
    return events


def gap_threshold(interval: float, timeout: float, n_addresses: int) -> float:
    """Longest wait between two requests to one device that is still normal."""
    from .measure.icmp import MAX_PPS, system_ping_cadence

    spacing = max(interval, n_addresses / MAX_PPS, system_ping_cadence())
    return max(MIN_GAP_S, 3 * spacing + 2 * timeout)


def _pinger_failure(outcome: Dict[str, Any]) -> str:
    if "crash" in outcome:
        return outcome["crash"]
    stats = outcome.get("stats") or {}
    errors = list(dict.fromkeys(s.error for s in stats.values() if s.error))
    if errors:
        return "; ".join(errors)
    return tr(
        "ping の処理が理由不明で終了しました", "the ping loop ended without a reason"
    )


def run_soak(
    devices: List[Device],
    duration: Optional[float],
    interval: float = 0.2,
    timeout: float = 1.0,
    bucket: float = DEFAULT_BUCKET,
    min_lost: int = MIN_LOST_FOR_OUTAGE,
    on_event: Optional[EventFn] = None,
    stop: Optional[threading.Event] = None,
    tick: float = 0.25,
) -> SoakReport:
    """Measure until *duration* elapses or *stop* is set (Ctrl-C sets it)."""
    import platform

    from . import netinfo
    from .measure import ping_stream
    from .measure.icmp import addresses_for

    stop = stop or threading.Event()
    clock = SoakClock()
    addr = addresses_for(d.host for d in devices)
    addrs = list(dict.fromkeys(a for a in addr.values() if a))
    gap_s = gap_threshold(interval, timeout, len(addrs))
    tracks = [DeviceTrack(d, addr[d.host], bucket, gap_s) for d in devices]
    if on_event is not None:
        for t in tracks:
            if not t.address:
                on_event("not_measured", {"track": t})
    rec = SoakRecorder(
        tracks,
        timeout=timeout,
        min_lost=min_lost,
        on_event=on_event,
        clock=clock,
        interval=interval,
    )
    hn = netinfo.snapshot()
    wired = [i.name for i in hn.interfaces if i.kind == netinfo.KIND_ETHERNET]
    counters = netinfo.link_stats()
    nic_events: List[NicEvent] = []
    started = clock.now()
    deadline = started + duration if duration is not None else math.inf
    outcome: Dict[str, Any] = {}

    def ping() -> None:
        try:
            outcome["stats"] = ping_stream(
                targets=addrs,
                on_sample=rec.feed,
                stop=stop,
                interval=interval,
                timeout=timeout,
            )
        except Exception as e:  # reported as the reason measuring stopped
            outcome["crash"] = f"{type(e).__name__}: {e}"

    pinger = threading.Thread(target=ping, daemon=True)
    if addrs:
        pinger.start()
    next_tick = started + bucket
    interrupted = False
    try:
        while addrs and not stop.is_set():
            now = clock.now()
            if now >= deadline or not pinger.is_alive():
                break
            watermark = now - rec.window
            rec.drain(watermark)
            if now >= next_tick:
                fresh = netinfo.link_stats()
                events = nic_deltas(counters, fresh, wired, now)
                counters = fresh or counters
                nic_events.extend(events)
                # Every sample sent before the watermark was drained above, so
                # minutes ending there are complete.
                for t in tracks:
                    closed = t.close_until(watermark)
                    if closed and on_event is not None:
                        on_event("bucket", {"track": t, "buckets": closed})
                if on_event is not None:
                    on_event("tick", {"ts": now, "nic": events})
                next_tick += bucket
                if next_tick <= now:  # woke from a stall: one tick, not a backlog
                    next_tick = now + bucket
            stop.wait(min(tick, max(0.0, deadline - now)))
        interrupted = stop.is_set()
    except KeyboardInterrupt:
        interrupted = True
    stopped_at = min(clock.now(), deadline)
    stopped_early = None
    if addrs and not interrupted and not pinger.is_alive() and stopped_at < deadline:
        stopped_early = _pinger_failure(outcome)
    if addrs and on_event is not None:
        on_event("stopping", {"interrupted": interrupted, "reason": stopped_early})
    stop.set()
    if addrs:
        pinger.join(rec.window + 5.0)
    finished = stopped_at
    rec.flush(finished)  # a request sent after the stop is not part of the run
    stats = outcome.get("stats") or {}
    for t in tracks:
        st = stats.get(t.address) if t.address else None
        if st is not None and st.error:
            t.error = st.error
    nic_events.extend(nic_deltas(counters, netinfo.link_stats(), wired, clock.now()))
    for t in tracks:
        t.finish()
    return SoakReport(
        tracks=tracks,
        started_at=started,
        finished_at=finished,
        interval=interval,
        timeout=timeout,
        bucket_s=bucket,
        min_lost=min_lost,
        nic_events=nic_events,
        hostname=platform.node(),
        interrupted=interrupted,
        stopped_early=stopped_early,
        planned_end=deadline if duration is not None else None,
        clock_step_s=clock.step(),
    )


def parse_duration(text: str) -> float:
    """``600`` / ``90s`` / ``10m`` / ``2h`` / ``1h30m`` -> seconds."""
    import re

    t = str(text).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", t)
    if not parts or "".join(a + b for a, b in parts) != re.sub(r"\s+", "", t):
        raise ValueError(f"invalid duration: {text!r} (examples: 600, 10m, 2h, 1h30m)")
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0}
    return sum(float(v) * scale[u] for v, u in parts)
