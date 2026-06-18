import csv
import io
import json
import math
import threading
from collections import namedtuple
from datetime import timedelta, timezone

import plotly
import plotly.graph_objects as go
import plotly.utils
from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from sqlalchemy import case, func

from ..models import PingResult, db
from ..utils import utcnow

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/")

# Plotly's JS bundle, served locally so the dashboard works fully offline and is
# pinned to the installed plotly version (no CDN). Loaded once (double-checked
# under a lock so concurrent first requests don't each rebuild the ~5MB string).
_PLOTLY_JS = None
_PLOTLY_JS_LOCK = threading.Lock()

# Bounds for the ?hours= query parameter shared by the data/chart/stats endpoints.
HOURS_MIN = 1
HOURS_MAX = 168  # one week
HOURS_DEFAULT = 24

# Cap on the number of points handed to the chart/data endpoints. Beyond this the
# raw rows are bucketed and averaged (see ``_downsample``) so the JSON payload and
# the browser-side render stay bounded no matter how long the window or how short
# the ping interval -- the dashboard re-fetches on a 30s timer.
MAX_CHART_POINTS = 2000

# A plotted point decoupled from the ORM row, carrying a tz-aware UTC timestamp so
# Plotly/the browser never reads a naive value as the viewer's local time.
_Point = namedtuple("_Point", ["timestamp", "response_time", "packet_loss"])


def _as_utc(dt):
    """Tag a naive ``datetime`` as UTC (timestamps are stored naive-UTC)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _downsample(results, max_points=MAX_CHART_POINTS):
    """Thin a time-ordered ``PingResult`` list to at most ~``max_points`` points.

    At or below the cap the rows pass through unchanged (callers get the exact
    same series). Above it, rows are grouped into uniform index buckets; each
    bucket emits the average RTT of its successful samples (when any) and, if it
    contained any packet loss, one loss marker -- so outages stay visible no
    matter how aggressively the successful samples are thinned.
    """
    n = len(results)
    if n <= max_points:
        return [
            _Point(_as_utc(r.timestamp), r.response_time, r.packet_loss)
            for r in results
        ]
    points = []
    bucket = math.ceil(n / max_points)
    for start in range(0, n, bucket):
        chunk = results[start : start + bucket]
        oks = [
            r.response_time
            for r in chunk
            if not r.packet_loss and r.response_time is not None
        ]
        if oks:
            points.append(
                _Point(_as_utc(chunk[0].timestamp), sum(oks) / len(oks), False)
            )
        if any(r.packet_loss for r in chunk):
            points.append(_Point(_as_utc(chunk[-1].timestamp), None, True))
    return points


def _parse_hours():
    """Parse and validate the ``?hours=`` query parameter.

    Returns ``(hours, None)`` on success, where ``hours`` is clamped to
    ``[HOURS_MIN, HOURS_MAX]``. On a non-integer value returns
    ``(None, (json_response, 400))`` so the caller can ``return error``
    instead of letting ``int()`` raise and surface a 500.
    """
    raw = request.args.get("hours", HOURS_DEFAULT)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return None, (
            jsonify({"error": "Invalid 'hours' parameter; must be an integer"}),
            400,
        )
    return max(HOURS_MIN, min(hours, HOURS_MAX)), None


def _host_window(require_host=False):
    """Resolve the ``(host, hours, since)`` shared by the data/chart/stats endpoints.

    Returns ``((host, hours, since), None)`` on success, or ``(None, error)`` where
    ``error`` is a ready-to-return ``(response, status)`` -- a 400 for an invalid
    ``hours`` value or, when ``require_host`` is set, a missing ``host``.
    """
    host = request.args.get("host")
    hours, error = _parse_hours()
    if error:
        return None, error
    if require_host and not host:
        return None, (jsonify({"error": "Host parameter is required"}), 400)
    return (host, hours, utcnow() - timedelta(hours=hours)), None


def _window_query(host, since):
    """Time-ordered ``PingResult`` rows for one host within the window."""
    return (
        db.session.query(PingResult)
        .filter(PingResult.target_host == host, PingResult.timestamp >= since)
        .order_by(PingResult.timestamp)
    )


@dashboard_bp.route("/")
def index():
    """Main dashboard page"""
    return render_template("dashboard.html")


@dashboard_bp.route("/vendor/plotly.min.js")
def plotly_js():
    """Serve the Plotly JS bundle shipped with the installed plotly package.

    Keeps the dashboard usable offline and version-locked to the dependency
    instead of fetching an unpinned build from the Plotly CDN.
    """
    global _PLOTLY_JS
    if _PLOTLY_JS is None:
        with _PLOTLY_JS_LOCK:
            if _PLOTLY_JS is None:
                from plotly.offline import get_plotlyjs

                _PLOTLY_JS = get_plotlyjs()
    resp = Response(
        _PLOTLY_JS,
        mimetype="text/javascript",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
    # ETag keyed on the installed plotly version: a same-version reload after the
    # cache TTL revalidates with a cheap 304 instead of re-sending the bundle, and
    # an upgrade changes the tag so clients pull the new build.
    resp.set_etag(f"plotly-{plotly.__version__}")
    return resp.make_conditional(request)


@dashboard_bp.route("/api/hosts")
def get_hosts():
    """Get list of monitored hosts"""
    hosts = db.session.query(PingResult.target_host).distinct().all()
    return jsonify([host[0] for host in hosts])


@dashboard_bp.route("/api/ping_data")
def get_ping_data():
    """Get ping data for visualization"""
    parsed, error = _host_window(require_host=True)
    if error:
        return error
    host, _hours, since = parsed

    results = _window_query(host, since).all()

    if not results:
        return jsonify({"error": "No data found for the specified host"}), 404

    # Plotted series come from the (possibly thinned) points; their timestamps are
    # tz-aware UTC so the browser cannot mis-read them as local time.
    points = _downsample(results)
    timestamps = [p.timestamp.isoformat() for p in points]
    response_times = [p.response_time if not p.packet_loss else None for p in points]
    packet_losses = [1 if p.packet_loss else 0 for p in points]

    # Headline counts are computed from the raw rows so they stay exact regardless
    # of any downsampling applied to the plotted series above.
    total = len(results)
    loss_count = sum(1 for r in results if r.packet_loss)

    return jsonify(
        {
            "host": host,
            "timestamps": timestamps,
            "response_times": response_times,
            "packet_losses": packet_losses,
            "total_pings": total,
            "successful_pings": total - loss_count,
            "packet_loss_rate": ((loss_count / total) * 100 if total else 0),
            "downsampled": len(points) != total,
        }
    )


@dashboard_bp.route("/api/ping_chart")
def get_ping_chart():
    """Generate Plotly chart for ping data"""
    parsed, error = _host_window(require_host=True)
    if error:
        return error
    host, hours, since = parsed

    results = _window_query(host, since).all()

    if not results:
        return jsonify({"error": "No data found"}), 404

    # Separate successful pings and packet losses. Points carry tz-aware UTC
    # timestamps and are thinned to a bounded count for long windows.
    points = _downsample(results)
    successful_times = []
    successful_response_times = []
    failed_times = []

    for p in points:
        if p.packet_loss:
            failed_times.append(p.timestamp)
        else:
            successful_times.append(p.timestamp)
            successful_response_times.append(p.response_time)

    # Create plotly figure
    fig = go.Figure()

    # Add successful pings
    if successful_times:
        fig.add_trace(
            go.Scatter(
                x=successful_times,
                y=successful_response_times,
                mode="lines+markers",
                name="Response Time (ms)",
                line=dict(color="green"),
                marker=dict(size=4),
            )
        )

    # Add packet loss markers
    if failed_times:
        fig.add_trace(
            go.Scatter(
                x=failed_times,
                y=(
                    [max(successful_response_times) * 1.1] * len(failed_times)
                    if successful_response_times
                    else [100] * len(failed_times)
                ),
                mode="markers",
                name="Packet Loss",
                marker=dict(color="red", symbol="x", size=8),
            )
        )

    fig.update_layout(
        title=f"Ping Results for {host} (Last {hours} hours)",
        xaxis_title="Time (UTC)",
        yaxis_title="Response Time (ms)",
        template="plotly_white",
        hovermode="x unified",
    )

    return jsonify(json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig)))


@dashboard_bp.route("/api/stats")
def get_stats():
    """Get overall statistics"""
    parsed, error = _host_window()
    if error:
        return error
    host, _hours, since = parsed

    # Aggregate in SQL rather than reading every row into Python, so latency does
    # not grow linearly with the number of stored pings (the composite
    # (target_host, timestamp) index serves the filter).
    base = db.session.query(PingResult).filter(PingResult.timestamp >= since)
    if host:
        base = base.filter(PingResult.target_host == host)

    total = base.with_entities(func.count()).scalar() or 0

    if total == 0:
        return jsonify(
            {
                "total_pings": 0,
                "successful_pings": 0,
                "failed_pings": 0,
                "packet_loss_rate": 0,
                "avg_response_time": 0,
                "min_response_time": 0,
                "max_response_time": 0,
            }
        )

    # "Successful" matches the prior definition: not a packet loss and an actual
    # response time was recorded. is_(False) is strict (NULL packet_loss is not
    # treated as success); the column is NOT NULL so NULLs should never occur.
    ok = base.filter(
        PingResult.packet_loss.is_(False), PingResult.response_time.isnot(None)
    )
    successful = ok.with_entities(func.count()).scalar() or 0
    avg_v, min_v, max_v = ok.with_entities(
        func.avg(PingResult.response_time),
        func.min(PingResult.response_time),
        func.max(PingResult.response_time),
    ).one()

    # Cast to float: on PostgreSQL func.avg/min/max return Decimal, which Flask's
    # JSON encoder can't serialize. On SQLite these are already float (no-op).
    return jsonify(
        {
            "total_pings": total,
            "successful_pings": successful,
            "failed_pings": total - successful,
            "packet_loss_rate": ((total - successful) / total) * 100,
            "avg_response_time": float(avg_v) if avg_v is not None else 0,
            "min_response_time": float(min_v) if min_v is not None else 0,
            "max_response_time": float(max_v) if max_v is not None else 0,
        }
    )


def _safe_filename(host):
    """A filesystem-safe filename stem derived from a host label."""
    return "".join(c if (c.isalnum() or c in ".-_") else "_" for c in host) or "host"


@dashboard_bp.route("/api/export")
def export_data():
    """Stream a host's raw ping rows as CSV for spreadsheets/offline analysis.

    Rows are streamed (not buffered) so a long window stays memory-bounded.
    Timestamps are explicit UTC, matching the chart endpoints.
    """
    fmt = request.args.get("format", "csv").lower()
    if fmt != "csv":
        return jsonify({"error": "Only format=csv is supported"}), 400
    parsed, error = _host_window(require_host=True)
    if error:
        return error
    host, _hours, since = parsed
    query = _window_query(host, since)

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush():
            data = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return data

        writer.writerow(
            ["timestamp_utc", "response_time_ms", "packet_loss", "error_message"]
        )
        yield flush()
        for r in query:
            writer.writerow(
                [
                    _as_utc(r.timestamp).isoformat(),
                    "" if r.response_time is None else r.response_time,
                    int(bool(r.packet_loss)),
                    r.error_message or "",
                ]
            )
            yield flush()

    filename = f"ping_{_safe_filename(host)}.csv"
    return Response(
        stream_with_context(rows()),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _prom_escape(value):
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dashboard_bp.route("/metrics")
def metrics():
    """Prometheus text exposition of per-host ping health over the window.

    Pull-based and dependency-free. Default window is the last hour; override
    with ``?hours=``. Per host:
      ``network_ping_up``         1 if any ping succeeded in the window, else 0
      ``network_ping_loss_ratio`` fraction of pings lost (0..1)
      ``network_ping_rtt_ms``     average RTT of successful pings (ms)
    """
    raw = request.args.get("hours", 1)  # metrics default to a short, recent window
    try:
        hours = max(HOURS_MIN, min(int(raw), HOURS_MAX))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid 'hours' parameter; must be an integer"}), 400
    since = utcnow() - timedelta(hours=hours)

    loss_count = func.sum(case((PingResult.packet_loss.is_(True), 1), else_=0))
    rtt_avg = func.avg(
        case((PingResult.packet_loss.is_(False), PingResult.response_time))
    )
    grouped = (
        db.session.query(PingResult.target_host, func.count(), loss_count, rtt_avg)
        .filter(PingResult.timestamp >= since)
        .group_by(PingResult.target_host)
        .all()
    )

    up_lines = [
        "# HELP network_ping_up 1 if the host had any successful ping in the window.",
        "# TYPE network_ping_up gauge",
    ]
    loss_lines = [
        "# HELP network_ping_loss_ratio Fraction of pings lost in the window (0-1).",
        "# TYPE network_ping_loss_ratio gauge",
    ]
    rtt_lines = [
        "# HELP network_ping_rtt_ms Average RTT of successful pings in the window (ms).",
        "# TYPE network_ping_rtt_ms gauge",
    ]
    for host, total, losses, rtt in grouped:
        total = total or 0
        losses = int(losses or 0)
        label = f'host="{_prom_escape(host)}"'
        up_lines.append(f"network_ping_up{{{label}}} {1 if total - losses > 0 else 0}")
        loss_lines.append(
            f"network_ping_loss_ratio{{{label}}} {(losses / total) if total else 0.0}"
        )
        if rtt is not None:
            rtt_lines.append(f"network_ping_rtt_ms{{{label}}} {float(rtt)}")

    body = "\n".join(up_lines + loss_lines + rtt_lines) + "\n"
    return Response(body, mimetype="text/plain; version=0.0.4; charset=utf-8")


# --- Host management (F4) ---------------------------------------------------
# These routes manage the in-process monitoring scheduler. They only function
# when one is attached (run-server on a loopback bind); otherwise they return
# 503, which is how the write API stays disabled on public/multi-worker setups.

DEFAULT_MONITOR_INTERVAL = 60


def _scheduler():
    """The in-process monitoring scheduler, or ``None`` if not running."""
    return current_app.extensions.get("scheduler")


@dashboard_bp.route("/api/jobs")
def get_jobs():
    """List the currently scheduled monitoring jobs."""
    scheduler = _scheduler()
    if scheduler is None:
        return jsonify({"error": "monitoring scheduler not running"}), 503
    jobs = []
    for job in scheduler.list_jobs():
        host = job.id[len("ping_") :] if job.id.startswith("ping_") else job.id
        jobs.append(
            {
                "job_id": job.id,
                "host": host,
                "next_run_time": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
            }
        )
    return jsonify(jobs)


@dashboard_bp.route("/api/hosts", methods=["POST"])
def add_host():
    """Add (or replace) a monitoring job for a host."""
    scheduler = _scheduler()
    if scheduler is None:
        return jsonify({"error": "monitoring scheduler not running"}), 503
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    if not host:
        return jsonify({"error": "host is required"}), 400
    try:
        interval = int(data.get("interval", DEFAULT_MONITOR_INTERVAL))
    except (TypeError, ValueError):
        return jsonify({"error": "interval must be an integer"}), 400
    if interval < 1:
        return jsonify({"error": "interval must be >= 1 second"}), 400
    scheduler.add_ping_job(host, interval)
    return jsonify({"host": host, "interval": interval}), 201


@dashboard_bp.route("/api/hosts/<host>", methods=["DELETE"])
def remove_host(host):
    """Stop monitoring a host."""
    scheduler = _scheduler()
    if scheduler is None:
        return jsonify({"error": "monitoring scheduler not running"}), 503
    scheduler.remove_ping_job(f"ping_{host}")
    return jsonify({"removed": host})
