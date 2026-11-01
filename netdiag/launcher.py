"""Entry point of the Windows ``netdiag.exe``.

Made for people who never open a terminal:

* **Double-click** the exe: this PC's network is checked, the result is shown,
  an HTML report is saved next to the exe and opened in the browser.
* **Drop a device list** (CSV saved from Excel, YAML, JSON, TOML) onto the
  exe: every device in it is checked; the report is saved next to the list.

Both wait for Enter before closing, so the console window does not vanish.
Anything else (``netdiag.exe check -f list.csv`` ...) is the normal CLI.
"""

from __future__ import annotations

import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

import click

LIST_SUFFIXES = {".csv", ".tsv", ".txt", ".yaml", ".yml", ".json", ".toml"}


def _report_path(folder: Path, stem: str) -> Path:
    from .i18n import tr

    stamp = time.strftime("%Y%m%d-%H%M")
    return folder / tr(f"{stem}_点検結果_{stamp}.html", f"{stem}_report_{stamp}.html")


def _open(path: Path) -> None:
    if os.environ.get("NETDIAG_NO_OPEN"):
        return
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())
    except OSError:
        pass


def _pause() -> None:
    from .i18n import tr

    try:
        input(tr("\nEnter キーを押すと終了します…", "\nPress Enter to close…"))
    except (EOFError, KeyboardInterrupt):
        pass


def _folder_of_exe() -> Path:
    exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    return exe.resolve().parent


def _save(html: str, folder: Path, stem: str) -> Path:
    """Write the report into *folder*, else the home folder (exe in Program Files...)."""
    err: Optional[OSError] = None
    for where in (folder, Path.home()):
        out = _report_path(where, stem)
        try:
            out.write_text(html, encoding="utf-8")
            return out
        except OSError as e:
            err = e
    assert err is not None
    raise err


def interactive(device_file: Optional[Path]) -> int:
    """Run a check, show it, save + open the HTML report. Returns the exit code."""
    from . import report as report_mod
    from .cli._app import Progress, _build_config, setup
    from .cli.cmd_check import _collect_devices
    from .i18n import tr
    from .runner import run_check

    setup()
    print(tr("netdiag ネットワーク点検", "netdiag network check"))
    devices = []
    if device_file is not None:
        print(tr(f"台帳: {device_file}", f"device list: {device_file}"))
        try:
            devices = _collect_devices((), (str(device_file),), ())
        except click.UsageError as e:
            print(
                tr(
                    f"台帳を読めませんでした: {e.message}",
                    f"could not read the device list: {e.message}",
                )
            )
            return 2
        print(
            tr(f"{len(devices)} 台を点検します。", f"checking {len(devices)} devices.")
        )
    else:
        print(
            tr(
                "このPCのネットワークを確認します。\n"
                "機器もまとめて点検するには、台帳（Excelで保存したCSV）をこの exe にドラッグ＆ドロップしてください。",
                "Checking this PC.\nTo check the devices on the line too, drop a device list (CSV) onto this exe.",
            )
        )
    progress = Progress()
    try:
        rep = run_check(
            devices, config=_build_config(None, None, (), None, None), progress=progress
        )
    except KeyboardInterrupt:
        print(tr("\n中断しました。", "\ninterrupted."))
        return 130
    finally:
        progress.done()
    print()
    print(report_mod.render_check_text(rep, color=False))
    folder = device_file.parent if device_file is not None else _folder_of_exe()
    stem = device_file.stem if device_file is not None else tr("このPC", "this-pc")
    try:
        out = _save(report_mod.render_check_html(rep), folder, stem)
    except OSError as e:
        print(
            tr(
                f"\nレポートを保存できませんでした: {e}",
                f"\ncould not save the report: {e}",
            )
        )
    else:
        print(tr(f"\nレポートを保存しました: {out}", f"\nreport saved: {out}"))
        _open(out)
    return rep.exit_code


def main(argv: Optional[List[str]] = None) -> None:
    from .cli._app import harden_streams

    harden_streams()
    args = sys.argv[1:] if argv is None else argv
    dropped = Path(args[0]) if len(args) == 1 else None
    if not args or (
        dropped is not None
        and dropped.suffix.lower() in LIST_SUFFIXES
        and dropped.is_file()
    ):
        code = interactive(dropped)
        _pause()
        sys.exit(code)
    from .cli import cli

    cli.main(args=args, prog_name="netdiag")


if __name__ == "__main__":  # pragma: no cover
    main()
