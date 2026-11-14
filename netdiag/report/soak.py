"""Rendering a long-duration measurement (:mod:`netdiag.soak`).

The HTML version draws one row per device and one cell per minute (green = no
loss, yellow/orange/red = increasing loss, grey = not measured), so "everything
dropped at 14:03" versus "only the robot drops, every 20 minutes" is visible at
a glance — without JavaScript (cell details are SVG tooltips).
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import time
from typing import Dict, List, Optional

from ..core import Status
from ..i18n import tr
from ..inventory import excel_safe
from ..soak import CLOCK_STEP_NOTE_S, Bucket, DeviceTrack, SoakReport, fmt_secs
from .common import Style, pad, status_word, truncate, width
from .html import _page
from .text import _fmt_num as _fmt_ms


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _kind_text() -> Dict[str, str]:
    return {
        "all": tr("全機器", "all devices"),
        "some": tr("複数台", "several"),
        "single": tr("単独", "single"),
        "unlocated": tr("場所不明", "location unknown"),
    }


def _cell_text(seconds: float) -> str:
    """One heatmap cell / CSV row, in words: 1分, 10秒, 1時間."""
    if seconds % 3600 == 0:
        n = int(seconds // 3600)
        return tr(f"{n}時間", f"{n} h")
    if seconds % 60 == 0:
        n = int(seconds // 60)
        return tr(f"{n}分", f"{n} min")
    return tr(f"{seconds:g}秒", f"{seconds:g} s")


def _span(rep: SoakReport) -> str:
    end = _when(rep.finished_at) if rep.multi_day else rep.at(rep.finished_at)
    return f"{_when(rep.started_at)} 〜 {end}"


def _notes(rep: SoakReport) -> List[str]:
    """Lines that qualify the times shown."""
    if abs(rep.clock_step_s) < CLOCK_STEP_NOTE_S:
        return []
    step = ("+" if rep.clock_step_s > 0 else "-") + fmt_secs(abs(rep.clock_step_s))
    return [
        tr(
            f"測定中にPCの時計が {step} 変更されました。表示の時刻は測定開始時の時計に合わせています。",
            f"This PC's clock was changed by {step} during the measurement; times shown follow the clock at the start.",
        )
    ]


def _device_header(with_downtime: bool) -> List[str]:
    head = [
        "",
        tr("名前", "Name"),
        tr("アドレス", "Address"),
        tr("送信", "Sent"),
        tr("損失", "Lost"),
        tr("損失率(95%上限)", "Loss (95% max)"),
        tr("断の回数", "Drops"),
        tr("最長の断", "Longest"),
    ]
    if with_downtime:
        head.append(tr("断の合計", "Total down"))
    return head + [tr("RTT中央/p95/最大(ms)", "RTT med/p95/max (ms)")]


def _rtt_cell(t: DeviceTrack) -> str:
    rtt = t.rtt_summary()
    return f"{_fmt_ms(rtt['median'])}/{_fmt_ms(rtt['p95'])}/{_fmt_ms(rtt['max'])}"


def _headline(rep: SoakReport, st: Style) -> str:
    status = rep.status
    if status == Status.OK:
        text = tr("結論: 通信断・ロスはありませんでした", "Result: no drops or loss")
    else:
        inc = rep.incidents
        labels = {
            "all": tr("全機器同時の断", "drops on all devices at once"),
            "some": tr("複数台同時の断", "drops on several devices at once"),
            "single": tr("単独の断", "single-device drops"),
            "unlocated": tr("場所不明の断", "drops of unknown location"),
        }
        parts = []
        for k in ("all", "some", "single", "unlocated"):
            n = sum(1 for i in inc if i.kind == k)
            if n:
                parts.append(labels[k] + tr(f" {n}回", f" x{n}"))
        never = sum(1 for t in rep.tracks if t.never_replied)
        if never:
            parts.append(tr(f"応答なし {never}台", f"{never} never replied"))
        sporadic = sum(1 for t in rep.tracks if t.sporadic_loss)
        if sporadic:
            parts.append(
                tr(f"散発的なロス {sporadic}台", f"sporadic loss on {sporadic}")
            )
        findings = rep.findings()
        codes = {f.code for f in findings}
        if "soak.pc_link_flap" in codes:
            parts.insert(0, tr("このPCのリンク切断", "this PC's link dropped"))
        if "soak.nic_errors" in codes:
            parts.append(tr("このPCのNICエラー", "NIC errors on this PC"))
        unmeasured = sum(1 for f in findings if f.code == "soak.not_measured")
        if unmeasured:
            parts.append(tr(f"測定不可 {unmeasured}台", f"{unmeasured} not measured"))
        if "soak.unmeasured_time" in codes:
            parts.append(tr("測定できていない時間帯あり", "time not measured"))
        if "soak.stopped_early" in codes:
            parts.append(tr("測定が途中で停止", "measuring stopped early"))
        text = tr("結論: ", "Result: ") + tr("、", "; ").join(
            parts or [status_word(status)]
        )
    return st.status(status, st.bold(f"{st.sym(status)} {text}"))


def render_soak_text(
    rep: SoakReport, color: bool = False, unicode: Optional[bool] = None
) -> str:
    st = Style(color=color, unicode=unicode)
    now = rep.finished_at
    lines: List[str] = []
    span = _span(rep)
    lines.append(
        st.bold(
            f"{st.rule} netdiag "
            + tr("長時間測定", "long-duration test")
            + f" {st.rule}"
        )
        + f"  {span} ({fmt_secs(now - rep.started_at)})  PC: {rep.hostname}"
        + (tr("  ※途中で終了", "  (stopped early)") if rep.interrupted else "")
    )
    lines.extend(st.dim(n) for n in _notes(rep))
    lines.append(_headline(rep, st))
    lines.append("")
    lines.append(st.bold(f"{st.bullet} " + tr("機器ごとの結果", "Per device")))
    header = _device_header(with_downtime=False)
    rows = []
    for t in rep.tracks:
        upper = t.loss_upper_pct
        loss = f"{t.loss_pct:.2f}%"
        if t.lost == 0 and upper is not None:
            loss = f"0% (≤{upper:.2f}%)"
        rows.append(
            [
                st.sym(rep.device_status(t)),
                truncate(t.name, 18),
                t.address or t.device.host,
                str(t.sent),
                str(t.lost),
                loss if t.sent else "—",
                str(len(t.outages)),
                fmt_secs(t.longest_outage(now)) if t.outages else "—",
                _rtt_cell(t),
            ]
        )
    widths = [max(width(r[i]) for r in rows + [header]) for i in range(len(header))]
    lines.append(
        "  "
        + st.dim(
            "  ".join(pad(header[i], widths[i]) for i in range(len(header))).rstrip()
        )
    )
    for t, r in zip(rep.tracks, rows):
        body = "  ".join(pad(r[i], widths[i]) for i in range(len(r))).rstrip()
        sym = r[0]
        lines.append("  " + st.status(rep.device_status(t), sym) + body[len(sym) :])

    incidents = rep.incidents
    if incidents:
        lines.append("")
        lines.append(
            st.bold(f"{st.bullet} " + tr("通信断の発生時刻", "When the drops happened"))
        )
        kind_text = _kind_text()
        for i in incidents[:50]:
            lines.append(
                f"  {rep.at(i.start)}〜{rep.at(i.end)} ({fmt_secs(i.end - i.start)})  "
                f"[{kind_text[i.kind]}] {', '.join(i.devices)}"
            )
        if len(incidents) > 50:
            lines.append(
                st.dim(
                    tr(
                        f"  …ほか {len(incidents) - 50} 件",
                        f"  … {len(incidents) - 50} more",
                    )
                )
            )

    lines.append("")
    lines.append(
        st.bold(f"{st.bullet} " + tr("診断と次の確認", "Diagnosis and next steps"))
    )
    for f in rep.findings():
        lines.append(
            f"  {st.status(f.status)} " + st.bold(f"{f.subject} — {f.summary}")
        )
        for ev in f.evidence:
            lines.append("      " + tr("根拠: ", "Evidence: ") + ev)
        for n, s in enumerate(f.steps, 1):
            lines.append(f"      {n}. {s}")
    lines.append("")
    lines.append(
        st.dim(
            tr(
                f"測定条件: 間隔 {rep.interval}秒・応答待ち {rep.timeout}秒・{rep.min_lost}回連続ロスで「断」",
                f"settings: interval {rep.interval}s, timeout {rep.timeout}s, "
                f"{rep.min_lost} consecutive losses = a drop",
            )
        )
    )
    return "\n".join(lines)


def render_soak_json(rep: SoakReport) -> str:
    return json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)


_CSV_HEADER = [
    ("time", ("時刻", "time")),
    ("name", ("名前", "name")),
    ("host", ("ホスト", "host")),
    ("sent", ("送信", "sent")),
    ("lost", ("損失", "lost")),
    ("loss_pct", ("損失率%", "loss_pct")),
    ("rtt_median_ms", ("RTT中央値ms", "rtt_median_ms")),
    ("rtt_p95_ms", ("RTT_p95_ms", "rtt_p95_ms")),
    ("rtt_max_ms", ("RTT最大ms", "rtt_max_ms")),
]


def csv_header() -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(
        [tr(ja, en) for _k, (ja, en) in _CSV_HEADER]
    )
    return buf.getvalue()


def csv_rows(track: DeviceTrack, buckets: List[Bucket]) -> str:
    """CSV lines for some closed minutes of one device (for progressive files)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    stamp = "%Y-%m-%d %H:%M" if track.bucket_s % 60 == 0 else "%Y-%m-%d %H:%M:%S"
    for b in buckets:
        row = {
            **b.to_dict(),
            "time": time.strftime(stamp, time.localtime(b.start)),
            "name": excel_safe(track.name),
            "host": excel_safe(track.device.host),
        }
        w.writerow(["" if row[k] is None else row[k] for k, _label in _CSV_HEADER])
    return buf.getvalue()


def render_soak_csv(rep: SoakReport) -> str:
    """One row per device per minute (open in Excel and chart it)."""
    parts = [csv_header()]
    for t in rep.tracks:
        parts.append(csv_rows(t, sorted(t.buckets.values(), key=lambda b: b.start)))
    return "".join(parts)


# -------------------------------------------------------------------------- HTML

_HTML_INCIDENTS = 200


def _cell_color(b: Optional[Bucket]) -> str:
    if b is None or not b.sent:
        return "var(--nodata)"
    pct = b.loss_pct
    if pct == 0:
        return "var(--h0)"
    if pct < 1:
        return "var(--l1)"
    if pct < 10:
        return "var(--l2)"
    if pct < 50:
        return "var(--l3)"
    return "var(--l4)"


def _cells_tip(name: str, cells, clock: str) -> str:
    """Tooltip for consecutive heatmap cells ``[(start, bucket or None), ...]``."""
    when = time.strftime(clock, time.localtime(cells[0][0]))
    if len(cells) > 1:
        when += "–" + time.strftime(clock, time.localtime(cells[-1][0]))
    tip = f"{when} {name}: "
    bs = [b for _s, b in cells if b is not None and b.sent]
    if not bs:
        return tip + tr("測定なし", "not measured")
    sent, lost = sum(b.sent for b in bs), sum(b.lost for b in bs)
    pct = lost / sent * 100.0
    tip += tr(
        f"送信 {sent} 損失 {lost} ({pct:.1f}%)", f"sent {sent} lost {lost} ({pct:.1f}%)"
    )
    if len(bs) == 1 and bs[0].median is not None:
        tip += tr(
            f" RTT中央 {_fmt_ms(bs[0].median)}ms",
            f" RTT median {_fmt_ms(bs[0].median)}ms",
        )
    maxima = [b.max for b in bs if b.max is not None]
    if maxima:
        tip += tr(f" 最大 {_fmt_ms(max(maxima))}ms", f" max {_fmt_ms(max(maxima))}ms")
    return tip


def _heatmap(rep: SoakReport) -> str:
    # Every cell of the run, so unmeasured time shows as grey, not as nothing.
    size = rep.bucket_s
    first = math.floor(rep.started_at / size) * size
    n = max(0, math.ceil((rep.finished_at - first) / size))
    starts = sorted(
        {first + i * size for i in range(n)}
        | {b.start for t in rep.tracks for b in t.buckets.values()}
    )
    if not starts:
        return ""
    label_w, cell, row_h = 170, max(4, min(18, int(820 / max(1, len(starts))))), 20
    width_px = label_w + cell * len(starts) + 10
    height_px = 30 + row_h * len(rep.tracks)
    out = [
        f'<svg class="heat" viewBox="0 0 {width_px} {height_px}" width="{width_px}" '
        f'height="{height_px}" role="img" aria-label="{html.escape(_heat_title(rep))}">'
    ]
    clock = "%H:%M:%S" if rep.bucket_s % 60 else "%H:%M"
    if rep.multi_day:
        clock = "%m/%d " + clock
    step = max(1, len(starts) // 8)
    for i, s in enumerate(starts):
        if i % step == 0:
            x = label_w + i * cell
            out.append(
                f'<text x="{x}" y="14" class="tick">{time.strftime(clock, time.localtime(s))}</text>'
            )
    for r, t in enumerate(rep.tracks):
        y = 22 + r * row_h
        out.append(
            f'<text x="0" y="{y + 13}" class="lbl">{html.escape(truncate(t.name, 22))}</text>'
        )
        by_start: Dict[float, Bucket] = {b.start: b for b in t.buckets.values()}
        cells = [(s, by_start.get(s)) for s in starts]
        # One rect per run of same-colour minutes: an overnight run of healthy
        # minutes stays one element instead of hundreds.
        i = 0
        while i < len(cells):
            color = _cell_color(cells[i][1])
            j = i + 1
            while j < len(cells) and _cell_color(cells[j][1]) == color:
                j += 1
            out.append(
                f'<rect x="{label_w + i * cell}" y="{y}" width="{(j - i) * cell - 1}" '
                f'height="{row_h - 3}" fill="{color}"><title>'
                f"{html.escape(_cells_tip(t.name, cells[i:j], clock))}</title></rect>"
            )
            i = j
    out.append("</svg>")
    return "".join(out)


def _heat_title(rep: SoakReport) -> str:
    cell = _cell_text(rep.bucket_s)
    return tr(f"{cell}ごとの損失率", f"Loss per {cell}")


# Added to the shared report stylesheet (report/html.py): heatmap and findings.
_SOAK_CSS = """
:root { --h0:#2da44e; --l1:#d4a72c; --l2:#e16f24; --l3:#cf222e; --l4:#82071e; --nodata:#d0d7de; }
@media (prefers-color-scheme: dark) { :root { --nodata:#30363d; } }
.heat { max-width: 100%; height: auto; } .heat text { fill: var(--fg); font-size: 11px; }
.legend span { display: inline-block; width: 12px; height: 12px; margin: 0 .2rem 0 .8rem;
  vertical-align: middle; }
.find { border-left: 4px solid var(--fail); background: var(--card); padding: .6rem 1rem;
  margin: .8rem 0; border-radius: 0 6px 6px 0; } .find.warn { border-color: var(--warn); }
.find.ok { border-color: var(--ok); } .find h3 { font-size: .98rem; margin: 0 0 .3rem; }
@media print { .find { break-inside: avoid; } }
"""


def render_soak_html(rep: SoakReport) -> str:
    from .. import __version__

    esc = html.escape
    now = rep.finished_at
    status = rep.status
    verdict_text = _headline(rep, Style(color=False, unicode=True))
    rows = []
    for t in rep.tracks:
        upper = t.loss_upper_pct
        loss = f"{t.loss_pct:.3f}%" + (
            f" (≤{upper:.3f}%)" if t.lost == 0 and upper is not None else ""
        )
        s = rep.device_status(t)
        rows.append(
            f"<tr class='{s.value}'><td class='st'>{esc(s.symbol)}</td><td>{esc(t.name)}</td>"
            f"<td class='num'>{esc(t.address or t.device.host)}</td><td class='num'>{t.sent}</td>"
            f"<td class='num'>{t.lost}</td><td class='num'>{esc(loss)}</td>"
            f"<td class='num'>{len(t.outages)}</td>"
            f"<td class='num'>{esc(fmt_secs(t.longest_outage(now)) if t.outages else '—')}</td>"
            f"<td class='num'>{esc(fmt_secs(t.downtime(now)) if t.outages else '—')}</td>"
            f"<td class='num'>{_rtt_cell(t)}</td></tr>"
        )
    head = _device_header(with_downtime=True)
    kind_text = _kind_text()
    incidents = rep.incidents
    inc_rows = "".join(
        f"<tr><td class='num'>{esc(rep.at(i.start))}〜{esc(rep.at(i.end))}</td>"
        f"<td class='num'>{esc(fmt_secs(i.end - i.start))}</td><td>{esc(kind_text[i.kind])}</td>"
        f"<td>{esc(', '.join(i.devices))}</td></tr>"
        for i in incidents[:_HTML_INCIDENTS]
    )
    if len(incidents) > _HTML_INCIDENTS:
        more = len(incidents) - _HTML_INCIDENTS
        inc_rows += (
            "<tr><td colspan='4' class='meta'>"
            + esc(
                tr(
                    f"…ほか {more} 件（全件は --format json で出力できます）",
                    f"… {more} more (--format json lists them all)",
                )
            )
            + "</td></tr>"
        )
    finds = "".join(
        f"<div class='find {f.status.value}'><h3>{esc(f.status.symbol)} {esc(f.subject)} — {esc(f.summary)}</h3>"
        + "".join(f"<p class='meta'>{esc(e)}</p>" for e in f.evidence)
        + (
            f"<ol>{''.join(f'<li>{esc(s)}</li>' for s in f.steps)}</ol>"
            if f.steps
            else ""
        )
        + "</div>"
        for f in rep.findings()
    )
    cell = _cell_text(rep.bucket_s)
    legend = (
        "<p class='legend meta'>"
        + esc(tr(f"1マス = {cell}：", f"one cell = {cell}: "))
        + f"<span style='background:var(--h0)'></span>{tr('損失なし', 'no loss')}"
        + "<span style='background:var(--l1)'></span>&lt;1%"
        + "<span style='background:var(--l2)'></span>1–10%"
        + "<span style='background:var(--l3)'></span>10–50%"
        + "<span style='background:var(--l4)'></span>≥50%"
        + f"<span style='background:var(--nodata)'></span>{tr('測定なし', 'not measured')}</p>"
    )
    body = (
        f"<h1>netdiag {esc(tr('長時間測定レポート', 'long-duration test report'))}</h1>"
        f"<div class='meta'>PC: {esc(rep.hostname)} · {esc(_when(rep.started_at))} 〜 {esc(_when(rep.finished_at))}"
        f" ({esc(fmt_secs(now - rep.started_at))}) · netdiag {esc(__version__)}</div>"
        + "".join(f"<p class='meta'>{esc(n)}</p>" for n in _notes(rep))
        + f"<div class='verdict {status.value}'>{esc(verdict_text)}</div>"
        f"<h2>{esc(_heat_title(rep))}</h2>{_heatmap(rep)}{legend}"
        f"<h2>{esc(tr('機器ごとの結果', 'Per device'))}</h2>"
        "<table><thead><tr>"
        + "".join(f"<th>{esc(h)}</th>" for h in head)
        + "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        + (
            f"<h2>{esc(tr('通信断の発生時刻', 'When the drops happened'))}</h2><table><thead><tr>"
            f"<th>{esc(tr('時刻', 'Time'))}</th><th>{esc(tr('長さ', 'Length'))}</th>"
            f"<th>{esc(tr('範囲', 'Scope'))}</th><th>{esc(tr('機器', 'Devices'))}</th></tr></thead>"
            f"<tbody>{inc_rows}</tbody></table>"
            if inc_rows
            else ""
        )
        + f"<h2>{esc(tr('診断と次の確認', 'Diagnosis and next steps'))}</h2>{finds}"
        + "<p class='foot'>"
        + esc(
            tr(
                f"測定条件: 間隔 {rep.interval}秒・応答待ち {rep.timeout}秒・{rep.min_lost}回連続ロスを「断」として記録。",
                f"Settings: interval {rep.interval}s, timeout {rep.timeout}s; "
                f"{rep.min_lost} consecutive losses are recorded as a drop.",
            )
        )
        + "</p>"
    )
    return _page("netdiag soak", body, _SOAK_CSS)
