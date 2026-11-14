"""``netdiag`` command-line interface.

Conclusion-first diagnosis with a low barrier: ``netdiag check`` runs the whole
layered sweep for this PC (and any devices given) and prints the cause and next
steps. Commands live in ``cmd_*`` modules; the click group and shared helpers in
``_app``.
"""

import sys
import traceback
from typing import List, NoReturn, Optional

import click

from ..core import ExitCode, InputError
from ..i18n import is_ja, tr
from . import (  # noqa: F401  (imported to register the subcommands)
    cmd_check,
    cmd_collect,
    cmd_learn,
    cmd_peer,
    cmd_scan,
    cmd_scenario,
    cmd_snapshot,
    cmd_soak,
)
from ._app import (
    DEFAULT_CONFIG,
    _build_config,
    _setup_logging,
    _split_csv,
    cli,
    harden_streams,
)
from .cmd_check import check, ping
from .cmd_collect import collect
from .cmd_scan import scan
from .cmd_scenario import scenario
from .cmd_soak import soak


def main(args: Optional[List[str]] = None, prog_name: Optional[str] = None) -> NoReturn:
    """Run the CLI and exit with an :class:`~netdiag.core.ExitCode`.

    Commands exit with their verdict themselves. Everything that ends a run
    without one is mapped here: bad input to 4, an unexpected exception to 5,
    Ctrl-C to 130.
    """
    harden_streams()  # before click prints anything, --help included
    try:
        if sys.platform == "win32":
            raise InputError(
                tr(
                    "netdiag 0.6.0 以降は Windows では動きません。Windows では 0.5.0 "
                    'を使ってください: pip install "network-check-tool==0.5.0"',
                    "netdiag 0.6.0 and later do not run on Windows. On Windows, use "
                    '0.5.0: pip install "network-check-tool==0.5.0"',
                )
            )
        code = cli.main(args=args, prog_name=prog_name, standalone_mode=False)
    except click.Abort:  # click's form of Ctrl-C
        click.echo(tr("中断しました。", "Interrupted."), err=True)
        sys.exit(ExitCode.INTERRUPTED)
    except click.ClickException as e:
        _usage_error(e)
        sys.exit(ExitCode.USAGE)
    except InputError as e:
        click.echo(tr("エラー: ", "Error: ") + str(e), err=True)
        sys.exit(ExitCode.USAGE)
    except Exception:
        click.echo(
            tr(
                "netdiag の内部エラーで処理を完了できませんでした。"
                "下の詳細を添えて報告してください。",
                "netdiag stopped because of an internal error. "
                "Please report it with the details below.",
            ),
            err=True,
        )
        traceback.print_exc()
        sys.exit(ExitCode.INTERNAL)
    sys.exit(code if isinstance(code, int) else ExitCode.OK)


def _usage_error(e: click.ClickException) -> None:
    """click's own message, after a Japanese headline in Japanese mode."""
    if is_ja():
        ctx = getattr(e, "ctx", None)
        command = ctx.command_path if ctx is not None else "netdiag"
        click.echo(
            f"指定に誤りがあります（詳細は下）。使い方は「{command} --help」で確認できます。",
            err=True,
        )
    e.show()


__all__ = [
    "cli",
    "main",
    "check",
    "collect",
    "ping",
    "scan",
    "scenario",
    "soak",
    "DEFAULT_CONFIG",
    "_build_config",
    "_setup_logging",
    "_split_csv",
]
