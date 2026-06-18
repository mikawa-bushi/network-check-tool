import json
import threading
from datetime import timedelta

import plotly
import plotly.graph_objects as go
import plotly.utils
from flask import Blueprint, Response, jsonify, render_template, request
from sqlalchemy import func

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
    host = request.args.get("host")
    hours, error = _parse_hours()
    if error:
        return error

    if not host:
        return jsonify({"error": "Host parameter is required"}), 400

    # Get data for the specified time range
    since = utcnow() - timedelta(hours=hours)

    results = (
        db.session.query(PingResult)
        .filter(PingResult.target_host == host, PingResult.timestamp >= since)
        .order_by(PingResult.timestamp)
        .all()
    )

    if not results:
        return jsonify({"error": "No data found for the specified host"}), 404

    # Prepare data for plotting
    timestamps = []
    response_times = []
    packet_losses = []

    for result in results:
        timestamps.append(result.timestamp.isoformat())
        response_times.append(result.response_time if not result.packet_loss else None)
        packet_losses.append(1 if result.packet_loss else 0)

    return jsonify(
        {
            "host": host,
            "timestamps": timestamps,
            "response_times": response_times,
            "packet_losses": packet_losses,
            "total_pings": len(results),
            "successful_pings": sum(1 for r in results if not r.packet_loss),
            "packet_loss_rate": (
                (sum(packet_losses) / len(packet_losses)) * 100 if packet_losses else 0
            ),
        }
    )


@dashboard_bp.route("/api/ping_chart")
def get_ping_chart():
    """Generate Plotly chart for ping data"""
    host = request.args.get("host")
    hours, error = _parse_hours()
    if error:
        return error

    if not host:
        return jsonify({"error": "Host parameter is required"}), 400

    since = utcnow() - timedelta(hours=hours)

    results = (
        db.session.query(PingResult)
        .filter(PingResult.target_host == host, PingResult.timestamp >= since)
        .order_by(PingResult.timestamp)
        .all()
    )

    if not results:
        return jsonify({"error": "No data found"}), 404

    # Separate successful pings and packet losses
    successful_times = []
    successful_response_times = []
    failed_times = []

    for result in results:
        if result.packet_loss:
            failed_times.append(result.timestamp)
        else:
            successful_times.append(result.timestamp)
            successful_response_times.append(result.response_time)

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
        xaxis_title="Time",
        yaxis_title="Response Time (ms)",
        template="plotly_white",
        hovermode="x unified",
    )

    return jsonify(json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig)))


@dashboard_bp.route("/api/stats")
def get_stats():
    """Get overall statistics"""
    host = request.args.get("host")
    hours, error = _parse_hours()
    if error:
        return error

    since = utcnow() - timedelta(hours=hours)

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
