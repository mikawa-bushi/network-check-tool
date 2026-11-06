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

from ..config.hosts import DEFAULT_MONITOR_INTERVAL, HostConfig
from ..models import AlertEvent, MonitoredHost, PingResult, db, upsert_hosts
from ..services.alerting import FAIL_THRESHOLD
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
METRICS_HOURS_DEFAULT = 1

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


class ApiError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


@dashboard_bp.errorhandler(ApiError)
def _api_error(e):
    return jsonify({"error": e.message}), e.status


def _int_arg(name, default, lo, hi):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        raise ApiError(f"Invalid '{name}' parameter; must be an integer", 400)
    return max(lo, min(value, hi))


def _parse_hours(default=HOURS_DEFAULT):
    return _int_arg("hours", default, HOURS_MIN, HOURS_MAX)


def _host_window(require_host=False):
    host = request.args.get("host")
    hours = _parse_hours()
    if require_host and not host:
        raise ApiError("Host parameter is required", 400)
    return host, hours, utcnow() - timedelta(hours=hours)


def _window_query(host, since):
    """Time-ordered ``PingResult`` rows for one host within the window."""
    return (
        db.session.query(PingResult)
        .filter(PingResult.target_host == host, PingResult.timestamp >= since)
        .order_by(PingResult.timestamp)
    )


def _ui_lang():
    """Display language: ``?lang=`` first, then the browser's, else Japanese."""
    from netdiag.i18n import normalize

    chosen = normalize(request.args.get("lang"))
    if chosen:
        return chosen
    return request.accept_languages.best_match(["ja", "en"], default="ja")


@dashboard_bp.route("/")
def index():
    """Main dashboard page: the line board plus per-host charts."""
    return render_template("dashboard.html", lang=_ui_lang())


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
    host, _hours, since = _host_window(require_host=True)

    results = _window_query(host, since).all()

    if not results:
        raise ApiError("No data found for the specified host", 404)

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


_CHART_WORDS = {
    "ja": {
        "title": "{host} の応答（直近 {hours} 時間）",
        "x": "時刻 (UTC)",
        "rtt": "応答時間 (ms)",
        "loss": "応答なし",
        "no_data": "この期間のデータはまだありません",
    },
    "en": {
        "title": "Ping Results for {host} (Last {hours} hours)",
        "x": "Time (UTC)",
        "rtt": "Response Time (ms)",
        "loss": "Packet Loss",
        "no_data": "No data found",
    },
}


@dashboard_bp.route("/api/ping_chart")
def get_ping_chart():
    """Generate Plotly chart for ping data"""
    host, hours, since = _host_window(require_host=True)
    # The page passes its language; other API clients keep the English figure.
    words = _CHART_WORDS["ja" if request.args.get("lang") == "ja" else "en"]

    results = _window_query(host, since).all()

    if not results:
        raise ApiError(words["no_data"], 404)

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
                name=words["rtt"],
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
                name=words["loss"],
                marker=dict(color="red", symbol="x", size=8),
            )
        )

    meta = db.session.get(MonitoredHost, host)
    label = f"{meta.name} ({host})" if meta is not None and meta.name else host
    fig.update_layout(
        title=words["title"].format(host=label, hours=hours),
        xaxis_title=words["x"],
        yaxis_title=words["rtt"],
        template="plotly_white",
        hovermode="x unified",
    )

    return jsonify(json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig)))


@dashboard_bp.route("/api/stats")
def get_stats():
    """Get overall statistics"""
    host, _hours, since = _host_window()

    # Aggregate in SQL rather than reading every row into Python, so latency does
    # not grow linearly with the number of stored pings (the composite
    # (target_host, timestamp) index serves the filter).
    base = db.session.query(PingResult).filter(PingResult.timestamp >= since)
    if host:
        base = base.filter(PingResult.target_host == host)

    total = base.with_entities(func.count()).scalar() or 0

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
            "packet_loss_rate": (total - successful) / total * 100 if total else 0,
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
        raise ApiError("Only format=csv is supported", 400)
    host, _hours, since = _host_window(require_host=True)
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


# --- Line board ---------------------------------------------------------------

OVERVIEW_HOURS_DEFAULT = 1
STATUS_ORDER = ("down", "warn", "ok", "unknown")


# Grouping/partitioning by this expression instead of the bare column keeps
# SQLite on the timestamp index (reads only the window) rather than walking the
# whole (host, timestamp) index -- all of history -- just to avoid a sort.
_HOST_KEY = func.coalesce(PingResult.target_host, "")


def _iso(dt):
    return _as_utc(dt).isoformat() if dt is not None else None


def _host_stats(since):
    """Per host: ``(total, lost, avg_rtt, max_rtt, last_ok_ts)`` of the samples since *since*."""
    ok = PingResult.packet_loss.is_(False)
    rows = (
        db.session.query(
            _HOST_KEY,
            func.count(),
            func.sum(case((PingResult.packet_loss.is_(True), 1), else_=0)),
            func.avg(case((ok, PingResult.response_time))),
            func.max(case((ok, PingResult.response_time))),
            func.max(case((ok, PingResult.timestamp))),
        )
        .filter(PingResult.timestamp >= since)
        .group_by(_HOST_KEY)
    )
    return {
        h: (n or 0, int(lost or 0), avg, mx, last_ok)
        for h, n, lost, avg, mx, last_ok in rows
    }


def _latest_samples(since, per_host):
    """The newest *per_host* samples of each host, among those since *since*."""
    newest_first = (PingResult.timestamp.desc(), PingResult.id.desc())
    rank = func.row_number().over(partition_by=_HOST_KEY, order_by=newest_first)
    ranked = (
        db.session.query(PingResult.id.label("id"), rank.label("rank"))
        .filter(PingResult.timestamp >= since)
        .subquery()
    )
    rows = (
        db.session.query(PingResult)
        .join(ranked, PingResult.id == ranked.c.id)
        .filter(ranked.c.rank <= per_host)
        .order_by(PingResult.target_host, *newest_first)
    )
    latest = {}
    for r in rows:
        latest.setdefault(r.target_host, []).append(r)
    return latest


def _stale_after(interval):
    """Seconds without a sample after which a tile turns ``unknown``."""
    return max(3 * (interval or DEFAULT_MONITOR_INTERVAL), 180)


def _host_status(total, losses, recent, meta, alert, now):
    """``(status, stale, reasons)`` for one tile.

    *status* is ``ok`` / ``warn`` / ``down`` / ``unknown``; *recent* are the
    newest samples (newest first). ``FAIL_THRESHOLD`` lost in a row (the
    alerter's rule) is down; any loss in the window, a slow last reply or an
    open alert is a warning (*reasons* says which); a silent monitor is
    unknown, with *stale* set.
    """
    if not recent:
        return "unknown", False, []
    stale = (now - recent[0].timestamp).total_seconds() > _stale_after(meta.interval)
    if stale or not total:
        return "unknown", stale, []
    if all(r.packet_loss for r in recent[:FAIL_THRESHOLD]):
        return "down", False, ["no_reply"]
    reasons = []
    loss_pct = losses * 100.0 / total
    limit = meta.loss_warn_pct
    if (loss_pct > 0) if limit is None else (loss_pct >= limit):
        reasons.append("loss")
    last_rtt = recent[0].response_time
    if meta.rtt_warn_ms is not None and last_rtt is not None:
        if last_rtt > meta.rtt_warn_ms:
            reasons.append("slow")
    if alert is not None and alert.event == "degraded":
        reasons.append("alert")
    return ("warn" if reasons else "ok"), False, reasons


@dashboard_bp.route("/api/overview")
def overview():
    """Every monitored device with a status, for the line board.

    Devices come from the device list (``MonitoredHost``, in list order) plus
    any other host with pings in the window. ``?hours=`` (default 1) sets the
    window for the loss rate and RTT.
    """
    hours = _parse_hours(default=OVERVIEW_HOURS_DEFAULT)
    now = utcnow()
    stats = _host_stats(now - timedelta(hours=hours))
    metas = db.session.query(MonitoredHost).order_by(MonitoredHost.position).all()
    meta_by_host = {m.host: m for m in metas}
    hosts = [m.host for m in metas] + sorted(h for h in stats if h not in meta_by_host)
    # Every host that is not stale has its newest samples in this span.
    span = max([_stale_after(m.interval) for m in metas] + [_stale_after(None)])
    latest = _latest_samples(now - timedelta(seconds=span), FAIL_THRESHOLD)

    latest_ids = db.session.query(func.max(AlertEvent.id)).group_by(AlertEvent.host)
    alerts = {
        a.host: a
        for a in db.session.query(AlertEvent).filter(AlertEvent.id.in_(latest_ids))
    }

    rows = []
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for host in hosts:
        meta = meta_by_host.get(host) or MonitoredHost(
            host=host, name="", group_name="", note=""
        )
        total, losses, avg, mx, last_ok = stats.get(host, (0, 0, None, None, None))
        recent = latest.get(host, [])
        if len(recent) < FAIL_THRESHOLD:  # quiet lately: look further back
            recent = (
                db.session.query(PingResult)
                .filter(PingResult.target_host == host)
                .order_by(PingResult.timestamp.desc())
                .limit(FAIL_THRESHOLD)
                .all()
            )
        if last_ok is None:  # no reply in the window: when was the last one?
            last_ok = (
                db.session.query(func.max(PingResult.timestamp))
                .filter(
                    PingResult.target_host == host, PingResult.packet_loss.is_(False)
                )
                .scalar()
            )
        alert = alerts.get(host)
        status, stale, reasons = _host_status(total, losses, recent, meta, alert, now)
        counts[status] += 1
        rows.append(
            {
                **meta.to_dict(),
                "status": status,
                "stale": stale,
                "reasons": reasons,
                "total": total,
                "lost": losses,
                "loss_pct": (losses * 100.0 / total) if total else None,
                "avg_rtt_ms": float(avg) if avg is not None else None,
                "max_rtt_ms": float(mx) if mx is not None else None,
                "last_ts": _iso(recent[0].timestamp) if recent else None,
                "last_rtt_ms": recent[0].response_time if recent else None,
                "last_loss": bool(recent[0].packet_loss) if recent else None,
                "last_ok_ts": _iso(last_ok),
                "alert": alert.to_dict() if alert is not None else None,
            }
        )
    return jsonify(
        {"hours": hours, "generated_at": _iso(now), "counts": counts, "hosts": rows}
    )


@dashboard_bp.route("/api/alerts")
def recent_alerts():
    """Confirmed outages/recoveries, newest first (``?hours=`` default 24)."""
    hours = _parse_hours()
    limit = _int_arg("limit", 50, 1, 500)
    since = utcnow() - timedelta(hours=hours)
    names = {m.host: m.name for m in db.session.query(MonitoredHost).all()}
    events = (
        db.session.query(AlertEvent)
        .filter(AlertEvent.timestamp >= since)
        .order_by(AlertEvent.timestamp.desc(), AlertEvent.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify([{**e.to_dict(), "name": names.get(e.host, "")} for e in events])


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
    hours = _parse_hours(default=METRICS_HOURS_DEFAULT)
    stats = _host_stats(utcnow() - timedelta(hours=hours))

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
    for host, (total, losses, rtt, _max, _last_ok) in sorted(stats.items()):
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


def _scheduler():
    scheduler = current_app.extensions.get("scheduler")
    if scheduler is None:
        raise ApiError("monitoring scheduler not running", 503)
    return scheduler


@dashboard_bp.route("/api/jobs")
def get_jobs():
    """List the currently scheduled monitoring jobs."""
    return jsonify(_scheduler().list_jobs())


@dashboard_bp.route("/api/hosts", methods=["POST"])
def add_host():
    """Add (or replace) a monitoring job for a host."""
    scheduler = _scheduler()
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    if not host:
        raise ApiError("host is required", 400)
    try:
        interval = int(data.get("interval", DEFAULT_MONITOR_INTERVAL))
    except (TypeError, ValueError):
        raise ApiError("interval must be an integer", 400)
    if interval < 1:
        raise ApiError("interval must be >= 1 second", 400)
    entry = HostConfig(
        name=host,
        interval=interval,
        label=str(data.get("name") or "").strip(),
        group=str(data.get("group") or "").strip(),
        note=str(data.get("note") or "").strip(),
    )
    scheduler.add_ping_jobs([entry])
    upsert_hosts([entry], from_list=False)
    named = {"name": entry.label, "group": entry.group, "note": entry.note}
    body = {"host": host, "interval": interval}
    body.update({k: v for k, v in named.items() if v})
    return jsonify(body), 201


@dashboard_bp.route("/api/hosts/<host>", methods=["DELETE"])
def remove_host(host):
    """Stop monitoring a host."""
    _scheduler().remove_ping_job(host)
    meta = db.session.get(MonitoredHost, host)
    if meta is not None:
        db.session.delete(meta)
        db.session.commit()
    return jsonify({"removed": host})
