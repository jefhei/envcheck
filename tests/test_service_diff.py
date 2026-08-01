"""Tests for the service version diff engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from envcheck.config import EnvironmentConfig, EnvcheckConfig, ServiceConfig
from envcheck.profile import EnvironmentProfile, build_profile
from envcheck.scanners.docker import DockerComposeScanResult, DockerScanResult
from envcheck.services import (
    ParsedImage,
    ServiceDiffKind,
    ServiceDiffResult,
    diff_services,
    parse_image,
)


# ===========================================================================
#  parse_image
# ===========================================================================


class TestParseImage:
    """Verify Docker image references are split into name/tag/digest."""

    @pytest.mark.parametrize(
        ("image", "name", "tag", "digest"),
        [
            # simple name:tag
            ("postgres:16", "postgres", "16", None),
            ("postgres:15", "postgres", "15", None),
            ("nginx:1.25-alpine", "nginx", "1.25-alpine", None),
            ("redis:7.2", "redis", "7.2", None),
            # name only → no tag (implicit latest)
            ("postgres", "postgres", None, None),
            ("ubuntu", "ubuntu", None, None),
            # registry-qualified names
            ("ghcr.io/org/app:2.1", "ghcr.io/org/app", "2.1", None),
            ("docker.io/library/nginx:latest", "docker.io/library/nginx", "latest", None),
            # registry with port — the port must NOT become the tag
            ("localhost:5000/postgres:16", "localhost:5000/postgres", "16", None),
            ("localhost:5000/img", "localhost:5000/img", None, None),
            # digests
            ("postgres@sha256:abc123", "postgres", None, "sha256:abc123"),
            ("postgres:16@sha256:abc123", "postgres", "16", "sha256:abc123"),
            # whitespace is trimmed
            ("  postgres:16  ", "postgres", "16", None),
        ],
    )
    def test_parse(self, image: str, name: str, tag: str | None, digest: str | None) -> None:
        parsed = parse_image(image)
        assert parsed.name == name
        assert parsed.tag == tag
        assert parsed.digest == digest

    def test_parse_returns_parsed_image_model(self) -> None:
        parsed = parse_image("postgres:16")
        assert isinstance(parsed, ParsedImage)

    def test_empty_image_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty"):
            parse_image("")
        with pytest.raises(ValueError, match="Empty"):
            parse_image("   ")

    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("postgres:16", "16"),
            ("nginx:1.25-alpine", "1.25-alpine"),
            # no tag → latest
            ("postgres", "latest"),
            ("localhost:5000/img", "latest"),
            # digest used when no tag
            ("postgres@sha256:abc123", "sha256:abc123"),
            # tag wins over digest
            ("postgres:16@sha256:abc123", "16"),
        ],
    )
    def test_version_property(self, image: str, expected: str) -> None:
        assert parse_image(image).version == expected


# ===========================================================================
#  diff_services — model-level tests
# ===========================================================================


def _profile(name: str, services: dict[str, str], source: str = "/p/docker-compose.yml") -> EnvironmentProfile:
    """Build an EnvironmentProfile with a single compose result."""
    return EnvironmentProfile(
        name=name,
        docker_result=DockerScanResult(
            compose_results=[
                DockerComposeScanResult(
                    source=source,
                    services=services,
                    total_services=len(services),
                ),
            ],
        ),
    )


class TestDiffServices:
    """Verify service classification: match / version / image / missing / extra."""

    def test_empty_profiles(self) -> None:
        result = diff_services(_profile("dev", {}), _profile("staging", {}))
        assert isinstance(result, ServiceDiffResult)
        assert result.base_env == "dev"
        assert result.target_env == "staging"
        assert result.diffs == []
        assert result.total_compared == 0
        assert result.drift_count == 0
        assert not result.has_drift

    def test_identical_services_all_match(self) -> None:
        base = _profile("dev", {"postgres": "postgres:16", "redis": "redis:7.2"})
        target = _profile("staging", {"postgres": "postgres:16", "redis": "redis:7.2"})
        result = diff_services(base, target)

        assert result.total_compared == 2
        assert len(result.matches) == 2
        assert result.version_changed == []
        assert result.image_changed == []
        assert result.missing == []
        assert result.extra == []
        assert result.drift_count == 0
        assert not result.has_drift
        assert all(d.kind == ServiceDiffKind.MATCH for d in result.diffs)

    def test_version_changed_detected(self) -> None:
        """The headline case: postgres:16 vs postgres:15."""
        base = _profile("dev", {"postgres": "postgres:16"})
        target = _profile("staging", {"postgres": "postgres:15"})
        result = diff_services(base, target)

        assert len(result.version_changed) == 1
        d = result.version_changed[0]
        assert d.kind == ServiceDiffKind.VERSION_CHANGED
        assert d.service == "postgres"
        assert d.base_image == "postgres:16"
        assert d.target_image == "postgres:15"
        assert d.base_version == "16"
        assert d.target_version == "15"
        assert result.has_drift
        assert result.drift_count == 1

    def test_version_changed_alpine_variant(self) -> None:
        base = _profile("dev", {"web": "nginx:1.25"})
        target = _profile("staging", {"web": "nginx:1.25-alpine"})
        result = diff_services(base, target)
        assert len(result.version_changed) == 1
        assert result.version_changed[0].base_version == "1.25"
        assert result.version_changed[0].target_version == "1.25-alpine"

    def test_untagged_vs_latest_is_match(self) -> None:
        base = _profile("dev", {"db": "postgres"})
        target = _profile("staging", {"db": "postgres:latest"})
        result = diff_services(base, target)
        assert len(result.matches) == 1
        assert result.matches[0].base_version == "latest"
        assert result.matches[0].target_version == "latest"
        assert not result.has_drift

    def test_image_changed_detected(self) -> None:
        base = _profile("dev", {"db": "postgres:16"})
        target = _profile("staging", {"db": "mysql:8.0"})
        result = diff_services(base, target)

        assert len(result.image_changed) == 1
        d = result.image_changed[0]
        assert d.kind == ServiceDiffKind.IMAGE_CHANGED
        assert d.base_image == "postgres:16"
        assert d.target_image == "mysql:8.0"
        assert result.has_drift

    def test_missing_and_extra(self) -> None:
        base = _profile("dev", {"postgres": "postgres:16", "redis": "redis:7.2"})
        target = _profile("staging", {"postgres": "postgres:16", "worker": "myapp:1.0"})
        result = diff_services(base, target)

        kinds = {d.service: d.kind for d in result.diffs}
        assert kinds["postgres"] == ServiceDiffKind.MATCH
        assert kinds["redis"] == ServiceDiffKind.MISSING
        assert kinds["worker"] == ServiceDiffKind.EXTRA

        missing = result.missing
        assert len(missing) == 1
        assert missing[0].service == "redis"
        assert missing[0].base_image == "redis:7.2"
        assert missing[0].base_version == "7.2"
        assert missing[0].target_image is None

        extra = result.extra
        assert len(extra) == 1
        assert extra[0].service == "worker"
        assert extra[0].target_image == "myapp:1.0"
        assert extra[0].target_version == "1.0"
        assert extra[0].base_image is None

        assert result.has_drift
        assert result.drift_count == 2
        assert result.total_compared == 3

    def test_sources_captured(self) -> None:
        base = _profile(
            "dev",
            {"postgres": "postgres:16"},
            source="/p/compose.dev.yml",
        )
        target = _profile(
            "staging",
            {"postgres": "postgres:15"},
            source="/p/compose.staging.yml",
        )
        result = diff_services(base, target)

        d = result.diffs[0]
        assert d.base_source == "/p/compose.dev.yml"
        assert d.target_source == "/p/compose.staging.yml"

    def test_missing_captures_base_source_only(self) -> None:
        base = _profile("dev", {"redis": "redis:7.2"}, source="/p/compose.dev.yml")
        target = _profile("staging", {})
        result = diff_services(base, target)
        assert result.missing[0].base_source == "/p/compose.dev.yml"
        assert result.missing[0].target_source is None

    def test_diffs_sorted_by_service_name(self) -> None:
        base = _profile("dev", {"zeta": "z:1", "alpha": "a:1", "mike": "m:1"})
        target = _profile("staging", {})
        result = diff_services(base, target)
        names = [d.service for d in result.diffs]
        assert names == sorted(names)

    def test_profiles_without_docker_results(self) -> None:
        base = EnvironmentProfile(name="dev")
        target = EnvironmentProfile(name="staging")
        result = diff_services(base, target)
        assert result.diffs == []
        assert not result.has_drift


# ===========================================================================
#  diff_services — tracked services (config-driven)
# ===========================================================================


class TestTrackedServices:
    """Verify the ``services:`` section of .envcheck.yaml is honored."""

    def test_tracked_restricts_comparison(self) -> None:
        base = _profile("dev", {"postgres": "postgres:16", "redis": "redis:7.2"})
        target = _profile("staging", {"postgres": "postgres:15", "redis": "redis:7.2"})
        tracked = {"postgres": ServiceConfig()}

        result = diff_services(base, target, tracked=tracked)
        assert result.total_compared == 1
        assert result.diffs[0].service == "postgres"
        assert result.diffs[0].kind == ServiceDiffKind.VERSION_CHANGED

    def test_tracked_service_missing_from_both_is_skipped(self) -> None:
        base = _profile("dev", {"redis": "redis:7.2"})
        target = _profile("staging", {"redis": "redis:7.2"})
        tracked = {"postgres": ServiceConfig(), "redis": ServiceConfig()}

        result = diff_services(base, target, tracked=tracked)
        assert result.total_compared == 1
        assert result.diffs[0].service == "redis"

    def test_tracked_service_missing_in_target(self) -> None:
        base = _profile("dev", {"postgres": "postgres:16"})
        target = _profile("staging", {})
        tracked = {"postgres": ServiceConfig()}

        result = diff_services(base, target, tracked=tracked)
        assert len(result.missing) == 1
        assert result.missing[0].service == "postgres"

    def test_unsupported_version_field_raises(self) -> None:
        base = _profile("dev", {"postgres": "postgres:16"})
        target = _profile("staging", {"postgres": "postgres:15"})
        tracked = {"postgres": ServiceConfig(version_field="environment")}

        with pytest.raises(ValueError, match="version_field"):
            diff_services(base, target, tracked=tracked)

    def test_path_restriction(self) -> None:
        """path: only consider the service when it lives in that compose file."""
        base = _profile(
            "dev",
            {"postgres": "postgres:16"},
            source="/p/compose.dev.yml",
        )
        target = _profile(
            "staging",
            {"postgres": "postgres:15"},
            source="/p/compose.staging.yml",
        )
        tracked = {"postgres": ServiceConfig(path="/p/compose.staging.yml")}

        # Service is not defined in the configured file for dev → treated as
        # absent in base, present in target → extra.
        result = diff_services(base, target, tracked=tracked)
        assert len(result.extra) == 1
        assert result.extra[0].service == "postgres"

        tracked_both = {"postgres": ServiceConfig(path="/p/compose.dev.yml")}
        result = diff_services(base, target, tracked=tracked_both)
        assert len(result.missing) == 1
        assert result.missing[0].service == "postgres"


# ===========================================================================
#  diff_services — integration tests (real scanners + profile builder)
# ===========================================================================


class TestDiffServicesIntegration:
    """End-to-end: scan fixture projects, build profiles, then diff services."""

    def test_diff_built_profiles(self, tmp_path: Path) -> None:
        (tmp_path / "docker-compose.dev.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "  redis:\n"
            "    image: redis:7.2\n"
            "  web:\n"
            "    image: nginx:1.25\n",
            encoding="utf-8",
        )
        (tmp_path / "docker-compose.staging.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:15\n"
            "  redis:\n"
            "    image: redis:7.2\n"
            "  worker:\n"
            "    image: myapp:1.0\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=["docker-compose.dev.yml"]),
                "staging": EnvironmentConfig(paths=["docker-compose.staging.yml"]),
            },
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)
        result = diff_services(dev, staging)

        kinds = {d.service: d.kind for d in result.diffs}
        assert kinds["postgres"] == ServiceDiffKind.VERSION_CHANGED
        assert kinds["redis"] == ServiceDiffKind.MATCH
        assert kinds["web"] == ServiceDiffKind.MISSING
        assert kinds["worker"] == ServiceDiffKind.EXTRA

        assert result.has_drift
        assert result.drift_count == 3
        assert result.total_compared == 4

        # Source paths come from the real scanner (absolute, resolved)
        postgres = next(d for d in result.diffs if d.service == "postgres")
        assert postgres.base_source == str((tmp_path / "docker-compose.dev.yml").resolve())
        assert postgres.target_source == str((tmp_path / "docker-compose.staging.yml").resolve())

    def test_diff_built_profiles_with_tracked_config(self, tmp_path: Path) -> None:
        """Tracked services from config narrow the comparison."""
        (tmp_path / "docker-compose.dev.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "  redis:\n"
            "    image: redis:7.2\n",
            encoding="utf-8",
        )
        (tmp_path / "docker-compose.staging.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    image: postgres:15\n"
            "  redis:\n"
            "    image: redis:7.2\n",
            encoding="utf-8",
        )

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=["docker-compose.dev.yml"]),
                "staging": EnvironmentConfig(paths=["docker-compose.staging.yml"]),
            },
            services={"postgres": ServiceConfig()},
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)
        result = diff_services(dev, staging, tracked=config.services)

        assert result.total_compared == 1
        assert result.diffs[0].service == "postgres"
        assert result.diffs[0].kind == ServiceDiffKind.VERSION_CHANGED
