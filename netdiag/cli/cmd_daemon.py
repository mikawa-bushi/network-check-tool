"""Always-on commands: ``monitor``, ``watch``, ``status``, ``history``, ``prune``."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import click

from ..i18n import tr
from ._app import DEFAULT_CONFIG, H, _split_csv, cli

logger = logging.getLogger("netdiag.cli")


def _verdict(diag) -> str:
    return "OK" if diag.healthy else (diag.primary.cause if diag.primary else "WARN")


@cli.command()
@click.option(
    "--interval",
    default=60,
    type=int,
    help=H("診断の間隔[秒]", "Seconds between sweeps."),
)
@click.option("--external-ip", default="1.1.1.1")
@click.option(
    "--only",
    help=H(
        "毎回実行するチェック（カンマ区切り）",
        "Comma-separated probes to run each tick.",
    ),
)
def monitor(interval, external_ip, only):
    """定期的に診断して履歴DBに記録 / Periodically diagnose and record metrics."""
    from ..scheduler import Monitor

    only_list = _split_csv(only)
    cfg = dict(DEFAULT_CONFIG, external_ip=external_ip)

    def on_tick(diag):
        click.echo(f"[{time.strftime('%H:%M:%S')}] {_verdict(diag)}")

    mon = Monitor(config=cfg, interval=interval, only=only_list, on_tick=on_tick)
    click.echo(
        tr(
            f"{interval}秒ごとに監視中 — Ctrl-C で停止",
            f"monitoring every {interval}s — Ctrl-C to stop",
        )
    )
    try:
        mon.start(background=False)
    except KeyboardInterrupt:
        mon.stop()
        click.echo(tr("\n停止しました", "\nstopped"))


@cli.command()
@click.option(
    "--interval",
    default=30,
    type=int,
    help=H("確認の間隔[秒]", "Seconds between sweeps."),
)
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
@click.option(
    "--internet/--no-internet",
    default=True,
    help=H(
        "外部到達とDNSも見張る（閉じた設備ネットワークでは --no-internet）",
        "Also watch external reachability and DNS (use --no-internet on closed networks).",
    ),
)
@click.option(
    "--light-probes",
    default=None,
    help=H(
        "毎回の軽いチェック（既定: interface,gateway,external,dns）",
        "Comma-separated tier-1 probes (default: interface,gateway,external,dns).",
    ),
)
@click.option(
    "--alert-file",
    type=click.Path(),
    help=H(
        "アラートJSONL（既定: ~/.netdiag/alerts.jsonl）",
        "JSONL alert file (default: ~/.netdiag/alerts.jsonl).",
    ),
)
@click.option(
    "--state-file",
    type=click.Path(),
    help=H(
        "現在状態JSONの保存先（既定: ~/.netdiag/state.json）",
        "Where to write current state JSON (default: ~/.netdiag/state.json).",
    ),
)
@click.option(
    "--fail-threshold",
    default=2,
    type=int,
    help=H(
        "何回連続NGで『劣化』とするか（ばたつき防止）",
        "Consecutive bad ticks before declaring DEGRADED (flap damping).",
    ),
)
@click.option(
    "--recover-threshold",
    default=2,
    type=int,
    help=H(
        "何回連続OKで『回復』とするか",
        "Consecutive good ticks before declaring RECOVERED.",
    ),
)
@click.option(
    "--escalate/--no-escalate",
    default=True,
    help=H(
        "劣化時に一度だけ全項目を診断し原因を添付",
        "On degradation, run the full diagnosis once for the root cause.",
    ),
)
@click.option(
    "--webhook-url",
    default=None,
    help=H(
        "アラートをJSONでこのURLへPOST", "Also POST each alert as JSON to this URL."
    ),
)
@click.option(
    "--slack-url",
    default=None,
    help=H(
        "Slack受信Webhookへ1行で通知",
        "Also post a one-line alert to this Slack incoming-webhook URL.",
    ),
)
@click.option(
    "--desktop",
    is_flag=True,
    help=H(
        "デスクトップ通知も出す（notify-send / osascript）",
        "Also show OS desktop notifications (notify-send / osascript).",
    ),
)
@click.option(
    "--save/--no-save",
    default=True,
    help=H("測定値を履歴DBに記録", "Record metrics to history db."),
)
@click.option(
    "--once",
    is_flag=True,
    help=H(
        "1回だけ実行して終了（テスト用）", "Run a single tick and exit (for testing)."
    ),
)
def watch(
    interval,
    external_ip,
    dns_name,
    internet,
    light_probes,
    alert_file,
    state_file,
    fail_threshold,
    recover_threshold,
    escalate,
    webhook_url,
    slack_url,
    desktop,
    save,
    once,
):
    """常時監視し、状態が変わったときだけ通知 / Watch continuously; alert on change.

    A light loop watches reachability and DNS; when degradation is confirmed it
    escalates to a full diagnosis once and writes the root cause to the alert
    file. Recoveries are logged too, with how long the outage lasted.
    """
    from ..alerting import DesktopSink, FileAlertSink, MultiSink, SlackSink, WebhookSink
    from ..watch import LIGHT_PROBES, Watcher

    default_probes = (
        list(LIGHT_PROBES) if internet else ["interface", "gateway", "neighbor"]
    )
    probes = _split_csv(light_probes) or default_probes
    cfg = dict(
        DEFAULT_CONFIG, external_ip=external_ip, dns_name=dns_name, internet=internet
    )

    # The file sink is always present (it owns the on-disk JSONL/log); optional
    # push channels are added only when requested and fanned out via MultiSink.
    file_sink = FileAlertSink(jsonl_path=Path(alert_file) if alert_file else None)
    extra_sinks = []
    if webhook_url:
        extra_sinks.append(WebhookSink(webhook_url))
    if slack_url:
        extra_sinks.append(SlackSink(slack_url))
    if desktop:
        extra_sinks.append(DesktopSink())
    sink = file_sink if not extra_sinks else MultiSink([file_sink, *extra_sinks])
    state_path = (
        Path(state_file) if state_file else Path.home() / ".netdiag" / "state.json"
    )

    def on_tick(diag, transition):
        marker = ""
        if transition:
            marker = f"  <<< {transition.event.upper()}" + (
                f" ({transition.cause})" if transition.cause else ""
            )
        click.echo(f"[{time.strftime('%H:%M:%S')}] {_verdict(diag)}{marker}")

    watcher = Watcher(
        config=cfg,
        interval=interval,
        light_probes=probes,
        escalate=escalate,
        sink=sink,
        fail_threshold=fail_threshold,
        recover_threshold=recover_threshold,
        save=save,
        on_tick=on_tick,
        state_path=state_path,
    )

    if once:
        watcher.tick()
        watcher.stop()
        return

    channels = ", ".join(
        ["file"]
        + (["webhook"] if webhook_url else [])
        + (["slack"] if slack_url else [])
        + (["desktop"] if desktop else [])
    )
    click.echo(
        tr(
            f"{interval}秒ごとに監視中 — 通知先 {file_sink.jsonl_path} [{channels}]",
            f"watching every {interval}s — alerts → {file_sink.jsonl_path} [{channels}]",
        )
    )
    click.echo(
        tr(
            f"ばたつき防止: {fail_threshold}回連続NG → 劣化, {recover_threshold}回連続OK → 回復。Ctrl-C で停止。",
            f"flap damping: {fail_threshold} bad → DEGRADED, {recover_threshold} good → RECOVERED. Ctrl-C to stop.",
        )
    )
    try:
        watcher.start(background=False)
    except KeyboardInterrupt:
        watcher.stop()
        click.echo(tr("\n停止しました", "\nstopped"))


@cli.command()
@click.option(
    "--probe",
    default="external",
    help=H(
        "分析するチェック名（機器なら reach）", "Probe to analyse (reach for devices)."
    ),
)
@click.option(
    "--metric", default="rtt_ms", help=H("分析する指標", "Metric to analyse.")
)
@click.option(
    "--target",
    default=None,
    help=H("対象（機器のIPなど）", "Target (e.g. a device IP)."),
)
@click.option("--hours", default=24, type=int)
def history(probe, metric, target, hours):
    """履歴から平常値・変化点・ばたつきを分析 / Baseline and change-point analysis."""
    from ..diagnosis import (
        classify,
        compute_baseline,
        detect_changepoint,
        detect_flapping,
    )
    from ..storage import Storage

    since = time.time() - hours * 3600
    with Storage() as st:
        series = st.series(probe, metric, target=target, since=since)
    if not series:
        click.echo(
            tr(
                f"直近{hours}時間の {probe}/{metric} の履歴がありません（先に 'netdiag check --save' や 'netdiag monitor' を実行）。",
                f"no history for {probe}/{metric} in the last {hours}h "
                f"(run 'netdiag check --save' or 'netdiag monitor' first).",
            )
        )
        return
    values = [v for _, v in series]
    base = compute_baseline(values)
    click.echo(
        tr(
            f"{probe}/{metric}: {hours}時間で {len(values)} サンプル",
            f"{probe}/{metric}: {len(values)} samples over {hours}h",
        )
    )
    if base:
        click.echo(
            tr(
                f"  平常値: 中央値 {base.median:.2f}, MAD {base.mad:.2f}",
                f"  baseline: median {base.median:.2f}, MAD {base.mad:.2f}",
            )
        )
        latest = values[-1]
        click.echo(
            tr(
                f"  最新:   {latest:.2f} ({classify(latest, base)})",
                f"  latest:   {latest:.2f} ({classify(latest, base)})",
            )
        )
    cp = detect_changepoint(series)
    if cp:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cp.timestamp))
        direction = tr("悪化", "worsened") if cp.worsened else tr("改善", "improved")
        click.echo(
            tr(
                f"  変化点: {when} に{direction}（{cp.before_mean:.1f} → {cp.after_mean:.1f}）",
                f"  change point: {direction} at {when} ({cp.before_mean:.1f} → {cp.after_mean:.1f})",
            )
        )
    flap = detect_flapping(series)
    if flap:
        click.echo(
            tr(
                f"  ばたつき検出: サンプルの {flap * 100:.0f}% で状態が入れ替わっています",
                f"  flapping detected: {flap*100:.0f}% of samples alternate state",
            )
        )


@cli.command()
@click.option(
    "--state-file",
    type=click.Path(),
    help=H(
        "watch が書く状態JSON",
        "State JSON written by 'watch' (default: ~/.netdiag/state.json).",
    ),
)
@click.option(
    "--alert-file",
    type=click.Path(),
    help=H("アラートJSONL", "Alert JSONL (default: ~/.netdiag/alerts.jsonl)."),
)
@click.option(
    "--limit",
    default=5,
    type=int,
    help=H("表示する最近のアラート数", "Recent alerts to show."),
)
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def status(state_file, alert_file, limit, fmt):
    """watch の現在状態と最近のアラート / Current watcher state and recent alerts.

    Reads the files 'netdiag watch' maintains; works even if the watcher runs in
    another process or under systemd.
    """
    from ..storage import Storage

    base = Path.home() / ".netdiag"
    state_path = Path(state_file) if state_file else base / "state.json"
    alert_path = Path(alert_file) if alert_file else base / "alerts.jsonl"

    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            state = None

    recent = []
    if alert_path.exists():
        try:
            lines = (
                alert_path.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()
            )
            for ln in lines[-limit:]:
                recent.append(json.loads(ln))
        except (OSError, json.JSONDecodeError):
            pass

    counts = {}
    try:
        with Storage() as st:
            counts = st.counts()
    except Exception:
        # History is optional context for `status`; don't fail the command, but
        # log so a broken/locked history db is observable with -v.
        logger.warning("could not read history db counts", exc_info=True)

    if fmt == "json":
        click.echo(
            json.dumps(
                {"state": state, "recent_alerts": recent, "history": counts},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if state is None:
        click.echo(
            tr(
                "watch の状態がありません — 'netdiag watch' で開始してください。",
                "no watcher state found — start one with 'netdiag watch'.",
            )
        )
    else:
        s = state.get("state", "?")
        mark = {"healthy": "✔", "degraded": "✘", "unknown": "—"}.get(s, "?")
        click.echo(
            tr(
                f"{mark} 状態: {s.upper()}（{state.get('since_iso', '?')} から）",
                f"{mark} state: {s.upper()}  (since {state.get('since_iso', '?')})",
            )
        )
        if state.get("cause"):
            click.echo(
                tr(
                    f"  現在の原因: {state['cause']}",
                    f"  current cause: {state['cause']}",
                )
            )
    if counts:
        click.echo(
            tr(
                f"  履歴: 生データ {counts.get('samples', 0)} 件, 集約 {counts.get('rollups', 0)} 件",
                f"  history: {counts.get('samples', 0)} raw samples, {counts.get('rollups', 0)} rollups",
            )
        )
    if recent:
        click.echo(tr("\n最近のアラート:", "\nrecent alerts:"))
        for a in recent:
            extra = f" — {a.get('cause')}" if a.get("cause") else ""
            click.echo(f"  [{a.get('iso', '?')}] {a.get('event', '?').upper()}{extra}")


@cli.command()
@click.option(
    "--raw-days",
    default=7,
    type=float,
    help=H("生データを残す日数", "Keep full-resolution samples for this many days."),
)
@click.option(
    "--rollup-days",
    default=90,
    type=float,
    help=H("1時間集約を残す日数", "Keep hourly rollups for this many days."),
)
def prune(raw_days, rollup_days):
    """古い生データを1時間集約にまとめ、古い集約を削除 / Downsample and drop old history."""
    from ..storage import Storage

    with Storage() as st:
        before = st.counts()
        result = st.retention(raw_days=raw_days, rollup_days=rollup_days)
        after = st.counts()
    click.echo(
        tr(
            f"生データ {result['raw_collapsed']} 行を集約、古い集約 {result['rollups_dropped']} 行を削除",
            f"collapsed {result['raw_collapsed']} raw rows, dropped {result['rollups_dropped']} old rollups",
        )
    )
    click.echo(
        f"  samples: {before['samples']} → {after['samples']}, "
        f"rollups: {before['rollups']} → {after['rollups']}"
    )
