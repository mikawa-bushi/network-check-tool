"""One complete check: this PC's network plus every listed device.

``run_check`` is what ``netdiag check`` executes. It is organised for speed
*and* measurement quality on a line with dozens of devices:

1. Snapshot the NIC error counters, then run the host sweep (this PC).
2. Resolve every device and decide its route once (a local computation).
3. Ping all routable devices in **one** paced sweep (see
   :mod:`netdiag.measure.icmp`), reading the ARP table before and after so the
   evidence about each device is fresh.
4. Diagnose each device with the ``route → reach → ports`` probes (in
   parallel; port checks only run for devices that are actually present).
5. Re-read the NIC counters: errors that *increased during the test* are
   strong evidence of a physical-layer problem on this PC's link, and are
   folded into the host diagnosis.
"""

from __future__ import annotations

import platform
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import netinfo
from . import probes as _probes  # noqa: F401  -- registers built-in probes
from .core import CheckResult, Context, Probe, Status, register
from .diagnosis import Diagnosis, Engine
from .i18n import Text, get_language, tr
from .inventory import Device
from .measure import ping_many
from .measure.icmp import _literal_family, resolve_all
from .probes._common import default_ping_count, oscmd

DEVICE_PROBES = ["route", "reach", "ports"]
DEVICE_WORKERS = 16

ProgressFn = Callable[[str], None]


@dataclass(frozen=True)
class DeviceMeasurement:
    address: Optional[str]
    sent: Optional[int]
    received: Optional[int]
    loss_pct: Optional[float]
    loss_upper_pct: Optional[float]
    rtt_median_ms: Optional[float]
    rtt_p95_ms: Optional[float]
    rtt_max_ms: Optional[float]
    jitter_ms: Optional[float]
    mac: str
    vendor: str


@dataclass
class DeviceReport:
    device: Device
    diagnosis: Optional[Diagnosis] = None
    skipped_reason: Optional[str] = None

    @property
    def status(self) -> Status:
        return diagnosis_status(self.diagnosis) if self.diagnosis else Status.SKIP

    def result(self, name: str) -> Optional[CheckResult]:
        if self.diagnosis is None:
            return None
        return self.diagnosis.results_by_name().get(name)

    @property
    def measurement(self) -> DeviceMeasurement:
        route, reach = self.result("route"), self.result("reach")
        detail = reach.detail if reach is not None else {}
        ping = detail.get("ping") or {}
        rtt = ping.get("rtt") or {}
        return DeviceMeasurement(
            address=route.detail.get("address") if route is not None else None,
            sent=ping.get("sent"),
            received=ping.get("received"),
            loss_pct=ping.get("loss_pct"),
            loss_upper_pct=ping.get("loss_upper_pct"),
            rtt_median_ms=rtt.get("median"),
            rtt_p95_ms=rtt.get("p95"),
            rtt_max_ms=rtt.get("max"),
            jitter_ms=rtt.get("jitter"),
            mac=detail.get("mac") or "",
            vendor=detail.get("vendor") or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        from .report import diagnosis_to_dict

        return {
            "device": self.device.to_dict(),
            "status": self.status.value,
            "skipped_reason": self.skipped_reason,
            "diagnosis": diagnosis_to_dict(self.diagnosis) if self.diagnosis else None,
        }


@dataclass
class CheckReport:
    host: Diagnosis
    devices: List[DeviceReport] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    hostname: str = field(default_factory=platform.node)
    platform: str = field(default_factory=platform.platform)
    settings: Dict[str, Any] = field(default_factory=dict)

    @property
    def host_status(self) -> Status:
        return diagnosis_status(self.host)

    @property
    def status(self) -> Status:
        return Status.worst([self.host_status] + [d.status for d in self.devices])

    @property
    def exit_code(self) -> int:
        """0 healthy, 1 a failure was found, 2 warnings only."""
        return self.status.exit_code

    def to_dict(self) -> Dict[str, Any]:
        from .report import diagnosis_to_dict

        counts = {s.value: 0 for s in Status}
        for d in self.devices:
            counts[d.status.value] += 1
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "hostname": self.hostname,
            "platform": self.platform,
            "language": get_language(),
            "settings": self.settings,
            "host": diagnosis_to_dict(self.host),
            "device_counts": counts,
            "devices": [d.to_dict() for d in self.devices],
        }


def diagnosis_status(diag: Optional[Diagnosis]) -> Status:
    if diag is None:
        return Status.SKIP
    if diag.healthy:
        return Status.OK
    if any(h.is_fault for h in diag.hypotheses) or any(
        r.status == Status.FAIL for r in diag.results
    ):
        return Status.FAIL
    return Status.WARN


# ------------------------------------------------------------- live NIC errors


@register
class LinkErrorsProbe(Probe):
    """Errors that increased on a wired NIC *while netdiag was measuring*.

    Not part of any sweep on its own (it needs a "before" snapshot); the runner
    evaluates it after the device measurements.
    """

    name = "link_errors"
    title = Text("測定中のNICエラー", "NIC errors during the test")
    layer = "L1"
    default = False
    what = Text(
        "測定の前後でNICのエラーカウンタが増えたかを確認します。",
        "Compares NIC error counters before and after the measurement.",
    )
    why = Text(
        "測定中に増えたCRC/受信エラーは、いま起きている物理層（ケーブル・コネクタ・ノイズ・ポート）の問題の直接の証拠です。",
        "CRC/receive errors that grow during the test are direct evidence of a "
        "current physical-layer problem (cable, connector, noise, port).",
    )

    def check(self, ctx: Context) -> CheckResult:
        before: Dict[str, netinfo.LinkStats] = ctx.get("_counters_before") or {}
        after: Dict[str, netinfo.LinkStats] = ctx.get("_counters_after") or {}
        hn = ctx.network()
        wired = {i.name for i in hn.interfaces if i.kind == netinfo.KIND_ETHERNET}
        grew = []
        flapped = []
        for name in sorted(wired & set(before) & set(after)):
            d = after[name].delta(before[name])
            errs = d.errors_total()
            if errs > 0:
                grew.append((name, errs, d))
            if d.link_drops() > 0:
                flapped.append((name, d.link_drops(), d))
        if not before or not after:
            return CheckResult.skip(
                self.name,
                tr("NICカウンタを取得できません", "NIC counters unavailable"),
                code="link_errors.unavailable",
            )
        if flapped:
            text = ", ".join(f"{n} {c}" for n, c, _d in flapped)
            return CheckResult.fail(
                self.name,
                tr(
                    f"測定中にこのPCのリンクが切断されました（{text} 回）— ケーブル/コネクタ/スイッチのポート",
                    f"this PC's link went down during the test ({text} time(s)) — cable/connector/switch port",
                ),
                metrics={f"link_drops.{n}": float(c) for n, c, _d in flapped},
                detail={n: d.to_dict() for n, _c, d in flapped},
                code="link_errors.link_flap",
            )
        if grew:
            text = ", ".join(f"{n} +{e}" for n, e, _d in grew)
            return CheckResult.warn(
                self.name,
                tr(
                    f"測定中にNICエラーが増加: {text}（ケーブル/コネクタ/ノイズ/ポートの疑い）",
                    f"NIC errors increased during the test: {text} (cable/connector/noise/port)",
                ),
                metrics={f"errors_delta.{n}": float(e) for n, e, _d in grew},
                detail={n: d.to_dict() for n, _e, d in grew},
                code="link_errors.increased",
            )
        return CheckResult.ok(
            self.name,
            tr("測定中のNICエラー増加なし", "no NIC errors during the test"),
            code="link_errors.ok",
        )

    def advice(self, result, results=None):
        return (
            tr(
                "このPCの有線リンクで、測定中にエラーが発生しています（物理層の問題）。",
                "This PC's wired link produced errors during the test (physical layer).",
            ),
            [
                tr(
                    "PC〜スイッチ間のLANケーブルを交換して再測定する",
                    "Replace the PC-to-switch cable and re-test",
                ),
                tr(
                    "スイッチの別ポートに挿し替えて再測定する",
                    "Try another switch port",
                ),
                tr(
                    "動力線・インバータからケーブルを離す、シールドケーブルにする",
                    "Route the cable away from power lines/inverters; use shielded cable",
                ),
                oscmd("ip -s link show <iface>", "Get-NetAdapterStatistics"),
            ],
        )


# -------------------------------------------------------------------- the run


def run_check(
    devices: Optional[List[Device]] = None,
    config: Optional[Dict[str, Any]] = None,
    only: Optional[List[str]] = None,
    skip: Optional[List[str]] = None,
    jobs: int = 1,
    include_host: bool = True,
    progress: Optional[ProgressFn] = None,
    on_probe: Optional[Callable[[str], None]] = None,
) -> CheckReport:
    """Run the host sweep and (optionally) check every device."""
    devices = list(devices or [])
    cfg = dict(config or {})
    cfg.setdefault("count", default_ping_count())
    started = time.time()

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    netinfo.clear_cache()
    hn = netinfo.snapshot()
    cfg["_netinfo"] = hn
    # The snapshot already carries counters (sysfs / Get-NetAdapterStatistics),
    # so the "before" side costs nothing extra.
    counters_before = {i.name: i.stats for i in hn.interfaces if i.stats is not None}
    if not counters_before:
        counters_before = netinfo.link_stats()

    if include_host:
        say(tr("このPCのネットワークを確認中…", "Checking this PC's network…"))
        host_diag = Engine(config=cfg).run(
            only=only, skip=skip, jobs=jobs, on_probe=on_probe
        )
    else:
        host_diag = Diagnosis(results=[], hypotheses=[], healthy=True)

    reports: List[DeviceReport] = []
    if devices:
        reports = _check_devices(devices, cfg, host_diag, hn, say)

    # Errors that grew during the whole measurement (host + devices).
    lk = Engine(
        config={
            **cfg,
            "_counters_before": counters_before,
            "_counters_after": netinfo.link_stats(),
        }
    ).run(only=["link_errors"])
    extra = [r for r in lk.results if r.status != Status.SKIP]
    if extra:
        host_diag = Engine(config=cfg).diagnose(
            host_diag.results + extra, host_diag.reasoning
        )

    return CheckReport(
        host=host_diag,
        devices=reports,
        started_at=started,
        finished_at=time.time(),
        settings={
            k: cfg.get(k)
            for k in (
                "count",
                "interval",
                "ping_timeout",
                "tcp_attempts",
                "tcp_timeout",
                "internet",
            )
            if k in cfg
        },
    )


def _check_devices(
    devices: List[Device],
    cfg: Dict[str, Any],
    host_diag: Diagnosis,
    hn: netinfo.HostNetwork,
    say: ProgressFn,
) -> List[DeviceReport]:
    iface = host_diag.results_by_name().get("interface")
    if iface is not None and iface.status == Status.FAIL:
        reason = tr(
            "このPCのネットワークアダプタに問題があるため未評価",
            "not evaluated: this PC's network interface is down",
        )
        return [DeviceReport(device=d, skipped_reason=reason) for d in devices]

    say(
        tr(
            f"{len(devices)} 台の機器の経路を確認中…",
            f"Resolving {len(devices)} device(s)…",
        )
    )
    names = [d.host for d in devices if _literal_family(d.host) is None]
    resolved = resolve_all(names, timeout=float(cfg.get("dns_timeout", 3.0)))
    addr: Dict[int, Optional[str]] = {}
    for i, d in enumerate(devices):
        addr[i] = d.host if _literal_family(d.host) else resolved.get(d.host)

    targets = []
    for i, ip in addr.items():
        if not ip:
            continue
        dec = hn.decide_route(ip)
        if dec.special is None and (dec.on_link or dec.gateway):
            targets.append(ip)
        elif dec.special == "ipv6":
            targets.append(ip)
    targets = list(dict.fromkeys(targets))

    neigh_before = {n.ip: n for n in netinfo.neighbors()}
    count = int(cfg.get("count") or default_ping_count())
    interval = float(cfg.get("interval") or 0.2)
    timeout = float(cfg.get("ping_timeout") or 1.0)
    stats = {}
    window = 0.0
    if targets:
        say(
            tr(
                f"{len(targets)} 台にping中（{count}回ずつ）…",
                f"Pinging {len(targets)} device(s) ({count} each)…",
            )
        )
        t0 = time.monotonic()
        stats = ping_many(targets, count=count, interval=interval, timeout=timeout)
        window = time.monotonic() - t0
    neigh_after = {n.ip: n for n in netinfo.neighbors()} if targets else {}
    # Silent on-link devices: ask for their ARP answer directly (on Windows,
    # SendARP returns a definite "nobody answered"), in parallel.
    silent = [
        ip
        for ip in targets
        if stats.get(ip) is not None
        and not stats[ip].any_reply
        and hn.decide_route(ip).on_link
    ]
    if silent:
        with ThreadPoolExecutor(max_workers=min(16, len(silent))) as pool:
            for ip, n in zip(silent, pool.map(netinfo.neighbor, silent)):
                if n is not None:
                    neigh_after[ip] = n

    say(tr("機器ごとに診断中…", "Diagnosing each device…"))

    def one(i: int) -> DeviceReport:
        d = devices[i]
        ip = addr[i]
        dcfg = dict(cfg)
        dcfg["target"] = d.to_dict()
        if ip is not None:
            dcfg["_address"] = ip
            dcfg["_ping"] = stats.get(ip)
            dcfg["_ping_window"] = window
            dcfg["_neighbor_before"] = neigh_before.get(ip)
            dcfg["_neighbor_after"] = neigh_after.get(ip) or _absent_or_none(
                ip, hn, neigh_after
            )
        engine = Engine(config=dcfg)
        # No ports listed -> no port check (and no misleading "ports skipped").
        probes = (
            DEVICE_PROBES if d.ports else [p for p in DEVICE_PROBES if p != "ports"]
        )
        diag = reconcile_device(engine, engine.run(only=probes))
        return DeviceReport(device=d, diagnosis=diag)

    with ThreadPoolExecutor(
        max_workers=max(1, min(DEVICE_WORKERS, len(devices)))
    ) as pool:
        return list(pool.map(one, range(len(devices))))


def reconcile_device(engine: Engine, diag: Diagnosis) -> Diagnosis:
    """Let later evidence settle an earlier, inconclusive verdict.

    A device that ignores ping but whose service ports accept connections is
    working: the application traffic is what matters on the line. ICMP silence
    is then reported as a note (OK), not a warning that sends someone hunting.
    """
    by = diag.results_by_name()
    reach, ports = by.get("reach"), by.get("ports")
    if (
        reach is not None
        and ports is not None
        and reach.code in ("reach.icmp_blocked", "reach.no_icmp")
        and ports.status == Status.OK
    ):
        reach.status = Status.OK
        reach.code = "reach.icmp_silent_ports_ok"
        reach.summary = tr(
            "pingには応答しない設定ですが、サービスポートで動作を確認しました",
            "does not answer ping, but its service ports work",
        )
        return engine.diagnose(diag.results, diag.reasoning)
    return diag


# Quick host-side probes for an on-demand diagnosis of one target (no internet
# assumptions, no slow optional checks).
QUICK_HOST_PROBES = ["interface", "link", "ipconfig", "gateway", "neighbor"]


def diagnose_target(
    host: str,
    ports: Optional[List[int]] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Why is *host* unreachable? Returns the most relevant :class:`Hypothesis`.

    Used by monitors when a device degrades: checks this PC's own network first
    (a broken local link explains every device at once), then the device itself
    (route, ARP presence, ping statistics, ports). Returns None when nothing is
    wrong any more.
    """
    device = Device(name=host, host=host, ports=list(ports or []))
    return _diagnose([device], config)[host]


def diagnose_targets(
    hosts: List[str], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """:func:`diagnose_target` for several hosts at once: ``{host: Hypothesis}``.

    One check covers them all -- this PC's network is examined once and the
    devices side by side -- so a whole line going down costs one diagnosis,
    not one per device.
    """
    hosts = list(dict.fromkeys(hosts))
    return _diagnose([Device(name=h, host=h) for h in hosts], config) if hosts else {}


def _diagnose(devices: List[Device], config: Optional[Dict[str, Any]]):
    cfg = {"internet": False, **(config or {})}
    rep = run_check(devices, config=cfg, only=QUICK_HOST_PROBES)
    host_fail = [h for h in rep.host.hypotheses if h.is_fault]
    found = {}
    for r in rep.devices:
        dev = r.diagnosis
        if host_fail:
            found[r.device.host] = host_fail[0]
        elif dev is not None and dev.primary is not None:
            found[r.device.host] = dev.primary
        else:
            found[r.device.host] = rep.host.primary
    return found


def _absent_or_none(ip: str, hn: netinfo.HostNetwork, table: Dict[str, Any]):
    """No ARP entry after pinging an on-link address means ARP got no answer.

    (Windows' ``arp -a`` and macOS list only resolved entries; Linux keeps a
    FAILED/INCOMPLETE entry, which the table lookup already returns.)
    """
    if not table:
        return None
    dec = hn.decide_route(ip)
    if dec.on_link and dec.special is None:
        return netinfo.Neighbor(ip=ip, state="incomplete")
    return None
