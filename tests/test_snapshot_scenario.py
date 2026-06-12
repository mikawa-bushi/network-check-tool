"""Tests for snapshot diffing and scenario parsing (no network needed)."""

from netdiag.snapshot.diff import diff_snapshots
from netdiag.scenario.loader import parse_scenario, merged_config


def _snap(routes, dns, ts="t"):
    return {"captured_at_iso": ts, "sources": {"routes": routes, "dns_resolvers": dns}}


def test_snapshot_diff_detects_route_change():
    base = _snap("default via 192.168.1.1 dev eth0", "nameserver 192.168.1.1", "t0")
    cur = _snap("default via 10.8.0.1 dev tun0", "nameserver 192.168.1.1", "t1")
    d = diff_snapshots(base, cur)
    assert d.any_change
    changed = {sd.key for sd in d.changed}
    assert changed == {"routes"}
    route_diff = next(sd for sd in d.changed if sd.key == "routes")
    assert any("tun0" in line for line in route_diff.added)
    assert any("eth0" in line for line in route_diff.removed)


def test_snapshot_diff_no_change():
    base = _snap("default via 192.168.1.1 dev eth0", "nameserver 1.1.1.1")
    d = diff_snapshots(base, base)
    assert not d.any_change


def test_scenario_parse_and_config_merge():
    raw = {
        "name": "office",
        "description": "corp checks",
        "checks": [
            {"probe": "gateway"},
            {"probe": "tcp", "target": "proxy.corp.local:8080", "label": "proxy"},
            {"probe": "dns", "resolver": "10.0.0.53", "expect": "intranet.corp.local"},
        ],
    }
    sc = parse_scenario(raw)
    assert sc.name == "office"
    assert sc.probe_names == ["gateway", "tcp", "dns"]
    cfg = merged_config(sc)
    assert cfg["tcp_targets"] == ["proxy.corp.local:8080"]
    assert cfg["dns_name"] == "intranet.corp.local"


def test_scenario_string_shorthand():
    sc = parse_scenario({"name": "x", "checks": ["gateway", "dns"]})
    assert sc.probe_names == ["gateway", "dns"]
