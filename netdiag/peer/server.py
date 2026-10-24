"""Peer responder: the 'other end' for two-host LAN quality tests.

Completely-local environments often can't reach anything outside, so 'is the
network healthy?' has to be answered between two of your own machines. This is a
tiny UDP echo (for loss/jitter) plus a TCP sink (for throughput) — an iperf-lite
built on the standard library so it needs no extra install on either host.
"""

from __future__ import annotations

import socket
import threading
import time

DEFAULT_PORT = 47600
_CHUNK = 65536


class PeerServer:
    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(_CHUNK)
                sock.sendto(data, addr)  # echo back for RTT/loss measurement
            except socket.timeout:
                continue
            except OSError:
                break
        sock.close()

    def _tcp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(4)
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()
        sock.close()

    @staticmethod
    def _drain(conn: socket.socket) -> None:
        """Read everything the client sends and reply with the byte count."""
        total = 0
        conn.settimeout(10)
        try:
            while True:
                chunk = conn.recv(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
            conn.sendall(str(total).encode())
        except OSError:
            pass
        finally:
            conn.close()

    def serve_forever(self) -> None:
        for target in (self._udp_loop, self._tcp_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
