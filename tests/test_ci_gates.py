"""The gates that guard CI and pushes: scripts/expect_verdict.sh and the pre-push checks."""

import os
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


OK_ID = ("hardwork9047", "155122235+hardwork9047@users.noreply.github.com")
CORP_ID = ("Real Name", "real.name@corp.example")
IDENTITY_GATE = ["scripts/check_commit_identity.py"]
TRACES_GATE = ["scripts/check_ai_traces.py", "--pre-push"]


class Repo:
    """A work repo with two bare remotes: origin (a private archive) and public."""

    def __init__(self, tmp_path: Path):
        self.work = tmp_path / "work"
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text("")
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PRE_COMMIT", "GIT_"))
        }
        self.env.update(
            HOME=str(tmp_path),
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            PYTHONIOENCODING="utf-8",
        )
        self.git("init", "-q", "-b", "main", str(self.work), cwd=tmp_path)
        for remote in ("origin", "public"):
            bare = tmp_path / f"{remote}.git"
            self.git("init", "-q", "--bare", str(bare), cwd=tmp_path)
            self.git("remote", "add", remote, str(bare))
        (self.work / "scripts").mkdir()
        for gate in ("check_commit_identity.py", "check_ai_traces.py"):
            shutil.copy(ROOT / "scripts" / gate, self.work / "scripts" / gate)
        self.commit("init", OK_ID)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message: str, who: tuple, path: str | None = None) -> str:
        if path:
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            (self.work / path).write_text(message + "\n", encoding="utf-8")
        ident = ("-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}")
        self.git(*ident, "add", "-A")
        self.git(*ident, "commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def gate(self, args: list, **push_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.work,
            env={**self.env, **push_env},
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def new_branch_push_env(self, branch: str, remote: str = "origin") -> dict:
        """What pre-commit exports for `git push <remote> <branch>` of a new branch."""
        tip = self.git("rev-parse", branch)
        first_new = self.git(
            "rev-list", "--topo-order", "--reverse", tip, "--not", f"--remotes={remote}"
        ).split()[0]
        return {
            "PRE_COMMIT_FROM_REF": self.git("rev-parse", f"{first_new}^"),
            "PRE_COMMIT_TO_REF": tip,
            "PRE_COMMIT_LOCAL_BRANCH": f"refs/heads/{branch}",
            "PRE_COMMIT_REMOTE_BRANCH": f"refs/heads/{branch}",
            "PRE_COMMIT_REMOTE_NAME": remote,
        }


def test_push_gates_check_the_pushed_branch_not_the_checked_out_one(tmp_path):
    repo = Repo(tmp_path)
    repo.git("push", "-q", "origin", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("wip", CORP_ID)
    repo.commit("notes", OK_ID, path=".demotool/cfg")
    repo.git("switch", "-q", "main")
    push = repo.new_branch_push_env("feature")

    identity = repo.gate(IDENTITY_GATE, **push)
    assert identity.returncode == 1
    assert "real.name@corp.example" in identity.stderr
    traces = repo.gate(TRACES_GATE, MONITOR_AI_TRACE_EXTRA="demotool", **push)
    assert traces.returncode == 1
    assert ".demotool/cfg" in traces.stderr


def test_push_gates_check_history_that_is_only_on_another_remote(tmp_path):
    repo = Repo(tmp_path)
    repo.commit("old identity", CORP_ID)
    repo.git("push", "-q", "origin", "main")
    push_to_empty_public = {
        "PRE_COMMIT_LOCAL_BRANCH": "refs/heads/main",
        "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/main",
        "PRE_COMMIT_REMOTE_NAME": "public",
    }

    identity = repo.gate(IDENTITY_GATE, **push_to_empty_public)
    assert identity.returncode == 1
    assert "real.name@corp.example" in identity.stderr


def test_push_gates_pass_a_clean_push_of_another_branch(tmp_path):
    repo = Repo(tmp_path)
    repo.git("push", "-q", "origin", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("fix", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    repo.commit("local only", CORP_ID)
    push = repo.new_branch_push_env("feature")

    assert repo.gate(IDENTITY_GATE, **push).returncode == 0
    assert repo.gate(TRACES_GATE, **push).returncode == 0


def test_push_gates_run_by_hand_check_head_against_origin(tmp_path):
    repo = Repo(tmp_path)
    repo.commit("wip", CORP_ID)
    assert repo.gate(IDENTITY_GATE).returncode == 1
    repo.git("push", "-q", "origin", "main")
    assert repo.gate(IDENTITY_GATE).returncode == 0
    assert repo.gate(TRACES_GATE).returncode == 0


PRE_PUSH_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: check-commit-identity
        name: identity
        entry: {python} scripts/check_commit_identity.py
        language: system
        stages: [pre-push]
        pass_filenames: false
        always_run: true
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the hook entry is POSIX")
def test_real_pre_commit_blocks_a_push_of_another_branch(tmp_path):
    pytest.importorskip("pre_commit")
    repo = Repo(tmp_path)
    (repo.work / ".pre-commit-config.yaml").write_text(
        PRE_PUSH_CONFIG.format(python=sys.executable), encoding="utf-8"
    )
    repo.commit("hooks", OK_ID)
    repo.env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-home")
    subprocess.run(
        [sys.executable, "-m", "pre_commit", "install", "-t", "pre-push"],
        cwd=repo.work,
        env=repo.env,
        check=True,
        capture_output=True,
    )
    repo.git("push", "-q", "origin", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("wip", CORP_ID)
    repo.git("switch", "-q", "main")

    push = subprocess.run(
        ["git", "push", "origin", "feature"],
        cwd=repo.work,
        env=repo.env,
        capture_output=True,
        text=True,
    )
    assert push.returncode != 0
    assert "real.name@corp.example" in push.stdout + push.stderr
    assert repo.git("ls-remote", "origin", "refs/heads/feature") == ""
