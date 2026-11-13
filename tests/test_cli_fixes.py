"""What the command line promises: options, output files, messages, exit codes.

The measurement layer is replaced by a fake; every test drives the real
``netdiag.cli.main`` entry point, the same one the console script and the exe use.
"""

import pytest

import netdiag.runner as runner_mod
from netdiag.cli import main
from netdiag.core import CheckResult, Status
from netdiag.diagnosis import Diagnosis
from netdiag.runner import CheckReport


def _report(status=Status.OK):
    result = CheckResult(
        name="interface", status=status, summary="eth0", code="interface.x"
    )
    healthy = status == Status.OK
    host = Diagnosis(results=[result], hypotheses=[], healthy=healthy)
    return CheckReport(host=host, hostname="pc", platform="test")


@pytest.fixture
def run(monkeypatch, capsys):
    """Run ``netdiag ARGS`` against a fake measurement; return (code, out, err)."""
    seen = {}

    def fake_run_check(devices, config=None, **kw):
        seen.update(devices=devices, config=config, kw=kw)
        return _report(seen.get("status", Status.OK))

    monkeypatch.setattr(runner_mod, "run_check", fake_run_check)

    def invoke(*args):
        with pytest.raises(SystemExit) as exc:
            main(list(args), prog_name="netdiag")
        out, err = capsys.readouterr()
        return exc.value.code, out, err

    invoke.seen = seen
    return invoke


# --------------------------------------------------------------- --http-url


def test_plain_http_url_does_not_ask_for_a_tls_check(run):
    code, _, _ = run("check", "--http-url", "http://192.0.2.10:8080/")
    config = run.seen["config"]
    assert code == 0
    assert config["http_url"] == "http://192.0.2.10:8080/"
    assert "tls_target" not in config


def test_https_url_is_also_the_tls_target(run):
    run("check", "--http-url", "https://192.0.2.10/")
    assert run.seen["config"]["tls_target"] == "https://192.0.2.10/"


def test_url_without_a_scheme_means_plain_http(run):
    run("check", "--http-url", "192.0.2.10:8080/status")
    config = run.seen["config"]
    assert config["http_url"] == "http://192.0.2.10:8080/status"
    assert "tls_target" not in config


@pytest.mark.parametrize("url", ["ftp://192.0.2.10/", "http://", "http://h:99999/"])
def test_unusable_http_url_is_a_usage_error(run, url):
    code, out, err = run("check", "--http-url", url)
    assert code == 4 and "config" not in run.seen
    assert "http:// or https://" in err
