"""``netdiag soak``: measure for minutes or hours to catch intermittent drops."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List

import click

from ..i18n import tr
from ._app import H, cli, color_wanted, format_from_path, write_output
from .cmd_check import _collect_devices


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
    help=H("1台あたりのping間隔[秒]", "Seconds between pings to each device."),
)
@click.option(
    "--timeout",
    "ping_timeout",
    type=float,
    default=1.0,
    show_default=True,
    help=H("1回の応答待ち[秒]", "Seconds to wait for each reply."),
)
@click.option(
    "--min-lost",
    type=int,
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
    終了コード: 0 = 断・ロスなし, 1 = 断あり, 2 = 散発的なロスのみ
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
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    devices = _collect_devices(targets, device_files, ())
    if not devices:
        raise click.UsageError(
            tr(
                "測定する機器を指定してください（例: netdiag soak 192.168.3.39 または -f 台帳.csv）",
                "give devices to measure (e.g. netdiag soak 192.168.3.39 or -f devices.csv)",
            )
        )
    fmt = format_from_path(output, fmt)
    st = Style(color=sys.stderr.isatty() and not no_color)
    progressive = output if fmt == "csv" else None
    if progressive:
        # Written as the minutes complete, so a long run that is cut short (power,
        # crash) still leaves data; rewritten in full at the end.
        Path(progressive).write_text(csv_header(), encoding="utf-8-sig", newline="")

    def say(line: str) -> None:
        if not quiet:
            click.echo(line, err=True)

    closed: List = []

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
        elif kind == "bucket":
            closed.append((data["track"], data["buckets"]))
        elif kind == "tick":
            lossy = []
            any_closed = bool(closed)
            if progressive and closed:
                with open(progressive, "a", encoding="utf-8", newline="") as fh:
                    fh.writelines(csv_rows(track, buckets) for track, buckets in closed)
            for track, buckets in closed:
                for b in buckets:
                    if b.lost:
                        lossy.append(f"{track.name} {b.loss_pct:.1f}%")
            closed.clear()
            stamp = time.strftime("%H:%M", time.localtime(data["ts"]))
            if any_closed:  # report only minutes that are complete
                say(
                    f"{stamp} "
                    + (
                        tr("ロス: ", "loss: ") + ", ".join(lossy)
                        if lossy
                        else tr("全機器 損失なし", "no loss on any device")
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

    say(
        tr(
            f"{len(devices)}台を {fmt_secs(seconds)} 測定します（{interval}秒間隔）。Ctrl-C で終了してレポートを表示します。",
            f"Measuring {len(devices)} device(s) for {fmt_secs(seconds)} (every {interval}s). "
            "Ctrl-C stops and prints the report.",
        )
    )
    rep = run_soak(
        devices,
        duration=seconds,
        interval=interval,
        timeout=ping_timeout,
        bucket=bucket,
        min_lost=min_lost,
        on_event=on_event,
    )

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
    write_output(text, output, fmt)
    sys.exit(rep.exit_code)
