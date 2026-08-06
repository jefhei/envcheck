import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from envcheck.config import EnvcheckConfig, load_config
from envcheck.diff import diff_env_vars
from envcheck.exit_codes import ExitCode, compute_exit_code
from envcheck.init import bootstrap
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
def init(
    root: Optional[Path] = typer.Option(
        None,
        "--root",
        help="Project root to scan (defaults to the current working directory)",
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Output path for the generated config (defaults to <root>/.envcheck.yaml)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Accept all discovered environments without prompting (for CI/scripts)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite an existing .envcheck.yaml",
    ),
):
    """Bootstrap a .envcheck.yaml by auto-discovering environment files.

    Scans the project root for .env* files, docker-compose files, and
    Dockerfiles, groups them into environments (dev, staging, prod, ...),
    and writes a valid .envcheck.yaml.

    In interactive mode (the default) the discovered environment names
    are presented and you can confirm or edit the comma-separated list.
    Use --yes to accept the discovery result without prompting.
    """
    base = root.resolve() if root else Path.cwd().resolve()
    out = config_path.resolve() if config_path else base / ".envcheck.yaml"

    try:
        environments = bootstrap(base, out, yes=yes, force=force)
    except (FileNotFoundError, NotADirectoryError, ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=ExitCode.ERROR) from exc

    env_list = ", ".join(environments)
    typer.echo(f"Created {out} with environments: {env_list}")
    if len(environments) >= 2:
        first, second = list(environments)[:2]
        typer.echo(f"Next: run 'envcheck diff {first} {second}' to compare environments")


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
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Fail (exit 1) on any drift, including extra variables present only in the target (for CI gates)",
    ),
):
    """Compare two environments and report drift.

    Scans the configured files for BASE_ENV and TARGET_ENV, builds an
    EnvironmentProfile for each, and reports missing/extra/changed
    environment variables plus Docker service version mismatches.

    By default the report is rendered as color-coded Rich tables.  With
    ``--json`` a single JSON document is written to stdout (see
    :mod:`envcheck.json_reporter` for the schema) for CI consumption.

    Exit codes:

    - ``0`` — environments are in sync (no parity-critical drift)
    - ``1`` — drift detected.  In default mode this means missing,
      changed, or type-changed variables, or any Docker-service drift
      (extra variables alone do not fail the run); with ``--strict``
      any drift at all, extras included, fails.
    - ``2`` — error (missing config, unknown environment, malformed
      YAML, unsupported service configuration, ...)
    """
    try:
        config = load_config(config_path)
        code = _run_diff(config, base_env, target_env, root, json_output, strict)
    except (FileNotFoundError, KeyError, ValueError, OSError, yaml.YAMLError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=ExitCode.ERROR) from exc

    if code != ExitCode.OK:
        raise typer.Exit(code=code)


def _run_diff(
    config: EnvcheckConfig,
    base_env: str,
    target_env: str,
    root: Optional[Path],
    json_output: bool,
    strict: bool,
) -> ExitCode:
    """Load profiles, compute diffs, print the report, and classify the exit code."""
    base = build_profile(config, base_env, root=root)
    target = build_profile(config, target_env, root=root)

    env_diff = diff_env_vars(base, target, ignore=config.ignore)
    service_diff = diff_services(base, target, tracked=config.services)

    if json_output:
        print_json_report(env_diff, service_diff, file=sys.stdout)
    else:
        print_report(env_diff, service_diff)

    return compute_exit_code(env_diff, service_diff, strict=strict)
