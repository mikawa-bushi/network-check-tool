"""``netdiag snapshot``: save and diff network configuration ('what changed?')."""

from __future__ import annotations

import json
import sys

import click

from ..core import ExitCode, InputError
from ..i18n import tr
from ._app import cli


@cli.group()
def snapshot():
    """設定のスナップショット保存と差分 / Save and diff configuration snapshots."""


@snapshot.command("save")
@click.argument("name", default="default")
def snapshot_save(name):
    """現在のネットワーク設定を保存（正常なときに）/ Save the current configuration.

    終了コード: 0 = 保存, 3 = 設定を読めず保存しなかった, 4 = 使えない名前・未対応のOS
    """
    from .. import snapshot as snap

    try:
        path = snap.save(name)
    except snap.NothingCaptured as e:
        click.echo(str(e), err=True)
        sys.exit(ExitCode.INCOMPLETE)
    click.echo(
        tr(
            f"スナップショット '{name}' を保存: {path}",
            f"snapshot '{name}' saved to {path}",
        )
    )


@snapshot.command("list")
def snapshot_list():
    """保存済みスナップショットの一覧 / List saved snapshots."""
    from .. import snapshot as snap

    names = snap.list_snapshots()
    click.echo(
        "\n".join(names)
        if names
        else tr("（保存されたスナップショットはありません）", "(no snapshots saved)")
    )


def _exit_code(d) -> ExitCode:
    if d.any_change:
        return ExitCode.WARN
    return ExitCode.INCOMPLETE if d.not_compared else ExitCode.OK


@snapshot.command("diff")
@click.argument("name", default="default")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def snapshot_diff(name, fmt):
    """保存時と現在の設定の差分 / Diff against now.

    終了コード: 0 = 変化なし, 2 = 変化あり, 3 = 一部を比較できなかった（変化は見つからず）
    """
    from .. import snapshot as snap

    baseline = snap.load(name)
    if baseline is None:
        raise InputError(
            tr(
                f"'{name}' という名前のスナップショットはありません。"
                f"正常なときに 'netdiag snapshot save {name}' を実行してください",
                f"no snapshot named '{name}'; run 'netdiag snapshot save {name}' "
                "while the network works",
            )
        )
    current = snap.capture()
    d = snap.diff_snapshots(baseline, current)

    if fmt == "json":
        click.echo(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(_exit_code(d))

    if d.any_change:
        click.echo(
            tr(
                f"'{name}'（{d.baseline_time} 保存）からの変化:\n",
                f"changes since '{name}' (saved {d.baseline_time}):\n",
            )
        )
        for sd in d.changed:
            click.echo(f"■ {sd.label} — {sd.why}")
            for line in sd.removed:
                click.echo(f"    - {line}")
            for line in sd.added:
                click.echo(f"    + {line}")
            click.echo("")
    else:
        click.echo(
            tr(
                f"'{name}'（{d.baseline_time} 保存）から設定の変化はありません。",
                f"no configuration changes since '{name}' (saved {d.baseline_time}).",
            )
        )
    for n in d.not_compared:
        click.echo(
            tr(
                f"? {n.label}: 比較できませんでした（{n.reason}）",
                f"? {n.label}: not compared ({n.reason})",
            )
        )
    sys.exit(_exit_code(d))
