"""netdiag — network checks for production engineers.

Checks this PC's network (link quality, IP settings, gateway, ARP) and every
device on the line (PLCs, HMIs, robot controllers: route, ping statistics, ARP
presence, service ports), then a dependency-graph engine separates the *root
cause* from its *consequences* and says what to check next — in Japanese or
English. Runs fully offline, as an unprivileged user, on Windows, Linux and
macOS, on the standard library alone (``click`` is the only hard dependency).
"""

import logging

from .core import CheckResult, Context, Probe, Status, register

# Library-friendly logging: attach a NullHandler so netdiag stays silent by
# default (no "lastResort" stderr output) until an application/CLI configures
# the "netdiag" logger. The library itself never calls basicConfig.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.2.0"

__all__ = ["CheckResult", "Status", "Probe", "Context", "register", "__version__"]
