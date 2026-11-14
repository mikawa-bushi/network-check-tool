"""``netdiag scenario``: every check in the file runs, bad files exit 4.

The host network and the probes are fakes; the scenario loader, the runner and
the CLI are real.
"""

import csv
import io
import json
import sys

import pytest

import netdiag.runner  # noqa: F401  (registers every probe before the registry is saved)
from netdiag import netinfo
from netdiag.cli import main
from netdiag.core import CheckResult, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.netinfo.model import HostNetwork


def _probe(pname, status):
    class _P(Probe):
        name = pname
        title = pname
        layer = "L3"
        depends_on = []

        def check(self, ctx):
            if status is None:
                raise RuntimeError("probe crashed")
            target = ctx.get("target", "-")
            return CheckResult(
                name=pname, status=status, summary=f"{pname} {target} {status.value}"
            )

    return _P


@pytest.fixture(autouse=True)
def fake_host(monkeypatch):
    saved = dict(_REGISTRY)
    for name, status in (
        ("echo", Status.OK),
        ("warny", Status.WARN),
        ("faily", Status.FAIL),
        ("crashy", None),
    ):
        _REGISTRY[name] = _probe(name, status)
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: HostNetwork())
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    yield
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _run(capsys, *args):
    with pytest.raises(SystemExit) as exc:
        main(["--lang", "en", "scenario", *map(str, args)], prog_name="netdiag")
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def _write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return p


def test_the_same_check_twice_runs_both_with_their_own_settings(tmp_path, capsys):
    sc = _write(
        tmp_path,
        "two.json",
        json.dumps(
            {
                "checks": [
                    {"probe": "echo", "target": "192.0.2.1"},
                    {"probe": "echo", "target": "192.0.2.2"},
                ]
            }
        ),
    )
    code, out, _ = _run(capsys, sc)
    assert code == 0, out
    assert "echo 192.0.2.1 ok" in out
    assert "echo 192.0.2.2 ok" in out


def test_a_failing_second_check_is_not_hidden_by_a_passing_first(tmp_path, capsys):
    sc = _write(
        tmp_path,
        "two.json",
        json.dumps({"checks": ["echo", {"probe": "faily", "target": "192.0.2.9"}]}),
    )
    code, out, _ = _run(capsys, sc)
    assert code == 1
    assert "faily 192.0.2.9 fail" in out


def test_label_is_shown_as_the_check_title(tmp_path, capsys):
    sc = _write(
        tmp_path,
        "label.json",
        json.dumps({"checks": [{"probe": "echo", "label": "Corporate proxy"}]}),
    )
    code, out, _ = _run(capsys, sc)
    assert code == 0
    assert "Corporate proxy" in out


@pytest.mark.parametrize(
    "probe, exit_code", [("warny", 2), ("faily", 1), ("echo", 0), ("crashy", 3)]
)
def test_host_only_scenario_exits_like_check(tmp_path, capsys, probe, exit_code):
    sc = _write(tmp_path, "s.json", json.dumps({"checks": [probe]}))
    assert _run(capsys, sc)[0] == exit_code


def test_csv_output_is_csv_without_devices(tmp_path, capsys):
    sc = _write(tmp_path, "s.json", json.dumps({"checks": ["echo"]}))
    out_csv = tmp_path / "r.csv"
    assert _run(capsys, sc, "-o", out_csv)[0] == 0
    rows = list(csv.reader(io.StringIO(out_csv.read_text(encoding="utf-8-sig"))))
    assert len(rows) >= 2 and len(rows[0]) > 3
    assert not rows[0][0].startswith("#")


def test_json_output_names_the_scenario(tmp_path, capsys):
    sc = _write(tmp_path, "s.json", json.dumps({"name": "Line 1", "checks": ["echo"]}))
    code, out, _ = _run(capsys, sc, "--format", "json")
    assert code == 0
    assert json.loads(out)["settings"]["scenario"] == "Line 1"


@pytest.mark.parametrize(
    "name, text, problem",
    [
        ("str.json", '{"checks": "echo"}', "'checks' must be a list"),
        (
            "map.json",
            '{"checks": {"echo": {"target": "x"}}}',
            "'checks' must be a list",
        ),
        ("noprobe.json", '{"checks": [{"target": "x"}]}', "check #1: missing 'probe'"),
        ("num.json", '{"checks": [5]}', "check #1"),
        ("empty.json", "", "the file is empty"),
        ("list.json", '["echo"]', "expected a mapping"),
        ("typo.json", '{"check": ["echo"]}', "unknown key: check"),
        ("cfg.json", '{"checks": ["echo"], "config": [1]}', "'config' must be"),
        ("bad.json", '{"checks": [', "bad.json"),
        ("unknown.json", '{"checks": ["ech"]}', "unknown check name"),
        (
            "port.json",
            '{"devices": [{"host": "192.0.2.5", "ports": 70000}]}',
            "port out of range",
        ),
        ("noext", '{"checks": ["echo"]}', ".yaml, .yml, .json or .toml"),
    ],
)
def test_bad_scenario_files_exit_4_naming_file_and_problem(
    tmp_path, capsys, name, text, problem
):
    sc = _write(tmp_path, name, text)
    code, out, err = _run(capsys, sc)
    assert code == 4, out + err
    assert "Traceback" not in err
    assert name in err and problem in err
    assert len(err.strip().splitlines()) <= 6


def test_a_directory_is_a_usage_error(tmp_path, capsys):
    code, _, err = _run(capsys, tmp_path)
    assert code == 4 and "Traceback" not in err


def test_uppercase_yaml_suffix_and_shift_jis_text_load(tmp_path, capsys):
    pytest.importorskip("yaml")
    sc = _write(
        tmp_path,
        "LINE1.YAML",
        "name: 1ライン点検\nchecks:\n  - probe: echo\n    label: 応答確認\n",
        encoding="cp932",
    )
    code, out, err = _run(capsys, sc)
    assert code == 0, err
    assert "1ライン点検" in out and "応答確認" in out


def test_toml_scenario_loads(tmp_path, capsys):
    sc = _write(
        tmp_path,
        "s.toml",
        'name = "toml line"\n'
        "[[checks]]\n"
        'probe = "echo"\n'
        'target = "192.0.2.7"\n',
    )
    code, out, err = _run(capsys, sc)
    assert code == 0, err
    assert "echo 192.0.2.7 ok" in out


def test_yaml_keeps_zero_padded_names_as_written():
    pytest.importorskip("yaml")
    from netdiag.scenario import parse_scenario
    from netdiag.scenario.loader import _load_yaml

    raw = _load_yaml("devices:\n  - {name: 0012, host: 192.0.2.12, ports: 0502}\n", "s")
    sc = parse_scenario(raw, "s.yaml")
    assert sc.devices[0].name == "0012"
    assert sc.devices[0].ports == [502]


def test_yaml_without_pyyaml_is_a_usage_error(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    sc = _write(tmp_path, "s.yaml", "checks: [echo]\n")
    code, _, err = _run(capsys, sc)
    assert code == 4
    assert "PyYAML" in err and "s.yaml" in err and "Traceback" not in err


def test_japanese_message_for_a_bad_file(tmp_path, capsys):
    sc = _write(tmp_path, "s.json", '{"checks": "echo"}')
    with pytest.raises(SystemExit) as exc:
        main(["--lang", "ja", "scenario", str(sc)], prog_name="netdiag")
    err = capsys.readouterr().err
    assert exc.value.code == 4
    assert "シナリオ" in err and "s.json" in err
