"""Tests for issue #35 (M5): DnsProbe honors a scenario custom resolver.

A scenario's ``resolver:`` becomes ``ctx["_resolver"]``; the DNS probe must query
that server (via nslookup) instead of silently ignoring it. The default path
(no custom resolver) is unchanged.
"""

from netdiag.core import CheckResult, Context, Status
from netdiag.core.util import CommandResult
from netdiag.probes import dns
from netdiag.probes.dns import DnsProbe, _parse_nslookup_addresses

_NSLOOKUP_OUT = "\n".join(
    [
        "Server:\t\t8.8.8.8",
        "Address:\t8.8.8.8#53",
        "",
        "Non-authoritative answer:",
        "Name:\texample.com",
        "Address: 93.184.216.34",
        "Name:\texample.com",
        "Address: 93.184.216.35",
    ]
)


# ----------------------------------------------------------------------- parser


def test_parse_nslookup_addresses_excludes_server_line():
    assert _parse_nslookup_addresses(_NSLOOKUP_OUT) == [
        "93.184.216.34",
        "93.184.216.35",
    ]


def test_parse_nslookup_addresses_empty():
    assert _parse_nslookup_addresses("server is unreachable\n") == []


# ------------------------------------------------------------------ custom path


def test_dns_uses_custom_resolver_when_set(monkeypatch):
    calls = {}

    def spy_run(args, timeout=None):
        calls["args"] = args
        return CommandResult(
            ok=True, code=0, stdout=_NSLOOKUP_OUT, stderr="", found=True
        )

    monkeypatch.setattr(dns, "have", lambda cmd: True)
    monkeypatch.setattr(dns, "run", spy_run)

    res = DnsProbe().check(Context({"_resolver": "8.8.8.8", "dns_name": "example.com"}))

    assert res.status == Status.OK
    assert res.detail["resolver"] == "8.8.8.8"
    assert "93.184.216.34" in res.detail["answers"]
    # It actually shelled out to nslookup against the requested server.
    assert "nslookup" in calls["args"]
    assert "8.8.8.8" in calls["args"]


def test_dns_default_path_unchanged_without_resolver(monkeypatch):
    used = {"system": False}

    def fake_resolve(name, timeout=3.0):
        used["system"] = True
        return ["1.2.3.4"]

    def boom_run(*a, **k):  # the system path must not shell out
        raise AssertionError("run() should not be called on the default path")

    monkeypatch.setattr(dns, "_resolve", fake_resolve)
    monkeypatch.setattr(dns, "run", boom_run)

    res = DnsProbe().check(Context({"dns_name": "example.com"}))

    assert used["system"] is True
    assert res.status == Status.OK


def test_dns_custom_resolver_fail_path(monkeypatch):
    # nslookup returns no answer addresses (e.g. NXDOMAIN): the server header
    # line is excluded, so there are no answers -> FAIL naming the resolver.
    nxdomain = (
        "Server:\t\t8.8.8.8\nAddress:\t8.8.8.8#53\n\n** server can't find x: NXDOMAIN\n"
    )
    monkeypatch.setattr(dns, "have", lambda cmd: True)
    monkeypatch.setattr(
        dns,
        "run",
        lambda args, timeout=None: CommandResult(
            ok=True, code=1, stdout=nxdomain, stderr="", found=True
        ),
    )

    res = DnsProbe().check(
        Context({"_resolver": "8.8.8.8", "dns_name": "nope.invalid"})
    )

    assert res.status == Status.FAIL
    assert "resolver 8.8.8.8" in res.summary


def test_dns_custom_resolver_without_nslookup_warns(monkeypatch):
    monkeypatch.setattr(dns, "have", lambda cmd: False)

    def boom_run(*a, **k):
        raise AssertionError("must not query when nslookup is unavailable")

    monkeypatch.setattr(dns, "run", boom_run)

    res = DnsProbe().check(Context({"_resolver": "8.8.8.8", "dns_name": "example.com"}))

    assert res.status == Status.WARN
    assert "8.8.8.8" in res.summary
    assert "nslookup" in res.summary
