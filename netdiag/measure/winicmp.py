"""Windows ICMP echo and ARP through ``iphlpapi.dll`` — no administrator rights.

``ping.exe`` paces itself at one request per second and reports whole
milliseconds, so ten pings take ten seconds and every LAN reply reads "<1ms".
``IcmpSendEcho`` (the API ping.exe itself uses) has neither limit: requests go
out at the interval netdiag asks for, the round trip is timed around the call
with ``perf_counter`` (sub-millisecond), several replies to one request reveal
an IP conflict, and statuses such as "destination host unreachable" are
explicit losses rather than "received" replies.

``SendARP`` asks the network for a device's MAC directly. When it reports that
nobody answered, that is definite evidence the device is absent — stronger
than inferring it from a missing ``arp -a`` line.

The Win32 calls sit behind :class:`WinIcmpApi` so the pinger can be exercised
with a fake on any OS.
"""

from __future__ import annotations

import ctypes
import itertools
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from .icmp import PingStats, SampleFn

IP_SUCCESS = 0
IP_REQ_TIMED_OUT = 11010
ERROR_BAD_NET_NAME = 67  # SendARP: nobody answered the ARP request

STATUS_TEXT = {
    11001: "reply buffer too small",
    11002: "destination net unreachable",
    11003: "destination host unreachable",
    11004: "destination protocol unreachable",
    11005: "destination port unreachable",
    11009: "packet too big",
    11010: "request timed out",
    11011: "bad request",
    11013: "TTL expired in transit",
    11015: "parameter problem",
    11032: "negotiating IPsec",
    11050: "general failure",
}

MAX_REPLIES = 4  # room for duplicate answers to one request


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class ICMP_ECHO_REPLY(ctypes.Structure):
    # Win32 ULONG/USHORT are 32/16-bit on every Windows target; fixed-width
    # types keep the layout right (28 bytes on x86, 40 on x64).
    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint32),
        ("DataSize", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("Data", ctypes.c_void_p),
        ("Options", IP_OPTION_INFORMATION),
    ]


def ipaddr(ip: str) -> int:
    """IPv4 text -> Win32 ``IPAddr`` (network byte order held in a ULONG)."""
    return struct.unpack("=I", socket.inet_aton(ip))[0]


def status_text(code: int) -> str:
    return STATUS_TEXT.get(code, f"ICMP status {code}")


class WinIcmpApi:
    """Thin ctypes wrapper over IcmpCreateFile / IcmpSendEcho / SendARP."""

    def __init__(self) -> None:
        lib = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)  # type: ignore[attr-defined]
        self._create = lib.IcmpCreateFile
        self._create.argtypes = []
        self._create.restype = ctypes.c_void_p
        self._send = lib.IcmpSendEcho
        self._send.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._send.restype = ctypes.c_uint32
        self._close = lib.IcmpCloseHandle
        self._close.argtypes = [ctypes.c_void_p]
        self._close.restype = ctypes.c_int
        self._arp = lib.SendARP
        self._arp.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._arp.restype = ctypes.c_uint32

    def open(self) -> int:
        handle = self._create()
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "IcmpCreateFile failed")
        return handle

    def close(self, handle: int) -> None:
        self._close(handle)

    def echo(
        self, handle: int, ip: str, payload: bytes, timeout_ms: int
    ) -> Tuple[int, int, int]:
        """One echo request: ``(status, replies, reported_rtt_ms)``."""
        size = (ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8) * MAX_REPLIES + 64
        reply = ctypes.create_string_buffer(size)
        request = ctypes.create_string_buffer(payload, len(payload))
        n = self._send(
            handle, ipaddr(ip), request, len(payload), None, reply, size, timeout_ms
        )
        if n == 0:
            return ctypes.get_last_error() or IP_REQ_TIMED_OUT, 0, 0
        first = ICMP_ECHO_REPLY.from_buffer(reply)
        return first.Status, n, first.RoundTripTime

    def send_arp(self, ip: str) -> Tuple[int, Optional[str]]:
        """Resolve *ip* to a MAC with an ARP request: ``(win32 error, mac)``."""
        mac = (ctypes.c_ubyte * 8)()
        length = ctypes.c_uint32(8)
        err = self._arp(ipaddr(ip), 0, ctypes.byref(mac), ctypes.byref(length))
        if err != 0 or length.value < 6:
            return err or ERROR_BAD_NET_NAME, None
        return 0, ":".join(f"{b:02x}" for b in bytes(mac)[:6])


_api: Optional[WinIcmpApi] = None
_api_ok: Optional[bool] = None
_api_lock = threading.Lock()


def get_api() -> Optional[WinIcmpApi]:
    """The Win32 API wrapper, or None off Windows / when it cannot be used."""
    global _api, _api_ok
    if _api_ok is None:
        with _api_lock:
            if _api_ok is None:
                try:
                    api = WinIcmpApi()
                    api.close(api.open())  # prove a handle can be created
                    _api, _api_ok = api, True
                except (AttributeError, OSError):
                    _api, _api_ok = None, False
    return _api


class RateLimiter:
    """Shared send-rate ceiling for many threads (requests per second)."""

    def __init__(self, per_second: float) -> None:
        self.gap = 1.0 / per_second if per_second > 0 else 0.0
        self._next = time.perf_counter()
        self._lock = threading.Lock()

    def acquire(self, stop: Optional[threading.Event] = None) -> None:
        if not self.gap:
            return
        with self._lock:
            now = time.perf_counter()
            slot = max(now, self._next)
            self._next = slot + self.gap
        delay = slot - time.perf_counter()
        if delay > 0:
            (stop.wait(delay) if stop is not None else time.sleep(delay))


class WinIcmpPinger:
    """One echo loop per address (IcmpSendEcho is synchronous), paced and capped."""

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
        api=None,
    ) -> None:
        self.jobs = jobs
        self.interval = max(0.001, interval)
        self.timeout_ms = max(50, int(timeout * 1000))
        self.payload = (b"netdiag" * ((size // 7) + 1))[: max(8, size)]
        self.count = count
        self.on_sample = on_sample
        self.stop = stop or threading.Event()
        self.api = api or get_api()
        self.limiter = RateLimiter(max_pps)
        self.keep_rtts = count is not None  # streaming keeps counts only

    def run(self):
        by_addr: Dict[str, List[str]] = {}
        for t, a in self.jobs.items():
            by_addr.setdefault(a, []).append(t)
        addrs = list(by_addr)
        stats = {
            t: PingStats(target=t, address=a, method="win-icmp", resolution_ms=0.001)
            for t, a in self.jobs.items()
        }
        if self.api is None:
            for s in stats.values():
                s.error = "Windows ICMP API unavailable"
            return stats

        lock = threading.Lock()
        t0 = time.perf_counter()
        n = len(addrs)

        def loop(j: int) -> None:
            addr = addrs[j]
            try:
                handle = self.api.open()
            except OSError as e:
                with lock:
                    for t in by_addr[addr]:
                        stats[t].error = f"IcmpCreateFile failed: {e}"
                return
            rounds = range(self.count) if self.count is not None else itertools.count()
            first_due = t0 + (j / n) * self.interval
            try:
                for i in rounds:
                    if self.stop.is_set():
                        break
                    delay = first_due + i * self.interval - time.perf_counter()
                    if delay < -self.interval:
                        # netdiag itself was frozen (Ctrl-Z, a VM pause): resume
                        # the pace instead of catching up on every missed request.
                        first_due -= delay
                        delay = 0.0
                    if delay > 0 and self.stop.wait(delay):
                        break
                    self.limiter.acquire(self.stop)
                    wall = time.time()
                    start = time.perf_counter()
                    status, replies, _reported = self.api.echo(
                        handle, addr, self.payload, self.timeout_ms
                    )
                    rtt = (time.perf_counter() - start) * 1000.0
                    ok = status == IP_SUCCESS and replies > 0
                    error = None if ok else status_text(status)
                    with lock:
                        for t in by_addr[addr]:
                            s = stats[t]
                            s.sent += 1
                            if ok:
                                s.received += 1
                                if self.keep_rtts:
                                    s.rtts_ms.append(rtt)
                                if replies > 1:
                                    s.duplicates += replies - 1
                            elif status != IP_REQ_TIMED_OUT:
                                s.add_error(error)
                    if self.on_sample is not None:
                        for t in by_addr[addr]:
                            self.on_sample(t, wall, rtt if ok else None, error)
            finally:
                self.api.close(handle)

        if self.count is None:
            threads = [
                threading.Thread(target=loop, args=(j,), daemon=True) for j in range(n)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(64, n))) as pool:
                list(pool.map(loop, range(n)))
        return stats


def arp_lookup(ip: str, api=None) -> Tuple[str, Optional[str]]:
    """``("present", mac)``, ``("absent", None)`` or ``("unknown", None)``."""
    api = api or get_api()
    if api is None:
        return "unknown", None
    try:
        err, mac = api.send_arp(ip)
    except OSError:
        return "unknown", None
    if err == 0 and mac:
        return "present", mac
    if err == ERROR_BAD_NET_NAME:
        return "absent", None
    return "unknown", None


__all__ = [
    "WinIcmpApi",
    "WinIcmpPinger",
    "RateLimiter",
    "arp_lookup",
    "get_api",
    "ipaddr",
    "status_text",
]
