"""Rich terminal reporter — color-coded diff tables.

Renders :class:`~envcheck.diff.EnvVarDiffResult` and
:class:`~envcheck.services.ServiceDiffResult` as Rich tables using the
PRD color scheme (F4):

- **green** — variables/services that match (ok)
- **yellow** — drift: value changes, type changes, service version/image
  changes
- **red** — present in the base environment but missing from the target
- **gray** — present only in the target environment (extra)

The module is console-agnostic.  Every ``render_*`` function returns a
:class:`rich.table.Table`; every ``print_*`` function accepts an
optional :class:`rich.console.Console` so output can be captured by
tests or redirected by the CLI.

The main entrypoint is :func:`print_report`, which renders both the
environment-variable diff and the service diff (when present) together
with a summary and a final verdict line.
"""

from __future__ import annotations

from typing import Dict, Optional

from rich.console import Console
from rich.table import Table

from envcheck.diff import DiffKind, EnvVarDiffResult
from envcheck.services import ServiceDiffKind, ServiceDiffResult

# ---------------------------------------------------------------------------
# Color scheme (PRD F4: green=ok, yellow=diff, red=missing, gray=extra)
# ---------------------------------------------------------------------------

#: Rich style applied to each environment-variable diff kind.
#: ``TYPE_CHANGED`` shares the yellow "diff" style; the type transition is
#: shown inline in the value cells instead of a separate color.
ENV_VAR_STYLES: Dict[DiffKind, str] = {
    DiffKind.MATCH: "green",
    DiffKind.CHANGED: "yellow",
    DiffKind.TYPE_CHANGED: "yellow",
    DiffKind.MISSING: "red",
    DiffKind.EXTRA: "bright_black",  # renders as gray in most terminals
}

#: Rich style applied to each Docker-service diff kind.
SERVICE_STYLES: Dict[ServiceDiffKind, str] = {
    ServiceDiffKind.MATCH: "green",
    ServiceDiffKind.VERSION_CHANGED: "yellow",
    ServiceDiffKind.IMAGE_CHANGED: "yellow",
    ServiceDiffKind.MISSING: "red",
    ServiceDiffKind.EXTRA: "bright_black",
}

#: Human-readable status label for each environment-variable diff kind.
ENV_VAR_LABELS: Dict[DiffKind, str] = {
    DiffKind.MATCH: "ok",
    DiffKind.CHANGED: "changed",
    DiffKind.TYPE_CHANGED: "type changed",
    DiffKind.MISSING: "missing",
    DiffKind.EXTRA: "extra",
}

#: Human-readable status label for each service diff kind.
SERVICE_LABELS: Dict[ServiceDiffKind, str] = {
    ServiceDiffKind.MATCH: "ok",
    ServiceDiffKind.VERSION_CHANGED: "version changed",
    ServiceDiffKind.IMAGE_CHANGED: "image changed",
    ServiceDiffKind.MISSING: "missing",
    ServiceDiffKind.EXTRA: "extra",
}

#: Long values are truncated to keep tables readable on narrow terminals.
_MAX_VALUE_LEN = 48

#: Placeholder cell for a value that does not exist in one environment.
_NO_VALUE = "—"


# ---------------------------------------------------------------------------
# Cell formatting
# ---------------------------------------------------------------------------


def _format_value(value: Optional[str]) -> str:
    """Truncate a value for display; ``None`` renders as an em dash."""
    if value is None:
        return _NO_VALUE
    if len(value) > _MAX_VALUE_LEN:
        return value[:_MAX_VALUE_LEN - 1] + "…"
    return value


def _format_value_with_type(value: Optional[str], type_name: Optional[str]) -> str:
    """Format a value cell together with its inferred type.

    Used for ``TYPE_CHANGED`` rows so the type transition is visible
    without a dedicated column, e.g. ``1 (int)`` vs ``true (bool)``.
    """
    return f"{_format_value(value)} ({type_name})"


# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------


def render_env_var_diff(result: EnvVarDiffResult) -> Table:
    """Build a color-coded Rich table from an env-var diff result.

    Columns are ``Variable``, ``Status``, the base environment's value,
    and the target environment's value.  Every row is styled according
    to its :class:`~envcheck.diff.DiffKind` (see :data:`ENV_VAR_STYLES`).
    """
    table = Table(
        title=f"Environment variables — {result.base_env} vs {result.target_env}",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Variable", style="bold", no_wrap=True)
    table.add_column("Status")
    table.add_column(result.base_env)
    table.add_column(result.target_env)

    for diff in result.diffs:
        style = ENV_VAR_STYLES[diff.kind]
        label = ENV_VAR_LABELS[diff.kind]

        if diff.kind == DiffKind.TYPE_CHANGED:
            base_cell = _format_value_with_type(diff.base_value, diff.base_type)
            target_cell = _format_value_with_type(diff.target_value, diff.target_type)
        else:
            base_cell = _format_value(diff.base_value)
            target_cell = _format_value(diff.target_value)

        table.add_row(
            diff.key,
            label,
            base_cell,
            target_cell,
            style=style,
        )

    return table


def render_service_diff(result: ServiceDiffResult) -> Table:
    """Build a color-coded Rich table from a service diff result.

    Columns are ``Service``, ``Status``, the base environment's image,
    and the target environment's image (``postgres:16`` vs
    ``postgres:15``).  Every row is styled according to its
    :class:`~envcheck.services.ServiceDiffKind` (see
    :data:`SERVICE_STYLES`).
    """
    table = Table(
        title=f"Docker services — {result.base_env} vs {result.target_env}",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Service", style="bold", no_wrap=True)
    table.add_column("Status")
    table.add_column(result.base_env)
    table.add_column(result.target_env)

    for diff in result.diffs:
        table.add_row(
            diff.service,
            SERVICE_LABELS[diff.kind],
            _format_value(diff.base_image),
            _format_value(diff.target_image),
            style=SERVICE_STYLES[diff.kind],
        )

    return table


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summarize_env_var_diff(result: EnvVarDiffResult) -> str:
    """One-line summary of an env-var comparison (counts per status)."""
    counts: list[str] = []
    if result.matches:
        counts.append(f"{len(result.matches)} ok")
    if result.changed:
        counts.append(f"{len(result.changed)} changed")
    if result.type_changed:
        counts.append(f"{len(result.type_changed)} type-changed")
    if result.missing:
        counts.append(f"{len(result.missing)} missing")
    if result.extra:
        counts.append(f"{len(result.extra)} extra")
    if counts:
        return f"Compared {result.total_compared} variables: " + ", ".join(counts)
    return f"Compared {result.total_compared} variables"


def summarize_service_diff(result: ServiceDiffResult) -> str:
    """One-line summary of a service comparison (counts per status)."""
    counts: list[str] = []
    if result.matches:
        counts.append(f"{len(result.matches)} ok")
    if result.version_changed:
        counts.append(f"{len(result.version_changed)} version-changed")
    if result.image_changed:
        counts.append(f"{len(result.image_changed)} image-changed")
    if result.missing:
        counts.append(f"{len(result.missing)} missing")
    if result.extra:
        counts.append(f"{len(result.extra)} extra")
    if counts:
        return f"Compared {len(result.diffs)} services: " + ", ".join(counts)
    return f"Compared {len(result.diffs)} services"


def build_verdict(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
) -> tuple[str, str]:
    """Return ``(message, rich_style)`` describing the overall drift status.

    A fully green report produces ``("✅ Environments are in sync",
    "bold green")``; any drift produces ``("⚠️ Drift detected: N
    difference(s)", "bold yellow")``.
    """
    drift = 0
    if env_diff is not None:
        drift += env_diff.drift_count
    if service_diff is not None:
        drift += service_diff.drift_count

    if drift == 0:
        return "✅ Environments are in sync", "bold green"
    noun = "difference" if drift == 1 else "differences"
    return f"⚠️ Drift detected: {drift} {noun}", "bold yellow"


# ---------------------------------------------------------------------------
# Printing helpers
# ---------------------------------------------------------------------------


def print_env_var_diff(
    result: EnvVarDiffResult,
    console: Optional[Console] = None,
) -> None:
    """Print an env-var diff table plus its summary to *console*."""
    console = console or Console()
    console.print(render_env_var_diff(result))
    console.print(summarize_env_var_diff(result))


def print_service_diff(
    result: ServiceDiffResult,
    console: Optional[Console] = None,
) -> None:
    """Print a service diff table plus its summary to *console*."""
    console = console or Console()
    console.print(render_service_diff(result))
    console.print(summarize_service_diff(result))


def print_report(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
    console: Optional[Console] = None,
) -> None:
    """Print the full drift report (env vars + services + verdict).

    Each non-``None`` diff is rendered as a table followed by its
    one-line summary; the report ends with a color-coded verdict
    (see :func:`build_verdict`).
    """
    console = console or Console()
    if env_diff is not None:
        console.print(render_env_var_diff(env_diff))
        console.print(summarize_env_var_diff(env_diff))
    if service_diff is not None:
        console.print(render_service_diff(service_diff))
        console.print(summarize_service_diff(service_diff))

    message, style = build_verdict(env_diff, service_diff)
    console.print(message, style=style)
