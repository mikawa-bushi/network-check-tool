"""The gates that guard CI and pushes: scripts/expect_verdict.sh and the pre-push checks."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="on Windows a bare `bash` may start WSL instead of Git Bash",
)


def _expect_verdict(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "expect_verdict.sh"),
            sys.executable,
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
    )


@needs_bash
@pytest.mark.parametrize("rc", [0, 1, 2, 3])
def test_expect_verdict_passes_every_verdict_exit_code(rc):
    assert _expect_verdict(f"import sys; sys.exit({rc})").returncode == 0


@needs_bash
@pytest.mark.parametrize("rc", [4, 5, 130])
def test_expect_verdict_fails_usage_internal_and_interrupt_exit_codes(rc):
    run = _expect_verdict(f"import sys; sys.exit({rc})")
    assert run.returncode != 0
    assert f"exit code {rc} is not a verdict" in run.stderr


@needs_bash
def test_expect_verdict_fails_an_unhandled_exception_that_exits_1():
    run = _expect_verdict("raise RuntimeError('probe crashed')")
    assert run.returncode != 0
    assert "netdiag crashed" in run.stderr


@needs_bash
def test_expect_verdict_fails_a_traceback_even_when_the_exit_code_is_0():
    code = (
        "import threading\n"
        "t = threading.Thread(target=lambda: 1 / 0)\n"
        "t.start(); t.join()\n"
    )
    run = _expect_verdict(code)
    assert run.returncode != 0
    assert "exit code: 0" in run.stderr and "netdiag crashed" in run.stderr
