"""Self-contained HTML reports (inline CSS, no JavaScript, no external URLs).

Meant to be saved next to a maintenance record or attached to a ticket: it
opens offline on any PC, prints on A4, and states when and from which PC the
measurement was taken.
"""

from __future__ import annotations

import html
import time
from typing import List

from .. import __version__
from ..core import Status
from ..diagnosis import Diagnosis
from ..i18n import get_language, tr
from .common import confidence_word, is_not_applicable, status_word
from .data import hypothesis_runbook
from .text import device_cells

_CSS = """
:root { --fg:#1b1f24; --muted:#5d6670; --bg:#ffffff; --card:#f6f7f9; --line:#d9dde3;
  --ok:#1a7f37; --warn:#9a6700; --fail:#cf222e; --unknown:#8250df; --skip:#6e7781; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6edf3; --muted:#9aa4af; --bg:#0d1117;
  --card:#161b22; --line:#30363d; --ok:#3fb950; --warn:#d29922; --fail:#f85149;
  --unknown:#a371f7; --skip:#8b949e; } }
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI",
  Meiryo, sans-serif; margin: 2rem auto; max-width: 1100px; padding: 0 1rem;
  color: var(--fg); background: var(--bg); line-height: 1.5; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; } h2 { font-size: 1.1rem; margin: 1.6rem 0 .5rem; }
.meta { color: var(--muted); font-size: .85rem; }
.verdict { padding: .8rem 1rem; border-radius: 8px; border-left: 6px solid var(--skip);
  background: var(--card); font-weight: 600; margin: 1rem 0; }
.verdict.ok { border-color: var(--ok); } .verdict.warn { border-color: var(--warn); }
.verdict.fail { border-color: var(--fail); } .verdict.unknown { border-color: var(--unknown); }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { padding: .35rem .5rem; border-bottom: 1px solid var(--line); text-align: left;
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.st { font-weight: 700; white-space: nowrap; }
.ok .st, .st.ok { color: var(--ok); } .warn .st, .st.warn { color: var(--warn); }
.fail .st, .st.fail { color: var(--fail); } .unknown .st, .st.unknown { color: var(--unknown); }
.skip .st, .st.skip, tr.skip td { color: var(--skip); }
.hyp { border-left: 4px solid var(--fail); background: var(--card); padding: .6rem 1rem;
  margin: .8rem 0; border-radius: 0 6px 6px 0; }
.hyp.low { border-color: var(--warn); }
.hyp h3 { font-size: .98rem; margin: 0 0 .3rem; }
.hyp .conf { color: var(--muted); font-weight: normal; font-size: .85rem; }
.hyp ol { margin: .3rem 0 .2rem 1.2rem; padding: 0; } .hyp ul { margin: .2rem 0; }
.cons, .foot { color: var(--muted); font-size: .82rem; }
@media print { body { margin: 0; max-width: none; } .hyp, .verdict { break-inside: avoid; } }
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _page(title: str, body: str, extra_css: str = "") -> str:
    lang = "ja" if get_language() == "ja" else "en"
    return (
        f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{_CSS}{extra_css}</style></head><body>\n{body}\n</body></html>"
    )


def _results_table(results, show_all: bool = True) -> str:
    rows = []
    for r in results:
        if not show_all and is_not_applicable(r):
            continue
        cls = r.status.value
        rows.append(
            f"<tr class='{cls}'><td class='st'>{_esc(r.status.symbol)}</td><td>{_esc(r.layer)}</td>"
            f"<td>{_esc(r.name)}</td><td>{_esc(r.title)}</td><td>{_esc(r.summary)}</td></tr>"
        )
    head = (
        "<tr><th></th><th>"
        + tr("層", "Layer")
        + "</th><th>"
        + tr("項目", "Check")
        + "</th><th>"
        + tr("名称", "Title")
        + "</th><th>"
        + tr("結果", "Result")
        + "</th></tr>"
    )
    return f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def _hypothesis_html(subject: str, h, index: int) -> str:
    steps = "".join(f"<li>{_esc(s)}</li>" for s in hypothesis_runbook(h))
    ev = "".join(f"<li>{_esc(e)}</li>" for e in h.evidence)
    conf = tr("確信度", "confidence") + f": {confidence_word(h.confidence)}"
    title = (
        f"{index}. {subject} — {h.summary}"
        if subject
        else f"{index}. {h.cause} — {h.summary}"
    )
    return (
        f"<div class='hyp {h.confidence}'><h3>{_esc(title)} <span class='conf'>[{_esc(conf)}]</span></h3>"
        + (f"<ul class='ev'>{ev}</ul>" if ev else "")
        + (
            f"<p>{tr('次の確認手順:', 'Next steps:')}</p><ol>{steps}</ol>"
            if steps
            else ""
        )
        + (
            f"<p class='cons'>{tr('連動して失敗/未評価: ', 'Consequent failures: ')}"
            f"{_esc(', '.join(h.consequences))}</p>"
            if h.consequences
            else ""
        )
        + "</div>"
    )


def render_html(diag: Diagnosis) -> str:
    """A single Diagnosis (host sweep / scenario) as a standalone page."""
    status = diag.status
    if status == Status.OK:
        verdict = tr("異常は見つかりませんでした", "All checks passed")
    elif diag.hypotheses:
        verdict = tr(
            f"推定原因 {len(diag.hypotheses)} 件",
            f"{len(diag.hypotheses)} suspected cause(s)",
        )
    else:
        verdict = status_word(status)
    cls = status.value
    hyps = "".join(_hypothesis_html("", h, i) for i, h in enumerate(diag.hypotheses, 1))
    body = (
        f"<h1>netdiag {tr('診断レポート', 'report')} — {_esc(verdict)}</h1>"
        f"<div class='verdict {cls}'>{_esc(verdict)}</div>"
        f"{_results_table(diag.results)}{hyps}"
    )
    return _page("netdiag report", body)


def render_check_html(report, show_all: bool = True) -> str:
    """A full :class:`netdiag.runner.CheckReport` as a standalone page."""
    status = report.status
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.started_at))
    took = max(0.0, report.finished_at - report.started_at)
    bad = sum(1 for d in report.devices if d.status == Status.FAIL)
    warn = sum(1 for d in report.devices if d.status == Status.WARN)
    if status == Status.OK:
        verdict = tr("異常は見つかりませんでした", "No problems found")
    else:
        verdict = status_word(status)
        if report.devices:
            verdict += tr(
                f" — 機器 {len(report.devices)}台中 異常 {bad}台・注意 {warn}台",
                f" — {bad} failing, {warn} warning of {len(report.devices)} devices",
            )
        if report.host_status in (Status.FAIL, Status.WARN):
            verdict += tr("（このPCにも指摘あり）", " (and findings on this PC)")
    meta = (
        f"PC: {_esc(report.hostname)} · {_esc(report.platform)} · {when} · "
        + tr(f"所要 {took:.1f}秒", f"took {took:.1f}s")
        + f" · netdiag {_esc(__version__)}"
    )
    settings = ", ".join(
        f"{k}={v}" for k, v in report.settings.items() if v is not None
    )

    parts: List[str] = [
        f"<h1>netdiag {tr('ネットワーク診断レポート', 'network check report')}</h1>",
        f"<div class='meta'>{meta}</div>",
        f"<div class='verdict {status.value}'>{_esc(status.symbol)} {_esc(verdict)}</div>",
    ]
    if report.devices:
        head = [
            "",
            tr("名前", "Name"),
            tr("アドレス", "Address"),
            tr("応答", "Replies"),
            tr("損失", "Loss"),
            tr("95%上限", "95% max"),
            tr("RTT 中央/p95/最大 (ms)", "RTT med/p95/max (ms)"),
            tr("ポート", "Ports"),
            tr("所見", "Finding"),
            tr("MAC / ベンダー", "MAC / vendor"),
        ]
        rows = []
        for dr in report.devices:
            cells = device_cells(dr)
            m = dr.measurement
            name = cells.name or dr.device.name
            rows.append(
                f"<tr class='{dr.status.value}'><td class='st'>{_esc(dr.status.symbol)}</td>"
                f"<td>{_esc(name)}<div class='meta'>{_esc(dr.device.group)} {_esc(dr.device.note)}</div></td>"
                f"<td class='num'>{_esc(cells.address)}</td><td class='num'>{_esc(cells.replies)}</td>"
                f"<td class='num'>{_esc(cells.loss)}</td><td class='num'>{_esc(cells.loss_bound)}</td>"
                f"<td class='num'>{_esc(cells.rtt)}</td><td>{_esc(cells.ports)}</td>"
                f"<td>{_esc(cells.finding)}</td><td class='num'>{_esc(m.mac)}<div class='meta'>{_esc(m.vendor)}</div></td></tr>"
            )
        parts.append(f"<h2>{tr('機器', 'Devices')} ({len(report.devices)})</h2>")
        parts.append(
            "<table><thead><tr>"
            + "".join(f"<th>{_esc(h)}</th>" for h in head)
            + "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    hyps: List[str] = []
    n = 0
    for h in report.host.hypotheses:
        n += 1
        hyps.append(_hypothesis_html(tr("このPC", "This PC"), h, n))
    for dr in report.devices:
        if dr.diagnosis is None:
            continue
        for h in dr.diagnosis.hypotheses:
            n += 1
            hyps.append(_hypothesis_html(dr.device.label, h, n))
    if hyps:
        parts.append(f"<h2>{tr('診断と次の確認', 'Diagnosis and next steps')}</h2>")
        parts.extend(hyps)

    parts.append(f"<h2>{tr('このPCのネットワーク', 'This PC')}</h2>")
    parts.append(_results_table(report.host.results, show_all=show_all))
    parts.append(
        f"<p class='foot'>{tr('測定条件', 'Settings')}: {_esc(settings)} — "
        + tr(
            "損失率の『95%上限』は、観測回数から見て真の損失率がこれ以下だと95%の確度で言える値です。断続的な問題は回数を増やして測定してください。",
            "'95% max' is the upper bound of the true loss rate given the number of samples; measure longer to catch intermittent problems.",
        )
        + "</p>"
    )
    return _page("netdiag report", "\n".join(parts))
