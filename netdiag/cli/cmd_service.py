"""``netdiag service``: systemd unit generation for ``netdiag watch``."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from ..i18n import tr
from ._app import H, cli


@cli.group()
def service():
    """watch を常駐サービス化する systemd ユニットを生成 / systemd unit for 'watch'."""


def _unit_text(exec_start: str, system: bool) -> str:
    """Render a systemd unit file that runs ``netdiag watch`` as a service.

    ``exec_start`` is used verbatim as the ``ExecStart=`` command. ``system``
    selects the install target: a system unit (``WantedBy=multi-user.target``)
    when True, or a per-user unit (``WantedBy=default.target``) when False. No
    ``User=`` directive is emitted in either case.
    """
    after = "After=network-online.target\nWants=network-online.target"
    install_target = "multi-user.target" if system else "default.target"
    return f"""[Unit]
Description=netdiag always-on network diagnosis watcher
{after}

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
# Bounded resource use for a long-running daemon.
Nice=10

[Install]
WantedBy={install_target}
"""


def _exec_start(interval, external_ip, dns_name) -> str:
    exe = shutil.which("netdiag")
    base = exe if exe else f"{sys.executable} -m netdiag.cli"
    return (
        f"{base} watch --interval {interval} --external-ip {external_ip} "
        f"--dns-name {dns_name}"
    )


@service.command("print")
@click.option(
    "--system",
    is_flag=True,
    help=H(
        "システムユニット（既定はユーザーユニット）",
        "System unit (default: user unit).",
    ),
)
@click.option("--interval", default=30, type=int)
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
def service_print(system, interval, external_ip, dns_name):
    """systemd ユニットを標準出力へ / Print a systemd unit file to stdout."""
    click.echo(_unit_text(_exec_start(interval, external_ip, dns_name), system))


@service.command("install")
@click.option("--interval", default=30, type=int)
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
def service_install(interval, external_ip, dns_name):
    """ユーザー systemd ユニットを書き出し、有効化コマンドを表示 / Write a user unit.

    Installs to ~/.config/systemd/user (no root needed). We deliberately do NOT
    run systemctl for you — enabling a service changes system state, so the
    commands are printed for you to review and run.
    """
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "netdiag-watch.service"
    unit_path.write_text(
        _unit_text(_exec_start(interval, external_ip, dns_name), system=False),
        encoding="utf-8",
    )
    click.echo(
        tr(
            f"{unit_path} を書き出しました\n\n有効化するには:",
            f"wrote {unit_path}\n\nEnable it with:",
        )
    )
    click.echo("  systemctl --user daemon-reload")
    click.echo("  systemctl --user enable --now netdiag-watch")
    click.echo(
        tr(
            "  # ログアウト後も動かす: sudo loginctl enable-linger $USER",
            "  # survive logout: sudo loginctl enable-linger $USER",
        )
    )
    click.echo(
        tr(
            "\nシステム全体のサービスにする場合:",
            "\nFor a system-wide service instead:",
        )
    )
    click.echo(
        "  netdiag service print --system | sudo tee /etc/systemd/system/netdiag-watch.service"
    )
