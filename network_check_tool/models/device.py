"""What the line board needs beyond raw pings: device names and alert history."""

import json
from datetime import datetime, timezone

from ..utils import utcnow
from .ping_result import db


class MonitoredHost(db.Model):
    """Display metadata for a monitored address (from a device list or the UI)."""

    __tablename__ = "monitored_hosts"

    host = db.Column(db.String(255), primary_key=True)
    name = db.Column(db.String(255), nullable=False, default="")
    group_name = db.Column(db.String(255), nullable=False, default="")
    note = db.Column(db.Text, nullable=False, default="")
    interval = db.Column(db.Integer, nullable=True)
    rtt_warn_ms = db.Column(db.Float, nullable=True)
    loss_warn_pct = db.Column(db.Float, nullable=True)
    # Order of the device list, so the board reads like the list it came from.
    position = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self):
        return {
            "host": self.host,
            "name": self.name,
            "group": self.group_name,
            "note": self.note,
            "interval": self.interval,
            "rtt_warn_ms": self.rtt_warn_ms,
            "loss_warn_pct": self.loss_warn_pct,
        }


def upsert_hosts(entries, from_list=True):
    """Insert or update ``MonitoredHost`` rows from ``HostConfig``-like objects.

    Each entry needs ``name`` (the address) and may carry ``label``, ``group``,
    ``note``, ``interval``, ``rtt_warn_ms`` and ``loss_warn_pct``. A device list
    (``from_list``) is authoritative: its order and thresholds replace what is
    stored. A single host added from the UI only fills in what it brings and goes
    to the end of the board. Commits.
    """
    existing = {h.host: h for h in db.session.query(MonitoredHost).all()}
    next_pos = max((h.position for h in existing.values()), default=-1) + 1
    for i, e in enumerate(entries):
        row = existing.get(e.name)
        if row is None:
            row = MonitoredHost(host=e.name, position=i if from_list else next_pos)
            next_pos += 1
        elif from_list:
            row.position = i
        row.name = getattr(e, "label", "") or row.name or ""
        row.group_name = getattr(e, "group", "") or row.group_name or ""
        row.note = getattr(e, "note", "") or row.note or ""
        for attr in ("interval", "rtt_warn_ms", "loss_warn_pct"):
            value = getattr(e, attr, None)
            if value is not None or from_list:
                setattr(row, attr, value)
        row.updated_at = utcnow()
        db.session.add(row)
    db.session.commit()


class AlertEvent(db.Model):
    """One confirmed DEGRADED/RECOVERED transition of a monitored host."""

    __tablename__ = "alert_events"
    __table_args__ = (db.Index("ix_alert_events_host_ts", "host", "timestamp"),)

    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(255), nullable=False)
    timestamp = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    event = db.Column(db.String(32), nullable=False)
    summary = db.Column(db.Text, nullable=False, default="")
    cause = db.Column(db.String(255), nullable=True)
    confidence = db.Column(db.String(32), nullable=True)
    diagnosis = db.Column(db.Text, nullable=False, default="")
    runbook = db.Column(db.Text, nullable=False, default="[]")  # JSON list
    duration_s = db.Column(db.Float, nullable=True)

    @classmethod
    def from_transition(cls, t):
        # HostAlerter seeds `cause` with the host itself; only a diagnosis
        # replaces it with a real root-cause code.
        cause = t.cause if t.cause and t.cause != t.host else None
        return cls(
            host=t.host or "",
            timestamp=datetime.fromtimestamp(t.ts, timezone.utc).replace(tzinfo=None),
            event=t.event,
            summary=t.summary or "",
            cause=cause,
            confidence=t.confidence if cause else None,
            diagnosis=t.diagnosis or "",
            runbook=json.dumps(list(t.runbook or []), ensure_ascii=False),
            duration_s=t.duration_s,
        )

    def to_dict(self):
        return {
            "host": self.host,
            "timestamp": self.timestamp.replace(tzinfo=timezone.utc).isoformat(),
            "event": self.event,
            "summary": self.summary,
            "cause": self.cause,
            "confidence": self.confidence,
            "diagnosis": self.diagnosis,
            "runbook": json.loads(self.runbook or "[]"),
            "duration_s": self.duration_s,
        }
