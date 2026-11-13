"""``netdiag peer``: two-host LAN quality test for closed/offline networks."""

from __future__ import annotations

import socket
import sys

import click

from ..core import InputError, Status
from ..i18n import tr
from ..peer import DEFAULT_PORT
from ._app import H, cli

_PORT = click.IntRange(1, 65535)


@cli.group()
def peer():
    """2台のPC間でLAN品質（ロス・ジッタ・スループット）を測定 / Two-host LAN test."""


@peer.command("serve")
@click.option(
    "--host", default="0.0.0.0", help=H("待ち受けアドレス", "Address to listen on.")
)
@click.option("--port", default=DEFAULT_PORT, type=_PORT)
def peer_serve(host, port):
    """相手側で応答役を起動（Ctrl-C で停止）/ Run the responder on the other end."""
    from ..peer import PeerServer

    server = PeerServer(host, port)
    try:
        server.bind()
    except OSError as e:
        reason = e.strerror or str(e)
        raise InputError(
            tr(
                f"{host}:{port} で待ち受けできません（{reason}）。"
                "別の --port を指定するか、そのポートを使っているプログラムを止めてください。"
                "--host にはこのPCのアドレスを指定します。",
                f"cannot listen on {host}:{port} ({reason}). Choose another --port "
                "or stop the program using it; --host must be an address of this PC.",
            )
        ) from e
    click.echo(
        tr(
            f"応答役を {host}:{port} (UDP+TCP) で起動しました。Ctrl-C で停止。",
            f"peer responder listening on {host}:{port} (UDP+TCP). Ctrl-C to stop.",
        )
    )
    server.serve_forever()


def _no_reply_message(address: str, port: int, refused: bool) -> str:
    if refused:
        return tr(
            f"? {address} は応答しましたが、UDP {port} で netdiag の応答役が待ち受けていません。"
            f"相手のPCで「netdiag peer serve --port {port}」を起動してください。",
            f"? {address} answered, but no netdiag peer responder listens on UDP {port}. "
            f"Start 'netdiag peer serve --port {port}' on that PC.",
        )
    return tr(
        f"? {address}:{port} から応答がありません。次のどれかです: 相手のPCで "
        "netdiag peer serve（0.6.0 以降）が動いていない、相手のファイアウォールが "
        f"UDP {port} を止めている、LAN の経路が切れている。経路は「netdiag ping {address}」で確認できます。",
        f"? no reply from {address}:{port}. Either 'netdiag peer serve' (0.6.0 or "
        f"later) is not running there, its firewall blocks UDP {port}, or the LAN "
        f"path is down. Check the path with 'netdiag ping {address}'.",
    )


@peer.command("test")
@click.argument("address")
@click.option("--port", default=DEFAULT_PORT, type=_PORT)
@click.option(
    "--count",
    default=50,
    type=click.IntRange(min=1),
    help=H("ロス/ジッタ測定のUDP回数", "UDP probes for loss/jitter."),
)
@click.option("--throughput/--no-throughput", default=True)
def peer_test(address, port, count, throughput):
    """ADDRESS の応答役に対してロス・ジッタ・スループットを測定 / Measure against a peer.

    終了コード: 0 = 正常, 1 = ロス 5% 以上・経路なし, 3 = 測定できなかった
    """
    from ..measure import loss_upper_bound_pct
    from ..peer import measure_loss_jitter, measure_throughput

    try:
        rep = measure_loss_jitter(address, port=port, count=count)
    except socket.gaierror as e:
        raise InputError(
            tr(
                f"{address} のアドレスがわかりません（{e.strerror or e}）",
                f"cannot resolve {address} ({e.strerror or e})",
            )
        ) from e
    if rep.error:
        click.echo(f"✘ {address}:{port} — {rep.error}", err=True)
        sys.exit(Status.FAIL.exit_code)
    if rep.received == 0:
        click.echo(_no_reply_message(address, port, rep.refused))
        sys.exit(Status.UNKNOWN.exit_code)
    upper = loss_upper_bound_pct(rep.sent - rep.received, rep.sent)
    bound = (
        f" (95% ≤ {upper:.1f}%)"
        if upper is not None and rep.received == rep.sent
        else ""
    )
    click.echo(
        tr(
            f"損失:   {rep.loss_pct:.1f}%{bound}  （{rep.received}/{rep.sent} 応答）",
            f"loss: {rep.loss_pct:.1f}%{bound}  ({rep.received}/{rep.sent} returned)",
        )
    )
    if rep.avg_rtt_ms is not None:
        click.echo(
            tr(
                f"RTT:    平均 {rep.avg_rtt_ms:.2f}ms  ジッタ {rep.jitter_ms or 0:.2f}ms",
                f"rtt:  avg {rep.avg_rtt_ms:.2f}ms  jitter {rep.jitter_ms or 0:.2f}ms",
            )
        )
    statuses = [Status.OK if rep.loss_pct < 5 else Status.FAIL]
    if throughput:
        mbps = measure_throughput(address, port=port)
        if mbps is not None:
            click.echo(
                tr(f"スループット: {mbps:.1f} Mbit/s", f"throughput: {mbps:.1f} Mbit/s")
            )
        else:
            statuses.append(Status.UNKNOWN)
            click.echo(
                tr(
                    f"スループット: 測定できませんでした（TCP {port} に接続できない）",
                    f"throughput: not measured (cannot connect to TCP {port})",
                )
            )
    sys.exit(Status.worst(statuses).exit_code)
