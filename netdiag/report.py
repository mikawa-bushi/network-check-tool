"""Render a Diagnosis for humans (terminal), machines (JSON), or archive (HTML).

Conclusion-first: the headline is the root cause and the next step. Full evidence
follows for those who want it. The HTML output is self-contained (no CDN, no
JavaScript) so it works in a fully offline environment.
"""

from __future__ import annotations

import html
import json
from typing import List, Optional

from .core import Status
from .diagnosis import Diagnosis, runbook_steps

# ANSI colours; disabled when output isn't a TTY (handled by the caller).
_COLORS = {
    Status.OK: "\033[32m",
    Status.WARN: "\033[33m",
    Status.FAIL: "\033[31m",
    Status.SKIP: "\033[90m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


def render_terminal(diag: Diagnosis, color: bool = True, verbose: bool = False) -> str:
    def c(code: str) -> str:
        return code if color else ""

    lines: List[str] = []
    for r in diag.results:
        col = c(_COLORS.get(r.status, ""))
        layer = f"[{r.layer}]".ljust(8)
        lines.append(
            f"{col}{r.status.symbol}{c(_RESET)} {layer} "
            f"{r.name.ljust(11)} {r.summary}"
        )

    lines.append("")
    if diag.healthy:
        lines.append(
            f"{c(_BOLD)}{c(_COLORS[Status.OK])}診断: 異常は見つかりませんでした。"
            f" All checks passed.{c(_RESET)}"
        )
        return "\n".join(lines)

    if not diag.hypotheses:
        lines.append(f"{c(_BOLD)}診断: 失敗はありませんが警告があります。{c(_RESET)}")
        return "\n".join(lines)

    lines.append(f"{c(_BOLD)}━━━ 診断 (Diagnosis) ━━━{c(_RESET)}")
    for i, h in enumerate(diag.hypotheses, 1):
        col = c(_COLORS[Status.FAIL] if h.confidence != "low" else _COLORS[Status.WARN])
        lines.append(
            f"{col}{c(_BOLD)}{i}. 推定原因: {h.cause} — {h.summary} "
            f"[確信度: {_confidence_ja(h.confidence)}]{c(_RESET)}"
        )
        for ev in h.evidence:
            lines.append(f"     根拠: {ev}")
        steps = runbook_steps(h.cause)
        if steps:
            lines.append("     次の確認手順:")
            for s in steps:
                lines.append(f"       - {s}")
        if h.consequences:
            lines.append(
                f"     これに連動して失敗した項目 (原因ではなく結果): "
                f"{', '.join(h.consequences)}"
            )
    if verbose and diag.reasoning:
        lines.append("")
        lines.append(f"{c(_BOLD)}推論過程:{c(_RESET)}")
        for step in diag.reasoning:
            lines.append(f"  {step}")
    return "\n".join(lines)


def _confidence_ja(conf: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(conf, conf)


def render_json(diag: Diagnosis) -> str:
    return json.dumps(
        {
            "healthy": diag.healthy,
            "results": [r.to_dict() for r in diag.results],
            "hypotheses": [
                {
                    "cause": h.cause,
                    "confidence": h.confidence,
                    "summary": h.summary,
                    "evidence": h.evidence,
                    "consequences": h.consequences,
                    "runbook": runbook_steps(h.cause),
                }
                for h in diag.hypotheses
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


def render_html(diag: Diagnosis) -> str:
    """Self-contained HTML (inline CSS, no external resources)."""

    def esc(s: str) -> str:
        return html.escape(str(s))

    rows = []
    for r in diag.results:
        cls = r.status.value
        rows.append(
            f"<tr class='{cls}'><td>{r.status.symbol}</td><td>{esc(r.layer)}</td>"
            f"<td>{esc(r.name)}</td><td>{esc(r.summary)}</td></tr>"
        )
    hyp_html = ""
    for i, h in enumerate(diag.hypotheses, 1):
        steps = "".join(f"<li>{esc(s)}</li>" for s in runbook_steps(h.cause))
        ev = "".join(f"<li>{esc(e)}</li>" for e in h.evidence)
        hyp_html += (
            f"<div class='hyp {h.confidence}'><h3>{i}. {esc(h.cause)} "
            f"— {esc(h.summary)} <span class='conf'>[{esc(h.confidence)}]</span></h3>"
            f"<ul class='ev'>{ev}</ul>"
            + (f"<p>Next steps:</p><ul>{steps}</ul>" if steps else "")
            + (
                f"<p class='cons'>Consequent failures: "
                f"{esc(', '.join(h.consequences))}</p>"
                if h.consequences
                else ""
            )
            + "</div>"
        )
    verdict = (
        "All checks passed"
        if diag.healthy
        else f"{len(diag.hypotheses)} suspected cause(s)"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>netdiag report</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
 td, th {{ padding: .4rem .6rem; border-bottom: 1px solid #ddd; font-size: .9rem; }}
 tr.ok td:first-child {{ color: #2a7; }} tr.fail td:first-child {{ color: #c33; }}
 tr.warn td:first-child {{ color: #d90; }} tr.skip td {{ color: #999; }}
 .hyp {{ border-left: 4px solid #c33; padding: .5rem 1rem; margin: 1rem 0;
         background: #fafafa; }}
 .hyp.low {{ border-color: #d90; }} .conf {{ color: #888; font-weight: normal; }}
 .cons {{ color: #888; font-size: .85rem; }}
 h1 {{ font-size: 1.3rem; }}
</style></head><body>
<h1>netdiag report — {esc(verdict)}</h1>
<table><thead><tr><th></th><th>Layer</th><th>Check</th><th>Result</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{hyp_html}
</body></html>"""
