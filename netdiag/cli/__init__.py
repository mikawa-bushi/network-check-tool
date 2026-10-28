"""``netdiag`` command-line interface.

Conclusion-first diagnosis with a low barrier: ``netdiag check`` runs the whole
layered sweep for this PC (and any devices given) and prints the cause and next
steps. Commands live in ``cmd_*`` modules; the click group and shared helpers in
``_app``.
"""

from . import (  # noqa: F401  (imported to register the subcommands)
    cmd_check,
    cmd_collect,
    cmd_daemon,
    cmd_learn,
    cmd_peer,
    cmd_scan,
    cmd_scenario,
    cmd_service,
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
from .cmd_service import _exec_start, _unit_text
from .cmd_soak import soak


def main() -> None:
    harden_streams()  # before click prints anything, --help included
    cli()


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
    "_unit_text",
    "_exec_start",
]
