"""Measurement primitives: precise ping, TCP port checks and their statistics.

Probes and the device checker build on these instead of each re-implementing a
subprocess call and a parser, so every report quotes the same, comparable
numbers (loss with a confidence bound, latency percentiles, jitter).
"""

from .icmp import (
    PingStats,
    icmp_socket_available,
    parse_posix_ping,
    parse_windows_ping,
    ping,
    ping_many,
    ping_stream,
    precise_ping_available,
    system_ping,
    win_icmp_available,
)
from .stats import (
    RttSummary,
    loss_upper_bound_pct,
    percentile,
    samples_for_loss_bound,
    summarize,
    wilson_interval,
)
from .tcp import PortCheck, tcp_check, tcp_check_many, tcp_connect_once

__all__ = [
    "PingStats",
    "ping",
    "ping_many",
    "ping_stream",
    "precise_ping_available",
    "win_icmp_available",
    "system_ping",
    "icmp_socket_available",
    "parse_posix_ping",
    "parse_windows_ping",
    "RttSummary",
    "summarize",
    "percentile",
    "wilson_interval",
    "loss_upper_bound_pct",
    "samples_for_loss_bound",
    "PortCheck",
    "tcp_check",
    "tcp_check_many",
    "tcp_connect_once",
]
