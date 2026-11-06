"""Long-duration measurement for intermittent faults ("ときどき切れる").

A spot check cannot catch a link that drops for eight seconds twice an hour.
``netdiag soak`` pings every device continuously (see
:func:`netdiag.measure.ping_stream`) and records, per device:

* **outages** — two or more consecutive lost requests, with start/end time and
  duration (the moments a cyclic PLC connection would have timed out);
* **per-minute statistics** — sent, lost, median/p95/max RTT;
* the PC's own **NIC error counters and link drops**, sampled every minute.

Outages that overlap in time are grouped into incidents, and the grouping is
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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import Status
from .i18n import tr
from .inventory import Device
from .measure.stats import loss_upper_bound_pct, summarize
from .netinfo import LinkStats

DEFAULT_BUCKET = 60.0
MIN_LOST_FOR_OUTAGE = 2
RESERVOIR = 4000

EventFn = Callable[[str, Dict[str, Any]], None]


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
    kind: str  # "all" | "some" | "single"

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

    def __init__(self, device: Device, address: Optional[str], bucket: float) -> None:
        self.device = device
        self.address = address
        self.error: Optional[str] = None if address else "name resolution failed"
        self.bucket_s = bucket
        self.sent = 0
        self.lost = 0
        self.buckets: Dict[float, Bucket] = {}
        self.outages: List[Outage] = []
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
    def received(self) -> int:
        return self.sent - self.lost

    def add(
        self, ts: float, rtt: Optional[float], min_lost: int
    ) -> List[Tuple[str, Outage]]:
        events: List[Tuple[str, Outage]] = []
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
                # Only a device that has answered before can "go down"; one
                # that never answers is reported as unreachable instead.
                self.open = Outage(
                    self.name, self.device.host, self.streak_start, lost=self.streak
                )
                self.outages.append(self.open)
                events.append(("outage_start", self.open))
            return events
        if self.first_reply is None:
            self.first_reply = ts
        b.rtts.append(rtt)
        self._sample(rtt)
        if self.open is not None:
            self.open.end = ts
            events.append(("outage_end", self.open))
            self.open = None
        self.streak = 0
        self.streak_start = None
        return events

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

    def longest_outage(self, now: float) -> float:
        return max((o.duration(now) for o in self.outages), default=0.0)

    def downtime(self, now: float) -> float:
        return sum(o.duration(now) for o in self.outages)

    def status(self) -> Status:
        if self.error or self.sent == 0:
            return Status.SKIP
        if self.received == 0 or self.outages:
            return Status.FAIL
        if self.lost:
            return Status.WARN
        return Status.OK

    def to_dict(self, now: float) -> Dict[str, Any]:
        return {
            "device": self.device.to_dict(),
            "address": self.address,
            "error": self.error,
            "status": self.status().value,
            "sent": self.sent,
            "lost": self.lost,
            "loss_pct": round(self.loss_pct, 4),
            "loss_upper_pct": _r(self.loss_upper_pct),
            "rtt": {k: _r(v) for k, v in self.rtt_summary().items()},
            "outages": [o.to_dict(now) for o in self.outages],
            "longest_outage_s": round(self.longest_outage(now), 3),
            "downtime_s": round(self.downtime(now), 3),
            "buckets": [
                b.to_dict()
                for b in sorted(self.buckets.values(), key=lambda b: b.start)
            ],
        }


def _r(v: Optional[float], digits: int = 3) -> Optional[float]:
    return round(v, digits) if v is not None else None


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
    ) -> None:
        self.tracks = tracks
        self.by_addr: Dict[str, List[DeviceTrack]] = {}
        for t in tracks:
            if t.address:
                self.by_addr.setdefault(t.address, []).append(t)
        self.window = timeout + 0.5
        self.min_lost = max(1, min_lost)
        self.on_event = on_event
        self._heap: List[Tuple[float, int, str, Optional[float]]] = []
        self._n = 0
        self._lock = threading.Lock()

    def feed(
        self, target: str, sent_at: float, rtt: Optional[float], _error=None
    ) -> None:
        with self._lock:
            self._n += 1
            heapq.heappush(self._heap, (sent_at, self._n, target, rtt))

    def drain(self, watermark: float) -> None:
        """Process every buffered sample sent before *watermark*."""
        ready = []
        with self._lock:
            while self._heap and self._heap[0][0] <= watermark:
                ready.append(heapq.heappop(self._heap))
        for ts, _n, target, rtt in ready:
            for track in self.by_addr.get(target, []):
                for kind, outage in track.add(ts, rtt, self.min_lost):
                    if self.on_event is not None:
                        self.on_event(kind, {"outage": outage})

    def flush(self) -> None:
        self.drain(float("inf"))


# --------------------------------------------------------------------- analysis


def group_incidents(
    tracks: List[DeviceTrack], now: float, tolerance: float
) -> List[Incident]:
    """Merge outages that overlap (within *tolerance* seconds) across devices."""
    alive = [t for t in tracks if t.first_reply is not None]
    spans = sorted(
        (o.start, o.end if o.end is not None else now, o.name)
        for t in alive
        for o in t.outages
    )
    incidents: List[Incident] = []
    for start, end, name in spans:
        if incidents and start <= incidents[-1].end + tolerance:
            inc = incidents[-1]
            inc.end = max(inc.end, end)
            if name not in inc.devices:
                inc.devices.append(name)
        else:
            incidents.append(
                Incident(start=start, end=end, devices=[name], kind="single")
            )
    n_alive = len(alive)
    for inc in incidents:
        if len(inc.devices) == 1:
            inc.kind = "single"
        elif n_alive >= 2 and len(inc.devices) >= max(2, math.ceil(0.8 * n_alive)):
            inc.kind = "all"
        else:
            inc.kind = "some"
    return incidents


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

    @property
    def incidents(self) -> List[Incident]:
        return group_incidents(
            self.tracks, self.finished_at, tolerance=max(2 * self.interval, 1.0)
        )

    @property
    def status(self) -> Status:
        return Status.worst(f.status for f in self.findings())

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    def _nic_near(self, start: float, end: float) -> List[NicEvent]:
        lo, hi = start - self.bucket_s, end + self.bucket_s
        return [e for e in self.nic_events if lo <= e.ts <= hi]

    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        now = self.finished_at
        this_pc = tr("このPC", "This PC")
        flaps = [e for e in self.nic_events if e.link_drops]
        if flaps:
            times = ", ".join(f"{hhmmss(e.ts)} {e.interface}" for e in flaps[:5])
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
        if common:
            times = ", ".join(
                f"{hhmmss(i.start)}〜{hhmmss(i.end)}({fmt_secs(i.end - i.start)})"
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
            group_of = {t.name: t.device.group for t in self.tracks}
            for names, incs in groups.items():
                times = ", ".join(
                    f"{hhmmss(i.start)}({fmt_secs(i.end - i.start)})" for i in incs[:5]
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
        in_shared = {n for i in common + some for n in i.devices}
        for t in self.tracks:
            if t.error:
                out.append(
                    Finding(
                        Status.SKIP,
                        t.name,
                        tr(f"測定できません（{t.error}）", f"not measured ({t.error})"),
                    )
                )
                continue
            if t.never_replied:
                out.append(
                    Finding(
                        Status.FAIL,
                        f"{t.name} ({t.device.host})",
                        tr(
                            "期間中ずっと応答がありません",
                            "No reply during the whole measurement",
                        ),
                        [],
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
            alone = [
                o
                for o in t.outages
                if t.name not in in_shared or self._alone(o, incidents)
            ]
            if alone:
                longest = fmt_secs(max(o.duration(now) for o in alone))
                total = fmt_secs(sum(o.duration(now) for o in alone))
                times = ", ".join(
                    f"{hhmmss(o.start)}({fmt_secs(o.duration(now))})" for o in alone[:6]
                )
                out.append(
                    Finding(
                        Status.FAIL,
                        f"{t.name} ({t.device.host})",
                        tr(
                            f"この機器だけが通信断（{len(alone)}回、最長 {longest}、合計 {total}）",
                            f"Only this device dropped ({len(alone)}x, longest {longest}, total {total})",
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
                )
            elif t.sporadic_loss:
                out.append(
                    Finding(
                        Status.WARN,
                        f"{t.name} ({t.device.host})",
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
                            f"{hhmmss(e.ts)} {e.interface} +{e.errors}"
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
        if not any(f.status in (Status.FAIL, Status.WARN) for f in out):
            sent = sum(t.sent for t in self.tracks)
            upper = loss_upper_bound_pct(0, sent)
            out.append(
                Finding(
                    Status.OK,
                    tr("全機器", "All devices"),
                    (
                        tr(
                            f"通信断・ロスなし（{sent}回中0回。真の損失率は95%の確度で {upper:.3f}% 以下）",
                            f"No drops or loss (0 of {sent}; true loss rate below {upper:.3f}% at 95% confidence)",
                        )
                        if upper is not None
                        else tr("測定データがありません", "no data")
                    ),
                    code="soak.ok",
                )
            )
        return out

    @staticmethod
    def _alone(outage: Outage, incidents: List[Incident]) -> bool:
        for i in incidents:
            if i.start - 1 <= outage.start <= i.end + 1 and outage.name in i.devices:
                return i.kind == "single"
        return True

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
            "hostname": self.hostname,
            "settings": {
                "interval": self.interval,
                "timeout": self.timeout,
                "bucket_s": self.bucket_s,
                "min_lost": self.min_lost,
            },
            "devices": [t.to_dict(now) for t in self.tracks],
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
    addr = addresses_for(d.host for d in devices)
    tracks = [DeviceTrack(d, addr[d.host], bucket) for d in devices]
    rec = SoakRecorder(tracks, timeout=timeout, min_lost=min_lost, on_event=on_event)
    hn = netinfo.snapshot()
    wired = [i.name for i in hn.interfaces if i.kind == netinfo.KIND_ETHERNET]
    counters = netinfo.link_stats()
    nic_events: List[NicEvent] = []
    started = time.time()
    addrs = list(dict.fromkeys(t.address for t in tracks if t.address))

    pinger = threading.Thread(
        target=ping_stream,
        kwargs=dict(
            targets=addrs,
            on_sample=rec.feed,
            stop=stop,
            interval=interval,
            timeout=timeout,
        ),
        daemon=True,
    )
    if addrs:
        pinger.start()
    next_tick = started + bucket
    interrupted = False
    try:
        while not stop.is_set():
            now = time.time()
            if duration is not None and now - started >= duration:
                break
            if addrs and not pinger.is_alive():
                break
            rec.drain(now - rec.window)
            if now >= next_tick:
                fresh = netinfo.link_stats()
                events = nic_deltas(counters, fresh, wired, now)
                counters = fresh or counters
                nic_events.extend(events)
                # Every sample sent before the watermark was drained above, so
                # minutes ending there are complete.
                for t in tracks:
                    closed = t.close_until(now - rec.window)
                    if closed and on_event is not None:
                        on_event("bucket", {"track": t, "buckets": closed})
                if on_event is not None:
                    on_event("tick", {"ts": now, "nic": events})
                next_tick += bucket
            stop.wait(tick)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        if addrs:
            pinger.join(timeout + 5)
    rec.flush()
    finished = time.time()
    nic_events.extend(nic_deltas(counters, netinfo.link_stats(), wired, finished))
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
