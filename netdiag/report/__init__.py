"""Render diagnoses for humans (terminal, HTML) and machines (JSON, CSV).

Conclusion-first: the headline is the verdict and where the problem is; the
evidence and the numbered next steps follow. HTML output is self-contained (no
CDN, no JavaScript) so it works on an offline shop-floor PC.

* ``render_terminal`` / ``render_json`` / ``render_html`` take a single
  :class:`~netdiag.diagnosis.Diagnosis` (host sweep, scenario, watch).
* ``render_check_*`` take a full :class:`~netdiag.runner.CheckReport` (this PC
  plus every device).
"""

from .common import is_not_applicable, unicode_ok
from .data import diagnosis_to_dict, render_check_csv, render_check_json, render_json
from .html import render_check_html, render_html
from .text import render_check_text, render_terminal

__all__ = [
    "render_terminal",
    "render_json",
    "render_html",
    "render_check_text",
    "render_check_json",
    "render_check_html",
    "render_check_csv",
    "diagnosis_to_dict",
    "is_not_applicable",
    "unicode_ok",
]
