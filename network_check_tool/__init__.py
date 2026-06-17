"""Network Checker - A network monitoring tool with ping tracking and dashboard visualization."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution version (pyproject).
    __version__ = version("network-check-tool")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0+unknown"
