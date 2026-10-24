"""Terminal rendering: conclusion first, then evidence, then what to do.

Written for someone standing next to the machine with a laptop: the first line
says whether there is a problem and where; the device table fits one device
per line; the diagnosis section gives numbered next steps in plain language.
"""

from __future__ import annotations

import time
from typing import List, Optional

from ..core import CheckResult, Status
from ..diagnosis import Diagnosis
from ..i18n import tr
from .common import (
    Style,
    confidence_word,
    is_not_applicable,
    pad,
    status_word,
    truncate,
    width,
)
from .data import hypothesis_runbook

# Short per-device findings for the table, keyed by result code.
_SHORT = {
    "reach.loss": ("ロスあり", "packet loss"),
    "reach.latency": ("遅延大", "high latency"),
    "reach.icmp_blocked": ("ping無応答(ARPは応答)", "no ping (ARP ok)"),
    "reach.no_arp": ("ARP応答なし", "no ARP reply"),
    "reach.no_reply": ("応答なし", "no reply"),
    "reach.no_icmp": ("ping無応答", "no ping reply"),
    "reach.duplicate_reply": ("IP重複の疑い", "IP conflict?"),
    "reach.mac_mismatch": ("MAC不一致", "MAC mismatch"),
    "reach.unmeasured": ("測定不可", "not measured"),
    "route.no_route": ("サブネット不一致", "subnet mismatch"),
    "route.link_down": ("NICリンクダウン", "NIC link down"),
    "route.dns": ("名前解決不可", "name unresolved"),
    "route.self": ("PC自身のIP", "PC's own IP"),
    "route.special_address": ("使用不可のIP", "unusable IP"),
    "route.ambiguous": ("経路が不定", "ambiguous route"),
    "route.invalid": ("IP形式エラー", "invalid IP"),
    "ports.closed": ("ポート閉", "port closed"),
    "ports.no_answer": ("ポート無応答", "port silent"),
    "ports.flaky": ("接続不安定", "flaky connect"),
    "ports.no_response": ("ping/TCPとも無応答", "no answer at all"),
    "reach.icmp_silent_ports_ok": ("ping無応答・ポート正常", "no ping, ports OK"),
}


def short_finding(code: str) -> str:
    pair = _SHORT.get(code)
    return tr(*pair) if pair else ""


# ------------------------------------------------------------- Diagnosis only


def _result_line(r: CheckResult, st: Style, name_width: int = 11) -> str:
    layer = f"[{r.layer}]".ljust(8)
    return f"{st.status(r.status)} {layer} {r.name.ljust(name_width)} {r.summary}"


def _hypothesis_lines(diag: Diagnosis, st: Style, indent: str = "") -> List[str]:
    lines: List[str] = []
    for i, h in enumerate(diag.hypotheses, 1):
        status = Status.FAIL if h.confidence != "low" else Status.WARN
        head = (
            f"{i}. "
            + tr("推定原因", "Likely cause")
            + f": {h.cause} — {h.summary} ["
            + tr("確信度", "confidence")
            + f": {confidence_word(h.confidence)}]"
        )
        lines.append(indent + st.status(status, st.bold(head)))
        for ev in h.evidence:
            lines.append(f"{indent}     " + tr("根拠", "Evidence") + f": {ev}")
        steps = hypothesis_runbook(h)
        if steps:
            lines.append(f"{indent}     " + tr("次の確認手順:", "Next steps:"))
            for s in steps:
                lines.append(f"{indent}       - {s}")
        if h.consequences:
            lines.append(
                f"{indent}     "
                + tr(
                    "これに連動して失敗した項目 (原因ではなく結果): ",
                    "Failed as a consequence (not causes): ",
                )
                + ", ".join(h.consequences)
            )
    return lines


def render_terminal(
    diag: Diagnosis,
    color: bool = True,
    verbose: bool = False,
    show_all: bool = True,
    unicode: Optional[bool] = None,
) -> str:
    """Render a single Diagnosis (host sweep, scenario, watch escalation)."""
    st = Style(color=color, unicode=unicode)
    lines: List[str] = []
    hidden = 0
    for r in diag.results:
        if not show_all and is_not_applicable(r):
            hidden += 1
            continue
        lines.append(_result_line(r, st))
    if hidden:
        lines.append(
            st.dim(
                tr(
                    f"  （対象外の項目 {hidden} 件は省略 — --all で表示）",
                    f"  ({hidden} not-applicable checks hidden — use --all)",
                )
            )
        )

    lines.append("")
    if diag.healthy:
        lines.append(
            st.status(
                Status.OK,
                st.bold(
                    tr(
                        "診断: 異常は見つかりませんでした。",
                        "Diagnosis: all checks passed.",
                    )
                ),
            )
        )
        return "\n".join(lines)

    if not diag.hypotheses:
        lines.append(
            st.bold(
                tr(
                    "診断: 失敗はありませんが警告があります。",
                    "Diagnosis: no failures, but warnings.",
                )
            )
        )
        return "\n".join(lines)

    lines.append(
        st.bold(f"{st.rule} " + tr("診断 (Diagnosis)", "Diagnosis") + f" {st.rule}")
    )
    lines.extend(_hypothesis_lines(diag, st))
    if verbose and diag.reasoning:
        lines.append("")
        lines.append(st.bold(tr("推論過程:", "Reasoning:")))
        for step in diag.reasoning:
            lines.append(f"  {step}")
    return "\n".join(lines)


# ------------------------------------------------------------ full CheckReport


def port_cell(dr, plain: bool = False) -> str:
    """``502/Modbus/TCP 開, 4840/OPC UA 閉`` for the device table / CSV."""
    from ..probes.device import port_label

    res = dr.result("ports")
    if res is None or not res.detail.get("ports"):
        if dr.device.ports and (res is None or res.status == Status.SKIP):
            return tr("未確認", "not checked")  # configured, but not reached
        return "—" if not plain else ""
    parts = []
    for p in res.detail["ports"]:
        state = {
            "open": tr("開", "open"),
            "closed": tr("閉", "closed"),
            "filtered": tr("無応答", "silent"),
            "unreachable": tr("到達不可", "unreach"),
        }.get(p.get("state"), tr("エラー", "error"))
        label = port_label(p["port"]) if plain else str(p["port"])
        parts.append(f"{label} {state}")
    return ", ".join(parts)


def _fmt_num(v, digits: int = 2) -> str:
    if v is None or v == "":
        return "—"
    v = float(v)
    if v >= 100:
        return f"{v:.0f}"
    if v >= 10:
        return f"{v:.1f}"
    return f"{v:.{digits}f}"


def _device_cells(dr) -> List[str]:
    d = dr.device
    reach = dr.result("reach")
    route = dr.result("route")
    ping = (reach.detail.get("ping") if reach is not None else None) or {}
    rtt = ping.get("rtt") or {}
    addr = (route.detail.get("address") if route is not None else None) or d.host
    if ping.get("sent"):
        replies = f"{ping.get('received', 0)}/{ping['sent']}"
        loss = ping.get("loss_pct")
        loss_s = (
            f"{loss:.0f}%"
            if loss is not None and (loss == 0 or loss >= 1)
            else f"{loss:.1f}%"
        )
        upper = ping.get("loss_upper_pct")
        bound = (
            f"≤{upper:.0f}%"
            if upper is not None and ping.get("received") == ping.get("sent")
            else ""
        )
    else:
        replies, loss_s, bound = "—", "—", ""
    rtt_s = (
        f"{_fmt_num(rtt.get('median'))}/{_fmt_num(rtt.get('p95'))}/{_fmt_num(rtt.get('max'))}"
        if rtt
        else "—"
    )
    if dr.diagnosis is None:
        finding = dr.skipped_reason or ""
    elif dr.diagnosis.primary is not None:
        h = dr.diagnosis.primary
        finding = short_finding(h.code) or truncate(h.summary, 28)
    else:
        # Healthy, but worth a note (e.g. silent to ping, ports fine).
        finding = short_finding(reach.code) if reach is not None else ""
    name = d.name if d.name != d.host else ""
    return [name, addr, replies, loss_s, bound, rtt_s, port_cell(dr), finding]


def _table(
    rows: List[List[str]], header: List[str], max_widths: List[int]
) -> List[str]:
    cols = len(header)
    widths = [width(h) for h in header]
    clipped = [[truncate(c, max_widths[i]) for i, c in enumerate(r)] for r in rows]
    for r in clipped:
        for i in range(cols):
            widths[i] = max(widths[i], width(r[i]))
    out = ["  ".join(pad(header[i], widths[i]) for i in range(cols)).rstrip()]
    for r in clipped:
        out.append("  ".join(pad(r[i], widths[i]) for i in range(cols)).rstrip())
    return out


def render_check_text(
    report,
    color: bool = True,
    verbose: bool = False,
    show_all: bool = False,
    unicode: Optional[bool] = None,
) -> str:
    """Render a :class:`netdiag.runner.CheckReport` for the terminal."""
    st = Style(color=color, unicode=unicode)
    lines: List[str] = []
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.started_at))
    lines.append(
        st.bold(
            f"{st.rule} netdiag "
            + tr("ネットワーク診断", "network check")
            + f" {st.rule}"
        )
        + f"  {when}  PC: {report.hostname}"
    )
    lines.append(_headline(report, st))

    # ---- this PC
    lines.append("")
    lines.append(
        st.bold(f"{st.bullet} " + tr("このPCのネットワーク", "This PC's network"))
    )
    shown = [r for r in report.host.results if show_all or not is_not_applicable(r)]
    title_w = min(22, max((width(r.title) for r in shown), default=8))
    for r in shown:
        name = pad(truncate(r.title, title_w), title_w)
        layer = f"[{r.layer}]".ljust(8)  # "[SYSTEM]" is the widest tag
        extra = st.dim(f" ({r.name})") if verbose else ""
        lines.append(f"  {st.status(r.status)} {layer} {name}  {r.summary}{extra}")
    hidden = len(report.host.results) - len(shown)
    if hidden:
        lines.append(
            st.dim(
                tr(
                    f"  （対象外の項目 {hidden} 件は省略 — --all で表示）",
                    f"  ({hidden} not-applicable checks hidden — use --all)",
                )
            )
        )

    # ---- devices
    if report.devices:
        lines.append("")
        lines.append(
            st.bold(
                f"{st.bullet} "
                + tr(
                    f"機器 {len(report.devices)}台", f"Devices ({len(report.devices)})"
                )
            )
        )
        header = [
            "",
            tr("名前", "Name"),
            tr("アドレス", "Address"),
            tr("応答", "Replies"),
            tr("損失", "Loss"),
            tr("95%上限", "95% max"),
            tr("RTT中央/p95/最大(ms)", "RTT med/p95/max (ms)"),
            tr("ポート", "Ports"),
            tr("所見", "Finding"),
        ]
        rows = []
        statuses = []
        for dr in report.devices:
            rows.append([st.sym(dr.status)] + _device_cells(dr))
            statuses.append(dr.status)
        table = _table(rows, header, [2, 18, 16, 7, 6, 7, 22, 30, 30])
        lines.append("  " + st.dim(table[0]))
        for status, row in zip(statuses, table[1:]):
            sym = st.sym(status)
            # Colour only the leading symbol; widths were computed uncoloured.
            lines.append("  " + st.status(status, sym) + row[len(sym) :])

    # ---- diagnosis
    problems = _problem_blocks(report, st)
    if problems:
        lines.append("")
        lines.append(
            st.bold(f"{st.bullet} " + tr("診断と次の確認", "Diagnosis and next steps"))
        )
        lines.extend(problems)
    if verbose and report.host.reasoning:
        lines.append("")
        lines.append(st.bold(tr("推論過程:", "Reasoning:")))
        lines.extend(f"  {s}" for s in report.host.reasoning)
    return "\n".join(lines)


def _headline(report, st: Style) -> str:
    status = report.status
    n = len(report.devices)
    bad = [d for d in report.devices if d.status == Status.FAIL]
    warn = [d for d in report.devices if d.status == Status.WARN]
    skipped = [d for d in report.devices if d.status == Status.SKIP]
    host_status = report.host_status
    parts: List[str] = []
    if host_status == Status.FAIL:
        parts.append(tr("このPCのネットワークに異常", "a problem on this PC's network"))
    elif host_status == Status.WARN:
        parts.append(tr("このPCに注意点", "warnings on this PC"))
    if bad:
        names = ", ".join(d.device.name for d in bad[:3]) + (
            " …" if len(bad) > 3 else ""
        )
        parts.append(
            tr(
                f"機器 {n}台中 {len(bad)}台に異常（{names}）",
                f"{len(bad)} of {n} devices failing ({names})",
            )
        )
    if warn:
        parts.append(tr(f"{len(warn)}台に注意", f"{len(warn)} with warnings"))
    if skipped:
        parts.append(tr(f"{len(skipped)}台は未評価", f"{len(skipped)} not checked"))
    if status == Status.OK:
        what = (
            tr(f"このPC と 機器 {n}台", f"this PC and {n} device(s)")
            if n
            else tr("このPC", "this PC")
        )
        text = tr(
            f"結論: 異常は見つかりませんでした（{what}）",
            f"Result: no problems found ({what})",
        )
    else:
        text = (
            tr("結論: ", "Result: ") + status_word(status) + " — " + "、".join(parts)
            if parts
            else status_word(status)
        )
        if not report_is_ja():
            text = text.replace("、", "; ")
    return st.status(status, st.bold(f"{st.sym(status)} {text}"))


def report_is_ja() -> bool:
    from ..i18n import is_ja

    return is_ja()


def _problem_blocks(report, st: Style) -> List[str]:
    lines: List[str] = []
    host = report.host
    for h in host.hypotheses:
        lines.extend(_cause_block(tr("このPC", "This PC"), h, st))
    for dr in report.devices:
        if dr.diagnosis is None:
            if dr.skipped_reason:
                lines.append(
                    f"  {st.status(Status.SKIP)} {dr.device.label}: {dr.skipped_reason}"
                )
            continue
        for h in dr.diagnosis.hypotheses:
            lines.extend(_cause_block(dr.device.label, h, st))
    return lines


def _cause_block(subject: str, h, st: Style) -> List[str]:
    status = Status.FAIL if h.confidence != "low" else Status.WARN
    head = f"{subject} — {h.summary}"
    conf = tr("確信度", "confidence") + f": {confidence_word(h.confidence)}"
    out = [f"  {st.status(status)} " + st.bold(head) + st.dim(f"  [{conf}]")]
    for ev in h.evidence:
        if ev and ev not in h.summary:
            out.append("      " + tr("根拠: ", "Evidence: ") + ev)
    steps = hypothesis_runbook(h)
    for i, s in enumerate(steps, 1):
        out.append(f"      {i}. {s}")
    if h.consequences:
        out.append(
            "      "
            + st.dim(
                tr("連動して未評価/失敗: ", "Consequently skipped/failed: ")
                + ", ".join(h.consequences)
            )
        )
    return out
