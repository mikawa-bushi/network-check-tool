"""Precise, unprivileged ICMP echo measurement.

Three back-ends, chosen automatically:

* **ICMP datagram socket** (Linux, macOS): ``socket(AF_INET, SOCK_DGRAM,
  IPPROTO_ICMP)`` needs no root, gives microsecond timing, sub-second send
  intervals and detects duplicate replies (two devices answering for one IP).
  One socket and one thread serve many targets at once, like ``fping``, so
  checking 30 devices takes as long as checking one.
* **Windows ICMP API** (``IcmpSendEcho``, see :mod:`.winicmp`): the same
  freedom on Windows without administrator rights — free intervals,
  sub-millisecond timing, duplicate answers, explicit "unreachable" statuses.
* **System ``ping`` command** (fallback when neither is usable):
  run in parallel per target and parsed *per reply line* in a language-neutral
  way. Japanese Windows prints ``時間 <1ms`` and ``(25% の損失)``; English prints
  ``time<1ms`` and ``(25% loss)`` — the parser relies only on the ``=``/``<``
  and ``ms`` tokens. Counting reply lines also fixes the classic Windows gotcha
  where "Destination host unreachable" replies are reported as *received* (0 %
  loss) in ping's own statistics.

All produce a :class:`PingStats` with per-reply RTTs, so every report shows the
same statistics (loss with a 95 % upper bound, min/median/p95/max, jitter).
:func:`ping_stream` runs the same back-ends open-ended for long measurements.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import selectors
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.util import have, is_macos, is_windows, run
from .stats import RttSummary, loss_upper_bound_pct, summarize

DEFAULT_COUNT = 10
DEFAULT_INTERVAL = 0.2  # seconds between echo requests to one target
DEFAULT_TIMEOUT = 1.0  # seconds to wait for each reply
DEFAULT_SIZE = 32  # payload bytes (Windows ping's default)
MAX_PPS = 200  # global send-rate ceiling across all targets (gentle on OT gear)

_ECHO_REQUEST = 8
_ECHO_REPLY = 0
_MAGIC = b"NDGP"


@dataclass
class PingStats:
    """Outcome of pinging one target ``count`` times."""

    target: str
    address: Optional[str] = None
    sent: int = 0
    received: int = 0
    duplicates: int = 0
    rtts_ms: List[float] = field(default_factory=list)
    errors: Dict[str, int] = field(default_factory=dict)
    method: str = ""
    resolution_ms: Optional[float] = None  # timer granularity (1.0 for Windows)
    error: Optional[str] = None  # fatal: could not measure at all
    raw: str = ""

    @property
    def lost(self) -> int:
        return max(0, self.sent - self.received)

    @property
    def loss_pct(self) -> float:
        return (self.lost / self.sent * 100.0) if self.sent else 100.0

    @property
    def loss_upper_pct(self) -> Optional[float]:
        """95 % upper bound of the true loss rate (Wilson)."""
        return loss_upper_bound_pct(self.lost, self.sent)

    @property
    def rtt(self) -> Optional[RttSummary]:
        return summarize(self.rtts_ms)

    @property
    def any_reply(self) -> bool:
        return self.received > 0

    def add_error(self, message: str) -> None:
        key = message.strip() or "error"
        self.errors[key] = self.errors.get(key, 0) + 1

    def to_metrics(self) -> Dict[str, float]:
        m: Dict[str, float] = {
            "sent": float(self.sent),
            "received": float(self.received),
            "loss_pct": round(self.loss_pct, 3),
        }
        upper = self.loss_upper_pct
        if upper is not None:
            m["loss_upper_pct"] = round(upper, 3)
        if self.duplicates:
            m["duplicates"] = float(self.duplicates)
        r = self.rtt
        if r is not None:
            m.update(
                {
                    "rtt_ms": round(r.avg, 3),
                    "rtt_min_ms": round(r.min, 3),
                    "rtt_median_ms": round(r.median, 3),
                    "rtt_p95_ms": round(r.p95, 3),
                    "rtt_max_ms": round(r.max, 3),
                    "jitter_ms": round(r.jitter, 3),
                }
            )
        return m

    def to_dict(self) -> dict:
        r = self.rtt
        return {
            "target": self.target,
            "address": self.address,
            "sent": self.sent,
            "received": self.received,
            "duplicates": self.duplicates,
            "loss_pct": round(self.loss_pct, 3),
            "loss_upper_pct": (
                round(self.loss_upper_pct, 3)
                if self.loss_upper_pct is not None
                else None
            ),
            "rtt": r.to_dict() if r else None,
            "errors": dict(self.errors),
            "method": self.method,
            "resolution_ms": self.resolution_ms,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# public API


def ping(
    target: str,
    count: int = DEFAULT_COUNT,
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    size: int = DEFAULT_SIZE,
    family: Optional[int] = None,
    method: str = "auto",
) -> PingStats:
    """Ping one target. See :func:`ping_many`."""
    return ping_many(
        [target],
        count=count,
        interval=interval,
        timeout=timeout,
        size=size,
        family=family,
        method=method,
    )[target]


def ping_many(
    targets: Sequence[str],
    count: int = DEFAULT_COUNT,
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    size: int = DEFAULT_SIZE,
    family: Optional[int] = None,
    method: str = "auto",
    max_pps: float = MAX_PPS,
) -> Dict[str, PingStats]:
    """Ping several targets concurrently; returns ``{target: PingStats}``.

    ``method`` is ``auto`` (ICMP socket or the Windows ICMP API when available,
    else the system ``ping``), ``socket``, ``win`` or ``system``. ``family``
    4/6 forces an address family.
    """
    return _run(
        targets,
        count=max(1, int(count)),
        interval=interval,
        timeout=timeout,
        size=size,
        family=family,
        method=method,
        max_pps=max_pps,
    )


SampleFn = Callable[[str, float, Optional[float], Optional[str]], None]


def ping_stream(
    targets: Sequence[str],
    on_sample: SampleFn,
    stop: threading.Event,
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    size: int = DEFAULT_SIZE,
    method: str = "auto",
    max_pps: float = MAX_PPS,
) -> Dict[str, PingStats]:
    """Ping continuously until *stop* is set, reporting every request.

    ``on_sample(target, sent_at, rtt_ms, error)`` is called once per request:
    ``rtt_ms`` is None for a lost request, ``sent_at`` is the wall-clock send
    time (for outage timelines). Memory stays bounded however long it runs;
    the returned stats carry counts only (no per-reply RTT list).
    """
    return _run(
        targets,
        count=None,
        interval=interval,
        timeout=timeout,
        size=size,
        family=None,
        method=method,
        max_pps=max_pps,
        on_sample=on_sample,
        stop=stop,
    )


def _choose(method: str, fam: Optional[int]) -> str:
    method = os.environ.get("NETDIAG_PING_METHOD", "") or method
    if method == "system" or fam == 6:
        return "system"
    if method in ("auto", "socket") and icmp_socket_available():
        return "socket"
    if method in ("auto", "win") and win_icmp_available():
        return "win"
    return "system"


def _run(
    targets: Sequence[str],
    count: Optional[int],
    interval: float,
    timeout: float,
    size: int,
    family: Optional[int],
    method: str,
    max_pps: float,
    on_sample: Optional[SampleFn] = None,
    stop: Optional[threading.Event] = None,
) -> Dict[str, PingStats]:
    out: Dict[str, PingStats] = {}
    jobs: Dict[str, Dict[str, str]] = {"socket": {}, "win": {}}
    system_jobs: List[Tuple[str, Optional[int]]] = []
    backends = {}
    for t in dict.fromkeys(targets):  # de-duplicate, keep order
        fam = family or _literal_family(t)
        backends[t] = _choose(method, fam)
        if backends[t] == "system":
            system_jobs.append((t, fam))
    # Names resolve side by side: with DNS down, each would otherwise add its
    # full timeout before any echo is sent.
    addrs = resolve_all(
        [t for t, b in backends.items() if b != "system"], timeout=max(timeout, 2.0)
    )
    for t, backend in backends.items():
        if backend == "system":
            continue
        addr = addrs[t]
        if addr is None:
            out[t] = PingStats(target=t, method=backend, error="name resolution failed")
            continue
        jobs[backend][t] = addr

    common = dict(
        interval=interval,
        timeout=timeout,
        size=size,
        max_pps=max_pps,
        count=count,
        on_sample=on_sample,
        stop=stop,
    )
    runners = []
    if jobs["socket"]:
        runners.append(_SocketPinger(jobs["socket"], **common).run)
    if jobs["win"]:
        from .winicmp import WinIcmpPinger

        runners.append(WinIcmpPinger(jobs["win"], **common).run)
    if system_jobs:
        runners.append(
            lambda: _system_many(
                system_jobs, count, interval, timeout, size, on_sample, stop
            )
        )
    if len(runners) == 1:
        out.update(runners[0]())
    elif runners:
        with ThreadPoolExecutor(max_workers=len(runners)) as pool:
            for res in pool.map(lambda r: r(), runners):
                out.update(res)
    return {t: out[t] for t in dict.fromkeys(targets)}


_socket_ok: Optional[bool] = None
_socket_lock = threading.Lock()


def icmp_socket_available() -> bool:
    """Can this process open an unprivileged ICMP datagram socket? (cached)"""
    global _socket_ok
    if _socket_ok is None:
        with _socket_lock:
            if _socket_ok is None:
                if is_windows():  # needs a raw socket (admin) on Windows
                    _socket_ok = False
                else:
                    try:
                        s = socket.socket(
                            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP
                        )
                        s.close()
                        _socket_ok = True
                    except (OSError, AttributeError):
                        _socket_ok = False
    return _socket_ok


def win_icmp_available() -> bool:
    """Is the Windows ICMP API (IcmpSendEcho, no admin needed) usable?"""
    if not is_windows():
        return False
    from .winicmp import get_api

    return get_api() is not None


def precise_ping_available() -> bool:
    """Sub-millisecond timing and free send intervals on this host (IPv4)?"""
    return _choose("auto", None) != "system"


def system_ping_cadence() -> float:
    """Shortest gap the OS ``ping`` keeps: ping.exe ~1 s, iputils 0.2 s unprivileged."""
    return 1.0 if is_windows() else 0.2


# --------------------------------------------------------------------------
# socket back-end


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def build_echo_request(ident: int, seq: int, payload: bytes) -> bytes:
    header = struct.pack("!BBHHH", _ECHO_REQUEST, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    csum = _checksum(header + payload)
    return (
        struct.pack("!BBHHH", _ECHO_REQUEST, 0, csum, ident & 0xFFFF, seq & 0xFFFF)
        + payload
    )


def parse_echo_reply(data: bytes) -> Optional[Tuple[int, int, bytes]]:
    """Return ``(ident, seq, payload)`` for an echo reply, else None.

    Linux datagram ICMP sockets deliver the bare ICMP message; macOS prepends
    the IPv4 header, which is detected by its version nibble and stripped.
    """
    if len(data) >= 20 and data[0] >> 4 == 4:
        data = data[(data[0] & 0x0F) * 4 :]
    if len(data) < 8:
        return None
    icmp_type, _code, _csum, ident, seq = struct.unpack("!BBHHH", data[:8])
    if icmp_type != _ECHO_REPLY:
        return None
    return ident, seq, data[8:]


class _SocketPinger:
    """fping-style: one socket, one thread, many targets, paced sends.

    Request ``k`` goes to address ``k % n`` at ``t0 + k * step``, so every
    address is pinged once per period, evenly spread and never faster than
    ``max_pps`` overall. With ``count=None`` it runs until ``stop`` is set,
    keeping only a short window of per-request state.
    """

    def __init__(
        self,
        jobs: Dict[str, str],
        interval: float,
        timeout: float,
        size: int,
        max_pps: float,
        count: Optional[int] = None,
        on_sample: Optional[SampleFn] = None,
        stop: Optional[threading.Event] = None,
    ) -> None:
        self.jobs = jobs
        self.count = count
        self.interval = max(0.001, interval)
        self.timeout = max(0.05, timeout)
        self.size = max(len(_MAGIC) + 8, size)
        self.min_gap = 1.0 / max_pps if max_pps > 0 else 0.0
        self.on_sample = on_sample
        self.stop = stop or threading.Event()
        self.keep_rtts = count is not None  # streaming keeps counts only
        self.token = os.urandom(4)
        self.ident = os.getpid() & 0xFFFF

    def _payload(self, k: int) -> bytes:
        body = _MAGIC + self.token + struct.pack("!I", k & 0xFFFFFFFF)
        return body + b"\x00" * max(0, self.size - len(body))

    def run(self) -> Dict[str, PingStats]:
        stats = {
            t: PingStats(target=t, address=a, method="icmp-socket", resolution_ms=0.001)
            for t, a in self.jobs.items()
        }
        by_addr: Dict[str, List[str]] = {}
        for t, a in self.jobs.items():
            by_addr.setdefault(a, []).append(t)
        self.addrs = addrs = list(by_addr)
        self.by_addr = by_addr
        self.stats = stats
        n = len(addrs)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except OSError as e:
            for s in stats.values():
                s.error = f"icmp socket unavailable: {e}"
            return stats
        sock.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ)

        period = max(self.interval, n * self.min_gap)
        step = period / n
        total = None if self.count is None else self.count * n
        # k -> (perf send time, wall send time, send error)
        self.outstanding: Dict[int, Tuple[float, float, Optional[str]]] = {}
        self.answered: Dict[int, float] = {}  # recently answered k -> perf time
        self.rtts: Dict[int, Dict[int, float]] = {j: {} for j in range(n)}
        self.late = [0] * n
        self.next_k = 0
        t0 = time.perf_counter()
        try:
            while True:
                now = time.perf_counter()
                stopping = self.stop.is_set()
                while (
                    not stopping
                    and (total is None or self.next_k < total)
                    and t0 + self.next_k * step <= now
                ):
                    self._send(sock, self.next_k, n)
                    self.next_k += 1
                    now = time.perf_counter()
                self._expire(now, n)
                done_sending = stopping or (total is not None and self.next_k >= total)
                if done_sending and not self.outstanding:
                    break
                if not done_sending:
                    wait = max(0.0, t0 + self.next_k * step - now)
                else:
                    oldest = min(v[0] for v in self.outstanding.values())
                    wait = max(0.0, oldest + self.timeout - now)
                for _key, _ev in sel.select(timeout=min(wait, 0.05)):
                    self._drain(sock, n)
        finally:
            sel.close()
            sock.close()

        for j, addr in enumerate(addrs):
            for t in by_addr[addr]:
                s = stats[t]
                if self.keep_rtts:
                    s.rtts_ms = [self.rtts[j][r] for r in sorted(self.rtts[j])]
                if self.late[j]:
                    s.errors["late reply (after timeout)"] = self.late[j]
        return stats

    def _emit(self, j: int, wall: float, rtt: Optional[float], err: Optional[str]):
        for t in self.by_addr[self.addrs[j]]:
            s = self.stats[t]
            if rtt is not None:
                s.received += 1
            elif err:
                s.add_error(err)
            if self.on_sample is not None:
                self.on_sample(t, wall, rtt, err)

    def _send(self, sock, k: int, n: int) -> None:
        j = k % n
        pkt = build_echo_request(self.ident, k, self._payload(k))
        err = None
        try:
            sock.sendto(pkt, (self.addrs[j], 0))
        except OSError as e:
            # e.g. "No route to host" after ARP failed: the packet never left,
            # so it counts as sent-and-lost, with the reason kept.
            err = e.strerror or str(e)
        self.outstanding[k] = (time.perf_counter(), time.time(), err)
        for t in self.by_addr[self.addrs[j]]:
            self.stats[t].sent += 1

    def _expire(self, now: float, n: int) -> None:
        for k, (sent_p, sent_w, err) in list(self.outstanding.items()):
            if now - sent_p > self.timeout:
                del self.outstanding[k]
                self._emit(k % n, sent_w, None, err)
        horizon = now - 2 * self.timeout
        for k, t in list(self.answered.items()):
            if t < horizon:
                del self.answered[k]

    def _drain(self, sock, n: int) -> None:
        while True:
            try:
                data, src = sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            t_recv = time.perf_counter()
            parsed = parse_echo_reply(data)
            if parsed is None:
                continue
            _ident, seq, payload = parsed
            if not payload.startswith(_MAGIC + self.token):
                continue  # someone else's echo reply
            try:
                (k,) = struct.unpack("!I", payload[8:12])
            except struct.error:
                continue
            if k & 0xFFFF != seq or k >= self.next_k:
                continue
            j = k % n
            if src[0] != self.addrs[j]:
                continue
            if k in self.answered:
                # A second answer to the same request: IP conflict or a loop.
                for t in self.by_addr[self.addrs[j]]:
                    self.stats[t].duplicates += 1
                continue
            entry = self.outstanding.pop(k, None)
            if entry is None:
                self.late[j] += 1  # answered after we had given up on it
                continue
            sent_p, sent_w, _err = entry
            self.answered[k] = t_recv
            rtt = (t_recv - sent_p) * 1000.0
            if self.keep_rtts:
                self.rtts[j][k // n] = rtt
            self._emit(j, sent_w, rtt, None)


def _system_many(
    jobs: List[Tuple[str, Optional[int]]],
    count: Optional[int],
    interval: float,
    timeout: float,
    size: int,
    on_sample: Optional[SampleFn],
    stop: Optional[threading.Event],
) -> Dict[str, PingStats]:
    """System ``ping`` per target in parallel; in streaming mode, in rounds."""
    workers = max(1, min(32, len(jobs)))
    if count is not None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                t: pool.submit(
                    system_ping,
                    t,
                    count=count,
                    interval=interval,
                    timeout=timeout,
                    size=size,
                    family=fam,
                )
                for t, fam in jobs
            }
            return {t: f.result() for t, f in futs.items()}

    totals = {
        t: PingStats(target=t, method="system-ping", resolution_ms=1.0) for t, _ in jobs
    }
    cadence = max(interval, system_ping_cadence())
    per_round = max(5, int(10 / cadence))  # ~10 s of samples per ping run

    def one_round(t: str, fam: Optional[int]) -> None:
        start = time.time()
        st = system_ping(
            t, count=per_round, interval=interval, timeout=timeout, family=fam
        )
        tot = totals[t]
        if st.error:
            tot.error = st.error
            return
        tot.sent += st.sent
        tot.received += st.received
        tot.duplicates += st.duplicates
        for msg, c in st.errors.items():
            tot.errors[msg] = tot.errors.get(msg, 0) + c
        if on_sample is not None:
            for i, rtt in enumerate(per_request_results(st, per_round)):
                on_sample(t, start + i * cadence, rtt, None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while stop is None or not stop.is_set():
            list(pool.map(lambda job: one_round(*job), jobs))
            if all(totals[t].error for t, _ in jobs):
                break
    return totals


_SEQ_RE = re.compile(r"(?:icmp_seq|seq)=(\d+)")


def per_request_results(st: PingStats, count: int) -> List[Optional[float]]:
    """Per-request RTT (None = lost) recovered from system ping output.

    POSIX output numbers each reply (``icmp_seq=N``); Windows prints exactly
    one line per request, in order. Used to place losses on a timeline.
    """
    text = st.raw or ""
    if is_windows():
        blocks = _paragraphs(text)
        region = blocks[0][1:] if blocks else []
        out = [_win_reply_rtt(line) for line in region[:count]]
        return out + [None] * (count - len(out))
    by_seq: Dict[int, float] = {}
    for line in text.splitlines():
        m_seq, rtt = _SEQ_RE.search(line), _posix_reply_rtt(line)
        if m_seq and rtt is not None:
            by_seq.setdefault(int(m_seq.group(1)), rtt)
    base = 0 if 0 in by_seq else 1
    return [by_seq.get(base + i) for i in range(count)]


# --------------------------------------------------------------------------
# system ping back-end


def build_ping_args(
    host: str,
    count: int,
    timeout: float,
    interval: float,
    size: Optional[int] = None,
    family: Optional[int] = None,
    os_name: Optional[str] = None,
) -> List[str]:
    """Argument vector for the platform ``ping`` command."""
    os_name = os_name or (
        "windows" if is_windows() else "macos" if is_macos() else "linux"
    )
    fam = ["-4"] if family == 4 else (["-6"] if family == 6 else [])
    if os_name == "windows":
        args = ["ping", *fam, "-n", str(count), "-w", str(int(timeout * 1000))]
        if size:
            args += ["-l", str(size)]
        return [*args, host]
    if os_name == "macos":
        # -W is milliseconds on macOS; -i below 0.1s needs root.
        args = ["ping", "-n", "-c", str(count), "-i", _fmt_interval(max(interval, 0.1))]
        args += ["-W", str(int(timeout * 1000))]
        if size:
            args += ["-s", str(size)]
        if family == 6:
            args[0] = "ping6"
        return [*args, host]
    # Linux iputils: -W is seconds; non-root interval floor is 0.2s on older builds.
    args = [
        "ping",
        *fam,
        "-n",
        "-c",
        str(count),
        "-i",
        _fmt_interval(max(interval, 0.2)),
    ]
    args += ["-W", str(max(1, int(math.ceil(timeout))))]
    if size:
        args += ["-s", str(size)]
    return [*args, host]


def _fmt_interval(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def system_ping(
    host: str,
    count: int = DEFAULT_COUNT,
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    size: Optional[int] = None,
    family: Optional[int] = None,
) -> PingStats:
    """Ping via the OS ``ping`` command and parse the output."""
    windows = is_windows()
    stats = PingStats(
        target=host,
        address=host if _literal_family(host) else None,
        method="system-ping",
        resolution_ms=1.0 if windows else 0.001,
    )
    if not have("ping"):
        stats.error = "ping command not available"
        return stats
    budget = count * (max(interval, system_ping_cadence()) + timeout) + 5
    res = run(
        build_ping_args(host, count, timeout, interval, size=size, family=family),
        timeout=budget,
    )
    text = res.stdout or res.stderr
    if not windows and _looks_like_usage_error(text):
        # BusyBox or an exotic ping rejected an option: retry with the minimum.
        res = run(["ping", "-c", str(count), host], timeout=budget)
        text = res.stdout or res.stderr
    stats.raw = text
    parsed = (
        parse_windows_ping(text, count) if windows else parse_posix_ping(text, count)
    )
    stats.sent, stats.received, stats.duplicates, stats.rtts_ms, errs = parsed
    for msg, c in errs.items():
        stats.errors[msg] = stats.errors.get(msg, 0) + c
    if stats.sent == 0 and not stats.received:
        stats.sent = count
    if not stats.received and not _measured(text, windows):
        # ping never got to send (permission denied, unknown host, bad option):
        # that is "could not measure", not "the device did not answer".
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        stats.error = first[:200] or "ping produced no output"
    return stats


def _measured(text: str, windows: bool) -> bool:
    """Did ping actually send requests (as opposed to failing up front)?"""
    if windows:
        blocks = _paragraphs(text)
        return bool(blocks) and len(blocks[0]) > 1
    return _POSIX_SUMMARY_RE.search(text) is not None or "bytes from" in text


def _looks_like_usage_error(text: str) -> bool:
    low = text.lower()
    return (
        any(k in low for k in ("invalid option", "unrecognized option", "usage: ping"))
        and "bytes from" not in low
    )


_POSIX_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_POSIX_SUMMARY_RE = re.compile(
    r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received"
    r"(?:,\s+\+(\d+)\s+duplicates)?",
    re.IGNORECASE,
)
_POSIX_ERROR_RE = re.compile(r"^From\s+\S+?:?\s+(?:icmp_seq=\d+\s+)?(.+?)\s*$")


def _posix_reply_rtt(line: str) -> Optional[float]:
    """RTT of a ``bytes from ... time=N ms`` reply line (duplicates excluded)."""
    if "bytes from" not in line or "(DUP!)" in line:
        return None
    m = _POSIX_TIME_RE.search(line)
    return float(m.group(1)) if m else None


def parse_posix_ping(
    text: str, count: int
) -> Tuple[int, int, int, List[float], Dict[str, int]]:
    """Parse iputils / BusyBox / macOS ping output (C locale).

    Returns ``(sent, received, duplicates, rtts_ms, errors)``; RTTs exclude
    duplicate replies so statistics describe one answer per request.
    """
    rtts: List[float] = []
    dup_lines = 0
    errors: Dict[str, int] = {}

    def _err(msg: str) -> None:
        errors[msg] = errors.get(msg, 0) + 1

    for line in text.splitlines():
        if "bytes from" in line:
            rtt = _posix_reply_rtt(line)
            if "(DUP!)" in line:
                dup_lines += 1
            elif rtt is not None:
                rtts.append(rtt)
            else:  # e.g. macOS "92 bytes from x: Destination Host Unreachable"
                _err(line.split(":", 1)[-1].strip())
            continue
        stripped = line.strip()
        m = _POSIX_ERROR_RE.match(stripped)
        if m:
            _err(m.group(1))
        elif stripped.startswith("ping: "):
            _err(stripped[len("ping: ") :])
    sent, received, dups = count, len(rtts), dup_lines
    m = _POSIX_SUMMARY_RE.search(text)
    if m:
        sent = int(m.group(1))
        received = int(m.group(2))
        if m.group(3):
            dups = int(m.group(3))
    return sent, received, dups, rtts, errors


_WIN_RTT_RE = re.compile(r"[=<]\s*(\d+)\s*ms", re.IGNORECASE)
_WIN_LIST_SEP = (",", "、", "，")
_WIN_SENT_RE = re.compile(r"(?:Sent|送信)\s*=\s*(\d+)", re.IGNORECASE)


def _paragraphs(text: str) -> List[List[str]]:
    """Blank-line separated blocks of stripped lines."""
    blocks: List[List[str]] = []
    cur: List[str] = []
    for line in text.splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def _win_reply_rtt(line: str) -> Optional[float]:
    """RTT of a ping.exe reply line in any language, None for anything else.

    A reply carries exactly one ``=N ms``/``<N ms`` token and no list separator
    (the statistics lines are comma/読点 separated). ``<1ms`` becomes 0.5 ms.
    """
    tokens = _WIN_RTT_RE.findall(line)
    if len(tokens) != 1 or any(sep in line for sep in _WIN_LIST_SEP):
        return None
    m = _WIN_RTT_RE.search(line)
    value = float(m.group(1))
    if "<" in m.group(0):
        value = max(0.0, value - 0.5)
    return value


def parse_windows_ping(
    text: str, count: int
) -> Tuple[int, int, int, List[float], Dict[str, int]]:
    """Parse Windows ``ping.exe`` output in any display language.

    The per-request lines sit in the first paragraph after the "Pinging ..."
    header. A line is a successful reply iff it carries exactly one
    ``=N ms``/``<N ms`` token and no list separator (the statistics lines are
    comma/読点 separated). Everything else in that paragraph — timeouts,
    "destination host unreachable", "TTL expired" — is a lost request, whatever
    language it is written in. ``<1ms`` is recorded as 0.5 ms (1 ms resolution).
    """
    blocks = _paragraphs(text)
    region: List[str] = blocks[0][1:] if blocks else []
    rtts: List[float] = []
    errors: Dict[str, int] = {}
    if blocks and len(blocks[0]) == 1 and len(blocks) == 1:
        # A lone line is a pre-flight error (unknown host, bad option ...).
        errors[blocks[0][0]] = 1
        return count, 0, 0, rtts, errors
    for line in region:
        rtt = _win_reply_rtt(line)
        if rtt is not None:
            rtts.append(rtt)
        else:
            errors[line] = errors.get(line, 0) + 1

    sent = len(region) or count
    m = _WIN_SENT_RE.search(text)
    if m:
        sent = int(m.group(1))
    return sent, len(rtts), 0, rtts, errors


# --------------------------------------------------------------------------
# helpers


def _literal_family(host: str) -> Optional[int]:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).version
    except ValueError:
        return None


def _resolve_v4(host: str, timeout: float = 2.0) -> Optional[str]:
    """IPv4 address for *host* (literal or name), bounded by *timeout*."""
    if _literal_family(host) == 4:
        return host
    result: List[Optional[str]] = [None]

    def _lookup() -> None:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            result[0] = infos[0][4][0] if infos else None
        except (OSError, UnicodeError):
            result[0] = None

    worker = threading.Thread(target=_lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0]


def resolve_all(hosts: Iterable[str], timeout: float = 2.0) -> Dict[str, Optional[str]]:
    """Resolve many names concurrently (each bounded by *timeout*)."""
    hosts = list(dict.fromkeys(hosts))
    if not hosts:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
        return dict(zip(hosts, pool.map(lambda h: _resolve_v4(h, timeout), hosts)))


def addresses_for(
    hosts: Iterable[str], timeout: float = 2.0
) -> Dict[str, Optional[str]]:
    hosts = list(hosts)
    resolved = resolve_all([h for h in hosts if _literal_family(h) is None], timeout)
    return {h: h if _literal_family(h) else resolved[h] for h in hosts}
