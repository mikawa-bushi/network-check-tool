"""Shared test setup.

* Pin the display language to English (before netdiag is imported, so CLI help
  texts are English too); tests that check Japanese output set it explicitly.
* Reset process-wide caches (language, network snapshot, OUI table) between
  tests so fakes installed by one test never leak into another.
"""

import os

os.environ["NETDIAG_LANG"] = "en"

import pytest  # noqa: E402

from netdiag import i18n, netinfo, oui  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_process_state(monkeypatch):
    monkeypatch.setenv("NETDIAG_LANG", "en")
    i18n.set_language("en")
    netinfo.clear_cache()
    oui.reset()
    yield
    i18n.set_language("en")
    netinfo.clear_cache()
    oui.reset()


@pytest.fixture
def ja():
    """Render in Japanese for the duration of a test."""
    i18n.set_language("ja")
    yield
    i18n.set_language("en")


class RecordingSink:
    """Alert sink that keeps every emitted transition."""

    def __init__(self):
        self.events = []

    def emit(self, transition):
        self.events.append(transition)


@pytest.fixture
def recording_sink():
    return RecordingSink()


@pytest.fixture
def idle_monitor(monkeypatch):
    """Monitors a test starts register hosts but never ping (no network, no rows)."""
    from network_check_tool.services import SchedulerService

    monkeypatch.setattr(SchedulerService, "_sweep", lambda self, interval: None)
