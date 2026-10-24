"""``netdiag scenario``: run a declarative YAML/JSON check (one file per line)."""

from __future__ import annotations

import sys

import click

from ._app import DEFAULT_CONFIG, H, cli, color_wanted, format_from_path, write_output


@cli.command()
@click.argument("path", type=click.Path(exists=True))
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
    """シナリオファイル（YAML/JSON）どおりに診断 / Run a declarative diagnosis scenario.

    devices: を含むシナリオは、そのラインの機器も一緒に診断します。
    """
    from .. import report as report_mod
    from ..diagnosis import Engine
    from ..scenario import load_scenario, merged_config

    sc = load_scenario(path)
    cfg = dict(DEFAULT_CONFIG)
    cfg.setdefault("internet", False)
    cfg.update(merged_config(sc))
    fmt = format_from_path(output, fmt)

    if sc.devices:
        from ..runner import run_check

        rep = run_check(sc.devices, config=cfg, only=sc.probe_names or None)
        if fmt == "json":
            text = report_mod.render_check_json(rep)
        elif fmt == "html":
            text = report_mod.render_check_html(rep)
        elif fmt == "csv":
            text = report_mod.render_check_csv(rep)
        else:
            text = f"# {sc.name}\n" + (sc.description + "\n" if sc.description else "")
            text += report_mod.render_check_text(
                rep,
                color=False if output else color_wanted(no_color),
                verbose=reasoning,
            )
        write_output(text, output, fmt)
        sys.exit(rep.exit_code)

    engine = Engine(config=cfg, verbose=reasoning)
    diag = engine.run(only=sc.probe_names)
    if fmt == "json":
        text = report_mod.render_json(diag)
    elif fmt == "html":
        text = report_mod.render_html(diag)
    else:
        header = f"# {sc.name}\n" + (sc.description + "\n\n" if sc.description else "")
        text = header + report_mod.render_terminal(
            diag, color=False if output else color_wanted(no_color), verbose=reasoning
        )
    write_output(text, output, fmt)
    sys.exit(0 if diag.healthy else 1)
