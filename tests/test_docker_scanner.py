"""Tests for the Docker configuration scanner.

Covers:

- ``docker-compose.yml`` with environment in array form, mapping form, env_file, images
- ``Dockerfile`` with ENV, ARG, FROM instructions
- Edge cases: missing files, empty files, quoted values, multi-var ENV lines
"""

from pathlib import Path

import pytest
import yaml

from envcheck.scanners.docker import (
    DockerComposeScanResult,
    DockerfileScanResult,
    DockerScanResult,
    DockerVarEntry,
    scan_docker_compose,
    scan_dockerfile,
    scan_docker_compose_files,
    scan_dockerfiles,
)


# ===================================================================
# Docker-compose scanner tests
# ===================================================================


class TestScanDockerCompose:
    def test_simple_array_environment(self, tmp_path: Path):
        compose = {
            "services": {
                "web": {
                    "image": "nginx:1.25",
                    "environment": [
                        "NODE_ENV=production",
                        "PORT=8080",
                    ],
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.source == str(f.resolve())
        assert result.total_services == 1
        assert result.services["web"] == "nginx:1.25"
        assert result.variables["NODE_ENV"].value == "production"
        assert result.variables["PORT"].value == "8080"
        assert result.variables["NODE_ENV"].instruction == "environment"
        assert result.variables["NODE_ENV"].service == "web"

    def test_mapping_environment(self, tmp_path: Path):
        compose = {
            "services": {
                "api": {
                    "image": "myapp:latest",
                    "environment": {
                        "DB_HOST": "localhost",
                        "DB_PORT": "5432",
                        "DEBUG": "true",
                    },
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.variables["DB_HOST"].value == "localhost"
        assert result.variables["DB_PORT"].value == "5432"
        assert result.variables["DEBUG"].value == "true"
        assert result.total_services == 1

    def test_env_file_references(self, tmp_path: Path):
        compose = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "env_file": ".env",
                },
                "db": {
                    "image": "postgres:16",
                    "env_file": [".env.db", ".env.shared"],
                },
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.env_files["web"] == [".env"]
        assert result.env_files["db"] == [".env.db", ".env.shared"]
        assert result.total_services == 2

    def test_mixed_environment_blocks(self, tmp_path: Path):
        """Services can have both environment and env_file."""
        compose = {
            "services": {
                "app": {
                    "image": "myapp:1.0",
                    "environment": {"APP_SECRET": "changeme"},
                    "env_file": ".env.app",
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.variables["APP_SECRET"].value == "changeme"
        assert result.env_files["app"] == [".env.app"]

    def test_multiple_services(self, tmp_path: Path):
        compose = {
            "services": {
                "web": {
                    "image": "nginx:1.25",
                    "environment": ["PORT=80"],
                },
                "api": {
                    "image": "myapi:2.0",
                    "environment": {"LOG_LEVEL": "info"},
                },
                "db": {
                    "image": "postgres:16",
                    "environment": ["POSTGRES_DB=myapp"],
                },
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.total_services == 3
        assert result.services["web"] == "nginx:1.25"
        assert result.services["api"] == "myapi:2.0"
        assert result.services["db"] == "postgres:16"
        assert result.variables["PORT"].value == "80"
        assert result.variables["LOG_LEVEL"].value == "info"
        assert result.variables["POSTGRES_DB"].value == "myapp"

    def test_no_environment_block(self, tmp_path: Path):
        compose = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.total_services == 1
        assert result.variables == {}
        assert result.services["web"] == "nginx:latest"

    def test_empty_services(self, tmp_path: Path):
        compose = {"services": {}}
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.total_services == 0
        assert result.variables == {}
        assert result.services == {}

    def test_no_services_key(self, tmp_path: Path):
        compose = {"version": "3.8"}
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.total_services == 0

    def test_env_array_without_values(self, tmp_path: Path):
        """Array form with just key names (no =value) should produce
        entries with empty values."""
        compose = {
            "services": {
                "web": {
                    "image": "nginx:latest",
                    "environment": [
                        "HOSTNAME",
                        "NODE_ENV",
                    ],
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        assert result.variables["HOSTNAME"].value == ""
        assert result.variables["NODE_ENV"].value == ""

    def test_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yml"
        with pytest.raises(FileNotFoundError, match="Docker compose file not found"):
            scan_docker_compose(missing)

    def test_invalid_yaml(self, tmp_path: Path):
        f = tmp_path / "docker-compose.yml"
        f.write_text("[[invalid")
        with pytest.raises(ValueError, match="Invalid YAML in docker-compose"):
            scan_docker_compose(f)

    def test_env_entry_has_line_numbers(self, tmp_path: Path):
        compose = {
            "services": {
                "web": {
                    "environment": [
                        "KEY_A=val_a",
                        "KEY_B=val_b",
                    ]
                }
            }
        }
        f = tmp_path / "docker-compose.yml"
        f.write_text(yaml.dump(compose))
        result = scan_docker_compose(f)

        # Line numbers in YAML are approximate (one per array entry)
        assert result.variables["KEY_A"].line_number == 1
        assert result.variables["KEY_B"].line_number == 2


# ===================================================================
# Dockerfile scanner tests
# ===================================================================


class TestScanDockerfile:
    def test_simple_env(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM python:3.13-slim\nENV PYTHONUNBUFFERED=1\n")
        result = scan_dockerfile(f)

        assert result.source == str(f.resolve())
        assert result.base_image == "python:3.13-slim"
        assert result.variables["PYTHONUNBUFFERED"].value == "1"
        assert result.variables["PYTHONUNBUFFERED"].instruction == "ENV"

    def test_env_key_value_form(self, tmp_path: Path):
        """ENV KEY VALUE form (space-separated, first space delimits value)."""
        f = tmp_path / "Dockerfile"
        f.write_text("FROM ubuntu:22.04\nENV DEBIAN_FRONTEND noninteractive\n")
        result = scan_dockerfile(f)

        assert result.variables["DEBIAN_FRONTEND"].value == "noninteractive"

    def test_env_quoted_value(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text('FROM node:20\nENV APP_NAME="my app"\n')
        result = scan_dockerfile(f)

        assert result.variables["APP_NAME"].value == "my app"

    def test_args_with_defaults(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text(
            "FROM python:3.13-slim\n"
            "ARG PYTHON_VERSION=3.13\n"
            "ARG BUILD_ENV=production\n"
        )
        result = scan_dockerfile(f)

        assert result.args["PYTHON_VERSION"] == "3.13"
        assert result.args["BUILD_ENV"] == "production"

    def test_args_without_defaults(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM alpine:3.19\nARG BUILDKIT_SIGNATURE\n")
        result = scan_dockerfile(f)

        assert result.args["BUILDKIT_SIGNATURE"] is None

    def test_combined_env_and_args(self, tmp_path: Path):
        dockerfile = """FROM python:3.13-slim
ARG PYTHON_VERSION
ARG APP_ENV=production
ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/app
ENV PATH /app/bin:$PATH
"""
        f = tmp_path / "Dockerfile"
        f.write_text(dockerfile)
        result = scan_dockerfile(f)

        assert result.base_image == "python:3.13-slim"
        assert result.args["PYTHON_VERSION"] is None
        assert result.args["APP_ENV"] == "production"
        assert result.variables["PYTHONUNBUFFERED"].value == "1"
        assert result.variables["APP_HOME"].value == "/app"
        assert result.variables["PATH"].value == "/app/bin:$PATH"
        assert result.total_lines == 6
        assert result.parsed_lines == 6

    def test_comments_and_blank_lines(self, tmp_path: Path):
        dockerfile = """# This is a comment
FROM node:20-alpine

# Another comment
ARG NODE_VERSION=20
ENV NODE_ENV=production

"""
        f = tmp_path / "Dockerfile"
        f.write_text(dockerfile)
        result = scan_dockerfile(f)

        assert result.base_image == "node:20-alpine"
        assert result.args["NODE_VERSION"] == "20"
        assert result.variables["NODE_ENV"].value == "production"
        assert result.total_lines == 7
        assert result.parsed_lines == 3

    def test_first_from_wins(self, tmp_path: Path):
        """Multi-stage builds: only the first FROM is captured as base_image."""
        dockerfile = """FROM python:3.13-slim AS builder
...
FROM python:3.13-slim
COPY --from=builder ...
"""
        f = tmp_path / "Dockerfile"
        f.write_text(dockerfile)
        result = scan_dockerfile(f)

        assert result.base_image == "python:3.13-slim"

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "Dockerfile"
        f.write_text("")
        result = scan_dockerfile(f)

        assert result.base_image is None
        assert result.variables == {}
        assert result.args == {}
        assert result.total_lines == 0
        assert result.parsed_lines == 0

    def test_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "Dockerfile"
        with pytest.raises(FileNotFoundError, match="Dockerfile not found"):
            scan_dockerfile(missing)

    def test_no_env_at_all(self, tmp_path: Path):
        """A minimal Dockerfile with only FROM."""
        f = tmp_path / "Dockerfile"
        f.write_text("FROM scratch\n")
        result = scan_dockerfile(f)

        assert result.base_image == "scratch"
        assert result.variables == {}
        assert result.args == {}

    def test_env_with_equals_in_value(self, tmp_path: Path):
        """ENV value can contain = signs."""
        f = tmp_path / "Dockerfile"
        f.write_text('FROM debian:12\nENV CONN_STRING="host=localhost port=5432"\n')
        result = scan_dockerfile(f)

        assert result.variables["CONN_STRING"].value == "host=localhost port=5432"


# ===================================================================
# Batch scanner tests
# ===================================================================


class TestBatchScanners:
    def test_scan_docker_compose_files(self, tmp_path: Path):
        a = tmp_path / "docker-compose.yml"
        a.write_text(yaml.dump({"services": {"web": {"image": "nginx:latest"}}}))
        b = tmp_path / "docker-compose.override.yml"
        b.write_text(yaml.dump({"services": {"db": {"image": "postgres:16"}}}))
        results = scan_docker_compose_files([a, b])
        assert len(results) == 2
        assert "web" in results[0].services
        assert "db" in results[1].services

    def test_scan_dockerfiles(self, tmp_path: Path):
        a = tmp_path / "Dockerfile"
        a.write_text("FROM python:3.13\nENV A=1\n")
        b = tmp_path / "Dockerfile.dev"
        b.write_text("FROM node:20\nENV B=2\n")
        results = scan_dockerfiles([a, b])
        assert len(results) == 2
        assert results[0].base_image == "python:3.13"
        assert results[1].base_image == "node:20"

    def test_missing_files_skipped(self, tmp_path: Path):
        existing = tmp_path / "docker-compose.yml"
        existing.write_text(yaml.dump({"services": {"web": {"image": "nginx:latest"}}}))
        results = scan_docker_compose_files(
            [tmp_path / "missing.yml", existing]
        )
        assert len(results) == 1

    def test_missing_dockerfiles_skipped(self, tmp_path: Path):
        existing = tmp_path / "Dockerfile"
        existing.write_text("FROM alpine\n")
        results = scan_dockerfiles([tmp_path / "Dockerfile.missing", existing])
        assert len(results) == 1


# ===================================================================
# DockerScanResult composition
# ===================================================================


class TestDockerScanResult:
    def test_aggregate_variables(self):
        from envcheck.scanners.docker import DockerScanResult, DockerVarEntry

        a = DockerComposeScanResult(
            source="/compose.yml",
            variables={"DB_HOST": DockerVarEntry(
                key="DB_HOST", value="localhost", source_file="/compose.yml",
                line_number=1, instruction="environment", service="db",
            )},
            services={"db": "postgres:16"},
        )
        b = DockerfileScanResult(
            source="/Dockerfile",
            variables={"PYTHONUNBUFFERED": DockerVarEntry(
                key="PYTHONUNBUFFERED", value="1", source_file="/Dockerfile",
                line_number=2, instruction="ENV",
            )},
            base_image="python:3.13",
        )
        combined = DockerScanResult(compose_results=[a], dockerfile_results=[b])
        all_vars = combined.all_variables
        assert "DB_HOST" in all_vars
        assert "PYTHONUNBUFFERED" in all_vars
        assert combined.all_images == {"db": "postgres:16"}
