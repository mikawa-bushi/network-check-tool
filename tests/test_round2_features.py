"""Round-2 review features (2026-06-17+): F7 snapshot diff JSON."""

import json

from click.testing import CliRunner

from netdiag.cli import cli


def test_snapshot_diff_to_dict_only_changed_sources():
    from netdiag.snapshot.diff import diff_snapshots

    base = {
        "captured_at_iso": "t0",
        "sources": {"routes": "default via 10.0.0.1\n", "hosts": "127.0.0.1 lo\n"},
    }
    cur = {
        "captured_at_iso": "t1",
        "sources": {"routes": "default via 10.0.0.2\n", "hosts": "127.0.0.1 lo\n"},
    }
    obj = diff_snapshots(base, cur).to_dict()
    assert obj["any_change"] is True
    assert {c["key"] for c in obj["changed"]} == {"routes"}  # unchanged hosts excluded
    routes = obj["changed"][0]
    assert "default via 10.0.0.2" in routes["added"]
    assert "default via 10.0.0.1" in routes["removed"]
    assert routes["label"]  # human label present


def test_snapshot_diff_to_dict_no_change():
    s = {"captured_at_iso": "t", "sources": {"routes": "x\n"}}
    obj = diff_snapshots_noop(s)
    assert obj["any_change"] is False
    assert obj["changed"] == []


def diff_snapshots_noop(snapshot):
    from netdiag.snapshot.diff import diff_snapshots

    return diff_snapshots(snapshot, dict(snapshot)).to_dict()


def test_snapshot_diff_cli_json(monkeypatch):
    import netdiag.snapshot as snapmod

    monkeypatch.setattr(
        snapmod,
        "load",
        lambda name: {"captured_at_iso": "t0", "sources": {"routes": "a\n"}},
    )
    monkeypatch.setattr(
        snapmod,
        "capture",
        lambda: {"captured_at_iso": "t1", "sources": {"routes": "b\n"}},
    )
    result = CliRunner().invoke(cli, ["snapshot", "diff", "x", "--format", "json"])
    assert result.exit_code == 2  # changed -> exit 2 (mirrors text)
    obj = json.loads(result.output)
    assert obj["any_change"] is True
    assert obj["changed"][0]["key"] == "routes"
