"""netdiag 0.6.0 runs on Linux and macOS only. On Windows it says so and exits 4."""

import pytest

from netdiag.cli import main

SENTENCE = (
    "Error: netdiag 0.6.0 and later do not run on Windows. On Windows, use 0.5.0: "
    'pip install "network-check-tool==0.5.0"'
)


@pytest.mark.parametrize("args", [["check"], ["--version"], ["--help"], []])
def test_windows_exits_4_with_one_sentence_and_measures_nothing(
    monkeypatch, capsys, args
):
    measured = []
    monkeypatch.setattr("netdiag.runner.run_check", lambda *a, **k: measured.append(a))
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(SystemExit) as exc:
        main(args, prog_name="netdiag")
    out = capsys.readouterr()
    assert exc.value.code == 4
    assert out.err.strip() == SENTENCE
    assert out.out == ""
    assert measured == []


def test_windows_sentence_in_japanese(monkeypatch, capsys, ja):
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(SystemExit) as exc:
        main(["check"], prog_name="netdiag")
    assert exc.value.code == 4
    assert capsys.readouterr().err.strip() == (
        "エラー: netdiag 0.6.0 以降は Windows では動きません。Windows では 0.5.0 "
        'を使ってください: pip install "network-check-tool==0.5.0"'
    )
