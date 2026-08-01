"""Tests for the environment profile builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from envcheck.config import EnvironmentConfig, EnvcheckConfig
from envcheck.profile import (
    EnvironmentProfile,
    _classify_path,
    build_profile,
)
from envcheck.scanners.ci import CiScanResult, CiVarEntry
from envcheck.scanners.docker import (
    DockerComposeScanResult,
    DockerScanResult,
    DockerVarEntry,
    DockerfileScanResult,
)
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry


# ===========================================================================
#  _classify_path
# ===========================================================================


class TestClassifyPath:
    """Verify that :func:`_classify_path` correctly identifies file types."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            (".env", "env"),
            (".env.dev", "env"),
            (".env.production", "env"),
            (".env.example", "env"),
            (".env.local", "env"),
            ("docker-compose.yml", "compose"),
            ("docker-compose.yaml", "compose"),
            ("compose.yml", "compose"),
            ("compose.yaml", "compose"),
            ("docker-compose.dev.yml", "compose"),
            ("docker-compose.staging.yml", "compose"),
            ("docker-compose.prod.yaml", "compose"),
            ("compose.dev.yml", "compose"),
            ("compose.staging.yaml", "compose"),
            ("Dockerfile", "dockerfile"),
            ("Containerfile", "dockerfile"),
            ("web.Dockerfile", "dockerfile"),
            (".gitlab-ci.yml", "ci"),
            ("deploy.yml", "ci"),
            ("release.yaml", "ci"),
        ],
    )
    def test_classify(self, filename: str, expected: str) -> None:
        # For filenames that live in .github/workflows/ we need a specific
        # parent directory, so test those via the `root` fixture context.
        if filename.endswith((".yml", ".yaml")) and not filename.startswith(".gitlab"):
            # Generic .yml/.yaml → "ci"
            path = Path("/project") / filename
        else:
            path = Path("/project") / filename
        assert _classify_path(path) == expected

    def test_classify_ci_under_github_workflows(self) -> None:
        path = Path("/project/.github/workflows/ci.yml")
        assert _classify_path(path) == "ci"

    def test_classify_unknown(self) -> None:
        path = Path("/project/readme.md")
        assert _classify_path(path) == "unknown"

    def test_classify_pyproject(self) -> None:
        path = Path("/project/pyproject.toml")
        assert _classify_path(path) == "unknown"


# ===========================================================================
#  EnvironmentProfile model
# ===========================================================================


class TestEnvironmentProfile:
    """Verify the :class:`EnvironmentProfile` computed properties."""

    def test_empty_profile(self) -> None:
        profile = EnvironmentProfile(name="dev")
        assert profile.name == "dev"
        assert profile.env_vars == {}
        assert profile.env_var_details == {}
        assert profile.docker_services == {}
        assert profile.ci_secrets == {}
        assert profile.ci_variables == {}
        assert profile.scanned_files == []
        assert profile.total_env_vars == 0
        assert profile.total_docker_services == 0
        assert profile.total_ci_secrets == 0
        assert profile.total_scanned_files == 0

    def test_env_vars_aggregated(self) -> None:
        """Env-file vars should appear in the merged view."""
        e1 = EnvVarEntry(key="DB_URL", value="localhost", source_file="/p/.env", line_number=1)
        e2 = EnvVarEntry(key="PORT", value="8080", source_file="/p/.env", line_number=2)

        profile = EnvironmentProfile(
            name="dev",
            env_file_results=[
                EnvFileScanResult(
                    source="/p/.env",
                    variables={"DB_URL": e1, "PORT": e2},
                    total_lines=2,
                    parsed_lines=2,
                ),
            ],
        )

        assert profile.env_vars == {"DB_URL": "localhost", "PORT": "8080"}
        assert profile.total_env_vars == 2
        assert profile.total_scanned_files == 1
        assert profile.scanned_files == ["/p/.env"]

    def test_docker_services_property(self) -> None:
        profile = EnvironmentProfile(
            name="staging",
            docker_result=DockerScanResult(
                compose_results=[
                    DockerComposeScanResult(
                        source="/p/docker-compose.yml",
                        variables={},
                        services={"postgres": "postgres:16", "redis": "redis:7"},
                        env_files={},
                        total_services=2,
                    ),
                ],
            ),
        )
        assert profile.docker_services == {"postgres": "postgres:16", "redis": "redis:7"}
        assert profile.total_docker_services == 2

    def test_scanned_files_dedup(self) -> None:
        e1 = EnvVarEntry(key="A", value="1", source_file="/p/.env", line_number=1)
        e2 = EnvVarEntry(key="B", value="2", source_file="/p/.env", line_number=2)

        profile = EnvironmentProfile(
            name="dev",
            env_file_results=[
                EnvFileScanResult(source="/p/.env", variables={"A": e1, "B": e2}, total_lines=2, parsed_lines=2),
            ],
            docker_result=DockerScanResult(
                compose_results=[
                    DockerComposeScanResult(
                        source="/p/docker-compose.yml",
                        variables={},
                        services={},
                        env_files={},
                        total_services=0,
                    ),
                ],
            ),
        )
        files = profile.scanned_files
        assert len(files) == 2
        assert "/p/.env" in files
        assert "/p/docker-compose.yml" in files


# ===========================================================================
#  build_profile
# ===========================================================================


class TestBuildProfile:
    """End-to-end tests for :func:`build_profile` with temporary fixtures."""

    def test_unknown_environment_raises(self, tmp_path: Path) -> None:
        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=[".env"])},
        )
        with pytest.raises(KeyError, match="staging"):
            build_profile(config, "staging", root=tmp_path)

    def test_no_files_gives_empty_profile(self, tmp_path: Path) -> None:
        """A config pointing to non-existent files should yield an empty profile."""
        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=[".env", "docker-compose.yml"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)
        assert profile.name == "dev"
        assert profile.env_vars == {}
        assert profile.docker_services == {}
        assert profile.scanned_files == []
        assert profile.total_scanned_files == 0

    def test_build_with_env_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("DATABASE_URL=postgres://localhost:5432/mydb\nPORT=3000\n", encoding="utf-8")

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=[".env"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)

        assert profile.name == "dev"
        assert profile.env_vars["DATABASE_URL"] == "postgres://localhost:5432/mydb"
        assert profile.env_vars["PORT"] == "3000"
        assert profile.total_env_vars == 2
        assert len(profile.scanned_files) == 1
        assert str(env_file.resolve()) in profile.scanned_files

    def test_build_with_docker_compose(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx:1.25\n"
            "    environment:\n"
            "      - NGINX_HOST=localhost\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    environment:\n"
            "      POSTGRES_PASSWORD: secret\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=["docker-compose.yml"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)

        assert profile.name == "dev"
        assert profile.docker_services == {"db": "postgres:16", "web": "nginx:1.25"}
        assert profile.env_vars["NGINX_HOST"] == "localhost"
        assert profile.env_vars["POSTGRES_PASSWORD"] == "secret"
        assert profile.total_docker_services == 2
        assert profile.total_env_vars == 2

    def test_build_with_dockerfile(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text(
            "FROM python:3.13-slim\n"
            "ENV PYTHONDONTWRITEBYTECODE=1\n"
            "ENV PYTHONUNBUFFERED=1\n"
            "ARG APP_HOME=/app\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=["Dockerfile"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)

        assert profile.name == "dev"
        assert profile.env_vars["PYTHONDONTWRITEBYTECODE"] == "1"
        assert profile.env_vars["PYTHONUNBUFFERED"] == "1"
        # ARGs are tracked separately, not in env_vars
        assert profile.total_env_vars == 2

    def test_build_with_ci_workflow(self, tmp_path: Path) -> None:
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        ci_file = workflows_dir / "ci.yml"
        ci_file.write_text(
            "name: CI\n"
            "on: [push]\n"
            "env:\n"
            "  NODE_VERSION: 20\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    env:\n"
            "      DB_URL: ${{ secrets.DB_URL }}\n"
            "    steps:\n"
            "      - name: Checkout\n"
            "        run: echo hello\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=[".github/workflows/ci.yml"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)

        assert profile.name == "dev"
        assert profile.env_vars["NODE_VERSION"] == "20"
        assert profile.env_vars["DB_URL"] == "${{ secrets.DB_URL }}"
        assert profile.total_env_vars == 2
        assert profile.total_ci_secrets >= 1
        assert "DB_URL" in profile.ci_secrets

    def test_build_with_mixed_sources(self, tmp_path: Path) -> None:
        """Profile correctly merges vars from all scanner types."""
        # .env file
        (tmp_path / ".env").write_text("COMMON_VAR=from_env\nONLY_ENV=only_in_env\n", encoding="utf-8")

        # docker-compose.yml
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx:1.25\n"
            "    environment:\n"
            "      - COMMON_VAR=from_docker\n"
            "      - ONLY_DOCKER=only_in_docker\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(
                paths=[".env", "docker-compose.yml"],
            )},
        )
        profile = build_profile(config, "dev", root=tmp_path)

        # Docker overrides .env for COMMON_VAR
        assert profile.env_vars["COMMON_VAR"] == "from_docker"
        assert profile.env_vars["ONLY_ENV"] == "only_in_env"
        assert profile.env_vars["ONLY_DOCKER"] == "only_in_docker"
        assert profile.total_env_vars == 3
        assert profile.total_docker_services == 1

    def test_build_ignores_unknown_file_types(self, tmp_path: Path) -> None:
        """Files that don't match any scanner pattern are silently skipped."""
        readme = tmp_path / "README.md"
        readme.write_text("# Project", encoding="utf-8")

        config = EnvcheckConfig(
            environments={"dev": EnvironmentConfig(paths=["README.md"])},
        )
        profile = build_profile(config, "dev", root=tmp_path)
        assert profile.env_vars == {}
        assert profile.total_scanned_files == 0
