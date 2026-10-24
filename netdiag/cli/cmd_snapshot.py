"""``netdiag snapshot``: save and diff network configuration ('what changed?')."""

from __future__ import annotations

import json
import sys

import click

from ..i18n import tr
from ._app import cli


@cli.group()
def snapshot():
    """設定のスナップショット保存と差分 / Save and diff configuration snapshots."""


@snapshot.command("save")
@click.argument("name", default="default")
def snapshot_save(name):
    """現在のネットワーク設定を保存（正常なときに）/ Save the current configuration."""
    from .. import snapshot as snap

    path = snap.save(name)
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


@snapshot.command("diff")
@click.argument("name", default="default")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def snapshot_diff(name, fmt):
    """保存時と現在の設定の差分（終了コード 0=変化なし, 2=変化あり）/ Diff against now."""
    from .. import snapshot as snap

    baseline = snap.load(name)
    if baseline is None:
        click.echo(
            tr(
                f"'{name}' という名前のスナップショットはありません — 正常なときに 'netdiag snapshot save {name}' を実行してください。",
                f"no snapshot named '{name}' — run 'netdiag snapshot save {name}' while the network works.",
            ),
            err=True,
        )
        sys.exit(1)
    current = snap.capture()
    d = snap.diff_snapshots(baseline, current)

    if fmt == "json":
        # Same exit-code contract as the text form: 0 = no change, 2 = changed.
        click.echo(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(2 if d.any_change else 0)

    if not d.any_change:
        click.echo(
            tr(
                f"'{name}'（{d.baseline_time} 保存）から設定の変化はありません。",
                f"no configuration changes since '{name}' (saved {d.baseline_time}).",
            )
        )
        return
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
    sys.exit(2)
