import json

import click

from .app import create_app
from .config.config import Config
from .config.hosts import DEFAULT_MONITOR_INTERVAL
from .services import SchedulerService


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """Network Checker CLI - Monitor network hosts with ping"""
    pass


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(host: str) -> bool:
    """Return True if ``host`` refers only to the local loopback interface.

    Recognises ``localhost``, ``::1`` and any ``127.0.0.0/8`` address. Wildcard
    binds such as ``0.0.0.0`` and ``::`` (all IPv6 interfaces) are NOT loopback
    and return False, so they trigger the public-bind warning / debug refusal.
    Used to keep the interactive debugger off network-facing interfaces.
    """
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


@cli.command("run-server")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to (default: localhost only; use 0.0.0.0 to expose on the network)",
)
@click.option("--port", "-p", default=9991, help="Port to bind to")
@click.option("--debug", is_flag=True, help="Enable debug mode (loopback hosts only)")
@click.option(
    "--devices",
    "devices_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Device list (CSV saved from Excel, YAML, JSON/TOML) whose names and groups "
        "label the line board; on a loopback bind every device is also monitored."
    ),
)
def run_server(host, port, debug, devices_path):
    """Run the Flask web application.

    Binds to localhost (127.0.0.1) by default. Pass ``--host 0.0.0.0`` to expose
    it on the network -- but the dashboard and data API have no authentication,
    so only do that behind a reverse proxy that adds access control.

    ``--devices 1ライン.csv`` turns it into a one-command line monitor: the board
    lists the devices by name and group and (on a loopback bind) pings them.
    """
    host_configs = _load_host_configs(devices_path) if devices_path else []
    if debug and not _is_loopback(host):
        raise click.ClickException(
            f"Refusing to enable --debug on non-loopback host {host!r}: the Werkzeug "
            "debugger allows arbitrary code execution. Debug only on --host 127.0.0.1."
        )
    if not _is_loopback(host):
        click.echo(
            f"WARNING: binding to {host} exposes the dashboard and data API on the "
            "network with no authentication. Put it behind a reverse proxy with access "
            "control, and do not use --debug.",
            err=True,
        )
    # Enable the in-process monitoring scheduler (host-management API) only on a
    # loopback bind and outside debug mode: the write API has no auth, and the
    # debug reloader would otherwise spawn a second scheduler in the child process.
    enable_scheduler = _is_loopback(host) and not debug
    app = create_app(
        "development" if debug else "production",
        register_scheduler=enable_scheduler,
    )
    if enable_scheduler:
        click.echo("Host-management API enabled (POST/DELETE /api/hosts).", err=True)
    if host_configs:
        from .models import upsert_hosts

        with app.app_context():
            upsert_hosts(host_configs)
        scheduler = app.extensions.get("scheduler")
        if scheduler is not None:
            scheduler.add_ping_jobs(host_configs)
            click.echo(f"Monitoring {len(host_configs)} devices.", err=True)
        else:
            click.echo(
                f"Loaded names for {len(host_configs)} devices (monitor them with "
                "start-monitoring --config).",
                err=True,
            )
    app.run(host=host, port=port, debug=debug)


def _load_host_configs(path, interval=DEFAULT_MONITOR_INTERVAL):
    """Read a hosts/device list, exiting with a clear message when it is invalid."""
    from .config.hosts import load_hosts

    try:
        return load_hosts(path, default_interval=interval)
    except (ValueError, OSError) as e:
        # Fail loudly rather than start up monitoring nothing.
        click.echo(f"Error: invalid device list {path}: {e}", err=True)
        raise SystemExit(1)


@cli.command()
@click.argument("hosts", nargs=-1, required=False)
@click.option(
    "--interval",
    "-i",
    default=DEFAULT_MONITOR_INTERVAL,
    help="Ping interval in seconds",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Hosts file: TOML/JSON [[host]] entries with per-host interval/thresholds, "
        "or a netdiag device list (CSV saved from Excel, YAML, JSON)."
    ),
)
@click.option(
    "--alert-file",
    "alert_file",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write threshold alerts (DEGRADED/RECOVERED) as JSONL to this path.",
)
@click.option(
    "--diagnose",
    is_flag=True,
    default=False,
    help="On a confirmed degradation, run netdiag once and attach the root cause.",
)
def start_monitoring(hosts, interval, config_path, alert_file, diagnose):
    """Start monitoring hosts (positional args, or a --config file).

    Without --config, each positional host shares the single --interval. With
    --config, each host carries its own interval and warning thresholds
    (rtt_warn_ms / loss_warn_pct), which raise DEGRADED/RECOVERED alerts. The
    same device list used by ``netdiag check -f`` works here.
    """
    # Resolve the host list: a --config file (per-host interval) takes precedence
    # over positional args (shared --interval). One or the other is required.
    if config_path:
        host_configs = _load_host_configs(config_path, interval)
    elif hosts:
        from .config.hosts import HostConfig

        host_configs = [HostConfig(name=h, interval=interval) for h in hosts]
    else:
        raise click.UsageError("provide one or more HOSTS or --config PATH")

    # Threshold alerts (DEGRADED/RECOVERED) are written to a JSONL sink; per-host
    # thresholds come from the config file. The default path lives under ~/.netdiag.
    from pathlib import Path

    from netdiag.alerting import FileAlertSink, MultiSink

    from .services.alerting import DbAlertSink

    file_sink = FileAlertSink(jsonl_path=Path(alert_file) if alert_file else None)

    app = create_app()
    # Alerts also go to the DB so a dashboard on the same database shows them.
    sink = MultiSink([file_sink, DbAlertSink(app)])

    with app.app_context():
        if config_path:
            # Names and groups from the list label the dashboard's line board.
            from .models import upsert_hosts

            upsert_hosts(host_configs)
        # Pass the app so each sweep (run in its own thread) can push its own
        # application context before writing to the DB. The configured
        # PING_TIMEOUT is forwarded so monitored pings honour it (M2).
        scheduler = SchedulerService(
            app=app,
            timeout=Config.PING_TIMEOUT,
            sink=sink,
            diagnose=diagnose,
        )

        # Startup must fail loudly: a job-registration or scheduler error here used
        # to be swallowed and the process exited 0, looking healthy while no pings
        # were ever recorded. Report it and exit non-zero instead.
        try:
            scheduler.start()
            names = ", ".join(hc.name for hc in host_configs)
            click.echo(f"Starting ping monitoring for: {names}")

            scheduler.add_ping_jobs(host_configs)
            for hc in host_configs:
                label = f" ({hc.label})" if getattr(hc, "label", "") else ""
                click.echo(
                    f"Added ping job for {hc.name}{label} (every {hc.interval}s)"
                )
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            scheduler.shutdown()
            raise SystemExit(1)

        click.echo(
            f"Monitoring started (alerts → {file_sink.jsonl_path}). Press Ctrl+C to stop."
        )

        # Keep the process running until the user interrupts (a clean exit 0).
        import time

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            click.echo("\nStopping monitoring...")
            scheduler.shutdown()
            click.echo("Monitoring stopped.")


@cli.command()
@click.argument("host")
@click.option("--count", "-c", default=4, help="Number of pings to send")
@click.option(
    "--json", "as_json", is_flag=True, help="Output results as JSON (UTC timestamps)."
)
def ping(host, count, as_json):
    """Send pings to a host and record them in the monitoring database.

    For a one-shot check that stores nothing, use ``netdiag ping`` instead.
    """
    app = create_app()

    with app.app_context():
        from .models import db
        from .services import PingService

        ping_service = PingService(timeout=Config.PING_TIMEOUT)

        if not as_json:
            click.echo(f"Pinging {host} {count} times...")

        # Add each sample to the session but commit once after the loop: a batch
        # of N ad-hoc pings is one transaction instead of N. The result object's
        # attributes are populated before commit, so per-ping output is unaffected.
        results = []
        for i in range(count):
            (result,) = ping_service.save_ping_results([ping_service.ping_host(host)])

            if not as_json:
                if result.packet_loss:
                    click.echo(f"Ping {i+1}: Request timeout ({result.error_message})")
                else:
                    click.echo(f"Ping {i+1}: {result.response_time:.2f}ms")

            results.append(result)

            if i < count - 1:
                import time

                time.sleep(1)

        db.session.commit()

        if as_json:
            # Emitted after commit so each row carries its assigned id.
            click.echo(json.dumps([r.to_dict() for r in results], ensure_ascii=False))


@cli.command()
@click.option("--host", help="Filter by host")
@click.option("--limit", "-l", default=10, help="Number of recent results to show")
@click.option(
    "--json", "as_json", is_flag=True, help="Output results as JSON (UTC timestamps)."
)
def status(host, limit, as_json):
    """Show recent ping results from the monitoring database.

    This reads the network-checker store; ``netdiag status`` reports the separate
    netdiag history DB.
    """
    app = create_app()

    with app.app_context():
        from sqlalchemy import desc

        from .models import PingResult

        query = PingResult.query

        if host:
            query = query.filter(PingResult.target_host == host)

        results = query.order_by(desc(PingResult.timestamp)).limit(limit).all()

        if as_json:
            # Empty result set is a valid [] rather than the human "no results" line.
            click.echo(json.dumps([r.to_dict() for r in results], ensure_ascii=False))
            return

        if not results:
            click.echo("No ping results found.")
            return

        click.echo(f"Recent ping results{' for ' + host if host else ''}:")
        click.echo("-" * 70)
        click.echo(f"{'Host':<20} {'Time':<20} {'Response':<12} {'Status'}")
        click.echo("-" * 70)

        for result in results:
            status_str = "FAIL" if result.packet_loss else "OK"
            response_str = (
                "timeout"
                if result.packet_loss or result.response_time is None
                else f"{result.response_time:.2f}ms"
            )
            time_str = result.timestamp.strftime("%Y-%m-%d %H:%M:%S")

            click.echo(
                f"{result.target_host:<20} {time_str:<20} {response_str:<12} {status_str}"
            )


@cli.command()
def init():
    """Initialize the database"""
    app = create_app()

    with app.app_context():
        from .models import db

        db.create_all()
        click.echo("Database initialized successfully.")


@cli.command()
@click.confirmation_option(prompt="Are you sure you want to clear all ping data?")
def clear():
    """Clear all ping data from the database"""
    app = create_app()

    with app.app_context():
        from .models import AlertEvent, PingResult, db

        db.session.query(PingResult).delete()
        db.session.query(AlertEvent).delete()
        db.session.commit()
        click.echo("All ping data cleared (alerts too; device names are kept).")


if __name__ == "__main__":
    cli()
