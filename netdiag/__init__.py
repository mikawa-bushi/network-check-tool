"""netdiag — local-first network & OS trouble isolation.

A diagnosis tool, not just a monitor: it runs a layered set of probes (L1→L7 plus
host/OS), then a dependency-graph engine separates the *root cause* from its
*consequences* and tells the user what to check next. Designed to run fully
offline, as an unprivileged user, on the standard library alone (``click`` is the
only hard dependency; everything else is optional).
"""

from .core import CheckResult, Status, Probe, Context, register

__version__ = "0.1.0"

__all__ = ["CheckResult", "Status", "Probe", "Context", "register", "__version__"]
