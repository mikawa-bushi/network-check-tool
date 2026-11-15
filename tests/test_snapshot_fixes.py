"""``netdiag snapshot``: real changes, and only real changes."""

import importlib
import json

import pytest

from netdiag import netinfo
from netdiag.cli import main
from netdiag.core.util import CommandResult
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    Address,
    HostNetwork,
    Interface,
    Neighbor,
    Route,
)

cap = importlib.import_module("netdiag.snapshot.capture")


def _run(capsys, *args, lang="en"):
    with pytest.raises(SystemExit) as exc:
        main(["--lang", lang, "snapshot", *args], prog_name="netdiag")
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def test_unsupported_os_refuses_instead_of_saving_an_empty_snapshot(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cap, "os_name", lambda: "other")
    monkeypatch.setattr(cap, "DEFAULT_DIR", tmp_path)
    code, _, err = _run(capsys, "save", "x")
    assert code == 4 and "not supported" in err
    assert not (tmp_path / "x.json").exists()


def _linux_host(gateway="192.0.2.1"):
    return HostNetwork(
        source="linux:ip-json",
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                mtu=1500,
                speed_mbps=1000.0,
                mac="00:26:92:aa:bb:cc",
                addresses=[
                    Address("192.0.2.10", 24, flags=["dhcp"]),
                    Address("2001:db8:1::abcd", 64, family=6),
                ],
            )
        ],
        routes=[Route("0.0.0.0/0", gateway=gateway, interface="eth0", metric=100)],
        resolvers=["192.0.2.53"],
    )


@pytest.fixture
def linux_pc(monkeypatch, tmp_path):
    state = {
        "hn": _linux_host(),
        "neigh": [Neighbor("192.0.2.1", "cc:1a:fa:c0:a0:6e", "reachable")],
        "ip -6 route show": "2001:db8:1::/64 dev eth0 proto ra metric 100 "
        "expires 1795sec pref medium\n",
        "ss -Htln": "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
    }
    monkeypatch.setattr(cap, "os_name", lambda: "linux")
    monkeypatch.setattr(netinfo, "collect", lambda: state["hn"])
    monkeypatch.setattr(netinfo, "neighbors", lambda: state["neigh"])

    def fake_run(args, timeout=10.0):
        key = " ".join(a for a in args if not a.startswith("net.ipv4"))
        text = state.get(key, "net.ipv4.ip_forward = 0\n")
        return CommandResult(True, 0, text, "", True)

    monkeypatch.setattr(cap, "run", fake_run)
    monkeypatch.setattr(cap, "_read", lambda path: None)
    monkeypatch.setattr(cap, "DEFAULT_DIR", tmp_path)
    return state


def test_linux_lifetimes_states_and_column_widths_are_not_changes(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["hn"].interfaces[0].addresses[1] = Address(
        "2001:db8:1::9999", 64, family=6
    )
    linux_pc["neigh"] = [Neighbor("192.0.2.1", "cc:1a:fa:c0:a0:6e", "stale")]
    linux_pc["ip -6 route show"] = linux_pc["ip -6 route show"].replace("1795", "1790")
    linux_pc["ss -Htln"] = (
        "LISTEN 0   128   0.0.0.0:22                 0.0.0.0:*\n"
        "LISTEN 3   4096  127.0.0.53%lo:53           0.0.0.0:*\n"
    )
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 0, out


def test_linux_gateway_and_dns_changes_are_reported(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["hn"] = _linux_host(gateway="192.0.2.254")
    linux_pc["hn"].resolvers = ["198.51.100.53"]
    code, out, _ = _run(capsys, "diff", "ok", "--format", "json")
    assert code == 2
    changed = {c["key"] for c in json.loads(out)["changed"]}
    assert changed == {"routes", "dns_resolvers"}


def test_a_source_unreadable_at_one_end_is_not_compared(linux_pc, capsys, monkeypatch):
    real = cap.run
    monkeypatch.setattr(
        cap,
        "run",
        lambda args, timeout=10.0: (
            CommandResult(False, 127, "", "", False) if args[0] == "ss" else real(args)
        ),
    )
    assert _run(capsys, "save", "ok")[0] == 0
    monkeypatch.setattr(cap, "run", real)
    linux_pc["ss -Htln"] = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 3
    assert "no configuration changes" in out
    assert "? Listening sockets: not compared (it could not be read when saved)" in out


@pytest.mark.parametrize("name", ["2026/11/13", "a\\b", "..", "x:y"])
def test_bad_snapshot_names_are_usage_errors(linux_pc, capsys, name):
    code, _, err = _run(capsys, "save", name)
    assert code == 4 and "cannot be a snapshot name" in err


@pytest.mark.parametrize(
    "content, problem",
    [
        ('{"version": 2, "sources": {"routes": "defa', "is damaged"),
        ('{"version": 1, "sources": {"routes": "default via x"}}', "older netdiag"),
    ],
)
def test_damaged_or_old_snapshots_are_usage_errors(
    linux_pc, capsys, tmp_path, content, problem
):
    (tmp_path / "s.json").write_text(content, encoding="utf-8")
    code, _, err = _run(capsys, "diff", "s")
    assert code == 4 and problem in err and "Traceback" not in err


def test_missing_snapshot_is_a_usage_error(linux_pc, capsys):
    code, _, err = _run(capsys, "diff", "nope")
    assert code == 4 and "no snapshot named 'nope'" in err


def test_a_device_answering_with_another_mac_is_a_change(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["neigh"] = [Neighbor("192.0.2.1", "00:0e:c6:01:02:03", "reachable")]
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 2
    assert "+ 192.0.2.1 00:0e:c6:01:02:03" in out


def test_japanese_headings(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["hn"] = _linux_host(gateway="192.0.2.254")
    code, out, _ = _run(capsys, "diff", "ok", lang="ja")
    assert code == 2
    assert "■ ルーティング表（IPv4）" in out


def test_save_refuses_and_exits_3_when_nothing_could_be_read(
    linux_pc, capsys, tmp_path
):
    linux_pc["hn"] = HostNetwork(source="linux", errors=["ip: command failed"])
    code, out, err = _run(capsys, "save", "empty")
    assert code == 3
    assert "nothing was saved" in err and "Traceback" not in err
    assert not (tmp_path / "empty.json").exists()
