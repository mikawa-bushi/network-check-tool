"""Peer tester: measure loss, jitter and throughput against a PeerServer.

Gives a verdict on the LAN itself, independent of any internet path — the only
way to assess link quality in a closed environment.
"""

from __future__ import annotations

import secrets
import socket
import statistics
import time
from dataclasses import dataclass
from typing import List, Optional

from .server import _BODY, _CHUNK, DEFAULT_PORT, REPLY, REQUEST


@dataclass
class PeerReport:
    host: str
    port: int
    sent: int
    received: int
    loss_pct: float
    rtts_ms: List[float]
    throughput_mbps: Optional[float] = None
    error: Optional[str] = None
    # The peer host answered 'port unreachable': no responder listens there.
    refused: bool = False

    @property
    def avg_rtt_ms(self) -> Optional[float]:
        return statistics.fmean(self.rtts_ms) if self.rtts_ms else None

    @property
    def jitter_ms(self) -> Optional[float]:
        return statistics.pstdev(self.rtts_ms) if len(self.rtts_ms) > 1 else None


def _wait_for_reply(sock: socket.socket, request: bytes, deadline: float) -> bool:
    """Read until the reply to *request* arrives (True) or *deadline* passes.

    Late replies to earlier probes, duplicates and anything else are skipped,
    so one slow reply never shifts every later probe onto the wrong answer.
    A 0.5 responder echoes the request unchanged; that counts as a reply too.
    """
    expected = (REPLY + request[len(REQUEST) :], request)
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return False
        sock.settimeout(remaining)
        try:
            data = sock.recv(2048)
        except socket.timeout:
            return False
        if data in expected:
            return True


def measure_loss_jitter(
    host: str,
    port: int = DEFAULT_PORT,
    count: int = 50,
    interval: float = 0.02,
    timeout: float = 1.0,
) -> PeerReport:
    """Send *count* numbered requests; raises ``socket.gaierror`` for an unknown host."""
    token = secrets.randbits(64)
    rtts: List[float] = []
    sent = received = 0
    refused = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            # Connected: replies from anyone else are dropped by the OS, and
            # an ICMP 'port unreachable' from the peer surfaces as a refusal.
            sock.connect((host, port))
            for seq in range(count):
                request = REQUEST + _BODY.pack(seq, token)
                t0 = time.perf_counter()
                sent += 1
                try:
                    sock.send(request)
                    if _wait_for_reply(sock, request, t0 + timeout):
                        rtts.append((time.perf_counter() - t0) * 1000.0)
                        received += 1
                except (ConnectionRefusedError, ConnectionResetError):
                    refused = True
                    if not received:
                        break
                time.sleep(interval)
        except socket.gaierror:
            raise
        except OSError as e:
            return PeerReport(host, port, sent, received, 100.0, rtts, error=str(e))
    loss = (sent - received) / sent * 100.0 if sent else 100.0
    return PeerReport(host, port, sent, received, loss, rtts, refused=refused)


def measure_throughput(
    host: str, port: int = DEFAULT_PORT, seconds: float = 3.0, timeout: float = 10.0
) -> Optional[float]:
    """Push data for *seconds* and return throughput in Mbit/s."""
    blob = b"\x00" * _CHUNK
    sent = 0
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            deadline = time.perf_counter() + seconds
            start = time.perf_counter()
            while time.perf_counter() < deadline:
                sock.sendall(blob)
                sent += len(blob)
            sock.shutdown(socket.SHUT_WR)
            try:
                sock.recv(64)  # server's acknowledged byte count
            except OSError:
                pass
            elapsed = time.perf_counter() - start
    except OSError:
        return None
    if elapsed <= 0:
        return None
    return (sent * 8) / elapsed / 1e6
