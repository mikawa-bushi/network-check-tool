"""``netdiag scan``: list the devices on a subnet (ICMP + ARP only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import click

from ..core import ExitCode, InputError
from ..i18n import tr
from ..inventory import devices_to_csv, parse_ports
from ._app import H, Progress, cli, color_wanted


@cli.command()
@click.argument("networks", nargs=-1, metavar="[NETWORK]...")
@click.option(
    "--save",
    type=click.Path(dir_okay=False),
    help=H(
        "見つかった機器を台帳CSVとして保存（名前を書き足して check -f に使えます）",
        "Save found devices as a device-list CSV (add names, then use with check -f).",
    ),
)
@click.option(
    "--count",
    type=int,
    default=2,
    help=H("1アドレスあたりのping回数", "Pings per address."),
)
@click.option(
    "--timeout",
    type=float,
    default=1.0,
    help=H("応答待ち[秒]", "Seconds to wait for replies."),
)
@click.option(
    "-p",
    "--port",
    "ports",
    multiple=True,
    help=H(
        "見つかった機器でだけ確認するTCPポート（指定時のみ。既定はポートに触れません）",
        "TCP port to check on found devices only (off by default).",
    ),
)
@click.option(
    "--names",
    is_flag=True,
    help=H("逆引きでホスト名も調べる", "Also reverse-resolve host names."),
)
@click.option(
    "--max-hosts",
    type=int,
    default=512,
    help=H("走査するアドレス数の上限", "Upper bound on addresses to sweep."),
)
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json", "csv"]), default="text"
)
@click.option(
    "--no-color", is_flag=True, help=H("色を付けない", "Disable ANSI colour.")
)
def scan(networks, save, count, timeout, ports, names, max_hosts, fmt, no_color):
    """同じネットワーク上の機器を一覧します / Discover devices on a subnet.

    \b
    NETWORK: 192.168.3.0/24, 192.168.3.10-60, 192.168.3.39（省略時はこのPCのサブネット）
    ping（ICMP）とARPだけを使い、指定しない限りポートには接続しません。
    """
    from .. import netinfo
    from ..scan import default_networks, parse_networks
    from ..scan import scan as run_scan

    port_list = parse_ports(list(ports)) if ports else []
    notes: List[str] = []
    nets = parse_networks(networks) if networks else None
    if save:
        _check_save_path(Path(save))
    # The default networks are bounded per NIC; a range the user named is
    # bounded as a whole.
    limit: Optional[int] = max_hosts
    if nets is None:
        nets, notes = default_networks(netinfo.snapshot(), max_hosts=max_hosts)
        limit = None
    if not nets:
        click.echo(
            tr(
                "走査できるネットワークがありません（IPv4を持つNICがない）",
                "no network to scan (no IPv4 NIC)",
            ),
            err=True,
        )
        sys.exit(ExitCode.INCOMPLETE)

    progress = Progress()
    progress(
        tr(
            f"{', '.join(map(str, nets))} を走査中…",
            f"scanning {', '.join(map(str, nets))}…",
        )
    )
    try:
        result = run_scan(
            nets,
            count=count,
            timeout=timeout,
            ports=port_list,
            resolve_names=names,
            max_hosts=limit,
        )
    finally:
        progress.done()
    result.notes.extend(notes)

    if fmt == "json":
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif fmt == "csv":
        click.echo(devices_to_csv(result.to_devices()), nl=False)
    else:
        _print_text(result, no_color)
    if save:
        _save(Path(save), devices_to_csv(result.to_devices()))


def _check_save_path(path: Path) -> None:
    """Refuse a --save target before the sweep, so no scan is wasted."""
    if path.exists():
        raise InputError(
            tr(
                f"{path} は既にあります。台帳を上書きしないため保存しません。"
                "別のファイル名を --save に指定してください",
                f"{path} already exists, and netdiag never overwrites a device "
                "list. Give --save a new file name.",
            )
        )
    if not path.parent.is_dir():
        raise InputError(
            tr(
                f"保存先のフォルダ {path.parent} がありません",
                f"the folder {path.parent} for --save does not exist",
            )
        )


def _save(path: Path, text: str) -> None:
    """Write a new device list; the results are already on screen."""
    try:
        with path.open("x", encoding="utf-8-sig", newline="") as f:
            f.write(text)
    except OSError as e:
        click.echo(
            tr(
                f"台帳CSVを保存できませんでした（{path}: {e.strerror or e}）。"
                "結果は上に表示したとおりです",
                f"could not save the device list ({path}: {e.strerror or e}); "
                "the results are shown above",
            ),
            err=True,
        )
        sys.exit(ExitCode.USAGE)
    click.echo(
        tr(f"台帳CSVを保存しました: {path}", f"device list saved to {path}"),
        err=True,
    )


def _print_text(result, no_color: bool) -> None:
    from ..core import Status
    from ..report.common import Style, pad, width

    st = Style(color=color_wanted(no_color))
    found = len(result.hits)
    silent = sum(1 for h in result.hits if h.arp_only)
    click.echo(
        st.bold(
            tr(
                f"{', '.join(result.networks)}: {result.probed} アドレス中 {found} 台を検出"
                + (f"（うち ping無応答 {silent} 台）" if silent else ""),
                f"{', '.join(result.networks)}: {found} of {result.probed} addresses answered"
                + (f" ({silent} silent to ping)" if silent else ""),
            )
        )
    )
    for n in result.notes:
        click.echo(st.dim(tr(f"  範囲を縮小: {n}", f"  range narrowed: {n}")))
    header = [
        tr("IPアドレス", "IP address"),
        tr("MACアドレス", "MAC"),
        tr("ベンダー", "Vendor"),
        tr("応答", "Reply"),
        "RTT",
        tr("備考", "Note"),
    ]
    rows = []
    for h in result.hits:
        reply = (
            tr("ping", "ping")
            if h.replied
            else (tr("ARPのみ", "ARP only") if h.arp_only else "")
        )
        note = []
        if h.is_self:
            note.append(tr("このPC", "this PC"))
        if h.is_gateway:
            note.append(tr("ゲートウェイ", "gateway"))
        if h.hostname:
            note.append(h.hostname)
        if h.ports:
            note.append(" ".join(f"{p}:{s}" for p, s in h.ports.items()))
        rtt = f"{h.rtt_ms:.2f}ms" if h.rtt_ms is not None else "—"
        rows.append(
            [h.ip, h.mac or "—", (h.vendor or "")[:28], reply, rtt, ", ".join(note)]
        )
    widths = [max(width(r[i]) for r in rows + [header]) for i in range(len(header))]
    click.echo(
        "  "
        + st.dim(
            "  ".join(pad(header[i], widths[i]) for i in range(len(header))).rstrip()
        )
    )
    for r in rows:
        click.echo(
            "  " + "  ".join(pad(r[i], widths[i]) for i in range(len(r))).rstrip()
        )
    for ip, macs in result.duplicates:
        seen = f" (MAC {', '.join(macs)})" if macs else ""
        click.echo(
            st.status(
                Status.FAIL,
                tr(
                    f"  IP重複の疑い: {ip} が1回のpingに複数回応答{seen}",
                    f"  possible IP conflict: {ip} answered one ping more than once{seen}",
                ),
            )
        )
