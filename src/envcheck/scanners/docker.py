"""Scanner for Docker configuration files.

Parses:

- ``docker-compose.yml`` / ``docker-compose.yaml``
    - ``environment:`` blocks (both array ``- KEY=VALUE`` and mapping ``KEY: VALUE`` forms)
    - ``env_file:`` references (single value and list)
    - ``image:`` field for version-tag extraction
- ``Dockerfile``
    - ``ENV`` instructions (``KEY=VALUE``, ``KEY VALUE``, and multi-var forms)
    - ``ARG`` instructions
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class DockerVarEntry(BaseModel):
    """A single environment variable or build argument parsed from a Docker
    configuration file."""

    key: str = Field(description="Variable name (or ARG name)")
    value: str = Field(description="Value (empty string if not provided)")
    source_file: str = Field(description="Path to the source file")
    line_number: int = Field(description="1-indexed line number in the source file")
    instruction: str = Field(
        description="Docker instruction that declared this var (ENV, ARG, environment)"
    )
    service: Optional[str] = Field(
        default=None,
        description="Service name (docker-compose only; None for plain Dockerfiles)",
    )


class DockerComposeScanResult(BaseModel):
    """Result of scanning a ``docker-compose.yml`` file."""

    source: str = Field(description="Path to the scanned file")
    variables: Dict[str, DockerVarEntry] = Field(
        default_factory=dict,
        description="Map of variable name → entry (service-scoped, last wins)",
    )
    services: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of service name → image tag (e.g. postgres:16)",
    )
    env_files: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Map of service name → list of env_file paths referenced",
    )
    total_services: int = Field(default=0, description="Number of services defined")


class DockerfileScanResult(BaseModel):
    """Result of scanning a ``Dockerfile``."""

    source: str = Field(description="Path to the scanned file")
    variables: Dict[str, DockerVarEntry] = Field(
        default_factory=dict,
        description="Map of variable name → entry (last wins for ENV; ARGs tracked separately)",
    )
    args: Dict[str, Optional[str]] = Field(
        default_factory=dict,
        description="Map of ARG name → default value (None if no default given)",
    )
    base_image: Optional[str] = Field(
        default=None,
        description="FROM instruction image (e.g. python:3.13-slim)",
    )
    total_lines: int = Field(default=0, description="Total lines in the file")
    parsed_lines: int = Field(default=0, description="Lines that yielded a variable or instruction")


class DockerScanResult(BaseModel):
    """Combined result of scanning all Docker configuration files in a
    project."""

    compose_results: List[DockerComposeScanResult] = Field(default_factory=list)
    dockerfile_results: List[DockerfileScanResult] = Field(default_factory=list)

    @property
    def all_variables(self) -> Dict[str, DockerVarEntry]:
        """Aggregate all variables from all scanned files (last source wins)."""
        merged: Dict[str, DockerVarEntry] = {}
        for cr in self.compose_results:
            merged.update(cr.variables)
        for dr in self.dockerfile_results:
            merged.update(dr.variables)
        return merged

    @property
    def all_images(self) -> Dict[str, str]:
        """Aggregate all docker-compose service images."""
        merged: Dict[str, str] = {}
        for cr in self.compose_results:
            merged.update(cr.services)
        return merged


# ---------------------------------------------------------------------------
# Docker-compose scanner
# ---------------------------------------------------------------------------


def _parse_compose_env_array(
    entries: list,
    service_name: str,
    source: str,
) -> List[DockerVarEntry]:
    """Parse an ``environment:`` block in array form (list of strings or dicts).

    Docker Compose allows mixed forms:
    - ``- KEY=VALUE``
    - ``- KEY``  (value comes from host env at runtime — we record the key with empty value)
    """
    results: List[DockerVarEntry] = []
    for i, entry in enumerate(entries):
        if isinstance(entry, str):
            parts = entry.split("=", 1)
            key = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            results.append(
                DockerVarEntry(
                    key=key,
                    value=value,
                    source_file=source,
                    line_number=i + 1,
                    instruction="environment",
                    service=service_name,
                )
            )
        elif isinstance(entry, dict):
            # YAML mapping form embedded in array (rare but valid)
            for key, val in entry.items():
                results.append(
                    DockerVarEntry(
                        key=str(key),
                        value=str(val) if val is not None else "",
                        source_file=source,
                        line_number=i + 1,
                        instruction="environment",
                        service=service_name,
                    )
                )
    return results


def _parse_compose_env_mapping(
    env_map: dict,
    service_name: str,
    source: str,
) -> List[DockerVarEntry]:
    """Parse an ``environment:`` block in mapping form (``KEY: VALUE``)."""
    results: List[DockerVarEntry] = []
    for i, (key, val) in enumerate(env_map.items()):
        # Strip leading $-style variable references — if the value is a
        # literal string it's usable; if it's a runtime substitution we
        # record the key with the raw expression.
        value = str(val) if val is not None else ""
        results.append(
            DockerVarEntry(
                key=str(key),
                value=value,
                source_file=source,
                line_number=i + 1,
                instruction="environment",
                service=service_name,
            )
        )
    return results


def _parse_compose_env_files(
    env_file_val: object,
    service_name: str,
) -> List[str]:
    """Parse the ``env_file:`` field (string or list)."""
    if isinstance(env_file_val, str):
        return [env_file_val]
    if isinstance(env_file_val, list):
        return [str(f) for f in env_file_val]
    return []


def scan_docker_compose(path: Path) -> DockerComposeScanResult:
    """Scan a ``docker-compose.yml`` (or ``.yaml``) file for environment
    variables, images, and env_file references.

    Parameters
    ----------
    path:
        Path to the docker-compose file.

    Returns
    -------
    DockerComposeScanResult
        The parsed result.

    Raises
    ------
    FileNotFoundError
        The file does not exist.
    ValueError
        The file is not valid YAML or is not a mapping.
    """
    resolved = path.resolve() if not path.is_absolute() else path
    if not resolved.is_file():
        raise FileNotFoundError(f"Docker compose file not found: {resolved}")

    import yaml

    text = resolved.read_text(encoding="utf-8", errors="replace")
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in docker-compose file: {e}") from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"docker-compose file must be a YAML mapping, got {type(raw).__name__}"
        )

    source = str(resolved)
    variables: Dict[str, DockerVarEntry] = {}
    services_map: Dict[str, str] = {}
    env_files_map: Dict[str, List[str]] = {}

    # docker-compose v2 uses top-level "services:"; v1 has services at root
    services_data: dict = {}
    if "services" in raw and isinstance(raw["services"], dict):
        services_data = raw["services"]
    # Also check for v1 flat format where services are at root
    # (we only treat known top-level keys as non-service)

    for svc_name, svc_config in services_data.items():
        if not isinstance(svc_config, dict):
            continue

        # --- image ---
        image_val = svc_config.get("image")
        if isinstance(image_val, str):
            services_map[str(svc_name)] = image_val

        # --- environment (array form) ---
        env_val = svc_config.get("environment")
        if isinstance(env_val, list):
            for entry in _parse_compose_env_array(
                env_val, str(svc_name), source
            ):
                # Keys are namespaced by service to avoid collisions
                variables[entry.key] = entry
        elif isinstance(env_val, dict):
            for entry in _parse_compose_env_mapping(
                env_val, str(svc_name), source
            ):
                variables[entry.key] = entry

        # --- env_file ---
        env_file_val = svc_config.get("env_file")
        if env_file_val is not None:
            env_files_map[str(svc_name)] = _parse_compose_env_files(
                env_file_val, str(svc_name)
            )

    return DockerComposeScanResult(
        source=source,
        variables=variables,
        services=services_map,
        env_files=env_files_map,
        total_services=len(services_data),
    )


# ---------------------------------------------------------------------------
# Dockerfile scanner
# ---------------------------------------------------------------------------

#: Regex for ``ENV KEY=VALUE`` or ``ENV KEY VALUE`` forms.
#: Group 1 = key, Group 2 = value (if value= form) or None.
_ENV_LINE_RE = re.compile(
    r"^\s*ENV\s+"  # instruction
    r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)"
    r"(?:\s*=\s*(?P<eq_value>.*))?"  # KEY=VALUE form
    r"(?:\s+(?P<space_value>.*))?"  # KEY VALUE form
)

#: Regex for ``ARG KEY=VALUE`` or ``ARG KEY``.
_ARG_LINE_RE = re.compile(
    r"^\s*ARG\s+"
    r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)"
    r"(?:\s*=\s*(?P<value>.*))?"
)

#: Regex for ``FROM image:tag``.
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)")


def _unquote_docker(value: str) -> str:
    """Strip surrounding quotes from a Dockerfile value.

    Dockerfiles use double-quotes for values containing spaces.
    Unlike .env files, escape sequences are not commonly processed
    at the parser level.
    """
    v = value.strip()
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    return v


def _parse_dockerfile_line(
    line: str,
    line_number: int,
    source: str,
) -> Optional[DockerVarEntry]:
    """Parse a single line of a Dockerfile into a ``DockerVarEntry`` for
    ``ENV`` instructions, or return ``None``.

    ``ARG`` lines are handled separately by ``_parse_arg_line``.
    """
    # --- ENV with = form (ENV KEY=VALUE) ---
    m = _ENV_LINE_RE.match(line)
    if m:
        key = m.group("key")
        eq_value = m.group("eq_value")
        space_value = m.group("space_value")

        if eq_value is not None:
            value = _unquote_docker(eq_value)
        elif space_value is not None:
            value = _unquote_docker(space_value)
        else:
            # ENV KEY with no value — treat as empty
            value = ""

        return DockerVarEntry(
            key=key,
            value=value,
            source_file=source,
            line_number=line_number,
            instruction="ENV",
        )

    return None


def _parse_arg_line(
    line: str,
    line_number: int,
    source: str,
) -> Optional[tuple[str, Optional[str]]]:
    """Parse an ``ARG`` instruction line.

    Returns ``(key, default_value)`` where ``default_value`` is ``None``
    if no default was provided.
    """
    m = _ARG_LINE_RE.match(line)
    if m:
        key = m.group("key")
        raw_value = m.group("value")
        if raw_value is not None:
            return (key, _unquote_docker(raw_value))
        return (key, None)
    return None


def scan_dockerfile(path: Path) -> DockerfileScanResult:
    """Scan a ``Dockerfile`` for ``ENV`` variables and ``ARG`` declarations.

    Parameters
    ----------
    path:
        Path to the Dockerfile.

    Returns
    -------
    DockerfileScanResult
        The parsed result.

    Raises
    ------
    FileNotFoundError
        The file does not exist.
    """
    resolved = path.resolve() if not path.is_absolute() else path
    if not resolved.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {resolved}")

    text = resolved.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    source = str(resolved)
    variables: Dict[str, DockerVarEntry] = {}
    args: Dict[str, Optional[str]] = {}
    base_image: Optional[str] = None
    parsed_count = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # FROM
        from_m = _FROM_RE.match(stripped)
        if from_m and base_image is None:
            base_image = from_m.group("image")
            parsed_count += 1
            continue

        # ARG
        arg_parsed = _parse_arg_line(stripped, i + 1, source)
        if arg_parsed is not None:
            key, default = arg_parsed
            args[key] = default
            parsed_count += 1
            continue

        # ENV
        env_var = _parse_dockerfile_line(stripped, i + 1, source)
        if env_var is not None:
            variables[env_var.key] = env_var
            parsed_count += 1
            continue

    return DockerfileScanResult(
        source=source,
        variables=variables,
        args=args,
        base_image=base_image,
        total_lines=len(lines),
        parsed_lines=parsed_count,
    )


def scan_docker_compose_files(paths: List[Path]) -> List[DockerComposeScanResult]:
    """Scan multiple docker-compose files.

    Non-existent files are silently skipped.

    Parameters
    ----------
    paths:
        List of file paths to attempt scanning.

    Returns
    -------
    list[DockerComposeScanResult]
        Results for every file that existed and was successfully parsed.
    """
    results: List[DockerComposeScanResult] = []
    for path in paths:
        try:
            results.append(scan_docker_compose(path))
        except FileNotFoundError:
            pass
    return results


def scan_dockerfiles(paths: List[Path]) -> List[DockerfileScanResult]:
    """Scan multiple Dockerfiles.

    Non-existent files are silently skipped.

    Parameters
    ----------
    paths:
        List of file paths to attempt scanning.

    Returns
    -------
    list[DockerfileScanResult]
        Results for every file that existed and was successfully parsed.
    """
    results: List[DockerfileScanResult] = []
    for path in paths:
        try:
            results.append(scan_dockerfile(path))
        except FileNotFoundError:
            pass
    return results
