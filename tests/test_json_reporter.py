"""Tests for the JSON output mode (``--json`` flag, PRD F7).

Covers the serializer schema (:mod:`envcheck.json_reporter`) and the
``envcheck diff --json`` CLI path end-to-end.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from envcheck.config import EnvironmentConfig, EnvcheckConfig
from envcheck.diff import (
    DiffKind,
    EnvVarDiffResult,
    VarDiff,
    diff_env_vars,
)
from envcheck.json_reporter import (
    build_json_report,
    print_json_report,
    render_json_report,
)
from envcheck.main import app
from envcheck.profile import build_profile
from envcheck.services import (
    ServiceDiff,
    ServiceDiffKind,
    ServiceDiffResult,
    diff_services,
)

runner = CliRunner()

#: One VarDiff of every kind for schema tests (sorted by key).
_ALL_KINDS = [
    VarDiff(key="DEBUG", kind=DiffKind.TYPE_CHANGED, base_value="1", target_value="true",
            base_type="int", target_type="bool", base_source="/p/.env.dev",
            target_source="/p/.env.staging"),
    VarDiff(key="FEATURE_FLAG", kind=DiffKind.MISSING, base_value="true",
            base_type="bool", base_source="/p/.env.dev"),
    VarDiff(key="LOG_LEVEL", kind=DiffKind.CHANGED, base_value="info", target_value="debug",
            base_type="string", target_type="string"),
    VarDiff(key="ONLY_STAGING", kind=DiffKind.EXTRA, target_value="present",
            target_type="string", target_source="/p/.env.staging"),
    VarDiff(key="PORT", kind=DiffKind.MATCH, base_value="8080", target_value="8080",
            base_type="int", target_type="int"),
]

_ALL_SERVICE_KINDS = [
    ServiceDiff(service="postgres", kind=ServiceDiffKind.VERSION_CHANGED,
                base_image="postgres:16", target_image="postgres:15",
                base_version="16", target_version="15"),
    ServiceDiff(service="redis", kind=ServiceDiffKind.MATCH,
                base_image="redis:7.2", target_image="redis:7.2",
                base_version="7.2", target_version="7.2"),
    ServiceDiff(service="worker", kind=ServiceDiffKind.EXTRA,
                target_image="myapp:1.0", target_version="1.0"),
]


def _env_result() -> EnvVarDiffResult:
    return EnvVarDiffResult(base_env="dev", target_env="staging", diffs=_ALL_KINDS)


def _service_result() -> ServiceDiffResult:
    return ServiceDiffResult(base_env="dev", target_env="staging", diffs=_ALL_SERVICE_KINDS)


def _parse(json_text: str) -> dict:
    """Parse rendered JSON output and assert it is valid."""
    return json.loads(json_text)


# ===========================================================================
#  build_json_report — schema
# ===========================================================================


class TestSchema:
    """Top-level document structure is stable and always complete."""

    def test_top_level_keys(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        assert list(report.keys()) == [
            "base_env",
            "target_env",
            "env_vars",
            "services",
            "drift_count",
            "verdict",
        ]

    def test_env_names(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        assert report["base_env"] == "dev"
        assert report["target_env"] == "staging"

    def test_env_vars_section_shape(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        env_section = report["env_vars"]
        assert set(env_section.keys()) == {"total_compared", "drift_count", "diffs"}
        assert env_section["total_compared"] == 5
        assert env_section["drift_count"] == 4
        assert len(env_section["diffs"]) == 5

    def test_services_section_shape(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        svc_section = report["services"]
        assert set(svc_section.keys()) == {"total_compared", "drift_count", "diffs"}
        assert svc_section["total_compared"] == 3
        assert svc_section["drift_count"] == 2
        assert len(svc_section["diffs"]) == 3

    def test_drift_count_is_sum_of_sections(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        assert report["drift_count"] == 4 + 2

    def test_verdict_drift_detected(self) -> None:
        report = build_json_report(_env_result(), _service_result())
        assert report["verdict"] == "drift detected"

    def test_verdict_in_sync_when_clean(self) -> None:
        clean = EnvVarDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MATCH],
        )
        report = build_json_report(clean, None)
        assert report["drift_count"] == 0
        assert report["verdict"] == "in sync"

    def test_none_results_produce_empty_sections(self) -> None:
        report = build_json_report(None, None)
        assert report["env_vars"] == {"total_compared": 0, "drift_count": 0, "diffs": []}
        assert report["services"] == {"total_compared": 0, "drift_count": 0, "diffs": []}
        assert report["drift_count"] == 0
        assert report["verdict"] == "in sync"
        assert report["base_env"] == ""
        assert report["target_env"] == ""


# ===========================================================================
#  build_json_report — diff row contents
# ===========================================================================


class TestEnvVarRows:
    """Every VarDiff serializes to a stable object with all fields."""

    def test_row_keys(self) -> None:
        report = build_json_report(_env_result(), None)
        row = report["env_vars"]["diffs"][0]
        assert list(row.keys()) == [
            "key",
            "kind",
            "base_value",
            "target_value",
            "base_type",
            "target_type",
            "base_source",
            "target_source",
        ]

    def test_kinds_serialize_to_string_values(self) -> None:
        report = build_json_report(_env_result(), None)
        kinds = [row["kind"] for row in report["env_vars"]["diffs"]]
        assert kinds == ["type_changed", "missing", "changed", "extra", "match"]

    def test_match_row_carries_both_values(self) -> None:
        report = build_json_report(_env_result(), None)
        by_key = {row["key"]: row for row in report["env_vars"]["diffs"]}
        port = by_key["PORT"]
        assert port["kind"] == "match"
        assert port["base_value"] == "8080"
        assert port["target_value"] == "8080"
        assert port["base_type"] == "int"
        assert port["target_type"] == "int"

    def test_missing_row_has_null_target_fields(self) -> None:
        report = build_json_report(_env_result(), None)
        by_key = {row["key"]: row for row in report["env_vars"]["diffs"]}
        missing = by_key["FEATURE_FLAG"]
        assert missing["kind"] == "missing"
        assert missing["base_value"] == "true"
        assert missing["target_value"] is None
        assert missing["target_type"] is None
        assert missing["target_source"] is None
        assert missing["base_source"] == "/p/.env.dev"

    def test_extra_row_has_null_base_fields(self) -> None:
        report = build_json_report(_env_result(), None)
        by_key = {row["key"]: row for row in report["env_vars"]["diffs"]}
        extra = by_key["ONLY_STAGING"]
        assert extra["kind"] == "extra"
        assert extra["base_value"] is None
        assert extra["target_value"] == "present"
        assert extra["target_source"] == "/p/.env.staging"


class TestServiceRows:
    """Every ServiceDiff serializes to a stable object with all fields."""

    def test_row_keys(self) -> None:
        report = build_json_report(None, _service_result())
        row = report["services"]["diffs"][0]
        assert list(row.keys()) == [
            "service",
            "kind",
            "base_image",
            "target_image",
            "base_version",
            "target_version",
            "base_source",
            "target_source",
        ]

    def test_version_changed_row(self) -> None:
        report = build_json_report(None, _service_result())
        by_name = {row["service"]: row for row in report["services"]["diffs"]}
        postgres = by_name["postgres"]
        assert postgres["kind"] == "version_changed"
        assert postgres["base_image"] == "postgres:16"
        assert postgres["target_image"] == "postgres:15"
        assert postgres["base_version"] == "16"
        assert postgres["target_version"] == "15"


# ===========================================================================
#  render_json_report / print_json_report
# ===========================================================================


class TestRendering:
    def test_render_is_valid_json(self) -> None:
        text = render_json_report(_env_result(), _service_result())
        parsed = _parse(text)
        assert parsed["drift_count"] == 6

    def test_render_ends_with_newline(self) -> None:
        assert render_json_report(None, None).endswith("\n")

    def test_render_utf8_no_escapes(self) -> None:
        # ensure_ascii=False — non-ASCII values survive unescaped
        result = EnvVarDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[
                VarDiff(key="GREETING", kind=DiffKind.CHANGED,
                        base_value="héllo", target_value="hëllo",
                        base_type="string", target_type="string"),
            ],
        )
        text = render_json_report(result, None)
        assert "héllo" in text
        assert "\\u" not in text

    def test_print_writes_to_file(self) -> None:
        buffer = io.StringIO()
        print_json_report(_env_result(), _service_result(), file=buffer)
        parsed = _parse(buffer.getvalue())
        assert parsed["base_env"] == "dev"
        assert parsed["verdict"] == "drift detected"


# ===========================================================================
#  CLI integration — envcheck diff --json
# ===========================================================================


class TestCliJson:
    """End-to-end: fixture project → `envcheck diff --json` on stdout."""

    @pytest.fixture()
    def project(self, tmp_path: Path) -> Path:
        (tmp_path / ".env.dev").write_text(
            "DATABASE_URL=postgres://localhost:5432/mydb\n"
            "PORT=3000\n"
            "DEBUG=1\n"
            "FEATURE_FLAG=true\n",
            encoding="utf-8",
        )
        (tmp_path / ".env.staging").write_text(
            "DATABASE_URL=postgres://db.internal:5432/mydb\n"
            "PORT=3000\n"
            "DEBUG=true\n"
            "ONLY_STAGING=present\n",
            encoding="utf-8",
        )
        (tmp_path / "docker-compose.dev.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:16\n",
            encoding="utf-8",
        )
        (tmp_path / "docker-compose.staging.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:15\n",
            encoding="utf-8",
        )
        (tmp_path / ".envcheck.yaml").write_text(
            "environments:\n"
            "  dev:\n"
            "    paths:\n"
            "      - .env.dev\n"
            "      - docker-compose.dev.yml\n"
            "  staging:\n"
            "    paths:\n"
            "      - .env.staging\n"
            "      - docker-compose.staging.yml\n",
            encoding="utf-8",
        )
        return tmp_path

    def _run(self, project: Path, *extra: str):
        return runner.invoke(
            app,
            ["diff", "dev", "staging", "--config", str(project / ".envcheck.yaml"),
             "--root", str(project), *extra],
        )

    def test_json_flag_emits_only_json(self, project: Path) -> None:
        result = self._run(project, "--json")
        assert result.exit_code == 0, result.output
        parsed = _parse(result.stdout)
        assert parsed["base_env"] == "dev"
        assert parsed["target_env"] == "staging"

    def test_json_contains_env_var_diff(self, project: Path) -> None:
        parsed = _parse(self._run(project, "--json").stdout)
        by_key = {row["key"]: row for row in parsed["env_vars"]["diffs"]}
        assert by_key["DATABASE_URL"]["kind"] == "changed"
        assert by_key["DEBUG"]["kind"] == "type_changed"
        assert by_key["FEATURE_FLAG"]["kind"] == "missing"
        assert by_key["ONLY_STAGING"]["kind"] == "extra"
        assert by_key["PORT"]["kind"] == "match"
        assert parsed["env_vars"]["total_compared"] == 5
        assert parsed["env_vars"]["drift_count"] == 4

    def test_json_contains_service_diff(self, project: Path) -> None:
        parsed = _parse(self._run(project, "--json").stdout)
        assert parsed["services"]["total_compared"] == 1
        assert parsed["services"]["diffs"][0]["service"] == "postgres"
        assert parsed["services"]["diffs"][0]["kind"] == "version_changed"
        assert parsed["drift_count"] == 5
        assert parsed["verdict"] == "drift detected"

    def test_json_clean_project_verdict(self, tmp_path: Path) -> None:
        (tmp_path / ".env.dev").write_text("A=1\n", encoding="utf-8")
        (tmp_path / ".env.staging").write_text("A=1\n", encoding="utf-8")
        (tmp_path / ".envcheck.yaml").write_text(
            "environments:\n"
            "  dev:\n    paths:\n      - .env.dev\n"
            "  staging:\n    paths:\n      - .env.staging\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app,
            ["diff", "dev", "staging", "--config", str(tmp_path / ".envcheck.yaml"),
             "--root", str(tmp_path), "--json"],
        )
        assert result.exit_code == 0, result.output
        parsed = _parse(result.stdout)
        assert parsed["drift_count"] == 0
        assert parsed["verdict"] == "in sync"

    def test_default_output_is_rich_table(self, project: Path) -> None:
        result = self._run(project)
        assert result.exit_code == 0, result.output
        assert "Environment variables — dev vs staging" in result.stdout
        assert "Drift detected" in result.stdout

    def test_unknown_environment_exits_with_error(self, project: Path) -> None:
        result = runner.invoke(
            app,
            ["diff", "dev", "prod", "--config", str(project / ".envcheck.yaml"),
             "--root", str(project), "--json"],
        )
        assert result.exit_code == 2
        assert "Error:" in result.stderr or "Error:" in result.output

    def test_missing_config_exits_with_error(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["diff", "dev", "staging", "--config", str(tmp_path / "nope.yaml"), "--json"],
        )
        assert result.exit_code == 2
        assert "Error:" in result.stderr or "Error:" in result.output


# ===========================================================================
#  Integration — library path produces identical data to CLI
# ===========================================================================


class TestJsonMatchesLibrary:
    def test_cli_output_equals_build_json_report(self, tmp_path: Path) -> None:
        (tmp_path / ".env.dev").write_text("A=1\nB=2\n", encoding="utf-8")
        (tmp_path / ".env.staging").write_text("A=1\nC=3\n", encoding="utf-8")
        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=[".env.dev"]),
                "staging": EnvironmentConfig(paths=[".env.staging"]),
            },
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)
        env_diff = diff_env_vars(dev, staging)
        svc_diff = diff_services(dev, staging)

        from_envcheck_diff = build_json_report(env_diff, svc_diff)

        (tmp_path / ".envcheck.yaml").write_text(
            "environments:\n"
            "  dev:\n    paths:\n      - .env.dev\n"
            "  staging:\n    paths:\n      - .env.staging\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app,
            ["diff", "dev", "staging", "--config", str(tmp_path / ".envcheck.yaml"),
             "--root", str(tmp_path), "--json"],
        )
        assert result.exit_code == 0, result.output
        assert _parse(result.stdout) == from_envcheck_diff
