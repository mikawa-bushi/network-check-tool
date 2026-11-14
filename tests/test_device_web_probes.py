"""TCP, HTTP and TLS checks against real local servers.

Each test is a case where the check used to report something other than
what the server did: a plain-HTTP server failing TLS, a Japanese URL path
crashing into "no problems", a redirect to a 404 shown as OK, an IPv4-only
site failing on its IPv6 address, a hidden check, or a dropped target.
"""

import http.server
import shutil
import socket
import ssl
import subprocess
import threading
from contextlib import contextmanager

import pytest

from netdiag import netinfo
from netdiag.cli import main
from netdiag.core import Status
from netdiag.core.result import CheckResult
from netdiag.diagnosis import Engine
from netdiag.diagnosis.rules import advice_for
from netdiag.measure.tcp import PortCheck
from netdiag.probes import dns as dns_probe
from netdiag.probes import tcp as tcp_probe
from netdiag.runner import run_check


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802 - http.server's naming
        self.seen.append(self.path)
        if self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "/missing")
        elif self.path == "/missing":
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@contextmanager
def _server(tls_context=None, address="127.0.0.1"):
    _Handler.seen = []
    srv = http.server.ThreadingHTTPServer((address, 0), _Handler)
    if tls_context is not None:
        srv.socket = tls_context.wrap_socket(srv.socket, server_side=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(only, **config):
    diag = Engine(config=config).run(only=only)
    return diag, diag.results_by_name()


def test_a_plain_http_url_gives_tls_nothing_to_check(monkeypatch, capsys):
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    with _server() as port:
        url = f"http://127.0.0.1:{port}/"
        diag, res = _run(["http", "tls"], http_url=url, tls_target=url)
        assert (res["http"].status, res["tls"].status) == (Status.OK, Status.SKIP)
        assert diag.status == Status.OK
        with pytest.raises(SystemExit) as exc:
            main(["check", "--only", "http,tls", "--http-url", url], "netdiag")
    assert exc.value.code == 0, capsys.readouterr().out


def test_a_japanese_url_path_is_fetched_not_crashed():
    with _server() as port:
        _diag, res = _run(["http"], http_url=f"http://127.0.0.1:{port}/画面?名前=1")
    assert res["http"].code == "http.ok"
    assert _Handler.seen == ["/%E7%94%BB%E9%9D%A2?%E5%90%8D%E5%89%8D=1"]


def test_a_redirect_is_followed_to_its_real_answer():
    with _server() as port:
        _diag, res = _run(["http"], http_url=f"http://127.0.0.1:{port}/old")
    assert res["http"].status == Status.WARN
    assert res["http"].code == "http.client_error"
    assert "/old → " in res["http"].summary and "HTTP 404" in res["http"].summary


def test_a_refused_connection_is_not_explained_as_a_reachable_server():
    url = f"http://127.0.0.1:{_closed_port()}/"
    diag, res = _run(["http"], http_url=url)
    assert res["http"].code == "http.connect_failed"
    means, _steps = advice_for(res["http"], res)
    assert "does not accept the TCP connection" in means
    assert "reaches the server" not in diag.primary.summary


def test_http_tries_the_next_address_when_the_first_one_fails(monkeypatch):
    try:
        server = _server(address="127.0.0.2")
        port = server.__enter__()
    except OSError:
        pytest.skip("needs a second loopback address")
    try:
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in ("127.0.0.1", "127.0.0.2")  # nothing listens on the first
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        _diag, res = _run(["http"], http_url=f"http://dual.example:{port}/")
    finally:
        server.__exit__(None, None, None)
    assert res["http"].code == "http.ok"
    assert res["http"].detail["address"] == "127.0.0.2"


def test_checks_on_other_targets_are_not_hidden_by_a_failure(monkeypatch):
    monkeypatch.setattr(
        dns_probe.DnsProbe,
        "check",
        lambda self, ctx: CheckResult.fail("dns", "no resolver", code="dns.failed"),
    )
    _diag, res = _run(
        ["dns", "tcp"], tcp_targets=[f"127.0.0.1:{_closed_port()}"], dns_name="x"
    )
    assert res["tcp"].code == "tcp.failed"  # measured, not "upstream dns failed"

    with _server() as port:
        _diag, res = _run(
            ["tcp", "http", "tls"],
            tcp_targets=[f"127.0.0.1:{_closed_port()}"],
            http_url=f"http://127.0.0.1:{port}/",
        )
    assert res["tcp"].status == Status.FAIL
    assert res["http"].code == "http.ok"


@pytest.mark.parametrize(
    "spec, message",
    [
        ("127.0.0.1:modbis", "--tcp 127.0.0.1:modbis: unknown port or protocol"),
        ("127.0.0.1", "--tcp 127.0.0.1: write host:port"),
        ("127.0.0.1:99999", "--tcp 127.0.0.1:99999: port out of range (1-65535)"),
    ],
)
def test_a_bad_tcp_target_exits_4_and_names_it(capsys, spec, message):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--tcp", "127.0.0.1:22", "--tcp", spec], "netdiag")
    assert exc.value.code == 4
    assert message in capsys.readouterr().err


def test_tcp_targets_are_normalised_and_protocol_names_read(monkeypatch):
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    asked = []

    def fake_check(pairs, **kw):
        asked.extend(pairs)
        return {
            (h, p): PortCheck(host=h, port=p, attempts=2, state="open", times_ms=[1.0])
            for h, p in pairs
        }

    monkeypatch.setattr(tcp_probe, "tcp_check_many", fake_check)
    rep = run_check(
        config={"tcp_targets": ["192.168.001.010:modbus,4840"]}, only=["tcp"]
    )
    assert asked == [("192.168.1.10", 502), ("192.168.1.10", 4840)]
    assert rep.host.results_by_name()["tcp"].summary.startswith("192.168.1.10:502 OK")


# ------------------------------------------------------------------ TLS dates


def _openssl(cwd, *args):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A test CA and two leaf certs for 127.0.0.1, one expired, one future."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command")
    d = tmp_path_factory.mktemp("tls")
    key = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    _openssl(
        d, "req", "-x509", *key, "-keyout", "ca.key", "-out", "ca.crt", "-subj",
        "/CN=netdiag test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign",
    )  # fmt: skip
    (d / "ca.cnf").write_text(
        "[ca]\ndefault_ca=d\n[d]\ndatabase=index.txt\nnew_certs_dir=.\n"
        "serial=serial\ndefault_md=sha256\npolicy=p\nunique_subject=no\n"
        "[p]\ncommonName=supplied\n"
    )
    (d / "san.ext").write_text("subjectAltName=IP:127.0.0.1\n")
    (d / "index.txt").write_text("")
    (d / "serial").write_text("01\n")
    for name, start, end in (
        ("expired", "20200101000000Z", "20210101000000Z"),
        ("future", "20900101000000Z", "20991231000000Z"),
    ):
        _openssl(
            d, "req", *key, "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", "/CN=127.0.0.1",
        )  # fmt: skip
        _openssl(
            d, "ca", "-batch", "-config", "ca.cnf", "-cert", "ca.crt", "-keyfile",
            "ca.key", "-in", f"{name}.csr", "-out", f"{name}.crt", "-startdate",
            start, "-enddate", end, "-extfile", "san.ext", "-notext",
        )  # fmt: skip
    return d


@pytest.mark.parametrize(
    "name, code, words",
    [
        ("expired", "tls.expired", "certificate EXPIRED"),
        ("future", "tls.not_yet_valid", "local clock wrong?"),
    ],
)
def test_a_certificate_outside_its_dates_says_which_and_when(
    monkeypatch, certs, name, code, words
):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certs / f"{name}.crt", certs / f"{name}.key")
    default = ssl.create_default_context
    monkeypatch.setattr(
        ssl, "create_default_context", lambda *a, **k: default(cafile=certs / "ca.crt")
    )
    with _server(server) as port:
        _diag, res = _run(["tls"], tls_target=f"https://127.0.0.1:{port}/")
    assert res["tls"].code == code
    assert words in res["tls"].summary
    if name == "expired":
        assert "days ago" in res["tls"].summary
        assert res["tls"].metrics["days_to_expiry"] < -365
