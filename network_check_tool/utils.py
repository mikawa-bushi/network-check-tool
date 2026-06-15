"""Small shared helpers for network_check_tool."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive ``datetime`` (drop-in for ``datetime.utcnow()``).

    Naive on purpose: the ``PingResult.timestamp`` column and the dashboard's
    time-range comparisons work in naive UTC, so this returns the same instant as
    the deprecated ``datetime.utcnow()`` while avoiding the Python 3.12 deprecation.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
