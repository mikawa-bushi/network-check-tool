"""PingService measures through netdiag's ping core.

``PingService.ping_hosts`` must obtain its measurements from netdiag's
``ping_many`` (one concurrent sweep for every host) instead of the ``ping3``
library, while keeping the same returned dict contract so the dashboard,
scheduler, and stored ``PingResult`` are unaffected.
"""

import re
from pathlib import Path

import network_check_tool.services.ping_service as ps
from netdiag.measure import PingStats
from network_check_tool.services.ping_service import PingService


def _reply(rtt):
    return PingStats(target="", sent=1, received=1, rtts_ms=[rtt])


def _patch(monkeypatch, fake):
    monkeypatch.setattr(ps, "ping_many", fake)


def test_success_maps_rtt_in_ms(monkeypatch):
    _patch(monkeypatch, lambda hosts, count, timeout: {"example.com": _reply(12.5)})

    out = PingService().ping_host("example.com")

    assert out["host"] == "example.com"
    assert out["timestamp"] is not None
    assert out["response_time"] == 12.5  # already ms — must not be doubled
    assert out["packet_loss"] is False
    assert out["error_message"] is None


def test_total_loss_maps_to_packet_loss(monkeypatch):
    _patch(
        monkeypatch,
        lambda hosts, count, timeout: {
            "10.0.0.9": PingStats(target="10.0.0.9", sent=1)
        },
    )

    out = PingService().ping_host("10.0.0.9")

    assert out["response_time"] is None
    assert out["packet_loss"] is True
    assert out["error_message"] == "Request timeout"


def test_unmeasurable_host_reports_the_reason(monkeypatch):
    failed = PingStats(target="nope.invalid", error="name resolution failed")
    _patch(monkeypatch, lambda hosts, count, timeout: {"nope.invalid": failed})

    out = PingService().ping_host("nope.invalid")

    assert out["packet_loss"] is True
    assert out["error_message"] == "name resolution failed"


def test_every_host_is_pinged_in_one_sweep(monkeypatch):
    calls = []

    def fake(hosts, count, timeout):
        calls.append(list(hosts))
        return {"a": _reply(1.0), "c": PingStats(target="c", sent=1)}

    _patch(monkeypatch, fake)

    out = PingService().ping_hosts(["a", "b", "c"])

    assert calls == [["a", "b", "c"]]
    assert [r["host"] for r in out] == ["a", "b", "c"]  # input order kept
    assert [r["packet_loss"] for r in out] == [False, True, True]
    assert out[1]["error_message"] == "not measured"
    assert len({r["timestamp"] for r in out}) == 1  # one sweep, one timestamp


def test_exception_maps_to_failure(monkeypatch):
    def boom(hosts, count, timeout):
        raise RuntimeError("kaboom")

    _patch(monkeypatch, boom)

    out = PingService().ping_host("example.com")

    assert out["response_time"] is None
    assert out["packet_loss"] is True
    assert out["error_message"] == "kaboom"


def test_timeout_is_forwarded(monkeypatch):
    captured = {}

    def spy(hosts, count, timeout):
        captured["timeout"] = timeout
        captured["count"] = count
        return {"example.com": _reply(5.0)}

    _patch(monkeypatch, spy)

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
