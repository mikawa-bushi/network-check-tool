"""Probe modules. Importing this package registers every built-in probe.

Each module decorates its probe classes with ``@register`` (see
:mod:`netdiag.core.probe`), so importing them here populates the global registry.
External probes can be added via entry points — see :mod:`netdiag.plugins`.
"""

from . import (  # noqa: F401  (imported for registration side effects)
    interface,
    neighbor,
    gateway,
    icmp,
    traceroute,
    dns,
    dualstack,
    tcp,
    firewall,
    http,
    tls,
    system,
    wifi,
)

__all__ = [
    "interface", "neighbor", "gateway", "icmp", "traceroute", "dns",
    "dualstack", "tcp", "firewall", "http", "tls", "system", "wifi",
]
