import sys
from pathlib import Path
from typing import Optional

import typer

from envcheck.config import EnvcheckConfig, load_config
from envcheck.diff import diff_env_vars
from envcheck.json_reporter import print_json_report
from envcheck.profile import build_profile
from envcheck.reporter import print_report
from envcheck.services import diff_services

app = typer.Typer(
    name="envcheck",
    help="Environment Parity Checker — detect drift across dev, staging, and prod",
    no_args_is_help=True,
)


@app.callback()
def callback():
    """envcheck: compare environment configurations across environments."""


@app.command()
def version():
    """Show the installed version."""
    from importlib.metadata import version as get_version

    ver = get_version("envcheck")
    typer.echo(f"envcheck v{ver}")


@app.command()
def diff(
    base_env: str = typer.Argument(..., help="Name of the base environment (e.g. dev)"),
    target_env: str = typer.Argument(
        ..., help="Name of the target environment to compare against (e.g. staging)"
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to .envcheck.yaml (auto-discovered from the working directory when omitted)",
    ),
    root: Optional[Path] = typer.Option(
        None,
        "--root",
        help="Project root used to resolve relative paths in .envcheck.yaml (defaults to the working directory)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a machine-readable JSON report instead of the Rich terminal tables",
    ),
):
    """Compare two environments and report drift.

    Scans the configured files for BASE_ENV and TARGET_ENV, builds an
    EnvironmentProfile for each, and reports missing/extra/changed
    environment variables plus Docker service version mismatches.

    By default the report is rendered as color-coded Rich tables.  With
    ``--json`` a single JSON document is written to stdout (see
    :mod:`envcheck.json_reporter` for the schema) for CI consumption.
    """
    try:
        config = load_config(config_path)
        _run_diff(config, base_env, target_env, root, json_output)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _run_diff(
    config: EnvcheckConfig,
    base_env: str,
    target_env: str,
    root: Optional[Path],
    json_output: bool,
) -> None:
    """Load profiles, compute diffs, and print the report."""
    base = build_profile(config, base_env, root=root)
    target = build_profile(config, target_env, root=root)

    env_diff = diff_env_vars(base, target, ignore=config.ignore)
    service_diff = diff_services(base, target, tracked=config.services)

    if json_output:
        print_json_report(env_diff, service_diff, file=sys.stdout)
    else:
        print_report(env_diff, service_diff)
