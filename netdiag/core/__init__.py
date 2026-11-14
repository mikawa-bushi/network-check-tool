"""Framework-independent core: results, probes, and shared helpers."""

from .errors import InputError
from .probe import (
    Context,
    Probe,
    all_probes,
    get_probe,
    register,
)
from .result import CheckResult, ExitCode, Status

__all__ = [
    "CheckResult",
    "ExitCode",
    "InputError",
    "Status",
    "Context",
    "Probe",
    "register",
    "get_probe",
    "all_probes",
]
