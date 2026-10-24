"""Precise, unprivileged ICMP echo measurement.

Two back-ends, chosen automatically:

* **ICMP datagram socket** (Linux, macOS): ``socket(AF_INET, SOCK_DGRAM,
  IPPROTO_ICMP)`` needs no root, gives microsecond timing, sub-second send
  intervals and detects duplicate replies (two devices answering for one IP).
  One socket and one thread serve many targets at once, like ``fping``, so
  checking 30 devices takes as long as checking one.
* **System ``ping`` command** (Windows, or when datagram ICMP is not permitted):
  run in parallel per target and parsed *per reply line* in a language-neutral
  way. Japanese Windows prints ``時間 <1ms`` and ``(25% の損失)``; English prints
  ``time<1ms`` and ``(25% loss)`` — the parser relies only on the ``=``/``<``
  and ``ms`` tokens. Counting reply lines also fixes the classic Windows gotcha
  where "Destination host unreachable" replies are reported as *received* (0 %
  loss) in ping's own statistics.

Both produce a :class:`PingStats` with per-reply RTTs, so every report shows the
same statistics (loss with a 95 % upper bound, min/median/p95/max, jitter).
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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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

    ``method`` is ``auto`` (socket when permitted, else system ping),
    ``socket`` or ``system``. ``family`` 4/6 forces an address family.
    """
    count = max(1, int(count))
    method = os.environ.get("NETDIAG_PING_METHOD", "") or method
    out: Dict[str, PingStats] = {}
    socket_jobs: Dict[str, str] = {}  # target -> IPv4 address
    system_jobs: List[Tuple[str, Optional[int]]] = []

    for t in dict.fromkeys(targets):  # de-duplicate, keep order
        fam = family or _literal_family(t)
        use_socket = (
            method in ("auto", "socket") and fam != 6 and icmp_socket_available()
        )
        if use_socket:
            addr = _resolve_v4(t, timeout=max(timeout, 2.0))
            if addr is None:
                out[t] = PingStats(
                    target=t, method="icmp-socket", error="name resolution failed"
                )
                continue
            socket_jobs[t] = addr
        else:
            system_jobs.append((t, fam))

    if socket_jobs:
        out.update(
            _SocketPinger(
                socket_jobs,
                count=count,
                interval=interval,
                timeout=timeout,
                size=size,
                max_pps=max_pps,
            ).run()
        )
    if system_jobs:
        workers = max(1, min(32, len(system_jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                t: pool.submit(
                    system_ping,
                    t,
                    count=count,
                    interval=interval,
                    timeout=timeout,
                    size=size,
                    family=fam,
                )
                for t, fam in system_jobs
            }
            for t, fut in futures.items():
                out[t] = fut.result()
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
    """fping-style: one socket, one thread, many targets, paced sends."""

    def __init__(
        self,
        jobs: Dict[str, str],
        count: int,
        interval: float,
        timeout: float,
        size: int,
        max_pps: float,
    ) -> None:
        self.jobs = jobs
        self.count = count
        self.interval = max(0.001, interval)
        self.timeout = max(0.05, timeout)
        self.size = max(len(_MAGIC) + 4, size)
        self.min_gap = 1.0 / max_pps if max_pps > 0 else 0.0
        self.token = os.urandom(4)
        self.ident = os.getpid() & 0xFFFF

    def _payload(self, seq: int) -> bytes:
        body = _MAGIC + self.token + struct.pack("!I", seq)
        return body + b"\x00" * max(0, self.size - len(body))

    def run(self) -> Dict[str, PingStats]:
        stats = {
            t: PingStats(target=t, address=a, method="icmp-socket", resolution_ms=0.001)
            for t, a in self.jobs.items()
        }
        by_addr: Dict[str, List[str]] = {}
        for t, a in self.jobs.items():
            by_addr.setdefault(a, []).append(t)
        addrs = list(by_addr)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        except OSError as e:
            for s in stats.values():
                s.error = f"icmp socket unavailable: {e}"
            return stats
        sock.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(sock, selectors.EVENT_READ)

        # Schedule: request i to every address, spread evenly over one interval,
        # never faster than min_gap overall. seq encodes (round, address index).
        n = len(addrs)
        schedule: List[Tuple[float, int, int]] = []
        step = max(self.interval / n, self.min_gap)
        for i in range(self.count):
            for j in range(n):
                schedule.append(
                    (i * max(self.interval, n * self.min_gap) + j * step, i, j)
                )
        schedule.sort()

        sent_at: Dict[Tuple[int, int], float] = {}  # (addr idx, round) -> t
        seen: Dict[Tuple[int, int], bool] = {}
        rtts: Dict[int, Dict[int, float]] = {j: {} for j in range(n)}
        late = [0] * n
        dups = [0] * n
        sent = [0] * n
        errors: List[Dict[str, int]] = [{} for _ in range(n)]
        t0 = time.perf_counter()
        next_idx = 0
        last_send = t0
        try:
            while True:
                now = time.perf_counter()
                # send everything that is due
                while next_idx < len(schedule) and t0 + schedule[next_idx][0] <= now:
                    _, rnd, j = schedule[next_idx]
                    next_idx += 1
                    seq = rnd * n + j
                    pkt = build_echo_request(self.ident, seq, self._payload(seq))
                    try:
                        sock.sendto(pkt, (addrs[j], 0))
                        sent_at[(j, rnd)] = time.perf_counter()
                    except OSError as e:
                        # e.g. "No route to host" after ARP failed: the packet
                        # never left, so it counts as sent-and-lost with a reason.
                        sent_at[(j, rnd)] = time.perf_counter()
                        msg = e.strerror or str(e)
                        errors[j][msg] = errors[j].get(msg, 0) + 1
                    sent[j] += 1
                    last_send = time.perf_counter()
                    now = last_send
                done_sending = next_idx >= len(schedule)
                outstanding = len(sent_at) - len(seen)
                if done_sending and (
                    outstanding <= 0 or now - last_send > self.timeout
                ):
                    break
                # wait for a reply or the next send, whichever comes first
                if not done_sending:
                    wait = max(0.0, t0 + schedule[next_idx][0] - now)
                else:
                    wait = max(0.0, last_send + self.timeout - now)
                for _key, _ev in sel.select(timeout=min(wait, 0.05)):
                    self._drain(sock, addrs, n, sent_at, seen, rtts, late, dups)
        finally:
            sel.close()
            sock.close()

        for j, addr in enumerate(addrs):
            for t in by_addr[addr]:
                s = stats[t]
                s.sent = sent[j]
                ordered = [rtts[j][r] for r in sorted(rtts[j])]
                s.rtts_ms = ordered
                s.received = len(ordered)
                s.duplicates = dups[j]
                if late[j]:
                    s.errors["late reply (after timeout)"] = late[j]
                for msg, c in errors[j].items():
                    s.errors[msg] = s.errors.get(msg, 0) + c
        return stats

    def _drain(self, sock, addrs, n, sent_at, seen, rtts, late, dups) -> None:
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
                (seq_in_payload,) = struct.unpack("!I", payload[8:12])
            except struct.error:
                continue
            if seq_in_payload & 0xFFFF != seq:
                continue
            seq = seq_in_payload
            rnd, j = divmod(seq, n)
            if j >= n or src[0] != addrs[j] or (j, rnd) not in sent_at:
                continue
            if seen.get((j, rnd)):
                dups[j] += 1  # a second answer to the same request: IP conflict/loop
                continue
            seen[(j, rnd)] = True
            rtt = (t_recv - sent_at[(j, rnd)]) * 1000.0
            if rtt > self.timeout * 1000.0:
                late[j] += 1
                continue
            rtts[j][rnd] = rtt


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
    budget = count * (max(interval, 1.0 if windows else 0.2) + timeout) + 5
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
        blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
        return bool(blocks) and len(blocks[0].splitlines()) > 1
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
            m = _POSIX_TIME_RE.search(line)
            if "(DUP!)" in line:
                dup_lines += 1
            elif m:
                rtts.append(float(m.group(1)))
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

    region: List[str] = blocks[0][1:] if blocks else []
    rtts: List[float] = []
    errors: Dict[str, int] = {}
    if blocks and len(blocks[0]) == 1 and len(blocks) == 1:
        # A lone line is a pre-flight error (unknown host, bad option ...).
        errors[blocks[0][0]] = 1
        return count, 0, 0, rtts, errors
    for line in region:
        tokens = _WIN_RTT_RE.findall(line)
        is_reply = len(tokens) == 1 and not any(sep in line for sep in _WIN_LIST_SEP)
        if is_reply:
            m = _WIN_RTT_RE.search(line)
            value = float(m.group(1))
            if "<" in line[m.start() : m.end()]:
                value = max(0.0, value - 0.5)  # "<1ms" -> 0.5 ms
            rtts.append(value)
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
