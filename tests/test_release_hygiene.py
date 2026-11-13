"""What a release ships: the license, the PyPI README, the version in reports."""

import json
import re
import zipfile
from pathlib import Path

import tomllib
from click.testing import CliRunner

import netdiag
from netdiag import collect as collect_mod
from netdiag import netinfo
from netdiag import runner as runner_mod
from netdiag.cli import cli
from netdiag.collect import Captured, run_collect
from netdiag.diagnosis import Diagnosis
from netdiag.inventory import Device
from netdiag.netinfo.model import HostNetwork
from netdiag.report import render_check_html
from netdiag.report.soak import render_soak_html
from netdiag.runner import CheckReport
from netdiag.soak import DeviceTrack, SoakReport

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_760_000_040.0


def _package_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["tool"]["poetry"]["version"]


def test_license_is_the_complete_mit_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2025-2026 hardwork9047" in text
    assert "subject to the following conditions:" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text
    assert text.rstrip().endswith("OTHER DEALINGS IN THE\nSOFTWARE.")


def test_readme_links_are_absolute_so_they_work_on_pypi():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    relative = re.findall(r"\]\((?!https?://|#)([^)]+)\)", readme)
    assert relative == [], "PyPI resolves relative links under pypi.org"


def test_package_version_is_the_pyproject_version():
    assert netdiag.__version__ == _package_version(), (
        "netdiag.__version__ differs from pyproject.toml; "
        "reinstall the package (poetry install) after a version bump"
    )


def test_version_option_prints_the_package_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.output.strip() == f"netdiag, version {_package_version()}"


def test_check_html_report_prints_the_package_version():
    rep = CheckReport(
        host=Diagnosis(results=[], hypotheses=[], healthy=True),
        started_at=T0,
        finished_at=T0 + 3,
        hostname="LINE1-PC",
        platform="test",
    )
    assert f"netdiag {_package_version()}</div>" in render_check_html(rep)


def test_soak_html_report_prints_the_package_version():
    track = DeviceTrack(Device(name="PLC", host="192.0.2.10"), "192.0.2.10", 60.0)
    track.finish()
    rep = SoakReport(
        tracks=[track],
        started_at=T0,
        finished_at=T0 + 60,
        interval=0.5,
        timeout=1.0,
        bucket_s=60.0,
        min_lost=2,
        hostname="LINE1-PC",
    )
    assert f"netdiag {_package_version()}</div>" in render_soak_html(rep)


def test_collect_manifest_records_the_package_version(monkeypatch, tmp_path):
    monkeypatch.setattr(netinfo, "collect", lambda: HostNetwork())
    monkeypatch.setattr(collect_mod, "os_name", lambda: "windows")

    def no_check(*args, **kwargs):
        raise RuntimeError("not measured in this test")

    monkeypatch.setattr(runner_mod, "run_check", no_check)
    out = tmp_path / "c.zip"
    run_collect(
        out,
        mask=False,
        ping=False,
        runner=lambda cmd: Captured(cmd.name, cmd.args, 0, b""),
    )
    manifest = json.loads(
        zipfile.ZipFile(out).read("netdiag-collect/manifest.json").decode("utf-8")
    )
    assert manifest["netdiag_version"] == _package_version()
