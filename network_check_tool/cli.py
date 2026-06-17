import os

import click

from .app import create_app
from .config.config import Config
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
def run_server(host, port, debug):
    """Run the Flask web application.

    Binds to localhost (127.0.0.1) by default. Pass ``--host 0.0.0.0`` to expose
    it on the network -- but the dashboard and data API have no authentication,
    so only do that behind a reverse proxy that adds access control.
    """
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
    app = create_app("development" if debug else "production")
    app.run(host=host, port=port, debug=debug)


@cli.command()
@click.argument("hosts", nargs=-1, required=True)
@click.option("--interval", "-i", default=60, help="Ping interval in seconds")
def start_monitoring(hosts, interval):
    """Start monitoring specified hosts"""
    app = create_app()

    with app.app_context():
        # Pass the app so each job (run in an APScheduler worker thread) can push
        # its own application context before writing to the DB. The configured
        # PING_TIMEOUT is forwarded so monitored pings honour it (M2); the worker
        # pool is sized from SCHEDULER_MAX_WORKERS rather than a fixed 20 (P3).
        scheduler = SchedulerService(
            app=app,
            timeout=Config.PING_TIMEOUT,
            max_workers=Config.SCHEDULER_MAX_WORKERS,
        )

        # Startup must fail loudly: a job-registration or scheduler error here used
        # to be swallowed and the process exited 0, looking healthy while no pings
        # were ever recorded. Report it and exit non-zero instead.
        try:
            scheduler.start()
            click.echo(f"Starting ping monitoring for: {', '.join(hosts)}")
            click.echo(f"Ping interval: {interval} seconds")

            for host in hosts:
                scheduler.add_ping_job(host, interval)
                click.echo(f"Added ping job for {host}")
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            scheduler.shutdown()
            raise SystemExit(1)

        click.echo("Monitoring started. Press Ctrl+C to stop.")

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
def ping(host, count):
    """Send pings to a host and record them in the monitoring database.

    For a one-shot check that stores nothing, use ``netdiag ping`` instead.
    """
    app = create_app()

    with app.app_context():
        from .services import PingService

        ping_service = PingService(timeout=Config.PING_TIMEOUT)

        click.echo(f"Pinging {host} {count} times...")

        # Add each sample to the session but commit once after the loop: a batch
        # of N ad-hoc pings is one transaction instead of N. The result object's
        # attributes are populated before commit, so per-ping output is unaffected.
        for i in range(count):
            result = ping_service.ping_and_save(host, commit=False)

            if result.packet_loss:
                click.echo(f"Ping {i+1}: Request timeout ({result.error_message})")
            else:
                click.echo(f"Ping {i+1}: {result.response_time:.2f}ms")

            if i < count - 1:
                import time

                time.sleep(1)

        from .models import db

        db.session.commit()


@cli.command()
@click.option("--host", help="Filter by host")
@click.option("--limit", "-l", default=10, help="Number of recent results to show")
def status(host, limit):
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
        from .models import PingResult, db

        db.session.query(PingResult).delete()
        db.session.commit()
        click.echo("All ping data cleared.")


if __name__ == "__main__":
    cli()
