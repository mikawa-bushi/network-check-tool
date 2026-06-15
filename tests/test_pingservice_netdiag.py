"""Tests for issue #22 (M1 phase 1): PingService delegates to netdiag's ping core.

``PingService.ping_host`` must obtain its measurement from netdiag's reusable
``ping`` (which already returns ``(avg_rtt_ms | None, loss_pct, raw)``) instead of
the ``ping3`` library, while keeping the same returned dict contract so the
dashboard, scheduler, and stored ``PingResult`` are unaffected.
"""

import re
from pathlib import Path

import network_check_tool.services.ping_service as ps
from network_check_tool.services.ping_service import PingService


def test_success_maps_rtt_in_ms(monkeypatch):
    monkeypatch.setattr(
        ps, "_measure_ping", lambda host, count, timeout: (12.5, 0.0, "raw")
    )
    monkeypatch.setattr(ps, "have", lambda cmd: True)

    out = PingService().ping_host("example.com")

    assert out["host"] == "example.com"
    assert out["timestamp"] is not None
    assert out["response_time"] == 12.5  # already ms — must not be doubled
    assert out["packet_loss"] is False
    assert out["error_message"] is None


def test_total_loss_maps_to_packet_loss(monkeypatch):
    monkeypatch.setattr(
        ps, "_measure_ping", lambda host, count, timeout: (None, 100.0, "raw")
    )
    monkeypatch.setattr(ps, "have", lambda cmd: True)

    out = PingService().ping_host("10.0.0.9")

    assert out["response_time"] is None
    assert out["packet_loss"] is True
    assert out["error_message"]


def test_reply_with_partial_loss_counts_as_success(monkeypatch):
    # Can't happen with count=1 (loss is 0 or 100), but pin the documented
    # behaviour: a measured RTT with 0 < loss < 100 is reported as a success
    # sample (not a packet-loss row).
    monkeypatch.setattr(
        ps, "_measure_ping", lambda host, count, timeout: (12.5, 50.0, "raw")
    )
    monkeypatch.setattr(ps, "have", lambda cmd: True)

    out = PingService().ping_host("example.com")

    assert out["response_time"] == 12.5
    assert out["packet_loss"] is False
    assert out["error_message"] is None


def test_exception_maps_to_failure(monkeypatch):
    def boom(host, count, timeout):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ps, "_measure_ping", boom)
    monkeypatch.setattr(ps, "have", lambda cmd: True)

    out = PingService().ping_host("example.com")

    assert out["response_time"] is None
    assert out["packet_loss"] is True
    assert out["error_message"] == "kaboom"


def test_missing_ping_command_does_not_measure(monkeypatch):
    called = {"n": 0}

    def spy(host, count, timeout):
        called["n"] += 1
        return (1.0, 0.0, "")

    monkeypatch.setattr(ps, "_measure_ping", spy)
    monkeypatch.setattr(ps, "have", lambda cmd: False)

    out = PingService().ping_host("example.com")

    assert called["n"] == 0
    assert out["packet_loss"] is True
    assert out["response_time"] is None
    assert out["error_message"]


def test_timeout_is_forwarded(monkeypatch):
    captured = {}

    def spy(host, count, timeout):
        captured["timeout"] = timeout
        captured["count"] = count
        return (5.0, 0.0, "raw")

    monkeypatch.setattr(ps, "_measure_ping", spy)
    monkeypatch.setattr(ps, "have", lambda cmd: True)

    PingService(timeout=2.5).ping_host("example.com")

    assert captured["timeout"] == 2.5
    assert captured["count"] == 1  # one measurement per sample


def test_ping3_no_longer_used():
    # The module must not import ping3 any more...
    assert not hasattr(ps, "ping3")
    # ...and it must be gone from the project dependencies. A regex line scan
    # avoids tomllib (3.11+ only) so this runs on the project's 3.10 floor.
    pyproject_text = (
        Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text()
    )
    assert not re.search(r"(?m)^\s*ping3\s*=", pyproject_text)
