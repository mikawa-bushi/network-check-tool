"""Framework-independent core: results, probes, and shared helpers."""

from .probe import (
    Context,
    Probe,
    all_probes,
    get_probe,
    register,
)
from .result import CheckResult, Status

__all__ = [
    "CheckResult",
    "Status",
    "Context",
    "Probe",
    "register",
    "get_probe",
    "all_probes",
]
