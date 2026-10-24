"""Machine-readable renderings: JSON (stable English keys) and CSV (for Excel)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

from ..core import CheckResult
from ..diagnosis import Diagnosis
from ..diagnosis.rules import runbook_steps
from ..i18n import tr
from ..inventory import excel_safe


def hypothesis_runbook(h) -> List[str]:
    """A hypothesis' contextual steps, falling back to the probe's generic ones."""
    return list(getattr(h, "runbook", None) or runbook_steps(h.cause))


def diagnosis_to_dict(diag: Diagnosis) -> Dict[str, Any]:
    return {
        "healthy": diag.healthy,
        "results": [r.to_dict() for r in diag.results],
        "hypotheses": [
            {
                "cause": h.cause,
                "code": getattr(h, "code", ""),
                "title": getattr(h, "title", ""),
                "confidence": h.confidence,
                "summary": h.summary,
                "evidence": h.evidence,
                "consequences": h.consequences,
                "runbook": hypothesis_runbook(h),
            }
            for h in diag.hypotheses
        ],
    }


def _json_default(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    return str(o)


def render_json(diag: Diagnosis) -> str:
    return json.dumps(
        diagnosis_to_dict(diag), indent=2, ensure_ascii=False, default=_json_default
    )


def render_check_json(report) -> str:
    return json.dumps(
        report.to_dict(), indent=2, ensure_ascii=False, default=_json_default
    )


# -------------------------------------------------------------------------- CSV


def device_row(dr) -> Dict[str, Any]:
    """Flatten one device's outcome into spreadsheet columns."""
    from .text import port_cell

    reach: Optional[CheckResult] = dr.result("reach")
    ping = (reach.detail.get("ping") if reach is not None else None) or {}
    rtt = ping.get("rtt") or {}
    primary = dr.diagnosis.primary if dr.diagnosis else None
    d = dr.device
    return {
        "status": dr.status.value,
        "name": d.name,
        "host": d.host,
        "group": d.group,
        "address": (
            dr.result("route").detail.get("address") if dr.result("route") else None
        )
        or "",
        "received": ping.get("received", ""),
        "sent": ping.get("sent", ""),
        "loss_pct": ping.get("loss_pct", ""),
        "loss_upper_pct": ping.get("loss_upper_pct", ""),
        "rtt_median_ms": rtt.get("median", ""),
        "rtt_p95_ms": rtt.get("p95", ""),
        "rtt_max_ms": rtt.get("max", ""),
        "jitter_ms": rtt.get("jitter", ""),
        "mac": (reach.detail.get("mac") if reach is not None else "") or "",
        "vendor": (reach.detail.get("vendor") if reach is not None else "") or "",
        "ports": port_cell(dr, plain=True),
        "cause": primary.summary if primary else (dr.skipped_reason or ""),
        "next_step": (
            hypothesis_runbook(primary)[0]
            if primary and hypothesis_runbook(primary)
            else ""
        ),
        "note": d.note,
    }


_CSV_HEADERS = [
    ("status", ("状態", "status")),
    ("name", ("名前", "name")),
    ("host", ("ホスト", "host")),
    ("group", ("グループ", "group")),
    ("address", ("IPアドレス", "address")),
    ("received", ("応答数", "received")),
    ("sent", ("送信数", "sent")),
    ("loss_pct", ("損失率%", "loss_pct")),
    ("loss_upper_pct", ("損失率95%上限%", "loss_upper_pct")),
    ("rtt_median_ms", ("RTT中央値ms", "rtt_median_ms")),
    ("rtt_p95_ms", ("RTT_p95_ms", "rtt_p95_ms")),
    ("rtt_max_ms", ("RTT最大ms", "rtt_max_ms")),
    ("jitter_ms", ("ジッタms", "jitter_ms")),
    ("mac", ("MACアドレス", "mac")),
    ("vendor", ("ベンダー", "vendor")),
    ("ports", ("ポート", "ports")),
    ("cause", ("診断", "diagnosis")),
    ("next_step", ("次の確認", "next_step")),
    ("note", ("備考", "note")),
]


def render_check_csv(report) -> str:
    """One row per device; without devices, one row per host check.

    Write it with a UTF-8 BOM (``utf-8-sig``) so Excel shows Japanese correctly.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if report.devices:
        w.writerow([tr(ja, en) for _key, (ja, en) in _CSV_HEADERS])
        for dr in report.devices:
            row = device_row(dr)
            w.writerow([excel_safe(row.get(key, "")) for key, _labels in _CSV_HEADERS])
        return buf.getvalue()
    w.writerow(
        [
            tr("状態", "status"),
            tr("層", "layer"),
            tr("項目", "check"),
            tr("名称", "title"),
            tr("結果", "result"),
            "code",
        ]
    )
    for r in report.host.results:
        w.writerow(
            [
                excel_safe(c)
                for c in (r.status.value, r.layer, r.name, r.title, r.summary, r.code)
            ]
        )
    return buf.getvalue()
