"""This host's network configuration, collected once per run, for any OS.

Probes ask questions of a :class:`HostNetwork` snapshot ("which NIC reaches
192.168.3.39?", "is there a default gateway?") instead of shelling out and
parsing on their own. Collection is OS-specific (``linux``/``macos``
modules); everything above this package is OS-neutral.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Dict, List, Optional

from ..core.util import os_name
from .model import (
    KIND_ETHERNET,
    KIND_LOOPBACK,
    KIND_OTHER,
    KIND_VIRTUAL,
    KIND_WIFI,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
    Neighbor,
    Route,
    RouteDecision,
    RouteRule,
    mac_is_local,
    normalize_mac,
)

__all__ = [
    "Address",
    "HostNetwork",
    "Interface",
    "LinkStats",
    "Neighbor",
    "Route",
    "RouteDecision",
    "RouteRule",
    "KIND_ETHERNET",
    "KIND_WIFI",
    "KIND_LOOPBACK",
    "KIND_VIRTUAL",
    "KIND_OTHER",
    "collect",
    "snapshot",
    "neighbors",
    "neighbor",
    "link_stats",
    "source_ip_for",
    "normalize_mac",
    "mac_is_local",
]


def _backend():
    if os_name() == "macos":
        from . import macos as mod
    else:
        from . import linux as mod
    return mod


def collect() -> HostNetwork:
    """Collect a fresh snapshot (never raises; problems land in ``errors``)."""
    try:
        return _backend().collect()
    except Exception as e:  # a collector bug must not abort a diagnosis
        return HostNetwork(source=os_name(), errors=[f"collection failed: {e}"])


_cache_lock = threading.Lock()
_cache: Dict[str, object] = {}
SNAPSHOT_TTL = 5.0


def snapshot(max_age: float = SNAPSHOT_TTL) -> HostNetwork:
    """A recent snapshot shared by all probes of one run (cached briefly).

    Several probes asking within a few seconds share one collection.
    """
    with _cache_lock:
        hn = _cache.get("hn")
        ts = _cache.get("ts", 0.0)
        if hn is None or time.monotonic() - ts > max_age:  # type: ignore[operator]
            hn = collect()
            _cache["hn"], _cache["ts"] = hn, time.monotonic()
        return hn  # type: ignore[return-value]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def neighbors() -> List[Neighbor]:
    try:
        return _backend().neighbors()
    except Exception:
        return []


def neighbor(ip: str) -> Optional[Neighbor]:
    """Current ARP/neighbour entry for *ip* (None if it cannot be determined)."""
    try:
        return _backend().neighbor(ip)
    except Exception:
        return None


def link_stats() -> Dict[str, LinkStats]:
    """Current per-interface counters (cheap; used for before/after deltas)."""
    try:
        return _backend().link_stats()
    except Exception:
        return {}


def source_ip_for(target_ip: str) -> Optional[str]:
    """Local address the OS would use to reach *target_ip* (sends no packet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
