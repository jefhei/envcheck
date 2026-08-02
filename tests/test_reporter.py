"""Tests for the Rich terminal reporter (color-coded diff tables)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from rich.table import Table

from envcheck.config import EnvironmentConfig, EnvcheckConfig
from envcheck.diff import (
    DiffKind,
    EnvVarDiffResult,
    VarDiff,
    diff_env_vars,
)
from envcheck.profile import EnvironmentProfile, build_profile
from envcheck.reporter import (
    ENV_VAR_STYLES,
    SERVICE_STYLES,
    build_verdict,
    print_env_var_diff,
    print_report,
    print_service_diff,
    render_env_var_diff,
    render_service_diff,
    summarize_env_var_diff,
    summarize_service_diff,
)
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry
from envcheck.services import (
    ServiceDiffKind,
    ServiceDiffResult,
    diff_services,
)


# ===========================================================================
#  Color scheme
# ===========================================================================


class TestColorScheme:
    """PRD F4: green=ok, yellow=diff, red=missing, gray=extra."""

    def test_env_var_styles_match_prd(self) -> None:
        assert ENV_VAR_STYLES[DiffKind.MATCH] == "green"
        # both "diff" flavors are yellow
        assert ENV_VAR_STYLES[DiffKind.CHANGED] == "yellow"
        assert ENV_VAR_STYLES[DiffKind.TYPE_CHANGED] == "yellow"
        assert ENV_VAR_STYLES[DiffKind.MISSING] == "red"
        assert ENV_VAR_STYLES[DiffKind.EXTRA] == "bright_black"

    def test_service_styles_match_prd(self) -> None:
        assert SERVICE_STYLES[ServiceDiffKind.MATCH] == "green"
        assert SERVICE_STYLES[ServiceDiffKind.VERSION_CHANGED] == "yellow"
        assert SERVICE_STYLES[ServiceDiffKind.IMAGE_CHANGED] == "yellow"
        assert SERVICE_STYLES[ServiceDiffKind.MISSING] == "red"
        assert SERVICE_STYLES[ServiceDiffKind.EXTRA] == "bright_black"


# ===========================================================================
#  Helpers
# ===========================================================================


def _env_profile(name: str, variables: dict[str, str]) -> EnvironmentProfile:
    """Build an EnvironmentProfile from raw key → value pairs."""
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


def _ansi_console() -> tuple[Console, io.StringIO]:
    """A console that emits ANSI codes into a StringIO buffer."""
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        color_system="standard",
        width=120,
    )
    return console, buffer


def _plain_console() -> tuple[Console, io.StringIO]:
    """A console that emits plain (colorless) text into a StringIO buffer."""
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=120)
    return console, buffer


#: One VarDiff of every kind, sorted by key, for rendering tests.
_ALL_KINDS = [
    VarDiff(key="DEBUG", kind=DiffKind.TYPE_CHANGED, base_value="1", target_value="true",
            base_type="int", target_type="bool"),
    VarDiff(key="FEATURE_FLAG", kind=DiffKind.MISSING, base_value="true",
            base_type="bool", base_source="/p/.env.dev"),
    VarDiff(key="LOG_LEVEL", kind=DiffKind.CHANGED, base_value="info", target_value="debug",
            base_type="string", target_type="string"),
    VarDiff(key="ONLY_STAGING", kind=DiffKind.EXTRA, target_value="present",
            target_type="string", target_source="/p/.env.staging"),
    VarDiff(key="PORT", kind=DiffKind.MATCH, base_value="8080", target_value="8080",
            base_type="int", target_type="int"),
]


def _full_env_result() -> EnvVarDiffResult:
    return EnvVarDiffResult(base_env="dev", target_env="staging", diffs=_ALL_KINDS)


# ===========================================================================
#  render_env_var_diff — structure
# ===========================================================================


class TestRenderEnvVarDiff:
    """Verify the table structure and column headers."""

    def test_returns_rich_table(self) -> None:
        table = render_env_var_diff(_full_env_result())
        assert isinstance(table, Table)
        assert len(table.columns) == 4

    def test_columns_named_after_environments(self) -> None:
        table = render_env_var_diff(_full_env_result())
        headers = [c.header for c in table.columns]
        assert headers == ["Variable", "Status", "dev", "staging"]

    def test_title_contains_environments(self) -> None:
        table = render_env_var_diff(_full_env_result())
        assert table.title is not None
        assert "dev" in table.title
        assert "staging" in table.title

    def test_one_row_per_diff(self) -> None:
        table = render_env_var_diff(_full_env_result())
        assert len(table.rows) == len(_ALL_KINDS)

    def test_empty_result_renders(self) -> None:
        result = EnvVarDiffResult(base_env="dev", target_env="staging", diffs=[])
        table = render_env_var_diff(result)
        assert len(table.rows) == 0


# ===========================================================================
#  render_env_var_diff — colors (ANSI codes)
# ===========================================================================


class TestEnvVarColors:
    """Each DiffKind row must carry the PRD color."""

    def _render_with_colors(self, result: EnvVarDiffResult) -> str:
        console, buffer = _ansi_console()
        console.print(render_env_var_diff(result))
        return buffer.getvalue()

    def test_match_rows_are_green(self) -> None:
        out = self._render_with_colors(_full_env_result())
        assert "\x1b[32m" in out  # green

    def test_changed_rows_are_yellow(self) -> None:
        out = self._render_with_colors(_full_env_result())
        assert "\x1b[33m" in out  # yellow

    def test_type_changed_rows_are_yellow(self) -> None:
        out = self._render_with_colors(_full_env_result())
        assert "\x1b[33m" in out  # yellow (shared "diff" style)

    def test_missing_rows_are_red(self) -> None:
        out = self._render_with_colors(_full_env_result())
        assert "\x1b[31m" in out  # red

    def test_extra_rows_are_gray(self) -> None:
        out = self._render_with_colors(_full_env_result())
        assert "\x1b[90m" in out  # bright black == gray

    def test_clean_result_has_green_but_no_drift_colors(self) -> None:
        result = EnvVarDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MATCH],
        )
        out = self._render_with_colors(result)
        assert "\x1b[32m" in out
        assert "\x1b[33m" not in out
        assert "\x1b[31m" not in out
        assert "\x1b[90m" not in out


# ===========================================================================
#  render_env_var_diff — content
# ===========================================================================


class TestEnvVarContent:
    """Status labels and value cells render correctly."""

    def _render_plain(self, result: EnvVarDiffResult) -> str:
        console, buffer = _plain_console()
        console.print(render_env_var_diff(result))
        return buffer.getvalue()

    def test_status_labels_present(self) -> None:
        out = self._render_plain(_full_env_result())
        assert "ok" in out
        assert "changed" in out
        assert "type changed" in out
        assert "missing" in out
        assert "extra" in out

    def test_keys_and_values_present(self) -> None:
        out = self._render_plain(_full_env_result())
        for key in ("PORT", "LOG_LEVEL", "DEBUG", "FEATURE_FLAG", "ONLY_STAGING"):
            assert key in out
        assert "8080" in out
        assert "info" in out
        assert "debug" in out

    def test_missing_row_shows_base_value_and_dash(self) -> None:
        out = self._render_plain(_full_env_result())
        # FEATURE_FLAG is missing in staging → base value shown, target is "—"
        assert "true" in out
        assert "—" in out

    def test_extra_row_shows_dash_and_target_value(self) -> None:
        out = self._render_plain(_full_env_result())
        assert "present" in out
        assert "—" in out

    def test_type_changed_cells_annotate_types(self) -> None:
        out = self._render_plain(_full_env_result())
        assert "1 (int)" in out
        assert "true (bool)" in out

    def test_long_values_truncated(self) -> None:
        long_value = "x" * 200
        result = EnvVarDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[
                VarDiff(key="TOKEN", kind=DiffKind.CHANGED, base_value=long_value,
                        target_value="short", base_type="string", target_type="string"),
            ],
        )
        out = self._render_plain(result)
        assert "x" * 200 not in out
        assert "short" in out


# ===========================================================================
#  render_service_diff
# ===========================================================================


class TestRenderServiceDiff:
    """Service tables: structure, colors, content."""

    def _render_ansi(self, result: ServiceDiffResult) -> str:
        console, buffer = _ansi_console()
        console.print(render_service_diff(result))
        return buffer.getvalue()

    def _render_plain(self, result: ServiceDiffResult) -> str:
        console, buffer = _plain_console()
        console.print(render_service_diff(result))
        return buffer.getvalue()

    def test_returns_rich_table(self) -> None:
        result = ServiceDiffResult(base_env="dev", target_env="staging", diffs=[])
        assert isinstance(render_service_diff(result), Table)

    def test_columns(self) -> None:
        result = ServiceDiffResult(base_env="dev", target_env="staging", diffs=[])
        headers = [c.header for c in render_service_diff(result).columns]
        assert headers == ["Service", "Status", "dev", "staging"]

    def test_version_changed_row_is_yellow_with_images(self) -> None:
        from envcheck.services import ServiceDiff

        result = ServiceDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[
                ServiceDiff(
                    service="postgres",
                    kind=ServiceDiffKind.VERSION_CHANGED,
                    base_image="postgres:16",
                    target_image="postgres:15",
                    base_version="16",
                    target_version="15",
                ),
            ],
        )
        ansi = self._render_ansi(result)
        assert "\x1b[33m" in ansi  # yellow

        plain = self._render_plain(result)
        assert "postgres" in plain
        assert "postgres:16" in plain
        assert "postgres:15" in plain
        assert "version changed" in plain

    def test_missing_and_extra_colors(self) -> None:
        from envcheck.services import ServiceDiff

        result = ServiceDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[
                ServiceDiff(service="redis", kind=ServiceDiffKind.MISSING,
                            base_image="redis:7.2", base_version="7.2"),
                ServiceDiff(service="worker", kind=ServiceDiffKind.EXTRA,
                            target_image="myapp:1.0", target_version="1.0"),
            ],
        )
        ansi = self._render_ansi(result)
        assert "\x1b[31m" in ansi  # missing → red
        assert "\x1b[90m" in ansi  # extra → gray


# ===========================================================================
#  Summaries + verdict
# ===========================================================================


class TestSummaries:
    def test_env_var_summary_counts(self) -> None:
        summary = summarize_env_var_diff(_full_env_result())
        assert summary == (
            "Compared 5 variables: 1 ok, 1 changed, 1 type-changed, 1 missing, 1 extra"
        )

    def test_env_var_summary_clean(self) -> None:
        result = EnvVarDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MATCH],
        )
        assert summarize_env_var_diff(result) == "Compared 1 variables: 1 ok"

    def test_env_var_summary_empty(self) -> None:
        result = EnvVarDiffResult(base_env="dev", target_env="staging", diffs=[])
        assert summarize_env_var_diff(result) == "Compared 0 variables"

    def test_service_summary(self) -> None:
        from envcheck.services import ServiceDiff

        result = ServiceDiffResult(
            base_env="dev",
            target_env="staging",
            diffs=[
                ServiceDiff(service="postgres", kind=ServiceDiffKind.VERSION_CHANGED,
                            base_image="postgres:16", target_image="postgres:15"),
                ServiceDiff(service="redis", kind=ServiceDiffKind.MATCH,
                            base_image="redis:7.2", target_image="redis:7.2"),
            ],
        )
        assert summarize_service_diff(result) == "Compared 2 services: 1 ok, 1 version-changed"


class TestBuildVerdict:
    def test_sync_when_no_drift(self) -> None:
        clean = EnvVarDiffResult(
            base_env="dev", target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MATCH],
        )
        message, style = build_verdict(clean, None)
        assert "in sync" in message
        assert "green" in style

    def test_sync_when_both_results_none(self) -> None:
        message, style = build_verdict(None, None)
        assert "in sync" in message
        assert "green" in style

    def test_drift_message_counts_env_and_service_drift(self) -> None:
        env_result = _full_env_result()  # 4 drift items
        from envcheck.services import ServiceDiff

        svc_result = ServiceDiffResult(
            base_env="dev", target_env="staging",
            diffs=[ServiceDiff(service="postgres", kind=ServiceDiffKind.VERSION_CHANGED,
                               base_image="postgres:16", target_image="postgres:15")],
        )
        message, style = build_verdict(env_result, svc_result)
        assert message == "⚠️ Drift detected: 5 differences"
        assert "yellow" in style

    def test_drift_singular(self) -> None:
        result = EnvVarDiffResult(
            base_env="dev", target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MISSING],
        )
        message, _ = build_verdict(result, None)
        assert message == "⚠️ Drift detected: 1 difference"


# ===========================================================================
#  Printing helpers
# ===========================================================================


class TestPrinting:
    def test_print_env_var_diff_writes_to_console(self) -> None:
        console, buffer = _plain_console()
        print_env_var_diff(_full_env_result(), console=console)
        out = buffer.getvalue()
        assert "Environment variables — dev vs staging" in out
        assert "Compared 5 variables" in out

    def test_print_service_diff_writes_to_console(self) -> None:
        from envcheck.services import ServiceDiff

        result = ServiceDiffResult(
            base_env="dev", target_env="staging",
            diffs=[ServiceDiff(service="postgres", kind=ServiceDiffKind.MATCH,
                               base_image="postgres:16", target_image="postgres:16")],
        )
        console, buffer = _plain_console()
        print_service_diff(result, console=console)
        out = buffer.getvalue()
        assert "Docker services — dev vs staging" in out
        assert "Compared 1 services" in out

    def test_print_report_renders_everything(self) -> None:
        from envcheck.services import ServiceDiff

        env_result = _full_env_result()
        svc_result = ServiceDiffResult(
            base_env="dev", target_env="staging",
            diffs=[ServiceDiff(service="postgres", kind=ServiceDiffKind.VERSION_CHANGED,
                               base_image="postgres:16", target_image="postgres:15")],
        )
        console, buffer = _plain_console()
        print_report(env_result, svc_result, console=console)
        out = buffer.getvalue()
        assert "Environment variables — dev vs staging" in out
        assert "Docker services — dev vs staging" in out
        assert "Compared 5 variables" in out
        assert "Compared 1 services" in out
        assert "Drift detected: 5 differences" in out

    def test_print_report_clean_verdict(self) -> None:
        clean = EnvVarDiffResult(
            base_env="dev", target_env="staging",
            diffs=[d for d in _ALL_KINDS if d.kind == DiffKind.MATCH],
        )
        console, buffer = _plain_console()
        print_report(clean, None, console=console)
        assert "Environments are in sync" in buffer.getvalue()

    def test_print_report_uses_default_console(self) -> None:
        # Must not raise when no console is passed (default Console()).
        print_report(_full_env_result(), None)


# ===========================================================================
#  Integration — real scanners → diff → reporter
# ===========================================================================


class TestReporterIntegration:
    """End-to-end: fixture project → profiles → diff → rendered tables."""

    def test_full_report_from_fixture_project(self, tmp_path: Path) -> None:
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

        config = EnvcheckConfig(
            environments={
                "dev": EnvironmentConfig(paths=[".env.dev", "docker-compose.dev.yml"]),
                "staging": EnvironmentConfig(paths=[".env.staging", "docker-compose.staging.yml"]),
            },
        )
        dev = build_profile(config, "dev", root=tmp_path)
        staging = build_profile(config, "staging", root=tmp_path)

        env_result = diff_env_vars(dev, staging)
        svc_result = diff_services(dev, staging)

        console, buffer = _ansi_console()
        print_report(env_result, svc_result, console=console)
        out = buffer.getvalue()

        # All four PRD colors present in a real drifted report
        assert "\x1b[32m" in out  # green — PORT match
        assert "\x1b[33m" in out  # yellow — DATABASE_URL changed, DEBUG type-changed,
        #                          #          postgres version-changed
        assert "\x1b[31m" in out  # red — FEATURE_FLAG missing
        assert "\x1b[90m" in out  # gray — ONLY_STAGING extra

        # Content sanity (plain console → no ANSI codes to break matches)
        console2, buffer2 = _plain_console()
        print_report(env_result, svc_result, console=console2)
        plain = buffer2.getvalue()
        assert "DATABASE_URL" in plain
        assert "postgres:16" in plain
        assert "postgres:15" in plain
        # 4 drifted vars (DATABASE_URL, DEBUG, FEATURE_FLAG, ONLY_STAGING)
        # + 1 drifted service (postgres) = 5 differences
        assert "Drift detected: 5 differences" in plain

    def test_drift_counts_from_real_diff(self, tmp_path: Path) -> None:
        """Exact drift arithmetic on real scanner output."""
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
        result = diff_env_vars(dev, staging)

        # A matches; B missing (base only); C extra (target only) → 2 drift
        console, buffer = _plain_console()
        print_env_var_diff(result, console=console)
        out = buffer.getvalue()
        assert "Compared 3 variables: 1 ok, 1 missing, 1 extra" in out

        console2, buffer2 = _plain_console()
        print_report(result, None, console=console2)
        assert "Drift detected: 2 differences" in buffer2.getvalue()
