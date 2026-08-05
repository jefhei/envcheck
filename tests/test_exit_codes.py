"""Tests for M2.5 — exit codes (0 clean / 1 drift / 2 error) and --strict mode.

Covers both the pure classification logic (:func:`compute_exit_code`) and
the real CLI behavior via Typer's ``CliRunner`` against fixture projects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from envcheck.diff import DiffKind, EnvVarDiffResult, VarDiff
from envcheck.exit_codes import ExitCode, compute_exit_code
from envcheck.main import app
from envcheck.services import ServiceDiff, ServiceDiffKind, ServiceDiffResult

runner = CliRunner()


# ===========================================================================
#  Helpers
# ===========================================================================


def _var_diff_result(*diffs: VarDiff, base: str = "dev", target: str = "staging") -> EnvVarDiffResult:
    """Build an EnvVarDiffResult from VarDiff rows."""
    return EnvVarDiffResult(base_env=base, target_env=target, diffs=list(diffs))


def _match(key: str, value: str = "1") -> VarDiff:
    return VarDiff(key=key, kind=DiffKind.MATCH, base_value=value, target_value=value)


def _missing(key: str, value: str = "1") -> VarDiff:
    return VarDiff(key=key, kind=DiffKind.MISSING, base_value=value, base_type="int")


def _extra(key: str, value: str = "2") -> VarDiff:
    return VarDiff(key=key, kind=DiffKind.EXTRA, target_value=value, target_type="int")


def _changed(key: str, base: str = "1", target: str = "2") -> VarDiff:
    return VarDiff(
        key=key,
        kind=DiffKind.CHANGED,
        base_value=base,
        target_value=target,
        base_type="int",
        target_type="int",
    )


def _type_changed(key: str) -> VarDiff:
    return VarDiff(
        key=key,
        kind=DiffKind.TYPE_CHANGED,
        base_value="1",
        target_value="true",
        base_type="int",
        target_type="bool",
    )


def _service_result(*diffs: ServiceDiff, base: str = "dev", target: str = "staging") -> ServiceDiffResult:
    return ServiceDiffResult(base_env=base, target_env=target, diffs=list(diffs))


def _service_match() -> ServiceDiff:
    return ServiceDiff(
        service="postgres",
        kind=ServiceDiffKind.MATCH,
        base_image="postgres:16",
        target_image="postgres:16",
        base_version="16",
        target_version="16",
    )


def _service_version_changed() -> ServiceDiff:
    return ServiceDiff(
        service="postgres",
        kind=ServiceDiffKind.VERSION_CHANGED,
        base_image="postgres:16",
        target_image="postgres:15",
        base_version="16",
        target_version="15",
    )


def _service_extra() -> ServiceDiff:
    return ServiceDiff(service="redis", kind=ServiceDiffKind.EXTRA, target_image="redis:7", target_version="7")


# ===========================================================================
#  Unit tests — compute_exit_code
# ===========================================================================


class TestComputeExitCode:
    """Pure classification of (env_diff, service_diff, strict) → exit code."""

    def test_no_inputs_is_clean(self) -> None:
        assert compute_exit_code(None, None) == ExitCode.OK
        assert compute_exit_code(None, None, strict=True) == ExitCode.OK

    def test_clean_is_ok_in_both_modes(self) -> None:
        result = _var_diff_result(_match("A"), _match("B"))
        assert compute_exit_code(result, None) == ExitCode.OK
        assert compute_exit_code(result, None, strict=True) == ExitCode.OK

    def test_all_matches_plus_service_matches_is_clean(self) -> None:
        env = _var_diff_result(_match("A"))
        svc = _service_result(_service_match())
        assert compute_exit_code(env, svc) == ExitCode.OK
        assert compute_exit_code(env, svc, strict=True) == ExitCode.OK

    @pytest.mark.parametrize(
        "diff",
        [_missing("DATABASE_URL"), _changed("PORT"), _type_changed("DEBUG")],
        ids=["missing", "changed", "type_changed"],
    )
    def test_critical_env_drift_fails_in_both_modes(self, diff: VarDiff) -> None:
        result = _var_diff_result(_match("A"), diff)
        assert compute_exit_code(result, None) == ExitCode.DRIFT
        assert compute_exit_code(result, None, strict=True) == ExitCode.DRIFT

    def test_extra_only_is_ok_by_default_but_fails_strict(self) -> None:
        result = _var_diff_result(_match("A"), _extra("B"))
        assert compute_exit_code(result, None) == ExitCode.OK
        assert compute_exit_code(result, None, strict=True) == ExitCode.DRIFT

    def test_extra_plus_critical_fails_in_both_modes(self) -> None:
        result = _var_diff_result(_match("A"), _extra("B"), _missing("C"))
        assert compute_exit_code(result, None) == ExitCode.DRIFT
        assert compute_exit_code(result, None, strict=True) == ExitCode.DRIFT

    def test_service_drift_fails_in_both_modes(self) -> None:
        svc = _service_result(_service_version_changed())
        assert compute_exit_code(None, svc) == ExitCode.DRIFT
        assert compute_exit_code(None, svc, strict=True) == ExitCode.DRIFT

    def test_service_extra_fails_in_both_modes(self) -> None:
        # Services are explicitly tracked in config, so a service present
        # in only one environment is parity-critical even in default mode.
        svc = _service_result(_service_extra())
        assert compute_exit_code(None, svc) == ExitCode.DRIFT
        assert compute_exit_code(None, svc, strict=True) == ExitCode.DRIFT

    def test_service_match_does_not_fail(self) -> None:
        svc = _service_result(_service_match())
        assert compute_exit_code(None, svc) == ExitCode.OK

    def test_strict_counts_extras_combined_with_services(self) -> None:
        env = _var_diff_result(_match("A"), _extra("B"))
        svc = _service_result(_service_match())
        assert compute_exit_code(env, svc) == ExitCode.OK
        assert compute_exit_code(env, svc, strict=True) == ExitCode.DRIFT


# ===========================================================================
#  CLI integration tests — fixture projects through the real command
# ===========================================================================


def _write_env_file(path: Path, variables: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in variables.items()), encoding="utf-8")


def _make_project(
    tmp_path: Path,
    dev: dict[str, str],
    staging: dict[str, str],
    *,
    extra_yaml: dict | None = None,
) -> Path:
    """Create a two-environment fixture project in tmp_path and return its path."""
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg: dict = {
        "environments": {
            "dev": {"paths": [".env.dev"]},
            "staging": {"paths": [".env.staging"]},
        }
    }
    if extra_yaml:
        cfg.update(extra_yaml)
    (proj / ".envcheck.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _write_env_file(proj / ".env.dev", dev)
    _write_env_file(proj / ".env.staging", staging)
    return proj


def _invoke(proj: Path, *extra_args: str) -> Result:
    """Run ``envcheck diff dev staging`` against *proj* with extra args."""
    return runner.invoke(
        app,
        [
            "diff",
            "dev",
            "staging",
            "--config",
            str(proj / ".envcheck.yaml"),
            "--root",
            str(proj),
            *extra_args,
        ],
    )


class TestCliExitCodes:
    """End-to-end exit codes through the real Typer command."""

    def test_clean_project_exits_zero(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1"})
        result = _invoke(proj)
        assert result.exit_code == ExitCode.OK

    def test_clean_project_exits_zero_with_strict(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1"})
        result = _invoke(proj, "--strict")
        assert result.exit_code == ExitCode.OK

    def test_missing_var_exits_one_in_both_modes(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1", "DATABASE_URL": "x"}, {"A": "1"})
        assert _invoke(proj).exit_code == ExitCode.DRIFT
        assert _invoke(proj, "--strict").exit_code == ExitCode.DRIFT

    def test_changed_value_exits_one(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"PORT": "8000"}, {"PORT": "8080"})
        result = _invoke(proj)
        assert result.exit_code == ExitCode.DRIFT
        assert "Drift detected" in result.stdout

    def test_type_changed_exits_one(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"DEBUG": "1"}, {"DEBUG": "true"})
        assert _invoke(proj).exit_code == ExitCode.DRIFT

    def test_extra_only_exits_zero_by_default(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1", "STAGING_ONLY": "2"})
        result = _invoke(proj)
        assert result.exit_code == ExitCode.OK
        assert "STAGING_ONLY" in result.stdout  # still reported as extra

    def test_extra_only_exits_one_in_strict_mode(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1", "STAGING_ONLY": "2"})
        result = _invoke(proj, "--strict")
        assert result.exit_code == ExitCode.DRIFT

    def test_json_output_still_exits_one_on_drift(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1", "GONE": "x"}, {"A": "1"})
        result = _invoke(proj, "--json")
        assert result.exit_code == ExitCode.DRIFT
        report = json.loads(result.stdout)
        assert report["drift_count"] == 1
        assert report["verdict"] == "drift detected"

    def test_json_output_exits_zero_when_clean(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1"})
        result = _invoke(proj, "--json")
        assert result.exit_code == ExitCode.OK
        assert json.loads(result.stdout)["verdict"] == "in sync"


class TestCliErrorExitCode:
    """Every failure mode must exit 2, never 1, and never traceback."""

    def test_missing_config_exits_two(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["diff", "dev", "staging", "--config", str(tmp_path / "nope.yaml")]
        )
        assert result.exit_code == ExitCode.ERROR
        assert "Error:" in result.stderr

    def test_unknown_environment_exits_two(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, {"A": "1"}, {"A": "1"})
        result = runner.invoke(
            app,
            [
                "diff",
                "dev",
                "prod",
                "--config",
                str(proj / ".envcheck.yaml"),
                "--root",
                str(proj),
            ],
        )
        assert result.exit_code == ExitCode.ERROR
        assert "Error:" in result.stderr

    def test_malformed_yaml_exits_two(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".envcheck.yaml").write_text("environments: [unclosed\n", encoding="utf-8")
        result = _invoke(proj)
        assert result.exit_code == ExitCode.ERROR
        assert "Error:" in result.stderr

    def test_unsupported_version_field_exits_two(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path,
            {"A": "1"},
            {"A": "1"},
            extra_yaml={"services": {"postgres": {"version_field": "build"}}},
        )
        result = _invoke(proj)
        assert result.exit_code == ExitCode.ERROR
        assert "version_field" in result.stderr

    def test_usage_error_exits_two(self, tmp_path: Path) -> None:
        # Missing required positional arguments is a usage error.
        result = runner.invoke(app, ["diff"])
        assert result.exit_code == ExitCode.ERROR


# ===========================================================================
#  CLI integration — service version drift
# ===========================================================================


class TestCliServiceExitCodes:
    """Docker-service drift produces exit 1 through the CLI."""

    def _make_service_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        cfg = {
            "environments": {
                "dev": {"paths": ["docker-compose.dev.yml"]},
                "staging": {"paths": ["docker-compose.staging.yml"]},
            },
            "services": {"postgres": {"version_field": "image"}},
        }
        (proj / ".envcheck.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        (proj / "docker-compose.dev.yml").write_text(
            "services:\n  postgres:\n    image: postgres:16\n", encoding="utf-8"
        )
        (proj / "docker-compose.staging.yml").write_text(
            "services:\n  postgres:\n    image: postgres:15\n", encoding="utf-8"
        )
        return proj

    def test_version_mismatch_exits_one(self, tmp_path: Path) -> None:
        proj = self._make_service_project(tmp_path)
        result = _invoke(proj)
        assert result.exit_code == ExitCode.DRIFT
        assert "version changed" in result.stdout

    def test_version_mismatch_exits_one_in_strict_mode(self, tmp_path: Path) -> None:
        proj = self._make_service_project(tmp_path)
        assert _invoke(proj, "--strict").exit_code == ExitCode.DRIFT
