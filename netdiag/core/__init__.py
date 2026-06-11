"""Framework-independent core: results, probes, and shared helpers."""

from .result import CheckResult, Status
from .probe import (
    Context,
    Probe,
    register,
    get_probe,
    all_probes,
    registry_names,
)

__all__ = [
    "CheckResult",
    "Status",
    "Context",
    "Probe",
    "register",
    "get_probe",
    "all_probes",
    "registry_names",
]
