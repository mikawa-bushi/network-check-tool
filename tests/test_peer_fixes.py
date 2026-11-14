"""``netdiag peer``: a safe responder and a client that counts what really came back.

Everything runs on 127.0.0.1 with free ports.
"""

import socket
import threading
import time

import pytest

from netdiag.cli import main
from netdiag.peer import PeerServer, measure_loss_jitter, measure_throughput
from netdiag.peer.server import REPLY, REQUEST, expected_reply, reply_for


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    started = []

    def start(**kw):
        srv = PeerServer("127.0.0.1", _free_port(), **kw)
        srv.bind()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.stop()


def _udp_client():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.3)
    return s


def _request(seq=1, token=b"\x00" * 8):
    return REQUEST + seq.to_bytes(4, "big") + token


def _recv_or_none(sock):
    try:
        return sock.recv(2048)
    except socket.timeout:
        return None


def test_server_answers_only_well_formed_requests(server):
    srv = server()
    c = _udp_client()
    for junk in (b"hello", b"0:1731500000.0", REPLY + b"\x00" * 12, _request() + b"x"):
        c.sendto(junk, ("127.0.0.1", srv.port))
        assert _recv_or_none(c) is None, junk
    request = _request(1026, b"ATTACKER")
    c.sendto(request, ("127.0.0.1", srv.port))
    reply = c.recv(2048)
    assert len(reply) == len(request) and reply.startswith(REPLY)
    assert request[len(REQUEST) :] not in reply and b"ATTACK" not in reply


def test_two_servers_cannot_loop(server):
    a, b = server(), server()
    sock_b = b._udp
    sock_b.sendto(_request(), ("127.0.0.1", a.port))
    time.sleep(0.5)
    assert a.replies_sent == 1 and b.replies_sent == 0


def test_server_never_answers_a_privileged_or_its_own_port():
    assert reply_for(_request(), ("192.0.2.5", 502), own_port=47600) is None
    assert reply_for(_request(), ("192.0.2.5", 47600), own_port=47600) is None
    assert reply_for(_request(), ("192.0.2.5", 50000), own_port=47600) is not None


def test_server_sends_at_most_its_rate_in_any_second(server):
    srv = server(reply_rate=20)
    c = _udp_client()
    for _ in range(2):
        for seq in range(100):
            c.sendto(_request(seq), ("127.0.0.1", srv.port))
        time.sleep(0.4)
    got = 0
    while _recv_or_none(c) is not None:
        got += 1
    assert 10 <= got <= 20


def test_udp_echo_survives_a_connection_reset(server):
    srv = server()
    real = srv._udp

    class _ResetOnce:
        def __init__(self):
            self.raised = False

        def __getattr__(self, name):
            return getattr(real, name)

        def recvfrom(self, n):
            if not self.raised:
                self.raised = True
                raise ConnectionResetError(10054, "reset by peer")
            return real.recvfrom(n)

    srv._udp = _ResetOnce()
    time.sleep(0.6)
    rep = measure_loss_jitter("127.0.0.1", srv.port, count=5, interval=0.0)
    assert rep.received == 5


def test_tcp_sink_keeps_accepting_after_a_flood_of_idle_connections(server):
    srv = server(max_connections=2)
    idle = [socket.create_connection(("127.0.0.1", srv.port)) for _ in range(6)]
    for s in idle:
        s.close()
    time.sleep(0.3)
    assert measure_throughput("127.0.0.1", srv.port, seconds=0.2) is not None


def _fake_echo(behaviour):
    """A UDP responder that answers netdiag requests the way *behaviour* says."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    stop = threading.Event()

    def loop():
        sock.settimeout(0.1)
        n = 0
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            behaviour(sock, data, addr, n)
            n += 1

    threading.Thread(target=loop, daemon=True).start()
    return sock.getsockname()[1], stop


def test_one_late_reply_does_not_make_later_probes_count_as_lost():
    first = []

    def late_first(sock, data, addr, n):
        if n == 0:
            first.append(data)
            return
        if n == 3:  # probe 0 timed out long ago; its reply lands just before 3's
            sock.sendto(expected_reply(first[0]), addr)
        sock.sendto(expected_reply(data), addr)

    port, stop = _fake_echo(late_first)
    rep = measure_loss_jitter("127.0.0.1", port, count=10, interval=0.0, timeout=0.2)
    stop.set()
    assert rep.received == 9


def test_duplicated_replies_do_not_make_later_probes_count_as_lost():
    def twice(sock, data, addr, n):
        sock.sendto(expected_reply(data), addr)
        sock.sendto(expected_reply(data), addr)

    port, stop = _fake_echo(twice)
    rep = measure_loss_jitter("127.0.0.1", port, count=10, interval=0.0, timeout=0.2)
    stop.set()
    assert rep.received == 10


def test_client_still_works_against_a_0_5_echo_server():
    port, stop = _fake_echo(lambda sock, data, addr, n: sock.sendto(data, addr))
    rep = measure_loss_jitter("127.0.0.1", port, count=5, interval=0.0, timeout=0.2)
    stop.set()
    assert rep.received == 5


def _peer_test(capsys, *args):
    with pytest.raises(SystemExit) as exc:
        main(["--lang", "en", "peer", "test", *map(str, args)], prog_name="netdiag")
    out = capsys.readouterr()
    return exc.value.code, out.out + out.err


def test_no_responder_listening_is_named_and_exits_3(capsys):
    code, out = _peer_test(capsys, "127.0.0.1", "--port", _free_port(), "--count", 3)
    assert code == 3
    assert "no netdiag peer responder" in out and "loss" not in out


def test_silent_peer_is_not_reported_as_a_lossy_lan(capsys):
    port, stop = _fake_echo(lambda *a: None)
    code, out = _peer_test(capsys, "127.0.0.1", "--port", port, "--count", 2)
    stop.set()
    assert code == 3
    assert "no reply" in out and "firewall" in out and "loss" not in out


def test_peer_test_against_a_running_server_exits_0(server, capsys, monkeypatch):
    import netdiag.peer as peer_mod

    real = peer_mod.measure_throughput
    monkeypatch.setattr(
        peer_mod, "measure_throughput", lambda host, port: real(host, port, seconds=0.3)
    )
    srv = server()
    code, out = _peer_test(capsys, "127.0.0.1", "--port", srv.port, "--count", 5)
    assert code == 0, out
    assert "loss: 0.0%" in out and "throughput:" in out


@pytest.mark.parametrize(
    "args", [["--count", "0"], ["--count", "-5"], ["--port", "70000"]]
)
def test_peer_test_rejects_bad_counts_and_ports(capsys, args):
    code, out = _peer_test(capsys, "127.0.0.1", *args)
    assert code == 4 and "Traceback" not in out


def test_serve_does_not_claim_to_listen_when_the_port_is_taken(capsys):
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    port = taken.getsockname()[1]
    try:
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--lang",
                    "en",
                    "peer",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                prog_name="netdiag",
            )
    finally:
        taken.close()
    out = capsys.readouterr()
    assert exc.value.code == 4
    assert "listening" not in out.out
    assert f"cannot listen on 127.0.0.1:{port}" in out.err
