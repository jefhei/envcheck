"""Tests for M3.1 — `envcheck init` (auto-discovery + config bootstrap).

Covers the file-discovery heuristics, environment grouping, config
rendering, and the real CLI command in both ``--yes`` (non-interactive)
and interactive (stdin) modes, plus error handling (existing config,
invalid names, missing root).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from envcheck.config import EnvcheckConfig, load_config
from envcheck.exit_codes import ExitCode
from envcheck.init import (
    DEFAULT_ENV,
    apply_env_names,
    bootstrap,
    discover_files,
    group_environments,
    parse_env_names,
    render_config,
    validate_env_name,
)
from envcheck.main import app
from envcheck.profile import build_profile

runner = CliRunner()


# ===========================================================================
#  Fixture helpers
# ===========================================================================


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a fixture project directory with the given files."""
    proj = tmp_path / "proj"
    proj.mkdir()
    for name, content in files.items():
        (proj / name).write_text(content, encoding="utf-8")
    return proj


def _invoke(*args: str, input: str | None = None) -> Result:
    return runner.invoke(app, list(args), input=input)


# ===========================================================================
#  Unit tests — discover_files
# ===========================================================================


class TestDiscoverFiles:
    def test_env_files_only(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                ".env.dev": "B=2\n",
                ".env.staging": "C=3\n",
                ".env.prod": "D=4\n",
                ".env.example": "E=5\n",
            },
        )
        result = discover_files(proj)
        names = [p.name for p in result["env"]]
        assert names == [".env", ".env.dev", ".env.example", ".env.prod", ".env.staging"]

    def test_env_pattern_excludes_non_env_files(self, tmp_path: Path) -> None:
        # .envrc (direnv), .envcheck.yaml (our own config), README.md
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                ".envrc": "export X=1\n",
                ".envcheck.yaml": "environments: {}\n",
                "README.md": "# hi\n",
            },
        )
        result = discover_files(proj)
        assert [p.name for p in result["env"]] == [".env"]

    def test_compose_variants(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                "docker-compose.yml": "services: {}\n",
                "docker-compose.yaml": "services: {}\n",
                "compose.yml": "services: {}\n",
                "compose.yaml": "services: {}\n",
                "docker-compose.staging.yml": "services: {}\n",
                "compose.prod.yaml": "services: {}\n",
            },
        )
        result = discover_files(proj)
        names = [p.name for p in result["compose"]]
        assert names == [
            "compose.prod.yaml",
            "compose.yaml",
            "compose.yml",
            "docker-compose.staging.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        ]

    def test_dockerfiles(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                "Dockerfile": "FROM python:3.13\n",
                "Containerfile": "FROM python:3.13\n",
                "Dockerfile.dev": "FROM python:3.13\n",
            },
        )
        result = discover_files(proj)
        names = [p.name for p in result["dockerfile"]]
        assert names == ["Containerfile", "Dockerfile", "Dockerfile.dev"]

    def test_empty_root(self, tmp_path: Path) -> None:
        assert discover_files(tmp_path) == {"env": [], "compose": [], "dockerfile": []}

    def test_mixed_project(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                ".env.staging": "B=2\n",
                "docker-compose.yml": "services: {}\n",
                "Dockerfile": "FROM python:3.13\n",
                "README.md": "not an env file\n",
                "pyproject.toml": "[project]\n",
            },
        )
        result = discover_files(proj)
        assert [p.name for p in result["env"]] == [".env", ".env.staging"]
        assert [p.name for p in result["compose"]] == ["docker-compose.yml"]
        assert [p.name for p in result["dockerfile"]] == ["Dockerfile"]


# ===========================================================================
#  Unit tests — group_environments
# ===========================================================================


class TestGroupEnvironments:
    def test_base_files_go_to_default_env(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                "docker-compose.yml": "services: {}\n",
                "Dockerfile": "FROM python:3.13\n",
            },
        )
        grouped = group_environments(discover_files(proj), proj)
        assert grouped == {
            DEFAULT_ENV: [".env", "Dockerfile", "docker-compose.yml"],
        }

    def test_suffix_files_map_to_named_envs(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env.dev": "B=2\n",
                ".env.staging": "C=3\n",
                ".env.example": "E=5\n",
                "docker-compose.staging.yml": "services: {}\n",
                "Dockerfile.dev": "FROM python:3.13\n",
            },
        )
        grouped = group_environments(discover_files(proj), proj)
        assert grouped["dev"] == [".env.dev", "Dockerfile.dev"]
        assert grouped["staging"] == [".env.staging", "docker-compose.staging.yml"]
        assert grouped["example"] == [".env.example"]

    def test_dev_is_always_first(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                ".env.zzz": "Z=1\n",
                ".env.aaa": "A2=1\n",
            },
        )
        grouped = group_environments(discover_files(proj), proj)
        assert list(grouped.keys()) == [DEFAULT_ENV, "aaa", "zzz"]

    def test_paths_are_relative_to_root(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n"})
        grouped = group_environments(discover_files(proj), proj)
        assert grouped[DEFAULT_ENV] == [".env"]


# ===========================================================================
#  Unit tests — env-name handling
# ===========================================================================


class TestEnvNames:
    def test_validate_env_name_accepts_valid(self) -> None:
        for name in ["dev", "staging", "prod-1", "my.env", "UPPER_env"]:
            assert validate_env_name(name) == name

    @pytest.mark.parametrize("name", ["", "has space", "has!sym", ".hidden", "-dash"])
    def test_validate_env_name_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_env_name(name)

    def test_parse_env_names_splits_and_dedupes(self) -> None:
        assert parse_env_names(" dev , staging,dev ") == ["dev", "staging"]

    def test_parse_env_names_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            parse_env_names("  , , ")

    def test_apply_env_names_base_to_dev(self) -> None:
        proposed = {DEFAULT_ENV: [".env"], "staging": [".env.staging"]}
        result = apply_env_names(proposed, ["dev", "staging"])
        assert result == {"dev": [".env"], "staging": [".env.staging"]}

    def test_apply_env_names_base_to_first_when_no_dev(self) -> None:
        proposed = {DEFAULT_ENV: [".env"], "staging": [".env.staging"]}
        result = apply_env_names(proposed, ["staging", "prod"])
        assert result == {"staging": [".env", ".env.staging"], "prod": []}

    def test_apply_env_names_drops_unselected(self) -> None:
        proposed = {DEFAULT_ENV: [".env"], "staging": [".env.staging"]}
        result = apply_env_names(proposed, ["dev"])
        assert result == {"dev": [".env"]}


# ===========================================================================
#  Unit tests — render_config
# ===========================================================================


class TestRenderConfig:
    def test_renders_valid_yaml_that_loads(self) -> None:
        environments = {DEFAULT_ENV: [".env"], "staging": [".env.staging"]}
        text = render_config(environments)
        data = yaml.safe_load(text)
        assert data == {
            "environments": {
                "dev": {"paths": [".env"]},
                "staging": {"paths": [".env.staging"]},
            }
        }

    def test_rendered_config_validates_through_pydantic(self) -> None:
        text = render_config({DEFAULT_ENV: [".env"]})
        cfg = EnvcheckConfig(**yaml.safe_load(text))
        assert cfg.environments["dev"].paths == [".env"]

    def test_empty_paths_renders(self) -> None:
        text = render_config({"prod": []})
        data = yaml.safe_load(text)
        assert data == {"environments": {"prod": {"paths": []}}}


# ===========================================================================
#  CLI integration tests
# ===========================================================================


class TestCliInitNonInteractive:
    def test_yes_creates_config_with_discovered_envs(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {
                ".env": "A=1\n",
                ".env.dev": "B=2\n",
                ".env.staging": "C=3\n",
                "docker-compose.yml": "services: {}\n",
                "Dockerfile": "FROM python:3.13\n",
            },
        )
        result = _invoke("init", "--root", str(proj), "--yes")
        assert result.exit_code == ExitCode.OK
        cfg_path = proj / ".envcheck.yaml"
        assert cfg_path.is_file()
        cfg = load_config(cfg_path)
        assert set(cfg.environments) == {"dev", "staging"}
        assert ".env" in cfg.environments["dev"].paths
        assert ".env.staging" in cfg.environments["staging"].paths

    def test_yes_with_no_files_creates_dev_skeleton(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"README.md": "hi\n"})
        result = _invoke("init", "--root", str(proj), "--yes")
        assert result.exit_code == ExitCode.OK
        cfg = load_config(proj / ".envcheck.yaml")
        assert set(cfg.environments) == {"dev"}
        assert cfg.environments["dev"].paths == []

    def test_existing_config_without_force_fails(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {".env": "A=1\n", ".envcheck.yaml": "environments:\n  dev:\n    paths: []\n"},
        )
        result = _invoke("init", "--root", str(proj), "--yes")
        assert result.exit_code == ExitCode.ERROR
        assert "already exists" in result.stderr
        assert "--force" in result.stderr
        # Config must be untouched
        assert load_config(proj / ".envcheck.yaml").environments["dev"].paths == []

    def test_force_overwrites_existing_config(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {".env": "A=1\n", ".env.staging": "B=2\n", ".envcheck.yaml": "old: true\n"},
        )
        result = _invoke("init", "--root", str(proj), "--yes", "--force")
        assert result.exit_code == ExitCode.OK
        cfg = load_config(proj / ".envcheck.yaml")
        assert set(cfg.environments) == {"dev", "staging"}

    def test_custom_config_path(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n"})
        out = tmp_path / "custom" / "envcheck.yml"
        result = _invoke("init", "--root", str(proj), "--config", str(out), "--yes")
        assert result.exit_code == ExitCode.OK
        assert load_config(out).environments["dev"].paths == [".env"]

    def test_missing_root_fails(self, tmp_path: Path) -> None:
        result = _invoke("init", "--root", str(tmp_path / "nope"), "--yes")
        assert result.exit_code == ExitCode.ERROR
        assert "Error:" in result.stderr

    def test_generated_config_is_usable_by_diff(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {".env": "SHARED=1\n", ".env.staging": "SHARED=1\n"},
        )
        result = _invoke("init", "--root", str(proj), "--yes")
        assert result.exit_code == ExitCode.OK
        # The generated config must load and build profiles successfully
        cfg = load_config(proj / ".envcheck.yaml")
        profile = build_profile(cfg, "dev", root=proj)
        assert profile.env_vars == {"SHARED": "1"}


class TestCliInitInteractive:
    def test_accepts_discovered_defaults_on_enter(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n", ".env.staging": "B=2\n"})
        result = _invoke("init", "--root", str(proj), input="\n")
        assert result.exit_code == ExitCode.OK
        cfg = load_config(proj / ".envcheck.yaml")
        assert set(cfg.environments) == {"dev", "staging"}

    def test_custom_env_names(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n", ".env.staging": "B=2\n"})
        result = _invoke("init", "--root", str(proj), input="dev,prod\n")
        assert result.exit_code == ExitCode.OK
        cfg = load_config(proj / ".envcheck.yaml")
        assert set(cfg.environments) == {"dev", "prod"}
        # staging was dropped from the list
        assert "staging" not in cfg.environments

    def test_invalid_env_name_fails(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n"})
        result = _invoke("init", "--root", str(proj), input="bad name!\n")
        assert result.exit_code == ExitCode.ERROR
        assert "Invalid environment name" in result.stderr
        assert not (proj / ".envcheck.yaml").exists()

    def test_interactive_no_files_prompts_for_names(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"README.md": "hi\n"})
        result = _invoke("init", "--root", str(proj), input="prod,qa\n")
        assert result.exit_code == ExitCode.OK
        cfg = load_config(proj / ".envcheck.yaml")
        assert set(cfg.environments) == {"prod", "qa"}
        assert cfg.environments["prod"].paths == []


class TestBootstrapFunction:
    def test_returns_written_environments(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".env": "A=1\n", ".env.staging": "B=2\n"})
        out = proj / ".envcheck.yaml"
        envs = bootstrap(proj, out, yes=True)
        assert envs == {"dev": [".env"], "staging": [".env.staging"]}
        assert out.is_file()

    def test_refuses_existing_without_force(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {".envcheck.yaml": "environments:\n  dev:\n    paths: []\n"})
        with pytest.raises(FileExistsError):
            bootstrap(proj, proj / ".envcheck.yaml", yes=True)

    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(NotADirectoryError):
            bootstrap(tmp_path / "nope", tmp_path / ".envcheck.yaml", yes=True)
