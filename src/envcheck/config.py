"""Configuration model and loader for .envcheck.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class EnvironmentConfig(BaseModel):
    """Scan configuration for a single environment."""

    paths: List[str] = Field(
        default_factory=list,
        description="Files or glob patterns to scan for this environment",
    )


class ServiceConfig(BaseModel):
    """Configuration for a tracked service (e.g. postgres, redis)."""

    version_field: str = Field(
        default="image",
        description="Docker-compose field that contains the version tag",
    )
    path: Optional[str] = Field(
        default=None,
        description="Specific compose-file path to restrict this service config to",
    )


class EnvcheckConfig(BaseModel):
    """Root configuration model for .envcheck.yaml."""

    environments: Dict[str, EnvironmentConfig] = Field(
        default_factory=dict,
        description="Map of environment names to their scan configurations",
    )
    ignore: List[str] = Field(
        default_factory=list,
        description="List of env-var name patterns (exact or glob) to ignore during comparison",
    )
    services: Dict[str, ServiceConfig] = Field(
        default_factory=dict,
        description="Map of service names to their version-tracking configuration",
    )

    @model_validator(mode="after")
    def _require_at_least_one_environment(self) -> EnvcheckConfig:
        if not self.environments:
            raise ValueError(
                "At least one environment must be defined under 'environments'"
            )
        return self


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[Path] = None) -> EnvcheckConfig:
    """Load and validate an ``.envcheck.yaml`` file.

    Parameters
    ----------
    config_path:
        Explicit path to the config file.  When *None* the loader walks up
        from the current working directory looking for a file named
        ``.envcheck.yaml``.

    Returns
    -------
    EnvcheckConfig
        Validated configuration object.

    Raises
    ------
    FileNotFoundError
        No ``.envcheck.yaml`` could be found.
    ValueError
        The file exists but is not valid YAML, is not a mapping, or fails
        Pydantic validation.
    yaml.YAMLError
        YAML parse error.
    """
    if config_path is None:
        config_path = _find_config()

    if config_path is None or not config_path.exists():
        raise FileNotFoundError(
            ".envcheck.yaml not found — run 'envcheck init' to create one"
        )

    raw_text = config_path.read_text(encoding="utf-8")
    raw: object = yaml.safe_load(raw_text)

    if not isinstance(raw, dict):
        raise ValueError(
            f"Invalid .envcheck.yaml: expected a top-level mapping, got {type(raw).__name__}"
        )

    return EnvcheckConfig(**raw)


def _find_config() -> Path | None:
    """Walk up from *cwd* looking for an ``.envcheck.yaml``."""
    current = Path.cwd().resolve()
    for parent in [current] + list(current.parents):
        candidate = parent / ".envcheck.yaml"
        if candidate.is_file():
            return candidate
    return None
