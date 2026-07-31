"""Tests for the environment-variable diff engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from envcheck.config import EnvironmentConfig, EnvcheckConfig
from envcheck.diff import (
    DiffKind,
    EnvVarDiffResult,
    VarDiff,
    diff_env_vars,
    infer_value_type,
)
from envcheck.profile import EnvironmentProfile, build_profile
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry


# ===========================================================================
#  infer_value_type
# ===========================================================================


class TestInferValueType:
    """Verify value-type inference used for type_changed detection."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # booleans (case-insensitive)
            ("true", "bool"),
            ("True", "bool"),
            ("TRUE", "bool"),
            ("false", "bool"),
            ("False", "bool"),
            # integers
            ("0", "int"),
            ("42", "int"),
            ("-7", "int"),
            ("+3", "int"),
            ("8080", "int"),
            # floats (decimal + scientific notation)
            ("3.14", "float"),
            (".5", "float"),
            ("1.", "float"),
            ("1e5", "float"),
            ("-2.5e-3", "float"),
            # strings
            ("hello", "string"),
            ("postgres://localhost:5432/mydb", "string"),
            ("0x10", "string"),  # hex is NOT treated as int
            ("1_000", "string"),  # underscore-separated is NOT treated as int
            (" 42 ", "string"),  # surrounding whitespace is not stripped here
            ("${{ secrets.API_KEY }}", "string"),
            # empty
            ("", "empty"),
        ],
    )
    def test_infer(self, value: str, expected: str) -> None:
        assert infer_value_type(value) == expected


# ===========================================================================
#  diff_env_vars — model-level tests
# ===========================================================================


def _profile(name: str, variables: dict[str, str]) -> EnvironmentProfile:
    """Build an EnvironmentProfile with env-file entries for the given vars."""
    entries = {
        key: EnvVarEntry(key=key, value=value, source_file=f"/p/.env.{name}", line_number=i + 1)
        for i, (key, value) in enumerate(variables.items())
    }
    return EnvironmentProfile(
        name=name,
        env_file_results=[
            EnvFileScanResult(
                source=f"/p/.env.{name}",
                variables=entries,
                total_lines=len(entries),
                parsed_lines=len(entries),
            ),
        ],
    )


class TestDiffEnvVars:
    """Verify the five-way classification of the diff engine."""

    def test_empty_profiles(self) -> None:
        result = diff_env_vars(_profile("dev", {}), _profile("staging", {}))
        assert isinstance(result, EnvVarDiffResult)
        assert result.base_env == "dev"
        assert result.target_env == "staging"
        assert result.diffs == []
        assert result.total_compared == 0
        assert result.drift_count == 0
        assert not result.has_drift

    def test_identical_profiles_all_match(self) -> None:
        base = _profile("dev", {"PORT": "8080", "DB_URL": "localhost"})
        target = _profile("staging", {"PORT": "8080", "DB_URL": "localhost"})
        result = diff_env_vars(base, target)

        assert result.total_compared == 2
        assert len(result.matches) == 2
        assert result.missing == []
        assert result.extra == []
        assert result.changed == []
        assert result.type_changed == []
        assert result.drift_count == 0
        assert not result.has_drift
        assert all(d.kind == DiffKind.MATCH for d in result.diffs)

    def test_missing_and_extra(self) -> None:
        base = _profile("dev", {"SHARED": "x", "ONLY_IN_BASE": "1"})
        target = _profile("staging", {"SHARED": "x", "ONLY_IN_TARGET": "2"})
        result = diff_env_vars(base, target)

        kinds = {d.key: d.kind for d in result.diffs}
        assert kinds["SHARED"] == DiffKind.MATCH
        assert kinds["ONLY_IN_BASE"] == DiffKind.MISSING
        assert kinds["ONLY_IN_TARGET"] == DiffKind.EXTRA

        missing = result.missing
        assert len(missing) == 1
        assert missing[0].key == "ONLY_IN_BASE"
        assert missing[0].base_value == "1"
        assert missing[0].base_type == "int"
        assert missing[0].target_value is None

        extra = result.extra
        assert len(extra) == 1
        assert extra[0].key == "ONLY_IN_TARGET"
        assert extra[0].target_value == "2"
        assert extra[0].target_type == "int"
        assert extra[0].base_value is None

        assert result.has_drift
        assert result.drift_count == 2
        assert result.total_compared == 3

    def test_value_changed_same_type(self) -> None:
        base = _profile("dev", {"PORT": "8080", "LOG_LEVEL": "info"})
        target = _profile("staging", {"PORT": "9090", "LOG_LEVEL": "debug"})
        result = diff_env_vars(base, target)

        assert len(result.changed) == 2
        assert result.type_changed == []
        for d in result.changed:
            assert d.kind == DiffKind.CHANGED
            assert d.base_type == d.target_type

        port = next(d for d in result.changed if d.key == "PORT")
        assert port.base_value == "8080"
        assert port.target_value == "9090"
        assert port.base_type == "int"
        assert port.target_type == "int"

    def test_type_changed_detected(self) -> None:
        base = _profile("dev", {"DEBUG": "1", "RATIO": "0.5"})
        target = _profile("staging", {"DEBUG": "true", "RATIO": "2"})
        result = diff_env_vars(base, target)

        type_changed = result.type_changed
        assert len(type_changed) == 2
        assert result.changed == []

        debug = next(d for d in type_changed if d.key == "DEBUG")
        assert debug.kind == DiffKind.TYPE_CHANGED
        assert debug.base_value == "1"
        assert debug.base_type == "int"
        assert debug.target_value == "true"
        assert debug.target_type == "bool"

        ratio = next(d for d in type_changed if d.key == "RATIO")
        assert ratio.base_type == "float"
        assert ratio.target_type == "int"

    def test_same_value_different_sources_is_match(self) -> None:
        """Same value defined in different files must still be a match."""
        base = _profile("dev", {"API_KEY": "abc"})
        target = _profile("staging", {"API_KEY": "abc"})
        result = diff_env_vars(base, target)
        assert len(result.matches) == 1
        assert not result.has_drift

    def test_sources_captured(self) -> None:
        base = _profile("dev", {"DB_URL": "localhost"})
        target = _profile("staging", {"DB_URL": "db.internal"})
        result = diff_env_vars(base, target)

        assert len(result.diffs) == 1
        d = result.diffs[0]
        assert d.base_source == "/p/.env.dev"
        assert d.target_source == "/p/.env.staging"

    def test_missing_captures_base_source_only(self) -> None:
        base = _profile("dev", {"ONLY": "1"})
        target = _profile("staging", {})
        result = diff_env_vars(base, target)
        assert result.missing[0].base_source == "/p/.env.dev"
        assert result.missing[0].target_source is None

    def test_diffs_sorted_by_key(self) -> None:
        base = _profile("dev", {"ZED": "1", "ALPHA": "2", "MIKE": "3"})
        target = _profile("staging", {})
        result = diff_env_vars(base, target)
        keys = [d.key for d in result.diffs]
        assert keys == sorted(keys)


class TestIgnorePatterns:
    """Verify exact-name and glob ignore patterns are honored."""

    def test_exact_ignore(self) -> None:
        base = _profile("dev", {"BUILD_ID": "123", "PORT": "8080"})
        target = _profile("staging", {"BUILD_ID": "456", "PORT": "8080"})
        result = diff_env_vars(base, target, ignore=["BUILD_ID"])
        assert result.total_compared == 1
        assert result.diffs[0].key == "PORT"

    def test_glob_ignore(self) -> None:
        base = _profile("dev", {"TIMESTAMP_A": "1", "TIMESTAMP_B": "2", "PORT": "8080"})
        target = _profile("staging", {"TIMESTAMP_A": "9", "TIMESTAMP_B": "9", "PORT": "8080"})
        result = diff_env_vars(base, target, ignore=["TIMESTAMP_*"])
        assert result.total_compared == 1
        assert not result.has_drift

    def test_ignore_removes_drift(self) -> None:
        base = _profile("dev", {"ONLY_BASE": "1", "SHARED": "x"})
        target = _profile("staging", {"ONLY_TARGET": "2", "SHARED": "x"})
        result = diff_env_vars(base, target, ignore=["ONLY_*"])
        assert result.total_compared == 1
        assert result.diffs[0].key == "SHARED"
        assert not result.has_drift

    def test_empty_ignore_list_matches_nothing(self) -> None:
        base = _profile("dev", {"A": "1"})
        target = _profile("staging", {"A": "2"})
        result = diff_env_vars(base, target, ignore=[])
        assert result.total_compared == 1
        assert result.has_drift


# ===========================================================================
#  diff_env_vars — integration tests (real scanners + profile builder)
# ===========================================================================


class TestDiffIntegration:
    """End-to-end: scan fixture projects, build profiles, then diff them."""

    def test_diff_built_profiles(self, tmp_path: Path) -> None:
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

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=[".env.dev"]),
                "staging": EnvironmentConfig(paths=[".env.staging"]),
            },
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)
        result = diff_env_vars(dev, staging)

        kinds = {d.key: d.kind for d in result.diffs}
        assert kinds["DATABASE_URL"] == DiffKind.CHANGED
        assert kinds["PORT"] == DiffKind.MATCH
        assert kinds["DEBUG"] == DiffKind.TYPE_CHANGED
        assert kinds["FEATURE_FLAG"] == DiffKind.MISSING
        assert kinds["ONLY_STAGING"] == DiffKind.EXTRA

        assert result.has_drift
        assert result.drift_count == 4
        assert result.total_compared == 5
        # Source paths come from the real scanner
        db = next(d for d in result.diffs if d.key == "DATABASE_URL")
        assert db.base_source == str((tmp_path / ".env.dev").resolve())
        assert db.target_source == str((tmp_path / ".env.staging").resolve())

    def test_diff_respects_config_ignore(self, tmp_path: Path) -> None:
        (tmp_path / ".env.dev").write_text("BUILD_ID=123\nPORT=8080\n", encoding="utf-8")
        (tmp_path / ".env.staging").write_text("BUILD_ID=456\nPORT=8080\n", encoding="utf-8")

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=[".env.dev"]),
                "staging": EnvironmentConfig(paths=[".env.staging"]),
            },
            ignore=["BUILD_ID"],
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)
        result = diff_env_vars(dev, staging, ignore=config.ignore)

        assert result.total_compared == 1
        assert result.diffs[0].key == "PORT"
        assert result.diffs[0].kind == DiffKind.MATCH
        assert not result.has_drift

    def test_diff_mixed_sources(self, tmp_path: Path) -> None:
        """Diff profiles that aggregate vars from .env + docker-compose."""
        (tmp_path / ".env").write_text("NGINX_HOST=localhost\nONLY_ENV=1\n", encoding="utf-8")
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    image: nginx:1.25\n"
            "    environment:\n"
            "      - NGINX_HOST=localhost\n"
            "      - ONLY_DOCKER=2\n",
            encoding="utf-8",
        )
        (tmp_path / ".env.prod").write_text("NGINX_HOST=nginx.internal\nONLY_ENV=1\n", encoding="utf-8")

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=[".env", "docker-compose.yml"]),
                "prod": EnvironmentConfig(paths=[".env.prod"]),
            },
        )
        dev = build_profile(config, "dev", root=tmp_path)
        prod = build_profile(config, "prod", root=tmp_path)
        result = diff_env_vars(dev, prod)

        kinds = {d.key: d.kind for d in result.diffs}
        # NGINX_HOST differs (localhost vs nginx.internal); ONLY_ENV matches;
        # ONLY_DOCKER exists only in the base profile (dev) → missing.
        assert kinds["NGINX_HOST"] == DiffKind.CHANGED
        assert kinds["ONLY_ENV"] == DiffKind.MATCH
        assert kinds["ONLY_DOCKER"] == DiffKind.MISSING
