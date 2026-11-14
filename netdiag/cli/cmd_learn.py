from __future__ import annotations

import click

from ..core import InputError, all_probes, get_probe
from ..i18n import tr
from ._app import cli


@cli.command()
@click.argument("probe_name", required=False)
def explain(probe_name):
    """各チェックの内容と、同じことを手で確かめるコマンドを表示 / Explain a check.

    With no argument, lists every probe. The goal is for users to learn to
    isolate faults by hand, not just read a verdict.
    """
    if not probe_name:
        click.echo(
            tr(
                "チェック一覧（詳細は 'netdiag explain <名前>'）:\n",
                "Available checks (run 'netdiag explain <name>' for detail):\n",
            )
        )
        for name in sorted(all_probes()):
            cls = all_probes()[name]
            scope = (
                tr(" [機器]", " [device]")
                if getattr(cls, "scope", "host") == "target"
                else ""
            )
            click.echo(f"  {name.ljust(12)} [{cls.layer.ljust(6)}] {cls.what}{scope}")
        return
    cls = get_probe(probe_name)
    if not cls:
        names = ", ".join(sorted(all_probes()))
        raise InputError(
            tr(
                f"不明なチェック名: {probe_name}。使えるチェック名: {names}",
                f"unknown check name: {probe_name}. Valid names: {names}",
            )
        )
    click.echo(f"# {probe_name} — {cls.title}  (layer {cls.layer})\n")
    click.echo(tr("内容: ", "What:  ") + str(cls.what))
    click.echo(tr("理由: ", "Why:   ") + str(cls.why))
    if cls.depends_on:
        click.echo(tr("前提: ", "Depends on: ") + ", ".join(cls.depends_on))
    if cls.requires_root:
        click.echo(
            tr(
                "注意: 詳細な確認には管理者/root権限が必要（無い場合は省略されます）",
                "Note:  needs root for full detail (degrades to skip otherwise).",
            )
        )
    if cls.manual_commands:
        click.echo(tr("\n手で確かめるには:", "\nDo it yourself:"))
        for cmd in cls.manual_commands:
            click.echo(f"  $ {cmd}")
    means, steps = cls().advice(None, {})
    if steps:
        click.echo(
            tr(
                "\nこれが原因と診断されたときの確認手順:",
                "\nWhen this is the root cause, next steps:",
            )
        )
        for s in steps:
            click.echo(f"  - {s}")
