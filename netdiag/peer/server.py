"""Peer responder: the 'other end' for two-host LAN quality tests.

Completely-local environments often can't reach anything outside, so 'is the
network healthy?' has to be answered between two of your own machines. This is a
tiny UDP responder (for loss/jitter) plus a TCP sink (for throughput) — an
iperf-lite built on the standard library so it needs no extra install on
either host.

The responder runs on a plant network next to PLCs, so it must not be usable
as a reflector: it answers only a well-formed netdiag request, never a reply
(two responders cannot ping-pong), never a privileged or its own source port,
and at a bounded rate.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Optional, Tuple

from ..core.util import is_windows

logger = logging.getLogger(__name__)

DEFAULT_PORT = 47600
_CHUNK = 65536

# Request and reply: a 4-byte magic, the probe's sequence number, and the
# client's per-run token. The reply only swaps the magic.
REQUEST = b"NDPQ"
REPLY = b"NDPR"
_BODY = struct.Struct("!IQ")
PACKET_SIZE = len(REQUEST) + _BODY.size

DEFAULT_REPLY_RATE = 200.0  # replies per second, all clients together
MAX_CONNECTIONS = 8  # concurrent throughput tests
_CONNECTION_SECONDS = 30.0  # a throughput test takes 3 s


def reply_for(data: bytes, addr: Tuple[str, int], own_port: int) -> Optional[bytes]:
    """The reply to one datagram, or None when the responder must stay silent."""
    if len(data) != PACKET_SIZE or not data.startswith(REQUEST):
        return None
    port = addr[1]
    if port < 1024 or port == own_port:
        return None
    return REPLY + data[len(REQUEST) :]


class _RateLimit:
    """Token bucket: at most *rate* events per second, bursts up to *rate*."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.tokens = rate
        self.last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def _exclusive(sock: socket.socket) -> None:
    """Keep another program from binding the same port while we hold it."""
    if is_windows():
        flag = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if flag is not None:
            sock.setsockopt(socket.SOL_SOCKET, flag, 1)


class PeerServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        reply_rate: float = DEFAULT_REPLY_RATE,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.host = host
        self.port = port
        self.replies_sent = 0
        self._limit = _RateLimit(reply_rate)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._udp: Optional[socket.socket] = None
        self._tcp: Optional[socket.socket] = None

    def bind(self) -> None:
        """Bind both sockets now; raises OSError (port in use, address not local)."""
        if self._udp is not None:
            return
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _exclusive(udp)
            udp.bind((self.host, self.port))
            if is_windows():
                _exclusive(tcp)
            else:  # restart quickly after a Ctrl-C (TIME_WAIT)
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind((self.host, self.port))
            tcp.listen(16)
        except OSError:
            udp.close()
            tcp.close()
            raise
        udp.settimeout(0.5)
        tcp.settimeout(0.5)
        self._udp, self._tcp = udp, tcp

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(2048)
            except socket.timeout:
                continue
            except (ConnectionResetError, ConnectionRefusedError):
                # Windows reports an ICMP 'port unreachable' for an earlier
                # reply on the next receive; the socket itself is fine.
                continue
            except OSError as e:
                if self._stop.is_set():
                    break
                logger.warning("peer UDP receive failed: %s", e)
                time.sleep(0.05)
                continue
            reply = reply_for(data, addr, self.port)
            if reply is None or not self._limit.allow():
                continue
            try:
                self._udp.sendto(reply, addr)
                self.replies_sent += 1
            except OSError as e:
                logger.debug("peer UDP reply to %s failed: %s", addr, e)

    def _tcp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError as e:  # EMFILE and friends: wait and keep serving
                if self._stop.is_set():
                    break
                logger.warning("peer TCP accept failed: %s", e)
                time.sleep(0.1)
                continue
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn: socket.socket) -> None:
        """Read everything the client sends and reply with the byte count."""
        total = 0
        deadline = time.monotonic() + _CONNECTION_SECONDS
        conn.settimeout(10)
        try:
            while time.monotonic() < deadline:
                chunk = conn.recv(_CHUNK)
                if not chunk:
                    conn.sendall(str(total).encode())
                    break
                total += len(chunk)
        except OSError:
            pass
        finally:
            conn.close()
            self._slots.release()

    def serve_forever(self) -> None:
        self.bind()
        threads = [
            threading.Thread(target=loop, daemon=True)
            for loop in (self._udp_loop, self._tcp_loop)
        ]
        for t in threads:
            t.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.stop()
        for t in threads:
            t.join(timeout=1.0)
        for sock in (self._udp, self._tcp):
            sock.close()

    def stop(self) -> None:
        self._stop.set()
