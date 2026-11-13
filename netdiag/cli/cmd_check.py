"""``netdiag check`` (this PC + devices) and ``netdiag ping``."""

from __future__ import annotations

import sys
from typing import List

import click

from ..i18n import tr
from ..inventory import (
    Device,
    InventoryError,
    load_devices,
    parse_ports,
    parse_target_spec,
)
from ._app import (
    H,
    Progress,
    _build_config,
    _split_csv,
    cli,
    color_wanted,
    format_from_path,
    write_output,
)


def effective_interval(interval) -> float:
    """Seconds between pings as actually sent on this PC.

    Without a precise pinger the OS ``ping`` sets the pace (``ping.exe``: about
    one request per second whatever is asked), so a ``--duration`` must be
    converted with that cadence or a 10-minute test would run for 50 minutes.
    """
    from ..measure import precise_ping_available
    from ..measure.icmp import system_ping_cadence

    wanted = interval or 0.2
    if precise_ping_available():
        return wanted
    return max(wanted, system_ping_cadence())


def _collect_devices(targets, device_files, ports) -> List[Device]:
    devices: List[Device] = []
    try:
        extra = parse_ports(list(ports)) if ports else []
        for path in device_files:
            devices.extend(load_devices(path))
        for spec in targets:
            devices.append(parse_target_spec(spec))
    except InventoryError as e:
        raise click.UsageError(str(e)) from e
    for d in devices:
        for p in extra:
            if p not in d.ports:
                d.ports.append(p)
    return devices


@cli.command()
@click.argument("targets", nargs=-1, metavar="[TARGET]...")
@click.option(
    "-f",
    "--file",
    "device_files",
    type=click.Path(exists=True, dir_okay=False),
    multiple=True,
    help=H(
        "機器台帳（CSV・YAML・JSON・TOML）。Excelで保存したCSVもそのまま読めます",
        "Device list (CSV/YAML/JSON/TOML); Excel-saved CSV works as-is.",
    ),
)
@click.option(
    "-p",
    "--port",
    "ports",
    multiple=True,
    help=H(
        "全機器で確認するTCPポート（番号 または modbus/enip/opcua/s7/melsoft/fins など）",
        "TCP port to check on every device (number or modbus/enip/opcua/s7/melsoft/fins ...).",
    ),
)
@click.option(
    "--count",
    type=int,
    default=None,
    help=H(
        "1台あたりのping回数（既定 10）。断続的な不調は 100〜300 で測ると確実です",
        "Pings per target (default 10). Use 100-300 to catch intermittent loss.",
    ),
)
@click.option(
    "--interval",
    type=float,
    default=None,
    help=H("ping間隔[秒]（既定 0.2）", "Seconds between pings (default 0.2)."),
)
@click.option(
    "--duration",
    type=float,
    default=None,
    help=H(
        "測定時間[秒]。指定すると回数 = 時間 ÷ 間隔",
        "Measure for this many seconds (count = duration / interval).",
    ),
)
@click.option(
    "--timeout",
    "ping_timeout",
    type=float,
    default=None,
    help=H(
        "1回のping応答待ち[秒]（既定 1.0）",
        "Seconds to wait for each reply (default 1.0).",
    ),
)
@click.option(
    "--internet",
    is_flag=True,
    help=H(
        "インターネット接続（外部到達・DNS・IPv6）も確認する。閉じた設備ネットワークでは不要",
        "Also check internet access (external, DNS, IPv6). Not needed on closed equipment networks.",
    ),
)
@click.option(
    "--external-ip",
    help=H(
        "外部到達確認に使うIP（指定すると --internet）",
        "Off-LAN IP for the reachability check (implies --internet).",
    ),
)
@click.option(
    "--dns-name",
    help=H(
        "名前解決確認に使うホスト名（指定すると --internet）",
        "Hostname for the DNS check (implies --internet).",
    ),
)
@click.option(
    "--tcp",
    multiple=True,
    metavar="HOST:PORT",
    help=H(
        "このPCから確認するTCPサービス（複数可）",
        "TCP service(s) to test from this PC. Repeatable.",
    ),
)
@click.option(
    "--http-url",
    help=H(
        "HTTP/TLSの段階別タイミングを測るURL",
        "URL for the HTTP/TLS phase-timing check.",
    ),
)
@click.option(
    "--only",
    help=H(
        "実行するチェックをカンマ区切りで限定",
        "Comma-separated probe names to run exclusively.",
    ),
)
@click.option(
    "--skip",
    help=H("省略するチェック（カンマ区切り）", "Comma-separated probe names to skip."),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "html", "csv"]),
    default=None,
    help=H(
        "出力形式（-o の拡張子からも判定）",
        "Output format (also inferred from -o's extension).",
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    help=H("レポートをファイルに保存", "Write the report to a file."),
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help=H("対象外の項目も表示", "Also show not-applicable checks."),
)
@click.option(
    "--reasoning",
    is_flag=True,
    help=H(
        "診断エンジンの推論過程を表示（-v はログ用で別物）",
        "Show the engine's step-by-step reasoning in the report. "
        "(Distinct from the global -v/--verbose, which controls logging.)",
    ),
)
@click.option(
    "--no-color", is_flag=True, help=H("色を付けない", "Disable ANSI colour.")
)
@click.option(
    "--jobs",
    type=int,
    default=1,
    help=H(
        "このPCのチェックを並列実行（1=直列。3〜4推奨）",
        "Run independent probes concurrently (1=serial default; 3-4 recommended).",
    ),
)
@click.option(
    "--no-host",
    is_flag=True,
    help=H(
        "このPCの確認を省き、機器だけ確認",
        "Skip this PC's checks; check the devices only.",
    ),
)
def check(
    targets,
    device_files,
    ports,
    count,
    interval,
    duration,
    ping_timeout,
    internet,
    external_ip,
    dns_name,
    tcp,
    http_url,
    only,
    skip,
    fmt,
    output,
    show_all,
    reasoning,
    no_color,
    jobs,
    no_host,
):
    """このPCと機器のネットワークを診断します / Diagnose this PC and devices.

    \b
    TARGET は  192.168.3.39  /  192.168.3.39:502  /  PLC1=192.168.3.39:modbus,4840
    終了コード: 0 = 正常, 1 = 異常あり, 2 = 注意のみ
    """
    from .. import report as report_mod
    from ..runner import run_check

    fmt = format_from_path(output, fmt)
    devices = _collect_devices(targets, device_files, ports)
    cfg = _build_config(external_ip, dns_name, tcp, http_url, count, internet=internet)
    if interval:
        cfg["interval"] = interval
    if ping_timeout:
        cfg["ping_timeout"] = ping_timeout
    if duration:
        cfg["count"] = max(1, int(duration / effective_interval(interval)))

    progress = Progress()
    on_probe = (
        (lambda name: progress(tr(f"確認中: {name}", f"checking {name}")))
        if progress.enabled
        else None
    )
    try:
        rep = run_check(
            devices,
            config=cfg,
            only=_split_csv(only),
            skip=_split_csv(skip),
            jobs=jobs,
            include_host=not no_host,
            progress=progress,
            on_probe=on_probe,
        )
    finally:
        progress.done()
    rep.settings["devices"] = len(devices)

    if fmt == "json":
        text = report_mod.render_check_json(rep)
    elif fmt == "html":
        text = report_mod.render_check_html(rep, show_all=True)
    elif fmt == "csv":
        text = report_mod.render_check_csv(rep)
    else:
        to_file = bool(output)
        text = report_mod.render_check_text(
            rep,
            color=False if to_file else color_wanted(no_color),
            verbose=reasoning,
            show_all=show_all,
            unicode=True if to_file else None,
        )
    write_output(text, output, fmt)
    sys.exit(rep.exit_code)


@cli.command()
@click.argument("host")
@click.option("--count", "-c", type=int, default=10, help=H("回数", "Number of pings."))
@click.option(
    "--interval",
    "-i",
    type=float,
    default=0.2,
    help=H("間隔[秒]", "Seconds between pings."),
)
@click.option(
    "--timeout",
    type=float,
    default=1.0,
    help=H("応答待ち[秒]", "Seconds to wait for each reply."),
)
@click.option("--json", "as_json", is_flag=True, help=H("JSONで出力", "Output JSON."))
def ping(host, count, interval, timeout, as_json):
    """pingの精密統計（損失の95%上限・中央値/p95/最大・ジッタ）/ Precise ping statistics.

    root権限不要。結果は保存しません。
    """
    import json

    from ..core import Status
    from ..measure import ping as measure_ping
    from ..probes._common import ping_summary
    from ..report.common import Style

    st = measure_ping(host, count=count, interval=interval, timeout=timeout)
    if as_json:
        click.echo(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0 if st.any_reply else 1)
    style = Style(color=color_wanted(False))
    if st.error:
        click.echo(f"{style.status(Status.SKIP)} {host}: {st.error}", err=True)
        sys.exit(1)
    status = (
        Status.OK
        if st.any_reply and not st.lost
        else (Status.WARN if st.any_reply else Status.FAIL)
    )
    click.echo(f"{style.status(status)} {host}: {ping_summary(st)}")
    if st.errors:
        for msg, n in list(st.errors.items())[:3]:
            click.echo(style.dim(f"    {msg} ×{n}"))
    sys.exit(0 if st.any_reply else 1)
