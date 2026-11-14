"""Entry point of the Windows ``netdiag.exe``.

Made for people who never open a terminal:

* **Double-click** the exe: this PC's network is checked, the result is shown,
  an HTML report is saved next to the exe and opened in the browser.
* **Drop a device list** (CSV saved from Excel, YAML, JSON, TOML) onto the
  exe: every device in it is checked; the report is saved next to the list.

Both wait for Enter before closing, so the console window does not vanish,
whatever the outcome: a dropped file that is not a device list, a list that
cannot be read, an internal error.
Anything else (``netdiag.exe check -f list.csv`` ...) is the normal CLI.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import webbrowser
from pathlib import Path
from typing import List, Optional

import click

from .core import ExitCode, InputError

LIST_SUFFIXES = {".csv", ".tsv", ".txt", ".yaml", ".yml", ".json", ".toml"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls"}


def _write_new_report(folder: Path, stem: str, html: str) -> Path:
    """Write *html* under a name no earlier report has, so checking again
    after a fix (even within the same second) keeps the "before" report."""
    from .i18n import tr

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = tr(f"{stem}_点検結果_{stamp}", f"{stem}_report_{stamp}")
    path, n = folder / f"{base}.html", 1
    while True:
        try:
            with path.open("x", encoding="utf-8") as f:
                f.write(html)
            return path
        except FileExistsError:
            n += 1
            path = folder / f"{base}-{n}.html"


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
        try:
            return _write_new_report(where, stem, html)
        except OSError as e:
            err = e
    assert err is not None
    raise err


def _drop_problem(paths: List[Path]) -> Optional[str]:
    """Why what was dropped on the exe is not one device list, or None."""
    from .i18n import tr

    if len(paths) > 1:
        return tr(
            f"台帳は1つずつドロップしてください（{len(paths)} 個がドロップされました）。",
            f"Drop one device list at a time ({len(paths)} items were dropped).",
        )
    path = paths[0]
    suffix = path.suffix.lower()
    if path.is_dir():
        return tr(
            "フォルダーではなく、台帳のファイル（Excel で保存した CSV）をドロップしてください。",
            "Drop a device list file (a CSV saved from Excel), not a folder.",
        )
    if suffix in EXCEL_SUFFIXES:
        return tr(
            "Excel のブックはそのままでは読めません。Excel の「名前を付けて保存」で"
            "「CSV UTF-8（コンマ区切り）」を選んで保存し、その CSV をドロップしてください。",
            "An Excel workbook cannot be read as it is. In Excel, choose Save As > "
            "'CSV UTF-8 (Comma delimited)', then drop that CSV.",
        )
    if suffix not in LIST_SUFFIXES:
        return tr(
            f"台帳として読めない種類のファイルです（{path.name}）。"
            "CSV・YAML・JSON・TOML の台帳をドロップしてください。",
            f"{path.name} is not a kind of file netdiag reads as a device list. "
            "Drop a CSV, YAML, JSON or TOML list.",
        )
    return None


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
        except (click.UsageError, InputError) as e:
            print(
                tr(
                    f"台帳を読めませんでした: {e}",
                    f"could not read the device list: {e}",
                )
            )
            return ExitCode.USAGE
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
        return ExitCode.INTERRUPTED
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
        return ExitCode.USAGE
    print(tr(f"\nレポートを保存しました: {out}", f"\nreport saved: {out}"))
    _open(out)
    return rep.exit_code


def _double_clicked_or_dropped(args: List[str]) -> int:
    """No arguments (double-click) or the paths Explorer passes for a drop."""
    if not args:
        return interactive(None)
    problem = _drop_problem([Path(a) for a in args])
    if problem is not None:
        print(problem)
        return ExitCode.USAGE
    return interactive(Path(args[0]))


def main(argv: Optional[List[str]] = None) -> None:
    from .cli import cli
    from .cli import main as cli_main
    from .cli._app import harden_streams
    from .i18n import tr

    harden_streams()
    args = sys.argv[1:] if argv is None else argv
    typed = bool(args) and (
        args[0].startswith("-")
        or args[0] in cli.commands
        or not all(Path(a).exists() for a in args)
    )
    if typed:
        cli_main(args, prog_name="netdiag")
    try:
        code = _double_clicked_or_dropped(args)
    except Exception:
        print(
            tr(
                "netdiag の内部エラーで点検を完了できませんでした。"
                "下の詳細を添えて報告してください。",
                "netdiag stopped because of an internal error. "
                "Please report it with the details below.",
            )
        )
        traceback.print_exc()
        code = ExitCode.INTERNAL
    _pause()
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
