"""``netdiag`` command-line interface.

Conclusion-first diagnosis with a low barrier: ``netdiag check`` runs the whole
layered sweep and prints the root cause and next steps. Everything else
(explain, snapshot, peer, monitor, scenario) supports that core workflow.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import click

from . import __version__
from . import probes as _probes  # noqa: F401  -- registers built-in probes
from . import report as report_mod
from .core import Status, all_probes, get_probe
from .diagnosis import (
    Engine,
    classify,
    compute_baseline,
    detect_changepoint,
    detect_flapping,
)
from .plugins import load_plugins
from .storage import Storage

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configure the ``netdiag`` logger for CLI use (library code never does this).

    Quiet by default: with neither ``verbose`` nor ``log_file`` set, no handler is
    attached and the package's NullHandler keeps things silent. ``verbose`` lowers
    the level to DEBUG and logs to stderr; ``log_file`` writes to that file instead.
    """
    if not verbose and not log_file:
        return
    lg = logging.getLogger("netdiag")
    # Idempotent: if a real (non-Null) handler is already attached, don't stack
    # another one (avoids duplicate lines / leaked file handles on repeat calls).
    if any(not isinstance(h, logging.NullHandler) for h in lg.handlers):
        return
    lg.setLevel(logging.DEBUG if verbose else logging.WARNING)
    handler: logging.Handler = (
        logging.FileHandler(log_file) if log_file else logging.StreamHandler()
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    lg.addHandler(handler)


def _resolve_version() -> str:
    """Installed package version, falling back to the in-tree __version__.

    Lets ``netdiag --version`` work both when pip-installed and when run from a
    source checkout (where the distribution metadata isn't present).
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("network-check-tool")
        except PackageNotFoundError:
            return __version__
    except ImportError:  # pragma: no cover
        return __version__


DEFAULT_CONFIG = {
    "external_ip": "1.1.1.1",
    "dns_name": "example.com",
    "count": 4,
}


def _build_config(external_ip, dns_name, tcp, http_url, count) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if external_ip:
        cfg["external_ip"] = external_ip
    if dns_name:
        cfg["dns_name"] = dns_name
    if tcp:
        cfg["tcp_targets"] = list(tcp)
    if http_url:
        cfg["http_url"] = http_url
        cfg.setdefault("tls_target", http_url)
    if count:
        cfg["count"] = count
    return cfg


def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated CLI option into a stripped list, or None if unset."""
    return [s.strip() for s in value.split(",")] if value else None


@click.group()
@click.version_option(version=_resolve_version(), prog_name="netdiag")
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Verbose netdiag logging to stderr (or --log-file). Quiet by default.",
)
@click.option(
    "--log-file",
    type=click.Path(),
    help="Write netdiag logs to this file instead of stderr.",
)
def cli(verbose: bool, log_file: Optional[str]) -> None:
    """netdiag — local-first network & OS trouble isolation."""
    _setup_logging(verbose=verbose, log_file=log_file)
    load_plugins()


# --- check ------------------------------------------------------------------
@cli.command()
@click.option("--external-ip", help="Off-LAN IP to test reachability (no DNS).")
@click.option("--dns-name", help="Hostname to resolve for the DNS check.")
@click.option(
    "--tcp",
    multiple=True,
    metavar="HOST:PORT",
    help="TCP service(s) to test. Repeatable.",
)
@click.option("--http-url", help="URL for the HTTP/TLS phase-timing check.")
@click.option("--count", type=int, default=4, help="Ping count per target.")
@click.option("--only", help="Comma-separated probe names to run exclusively.")
@click.option("--skip", help="Comma-separated probe names to skip.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json", "html"]), default="text"
)
@click.option("--output", type=click.Path(), help="Write the report to a file.")
@click.option(
    "--reasoning",
    is_flag=True,
    help="Show the engine's step-by-step reasoning in the report. "
    "(Distinct from the global -v/--verbose, which controls logging.)",
)
@click.option("--save/--no-save", default=False, help="Record metrics to history db.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour.")
@click.option(
    "--jobs",
    type=int,
    default=1,
    help="Run independent probes concurrently (1=serial default; 3-4 recommended).",
)
def check(
    external_ip,
    dns_name,
    tcp,
    http_url,
    count,
    only,
    skip,
    fmt,
    output,
    reasoning,
    save,
    no_color,
    jobs,
):
    """Run the full layered diagnosis and report the root cause."""
    cfg = _build_config(external_ip, dns_name, tcp, http_url, count)
    only_list = _split_csv(only)
    skip_list = _split_csv(skip)

    # Progress feedback for long sweeps -- only to stderr, only on a TTY, and only
    # for text output, so a redirected report (or json/html) stays clean.
    on_probe = None
    if fmt == "text" and sys.stderr.isatty():

        def on_probe(name):
            click.echo(f"\r\033[K… {name}", nl=False, err=True)

    engine = Engine(config=cfg, verbose=reasoning)
    diag = engine.run(only=only_list, skip=skip_list, jobs=jobs, on_probe=on_probe)
    if on_probe is not None:
        click.echo("\r\033[K", nl=False, err=True)  # clear the progress line

    if save:
        with Storage() as st:
            st.record_diagnosis(diag, target=cfg.get("external_ip"))

    if fmt == "json":
        text = report_mod.render_json(diag)
    elif fmt == "html":
        text = report_mod.render_html(diag)
    else:
        color = sys.stdout.isatty() and not no_color
        text = report_mod.render_terminal(diag, color=color, verbose=reasoning)

    if output:
        Path(output).write_text(text)
        click.echo(f"report written to {output}")
    else:
        click.echo(text)

    # Exit code: 0 healthy, 1 a cause was found, 2 only warnings.
    if diag.healthy:
        sys.exit(0)
    if diag.hypotheses and any(h.confidence != "low" for h in diag.hypotheses):
        sys.exit(1)
    sys.exit(2)


# --- ping (ad-hoc single target) -------------------------------------------
@cli.command()
@click.argument("host")
@click.option("--count", "-c", type=int, default=4)
def ping(host, count):
    """Ad-hoc ICMP ping of HOST (uses the system ping, no root needed)."""
    from .core import Context

    probe = get_probe("icmp")()
    ctx = Context({"target": host, "count": count})
    res = probe.run(ctx)
    click.echo(f"{res.status.symbol} {res.summary}")
    sys.exit(0 if res.status.ok else 1)


# --- explain (teaching mode) ------------------------------------------------
@cli.command()
@click.argument("probe_name", required=False)
def explain(probe_name):
    """Explain what a check does and the manual commands that do the same.

    With no argument, lists every probe. The goal is for users to learn to
    isolate faults by hand, not just read a verdict.
    """
    if not probe_name:
        click.echo("Available checks (run 'netdiag explain <name>' for detail):\n")
        for name in sorted(all_probes()):
            cls = all_probes()[name]
            click.echo(f"  {name.ljust(12)} [{cls.layer.ljust(6)}] {cls.what}")
        return
    cls = get_probe(probe_name)
    if not cls:
        click.echo(f"unknown probe: {probe_name}", err=True)
        sys.exit(1)
    click.echo(f"# {probe_name} — {cls.title}  (layer {cls.layer})\n")
    click.echo(f"What:  {cls.what}")
    click.echo(f"Why:   {cls.why}")
    if cls.depends_on:
        click.echo(f"Depends on: {', '.join(cls.depends_on)}")
    if cls.requires_root:
        click.echo("Note:  needs root for full detail (degrades to skip otherwise).")
    if cls.manual_commands:
        click.echo("\nDo it yourself:")
        for cmd in cls.manual_commands:
            click.echo(f"  $ {cmd}")


# --- list-probes ------------------------------------------------------------
@cli.command(name="list-probes")
def list_probes():
    """List registered probes (including plugins) in dependency layers."""
    by_layer: dict = {}
    for name, cls in all_probes().items():
        by_layer.setdefault(cls.layer, []).append(name)
    for layer in ("L1", "L2", "L3", "L4", "L7", "SYSTEM", "?"):
        if layer in by_layer:
            click.echo(f"{layer}: {', '.join(sorted(by_layer[layer]))}")


# --- snapshot ---------------------------------------------------------------
@cli.group()
def snapshot():
    """Save and diff network-configuration snapshots ('what changed?')."""


@snapshot.command("save")
@click.argument("name", default="default")
def snapshot_save(name):
    from . import snapshot as snap

    path = snap.save(name)
    click.echo(f"snapshot '{name}' saved to {path}")


@snapshot.command("list")
def snapshot_list():
    from . import snapshot as snap

    names = snap.list_snapshots()
    click.echo("\n".join(names) if names else "(no snapshots saved)")


@snapshot.command("diff")
@click.argument("name", default="default")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def snapshot_diff(name, fmt):
    """Diff a saved snapshot against the current configuration."""
    from . import snapshot as snap

    baseline = snap.load(name)
    if baseline is None:
        click.echo(
            f"no snapshot named '{name}' — run 'netdiag snapshot save {name}' "
            f"while the network works.",
            err=True,
        )
        sys.exit(1)
    current = snap.capture()
    d = snap.diff_snapshots(baseline, current)

    if fmt == "json":
        # Same exit-code contract as the text form: 0 = no change, 2 = changed.
        click.echo(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(2 if d.any_change else 0)

    if not d.any_change:
        click.echo(
            f"no configuration changes since '{name}' " f"(saved {d.baseline_time})."
        )
        return
    click.echo(f"changes since '{name}' (saved {d.baseline_time}):\n")
    for sd in d.changed:
        click.echo(f"■ {sd.label} — {sd.why}")
        for line in sd.removed:
            click.echo(f"    - {line}")
        for line in sd.added:
            click.echo(f"    + {line}")
        click.echo("")
    sys.exit(2)


# --- peer -------------------------------------------------------------------
@cli.group()
def peer():
    """Two-host LAN quality test for closed/offline networks."""


@peer.command("serve")
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=47600, type=int)
def peer_serve(host, port):
    """Run the responder on this machine (the 'other end')."""
    from .peer import PeerServer

    click.echo(f"peer responder listening on {host}:{port} (UDP+TCP). Ctrl-C to stop.")
    PeerServer(host, port).serve_forever()


@peer.command("test")
@click.argument("address")
@click.option("--port", default=47600, type=int)
@click.option("--count", default=50, type=int, help="UDP probes for loss/jitter.")
@click.option("--throughput/--no-throughput", default=True)
def peer_test(address, port, count, throughput):
    """Measure loss, jitter and throughput against a peer responder at ADDRESS."""
    from .peer import measure_loss_jitter, measure_throughput

    rep = measure_loss_jitter(address, port=port, count=count)
    if rep.error:
        click.echo(f"✘ {address}:{port} — {rep.error}", err=True)
        sys.exit(1)
    click.echo(f"loss: {rep.loss_pct:.1f}%  ({rep.received}/{rep.sent} returned)")
    if rep.avg_rtt_ms is not None:
        click.echo(f"rtt:  avg {rep.avg_rtt_ms:.2f}ms  jitter {rep.jitter_ms:.2f}ms")
    if throughput:
        mbps = measure_throughput(address, port=port)
        if mbps is not None:
            click.echo(f"throughput: {mbps:.1f} Mbit/s")
        else:
            click.echo("throughput: measurement failed")
    sys.exit(0 if rep.loss_pct < 5 else 1)


# --- scenario ---------------------------------------------------------------
@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option(
    "--reasoning",
    is_flag=True,
    help="Show the engine's step-by-step reasoning in the report.",
)
@click.option("--no-color", is_flag=True, help="Disable ANSI colour.")
def scenario(path, fmt, reasoning, no_color):
    """Run a declarative YAML/JSON diagnosis scenario."""
    from .scenario import load_scenario, merged_config

    sc = load_scenario(path)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(merged_config(sc))
    engine = Engine(config=cfg, verbose=reasoning)
    diag = engine.run(only=sc.probe_names)
    click.echo(f"# {sc.name}")
    if sc.description:
        click.echo(sc.description + "\n")
    if fmt == "json":
        click.echo(report_mod.render_json(diag))
    else:
        color = sys.stdout.isatty() and not no_color
        click.echo(report_mod.render_terminal(diag, color=color, verbose=reasoning))
    sys.exit(0 if diag.healthy else 1)


# --- monitor ----------------------------------------------------------------
@cli.command()
@click.option("--interval", default=60, type=int, help="Seconds between sweeps.")
@click.option("--external-ip", default="1.1.1.1")
@click.option("--only", help="Comma-separated probes to run each tick.")
def monitor(interval, external_ip, only):
    """Continuously run the diagnosis and record metrics to history."""
    from .scheduler import Monitor

    only_list = _split_csv(only)
    cfg = dict(DEFAULT_CONFIG, external_ip=external_ip)

    def on_tick(diag):
        stamp = time.strftime("%H:%M:%S")
        verdict = (
            "OK" if diag.healthy else (diag.primary.cause if diag.primary else "WARN")
        )
        click.echo(f"[{stamp}] {verdict}")

    mon = Monitor(config=cfg, interval=interval, only=only_list, on_tick=on_tick)
    click.echo(f"monitoring every {interval}s — Ctrl-C to stop")
    try:
        mon.start(background=False)
    except KeyboardInterrupt:
        mon.stop()
        click.echo("\nstopped")


# --- watch (always-on, alert on change) -------------------------------------
@cli.command()
@click.option("--interval", default=30, type=int, help="Seconds between sweeps.")
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
@click.option(
    "--light-probes",
    default=None,
    help="Comma-separated tier-1 probes (default: interface,gateway,external,dns).",
)
@click.option(
    "--alert-file",
    type=click.Path(),
    help="JSONL alert file (default: ~/.netdiag/alerts.jsonl).",
)
@click.option(
    "--state-file",
    type=click.Path(),
    help="Where to write current state JSON (default: ~/.netdiag/state.json).",
)
@click.option(
    "--fail-threshold",
    default=2,
    type=int,
    help="Consecutive bad ticks before declaring DEGRADED (flap damping).",
)
@click.option(
    "--recover-threshold",
    default=2,
    type=int,
    help="Consecutive good ticks before declaring RECOVERED.",
)
@click.option(
    "--escalate/--no-escalate",
    default=True,
    help="On degradation, run the full diagnosis once for the root cause.",
)
@click.option(
    "--webhook-url", default=None, help="Also POST each alert as JSON to this URL."
)
@click.option(
    "--slack-url",
    default=None,
    help="Also post a one-line alert to this Slack incoming-webhook URL.",
)
@click.option(
    "--desktop",
    is_flag=True,
    help="Also show OS desktop notifications (notify-send / osascript).",
)
@click.option("--save/--no-save", default=True, help="Record metrics to history db.")
@click.option("--once", is_flag=True, help="Run a single tick and exit (for testing).")
def watch(
    interval,
    external_ip,
    dns_name,
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
    """Run continuously; alert (file/JSONL) only when the verdict changes.

    A light loop watches reachability and DNS; when degradation is confirmed it
    escalates to a full diagnosis once and writes the root cause to the alert
    file. Recoveries are logged too, with how long the outage lasted.
    """
    from pathlib import Path

    from .alerting import (
        DesktopSink,
        FileAlertSink,
        MultiSink,
        SlackSink,
        WebhookSink,
    )
    from .watch import LIGHT_PROBES, Watcher

    probes = _split_csv(light_probes) or list(LIGHT_PROBES)
    cfg = dict(DEFAULT_CONFIG, external_ip=external_ip, dns_name=dns_name)

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
    default_state = Path.home() / ".netdiag" / "state.json"
    state_path = Path(state_file) if state_file else default_state

    def on_tick(diag, transition):
        stamp = time.strftime("%H:%M:%S")
        verdict = (
            "OK" if diag.healthy else (diag.primary.cause if diag.primary else "WARN")
        )
        marker = ""
        if transition:
            marker = f"  <<< {transition.event.upper()}" + (
                f" ({transition.cause})" if transition.cause else ""
            )
        click.echo(f"[{stamp}] {verdict}{marker}")

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
        f"watching every {interval}s — alerts → {file_sink.jsonl_path} [{channels}]"
    )
    click.echo(
        f"flap damping: {fail_threshold} bad → DEGRADED, "
        f"{recover_threshold} good → RECOVERED. Ctrl-C to stop."
    )
    try:
        watcher.start(background=False)
    except KeyboardInterrupt:
        watcher.stop()
        click.echo("\nstopped")


# --- history / baseline -----------------------------------------------------
@cli.command()
@click.option("--probe", default="external", help="Probe to analyse.")
@click.option("--metric", default="rtt_ms", help="Metric to analyse.")
@click.option("--target", default=None)
@click.option("--hours", default=24, type=int)
def history(probe, metric, target, hours):
    """Show baseline and change-point analysis from recorded history."""
    since = time.time() - hours * 3600
    with Storage() as st:
        series = st.series(probe, metric, target=target, since=since)
    if not series:
        click.echo(
            f"no history for {probe}/{metric} in the last {hours}h "
            f"(run 'netdiag check --save' or 'netdiag monitor' first)."
        )
        return
    values = [v for _, v in series]
    base = compute_baseline(values)
    click.echo(f"{probe}/{metric}: {len(values)} samples over {hours}h")
    if base:
        click.echo(f"  baseline: median {base.median:.2f}, MAD {base.mad:.2f}")
        latest = values[-1]
        click.echo(f"  latest:   {latest:.2f} ({classify(latest, base)})")
    cp = detect_changepoint(series)
    if cp:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cp.timestamp))
        direction = "worsened" if cp.worsened else "improved"
        click.echo(
            f"  change point: {direction} at {when} "
            f"({cp.before_mean:.1f} → {cp.after_mean:.1f})"
        )
    flap = detect_flapping(series)
    if flap:
        click.echo(f"  flapping detected: {flap*100:.0f}% of samples alternate state")


# --- status (live view of the always-on watcher) ----------------------------
@cli.command()
@click.option(
    "--state-file",
    type=click.Path(),
    help="State JSON written by 'watch' (default: ~/.netdiag/state.json).",
)
@click.option(
    "--alert-file",
    type=click.Path(),
    help="Alert JSONL (default: ~/.netdiag/alerts.jsonl).",
)
@click.option("--limit", default=5, type=int, help="Recent alerts to show.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def status(state_file, alert_file, limit, fmt):
    """Show the current watcher state and the most recent alerts.

    Reads the files 'netdiag watch' maintains; works even if the watcher runs in
    another process or under systemd.
    """
    import json
    from pathlib import Path

    base = Path.home() / ".netdiag"
    state_path = Path(state_file) if state_file else base / "state.json"
    alert_path = Path(alert_file) if alert_file else base / "alerts.jsonl"

    state = None
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = None

    recent = []
    if alert_path.exists():
        try:
            lines = alert_path.read_text().strip().splitlines()
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
        click.echo("no watcher state found — start one with 'netdiag watch'.")
    else:
        s = state.get("state", "?")
        mark = {"healthy": "✔", "degraded": "✘", "unknown": "—"}.get(s, "?")
        click.echo(f"{mark} state: {s.upper()}  (since {state.get('since_iso', '?')})")
        if state.get("cause"):
            click.echo(f"  current cause: {state['cause']}")
    if counts:
        click.echo(
            f"  history: {counts.get('samples', 0)} raw samples, "
            f"{counts.get('rollups', 0)} rollups"
        )
    if recent:
        click.echo("\nrecent alerts:")
        for a in recent:
            extra = f" — {a.get('cause')}" if a.get("cause") else ""
            click.echo(f"  [{a.get('iso', '?')}] {a.get('event', '?').upper()}{extra}")


# --- prune (manual retention) -----------------------------------------------
@cli.command()
@click.option(
    "--raw-days",
    default=7,
    type=float,
    help="Keep full-resolution samples for this many days.",
)
@click.option(
    "--rollup-days",
    default=90,
    type=float,
    help="Keep hourly rollups for this many days.",
)
def prune(raw_days, rollup_days):
    """Downsample old raw samples to hourly rollups and drop ancient rollups."""
    with Storage() as st:
        before = st.counts()
        result = st.retention(raw_days=raw_days, rollup_days=rollup_days)
        after = st.counts()
    click.echo(
        f"collapsed {result['raw_collapsed']} raw rows, "
        f"dropped {result['rollups_dropped']} old rollups"
    )
    click.echo(
        f"  samples: {before['samples']} → {after['samples']}, "
        f"rollups: {before['rollups']} → {after['rollups']}"
    )


# --- service (systemd packaging) --------------------------------------------
@cli.group()
def service():
    """Generate a systemd unit to run 'netdiag watch' as an always-on service."""


def _unit_text(exec_start: str, system: bool) -> str:
    """Render a systemd unit file that runs ``netdiag watch`` as a service.

    ``exec_start`` is used verbatim as the ``ExecStart=`` command. ``system``
    selects the install target: a system unit (``WantedBy=multi-user.target``)
    when True, or a per-user unit (``WantedBy=default.target``) when False. No
    ``User=`` directive is emitted in either case.
    """
    after = "After=network-online.target\nWants=network-online.target"
    install_target = "multi-user.target" if system else "default.target"
    return f"""[Unit]
Description=netdiag always-on network diagnosis watcher
{after}

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
# Bounded resource use for a long-running daemon.
Nice=10

[Install]
WantedBy={install_target}
"""


def _exec_start(interval, external_ip, dns_name) -> str:
    import shutil

    exe = shutil.which("netdiag")
    base = exe if exe else f"{sys.executable} -m netdiag.cli"
    return (
        f"{base} watch --interval {interval} --external-ip {external_ip} "
        f"--dns-name {dns_name}"
    )


@service.command("print")
@click.option("--system", is_flag=True, help="System unit (default: user unit).")
@click.option("--interval", default=30, type=int)
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
def service_print(system, interval, external_ip, dns_name):
    """Print a systemd unit file to stdout."""
    click.echo(_unit_text(_exec_start(interval, external_ip, dns_name), system))


@service.command("install")
@click.option("--interval", default=30, type=int)
@click.option("--external-ip", default="1.1.1.1")
@click.option("--dns-name", default="example.com")
def service_install(interval, external_ip, dns_name):
    """Write a *user* systemd unit and print the commands to enable it.

    Installs to ~/.config/systemd/user (no root needed). We deliberately do NOT
    run systemctl for you — enabling a service changes system state, so the
    commands are printed for you to review and run.
    """
    from pathlib import Path

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "netdiag-watch.service"
    unit_path.write_text(
        _unit_text(_exec_start(interval, external_ip, dns_name), system=False)
    )
    click.echo(f"wrote {unit_path}\n\nEnable it with:")
    click.echo("  systemctl --user daemon-reload")
    click.echo("  systemctl --user enable --now netdiag-watch")
    click.echo("  # survive logout: sudo loginctl enable-linger $USER")
    click.echo("\nFor a system-wide service instead:")
    click.echo(
        "  netdiag service print --system | sudo tee /etc/systemd/system/netdiag-watch.service"
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
