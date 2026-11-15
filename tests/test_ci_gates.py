"""The gates that guard CI and pushes: scripts/expect_verdict.sh and the pre-push checks."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")


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

    def push(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "push", *args],
            cwd=self.work,
            env=self.env,
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


def test_push_gates_block_any_push_while_a_local_branch_has_a_bad_commit(tmp_path):
    repo = Repo(tmp_path)
    repo.git("push", "-q", "origin", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("fix", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    repo.commit("local only", CORP_ID, path=".demotool/cfg")
    push = repo.new_branch_push_env("feature")

    identity = repo.gate(IDENTITY_GATE, **push)
    assert identity.returncode == 1
    assert "real.name@corp.example" in identity.stderr
    traces = repo.gate(TRACES_GATE, MONITOR_AI_TRACE_EXTRA="demotool", **push)
    assert traces.returncode == 1
    assert ".demotool/cfg" in traces.stderr


def test_push_gates_pass_a_clean_push_of_another_branch(tmp_path):
    repo = Repo(tmp_path)
    repo.git("push", "-q", "origin", "main")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("fix", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    repo.commit("local only", OK_ID, path="b.txt")
    push = repo.new_branch_push_env("feature")

    assert repo.gate(IDENTITY_GATE, **push).returncode == 0
    assert repo.gate(TRACES_GATE, **push).returncode == 0


def test_push_gates_run_by_hand_check_every_local_branch_against_origin(tmp_path):
    repo = Repo(tmp_path)
    repo.commit("wip", CORP_ID)
    assert repo.gate(IDENTITY_GATE).returncode == 1
    repo.git("push", "-q", "origin", "main")
    assert repo.gate(IDENTITY_GATE).returncode == 0
    assert repo.gate(TRACES_GATE).returncode == 0
    repo.git("switch", "-q", "-c", "other")
    repo.commit("other", CORP_ID)
    repo.git("switch", "-q", "main")
    assert repo.gate(IDENTITY_GATE).returncode == 1


PRE_PUSH_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: check-commit-identity
        name: identity
        entry: {identity}
        language: system
        stages: [pre-push]
        pass_filenames: false
        always_run: true
      - id: check-ai-traces-commits
        name: traces
        entry: {traces}
        language: system
        stages: [pre-push]
        pass_filenames: false
        always_run: true
"""


@pytest.fixture
def hooked_repo(tmp_path) -> Repo:
    """A repo whose `git push` runs both gates through real pre-commit, main pushed."""
    pytest.importorskip("pre_commit")
    repo = Repo(tmp_path)
    (repo.work / ".pre-commit-config.yaml").write_text(
        PRE_PUSH_CONFIG.format(
            identity=shlex.join([sys.executable, *IDENTITY_GATE]),
            traces=shlex.join([sys.executable, *TRACES_GATE]),
        ),
        encoding="utf-8",
    )
    repo.commit("hooks", OK_ID)
    repo.env["PRE_COMMIT_HOME"] = str(tmp_path / "pre-commit-home")
    repo.env["MONITOR_AI_TRACE_EXTRA"] = "demotool"
    subprocess.run(
        [sys.executable, "-m", "pre_commit", "install", "-t", "pre-push"],
        cwd=repo.work,
        env=repo.env,
        check=True,
        capture_output=True,
    )
    repo.git("push", "-q", "origin", "main")
    return repo


def _bad(repo: Repo, name: str) -> None:
    """A commit both gates must stop: a corporate email and an AI-tool path."""
    repo.commit(name, CORP_ID, path=f".demotool/{name}")


def _other_branch(repo: Repo) -> list:
    repo.git("switch", "-q", "-c", "feature")
    _bad(repo, "wip")
    repo.git("switch", "-q", "main")
    return ["origin", "feature"]


def _bad_branch_checked_out_and_pushed_second(repo: Repo) -> list:
    repo.git("switch", "-q", "-c", "good")
    repo.commit("good", OK_ID, path="good.txt")
    repo.git("switch", "-q", "-c", "bad", "main")
    _bad(repo, "bad")
    return ["origin", "good", "bad"]


def _bad_branch_checked_out_and_pushed_after_main(repo: Repo) -> list:
    repo.commit("more", OK_ID, path="more.txt")
    repo.git("switch", "-q", "-c", "feature")
    _bad(repo, "wip")
    return ["origin", "main", "feature"]


def _push_all_with_one_bad_branch(repo: Repo) -> list:
    repo.git("switch", "-q", "-c", "zfeat")
    _bad(repo, "wip")
    repo.git("switch", "-q", "-c", "afeat", "main")
    repo.commit("fix", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    return ["--all", "origin"]


def _detached_head_pushed_after_main(repo: Repo) -> list:
    repo.commit("more", OK_ID, path="more.txt")
    repo.git("switch", "-q", "--detach")
    _bad(repo, "wip")
    return ["origin", "main", "HEAD:refs/heads/x"]


def _tag_pushed_after_main(repo: Repo) -> list:
    repo.commit("more", OK_ID, path="more.txt")
    repo.git("switch", "-q", "--detach")
    _bad(repo, "wip")
    repo.git("tag", "v1")
    repo.git("switch", "-q", "main")
    return ["origin", "main", "v1"]


def _clean_branch_while_main_has_a_bad_commit(repo: Repo) -> list:
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("fix", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    _bad(repo, "local-only")
    return ["origin", "feature"]


def _clean_multi_ref(repo: Repo) -> list:
    repo.commit("more", OK_ID, path="more.txt")
    repo.git("switch", "-q", "-c", "feature")
    repo.commit("fix", OK_ID, path="a.txt")
    return ["origin", "main", "feature"]


def _clean_push_all(repo: Repo) -> list:
    repo.git("switch", "-q", "-c", "zfeat")
    repo.commit("z", OK_ID, path="z.txt")
    repo.git("switch", "-q", "-c", "afeat", "main")
    repo.commit("a", OK_ID, path="a.txt")
    repo.git("switch", "-q", "main")
    return ["--all", "origin"]


def _case_id(make_push) -> str:
    return make_push.__name__.lstrip("_")


@pytest.mark.parametrize(
    "make_push",
    [
        _other_branch,
        _bad_branch_checked_out_and_pushed_second,
        _bad_branch_checked_out_and_pushed_after_main,
        _push_all_with_one_bad_branch,
        _detached_head_pushed_after_main,
        _tag_pushed_after_main,
        _clean_branch_while_main_has_a_bad_commit,
    ],
    ids=_case_id,
)
def test_real_push_is_refused_while_any_local_ref_has_a_bad_commit(
    hooked_repo, make_push
):
    remote_before = hooked_repo.git("ls-remote", "origin")
    push = hooked_repo.push(*make_push(hooked_repo))

    assert push.returncode != 0
    assert "real.name@corp.example" in push.stdout + push.stderr
    assert ".demotool/" in push.stdout + push.stderr
    assert hooked_repo.git("ls-remote", "origin") == remote_before


@pytest.mark.parametrize("make_push", [_clean_multi_ref, _clean_push_all], ids=_case_id)
def test_real_clean_push_sends_every_branch(hooked_repo, make_push):
    push = hooked_repo.push(*make_push(hooked_repo))

    assert push.returncode == 0, push.stdout + push.stderr
    heads = hooked_repo.git(
        "for-each-ref", "--format=%(objectname)\t%(refname)", "refs/heads"
    )
    assert hooked_repo.git("ls-remote", "--heads", "origin") == heads
