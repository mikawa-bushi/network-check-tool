"""``netdiag scenario``: run a declarative YAML/JSON/TOML check (one file per line)."""

from __future__ import annotations

import sys

import click

from ..i18n import tr
from ._app import (
    DEFAULT_CONFIG,
    H,
    Progress,
    cli,
    color_wanted,
    format_from_path,
    write_output,
)


@cli.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json", "html", "csv"]), default=None
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    help=H("レポートをファイルに保存", "Write the report to a file."),
)
@click.option(
    "--reasoning",
    is_flag=True,
    help=H(
        "診断エンジンの推論過程を表示",
        "Show the engine's step-by-step reasoning in the report.",
    ),
)
@click.option(
    "--no-color", is_flag=True, help=H("色を付けない", "Disable ANSI colour.")
)
def scenario(path, fmt, output, reasoning, no_color):
    """シナリオファイル（YAML/JSON/TOML）どおりに診断 / Run a declarative diagnosis scenario.

    devices: を含むシナリオは、そのラインの機器も一緒に診断します。
    終了コードは check と同じです。
    """
    from .. import report as report_mod
    from ..scenario import load_scenario
    from ..scenario.run import run_scenario

    sc = load_scenario(path)
    cfg = dict(DEFAULT_CONFIG)
    cfg["internet"] = False
    cfg.update(sc.config)
    fmt = format_from_path(output, fmt)

    progress = Progress()
    on_probe = (
        (lambda name: progress(tr(f"確認中: {name}", f"checking {name}")))
        if progress.enabled
        else None
    )
    try:
        rep = run_scenario(sc, cfg, progress=progress, on_probe=on_probe)
    finally:
        progress.done()

    if fmt == "json":
        text = report_mod.render_check_json(rep)
    elif fmt == "html":
        text = report_mod.render_check_html(rep, show_all=True)
    elif fmt == "csv":
        text = report_mod.render_check_csv(rep)
    else:
        text = f"# {sc.name}\n" + (sc.description + "\n" if sc.description else "")
        text += report_mod.render_check_text(
            rep,
            color=False if output else color_wanted(no_color),
            verbose=reasoning,
            unicode=True if output else None,
        )
    write_output(text, output, fmt)
    sys.exit(rep.exit_code)
