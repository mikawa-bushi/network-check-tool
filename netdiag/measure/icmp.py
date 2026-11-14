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

import functools
import ipaddress
import math
import os
import queue
import re
import selectors
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from ..core.util import (
    _command_env,
    _no_window_kwargs,
    decode_output,
    have,
    is_macos,
    is_windows,
    run,
)
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
    stop: Optional[threading.Event] = None,
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
        stop=stop,
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
    stop: Optional[threading.Event] = None,
) -> Dict[str, PingStats]:
    """Ping several targets concurrently; returns ``{target: PingStats}``.

    ``method`` is ``auto`` (ICMP socket or the Windows ICMP API when available,
    else the system ``ping``), ``socket``, ``win`` or ``system``. ``family``
    4/6 forces an address family.

    Ctrl-C in the calling thread stops every pinger within about a second.
    A caller that pings from a worker thread passes *stop* and sets it.
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
        stop=stop,
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

    ``on_sample(target, sent_at, rtt_ms, error)`` is called once for every
    request due before *stop* was set, and never for one due after it.
    ``sent_at`` is the wall-clock send time (for outage timelines) and
    ``rtt_ms`` is None for a lost request. ``error`` says why a request was
    lost when ping knows, for example ``connect: Network is unreachable``:
    a request that could not be sent still counts as lost. A sample arrives
    at most ``max(timeout, ping pace) + 0.3`` seconds after ``sent_at``.
    While netdiag itself is frozen (Ctrl-Z, a VM pause) nothing falls due,
    so the pause leaves a gap in the samples, not a burst or a loss.

    It returns once *stop* is set, or earlier only when no target can be
    pinged at all. A target that stopped being measured carries the reason
    in ``error`` of its returned :class:`PingStats`. Memory stays bounded
    however long it runs; the stats carry counts only (no RTT list).
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
    stop = stop or threading.Event()
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
    for res in run_together(runners, stop):
        out.update(res)
    return {t: out[t] for t in dict.fromkeys(targets)}


_WIND_DOWN = 2.0  # seconds Ctrl-C waits for pingers to stop and reap their ping


def run_together(
    fns: Sequence[Callable[[], Any]], stop: threading.Event, limit: int = 0
) -> List[Any]:
    """Run *fns* side by side, at most *limit* at once (0: all), in order.

    The caller waits in short slices. An untimed wait would hold Ctrl-C until
    every ping had finished, and on Windows it cannot be interrupted at all.
    On Ctrl-C it sets *stop*, which every pinger watches, gives them a moment
    to wind down and re-raises. A failure inside a function is re-raised here.
    """
    results: List[Any] = [None] * len(fns)
    failures: List[BaseException] = []
    gate = threading.Semaphore(limit or max(1, len(fns)))
    # Events, not Thread.join: a join cut short by Ctrl-C can later report a
    # live thread as finished.
    finished = [threading.Event() for _ in fns]

    def work(i: int) -> None:
        try:
            with gate:
                results[i] = fns[i]()
        except BaseException as e:
            failures.append(e)
        finally:
            finished[i].set()

    for i in range(len(fns)):
        threading.Thread(target=work, args=(i,), daemon=True).start()
    try:
        for done in finished:
            while not done.wait(0.1):
                pass
    except BaseException:
        stop.set()
        end = time.monotonic() + _WIND_DOWN
        for done in finished:
            done.wait(max(0.0, end - time.monotonic()))
        raise
    if failures:
        raise failures[0]
    return results


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
        last_send = t0 - self.min_gap
        try:
            while True:
                now = time.perf_counter()
                behind = now - (t0 + self.next_k * step)
                if behind > period:
                    # netdiag itself was frozen (Ctrl-Z, a VM pause): resume the
                    # pace instead of sending every missed request at once.
                    t0 += behind
                stopping = self.stop.is_set()
                next_at = max(t0 + self.next_k * step, last_send + self.min_gap)
                while (
                    not stopping
                    and (total is None or self.next_k < total)
                    and next_at <= now
                ):
                    self._send(sock, self.next_k, n)
                    self.next_k += 1
                    last_send = now
                    now = time.perf_counter()
                    next_at = max(t0 + self.next_k * step, last_send + self.min_gap)
                self._expire(now, n)
                done_sending = stopping or (total is not None and self.next_k >= total)
                if done_sending and not self.outstanding:
                    break
                if not done_sending:
                    wait = max(0.0, next_at - now)
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
    stop: threading.Event,
) -> Dict[str, PingStats]:
    """System ``ping`` per target in parallel, or one stream per target."""
    if count is None:
        fns = [
            functools.partial(
                _stream_system, t, fam, interval, timeout, size, on_sample, stop
            )
            for t, fam in jobs
        ]
    else:

        def one(t: str, fam: Optional[int]) -> PingStats:
            if stop.is_set():
                return PingStats(target=t, method="system-ping", error="stopped")
            return system_ping(
                t,
                count=count,
                interval=interval,
                timeout=timeout,
                size=size,
                family=fam,
            )

        fns = [functools.partial(one, t, fam) for t, fam in jobs]
    limit = 32 if count is not None else 0
    return dict(zip((t for t, _ in jobs), run_together(fns, stop, limit)))


_GRACE = 0.25  # slack on a reply deadline: process start-up and pacing drift
_STALL = 1.0  # a gap this long in the read loop means netdiag itself was frozen
_RUN_SECONDS = 10.0  # length of one streaming ping run
_SETTLE = 0.2  # Ctrl-C reaches ping too and can end it just before stop is set


def _stream_system(
    target: str,
    family: Optional[int],
    interval: float,
    timeout: float,
    size: int,
    on_sample: Optional[SampleFn],
    stop: threading.Event,
) -> PingStats:
    """Stream one target through back-to-back runs of the OS ``ping``.

    A run that fails before sending anything, such as ``connect: Network is
    unreachable`` once this PC lost its route, still reports each request
    that fell due as lost, with that reason, and ping is tried again. Only a
    failure meaning netdiag cannot ping this target at all (no ``ping``, no
    permission, options rejected) on a target never measured ends the
    stream, with the reason in ``error``.
    """
    total = PingStats(
        target=target,
        address=target if _literal_family(target) else None,
        method="system-ping",
        resolution_ms=1.0 if is_windows() else 0.001,
    )
    n = max(5, int(_RUN_SECONDS / _cadence(interval, family, _ping_os())))
    commands = _ping_commands(target, n, interval, timeout, size, family)
    which, measured = 0, False
    while not stop.is_set():
        cmd = commands[which]
        run = _StreamRun(target, n, cmd.cadence, timeout, total, on_sample, stop)
        run.execute(cmd.args, cmd.budget(n, timeout))
        if _looks_like_usage_error(run.text) and which + 1 < len(commands):
            which += 1
            continue
        if run.cut_short and stop.wait(_SETTLE):
            break  # Ctrl-C ended ping just before it reached us
        if run.measured:
            measured = True
            continue
        reason = _network_failure(run.text)
        if reason is None:
            reason = _first_line(run.text) or "ping produced no output"
            if not measured:
                total.error = reason
                break
        if not run.lose_unsent(reason, max(timeout, cmd.cadence)):
            break
    return total


class _StreamRun:
    """One run of the OS ``ping`` in streaming mode, read as it prints.

    Request ``i`` is due at ``anchor + i * cadence`` (perf time); each reply
    re-anchors that estimate from its own round trip. A reply goes to
    ``on_sample`` as soon as ping prints it. On POSIX a request still
    unanswered ``timeout`` after it was due is reported lost, as on the
    socket back-end; ping.exe prints one line per request, which settles it.
    Requests due after *stop* was set are never reported.
    """

    def __init__(
        self,
        target: str,
        n: int,
        cadence: float,
        timeout: float,
        total: PingStats,
        on_sample: Optional[SampleFn],
        stop: threading.Event,
    ) -> None:
        self.target, self.n, self.cadence, self.timeout = target, n, cadence, timeout
        self.total, self.on_sample, self.stop = total, on_sample, stop
        self.windows = is_windows()
        self.lines: List[str] = []
        self.settled: set[int] = set()
        self.base: Optional[int] = None  # POSIX icmp_seq of request 0
        self.win_index: Optional[int] = None  # next ping.exe request line
        self.reason: Optional[str] = None  # latest error ping printed
        self.stopped_at: Optional[float] = None
        self.ran_out = False  # ping exited on its own
        self.timed_out = False
        self.measured = False
        self.cut_short = True
        self.started = self.anchor = time.perf_counter()
        self.wall_offset = time.time() - self.started

    @property
    def text(self) -> str:
        return "\n".join(self.lines) or ("timed out" if self.timed_out else "")

    def execute(self, args: List[str], budget: float) -> None:
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_command_env(),
                **_no_window_kwargs(),
            )
        except FileNotFoundError:
            self.lines.append(f"{args[0]} command not available")
            return self._finish()
        except OSError as e:
            self.lines.append(e.strerror or str(e))
            return self._finish()
        lines: "queue.Queue[Tuple[float, Optional[str]]]" = queue.Queue()
        reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
        reader.start()
        last = self.started
        try:
            while True:
                try:
                    item: Optional[Tuple[float, Optional[str]]] = lines.get(
                        timeout=0.05
                    )
                except queue.Empty:
                    item = None
                now = time.perf_counter()
                if now - last > _STALL:
                    self.anchor += now - last  # frozen with its ping (Ctrl-Z)
                last = now
                if item is not None:
                    at, line = item
                    if line is None:
                        self.ran_out = True
                        break
                    self._line(at, line)
                if self._should_end(now, budget):
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join(1.0)
            proc.stdout.close()
        self._finish()

    def _should_end(self, now: float, budget: float) -> bool:
        if not self.windows:
            self._expire(now)
        if self.stopped_at is None and self.stop.is_set():
            self.stopped_at = now
        if now - self.started > budget:
            self.timed_out = True
            return True
        if self.stopped_at is None:
            return False
        wind_down = self.stopped_at + self.timeout + self.cadence + _GRACE
        pending = (
            i
            for i in range(self.n)
            if i not in self.settled and self._due(i) <= self.stopped_at
        )
        return now > wind_down or next(pending, None) is None

    def _due(self, i: int) -> float:
        return self.anchor + i * self.cadence

    def _line(self, at: float, line: str) -> None:
        self.lines.append(line)
        if self.windows:
            self._windows_line(at, line.strip())
            return
        if self.base is None and line.startswith("PING"):
            self.base = _seq_base(line)
            return
        m = _SEQ_RE.search(line)
        i = int(m.group(1)) - (1 if self.base is None else self.base) if m else None
        if "bytes from" in line:
            rtt = _posix_reply_rtt(line)
            if "(DUP!)" in line:
                self.total.duplicates += 1
            elif rtt is None:  # macOS "92 bytes from x: Destination Host Unreachable"
                self.reason = line.split(":", 1)[-1].strip()
            elif i is not None:
                self._reply(i, at, rtt)
            return
        err = _POSIX_ERROR_RE.match(line.strip())
        if err:  # "From 192.0.2.1 icmp_seq=3 Destination Host Unreachable"
            self.reason = err.group(1)
            if i is not None:
                self._settle(i, self._due(i), None, self.reason)
        elif line.startswith("ping: "):
            self.reason = line[len("ping: ") :].strip()

    def _windows_line(self, at: float, line: str) -> None:
        if self.win_index is None:
            if line:  # the "Pinging x with 32 bytes of data:" header
                self.win_index = 0
            return
        if not line or self.win_index >= self.n:
            self.win_index = self.n  # past the request lines: statistics follow
            return
        i = self.win_index
        self.win_index += 1
        rtt = _win_reply_rtt(line)
        if rtt is None:
            self._settle(i, min(self._due(i), at), None, line)
        else:
            self._reply(i, at, rtt)

    def _reply(self, i: int, at: float, rtt: float) -> None:
        sent = at - rtt / 1000.0
        self.reason = None
        if 0 <= i < self.n:
            self.anchor = sent - i * self.cadence
        if i in self.settled or rtt > self.timeout * 1000.0:
            self.total.add_error("late reply (after timeout)")
            self._settle(i, sent, None, None)
        else:
            self._settle(i, sent, rtt, None)

    def _expire(self, now: float) -> None:
        for i in range(self.n):
            if i in self.settled:
                continue
            due = self._due(i)
            if due + self.timeout + _GRACE > now:
                return
            if self.stopped_at is not None and due > self.stopped_at:
                return
            self._settle(i, due, None, self.reason)

    def _settle(
        self, i: int, sent: float, rtt: Optional[float], error: Optional[str]
    ) -> None:
        if i in self.settled or not 0 <= i < self.n:
            return
        if self.stopped_at is not None and sent > self.stopped_at:
            return
        self.settled.add(i)
        self.total.sent += 1
        if rtt is not None:
            self.total.received += 1
        elif error:
            self.total.add_error(error)
        if self.on_sample is not None:
            self.on_sample(self.target, self.wall_offset + sent, rtt, error)

    def _finish(self) -> None:
        """Settle what ping's exit decided; keep what it cut short unreported."""
        end = time.perf_counter()
        text = "\n".join(self.lines)
        self.measured = _measured(text, self.windows)
        m = (_WIN_SENT_RE if self.windows else _POSIX_SUMMARY_RE).search(text)
        sent = int(m.group(1)) if m else None
        complete = self.ran_out and sent is not None and sent >= self.n
        self.cut_short = not complete
        for i in range(self.n):
            due = self._due(i)
            if (sent is not None and i >= sent) or (sent is None and due > end):
                return  # never sent
            if self.stopped_at is not None and due > self.stopped_at:
                return
            if complete or due + self.timeout <= end:
                self._settle(i, due, None, self.reason)

    def lose_unsent(self, reason: str, span: float) -> bool:
        """Report each request due within *span* of this run's start as lost,
        as it falls due, then wait out the span. False once *stop* is set."""
        m = max(1, math.ceil(span / self.cadence - 1e-9))
        for i in range(m):
            due = self.started + i * self.cadence
            if self.stop.wait(max(0.0, due - time.perf_counter())):
                return False
            self._settle(i, due, None, reason)
        end = self.started + m * self.cadence
        return not self.stop.wait(max(0.0, end - time.perf_counter()))


def _pump(stream, lines: "queue.Queue[Tuple[float, Optional[str]]]") -> None:
    """Queue each line ping prints with its arrival time, then None at the end."""
    for raw in iter(stream.readline, b""):
        lines.put((time.perf_counter(), decode_output(raw).rstrip("\r\n")))
    lines.put((time.perf_counter(), None))


_SEQ_RE = re.compile(r"(?:icmp_seq|seq)=(\d+)")
_IPUTILS_HEADER_RE = re.compile(r"PING \S+ ?\([^)]*\) \d")


def _seq_base(text: str) -> int:
    """``icmp_seq`` of a run's first request, read from ping's header line.

    iputils counts from 1 (``PING h (a) 56(84) bytes of data.``); macOS, BSD,
    BusyBox and GNU inetutils count from 0 (``PING h (a): 56 data bytes``).
    """
    header = next((ln for ln in text.splitlines() if ln.startswith("PING")), "")
    return 0 if header and not _IPUTILS_HEADER_RE.match(header) else 1


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
    base = _seq_base(text)
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
    os_name = os_name or _ping_os()
    pace = _fmt_interval(_cadence(interval, family, os_name))
    fam = ["-4"] if family == 4 else (["-6"] if family == 6 else [])
    if os_name == "windows":
        args = ["ping", *fam, "-n", str(count), "-w", str(int(timeout * 1000))]
        if size:
            args += ["-l", str(size)]
        return [*args, host]
    if os_name == "macos" and family == 6:
        # KAME ping6: -W takes no value, and -i below 1 s needs root.
        args = ["ping6", "-n", "-c", str(count), "-i", pace]
    elif os_name == "macos":
        # -W is milliseconds on macOS; -i below 0.1s needs root.
        args = ["ping", "-n", "-c", str(count), "-i", pace]
        args += ["-W", str(int(timeout * 1000))]
    else:
        # Linux iputils: -W is seconds; non-root interval floor is 0.2s on older builds.
        args = ["ping", *fam, "-n", "-c", str(count), "-i", pace]
        args += ["-W", _wait_seconds(timeout)]
    if size:
        args += ["-s", str(size)]
    return [*args, host]


def _ping_os() -> str:
    return "windows" if is_windows() else "macos" if is_macos() else "linux"


def _cadence(interval: float, family: Optional[int], os_name: str) -> float:
    """Seconds between the requests of one ``ping`` run."""
    if os_name == "windows":
        return 1.0  # ping.exe has no interval option
    if os_name == "macos":
        return max(interval, 1.0 if family == 6 else 0.1)
    return max(interval, 0.2)


def _wait_seconds(timeout: float) -> str:
    return str(max(1, int(math.ceil(timeout))))


def _fmt_interval(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


class _PingCommand(NamedTuple):
    """One way to run the OS ``ping``, with the pace it keeps."""

    args: List[str]
    cadence: float  # seconds between requests
    linger: float = 0.0  # seconds ping may wait after its last request

    def budget(self, count: int, timeout: float) -> float:
        return count * (self.cadence + timeout) + self.linger + 5


def _ping_commands(
    host: str,
    count: int,
    interval: float,
    timeout: float,
    size: Optional[int] = None,
    family: Optional[int] = None,
) -> List[_PingCommand]:
    """The ``ping`` invocations to try in turn while one is a usage error.

    Old iputils and GNU inetutils reject ``-4``/``-6``, BusyBox rejects ``-n``.
    The second form drops those flags but keeps the pace and the reply wait.
    Plain ``ping -c N`` comes last: its 1 s pace and its linger of up to 10 s
    without replies set its time limit.
    """
    os_name = _ping_os()
    full = _PingCommand(
        build_ping_args(host, count, timeout, interval, size, family, os_name),
        _cadence(interval, family, os_name),
        10.0 if os_name == "macos" and family == 6 else 0.0,
    )
    if os_name == "windows":
        return [full]
    prog = "ping6" if family == 6 and have("ping6") else "ping"
    plain = _PingCommand([prog, "-c", str(count), host], 1.0, 10.0)
    if os_name == "macos":
        return [full, plain]
    pace = max(interval, 0.2)
    compat = _PingCommand(
        [prog, "-c", str(count), "-i", _fmt_interval(pace)]
        + ["-W", _wait_seconds(timeout), host],
        pace,
    )
    return [full, compat, plain]


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
    text = ""
    for cmd in _ping_commands(host, count, interval, timeout, size, family):
        res = run(cmd.args, timeout=cmd.budget(count, timeout))
        text = res.stdout or res.stderr
        if windows or not _looks_like_usage_error(text):
            break
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
        reason = _network_failure(text)
        if reason:
            stats.errors = {reason: stats.sent}
        else:
            # ping never got to send (permission denied, unknown host, bad
            # option): that is "could not measure", not "no answer".
            stats.error = _first_line(text) or "ping produced no output"
    return stats


_NETWORK_FAILURES = (
    "unreachable",
    "no route",
    "network is down",
    "host is down",
    "assign requested address",
)


def _network_failure(text: str) -> Optional[str]:
    """Why ping could not send, when the cause is this PC's network, else None.

    ``connect: Network is unreachable`` (cable pulled, DHCP or RA lost) means
    the requests could not leave, as when sendto fails on the socket
    back-end: they are lost, not "could not measure".
    """
    for line in text.splitlines():
        if any(k in line.lower() for k in _NETWORK_FAILURES):
            return line.strip().removeprefix("ping: ")
    return None


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:200]


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
