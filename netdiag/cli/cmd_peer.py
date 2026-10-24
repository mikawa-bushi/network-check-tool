"""``netdiag peer``: two-host LAN quality test for closed/offline networks."""

from __future__ import annotations

import sys

import click

from ..i18n import tr
from ._app import H, cli


@cli.group()
def peer():
    """2台のPC間でLAN品質（ロス・ジッタ・スループット）を測定 / Two-host LAN test."""


@peer.command("serve")
@click.option(
    "--host", default="0.0.0.0", help=H("待ち受けアドレス", "Address to listen on.")
)
@click.option("--port", default=47600, type=int)
def peer_serve(host, port):
    """相手側で応答役を起動（Ctrl-C で停止）/ Run the responder on the other end."""
    from ..peer import PeerServer

    click.echo(
        tr(
            f"応答役を {host}:{port} (UDP+TCP) で起動しました。Ctrl-C で停止。",
            f"peer responder listening on {host}:{port} (UDP+TCP). Ctrl-C to stop.",
        )
    )
    PeerServer(host, port).serve_forever()


@peer.command("test")
@click.argument("address")
@click.option("--port", default=47600, type=int)
@click.option(
    "--count",
    default=50,
    type=int,
    help=H("ロス/ジッタ測定のUDP回数", "UDP probes for loss/jitter."),
)
@click.option("--throughput/--no-throughput", default=True)
def peer_test(address, port, count, throughput):
    """ADDRESS の応答役に対してロス・ジッタ・スループットを測定 / Measure against a peer."""
    from ..measure import loss_upper_bound_pct
    from ..peer import measure_loss_jitter, measure_throughput

    rep = measure_loss_jitter(address, port=port, count=count)
    if rep.error:
        click.echo(f"✘ {address}:{port} — {rep.error}", err=True)
        sys.exit(1)
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
    if throughput:
        mbps = measure_throughput(address, port=port)
        if mbps is not None:
            click.echo(
                tr(f"スループット: {mbps:.1f} Mbit/s", f"throughput: {mbps:.1f} Mbit/s")
            )
        else:
            click.echo(tr("スループット: 測定失敗", "throughput: measurement failed"))
    sys.exit(0 if rep.loss_pct < 5 else 1)
