"""TCP port reachability with an honest three-way verdict.

``open`` (a listener accepted), ``closed`` (the host answered with a reset —
it is alive but nothing listens on that port) and ``filtered`` (no answer at
all — a firewall dropped it or the host is gone) mean very different things to
whoever has to fix it, so they are kept apart. Each check opens and immediately
closes the connection a few times; that is gentle enough for PLC/robot
controllers, which often allow only a handful of concurrent TCP sessions.
"""

from __future__ import annotations

import errno
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .stats import summarize

DEFAULT_ATTEMPTS = 2
DEFAULT_TIMEOUT = 1.5

_UNREACHABLE = {
    getattr(errno, name)
    for name in (
        "EHOSTUNREACH",
        "ENETUNREACH",
        "EHOSTDOWN",
        "WSAEHOSTUNREACH",
        "WSAENETUNREACH",
    )
    if hasattr(errno, name)
} | {10065, 10051}


@dataclass
class PortCheck:
    host: str
    port: int
    attempts: int = 0
    state: str = "error"  # open | closed | filtered | unreachable | error
    times_ms: List[float] = field(default_factory=list)  # successful connects
    reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def flaky(self) -> bool:
        """Open, but at least one attempt failed (intermittent)."""
        return self.is_open and len(self.times_ms) < self.attempts

    def to_dict(self) -> dict:
        s = summarize(self.times_ms)
        return {
            "host": self.host,
            "port": self.port,
            "state": self.state,
            "attempts": self.attempts,
            "succeeded": len(self.times_ms),
            "connect_ms": s.to_dict() if s else None,
            "reasons": dict(self.reasons),
        }


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, socket.gaierror):
        return "name resolution failed"
    if isinstance(exc, OSError):
        code = getattr(exc, "winerror", None) or exc.errno
        if code in _UNREACHABLE:
            return "unreachable"
        return exc.strerror or str(exc)
    return str(exc)


def tcp_connect_once(
    host: str, port: int, timeout: float = DEFAULT_TIMEOUT
) -> Tuple[bool, float, str]:
    """One connect attempt: ``(connected, elapsed_ms, reason)``."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - start) * 1000.0, "connected"
    except (OSError, ValueError) as e:
        return False, (time.perf_counter() - start) * 1000.0, _classify(e)


def tcp_check(
    host: str,
    port: int,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    pause: float = 0.1,
) -> PortCheck:
    res = PortCheck(host=host, port=port)
    failures = 0
    for i in range(max(1, attempts)):
        ok, ms, reason = tcp_connect_once(host, port, timeout)
        res.attempts += 1
        if ok:
            res.times_ms.append(ms)
        else:
            failures += 1
            res.reasons[reason] = res.reasons.get(reason, 0) + 1
            # A refusal or a missing name is definitive; a timeout earns one
            # retry (a single dropped SYN must not read as "filtered"), and a
            # port that keeps failing is not hammered further.
            if reason != "timeout" or failures >= 2:
                if not res.times_ms:
                    break
        if i + 1 < attempts and pause > 0:
            time.sleep(pause)
    res.state = _verdict(res)
    return res


def _verdict(res: PortCheck) -> str:
    if res.times_ms:
        return "open"
    if res.reasons.get("refused"):
        return "closed"
    if res.reasons.get("unreachable"):
        return "unreachable"
    if res.reasons and set(res.reasons) == {"timeout"}:
        return "filtered"
    return "error"


def tcp_check_many(
    items: Iterable[Tuple[str, int]],
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int = 8,
) -> Dict[Tuple[str, int], PortCheck]:
    """Check several (host, port) pairs concurrently (bounded)."""
    pairs = list(dict.fromkeys(items))
    if not pairs:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pairs)))) as pool:
        results = pool.map(
            lambda hp: tcp_check(hp[0], hp[1], attempts=attempts, timeout=timeout),
            pairs,
        )
        return dict(zip(pairs, results))


def describe_state(state: str) -> Optional[str]:
    """Machine key -> short human hint (English; reports localise separately)."""
    return {
        "open": "listening",
        "closed": "host answered, nothing listening (RST)",
        "filtered": "no answer (firewall drop or host down)",
        "unreachable": "no route / host unreachable",
    }.get(state)
