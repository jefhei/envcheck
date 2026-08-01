"""Environment profile builder — aggregate scanner results into a unified view.

The ``EnvironmentProfile`` model combines results from all three scanner
types (.env files, Docker configs, CI workflows) into a single structured
profile for a named environment (dev, staging, prod, etc.).

The ``build_profile`` function is the main entrypoint: it takes a loaded
:class:`EnvcheckConfig` and an environment name, resolves the configured
file paths, runs the appropriate scanners, and returns a populated
:class:`EnvironmentProfile`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from envcheck.config import EnvcheckConfig
from envcheck.scanners.ci import (
    CiScanResult,
    CiSecretEntry,
    CiVarEntry,
    scan_ci_workflows,
)
from envcheck.scanners.docker import (
    DockerScanResult,
    DockerVarEntry,
    scan_docker_compose_files,
    scan_dockerfiles,
)
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry, scan_env_files

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class EnvironmentProfile(BaseModel):
    """Aggregated environment configuration for a single named environment.

    Combines results from ``.env`` files, Docker configs, and CI configs
    into a unified view of what environment variables, services, and
    secrets are expected in a given environment (dev, staging, prod, etc.).
    """

    name: str = Field(description="Environment name (e.g. dev, staging, prod)")

    # --- Raw scanner results ---
    env_file_results: List[EnvFileScanResult] = Field(
        default_factory=list,
        description="Results from scanning .env-format files",
    )
    docker_result: Optional[DockerScanResult] = Field(
        default=None,
        description="Combined results from Docker config files (compose + Dockerfiles)",
    )
    ci_result: Optional[CiScanResult] = Field(
        default=None,
        description="Combined results from CI workflow files",
    )

    # ------------------------------------------------------------------
    # Computed aggregate properties
    # ------------------------------------------------------------------

    @property
    def env_vars(self) -> Dict[str, str]:
        """All environment variables merged from every source (flat key → value).

        Merge order: ``.env`` files → Docker config → CI config.
        Later sources override earlier ones for duplicate keys.
        """
        merged: Dict[str, str] = {}
        for result in self.env_file_results:
            for key, entry in result.variables.items():
                merged[key] = entry.value
        if self.docker_result is not None:
            for key, entry in self.docker_result.all_variables.items():
                merged[key] = entry.value
        if self.ci_result is not None:
            for key, entry in self.ci_result.all_variables.items():
                merged[key] = entry.value
        return merged

    @property
    def env_var_details(self) -> Dict[str, EnvVarEntry | DockerVarEntry | CiVarEntry]:
        """All variables with full metadata (key → entry from whichever source defined it).

        Merge order follows :attr:`env_vars` precedence: env-file entries
        are shadowed by Docker entries, which are shadowed by CI entries.
        """
        merged: Dict[str, EnvVarEntry | DockerVarEntry | CiVarEntry] = {}
        for result in self.env_file_results:
            for key, entry in result.variables.items():
                merged[key] = entry
        if self.docker_result is not None:
            for key, entry in self.docker_result.all_variables.items():
                merged[key] = entry
        if self.ci_result is not None:
            for key, entry in self.ci_result.all_variables.items():
                merged[key] = entry
        return merged

    @property
    def docker_services(self) -> Dict[str, str]:
        """Service name → image tag from docker-compose files."""
        if self.docker_result is not None:
            return self.docker_result.all_images
        return {}

    @property
    def ci_secrets(self) -> Dict[str, CiSecretEntry]:
        """All CI secrets referenced across workflow files."""
        if self.ci_result is not None:
            return self.ci_result.all_secrets
        return {}

    @property
    def ci_variables(self) -> Dict[str, CiVarEntry]:
        """All CI variables defined across workflow files."""
        if self.ci_result is not None:
            return self.ci_result.all_variables
        return {}

    @property
    def scanned_files(self) -> List[str]:
        """All file paths that were successfully scanned, deduplicated and sorted."""
        files: List[str] = []
        for result in self.env_file_results:
            files.append(result.source)
        if self.docker_result is not None:
            for cr in self.docker_result.compose_results:
                files.append(cr.source)
            for dr in self.docker_result.dockerfile_results:
                files.append(dr.source)
        if self.ci_result is not None:
            for wr in self.ci_result.workflow_results:
                files.append(wr.source)
        return sorted(set(files))

    @property
    def total_env_vars(self) -> int:
        """Number of unique environment variables across all sources."""
        return len(self.env_vars)

    @property
    def total_docker_services(self) -> int:
        """Number of services defined in docker-compose files."""
        return len(self.docker_services)

    @property
    def total_ci_secrets(self) -> int:
        """Number of unique CI secrets referenced."""
        return len(self.ci_secrets)

    @property
    def total_scanned_files(self) -> int:
        """Number of unique files scanned."""
        return len(self.scanned_files)


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

#: File-name heuristics used by :func:`_classify_path` to decide which
#: scanner should handle a given file path.
_ENV_FILE_PREFIXES = (".env",)
_COMPOSE_FILENAMES = frozenset({
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
})
_DOCKERFILE_FILENAMES = frozenset({"dockerfile", "containerfile", "dockerfile.dockerignore"})
_CI_DIR_MARKERS = (".github/workflows", ".gitlab-ci.yml")


def _classify_path(path: Path) -> str:
    """Determine which scanner should handle *path* based on its name.

    Returns one of ``"env"``, ``"compose"``, ``"dockerfile"``, ``"ci"``,
    or ``"unknown"``.
    """
    name = path.name.lower()
    parent_str = str(path.parent).lower()

    # .env* files
    if any(name.startswith(prefix) for prefix in _ENV_FILE_PREFIXES):
        return "env"

    # docker-compose files (exact names plus per-environment variants
    # like docker-compose.dev.yml / compose.staging.yaml)
    if name in _COMPOSE_FILENAMES or (
        (name.startswith("docker-compose.") or name.startswith("compose."))
        and name.endswith((".yml", ".yaml"))
    ):
        return "compose"

    # Dockerfiles
    if name in _DOCKERFILE_FILENAMES or name.endswith(".dockerfile"):
        return "dockerfile"

    # CI workflow files (YAML under .github/workflows/)
    if ".github/workflows" in parent_str and name.endswith((".yml", ".yaml")):
        return "ci"

    # CI config at expected paths
    if name == ".gitlab-ci.yml" or parent_str.endswith(".gitlab-ci.yml"):
        return "ci"

    # Generic .yml/.yaml files that didn't match compose
    if name.endswith((".yml", ".yaml")):
        return "ci"

    return "unknown"


def build_profile(
    config: EnvcheckConfig,
    env_name: str,
    root: Path | None = None,
) -> EnvironmentProfile:
    """Build an :class:`EnvironmentProfile` for a named environment.

    Parameters
    ----------
    config:
        Loaded ``.envcheck.yaml`` configuration.
    env_name:
        Name of the environment to build a profile for (must exist in
        ``config.environments``).
    root:
        Root directory for resolving relative file paths.  Defaults to
        the current working directory at call time.

    Returns
    -------
    EnvironmentProfile
        The aggregated profile for the requested environment.

    Raises
    ------
    KeyError
        *env_name* is not defined in ``config.environments``.
    """
    if env_name not in config.environments:
        raise KeyError(
            f"Environment {env_name!r} not found in config. "
            f"Available: {list(config.environments.keys())}"
        )

    env_config = config.environments[env_name]
    base = root.resolve() if root else Path.cwd().resolve()

    # Classify each configured path by file name
    env_paths: List[Path] = []
    compose_paths: List[Path] = []
    dockerfile_paths: List[Path] = []
    ci_paths: List[Path] = []

    for raw_path in env_config.paths:
        p = base / raw_path if not Path(raw_path).is_absolute() else Path(raw_path)
        resolved = p.resolve()

        kind = _classify_path(resolved)
        if kind == "env":
            env_paths.append(resolved)
        elif kind == "compose":
            compose_paths.append(resolved)
        elif kind == "dockerfile":
            dockerfile_paths.append(resolved)
        elif kind == "ci":
            ci_paths.append(resolved)
        # "unknown" paths are silently skipped

    # Run scanners (each silently skips non-existent files)
    env_results = scan_env_files(env_paths)
    compose_results = scan_docker_compose_files(compose_paths)
    dockerfile_results = scan_dockerfiles(dockerfile_paths)
    ci_results = scan_ci_workflows(ci_paths)

    # Wrap Docker results if there are any
    docker_result: Optional[DockerScanResult] = None
    if compose_results or dockerfile_results:
        docker_result = DockerScanResult(
            compose_results=compose_results,
            dockerfile_results=dockerfile_results,
        )

    # Wrap CI results if there are any
    ci_result: Optional[CiScanResult] = None
    if ci_results:
        ci_result = CiScanResult(workflow_results=ci_results)

    return EnvironmentProfile(
        name=env_name,
        env_file_results=env_results,
        docker_result=docker_result,
        ci_result=ci_result,
    )
