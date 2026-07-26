"""Tests for the .envcheck.yaml config schema and loader."""

from pathlib import Path

import pytest
import yaml

from envcheck.config import EnvcheckConfig, load_config, EnvironmentConfig, ServiceConfig


# ---------------------------------------------------------------------------
# Unit tests — EnvcheckConfig model
# ---------------------------------------------------------------------------


def test_minimal_config():
    """A config with one env and no extras should build cleanly."""
    cfg = EnvcheckConfig(environments={"dev": {"paths": [".env"]}})
    assert "dev" in cfg.environments
    assert cfg.environments["dev"].paths == [".env"]
    assert cfg.ignore == []
    assert cfg.services == {}


def test_full_config():
    """A config exercising all fields."""
    raw = {
        "environments": {
            "dev": {"paths": [".env", ".env.dev"]},
            "staging": {"paths": [".env.staging"]},
        },
        "ignore": ["BUILD_ID", "CI_*"],
        "services": {
            "postgres": {"version_field": "image"},
            "redis": {"version_field": "image", "path": "docker-compose.yml"},
        },
    }
    cfg = EnvcheckConfig(**raw)
    assert set(cfg.environments) == {"dev", "staging"}
    assert cfg.ignore == ["BUILD_ID", "CI_*"]
    assert cfg.services["postgres"].version_field == "image"
    assert cfg.services["redis"].path == "docker-compose.yml"


def test_empty_paths_allowed():
    """paths may be empty — the scanner will simply do nothing."""
    cfg = EnvcheckConfig(environments={"dev": {"paths": []}})
    assert cfg.environments["dev"].paths == []


def test_service_defaults():
    """ServiceConfig defaults to version_field='image' and path=None."""
    cfg = EnvcheckConfig(
        environments={"dev": {"paths": [".env"]}},
        services={"postgres": {}},
    )
    assert cfg.services["postgres"].version_field == "image"
    assert cfg.services["postgres"].path is None


def test_no_environments_raises():
    """At least one environment is required."""
    with pytest.raises(ValueError, match="At least one environment"):
        EnvcheckConfig(environments={})


def test_unknown_fields_are_ignored():
    """Pydantic v2 by default ignores unknown top-level keys (good for forward compat)."""
    cfg = EnvcheckConfig(
        environments={"dev": {"paths": [".env"]}},
        watcher={"interval": 30},  # not a field — silently ignored
    )
    assert "dev" in cfg.environments
    assert not hasattr(cfg, "watcher")


# ---------------------------------------------------------------------------
# Unit tests — EnvironmentConfig
# ---------------------------------------------------------------------------


class TestEnvironmentConfig:
    def test_default_paths_empty(self):
        env = EnvironmentConfig()
        assert env.paths == []

    def test_paths_from_args(self):
        env = EnvironmentConfig(paths=[".env", "docker-compose.yml"])
        assert env.paths == [".env", "docker-compose.yml"]


# ---------------------------------------------------------------------------
# Unit tests — ServiceConfig
# ---------------------------------------------------------------------------


class TestServiceConfig:
    def test_defaults(self):
        svc = ServiceConfig()
        assert svc.version_field == "image"
        assert svc.path is None

    def test_custom(self):
        svc = ServiceConfig(version_field="tag", path="docker-compose.yml")
        assert svc.version_field == "tag"
        assert svc.path == "docker-compose.yml"


# ---------------------------------------------------------------------------
# Integration tests — load_config
# ---------------------------------------------------------------------------


def test_load_config_from_path(tmp_path: Path):
    """load_config reads and validates a yaml file at an explicit path."""
    config_file = tmp_path / ".envcheck.yaml"
    config_file.write_text(
        yaml.dump({"environments": {"dev": {"paths": [".env"]}}})
    )
    cfg = load_config(config_file)
    assert isinstance(cfg, EnvcheckConfig)
    assert cfg.environments["dev"].paths == [".env"]


def test_load_config_not_found():
    """FileNotFoundError when the config doesn't exist."""
    with pytest.raises(FileNotFoundError, match="not found"):
        load_config(Path("/nonexistent/path/.envcheck.yaml"))


def test_load_config_invalid_yaml(tmp_path: Path):
    """YAML parse errors propagate."""
    config_file = tmp_path / ".envcheck.yaml"
    config_file.write_text("[[invalid yaml::")
    with pytest.raises(yaml.YAMLError):
        load_config(config_file)


def test_load_config_not_a_mapping(tmp_path: Path):
    """A scalar/sequence yaml file raises ValueError."""
    config_file = tmp_path / ".envcheck.yaml"
    config_file.write_text("just a string")
    with pytest.raises(ValueError, match="expected a top-level mapping"):
        load_config(config_file)


def test_load_config_invalid_schema(tmp_path: Path):
    """Pydantic validation errors bubble up."""
    config_file = tmp_path / ".envcheck.yaml"
    config_file.write_text(
        yaml.dump({"environments": {}, "services": "not-a-dict"})
    )
    # environments empty → ValueError; also services is wrong type → also fails
    with pytest.raises(ValueError):
        load_config(config_file)


def test_load_config_auto_discover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When no path is given, load_config walks up from cwd."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    config_file = tmp_path / ".envcheck.yaml"
    config_file.write_text(
        yaml.dump({"environments": {"prod": {"paths": [".env.prod"]}}})
    )

    monkeypatch.chdir(nested)
    cfg = load_config()
    assert isinstance(cfg, EnvcheckConfig)
    assert "prod" in cfg.environments


def test_load_config_auto_discover_not_found(tmp_path: Path, monkeypatch):
    """FileNotFoundError when no .envcheck.yaml exists in any parent dir."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="not found"):
        load_config()
