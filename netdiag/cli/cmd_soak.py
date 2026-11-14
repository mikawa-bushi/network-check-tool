"""``netdiag soak``: measure for minutes or hours to catch intermittent drops."""

from __future__ import annotations

import math
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import click

from ..core import ExitCode, InputError
from ..i18n import tr
from ._app import H, cli, color_wanted, format_from_path, save_report, write_output
from .cmd_check import _collect_devices


def _seconds(ctx: click.Context, param: click.Parameter, value: float) -> float:
    if not (math.isfinite(value) and value > 0):
        raise click.BadParameter(
            tr(
                f"0より大きい秒数を指定してください（{value} は使えません）",
                f"give a number of seconds above 0 (not {value})",
            )
        )
    return value


def _check_output(output: str, device_files) -> None:
    """Refuse an output that would overwrite a device list or cannot be written.

    Checked before measuring, so a long run never ends with nowhere to save.
    """
    target = Path(output)
    for f in device_files:
        if target.exists() and target.resolve() == Path(f).resolve():
            raise InputError(
                tr(
                    f"-o {output} は読み込む機器台帳と同じファイルです。台帳を上書きしないよう、別のファイル名を指定してください",
                    f"-o {output} is the device list being read; give another file name so the list is not overwritten",
                )
            )
    existed = target.exists()
    try:
        with open(target, "ab"):
            pass
    except OSError as e:
        raise InputError(
            tr(
                f"{output} に保存できません（{e.strerror or e}）。保存先を確認してください",
                f"cannot write to {output} ({e.strerror or e}); check the output path",
            )
        ) from e
    if not existed:
        target.unlink()


def _fallback_path(output: str) -> Path:
    p = Path(output)
    return p.with_name(f"{p.stem}-{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}")


@contextmanager
def _ctrl_c_once() -> Iterator[List[bool]]:
    """The first Ctrl-C stops measuring; later ones must not lose the report.

    Yields a list: once anything is in it (the first Ctrl-C, or the caller
    saying measuring is over), every Ctrl-C is ignored until the block ends.
    """
    done: List[bool] = []

    def on_sigint(signum, frame) -> None:
        if not done:
            done.append(True)
            raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGINT, on_sigint)
    except ValueError:  # not the main thread: leave Ctrl-C alone
        yield done
        return
    try:
        yield done
    finally:
        signal.signal(signal.SIGINT, previous)


@cli.command()
@click.argument("targets", nargs=-1, metavar="[TARGET]...")
@click.option(
    "-f",
    "--file",
    "device_files",
    type=click.Path(exists=True, dir_okay=False),
    multiple=True,
    help=H("機器台帳（CSV・YAML・JSON・TOML）", "Device list (CSV/YAML/JSON/TOML)."),
)
@click.option(
    "--duration",
    default="10m",
    show_default=True,
    help=H(
        "測定時間（例: 600, 30m, 2h, 1h30m）。Ctrl-C でいつでも終了してレポートを表示",
        "How long to measure (600, 30m, 2h, 1h30m). Ctrl-C stops early and still reports.",
    ),
)
@click.option(
    "--interval",
    type=float,
    default=0.2,
    show_default=True,
    callback=_seconds,
    help=H("1台あたりのping間隔[秒]", "Seconds between pings to each device."),
)
@click.option(
    "--timeout",
    "ping_timeout",
    type=float,
    default=1.0,
    show_default=True,
    callback=_seconds,
    help=H("1回の応答待ち[秒]", "Seconds to wait for each reply."),
)
@click.option(
    "--min-lost",
    type=click.IntRange(min=1),
    default=2,
    show_default=True,
    help=H(
        "何回連続で失われたら「断」とするか", "Consecutive losses that count as a drop."
    ),
)
@click.option(
    "--bucket",
    type=float,
    default=60.0,
    show_default=True,
    callback=_seconds,
    help=H(
        "集計の単位[秒]（表・グラフの1マス）",
        "Seconds per summary cell (table/heatmap).",
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "html", "csv"]),
    default=None,
    help=H(
        "出力形式（-o の拡張子からも判定）", "Output format (also inferred from -o)."
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    help=H(
        "レポートの保存先。.csv は測定中も1分ごとに追記されます",
        "Write the report here; a .csv is appended every minute while measuring.",
    ),
)
@click.option(
    "--quiet", is_flag=True, help=H("途中経過を表示しない", "No live progress lines.")
)
@click.option(
    "--no-color", is_flag=True, help=H("色を付けない", "Disable ANSI colour.")
)
def soak(
    targets,
    device_files,
    duration,
    interval,
    ping_timeout,
    min_lost,
    bucket,
    fmt,
    output,
    quiet,
    no_color,
):
    """長時間測定で「ときどき切れる」を捉えます / Catch intermittent drops.

    \b
    例: netdiag soak -f 台帳.csv --duration 2h -o 夜間測定.html
    全機器が同時に切れたのか、1台だけなのかで原因（このPC・スイッチ・その機器）を絞ります。
    終了コード: 0 = 断・ロスなし, 1 = 断あり, 2 = 散発的なロスのみ,
    3 = 測定できなかった機器・時間帯あり（断なし）, 4 = 指定の誤り
    """
    from ..core import Status
    from ..report.common import Style
    from ..report.soak import (
        csv_header,
        csv_rows,
        render_soak_csv,
        render_soak_html,
        render_soak_json,
        render_soak_text,
    )
    from ..soak import fmt_secs, hhmmss, parse_duration, run_soak

    try:
        seconds = parse_duration(duration)
    except ValueError:
        seconds = 0.0
    if not seconds > 0:
        raise click.BadParameter(
            tr(
                f"測定時間 {duration!r} は使えません（例: 600, 10m, 2h, 1h30m）",
                f"cannot use {duration!r} as a duration (examples: 600, 10m, 2h, 1h30m)",
            ),
            param_hint="'--duration'",
        )
    devices = _collect_devices(targets, device_files, ())
    if not devices:
        raise click.UsageError(
            tr(
                "測定する機器を指定してください（例: netdiag soak 192.168.3.39 または -f 台帳.csv）",
                "give devices to measure (e.g. netdiag soak 192.168.3.39 or -f devices.csv)",
            )
        )
    fmt = format_from_path(output, fmt)
    if output:
        _check_output(output, device_files)
    st = Style(color=sys.stderr.isatty() and not no_color)
    progressive: Optional[str] = output if fmt == "csv" else None
    if progressive:
        # Written as the minutes complete, so a long run that is cut short (power,
        # crash) still leaves data; rewritten in full at the end.
        Path(progressive).write_text(csv_header(), encoding="utf-8-sig", newline="")
    clock_fmt = "%H:%M" if bucket % 60 == 0 else "%H:%M:%S"

    def say(line: str) -> None:
        if not quiet:
            click.echo(line, err=True)

    closed: List = []

    def append_csv() -> None:
        nonlocal progressive
        try:
            with open(progressive, "a", encoding="utf-8", newline="") as fh:
                fh.writelines(csv_rows(track, buckets) for track, buckets in closed)
        except OSError as e:
            click.echo(
                tr(
                    f"注意: {progressive} に追記できません（{e.strerror or e}）。Excel などで開いていませんか。測定は続け、終了時にもう一度保存します",
                    f"Warning: cannot append to {progressive} ({e.strerror or e}); is it open in Excel? Measuring continues and the file is saved again at the end",
                ),
                err=True,
            )
            progressive = None

    def on_event(kind, data) -> None:
        if kind == "outage_start":
            o = data["outage"]
            say(
                f"{hhmmss(o.start)} {st.status(Status.FAIL)} "
                + tr(f"{o.name} ({o.host}) 通信断 開始", f"{o.name} ({o.host}) dropped")
            )
        elif kind == "outage_end":
            o = data["outage"]
            say(
                f"{hhmmss(o.end)} {st.status(Status.OK)} "
                + tr(
                    f"{o.name} 復旧（断 {fmt_secs(o.duration())}・ロス {o.lost}回）",
                    f"{o.name} back (down {fmt_secs(o.duration())}, {o.lost} lost)",
                )
            )
        elif kind == "not_measured":
            t = data["track"]
            say(
                st.status(Status.UNKNOWN)
                + tr(
                    f" {t.name} ({t.device.host}) は名前解決できないため測定しません",
                    f" {t.name} ({t.device.host}) is not measured: name resolution failed",
                )
            )
        elif kind == "bucket":
            closed.append((data["track"], data["buckets"]))
        elif kind == "tick":
            if progressive and closed:
                append_csv()
            by_start: Dict[float, List[str]] = {}
            for track, buckets in closed:
                for b in buckets:
                    lossy = by_start.setdefault(b.start, [])
                    if b.lost:
                        lossy.append(f"{track.name} {b.loss_pct:.1f}%")
            closed.clear()
            for start in sorted(by_start):  # report only cells that are complete
                lossy = by_start[start]
                stamp = time.strftime(clock_fmt, time.localtime(start))
                say(
                    f"{stamp} "
                    + (
                        tr("ロス: ", "loss: ") + ", ".join(lossy)
                        if lossy
                        else tr("損失なし", "no loss")
                    )
                )
            for e in data["nic"]:
                if e.link_drops:
                    say(
                        st.status(Status.FAIL)
                        + tr(
                            f" このPCのリンク切断: {e.interface}",
                            f" this PC's link dropped: {e.interface}",
                        )
                    )
                elif e.errors:
                    say(
                        st.status(Status.WARN)
                        + tr(
                            f" NICエラー増加: {e.interface} +{e.errors}",
                            f" NIC errors: {e.interface} +{e.errors}",
                        )
                    )
        elif kind == "stopping":
            measured.append(True)
            if data["reason"]:
                say(
                    st.status(Status.UNKNOWN)
                    + tr(
                        f" 測定が止まりました: {data['reason']}",
                        f" Measuring stopped: {data['reason']}",
                    )
                )
            say(
                tr(
                    "測定を終了しています。最後の応答を待ってレポートを作成します…",
                    "Stopping: waiting for the last replies, then writing the report…",
                )
            )

    say(
        tr(
            f"{len(devices)}台を {fmt_secs(seconds)} 測定します（{interval}秒間隔）。Ctrl-C で終了してレポートを表示します。",
            f"Measuring {len(devices)} device(s) for {fmt_secs(seconds)} (every {interval}s). "
            "Ctrl-C stops and prints the report.",
        )
    )
    with _ctrl_c_once() as measured:
        rep = run_soak(
            devices,
            duration=seconds,
            interval=interval,
            timeout=ping_timeout,
            bucket=bucket,
            min_lost=min_lost,
            on_event=on_event,
            stop=threading.Event(),
        )
        measured.append(True)
        if fmt == "json":
            text = render_soak_json(rep)
        elif fmt == "html":
            text = render_soak_html(rep)
        elif fmt == "csv":
            text = render_soak_csv(rep)
        else:
            text = render_soak_text(
                rep,
                color=False if output else color_wanted(no_color),
                unicode=True if output else None,
            )
        code = _deliver(text, output, fmt)
    sys.exit(rep.exit_code if code is None else code)


def _deliver(text: str, output: Optional[str], fmt: str) -> Optional[ExitCode]:
    """Write the report; never lose it. Returns an exit code when saving failed."""
    if not output:
        write_output(text, None, fmt)
        return None
    try:
        save_report(text, output, fmt)
        return None
    except OSError as e:
        reason = e.strerror or str(e)
    spare = _fallback_path(output)
    try:
        save_report(text, str(spare), fmt)
    except OSError:
        click.echo(
            tr(
                f"エラー: レポートを {output} に保存できませんでした（{reason}）。内容をここに表示します。",
                f"Error: could not save the report to {output} ({reason}); here it is instead.",
            ),
            err=True,
        )
        write_output(text, None, fmt)
        return ExitCode.USAGE
    click.echo(
        tr(
            f"エラー: レポートを {output} に保存できませんでした（{reason}）。代わりに {spare} に保存しました。",
            f"Error: could not save the report to {output} ({reason}); saved it to {spare} instead.",
        ),
        err=True,
    )
    return ExitCode.USAGE
