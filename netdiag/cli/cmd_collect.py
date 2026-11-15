"""``netdiag collect``: bundle raw command output for field verification."""

from __future__ import annotations

from pathlib import Path

import click

from ..i18n import tr
from ._app import H, Progress, cli


@cli.command()
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help=H(
        "保存先のzip（既定: netdiag-collect-日時.zip）",
        "Zip to write (default: netdiag-collect-<time>.zip).",
    ),
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    help=H("追加でpingする機器（複数可）", "Extra device to ping (repeatable)."),
)
@click.option(
    "--no-mask",
    is_flag=True,
    help=H(
        "IP・MAC・PC名などを伏せずに保存（社内でのみ使用）",
        "Keep IPs/MACs/host names unmasked (internal use only).",
    ),
)
@click.option(
    "--no-ping", is_flag=True, help=H("pingを行わない", "Do not send any ping.")
)
def collect(output, targets, no_mask, no_ping):
    """実機の生出力をzipに収集（不具合報告・検証用）/ Collect raw output for verification.

    netdiag が使うOSコマンド（ping・ip・arp・route など）の出力を文字コード
    変換前のまま保存し、netdiag の解釈結果と一緒にまとめます。IPアドレスやMAC、PC名は
    既定で伏せます（自動処理のため、社外に送る前に中身を確認してください）。
    """
    from ..collect import default_output, run_collect

    path = Path(output) if output else default_output()
    progress = Progress()
    try:
        res = run_collect(
            path,
            mask=not no_mask,
            targets=list(targets),
            ping=not no_ping,
            progress=lambda name: progress(tr(f"収集中: {name}", f"collecting {name}")),
        )
    finally:
        progress.done()
    ran = len(res.commands)
    click.echo(
        tr(
            f"{ran} 個のコマンド出力を保存しました: {res.path}",
            f"saved {ran} command outputs to {res.path}",
        )
    )
    if res.missing:
        click.echo(
            tr(
                f"  この環境に無いコマンド: {', '.join(res.missing)}",
                f"  not available here: {', '.join(res.missing)}",
            )
        )
    click.echo(
        tr(
            "  IP・MAC・PC名・SSID・ドメインは伏せてあります（--no-mask で無効）。送る前に内容を確認してください。",
            "  IPs, MACs, host/user names, SSIDs and domains are masked (--no-mask to keep). Review before sharing.",
        )
        if res.masked
        else tr(
            "  マスクなしで保存しました（社外に出さないでください）。",
            "  saved WITHOUT masking — keep it internal.",
        )
    )
