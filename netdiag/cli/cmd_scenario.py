"""``netdiag scenario``: run a declarative YAML/JSON/TOML check (one file per line)."""

from __future__ import annotations

import sys

import click

from ._app import (
    DEFAULT_CONFIG,
    H,
    Progress,
    cli,
    format_from_path,
    log_file_apart_from,
    output_path,
    write_check_report,
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
    "--all",
    "show_all",
    is_flag=True,
    help=H("対象外の項目も表示", "Also show not-applicable checks."),
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
def scenario(path, fmt, output, show_all, reasoning, no_color):
    """シナリオファイル（YAML/JSON/TOML）どおりに診断 / Run a declarative diagnosis scenario.

    devices: を含むシナリオは、そのラインの機器も一緒に診断します。
    終了コードは check と同じです。
    """
    from ..scenario import load_scenario
    from ..scenario.run import run_scenario

    log_file_apart_from([path])
    sc = load_scenario(path)
    cfg = dict(DEFAULT_CONFIG)
    cfg["internet"] = False
    cfg.update(sc.config)
    fmt = format_from_path(output, fmt)
    output = output_path(output, inputs=[path])

    progress = Progress()
    try:
        rep = run_scenario(sc, cfg, progress=progress, on_probe=progress.checking)
    finally:
        progress.done()
    write_check_report(
        rep,
        fmt,
        output,
        show_all=show_all,
        reasoning=reasoning,
        no_color=no_color,
        header=f"# {sc.name}\n" + (sc.description + "\n" if sc.description else ""),
    )
    sys.exit(rep.exit_code)
