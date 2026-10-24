"""Parsers for traceroute and ping output (no real network / subprocess).

The ping fixtures are verbatim formats of iputils (Linux, C locale), macOS,
BusyBox and Windows ``ping.exe`` in **Japanese and English** — the parser must
count replies the same way in every language, and must not trust Windows' own
statistics, which count "destination host unreachable" replies as received.
"""

from netdiag.core.util import CommandResult
from netdiag.measure import icmp as pingmod
from netdiag.measure.icmp import (
    build_ping_args,
    parse_posix_ping,
    parse_windows_ping,
    system_ping,
)
from netdiag.probes import icmp
from netdiag.probes.traceroute import _parse_hops

# ---------------------------------------------------------------- traceroute


def test_parse_hops_basic():
    text = "\n".join(
        [
            " 1  10.0.0.1  1.234 ms  1.111 ms",
            " 2  * * *",
            " 3  203.0.113.5  10.5 ms",
        ]
    )
    hops = _parse_hops(text)

    assert hops == [
        {"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.17, "timeout": False},
        {"hop": 2, "ip": None, "rtt_ms": None, "timeout": True},
        {"hop": 3, "ip": "203.0.113.5", "rtt_ms": 10.5, "timeout": False},
    ]


def test_parse_hops_upgrades_timed_out_hop_to_resolved():
    # Same hop number appears twice: first a timeout, then a resolved line.
    # The resolved entry must replace the timed-out one.
    text = "\n".join(
        [
            " 4  *",
            " 4  198.51.100.7  20.0 ms",
        ]
    )
    hops = _parse_hops(text)

    assert hops == [{"hop": 4, "ip": "198.51.100.7", "rtt_ms": 20.0, "timeout": False}]


# --------------------------------------------------------------- posix ping

IPUTILS_OK = """\
PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.456 ms
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.789 ms (DUP!)
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=0.512 ms
From 192.168.1.10 icmp_seq=3 Destination Host Unreachable

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 2 received, +1 duplicates, +1 errors, 33.3333% packet loss, time 2003ms
rtt min/avg/max/mdev = 0.456/0.584/0.789/0.166 ms
"""

IPUTILS_DOWN = """\
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2000ms
"""

MACOS = """\
PING 192.168.1.1 (192.168.1.1): 56 data bytes
64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=2.000 ms
Request timeout for icmp_seq 1
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=3.000 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 2.000/2.500/3.000/0.500 ms
"""

BUSYBOX = """\
PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: seq=0 ttl=117 time=10.123 ms
64 bytes from 8.8.8.8: seq=0 ttl=117 time=10.456 ms (DUP!)

--- 8.8.8.8 ping statistics ---
2 packets transmitted, 1 packets received, 1 duplicates, 50% packet loss
round-trip min/avg/max = 10.123/10.289/10.456 ms
"""


def test_iputils_counts_replies_dups_and_errors():
    sent, received, dups, rtts, errors = parse_posix_ping(IPUTILS_OK, count=3)
    assert (sent, received, dups) == (3, 2, 1)
    assert rtts == [0.456, 0.512]  # the DUP reply is not a second sample
    assert errors == {"Destination Host Unreachable": 1}


def test_iputils_total_loss():
    sent, received, dups, rtts, _ = parse_posix_ping(IPUTILS_DOWN, count=3)
    assert (sent, received, dups, rtts) == (3, 0, 0, [])


def test_macos_summary_wins_over_timeout_lines():
    sent, received, _dups, rtts, _ = parse_posix_ping(MACOS, count=3)
    assert (sent, received) == (3, 2)
    assert rtts == [2.0, 3.0]


def test_busybox_dup_line_counted_even_without_plus_sign():
    sent, received, dups, rtts, _ = parse_posix_ping(BUSYBOX, count=2)
    assert (sent, received, dups) == (2, 1, 1)
    assert rtts == [10.123]


# ------------------------------------------------------------- windows ping

WIN_JA = """
192.168.2.102 に ping を送信しています 32 バイトのデータ:
192.168.2.102 からの応答: バイト数 =32 時間 =4ms TTL=64
192.168.2.102 からの応答: バイト数 =32 時間 <1ms TTL=64
要求がタイムアウトしました。
192.168.2.101 からの応答: 宛先ホストに到達できません。

192.168.2.102 の ping 統計:
    パケット数: 送信 = 4、受信 = 3、損失 = 1 (25% の損失)、
ラウンド トリップの概算時間 (ミリ秒):
    最小 = 0ms、最大 = 4ms、平均 = 2ms
"""

WIN_JA_UNREACHABLE_ONLY = """
192.168.2.200 に ping を送信しています 32 バイトのデータ:
192.168.2.101 からの応答: 宛先ホストに到達できません。
192.168.2.101 からの応答: 宛先ホストに到達できません。
192.168.2.101 からの応答: 宛先ホストに到達できません。
192.168.2.101 からの応答: 宛先ホストに到達できません。

192.168.2.200 の ping 統計:
    パケット数: 送信 = 4、受信 = 4、損失 = 0 (0% の損失)、
"""

WIN_EN = """
Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time=3ms TTL=64
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Request timed out.
Reply from 179.60.213.149: TTL expired in transit.

Ping statistics for 192.168.1.1:
    Packets: Sent = 4, Received = 3, Lost = 1 (25% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 3ms, Average = 1ms
"""

WIN_IPV6 = """
Pinging google.com [2404:6800:4006:810::200e] with 32 bytes of data:
Reply from 2404:6800:4006:810::200e: time=1ms
Reply from 2404:6800:4006:810::200e: time<1ms

Ping statistics for 2404:6800:4006:810::200e:
    Packets: Sent = 2, Received = 2, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 1ms, Average = 0ms
"""

WIN_JA_UNKNOWN_HOST = "ping 要求ではホスト plc-x が見つかりませんでした。ホスト名を確認してもう一度実行してください。\n"


def test_windows_japanese_replies_and_losses():
    sent, received, _dups, rtts, errors = parse_windows_ping(WIN_JA, count=4)
    assert (sent, received) == (4, 2)
    assert rtts == [4.0, 0.5]  # "<1ms" -> 0.5 ms (1 ms resolution)
    assert "要求がタイムアウトしました。" in errors
    assert any("宛先ホストに到達できません" in e for e in errors)


def test_windows_unreachable_replies_are_losses_not_success():
    # ping.exe itself reports "受信 = 4、損失 = 0 (0% の損失)" here.
    sent, received, _dups, rtts, _ = parse_windows_ping(
        WIN_JA_UNREACHABLE_ONLY, count=4
    )
    assert (sent, received, rtts) == (4, 0, [])


def test_windows_english_same_result_as_japanese():
    sent, received, _dups, rtts, errors = parse_windows_ping(WIN_EN, count=4)
    assert (sent, received) == (4, 2)
    assert rtts == [3.0, 0.5]
    assert "Request timed out." in errors


def test_windows_ipv6_replies_without_ttl():
    sent, received, _dups, rtts, _ = parse_windows_ping(WIN_IPV6, count=2)
    assert (sent, received, rtts) == (2, 2, [1.0, 0.5])


def test_windows_unknown_host_is_a_single_error_line():
    sent, received, _dups, rtts, errors = parse_windows_ping(
        WIN_JA_UNKNOWN_HOST, count=4
    )
    assert (sent, received, rtts) == (4, 0, [])
    assert len(errors) == 1


# ------------------------------------------------------------- system_ping


def _canned(monkeypatch, stdout, windows=False):
    monkeypatch.setattr(pingmod, "is_windows", lambda: windows)
    monkeypatch.setattr(pingmod, "have", lambda cmd: True)
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        return CommandResult(ok=True, code=0, stdout=stdout, stderr="", found=True)

    monkeypatch.setattr(pingmod, "run", fake_run)
    return calls


def test_system_ping_success_statistics(monkeypatch):
    _canned(monkeypatch, IPUTILS_OK)
    st = system_ping("192.168.1.1", count=3)
    assert st.method == "system-ping"
    assert (st.sent, st.received, st.duplicates) == (3, 2, 1)
    assert st.error is None
    assert round(st.loss_pct, 1) == 33.3
    assert st.rtt.median == 0.484


def test_system_ping_windows_japanese(monkeypatch):
    _canned(monkeypatch, WIN_JA, windows=True)
    st = system_ping("192.168.2.102", count=4)
    assert (st.sent, st.received) == (4, 2)
    assert st.resolution_ms == 1.0


def test_system_ping_permission_error_is_not_a_down_device(monkeypatch):
    _canned(monkeypatch, "ping: permission denied (are you root?)\n")
    st = system_ping("192.168.1.1", count=3)
    assert st.error and "permission denied" in st.error
    assert st.received == 0


def test_system_ping_unknown_host_on_windows_is_an_error(monkeypatch):
    _canned(monkeypatch, WIN_JA_UNKNOWN_HOST, windows=True)
    st = system_ping("plc-x", count=4)
    assert st.error and "plc-x" in st.error


def test_build_ping_args_per_platform():
    linux = build_ping_args("h", count=5, timeout=1.0, interval=0.2, os_name="linux")
    assert linux[:3] == ["ping", "-n", "-c"] and "-W" in linux and linux[-1] == "h"
    assert linux[linux.index("-i") + 1] == "0.2"
    win = build_ping_args(
        "h", count=5, timeout=1.0, interval=0.2, family=4, os_name="windows"
    )
    assert win == ["ping", "-4", "-n", "5", "-w", "1000", "h"]
    mac = build_ping_args("h", count=5, timeout=1.0, interval=0.05, os_name="macos")
    assert mac[mac.index("-i") + 1] == "0.1"  # non-root floor on macOS
    assert mac[mac.index("-W") + 1] == "1000"  # milliseconds on macOS


def test_icmp_ping_compat_wrapper(monkeypatch):
    # The legacy (avg_rtt, loss_pct, raw) API is kept for external callers.
    _canned(monkeypatch, IPUTILS_DOWN)
    monkeypatch.setenv("NETDIAG_PING_METHOD", "system")
    rtt, loss, raw = icmp.ping("8.8.8.8", count=3, timeout=2.0)
    assert rtt is None
    assert loss == 100.0
    assert raw == IPUTILS_DOWN
