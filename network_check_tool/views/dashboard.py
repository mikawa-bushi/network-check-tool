from flask import Blueprint, render_template, jsonify, request
from datetime import datetime, timedelta
from sqlalchemy import desc, func
import plotly.graph_objects as go
import plotly.utils
import json
from ..models import db, PingResult

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/")


@dashboard_bp.route("/")
def index():
    """Main dashboard page"""
    return render_template("dashboard.html")


@dashboard_bp.route("/api/hosts")
def get_hosts():
    """Get list of monitored hosts"""
    hosts = db.session.query(PingResult.target_host).distinct().all()
    return jsonify([host[0] for host in hosts])


@dashboard_bp.route("/api/ping_data")
def get_ping_data():
    """Get ping data for visualization"""
    host = request.args.get("host")
    hours = int(request.args.get("hours", 24))

    if not host:
        return jsonify({"error": "Host parameter is required"}), 400

    # Get data for the specified time range
    since = datetime.utcnow() - timedelta(hours=hours)

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
    hours = int(request.args.get("hours", 24))

    if not host:
        return jsonify({"error": "Host parameter is required"}), 400

    since = datetime.utcnow() - timedelta(hours=hours)

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
    hours = int(request.args.get("hours", 24))

    since = datetime.utcnow() - timedelta(hours=hours)

    if host:
        query = db.session.query(PingResult).filter(
            PingResult.target_host == host, PingResult.timestamp >= since
        )
    else:
        query = db.session.query(PingResult).filter(PingResult.timestamp >= since)

    results = query.all()

    if not results:
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

    successful = [
        r for r in results if not r.packet_loss and r.response_time is not None
    ]
    successful_response_times = [r.response_time for r in successful]

    return jsonify(
        {
            "total_pings": len(results),
            "successful_pings": len(successful),
            "failed_pings": len(results) - len(successful),
            "packet_loss_rate": ((len(results) - len(successful)) / len(results)) * 100,
            "avg_response_time": (
                sum(successful_response_times) / len(successful_response_times)
                if successful_response_times
                else 0
            ),
            "min_response_time": (
                min(successful_response_times) if successful_response_times else 0
            ),
            "max_response_time": (
                max(successful_response_times) if successful_response_times else 0
            ),
        }
    )
