"""Peer tester: measure loss, jitter and throughput against a PeerServer.

Gives a verdict on the LAN itself, independent of any internet path — the only
way to assess link quality in a closed environment.
"""

from __future__ import annotations

import socket
import statistics
import time
from dataclasses import dataclass
from typing import List, Optional

from .server import _CHUNK, DEFAULT_PORT


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

    @property
    def avg_rtt_ms(self) -> Optional[float]:
        return statistics.fmean(self.rtts_ms) if self.rtts_ms else None

    @property
    def jitter_ms(self) -> Optional[float]:
        return statistics.pstdev(self.rtts_ms) if len(self.rtts_ms) > 1 else None


def measure_loss_jitter(
    host: str,
    port: int = DEFAULT_PORT,
    count: int = 50,
    interval: float = 0.02,
    timeout: float = 1.0,
) -> PeerReport:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    rtts: List[float] = []
    received = 0
    try:
        for seq in range(count):
            payload = f"{seq}:{time.time()}".encode()
            t0 = time.perf_counter()
            try:
                sock.sendto(payload, (host, port))
                data, _ = sock.recvfrom(_CHUNK)
                if data == payload:
                    rtts.append((time.perf_counter() - t0) * 1000.0)
                    received += 1
            except socket.timeout:
                pass
            time.sleep(interval)
    except OSError as e:
        return PeerReport(host, port, count, received, 100.0, rtts, error=str(e))
    finally:
        sock.close()
    loss = (count - received) / count * 100.0 if count else 100.0
    return PeerReport(host, port, count, received, loss, rtts)


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
