import click
import os
from .app import create_app
from .services import SchedulerService
from .config.config import Config


@click.group()
def cli():
    """Network Checker CLI - Monitor network hosts with ping"""
    pass


@cli.command("run-server")
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind to")
@click.option("--port", "-p", default=9991, help="Port to bind to")
@click.option("--debug", is_flag=True, help="Enable debug mode")
def run_server(host, port, debug):
    """Run the Flask web application"""
    app = create_app("development" if debug else "production")
    app.run(host=host, port=port, debug=debug)


@cli.command()
@click.argument("hosts", nargs=-1, required=True)
@click.option("--interval", "-i", default=60, help="Ping interval in seconds")
def start_monitoring(hosts, interval):
    """Start monitoring specified hosts"""
    app = create_app()

    with app.app_context():
        scheduler = SchedulerService()

        try:
            scheduler.start()
            click.echo(f"Starting ping monitoring for: {', '.join(hosts)}")
            click.echo(f"Ping interval: {interval} seconds")

            for host in hosts:
                scheduler.add_ping_job(host, interval)
                click.echo(f"Added ping job for {host}")

            click.echo("Monitoring started. Press Ctrl+C to stop.")

            # Keep the process running
            import time

            while True:
                time.sleep(1)

        except KeyboardInterrupt:
            click.echo("\nStopping monitoring...")
            scheduler.shutdown()
            click.echo("Monitoring stopped.")
        except Exception as e:
            click.echo(f"Error: {e}")
            scheduler.shutdown()


@cli.command()
@click.argument("host")
@click.option("--count", "-c", default=4, help="Number of pings to send")
def ping(host, count):
    """Send ping to a specific host"""
    app = create_app()

    with app.app_context():
        from .services import PingService

        ping_service = PingService()

        click.echo(f"Pinging {host} {count} times...")

        for i in range(count):
            result = ping_service.ping_and_save(host)

            if result.packet_loss:
                click.echo(f"Ping {i+1}: Request timeout ({result.error_message})")
            else:
                click.echo(f"Ping {i+1}: {result.response_time:.2f}ms")

            if i < count - 1:
                import time

                time.sleep(1)


@cli.command()
@click.option("--host", help="Filter by host")
@click.option("--limit", "-l", default=10, help="Number of recent results to show")
def status(host, limit):
    """Show recent ping status"""
    app = create_app()

    with app.app_context():
        from .models import PingResult
        from sqlalchemy import desc

        query = PingResult.query

        if host:
            query = query.filter(PingResult.target_host == host)

        results = query.order_by(desc(PingResult.timestamp)).limit(limit).all()

        if not results:
            click.echo("No ping results found.")
            return

        click.echo(f"Recent ping results{'for ' + host if host else ''}:")
        click.echo("-" * 70)
        click.echo(f"{'Host':<20} {'Time':<20} {'Response':<12} {'Status'}")
        click.echo("-" * 70)

        for result in results:
            status_str = "FAIL" if result.packet_loss else "OK"
            response_str = (
                f"{result.response_time:.2f}ms" if result.response_time else "timeout"
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
        from .models import db, PingResult

        db.session.query(PingResult).delete()
        db.session.commit()
        click.echo("All ping data cleared.")


if __name__ == "__main__":
    cli()
