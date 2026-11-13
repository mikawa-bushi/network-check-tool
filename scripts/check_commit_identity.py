#!/usr/bin/env python3
"""push 前に author/committer メールの許可リストを検証する pre-push ガード。

これから push されるコミット（push 先リモートに未到達のもの。範囲は
_pushed_revs が決める）を走査し、author か committer のメールが
許可リスト外なら push を中止する。
秘密情報スキャナはファイル内容しか見ず、コミットの著者メタデータは検査しない
ため、社用/別 identity の混入（例: ``@company.example``）はこのガードで防ぐ。

許可リストは ``MONITOR_ALLOWED_EMAILS``（カンマ区切り、完全一致を追加）で拡張可。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# 完全一致で許可するメール（正規の公開 identity）
ALLOWED_EXACT = {
    "155122235+hardwork9047@users.noreply.github.com",  # hardwork9047
    "noreply@github.com",  # GitHub Web のマージコミット committer
}
# 正規表現で許可するメール
ALLOWED_PATTERNS = [
    re.compile(r".+@users\.noreply\.github\.com$"),  # GitHub のアカウント別 noreply
]

_FS = "\x1f"  # フィールド区切り（コミットメッセージと衝突しない制御文字）


def _allowed(email: str) -> bool:
    if email in ALLOWED_EXACT:
        return True
    return any(p.match(email) for p in ALLOWED_PATTERNS)


def _pushed_revs() -> list[str]:
    """これから push されるコミットを選ぶ ``git rev-list`` 引数。

    pre-commit の pre-push ステージは push する先頭コミットを PRE_COMMIT_TO_REF
    （root コミットを含む push では PRE_COMMIT_LOCAL_BRANCH）に、push 先を
    PRE_COMMIT_REMOTE_NAME に入れる。今いるブランチではなくそれを検査し、除外は
    push 先に既にあるコミットだけにする（別リモートにしかない履歴も検査する）。
    手で実行した場合は HEAD を origin と比べる。
    """
    env = os.environ
    tip = env.get("PRE_COMMIT_TO_REF") or env.get("PRE_COMMIT_LOCAL_BRANCH") or "HEAD"
    remote = env.get("PRE_COMMIT_REMOTE_NAME") or "origin"
    on_remote = env.get("PRE_COMMIT_FROM_REF")
    return [tip, "--not", f"--remotes={remote}", *([on_remote] if on_remote else [])]


def _outgoing_commits() -> list[tuple[str, str, str, str]]:
    """push されるコミットを (sha, author_email, committer_email, subject) で返す。"""
    result = subprocess.run(
        [
            "git",
            "rev-list",
            f"--format=%H{_FS}%ae{_FS}%ce{_FS}%s",
            *_pushed_revs(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    commits = []
    for line in result.stdout.splitlines():
        if _FS not in line:  # rev-list が挟む "commit <sha>" 行はスキップ
            continue
        sha, author_email, committer_email, subject = line.split(_FS, 3)
        commits.append((sha, author_email, committer_email, subject))
    return commits


def main() -> int:
    for extra in os.environ.get("MONITOR_ALLOWED_EMAILS", "").split(","):
        extra = extra.strip()
        if extra:
            ALLOWED_EXACT.add(extra)

    try:
        commits = _outgoing_commits()
    except subprocess.CalledProcessError as exc:
        print(f"check-commit-identity: git rev-list に失敗: {exc}", file=sys.stderr)
        return 1

    offenders = []
    for sha, author_email, committer_email, subject in commits:
        bad = {
            role: email
            for role, email in (
                ("author", author_email),
                ("committer", committer_email),
            )
            if not _allowed(email)
        }
        if bad:
            offenders.append((sha[:10], subject, bad))

    if not offenders:
        return 0

    print(
        "\n⛔ push を中止しました: 許可されていない author/committer メールを検出。\n",
        file=sys.stderr,
    )
    for sha, subject, bad in offenders:
        for role, email in bad.items():
            print(f"  {sha}  [{role}: {email}]  {subject}", file=sys.stderr)
    allow = ", ".join(sorted(ALLOWED_EXACT)) + ", *@users.noreply.github.com"
    print(f"\n許可リスト: {allow}", file=sys.stderr)
    print(
        "対処: `git config user.email <正しいメール>` を設定し、該当コミットを "
        "rebase/amend で修正してから push してください。\n"
        "（許可リストの拡張は MONITOR_ALLOWED_EMAILS 環境変数で可能）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
