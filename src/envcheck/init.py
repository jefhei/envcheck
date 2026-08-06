"""`envcheck init` — bootstrap a ``.envcheck.yaml`` by auto-discovery (M3.1).

The command scans a project root for environment configuration files:

- ``.env*`` files (``.env``, ``.env.dev``, ``.env.staging``, ``.env.prod``,
  ``.env.example``, ``.env.local``, ...)
- docker-compose files (``docker-compose.yml``, ``compose.yml`` and their
  per-environment variants like ``docker-compose.staging.yml``)
- Dockerfiles (``Dockerfile``, ``Containerfile`` and per-environment
  variants like ``Dockerfile.dev``)

Files are grouped into environments using file-name heuristics (see
:func:`group_environments`), and a valid ``.envcheck.yaml`` is written.
In interactive mode the user is prompted for the list of environment
names; ``--yes`` accepts the discovered environments without prompting
for CI/scripted use.

The generated config always passes :class:`envcheck.config.EnvcheckConfig`
validation — the render step is verified before anything is written.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from envcheck.config import EnvcheckConfig

#: Name used for the base environment (files with no suffix: ``.env``,
#: ``docker-compose.yml``, ``Dockerfile``).
DEFAULT_ENV = "dev"

# ---------------------------------------------------------------------------
# Discovery — file-name classification
# ---------------------------------------------------------------------------

#: ``.env`` or ``.env.<suffix>`` (excludes ``.envrc``, ``.envcheck.yaml``)
_ENV_FILE_RE = re.compile(r"^\.env(?:\.([A-Za-z0-9][A-Za-z0-9._-]*))?$")

#: docker-compose base names (no environment suffix)
_COMPOSE_BASE_NAMES = frozenset(
    {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
)

#: docker-compose per-environment variants: docker-compose.<env>.yml
_COMPOSE_VARIANT_RE = re.compile(
    r"^(?:docker-compose|compose)\.([A-Za-z0-9][A-Za-z0-9._-]*)\.ya?ml$"
)

#: Dockerfile base names (no environment suffix)
_DOCKERFILE_BASE_NAMES = frozenset({"Dockerfile", "Containerfile"})

#: Dockerfile per-environment variants: Dockerfile.<env>
_DOCKERFILE_VARIANT_RE = re.compile(
    r"^(?:Dockerfile|Containerfile)\.([A-Za-z0-9][A-Za-z0-9._-]*)$"
)

#: Valid characters for an environment name (YAML key + CLI argument safe).
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def discover_files(root: Path) -> Dict[str, List[Path]]:
    """Categorize environment config files found in *root*.

    Scans the top level of *root* only (environment files conventionally
    live at the project root).  Returns a mapping of category
    (``"env"``, ``"compose"``, ``"dockerfile"``) to a sorted list of
    file paths.

    Parameters
    ----------
    root:
        Project root directory to scan.

    Returns
    -------
    dict[str, list[Path]]
        Category → discovered files, sorted by name for determinism.
    """
    env_files: List[Path] = []
    compose_files: List[Path] = []
    dockerfile_files: List[Path] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        name = entry.name

        if _ENV_FILE_RE.match(name):
            env_files.append(entry)
        elif name in _COMPOSE_BASE_NAMES or _COMPOSE_VARIANT_RE.match(name):
            compose_files.append(entry)
        elif name in _DOCKERFILE_BASE_NAMES or _DOCKERFILE_VARIANT_RE.match(name):
            dockerfile_files.append(entry)
        # Everything else (README.md, .gitignore, pyproject.toml,
        # *.dockerignore, ...) is not an environment config.

    return {
        "env": env_files,
        "compose": compose_files,
        "dockerfile": dockerfile_files,
    }


def _suffix_from_env_name(name: str) -> Optional[str]:
    """Extract the environment suffix from a ``.env``-style file name."""
    m = _ENV_FILE_RE.match(name)
    return m.group(1) if m else None


def group_environments(files: Dict[str, List[Path]], root: Path) -> Dict[str, List[str]]:
    """Group discovered files into per-environment relative path lists.

    Heuristics:

    - ``.env``, ``docker-compose.yml``, ``Dockerfile`` (no suffix) →
      the default environment (``dev``)
    - ``.env.<suffix>`` → environment ``<suffix>``
    - ``docker-compose.<suffix>.yml`` → environment ``<suffix>``
    - ``Dockerfile.<suffix>`` → environment ``<suffix>``

    Returns an ordered mapping (default environment first, then the rest
    alphabetically) of environment name → sorted relative file paths.

    Parameters
    ----------
    files:
        Output of :func:`discover_files`.
    root:
        Project root, used to compute paths relative to it.

    Returns
    -------
    dict[str, list[str]]
        Environment name → relative paths (POSIX separators).
    """
    envs: Dict[str, List[str]] = {}
    base_files: List[str] = []

    def rel(path: Path) -> str:
        return path.relative_to(root).as_posix()

    # .env* files
    for path in files["env"]:
        suffix = _suffix_from_env_name(path.name)
        if suffix:
            envs.setdefault(suffix, []).append(rel(path))
        else:
            base_files.append(rel(path))

    # docker-compose files
    for path in files["compose"]:
        m = _COMPOSE_VARIANT_RE.match(path.name)
        if m:
            envs.setdefault(m.group(1), []).append(rel(path))
        else:
            base_files.append(rel(path))

    # Dockerfiles
    for path in files["dockerfile"]:
        m = _DOCKERFILE_VARIANT_RE.match(path.name)
        if m:
            envs.setdefault(m.group(1), []).append(rel(path))
        else:
            base_files.append(rel(path))

    # Merge base files into the default environment
    if base_files:
        envs.setdefault(DEFAULT_ENV, []).extend(sorted(base_files))

    # Deterministic ordering: default environment first, then alphabetical
    ordered: Dict[str, List[str]] = {}
    if DEFAULT_ENV in envs:
        ordered[DEFAULT_ENV] = sorted(envs.pop(DEFAULT_ENV))
    for env_name in sorted(envs):
        ordered[env_name] = sorted(envs[env_name])
    return ordered


# ---------------------------------------------------------------------------
# Environment-name handling
# ---------------------------------------------------------------------------


def validate_env_name(name: str) -> str:
    """Validate an environment name; raise ``ValueError`` if invalid."""
    if not _ENV_NAME_RE.match(name):
        raise ValueError(
            f"Invalid environment name {name!r} — use letters, digits, "
            f"dots, dashes, or underscores (must start with a letter or digit)"
        )
    return name


def parse_env_names(raw: str) -> List[str]:
    """Parse and validate a comma-separated list of environment names."""
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise ValueError("No environment names provided")
    for name in names:
        validate_env_name(name)
    # De-duplicate while preserving order
    seen: set[str] = set()
    result: List[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def apply_env_names(
    proposed: Dict[str, List[str]], names: List[str]
) -> Dict[str, List[str]]:
    """Rebuild the environment mapping from a user-chosen name list.

    - Base files (from the default environment) attach to ``dev`` if it
      is in *names*, otherwise to the first name.
    - Suffix environments are kept when their name is in *names*;
      otherwise they are dropped (callers may warn about the drop).
    - Names with no discovered files get an empty path list.

    Returns
    -------
    dict[str, list[str]]
        Ordered mapping of chosen environment name → relative paths.
    """
    result: Dict[str, List[str]] = {name: [] for name in names}

    base_target = DEFAULT_ENV if DEFAULT_ENV in result else (names[0] if names else DEFAULT_ENV)
    if DEFAULT_ENV in proposed:
        result[base_target] = list(proposed[DEFAULT_ENV])

    for env_name, paths in proposed.items():
        if env_name == DEFAULT_ENV:
            continue
        if env_name in result:
            # Extend, not replace: the base files may already live here
            # when this env is also the base target (e.g. no "dev" chosen).
            result[env_name] = sorted(set(result[env_name]) | set(paths))

    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_config(environments: Dict[str, List[str]]) -> str:
    """Render an environment mapping as a valid ``.envcheck.yaml`` file.

    The output is human-formatted YAML (not dumped with ``yaml.safe_dump``
    so list indentation and comments stay readable) and is guaranteed to
    load through :class:`envcheck.config.EnvcheckConfig`.
    """
    lines = [
        "# .envcheck.yaml — generated by `envcheck init`",
        "# Edit environment names and paths to match your project.",
        "",
        "environments:",
    ]
    for env_name, paths in environments.items():
        lines.append(f"  {env_name}:")
        if paths:
            lines.append("    paths:")
            for path in paths:
                lines.append(f"      - {path}")
        else:
            lines.append("    paths: []")
    lines += [
        "",
        "# Optional: variable name patterns to ignore during comparison",
        "# ignore:",
        "#   - BUILD_ID",
        "#",
        "# Optional: docker-compose services to track for version drift",
        "# services:",
        "#   postgres:",
        "#     version_field: image",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_discovery_table(environments: Dict[str, List[str]]) -> None:
    """Print a Rich table of the discovered environment mapping."""
    table = Table(title="Discovered environments")
    table.add_column("Environment", style="cyan", no_wrap=True)
    table.add_column("Files", style="white")
    for env_name, paths in environments.items():
        table.add_row(env_name, ", ".join(paths) if paths else "(none)")
    Console().print(table)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def bootstrap(
    root: Path,
    out: Path,
    *,
    yes: bool = False,
    force: bool = False,
) -> Dict[str, List[str]]:
    """Run the ``init`` flow: discover → prompt → write.

    Parameters
    ----------
    root:
        Project root to scan.
    out:
        Output path for the generated config file.
    yes:
        Accept all discovered environments without prompting.
    force:
        Overwrite *out* if it already exists.

    Returns
    -------
    dict[str, list[str]]
        The environment mapping that was written to *out*.

    Raises
    ------
    FileExistsError
        *out* exists and *force* is False.
    NotADirectoryError
        *root* is not a directory.
    ValueError
        The user-supplied environment names are invalid, or the rendered
        config fails validation.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"Project root not found: {root}")
    if out.exists() and not force:
        raise FileExistsError(
            f"{out} already exists — use --force to overwrite it"
        )

    console = Console()
    proposed = group_environments(discover_files(root), root)

    if proposed:
        console.print("[bold]Discovered environments:[/bold]")
        print_discovery_table(proposed)
    else:
        console.print(
            "[yellow]No environment files found — creating a config with "
            f"the {DEFAULT_ENV!r} environment (edit paths manually).[/yellow]"
        )

    if yes:
        chosen = proposed or {DEFAULT_ENV: []}
    else:
        default_names = ",".join(proposed.keys()) if proposed else DEFAULT_ENV
        try:
            raw = typer.prompt(
                "Environment names (comma-separated)", default=default_names
            )
        except (typer.Abort, EOFError, KeyboardInterrupt):
            raw = default_names
        names = parse_env_names(raw)
        chosen = apply_env_names(proposed, names)

        dropped = [name for name in proposed if name not in chosen]
        if dropped:
            console.print(
                f"[dim]Skipped environments (not in your list): {', '.join(dropped)}[/dim]"
            )

    content = render_config(chosen)

    # Validate before writing — never emit a config that fails to load.
    EnvcheckConfig(**yaml.safe_load(content))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    return chosen
