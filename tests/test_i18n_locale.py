"""The display language when the environment names none: Japanese, unless the
OS locale says English."""

import os
import subprocess
import sys

import pytest

from netdiag import i18n


@pytest.mark.parametrize(
    "raw, parsed, expected",
    [
        ("C.UTF-8", ("en_US", "UTF-8"), "ja"),  # Python 3.11's view of coerced C
        ("C", (None, None), "ja"),
        ("POSIX", (None, None), "ja"),
        ("Japanese_Japan.932", ("Japanese_Japan", "932"), "ja"),
        ("English_United States.1252", ("English_United States", "1252"), "en"),
        ("en_US.UTF-8", ("en_US", "UTF-8"), "en"),
    ],
)
def test_os_locale_decides_only_when_it_names_a_language(
    monkeypatch, raw, parsed, expected
):
    monkeypatch.setattr(i18n.locale, "setlocale", lambda *a: raw)
    monkeypatch.setattr(i18n.locale, "getlocale", lambda *a: parsed)
    assert i18n.detect({}) == expected


@pytest.mark.skipif(os.name == "nt", reason="the C locale is a POSIX setting")
@pytest.mark.parametrize("lang", ["C", "POSIX", "C.UTF-8", None])
def test_c_locale_gives_japanese(lang):
    env = {"PATH": os.environ.get("PATH", "")}
    if lang is not None:
        env["LANG"] = lang
    out = subprocess.run(
        [sys.executable, "-c", "from netdiag.i18n import detect; print(detect())"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "ja"
